# Relative Strength Analysis Dashboard

A Streamlit web application for analyzing daily Relative Strength (RS) calculation logs.

## Features

- **Overview Dashboard**: Quick stats on stocks, average RS, percentiles, and prices
- **Top Performers**: Filter and view top stocks by RS, percentile, market cap, and 6-month RS percentile
- **Distributions**: Visualize distributions of key metrics (RS, percentile, price, short float)
- **Deep Analysis**: Scatter plots and correlation analysis between RS and other factors
- **Data Table**: Full filterable dataset with custom column selection and download capability

## Installation

1. **Clone or navigate to the project directory**:
   ```bash
   cd /Users/sw/Desktop/stock/rs-log
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## Running the App

Run the Streamlit app with:

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`

## Features Explained

### Sidebar Filters
- **Select Dataset**: Choose a specific CSV file or view all combined
- **Sector**: Filter by one or multiple sectors
- **Min Percentile**: Set minimum percentile threshold
- **Min Relative Strength**: Set minimum RS threshold

### Tabs

1. **📈 Overview**
   - Key metrics summary
   - Top sectors by stock count
   - Top sectors by average RS

2. **🎯 Top Performers**
   - Top 15 stocks by RS
   - Top 15 stocks by percentile
   - Top 15 stocks by market cap
   - Top 15 stocks by 6-month RS percentile

3. **📊 Distributions**
   - Histograms for RS, Percentile, Price, and Short Float %

4. **🔬 Deep Analysis**
   - RS vs Price scatter plot
   - RS percentiles comparison (1M, 3M, 6M)
   - Distance from 52-week high
   - Revenue growth vs RS correlation
   - Detailed statistics summary

5. **📋 Data Table**
   - Full dataset view with custom columns
   - Sorting and filtering
   - CSV download functionality

## Data Structure

The app expects CSV files in the `output/` directory with the following columns:

- Rank
- Ticker
- Sector
- Industry
- Exchange
- Relative Strength
- Percentile
- 1M_RS_Percentile
- 3M_RS_Percentile
- 6M_RS_Percentile
- Price
- MarketCap
- Float
- ShortFloatPct
- PctFrom52WkHigh
- AvgVol10
- AvgVol30
- AvgVol50
- RevenueGrowth

## Tips

- Use the filters to focus on specific sectors or metrics
- Click on data points in scatter plots for more information
- Download filtered data for further analysis
- Combine multiple filters for detailed analysis

## Requirements

- Python 3.7+
- See `requirements.txt` for package dependencies
