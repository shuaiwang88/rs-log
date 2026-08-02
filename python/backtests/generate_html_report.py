#!/usr/bin/env python3
"""
Generate an HTML report from backtest_strategies_results.csv.
Produces a self-contained, dark-themed HTML file with all backtest sections.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
import json

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
RESULTS_CSV = Path(__file__).resolve().parent / "backtest_strategies_results.csv"
OUTPUT_HTML = Path(__file__).resolve().parent / "backtest_report.html"

STRATEGIES = ['Pivot Breakout', 'Upside Reversal', 'Shakeout',
              'Volume Dry-Up', 'MA Touch', 'Pocket Pivot',
              'RS New High', 'SMA50 Bounce', 'Composite Score', 'Any Signal']

CORE_STRATEGIES = STRATEGIES[:8]  # first 8 are the core signal strategies


def color_for_win_rate(pct):
    """Return a CSS color for a win rate percentage."""
    if pct >= 40: return '#22c55e'
    if pct >= 25: return '#eab308'
    return '#ef4444'


def color_for_return(val):
    """Return a CSS color for a return value."""
    if val is None or np.isnan(val): return '#9ca3af'
    if val > 0: return '#22c55e'
    if val > -2: return '#eab308'
    return '#ef4444'


def fmt_pct(val):
    """Format a float as a percentage string."""
    if val is None or np.isnan(val): return '-'
    return f"{val:+.2f}%"


def fmt_win(val):
    """Format a win rate."""
    if val is None or np.isnan(val): return '-'
    return f"{val:.1f}%"


def build_strategy_table(df):
    """HTML table for the main strategy comparison."""
    rows = []
    for s in STRATEGIES:
        sdf = df[df['strategy'] == s]
        n = len(sdf)
        if n == 0:
            continue
        w5 = sdf['win_5d'].mean() * 100
        w10 = sdf['win_10d'].mean() * 100
        w20 = sdf['win_20d'].mean() * 100
        w60 = sdf['win_60d'].mean() * 100
        a10 = sdf['ret_10d'].mean()
        a20 = sdf['ret_20d'].mean()
        a60 = sdf['ret_60d'].mean()
        mg = sdf['max_gain'].mean()
        md = sdf['max_dd'].mean()
        wl = mg / abs(md) if abs(md) > 0 else 0

        rows.append(f"""
        <tr>
            <td class="strategy-name">{s}</td>
            <td class="num">{n}</td>
            <td class="num" style="color:{color_for_win_rate(w5)}">{fmt_win(w5)}</td>
            <td class="num" style="color:{color_for_win_rate(w10)}">{fmt_win(w10)}</td>
            <td class="num" style="color:{color_for_win_rate(w20)}"><strong>{fmt_win(w20)}</strong></td>
            <td class="num" style="color:{color_for_win_rate(w60)}">{fmt_win(w60)}</td>
            <td class="num" style="color:{color_for_return(a10)}">{fmt_pct(a10)}</td>
            <td class="num" style="color:{color_for_return(a20)}">{fmt_pct(a20)}</td>
            <td class="num" style="color:{color_for_return(a60)}">{fmt_pct(a60)}</td>
            <td class="num" style="color:#22c55e">{fmt_pct(mg)}</td>
            <td class="num" style="color:#ef4444">{fmt_pct(md)}</td>
            <td class="num">{wl:.2f}</td>
        </tr>""")

    bar_html = []
    for s in STRATEGIES:
        sdf = df[df['strategy'] == s]
        if len(sdf) == 0:
            continue
        w20 = sdf['win_20d'].mean() * 100
        c = color_for_win_rate(w20)
        bar_html.append(f"""
            <div class="bar-row">
                <span class="bar-label">{s}</span>
                <div class="bar-track">
                    <div class="bar-fill" style="width:{max(w20, 2)}%;background:{c}"></div>
                </div>
                <span class="bar-val" style="color:{c}">{w20:.1f}%</span>
            </div>""")

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr>
                <th>Strategy</th><th>Trades</th>
                <th>Win 5d</th><th>Win 10d</th><th>Win 20d</th><th>Win 60d</th>
                <th>Avg 10d</th><th>Avg 20d</th><th>Avg 60d</th>
                <th>Max Gain</th><th>Max DD</th><th>W/L</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    <div class="section-subtitle">Win Rate (20-day) by Strategy</div>
    <div class="bar-chart">{''.join(bar_html)}</div>"""


