import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path
import glob
from datetime import datetime, timedelta
import requests
import json

# Page configuration
st.set_page_config(
    page_title="RS Analysis Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("📊 Relative Strength Analysis Dashboard")
st.markdown("Daily RS Calculation Logs Analysis and Insights | Historical Data from Oct 2021 to Present")

# Load data
@st.cache_data
def load_csv_files():
    """Load historical CSV file if available, otherwise load current files"""
    output_dir = Path(__file__).parent / "output"
    
    # Try to load historical stock data first (newer)
    stocks_historical_file = output_dir / "rs_stocks_historical.csv"
    if stocks_historical_file.exists():
        try:
            df = pd.read_csv(stocks_historical_file)
            df['date'] = pd.to_datetime(df['date'])
            df['source_file'] = 'stocks_historical'
            unique_dates = df['date'].nunique() if 'date' in df.columns else 0
            st.success(f"✅ Loaded historical stock data ({len(df):,} records, {unique_dates} trading days)")
            return df
        except Exception as e:
            st.warning(f"Error loading stock historical data: {e}")
    
    # Try older historical data format
    historical_file = output_dir / "rs_historical_all.csv"
    if historical_file.exists():
        try:
            df = pd.read_csv(historical_file)
            df['date'] = pd.to_datetime(df['date'])
            df['source_file'] = 'historical_all'
            st.success("✅ Loaded historical data (5.87M records, Oct 2021-Present)")
            return df
        except Exception as e:
            st.warning(f"Error loading historical data: {e}")
    
    # Fallback to current CSV files
    csv_files = sorted(glob.glob(str(output_dir / "rs_stocks*.csv")))
    if not csv_files:
        return None
    
    dfs = []
    for file in csv_files:
        try:
            df = pd.read_csv(file)
            df['source_file'] = Path(file).name
            dfs.append(df)
        except Exception as e:
            st.warning(f"Error loading {file}: {e}")
    
    if dfs:
        return pd.concat(dfs, ignore_index=True)
    return None

# Financial Modeling Prep API Functions
@st.cache_data
def get_company_profile(ticker, api_key):
    """Fetch company profile from Financial Modeling Prep API"""
    try:
        url = f"https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={api_key}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data[0] if data else None
    except Exception as e:
        st.error(f"Error fetching profile for {ticker}: {e}")
    return None

@st.cache_data
def get_company_key_metrics(ticker, api_key):
    """Fetch key metrics from Financial Modeling Prep API"""
    try:
        url = f"https://financialmodelingprep.com/api/v3/key-metrics/{ticker}?limit=1&apikey={api_key}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data[0] if data else None
    except Exception as e:
        st.error(f"Error fetching metrics for {ticker}: {e}")
    return None

@st.cache_data
def get_financial_ratios(ticker, api_key):
    """Fetch financial ratios from Financial Modeling Prep API"""
    try:
        url = f"https://financialmodelingprep.com/api/v3/ratios/{ticker}?limit=1&apikey={api_key}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data[0] if data else None
    except Exception as e:
        st.error(f"Error fetching ratios for {ticker}: {e}")
    return None

@st.cache_data
def get_earnings_dates(ticker, api_key):
    """Fetch upcoming earnings dates from Financial Modeling Prep API"""
    try:
        url = f"https://financialmodelingprep.com/api/v4/earning_calendar?symbol={ticker}&apikey={api_key}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        st.error(f"Error fetching earnings for {ticker}: {e}")
    return []

# Load industry data
@st.cache_data
def load_industry_data():
    """Load industry RS data from rs_industries_historical.csv (latest snapshot) or rs_industries.csv"""
    output_dir = Path(__file__).parent / "output"
    
    # Try historical file first (get latest snapshot)
    historical_file = output_dir / "rs_industries_historical.csv"
    if historical_file.exists():
        try:
            df_hist = pd.read_csv(historical_file)
            # Get latest date snapshot
            if 'date' in df_hist.columns:
                latest_date = pd.to_datetime(df_hist['date']).max()
                df_industry = df_hist[pd.to_datetime(df_hist['date']) == latest_date].copy()
            else:
                df_industry = df_hist.head(len(df_hist.groupby('Industry')))
            
            # Convert numeric columns
            numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile',
                          '3M_RS_Percentile', '6M_RS_Percentile', '1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank']
            for col in numeric_cols:
                if col in df_industry.columns:
                    df_industry[col] = pd.to_numeric(df_industry[col], errors='coerce')
            return df_industry
        except Exception as e:
            st.warning(f"Error loading historical industry data: {e}")
    
    # Fallback to current CSV file
    industry_file = output_dir / "rs_industries.csv"
    if industry_file.exists():
        try:
            df_industry = pd.read_csv(industry_file)
            # Convert numeric columns
            numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile',
                          '3M_RS_Percentile', '6M_RS_Percentile', '1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank']
            for col in numeric_cols:
                if col in df_industry.columns:
                    df_industry[col] = pd.to_numeric(df_industry[col], errors='coerce')
            return df_industry
        except Exception as e:
            st.warning(f"Error loading industry data: {e}")
    return None

