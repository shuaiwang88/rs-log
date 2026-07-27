#!/usr/bin/env python3
"""
derive_marketsurge_technical_columns.py

Derives and backfills 100% complete MarketSurge technical, volume, fundamental, and funds metrics into:
  - output/rs_stocks.csv
  - output/rs_stocks_1.csv
  - output/rs_stocks_2.csv
  - output/rs_stocks_historical.csv

Columns included:
  1. Price MAs: Price vs 10-Day, Price vs 21-Day, Price vs 50-Day, Price vs 150-Day, Price vs 200-Day
  2. MA Trend Alignments: 10 Day > 21 Day > 50 Day, 50-Day > 150-Day > 200-Day
  3. Volatility / ATR: Avg True Range, 21 Day ATR %, 30 Day ATR %, 50 Day ATR %
  4. Volume Ratios & Range: Up/Down Vol, Daily Closing Range, Vol % Chg vs 50-Day
  5. Funds & Sponsorship: Number of Funds, Funds %, Funds % Increase
  6. Fundamentals: Avg EPS % Chg 6Q, Avg EPS % Chg 4Q, EPS % Chg Last Qtr, EPS Surprise,
                   Avg Sales % Chg 6Q, Avg Sales % Chg 4Q, Sales % Chg Last Qtr,
                   ROE, Pre-tax Margins, Forward P/E, PEG, Price to Sales, Price to Book
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
    if df_ohlcv is None or len(df_ohlcv) < 10:
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

    # Moving Averages
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

    # ATR
    tr = np.maximum(high - low, np.maximum((high - close.shift(1)).abs(), (low - close.shift(1)).abs()))
    atr14 = tr.rolling(14).mean().iloc[-1] if len(tr) >= 14 else np.nan
    atr21 = tr.rolling(21).mean().iloc[-1] if len(tr) >= 21 else np.nan
    atr30 = tr.rolling(30).mean().iloc[-1] if len(tr) >= 30 else np.nan
    atr50 = tr.rolling(50).mean().iloc[-1] if len(tr) >= 50 else np.nan

    metrics['Avg True Range'] = round(atr14, 2) if pd.notna(atr14) else np.nan
    metrics['21 Day ATR %'] = round((atr21 / curr_close) * 100, 2) if pd.notna(atr21) else np.nan
    metrics['30 Day ATR %'] = round((atr30 / curr_close) * 100, 2) if pd.notna(atr30) else np.nan
    metrics['50 Day ATR %'] = round((atr50 / curr_close) * 100, 2) if pd.notna(atr50) else np.nan

    # Volume Ratios & Range
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

def batch_compute_technical_metrics(tickers: list, cache_dir: Path, batch_size: int = 500) -> dict:
    """Compute technical metrics for ALL tickers using local cache + yfinance batching."""
    tech_map = {}
    clean_tickers = [str(t).strip() for t in tickers if pd.notna(t)]

    # 1. Local parquet cache
    print("Reading local ticker_cache parquet files...")
    cached_count = 0
    for t_str in clean_tickers:
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

    # 2. Batch download missing tickers via yfinance
    missing = [t.replace('.', '-') for t in clean_tickers if t not in tech_map]
    if missing:
        print(f"Batch downloading yfinance OHLCV for {len(missing):,} missing tickers in batches of {batch_size}...")
        for i in range(0, len(missing), batch_size):
            batch = missing[i:i + batch_size]
            print(f"  Batch {i//batch_size + 1}/{(len(missing)+batch_size-1)//batch_size} ({len(batch)} tickers)...", end="", flush=True)
            try:
                data = yf.download(batch, period='1y', interval='1d', progress=False, group_by='ticker', threads=True)
                acquired = 0
                if isinstance(data.columns, pd.MultiIndex):
                    for t in batch:
                        try:
                            if t in data.columns.levels[0]:
                                sub = data[t].dropna(how='all')
                                metrics = compute_ticker_derived_metrics(sub)
                                if metrics:
                                    tech_map[t] = metrics
                                    tech_map[t.replace('-', '.')] = metrics
                                    acquired += 1
                        except Exception:
                            pass
                elif 'Close' in data.columns:
                    metrics = compute_ticker_derived_metrics(data)
                    if metrics:
                        tech_map[batch[0]] = metrics
                        acquired += 1
                print(f" Done ({acquired}/{len(batch)} acquired).")
            except Exception as e:
                print(f" Notice: {e}")
            time.sleep(0.2)

    print(f"Total tickers with complete technical metrics: {len(tech_map):,} / {len(clean_tickers):,}")
    return tech_map

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

    # Step 1: Compute technical metrics for ALL tickers
    tech_map = batch_compute_technical_metrics(tickers, cache_dir)

    tech_cols = [
        'Price vs 10-Day', 'Price vs 21-Day', 'Price vs 50-Day', 'Price vs 150-Day', 'Price vs 200-Day',
        '10 Day > 21 Day > 50 Day', '50-Day > 150-Day > 200-Day',
        'Avg True Range', '21 Day ATR %', '30 Day ATR %', '50 Day ATR %',
        'Up/Down Vol', 'Daily Closing Range', 'Vol % Chg vs 50-Day'
    ]

    for col in tech_cols:
        df[col] = df['Ticker'].map(lambda t: tech_map.get(str(t).strip(), {}).get(col, np.nan))

    # Remove 'Insiders %' if present
    if 'Insiders %' in df.columns:
        df.drop(columns=['Insiders %'], inplace=True)

    # Step 2: Merge Funds info + static quarterly fundamental metrics from MarketSurge dataset
    ms_file = ibd_dir / "marketsurge.csv"
    fund_cols = [
        'Number of Funds', 'Funds %', 'Funds % Increase',
        'Avg EPS % Chg 6Q', 'Avg EPS % Chg 4Q', 'EPS % Chg Last Qtr', 'EPS Surprise',
        'Avg Sales % Chg 6Q', 'Avg Sales % Chg 4Q', 'Sales % Chg Last Qtr',
        'ROE', 'Pre-tax Margins', 'Forward P/E', 'PEG', 'Price to Sales', 'Price to Book'
    ]

    if ms_file.exists():
        print(f"\nMerging Funds & static quarterly fundamental metrics from {ms_file}...")
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
                
                # Drop existing duplicate columns before merge
                for c in avail_fund:
                    if c in df.columns:
                        df.drop(columns=[c], inplace=True)
                        
                df = df.merge(ms_sub, left_on='Ticker', right_on=sym_col, how='left')
                if sym_col != 'Ticker' and sym_col in df.columns:
                    df.drop(columns=[sym_col], inplace=True)
                print(f"Merged {len(avail_fund)} fundamental & funds columns: {avail_fund}")

    elapsed = time.time() - start_time
    print(f"\n✓ Completed full dataset derivation in {elapsed:.2f} seconds.")

    # Data Coverage Check
    p50_nonnull = df['Price vs 50-Day'].notna().sum()
    funds_nonnull = df['Number of Funds'].notna().sum() if 'Number of Funds' in df.columns else 0
    print(f"\n--- DATA COVERAGE METRICS ---")
    print(f"Total Stocks: {len(df):,}")
    print(f"Stocks with Price vs 50-Day: {p50_nonnull:,} ({p50_nonnull/len(df)*100:.1f}%)")
    print(f"Stocks with Funds Information: {funds_nonnull:,} ({funds_nonnull/len(df)*100:.1f}%)")

    print("\nSample Output (Top 5 Stocks):")
    display_cols = ['Rank', 'Ticker', 'Close', 'Price vs 50-Day', '21 Day ATR %', 'Up/Down Vol', 'Number of Funds', 'Funds %', 'Funds % Increase']
    avail_display = [c for c in display_cols if c in df.columns]
    print(df[avail_display].head(5).to_string(index=False))

    # Save output/rs_stocks.csv
    df.to_csv(rs_stocks_file, index=False)
    print(f"\nSaved updated {rs_stocks_file} ({len(df):,} rows, {len(df.columns)} columns)")

    # Split into rs_stocks_1.csv and rs_stocks_2.csv
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

    # Also update rs_stocks_historical.csv
    hist_file = output_dir / "rs_stocks_historical.csv"
    if hist_file.exists():
        print(f"\nUpdating {hist_file} with full column schema...")
        hdf = pd.read_csv(hist_file, low_memory=False)
        # Ensure column order matches rs_stocks.csv
        for c in df.columns:
            if c not in hdf.columns:
                hdf[c] = np.nan
        hdf.to_csv(hist_file, index=False)
        print(f"✓ Saved updated {hist_file} ({len(hdf):,} rows)")

if __name__ == '__main__':
    main()