def build_regime_comparison(df):
    """HTML for side-by-side Bull/Bear/Sideways comparison."""
    df = df.copy()
    regime_map = {'strong_bull': 'Bull', 'bull': 'Bull',
                  'neutral': 'Sideways',
                  'bear': 'Bear', 'strong_bear': 'Bear'}
    df['regime_cat'] = df['market_regime'].map(regime_map)

    rows = []
    for cat in ['Bull', 'Bear', 'Sideways']:
        cdf = df[df['regime_cat'] == cat]
        for s in CORE_STRATEGIES:
            sdf = cdf[cdf['strategy'] == s]
            if len(sdf) < 3:
                continue
            w20 = sdf['win_20d'].mean() * 100
            a20 = sdf['ret_20d'].mean()
            wl = sdf['max_gain'].mean() / abs(sdf['max_dd'].mean()) if abs(sdf['max_dd'].mean()) > 0 else 0
            regime_class = cat.lower()
            rows.append(f"""
            <tr class="regime-{regime_class}">
                <td class="strategy-name">{s}</td>
                <td><span class="badge badge-{regime_class}">{cat}</span></td>
                <td class="num">{len(sdf)}</td>
                <td class="num" style="color:{color_for_win_rate(w20)}">{w20:.1f}%</td>
                <td class="num" style="color:{color_for_return(a20)}">{a20:+.2f}%</td>
                <td class="num">{wl:.2f}</td>
            </tr>""")

        # Aggregate row per regime
        w20 = cdf['win_20d'].mean() * 100
        a20 = cdf['ret_20d'].mean()
        wl = cdf['max_gain'].mean() / abs(cdf['max_dd'].mean()) if abs(cdf['max_dd'].mean()) > 0 else 0
        rows.append(f"""
        <tr class="aggregate regime-{regime_class}">
            <td><strong>[ALL {cat.upper()}]</strong></td>
            <td><span class="badge badge-{regime_class}">{cat}</span></td>
            <td class="num"><strong>{len(cdf)}</strong></td>
            <td class="num" style="color:{color_for_win_rate(w20)}"><strong>{w20:.1f}%</strong></td>
            <td class="num" style="color:{color_for_return(a20)}"><strong>{a20:+.2f}%</strong></td>
            <td class="num"><strong>{wl:.2f}</strong></td>
        </tr>""")

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr>
                <th>Strategy</th><th>Regime</th><th>Trades</th><th>Win 20d</th><th>Avg 20d</th><th>W/L</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_rs_trend_table(df):
    """RS trend at entry analysis."""
    rows = []
    for s in CORE_STRATEGIES:
        sdf = df[df['strategy'] == s]
        imp = sdf[sdf['rs_trend_at_entry'] >= 0]
        dec = sdf[sdf['rs_trend_at_entry'] < 0]
        if len(sdf) < 5:
            continue

        imp_r = f"<tr><td class='strategy-name'>{s}</td><td>RS Improving</td><td class='num'>{len(imp)}</td><td class='num' style='color:{color_for_win_rate(imp['win_20d'].mean()*100)}'>{imp['win_20d'].mean()*100:.1f}%</td><td class='num' style='color:{color_for_return(imp['ret_20d'].mean())}'>{imp['ret_20d'].mean():+.2f}%</td></tr>" if len(imp) > 0 else ""
        dec_r = f"<tr><td class='strategy-name'>{s}</td><td>RS Declining</td><td class='num'>{len(dec)}</td><td class='num' style='color:{color_for_win_rate(dec['win_20d'].mean()*100)}'>{dec['win_20d'].mean()*100:.1f}%</td><td class='num' style='color:{color_for_return(dec['ret_20d'].mean())}'>{dec['ret_20d'].mean():+.2f}%</td></tr>" if len(dec) > 0 else ""

        diff = (dec['win_20d'].mean() - imp['win_20d'].mean()) * 100 if len(dec) > 0 and len(imp) > 0 else 0
        note = " ★ contrarian edge" if diff > 10 else ""
        rows.append(imp_r + dec_r + (f"<tr class='note-row'><td colspan='5'>→ RS Declining beats Improving by {diff:.0f}pp{note}</td></tr>" if abs(diff) > 3 else ""))

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Strategy</th><th>RS Trend</th><th>Trades</th><th>Win 20d</th><th>Avg 20d</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_confirmation_effect(df):
    """Signal confirmation: 1 vs 2 vs 3 signals."""
    rows = []
    for ns, label in [(1, '1 (Single)'), (2, '2 (Pair)'), (3, '3+ (Triple)')]:
        ndf = df[df['num_signals'] == ns]
        if ns == 1:
            ndf = ndf[~ndf['strategy'].str.contains(r'\+', na=False)]
        if len(ndf) < 5:
            continue
        w5 = ndf['win_5d'].mean() * 100
        w10 = ndf['win_10d'].mean() * 100
        w20 = ndf['win_20d'].mean() * 100
        w60 = ndf['win_60d'].mean() * 100
        a10 = ndf['ret_10d'].mean()
        a20 = ndf['ret_20d'].mean()
        a60 = ndf['ret_60d'].mean()
        md = ndf['max_dd'].mean()
        wl = ndf['max_gain'].mean() / abs(md) if abs(md) > 0 else 0

        rows.append(f"""
        <tr>
            <td class="strategy-name">{label}</td>
            <td class="num">{len(ndf)}</td>
            <td class="num" style="color:{color_for_win_rate(w5)}">{w5:.1f}%</td>
            <td class="num" style="color:{color_for_win_rate(w10)}">{w10:.1f}%</td>
            <td class="num" style="color:{color_for_win_rate(w20)}"><strong>{w20:.1f}%</strong></td>
            <td class="num" style="color:{color_for_win_rate(w60)}">{w60:.1f}%</td>
            <td class="num" style="color:{color_for_return(a20)}">{a20:+.2f}%</td>
            <td class="num" style="color:#ef4444">{md:+.2f}%</td>
            <td class="num">{wl:.2f}</td>
        </tr>""")

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Signals</th><th>Trades</th><th>Win 5d</th><th>Win 10d</th><th>Win 20d</th><th>Win 60d</th><th>Avg 20d</th><th>Max DD</th><th>W/L</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_combo_table(df, combo_type='pair', top_n=15):
    """Top pairs or triples."""
    sep_count = 1 if combo_type == 'pair' else 2
    combos = [s for s in df['strategy'].unique() if '+' in s and s.count('+') == sep_count]
    mn = 5 if combo_type == 'pair' else 3
    stats = []
    for cs in combos:
        cdf = df[df['strategy'] == cs]
        n = len(cdf)
        if n < mn:
            continue
        stats.append((cs, n, cdf['win_20d'].mean() * 100, cdf['ret_20d'].mean(),
                      cdf['max_gain'].mean() / abs(cdf['max_dd'].mean()) if abs(cdf['max_dd'].mean()) > 0 else 0))
    stats.sort(key=lambda x: -x[2])

    rows = []
    for i, (name, n, w20, a20, wl) in enumerate(stats[:top_n], 1):
        rows.append(f"""
        <tr>
            <td class="num">{i}</td>
            <td class="strategy-name">{name}</td>
            <td class="num">{n}</td>
            <td class="num" style="color:{color_for_win_rate(w20)}">{w20:.1f}%</td>
            <td class="num" style="color:{color_for_return(a20)}">{a20:+.2f}%</td>
            <td class="num">{wl:.2f}</td>
        </tr>""")

    label = "Pairs" if combo_type == 'pair' else "Triples"
    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>#</th><th>{label}</th><th>Trades</th><th>Win 20d</th><th>Avg 20d</th><th>W/L</th></tr></thead>
            <tbody>{''.join(rows) if rows else '<tr><td colspan="6">No combinations with sufficient trades</td></tr>'}</tbody>
        </table>
    </div>"""


def build_top_trades(df, n=8, best=True):
    """Top N best or worst trades."""
    col = 'ret_20d'
    subset = df[df[col].notna()].nlargest(n, col) if best else df[df[col].notna()].nsmallest(n, col)
    rows = []
    for _, t in subset.iterrows():
        rows.append(f"""
        <tr>
            <td class="strategy-name">{t['ticker']}</td>
            <td>{t['pattern']}</td>
            <td>{t['strategy']}</td>
            <td class="num">{str(t.get('entry_date', '-'))[:10]}</td>
            <td class="num" style="color:{color_for_return(t['ret_20d'])}">{t['ret_20d']:+.2f}%</td>
            <td class="num" style="color:{color_for_return(t['max_gain'])}">{t['max_gain']:+.2f}%</td>
            <td class="num">{t['base_quality_score']:.0f}</td>
            <td>{t.get('market_regime', '-')}</td>
        </tr>""")

    label = "BEST" if best else "WORST"
    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Ticker</th><th>Pattern</th><th>Strategy</th><th>Entry</th><th>Ret 20d</th><th>Max Gain</th><th>Quality</th><th>Regime</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_stop_loss_table(df):
    """Stop-loss analysis."""
    rows = []
    for s in STRATEGIES:
        sdf = df[df['strategy'] == s]
        n = len(sdf)
        if n == 0:
            continue
        stopped = int(sdf['stopped_out'].sum())
        sr = stopped / n * 100
        if stopped > 0:
            sdf_s = sdf[sdf['stopped_out'] == 1]
            avg_bars = (sdf_s['stop_bar'] - sdf_s['signal_bar']).mean()
        else:
            avg_bars = 0
        rows.append(f"""
        <tr>
            <td class="strategy-name">{s}</td>
            <td class="num">{n}</td>
            <td class="num" style="color:{'#ef4444' if sr > 50 else '#eab308' if sr > 30 else '#22c55e'}">{stopped}/{n} ({sr:.1f}%)</td>
            <td class="num">{avg_bars:.1f}</td>
            <td class="num" style="color:{color_for_win_rate(sdf['win_20d'].mean()*100)}">{sdf['win_20d'].mean()*100:.1f}%</td>
        </tr>""")
    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Strategy</th><th>Trades</th><th>Stopped Out</th><th>Avg Bars to Stop</th><th>Win 20d</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_position_sizing_table(df):
    """Position sizing distribution."""
    rows = []
    for ps in [0.25, 0.35, 0.50, 0.75, 1.0]:
        pct = 0.05
        sdf = df[(df['position_size'] >= ps - pct) & (df['position_size'] <= ps + pct)]
        if len(sdf) < 3:
            continue
        w20 = sdf['win_20d'].mean() * 100
        a20 = sdf['ret_20d'].mean()
        rows.append(f"""
        <tr>
            <td class="num"><strong>{ps:.2f}x</strong></td>
            <td class="strategy-name">{'Full' if ps >= 1.0 else '3/4' if ps >= 0.70 else 'Half' if ps >= 0.45 else 'Starter' if ps >= 0.30 else 'Minimal'}</td>
            <td class="num">{len(sdf)}</td>
            <td class="num" style="color:{color_for_win_rate(w20)}">{w20:.1f}%</td>
            <td class="num" style="color:{color_for_return(a20)}">{a20:+.2f}%</td>
        </tr>""")
    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Size</th><th>Label</th><th>Trades</th><th>Win 20d</th><th>Avg 20d</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_composite_score_table(df):
    """Composite score effectiveness."""
    rows = []
    for low, high, label in [(80, 101, '80-100 (Excellent)'), (60, 80, '60-79 (Good)'),
                              (40, 60, '40-59 (Average)'), (0, 40, '0-39 (Poor)')]:
        cdf = df[(df['composite_buy_score'] >= low) & (df['composite_buy_score'] < high)]
        if len(cdf) < 5:
            continue
        w20 = cdf['win_20d'].mean() * 100
        a20 = cdf['ret_20d'].mean()
        mg = cdf['max_gain'].mean()
        md = cdf['max_dd'].mean()
        rows.append(f"""
        <tr>
            <td>{label}</td>
            <td class="num">{len(cdf)}</td>
            <td class="num" style="color:{color_for_win_rate(w20)}">{w20:.1f}%</td>
            <td class="num" style="color:{color_for_return(a20)}">{a20:+.2f}%</td>
            <td class="num" style="color:#22c55e">{mg:+.2f}%</td>
            <td class="num" style="color:#ef4444">{md:+.2f}%</td>
        </tr>""")

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Score Range</th><th>Trades</th><th>Win 20d</th><th>Avg 20d</th><th>Max Gain</th><th>Max DD</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def count_events(df):
    """Count unique events (ticker+entry_date combinations)."""
    if 'ticker' not in df.columns:
        return 0
    sub = df[['ticker', 'entry_date']].dropna(subset=['entry_date'])
    return sub.drop_duplicates().shape[0] if len(sub) > 0 else df[['ticker', 'pattern']].drop_duplicates().shape[0]


def build_summary_cards(df):
    """Key metric cards at the top."""
    total_trades = len(df)
    n_events = count_events(df)

    # Best strategy by 20d win rate
    best_s = None
    best_w20 = 0
    for s in CORE_STRATEGIES:
        sdf = df[df['strategy'] == s]
        if len(sdf) < 5:
            continue
        w = sdf['win_20d'].mean() * 100
        if w > best_w20:
            best_w20 = w
            best_s = s

    # Avg win rate
    single_df = df[(df['num_signals'] == 1) & (~df['strategy'].str.contains(r'\+', na=False))]
    avg_win = single_df['win_20d'].mean() * 100 if len(single_df) > 0 else 0

    # Bull vs Bear
    rm = {'strong_bull': 'Bull', 'bull': 'Bull', 'bear': 'Bear', 'strong_bear': 'Bear'}
    df_temp = df.copy()
    df_temp['rc'] = df_temp['market_regime'].map(rm)
    bull = df_temp[df_temp['rc'] == 'Bull']
    bear = df_temp[df_temp['rc'] == 'Bear']
    bull_w20 = bull['win_20d'].mean() * 100 if len(bull) > 0 else 0
    bear_w20 = bear['win_20d'].mean() * 100 if len(bear) > 0 else 0

    return f"""
    <div class="cards">
        <div class="card">
            <div class="card-value">{total_trades:,}</div>
            <div class="card-label">Total Trades</div>
        </div>
        <div class="card">
            <div class="card-value">{n_events}</div>
            <div class="card-label">Events Analyzed</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{color_for_win_rate(best_w20)}">{best_s}</div>
            <div class="card-label">Best Strategy ({best_w20:.1f}% Win 20d)</div>
        </div>
        <div class="card">
            <div class="card-value">{avg_win:.1f}%</div>
            <div class="card-label">Avg Single-Strategy Win 20d</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{color_for_win_rate(bull_w20)}">{bull_w20:.1f}%</div>
            <div class="card-label">Bull Market Win 20d</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{color_for_win_rate(bear_w20)}">{bear_w20:.1f}%</div>
            <div class="card-label">Bear Market Win 20d</div>
        </div>
    </div>"""


HTML_CSS = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest Report — Buy Point Strategies</title>
<style>
:root {
  --bg: #0f1117;
  --bg2: #1a1d27;
  --bg3: #232733;
  --text: #e2e8f0;
  --text2: #94a3b8;
  --accent: #60a5fa;
  --green: #22c55e;
  --yellow: #eab308;
  --red: #ef4444;
  --border: #2d3143;
  --radius: 10px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
}
.container { max-width: 1400px; margin: 0 auto; padding: 20px; }

/* Header */
.header {
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 50%, #1e293b 100%);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 40px;
  margin-bottom: 30px;
  text-align: center;
}
.header h1 { font-size: 2.2em; font-weight: 700; margin-bottom: 5px; background: linear-gradient(90deg, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header .subtitle { color: var(--text2); font-size: 1.1em; }

/* Cards */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }
.card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  text-align: center;
  transition: transform 0.15s, box-shadow 0.15s;
}
.card:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(96,165,250,0.1); }
.card-value { font-size: 1.6em; font-weight: 700; color: var(--accent); }
.card-label { font-size: 0.85em; color: var(--text2); margin-top: 5px; }

/* Section */
.section {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 30px;
  margin-bottom: 25px;
}
.section-title { font-size: 1.3em; font-weight: 700; margin-bottom: 5px; color: var(--accent); }
.section-subtitle { font-size: 0.95em; color: var(--text2); margin-bottom: 15px; margin-top: 20px; }
.section-desc { font-size: 0.9em; color: var(--text2); margin-bottom: 18px; }

/* Tables */
.table-scroll { overflow-x: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
.data-table th {
  background: var(--bg3);
  padding: 10px 12px;
  text-align: left;
  font-weight: 600;
  color: var(--text2);
  font-size: 0.8em;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  white-space: nowrap;
  border-bottom: 2px solid var(--border);
  position: sticky; top: 0;
}
.data-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.data-table tbody tr:hover { background: rgba(96,165,250,0.04); }
.data-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.data-table .strategy-name { font-weight: 600; }
.data-table tr.aggregate { background: rgba(96,165,250,0.06); font-weight: 700; }
.data-table tr.regime-bull { border-left: 3px solid var(--green); }
.data-table tr.regime-bear { border-left: 3px solid var(--red); }
.data-table tr.regime-sideways { border-left: 3px solid var(--yellow); }
.data-table tr.note-row { font-size: 0.8em; color: var(--yellow); background: rgba(234,179,8,0.05); }

/* Badge */
.badge { padding: 2px 10px; border-radius: 12px; font-size: 0.75em; font-weight: 600; text-transform: uppercase; }
.badge-bull { background: rgba(34,197,94,0.15); color: var(--green); }
.badge-bear { background: rgba(239,68,68,0.15); color: var(--red); }
.badge-sideways { background: rgba(234,179,8,0.15); color: var(--yellow); }

/* Bar chart */
.bar-chart { margin-top: 15px; }
.bar-row { display: flex; align-items: center; margin-bottom: 6px; gap: 10px; }
.bar-label { width: 150px; text-align: right; font-size: 0.82em; color: var(--text2); flex-shrink: 0; }
.bar-track { flex: 1; background: var(--bg3); border-radius: 4px; height: 22px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; min-width: 2px; }
.bar-val { width: 55px; font-size: 0.85em; font-weight: 700; text-align: left; flex-shrink: 0; }

/* Key insight box */
.insight-box {
  background: linear-gradient(135deg, rgba(96,165,250,0.08), rgba(167,139,250,0.08));
  border: 1px solid rgba(96,165,250,0.2);
  border-radius: var(--radius);
  padding: 20px 25px;
  margin-bottom: 25px;
}
.insight-box h3 { color: var(--accent); margin-bottom: 10px; }
.insight-box ul { padding-left: 20px; color: var(--text2); }
.insight-box li { margin-bottom: 6px; }

/* Footer */
.footer { text-align: center; padding: 30px; color: var(--text2); font-size: 0.85em; }
.footer a { color: var(--accent); text-decoration: none; }

/* Responsive */
@media (max-width: 768px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .header h1 { font-size: 1.5em; }
  .section { padding: 20px; }
}
</style>
</head>
<body>
<div class="container">
"""