# Load data
df = load_csv_files()
df_industry = load_industry_data()

if df is None or df.empty:
    st.error("No data found. Please check the CSV files in the output directory.")
    st.stop()

# Convert numeric columns
numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile', 
                '3M_RS_Percentile', '6M_RS_Percentile', 'Price', 'MarketCap', 
                'Float', 'ShortFloatPct', 'PctFrom52WkHigh', 'AvgVol10', 
                'AvgVol30', 'AvgVol50', 'RevenueGrowth']

for col in numeric_cols:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')

# Sidebar filters
st.sidebar.header("🔍 Filters")

# API Key Configuration
st.sidebar.subheader("🔑 API Configuration")
fmp_api_key = st.sidebar.text_input(
    "Financial Modeling Prep API Key",
    type="password",
    help="Get your free API key from https://financialmodelingprep.com/"
)

# Check if we have historical data
has_historical = 'date' in df.columns
has_dates = has_historical

# Date range filter (for historical data)
if has_historical:
    min_date = df['date'].min().date()
    max_date = df['date'].max().date()
    
    st.sidebar.subheader("📅 Date Range")
    date_range = st.sidebar.date_input(
        "Select date range",
        value=(max_date - timedelta(days=30), max_date),
        min_value=min_date,
        max_value=max_date,
        key="date_range"
    )
    
    if len(date_range) == 2:
        start_date, end_date = date_range
        filtered_df = df[(df['date'].dt.date >= start_date) & (df['date'].dt.date <= end_date)].copy()
    else:
        filtered_df = df.copy()
else:
    # Select dataset
    selected_file = st.sidebar.selectbox(
        "Select Dataset",
        options=['All Files'] + sorted(df['source_file'].unique().tolist()),
        help="Choose a specific RS log file or view all combined"
    )
    
    if selected_file != 'All Files':
        filtered_df = df[df['source_file'] == selected_file].copy()
    else:
        filtered_df = df.copy()

# Sector filter
sectors = ['All'] + sorted(filtered_df['Sector'].dropna().unique().tolist())
selected_sectors = st.sidebar.multiselect("Sector", sectors, default=['All'])
if 'All' not in selected_sectors:
    filtered_df = filtered_df[filtered_df['Sector'].isin(selected_sectors)]

# Percentile filter
min_percentile = st.sidebar.slider("Min Percentile", 0, 100, 0)
filtered_df = filtered_df[filtered_df['Percentile'] >= min_percentile]

# RS filter
min_rs = st.sidebar.number_input("Min Relative Strength", value=0.0)
filtered_df = filtered_df[filtered_df['Relative Strength'] >= min_rs]

# Display tabs
if has_historical:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(
        ["📈 Overview", "📊 Time Series", "🎯 Top Performers", "🔬 Deep Analysis", "📉 Trends", "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table"]
    )
else:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
        ["📈 Overview", "🎯 Top Performers", "📊 Distributions", "🔬 Deep Analysis", "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table"]
    )

# TAB 1: Overview
with tab1:
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Total Stocks", len(filtered_df))
    
    with col2:
        st.metric("Avg RS", f"{filtered_df['Relative Strength'].mean():.1f}")
    
    with col3:
        st.metric("Avg Percentile", f"{filtered_df['Percentile'].mean():.1f}")
    
    with col4:
        st.metric("Avg Price", f"${filtered_df['Price'].mean():.2f}")
    
    if has_historical:
        st.divider()
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("📊 Data Coverage")
            if 'date' in filtered_df.columns:
                unique_dates = filtered_df['date'].nunique()
                unique_stocks = filtered_df['Ticker'].nunique()
                st.metric("Trading Days", unique_dates)
                st.metric("Unique Stocks", unique_stocks)
    
    st.divider()
    
    # Top sectors by count
    col1, col2 = st.columns(2)
    
    with col1:
        sector_counts = filtered_df.drop_duplicates(subset=['Ticker'])['Sector'].value_counts().head(10)
        fig = px.bar(
            x=sector_counts.values,
            y=sector_counts.index,
            orientation='h',
            title="Top 10 Sectors by Stock Count",
            labels={'x': 'Count', 'y': 'Sector'}
        )
        st.plotly_chart(fig, width='stretch')
    
    with col2:
        avg_rs_by_sector = filtered_df.drop_duplicates(subset=['Ticker']).groupby('Sector')['Relative Strength'].mean().sort_values(ascending=False).head(10)
        fig = px.bar(
            x=avg_rs_by_sector.values,
            y=avg_rs_by_sector.index,
            orientation='h',
            title="Top 10 Sectors by Avg RS",
            labels={'x': 'Avg RS', 'y': 'Sector'},
            color=avg_rs_by_sector.values,
            color_continuous_scale='Viridis'
        )
        st.plotly_chart(fig, width='stretch')

