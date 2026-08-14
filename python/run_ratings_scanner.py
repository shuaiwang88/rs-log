#!/usr/bin/env python3
"""
run_ratings_scanner.py

Standalone CLI for the IBD-Style Ratings Scanner (app.py's "📊 Ratings Scanner"
tab, Quick Scan mode): computes RS Rating, RS 3M/6M, A/D, % Off 52W High over
the whole ticker_cache and persists the result to output/ratings_scan.csv.

Chained after the price pass by update_ticker_cache.py --run-ratings-scan (the
app.py Price Cache button passes it), so the ratings snapshot always reflects
the freshly fetched bars. Quick Scan is deliberately OHLCV-only - it matches
the tab's default mode and needs no yfinance fundamentals fetch. The Daily
Screener (build_daily_screener.py) already covers the fundamentals-driven EPS /
SMR / Composite columns; this file is the price-side ratings view.

Usage:
  python python/run_ratings_scanner.py            # full universe, ~2-3 min
  python python/run_ratings_scanner.py --limit 50 # quick smoke test
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calc_ibd_ratings import (  # noqa: E402
    apply_rating_percentiles, calc_ad_raw_score, calc_pct_off_52w_high_snapshot,
    calc_rs_raw_score, calc_rs_sub_raw_score, spy_perf_windows,
)

REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_DIR / "ticker_cache"
OUTPUT_DIR = REPO_DIR / "output"
OUTPUT_CSV = OUTPUT_DIR / "ratings_scan.csv"
BENCHMARKS = ("SPY", "QQQ", "IWM", "DIA", "VTI")
MIN_BARS = 65


def scan_universe(limit=None):
    """Mirror of app.py's Ratings Scanner Quick Scan loop.

    Returns the post-percentile DataFrame (same renames the tab applies before
    showing the table), so the persisted CSV matches what the tab displays.
    """
    _spy_rs = pd.read_parquet(CACHE_DIR / "SPY_1d.parquet", columns=["Close"])
    _spy_rs.index = pd.to_datetime(_spy_rs.index)
    _spy_perf = spy_perf_windows(_spy_rs["Close"].astype(float).sort_index())

    ticker_files = sorted(CACHE_DIR.glob("*_1d.parquet"))
    if limit:
        ticker_files = ticker_files[:limit]
    total = len(ticker_files)

    results = []
    for i, fp in enumerate(ticker_files):
        ticker = fp.stem.replace("_1d", "")
        if ticker in BENCHMARKS:
            continue
        try:
            df = pd.read_parquet(fp)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            if df.empty or len(df) < MIN_BARS or "Close" not in df.columns:
                continue

            rs_final = calc_rs_raw_score(df["Close"], _spy_perf)
            rs3m_raw = calc_rs_sub_raw_score(df["Close"], 63)
            rs6m_raw = calc_rs_sub_raw_score(df["Close"], 126)
            ad_raw = calc_ad_raw_score(df)
            pct_off = calc_pct_off_52w_high_snapshot(df)
            latest = float(df["Close"].iloc[-1])

            results.append({
                "Ticker": ticker,
                "Current Price": round(latest, 2),
                "Market Cap (mil)": None,          # Quick Scan: no fundamentals fetch
                "% Off 52W High": round(pct_off, 2) if not np.isnan(pct_off) else None,
                "RS Rating": rs_final,
                "_rs3m_raw": rs3m_raw,
                "_rs6m_raw": rs6m_raw,
                "_ad_raw": ad_raw,
                "_smr_raw": None,
                "EPS Rating": None,
            })
        except Exception:
            pass

        if (i + 1) % 250 == 0 or i == total - 1:
            print(f"    scanned {i + 1:,}/{total:,} tickers...", flush=True)

    if not results:
        return pd.DataFrame()

    result_df = pd.DataFrame(results)
    # Universe post-pass: percentile-ranks raw scores against the eligible
    # (price >= $4, mktcap >= $50M when known) scanned universe.
    result_df = apply_rating_percentiles(result_df)
    return result_df.rename(columns={
        "Current Price": "Close",
        "RS 3-Month Rating": "RS 3M",
        "RS 6-Month Rating": "RS 6M",
        "SMR Rating": "SMR Grade",
    })


def main():
    limit = None
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        if i + 1 < len(sys.argv):
            try:
                limit = max(0, int(sys.argv[i + 1]))
            except ValueError:
                pass

    print(f"📊 Running IBD-Style Ratings Scanner over ticker_cache "
          f"({'limit ' + str(limit) if limit else 'full universe'})...")
    t0 = time.time()
    result_df = scan_universe(limit=limit)

    if result_df.empty:
        print("✗ No results - check that ticker_cache has data.")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result_df.to_csv(OUTPUT_CSV, index=False)
    n_scored = int(result_df["RS Rating"].notna().sum())
    print(f"  ✓ Scanned {len(result_df):,} tickers "
          f"({n_scored:,} with RS Rating) in {time.time() - t0:.1f}s")
    print(f"  saved {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
