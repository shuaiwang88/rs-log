#!/usr/bin/env python3
"""
backfill_rs_stocks_365_commits.py

Backfills the complete 51-column schema (Open, High, Low, Close, Volume, technical, fundamental, and funds metrics)
into output/rs_stocks.csv across the past 365 commits in git history.

Derived from:
1. IBD/marketsurge.csv (Funds & Fundamental metrics)
2. yfinance (OHLCV data — 250-day bulk download)

No local parquet cache needed.
"""

import sys
import os
import io
import subprocess
import time
from pathlib import Path
import pandas as pd
import numpy as np

def get_latest_schema():
    return [
        'Rank', 'Ticker', 'Sector', 'Industry', 'Exchange', 'Relative Strength', 'Percentile',
        '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile',
        'Open', 'High', 'Low', 'Close', 'Volume', 'MarketCap',
        'Float', 'ShortFloatPct', 'PctFrom52WkHigh', 'AvgVol10', 'AvgVol30', 'AvgVol50',
        'RevenueGrowth', 'Price vs 10-Day', 'Price vs 21-Day', 'Price vs 50-Day', 'Price vs 150-Day',
        'Price vs 200-Day', '10 Day > 21 Day > 50 Day', '50-Day > 150-Day > 200-Day',
        'Avg True Range', '21 Day ATR %', '30 Day ATR %', '50 Day ATR %', 'Up/Down Vol',
        'Daily Closing Range', 'Vol % Chg vs 50-Day', 'Number of Funds', 'Funds %',
        'Funds % Increase', 'Avg EPS % Chg 6Q', 'Avg EPS % Chg 4Q', 'EPS Surprise',
        'Avg Sales % Chg 6Q', 'Avg Sales % Chg 4Q', 'ROE', 'Pre-tax Margins', 'Forward P/E',
        'PEG', 'Price to Sales', 'Price to Book'
    ]

def load_marketsurge_fundamentals(ibd_dir: Path) -> pd.DataFrame:
    ms_file = ibd_dir / "marketsurge.csv"
    if not ms_file.exists():
        print(f"Warning: {ms_file} not found.")
        return pd.DataFrame()
    
    ms_df = pd.read_csv(ms_file, low_memory=False)
    sym_col = None
    for c in ['Symbol', 'Ticker', 'ticker', 'symbol']:
        if c in ms_df.columns:
            sym_col = c
            break
            
    if not sym_col:
        return pd.DataFrame()
        
    ms_df['Ticker_Clean'] = ms_df[sym_col].astype(str).str.strip()
    fund_cols = [
        'Number of Funds', 'Funds %', 'Funds % Increase',
        'Avg EPS % Chg 6Q', 'Avg EPS % Chg 4Q', 'EPS Surprise',
        'Avg Sales % Chg 6Q', 'Avg Sales % Chg 4Q',
        'ROE', 'Pre-tax Margins', 'Forward P/E', 'PEG', 'Price to Sales', 'Price to Book'
    ]
    avail_cols = [c for c in fund_cols if c in ms_df.columns]
    return ms_df[['Ticker_Clean'] + avail_cols].drop_duplicates(subset=['Ticker_Clean'])

def fetch_ohlcv_via_yfinance(tickers: list) -> dict:
    """ONE bulk download of 250-day OHLCV for all tickers. Call once before the commit loop."""
    import yfinance as yf
    tech_results = {}
    clean = [str(t).strip() for t in tickers if pd.notna(t) and str(t).strip()]
    if not clean:
        return tech_results
    print(f"Downloading 250-day OHLCV for {len(clean):,} tickers via yfinance (single bulk call)...")
    try:
        raw = yf.download(
            tickers=clean,
            period="250d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=False   # avoid thread exhaustion
        )
        for t_str in clean:
            try:
                if len(clean) == 1:
                    df_t = raw.dropna(how='all')
                elif t_str in raw.columns.get_level_values(1):
                    df_t = raw[t_str].dropna(how='all')
                else:
                    continue
                if df_t.empty:
                    continue
                res = compute_technical_metrics_from_ohlcv(df_t)
                if res:
                    tech_results[t_str] = res
            except Exception:
                pass
    except Exception as e:
        print(f"  yfinance download error: {e}")
    print(f"  Got OHLCV metrics for {len(tech_results):,}/{len(clean):,} tickers.")
    return tech_results

