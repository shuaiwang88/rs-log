"""
tv_pattern_scanner.py

Bar-by-bar Python port of the **Chart Pattern Recognition** section of
`pine/drw_pattern.pine` (TradeVizion's "MarketSmith Indicator") on the DAILY timeframe -
the indicator the user actually reads charts from.

This is deliberately NOT `ibd_pattern_scanner.py`. That one is a port of a different script
(`drw_pattern_scanner.pine`) with its own parameters, scoring and sub-signals. This module
reproduces `drw_pattern.pine`'s own state machine statement-for-statement, in the same order
Pine executes it, so that what the scan reports is what the chart draws:

    pivots  ->  baseCond/isBase  ->  [double bottom]  ->  cup  ->  handle  ->  HTF flag
            ->  line/box mutation block (base low updates, invalidation, breakout, boxes)

The double-bottom stage is switched off - see DETECT_DOUBLE_BOTTOM. Its code is left in place
so the port still maps to the Pine line for line, but detection is gated at the source, so the
machine runs the Pine's own no-DB path and no record can come back labelled one.

Every block below cites the `drw_pattern.pine` line numbers it came from. Pine semantics that
matter and are reproduced here rather than approximated:

  * `ta.pivothigh(high, 9, 9)` registers a pivot 9 bars AFTER it happens, so the base top is
    only recognised 25 bars later - the detector is intentionally lagged, and re-deriving the
    lag from scratch is how a port drifts from the chart.
  * `x[1]` is the value at the END of the previous bar, so variables that get mutated later in
    the same bar (isBase on a breakout) still read their pre-mutation value earlier in it.
  * `var` variables persist; plain locals reset each bar but keep history for `[n]`.
  * Line objects are used by the script AS STATE (`line.get_y1(highLine)` is the live base top
    and `line.get_y1(lowLine)` the live base low), including going na when a line is deleted.
    They are modelled here as plain floats that become NaN on delete.

Four places in the Pine are self-contradictory, double-scaled, or the opposite of their own
tooltip. They are NOT silently "fixed": each is a switch in PINE_QUIRKS below, documented with
the line number, so flipping one and rerunning reproduces the literal reading. Two further
problems are structural rather than constants - a handle branch that cannot fire and a cup
flag that decays as the cup completes - and are written up under PINE_QUIRKS as well.

Two outputs:

  * `python/tv_pattern_results.json` - one record per ticker with a pattern live on the last
    bar, plus the exact geometry (`overlay`) needed to redraw the Pine's own shapes in
    `tv_pattern_chart.py`: the cup's two exponential arcs, the base channel a flat base or
    consolidation is drawn as, the handle box, the flag pole, the 3-weeks-tight squeeze boxes
    and the trade boxes.
  * `python/tv_pattern_history.json` - every base the replay has ever ended, from every ticker
    including the ones with nothing live today, with what each breakout was worth. The Pine
    only ever draws its current state, so this is the part of the record the chart throws away.
"""

import glob
import json
import math
import os
import sys
import time
import warnings
from bisect import bisect_left
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_JSON_PATH = ROOT_DIR / "python" / "tv_pattern_results.json"
HISTORY_JSON_PATH = ROOT_DIR / "python" / "tv_pattern_history.json"
TREND_PARQUET_PATH = ROOT_DIR / "python" / "trend_metrics.parquet"

# TradingView feeds the indicator the whole chart; 1500 daily bars (~6 years) is far more than
# the longest state the machine carries (a 325-bar base plus its 103-bar prior leg), and it is
# what the sibling scanner uses, so the two see the same history.
MAX_BARS = 1500
NAN = float("nan")

SKIP_TICKERS = {"SPY", "QQQ", "IWM"}

# ── Pine inputs, verbatim (drw_pattern.pine:447, 486-517) ────────────────────────────────
I_PIVOT                = 9        # line 447, shared by the pattern block
I_BASE_DEPTH           = 0.35     # line 489 (35) rescaled by line 519
I_BASE_LENGTH          = 325      # line 490 (65 weeks) rescaled by line 521
I_DB_DEPTH             = 0.40     # line 491 (40) rescaled by line 520
I_DB_LENGTH            = 85       # line 492 (17 weeks) rescaled by line 522
I_DB_PRIOR_UPTREND     = 30.0     # line 493
I_DB_MIN_LENGTH_WEEKS  = 7        # line 494
I_DB_REQUIRE_UNDERCUT  = True     # line 495
I_SHOW_CUP_HANDLE_SEP  = True     # line 498
I_HANDLE_MIN_BARS      = 5        # line 499
I_HANDLE_MAX_BARS      = 25       # line 500
I_HANDLE_MAX_DECLINE   = 12.0     # line 501
I_HANDLE_MIN_HEIGHT    = 60.0     # line 502
I_USE_VOL_HANDLE       = True     # line 503
I_REQUIRE_CUP_HANDLE   = True     # line 504
I_VOL_MA_HANDLE        = 50       # line 505
I_VOL_CONTRACTION_MAX  = 50.0     # line 506
I_VOL_BREAKOUT_MULT    = 1.5      # line 507
I_HTF_ON               = True     # line 509
I_HTF_DEPTH            = 25.0     # line 510
I_HTF_FMAX             = 20       # line 511
I_HTF_FMIN             = 6        # line 512
I_HTF_POLE             = 85.0     # line 514
I_HTF_PB               = 40       # line 515
I_HTF_PBMIN            = 5        # line 516
I_HTF_VOL              = 1.1      # line 517

# ── Where drw_pattern.pine contradicts itself ────────────────────────────────────────────
#
# Lines 519-522 rescale four inputs in place (35 -> 0.35, 65 weeks -> 325 bars ...). Three
# later expressions rescale the SAME value a second time. Only one of the three is fatal, but
# all three change what gets detected, so all three are switchable.
#
#   db_depth_double_scaled  line 662: `sL >= (1.0 - i_dbDepth / 100.0) * peak` with i_dbDepth
#       already 0.40 asks for the second low to sit within 0.4% of the pattern's peak, while
#       line 663 requires it at least 5% BELOW that peak. The two can never both hold, so a
#       literal reading detects zero double bottoms, ever. Default False = the intended
#       "at most 40% deep" (floor 0.60 * peak). Set True to reproduce the literal Pine.
#
#   db_length_double_scaled line 636: `int maxDbBars = i_dbLength * 5` with i_dbLength already
#       85 gives a 425-bar search window - 85 weeks - while the input right above it is
#       labelled "Max Duration (Weeks): 17". Default False = the advertised 17 weeks. LOGI is
#       what the literal reading buys: a 136-bar span called a double bottom.
#
#   db_undercut_literal     line 661: with i_dbRequireUndercut on, the test is `sL < fL * 1.05`,
#       so the second bottom may sit up to 5% ABOVE the first - the opposite of the input's own
#       tooltip, "2nd bottom low must undercut (be strictly lower than) 1st bottom low". LOGI
#       passed with a second low 4.5% above the first (91.32 vs 87.38, against a 91.75 limit).
#       Default False = the tooltip's `sL < fL`. Set True for the literal 1.05.
#
#   cup_ratio_int_division  lines 733/738/750: `numberOfValidCl1 / baseTier >= 0.3` divides
#       two ints. If Pine truncates that to an int, the test becomes "count >= tier", i.e.
#       effectively 100% rather than 30%/90%, and almost nothing is a cup. Default False =
#       true ratios, which is what the 0.3 / 0.90 literals plainly mean.
PINE_QUIRKS = {
    "db_depth_double_scaled":  False,
    "db_length_double_scaled": False,
    "db_undercut_literal":     False,
    "cup_ratio_int_division":  False,
}

# ── Double bottoms are switched off (user request, 2026-08-03) ────────────────────────────
#
# The three db_* quirks above are kept because they document what the Pine says, but nothing
# reads them while this is False. Gating `detectDB` at its source is what makes the removal
# total rather than cosmetic: `isDoubleBottom` seeds False and is only ever set from detectDB,
# so with detection off every downstream branch that tests it is inert, and the state machine
# is exactly the Pine's own non-DB path. That is not just tidier, it closes both DB-gated
# state holes found on 2026-08-03 - the expiry branch (lines 1092-1099) that deleted the base
# lines with no replacement and left the base un-invalidatable, and line 1119's
# `not isDoubleBottom[1]` guard that let AU carry a 35.2% base past the 35% cap. Bases that
# used to be reported as "Dbl Bottom" now report as whatever they are without the W: a Cup, or
# a Base priced off the base top, which is the higher and more conservative buy point.
#
# Set True to bring them back; nothing else needs changing.
DETECT_DOUBLE_BOTTOM = False

