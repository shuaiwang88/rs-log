"""
build_modelbook.py

Build a **model book** of the successful breakouts from the TV pattern history —
`python/tv_pattern_history.json` (every base `pine/drw_pattern.pine` has ever ended,
scored by the scanner's own walk-forward: +20% target vs −8% stop over 60 bars).

For every winning breakout (outcome == "Target"), this script renders a chart that is
*exactly* the 📐 TV Pattern tab's chart — it reuses `tv_pattern_chart.build_tv_pattern_figure`,
feeding it the same `overlay` geometry (base channel, cup arcs, trade boxes, 3-weeks-tight
squeeze boxes) reconstructed from the history record — and exports it to PNG via kaleido.

Each chart shows:
  * **up to ~1 year of daily bars before the breakout** (252 trading days; extended to the
    full base + 30 days when the base is longer, so the pattern itself never gets clipped)
  * **up to ~6 months of daily bars after the breakout** (126 trading days, or less if the
    cached history ends sooner)
  * the base channel + buy point, the scanner's trade boxes (entry +5% / stop −8% / target
    +20% bands), the cup arcs (Cup / Cup+Handle), the Pine's Tight Closes squeeze boxes, and
    a ▲ marker on the breakout bar.

By default **every** winning breakout is charted (`--n 0` = all), ranked by max gain, with
degenerate cases excluded so the book teaches real breakouts rather than data glitches:
  * pivot (buy point) >= $5            (skip microcap / sub-penny noise)
  * max gain <= 300%                   (skip corporate-action / split artifacts)
  * bars-to-target >= 2                (a 1-bar vertical spike is a data artifact, not a
                                         teachable breakout)
Pass `--min-pivot 0 --max-gain 99999 --min-bars-to-outcome 0` to include those too.

Charts render in parallel (`--workers`, default 4) because kaleido's write_image dominates
the runtime; 4,000+ charts take ~1.5-2h across 4 workers.

Usage:
    python3 python/backtests/build_modelbook.py                      # all winners, 4 workers
    python3 python/backtests/build_modelbook.py --workers 8
    python3 python/backtests/build_modelbook.py --rank score         # penalize max drawdown
    python3 python/backtests/build_modelbook.py --n 100 --per-ticker 2   # quick sample
    python3 python/backtests/build_modelbook.py --min-pivot 10 --max-gain 150

Outputs (in output/modelbook/):
    NNNN_TICKER_PATTERN_BO-YYYY-MM-DD.png   one chart per breakout, rank-ordered
    modelbook_index.csv                     every chart + the trade stats behind it
    modelbook_index.html                    dark gallery page to flip through the book
    README.md                               methodology + the selection table
"""

import argparse
import json
import math
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "output" / "modelbook"
HISTORY_JSON = ROOT / "python" / "tv_pattern_history.json"
TICKER_CACHE = ROOT / "ticker_cache"

sys.path.insert(0, str(ROOT / "python"))
from tv_pattern_scanner import MAX_BARS, _tight_closes      # noqa: E402
from tv_pattern_chart import build_tv_pattern_figure        # noqa: E402

PRE_BARS = 252    # up to a year of daily bars before the breakout
POST_BARS = 126   # up to six months of daily bars after the breakout

UP, DOWN = "#26a69a", "#ef5350"
BO_C = "#FFD54F"


def _trim_frame(df):
    """Mirror scan_ticker's frame prep so the history's dates map onto the parquet."""
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(subset=["High", "Low", "Close"])
    if len(df) > MAX_BARS:
        df = df.iloc[-MAX_BARS:]
    return df


