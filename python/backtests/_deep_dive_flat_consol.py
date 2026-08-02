#!/usr/bin/env python3
"""Deep dive: Flat Base + Consolidation × Pivot Breakout+SMA50 Bounce × target_5r"""
import pandas as pd
import numpy as np

df = pd.read_csv(__import__('pathlib').Path(__file__).resolve().parent / 'scanner_universe_trades.csv')
s = df[
    (df['strategy'] == 'Pivot Breakout+SMA50 Bounce') &
    (df['exit_rule'] == 'target_5r') &
    (df['pattern'].isin(['Flat Base', 'Consolidation']))
]

print("=" * 95)
print("FLAT BASE + CONSOLIDATION → PIVOT BREAKOUT + SMA50 BOUNCE × TARGET 5:1 R:R")
print("=" * 95)
print(f"Total trades:     {len(s):,}")
print(f"Unique tickers:   {s['ticker'].nunique():,}")
print(f"Win rate:         {s['win'].mean()*100:.1f}%")
print(f"Avg return (raw): {s['ret_raw'].mean():+.2f}%")
print(f"Avg return (pos): {s['ret'].mean():+.2f}%")
print(f"Median return:    {s['ret'].median():+.2f}%")
print(f"Sharpe:           {s['ret'].mean()/s['ret'].std():.2f}" if s['ret'].std() > 0 else "")

# ── By pattern ──
print(f"\n{'='*95}")
print("BY PATTERN")
print(f"{'='*95}")
for pat in ['Flat Base', 'Consolidation']:
    p = s[s['pattern'] == pat]
    wins = p[p['win']]
    losses = p[~p['win']]
    print(f"\n--- {pat} ({len(p)} trades, {len(p[p['win']])}/{len(p[~p['win']])} W/L) ---")
    print(f"  Win rate:  {p['win'].mean()*100:.1f}%")
    print(f"  Avg ret:   {p['ret'].mean():+.2f}%  median: {p['ret'].median():+.2f}%")
    print(f"  Wins avg:  {wins['ret'].mean():+.2f}%  Losses avg: {losses['ret'].mean():+.2f}%")
    print(f"  Sharpe:    {p['ret'].mean()/p['ret'].std():.2f}" if p['ret'].std() > 0 else "")

# ── Base characteristics vs return ──
print(f"\n{'='*95}")
print("BASE CHARACTERISTICS → PERFORMANCE")
print(f"{'='*95}")

# Depth buckets
print(f"\n--- By Base Depth ---")
depth_bins = [0, 8, 12, 18, 25, 35, 50, 100]
depth_labels = ['0-8%', '8-12%', '12-18%', '18-25%', '25-35%', '35-50%', '50%+']
s['depth_bucket'] = pd.cut(s['depth'], bins=depth_bins, labels=depth_labels)
for bucket in depth_labels:
    b = s[s['depth_bucket'] == bucket]
    if len(b) < 5:
        continue
    print(f"  Depth {bucket:>8s}: n={len(b):>5,}  win={b['win'].mean()*100:>5.1f}%  avg_ret={b['ret'].mean():>+7.2f}%  median={b['ret'].median():>+7.2f}%")

# Length buckets
print(f"\n--- By Base Length (days) ---")
len_bins = [0, 20, 40, 65, 100, 130, 200, 500]
len_labels = ['<20', '20-40', '40-65', '65-100', '100-130', '130-200', '200+']
s['len_bucket'] = pd.cut(s['length'], bins=len_bins, labels=len_labels)
for bucket in len_labels:
    b = s[s['len_bucket'] == bucket]
    if len(b) < 5:
        continue
    print(f"  Len {bucket:>10s}: n={len(b):>5,}  win={b['win'].mean()*100:>5.1f}%  avg_ret={b['ret'].mean():>+7.2f}%  median={b['ret'].median():>+7.2f}%")

# Quality buckets
print(f"\n--- By Base Quality ---")
qual_bins = [0, 50, 60, 70, 80, 100]
qual_labels = ['<50', '50-60', '60-70', '70-80', '80+']
s['qual_bucket'] = pd.cut(s['base_quality'], bins=qual_bins, labels=qual_labels)
for bucket in qual_labels:
    b = s[s['qual_bucket'] == bucket]
    if len(b) < 5:
        continue
    print(f"  Q {bucket:>10s}: n={len(b):>5,}  win={b['win'].mean()*100:>5.1f}%  avg_ret={b['ret'].mean():>+7.2f}%  median={b['ret'].median():>+7.2f}%")

# ── Depth × Length heatmap ──
print(f"\n{'='*95}")
print("DEPTH × LENGTH HEATMAP (avg return %)")
print(f"{'='*95}")
print(f"{'Depth↓ / Len→':<16}", end='')
for l in len_labels:
    print(f" {l:>9}", end='')
print(f"\n{'-'*16}{'-'*10*len(len_labels)}")
for dl in depth_labels:
    row = s[s['depth_bucket'] == dl]
    print(f"  {dl:<14}", end='')
    for ll in len_labels:
        cell = row[row['len_bucket'] == ll]
        if len(cell) < 3:
            print(f" {'-':>9}", end='')
        else:
            print(f" {cell['win'].mean()*100:>5.1f}% {cell['ret'].mean():>+3.1f}", end='')
    print()

# ── Top tickers (recurring winners with >=3 trades) ──
print(f"\n{'='*95}")
print("TOP MULTI-TRADE TICKERS (≥3 trades)")
print(f"{'='*95}")
tickers_agg = s.groupby('ticker').agg(
    n=('ret', 'count'),
    win_pct=('win', 'mean'),
    avg_ret=('ret', 'mean'),
    med_ret=('ret', 'median'),
    total_ret=('ret', 'sum'),
).query('n >= 3').sort_values('avg_ret', ascending=False)

print(f"  {'Ticker':<8s} {'n':>4s} {'Win%':>7s} {'AvgRet':>8s} {'MedRet':>8s} {'TotalRet':>9s}  {'Patterns'}")
print(f"  {'-'*60}")
for t, r in tickers_agg.head(15).iterrows():
    # Get unique patterns for this ticker
    pats = ', '.join(s[s['ticker'] == t]['pattern'].unique())
    print(f"  {t:<8s} {int(r['n']):>4d} {r['win_pct']*100:>6.1f}% {r['avg_ret']:>+7.2f}% {r['med_ret']:>+7.2f}% {r['total_ret']:>+8.2f}%  {pats}")

# ── Worst tickers ──
print(f"\nWORST MULTI-TRADE TICKERS (≥3 trades)")
print(f"{'='*95}")
worst = tickers_agg.sort_values('avg_ret').head(10)
for t, r in worst.iterrows():
    pats = ', '.join(s[s['ticker'] == t]['pattern'].unique())
    print(f"  {t:<8s} {int(r['n']):>4d} {r['win_pct']*100:>6.1f}% {r['avg_ret']:>+7.2f}% {r['med_ret']:>+7.2f}% {r['total_ret']:>+8.2f}%  {pats}")

# ── Ticker concentration ──
print(f"\n{'='*95}")
print("TICKER CONCENTRATION")
print(f"{'='*95}")
ticker_counts = s['ticker'].value_counts()
for threshold, label in [(5, '5+'), (3, '3+'), (2, '2+'), (1, '1')]:
    cnt = (ticker_counts >= threshold).sum()
    trades = ticker_counts[ticker_counts >= threshold].sum()
    print(f"  Tickers with {label} trades: {cnt:>5,} ({cnt/s['ticker'].nunique()*100:.0f}%) — {trades:>6,} trades ({trades/len(s)*100:.0f}%)")
