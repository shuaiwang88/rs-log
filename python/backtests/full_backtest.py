#!/usr/bin/env python3
"""
Full Backtest — All Ticker Cache Tickers
========================================
Scans every ticker in ticker_cache/ with filters (price > $12, avg vol > 500K),
runs OUR pattern engine (python/tv_pattern_scanner.py — the drw_pattern.pine port,
same patterns as the 📐 TV Pattern tab), tests all buy strategies + exit rules, and
generates a comprehensive HTML report.

Buy Strategies (8 core + Composite + Any) — the engine's per-bar signals:
  1. Pivot Breakout       — the scanner-confirmed breakout bar above base pivot
  2. Upside Reversal      — wide-range up bar within base
  3. Shakeout near Pivot  — undercut swing low then reclaim
  4. Volume Dry-Up        — volume < 55% of its 50d avg near pivot
  5. MA Touch             — touches EMA10/21/34
  6. Pocket Pivot         — up day vol > max down-day vol in 10 bars
  7. RS New High          — RS makes new high within base
  8. SMA50 Bounce         — dips near SMA50 then reclaims

New in this round: every trade carries its SPY market regime at entry, % vs SPY 200-day,
price bucket, base shape and acc/dis ratios; --spy-regime / --max-price apply the filters
the history backtest found (see tv_pattern_history_backtest.py).

Exit Rules:
  - Stop-Loss: base low
  - Trailing Stop: ATR-based (2x, 3x ATR)
  - Time Stop: exit after 20/40/60 bars
  - Profit Target: R:R of 2:1, 3:1, 5:1

Risk Management:
  - Position sizing: quality-based (0.25x–1.0x)
  - Risk/Reward ratio tracking
  - Max drawdown per trade
"""

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import combinations
import glob
import time
import warnings
warnings.filterwarnings("ignore")

from tv_engine import (extract_bases, pat_groups, price_bucket, regime_arrays,
                       regime_label, scan_record, ticker_signals,
                       detect_buy_signals as _engine_detect_buy_signals)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = Path(__file__).resolve().parent

# ── Filters ──
MIN_PRICE = 12.0
MIN_AVG_VOL_50 = 500_000

# ── Strategy lists ──
BUY_STRATEGIES = [
    'Pivot Breakout', 'Upside Reversal', 'Shakeout', 'Volume Dry-Up',
    'MA Touch', 'Pocket Pivot', 'RS New High', 'SMA50 Bounce'
]
EXIT_RULES = ['stop_loss', 'trail_2atr', 'trail_3atr', 'time_20', 'time_40',
              'time_60', 'target_2r', 'target_3r', 'target_5r']


# ══════════════════════════════════════════════════════════════════════════════
# Technical indicators (reused from backtest_base_quality.py)
# ══════════════════════════════════════════════════════════════════════════════

