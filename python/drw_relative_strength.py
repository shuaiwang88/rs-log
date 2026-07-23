#!/usr/bin/env python3
"""
drw_relative_strength.py

Python implementation of Pine Script Relative Strength indicator (drw_relative_strength_all.pine)
WITHOUT RS Pattern Recognition (Base / Flat Base / Cup).

Features:
  1. RS Line & Comparative Benchmark Scaling
  2. RS Moving Averages (Quick, QuickSand, Grateful Dead)
  3. RS New Highs (1Y, 6M, 3M) & RS Leads Price detection
  4. RS New Lows (250-bar lookback)
  5. IBD-Style RS Ratings (12M, 6M, 3M, 1W Change, 1M Change)
  6. RS Trendlines & Breakout Detection
  7. RS / Price Bullish Divergence Detection
  8. CLI Runner & Dashboard Summary
"""

import sys
import math
import argparse
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# Helper TA Functions
# ---------------------------------------------------------------------------

def compute_ema(series: pd.Series, span: int) -> pd.Series:
    """Compute Exponential Moving Average matching Pine Script ta.ema."""
    return series.ewm(span=span, adjust=False).mean()


def find_pivot_highs(series: pd.Series, left: int, right: int) -> pd.Series:
    """
    Find pivot highs matching Pine Script ta.pivothigh(series, left, right).
    Returns a series where value is pivot high price at the bar it was CONFIRMED (i.e. bar i),
    or NaN if no pivot high confirmed at bar i.
    """
    n = len(series)
    pivots = pd.Series(index=series.index, dtype=float)
    vals = series.values

    for i in range(left + right, n):
        target_idx = i - right
        val = vals[target_idx]
        if pd.isna(val):
            continue
        
        is_pivot = True
        for k in range(target_idx - left, target_idx + right + 1):
            if k != target_idx and not pd.isna(vals[k]) and vals[k] >= val:
                is_pivot = False
                break
        if is_pivot:
            pivots.iloc[i] = val

    return pivots


def find_pivot_lows(series: pd.Series, left: int, right: int) -> pd.Series:
    """
    Find pivot lows matching Pine Script ta.pivotlow(series, left, right).
    Returns a series where value is pivot low price at the bar it was CONFIRMED (i.e. bar i).
    """
    n = len(series)
    pivots = pd.Series(index=series.index, dtype=float)
    vals = series.values

    for i in range(left + right, n):
        target_idx = i - right
        val = vals[target_idx]
        if pd.isna(val):
            continue

        is_pivot = True
        for k in range(target_idx - left, target_idx + right + 1):
            if k != target_idx and not pd.isna(vals[k]) and vals[k] <= val:
                is_pivot = False
                break
        if is_pivot:
            pivots.iloc[i] = val

    return pivots


# ---------------------------------------------------------------------------
# Percentile Rating Interpolation
# ---------------------------------------------------------------------------

def attribute_percentile(score: float, taller_perf: float, smaller_perf: float, 
                         range_up: float, range_dn: float, weight: float) -> float:
    """Interpolate RS Rating based on score thresholds (matches Pine script f_attributePercentile)."""
    sum_val = score + (score - smaller_perf) * weight
    if sum_val > taller_perf - 1.0:
        sum_val = taller_perf - 1.0
    
    k1 = smaller_perf / range_dn
    k2 = (taller_perf - 1.0) / range_up
    denom = (taller_perf - 1.0 - smaller_perf)
    
    if denom == 0:
        return range_dn
        
    k3 = (k1 - k2) / denom
    
    denom2 = (k1 - k3 * (score - smaller_perf))
    if denom2 == 0:
        return range_dn
        
    rs_rating = sum_val / denom2
    return max(range_dn, min(range_up, rs_rating))