def _record_to_overlay(rec, idx, o, h, l, c):
    """History record -> the `overlay` dict build_tv_pattern_figure draws.

    The live scan emits this geometry as it walks the frame; the history record already
    carries the same anchors (the scan wrote them), so nothing is re-inferred. Trade boxes
    use the scanner's own constants (tv_pattern_scanner.py lines 1622-1628): entry
    pivot..+5%, stop -5%..-8%, target +20%..+25%.
    """
    pivot = rec.get("pivot")
    ov = {
        "base_start_date": rec.get("base_start_date") or rec.get("start_date"),
        "base_low_date": rec.get("base_low_date"),
        "base_top": rec.get("base_top") or pivot,
        "base_low": rec.get("base_low"),
        "base_shape": rec.get("base_shape"),
        "last_date": rec.get("end_date"),
        "cup": rec.get("cup") if isinstance(rec.get("cup"), dict) else None,
    }
    if pivot and isinstance(pivot, (int, float)) and not math.isnan(pivot):
        ov["boxes"] = {
            "start_date": rec.get("end_date"),
            "end_date": None,                     # run the trade bands through the 6-mo window
            "entry": [pivot, pivot * 1.05],
            "stop": [pivot * 0.92, pivot * 0.95],
            "target": [pivot * 1.20, pivot * 1.25],
        }
    # 3-weeks-tight squeeze boxes (drw_pattern.pine:409-440) over the base window - the
    # chart's show_tight toggle draws these from overlay["tight_closes"].
    try:
        bs = rec.get("base_start_date") or rec.get("start_date")
        since = int(idx.searchsorted(pd.Timestamp(bs))) if bs else 0
        tights = _tight_closes(idx, o, h, l, c, since)
        if tights:
            ov["tight_closes"] = tights
    except Exception:
        pass
    return ov


def _record_to_result(rec, overlay):
    return {
        "pattern_name": rec.get("pattern"),
        "base_shape": rec.get("base_shape"),
        "status": "Post-BO",
        "bars_sbo": 0,
        "pivot": rec.get("pivot"),
        "days_in_base": rec.get("days"),
        "base_depth_pct": rec.get("depth_pct"),
        "acc_days": rec.get("acc_days", 0),
        "dis_days": rec.get("dis_days", 0),
        "neu_days": rec.get("neu_days", 0),
        "overlay": overlay,
    }


def _score(rec):
    """Risk-adjusted success score: reward gain, penalize drawdown (2x)."""
    return rec["max_gain_pct"] - 2.0 * abs(rec.get("max_drawdown_pct") or 0.0)


def _json_safe_fig(fig):
    """kaleido's write_image serialises through a strict JSON bridge and chokes on pandas
    Timestamps / numpy scalars that plotly's own encoder handles for the browser. Walk the
    figure dict and convert them before export."""
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        if isinstance(v, pd.Timestamp):
            return str(v.date())
        if isinstance(v, np.datetime64):
            return str(pd.Timestamp(v).date())
        if isinstance(v, (np.integer, np.floating)):
            return v.item()
        if isinstance(v, np.ndarray):
            return [clean(x) for x in v.tolist()]
        return v
    return go.Figure(clean(fig.to_dict()))


