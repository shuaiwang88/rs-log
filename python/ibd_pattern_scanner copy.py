"""
ibd_pattern_scanner.py

Python implementation of the TradingView IBD Pattern Scanner (drw_pattern_scanner.pine).
Scans all ticker parquet data in `ticker_cache/` to detect MarketSmith / IBD patterns:
  1. Base (Deep Base)
  2. Flat Base
  3. Cup
  4. Cup + Handle
  5. Double Bottom
  6. High Tight Flag (HTF)
  7. 6-Wk Flat Base

Calculates technical metrics & scoring matching drw_pattern_scanner.pine:
  - In-Base / Post-BO status & Days in Base / Bars Since Breakout
  - Distance to Pivot %
  - % Off 52W High
  - RS New High count & signals
  - Before-BO Score (0-6) & Post-BO Score (0-6) & Composite Score (0-12)
    (Pocket Pivot, Shakeout, MA Touch, Volume Dry-Up, RS New High, Upside Reversal)
"""

import os
import sys
import glob
import warnings
warnings.filterwarnings("ignore")
import json
import time
import math
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import pickle

# Root project directory
ROOT_DIR = Path(__file__).resolve().parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_JSON_PATH = ROOT_DIR / "python" / "ibd_pattern_results.json"
MODEL_PATH = ROOT_DIR / "python" / "pattern_model.pkl"

PATTERN_MODEL = None
if MODEL_PATH.exists():
    try:
        with open(MODEL_PATH, "rb") as f:
            PATTERN_MODEL = pickle.load(f)
    except Exception:
        PATTERN_MODEL = None


