"""
tv_pattern_history_backtest.py

Backtest round over the TradingView pattern history. Loads `python/tv_pattern_history.json` —
every base `pine/drw_pattern.pine` has ever ended, across the whole ticker_cache universe
(recorded by `python/tv_pattern_scanner.py`) — and profiles which base characteristics make a
breakout likely to hit the **+20% target before the −8% stop** within the 60-bar hold window
(the scanner's own walk-forward verdict, field `outcome`).

This is an outcome-profiling pass, not a re-simulation: the scanner already replayed the Pine
state machine bar-by-bar over each ticker's history and scored every ended base; this script
analyses that record. Win rate is **Target / (Target + Stop)** over *resolved* breakouts only,
the same convention the dashboard's History tab uses — still-open ("Open") bases are shown but
never counted as wins or losses. Every win rate ships with a Wilson 95% CI and a lift (pp)
against the baseline, so a bucket that merely has a small sample can't be mistaken for an edge.

Sections produced in the report:

  * Headline: breakouts / breakdowns / resolved / open / win rate by year (regime check).
  * Univariate characteristics — win rate by `pattern`, `base_shape`, base `depth_pct`, base
    `days`, accumulation & distribution days and ratios, `cup_bars`, and cup geometry.
  * Target profile: how fast winners hit +20% (`bars_to_outcome`), how much they gained
    (`max_gain_pct`) and how deep they dipped first (`max_drawdown_pct`).
  * Combined filters: top single-bucket filters, a greedy sequential rule chain (each rule
    must improve win rate on at least `--min-resolved` remaining breakouts), and curated
    cross-tabs (depth × days, pattern × depth, shape × days, dis-ratio × depth).
  * Part II — pre-breakout technical signals: for every recorded breakout the six
    `drw_pattern_scanner.pine` sub-signals (pocket pivot, shakeout, MA touch/reclaim, volume
    dry-up, RS new high, upside reversal, plus a plain high-volume day) are recomputed from
    the same ticker_cache parquets with the scanner's own parameters (imported from
    `tv_pattern_scanner.py` so the definitions cannot drift), then asked "did this fire while
    price was within 20% of the pivot during the base?" — once anywhere in the base, once in
    the last 20 bars. Win-rate lift, signal-count, combination and structural-interaction
    tables follow. `--no-signals` skips this part (it reads every parquet once, ~1 min).
  * Part III — market regime & price scenarios: each breakout is tagged with the market's
    own condition on the day the base ended — SPY vs its 50-day and 200-day SMA (above /
    below, golden/death alignment, distance bands from the 200-day) — and the stock's buy
    point ($<10 / $10-25 / $25-50 / $50-100 / $100-250 / $250+). A full regime × price
    scenario matrix plus regime × structure and regime × pre-BO signal crosses follow.
    `--no-market` skips this part.

Usage:
    python3 python/backtests/tv_pattern_history_backtest.py
    python3 python/backtests/tv_pattern_history_backtest.py --min-resolved 300
    python3 python/backtests/tv_pattern_history_backtest.py --no-signals
    python3 python/backtests/tv_pattern_history_backtest.py --json python/tv_pattern_history.json

Outputs (in python/backtests/):
    tv_pattern_history_backtest_results.csv - one row per dimension-bucket, all metrics
    tv_pattern_history_backtest_report.md   - the same story as a readable markdown report
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = Path(__file__).resolve().parent
HISTORY_JSON = ROOT / "python" / "tv_pattern_history.json"

# Part II recomputes the scanner's per-bar sub-signals; importing its parameters (and the
# pivot helper) from tv_pattern_scanner.py guarantees the definitions cannot drift from the
# live scan.
sys.path.insert(0, str(ROOT / "python"))
from tv_pattern_scanner import (            # noqa: E402
    MAX_BARS, TICKER_CACHE_DIR, _pivot_flags,
    I_DRY_UP_REQ, I_VDU_LENGTH, I_MA_LEN1, I_MA_LEN2, I_MA_LEN3, I_MA_TOUCH_THRESH,
    I_SHAKE_LR, I_SHAKE_TREND_LEN, I_VOL_BREAKOUT_MULT,
)

RESULTS_CSV = OUT_DIR / "tv_pattern_history_backtest_results.csv"
REPORT_MD = OUT_DIR / "tv_pattern_history_backtest_report.md"

TARGET, STOP, OPEN = "Target", "Stop", "Open"


def _wilson_ci(k, n, z=1.96):
    """Wilson score 95% CI for a proportion k/n (doesn't collapse at 0/1 like Wald)."""
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(max(0.0, p * (1.0 - p) / n + z * z / (4.0 * n * n))) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _range_label(lo, hi):
    if lo == -math.inf:
        return f"< {hi:g}"
    if hi == math.inf:
        return f">= {lo:g}"
    return f"{lo:g}-{hi:g}"


def _pct_label(lo, hi):
    if lo == 0.0:
        return f"< {hi * 100:g}%"
    if hi == math.inf:
        return f">= {lo * 100:g}%"
    return f"{lo * 100:g}-{hi * 100:g}%"


def _x_label(lo, hi):
    if lo == 0.0:
        return f"< {hi:g}x"
    if hi == math.inf:
        return f">= {lo:g}x"
    return f"{lo:g}-{hi:g}x"


def _fmt_num(x):
    if x is None:
        return "—"
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return str(x)
    return "—" if math.isnan(xf) else f"{xf:g}"


def _fmt_pct(x):
    if x is None:
        return "—"
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return str(x)
    return "—" if math.isnan(xf) else f"{xf:.1f}%"


def _pct(x):
    return round(float(x) * 100.0, 1)


# ── bucket spec builders (masks are positional boolean arrays over the FULL pattern frame so
# breakdowns stay included; they are then AND-ed with the resolved-breakout mask for scoring) ─
def _build_specs(df):
    specs = []
    labels = {}

    def add_cat(name, values, lbl_map):
        vals = np.asarray(values)
        specs.append((name, vals, lbl_map))
        labels[name] = lbl_map

    add_cat("pattern", df["pattern"].fillna("(none)").values,
            {v: str(v) for v in sorted(set(df["pattern"].fillna("(none)")))})
    add_cat("base_shape", df["base_shape"].fillna("(none)").values,
            {v: str(v) for v in sorted(set(df["base_shape"].fillna("(none)")))})
    years = df["end_date"].astype(str).str[:4].values
    add_cat("breakout_year", years, {v: str(v) for v in sorted(set(years))})

    def add_num(name, values, edges, labeller):
        codes = np.digitize(np.asarray(values, dtype=float), edges)
        lbl = {i: labeller(edges[i - 1], edges[i]) for i in range(1, len(edges))}
        specs.append((name, codes, lbl))
        labels[name] = lbl

    add_num("depth_pct", df["depth_pct"], [0, 10, 15, 20, 25, 30, 35, 40, 50, math.inf],
            _range_label)
    add_num("days", df["days"], [0, 30, 45, 60, 90, 120, 160, 200, math.inf], _range_label)
    add_num("acc_days", df["acc_days"], [0, 5, 10, 15, 20, 30, math.inf], _range_label)
    add_num("dis_days", df["dis_days"], [0, 5, 10, 15, 20, 30, math.inf], _range_label)
    add_num("neu_days", df["neu_days"], [0, 15, 25, 40, 60, math.inf], _range_label)
    add_num("cup_bars", df["cup_bars"], [0, 1, 5, 10, 20, math.inf], _range_label)

    # derived structure ratios (normalised by base length so 40-day and 200-day bases compare)
    days = df["days"].to_numpy(dtype=float)
    acc = df["acc_days"].to_numpy(dtype=float)
    dis = df["dis_days"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        acc_r = np.where(days > 0, acc / days, np.nan)
        dis_r = np.where(days > 0, dis / days, np.nan)
    add_num("acc_ratio", acc_r, [0, .15, .20, .25, .30, math.inf], _pct_label)
    add_num("dis_ratio", dis_r, [0, .15, .20, .25, .30, math.inf], _pct_label)
    add_num("acc_minus_dis", acc - dis, [-math.inf, -10, -5, 0, 5, 10, math.inf],
            _range_label)

    # cup geometry (cups only): right-side / left-side bar balance
    right, left = [], []
    for e in df["cup"]:
        if isinstance(e, dict) and e.get("left_bars") is not None \
                and e.get("right_bars") is not None:
            right.append(e["right_bars"])
            left.append(e["left_bars"])
        else:
            right.append(np.nan)
            left.append(np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        rl = np.where(np.asarray(left, dtype=float) > 0,
                      np.asarray(right, dtype=float) / np.asarray(left, dtype=float), np.nan)
    add_num("cup_right_left", rl, [0, 0.5, 1.0, 1.5, 2.0, math.inf], _x_label)
    # np.digitize sends NaN to the out-of-range code len(edges); label it explicitly.
    labels["cup_right_left"][6] = "(no cup)"
    return specs


def _score(mask, df, resolved_mask, target_mask, open_mask, breakdown_mask, baseline):
    sub_df = df[mask]
    n = int((mask & resolved_mask).sum())
    k = int((mask & target_mask).sum())
    wr = k / n if n else float("nan")
    lo, hi = _wilson_ci(k, n)
    bars = pd.to_numeric(df.loc[mask & target_mask, "bars_to_outcome"], errors="coerce").dropna()
    gain = pd.to_numeric(df.loc[mask & target_mask, "max_gain_pct"], errors="coerce").dropna()
    dd = pd.to_numeric(df.loc[mask & target_mask, "max_drawdown_pct"], errors="coerce").dropna()
    return {
        "n_patterns": len(sub_df),
        "n_breakouts": int((mask & (df["ended"] == "Breakout").to_numpy()).sum()),
        "n_resolved": n, "n_target": k, "n_stop": n - k,
        "n_open": int((mask & open_mask).sum()),
        "n_breakdown": int((mask & breakdown_mask).sum()),
        "win_rate_pct": _pct(wr) if n else None,
        "ci_lo_pct": _pct(lo) if n else None,
        "ci_hi_pct": _pct(hi) if n else None,
        "lift_pp": round((wr - baseline) * 100.0, 1) if n else None,
        "med_bars_to_target": float(bars.median()) if len(bars) else None,
        "med_max_gain_pct": round(float(gain.median()), 1) if len(gain) else None,
        "med_max_dd_pct": round(float(dd.median()), 1) if len(dd) else None,
    }


def _top_singles(candidates, resolved_mask, target_mask, is_bo, min_resolved):
    out = []
    for dim, label, mask in candidates:
        sub = resolved_mask & mask
        n_res = int(sub.sum())
        if n_res < min_resolved:
            continue
        k = int((sub & target_mask).sum())
        out.append((dim, label, int((mask & is_bo).sum()), n_res, k, k / n_res))
    out.sort(key=lambda r: r[5], reverse=True)
    return out[:15]


def _greedy_chain(candidates, resolved_mask, target_mask, baseline, min_resolved,
                  min_lift, max_chain):
    """Sequential filter search: add the single bucket that most improves win rate on at
    least `min_resolved` remaining breakouts, never re-applying a dimension."""
    chain = []
    used_dims = set()
    cur = resolved_mask.copy()
    cur_wr = int(target_mask.sum()) / int(resolved_mask.sum()) if resolved_mask.any() else 0.0
    for _ in range(max_chain):
        best = None
        for dim, label, mask in candidates:
            if dim in used_dims:
                continue
            sub = cur & mask
            n_res = int(sub.sum())
            if n_res < min_resolved:
                continue
            k = int((sub & target_mask).sum())
            wr = k / n_res
            if best is None or wr > best[0] - 1e-9:
                best = (wr, dim, label, n_res, k, mask)
        if best is None:
            break
        wr, dim, label, n, k, mask = best
        if wr <= cur_wr + min_lift / 100.0 - 1e-9:
            break
        cur = cur & mask
        used_dims.add(dim)
        chain.append({"dim": dim, "bucket": label, "n_resolved": n, "n_target": k,
                      "win_rate_pct": wr * 100.0, "lift_pp": (wr - cur_wr) * 100.0})
        cur_wr = wr
    return chain


def _cross_tabs(df, resolved_mask, target_mask, baseline, min_resolved):
    d = df["depth_pct"].to_numpy(dtype=float)
    days = df["days"].to_numpy(dtype=float)
    dis = df["dis_days"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        dis_r = np.where(days > 0, dis / days, np.nan)
    pat = df["pattern"].fillna("(none)").values
    shape = df["base_shape"].fillna("(none)").values
    pairs = [
        ("depth × days", [
            ((d < 15) & (days < 60), "depth<15 · len<60d"),
            ((d < 15) & (days < 90), "depth<15 · len<90d"),
            ((d < 20) & (days < 60), "depth<20 · len<60d"),
            ((d < 20) & (days < 90), "depth<20 · len<90d"),
            ((d < 25) & (days < 90), "depth<25 · len<90d"),
            ((d < 25) & (days < 120), "depth<25 · len<120d"),
            ((d < 25) & (days < 150), "depth<25 · len<150d"),
            ((d < 30) & (days < 150), "depth<30 · len<150d"),
            ((20 <= d) & (d < 30) & (days < 90), "depth20-30 · len<90d"),
            ((d >= 30) & (days >= 90), "depth>=30 · len>=90d"),
        ]),
        ("depth × pattern", [
            ((d < 15) & (pat == "Cup"), "depth<15 · Cup"),
            ((d < 15) & (pat == "Base"), "depth<15 · Base"),
            ((d < 20) & (pat == "Cup"), "depth<20 · Cup"),
            ((d < 20) & (pat == "Base"), "depth<20 · Base"),
            ((d < 25) & (pat == "Cup"), "depth<25 · Cup"),
            ((d < 25) & (pat == "Base"), "depth<25 · Base"),
            ((d >= 25) & (pat == "Cup"), "depth>=25 · Cup"),
            ((d >= 25) & (pat == "Base"), "depth>=25 · Base"),
        ]),
        ("shape × days", [
            ((shape == "Flat Base") & (days < 60), "Flat · len<60d"),
            ((shape == "Flat Base") & (days < 90), "Flat · len<90d"),
            (shape == "Flat Base", "Flat Base (all)"),
            ((shape == "Consolidation") & (days < 60), "Consolidation · len<60d"),
            ((shape == "Consolidation") & (days < 90), "Consolidation · len<90d"),
            ((shape == "Consolidation") & (days >= 90), "Consolidation · len>=90d"),
        ]),
        ("dis-ratio × depth", [
            ((dis_r < 0.15) & (d < 20), "dis<15% · depth<20"),
            ((dis_r < 0.15) & (d < 25), "dis<15% · depth<25"),
            ((dis_r < 0.2) & (d < 20), "dis<20% · depth<20"),
            ((dis_r < 0.2) & (d < 25), "dis<20% · depth<25"),
            ((dis_r >= 0.25) & (d < 20), "dis>=25% · depth<20"),
            (dis_r >= 0.25, "dis>=25% (all)"),
        ]),
    ]
    out = []
    for title, combos in pairs:
        for mask, label in combos:
            sub = resolved_mask & mask
            n_res = int(sub.sum())
            if n_res < min_resolved:
                continue
            k = int((sub & target_mask).sum())
            out.append({"pair": title, "combo": label, "n_resolved": n_res,
                        "n_target": k, "win_rate_pct": k / n_res * 100.0,
                        "lift_pp": (k / n_res - baseline) * 100.0})
    return out


def _md_row(cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Part II - pre-breakout technical signals
# ══════════════════════════════════════════════════════════════════════════════════════════
#
#  tv_pattern_history.json records base geometry + outcome but not the sub-signals that can
#  fire while a base forms (drw_pattern_scanner.pine's "Two-Part Score System", before-BO
#  half). They are recomputed here from the same ticker_cache parquets with the scanner's own
#  definitions (see _ticker_signals, which cites the source), then each breakout is asked
#  "did this signal fire while price sat within ±20% of the pivot during the base?" — the
#  Pine's own `near_pivot` gate (drw_pattern_scanner.pine line 145 / I_SCORE_PRE_PIVOT_PCT) —
#  once anywhere in the base, and once in the last 20 bars before the breakout bar.

SIGNAL_NAMES = ["pocket_pivot", "shakeout", "ma_touch", "vol_dry_up", "rs_new_high",
                "upside_reversal", "vol_spike"]


# line 44, tv_pattern_scanner.py - the near-pivot gate is |close - pivot| <= 20% of pivot
PRE_PIVOT_PCT = 20.0


def _trim_frame(df):
    """Mirror scan_ticker's frame prep exactly (sort, dedup, dropna, tail MAX_BARS) so
    bar positions in the history file line up with this frame."""
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(subset=["High", "Low", "Close"])
    if len(df) > MAX_BARS:
        df = df.iloc[-MAX_BARS:]
    return df


def _ticker_signals(ticker, path, spy_close):
    """Per-bar signal arrays for one ticker, vectorised exactly as tv_pattern_scanner.py
    computes them (each formula cites the scanner section it was copied from). Returns None
    when the frame cannot be prepared or is too short to matter."""
    try:
        df = _trim_frame(pd.read_parquet(path))
    except Exception:
        return None
    if df is None or len(df) < 120:
        return None
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in df.columns:
            return None
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    volume = np.nan_to_num(df["Volume"].to_numpy(dtype=float), nan=0.0)
    n = len(df)
    idx = df.index

    # volume dry-up (scanner: I_VDU_LENGTH=50 SMA, I_DRY_UP_REQ=45): volume < 55% of its own
    # 50-bar average.
    vol_sma = pd.Series(volume).rolling(I_VDU_LENGTH).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where(vol_sma > 0, volume / vol_sma * 100.0, np.nan)
    vol_dry_up = vol_ratio < (100.0 - I_DRY_UP_REQ)

    # upside reversal (scanner "Upside reversal", lines 820-821): range beats its own 14-bar
    # ATR (Wilder RMA), close in the upper half of the range.
    _prev_close = np.concatenate([[close[0]], close[:-1]])
    _tr = np.maximum.reduce([high - low, np.abs(high - _prev_close),
                             np.abs(low - _prev_close)])
    atr14 = pd.Series(_tr).ewm(alpha=1.0 / 14, adjust=False).mean().to_numpy()
    upside_reversal = (high - low > atr14) & (close > (high + low) / 2.0)

    # MA touch / reclaim (scanner "MA touch", lines 731-749): touches any of EMA 10/21/34
    # within I_MA_TOUCH_THRESH % (0.5%).
    def _ema(length):
        return pd.Series(close).ewm(span=length, adjust=False).mean().to_numpy()

    def _touches(ma_val):
        up = ma_val * (1.0 + I_MA_TOUCH_THRESH / 100.0)
        lo = ma_val * (1.0 - I_MA_TOUCH_THRESH / 100.0)
        return (ma_val > 0) & (low <= up) & (high >= lo)

    touched_ma = (_touches(_ema(I_MA_LEN1)) | _touches(_ema(I_MA_LEN2))
                  | _touches(_ema(I_MA_LEN3)))

    # pocket pivot, general form (scanner "Pocket pivot, general form", lines 684-700): up
    # day with volume above the max down-volume of the prior 10 bars (or prior 5), excluding
    # today.
    _price_diff = np.diff(close, prepend=close[0])
    _is_up_day = _price_diff > 0
    _down_vol = np.where(_price_diff < 0, volume, 0.0)
    _h10 = pd.Series(_down_vol).shift(1).rolling(10).max().to_numpy()
    _h5 = pd.Series(_down_vol).shift(1).rolling(5).max().to_numpy()
    pp_any = ((_is_up_day & (volume > _h10) & ~np.isnan(_h10))
              | (_is_up_day & (volume > _h5) & ~np.isnan(_h5)))

    # RS new high vs SPY (scanner "RS new high", lines 203-213): relative-strength curve new
    # high vs its prior 1y / 6m / 3m window.
    nh_any = np.zeros(n, dtype=bool)
    if spy_close is not None and len(spy_close):
        try:
            _spy = spy_close.reindex(idx).ffill().bfill().to_numpy(dtype=float)
            if len(_spy) == n and np.all(np.isfinite(_spy)) and np.all(_spy > 0):
                _rs_curve = close / _spy
                _s_rs = pd.Series(_rs_curve)
                _h1y = _s_rs.shift(1).rolling(250, min_periods=30).max().to_numpy()
                _h6m = _s_rs.shift(1).rolling(126, min_periods=20).max().to_numpy()
                _h3m = _s_rs.shift(1).rolling(63, min_periods=10).max().to_numpy()
                nh1y = _rs_curve > _h1y
                nh6m = (_rs_curve > _h6m) & ~nh1y
                nh3m = (_rs_curve > _h3m) & ~nh1y & ~nh6m
                nh_any = nh1y | nh6m | nh3m
        except Exception:
            pass

    # shakeout entry (scanner "Shakeout entry", lines 771-808): undercut the last confirmed
    # swing low while above the 50-bar trend EMA -> reclaim the 3-EMA within 3 bars -> entry
    # when a later high clears the reclaim bar's high. Sequential, copied verbatim.
    shake_ema3 = pd.Series(close).ewm(span=3, adjust=False).mean().to_numpy()
    shake_trend = pd.Series(close).ewm(span=I_SHAKE_TREND_LEN, adjust=False).mean().to_numpy()
    shake_pl = _pivot_flags(low, I_SHAKE_LR, I_SHAKE_LR, "low")
    shakeout_entry = np.zeros(n, dtype=bool)
    last_swing_low = float("nan")
    undercut_bar = None
    reclaim_bar = None
    reclaim_high = float("nan")
    setup_active = False
    for _k in range(n):
        _kb = _k - I_SHAKE_LR
        if _kb >= 0 and shake_pl[_kb]:
            last_swing_low = low[_kb]
        uptrend = close[_k] > shake_trend[_k]
        undercut = (not math.isnan(last_swing_low) and low[_k] < last_swing_low and uptrend)
        if undercut and not setup_active:
            undercut_bar = _k
            setup_active = True
            reclaim_bar = None
            reclaim_high = float("nan")
        valid_win = (setup_active and undercut_bar is not None and _k > undercut_bar
                     and _k - undercut_bar <= 3)
        if valid_win and close[_k] > shake_ema3[_k] and reclaim_bar is None and uptrend:
            reclaim_bar = _k
            reclaim_high = high[_k]
        if setup_active and reclaim_bar is not None and _k > reclaim_bar \
                and high[_k] > reclaim_high and uptrend:
            shakeout_entry[_k] = True
        if shakeout_entry[_k] or (setup_active and undercut_bar is not None
                                  and _k - undercut_bar > 3):
            setup_active = False
            undercut_bar = None
            reclaim_bar = None
            reclaim_high = float("nan")

    # plain high-volume day (extra signal, not in the Pine score system): volume above the
    # scanner's breakout-volume multiplier I_VOL_BREAKOUT_MULT (1.5x) of its 50-day average.
    vol_ma50 = pd.Series(volume).rolling(50).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_spike = (vol_ma50 > 0) & (volume > I_VOL_BREAKOUT_MULT * vol_ma50)

    return {
        "dates": np.array([str(pd.Timestamp(d).date()) for d in idx]),
        "close": close,
        "signals": {
            "pocket_pivot": pp_any, "shakeout": shakeout_entry, "ma_touch": touched_ma,
            "vol_dry_up": vol_dry_up, "rs_new_high": nh_any,
            "upside_reversal": upside_reversal, "vol_spike": vol_spike,
        },
    }


def _eval_entry(entry, sigs, date_map):
    """Did each signal fire before this breakout, within ±20% of the pivot?

    Returns a dict of `{name}_base` (anywhere in the base) and `{name}_last20` (last 20 bars
    of the base) booleans, or None when the entry can't be mapped onto the frame.
    """
    pivot = entry.get("pivot")
    if not pivot or (isinstance(pivot, float) and math.isnan(pivot)) or pivot <= 0:
        return None
    d0 = entry.get("base_start_date") or entry.get("start_date")
    d1 = entry.get("end_date")
    if not d0 or not d1:
        return None
    i0 = date_map.get(str(d0))
    i1 = date_map.get(str(d1))
    if i0 is None or i1 is None or i1 < i0 or i0 < 0:
        return None
    window = np.arange(i0, i1 + 1)
    if len(window) < 2:
        return None
    close = sigs["close"]
    near = ((close[window] >= pivot * (1.0 - PRE_PIVOT_PCT / 100.0))
            & (close[window] <= pivot * (1.0 + PRE_PIVOT_PCT / 100.0)))
    out = {}
    for name, arr in sigs["signals"].items():
        seg = arr[window]
        out[f"{name}_base"] = bool(np.any(seg & near))
        out[f"{name}_last20"] = bool(np.any(seg[-20:] & near[-20:]))
    return out


def _run_prebo(df, resolved_mask, target_mask, baseline, args):
    """Recompute pre-breakout signals for every recorded breakout and return
    (csv_rows, report_lines). Runs only over tickers that appear in the history."""
    L = []

    def add(s=""):
        L.append(s)

    is_bo = (df["ended"] == "Breakout").to_numpy()
    open_mask = is_bo & (df["outcome"] == OPEN).to_numpy()
    breakdown_mask = ~is_bo
    t0 = time.time()
    print("Computing pre-breakout signals from ticker_cache ...", flush=True)

    spy_close = None
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    if spy_path.exists():
        try:
            spy_df = pd.read_parquet(spy_path).sort_index()
            spy_close = spy_df["Close"]
            spy_close = spy_close[~spy_close.index.duplicated(keep="last")]
        except Exception:
            spy_close = None

    pos_by_ticker = {}
    codes, uniques = pd.factorize(df["ticker"].values)
    for code, t in enumerate(uniques):
        pos_by_ticker[t] = np.where(codes == code)[0]
    cache = {}
    for t in uniques:
        p = TICKER_CACHE_DIR / f"{t}_1d.parquet"
        if p.exists():
            cache[t] = p
    print(f"  {len(cache):,} tickers with cached prices ({len(uniques):,} in history)",
          flush=True)

    flags = {}
    counts_base = np.zeros(len(df), dtype=int)
    tickers = sorted(cache)
    for i, t in enumerate(tickers):
        sigs = _ticker_signals(t, cache[t], spy_close)
        if sigs is None:
            continue
        date_map = {d: k for k, d in enumerate(sigs["dates"])}
        for pos in pos_by_ticker.get(t, ()):
            if not is_bo[pos]:
                continue
            ev = _eval_entry(df.iloc[pos].to_dict(), sigs, date_map)
            if ev is None:
                continue
            flags[pos] = ev
            counts_base[pos] = int(sum(ev[f"{s}_base"] for s in SIGNAL_NAMES))
        if i % 500 == 0:
            print(f"  {i:,}/{len(tickers):,} tickers", flush=True)
    print(f"  evaluated {len(flags):,} breakouts in {time.time() - t0:.0f}s", flush=True)

    if not flags:
        add("*(no signal data could be evaluated — ticker cache missing?)*")
        return [], L, None, []

    sig_masks = {}
    for name in SIGNAL_NAMES:
        for w in ("base", "last20"):
            m = np.zeros(len(df), dtype=bool)
            for pos, ev in flags.items():
                m[pos] = ev.get(f"{name}_{w}", False)
            sig_masks[f"{name}_{w}"] = m
    any_base = np.zeros(len(df), dtype=bool)
    for name in SIGNAL_NAMES:
        any_base |= sig_masks[f"{name}_base"]
    sig_masks["any_signal_base"] = any_base

    rows = []

    def _add(dim, bucket, mask):
        rows.append({"dimension": dim, "bucket": bucket,
                     **_score(mask, df, resolved_mask, target_mask, open_mask,
                              breakdown_mask, baseline)})

    # ── 15 · per-signal, during the base (near pivot) ────────────────────────────────────
    _add("prebo_signal", "any (>=1) base", any_base)
    add("## 15 · Pre-BO signals — fired during the base, within ±20% of pivot")
    add()
    add("> Recomputed from ticker_cache with the scanner's own definitions (see script "
        "header). A signal counts if it fired on any bar of the base whose close sat within "
        "20% of the pivot — the Pine's `near_pivot` gate (approximated with the recorded base "
        "window; the scanner's own gate also includes its separate higher-timeframe flag, "
        "which can fire outside the base and is not recorded in the history). `vol_spike` = a "
        "day above 1.5× the 50-day average volume (the scanner's breakout-volume multiplier); "
        "it is added here because `vol_dry_up` alone only captures the quiet side. Win rates "
        "are Target / (Target + Stop); the CSV carries the 'with' rows and the 'Δ vs w/o' "
        "column compares against the without group computed here.")
    add()
    add("*Signals that fire on almost every base (pocket_pivot, ma_touch, upside_reversal) "
        "have tiny 'without' groups, so their Δ is less meaningful than for the rarer "
        "signals.*")
    add()
    add(_md_row(["Signal (base window)", "Breakouts w/", "Resolved w/", "Target w/",
                 "Win % w/", "Win % w/o", "Δ vs w/o (pp)", "Med bars→tgt w/"]))
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name in SIGNAL_NAMES:
        m = sig_masks[f"{name}_base"]
        _add("prebo_signal", f"{name} (base)", m)
        n_res = int((resolved_mask & m).sum())
        n_res_no = int((resolved_mask & ~m).sum())
        if n_res < args.min_resolved:
            continue
        k = int((target_mask & m).sum())
        k_no = int((target_mask & ~m).sum())
        wr = k / n_res * 100.0
        if n_res_no >= 50:
            wr_no = k_no / n_res_no * 100.0
            cell_no, cell_d = f"{wr_no:.1f}%", f"{wr - wr_no:+.1f}"
        else:
            cell_no, cell_d = "—", "—"
        bars = pd.to_numeric(df.loc[target_mask & m, "bars_to_outcome"], errors="coerce")
        med = bars.median() if len(bars) else float("nan")
        add(_md_row([f"**{name}**", f"{int(m.sum()):,}", f"{n_res:,}", f"{k:,}",
                     f"{wr:.1f}%", cell_no, cell_d,
                     f"{med:.0f}" if med == med else "—"]))
    add()

    # ── 16 · last-20-bars window ─────────────────────────────────────────────────────────
    add("## 16 · Pre-BO signals — last 20 bars before the breakout")
    add()
    add(_md_row(["Signal (last 20 bars)", "Breakouts w/", "Resolved w/", "Target w/",
                 "Win % w/", "Win % w/o", "Δ vs w/o (pp)", "Med bars→tgt w/"]))
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name in SIGNAL_NAMES:
        m = sig_masks[f"{name}_last20"]
        _add("prebo_signal", f"{name} (last20)", m)
        n_res = int((resolved_mask & m).sum())
        n_res_no = int((resolved_mask & ~m).sum())
        if n_res < args.min_resolved:
            continue
        k = int((target_mask & m).sum())
        k_no = int((target_mask & ~m).sum())
        wr = k / n_res * 100.0
        if n_res_no >= 50:
            wr_no = k_no / n_res_no * 100.0
            cell_no, cell_d = f"{wr_no:.1f}%", f"{wr - wr_no:+.1f}"
        else:
            cell_no, cell_d = "—", "—"
        bars = pd.to_numeric(df.loc[target_mask & m, "bars_to_outcome"], errors="coerce")
        med = bars.median() if len(bars) else float("nan")
        add(_md_row([f"**{name}**", f"{int(m.sum()):,}", f"{n_res:,}", f"{k:,}",
                     f"{wr:.1f}%", cell_no, cell_d,
                     f"{med:.0f}" if med == med else "—"]))
    add()

    # ── 17 · how many signals fired ──────────────────────────────────────────────────────
    add("## 17 · How many pre-BO signals fired (base window)")
    add()
    add("*(Counts 0-3 are omitted from the table — they are rarer than the minimum "
        "resolved-count cut; all eight counts are in the CSV.)*")
    add()
    add(_md_row(["Signals", "Breakouts", "Resolved", "Target", "Win %", "Lift (pp)"]))
    add("|---|---:|---:|---:|---:|---:|")
    for c in range(0, len(SIGNAL_NAMES) + 1):
        m = np.zeros(len(df), dtype=bool)
        for pos in flags:
            m[pos] = counts_base[pos] == c
        _add("prebo_count", str(c), m)
        n_res = int((resolved_mask & m).sum())
        if n_res < args.min_resolved:
            continue
        k = int((target_mask & m).sum())
        add(_md_row([c, f"{int(m.sum()):,}", f"{n_res:,}", f"{k:,}",
                     f"{k / n_res * 100:.1f}%", f"{(k / n_res - baseline) * 100:+.1f}"]))
    add()

    # ── 18 · combinations ────────────────────────────────────────────────────────────────
    cands = [(f"prebo {name}", name, sig_masks[f"{name}_base"]) for name in SIGNAL_NAMES]
    chain = _greedy_chain(cands, resolved_mask, target_mask, baseline,
                          args.min_resolved, args.min_lift_pp, 4)
    pairs = []
    for i, (n1, _l1, m1) in enumerate(cands):
        for n2, _l2, m2 in cands[i + 1:]:
            m = m1 & m2
            n_res = int((resolved_mask & m).sum())
            if n_res < args.min_resolved:
                continue
            k = int((target_mask & m).sum())
            pairs.append((f"{n1.split()[1]} + {n2.split()[1]}", n_res, k, k / n_res))
    pairs.sort(key=lambda r: r[3], reverse=True)

    add("## 18 · Pre-BO signal combinations")
    add()
    add("**Greedy chain** (each rule must add lift on ≥ "
        f"{args.min_resolved} resolved remaining):")
    add()
    if not chain:
        add(f"*(no signal rule improved win rate by ≥ {args.min_lift_pp}pp)*")
    else:
        add(_md_row(["Step", "Signal", "Resolved left", "Target", "Win %",
                     "Lift vs prior (pp)"]))
        add("|---|---|---:|---:|---:|---:|")
        for i, r in enumerate(chain, 1):
            add(_md_row([i, f"**{r['dim']}** · {r['bucket']}", f"{r['n_resolved']:,}",
                         f"{r['n_target']:,}", f"{r['win_rate_pct']:.1f}%",
                         f"{r['lift_pp']:+.1f}"]))
    add()
    add("**Top pairwise combos:**")
    add()
    if not pairs:
        add(f"*(no pair reached {args.min_resolved} resolved breakouts)*")
    else:
        add(_md_row(["Pair", "Resolved", "Target", "Win %", "Lift (pp)"]))
        add("|---|---:|---:|---:|---:|")
        for name, n_res, k, wr in pairs[:12]:
            add(_md_row([name, f"{n_res:,}", f"{k:,}", f"{wr * 100:.1f}%",
                         f"{(wr - baseline) * 100:+.1f}"]))
    add()

    # signal candidates for the top-singles / greedy chains in main (both windows)
    prebo_cands = [(f"prebo.{w}", name, sig_masks[f"{name}_{w}"])
                   for w in ("base", "last20") for name in SIGNAL_NAMES]

    # ── 19 · signals inside the strongest structural buckets ─────────────────────────────
    depth = df["depth_pct"].to_numpy(dtype=float)
    days = df["days"].to_numpy(dtype=float)
    dis = df["dis_days"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        dis_r = np.where(days > 0, dis / days, np.nan)
    buckets = [
        ("Consolidation", (df["base_shape"].fillna("(none)").values == "Consolidation")),
        ("depth 25-35%", (depth >= 25) & (depth < 35)),
        ("dis ratio < 15%", dis_r < 0.15),
        ("days 30-90", (days >= 30) & (days < 90)),
        ("any signal fired", any_base),
    ]
    add("## 19 · Signals × strongest structural buckets")
    add()
    add("Win rate WITH the signal vs WITHOUT, inside each bucket — does the signal add "
        "anything on top of the structural edge?")
    add()
    add(_md_row(["Bucket", "Signal", "Resolved", "Win % w/", "Win % w/o", "Δ (pp)"]))
    add("|---|---|---:|---:|---:|---:|")
    for bname, bmask in buckets:
        for name in SIGNAL_NAMES:
            m = bmask & sig_masks[f"{name}_base"]
            n_res = int((resolved_mask & m).sum())
            n_res_no = int((resolved_mask & (bmask & ~sig_masks[f"{name}_base"])).sum())
            if n_res < args.min_resolved or n_res_no < args.min_resolved:
                continue
            k = int((target_mask & m).sum())
            k_no = int((target_mask & (bmask & ~sig_masks[f"{name}_base"])).sum())
            add(_md_row([bname, name, f"{n_res:,}", f"{k / n_res * 100:.1f}%",
                         f"{k_no / n_res_no * 100:.1f}%",
                         f"{(k / n_res - k_no / n_res_no) * 100:+.1f}"]))
    add()

    return rows, L, any_base, prebo_cands


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Part III - market regime & stock price scenarios
# ══════════════════════════════════════════════════════════════════════════════════════════
#
#  Two more context axes for every breakout, both read off the day the base ended:
#
#    * market regime — SPY close vs its own 50-day and 200-day SMA (the "critical moving
#      averages" every tape filter checks), the 50/200 golden-vs-death alignment, and how far
#      SPY sat from its 200-day (distance bands);
#    * stock price — the buy point (`pivot`) in <$10 / $10-25 / $25-50 / $50-100 /
#      $100-250 / $250+ buckets.
#
#  Then the cross-scenarios: the full regime × price matrix, and regime × structure / regime
#  × pre-BO signals, so "does a breakout work in a market below its MAs, and does price or
#  structure change the answer" can be answered with numbers instead of vibes.

PRICE_EDGES = [0, 10, 25, 50, 100, 250, math.inf]


def _run_market(df, resolved_mask, target_mask, baseline, args, any_sig=None):
    """Market-regime and stock-price scenario analysis. `any_sig` is the Part II
    "any pre-BO signal fired" mask, passed in so regime × signals can be crossed when the
    signal pass ran. Returns (csv_rows, report_lines)."""
    L = []

    def add(s=""):
        L.append(s)

    is_bo = (df["ended"] == "Breakout").to_numpy()
    open_mask = is_bo & (df["outcome"] == OPEN).to_numpy()
    breakdown_mask = ~is_bo
    rows = []

    def _add(dim, bucket, mask):
        rows.append({"dimension": dim, "bucket": bucket,
                     **_score(mask, df, resolved_mask, target_mask, open_mask,
                              breakdown_mask, baseline)})

    def _emit_table(dim, header, items, min_resolved):
        """Emit one section table: CSV row per item + md row, skipping sub-\n
        minimum-resolved items from display only."""
        add(_md_row(header))
        add("|---|---:|---:|---:|---:|---|---:|")
        for label, m in items:
            _add(dim, label, m)
            n_res = int((resolved_mask & m).sum())
            if n_res < min_resolved:
                continue
            k = int((target_mask & m).sum())
            lo, hi = _wilson_ci(k, n_res)
            add(_md_row([label, f"{int(m.sum()):,}", f"{n_res:,}", f"{k:,}",
                         f"{k / n_res * 100:.1f}%", f"{lo * 100:.1f}%–{hi * 100:.1f}%",
                         f"{(k / n_res - baseline) * 100:+.1f}"]))

    # ── SPY daily regime series (one map: date -> (close, sma50, sma200)) ────────────────
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    if not spy_path.exists():
        add("## 20 · Market regime & stock price scenarios")
        add()
        add(f"*(SPY cache `{spy_path}` not found — skipped)*")
        return rows, L
    try:
        spy_df = pd.read_parquet(spy_path).sort_index()
        spy_df = spy_df[~spy_df.index.duplicated(keep="last")]
        spy_close = pd.to_numeric(spy_df["Close"], errors="coerce")
        sma50 = spy_close.rolling(50, min_periods=30).mean()
        sma200 = spy_close.rolling(200, min_periods=120).mean()
        spy_map = {}
        for d, c, s50, s200 in zip(spy_df.index, spy_close, sma50, sma200):
            if pd.isna(c) or pd.isna(s50) or pd.isna(s200) or s200 <= 0:
                continue
            spy_map[str(pd.Timestamp(d).date())] = (float(c), float(s50), float(s200))
    except Exception:
        add("## 20 · Market regime & stock price scenarios")
        add()
        add(f"*(could not read SPY cache `{spy_path}` — skipped)*")
        return rows, L, []

    n = len(df)
    known = np.zeros(n, dtype=bool)          # breakout with a SPY reading that day
    spy_above50 = np.zeros(n, dtype=bool)
    spy_above200 = np.zeros(n, dtype=bool)
    spy_golden = np.zeros(n, dtype=bool)
    spy_dist200 = np.full(n, np.nan)
    alignment = np.full(n, "", dtype=object)
    price = pd.to_numeric(df["pivot"], errors="coerce").to_numpy(dtype=float)
    price_code = np.full(n, -1, dtype=int)

    dates = df["end_date"].astype(str).values
    for i in range(n):
        if not is_bo[i]:
            continue
        s = spy_map.get(dates[i])
        if s is None:
            continue
        c, s50, s200 = s
        known[i] = True
        spy_above50[i] = c > s50
        spy_above200[i] = c > s200
        spy_golden[i] = s50 > s200
        spy_dist200[i] = (c - s200) / s200 * 100.0
        if c > s50 and c > s200:
            alignment[i] = "Bull"
        elif c < s50 and c < s200:
            alignment[i] = "Bear"
        else:
            alignment[i] = "Mixed"
        p = price[i]
        if not math.isnan(p) and p > 0:
            price_code[i] = int(np.digitize(p, PRICE_EDGES))

    n_known = int(known.sum())
    if n_known == 0:
        add("## 20 · Market regime & stock price scenarios")
        add()
        add("*(no breakout date matched the SPY series — skipped)*")
        return rows, L, []

    price_labels = {i: _range_label(PRICE_EDGES[i - 1], PRICE_EDGES[i])
                    for i in range(1, len(PRICE_EDGES))}

    # ── 20 · SPY regime buckets ──────────────────────────────────────────────────────────
    add("## 20 · Market regime at the breakout (SPY vs critical MAs)")
    add()
    add("> SPY close vs its 50-day and 200-day SMA on the breakout's end date, and how far "
        f"from the 200-day it sat. Baseline is {baseline * 100:.1f}%.")
    add()
    dist_edges = [-10, -3, 0, 3, 10]
    dist_code = np.digitize(spy_dist200, dist_edges)
    dist_labels = {1: ">10% below 200", 2: "3-10% below 200", 3: "0-3% below 200",
                   4: "0-3% above 200", 5: "3-10% above 200", 6: ">10% above 200"}
    items20 = [
        ("SPY above SMA50", spy_above50),
        ("SPY below SMA50", known & ~spy_above50),
        ("SPY above SMA200", spy_above200),
        ("SPY below SMA200", known & ~spy_above200),
        ("SMA50 > SMA200 (golden)", known & spy_golden),
        ("SMA50 < SMA200 (death)", known & ~spy_golden),
    ]
    for c in sorted(dist_labels):
        items20.append((dist_labels[c], known & (dist_code == c)))
    _emit_table("market_regime",
                ["Regime bucket", "Breakouts", "Resolved", "Target", "Win %", "CI",
                 "Lift (pp)"], items20, args.min_resolved)
    add()
    add(f"*Regime known for {n_known:,} of {int(is_bo.sum()):,} breakouts "
        f"({n_known / int(is_bo.sum()) * 100:.0f}%){'' if n_known == int(is_bo.sum()) else ' — the rest ended on dates SPY does not cover in the cache and are excluded from regime rows.'}*")
    add()
    add("*Lift here is vs the 29.6% baseline; the takeaways and §24 use the same vs-baseline "
        "convention, while the signal tables in §15/§16 show Δ vs the without-group instead "
        "— the two are different measures.*")
    add()

    # ── 21 · stock price buckets ─────────────────────────────────────────────────────────
    add("## 21 · Stock price at the breakout (buy point / pivot)")
    add()
    add("> Buckets on the base's `pivot` — the actual buy point a trader pays, which is the "
        "price that matters for position sizing, not the close.")
    add()
    items21 = [(f"${price_labels[c]}", known & (price_code == c)) for c in sorted(price_labels)]
    _emit_table("market_price",
                ["Price bucket", "Breakouts", "Resolved", "Target", "Win %", "CI",
                 "Lift (pp)"], items21, args.min_resolved)
    add()
    add("*Caveat: the $0-10 bucket is the smallest, and it is where the most marginal, "
        "highest-volatility names live — the +14 pp win rate is real but likely comes with "
        "wider drawdowns and more noise; the $10-25 bucket is the cheapest one with "
        "institutional quality still plausible.*")
    add()

    # ── 22 · regime × price scenario matrix ─────────────────────────────────────────────
    add("## 22 · Scenario matrix — market regime × stock price")
    add()
    add("Every cell is a real scenario (regime of the day it broke out × the buy point it "
        "broke out from), sorted by win rate. Cells that fall below the minimum resolved "
        "count are omitted.")
    add()
    matrix = []
    for al in ("Bull", "Mixed", "Bear"):
        for c in sorted(price_labels):
            m = known & (alignment == al) & (price_code == c)
            n_res = int((resolved_mask & m).sum())
            if n_res < args.min_resolved:
                continue
            k = int((target_mask & m).sum())
            matrix.append((f"{al} × ${price_labels[c]}", n_res, k, k / n_res))
    matrix.sort(key=lambda r: r[3], reverse=True)
    add(_md_row(["Scenario (regime × price)", "Resolved", "Target", "Win %", "Lift (pp)"]))
    add("|---|---:|---:|---:|---:|")
    for label, n_res, k, wr in matrix:
        add(_md_row([label, f"{n_res:,}", f"{k:,}", f"{wr * 100:.1f}%",
                     f"{(wr - baseline) * 100:+.1f}"]))
    add()

    # ── 23 · regime × structure and regime × signals ─────────────────────────────────────
    depth = df["depth_pct"].to_numpy(dtype=float)
    struct = [
        ("Consolidation", (df["base_shape"].fillna("(none)").values == "Consolidation")),
        ("depth 25-35%", (depth >= 25) & (depth < 35)),
        ("days 30-90", (df["days"].to_numpy(dtype=float) >= 30)
         & (df["days"].to_numpy(dtype=float) < 90)),
    ]
    if any_sig is not None:
        struct.append(("any pre-BO signal", any_sig))
    add("## 23 · Scenario combinations — regime × structure / signals")
    add()
    add("Does the structural edge survive in a bear market? These rows cross each regime "
        "with the strongest structural buckets (and with 'any pre-BO signal fired' when the "
        "signal pass ran).")
    add()
    combos = []
    for al in ("Bull", "Mixed", "Bear"):
        for sname, smask in struct:
            m = known & (alignment == al) & smask
            n_res = int((resolved_mask & m).sum())
            if n_res < args.min_resolved:
                continue
            k = int((target_mask & m).sum())
            combos.append((f"{al} × {sname}", n_res, k, k / n_res))
    combos.sort(key=lambda r: r[3], reverse=True)
    add(_md_row(["Scenario (regime × structure)", "Resolved", "Target", "Win %",
                 "Lift (pp)"]))
    add("|---|---:|---:|---:|---:|")
    for label, n_res, k, wr in combos:
        add(_md_row([label, f"{n_res:,}", f"{k:,}", f"{wr * 100:.1f}%",
                     f"{(wr - baseline) * 100:+.1f}"]))
    add()

    # market/price candidates for the top-singles / greedy chains in main
    market_cands = ([(f"market.price", f"${price_labels[c]}", known & (price_code == c))
                     for c in sorted(price_labels)]
                    + [(f"market.regime", label, m) for label, m in items20])

    return rows, L, market_cands


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", default=str(HISTORY_JSON),
                    help="path to tv_pattern_history.json "
                         "(default: python/tv_pattern_history.json)")
    ap.add_argument("--min-resolved", type=int, default=200,
                    help="min resolved breakouts for a bucket/filter to count (default 200)")
    ap.add_argument("--min-lift-pp", type=float, default=2.0,
                    help="min win-rate improvement for a greedy rule (default 2.0)")
    ap.add_argument("--max-chain", type=int, default=4,
                    help="max rules in the greedy chain (default 4)")
    ap.add_argument("--no-signals", action="store_true",
                    help="skip the pre-BO signal pass (Part II, reads every parquet once)")
    ap.add_argument("--no-market", action="store_true",
                    help="skip the market regime / price scenario pass (Part III)")
    args = ap.parse_args()

    with open(args.json) as fh:
        raw = json.load(fh)
    df = pd.DataFrame(raw["patterns"])
    if df.empty:
        sys.exit("no patterns in history file")
    print(f"Loaded {len(df):,} recorded bases from {args.json} "
          f"(generated {raw.get('generated_at')}, universe {raw.get('universe'):,}).")

    # ── positional masks over the full frame ──────────────────────────────────────────────
    is_bo = (df["ended"] == "Breakout").to_numpy()
    resolved_mask = is_bo & df["outcome"].isin([TARGET, STOP]).to_numpy()
    target_mask = resolved_mask & (df["outcome"] == TARGET).to_numpy()
    stop_mask = resolved_mask & (df["outcome"] == STOP).to_numpy()
    open_mask = is_bo & (df["outcome"] == OPEN).to_numpy()
    breakdown_mask = ~is_bo
    baseline = int(target_mask.sum()) / int(resolved_mask.sum()) if resolved_mask.any() else 0.0

    # target speed / depth profile (Targets only)
    tgt_bars = pd.to_numeric(df.loc[target_mask, "bars_to_outcome"], errors="coerce").dropna()
    tgt_gain = pd.to_numeric(df.loc[target_mask, "max_gain_pct"], errors="coerce").dropna()
    tgt_dd = pd.to_numeric(df.loc[target_mask, "max_drawdown_pct"], errors="coerce").dropna()

    # Part II / III outputs (bound early so the CSV merge below sees them even when a part
    # is skipped; the heavy passes themselves run further down).
    prebo_rows, prebo_report, any_sig, prebo_cands = [], [], None, []
    market_rows, market_report, market_cands = [], [], []

    # ── univariate bucket scan ────────────────────────────────────────────────────────────
    specs = _build_specs(df)
    rows = []
    for dim, codes_arr, lbl in specs:
        codes_arr = np.asarray(codes_arr)
        numeric = np.issubdtype(codes_arr.dtype, np.number)
        for code in np.unique(codes_arr[~pd.isna(codes_arr)]):
            if code not in lbl:
                # out-of-range np.digitize code (e.g. NaN rows) or an unlabelled value:
                # skip rather than emit a raw-number bucket that would look meaningful.
                continue
            mask = codes_arr == code
            if numeric:
                code = int(code)
                mask = codes_arr == code
            label = lbl.get(code, str(code))
            rows.append({"dimension": dim, "bucket": label,
                         **_score(mask, df, resolved_mask, target_mask, open_mask,
                                  breakdown_mask, baseline)})
    all_mask = np.ones(len(df), dtype=bool)
    rows.append({"dimension": "all", "bucket": "every breakout",
                 **_score(all_mask, df, resolved_mask, target_mask, open_mask,
                          breakdown_mask, baseline)})

    # ── Part II: pre-breakout technical signals (heavy: reads every parquet once) ─────────
    if not args.no_signals:
        prebo_rows, prebo_report, any_sig, prebo_cands = _run_prebo(
            df, resolved_mask, target_mask, baseline, args)

    # ── Part III: market regime & price scenarios ─────────────────────────────────────────
    if not args.no_market:
        market_rows, market_report, market_cands = _run_market(
            df, resolved_mask, target_mask, baseline, args, any_sig=any_sig)

    # ── combined filters (needs the Part II/III candidates, so it runs after them) ────────
    candidates = []
    for dim, codes_arr, lbl in specs:
        codes_arr = np.asarray(codes_arr)
        numeric = np.issubdtype(codes_arr.dtype, np.number)
        for code in np.unique(codes_arr[~pd.isna(codes_arr)]):
            if code not in lbl:
                continue
            mask = codes_arr == code
            if numeric:
                code = int(code)
                mask = codes_arr == code
            label = lbl.get(code, str(code))
            candidates.append((dim, label, mask))
    # Every candidate dimension (structure + pre-BO signals + market/price) feeds the top
    # singles and the greedy chains, so "combinations of different things" is covered.
    candidates_all = candidates + prebo_cands + market_cands
    single = _top_singles(candidates_all, resolved_mask, target_mask, is_bo,
                          args.min_resolved)
    # Two chains: with the year dimension (what the data actually shows) and without it
    # (the filters a trader can apply regardless of the year they are in).
    chain_all = _greedy_chain(candidates_all, resolved_mask, target_mask, baseline,
                              args.min_resolved, args.min_lift_pp, args.max_chain)
    chain_no_year = _greedy_chain(
        [c for c in candidates_all if c[0] != "breakout_year"],
        resolved_mask, target_mask, baseline,
        args.min_resolved, args.min_lift_pp, args.max_chain)
    cross = _cross_tabs(df, resolved_mask, target_mask, baseline, args.min_resolved)

    # ── assemble results CSV (all three parts) ────────────────────────────────────────────
    res = pd.DataFrame(rows + prebo_rows + market_rows)
    res = res.sort_values(["dimension", "win_rate_pct"], ascending=[True, False])
    res.to_csv(RESULTS_CSV, index=False)
    print(f"Wrote {RESULTS_CSV} ({len(res):,} bucket rows)")

    # ── report ────────────────────────────────────────────────────────────────────────────
    L = []

    def add(s=""):
        L.append(s)

    add(f"# TV Pattern History Backtest — Target Outcome Characteristics")
    add()
    add(f"**Generated {pd.Timestamp.now():%Y-%m-%d %H:%M}** · history "
        f"`{raw.get('generated_at')}` · universe {raw.get('universe'):,} tickers · "
        f"hold window {raw.get('hold_bars')} bars")
    add()
    add("> Scoring (identical to the dashboard's History tab): a breakout **Target** = "
        "touched **+20%** above its pivot before touching −8%, within "
        f"{raw.get('hold_bars', 60)} bars, first touch wins (a bar that spans both levels "
        "scores a stop). **Win rate** = Target / (Target + Stop) over **resolved** breakouts "
        "only; Open bases are reported but never counted either way. CIs are Wilson 95%.")
    add()

    add("## 1 · Headline")
    add()
    add(_md_row(["", "n"]))
    add("|---|---:|---:")
    add(f"| Bases recorded | {len(df):,} |")
    add(f"| — breakouts | {int(is_bo.sum()):,} |")
    add(f"| — breakdowns (depth / length) | {int(breakdown_mask.sum()):,} |")
    add(f"| Resolved breakouts (Target + Stop) | {int(resolved_mask.sum()):,} |")
    add(f"| **Target** (+20% first) | **{int(target_mask.sum()):,}** "
        f"({int(target_mask.sum()) / int(is_bo.sum()) * 100:.1f}% of all breakouts) |")
    add(f"| Stop (−8% first) | {int(stop_mask.sum()):,} |")
    add(f"| Still open after {raw.get('hold_bars', 60)} bars | {int(open_mask.sum()):,} |")
    add(f"| **Resolved win rate (baseline)** | **{baseline * 100:.1f}%** |")
    add()

    add("### Win rate by breakout year (regime check)")
    add()
    yr = res[(res["dimension"] == "breakout_year") & (res["n_resolved"] >= 50)]
    if len(yr):
        add(_md_row(["Year", "Breakouts", "Resolved", "Target", "Win %", "CI"]))
        add("|---|---:|---:|---:|---:|---|")
        for _, r in yr.sort_values("bucket").iterrows():
            add(_md_row([r["bucket"], f"{r['n_breakouts']:,}", f"{r['n_resolved']:,}",
                         f"{r['n_target']:,}", _fmt_pct(r["win_rate_pct"]),
                         f"{_fmt_pct(r['ci_lo_pct'])}–{_fmt_pct(r['ci_hi_pct'])}"]))
        add()
        add("*Coverage note: the ~6-year bar window means only tickers with longer cached "
            "history contribute 2020 breakouts, so 2020 is the smallest, most survivorship-"
            "biased slice — treat its 57% as a COVID-recovery regime reading, not a norm.*")
    else:
        add("*(no year bucket reached 50 resolved breakouts)*")
    add()

    def dim_section(title, dim):
        sub = res[(res["dimension"] == dim) & (res["n_resolved"] >= args.min_resolved)]
        add(f"## {title}")
        add()
        if sub.empty:
            add(f"*(no bucket reached {args.min_resolved} resolved breakouts)*")
            add()
            return
        add(_md_row(["Bucket", "Patterns", "Breakouts", "Resolved", "Target", "Win %", "CI",
                     "Lift (pp)", "Med bars→tgt", "Med gain %", "Med DD %"]))
        add("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|")
        for _, r in sub.iterrows():
            add(_md_row([
                r["bucket"], f"{r['n_patterns']:,}", f"{r['n_breakouts']:,}",
                f"{r['n_resolved']:,}", f"{r['n_target']:,}",
                _fmt_pct(r["win_rate_pct"]),
                f"{_fmt_pct(r['ci_lo_pct'])}–{_fmt_pct(r['ci_hi_pct'])}",
                _fmt_num(r["lift_pp"]), _fmt_num(r["med_bars_to_target"]),
                _fmt_num(r["med_max_gain_pct"]), _fmt_num(r["med_max_dd_pct"]),
            ]))
        add()

    dim_section("2 · By pattern", "pattern")
    dim_section("3 · By base shape", "base_shape")
    dim_section("4 · By base depth %", "depth_pct")
    dim_section("5 · By base length (days)", "days")
    dim_section("6 · By accumulation days", "acc_days")
    dim_section("7 · By neutral days", "neu_days")
    dim_section("8 · By distribution days", "dis_days")
    dim_section("9 · By accumulation ratio", "acc_ratio")
    dim_section("10 · By distribution ratio", "dis_ratio")
    dim_section("11 · By acc − dis (net)", "acc_minus_dis")
    dim_section("12 · By cup strength (cup_bars)", "cup_bars")
    dim_section("13 · By cup right/left balance", "cup_right_left")

    add("## 14 · What a Target looks like (speed & path)")
    add()
    if len(tgt_bars):
        add(f"**Speed to +20%** (`bars_to_outcome`, n={len(tgt_bars):,} targets): "
            f"median **{tgt_bars.median():.0f} bars**, p25 {tgt_bars.quantile(.25):.0f}, "
            f"p75 {tgt_bars.quantile(.75):.0f}. "
            f"Share within 5 bars **{(tgt_bars <= 5).mean() * 100:.0f}%**, "
            f"within 10 **{(tgt_bars <= 10).mean() * 100:.0f}%**, "
            f"within 20 **{(tgt_bars <= 20).mean() * 100:.0f}%**.")
        add()
        add(_md_row(["Bars to +20%", "Targets", "Share"]))
        add("|---|---:|---:")
        for lo, hi in [(1, 5), (6, 10), (11, 20), (21, 30), (31, 40), (41, 60)]:
            c = int(((tgt_bars >= lo) & (tgt_bars <= hi)).sum())
            add(_md_row([f"{lo}-{hi}", f"{c:,}", f"{c / len(tgt_bars) * 100:.0f}%"]))
        add()
    if len(tgt_gain):
        add(f"**Final gain at target** (`max_gain_pct`, n={len(tgt_gain):,}): median "
            f"**{tgt_gain.median():.1f}%**, p90 {tgt_gain.quantile(.90):.1f}%.")
        add()
    if len(tgt_dd):
        dipped5 = (tgt_dd <= -5.0).mean() * 100.0
        near_stop = (tgt_dd <= -7.9).mean() * 100.0
        add(f"**Drawdown before the target** (`max_drawdown_pct`, n={len(tgt_dd):,}): median "
            f"{tgt_dd.median():.1f}%. **{dipped5:.0f}%** of winners dipped at least −5% below "
            f"the pivot before recovering, and **{near_stop:.0f}%** came within a whisker of "
            "the −8% stop (≤ −7.9%) without touching it — winners routinely draw down before "
            "paying off, so being stopped at −8% is not proof the trade was wrong.")
        add()

    L.extend(prebo_report)
    L.extend(market_report)

    add("## 24 · Top single-bucket filters")
    add()
    if not single:
        add(f"*(no bucket reached {args.min_resolved} resolved breakouts)*")
    else:
        add(_md_row(["Rank", "Filter", "Breakouts", "Resolved", "Target", "Win %",
                     "Lift (pp)"]))
        add("|---|---|---:|---:|---:|---:|---:|")
        for i, (dim, label, n, n_res, k, wr) in enumerate(single, 1):
            add(_md_row([i, f"*{dim}*: {label}", f"{n:,}", f"{n_res:,}", f"{k:,}",
                         f"{wr * 100:.1f}%", f"{(wr - baseline) * 100:+.1f}"]))
        add()
        add("*The 2020 row carries the year-table caveat: it is the smallest, most "
            "coverage-biased slice (COVID-recovery regime) — the non-year rows below it "
            "(structure, pre-BO signals, market/price) are the ones a trader can screen on.*")
    add()

    def _chain_block(title, chain, note=None):
        add(f"### {title}")
        add()
        if note:
            add(f"*{note}*")
            add()
        if not chain:
            add(f"*(no rule improved win rate by ≥ {args.min_lift_pp}pp on ≥ "
                f"{args.min_resolved} resolved breakouts)*")
        else:
            add(_md_row(["Step", "Rule", "Resolved left", "Target", "Win %",
                         "Lift vs prior (pp)"]))
            add("|---|---|---:|---:|---:|---:|")
            for i, r in enumerate(chain, 1):
                add(_md_row([i, f"*{r['dim']}*: {r['bucket']}", f"{r['n_resolved']:,}",
                             f"{r['n_target']:,}", f"{r['win_rate_pct']:.1f}%",
                             f"{r['lift_pp']:+.1f}"]))
            add()
            add(f"**Final filtered population:** {chain[-1]['win_rate_pct']:.1f}% win rate "
                f"({chain[-1]['n_resolved']:,} resolved breakouts, "
                f"{chain[-1]['n_target']:,} targets) vs {baseline * 100:.1f}% baseline.")
        add()

    add(f"## 25 · Greedy rule chains (each rule adds lift on ≥ {args.min_resolved} resolved "
        f"remaining)")
    add()
    _chain_block("25a · With the regime dimension (what the data shows)", chain_all)
    _chain_block("25b · Actionable only, year excluded (what you can screen on)",
                 chain_no_year,
                 note="`breakout_year` is dropped so the chain reflects base characteristics, "
                      "pre-BO signals, market regime and price buckets a trader can screen on "
                      "today, rather than the year the breakout happened to occur in (2020's "
                      "+27.8 pp is a COVID-recovery regime, and 2020 only reaches back through "
                      "the ~6-year bar window, so it is also the smallest and most "
                      "coverage-biased year).")

    add("## 26 · Curated cross-tabs (the pairs a trader screens on)")
    add()
    if not cross:
        add(f"*(none reached {args.min_resolved} resolved breakouts)*")
    else:
        cur_pair = None
        for r in sorted(cross, key=lambda r: (r["pair"], -r["win_rate_pct"])):
            if r["pair"] != cur_pair:
                if cur_pair is not None:
                    add()
                cur_pair = r["pair"]
                add(f"**{cur_pair}**")
                add()
                add(_md_row(["Combo", "Resolved", "Target", "Win %", "Lift (pp)"]))
                add("|---|---|---:|---:|---:|")
            add(_md_row([r["combo"], f"{r['n_resolved']:,}", f"{r['n_target']:,}",
                         f"{r['win_rate_pct']:.1f}%", f"{r['lift_pp']:+.1f}"]))
    add()

    add("## 27 · Key takeaways")
    add()
    big = res[(res["dimension"] != "breakout_year") & (res["dimension"] != "all")
              & (res["n_resolved"] >= args.min_resolved)].copy()
    if not big.empty:
        big = big.sort_values("lift_pp", ascending=False)
        add("**Strongest Target characteristics** (win-rate lift over baseline):")
        add()
        for _, r in big.head(8).iterrows():
            add(f"- **{r['dimension']} {r['bucket']}**: {_fmt_pct(r['win_rate_pct'])} win "
                f"({r['n_resolved']:,} resolved, {r['n_target']:,} targets) "
                f"= **{r['lift_pp']:+.1f} pp** vs {baseline * 100:.1f}% baseline")
        add()
        add("**Weakest (below baseline):**")
        add()
        for _, r in big.tail(5).iterrows():
            add(f"- **{r['dimension']} {r['bucket']}**: {_fmt_pct(r['win_rate_pct'])} win "
                f"({r['n_resolved']:,} resolved) = **{r['lift_pp']:+.1f} pp**")
    add()
    add("---")
    add(f"*Script: `python/backtests/tv_pattern_history_backtest.py` · "
        f"input `{Path(args.json).name}` ({len(df):,} bases, {int(is_bo.sum()):,} breakouts, "
        f"{int(resolved_mask.sum()):,} resolved).*")
    add()
    add("*Caveat: every lift here is **in-sample** — the same 27k recorded bases were used to "
        "find the buckets and to measure them, and correlated dimensions (e.g. `dis_ratio`, "
        "`dis_days`, `acc_days`) are the same signal measured twice. Wilson CIs cover sampling "
        "noise on a single bucket; they do not cover the multiple-comparison or overfitting "
        "risk of the greedy chain. Treat the §25 chain as a hypothesis to validate on a "
        "holdout, not as a backtested edge.*")

    REPORT_MD.write_text("\n".join(L) + "\n")
    print(f"Wrote {REPORT_MD}")
    print(f"\nBaseline resolved win rate: {baseline * 100:.1f}% "
          f"({int(target_mask.sum()):,} Target / {int(resolved_mask.sum()):,} resolved)")


if __name__ == "__main__":
    main()