# TAB 2: Time Series (only if historical data)
if has_historical:
    with tab2:
        st.subheader("📈 Time Series Analysis")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Average RS Over Time")
            daily_avg = filtered_df.groupby('date')['Relative Strength'].agg(['mean', 'median', 'max']).reset_index()
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=daily_avg['date'], y=daily_avg['mean'], name='Mean RS', mode='lines'))
            fig.add_trace(go.Scatter(x=daily_avg['date'], y=daily_avg['median'], name='Median RS', mode='lines'))
            fig.update_layout(title="Daily Average RS Trend", xaxis_title="Date", yaxis_title="RS Value", hovermode='x unified')
            st.plotly_chart(fig, width='stretch')
        
        with col2:
            st.subheader("Average Percentile Over Time")
            daily_percentile = filtered_df.groupby('date')['Percentile'].mean().reset_index()
            fig = px.line(daily_percentile, x='date', y='Percentile', title="Daily Average Percentile Trend")
            fig.update_layout(xaxis_title="Date", yaxis_title="Percentile")
            st.plotly_chart(fig, width='stretch')
        
        st.divider()
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Stock Count Tracked Over Time")
            daily_count = filtered_df.groupby('date')['Ticker'].nunique().reset_index()
            daily_count.columns = ['date', 'stock_count']
            fig = px.line(daily_count, x='date', y='stock_count', title="Number of Stocks in Universe Over Time")
            fig.update_layout(xaxis_title="Date", yaxis_title="Stock Count")
            st.plotly_chart(fig, width='stretch')
        
        with col2:
            st.subheader("Price Trend (Average)")
            daily_price = filtered_df.groupby('date')['Price'].mean().reset_index()
            fig = px.line(daily_price, x='date', y='Price', title="Average Stock Price Over Time")
            fig.update_layout(xaxis_title="Date", yaxis_title="Avg Price ($)")
            st.plotly_chart(fig, width='stretch')
        
        st.divider()
        
        # Sector trends
        st.subheader("📊 Sector RS Trends")
        selected_sector_trend = st.selectbox("Select sector for trend", 
                                            filtered_df['Sector'].unique(), 
                                            key="sector_trend")
        
        sector_trend_data = filtered_df[filtered_df['Sector'] == selected_sector_trend].groupby('date')['Relative Strength'].agg(['mean', 'min', 'max']).reset_index()
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['mean'], name='Mean RS', mode='lines+markers'))
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['max'], name='Max RS', fill='tozeroy', mode='lines', opacity=0.2))
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['min'], name='Min RS', fill='tonexty', mode='lines', opacity=0.2))
        fig.update_layout(title=f"{selected_sector_trend} - RS Trend", xaxis_title="Date", yaxis_title="RS Value", hovermode='x unified')
        st.plotly_chart(fig, width='stretch')

    # TAB 3: Top Performers
    tab3_label = "🎯 Top Performers"
    tab3_obj = tab3
else:
    # TAB 2: Distributions (if no historical data)
    with tab2:
        col1, col2 = st.columns(2)
        
        with col1:
            fig = px.histogram(
                filtered_df,
                x='Relative Strength',
                nbins=50,
                title="Distribution of Relative Strength",
                labels={'Relative Strength': 'RS Value', 'count': 'Frequency'}
            )
            st.plotly_chart(fig, width='stretch')
        
        with col2:
            fig = px.histogram(
                filtered_df,
                x='Percentile',
                nbins=50,
                title="Distribution of Percentile Rank",
                labels={'Percentile': 'Percentile', 'count': 'Frequency'}
            )
            st.plotly_chart(fig, width='stretch')
        
        col1, col2 = st.columns(2)
        
        with col1:
            fig = px.histogram(
                filtered_df,
                x='Price',
                nbins=50,
                title="Distribution of Stock Price",
                labels={'Price': 'Price ($)', 'count': 'Frequency'}
            )
            st.plotly_chart(fig, width='stretch')
        
        with col2:
            fig = px.histogram(
                filtered_df.dropna(subset=['ShortFloatPct']),
                x='ShortFloatPct',
                nbins=50,
                title="Distribution of Short Float %",
                labels={'ShortFloatPct': 'Short Float %', 'count': 'Frequency'}
            )
            st.plotly_chart(fig, width='stretch')
    
    tab3_label = "🎯 Top Performers"
    tab3_obj = tab3

