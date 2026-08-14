"""
recommend_live_breakouts.py

Score every ACTIONABLE live pattern in `python/tv_pattern_results.json` with the win-rate
lifts measured by `python/backtests/tv_pattern_history_backtest.py` (the same data they
come from) and recommend the top candidates to take a breakout.

Actionable pools (a buy point is live or the breakout is fresh):

  * "In Base" patterns whose close sits within ±5% of the pivot — about to break out;
  * "Post-BO" patterns no more than 5 bars past the breakout — still inside the 60-bar
    target window with most of it ahead.

The model is transparent: P(Target) ≈ 29.6% baseline + the sum of per-characteristic lifts
(pp) from the backtest report. It is a RANKING score, not a calibrated probability — the
caveat at the bottom of the backtest report applies (in-sample, correlated dimensions), and
the market regime (SPY vs its 50/200-day MAs on the scan date) shifts every candidate the
same way and is reported, not scored.

Only factors with a meaningful backtest lift (|lift| >= 1.5 pp) are used, and the total is
clamped so no single combo can over-stack.

Usage:
    python3 python/backtests/recommend_live_breakouts.py
    python3 python/backtests/recommend_live_breakouts.py --top 10

Outputs (in python/backtests/):
    live_breakout_picks.csv         - every scored actionable candidate
    live_breakout_picks.md          - the shortlist with per-ticker drivers
    live_breakout_picks_summary.csv - top-N shortlist (matches the repo's *_summary.csv
                                      convention, shows in Backtest "Summary CSVs")
    live_breakout_picks_report.html - dark-themed HTML report (Backtest "HTML Reports")
    watchlist_history.log           - dated picks block appended (same log as
                                      daily_watchlist_runner.py writes to)
"""

import argparse
import html
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = Path(__file__).resolve().parent
RESULTS_JSON = ROOT / "python" / "tv_pattern_results.json"
TICKER_CACHE_DIR = ROOT / "ticker_cache"

PICKS_CSV = OUT_DIR / "live_breakout_picks.csv"
PICKS_MD = OUT_DIR / "live_breakout_picks.md"
PICKS_SUMMARY_CSV = OUT_DIR / "live_breakout_picks_summary.csv"
PICKS_REPORT_HTML = OUT_DIR / "live_breakout_picks_report.html"
WATCHLIST_LOG = OUT_DIR / "watchlist_history.log"

BASELINE = 29.6          # resolved win rate from the history backtest
MAX_DIST_PCT = 5.0       # In Base: within this % of the pivot
MAX_BARS_SBO = 5         # Post-BO: at most this many bars since breakout
MIN_PBO_DIST = -8.0      # Post-BO: not too deep below the pivot (failed breakout)
MAX_PBO_DIST = 10.0      # Post-BO: not already extended past the entry zone
CAP_PP = 40.0            # clamp total bonus so no combo over-stacks


def _lift_price(p):
    if p < 10:
        return 14.0
    if p < 25:
        return 2.3
    if p < 50:
        return -1.5
    if p < 100:
        return -1.7
    if p < 250:
        return -2.8
    return -4.8


def _lift_shape(shape):
    return {"Consolidation": 4.5, "Flat Base": -2.6}.get(shape, -1.0)  # None = Cup


def _lift_depth(d):
    if d < 10:
        return -4.4
    if d < 15:
        return -3.6
    if d < 20:
        return 0.1
    if d < 25:
        return 2.2
    if d < 30:
        return 4.4
    if d < 35:
        return 3.1
    return -2.0


def _lift_days(n):
    if n < 30:
        return 0.7
    if n < 45:
        return 2.3
    if n < 60:
        return 1.6
    if n < 90:
        return 1.5
    if n < 120:
        return -5.1
    if n < 160:
        return -5.0
    if n < 200:
        return -7.6
    return -11.1


def _lift_dis_ratio(r):
    if r < 0.15:
        return 5.0
    if r < 0.20:
        return 2.9
    if r < 0.25:
        return -0.4
    if r < 0.30:
        return -2.0
    return 1.0


