#!/usr/bin/env python3
"""
Generate an HTML report from trend_following_summary.csv
Ranks all 48 Turtle/Seykota trend-following strategies across the universe.
"""
import pandas as pd
from pathlib import Path
from datetime import datetime

SUMMARY_CSV = Path(__file__).resolve().parent / "trend_following_summary.csv"
OUTPUT_HTML = Path(__file__).resolve().parent / "trend_following_report.html"


def color_for_val(val, thresholds=(70, 40, 20, 0)):
    t1, t2, t3, t4 = thresholds
    if val >= t1: return '#22c55e'
    if val >= t2: return '#84cc16'
    if val >= t3: return '#eab308'
    return '#ef4444'


def color_for_ret(val):
    if val > 20: return '#22c55e'
    if val > 0: return '#84cc16'
    if val > -10: return '#eab308'
    return '#ef4444'


def build_cards(df):
    total_tickers = int(df['tickers'].iloc[0])
    best_success = df.loc[df['success_rate_pct'].idxmax()]
    best_median = df.loc[df['median_total_ret'].idxmax()]
    best_pf = df.loc[df['avg_profit_factor'].idxmax()]
    avg_success = df['success_rate_pct'].mean()
    avg_win_rate = df['avg_win_rate'].mean()

    return f"""
    <div class="cards">
        <div class="card">
            <div class="card-value">{total_tickers:,}</div>
            <div class="card-label">Tickers in Universe</div>
        </div>
        <div class="card">
            <div class="card-value">{len(df)}</div>
            <div class="card-label">Strategies Tested</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{color_for_val(best_success['success_rate_pct'])}">{best_success['success_rate_pct']:.1f}%</div>
            <div class="card-label">Best Success Rate ({best_success['strategy']})</div>
        </div>
        <div class="card">
            <div class="card-value">{avg_success:.1f}%</div>
            <div class="card-label">Avg Success Rate</div>
        </div>
        <div class="card">
            <div class="card-value">{avg_win_rate:.1f}%</div>
            <div class="card-label">Avg Win Rate (per-trade)</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{color_for_ret(best_median['median_total_ret'])}">{best_median['median_total_ret']:+.1f}%</div>
            <div class="card-label">Best Median Return ({best_median['strategy']})</div>
        </div>
    </div>"""


def build_ranking_table(df, sort_by='success_rate_pct', n=48, title=''):
    top = df.nlargest(n, sort_by).reset_index(drop=True)
    rows = []
    for i, (_, r) in enumerate(top.iterrows(), 1):
        rows.append(f"""
        <tr>
            <td class="num">{i}</td>
            <td class="strategy-name">{r['strategy']}</td>
            <td class="num" style="color:{color_for_val(r['success_rate_pct'])}"><strong>{r['success_rate_pct']:.1f}%</strong></td>
            <td class="num">{int(r['profitable_tickers']):,}</td>
            <td class="num" style="color:{color_for_ret(r['median_total_ret'])}">{r['median_total_ret']:+.1f}%</td>
            <td class="num" style="color:{color_for_ret(r['avg_total_ret'])}">{r['avg_total_ret']:+.1f}%</td>
            <td class="num">{r['avg_win_rate']:.1f}%</td>
            <td class="num">{r['avg_profit_factor']:.2f}</td>
            <td class="num">{r['avg_max_dd']:.1f}%</td>
            <td class="num">{r['avg_trades']:.1f}</td>
        </tr>""")

    return f"""
    <div class="section">
        <div class="section-title">{title}</div>
        <div class="table-scroll">
            <table class="data-table">
                <thead><tr>
                    <th>#</th><th>Strategy</th><th>Success%</th><th>Profitable</th>
                    <th>Median Ret</th><th>Avg Ret</th><th>Win Rate</th>
                    <th>Profit Factor</th><th>Max DD</th><th>Avg Trades</th>
                </tr></thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
    </div>"""


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trend-Following Strategy Rankings — Full Universe</title>
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
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }

