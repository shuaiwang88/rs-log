#!/usr/bin/env python3
"""
Generate a comprehensive HTML report from full_backtest_summary.csv
Shows all buy strategy × exit rule combinations with rankings and insights.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

SUMMARY_CSV = Path(__file__).resolve().parent / "scanner_universe_summary.csv"
TRADES_CSV = Path(__file__).resolve().parent / "scanner_universe_trades.csv"
OUTPUT_HTML = Path(__file__).resolve().parent / "scanner_universe_report.html"

EXIT_LABELS = {
    'stop_loss': 'Stop-Loss (Base Low)', 'trail_2atr': 'Trail 2× ATR',
    'trail_3atr': 'Trail 3× ATR', 'time_20': 'Time 20 Bars',
    'time_40': 'Time 40 Bars', 'time_60': 'Time 60 Bars',
    'target_2r': 'Target 2:1 R:R', 'target_3r': 'Target 3:1 R:R',
    'target_5r': 'Target 5:1 R:R',
}


def color_for_win(pct):
    if pct >= 70: return '#22c55e'
    if pct >= 55: return '#84cc16'
    if pct >= 40: return '#eab308'
    return '#ef4444'


def color_for_ret(val):
    if val > 3: return '#22c55e'
    if val > 0: return '#84cc16'
    if val > -2: return '#eab308'
    return '#ef4444'


def build_full_matrix(df):
    """HTML matrix: buy strategies (rows) × exit rules (cols) with win% and avg ret."""
    strategies = df['buy_strategy'].unique().tolist()
    exit_rules = df['exit_rule'].unique().tolist()

    rows = []
    for s in strategies:
        cells = f'<td class="strategy-name">{s}</td>'
        for e in exit_rules:
            r = df[(df['buy_strategy'] == s) & (df['exit_rule'] == e)]
            if len(r) == 0:
                cells += '<td class="num">-</td>'
            else:
                w = r.iloc[0]['win_pct']
                ret = r.iloc[0]['avg_ret']
                cells += f'<td class="num" style="color:{color_for_win(w)}"><strong>{w:.1f}%</strong><br><span style="font-size:0.75em;color:{color_for_ret(ret)}">{ret:+.2f}%</span></td>'
        rows.append(f'<tr>{cells}</tr>')

    header = '<th>Strategy</th>' + ''.join(f'<th>{EXIT_LABELS.get(e, e)}</th>' for e in exit_rules)

    return f"""
    <div class="table-scroll">
        <table class="data-table heatmap">
            <thead><tr>{header}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_top_table(df, sort_by='win_pct', n=15, label="Win Rate"):
    """Top N combinations by metric."""
    top = df.nlargest(n, sort_by)
    rows = []
    for i, (_, r) in enumerate(top.iterrows(), 1):
        rows.append(f"""
        <tr>
            <td class="num">{i}</td>
            <td class="strategy-name">{r['buy_strategy']}</td>
            <td>{EXIT_LABELS.get(r['exit_rule'], r['exit_rule'])}</td>
            <td class="num">{int(r['trades']):,}</td>
            <td class="num" style="color:{color_for_win(r['win_pct'])}"><strong>{r['win_pct']:.1f}%</strong></td>
            <td class="num" style="color:{color_for_ret(r['avg_ret'])}">{r['avg_ret']:+.2f}%</td>
            <td class="num">{r['avg_rr']:.2f}</td>
            <td class="num">{r['sharpe']:.2f}</td>
        </tr>""")

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>#</th><th>Buy Strategy</th><th>Exit Rule</th><th>Trades</th><th>Win%</th><th>Avg Ret</th><th>Avg R:R</th><th>Sharpe</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_strategy_summary(df):
    """Average performance per buy strategy across all exit rules."""
    rows = []
    for s in df['buy_strategy'].unique():
        sdf = df[df['buy_strategy'] == s]
        rows.append(f"""
        <tr>
            <td class="strategy-name">{s}</td>
            <td class="num">{int(sdf['trades'].sum()):,}</td>
            <td class="num" style="color:{color_for_win(sdf['win_pct'].mean())}">{sdf['win_pct'].mean():.1f}%</td>
            <td class="num" style="color:{color_for_ret(sdf['avg_ret'].mean())}">{sdf['avg_ret'].mean():+.2f}%</td>
            <td class="num">{sdf['avg_rr'].mean():.2f}</td>
            <td class="num">{sdf['sharpe'].mean():.2f}</td>
        </tr>""")

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Buy Strategy</th><th>Total Trades</th><th>Avg Win%</th><th>Avg Ret</th><th>Avg R:R</th><th>Avg Sharpe</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_exit_summary(df):
    """Average performance per exit rule across all strategies."""
    rows = []
    for e in df['exit_rule'].unique():
        edf = df[df['exit_rule'] == e]
        rows.append(f"""
        <tr>
            <td class="strategy-name">{EXIT_LABELS.get(e, e)}</td>
            <td class="num">{int(edf['trades'].sum()):,}</td>
            <td class="num" style="color:{color_for_win(edf['win_pct'].mean())}">{edf['win_pct'].mean():.1f}%</td>
            <td class="num" style="color:{color_for_ret(edf['avg_ret'].mean())}">{edf['avg_ret'].mean():+.2f}%</td>
            <td class="num">{edf['avg_rr'].mean():.2f}</td>
            <td class="num">{edf['sharpe'].mean():.2f}</td>
        </tr>""")

    return f"""
    <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th>Exit Rule</th><th>Total Trades</th><th>Avg Win%</th><th>Avg Ret</th><th>Avg R:R</th><th>Avg Sharpe</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def build_cards(df):
    total_trades = int(df['trades'].sum())
    best_w = df.loc[df['win_pct'].idxmax()]
    best_s = df.loc[df['sharpe'].idxmax()]
    avg_w = df['win_pct'].mean()
    avg_rr = df['avg_rr'].mean()

    n_strategies = df['buy_strategy'].nunique()
    n_exits = df['exit_rule'].nunique()

    return f"""
    <div class="cards">
        <div class="card">
            <div class="card-value">{total_trades:,}</div>
            <div class="card-label">Total Trades</div>
        </div>
        <div class="card">
            <div class="card-value">{n_strategies}×{n_exits}</div>
            <div class="card-label">Strategy × Exit Combos</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{color_for_win(best_w['win_pct'])}">{best_w['win_pct']:.1f}%</div>
            <div class="card-label">Best Win Rate ({best_w['buy_strategy']} + {EXIT_LABELS.get(best_w['exit_rule'], best_w['exit_rule'])})</div>
        </div>
        <div class="card">
            <div class="card-value">{avg_w:.1f}%</div>
            <div class="card-label">Average Win Rate</div>
        </div>
        <div class="card">
            <div class="card-value">{best_s['sharpe']:.2f}</div>
            <div class="card-label">Best Sharpe ({best_s['buy_strategy']} + {EXIT_LABELS.get(best_s['exit_rule'], best_s['exit_rule'])})</div>
        </div>
        <div class="card">
            <div class="card-value">{avg_rr:.2f}</div>
            <div class="card-label">Avg Risk/Reward</div>
        </div>
    </div>"""


HTML_CSS = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Full Backtest Report — All Ticker Cache</title>
<style>
:root {
  --bg: #0f1117; --bg2: #1a1d27; --bg3: #232733;
  --text: #e2e8f0; --text2: #94a3b8; --accent: #60a5fa;
  --green: #22c55e; --yellow: #eab308; --red: #ef4444;
  --border: #2d3143; --radius: 10px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.6;
}
.container { max-width: 1500px; margin: 0 auto; padding: 20px; }

.header {
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 50%, #1e293b 100%);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 40px; margin-bottom: 30px; text-align: center;
}
.header h1 { font-size: 2.2em; font-weight: 700; margin-bottom: 5px;
  background: linear-gradient(90deg, #60a5fa, #a78bfa);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header .subtitle { color: var(--text2); font-size: 1.1em; }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 30px; }
.card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; text-align: center; transition: transform 0.15s, box-shadow 0.15s;
}
.card:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(96,165,250,0.1); }
.card-value { font-size: 1.6em; font-weight: 700; color: var(--accent); }
.card-label { font-size: 0.85em; color: var(--text2); margin-top: 5px; }

.section {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 30px; margin-bottom: 25px;
}
.section-title { font-size: 1.3em; font-weight: 700; margin-bottom: 5px; color: var(--accent); }
.section-desc { font-size: 0.9em; color: var(--text2); margin-bottom: 18px; }

.table-scroll { overflow-x: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 0.88em; }
.data-table th {
  background: var(--bg3); padding: 10px 12px; text-align: left; font-weight: 600;
  color: var(--text2); font-size: 0.78em; text-transform: uppercase;
  letter-spacing: 0.5px; white-space: nowrap; border-bottom: 2px solid var(--border);
}
.data-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.data-table tbody tr:hover { background: rgba(96,165,250,0.04); }
.data-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.data-table .strategy-name { font-weight: 600; }

.heatmap td { min-width: 100px; font-size: 0.85em; }
.heatmap th { font-size: 0.7em; text-align: center; max-width: 100px; }

.insight-box {
  background: linear-gradient(135deg, rgba(96,165,250,0.08), rgba(167,139,250,0.08));
  border: 1px solid rgba(96,165,250,0.2); border-radius: var(--radius);
  padding: 20px 25px; margin-bottom: 25px;
}
.insight-box h3 { color: var(--accent); margin-bottom: 10px; }
.insight-box ul { padding-left: 20px; color: var(--text2); }
.insight-box li { margin-bottom: 6px; }

.footer { text-align: center; padding: 30px; color: var(--text2); font-size: 0.85em; }

@media (max-width: 768px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .header h1 { font-size: 1.5em; }
}
</style>
</head>
<body>
<div class="container">
"""

