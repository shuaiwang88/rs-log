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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yf_ratelimit as yfrl

REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_DIR / "ticker_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── universe filters (user's call, 2026-08-03) ───────────────────────────────────────────
# Only liquid, non-penny names are worth carrying: the cache had grown to 7,132 tickers, of
# which 4,721 were under $10 or under 300K average volume - two thirds of the files for names
# that were never going to be traded. These thresholds are applied when choosing what to fetch
# AND by prune_cache() below, so the cache cannot silently refill with them.
MIN_PRICE = 10.0
MIN_AVG_VOL50 = 300_000

BENCHMARKS = ['SPY', 'QQQ', 'IWM', 'DIA', 'VTI']
# The main sector and industry ETFs only - the long tail of thematic, country and commodity
# funds was dropped on 2026-08-03. Every symbol kept here clears the filters above on its own,
# so this list is a statement of intent rather than an exemption. `SPY` is not optional: both
# pattern scanners use it as the RS comparative symbol.
SECTOR_ETFS = [
    # GICS Select Sector SPDRs - the canonical sector set
    'XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP', 'XLRE', 'XLU', 'XLV', 'XLY',
    # The most heavily traded industry funds, one per theme (SMH over SOXX, XBI over IBB)
    'SMH',      # semiconductors     12.3M avg vol
    'IGV',      # software           17.3M
    'XBI',      # biotech             9.1M
    'KRE',      # regional banks     14.4M
    'GDX',      # gold miners        21.8M
    'XRT',      # retail              5.2M
    'XOP',      # oil & gas E&P       3.6M
    'XHB',      # homebuilders        2.8M
]
WATCHLIST_ETFS = []          # folded into the two lists above

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


ALWAYS_KEEP = set(BENCHMARKS + WATCHLIST_ETFS + SECTOR_ETFS)


def cache_price_volume(ticker):
    """Last close and 50-day average volume from the cached parquet, or (None, None)."""
    fp = CACHE_DIR / f"{str(ticker).strip().replace('.', '-')}_1d.parquet"
    if not fp.exists():
        return None, None
    try:
        d = pd.read_parquet(fp, columns=['Close', 'Volume']).sort_index()
    except Exception:
        return None, None
    if d.empty:
        return None, None
    c = pd.to_numeric(d['Close'], errors='coerce').dropna()
    v = pd.to_numeric(d['Volume'], errors='coerce')
    # Last VALID close, not the last row's: a feed that leaves today's close NaN while still
    # reporting volume would otherwise make the ticker look unmeasurable and exempt it.
    close = float(c.iloc[-1]) if len(c) else None
    av = v.rolling(50, min_periods=25).mean().iloc[-1] if len(v) else np.nan
    return close, (float(av) if pd.notna(av) else None)


def _csv_price_volume():
    """Close / AvgVol50 per ticker from the RS csvs - what a NEW ticker is judged on.

    A ticker with no cached file yet cannot be measured from the cache, and fetching it just to
    find out it is a $3 shell is the cost this is avoiding. Both csvs carry Close and AvgVol50
    already, so the decision is made before any network call.
    """
    out = {}
    for name in ("rs_stocks.csv", "rs_stocks_historical.csv"):
        fp = REPO_DIR / "output" / name
        if not fp.exists():
            continue
        try:
            cols = ['Ticker', 'Close', 'AvgVol50']
            d = pd.read_csv(fp, usecols=lambda c: c in cols)
            if 'date' not in d.columns and name.endswith("historical.csv"):
                pass
            d = d.dropna(subset=['Ticker'])
            for t, c, v in zip(d['Ticker'], d.get('Close', pd.Series(dtype=float)),
                               d.get('AvgVol50', pd.Series(dtype=float))):
                t = str(t).strip()
                if t:                       # later files win; historical is read second
                    out[t] = (c, v)
        except Exception:
            continue
    return out


def passes_universe_filter(ticker, csv_pv=None):
    """True if `ticker` is worth carrying: at least MIN_PRICE and MIN_AVG_VOL50.

    Judged on the cached parquet when there is one, otherwise on the RS csvs. A ticker we know
    nothing about is kept - the filter removes what is measurably too small, and never guesses.
    """
    if str(ticker).strip() in ALWAYS_KEEP:
        return True
    close, vol = cache_price_volume(ticker)
    # Each field falls back on its own. Filling both from the csv whenever the close happened
    # to be missing threw away a volume the cache knew perfectly well, and let GALDY through
    # at 118K on a day its close came back NaN.
    if (close is None or vol is None) and csv_pv is not None:
        c_csv, v_csv = csv_pv.get(str(ticker).strip(), (None, None))
        if close is None:
            close = c_csv
        if vol is None:
            vol = v_csv
    if close is not None and pd.notna(close) and float(close) < MIN_PRICE:
        return False
    if vol is not None and pd.notna(vol) and float(vol) < MIN_AVG_VOL50:
        return False
    return True


