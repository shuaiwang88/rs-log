#!/usr/bin/env python3
"""
trend_following_backtest.py
===========================
Python port of the 48 Turtle/Seykota trend-following strategies from
github.com/trustdan/trend-following-backtesting-strategies, run across the full
ticker_cache universe.

The repo ships ~47 Pine Script "alt" variants layered on one core Donchian-breakout
engine (Ed-Seykota.pine). This script reimplements that engine in numpy and encodes
every variant as a config dict faithful to the Pine inputs (entry lookback, N=ATR,
stops, pyramiding, profit targets, time exits, breakeven locks, filters, SAR, etc).

Strategy registry: "Baseline" (Turtle Core v2.2) + alt1..alt47.

Usage:
    python3 python/backtests/trend_following_backtest.py
    python3 python/backtests/trend_following_backtest.py --strategies alt10,alt26,alt45
    python3 python/backtests/trend_following_backtest.py --universe all --min-bars 250

Outputs (in python/backtests/):
    trend_following_results.csv      per strategy x ticker metrics
    trend_following_summary.csv      strategy power rankings across the universe
    trend_following_report.html      browsable HTML report
"""
import argparse
import glob
import json
import math
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = Path(__file__).resolve().parent

DEFAULT_MIN_PRICE = 12.0
DEFAULT_MIN_VOL_50 = 500_000
MIN_BARS = 100
INITIAL_CAPITAL = 100_000.0
# Minimum ATR (N) as a fraction of price for an entry to be sized at all. Below this,
# per-share risk is too close to zero and position sizing (cash * risk% / per_share_risk)
# blows up to an unrealistic share count on the first adverse tick (seen historically as
# -inf / 1e27-scale returns on illiquid or stale-data tickers, e.g. alt23_keltner_channel).
MIN_N_FRACTION = 0.001


# ══════════════════════════════════════════════════════════════════════════════
# Indicator library
# ══════════════════════════════════════════════════════════════════════════════

def atr_wilder(h, l, c, n=20):
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))
    atr = np.zeros_like(c)
    atr[0] = tr[0]
    alpha = 1.0 / n
    for i in range(1, len(c)):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    return atr


def rolling_max_prev(x, n):
    s = pd.Series(x)
    return s.rolling(n).max().shift(1).values


def rolling_min_prev(x, n):
    s = pd.Series(x)
    return s.rolling(n).min().shift(1).values


def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().values


def sma(x, n):
    return pd.Series(x).rolling(n, min_periods=min(10, n)).mean().values


def rsi(x, n=14):
    s = pd.Series(x)
    d = s.diff()
    up = d.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d.clip(upper=0.0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    r = 100 - 100 / (1 + rs)
    return r.fillna(50.0).values


def adx(h, l, c, n=14):
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_s = tr.ewm(alpha=1.0 / n, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1.0 / n, adjust=False).mean() / atr_s
    minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1.0 / n, adjust=False).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / n, adjust=False).mean().fillna(0.0).values


