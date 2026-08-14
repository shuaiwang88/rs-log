"""
tv_engine.py — the shared pattern engine behind every backtest.
===============================================================
Replaces the IBD pattern scanner (python/ibd_pattern_scanner.py, a port of
drw_pattern_scanner.pine) with OUR pattern engine (python/tv_pattern_scanner.py, a
faithful port of pine/drw_pattern.pine) so every backtest tests the same patterns the
📐 TV Pattern tab reports and the user actually trades.

What this module provides:
  * prepare_frame()       - the exact frame prep scan_ticker applies (sort, dedup,
                            dropna, tail MAX_BARS), so history `end_bar` positions line
                            up 1:1 with the frame we trade on.
  * scan_record()         - run scan_ticker once per ticker (df handed over to avoid a
                            double read) and return (record, prepared_df).
  * ticker_signals()      - per-bar boolean signal arrays (vol dry-up, upside reversal,
                            MA touch, pocket pivot, RS new high, shakeout, high volume)
                            vectorised exactly as tv_pattern_scanner.py computes them.
  * extract_bases()       - translate the scanner's ended-base history into the base
                            dicts the trade simulators expect (pivot, bLow, depth, days,
                            pattern/shape, acc/dis days, breakout bar).
  * detect_buy_signals()  - the 8-strategy signal book (same contract as the old
                            scanner_universe loop, but fed from our signal arrays).
  * regime_arrays()       - per-bar SPY regime (above SMA200 / above SMA50+SMA200),
                            the market filter the history backtest found matters.
  * price_bucket()        - the price buckets from the history backtest.

The buy signals here are the same six drw_pattern_scanner.pine sub-signals our history
backtest Part II recomputes (see tv_pattern_history_backtest.py), so every trade the
simulators produce is judged by the same definitions as the 27K-base profile.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "python"))

from tv_pattern_scanner import (          # noqa: E402  (the engine's own source)
    MAX_BARS, scan_ticker,
    I_VDU_LENGTH, I_DRY_UP_REQ, I_MA_TOUCH_THRESH,
    I_MA_LEN1, I_MA_LEN2, I_MA_LEN3, I_SHAKE_TREND_LEN, I_SHAKE_LR,
    I_VOL_BREAKOUT_MULT, _pivot_flags,
)

# ── Pattern groups, mapped onto OUR pattern/shape vocabulary. The history only carries
# pattern (Cup / Cup+Handle / Base) + base_shape (Flat Base / Consolidation) — the
# double-bottom flag lives only on live records, so that group is empty in the sims. ──
PATTERN_GROUPS = {
    "Cup+Handle": {"Cup+Handle"},
    "Cup": {"Cup"},
    "Flat Base": {"Flat Base"},
    "Consolidation": {"Consolidation"},
    "Deep Base": {"Deep Base"},        # plain Base whose depth is > 18% (see _pat_groups)
    "VCP-ready": {"Cup+Handle", "Cup", "Flat Base", "Consolidation"},
}


def prepare_frame(df):
    """Mirror scan_ticker's frame prep exactly (sort, dedup, dropna, tail MAX_BARS) so
    the history `end_bar` positions line up with the frame we simulate on."""
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
    if len(df) < 120:                  # same floor as scan_ticker
        return None
    return df


def scan_record(ticker, fpath, spy_close, df):
    """Run our scanner on an already-prepared frame; returns (record, prepared_df)."""
    df = prepare_frame(df)
    if df is None:
        return None, None
    try:
        rec = scan_ticker(str(ticker), str(fpath), spy_close, df=df)
    except Exception:
        return None, df
    return rec, df


def ticker_signals(df, spy_close):
    """Per-bar signal arrays for one prepared frame, vectorised exactly as
    tv_pattern_scanner.py computes them (each formula cites the scanner section it was
    copied from). Returns a dict of bool/float arrays, or None when unusable."""
    if df is None or len(df) < 120:
        return None
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    volume = np.nan_to_num(df["Volume"].to_numpy(dtype=float), nan=0.0)
    n = len(df)
    idx = df.index

    # volume dry-up (scanner: I_VDU_LENGTH=50 SMA, I_DRY_UP_REQ=45): volume < 55% of its
    # own 50-bar average.
    vol_sma = pd.Series(volume).rolling(I_VDU_LENGTH).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where(vol_sma > 0, volume / vol_sma * 100.0, np.nan)
    vol_dry_up = vol_ratio < (100.0 - I_DRY_UP_REQ)

    # upside reversal (scanner lines 820-821): range beats its own 14-bar ATR (Wilder
    # RMA), close in the upper half of the range.
    _prev_close = np.concatenate([[close[0]], close[:-1]])
    _tr = np.maximum.reduce([high - low, np.abs(high - _prev_close),
                             np.abs(low - _prev_close)])
    atr14 = pd.Series(_tr).ewm(alpha=1.0 / 14, adjust=False).mean().to_numpy()
    upside_reversal = (high - low > atr14) & (close > (high + low) / 2.0)

    # MA touch (scanner lines 731-749): touches any of EMA 10/21/34 within
    # I_MA_TOUCH_THRESH % (0.5%).
    def _ema(length):
        return pd.Series(close).ewm(span=length, adjust=False).mean().to_numpy()

    def _touches(ma_val):
        up = ma_val * (1.0 + I_MA_TOUCH_THRESH / 100.0)
        lo = ma_val * (1.0 - I_MA_TOUCH_THRESH / 100.0)
        return (ma_val > 0) & (low <= up) & (high >= lo)

    touched_ma = (_touches(_ema(I_MA_LEN1)) | _touches(_ema(I_MA_LEN2))
                  | _touches(_ema(I_MA_LEN3)))

    # pocket pivot, general form (scanner lines 684-700): up day with volume above the
    # max down-volume of the prior 10 bars (or prior 5), excluding today.
    _price_diff = np.diff(close, prepend=close[0])
    _is_up_day = _price_diff > 0
    _down_vol = np.where(_price_diff < 0, volume, 0.0)
    _h10 = pd.Series(_down_vol).shift(1).rolling(10).max().to_numpy()
    _h5 = pd.Series(_down_vol).shift(1).rolling(5).max().to_numpy()
    pp_any = ((_is_up_day & (volume > _h10) & ~np.isnan(_h10))
              | (_is_up_day & (volume > _h5) & ~np.isnan(_h5)))

    # RS new high vs SPY (scanner lines 203-213): relative-strength curve new high vs
    # its prior 1y / 6m / 3m window.
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

    # shakeout entry (scanner lines 771-808): undercut the last confirmed swing low
    # while above the 50-bar trend EMA -> reclaim the 3-EMA within 3 bars -> entry when
    # a later high clears the reclaim bar's high. Sequential, copied verbatim.
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
        undercut = (not np.isnan(last_swing_low) and low[_k] < last_swing_low and uptrend)
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

    # plain high-volume day (extra, not in the Pine score system): volume above
    # I_VOL_BREAKOUT_MULT (1.5x) of its 50-day average.
    vol_ma50 = pd.Series(volume).rolling(50).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        _ratio = volume / np.where(vol_ma50 > 0, vol_ma50, np.nan)
    vol_spike = _ratio > I_VOL_BREAKOUT_MULT

    sma50 = pd.Series(close).rolling(50, min_periods=10).mean().to_numpy()

    return {
        "vol_dry_up": vol_dry_up, "upside_reversal": upside_reversal,
        "touched_ma": touched_ma, "pp_any": pp_any, "nh_any": nh_any,
        "shakeout_entry": shakeout_entry, "vol_spike": vol_spike, "sma50": sma50,
    }


def extract_bases(rec, n):
    """Translate the scanner's ended-base history into the base dicts the trade
    simulators expect. Only finished bases are carried: a 'Breakout' base gets its
    breakout bar (end_bar); a 'Breakdown' base is a failed base and gets none (the
    mid-base signals inside it are still simulated, same as the old engine)."""
    hist = (rec or {}).get("history") or []
    bases = []
    for ep in hist:
        ended = ep.get("ended")
        if ended not in ("Breakout", "Breakdown"):
            continue
        end_bar = ep.get("end_bar")
        if end_bar is None or not (0 <= end_bar < n):
            continue
        days = int(ep.get("days") or 0)
        start = max(0, end_bar - days)
        pivot = ep.get("pivot") or ep.get("base_top")
        bLow = ep.get("base_low")
        if pivot is None or pivot <= 0 or bLow is None or bLow <= 0:
            continue
        pattern = ep.get("pattern") or "Base"
        shape = ep.get("base_shape")
        bases.append({
            "start": start, "end": end_bar,
            "bo_bar": end_bar if ended == "Breakout" else None,
            "pivot": pivot, "bLow": bLow, "bTop": ep.get("base_top") or pivot,
            "bDepPct": ep.get("depth_pct"), "bCount": days,
            "pattern": shape or pattern,        # display name (shape wins for Base)
            "raw_pattern": pattern, "shape": shape,
            "acc_days": int(ep.get("acc_days") or 0),
            "dis_days": int(ep.get("dis_days") or 0),
            "neu_days": int(ep.get("neu_days") or 0),
            "ended": ended,
        })
    return bases


def pat_groups(base):
    """Set of pattern-group names a base belongs to (see PATTERN_GROUPS)."""
    p = base.get("pattern") or ""
    rp = base.get("raw_pattern") or ""
    g = set()
    if p == "Cup+Handle":
        g |= {"Cup+Handle", "VCP-ready"}
    elif p == "Cup":
        g |= {"Cup", "VCP-ready"}
    elif p == "Flat Base":
        g |= {"Flat Base", "VCP-ready"}
    elif p == "Consolidation":
        g |= {"Consolidation", "VCP-ready"}
    if rp == "Base" and (base.get("bDepPct") or 0) > 18:
        g.add("Deep Base")
    return g


def detect_buy_signals(sig, highs, lows, closes, opens, pivot, bLow,
                       search_start, search_end, bo_bar):
    """The 8-strategy signal book (same contract as the old scanner_universe loop, fed
    from our per-bar arrays). Returns {signal_name: (bar, entry_price)}."""
    n = len(closes)
    signals = {}

    # 1. Pivot Breakout — the bar our scanner recorded the base ending as a breakout.
    if bo_bar is not None and bo_bar <= n - 1:
        entry = max(pivot, closes[bo_bar]) if closes[bo_bar] > pivot else pivot
        signals["Pivot Breakout"] = (bo_bar, entry)

    for j in range(search_start, min(search_end + 1, n)):
        c = closes[j]
        # 2. Upside Reversal
        if sig["upside_reversal"][j] and bLow <= c <= pivot * 1.01 and "Upside Reversal" not in signals:
            signals["Upside Reversal"] = (j, c)
        # 3. Shakeout
        if sig["shakeout_entry"][j] and pivot * 0.85 <= c <= pivot and "Shakeout" not in signals:
            signals["Shakeout"] = (j, c)
        # 4. Volume Dry-Up
        if sig["vol_dry_up"][j] and pivot * 0.95 <= c <= pivot * 1.01 and "Volume Dry-Up" not in signals:
            signals["Volume Dry-Up"] = (j, c)
        # 5. MA Touch
        if sig["touched_ma"][j] and c >= bLow and c <= pivot and "MA Touch" not in signals:
            signals["MA Touch"] = (j, c)
        # 6. Pocket Pivot
        if sig["pp_any"][j] and pivot * 0.90 <= c <= pivot * 1.01 and "Pocket Pivot" not in signals:
            signals["Pocket Pivot"] = (j, c)
        # 7. RS New High
        if sig["nh_any"][j] and pivot * 0.85 <= c <= pivot * 1.01 and "RS New High" not in signals:
            signals["RS New High"] = (j, c)
        # 8. SMA50 Bounce — dip to SMA50 then reclaim
        sma50 = sig["sma50"]
        if j >= 2 and not np.isnan(sma50[j]) and sma50[j] > 0:
            prev_tested = (lows[j - 1] <= sma50[j - 1] * 1.02
                           if not np.isnan(sma50[j - 1]) and sma50[j - 1] > 0 else False)
            if (prev_tested and c >= opens[j]
                    and bLow <= c <= pivot and "SMA50 Bounce" not in signals):
                signals["SMA50 Bounce"] = (j, c)
    return signals


def regime_arrays(spy_close, df):
    """Per-bar SPY regime aligned to the prepared frame, as a dict with per-bar arrays
    (or None when SPY is unavailable/unaligned):
        above200  - SPY > its 200-day SMA
        bull      - SPY > SMA50 AND > SMA200 (the tape the history backtest found best)
        spy       - aligned SPY close
        s200      - SPY 200-day SMA"""
    if spy_close is None or len(spy_close) == 0:
        return None
    try:
        idx = df.index
        _spy = spy_close.reindex(idx).ffill().bfill().to_numpy(dtype=float)
        if len(_spy) != len(df) or not np.all(np.isfinite(_spy)) or np.any(_spy <= 0):
            return None
        c = pd.Series(_spy)
        s50 = c.rolling(50, min_periods=30).mean().to_numpy()
        s200 = c.rolling(200, min_periods=120).mean().to_numpy()
        return {"above200": _spy > s200, "bull": (_spy > s50) & (_spy > s200),
                "spy": _spy, "s200": s200}
    except Exception:
        return None


def regime_label(above200, bull):
    if above200 is None or bull is None:
        return "unknown"
    if bool(above200) and bool(bull):
        return "Bull"
    if bool(above200):
        return "Mixed"
    return "Bear"


def base_gate(rec, prepared_n, n_bars, prepared_idx=None, full_idx=None):
    """Bool array (len n_bars) True where the ticker was inside a finished or currently
    forming base, reconstructed from the scanner's ended-base history + live status. Used
    by trend-following strategies that gate entries on pattern context (alt48).

    When prepared_idx/full_idx are supplied, base windows are mapped by date so rows
    dropped by prepare_frame (interior NaNs, duplicate dates) don't shift the windows;
    otherwise a front-offset heuristic is used (exact when the only difference is the
    MAX_BARS tail-trim)."""
    gate = np.zeros(n_bars, dtype=bool)
    if rec is None:
        return gate
    if prepared_idx is not None and full_idx is not None:
        try:
            # prepared date -> its position in the FULL frame (last dup wins, -1 if absent)
            full_pos = dict(zip(full_idx, range(n_bars)))
            pos_arr = np.array([full_pos.get(d, -1) for d in prepared_idx])
        except Exception:
            pos_arr = None
        if pos_arr is not None:
            for ep in (rec.get("history") or []):
                end = ep.get("end_bar")
                if end is None or not (0 <= end < prepared_n):
                    continue
                days = int(ep.get("days") or 0)
                start = max(0, end - days)
                seg = pos_arr[start:end + 1]
                seg = seg[seg >= 0]
                if len(seg):
                    gate[seg] = True
            if rec.get("status") == "In Base":
                days = int(rec.get("days_in_base") or 0)
                last = prepared_n - 1
                seg = pos_arr[max(0, last - days):last + 1]
                seg = seg[seg >= 0]
                if len(seg):
                    gate[seg] = True
            return gate
    offset = n_bars - prepared_n
    if offset < 0:
        return gate
    for ep in (rec.get("history") or []):
        end = ep.get("end_bar")
        if end is None or not (0 <= end < prepared_n):
            continue
        days = int(ep.get("days") or 0)
        start = max(0, end - days)
        gate[offset + start:offset + end + 1] = True
    if rec.get("status") == "In Base":
        days = int(rec.get("days_in_base") or 0)
        last = prepared_n - 1
        gate[offset + max(0, last - days):offset + last + 1] = True
    return gate


def price_bucket(price):
    if price < 10:
        return "<$10"
    if price < 25:
        return "$10-25"
    if price < 50:
        return "$25-50"
    if price < 100:
        return "$50-100"
    if price < 250:
        return "$100-250"
    return "$250+"