# TAB 3 (or 4 if historical): Top Performers
with tab3:
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("🏆 Top 15 by Relative Strength")
        top_rs = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'Relative Strength')[
            ['Rank', 'Ticker', 'Sector', 'Relative Strength', 'Percentile', 'Price']
        ].copy()
        st.dataframe(top_rs.reset_index(drop=True), width='stretch', hide_index=True)
    
    with col2:
        st.subheader("⭐ Top 15 by Percentile")
        top_percentile = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'Percentile')[
            ['Rank', 'Ticker', 'Sector', 'Percentile', 'Relative Strength', 'Price']
        ].copy()
        st.dataframe(top_percentile.reset_index(drop=True), width='stretch', hide_index=True)
    
    st.divider()
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("💰 Top 15 by Market Cap")
        top_mcap = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'MarketCap')[
            ['Ticker', 'Sector', 'MarketCap', 'Relative Strength', 'Percentile']
        ].copy()
        top_mcap['MarketCap'] = top_mcap['MarketCap'].apply(lambda x: f"${x/1e9:.2f}B" if pd.notna(x) else "N/A")
        st.dataframe(top_mcap.reset_index(drop=True), width='stretch', hide_index=True)
    
    with col2:
        st.subheader("📈 Highest 6M")
        top_6m = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, '6M_RS_Percentile')[
            ['Ticker', '6M_RS_Percentile', '3M_RS_Percentile', '1M_RS_Percentile']
        ].copy()
        top_6m = top_6m.rename(columns={
            '1M_RS_Percentile': '1M',
            '3M_RS_Percentile': '3M',
            '6M_RS_Percentile': '6M'
        })
        st.dataframe(top_6m.reset_index(drop=True), width='stretch', hide_index=True)

# TAB 4 (or 5 if historical): Deep Analysis
with tab4:
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("RS vs Price Scatter")
        fig = px.scatter(
            filtered_df,
            x='Price',
            y='Relative Strength',
            color='Percentile',
            hover_data=['Ticker', 'Sector'],
            title="Relative Strength vs Price",
            color_continuous_scale='Viridis'
        )
        st.plotly_chart(fig, width='stretch')
    
    with col2:
        st.subheader("RS Percentiles Comparison")
        percentile_data = filtered_df[[
            '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile'
        ]].mean()
        
        fig = go.Figure(data=[
            go.Bar(name='1M', x=['1M'], y=[percentile_data['1M_RS_Percentile']]),
            go.Bar(name='3M', x=['3M'], y=[percentile_data['3M_RS_Percentile']]),
            go.Bar(name='6M', x=['6M'], y=[percentile_data['6M_RS_Percentile']])
        ])
        fig.update_layout(
            title="Average RS Percentile Comparison",
            barmode='group',
            yaxis_title="Avg Percentile"
        )
        st.plotly_chart(fig, width='stretch')
    
    st.divider()
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Distance from 52W High")
        fig = px.histogram(
            filtered_df.dropna(subset=['PctFrom52WkHigh']),
            x='PctFrom52WkHigh',
            nbins=40,
            title="Distribution of % from 52W High",
            labels={'PctFrom52WkHigh': '% From 52W High', 'count': 'Frequency'}
        )
        st.plotly_chart(fig, width='stretch')
    
    with col2:
        st.subheader("Revenue Growth vs RS")
        fig = px.scatter(
            filtered_df.dropna(subset=['RevenueGrowth']),
            x='RevenueGrowth',
            y='Relative Strength',
            color='Percentile',
            hover_data=['Ticker', 'Sector'],
            title="Revenue Growth vs Relative Strength",
            color_continuous_scale='Viridis'
        )
        st.plotly_chart(fig, width='stretch')
    
    st.divider()
    
    st.subheader("Key Statistics Summary")
    summary_stats = filtered_df[[
        'Relative Strength', 'Percentile', '1M_RS_Percentile', 
        '3M_RS_Percentile', '6M_RS_Percentile', 'Price', 'AvgVol10', 
        'AvgVol30', 'AvgVol50', 'ShortFloatPct', 'PctFrom52WkHigh', 'RevenueGrowth'
    ]].describe()
    
    st.dataframe(summary_stats, width='stretch')

