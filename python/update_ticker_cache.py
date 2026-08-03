#!/usr/bin/env python3
"""
update_ticker_cache.py

Automates updating and maintaining max daily OHLCV historical parquet files in `ticker_cache/`.
- For new tickers: Fetches full max daily historical data via yfinance.
- For existing tickers: Fetches latest bars (period='5d'), merges incrementally, and updates parquets.
- Maintains `<TICKER>_1d.parquet` (full history).
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
# ETFs the IBD Live tab references that aren't core benchmarks but should be kept current too.
WATCHLIST_ETFS = ['VOO', 'XLF', 'TBT', 'XBI', 'RSP', 'MUZ', 'JMKE']
# Sector & industry ETFs (GICS Select Sector SPDRs + key industry/thematic funds the show follows).
SECTOR_ETFS = [
    'XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP', 'XLRE', 'XLU', 'XLV', 'XLY',  # Select Sector SPDRs
    'SMH', 'SOXX', 'IGV',            # semiconductors / software
    'XBI', 'IBB', 'IHI',            # biotech / health care
    'KRE',                          # regional banks
    'XOP', 'OIH',                   # energy producers / oil services
    'ITA', 'XHB', 'IYT',            # aerospace-defense / homebuilders / transports
    'XRT',                          # retail
    'URA', 'TAN',                   # uranium / clean energy
    'VNQ',                          # REITs
    'KWEB',                         # China internet
    'GLD', 'SLV', 'GDX', 'XME', 'EWZ',  # commodities / metals / emerging markets
]

SPLIT_FACTORS = (2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 50, 100)

# Ledger of split repairs already attempted, so a ticker is never re-downloaded for the same
# discontinuity twice. Without it the history scan below would refetch the same ~360 damaged
# tickers on EVERY run, and for splits old enough that Yahoo does not adjust them either the
# retry would never terminate. Recording the outcome turns "unfixable" into a fact we keep.
REPAIR_LEDGER = CACHE_DIR / ".split_repairs.json"
_LEDGER = {}
SCAN_WINDOW = 1500          # the scanner only ever reads this many bars; older gaps are inert


def _load_ledger():
    try:
        import json
        with open(REPAIR_LEDGER) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ledger(led):
    try:
        import json
        with open(REPAIR_LEDGER, "w") as f:
            json.dump(led, f, indent=1, sort_keys=True)
    except Exception:
        pass


def _history_split_gaps(df, window=SCAN_WINDOW):
    """Split-like discontinuities anywhere in the window the pattern scanner actually reads.

    The seam check catches a split happening right now. This catches damage already sitting in
    the file from before the check existed - 360 tickers at the time it was written. Scanning
    only the last `window` bars is deliberate: the scanner trims to 1500, so an unadjusted
    1998 split is real but inert, and refetching for it would be pure cost.
    """
    try:
        from detect_split_gaps import scan_one
    except Exception:
        return []
    try:
        d = df.iloc[-window:] if len(df) > window else df
        return scan_one(d)
    except Exception:
        return []


def _looks_like_split(existing_df, new_df, min_price=1.0, tol=0.08, vol_confirm=2.0):
    """True when the seam between cached and freshly fetched rows looks like a split.

    Conservative on purpose, because the consequence of a false positive is a needless
    download while a false negative silently corrupts the file. Requires all of:
      - an overnight gap (new OPEN vs last cached CLOSE) near a clean split factor,
      - the new session's CLOSE on the same new scale, not just the open,
      - volume that does NOT confirm - a genuine 10x move trades enormous size, a split does
        not (BANL traded 0.58x its average the day it "rose" tenfold),
      - a real price on at least one side, since sub-penny quotes are rounded to four
        decimals and 0.0001 -> 0.0100 is "exactly" 1-for-100 by arithmetic alone.
    """
    try:
        if existing_df is None or new_df is None or existing_df.empty or new_df.empty:
            return False
        prior = existing_df[existing_df.index < new_df.index.min()]
        if prior.empty:
            return False
        pc = float(prior["Close"].iloc[-1])
        no = float(new_df["Open"].iloc[0])
        nc = float(new_df["Close"].iloc[0])
        if not all(np.isfinite(x) and x > 0 for x in (pc, no, nc)):
            return False
        if max(pc, nc) < min_price:
            return False
        ratio = no / pc
        best = min(((abs(ratio - v) / v, v)
                    for f in SPLIT_FACTORS for v in (float(f), 1.0 / f)),
                   key=lambda t: t[0])
        if best[0] > tol:
            return False
        if abs(nc / pc - best[1]) / best[1] > tol * 4:
            return False
        vol = new_df["Volume"].iloc[0]
        avg = prior["Volume"].tail(20).mean()
        if np.isfinite(vol) and np.isfinite(avg) and avg > 0 and (vol / avg) >= vol_confirm:
            return False       # volume confirms it -> a real move, leave the history alone
        return True
    except Exception:
        return False


def get_target_tickers():
    tickers = set(BENCHMARKS + WATCHLIST_ETFS + SECTOR_ETFS)

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

    # Add tickers from rs_stocks_historical.csv if present
    rs_hist = REPO_DIR / "output" / "rs_stocks_historical.csv"
    if rs_hist.exists():
        try:
            df_h = pd.read_csv(rs_hist, usecols=['Ticker'])
            for t in df_h['Ticker'].dropna():
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

def update_ticker_cache_batch(tickers=None, batch_size=100, delay_between_batches=0.4, target_min_date="2024-06-04"):
    if tickers is None:
        tickers = get_target_tickers()

    if not tickers:
        print("No tickers found to update.")
        return

    print(f"🔄 Starting ticker_cache update for {len(tickers):,} tickers (Target earliest date: {target_min_date})...")
    start_time = time.time()

    # Determine which tickers need full initial fetch vs incremental 5d update
    need_full = []
    need_incremental = []

    for t in tickers:
        clean = t.replace(".", "-")
        p_1d = CACHE_DIR / f"{clean}_1d.parquet"
        if not p_1d.exists():
            need_full.append(t)
        else:
            try:
                existing_df = pd.read_parquet(p_1d)
                if existing_df.empty:
                    need_full.append(t)
                else:
                    min_d = str(existing_df.index.min())[:10]
                    if min_d > target_min_date:
                        need_full.append(t)
                    else:
                        need_incremental.append(t)
            except Exception:
                need_full.append(t)

    print(f"  • {len(need_incremental):,} tickers with complete cache (incremental 5d update)")
    print(f"  • {len(need_full):,} tickers missing or lacking pre-{target_min_date} history (full max backfill)")

    def process_download_data(t_list, period_str):
        success_count = 0
        total_len = len(t_list)
        # Ledger is read once per pass and written once at the end, so a crash mid-run costs
        # at most one repeated repair rather than corrupting the record.
        global _LEDGER
        _LEDGER = _load_ledger()
        _pending_ledger = {}
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
                    time.sleep(0.5)
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

                                # A split makes this merge silently wrong. We only ever fetch
                                # the last few days, so rows older than that are never
                                # revisited: Yahoo back-adjusts ITS history on a split, but
                                # ours keeps the pre-split prices forever and the file ends up
                                # with a step change nothing downstream knows about. BANL
                                # went $0.36 -> $3.82 overnight on LOWER volume (963K then
                                # 113K) - a 1-for-10 reverse on 2026-07-20 - and the pattern
                                # scanner read it as a +1908% flagpole into a textbook flag.
                                # Any pattern spanning the date is affected, not just HTF: a
                                # cup would show a fabricated depth, a base top would sit at
                                # a price that never traded.
                                #
                                # So check the seam and, if it looks like a split, refetch the
                                # whole history instead of patching it. Refetching is
                                # authoritative - Yahoo has already done the adjustment - and
                                # needs no split factor or ratio arithmetic on our side, which
                                # is the part that would be dangerous to get wrong.
                                # Two ways a split shows up. The SEAM check catches one
                                # happening right now, between the cached rows and today's
                                # fetch. The HISTORY scan catches damage already in the file
                                # from before this existed - 360 tickers when it was written.
                                # Both want the same repair, so decide once.
                                reason = None
                                if _looks_like_split(existing_df, df_t):
                                    reason = "merge seam"
                                else:
                                    gaps = _history_split_gaps(combined)
                                    if gaps:
                                        # Skip if we already tried and Yahoo's own history
                                        # still carries the gap - retrying downloads forever
                                        # and never converges.
                                        seen = _LEDGER.get(clean_t, {})
                                        if seen.get("date") != gaps[0]["date"]:
                                            reason = f"history {gaps[0]['date']} " \
                                                     f"x{gaps[0]['ratio']:.2f}"
                                            _pending_ledger[clean_t] = {
                                                "date": gaps[0]["date"],
                                                "ratio": gaps[0]["ratio"],
                                                "kind": gaps[0]["kind"]}
                                if reason:
                                    print(f"  ! {clean_t}: price discontinuity ({reason}) "
                                          f"- refetching full history")
                                    try:
                                        full = yf.download(clean_t, period="max",
                                                           interval="1d", auto_adjust=False,
                                                           progress=False)
                                        if full is not None and not full.empty:
                                            if isinstance(full.columns, pd.MultiIndex):
                                                full.columns = full.columns.get_level_values(0)
                                            keep = [c for c in req_cols if c in full.columns]
                                            if keep:
                                                refetched = full[keep].dropna(how="all")
                                                # Only accept the refetch if it actually
                                                # resolves the discontinuity. Yahoo does not
                                                # adjust every old split either, and swapping
                                                # in an equally broken frame that also drops
                                                # cached history would be a net loss.
                                                if not _history_split_gaps(refetched):
                                                    combined = refetched
                                                    if clean_t in _pending_ledger:
                                                        _pending_ledger[clean_t]["resolved"] = True
                                                elif clean_t in _pending_ledger:
                                                    _pending_ledger[clean_t]["resolved"] = False
                                    except Exception:
                                        pass   # keep the merged frame; the scan will flag it
                                df_t = combined
                            except Exception:
                                pass

                        # Save full history as <TICKER>_1d.parquet
                        df_t.to_parquet(p_1d)

                        success_count += 1

                    except Exception:
                        pass
            except Exception as e:
                print(f"  Notice during batch download ({i}/{total_len}): {e}")
                time.sleep(1.0)

            time.sleep(delay_between_batches)
            processed_num = min(i + batch_size, total_len)
            if (i // batch_size + 1) % 5 == 0 or processed_num == total_len:
                print(f"    Batch {i // batch_size + 1}: Processed {processed_num:,} / {total_len:,} tickers ({success_count:,} saved successfully)...")
        if _pending_ledger:
            _LEDGER.update(_pending_ledger)
            _save_ledger(_LEDGER)
            n_fixed = sum(1 for v in _pending_ledger.values() if v.get("resolved"))
            print(f"  split repairs: {n_fixed} resolved, "
                  f"{len(_pending_ledger) - n_fixed} still discontinuous after refetch "
                  f"(recorded, will not retry)")
        return success_count

    # 1. Process incremental updates for existing files that already cover target_min_date
    if need_incremental:
        print("  Updating existing complete ticker cache files...")
        inc_ok = process_download_data(need_incremental, period_str="5d")
        print(f"  ✓ Updated {inc_ok:,} / {len(need_incremental):,} existing ticker parquets.")

    # 2. Process full max downloads for new/incomplete tickers
    if need_full:
        print("  Fetching full history for missing/incomplete tickers (max period)...")
        full_ok = process_download_data(need_full, period_str="max")
        print(f"  ✓ Created/Backfilled {full_ok:,} / {len(need_full):,} ticker parquets.")

    elapsed = time.time() - start_time
    print(f"✅ Ticker cache update finished in {elapsed:.2f} seconds.")

if __name__ == "__main__":
    update_ticker_cache_batch()

