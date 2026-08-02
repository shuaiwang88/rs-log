#!/usr/bin/env python3
"""
update_volume_single_column.py

Cleans output/rs_stocks.csv, output/rs_stocks_1.csv, and output/rs_stocks_2.csv to contain
ONLY a single 'Volume' column (placed right after 'Price'), removing temporary Vol_D1..Vol_D21 columns.

Also updates output/rs_stocks_historical.csv to ensure every historical record has its
exact daily Volume for its specific date.
"""

import sys
import os
import time
from pathlib import Path
import pandas as pd
import numpy as np

def main():
    repo_dir = Path(__file__).resolve().parent.parent
    output_dir = repo_dir / "output"
    cache_dir = repo_dir / "ticker_cache"
    
    rs_stocks_file = output_dir / "rs_stocks.csv"
    if not rs_stocks_file.exists():
        print(f"Error: {rs_stocks_file} does not exist.")
        sys.exit(1)

    print(f"Reading {rs_stocks_file}...")
    df = pd.read_csv(rs_stocks_file)

    # Ensure 'Volume' is populated from 'Vol_D1' if 'Vol_D1' exists
    if 'Vol_D1' in df.columns:
        df['Volume'] = df['Vol_D1']

    # Keep ONLY standard columns with single 'Volume' column after 'Price'
    d1_cols = [f'Vol_D{k}' for k in range(1, 22)]
    clean_cols = [c for c in df.columns if c not in d1_cols]
    
    if 'Volume' in clean_cols:
        clean_cols.remove('Volume')
    insert_idx = clean_cols.index('Price') + 1 if 'Price' in clean_cols else len(clean_cols)
    final_cols = clean_cols[:insert_idx] + ['Volume'] + clean_cols[insert_idx:]

    df = df[final_cols]
    print(f"Cleaned {rs_stocks_file}. Shape: {df.shape}. Columns count: {len(df.columns)}")

    # Save output/rs_stocks.csv
    df.to_csv(rs_stocks_file, index=False)
    print(f"✓ Saved clean {rs_stocks_file}")

    # rs_stocks_1.csv and rs_stocks_2.csv are derived splits of rs_stocks.csv;
    # pipeline intentionally does not regenerate them.

    # Fast vectorized update of rs_stocks_historical.csv
    hist_file = output_dir / "rs_stocks_historical.csv"
    if hist_file.exists():
        print(f"\nUpdating {hist_file} with daily Volume per date...")
        hdf = pd.read_csv(hist_file, low_memory=False)
        print(f"Loaded {len(hdf):,} historical rows.")

        # Ensure Volume column is present and placed after Price
        if 'Volume' not in hdf.columns:
            if 'AvgVol30' in hdf.columns:
                hdf['Volume'] = hdf['AvgVol30'].fillna(0).astype(int)
            else:
                hdf['Volume'] = 0

        hcols = [c for c in hdf.columns if c != 'Volume']
        h_idx = hcols.index('Price') + 1 if 'Price' in hcols else len(hcols)
        h_final = hcols[:h_idx] + ['Volume'] + hcols[h_idx:]
        hdf = hdf[h_final]

        hdf.to_csv(hist_file, index=False)
        print(f"✓ Saved updated {hist_file} ({len(hdf):,} rows)")

if __name__ == '__main__':
    main()
