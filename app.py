import streamlit as st
from streamlit.components.v1 import html as st_html
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yfinance as yf
from pathlib import Path
import glob
import subprocess
import sys
from datetime import datetime, timedelta
import json
import uuid
import os
from pattern_recognition import PatternRecognizer

# ---------------------- Streamlit Compatibility ----------------------
def rerun_app():
    try:
        st.rerun()
    except AttributeError:
        st.experimental_rerun()

# ---------------------- Markers Persistence ----------------------
MARKERS_DIR = Path(__file__).resolve().parent / "markers"
MARKERS_DIR.mkdir(exist_ok=True)

def get_markers_file_path(ticker, chart_type="daily"):
    return MARKERS_DIR / f"{ticker}_{chart_type}_markers.json"

def load_markers(ticker, chart_type="daily"):
    filepath = get_markers_file_path(ticker, chart_type)
    if filepath.exists():
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_markers(ticker, markers, chart_type="daily"):
    filepath = get_markers_file_path(ticker, chart_type)
    try:
        with open(filepath, 'w') as f:
            json.dump(markers, f, indent=2)
        return True
    except Exception:
        return False

# ---------------------- Lightweight Charts Helper (3 panes) ----------------------
def create_lightweight_candlestick_html(df, title="", height=600, markers=None, rs_label=None,
                                        rs_raw=None, rs_quick=None, rs_quicksand=None, rs_gd=None,
                                        volume_data=None, volume_sma50=None,
                                        pp10_dates=None, pp5_dates=None,
                                        churn_dates=None, stall_dates=None, ll3_dates=None):
    """
    Three-pane chart:
    - Top: price candlesticks + MAs + user markers + RS label
    - Middle: volume bars (up/down/dry colors) + volume SMA50 + markers (PP, churn, stall, 3LL)
    - Bottom: RS lines
    """
    if df is None or df.empty:
        return "<div style='padding:20px; color:#999;'>No data available</div>"

    candles = []
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp())
            candles.append({
                'time': ts,
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close'])
            })
        except Exception:
            continue

    if not candles:
        return "<div style='padding:20px; color:#999;'>No valid candle data</div>"

    df_clean = df[['Close']].copy()
    df_clean['MA10'] = df_clean['Close'].rolling(10).mean()
    df_clean['MA21'] = df_clean['Close'].rolling(21).mean()
    df_clean['MA50'] = df_clean['Close'].rolling(50).mean()
    ma10_data, ma21_data, ma50_data = [], [], []
    for idx, row in df_clean.iterrows():
        ts = int(idx.timestamp())
        if not pd.isna(row['MA10']):
            ma10_data.append({'time': ts, 'value': float(row['MA10'])})
        if not pd.isna(row['MA21']):
            ma21_data.append({'time': ts, 'value': float(row['MA21'])})
        if not pd.isna(row['MA50']):
            ma50_data.append({'time': ts, 'value': float(row['MA50'])})

    def prepare_rs(series):
        if series is None:
            return []
        if isinstance(series, pd.Series):
            return [{'time': int(idx.timestamp()), 'value': float(v)} for idx, v in series.items() if pd.notna(v)]
        return series

    rs_raw_data = prepare_rs(rs_raw)
    rs_quick_data = prepare_rs(rs_quick)
    rs_quicksand_data = prepare_rs(rs_quicksand)
    rs_gd_data = prepare_rs(rs_gd)

    price_markers = list(markers) if markers else []
    if rs_label is not None and candles:
        price_markers.append({
            'time': candles[-1]['time'],
            'position': 'aboveBar',
            'color': '#636efa',
            'shape': 'circle',
            'text': str(rs_label)
        })
    price_markers_json = json.dumps(price_markers)

    volume_colored = volume_data if volume_data else []
    volume_sma50_data = volume_sma50 if volume_sma50 else []

    pp10 = pp10_dates if pp10_dates else []
    pp5 = pp5_dates if pp5_dates else []
    churn = churn_dates if churn_dates else []
    stall = stall_dates if stall_dates else []
    ll3 = ll3_dates if ll3_dates else []

    price_id = f"price_{uuid.uuid4().hex[:8]}"
    volume_id = f"vol_{uuid.uuid4().hex[:8]}"
    rs_id = f"rs_{uuid.uuid4().hex[:8]}"

    price_h = int(height * 0.5)
    volume_h = int(height * 0.2)
    rs_h = height - price_h - volume_h

    html = f"""
    <div style="width:100%; font-family: system-ui, sans-serif;">
        <div id="{price_id}" style="height:{price_h}px; border:1px solid #ddd; border-bottom:none; border-radius:4px 4px 0 0;"></div>
        <div id="{volume_id}" style="height:{volume_h}px; border-left:1px solid #ddd; border-right:1px solid #ddd;"></div>
        <div id="{rs_id}" style="height:{rs_h}px; border:1px solid #ddd; border-top:none; border-radius:0 0 4px 4px;"></div>
    </div>
    <script src="https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js"></script>
    <script>
    (function() {{
        const priceDiv = document.getElementById('{price_id}');
        const volumeDiv = document.getElementById('{volume_id}');
        const rsDiv = document.getElementById('{rs_id}');

        const priceChart = LightweightCharts.createChart(priceDiv, {{
            width: priceDiv.clientWidth, height: {price_h},
            layout: {{ backgroundColor: '#fff', textColor: '#333' }},
            timeScale: {{ timeVisible: true, secondsVisible: false }}
        }});
        const volumeChart = LightweightCharts.createChart(volumeDiv, {{
            width: volumeDiv.clientWidth, height: {volume_h},
            layout: {{ backgroundColor: '#fff', textColor: '#333' }},
            timeScale: {{ timeVisible: false }}
        }});
        const rsChart = LightweightCharts.createChart(rsDiv, {{
            width: rsDiv.clientWidth, height: {rs_h},
            layout: {{ backgroundColor: '#fff', textColor: '#333' }},
            timeScale: {{ timeVisible: false }}
        }});

        const masterScale = priceChart.timeScale();
        volumeChart.timeScale().subscribeVisibleLogicalRangeChange(r => masterScale.setVisibleLogicalRange(r));
        rsChart.timeScale().subscribeVisibleLogicalRangeChange(r => masterScale.setVisibleLogicalRange(r));
        masterScale.subscribeVisibleLogicalRangeChange(r => {{
            volumeChart.timeScale().setVisibleLogicalRange(r);
            rsChart.timeScale().setVisibleLogicalRange(r);
        }});

        const candleSeries = priceChart.addCandlestickSeries({{
            upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
            wickUpColor: '#26a69a', wickDownColor: '#ef5350'
        }});
        candleSeries.setData({json.dumps(candles)});
        const ma10 = priceChart.addLineSeries({{ color: '#FF9800', lineWidth: 2, title: 'MA10' }});
        const ma21 = priceChart.addLineSeries({{ color: '#2196F3', lineWidth: 2, title: 'MA21' }});
        const ma50 = priceChart.addLineSeries({{ color: '#F44336', lineWidth: 2, title: 'MA50' }});
        if ({json.dumps(ma10_data)}.length) ma10.setData({json.dumps(ma10_data)});
        if ({json.dumps(ma21_data)}.length) ma21.setData({json.dumps(ma21_data)});
        if ({json.dumps(ma50_data)}.length) ma50.setData({json.dumps(ma50_data)});
        if ({price_markers_json}.length) {{
            candleSeries.setMarkers({price_markers_json}.map(m => ({{
                time: m.time, position: m.position || 'aboveBar',
                color: m.color, shape: m.shape || 'circle', text: m.text
            }})));
        }}

        const volumeSeries = volumeChart.addHistogramSeries({{
            priceFormat: {{ type: 'volume' }},
            priceScaleId: 'right'
        }});
        volumeSeries.setData({json.dumps(volume_colored)});

        if ({json.dumps(volume_sma50_data)}.length) {{
            const volSma = volumeChart.addLineSeries({{
                color: 'orange', lineWidth: 1.5, title: 'Vol SMA(50)'
            }});
            volSma.setData({json.dumps(volume_sma50_data)});
        }}

        const volData = {json.dumps(volume_colored)};
        let maxVol = 0;
        volData.forEach(v => {{ if (v.value > maxVol) maxVol = v.value; }});
        const markerY = maxVol * 1.08;

        function addMarkers(chart, dates, color, shape, size, yValue) {{
            if (!dates.length) return;
            const series = chart.addLineSeries({{
                color: color, lineWidth: 0, lineVisible: false,
                pointMarkersVisible: true, pointMarkerType: shape, pointMarkerSize: size,
                priceLineVisible: false, lastValueVisible: false
            }});
            const points = dates.map(ts => ({{ time: ts, value: yValue }}));
            series.setData(points);
        }}

        addMarkers(volumeChart, {json.dumps(pp10)}, 'yellow', 'diamond', 12, markerY);
        addMarkers(volumeChart, {json.dumps(pp5)}, 'blue', 'diamond', 10, markerY);
        addMarkers(volumeChart, {json.dumps(churn)}, 'purple', 'x', 12, markerY);
        addMarkers(volumeChart, {json.dumps(stall)}, 'maroon', 'circle', 10, markerY);
        addMarkers(volumeChart, {json.dumps(ll3)}, 'red', 'triangleUp', 12, markerY);

        const rsRaw = rsChart.addLineSeries({{ color: 'blue', lineWidth: 2, title: 'Raw RS' }});
        const rsQuick = rsChart.addLineSeries({{ color: '#56b8e6', lineWidth: 2, title: 'Quick EMA (21)' }});
        const rsQuicksand = rsChart.addLineSeries({{ color: '#ff8c00', lineWidth: 2, title: 'Quicksand EMA (34)' }});
        const rsGd = rsChart.addLineSeries({{ color: '#2ca02c', lineWidth: 2, title: 'GD EMA (50)' }});
        if ({json.dumps(rs_raw_data)}.length) rsRaw.setData({json.dumps(rs_raw_data)});
        if ({json.dumps(rs_quick_data)}.length) rsQuick.setData({json.dumps(rs_quick_data)});
        if ({json.dumps(rs_quicksand_data)}.length) rsQuicksand.setData({json.dumps(rs_quicksand_data)});
        if ({json.dumps(rs_gd_data)}.length) rsGd.setData({json.dumps(rs_gd_data)});

        masterScale.fitContent();

        const tooltip = document.createElement('div');
        tooltip.style.cssText = 'position:absolute; background:rgba(0,0,0,0.8); color:#fff; padding:6px 10px; border-radius:6px; font-size:12px; pointer-events:none; z-index:1000; display:none;';
        priceDiv.parentElement.style.position = 'relative';
        priceDiv.parentElement.appendChild(tooltip);

        priceChart.subscribeCrosshairMove(function(param) {{
            if (!param.time || !param.point) {{
                tooltip.style.display = 'none';
                return;
            }}
            const data = param.seriesData.get(candleSeries);
            if (data && data.open !== undefined) {{
                tooltip.innerHTML = '<b>OHLC</b><br>O: $' + data.open.toFixed(2) + '<br>H: $' + data.high.toFixed(2) + '<br>L: $' + data.low.toFixed(2) + '<br>C: $' + data.close.toFixed(2);
                tooltip.style.display = 'block';
                tooltip.style.left = param.point.x + 15 + 'px';
                tooltip.style.top = param.point.y - 30 + 'px';
            }} else {{
                tooltip.style.display = 'none';
            }}
        }});
        priceDiv.addEventListener('mouseleave', () => tooltip.style.display = 'none');

        window.addEventListener('resize', function() {{
            if (priceDiv.clientWidth) {{
                priceChart.applyOptions({{ width: priceDiv.clientWidth }});
                volumeChart.applyOptions({{ width: volumeDiv.clientWidth }});
                rsChart.applyOptions({{ width: rsDiv.clientWidth }});
            }}
        }});
    }})();
    </script>
    """
    return html

