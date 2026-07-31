"""
plot_patterns_openbb.py

Uses OpenBB to plot IBD patterns detected by ibd_pattern_scanner.py.
Draws pattern shapes (bases, handles, HTF flags, double bottoms) on charts
following the logic from drw_pattern.pine.
"""

import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle, Polygon
from matplotlib.lines import Line2D

try:
    from openbb import obb
    OPENBB_AVAILABLE = True
except ImportError:
    OPENBB_AVAILABLE = False
    print("Warning: OpenBB not available. Using matplotlib fallback.")

ROOT_DIR = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT_DIR / "python" / "ibd_pattern_results.json"
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = ROOT_DIR / "python" / "pattern_charts"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PATTERN_COLORS = {
    'Base': '#92C183',
    'Flat Base': '#1E90FF',
    '6-Wk Flat': '#00BFFF',
    'Cup': '#FF6B6B',
    'Cup+Handle': '#FF8C00',
    'Dbl Bottom': '#9370DB',
    'HTF': '#FFD700',
    'Ascending Base': '#20B2AA',
    'Consolidation': '#A0A0A0',
}

PATTERN_CODES = {
    1: 'Base',
    2: 'Flat Base',
    3: 'Cup',
    4: 'Cup+Handle',
    5: 'Dbl Bottom',
    6: 'HTF',
    7: '6-Wk Flat',
    8: 'Ascending Base',
    9: 'Consolidation',
}


def load_results() -> List[Dict[str, Any]]:
    """Load pattern scan results from JSON."""
    if not RESULTS_PATH.exists():
        raise FileNotFoundError(f"Results not found at {RESULTS_PATH}. Run ibd_pattern_scanner.py first.")
    with open(RESULTS_PATH, 'r') as f:
        return json.load(f)


def load_ticker_data(ticker: str, bars: int = 400) -> Optional[pd.DataFrame]:
    """Load ticker parquet data from cache."""
    file_path = TICKER_CACHE_DIR / f"{ticker}_1d.parquet"
    if not file_path.exists():
        return None
    try:
        df = pd.read_parquet(file_path)
        df = df.sort_index()
        if len(df) > bars:
            df = df.iloc[-bars:]
        return df
    except Exception as e:
        print(f"Error loading {ticker}: {e}")
        return None


def find_base_bounds(history: List[Dict], pattern_name: str) -> Tuple[Optional[int], Optional[int], Optional[float], Optional[float]]:
    """
    Find base start/end bars and top/bottom prices from history.
    Returns (start_bar, end_bar, top_price, bottom_price)
    """
    in_base_bars = [(i, h) for i, h in enumerate(history) if h.get('inBase', False)]
    if not in_base_bars:
        return None, None, None, None
    
    start_bar = in_base_bars[0][0]
    end_bar = in_base_bars[-1][0]
    
    bTop = in_base_bars[0][1].get('bTop')
    bLow = in_base_bars[0][1].get('bLow')
    
    for _, h in in_base_bars:
        if h.get('bTop') is not None:
            bTop = h['bTop']
        if h.get('bLow') is not None:
            bLow = h['bLow']
    
    return start_bar, end_bar, bTop, bLow


def find_handle_bounds(history: List[Dict], end_bar: int) -> Tuple[Optional[int], Optional[int], Optional[float], Optional[float]]:
    """Find handle start/end and high, low, and high for Cup+Handle."""
    handle_bars = []
    for i, h in enumerate(history):
        if h.get('isCupH', False) and i <= end_bar:
            handle_bars.append((i, h))
    
    if not handle_bars:
        return None, None, None, None
    
    handle_start = handle_bars[0][0]
    handle_end = handle_bars[-1][0]
    handle_high = handle_bars[-1][1].get('cupHandlePivot') or handle_bars[-1][1].get('bTop')
    handle_low = min(h[1].get('bLow', float('inf')) for h in handle_bars if h[1].get('bLow'))
    
    return handle_start, handle_end, handle_high, handle_low


def find_htf_bounds(history: List[Dict]) -> Tuple[Optional[int], Optional[int], Optional[float], Optional[float], Optional[float]]:
    """Find HTF flag bounds: pole low, flag high, flag low, start, end."""
    htf_bars = [(i, h) for i, h in enumerate(history) if h.get('isHTF', False)]
    if not htf_bars:
        return None, None, None, None, None
    
    start_bar = htf_bars[0][0]
    end_bar = htf_bars[-1][0]
    
    flag_high = None
    flag_low = None
    pole_low = None
    
    for _, h in htf_bars:
        if h.get('pName') == 'HTF':
            if h.get('bTop'):
                flag_high = h['bTop']
            if h.get('bLow'):
                flag_low = h['bLow']
    
    return start_bar, end_bar, flag_high, flag_low, pole_low


def find_double_bottom_bounds(history: List[Dict]) -> Dict[str, Any]:
    """Find double bottom pivot points from history."""
    db_bars = [(i, h) for i, h in enumerate(history) if h.get('isDB', False)]
    if not db_bars:
        return {}
    
    result = {}
    for _, h in db_bars:
        if h.get('dbMiddlePivot'):
            result['middle_pivot'] = h['dbMiddlePivot']
        if h.get('bTop'):
            result['top'] = h['bTop']
        if h.get('bLow'):
            result['bottom'] = h['bLow']
    
    return result