def prune_cache(dry_run=False, verbose=True):
    """Delete cached parquets for tickers that no longer clear the universe filters.

    Run at the end of every update so a name that drifts under $10 or dries up on volume leaves
    the cache on its own, instead of the cache only ever growing. Weekly files follow their
    daily counterpart so the two never disagree about which tickers exist.
    """
    csv_pv = _csv_price_volume()
    tickers = sorted({f.stem.split("_")[0] for f in CACHE_DIR.glob("*.parquet") if "_" in f.stem})
    doomed = [t for t in tickers if not passes_universe_filter(t, csv_pv)]
    freed = 0
    for t in doomed:
        for fp in CACHE_DIR.glob(f"{t}_*.parquet"):
            freed += fp.stat().st_size
            if not dry_run:
                fp.unlink()
    if verbose:
        verb = "would remove" if dry_run else "removed"
        print(f"🧹 prune: {verb} {len(doomed):,} of {len(tickers):,} tickers "
              f"(<${MIN_PRICE:g} or <{MIN_AVG_VOL50:,} avg vol), {freed/1e6:.0f} MB")
    return doomed


def get_target_tickers():
    tickers = set(ALWAYS_KEEP)
    csv_pv = _csv_price_volume()

    def add_from(fp):
        if not fp.exists():
            return
        try:
            df = pd.read_csv(fp, usecols=['Ticker'])
            for t in df['Ticker'].dropna():
                clean = str(t).strip()
                if clean and passes_universe_filter(clean, csv_pv):
                    tickers.add(clean)
        except Exception:
            pass

    add_from(REPO_DIR / "output" / "rs_stocks.csv")
    add_from(REPO_DIR / "output" / "rs_stocks_historical.csv")

    # Existing tickers in ticker_cache/ - still filtered, so a name that has fallen under the
    # thresholds stops being refreshed even before prune_cache() deletes it.
    for f in CACHE_DIR.glob("*.parquet"):
        stem = f.stem
        if "_" in stem:
            t = stem.split("_")[0].strip()
            if t and passes_universe_filter(t, csv_pv):
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
                # Rate limiting used to land in the bare `except Exception` below, which made
                # a 429'd batch look exactly like a batch of delisted symbols: 100 tickers
                # silently skipped and the run still reported success. Back off and retry
                # instead, and record anything still unfetchable so the summary says so.
                data = yfrl.download(
                    tickers=clean_batch,
                    period=period_str,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                    label=f"batch {i}-{i + len(batch)}",
                )
                if data is None or data.empty:
                    if data is None:
                        yfrl.note_dropped(f"batch {i}-{i + len(batch)}",
                                          f"{len(clean_batch)} tickers unfetched")
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
                                        full = yfrl.download(clean_t, period="max",
                                                             interval="1d", auto_adjust=False,
                                                             progress=False,
                                                             label=f"{clean_t} split refetch")
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

def update_fundamentals_cache(tickers, max_age_days=7, delay=1.0):
    """Fetch and cache EPS/ROE fundamentals from yfinance for the given tickers.

    Called after the OHLCV update so fresh price data and fresh fundamentals land
    together. Uses fetch_fundamentals.batch_fetch_fundamentals for the actual work;
    each ticker result is cached to `fundamentals_cache/<TICKER>.json` independently.

    Benchmarks and sector ETFs (ALWAYS_KEEP) are skipped — yfinance has no meaningful
    EPS/ROE for them.
    """
    try:
        from fetch_fundamentals import batch_fetch_fundamentals
    except ImportError:
        print("⚠ fundamentals: fetch_fundamentals module not found — skipping.")
        return

    if not tickers:
        return

    # Skip benchmarks and sector ETFs — fundamentals data makes no sense for them
    tickers = [t for t in tickers if str(t).strip() not in ALWAYS_KEEP]
    if not tickers:
        print("⚠ fundamentals: no stock tickers to fetch (all are benchmarks/ETFs).")
        return

    print(f"📊 Fetching fundamentals (EPS/ROE) for {len(tickers):,} tickers "
          f"(cached if < {max_age_days}d old)...")
    start = time.time()
    results = batch_fetch_fundamentals(tickers, max_age_days=max_age_days,
                                        delay=delay, verbose=True)
    n_ok = sum(1 for v in results.values() if not v.get('error'))
    n_err = sum(1 for v in results.values() if v.get('error'))
    print(f"  fundamentals: {n_ok:,} ok, {n_err} errors "
          f"in {time.time() - start:.1f}s")
    yfrl.report()

if __name__ == "__main__":
    # --prune / --prune-dry-run maintain the universe without refetching anything.
    if "--prune-dry-run" in sys.argv:
        prune_cache(dry_run=True)
    elif "--prune" in sys.argv:
        prune_cache()
    elif "--fundamentals-only" in sys.argv:
        # Refresh only the EPS/ROE fundamentals cache; skip the OHLCV price pass
        # (useful as a fast daily refresh once prices are up to date).
        tickers = get_target_tickers()
        update_fundamentals_cache(tickers=tickers)
    else:
        with_fundamentals = "--with-fundamentals" in sys.argv
        tickers = get_target_tickers()
        update_ticker_cache_batch(tickers=tickers)
        # Names that drifted under the thresholds leave on the same run that refreshed them,
        # so the cache tracks the universe instead of accumulating everything ever fetched.
        prune_cache()
        if with_fundamentals:
            update_fundamentals_cache(tickers=tickers)