# ── And one that is not switchable, because it is structural, not a constant ──────────────
#
# The separate cup-with-handle detector (i_showCupHandleSep, lines 759-820) never fires on
# daily data. Its three state variables - rightSideHighCloseSep, handlePeakBarSep and
# handleLowPointSep - are only ever reset when `rightSide` goes false (lines 762, 771, 792),
# and `rightSide = isBase and bar_index > lowerBaseBar` stays true for the rest of the base
# once the base has a low. handleLowPointSep is therefore a running minimum reaching back to
# the base low rather than to the handle peak, so line 804's "handle must sit above the middle
# of the base" fails by construction. Measured over the full ticker_cache: of 1476 live bases,
# 16 pass that test, 5 pass every test except volume, and 0 pass all of them.
#
# Nothing here papers over that - a cup base is reported as "Cup" and priced off the base top,
# which is the higher, more conservative buy point. `handle_gates` in each record shows how
# close a base came, so near-misses stay visible instead of being invented.
#
# ── CUP_LATCH: why the name comes from isCup, not detectCup ──────────────────────────────
#
# The Pine keeps two cup flags. `detectCup` is recomputed every bar and is what the drawing
# code tests (lines 1006, 1194); `isCup` latches on the first detectCup and is what the handle
# logic tests (line 815). Naming a base from detectCup alone is wrong, because that flag falls
# false exactly when a cup finishes:
#
#   condThird (line 738) asks that the base's middle THIRD - lookback offsets
#   [baseTier, baseCount - baseTier] - close below the cup midpoint on 90% of its bars. Those
#   offsets are measured from today, so as the base ages the window slides forward off the cup
#   bottom and into the right-side recovery, where price is back above the midpoint. SNOW:
#   10/10 bars qualifying on 2026-07-15 (detectCup true), 9/11 two days later, 7/13 by the
#   2026-07-29 breakout - the count falls while the denominator grows, so a completed cup
#   scores worse than a half-built one. It was a "Cup" for exactly 2 bars, then a "Base".
#
# So the label follows `isCup`, scoped to the CURRENT base (cup_bars_in_base > 0) rather than
# latched forever as the Pine leaves it - which also repairs the Pine's own never-reset bug.
# 568 of 1430 bases were affected. Detection is untouched; `cup_bars_in_base` and
# `cup_last_bars_ago` travel with every record so the strength of the evidence stays visible.


def _json_safe(o):
    """Fallback encoder for numpy scalars / NaN / Timestamps (see ibd_pattern_scanner.py)."""
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    if o is pd.NaT:
        return None
    if isinstance(o, pd.Timestamp) or hasattr(o, "isoformat"):
        return str(o)
    if isinstance(o, float) and math.isnan(o):
        return None
    raise TypeError(f"Object of type {type(o).__module__}.{type(o).__name__} "
                    f"is not JSON serializable")


def _f(x):
    """Round for JSON, mapping NaN/None to None so the dashboard gets a real gap."""
    if x is None:
        return None
    x = float(x)
    return None if (math.isnan(x) or math.isinf(x)) else round(x, 4)


class _RangeExtreme:
    """O(1) range max/min (sparse table).

    Needed because four `ta.highest/ta.lowest` calls in the Pine take a length that changes
    every bar - `ta.highest(high, math.min(4950, math.max(1, bar_index - 26)))[26]` and
    friends - which no fixed rolling window reproduces.
    """

    __slots__ = ("_levels", "_f", "_n")

    def __init__(self, a, kind="max"):
        a = np.asarray(a, dtype=float)
        n = len(a)
        self._n = n
        self._f = np.maximum if kind == "max" else np.minimum
        self._levels = [a]
        j = 1
        while (1 << j) <= n:
            prev = self._levels[-1]
            half = 1 << (j - 1)
            width = n - (1 << j) + 1
            self._levels.append(self._f(prev[:width], prev[half:half + width]))
            j += 1

    def query(self, lo, hi):
        lo = max(0, int(lo))
        hi = min(self._n - 1, int(hi))
        if hi < lo:
            return NAN
        k = (hi - lo + 1).bit_length() - 1
        lv = self._levels[k]
        return float(self._f(lv[lo], lv[hi - (1 << k) + 1]))


def _pivot_flags(values, left, right, kind="high"):
    """`ta.pivothigh` / `ta.pivotlow`: strictly beyond every bar in the +/- window."""
    n = len(values)
    out = np.zeros(n, dtype=bool)
    w = left + right + 1
    if n < w:
        return out
    win = np.lib.stride_tricks.sliding_window_view(values, w)
    if kind == "high":
        ext = win.max(axis=1)
    else:
        ext = win.min(axis=1)
    # A tie anywhere in the window means the centre is not strictly the extreme, so it is not
    # a pivot - matching find_pivots() in the sibling scanner and TradingView's behaviour.
    unique = (win == ext[:, None]).sum(axis=1) == 1
    centres = values[left:n - right]
    out[left:n - right] = (centres == ext) & unique
    return out


def _acc_dis_neutral(closes, volumes, end_bar, days):
    """Pine's base label loop (drw_pattern.pine:1180-1190): accumulation / distribution days."""
    if days <= 0:
        return 0, 0, 0
    i0 = max(1, end_bar - days + 1)
    if i0 > end_bar:
        return 0, 0, 0
    c = closes[i0:end_bar + 1]
    cp = closes[i0 - 1:end_bar]
    v = volumes[i0:end_bar + 1]
    vp = volumes[i0 - 1:end_bar]
    acc = int(np.count_nonzero((c > cp) & (v > vp)))
    dis = int(np.count_nonzero((c < cp) & (v > vp)))
    return acc, dis, len(c) - acc - dis


