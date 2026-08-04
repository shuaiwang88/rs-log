"""Draw the shapes `pine/drw_pattern.pine` itself draws, from tv_pattern_scanner output.

`pattern_chart.py` paints the OTHER scanner's patterns and has to infer geometry from its
per-bar history (a parabola through three points for a cup, a W guessed from the two halves of
the base). Nothing is inferred here: tv_pattern_scanner emits the same anchors the Pine passes
to line.new()/box.new(), so the cup is the indicator's own pair of exponential arcs, the base
channel is its highLine/lowLine pair, the squeeze boxes are its Tight Closes Detector and the
trade boxes are its +5% / -8% / +25% rectangles.

Shape per pattern:

    Cup / Cup+Handle   the two exponential arcs (lines 1017-1048), plus the handle box
    Flat Base          the channel (lines 984-985) shaded, because it is a shallow one
    Consolidation      the same channel, shaded, deeper
    HTF                pole into the flag lines (lines 914-916)
    any of them        3-weeks-tight squeeze boxes (lines 409-440) where the Pine finds them

The cup is drawn as a Scatter trace rather than a `path` shape on purpose, and the reason is
worth keeping: until 2026-08-03 it WAS a path, and it silently drew nothing. The x values here
are pandas Timestamps, and a path is a raw SVG string - `f"{ts}"` renders a Timestamp as
"2026-02-06 00:00:00", whose SPACE is a path coordinate separator, so every point turned into
garbage tokens and plotly dropped the whole shape without an error. (Category axes are not the
problem: a path built from "2026-02-06" strings draws correctly, which is why the sibling
`pattern_chart.py` - it formats its x values with strftime - never hit this.) A Scatter trace
takes the x values as data and cannot be broken this way, so the curve is a trace.

Colours are the Pine's: base lines rgb(146,193,131), handle orange with a blue box, tight
closes aqua, trade boxes blue / red / green.
"""
from typing import Any, Dict

import math

import pandas as pd
import plotly.graph_objects as go

BASE_C = "#92C183"      # color.rgb(146, 193, 131) - base high/low lines and the cup
HANDLE_C = "#FF9800"    # color.orange
HANDLE_BOX_C = "#2196F3"
HTF_C = "#26A69A"
TIGHT_C = "#00BCD4"     # color.aqua - Tight Closes Detector boxes
ENTRY_C = "#007AFF"     # color.rgb(0, 122, 255, 95)
STOP_C = "#FF3B30"      # color.rgb(255, 59, 48, 95)
TARGET_C = "#34C759"    # color.rgb(52, 199, 89, 95)
UP, DOWN = "#26a69a", "#ef5350"


def _pos(index: pd.DatetimeIndex, date_str):
    """Date string -> integer position in the plotted window, or None if it is off-screen."""
    if not date_str:
        return None
    try:
        ts = pd.Timestamp(date_str)
    except Exception:
        return None
    loc = index.searchsorted(ts)
    if loc >= len(index):
        return len(index) - 1
    return int(loc)