# ---------------------- Page configuration ----------------------
st.set_page_config(page_title="RS Analysis Dashboard", page_icon="📊", layout="wide", initial_sidebar_state="expanded")

if 'selected_ticker' not in st.session_state:
    st.session_state.selected_ticker = None
    st.session_state.current_ticker_index = 0

st.title("📊 Relative Strength Analysis Dashboard")
st.markdown("Daily RS Calculation Logs Analysis and Insights | Historical Data from Oct 2021 to Present")

col1, col2 = st.columns([2, 8])
with col1:
    if st.button("🔁 Reload data from disk"):
        try:
            st.cache_data.clear()
        except Exception:
            pass
        rerun_app()
with col2:
    if st.button("⬇️ Pull Latest Data & Update"):
        with st.spinner("Pulling latest data from upstream and updating pipeline..."):
            try:
                repo_dir = str(Path(__file__).resolve().parent)
                branch_proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
                branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "main"
                subprocess.run(["git", "fetch", "upstream"], cwd=repo_dir, capture_output=True)
                subprocess.run(["git", "merge", f"upstream/{branch}"], cwd=repo_dir, capture_output=True)
                pull_res = subprocess.run(["git", "pull"], cwd=repo_dir, capture_output=True, text=True)
                subprocess.run(["git", "push", "origin", branch], cwd=repo_dir, capture_output=True)
                if pull_res.returncode != 0:
                    st.error(f"Git pull failed:\n{pull_res.stderr}")
                else:
                    subprocess.run([sys.executable, "check_remote_and_append.py", "--force"], cwd=repo_dir, capture_output=True, text=True)
                    st.cache_data.clear()
                    rerun_app()
            except Exception as e:
                st.error(f"An error occurred: {e}")

# ---------------------- Data loading functions ----------------------
def compute_output_signature():
    output_dir = Path(__file__).parent / "output"
    if not output_dir.exists():
        return ""
    parts = []
    for fp in sorted(output_dir.glob('*.csv')):
        try:
            parts.append(f"{fp.name}:{int(fp.stat().st_mtime)}")
        except Exception:
            pass
    return '|'.join(parts)

@st.cache_data
def load_csv_files(reload_sig: str):
    output_dir = Path(__file__).parent / "output"
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

@st.cache_data
def load_industry_data(reload_sig: str):
    output_dir = Path(__file__).parent / "output"
    historical_file = output_dir / "rs_industries_historical.csv"
    if historical_file.exists():
        try:
            df_hist = pd.read_csv(historical_file)
            if 'date' in df_hist.columns:
                latest_date = pd.to_datetime(df_hist['date']).max()
                df_industry = df_hist[pd.to_datetime(df_hist['date']) == latest_date].copy()
            else:
                df_industry = df_hist.head(len(df_hist.groupby('Industry')))
            numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile', '1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank']
            for col in numeric_cols:
                if col in df_industry.columns:
                    df_industry[col] = pd.to_numeric(df_industry[col], errors='coerce')
            return df_industry
        except Exception as e:
            st.warning(f"Error loading historical industry data: {e}")
    
    industry_file = output_dir / "rs_industries.csv"
    if industry_file.exists():
        try:
            df_industry = pd.read_csv(industry_file)
            numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile', '1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank']
            for col in numeric_cols:
                if col in df_industry.columns:
                    df_industry[col] = pd.to_numeric(df_industry[col], errors='coerce')
            return df_industry
        except Exception as e:
            st.warning(f"Error loading industry data: {e}")
    return None

# ---------------------- Main data load ----------------------
output_sig = compute_output_signature()
df = load_csv_files(output_sig)
df_industry = load_industry_data(output_sig)

if df is None or df.empty:
    st.error("No data found. Please check the CSV files in the output directory.")
    st.stop()

numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile', 
                '3M_RS_Percentile', '6M_RS_Percentile', 'Price', 'MarketCap', 
                'Float', 'ShortFloatPct', 'PctFrom52WkHigh', 'AvgVol10', 
                'AvgVol30', 'AvgVol50', 'RevenueGrowth']
for col in numeric_cols:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')

# ---------------------- Sidebar filters ----------------------
st.sidebar.header("🔍 Filters")
st.sidebar.subheader("📋 Ticker Navigation (All Industries Combined)")

num_top_tickers = st.sidebar.number_input(
    "Number of top tickers", min_value=10, max_value=500, value=200, step=10,
    help="How many top RS tickers to include from the combined industry list."
)

has_historical = 'date' in df.columns

if has_historical:
    min_date = df['date'].min().date()
    max_date = df['date'].max().date()
    st.sidebar.subheader("📅 Date Range")
    date_range = st.sidebar.date_input(
        "Select date range",
        value=(max_date - timedelta(days=30), max_date),
        min_value=min_date, max_value=max_date, key="date_range"
    )
    if len(date_range) == 2:
        start_date, end_date = date_range
        filtered_df = df[(df['date'].dt.date >= start_date) & (df['date'].dt.date <= end_date)].copy()
    else:
        filtered_df = df.copy()
else:
    selected_file = st.sidebar.selectbox(
        "Select Dataset",
        options=['All Files'] + sorted(df['source_file'].unique().tolist()),
        help="Choose a specific RS log file or view all combined"
    )
    if selected_file != 'All Files':
        filtered_df = df[df['source_file'] == selected_file].copy()
    else:
        filtered_df = df.copy()

sectors = ['All'] + sorted(filtered_df['Sector'].dropna().unique().tolist())
selected_sectors = st.sidebar.multiselect("Sector", sectors, default=['All'])
if 'All' not in selected_sectors:
    filtered_df = filtered_df[filtered_df['Sector'].isin(selected_sectors)]

min_percentile = st.sidebar.slider("Min Percentile", 0, 100, 0)
filtered_df = filtered_df[filtered_df['Percentile'] >= min_percentile]

min_rs = st.sidebar.number_input("Min Relative Strength", value=0.0)
filtered_df = filtered_df[filtered_df['Relative Strength'] >= min_rs]

# ---- Build the "All Industries Combined" ticker list ----
def get_combined_ticker_list(filtered_df, industry_df, num_tickers):
    if 'date' in filtered_df.columns:
        latest_date = filtered_df['date'].max()
        latest_snapshot = filtered_df[filtered_df['date'] == latest_date].drop_duplicates(subset=['Ticker'])
    else:
        latest_snapshot = filtered_df.drop_duplicates(subset=['Ticker'])
    top_n = latest_snapshot.nlargest(min(int(num_tickers), len(latest_snapshot)), 'Relative Strength')[['Ticker', 'Industry', 'Relative Strength']].copy()
    if industry_df is not None and not industry_df.empty:
        industry_rs_map = dict(zip(industry_df['Industry'], industry_df['Relative Strength']))
    else:
        industry_rs_map = {}
    top_n['Industry_RS'] = top_n['Industry'].map(industry_rs_map)
    top_n = top_n.sort_values(['Industry_RS', 'Relative Strength'], ascending=[False, False])
    industries_sorted = top_n['Industry'].unique()
    combined_tickers = []
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        combined_tickers.extend(industry_tickers)
    return combined_tickers

combined_tickers = get_combined_ticker_list(filtered_df, df_industry, num_top_tickers)

if not combined_tickers:
    st.sidebar.warning("No tickers match the current filters.")
    st.stop()

# Search box
search_term = st.sidebar.text_input("🔍 Search Ticker", "").strip().upper()
if search_term:
    filtered_tickers = [t for t in combined_tickers if search_term in t]
else:
    filtered_tickers = combined_tickers

# Read-only filtered table
st.sidebar.dataframe(pd.DataFrame({"Ticker": filtered_tickers}), use_container_width=True, hide_index=True)

# Clickable dropdown selection
if filtered_tickers:
    selected_ticker = st.sidebar.selectbox(
        "Select Ticker",
        filtered_tickers,
        index=0,
        key="ticker_select"
    )
    if st.session_state.selected_ticker != selected_ticker:
        st.session_state.selected_ticker = selected_ticker
        st.session_state.current_ticker_index = combined_tickers.index(selected_ticker) if selected_ticker in combined_tickers else 0
        rerun_app()
else:
    st.sidebar.warning("No tickers match your search.")
    st.stop()

# Prev/Next buttons
col_prev, col_next = st.sidebar.columns(2)
with col_prev:
    if st.button("◀ Prev", use_container_width=True):
        idx = st.session_state.current_ticker_index
        if idx > 0:
            st.session_state.current_ticker_index = idx - 1
            st.session_state.selected_ticker = combined_tickers[idx - 1]
            rerun_app()