def calculate_rs_rating(score: float, f: float, s: float, t: float, 
                         fr: float, ff: float, sx: float, sv: float) -> float:
    """Calculate 1-99 RS rating score given benchmark performance cutoffs."""
    if pd.isna(score) or score <= 0:
        return -1.0
    if score >= f:
        return 99.0
    elif score <= sv:
        return 1.0
    elif score < f and score >= s:
        return attribute_percentile(score, f, s, 98, 90, 0.33)
    elif score < s and score >= t:
        return attribute_percentile(score, s, t, 89, 70, 2.1)
    elif score < t and score >= fr:
        return attribute_percentile(score, t, fr, 69, 50, 0)
    elif score < fr and score >= ff:
        return attribute_percentile(score, fr, ff, 49, 30, 0)
    elif score < ff and score >= sx:
        return attribute_percentile(score, ff, sx, 29, 10, 0)
    elif score < sx and score >= sv:
        return attribute_percentile(score, sx, sv, 9, 2, 0)
    return -1.0


# ---------------------------------------------------------------------------
# Core Relative Strength Calculator
# ---------------------------------------------------------------------------

@dataclass
class RSTrendlineInfo:
    anchor_bar: int
    anchor_price: float
    end_bar: int
    end_price: float
    touches: int
    active: bool
    breakout: bool = False


