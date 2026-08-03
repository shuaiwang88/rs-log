# yfinance rate limits — what is actually true

Measured 2026-08-02 against yfinance 1.0. Yahoo publishes no rate limits and the endpoints are
undocumented and unofficial, so everything below is either read out of the installed library
or measured directly. Treat the measured numbers as "what did not get blocked today", not as
a contract.

## The one thing that matters most

**yfinance does not retry, back off, or throttle.** On HTTP 429 it raises `YFRateLimitError`
and stops (`data.py:451`, `data.py:339`, `data.py:231`). `scrapers/history.py:217` re-raises it
explicitly rather than swallowing it. There is no built-in limiter, and since the switch to
`curl_cffi` the old `requests_cache` session trick no longer applies (`data.py:109`).

So every bit of pacing is the caller's responsibility, and a 429 anywhere in this repo means
data silently goes missing rather than the job failing loudly — most of our call sites wrap
the download in a bare `except: pass`.

## Concurrency yfinance actually uses

`yf.download(..., threads=True)` sets the pool to `min(len(tickers), cpu_count * 2)`
(`multi.py:148`). On this machine `cpu_count` is 10, so **`threads=True` means 20 concurrent
requests**, regardless of how large the batch is. A batch of 400 tickers is not 400 parallel
requests; it is 400 requests pushed through 20 lanes.

One ticker = one chart request. There is no multi-symbol endpoint being used.

## Measured behaviour

| test | result |
|---|---|
| 6 sequential chart requests, 1 s apart | 6 × 200 |
| 20 concurrent chart requests (one `threads=True` batch) | 20 × 200 in 1.6 s |
| 5 × 20 concurrent, 0.4 s apart = 100 requests | 100 × 200 in 10.2 s (~10 req/s) |
| `Ticker.info` / `.splits` / `.fast_info` | all 200 once recovered |

Earlier in the same session **every** `Ticker.splits` call failed with a rate-limit error, and
a raw `curl` to `query1.finance.yahoo.com` returned **429**. Roughly twenty minutes later,
with no change other than elapsed time, all of it worked. So:

- the limit is real and reachable through ordinary use,
- recovery is on the order of minutes, not hours,
- the chart endpoint tolerated ~10 req/s sustained without complaint on the day of testing.

The trigger was almost certainly the 7 132-ticker cache scan plus repeated `Ticker` metadata
calls in quick succession, not the paced chart traffic.

## What this repo currently does

| script | batch | sleep | threads | requests per full run |
|---|---|---|---|---|
| `update_ticker_cache.py` | 100 | 0.4 s | yes (20) | ~7 132 |
| `add_21d_daily_volume_to_stocks.py` | 300 | 0.3 s | yes (20) | one per ticker |
| `update_volume_column.py` | 400 | 0.1 s | yes (20) | one per ticker |
| `backfill_rs_stocks_365_commits.py` | 500 | 2.0 s | no | one per ticker |
| `fast_git_rewrite_365.py` | 500 | 2.0 s | no | one per ticker |
| `fetch_top_funds.py` | — | — | `ThreadPoolExecutor(10)` | one `Ticker` per fund/holding |

`update_volume_column.py` is the most aggressive: 400-ticker batches with a **0.1 s** pause.
`fetch_top_funds.py` is the most exposed, because it uses `Ticker` metadata (the heavier path)
across ten threads.

## Practical guidance

1. **Chart data (`yf.download`, `Ticker.history`) is the cheap path.** Keep it near or below
   ~10 req/s and it has been fine. The existing 0.3–0.4 s inter-batch sleeps are adequate;
   the 0.1 s in `update_volume_column.py` is the one worth raising.
2. **`Ticker.info` / `.splits` / `.financials` are the expensive path.** They need a
   cookie+crumb handshake and are what actually produced the 429 here. Space them out, cache
   the result, and never call them in a tight loop over thousands of symbols.
3. **Catch `YFRateLimitError` explicitly.** Today a 429 lands in a bare `except: pass` and the
   ticker is silently skipped — the run "succeeds" with missing data. It should back off and
   retry, or at minimum report which symbols were dropped.
4. **Back off exponentially and resume.** Recovery took minutes, so a 60 s sleep and one retry
   would have salvaged every failed call in this session.
5. **Do not raise concurrency.** `threads=True` already gives 20 lanes; passing an integer
   larger than `cpu_count * 2` is the fastest way to get blocked.

## Re-running these measurements

```
python3 - <<'PY'
import time, requests, concurrent.futures as cf
from collections import Counter
H = {"User-Agent": "Mozilla/5.0"}
def get(s):
    return requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=5d&interval=1d",
        headers=H, timeout=10).status_code
syms = "AAPL MSFT NVDA AMZN GOOG META TSLA AVGO JPM V".split() * 2
with cf.ThreadPoolExecutor(max_workers=20) as ex:
    print(dict(Counter(ex.map(get, syms))))
PY
```

A `{200: 20}` means there is headroom; a `429` in the mix means back off and wait a few
minutes before running any bulk job.
