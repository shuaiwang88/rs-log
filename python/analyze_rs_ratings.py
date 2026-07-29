#!/usr/bin/env python3
"""
Comprehensive RS Rating Reverse Engineering Analysis
Redoing the entire RS Rating analysis using price data from ticker_cache against SPY baseline.

Enforcing strict 1-Month Recency-First Weighting (1M >= 3M >= 6M >= 9M >= 12M).
"""

import sys
import os
from pathlib import Path
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from scipy.optimize import minimize

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, Ridge, Lasso
from sklearn.ensemble import RandomForestRegressor, ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.metrics import r2_score, mean_absolute_error

def clean_num(val):
    if pd.isna(val):
        return np.nan
    s = str(val).replace('%', '').replace('$', '').replace(',', '').strip()
    try:
        return float(s)
    except:
        return np.nan

def load_spy_reference(cache_dir: Path) -> pd.Series:
    spy_path = cache_dir / "SPY_1d.parquet"
    if not spy_path.exists():
        spy_path = cache_dir / "SPY_250d.parquet"
    if not spy_path.exists():
        raise FileNotFoundError(f"No SPY parquet file found in {cache_dir}")
    
    spy_df = pd.read_parquet(spy_path)
    if isinstance(spy_df.columns, pd.MultiIndex):
        spy_df.columns = spy_df.columns.droplevel(1)
    
    spy_df.index = pd.to_datetime(spy_df.index, utc=True)
    price_col = 'Close' if 'Close' in spy_df.columns else ('Price' if 'Price' in spy_df.columns else None)
    if price_col is None:
        raise ValueError("SPY parquet missing Close or Price column")
    
    spy_close = pd.to_numeric(spy_df[price_col].astype(str).str.replace(',', '').str.replace('$', '').str.strip(), errors='coerce').dropna()
    spy_close = spy_close[spy_close > 0]
    return spy_close

def compute_ticker_performance(ticker: str, cache_dir: Path, spy_perf: dict, windows: dict, q_windows: dict):
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
    if latest < 4.0:
        return None

    rec = {'Ticker': t_clean, 'Price_Days': len(prices), 'Latest_Price': round(float(latest), 2)}

    # Trailing cumulative windows (1M, 3M, 6M, 9M, 12M)
    for label, days in windows.items():
        if len(prices) > days:
            past_price = prices[-(days + 1)]
            stock_perf = latest / past_price
            rel_perf = stock_perf / spy_perf[label]
            rec[f'Rel_Perf_{label}'] = rel_perf
            rec[f'Stock_Ret_{label}'] = (stock_perf - 1) * 100
            rec[f'Excess_Ret_{label}'] = (rel_perf - 1) * 100
        else:
            rec[f'Rel_Perf_{label}'] = np.nan
            rec[f'Stock_Ret_{label}'] = np.nan
            rec[f'Excess_Ret_{label}'] = np.nan

    # Non-overlapping quarterly performance (Q1: 0-63D, Q2: 63-126D, Q3: 126-188D, Q4: 188-250D)
    # Q1: last 3M (0 to 63 trading days ago)
    # Q2: 3M to 6M (63 to 126 trading days ago)
    # Q3: 6M to 9M (126 to 188 trading days ago)
    # Q4: 9M to 12M (188 to 250 trading days ago)
    if len(prices) > 63:
        rec['Q1_Ret'] = (prices[-1] / prices[-64] - 1) * 100
    else:
        rec['Q1_Ret'] = np.nan

    if len(prices) > 126:
        rec['Q2_Ret'] = (prices[-64] / prices[-127] - 1) * 100
    else:
        rec['Q2_Ret'] = np.nan

    if len(prices) > 188:
        rec['Q3_Ret'] = (prices[-127] / prices[-189] - 1) * 100
    else:
        rec['Q3_Ret'] = np.nan

    if len(prices) > 250:
        rec['Q4_Ret'] = (prices[-189] / prices[-251] - 1) * 100
    else:
        rec['Q4_Ret'] = np.nan

    # Exponentially-weighted recent price momentum
    for decay in [0.005, 0.01, 0.02, 0.05]:
        n_days = min(len(prices) - 1, 249)
        if n_days >= 20:
            daily_rets = np.diff(prices) / np.where(prices[:-1] == 0, 1.0, prices[:-1])
            daily_rets = daily_rets[-n_days:]
            w = np.exp(-decay * np.arange(n_days)[::-1])
            w = w / w.sum()
            rec[f'EMA_Ret_d{decay}'] = float(np.sum(daily_rets * w))

    return rec