def _lift_acc_ratio(r):
    if r >= 0.30:
        return 4.3
    if r >= 0.25:
        return 0.5
    if r >= 0.20:
        return -1.4
    if r >= 0.15:
        return -1.8
    return 2.2


def _lift_cup_bars(pat, n):
    if pat != "Cup":            # bases all carry 0 cup-bars; the lift belongs to cups only
        return 0.0
    if n < 1:
        return 1.3
    if n < 5:
        return 1.4
    if n < 10:
        return 1.8
    if n < 20:
        return -3.2
    return -5.9


_atr_cache = {}

def _atr21_pct(ticker):
    """21-day ATR% from the cached daily bars, same convention as build_daily_screener.py
    (mean of the last 21 true ranges / close * 100). Cached per ticker."""
    if ticker in _atr_cache:
        return _atr_cache[ticker]
    val = None
    try:
        df = pd.read_parquet(TICKER_CACHE_DIR / f"{ticker}_1d.parquet")
        if df is not None and len(df) >= 22:
            high = df["High"].to_numpy(dtype=float)
            low = df["Low"].to_numpy(dtype=float)
            close = df["Close"].to_numpy(dtype=float)
            prev_close = np.roll(close, 1)
            prev_close[0] = close[0]
            tr = np.maximum(high - low,
                            np.maximum(np.abs(high - prev_close),
                                       np.abs(low - prev_close)))
            atr21 = float(np.mean(tr[-21:]))
            c = float(close[-1])
            if c > 0:
                val = round(atr21 / c * 100.0, 2)
    except Exception:
        val = None
    _atr_cache[ticker] = val
    return val


def _drivers(r, price_lift, struct_lift):
    """Human-readable list of what moved the score for this record."""
    out = []
    pat, shape = r.get("pattern_name"), r.get("base_shape")
    out.append(f"${r.get('pivot', 0):,.0f} price ({price_lift:+.1f})")
    out.append(f"{shape or pat} ({struct_lift.get('shape', 0):+.1f})")
    out.append(f"depth {r.get('base_depth_pct', 0):.0f}% ({struct_lift.get('depth', 0):+.1f})")
    out.append(f"{r.get('days_in_base', 0):.0f}d ({struct_lift.get('days', 0):+.1f})")
    d = r.get("dis_days", 0)
    n = r.get("days_in_base", 0)
    acc = r.get("acc_days", 0)
    out.append(f"dis-ratio {d / n * 100:.0f}% ({struct_lift.get('dis', 0):+.1f})")
    out.append(f"acc-ratio {acc / n * 100:.0f}% ({struct_lift.get('acc', 0):+.1f})")
    if pat == "Cup" and struct_lift.get("cup"):
        out.append(f"cup-bars {r.get('cup_bars_in_base', 0):.0f} ({struct_lift['cup']:+.1f})")
    det = (r.get("score") or {}).get("before_bo_detail") or {}
    if det.get("rs_new_high"):
        out.append("RS new high before BO (+4.2)")
    if det.get("vol_dry_up"):
        out.append("volume dry-up (-6.5)")
    if det.get("shakeout"):
        out.append("shakeout (-2.4)")
    return "; ".join(out)


def _entry_str(r):
    """Human-readable entry zone for a scored row (both display formats)."""
    if r["status"] == "In Base":
        return f"{abs(r['dist_or_sbo']):.0f}% below pivot"
    s = f"{r['dist_or_sbo']:.0f} bar{'s' if r['dist_or_sbo'] != 1 else ''} ago"
    if r["dist_pct"] < 0:
        s += f" · {abs(r['dist_pct']):.0f}% below pivot (retest)"
    else:
        s += f" · {r['dist_pct']:.0f}% above pivot"
    return s