def calculate_relative_strength(
    df: pd.DataFrame,
    bench_df: pd.DataFrame,
    timeframe: str = 'daily',
    spx_value: float = 4200.0,
    offset: float = 130.0,
    lookback_1y: Optional[int] = None,
    lookback_6m: Optional[int] = None,
    lookback_3m: Optional[int] = None,
    # Rating thresholds (Replay / Standard mode defaults from Pine Script)
    first_thresh: float = 195.93,
    scnd_thresh: float = 117.11,
    thrd_thresh: float = 99.04,
    frth_thresh: float = 91.66,
    ffth_thresh: float = 80.96,
    sxth_thresh: float = 53.64,
    svth_thresh: float = 24.86,
    # Trendline settings
    tl_bars: int = 5,
    tl_buffer_pct: float = 0.1,
    tl_num_touches: int = 3,
    tl_new_h: int = 100,
    tl_show_num: int = 1,
    # Divergence settings
    piv_len: int = 9
) -> pd.DataFrame:
    """
    Calculate all Relative Strength metrics, moving averages, new highs/lows,
    RS ratings, trendlines, and bullish divergences on a given price DataFrame.

    Parameters:
    -----------
    df : pd.DataFrame
        DataFrame with datetime index and columns ['open', 'high', 'low', 'close', 'volume'].
    bench_df : pd.DataFrame
        DataFrame for reference symbol (e.g. S&P 500 / SPY) with datetime index and column ['close'].
    timeframe : str
        'daily', 'weekly', or 'monthly'.
    """
    res = df.copy()

    # Normalize column names to lowercase
    res.columns = [c.lower() for c in res.columns]
    bench_close = bench_df['close'].reindex(res.index).ffill()

    is_weekly = timeframe.lower() == 'weekly'
    is_monthly = timeframe.lower() == 'monthly'

    # Set dynamic lookbacks based on timeframe
    if lookback_1y is None:
        lookback_1y = 52 if is_weekly else 250
    if lookback_6m is None:
        lookback_6m = 24 if is_weekly else 126
    if lookback_3m is None:
        lookback_3m = 12 if is_weekly else 63

    # 1. RS Curve & Scaled RS Line
    rs_curve = res['close'] / bench_close
    rs_ratio = spx_value * (offset - 10) / 100.0 if is_weekly else spx_value * offset / 100.0
    rs_line = rs_curve * rs_ratio

    res['rs_curve'] = rs_curve
    res['rs_line'] = rs_line

    # 2. RS Moving Averages
    quick_len = 5 if is_monthly else (8 if is_weekly else 21)
    quicksand_len = 8 if is_monthly else (13 if is_weekly else 34)
    gd_len = 13 if is_monthly else (21 if is_weekly else 50)

    res['ema_quick'] = compute_ema(rs_line, quick_len)
    res['ema_quicksand'] = compute_ema(rs_line, quicksand_len)
    res['ema_grateful'] = compute_ema(rs_line, gd_len)

    # 3. RS New Highs & RS Leads Price
    n = len(res)
    rs_nh_1y = pd.Series(False, index=res.index)
    rs_nh_6m = pd.Series(False, index=res.index)
    rs_nh_3m = pd.Series(False, index=res.index)
    rs_nh_any = pd.Series(False, index=res.index)

    price_nh_1y = pd.Series(False, index=res.index)
    price_nh_6m = pd.Series(False, index=res.index)
    price_nh_3m = pd.Series(False, index=res.index)
    price_nh_matching = pd.Series(False, index=res.index)

    rs_leads_price = pd.Series(False, index=res.index)

    rs_curve_vals = rs_curve.values
    high_vals = res['high'].values

    for i in range(1, n):
        # RS Highs over lookback windows (excluding current bar: [i - window : i])
        h1y = np.nanmax(rs_curve_vals[max(0, i - lookback_1y):i]) if i > 0 else np.nan
        h6m = np.nanmax(rs_curve_vals[max(0, i - lookback_6m):i]) if i > 0 else np.nan
        h3m = np.nanmax(rs_curve_vals[max(0, i - lookback_3m):i]) if i > 0 else np.nan

        cur_rs = rs_curve_vals[i]
        if not np.isnan(cur_rs):
            nh1 = not np.isnan(h1y) and cur_rs > h1y
            nh6 = not np.isnan(h6m) and cur_rs > h6m and not nh1
            nh3 = not np.isnan(h3m) and cur_rs > h3m and not nh1 and not nh6

            rs_nh_1y.iloc[i] = nh1
            rs_nh_6m.iloc[i] = nh6
            rs_nh_3m.iloc[i] = nh3
            rs_nh_any.iloc[i] = nh1 or nh6 or nh3

        # Price Highs over lookback windows
        ph1y = np.nanmax(high_vals[max(0, i - lookback_1y):i]) if i > 0 else np.nan
        ph6m = np.nanmax(high_vals[max(0, i - lookback_6m):i]) if i > 0 else np.nan
        ph3m = np.nanmax(high_vals[max(0, i - lookback_3m):i]) if i > 0 else np.nan

        cur_high = high_vals[i]
        if not np.isnan(cur_high):
            p_nh1 = not np.isnan(ph1y) and cur_high > ph1y
            p_nh6 = not np.isnan(ph6m) and cur_high > ph6m
            p_nh3 = not np.isnan(ph3m) and cur_high > ph3m

            price_nh_1y.iloc[i] = p_nh1
            price_nh_6m.iloc[i] = p_nh6
            price_nh_3m.iloc[i] = p_nh3

            # Matching price NH for the RS NH period
            if rs_nh_1y.iloc[i]:
                p_match = p_nh1
            elif rs_nh_6m.iloc[i]:
                p_match = p_nh6
            else:
                p_match = p_nh3
            
            price_nh_matching.iloc[i] = p_match
            if rs_nh_any.iloc[i] and not p_match:
                rs_leads_price.iloc[i] = True

    res['rs_nh_1y'] = rs_nh_1y
    res['rs_nh_6m'] = rs_nh_6m
    res['rs_nh_3m'] = rs_nh_3m
    res['rs_nh_any'] = rs_nh_any
    res['rs_leads_price'] = rs_leads_price

    # 4. RS New Lows (250-bar lookback)
    lookback_low = lookback_1y
    rs_nl = pd.Series(False, index=res.index)
    price_nl = pd.Series(False, index=res.index)
    low_vals = res['low'].values

    for i in range(1, n):
        rsl = np.nanmin(rs_curve_vals[max(0, i - lookback_low):i]) if i > 0 else np.nan
        prl = np.nanmin(low_vals[max(0, i - lookback_low):i]) if i > 0 else np.nan

        if not np.isnan(rs_curve_vals[i]) and not np.isnan(rsl) and rs_curve_vals[i] < rsl:
            rs_nl.iloc[i] = True
        if not np.isnan(low_vals[i]) and not np.isnan(prl) and low_vals[i] < prl:
            price_nl.iloc[i] = True

    res['rs_nl'] = rs_nl
    res['price_nl'] = price_nl

    # 5. IBD RS Ratings (12M, 6M, 3M, 1W Change, 1M Change)
    close_vals = res['close'].values
    bclose_vals = bench_close.values

    rs_rating_12m = pd.Series(np.nan, index=res.index)
    rs_rating_6m = pd.Series(np.nan, index=res.index)
    rs_rating_3m = pd.Series(np.nan, index=res.index)

    for i in range(n):
        i63 = max(0, i - 63)
        i126 = max(0, i - 126)
        i189 = max(0, i - 189)
        i252 = max(0, i - 252)

        p63 = close_vals[i] / close_vals[i63] if close_vals[i63] > 0 else 1.0
        p126 = close_vals[i] / close_vals[i126] if close_vals[i126] > 0 else 1.0
        p189 = close_vals[i] / close_vals[i189] if close_vals[i189] > 0 else 1.0
        p252 = close_vals[i] / close_vals[i252] if close_vals[i252] > 0 else 1.0

        bp63 = bclose_vals[i] / bclose_vals[i63] if bclose_vals[i63] > 0 else 1.0
        bp126 = bclose_vals[i] / bclose_vals[i126] if bclose_vals[i126] > 0 else 1.0
        bp189 = bclose_vals[i] / bclose_vals[i189] if bclose_vals[i189] > 0 else 1.0
        bp252 = bclose_vals[i] / bclose_vals[i252] if bclose_vals[i252] > 0 else 1.0

        rs_stock = 0.4 * p63 + 0.2 * p126 + 0.2 * p189 + 0.2 * p252
        rs_ref = 0.4 * bp63 + 0.2 * bp126 + 0.2 * bp189 + 0.2 * bp252

        total_rs_score = (rs_stock / rs_ref) * 100.0 if rs_ref > 0 else 100.0
        score_3m = (p63 / bp63) * 100.0 if bp63 > 0 else 100.0
        score_6m = (p126 / bp126) * 100.0 if bp126 > 0 else 100.0

        r12m = calculate_rs_rating(total_rs_score, first_thresh, scnd_thresh, thrd_thresh,
                                   frth_thresh, ffth_thresh, sxth_thresh, svth_thresh)
        r6m = calculate_rs_rating(score_6m, first_thresh, scnd_thresh, thrd_thresh,
                                  frth_thresh, ffth_thresh, sxth_thresh, svth_thresh)
        r3m = calculate_rs_rating(score_3m, first_thresh, scnd_thresh, thrd_thresh,
                                  frth_thresh, ffth_thresh, sxth_thresh, svth_thresh)

        rs_rating_12m.iloc[i] = r12m
        rs_rating_6m.iloc[i] = r6m
        rs_rating_3m.iloc[i] = r3m

    res['rs_rating_12m'] = rs_rating_12m
    res['rs_rating_6m'] = rs_rating_6m
    res['rs_rating_3m'] = rs_rating_3m

    # Rating changes (5 days ago for 1W, 20 days ago for 1M)
    res['rs_rating_1w_change'] = res['rs_rating_12m'] - res['rs_rating_12m'].shift(5)
    res['rs_rating_1m_change'] = res['rs_rating_12m'] - res['rs_rating_12m'].shift(20)

    # 6. RS Trendline & Breakout Detection
    ph_series = find_pivot_highs(rs_line, tl_bars, tl_bars)
    buffer_frac = tl_buffer_pct / 100.0

    piv_h = np.nan
    piv_b = -1
    trendlines: List[Dict[str, Any]] = []
    rs_breakout = pd.Series(False, index=res.index)

    rs_vals = rs_line.values

    for i in range(n):
        # Check pivot high confirmed at bar i (occurred at i - tl_bars)
        if not np.isnan(ph_series.iloc[i]):
            conf_h = rs_vals[i - tl_bars]
            conf_b = i - tl_bars
            if np.isnan(piv_h) or conf_h > piv_h or (i - piv_b > tl_new_h):
                piv_h = conf_h
                piv_b = conf_b

        bo_flag = False

        if not np.isnan(piv_h) and piv_b >= 0 and i > piv_b:
            if rs_vals[i] < piv_h:
                # Calculate trial line from (piv_b, piv_h) to (i, rs_vals[i])
                slope = (rs_vals[i] - piv_h) / (i - piv_b)
                touches = 0
                valid = True

                for k in range(piv_b, i + 1):
                    line_price = piv_h + slope * (k - piv_b)
                    if rs_vals[k] > line_price * (1.0 + buffer_frac):
                        valid = False
                        break
                    elif (line_price * (1.0 - buffer_frac) <= rs_vals[k] <= line_price * (1.0 + buffer_frac)):
                        touches += 1

                if valid and touches >= tl_num_touches:
                    trendlines.append({
                        'piv_b': piv_b,
                        'piv_h': piv_h,
                        'slope': slope,
                        'created_b': i,
                        'active': True
                    })
                    if len(trendlines) > tl_show_num:
                        trendlines.pop(0)

        # Process active trendlines for breakout
        for tl in trendlines:
            if tl['active']:
                line_price_i = tl['piv_h'] + tl['slope'] * (i - tl['piv_b'])
                if rs_vals[i] > line_price_i * (1.0 + buffer_frac):
                    bo_flag = True
                    tl['active'] = False

        rs_breakout.iloc[i] = bo_flag

    res['rs_breakout'] = rs_breakout

    # 7. RS / Price Bullish Divergence Detection
    rs_pl_series = find_pivot_lows(rs_line, piv_len, piv_len)
    pr_pl_series = find_pivot_lows(res['low'], piv_len, piv_len)

    bullish_div = pd.Series(False, index=res.index)
    last_rs_pl = np.nan
    prev_rs_pl = np.nan
    last_pr_pl = np.nan
    prev_pr_pl = np.nan

    for i in range(n):
        has_rs_piv = not np.isnan(rs_pl_series.iloc[i])
        has_pr_piv = not np.isnan(pr_pl_series.iloc[i])

        if has_rs_piv:
            prev_rs_pl = last_rs_pl
            last_rs_pl = rs_vals[i - piv_len]

        if has_pr_piv:
            prev_pr_pl = last_pr_pl
            last_pr_pl = low_vals[i - piv_len]

        if has_rs_piv and has_pr_piv and not np.isnan(prev_rs_pl) and not np.isnan(prev_pr_pl):
            if (last_rs_pl > prev_rs_pl) and (last_pr_pl < prev_pr_pl):
                bullish_div.iloc[i] = True

    res['rs_bullish_divergence'] = bullish_div

    return res


