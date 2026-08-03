"""Rate-limit-aware wrappers around yfinance, plus a record of what got dropped.

yfinance 1.0 does NOT retry, back off, or throttle. On HTTP 429 it raises YFRateLimitError and
stops (data.py:451). Every bulk caller in this repo wrapped its download in a bare
`except Exception: pass`, so a rate-limited batch was indistinguishable from a batch of
delisted symbols: the tickers silently vanished and the run still reported success. That is
the failure mode this module exists to remove - a partial refresh that looks complete is worse
than one that fails, because every downstream metric is then computed on quietly missing data.

Measured on 2026-08-02: the chart endpoint sustained ~10 req/s without complaint, while the
`Ticker` metadata path (cookie+crumb - .info, .splits, .funds_data) produced 429s and stayed
limited for roughly twenty minutes. So the backoff here starts at a minute, not a second.

See docs/yfinance_rate_limits.md for the measurements.
"""
import time

try:
    from yfinance.exceptions import YFRateLimitError
except Exception:                       # older/newer yfinance, or import failure
    class YFRateLimitError(Exception):
        pass

# Recovery was on the order of minutes when measured, so short retries just burn the budget.
BACKOFF = (60, 180, 420)

_dropped = {}          # label -> list of reasons; what a run failed to fetch
_rate_limit_hits = 0


def _is_rate_limit(exc):
    """429 does not always arrive as YFRateLimitError - some paths surface it as text."""
    if isinstance(exc, YFRateLimitError):
        return True
    s = str(exc).lower()
    return '429' in s or 'too many requests' in s or 'rate limit' in s


def note_dropped(label, reason):
    _dropped.setdefault(str(label), []).append(str(reason)[:120])


def dropped():
    return dict(_dropped)


def report(prefix="  "):
    """Print what the run failed to fetch. Call this before a script exits."""
    if not _dropped and not _rate_limit_hits:
        return
    if _rate_limit_hits:
        print(f"{prefix}⚠ hit yfinance rate limiting {_rate_limit_hits} time(s) - "
              f"backed off and retried")
    if _dropped:
        n = len(_dropped)
        print(f"{prefix}⚠ {n} item(s) could NOT be fetched and were skipped:")
        for k in sorted(_dropped)[:20]:
            print(f"{prefix}    {k}: {_dropped[k][0]}")
        if n > 20:
            print(f"{prefix}    ... and {n - 20} more")


def call(fn, *args, label=None, retries=len(BACKOFF), **kwargs):
    """Run `fn`, retrying ONLY on rate limiting. Returns None if it never succeeds.

    Deliberately narrow: any other exception is re-raised for the caller to handle as before,
    because retrying a delisted symbol or a bad argument just wastes the request budget that
    the real rate limiting is competing for.
    """
    global _rate_limit_hits
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _is_rate_limit(e):
                raise
            _rate_limit_hits += 1
            if attempt >= retries:
                note_dropped(label or getattr(fn, '__name__', 'call'),
                             f"rate limited after {retries + 1} attempts")
                return None
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            print(f"  ⏳ yfinance rate limited; sleeping {wait}s "
                  f"(attempt {attempt + 1}/{retries})", flush=True)
            time.sleep(wait)
    return None


def download(*args, label=None, **kwargs):
    """yf.download with rate-limit backoff. Returns None when it could not be fetched."""
    import yfinance as yf
    return call(yf.download, *args, label=label or 'download', **kwargs)
