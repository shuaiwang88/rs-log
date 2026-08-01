#!/usr/bin/env python3
"""Deep dive: both-fired bases — what separates winners from losers."""
import pandas as pd, numpy as np

df = pd.read_csv("python/backtests/two_phase_results.csv")
both = df[df["both_fired"]].copy()
print(f"=== Both-Phase Bases: {len(both):,} total ===")
print(f"   Winners: {both['combined_ret'].gt(0).sum():,} ({both['combined_ret'].gt(0).mean()*100:.1f}%)")
print(f"   Losers:  {both['combined_ret'].le(0).sum():,} ({both['combined_ret'].le(0).mean()*100:.1f}%)\n")

# ── Depth buckets ──
depth_bins = [(0,8), (8,12), (12,18), (18,25), (25,35), (35,50)]
print("=== DEPTH — Win% per Bucket ===")
print(f"{'Depth':<12} {'N':>6} {'Win%':>7} {'AvgComb':>8} {'AvgP1':>8} {'AvgP2':>8}")
for lo, hi in depth_bins:
    s = both[(both["depth"] >= lo) & (both["depth"] < hi)]
    if len(s) < 5: continue
    print(f"{lo}-{hi}%{'':<5} {len(s):>6,} {s['combined_ret'].gt(0).mean()*100:>6.1f}% {s['combined_ret'].mean():>+7.2f}% {s['p1_ret'].mean():>+7.2f}% {s['p2_ret'].mean():>+7.2f}%")

# ── Length buckets ──
len_bins = [(0,20), (20,40), (40,65), (65,100), (100,130), (130,200), (200,400)]
print(f"\n=== LENGTH — Win% per Bucket ===")
print(f"{'Length':<12} {'N':>6} {'Win%':>7} {'AvgComb':>8} {'AvgP1':>8} {'AvgP2':>8}")
for lo, hi in len_bins:
    s = both[(both["length"] >= lo) & (both["length"] < hi)]
    if len(s) < 5: continue
    print(f"{lo}-{hi}d{'':<6} {len(s):>6,} {s['combined_ret'].gt(0).mean()*100:>6.1f}% {s['combined_ret'].mean():>+7.2f}% {s['p1_ret'].mean():>+7.2f}% {s['p2_ret'].mean():>+7.2f}%")

# ── Pattern ──
print(f"\n=== PATTERN — Win% ===")
print(f"{'Pattern':<18} {'N':>6} {'Win%':>7} {'AvgComb':>8} {'AvgP1':>8} {'AvgP2':>8}")
for pat in sorted(both["pattern"].unique()):
    s = both[both["pattern"] == pat]
    if len(s) < 5: continue
    print(f"{pat:<18} {len(s):>6,} {s['combined_ret'].gt(0).mean()*100:>6.1f}% {s['combined_ret'].mean():>+7.2f}% {s['p1_ret'].mean():>+7.2f}% {s['p2_ret'].mean():>+7.2f}%")

# ── Depth × Length heatmap ──
print(f"\n=== DEPTH × LENGTH HEATMAP (win% / avg combined) ===")
depth_bins_h = [(0,12), (12,18), (18,25), (25,35), (35,50)]
len_bins_h = [(0,40), (40,65), (65,100), (100,200)]
print(f"{'Depth↓/Len→':<16}", end="")
for lo_l, hi_l in len_bins_h:
    print(f"  {lo_l}-{hi_l}d  ", end="")
print()
for lo_d, hi_d in depth_bins_h:
    print(f"{lo_d}-{hi_d}%{'':<10}", end="")
    for lo_l, hi_l in len_bins_h:
        s = both[(both["depth"] >= lo_d) & (both["depth"] < hi_d) &
                 (both["length"] >= lo_l) & (both["length"] < hi_l)]
        if len(s) >= 3:
            w = s["combined_ret"].gt(0).mean()*100
            a = s["combined_ret"].mean()
            print(f" {w:5.0f}% {a:+5.1f}%", end="")
        else:
            print(f"    · ·   ", end="")
    print()

