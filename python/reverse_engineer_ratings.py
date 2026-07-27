#!/usr/bin/env python3
"""
Reverse Engineering IBD Ratings (EPS Rating, SMR Rating, Composite Rating, A/D Rating, and RS Rating)
Using Advanced Machine Learning Incorporating Up to 365-Day Historical Price & Volume Records.

A/D Rating Reverse Engineering includes:
1. 5-Tier Foundation Models (Regression-to-5-Tier & Direct 5-Class Classification)
2. Post-Regression 12-Tier Scoring System (Continuous Regression Score -> 12 Calibrated Sub-Tiers)
"""

import sys
import os
from pathlib import Path
import pandas as pd
import numpy as np

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, Ridge, LogisticRegression
from sklearn.ensemble import (
    RandomForestRegressor, ExtraTreesRegressor, HistGradientBoostingRegressor,
    RandomForestClassifier, ExtraTreesClassifier, HistGradientBoostingClassifier
)
from sklearn.metrics import (
    r2_score, mean_absolute_error, accuracy_score, f1_score,
    classification_report
)

def clean_num(val):
    if pd.isna(val):
        return np.nan
    s = str(val).replace('%', '').replace('$', '').replace(',', '').strip()
    if s in ['Yes', 'YES']:
        return 1.0
    if s in ['No', 'NO']:
        return 0.0
    try:
        return float(s)
    except:
        return np.nan

SUBTIER_13_MAP = {
    'A+': 13.0, 'A': 12.0, 'A-': 11.0,
    'B+': 10.0, 'B': 9.0,  'B-': 8.0,
    'C+': 7.0,  'C': 6.0,  'C-': 5.0,
    'D+': 4.0,  'D': 3.0,  'D-': 2.0,
    'E': 1.0
}

def grade_to_num(val):
    if pd.isna(val):
        return np.nan
    s = str(val).strip().upper()
    return SUBTIER_13_MAP.get(s, np.nan)

def smr_grade_to_num(val):
    if pd.isna(val):
        return np.nan
    s = str(val).strip().upper()
    mapping = {
        'A+': 95.0, 'A': 90.0, 'A-': 85.0,
        'B+': 75.0, 'B': 70.0, 'B-': 65.0,
        'C+': 55.0, 'C': 50.0, 'C-': 45.0,
        'D+': 35.0, 'D': 30.0, 'D-': 25.0,
        'E': 10.0
    }
    return mapping.get(s, np.nan)

def ad_5tier(val):
    if pd.isna(val): return np.nan
    s = str(val).strip().upper()[0]
    return s if s in ['A', 'B', 'C', 'D', 'E'] else np.nan

def ad_subtier(val):
    if pd.isna(val): return np.nan
    s = str(val).strip().upper()
    return s if s in SUBTIER_13_MAP else np.nan

def num_to_5tier(val):
    if val >= 10.5: return 'A'
    elif val >= 7.5: return 'B'
    elif val >= 5.5: return 'C'
    elif val >= 2.5: return 'D'
    else: return 'E'

# 12-Tier Mapping Function (12 Sub-Tier Buckets)
# Map 1..13 continuous regression score into 12 sub-tiers: A+, A, A-, B+, B, B-, C+, C, C-, D+, D, E
def num_to_12tier(val):
    if val >= 12.25: return 'A+'
    elif val >= 11.25: return 'A'
    elif val >= 10.25: return 'A-'
    elif val >= 9.25: return 'B+'
    elif val >= 8.25: return 'B'
    elif val >= 7.25: return 'B-'
    elif val >= 6.25: return 'C+'
    elif val >= 5.25: return 'C'
    elif val >= 4.25: return 'C-'
    elif val >= 3.25: return 'D+'
    elif val >= 2.25: return 'D'
    else: return 'E'

SUBTIER_12_MAP = {
    'A+': 12.0, 'A': 11.0, 'A-': 10.0,
    'B+': 9.0,  'B': 8.0,  'B-': 7.0,
    'C+': 6.0,  'C': 5.0,  'C-': 4.0,
    'D+': 3.0,  'D': 2.0,  'E': 1.0
}

def subtier_to_maintier(subtier):
    if pd.isna(subtier): return np.nan
    s = str(subtier).strip().upper()[0]
    return s if s in ['A', 'B', 'C', 'D', 'E'] else np.nan

MAIN_TIER_ORDER = {'A': 5, 'B': 4, 'C': 3, 'D': 2, 'E': 1}

def compute_window_features(prices, vols, window_size, highs=None, lows=None):
    """Compute volume and price metrics for a trailing window of trading days."""
    if len(prices) < min(5, window_size):
        return {}

    w_prices = prices[-window_size:]
    w_vols = vols[-window_size:]

    p_first = w_prices[0]
    p_last = w_prices[-1]
    ret_pct = (p_last / p_first - 1) * 100 if p_first > 0 else 0.0

    p_diff = np.diff(w_prices)
    safe_p_prev = np.where(w_prices[:-1] == 0, 1.0, w_prices[:-1])
    p_rets = p_diff / safe_p_prev

    vol_tail = w_vols[1:]
    mean_vol = max(1, np.mean(w_vols))
    vol_ratios = vol_tail / mean_vol

    up_mask = p_rets > 0
    dn_mask = p_rets < 0

    up_vol_sum = np.sum(vol_tail[up_mask])
    dn_vol_sum = np.sum(vol_tail[dn_mask])
    up_dn_vol_ratio = up_vol_sum / max(1, dn_vol_sum)

    heavy_up_mask = (p_rets > 0) & (vol_ratios > 1.2)
    heavy_dn_mask = (p_rets < 0) & (vol_ratios > 1.2)

    heavy_up_vol = np.sum(vol_tail[heavy_up_mask])
    heavy_dn_vol = np.sum(vol_tail[heavy_dn_mask])
    heavy_net_ratio = heavy_up_vol / max(1, heavy_up_vol + heavy_dn_vol)

    heavy_up_cnt = int(np.sum(heavy_up_mask))
    heavy_dn_cnt = int(np.sum(heavy_dn_mask))
    net_heavy_days = heavy_up_cnt - heavy_dn_cnt

    heavy_up_intensity = np.sum(p_rets[heavy_up_mask] * vol_ratios[heavy_up_mask])
    heavy_dn_intensity = np.sum(np.abs(p_rets[heavy_dn_mask]) * vol_ratios[heavy_dn_mask])
    net_heavy_intensity = heavy_up_intensity - heavy_dn_intensity

    tag = f"{window_size}D"
    res = {
        f'Price_Pct_Chg_{tag}': round(ret_pct, 2),
        f'Up_Dn_Vol_{tag}': round(up_dn_vol_ratio, 3),
        f'Heavy_Net_Ratio_{tag}': round(heavy_net_ratio, 3),
        f'Heavy_Up_Cnt_{tag}': heavy_up_cnt,
        f'Heavy_Dn_Cnt_{tag}': heavy_dn_cnt,
        f'Net_Heavy_Days_{tag}': net_heavy_days,
        f'Heavy_Up_Intensity_{tag}': round(heavy_up_intensity, 4),
        f'Heavy_Dn_Intensity_{tag}': round(heavy_dn_intensity, 4),
        f'Net_Heavy_Intensity_{tag}': round(net_heavy_intensity, 4),
    }

    if highs is not None and lows is not None and len(highs) >= window_size and len(lows) >= window_size:
        w_highs = highs[-window_size:]
        w_lows = lows[-window_size:]
        rng = np.maximum(0.01, w_highs - w_lows)
        cls_rng = (w_prices - w_lows) / rng * 100
        res[f'Avg_Closing_Range_{tag}'] = round(np.mean(cls_rng), 2)
        res[f'Vol_Weighted_Closing_Range_{tag}'] = round(np.sum(cls_rng * w_vols) / max(1, np.sum(w_vols)), 2)

    return res

