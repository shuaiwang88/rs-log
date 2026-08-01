#!/usr/bin/env python3
"""
Direct Comparison: Sub-Tier Regression vs 13-Class Classification for IBD A/D Rating.
Evaluates continuous regression models vs discrete classification models.
"""

import sys
from pathlib import Path
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.ensemble import HistGradientBoostingRegressor, ExtraTreesRegressor, RandomForestRegressor, HistGradientBoostingClassifier, ExtraTreesClassifier, RandomForestClassifier
from sklearn.metrics import r2_score, mean_absolute_error, accuracy_score

SUBTIER_13_MAP = {
    'A+': 13.0, 'A': 12.0, 'A-': 11.0,
    'B+': 10.0, 'B': 9.0,  'B-': 8.0,
    'C+': 7.0,  'C': 6.0,  'C-': 5.0,
    'D+': 4.0,  'D': 3.0,  'D-': 2.0,
    'E': 1.0
}

MAIN_TIER_ORDER = {'A': 5, 'B': 4, 'C': 3, 'D': 2, 'E': 1}

def ad_subtier(val):
    if pd.isna(val): return np.nan
    s = str(val).strip().upper()
    return s if s in SUBTIER_13_MAP else np.nan

def ad_5tier(val):
    if pd.isna(val): return np.nan
    s = str(val).strip().upper()[0]
    return s if s in ['A', 'B', 'C', 'D', 'E'] else np.nan

def num_to_13tier(val):
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

sys.path.append(str(Path(__file__).resolve().parent))
from analyze_ad_ratings import extract_ticker_ad_features