def parabolic_sar(h, l, start=0.02, inc=0.02, maximum=0.2):
    n = len(h)
    sar = np.empty(n)
    af = start
    trend = 1
    ep = h[0]
    sar[0] = l[0]
    for i in range(1, n):
        sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
        if trend == 1:
            sar[i] = min(sar[i], l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < sar[i]:
                trend = -1
                sar[i] = ep
                ep = l[i]
                af = start
            elif h[i] > ep:
                ep = h[i]
                af = min(af + inc, maximum)
        else:
            sar[i] = max(sar[i], h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > sar[i]:
                trend = 1
                sar[i] = ep
                ep = h[i]
                af = start
            elif l[i] < ep:
                ep = l[i]
                af = min(af + inc, maximum)
    return sar


def efficiency_ratio(c, n=20):
    s = pd.Series(c)
    direction = (s - s.shift(n)).abs()
    vol = s.diff().abs().rolling(n).sum()
    return (direction / vol.replace(0, np.nan)).fillna(0.0).values


# ══════════════════════════════════════════════════════════════════════════════
# Core engine
# ══════════════════════════════════════════════════════════════════════════════

def run_strategy(o, h, l, c, v, dates, cfg, spy_above_200=None, pattern_gate=None):
    """Simulate one strategy on one ticker. Returns trade list + equity curve stats.

    pattern_gate: optional bool array aligned to bars, True where the ticker is inside a
    real IBD base per the production scanner (see alt48_pattern_gated). When set, entries
    are only allowed while the gate is True.
    """
    n = len(c)
    N = atr_wilder(h, l, c, cfg.get("nLen", 20))
    entry_len = cfg.get("entryLen", 55)
    exit_len = cfg.get("exitLen", 10)
    stop_n = cfg.get("stopN", 2.0)
    add_step_n = cfg.get("addStepN", 0.5)
    max_units = cfg.get("maxUnits", 4)
    risk_pct = cfg.get("riskPct", 1.0)
    allow_short = cfg.get("allowShort", True)

    # Entry-mode indicators
    entry_mode = cfg.get("entry", "donchian")
    if entry_mode == "ema":
        fast_e = ema(c, cfg.get("fastLen", 50))
        slow_e = ema(c, cfg.get("slowLen", 200))
    if entry_mode == "atr_channel":
        base_ma = sma(c, cfg.get("baseLen", 100))
        k_entry = cfg.get("kEntry", 2.0)
        k_exit = cfg.get("kExit", 2.0)
        upper_e = base_ma + k_entry * N
        lower_e = base_ma - k_entry * N
        upper_x = base_ma - k_exit * N
        lower_x = base_ma + k_exit * N
    if entry_mode == "keltner":
        kema = ema(c, cfg.get("keltnerLen", 55))
        kmult = cfg.get("keltnerMult", 2.5)
        upper_e = kema + kmult * N
        lower_e = kema - kmult * N
    if entry_mode == "weekly_donchian":
        don_hi_prev = rolling_max_prev(h, cfg.get("wEntryLen", 26) * 5)
        don_lo_prev = rolling_min_prev(l, cfg.get("wEntryLen", 26) * 5)
        exit_hi_prev = rolling_max_prev(h, cfg.get("wExitLen", 13) * 5)
        exit_lo_prev = rolling_min_prev(l, cfg.get("wExitLen", 13) * 5)
    else:
        don_hi_prev = rolling_max_prev(h, entry_len)
        don_lo_prev = rolling_min_prev(l, entry_len)
        exit_hi_prev = rolling_max_prev(h, exit_len)
        exit_lo_prev = rolling_min_prev(l, exit_len)

    # Trailing indicators
    trail_len = cfg.get("trailLen", 22)
    trail_n = cfg.get("trailN", 3.0)
    trail_hi = rolling_max_prev(h, trail_len) if trail_len else None
    trail_lo = rolling_min_prev(l, trail_len) if trail_len else None

    # Filters
    rsi_v = rsi(c, cfg.get("rsiLen", 14))
    adx_v = adx(pd.Series(h), pd.Series(l), pd.Series(c), cfg.get("adxLen", 14)) if cfg.get("useAdx") or cfg.get("adxFilter") else None
    adx_thresh = cfg.get("adxThresh", 25.0)
    sar_v = parabolic_sar(h, l, cfg.get("sarStart", 0.02), cfg.get("sarIncrement", 0.02), cfg.get("sarMax", 0.2)) if cfg.get("exit") == "parabolic" else None
    er_v = efficiency_ratio(c, cfg.get("erLen", 20)) if cfg.get("antiChop") else None
    tide_v = sma(c, cfg.get("tideLen", 200)) if cfg.get("tide") else None
    pb_ema_v = ema(c, cfg.get("pullbackLen", 10)) if cfg.get("pullbackPyramid") else None

    # State
    cash = INITIAL_CAPITAL
    shares = 0.0
    avg_cost = 0.0
    units = 0
    N_entry = None
    last_add = None
    entry_price = None
    bars_in_pos = 0
    be_locked = False
    t_hits = [False] * 6
    momentum_scaled = cfg.get("momentumScale", False)
    half_size = cfg.get("initialSize", 0.5) if momentum_scaled else 1.0

    eq_curve = np.empty(n)
    trades = []
    peak_eq = INITIAL_CAPITAL
    max_dd = 0.0
    open_trade = None  # dict tracking entry equity for round-trip stats

    def shares_for_unit(risk_mult=1.0, size_mult=1.0):
        eff_risk = risk_pct * risk_mult * size_mult
        per_share_risk = max(stop_n * (N_entry or 0.01), 1e-9)
        return max(1, math.floor(cash * (eff_risk / 100.0) / per_share_risk))

    for i in range(1, n):
        if i < 1:
            continue
        in_pos = shares > 0 or shares < 0
        dir_sign = 1 if shares > 0 else (-1 if shares < 0 else 0)
        if in_pos:
            bars_in_pos += 1

        # ── Regime filter (SPY > SMA200) ──
        regime_ok = True
        if cfg.get("useMarket") and spy_above_200 is not None and dates[i] in spy_above_200:
            regime_ok = spy_above_200[dates[i]]

        # ── Entries ──
        long_entry = short_entry = False
        if not in_pos and regime_ok and cash > 0:
            vol_ok = True
            if cfg.get("minVol", 0) > 0:
                vol_ok = v[max(0, i - 20):i + 1].mean() >= cfg.get("minVol", 0)
            adx_ok = True
            if adx_v is not None and cfg.get("adxFilter"):
                adx_ok = adx_v[i] >= adx_thresh
            tide_ok = True
            if tide_v is not None and not np.isnan(tide_v[i]):
                tide_ok = c[i] > tide_v[i]
            er_ok = True
            if er_v is not None:
                er_ok = er_v[i] >= cfg.get("erThresh", 0.3)
            rsi_ok_long = not cfg.get("rsiEntry") or rsi_v[i] > cfg.get("rsiLongThresh", 50)
            rsi_ok_short = not cfg.get("rsiEntry") or rsi_v[i] < cfg.get("rsiShortThresh", 50)

            if entry_mode == "ema":
                long_entry = (fast_e[i] > slow_e[i] and fast_e[i - 1] <= slow_e[i - 1]) and adx_ok and vol_ok and rsi_ok_long
                short_entry = (fast_e[i] < slow_e[i] and fast_e[i - 1] >= slow_e[i - 1]) and adx_ok and vol_ok and rsi_ok_short
            elif entry_mode == "atr_channel":
                long_entry = c[i] > upper_e[i] and adx_ok and vol_ok and rsi_ok_long
                short_entry = c[i] < lower_e[i] and adx_ok and vol_ok and rsi_ok_short
            elif entry_mode == "keltner":
                long_entry = c[i] > upper_e[i] and adx_ok and vol_ok and rsi_ok_long
                short_entry = c[i] < lower_e[i] and adx_ok and vol_ok and rsi_ok_short
            else:
                use_intrabar = cfg.get("intrabar", False)
                lv = h[i] if use_intrabar else c[i]
                sv = l[i] if use_intrabar else c[i]
                long_entry = (lv > don_hi_prev[i]) and adx_ok and vol_ok and tide_ok and er_ok and rsi_ok_long
                short_entry = (sv < don_lo_prev[i]) and adx_ok and vol_ok and tide_ok and er_ok and rsi_ok_short
                # close-confirmed variant requires close > donHi (already close-based)
                if cfg.get("closeConfirmed") and use_intrabar:
                    long_entry = long_entry and c[i] > don_hi_prev[i]
                    short_entry = short_entry and c[i] < don_lo_prev[i]

            if pattern_gate is not None:
                gate_ok = i < len(pattern_gate) and bool(pattern_gate[i])
                long_entry = long_entry and gate_ok
                short_entry = short_entry and gate_ok

        if long_entry and not in_pos:
            N_entry = N[i]
            if N_entry is None or N_entry <= 0 or np.isnan(N_entry):
                N_entry = max(N[i - 1], 0.01)
            if N_entry >= c[i] * MIN_N_FRACTION:
                sh = shares_for_unit(size_mult=half_size)
                cost = sh * c[i]
                cash -= cost
                shares = sh
                avg_cost = c[i]
                units = 1
                entry_price = c[i]
                last_add = c[i]
                be_locked = False
                t_hits = [False] * 6
                bars_in_pos = 0
                open_trade = {"entry_eq": cash + shares * c[i], "dir": 1, "units": sh}
        elif short_entry and not in_pos and allow_short:
            N_entry = N[i]
            if N_entry is None or N_entry <= 0 or np.isnan(N_entry):
                N_entry = max(N[i - 1], 0.01)
            if N_entry >= c[i] * MIN_N_FRACTION:
                sh = shares_for_unit(size_mult=half_size)
                cost = sh * c[i]
                cash += cost
                shares = -sh
                avg_cost = c[i]
                units = 1
                entry_price = c[i]
                last_add = c[i]
                be_locked = False
                t_hits = [False] * 6
                bars_in_pos = 0
                open_trade = {"entry_eq": cash + shares * c[i], "dir": -1, "units": sh}

        # ── Add-on pyramiding ──
        if in_pos and units < max_units and regime_ok:
            add_ok = False
            if dir_sign > 0 and c[i] >= (last_add or 0) + add_step_n * N_entry:
                add_ok = True
            if dir_sign < 0 and c[i] <= (last_add or 0) - add_step_n * N_entry:
                add_ok = True
            # momentum gating for adds
            if add_ok and cfg.get("momentumAdds"):
                if dir_sign > 0:
                    add_ok = rsi_v[i] > cfg.get("rsiLongAdd", 60)
                else:
                    add_ok = rsi_v[i] < cfg.get("rsiShortAdd", 40)
            # volatility gating for adds
            if add_ok and cfg.get("volGate") and N[i] > (N_entry or 1e-9) * cfg.get("volGateMult", 1.0):
                add_ok = False
            # pullback gating: wait for pullback to EMA
            if add_ok and cfg.get("pullbackPyramid") and pb_ema_v is not None:
                add_ok = dir_sign > 0 and c[i] <= pb_ema_v[i] * 1.02 or (dir_sign < 0 and c[i] >= pb_ema_v[i] * 0.98)

            if add_ok:
                mult = 1.0
                if cfg.get("fractional"):
                    mult = [1.0, 0.75, 0.5, 0.25][min(units, 3)]
                if cfg.get("tapered"):
                    mult = [1.0, 0.75, 0.5, 0.33][min(units, 3)]
                sh = shares_for_unit(size_mult=mult)
                if dir_sign > 0:
                    cash -= sh * c[i]
                    shares += sh
                else:
                    cash += sh * c[i]
                    shares -= sh
                units += 1
                last_add = c[i]
                open_trade["units"] = open_trade.get("units", 0) + sh

        # ── Exits ──
        if in_pos:
            # Profit targets (scale out)
            targets = cfg.get("targets")
            if targets:
                t1, t2, t3 = targets
                for k, (tN, frac) in enumerate(((t1, 0.25), (t2, 0.25), (t3, 0.25))):
                    if not t_hits[k]:
                        target_px = entry_price + tN * (N_entry or 0) if dir_sign > 0 else entry_price - tN * (N_entry or 0)
                        hit = h[i] >= target_px if dir_sign > 0 else l[i] <= target_px
                        if hit:
                            t_hits[k] = True
                            close_sh = open_trade["units"] * frac if open_trade else 0
                            if close_sh > 0 and abs(shares) >= close_sh:
                                if dir_sign > 0:
                                    cash += close_sh * target_px
                                    shares -= close_sh
                                else:
                                    cash -= close_sh * target_px
                                    shares += close_sh
                                open_trade["units"] -= close_sh

            # Stop / trailing computation
            stop_px = None
            if dir_sign > 0:
                init_stop = avg_cost - stop_n * (N_entry or 0)
                trail_stop = None
                if cfg.get("exit") == "chandelier" and trail_hi is not None:
                    trail_stop = trail_hi[i] - trail_n * (N_entry or 0)
                elif cfg.get("exit") == "parabolic" and sar_v is not None:
                    trail_stop = max(init_stop, sar_v[i])
                elif cfg.get("exit") == "pure_atr":
                    trail_stop = init_stop
                elif cfg.get("exit") in (None, "donchian") and exit_lo_prev is not None:
                    trail_stop = exit_lo_prev[i]
                # time-based tight trail
                if cfg.get("tightTrail") and bars_in_pos >= cfg.get("tightenAfter", 30):
                    trail_stop = max(trail_stop or init_stop, (trail_hi[i] if trail_hi is not None else h[i]) - cfg.get("tightTrailN", 1.5) * (N_entry or 0))
                stop_px = max(init_stop, trail_stop or -np.inf)
                # breakeven lock
                if cfg.get("breakeven") and (c[i] - entry_price) >= cfg.get("breakEvenN", 2.0) * (N_entry or 0):
                    stop_px = max(stop_px, entry_price)
                    be_locked = True
                if cfg.get("progressiveBE"):
                    gain = c[i] - entry_price
                    mult = (N_entry or 0)
                    if mult > 0:
                        if gain >= 3 * mult:
                            stop_px = max(stop_px, entry_price + 1.0 * mult)
                        elif gain >= 2 * mult:
                            stop_px = max(stop_px, entry_price + 0.5 * mult)
                        elif gain >= 1 * mult:
                            stop_px = max(stop_px, entry_price)
            else:
                init_stop = avg_cost + stop_n * (N_entry or 0)
                trail_stop = None
                if cfg.get("exit") == "chandelier" and trail_lo is not None:
                    trail_stop = trail_lo[i] + trail_n * (N_entry or 0)
                elif cfg.get("exit") == "parabolic" and sar_v is not None:
                    trail_stop = min(init_stop, sar_v[i])
                elif cfg.get("exit") == "pure_atr":
                    trail_stop = init_stop
                elif cfg.get("exit") in (None, "donchian") and exit_hi_prev is not None:
                    trail_stop = exit_hi_prev[i]
                if cfg.get("tightTrail") and bars_in_pos >= cfg.get("tightenAfter", 30):
                    trail_stop = min(trail_stop or init_stop, (trail_lo[i] if trail_lo is not None else l[i]) + cfg.get("tightTrailN", 1.5) * (N_entry or 0))
                stop_px = min(init_stop, trail_stop or np.inf)
                if cfg.get("breakeven") and (entry_price - c[i]) >= cfg.get("breakEvenN", 2.0) * (N_entry or 0):
                    stop_px = min(stop_px, entry_price)
                    be_locked = True
                if cfg.get("progressiveBE"):
                    gain = entry_price - c[i]
                    mult = (N_entry or 0)
                    if mult > 0:
                        if gain >= 3 * mult:
                            stop_px = min(stop_px, entry_price - 1.0 * mult)
                        elif gain >= 2 * mult:
                            stop_px = min(stop_px, entry_price - 0.5 * mult)
                        elif gain >= 1 * mult:
                            stop_px = min(stop_px, entry_price)

            hit_stop = (dir_sign > 0 and l[i] <= stop_px) or (dir_sign < 0 and h[i] >= stop_px)
            time_exit = False
            if cfg.get("timeExitBars") and bars_in_pos >= cfg.get("timeExitBars"):
                time_exit = True

            if hit_stop or time_exit:
                fill = stop_px if hit_stop else c[i]
                if dir_sign > 0:
                    cash += shares * fill
                else:
                    cash -= abs(shares) * fill
                # record round trip
                if open_trade:
                    end_eq = cash + 0
                    net_pnl = end_eq - open_trade["entry_eq"]
                    trades.append({
                        "entry_eq": open_trade["entry_eq"], "pnl": net_pnl,
                        "ret_pct": net_pnl / open_trade["entry_eq"] * 100.0,
                        "bars": bars_in_pos,
                        "dir": dir_sign,
                        "units": open_trade["units"],
                        "exit_type": "stop" if hit_stop else "time",
                    })
                shares = 0.0
                units = 0
                avg_cost = 0.0
                open_trade = None
                bars_in_pos = 0

        # equity mark
        eq = cash + shares * c[i]
        eq_curve[i] = eq
        peak_eq = max(peak_eq, eq)
        dd = (peak_eq - eq) / peak_eq * 100.0 if peak_eq > 0 else 0.0
        max_dd = max(max_dd, dd)

    # Close any open position at last close
    if shares != 0 and open_trade:
        if shares > 0:
            cash += shares * c[-1]
        else:
            cash -= abs(shares) * c[-1]
        end_eq = cash
        net_pnl = end_eq - open_trade["entry_eq"]
        trades.append({
            "entry_eq": open_trade["entry_eq"], "pnl": net_pnl,
            "ret_pct": net_pnl / open_trade["entry_eq"] * 100.0,
            "bars": bars_in_pos, "dir": 1 if shares > 0 else -1,
            "units": open_trade["units"], "exit_type": "eod",
        })
        eq_curve[-1] = cash
        peak_eq = max(peak_eq, cash)
        dd = (peak_eq - cash) / peak_eq * 100.0 if peak_eq > 0 else 0.0
        max_dd = max(max_dd, dd)

    final_eq = cash
    total_ret = (final_eq / INITIAL_CAPITAL - 1.0) * 100.0
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    return {
        "trades": len(trades),
        "total_ret": total_ret,
        "win_rate": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
        "max_dd": max_dd,
        "final_eq": final_eq,
        "avg_ret_pct": np.mean([t["ret_pct"] for t in trades]) if trades else 0.0,
        "median_ret_pct": np.median([t["ret_pct"] for t in trades]) if trades else 0.0,
        "avg_bars": np.mean([t["bars"] for t in trades]) if trades else 0.0,
        "long_trades": sum(1 for t in trades if t["dir"] == 1),
        "short_trades": sum(1 for t in trades if t["dir"] == -1),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Strategy registry (faithful to the Pine inputs)
# ══════════════════════════════════════════════════════════════════════════════

BASE = {"entry": "donchian", "entryLen": 55, "exitLen": 10, "nLen": 20,
        "stopN": 2.0, "addStepN": 0.5, "maxUnits": 4, "riskPct": 1.0,
        "allowShort": True}

STRATEGIES = {
    "Baseline": dict(BASE),
    "alt1_fast_breakout": {**BASE, "entryLen": 20},
    "alt2_ema_crossover": {**BASE, "entry": "ema", "fastLen": 50, "slowLen": 200,
                           "stopN": 2.5, "trailLen": 22, "trailN": 3.0, "exit": "chandelier"},
    "alt3_atr_channel": {**BASE, "entry": "atr_channel", "baseLen": 100, "kEntry": 2.0, "kExit": 2.0},
    "alt4_weekly_donchian": {**BASE, "entry": "weekly_donchian", "wEntryLen": 26, "wExitLen": 13},
    "alt5_close_confirmed": {**BASE, "closeConfirmed": True, "tide": True, "tideLen": 200},
    "alt6_pure_atr_stop": {**BASE, "exit": "pure_atr"},
    "alt7_chandelier_trail": {**BASE, "exit": "chandelier", "trailLen": 22, "trailN": 3.0},
    "alt9_time_exit": {**BASE, "timeExitBars": 40},
    "alt10_profit_targets": {**BASE, "exit": "chandelier", "trailLen": 22, "trailN": 3.0,
                             "targets": (3.0, 6.0, 9.0)},
    "alt11_tapered_pyramid": {**BASE, "tapered": True},
    "alt12_accelerated_pyramid": {**BASE, "addStepN": 0.25, "maxUnits": 6, "riskPct": 0.75},
    "alt13_volatility_gated": {**BASE, "volGate": True, "volGateMult": 1.0},
    "alt14_pullback_pyramid": {**BASE, "pullbackPyramid": True, "pullbackLen": 10},
    "alt15_single_position": {**BASE, "maxUnits": 1, "riskPct": 4.0},
    "alt16_anti_chop": {**BASE, "adxFilter": True, "adxThresh": 25, "antiChop": True, "erThresh": 0.3},
    "alt17_dual_timeframe": {**BASE, "useMarket": True},
    "alt19_intrabar_execution": {**BASE, "intrabar": True},
    "alt20_asymmetric_ls": {**BASE, "entryLen": 35, "stopN": 2.5, "addStepN": 0.75},  # short-side tuned
    "alt21_breakeven_lock": {**BASE, "breakeven": True, "breakEvenN": 2.0,
                             "exit": "chandelier", "trailLen": 22, "trailN": 3.0},
    "alt22_parabolic_sar": {**BASE, "exit": "parabolic", "sarStart": 0.02, "sarIncrement": 0.02,
                            "sarMax": 0.2, "targets": (3.0, 6.0, 9.0)},
    "alt23_keltner_channel": {**BASE, "entry": "keltner", "keltnerLen": 55, "keltnerMult": 2.5},
    "alt24_vol_adjusted_targets": {**BASE, "targets": (2.5, 5.0, 8.0)},
    "alt25_time_profit_lock": {**BASE, "tightTrail": True, "tightenAfter": 30, "tightTrailN": 1.5},
    "alt26_fractional_pyramid": {**BASE, "fractional": True, "exit": "chandelier", "trailLen": 22,
                                 "trailN": 3.0, "targets": (3.0, 6.0, 9.0)},
    "alt27_asymmetric_rr": {**BASE, "stopN": 1.5, "trailN": 2.5, "targets": (4.0, 8.0, 12.0)},
    "alt28_adx_filter": {**BASE, "adxFilter": True, "adxThresh": 25, "exit": "chandelier",
                         "trailLen": 22, "trailN": 3.0, "targets": (3.0, 6.0, 9.0)},
    "alt29_multi_stage_scaling": {**BASE, "maxUnits": 6, "riskPct": 0.75, "targets": (1.5, 3.0, 5.0)},
    "alt30_momentum_pyramid": {**BASE, "momentumAdds": True, "rsiLongAdd": 60, "rsiShortAdd": 40},
    "alt31_fractional_breakeven": {**BASE, "fractional": True, "breakeven": True, "breakEvenN": 2.0,
                                   "exit": "chandelier", "trailLen": 22, "trailN": 3.0, "targets": (3.0, 6.0, 9.0)},
    "alt32_momentum_time": {**BASE, "momentumAdds": True, "rsiLongAdd": 60, "rsiShortAdd": 40,
                            "tightTrail": True, "tightenAfter": 30, "tightTrailN": 1.5},
    "alt33_progressive_breakeven": {**BASE, "progressiveBE": True},
    "alt34_fractional_momentum": {**BASE, "fractional": True, "momentumAdds": True,
                                  "rsiLongAdd": 60, "rsiShortAdd": 40},
    "alt35_fractional_time": {**BASE, "fractional": True, "tightTrail": True, "tightenAfter": 30,
                              "tightTrailN": 1.5},
    "alt36_breakeven_momentum": {**BASE, "breakeven": True, "breakEvenN": 2.0, "momentumAdds": True,
                                 "rsiLongAdd": 60, "rsiShortAdd": 40},
    "alt37_breakeven_time": {**BASE, "breakeven": True, "breakEvenN": 2.0, "tightTrail": True,
                             "tightenAfter": 30, "tightTrailN": 1.5},
    "alt38_triple_combo": {**BASE, "fractional": True, "breakeven": True, "breakEvenN": 2.0,
                           "momentumAdds": True, "rsiLongAdd": 60, "rsiShortAdd": 40},
    "alt39_adaptive_targets": {**BASE, "targets": (3.0, 6.0, 9.0), "exit": "chandelier",
                               "trailLen": 22, "trailN": 3.0},  # age-adaptive targets simplified
    "alt40_ultimate": {**BASE, "fractional": True, "breakeven": True, "breakEvenN": 2.0,
                       "momentumAdds": True, "rsiLongAdd": 60, "rsiShortAdd": 40,
                       "tightTrail": True, "tightenAfter": 30, "tightTrailN": 1.5},
    "alt41_sector_adaptive": {**BASE, "fractional": True, "targets": (2.0, 4.0, 6.0)},
    "alt42_momentum_gated_time": {**BASE, "timeExitBars": 40, "momentumAdds": True,
                                  "rsiLongAdd": 60, "rsiShortAdd": 40},
    "alt43_volatility_adaptive": {**BASE, "targets": (3.0, 6.0, 9.0)},
    "alt44_adx_pyramiding": {**BASE, "adxFilter": True, "adxThresh": 25, "fractional": True,
                             "targets": (3.0, 6.0, 9.0)},
    "alt45_dual_momentum": {**BASE, "rsiEntry": True, "rsiLongThresh": 50, "rsiShortThresh": 50,
                            "targets": (3.0, 6.0, 9.0), "exit": "chandelier", "trailLen": 22, "trailN": 3.0},
    "alt46_sector_adaptive_params": {**BASE, "fractional": True, "targets": (4.0, 7.0, 10.0)},
    "alt47_momentum_scaled_sizing": {**BASE, "momentumScale": True, "initialSize": 0.5,
                                     "rsiEntry": True, "rsiLongThresh": 50, "rsiShortThresh": 50,
                                     "fractional": True, "targets": (3.0, 6.0, 9.0)},
    # Same Donchian core as Baseline, but only allowed to enter while the ticker is
    # concurrently inside one of OUR scanner's bases (python/tv_pattern_scanner.py, the
    # drw_pattern.pine port). Tests whether gating trend-following entries by pattern
    # quality beats the ungated baseline.
    "alt48_pattern_gated": {**BASE, "patternGate": True},
}


# ══════════════════════════════════════════════════════════════════════════════
# Parallel runner
# ══════════════════════════════════════════════════════════════════════════════

_G_SPY = None
_G_CFG = None
_G_SCAN_FN = None  # lazily loaded per worker, only if a requested strategy needs patternGate


def _init_worker(spy_above_200, cfg):
    global _G_SPY, _G_CFG
    _G_SPY = spy_above_200
    _G_CFG = cfg


def _pattern_gate_for(ticker, fpath, n_bars):
    """Bool array (len n_bars) True where the ticker was inside one of OUR scanner's
    bases (drw_pattern.pine port), rebuilt from its ended-base history + live status.
    Loaded lazily/once per worker process to avoid paying the cost for strategies that
    don't use it."""
    global _G_SCAN_FN
    if _G_SCAN_FN is None:
        from tv_engine import base_gate, prepare_frame, scan_record
        _G_SCAN_FN = (prepare_frame, scan_record, base_gate)
    gate = np.zeros(n_bars, dtype=bool)
    try:
        _prepare, _scan, _gate = _G_SCAN_FN
        raw = pd.read_parquet(fpath)
        df = _prepare(raw)
        if df is None:
            return gate
        rec, _ = _scan(ticker, str(fpath), None, df)
        if rec is None:
            return gate
        gate = _gate(rec, len(df), n_bars, df.index, raw.index)
    except Exception:
        pass
    return gate


def _run_ticker(args):
    ticker, fpath, strat_names = args
    out = []
    try:
        df = pd.read_parquet(fpath)
        if df.empty or len(df) < MIN_BARS:
            return []
        if df["Close"].iloc[-1] < _G_CFG["min_price"] or df["Volume"].tail(50).mean() < _G_CFG["min_vol"]:
            return []
        o = df["Open"].values
        h = df["High"].values
        l = df["Low"].values
        c = df["Close"].values
        v = df["Volume"].values
        dates = [str(d)[:10] for d in df.index]
        gate_cache = None
        for name in strat_names:
            cfg = STRATEGIES[name]
            try:
                pattern_gate = None
                if cfg.get("patternGate"):
                    if gate_cache is None:
                        gate_cache = _pattern_gate_for(ticker, fpath, len(c))
                    pattern_gate = gate_cache
                r = run_strategy(o, h, l, c, v, dates, cfg, _G_SPY, pattern_gate)
                out.append({"strategy": name, "ticker": ticker, **r})
            except Exception:
                continue
    except Exception:
        return []
    return out


def load_spy_regime():
    """SPY close > SMA200 boolean per date, for the market-regime variants."""
    fp = TICKER_CACHE_DIR / "SPY_1d.parquet"
    if not fp.exists():
        return None
    try:
        spy = pd.read_parquet(fp)["Close"]
        ma = spy.rolling(200, min_periods=50).mean()
        above = spy > ma
        return {str(d)[:10]: bool(above.loc[d]) for d in above.index}
    except Exception:
        return None


def main(args):
    t0 = time.time()
    cfg = {"min_price": args.min_price, "min_vol": args.min_vol}

    if args.strategies:
        strat_names = [s.strip() for s in args.strategies.split(",") if s.strip() in STRATEGIES]
        if not strat_names:
            print("❌ No valid strategies selected")
            return
    else:
        strat_names = list(STRATEGIES.keys())

    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    tasks = []
    for f in files:
        t = Path(f).name.replace("_1d.parquet", "")
        if t in ("SPY", "QQQ", "IWM"):
            continue
        tasks.append((t, f, strat_names))
    if args.max_tickers:
        tasks = tasks[: args.max_tickers]

    spy_regime = load_spy_regime()
    print(f"🔍 Trend-following backtest: {len(strat_names)} strategies × {len(tasks)} tickers")
    print(f"   Source: github.com/trustdan/trend-following-backtesting-strategies (Turtle/Seykota engine)")
    print(f"   Filters: price ≥ ${args.min_price}, 50d vol ≥ {args.min_vol:,.0f}, min {MIN_BARS} bars")

    all_rows = []
    workers = args.workers if args.workers > 0 else None
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(spy_regime, cfg)) as ex:
        futs = {ex.submit(_run_ticker, task): task[0] for task in tasks}
        done = 0
        for fut in as_completed(futs):
            done += 1
            rows = fut.result()
            if rows:
                all_rows.extend(rows)
            if done % 300 == 0 or done == len(tasks):
                print(f"   {done}/{len(tasks)} tickers · {len(all_rows):,} rows · {time.time()-t0:.0f}s", flush=True)

    if not all_rows:
        print("❌ No results")
        return

    df = pd.DataFrame(all_rows)
    elapsed = time.time() - t0
    print(f"\n{'='*100}")
    print(f"📊 TREND-FOLLOWING BACKTEST COMPLETE — {elapsed:.0f}s")
    print(f"   Strategies: {df['strategy'].nunique()} · Tickers: {df['ticker'].nunique()}")
    print(f"{'='*100}\n")

    results_path = OUTPUT_DIR / "trend_following_results.csv"
    df.to_csv(results_path, index=False)
    print(f"💾 Per-strategy×ticker results saved to {results_path}")

    # ── Power rankings (aggregate across universe) ──
    summary_rows = []
    for name, g in df.groupby("strategy"):
        n_tickers = len(g)
        profitable = (g["total_ret"] > 0).sum()
        summary_rows.append({
            "strategy": name,
            "tickers": n_tickers,
            "success_rate_pct": round(profitable / n_tickers * 100, 1),
            "profitable_tickers": int(profitable),
            "avg_total_ret": round(g["total_ret"].mean(), 2),
            "median_total_ret": round(g["total_ret"].median(), 2),
            "best_ticker_ret": round(g["total_ret"].max(), 2),
            "worst_ticker_ret": round(g["total_ret"].min(), 2),
            "avg_win_rate": round(g["win_rate"].mean(), 1),
            "median_profit_factor": round(g["profit_factor"].median(), 2),
            "avg_profit_factor": round(g["profit_factor"].mean(), 2),
            "avg_max_dd": round(g["max_dd"].mean(), 2),
            "avg_trades": round(g["trades"].mean(), 1),
            "avg_ret_per_trade": round(g["avg_ret_pct"].mean(), 2),
            "avg_hold_bars": round(g["avg_bars"].mean(), 1),
        })
    summary_df = pd.DataFrame(summary_rows)
    # Rank by a robust headline metric: median total return across the universe
    # (mean returns are distorted by compounding outliers in long bull runs).
    summary_df = summary_df.sort_values("median_total_ret", ascending=False).reset_index(drop=True)
    summary_path = OUTPUT_DIR / "trend_following_summary.csv"
    summary_df.to_csv(summary_path, index=False)
    print(f"💾 Power rankings saved to {summary_path}")

    print(f"\n🏆 STRATEGY POWER RANKINGS (by success rate across universe)")
    print(f"{'Strategy':<28} {'Success%':>9} {'AvgRet':>8} {'MedRet':>8} {'WinRate':>8} {'PF':>6} {'MaxDD':>7} {'Trades':>7}")
    for _, r in summary_df.head(25).iterrows():
        print(f"{r['strategy']:<28} {r['success_rate_pct']:>8.1f}% {r['avg_total_ret']:>7.1f}% {r['median_total_ret']:>7.1f}% "
              f"{r['avg_win_rate']:>7.1f}% {r['avg_profit_factor']:>6.2f} {r['avg_max_dd']:>6.1f}% {r['avg_trades']:>7.1f}")

    # Top tickers per top strategy
    top5 = summary_df.head(5)["strategy"].tolist()
    print(f"\n🔥 TOP TICKERS for top-5 strategies:")
    for name in top5:
        g = df[df["strategy"] == name].nlargest(5, "total_ret")
        top = ", ".join(f"{r.ticker} {r.total_ret:+.1f}%" for _, r in g.iterrows())
        print(f"   {name:<28} {top}")

    return df, summary_df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--max-tickers", type=int, default=0)
    ap.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE)
    ap.add_argument("--min-vol", type=float, default=DEFAULT_MIN_VOL_50)
    ap.add_argument("--strategies", type=str, default="",
                    help="comma-separated strategy names; default = all")
    args = ap.parse_args()
    main(args)
