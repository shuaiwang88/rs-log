#!/usr/bin/env python3
"""
two_phase_backtest.py
=====================
Two-phase entry strategy on OUR pattern engine (tv_pattern_scanner.py, the
drw_pattern.pine port — same patterns the 📐 TV Pattern tab scans).

Phase 1 (pre-breakout): SMA50 Bounce+Shakeout -> half pos -> target_2r
Phase 2 (breakout):     Pivot Breakout          -> full pos -> target_5r

The signal detection is the same engine tv_engine.detect_buy_signals feeds the
scanner_universe backtest, so the two engines can't drift. Each base now also carries
its SPY regime at entry, price bucket and base shape, and --spy-regime / --max-price
apply the market filters the history backtest found.

Usage:
    python3 python/backtests/two_phase_backtest.py
    python3 python/backtests/two_phase_backtest.py --max-tickers 300 --workers 8
    python3 python/backtests/two_phase_backtest.py --spy-regime bull --max-price 250

Outputs:
    two_phase_results.csv     every base with P1/P2 entries and combined return
"""
import glob, time, warnings
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np, pandas as pd

warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = Path(__file__).resolve().parent

from tv_engine import (detect_buy_signals, extract_bases, price_bucket, regime_arrays,
                       regime_label, scan_record, ticker_signals)

MIN_PRICE, MIN_VOL, MIN_BARS = 12.0, 500_000, 100


def apply_exit(highs, closes, entry_bar, entry_price, base_low, target_rr, max_hold=60):
    """Exit at target R:R or time out — matching scanner_universe's target exit logic (NO stop-loss)."""
    n = len(closes)
    risk = entry_price - base_low
    target = entry_price + risk * target_rr if risk > 0 else entry_price * 1.5
    for bar in range(entry_bar + 1, min(entry_bar + max_hold + 1, n)):
        if highs[bar] >= target:
            return bar, target, (target - entry_price) / entry_price * 100, 'target'
    exit_bar = min(entry_bar + max_hold, n - 1)
    return exit_bar, closes[exit_bar], (closes[exit_bar] - entry_price) / entry_price * 100, 'timeout'


# ── Worker (our scanner imported per-process; nothing exec'd) ──
_SPY = None


