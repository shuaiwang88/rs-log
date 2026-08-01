#!/usr/bin/env python3
"""
unified_watchlist.py
===================
Current-date watchlist: tickers that are TODAY in a Flat Base or Consolidation
pattern (depth 8-25%, length 100-200 days) AND show bullish trend-following
momentum (above SMA200, EMA10>EMA20, near 52-week high).

NOW WITH BOTH ENGINE'S TOP COMBOS:
  full_backtest (mid-base): SMA50 Bounce+Shakeout, SMA50 Bounce,
    Shakeout+Upside Reversal, Upside Reversal
  scanner_universe (breakout): Pivot Breakout+SMA50 Bounce,
    MA Touch+Pivot Breakout+Upside Reversal, Pivot Breakout

Uses the real ibd_pattern_scanner.py (patched to return full history).

Output:
  1. Console table ranked by composite score
  2. CSV: python/backtests/unified_watchlist.csv
"""
import argparse
import glob
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = Path(__file__).resolve().parent
SCANNER_PATH = ROOT_DIR / "python" / "ibd_pattern_scanner.py"

MIN_PRICE = 12.0
MIN_VOL_50 = 500_000
MIN_BARS = 100

OPT_DEPTH_MIN = 8.0
OPT_DEPTH_MAX = 25.0
OPT_LEN_MIN = 100
OPT_LEN_MAX = 200

# Trend-following momentum weights
TF_WEIGHTS = {
    'above_sma200': 2.0,
    'ema_bullish': 1.5,
    'near_52w_high': 1.0,
    'rsi_bullish': 0.5,
}

# Combo weights — full_backtest (mid-base) + scanner_universe (breakout)
COMBO_WEIGHTS = {
    # full_backtest top combos (mid-base)
    'SMA50 Bounce+Shakeout': 2.0,              # 0.56 sharpe
    'SMA50 Bounce': 1.5,                       # 0.55 sharpe
    'Shakeout+Upside Reversal': 1.0,           # 0.53 sharpe
    'Upside Reversal': 0.8,                    # 0.53 sharpe
    # scanner_universe top combos (breakout)
    'Pivot Breakout+SMA50 Bounce': 1.8,        # 0.40 sharpe
    'MA Touch+PB+Upside Reversal': 1.5,        # 0.34 sharpe
    'Pivot Breakout': 1.2,                     # baseline breakout
}