# ---------------------------------------------------------------------------
# Summary Data Extraction
# ---------------------------------------------------------------------------

def get_rs_summary(df_rs: pd.DataFrame) -> Dict[str, Any]:
    """Extracts latest RS metrics dictionary from calculated DataFrame."""
    last_row = df_rs.iloc[-1]
    
    is_nh = bool(last_row.get('rs_nh_any', False))
    is_nl = bool(last_row.get('rs_nl', False))
    leads_price = bool(last_row.get('rs_leads_price', False))

    if is_nh:
        nh_period = '1Y' if last_row.get('rs_nh_1y') else ('6M' if last_row.get('rs_nh_6m') else '3M')
        rs_state = f"New High ({nh_period})" + (" [Leads Price]" if leads_price else "")
    elif is_nl:
        rs_state = "New Low"
    else:
        recent_nh = df_rs['rs_nh_any'].tail(5).any()
        recent_nl = df_rs['rs_nl'].tail(5).any()
        if recent_nh:
            rs_state = "New High (Recent)"
        elif recent_nl:
            rs_state = "New Low (Recent)"
        else:
            rs_state = "Neutral"

    return {
        'date': str(df_rs.index[-1].strftime('%Y-%m-%d')) if hasattr(df_rs.index[-1], 'strftime') else str(df_rs.index[-1]),
        'close': float(last_row['close']),
        'rs_line': float(last_row['rs_line']),
        'rs_rating_12m': round(float(last_row['rs_rating_12m']), 1) if not pd.isna(last_row['rs_rating_12m']) else None,
        'rs_rating_6m': round(float(last_row['rs_rating_6m']), 1) if not pd.isna(last_row['rs_rating_6m']) else None,
        'rs_rating_3m': round(float(last_row['rs_rating_3m']), 1) if not pd.isna(last_row['rs_rating_3m']) else None,
        'rs_rating_1w_change': round(float(last_row['rs_rating_1w_change']), 1) if not pd.isna(last_row['rs_rating_1w_change']) else None,
        'rs_rating_1m_change': round(float(last_row['rs_rating_1m_change']), 1) if not pd.isna(last_row['rs_rating_1m_change']) else None,
        'rs_state': rs_state,
        'rs_leads_price': leads_price,
        'rs_breakout': bool(last_row.get('rs_breakout', False)),
        'rs_bullish_divergence': bool(last_row.get('rs_bullish_divergence', False)),
    }


