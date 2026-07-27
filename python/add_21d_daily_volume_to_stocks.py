#!/usr/bin/env python3
"""
add_21d_daily_volume_to_stocks.py

Fetches the past 21 trading days daily volume series for all tickers in output/rs_stocks.csv
using yfinance (period='1mo'), and adds Volume + 21 daily volume columns (Vol_D1..Vol_D21)
to output/rs_stocks.csv, output/rs_stocks_1.csv, and output/rs_stocks_2.csv.
"""

import sys
import os
import time
from pathlib import Path
import pandas as pd
import numpy as np
import yfinance as yf

def fetch_all_21_daily_volumes(tickers: list, batch_size: int = 300) -> dict:
    """Fetch past 21 trading days volume series for all tickers with yfinance period='1mo'."""
    vol_map = {}
    clean_tickers = [str(t).strip().replace('.', '-') for t in tickers if pd.notna(t)]
    total = len(clean_tickers)
    
    print(f"Downloading 21 daily volume bars for {total:,} tickers in batches of {batch_size}...")

    for i in range(0, total, batch_size):
        batch = clean_tickers[i:i + batch_size]
        print(f"Downloading batch {i//batch_size + 1}/{(total + batch_size - 1)//batch_size} ({len(batch)} tickers)...", end="", flush=True)
        try:
            data = yf.download(batch, period='1mo', interval='1d', progress=False, group_by='ticker', threads=True)
            batch_found = 0
            if isinstance(data.columns, pd.MultiIndex):
                for t in batch:
                    try:
                        if t in data.columns.levels[0]:
                            vs = data[t]['Volume'].dropna()
                            if len(vs) > 0:
                                vols = [int(v) for v in vs.tail(21).values[::-1]]
                                if len(vols) < 21:
                                    vols = vols + [vols[-1] if len(vols)>0 else 0] * (21 - len(vols))
                                vol_map[t] = vols
                                vol_map[t.replace('-', '.')] = vols
                                batch_found += 1
                    except Exception:
                        pass
            elif 'Volume' in data.columns:
                vs = data['Volume'].dropna()
                if len(vs) > 0:
                    vols = [int(v) for v in vs.tail(21).values[::-1]]
                    if len(vols) < 21:
                        vols = vols + [vols[-1] if len(vols)>0 else 0] * (21 - len(vols))
                    vol_map[batch[0]] = vols
                    batch_found += 1
            print(f" Done ({batch_found}/{len(batch)} acquired).")
        except Exception as e:
            print(f" Error: {e}")
        time.sleep(0.3)

    print(f"Total tickers successfully retrieved: {len(vol_map):,} / {total:,}")
    return vol_map

def main():
    repo_dir = Path(__file__).resolve().parent.parent
    output_dir = repo_dir / "output"
    
    rs_stocks_file = output_dir / "rs_stocks.csv"
    if not rs_stocks_file.exists():
        print(f"Error: {rs_stocks_file} does not exist.")
        sys.exit(1)

    print(f"Reading {rs_stocks_file}...")
    df = pd.read_csv(rs_stocks_file)
    print(f"Loaded {len(df):,} stocks.")

    tickers = df['Ticker'].tolist()
    start_time = time.time()
    vol_map = fetch_all_21_daily_volumes(tickers)
    elapsed = time.time() - start_time
    print(f"\nCompleted daily volume download in {elapsed:.2f} seconds.")

    # 21 daily volume columns: Vol_D1 (most recent day) to Vol_D21 (21st day ago)
    d1_cols = [f'Vol_D{k}' for k in range(1, 22)]
    
    vol_matrix = []
    constant_count = 0

    for idx, row in df.iterrows():
        t = str(row['Ticker']).strip()
        vols = vol_map.get(t, vol_map.get(t.replace('.', '-'), None))
        if vols is None:
            constant_count += 1
            fallback_vol = 0
            if 'AvgVol30' in row and pd.notna(row['AvgVol30']) and row['AvgVol30'] > 0:
                fallback_vol = int(row['AvgVol30'])
            elif 'AvgVol10' in row and pd.notna(row['AvgVol10']) and row['AvgVol10'] > 0:
                fallback_vol = int(row['AvgVol10'])
            vols = [fallback_vol] * 21
        elif len(vols) < 21:
            vols = vols + [vols[-1] if len(vols)>0 else 0] * (21 - len(vols))
            
        vol_matrix.append(vols[:21])

    vol_df = pd.DataFrame(vol_matrix, columns=d1_cols, index=df.index)
    
    df['Volume'] = vol_df['Vol_D1']
    for col in d1_cols:
        df[col] = vol_df[col]

    # Reorder columns to place Volume and Vol_D1..Vol_D21 right after Price
    cols = [c for c in df.columns if c not in (['Volume'] + d1_cols)]
    insert_idx = cols.index('Price') + 1 if 'Price' in cols else len(cols)
    new_cols = cols[:insert_idx] + ['Volume'] + d1_cols + cols[insert_idx:]
    df = df[new_cols]

    # Verify how many tickers have unique non-constant daily volumes
    is_constant = (vol_df.nunique(axis=1) <= 1)
    real_count = len(df) - is_constant.sum()
    print(f"\n--- DAILY VOLUME METRICS ANALYSIS ---")
    print(f"Total stocks: {len(df):,}")
    print(f"Stocks with UNIQUE REAL 21-day daily volume history: {real_count:,} ({real_count/len(df)*100:.1f}%)")
    print(f"Stocks with constant/fallback volume: {is_constant.sum():,} ({is_constant.sum()/len(df)*100:.1f}%)")

    print("\nSample Output (Top 5 Stocks with Daily Volume Columns):")
    display_cols = ['Rank', 'Ticker', 'Price', 'Volume', 'Vol_D1', 'Vol_D2', 'Vol_D3', 'Vol_D21']
    print(df[display_cols].head(5).to_string(index=False))

    # Save to main rs_stocks.csv
    df.to_csv(rs_stocks_file, index=False)
    print(f"\nSaved updated {rs_stocks_file}")

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

if __name__ == '__main__':
    main()