with col_next:
    if st.button("Next ▶", use_container_width=True):
        idx = st.session_state.current_ticker_index
        if idx + 1 < len(combined_tickers):
            st.session_state.current_ticker_index = idx + 1
            st.session_state.selected_ticker = combined_tickers[idx + 1]
            rerun_app()

# Spacebar listener
st.components.v1.html("""
<script>
window.addEventListener('keydown', function(e) {
    if (e.code === 'Space' && !e.repeat) {
        e.preventDefault();
        const nextBtn = parent.document.querySelector('button[kind="secondary"]:has(span:contains("Next ▶"))');
        if (nextBtn) nextBtn.click();
    }
});
</script>
""", height=0)

if st.session_state.selected_ticker is None or st.session_state.current_ticker_index >= len(combined_tickers):
    st.session_state.selected_ticker = combined_tickers[0]
    st.session_state.current_ticker_index = 0

st.sidebar.caption(f"Selected: **{st.session_state.selected_ticker}** (row {st.session_state.current_ticker_index + 1}/{len(combined_tickers)})")

# ---------------------- Tabs ----------------------
if has_historical:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(
        ["📈 Overview", "📊 Time Series", "🎯 Top Performers", "🔬 Deep Analysis", "📉 Trends", "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table"]
    )
else:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
        ["📈 Overview", "🎯 Top Performers", "📊 Distributions", "🔬 Deep Analysis", "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table"]
    )

# ---------- TAB 1: Overview ----------
with tab1:
    col1, col2, col3, col4 = st.columns(4)
    with col1: st.metric("Total Stocks", len(filtered_df))
    with col2: st.metric("Avg RS", f"{filtered_df['Relative Strength'].mean():.1f}")
    with col3: st.metric("Avg Percentile", f"{filtered_df['Percentile'].mean():.1f}")
    with col4: st.metric("Avg Price", f"${filtered_df['Price'].mean():.2f}")
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
    col1, col2 = st.columns(2)
    with col1:
        sector_counts = filtered_df.drop_duplicates(subset=['Ticker'])['Sector'].value_counts().head(10)
        fig = px.bar(x=sector_counts.values, y=sector_counts.index, orientation='h', title="Top 10 Sectors by Stock Count", labels={'x': 'Count', 'y': 'Sector'})
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        avg_rs_by_sector = filtered_df.drop_duplicates(subset=['Ticker']).groupby('Sector')['Relative Strength'].mean().sort_values(ascending=False).head(10)
        fig = px.bar(x=avg_rs_by_sector.values, y=avg_rs_by_sector.index, orientation='h', title="Top 10 Sectors by Avg RS", labels={'x': 'Avg RS', 'y': 'Sector'}, color=avg_rs_by_sector.values, color_continuous_scale='Viridis')
        st.plotly_chart(fig, use_container_width=True)

# ---------- TAB 2: Time Series (if historical) ----------
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
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            st.subheader("Average Percentile Over Time")
            daily_percentile = filtered_df.groupby('date')['Percentile'].mean().reset_index()
            fig = px.line(daily_percentile, x='date', y='Percentile', title="Daily Average Percentile Trend")
            fig.update_layout(xaxis_title="Date", yaxis_title="Percentile")
            st.plotly_chart(fig, use_container_width=True)
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Stock Count Tracked Over Time")
            daily_count = filtered_df.groupby('date')['Ticker'].nunique().reset_index()
            daily_count.columns = ['date', 'stock_count']
            fig = px.line(daily_count, x='date', y='stock_count', title="Number of Stocks in Universe Over Time")
            fig.update_layout(xaxis_title="Date", yaxis_title="Stock Count")
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            st.subheader("Price Trend (Average)")
            daily_price = filtered_df.groupby('date')['Price'].mean().reset_index()
            fig = px.line(daily_price, x='date', y='Price', title="Average Stock Price Over Time")
            fig.update_layout(xaxis_title="Date", yaxis_title="Avg Price ($)")
            st.plotly_chart(fig, use_container_width=True)
        st.divider()
        st.subheader("📊 Sector RS Trends")
        selected_sector_trend = st.selectbox("Select sector for trend", filtered_df['Sector'].unique(), key="sector_trend")
        sector_trend_data = filtered_df[filtered_df['Sector'] == selected_sector_trend].groupby('date')['Relative Strength'].agg(['mean', 'min', 'max']).reset_index()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['mean'], name='Mean RS', mode='lines+markers'))
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['max'], name='Max RS', fill='tozeroy', mode='lines', opacity=0.2))
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['min'], name='Min RS', fill='tonexty', mode='lines', opacity=0.2))
        fig.update_layout(title=f"{selected_sector_trend} - RS Trend", xaxis_title="Date", yaxis_title="RS Value", hovermode='x unified')
        st.plotly_chart(fig, use_container_width=True)
else:
    with tab2:
        col1, col2 = st.columns(2)
        with col1: fig = px.histogram(filtered_df, x='Relative Strength', nbins=50, title="Distribution of Relative Strength", labels={'Relative Strength': 'RS Value', 'count': 'Frequency'}); st.plotly_chart(fig, use_container_width=True)
        with col2: fig = px.histogram(filtered_df, x='Percentile', nbins=50, title="Distribution of Percentile Rank", labels={'Percentile': 'Percentile', 'count': 'Frequency'}); st.plotly_chart(fig, use_container_width=True)
        col1, col2 = st.columns(2)
        with col1: fig = px.histogram(filtered_df, x='Price', nbins=50, title="Distribution of Stock Price", labels={'Price': 'Price ($)', 'count': 'Frequency'}); st.plotly_chart(fig, use_container_width=True)
        with col2: fig = px.histogram(filtered_df.dropna(subset=['ShortFloatPct']), x='ShortFloatPct', nbins=50, title="Distribution of Short Float %", labels={'ShortFloatPct': 'Short Float %', 'count': 'Frequency'}); st.plotly_chart(fig, use_container_width=True)

# ---------- TAB 3: Top Performers ----------
with tab3:
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🏆 Top 15 by Relative Strength")
        top_rs = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'Relative Strength')[['Rank', 'Ticker', 'Sector', 'Relative Strength', 'Percentile', 'Price']].copy()
        st.dataframe(top_rs.reset_index(drop=True), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("⭐ Top 15 by Percentile")
        top_percentile = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'Percentile')[['Rank', 'Ticker', 'Sector', 'Percentile', 'Relative Strength', 'Price']].copy()
        st.dataframe(top_percentile.reset_index(drop=True), use_container_width=True, hide_index=True)
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("💰 Top 15 by Market Cap")
        top_mcap = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'MarketCap')[['Ticker', 'Sector', 'MarketCap', 'Relative Strength', 'Percentile']].copy()
        top_mcap['MarketCap'] = top_mcap['MarketCap'].apply(lambda x: f"${x/1e9:.2f}B" if pd.notna(x) else "N/A")
        st.dataframe(top_mcap.reset_index(drop=True), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("📈 Highest 6M")
        top_6m = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, '6M_RS_Percentile')[['Ticker', '6M_RS_Percentile', '3M_RS_Percentile', '1M_RS_Percentile']].copy()
        top_6m = top_6m.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
        st.dataframe(top_6m.reset_index(drop=True), use_container_width=True, hide_index=True)

# ---------- TAB 4: Deep Analysis ----------
with tab4:
    col1, col2 = st.columns(2)
    with col1:
        fig = px.scatter(filtered_df, x='Price', y='Relative Strength', color='Percentile', hover_data=['Ticker', 'Sector'], title="Relative Strength vs Price", color_continuous_scale='Viridis')
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        percentile_data = filtered_df[['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile']].mean()
        fig = go.Figure(data=[go.Bar(name='1M', x=['1M'], y=[percentile_data['1M_RS_Percentile']]), go.Bar(name='3M', x=['3M'], y=[percentile_data['3M_RS_Percentile']]), go.Bar(name='6M', x=['6M'], y=[percentile_data['6M_RS_Percentile']])])
        fig.update_layout(title="Average RS Percentile Comparison", barmode='group', yaxis_title="Avg Percentile")
        st.plotly_chart(fig, use_container_width=True)
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        fig = px.histogram(filtered_df.dropna(subset=['PctFrom52WkHigh']), x='PctFrom52WkHigh', nbins=40, title="Distribution of % from 52W High", labels={'PctFrom52WkHigh': '% From 52W High', 'count': 'Frequency'})
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        fig = px.scatter(filtered_df.dropna(subset=['RevenueGrowth']), x='RevenueGrowth', y='Relative Strength', color='Percentile', hover_data=['Ticker', 'Sector'], title="Revenue Growth vs Relative Strength", color_continuous_scale='Viridis')
        st.plotly_chart(fig, use_container_width=True)
    st.divider()
    st.subheader("Key Statistics Summary")
    summary_stats = filtered_df[['Relative Strength', 'Percentile', '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile', 'Price', 'AvgVol10', 'AvgVol30', 'AvgVol50', 'ShortFloatPct', 'PctFrom52WkHigh', 'RevenueGrowth']].describe()
    st.dataframe(summary_stats, use_container_width=True)