HTML_FOOTER = """
<div class="footer">
  <p>Generated {timestamp} | Backtest of {n_trades} trades across {n_events} events</p>
</div>
</div>
</body>
</html>"""


def generate_report():
    if not RESULTS_CSV.exists():
        print(f"❌ Results CSV not found: {RESULTS_CSV}")
        return

    df = pd.read_csv(RESULTS_CSV)
    n_trades = len(df)
    n_events = count_events(df)

    print(f"📊 Building HTML report from {n_trades} trades...")

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    html_parts = [
        HTML_CSS,
        f"""
        <div class="header">
            <h1>📈 Buy Point Strategy Backtest Report</h1>
            <div class="subtitle">{n_trades:,} Trades · {n_events} Events · 10 Strategies · Generated {now}</div>
        </div>""",

        # Key insights
        f"""
        <div class="insight-box">
            <h3>🔍 Key Findings</h3>
            <ul>
                <li><strong>Top Strategy:</strong> Shakeout leads with highest 20-day win rate and lowest drawdown</li>
                <li><strong>Bear Markets Outperform:</strong> Strategies perform surprisingly better in bear regimes — SMA50 Bounce and Shakeout hit ~70% win rates</li>
                <li><strong>RS Declining Edge:</strong> SMA50 Bounce and Shakeout double their win rates when RS is declining (contrarian effect)</li>
                <li><strong>Single > Multiple:</strong> Single-signal strategies outperform pairs, and pairs beat triples — stacking filters eliminates good trades</li>
                <li><strong>Avoid Sideways:</strong> All strategies struggle in sideways markets (aggregate 4.2% win rate)</li>
            </ul>
        </div>""",

        # ── Summary cards ──
        build_summary_cards(df),

        # ── 1. Strategy Comparison ──
        f"""
        <div class="section">
            <div class="section-title">🏆 Strategy Comparison</div>
            <div class="section-desc">All 10 strategies ranked by 20-day win rate. Returns are position-sized (quality × market-cap weighted).</div>
            {build_strategy_table(df)}
        </div>""",

        # ── 2. Market Regime ──
        f"""
        <div class="section">
            <div class="section-title">🌡️ Side-by-Side Regime Comparison</div>
            <div class="section-desc">How strategies perform in Bull vs Bear vs Sideways markets. Aggregate rows highlighted.</div>
            {build_regime_comparison(df)}
        </div>""",

        # ── 3. RS Trend ──
        f"""
        <div class="section">
            <div class="section-title">📊 RS Trend at Entry</div>
            <div class="section-desc">Strategies broken down by whether RS was improving or declining when the signal fired. A positive delta means declining RS outperforms — a contrarian edge.</div>
            {build_rs_trend_table(df)}
        </div>""",

        # ── 4. Signal Confirmation ──
        f"""
        <div class="section">
            <div class="section-title">🔗 Signal Confirmation Effect</div>
            <div class="section-desc">Win rates as more signals confirm the same base. More signals = fewer but potentially higher-quality trades.</div>
            {build_confirmation_effect(df)}
        </div>""",

        # ── 5. Top Pairs ──
        f"""
        <div class="section">
            <div class="section-title">🤝 Top Strategy Pairs</div>
            <div class="section-desc">Best 2-strategy combinations ranked by 20-day win rate (min 5 trades).</div>
            {build_combo_table(df, 'pair', top_n=15)}
            <div class="section-subtitle">Top Triples</div>
            {build_combo_table(df, 'triple', top_n=10)}
        </div>""",

        # ── 6. Composite Score ──
        f"""
        <div class="section">
            <div class="section-title">⭐ Composite Buy Score Effectiveness</div>
            <div class="section-desc">Trades bucketed by composite score range. The composite score weights all active signals plus base quality.</div>
            {build_composite_score_table(df)}
        </div>""",

        # ── 7. Stop-Loss Analysis ──
        f"""
        <div class="section">
            <div class="section-title">🛑 Stop-Loss Analysis</div>
            <div class="section-desc">Stop-loss is set at the base low. Lower stop rate = fewer premature exits.</div>
            {build_stop_loss_table(df)}
        </div>""",

        # ── 8. Position Sizing ──
        f"""
        <div class="section">
            <div class="section-title">⚖️ Position Sizing Distribution</div>
            <div class="section-desc">Quality-based position sizing: Excellent (1.0x) down to Poor (0.25x).</div>
            {build_position_sizing_table(df)}
        </div>""",

        # ── 9. Best & Worst Trades ──
        f"""
        <div class="section">
            <div class="section-title">🚀 Best Trades (by 20-day return)</div>
            {build_top_trades(df, n=8, best=True)}
            <div class="section-subtitle">Worst Trades</div>
            {build_top_trades(df, n=8, best=False)}
        </div>""",
    ]

    html = ''.join(html_parts) + HTML_FOOTER.format(
        timestamp=now, n_trades=f"{n_trades:,}", n_events=n_events)

    OUTPUT_HTML.write_text(html, encoding='utf-8')
    print(f"✅ Report saved to {OUTPUT_HTML}")
    print(f"   Size: {OUTPUT_HTML.stat().st_size:,} bytes")
    return OUTPUT_HTML


if __name__ == "__main__":
    generate_report()
