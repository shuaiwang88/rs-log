"""
pattern_painter.py

MarketSurge-style green pattern overlay generator.

Visual language (matching the screenshot exactly):
  ┌─────────────────────────────────────────────────────────────┐
  │  Cup / Cup-with-Handle                                      │
  │    • Thick smooth green arc from left high → bottom → right │
  │    • Dashed green horizontal line at left-side high (pivot) │
  │    • Handle: smaller descending arc after the right lip     │
  │    • Price labels at left high, bottom, right high          │
  │                                                             │
  │  Double Bottom                                              │
  │    • Two smooth green arcs forming a W                      │
  │    • Dashed green line at the mid-peak (buy point)          │
  │    • Price labels at each key point                         │
  │                                                             │
  │  Flat Base                                                  │
  │    • Solid green line at the base low                       │
  │    • Dashed green line at the base high (pivot)             │
  │    • Price labels at both levels                            │
  └─────────────────────────────────────────────────────────────┘

Progressive painting:
  - Every bar inside a forming base extends the dashed top line rightward
  - The arc is drawn as soon as there's a confirmed bottom;
    the right side of the arc grows bar-by-bar as price recovers
  - Labels are shown from pattern start; prices update live

Public API
----------
    painter = PatternPainter(df_ohlcv, recognizer)
    traces  = painter.get_plotly_traces()   # list of go.* objects
    lw_data = painter.get_lightweight_data() # dict for JS injection
"""

from __future__ import annotations

import math
import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple

try:
    import plotly.graph_objects as go
except ImportError:
    go = None  # Plotly optional (Lightweight Charts path still works)

# ---------------------------------------------------------------------------
# Colour palette (matching MarketSurge green)
# ---------------------------------------------------------------------------
GREEN_SOLID  = "rgba(60, 160, 80, 1.0)"    # thick arc / solid base line
GREEN_DASH   = "rgba(60, 160, 80, 0.85)"   # dashed pivot line
GREEN_FILL   = "rgba(60, 160, 80, 0.06)"   # light shaded region
GREEN_LABEL  = "rgba(60, 160, 80, 1.0)"    # annotation text
ARC_WIDTH    = 2.5                          # px – matches MarketSurge thickness
DASH_WIDTH   = 1.5
LABEL_SIZE   = 11


# ---------------------------------------------------------------------------
# Smooth arc helpers
# ---------------------------------------------------------------------------

