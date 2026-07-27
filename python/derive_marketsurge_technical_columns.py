#!/usr/bin/env python3
"""
derive_marketsurge_technical_columns.py

Derives and backfills MarketSurge technical, volume, fundamental, and yfinance funds/institutional metrics into
output/rs_stocks.csv, output/rs_stocks_1.csv, output/rs_stocks_2.csv, and output/rs_stocks_historical.csv.
"""

import sys
import os
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import yfinance as yf

def compute_ticker_derived_metrics(df_ohlcv: pd.DataFrame) -> dict:
    metrics = {}
    if len(df_ohlcv) < 15:
        return metrics

    df = df_ohlcv.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    close = df['Close']
    high = df['High']
    low = df['Low']
    volume = df['Volume']

    curr_close = close.iloc[-1]
    curr_vol = volume.iloc[-1]

    if pd.isna(curr_close) or curr_close <= 0:
        return metrics

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

    curr_high = high.iloc[-1]
    curr_low = low.iloc[-1]
    rng = max(0.01, curr_high - curr_low)
    metrics['Daily Closing Range'] = round((curr_close - curr_low) / rng * 100, 1)

    return metrics

def _fetch_single_ticker_funds(t_str: str):
    try:
        tk = yf.Ticker(t_str.replace('.', '-'))
        inst_count = np.nan
        inst_pct = np.nan
        insider_pct = np.nan
        try:
            mh = tk.major_holders
            if mh is not None and not mh.empty:
                val_col = 'Value' if 'Value' in mh.columns else mh.columns[0]
                for idx, row in mh.iterrows():
                    row_key = str(idx).lower()
                    val = row[val_col]
                    if 'institutionscount' in row_key:
                        inst_count = int(float(val))
                    elif 'institutionspercentheld' in row_key:
                        inst_pct = float(val) * 100
                    elif 'insiderspercentheld' in row_key:
                        insider_pct = float(val) * 100
        except Exception:
            pass
            
        if pd.isna(inst_pct) or pd.isna(inst_count):
            try:
                info = tk.info
                if pd.isna(inst_pct) and info.get('heldPercentInstitutions') is not None:
                    inst_pct = info.get('heldPercentInstitutions') * 100
                if pd.isna(insider_pct) and info.get('heldPercentInsiders') is not None:
                    insider_pct = info.get('heldPercentInsiders') * 100
            except Exception:
                pass

        if pd.notna(inst_count) or pd.notna(inst_pct):
            return t_str, {
                'Number of Funds': inst_count,
                'Funds %': round(inst_pct, 2) if pd.notna(inst_pct) else np.nan,
                'Insiders %': round(insider_pct, 2) if pd.notna(insider_pct) else np.nan
            }
    except Exception:
        pass
    return t_str, None

def fetch_yfinance_funds_info(tickers: list, limit: int = 1500, max_workers: int = 20) -> dict:
    funds_map = {}
    target_tickers = [str(t).strip() for t in tickers[:limit] if pd.notna(t)]
    print(f"Fetching yfinance Funds & Institutional Holdings for {len(target_tickers)} tickers in parallel...")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_single_ticker_funds, t): t for t in target_tickers}
        for future in as_completed(futures):
            t_str, res = future.result()
            if res:
                funds_map[t_str] = res

    print(f"Acquired yfinance Funds & Institutional data for {len(funds_map):,} tickers.")
    return funds_map