# ── Find optimal filter ──
print(f"\n=== SWEEPING FOR OPTIMAL FILTER ===")
print(f"{'Filter':<55} {'N':>6} {'Win%':>7} {'Avg':>8} {'Sharpe':>7}")
best = None
for max_depth in [8, 12, 15, 18, 20, 22, 25, 30, 35]:
    for max_len in [20, 30, 40, 50, 60, 65, 80, 100, 120, 150]:
        f = both[(both["depth"] <= max_depth) & (both["length"] <= max_len)]
        if len(f) < 10: continue
        r = f["combined_ret"]
        w = r.gt(0).mean()*100
        s = r.mean()/r.std() if r.std() > 0 else 0
        label = f"depth≤{max_depth}%  len≤{max_len}d"
        if best is None or s > best[1]:
            best = (label, s, len(f), w, r.mean())
        if w >= 70:  # only print good filters
            print(f"{label:<55} {len(f):>6,} {w:>6.1f}% {r.mean():>+7.2f}% {s:>7.2f}")

if best:
    label, s, n, w, a = best
    print(f"\n🏆 BEST FILTER: {label}  →  n={n:,}  win={w:.1f}%  avg={a:+.2f}%  sharpe={s:.2f}")

# ── Win/Loss asymmetry ──
wins = both[both["combined_ret"] > 0]
losses = both[both["combined_ret"] <= 0]
print(f"\n=== WIN/LOSS ASYMMETRY ===")
print(f"  Wins:   n={len(wins):,}  avg comb={wins['combined_ret'].mean():+.2f}%  avg P1={wins['p1_ret'].mean():+.2f}%  avg P2={wins['p2_ret'].mean():+.2f}%")
print(f"  Losses: n={len(losses):,}  avg comb={losses['combined_ret'].mean():+.2f}%  avg P1={losses['p1_ret'].mean():+.2f}%  avg P2={losses['p2_ret'].mean():+.2f}%")
print(f"  Ratio:  {abs(wins['combined_ret'].mean()/losses['combined_ret'].mean()):.1f}:1")

# ── Return distribution ──
print(f"\n=== RETURN DISTRIBUTION ===")
bins = [(-100,-20), (-20,-10), (-10,-5), (-5,-2), (-2,0), (0,2), (2,5), (5,10), (10,20), (20,50), (50,500)]
for lo, hi in bins:
    n = both[(both["combined_ret"] >= lo) & (both["combined_ret"] < hi)]
    bar = "█" * max(1, int(len(n)/max(1, len(both)/60)))
    print(f"  {lo:>5}..{hi:>5}%: {len(n):>4}  {bar}")

# ── Top recurring tickers ──
print(f"\n=== TOP RECURRING TICKERS (both fired, ≥2 trades) ===")
ticker_stats = both.groupby("ticker").agg(
    n=("combined_ret", "count"),
    win_pct=("combined_ret", lambda x: (x>0).mean()*100),
    avg_ret=("combined_ret", "mean")
).query("n >= 2").sort_values("win_pct", ascending=False)
for ticker, row in ticker_stats.head(15).iterrows():
    print(f"  {ticker:<6}  n={int(row['n']):>2}  win={row['win_pct']:>5.0f}%  avg={row['avg_ret']:>+6.1f}%")

# ── P1 vs P2 contribution ──
print(f"\n=== P1 vs P2 CONTRIBUTION ===")
print(f"  P1 avg ret: {both['p1_ret'].mean():+.2f}%  (win: {(both['p1_ret']>0).mean()*100:.1f}%)")
print(f"  P2 avg ret: {both['p2_ret'].mean():+.2f}%  (win: {(both['p2_ret']>0).mean()*100:.1f}%)")
print(f"  Combined:   {both['combined_ret'].mean():+.2f}%")
print(f"  P2 is the dominant driver — 1.0x weight vs P1's 0.5x")
