#!/usr/bin/env python3
"""Generate pretty golden-tier watchlist report from unified_watchlist.csv."""
import pandas as pd
from datetime import datetime

df = pd.read_csv("python/backtests/unified_watchlist.csv")

# Golden tier: SMA50+Shakeout AND TF≥3
golden = df[(df["combo_SMA50_Shakeout"] == True) & (df["tf_flags_on"] >= 3)].copy()
golden = golden.sort_values("composite", ascending=False)

print("╔══════════════════════════════════════════════════════════════════════════════════════════╗")
print("║                      🏆 GOLDEN TIER WATCHLIST — August 1, 2026                          ║")
print("║               SMA50 Bounce + Shakeout  ×  TF≥3  │  Depth ≤ 25%,  Len ≤ 150d            ║")
print("╚══════════════════════════════════════════════════════════════════════════════════════════╝")
print(f"\n  {len(golden)} tickers  │  filtered from {len(df)} total watchlist  │  generated {datetime.now().strftime('%H:%M')}\n")

# ── Summary stats ──
deep_quality = golden[(golden["depth"] <= 25) & (golden["length"] <= 150)]
print(f"  ── Quality Filter: depth≤25% & len≤150d ──")
print(f"  📊 {len(deep_quality)} tickers pass  │  avg composite {deep_quality['composite'].mean():.1f}")
print(f"  Avg depth: {deep_quality['depth'].mean():.1f}%  │  Avg length: {deep_quality['length'].mean():.0f}d")
print()

# ── Top 20 table ──
cols = ["ticker", "pattern", "depth", "length", "dist_to_pivot", "composite",
        "combo_PB_SMA50", "tf_flags_on", "rsi"]
show = golden.head(30) if len(golden) > 30 else golden

header = f"  {'Ticker':<8} {'Pattern':<15} {'Depth':>7} {'Len':>5} {'DistPiv':>8} {'Comp':>6} {'PB+SMA50':>9} {'TF':>3} {'RSI':>5}"
print(header)
print("  " + "─" * (len(header) - 2))

for _, r in show.iterrows():
    pb_flag = "✅" if r.get("combo_PB_SMA50", False) else "—"
    dist = f"{r['dist_to_pivot']:+.1f}%" if pd.notna(r["dist_to_pivot"]) else "N/A"
    print(f"  {r['ticker']:<8} {r['pattern']:<15} {r['depth']:>6.1f}% {int(r['length']):>5}  {dist:>7}  {r['composite']:>5.1f}    {pb_flag:>7}   {int(r['tf_flags_on']):>2}  {r['rsi']:>5.0f}")

# ── Per-pattern breakdown ──
print(f"\n  ── Per-Pattern Breakdown ──")
for pat in sorted(golden["pattern"].unique()):
    s = golden[golden["pattern"] == pat]
    print(f"  {pat:<15}: {len(s):>3} tickers  avg depth {s['depth'].mean():.1f}%  avg comp {s['composite'].mean():.1f}")

# ── Depth distribution ──
print(f"\n  ── Depth Distribution ──")
for lo, hi in [(0,12), (12,18), (18,25), (25,35), (35,50)]:
    n = golden[(golden["depth"] >= lo) & (golden["depth"] < hi)]
    bar = "█" * max(1, len(n))
    print(f"  {lo:>2}-{hi}%: {len(n):>3}  {bar}")

# ── TF flag breakdown ──
print(f"\n  ── Trend-Following Signals ──")
for col in ["above_sma200", "ema_bullish", "near_52w_high", "rsi_bullish"]:
    n = golden[golden[col] == True]
    label = col.replace("_", " ").title()
    print(f"  {label:<18}: {len(n):>3}/{len(golden)} ({len(n)/len(golden)*100:.0f}%)")

# ── Both engines firing ──
both = golden[golden.get("combo_PB_SMA50", pd.Series([False]*len(golden))) == True]
if len(both) > 0:
    print(f"\n  ── 🔥 Both Engines Firing (PB+SMA50 too!) ──")
    for _, r in both.iterrows():
        print(f"  {r['ticker']:<8} {r['pattern']:<15} depth={r['depth']:.1f}%  len={int(r['length'])}d  comp={r['composite']:.1f}")

# ── Top by composite ──
print(f"\n  ── 🥇 Top 5 by Composite Score ──")
for i, (_, r) in enumerate(golden.head(5).iterrows(), 1):
    tf_labels = []
    if r.get("above_sma200"): tf_labels.append("SMA200")
    if r.get("ema_bullish"): tf_labels.append("EMA")
    if r.get("near_52w_high"): tf_labels.append("52WH")
    if r.get("rsi_bullish"): tf_labels.append("RSI")
    print(f"  {i}. {r['ticker']:<8}  {r['pattern']:<15}  depth {r['depth']:.1f}%  len {int(r['length'])}d  comp {r['composite']:.1f}")
    print(f"     TF: {', '.join(tf_labels)}  |  dist to pivot: {r['dist_to_pivot']:+.1f}%  |  RSI: {r['rsi']:.0f}")

print()