def process_ticker(ticker, fpath):
    try:
        df = pd.read_parquet(fpath)
        if df.empty or len(df) < MIN_BARS:
            return []
        if df["Close"].iloc[-1] < MIN_PRICE or df["Volume"].tail(50).mean() < MIN_VOL:
            return []

        rec, df = scan_record(ticker, str(fpath), _SPY, df)
        if rec is None or not rec.get("history"):
            return []

        bases = extract_bases(rec, len(df))
        sig = ticker_signals(df, _SPY)
        if not bases or sig is None:
            return []

        n = len(df)
        highs = df["High"].to_numpy(dtype=float)
        lows = df["Low"].to_numpy(dtype=float)
        closes = df["Close"].to_numpy(dtype=float)
        opens = df["Open"].to_numpy(dtype=float)
        reg = regime_arrays(_SPY, df)

        trades = []
        for base in bases:
            pivot = base["pivot"]
            bLow = base["bLow"]
            if pivot is None or pivot <= 0 or bLow is None or bLow <= 0:
                continue
            search_s = max(0, base["start"])
            search_e = min(n - 1, (base["bo_bar"] if base["bo_bar"] is not None else base["end"]) + 5)

            signals = detect_buy_signals(sig, highs, lows, closes, opens, pivot, bLow,
                                         search_s, search_e, base["bo_bar"])
            if not signals:
                continue

            # Phase 1: SMA50 Bounce+Shakeout (need both)
            p1 = None
            if "SMA50 Bounce" in signals and "Shakeout" in signals:
                sb = signals["SMA50 Bounce"]
                sk = signals["Shakeout"]
                p1 = (min(sb[0], sk[0]), closes[min(sb[0], sk[0])])

            # Phase 2: PB+SMA50 Bounce combo — use EARLIEST bar (matching scanner_universe)
            p2 = None
            if "Pivot Breakout" in signals and "SMA50 Bounce" in signals:
                pb = signals["Pivot Breakout"]
                sb = signals["SMA50 Bounce"]
                p2 = (sb[0], sb[1]) if sb[0] <= pb[0] else (pb[0], pb[1])

            if not p1 and not p2:
                continue

            res = {"ticker": ticker, "pattern": base["pattern"],
                   "shape": base["shape"] or "", "raw_pattern": base["raw_pattern"],
                   "depth": base["bDepPct"], "length": base["bCount"],
                   "pivot": pivot, "base_low": bLow,
                   "price_bucket": price_bucket(pivot)}

            for tag, ph, rr in (("p1", p1, 2), ("p2", p2, 5)):
                if ph:
                    eb, ep = ph
                    ex_b, ex_p, ret, ext = apply_exit(highs, closes, eb, ep, bLow, rr, 60)
                    res.update({f"{tag}_entry_bar": eb, f"{tag}_entry": round(ep, 2),
                                f"{tag}_exit_bar": ex_b, f"{tag}_exit_price": round(ex_p, 2),
                                f"{tag}_ret": round(ret, 2), f"{tag}_exit_type": ext,
                                f"{tag}_win": ret > 0})
                    if reg is not None and eb < len(reg["above200"]):
                        res[f"{tag}_spy_regime"] = regime_label(bool(reg["above200"][eb]),
                                                                bool(reg["bull"][eb]))
                        s200 = reg["s200"][eb]
                        spy = reg["spy"][eb]
                        res[f"{tag}_spy_vs_200"] = (round((spy / s200 - 1.0) * 100, 1)
                                                    if (np.isfinite(s200) and s200 > 0) else None)
                else:
                    res.update({f"{tag}_entry_bar": -1, f"{tag}_entry": 0, f"{tag}_exit_bar": -1,
                                f"{tag}_exit_price": 0, f"{tag}_ret": 0,
                                f"{tag}_exit_type": "none", f"{tag}_win": False})

            p1_weighted = res["p1_ret"] * 0.5 if p1 else 0
            p2_weighted = res["p2_ret"] * 1.0 if p2 else 0
            divisor = 1.5 if (p1 and p2) else (0.5 if p1 else 1.0)
            res["combined_ret"] = round((p1_weighted + p2_weighted) / divisor, 2)
            res["both_fired"] = bool(p1 and p2)
            trades.append(res)

        return trades
    except Exception:
        return []