.header {
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 50%, #1e293b 100%);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 40px; margin-bottom: 30px; text-align: center;
}
.header h1 { font-size: 2.2em; font-weight: 700; margin-bottom: 5px;
  background: linear-gradient(90deg, #60a5fa, #a78bfa);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header .subtitle { color: var(--text2); font-size: 1.1em; }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }
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
.data-table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
.data-table th {
  background: var(--bg3); padding: 10px 12px; text-align: left; font-weight: 600;
  color: var(--text2); font-size: 0.75em; text-transform: uppercase;
  letter-spacing: 0.5px; white-space: nowrap; border-bottom: 2px solid var(--border);
}
.data-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.data-table tbody tr:hover { background: rgba(96,165,250,0.04); }
.data-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.data-table .strategy-name { font-weight: 600; }

.insight-box {
  background: linear-gradient(135deg, rgba(96,165,250,0.08), rgba(167,139,250,0.08));
  border: 1px solid rgba(96,165,250,0.2); border-radius: var(--radius);
  padding: 20px 25px; margin-bottom: 25px;
}
.insight-box h3 { color: var(--accent); margin-bottom: 10px; }
.insight-box ul { padding-left: 20px; color: var(--text2); }
.insight-box li { margin-bottom: 6px; }

.warning-box {
  background: linear-gradient(135deg, rgba(239,68,68,0.08), rgba(234,179,8,0.08));
  border: 1px solid rgba(239,68,68,0.2); border-radius: var(--radius);
  padding: 20px 25px; margin-bottom: 25px;
}
.warning-box h3 { color: #f87171; margin-bottom: 10px; }
.warning-box ul { padding-left: 20px; color: var(--text2); }
.warning-box li { margin-bottom: 6px; }

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


def generate():
    if not SUMMARY_CSV.exists():
        print(f"❌ Summary CSV not found: {SUMMARY_CSV}")
        return

    df = pd.read_csv(SUMMARY_CSV)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    n_tickers = int(df['tickers'].iloc[0])

    # Separate realistic from buggy strategies (avg_total_ret > 500% or < -1000% or success > 95%)
    buggy_mask = (
        (df['avg_total_ret'].abs() > 500) |
        (df['success_rate_pct'] > 95) |
        (df['avg_max_dd'] > 99)
    )
    realistic = df[~buggy_mask].copy()
    buggy = df[buggy_mask].copy()

    # Dynamic insights
    top_success = realistic.loc[realistic['success_rate_pct'].idxmax()]
    top_median = realistic.loc[realistic['median_total_ret'].idxmax()]
    top_pf = realistic.loc[realistic['avg_profit_factor'].idxmax()]
    best_balanced = realistic.loc[(realistic['avg_max_dd'] < 60) & (realistic['success_rate_pct'] > 40)].nlargest(1, 'median_total_ret').iloc[0]

    print(f"📊 Building trend-following HTML report ({len(realistic)} realistic + {len(buggy)} buggy strategies)...")

    html = HTML + f"""
    <div class="header">
        <h1>🐢 Trend-Following Strategy Rankings</h1>
        <div class="subtitle">48 Turtle/Seykota Strategies · {n_tickers:,} Tickers · Full Universe · Generated {now}</div>
    </div>

    <div class="insight-box">
        <h3>🔍 Key Findings</h3>
        <ul>
            <li><strong>Best Success Rate:</strong> {top_success['strategy']} = {top_success['success_rate_pct']:.1f}% of tickers profitable ({int(top_success['profitable_tickers']):,} tickers) with {top_success['median_total_ret']:+.1f}% median return</li>
            <li><strong>Best Median Return:</strong> {top_median['strategy']} = {top_median['median_total_ret']:+.1f}% median across all tickers — most consistent strategy</li>
            <li><strong>Best Profit Factor:</strong> {top_pf['strategy']} = {top_pf['avg_profit_factor']:.2f} PF — highest reward vs risk</li>
            <li><strong>Best Balanced:</strong> {best_balanced['strategy']} = {best_balanced['success_rate_pct']:.1f}% success, {best_balanced['median_total_ret']:+.1f}% median, {best_balanced['avg_max_dd']:.1f}% max DD — best all-around</li>
            <li><strong>{len(buggy)} strategies flagged as buggy</strong> — parabolic SAR / pure ATR / ATR channel variants that leave positions open indefinitely. These are excluded from rankings below.</li>
        </ul>
    </div>
    """

    if len(buggy) > 0:
        buggy_rows = []
        for _, r in buggy.iterrows():
            buggy_rows.append(f"<li><strong>{r['strategy']}</strong>: {r['avg_total_ret']:+.0f}% avg ret, {r['success_rate_pct']:.1f}% success — likely exit logic allows positions to compound indefinitely</li>")
        html += f"""
    <div class="warning-box">
        <h3>⚠️ Buggy Strategies Excluded</h3>
        <ul>{''.join(buggy_rows)}</ul>
    </div>
    """

    html += build_cards(realistic) + \
        build_ranking_table(realistic, 'success_rate_pct', 45, '🏆 Ranked by Success Rate (% of tickers profitable)') + \
        build_ranking_table(realistic, 'median_total_ret', 45, '📊 Ranked by Median Return (most consistent per-ticker outcome)') + \
        build_ranking_table(realistic, 'avg_profit_factor', 45, '⚖️ Ranked by Profit Factor (gross win / gross loss)') + \
        f"""
    <div class="section">
        <div class="section-title">💡 How to Read This Report</div>
        <div class="section-desc">
            <strong>Success Rate:</strong> % of the {n_tickers:,} tickers where the strategy ended profitable (final equity > $100K). Core metric — high success = strategy works broadly.<br>
            <strong>Median Return:</strong> Median total return across all tickers. Less distorted by outliers than average return. If median is negative but success rate is high, the strategy depends on a few home runs.<br>
            <strong>Win Rate:</strong> Average per-trade win rate across tickers. Trend-following typically has low win rates (20-40%) but large winners.<br>
            <strong>Profit Factor:</strong> Gross wins / gross losses. Above 2.0 is strong.<br>
            <strong>Max Drawdown:</strong> Average maximum peak-to-trough decline per ticker. Higher = more volatile equity curve.<br>
            <strong>Source:</strong> github.com/trustdan/trend-following-backtesting-strategies (Turtle/Seykota engine) — 48 strategy variants across Donchian, EMA, ATR channel, Keltner, with pyramiding, trailing stops, and profit targets.
        </div>
    </div>
    """ + f"""
<div class="footer">
  <p>Generated {now} | {len(realistic)} strategies across {n_tickers:,} tickers</p>
</div>
</div>
</body>
</html>"""

    OUTPUT_HTML.write_text(html, encoding='utf-8')
    print(f"✅ Report saved to {OUTPUT_HTML}")
    print(f"   Size: {OUTPUT_HTML.stat().st_size:,} bytes")


if __name__ == "__main__":
    generate()
