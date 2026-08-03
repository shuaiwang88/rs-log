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
    # history bar indices refer to the scanner's own (possibly trimmed) frame, so shift them
    # into this window's coordinates.
    offset = (full_len - len(df)) + int(result.get('df_trim_offset') or 0)

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
    shapes, annos = [], []

    def bar_x(i):
        """Bar index -> x value, clamped into the visible window."""
        if i is None:
            return None
        i = int(max(0, min(len(x) - 1, i)))
        return x[i]

    s0, s1, btop, blow = _base_span(history, offset)
    if s0 is not None and btop and blow:
        # The base itself: a box from its first bar to its last, spanning bTop..bLow.
        shapes.append(dict(type='rect', xref='x', yref='y',
                           x0=bar_x(s0), x1=bar_x(s1), y0=blow, y1=btop,
                           line=dict(color=color, width=1.5),
                           fillcolor=color, opacity=0.10, layer='below'))
        depth = (btop - blow) / btop * 100 if btop else 0
        annos.append(dict(x=bar_x(s0), y=btop, xref='x', yref='y', showarrow=False,
                          text=f"<b>{pname}</b> {depth:.0f}% deep, {max(0, s1 - s0)} bars",
                          font=dict(color=color, size=11), xanchor='left', yanchor='bottom'))

    # ── Cup+Handle: the handle is a tighter box near the top of the cup ──────────────
    if pname == 'Cup+Handle':
        hb = [i for i, s in enumerate(history) if s.get('isCupH')]
        if hb:
            h0, h1 = hb[0] - offset, hb[-1] - offset
            seg = df.iloc[max(0, int(h0)):int(h1) + 1]
            if len(seg):
                shapes.append(dict(type='rect', xref='x', yref='y',
                                   x0=bar_x(h0), x1=bar_x(h1),
                                   y0=float(seg['Low'].min()), y1=float(seg['High'].max()),
                                   line=dict(color='#FF8C00', width=1.5, dash='dot'),
                                   fillcolor='#FF8C00', opacity=0.14, layer='below'))
                annos.append(dict(x=bar_x(h1), y=float(seg['High'].max()), xref='x', yref='y',
                                  showarrow=False, text='handle', xanchor='right',
                                  yanchor='bottom', font=dict(color='#FF8C00', size=10)))

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
            annos.append(dict(x=bar_x(fs - offset), y=ctx['flag_high'], xref='x', yref='y',
                              showarrow=False, xanchor='left', yanchor='bottom',
                              text=f"HTF pole +{ctx['pole_gain_pct']:.0f}% · "
                                   f"flag {ctx['flag_bars']}b {ctx['flag_depth_pct']:.0f}%",
                              font=dict(color='#FFD700', size=10)))

    # ── Every reading's buy point. They price off DIFFERENT levels, which is the whole
    #    reason the layered list exists, so drawing only the headline hides the disagreement.
    seen = set()
    for j, p in enumerate(result.get('patterns') or []):
        pv = p.get('pivot')
        if not pv or round(pv, 2) in seen:
            continue
        seen.add(round(pv, 2))
        pc = PATTERN_COLORS.get(p.get('name'), '#888')
        shapes.append(dict(type='line', xref='paper', yref='y', x0=0, x1=1, y0=pv, y1=pv,
                           line=dict(color=pc, width=1.2,
                                     dash='solid' if j == 0 else 'dash')))
        annos.append(dict(x=1, y=pv, xref='paper', yref='y', showarrow=False,
                          text=f" {p.get('name')} {pv:,.2f}", xanchor='left',
                          font=dict(color=pc, size=10)))

    # The reported buy point, drawn last so it sits on top of any reading line it coincides with.
    piv = result.get('pivot')
    if piv:
        shapes.append(dict(type='line', xref='paper', yref='y', x0=0, x1=1, y0=piv, y1=piv,
                           line=dict(color='#ffffff', width=1.6, dash='longdash')))
        annos.append(dict(x=0, y=piv, xref='paper', yref='y', showarrow=False,
                          text=f"<b>buy point {piv:,.2f}</b>  ({result.get('dist_pct')}%) ",
                          xanchor='left', yanchor='bottom',
                          font=dict(color='#ffffff', size=11)))

    fig.update_layout(
        height=height, margin=dict(l=8, r=110, t=34, b=8),
        template='plotly_dark', paper_bgcolor='#131722', plot_bgcolor='#131722',
        xaxis=dict(rangeslider=dict(visible=False), showgrid=True, gridcolor='#1f2430',
                   type='category', nticks=12),
        yaxis=dict(domain=[0.26, 1.0], side='right', showgrid=True, gridcolor='#1f2430',
                   title=None),
        yaxis2=dict(domain=[0.0, 0.20], side='right', showgrid=False, title=None),
        shapes=shapes, annotations=annos, showlegend=False,
        title=dict(text=f"{ticker} — {pname} · {result.get('status','')}",
                   font=dict(size=13), x=0.01, xanchor='left'),
        hovermode='x unified', bargap=0.15,
    )
    return fig
