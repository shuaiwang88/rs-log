#!/usr/bin/env python3
"""Deep dive: SMA50 Bounce+Shakeout × target_2r from full_backtest_results.csv"""
import pandas as pd, numpy as np

df = pd.read_csv(__import__('pathlib').Path(__file__).resolve().parent / 'full_backtest_results.csv')
s = df[(df['strategy'] == 'SMA50 Bounce+Shakeout') & (df['exit_rule'] == 'target_2r')]

print("=" * 95)
print("SMA50 BOUNCE + SHAKEOUT × TARGET 2:1 R:R — DEEP DIVE")
print("=" * 95)
print(f"Total trades:     {len(s):,}")
print(f"Unique tickers:   {s['ticker'].nunique():,}")
print(f"Win rate:         {s['win'].mean()*100:.1f}%")
print(f"Avg return (raw): {s['ret_raw'].mean():+.2f}%")
print(f"Avg return (pos): {s['ret'].mean():+.2f}%")
print(f"Median return:    {s['ret'].median():+.2f}%")
print(f"Std dev:          {s['ret'].std():.2f}%")
print(f"Sharpe:           {s['ret'].mean()/s['ret'].std():.2f}" if s['ret'].std()>0 else "")

wins = s[s['win']]; losses = s[~s['win']]
print(f"\n--- Win/Loss ---")
print(f"Wins:   {len(wins):,} ({len(wins)/len(s)*100:.1f}%)  avg: {wins['ret'].mean():+.2f}%  median: {wins['ret'].median():+.2f}%")
print(f"Losses: {len(losses):,} ({len(losses)/len(s)*100:.1f}%)  avg: {losses['ret'].mean():+.2f}%  median: {losses['ret'].median():+.2f}%")

# Return distribution
print(f"\n--- Return Distribution ---")
bins = [-100, -20, -10, -5, -2, 0, 2, 5, 10, 20, 50, 1000]
labels = ['<-20%','-20..-10%','-10..-5%','-5..-2%','-2..0%','0..2%','2..5%','5..10%','10..20%','20..50%','>50%']
s['bucket'] = pd.cut(s['ret'], bins=bins, labels=labels)
for b in labels:
    cnt = (s['bucket'] == b).sum()
    bar = '█' * max(1, cnt // 20)
    print(f"  {b:>14s}: {cnt:>6,}  {bar}")

# Per-pattern
print(f"\n--- Per-Pattern ---")
for pat in sorted(s['pattern'].unique()):
    p = s[s['pattern'] == pat]
    print(f"  {pat:<16s}: n={len(p):>6,}  win={p['win'].mean()*100:>5.1f}%  avg_ret={p['ret'].mean():>+7.2f}%  median={p['ret'].median():>+7.2f}%  sharpe={p['ret'].mean()/p['ret'].std():.2f}" if p['ret'].std()>0 else f"  {pat:<16s}: n={len(p):>6,}  win={p['win'].mean()*100:>5.1f}%  avg_ret={p['ret'].mean():>+7.2f}%")

# Depth x Length
depth_bins = [0, 8, 12, 18, 25, 35, 50, 100]
depth_labels = ['0-8%','8-12%','12-18%','18-25%','25-35%','35-50%','50%+']
s['db'] = pd.cut(s['depth'], bins=depth_bins, labels=depth_labels)
len_bins = [0, 20, 40, 65, 100, 130, 200, 500]
len_labels = ['<20','20-40','40-65','65-100','100-130','130-200','200+']
s['lb'] = pd.cut(s['length'], bins=len_bins, labels=len_labels)

print(f"\n--- By Base Depth ---")
for b in depth_labels:
    d = s[s['db'] == b]
    if len(d) < 5: continue
    print(f"  {b:>8s}: n={len(d):>5,}  win={d['win'].mean()*100:>5.1f}%  avg_ret={d['ret'].mean():>+7.2f}%  median={d['ret'].median():>+7.2f}%")

print(f"\n--- By Base Length ---")
for b in len_labels:
    d = s[s['lb'] == b]
    if len(d) < 5: continue
    print(f"  {b:>10s}: n={len(d):>5,}  win={d['win'].mean()*100:>5.1f}%  avg_ret={d['ret'].mean():>+7.2f}%  median={d['ret'].median():>+7.2f}%")

# Depth x Length heatmap (win%)
print(f"\n--- Depth × Length Heatmap (win%) ---")
print(f"{'Depth↓':<10}", end='')
for ll in len_labels: print(f" {ll:>9}", end='')
print()
for dl in depth_labels:
    row = s[s['db'] == dl]
    print(f"  {dl:<8}", end='')
    for ll in len_labels:
        cell = row[row['lb'] == ll]
        if len(cell) < 3: print(f" {'-':>9}", end='')
        else: print(f" {cell['win'].mean()*100:>5.1f}%{cell['ret'].mean():>+4.1f}", end='')
    print()

# Top/bottom tickers (≥3 trades)
print(f"\n--- Top Tickers (≥3 trades) ---")
top = s.groupby('ticker').agg(n=('ret','count'),win=('win','mean'),avg=('ret','mean'),med=('ret','median'),total=('ret','sum')).query('n>=3').sort_values('avg',ascending=False)
for t,r in top.head(12).iterrows():
    print(f"  {t:<8s} n={int(r['n']):>2d}  win={r['win']*100:>5.1f}%  avg={r['avg']:>+7.2f}%  median={r['med']:>+7.2f}%  total={r['total']:>+8.2f}%")

print(f"\n--- Worst Tickers (≥3 trades) ---")
bot = top.sort_values('avg').head(8)
for t,r in bot.iterrows():
    print(f"  {t:<8s} n={int(r['n']):>2d}  win={r['win']*100:>5.1f}%  avg={r['avg']:>+7.2f}%  median={r['med']:>+7.2f}%  total={r['total']:>+8.2f}%")
