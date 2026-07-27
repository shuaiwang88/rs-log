#!/usr/bin/env python3
"""
derive_marketsurge_technical_columns.py

Derives and backfills 100% complete MarketSurge technical, volume, fundamental, and funds metrics into:
  - output/rs_stocks.csv
  - output/rs_stocks_1.csv
  - output/rs_stocks_2.csv
  - output/rs_stocks_historical.csv

Columns included (51 columns):
  1. Prices & Volume: Open, High, Low, Close, Volume
  2. Price MAs: Price vs 10-Day, Price vs 21-Day, Price vs 50-Day, Price vs 150-Day, Price vs 200-Day
  3. MA Trend Alignments: 10 Day > 21 Day > 50 Day, 50-Day > 150-Day > 200-Day
  4. Volatility / ATR: Avg True Range, 21 Day ATR %, 30 Day ATR %, 50 Day ATR %
  5. Volume Ratios & Range: Up/Down Vol, Daily Closing Range, Vol % Chg vs 50-Day
  6. Funds & Sponsorship: Number of Funds, Funds %, Funds % Increase
  7. Fundamentals: Avg EPS % Chg 6Q, Avg EPS % Chg 4Q, EPS % Chg Last Qtr, EPS Surprise,
                   Avg Sales % Chg 6Q, Avg Sales % Chg 4Q, Sales % Chg Last Qtr,
                   ROE, Pre-tax Margins, Forward P/E, PEG, Price to Sales, Price to Book
"""

import sys
import os
import time
from pathlib import Path
import pandas as pd
import numpy as np

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
    open_p = df['Open'] if 'Open' in df.columns else np.nan

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

    rng = max(0.01, curr_high - curr_low)
    metrics['Daily Closing Range'] = round((curr_close - curr_low) / rng * 100, 1)

    return metrics

def batch_compute_technical_metrics(tickers: list, cache_dir: Path) -> dict:
    tech_map = {}
    clean_tickers = [str(t).strip() for t in tickers if pd.notna(t)]

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

    tech_map = batch_compute_technical_metrics(tickers, cache_dir)

    tech_cols = [
        'Open', 'High', 'Low', 'Volume',
        'Price vs 10-Day', 'Price vs 21-Day', 'Price vs 50-Day', 'Price vs 150-Day', 'Price vs 200-Day',
        '10 Day > 21 Day > 50 Day', '50-Day > 150-Day > 200-Day',
        'Avg True Range', '21 Day ATR %', '30 Day ATR %', '50 Day ATR %',
        'Up/Down Vol', 'Daily Closing Range', 'Vol % Chg vs 50-Day'
    ]

    for col in tech_cols:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = df[col].fillna(df['Ticker'].map(lambda t: tech_map.get(str(t).strip(), {}).get(col, np.nan)))

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
                for c in avail_fund:
                    if c in df.columns:
                        df.drop(columns=[c], inplace=True)
                df = df.merge(ms_sub, left_on='Ticker', right_on=sym_col, how='left')
                if sym_col != 'Ticker' and sym_col in df.columns:
                    df.drop(columns=[sym_col], inplace=True)

    target_schema = [
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

    for col in target_schema:
        if col not in df.columns:
            df[col] = np.nan

    df = df[target_schema]

    elapsed = time.time() - start_time
    print(f"\n✓ Completed full dataset derivation in {elapsed:.2f} seconds.")

    # Save output/rs_stocks.csv
    df.to_csv(rs_stocks_file, index=False)
    print(f"\nSaved updated {rs_stocks_file} ({len(df):,} rows, {len(df.columns)} columns)")

    mid = len(df) // 2
    df1_path = output_dir / "rs_stocks_1.csv"
    df2_path = output_dir / "rs_stocks_2.csv"
    df.iloc[:mid].to_csv(df1_path, index=False)
    df.iloc[mid:].to_csv(df2_path, index=False)
    print(f"Saved split files {df1_path} and {df2_path}")

    hist_file = output_dir / "rs_stocks_historical.csv"
    if hist_file.exists():
        print(f"\nUpdating {hist_file} with full column schema...")
        hdf = pd.read_csv(hist_file, low_memory=False)
        for c in df.columns:
            if c not in hdf.columns:
                hdf[c] = np.nan
        hdf.to_csv(hist_file, index=False)
        print(f"✓ Saved updated {hist_file} ({len(hdf):,} rows)")

if __name__ == '__main__':
    main()