def calculate_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, length: int = 14) -> np.ndarray:
    """Calculate Average True Range (ATR)."""
    n = len(closes)
    if n == 0:
        return np.zeros(0)
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    
    tr1 = highs - lows
    tr2 = np.abs(highs - prev_close)
    tr3 = np.abs(lows - prev_close)
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    
    alpha = 1.0 / length
    atr = np.zeros(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    return atr


def find_pivots(highs: np.ndarray, lows: np.ndarray, left: int = 5, right: int = 5):
    """Find pivot highs and pivot lows."""
    n = len(highs)
    pivot_highs = {} # bar_idx -> price
    pivot_lows = {}  # bar_idx -> price
    
    for i in range(left, n - right):
        h = highs[i]
        l = lows[i]
        
        # Check pivot high
        is_ph = True
        for j in range(i - left, i + right + 1):
            if j != i and highs[j] >= h:
                is_ph = False
                break
        if is_ph:
            pivot_highs[i] = h
            
        # Check pivot low
        is_pl = True
        for j in range(i - left, i + right + 1):
            if j != i and lows[j] <= l:
                is_pl = False
                break
        if is_pl:
            pivot_lows[i] = l
            
    return pivot_highs, pivot_lows


# Patterns a VCP is allowed to be reported inside. VCP is a SUB-pattern: it describes how
# supply dries up within a base, so it qualifies the host pattern rather than replacing it.
# (A Double Bottom is excluded - its defining shape is the second undercut, which is the
# opposite of a monotonically tightening contraction sequence.)
VCP_HOST_PATTERNS = {'Cup+Handle', 'Cup', 'Flat Base', '6-Wk Flat', 'Consolidation'}

# Overrides for detect_vcp(), applied by scan_single_ticker. Defaults below match
# drw_vcp.pine exactly. Measured against the 163 Breakaway Gap events, counting a VCP that
# is ready anywhere in [-10, +2] bars around IBD's breakout:
#     pine defaults (final <= 10%, trend 20%) .....  7 ready /  5 breakouts
#     maxFinalDepth 15 ............................ 15 / 13
#     maxFinalDepth 15 + useTrend False ........... 28 / 24
#     maxFinalDepth 20 + useTrend False ........... 29 / 25
# The prior-uptrend filter is the single biggest gate: it wants a 20% run in the previous
# 60 bars, and plenty of IBD bases form coming out of a correction instead.
VCP_PARAMS = {}

# Report a second, lower buy point when the base's top two candidate pivot highs differ by
# more than this. Below it the two levels are close enough that the choice is immaterial.
PIVOT_AMBIGUITY_PCT = 5.0

# How far back to gather layered readings. A base is identified long before it breaks out
# (Cup Without Handle a median 66 bars ahead, Flat Base 86), and the breakout bar is where
# the instantaneous label is least reliable, so the recent window is more informative than
# the final bar alone. 20 bars = about a month.
PATTERN_WINDOW_BARS = 20

# Two readings quoting buy points within this of each other are the same decision, so the
# lower-ranked name is folded into `also_reads_as` rather than listed separately.
PIVOT_SAME_PCT = 1.0


def detect_vcp(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
               pivot_highs: dict, pivot_lows: dict, pivLen: int = 5,
               minLegs: int = 2, maxLegs: int = 5, minLegBars: int = 5,
               maxFirstDepth: float = 35.0, maxFinalDepth: float = 10.0,
               shrinkPct: float = 110.0, useTrend: bool = True,
               trendBars: int = 60, trendGain: float = 20.0,
               hiBufPct: float = 5.0, maxBaseBars: int = 300,
               hiResetBars: int = 150, brkOnClose: bool = True):
    """Volatility Contraction Pattern - port of the drw_vcp.pine state machine.

    Minervini's VCP: a prior uptrend, then 2-5 pullbacks where each contraction makes a
    HIGHER low and is SHALLOWER than the one before, the last one tight. The pivot is the
    high of the final contraction.

    Runs standalone over the whole series and returns per-bar arrays, so it can never
    perturb the base state machine - VCP only ever annotates whatever base is already there.
    `detail` carries the contraction legs (bar/price/depth) so the formation can be drawn
    the way drw_vcp.pine paints it.
    """
    n = len(highs)
    hiBuf = 1.0 + hiBufPct / 100.0
    shrink = shrinkPct / 100.0
    effMaxLegs = max(minLegs, maxLegs)
    P_IDLE, P_SEEK_LOW, P_SEEK_HIGH = 0, 1, 2

    phase = P_IDLE
    baseStartBar = actHigh = actHighBar = pivot = None
    ready = False
    hs, hBars, ls, lBars, depths = [], [], [], [], []

    out = {
        'ready': np.zeros(n, dtype=bool),
        'active': np.zeros(n, dtype=bool),
        'legs': np.zeros(n, dtype=int),
        'pivot': np.full(n, np.nan),
        'last_depth': np.full(n, np.nan),
        'breakout': np.zeros(n, dtype=bool),
        'detail': [()] * n,
    }
    cur_detail = ()

    def snapshot():
        return tuple({'high_bar': int(hBars[k]), 'high': float(hs[k]),
                      'low_bar': int(lBars[k]), 'low': float(ls[k]),
                      'depth_pct': float(depths[k])} for k in range(len(hs)))

    def reset():
        nonlocal phase, baseStartBar, actHigh, actHighBar, pivot, ready, cur_detail
        hs.clear(); hBars.clear(); ls.clear(); lBars.clear(); depths.clear()
        phase = P_IDLE
        baseStartBar = actHigh = actHighBar = pivot = None
        ready = False
        cur_detail = ()

    def anchor(hi, hiBar):
        nonlocal phase, baseStartBar, actHigh, actHighBar
        baseStartBar = hiBar
        actHigh = hi
        actHighBar = hiBar
        phase = P_SEEK_LOW

    for i in range(n):
        pvBar = i - pivLen
        ph = pivot_highs.get(pvBar)
        pl = pivot_lows.get(pvBar)

        histOK = i > pivLen + trendBars
        trendOK = (not useTrend) or (
            histOK and (closes[i - pivLen] / closes[i - pivLen - trendBars] - 1.0) * 100.0 >= trendGain)

        # real-time invalidation: undercut, excessive depth, stale base or stale high
        if phase != P_IDLE:
            undercut = bool(ls) and lows[i] < ls[-1]
            tooDeep = False
            if phase == P_SEEK_LOW and actHigh:
                curDepth = (actHigh - lows[i]) / actHigh * 100.0
                allowed = maxFirstDepth if not hs else depths[-1] * shrink
                tooDeep = curDepth > allowed
            stale = baseStartBar is not None and (i - baseStartBar) > maxBaseBars
            staleHigh = actHighBar is not None and (i - actHighBar) > hiResetBars
            if undercut or tooDeep or stale or staleHigh:
                reset()

        # confirmed swing high
        if ph is not None:
            if phase == P_IDLE:
                if trendOK:
                    anchor(ph, pvBar)
            elif phase == P_SEEK_LOW:
                if hs and ph > hs[0] * hiBuf:
                    reset()                       # cleared the left-side high without a breakout
                    if trendOK:
                        anchor(ph, pvBar)
                elif actHigh is not None and ph > actHigh:
                    actHigh, actHighBar = ph, pvBar   # still pushing up before any pullback
                    if not hs:
                        baseStartBar = pvBar
            else:                                  # P_SEEK_HIGH -> recovery high starts next leg
                if hs and ph > hs[0] * hiBuf:
                    reset()
                    if trendOK:
                        anchor(ph, pvBar)
                elif len(hs) >= effMaxLegs:
                    reset()
                else:
                    actHigh, actHighBar = ph, pvBar
                    phase = P_SEEK_LOW

        # confirmed swing low -> try to record a contraction
        if pl is not None and phase == P_SEEK_LOW and actHigh:
            legBars = pvBar - actHighBar
            d = (actHigh - pl) / actHigh * 100.0
            allowed = maxFirstDepth if not hs else depths[-1] * shrink
            hlOK = (not hs) or pl > ls[-1]
            if d > 0 and legBars >= minLegBars:
                if d <= allowed and hlOK:
                    hs.append(actHigh); hBars.append(actHighBar)
                    ls.append(pl); lBars.append(pvBar); depths.append(d)
                    phase = P_SEEK_HIGH
                    pivot = hs[-1]
                    ready = len(hs) >= minLegs and d <= maxFinalDepth
                    cur_detail = snapshot()
                else:
                    reset()
            # legs shorter than minLegBars are noise and are ignored, as in the Pine

        # breakout: price clears the pivot while the sequence qualifies
        brkSrc = closes[i] if brkOnClose else highs[i]
        if ready and pivot and brkSrc > pivot:
            out['breakout'][i] = True
            out['ready'][i] = True
            out['active'][i] = True
            out['legs'][i] = len(hs)
            out['pivot'][i] = pivot
            out['last_depth'][i] = depths[-1] if depths else np.nan
            out['detail'][i] = cur_detail
            reset()                                # completed; drawings persist in the Pine
            continue

        out['ready'][i] = ready
        out['active'][i] = phase != P_IDLE
        out['legs'][i] = len(hs)
        if pivot:
            out['pivot'][i] = pivot
        if depths:
            out['last_depth'][i] = depths[-1]
        out['detail'][i] = cur_detail

    return out


def detect_candidate_bases(highs, lows, closes, pivot_highs, pivLen=5, bdF=0.50,
                           bLenB=325, max_bases=6, min_bars=20, dedupe_pct=0.5,
                           seed_lookback=25, seed_tol=0.03):
    """Track several candidate bases at once and return those still alive on the last bar.

    The scanner carries exactly ONE base, so when a tighter structure forms inside or after
    an older one it has to choose, and it keeps the older. Measured against MarketSmith
    progressions supplied for real tickers:

      PBI   truth switches on 4/2 to a newer base, pivot 11.62 (2/18/26 high). We hold the
            older 13.11 throughout. Capping base length to 160 bars yields 11.62 exactly -
            the newer base IS there, we simply never adopt it.
      GIII  truth base is 33 bars; ours runs 188 back to Oct 2025, which drags bLow from
            24.61 and corrupts cupMid, bDepPct and the cupH_allowed guard.

    But length caps are the wrong instrument: FTNT's base legitimately spans 2/18/2025 to
    5/11/2026 (~15 months) and MarketSmith keeps that left high the whole way, so `bLenB 90`
    would fix PBI and destroy FTNT. The distinction is not age - it is that a NEWER, tighter
    base can coexist with an older one, and both are defensible readings. Webster calls this
    the Microsoft problem ("where you used to see a consolidation, now you see two bases")
    and lists it as a later phase.

    So: track them concurrently and report each with its own pivot, rather than forcing one
    to win. Runs standalone over the series, so it cannot perturb the base state machine -
    its output feeds only the layered `patterns` list.
    """
    n = len(highs)
    live = []          # each: dict(start, top, low, count)
    for i in range(n):
        conf = i - pivLen
        # A confirmed pivot high that tops the prior `seed_lookback` bars seeds a base.
        # Webster's primary rule is a 13-week (65-bar) high, but that is the rule for the
        # ONE base he draws; the alternatives MarketSmith also shows sit below it. Measured
        # against the supplied progressions, 65 bars misses FTNT's true left high entirely
        # and 40 recovers PBI, FTNT, GIII and CLMT - all four base pivots.
        #
        # `seed_tol` then lets a left top sit slightly BELOW the window max, because
        # MarketSmith takes such tops: LASR's 7/17/2024 left high (13.16) is under 5/20's
        # 13.44, and its base is nonetheless a textbook Cup+Handle (depth 25.5% vs IBD's
        # 26, handle 5.7% vs 6, handle pivot 8/21 = 12.02 exactly).
        # Tuned jointly with dedupe_pct and max_bases over the 172 events. Primary label and
        # layered broad are both unchanged (90/126 and 152); the buy point improves - within
        # 1% 132 -> 139, within 2% 144 -> 146 - and seven events move to the truth pivot
        # EXACTLY (URGN 8.07%->0, ELTX 7.49%->0, CALY 6.94%->0, SBCF 5.93%->0, PESI, GOLF,
        # DHI). Landing to the cent on seven bases is what finding the right frame looks
        # like; a threshold that merely drifted would not. Cost: readings per event 1.9 ->
        # 2.2. The tighter dedupe (1.5 -> 0.5) is what keeps DLO's 14.49 from being folded
        # into a neighbouring base once more candidates exist.
        if conf >= 0 and conf in pivot_highs:
            ph = highs[conf]
            w0 = max(0, conf - seed_lookback)
            if conf > w0 and ph >= np.max(highs[w0:conf]) * (1.0 - seed_tol):
                if not any(abs(ph - b['top']) / max(b['top'], 1e-9) * 100.0 <= dedupe_pct
                           for b in live):
                    live.append({'start': conf, 'top': float(ph),
                                 'low': float(np.min(lows[conf:i + 1])), 'count': i - conf})
                    live.sort(key=lambda b: -b['start'])
                    del live[max_bases:]
        for b in live:
            b['count'] += 1
            if highs[i] > b['top'] and highs[i] <= b['top'] * 1.05:
                b['top'] = float(highs[i])
            if lows[i] < b['low'] and lows[i] >= b['top'] * (1.0 - bdF):
                b['low'] = float(lows[i])
        live = [b for b in live
                if lows[i] >= b['top'] * (1.0 - bdF)
                and b['count'] <= bLenB
                and closes[i] <= b['top'] * 1.40]
    return [b for b in live if b['count'] >= min_bars]


def detect_htf_context(highs, lows, min_pole_gain=80.0, max_pole_bars=60,
                       min_flag_bars=10, max_flag_bars=50, max_flag_depth=25.0):
    """Is the CURRENT structure the flag portion of a High Tight Flag? Annotation only.

    Separate from the `isHTF` state machine on purpose. That one demands a 300% pole, which
    is far outside IBD's definition (100-120% in 4-8 weeks), so it fires on 11 of 172
    benchmark events and misses ordinary HTFs outright - DELL ran 137.50 -> 469.47 (+241%)
    into a 42-bar flag 23.9% deep, NTAP 94.89 -> 192.83 (+103% in 7 weeks) into a 43-bar flag
    22.6% deep, and neither registers.

    Relaxing the threshold in place was measured and REJECTED: `isHTF` also feeds `inBase`
    and `activeBTop`, so more flags corrupt breakout tracking. At i_htfPole 300 -> 100,
    primary exact falls 90 -> 79, pivot within 1% 96 -> 88, and buy points quoted dangerously
    low rise 20 -> 26.

    So detect the flag independently and report it as CONTEXT. It runs standalone over the
    series and returns a dict, touching no state the base machine reads, which is the same
    arrangement `detect_candidate_bases` uses and for the same reason: a reading that cannot
    perturb the primary costs nothing to be wrong about.

    The flag encloses whatever forms inside it - a cup, a double bottom - and that inner
    pattern is the tradable one with its own buy point. This only says which larger structure
    it is sitting in.

    `min_pole_gain` is 80% by request, below IBD's nominal 100-120%. That is a deliberate
    widening of a CONTEXT label, and it is safe here in a way it would not be on `isHTF`:
    nothing downstream branches on it, so a loose flag annotates a chart without moving a buy
    point or a breakout. It does mean the label is looser than IBD's - an 80% pole is a
    strong move but not the "up 100%+ in weeks" that gives the real pattern its edge - so
    read `pole_gain_pct` rather than trusting the flag itself.
    """
    n = len(highs)
    if n < min_flag_bars + 20:
        return None
    w = min(max_flag_bars, n - 1)
    seg = highs[n - w:]
    t = n - w + int(np.argmax(seg))          # flag top: highest high in the recent window
    flag_bars = n - 1 - t
    if not (min_flag_bars <= flag_bars <= max_flag_bars):
        return None
    top = float(highs[t])
    if top <= 0:
        return None
    flag_low = float(np.min(lows[t:]))
    depth = (top - flag_low) / top * 100.0
    if depth > max_flag_depth:
        return None
    # Where the pole STARTS. A fixed lookback was wrong: the pole of a High Tight Flag
    # routinely begins inside an earlier pattern - a stock bases, breaks out, and the
    # breakout IS the start of the advance - so truncating at N bars measures from a higher
    # low and understates the gain. In the local cache that truncation was biting hard, with
    # TVTX, OKTA, PANW, CLYM, HUM, FTNT, ROMA and NWPX all reporting a pole of exactly 60
    # bars, i.e. the cap rather than the structure.
    #
    # So walk back to the last bar that traded ABOVE the flag high instead. A High Tight Flag
    # tops at a NEW high, so any earlier peak above it belongs to a previous structure and
    # the advance being measured starts after it. That boundary is structural rather than a
    # chosen number, and it lets the pole run back through whatever pattern it emerged from.
    # `max_pole_bars` remains only as a backstop for a stock that has simply been making new
    # highs for a year, where no such peak exists.
    # Where the pole STARTS. This window is deliberately pattern-AGNOSTIC: it looks only at
    # price, so it neither knows nor cares whether the pole low sits inside an earlier base.
    # A pole that begins within another pattern - stock bases, breaks out, and the breakout
    # is the start of the advance - is therefore already admitted, and always was.
    #
    # Extending the lookback so the pole could run further back through a prior pattern was
    # tried and REVERTED. Two versions, both worse:
    #   - walk back to the last peak above the flag high: a stock making new highs all year
    #     has no such peak, so the walk hits the backstop and DELL became a 250-bar "pole" of
    #     +341%, which is a year-long advance rather than a pole.
    #   - stop where the advance was interrupted by a `max_pullback` drawdown: self-defeating,
    #     because the running low descends as the walk proceeds and drags the threshold down
    #     with it, so it never fires. DELL came out +325.9% over 90 bars and OKTA vanished.
    # At 60 bars DELL reads +241.4% over 57 and NTAP +103.2% over 35, both textbook.
    #
    # What the cap DOES cost is honesty about long poles: names whose advance began earlier
    # report exactly 60 bars (TVTX, OKTA, PANW, FTNT and others all pinned there), and their
    # gain is understated because it is measured from a higher low. That understates, so it
    # can only cause a miss, never a false flag - the safe direction for a context label.
    p0 = max(0, t - max_pole_bars)
    if t - p0 < 5:
        return None
    j = p0 + int(np.argmin(lows[p0:t]))
    pole_low = float(lows[j])
    if pole_low <= 0:
        return None
    gain = (top / pole_low - 1.0) * 100.0
    if gain < min_pole_gain:
        return None
    return {'flag_high': round(top, 2), 'flag_low': round(flag_low, 2),
            'flag_bars': int(flag_bars), 'flag_depth_pct': round(depth, 1),
            'pole_low': round(pole_low, 2), 'pole_gain_pct': round(gain, 1),
            'pole_bars': int(t - j), 'flag_start_idx': int(t)}


def locate_handle(highs, lows, volumes, sma20_vol, start, top, low, end,
                  min_age=6, lo_frac=0.88, hi_frac=1.01,
                  max_hdep=30.0, max_hdratio=0.55, vol_ratio=1.15):
    """Find a handle inside a GIVEN base frame, and return (handle_high, handle_low).

    Separate from the main loop's trailing-window handle so it can be run per candidate
    base. The main loop measures a fixed ~15-bar window ending at the current bar; that
    finds a recent consolidation, which empirically prices the entry well (median pivot
    error 0.49% vs 1.13% for structurally-located handles) but is often NOT the handle IBD
    drew. This locates the structural one: a swing high at least `min_age` sessions old -
    every one of the 41 ground-truth Cup+Handles has its handle high >=7 sessions before the
    breakout - sitting in the upper part of the base, with the handle low taken as the
    lowest low since that high, which is what IBD's handle depth measures.

    Run per candidate base because "upper part of the base" is meaningless when the base
    frame is wrong: GIII's primary base ran 188 bars back to a low nine months stale, which
    dragged cupMid down and made the test vacuous.
    """
    if top <= 0 or end - start < 20:
        return None, None, None
    rng = max(top - low, 1e-9)
    lo_i = start + int(np.argmin(lows[start:end + 1]))
    best = None
    for b in range(end - min_age, lo_i, -1):
        if b - 5 < start or b + 5 > end:
            continue
        if highs[b] < np.max(highs[b - 5:b + 6]):      # not a confirmed swing high
            continue
        if not (lo_frac <= highs[b] / top <= hi_frac):
            continue
        best = b
        break
    if best is None:
        return None, None, None
    h_hi = float(highs[best])
    h_lo = float(np.min(lows[best:end + 1]))
    hdep = (h_hi - h_lo) / h_hi * 100.0 if h_hi > 0 else 999.0
    bdep = (top - low) / top * 100.0
    if not (2.0 <= hdep <= max_hdep):
        return None, None, None
    if bdep > 0 and hdep / bdep > max_hdratio:
        return None, None, None
    # handle must sit in the upper half of the base (Webster: midpoint vs midpoint)
    if (h_hi + h_lo) / 2.0 < (top + low) / 2.0:
        return None, None, None
    ref = sma20_vol[best] if (best < len(sma20_vol) and not np.isnan(sma20_vol[best])) else None
    if ref and ref > 0 and np.mean(volumes[best:end + 1]) >= ref * vol_ratio:
        return None, None, None
    return h_hi, h_lo, best


def classify_candidate_base(highs, lows, top, low, count, end, lag=8):
    """Name a candidate base and give its pivot.

    Mirrors the depth/length gates in the main loop (Cup bands by length, Flat Base by
    recent depth, Consolidation as the fallback). Cup+Handle is deliberately NOT decided
    here - the handle needs the volume and drift tests that only the main loop computes, so
    a candidate that would be a Cup+Handle is reported as a Cup at the base pivot, which is
    the conservative (higher) buy point.
    """
    if top <= 0 or count < 20:
        return None, None
    dep = (top - low) / top * 100.0
    rw = max(20, min(count, 65))
    s = max(0, end - rw + 1)
    rTop = float(np.max(highs[s:end + 1]))
    rLow = float(np.min(lows[s:end + 1]))
    rDep = (rTop - rLow) / rTop * 100.0 if rTop > 0 else 0.0
    e = end + 1 - lag
    pivot = float(np.max(highs[max(0, end - count):e])) if e > max(0, end - count) else top
    if rDep <= 20.0 and 20 <= count <= 130:
        return 'Flat Base', pivot
    if (25 <= count <= 130 and 8.0 <= dep <= 55.0) or \
       (130 < count <= 250 and 15.0 <= dep <= 45.0) or \
       (count > 250 and 20.0 <= dep <= 50.0):
        return 'Cup', pivot
    if count > 200 or 5.0 <= dep <= 35.0:
        return 'Consolidation', pivot
    return None, None


def scan_single_ticker(ticker: str, file_path: str, spy_close_series: pd.Series = None):
    """
    Scan a single ticker parquet file for patterns & metrics matching drw_pattern_scanner.pine.
    """
    try:
        df = pd.read_parquet(file_path)
        if df.empty or len(df) < 60:
            return None
            
        # Standardize columns
        required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in required_cols:
            if col not in df.columns:
                return None
                
        df = df.sort_index()
        # Keep full df (or at least 1500 bars) for accurate base transition tracking from inception
        df_trim_offset = 0
        if len(df) > 1500:
            df_trim_offset = len(df) - 1500
            df = df.iloc[-1500:]
            
        highs = df['High'].values
        lows = df['Low'].values
        closes = df['Close'].values
        volumes = df['Volume'].values
        opens = df['Open'].values
        n = len(df)
        
        # Parameters (Daily timeframe -> barsPerWeek = 5)
        barsPerWeek = 5
        pivLag = 5  # 5 daily bars (matching drw_pattern_scanner.pine i_pivot = 5)
        win13wk = 65
        win20wk = 100
        bLenB = 325  # 65 weeks
        bdF = 0.50
        pivLen = 5
        
        # Fast Moving Averages
        close_series = pd.Series(closes)
        ema10 = close_series.ewm(span=10, adjust=False).mean().values
        ema20 = close_series.ewm(span=20, adjust=False).mean().values
        sma50 = close_series.rolling(50, min_periods=10).mean().values
        sma20_vol = pd.Series(volumes).rolling(20, min_periods=5).mean().values
        atr14 = calculate_atr(highs, lows, closes, 14)
        
        # Raw RS Calculation if SPY provided
        rs_raw = None
        if spy_close_series is not None and not spy_close_series.empty:
            aligned_spy = spy_close_series.reindex(df.index).ffill().bfill().values
            if len(aligned_spy) == n and np.all(aligned_spy > 0):
                rs_raw = closes * 7.0 * 1000.0 / aligned_spy
                
        if rs_raw is None:
            rs_raw = closes.copy() # fallback
            
        # RS New High lookbacks (1Y=252, 6M=126, 3M=63)
        rs_s = pd.Series(rs_raw, index=df.index)
        rs_h1y = rs_s.shift(1).rolling(min(252, n), min_periods=30).max().values
        rs_h6m = rs_s.shift(1).rolling(min(126, n), min_periods=20).max().values
        rs_h3m = rs_s.shift(1).rolling(min(63, n), min_periods=10).max().values
        
        rs_nh_1y = (rs_raw > rs_h1y) & (~np.isnan(rs_h1y))
        rs_nh_6m = (rs_raw > rs_h6m) & (~np.isnan(rs_h6m))
        rs_nh_3m = (rs_raw > rs_h3m) & (~np.isnan(rs_h3m))
        rs_nh_any = rs_nh_1y | rs_nh_6m | rs_nh_3m
        
        # Pre-compute Pivots
        pivot_highs, pivot_lows = find_pivots(highs, lows, pivLen, pivLen)

        # VCP sub-pattern (independent of the base state machine; annotates the host base)
        vcp = detect_vcp(highs, lows, closes, pivot_highs, pivot_lows, pivLen=pivLen, **VCP_PARAMS)
        
        # We track base state bar-by-bar
        aHP_list = [] # list of (bar_idx, price) for highs
        aLP_list = [] # list of (bar_idx, price) for lows
        
        # Base variables
        bTop = None
        bLow = None
        bStart = None
        isBase = False
        bCount = 0
        lastBTop = None
        
        boPivot = None
        boBar = None
        boPatternCode = 0
        boPatternName = 'None'
        prevIsFlatBase = False
        
        rsCount = 0
        
        # Track active state per bar
        history_state = []
        
        # HTF state variables
        htf_flag_baseHigh = None
        htf_flag_startIndex = None
        htf_flag_flagLength = 0
        htf_flag_baseLow = None
        htf_flag_lowIndex = None
        htf_flag_flagBool = False
        htf_poleLow = None
        htf_poleLowIndex = None
        htf_boBar = None
        htf_history_is_flag = []
        
        # Sub-signals
        down_vols = np.where(pd.Series(closes).diff() < 0, volumes, 0.0)
        
        # Score flags pre-BO / post-BO
        scorePP_pre = False
        scoreShake_pre = False
        scoreTouch_pre = False
        scoreVDU_pre = False
        scoreRS_pre = False
        scoreUpRev_pre = False
        
        scorePP_post = False
        scoreShake_post = False
        scoreTouch_post = False
        scoreVDU_post = False
        scoreRS_post = False
        scoreUpRev_post = False
        
        # Shakeout state variables
        shakeTrendEMA = sma50
        shakeEma3 = close_series.ewm(span=3, adjust=False).mean().values
        shakeLastSwingLow = None
        shakeUndercutBar = None
        shakeReclaimBar = None
        shakeReclaimHigh = None
        shakeSetupActive = False
        
        for i in range(n):
            current_bar = i
            # Check if pivot high or low confirmed at i - pivLen
            conf_bar = i - pivLen
            if conf_bar in pivot_highs:
                aHP_list.insert(0, (conf_bar, pivot_highs[conf_bar]))
            if conf_bar in pivot_lows:
                aLP_list.insert(0, (conf_bar, pivot_lows[conf_bar]))
                shakeLastSwingLow = pivot_lows[conf_bar]
                
            # Highest / Lowest windows at bar i
            w25_start = max(0, i - pivLag + 1)
            H25 = np.max(highs[w25_start:i+1]) if i >= w25_start else highs[i]
            L25 = np.min(lows[w25_start:i+1]) if i >= w25_start else lows[i]
            
            w103_start = max(0, i - 130 + 1)
            L103 = np.min(lows[w103_start:i+1])
            
            # H65s: 65-bar high shifted by 26 bars (i - pivLag - 1)
            shift_idx = i - pivLag - 1
            if shift_idx >= 0:
                w65_start = max(0, shift_idx - 65 + 1)
                H65s = np.max(highs[w65_start:shift_idx+1])
            else:
                H65s = highs[i]
                
            # Base start check (newBase)
            newBase = False
            if len(aHP_list) >= 3 and i >= pivLag:
                piv_idx = i - pivLag
                piv_h = highs[piv_idx]
                
                recent_hp_prices = [p for b, p in aHP_list[:3]]
                bH = any(abs(piv_h - p) < 1e-4 for p in recent_hp_prices)
                bPH = (piv_h > H65s) or (bTop is not None and piv_h > bTop)
                lUp = (L103 * 1.20 <= piv_h)
                dep = (piv_h * (1.0 - bdF) <= L25)
                noAb = (H25 <= piv_h)
                
                noNe = True
                if len(history_state) >= pivLag:
                    noNe = not history_state[i - pivLag]['inBase']

                newBase = bH and bPH and lUp and dep and noAb and noNe

            prev_isBase = history_state[-1]['inBase'] if history_state else False
            
            if newBase and not prev_isBase:
                piv_idx = i - pivLag
                bTop = highs[piv_idx]
                bLow = L25
                bStart = piv_idx
                bCount = pivLag
                isBase = True
                lastBTop = bTop
                scorePP_pre = False
                scoreShake_pre = False
                scoreTouch_pre = False
                scoreVDU_pre = False
                scoreRS_pre = False
                scoreUpRev_pre = False
            elif not newBase and prev_isBase:
                isBase = True
                
            if isBase:
                bCount += 1
                if bTop is not None and highs[i] > bTop and highs[i] <= bTop * 1.05:
                    bTop = highs[i]
                lastBTop = bTop
                if bLow is not None and lows[i] < bLow and bTop is not None and lows[i] >= bTop * (1.0 - bdF):
                    bLow = lows[i]
                    
            # Invalidation: too deep, too long, or price has run too far above bTop for the
            # 5%-per-step ratchet to ever catch up (stale base top -> wildly wrong pivot).
            if isBase and bTop is not None:
                if lows[i] < bTop * (1.0 - bdF) or bCount > bLenB or closes[i] > bTop * 1.40:
                    isBase = False
            
            # Reset pattern flags when no active base (prevent stale detections)
            if not isBase:
                isCupH = False
                isCup = False
                isDB = False
                isAscendingBase = False
                has_u_shape = False
                    
            # Depth Pct & Base Types (Evaluated while in base BEFORE breakout check)
            bDepPct = (bTop - bLow) / bTop * 100.0 if (bTop and bLow and bTop > 0) else None
            recent_win = min(i + 1, max(20, min(bCount, 65)))
            is_flat_bo = (boPatternName in ('Flat Base', '6-Wk Flat'))
            end_r_idx = max(0, min(i, boBar - 1 if (boBar is not None and i - boBar <= 10 and is_flat_bo) else i))
            rTop = np.max(highs[max(0, end_r_idx - recent_win + 1) : end_r_idx + 1])
            rLow = np.min(lows[max(0, end_r_idx - recent_win + 1) : end_r_idx + 1])
            rDepPct = (rTop - rLow) / rTop * 100.0 if rTop > 0 else 0.0

            # Preliminary Consolidation check (before other patterns, for guard use)
            isLikelyConsolidation = isBase and bCount > 250

            # DETECTION ORDER: Cup+H first (most specific), then others with guards
            isCupH = False
            cupHandlePivot = None
            cupMid = bLow + (bTop - bLow) * 0.5 if (bTop and bLow) else None

            # U-shape check: has the second half of the base recovered near the top?
            # Used below for Cup vs Flat Base vs Consolidation disambiguation.
            has_u_shape = False
            if isBase and bTop and bLow and bCount > 100:
                mid_bar = i - bCount // 2
                if mid_bar >= 0:
                    second_half_high = np.max(highs[mid_bar:i+1])
                    has_u_shape = (second_half_high >= bTop * 0.90)

            # 6. Cup With Handle (independent of cup detection — allows handles on long cups)
            # Only apply the flat-base guard for shallow patterns (bDepPct < 25) where
            # handles are unlikely; for deeper bases, allow handle detection even if the
            # pattern was temporarily classified as flat.
            cupH_allowed = (not prevIsFlatBase or not isFlatBase) or (bDepPct is not None and bDepPct >= 25.0)
            if isBase and bTop and bLow and cupMid and bCount >= 20 and cupH_allowed and (bDepPct is not None and 20.0 <= bDepPct <= 50.0) and rDepPct > 12:
                # Fixed 15-bar handle window. IBD handles run ~1-4 weeks regardless of how
                # long the cup took, so scaling this with bCount measured the wrong span on
                # long bases (it pinned to the 25-bar cap and swept in non-handle action).
                handle_len = 15
                is_cuph_bo = (boPatternName == 'Cup+Handle')
                end_h_idx = max(1, min(i - 1, boBar - 1 if (boBar is not None and i - boBar <= 10 and is_cuph_bo) else i - 1))
                # End the handle where the breakout advance BEGINS, not blindly at i-1.
                # The bTop ratchet (line ~303) drags the base top up with price, so a slow
                # grind into the pivot never fires a breakout and this window swallowed the
                # cup's right side: measured handle depth ran 14.4% against the 9.0% IBD
                # records in Breakaway Gap.csv's own 'handle depth' column. Walking the end
                # back past bars that are setting new short-term highs cuts that bias to
                # +1.5pp. It is also the spirit of Webster's "lock in the FIRST handle"
                # rule - the handle must stop drifting forward into the advance.
                _trim = 0
                while end_h_idx > 1 and _trim < 10 and highs[end_h_idx] >= np.max(highs[max(0, end_h_idx - 10):end_h_idx]):
                    end_h_idx -= 1
                    _trim += 1
                w12_start = max(0, end_h_idx - handle_len)
                H12 = np.max(highs[w12_start:end_h_idx + 1]) if end_h_idx >= w12_start else highs[i]
                L12 = np.min(lows[w12_start:end_h_idx + 1])
                hDep = (H12 - L12) / H12 * 100.0 if H12 > 0 else 999.0
                # Handle must sit in the upper half of the cup (IBD rule), enforced strictly:
                # its low stays at/above the cup midpoint rather than 30% below it.
                inTop = (L12 >= cupMid * 0.95)
                max_hDep = 20.0 if bCount > 250 else 30.0
                depOk_h = (5.0 <= hDep <= max_hDep)
                ref_vol = sma20_vol[w12_start] if (w12_start < len(sma20_vol) and not np.isnan(sma20_vol[w12_start])) else None
                handle_avg_vol = np.mean(volumes[w12_start:end_h_idx + 1])
                volOk_h = (ref_vol is None or ref_vol <= 0) or (handle_avg_vol < ref_vol * 1.15)
                # A handle must not still be ADVANCING. IBD requires it to drift down (or
                # sideways); a window whose highs are still rising is the cup's right side,
                # not a handle. Measured on the detections this replaces: the drift of the
                # handle's highs runs a median 0.00 %/bar on true Cup+Handles versus +0.48
                # on the Cup-Without-Handle events that were wrongly called Cup+Handle -
                # the one feature that separates them where depth and hDep/bDep do not.
                # Least-squares slope, written out rather than np.polyfit because this runs
                # per-bar across the whole cache.
                slopeOk_h = True
                _hw = highs[w12_start:end_h_idx + 1]
                if len(_hw) >= 4 and H12 > 0:
                    _hx = np.arange(len(_hw), dtype=float)
                    _hxm = _hx - _hx.mean()
                    _hsl = float((_hxm * (_hw - _hw.mean())).sum() / (_hxm * _hxm).sum()) / H12 * 100.0
                    slopeOk_h = (_hsl <= 0.60)
                if inTop and depOk_h and volOk_h and slopeOk_h and H12 < bTop * 1.02:
                    hdRatio = hDep / bDepPct if bDepPct and bDepPct > 0 else 1.0
                    # Handle depth must stay well under the cup depth (IBD: no more than
                    # a third to a half of the cup's decline).
                    if hdRatio <= 0.45:
                        isCupH = True
                        cupHandlePivot = H12

            # 5. Cup Without Handle
            isCup = False
            if isBase and bTop and bLow and not isCupH:
                # For very long bases (> 100 bars), require U-shape recovery as confirmation
                # U-shape gate disabled: empirically it rejected more true Cups than false
                # ones (bCount is inflated vs. real base length, so the "second half" window
                # it measures is not the cup's actual right side).
                cup_ok = has_u_shape if bCount > 999 else True
                if cup_ok:
                    if (25 <= bCount <= 130):
                        depOk = (bDepPct is not None and 8.0 <= bDepPct <= 55.0)
                        if depOk and not isLikelyConsolidation:
                            isCup = True
                    elif (130 < bCount <= 250):
                        depOk = (bDepPct is not None and 15.0 <= bDepPct <= 45.0)
                        if depOk and not isLikelyConsolidation:
                            isCup = True
                    elif (bCount > 250):
                        depOk = (bDepPct is not None and 20.0 <= bDepPct <= 50.0 and not (bDepPct >= 30.0 and rDepPct < 25.0))
                        if depOk:
                            isCup = True

            # Flat Base (guarded by not isCupH)
            isFlatBase = isBase and (rDepPct <= 20.0) and (20 <= bCount <= 130) and not isCupH and not isLikelyConsolidation
            # Additional flat base check: recent 25-bar depth
            if isBase and not isFlatBase and not isCupH:
                rTop25 = np.max(highs[max(0, end_r_idx - 24) : end_r_idx + 1])
                rLow25 = np.min(lows[max(0, end_r_idx - 24) : end_r_idx + 1])
                rDep25 = (rTop25 - rLow25) / rTop25 * 100.0 if rTop25 > 0 else 0.0
                isFlatBase = (rDep25 <= 15.0) and (20 <= bCount <= 300)
            isDeepBase = isBase and not isFlatBase
            # 6-Wk Flat disabled as a separate label: it is just a Flat Base that has run at
            # least six weeks, so emitting it as its own pattern only splits Flat Base's
            # detections across two names without changing the pivot (both use bTop).
            is6WkFlat = False and isFlatBase and (25 <= recent_win <= 35)
            
            # 2. Ascending Base Detection (Strict 3 stair-step pullbacks spaced apart)
            # Disabled: rare in ground truth (5/177) and its "not isCupH" guard let it
            # steal bars from Cup/Cup+Handle/Consolidation once those were tightened.
            isAscendingBase = False
            if False and isBase and not isCupH and not isLikelyConsolidation and len(aHP_list) >= 3 and len(aLP_list) >= 3:
                recent_hps = [p for p in aHP_list if p[0] >= i - 90][:3]
                recent_lps = [p for p in aLP_list if p[0] >= i - 90][:3]
                if len(recent_hps) == 3 and len(recent_lps) == 3:
                    recent_hps.sort(key=lambda x: x[0])
                    recent_lps.sort(key=lambda x: x[0])
                    h1, h2, h3 = recent_hps[0][1], recent_hps[1][1], recent_hps[2][1]
                    l1, l2, l3 = recent_lps[0][1], recent_lps[1][1], recent_lps[2][1]
                    t_spaced = (recent_hps[1][0] - recent_hps[0][0] >= 8) and (recent_hps[2][0] - recent_hps[1][0] >= 8)
                    hh = (h1 < h2 < h3) or (h3 >= h1 * 1.01 and h2 >= h1 * 0.98)
                    hl = (l1 < l2 < l3) or (l3 >= l1 * 1.01 and l2 >= l1 * 0.98)
                    pb1, pb2, pb3 = (h1 - l1) / h1, (h2 - l2) / h2, (h3 - l3) / h3
                    pb_ok = all(0.04 <= p <= 0.25 for p in [pb1, pb2, pb3])
                    if t_spaced and hh and hl and pb_ok:
                        isAscendingBase = True
            
            # 3. Double Bottom Detection (W-shape symmetry)
            isDB = False
            dbMiddlePivot = None
            # Tighter W span: 85 bars let unrelated swing pairs far apart in time qualify,
            # which produced most of the Double Bottom false positives.
            dbMaxBars = 55
            # Guard relaxed to `not isLikelyConsolidation` only, so the W is evaluated on its
            # own merits for the layered output below. The Flat Base / Cup+Handle exclusions
            # are re-applied immediately after, leaving isDB itself bit-for-bit unchanged.
            if isBase and not isLikelyConsolidation and (bDepPct is not None and 15.0 <= bDepPct <= 40.0) and len(aHP_list) >= 2 and len(aLP_list) >= 2:
                for hp_i in range(min(5, len(aHP_list) - 1)):
                    for hp_j in range(hp_i + 1, min(len(aHP_list), hp_i + 5)):
                        sH_t, sH = aHP_list[hp_i]
                        fH_t, fH = aHP_list[hp_j]
                        if fH_t < i - dbMaxBars:
                            continue
                        
                        l1_candidates = [p for p in aLP_list if fH_t < p[0] < sH_t]
                        l2_candidates = [p for p in aLP_list if sH_t < p[0] <= i]
                        
                        if l1_candidates and l2_candidates:
                            fLt, fL = l1_candidates[0]
                            sLt, sL = l2_candidates[0]
                            
                            # IBD Rule: Second leg typically undercuts first leg
                            second_leg_undercut = sL < fL
                            
                            peak = max(fH, sH)
                            prL250 = np.min(lows[max(0, i-250):i+1])
                            cPT = (fH >= prL250 * 1.10) or (sH >= prL250 * 1.10)
                            # The two lows must be near-equal (a real W), not merely within
                            # 15% of each other.
                            cA = (sL <= fL * 1.04) and (sL >= fL * 0.94)
                            cB = (sL >= (1 - bdF) * peak)
                            cC = (sL <= peak * 0.95)
                            cD = (sH >= fL + (fH - fL) * 0.30) and (sH >= sL + (peak - sL) * 0.30)
                            cE = (sH <= fH * 1.02) and (sH >= fH * 0.75)
                            cTA = (fH_t < fLt < sH_t < sLt)
                            cTB = (sLt - fH_t <= dbMaxBars) and (i - fH_t <= dbMaxBars)
                            cTC = (sLt - fH_t >= 5)
                            db_end_idx = min(i, boBar if (boBar is not None and i - boBar <= 10) else i)
                            highest_since_2nd_low = np.max(highs[sLt:db_end_idx]) if sLt < db_end_idx else highs[db_end_idx-1]
                            cSh = (highest_since_2nd_low <= sH * 1.10)
                            # Volume asymmetry: a genuine double bottom typically sees higher
                            # volume on the first low (capitulation) than the second (retest).
                            cVol = volumes[fLt] >= volumes[sLt] * 0.90

                            if cPT and cA and cB and cC and cD and cE and cTA and cTB and cTC and cSh and cVol and second_leg_undercut:
                                isDB = True
                                dbMiddlePivot = sH
                                break
                    if isDB:
                        break
                        
            # --- LAYERED READINGS -------------------------------------------------------
            # Independent verdicts, before the priority chain discards all but one.
            #
            # The detectors are written mutually exclusive (isCup requires `not isCupH`,
            # isFlatBase requires `not isCupH`, isDB requires neither, isConsolidation
            # requires none of the others), so a base can never register two readings even
            # when two are defensible. Webster is explicit that it often is - "we could both
            # look at the same chart and see it differently, and Bill would agree we were
            # both right" - and the ambiguity is measurable: IBD's own Depth and Length
            # recover their own labels only 43.6% of the time.
            #
            # Judged independently, 78 of 165 bases carry two readings, 36 carry three.
            # Reporting the runner-up costs nothing - it is already computed.
            isDB_ind = isDB
            dbMiddlePivot_ind = dbMiddlePivot
            if isFlatBase or isCupH:          # restore the original isDB semantics exactly
                isDB = False
                dbMiddlePivot = None

            isFlat_ind = isBase and (rDepPct <= 20.0) and (20 <= bCount <= 130) and not isLikelyConsolidation
            if isBase and not isFlat_ind:
                _t25 = np.max(highs[max(0, end_r_idx - 24): end_r_idx + 1])
                _l25 = np.min(lows[max(0, end_r_idx - 24): end_r_idx + 1])
                _d25 = (_t25 - _l25) / _t25 * 100.0 if _t25 > 0 else 0.0
                isFlat_ind = (_d25 <= 15.0) and (20 <= bCount <= 300)
            isCup_ind = False
            if isBase and bTop and bLow:
                if (25 <= bCount <= 130):
                    isCup_ind = (bDepPct is not None and 8.0 <= bDepPct <= 55.0) and not isLikelyConsolidation
                elif (130 < bCount <= 250):
                    isCup_ind = (bDepPct is not None and 15.0 <= bDepPct <= 45.0) and not isLikelyConsolidation
                elif (bCount > 250):
                    isCup_ind = (bDepPct is not None and 20.0 <= bDepPct <= 50.0
                                 and not (bDepPct >= 30.0 and rDepPct < 25.0))

            # 7. Consolidation: Long bases (> 250 daily bars) or general consolidation.
            # A clear U-shape recovery strongly suggests Cup, not Consolidation.
            isConsolidation = isBase and (
                (bCount > 200 and not isCup and not isCupH) or
                (bDepPct is not None and 5.0 <= bDepPct <= 35.0 and not isCup and not isCupH and not isFlatBase and not isDB and not isAscendingBase)
            )

            # Independent Consolidation verdict + the base-top pivot the bTop-family
            # patterns all price off (same 8-bar lag as pivRef below, so they agree).
            isConsol_ind = isBase and (
                bCount > 200 or (bDepPct is not None and 5.0 <= bDepPct <= 35.0)
            )
            _pe = i + 1 - 8
            basePivot = (float(np.max(highs[bStart:_pe]))
                         if (isBase and bStart is not None and _pe > bStart) else bTop)

            # Determine active base pattern name BEFORE breakout check (align with final pName priority)
            currPName = 'Base'
            currPCode = 1
            if isAscendingBase: currPName, currPCode = 'Ascending Base', 8
            elif is6WkFlat: currPName, currPCode = '6-Wk Flat', 7
            elif isFlatBase: currPName, currPCode = 'Flat Base', 2
            elif isDB: currPName, currPCode = 'Dbl Bottom', 5
            elif isCupH: currPName, currPCode = 'Cup+Handle', 4
            elif isCup: currPName, currPCode = 'Cup', 3
            elif isConsolidation: currPName, currPCode = 'Consolidation', 9

            if False and isBase and PATTERN_MODEL is not None:
                lookback65 = min(i+1, 65)
                h65_f = np.max(highs[i+1-lookback65:i+1])
                l65_f = np.min(lows[i+1-lookback65:i+1])
                dep65_f = (h65_f - l65_f) / h65_f * 100.0 if h65_f > 0 else 0.0

                lookback30 = min(i+1, 30)
                h30_f = np.max(highs[i+1-lookback30:i+1])
                l30_f = np.min(lows[i+1-lookback30:i+1])
                dep30_f = (h30_f - l30_f) / h30_f * 100.0 if h30_f > 0 else 0.0

                lookback90 = min(i+1, 90)
                h90_f = np.max(highs[i+1-lookback90:i+1])
                l90_f = np.min(lows[i+1-lookback90:i+1])
                dep90_f = (h90_f - l90_f) / h90_f * 100.0 if h90_f > 0 else 0.0

                lookback12 = min(i+1, 12)
                h12_f = np.max(highs[i+1-lookback12:i+1])
                l12_f = np.min(lows[i+1-lookback12:i+1])
                dep12_f = (h12_f - l12_f) / h12_f * 100.0 if h12_f > 0 else 0.0

                handle_pos_f = (l12_f - l65_f) / (h65_f - l65_f) if (h65_f > l65_f) else 0.0
                has_w_shape_f = 1 if isDB else 0
                has_asc_base_f = 1 if isAscendingBase else 0

                feat_vec = np.array([[dep65_f, dep30_f, dep90_f, dep12_f, handle_pos_f, has_w_shape_f, has_asc_base_f]])
                try:
                    pred_label = PATTERN_MODEL.predict(feat_vec)[0]
                    if pred_label == 'Cup Without Handle': currPName, currPCode = 'Cup', 3
                    elif pred_label == 'Cup With Handle': currPName, currPCode = 'Cup+Handle', 4
                    elif pred_label == 'Flat Base': currPName, currPCode = 'Flat Base', 2
                    elif pred_label == 'Double Bottom': currPName, currPCode = 'Dbl Bottom', 5
                    elif pred_label == 'Ascending Base': currPName, currPCode = 'Ascending Base', 8
                    elif pred_label == 'Consolidation': currPName, currPCode = 'Consolidation', 9
                except Exception:
                    pass

            # Breakout: price clears the base top or middle pivot
            active_pivot = dbMiddlePivot if (isDB and dbMiddlePivot is not None) else (cupHandlePivot if (isCupH and cupHandlePivot is not None) else bTop)
            if isBase and active_pivot is not None and highs[i] > active_pivot:
                isBase = False
                boPivot = active_pivot
                boBar = i
                boPatternCode = currPCode
                boPatternName = currPName
                
            # Breakout tracking flag
            was_in_base = prev_isBase
            activeBTop = flag_baseHigh_prev if 'flag_baseHigh_prev' in locals() and history_state and history_state[-1]['isHTF'] else lastBTop
            
            if was_in_base and not isBase and boBar != i and activeBTop is not None and highs[i] > activeBTop:
                boPivot = activeBTop
                boBar = i
                boPatternCode = history_state[-1]['pCode'] if history_state and history_state[-1]['pCode'] > 0 else 1
                boPatternName = history_state[-1]['pName'] if history_state and history_state[-1]['pName'] != 'None' else 'Base'
                
            if newBase:
                boPivot = None
                boBar = None
                boPatternCode = 0
                boPatternName = 'None'

            # --- HTF Detection (drw_pattern_scanner.pine state machine engine) ---
            # Stays at 300 despite being far above IBD's 100-120%. Lowering it was tried
            # and REVERTED: `isHTF` feeds `inBase` and `activeBTop`, so extra flags rewire
            # breakout tracking and re-seed the base machine onto the wrong structure. At 80
            # DELL's base frame became bTop 221.50 / 106 bars instead of the correct 469.47 /
            # 43 bars, and its Double Bottom vanished - the exact opposite of letting patterns
            # form inside the flag. Measured over the 172 events:
            #
            #   i_htfPole    300    100     80
            #   primary ex    90     79     82
            #   primary br   126    123    122
            #   layered br   154    152    151
            #   pivot <=1%    96     88     88
            #   quoted low    20     26     29
            #   HTF events    11     59     77
            #
            # The flag is instead detected independently by detect_htf_context() at an 80%
            # pole and reported as a reading plus `htf_context`, which costs nothing because
            # nothing downstream branches on it.
            i_htfPole = 300.0
            i_htfPB = 60
            i_htfPBMin = 5
            i_htfRet = 28.0
            i_htfFMin = 1
            i_htfFMax = 50
            i_bsoMax = 15

            prev_htf_flag_baseHigh = htf_flag_baseHigh
            
            if i > 30:
                if htf_flag_baseHigh is None or highs[i] > htf_flag_baseHigh:
                    htf_flag_baseHigh = highs[i]
                    htf_flag_startIndex = i
                    htf_flag_flagLength = 0
                    htf_flag_baseLow = lows[i]
                    htf_flag_lowIndex = i
                    
                if highs[i] <= htf_flag_baseHigh and (htf_flag_baseLow is None or lows[i] < htf_flag_baseLow):
                    htf_flag_baseLow = lows[i]
                    htf_flag_lowIndex = i
                    
                if highs[i] <= htf_flag_baseHigh and htf_flag_lowIndex == htf_flag_startIndex:
                    htf_flag_baseLow = lows[i]
                    htf_flag_lowIndex = i
                    
                findDepth = abs(((htf_flag_baseLow / htf_flag_baseHigh) - 1.0) * 100.0) if (htf_flag_baseHigh and htf_flag_baseHigh > 0) else 0.0
                lower_close = (closes[i] < closes[i-1]) if i > 0 else False
                
                if (highs[i] < htf_flag_baseHigh and findDepth <= i_htfRet) or (highs[i] == htf_flag_baseHigh and lower_close):
                    htf_flag_flagLength += 1
                else:
                    htf_flag_flagLength = 0
                    
                if not htf_flag_flagBool or highs[i] == htf_flag_baseHigh:
                    searchBars = min(i_htfPB, i - 1)
                    if searchBars >= i_htfPBMin:
                        minLow = lows[i-1]
                        minLowIdx = i - 1
                        for k in range(1, searchBars + 1):
                            if lows[i - k] < minLow:
                                minLow = lows[i - k]
                                minLowIdx = i - k
                        if minLow > 0 and ((htf_flag_baseHigh / minLow) - 1.0) * 100.0 >= i_htfPole:
                            htf_flag_flagBool = True
                            htf_poleLow = minLow
                            htf_poleLowIndex = minLowIdx
                            
                if findDepth >= i_htfRet or htf_flag_flagLength > i_htfFMax:
                    htf_flag_flagBool = False
                    htf_flag_flagLength = 0
                    htf_flag_baseHigh = None
                    htf_flag_startIndex = None
                    htf_flag_lowIndex = None
                    htf_flag_baseLow = None
                    
                if prev_htf_flag_baseHigh is not None and highs[i] > prev_htf_flag_baseHigh and htf_flag_flagLength < i_htfFMin:
                    htf_flag_baseHigh = highs[i]
                    htf_flag_flagLength = 0
                    htf_flag_startIndex = i
                    htf_flag_lowIndex = i
                    htf_flag_baseLow = lows[i]
                    
                is_flag = (htf_flag_flagBool == True) and (htf_flag_flagLength <= i_htfFMax) and (findDepth < i_htfRet) and (htf_flag_flagLength >= i_htfFMin) and (htf_flag_startIndex is not None and htf_poleLowIndex is not None and (htf_flag_startIndex - htf_poleLowIndex) <= i_htfPB)
                
                prev_is_flag = htf_history_is_flag[-1] if htf_history_is_flag else False
                breakout = (prev_htf_flag_baseHigh is not None and highs[i] > prev_htf_flag_baseHigh) and (htf_flag_flagLength >= i_htfFMin) and (htf_flag_flagBool == True)
                plotBO = prev_is_flag and (prev_htf_flag_baseHigh is not None and highs[i] > prev_htf_flag_baseHigh) and (htf_flag_flagBool == True)
                
                if plotBO:
                    htf_boBar = i
                    
                htfPostBOActive = (htf_boBar is not None) and ((i - htf_boBar) <= i_bsoMax)
                isHTF = plotBO or is_flag or htfPostBOActive
                
                if breakout:
                    htf_flag_flagLength = 0
                    htf_flag_baseHigh = highs[i]
                    htf_flag_startIndex = i
                    htf_flag_lowIndex = i
                    htf_flag_baseLow = lows[i]
            else:
                is_flag = False
                isHTF = False
                
            htf_history_is_flag.append(is_flag)
            flag_baseHigh_prev = htf_flag_baseHigh
            
            # Active pattern evaluation
            inBase = isBase or isDB or isCup or isCupH or isHTF or isAscendingBase or isConsolidation
            barsSBO = (i - boBar) if boBar is not None else None
            
            pName = 'None'
            pCode = 0
            pOn = False
            
            if inBase:
                # HTF could form within a base (Flat Base, Consolidation, Cup), so letting it
                # preempt the base label lost the underlying pattern. It stays available as a
                # flag (state['isHTF']) for scoring/annotation.
                if False and isHTF: pName, pCode, pOn = 'HTF', 6, True
                elif isAscendingBase: pName, pCode, pOn = 'Ascending Base', 8, True
                elif is6WkFlat: pName, pCode, pOn = '6-Wk Flat', 7, True
                elif isFlatBase: pName, pCode, pOn = 'Flat Base', 2, True
                elif isDB: pName, pCode, pOn = 'Dbl Bottom', 5, True
                elif isCupH: pName, pCode, pOn = 'Cup+Handle', 4, True
                elif isCup: pName, pCode, pOn = 'Cup', 3, True
                elif isConsolidation: pName, pCode, pOn = 'Consolidation', 9, True
                elif isDeepBase: pName, pCode, pOn = 'Base', 1, True
            else:
                if barsSBO is not None and barsSBO <= 15:
                    pCode = boPatternCode
                    pName = boPatternName
                    pOn = (pCode > 0)
                    
            # PivRef & Distance %
            # bTop is corrected for a systematic overshoot: its 5%-per-step ratchet can sit
            # up to 5% above the true left-side high it's chasing, so it runs ~2.5% (half the
            # cap) high on average versus ground truth. The handle-high and double-bottom
            # middle-peak pivots are computed differently (window max / specific pivot value)
            # and don't share this bias, so the correction is scoped to bTop only.
            if inBase:
                # A pattern can form INSIDE the flag portion of a High Tight Flag, and when
                # one does it prices off ITS OWN level, not the flag high. HTF used to
                # preempt every other pivot here, so a cup or double bottom building inside
                # the flag was quoted at the flag high - a level belonging to the enclosing
                # structure, usually well above the inner pattern's own buy point.
                #
                # The label precedence below already worked this way (see the `if False and
                # isHTF` line and its note), so the two were inconsistent: the scanner would
                # report "Cup" and then price it off the flag. Now the inner pattern wins and
                # the flag high is the FALLBACK, used only when HTF is the sole structure.
                # HTF is preserved as context - `in_htf_flag` / `htf_flag_high` on the result
                # and an 'HTF' entry in the layered readings - so the flag is still visible
                # rather than silently dropped.
                # Quote the base pivot off the base's HIGHEST HIGH, not the ratcheted bTop.
                # Measured against the ground truth at 20 bars before the breakout, over
                # base-top patterns whose pivot was >5% too low:
                #     true pivot / max high in base = 1.000   (IBD's pivot IS that high)
                #     bTop       / max high in base = 0.948   (we quote 5.2% under it)
                # bTop lags because the ratchet only absorbs moves up to 5% above it; a
                # bigger jump fires a breakout instead and the base then resurrects still
                # carrying the stale value, so the quoted buy point is a level price has
                # already traded through.
                #
                # The 8-bar lag excludes the current thrust: without it the running max picks
                # up the breakout bar itself and overshoots (piv3 101 -> 88). A pivot should
                # be a prior confirmed swing high, and pivot confirmation already costs
                # pivLen(5) bars. Results are flat for lag 6-10, so this is a plateau rather
                # than a tuned edge.
                # No fudge factor: the old bTop * 0.975 existed to cancel the ratchet's
                # systematic overshoot, and the base high has no overshoot to cancel. Keeping
                # it manufactured a 2.5% error by construction (median error was exactly
                # 2.50%); dropping it takes the median to 0.02% and lifts within-1% from 24
                # to 101 events.
                _e = i + 1 - 8
                _bp = float(np.max(highs[bStart:_e])) if (bStart is not None and _e > bStart) else bTop
                _base_piv = _bp if _bp else bTop
                if isCupH and cupHandlePivot is not None: pivRef = cupHandlePivot
                elif isDB and dbMiddlePivot is not None: pivRef = dbMiddlePivot
                elif _base_piv: pivRef = _base_piv
                elif isHTF: pivRef = htf_flag_baseHigh
            else:
                pivRef = boPivot
            distPct = (closes[i] - pivRef) / pivRef * 100.0 if (pivRef and pivRef > 0) else None
            
            # Sub-signal evaluations
            volDryUp1 = (volumes[i] < sma20_vol[i] * 0.55) if (sma20_vol[i] > 0) else False
            
            pp10 = False
            pp5 = False
            if i >= 10 and closes[i] > closes[i-1]:
                max_dn_10 = np.max(down_vols[i-10:i])
                if max_dn_10 > 0 and volumes[i] > max_dn_10:
                    pp10 = True
                max_dn_5 = np.max(down_vols[i-5:i])
                if max_dn_5 > 0 and volumes[i] > max_dn_5:
                    pp5 = True
            ppAny = pp10 or pp5
            
            touchMA1 = (lows[i] <= ema10[i] * 1.025 and highs[i] >= ema10[i] * 0.975) if ema10[i] > 0 else False
            touchMA2 = (lows[i] <= ema20[i] * 1.025 and highs[i] >= ema20[i] * 0.975) if ema20[i] > 0 else False
            touchMA3 = (lows[i] <= sma50[i] * 1.025 and highs[i] >= sma50[i] * 0.975) if (not np.isnan(sma50[i]) and sma50[i] > 0) else False
            touchedMA = touchMA1 or touchMA2 or touchMA3
            
            shakeoutEntry = False
            if not np.isnan(shakeTrendEMA[i]) and closes[i] > shakeTrendEMA[i]:
                if shakeLastSwingLow and lows[i] < shakeLastSwingLow and not shakeSetupActive:
                    shakeUndercutBar = i
                    shakeSetupActive = True
                if shakeSetupActive and shakeUndercutBar and (i - shakeUndercutBar <= 3) and closes[i] > shakeEma3[i] and not shakeReclaimBar:
                    shakeReclaimBar = i
                    shakeReclaimHigh = highs[i]
                if shakeSetupActive and shakeReclaimBar and i > shakeReclaimBar and highs[i] > shakeReclaimHigh:
                    shakeoutEntry = True
                    shakeSetupActive = False
                    shakeUndercutBar = None
                    shakeReclaimBar = None
            if shakeSetupActive and shakeUndercutBar and (i - shakeUndercutBar > 3):
                shakeSetupActive = False
                
            upsideReversal = ((highs[i] - lows[i]) > atr14[i]) and (closes[i] > (highs[i] + lows[i]) / 2.0)
            
            rsWindow = inBase or (not inBase and boBar is not None and barsSBO is not None and barsSBO <= 15)
            if newBase:
                rsCount = 1 if rs_nh_any[i] else 0
            elif rsWindow and rs_nh_any[i]:
                rsCount += 1
                
            nearPivotScore = inBase and (distPct is not None and abs(distPct) <= 15.0)
            if newBase or not inBase:
                scorePP_post = False
                scoreShake_post = False
                scoreTouch_post = False
                scoreVDU_post = False
                scoreRS_post = False
                scoreUpRev_post = False
                
            if inBase:
                if ppAny: scorePP_pre = True
                if shakeoutEntry: scoreShake_pre = True
                if touchedMA: scoreTouch_pre = True
                if volDryUp1: scoreVDU_pre = True
                if rs_nh_any[i]: scoreRS_pre = True
                if upsideReversal: scoreUpRev_pre = True
                
            postBOWindowScore = (not inBase) and (barsSBO is not None and barsSBO <= 15)
            if postBOWindowScore:
                if ppAny: scorePP_post = True
                if shakeoutEntry: scoreShake_post = True
                if touchedMA: scoreTouch_post = True
                if volDryUp1: scoreVDU_post = True
                if rs_nh_any[i]: scoreRS_post = True
                if upsideReversal: scoreUpRev_post = True
                
            beforeBOScore = sum([scorePP_pre, scoreShake_pre, scoreTouch_pre, scoreVDU_pre, scoreRS_pre, scoreUpRev_pre])
            postBOScore = sum([scorePP_post, scoreShake_post, scoreTouch_post, scoreVDU_post, scoreRS_post, scoreUpRev_post])
            compositeScore = beforeBOScore + postBOScore
            
            state = {
                'bar': i,
                'date': str(df.index[i])[:10],
                'close': float(closes[i]),
                'inBase': inBase,
                'isHTF': isHTF,
                'pName': pName,
                'pCode': pCode,
                'pOn': pOn,
                'bCount': bCount if inBase else None,
                'bTop': bTop,
                'bLow': bLow,
                'bDepPct': float(bDepPct) if bDepPct is not None else None,
                'barsSBO': barsSBO,
                'boBar': boBar,
                'boPatternName': boPatternName,
                'boPivot': boPivot,
                'distPct': float(distPct) if distPct is not None else None,
                'beforeBOScore': beforeBOScore,
                'postBOScore': postBOScore,
                'compositeScore': compositeScore,
                'rsCount': rsCount,
                'volDryUp': volDryUp1,
                'ppAny': ppAny,
                'touchedMA': touchedMA,
                'shakeoutEntry': shakeoutEntry,
                'upsideReversal': upsideReversal,
                'rsNH': bool(rs_nh_any[i]),
                'isCupH': isCupH,
                # Layered readings for this bar: (name, pivot). Each pattern prices off a
                # DIFFERENT level, so the pivot travels with the label - Cup+Handle buys the
                # handle high, Double Bottom the middle peak, the rest the base high.
                'altPat': tuple(
                    (nm, pv) for nm, ok, pv in (
                        ('Cup+Handle', isCupH, cupHandlePivot),
                        ('Dbl Bottom', isDB_ind, dbMiddlePivot_ind),
                        ('Flat Base', isFlat_ind, basePivot),
                        ('Cup', isCup_ind, basePivot),
                        ('Consolidation', isConsol_ind, basePivot),
                        # HTF is a READING like the rest, not a verdict that replaces them.
                        # It encloses whatever forms inside it, so it prices off the flag
                        # high while the inner pattern keeps its own buy point, and both are
                        # listed. Previously HTF appeared nowhere in the readings and merely
                        # hijacked the pivot, which lost the flag on any bar where a base was
                        # also live.
                        ('HTF', isHTF, htf_flag_baseHigh),
                    ) if ok and pv
                ),
                'isHTFFlag': bool(isHTF),
                'htfFlagHigh': float(htf_flag_baseHigh) if (isHTF and htf_flag_baseHigh) else None,
                # VCP sub-pattern state, so the contraction sequence can be painted as it forms
                'vcpReady': bool(vcp['ready'][i]),
                'vcpActive': bool(vcp['active'][i]),
                'vcpLegs': int(vcp['legs'][i]),
                'vcpPivot': float(vcp['pivot'][i]) if not np.isnan(vcp['pivot'][i]) else None,
                'vcpLastDepth': float(vcp['last_depth'][i]) if not np.isnan(vcp['last_depth'][i]) else None,
                'vcpBO': bool(vcp['breakout'][i]),
                'isFlatBase': isFlatBase,
                'isDB': isDB,
                'isAscendingBase': isAscendingBase,
            }
            prevIsFlatBase = isFlatBase
            history_state.append(state)
            
        latest = history_state[-1]
        
        # Calculate % Off 52W High on latest bar
        high252 = np.max(highs[max(0, n-252):n])
        pctOff52wHigh = (high252 - closes[-1]) / high252 * 100.0 if high252 > 0 else 0.0

        # --- explicit pivot + ambiguity disclosure -----------------------------------
        # `pivot` was previously only implied (close / (1 + dist_pct/100)); emit it directly.
        #
        # A base often carries two defensible pivot highs. Measured over 1320 candidate
        # swing highs in 119 bases, IBD's pivot is the HIGHEST 51% of the time and the
        # second-highest 48% - and nothing separates them (volume-above, volume-at, touch
        # count, recency and wick rejection are all at chance once you control for height).
        # The scanner has to pick one, and picks the higher.
        #
        # That choice is not symmetric. Over 94 events entered on a genuine breakout (five
        # closes below the level, then a cross), a pivot quoted ABOVE the real buy point
        # cost ~13pp of 20-bar return and ~7pp of extra drawdown - you end up chasing a move
        # that is already ~6 bars extended - while one quoted below was mildly beneficial.
        #
        # So when the top two candidates disagree materially, report the lower one as well
        # instead of silently discarding it. This is ADDITIVE: pattern_name, dist_pct and
        # every accuracy metric are untouched (verified unchanged at exact 90/172,
        # broad 127/172, pivot within 1% 101/164, median pivot error 0.02%).
        latest_pivot = None
        if latest['distPct'] is not None and (1.0 + latest['distPct'] / 100.0) != 0:
            latest_pivot = latest['close'] / (1.0 + latest['distPct'] / 100.0)

        # --- layered readings over the formation window --------------------------------
        # A base is identified well before it breaks out - a Cup Without Handle a median 66
        # bars ahead, a Flat Base 86 - because their pivot is the base high, which exists
        # from the start. Only Cup+Handle is late (median 6 bars), since the handle is the
        # last thing to form. But the breakout bar is where the label is LEAST stable: the
        # trailing handle window swallows the thrust and 9 of 15 lost labels flip into
        # Cup+Handle exactly there. So collect what the base was read as across the window,
        # not just on the final bar, and rank by specificity.
        # HTF ranks LAST: it is the enclosing structure, so whatever forms inside the flag is
        # the more specific read and should lead the list. It still earns an entry of its own
        # because it prices off a different level (the flag high).
        PATTERN_RANK = {'Cup+Handle': 0, 'Dbl Bottom': 1, 'Flat Base': 2,
                        'Cup': 3, 'Consolidation': 4, 'HTF': 5}
        layered = {}
        for _st in history_state[-(PATTERN_WINDOW_BARS + 1):]:
            if not _st.get('pOn'):
                continue
            for _nm, _pv in _st.get('altPat', ()):
                prev = layered.get(_nm)
                if prev is None or _st['bar'] >= prev[1]:
                    layered[_nm] = (_pv, _st['bar'], _st['date'])
        # Collapse readings that quote the SAME buy point. Flat Base, Cup and Consolidation
        # all price off the base high, so listing three of them is a naming distinction, not
        # a decision - it changes nothing you would do. A second entry earns its place only
        # when it moves the entry price, which in practice means Cup+Handle (handle high) or
        # Double Bottom (middle peak) disagreeing with the base high. Names that share a
        # pivot are folded into `also_reads_as` on the entry that owns it.
        patterns = []
        for _nm in sorted(layered, key=lambda k: PATTERN_RANK.get(k, 9)):
            _pv, _bar, _dt = layered[_nm]
            _dup = next((p for p in patterns
                         if abs(p['pivot'] - _pv) / max(_pv, 1e-9) * 100.0 <= PIVOT_SAME_PCT), None)
            if _dup is not None:
                _dup['also_reads_as'].append(_nm)
                continue
            patterns.append({
                'name': _nm,
                'pivot': float(round(_pv, 2)),
                'dist_pct': float(round((latest['close'] - _pv) / _pv * 100.0, 2)),
                'bars_ago': int(latest['bar'] - _bar),
                'last_seen': _dt,
                'also_reads_as': [],
            })

        # Concurrent candidate bases - the structures the single-base machine passed over.
        # Matched on PIVOT, not name: the whole point is a different buy point, and the
        # candidate often carries the same label as the primary (PBI reads Flat Base at both
        # 13.11 and 11.62). Keying on name silently dropped exactly the cases this is for.
        for _cb in detect_candidate_bases(highs, lows, closes, pivot_highs, pivLen=pivLen,
                                          bdF=bdF, bLenB=bLenB):
            _nm, _pv = classify_candidate_base(highs, lows, _cb['top'], _cb['low'],
                                               _cb['count'], n - 1)
            # A handle located inside THIS base's frame overrides the base-top reading:
            # it is a more specific pattern and prices off a lower level.
            _hh, _hl, _hb = locate_handle(highs, lows, volumes, sma20_vol, _cb['start'],
                                          _cb['top'], _cb['low'], n - 1)
            if _hh:
                _nm, _pv = 'Cup+Handle', _hh
            if not _nm or not _pv:
                continue
            # Same-pivot collapse as above, but FOLD the name in rather than dropping it.
            # This branch used to `continue`, which silently threw away a reading that had
            # already been computed: a Cup+Handle whose handle high sits within 1% of the
            # base top would vanish entirely (CLMT, SOLV both locate a valid handle and then
            # lose the label here). Recording it changes no pivot and adds no detection - it
            # only stops us discarding a name we already know.
            _dup = next((p for p in patterns
                         if abs(p['pivot'] - _pv) / max(_pv, 1e-9) * 100.0 <= PIVOT_SAME_PCT), None)
            if _dup is not None:
                if _nm != _dup['name'] and _nm not in _dup['also_reads_as']:
                    _dup['also_reads_as'].append(_nm)
                continue
            patterns.append({
                'name': _nm,
                'pivot': float(round(_pv, 2)),
                'dist_pct': float(round((latest['close'] - _pv) / _pv * 100.0, 2)),
                'bars_ago': 0,
                'last_seen': latest['date'],
                'also_reads_as': [],
                'alt_base': True,          # from a concurrent base, not the primary one
            })

        # With no active primary pattern, `distPct` still carries the dead base's last
        # reading, so the headline pivot would quote a level nothing on the chart supports
        # (DVA: 106.96 off a base that expired five weeks earlier). When the only thing being
        # reported IS a reading, price off that reading instead.
        # `dist_pct` travels with it: downstream (evaluate_breakaway_gap.py, fast_eval) the
        # buy point is reconstructed as close/(1+dist_pct/100), so leaving the stale value
        # would report one level in `pivot` and a different one everywhere else.
        # Enclosing High Tight Flag, if any. Annotation only - see detect_htf_context. The
        # inner pattern keeps its own buy point; this says which structure it formed inside.
        _htf_ctx = detect_htf_context(highs, lows)

        # Surface the flag AS A READING, alongside the inner patterns rather than instead of
        # them. It prices off the flag high, which is a different level from the inner
        # pattern's buy point, so it earns its own entry on the same terms as everything else
        # here. Appended last because the flag encloses whatever formed inside it, and folded
        # into `also_reads_as` when the two levels coincide - DELL's cup tops out at the flag
        # high, so it reads "Cup, also HTF" rather than listing 469.47 twice.
        #
        # This comes from detect_htf_context, NOT from `isHTF`, which is why it can use an 80%
        # pole without touching the state machine. See the i_htfPole note for what happened
        # when the state machine was widened instead.
        if _htf_ctx:
            _hv = float(_htf_ctx['flag_high'])
            _hdup = next((p for p in patterns
                          if abs(p['pivot'] - _hv) / max(_hv, 1e-9) * 100.0 <= PIVOT_SAME_PCT), None)
            if _hdup is not None:
                if 'HTF' not in _hdup['also_reads_as']:
                    _hdup['also_reads_as'].append('HTF')
            else:
                patterns.append({
                    'name': 'HTF',
                    'pivot': float(round(_hv, 2)),
                    'dist_pct': float(round((latest['close'] - _hv) / _hv * 100.0, 2)),
                    'bars_ago': 0,
                    'last_seen': latest['date'],
                    'also_reads_as': [],
                    'htf_flag': True,
                })

        latest_dist = latest['distPct']
        if not (latest['pOn'] and latest['pCode'] > 0) and patterns:
            latest_pivot = patterns[0]['pivot']
            latest_dist = patterns[0]['dist_pct']

        # --- quote the base top, not a sub-structure level -----------------------------
        # Cup+Handle and Double Bottom price off a level INSIDE the base (handle high,
        # middle peak); Cup / Flat Base / Consolidation price off the base top. Cup+Handle
        # is claimed on 85 of 172 events when only 46 are truly one - precision 36.5% - so
        # two times in three the handle is invented, and an invented handle quotes a buy
        # point BELOW the level price actually has to clear. That is the costly direction:
        # a buy point too low reports a breakout that has not happened and takes the entry
        # into overhead supply, where one too high only misses or delays the trade.
        #
        # So when the reported level is a sub-structure quote and a base-top reading is on
        # offer, report the base top instead. Measured over all 171 events at four lead
        # times (within 1% / quoted low by >3%):
        #
        #                   T-0        T-5       T-10       T-20
        #   before      93 / 29    95 / 28    96 / 27    85 / 34
        #   after       97 / 18   100 / 17    98 / 18    95 / 19
        #
        # Better on both axes at every lead time, and the dangerous quotes fall by ~35%
        # throughout. It is not free: on events whose truth really IS Cup With Handle it
        # costs 12 hits at T-0, 4 at T-10, 1 at T-20 - when a handle is real and well
        # located the handle high is the buy point and the base top is a few percent late.
        # The trade is deliberate, and the decay of that cost with lead time says most of
        # the current handle advantage exists only on breakout day.
        #
        # A narrower version was tried and REJECTED: override only when the base top sits
        # more than 12-25% above the handle quote, on the theory that a real handle is a
        # shallow drift just under its base top (IBD caps handle depth near 15%) so a large
        # gap means the two describe different bases. The geometry is sound and the rule
        # still loses - +5 hits against +21 for the blanket version - because the good
        # corrections are not all large-gap. The gap distribution had only been read off the
        # failures, never off the successes.
        #
        # Labels are untouched: this moves `pivot`/`dist_pct` only, never pattern_name or
        # pattern_code, so every label metric is identical by construction.
        BASE_TOP_READINGS = ('Cup', 'Flat Base', 'Consolidation')
        _prim_basetop = (latest['pOn'] and latest['pCode'] > 0
                         and latest['pName'] in BASE_TOP_READINGS)
        if not _prim_basetop:
            _bt = next((p for p in patterns if p['name'] in BASE_TOP_READINGS), None)
            if _bt is not None:
                latest_pivot = _bt['pivot']
                latest_dist = _bt['dist_pct']

        cons_pivot = amb_pct = cons_dist = None
        if latest['inBase'] and bStart is not None:
            # 8 bars: must match the lag used for pivRef above, so both read the same window.
            piv_end = n - 8
            cands = sorted((p for b, p in aHP_list if bStart <= b < piv_end), reverse=True)
            if len(cands) > 1 and cands[0] > 0:
                gap = (cands[0] - cands[1]) / cands[0] * 100.0
                if gap > PIVOT_AMBIGUITY_PCT:
                    cons_pivot = float(cands[1])
                    amb_pct = gap
                    cons_dist = (latest['close'] - cons_pivot) / cons_pivot * 100.0
        
        # Filter for active tickers: either currently in pattern base or post-breakout within 15 bars
        # Emit a result whenever there is SOMETHING to report - either the state machine has
        # an active pattern, or a concurrent candidate base does. Previously a ticker with no
        # active primary pattern returned None and the layered readings were computed and
        # thrown away, which is how all 7 undetected benchmark events failed: each one HAD a
        # correct reading. DVA is the clearest case - its 325-bar base hit the length cap on
        # 12/26/2023 and died, the successor base's seed high (12/14, 111.47) passed five of
        # the six newBase gates and failed only `noNe` (the old base was still alive for six
        # more sessions), and by the time `noNe` cleared that high had aged out of the three
        # most recent pivot highs. The candidate tracker, which has no such restriction, found
        # the base and priced it at 110.50 - the exact MarketSmith pivot.
        # Additive by construction: pattern_name/pattern_code are unchanged, so primary
        # accuracy is identical (exact 90/172, broad 126/172). Layered broad 146 -> 152,
        # layered exact 135 -> 139, pivot within 1% 125 -> 132, and the count of events with
        # no reading at all goes 7 -> 0.
        if (latest['pOn'] and (latest['pCode'] > 0)) or patterns:
            # VCP qualifies the host base rather than replacing it, so it is only surfaced
            # when the active pattern is one it can form inside.
            vcp_host_ok = latest['pName'] in VCP_HOST_PATTERNS
            vcp_on = bool(latest['vcpReady'] and vcp_host_ok)
            last_bar = latest['bar']
            vcp_dist_pct = None
            if vcp_on and latest['vcpPivot']:
                vcp_dist_pct = (latest['close'] - latest['vcpPivot']) / latest['vcpPivot'] * 100.0
            result = {
                'ticker': str(ticker),
                'date': str(latest['date']),
                'close': float(round(latest['close'], 2)),
                'pattern_name': str(latest['pName']),
                'pattern_code': int(latest['pCode']),
                'status': 'In Base' if latest['inBase'] else 'Post-BO',
                'days_in_base': int(latest['bCount']) if latest['bCount'] is not None else None,
                'bars_sbo': int(latest['barsSBO']) if latest['barsSBO'] is not None else None,
                'dist_pct': float(round(latest_dist, 2)) if latest_dist is not None else None,
                'pct_off_52w_high': float(round(pctOff52wHigh, 2)),
                'before_bo_score': int(latest['beforeBOScore']),
                'post_bo_score': int(latest['postBOScore']),
                'composite_score': int(latest['compositeScore']),
                'rs_nh_count': int(latest['rsCount']),
                'vol_dry_up': bool(latest['volDryUp']),
                'pocket_pivot': bool(latest['ppAny']),
                'touched_ma': bool(latest['touchedMA']),
                'shakeout_entry': bool(latest['shakeoutEntry']),
                'upside_reversal': bool(latest['upsideReversal']),
                'rs_nh': bool(latest['rsNH']),
                # --- layered readings: every defensible pattern for this base, ranked by
                # specificity, each with the pivot IT prices off. patterns[0] is the primary.
                'patterns': patterns,
                'pattern_count': len(patterns),
                # --- High Tight Flag CONTEXT ------------------------------------------
                # A base forming inside the flag is the tradable pattern and keeps its own
                # buy point; the flag is the structure enclosing it. Reported alongside so
                # "a cup inside the flag part of an HTF" is visible as exactly that, rather
                # than one of the two silently replacing the other.
                'in_htf_flag': bool(latest.get('isHTFFlag')) or bool(_htf_ctx),
                'htf_flag_high': latest.get('htfFlagHigh') or (
                    _htf_ctx['flag_high'] if _htf_ctx else None),
                'htf_context': _htf_ctx,
                # --- buy point (see the note above the computation) ---
                'pivot': float(round(latest_pivot, 2)) if latest_pivot else None,
                'conservative_pivot': float(round(cons_pivot, 2)) if cons_pivot else None,
                'pivot_ambiguity_pct': float(round(amb_pct, 1)) if amb_pct is not None else None,
                'conservative_dist_pct': float(round(cons_dist, 2)) if cons_dist is not None else None,
                # --- VCP sub-pattern (forms inside Cup+Handle / Cup / Flat Base / Consolidation) ---
                'vcp': vcp_on,
                'vcp_forming': bool(latest['vcpActive'] and vcp_host_ok and not vcp_on),
                'vcp_contractions': int(latest['vcpLegs']) if vcp_host_ok else 0,
                'vcp_pivot': float(round(latest['vcpPivot'], 2)) if (vcp_on and latest['vcpPivot']) else None,
                'vcp_last_depth_pct': float(round(latest['vcpLastDepth'], 2)) if (vcp_on and latest['vcpLastDepth'] is not None) else None,
                'vcp_dist_pct': float(round(vcp_dist_pct, 2)) if vcp_dist_pct is not None else None,
                'vcp_breakout': bool(latest['vcpBO'] and vcp_host_ok),
                # Leg geometry for drawing the contraction sequence as it forms.
                'vcp_legs': [dict(c) for c in vcp['detail'][last_bar]] if vcp_host_ok else [],
                'history': history_state,
                'df_trim_offset': df_trim_offset
            }
            return result
        return None
        
    except Exception as e:
        return None


def run_ibd_pattern_scan(max_workers: int = None):
    """Run full scan over all parquet files in ticker_cache/."""
    if max_workers is None:
        max_workers = os.cpu_count() or 8
        
    start_time = time.time()
    files = glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet"))
    print(f"🔍 Found {len(files)} ticker cache files in {TICKER_CACHE_DIR}")
    
    # Load SPY close series if available
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    spy_close = None
    if spy_path.exists():
        try:
            spy_df = pd.read_parquet(spy_path)
            spy_close = spy_df['Close']
        except Exception:
            pass

    results = []
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for f in files:
            ticker = Path(f).name.split("_1d.parquet")[0]
            if ticker in ["SPY", "QQQ", "IWM"]:
                continue
            fut = executor.submit(scan_single_ticker, ticker, f, spy_close)
            futures[fut] = ticker
            
        count = 0
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                results.append(res)
            count += 1

    # Sort results by composite_score desc, pattern_code desc, ticker asc
    results.sort(key=lambda x: (-x['composite_score'], x['pattern_code'], x['ticker']))
    
    elapsed = time.time() - start_time
    print(f"✅ Scan completed in {elapsed:.2f} seconds! Found {len(results)} pattern signals.")
    
    # Save to JSON
    OUTPUT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        
    print(f"💾 Results saved to {OUTPUT_JSON_PATH}")
    return results


if __name__ == "__main__":
    run_ibd_pattern_scan()
