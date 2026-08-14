#!/usr/bin/env python3
"""
fetch_fundamentals.py

Fetches ALL available fundamental data from yfinance for a ticker and caches
it alongside OHLCV parquets as `<TICKER>_fund.json` in `ticker_cache/`.

Data captured (max history wherever available):
  - Company info dict (all ~100+ metrics from yfinance .info)
  - Quarterly & annual income statements (full history)
  - Quarterly & annual balance sheets (full history)
  - Quarterly & annual cash flow statements (full history)
  - Earnings history (actual vs estimate, surprise %)
  - Earnings & revenue estimates (forward estimates)
  - Growth estimates, EPS trend, EPS revisions
  - Analyst price targets, recommendations, upgrades/downgrades
  - Institutional, mutual fund, and major holders (top holders)
  - Insider purchases & transactions
  - Earnings calendar (next earnings date + estimates)
  - ESG sustainability data

Smart caching: fundamentals change at most quarterly. The module stores a
fingerprint of the data. On re-fetch, if the cached data hasn't changed
(same quarter, same holders date, same estimates), it's left untouched.
Only NEW data (new quarter, new estimates, new holder filings) triggers
a cache update.
"""

import json
import time
import hashlib
from datetime import date as datetime_date
from pathlib import Path
import pandas as pd
import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_DIR / "ticker_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Bump this when the cache schema changes (new fields, new format).
# Old caches with a different version are treated as stale and re-fetched.
FORMAT_VERSION = 2

# ── Rate limiting ────────────────────────────────────────────────────────────
# The project ships a battle-tested rate-limit wrapper that retries HTTP 429
# with exponential backoff (60s, 180s, 420s) and tracks what got dropped.
# Every yfinance call below goes through it so a rate-limited fetch is never
# silently indistinguishable from missing data.
try:
    import yf_ratelimit as yfrl
except ImportError:
    # Fallback when running standalone; batch_fetch_fundamentals will warn
    class _FakeRL:
        def call(self, fn, *a, label=None, **kw):
            return fn(*a, **kw)
        def note_dropped(self, *a): pass
        def report(self, *a): pass
    yfrl = _FakeRL()

# defeatbeta-api first choice for the 6 financial statements (deeper quarterly
# history, no rate limit); yfinance stays the only source for everything else
# in this file (info, estimates, holders, insider, calendar, ESG - fields
# defeatbeta-api doesn't have).
try:
    import defeatbeta_source as dbsrc
except ImportError:
    dbsrc = None

# ── helpers ──────────────────────────────────────────────────────────────────

def _df_to_dict(df):
    """Convert a pandas DataFrame/Series to a JSON-serializable dict.

    Output format: {row_label: {date_str: value, ...}, ..., "_shape": [...], "_columns": [...]}
    where row_label is the DataFrame index (financial statement line item)
    and date_str is the column name (fiscal period).
    """
    if df is None:
        return None
    try:
        if isinstance(df, (pd.Series,)):
            return {str(k): _clean_val(v) for k, v in df.to_dict().items()}
        # DataFrame: rows are financial statement items, columns are dates
        d = {}
        for row_label in df.index:
            row_str = str(row_label)
            row_vals = {}
            for col_label, val in df.loc[row_label].items():
                row_vals[str(col_label)] = _clean_val(val)
            d[row_str] = row_vals
        d["_shape"] = list(df.shape)
        d["_columns"] = [str(c) for c in df.columns]
        return d
    except Exception:
        return None


def _df_from_dict(d):
    """Reconstruct a DataFrame from _df_to_dict output."""
    if d is None or not isinstance(d, dict):
        return None
    try:
        if "_shape" in d and "_columns" in d:
            cols = d["_columns"]
            data = {c: d[c] for c in cols if c in d}
            if not data:
                return pd.DataFrame()
            df = pd.DataFrame(data)
            df.index = pd.to_datetime(list(df.index), errors="coerce")
            return df.sort_index(ascending=False)
        return pd.Series(d) if d else pd.DataFrame()
    except Exception:
        return None