def _init_worker(spy):
    global _SPY
    _SPY = spy


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--max-tickers", type=int, default=0)
    ap.add_argument("--spy-regime", choices=["all", "above200", "bull"], default="all",
                    help="keep only bases whose entry happened while SPY held its 200-day "
                         "(above200) or both 50 & 200-day (bull); all = no filter")
    ap.add_argument("--max-price", type=float, default=0.0,
                    help="drop bases whose pivot is above this (0 = no cap)")
    args = ap.parse_args()

    t0 = time.time()
    spy = None
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    if spy_path.exists():
        try:
            spy = pd.read_parquet(spy_path)["Close"]
        except Exception:
            spy = None

    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    tasks = [(Path(f).name.replace("_1d.parquet", ""), f) for f in files
             if Path(f).name.replace("_1d.parquet", "") not in ("SPY", "QQQ", "IWM")]
    if args.max_tickers:
        tasks = tasks[:args.max_tickers]

    print(f"🐢 TWO-PHASE BACKTEST — {len(tasks)} tickers (our tv_pattern_scanner engine)")
    print(f"   P1: SMA50+Shakeout → ½ pos → 2:1")
    print(f"   P2: PB+SMA50 Bounce → full pos → 5:1")
    if args.spy_regime != "all":
        print(f"   Market regime: {args.spy_regime} only")
    if args.max_price > 0:
        print(f"   Max pivot price: ${args.max_price:,.0f}")

    all_trades = []
    workers = args.workers if args.workers > 0 else None
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(spy,)) as ex:
        futs = {ex.submit(process_ticker, t, fp): t for t, fp in tasks}
        done = 0
        for fut in as_completed(futs):
            done += 1
            trades = fut.result()
            if trades:
                all_trades.extend(trades)
            if done % 500 == 0 or done == len(tasks):
                print(f"   {done}/{len(tasks)} · {len(all_trades):,} bases · {time.time()-t0:.0f}s", flush=True)

    if not all_trades:
        print("❌ No trades")
        return

    df = pd.DataFrame(all_trades)
    elapsed = time.time() - t0

    # ── findings filters ──
    def _entry_above200(r):
        return (r.get("p1_spy_regime") not in ("Bear", None)) or \
               (r.get("p2_spy_regime") not in ("Bear", None))

    def _entry_bull(r):
        return (r.get("p1_spy_regime") == "Bull") or (r.get("p2_spy_regime") == "Bull")

    if args.spy_regime == "above200":
        before = len(df)
        df = df[df.apply(_entry_above200, axis=1)]
        print(f"   SPY regime 'above200': kept {len(df):,} of {before:,} bases")
    elif args.spy_regime == "bull":
        before = len(df)
        df = df[df.apply(_entry_bull, axis=1)]
        print(f"   SPY regime 'bull': kept {len(df):,} of {before:,} bases")
    if args.max_price > 0:
        before = len(df)
        df = df[df["pivot"] <= args.max_price]
        print(f"   Max pivot price ${args.max_price:,.0f}: kept {len(df):,} of {before:,} bases")
    if df.empty:
        print("❌ No trades after filters")
        return

    p1_events = df[df["p1_entry_bar"] >= 0]
    p2_events = df[df["p2_entry_bar"] >= 0]
    both = df[df["both_fired"]]

    print(f"\n{'='*90}")
    print(f"📊 TWO-PHASE — {elapsed:.0f}s | {df['ticker'].nunique():,} tickers | {len(df):,} bases")
    print(f"{'='*90}")

    for label, d in [
        ("Phase 1: SMA50+Shakeout (½ pos, 2:1)", p1_events),
        ("Phase 2: PB+SMA50 Bounce (full pos, 5:1)", p2_events),
        ("COMBINED (both phases fired)", both),
        ("COMBINED (all bases)", df),
    ]:
        n = len(d)
        if n == 0:
            continue
        ret_col = "combined_ret" if label.startswith("COMBINED") else ("p1_ret" if "Phase 1" in label else "p2_ret")
        rets = d[ret_col]
        win = (rets > 0).mean()
        s = rets.mean()/rets.std() if rets.std() > 0 else 0
        print(f"\n  {label}")
        print(f"    Events: {n:>8,}  |  Win: {win*100:>5.1f}%  |  Avg: {rets.mean():>+6.2f}%  |  Med: {rets.median():>+6.2f}%  |  Sharpe: {s:.2f}")

    print(f"\n  --- Per-Pattern (combined) ---")
    for pat in sorted(df["pattern"].unique()):
        p = df[df["pattern"] == pat]
        if len(p) < 5:
            continue
        rets = p["combined_ret"]
        s = rets.mean()/rets.std() if rets.std() > 0 else 0
        print(f"    {pat:<16s}: n={len(p):>6,}  win={(rets>0).mean()*100:>5.1f}%  avg={rets.mean():>+6.2f}%  sharpe={s:.2f}")

    if "shape" in df.columns and df["shape"].notna().any():
        print(f"\n  --- By Base Shape (combined) ---")
        for sh, p in df.groupby("shape"):
            if not sh or len(p) < 5:
                continue
            rets = p["combined_ret"]
            print(f"    {sh:<16s}: n={len(p):>6,}  win={(rets>0).mean()*100:>5.1f}%  avg={rets.mean():>+6.2f}%")

    if "price_bucket" in df.columns:
        print(f"\n  --- By Price Bucket (combined) ---")
        for bk, p in df.groupby("price_bucket"):
            if len(p) < 5:
                continue
            rets = p["combined_ret"]
            print(f"    {bk:<9s}: n={len(p):>6,}  win={(rets>0).mean()*100:>5.1f}%  avg={rets.mean():>+6.2f}%")

    csv_path = OUTPUT_DIR / "two_phase_results.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n💾 Saved to {csv_path} ({len(df):,} rows)")


if __name__ == "__main__":
    main()