# TAB 5 (or 6 if historical): Trends (only for historical data)
if has_historical:
    with tab5:
        st.subheader("📉 Trend Analysis")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Stock Momentum by Sector")
            # Get latest and oldest data
            latest_date = filtered_df['date'].max()
            oldest_date = filtered_df['date'].min()
            
            latest_rs = filtered_df[filtered_df['date'] == latest_date].groupby('Sector')['Relative Strength'].mean()
            oldest_rs = filtered_df[filtered_df['date'] == oldest_date].groupby('Sector')['Relative Strength'].mean()
            
            # Calculate change
            momentum = (latest_rs - oldest_rs).sort_values(ascending=False)
            
            fig = px.bar(
                x=momentum.values,
                y=momentum.index,
                orientation='h',
                title=f"RS Change by Sector ({oldest_date.date()} to {latest_date.date()})",
                labels={'x': 'RS Change', 'y': 'Sector'},
                color=momentum.values,
                color_continuous_scale='RdYlGn'
            )
            st.plotly_chart(fig, width='stretch')
        
        with col2:
            st.subheader("Top Gainers in RS")
            # Find stocks with biggest RS increase
            latest_stocks = filtered_df[filtered_df['date'] == latest_date][['Ticker', 'Sector', 'Relative Strength']].drop_duplicates(subset=['Ticker'], keep='first').copy()
            oldest_stocks = filtered_df[filtered_df['date'] == oldest_date][['Ticker', 'Relative Strength']].drop_duplicates(subset=['Ticker'], keep='first').copy()
            
            if not latest_stocks.empty and not oldest_stocks.empty:
                merged = latest_stocks.merge(oldest_stocks, on='Ticker', suffixes=('_latest', '_oldest'))
                merged['RS_change'] = merged['Relative Strength_latest'] - merged['Relative Strength_oldest']
                top_gainers = merged.nlargest(10, 'RS_change')[['Ticker', 'Sector', 'RS_change']]
                
                fig = px.bar(
                    top_gainers,
                    x='RS_change',
                    y='Ticker',
                    orientation='h',
                    title="Top 10 RS Gainers",
                    labels={'RS_change': 'RS Change', 'Ticker': 'Ticker'},
                    color='RS_change',
                    color_continuous_scale='Greens'
                )
                st.plotly_chart(fig, width='stretch')
        
        st.divider()
        
        st.subheader("🔍 Individual Stock Trajectory")
        selected_ticker = st.selectbox("Select ticker to analyze", 
                                      sorted(filtered_df['Ticker'].dropna().unique()),
                                      key="trajectory_ticker")
        
        ticker_data = filtered_df[filtered_df['Ticker'] == selected_ticker].sort_values('date')
        
        if not ticker_data.empty:
            col1, col2 = st.columns(2)
            
            with col1:
                fig = px.line(ticker_data, x='date', y='Relative Strength', 
                            title=f"{selected_ticker} - RS Trend",
                            markers=True)
                fig.update_layout(xaxis_title="Date", yaxis_title="RS Value")
                st.plotly_chart(fig, width='stretch')
            
            with col2:
                fig = px.line(ticker_data, x='date', y='Percentile',
                            title=f"{selected_ticker} - Percentile Trend",
                            markers=True, color_discrete_sequence=['orange'])
                fig.update_layout(xaxis_title="Date", yaxis_title="Percentile")
                st.plotly_chart(fig, width='stretch')
            
            col1, col2 = st.columns(2)
            
            with col1:
                fig = px.line(ticker_data, x='date', y='Price',
                            title=f"{selected_ticker} - Price Trend",
                            markers=True, color_discrete_sequence=['purple'])
                fig.update_layout(xaxis_title="Date", yaxis_title="Price ($)")
                st.plotly_chart(fig, width='stretch')
            
            with col2:
                fig = px.bar(ticker_data, x='date', y='Rank',
                           title=f"{selected_ticker} - Rank Over Time",
                           color='Rank', color_continuous_scale='Reds_r')
                fig.update_layout(xaxis_title="Date", yaxis_title="Rank (Lower is Better)")
                st.plotly_chart(fig, width='stretch')

