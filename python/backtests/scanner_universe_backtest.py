#!/usr/bin/env python3
"""
scanner_universe_backtest.py
============================
Full-universe backtest driven by the REAL python/ibd_pattern_scanner.py.

Unlike full_backtest.py (which re-implements a lightweight scanner), this script loads
the production scanner source, patches ONLY its final "latest bar must be in a pattern"
guard so the per-bar history is always returned, and then walks that history to extract
every base + breakout event. Buy signals come straight from the scanner's own per-bar
flags (volDryUp, ppAny, touchedMA, shakeoutEntry, upsideReversal, rsNH) plus pivot
breakout / composite-score rules. Every buy strategy is combined with every exit rule
(stop-loss, ATR trails, time stops, R:R targets) and every pattern group.

Usage:
    python3 python/backtests/scanner_universe_backtest.py
    python3 python/backtests/scanner_universe_backtest.py --max-tickers 300 --workers 12
    python3 python/backtests/scanner_universe_backtest.py --min-price 20 --min-vol 1e6

Outputs (in python/backtests/):
    scanner_universe_trades.csv     every simulated trade
    scanner_universe_summary.csv    buy-strategy x exit-rule aggregates
    scanner_universe_report.html    browsable HTML report
"""
import argparse
import glob
import json
import time
import warnings
from itertools import combinations
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from trend_following_backtest import parabolic_sar, ema

warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = Path(__file__).resolve().parent
SCANNER_PATH = ROOT_DIR / "python" / "ibd_pattern_scanner.py"

# ── Default filters (match full_backtest.py convention) ──
DEFAULT_MIN_PRICE = 12.0
DEFAULT_MIN_VOL_50 = 500_000
MIN_BARS = 100

# ── Strategy / exit lists ──
BUY_STRATEGIES = [
    "Pivot Breakout", "Upside Reversal", "Shakeout", "Volume Dry-Up",
    "MA Touch", "Pocket Pivot", "RS New High", "SMA50 Bounce",
]
# First 9 are fixed-horizon (forced closed within 60 bars of entry). Everything after is
# uncapped so a genuine breakout can ride a trend instead of being force-closed at bar 60
# — see apply_exit_rules(). Next 5 are trend-following-style exits ported from
# trend_following_backtest.py's stop/trail library. Last 9 are IBD sell-discipline rules
# (hard %-stop alone, combined %-stop + %-profit-take, RS-line deterioration from
# pine/drw_relative_strength_all.pine, batch-sell-in-thirds at key moving averages with
# add-back).
EXIT_RULES = ["stop_loss", "trail_2atr", "trail_3atr", "time_20", "time_40",
              "time_60", "target_2r", "target_3r", "target_5r",
              "chandelier_uncapped", "parabolic_sar", "breakeven_trail",
              "tighten_after_30", "scale_out_369",
              "stop_6pct", "stop_8pct",
              "ibd_stop6_tp15", "ibd_stop6_tp20", "ibd_stop8_tp15", "ibd_stop8_tp20",
              "rs_quicksand_exit", "rs_breakdown_exit", "scale_ma_1_3"]

# Pattern groups used for slicing the summary
PATTERN_GROUPS = {
    "Cup+Handle": {"Cup+Handle"},
    "Cup": {"Cup"},
    "Flat Base": {"Flat Base", "6-Wk Flat"},
    "Double Bottom": {"Dbl Bottom"},
    "Consolidation": {"Consolidation"},
    "Deep Base": {"Base", "Deep Base"},
    "VCP-ready": {"Cup+Handle", "Cup", "Flat Base", "Consolidation", "6-Wk Flat"},
}


def load_patched_scanner():
    """Load the production scanner with its final guard patched so history is always returned."""
    src = SCANNER_PATH.read_text()
    guard = "if latest['pOn'] and (latest['pCode'] > 0):"
    assert guard in src, "scanner guard anchor not found"
    patched = src.replace(guard, "if True:  # patched by scanner_universe_backtest: always return history", 1)
    ns = {"__file__": str(SCANNER_PATH)}
    exec(compile(patched, "ibd_pattern_scanner_patched", "exec"), ns)
    return ns["scan_single_ticker"], ns["calculate_atr"]


