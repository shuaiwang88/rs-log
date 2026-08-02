#!/usr/bin/env python3
"""
daily_watchlist_runner.py
=========================
Runs unified_watchlist.py, saves dated output, diffs against previous day,
and reports new entrants to the SMA50+Shakeout+TF>=3 golden tier.

Cron (weekdays after market close):
  0 17 * * 1-5 cd /home/shuaiwang/Desktop/rs-log && python3 python/backtests/daily_watchlist_runner.py >> python/backtests/watchlist_history.log 2>&1
"""
import subprocess
import sys
from pathlib import Path
from datetime import datetime, date, timedelta

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
BACKTESTS = Path(__file__).resolve().parent
WATCHLIST_SCRIPT = BACKTESTS / "unified_watchlist.py"
HISTORY_DIR = BACKTESTS / "watchlist_history"

today = date.today()
yesterday = today - timedelta(days=1)
today_csv = HISTORY_DIR / f"watchlist_{today.isoformat()}.csv"
yesterday_csv = HISTORY_DIR / f"watchlist_{yesterday.isoformat()}.csv"

HISTORY_DIR.mkdir(exist_ok=True)

print(f"\n{'='*80}")
print(f"📅 DAILY WATCHLIST RUNNER — {today.isoformat()}")
print(f"{'='*80}")

# ── Step 1: Run the watchlist ──
print(f"\n⏳ Running unified_watchlist.py...")
result = subprocess.run(
    [sys.executable, str(WATCHLIST_SCRIPT)],
    capture_output=True, text=True, cwd=str(ROOT), timeout=600
)

if result.returncode != 0:
    print(f"❌ Watchlist crashed:\n{result.stderr}")
    sys.exit(1)

lines = result.stdout.strip().split('\n')
print(lines[-2] if len(lines) >= 2 else "Done")

# ── Step 2: Save dated copy ──
live_csv = BACKTESTS / "unified_watchlist.csv"
if not live_csv.exists():
    print("❌ No unified_watchlist.csv produced")
    sys.exit(1)

df = pd.read_csv(live_csv)
df.to_csv(today_csv, index=False)
print(f"💾 Saved {len(df)} tickers to {today_csv}")

# ── Step 3: Compute golden tier (SMA50+Shakeout + TF>=3) ──
tf_cols = ['above_sma200', 'ema_bullish', 'near_52w_high', 'rsi_bullish']
golden_mask = df['combo_SMA50_Shakeout'] & (df[tf_cols].sum(axis=1) >= 3)
golden_today = set(df[golden_mask]['ticker'])
all_today = set(df['ticker'])

new_golden = set()
left_golden = set()
new_entrants = set()
removals = set()

if yesterday_csv.exists():
    df_prev = pd.read_csv(yesterday_csv)
    golden_prev = set(df_prev[df_prev['combo_SMA50_Shakeout'] & (df_prev[tf_cols].sum(axis=1) >= 3)]['ticker'])
    all_prev = set(df_prev['ticker'])

    new_golden = golden_today - golden_prev
    left_golden = golden_prev - golden_today
    new_entrants = all_today - all_prev
    removals = all_prev - all_today
else:
    print(f"\n⚠️  No previous file ({yesterday_csv.name}) — skipping diff")

# ── Step 4: Report ──
print(f"\n{'='*80}")
print(f"📊 GOLDEN TIER (SMA50+Shakeout + TF>=3): {len(golden_today)} tickers")
print(f"{'='*80}")

if yesterday_csv.exists():
    hdr = '🔥 NEW TO GOLDEN TIER' if new_golden else '✅ No new golden entrants'
    print(f"\n{hdr}: {len(new_golden)}")
    for t in sorted(new_golden)[:20]:
        row = df[df['ticker'] == t].iloc[0]
        print(f"  🔥 {t:<8s} {row['pattern']:<16s} depth={row['depth']:.1f}% len={int(row['length'])}d dist={row['dist_to_pivot']:+.1f}%")
    if len(new_golden) > 20:
        print(f"  ... and {len(new_golden) - 20} more")

    hdr2 = '⬇️  LEFT GOLDEN TIER' if left_golden else '✅ None left golden tier'
    print(f"\n{hdr2}: {len(left_golden)}")
    for t in sorted(left_golden)[:10]:
        print(f"  ⬇️  {t}")

    hdr3 = '🆕 NEW WATCHLIST ENTRANTS' if new_entrants else '✅ No new entrants'
    print(f"\n{hdr3}: {len(new_entrants)}")
    for t in sorted(new_entrants)[:10]:
        print(f"  🆕 {t}")

    hdr4 = '🚫 REMOVED FROM WATCHLIST' if removals else '✅ No removals'
    print(f"\n{hdr4}: {len(removals)}")
    for t in sorted(removals)[:10]:
        print(f"  🚫 {t}")

# ── Step 5: Summary line ──
print(f"\n📋 {today.isoformat()}: {len(all_today)} total | {len(golden_today)} golden | "
      f"{len(new_golden)} new-golden | {len(left_golden)} left-golden | "
      f"{len(new_entrants)} new | {len(removals)} removed")
print(f"{'='*80}\n")