def main():
    repo_dir = Path(__file__).resolve().parent.parent
    output_dir = repo_dir / "output"
    cache_dir = repo_dir / "ticker_cache"
    ibd_dir = repo_dir / "IBD"

    rs_stocks_file = output_dir / "rs_stocks.csv"
    if not rs_stocks_file.exists():
        print(f"Error: {rs_stocks_file} does not exist.")
        sys.exit(1)

    print(f"Reading {rs_stocks_file}...")
    df = pd.read_csv(rs_stocks_file)
    print(f"Loaded {len(df):,} stocks.")

    tickers = df['Ticker'].dropna().tolist()
    start_time = time.time()

    # Step 1: Compute derived technical metrics from local ticker_cache
    print("\nComputing MarketSurge derived technical metrics from local ticker_cache...")
    tech_map = {}
    cached_count = 0

    for t in tickers:
        t_str = str(t).strip()
        p1 = cache_dir / f"{t_str}_1d.parquet"
        p2 = cache_dir / f"{t_str.replace('.', '-')}_1d.parquet"
        target_path = p1 if p1.exists() else (p2 if p2.exists() else None)
        
        if target_path:
            try:
                cdf = pd.read_parquet(target_path)
                metrics = compute_ticker_derived_metrics(cdf)
                if metrics:
                    tech_map[t_str] = metrics
                    cached_count += 1
            except Exception:
                pass

    print(f"Retrieved technical metrics for {cached_count:,} stocks from local cache.")

    tech_cols = [
        'Price vs 10-Day', 'Price vs 21-Day', 'Price vs 50-Day', 'Price vs 150-Day', 'Price vs 200-Day',
        '10 Day > 21 Day > 50 Day', '50-Day > 150-Day > 200-Day',
        'Avg True Range', '21 Day ATR %', '30 Day ATR %', '50 Day ATR %',
        'Up/Down Vol', 'Daily Closing Range', 'Vol % Chg vs 50-Day'
    ]

    for col in tech_cols:
        df[col] = df['Ticker'].map(lambda t: tech_map.get(str(t).strip(), {}).get(col, np.nan))

    # Step 2: Fetch Funds & Institutional Holdings directly from yfinance
    print("\nFetching Funds & Institutional Ownership directly from yfinance...")
    yfinance_funds_map = fetch_yfinance_funds_info(tickers, limit=1500)

    df['Number of Funds'] = df['Ticker'].map(lambda t: yfinance_funds_map.get(str(t).strip(), {}).get('Number of Funds', np.nan))
    df['Funds %'] = df['Ticker'].map(lambda t: yfinance_funds_map.get(str(t).strip(), {}).get('Funds %', np.nan))
    df['Insiders %'] = df['Ticker'].map(lambda t: yfinance_funds_map.get(str(t).strip(), {}).get('Insiders %', np.nan))

    if 'Funds % Increase' in df.columns:
        df.drop(columns=['Funds % Increase'], inplace=True)

    # Step 3: Merge static quarterly fundamental metrics from MarketSurge dataset
    ms_file = ibd_dir / "marketsurge.csv"
    fund_cols = [
        'Avg EPS % Chg 6Q', 'Avg EPS % Chg 4Q', 'EPS % Chg Last Qtr', 'EPS Surprise',
        'Avg Sales % Chg 6Q', 'Avg Sales % Chg 4Q', 'Sales % Chg Last Qtr',
        'ROE', 'Pre-tax Margins', 'Forward P/E', 'PEG', 'Price to Sales', 'Price to Book'
    ]

    if ms_file.exists():
        print(f"\nMerging static quarterly fundamental metrics from {ms_file}...")
        ms_df = pd.read_csv(ms_file, low_memory=False)
        sym_col = None
        for c in ['Symbol', 'Ticker', 'ticker', 'symbol']:
            if c in ms_df.columns:
                sym_col = c
                break

        if sym_col:
            ms_df[sym_col] = ms_df[sym_col].astype(str).str.strip()
            avail_fund = [c for c in fund_cols if c in ms_df.columns]
            if avail_fund:
                ms_sub = ms_df[[sym_col] + avail_fund].drop_duplicates(subset=[sym_col])
                
                for c in avail_fund:
                    if c in df.columns:
                        df.drop(columns=[c], inplace=True)
                        
                df = df.merge(ms_sub, left_on='Ticker', right_on=sym_col, how='left')
                if sym_col != 'Ticker' and sym_col in df.columns:
                    df.drop(columns=[sym_col], inplace=True)
                print(f"Merged {len(avail_fund)} static fundamental columns: {avail_fund}")

    elapsed = time.time() - start_time
    print(f"\n✓ Completed metrics processing in {elapsed:.2f} seconds.")

    print("\nSample Output (Top 5 Stocks with yfinance Funds / Institutional Data):")
    display_cols = ['Rank', 'Ticker', 'Price', 'Number of Funds', 'Funds %', 'Insiders %', 'ROE']
    avail_display = [c for c in display_cols if c in df.columns]
    print(df[avail_display].head(5).to_string(index=False))

    df.to_csv(rs_stocks_file, index=False)
    print(f"\nSaved updated {rs_stocks_file} ({len(df):,} rows, {len(df.columns)} columns)")

    n = len(df)
    mid = n // 2
    df1 = df.iloc[:mid]
    df2 = df.iloc[mid:]

    df1_path = output_dir / "rs_stocks_1.csv"
    df2_path = output_dir / "rs_stocks_2.csv"

    df1.to_csv(df1_path, index=False)
    df2.to_csv(df2_path, index=False)
    print(f"Saved {df1_path} ({len(df1):,} rows)")
    print(f"Saved {df2_path} ({len(df2):,} rows)")

if __name__ == '__main__':
    main()
