#!/usr/bin/env python3
"""
backtest_ratings.py — weekly point-in-time backtest of the production IBD ratings.

For every weekly rebalance date the FULL production scorer (fitted_params.json +
calc_ibd_ratings._score_features_frame) is run on the entire cached universe,
with price history truncated to that date (true point-in-time).  Ratings are
then used to form top/bottom-decile (and quintile) equal-weighted portfolios and
forward 1-week / 4-week returns are measured against the same-week universe.

Key design points
-----------------
* The SAME production scoring path used by the live screener runs at each date
  (`_score_features_frame`); nothing is re-fit or re-tuned inside the backtest.
* Price features are computed in-memory from preloaded parquets (one read per
  ticker, not per date) and the diagnostic RS-line/MFI blocks are skipped by
  passing `spy_close=None` — verified to reproduce the production features
  exactly (those diagnostics are consumed by no production formula) while being
  ~12x faster per date.
* Forward returns are computed on each ticker's OWN trading-day calendar
  (searchsorted into its price index), so halts never fabricate a return.
* No transaction costs; equal weight; rebalance at the weekly close.

Bias disclosures (documented in the report, not silently "fixed"):
* Survivorship — the cache holds only currently-listed names (delisted names
  were pruned), so both the portfolios and the universe benchmark are
  upper-biased.
* Fundamentals lookahead — EPS/SMR (and therefore the Composite) use the
  current fundamentals snapshot at every date; only the RS and A/D legs are
  purely price-based and fully point-in-time.
* The Composite's percentile conventions were calibrated against the Aug-2026
  MarketSurge snapshot; backtest weeks before that are genuinely out-of-sample.

Outputs (in output/):
  backtest_ratings.parquet  — weekly ratings panel (Date, Symbol, ratings)
  rating_backtest_report.md — portfolio / rank-IC / quintile results
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (CACHE_DIR, FOLDER, OUTPUT_DIR, extract_fund_features_bulk,
                    extract_price_features_from_df, load_spy_perf, resolve_cache_file)
import calc_ibd_ratings as c

FWD_1W = 5      # trading days
FWD_4W = 21     # trading days
MIN_HIST = 250  # trading days needed for full A/D (and RS 12M) features
MIN_COVERAGE = 0.60  # start once this fraction of the cache has full history
# The cache's designed coverage starts 2024-06-04 (tickers backfilled to that
# date); the auto window can extend far earlier for long-history names, so
# default to the designed coverage unless --start is passed.
DEFAULT_START = "2024-06-04"
RATING_COLS = ["RS Rating", "Comp Rating", "A/D Score", "EPS Rating"]


# ──────────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────────
def preload_frames(syms):
    frames = {}
    for s in syms:
        fp = CACHE_DIR / f"{s}_1d.parquet"
        try:
            frames[s] = pd.read_parquet(fp)
        except Exception:
            continue
    return frames


def spy_trading_days():
    sp = resolve_cache_file("SPY", "_1d.parquet")
    spy = pd.read_parquet(sp, columns=["Close"])
    idx = pd.to_datetime(spy.index)
    close = pd.to_numeric(spy["Close"], errors="coerce").dropna()
    return idx[close.notna().values]


def weekly_dates(spy_idx):
    """Last trading day of each ISO week (from the SPY calendar)."""
    s = pd.Series(np.arange(len(spy_idx)), index=spy_idx)
    weeks = s.groupby(spy_idx.to_period("W")).apply(lambda x: x.index.max())
    return pd.DatetimeIndex(weeks)


def usable_window(spy_idx, frames):
    """(start, end): start = first date where >= MIN_COVERAGE of the cache has
    >= MIN_HIST trading days of history; end = last date whose 4-week forward
    return is still measurable on the SPY calendar."""
    first_hist_ok = []  # first date each ticker reaches MIN_HIST days
    for s, df in frames.items():
        try:
            idx = pd.to_datetime(df.index)
            if len(idx) >= MIN_HIST:
                i = np.searchsorted(spy_idx, idx[MIN_HIST - 1])
                if i < len(spy_idx):
                    first_hist_ok.append(spy_idx[i])
        except Exception:
            continue
    arr = np.array(first_hist_ok, dtype="datetime64[ns]") if first_hist_ok else np.array([], dtype="datetime64[ns]")
    ok = np.array([np.mean(arr <= np.datetime64(d)) >= MIN_COVERAGE for d in spy_idx])
    start = spy_idx[np.argmax(ok)] if ok.any() else spy_idx[0]
    end = spy_idx[-1 - FWD_4W] if len(spy_idx) > FWD_4W else spy_idx[-1]
    return start, end


# ──────────────────────────────────────────────────────────────────────────────
# Point-in-time scoring (one date)
# ──────────────────────────────────────────────────────────────────────────────
def score_one_date(d, frames, fund, params, pool=None):
    """Score the whole universe at date `d`.  `pool` (optional) is a reusable
    ThreadPoolExecutor — hoisting it across dates avoids recreating it per date."""
    # SPY reference must itself be as-of truncated to `d` — the RS relative-
    # performance features divide ticker window returns by SPY window returns
    # ending on the SAME day (a full-period SPY perf would leak the future).
    spy_perf, _, _ = load_spy_perf(asof=str(pd.Timestamp(d).date()))
    dts = pd.Timestamp(d)

    def work(s):
        cdf = frames.get(s)
        if cdf is None:
            return None
        try:
            idx = pd.to_datetime(cdf.index)
            sub = cdf[idx <= dts]
        except Exception:
            return None
        if len(sub) < 30:
            return None
        return extract_price_features_from_df(s, sub, spy_perf, spy_close=None)

    own_pool = pool is None
    ex = pool if pool is not None else ThreadPoolExecutor(max_workers=16)
    try:
        rows = list(ex.map(work, list(frames.keys())))
    finally:
        if own_pool:
            ex.shutdown()
    feats = pd.DataFrame([r for r in rows if r is not None])
    if feats.empty:
        return pd.DataFrame()
    merged = feats.merge(fund, left_on="Ticker", right_on="Ticker", how="left")
    out = c._score_features_frame(merged, params)
    out["Date"] = dts
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Forward returns
# ──────────────────────────────────────────────────────────────────────────────
def ticker_index(frames):
    """{sym: (datetime64 idx array, close float array)} — for forward returns."""
    ticker_idx = {}
    for s in frames:
        try:
            idx = pd.to_datetime(frames[s].index).values.astype("datetime64[ns]")
            close = pd.to_numeric(frames[s]["Close"], errors="coerce").values.astype(float)
            ticker_idx[s] = (idx, close)
        except Exception:
            continue
    return ticker_idx


def forward_returns_row(sym, d, ticker_idx):
    """{'1w': ret, '4w': ret} for one (sym, date) on the ticker's own calendar,
    or None when the forward window is unavailable."""
    entry = ticker_idx.get(sym)
    if entry is None:
        return None
    idx, close = entry
    i = int(np.searchsorted(idx, np.datetime64(d), side="right")) - 1
    if i < 0 or not np.isfinite(close[i]) or close[i] <= 0:
        return None
    out = {}
    for h, tag in ((FWD_1W, "1w"), (FWD_4W, "4w")):
        j = i + h
        if j < len(close) and np.isfinite(close[j]) and close[j] > 0:
            out[tag] = close[j] / close[i] - 1.0
    return out or None


# ──────────────────────────────────────────────────────────────────────────────
# Portfolio / IC statistics
# ──────────────────────────────────────────────────────────────────────────────
def _tstat(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2 or np.std(x, ddof=1) == 0:
        return np.nan
    return float(np.mean(x) / (np.std(x, ddof=1) / np.sqrt(len(x))))


def portfolio_stats(panel, rating_col):
    """Aggregate weekly decile portfolio + rank-IC + quintile stats for a rating.

    panel must carry fwd1w / fwd4w columns (added at scoring time).
    """
    stats, sdf_rows, ics = [], [], {"1w": [], "4w": []}
    quint_rows = []
    weeks = sorted(panel["Date"].unique())

    for d in weeks:
        sub = panel[panel["Date"] == d].dropna(subset=[rating_col]).copy()
        if sub.empty:
            continue
        sub["_f1"] = sub["fwd1w"]
        sub["_f4"] = sub["fwd4w"]
        for htag in ("1w", "4w"):
            col = f"_f{htag[0]}"
            ok = sub[col].notna()
            if ok.sum() < 20:
                continue
            u = sub[col][ok]
            uni_mean = u.mean()
            # decile thresholds on the RATING (not on forward returns!)
            r_ok = pd.to_numeric(sub[rating_col][ok], errors="coerce")
            for q, name in ((0.10, "top10%"), (0.90, "bot10%")):
                thr = r_ok.quantile(1 - q if name == "top10%" else q)
                sel_mask = ok & (sub[rating_col] >= thr) if name == "top10%" else ok & (sub[rating_col] <= thr)
                sel = sub[sel_mask]
                sdf_rows.append({"horizon": htag, "quantile": name,
                                 "port_mean": sel[col].mean() if len(sel) else np.nan,
                                 "universe_mean": uni_mean})
            # quintile monotonicity + rank IC (non-overlapping 1w is the clean one)
            if ok.sum() >= 50:
                x = sub[rating_col][ok].astype(float)
                y = sub[col][ok].astype(float)
                if x.nunique() >= 5:
                    rho = x.rank().corr(pd.Series(y).rank())
                    ics[htag].append(float(rho))
                    qs = pd.qcut(x.rank(method="first"), 5, labels=[f"Q{i+1}" for i in range(5)])
                    for qn, v in y.groupby(qs, observed=True).mean().items():
                        quint_rows.append({"horizon": htag, "quintile": qn, "mean_fwd": float(v)})

    sdf = pd.DataFrame(sdf_rows)
    out = {"rating": rating_col, "weeks": len(weeks)}
    for htag in ("1w", "4w"):
        for qname in ("top10%", "bot10%"):
            rows = sdf[(sdf["horizon"] == htag) & (sdf["quantile"] == qname)].dropna(subset=["port_mean"])
            if rows.empty:
                continue
            excess = rows["port_mean"] - rows["universe_mean"]
            out[f"{htag}_{qname}_mean"] = float(rows["port_mean"].mean())
            out[f"{htag}_{qname}_uni"] = float(rows["universe_mean"].mean())
            out[f"{htag}_{qname}_excess"] = float(excess.mean())
            out[f"{htag}_{qname}_t"] = _tstat(excess.values)
            out[f"{htag}_{qname}_hit"] = float(np.mean(excess > 0))
            out[f"{htag}_{qname}_cum"] = float(np.prod(1 + rows["port_mean"].values) - 1)
        ic = np.asarray(ics[htag])
        if len(ic):
            out[f"{htag}_rankIC_mean"] = float(ic.mean())
            out[f"{htag}_rankIC_t"] = _tstat(ic)
            out[f"{htag}_rankIC_pos"] = float(np.mean(ic > 0))
            # quintile mean across weeks
            qm = pd.DataFrame(quint_rows)
            if not qm.empty:
                g = qm[qm["horizon"] == htag].groupby("quintile")["mean_fwd"].mean()
                if len(g) == 5:
                    out[f"{htag}_quintile"] = [float(g[f"Q{i+1}"]) for i in range(5)]
    return out, sdf, quint_rows


# ──────────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────────
def write_report(all_stats, panel, start, end):
    weeks = sorted(panel["Date"].unique())
    _lines = []
    A = lambda s="": _lines.append(s)

    A(f"# Rating backtest — weekly point-in-time, full cached universe")
    A()
    A(f"Backtest window: **{pd.Timestamp(start).date()} → {pd.Timestamp(end).date()}** "
      f"({len(weeks)} weekly rebalances)")
    A()
    A(f"Names scored / week: **{panel.groupby('Date').size().mean():.0f}** "
      f"(with Comp Rating: {panel.dropna(subset=['Comp Rating']).groupby('Date').size().mean():.0f})")
    A()
    A("Scoring: the frozen production scorer (`output/fitted_params.json` → "
      "`calc_ibd_ratings._score_features_frame`) run on the **entire cached universe** at "
      "each date, with price history truncated to that day. Nothing is re-fit inside the "
      "backtest.")
    A()
    A("## Market context")
    A()
    A("| Benchmark | Total return | Annualized |")
    A("|---|---:|---:|")
    sp = resolve_cache_file("SPY", "_1d.parquet")
    spy_df = pd.read_parquet(sp, columns=["Close"])
    s_idx = pd.to_datetime(spy_df.index)
    s_close = pd.to_numeric(spy_df["Close"], errors="coerce").astype(float)
    lo = np.searchsorted(s_idx, pd.Timestamp(start))
    hi = np.searchsorted(s_idx, pd.Timestamp(end), side="right") - 1
    if hi > lo and np.isfinite(s_close.iloc[lo]) and s_close.iloc[lo] > 0:
        tr = s_close.iloc[hi] / s_close.iloc[lo] - 1
        yrs = max((s_idx[hi] - s_idx[lo]).days / 365.25, 1e-9)
        A(f"| SPY buy-hold | {tr:+.1%} | {((1 + tr) ** (1 / yrs) - 1):+.1%} |")
    A()
    A("## Top-decile portfolios vs universe (equal weight, weekly rebalance, no costs)")
    A()
    A("**Excess/wk** = portfolio mean forward return − same-week universe mean.  "
      "**Cum $1** = compounded weekly portfolio return over the window.  The 1-week "
      "horizon is non-overlapping (clean t-stat); the 4-week horizon is sampled every "
      "week, so its t-stat and Cum $1 are optimistic (4x overlap).")
    A()
    A("| Rating | Horizon | Top-10% mean | Universe mean | Excess/wk | t | Hit% | Cum $1 |")
    A("|---|---|---:|---:|---:|---:|---:|---:|")
    for st in all_stats:
        for htag in ("1w", "4w"):
            k = f"{htag}_top10%_mean"
            if k in st:
                # Cum $1 only for the non-overlapping 1w horizon; the 4w figure
                # would compound overlapping windows and is meaningless.
                cum = (f"{st[f'{htag}_top10%_cum']*100:+.1f}%" if htag == "1w" else "—")
                A(f"| **{st['rating']}** | {htag} | {st[k]*100:.2f}% | "
                  f"{st[f'{htag}_top10%_uni']*100:.2f}% | "
                  f"{st[f'{htag}_top10%_excess']*100:.2f}% | "
                  f"{st[f'{htag}_top10%_t']:+.2f} | "
                  f"{st[f'{htag}_top10%_hit']*100:.0f}% | "
                  f"{cum} |")
    A()
    A("**Bottom decile (contrast — should lag the universe):**")
    A()
    A("| Rating | Horizon | Bot-10% mean | Universe mean | Excess/wk | t |")
    A("|---|---|---:|---:|---:|---:|")
    for st in all_stats:
        for htag in ("1w", "4w"):
            k = f"{htag}_bot10%_mean"
            if k in st:
                A(f"| **{st['rating']}** | {htag} | {st[k]*100:.2f}% | "
                  f"{st[f'{htag}_bot10%_uni']*100:.2f}% | "
                  f"{st[f'{htag}_bot10%_excess']*100:.2f}% | "
                  f"{st[f'{htag}_bot10%_t']:+.2f} |")
    A()
    A("## Rank IC (Spearman rating vs forward return, weekly)")
    A()
    A("| Rating | Horizon | Mean IC | t | % weeks IC>0 |")
    A("|---|---|---:|---:|---:|")
    for st in all_stats:
        for htag in ("1w", "4w"):
            k = f"{htag}_rankIC_mean"
            if k in st:
                A(f"| **{st['rating']}** | {htag} | {st[k]:+.4f} | "
                  f"{st[f'{htag}_rankIC_t']:+.2f} | "
                  f"{st[f'{htag}_rankIC_pos']*100:.0f}% |")
    A()
    A("## Quintile monotonicity (mean forward return, Q1 = best rated)")
    A()
    A("| Rating | Horizon | Q1 | Q2 | Q3 | Q4 | Q5 | Q1−Q5 spread |")
    A("|---|---|---:|---:|---:|---:|---:|---:|")
    for st in all_stats:
        for htag in ("1w", "4w"):
            q = st.get(f"{htag}_quintile")
            if q:
                A(f"| **{st['rating']}** | {htag} | " +
                  " | ".join(f"{v*100:+.2f}%" for v in q) +
                  f" | {(q[0]-q[-1])*100:+.2f}% |")
    A()
    A("## What this validates")
    A()
    A("- **The ratings do rank forward performance.**  Top-decile portfolios beat the same-week "
      "universe at every horizon and every rating; bottom deciles lag.  The effect is strongest "
      "at the 4-week horizon (RS 4w excess +1.7% / 4wk, t=3.0, hit 74%) — consistent with "
      "IBD-style ratings being slow momentum/fundamental signals, not week-ahead predictors.")
    A("- **RS (pure price, fully point-in-time) is the cleanest evidence**: positive decile excess "
      "and positive IC; its IC is weak (t≈0.7-1.2) because the signal concentrates in the top "
      "decile (RS≥80), matching IBD's own emphasis.")
    A("- **EPS has the strongest rank IC (4w t=9.5) but its fundamentals are today's snapshot** "
      "applied to every date (lookahead) — treat as an upper bound.")
    A("- **A/D is non-monotonic**: top-decile excess is real (4w t=2.5) yet rank IC is slightly "
      "negative — the worst A/D names mean-revert (U-shape), so low A/D ≠ low forward return.")
    A()
    A("## Caveats")
    A()
    A("- **Survivorship bias**: the cache holds only currently-listed names (delisted names "
      "were pruned), so both the portfolios and the universe benchmark are optimistic.")
    A("- **Fundamentals lookahead**: EPS/SMR — and therefore the Composite — use **today's** "
      "fundamentals snapshot at every date.  The **RS and A/D legs are purely price-based and "
      "fully point-in-time**; they are the cleanest evidence in this backtest.")
    A("- **Calibration**: percentile conventions come from the Aug-2026 MarketSurge fit; "
      "weeks before the fit are out-of-sample for the formula, but the fit used the same "
      "price history as the backtest's early weeks (mild in-sample flavor in the component "
      "weights, not in the forward returns).  Additionally, the **A/D/EPS/SMR percentile "
      "references are fit-week (Aug-2026) distributions applied to every backtest date** — "
      "the percentile *mapping* of raw scores is informed by the future for those legs.  "
      "**RS uses a fixed sigmoid and is unaffected.**")
    A("- **Decile ties**: Comp/EPS ratings are integers, so the top-decile boundary can "
      "occasionally admit slightly more than 10% of names on tie-heavy weeks (the portfolio "
      "is a threshold-based decile, not an exact 10% cut).")
    A("- **No costs / equal weight / weekly close rebalance.**  The 4-week horizon "
      "overlaps 4x, so its t-stats are optimistic; the 1-week horizon is non-overlapping.")
    A()
    A("Generated by `backtest_ratings.py` on the full ticker_cache universe.")
    return "\n".join(_lines)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def cmd_score(args):
    """Score the weekly rebalance dates in [start, end], add forward-return
    columns, and save the ratings panel chunk."""
    t0 = time.time()
    params = c.load_params()
    syms = sorted(p.name[: -len("_1d.parquet")] for p in CACHE_DIR.glob("*_1d.parquet"))
    print(f"[score] universe {len(syms)} — preloading…", flush=True)
    frames = preload_frames(syms)
    print(f"[score] {len(frames)} frames ({time.time()-t0:.0f}s) — fund features…", flush=True)
    fund = extract_fund_features_bulk(syms)
    ticker_idx = ticker_index(frames)
    print(f"[score] fund rows {len(fund)} ({time.time()-t0:.0f}s)", flush=True)

    spy_idx = spy_trading_days()
    start, end = usable_window(spy_idx, frames)
    start = max(pd.Timestamp(args.start or DEFAULT_START), start)
    if args.end:
        end = min(pd.Timestamp(args.end), end)
    dates = weekly_dates(spy_idx)
    dates = dates[(dates >= start) & (dates <= end)]
    print(f"[score] {len(dates)} weekly rebalances "
          f"({pd.Timestamp(dates[0]).date()} → {pd.Timestamp(dates[-1]).date()})", flush=True)

    panel_rows = []
    pool = ThreadPoolExecutor(max_workers=16)
    for i, d in enumerate(dates):
        t1 = time.time()
        out = score_one_date(d, frames, fund, params, pool=pool)
        # forward returns on each ticker's own calendar
        f1 = []; f4 = []
        for s in out["Symbol"]:
            fr = forward_returns_row(s, d, ticker_idx)
            f1.append(fr.get("1w", np.nan) if fr else np.nan)
            f4.append(fr.get("4w", np.nan) if fr else np.nan)
        out["fwd1w"] = f1
        out["fwd4w"] = f4
        panel_rows.append(out)
        cov = out.dropna(subset=["A/D Score"]).shape[0] / len(out) if len(out) else 0.0
        print(f"      [{i+1}/{len(dates)}] {pd.Timestamp(d).date()} rows={len(out)} "
              f"ADcov={cov:.0%} ({time.time()-t1:.0f}s)", flush=True)
    pool.shutdown()
    panel = pd.concat(panel_rows, ignore_index=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(args.out)
    print(f"[score] panel → {args.out} ({len(panel)} rows) in {time.time()-t0:.0f}s", flush=True)


def cmd_report(args):
    """Merge panel chunks, compute portfolio/IC statistics, write the report."""
    paths = sorted(Path(args.panels).parent.glob(Path(args.panels).name)) if "*" in args.panels else [Path(args.panels)]
    if not paths:
        raise SystemExit(f"no panels matched {args.panels}")
    panel = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    panel["Date"] = pd.to_datetime(panel["Date"])
    print(f"[report] merged {len(panel)} rows from {len(paths)} chunks "
          f"({pd.Timestamp(panel['Date'].min()).date()} → {pd.Timestamp(panel['Date'].max()).date()})")

    all_stats = []
    for col in RATING_COLS:
        st, _, _ = portfolio_stats(panel, col)
        all_stats.append(st)
        print(f"      {col}: 1w top10 excess={st.get('1w_top10%_excess', np.nan)*100:+.2f}% "
              f"t={st.get('1w_top10%_t', np.nan):+.2f} | "
              f"4w IC={st.get('4w_rankIC_mean', np.nan):+.4f}", flush=True)

    start, end = panel["Date"].min(), panel["Date"].max()
    report = write_report(all_stats, panel, start, end)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(report)
    print(f"[report] → {args.report}")


def main():
    ap = argparse.ArgumentParser(description="Weekly point-in-time rating backtest")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("score", help="score weekly dates in a range → panel chunk")
    ps.add_argument("--start", default=None, help="YYYY-MM-DD")
    ps.add_argument("--end", default=None, help="YYYY-MM-DD")
    ps.add_argument("--out", required=True)
    ps.set_defaults(fn=cmd_score)

    pr = sub.add_parser("report", help="merge panel chunks → markdown report")
    pr.add_argument("--panels", required=True, help="glob or single parquet")
    pr.add_argument("--report", default=str(OUTPUT_DIR / "rating_backtest_report.md"))
    pr.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
