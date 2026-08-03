"""Paint detected IBD patterns on a candlestick chart, following pine/drw_pattern.pine.

The Pine indicator is the reference for WHAT each pattern looks like on a chart, so the shapes
here mirror what it draws: the base box between bTop and bLow, the handle as a tighter box in
the upper half of a cup, the double bottom as its two lows and middle peak, and the High Tight
Flag as a pole line into a flag box.

Why Plotly rather than an actual TradingView chart: the TradingView embed widget renders
THEIR data and cannot draw our geometry, so it can show XOM but not the 325-bar base the
scanner found inside it. Painting the patterns is the whole point, so the chart has to be one
we control. Plotly is already used throughout app.py, so this adds no dependency.

Geometry comes from the scanner's per-bar `history`. That is no longer written to
ibd_pattern_results.json (it was 99.8% of an 8.9 GB file), so the caller rescans the single
selected ticker - about 15 ms - rather than the results carrying it for all 6,000.
"""
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go

# Same palette as the Pine indicator so a chart here matches the one on TradingView.
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
UP, DOWN = '#26a69a', '#ef5350'          # TradingView's default candle colours


def _base_span(history: List[Dict[str, Any]], offset: int):
    """First and last bar of the CURRENT base, as indices into the plotted frame.

    Walks back from the end while inBase holds, rather than taking the first inBase bar in
    history: a ticker often has several bases in its history and the chart is about the one
    live now. Post-breakout the run has already ended, so fall back to the last one seen.
    """
    if not history:
        return None, None, None, None
    run = []
    for s in reversed(history):
        if s.get('inBase'):
            run.append(s)
        elif run:
            break
    if not run:
        # Post-BO: use the most recent completed in-base stretch.
        idxs = [i for i, s in enumerate(history) if s.get('inBase')]
        if not idxs:
            return None, None, None, None
        end = idxs[-1]
        start = end
        while start > 0 and history[start - 1].get('inBase'):
            start -= 1
        run = history[start:end + 1]
    run.reverse()
    b0 = run[0].get('bar')
    b1 = run[-1].get('bar')
    top = next((s.get('bTop') for s in reversed(run) if s.get('bTop')), None)
    low = next((s.get('bLow') for s in reversed(run) if s.get('bLow')), None)
    if b0 is None or b1 is None:
        return None, None, None, None
    return b0 - offset, b1 - offset, top, low


