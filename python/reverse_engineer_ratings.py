#!/usr/bin/env python3
"""
Reverse Engineering IBD Ratings (EPS Rating, SMR Rating, Composite Rating, and A/D Rating)
Using Advanced Machine Learning (Gradient Boosting, ExtraTrees, Classifier & Regressor Ensembles)
incorporating ALL 21 individual historical daily price & volume records, Up/Down Vol, Price vs 50-Day, and Percentile Ranks.
"""

import sys
import os
from pathlib import Path
import pandas as pd
import numpy as np

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.ensemble import RandomForestRegressor, ExtraTreesRegressor, HistGradientBoostingClassifier
from sklearn.metrics import r2_score, mean_absolute_error

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

def grade_to_num(val):
    if pd.isna(val):
        return np.nan
    s = str(val).strip().upper()
    mapping = {
        'A+': 13.0, 'A': 12.0, 'A-': 11.0,
        'B+': 10.0, 'B': 9.0,  'B-': 8.0,
        'C+': 7.0,  'C': 6.0,  'C-': 5.0,
        'D+': 4.0,  'D': 3.0,  'D-': 2.0,
        'E': 1.0
    }
    return mapping.get(s, np.nan)

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

def compute_21_commit_all_daily_features(hist_file: Path) -> pd.DataFrame:
    print(f"Extracting all 21 individual daily price and volume records from {hist_file}...")
    df_hist = pd.read_csv(hist_file, low_memory=False)
    if 'date' in df_hist.columns:
        df_hist['date'] = pd.to_datetime(df_hist['date'])
        df_hist = df_hist.sort_values(by=['Ticker', 'date'])

    records = []
    for ticker, group in df_hist.groupby('Ticker'):
        if len(group) < 21:
            continue
        g21 = group.tail(21).copy()
        prices = g21['Price'].values
        vols = g21['Volume'].values
        
        if len(prices) < 21 or prices[0] <= 0:
            continue
            
        p_last = prices[-1]
        p_first = prices[0]
        ret_tot = (p_last / p_first - 1) * 100
        
        price_diff = np.diff(prices)
        vol_diff = np.diff(vols)
        vol_tail = vols[1:]
        
        is_up = price_diff > 0
        is_dn = price_diff < 0
        
        up_vol_sum = np.sum(vol_tail[is_up])
        dn_vol_sum = np.sum(vol_tail[is_dn])
        ud_ratio_21 = up_vol_sum / max(1, dn_vol_sum)
        
        acc_days = np.sum((price_diff > 0) & (vol_diff > 0))
        dist_days = np.sum((price_diff < 0) & (vol_diff > 0))
        net_acc_days = acc_days - dist_days
        
        mean_vol = max(1, np.mean(vols))
        safe_prices = np.where(prices[:-1] == 0, 1.0, prices[:-1])
        daily_rets = price_diff / safe_prices
        vol_ratios = vol_tail / mean_vol
        vw_ret = np.sum(daily_rets * vol_ratios)
        
        rec = {
            'Ticker': ticker,
            'Hist21D_Price_Pct_Chg': ret_tot,
            'Hist21D_UD_Ratio': ud_ratio_21,
            'Hist21D_Acc_Days': int(acc_days),
            'Hist21D_Dist_Days': int(dist_days),
            'Hist21D_Net_Acc_Days': int(net_acc_days),
            'Hist21D_VW_Ret': vw_ret
        }
        
        # Include exact individual daily returns and volume ratios for all 20 historical step intervals across the 21 commits
        for i in range(20):
            rec[f'Ret_D{i+1}'] = daily_rets[i]
            rec[f'VolRatio_D{i+1}'] = vol_ratios[i]
            
        records.append(rec)

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
    # 3. ACCUMULATION / DISTRIBUTION (A/D) RATING MODEL (21 INDIVIDUAL DAYS + TECHNICALS)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("3. REVERSE ENGINEERING ACCUMULATION / DISTRIBUTION (A/D) RATING")
    print("="*80)

    if hist_file.exists() and rs_stocks_file.exists():
        df_21_feat = compute_21_commit_all_daily_features(hist_file)
        rs_df = pd.read_csv(rs_stocks_file, low_memory=False)
        merged_ad = rs_df.merge(df_21_feat, on='Ticker', how='inner')
        merged_ad = merged_ad.merge(df[['Symbol', 'AD_Num', 'AD_Tier', 'A/D Rating']], left_on='Ticker', right_on='Symbol', how='inner')

        # Percentile Rank Scaling
        merged_ad['Rank_Price_50D'] = merged_ad['Price vs 50-Day'].rank(pct=True) * 100
        merged_ad['Rank_Up_Down_Vol'] = merged_ad['Up/Down Vol'].rank(pct=True) * 100
        merged_ad['Rank_Net_Acc_Days'] = merged_ad['Hist21D_Net_Acc_Days'].rank(pct=True) * 100
        merged_ad['Inter_50D_UD'] = merged_ad['Price vs 50-Day'] * merged_ad['Up/Down Vol']

        daily_features = [f'Ret_D{i+1}' for i in range(20)] + [f'VolRatio_D{i+1}' for i in range(20)]
        macro_features = [
            'Rank_Price_50D', 'Rank_Up_Down_Vol', 'Rank_Net_Acc_Days', 'Inter_50D_UD',
            'Hist21D_Net_Acc_Days', 'Hist21D_Acc_Days', 'Hist21D_Dist_Days', 'Hist21D_Price_Pct_Chg',
            'Hist21D_UD_Ratio', 'Hist21D_VW_Ret', 'Price vs 50-Day', 'Price vs 200-Day',
            'Up/Down Vol', 'Daily Closing Range', 'Vol % Chg vs 50-Day', '21 Day ATR %'
        ]

        ad_features = macro_features + daily_features

        sub_ad = merged_ad[ad_features + ['AD_Num', 'AD_Tier']].dropna()
        X_ad = sub_ad[ad_features]
        y_ad_num = sub_ad['AD_Num']
        y_ad_tier = sub_ad['AD_Tier']

        X_train_ad, X_test_ad, y_train_ad, y_test_ad, y_tr_tier, y_te_tier = train_test_split(
            X_ad, y_ad_num, y_ad_tier, test_size=0.2, random_state=42
        )

        et_ad = ExtraTreesRegressor(n_estimators=100, max_depth=16, random_state=42)
        et_ad.fit(X_train_ad, y_train_ad)
        y_pred_ad = et_ad.predict(X_test_ad)

        r2_ad = r2_score(y_test_ad, y_pred_ad)
        mae_ad = mean_absolute_error(y_test_ad, y_pred_ad)

        clf_ad = HistGradientBoostingClassifier(max_iter=200, random_state=42)
        clf_ad.fit(X_train_ad, y_tr_tier)
        y_pred_tier = clf_ad.predict(X_test_ad)

        tier_order = {'A':5, 'B':4, 'C':3, 'D':2, 'E':1}
        te_num = y_te_tier.map(tier_order)
        pr_num = pd.Series(y_pred_tier, index=y_te_tier.index).map(tier_order)
        within_1_tier_acc = (np.abs(te_num - pr_num) <= 1).mean() * 100

        print(f"A/D Rating Model (with ALL 21 Daily Prices & Volumes) - ExtraTrees R² across {len(sub_ad):,} stocks: {r2_ad:.4f} | MAE: {mae_ad:.2f} grade points")
        print(f"A/D Rating Model - 5-Tier Classifier Within-1-Tier Accuracy: {within_1_tier_acc:.2f}%")

        ad_weights = pd.DataFrame({
            'Feature': ad_features,
            'Importance': et_ad.feature_importances_
        }).sort_values(by='Importance', ascending=False)
        ad_weights['Rel_Weight_Pct'] = ad_weights['Importance'] * 100

        output_report.append("## 3. Accumulation / Distribution (A/D) Rating Model (All 21 Daily Prices & Volumes)\n")
        output_report.append(f"- **Evaluated Stock Dataset**: `{len(sub_ad):,}` stocks with ALL 21 individual daily price & volume records")
        output_report.append(f"- **ExtraTrees Regressor $R^2$**: `{r2_ad:.4f}` | **MAE**: `{mae_ad:.2f}` grade points (out of 13 scale points)")
        output_report.append(f"- **5-Tier Grade Classifier Within-1-Tier Accuracy**: `{within_1_tier_acc:.2f}%` (Predicts `A, B, C, D, E` tier correctly or within $\\pm 1$ tier)\n")
        output_report.append("### Top 15 Feature Importances for A/D Rating Model\n")
        output_report.append(ad_weights.head(15)[['Feature', 'Rel_Weight_Pct', 'Importance']].to_markdown(index=False))
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
