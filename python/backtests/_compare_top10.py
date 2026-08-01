#!/usr/bin/env python3
"""Compare top-10 combos from full_backtest vs scanner_universe_backtest"""
import pandas as pd

fb = pd.read_csv('python/backtests/full_backtest_summary.csv')
su = pd.read_csv('python/backtests/scanner_universe_summary.csv')

def top10(df, source):
    """Return top 10 by sharpe"""
    return df.sort_values('sharpe', ascending=False).head(10)[['buy_strategy','exit_rule','trades','win_pct','avg_ret','sharpe']].assign(source=source)

fb10 = top10(fb, 'full_backtest')
su10 = top10(su, 'scanner_universe')

# Normalize combo names
def key(r):
    return f"{r['buy_strategy']} × {r['exit_rule']}"

fb_set = {key(r) for _, r in fb10.iterrows()}
su_set = {key(r) for _, r in su10.iterrows()}

both = fb_set & su_set
fb_only = fb_set - su_set
su_only = su_set - fb_set

print("=" * 100)
print("TOP-10 COMPARISON: full_backtest vs scanner_universe_backtest")
print("=" * 100)

# Full backtest top-10
print(f"\n{'─'*50} FULL_BACKTEST TOP 10 {'─'*50}")
for _, r in fb10.iterrows():
    marker = " ⭐ BOTH" if key(r) in both else ""
    print(f"  {r['buy_strategy']:<48s} {r['exit_rule']:<14s} n={int(r['trades']):>6,}  win={r['win_pct']:>5.1f}%  ret={r['avg_ret']:>+6.2f}%  sharpe={r['sharpe']:>.2f}{marker}")

# Scanner universe top-10
print(f"\n{'─'*50} SCANNER_UNIVERSE TOP 10 {'─'*48}")
for _, r in su10.iterrows():
    marker = " ⭐ BOTH" if key(r) in both else ""
    print(f"  {r['buy_strategy']:<48s} {r['exit_rule']:<14s} n={int(r['trades']):>6,}  win={r['win_pct']:>5.1f}%  ret={r['avg_ret']:>+6.2f}%  sharpe={r['sharpe']:>.2f}{marker}")

# Overlap / difference analysis
print(f"\n{'='*100}")
print("COMPARISON STATS")
print(f"{'='*100}")
print(f"  In BOTH backtests:            {len(both)}")
print(f"  FULL_BACKTEST only:           {len(fb_only)}")
print(f"  SCANNER_UNIVERSE only:        {len(su_only)}")

if both:
    print(f"\n  ⭐ SHARED WINNERS:")
    for s in sorted(both):
        fb_row = fb10[[key(r) == s for _, r in fb10.iterrows()]]
        su_row = su10[[key(r) == s for _, r in su10.iterrows()]]
        fw, sw = fb_row.iloc[0]['win_pct'], su_row.iloc[0]['win_pct']
        fr, sr = fb_row.iloc[0]['avg_ret'], su_row.iloc[0]['avg_ret']
        print(f"    {s:<65s}  FB:{fw:.1f}%/{fr:+.2f}%  SU:{sw:.1f}%/{sr:+.2f}%")

if fb_only:
    print(f"\n  🔵 FULL_BACKTEST unique:")
    for s in sorted(fb_only): print(f"    {s}")

if su_only:
    print(f"\n  🟡 SCANNER_UNIVERSE unique:")
    for s in sorted(su_only): print(f"    {s}")

# Compare avg stats
print(f"\n{'='*100}")
print(f"  FB top-10 avg sharpe: {fb10['sharpe'].mean():.2f}  |  SU top-10 avg sharpe: {su10['sharpe'].mean():.2f}")
print(f"  FB top-10 avg win%:   {fb10['win_pct'].mean():.1f}%  |  SU top-10 avg win%:   {su10['win_pct'].mean():.1f}%")
print(f"  FB top-10 avg ret:    {fb10['avg_ret'].mean():+.2f}%  |  SU top-10 avg ret:    {su10['avg_ret'].mean():+.2f}%")
print(f"  FB top-10 avg trades: {int(fb10['trades'].mean()):,}   |  SU top-10 avg trades: {int(su10['trades'].mean()):,}")
