# RS Historical Data Pipeline & Streamlit App Update

## 🚀 What Was Created

### 1. **Historical Data Pipeline** (`build_historical_data.py`)
A Python script that extracts all daily RS data from git commit history:

- **Extracts data from 1,268 git commits** (Oct 2021 - May 2026)
- **Consolidates into single historical file** (`rs_historical_all.csv`)
- **Results**: 5,869,265 records across 9,710 unique tickers and 1,238 trading days
- **Handles missing data gracefully** with warnings for commits without RS files

#### Running the Pipeline
```bash
cd /Users/sw/Desktop/stock/rs-log
python3 build_historical_data.py
```

This creates `output/rs_historical_all.csv` with all historical data.

---

## 📊 Enhanced Streamlit App Features

The app now automatically loads historical data and provides 6 tabs:

### **Tab 1: 📈 Overview**
- Key metrics (total stocks, avg RS, avg percentile, avg price)
- Data coverage info (trading days, unique stocks)
- Top 10 sectors by count and average RS

### **Tab 2: 📊 Time Series** *(NEW - Historical Data Only)*
- **Daily average RS trend** with mean, median, and max lines
- **Daily average percentile trend** over time
- **Stock universe size** over time (how many stocks tracked each day)
- **Average price movement** across all stocks
- **Sector-specific RS trends** with min/max bands

### **Tab 3: 🎯 Top Performers**
- Top 15 stocks by RS
- Top 15 stocks by percentile
- Top 15 stocks by market cap
- Top 15 stocks by 6-month RS percentile

### **Tab 4: 🔬 Deep Analysis**
- **RS vs Price scatter plot** with percentile coloring
- **RS percentiles comparison** (1M, 3M, 6M)
- **Distance from 52-week high** distribution
- **Revenue growth vs RS correlation**
- **Detailed statistics summary** table

### **Tab 5: 📉 Trends** *(NEW - Historical Data Only)*
- **Sector momentum analysis**: RS change from oldest to latest date in range
- **Top RS gainers**: Stocks with biggest RS increase
- **Individual stock trajectory**: Select any ticker to see:
  - RS trend over time
  - Percentile rank progression
  - Price movement
  - Rank changes

### **Tab 6: 📋 Data Table**
- Full dataset viewer with custom column selection
- Flexible sorting options
- CSV export functionality

---

## 🔍 Interactive Filters (Sidebar)

### For Historical Data:
- **Date Range**: Select custom date range to analyze
  - Default: Last 30 days
  - Can view any historical period

### For All Data:
- **Sector**: Multi-select filtering by sector
- **Min Percentile**: Threshold for percentile rank
- **Min Relative Strength**: Threshold for RS value

---

## 📈 Key Insights Available

### Time-Series Analysis
- Watch how RS distribution changes over 5 years
- See sector rotation and leadership changes
- Track universe expansion/contraction

### Trend Analysis
- Identify which sectors are gaining momentum
- Find stocks with strongest uptrends
- Analyze individual stock RS progression

### Period Comparison
- Compare performance between different time periods
- Identify seasonal patterns
- Track market regime changes

---

## 🛠️ Technical Details

### Data Structure
The historical dataset includes:
- **1,238 unique trading dates** (Oct 20, 2021 - May 23, 2026)
- **9,710 unique tickers** tracked over time
- **5,869,265 total records**

### Columns Available
Standard RS calculation columns:
- Rank, Ticker, Sector, Industry, Exchange
- Relative Strength, Percentile
- 1M_RS_Percentile, 3M_RS_Percentile, 6M_RS_Percentile
- Price, MarketCap, Float, ShortFloatPct
- PctFrom52WkHigh, AvgVol10, AvgVol30, AvgVol50
- RevenueGrowth
- Plus: 52WkHigh, 52WkLow, AvgVol60, 1/3/6 Month Ago, Universe
- **date**: Trading date (added by pipeline)

---

## 🚀 Quick Start

1. **Build historical data** (one-time):
   ```bash
   python3 build_historical_data.py
   ```

2. **Run the Streamlit app**:
   ```bash
   streamlit run app.py
   ```

3. **Open browser** to `http://localhost:8501`

4. **Explore**:
   - Select date range in sidebar
   - Choose filters (sector, percentile, RS)
   - Switch between tabs for different views
   - Download filtered data as needed

---

## 💡 Use Cases

### For Traders
- Identify sector leadership and rotation
- Find stocks breaking into new highs
- Track momentum changes over time
- Compare current performance to historical norms

### For Analysts
- Understand multi-year RS trends
- Analyze correlation between RS and other metrics
- Study sector dynamics
- Generate insights on market structure

### For Researchers
- Historical data spanning 5 years
- 9700+ stocks in dataset
- Complete daily snapshots
- Export data for further analysis

---

## 📝 Notes

- **Historical data is cached** for performance (uses Streamlit's @st.cache_data)
- **Date range is interactive** - changes automatically update all visualizations
- **All filters work together** - combine multiple filters for deeper analysis
- **Sector trends** show both average and range (min/max)
- **Stock trajectories** automatically update based on selected ticker

---

## 🔄 Updating Data

To add new data as it becomes available:

1. Commit new RS data to git
2. Run `python3 build_historical_data.py` again
3. Streamlit app will automatically load the updated historical file

The pipeline is designed to handle repeated runs - it will combine all available data.
