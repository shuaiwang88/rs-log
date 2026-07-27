#!/usr/bin/env python3
"""
Reverse Engineering IBD Ratings (EPS Rating, SMR Rating, Composite Rating, and A/D Rating)
Using Advanced Machine Learning Incorporating Up to 365-Day Historical Price & Volume Records.

A/D Rating Reverse Engineering includes:
1. 13-Subtier Regression Models (predicting continuous 1..13 grade score, then converting to sub-tier A+, A, A-, B+, B, B-, C+, C, C-, D+, D, D-, E)
2. 13-Class Direct Classification Models (directly classifying into 13 sub-tier classes)
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

SUBTIER_MAP = {
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
    return SUBTIER_MAP.get(s, np.nan)

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
    return s if s in SUBTIER_MAP else np.nan

def num_to_13subtier(val):
    if val >= 12.5: return 'A+'
    elif val >= 11.5: return 'A'
    elif val >= 10.5: return 'A-'
    elif val >= 9.5: return 'B+'
    elif val >= 8.5: return 'B'
    elif val >= 7.5: return 'B-'
    elif val >= 6.5: return 'C+'
    elif val >= 5.5: return 'C'
    elif val >= 4.5: return 'C-'
    elif val >= 3.5: return 'D+'
    elif val >= 2.5: return 'D'
    elif val >= 1.5: return 'D-'
    else: return 'E'

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
    # 3. ACCUMULATION / DISTRIBUTION (A/D) RATING MODELS (13 SUB-TIERS SCALE)
    #    - Regression Model (Predicting 1..13, Converted to 13 Sub-Tiers A+, A, A- ... E)
    #    - Direct 13-Class Classification Model
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("3. REVERSE ENGINEERING ACCUMULATION / DISTRIBUTION (A/D) RATING (13 SUB-TIER SCALE)")
    print("   - Paradigm 1: 13-Subtier Regression Model (Converted to A+, A, A-, B+... E)")
    print("   - Paradigm 2: Direct 13-Class Classification Model")
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

    sub_ad = merged_ad[ad_features + ['AD_Num', 'AD_Subtier', 'AD_Tier']].dropna(subset=['AD_Num', 'AD_Subtier'])
    X_ad = sub_ad[ad_features].copy()
    for c in ad_features:
        X_ad[c] = X_ad[c].apply(clean_num)

    y_ad_num = sub_ad['AD_Num']           # Continuous 1..13
    y_ad_subtier = sub_ad['AD_Subtier']   # 13 Sub-tier strings (A+, A, A-, B+ ... E)
    y_ad_maintier = sub_ad['AD_Tier']     # 5 Main tier strings (A, B, C, D, E)

    imputer_ad = SimpleImputer(strategy='median')
    X_ad_imp = imputer_ad.fit_transform(X_ad)

    X_train_ad, X_test_ad, y_tr_num, y_te_num, y_tr_sub, y_te_sub = train_test_split(
        X_ad_imp, y_ad_num, y_ad_subtier, test_size=0.2, random_state=42, stratify=y_ad_subtier
    )

    scaler_ad = StandardScaler()
    X_train_ad_std = scaler_ad.fit_transform(X_train_ad)
    X_test_ad_std  = scaler_ad.transform(X_test_ad)

    # -----------------------------------------------------------------------
    # Approach 1: 13-Subtier Regression Model (Converted to Sub-Tier)
    # -----------------------------------------------------------------------
    print("\n" + "-"*60)
    print("Approach 1: 13-Subtier Regression Models (Converted to A+, A, A- ... E)")
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
    best_reg_sub_acc = 0.0

    for name, m in reg_models.items():
        if 'Linear' in name or 'Ridge' in name:
            m.fit(X_train_ad_std, y_tr_num)
            y_pred_num = m.predict(X_test_ad_std)
        else:
            m.fit(X_train_ad, y_tr_num)
            y_pred_num = m.predict(X_test_ad)

        r2 = r2_score(y_te_num, y_pred_num)
        mae = mean_absolute_error(y_te_num, y_pred_num)

        # Convert continuous predictions to 13 sub-tiers
        pred_subtier = pd.Series(y_pred_num, index=y_te_sub.index).apply(num_to_13subtier)

        exact_subtier_acc = accuracy_score(y_te_sub, pred_subtier) * 100
        macro_f1_sub = f1_score(y_te_sub, pred_subtier, average='macro') * 100

        # Distance checks
        te_sub_num = y_te_sub.map(SUBTIER_MAP)
        pr_sub_num = pred_subtier.map(SUBTIER_MAP)
        within_1_subtier_acc = (np.abs(te_sub_num - pr_sub_num) <= 1).mean() * 100

        # Main tier accuracy check
        te_main_num = y_te_sub.apply(subtier_to_maintier).map(MAIN_TIER_ORDER)
        pr_main_num = pred_subtier.apply(subtier_to_maintier).map(MAIN_TIER_ORDER)
        within_1_maintier_acc = (np.abs(te_main_num - pr_main_num) <= 1).mean() * 100

        reg_results.append({
            'Model Paradigm': '13-Subtier Regression',
            'Model Name': name,
            'R² Score': round(r2, 4),
            'MAE (13-Pt Scale)': round(mae, 2),
            'Exact Subtier Acc (%)': round(exact_subtier_acc, 2),
            'Within 1 Subtier Acc (%)': round(within_1_subtier_acc, 2),
            'Within 1 MainTier Acc (%)': round(within_1_maintier_acc, 2),
            'Macro F1 (%)': round(macro_f1_sub, 2)
        })

        if exact_subtier_acc > best_reg_sub_acc:
            best_reg_sub_acc = exact_subtier_acc
            best_reg_model = (name, m)

    reg_df = pd.DataFrame(reg_results)
    print(reg_df.to_string(index=False))

    # -----------------------------------------------------------------------
    # Approach 2: Direct 13-Class Classification Models
    # -----------------------------------------------------------------------
    print("\n" + "-"*60)
    print("Approach 2: Direct 13-Class Classification Models")
    print("-"*60)

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
            m.fit(X_train_ad_std, y_tr_sub)
            y_pred_sub = m.predict(X_test_ad_std)
        else:
            m.fit(X_train_ad, y_tr_sub)
            y_pred_sub = m.predict(X_test_ad)

        exact_subtier_acc = accuracy_score(y_te_sub, y_pred_sub) * 100
        macro_f1_sub = f1_score(y_te_sub, y_pred_sub, average='macro') * 100

        te_sub_num = y_te_sub.map(SUBTIER_MAP)
        pr_sub_num = pd.Series(y_pred_sub, index=y_te_sub.index).map(SUBTIER_MAP)
        within_1_subtier_acc = (np.abs(te_sub_num - pr_sub_num) <= 1).mean() * 100

        te_main_num = y_te_sub.apply(subtier_to_maintier).map(MAIN_TIER_ORDER)
        pr_main_num = pd.Series(y_pred_sub, index=y_te_sub.index).apply(subtier_to_maintier).map(MAIN_TIER_ORDER)
        within_1_maintier_acc = (np.abs(te_main_num - pr_main_num) <= 1).mean() * 100

        clf_results.append({
            'Model Paradigm': '13-Class Classification',
            'Model Name': name,
            'R² Score': 'N/A',
            'MAE (13-Pt Scale)': 'N/A',
            'Exact Subtier Acc (%)': round(exact_subtier_acc, 2),
            'Within 1 Subtier Acc (%)': round(within_1_subtier_acc, 2),
            'Within 1 MainTier Acc (%)': round(within_1_maintier_acc, 2),
            'Macro F1 (%)': round(macro_f1_sub, 2)
        })

        if exact_subtier_acc > best_clf_acc:
            best_clf_acc = exact_subtier_acc
            best_clf_model = (name, m)
            best_clf_y_pred = y_pred_sub

    clf_df = pd.DataFrame(clf_results)
    print(clf_df.to_string(index=False))

    # Combined Comparison
    combined_comparison = pd.concat([reg_df, clf_df], ignore_index=True)

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
    reg_imps = get_model_feature_importances(b_reg_m, reg_test_x, y_te_num)

    best_clf_name, b_clf_m = best_clf_model
    clf_test_x = X_test_ad_std if 'Logistic' in best_clf_name else X_test_ad
    clf_imps = get_model_feature_importances(b_clf_m, clf_test_x, y_te_sub)

    feat_imp_df = pd.DataFrame({
        'Feature': ad_features,
        'Reg_Importance': reg_imps,
        'Clf_Importance': clf_imps
    })
    feat_imp_df['Reg_Weight_Pct'] = round((feat_imp_df['Reg_Importance'] / max(1e-6, feat_imp_df['Reg_Importance'].sum())) * 100, 2)
    feat_imp_df['Clf_Weight_Pct'] = round((feat_imp_df['Clf_Importance'] / max(1e-6, feat_imp_df['Clf_Importance'].sum())) * 100, 2)
    feat_imp_df = feat_imp_df.sort_values(by='Reg_Weight_Pct', ascending=False)

    # Classification report for best classifier
    labels_present = sorted(list(y_te_sub.unique()))
    clf_rep = classification_report(y_te_sub, best_clf_y_pred, labels=labels_present, output_dict=True)
    clf_rep_df = pd.DataFrame(clf_rep).transpose().reset_index().rename(columns={'index': 'Sub-Tier / Metric'})

    output_report.append("## 3. Accumulation / Distribution (A/D) Rating Model (13 Sub-Tier Scale)\n")
    output_report.append(f"- **Evaluated Stock Dataset**: `{len(sub_ad):,}` stocks with 250D/365D Historical Price & Volume Records")
    output_report.append(f"- **Scale**: 13 Sub-Tiers (`A+`, `A`, `A-`, `B+`, `B`, `B-`, `C+`, `C`, `C-`, `D+`, `D`, `D-`, `E`)\n")
    output_report.append("### Comparative Model Performance Table (13 Sub-Tier Scale)\n")
    output_report.append(combined_comparison.to_markdown(index=False))
    output_report.append("\n")
    output_report.append("### Top 20 Feature Importances for A/D Rating Models\n")
    output_report.append(feat_imp_df.head(20)[['Feature', 'Reg_Weight_Pct', 'Clf_Weight_Pct']].to_markdown(index=False))
    output_report.append("\n")
    output_report.append(f"### Direct Classification Per-Subtier Report (`{best_clf_name}`)\n")
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

    artifact_dir = "/Users/vanstark/.gemini/antigravity-ide/brain/89712f63-1122-4914-b29a-40d1f2f4f77e"
    os.makedirs(artifact_dir, exist_ok=True)
    report_file = os.path.join(artifact_dir, "reverse_engineering_ratings_report.md")

    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(output_report))

    print(f"\nSaved updated analysis report to {report_file}")

if __name__ == '__main__':
    run_pipeline()