# TAB 6 (or 5 if no historical): Industry Rotation
with tab6 if has_historical else tab5:
    st.subheader("🏭 Industry Rotation & Analysis")
    
    if df_industry is not None and not df_industry.empty:
        # Industry RS Rankings & Momentum
        st.write("**Industry Rankings & Momentum**")
        
        ind_display = df_industry.copy()
        
        # Check if we have rank columns (preferred) or percentile columns
        has_ranks = all(col in ind_display.columns for col in ['1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank'])
        has_percentiles = all(col in ind_display.columns for col in ['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile'])
        
        if has_ranks:
            # Use ranks instead of percentiles
            # For ranks: lower number = stronger
            ind_display['Delta 1M-3M'] = ind_display['3M_RS_Rank'] - ind_display['1M_RS_Rank']
            ind_display['Delta 1M-6M'] = ind_display['6M_RS_Rank'] - ind_display['1M_RS_Rank']

            # Rename rank columns to compact names and convert numeric values to integers
            ind_display = ind_display.rename(columns={
                '1M_RS_Rank': '1M',
                '3M_RS_Rank': '3M',
                '6M_RS_Rank': '6M'
            })

            # Cast numeric columns to integer (nullable Int64 to preserve NA)
            int_cols = ['Rank', '1M', '3M', '6M', 'Delta 1M-3M', 'Delta 1M-6M']
            for c in int_cols:
                if c in ind_display.columns:
                    ind_display[c] = pd.to_numeric(ind_display[c], errors='coerce')
                    ind_display[c] = ind_display[c].round().astype('Int64')

            cols_to_show = ['Rank', 'Industry', '1M', '3M', '6M', 
                            'Delta 1M-3M', 'Delta 1M-6M', 'Top 5 Tickers']
        elif has_percentiles:
            # Fallback to percentiles for backward compatibility
            ind_display['Delta 1M-3M'] = ind_display['1M_RS_Percentile'] - ind_display['3M_RS_Percentile']
            ind_display['Delta 1M-6M'] = ind_display['1M_RS_Percentile'] - ind_display['6M_RS_Percentile']

            ind_display = ind_display.rename(columns={
                '1M_RS_Percentile': '1M',
                '3M_RS_Percentile': '3M',
                '6M_RS_Percentile': '6M'
            })

            # Percentiles are 0-100 floats; convert to integer for display
            pct_cols = ['1M', '3M', '6M', 'Delta 1M-3M', 'Delta 1M-6M']
            for c in pct_cols:
                if c in ind_display.columns:
                    ind_display[c] = pd.to_numeric(ind_display[c], errors='coerce')
                    ind_display[c] = ind_display[c].round().astype('Int64')

            cols_to_show = ['Rank', 'Industry', '1M', '3M', '6M', 
                            'Delta 1M-3M', 'Delta 1M-6M', 'Top 5 Tickers']
        else:
            cols_to_show = ['Rank', 'Industry']
            
        # Compute Top 5 tickers per industry using the latest stock snapshot
        if 'date' in df.columns:
            latest_snapshot = df[df['date'] == df['date'].max()].drop_duplicates(subset=['Ticker'])
        else:
            latest_snapshot = filtered_df.drop_duplicates(subset=['Ticker'])

        top_tickers = []
        for _, ind_row in ind_display.iterrows():
            industry_name = ind_row.get('Industry', '')
            tickers_df = latest_snapshot[latest_snapshot['Industry'] == industry_name][['Ticker', 'Relative Strength']].dropna()
            if not tickers_df.empty:
                top5 = tickers_df.sort_values('Relative Strength', ascending=False).head(5)['Ticker'].tolist()
            else:
                tickers_str = ind_row.get('Tickers', '') if 'Tickers' in ind_row else ''
                top5 = [t.strip() for t in str(tickers_str).split(',') if t.strip()][:5]
            top_tickers.append(', '.join(top5))

        ind_display['Top 5 Tickers'] = top_tickers

        available_cols = [c for c in cols_to_show if c in ind_display.columns]

        if 'Rank' in ind_display.columns:
            ind_display = ind_display.sort_values('Rank', ascending=True)

        display_data = ind_display[available_cols].reset_index(drop=True)
        
        # Color coding for Delta columns
        def color_delta(val):
            if pd.isna(val):
                return ''
            try:
                val_float = float(val)
                if has_ranks:
                    # For ranks: positive delta = improving (lower rank = better)
                    color = 'green' if val_float > 0 else 'red' if val_float < 0 else 'gray'
                else:
                    # For percentiles: positive delta = improving (higher percentile = better)
                    color = 'green' if val_float > 0 else 'red' if val_float < 0 else 'gray'
                return f'color: {color}'
            except ValueError:
                return ''
                
        styled_df = display_data.style
        delta_cols = [c for c in ['Delta 1M-3M', 'Delta 1M-6M'] if c in available_cols]
        if delta_cols:
            try:
                styled_df = styled_df.map(color_delta, subset=delta_cols)
            except AttributeError:
                styled_df = styled_df.applymap(color_delta, subset=delta_cols)

        st.dataframe(
            styled_df,
            width='content',
            hide_index=True
        )
    else:
        st.warning("Industry RS data not found. Please check rs_industries.csv file.")