def _cup_arc_points(
    x_left: float, y_left: float,
    x_bottom: float, y_bottom: float,
    x_right: float, y_right: float,
    n_pts: int = 120,
) -> Tuple[List[float], List[float]]:
    """
    Generate a smooth U-shaped arc using the exact MarketSurge exponential formula.
    Left side:  exponential decay   (high → bottom)
    Right side: exponential growth  (bottom → high)
    Returns (x_pts, y_pts) as floats (timestamps or bar indices).
    """
    xs, ys = [], []

    # --- left arm ---
    span_l = x_bottom - x_left
    if span_l > 0:
        for i in range(n_pts // 2 + 1):
            t = i / (n_pts // 2)
            x = x_left + span_l * t
            y = y_bottom + (y_left - y_bottom) * math.exp(-6 * t)
            xs.append(x)
            ys.append(y)

    # --- right arm ---
    span_r = x_right - x_bottom
    if span_r > 0:
        exp6 = math.exp(6) - 1
        for i in range(1, n_pts // 2 + 1):
            t = i / (n_pts // 2)
            x = x_bottom + span_r * t
            y = y_bottom + (y_right - y_bottom) * (math.exp(6 * t) - 1) / exp6
            xs.append(x)
            ys.append(y)

    return xs, ys


def _handle_arc_points(
    x_start: float, y_start: float,
    x_low: float, y_low: float,
    x_end: float, y_end: float,
    n_pts: int = 40,
) -> Tuple[List[float], List[float]]:
    """Small concave arc for the handle shakeout."""
    xs, ys = [], []
    # Simple quadratic bezier: start → low → end
    for i in range(n_pts + 1):
        t = i / n_pts
        # quadratic Bézier
        x = (1-t)**2 * x_start + 2*(1-t)*t * x_low + t**2 * x_end
        # control point y is the handle low (pulled down)
        ctrl_y = y_low - (y_start - y_low) * 0.3   # slight extra dip
        y = (1-t)**2 * y_start + 2*(1-t)*t * ctrl_y + t**2 * y_end
        xs.append(x)
        ys.append(y)
    return xs, ys


# ---------------------------------------------------------------------------
# Main painter class
# ---------------------------------------------------------------------------

class PatternPainter:
    """
    Takes an OHLCV DataFrame (DatetimeIndex) and a fitted PatternRecognizer,
    and produces overlay traces for Plotly or Lightweight Charts.

    Parameters
    ----------
    df : pd.DataFrame
        Must have DatetimeIndex and columns: Open, High, Low, Close, Volume.
    recognizer : PatternRecognizer
        Already fed all bars (process_bar called for each row).
    label_prices : bool
        Whether to annotate key prices (True matches MarketSurge style).
    """

    def __init__(self, df: pd.DataFrame, recognizer, label_prices: bool = True):
        self.df = df
        self.rec = recognizer
        self.label_prices = label_prices
        self._idx_to_ts = {i: df.index[i].timestamp() for i in range(len(df))}
        self._idx_to_date = {i: df.index[i] for i in range(len(df))}

    # ------------------------------------------------------------------
    # Public: Plotly traces
    # ------------------------------------------------------------------

    def get_plotly_traces(self) -> List[Any]:
        """Return a list of plotly trace objects to add to a figure (row=1, col=1)."""
        if go is None:
            raise ImportError("plotly is required for get_plotly_traces()")
        traces = []
        annotations = []
        patterns = self.rec.get_all_patterns()

        # ---- Flat Bases ----
        for fb in patterns["flat_bases"]:
            t = self._flat_base_plotly(fb)
            traces.extend(t["traces"])
            annotations.extend(t["annotations"])

        # ---- Cups (no handle) ----
        for cup in patterns["cups"]:
            t = self._cup_plotly(cup, has_handle=False)
            traces.extend(t["traces"])
            annotations.extend(t["annotations"])

        # ---- Cups with Handle ----
        for cup in patterns["cups_with_handle"]:
            t = self._cup_plotly(cup, has_handle=True)
            traces.extend(t["traces"])
            annotations.extend(t["annotations"])

        # ---- Double Bottoms ----
        for db in patterns["double_bottoms"]:
            t = self._double_bottom_plotly(db)
            traces.extend(t["traces"])
            annotations.extend(t["annotations"])

        # Store annotations separately so caller can add them to layout
        self._pending_annotations = annotations
        return traces

    def get_pending_annotations(self) -> List[Dict]:
        """Call after get_plotly_traces() to get price label annotations."""
        return getattr(self, "_pending_annotations", [])

    # ------------------------------------------------------------------
    # Public: Lightweight Charts data
    # ------------------------------------------------------------------

    def get_lightweight_data(self) -> Dict[str, Any]:
        """
        Returns a dict with lists of line/area series data for the JS renderer.
        Structure:
          {
            "arcs":  [{"x": [...timestamps...], "y": [...prices...], "color": ..., "width": ...}, ...],
            "hlines": [{"x0": ts, "x1": ts, "y": price, "dash": bool, "color": ...}, ...],
            "labels": [{"x": ts, "y": price, "text": "244.14"}, ...],
          }
        """
        out = {"arcs": [], "hlines": [], "labels": []}
        patterns = self.rec.get_all_patterns()

        for fb in patterns["flat_bases"]:
            self._flat_base_lw(fb, out)

        for cup in patterns["cups"]:
            self._cup_lw(cup, has_handle=False, out=out)

        for cup in patterns["cups_with_handle"]:
            self._cup_lw(cup, has_handle=True, out=out)

        for db in patterns["double_bottoms"]:
            self._double_bottom_lw(db, out)

        return out

    # ------------------------------------------------------------------
    # Internal helpers: bar index → x coordinate
    # ------------------------------------------------------------------

    def _x(self, bar_idx: int):
        """Return the Pandas Timestamp for a given bar index (for Plotly)."""
        if bar_idx < 0 or bar_idx >= len(self.df):
            bar_idx = max(0, min(bar_idx, len(self.df) - 1))
        return self.df.index[bar_idx]

    def _ts(self, bar_idx: int) -> float:
        """Return Unix timestamp for Lightweight Charts."""
        if bar_idx < 0 or bar_idx >= len(self.df):
            bar_idx = max(0, min(bar_idx, len(self.df) - 1))
        return self.df.index[bar_idx].timestamp()

    def _bar_to_float(self, bar_idx: int) -> float:
        """Float bar index for arc maths (we work in bar-space then convert)."""
        return float(bar_idx)

    def _arc_bars_to_dates(self, xs_float: List[float]) -> List:
        """Convert float bar positions → nearest Timestamps (for Plotly x-axis)."""
        result = []
        n = len(self.df)
        for xf in xs_float:
            lo = int(math.floor(xf))
            hi = int(math.ceil(xf))
            lo = max(0, min(lo, n - 1))
            hi = max(0, min(hi, n - 1))
            if lo == hi:
                result.append(self.df.index[lo])
            else:
                frac = xf - lo
                ts_lo = self.df.index[lo].timestamp()
                ts_hi = self.df.index[hi].timestamp()
                ts_interp = ts_lo + frac * (ts_hi - ts_lo)
                result.append(pd.Timestamp(ts_interp, unit="s", tz=self.df.index.tz))
        return result

    def _arc_bars_to_ts(self, xs_float: List[float]) -> List[int]:
        """Convert float bar positions → Unix timestamps (for Lightweight Charts)."""
        result = []
        n = len(self.df)
        for xf in xs_float:
            lo = int(math.floor(xf))
            hi = int(math.ceil(xf))
            lo = max(0, min(lo, n - 1))
            hi = max(0, min(hi, n - 1))
            if lo == hi:
                result.append(int(self.df.index[lo].timestamp()))
            else:
                frac = xf - lo
                ts_lo = self.df.index[lo].timestamp()
                ts_hi = self.df.index[hi].timestamp()
                result.append(int(ts_lo + frac * (ts_hi - ts_lo)))
        return result

    # ------------------------------------------------------------------
    # Flat Base
    # ------------------------------------------------------------------

    def _flat_base_plotly(self, fb) -> Dict:
        traces, annotations = [], []
        b = fb.base
        x_start = self._x(b.start_bar)
        x_end   = self._x(min(b.start_bar + b.length, len(self.df) - 1))

        # Solid bottom line
        traces.append(go.Scatter(
            x=[x_start, x_end], y=[b.low_price, b.low_price],
            mode="lines", line=dict(color=GREEN_SOLID, width=ARC_WIDTH),
            name="Flat Base Low", showlegend=False, hoverinfo="skip",
        ))
        # Dashed top line (pivot high)
        traces.append(go.Scatter(
            x=[x_start, x_end], y=[b.start_price, b.start_price],
            mode="lines", line=dict(color=GREEN_DASH, width=DASH_WIDTH, dash="dash"),
            name="Flat Base High", showlegend=False, hoverinfo="skip",
        ))
        # Light fill between
        traces.append(go.Scatter(
            x=[x_start, x_end, x_end, x_start],
            y=[b.low_price, b.low_price, b.start_price, b.start_price],
            fill="toself", fillcolor=GREEN_FILL,
            line=dict(width=0), mode="lines",
            showlegend=False, hoverinfo="skip",
        ))
        if self.label_prices:
            annotations += [
                _annotation(x_start, b.start_price, f"{b.start_price:.2f}", "top left"),
                _annotation(x_start, b.low_price,   f"{b.low_price:.2f}",   "bottom left"),
                _annotation(x_end,   b.start_price, f"{b.start_price:.2f}", "top right"),
            ]
        return {"traces": traces, "annotations": annotations}

    def _flat_base_lw(self, fb, out: Dict):
        b = fb.base
        ts_start = self._ts(b.start_bar)
        ts_end   = self._ts(min(b.start_bar + b.length, len(self.df) - 1))
        out["hlines"].append({"x0": ts_start, "x1": ts_end, "y": b.low_price,   "dash": False, "color": GREEN_SOLID, "width": ARC_WIDTH})
        out["hlines"].append({"x0": ts_start, "x1": ts_end, "y": b.start_price, "dash": True,  "color": GREEN_DASH,  "width": DASH_WIDTH})
        if self.label_prices:
            out["labels"] += [
                {"x": ts_start, "y": b.start_price, "text": f"{b.start_price:.2f}", "position": "top"},
                {"x": ts_start, "y": b.low_price,   "text": f"{b.low_price:.2f}",   "position": "bottom"},
            ]

    # ------------------------------------------------------------------
    # Cup / Cup-with-Handle
    # ------------------------------------------------------------------

    def _cup_plotly(self, cup, has_handle: bool) -> Dict:
        traces, annotations = [], []

        left_bar   = cup.left_bar
        bottom_bar = cup.bottom_bar
        right_bar  = cup.right_bar

        # Clamp to available df length
        n = len(self.df)
        left_bar   = min(left_bar,   n - 1)
        bottom_bar = min(bottom_bar, n - 1)
        right_bar  = min(right_bar,  n - 1)

        left_high  = cup.left_high
        bottom     = cup.bottom
        right_high = cup.right_high

        # --- Dashed horizontal line at left-side high (extends full base width) ---
        x_line_end = self._x(cup.handle_start_bar + cup.handle_length - 1) if has_handle else self._x(right_bar)
        traces.append(go.Scatter(
            x=[self._x(left_bar), x_line_end],
            y=[left_high, left_high],
            mode="lines",
            line=dict(color=GREEN_DASH, width=DASH_WIDTH, dash="dash"),
            name="Cup Pivot", showlegend=False, hoverinfo="skip",
        ))

        # --- Smooth arc (left high → bottom → right high) ---
        xs_f, ys_arc = _cup_arc_points(
            float(left_bar), left_high,
            float(bottom_bar), bottom,
            float(right_bar), right_high,
        )
        x_dates = self._arc_bars_to_dates(xs_f)
        traces.append(go.Scatter(
            x=x_dates, y=ys_arc,
            mode="lines",
            line=dict(color=GREEN_SOLID, width=ARC_WIDTH, shape="spline", smoothing=0.8),
            name="Cup Arc", showlegend=False, hoverinfo="skip",
        ))

        # --- Handle arc ---
        if has_handle and cup.handle_start_bar >= 0:
            hs  = min(cup.handle_start_bar, n - 1)
            hl  = min(cup.handle_low_bar,   n - 1)
            he  = min(cup.handle_start_bar + cup.handle_length - 1, n - 1)
            hxs, hys = _handle_arc_points(
                float(hs),  right_high,          # start = right lip
                float(hl),  cup.handle_low_price, # dip
                float(he),  cup.pivot_price,       # end at handle high = buy point
            )
            hx_dates = self._arc_bars_to_dates(hxs)
            traces.append(go.Scatter(
                x=hx_dates, y=hys,
                mode="lines",
                line=dict(color=GREEN_SOLID, width=ARC_WIDTH - 0.5, shape="spline", smoothing=0.8),
                name="Handle Arc", showlegend=False, hoverinfo="skip",
            ))
            # Dashed buy-point line at handle high
            traces.append(go.Scatter(
                x=[self._x(hs), self._x(he)],
                y=[cup.pivot_price, cup.pivot_price],
                mode="lines",
                line=dict(color=GREEN_DASH, width=DASH_WIDTH, dash="dash"),
                name="Handle Pivot", showlegend=False, hoverinfo="skip",
            ))
            if self.label_prices:
                annotations.append(_annotation(self._x(he), cup.pivot_price, f"{cup.pivot_price:.2f}", "top right"))
                annotations.append(_annotation(self._x(hl), cup.handle_low_price, f"{cup.handle_low_price:.2f}", "bottom center"))

        # --- Price labels ---
        if self.label_prices:
            annotations += [
                _annotation(self._x(left_bar),   left_high, f"{left_high:.2f}",  "top left"),
                _annotation(self._x(bottom_bar), bottom,    f"{bottom:.2f}",     "bottom center"),
                _annotation(self._x(right_bar),  right_high, f"{right_high:.2f}", "top right"),
            ]

        return {"traces": traces, "annotations": annotations}

    def _cup_lw(self, cup, has_handle: bool, out: Dict):
        n = len(self.df)
        left_bar   = min(cup.left_bar,   n - 1)
        bottom_bar = min(cup.bottom_bar, n - 1)
        right_bar  = min(cup.right_bar,  n - 1)

        # Dashed pivot line
        ts_end = self._ts(cup.handle_start_bar + cup.handle_length - 1) if has_handle else self._ts(right_bar)
        out["hlines"].append({"x0": self._ts(left_bar), "x1": ts_end,
                               "y": cup.left_high, "dash": True,
                               "color": GREEN_DASH, "width": DASH_WIDTH})

        # Arc series
        xs_f, ys_arc = _cup_arc_points(
            float(left_bar), cup.left_high,
            float(bottom_bar), cup.bottom,
            float(right_bar), cup.right_high,
        )
        ts_list = self._arc_bars_to_ts(xs_f)
        out["arcs"].append({
            "x": ts_list, "y": ys_arc,
            "color": GREEN_SOLID, "width": ARC_WIDTH,
        })

        # Handle arc
        if has_handle and cup.handle_start_bar >= 0:
            hs = min(cup.handle_start_bar, n - 1)
            hl = min(cup.handle_low_bar,   n - 1)
            he = min(cup.handle_start_bar + cup.handle_length - 1, n - 1)
            hxs, hys = _handle_arc_points(
                float(hs), cup.right_high,
                float(hl), cup.handle_low_price,
                float(he), cup.pivot_price,
            )
            out["arcs"].append({
                "x": self._arc_bars_to_ts(hxs), "y": hys,
                "color": GREEN_SOLID, "width": ARC_WIDTH - 0.5,
            })
            out["hlines"].append({"x0": self._ts(hs), "x1": self._ts(he),
                                   "y": cup.pivot_price, "dash": True,
                                   "color": GREEN_DASH, "width": DASH_WIDTH})
            if self.label_prices:
                out["labels"] += [
                    {"x": self._ts(he), "y": cup.pivot_price,       "text": f"{cup.pivot_price:.2f}",       "position": "top"},
                    {"x": self._ts(hl), "y": cup.handle_low_price, "text": f"{cup.handle_low_price:.2f}", "position": "bottom"},
                ]

        if self.label_prices:
            out["labels"] += [
                {"x": self._ts(left_bar),   "y": cup.left_high, "text": f"{cup.left_high:.2f}",  "position": "top"},
                {"x": self._ts(bottom_bar), "y": cup.bottom,    "text": f"{cup.bottom:.2f}",     "position": "bottom"},
                {"x": self._ts(right_bar),  "y": cup.right_high,"text": f"{cup.right_high:.2f}", "position": "top"},
            ]

    # ------------------------------------------------------------------
    # Double Bottom (W pattern)
    # ------------------------------------------------------------------

    def _double_bottom_plotly(self, db) -> Dict:
        traces, annotations = [], []
        n = len(self.df)

        fb1 = min(db.first_high_bar,  n - 1)
        fl1 = min(db.first_low_bar,   n - 1)
        mh  = min(db.mid_high_bar,    n - 1)
        fl2 = min(db.second_low_bar,  n - 1)
        # Right edge = current bar (last available)
        right_bar = n - 1

        fh1p = db.first_high_price
        fl1p = db.first_low_price
        mhp  = db.mid_high_price
        fl2p = db.second_low_price
        # Right side recovers to approx mid_high (still forming)
        right_p = self.df['Close'].iloc[right_bar]

        # --- Left arc: first high → first low → mid high ---
        xs1, ys1 = _cup_arc_points(
            float(fb1), fh1p,
            float(fl1), fl1p,
            float(mh),  mhp,
        )
        traces.append(go.Scatter(
            x=self._arc_bars_to_dates(xs1), y=ys1,
            mode="lines",
            line=dict(color=GREEN_SOLID, width=ARC_WIDTH, shape="spline", smoothing=0.8),
            name="DB Left Arc", showlegend=False, hoverinfo="skip",
        ))

        # --- Right arc: mid high → second low → current ---
        xs2, ys2 = _cup_arc_points(
            float(mh),       mhp,
            float(fl2),      fl2p,
            float(right_bar), right_p,
        )
        traces.append(go.Scatter(
            x=self._arc_bars_to_dates(xs2), y=ys2,
            mode="lines",
            line=dict(color=GREEN_SOLID, width=ARC_WIDTH, shape="spline", smoothing=0.8),
            name="DB Right Arc", showlegend=False, hoverinfo="skip",
        ))

        # --- Dashed line at mid-peak (buy point) ---
        traces.append(go.Scatter(
            x=[self._x(mh), self._x(right_bar)],
            y=[mhp, mhp],
            mode="lines",
            line=dict(color=GREEN_DASH, width=DASH_WIDTH, dash="dash"),
            name="DB Pivot", showlegend=False, hoverinfo="skip",
        ))

        # --- Dashed line at first high level (full width) ---
        traces.append(go.Scatter(
            x=[self._x(fb1), self._x(right_bar)],
            y=[fh1p, fh1p],
            mode="lines",
            line=dict(color=GREEN_DASH, width=DASH_WIDTH - 0.5, dash="dot"),
            name="DB Top", showlegend=False, hoverinfo="skip",
        ))

        if self.label_prices:
            annotations += [
                _annotation(self._x(fb1), fh1p, f"{fh1p:.2f}", "top left"),
                _annotation(self._x(fl1), fl1p, f"{fl1p:.2f}", "bottom center"),
                _annotation(self._x(mh),  mhp,  f"{mhp:.2f}",  "top center"),
                _annotation(self._x(fl2), fl2p, f"{fl2p:.2f}", "bottom center"),
            ]

        return {"traces": traces, "annotations": annotations}

    def _double_bottom_lw(self, db, out: Dict):
        n = len(self.df)
        fb1 = min(db.first_high_bar,  n - 1)
        fl1 = min(db.first_low_bar,   n - 1)
        mh  = min(db.mid_high_bar,    n - 1)
        fl2 = min(db.second_low_bar,  n - 1)
        right_bar = n - 1
        right_p = float(self.df['Close'].iloc[right_bar])

        xs1, ys1 = _cup_arc_points(float(fb1), db.first_high_price, float(fl1), db.first_low_price, float(mh), db.mid_high_price)
        xs2, ys2 = _cup_arc_points(float(mh), db.mid_high_price, float(fl2), db.second_low_price, float(right_bar), right_p)

        out["arcs"].append({"x": self._arc_bars_to_ts(xs1), "y": ys1, "color": GREEN_SOLID, "width": ARC_WIDTH})
        out["arcs"].append({"x": self._arc_bars_to_ts(xs2), "y": ys2, "color": GREEN_SOLID, "width": ARC_WIDTH})
        out["hlines"].append({"x0": self._ts(mh), "x1": self._ts(right_bar), "y": db.mid_high_price, "dash": True, "color": GREEN_DASH, "width": DASH_WIDTH})
        out["hlines"].append({"x0": self._ts(fb1), "x1": self._ts(right_bar), "y": db.first_high_price, "dash": True, "color": GREEN_DASH, "width": DASH_WIDTH - 0.5})

        if self.label_prices:
            out["labels"] += [
                {"x": self._ts(fb1), "y": db.first_high_price, "text": f"{db.first_high_price:.2f}", "position": "top"},
                {"x": self._ts(fl1), "y": db.first_low_price,  "text": f"{db.first_low_price:.2f}",  "position": "bottom"},
                {"x": self._ts(mh),  "y": db.mid_high_price,   "text": f"{db.mid_high_price:.2f}",   "position": "top"},
                {"x": self._ts(fl2), "y": db.second_low_price, "text": f"{db.second_low_price:.2f}", "position": "bottom"},
            ]


# ---------------------------------------------------------------------------
# Helper: create a Plotly annotation dict
# ---------------------------------------------------------------------------

def _annotation(x, y: float, text: str, position: str = "top center") -> Dict:
    pos_map = {
        "top left":     ("left",   "bottom"),
        "top right":    ("right",  "bottom"),
        "top center":   ("center", "bottom"),
        "bottom left":  ("left",   "top"),
        "bottom right": ("right",  "top"),
        "bottom center":("center", "top"),
    }
    xanchor, yanchor = pos_map.get(position, ("center", "bottom"))
    ay = -18 if "top" in position else 18
    return dict(
        x=x, y=y,
        text=f"<b>{text}</b>",
        showarrow=False,
        xanchor=xanchor, yanchor=yanchor,
        font=dict(size=LABEL_SIZE, color=GREEN_LABEL, family="monospace"),
        bgcolor="rgba(255,255,255,0.75)",
        borderpad=2,
        ay=ay,
    )


# ---------------------------------------------------------------------------
# Lightweight Charts JS injection helper
# ---------------------------------------------------------------------------

def build_lw_pattern_js(lw_data: Dict[str, Any], chart_var: str = "priceChart") -> str:
    """
    Returns a JS snippet (to embed in the HTML template) that paints arcs,
    horizontal lines, and price labels onto a Lightweight Charts instance.

    Parameters
    ----------
    lw_data : dict
        Output of PatternPainter.get_lightweight_data()
    chart_var : str
        Name of the JS variable holding the LightweightCharts chart instance.
    """
    import json

    arcs   = lw_data.get("arcs",   [])
    hlines = lw_data.get("hlines", [])
    labels = lw_data.get("labels", [])

    js_parts = []

    # --- arcs (rendered as line series) ---
    for arc in arcs:
        pts = [{"time": int(t), "value": v} for t, v in zip(arc["x"], arc["y"])]
        js_parts.append(f"""
(function() {{
    const arcSeries = {chart_var}.addLineSeries({{
        color: '{arc["color"]}',
        lineWidth: {arc["width"]},
        crosshairMarkerVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        lineStyle: 0
    }});
    arcSeries.setData({json.dumps(pts)});
}})();
""")

    # --- horizontal lines (rendered as single-value line series spanning x0→x1) ---
    for hl in hlines:
        dash_style = "1" if hl.get("dash") else "0"   # 0=solid, 1=dotted, 2=dashed, 3=largeDashed, 4=sparseDotted
        js_parts.append(f"""
(function() {{
    const hlSeries = {chart_var}.addLineSeries({{
        color: '{hl["color"]}',
        lineWidth: {hl["width"]},
        crosshairMarkerVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        lineStyle: {dash_style}
    }});
    hlSeries.setData([
        {{ time: {int(hl["x0"])}, value: {hl["y"]} }},
        {{ time: {int(hl["x1"])}, value: {hl["y"]} }}
    ]);
}})();
""")

    # --- price labels (rendered as markers on a zero-width line series) ---
    # Group all labels into a single marker set on the candlestick series
    if labels:
        marker_list = []
        for lb in labels:
            position = "aboveBar" if lb.get("position", "top") == "top" else "belowBar"
            marker_list.append({
                "time": int(lb["x"]),
                "position": position,
                "color": GREEN_LABEL,
                "shape": "circle",
                "size": 0,           # invisible dot; only text shown
                "text": lb["text"],
            })
        # We inject these onto the candle series via JS after series is created.
        # Caller is responsible for merging with any existing markers.
        js_parts.append(f"""
(function() {{
    // Pattern price labels — caller should merge with candleSeries.setMarkers()
    if (typeof window._patternMarkers === 'undefined') window._patternMarkers = [];
    window._patternMarkers = window._patternMarkers.concat({json.dumps(marker_list)});
}})();
""")

    return "\n".join(js_parts)