def run_comparison():
    repo_dir = Path(__file__).resolve().parent.parent
    cache_dir = repo_dir / "ticker_cache"
    csv_path = repo_dir / "IBD" / "marketsurge.csv"

    df_ms = pd.read_csv(csv_path, low_memory=False)
    df_ms['AD_Subtier'] = df_ms['A/D Rating'].apply(ad_subtier)
    df_ms['AD_Tier'] = df_ms['A/D Rating'].apply(ad_5tier)
    df_ms['AD_Num'] = df_ms['AD_Subtier'].map(SUBTIER_13_MAP)

    df_valid = df_ms.dropna(subset=['Symbol', 'AD_Num']).copy()
    df_valid['Symbol'] = df_valid['Symbol'].astype(str).str.strip()

    ms_symbols = df_valid['Symbol'].unique().tolist()[:2500]

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda t: extract_ticker_ad_features(t, cache_dir), ms_symbols))

    records = [r for r in results if r is not None]
    df_feat = pd.DataFrame(records)

    merged = df_valid[['Symbol', 'A/D Rating', 'AD_Subtier', 'AD_Tier', 'AD_Num']].merge(
        df_feat, left_on='Symbol', right_on='Ticker', how='inner'
    )

    merged = merged[merged['Latest_Price'] >= 4.0].copy()

    candidate_features = [
        'Up_Dn_Vol_65D', 'Heavy_Net_Ratio_65D', 'Net_Heavy_Days_65D', 'Net_Heavy_Intensity_65D', 'Avg_Closing_Range_65D', 'Vol_Weighted_Closing_Range_65D', 'CMF_65D',
        'Up_Dn_Vol_130D', 'Heavy_Net_Ratio_130D', 'Net_Heavy_Days_130D', 'Net_Heavy_Intensity_130D', 'Avg_Closing_Range_130D', 'Vol_Weighted_Closing_Range_130D', 'CMF_130D',
        'Up_Dn_Vol_250D', 'Heavy_Net_Ratio_250D', 'Net_Heavy_Days_250D', 'Net_Heavy_Intensity_250D', 'Avg_Closing_Range_250D', 'Vol_Weighted_Closing_Range_250D', 'CMF_250D',
        'Up_Dn_Vol_30D', 'Heavy_Net_Ratio_30D', 'Net_Heavy_Days_30D', 'Net_Heavy_Intensity_30D', 'Avg_Closing_Range_30D', 'Vol_Weighted_Closing_Range_30D', 'CMF_30D',
        'Up_Day_Avg_Vol_Ratio', 'Dn_Day_Avg_Vol_Ratio', 'Price_Vol_Corr',
        'Dist_10D_MA', 'Dist_21D_MA', 'Dist_50D_MA', 'Dist_150D_MA', 'Dist_200D_MA',
        'Price_Pct_Chg_30D', 'Price_Pct_Chg_65D', 'Price_Pct_Chg_130D', 'Price_Pct_Chg_250D'
    ]
    feature_cols = [c for c in candidate_features if c in merged.columns]
    merged = merged.dropna(subset=feature_cols).copy()

    X = merged[feature_cols].copy()
    for c in feature_cols:
        X[c] = pd.to_numeric(X[c], errors='coerce')

    y_num = merged['AD_Num'].values
    y_sub = merged['AD_Subtier'].values
    y_tier = merged['AD_Tier'].values

    imputer = SimpleImputer(strategy='median')
    X_imp = imputer.fit_transform(X)

    X_train, X_test, y_tr_num, y_te_num, y_tr_sub, y_te_sub, y_tr_tier, y_te_tier = train_test_split(
        X_imp, y_num, y_sub, y_tier, test_size=0.2, random_state=42, stratify=y_tier
    )

    print("\n" + "="*90)
    print(f"EVALUATION: SUB-TIER REGRESSION VS DIRECT 13-CLASS CLASSIFICATION ({len(merged):,} STOCKS, PRICE >= $4.00)")
    print("="*90 + "\n")

    # 1. Regression Models
    reg_models = {
        'HistGradientBoosting Regressor': HistGradientBoostingRegressor(max_iter=40, learning_rate=0.05, random_state=42),
        'Random Forest Regressor': RandomForestRegressor(n_estimators=30, max_depth=8, random_state=42, n_jobs=4),
        'ExtraTrees Regressor': ExtraTreesRegressor(n_estimators=30, max_depth=8, random_state=42, n_jobs=4)
    }

    reg_rows = []
    for name, m in reg_models.items():
        m.fit(X_train, y_tr_num)
        pred_num = m.predict(X_test)
        r2 = r2_score(y_te_num, pred_num)
        mae = mean_absolute_error(y_te_num, pred_num)

        pred_13sub = pd.Series(pred_num).apply(num_to_13tier).values
        pred_5main = pd.Series(pred_num).apply(lambda v: num_to_13tier(v)[0]).values

        exact_sub = accuracy_score(y_te_sub, pred_13sub) * 100
        exact_main = accuracy_score(y_te_tier, pred_5main) * 100

        te_sub_v = pd.Series(y_te_sub).map(SUBTIER_13_MAP).values
        pr_sub_v = pd.Series(pred_13sub).map(SUBTIER_13_MAP).values
        within_1_sub = (np.abs(te_sub_v - pr_sub_v) <= 1.0).mean() * 100

        te_main_v = pd.Series(y_te_tier).map(MAIN_TIER_ORDER).values
        pr_main_v = pd.Series(pred_5main).map(MAIN_TIER_ORDER).values
        within_1_main = (np.abs(te_main_v - pr_main_v) <= 1.0).mean() * 100

        reg_rows.append({
            'Paradigm': 'Sub-Tier Regression (Continuous 1..13)',
            'Model': name,
            'R² Score': round(r2, 4),
            'MAE (Pts)': round(mae, 2),
            'Exact 13-Sub Acc (%)': round(exact_sub, 2),
            '±1 Sub-Tier Acc (%)': round(within_1_sub, 2),
            'Exact 5-Main Acc (%)': round(exact_main, 2),
            '±1 Main-Tier Acc (%)': round(within_1_main, 2)
        })

    # 2. Classification Models
    clf_models = {
        'HistGradientBoosting Classifier': HistGradientBoostingClassifier(max_iter=40, learning_rate=0.05, random_state=42),
        'Random Forest Classifier': RandomForestClassifier(n_estimators=30, max_depth=8, random_state=42, n_jobs=4),
        'ExtraTrees Classifier': ExtraTreesClassifier(n_estimators=30, max_depth=8, random_state=42, n_jobs=4)
    }

    clf_rows = []
    for name, m in clf_models.items():
        m.fit(X_train, y_tr_sub)
        pred_13sub = m.predict(X_test)
        pred_5main = pd.Series(pred_13sub).apply(lambda s: str(s)[0] if pd.notna(s) else 'E').values

        exact_sub = accuracy_score(y_te_sub, pred_13sub) * 100
        exact_main = accuracy_score(y_te_tier, pred_5main) * 100

        te_sub_v = pd.Series(y_te_sub).map(SUBTIER_13_MAP).values
        pr_sub_v = pd.Series(pred_13sub).map(SUBTIER_13_MAP).values
        within_1_sub = (np.abs(te_sub_v - pr_sub_v) <= 1.0).mean() * 100

        te_main_v = pd.Series(y_te_tier).map(MAIN_TIER_ORDER).values
        pr_main_v = pd.Series(pred_5main).map(MAIN_TIER_ORDER).values
        within_1_main = (np.abs(te_main_v - pr_main_v) <= 1.0).mean() * 100

        clf_rows.append({
            'Paradigm': 'Direct 13-Class Classification',
            'Model': name,
            'R² Score': 'N/A',
            'MAE (Pts)': 'N/A',
            'Exact 13-Sub Acc (%)': round(exact_sub, 2),
            '±1 Sub-Tier Acc (%)': round(within_1_sub, 2),
            'Exact 5-Main Acc (%)': round(exact_main, 2),
            '±1 Main-Tier Acc (%)': round(within_1_main, 2)
        })

    df_comp = pd.concat([pd.DataFrame(reg_rows), pd.DataFrame(clf_rows)], ignore_index=True)
    print(df_comp.to_markdown(index=False))

if __name__ == '__main__':
    run_comparison()