def compute_historical_heavy_volume_features(tickers: list, repo_dir: Path, hist_file: Path) -> pd.DataFrame:
    """Extract trailing 250D / 365D price & volume interaction variables for each ticker using parallel threads."""
    from concurrent.futures import ThreadPoolExecutor
    print(f"Extracting trailing price & volume interaction variables for {len(tickers):,} tickers across 16 threads...")
    cache_dir = repo_dir / "ticker_cache"

    def process_single_ticker(t):
        t_clean = str(t).strip()
        p1 = cache_dir / f"{t_clean}_250d.parquet"
        p2 = cache_dir / f"{t_clean}_1d.parquet"
        p3 = cache_dir / f"{t_clean.replace('.', '-')}_250d.parquet"

        cdf = None
        for p_cand in [p1, p2, p3]:
            if p_cand.exists():
                try:
                    cdf = pd.read_parquet(p_cand)
                    if isinstance(cdf.columns, pd.MultiIndex):
                        cdf.columns = cdf.columns.droplevel(1)
                    break
                except Exception:
                    pass

        if cdf is None or cdf.empty or len(cdf) < 5:
            return None

        price_c = 'Close' if 'Close' in cdf.columns else ('Price' if 'Price' in cdf.columns else None)
        if not price_c or 'Volume' not in cdf.columns:
            return None

        prices = pd.to_numeric(cdf[price_c].astype(str).str.replace(',', '').str.replace('$', '').str.strip(), errors='coerce').values
        vols = pd.to_numeric(cdf['Volume'].astype(str).str.replace(',', '').str.replace('$', '').str.strip(), errors='coerce').values
        highs, lows = None, None
        if 'High' in cdf.columns and 'Low' in cdf.columns:
            highs = pd.to_numeric(cdf['High'].astype(str).str.replace(',', '').str.replace('$', '').str.strip(), errors='coerce').values
            lows = pd.to_numeric(cdf['Low'].astype(str).str.replace(',', '').str.replace('$', '').str.strip(), errors='coerce').values

        valid = ~np.isnan(prices) & ~np.isnan(vols) & (prices > 0)
        prices = prices[valid]
        vols = vols[valid]
        if highs is not None and lows is not None and len(highs) == len(valid):
            highs = highs[valid]
            lows = lows[valid]
        else:
            highs, lows = None, None

        if len(prices) < 5:
            return None

        rec = {'Ticker': t_clean, 'Hist_Days_Count': len(prices)}

        # Full available window
        full_w = compute_window_features(prices, vols, len(prices), highs, lows)
        for k, v in full_w.items():
            rec[f'Full_{k}'] = v

        # Rolling sub-windows: 30D, 65D (~13W), 130D (~26W), 250D (~52W)
        for w_size in [30, 65, 130, 250]:
            w_feats = compute_window_features(prices, vols, w_size, highs, lows)
            rec.update(w_feats)

        # Sequence correlation features
        if len(prices) >= 10:
            p_rets = np.diff(prices) / np.where(prices[:-1] == 0, 1.0, prices[:-1])
            vol_tail = vols[1:]
            mean_vol = max(1, np.mean(vols))
            vol_ratios = vol_tail / mean_vol

            up_mask = p_rets > 0
            dn_mask = p_rets < 0

            rec['Up_Day_Avg_Vol_Ratio'] = round(float(np.mean(vol_ratios[up_mask])), 3) if np.any(up_mask) else 1.0
            rec['Dn_Day_Avg_Vol_Ratio'] = round(float(np.mean(vol_ratios[dn_mask])), 3) if np.any(dn_mask) else 1.0
            if len(p_rets) > 2 and np.std(p_rets) > 0 and np.std(vol_ratios) > 0:
                rec['Price_Vol_Corr'] = round(float(np.corrcoef(p_rets, vol_ratios)[0, 1]), 3)

        return rec

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(process_single_ticker, tickers))

    records = [r for r in results if r is not None]
    print(f"✓ Computed trailing historical volume features for {len(records):,} tickers in parallel!")
    return pd.DataFrame(records)