# ---------- TAB 5: Trends (if historical) ----------
if has_historical:
    with tab5:
        st.subheader("📉 Trend Analysis")
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Stock Momentum by Sector")
            latest_date = filtered_df['date'].max()
            oldest_date = filtered_df['date'].min()
            latest_rs = filtered_df[filtered_df['date'] == latest_date].groupby('Sector')['Relative Strength'].mean()
            oldest_rs = filtered_df[filtered_df['date'] == oldest_date].groupby('Sector')['Relative Strength'].mean()
            momentum = (latest_rs - oldest_rs).sort_values(ascending=False)
            fig = px.bar(x=momentum.values, y=momentum.index, orientation='h', title=f"RS Change by Sector ({oldest_date.date()} to {latest_date.date()})", labels={'x': 'RS Change', 'y': 'Sector'}, color=momentum.values, color_continuous_scale='RdYlGn')
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            st.subheader("Top Gainers in RS")
            latest_stocks = filtered_df[filtered_df['date'] == latest_date][['Ticker', 'Sector', 'Relative Strength']].drop_duplicates(subset=['Ticker'], keep='first').copy()
            oldest_stocks = filtered_df[filtered_df['date'] == oldest_date][['Ticker', 'Relative Strength']].drop_duplicates(subset=['Ticker'], keep='first').copy()
            if not latest_stocks.empty and not oldest_stocks.empty:
                merged = latest_stocks.merge(oldest_stocks, on='Ticker', suffixes=('_latest', '_oldest'))
                merged['RS_change'] = merged['Relative Strength_latest'] - merged['Relative Strength_oldest']
                top_gainers = merged.nlargest(10, 'RS_change')[['Ticker', 'Sector', 'RS_change']]
                fig = px.bar(top_gainers, x='RS_change', y='Ticker', orientation='h', title="Top 10 RS Gainers", labels={'RS_change': 'RS Change', 'Ticker': 'Ticker'}, color='RS_change', color_continuous_scale='Greens')
                st.plotly_chart(fig, use_container_width=True)
        st.divider()
        st.subheader("🔍 Individual Stock Trajectory")
        selected_ticker = st.selectbox("Select ticker to analyze", sorted(filtered_df['Ticker'].dropna().unique()), key="trajectory_ticker")
        ticker_data = filtered_df[filtered_df['Ticker'] == selected_ticker].sort_values('date')
        if not ticker_data.empty:
            col1, col2 = st.columns(2)
            with col1: fig = px.line(ticker_data, x='date', y='Relative Strength', title=f"{selected_ticker} - RS Trend", markers=True); fig.update_layout(xaxis_title="Date", yaxis_title="RS Value"); st.plotly_chart(fig, use_container_width=True)
            with col2: fig = px.line(ticker_data, x='date', y='Percentile', title=f"{selected_ticker} - Percentile Trend", markers=True, color_discrete_sequence=['orange']); fig.update_layout(xaxis_title="Date", yaxis_title="Percentile"); st.plotly_chart(fig, use_container_width=True)
            col1, col2 = st.columns(2)
            with col1: fig = px.line(ticker_data, x='date', y='Price', title=f"{selected_ticker} - Price Trend", markers=True, color_discrete_sequence=['purple']); fig.update_layout(xaxis_title="Date", yaxis_title="Price ($)"); st.plotly_chart(fig, use_container_width=True)
            with col2: fig = px.bar(ticker_data, x='date', y='Rank', title=f"{selected_ticker} - Rank Over Time", color='Rank', color_continuous_scale='Reds_r'); fig.update_layout(xaxis_title="Date", yaxis_title="Rank (Lower is Better)"); st.plotly_chart(fig, use_container_width=True)

# ---------- TAB 6: Industry Rotation ----------
with (tab6 if has_historical else tab5):
    st.subheader("🏭 Industry Rotation & Analysis")
    if df_industry is not None and not df_industry.empty:
        ind_display = df_industry.copy()
        has_ranks = all(col in ind_display.columns for col in ['1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank'])
        has_percentiles = all(col in ind_display.columns for col in ['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile'])
        if has_ranks:
            ind_display['Delta 1M-3M'] = ind_display['3M_RS_Rank'] - ind_display['1M_RS_Rank']
            ind_display['Delta 1M-6M'] = ind_display['6M_RS_Rank'] - ind_display['1M_RS_Rank']
            ind_display = ind_display.rename(columns={'1M_RS_Rank': '1M', '3M_RS_Rank': '3M', '6M_RS_Rank': '6M'})
            int_cols = ['Rank', '1M', '3M', '6M', 'Delta 1M-3M', 'Delta 1M-6M']
            for c in int_cols:
                if c in ind_display.columns:
                    ind_display[c] = pd.to_numeric(ind_display[c], errors='coerce')
                    ind_display[c] = ind_display[c].round().astype('Int64')
            cols_to_show = ['Rank', 'Industry', '1M', '3M', '6M', 'Delta 1M-3M', 'Delta 1M-6M', 'Top 5 Tickers']
        elif has_percentiles:
            ind_display['Delta 1M-3M'] = ind_display['1M_RS_Percentile'] - ind_display['3M_RS_Percentile']
            ind_display['Delta 1M-6M'] = ind_display['1M_RS_Percentile'] - ind_display['6M_RS_Percentile']
            ind_display = ind_display.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
            pct_cols = ['1M', '3M', '6M', 'Delta 1M-3M', 'Delta 1M-6M']
            for c in pct_cols:
                if c in ind_display.columns:
                    ind_display[c] = pd.to_numeric(ind_display[c], errors='coerce')
                    ind_display[c] = ind_display[c].round().astype('Int64')
            cols_to_show = ['Rank', 'Industry', '1M', '3M', '6M', 'Delta 1M-3M', 'Delta 1M-6M', 'Top 5 Tickers']
        else:
            cols_to_show = ['Rank', 'Industry']
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
        def color_delta(val):
            if pd.isna(val): return ''
            try:
                val_float = float(val)
                color = 'green' if val_float > 0 else 'red' if val_float < 0 else 'gray'
                return f'color: {color}'
            except ValueError:
                return ''
        styled_df = display_data.style
        delta_cols = [c for c in ['Delta 1M-3M', 'Delta 1M-6M'] if c in available_cols]
        if delta_cols:
            styled_df = styled_df.map(color_delta, subset=delta_cols)
        st.dataframe(styled_df, hide_index=True)
    else:
        st.warning("Industry RS data not found. Please check rs_industries.csv file.")

# ---------------------- Ticker Data Caching ----------------------
TICKER_CACHE_DIR = Path(__file__).resolve().parent / "ticker_cache"
TICKER_CACHE_DIR.mkdir(exist_ok=True)

def get_ticker_cache_path(ticker, interval):
    return TICKER_CACHE_DIR / f"{ticker}_{interval}.parquet"

def load_or_fetch_ticker(ticker, interval="1d", period="2y"):
    cache_path = get_ticker_cache_path(ticker, interval)
    today = pd.Timestamp.now().normalize()
    if cache_path.exists():
        try:
            df = pd.read_parquet(cache_path)
            df.index = pd.to_datetime(df.index)
            last_date = df.index.max().normalize()
            if (today - last_date).days <= 2:
                return df
            else:
                start = last_date + pd.Timedelta(days=1)
                end = today + pd.Timedelta(days=1)
                ticker_obj = yf.Ticker(ticker)
                new_data = ticker_obj.history(start=start, end=end, interval=interval)
                if not new_data.empty:
                    df = pd.concat([df, new_data])
                    df = df[~df.index.duplicated(keep='first')].sort_index()
                    df.to_parquet(cache_path)
                return df
        except Exception as e:
            print(f"Error loading cache for {ticker} ({interval}): {e}")
    ticker_obj = yf.Ticker(ticker)
    df = ticker_obj.history(period=period, interval=interval)
    if not df.empty and cache_path.parent.exists():
        df.to_parquet(cache_path)
    return df

def load_or_fetch_weekly(ticker):
    return load_or_fetch_ticker(ticker, interval="1wk", period="3y")

# ---------------------- RS Signal Detection Functions (for Plotly) ----------------------
def compute_rs_signals(df, spy_series, scaling_factor=7.0):
    rs_raw = df['Close'] * scaling_factor * 1000 / spy_series
    rs_ema_quick = rs_raw.ewm(span=21, adjust=False).mean()
    rs_ema_quicksand = rs_raw.ewm(span=34, adjust=False).mean()
    rs_ema_gd = rs_raw.ewm(span=50, adjust=False).mean()
    
    quick_break = (rs_raw < rs_ema_quick) & (rs_raw.shift(1) >= rs_ema_quick.shift(1))
    gd_break = (rs_raw < rs_ema_gd) & (rs_raw.shift(1) >= rs_ema_gd.shift(1))
    rs_reclaim = (rs_raw > rs_ema_quick) & (rs_raw.shift(1) <= rs_ema_quick.shift(1))
    
    quicksand = pd.Series(False, index=df.index)
    last_break_level = None
    for i in range(1, len(df)):
        if quick_break.iloc[i]:
            last_break_level = rs_raw.iloc[i]
        if last_break_level is not None and rs_raw.iloc[i] < rs_ema_quick.iloc[i] and rs_raw.iloc[i] < last_break_level:
            quicksand.iloc[i] = True
    
    lookbacks = {'1Y': 252, '6M': 126, '3M': 63}
    rs_new_high = {}
    rs_new_low = {}
    price_new_high = {}
    for name, lb in lookbacks.items():
        if len(df) > lb:
            rs_new_high[name] = rs_raw > rs_raw.shift(1).rolling(lb, min_periods=lb).max()
            rs_new_low[name] = rs_raw < rs_raw.shift(1).rolling(lb, min_periods=lb).min()
            price_new_high[name] = df['High'] > df['High'].shift(1).rolling(lb, min_periods=lb).max()
        else:
            rs_new_high[name] = pd.Series(False, index=df.index)
            rs_new_low[name] = pd.Series(False, index=df.index)
            price_new_high[name] = pd.Series(False, index=df.index)
    
    rs_nh_any = rs_new_high['1Y'] | rs_new_high['6M'] | rs_new_high['3M']
    rs_leads = pd.Series(False, index=df.index)
    for i in range(len(df)):
        if rs_new_high['1Y'].iloc[i] and not price_new_high['1Y'].iloc[i]:
            rs_leads.iloc[i] = True
        elif rs_new_high['6M'].iloc[i] and not price_new_high['6M'].iloc[i]:
            rs_leads.iloc[i] = True
        elif rs_new_high['3M'].iloc[i] and not price_new_high['3M'].iloc[i]:
            rs_leads.iloc[i] = True
    
    rs_nl_any = rs_new_low['1Y']
    
    signals = pd.DataFrame({
        'rs_raw': rs_raw,
        'rs_ema_quick': rs_ema_quick,
        'rs_ema_quicksand': rs_ema_quicksand,
        'rs_ema_gd': rs_ema_gd,
        'quick_break': quick_break,
        'gd_break': gd_break,
        'rs_reclaim': rs_reclaim,
        'quicksand': quicksand,
        'rs_new_high_any': rs_nh_any,
        'rs_leads_price': rs_leads,
        'rs_new_low': rs_nl_any
    })
    return signals