def _write_html_report(top, scored, raw, regime):
    """Self-contained dark-themed HTML report (same visual language as the other
    *_report.html files in python/backtests/)."""
    gen = html.escape(str(raw.get("generated_at", "?")))
    uni = raw.get("universe", 0)
    regime_e = html.escape(str(regime))
    n_actionable = len(scored)
    n_in = int((scored["status"] == "In Base").sum())
    n_pbo = n_actionable - n_in
    best = float(top["model_win_pct"].iloc[0]) if len(top) else 0.0

    top_rows = "\n".join(
        f"<tr><td class=\"num\">{i}</td>"
        f"<td class=\"ticker\">{html.escape(str(r['ticker']))}</td>"
        f"<td><span class=\"badge {r['status'].lower().replace(' ', '-')}\">{r['status']}</span></td>"
        f"<td>{html.escape(str(r['pattern']))} / {html.escape(str(r['shape']))}</td>"
        f"<td class=\"num\">${r['pivot']:,.2f}</td>"
        f"<td>{html.escape(_entry_str(r))}</td>"
        f"<td class=\"num model\">{r['model_win_pct']:.0f}%</td></tr>"
        for i, (_, r) in enumerate(top.iterrows(), 1))

    drv_rows = "\n".join(
        f"<li><strong>{i}. {html.escape(str(r['ticker']))}</strong> "
        f"<span class=\"model\">{r['model_win_pct']:.0f}%</span> — {html.escape(str(r['drivers']))}</li>"
        for i, (_, r) in enumerate(top.iterrows(), 1))

    all_rows = "\n".join(
        f"<tr><td class=\"num\">{i}</td>"
        f"<td class=\"ticker\">{html.escape(str(r['ticker']))}</td>"
        f"<td><span class=\"badge {r['status'].lower().replace(' ', '-')}\">{r['status']}</span></td>"
        f"<td>{html.escape(str(r['pattern']))} / {html.escape(str(r['shape']))}</td>"
        f"<td class=\"num\">${r['pivot']:,.2f}</td>"
        f"<td class=\"num\">{r['dist_pct']:+.1f}%</td>"
        f"<td class=\"num\">{r['days_in_base']:.0f}d / {r['depth_pct']:.0f}%</td>"
        f"<td class=\"num\">{r['price_lift']:+.1f}</td>"
        f"<td class=\"num\">{r['struct_sig_lift']:+.1f}</td>"
        f"<td class=\"num model\">{r['model_win_pct']:.0f}%</td></tr>"
        for i, (_, r) in enumerate(scored.head(50).iterrows(), 1))

    report = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Breakout Picks — {gen}</title>