def rs_rating_section(df: pd.DataFrame, repo_dir: Path, output_report: list):
    """
    Section 5: Reverse-engineer the IBD RS Rating using historical close prices vs SPY.
    Enforcing 1M RS rating recency weighting (1M >= 3M >= 6M >= 9M >= 12M).
    """
    from concurrent.futures import ThreadPoolExecutor
    from scipy.optimize import minimize

    cache_dir = repo_dir / "ticker_cache"

    # ── Load SPY reference prices ──────────────────────────────────────────
    spy_path = cache_dir / "SPY_1d.parquet"
    if not spy_path.exists():
        spy_path = cache_dir / "SPY_250d.parquet"
    if not spy_path.exists():
        print("ERROR: No SPY parquet file found in ticker_cache. Skipping RS section.")
        return

    spy_df = pd.read_parquet(spy_path)
    if isinstance(spy_df.columns, pd.MultiIndex):
        spy_df.columns = spy_df.columns.droplevel(1)
    spy_df.index = pd.to_datetime(spy_df.index, utc=True)
    spy_close = spy_df['Close'].dropna().astype(float)
    spy_close = spy_close[spy_close > 0]
    print(f"SPY reference: {len(spy_close)} trading days ({spy_close.index[0].date()} -> {spy_close.index[-1].date()})")

    # ── Define performance windows (trading days) ──────────────────────────
    windows = {
        '1M': 21,
        '3M': 63,
        '6M': 126,
        '9M': 188,
        '12M': 249
    }

    # ── Compute SPY returns for each window ────────────────────────────────
    spy_latest = float(spy_close.iloc[-1])
    spy_perf = {}
    for label, days in windows.items():
        if len(spy_close) > days:
            spy_past = float(spy_close.iloc[-(days+1)])
            spy_perf[label] = spy_latest / spy_past
        else:
            spy_perf[label] = 1.0
    print(f"SPY performance ratios: {', '.join(f'{k}={v:.4f}' for k,v in spy_perf.items())}")

    # ── Load ticker prices and compute relative performance vs SPY ─────────
    ms_symbols = [str(s).strip() for s in df['Symbol'].dropna().unique() if str(s).strip()]
    print(f"Computing relative performance for {len(ms_symbols):,} tickers vs SPY...")

    def compute_ticker_rs(ticker):
        t_clean = str(ticker).strip()
        cdf = None
        for p_cand in [
            cache_dir / f"{t_clean}_250d.parquet",
            cache_dir / f"{t_clean}_1d.parquet",
            cache_dir / f"{t_clean.replace('.', '-')}_250d.parquet",
        ]:
            if p_cand.exists():
                try:
                    cdf = pd.read_parquet(p_cand)
                    if isinstance(cdf.columns, pd.MultiIndex):
                        cdf.columns = cdf.columns.droplevel(1)
                    break
                except Exception:
                    pass

        if cdf is None or cdf.empty or len(cdf) < 10:
            return None

        price_c = 'Close' if 'Close' in cdf.columns else ('Price' if 'Price' in cdf.columns else None)
        if not price_c:
            return None

        prices = pd.to_numeric(
            cdf[price_c].astype(str).str.replace(',', '').str.replace('$', '').str.strip(),
            errors='coerce'
        ).values
        prices = prices[~np.isnan(prices) & (prices > 0)]

        if len(prices) < 10:
            return None

        latest = prices[-1]
        rec = {'Ticker': t_clean, 'Price_Days': len(prices)}

        for label, days in windows.items():
            if len(prices) > days:
                past_price = prices[-(days+1)]
                stock_perf = latest / past_price
                rel_perf = stock_perf / spy_perf[label]
                rec[f'Rel_Perf_{label}'] = rel_perf
                rec[f'Stock_Ret_{label}'] = (stock_perf - 1) * 100
                rec[f'Excess_Ret_{label}'] = (rel_perf - 1) * 100
            else:
                rec[f'Rel_Perf_{label}'] = np.nan
                rec[f'Stock_Ret_{label}'] = np.nan
                rec[f'Excess_Ret_{label}'] = np.nan

        # Exponentially-weighted recent momentum features
        for decay in [0.005, 0.01, 0.02]:
            n_days = min(len(prices) - 1, 249)
            if n_days >= 60:
                daily_rets = np.diff(prices) / np.where(prices[:-1] == 0, 1.0, prices[:-1])
                daily_rets = daily_rets[-n_days:]
                w = np.exp(-decay * np.arange(n_days)[::-1])
                w = w / w.sum()
                rec[f'EMA_Ret_d{decay}'] = float(np.sum(daily_rets * w))

        return rec

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(compute_ticker_rs, ms_symbols))

    rs_records = [r for r in results if r is not None]
    df_rs = pd.DataFrame(rs_records)
    print(f"✓ Computed relative performance features for {len(df_rs):,} tickers")

    # ── Merge with marketsurge RS Ratings ───────────────────────────────────
    merged = df[['Symbol', 'RS Rating', 'RS 3-Month Rating', 'RS 6-Month Rating']].copy()
    merged = merged.merge(df_rs, left_on='Symbol', right_on='Ticker', how='inner')
    merged = merged.dropna(subset=['RS Rating'])
    print(f"Merged dataset with RS Ratings: {len(merged):,} stocks")

    required_perf_cols = [f'Rel_Perf_{w}' for w in ['1M', '3M', '6M', '9M', '12M']]
    merged_full = merged.dropna(subset=required_perf_cols).copy()
    print(f"Stocks with all 5 windows: {len(merged_full):,}")

    # ── Approach 1: Monotonic Recency & 1M Recency-Heavy Weight Presets ─────
    print("\n" + "-"*60)
    print("Approach 1: Monotonic Recency & 1M Recency-Heavy Weight Schemes")
    print("-"*60)

    weight_configs = {
        '1M_Heavy (35/25/20/12/8)':       {'1M': 0.35, '3M': 0.25, '6M': 0.20, '9M': 0.12, '12M': 0.08},
        'Linear_Decay (33.3/26.7/20/13.3/6.7)': {'1M': 5/15, '3M': 4/15, '6M': 3/15, '9M': 2/15, '12M': 1/15},
        'Moderate_Recency (30/25/20/15/10)':    {'1M': 0.30, '3M': 0.25, '6M': 0.20, '9M': 0.15, '12M': 0.10},
        '1M_Dominant (40/30/15/10/5)':    {'1M': 0.40, '3M': 0.30, '6M': 0.15, '9M': 0.10, '12M': 0.05},
        'Equal_Weight (20/20/20/20/20)':  {'1M': 0.20, '3M': 0.20, '6M': 0.20, '9M': 0.20, '12M': 0.20},
        'Pine_Original (0/40/20/20/20)':  {'1M': 0.00, '3M': 0.40, '6M': 0.20, '9M': 0.20, '12M': 0.20},
    }

    weighted_results = []
    for config_name, weights in weight_configs.items():
        cols = [f'Rel_Perf_{k}' for k in ['1M', '3M', '6M', '9M', '12M']]
        ws = np.array([weights.get(k, 0) for k in ['1M', '3M', '6M', '9M', '12M']])
        perf_vals = merged_full[cols].values
        raw_scores = perf_vals @ ws * 100

        ranks = pd.Series(raw_scores).rank(pct=True) * 99
        ranks = np.clip(ranks, 1, 99)

        r2 = r2_score(merged_full['RS Rating'], ranks)
        mae = mean_absolute_error(merged_full['RS Rating'], ranks)
        corr = np.corrcoef(merged_full['RS Rating'], ranks)[0, 1]

        weighted_results.append({
            'Weight Config': config_name,
            '1M': round(weights.get('1M', 0), 4),
            '3M': round(weights.get('3M', 0), 4),
            '6M': round(weights.get('6M', 0), 4),
            '9M': round(weights.get('9M', 0), 4),
            '12M': round(weights.get('12M', 0), 4),
            'R²': round(r2, 4),
            'MAE': round(mae, 2),
            'Correlation': round(corr, 4)
        })
        print(f"  {config_name}: R²={r2:.4f}, MAE={mae:.2f}, Corr={corr:.4f}")

    weighted_df = pd.DataFrame(weighted_results)

    # ── Approach 2: Monotonic Recency Optimization (1M >= 3M >= 6M >= 9M >= 12M) ──
    print("\n" + "-"*60)
    print("Approach 2: Monotonic Recency Constrained Optimization (1M >= 3M >= 6M >= 9M >= 12M)")
    print("-"*60)

    y_true_rs = merged_full['RS Rating'].values
    perf_matrix = merged_full[required_perf_cols].values

    def mono_recency_objective(params):
        v = np.abs(params)
        w5 = v[4]
        w4 = w5 + v[3]
        w3 = w4 + v[2]
        w2 = w3 + v[1]
        w1 = w2 + v[0]
        w = np.array([w1, w2, w3, w4, w5])
        w = w / w.sum()
        raw = perf_matrix @ w * 100
        ranks = pd.Series(raw).rank(pct=True).values * 99
        ranks = np.clip(ranks, 1, 99)
        return np.mean(np.abs(y_true_rs - ranks))

    res_mono = minimize(mono_recency_objective, [0.1, 0.1, 0.1, 0.1, 0.1], method='Nelder-Mead',
                        options={'maxiter': 5000, 'xatol': 1e-6, 'fatol': 1e-6})

    v = np.abs(res_mono.x)
    w5 = v[4]; w4 = w5 + v[3]; w3 = w4 + v[2]; w2 = w3 + v[1]; w1 = w2 + v[0]
    opt_w_mono = np.array([w1, w2, w3, w4, w5])
    opt_w_mono = opt_w_mono / opt_w_mono.sum()
    opt_labels = ['1M', '3M', '6M', '9M', '12M']

    print(f"Optimal Monotonic Recency Weights: {', '.join(f'{l}={w:.4f}' for l, w in zip(opt_labels, opt_w_mono))}")

    raw_mono = perf_matrix @ opt_w_mono * 100
    ranks_mono = pd.Series(raw_mono).rank(pct=True).values * 99
    ranks_mono = np.clip(ranks_mono, 1, 99)
    r2_mono = r2_score(y_true_rs, ranks_mono)
    mae_mono = mean_absolute_error(y_true_rs, ranks_mono)
    corr_mono = np.corrcoef(y_true_rs, ranks_mono)[0, 1]

    print(f"Monotonic Constrained R²={r2_mono:.4f}, MAE={mae_mono:.2f}, Corr={corr_mono:.4f}")

    # ── Approach 3: Exponential Recency Decay Optimization ─────────────────
    print("\n" + "-"*60)
    print("Approach 3: Parametric Exponential Recency Decay Optimization")
    print("-"*60)

    def exp_recency_decay_objective(beta):
        k = np.array([0, 1, 2, 3, 4])  # 1M=0, 3M=1, 6M=2, 9M=3, 12M=4
        w = np.exp(-abs(beta[0]) * k)
        w = w / w.sum()
        raw = perf_matrix @ w * 100
        ranks = pd.Series(raw).rank(pct=True).values * 99
        ranks = np.clip(ranks, 1, 99)
        return np.mean(np.abs(y_true_rs - ranks))

    res_exp = minimize(exp_recency_decay_objective, [0.15], method='Nelder-Mead',
                       options={'maxiter': 3000, 'xatol': 1e-8, 'fatol': 1e-8})

    opt_beta = abs(res_exp.x[0])
    k_steps = np.array([0, 1, 2, 3, 4])
    exp_weights = np.exp(-opt_beta * k_steps)
    exp_weights = exp_weights / exp_weights.sum()

    print(f"Optimal Recency Decay β = {opt_beta:.6f}")
    print(f"Exp Recency Weights: {', '.join(f'{l}={w:.4f}' for l, w in zip(opt_labels, exp_weights))}")

    raw_exp = perf_matrix @ exp_weights * 100
    ranks_exp = pd.Series(raw_exp).rank(pct=True).values * 99
    ranks_exp = np.clip(ranks_exp, 1, 99)
    r2_exp = r2_score(y_true_rs, ranks_exp)
    mae_exp = mean_absolute_error(y_true_rs, ranks_exp)
    corr_exp = np.corrcoef(y_true_rs, ranks_exp)[0, 1]

    print(f"Exp Recency R²={r2_exp:.4f}, MAE={mae_exp:.2f}, Corr={corr_exp:.4f}")

    # ── Approach 4: ML Regression Models (Feature-Rich) ───────────────────
    print("\n" + "-"*60)
    print("Approach 4: ML Regression Models (Feature-Rich)")
    print("-"*60)

    ml_feature_candidates = (
        [f'Rel_Perf_{w}' for w in ['1M', '3M', '6M', '9M', '12M']] +
        [f'Stock_Ret_{w}' for w in ['1M', '3M', '6M', '9M', '12M']] +
        [f'Excess_Ret_{w}' for w in ['1M', '3M', '6M', '9M', '12M']] +
        [f'EMA_Ret_d{d}' for d in [0.005, 0.01, 0.02]]
    )
    ml_features = [c for c in ml_feature_candidates if c in merged_full.columns and merged_full[c].notna().sum() > 50]

    X_rs = merged_full[ml_features].copy()
    y_rs = merged_full['RS Rating'].values

    imputer_rs = SimpleImputer(strategy='median')
    X_rs_imp = imputer_rs.fit_transform(X_rs)

    X_train_rs, X_test_rs, y_train_rs, y_test_rs = train_test_split(
        X_rs_imp, y_rs, test_size=0.2, random_state=42
    )

    scaler_rs = StandardScaler()
    X_train_rs_std = scaler_rs.fit_transform(X_train_rs)
    X_test_rs_std = scaler_rs.transform(X_test_rs)

    ml_models_rs = {
        'Ridge (α=50)': Ridge(alpha=50.0),
        'Linear Regression': LinearRegression(),
        'HistGradientBoosting': HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=42),
        'Random Forest': RandomForestRegressor(n_estimators=150, max_depth=15, random_state=42, n_jobs=-1),
        'ExtraTrees': ExtraTreesRegressor(n_estimators=150, max_depth=15, random_state=42, n_jobs=-1),
    }

    ml_results = []
    best_ml_model = None
    best_ml_r2 = -999

    for name, model in ml_models_rs.items():
        if 'Linear' in name or 'Ridge' in name:
            model.fit(X_train_rs_std, y_train_rs)
            y_pred = model.predict(X_test_rs_std)
        else:
            model.fit(X_train_rs, y_train_rs)
            y_pred = model.predict(X_test_rs)

        r2 = r2_score(y_test_rs, y_pred)
        mae = mean_absolute_error(y_test_rs, y_pred)

        within_5 = (np.abs(y_test_rs - y_pred) <= 5).mean() * 100
        within_10 = (np.abs(y_test_rs - y_pred) <= 10).mean() * 100

        ml_results.append({
            'Model': name,
            'R²': round(r2, 4),
            'MAE': round(mae, 2),
            '±5 Acc (%)': round(within_5, 2),
            '±10 Acc (%)': round(within_10, 2)
        })

        if r2 > best_ml_r2:
            best_ml_r2 = r2
            best_ml_model = (name, model)

        print(f"  {name}: R²={r2:.4f}, MAE={mae:.2f}, ±5={within_5:.1f}%, ±10={within_10:.1f}%")

    ml_results_df = pd.DataFrame(ml_results)

    # ── Build report ──────────────────────────────────────────────────────
    output_report.append("## 5. RS Rating Model (Historical Price vs SPY - Recency-First Weighting)\n")
    output_report.append(f"- **Total Stocks with Price History**: `{len(df_rs):,}`")
    output_report.append(f"- **Merged with RS Ratings**: `{len(merged_full):,}` stocks")
    output_report.append(f"- **SPY Baseline**: `{len(spy_close)}` trading days\n")

    output_report.append("### A. Recency-Weighted RS Score Schemes (Enforcing 1M RS Input)\n")
    output_report.append(weighted_df.to_markdown(index=False))
    output_report.append("\n")

    output_report.append("### B. Monotonic Recency Constrained Optimization (1M ≥ 3M ≥ 6M ≥ 9M ≥ 12M)\n")
    opt_weights_df = pd.DataFrame({
        'Window': opt_labels,
        'Monotonic Constrained Weight': np.round(opt_w_mono, 4),
        'Exponential Decay Weight (β={:.4f})'.format(opt_beta): np.round(exp_weights, 4),
        '1M Heavy Preset (35/25/20/12/8)': [0.35, 0.25, 0.20, 0.12, 0.08],
        'Moderate Recency Preset (30/25/20/15/10)': [0.30, 0.25, 0.20, 0.15, 0.10],
    })
    output_report.append(opt_weights_df.to_markdown(index=False))
    output_report.append(f"\n- **Monotonic Constrained Optimization**: R²=`{r2_mono:.4f}`, MAE=`{mae_mono:.2f}`, Corr=`{corr_mono:.4f}`")
    output_report.append(f"- **Exponential Recency Decay (β={opt_beta:.4f})**: R²=`{r2_exp:.4f}`, MAE=`{mae_exp:.2f}`, Corr=`{corr_exp:.4f}`\n")

    output_report.append("### C. ML Regression Models (Feature-Rich)\n")
    output_report.append(ml_results_df.to_markdown(index=False))
    output_report.append(f"\n**Best ML Model**: `{best_ml_model[0]}` (R²=`{best_ml_r2:.4f}`)\n")

    output_report.append("### D. Recommended RS Formulae for Pine Script & Python\n")
    output_report.append("```text")
    output_report.append("// 1. Monotonic Recency Constrained Weights (1M >= 3M >= 6M >= 9M >= 12M)")
    output_report.append(f"rs_stock = {opt_w_mono[0]:.4f} * perf_1M + {opt_w_mono[1]:.4f} * perf_3M + {opt_w_mono[2]:.4f} * perf_6M + {opt_w_mono[3]:.4f} * perf_9M + {opt_w_mono[4]:.4f} * perf_12M")
    output_report.append("")
    output_report.append(f"// 2. Exponential Recency Decay Weights (β={opt_beta:.4f})")
    output_report.append(f"rs_stock = {exp_weights[0]:.4f} * perf_1M + {exp_weights[1]:.4f} * perf_3M + {exp_weights[2]:.4f} * perf_6M + {exp_weights[3]:.4f} * perf_9M + {exp_weights[4]:.4f} * perf_12M")
    output_report.append("")
    output_report.append("// 3. Moderate Recency Preset (Clean 30/25/20/15/10 Weighting)")
    output_report.append("rs_stock = 0.30 * perf_1M + 0.25 * perf_3M + 0.20 * perf_6M + 0.15 * perf_9M + 0.10 * perf_12M")
    output_report.append("```\n")

    print(f"\n{'='*60}")
    print("RS Rating Section Complete!")
    print(f"{'='*60}")