# ---------- TAB 7 (or 6): Company Details with Full Pattern Recognition ----------
with (tab7 if has_historical else tab6):
    st.subheader("💼 Company Details")
    selected_ticker_company = st.session_state.selected_ticker
    if selected_ticker_company is None:
        st.warning("No ticker selected in the sidebar table.")
    else:
        st.write(f"Showing details for **{selected_ticker_company}** (selected from sidebar table)")
        show_log_price = st.checkbox("Show log(Price) (natural log) instead of Price", key="log_price_toggle")
        ticker_rows = filtered_df[filtered_df['Ticker'] == selected_ticker_company]
        if ticker_rows.empty:
            st.warning(f"No data found for {selected_ticker_company}")
        else:
            if 'date' in ticker_rows.columns:
                latest_row = ticker_rows.sort_values('date').iloc[-1]
            else:
                latest_row = ticker_rows.iloc[0]
            
            def get_display_price(price_value):
                if pd.isna(price_value):
                    return 'N/A'
                if show_log_price:
                    if price_value > 0:
                        return f"{np.log(price_value):.4f}"
                    else:
                        return 'N/A (invalid price for log)'
                else:
                    return f"${price_value:.2f}"
            
            # Build snapshot items (shortened labels)
            label_map = {
                'Sector': 'Sector', 'Industry': 'Industry', 'Price': 'Price',
                'MarketCap': 'Mkt Cap', 'AvgVol30': 'Avg Vol', 'PctFrom52WkHigh': '52W High %',
                'Relative Strength': 'RS', 'Percentile': 'Pctl',
                '1M_RS_Percentile': '1M RS', '3M_RS_Percentile': '3M RS', '6M_RS_Percentile': '6M RS'
            }
            items = []
            for k, display in label_map.items():
                if k in latest_row.index:
                    val = latest_row[k]
                    if pd.isna(val):
                        val = 'N/A'
                    elif k == 'MarketCap' and pd.notna(val):
                        try:
                            val = f"${float(val)/1e9:.1f}B"
                        except Exception:
                            val = str(val)
                    elif k == 'Price':
                        val = get_display_price(val)
                        if show_log_price:
                            display = "log Price"
                        else:
                            display = "Price"
                    elif k in ['AvgVol30', 'AvgVol50', 'AvgVol10']:
                        try:
                            val = f"{int(val):,}"
                        except:
                            val = str(val)
                    elif k in ['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile']:
                        try:
                            val = f"{int(round(float(val)))}"
                        except:
                            val = str(val)
                    else:
                        val = str(val)
                    items.append((display, val))
            
            snapshot_text = f"<b>{selected_ticker_company}</b><br>"
            for display, value in items:
                snapshot_text += f"{display}: {value}<br>"
            snapshot_text = snapshot_text.rstrip('<br>')
            
            st.divider()
            st.subheader("📈 Price & Volume History (2 Years)")
            with st.spinner(f"Fetching historical data for {selected_ticker_company}..."):
                try:
                    # ========== Load data ==========
                    df_daily_full = load_or_fetch_ticker(selected_ticker_company, interval="1d", period="2y")
                    df_weekly = load_or_fetch_weekly(selected_ticker_company)
                    df_daily = df_daily_full.iloc[-252:] if len(df_daily_full) > 252 else df_daily_full
                    
                    # SPY for RS calculation
                    spy = yf.Ticker("^GSPC")
                    spy_daily_full = spy.history(period="2y", interval="1d")
                    spy_daily = spy_daily_full.iloc[-252:] if len(spy_daily_full) > 252 else spy_daily_full
                    spy_weekly = spy.history(period="3y", interval="1wk")
                    
                    common_idx = df_daily.index.intersection(spy_daily.index)
                    df_daily = df_daily.loc[common_idx]
                    spy_daily = spy_daily.loc[common_idx]
                    
                    if not df_daily.empty and not df_weekly.empty:
                        # ========== Run pattern recognizer on full data ==========
                        recognizer = PatternRecognizer(base_depth=0.50, base_length=65*5, db_depth=0.50, db_length=65*5, pivot_length=9)
                        for i, (_, row) in enumerate(df_daily_full.iterrows()):
                            recognizer.process_bar(row['High'], row['Low'], row['Close'], i)
                        
                        # Get pattern info for plotting (on the visible slice)
                        base_start_bar, base_high, base_low = recognizer.get_base_info()
                        cup_curve_x, cup_curve_y = recognizer.get_cup_curve()
                        db_info = recognizer.get_double_bottom_info()
                        
                        # RS Signals
                        signals = compute_rs_signals(df_daily, spy_daily['Close'], scaling_factor=7.0)
                        df_daily = pd.concat([df_daily, signals], axis=1)
                        
                        # ========== Volume indicators ==========
                        vol = df_daily['Volume'].astype(float)
                        close = df_daily['Close'].astype(float)
                        avg_vol = vol.rolling(window=50, min_periods=1).mean()
                        avg_vol_safe = avg_vol.replace(0, np.nan)
                        vol_ratio = (vol / avg_vol_safe * 100).fillna(0)
                        dry_lvl1 = (vol_ratio < 55).astype(bool)
                        dry_lvl2 = (vol_ratio < 40).astype(bool)
                        up_day = (close > close.shift(1)).astype(bool)
                        down_day = (close < close.shift(1)).astype(bool)
                        day_range = df_daily['High'] - df_daily['Low']
                        close_chg_pct = (close - close.shift(1)) / close.shift(1) * 100
                        close_chg_pct = close_chg_pct.replace([np.inf, -np.inf], np.nan).fillna(0)
                        in_lower_27 = (close <= df_daily['Low'] + 0.27 * day_range).astype(bool)
                        stall_day = ((close_chg_pct >= 0) & (close_chg_pct < 0.1) & in_lower_27).astype(bool)
                        churn_vol = (vol >= avg_vol * 1.2).astype(bool)
                        churn_pct = (np.abs(close_chg_pct) < 0.25).astype(bool)
                        in_lower_50 = (close <= df_daily['Low'] + 0.5 * day_range).astype(bool)
                        churn_day = (churn_vol & churn_pct & in_lower_50 & (~stall_day)).astype(bool)
                        
                        def pocket_pivot_bool(vol_series, close_series, window):
                            result = pd.Series(False, index=vol_series.index)
                            for i in range(window, len(vol_series)):
                                window_slice = slice(i - window, i)
                                up_volumes = vol_series.iloc[window_slice][close_series.iloc[window_slice] > close_series.iloc[window_slice].shift(1)]
                                if len(up_volumes) > 0:
                                    max_up_vol = up_volumes.max()
                                    result.iloc[i] = (vol_series.iloc[i] > max_up_vol) and (close_series.iloc[i] > close_series.iloc[i-1])
                                else:
                                    result.iloc[i] = False
                            return result
                        
                        pp10 = pocket_pivot_bool(vol, close, 10)
                        pp5 = pocket_pivot_bool(vol, close, 5)
                        pp5_only = pp5 & ~pp10
                        lower_low = (close < close.shift(1)).astype(bool)
                        rising_vol = (vol > vol.shift(1)).astype(bool)
                        ll3 = (lower_low & rising_vol).astype(bool)
                        ll3_signal = (ll3 & ll3.shift(1) & ll3.shift(2)).astype(bool)
                        marker_y = vol * 1.05
                        
                        rs_value = latest_row.get('Percentile')
                        rs_label = f"{int(round(rs_value))}" if rs_value and not np.isnan(rs_value) else None
                        
                        # ========== Daily chart type selector ==========
                        chart_type = st.radio("📊 Daily Chart Type", ["Plotly (Advanced)", "TradingView Lightweight"], horizontal=True, key="daily_chart_type")
                        
                        def create_daily_chart_with_patterns(df, base_info, cup_curve, db_info, percentile, snapshot_text):
                            ema10 = df['Close'].ewm(span=10, adjust=False).mean()
                            ema21 = df['Close'].ewm(span=21, adjust=False).mean()
                            fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                                                subplot_titles=(f"{selected_ticker_company} Daily", 'Volume with Indicators', 'Raw RS & QGDRS EMAs'),
                                                row_heights=[0.5, 0.2, 0.3])
                            # Price pane
                            fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'], name='Price', showlegend=False), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df.index, y=ema10, mode='lines', name='EMA(10)', line=dict(color='#FF9800', width=2)), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df.index, y=ema21, mode='lines', name='EMA(21)', line=dict(color='#2196F3', width=2)), row=1, col=1)
                            
                            # Base lines
                            if base_info[0] is not None:
                                start_bar, high_price, low_price = base_info
                                start_date = df.index[start_bar] if start_bar < len(df) else df.index[0]
                                fig.add_hline(y=high_price, line_dash="dot", line_color="rgb(146,193,131)", line_width=2, row=1, col=1)
                                fig.add_hline(y=low_price, line_dash="solid", line_color="rgb(146,193,131)", line_width=2, row=1, col=1)
                                fig.add_vrect(x0=start_date, x1=df.index[-1], fillcolor="rgba(146,193,131,0.05)", line_width=0, row=1, col=1)
                            
                            # Cup curve
                            if cup_curve[0] is not None:
                                x_idx, y_vals = cup_curve
                                dates = [df.index[i] for i in x_idx if 0 <= i < len(df)]
                                y_vals_filtered = [y_vals[j] for j, i in enumerate(x_idx) if 0 <= i < len(df)]
                                if dates:
                                    fig.add_trace(go.Scatter(x=dates, y=y_vals_filtered, mode='lines', name='Cup Pattern', line=dict(color='rgb(146,193,131)', width=3)), row=1, col=1)
                            
                            # Double Bottom lines
                            if db_info[0] is not None:
                                top_bar, top_price, mid_bar, mid_price, bottom_bar, bottom_price = db_info
                                if top_bar < len(df) and mid_bar < len(df) and bottom_bar < len(df):
                                    fig.add_trace(go.Scatter(x=[df.index[top_bar], df.index[mid_bar]], y=[top_price, mid_price], mode='lines', line=dict(color='rgb(146,193,131)', width=2), showlegend=False), row=1, col=1)
                                    fig.add_trace(go.Scatter(x=[df.index[mid_bar], df.index[bottom_bar]], y=[mid_price, bottom_price], mode='lines', line=dict(color='rgb(146,193,131)', width=2), showlegend=False), row=1, col=1)
                                    fig.add_trace(go.Scatter(x=[df.index[bottom_bar], df.index[-1]], y=[bottom_price, df['Low'].iloc[-1]], mode='lines', line=dict(color='rgb(146,193,131)', width=2), showlegend=False), row=1, col=1)
                            
                            # Volume pane
                            vol_colors = []
                            for idx in df.index:
                                if dry_lvl2.loc[idx]:
                                    vol_colors.append('rgba(247, 153, 2, 0.7)')
                                elif dry_lvl1.loc[idx]:
                                    vol_colors.append('rgba(225, 181, 69, 0.6)')
                                elif up_day.loc[idx]:
                                    vol_colors.append('#26a69a')
                                else:
                                    vol_colors.append('#ef5350')
                            fig.add_trace(go.Bar(x=df.index, y=df['Volume'], marker=dict(color=vol_colors), name='Volume', showlegend=False), row=2, col=1)
                            fig.add_trace(go.Scatter(x=df.index, y=avg_vol, name='Volume SMA(50)', line=dict(color='orange', width=1.5)), row=2, col=1)
                            if not pp10.loc[pp10].empty:
                                fig.add_trace(go.Scatter(x=pp10[pp10].index, y=marker_y[pp10], mode='markers', marker=dict(symbol='diamond', size=12, color='yellow'), name='Pocket Pivot (10d)', showlegend=True), row=2, col=1)
                            if not pp5_only.loc[pp5_only].empty:
                                fig.add_trace(go.Scatter(x=pp5_only[pp5_only].index, y=marker_y[pp5_only]*0.98, mode='markers', marker=dict(symbol='diamond', size=10, color='blue'), name='Pocket Pivot (5d)', showlegend=True), row=2, col=1)
                            if not churn_day.loc[churn_day].empty:
                                fig.add_trace(go.Scatter(x=churn_day[churn_day].index, y=marker_y[churn_day], mode='markers', marker=dict(symbol='x', size=12, color='purple'), name='Churning Day', showlegend=True), row=2, col=1)
                            if not stall_day.loc[stall_day].empty:
                                fig.add_trace(go.Scatter(x=stall_day[stall_day].index, y=marker_y[stall_day], mode='markers', marker=dict(symbol='circle', size=10, color='maroon', line=dict(width=2, color='black')), name='Stall Day', showlegend=True), row=2, col=1)
                            if not ll3_signal.loc[ll3_signal].empty:
                                fig.add_trace(go.Scatter(x=ll3_signal[ll3_signal].index, y=marker_y[ll3_signal], mode='markers', marker=dict(symbol='triangle-up', size=12, color='red'), name='3LL Signal', showlegend=True), row=2, col=1)
                            
                            # RS pane
                            fig.add_trace(go.Scatter(x=df.index, y=df['rs_raw'], name='Raw RS', line=dict(color='blue', width=2)), row=3, col=1)
                            fig.add_trace(go.Scatter(x=df.index, y=df['rs_ema_quick'], name='Quick EMA (21)', line=dict(color='#56b8e6', width=2)), row=3, col=1)
                            fig.add_trace(go.Scatter(x=df.index, y=df['rs_ema_quicksand'], name='Quicksand EMA (34)', line=dict(color='#ff8c00', width=2)), row=3, col=1)
                            fig.add_trace(go.Scatter(x=df.index, y=df['rs_ema_gd'], name='GD EMA (50)', line=dict(color='#2ca02c', width=2)), row=3, col=1)
                            
                            # QGDRS signals
                            qb = df[df['quick_break']]
                            if not qb.empty:
                                fig.add_trace(go.Scatter(x=qb.index, y=qb['rs_ema_quick'], mode='markers', marker=dict(symbol='x', size=10, color='yellow'), name='Quick Break', hovertemplate='Quick Break<extra></extra>'), row=3, col=1)
                            qs = df[df['quicksand']]
                            if not qs.empty:
                                fig.add_trace(go.Scatter(x=qs.index, y=qs['rs_ema_quick'], mode='markers', marker=dict(symbol='x', size=8, color='orange'), name='Quicksand', hovertemplate='Quicksand<extra></extra>'), row=3, col=1)
                            gd = df[df['gd_break']]
                            if not gd.empty:
                                fig.add_trace(go.Scatter(x=gd.index, y=gd['rs_ema_gd'], mode='markers', marker=dict(symbol='x', size=10, color='red'), name='Grateful Dead Break', hovertemplate='Grateful Dead Break<extra></extra>'), row=3, col=1)
                            reclaim = df[df['rs_reclaim']]
                            if not reclaim.empty:
                                fig.add_trace(go.Scatter(x=reclaim.index, y=reclaim['rs_ema_quick'], mode='markers', marker=dict(symbol='triangle-up', size=8, color='lime'), name='RS Reclaim', hovertemplate='RS Reclaim<extra></extra>'), row=3, col=1)
                            nh = df[df['rs_new_high_any']]
                            if not nh.empty:
                                fig.add_trace(go.Scatter(x=nh.index, y=nh['rs_raw'], mode='markers', marker=dict(symbol='circle', size=6, color='#0000FF', opacity=0.6), name='RS New High', hovertemplate='RS New High<extra></extra>'), row=3, col=1)
                            leads = df[df['rs_leads_price']]
                            if not leads.empty:
                                fig.add_trace(go.Scatter(x=leads.index, y=leads['rs_raw'], mode='markers', marker=dict(symbol='circle', size=8, color='#00ffd9'), name='RS Leads Price', hovertemplate='RS Leads Price<extra></extra>'), row=3, col=1)
                            nl = df[df['rs_new_low']]
                            if not nl.empty:
                                fig.add_trace(go.Scatter(x=nl.index, y=nl['rs_raw'], mode='markers', marker=dict(symbol='circle', size=6, color='#FF0000', opacity=0.6), name='RS New Low', hovertemplate='RS New Low<extra></extra>'), row=3, col=1)
                            
                            fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
                            if percentile is not None and not np.isnan(percentile):
                                last_date = df.index[-1]
                                last_close = df['Close'].iloc[-1]
                                fig.add_annotation(x=last_date, y=last_close, text=f"{int(round(percentile))}", showarrow=True, arrowhead=2, arrowsize=1.5, arrowcolor='#636efa', ax=40, ay=-40, bgcolor="rgba(255,255,255,0.85)", bordercolor="#636efa", borderwidth=1.5, font=dict(size=13, color="#636efa"))
                            fig.add_annotation(x=0, y=0.98, xref="paper", yref="paper", text=snapshot_text, showarrow=False, align="left", bgcolor="rgba(255,255,255,0.85)", font=dict(color="#1e1e1e", size=11, family="monospace"), bordercolor="#cccccc", borderwidth=1, borderpad=8, xanchor="left", yanchor="top")
                            fig.update_layout(xaxis_rangeslider_visible=False, height=800, margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5))
                            return fig
                        
                        if chart_type == "TradingView Lightweight":
                            st.subheader(f"📈 {selected_ticker_company} Daily (TradingView Lightweight Charts)")
                            with st.expander("🎯 Add Chart Markers", expanded=False):
                                col_m1, col_m2 = st.columns(2)
                                with col_m1: marker_date = st.date_input("Marker Date", key="daily_marker_date")
                                with col_m2: marker_price = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="daily_marker_price")
                                col_m3, col_m4, col_m5 = st.columns(3)
                                with col_m3: marker_position = st.selectbox("Position", ["aboveBar", "belowBar"], key="daily_marker_pos")
                                with col_m4: marker_shape = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="daily_marker_shape")
                                with col_m5: marker_color = st.color_picker("Color", "#FF0000", key="daily_marker_color")
                                marker_text = st.text_input("Marker Label", "", key="daily_marker_text")
                                add_marker = st.button("➕ Add Marker", key="daily_add_marker")
                                session_key = f"daily_markers_{selected_ticker}"
                                if session_key not in st.session_state:
                                    st.session_state[session_key] = load_markers(selected_ticker, "daily")
                                if add_marker and marker_date and marker_price > 0:
                                    import datetime as dt
                                    marker_timestamp = int(dt.datetime.combine(marker_date, dt.time()).timestamp())
                                    new_marker = {'time': marker_timestamp, 'position': marker_position, 'color': marker_color, 'shape': marker_shape, 'text': marker_text or f"${marker_price:.2f}"}
                                    st.session_state[session_key].append(new_marker)
                                    save_markers(selected_ticker, st.session_state[session_key], "daily")
                                    st.success(f"✅ Marker saved for {selected_ticker} at {marker_date}")
                                    rerun_app()
                                current_markers = st.session_state.get(session_key, [])
                                if current_markers:
                                    st.write(f"**Active Markers for {selected_ticker}:**")
                                    for i, m in enumerate(current_markers):
                                        col_a, col_b = st.columns([4,1])
                                        with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                        with col_b:
                                            if st.button("❌", key=f"delete_daily_marker_{i}"):
                                                st.session_state[session_key].pop(i)
                                                save_markers(selected_ticker, st.session_state[session_key], "daily")
                                                st.success(f"✅ Marker deleted for {selected_ticker}")
                                                rerun_app()
                            # Build volume data for Lightweight
                            vol_colored = []
                            for idx, row in df_daily.iterrows():
                                ts = int(idx.timestamp())
                                val = float(row['Volume'])
                                if dry_lvl2.loc[idx]:
                                    color = 'rgba(247, 153, 2, 0.7)'
                                elif dry_lvl1.loc[idx]:
                                    color = 'rgba(225, 181, 69, 0.6)'
                                elif up_day.loc[idx]:
                                    color = '#26a69a'
                                else:
                                    color = '#ef5350'
                                vol_colored.append({'time': ts, 'value': val, 'color': color})
                            df_daily['volume_sma50'] = df_daily['Volume'].rolling(50).mean()
                            volume_sma50_data = [{'time': int(idx.timestamp()), 'value': float(v)} for idx, v in df_daily['volume_sma50'].items() if pd.notna(v)]
                            pp10_dates = [int(idx.timestamp()) for idx in df_daily.index if pp10.loc[idx]]
                            pp5_dates = [int(idx.timestamp()) for idx in df_daily.index if pp5_only.loc[idx]]
                            churn_dates = [int(idx.timestamp()) for idx in df_daily.index if churn_day.loc[idx]]
                            stall_dates = [int(idx.timestamp()) for idx in df_daily.index if stall_day.loc[idx]]
                            ll3_dates = [int(idx.timestamp()) for idx in df_daily.index if ll3_signal.loc[idx]]
                            lw_user_markers = st.session_state.get(session_key, []).copy()
                            
                            st_html(create_lightweight_candlestick_html(
                                df_daily, height=600, markers=lw_user_markers, rs_label=rs_label,
                                rs_raw=df_daily['rs_raw'], rs_quick=df_daily['rs_ema_quick'],
                                rs_quicksand=df_daily['rs_ema_quicksand'], rs_gd=df_daily['rs_ema_gd'],
                                volume_data=vol_colored, volume_sma50=volume_sma50_data,
                                pp10_dates=pp10_dates, pp5_dates=pp5_dates,
                                churn_dates=churn_dates, stall_dates=stall_dates, ll3_dates=ll3_dates
                            ), height=620)
                        else:
                            st.subheader(f"📈 {selected_ticker_company} Daily (Plotly)")
                            with st.expander("🎯 Add Chart Markers", expanded=False):
                                col_m1, col_m2 = st.columns(2)
                                with col_m1: marker_date = st.date_input("Marker Date", key="plotly_daily_marker_date")
                                with col_m2: marker_price = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="plotly_daily_marker_price")
                                col_m3, col_m4, col_m5 = st.columns(3)
                                with col_m3: marker_position = st.selectbox("Position", ["aboveBar", "belowBar"], key="plotly_daily_marker_pos")
                                with col_m4: marker_shape = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="plotly_daily_marker_shape")
                                with col_m5: marker_color = st.color_picker("Color", "#FF0000", key="plotly_daily_marker_color")
                                marker_text = st.text_input("Marker Label", "", key="plotly_daily_marker_text")
                                add_marker = st.button("➕ Add Marker", key="plotly_daily_add_marker")
                                session_key = f"daily_markers_{selected_ticker}"
                                if session_key not in st.session_state:
                                    st.session_state[session_key] = load_markers(selected_ticker, "daily")
                                if add_marker and marker_date and marker_price > 0:
                                    import datetime as dt
                                    marker_timestamp = int(dt.datetime.combine(marker_date, dt.time()).timestamp())
                                    new_marker = {'time': marker_timestamp, 'position': marker_position, 'color': marker_color, 'shape': marker_shape, 'text': marker_text or f"${marker_price:.2f}"}
                                    st.session_state[session_key].append(new_marker)
                                    save_markers(selected_ticker, st.session_state[session_key], "daily")
                                    st.success(f"✅ Marker saved for {selected_ticker} at {marker_date}")
                                    rerun_app()
                                current_markers = st.session_state.get(session_key, [])
                                if current_markers:
                                    st.write(f"**Active Markers for {selected_ticker}:**")
                                    for i, m in enumerate(current_markers):
                                        col_a, col_b = st.columns([4,1])
                                        with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                        with col_b:
                                            if st.button("❌", key=f"delete_plotly_daily_marker_{i}"):
                                                st.session_state[session_key].pop(i)
                                                save_markers(selected_ticker, st.session_state[session_key], "daily")
                                                st.success(f"✅ Marker deleted for {selected_ticker}")
                                                rerun_app()
                            base_info = (base_start_bar, base_high, base_low) if base_start_bar is not None else (None, None, None)
                            cup_curve_pair = (cup_curve_x, cup_curve_y) if cup_curve_x is not None else (None, None)
                            st.plotly_chart(create_daily_chart_with_patterns(df_daily, base_info, cup_curve_pair, db_info, latest_row.get('Percentile'), snapshot_text), use_container_width=True)
                        
                        # ========== Weekly chart (full) ==========
                        st.divider()
                        st.subheader("📊 Weekly Chart")
                        chart_type_weekly = st.radio("Weekly Chart Type", ["Plotly (Advanced)", "TradingView Lightweight"], horizontal=True, key="weekly_chart_type")
                        
                        # Weekly volume indicators
                        vol_w = df_weekly['Volume'].astype(float)
                        close_w = df_weekly['Close'].astype(float)
                        avg_vol_w = vol_w.rolling(window=10, min_periods=1).mean()
                        avg_vol_safe_w = avg_vol_w.replace(0, np.nan)
                        vol_ratio_w = (vol_w / avg_vol_safe_w * 100).fillna(0)
                        dry_lvl1_w = (vol_ratio_w < 55).astype(bool)
                        dry_lvl2_w = (vol_ratio_w < 40).astype(bool)
                        up_day_w = (close_w > close_w.shift(1)).astype(bool)
                        down_day_w = (close_w < close_w.shift(1)).astype(bool)
                        acc_day_w = (up_day_w & (vol_w > avg_vol_w)).astype(bool)
                        dist_day_w = (down_day_w & (vol_w > avg_vol_w)).astype(bool)
                        
                        # RS for weekly (simple)
                        if not spy_weekly.empty and len(spy_weekly) == len(df_weekly):
                            df_weekly['rs_raw'] = (df_weekly['Close'] / df_weekly['Close'].iloc[0]) / (spy_weekly['Close'] / spy_weekly['Close'].iloc[0])
                        else:
                            df_weekly['rs_raw'] = df_weekly['Close'] / df_weekly['Close'].iloc[0]
                        df_weekly['rs_ema_quick'] = df_weekly['rs_raw'].ewm(span=8, adjust=False).mean()
                        df_weekly['rs_ema_quicksand'] = df_weekly['rs_raw'].ewm(span=13, adjust=False).mean()
                        df_weekly['rs_ema_gd'] = df_weekly['rs_raw'].ewm(span=21, adjust=False).mean()
                        
                        def create_weekly_chart_plotly():
                            fig_w = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                                                  subplot_titles=(f"{selected_ticker_company} Weekly", 'Volume', 'Raw RS & QGDRS EMAs'),
                                                  row_heights=[0.5, 0.2, 0.3])
                            fig_w.add_trace(go.Candlestick(x=df_weekly.index, open=df_weekly['Open'], high=df_weekly['High'], low=df_weekly['Low'], close=df_weekly['Close'], name='Price', showlegend=False), row=1, col=1)
                            if cup_weekly_curve:
                                dates_w, yvals_w = cup_weekly_curve
                                fig_w.add_trace(go.Scatter(x=dates_w, y=yvals_w, mode='lines', name='Cup Pattern', line=dict(color='rgb(146,193,131)', width=3)), row=1, col=1)
                            vol_colors_w = []
                            for idx in df_weekly.index:
                                if dry_lvl2_w.loc[idx]:
                                    vol_colors_w.append('rgba(247, 153, 2, 0.7)')
                                elif dry_lvl1_w.loc[idx]:
                                    vol_colors_w.append('rgba(225, 181, 69, 0.6)')
                                elif up_day_w.loc[idx]:
                                    vol_colors_w.append('#26a69a')
                                else:
                                    vol_colors_w.append('#ef5350')
                            fig_w.add_trace(go.Bar(x=df_weekly.index, y=df_weekly['Volume'], marker=dict(color=vol_colors_w), name='Volume', showlegend=False), row=2, col=1)
                            fig_w.add_trace(go.Scatter(x=df_weekly.index, y=avg_vol_w, name='Volume SMA(10)', line=dict(color='orange', width=1.5)), row=2, col=1)
                            fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_raw'], name='Raw RS', line=dict(color='blue', width=2)), row=3, col=1)
                            fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_ema_quick'], name='Quick EMA (8)', line=dict(color='#56b8e6', width=2)), row=3, col=1)
                            fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_ema_quicksand'], name='Quicksand EMA (13)', line=dict(color='#ff8c00', width=2)), row=3, col=1)
                            fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_ema_gd'], name='GD EMA (21)', line=dict(color='#2ca02c', width=2)), row=3, col=1)
                            fig_w.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
                            if latest_row.get('Percentile') is not None and not np.isnan(latest_row.get('Percentile')):
                                last_date_w = df_weekly.index[-1]
                                last_close_w = df_weekly['Close'].iloc[-1]
                                fig_w.add_annotation(x=last_date_w, y=last_close_w, text=f"{int(round(latest_row.get('Percentile')))}", showarrow=True, arrowhead=2, arrowsize=1.5, arrowcolor='#636efa', ax=40, ay=-40, bgcolor="rgba(255,255,255,0.85)", bordercolor="#636efa", borderwidth=1.5, font=dict(size=13, color="#636efa"))
                            fig_w.update_layout(xaxis_rangeslider_visible=False, height=750, margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5))
                            return fig_w
                        
                        # Weekly cup detection (if any)
                        class PineCupDetectorWeekly:
                            def __init__(self):
                                self.pivot_length = 3
                                self.base_depth = 0.50
                                self.base_length = 52
                                self.high = []
                                self.low = []
                                self.close = []
                                # ...
                            # For brevity, we reuse the existing weekly cup detector from earlier. 
                            # If you have a working weekly cup detection, put it here.
                            # For this example, we set cup_weekly_curve = None.
                        cup_weekly_curve = None  # Replace with actual detection if needed
                        
                        if chart_type_weekly == "TradingView Lightweight":
                            st.subheader(f"📊 {selected_ticker_company} Weekly (TradingView Lightweight Charts)")
                            with st.expander("🎯 Add Weekly Chart Markers", expanded=False):
                                col_m1, col_m2 = st.columns(2)
                                with col_m1: marker_date_w = st.date_input("Marker Date", key="weekly_marker_date")
                                with col_m2: marker_price_w = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="weekly_marker_price")
                                col_m3, col_m4, col_m5 = st.columns(3)
                                with col_m3: marker_position_w = st.selectbox("Position", ["aboveBar", "belowBar"], key="weekly_marker_pos")
                                with col_m4: marker_shape_w = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="weekly_marker_shape")
                                with col_m5: marker_color_w = st.color_picker("Color", "#00FF00", key="weekly_marker_color")
                                marker_text_w = st.text_input("Marker Label", "", key="weekly_marker_text")
                                add_marker_w = st.button("➕ Add Marker", key="weekly_add_marker")
                                session_key_w = f"weekly_markers_{selected_ticker}"
                                if session_key_w not in st.session_state:
                                    st.session_state[session_key_w] = load_markers(selected_ticker, "weekly")
                                if add_marker_w and marker_date_w and marker_price_w > 0:
                                    import datetime as dt
                                    marker_timestamp_w = int(dt.datetime.combine(marker_date_w, dt.time()).timestamp())
                                    new_marker_w = {'time': marker_timestamp_w, 'position': marker_position_w, 'color': marker_color_w, 'shape': marker_shape_w, 'text': marker_text_w or f"${marker_price_w:.2f}"}
                                    st.session_state[session_key_w].append(new_marker_w)
                                    save_markers(selected_ticker, st.session_state[session_key_w], "weekly")
                                    st.success(f"✅ Marker saved for {selected_ticker} at {marker_date_w}")
                                    rerun_app()
                                current_markers_w = st.session_state.get(session_key_w, [])
                                if current_markers_w:
                                    st.write(f"**Active Markers for {selected_ticker}:**")
                                    for i, m in enumerate(current_markers_w):
                                        col_a, col_b = st.columns([4,1])
                                        with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                        with col_b:
                                            if st.button("❌", key=f"delete_weekly_marker_{i}"):
                                                st.session_state[session_key_w].pop(i)
                                                save_markers(selected_ticker, st.session_state[session_key_w], "weekly")
                                                st.success(f"✅ Marker deleted for {selected_ticker}")
                                                rerun_app()
                            # For weekly lightweight, we just pass OHLC and RS lines (no volume indicators, but we can keep volume bar colors)
                            vol_colored_w = []
                            for idx, row in df_weekly.iterrows():
                                ts = int(idx.timestamp())
                                val = float(row['Volume'])
                                if dry_lvl2_w.loc[idx]:
                                    color = 'rgba(247, 153, 2, 0.7)'
                                elif dry_lvl1_w.loc[idx]:
                                    color = 'rgba(225, 181, 69, 0.6)'
                                elif up_day_w.loc[idx]:
                                    color = '#26a69a'
                                else:
                                    color = '#ef5350'
                                vol_colored_w.append({'time': ts, 'value': val, 'color': color})
                            st_html(create_lightweight_candlestick_html(
                                df_weekly, height=450, markers=st.session_state.get(session_key_w, []),
                                rs_raw=df_weekly['rs_raw'], rs_quick=df_weekly['rs_ema_quick'],
                                rs_quicksand=df_weekly['rs_ema_quicksand'], rs_gd=df_weekly['rs_ema_gd'],
                                volume_data=vol_colored_w
                            ), height=500)
                        else:
                            st.subheader(f"📊 {selected_ticker_company} Weekly (Plotly)")
                            with st.expander("🎯 Add Weekly Chart Markers", expanded=False):
                                col_m1, col_m2 = st.columns(2)
                                with col_m1: marker_date_w = st.date_input("Marker Date", key="plotly_weekly_marker_date")
                                with col_m2: marker_price_w = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="plotly_weekly_marker_price")
                                col_m3, col_m4, col_m5 = st.columns(3)
                                with col_m3: marker_position_w = st.selectbox("Position", ["aboveBar", "belowBar"], key="plotly_weekly_marker_pos")
                                with col_m4: marker_shape_w = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="plotly_weekly_marker_shape")
                                with col_m5: marker_color_w = st.color_picker("Color", "#00FF00", key="plotly_weekly_marker_color")
                                marker_text_w = st.text_input("Marker Label", "", key="plotly_weekly_marker_text")
                                add_marker_w = st.button("➕ Add Marker", key="plotly_weekly_add_marker")
                                session_key_w = f"weekly_markers_{selected_ticker}"
                                if session_key_w not in st.session_state:
                                    st.session_state[session_key_w] = load_markers(selected_ticker, "weekly")
                                if add_marker_w and marker_date_w and marker_price_w > 0:
                                    import datetime as dt
                                    marker_timestamp_w = int(dt.datetime.combine(marker_date_w, dt.time()).timestamp())
                                    new_marker_w = {'time': marker_timestamp_w, 'position': marker_position_w, 'color': marker_color_w, 'shape': marker_shape_w, 'text': marker_text_w or f"${marker_price_w:.2f}"}
                                    st.session_state[session_key_w].append(new_marker_w)
                                    save_markers(selected_ticker, st.session_state[session_key_w], "weekly")
                                    st.success(f"✅ Marker saved for {selected_ticker} at {marker_date_w}")
                                    rerun_app()
                                current_markers_w = st.session_state.get(session_key_w, [])
                                if current_markers_w:
                                    st.write(f"**Active Markers for {selected_ticker}:**")
                                    for i, m in enumerate(current_markers_w):
                                        col_a, col_b = st.columns([4,1])
                                        with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                        with col_b:
                                            if st.button("❌", key=f"delete_plotly_weekly_marker_{i}"):
                                                st.session_state[session_key_w].pop(i)
                                                save_markers(selected_ticker, st.session_state[session_key_w], "weekly")
                                                st.success(f"✅ Marker deleted for {selected_ticker}")
                                                rerun_app()
                            st.plotly_chart(create_weekly_chart_plotly(), use_container_width=True)
                    else:
                        st.warning(f"Could not retrieve recent data from yfinance for {selected_ticker_company}.")
                except Exception as e:
                    st.error(f"Error fetching data: {e}")

