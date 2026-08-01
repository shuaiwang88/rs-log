#!/usr/bin/env python3
"""
two_phase_backtest.py
=====================
Two-phase entry strategy — reuses scanner_universe_backtest's exact signal
detection (SMA50 Bounce, Shakeout, Pivot Breakout) to avoid engine mismatch.

Phase 1 (pre-breakout): SMA50 Bounce+Shakeout -> half pos -> target_2r
Phase 2 (breakout):     Pivot Breakout          -> full pos -> target_5r

Builds on scanner_universe_backtest's process_ticker signal detection verbatim.
"""
import glob, time, warnings
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np, pandas as pd

warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = Path(__file__).resolve().parent
SCANNER_PATH = ROOT_DIR / "python" / "ibd_pattern_scanner.py"

MIN_PRICE, MIN_VOL, MIN_BARS = 12.0, 500_000, 100


def load_patched_scanner():
    src = SCANNER_PATH.read_text()
    guard = "if latest['pOn'] and (latest['pCode'] > 0):"
    if guard not in src:
        raise RuntimeError("Scanner guard anchor not found")
    patched = src.replace(guard, "if True:  # patched", 1)
    ns = {"__file__": str(SCANNER_PATH)}
    exec(compile(patched, "ibd_patched", "exec"), ns)
    return ns["scan_single_ticker"]


# ── EXACT signal detection from scanner_universe_backtest.py ──

def sma50_series(closes):
    return pd.Series(closes).rolling(50, min_periods=10).mean().values


def detect_signals(hist, highs, lows, closes, opens, sma50, pivot, bLow, search_start, search_end):
    """
    EXACT copy of scanner_universe_backtest.py's signal detection loop.
    Returns dict: signal_name -> (bar, price)
    """
    n = len(closes)
    signals = {}

    # 1. Pivot Breakout — the bar the scanner marked as breakout
    # (We scan hist for boBar, same as scanner_universe)
    for j in range(search_start, min(search_end + 1, n)):
        if j < len(hist) and hist[j].get("boBar") is not None and hist[j].get("boBar") == j:
            bo = j
            entry = max(pivot, closes[bo]) if closes[bo] > pivot else pivot
            signals["Pivot Breakout"] = (bo, entry)
            break

    # Scan per-bar for remaining signals (exact copy of scanner_universe process_ticker)
    for j in range(search_start, min(search_end + 1, n)):
        st = hist[j]
        c = closes[j]

        # 2. Upside Reversal
        if st.get("upsideReversal") and bLow <= c <= pivot * 1.01 and "Upside Reversal" not in signals:
            signals["Upside Reversal"] = (j, c)
        # 3. Shakeout
        if st.get("shakeoutEntry") and pivot * 0.85 <= c <= pivot and "Shakeout" not in signals:
            signals["Shakeout"] = (j, c)
        # 4. Volume Dry-Up
        if st.get("volDryUp") and pivot * 0.95 <= c <= pivot * 1.01 and "Volume Dry-Up" not in signals:
            signals["Volume Dry-Up"] = (j, c)
        # 5. MA Touch
        if st.get("touchedMA") and c >= bLow and c <= pivot and "MA Touch" not in signals:
            signals["MA Touch"] = (j, c)
        # 6. Pocket Pivot
        if st.get("ppAny") and pivot * 0.90 <= c <= pivot * 1.01 and "Pocket Pivot" not in signals:
            signals["Pocket Pivot"] = (j, c)
        # 7. RS New High
        if st.get("rsNH") and pivot * 0.85 <= c <= pivot * 1.01 and "RS New High" not in signals:
            signals["RS New High"] = (j, c)
        # 8. SMA50 Bounce — EXACT copy of scanner_universe code
        if j >= 2 and not np.isnan(sma50[j]) and sma50[j] > 0:
            prev_tested = (lows[j - 1] <= sma50[j - 1] * 1.02
                           if not np.isnan(sma50[j - 1]) and sma50[j - 1] > 0 else False)
            if (prev_tested and c >= opens[j]
                    and bLow <= c <= pivot and "SMA50 Bounce" not in signals):
                signals["SMA50 Bounce"] = (j, c)

    return signals


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