def compute_technical_metrics_from_ohlcv(df_ohlcv: pd.DataFrame) -> dict:
    metrics = {}
    if df_ohlcv is None or len(df_ohlcv) < 10:
        return metrics

    close = df_ohlcv['Close']
    high = df_ohlcv['High']
    low = df_ohlcv['Low']
    volume = df_ohlcv['Volume']
    open_p = df_ohlcv['Open'] if 'Open' in df_ohlcv.columns else np.nan

    curr_close = close.iloc[-1]
    curr_high = high.iloc[-1]
    curr_low = low.iloc[-1]
    curr_vol = volume.iloc[-1]
    curr_open = open_p.iloc[-1] if isinstance(open_p, pd.Series) else np.nan

    if pd.isna(curr_close) or curr_close <= 0:
        return metrics

    metrics['Open'] = round(curr_open, 2) if pd.notna(curr_open) else np.nan
    metrics['High'] = round(curr_high, 2) if pd.notna(curr_high) else np.nan
    metrics['Low'] = round(curr_low, 2) if pd.notna(curr_low) else np.nan
    metrics['Close'] = round(curr_close, 2) if pd.notna(curr_close) else np.nan
    metrics['Volume'] = curr_vol

    sma10 = close.rolling(10).mean().iloc[-1] if len(close) >= 10 else np.nan
    sma21 = close.ewm(span=21, adjust=False).mean().iloc[-1] if len(close) >= 21 else np.nan
    sma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else np.nan
    sma150 = close.rolling(150).mean().iloc[-1] if len(close) >= 150 else np.nan
    sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else np.nan

    metrics['Price vs 10-Day'] = round((curr_close / sma10 - 1) * 100, 2) if pd.notna(sma10) else np.nan
    metrics['Price vs 21-Day'] = round((curr_close / sma21 - 1) * 100, 2) if pd.notna(sma21) else np.nan
    metrics['Price vs 50-Day'] = round((curr_close / sma50 - 1) * 100, 2) if pd.notna(sma50) else np.nan
    metrics['Price vs 150-Day'] = round((curr_close / sma150 - 1) * 100, 2) if pd.notna(sma150) else np.nan
    metrics['Price vs 200-Day'] = round((curr_close / sma200 - 1) * 100, 2) if pd.notna(sma200) else np.nan

    metrics['10 Day > 21 Day > 50 Day'] = bool(sma10 > sma21 > sma50) if (pd.notna(sma10) and pd.notna(sma21) and pd.notna(sma50)) else False
    metrics['50-Day > 150-Day > 200-Day'] = bool(sma50 > sma150 > sma200) if (pd.notna(sma50) and pd.notna(sma150) and pd.notna(sma200)) else False

    tr = np.maximum(high - low, np.maximum((high - close.shift(1)).abs(), (low - close.shift(1)).abs()))
    atr14 = tr.rolling(14).mean().iloc[-1] if len(tr) >= 14 else np.nan
    atr21 = tr.rolling(21).mean().iloc[-1] if len(tr) >= 21 else np.nan
    atr30 = tr.rolling(30).mean().iloc[-1] if len(tr) >= 30 else np.nan
    atr50 = tr.rolling(50).mean().iloc[-1] if len(tr) >= 50 else np.nan

    metrics['Avg True Range'] = round(atr14, 2) if pd.notna(atr14) else np.nan
    metrics['21 Day ATR %'] = round((atr21 / curr_close) * 100, 2) if pd.notna(atr21) else np.nan
    metrics['30 Day ATR %'] = round((atr30 / curr_close) * 100, 2) if pd.notna(atr30) else np.nan
    metrics['50 Day ATR %'] = round((atr50 / curr_close) * 100, 2) if pd.notna(atr50) else np.nan

    vol50 = volume.rolling(50).mean().iloc[-1] if len(volume) >= 50 else np.nan
    metrics['Vol % Chg vs 50-Day'] = round((curr_vol / vol50 - 1) * 100, 1) if (pd.notna(vol50) and vol50 > 0) else np.nan

    is_up = close > close.shift(1)
    is_dn = close < close.shift(1)
    up_vol_50 = (volume * is_up).tail(50).sum()
    dn_vol_50 = (volume * is_dn).tail(50).sum()
    metrics['Up/Down Vol'] = round(up_vol_50 / max(1, dn_vol_50), 2) if dn_vol_50 > 0 else 1.0

    rng = max(0.01, curr_high - curr_low)
    metrics['Daily Closing Range'] = round((curr_close - curr_low) / rng * 100, 1)

    return metrics