HTML_FOOTER = """
<div class="footer">
  <p>Generated {timestamp} | Full Backtest — {n_trades} combined trades</p>
</div>
</div>
</body>
</html>"""


def generate_full_report():
    if not SUMMARY_CSV.exists():
        print(f"❌ Summary CSV not found: {SUMMARY_CSV}")
        return

    df = pd.read_csv(SUMMARY_CSV)
    total_combos = int(df['trades'].sum())
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    print(f"📊 Building full backtest HTML report from {len(df)} strategy×exit combinations...")

    # Dynamically compute insights
    top_win = df.loc[df['win_pct'].idxmax()]
    top_sharpe = df.loc[df['sharpe'].idxmax()]
    top_ret = df.loc[df['avg_ret'].idxmax()]
    top_trades_row = df.loc[df['trades'].idxmax()]
    
    # Best exit rule by avg win rate
    exit_avg_win = df.groupby('exit_rule')['win_pct'].mean()
    best_exit = exit_avg_win.idxmax()
    worst_exit = exit_avg_win.idxmin()

    # Count unique tickers from trades CSV
    n_tickers = "?"
    if TRADES_CSV.exists():
        try:
            n_tickers = f"{pd.read_csv(TRADES_CSV, usecols=['ticker'])['ticker'].nunique():,}"
        except Exception:
            pass

    html = HTML_CSS + f"""
    <div class="header">
        <h1>📊 Scanner Universe Backtest Report</h1>
        <div class="subtitle">{len(df)} Strategy×Exit Combinations · {n_tickers} Tickers · Generated {now}</div>
    </div>

    <div class="insight-box">
        <h3>🔍 Key Findings</h3>
        <ul>
            <li><strong>Best Win Rate:</strong> {top_win['buy_strategy']} + {EXIT_LABELS.get(top_win['exit_rule'], top_win['exit_rule'])} = <strong>{top_win['win_pct']:.1f}%</strong> win rate with {top_win['avg_ret']:+.2f}% avg return</li>
            <li><strong>Best Sharpe:</strong> {top_sharpe['buy_strategy']} + {EXIT_LABELS.get(top_sharpe['exit_rule'], top_sharpe['exit_rule'])} = <strong>{top_sharpe['sharpe']:.2f}</strong> Sharpe — most consistent risk-adjusted returns</li>
            <li><strong>Highest Avg Return:</strong> {top_ret['buy_strategy']} + {EXIT_LABELS.get(top_ret['exit_rule'], top_ret['exit_rule'])} = <strong>{top_ret['avg_ret']:+.2f}%</strong> per trade</li>
            <li><strong>Most Liquid Strategy:</strong> {top_trades_row['buy_strategy']} + {EXIT_LABELS.get(top_trades_row['exit_rule'], top_trades_row['exit_rule'])} = {int(top_trades_row['trades']):,} trades — most actionable</li>
            <li><strong>Exit Rule Comparison:</strong> {EXIT_LABELS.get(best_exit, best_exit)} ({exit_avg_win[best_exit]:.1f}% avg win) vs {EXIT_LABELS.get(worst_exit, worst_exit)} ({exit_avg_win[worst_exit]:.1f}% avg win)</li>
        </ul>
    </div>
    """ + build_cards(df) + f"""
    <div class="section">
        <div class="section-title">🔥 Top 15 — By Win Rate</div>
        <div class="section-desc">Highest win-rate strategy×exit combinations. Sorted by win percentage.</div>
        {build_top_table(df, 'win_pct', 15)}
    </div>

    <div class="section">
        <div class="section-title">📈 Top 15 — By Sharpe Ratio</div>
        <div class="section-desc">Best risk-adjusted returns. Higher Sharpe = more consistent profitability.</div>
        {build_top_table(df, 'sharpe', 15, 'Sharpe')}
    </div>

    <div class="section">
        <div class="section-title">🧩 Full Strategy × Exit Matrix</div>
        <div class="section-desc">Every buy strategy tested against every exit rule. Cells show Win% and Avg Return.</div>
        {build_full_matrix(df)}
    </div>

    <div class="section">
        <div class="section-title">🎯 Buy Strategy Summary</div>
        <div class="section-desc">Average performance by buy strategy (across all 9 exit rules).</div>
        {build_strategy_summary(df)}
    </div>

    <div class="section">
        <div class="section-title">🚪 Exit Rule Effectiveness</div>
        <div class="section-desc">Average performance by exit rule (across all 10 buy strategies).</div>
        {build_exit_summary(df)}
    </div>

    <div class="section">
        <div class="section-title">💡 How to Read This Report</div>
        <div class="section-desc">
            <strong>Win Rate:</strong> % of trades that closed with positive return (position-sized).<br>
            <strong>Avg Ret:</strong> Average return per trade, scaled by position size (0.25x–1.0x based on base quality).<br>
            <strong>R:R Ratio:</strong> Average risk/reward — higher is better.<br>
            <strong>Sharpe:</strong> Return / StdDev — measures consistency. Above 0.3 is strong for daily-frequency signals.<br>
            <strong>Filters applied:</strong> Price > $12, 50-day avg volume > 500K, minimum 100 bars of history.
        </div>
    </div>
    """ + HTML_FOOTER.format(timestamp=now, n_trades=f"{total_combos:,}")

    OUTPUT_HTML.write_text(html, encoding='utf-8')
    print(f"✅ Report saved to {OUTPUT_HTML}")
    print(f"   Size: {OUTPUT_HTML.stat().st_size:,} bytes")


if __name__ == "__main__":
    generate_full_report()