def _render_one(job):
    """Render one breakout chart in a worker process. job = (rank, rec, out_dir_str).

    Returns (rank, row_dict_or_None, error_or_None). Kept module-level so it is picklable
    under multiprocessing (spawn on macOS).
    """
    i, rec, out_dir_str = job
    out_dir = Path(out_dir_str)
    ticker = rec["ticker"]
    fp = TICKER_CACHE / f"{str(ticker).strip().replace('.', '-')}_1d.parquet"
    if not fp.exists():
        return i, None, f"{ticker}: no cache parquet"
    try:
        full = _trim_frame(pd.read_parquet(fp))
        if not {"Open", "High", "Low", "Close", "Volume"} <= set(full.columns):
            return i, None, f"{ticker}: bad schema"
        idx = pd.DatetimeIndex(full.index)
        bo_ts = pd.Timestamp(rec["end_date"])
        loc = idx.searchsorted(bo_ts)
        if loc >= len(idx) or idx[loc] != bo_ts:
            # one-bar tolerance - the cache may have refreshed since the scan
            if loc > 0 and idx[loc - 1] == bo_ts:
                loc -= 1
            elif loc < len(idx) - 1 and idx[loc + 1] == bo_ts:
                loc += 1
            else:
                return i, None, f"{ticker}: BO date {rec['end_date']} not in cache"

        base_days = rec.get("days") or 0
        pre = max(PRE_BARS, int(base_days) + 30)   # keep the whole base in view
        start = max(0, loc - pre)
        end = min(len(full), loc + POST_BARS + 1)
        wdf = full.iloc[start:end]
        if len(wdf) < 60:
            return i, None, f"{ticker}: window too short"

        overlay = _record_to_overlay(rec, idx, full["Open"].to_numpy(float),
                                     full["High"].to_numpy(float),
                                     full["Low"].to_numpy(float),
                                     full["Close"].to_numpy(float))
        res = _record_to_result(rec, overlay)
        fig = build_tv_pattern_figure(ticker, wdf, res, bars=len(wdf), show_tight=True)

        # ── modelbook dressing: breakout marker + stats title ──────────────────────────
        y_lo = float(wdf["Low"].min()) * 0.98
        y_hi = float(wdf["High"].max()) * 1.02
        fig.add_shape(type="line", x0=rec["end_date"], x1=rec["end_date"],
                      y0=y_lo, y1=y_hi, xref="x", yref="y",
                      line=dict(color=BO_C, width=1.2, dash="dot"))
        fig.add_annotation(x=rec["end_date"], y=y_hi, text="▲ BO", showarrow=False,
                           xanchor="center", yanchor="bottom",
                           font=dict(color=BO_C, size=11))
        fig.add_annotation(x=str(pd.Timestamp(wdf.index[0]).date()), y=y_hi, text="1y",
                           showarrow=False, xanchor="left", yanchor="bottom",
                           font=dict(color="#8b949e", size=9))
        fig.add_annotation(x=str(pd.Timestamp(wdf.index[-1]).date()), y=y_hi, text="+6m",
                           showarrow=False, xanchor="right", yanchor="bottom",
                           font=dict(color="#8b949e", size=9))

        shape = rec.get("base_shape")
        label = f"{rec.get('pattern', '')}" + (f" · {shape}" if shape else "")
        dd = rec.get("max_drawdown_pct") or 0.0
        title = (f"{ticker} · {label} · BO {rec['end_date']}"
                 f"<br><span style='font-size:11px'>#{i} · Pivot ${rec.get('pivot', float('nan')):,.2f}"
                 f" · base {rec.get('days') or 0}d {rec.get('depth_pct') or 0:.1f}% deep · "
                 f"max +{rec['max_gain_pct']:.1f}% / −{abs(dd):.1f}% DD · "
                 f"{rec.get('bars_to_outcome')} bars to +20% target</span>")
        fig.update_layout(title=dict(text=title, font=dict(size=14)),
                          width=1280, height=760)

        fname = f"{i:04d}_{ticker}_{rec.get('pattern', 'PAT').replace(' ', '')}_BO-{rec['end_date']}.png"
        out_png = out_dir / fname
        _json_safe_fig(fig).write_image(str(out_png), scale=2)

        row = {
            "rank": i, "ticker": ticker, "pattern": rec.get("pattern"),
            "base_shape": shape, "bo_date": rec["end_date"],
            "base_start": rec.get("start_date"), "base_days": rec.get("days"),
            "depth_pct": round(rec.get("depth_pct") or 0.0, 1),
            "pivot": round(rec.get("pivot") or float("nan"), 2),
            "max_gain_pct": round(rec["max_gain_pct"], 1),
            "max_drawdown_pct": round(dd, 1),
            "bars_to_target": rec.get("bars_to_outcome"),
            "window_start": str(pd.Timestamp(wdf.index[0]).date()),
            "window_end": str(pd.Timestamp(wdf.index[-1]).date()),
            "pre_bars": int(loc - start), "post_bars": int(end - 1 - loc),
            "file": fname,
        }
        return i, row, None
    except Exception as e:
        return i, None, f"{ticker}: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=0,
                    help="number of breakouts to chart; 0 = ALL winners (default 0)")
    ap.add_argument("--min-pivot", type=float, default=5.0,
                    help="minimum buy-point price to include (default 5.0)")
    ap.add_argument("--max-gain", type=float, default=300.0,
                    help="cap on max_gain_pct - drops corporate-action artifacts (default 300)")
    ap.add_argument("--min-bars-to-outcome", type=int, default=2,
                    help="skip 1-bar vertical spikes (data artifacts) (default 2)")
    ap.add_argument("--per-ticker", type=int, default=0,
                    help="max charts per ticker for variety; 0 = unlimited (default 0)")
    ap.add_argument("--rank", choices=["gain", "score"], default="gain",
                    help="gain = raw max gain (default); score = gain - 2x max drawdown")
    ap.add_argument("--workers", type=int, default=min(4, (mp.cpu_count() or 1)),
                    help="parallel render workers (default 4)")
    ap.add_argument("--history", type=Path, default=HISTORY_JSON)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    t0 = time.time()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    history = json.load(open(args.history))
    pats = history.get("patterns", [])
    wins = [p for p in pats
            if p.get("ended") == "Breakout" and p.get("outcome") == "Target"
            and p.get("max_gain_pct") is not None
            and (p.get("pivot") or 0) >= args.min_pivot
            and p["max_gain_pct"] <= args.max_gain
            and (p.get("bars_to_outcome") or 0) >= args.min_bars_to_outcome]
    print(f"{len(wins):,} winners after filters "
          f"(pivot>={args.min_pivot:g}, gain<={args.max_gain:g}%, bars>={args.min_bars_to_outcome})",
          flush=True)

    seen = set()
    uniq = []
    for p in wins:
        k = (p["ticker"], p.get("end_date"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)

    uniq.sort(key=_score if args.rank == "score" else lambda p: p["max_gain_pct"],
              reverse=True)

    chosen = []
    counts = {}
    for p in uniq:
        if args.per_ticker:
            c = counts.get(p["ticker"], 0)
            if c >= args.per_ticker:
                continue
            counts[p["ticker"]] = c + 1
        chosen.append(p)
        if args.n and len(chosen) >= args.n:
            break

    n_chart = len(chosen)
    scope = "ALL" if not args.n else str(args.n)
    print(f"charting {n_chart:,} breakouts ({scope}, rank={args.rank}, "
          f"per-ticker={args.per_ticker}, workers={args.workers})", flush=True)

    jobs = [(i, rec, str(out_dir)) for i, rec in enumerate(chosen, 1)]
    rows, errors = [], []
    done = 0
    with mp.Pool(args.workers) as pool:
        for _i, row, err in pool.imap_unordered(_render_one, jobs, chunksize=8):
            done += 1
            if err:
                errors.append(err)
            if row:
                rows.append(row)
            if done % 100 == 0 or done == n_chart:
                print(f"  [{done:,}/{n_chart:,}] {len(rows):,} charts written "
                      f"({time.time() - t0:.0f}s)", flush=True)

    rows.sort(key=lambda r: r["rank"])
    if errors:
        print(f"  {len(errors)} skipped/errors (first 10):")
        for e in errors[:10]:
            print(f"    - {e}")

    if not rows:
        print("No charts produced - nothing to write.")
        sys.exit(1)

    # ── index CSV ───────────────────────────────────────────────────────────────────────
    csv_path = out_dir / "modelbook_index.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"Wrote {csv_path} ({len(rows)} rows)")

    # ── dark gallery HTML ───────────────────────────────────────────────────────────────
    html_path = out_dir / "modelbook_index.html"
    cards = []
    for r in rows:
        cards.append(f"""
    <div class="card">
      <img src="{r['file']}" alt="{r['ticker']} {r['bo_date']}" loading="lazy">
      <div class="meta">
        <b>#{r['rank']} {r['ticker']}</b> · {r['pattern']}{' · ' + r['base_shape'] if r['base_shape'] else ''}
        <br><span class="stat">BO {r['bo_date']} · Pivot ${r['pivot']:,.2f} · base {r['base_days']}d {r['depth_pct']:.1f}% deep
        · max <b style="color:#34C759">+{r['max_gain_pct']:.1f}%</b> / {abs(r['max_drawdown_pct']):.1f}% DD · {r['bars_to_target']} bars to +20%</span>
        <br><span class="stat">window {r['window_start']} → {r['window_end']} ({r['pre_bars']} pre / {r['post_bars']} post)</span>
      </div>
    </div>""")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Model Book — {len(rows)} Winning Breakouts</title>
<style>
body {{ background:#0d1117; color:#e6edf3; font-family:-apple-system, 'Segoe UI', Roboto, sans-serif;
       margin:0; padding:24px 28px 60px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:#8b949e; font-size:13px; margin-bottom:22px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(520px, 1fr)); gap:18px; }}
.card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; overflow:hidden; }}
.card img {{ width:100%; display:block; }}
.meta {{ padding:10px 12px 12px; font-size:13px; line-height:1.55; }}
.meta b {{ color:#FFD54F; }}
.stat {{ color:#8b949e; }}
</style></head><body>
<h1>📖 Model Book — {len(rows)} Winning Breakouts</h1>
<div class="sub">Every winning breakout (hit the +20% target) ·
pivot &ge; ${args.min_pivot:g} · gain &le; {args.max_gain:g}% · &ge;{args.min_bars_to_outcome} bars to target ·
window ~1yr pre-BO / 6mo post-BO · chart = 📐 TV Pattern renderer</div>
<div class="cards">{''.join(cards)}
</div>
</body></html>"""
    html_path.write_text(html)
    print(f"Wrote {html_path}")

    # ── README ──────────────────────────────────────────────────────────────────────────
    readme = out_dir / "README.md"
    lines = [
        "# 📖 Model Book — Winning Breakouts",
        "",
        f"`{len(rows)}` charts of the successful breakouts from `python/tv_pattern_history.json` "
        "(every base `drw_pattern.pine` ever ended, scored by the scanner's walk-forward: +20% target "
        "vs −8% stop over 60 bars). Built by `python/backtests/build_modelbook.py`.",
        "",
        "## Selection",
        "- `ended == 'Breakout'` and `outcome == 'Target'` (hit the +20% target before the −8% stop)",
        f"- buy point ≥ ${args.min_pivot:g} (skip microcap noise)",
        f"- max gain ≤ {args.max_gain:g}% (skip corporate-action / split artifacts)",
        f"- bars to target ≥ {args.min_bars_to_outcome} (a 1-bar vertical spike is a data artifact)",
        f"- {'max ' + str(args.per_ticker) + ' breakout(s) per ticker' if args.per_ticker else 'every breakout, unlimited per ticker'}",
        f"- ranked by **{'max gain' if args.rank == 'gain' else 'max gain − 2× max drawdown'}**",
        "",
        "## What each chart shows",
        "- up to ~1 year of daily bars **before** the breakout (extended so the whole base is visible)",
        "- up to ~6 months of daily bars **after** the breakout",
        "- the base channel + buy point, trade boxes (entry +5% / stop −8% / target +20%), cup arcs, "
        "3-weeks-tight squeeze boxes, ▲ breakout marker",
        "",
        "## Files",
        "- `NNNN_TICKER_PATTERN_BO-YYYY-MM-DD.png` — one chart per breakout, rank-ordered",
        "- `modelbook_index.csv` — every chart + trade stats",
        "- `modelbook_index.html` — dark gallery page",
        "",
        "## Rebuild with different criteria",
        "```bash",
        "python3 python/backtests/build_modelbook.py --workers 8",
        "python3 python/backtests/build_modelbook.py --rank score   # penalize drawdown",
        "python3 python/backtests/build_modelbook.py --n 100 --per-ticker 2   # quick sample",
        "```",
        "",
        "## Top 20",
        "",
        "| # | Ticker | Pattern | Shape | BO date | Pivot | Base | Max gain | Max DD | Bars→tgt |",
        "|---|--------|---------|-------|---------|-------|------|----------|--------|----------|",
    ]
    for r in rows[:20]:
        lines.append(f"| {r['rank']} | {r['ticker']} | {r['pattern']} | {r['base_shape'] or '—'} | "
                     f"{r['bo_date']} | ${r['pivot']:,.2f} | {r['base_days']}d | "
                     f"+{r['max_gain_pct']:.1f}% | {abs(r['max_drawdown_pct']):.1f}% | "
                     f"{r['bars_to_target']} |")
    readme.write_text("\n".join(lines) + "\n")
    print(f"Wrote {readme}")

    print(f"Done. {len(rows):,}/{n_chart:,} charts in {time.time() - t0:.0f}s -> {out_dir}")


if __name__ == "__main__":
    main()
