#!/usr/bin/env python3
"""Deep dive: Pivot Breakout+SMA50 Bounce × target_5r"""
import pandas as pd
import numpy as np

df = pd.read_csv(__import__('pathlib').Path(__file__).resolve().parent / 'scanner_universe_trades.csv')
s = df[(df['strategy'] == 'Pivot Breakout+SMA50 Bounce') & (df['exit_rule'] == 'target_5r')]

print("=" * 90)
print("PIVOT BREAKOUT + SMA50 BOUNCE × TARGET 5:1 R:R — DEEP DIVE")
print("=" * 90)
print(f"Total trades:     {len(s):,}")
print(f"Unique tickers:   {s['ticker'].nunique():,}")
print(f"Win rate:         {s['win'].mean()*100:.1f}%")
print(f"Avg return (raw): {s['ret_raw'].mean():+.2f}%")
print(f"Avg return (pos): {s['ret'].mean():+.2f}%")
print(f"Median return:    {s['ret'].median():+.2f}%")
print(f"Std dev:          {s['ret'].std():.2f}%")
print(f"Max return:       {s['ret'].max():+.2f}%")
print(f"Min return:       {s['ret'].min():+.2f}%")
print(f"Avg R:R ratio:    {s['rr_ratio'].mean():.2f}")
print(f"Median R:R:       {s['rr_ratio'].median():.2f}")
print(f"Avg base quality: {s['base_quality'].mean():.1f}")

# Win/Loss distribution
wins = s[s['win']]
losses = s[~s['win']]
print(f"\n--- Win/Loss Distribution ---")
print(f"Wins:   {len(wins):,} ({len(wins)/len(s)*100:.1f}%)  avg: {wins['ret'].mean():+.2f}%  median: {wins['ret'].median():+.2f}%")
print(f"Losses: {len(losses):,} ({len(losses)/len(s)*100:.1f}%)  avg: {losses['ret'].mean():+.2f}%  median: {losses['ret'].median():+.2f}%")

# Ret distribution buckets
print(f"\n--- Return Distribution ---")
bins = [-100, -20, -10, -5, -2, 0, 2, 5, 10, 20, 50, 100, 1000]
labels = ['<-20%', '-20..-10%', '-10..-5%', '-5..-2%', '-2..0%', '0..2%', '2..5%', '5..10%', '10..20%', '20..50%', '50..100%', '>100%']
s['bucket'] = pd.cut(s['ret'], bins=bins, labels=labels)
for bucket in labels:
    cnt = (s['bucket'] == bucket).sum()
    bar = '█' * (cnt // 20)
    print(f"  {bucket:>14s}: {cnt:>6,}  {bar}")

# Per-pattern breakdown
print(f"\n--- Per-Pattern Breakdown ---")
for pat in sorted(s['pattern'].unique()):
    p = s[s['pattern'] == pat]
    print(f"  {pat:<16s}: trades={len(p):>6,}  win={p['win'].mean()*100:>5.1f}%  avg_ret={p['ret'].mean():>+7.2f}%  median={p['ret'].median():>+7.2f}%  sharpe={p['ret'].mean()/p['ret'].std() if p['ret'].std()>0 else 0:>5.2f}")

# Max drawdown (equity curve simulation)
print(f"\n--- Equity Curve (cumulative raw returns) ---")
s_sorted = s.sort_values('entry_bar')
cumret = (1 + s_sorted['ret_raw'].values / 100).cumprod()
peak = np.maximum.accumulate(cumret)
dd = (cumret - peak) / peak * 100
final = cumret[-1] if len(cumret) > 0 else 1.0
print(f"Starting equity:   1.00")
print(f"Final equity:      {final:.2f}  ({'+' if final>=1 else ''}{(final-1)*100:.1f}%)")
print(f"Peak equity:       {peak.max():.2f}")
print(f"Max drawdown:      {dd.min():.2f}%")
print(f"Avg drawdown:      {dd.mean():.2f}%")

# Top/Bottom 10 tickers
print(f"\n--- Top 10 Tickers (by avg return) ---")
top_tickers = s.groupby('ticker')['ret'].agg(['mean','count','std']).sort_values('mean', ascending=False).head(10)
for t, r in top_tickers.iterrows():
    print(f"  {t:<8s}: avg={r['mean']:>+7.2f}%  n={int(r['count']):>4}  std={r['std']:>6.2f}%")

print(f"\n--- Bottom 10 Tickers (by avg return) ---")
bot_tickers = s.groupby('ticker')['ret'].agg(['mean','count','std']).sort_values('mean').head(10)
for t, r in bot_tickers.iterrows():
    print(f"  {t:<8s}: avg={r['mean']:>+7.2f}%  n={int(r['count']):>4}  std={r['std']:>6.2f}%")
