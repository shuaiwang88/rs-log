#!/usr/bin/env python3
"""Cross-reference: IBD Pattern Strategy vs Trend-Following — find tickers that win in both."""
import pandas as pd
import numpy as np

ibd = pd.read_csv('python/backtests/scanner_universe_trades.csv')
tf = pd.read_csv('python/backtests/trend_following_results.csv')

# ── IBD: Pivot Breakout+SMA50 Bounce × target_5r, Flat Base + Consolidation only ──
ibd_top = ibd[
    (ibd['strategy'] == 'Pivot Breakout+SMA50 Bounce') &
    (ibd['exit_rule'] == 'target_5r') &
    (ibd['pattern'].isin(['Flat Base', 'Consolidation']))
]
ibd_agg = ibd_top.groupby('ticker').agg(
    ibd_trades=('ret', 'count'),
    ibd_win_pct=('win', 'mean'),
    ibd_avg_ret=('ret', 'mean'),
    ibd_total_ret=('ret', 'sum'),
).reset_index()

# ── Trend-following: best 3 realistic strategies ──
tf_best = tf[tf['strategy'].isin(['alt35_fractional_time', 'alt2_ema_crossover', 'alt5_close_confirmed'])]

# Per-ticker: best trend-following strategy for each ticker
tf_best_ticker = tf_best.loc[tf_best.groupby('ticker')['total_ret'].idxmax()][
    ['ticker', 'strategy', 'total_ret', 'win_rate', 'profit_factor', 'max_dd', 'trades']
].rename(columns={
    'strategy': 'tf_strategy', 'total_ret': 'tf_total_ret',
    'win_rate': 'tf_win_rate', 'profit_factor': 'tf_pf',
    'max_dd': 'tf_max_dd', 'trades': 'tf_trades'
})

# ── Merge ──
merged = ibd_agg.merge(tf_best_ticker, on='ticker', how='inner')

# Composite score: normalized blend of IBD avg ret + TF total ret
merged['ibd_z'] = (merged['ibd_avg_ret'] - merged['ibd_avg_ret'].mean()) / merged['ibd_avg_ret'].std()
merged['tf_z'] = (merged['tf_total_ret'] - merged['tf_total_ret'].mean()) / merged['tf_total_ret'].std()
merged['composite'] = merged['ibd_z'] * 0.5 + merged['tf_z'] * 0.5
merged = merged.sort_values('composite', ascending=False)

print("=" * 105)
print("CROSS-REFERENCE: Tickers Profitable in BOTH IBD Pattern + Trend-Following Strategies")
print("=" * 105)
print(f"IBD universe (Flat Base + Consolidation): {ibd_agg['ticker'].nunique():,} tickers")
print(f"TF universe (alt2/5/35):                 {tf_best_ticker['ticker'].nunique():,} tickers")
print(f"OVERLAP:                                {len(merged):,} tickers")
print(f"Profitable in BOTH:                     {(merged['ibd_avg_ret'] > 0).sum()} tickers → IBD-positive AND TF-positive")
print()

# Top 30 by composite
print(f"{'Ticker':<8s} {'IBD Tr':>6s} {'IBD Win%':>8s} {'IBD Avg':>8s} {'TF Strat':<24s} {'TF Ret':>8s} {'TF Win%':>8s} {'TF PF':>6s} {'TF MaxDD':>8s} {'Score':>7s}")
print("-" * 105)

for _, r in merged.head(30).iterrows():
    ibd_ret_str = f"{r['ibd_avg_ret']:+.1f}%"
    tf_ret_str = f"{r['tf_total_ret']:+.0f}%"
    tf_dd_str = f"{r['tf_max_dd']:.1f}%"
    print(f"{r['ticker']:<8s} {int(r['ibd_trades']):>6d} {r['ibd_win_pct']*100:>7.1f}% {ibd_ret_str:>8s} {r['tf_strategy']:<24s} {tf_ret_str:>8s} {r['tf_win_rate']:>7.1f}% {r['tf_pf']:>6.2f} {tf_dd_str:>8s} {r['composite']:>+6.2f}")

# ── Summary stats ──
print(f"\n{'='*105}")
print("SUMMARY STATS — Overlap Tickers")
print(f"{'='*105}")
print(f"  IBD avg return (overlap only):    {merged['ibd_avg_ret'].mean():+.2f}%")
print(f"  IBD win rate (overlap only):      {merged['ibd_win_pct'].mean()*100:.1f}%")
print(f"  TF avg total return (overlap):    {merged['tf_total_ret'].mean():+.1f}%")
print(f"  TF avg win rate (overlap):        {merged['tf_win_rate'].mean():.1f}%")
print(f"  TF avg profit factor (overlap):   {merged['tf_pf'].mean():.2f}")
print(f"  TF avg max drawdown (overlap):    {merged['tf_max_dd'].mean():.1f}%")

# ── Also show full IBD universe stats for comparison ──
print(f"\n  (Full IBD universe avg ret: {ibd_agg['ibd_avg_ret'].mean():+.2f}%, win: {ibd_agg['ibd_win_pct'].mean()*100:.1f}%)")
print(f"  (Full TF universe avg ret:   {tf_best_ticker['tf_total_ret'].mean():+.1f}%, win: {tf_best_ticker['tf_win_rate'].mean():.1f}%)")

# ── Count tickers profitable in IBD that also exist in TF ──
ibd_winners = set(ibd_agg[ibd_agg['ibd_avg_ret'] > 0]['ticker'])
tf_winners = set(tf_best_ticker[tf_best_ticker['tf_total_ret'] > 0]['ticker'])
both_winners = ibd_winners & tf_winners
print(f"\n  Tickers profitable in IBD:         {len(ibd_winners):,}")
print(f"  Tickers profitable in TF:          {len(tf_winners):,}")
print(f"  Winners in BOTH:                   {len(both_winners):,} ({len(both_winners)/max(len(ibd_winners & set(merged['ticker'])),1)*100:.0f}% of overlap)")
