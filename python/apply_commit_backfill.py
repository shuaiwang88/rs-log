#!/usr/bin/env python3
"""
apply_commit_backfill.py

Executable script invoked by git filter-branch / python git rewriting loop
to update output/rs_stocks.csv in place for every historical commit.
"""

import os
import sys
from pathlib import Path
import pandas as pd
import numpy as np

def clean_and_backfill():
    repo_dir = Path.cwd()
    rs_file = repo_dir / "output" / "rs_stocks.csv"
    ms_file = Path("/Users/vanstark/Desktop/stock/rs-log/IBD/marketsurge.csv")

    if not rs_file.exists():
        return

    try:
        df = pd.read_csv(rs_file, low_memory=False)
    except Exception:
        return

    target_schema = [
        'Rank', 'Ticker', 'Sector', 'Industry', 'Exchange', 'Relative Strength', 'Percentile',
        '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile', 'Close', 'Volume', 'MarketCap',
        'Float', 'ShortFloatPct', 'PctFrom52WkHigh', 'AvgVol10', 'AvgVol30', 'AvgVol50',
        'RevenueGrowth', 'Price vs 10-Day', 'Price vs 21-Day', 'Price vs 50-Day', 'Price vs 150-Day',
        'Price vs 200-Day', '10 Day > 21 Day > 50 Day', '50-Day > 150-Day > 200-Day',
        'Avg True Range', '21 Day ATR %', '30 Day ATR %', '50 Day ATR %', 'Up/Down Vol',
        'Daily Closing Range', 'Vol % Chg vs 50-Day', 'Number of Funds', 'Funds %',
        'Funds % Increase', 'Avg EPS % Chg 6Q', 'Avg EPS % Chg 4Q', 'EPS Surprise',
        'Avg Sales % Chg 6Q', 'Avg Sales % Chg 4Q', 'ROE', 'Pre-tax Margins', 'Forward P/E',
        'PEG', 'Price to Sales', 'Price to Book'
    ]

    has_all = all(c in df.columns for c in target_schema)
    has_funds = 'Number of Funds' in df.columns and df['Number of Funds'].notna().sum() > 500
    if has_all and has_funds:
        return

    df['Ticker_Clean'] = df['Ticker'].astype(str).str.strip()

    # Fundamentals & Funds merge from MarketSurge
    if ms_file.exists():
        try:
            ms_df = pd.read_csv(ms_file, low_memory=False)
            sym_col = None
            for c in ['Symbol', 'Ticker', 'ticker', 'symbol']:
                if c in ms_df.columns:
                    sym_col = c
                    break
            if sym_col:
                ms_df['Ticker_Clean'] = ms_df[sym_col].astype(str).str.strip()
                fund_cols = [
                    'Number of Funds', 'Funds %', 'Funds % Increase',
                    'Avg EPS % Chg 6Q', 'Avg EPS % Chg 4Q', 'EPS Surprise',
                    'Avg Sales % Chg 6Q', 'Avg Sales % Chg 4Q',
                    'ROE', 'Pre-tax Margins', 'Forward P/E', 'PEG', 'Price to Sales', 'Price to Book'
                ]
                avail_cols = [c for c in fund_cols if c in ms_df.columns]
                ms_sub = ms_df[['Ticker_Clean'] + avail_cols].drop_duplicates(subset=['Ticker_Clean'])
                
                for c in avail_cols:
                    if c in df.columns:
                        df.drop(columns=[c], inplace=True)
                        
                df = df.merge(ms_sub, on='Ticker_Clean', how='left')
        except Exception:
            pass

    for col in target_schema:
        if col not in df.columns:
            df[col] = np.nan

    if 'Ticker_Clean' in df.columns:
        df.drop(columns=['Ticker_Clean'], inplace=True)

    df_out = df[target_schema]
    df_out.to_csv(rs_file, index=False)

    # Also update split files if they exist in the repository
    df1_path = repo_dir / "output" / "rs_stocks_1.csv"
    df2_path = repo_dir / "output" / "rs_stocks_2.csv"
    if df1_path.exists() and df2_path.exists():
        mid = len(df_out) // 2
        df_out.iloc[:mid].to_csv(df1_path, index=False)
        df_out.iloc[mid:].to_csv(df2_path, index=False)

if __name__ == '__main__':
    clean_and_backfill()
