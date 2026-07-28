#!/usr/bin/env python3
"""
update_ticker_cache.py

Automates updating and maintaining max daily OHLCV historical parquet files in `ticker_cache/`.
- For new tickers: Fetches full max daily historical data via yfinance.
- For existing tickers: Fetches latest bars (period='5d'), merges incrementally, and updates parquets.
- Maintains both `<TICKER>_1d.parquet` (full history) and `<TICKER>_250d.parquet` (last 250 bars).
"""

import sys
import time
from pathlib import Path
import pandas as pd
import numpy as np
import yfinance as yf

REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_DIR / "ticker_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BENCHMARKS = ['SPY', 'QQQ', 'IWM', 'DIA', 'VTI']

def get_target_tickers():
    tickers = set(BENCHMARKS)

    # Add tickers from rs_stocks.csv if present
    rs_file = REPO_DIR / "output" / "rs_stocks.csv"
    if rs_file.exists():
        try:
            df = pd.read_csv(rs_file, usecols=['Ticker'])
            for t in df['Ticker'].dropna():
                clean = str(t).strip()
                if clean:
                    tickers.add(clean)
        except Exception:
            pass

    # Add any existing tickers in ticker_cache/
    for f in CACHE_DIR.glob("*.parquet"):
        stem = f.stem
        if "_" in stem:
            t = stem.split("_")[0].strip()
            if t:
                tickers.add(t)

    return sorted(tickers)

def update_ticker_cache_batch(tickers=None, batch_size=60, delay_between_batches=0.6):
    if tickers is None:
        tickers = get_target_tickers()

    if not tickers:
        print("No tickers found to update.")
        return

    print(f"🔄 Starting rate-limit safe ticker_cache update for {len(tickers):,} tickers...")
    start_time = time.time()

    # Determine which tickers need full initial fetch vs incremental 5d update
    need_full = []
    need_incremental = []

    for t in tickers:
        clean = t.replace(".", "-")
        p_1d = CACHE_DIR / f"{clean}_1d.parquet"
        p_250d = CACHE_DIR / f"{clean}_250d.parquet"
        if not p_1d.exists() and not p_250d.exists():
            need_full.append(t)
        else:
            need_incremental.append(t)

    print(f"  • {len(need_incremental):,} tickers with existing cache (incremental 5d update)")
    print(f"  • {len(need_full):,} tickers missing cache (full max history download)")

    def process_download_data(t_list, period_str):
        success_count = 0
        total_len = len(t_list)
        for i in range(0, total_len, batch_size):
            batch = t_list[i:i + batch_size]
            clean_batch = [str(t).strip().replace(".", "-") for t in batch]
            try:
                data = yf.download(
                    tickers=clean_batch,
                    period=period_str,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=False,
                    progress=False,
                    threads=True
                )
                if data is None or data.empty:
                    time.sleep(1.0)
                    continue

                is_multi = isinstance(data.columns, pd.MultiIndex)

                for raw_t, clean_t in zip(batch, clean_batch):
                    try:
                        if is_multi:
                            if clean_t in data.columns.levels[0]:
                                df_t = data[clean_t].dropna(how="all").copy()
                            else:
                                continue
                        else:
                            df_t = data.dropna(how="all").copy()

                        if df_t.empty or "Close" not in df_t.columns:
                            continue

                        # Clean index timezone and formatting
                        df_t.index = pd.to_datetime(df_t.index)
                        if df_t.index.tz is not None:
                            df_t.index = df_t.index.tz_localize(None)
                        df_t.index.name = "Date"

                        # Ensure standard OHLCV column names capitalized
                        col_map = {c: c.capitalize() for c in df_t.columns if str(c).lower() in ["open", "high", "low", "close", "volume"]}
                        df_t = df_t.rename(columns=col_map)

                        req_cols = ["Open", "High", "Low", "Close", "Volume"]
                        available = [c for c in req_cols if c in df_t.columns]
                        if not available:
                            continue
                        df_t = df_t[available]

                        # Check existing 1d parquet
                        p_1d = CACHE_DIR / f"{clean_t}_1d.parquet"
                        if p_1d.exists():
                            try:
                                existing_df = pd.read_parquet(p_1d)
                                existing_df.index = pd.to_datetime(existing_df.index)
                                if existing_df.index.tz is not None:
                                    existing_df.index = existing_df.index.tz_localize(None)

                                # Combine existing and new data
                                combined = pd.concat([existing_df, df_t])
                                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                                df_t = combined
                            except Exception:
                                pass

                        # Save full history as <TICKER>_1d.parquet
                        df_t.to_parquet(p_1d)

                        # Save last 250 rows as <TICKER>_250d.parquet
                        p_250d = CACHE_DIR / f"{clean_t}_250d.parquet"
                        df_250 = df_t.tail(250)
                        df_250.to_parquet(p_250d)

                        success_count += 1

                    except Exception:
                        pass
            except Exception as e:
                print(f"  Notice during batch download ({i}/{total_len}): {e}")
                time.sleep(2.0)  # Pause if rate limited or connection issue

            time.sleep(delay_between_batches)
        return success_count

    # 1. Process incremental updates for existing files
    if need_incremental:
        print("  Updating existing ticker cache files...")
        inc_ok = process_download_data(need_incremental, period_str="5d")
        print(f"  ✓ Updated {inc_ok:,} / {len(need_incremental):,} existing ticker parquets.")

    # 2. Process full max downloads for new tickers
    if need_full:
        print("  Fetching full history for new tickers...")
        full_ok = process_download_data(need_full, period_str="max")
        print(f"  ✓ Created {full_ok:,} / {len(need_full):,} new ticker parquets.")

    elapsed = time.time() - start_time
    print(f"✅ Ticker cache update finished in {elapsed:.2f} seconds.")

if __name__ == "__main__":
    update_ticker_cache_batch()
