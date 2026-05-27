# Stock RS Historical Data - Auto-Update Setup

**Status**: ✅ Complete and Tested  
**Date**: May 26, 2026

## What Was Added

### 📊 Historical Stock RS Dataset
- **Total Records**: 369,709
- **Unique Stocks**: 6,291
- **Unique Trading Days**: 63
- **Date Range**: Feb 28, 2026 → May 26, 2026
- **File Size**: 73 MB

### 🔄 Automation Scripts

**`build_stocks_history.py`**
- Scans all 1,269 git commits for `rs_stocks_1.csv` and `rs_stocks_2.csv`
- Processed 126 stock CSV files
- Converts percentiles to ranks (same as industries)
- Creates `rs_stocks_historical.csv`

**`append_stocks_history.py`**
- Appends new stock data from recent commits
- Runs daily Mon-Fri at 9:00 AM (same schedule as industries)
- Detects last date in history automatically
- Only adds new records (no duplicates)
- Uses Ticker + Date as deduplication key

### ⚙️ Automation Configuration
- **Updated**: `setup_auto_update.py`
  - Now runs both industry AND stock append scripts
  - Single LaunchAgent with combined execution
  - Shared logging: `append_rs_out.log` and `append_rs_error.log`

### 📱 App Integration
- **Updated**: `app.py`
  - `load_csv_files()` now checks for `rs_stocks_historical.csv` first
  - Falls back to current `rs_stocks*.csv` files
  - Auto-loads with success message showing record count

## Data Structure

### Stock Historical CSV Columns
```
Rank, Ticker, Sector, Industry, Relative Strength, Percentile,
1M_RS_Percentile, 3M_RS_Percentile, 6M_RS_Percentile,
Price, MarketCap, Float, ShortFloatPct, PctFrom52WkHigh,
AvgVol10, AvgVol30, AvgVol50, RevenueGrowth, date, commit_hash,
1M_RS_Rank, 3M_RS_Rank, 6M_RS_Rank
```

## Quick Start

### 1. The automation is already configured
No additional setup needed - just enable the LaunchAgent:

```bash
launchctl load ~/Library/LaunchAgents/com.rs-log.append-rs-history.plist
```

### 2. Monitor Both Updates
```bash
tail -f logs/append_rs_out.log      # Both industry and stock updates
tail -f logs/append_rs_error.log    # Error logs
```

### 3. Manual Update (Optional)
```bash
python3 append_industry_history.py
python3 append_stocks_history.py
```

## What Happens Daily (Mon-Fri 9 AM)

1. ✅ Append industry data (usually 0-1 new records)
2. ✅ Append stock data (usually 0-1 new batch of 6,291 stocks)
3. ✅ Remove duplicates from both datasets
4. ✅ Update metadata JSON files
5. ✅ Log results

## Now Both Are Auto-Updated

| Data Type | Records | Dates | Status |
|-----------|---------|-------|--------|
| Industry RS | 192,223 | 1,238 (4+ years) | ✅ Auto-updating |
| Stock RS | 369,709 | 63 (3 months) | ✅ Auto-updating |

## Summary

### Before
```
✅ Industry RS: Auto-updated with 4+ years history
❌ Stock RS: No historical tracking
```

### After
```
✅ Industry RS: Auto-updated with 4+ years history (192K records)
✅ Stock RS: Auto-updated with 3+ months history (369K records)
🔄 Both: Single daily automation at 9:00 AM Mon-Fri
```

## Files Created/Updated

```
✅ build_stocks_history.py              (New - Build stock history)
✅ append_stocks_history.py             (New - Daily append stocks)
✅ setup_auto_update.py                 (Updated - Combined automation)
✅ app.py                               (Updated - Load stock history)
✅ rs_stocks_historical.csv             (New - 369K records, 73 MB)
✅ rs_stocks_metadata.json              (New - Stats & metadata)
```

## Testing Results

✅ **Build**: 126 stock files processed → 369,709 records  
✅ **Append**: No new commits detected (correct behavior)  
✅ **App Integration**: Stock history loads automatically  
✅ **Ranks**: Percentiles converted to ranks successfully  

---

**Status: Ready for Production**