# ---------------------------------------------------------------------------
# CLI Command Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Calculate Relative Strength metrics without RS Patterns.")
    parser.add_argument("--ticker", type=str, default="AAPL", help="Stock ticker symbol (default: AAPL)")
    parser.add_argument("--benchmark", type=str, default="SPY", help="Benchmark ticker symbol (default: SPY)")
    parser.add_argument("--period", type=str, default="2y", help="Historical data period (default: 2y)")
    parser.add_argument("--timeframe", type=str, choices=['daily', 'weekly', 'monthly'], default="daily", help="Chart timeframe")
    parser.add_argument("--csv", type=str, default=None, help="Path to input stock CSV (optional)")
    parser.add_argument("--bench-csv", type=str, default=None, help="Path to input benchmark CSV (optional)")
    parser.add_argument("--json", action="store_true", help="Output summary in JSON format")

    args = parser.parse_args()

    if args.csv and args.bench_csv:
        df = pd.read_csv(args.csv, parse_dates=True, index_col=0)
        bench_df = pd.read_csv(args.bench_csv, parse_dates=True, index_col=0)
    else:
        try:
            import yfinance as yf
        except ImportError:
            print("Error: yfinance module not found. Please install yfinance (`pip install yfinance`) or provide CSV inputs via --csv and --bench-csv.", file=sys.stderr)
            sys.exit(1)

        print(f"Fetching market data for {args.ticker} and {args.benchmark} ({args.period})...", file=sys.stderr)
        df = yf.download(args.ticker, period=args.period, progress=False)
        bench_df = yf.download(args.benchmark, period=args.period, progress=False)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if isinstance(bench_df.columns, pd.MultiIndex):
            bench_df.columns = bench_df.columns.get_level_values(0)

        if df.empty or bench_df.empty:
            print(f"Error: Failed to fetch market data for {args.ticker} or {args.benchmark}.", file=sys.stderr)
            sys.exit(1)

    # Calculate Relative Strength metrics
    rs_df = calculate_relative_strength(df, bench_df, timeframe=args.timeframe)
    summary = get_rs_summary(rs_df)

    import json
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