def calculate_atr(highs, lows, closes, length=14):
    n = len(closes)
    if n == 0:
        return np.zeros(0)
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    alpha = 1.0 / length
    atr = np.zeros(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    return atr


def find_pivots(highs, lows, left=5, right=5):
    n = len(highs)
    ph, pl = {}, {}
    for i in range(left, n - right):
        if all(highs[j] < highs[i] for j in range(i - left, i + right + 1) if j != i):
            ph[i] = highs[i]
        if all(lows[j] > lows[i] for j in range(i - left, i + right + 1) if j != i):
            pl[i] = lows[i]
    return ph, pl


# ══════════════════════════════════════════════════════════════════════════════
# Pattern detection — OUR scanner (tv_pattern_scanner.py / drw_pattern.pine port),
# via the shared tv_engine.
# ══════════════════════════════════════════════════════════════════════════════

def scan_ticker_for_bases(df, spy_close_series=None, ticker="", fpath=""):
    """Detect all base patterns in a ticker's history using OUR pattern engine.

    Returns (bases, arrays_tuple, sig):
      bases        — list of base events {start_bar, end_bar, bTop, bLow, bDepPct,
                     bCount, pattern_name, shape, raw_pattern, break_bar, pivot_price,
                     acc_days, dis_days, neu_days};
      arrays_tuple — (highs, lows, closes, opens, volumes, ema10, ema20, sma50,
                     sma20_vol, atr14, rs_raw) over the prepared frame;
      sig          — per-bar signal dict from tv_engine (fed to detect_buy_signals).
    """
    rec, df = scan_record(ticker, str(fpath), spy_close_series, df)
    if rec is None:
        return [], None, None
    n = len(df)
    bases = []
    for b in extract_bases(rec, n):
        bases.append({
            "start_bar": b["start"], "end_bar": b["end"],
            "bTop": b["bTop"], "bLow": b["bLow"],
            "bDepPct": b["bDepPct"], "bCount": b["bCount"],
            "pattern_name": b["pattern"], "raw_pattern": b["raw_pattern"],
            "shape": b["shape"] or "",
            "break_bar": b["bo_bar"] if b["bo_bar"] is not None else b["end"],
            "pivot_price": b["pivot"], "rs_count": 0,
            "acc_days": b["acc_days"], "dis_days": b["dis_days"],
            "neu_days": b["neu_days"],
        })
    if not bases:
        return bases, None, None
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)
    opens = df["Open"].to_numpy(dtype=float)
    volumes = df["Volume"].to_numpy(dtype=float)
    ema10 = pd.Series(closes).ewm(span=10, adjust=False).mean().values
    ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean().values
    sma20_vol = pd.Series(volumes).rolling(20, min_periods=5).mean().values
    atr14 = calculate_atr(highs, lows, closes, 14)
    rs_raw = closes.copy()
    if spy_close_series is not None and not spy_close_series.empty:
        aligned_spy = spy_close_series.reindex(df.index).ffill().bfill().values
        if len(aligned_spy) == n and np.all(aligned_spy > 0):
            rs_raw = closes * 7.0 * 1000.0 / aligned_spy
    sig = ticker_signals(df, spy_close_series)
    if sig is None:
        return bases, None, None
    return bases, (highs, lows, closes, opens, volumes, ema10, ema20,
                   sig["sma50"], sma20_vol, atr14, rs_raw), sig


# ══════════════════════════════════════════════════════════════════════════════
# Buy signal detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_buy_signals(sig, highs, lows, closes, opens, pivot_price, bLow,
                       search_start, search_end, bo_bar):
    """Detect all buy strategies within a base window — OUR engine's signal book
    (the same 8-strategy definitions the scanner_universe backtest uses)."""
    return _engine_detect_buy_signals(sig, highs, lows, closes, opens, pivot_price,
                                      bLow, search_start, search_end, bo_bar)


# ══════════════════════════════════════════════════════════════════════════════
# Exit rules
# ══════════════════════════════════════════════════════════════════════════════