def run_pipeline():
    repo_dir = Path(__file__).resolve().parent.parent
    csv_path = repo_dir / "IBD" / "marketsurge.csv"
    rs_stocks_file = repo_dir / "output" / "rs_stocks.csv"
    hist_file = repo_dir / "output" / "rs_stocks_historical.csv"

    if not csv_path.exists():
        print(f"Error: CSV file not found at {csv_path}")
        return

    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path, low_memory=False)
    print(f"Dataset shape: {df.shape}")

    non_numeric_cols = ['#', 'Symbol', 'Name', 'EPS Due Date', 'EPS Lst Rptd', 'Company Description', 'SMR Rating', 'A/D Rating', 'Ind Group RS']
    numeric_cols = [c for c in df.columns if c not in non_numeric_cols]

    for c in numeric_cols:
        df[c] = df[c].apply(clean_num)

    df['SMR_Num'] = df['SMR Rating'].apply(smr_grade_to_num)
    df['AD_Num']  = df['A/D Rating'].apply(grade_to_num)
    df['AD_Subtier'] = df['A/D Rating'].apply(ad_subtier)
    df['AD_Tier'] = df['A/D Rating'].apply(ad_5tier)
    df['GroupRS_Num'] = df['Ind Group RS'].apply(grade_to_num)

    output_report = []
    output_report.append("# Reverse Engineering IBD / MarketSurge Ratings Report\n")
    output_report.append(f"**Dataset**: `{csv_path}` | **Total Stock Records**: {len(df):,}\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. EPS RATING MODEL
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("1. REVERSE ENGINEERING EPS RATING")
    print("="*80)

    eps_candidates = [
        'EPS % Growth 5 Yr', 'EPS % Growth 3 Yr', 'EPS % Growth 1 Yr',
        'EPS % Chg 1 Yr Ago', 'EPS % Chg Lst Yr', 'EPS Surprise',
        'EPS Est Cur Qtr %', 'Avg EPS % Chg 6Q', 'Avg EPS % Chg 5Q',
        'Avg EPS % Chg 4Q', 'Avg EPS % Chg 3Q', 'Avg EPS % Chg 2Q',
        'EPS % Chg 3 Q Ago (-/+)', 'EPS Accel 3 Qtrs', 'EPS % Chg 2 Q Ago (-/+)',
        'EPS % Chg 1 Q Ago (-/+)', 'EPS % Chg Last Qtr (-/+)',
        'ROE', 'ROE 5-Yr Avg', 'AT Margin', 'Pre-tax Margins'
    ]

    eps_features = [c for c in eps_candidates if df[c].notna().sum() > 50]

    df_eps = df[['EPS Rating'] + eps_features].dropna(subset=['EPS Rating'])
    X_eps = df_eps[eps_features].copy()
    y_eps = df_eps['EPS Rating']

    for c in eps_features:
        X_eps[c] = np.clip(X_eps[c], -300, 300)

    imputer_eps = SimpleImputer(strategy='median')
    X_eps_imp = imputer_eps.fit_transform(X_eps)
    X_train_eps, X_test_eps, y_train_eps, y_test_eps = train_test_split(X_eps_imp, y_eps, test_size=0.2, random_state=42)

    scaler_eps = StandardScaler()
    X_train_eps_std = scaler_eps.fit_transform(X_train_eps)
    X_test_eps_std  = scaler_eps.transform(X_test_eps)

    lr_eps = Ridge(alpha=50.0)
    lr_eps.fit(X_train_eps_std, y_train_eps)
    y_pred_eps = lr_eps.predict(X_test_eps_std)

    r2_eps = r2_score(y_test_eps, y_pred_eps)
    mae_eps = mean_absolute_error(y_test_eps, y_pred_eps)

    rf_eps = RandomForestRegressor(n_estimators=50, max_depth=12, n_jobs=-1, random_state=42)
    rf_eps.fit(X_train_eps_std, y_train_eps)
    rf_r2_eps = r2_score(y_test_eps, rf_eps.predict(X_test_eps_std))

    print(f"EPS Rating Model - Ridge R²: {r2_eps:.4f} | MAE: {mae_eps:.2f}")
    print(f"EPS Rating Model - Random Forest R²: {rf_r2_eps:.4f}")

    eps_weights = pd.DataFrame({
        'Feature': eps_features,
        'Std_Coef': lr_eps.coef_,
        'Abs_Coef': np.abs(lr_eps.coef_),
        'RF_Importance': rf_eps.feature_importances_
    }).sort_values(by='RF_Importance', ascending=False)

    eps_weights['Rel_Weight_Pct'] = (eps_weights['Abs_Coef'] / eps_weights['Abs_Coef'].sum()) * 100

    output_report.append("## 1. EPS Rating Model\n")
    output_report.append(f"- **Ridge Regression $R^2$**: `{r2_eps:.4f}` | **MAE**: `{mae_eps:.2f}` points")
    output_report.append(f"- **Random Forest $R^2$**: `{rf_r2_eps:.4f}`\n")
    output_report.append("### Feature Importances & Standardized Weights for EPS Rating\n")
    output_report.append(eps_weights[['Feature', 'Std_Coef', 'Rel_Weight_Pct', 'RF_Importance']].to_markdown(index=False))
    output_report.append("\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. SMR RATING MODEL
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("2. REVERSE ENGINEERING SMR RATING")
    print("="*80)

    smr_candidates = [
        'Sales Growth 5 Yr', 'Sales Growth 3 Yr', 'Sales % Chg Lst Yr',
        'Avg Sales % Chg 6Q', 'Avg Sales % Chg 5Q', 'Avg Sales % Chg 4Q',
        'Avg Sales % Chg 3Q', 'Avg Sales % Chg 2Q',
        'Sales % Chg Lst Qtr', 'Sales Accel 3 Qtrs',
        'AT Margin', 'Pre-tax Margins', 'Avg AT Margin 6Q', 'Avg AT Margin 5Q',
        'Avg AT Margin 4Q', 'Avg AT Margin 3Q', 'Avg AT Margin 2Q',
        'ROE 5-Yr Avg', 'ROE'
    ]

    smr_features = [c for c in smr_candidates if df[c].notna().sum() > 50]

    df_smr = df[['SMR_Num'] + smr_features].dropna(subset=['SMR_Num'])
    X_smr = df_smr[smr_features].copy()
    y_smr = df_smr['SMR_Num']

    for c in smr_features:
        X_smr[c] = np.clip(X_smr[c], -200, 200)

    imputer_smr = SimpleImputer(strategy='median')
    X_smr_imp = imputer_smr.fit_transform(X_smr)
    X_train_smr, X_test_smr, y_train_smr, y_test_smr = train_test_split(X_smr_imp, y_smr, test_size=0.2, random_state=42)

    scaler_smr = StandardScaler()
    X_train_smr_std = scaler_smr.fit_transform(X_train_smr)
    X_test_smr_std  = scaler_smr.transform(X_test_smr)

    lr_smr = Ridge(alpha=50.0)
    lr_smr.fit(X_train_smr_std, y_train_smr)
    y_pred_smr = lr_smr.predict(X_test_smr_std)

    r2_smr = r2_score(y_test_smr, y_pred_smr)
    mae_smr = mean_absolute_error(y_test_smr, y_pred_smr)

    rf_smr = RandomForestRegressor(n_estimators=40, max_depth=12, n_jobs=-1, random_state=42)
    rf_smr.fit(X_train_smr_std, y_train_smr)
    rf_r2_smr = r2_score(y_test_smr, rf_smr.predict(X_test_smr_std))

    print(f"SMR Rating Model - Ridge R²: {r2_smr:.4f} | MAE: {mae_smr:.2f}")
    print(f"SMR Rating Model - Random Forest R²: {rf_r2_smr:.4f}")

    smr_weights = pd.DataFrame({
        'Feature': smr_features,
        'Std_Coef': lr_smr.coef_,
        'Abs_Coef': np.abs(lr_smr.coef_),
        'RF_Importance': rf_smr.feature_importances_
    }).sort_values(by='Abs_Coef', ascending=False)

    smr_weights['Rel_Weight_Pct'] = (smr_weights['Abs_Coef'] / smr_weights['Abs_Coef'].sum()) * 100

    def categorize_pillar(feat):
        if 'Sales' in feat: return 'Sales Growth'
        if 'Margin' in feat: return 'Profit Margin'
        if 'ROE' in feat: return 'ROE'
        return 'Other'

    smr_weights['Pillar'] = smr_weights['Feature'].apply(categorize_pillar)
    pillar_summary = smr_weights.groupby('Pillar')['Rel_Weight_Pct'].sum().reset_index().sort_values(by='Rel_Weight_Pct', ascending=False)

    output_report.append("## 2. SMR Rating Model\n")
    output_report.append(f"- **Ridge Regression $R^2$**: `{r2_smr:.4f}` | **MAE**: `{mae_smr:.2f}` points")
    output_report.append(f"- **Random Forest $R^2$**: `{rf_r2_smr:.4f}`\n")
    output_report.append("### SMR Rating 3-Pillar Breakdown\n")
    output_report.append(pillar_summary.to_markdown(index=False))
    output_report.append("\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. ACCUMULATION / DISTRIBUTION (A/D) RATING MODELS (5-TIER FOUNDATION + POST-REGRESSION 12-TIER SYSTEM)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("3. REVERSE ENGINEERING ACCUMULATION / DISTRIBUTION (A/D) RATING")
    print("   - Core Foundation: 5-Tier Regression & Classification Models (A, B, C, D, E)")
    print("   - Post-Regression Scoring: Converting Continuous Regression Output to 12 Sub-Tiers")
    print("="*80)

    ms_tickers = [str(s).strip() for s in df['Symbol'].dropna().unique() if str(s).strip()]
    df_365_feat = compute_historical_heavy_volume_features(ms_tickers, repo_dir, hist_file)

    merged_ad = df.merge(df_365_feat, left_on='Symbol', right_on='Ticker', how='inner')
    print(f"Merged A/D evaluation dataset: {len(merged_ad):,} stocks")

    # Additional interaction & rank features
    if 'Price vs 50-Day' in merged_ad.columns:
        merged_ad['Rank_Price_50D'] = merged_ad['Price vs 50-Day'].rank(pct=True) * 100
    if 'Up/Down Vol' in merged_ad.columns:
        merged_ad['Rank_Up_Down_Vol'] = merged_ad['Up/Down Vol'].rank(pct=True) * 100
    if 'Net_Heavy_Intensity_65D' in merged_ad.columns:
        merged_ad['Rank_Net_Heavy_Intensity_65D'] = merged_ad['Net_Heavy_Intensity_65D'].rank(pct=True) * 100
        if 'Price vs 50-Day' in merged_ad.columns:
            merged_ad['Inter_50D_Heavy'] = merged_ad['Price vs 50-Day'] * merged_ad['Net_Heavy_Intensity_65D']

    # Selected feature list
    candidate_ad_features = [
        'Hist_Days_Count',
        'Full_Price_Pct_Chg_250D', 'Full_Up_Dn_Vol_250D', 'Full_Heavy_Net_Ratio_250D',
        'Full_Net_Heavy_Days_250D', 'Full_Net_Heavy_Intensity_250D',
        'Price_Pct_Chg_30D', 'Up_Dn_Vol_30D', 'Heavy_Net_Ratio_30D', 'Net_Heavy_Days_30D', 'Net_Heavy_Intensity_30D', 'Avg_Closing_Range_30D', 'Vol_Weighted_Closing_Range_30D',
        'Price_Pct_Chg_65D', 'Up_Dn_Vol_65D', 'Heavy_Net_Ratio_65D', 'Net_Heavy_Days_65D', 'Net_Heavy_Intensity_65D', 'Avg_Closing_Range_65D', 'Vol_Weighted_Closing_Range_65D',
        'Price_Pct_Chg_130D', 'Up_Dn_Vol_130D', 'Heavy_Net_Ratio_130D', 'Net_Heavy_Days_130D', 'Net_Heavy_Intensity_130D', 'Avg_Closing_Range_130D', 'Vol_Weighted_Closing_Range_130D',
        'Price_Pct_Chg_250D', 'Up_Dn_Vol_250D', 'Heavy_Net_Ratio_250D', 'Net_Heavy_Days_250D', 'Net_Heavy_Intensity_250D', 'Avg_Closing_Range_250D', 'Vol_Weighted_Closing_Range_250D',
        'Up_Day_Avg_Vol_Ratio', 'Dn_Day_Avg_Vol_Ratio', 'Price_Vol_Corr',
        'Rank_Price_50D', 'Rank_Up_Down_Vol', 'Rank_Net_Heavy_Intensity_65D', 'Inter_50D_Heavy',
        'Price vs 10-Day', 'Price vs 21-Day', 'Price vs 50-Day', 'Price vs 150-Day', 'Price vs 200-Day',
        'Up/Down Vol', 'Daily Closing Range', 'Vol % Chg vs 50-Day', '21 Day ATR %', '30 Day ATR %',
        'Number of Funds', 'Funds %', 'Funds % Increase'
    ]

    ad_features = [c for c in candidate_ad_features if c in merged_ad.columns]

    sub_ad = merged_ad[ad_features + ['AD_Num', 'AD_Subtier', 'AD_Tier']].dropna(subset=['AD_Num', 'AD_Tier'])
    X_ad = sub_ad[ad_features].copy()
    for c in ad_features:
        X_ad[c] = X_ad[c].apply(clean_num)

    y_ad_num = sub_ad['AD_Num']           # Continuous 1..13
    y_ad_tier = sub_ad['AD_Tier']         # 5 Main tier strings (A, B, C, D, E)
    y_ad_subtier = sub_ad['AD_Subtier']   # Ground truth sub-tier strings

    imputer_ad = SimpleImputer(strategy='median')
    X_ad_imp = imputer_ad.fit_transform(X_ad)

    X_train_ad, X_test_ad, y_train_ad_num, y_test_ad_num, y_train_ad_tier, y_test_ad_tier = train_test_split(
        X_ad_imp, y_ad_num, y_ad_tier, test_size=0.2, random_state=42, stratify=y_ad_tier
    )

    scaler_ad = StandardScaler()
    X_train_ad_std = scaler_ad.fit_transform(X_train_ad)
    X_test_ad_std  = scaler_ad.transform(X_test_ad)

    # -----------------------------------------------------------------------
    # Part 1: Core 5-Tier Models (A, B, C, D, E)
    # -----------------------------------------------------------------------
    print("\n" + "-"*60)
    print("Part 1: 5-Tier Models (Regression -> 5-Tier vs Direct Classification)")
    print("-"*60)

    reg_models = {
        'HistGradientBoosting Regressor': HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=42),
        'Random Forest Regressor': RandomForestRegressor(n_estimators=150, max_depth=15, random_state=42, n_jobs=-1),
        'ExtraTrees Regressor': ExtraTreesRegressor(n_estimators=150, max_depth=15, random_state=42, n_jobs=-1),
        'Ridge Regression (alpha=50)': Ridge(alpha=50.0),
        'Linear Regression': LinearRegression()
    }

    reg_results = []
    best_reg_model = None
    best_reg_tier_acc = 0.0
    best_y_pred_num = None

    for name, m in reg_models.items():
        if 'Linear' in name or 'Ridge' in name:
            m.fit(X_train_ad_std, y_train_ad_num)
            y_pred_num = m.predict(X_test_ad_std)
        else:
            m.fit(X_train_ad, y_train_ad_num)
            y_pred_num = m.predict(X_test_ad)

        r2 = r2_score(y_test_ad_num, y_pred_num)
        mae = mean_absolute_error(y_test_ad_num, y_pred_num)

        # Convert continuous predictions to 5-tier A/B/C/D/E
        pred_converted_tier = pd.Series(y_pred_num, index=y_test_ad_tier.index).apply(num_to_5tier)

        exact_acc = accuracy_score(y_test_ad_tier, pred_converted_tier) * 100
        macro_f1 = f1_score(y_test_ad_tier, pred_converted_tier, average='macro') * 100

        te_num = y_test_ad_tier.map(MAIN_TIER_ORDER)
        pr_num = pred_converted_tier.map(MAIN_TIER_ORDER)
        within_1_tier_acc = (np.abs(te_num - pr_num) <= 1).mean() * 100

        reg_results.append({
            'Model Paradigm': 'Regression -> 5-Tier',
            'Model Name': name,
            'R² Score': round(r2, 4),
            'MAE (Grade Pts)': round(mae, 2),
            'Exact 5-Tier Acc (%)': round(exact_acc, 2),
            'Within 1 Tier Acc (%)': round(within_1_tier_acc, 2),
            'Macro F1 (%)': round(macro_f1, 2)
        })

        if exact_acc > best_reg_tier_acc:
            best_reg_tier_acc = exact_acc
            best_reg_model = (name, m)
            best_y_pred_num = y_pred_num

    reg_df = pd.DataFrame(reg_results)
    print(reg_df.to_string(index=False))

    clf_models = {
        'HistGradientBoosting Classifier': HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=42),
        'Random Forest Classifier': RandomForestClassifier(n_estimators=150, max_depth=15, random_state=42, n_jobs=-1),
        'ExtraTrees Classifier': ExtraTreesClassifier(n_estimators=150, max_depth=15, random_state=42, n_jobs=-1),
        'Logistic Regression': LogisticRegression(max_iter=1000, C=1.0, random_state=42)
    }

    clf_results = []
    best_clf_model = None
    best_clf_acc = 0.0
    best_clf_y_pred = None

    for name, m in clf_models.items():
        if 'Logistic' in name:
            m.fit(X_train_ad_std, y_train_ad_tier)
            y_pred_cls = m.predict(X_test_ad_std)
        else:
            m.fit(X_train_ad, y_train_ad_tier)
            y_pred_cls = m.predict(X_test_ad)

        exact_acc = accuracy_score(y_train_ad_tier.iloc[:len(y_pred_cls)], y_pred_cls) * 100 if len(y_train_ad_tier) == len(y_pred_cls) else accuracy_score(y_test_ad_tier, y_pred_cls) * 100
        macro_f1 = f1_score(y_test_ad_tier, y_pred_cls, average='macro') * 100

        te_num = y_test_ad_tier.map(MAIN_TIER_ORDER)
        pr_num = pd.Series(y_pred_cls, index=y_test_ad_tier.index).map(MAIN_TIER_ORDER)
        within_1_tier_acc = (np.abs(te_num - pr_num) <= 1).mean() * 100

        clf_results.append({
            'Model Paradigm': 'Direct 5-Class Clf',
            'Model Name': name,
            'R² Score': 'N/A',
            'MAE (Grade Pts)': 'N/A',
            'Exact 5-Tier Acc (%)': round(exact_acc, 2),
            'Within 1 Tier Acc (%)': round(within_1_tier_acc, 2),
            'Macro F1 (%)': round(macro_f1, 2)
        })

        if exact_acc > best_clf_acc:
            best_clf_acc = exact_acc
            best_clf_model = (name, m)
            best_clf_y_pred = y_pred_cls

    clf_df = pd.DataFrame(clf_results)
    print(clf_df.to_string(index=False))

    combined_comparison = pd.concat([reg_df, clf_df], ignore_index=True)

    # -----------------------------------------------------------------------
    # Part 2: Post-Regression 12-Tier Scoring System
    # -----------------------------------------------------------------------
    print("\n" + "-"*60)
    print("Part 2: Post-Regression 12-Tier Scoring System")
    print("Converting Continuous Regression Score y_pred -> 12 Calibrated Sub-Tiers")
    print("-"*60)

    # Convert best continuous regression predictions into 12 Sub-Tiers
    pred_12tier = pd.Series(best_y_pred_num, index=y_test_ad_tier.index).apply(num_to_12tier)
    y_test_subtier = sub_ad.loc[y_test_ad_tier.index, 'AD_Subtier']

    # Subtier accuracy metrics
    te_sub_12 = y_test_subtier.map(SUBTIER_12_MAP)
    pr_sub_12 = pred_12tier.map(SUBTIER_12_MAP)

    exact_12tier_acc = (pred_12tier == y_test_subtier).mean() * 100
    within_1_subtier_acc = (np.abs(te_sub_12 - pr_sub_12) <= 1).mean() * 100

    # Main tier accuracy from 12-tier post-regression score
    pred_main_from_12 = pred_12tier.apply(subtier_to_maintier).map(MAIN_TIER_ORDER)
    te_main = y_test_ad_tier.map(MAIN_TIER_ORDER)
    within_1_maintier_acc_12 = (np.abs(te_main - pred_main_from_12) <= 1).mean() * 100

    post_reg_summary = pd.DataFrame([{
        'Scoring System': 'Post-Regression 12-Tier Mapping',
        'Base Regressor': best_reg_model[0],
        'Regression R²': round(r2_score(y_test_ad_num, best_y_pred_num), 4),
        'Within 1 Main-Tier Acc (%)': round(within_1_maintier_acc_12, 2),
        'Within 1 Sub-Tier Acc (%)': round(within_1_subtier_acc, 2),
        'Exact Sub-Tier Match (%)': round(exact_12tier_acc, 2)
    }])

    print(post_reg_summary.to_string(index=False))

    # Feature importances
    def get_model_feature_importances(model, X_test, y_test):
        if hasattr(model, 'feature_importances_'):
            return model.feature_importances_
        elif hasattr(model, 'coef_'):
            coef = np.abs(model.coef_)
            return coef.mean(axis=0) if coef.ndim > 1 else coef
        else:
            try:
                from sklearn.inspection import permutation_importance
                res = permutation_importance(model, X_test, y_test, n_repeats=5, random_state=42, n_jobs=-1)
                return np.maximum(0, res.importances_mean)
            except Exception:
                return np.ones(X_test.shape[1])

    best_reg_name, b_reg_m = best_reg_model
    reg_test_x = X_test_ad_std if ('Linear' in best_reg_name or 'Ridge' in best_reg_name) else X_test_ad
    reg_imps = get_model_feature_importances(b_reg_m, reg_test_x, y_test_ad_num)

    best_clf_name, b_clf_m = best_clf_model
    clf_test_x = X_test_ad_std if 'Logistic' in best_clf_name else X_test_ad
    clf_imps = get_model_feature_importances(b_clf_m, clf_test_x, y_train_ad_tier)

    feat_imp_df = pd.DataFrame({
        'Feature': ad_features,
        'Reg_Importance': reg_imps,
        'Clf_Importance': clf_imps
    })
    feat_imp_df['Reg_Weight_Pct'] = round((feat_imp_df['Reg_Importance'] / max(1e-6, feat_imp_df['Reg_Importance'].sum())) * 100, 2)
    feat_imp_df['Clf_Weight_Pct'] = round((feat_imp_df['Clf_Importance'] / max(1e-6, feat_imp_df['Clf_Importance'].sum())) * 100, 2)
    feat_imp_df = feat_imp_df.sort_values(by='Reg_Weight_Pct', ascending=False)

    clf_rep = classification_report(y_test_ad_tier, best_clf_y_pred, target_names=['A', 'B', 'C', 'D', 'E'], output_dict=True)
    clf_rep_df = pd.DataFrame(clf_rep).transpose().reset_index().rename(columns={'index': 'Tier / Metric'})

    output_report.append("## 3. Accumulation / Distribution (A/D) Rating Model (5-Tier Foundation & 12-Tier Post-Regression Scoring)\n")
    output_report.append(f"- **Evaluated Stock Dataset**: `{len(sub_ad):,}` stocks with 250D/365D Historical Price & Volume Records")
    output_report.append(f"- **Feature Count**: `{len(ad_features)}` technical, volume accumulation, and fund footprint metrics\n")
    output_report.append("### Comparative 5-Tier Performance Table (Regression -> 5-Tier vs Direct Classification)\n")
    output_report.append(combined_comparison.to_markdown(index=False))
    output_report.append("\n")
    output_report.append("### Post-Regression 12-Tier Sub-Tier Scoring System\n")
    output_report.append(post_reg_summary.to_markdown(index=False))
    output_report.append("\n")
    output_report.append("### Top 20 Feature Importances for A/D Rating Models\n")
    output_report.append(feat_imp_df.head(20)[['Feature', 'Reg_Weight_Pct', 'Clf_Weight_Pct']].to_markdown(index=False))
    output_report.append("\n")
    output_report.append(f"### Direct Classification Per-Class Report (`{best_clf_name}`)\n")
    output_report.append(clf_rep_df.to_markdown(index=False))
    output_report.append("\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. COMPOSITE RATING MODEL (Excluding Group RS)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("4. REVERSE ENGINEERING COMPOSITE RATING (EXCLUDING GROUP RS)")
    print("="*80)

    comp_features = ['EPS Rating', 'RS Rating', 'SMR_Num', 'AD_Num']
    df_comp = df[['Comp Rating'] + comp_features].dropna()

    X_comp = df_comp[comp_features]
    y_comp = df_comp['Comp Rating']

    X_train_comp, X_test_comp, y_train_comp, y_test_comp = train_test_split(X_comp, y_comp, test_size=0.2, random_state=42)

    lr_unstd = LinearRegression()
    lr_unstd.fit(X_train_comp, y_train_comp)
    y_pred_comp = lr_unstd.predict(X_test_comp)

    r2_comp = r2_score(y_test_comp, y_pred_comp)
    mae_comp = mean_absolute_error(y_test_comp, y_pred_comp)

    scaler_comp = StandardScaler()
    X_train_comp_std = scaler_comp.fit_transform(X_train_comp)
    X_test_comp_std  = scaler_comp.transform(X_test_comp)

    lr_comp_std = LinearRegression()
    lr_comp_std.fit(X_train_comp_std, y_train_comp)

    rf_comp = RandomForestRegressor(n_estimators=40, max_depth=10, n_jobs=-1, random_state=42)
    rf_comp.fit(X_train_comp, y_train_comp)

    print(f"Composite Rating Model (No Group RS) - Linear Regression R²: {r2_comp:.4f} | MAE: {mae_comp:.2f}")

    comp_weights = pd.DataFrame({
        'Component_Rating': comp_features,
        'Unstd_Coef (Weight)': lr_unstd.coef_,
        'Std_Coef': lr_comp_std.coef_,
        'Abs_Std_Coef': np.abs(lr_comp_std.coef_),
        'RF_Importance': rf_comp.feature_importances_
    }).sort_values(by='Abs_Std_Coef', ascending=False)

    comp_weights['Rel_Weight_Pct'] = (comp_weights['Abs_Std_Coef'] / comp_weights['Abs_Std_Coef'].sum()) * 100
    min_weight = comp_weights['Unstd_Coef (Weight)'].abs().min()
    comp_weights['IBD_Theoretical_Ratio'] = comp_weights['Unstd_Coef (Weight)'] / (min_weight if min_weight > 0 else 1.0)

    output_report.append("## 4. Composite Rating Model (Excluding Group RS)\n")
    output_report.append(f"- **Linear Regression $R^2$**: `{r2_comp:.4f}` | **MAE**: `{mae_comp:.2f}` points")
    output_report.append(f"- **Regression Intercept**: `{lr_unstd.intercept_:.4f}`\n")
    output_report.append("### Component Rating Weights & Ratios (No Group RS)\n")
    output_report.append(comp_weights[['Component_Rating', 'Unstd_Coef (Weight)', 'Rel_Weight_Pct', 'IBD_Theoretical_Ratio', 'RF_Importance']].to_markdown(index=False))
    output_report.append("\n")

    output_report.append("### Reverse-Engineered Composite Formula (No Group RS)\n")
    formula_str = f"Comp Rating ≈ {lr_unstd.intercept_:.2f}"
    for idx, row in comp_weights.iterrows():
        sign = "+" if row['Unstd_Coef (Weight)'] >= 0 else "-"
        formula_str += f" {sign} {abs(row['Unstd_Coef (Weight)']):.4f} × [{row['Component_Rating']}]"
    output_report.append(f"```text\n{formula_str}\n```\n")

    rs_rating_section(df, repo_dir, output_report)

    artifact_dir = "/Users/vanstark/.gemini/antigravity-ide/brain/89712f63-1122-4914-b29a-40d1f2f4f77e"
    os.makedirs(artifact_dir, exist_ok=True)
    report_file = os.path.join(artifact_dir, "reverse_engineering_ratings_report.md")

    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(output_report))

    print(f"\nSaved updated analysis report to {report_file}")

if __name__ == '__main__':
    run_pipeline()