def plot_pattern_matplotlib(ticker: str, df: pd.DataFrame, result: Dict[str, Any], save_path: Path):
    """Plot pattern using matplotlib (fallback when OpenBB not available)."""
    history = result.get('history', [])
    pattern_name = result.get('pattern_name', 'Unknown')
    pattern_code = result.get('pattern_code', 0)
    status = result.get('status', '')
    date = result.get('date', '')
    close = result.get('close', 0)
    
    fig, (ax_price, ax_vol) = plt.subplots(2, 1, figsize=(16, 10), 
                                            gridspec_kw={'height_ratios': [3, 1]}, 
                                            sharex=True)
    
    dates = df.index
    opens = df['Open'].values
    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    volumes = df['Volume'].values
    
    color = PATTERN_COLORS.get(pattern_name, '#92C183')
    
    for i in range(len(df)):
        c = 'green' if closes[i] >= opens[i] else 'red'
        ax_price.plot([dates[i], dates[i]], [lows[i], highs[i]], color=c, linewidth=0.8)
        ax_price.plot([dates[i], dates[i]], [opens[i], closes[i]], color=c, linewidth=3)
    
    ax_vol.bar(dates, volumes, color='gray', alpha=0.5, width=0.8)
    
    start_bar, end_bar, bTop, bLow = find_base_bounds(history, pattern_name)
    
    if start_bar is not None and end_bar is not None and bTop is not None and bLow is not None:
        base_dates = [dates[start_bar], dates[end_bar]]
        
        ax_price.axhline(y=bTop, xmin=0, xmax=1, color=color, linestyle='--', linewidth=2, alpha=0.7)
        ax_price.axhline(y=bLow, xmin=0, xmax=1, color=color, linestyle='--', linewidth=2, alpha=0.7)
        
        ax_price.fill_between(dates[start_bar:end_bar+1], bLow, bTop, 
                             alpha=0.1, color=color, label=f'{pattern_name} Base')
        
        if pattern_name == 'Cup+Handle':
            h_start, h_end, h_high, h_low = find_handle_bounds(history, end_bar)
            if h_start is not None and h_end is not None and h_high and h_low:
                ax_price.axhline(y=h_high, xmin=0, xmax=1, color='orange', linestyle=':', linewidth=2, alpha=0.7)
                ax_price.axhline(y=h_low, xmin=0, xmax=1, color='orange', linestyle=':', linewidth=2, alpha=0.7)
                ax_price.fill_between(dates[h_start:h_end+1], h_low, h_high, 
                                     alpha=0.15, color='orange', label='Handle')
        
        elif pattern_name == 'Dbl Bottom':
            db_info = find_double_bottom_bounds(history)
            if 'middle_pivot' in db_info:
                ax_price.axhline(y=db_info['middle_pivot'], xmin=0, xmax=1, 
                               color='purple', linestyle='-.', linewidth=2, alpha=0.7, label='DB Middle Pivot')
        
        elif pattern_name == 'HTF':
            htf_start, htf_end, flag_high, flag_low, _ = find_htf_bounds(history)
            if htf_start is not None and htf_end is not None and flag_high and flag_low:
                ax_price.axhline(y=flag_high, xmin=0, xmax=1, color='gold', linestyle='--', linewidth=2, alpha=0.8)
                ax_price.axhline(y=flag_low, xmin=0, xmax=1, color='gold', linestyle='--', linewidth=2, alpha=0.8)
                ax_price.fill_between(dates[htf_start:htf_end+1], flag_low, flag_high, 
                                     alpha=0.15, color='gold', label='HTF Flag')
    
    boBar = None
    for i, h in enumerate(history):
        if h.get('boBar') is not None:
            boBar = h['boBar']
            break
    
    if boBar is not None and boBar < len(dates):
        ax_price.axvline(x=dates[boBar], color='red', linestyle='-', linewidth=2, alpha=0.8, label='Breakout')
        boPivot = None
        for h in history:
            if h.get('boPivot'):
                boPivot = h['boPivot']
                break
        if boPivot:
            ax_price.axhline(y=boPivot, color='red', linestyle='-', linewidth=2, alpha=0.8)
            ax_price.annotate(f'BO: {boPivot:.2f}', 
                             xy=(dates[boBar], boPivot), 
                             xytext=(10, 10), textcoords='offset points',
                             color='red', fontweight='bold',
                             bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='red'))
    
    ema10 = pd.Series(closes).ewm(span=10, adjust=False).mean()
    ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean()
    sma50 = pd.Series(closes).rolling(50).mean()
    
    ax_price.plot(dates, ema10, color='#F08DF0', linewidth=1, label='EMA 10')
    ax_price.plot(dates, ema20, color='#44BA4C', linewidth=1, label='EMA 20')
    ax_price.plot(dates, sma50, color='#FF2121', linewidth=1, label='SMA 50')
    
    ax_price.set_title(f'{ticker} - {pattern_name} ({status}) - {date} - Close: {close:.2f} - Composite: {result.get("composite_score", 0)}', 
                       fontsize=14, fontweight='bold')
    ax_price.set_ylabel('Price')
    ax_price.legend(loc='upper left', fontsize=9)
    ax_price.grid(True, alpha=0.3)
    
    ax_vol.set_ylabel('Volume')
    ax_vol.grid(True, alpha=0.3)
    
    ax_price.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax_price.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.setp(ax_price.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved chart: {save_path}")


def plot_pattern_openbb(ticker: str, df: pd.DataFrame, result: Dict[str, Any], save_path: Path):
    """Plot pattern using OpenBB Terminal."""
    if not OPENBB_AVAILABLE:
        plot_pattern_matplotlib(ticker, df, result, save_path)
        return
    
    try:
        from openbb_terminal.sdk import openbb
        
        history = result.get('history', [])
        pattern_name = result.get('pattern_name', 'Unknown')
        
        start_bar, end_bar, bTop, bLow = find_base_bounds(history, pattern_name)
        
        chart_data = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        chart_data.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        
        overlays = []
        
        if bTop is not None and bLow is not None:
            overlays.append({
                'type': 'hline',
                'price': bTop,
                'color': PATTERN_COLORS.get(pattern_name, '#92C183'),
                'style': 'dashed',
                'width': 2
            })
            overlays.append({
                'type': 'hline',
                'price': bLow,
                'color': PATTERN_COLORS.get(pattern_name, '#92C183'),
                'style': 'dashed',
                'width': 2
            })
        
        if pattern_name == 'Cup+Handle':
            h_start, h_end, h_high, h_low = find_handle_bounds(history, end_bar if end_bar else len(df)-1)
            if h_high and h_low:
                overlays.append({
                    'type': 'hline',
                    'price': h_high,
                    'color': 'orange',
                    'style': 'dotted',
                    'width': 2
                })
                overlays.append({
                    'type': 'hline',
                    'price': h_low,
                    'color': 'orange',
                    'style': 'dotted',
                    'width': 2
                })
        
        elif pattern_name == 'Dbl Bottom':
            db_info = find_double_bottom_bounds(history)
            if 'middle_pivot' in db_info:
                overlays.append({
                    'type': 'hline',
                    'price': db_info['middle_pivot'],
                    'color': 'purple',
                    'style': 'dashdot',
                    'width': 2
                })
        
        elif pattern_name == 'HTF':
            htf_start, htf_end, flag_high, flag_low, _ = find_htf_bounds(history)
            if flag_high and flag_low:
                overlays.append({
                    'type': 'hline',
                    'price': flag_high,
                    'color': 'gold',
                    'style': 'dashed',
                    'width': 2
                })
                overlays.append({
                    'type': 'hline',
                    'price': flag_low,
                    'color': 'gold',
                    'style': 'dashed',
                    'width': 2
                })
        
        boBar = None
        for i, h in enumerate(history):
            if h.get('boBar') is not None:
                boBar = h['boBar']
                break
        
        if boBar is not None and boBar < len(df):
            overlays.append({
                'type': 'vline',
                'x': df.index[boBar],
                'color': 'red',
                'style': 'solid',
                'width': 2
            })
        
        openbb.technical.load(chart_data, symbol=ticker)
        openbb.technical.ma()
        openbb.technical.ema(length=10)
        openbb.technical.ema(length=20)
        
        print(f"OpenBB chart for {ticker} - {pattern_name} (saved to {save_path})")
        
    except Exception as e:
        print(f"OpenBB plotting failed for {ticker}: {e}, falling back to matplotlib")
        plot_pattern_matplotlib(ticker, df, result, save_path)


def main(max_charts: int = 20, use_openbb: bool = True):
    """Main function to plot all detected patterns."""
    results = load_results()
    print(f"Loaded {len(results)} pattern results")
    
    if not results:
        print("No patterns found. Run scanner first.")
        return
    
    plotted = 0
    for result in results[:max_charts]:
        ticker = result['ticker']
        print(f"\nPlotting {ticker} - {result['pattern_name']}...")
        
        df = load_ticker_data(ticker, bars=400)
        if df is None or df.empty:
            print(f"  No data for {ticker}")
            continue
        
        save_path = OUTPUT_DIR / f"{ticker}_{result['pattern_name'].replace('+', '_')}_{result['date']}.png"
        
        if use_openbb and OPENBB_AVAILABLE:
            plot_pattern_openbb(ticker, df, result, save_path)
        else:
            plot_pattern_matplotlib(ticker, df, result, save_path)
        
        plotted += 1
    
    print(f"\nDone! Plotted {plotted} charts to {OUTPUT_DIR}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Plot IBD patterns using OpenBB or matplotlib')
    parser.add_argument('--max', type=int, default=20, help='Maximum charts to plot')
    parser.add_argument('--no-openbb', action='store_true', help='Force matplotlib instead of OpenBB')
    args = parser.parse_args()
    
    main(max_charts=args.max, use_openbb=not args.no_openbb)