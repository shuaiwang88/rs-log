"""Paint detected IBD patterns on a candlestick chart, following pine/drw_pattern.pine.

The Pine indicator is the reference for WHAT each pattern looks like on a chart, and it is
almost entirely a LINE vocabulary, not a box one. Filled rectangles read as "a pattern lives
somewhere in here"; Pine's lines say where the structure actually runs. Each shape below is
the Pine primitive it is named after:

  channel     drw_pattern.pine:984  dotted width-3 line at the base high + solid width-2 line
              at the base low, both running from the base's first bar to now. This is what a
              Consolidation, a Flat Base, and any unnamed base look like - the whole shape.
  cup         :1024-1047  two exponential curves, decay from the left rim down to the low and
              a mirror rise out to the right rim. Not a parabola and not a box.
  dbl bottom  :1060-1064  five segments - a dashed line at the middle peak plus the W itself
              through fH -> fL -> sH -> sL, drawn from the corners the DETECTOR matched.
  handle      :1140-1146  orange dotted line at the handle's peak, blue dashed box beneath.
  flag (HTF)  :914-916  three dotted lines - flag high, flag low, and the pole running up
              into the flag's low. Deliberately not a box: the pole is a line in Pine, and a
              box hides that the flag is a tight range rather than a filled zone.
  trade zone  :1244-1246  entry / stop / target bands off the active pivot.

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

# Pine's literal colours, so a shape here is the same colour as on TradingView.
PINE_BASE = '#92C183'      # color.rgb(146, 193, 131) - channel and cup
PINE_GREEN = '#4CAF50'     # color.green  - double bottom
PINE_ORANGE = '#FF9800'    # color.orange - handle separation line
PINE_BLUE = '#2196F3'      # color.blue   - handle box
ZONE_ENTRY, ZONE_SL, ZONE_TP = '#007AFF', '#FF3B30', '#34C759'


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
    df_full = df.sort_index()
    df = df_full
    full_len = len(df)
    if full_len > bars:
        df = df.iloc[-bars:]
    win0 = full_len - len(df)                # window column k  ->  df_full row  k + win0
    # Map history bar indices into this window's coordinates.
    #   history bar i  ->  parquet row  i + df_trim_offset      (scanner keeps the last 1500)
    #   parquet row r  ->  plotted col  r - (full_len - len(df))
    # so the shift SUBTRACTS df_trim_offset. Adding it put every index far negative - XOM's
    # base landed at -29,000, got clamped away, and the cup curve silently never drew.
    offset = (full_len - len(df)) - int(result.get('df_trim_offset') or 0)

    # Category axis, so the x values ARE the category labels: keep them as plain strings.
    # Passing Timestamps works in the browser only because Plotly's own encoder converts
    # them; anything else serialising the figure (a static export, say) chokes on them, and
    # a shape whose x doesn't match a category string silently fails to place.
    xd = list(df.index)
    x = [ts.strftime('%Y-%m-%d') for ts in xd]
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

    def seg(i0, y0, i1, y1, col, width=2, dash=None):
        """One Pine `line.new`: bar index -> bar index, price -> price."""
        shapes.append(dict(type='line', xref='x', yref='y',
                           x0=bar_x(i0), y0=y0, x1=bar_x(i1), y1=y1,
                           line=dict(color=col, width=width, dash=dash or 'solid')))

    s0, s1, btop, blow = _base_span(history, offset)
    # The base as it really is, independent of how far back the chart is zoomed. s0/s1 get
    # clamped to the window below, and measuring the cup off the clamped span made the SAME
    # base read as a different shape at different zooms - AAPL drew a cup at 120 bars and
    # refused one at 300, because clamping s0 to 0 moved the low 60 bars deeper into it.
    f0 = f1 = None
    if s0 is not None and s1 is not None:
        f0 = int(max(0, min(full_len - 1, s0 + win0)))
        f1 = int(max(0, min(full_len - 1, s1 + win0)))

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
    tf = None                            # bar bTop was last set, as a df_full row
    if live_top is not None and f0 is not None:
        tf = live_top - offset + win0
        # Under ~10 bars there is no "range since the high" to speak of - AAPL reported
        # "13% deep, 2 bars", which is a two-day pullback dressed up as a structure. Measured
        # against the true base bounds so the verdict does not change with the zoom.
        if not (f0 < tf < f1) or (f1 - tf) < 10:
            tf = None
    # The base can start before the visible window (a 320-bar base on a 120-bar chart) or,
    # after trimming, land entirely outside it. Clamp into range and drop it if nothing of it
    # is on screen, rather than slicing an empty frame.
    if s0 is not None and s1 is not None:
        s0 = int(max(0, min(len(x) - 1, s0)))
        s1 = int(max(0, min(len(x) - 1, s1)))
        if s1 <= s0:
            s0 = s1 = None
    # Double Bottom is a SUB-PATTERN as of 2026-08-04, not a label of its own, so this can no
    # longer key off `pname` - that never says 'Dbl Bottom' any more and the W simply stopped
    # being drawn. It keys off the recorded W instead, and the host pattern keeps its channel:
    # the Pine deleted highLine/lowLine before redrawing them (drw_pattern.pine:1060-1064)
    # because a DB was the whole pattern there, whereas here it sits inside a Cup / Flat Base /
    # Consolidation that still needs its own shape drawn.
    # Gated on the RESULT's flag, not on a free search of history. Searching all of history
    # finds any W the base ever contained and drew one on every chart tested, including bases
    # the scanner does not report as double bottoms - the same window mismatch the scanner
    # itself had between `latest['isDB']` and the layered reading. `is_double_bottom` is
    # computed over the last PATTERN_WINDOW_BARS bars, so defer to it and the chart, the
    # attribute and the `also_reads_as` entry all agree by construction.
    dbs = (next((s for s in reversed(history) if s.get('dbPts') and s.get('isDB')), None)
           if result.get('is_double_bottom') else None)

    if s0 is not None and btop and blow:
        depth = (btop - blow) / btop * 100 if btop else 0
        # Length from the true span, not the on-screen one: zooming out must not lengthen a
        # base. MUSA's read "248 bars" at 250 bars of chart and 118 at 120 - the same base.
        cut = ' ◀' if (f0 - win0) < 0 else ''
        facts.append(f"<b>{pname}</b>  {depth:.0f}% deep · {f1 - f0} bars{cut}")
        # ── The channel. Pine's highLine + lowLine, and for a Consolidation or a Flat
        #    Base this IS the pattern - there is nothing else to draw. A filled box in
        #    its place said only "something is here"; the two lines say the price has
        #    been capped at this high and held above this low for the whole span.
        #    Drawn unconditionally now: a double bottom no longer suppresses it, because it
        #    is an annotation on this base rather than a replacement for it.
        seg(s0, btop, s1, btop, PINE_BASE, width=3, dash='dot')
        seg(s0, blow, s1, blow, PINE_BASE, width=2)
        # The actual range since the high. bTop ratchets up 5% at a time while bLow never
        # re-anchors, so the carried base can be far deeper and longer than the structure on
        # screen - XOM: 37.4% over 212 bars carried, 23.5% over 86 bars real. Drawn as a
        # dashed low line under the same bTop rather than a second box, so the two readings
        # share a ceiling and the eye compares floors.
        if tf is not None:
            lo_since = float(df_full['Low'].iloc[tf:f1 + 1].min())
            d_since = (btop - lo_since) / btop * 100 if btop else 0
            if lo_since > blow * 1.005:
                seg(tf - win0, lo_since, f1 - win0, lo_since, '#8b93a3', width=1.4, dash='dash')
                facts.append(f"since the high  {d_since:.0f}% deep · {f1 - tf} bars")

    # ── Cup: Pine's two exponential curves ───────────────────────────────────────────
    # drw_pattern.pine:1024-1047. The left side decays  bottom + (rim - bottom)*e^(-6t)  from
    # the left rim down to the low; the right side rises  bottom + (rim - bottom)*(e^(6t)-1)/e^6
    # back out. The 6 is Pine's own constant and sets how square the U reads - a parabola
    # (what this drew before) bottoms out far too gently and made every cup look like a bowl.
    # The rims sit at the LOW of the base's first and last bars, x0.99, so the curve nests
    # just inside the channel exactly as it does on TradingView.
    if pname in ('Cup', 'Cup+Handle') and s0 is not None and f1 > f0 and btop and blow:
        lo_f = int(np.argmin(df_full['Low'].to_numpy()[f0:f1 + 1])) + f0
        n_l, n_r = lo_f - f0, f1 - lo_f
        # A cup needs two sides. Where the low sits at the base's first bar the left "side"
        # is a one-bar drop and the exponential draws a wall - AAPL's 236-bar base bottoms on
        # bar 1 and painted a 120-point vertical line, then a curve sweeping across eight
        # months of price it has nothing to do with. Pine shares the construction and the
        # blind spot, but it does delete a Double Bottom on a 2:1 asymmetry (:1101), so
        # refusing a lopsided cup is in its spirit.
        #
        # This refuses 54% of the 1,958 Cup / Cup+Handle signals in the last scan, and the
        # refusals are not borderline - the asymmetric ones sit at a median 11:1 split. That
        # is the bTop ratchet showing up again, not a strict threshold. Say which, rather
        # than drawing a shape the data does not support.
        why = None
        if min(n_l, n_r) < 3:
            why = f"the base low is bar {n_l} of {n_l + n_r}"
        elif max(n_l, n_r) > 5 * min(n_l, n_r):
            why = f"sides are {n_l} / {n_r} bars"
        if why:
            facts.append(f"<i>no cup curve — {why}</i>")
        else:
            bottom = blow * 0.99
            rim_l = float(df_full['Low'].iloc[f0]) * 0.99
            rim_r = float(df_full['Low'].iloc[max(f1 - 1, 0)]) * 0.99
            # Pine guards on `startUpPrice > bottomPrice`; with no rim above the low there is
            # no curve. Fall back to the channel top so a cup label still shows a cup.
            if rim_l <= bottom * 1.01:
                rim_l = btop * 0.99
            if rim_r <= bottom * 1.01:
                rim_r = btop * 0.99
            # Clip to the window, do not clamp. bar_x() pins an out-of-range index to column
            # 0, which is right for a horizontal line (it just starts at the edge) and wrong
            # for a curve: MUSA's base begins before a 250-bar window, so every off-screen
            # point of the left side stacked onto column 0 and drew a 140-point vertical wall
            # that no candle there supports.
            pts = []
            for k in range(n_l + 1):                                 # left rim -> low
                t = k / n_l
                y = bottom if k == n_l else bottom + (rim_l - bottom) * np.exp(-6.0 * t)
                pts.append((f0 + k - win0, y))
            for k in range(1, n_r + 1):                              # low -> right rim
                t = k / n_r
                pts.append((lo_f + k - win0,
                            bottom + (rim_r - bottom) * (np.exp(6.0 * t) - 1.0) / np.exp(6.0)))
            pts = [(bar_x(i), y) for i, y in pts if 0 <= i < len(x)]
            if len(pts) > 2:
                path = 'M ' + ' L '.join(f'{px},{py}' for px, py in pts)
                shapes.append(dict(type='path', xref='x', yref='y', path=path,
                                   line=dict(color=PINE_BASE, width=3)))

    # ── Double Bottom: Pine's five segments through the corners the detector matched ──
    # drw_pattern.pine:1060-1064. Not re-derived from the bar data: argmin over each half of
    # the base finds A W, but not necessarily the one that passed the eleven conditions, and
    # a chart that draws a different W than the detector matched is worse than no chart.
    if dbs is not None:
        fHt, fH, fLt, fL, sHt, sH, sLt, sL = dbs['dbPts']
        fHt, fLt, sHt, sLt = (v - offset for v in (fHt, fLt, sHt, sLt))
        # The middle-peak line that used to run from sH to the right edge is GONE. It read as
        # the buy point, and the middle peak is no longer quoted as one - it came in a median
        # 8.4% below IBD's pivot where it led. Only the W itself is drawn now; the buy point
        # on this chart is the host pattern's, which the channel above already shows.
        seg(fHt, fH, fLt, fL, PINE_GREEN)                            # dbLine1
        seg(fLt, fL, sHt, sH, PINE_GREEN)                            # dbLine2
        seg(sHt, sH, sLt, sL, PINE_GREEN)                            # dbLine3
        facts.append(f"<span style='color:{PINE_GREEN}'>W</span>  double bottom · lows "
                     f"{fL:,.2f} / {sL:,.2f} · middle peak {sH:,.2f} (not the buy point)")

    # ── Handle: drawn from the scanner's OWN recorded window ────────────────────────
    # Not from the run of bars where isCupH is true. That flag is per-bar over a trailing
    # window, so the run is long and describes nothing - XOM's spans 94 bars while the real
    # handle is 16 bars and 6.3% deep. hStart/hEnd carry the actual bounds.
    hstate = next((s for s in reversed(history) if s.get('hStart') is not None), None)
    if pname == 'Cup+Handle' and hstate:
        h0, h1 = hstate['hStart'] - offset, hstate['hEnd'] - offset
        # handleSepLine: the peak the handle drifts down from, extended to the right edge -
        # that level is the buy point, which is why Pine draws it as a line and not just as
        # the top of the box (drw_pattern.pine:1140).
        seg(h0, hstate['hHigh'], len(x) - 1, hstate['hHigh'], PINE_ORANGE, width=2, dash='dot')
        shapes.append(dict(type='rect', xref='x', yref='y',
                           x0=bar_x(h0), x1=bar_x(h1),
                           y0=hstate['hLow'], y1=hstate['hHigh'],
                           line=dict(color=PINE_BLUE, width=1, dash='dash'),
                           fillcolor=PINE_BLUE, opacity=0.10, layer='below'))
        facts.append(f"<span style='color:{PINE_ORANGE}'>handle</span>  "
                     f"{hstate['hEnd'] - hstate['hStart'] + 1} bars · {hstate['hDepPct']:.1f}% deep")

    # ── High Tight Flag: Pine's three dotted lines, not a box ───────────────────────
    # drw_pattern.pine:914-916 - flagHLine and flagLLine cap and floor the flag, flagPLine
    # runs from the pole's low up into the flag's LOW (not its high: the pole ends where the
    # consolidation begins). A filled box lost the pole entirely and made a 15% flag look
    # like a zone rather than the tight range that earns the name.
    ctx = result.get('htf_context') or {}
    if ctx:
        fs = ctx.get('flag_start_idx')
        pl = ctx.get('pole_low_idx')
        if fs is not None and pl is not None:
            fs_x, pl_x, last = fs - offset, pl - offset, len(x) - 1
            seg(pl_x, ctx['pole_low'], fs_x, ctx['flag_low'], '#FFD700', width=2, dash='dot')
            seg(fs_x, ctx['flag_high'], last, ctx['flag_high'], '#FFD700', width=2, dash='dot')
            seg(fs_x, ctx['flag_low'], last, ctx['flag_low'], '#FFD700', width=2, dash='dot')
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
        shift = sum(1 for q in placed[:-1] if abs(q - pv) / max(pv, 1e-9) < 0.02)
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
        # Trade zones (drw_pattern.pine:1244-1246): the 5% buy range above the pivot, the
        # 8% stop below it, and the 20-25% target. Pine anchors them at the right edge and
        # extends them while price stays in range, so they stay a right-edge ribbon here
        # rather than washing over the pattern itself.
        z0 = bar_x(int(len(x) * 0.88))
        for lo, hi, col in ((piv, piv * 1.05, ZONE_ENTRY),
                            (piv * 0.92, piv * 0.95, ZONE_SL),
                            (piv * 1.20, piv * 1.25, ZONE_TP)):
            shapes.append(dict(type='rect', xref='x', yref='y', x0=z0, x1=x[-1],
                               y0=lo, y1=hi, line=dict(width=0),
                               fillcolor=col, opacity=0.16, layer='below'))

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
    for k, ts in enumerate(xd):
        m = (ts.year, ts.month)
        if m != last_m:
            ticks_v.append(x[k])
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