<style>
:root {{
  --bg: #0f1117; --bg2: #1a1d27; --bg3: #232733;
  --text: #e2e8f0; --text2: #94a3b8; --accent: #60a5fa;
  --green: #22c55e; --yellow: #eab308; --red: #ef4444;
  --border: #2d3143; --radius: 10px;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.6;
}}
.container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
.header {{
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 50%, #1e293b 100%);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 36px; margin-bottom: 28px; text-align: center;
}}
.header h1 {{ font-size: 2em; font-weight: 700; margin-bottom: 6px;
  background: linear-gradient(90deg, #60a5fa, #a78bfa);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
.header .subtitle {{ color: var(--text2); font-size: 1.05em; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 26px; }}
.card {{
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; text-align: center; transition: transform 0.15s, box-shadow 0.15s;
}}
.card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 20px rgba(96,165,250,0.1); }}
.card-value {{ font-size: 1.6em; font-weight: 700; color: var(--accent); }}
.card-label {{ font-size: 0.85em; color: var(--text2); margin-top: 4px; }}
.section {{
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 26px; margin-bottom: 24px;
}}
.section-title {{ font-size: 1.25em; font-weight: 700; margin-bottom: 4px; color: var(--accent); }}
.section-desc {{ font-size: 0.9em; color: var(--text2); margin-bottom: 16px; }}
.table-scroll {{ overflow-x: auto; }}
.data-table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
.data-table th {{
  background: var(--bg3); padding: 10px 12px; text-align: left; font-weight: 600;
  color: var(--text2); border-bottom: 2px solid var(--border); white-space: nowrap;
}}
.data-table td {{ padding: 9px 12px; border-bottom: 1px solid var(--border); }}
.data-table tr:hover {{ background: var(--bg3); }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.ticker {{ font-weight: 700; }}
.model {{ color: var(--green); font-weight: 700; }}
.badge {{
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 0.75em; font-weight: 600;
}}
.badge.in-base {{ background: #1e3a5f; color: #7dd3fc; }}
.badge.post-bo {{ background: #14351f; color: #86efac; }}
.drivers li {{ margin-bottom: 10px; }}
.drivers .model {{ color: var(--accent); }}
.footer {{
  color: var(--text2); font-size: 0.82em; text-align: center;
  padding: 10px 0 30px;
}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🚀 Live Breakout Picks</h1>
    <div class="subtitle">Scan {gen} · universe {uni:,} · <strong>market: {regime_e}</strong></div>
  </div>
  <div class="cards">
    <div class="card"><div class="card-value">{n_actionable:,}</div><div class="card-label">Actionable Candidates</div></div>
    <div class="card"><div class="card-value">{n_in:,}</div><div class="card-label">In Base (about to break out)</div></div>
    <div class="card"><div class="card-value">{n_pbo:,}</div><div class="card-label">Post-BO (fresh breakout)</div></div>
    <div class="card"><div class="card-value" style="color:var(--green)">{best:.0f}%</div><div class="card-label">Top Model Win %</div></div>
    <div class="card"><div class="card-value">29.6%</div><div class="card-label">Backtest Baseline</div></div>
  </div>
  <div class="section">
    <div class="section-title">Top {len(top)} Picks</div>
    <div class="section-desc">Ranked by the transparent lift model: 29.6% baseline + price bucket + shape / depth / length + dis/acc ratios + cup bars + pre-BO signals, clamped. It ranks candidates — it is not a calibrated probability.</div>
    <div class="table-scroll"><table class="data-table">
      <thead><tr><th>#</th><th>Ticker</th><th>Status</th><th>Pattern / Shape</th><th>Buy point</th><th>Entry</th><th>Model win %</th></tr></thead>
      <tbody>{top_rows}</tbody>
    </table></div>
  </div>
  <div class="section">
    <div class="section-title">Why Each One Scored</div>
    <ul class="drivers">{drv_rows}</ul>
  </div>
  <div class="section">
    <div class="section-title">All Scored Candidates (top 50 of {n_actionable:,})</div>
    <div class="table-scroll"><table class="data-table">
      <thead><tr><th>#</th><th>Ticker</th><th>Status</th><th>Pattern / Shape</th><th>Buy point</th><th>Dist to pivot</th><th>Base len/depth</th><th>Price lift</th><th>Struct+Signal</th><th>Model win %</th></tr></thead>
      <tbody>{all_rows}</tbody>
    </table></div>
  </div>
  <div class="footer">Model caveats: in-sample lifts from tv_pattern_history_backtest.py; correlated dimensions double-count partially; the $&lt;10 price bonus barely exists in the current live set. Post-BO picks trading <strong>below</strong> the pivot are retests — buy-on-reclaim entries, not fresh breakouts.</div>
</div>
</body>
</html>
"""
    PICKS_REPORT_HTML.write_text(report)
    print(f"Wrote {PICKS_REPORT_HTML}")


def _append_watchlist_log(top, scored, raw, regime):
    """Append a dated picks block to watchlist_history.log (the same log the daily
    watchlist runner's cron redirects into), so the picks are part of the history."""
    gen = raw.get("generated_at", "?")
    lines = [
        "",
        "=" * 80,
        f"🚀 LIVE BREAKOUT PICKS — {date.today().isoformat()} (scan {gen})",
        "=" * 80,
        f"Market: {regime}",
        f"Scored: {len(scored):,} actionable · top {len(top)} recommended",
    ]
    for i, (_, r) in enumerate(top.iterrows(), 1):
        lines.append(f"  {i:>2}. {r['ticker']:<8} {r['status']:<9} "
                     f"{r['pattern']}/{r['shape']:<18} buy ${r['pivot']:>9,.2f}  "
                     f"{_entry_str(r):<34} {r['model_win_pct']:>5.1f}%")
    lines.append("=" * 80)
    lines.append("")
    with open(WATCHLIST_LOG, "a") as fh:
        fh.write("\n".join(lines))
    print(f"Appended picks to {WATCHLIST_LOG}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=10, help="how many to recommend (default 10)")
    ap.add_argument("--no-log", action="store_true",
                    help="skip appending the picks block to watchlist_history.log")
    args = ap.parse_args()

    with open(RESULTS_JSON) as fh:
        raw = json.load(fh)
    res = raw.get("results", [])
    if not res:
        sys.exit("no live patterns in tv_pattern_results.json")
    print(f"Loaded {len(res):,} live patterns (scan {raw.get('generated_at')}, "
          f"universe {raw.get('universe'):,}).")

    # ── market regime context (reported, not scored: same shift for every candidate) ──────
    regime = "unknown"
    try:
        spy = pd.read_parquet(TICKER_CACHE_DIR / "SPY_1d.parquet").sort_index()
        spy = spy[~spy.index.duplicated(keep="last")]
        c = pd.to_numeric(spy["Close"], errors="coerce")
        s50 = c.rolling(50, min_periods=30).mean()
        s200 = c.rolling(200, min_periods=120).mean()
        last = spy.index[-1]
        cl, a50, a200 = float(c.iloc[-1]), float(s50.iloc[-1]), float(s200.iloc[-1])
        if cl > a50 and cl > a200:
            regime = "Bull (above 50 & 200)"
        elif cl < a50 and cl < a200:
            regime = "Bear (below 50 & 200)"
        else:
            regime = "Mixed"
        regime += f" · SPY {cl:,.0f} vs SMA50 {a50:,.0f}/SMA200 {a200:,.0f} " \
                  f"({(cl / a200 - 1) * 100:+.0f}% vs 200) on {last.date()}"
    except Exception as e:
        print(f"  (regime unavailable: {e})")
    print(f"Market regime: {regime}")

    # ── score the actionable pools ────────────────────────────────────────────────────────
    rows = []
    for r in res:
        status = r.get("status")
        if status == "In Base":
            d = r.get("dist_pct")
            if d is None or abs(d) > MAX_DIST_PCT:
                continue
        elif status == "Post-BO":
            if (r.get("bars_sbo") or 999) > MAX_BARS_SBO:
                continue
            d = r.get("dist_pct")
            # A breakout already 10%+ past the buy point is chasing, not a fresh entry
            # (and if it is 20%+ past, the +20% target is already hit). Below -8% the
            # breakout has failed back under the base. Either way: not recommendable.
            if d is None or not (MIN_PBO_DIST <= d <= MAX_PBO_DIST):
                continue
        else:
            continue

        pivot = r.get("pivot")
        if not pivot or (isinstance(pivot, float) and math.isnan(pivot)) or pivot <= 0:
            continue
        days = r.get("days_in_base") or 0
        dis = r.get("dis_days") or 0
        acc = r.get("acc_days") or 0

        pl = _lift_price(pivot)
        sl = {
            "shape": _lift_shape(r.get("base_shape")),
            "depth": _lift_depth(r.get("base_depth_pct") or 0),
            "days": _lift_days(days),
            "dis": _lift_dis_ratio(dis / days if days else 0),
            "acc": _lift_acc_ratio(acc / days if days else 0),
            "cup": _lift_cup_bars(r.get("pattern_name"), r.get("cup_bars_in_base") or 0),
        }
        det = (r.get("score") or {}).get("before_bo_detail") or {}
        sig = 0.0
        if det.get("rs_new_high"):
            sig += 4.2
        if det.get("vol_dry_up"):
            sig -= 6.5
        if det.get("shakeout"):
            sig -= 2.4

        bonus = sum(sl.values()) + sig
        bonus = max(-CAP_PP, min(CAP_PP, bonus))
        prob = max(8.0, min(80.0, BASELINE + pl + bonus))

        rows.append({
            "ticker": r["ticker"],
            "status": status,
            "pattern": r.get("pattern_name"),
            "shape": r.get("base_shape") or r.get("pattern_name"),
            "pivot": round(pivot, 2),
            "dist_pct": round(d, 1),          # vs pivot: negative = below (pullback)
            "dist_or_sbo": (round(d, 1) if status == "In Base"
                            else r.get("bars_sbo")),
            "days_in_base": days,
            "depth_pct": round(r.get("base_depth_pct") or 0, 1),
            "atr21_pct": _atr21_pct(r["ticker"]),
            "price_lift": round(pl, 1),
            "struct_sig_lift": round(bonus, 1),
            "model_win_pct": round(prob, 1),
            "drivers": _drivers(r, pl, sl),
        })

    if not rows:
        sys.exit("no actionable candidates (In Base within ±5% of pivot, or Post-BO ≤ 5 bars)")

    scored = pd.DataFrame(rows).sort_values("model_win_pct", ascending=False)
    scored.to_csv(PICKS_CSV, index=False)
    print(f"Wrote {PICKS_CSV} ({len(scored):,} actionable candidates scored)")

    top = scored.head(args.top)
    lines = []
    lines.append(f"# Live Breakout Picks — top {len(top)} of {len(scored):,} actionable\n")
    lines.append(f"**Scan {raw.get('generated_at')}** · universe {raw.get('universe'):,} · "
                 f"**market: {regime}**\n")
    lines.append("> Model = 29.6% baseline + backtest lifts (price, shape, depth, days, "
                 "dis/acc ratios, cup, pre-BO signals). It ranks candidates; it is not a "
                 "calibrated probability. In Base = close within ±5% of the pivot "
                 f"(will break out); Post-BO = ≤ {MAX_BARS_SBO} bars since the breakout.\n")
    lines.append("| # | Ticker | Status | Pattern / Shape | Buy point | Entry | Model win % |")
    lines.append("|---|---|---|---:|---:|---:|---:|")
    for i, (_, r) in enumerate(top.iterrows(), 1):
        lines.append(f"| {i} | **{r['ticker']}** | {r['status']} | "
                     f"{r['pattern']} / {r['shape']} | ${r['pivot']:,.2f} | {_entry_str(r)} | "
                     f"{r['model_win_pct']:.0f}% |")
    lines.append("")
    lines.append("## Why each one scored (drivers)\n")
    for i, (_, r) in enumerate(top.iterrows(), 1):
        lines.append(f"**{i}. {r['ticker']}** ({r['model_win_pct']:.0f}%) — {r['drivers']}")
    lines.append("")
    lines.append("---")
    lines.append("*Model caveats: in-sample lifts from "
                 "`tv_pattern_history_backtest.py`; correlated dimensions double-count "
                 "partially; the $<10 price bonus barely exists in the current live set "
                 "(1 candidate). Post-BO picks trading **below** the pivot are retests — "
                 "they have already broken out and pulled back under the buy point, so "
                 "treat them as buy-on-reclaim entries, not fresh breakouts. Full scored "
                 "set (with `dist_pct` per candidate) in `live_breakout_picks.csv`.*")
    PICKS_MD.write_text("\n".join(lines) + "\n")
    print(f"Wrote {PICKS_MD}")

    # ── Summary CSV (top-N; matches the repo's *_summary.csv convention, so it shows
    #    up in Backtest "Summary CSVs" and Scans & Leads "Performance") ──────────────
    summ = top.copy()
    summ.insert(0, "rank", range(1, len(summ) + 1))
    summ["entry"] = summ.apply(_entry_str, axis=1)
    summ.to_csv(PICKS_SUMMARY_CSV, index=False)
    print(f"Wrote {PICKS_SUMMARY_CSV} ({len(summ)} rows)")

    # ── HTML report (matches *_report.html convention, shows in Backtest "HTML Reports") ──
    _write_html_report(top, scored, raw, regime)

    # ── watchlist_history.log ──
    if not args.no_log:
        _append_watchlist_log(top, scored, raw, regime)

    print(f"\n{'TOP BREAKOUT PICKS':─^78}")
    print(f"{'#':>2}  {'TICKER':<8} {'STATUS':<9} {'PATTERN/SHAPE':<20} {'BUY':>10} "
          f"{'ENTRY':>12} {'MODEL%':>7}")
    for i, (_, r) in enumerate(top.iterrows(), 1):
        print(f"{i:>2}  {r['ticker']:<8} {r['status']:<9} "
              f"{r['pattern'] + '/' + r['shape']:<20} ${r['pivot']:>9,.2f} "
              f"{_entry_str(r):>26} {r['model_win_pct']:>6.1f}%")


if __name__ == "__main__":
    main()