def apply_exit_rules(highs, lows, closes, signal_bar, entry_price, base_low, atr):
    """Apply multiple exit rules and return the exit that triggers first."""
    n = len(closes)
    results = {}

    # Stop-loss at base low
    for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
        if lows[bar] <= base_low:
            ret = (base_low - entry_price) / entry_price * 100.0
            results['stop_loss'] = {'exit_bar': bar, 'exit_price': base_low, 'ret': ret}
            break
    else:
        ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
        results['stop_loss'] = {'exit_bar': min(signal_bar + 60, n - 1),
                                'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    # Trailing stop (2x ATR)
    highest_since = entry_price
    for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
        highest_since = max(highest_since, highs[bar])
        trail = highest_since - 2 * atr[bar] if bar < len(atr) else highest_since * 0.92
        if lows[bar] <= trail:
            ret = (trail - entry_price) / entry_price * 100.0
            results['trail_2atr'] = {'exit_bar': bar, 'exit_price': trail, 'ret': ret}
            break
    else:
        ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
        results['trail_2atr'] = {'exit_bar': min(signal_bar + 60, n - 1),
                                 'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    # Trailing stop (3x ATR)
    highest_since = entry_price
    for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
        highest_since = max(highest_since, highs[bar])
        trail = highest_since - 3 * atr[bar] if bar < len(atr) else highest_since * 0.88
        if lows[bar] <= trail:
            ret = (trail - entry_price) / entry_price * 100.0
            results['trail_3atr'] = {'exit_bar': bar, 'exit_price': trail, 'ret': ret}
            break
    else:
        ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
        results['trail_3atr'] = {'exit_bar': min(signal_bar + 60, n - 1),
                                 'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    # Time stops
    for t_bars, key in [(20, 'time_20'), (40, 'time_40'), (60, 'time_60')]:
        exit_bar = min(signal_bar + t_bars, n - 1)
        ret = (closes[exit_bar] - entry_price) / entry_price * 100.0
        results[key] = {'exit_bar': exit_bar, 'exit_price': closes[exit_bar], 'ret': ret}

    # Profit targets (R:R ratios based on risk = entry - base_low)
    risk = entry_price - base_low
    if risk > 0:
        for rr, key in [(2, 'target_2r'), (3, 'target_3r'), (5, 'target_5r')]:
            target_price = entry_price + risk * rr
            for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
                if highs[bar] >= target_price:
                    ret = (target_price - entry_price) / entry_price * 100.0
                    results[key] = {'exit_bar': bar, 'exit_price': target_price, 'ret': ret}
                    break
            else:
                ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
                results[key] = {'exit_bar': min(signal_bar + 60, n - 1),
                                'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    return results


# ══════════════════════════════════════════════════════════════════════════════
# Base quality scoring
# ══════════════════════════════════════════════════════════════════════════════

def calc_base_quality(bDepPct, bCount):
    """Simplified base quality score (0-100)."""
    score = 50.0
    if bDepPct and 15 <= bDepPct <= 35:
        score += 15
    elif bDepPct and 10 <= bDepPct <= 45:
        score += 8
    if 20 <= bCount <= 150:
        score += 15
    elif 15 <= bCount <= 200:
        score += 8
    if bDepPct and 18.1 <= bDepPct <= 50.0:
        score += 10  # deeper bases get bonus
    return min(100, max(0, score))


def pos_size(quality):
    if quality >= 80: return 1.0
    elif quality >= 60: return 0.75
    elif quality >= 40: return 0.50
    elif quality >= 20: return 0.35
    return 0.25


# ══════════════════════════════════════════════════════════════════════════════
# Main backtest
# ══════════════════════════════════════════════════════════════════════════════

def run_full_backtest(args=None):
    start_time = time.time()

    spy_regime = getattr(args, "spy_regime", "all")
    max_price = getattr(args, "max_price", 0.0)
    max_tickers = getattr(args, "max_tickers", 0)

    # Load SPY
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    spy_close = None
    if spy_path.exists():
        try:
            spy_df = pd.read_parquet(spy_path)
            spy_close = spy_df['Close']
            print(f"✅ Loaded SPY data ({len(spy_df)} bars)")
        except Exception:
            pass

    # Find all ticker files
    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    print(f"📂 Found {len(files)} ticker files")

    # ── Filter tickers ──
    qualified = []
    for f in files:
        ticker = Path(f).name.replace("_1d.parquet", "")
        if ticker in ("SPY", "QQQ", "IWM"):
            continue
        try:
            df = pd.read_parquet(f)
            if df.empty or len(df) < 100:
                continue
            last_close = df['Close'].iloc[-1]
            avg_vol = df['Volume'].tail(50).mean()
            if last_close >= MIN_PRICE and avg_vol >= MIN_AVG_VOL_50:
                qualified.append((ticker, f, last_close, avg_vol))
        except Exception:
            continue
    if max_tickers:
        qualified = qualified[:max_tickers]

    print(f"✅ {len(qualified)} tickers pass filters (price > ${MIN_PRICE}, vol > {MIN_AVG_VOL_50:,})")
    print(f"   Scanner: tv_pattern_scanner.py (our drw_pattern.pine port, via tv_engine)")
    if spy_regime != "all":
        print(f"   Market regime: {spy_regime} only")
    if max_price > 0:
        print(f"   Max pivot price: ${max_price:,.0f}")

    all_trades = []
    bases_total = 0
    tickers_processed = 0

    for ticker, fpath, lc, av in qualified:
        try:
            df = pd.read_parquet(fpath)
            if df.empty:
                continue
            bases, arrays, sig = scan_ticker_for_bases(df, spy_close, ticker, fpath)
            if not bases or arrays is None or sig is None:
                continue
            highs, lows, closes, opens, volumes, ema10, ema20, sma50, sma20_vol, atr14, rs_raw = arrays
            reg = regime_arrays(spy_close, df)

            bases_total += len(bases)
            tickers_processed += 1

            for base in bases:
                bq = calc_base_quality(base['bDepPct'], base['bCount'])
                ps = pos_size(bq)
                ctx = {
                    "pattern": base['pattern_name'], "raw_pattern": base['raw_pattern'],
                    "shape": base.get('shape') or "",
                    "pat_groups": "|".join(sorted(pat_groups({
                        "pattern": base['pattern_name'], "raw_pattern": base['raw_pattern'],
                        "bDepPct": base['bDepPct'],
                    }))),
                    "acc_ratio": (round(base['acc_days'] / base['bCount'], 3)
                                   if base['bCount'] else None),
                    "dis_ratio": (round(base['dis_days'] / base['bCount'], 3)
                                   if base['bCount'] else None),
                    "neu_ratio": (round(base['neu_days'] / base['bCount'], 3)
                                   if base['bCount'] else None),
                }

                # Search window: from base start to break bar + 5
                search_start = max(0, base['start_bar'])
                search_end = min(base['break_bar'] + 5, len(closes) - 1)

                buy_signals = detect_buy_signals(
                    sig, highs, lows, closes, opens,
                    base['pivot_price'], base['bLow'],
                    search_start, search_end, base['break_bar'])

                if not buy_signals:
                    continue

                def _ctx_cols(entry_bar, entry_price):
                    cols = {
                        "shape": ctx["shape"], "raw_pattern": ctx["raw_pattern"],
                        "acc_ratio": ctx["acc_ratio"], "dis_ratio": ctx["dis_ratio"],
                        "neu_ratio": ctx["neu_ratio"], "pat_groups": ctx["pat_groups"],
                        "price_bucket": price_bucket(entry_price),
                    }
                    if reg is not None and entry_bar < len(reg["above200"]):
                        ab = bool(reg["above200"][entry_bar])
                        bl = bool(reg["bull"][entry_bar])
                        s200 = reg["s200"][entry_bar]
                        spy = reg["spy"][entry_bar]
                        cols["spy_regime"] = regime_label(ab, bl)
                        cols["spy_above200"] = ab
                        cols["spy_bull"] = bl
                        cols["spy_vs_200_pct"] = (
                            round((spy / s200 - 1.0) * 100, 1)
                            if (np.isfinite(s200) and s200 > 0) else None)
                    else:
                        cols.update({"spy_regime": "unknown", "spy_above200": None,
                                     "spy_bull": None, "spy_vs_200_pct": None})
                    return cols

                # Composite Score
                sig_names_real = {k: v for k, v in buy_signals.items()
                                  if k not in ('Any Signal',)}
                composite_score = min(100, bq * 0.3 + len(sig_names_real) * 10)
                if composite_score >= 30 and sig_names_real:
                    best = max(sig_names_real.keys(),
                               key=lambda s: (15 if s in ('Pivot Breakout', 'Pocket Pivot', 'Shakeout', 'RS New High')
                                              else 10 if s in ('Upside Reversal', 'SMA50 Bounce')
                                              else 8 if s == 'Volume Dry-Up' else 5))
                    buy_signals['Composite Score'] = sig_names_real[best]

                for strategy, (sig_bar, entry_price) in buy_signals.items():
                    exit_results = apply_exit_rules(
                        highs, lows, closes, sig_bar, entry_price, base['bLow'], atr14)

                    for exit_rule, exit_data in exit_results.items():
                        ret_raw = exit_data['ret']
                        ret = ret_raw * ps  # position-size the return
                        risk = entry_price - base['bLow']
                        rr = abs(ret_raw / (risk / entry_price * 100)) if risk > 0 else 0

                        all_trades.append({
                            'ticker': ticker,
                            'pattern': base['pattern_name'],
                            'depth': base['bDepPct'],
                            'length': base['bCount'],
                            'pivot_price': base['pivot_price'],
                            'base_low': base['bLow'],
                            'strategy': strategy,
                            'exit_rule': exit_rule,
                            'entry_bar': sig_bar,
                            'entry_price': entry_price,
                            'exit_bar': exit_data['exit_bar'],
                            'exit_price': exit_data['exit_price'],
                            'ret': ret,
                            'ret_raw': ret_raw,
                            'base_quality': bq,
                            'pos_size': ps,
                            'risk_amount': risk,
                            'rr_ratio': rr,
                            'win': ret > 0,
                            'last_close': lc,
                            'avg_vol_50d': av,
                            **_ctx_cols(sig_bar, entry_price),
                        })

                # Generate ALL combinations (pairs through all-N).
                # Each combo enters on the EARLIEST bar among its signals — the idea
                # is that multiple signals firing close together is higher-confidence.
                real_sigs = {k: v for k, v in buy_signals.items()
                             if k not in ('Any Signal', 'Composite Score')}
                sig_names = sorted(real_sigs.keys())
                for combo_size in range(2, len(sig_names) + 1):
                    for combo in combinations(sig_names, combo_size):
                        bars = [real_sigs[s][0] for s in combo]
                        prices = [real_sigs[s][1] for s in combo]
                        ei = bars.index(min(bars))
                        combo_name = '+'.join(combo)
                        exit_results = apply_exit_rules(
                            highs, lows, closes, bars[ei], prices[ei], base['bLow'], atr14)
                        for exit_rule, exit_data in exit_results.items():
                            ret_raw = exit_data['ret']
                            ret = ret_raw * ps
                            risk = prices[ei] - base['bLow']
                            rr = abs(ret_raw / (risk / prices[ei] * 100)) if risk > 0 else 0
                            all_trades.append({
                                'ticker': ticker, 'pattern': base['pattern_name'],
                                'depth': base['bDepPct'], 'length': base['bCount'],
                                'pivot_price': base['pivot_price'], 'base_low': base['bLow'],
                                'strategy': combo_name, 'exit_rule': exit_rule,
                                'entry_bar': bars[ei], 'entry_price': prices[ei],
                                'exit_bar': exit_data['exit_bar'], 'exit_price': exit_data['exit_price'],
                                'ret': ret, 'ret_raw': ret_raw,
                                'base_quality': bq, 'pos_size': ps,
                                'risk_amount': risk, 'rr_ratio': rr, 'win': ret > 0,
                                'last_close': lc, 'avg_vol_50d': av,
                                **_ctx_cols(bars[ei], prices[ei]),
                            })

        except Exception:
            continue

    if not all_trades:
        print("❌ No trades generated")
        return

    df = pd.DataFrame(all_trades)
    elapsed = time.time() - start_time

    # ── Findings filters (tv_pattern_history_backtest.py): market regime + price cap ──
    if spy_regime == "above200":
        before = len(df)
        df = df[df["spy_above200"].fillna(True)]
        print(f"   SPY regime 'above200': kept {len(df):,} of {before:,} trades")
    elif spy_regime == "bull":
        before = len(df)
        df = df[df["spy_bull"].fillna(True)]
        print(f"   SPY regime 'bull': kept {len(df):,} of {before:,} trades")
    if max_price > 0:
        before = len(df)
        df = df[df["pivot_price"] <= max_price]
        print(f"   Max pivot price ${max_price:,.0f}: kept {len(df):,} of {before:,} trades")
    if df.empty:
        print("❌ No trades after filters")
        return

    print(f"\n{'='*100}")
    print(f"📊 FULL BACKTEST COMPLETE — {elapsed:.0f}s")
    print(f"   Tickers processed: {tickers_processed}")
    print(f"   Bases detected: {bases_total}")
    print(f"   Total trades (buy×exit×combo): {len(df):,}")
    print(f"{'='*100}\n")

    # ── Save results ──
    results_path = OUTPUT_DIR / "full_backtest_results.csv"
    df.to_csv(results_path, index=False)
    print(f"💾 Full results saved to {results_path} ({results_path.stat().st_size:,} bytes)")

    # ── Summary by buy strategy × exit rule (all strategies including combos) ──
    all_buy_sigs = sorted(df['strategy'].unique())
    print(f"\n📊 BUY STRATEGY × EXIT RULE SUMMARY (top 40 by Sharpe)")
    print(f"{'='*100}")
    print(f"{'Buy Strategy':<42} {'Exit Rule':<14} {'Trades':>7} {'Win%':>7} {'Avg Ret':>8} {'Avg R:R':>8} {'Sharpe':>8}")
    print(f"{'-'*100}")

    summary_rows = []
    for buy_s in all_buy_sigs:
        for exit_r in EXIT_RULES:
            sdf = df[(df['strategy'] == buy_s) & (df['exit_rule'] == exit_r)]
            n = len(sdf)
            if n < 5:
                continue
            win_pct = sdf['win'].mean() * 100
            avg_ret = sdf['ret'].mean()
            avg_rr = sdf['rr_ratio'].mean()
            sharpe = sdf['ret'].mean() / sdf['ret'].std() if sdf['ret'].std() > 0 else 0
            summary_rows.append({
                'buy_strategy': buy_s, 'exit_rule': exit_r, 'trades': n,
                'win_pct': round(win_pct, 1), 'avg_ret': round(avg_ret, 2),
                'avg_rr': round(avg_rr, 2), 'sharpe': round(sharpe, 2),
            })

    summary_df = pd.DataFrame(summary_rows)
    summary_df = summary_df.sort_values('sharpe', ascending=False)
    summary_path = OUTPUT_DIR / "full_backtest_summary.csv"
    summary_df.to_csv(summary_path, index=False)

    # Print top 40
    for _, r in summary_df.head(40).iterrows():
        print(f"{r['buy_strategy']:<42} {r['exit_rule']:<14} {int(r['trades']):>7,} {r['win_pct']:>6.1f}% {r['avg_ret']:>7.2f}% {r['avg_rr']:>7.2f} {r['sharpe']:>7.2f}")

    print(f"\n💾 Summary saved to {summary_path} ({len(summary_df)} rows)")

    # ── Findings breakdowns (tv_pattern_history_backtest.py) ──
    if "spy_regime" in df.columns:
        print("\n📈 BY MARKET REGIME AT ENTRY")
        for rl in ["Bull", "Mixed", "Bear", "unknown"]:
            s = df[df["spy_regime"] == rl]
            if len(s) >= 5:
                print(f"   {rl:<8s}: {len(s):>8,} trades  win {(s['win'].mean() * 100):>5.1f}%  "
                      f"avg {s['ret'].mean():>+6.2f}%")
    if "price_bucket" in df.columns:
        print("\n💰 BY PRICE BUCKET")
        order = ["<$10", "$10-25", "$25-50", "$50-100", "$100-250", "$250+"]
        for bk in [b for b in order if b in set(df["price_bucket"])]:
            s = df[df["price_bucket"] == bk]
            if len(s) >= 5:
                print(f"   {bk:<9s}: {len(s):>8,} trades  win {(s['win'].mean() * 100):>5.1f}%  "
                      f"avg {s['ret'].mean():>+6.2f}%")

    return df, summary_df


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Full backtest on our TV pattern engine")
    ap.add_argument("--max-tickers", type=int, default=0)
    ap.add_argument("--spy-regime", choices=["all", "above200", "bull"], default="all",
                    help="keep only trades entered while SPY held its 200-day (above200) or "
                         "both 50 & 200-day (bull); all = no filter")
    ap.add_argument("--max-price", type=float, default=0.0,
                    help="drop trades whose pivot price is above this (0 = no cap)")
    run_full_backtest(ap.parse_args())