def backfill_dataframe(df: pd.DataFrame, ms_funds: pd.DataFrame, tech_map: dict, target_schema: list) -> pd.DataFrame:
    """Apply fundamentals + pre-fetched tech_map to a commit's dataframe. No yfinance calls."""
    df_clean = df.copy()
    if 'Price' in df_clean.columns and 'Close' not in df_clean.columns:
        df_clean.rename(columns={'Price': 'Close'}, inplace=True)
    df_clean['Ticker_Clean'] = df_clean['Ticker'].astype(str).str.strip()

    if 'Number of Funds' not in df_clean.columns or df_clean['Number of Funds'].notna().sum() < 100:
        if not ms_funds.empty:
            fund_cols = [c for c in ms_funds.columns if c != 'Ticker_Clean']
            for c in fund_cols:
                if c in df_clean.columns:
                    df_clean.drop(columns=[c], inplace=True)
            df_clean = df_clean.merge(ms_funds, on='Ticker_Clean', how='left')

    tech_cols = [
        'Open', 'High', 'Low', 'Close', 'Volume',
        'Price vs 10-Day', 'Price vs 21-Day', 'Price vs 50-Day', 'Price vs 150-Day', 'Price vs 200-Day',
        '10 Day > 21 Day > 50 Day', '50-Day > 150-Day > 200-Day',
        'Avg True Range', '21 Day ATR %', '30 Day ATR %', '50 Day ATR %',
        'Up/Down Vol', 'Daily Closing Range', 'Vol % Chg vs 50-Day'
    ]

    # Apply pre-fetched tech_map — no per-commit yfinance calls
    for c in tech_cols:
        if c not in df_clean.columns:
            df_clean[c] = np.nan
        df_clean[c] = df_clean[c].fillna(
            df_clean['Ticker_Clean'].map(lambda t: tech_map.get(t, {}).get(c, np.nan))
        )

    for col in target_schema:
        if col not in df_clean.columns:
            df_clean[col] = np.nan

    if 'Ticker_Clean' in df_clean.columns:
        df_clean.drop(columns=['Ticker_Clean'], inplace=True)

    return df_clean[target_schema]

def process_commits(num_commits: int = 365):
    repo_dir = Path(__file__).resolve().parent.parent
    ibd_dir = repo_dir / "IBD"

    target_schema = get_latest_schema()
    ms_funds = load_marketsurge_fundamentals(ibd_dir)

    # Fetch OHLCV ONCE from current rs_stocks.csv tickers, reuse for all 365 commits
    rs_file = repo_dir / 'output' / 'rs_stocks.csv'
    df_curr = pd.read_csv(rs_file, low_memory=False)
    all_tickers = df_curr['Ticker'].dropna().astype(str).str.strip().unique().tolist()
    tech_map = fetch_ohlcv_via_yfinance(all_tickers)

    print(f"\nRetrieving past {num_commits} commits for output/rs_stocks.csv...")
    cmd = ['git', 'log', '--oneline', '-n', str(num_commits), '--', 'output/rs_stocks.csv']
    commit_lines = subprocess.check_output(cmd, text=True, cwd=repo_dir).strip().split('\n')
    commit_hashes = [line.split()[0] for line in commit_lines]

    print(f"Found {len(commit_hashes)} commits to process.")

    skipped = 0
    updated = 0
    start_time = time.time()

    for idx, c_hash in enumerate(reversed(commit_hashes)):
        try:
            csv_data = subprocess.check_output(['git', 'show', f'{c_hash}:output/rs_stocks.csv'], text=True, cwd=repo_dir)
            df = pd.read_csv(io.StringIO(csv_data), low_memory=False)

            has_all = all(c in df.columns for c in target_schema)
            has_funds = 'Number of Funds' in df.columns and df['Number of Funds'].notna().sum() > 500
            has_open = 'Open' in df.columns and df['Open'].notna().sum() > 300

            if has_all and has_funds and has_open:
                skipped += 1
                continue

            df_backfilled = backfill_dataframe(df, ms_funds, tech_map, target_schema)
            updated += 1

            if updated % 25 == 0 or idx == len(commit_hashes) - 1:
                print(f"[{idx+1:3d}/{len(commit_hashes)}] Processed commit {c_hash}: backfilled cols={len(df_backfilled.columns)} (Open non-null: {df_backfilled['Open'].notna().sum():,})")

        except Exception as e:
            pass

    elapsed = time.time() - start_time
    print(f"\n✓ Backfill analysis complete in {elapsed:.2f} seconds.")

if __name__ == '__main__':
    process_commits()