def build_tv_pattern_figure(ticker: str, df: pd.DataFrame, result: Dict[str, Any],
                            bars: int = 300, height: int = 640,
                            show_tight: bool = True) -> go.Figure:
    df = df.sort_index()
    if len(df) > bars:
        df = df.iloc[-bars:]
    x = list(df.index)
    idx = pd.DatetimeIndex(df.index)
    n = len(x)

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
        increasing_line_color=UP, decreasing_line_color=DOWN,
        increasing_fillcolor=UP, decreasing_fillcolor=DOWN, name=ticker, yaxis="y"))
    vol_colors = [UP if c >= o else DOWN for c, o in zip(df["Close"], df["Open"])]
    fig.add_trace(go.Bar(x=x, y=df["Volume"], marker_color=vol_colors, opacity=0.45,
                         name="Volume", yaxis="y2", showlegend=False))

    ov = result.get("overlay") or {}
    shapes, annos = [], []

    def X(i):
        if i is None:
            return None
        return x[int(max(0, min(n - 1, i)))]

    def line(x0, y0, x1, y1, color, width=2, dash=None):
        if None in (x0, x1) or y0 is None or y1 is None:
            return
        shapes.append(dict(type="line", xref="x", yref="y", x0=x0, y0=y0, x1=x1, y1=y1,
                           line=dict(color=color, width=width, dash=dash)))

    def rect(x0, y0, x1, y1, color, opacity=0.12, dash=None, width=1.4):
        if None in (x0, x1) or y0 is None or y1 is None:
            return
        shapes.append(dict(type="rect", xref="x", yref="y", x0=x0, y0=y0, x1=x1, y1=y1,
                           line=dict(color=color, width=width, dash=dash),
                           fillcolor=color, opacity=opacity, layer="below"))

    def curve(xs, ys, color, width=3):
        """A shape that has to follow data points - see the module note on category axes."""
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", yaxis="y", showlegend=False,
                                 hoverinfo="skip", line=dict(color=color, width=width)))

    b0 = _pos(idx, ov.get("base_start_date"))
    blow_pos = _pos(idx, ov.get("base_low_date"))
    base_top, base_low = ov.get("base_top"), ov.get("base_low")
    pname = result.get("pattern_name", "")
    shape_name = ov.get("base_shape") or result.get("base_shape")
    is_cup = pname in ("Cup", "Cup+Handle")

    # ── 3-weeks-tight squeeze boxes (drw_pattern.pine:438-440) ───────────────────────────
    # Drawn first so everything else sits on top of them.
    if show_tight:
        for tb in (ov.get("tight_closes") or []):
            s, e = _pos(idx, tb.get("start_date")), _pos(idx, tb.get("end_date"))
            if s is None or e is None:
                continue
            rect(X(s), tb.get("low"), X(e), tb.get("high"), TIGHT_C, 0.10, "dot", 1)

    # ── base high / low lines (drw_pattern.pine:984-985) ─────────────────────────────────
    # For a flat base or a consolidation this pair IS the pattern, so the channel between them
    # is shaded. A cup gets the lines only, so the arcs stay readable inside them.
    if b0 is not None and base_top and pname != "HTF":
        if not is_cup and base_low:
            rect(X(b0), base_low, X(n - 1), base_top, BASE_C, 0.07, None, 0)
        line(X(b0), base_top, X(n - 1), base_top, BASE_C, 3, "dot")
        if base_low:
            line(X(b0), base_low, X(n - 1), base_low, BASE_C, 2)
        depth = (base_top - base_low) / base_top * 100 if (base_top and base_low) else 0
        annos.append(dict(
            x=X(b0), y=base_top, xref="x", yref="y", showarrow=False, xanchor="left",
            yanchor="bottom", font=dict(color=BASE_C, size=11),
            text=f"<b>{shape_name or pname}</b> {result.get('days_in_base') or 0}d "
                 f"{depth:.1f}% deep · A{result.get('acc_days', 0)} "
                 f"D{result.get('dis_days', 0)} N{result.get('neu_days', 0)}"))

    # ── cup: the indicator's two exponential arcs (lines 1017-1048) ──────────────────────
    # Left arc spans startBaseBar -> lowerBaseBar decaying exp(-6i/lengthLeft); right arc spans
    # lowerBaseBar -> the last bar rising (exp(6j/lengthRight)-1)/exp(6).
    # Either half can legitimately be zero bars long: a base whose low is the newest bar has no
    # right side yet (11 of 1071 cups), one whose low is its first bar has no left side (4), and
    # Pine's loops divide by that length. Each half is therefore drawn only if it exists, rather
    # than the whole cup disappearing because one of them does not. `startUpPrice > bottomPrice`
    # is the Pine's own guard (line 1020) - below it the indicator draws no cup either.
    cup = ov.get("cup")
    if cup and b0 is not None and blow_pos is not None:
        bottom = cup.get("bottom")
        start_up, end_up = cup.get("start_up"), cup.get("end_up")
        ll, lr = cup.get("left_bars") or 0, cup.get("right_bars") or 0
        if bottom and start_up and start_up > bottom:
            pts = []
            if ll > 0:
                for i in range(ll + 1):
                    y = bottom + (start_up - bottom) * math.exp(-6.0 * i / ll)
                    pts.append((b0 + i, bottom if i == ll else y))
            if lr > 0 and end_up:
                if not pts:
                    pts.append((blow_pos, bottom))
                for j in range(1, lr + 1):
                    y = bottom + (end_up - bottom) * (math.exp(6.0 * j / lr) - 1) / math.exp(6.0)
                    pts.append((blow_pos + j, y))
            pts = [(p, py) for p, py in pts if 0 <= p < n]
            if len(pts) > 2:
                curve([X(p) for p, _ in pts], [py for _, py in pts], BASE_C)

    # ── handle: dotted peak line + box (lines 1140-1153) ─────────────────────────────────
    h = ov.get("handle")
    if h:
        hp = _pos(idx, h.get("peak_date"))
        if hp is not None and h.get("peak"):
            line(X(hp), h["peak"], X(n - 1), h["peak"], HANDLE_C, 2, "dot")
            if h.get("low"):
                rect(X(hp), h["low"], X(n - 1), h["peak"], HANDLE_BOX_C, 0.10, "dash")
            annos.append(dict(x=X(n - 1), y=h["peak"], xref="x", yref="y", showarrow=False,
                              xanchor="left", yanchor="bottom",
                              text=f" handle {h.get('bars', 0)}b {h.get('depth_pct') or 0:.1f}%",
                              font=dict(color=HANDLE_C, size=10)))

    # ── high tight flag: pole into the flag channel (lines 914-916) ──────────────────────
    htf = ov.get("htf")
    if htf:
        fs = _pos(idx, htf.get("flag_start_date"))
        pl = _pos(idx, htf.get("pole_low_date"))
        fh, fl = htf.get("flag_high"), htf.get("flag_low")
        if fs is not None and fh:
            if fl:
                rect(X(fs), fl, X(n - 1), fh, HTF_C, 0.08, None, 0)
            line(X(fs), fh, X(n - 1), fh, HTF_C, 2, "dot")
            line(X(fs), fl, X(n - 1), fl, HTF_C, 2, "dot")
            if pl is not None and htf.get("pole_low"):
                line(X(pl), htf["pole_low"], X(fs), fl, HTF_C, 2, "dot")
            ctx = result.get("htf_context") or {}
            annos.append(dict(x=X(fs), y=fh, xref="x", yref="y", showarrow=False,
                              xanchor="left", yanchor="bottom",
                              text=f" pole +{ctx.get('pole_gain_pct') or 0:.0f}% · "
                                   f"flag {htf.get('flag_bars', 0)}b "
                                   f"{ctx.get('flag_depth_pct') or 0:.1f}% deep",
                              font=dict(color=HTF_C, size=10)))

    # ── trade boxes (lines 1244-1246) ────────────────────────────────────────────────────
    bx = ov.get("boxes")
    if bx:
        s = _pos(idx, bx.get("start_date"))
        e = _pos(idx, bx.get("end_date"))
        if s is not None:
            e = n - 1 if e is None else e
            for key, col in (("entry", ENTRY_C), ("stop", STOP_C), ("target", TARGET_C)):
                lo, hi = (bx.get(key) or [None, None])[:2]
                rect(X(s), lo, X(e), hi, col, 0.16, width=0)

    # The buy point is deliberately NOT drawn. It is the base top, which the highLine above
    # already marks, and a full-width rule across the chart competed with the shape it was
    # supposed to describe. The number itself is still on the record and in the metrics row
    # under the chart.

    label = f"{pname} ({shape_name})" if (shape_name and shape_name != pname) else pname
    fig.update_layout(
        height=height, margin=dict(l=8, r=120, t=34, b=8),
        template="plotly_dark", paper_bgcolor="#131722", plot_bgcolor="#131722",
        xaxis=dict(rangeslider=dict(visible=False), showgrid=True, gridcolor="#1f2430",
                   type="category", nticks=12),
        yaxis=dict(domain=[0.26, 1.0], side="right", showgrid=True, gridcolor="#1f2430"),
        yaxis2=dict(domain=[0.0, 0.20], side="right", showgrid=False),
        shapes=shapes, annotations=annos, showlegend=False,
        title=dict(text=f"{ticker} — {label} · {result.get('status', '')}"
                        f"  ·  drw_pattern.pine",
                   font=dict(size=13), x=0.01, xanchor="left"),
        hovermode="x unified", bargap=0.15,
    )
    return fig