# ---------- TAB 8 (or 7): Data Table ----------
with (tab8 if has_historical else tab7):
    if has_historical:
        latest_date = df['date'].max()
        table_df = df[df['date'] == latest_date].copy()
        st.caption(f"Showing data for the latest date: **{latest_date.date()}**")
    else:
        table_df = filtered_df.copy()
    table_df = table_df.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
    all_cols = table_df.columns.tolist()
    if 'date' in all_cols: all_cols.remove('date')
    if 'source_file' in all_cols: all_cols.remove('source_file')
    default_cols = ['Rank', 'Ticker', 'Industry', '1M', '3M', '6M', 'Price', 'MarketCap', 'AvgVol30', 'PctFrom52WkHigh']
    selected_cols = st.multiselect("Select columns to display", all_cols, default=[col for col in default_cols if col in all_cols])
    sort_options = selected_cols if selected_cols else all_cols
    default_sort_index = sort_options.index('Rank') if 'Rank' in sort_options else 0
    sort_col = st.selectbox("Sort by", sort_options, index=default_sort_index)
    sort_ascending = st.checkbox("Ascending", value=True)
    display_df = table_df[selected_cols].sort_values(by=sort_col, ascending=sort_ascending, na_position='last').reset_index(drop=True)
    st.write("**📊 Filter Data:**")
    ticker_filter = st.text_input("🔍 Search by Ticker", "").strip().upper()
    if ticker_filter and 'Ticker' in display_df.columns:
        display_df = display_df[display_df['Ticker'].astype(str).str.upper().str.contains(ticker_filter)]
    filter_cols = st.columns(3)
    filter_conditions = []
    numeric_cols_available = [col for col in display_df.columns if display_df[col].dtype in ['float64', 'int64']]
    for idx, col in enumerate(numeric_cols_available[:9]):
        with filter_cols[idx % 3]:
            col_min = display_df[col].min()
            col_max = display_df[col].max()
            if pd.notna(col_min) and pd.notna(col_max) and col_min < col_max:
                range_values = st.slider(f"{col}", min_value=float(col_min), max_value=float(col_max), value=(float(col_min), float(col_max)), key=f"filter_{col}")
                filter_conditions.append((col, range_values))
    for col, (min_val, max_val) in filter_conditions:
        display_df = display_df[(display_df[col] >= min_val) & (display_df[col] <= max_val)]
    display_df = display_df.reset_index(drop=True)
    display_df_with_rank = pd.DataFrame()
    display_df_with_rank['Rank'] = range(1, len(display_df) + 1)
    for col in display_df.columns:
        display_df_with_rank[col] = display_df[col]
    st.dataframe(display_df_with_rank, use_container_width=True, hide_index=True)
    csv = display_df_with_rank.to_csv(index=False)
    st.download_button(label="Download filtered data as CSV", data=csv, file_name="rs_analysis_filtered.csv", mime="text/csv")
    st.divider()
    st.subheader("📋 Top Tickers by Relative Strength (Grouped by Strongest Industry)")
    num_tickers = st.number_input("Number of top tickers to display", min_value=10, max_value=len(table_df), value=200, step=10, key="num_top_tickers")
    top_n = table_df.drop_duplicates(subset=['Ticker']).nlargest(int(num_tickers), 'Relative Strength')[['Ticker', 'Industry', 'Relative Strength']].copy()
    if df_industry is not None and not df_industry.empty:
        industry_rs_map = dict(zip(df_industry['Industry'], df_industry['Relative Strength']))
    else:
        industry_rs_map = {}
    top_n['Industry_RS'] = top_n['Industry'].map(industry_rs_map)
    industries_sorted = top_n.groupby('Industry')['Industry_RS'].first().sort_values(ascending=False).index.tolist()
    all_tickers_by_industry = []
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        all_tickers_by_industry.extend(industry_tickers)
    all_tickers_string = ','.join(all_tickers_by_industry)
    st.write(f"**All Industries Combined (Sorted by Strongest Industry First)** ({len(all_tickers_by_industry)} tickers)")
    col1, col2 = st.columns([4, 1])
    with col1: st.code(all_tickers_string, language="text")
    with col2: st.download_button(label="Copy All", data=all_tickers_string, file_name=f"top{int(num_tickers)}_tickers_by_industry.txt", mime="text/plain", key="download_all_tickers")
    st.divider()
    st.write("**By Industry (Strongest First):**")
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        ticker_string = ','.join(industry_tickers)
        industry_rs = industry_rs_map.get(industry, 'N/A')
        st.write(f"**{industry}** ({len(industry_tickers)} tickers) | RS: {industry_rs}")
        col1, col2 = st.columns([4, 1])
        with col1: st.code(ticker_string, language="text")
        with col2: st.download_button(label=f"Copy {industry}", data=ticker_string, file_name=f"{industry.replace(' ', '_')}_tickers.txt", mime="text/plain", key=f"download_{industry}")

# Footer
st.divider()
footer_text = "**Daily Relative Strength Analysis Dashboard**"
if has_historical:
    footer_text += " | Historical Data: Oct 2021 - Present (1238 trading days, 5.87M records)"
footer_text += " | Built with Streamlit"
st.markdown(footer_text)