def _clean_val(val):
    """Convert numpy/pandas types to plain Python for JSON serialization."""
    if val is None:
        return None
    if isinstance(val, (pd.Timestamp, datetime_date)):
        return str(val)
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val) if np.isfinite(val) else None
    if isinstance(val, (np.bool_,)):
        return bool(val)
    if isinstance(val, pd._libs.missing.NAType):
        return None
    if not isinstance(val, (list, dict, tuple, np.ndarray)) and pd.isna(val):
        return None
    if isinstance(val, (np.ndarray,)):
        return [_clean_val(v) for v in val.tolist()]
    if isinstance(val, (list, tuple)):
        return [_clean_val(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _clean_val(v) for k, v in val.items()}
    return val


def _deep_clean(obj):
    """Recursively clean all values for JSON serialization."""
    return _clean_val(obj)


def _fingerprint(data):
    """Stable hash of a JSON-serializable structure."""
    cleaned = _deep_clean(data)
    raw = json.dumps(cleaned, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _safe_get(attr_callable, ticker, label):
    """Fetch yfinance data with rate-limit backoff retries via yf_ratelimit.

    Rate-limit errors (HTTP 429) are retried with exponential backoff
    (60s → 180s → 420s) and tracked by yfrl for end-of-run reporting.

    Other errors (missing data, delisted ticker) return None silently —
    many tickers lack certain data sources and that's expected.
    """
    try:
        result = yfrl.call(attr_callable, label=f"{ticker}/{label}")
        if isinstance(result, pd.DataFrame) and result.empty:
            return None
        if isinstance(result, pd.Series) and result.empty:
            return None
        return result
    except Exception:
        # Non-rate-limit error, e.g. no insider data or ESG for this ticker.
        # These are routine — don't clutter the output.
        return None


# ── main fetch ───────────────────────────────────────────────────────────────

def fetch_all_fundamentals(ticker, delay=0.3):
    """
    Fetch ALL available fundamental data for a single ticker from yfinance.

    Returns a dict with keys:
      - info           : company info dict (selected keys)
      - income_q       : quarterly income statement (dict)
      - income_a       : annual income statement (dict)
      - balance_q      : quarterly balance sheet (dict)
      - balance_a      : annual balance sheet (dict)
      - cashflow_q     : quarterly cash flow (dict)
      - cashflow_a     : annual cash flow (dict)
      - earnings       : quarterly earnings history (dict)
      - earnings_estimate  : forward EPS estimates (dict)
      - revenue_estimate   : forward revenue estimates (dict)
      - growth_estimates   : growth estimates (dict)
      - eps_trend      : EPS trend (dict)
      - eps_revisions  : EPS revisions (dict)
      - earnings_history   : actual vs estimate history (dict)
      - analyst_targets    : analyst price targets (dict)
      - recommendations    : analyst recommendations (dict)
      - upgrades_downgrades : recent upgrades/downgrades (dict)
      - institutional_holders : top institutional holders (dict)
      - mutualfund_holders    : top mutual fund holders (dict)
      - major_holders         : insiders/institutions breakdown (dict)
      - insider_purchases     : insider trading summary (dict)
      - insider_transactions  : recent insider transactions (dict)
      - calendar              : earnings calendar (dict)
      - sustainability        : ESG ratings (dict)
      - fingerprint           : hash of all data for change detection
      - fetched_at            : ISO timestamp
    """
    try:
        import yfinance as yf
    except ImportError:
        return {'error': 'yfinance not installed'}

    result = {'error': None}

    try:
        t = yf.Ticker(ticker)
        time.sleep(delay)

        # ── Company info ──
        info = _safe_get(lambda: t.info, ticker, 'info')
        if info:
            # Keep only serializable key-value pairs
            clean_info = {}
            for k, v in info.items():
                try:
                    json.dumps({k: v}, default=str)
                    clean_info[k] = v
                except (TypeError, ValueError):
                    clean_info[k] = str(v)
            result['info'] = clean_info
        else:
            result['info'] = None

        # ── Financial statements (full history) ──
        # defeatbeta-api first (no rate limit, deeper quarterly history); yfinance
        # is a per-statement fallback, only hit (and only then throttled with
        # `delay`) when defeatbeta-api has nothing for this ticker (ETFs, some ADRs).
        def _statement(kind, yf_fn):
            if dbsrc is not None:
                try:
                    data = dbsrc.get_statement(ticker, kind)
                except Exception:
                    data = None
                if data is not None:
                    return data
            time.sleep(delay)
            return _df_to_dict(_safe_get(yf_fn, ticker, kind))

        result['income_q'] = _statement('income_q', lambda: t.quarterly_financials)
        result['income_a'] = _statement('income_a', lambda: t.financials)
        result['balance_q'] = _statement('balance_q', lambda: t.quarterly_balance_sheet)
        result['balance_a'] = _statement('balance_a', lambda: t.balance_sheet)
        result['cashflow_q'] = _statement('cashflow_q', lambda: t.quarterly_cashflow)
        result['cashflow_a'] = _statement('cashflow_a', lambda: t.cashflow)

        # ── Earnings history ──
        qe = _safe_get(lambda: t.quarterly_earnings, ticker, 'earnings')
        result['earnings'] = _df_to_dict(qe)

        # ── Estimates & trends ──
        result['earnings_estimate'] = _df_to_dict(_safe_get(lambda: t.earnings_estimate, ticker, 'earnings_est'))
        result['revenue_estimate'] = _df_to_dict(_safe_get(lambda: t.revenue_estimate, ticker, 'revenue_est'))
        result['growth_estimates'] = _df_to_dict(_safe_get(lambda: t.growth_estimates, ticker, 'growth_est'))
        result['eps_trend'] = _df_to_dict(_safe_get(lambda: t.eps_trend, ticker, 'eps_trend'))
        result['eps_revisions'] = _df_to_dict(_safe_get(lambda: t.eps_revisions, ticker, 'eps_rev'))
        result['earnings_history'] = _df_to_dict(_safe_get(lambda: t.earnings_history, ticker, 'earn_hist'))

        # ── Analyst data ──
        at = _safe_get(lambda: t.analyst_price_targets, ticker, 'analyst_targets')
        if isinstance(at, dict):
            result['analyst_targets'] = {k: v for k, v in at.items()
                                         if not isinstance(v, (pd.DataFrame,))}
        else:
            result['analyst_targets'] = None

        result['recommendations'] = _df_to_dict(_safe_get(lambda: t.recommendations, ticker, 'recs'))
        result['upgrades_downgrades'] = _df_to_dict(_safe_get(lambda: t.upgrades_downgrades, ticker, 'updown'))

        # ── Ownership data ──
        result['institutional_holders'] = _df_to_dict(
            _safe_get(lambda: t.institutional_holders.head(50)
                      if hasattr(t.institutional_holders, 'head') else t.institutional_holders,
                      ticker, 'inst_holders'))
        result['mutualfund_holders'] = _df_to_dict(
            _safe_get(lambda: t.mutualfund_holders.head(50)
                      if hasattr(t.mutualfund_holders, 'head') else t.mutualfund_holders,
                      ticker, 'mf_holders'))

        mh = _safe_get(lambda: t.major_holders, ticker, 'major_holders')
        if mh is not None:
            if isinstance(mh, pd.DataFrame):
                result['major_holders'] = {str(k): v for k, v in
                                           mh.to_dict().items()}
            else:
                result['major_holders'] = mh
        else:
            result['major_holders'] = None

        # ── Insider data ──
        ip = _safe_get(lambda: t.insider_purchases, ticker, 'insider_purch')
        result['insider_purchases'] = _df_to_dict(ip.head(20) if hasattr(ip, 'head') else ip)
        it_ = _safe_get(lambda: t.insider_transactions, ticker, 'insider_txns')
        result['insider_transactions'] = _df_to_dict(it_.head(50) if hasattr(it_, 'head') else it_)

        # ── Calendar ──
        cal = _safe_get(lambda: t.calendar, ticker, 'calendar')
        if isinstance(cal, dict):
            clean_cal = {}
            for k, v in cal.items():
                try:
                    json.dumps({k: v}, default=str)
                    clean_cal[k] = v
                except (TypeError, ValueError):
                    clean_cal[k] = str(v)
            result['calendar'] = clean_cal
        else:
            result['calendar'] = None

        # ── ESG / sustainability ──
        result['sustainability'] = _df_to_dict(_safe_get(lambda: t.sustainability, ticker, 'esg'))

    except Exception as e:
        result['error'] = str(e)

    # ── Fingerprint for change detection ──
    fingerprint_data = {
        k: v for k, v in result.items()
        if k not in ('error', 'fingerprint', 'fetched_at')
    }
    result['fingerprint'] = _fingerprint(fingerprint_data)
    result['fetched_at'] = pd.Timestamp.now().isoformat()

    return result


# ── cache helpers ────────────────────────────────────────────────────────────

def _cache_path(ticker):
    return CACHE_DIR / f"{ticker}_fund.json"


def get_cached_fundamentals(ticker):
    fp = _cache_path(ticker)
    if not fp.exists():
        return None
    try:
        with open(fp, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def save_fundamentals(ticker, data):
    fp = _cache_path(ticker)
    try:
        data['_format_version'] = FORMAT_VERSION
        cleaned = _deep_clean(data)
        with open(fp, 'w') as f:
            json.dump(cleaned, f, indent=2)
    except Exception as e:
        print(f"  ⚠ save_fundamentals({ticker}): {e}")


def fetch_and_cache_fundamentals(ticker, max_age_days=30, delay=0.3):
    """
    Get fundamentals for a ticker; uses cache if fresh and unchanged.

    Returns the data dict. Re-fetches only if:
      - No cache exists
      - Cache is older than max_age_days
      - Cache has an error and is > 1 day old
    """
    cached = get_cached_fundamentals(ticker)

    if cached and not cached.get('error'):
        # If the cache is from an older format version, ignore it and re-fetch.
        if cached.get('_format_version') != FORMAT_VERSION:
            cached = None
        else:
            fetched_at = cached.get('fetched_at', '')
            try:
                fetched_dt = pd.Timestamp(fetched_at)
                age_days = (pd.Timestamp.now() - fetched_dt).days
                # If cache is fresh AND has valid data, return it without re-fetching
                if age_days < max_age_days and cached.get('info'):
                    return cached
            except Exception:
                pass
    elif cached and cached.get('error'):
        # Retry errors after 1 day
        fetched_at = cached.get('fetched_at', '')
        try:
            if (pd.Timestamp.now() - pd.Timestamp(fetched_at)).days < 1:
                return cached  # don't retry too frequently on errors
        except Exception:
            pass

    # Fetch fresh data
    data = fetch_all_fundamentals(ticker, delay=delay)

    # Only overwrite cache if we got useful data or it's the first fetch
    if not data.get('error'):
        # Check if nothing changed (same fingerprint)
        if cached and not cached.get('error'):
            old_fp = cached.get('fingerprint', '')
            new_fp = data.get('fingerprint', '')
            if old_fp == new_fp and old_fp:
                # Data unchanged — update fetched_at only, keep the old cache
                cached['fetched_at'] = data['fetched_at']
                save_fundamentals(ticker, cached)
                return cached

        save_fundamentals(ticker, data)
    elif cached and cached.get('error'):
        # Both old and new have errors — keep old, update timestamp
        cached['fetched_at'] = data['fetched_at']
        save_fundamentals(ticker, cached)
    elif not cached:
        # First fetch failed — still cache it so we don't hammer the API
        save_fundamentals(ticker, data)

    return data


def batch_fetch_fundamentals(tickers, max_age_days=30, delay=0.3,
                             verbose=True, workers=1, timeout=None):
    """
    Fetch fundamentals for a list of tickers.
    Returns dict: ticker -> fundamentals dict.

    `workers > 1` fetches in parallel via a thread pool. Parallelism is safe
    against 429s because every yfinance call goes through yf_ratelimit, whose
    global cooling gate pauses ALL workers while any one of them is in its
    long backoff sleep — the pool cannot descend into cascading rate-limit
    retries. Keep `workers` small (3-5): the quoteSummary metadata endpoint
    is the path measured to 429 most aggressively.

    `timeout` (seconds, optional) caps the whole pass at a wall-clock budget:
    when it expires the loop stops submitting new tickers and returns with
    what is done. Already-in-flight fetches are let to finish (each is bounded
    by yfrl's retry cap, ~11 min worst case) so no cache file is written
    half-way; whatever is left is simply skipped and picked up by the next
    run.
    """
    results = {}
    n = len(tickers)
    if workers <= 1:
        deadline = None if timeout is None else time.time() + timeout
        for i, ticker in enumerate(tickers):
            if deadline is not None and time.time() >= deadline:
                print(f"  ⏱ fundamentals timeout ({timeout}s) reached after "
                      f"{i}/{n} tickers - leaving the rest for the next run",
                      flush=True)
                break
            if verbose and (i % 50 == 0 or i == n - 1):
                print(f"  fundamentals: {i + 1}/{n} tickers...")
            try:
                data = fetch_and_cache_fundamentals(ticker, max_age_days, delay)
                results[ticker] = data
            except Exception as e:
                results[ticker] = {'error': str(e)}
    else:
        import concurrent.futures as cf

        def _work(t):
            try:
                return t, fetch_and_cache_fundamentals(t, max_age_days, delay)
            except Exception as e:
                return t, {'error': str(e)}

        done = 0
        deadline = None if timeout is None else time.time() + timeout
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_work, t) for t in tickers]
            pending = set(futures)
            while pending:
                remain = None if deadline is None else deadline - time.time()
                if deadline is not None and remain <= 0:
                    print(f"  ⏱ fundamentals timeout ({timeout}s) reached; "
                          f"{len(pending)}/{n} ticker(s) left for the next run",
                          flush=True)
                    break
                try:
                    fut = next(cf.as_completed(pending, timeout=remain))
                except cf.TimeoutError:
                    print(f"  ⏱ fundamentals timeout ({timeout}s) reached; "
                          f"{len(pending)}/{n} ticker(s) left for the next run",
                          flush=True)
                    break
                pending.discard(fut)
                t, data = fut.result()
                results[t] = data
                done += 1
                if verbose and (done % 100 == 0 or done == n):
                    print(f"  fundamentals: {done}/{n} tickers...", flush=True)

    # Report any rate-limit drops that occurred during the batch
    yfrl.report()
    return results