def load_patched_scanner():
    src = SCANNER_PATH.read_text()
    guard = "if latest['pOn'] and (latest['pCode'] > 0):"
    if guard not in src:
        raise RuntimeError("Scanner guard anchor not found")
    patched = src.replace(guard, "if True:  # patched: always return history", 1)
    ns = {"__file__": str(SCANNER_PATH)}
    exec(compile(patched, "ibd_pattern_scanner_patched", "exec"), ns)
    return ns["scan_single_ticker"]


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi_calc(closes, length=14):
    s = pd.Series(closes)
    d = s.diff()
    up = d.clip(lower=0.0).ewm(alpha=1.0 / length, adjust=False).mean()
    dn = (-d.clip(upper=0.0)).ewm(alpha=1.0 / length, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def find_pivots(highs, lows, left=5, right=5):
    n = len(highs)
    ph, pl = {}, {}
    for i in range(left, n - right):
        if all(highs[j] < highs[i] for j in range(i - left, i + right + 1) if j != i):
            ph[i] = highs[i]
        if all(lows[j] > lows[i] for j in range(i - left, i + right + 1) if j != i):
            pl[i] = lows[i]
    return ph, pl


def check_tf_momentum(closes, sma200, ema10, ema20, rsi_vals, highs):
    i = -1
    score = 0.0
    flags = {}

    flags['above_sma200'] = not np.isnan(sma200[i]) and closes[i] > sma200[i]
    if flags['above_sma200']:
        score += TF_WEIGHTS['above_sma200']

    flags['ema_bullish'] = ema10[i] > ema20[i]
    if flags['ema_bullish']:
        score += TF_WEIGHTS['ema_bullish']

    high52 = np.max(highs[-252:]) if len(highs) >= 252 else np.max(highs)
    pct_off_high = (high52 - closes[i]) / high52 * 100.0 if high52 > 0 else 100.0
    flags['near_52w_high'] = pct_off_high <= 15.0
    flags['pct_off_52w_high'] = round(pct_off_high, 1)
    if flags['near_52w_high']:
        score += TF_WEIGHTS['near_52w_high']

    flags['rsi_bullish'] = rsi_vals[i] > 50
    flags['rsi'] = round(rsi_vals[i], 1)
    if flags['rsi_bullish']:
        score += TF_WEIGHTS['rsi_bullish']

    return score, flags


def detect_combo_signals(highs, lows, closes, opens,
                         sma50, atr14,
                         pivot_price, bLow, lookback=20):
    """
    Detect the top-5 combo buy signals from full_backtest on recent bars.
    Searches the last `lookback` bars for SMA50 Bounce, Shakeout, Upside Reversal.

    Returns dict: combo_name -> (bar_offset, price) or None if not detected.
    """
    n = len(closes)
    search_start = max(0, n - lookback)
    search_end = n - 1
    signals = {}

    # 1. SMA50 Bounce — dip to SMA50 then reclaim
    for i in range(search_start, search_end + 1):
        if i < 2 or i >= n:
            continue
        if np.isnan(sma50[i]) or sma50[i] <= 0:
            continue
        prev_tested = (lows[i - 1] <= sma50[i - 1] * 1.02
                       if not np.isnan(sma50[i - 1]) and sma50[i - 1] > 0 else False)
        if not prev_tested or closes[i] <= sma50[i]:
            continue
        if closes[i] < opens[i]:
            continue
        if closes[i] < bLow or closes[i] > pivot_price:
            continue
        signals['SMA50 Bounce'] = (i, closes[i])
        break

    # 2. Upside Reversal — wide-range up bar within base
    for i in range(search_start, search_end + 1):
        if i < 1 or i >= n or i >= len(atr14):
            continue
        bar_range = highs[i] - lows[i]
        if bar_range <= 0 or atr14[i] <= 0 or bar_range < atr14[i] * 0.8:
            continue
        if closes[i] <= (highs[i] + lows[i]) / 2.0 or closes[i] < opens[i]:
            continue
        if closes[i] < bLow or closes[i] > pivot_price:
            continue
        signals['Upside Reversal'] = (i, closes[i])
        break

    # 3. Shakeout — undercut swing low then reclaim
    ema3_vals = pd.Series(closes).ewm(span=3, adjust=False).mean().values
    pl, _ = find_pivots(highs, lows, 3, 3)
    swing_lows = [(b, p) for b, p in pl.items()
                  if search_start - 10 <= b <= search_end]
    for sl_bar, sl_price in swing_lows:
        for i in range(sl_bar + 1, min(sl_bar + 6, search_end + 1, n)):
            if lows[i] < sl_price:
                for j in range(i, min(i + 4, search_end + 1, n)):
                    if closes[j] > ema3_vals[j] and ema3_vals[j] > 0:
                        if pivot_price * 0.85 <= closes[j] <= pivot_price:
                            signals['Shakeout'] = (j, closes[j])
                            break
                break

    # 4. Pivot Breakout — recent bar crosses pivot AND close stays above (the scanner_universe anchor)
    for i in range(search_start, search_end + 1):
        if highs[i] > pivot_price and closes[i] > pivot_price:
            # Only count if the breakout is still valid (close hasn't fallen back below pivot)
            if closes[-1] > pivot_price:
                signals['Pivot Breakout'] = (i, pivot_price)
            break

    # 5. MA Touch — bar touches EMA10/EMA20/SMA50
    ema10_v = pd.Series(closes).ewm(span=10, adjust=False).mean().values
    ema20_v = pd.Series(closes).ewm(span=20, adjust=False).mean().values
    for i in range(search_start, search_end + 1):
        if closes[i] < bLow or closes[i] > pivot_price:
            continue
        touched = any([
            ema10_v[i] > 0 and lows[i] <= ema10_v[i] * 1.025 and highs[i] >= ema10_v[i] * 0.975,
            ema20_v[i] > 0 and lows[i] <= ema20_v[i] * 1.025 and highs[i] >= ema20_v[i] * 0.975,
            not np.isnan(sma50[i]) and sma50[i] > 0 and lows[i] <= sma50[i] * 1.025 and highs[i] >= sma50[i] * 0.975,
        ])
        if touched:
            signals['MA Touch'] = (i, closes[i])
            break

    # ── Compose combos ──
    combos = {}

    # FULL_BACKTEST combos (mid-base)
    if 'SMA50 Bounce' in signals and 'Shakeout' in signals:
        combos['SMA50 Bounce+Shakeout'] = True
    elif 'SMA50 Bounce' in signals:
        combos['SMA50 Bounce'] = True

    if 'Shakeout' in signals and 'Upside Reversal' in signals:
        combos['Shakeout+Upside Reversal'] = True
    elif 'Upside Reversal' in signals:
        combos['Upside Reversal'] = True

    # SCANNER_UNIVERSE combos (breakout)
    if 'Pivot Breakout' in signals:
        if 'SMA50 Bounce' in signals:
            combos['Pivot Breakout+SMA50 Bounce'] = True
        elif 'MA Touch' in signals and 'Upside Reversal' in signals:
            combos['MA Touch+PB+Upside Reversal'] = True
        else:
            combos['Pivot Breakout'] = True

    # Raw signals too
    for sig_name, (bar, price) in signals.items():
        combos[f'sig_{sig_name}'] = True

    return combos, signals


def compute_combo_score(combos):
    """Score from active combo signals."""
    score = 0.0
    for cname, weight in COMBO_WEIGHTS.items():
        if combos.get(cname):
            score += weight
    return min(score, 10.0)


def main(args):
    t0 = time.time()
    scan_fn = load_patched_scanner()

    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    print(f"🔍 Scanning {len(files)} tickers for triple-signal watchlist...")
    print(f"   IBD: Flat Base / Consolidation, depth {args.min_depth}-{args.max_depth}%, "
          f"length {args.min_length}-{args.max_length} days")
    print(f"   TF:  SMA200+EMA10>EMA20+near 52W high+RSI>50")
    print(f"   BUY: Mid-base (SMA50+Shake, UpRev) + Breakout (PB+SMA50, MA+PB+UpRev)")

    results = []

    for f in files:
        ticker = Path(f).name.replace("_1d.parquet", "")
        if ticker in ("SPY", "QQQ", "IWM"):
            continue

        try:
            df = pd.read_parquet(f)
            if df.empty or len(df) < MIN_BARS:
                continue
            if df["Close"].iloc[-1] < MIN_PRICE or df["Volume"].tail(50).mean() < MIN_VOL_50:
                continue

            # ── IBD Pattern Check ──
            res = scan_fn(ticker, str(f))
            if not res or not res.get("history"):
                continue

            hist = res["history"]
            latest = hist[-1]

            p_on = latest.get("pOn", False)
            p_name = latest.get("pName", "")
            if not p_on or p_name not in ("Flat Base", "Consolidation"):
                continue

            b_depth = latest.get("bDepPct")
            b_length = latest.get("bCount")
            b_top = latest.get("bTop")
            b_low = latest.get("bLow")
            pivot = latest.get("boPivot") or b_top

            if b_depth is None or b_length is None:
                continue
            if not (args.min_depth <= b_depth <= args.max_depth):
                continue
            if not (args.min_length <= b_length <= args.max_length):
                continue

            # ── Price data ──
            closes = df["Close"].values
            highs = df["High"].values
            lows = df["Low"].values
            opens = df["Open"].values
            volumes = df["Volume"].values

            # ── Trend-Following Momentum ──
            sma200 = pd.Series(closes).rolling(200, min_periods=50).mean().values
            e10 = ema(pd.Series(closes), 10).values
            e20 = ema(pd.Series(closes), 20).values
            rsi_v = rsi_calc(closes).values
            tf_score, tf_flags = check_tf_momentum(closes, sma200, e10, e20, rsi_v, highs)

            # ── Combo Buy Signals ──
            sma20_vol = pd.Series(volumes).rolling(20, min_periods=5).mean().values
            # Approximate SMA50 and ATR14 from scanner data
            sma50 = pd.Series(closes).rolling(50, min_periods=10).mean().values
            atr14 = _calc_atr(highs, lows, closes, 14)

            combos, raw_sigs = detect_combo_signals(
                highs, lows, closes, opens,
                sma50, atr14,
                pivot, b_low, lookback=20)

            combo_score = compute_combo_score(combos)
            combo_count = sum(1 for c in COMBO_WEIGHTS if combos.get(c))
            sig_count = sum(1 for k in raw_sigs if k in ('SMA50 Bounce', 'Shakeout', 'Upside Reversal'))

            # ── Composite scoring ──
            ibd_score = 0.0
            if b_depth <= 15: ibd_score += 2.0
            elif b_depth <= 20: ibd_score += 1.0
            if 100 <= b_length <= 150: ibd_score += 1.5
            elif 150 < b_length <= 200: ibd_score += 0.5

            composite = ibd_score + tf_score + combo_score
            dist_pct = (closes[-1] - pivot) / pivot * 100.0 if pivot and pivot > 0 else None

            results.append({
                "ticker": ticker,
                "pattern": p_name,
                "depth": round(b_depth, 1),
                "length": int(b_length),
                "pivot": round(pivot, 2) if pivot else None,
                "close": round(closes[-1], 2),
                "dist_to_pivot": round(dist_pct, 1) if dist_pct else None,
                "ibd_score": round(ibd_score, 2),
                "tf_score": round(tf_score, 2),
                "combo_score": round(combo_score, 2),
                "composite": round(composite, 2),
                # TF flags
                "above_sma200": tf_flags.get("above_sma200", False),
                "ema_bullish": tf_flags.get("ema_bullish", False),
                "near_52w_high": tf_flags.get("near_52w_high", False),
                "pct_off_52w": tf_flags.get("pct_off_52w_high", None),
                "rsi": tf_flags.get("rsi", None),
                "rsi_bullish": tf_flags.get("rsi_bullish", False),
                "tf_flags_on": sum((
                    tf_flags.get("above_sma200", False),
                    tf_flags.get("ema_bullish", False),
                    tf_flags.get("near_52w_high", False),
                    tf_flags.get("rsi_bullish", False),
                )),
                # Combo flags — full_backtest (mid-base) + scanner_universe (breakout)
                "combo_SMA50_Shakeout": combos.get("SMA50 Bounce+Shakeout", False),
                "combo_SMA50": combos.get("SMA50 Bounce", False),
                "combo_Shakeout_Upside": combos.get("Shakeout+Upside Reversal", False),
                "combo_Upside": combos.get("Upside Reversal", False),
                "combo_PB_SMA50": combos.get("Pivot Breakout+SMA50 Bounce", False),
                "combo_MA_PB_UpRev": combos.get("MA Touch+PB+Upside Reversal", False),
                "combo_PB": combos.get("Pivot Breakout", False),
                "combo_count": combo_count,
                "sig_count": sig_count,
                "quality_filter": (b_depth is not None and b_depth <= 25 and b_length is not None and b_length <= 150),
            })

        except Exception:
            continue

    if not results:
        print("❌ No tickers found.")
        return

    df_out = pd.DataFrame(results).sort_values("composite", ascending=False).reset_index(drop=True)
    elapsed = time.time() - t0

    n_all = len(results)
    n_combo = (df_out["combo_count"] > 0).sum()
    n_best = (df_out["combo_SMA50_Shakeout"] & (df_out["tf_flags_on"] >= 3)).sum()
    n_quality = df_out["quality_filter"].sum()

    print(f"\n{'='*130}")
    print(f"🎯 UNIFIED WATCHLIST (IBD + TF + BUY COMBOS) — {elapsed:.0f}s")
    print(f"   Total: {n_all}  |  Any combo: {n_combo}  |  SMA50+Shakeout+TF≥3: {n_best}  |  Quality (≤25%,≤150d): {n_quality}")
    print(f"{'='*130}")

    HEADER = (f"{'Ticker':<8s} {'Pat':<16s} {'Depth':>7s} {'Len':>6s} {'Close':>8s} "
              f"{'Dist':>7s} {'IBD':>5s} {'TF':>5s} {'Buy':>5s} {'Comp':>6s} "
              f"{'TF Flags':>12s} {'RSI':>6s} {'%52W':>6s} {'Mid-Base':>18s} {'Breakout':>16s}")
    print(f"\n{HEADER}")
    print("-" * 130)

    for _, r in df_out.iterrows():
        flags_str = " ".join([
            "SMA" if r["above_sma200"] else "·",
            "EMA" if r["ema_bullish"] else "·",
            "52H" if r["near_52w_high"] else "·",
            "RSI" if r["rsi_bullish"] else "·",
        ])
        rsi_str = f"{r['rsi']:.0f}" if r["rsi"] else "-"
        p52_str = f"{r['pct_off_52w']:.0f}%" if r["pct_off_52w"] is not None else "-"

        combo_parts = []
        if r["combo_SMA50_Shakeout"]: combo_parts.append("SMA50+Shake")
        elif r["combo_SMA50"]: combo_parts.append("SMA50")
        if r["combo_Shakeout_Upside"]: combo_parts.append("Shake+Rev")
        elif r["combo_Upside"]: combo_parts.append("UpRev")
        midbase_str = ", ".join(combo_parts) if combo_parts else "·"

        bo_parts = []
        if r["combo_PB_SMA50"]: bo_parts.append("PB+SMA50")
        elif r["combo_MA_PB_UpRev"]: bo_parts.append("MA+PB+UpRev")
        elif r["combo_PB"]: bo_parts.append("PB only")
        breakout_str = ", ".join(bo_parts) if bo_parts else "·"

        print(f"{r['ticker']:<8s} {r['pattern']:<16s} {r['depth']:>6.1f}% {int(r['length']):>5d} "
              f"${r['close']:>7.2f} {r['dist_to_pivot']:>+6.1f}% "
              f"{r['ibd_score']:>5.1f} {r['tf_score']:>4.1f} {r['combo_score']:>4.1f} {r['composite']:>5.1f} "
              f"{flags_str:>12s} {rsi_str:>5s} {p52_str:>5s} {midbase_str:<18s} {breakout_str:<16s}")

    print("-" * 130)
    print(f"\nTF: SMA=Above SMA200 EMA=EMA10>EMA20 52H=Near 52W High RSI=RSI>50")
    print(f"Buy Mid-Base: SMA50+Shake SMA50 Shake+Rev UpRev | Breakout: PB+SMA50 MA+PB+UpRev PB only")

    csv_path = OUTPUT_DIR / "unified_watchlist.csv"
    df_out.to_csv(csv_path, index=False)
    print(f"\n💾 Watchlist saved to {csv_path} ({len(df_out)} tickers)")

    n_pb = (df_out["combo_PB_SMA50"] | df_out["combo_MA_PB_UpRev"] | df_out["combo_PB"]).sum()
    print(f"\n📊 WATCHLIST SUMMARY")
    print(f"   Pattern breakdown: {dict(df_out['pattern'].value_counts())}")
    print(f"   Avg composite: {df_out['composite'].mean():.1f}")
    print(f"   Mid-base combos: {n_combo}  |  Best (SMA50+Shakeout): {(df_out['combo_SMA50_Shakeout']).sum()}")
    print(f"   Breakout combos: {n_pb}  |  Best (PB+SMA50): {(df_out['combo_PB_SMA50']).sum()}")
    print(f"   Both engines firing: {((df_out['combo_SMA50_Shakeout'] | df_out['combo_SMA50'] | df_out['combo_Shakeout_Upside'] | df_out['combo_Upside']) & (df_out['combo_PB_SMA50'] | df_out['combo_MA_PB_UpRev'] | df_out['combo_PB'])).sum()}")
    print(f"   Quality filter (depth≤25% & len≤150d): {n_quality}  |  Golden+Quality: {(df_out['combo_SMA50_Shakeout'] & (df_out['tf_flags_on'] >= 3) & df_out['quality_filter']).sum()}")
    print(f"   SMA200+EMA+RISING (triple TF): {(df_out['above_sma200'] & df_out['ema_bullish'] & df_out['near_52w_high']).sum()}")


def _calc_atr(highs, lows, closes, length=14):
    n = len(closes)
    prev_close = np.roll(closes, 1); prev_close[0] = closes[0]
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    atr = np.zeros(n); atr[0] = tr[0]
    alpha = 1.0 / length
    for i in range(1, n): atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    return atr


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Unified IBD+TF+BuyCombo watchlist")
    ap.add_argument("--min-depth", type=float, default=OPT_DEPTH_MIN)
    ap.add_argument("--max-depth", type=float, default=OPT_DEPTH_MAX)
    ap.add_argument("--min-length", type=int, default=OPT_LEN_MIN)
    ap.add_argument("--max-length", type=int, default=OPT_LEN_MAX)
    args = ap.parse_args()
    main(args)