# TAB 7 (or 6 if no historical): Company Details
with tab7 if has_historical else tab6:
    st.subheader("💼 Company Details & Fundamentals")
    
    if not fmp_api_key:
        st.info("📌 Enter your Financial Modeling Prep API key in the sidebar to access company details and fundamentals.")
        st.markdown("""
        Get your free API key at: https://financialmodelingprep.com/
        """)
    else:
        selected_ticker_company = st.selectbox(
            "Select ticker to view company details",
            sorted(filtered_df['Ticker'].dropna().unique()),
            key="company_details_ticker"
        )
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader(f"📊 Company Profile - {selected_ticker_company}")
            profile = get_company_profile(selected_ticker_company, fmp_api_key)
            
            if profile:
                profile_data = {
                    'Company Name': profile.get('companyName', 'N/A'),
                    'Sector': profile.get('sector', 'N/A'),
                    'Industry': profile.get('industry', 'N/A'),
                    'Market Cap': f"${profile.get('mktCap', 0)/1e9:.2f}B" if profile.get('mktCap') else 'N/A',
                    'Price': f"${profile.get('price', 0):.2f}",
                    'Exchange': profile.get('exchangeShortName', 'N/A'),
                    'Website': profile.get('website', 'N/A'),
                }
                
                for key, value in profile_data.items():
                    st.write(f"**{key}:** {value}")
                
                if profile.get('description'):
                    st.write(f"**Description:** {profile.get('description')[:300]}...")
            else:
                st.warning(f"Could not fetch profile for {selected_ticker_company}")
        
        with col2:
            st.subheader(f"📈 Key Metrics - {selected_ticker_company}")
            metrics = get_company_key_metrics(selected_ticker_company, fmp_api_key)
            
            if metrics:
                metrics_display = {
                    'PE Ratio': f"{metrics.get('peRatio', 'N/A')}",
                    'ROE': f"{metrics.get('roe', 'N/A')*100:.2f}%" if metrics.get('roe') else 'N/A',
                    'Debt-to-Equity': f"{metrics.get('debtToEquity', 'N/A')}",
                    'Current Ratio': f"{metrics.get('currentRatio', 'N/A')}",
                    'Quick Ratio': f"{metrics.get('quickRatio', 'N/A')}",
                    'PB Ratio': f"{metrics.get('pbRatio', 'N/A')}",
                }
                
                for key, value in metrics_display.items():
                    st.write(f"**{key}:** {value}")
            else:
                st.warning(f"Could not fetch metrics for {selected_ticker_company}")
        
        st.divider()
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader(f"📊 Financial Ratios - {selected_ticker_company}")
            ratios = get_financial_ratios(selected_ticker_company, fmp_api_key)
            
            if ratios:
                ratios_display = {
                    'Gross Profit Margin': f"{ratios.get('grossProfitMargin', 'N/A')*100:.2f}%" if ratios.get('grossProfitMargin') else 'N/A',
                    'Operating Profit Margin': f"{ratios.get('operatingProfitMargin', 'N/A')*100:.2f}%" if ratios.get('operatingProfitMargin') else 'N/A',
                    'Net Profit Margin': f"{ratios.get('netProfitMargin', 'N/A')*100:.2f}%" if ratios.get('netProfitMargin') else 'N/A',
                    'Asset Turnover': f"{ratios.get('assetTurnover', 'N/A')}",
                    'Debt-to-Assets': f"{ratios.get('debtToAssets', 'N/A')}",
                }
                
                for key, value in ratios_display.items():
                    st.write(f"**{key}:** {value}")
            else:
                st.warning(f"Could not fetch ratios for {selected_ticker_company}")
        
        with col2:
            st.subheader(f"📅 Earnings Information - {selected_ticker_company}")
            earnings = get_earnings_dates(selected_ticker_company, fmp_api_key)
            
            if earnings:
                earnings_df = pd.DataFrame(earnings[:10])  # Show next 10 earnings dates
                if 'date' in earnings_df.columns:
                    st.dataframe(
                        earnings_df[['date', 'epsEstimated', 'epsActual']].head(10),
                        width='stretch',
                        hide_index=True
                    )
            else:
                st.info("No earnings data available for this ticker")

