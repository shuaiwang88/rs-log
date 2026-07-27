#!/usr/bin/env python3
"""
update_volume_column.py

Automates adding the single 'Volume' column (placed right after 'Price') to output/rs_stocks.csv,
output/rs_stocks_1.csv, and output/rs_stocks_2.csv whenever new commits are fetched from upstream.

Queries yfinance for ONLY the same-day trading volume (period='5d', interval='1d', taking latest bar).
"""

import sys
import os
import time
from pathlib import Path
import pandas as pd
import numpy as np
import yfinance as yf

def add_volume_to_rs_stocks(repo_dir: Path = None) -> bool:
    if repo_dir is None:
        repo_dir = Path(__file__).resolve().parent.parent

    output_dir = repo_dir / "output"
    rs_stocks_file = output_dir / "rs_stocks.csv"

    if not rs_stocks_file.exists():
        print(f"Error: {rs_stocks_file} does not exist.")
        return False

    print(f"🔄 Processing {rs_stocks_file} to add same-day 'Volume' column...")
    df = pd.read_csv(rs_stocks_file)
    tickers = df['Ticker'].dropna().tolist()

    # Remove old Vol_D1..Vol_D21 columns if present
    d1_cols = [f'Vol_D{k}' for k in range(1, 22)]
    for c in d1_cols:
        if c in df.columns:
            df.drop(columns=[c], inplace=True)

    # Fetch ONLY same-day volume via yfinance (period='5d' gets latest trading day)
    clean_tickers = [str(t).strip().replace('.', '-') for t in tickers]
    vol_map = {}
    batch_size = 400
    total = len(clean_tickers)

    print(f"Fetching same-day volume via yfinance for {total:,} tickers...")
    for i in range(0, total, batch_size):
        batch = clean_tickers[i:i + batch_size]
        try:
            data = yf.download(batch, period='5d', interval='1d', progress=False, group_by='ticker', threads=True)
            if isinstance(data.columns, pd.MultiIndex):
                for t in batch:
                    try:
                        if t in data.columns.levels[0]:
                            vs = data[t]['Volume'].dropna()
                            if len(vs) > 0:
                                latest_v = int(vs.iloc[-1])
                                vol_map[t] = latest_v
                                vol_map[t.replace('-', '.')] = latest_v
                    except Exception:
                        pass
            elif 'Volume' in data.columns:
                vs = data['Volume'].dropna()
                if len(vs) > 0:
                    vol_map[batch[0]] = int(vs.iloc[-1])
        except Exception as e:
            print(f"Notice during same-day volume fetch (batch {i}): {e}")
        time.sleep(0.1)

    print(f"Retrieved same-day volume data for {len(vol_map):,} / {total:,} tickers.")

    # Assign Volume column
    def get_same_day_vol(row):
        t = str(row['Ticker']).strip()
        if t in vol_map:
            return vol_map[t]
        t_dash = t.replace('.', '-')
        if t_dash in vol_map:
            return vol_map[t_dash]
        if 'Volume' in row and pd.notna(row['Volume']) and row['Volume'] > 0:
            return int(row['Volume'])
        if 'AvgVol30' in row and pd.notna(row['AvgVol30']) and row['AvgVol30'] > 0:
            return int(row['AvgVol30'])
        return 0

    df['Volume'] = df.apply(get_same_day_vol, axis=1)

    # Position Volume column right after Price
    cols = [c for c in df.columns if c != 'Volume']
    insert_idx = cols.index('Price') + 1 if 'Price' in cols else len(cols)
    final_cols = cols[:insert_idx] + ['Volume'] + cols[insert_idx:]
    df = df[final_cols]

    # Save main rs_stocks.csv
    df.to_csv(rs_stocks_file, index=False)
    print(f"✅ Updated {rs_stocks_file} ({len(df):,} rows, {len(df.columns)} columns)")

    # Split into rs_stocks_1.csv and rs_stocks_2.csv
    n = len(df)
    mid = n // 2
    df1 = df.iloc[:mid]
    df2 = df.iloc[mid:]

    df1_path = output_dir / "rs_stocks_1.csv"
    df2_path = output_dir / "rs_stocks_2.csv"

    df1.to_csv(df1_path, index=False)
    df2.to_csv(df2_path, index=False)
    print(f"✅ Updated {df1_path} ({len(df1):,} rows)")
    print(f"✅ Updated {df2_path} ({len(df2):,} rows)")

    return True

if __name__ == '__main__':
    add_volume_to_rs_stocks()