# ── Worker (scanner loaded per-process) ──
_scan = None


def process_ticker(ticker, fpath):
    global _scan
    scan_fn = _scan
    try:
        df = pd.read_parquet(fpath)
        if df.empty or len(df) < MIN_BARS: return []
        if df["Close"].iloc[-1] < MIN_PRICE or df["Volume"].tail(50).mean() < MIN_VOL: return []

        res = scan_fn(ticker, str(fpath))
        if not res or not res.get("history"): return []

        hist = res["history"]; n_hist = len(hist)
        if n_hist < 60: return []

        highs = df["High"].values[-n_hist:]
        lows = df["Low"].values[-n_hist:]
        closes = df["Close"].values[-n_hist:]
        opens = df["Open"].values[-n_hist:]
        sma50 = sma50_series(closes)

        # Extract bases (exact copy of scanner_universe logic)
        bases = []; i = 0
        while i < n_hist:
            st = hist[i]
            if st.get("pOn") and st.get("pCode", 0) > 0:
                run_start = i; run_end = i
                for j in range(i, n_hist):
                    if not (hist[j].get("pOn") and hist[j].get("pCode", 0) > 0) and j > i: break
                    run_end = j
                bo_bar = None
                for j in range(run_start, min(run_end + 1, n_hist)):
                    if hist[j].get("boBar") is not None and hist[j].get("boBar") == j:
                        bo_bar = j; break
                pivot = bLow = bTop = bDepPct = bCount = pat_name = None
                for j in range(run_start, run_end + 1):
                    sj = hist[j]
                    if sj.get("pName"): pat_name = sj.get("pName")
                    if sj.get("boPivot") is not None: pivot = sj.get("boPivot")
                    if sj.get("bLow") is not None: bLow = sj.get("bLow")
                    if sj.get("bTop") is not None: bTop = sj.get("bTop")
                    if sj.get("bDepPct") is not None: bDepPct = sj.get("bDepPct")
                    if sj.get("bCount") is not None: bCount = sj.get("bCount")
                if pivot is None: pivot = bTop
                bases.append({"start": run_start, "end": run_end, "bo_bar": bo_bar,
                              "pivot": pivot, "bLow": bLow, "bTop": bTop,
                              "bDepPct": bDepPct, "bCount": bCount,
                              "pattern": pat_name or "Base"})
                i = run_end + 1; continue
            i += 1

        if not bases: return []

        trades = []
        for base in bases:
            pivot = base["pivot"]; bLow = base["bLow"]
            if pivot is None or pivot <= 0 or bLow is None or bLow <= 0: continue
            search_s = max(0, base["start"])
            search_e = min(len(closes) - 1, (base["bo_bar"] if base["bo_bar"] is not None else base["end"]) + 5)

            signals = detect_signals(hist, highs, lows, closes, opens, sma50, pivot, bLow, search_s, search_e)

            if not signals: continue

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

            if not p1 and not p2: continue

            res = {"ticker": ticker, "pattern": base["pattern"],
                   "depth": base["bDepPct"], "length": base["bCount"],
                   "pivot": pivot, "base_low": bLow}

            if p1:
                eb, ep = p1
                ex_b, ex_p, ret, ext = apply_exit(highs, closes, eb, ep, bLow, 2, 60)
                res.update({"p1_entry_bar": eb, "p1_entry": round(ep, 2),
                            "p1_exit_bar": ex_b, "p1_exit_price": round(ex_p, 2),
                            "p1_ret": round(ret, 2), "p1_exit_type": ext, "p1_win": ret > 0})
            else:
                res.update({"p1_entry_bar": -1, "p1_entry": 0, "p1_exit_bar": -1,
                            "p1_exit_price": 0, "p1_ret": 0, "p1_exit_type": "none", "p1_win": False})

            if p2:
                eb, ep = p2
                ex_b, ex_p, ret, ext = apply_exit(highs, closes, eb, ep, bLow, 5, 60)
                res.update({"p2_entry_bar": eb, "p2_entry": round(ep, 2),
                            "p2_exit_bar": ex_b, "p2_exit_price": round(ex_p, 2),
                            "p2_ret": round(ret, 2), "p2_exit_type": ext, "p2_win": ret > 0})
            else:
                res.update({"p2_entry_bar": -1, "p2_entry": 0, "p2_exit_bar": -1,
                            "p2_exit_price": 0, "p2_ret": 0, "p2_exit_type": "none", "p2_win": False})

            p1_weighted = res["p1_ret"] * 0.5 if p1 else 0
            p2_weighted = res["p2_ret"] * 1.0 if p2 else 0
            divisor = 1.5 if (p1 and p2) else (0.5 if p1 else 1.0)
            res["combined_ret"] = round((p1_weighted + p2_weighted) / divisor, 2)
            res["both_fired"] = bool(p1 and p2)
            trades.append(res)

        return trades
    except Exception:
        return []