# TAB 8 (or 7 if no historical): Data Table
with tab8 if has_historical else tab7:
    st.subheader("Full Data Table")
    
    # Always show only the most recent data
    table_df = filtered_df.copy()
    if 'date' in df.columns:
        # Use the absolute latest date from the entire dataset
        latest_date = df['date'].max()
        table_df = df[df['date'] == latest_date].copy()
        st.caption(f"📅 Showing latest data from: {latest_date.date()}")
    else:
        st.caption("📅 Showing all available data")
        
    table_df = table_df.rename(columns={
        '1M_RS_Percentile': '1M',
        '3M_RS_Percentile': '3M',
        '6M_RS_Percentile': '6M'
    })
    
    # Column selector
    all_cols = table_df.columns.tolist()
    # Remove date column from selection if it exists
    if 'date' in all_cols:
        all_cols.remove('date')
    if 'source_file' in all_cols:
        all_cols.remove('source_file')
    
    default_cols = ['Rank', 'Ticker', 'Industry', '1M', '3M', '6M', 'Price', 'MarketCap', 'AvgVol30', 'PctFrom52WkHigh']
    
    selected_cols = st.multiselect(
        "Select columns to display",
        all_cols,
        default=[col for col in default_cols if col in all_cols]
    )
    
    # Sort options
    sort_options = selected_cols if selected_cols else all_cols
    default_sort_index = sort_options.index('Rank') if 'Rank' in sort_options else 0
    sort_col = st.selectbox("Sort by", sort_options, index=default_sort_index)
    sort_ascending = st.checkbox("Ascending", value=True)
    
    display_df = table_df[selected_cols].sort_values(
        by=sort_col, 
        ascending=sort_ascending,
        na_position='last'
    ).reset_index(drop=True)
    
    # Filters
    st.write("**📊 Filter Data:**")
    
    ticker_filter = st.text_input("🔍 Search by Ticker", "").strip().upper()
    if ticker_filter and 'Ticker' in display_df.columns:
        display_df = display_df[display_df['Ticker'].astype(str).str.upper().str.contains(ticker_filter)]

    filter_cols = st.columns(3)
    filter_conditions = []
    
    numeric_cols_available = [col for col in display_df.columns if display_df[col].dtype in ['float64', 'int64']]
    
    if numeric_cols_available:
        for idx, col in enumerate(numeric_cols_available[:9]):  # Show max 9 filters (3x3 grid)
            with filter_cols[idx % 3]:
                col_min = display_df[col].min()
                col_max = display_df[col].max()
                if pd.notna(col_min) and pd.notna(col_max) and col_min < col_max:
                    range_values = st.slider(
                        f"{col}",
                        min_value=float(col_min),
                        max_value=float(col_max),
                        value=(float(col_min), float(col_max)),
                        key=f"filter_{col}"
                    )
                    filter_conditions.append((col, range_values))
    
    # Apply range filters
    for col, (min_val, max_val) in filter_conditions:
        display_df = display_df[(display_df[col] >= min_val) & (display_df[col] <= max_val)]
    
    display_df = display_df.reset_index(drop=True)

    # Add Rank column as first column
    display_df_with_rank = pd.DataFrame()
    display_df_with_rank['Rank'] = range(1, len(display_df) + 1)
    for col in display_df.columns:
        display_df_with_rank[col] = display_df[col]
    
    # Display table
    st.dataframe(display_df_with_rank, width='stretch', hide_index=True)
    
    # Download button
    csv = display_df_with_rank.to_csv(index=False)
    st.download_button(
        label="Download filtered data as CSV",
        data=csv,
        file_name="rs_analysis_filtered.csv",
        mime="text/csv"
    )
    
    st.divider()
    
    # Top N Tickers by RS grouped by Industry (sorted by strongest industry)
    st.subheader("📋 Top Tickers by Relative Strength (Grouped by Strongest Industry)")
    
    # Variable input for number of tickers
    num_tickers = st.number_input(
        "Number of top tickers to display",
        min_value=10,
        max_value=len(table_df),
        value=200,
        step=10,
        key="num_top_tickers"
    )
    
    top_n = table_df.drop_duplicates(subset=['Ticker']).nlargest(int(num_tickers), 'Relative Strength')[['Ticker', 'Industry', 'Relative Strength']].copy()
    
    # Get industry RS ranking from df_industry
    if df_industry is not None and not df_industry.empty:
        industry_rs_map = dict(zip(df_industry['Industry'], df_industry['Relative Strength']))
    else:
        industry_rs_map = {}
    
    # Add industry RS score to help with sorting
    top_n['Industry_RS'] = top_n['Industry'].map(industry_rs_map)
    
    # Group by Industry and sort by industry RS strength (descending)
    industries_sorted = top_n.groupby('Industry')['Industry_RS'].first().sort_values(ascending=False).index.tolist()
    
    # Create combined ticker string ordered by strongest industry first
    all_tickers_by_industry = []
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        all_tickers_by_industry.extend(industry_tickers)
    
    all_tickers_string = ','.join(all_tickers_by_industry)
    
    # Display combined box at top
    st.write(f"**All Industries Combined (Sorted by Strongest Industry First)** ({len(all_tickers_by_industry)} tickers)")
    col1, col2 = st.columns([4, 1])
    with col1:
        st.code(all_tickers_string, language="text")
    with col2:
        st.download_button(
            label="Copy All",
            data=all_tickers_string,
            file_name=f"top{int(num_tickers)}_tickers_by_industry.txt",
            mime="text/plain",
            key="download_all_tickers"
        )
    
    st.divider()
    
    # Individual industry boxes (sorted by strength)
    st.write("**By Industry (Strongest First):**")
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        ticker_string = ','.join(industry_tickers)
        
        # Get industry RS info if available
        industry_rs = industry_rs_map.get(industry, 'N/A')
        industry_label = f"**{industry}** ({len(industry_tickers)} tickers) | RS: {industry_rs}"
        st.write(industry_label)
        
        col1, col2 = st.columns([4, 1])
        with col1:
            st.code(ticker_string, language="text")
        with col2:
            st.download_button(
                label=f"Copy {industry}",
                data=ticker_string,
                file_name=f"{industry.replace(' ', '_')}_tickers.txt",
                mime="text/plain",
                key=f"download_{industry}"
            )

# Footer
st.divider()
footer_text = "**Daily Relative Strength Analysis Dashboard**"
if has_historical:
    footer_text += " | Historical Data: Oct 2021 - Present (1238 trading days, 5.87M records)"
footer_text += " | Built with Streamlit"
st.markdown(footer_text)