def calculate_atr(highs, lows, closes, length=14):
    """Vectorised Wilder ATR (same contract as the scanner's helper)."""
    n = len(closes)
    if n == 0:
        return np.zeros(0)
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    alpha = 1.0 / length
    atr = np.zeros(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    return atr


def sma50_series(closes):
    return pd.Series(closes).rolling(50, min_periods=10).mean().values


def find_pivots(highs, lows, left=5, right=5):
    n = len(highs)
    ph, pl = {}, {}
    for i in range(left, n - right):
        if all(highs[j] < highs[i] for j in range(i - left, i + right + 1) if j != i):
            ph[i] = highs[i]
        if all(lows[j] > lows[i] for j in range(i - left, i + right + 1) if j != i):
            pl[i] = lows[i]
    return ph, pl


# ══════════════════════════════════════════════════════════════════════════════
# Trade simulation
# ══════════════════════════════════════════════════════════════════════════════

def apply_exit_rules(highs, lows, closes, signal_bar, entry_price, base_low, atr, sar=None,
                      rs_line=None, rs_ema21=None, rs_ema34=None, rs_ema50=None,
                      price_ema21=None, price_sma50=None, price_sma200=None):
    """Apply every exit rule from a signal bar; return exit results keyed by rule.

    sar: optional precomputed Wilder parabolic-SAR array (see parabolic_sar()), used by
    the 'parabolic_sar' exit. Computed once per ticker by the caller, not per trade.

    rs_line/rs_ema21/rs_ema34/rs_ema50: RS line (close/SPY close) and its Quick/QuickSand/
    Grateful Dead EMAs (pine/drw_relative_strength_all.pine), used by the 'rs_quicksand_exit'
    and 'rs_breakdown_exit' rules.

    price_ema21/price_sma50/price_sma200: price moving averages used by 'scale_ma_1_3'.
    """
    n = len(closes)
    results = {}

    # Stop-loss at base low
    for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
        if lows[bar] <= base_low:
            ret = (base_low - entry_price) / entry_price * 100.0
            results["stop_loss"] = {"exit_bar": bar, "exit_price": base_low, "ret": ret}
            break
    else:
        ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
        results["stop_loss"] = {"exit_bar": min(signal_bar + 60, n - 1),
                                "exit_price": closes[min(signal_bar + 60, n - 1)], "ret": ret}

    # Trailing stops (2x / 3x ATR)
    for mult, key in ((2, "trail_2atr"), (3, "trail_3atr")):
        highest_since = entry_price
        for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
            highest_since = max(highest_since, highs[bar])
            trail = highest_since - mult * atr[bar] if bar < len(atr) else highest_since * 0.92
            if lows[bar] <= trail:
                ret = (trail - entry_price) / entry_price * 100.0
                results[key] = {"exit_bar": bar, "exit_price": trail, "ret": ret}
                break
        else:
            ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
            results[key] = {"exit_bar": min(signal_bar + 60, n - 1),
                            "exit_price": closes[min(signal_bar + 60, n - 1)], "ret": ret}

    # Time stops
    for t_bars, key in ((20, "time_20"), (40, "time_40"), (60, "time_60")):
        exit_bar = min(signal_bar + t_bars, n - 1)
        ret = (closes[exit_bar] - entry_price) / entry_price * 100.0
        results[key] = {"exit_bar": exit_bar, "exit_price": closes[exit_bar], "ret": ret}

    # Profit targets (R:R vs risk = entry - base_low)
    risk = entry_price - base_low
    if risk > 0:
        for rr, key in ((2, "target_2r"), (3, "target_3r"), (5, "target_5r")):
            target_price = entry_price + risk * rr
            for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
                if highs[bar] >= target_price:
                    ret = (target_price - entry_price) / entry_price * 100.0
                    results[key] = {"exit_bar": bar, "exit_price": target_price, "ret": ret}
                    break
            else:
                ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
                results[key] = {"exit_bar": min(signal_bar + 60, n - 1),
                                "exit_price": closes[min(signal_bar + 60, n - 1)], "ret": ret}

    # ── Trend-following-style exits, ported from trend_following_backtest.py's stop
    # library, run with NO time cap so a genuine breakout can ride a trend past bar 60. ──
    last_bar = n - 1

    # Chandelier trail: highest high since entry minus 3x ATR (same mechanic as
    # trail_3atr above, just not force-closed at bar 60).
    highest_since = entry_price
    exit_bar, exit_price = last_bar, closes[last_bar]
    for bar in range(signal_bar + 1, n):
        highest_since = max(highest_since, highs[bar])
        trail = highest_since - 3 * atr[bar] if bar < len(atr) else highest_since * 0.88
        if lows[bar] <= trail:
            exit_bar, exit_price = bar, trail
            break
    ret = (exit_price - entry_price) / entry_price * 100.0
    results["chandelier_uncapped"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    # Parabolic SAR trailing stop.
    if sar is not None:
        exit_bar, exit_price = last_bar, closes[last_bar]
        for bar in range(signal_bar + 1, n):
            stop_px = sar[bar] if bar < len(sar) else -np.inf
            if lows[bar] <= stop_px:
                exit_bar, exit_price = bar, stop_px
                break
        ret = (exit_price - entry_price) / entry_price * 100.0
        results["parabolic_sar"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    # Breakeven lock at +2x ATR, then chandelier trail (stop never given back below entry
    # once locked; held at base_low before that).
    highest_since = entry_price
    be_locked = False
    exit_bar, exit_price = last_bar, closes[last_bar]
    for bar in range(signal_bar + 1, n):
        highest_since = max(highest_since, highs[bar])
        a = atr[bar] if bar < len(atr) else 0.0
        trail = highest_since - 3 * a
        if not be_locked and closes[bar] >= entry_price + 2 * a:
            be_locked = True
        stop_px = max(trail, entry_price) if be_locked else max(trail, base_low)
        if lows[bar] <= stop_px:
            exit_bar, exit_price = bar, stop_px
            break
    ret = (exit_price - entry_price) / entry_price * 100.0
    results["breakeven_trail"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    # Base-low stop for the first 30 bars, then tighten to a 1.5x ATR trail.
    highest_since = entry_price
    exit_bar, exit_price = last_bar, closes[last_bar]
    for bar in range(signal_bar + 1, n):
        highest_since = max(highest_since, highs[bar])
        a = atr[bar] if bar < len(atr) else 0.0
        if bar - signal_bar < 30:
            stop_px = base_low
        else:
            stop_px = max(base_low, highest_since - 1.5 * a)
        if lows[bar] <= stop_px:
            exit_bar, exit_price = bar, stop_px
            break
    ret = (exit_price - entry_price) / entry_price * 100.0
    results["tighten_after_30"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    # Scale out 25% at each of 3R/6R/9R (R = entry - base_low); remainder trails via a
    # 3x ATR chandelier. Reported return is the size-weighted blend across all fills.
    if risk > 0:
        target_prices = [entry_price + risk * m for m in (3, 6, 9)]
        targets_hit = [False, False, False]
        remaining = 1.0
        realized = 0.0
        highest_since = entry_price
        exit_bar, exit_price = last_bar, closes[last_bar]
        for bar in range(signal_bar + 1, n):
            highest_since = max(highest_since, highs[bar])
            trail = highest_since - 3 * atr[bar] if bar < len(atr) else highest_since * 0.88
            for k, tp in enumerate(target_prices):
                if not targets_hit[k] and highs[bar] >= tp:
                    targets_hit[k] = True
                    realized += 0.25 * (tp - entry_price) / entry_price * 100.0
                    remaining -= 0.25
            if remaining > 1e-9 and lows[bar] <= trail:
                realized += remaining * (trail - entry_price) / entry_price * 100.0
                exit_bar, exit_price = bar, trail
                remaining = 0.0
                break
        if remaining > 1e-9:
            realized += remaining * (closes[last_bar] - entry_price) / entry_price * 100.0
        results["scale_out_369"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": realized}

    # ── IBD hard stop-loss: sell no matter what if the stock falls 6% / 8% below entry ──
    for pct, key in ((0.06, "stop_6pct"), (0.08, "stop_8pct")):
        stop_px = entry_price * (1 - pct)
        exit_bar, exit_price = last_bar, closes[last_bar]
        for bar in range(signal_bar + 1, n):
            if lows[bar] <= stop_px:
                exit_bar, exit_price = bar, stop_px
                break
        ret = (exit_price - entry_price) / entry_price * 100.0
        results[key] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    # ── IBD classic sell discipline: cut losses at 6%/8%, take profit at 15%/20% —
    # whichever hits first. A profit target with no attached stop and no time cap would
    # just be "wait indefinitely for +N%", which inflates win rate in a backtest (nothing
    # ever gets marked a loser except tickers that ran out of data) without being a real
    # tradeable rule — pairing it with the stop is both what IBD's rule actually is and
    # what avoids that artifact. On a bar where both thresholds are crossed, the stop wins
    # (conservative — we don't know the intrabar sequence).
    for stop_pct, tp_pct, key in ((0.06, 0.15, "ibd_stop6_tp15"), (0.06, 0.20, "ibd_stop6_tp20"),
                                   (0.08, 0.15, "ibd_stop8_tp15"), (0.08, 0.20, "ibd_stop8_tp20")):
        stop_px = entry_price * (1 - stop_pct)
        target_px = entry_price * (1 + tp_pct)
        exit_bar, exit_price = last_bar, closes[last_bar]
        for bar in range(signal_bar + 1, n):
            if lows[bar] <= stop_px:
                exit_bar, exit_price = bar, stop_px
                break
            if highs[bar] >= target_px:
                exit_bar, exit_price = bar, target_px
                break
        ret = (exit_price - entry_price) / entry_price * 100.0
        results[key] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    # ── RS line deterioration (pine/drw_relative_strength_all.pine Quick/QuickSand/
    # Grateful Dead EMAs of the RS line) ──
    if rs_line is not None and rs_ema34 is not None:
        exit_bar, exit_price = last_bar, closes[last_bar]
        for bar in range(max(signal_bar + 1, 1), n):
            if rs_line[bar - 1] >= rs_ema34[bar - 1] and rs_line[bar] < rs_ema34[bar]:
                exit_bar, exit_price = bar, closes[bar]
                break
        ret = (exit_price - entry_price) / entry_price * 100.0
        results["rs_quicksand_exit"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    if rs_line is not None and rs_ema21 is not None and rs_ema34 is not None and rs_ema50 is not None:
        exit_bar, exit_price = last_bar, closes[last_bar]
        for bar in range(signal_bar + 1, n):
            if rs_line[bar] < rs_ema21[bar] and rs_line[bar] < rs_ema34[bar] and rs_line[bar] < rs_ema50[bar]:
                exit_bar, exit_price = bar, closes[bar]
                break
        ret = (exit_price - entry_price) / entry_price * 100.0
        results["rs_breakdown_exit"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": ret}

    # ── Batch sell in thirds as price breaks its own EMA21 / SMA50 / SMA200 (tightest to
    # loosest), with one add-back per level if the average is reclaimed before the position
    # is fully closed. Blended return weighted by 1/3 per lot, normalized against the
    # original entry_price (same convention as scale_out_369). ──
    if price_ema21 is not None and price_sma50 is not None and price_sma200 is not None:
        levels = [price_ema21, price_sma50, price_sma200]
        lot_active = [True, True, True]
        lot_sold_once = [False, False, False]
        lot_bought_back = [False, False, False]
        lot_basis = [entry_price, entry_price, entry_price]
        lot_frac = 1.0 / 3.0
        realized = 0.0
        exit_bar, exit_price = last_bar, closes[last_bar]
        fully_closed = False
        for bar in range(signal_bar + 1, n):
            for lvl in range(3):
                ma = levels[lvl]
                if bar >= len(ma) or np.isnan(ma[bar]):
                    continue
                if lot_active[lvl] and closes[bar] < ma[bar]:
                    realized += lot_frac * (closes[bar] - lot_basis[lvl]) / entry_price * 100.0
                    lot_active[lvl] = False
                    lot_sold_once[lvl] = True
                elif (not lot_active[lvl] and lot_sold_once[lvl] and not lot_bought_back[lvl]
                      and closes[bar] > ma[bar]):
                    lot_active[lvl] = True
                    lot_basis[lvl] = closes[bar]
                    lot_bought_back[lvl] = True
            if not any(lot_active):
                exit_bar, exit_price = bar, closes[bar]
                fully_closed = True
                break
        if not fully_closed:
            for lvl in range(3):
                if lot_active[lvl]:
                    realized += lot_frac * (closes[last_bar] - lot_basis[lvl]) / entry_price * 100.0
        results["scale_ma_1_3"] = {"exit_bar": exit_bar, "exit_price": exit_price, "ret": realized}

    return results


def calc_base_quality(bDepPct, bCount):
    score = 50.0
    if bDepPct and 15 <= bDepPct <= 35:
        score += 15
    elif bDepPct and 10 <= bDepPct <= 45:
        score += 8
    if 20 <= bCount <= 150:
        score += 15
    elif 15 <= bCount <= 200:
        score += 8
    if bDepPct and 18.1 <= bDepPct <= 50.0:
        score += 10
    return min(100, max(0, score))


def pos_size(quality):
    if quality >= 80:
        return 1.0
    if quality >= 60:
        return 0.75
    if quality >= 40:
        return 0.50
    if quality >= 20:
        return 0.35
    return 0.25


# ══════════════════════════════════════════════════════════════════════════════
# Per-ticker processing
# ══════════════════════════════════════════════════════════════════════════════

def process_ticker(args):
    """Scan one ticker with the real scanner and simulate every strategy x exit combo."""
    ticker, fpath = args
    try:
        df = pd.read_parquet(fpath)
        if df.empty or len(df) < MIN_BARS:
            return []
        if df["Close"].iloc[-1] < _MIN_PRICE or df["Volume"].tail(50).mean() < _MIN_VOL:
            return []

        res = _scan(ticker, str(fpath))
        if not res or not res.get("history"):
            return []

        hist = res["history"]
        n = len(hist)
        if n < 60:
            return []

        highs = df["High"].values[-n:]
        lows = df["Low"].values[-n:]
        closes = df["Close"].values[-n:]
        opens = df["Open"].values[-n:]
        volumes = df["Volume"].values[-n:]
        atr14 = calculate_atr(highs, lows, closes, 14)
        sar = parabolic_sar(highs, lows)
        sma50 = sma50_series(closes)
        sma200 = pd.Series(closes).rolling(200, min_periods=50).mean().values
        ema10 = pd.Series(closes).ewm(span=10, adjust=False).mean().values
        ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean().values
        ema21_px = ema(closes, 21)

        # RS line (close / SPY close) and its Quick(21)/QuickSand(34)/Grateful Dead(50)
        # EMAs — same lengths as pine/drw_relative_strength_all.pine's daily-timeframe
        # quickLen/quickSandLen/gdLen (lines 156-158).
        rs_line = rs_ema21 = rs_ema34 = rs_ema50 = None
        if _SPY_CLOSE is not None:
            spy_aligned = _SPY_CLOSE.reindex(df.index[-n:]).ffill().bfill().values
            if not np.any(np.isnan(spy_aligned)) and np.all(spy_aligned > 0):
                rs_line = closes / spy_aligned
                rs_ema21 = ema(rs_line, 21)
                rs_ema34 = ema(rs_line, 34)
                rs_ema50 = ema(rs_line, 50)

        # ── Segment history into base runs ──
        bases = []  # dicts with start/end/pattern/pivot/bLow/boBar etc.
        i = 0
        while i < n:
            st = hist[i]
            if st.get("pOn") and st.get("pCode", 0) > 0:
                run_start = i
                # Find breakout bar inside the run (first bar where boBar == bar)
                bo_bar = None
                for j in range(i, n):
                    sj = hist[j]
                    if not (sj.get("pOn") and sj.get("pCode", 0) > 0) and j > i:
                        break
                    if sj.get("boBar") is not None and sj.get("boBar") == j:
                        bo_bar = j
                        break
                run_end = i
                for j in range(i, n):
                    if not (hist[j].get("pOn") and hist[j].get("pCode", 0) > 0) and j > i:
                        break
                    run_end = j
                if run_end >= i:
                    pivot = None
                    bLow = None
                    bTop = None
                    bDepPct = None
                    bCount = None
                    pat_name = None
                    for j in range(run_start, run_end + 1):
                        sj = hist[j]
                        if sj.get("pName"):
                            pat_name = sj.get("pName")
                        if sj.get("boPivot") is not None:
                            pivot = sj.get("boPivot")
                        if sj.get("bLow") is not None:
                            bLow = sj.get("bLow")
                        if sj.get("bTop") is not None:
                            bTop = sj.get("bTop")
                        if sj.get("bDepPct") is not None:
                            bDepPct = sj.get("bDepPct")
                        if sj.get("bCount") is not None:
                            bCount = sj.get("bCount")
                    if pivot is None:
                        pivot = bTop
                    bases.append({
                        "start": run_start, "end": run_end,
                        "bo_bar": bo_bar, "pivot": pivot, "bLow": bLow,
                        "bTop": bTop, "bDepPct": bDepPct, "bCount": bCount,
                        "pattern": pat_name or "Base",
                    })
                    i = run_end + 1
                    continue
            i += 1

        if not bases:
            return []

        trades = []
        rs_raw = closes.copy()  # fallback when SPY absent (scanner already computed internally)

        for base in bases:
            pivot = base["pivot"]
            bLow = base["bLow"]
            if pivot is None or pivot <= 0 or bLow is None or bLow <= 0:
                continue
            search_start = max(0, base["start"])
            search_end = min(len(closes) - 1, (base["bo_bar"] if base["bo_bar"] is not None else base["end"]) + 5)

            bq = calc_base_quality(base["bDepPct"], base["bCount"])
            ps = pos_size(bq)

            # Build signal book from scanner's own flags
            signals = {}

            # 1. Pivot Breakout — the bar the scanner marked as breakout
            if base["bo_bar"] is not None and base["bo_bar"] <= len(closes) - 1:
                bo = base["bo_bar"]
                entry = max(pivot, closes[bo]) if closes[bo] > pivot else pivot
                signals["Pivot Breakout"] = (bo, entry)

            for j in range(search_start, search_end + 1):
                st = hist[j]
                c = closes[j]
                if c < bLow or c > pivot * 1.05:
                    pass  # still allow near-pivot checks below
                # 2. Upside Reversal
                if st.get("upsideReversal") and bLow <= c <= pivot * 1.01 and "Upside Reversal" not in signals:
                    signals["Upside Reversal"] = (j, c)
                # 3. Shakeout
                if st.get("shakeoutEntry") and pivot * 0.85 <= c <= pivot and "Shakeout" not in signals:
                    signals["Shakeout"] = (j, c)
                # 4. Volume Dry-Up
                if st.get("volDryUp") and pivot * 0.95 <= c <= pivot * 1.01 and "Volume Dry-Up" not in signals:
                    signals["Volume Dry-Up"] = (j, c)
                # 5. MA Touch
                if st.get("touchedMA") and c >= bLow and c <= pivot and "MA Touch" not in signals:
                    signals["MA Touch"] = (j, c)
                # 6. Pocket Pivot
                if st.get("ppAny") and pivot * 0.90 <= c <= pivot * 1.01 and "Pocket Pivot" not in signals:
                    signals["Pocket Pivot"] = (j, c)
                # 7. RS New High
                if st.get("rsNH") and pivot * 0.85 <= c <= pivot * 1.01 and "RS New High" not in signals:
                    signals["RS New High"] = (j, c)
                # 8. SMA50 Bounce (dip to SMA50 then reclaim)
                if j >= 2 and not np.isnan(sma50[j]) and sma50[j] > 0:
                    prev_tested = (lows[j - 1] <= sma50[j - 1] * 1.02
                                   if not np.isnan(sma50[j - 1]) and sma50[j - 1] > 0 else False)
                    if (prev_tested and c >= opens[j]
                            and bLow <= c <= pivot and "SMA50 Bounce" not in signals):
                        signals["SMA50 Bounce"] = (j, c)

            if not signals:
                continue

            # Composite Score proxy: 30% quality + 10/signal
            real_sigs = {k: v for k, v in signals.items()}
            composite = min(100, bq * 0.3 + len(real_sigs) * 10)
            if composite >= 30 and real_sigs:
                best = max(real_sigs, key=lambda s: (15 if s in ("Pivot Breakout", "Pocket Pivot", "Shakeout", "RS New High")
                                                     else 10 if s in ("Upside Reversal", "SMA50 Bounce")
                                                     else 8 if s == "Volume Dry-Up" else 5))
                signals["Composite Score"] = real_sigs[best]

            for strategy, (sig_bar, entry_price) in signals.items():
                exit_results = apply_exit_rules(highs, lows, closes, sig_bar, entry_price, bLow, atr14, sar,
                                                 rs_line, rs_ema21, rs_ema34, rs_ema50,
                                                 ema21_px, sma50, sma200)
                for exit_rule, ex in exit_results.items():
                    ret_raw = ex["ret"]
                    ret = ret_raw * ps
                    risk = entry_price - bLow
                    rr = abs(ret_raw / (risk / entry_price * 100)) if risk > 0 else 0
                    trades.append({
                        "ticker": ticker, "pattern": base["pattern"],
                        "depth": base["bDepPct"], "length": base["bCount"],
                        "pivot_price": pivot, "base_low": bLow,
                        "strategy": strategy, "exit_rule": exit_rule,
                        "entry_bar": sig_bar, "entry_price": entry_price,
                        "exit_bar": ex["exit_bar"], "exit_price": ex["exit_price"],
                        "ret": ret, "ret_raw": ret_raw,
                        "base_quality": bq, "pos_size": ps,
                        "risk_amount": risk, "rr_ratio": rr, "win": ret > 0,
                        "composite_score": composite,
                    })

            # Pair / triple signal combos (earliest signal of the combo)
            real_sig_names = sorted(k for k in signals if k not in ("Any Signal", "Composite Score"))
            for combo in list(combinations(real_sig_names, 2)) + list(combinations(real_sig_names, 3)):
                bars = [signals[s][0] for s in combo]
                prices = [signals[s][1] for s in combo]
                ei = bars.index(min(bars))
                combo_name = "+".join(combo)
                exit_results = apply_exit_rules(highs, lows, closes, bars[ei], prices[ei], bLow, atr14, sar,
                                                 rs_line, rs_ema21, rs_ema34, rs_ema50,
                                                 ema21_px, sma50, sma200)
                for exit_rule, ex in exit_results.items():
                    ret_raw = ex["ret"]
                    ret = ret_raw * ps
                    risk = prices[ei] - bLow
                    rr = abs(ret_raw / (risk / prices[ei] * 100)) if risk > 0 else 0
                    trades.append({
                        "ticker": ticker, "pattern": base["pattern"],
                        "depth": base["bDepPct"], "length": base["bCount"],
                        "pivot_price": pivot, "base_low": bLow,
                        "strategy": combo_name, "exit_rule": exit_rule,
                        "entry_bar": bars[ei], "entry_price": prices[ei],
                        "exit_bar": ex["exit_bar"], "exit_price": ex["exit_price"],
                        "ret": ret, "ret_raw": ret_raw,
                        "base_quality": bq, "pos_size": ps,
                        "risk_amount": risk, "rr_ratio": rr, "win": ret > 0,
                        "composite_score": composite,
                    })
        return trades
    except Exception:
        return []


# Module-level bindings for worker processes
_scan = None
_MIN_PRICE = DEFAULT_MIN_PRICE
_MIN_VOL = DEFAULT_MIN_VOL_50
_SPY_CLOSE = None  # Series, Date-indexed; used to build the RS line (close / SPY close)


def _init_worker(min_price, min_vol, spy_close):
    """Each worker loads its own copy of the patched scanner. scan_single_ticker is
    created via exec() at runtime, so it has no importable __module__/__qualname__ and
    cannot be pickled through ProcessPoolExecutor's initargs (fails under the 'spawn'
    start method, e.g. on macOS) — loading it locally in the worker avoids that."""
    global _scan, _MIN_PRICE, _MIN_VOL, _SPY_CLOSE
    _scan, _ = load_patched_scanner()
    _MIN_PRICE = min_price
    _MIN_VOL = min_vol
    _SPY_CLOSE = spy_close


def run_universe_backtest(args):
    global _scan, _MIN_PRICE, _MIN_VOL
    _MIN_PRICE = args.min_price
    _MIN_VOL = args.min_vol

    t0 = time.time()

    # SPY close series for the RS line (close / SPY close), same source/alignment pattern
    # as full_backtest.py's scan_ticker_for_bases(df, spy_close_series).
    spy_close = None
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    if spy_path.exists():
        try:
            spy_close = pd.read_parquet(spy_path)["Close"]
        except Exception:
            spy_close = None

    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    tasks = []
    for f in files:
        t = Path(f).name.replace("_1d.parquet", "")
        if t in ("SPY", "QQQ", "IWM"):
            continue
        tasks.append((t, f))
    if args.max_tickers:
        tasks = tasks[: args.max_tickers]

    print(f"🔍 Full-universe scanner backtest over {len(tasks)} tickers ({TICKER_CACHE_DIR})")
    print(f"   Scanner: {SCANNER_PATH.name} (real production scanner, history-driven)")
    print(f"   Filters: price ≥ ${args.min_price}, 50d vol ≥ {args.min_vol:,.0f}")

    all_trades = []
    workers = args.workers if args.workers > 0 else None
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(args.min_price, args.min_vol, spy_close)) as ex:
        futs = {ex.submit(process_ticker, task): task[0] for task in tasks}
        done = 0
        for fut in as_completed(futs):
            done += 1
            trades = fut.result()
            if trades:
                all_trades.extend(trades)
            if done % 300 == 0 or done == len(tasks):
                print(f"   {done}/{len(tasks)} tickers · {len(all_trades):,} trades · {time.time()-t0:.0f}s", flush=True)

    if not all_trades:
        print("❌ No trades generated")
        return

    df = pd.DataFrame(all_trades)
    elapsed = time.time() - t0
    print(f"\n{'='*100}")
    print(f"📊 SCANNER UNIVERSE BACKTEST COMPLETE — {elapsed:.0f}s")
    print(f"   Tickers processed: {len(tasks)}")
    print(f"   Trades simulated: {len(df):,}")
    print(f"   Patterns: {sorted(df['pattern'].unique())}")
    print(f"{'='*100}\n")

    trades_path = OUTPUT_DIR / "scanner_universe_trades.csv"
    df.to_csv(trades_path, index=False)
    print(f"💾 Trades saved to {trades_path} ({trades_path.stat().st_size:,} bytes)")

    # ── Summary: buy strategy x exit rule ──
    summary_rows = []
    for buy_s in sorted(df["strategy"].unique()):
        for exit_r in sorted(df["exit_rule"].unique()):
            sdf = df[(df["strategy"] == buy_s) & (df["exit_rule"] == exit_r)]
            n = len(sdf)
            if n < 5:
                continue
            summary_rows.append({
                "buy_strategy": buy_s, "exit_rule": exit_r, "trades": n,
                "win_pct": round(sdf["win"].mean() * 100, 1),
                "avg_ret": round(sdf["ret"].mean(), 2),
                "avg_rr": round(sdf["rr_ratio"].mean(), 2),
                "sharpe": round(sdf["ret"].mean() / sdf["ret"].std(), 2) if sdf["ret"].std() > 0 else 0,
                "median_ret": round(sdf["ret"].median(), 2),
                "max_ret": round(sdf["ret"].max(), 2),
                "min_ret": round(sdf["ret"].min(), 2),
            })
    summary_df = pd.DataFrame(summary_rows)
    summary_path = OUTPUT_DIR / "scanner_universe_summary.csv"
    summary_df.to_csv(summary_path, index=False)
    print(f"💾 Summary saved to {summary_path}")

    # ── Pattern x strategy x exit rollups ──
    pattern_rows = []
    for pat, names in PATTERN_GROUPS.items():
        pdf = df[df["pattern"].isin(names)]
        for buy_s in sorted(df["strategy"].unique()):
            sdf = pdf[pdf["strategy"] == buy_s]
            if len(sdf) < 5:
                continue
            for exit_r in ("stop_loss", "trail_2atr", "target_3r",
                           "chandelier_uncapped", "parabolic_sar", "scale_out_369",
                           "stop_8pct", "ibd_stop8_tp20", "rs_quicksand_exit", "scale_ma_1_3"):
                es = sdf[sdf["exit_rule"] == exit_r]
                if len(es) < 3:
                    continue
                pattern_rows.append({
                    "pattern": pat, "strategy": buy_s, "exit_rule": exit_r,
                    "trades": len(es),
                    "win_pct": round(es["win"].mean() * 100, 1),
                    "avg_ret": round(es["ret"].mean(), 2),
                })
    pat_df = pd.DataFrame(pattern_rows)
    pat_path = OUTPUT_DIR / "scanner_universe_pattern_summary.csv"
    pat_df.to_csv(pat_path, index=False)
    print(f"💾 Pattern summary saved to {pat_path}")

    # ── Print top lines ──
    print(f"\n📊 BUY STRATEGY × EXIT RULE SUMMARY (top 20 by Sharpe)")
    print(f"{'Buy Strategy':<40} {'Exit':<10} {'Trades':>8} {'Win%':>7} {'AvgRet':>8} {'Sharpe':>7}")
    top = summary_df.sort_values("sharpe", ascending=False).head(20)
    for _, r in top.iterrows():
        print(f"{r['buy_strategy']:<40} {r['exit_rule']:<10} {int(r['trades']):>8,} {r['win_pct']:>6.1f}% {r['avg_ret']:>7.2f}% {r['sharpe']:>7.2f}")

    return df, summary_df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=0, help="0 = all cores")
    ap.add_argument("--max-tickers", type=int, default=0)
    ap.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE)
    ap.add_argument("--min-vol", type=float, default=DEFAULT_MIN_VOL_50)
    args = ap.parse_args()
    run_universe_backtest(args)