def _init_worker():
    global _scan
    _scan = load_patched_scanner()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--max-tickers", type=int, default=0)
    args = ap.parse_args()

    t0 = time.time()
    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    tasks = [(Path(f).name.replace("_1d.parquet", ""), f) for f in files
             if Path(f).name.replace("_1d.parquet", "") not in ("SPY", "QQQ", "IWM")]
    if args.max_tickers: tasks = tasks[:args.max_tickers]

    print(f"🐢 TWO-PHASE BACKTEST v2 — {len(tasks)} tickers")
    print(f"   P1: SMA50+Shakeout → ½ pos → 2:1 (signals from scanner_universe engine)")
    print(f"   P2: PB+SMA50 Bounce → full pos → 5:1")

    workers = args.workers if args.workers > 0 else None
    all_trades = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
        futs = {ex.submit(process_ticker, t, fp): t for t, fp in tasks}
        done = 0
        for fut in as_completed(futs):
            done += 1
            trades = fut.result()
            if trades: all_trades.extend(trades)
            if done % 500 == 0 or done == len(tasks):
                print(f"   {done}/{len(tasks)} · {len(all_trades):,} bases · {time.time()-t0:.0f}s", flush=True)

    if not all_trades:
        print("❌ No trades"); return

    df = pd.DataFrame(all_trades)
    elapsed = time.time() - t0
    p1_events = df[df["p1_entry_bar"] >= 0]
    p2_events = df[df["p2_entry_bar"] >= 0]
    both = df[df["both_fired"]]

    print(f"\n{'='*90}")
    print(f"📊 TWO-PHASE v2 — {elapsed:.0f}s | {df['ticker'].nunique():,} tickers | {len(df):,} bases")
    print(f"{'='*90}")

    for label, d in [
        ("Phase 1: SMA50+Shakeout (½ pos, 2:1)", p1_events),
        ("Phase 2: PB+SMA50 Bounce (full pos, 5:1)", p2_events),
        ("COMBINED (both phases fired)", both),
        ("COMBINED (all bases)", df),
    ]:
        n = len(d)
        if n == 0: continue
        ret_col = "combined_ret" if label.startswith("COMBINED") else ("p1_ret" if "Phase 1" in label else "p2_ret")
        rets = d[ret_col]
        win = (rets > 0).mean()
        s = rets.mean()/rets.std() if rets.std()>0 else 0
        print(f"\n  {label}")
        print(f"    Events: {n:>8,}  |  Win: {win*100:>5.1f}%  |  Avg: {rets.mean():>+6.2f}%  |  Med: {rets.median():>+6.2f}%  |  Sharpe: {s:.2f}")

    print(f"\n  --- Per-Pattern (combined) ---")
    for pat in sorted(df["pattern"].unique()):
        p = df[df["pattern"] == pat]
        if len(p) < 5: continue
        rets = p["combined_ret"]
        s = rets.mean()/rets.std() if rets.std()>0 else 0
        print(f"    {pat:<16s}: n={len(p):>6,}  win={(rets>0).mean()*100:>5.1f}%  avg={rets.mean():>+6.2f}%  sharpe={s:.2f}")

    csv_path = OUTPUT_DIR / "two_phase_results.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n💾 Saved to {csv_path} ({len(df):,} rows)")


if __name__ == "__main__":
    main()