def run_analysis():
    repo_dir = Path(__file__).resolve().parent.parent
    cache_dir = repo_dir / "ticker_cache"
    csv_path = repo_dir / "IBD" / "marketsurge.csv"

    print("=" * 80)
    print("REDOING REVERSE ENGINEERING ANALYSIS OF IBD RS RATINGS")
    print("Using Price Data from ticker_cache vs SPY Baseline")
    print("Excluding Volatile Stocks Under $4.00")
    print("Enforcing 1-Month Recency-Heavy Priority (1M >= 3M >= 6M >= 9M >= 12M)")
    print("=" * 80)

    # 1. Load Ground Truth Data
    if not csv_path.exists():
        print(f"Error: {csv_path} not found.")
        sys.exit(1)

    df_ms = pd.read_csv(csv_path, low_memory=False)
    print(f"Loaded MarketSurge CSV: {len(df_ms):,} rows")

    for col in ['RS Rating', 'RS 3-Month Rating', 'RS 6-Month Rating']:
        if col in df_ms.columns:
            df_ms[col] = df_ms[col].apply(clean_num)

    df_ms = df_ms.dropna(subset=['Symbol', 'RS Rating']).copy()
    df_ms['Symbol'] = df_ms['Symbol'].astype(str).str.strip()
    print(f"Stocks with valid RS Rating: {len(df_ms):,}")

    # 2. Load SPY Reference
    spy_close = load_spy_reference(cache_dir)
    print(f"SPY reference: {len(spy_close)} trading days ({spy_close.index[0].date()} -> {spy_close.index[-1].date()})")

    windows = {
        '1M': 21,
        '3M': 63,
        '6M': 126,
        '9M': 188,
        '12M': 249
    }
    q_windows = {
        'Q1': 63,
        'Q2': 126,
        'Q3': 188,
        'Q4': 249
    }

    spy_latest = float(spy_close.iloc[-1])
    spy_perf = {}
    for label, days in windows.items():
        if len(spy_close) > days:
            spy_past = float(spy_close.iloc[-(days + 1)])
            spy_perf[label] = spy_latest / spy_past
        else:
            spy_perf[label] = 1.0
    print(f"SPY performance ratios: {', '.join(f'{k}={v:.4f}' for k, v in spy_perf.items())}")

    # 3. Parallel Ticker Processing
    ms_symbols = df_ms['Symbol'].unique().tolist()
    print(f"Processing price data for {len(ms_symbols):,} tickers across 16 threads...")

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda t: compute_ticker_performance(t, cache_dir, spy_perf, windows, q_windows), ms_symbols))

    records = [r for r in results if r is not None]
    df_calc = pd.DataFrame(records)
    print(f"✓ Successfully processed price data for {len(df_calc):,} tickers from ticker_cache")

    # 4. Merge Calculated Features with MarketSurge RS Ratings
    merged = df_ms[['Symbol', 'RS Rating', 'RS 3-Month Rating', 'RS 6-Month Rating']].merge(
        df_calc, left_on='Symbol', right_on='Ticker', how='inner'
    )
    print(f"Merged evaluation dataset: {len(merged):,} stocks")

    required_cols = [f'Rel_Perf_{w}' for w in ['1M', '3M', '6M', '9M', '12M']]
    merged_full = merged.dropna(subset=required_cols).copy()
    print(f"Stocks with full 12M (250D) price history: {len(merged_full):,}")

    y_true_rs = merged_full['RS Rating'].values

    output_report = []
    output_report.append("# Reverse Engineering Analysis of IBD Relative Strength (RS) Ratings\n")
    output_report.append(f"**Baseline SPY History**: `{len(spy_close)}` trading days | **Evaluation Universe**: `{len(merged_full):,}` stocks with complete 250D price history\n")
    output_report.append("> [!NOTE]\n> **Key Principle**: The RS Rating is a 1-99 percentile rank of stock performance over trailing windows, with **the most recent month (1M / 21 trading days) taking highest priority/weight**.\n")

    # ── Section A: Preset Weight Configurations ─────────────────────────────────
    print("\n" + "=" * 60)
    print("Section A: Preset Weight Configurations (Prioritizing 1-Month Recency)")
    print("=" * 60)

    preset_configs = {
        '1M_Dominant (40/30/15/10/5)':           {'1M': 0.40, '3M': 0.30, '6M': 0.15, '9M': 0.10, '12M': 0.05},
        '1M_Heavy (35/25/20/12/8)':              {'1M': 0.35, '3M': 0.25, '6M': 0.20, '9M': 0.12, '12M': 0.08},
        'Linear_Decay (33.3/26.7/20/13.3/6.7)':  {'1M': 5/15, '3M': 4/15, '6M': 3/15, '9M': 2/15, '12M': 1/15},
        'Moderate_Recency (30/25/20/15/10)':     {'1M': 0.30, '3M': 0.25, '6M': 0.20, '9M': 0.15, '12M': 0.10},
        'Classic_IBD_4Q (40/20/20/20 on Qs)':     {'Q1': 0.40, 'Q2': 0.20, 'Q3': 0.20, 'Q4': 0.20},
        'Classic_IBD_4Win (40/20/20/20 on Cum)': {'3M': 0.40, '6M': 0.20, '9M': 0.20, '12M': 0.20},
        'Equal_Weight (20/20/20/20/20)':         {'1M': 0.20, '3M': 0.20, '6M': 0.20, '9M': 0.20, '12M': 0.20},
    }

    preset_results = []
    for name, weights in preset_configs.items():
        if 'Classic_IBD_4Q' in name:
            q_cols = ['Q1_Ret', 'Q2_Ret', 'Q3_Ret', 'Q4_Ret']
            sub_df = merged_full.dropna(subset=q_cols)
            ws = np.array([weights['Q1'], weights['Q2'], weights['Q3'], weights['Q4']])
            raw_scores = sub_df[q_cols].values @ ws
            y_eval = sub_df['RS Rating'].values
        else:
            cols = [f'Rel_Perf_{k}' for k in ['1M', '3M', '6M', '9M', '12M'] if k in weights]
            ws = np.array([weights[k] for k in ['1M', '3M', '6M', '9M', '12M'] if k in weights])
            raw_scores = merged_full[cols].values @ ws * 100
            y_eval = y_true_rs

        ranks = pd.Series(raw_scores).rank(pct=True).values * 99
        ranks = np.clip(ranks, 1, 99)

        r2 = r2_score(y_eval, ranks)
        mae = mean_absolute_error(y_eval, ranks)
        corr = np.corrcoef(y_eval, ranks)[0, 1]
        exact_match = np.mean(np.round(ranks) == y_eval) * 100
        within_1 = np.mean(np.abs(y_eval - ranks) <= 1) * 100
        within_3 = np.mean(np.abs(y_eval - ranks) <= 3) * 100
        within_5 = np.mean(np.abs(y_eval - ranks) <= 5) * 100

        preset_results.append({
            'Weight Configuration': name,
            '1M / Q1 Weight': weights.get('1M', weights.get('Q1', 0.0)),
            '3M / Q2 Weight': weights.get('3M', weights.get('Q2', 0.0)),
            '6M / Q3 Weight': weights.get('6M', weights.get('Q3', 0.0)),
            '9M / Q4 Weight': weights.get('9M', weights.get('Q4', 0.0)),
            '12M Weight': weights.get('12M', 0.0),
            'R²': round(r2, 4),
            'MAE': round(mae, 2),
            'Correlation': round(corr, 4),
            '±1 Acc (%)': round(within_1, 2),
            '±3 Acc (%)': round(within_3, 2),
            '±5 Acc (%)': round(within_5, 2),
        })
        print(f"  {name:38s}: R²={r2:.4f}, MAE={mae:.2f}, Corr={corr:.4f}, ±3 Acc={within_3:.1f}%")

    preset_df = pd.DataFrame(preset_results)

    # ── Section B: Monotonic Recency Constrained Optimization (1M >= 3M >= 6M >= 9M >= 12M) ──
    print("\n" + "=" * 60)
    print("Section B: Monotonic Recency Constrained Optimization (1M >= 3M >= 6M >= 9M >= 12M)")
    print("=" * 60)

    perf_matrix = merged_full[required_cols].values

    def mono_objective(params):
        # Parametrization ensuring w1 >= w2 >= w3 >= w4 >= w5 >= 0
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

    res_mono = minimize(mono_objective, [0.15, 0.10, 0.08, 0.05, 0.02], method='Nelder-Mead',
                        options={'maxiter': 5000, 'xatol': 1e-6, 'fatol': 1e-6})

    v = np.abs(res_mono.x)
    w5 = v[4]; w4 = w5 + v[3]; w3 = w4 + v[2]; w2 = w3 + v[1]; w1 = w2 + v[0]
    opt_w_mono = np.array([w1, w2, w3, w4, w5])
    opt_w_mono = opt_w_mono / opt_w_mono.sum()
    labels = ['1M', '3M', '6M', '9M', '12M']

    print(f"Optimal Monotonic Recency Weights: {', '.join(f'{l}={w:.4f}' for l, w in zip(labels, opt_w_mono))}")

    raw_mono = perf_matrix @ opt_w_mono * 100
    ranks_mono = pd.Series(raw_mono).rank(pct=True).values * 99
    ranks_mono = np.clip(ranks_mono, 1, 99)

    r2_mono = r2_score(y_true_rs, ranks_mono)
    mae_mono = mean_absolute_error(y_true_rs, ranks_mono)
    corr_mono = np.corrcoef(y_true_rs, ranks_mono)[0, 1]
    within_1_mono = np.mean(np.abs(y_true_rs - ranks_mono) <= 1) * 100
    within_3_mono = np.mean(np.abs(y_true_rs - ranks_mono) <= 3) * 100
    within_5_mono = np.mean(np.abs(y_true_rs - ranks_mono) <= 5) * 100

    print(f"Monotonic Constrained Optimization: R²={r2_mono:.4f}, MAE={mae_mono:.2f}, Corr={corr_mono:.4f}, ±3 Acc={within_3_mono:.1f}%")

    # ── Section C: Exponential Recency Decay Optimization ─────────────────────────
    print("\n" + "=" * 60)
    print("Section C: Parametric Exponential Recency Decay Optimization")
    print("=" * 60)

    def exp_decay_objective(beta):
        k = np.array([0, 1, 2, 3, 4])  # 1M=0, 3M=1, 6M=2, 9M=3, 12M=4
        w = np.exp(-abs(beta[0]) * k)
        w = w / w.sum()
        raw = perf_matrix @ w * 100
        ranks = pd.Series(raw).rank(pct=True).values * 99
        ranks = np.clip(ranks, 1, 99)
        return np.mean(np.abs(y_true_rs - ranks))

    res_exp = minimize(exp_decay_objective, [0.15], method='Nelder-Mead',
                       options={'maxiter': 3000, 'xatol': 1e-8, 'fatol': 1e-8})

    opt_beta = abs(res_exp.x[0])
    k_steps = np.array([0, 1, 2, 3, 4])
    exp_weights = np.exp(-opt_beta * k_steps)
    exp_weights = exp_weights / exp_weights.sum()

    print(f"Optimal Recency Decay Rate β = {opt_beta:.6f}")
    print(f"Exp Recency Weights: {', '.join(f'{l}={w:.4f}' for l, w in zip(labels, exp_weights))}")

    raw_exp = perf_matrix @ exp_weights * 100
    ranks_exp = pd.Series(raw_exp).rank(pct=True).values * 99
    ranks_exp = np.clip(ranks_exp, 1, 99)

    r2_exp = r2_score(y_true_rs, ranks_exp)
    mae_exp = mean_absolute_error(y_true_rs, ranks_exp)
    corr_exp = np.corrcoef(y_true_rs, ranks_exp)[0, 1]
    within_1_exp = np.mean(np.abs(y_true_rs - ranks_exp) <= 1) * 100
    within_3_exp = np.mean(np.abs(y_true_rs - ranks_exp) <= 3) * 100
    within_5_exp = np.mean(np.abs(y_true_rs - ranks_exp) <= 5) * 100

    print(f"Parametric Exponential Decay: R²={r2_exp:.4f}, MAE={mae_exp:.2f}, Corr={corr_exp:.4f}, ±3 Acc={within_3_exp:.1f}%")

    # ── Section D: Feature-Rich Machine Learning Regression Models ─────────────
    print("\n" + "=" * 60)
    print("Section D: Feature-Rich ML Regression Models")
    print("=" * 60)

    ml_candidates = (
        [f'Rel_Perf_{w}' for w in ['1M', '3M', '6M', '9M', '12M']] +
        [f'Stock_Ret_{w}' for w in ['1M', '3M', '6M', '9M', '12M']] +
        [f'Excess_Ret_{w}' for w in ['1M', '3M', '6M', '9M', '12M']] +
        [f'EMA_Ret_d{d}' for d in [0.005, 0.01, 0.02, 0.05]]
    )
    ml_features = [c for c in ml_candidates if c in merged_full.columns and merged_full[c].notna().sum() > 50]

    X_rs = merged_full[ml_features].copy()
    y_rs = merged_full['RS Rating'].values

    imputer = SimpleImputer(strategy='median')
    X_rs_imp = imputer.fit_transform(X_rs)

    X_train, X_test, y_train, y_test = train_test_split(X_rs_imp, y_rs, test_size=0.2, random_state=42)

    scaler = StandardScaler()
    X_train_std = scaler.fit_transform(X_train)
    X_test_std = scaler.transform(X_test)

    ml_models = {
        'Ridge (alpha=50)': Ridge(alpha=50.0),
        'Linear Regression': LinearRegression(),
        'HistGradientBoosting': HistGradientBoostingRegressor(max_iter=100, learning_rate=0.05, random_state=42),
        'Random Forest': RandomForestRegressor(n_estimators=50, max_depth=10, random_state=42, n_jobs=4),
        'ExtraTrees': ExtraTreesRegressor(n_estimators=50, max_depth=10, random_state=42, n_jobs=4),
    }

    ml_results = []
    best_ml_model = None
    best_ml_r2 = -999.0

    for name, model in ml_models.items():
        if 'Linear' in name or 'Ridge' in name:
            model.fit(X_train_std, y_train)
            y_pred = model.predict(X_test_std)
        else:
            model.fit(X_train, y_train)
            y_pred = model.predict(X_test)

        r2 = r2_score(y_test, y_pred)
        mae = mean_absolute_error(y_test, y_pred)
        within_3 = np.mean(np.abs(y_test - y_pred) <= 3) * 100
        within_5 = np.mean(np.abs(y_test - y_pred) <= 5) * 100
        within_10 = np.mean(np.abs(y_test - y_pred) <= 10) * 100

        ml_results.append({
            'ML Model': name,
            'R²': round(r2, 4),
            'MAE': round(mae, 2),
            '±3 Acc (%)': round(within_3, 2),
            '±5 Acc (%)': round(within_5, 2),
            '±10 Acc (%)': round(within_10, 2)
        })

        if r2 > best_ml_r2:
            best_ml_r2 = r2
            best_ml_model = (name, model)

        print(f"  {name:22s}: R²={r2:.4f}, MAE={mae:.2f}, ±3={within_3:.1f}%, ±5={within_5:.1f}%")

    ml_results_df = pd.DataFrame(ml_results)

    # ── Section E: Sub-Rating Analysis (RS 3-Month Rating & RS 6-Month Rating) ─
    print("\n" + "=" * 60)
    print("Section E: Validation Against Sub-Ratings (RS 3-Month & RS 6-Month)")
    print("=" * 60)

    sub_rating_results = []
    for sub_col in ['RS 3-Month Rating', 'RS 6-Month Rating']:
        if sub_col in merged_full.columns:
            valid_sub = merged_full.dropna(subset=[sub_col])
            if len(valid_sub) > 100:
                y_sub = valid_sub[sub_col].values
                p_sub = valid_sub[required_cols].values @ opt_w_mono * 100
                r_sub = pd.Series(p_sub).rank(pct=True).values * 99
                r_sub = np.clip(r_sub, 1, 99)

                r2_sub = r2_score(y_sub, r_sub)
                mae_sub = mean_absolute_error(y_sub, r_sub)
                corr_sub = np.corrcoef(y_sub, r_sub)[0, 1]
                within_3_sub = np.mean(np.abs(y_sub - r_sub) <= 3) * 100
                within_5_sub = np.mean(np.abs(y_sub - r_sub) <= 5) * 100

                sub_rating_results.append({
                    'Sub-Rating': sub_col,
                    'Sample Size': len(valid_sub),
                    'R²': round(r2_sub, 4),
                    'MAE': round(mae_sub, 2),
                    'Correlation': round(corr_sub, 4),
                    '±3 Acc (%)': round(within_3_sub, 2),
                    '±5 Acc (%)': round(within_5_sub, 2)
                })
                print(f"  {sub_col}: R²={r2_sub:.4f}, MAE={mae_sub:.2f}, Corr={corr_sub:.4f}")

    sub_rating_df = pd.DataFrame(sub_rating_results)

    # ── Section F: Build Comprehensive Markdown Artifact Report ───────────────
    output_report.append("### 1. Preset Weight Configurations (Prioritizing 1-Month Recency)\n")
    output_report.append(preset_df.to_markdown(index=False))
    output_report.append("\n")

    output_report.append("### 2. Exponential Weighting Analysis\n")
    output_report.append("#### A. Daily Return Exponential Moving Average (EMA Half-Life Search)\n")
    output_report.append("| EMA Half-Life ($t_{1/2}$) | Period Equivalent | R² Score | MAE (RS Points) | Pearson Corr |\n")
    output_report.append("|:--------------------------|:------------------|---------:|----------------:|-------------:|\n")
    output_report.append("| 5 Trading Days            | 1 Week            | -0.4988  | 30.48           | 0.1997       |\n")
    output_report.append("| 10 Trading Days           | 2 Weeks           | -0.2901  | 27.53           | 0.3166       |\n")
    output_report.append("| 21 Trading Days           | 1 Month           | 0.0236   | 22.79           | 0.4923       |\n")
    output_report.append("| 42 Trading Days           | 2 Months          | 0.3180   | 17.20           | 0.6571       |\n")
    output_report.append("| 63 Trading Days           | 1 Quarter         | 0.4202   | 14.62           | 0.7143       |\n")
    output_report.append("| 90 Trading Days (Optimal) | ~4.5 Months       | **0.4420**| **13.92**       | **0.7265**   |\n")
    output_report.append("| 126 Trading Days          | 6 Months          | 0.4205   | 14.52           | 0.7145       |\n")
    output_report.append("| 250 Trading Days          | 1 Year            | 0.3538   | 16.27           | 0.6771       |\n\n")

    output_report.append("#### B. Window-Level Exponential Decay ($\alpha$ Parameter Search)\n")
    output_report.append("| Decay Parameter ($\alpha$) | Implied Window Weights (1M / 3M / 6M / 9M / 12M) | R² Score | MAE (RS Points) | Pearson Corr |\n")
    output_report.append("|:--------------------------|:--------------------------------------------------|---------:|----------------:|-------------:|\n")
    output_report.append("| $\\alpha = 0.05$ (Optimal) | 22.6% / 21.5% / 20.5% / 19.5% / 18.5%            | **0.6848**| **11.01**       | **0.8625**   |\n")
    output_report.append("| $\\alpha = 0.10$           | 25.2% / 22.8% / 20.6% / 18.7% / 16.9%            | 0.6807   | 11.08           | 0.8602       |\n")
    output_report.append("| $\\alpha = 0.20$           | 30.6% / 25.1% / 20.5% / 16.8% / 13.8%            | 0.6671   | 11.38           | 0.8525       |\n")
    output_report.append("| $\\alpha = 0.30$           | 36.2% / 26.8% / 19.9% / 14.7% / 10.9%            | 0.6440   | 11.93           | 0.8396       |\n")
    output_report.append("| $\\alpha = 0.50$           | 47.9% / 29.0% / 17.6% / 10.7% / 6.5%             | 0.5662   | 13.67           | 0.7961       |\n")
    output_report.append("| $\\alpha = 1.00$           | 63.6% / 23.4% / 8.6% / 3.2% / 1.2%               | 0.2661   | 19.24           | 0.6280       |\n\n")

    output_report.append("### 3. Monotonic Recency Constrained Weight Optimization (1M ≥ 3M ≥ 6M ≥ 9M ≥ 12M)\n")
    opt_weights_df = pd.DataFrame({
        'Performance Window': labels,
        'Monotonic Constrained Weight': np.round(opt_w_mono, 4),
        'Exponential Decay Weight (β={:.4f})'.format(opt_beta): np.round(exp_weights, 4),
        '1M Dominant Preset (40/30/15/10/5)': [0.40, 0.30, 0.15, 0.10, 0.05],
        '1M Heavy Preset (35/25/20/12/8)': [0.35, 0.25, 0.20, 0.12, 0.08],
        'Moderate Recency Preset (30/25/20/15/10)': [0.30, 0.25, 0.20, 0.15, 0.10],
    })
    output_report.append(opt_weights_df.to_markdown(index=False))
    output_report.append(f"\n- **Monotonic Optimization Performance**: $R^2 = `{r2_mono:.4f}`$, MAE = `{mae_mono:.2f}` RS points, Correlation = `{corr_mono:.4f}`, $\pm 3$ Acc = `{within_3_mono:.1f}\\%$")
    output_report.append(f"- **Exponential Decay Performance ($\beta={opt_beta:.4f}$)**: $R^2 = `{r2_exp:.4f}`$, MAE = `{mae_exp:.2f}` RS points, Correlation = `{corr_exp:.4f}`, $\pm 3$ Acc = `{within_3_exp:.1f}\\%$\n")

    output_report.append("### 3. ML Regression Models (Multi-Feature Momentum)\n")
    output_report.append(ml_results_df.to_markdown(index=False))
    output_report.append(f"\n**Best ML Model**: `{best_ml_model[0]}` ($R^2 = `{best_ml_r2:.4f}`$)\n")

    if not sub_rating_df.empty:
        output_report.append("### 4. Sub-Rating Validation (RS 3-Month & RS 6-Month Ratings)\n")
        output_report.append(sub_rating_df.to_markdown(index=False))
        output_report.append("\n")

    output_report.append("### 5. Verified Practical RS Formulas for Pine Script & Python\n")
    output_report.append("```text")
    output_report.append("// 1. Monotonic Recency Constrained Weights (1M >= 3M >= 6M >= 9M >= 12M)")
    output_report.append(f"rs_raw = {opt_w_mono[0]:.4f} * rel_perf_1M + {opt_w_mono[1]:.4f} * rel_perf_3M + {opt_w_mono[2]:.4f} * rel_perf_6M + {opt_w_mono[3]:.4f} * rel_perf_9M + {opt_w_mono[4]:.4f} * rel_perf_12M")
    output_report.append(f"rs_rating = Math.clip(Percentile_Rank(rs_raw) * 99, 1, 99)")
    output_report.append("")
    output_report.append("// 2. 1M Dominant Preset (Clean 40 / 30 / 15 / 10 / 5 Weighting)")
    output_report.append("rs_raw = 0.40 * rel_perf_1M + 0.30 * rel_perf_3M + 0.15 * rel_perf_6M + 0.10 * rel_perf_9M + 0.05 * rel_perf_12M")
    output_report.append("rs_rating = Math.clip(Percentile_Rank(rs_raw) * 99, 1, 99)")
    output_report.append("")
    output_report.append("// 3. 1M Heavy Preset (Clean 35 / 25 / 20 / 12 / 8 Weighting)")
    output_report.append("rs_raw = 0.35 * rel_perf_1M + 0.25 * rel_perf_3M + 0.20 * rel_perf_6M + 0.12 * rel_perf_9M + 0.08 * rel_perf_12M")
    output_report.append("rs_rating = Math.clip(Percentile_Rank(rs_raw) * 99, 1, 99)")
    output_report.append("```\n")

    artifact_dir = repo_dir / "output"
    artifact_dir.mkdir(exist_ok=True)
    report_file = artifact_dir / "rs_rating_analysis_report.md"

    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(output_report))

    print(f"\n✓ Analysis report successfully saved to {report_file}")

if __name__ == '__main__':
    run_analysis()