def _tight_closes(idx, o, h, l, c, since_bar):
    """Pine's Tight Closes Detector (drw_pattern.pine:409-440), the 3-weeks-tight squeeze box.

    The Pine runs this block only under `if tfWeekly`, so it draws nothing on the daily chart
    this module ports - and its `WtClose` input ships false besides. It is the one shape the
    indicator has for "price has stopped moving", which is what a flat base or a tight
    consolidation looks like, so the daily bars are resampled to weeks here and the conditions
    applied verbatim: three closes within +/-1.5% of each other, three highs OR three lows
    equally tight, and a first candle that is either mostly wick or narrower than the week
    after it. Box corners are the Pine's: highest high and lowest low of the three weeks,
    spanning weekly bar_index[2] to bar_index (lines 438-440).

    Returns boxes whose last week ends at or after `since_bar`, oldest first.
    """
    if len(idx) < 15:
        return []
    df = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}, index=pd.DatetimeIndex(idx))
    wk = df.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
    wk = wk.dropna()
    if len(wk) < 3:
        return []
    # First and last daily bar inside each weekly bucket, so a weekly box maps back to days.
    bucket = pd.Series(np.arange(len(df)), index=df.index).resample("W-FRI").agg(["first", "last"])
    bucket = bucket.reindex(wk.index).dropna()
    if len(bucket) < 3:
        return []
    wk = wk.loc[bucket.index]
    O, H, L, C = (wk[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    first_bar = bucket["first"].to_numpy(int)
    last_bar = bucket["last"].to_numpy(int)

    thr = 0.015
    out = []
    for i in range(2, len(wk)):
        if last_bar[i] < since_bar:
            continue
        c0, c1, c2 = C[i], C[i - 1], C[i - 2]
        h0, h1, h2 = H[i], H[i - 1], H[i - 2]
        l0, l1, l2 = L[i], L[i - 1], L[i - 2]
        o2 = O[i - 2]
        near = lambda a, b: b * (1 - thr) < a < b * (1 + thr)
        tight_close = near(c0, c1) and near(c1, c2) and near(c0, c2)
        if not tight_close:
            continue
        tight_high = near(h0, h1) and near(h1, h2)
        tight_low = near(l0, l1) and near(l1, l2)
        if not (tight_high or tight_low):
            continue
        if c2 >= o2:                                              # lines 429-434
            first_candle = (h2 - c2 + o2 - l2) > 2 * (c2 - o2) or (h2 - l2) < (h1 - l1)
        else:
            first_candle = (h2 - o2 + c2 - l2) > 2 * (o2 - c2) or (h2 - l2) < (h1 - l1)
        if not first_candle:
            continue
        out.append({
            "start_date": str(pd.Timestamp(idx[first_bar[i - 2]]).date()),
            "end_date": str(pd.Timestamp(idx[last_bar[i]]).date()),
            "high": _f(max(h0, h1, h2)),
            "low": _f(min(l0, l1, l2)),
        })
    return out


def _iso(idx, bar):
    """Bar index -> ISO date string, so the chart never has to reconstruct an offset."""
    if bar is None or (isinstance(bar, float) and math.isnan(bar)):
        return None
    bar = int(bar)
    if bar < 0 or bar >= len(idx):
        return None
    return str(pd.Timestamp(idx[bar]).date())


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The port
# ══════════════════════════════════════════════════════════════════════════════════════════
def _trend_row(ticker: str, raw: pd.DataFrame):
    """SMA50 / SMA200 / 50-day average volume, for `app.py`'s trend filters.

    Nothing to do with the Pine. It lives here because this scan already opens every parquet in
    ticker_cache, so these three numbers are free at this point - whereas the dashboard was
    deriving them by reopening one parquet per ticker on every cold load, ~8,200 files and 19
    seconds of it. Written to trend_metrics.parquet by run_scan.

    Deliberately computed off the RAW frame - sorted only, not de-duplicated and not trimmed to
    MAX_BARS - because that is exactly what `_ibd_trend_metrics` did, and the two must agree to
    the last decimal or the dashboard's filters shift under the user.
    """
    if raw is None or len(raw) < 50 or not {"Close", "Volume"} <= set(raw.columns):
        return None
    c = pd.to_numeric(raw["Close"], errors="coerce")
    v = pd.to_numeric(raw["Volume"], errors="coerce")
    return {
        "ticker": str(ticker),
        "sma50": c.rolling(50, min_periods=50).mean().iloc[-1],
        "sma200": c.rolling(200, min_periods=200).mean().iloc[-1] if len(raw) >= 200 else np.nan,
        "avg_vol50": v.rolling(50, min_periods=25).mean().iloc[-1],
    }


def scan_ticker(ticker: str, file_path: str, spy_close: pd.Series = None, debug: int = 0,
                df: pd.DataFrame = None):
    """Replay drw_pattern.pine over one ticker and report the state of the LAST bar.

    Returns None when nothing is live on the last bar - no base, no trade box still open and
    no flag - which is the same thing as the indicator drawing nothing on that chart today.

    `df` lets the caller hand over a frame it has already read, so the worker can compute the
    trend metrics off the same read instead of opening the file twice.
    """
    if df is None:
        try:
            df = pd.read_parquet(file_path)
        except Exception:
            return None
    if df is None or df.empty:
        return None
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in df.columns:
            return None

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(subset=["High", "Low", "Close"])
    if len(df) > MAX_BARS:
        df = df.iloc[-MAX_BARS:]
    n = len(df)
    if n < 120:                       # under ~6 months nothing can complete a base anyway
        return None

    idx = df.index
    open_ = df["Open"].to_numpy(dtype=float)      # only the tight-closes box reads this
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    volume = np.nan_to_num(df["Volume"].to_numpy(dtype=float), nan=0.0)
    if not np.isfinite(close[-1]) or close[-1] <= 0:
        return None

    # ── precomputed ta.* series ──────────────────────────────────────────────────────────
    s_high, s_low, s_vol = pd.Series(high), pd.Series(low), pd.Series(volume)
    highest25 = s_high.rolling(25).max().to_numpy()          # line 561
    lowest25 = s_low.rolling(25).min().to_numpy()            # line 563
    highest10 = s_high.rolling(10).max().to_numpy()          # line 844  _flagHighest10
    highest65 = s_high.rolling(65).max().to_numpy()          # line 568  (shifted 26 below)
    htf_avg_vol = s_vol.rolling(20).mean().to_numpy()        # line 560
    vol_ma_handle = s_vol.rolling(I_VOL_MA_HANDLE).mean().to_numpy()   # line 806
    vol_cum = np.concatenate([[0.0], np.cumsum(volume)])     # line 808  math.sum(volume, len)
    rmq_high = _RangeExtreme(high, "max")
    rmq_low = _RangeExtreme(low, "min")
    ph_flags = _pivot_flags(high, I_PIVOT, I_PIVOT, "high")
    pl_flags = _pivot_flags(low, I_PIVOT, I_PIVOT, "low")

    quirk_db_depth = PINE_QUIRKS["db_depth_double_scaled"]
    quirk_db_len = PINE_QUIRKS["db_length_double_scaled"]
    quirk_cup_int = PINE_QUIRKS["cup_ratio_int_division"]
    quirk_undercut = PINE_QUIRKS["db_undercut_literal"]
    db_floor = (1.0 - I_DB_DEPTH / 100.0) if quirk_db_depth else (1.0 - I_DB_DEPTH)
    max_db_bars = (I_DB_LENGTH * 5) if quirk_db_len else I_DB_LENGTH
    min_db_bars = I_DB_MIN_LENGTH_WEEKS * 5                  # line 674, daily timeframe

    # ── Pine state ───────────────────────────────────────────────────────────────────────
    piv_h_prices, piv_h_bars = [], []          # newest-first, as Pine's array.unshift leaves them
    piv_l_prices, piv_l_bars = [], []
    pl_bars_asc, pl_prices_asc = [], []        # same lows, ascending, for bisect

    start_base_price, low_base_price = [], []  # newest-first stacks (lines 526-527)
    bottom_db_price, top_db_price = [], []
    low_of_base_high = 0.0
    start_base_bar = 0
    lower_base_bar = 0
    top_db_bar = 0
    db_fH = db_fL = db_sH = db_sL = NAN
    db_fHt = db_fLt = db_sHt = db_sLt = -1

    base_cond = False                          # `var baseCond = false` (line 565)
    is_base_hist = np.zeros(n, dtype=bool)     # isBase[n] lookback (needs [1] and [25])
    is_base_prev = False
    is_db_prev = False
    is_cup_sticky = False                      # isCup is never reset in the Pine - see line 755
    base_count = 0
    is_box_prev, box_over_prev = False, True
    cond_third_prev = cond_third_two_prev = False
    barssince_low = None                       # ta.barssince(low == lowestBaseLow) (line 591)

    high_line_y = NAN                          # line.get_y1(highLine) - live base top
    low_line_y = NAN                           # line.get_y1(lowLine)  - live base low
    db_line1_x1 = NAN                          # firstPivTime (line 1100)
    db_line2_x2 = NAN                          # thrdPivTime  (line 1101)
    active_pivot = 0.0                         # `var float activePivot` (line 977)

    right_side_high_close_prev = 0.0           # lines 761-768
    handle_peak_bar_prev = 0                   # lines 770-779
    handle_peak_locked_prev = 0.0              # lines 783-789
    handle_low_point_prev = 0.0                # lines 791-798

    flag_base_high = NAN                       # lines 823-830
    flag_start_index = -1
    flag_flag_length = 0
    flag_base_low = NAN
    flag_low_index = -1
    flag_flag_bool = False
    flag_pole_low = NAN
    flag_pole_low_index = -1
    flag_base_high_prev = NAN
    flag_flag_length_prev = 0
    is_flag_prev = False

    # bookkeeping this port adds (not Pine state): what the last bar needs to report
    cup_bars_in_base = 0
    cup_last_bar = None
    breakout_bar = None
    bo_info = None
    box_end_bar = None
    handle_box_live = False

    detect_cup = handle_forming = handle_breakout = False
    is_base = is_double_bottom = is_flag = is_htf = is_box = False
    htf_snap = None

    # ── every base this ticker has formed, not just the one live today ───────────────────
    # The Pine only ever draws its current state, so a base that broke out last March leaves
    # nothing on the chart. Each base is recorded here at the moment it ends, with the same
    # anchors the live record carries so the same chart code can redraw it.
    history = []

    def _record(end_bar, ended, pattern, top, low_, pivot_):
        """One finished base: the shape it was, where it ended and what it was worth."""
        if math.isnan(top) or top <= 0 or end_bar <= start_base_bar:
            return
        days = end_bar - start_base_bar
        depth = ((top - low_) / top * 100.0) \
            if (not math.isnan(low_) and low_ > 0) else NAN
        a, d, nu = _acc_dis_neutral(close, volume, end_bar, days)
        shape = None
        if pattern == "Base":
            shape = "Flat Base" if (not math.isnan(depth) and depth <= 15.0 and days >= 25) \
                else "Consolidation"
        ep = {
            "pattern": pattern, "base_shape": shape, "ended": ended,
            "start_date": _iso(idx, start_base_bar), "end_date": _iso(idx, end_bar),
            "end_bar": int(end_bar),
            "pivot": _f(pivot_), "base_top": _f(top), "base_low": _f(low_),
            "depth_pct": _f(depth), "days": int(days),
            "acc_days": int(a), "dis_days": int(d), "neu_days": int(nu),
            "cup_bars": int(cup_bars_in_base),
            "base_start_date": _iso(idx, start_base_bar),
            "base_low_date": _iso(idx, lower_base_bar),
        }
        if pattern in ("Cup", "Cup+Handle"):
            ep["cup"] = {
                "start_up": _f(low_of_base_high * 0.99),
                "bottom": _f((low_base_price[0] if low_base_price else NAN) * 0.99),
                "end_up": _f(low[end_bar - 1] * 0.99) if end_bar >= 1 else None,
                "left_bars": int(max(0, lower_base_bar - start_base_bar)),
                "right_bars": int(max(0, end_bar - lower_base_bar)),
            }
        history.append(ep)
    debug_rows = []
    handle_peak_locked = handle_low_point = 0.0
    handle_bar_count = 0
    handle_peak_bar = 0
    vol_avg_handle = NAN

    for t in range(n):
        # ── pivots: registered i_pivot bars late (lines 548-556) ─────────────────────────
        pb = t - I_PIVOT
        if pb >= I_PIVOT:
            if ph_flags[pb]:
                piv_h_prices.insert(0, high[pb])
                piv_h_bars.insert(0, pb)
            if pl_flags[pb]:
                piv_l_prices.insert(0, low[pb])
                piv_l_bars.insert(0, pb)
                pl_bars_asc.append(pb)
                pl_prices_asc.append(low[pb])

        # ── baseCond (lines 558-590) ─────────────────────────────────────────────────────
        if piv_h_prices:
            if t >= 25:
                h25 = high[t - 25]
                cnt = len(piv_h_prices)
                bool_high_base = (h25 == piv_h_prices[0]
                                  or (cnt > 1 and h25 == piv_h_prices[1])
                                  or (cnt > 2 and h25 == piv_h_prices[2]))

                # highestPrior: 13-week high ending one bar before the candidate top. Falls
                # back to "everything available" while the 65-bar window is still na.
                h65s = highest65[t - 26] if t >= 26 else NAN
                if math.isnan(h65s):
                    if t >= 26:
                        # ta.highest(high, L)[26] evaluates the length AT bar t-26, not now.
                        span = min(4950, max(1, (t - 26) - 26))
                        highest_prior = rmq_high.query((t - 26) - span + 1, t - 26)
                    else:
                        highest_prior = NAN
                else:
                    highest_prior = h65s

                bool_13wk_high = (not math.isnan(highest_prior)) and h25 >= highest_prior
                prior_top = start_base_price[0] if start_base_price else NAN
                bool_prior_base_bo = (not math.isnan(prior_top)) and h25 > prior_top
                bool_higher_piv = bool_13wk_high or bool_prior_base_bo

                leg_span = min(103, max(1, t))
                lowest_ipo_leg = rmq_low.query(t - leg_span + 1, t)
                leg_up_cond = h25 >= lowest_ipo_leg * 1.20
                first_base_depth = (h25 * (1 - I_BASE_DEPTH)) <= lowest25[t]
                no_candle_above = highest25[t] <= h25
                no_base_in_base = not bool(is_base_hist[t - 25])
                base_cond = bool(bool_high_base and bool_higher_piv and leg_up_cond
                                 and first_base_depth and no_candle_above and no_base_in_base)
            else:
                base_cond = False

        # ── ta.barssince(low == lowestBaseLow) (line 591) ────────────────────────────────
        if not math.isnan(lowest25[t]) and low[t] == lowest25[t]:
            barssince_low = 0
        elif barssince_low is not None:
            barssince_low += 1

        # ── a base is born (lines 592-604) ───────────────────────────────────────────────
        is_base = False
        if base_cond and not is_base_prev:
            start_base_price.insert(0, high[t - 25])
            start_base_bar = t - 25
            low_of_base_high = low[t - 25]
            low_base_price.insert(0, lowest25[t])
            lower_base_bar = t - (barssince_low if barssince_low is not None else 0)
            cup_bars_in_base = 0
            cup_last_bar = None
        if base_cond or is_base_prev:
            is_base = True

        # ── baseCount (lines 606-612) ────────────────────────────────────────────────────
        if base_cond and not is_base_prev:
            base_count = t - start_base_bar
        if is_base:
            base_count += 1

        # ── trade box carries over while it has not been closed out (lines 614-618) ──────
        is_box = False
        box_over = True
        if is_box_prev and not box_over_prev:
            is_box = True

        # ── Double Bottom (lines 620-700) - off, see DETECT_DOUBLE_BOTTOM ────────────────
        detect_db = False
        if DETECT_DOUBLE_BOTTOM and is_base:
            num_hp, num_lp = len(piv_h_prices), len(piv_l_prices)
            if num_hp >= 2 and num_lp >= 2:
                span = max(1, min(t, 250))
                prior_low_full = rmq_low.query(t - span + 1, t)     # line 622
                max_hp = min(num_hp - 1, 4)
                found = False
                for hp_i in range(0, max_hp):
                    for hp_j in range(hp_i + 1, min(num_hp - 1, hp_i + 4) + 1):
                        sH, fH = piv_h_prices[hp_i], piv_h_prices[hp_j]
                        sHt, fHt = piv_h_bars[hp_i], piv_h_bars[hp_j]
                        if fHt < t - max_db_bars:
                            continue
                        # Pine scans its newest-first arrays and breaks on the first hit, i.e.
                        # takes the LATEST qualifying pivot low in each window.
                        k = bisect_left(pl_bars_asc, sHt) - 1
                        if k >= 0 and pl_bars_asc[k] > fHt:
                            fLt, fL = pl_bars_asc[k], pl_prices_asc[k]
                        else:
                            fLt, fL = -1, NAN
                        if pl_bars_asc and pl_bars_asc[-1] > sHt:
                            sLt, sL = pl_bars_asc[-1], pl_prices_asc[-1]
                        else:
                            sLt, sL = -1, NAN
                        if fLt == -1 or sLt == -1:
                            continue

                        peak = max(fH, sH)
                        thr = 1.0 + I_DB_PRIOR_UPTREND / 100.0
                        cond_prior_trend = (fH >= prior_low_full * thr) or (sH >= prior_low_full * thr)
                        if start_base_price:
                            _bt = start_base_price[0]
                            cond_prices_h = (abs(fH - _bt) < _bt * 0.15) or (abs(sH - _bt) < _bt * 0.15)
                        else:
                            cond_prices_h = True
                        if I_DB_REQUIRE_UNDERCUT:
                            cond_prices_a = (sL < fL * 1.05) if quirk_undercut else (sL < fL)
                        else:
                            cond_prices_a = (sL <= fL * 1.05 or sL >= fL * 0.80)
                        cond_prices_b = sL >= db_floor * peak
                        cond_prices_c = sL <= peak * 0.95
                        cond_prices_d = sH >= sL + (peak - sL) * 0.15
                        cond_prices_e = sH <= fH * 1.10
                        cond_prices_f = sH >= fL + (fH - fL) * 0.15
                        cond_prices_g = (fH - fL) > 0 and (sH - sL) > 0
                        cond_time_a = fHt < fLt < sHt < sLt
                        cond_time_b = (sLt - fHt) <= max_db_bars
                        cond_time_c = (sLt - fHt) >= min_db_bars
                        check_len = min(4950, max(1, t - sLt + 1))
                        cond_both_a = rmq_high.query(t - check_len + 1, t) <= sH * 1.02

                        if (cond_prior_trend and cond_prices_h and cond_prices_a and cond_prices_b
                                and cond_prices_c and cond_prices_d and cond_prices_e
                                and cond_prices_f and cond_prices_g and cond_time_a
                                and cond_time_b and cond_time_c and cond_both_a):
                            detect_db = True
                            db_fH, db_fHt, db_fL, db_fLt = fH, fHt, fL, fLt
                            db_sH, db_sHt, db_sL, db_sLt = sH, sHt, sL, sLt
                            bottom_db_price.insert(0, sL)
                            top_db_price.insert(0, sH)
                            top_db_bar = sHt
                            found = True
                            break
                    if found:
                        break
        is_double_bottom = detect_db or is_db_prev

        # ── Cup (lines 705-757) ──────────────────────────────────────────────────────────
        detect_cup = False
        cond_third = cond_third_two = False
        if is_base:
            high_cup = start_base_price[0] if start_base_price else 0.0
            low_cup = low_base_price[0] if low_base_price else 0.0
            middle_of_cup = low_cup + (high_cup - low_cup) * 0.5
            cond_max_depth = low_cup >= (1 - I_BASE_DEPTH) * high_cup
            cond_min_depth = low_cup <= 0.92 * high_cup
            cond_max_length = (t - start_base_bar) <= I_BASE_LENGTH
            cond_min_length = (t - start_base_bar) >= 30
            cond_absolute_pos = ((high_cup - low_cup) * 0.5 + low_cup) <= high[t]

            base_tier = base_count // 3          # int, or the Pine `for` bounds would not compile
            base_fourth = base_count // 4
            cl1 = cl2 = 0

            def _count(lo_off, hi_off, above):
                """close[] over lookback offsets [hi_off .. lo_off], counted vs the midpoint."""
                a = max(0, t - lo_off)
                b = t - hi_off
                if b < a:
                    return 0
                seg = close[a:b + 1]
                return int(np.count_nonzero(seg >= middle_of_cup) if above
                           else np.count_nonzero(seg <= middle_of_cup))

            # The Pine's first loop (line 729) writes condThirdTwo, then line 745 overwrites it
            # from the quarter-based loop before anything reads it, so only three loops matter.
            if base_tier > 0:
                cl2 = _count(base_count - base_tier, base_tier, above=False)   # middle third
                cond_third = (cl2 // base_tier >= 0.90 if quirk_cup_int
                              else cl2 / base_tier >= 0.90)
            if base_fourth > 0:
                cl1 = _count(base_count, 3 * base_fourth, above=True)          # oldest quarter
                cond_third_two = (cl1 // base_fourth >= 0.3 if quirk_cup_int
                                  else cl1 / base_fourth >= 0.3)

            # cupForm's second half is dead in the Pine: condFourthTwo (line 712) is declared
            # and never assigned, so `condFourth and condFourthTwo` can never be true.
            cup_form = cond_third and cond_third_prev and cond_third_two and cond_third_two_prev
            detect_cup = bool(cond_max_depth and cond_min_depth and cond_max_length
                              and cond_min_length and cond_absolute_pos and cup_form)
            if detect_cup:
                cup_bars_in_base += 1
                cup_last_bar = t
            if debug and t >= n - debug:
                debug_rows.append({
                    "date": str(pd.Timestamp(idx[t]).date()), "bar": t,
                    "baseCount": base_count, "tier": base_tier, "fourth": base_fourth,
                    "top": round(high_cup, 2), "low": round(low_cup, 2),
                    "mid": round(middle_of_cup, 2),
                    "cl2_mid_third": cl2 if base_tier > 0 else None,
                    "cl1_old_quarter": cl1 if base_fourth > 0 else None,
                    "condThird": cond_third, "condThirdTwo": cond_third_two,
                    "maxDepth": cond_max_depth, "minDepth": cond_min_depth,
                    "minLen": cond_min_length, "absPos": cond_absolute_pos,
                    "cupForm": bool(cup_form), "detectCup": detect_cup,
                })
        if detect_cup:
            is_cup_sticky = True                 # line 755: isCup latches and is never cleared

        # ── Handle (lines 759-820) ───────────────────────────────────────────────────────
        right_side = is_base and t > lower_base_bar
        base_top_now = start_base_price[0] if start_base_price else NAN

        if not right_side:
            right_side_high_close = 0.0
        elif (close[t] > right_side_high_close_prev
              and not math.isnan(base_top_now) and close[t] <= base_top_now):
            right_side_high_close = close[t]
        else:
            right_side_high_close = right_side_high_close_prev

        if (not right_side) or right_side_high_close == 0.0:
            handle_peak_bar = 0
        else:
            min_height = base_top_now * (I_HANDLE_MIN_HEIGHT / 100.0) \
                if not math.isnan(base_top_now) else 0.0
            if (close[t] > right_side_high_close_prev * 1.05
                    and not math.isnan(base_top_now) and close[t] <= base_top_now
                    and close[t] >= min_height):
                handle_peak_bar = t
            else:
                handle_peak_bar = handle_peak_bar_prev

        handle_bar_count = (t - handle_peak_bar) if (right_side and handle_peak_bar > 0) else 0

        if (not right_side) or right_side_high_close == 0.0:
            handle_peak_locked = 0.0
        elif handle_bar_count == 0:
            handle_peak_locked = right_side_high_close
        else:
            handle_peak_locked = handle_peak_locked_prev

        if (not right_side) or handle_peak_locked == 0.0:
            handle_low_point = 0.0
        elif low[t] < handle_low_point_prev or handle_low_point_prev == 0.0:
            handle_low_point = low[t]
        else:
            handle_low_point = handle_low_point_prev

        handle_depth_ok = handle_low_point >= handle_peak_locked * (1.0 - I_HANDLE_MAX_DECLINE / 100.0)
        _bl = low_base_price[0] if low_base_price else NAN
        handle_mid_ok = (not math.isnan(base_top_now) and not math.isnan(_bl)
                         and handle_low_point > (base_top_now + _bl) * 0.5)

        vol_ma_h = vol_ma_handle[t]
        vol_avg_handle = NAN
        if right_side and handle_peak_locked > 0.0 and handle_bar_count > 0:
            a = max(0, t + 1 - handle_bar_count)
            vol_avg_handle = (vol_cum[t + 1] - vol_cum[a]) / handle_bar_count
        vol_contraction_ok = (not I_USE_VOL_HANDLE) or (
            handle_bar_count > 0 and not math.isnan(vol_avg_handle) and not math.isnan(vol_ma_h)
            and vol_avg_handle <= vol_ma_h * I_VOL_CONTRACTION_MAX / 100.0)
        require_cup_ok = (not I_REQUIRE_CUP_HANDLE) or is_cup_sticky or is_double_bottom

        handle_forming = bool(I_SHOW_CUP_HANDLE_SEP and right_side and handle_peak_locked > 0.0
                              and I_HANDLE_MIN_BARS <= handle_bar_count <= I_HANDLE_MAX_BARS
                              and handle_depth_ok and handle_mid_ok and vol_contraction_ok
                              and require_cup_ok)
        vol_breakout_ok = (not I_USE_VOL_HANDLE) or (
            not math.isnan(vol_ma_h) and volume[t] >= I_VOL_BREAKOUT_MULT * vol_ma_h)
        handle_breakout = handle_forming and close[t] > handle_peak_locked and vol_breakout_ok

        # ── High Tight Flag (lines 822-963) ──────────────────────────────────────────────
        if I_HTF_ON and t > 30:
            if math.isnan(flag_base_high) or high[t] > flag_base_high:
                flag_base_high = high[t]
                flag_start_index = t
                flag_flag_length = 0
                flag_base_low = low[t]
                flag_low_index = t
            if high[t] <= flag_base_high and low[t] < flag_base_low:
                flag_base_low = low[t]
                flag_low_index = t
            if high[t] <= flag_base_high and flag_low_index == flag_start_index:
                flag_base_low = low[t]
                flag_low_index = t

            find_depth = abs(((flag_base_low / flag_base_high) - 1.0) * 100.0) \
                if (flag_base_high and flag_base_high > 0) else 0.0
            lower_close = close[t] < close[t - 1] if t >= 1 else False

            if (high[t] < flag_base_high and find_depth <= I_HTF_DEPTH) or \
               (high[t] == flag_base_high and lower_close):
                flag_flag_length += 1
            else:
                flag_flag_length = 0

            if (not flag_flag_bool) or high[t] == flag_base_high:
                search_bars = min(I_HTF_PB, t - 1)
                if search_bars >= I_HTF_PBMIN:
                    # Pine walks i = 1..searchBars with a strict `<`, so it keeps the most
                    # RECENT bar holding the minimum low.
                    w = low[t - search_bars:t][::-1]
                    j = int(np.argmin(w))
                    min_low = float(w[j])
                    min_low_idx = t - (j + 1)
                    if min_low > 0 and ((flag_base_high / min_low) - 1.0) * 100.0 >= I_HTF_POLE:
                        flag_flag_bool = True
                        flag_pole_low = min_low
                        flag_pole_low_index = min_low_idx

            if find_depth >= I_HTF_DEPTH or flag_flag_length > I_HTF_FMAX:
                flag_flag_bool = False
                flag_flag_length = 0
                flag_base_high = NAN
                flag_start_index = -1
                flag_low_index = -1
                flag_base_low = NAN

            _fbh_prev = 0.0 if math.isnan(flag_base_high_prev) else flag_base_high_prev
            if high[t] > _fbh_prev and flag_flag_length < I_HTF_FMIN:
                flag_base_high = high[t]
                flag_flag_length = 0
                flag_start_index = t
                flag_low_index = t
                flag_base_low = low[t]

            is_flag = bool(flag_flag_bool and flag_flag_length <= I_HTF_FMAX
                           and find_depth < I_HTF_DEPTH and flag_flag_length >= I_HTF_FMIN
                           and flag_start_index >= 0 and flag_pole_low_index >= 0
                           and (flag_start_index - flag_pole_low_index) <= I_HTF_PB)
            breakout_htf = high[t] > _fbh_prev and flag_flag_length >= I_HTF_FMIN and flag_flag_bool
            breakdown_htf = find_depth >= I_HTF_DEPTH or flag_flag_length > I_HTF_FMAX
            plot_bo = (is_flag_prev and high[t] > _fbh_prev
                       and flag_flag_length_prev >= I_HTF_FMIN and flag_flag_bool)
            vol_ok = volume[t] >= htf_avg_vol[t] * I_HTF_VOL if not math.isnan(htf_avg_vol[t]) else False

            if (plot_bo and vol_ok) or is_flag:
                # Pine latches the flag's geometry here (lines 929-936) BEFORE the breakout
                # and breakdown branches below move flag_startIndex/flag_baseHigh to the
                # current bar. Reading those variables after the loop instead reports a pole
                # that never passed the 85% gate.
                is_htf = True
                htf_snap = {
                    "pole_low": flag_pole_low,
                    "pole_low_bar": flag_pole_low_index,
                    "flag_start_bar": flag_start_index,
                    "flag_high": flag_base_high,
                    "flag_low": flag_base_low,
                    "flag_bars": flag_flag_length,
                }
            else:
                is_htf = False

            if breakout_htf:
                flag_flag_length = 0
                flag_base_high = high[t]
                flag_start_index = t
                flag_low_index = t
                flag_base_low = low[t]
            if breakdown_htf or flag_flag_length == I_HTF_FMAX:
                flag_flag_bool = False
                flag_base_high = highest10[t]
                flag_start_index = t
                flag_low_index = t
                flag_base_low = low[t]
        else:
            is_htf = False
            is_flag = False

        # ── line / box block (lines 966-1259): this is where isBase actually ends ────────
        if base_cond and not is_base_prev:                              # lines 983-985
            high_line_y = start_base_price[0]
            low_line_y = low_base_price[0]

        if lower_base_bar - start_base_bar <= 0:                         # lines 997-1003
            w0 = max(0, t - base_count)
            seg = low[w0:t + 1]
            if len(seg):
                rel = int(np.argmin(seg))
                if seg[rel] < low[t]:            # Pine seeds lowValue with the CURRENT low
                    lower_base_bar = w0 + rel

        if detect_db and not is_db_prev:                                 # lines 1053-1064
            high_line_y = db_sH
            low_line_y = db_sL
            db_line1_x1 = db_fHt
            db_line2_x2 = db_sHt

        if bottom_db_price and is_double_bottom and low[t] < bottom_db_price[0]:   # 1081-1091
            is_double_bottom = False
            high_line_y = start_base_price[0] if start_base_price else NAN
            low_line_y = low_base_price[0] if low_base_price else NAN
            db_line1_x1 = db_line2_x2 = NAN

        if is_double_bottom and (t - top_db_bar) > I_DB_LENGTH:          # lines 1092-1099
            is_double_bottom = False
            high_line_y = NAN            # the Pine deletes both lines and draws no replacement
            low_line_y = NAN
            db_line1_x1 = db_line2_x2 = NAN

        if is_double_bottom and not math.isnan(db_line1_x1) and not math.isnan(db_line2_x2):
            first_piv, thrd_piv = db_line1_x1, db_line2_x2               # lines 1100-1111
            if ((thrd_piv - first_piv) * 2 <= t - thrd_piv) or ((thrd_piv - first_piv) >= (t - thrd_piv) * 2):
                is_double_bottom = False
                high_line_y = start_base_price[0] if start_base_price else NAN
                low_line_y = low_base_price[0] if low_base_price else NAN
                db_line1_x1 = db_line2_x2 = NAN

        if (not math.isnan(low_line_y)) and low[t] < low_line_y and is_base and not is_double_bottom:
            low_line_y = low[t]                                          # lines 1113-1118
            lower_base_bar = t
            if low_base_price:
                low_base_price.insert(0, low[t])

        broke_down = ((not math.isnan(high_line_y)) and low[t] < high_line_y * (1 - I_BASE_DEPTH)) \
            or base_count > I_BASE_LENGTH
        if broke_down and is_base_prev and not is_db_prev:               # lines 1119-1128
            # Recorded before the lines are nulled - after this point the top is unrecoverable.
            _record(t, "Breakdown",
                    "Cup+Handle" if handle_forming else
                    ("Cup" if (detect_cup or cup_bars_in_base > 0) else "Base"),
                    high_line_y, low_line_y, high_line_y)
            high_line_y = NAN
            low_line_y = NAN
            handle_box_live = False
            is_base = False

        if (not math.isnan(high_line_y)) and (not math.isnan(low_line_y)) \
                and high[t] <= high_line_y and low[t] >= low_line_y and is_base:   # 1132-1161
            handle_box_live = bool(handle_forming)
        elif not is_base:
            handle_box_live = False

        is_handle_breakout = handle_breakout and (not is_box) and is_base
        broke_out = ((not math.isnan(high_line_y)) and high[t] > high_line_y
                     and (is_base_prev or is_db_prev)) or is_handle_breakout
        if broke_out:                                                    # lines 1162-1251
            was_cup = detect_cup or cup_bars_in_base > 0     # see CUP_LATCH note
            is_base = False
            handle_box_live = False
            if is_double_bottom:
                is_double_bottom = False
            active_pivot = handle_peak_locked if is_handle_breakout else high_line_y

            base_low_ = (low_base_price[0] if low_base_price else NAN) \
                if math.isnan(low_line_y) else low_line_y
            base_days = t - start_base_bar
            base_depth = ((active_pivot - base_low_) / active_pivot * 100.0) \
                if (active_pivot and not math.isnan(base_low_) and active_pivot > 0) else NAN
            acc, dis, neu = _acc_dis_neutral(close, volume, t, base_days)
            bo_info = {
                "pattern": ("Cup+Handle" if (is_handle_breakout or handle_forming) else
                            "Cup" if was_cup else "Base"),
                "pivot": active_pivot,
                "base_top": high_line_y,
                "base_low": base_low_,
                "base_days": base_days,
                "base_depth": base_depth,
                "base_start_bar": start_base_bar,
                "acc": acc, "dis": dis, "neu": neu,
                "handle_breakout": bool(is_handle_breakout),
                "handle_peak": handle_peak_locked if is_handle_breakout else None,
                # Geometry frozen at the breakout bar. A Post-BO row read from live state
                # instead would stretch the cup's right arc, and re-report whichever double
                # bottom happened to be detected LAST - which may belong to a later base.
                "cup_geo": {
                    "start_up": low_of_base_high * 0.99,
                    "bottom": (low_base_price[0] if low_base_price else NAN) * 0.99,
                    "end_up": low[t - 1] * 0.99 if t >= 1 else NAN,
                    "left_bars": int(max(0, lower_base_bar - start_base_bar)),
                    "right_bars": int(max(0, t - lower_base_bar)),
                } if was_cup else None,
                "base_low_bar": lower_base_bar,
                "handle_geo": {
                    "peak_bar": handle_peak_bar, "peak": handle_peak_locked,
                    "low": handle_low_point, "bars": int(handle_bar_count),
                    "vol_avg": vol_avg_handle, "vol_ma": vol_ma_handle[t],
                } if is_handle_breakout else None,
            }
            _record(t, "Breakout", bo_info["pattern"], high_line_y, base_low_, active_pivot)
            breakout_bar = t
            box_end_bar = t
            is_box = True
            box_over = False

        if is_box and active_pivot and high[t] <= active_pivot * 1.25 \
                and low[t] >= active_pivot * (1 - 0.08):                 # lines 1253-1259
            is_box = True
            box_over = False
            box_end_bar = t

        # ── end of bar: everything [1] and [25] will read next ───────────────────────────
        is_base_hist[t] = is_base
        is_base_prev = is_base
        is_db_prev = is_double_bottom
        is_box_prev, box_over_prev = is_box, box_over
        cond_third_prev, cond_third_two_prev = cond_third, cond_third_two
        right_side_high_close_prev = right_side_high_close
        handle_peak_bar_prev = handle_peak_bar
        handle_peak_locked_prev = handle_peak_locked
        handle_low_point_prev = handle_low_point
        flag_base_high_prev = flag_base_high
        flag_flag_length_prev = flag_flag_length
        is_flag_prev = is_flag

    # ══════════════════════════════════════════════════════════════════════════════════════
    #  What each historical breakout was actually worth
    # ══════════════════════════════════════════════════════════════════════════════════════
    #
    # "Did it ever reach +20%" is the flattering way to ask this and it inflates twice: a
    # breakout that fell 8% first and only recovered months later still counts as a win, and
    # every base still open counts as nothing. So the walk stops at whichever level is touched
    # FIRST, bar by bar, and a bar that spans both - its high above the target, its low below
    # the stop - is scored a stop, because the order inside a daily bar is unknowable and the
    # pessimistic reading is the one that will not flatter the scan. `bars_forward` says how
    # much room the verdict actually had, so a breakout from last week reads as Open with 4
    # bars rather than as a failure.
    HOLD_BARS = 60                            # ~3 months, IBD's usual judgement window
    for ep in history:
        if ep["ended"] != "Breakout" or not ep["pivot"]:
            continue
        piv = ep["pivot"]
        k0, k1 = ep["end_bar"] + 1, min(ep["end_bar"] + HOLD_BARS, n - 1)
        res, bars_to, mx, mn = "Open", None, 0.0, 0.0
        for k in range(k0, k1 + 1):
            # Tested on the same percentages that get REPORTED - rounded exactly as _f() will
            # round them - not on piv*1.20 / piv*0.92. Comparing raw prices leaves a handful of
            # rows whose max_gain reads 20.0 next to an outcome of Open, because the price
            # missed the float threshold by a tick: a contradiction on screen with nothing
            # behind it.
            g = round((high[k] - piv) / piv * 100.0, 4)
            d = round((low[k] - piv) / piv * 100.0, 4)
            mx, mn = max(mx, g), min(mn, d)
            if d <= -8.0:
                res, bars_to = "Stop", k - ep["end_bar"]
                break
            if g >= 20.0:
                res, bars_to = "Target", k - ep["end_bar"]
                break
        ep["outcome"] = res
        ep["bars_to_outcome"] = bars_to
        ep["max_gain_pct"] = _f(mx)
        ep["max_drawdown_pct"] = _f(mn)
        ep["bars_forward"] = int(max(0, k1 - ep["end_bar"]))

    # ══════════════════════════════════════════════════════════════════════════════════════
    #  Report the last bar - i.e. what the indicator is drawing on that chart right now
    # ══════════════════════════════════════════════════════════════════════════════════════
    t = n - 1
    last_close = float(close[t])

    if is_base:
        status = "In Base"
        if handle_forming:
            pattern = "Cup+Handle"
        elif detect_cup or cup_bars_in_base > 0:
            pattern = "Cup"                        # see CUP_LATCH note below
        else:
            pattern = "Base"
        # A base could outlive its own lines while double bottoms were on: the DB expiry branch
        # (lines 1092-1099) deleted highLine and lowLine and drew no replacement, so every
        # later test - the 35% breakdown, the breakout, the base-low update - read
        # `line.get_y1()` as na and the base stopped being invalidatable. With
        # DETECT_DOUBLE_BOTTOM off that branch is unreachable and this should now always be
        # True; it is still reported so the claim is checkable rather than assumed.
        lines_live = not math.isnan(high_line_y)
        base_top = high_line_y if lines_live else (
            start_base_price[0] if start_base_price else NAN)
        base_low = low_line_y if not math.isnan(low_line_y) else (
            low_base_price[0] if low_base_price else NAN)
        pivot = handle_peak_locked if (handle_forming and handle_peak_locked > 0) else base_top
        base_days = t - start_base_bar
        acc, dis, neu = _acc_dis_neutral(close, volume, t, base_days)
        base_depth = ((base_top - base_low) / base_top * 100.0) \
            if (not math.isnan(base_top) and not math.isnan(base_low) and base_top > 0) else NAN
        bars_sbo = None
    elif is_box and bo_info is not None:
        lines_live = True                      # the base ended at a real breakout line
        status = "Post-BO"
        pattern = bo_info["pattern"]
        pivot = bo_info["pivot"]
        base_top = bo_info["base_top"]
        base_low = bo_info["base_low"]
        base_days = bo_info["base_days"]
        base_depth = bo_info["base_depth"]
        acc, dis, neu = bo_info["acc"], bo_info["dis"], bo_info["neu"]
        start_base_bar = bo_info["base_start_bar"]
        bars_sbo = t - breakout_bar
    elif is_flag and htf_snap:
        lines_live = True
        status = "In Flag"
        pattern = "HTF"
        pivot = htf_snap["flag_high"]          # the flag high is the breakout level
        base_top = htf_snap["flag_high"]
        base_low = htf_snap["flag_low"]
        base_days = htf_snap["flag_bars"]
        base_depth = abs(((htf_snap["flag_low"] / htf_snap["flag_high"]) - 1.0) * 100.0) \
            if htf_snap["flag_high"] else NAN
        acc, dis, neu = _acc_dis_neutral(close, volume, t, htf_snap["flag_bars"])
        bars_sbo = None
    else:
        status = None

    # Nothing live today. The ticker still gets a record if it has a past base, so the history
    # view is not silently restricted to the ~2k names that happen to be in a pattern right now
    # - most of the history belongs to tickers the live scan drops.
    _hist_only = {"ticker": str(ticker), "date": _iso(idx, t), "pattern_name": None,
                  "history": history}
    if status is None:
        return _hist_only if history else None

    if pivot is None or (isinstance(pivot, float) and (math.isnan(pivot) or pivot <= 0)):
        return _hist_only if history else None

    dist_pct = (last_close - pivot) / pivot * 100.0

    # 52-week high, and the Pine's RS-line new highs (lines 178-209) against SPY as the
    # comparative symbol - SP:SPX itself is not in ticker_cache.
    win52 = min(252, n)
    high_52w = float(np.nanmax(high[-win52:]))
    pct_off_52w = (high_52w - last_close) / high_52w * 100.0 if high_52w > 0 else NAN

    rs_score = None
    rs_nh = False
    rs_nh_period = None
    rs_leads_price = False
    rs_nh_count = 0
    if spy_close is not None and len(spy_close):
        try:
            spy = spy_close.reindex(idx).ffill().bfill().to_numpy(dtype=float)
            if len(spy) == n and np.all(np.isfinite(spy)) and np.all(spy > 0):
                rs_curve = close / spy
                s_rs = pd.Series(rs_curve)
                h1y = s_rs.shift(1).rolling(250, min_periods=30).max().to_numpy()
                h6m = s_rs.shift(1).rolling(126, min_periods=20).max().to_numpy()
                h3m = s_rs.shift(1).rolling(63, min_periods=10).max().to_numpy()
                nh1y = rs_curve > h1y
                nh6m = (rs_curve > h6m) & ~nh1y
                nh3m = (rs_curve > h3m) & ~nh1y & ~nh6m
                nh_any = nh1y | nh6m | nh3m
                rs_nh = bool(nh_any[t])
                rs_nh_count = int(np.count_nonzero(nh_any[-50:]))
                if rs_nh:
                    rs_nh_period = "1Y" if nh1y[t] else ("6M" if nh6m[t] else "3M")
                    lb = 250 if nh1y[t] else (126 if nh6m[t] else 63)
                    prior_high = float(np.nanmax(high[max(0, t - lb):t])) if t > 0 else NAN
                    rs_leads_price = bool(not (high[t] > prior_high))
                # totalRsScore (lines 281-294): 0.4/0.2/0.2/0.2 over 63/126/189/252 bars.
                def _perf(a, k):
                    return a[t] / a[t - min(k, t)] if a[t - min(k, t)] > 0 else NAN
                stock = (0.4 * _perf(close, 63) + 0.2 * _perf(close, 126)
                         + 0.2 * _perf(close, 189) + 0.2 * _perf(close, 252))
                ref = (0.4 * _perf(spy, 63) + 0.2 * _perf(spy, 126)
                       + 0.2 * _perf(spy, 189) + 0.2 * _perf(spy, 252))
                if ref and np.isfinite(ref) and ref != 0:
                    rs_score = float(stock / ref * 100.0)
        except Exception:
            pass

    # ── base_shape: what the channel the Pine draws actually looks like ──────────────────
    #
    # drw_pattern.pine draws exactly ONE shape for every base that is not a cup: the
    # highLine/lowLine pair from lines 984-985, i.e. a horizontal channel. It never names it.
    # These names describe that same channel so the chart can paint it as what it is; they use
    # the usual IBD cutoffs and change no detection, no pivot and no pattern_name.
    #
    #   Flat Base      <= 15% deep over >= 25 bars - five weeks or more of sideways trade
    #   Consolidation  any other non-cup base
    #
    # A cup keeps its arcs and is left alone.
    if pattern in ("Cup", "Cup+Handle", "HTF"):
        base_shape = None
    elif (not math.isnan(base_depth)) and base_depth <= 15.0 and (base_days or 0) >= 25:
        base_shape = "Flat Base"
    else:
        base_shape = "Consolidation"

    # ── geometry, so the chart can redraw the Pine's own shapes ──────────────────────────
    #
    # Everything below is built from the reading being REPORTED, never from whatever the state
    # machine happens to hold: geometry frozen at a breakout bar is used for Post-BO rows so a
    # stale shape from a later base can never be drawn over an older one.
    post_bo = (status == "Post-BO")
    overlay = {
        "last_date": _iso(idx, t),
        "base_top": _f(base_top),
        "base_low": _f(base_low),
        "pivot": _f(pivot),
        "base_shape": base_shape,
    }
    if pattern == "HTF":
        overlay["base_start_date"] = _iso(idx, htf_snap["flag_start_bar"])
    else:
        overlay["base_start_date"] = _iso(idx, start_base_bar)
        overlay["base_low_date"] = _iso(idx, bo_info["base_low_bar"] if post_bo else lower_base_bar)

    if pattern in ("Cup", "Cup+Handle"):
        # Cup arcs (lines 1017-1048): the left side decays exp(-6i/lengthLeft) from the low of
        # the base-top bar down to the base low, the right side rises
        # (exp(6i/lengthRight)-1)/exp(6) up to the previous bar's low. All three anchors carry
        # the Pine's 1% haircut.
        geo = bo_info["cup_geo"] if post_bo else {
            "start_up": low_of_base_high * 0.99,
            "bottom": (low_base_price[0] if low_base_price else NAN) * 0.99,
            "end_up": low[t - 1] * 0.99 if t >= 1 else NAN,
            "left_bars": int(max(0, lower_base_bar - start_base_bar)),
            "right_bars": int(max(0, t - lower_base_bar)),
        }
        if geo:
            overlay["cup"] = {"start_up": _f(geo["start_up"]), "bottom": _f(geo["bottom"]),
                              "end_up": _f(geo["end_up"]), "left_bars": geo["left_bars"],
                              "right_bars": geo["right_bars"]}

    hg = bo_info["handle_geo"] if (post_bo and bo_info.get("handle_geo")) else (
        {"peak_bar": handle_peak_bar, "peak": handle_peak_locked, "low": handle_low_point,
         "bars": int(handle_bar_count), "vol_avg": vol_avg_handle, "vol_ma": vol_ma_handle[t]}
        if (handle_forming and not post_bo) else None)
    if hg and hg["peak"]:
        overlay["handle"] = {
            "peak_date": _iso(idx, hg["peak_bar"]),
            "peak": _f(hg["peak"]), "low": _f(hg["low"]), "bars": hg["bars"],
            "depth_pct": _f((hg["peak"] - hg["low"]) / hg["peak"] * 100.0),
            "vol_avg": _f(hg["vol_avg"]), "vol_ma": _f(hg["vol_ma"]),
            "box_live": bool(handle_box_live),
        }

    if (is_flag or is_htf) and htf_snap:
        overlay["htf"] = {
            "pole_low": _f(htf_snap["pole_low"]),
            "pole_low_date": _iso(idx, htf_snap["pole_low_bar"]),
            "flag_start_date": _iso(idx, htf_snap["flag_start_bar"]),
            "flag_high": _f(htf_snap["flag_high"]),
            "flag_low": _f(htf_snap["flag_low"]),
            "flag_bars": int(htf_snap["flag_bars"]),
        }
    # 3-weeks-tight squeeze boxes (lines 409-440), limited to the 300 bars the chart draws.
    tight = _tight_closes(idx, open_, high, low, close, since_bar=max(0, t - 300))
    if tight:
        overlay["tight_closes"] = tight

    if is_box and breakout_bar is not None and active_pivot:
        # Trade boxes (lines 1242-1251): entry pivot..+5%, stop -5%..-8%, target +20%..+25%.
        overlay["boxes"] = {
            "start_date": _iso(idx, breakout_bar),
            "end_date": _iso(idx, box_end_bar if box_end_bar is not None else t),
            "entry": [_f(active_pivot), _f(active_pivot * 1.05)],
            "stop": [_f(active_pivot * 0.92), _f(active_pivot * 0.95)],
            "target": [_f(active_pivot * 1.20), _f(active_pivot * 1.25)],
        }

    htf_ctx = None
    if (is_flag or is_htf) and htf_snap:
        _pl, _fh, _fl = htf_snap["pole_low"], htf_snap["flag_high"], htf_snap["flag_low"]
        htf_ctx = {
            "pole_low": _f(_pl),
            "flag_high": _f(_fh),
            "flag_low": _f(_fl),
            "pole_gain_pct": _f(((_fh / _pl) - 1.0) * 100.0) if (_pl and _pl > 0) else None,
            "pole_bars": int(htf_snap["flag_start_bar"] - htf_snap["pole_low_bar"])
            if (htf_snap["flag_start_bar"] >= 0 and htf_snap["pole_low_bar"] >= 0) else None,
            "flag_bars": int(htf_snap["flag_bars"]),
            "flag_depth_pct": _f(abs(((_fl / _fh) - 1.0) * 100.0)) if (_fh and _fh > 0) else None,
        }

    return {
        "ticker": str(ticker),
        "date": _iso(idx, t),
        "close": _f(last_close),
        "pattern_name": pattern,
        "base_shape": base_shape,          # how a non-cup base is drawn; see above
        "status": status,
        "pivot": _f(pivot),
        "dist_pct": _f(dist_pct),
        "base_top": _f(base_top),
        "base_low": _f(base_low),
        "base_depth_pct": _f(base_depth),
        "days_in_base": int(base_days) if base_days is not None else None,
        "base_lines_live": bool(lines_live),
        "base_start_date": _iso(idx, start_base_bar),
        "bars_sbo": int(bars_sbo) if bars_sbo is not None else None,
        "acc_days": int(acc), "dis_days": int(dis), "neu_days": int(neu),
        "acc_dis_ratio": _f(acc / dis) if dis else None,
        "pct_off_52w_high": _f(pct_off_52w),
        "handle_bars": int(handle_bar_count) if handle_forming else None,
        "handle_depth_pct": _f((handle_peak_locked - handle_low_point) / handle_peak_locked * 100.0)
        if (handle_forming and handle_peak_locked) else None,
        "handle_vol_contraction_pct": _f(vol_avg_handle / vol_ma_handle[t] * 100.0)
        if (handle_forming and vol_ma_handle[t] and not math.isnan(vol_avg_handle)) else None,
        "handle_breakout": bool(handle_breakout),
        # Which of the Pine's six handle gates the ticker passes. A handle is rare - the
        # volume gate alone asks the handle to trade at half the 50-day average - so when a
        # base that looks like a cup-with-handle is reported as a plain Cup, this says why.
        "handle_gates": {
            "right_side": bool(right_side),
            "peak_locked": _f(handle_peak_locked) if handle_peak_locked else None,
            "bars": int(handle_bar_count),
            "bars_ok": bool(I_HANDLE_MIN_BARS <= handle_bar_count <= I_HANDLE_MAX_BARS),
            "low": _f(handle_low_point) if handle_low_point else None,
            "base_mid": _f((base_top_now + _bl) * 0.5)
            if (not math.isnan(base_top_now) and not math.isnan(_bl)) else None,
            "depth_ok": bool(handle_depth_ok and handle_peak_locked > 0),
            "mid_ok": bool(handle_mid_ok),
            "vol_ok": bool(vol_contraction_ok),
            "vol_pct_of_ma": _f(vol_avg_handle / vol_ma_handle[t] * 100.0)
            if (vol_ma_handle[t] and not math.isnan(vol_avg_handle)) else None,
            "cup_ok": bool(require_cup_ok),
        } if status == "In Base" else None,
        "cup_bars_in_base": int(cup_bars_in_base),
        "cup_last_bars_ago": int(t - cup_last_bar) if cup_last_bar is not None else None,
        "is_cup_latched": bool(is_cup_sticky),
        "in_flag": bool(is_flag),
        "rs_score": _f(rs_score),
        "rs_nh": bool(rs_nh),
        "rs_nh_period": rs_nh_period,
        "rs_leads_price": bool(rs_leads_price),
        "rs_nh_count": int(rs_nh_count),
        "htf_context": htf_ctx,
        "overlay": overlay,
        # Split out into tv_pattern_history.json by run_scan, so the live file stays small.
        "history": history,
        # Per-bar cup diagnostics for the last `debug` bars. Off by default (the scan never
        # carries it); `--debug TICKER` prints it, which is how the "why is this a Base and
        # not a Cup" question gets answered without re-deriving the state machine by hand.
        **({"_debug": debug_rows} if debug else {}),
    }


def _worker(args):
    """One parquet read serves both the pattern replay and the trend metrics."""
    ticker, path, spy = args
    try:
        raw = pd.read_parquet(path)
    except Exception:
        return None, None
    raw = raw.sort_index()
    try:
        trend = _trend_row(ticker, raw)
    except Exception:
        trend = None
    try:
        return scan_ticker(ticker, path, spy, df=raw), trend
    except Exception:
        return None, trend


def _write_json(path: Path, payload: dict, indent=2):
    """Atomic write - a half-written results file is what the app cannot recover from."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent, default=_json_safe)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def run_scan(max_workers: int = None, limit: int = None):
    """Scan every ticker_cache parquet and write python/tv_pattern_results.json."""
    if max_workers is None:
        max_workers = os.cpu_count() or 8
    start = time.time()
    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    print(f"🔍 {len(files)} ticker cache files in {TICKER_CACHE_DIR}", flush=True)

    spy_close = None
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    if spy_path.exists():
        try:
            spy_df = pd.read_parquet(spy_path).sort_index()
            spy_close = spy_df["Close"]
            spy_close = spy_close[~spy_close.index.duplicated(keep="last")]
        except Exception:
            spy_close = None

    jobs = []
    for f in files:
        ticker = Path(f).name.split("_1d.parquet")[0]
        if ticker in SKIP_TICKERS:
            continue
        jobs.append((ticker, f, spy_close))
    if limit:
        jobs = jobs[:limit]

    results, history, trend = [], [], []
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_worker, j) for j in jobs]
        for fut in as_completed(futures):
            r, tr = fut.result()
            if tr is not None:
                trend.append(tr)
            if r is None:
                continue
            # Every ticker with a past base comes back, whether or not it is in one today.
            for ep in r.pop("history", None) or []:
                ep["ticker"] = r["ticker"]
                history.append(ep)
            if r.get("pattern_name") is not None:
                results.append(r)

    order = {"Cup+Handle": 0, "Cup": 1, "HTF": 2, "Base": 3}
    results.sort(key=lambda r: (order.get(r["pattern_name"], 9),
                                abs(r["dist_pct"]) if r["dist_pct"] is not None else 999.0,
                                r["ticker"]))
    history.sort(key=lambda e: (e["end_date"] or "", e["ticker"]), reverse=True)

    stamp = pd.Timestamp.now().isoformat(timespec="seconds")
    meta = {"generated_at": stamp, "source": "pine/drw_pattern.pine",
            "pine_quirks": PINE_QUIRKS, "universe": len(jobs)}
    _write_json(OUTPUT_JSON_PATH, {**meta, "results": results})
    # History is written separately and without indentation: it is ~20x the row count of the
    # live file and the app only loads it when the History tab is opened.
    _write_json(HISTORY_JSON_PATH, {**meta, "hold_bars": 60, "patterns": history}, indent=None)
    # One small table instead of app.py reopening a parquet per ticker on every cold load.
    if trend:
        tmp = TREND_PARQUET_PATH.with_suffix(".parquet.tmp")
        pd.DataFrame(trend).to_parquet(tmp, index=False)
        os.replace(tmp, TREND_PARQUET_PATH)

    elapsed = time.time() - start
    by_pat = {}
    for r in results:
        by_pat[r["pattern_name"]] = by_pat.get(r["pattern_name"], 0) + 1
    out = {}
    for e in history:
        out[e.get("outcome") or e["ended"]] = out.get(e.get("outcome") or e["ended"], 0) + 1
    print(f"✅ {len(results)} live patterns from {len(jobs)} tickers in {elapsed:.1f}s", flush=True)
    print("   " + ", ".join(f"{k}: {v}" for k, v in sorted(by_pat.items())), flush=True)
    print(f"📜 {len(history)} historical patterns — "
          + ", ".join(f"{k}: {v}" for k, v in sorted(out.items())), flush=True)
    print(f"💾 {OUTPUT_JSON_PATH}\n💾 {HISTORY_JSON_PATH}"
          + (f"\n💾 {TREND_PARQUET_PATH} ({len(trend)} tickers)" if trend else ""), flush=True)
    return results


if __name__ == "__main__":
    _limit = None
    _workers = None
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            _limit = int(a.split("=", 1)[1])
        elif a.startswith("--workers="):
            _workers = int(a.split("=", 1)[1])
    run_scan(max_workers=_workers, limit=_limit)
