# RS Industries Historical Dataset & Rank-Based Analysis

## Overview

This setup creates and maintains a comprehensive historical dataset from git commits, replacing percentile-based metrics with rank-based analysis in the Industry Rotation section.

### What's New

1. **Historical Dataset**: Combined 1,255+ commits into a single 194K+ record dataset
2. **Rank-Based Metrics**: Industry Rotation now uses ranks (1M/3M/6M RS Rank) instead of percentiles
3. **Automatic Updates**: Daily appending of new commits to keep history current
4. **Time Series Analysis**: Full capability to analyze industry rotation trends over time

## Files

### Core Scripts

- **`build_industry_history.py`** - Builds initial historical dataset from git
  - Scans all git commits for `rs_industries.csv`
  - Creates `rs_industries_historical.csv` with 1,255 snapshots
  - Calculates rank columns (1M_RS_Rank, 3M_RS_Rank, 6M_RS_Rank)
  - Generates metadata JSON with summary statistics

- **`append_industry_history.py`** - Appends new commits to historical dataset
  - Runs daily (via cron/LaunchAgent)
  - Detects last commit in history
  - Adds only new commits
  - Maintains data integrity with deduplication
  - Updates metadata

- **`setup_auto_update.py`** - Configures automatic daily execution
  - macOS: Sets up LaunchAgent (runs 9:00 AM, Mon-Fri)
  - Linux: Provides crontab configuration
  - Creates log directories

### Output Files

- **`output/rs_industries_historical.csv`** (57 MB)
  - Complete historical snapshots (1,238 unique dates)
  - Columns: All original + `date`, `commit_hash`, and rank columns
  - Date range: 2021-10-20 to 2026-05-23
  - 194,536 total records

- **`output/rs_industries_metadata.json`**
  - Total records, unique dates, date range
  - Last update timestamp

## Quick Start

### 1. Build Initial Historical Dataset

```bash
cd /Users/sw/Desktop/stock/rs-log
python3 build_industry_history.py
```

Output:
```
✓ Processed 1255 commits with rs_industries.csv
✅ Saved historical data: 194536 records
   Date range: 2021-10-20 to 2026-05-23
```

### 2. Setup Automatic Daily Updates

```bash
python3 setup_auto_update.py
```

Then for macOS:
```bash
launchctl load ~/Library/LaunchAgents/com.rs-log.append-industry-history.plist
```

### 3. Manual Update (Optional)

```bash
python3 append_industry_history.py
```

## Rank vs Percentile Conversion

### Understanding Ranks

In the rank system:
- **Lower rank = Stronger performance**
- Rank 1 = Best (99th percentile)
- Rank 99 = Worst (1st percentile)

### Conversion Formula

```
Rank = 101 - Percentile
```

**Example:**
- Percentile 99 → Rank 2 (strong)
- Percentile 50 → Rank 51 (neutral)
- Percentile 10 → Rank 91 (weak)

### Delta Interpretation (Ranks)

For rank-based deltas in Industry Rotation:
- **Positive delta = Industry improving** (getting lower rank)
- **Negative delta = Industry weakening** (getting higher rank)

**Example:**
- 3M Rank: 40, 1M Rank: 35
- Delta (3M - 1M): +5 = **Improving** ✓ (Green)

## App Features

### Industry Rotation & Analysis Tab

The "Industry Rotation" tab now displays:

| Column | Description |
|--------|-------------|
| Rank | Industry rank |
| Industry | Industry name |
| 1M Rank | 1-month performance rank |
| 3M Rank | 3-month performance rank |
| 6M Rank | 6-month performance rank |
| Delta 1M-3M | Change vs 3 months ago (positive = improving) |
| Delta 1M-6M | Change vs 6 months ago (positive = improving) |

**Color Coding:**
- 🟢 Green = Improving (positive delta)
- 🔴 Red = Weakening (negative delta)

### Time Series Analysis

The "Time Series" tab includes trend analysis for:
- Industry ranking changes over time
- Sector strength momentum
- Historical rank progression

### Backward Compatibility

If rank columns are unavailable, app falls back to percentile-based display automatically.

## Data Structure

### Historical CSV Columns

```
Rank,Industry,Sector,Relative Strength,Percentile,1M_RS_Percentile,
3M_RS_Percentile,6M_RS_Percentile,Tickers,date,commit_hash,
1M_RS_Rank,3M_RS_Rank,6M_RS_Rank
```

### Example Row

```
1,Semiconductor Equipment & Materials,Technology,188.81,99,99.0,95.0,88.0,
"AEHR,TRT,ASYS,...",2026-05-23,c7d9629,2.0,6.0,13.0
```

## Automation Details

### macOS LaunchAgent

- **Frequency**: Monday-Friday at 9:00 AM
- **User**: Your local user account
- **Logs**: 
  - stdout: `logs/append_industry_out.log`
  - stderr: `logs/append_industry_error.log`
- **Location**: `~/Library/LaunchAgents/com.rs-log.append-industry-history.plist`

### Commands

```bash
# Load (enable)
launchctl load ~/Library/LaunchAgents/com.rs-log.append-industry-history.plist

# Unload (disable)
launchctl unload ~/Library/LaunchAgents/com.rs-log.append-industry-history.plist

# Check status
launchctl list | grep rs-log

# View recent logs
tail -f logs/append_industry_out.log
tail -f logs/append_industry_error.log
```

## Usage in App

### Loading Data Priority

The app loads industry data in this order:

1. ✅ `rs_industries_historical.csv` (if exists) → **Uses latest snapshot**
2. ✅ `rs_industries.csv` (fallback)
3. ❌ Error if neither exists

### Accessing Ranks in App

All rank columns are automatically available:
- `1M_RS_Rank`: 1-month rank
- `3M_RS_Rank`: 3-month rank  
- `6M_RS_Rank`: 6-month rank

The app intelligently uses ranks if available, otherwise falls back to percentiles.

## Performance

- **Initial Build**: ~90 seconds (1,268 commits)
- **Daily Update**: ~2-5 seconds (usually 0-2 new commits)
- **Historical File Size**: 57 MB
- **Query Speed**: Sub-second (latest snapshot)

## Troubleshooting

### No Historical Data Loading

1. Check file exists: `ls -lh output/rs_industries_historical.csv`
2. Verify path is correct in app
3. Check metadata: `cat output/rs_industries_metadata.json`
4. Rebuild if needed: `python3 build_industry_history.py`

### Automation Not Running

**macOS:**
```bash
# Check if loaded
launchctl list | grep rs-log

# Verify plist file exists
ls -la ~/Library/LaunchAgents/com.rs-log.append-industry-history.plist

# Check logs
tail logs/append_industry_error.log
```

### Duplicate Data Issues

The append script automatically deduplicates on (Industry, date) combination.

To force rebuild:
```bash
rm output/rs_industries_historical.csv
python3 build_industry_history.py
```

## Future Enhancements

- [ ] Add quarterly rank analysis
- [ ] Industry trend alerts
- [ ] Export historical trend reports
- [ ] Interactive rank heatmaps
- [ ] Comparative industry benchmarking

## References

- Relative Strength analysis framework
- Industry rotation strategies
- Rank percentile conversion methodology