def build_pattern_figure(ticker: str, df: pd.DataFrame, result: Dict[str, Any],
                         bars: int = 300, height: int = 620) -> go.Figure:
    """Candlestick + volume with the detected pattern painted on top."""
    df = df.sort_index()
    full_len = len(df)
    if full_len > bars:
        df = df.iloc[-bars:]
    # Map history bar indices into this window's coordinates.
    #   history bar i  ->  parquet row  i + df_trim_offset      (scanner keeps the last 1500)
    #   parquet row r  ->  plotted col  r - (full_len - len(df))
    # so the shift SUBTRACTS df_trim_offset. Adding it put every index far negative - XOM's
    # base landed at -29,000, got clamped away, and the cup curve silently never drew.
    offset = (full_len - len(df)) - int(result.get('df_trim_offset') or 0)

    x = list(df.index)
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x, open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'],
        increasing_line_color=UP, decreasing_line_color=DOWN,
        increasing_fillcolor=UP, decreasing_fillcolor=DOWN,
        name=ticker, yaxis='y'))

    vol_colors = [UP if c >= o else DOWN for c, o in zip(df['Close'], df['Open'])]
    fig.add_trace(go.Bar(x=x, y=df['Volume'], marker_color=vol_colors, opacity=0.45,
                         name='Volume', yaxis='y2', showlegend=False))

    history = result.get('history') or []
    pname = result.get('pattern_name') or 'None'
    color = PATTERN_COLORS.get(pname, '#92C183')
    # `facts` collects every metric into ONE block instead of scattering text across the
    # plot. With HTF + Double Bottom both live, DELL was drawing the base label, the
    # since-the-high label, "middle peak", the pole/flag metrics, the buy point and three
    # pivot labels - most of them near the same corner, overlapping each other and the
    # candles. Positional text is kept only where the position IS the information.
    shapes, annos, facts = [], [], []

    def bar_x(i):
        """Bar index -> x value, clamped into the visible window."""
        if i is None:
            return None
        i = int(max(0, min(len(x) - 1, i)))
        return x[i]

    s0, s1, btop, blow = _base_span(history, offset)

    # The base the scanner CARRIES is often not the structure on the chart. `bTop` ratchets
    # up 5% at a time while `bLow` never re-anchors, so a stock that ADVANCED gets recorded as
    # consolidating and keeps a low from months earlier. XOM: bTop climbed 118.36 -> 176.41
    # across 28 steps between 2025-10-03 and 2026-03-30 while bLow stayed pinned at 110.39
    # from November, giving a "37.4% deep, 212 bar" base where the real consolidation is
    # 176.41/134.95, 23.5% deep, since the 2026-03-30 high.
    #
    # Re-anchoring bLow inside the scanner produces exactly that - and costs 34 primary-exact
    # events on the benchmark, which scores labels and pivots and never looks at geometry. So
    # the detection keeps its frame and the CHART draws the honest one: the range since bTop
    # was last set, with the full base span still stated in the label so nothing is hidden.
    live_top = next((s.get('bTopBar') for s in reversed(history) if s.get('bTopBar') is not None), None)
    t0 = None
    if live_top is not None and s0 is not None:
        t0 = int(max(0, min(len(x) - 1, live_top - offset)))
        if t0 <= s0 or t0 >= (s1 if s1 is not None else 0):
            t0 = None                    # bTop never ratcheted, or lands outside the window
    # The base can start before the visible window (a 320-bar base on a 120-bar chart) or,
    # after trimming, land entirely outside it. Clamp into range and drop it if nothing of it
    # is on screen, rather than slicing an empty frame.
    if s0 is not None and s1 is not None:
        s0 = int(max(0, min(len(x) - 1, s0)))
        s1 = int(max(0, min(len(x) - 1, s1)))
        if s1 <= s0:
            s0 = s1 = None
    if s0 is not None and btop and blow:
        # The base itself: a box from its first bar to its last, spanning bTop..bLow.
        shapes.append(dict(type='rect', xref='x', yref='y',
                           x0=bar_x(s0), x1=bar_x(s1), y0=blow, y1=btop,
                           line=dict(color=color, width=1.5),
                           fillcolor=color, opacity=0.10, layer='below'))
        depth = (btop - blow) / btop * 100 if btop else 0
        facts.append(f"<b>{pname}</b>  {depth:.0f}% deep · {max(0, s1 - s0)} bars")
        # The actual range since the high, drawn solid over the carried base.
        if t0 is not None:
            seg = df.iloc[t0:s1 + 1]
            if len(seg) > 2:
                lo_since = float(seg['Low'].min())
                d_since = (btop - lo_since) / btop * 100 if btop else 0
                shapes.append(dict(type='rect', xref='x', yref='y',
                                   x0=bar_x(t0), x1=bar_x(s1), y0=lo_since, y1=btop,
                                   line=dict(color='#e0e0e0', width=1.6),
                                   fillcolor='#e0e0e0', opacity=0.07, layer='below'))
                facts.append(f"since the high  {d_since:.0f}% deep · {s1 - t0} bars")

    # ── Cup: draw the actual U, not just its bounding box ────────────────────────────
    # A rectangle says "a base lives here"; the point of calling something a cup is the
    # rounded descent and recovery, so trace it. Sampled as a parabola through the three
    # points the scanner already commits to - left top, the low, right top - which is enough
    # to show at a glance whether the shape earns the name. XOM's "cup" is 320 bars and 44.6%
    # deep, and that reads very differently as a curve than as a box.
    if pname in ('Cup', 'Cup+Handle') and s0 is not None and btop and blow:
        lo_rel = int(np.argmin(df['Low'].to_numpy()[max(0, s0):s1 + 1])) + max(0, s0)
        span_l = max(lo_rel - s0, 1)
        span_r = max(s1 - lo_rel, 1)
        pts = []
        for k in range(max(0, s0), lo_rel + 1):            # left side down
            t = (lo_rel - k) / span_l
            pts.append((bar_x(k), blow + (btop - blow) * (t ** 2)))
        for k in range(lo_rel + 1, s1 + 1):                # right side up
            t = (k - lo_rel) / span_r
            pts.append((bar_x(k), blow + (btop - blow) * (t ** 2)))
        if len(pts) > 2:
            path = 'M ' + ' L '.join(f'{px},{py}' for px, py in pts)
            shapes.append(dict(type='path', xref='x', yref='y', path=path,
                               line=dict(color=color, width=2)))

    # ── Double Bottom: the W - two lows and the middle peak between them ─────────────
    if pname == 'Dbl Bottom' and s0 is not None and btop and blow:
        seg = df.iloc[max(0, s0):s1 + 1]
        if len(seg) > 6:
            lows_a = seg['Low'].to_numpy()
            i1 = int(np.argmin(lows_a[:len(lows_a) // 2]))
            i2 = len(lows_a) // 2 + int(np.argmin(lows_a[len(lows_a) // 2:]))
            mid = i1 + int(np.argmax(seg['High'].to_numpy()[i1:i2 + 1])) if i2 > i1 else i1
            w = [(max(0, s0), btop), (max(0, s0) + i1, float(lows_a[i1])),
                 (max(0, s0) + mid, float(seg['High'].iloc[mid])),
                 (max(0, s0) + i2, float(lows_a[i2])), (s1, btop)]
            path = 'M ' + ' L '.join(f'{bar_x(k)},{v}' for k, v in w)
            shapes.append(dict(type='path', xref='x', yref='y', path=path,
                               line=dict(color=color, width=2)))
            annos.append(dict(x=bar_x(max(0, s0) + mid), y=float(seg['High'].iloc[mid]),
                              xref='x', yref='y', showarrow=False, text='middle peak',
                              yanchor='bottom', font=dict(color=color, size=10)))

    # ── Handle: drawn from the scanner's OWN recorded window ────────────────────────
    # Not from the run of bars where isCupH is true. That flag is per-bar over a trailing
    # window, so the run is long and describes nothing - XOM's spans 94 bars while the real
    # handle is 16 bars and 6.3% deep. hStart/hEnd carry the actual bounds.
    hstate = next((s for s in reversed(history) if s.get('hStart') is not None), None)
    if pname == 'Cup+Handle' and hstate:
        h0, h1 = hstate['hStart'] - offset, hstate['hEnd'] - offset
        shapes.append(dict(type='rect', xref='x', yref='y',
                           x0=bar_x(h0), x1=bar_x(h1),
                           y0=hstate['hLow'], y1=hstate['hHigh'],
                           line=dict(color='#FF8C00', width=1.6),
                           fillcolor='#FF8C00', opacity=0.18, layer='below'))
        facts.append(f"<span style='color:#FF8C00'>handle</span>  "
                     f"{hstate['hEnd'] - hstate['hStart'] + 1} bars · {hstate['hDepPct']:.1f}% deep")

    # ── High Tight Flag: pole line into the flag box ─────────────────────────────────
    ctx = result.get('htf_context') or {}
    if ctx:
        fs = ctx.get('flag_start_idx')
        pl = ctx.get('pole_low_idx')
        if fs is not None and pl is not None:
            shapes.append(dict(type='line', xref='x', yref='y',
                               x0=bar_x(pl - offset), y0=ctx['pole_low'],
                               x1=bar_x(fs - offset), y1=ctx['flag_high'],
                               line=dict(color='#FFD700', width=2, dash='dot')))
            shapes.append(dict(type='rect', xref='x', yref='y',
                               x0=bar_x(fs - offset), x1=x[-1],
                               y0=ctx['flag_low'], y1=ctx['flag_high'],
                               line=dict(color='#FFD700', width=1.5),
                               fillcolor='#FFD700', opacity=0.12, layer='below'))
            facts.append(f"<span style='color:#FFD700'>HTF</span>  pole +{ctx['pole_gain_pct']:.0f}%"
                         f" · flag {ctx['flag_bars']}b {ctx['flag_depth_pct']:.0f}% deep")

    # ── Every reading's buy point. They price off DIFFERENT levels, which is the whole
    #    reason the layered list exists, so drawing only the headline hides the disagreement.
    seen = set()
    placed = []
    for j, p in enumerate(result.get('patterns') or []):
        pv = p.get('pivot')
        if not pv or round(pv, 2) in seen:
            continue
        seen.add(round(pv, 2))
        pc = PATTERN_COLORS.get(p.get('name'), '#888')
        shapes.append(dict(type='line', xref='paper', yref='y', x0=0, x1=1, y0=pv, y1=pv,
                           line=dict(color=pc, width=1.2,
                                     dash='solid' if j == 0 else 'dash')))
        placed.append(pv)
        # Nudge a label that would sit on top of one already placed. Readings often price
        # within a percent of each other, which stacked their labels into an unreadable blur.
        shift = sum(1 for q in placed[:-1] if abs(q - pv) / max(pv, 1e-9) < 0.012)
        annos.append(dict(x=1, y=pv, xref='paper', yref='y', showarrow=False,
                          text=f" {p.get('name')} {pv:,.2f}", xanchor='left',
                          yshift=shift * -12,
                          font=dict(color=pc, size=10)))

    # The reported buy point, drawn last so it sits on top of any reading line it coincides with.
    piv = result.get('pivot')
    if piv:
        shapes.append(dict(type='line', xref='paper', yref='y', x0=0, x1=1, y0=piv, y1=piv,
                           line=dict(color='#ffffff', width=1.6, dash='longdash')))
        facts.insert(0, f"<b>buy point {piv:,.2f}</b>  ({result.get('dist_pct'):+.1f}% away)")

    if facts:
        annos.append(dict(x=0.005, y=0.985, xref='paper', yref='paper', showarrow=False,
                          text='<br>'.join(facts), align='left',
                          xanchor='left', yanchor='top',
                          font=dict(size=11, color='#d8dde6'),
                          bgcolor='rgba(19,23,34,0.82)', bordercolor='#2a3040',
                          borderwidth=1, borderpad=6))

    # Date axis. A category axis gives every bar its own tick slot, so 300 bars produced a
    # solid smear of dates. Keep category (it suppresses weekend gaps, which a date axis
    # would leave as holes) but place ticks only at month boundaries.
    ticks_v, ticks_t = [], []
    last_m = None
    for k, ts in enumerate(x):
        m = (ts.year, ts.month)
        if m != last_m:
            ticks_v.append(ts)
            ticks_t.append(ts.strftime('%b %Y') if m[1] == 1 else ts.strftime('%b'))
            last_m = m
    step = max(1, -(-len(ticks_v) // 11))      # ceiling: at most 11 labels, any window
    ticks_v, ticks_t = ticks_v[::step], ticks_t[::step]

    fig.update_layout(
        height=height, margin=dict(l=8, r=110, t=34, b=8),
        template='plotly_dark', paper_bgcolor='#131722', plot_bgcolor='#131722',
        xaxis=dict(rangeslider=dict(visible=False), showgrid=True, gridcolor='#1f2430',
                   type='category', tickmode='array', tickvals=ticks_v, ticktext=ticks_t,
                   tickangle=0, tickfont=dict(size=10), ticks='outside', ticklen=4),
        yaxis=dict(domain=[0.26, 1.0], side='right', showgrid=True, gridcolor='#1f2430',
                   title=None),
        yaxis2=dict(domain=[0.0, 0.20], side='right', showgrid=False, title=None),
        shapes=shapes, annotations=annos, showlegend=False,
        title=dict(text=f"{ticker} — {pname} · {result.get('status','')}",
                   font=dict(size=13), x=0.01, xanchor='left'),
        hovermode='x unified', bargap=0.15,
    )
    return fig
