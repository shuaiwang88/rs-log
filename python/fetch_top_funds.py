#!/usr/bin/env python3
"""
fetch_top_funds.py

Fetches and formats top mutual funds & institutional holders for any stock ticker
in the MarketSurge style layout.

Usage:
    python python/fetch_top_funds.py NVDA
    python python/fetch_top_funds.py META --top 8
"""

import sys
import argparse
from pathlib import Path
import pandas as pd
import numpy as np
import yfinance as yf

def format_shares(val):
    if pd.isna(val):
        return 'N/A'
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    elif val >= 1_000:
        return f"{val / 1_000:.2f}K"
    return str(val)

def fetch_and_display_top_funds(symbol: str, top_n: int = 5, save_history: bool = True):
    symbol = symbol.upper().strip()
    print(f"Fetching Top Funds & Institutional Holders for {symbol} via yfinance...")

    tk = yf.Ticker(symbol.replace('.', '-'))
    mf = tk.mutualfund_holders

    if mf is None or mf.empty:
        print(f"Notice: No mutual fund holders data found for {symbol}. Trying institutional holders...")
        mf = tk.institutional_holders

    if mf is None or mf.empty:
        print(f"Error: No fund or institutional ownership data found for {symbol}.")
        return

    # Sort by percentage held if available
    if 'pctHeld' in mf.columns:
        mf = mf.sort_values(by='pctHeld', ascending=False)

    print(f"\n==================================================")
    print(f"   TOP {top_n} FUNDS / INSTITUTIONAL HOLDERS FOR {symbol}")
    print(f"==================================================\n")

    top_funds = mf.head(top_n)

    for idx, row in top_funds.iterrows():
        holder_name = str(row['Holder']).strip()
        pct_held = f"{row['pctHeld'] * 100:.2f}%" if ('pctHeld' in row and pd.notna(row['pctHeld'])) else "N/A"
        
        date_rep = row.get('Date Reported', None)
        date_str = pd.to_datetime(date_rep).strftime('%b-%y') if pd.notna(date_rep) else "N/A"
        
        shares = row.get('Shares', np.nan)
        shares_str = format_shares(shares)

        print(f"{holder_name}")
        print(f"{pct_held}")
        print(f"{date_str}")
        print(f"{shares_str}\n")

    # Optionally persist to local history file to track quarters side-by-side
    if save_history:
        repo_dir = Path(__file__).resolve().parent.parent
        output_dir = repo_dir / "output"
        history_file = output_dir / "fund_holdings_history.csv"

        df_save = top_funds.copy()
        df_save['Ticker'] = symbol
        df_save['FetchedAt'] = pd.Timestamp.now().strftime('%Y-%m-%d')

        if history_file.exists():
            old_df = pd.read_csv(history_file)
            combined = pd.concat([old_df, df_save], ignore_index=True).drop_duplicates(
                subset=['Ticker', 'Holder', 'Date Reported']
            )
            combined.to_csv(history_file, index=False)
        else:
            df_save.to_csv(history_file, index=False)

        print(f"✓ Updated local fund history database at {history_file}")

def main():
    parser = argparse.ArgumentParser(description="Fetch and format top fund holders for a stock.")
    parser.add_argument("ticker", type=str, help="Stock ticker symbol (e.g. NVDA, META, AAPL)")
    parser.add_argument("--top", type=int, default=5, help="Number of top funds to display (default: 5)")
    args = parser.parse_args()

    fetch_and_display_top_funds(args.ticker, top_n=args.top)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        fetch_and_display_top_funds("META", top_n=5)
    else:
        main()
