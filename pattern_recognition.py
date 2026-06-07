"""
pattern_recognition.py

Comprehensive chart pattern recognizer implementing MarketSmith-style logic for:
  - Flat Base
  - Cup (without handle)
  - Cup with Handle
  - Double Bottom

Design incorporates:
  - Heiken Ashi price smoothing (from R script) for cleaner pivot detection
  - Pivot high/low detection on smoothed prices
  - RPV (Rate × Price × Volume) momentum scoring (from R script)
  - O'Neil base rules: 6–65 week length, ≤50% depth, prior uptrend requirement
  - Cup curve generation (exponential left descent, exponential right ascent)
  - Handle validation: tight, downward-drifting, low-volume shakeout
  - Double Bottom W-pattern validation with timing and symmetry checks

Usage
-----
    recognizer = PatternRecognizer(weekly=True)
    for i, row in enumerate(ohlcv_rows):
        recognizer.process_bar(row.high, row.low, row.close,
                               row.volume, row.open, idx=i)
    results = recognizer.get_all_patterns()
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes for pattern results
# ---------------------------------------------------------------------------

@dataclass
class BaseInfo:
    start_bar: int
    start_price: float      # left-side high (pivot high that started the base)
    low_price: float        # lowest low within the base
    low_bar: int
    depth_pct: float        # (start_price - low_price) / start_price
    length: int             # bars since base started


@dataclass
class FlatBaseResult:
    base: BaseInfo
    is_valid: bool
    notes: str = ""


@dataclass
class CupResult:
    base: BaseInfo
    left_high: float
    left_bar: int
    bottom: float
    bottom_bar: int
    right_high: float
    right_bar: int
    curve_x: List[int] = field(default_factory=list)
    curve_y: List[float] = field(default_factory=list)
    has_handle: bool = False
    handle_start_bar: int = -1
    handle_low_bar: int = -1
    handle_low_price: float = 0.0
    handle_depth_pct: float = 0.0
    handle_length: int = 0
    pivot_price: float = 0.0   # buy point = left_high (+ small tolerance)
    score: float = 0.0         # RPV-based quality score


@dataclass
class DoubleBottomResult:
    base: BaseInfo
    first_high_bar: int
    first_high_price: float
    first_low_bar: int
    first_low_price: float
    mid_high_bar: int
    mid_high_price: float
    second_low_bar: int
    second_low_price: float
    pivot_price: float          # mid_high_price = buy point
    score: float = 0.0


# ---------------------------------------------------------------------------
# Heiken Ashi helper
# ---------------------------------------------------------------------------

def compute_heiken_ashi(
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
) -> Tuple[List[float], List[float], List[float], List[float]]:
    """Return (ha_open, ha_high, ha_low, ha_close) arrays."""
    n = len(closes)
    ha_open = [0.0] * n
    ha_high = [0.0] * n
    ha_low = [0.0] * n
    ha_close = [0.0] * n

    ha_open[0] = opens[0]
    ha_close[0] = (opens[0] + highs[0] + lows[0] + closes[0]) / 4
    ha_high[0] = highs[0]
    ha_low[0] = lows[0]

    for i in range(1, n):
        ha_close[i] = (opens[i] + highs[i] + lows[i] + closes[i]) / 4
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2
        ha_high[i] = max(highs[i], ha_open[i], ha_close[i])
        ha_low[i] = min(lows[i], ha_open[i], ha_close[i])

    return ha_open, ha_high, ha_low, ha_close


# ---------------------------------------------------------------------------
# Simple moving average
# ---------------------------------------------------------------------------

def _sma(data: List[float], period: int, idx: int) -> Optional[float]:
    if idx < period - 1:
        return None
    return sum(data[idx - period + 1 : idx + 1]) / period


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class PatternRecognizer:
    """
    Stream-based pattern recognizer. Call `process_bar` once per bar in order.
    After each bar you can query `get_all_patterns()` for current detections.

    Parameters
    ----------
    weekly : bool
        If True, treat each bar as a week (6–65 bar base = 6–65 weeks).
        If False, treat each bar as a day; base limits are scaled to days
        (30–325 bars ≈ 6–65 weeks × 5).
    base_depth : float
        Maximum allowed depth of a base as a fraction of the left-side high.
        Default 0.50 (50 %).
    flat_base_max_depth : float
        Maximum depth for a pattern to be classified as a Flat Base.
        Default 0.15 (15 %).
    pivot_length : int
        Bars on each side required to confirm a pivot high/low.
    handle_max_depth : float
        Maximum handle depth as a fraction of the cup's left-side high.
        Default 0.12 (12 %).
    handle_max_bars : int
        Maximum bars allowed for a handle. Default 5 (weeks).
    volume_period : int
        SMA period for average volume (used in RPV scoring).
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        weekly: bool = True,
        base_depth: float = 0.50,
        flat_base_max_depth: float = 0.15,
        pivot_length: int = 5,
        handle_max_depth: float = 0.12,
        handle_max_bars: int = 5,
        volume_period: int = 50,
    ):
        self.weekly = weekly
        self.base_depth = base_depth
        self.flat_base_max_depth = flat_base_max_depth
        self.pivot_length = pivot_length
        self.handle_max_depth = handle_max_depth
        self.handle_max_bars = handle_max_bars
        self.volume_period = volume_period

        # Scale base length limits by time unit
        if weekly:
            self.base_min_bars = 6
            self.base_max_bars = 65
            self.cup_min_bars = 7       # cups need a bit more room
            self.prior_uptrend_bars = 26  # look-back for prior uptrend check
            self.prior_uptrend_pct = 0.30 # 30% gain before the base
            self.leg_up_min_pct = 0.20
        else:
            self.base_min_bars = 30
            self.base_max_bars = 325
            self.cup_min_bars = 35
            self.prior_uptrend_bars = 130
            self.prior_uptrend_pct = 0.30
            self.leg_up_min_pct = 0.20

        # --- raw price series ---
        self.raw_open: List[float] = []
        self.raw_high: List[float] = []
        self.raw_low: List[float] = []
        self.raw_close: List[float] = []
        self.volume: List[float] = []

        # --- smoothed (Heiken Ashi) series ---
        self.ha_open: List[float] = []
        self.ha_high: List[float] = []
        self.ha_low: List[float] = []
        self.ha_close: List[float] = []

        # --- RPV (rate × price × volume) ---
        self.rpv: List[float] = []           # bar-level RPV
        self.avg_volume: List[float] = []    # 50-bar SMA of volume

        # --- pivot storage (bar index, price) ---
        self.pivot_highs: List[Tuple[int, float]] = []  # (bar, price)
        self.pivot_lows: List[Tuple[int, float]] = []

        # --- active base state ---
        self._reset_base()

        # --- completed / active pattern results ---
        self.flat_bases: List[FlatBaseResult] = []
        self.cups: List[CupResult] = []
        self.double_bottoms: List[DoubleBottomResult] = []

        self._current_bar = -1

    # ------------------------------------------------------------------
    # Internal reset
    # ------------------------------------------------------------------

    def _reset_base(self):
        self._in_base = False
        self._base_start_bar: int = -1
        self._base_start_price: float = 0.0     # left-side high
        self._base_low_price: float = float("inf")
        self._base_low_bar: int = -1
        self._base_closes: List[float] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_bar(
        self,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
        open_: float = 0.0,
        idx: int = -1,
    ):
        """
        Ingest one OHLCV bar and run pattern detection.

        Parameters
        ----------
        high, low, close : float
            OHLCV values for this bar.
        volume : float
            Bar volume (used for RPV scoring; can be omitted / set to 0).
        open_ : float
            Bar open price (for Heiken Ashi; defaults to previous close if 0).
        idx : int
            Optional explicit bar index. If -1, auto-incremented.
        """
        if idx == -1:
            idx = self._current_bar + 1
        self._current_bar = idx

        # default open to previous close if not supplied
        if open_ == 0.0 and self.raw_close:
            open_ = self.raw_close[-1]
        elif open_ == 0.0:
            open_ = close

        # store raw
        self.raw_open.append(open_)
        self.raw_high.append(high)
        self.raw_low.append(low)
        self.raw_close.append(close)
        self.volume.append(volume)

        # recompute full Heiken Ashi (efficient enough for typical scan sizes)
        ha_o, ha_h, ha_l, ha_c = compute_heiken_ashi(
            self.raw_open, self.raw_high, self.raw_low, self.raw_close
        )
        self.ha_open = ha_o
        self.ha_high = ha_h
        self.ha_low = ha_l
        self.ha_close = ha_c

        # RPV: change in HA close × volume
        if len(self.ha_close) >= 2:
            rate = self.ha_close[-1] - self.ha_close[-2]
            self.rpv.append(rate * volume)
        else:
            self.rpv.append(0.0)

        # average volume SMA
        avg_vol = _sma(self.volume, self.volume_period, idx)
        self.avg_volume.append(avg_vol if avg_vol is not None else volume)

        # pivot detection (needs pivot_length bars on each side — runs on
        # bar idx - pivot_length, i.e. lagged)
        self._detect_pivot(idx)

        # base / pattern detection
        self._run_base_logic(idx)

    def get_all_patterns(self) -> dict:
        """Return all currently detected patterns."""
        return {
            "flat_bases": self.flat_bases,
            "cups": [c for c in self.cups if not c.has_handle],
            "cups_with_handle": [c for c in self.cups if c.has_handle],
            "double_bottoms": self.double_bottoms,
        }

    # ------------------------------------------------------------------
    # Pivot detection (on HA prices)
    # ------------------------------------------------------------------

    def _detect_pivot(self, current_idx: int):
        """
        Detect pivot highs/lows at bar (current_idx - pivot_length).
        Uses Heiken Ashi high/low for smoother signals.
        """
        check_idx = current_idx - self.pivot_length
        if check_idx < self.pivot_length:
            return
        if check_idx + self.pivot_length >= len(self.ha_high):
            return

        h = self.ha_high[check_idx]
        l = self.ha_low[check_idx]

        # pivot high: strictly highest in window
        is_ph = all(
            h > self.ha_high[check_idx - i] and h > self.ha_high[check_idx + i]
            for i in range(1, self.pivot_length + 1)
        )
        if is_ph:
            self.pivot_highs.insert(0, (check_idx, h))

        # pivot low: strictly lowest in window
        is_pl = all(
            l < self.ha_low[check_idx - i] and l < self.ha_low[check_idx + i]
            for i in range(1, self.pivot_length + 1)
        )
        if is_pl:
            self.pivot_lows.insert(0, (check_idx, l))

    # ------------------------------------------------------------------
    # Base detection state machine
    # ------------------------------------------------------------------

    def _run_base_logic(self, idx: int):
        if not self._in_base:
            self._try_start_base(idx)
        else:
            self._update_active_base(idx)

    def _try_start_base(self, idx: int):
        """
        A new base begins when we identify a significant pivot high that:
        1. Is a new high over the prior base_max_bars window (or beats the
           prior base's left-side high → base on base).
        2. Is preceded by a leg up of at least 20% from the prior low.
        """
        if len(self.pivot_highs) == 0:
            return

        # Most recent pivot high
        ph_bar, ph_price = self.pivot_highs[0]

        # Must have some look-back room
        if ph_bar < self.prior_uptrend_bars:
            return

        # --- prior uptrend check ---
        # Price must be at least 20% above the lowest close in the prior window
        look_back = self.raw_low[max(0, ph_bar - self.prior_uptrend_bars) : ph_bar]
        if not look_back:
            return
        prior_low = min(look_back)
        if ph_price < prior_low * (1 + self.leg_up_min_pct):
            return

        # --- 52-week high check (or beats prior base top) ---
        look_high = self.raw_high[max(0, ph_bar - self.base_max_bars) : ph_bar]
        is_new_high = ph_price >= max(look_high) if look_high else True
        beats_prior_base = False
        if self.cups:
            beats_prior_base = ph_price > self.cups[-1].left_high
        if self.flat_bases:
            beats_prior_base = beats_prior_base or (ph_price > self.flat_bases[-1].base.start_price)

        if not (is_new_high or beats_prior_base):
            return

        # --- start the base ---
        self._in_base = True
        self._base_start_bar = ph_bar
        self._base_start_price = ph_price
        # collect lows from the pivot high bar forward
        self._base_low_price = min(self.raw_low[ph_bar:idx + 1]) if idx >= ph_bar else self.raw_low[ph_bar]
        self._base_low_bar = (
            ph_bar + self.raw_low[ph_bar:idx + 1].index(self._base_low_price)
            if idx >= ph_bar else ph_bar
        )
        self._base_closes = list(self.raw_close[ph_bar : idx + 1])

    def _update_active_base(self, idx: int):
        """
        Continue tracking the active base. On each bar:
        - Update low.
        - Check if base is broken (too deep or too long → reset).
        - Try to classify as Flat Base, Cup/CwH, or Double Bottom.
        """
        self._base_closes.append(self.raw_close[idx])
        bar_count = idx - self._base_start_bar

        # Update base low
        if self.raw_low[idx] < self._base_low_price:
            self._base_low_price = self.raw_low[idx]
            self._base_low_bar = idx

        depth = (self._base_start_price - self._base_low_price) / self._base_start_price

        # --- base invalidation ---
        if depth > self.base_depth or bar_count > self.base_max_bars:
            self._reset_base()
            return

        # Need minimum length before trying to classify
        if bar_count < self.base_min_bars:
            return

        base_info = BaseInfo(
            start_bar=self._base_start_bar,
            start_price=self._base_start_price,
            low_price=self._base_low_price,
            low_bar=self._base_low_bar,
            depth_pct=depth,
            length=bar_count,
        )

        # --- Flat Base (tight consolidation ≤ 15% depth) ---
        if depth <= self.flat_base_max_depth:
            result = FlatBaseResult(base=base_info, is_valid=True,
                                    notes=f"depth={depth:.1%}, length={bar_count}w")
            self._upsert_flat_base(result)

        # --- Cup / Cup with Handle ---
        cup = self._try_cup(base_info, idx)
        if cup is not None:
            self._upsert_cup(cup)

        # --- Double Bottom ---
        db = self._try_double_bottom(base_info, idx)
        if db is not None:
            self._upsert_double_bottom(db)

    # ------------------------------------------------------------------
    # Flat Base classification
    # ------------------------------------------------------------------

    def _upsert_flat_base(self, result: FlatBaseResult):
        # Replace the last flat base if it shares the same start bar
        if self.flat_bases and self.flat_bases[-1].base.start_bar == result.base.start_bar:
            self.flat_bases[-1] = result
        else:
            self.flat_bases.append(result)

    # ------------------------------------------------------------------
    # Cup (with or without handle) detection
    # ------------------------------------------------------------------

    def _try_cup(self, base_info: BaseInfo, current_idx: int) -> Optional[CupResult]:
        """
        Cup shape requirements (MarketSmith / O'Neil rules):
          - Length: 7–65 weeks
          - Depth: 8–50% (depth already bounded by base logic at 50%)
          - Bottom must be below 92% of left-side high (not too shallow)
          - Close distribution: first third mostly above midpoint;
            middle portion mostly below midpoint (U-shape, not V-shape)
          - Right side must recover toward left-side high
          - Current price ≥ 50% up from the bottom toward the left high
            (forming the right lip)
        """
        if base_info.length < self.cup_min_bars:
            return None

        left_high = base_info.start_price
        bottom = base_info.low_price
        depth = base_info.depth_pct

        # Minimum meaningful cup depth (8%)
        if depth < 0.08:
            return None
        # Bottom must be meaningfully below the left high
        if bottom > left_high * 0.92:
            return None

        midpoint = bottom + (left_high - bottom) * 0.5

        closes = self._base_closes
        n = len(closes)
        if n < 6:
            return None

        third = max(1, n // 3)
        quarter = max(1, n // 4)

        # First third: >= 30% of closes above midpoint (still near the top)
        first_third = closes[:third]
        frac_above = sum(1 for c in first_third if c >= midpoint) / third

        # Last third: >= 85% of closes below midpoint (in the cup bowl)
        last_third = closes[-third:]
        frac_below_third = sum(1 for c in last_third if c <= midpoint) / third

        # First quarter: >= 30% above midpoint
        first_quarter = closes[:quarter]
        frac_above_q = sum(1 for c in first_quarter if c >= midpoint) / quarter

        # Last half: >= 85% below midpoint
        last_half = closes[-(2 * quarter):]
        frac_below_half = (
            sum(1 for c in last_half if c <= midpoint) / (2 * quarter)
            if 2 * quarter > 0 else 0
        )

        cup_form_thirds = frac_above >= 0.30 and frac_below_third >= 0.85
        cup_form_quarters = frac_above_q >= 0.30 and frac_below_half >= 0.85
        if not (cup_form_thirds or cup_form_quarters):
            return None

        # Right side must be recovering: current HA close ≥ midpoint
        if self.ha_close[current_idx] < midpoint:
            return None

        right_high = self.raw_high[current_idx]

        # Build cup curve
        curve_x, curve_y = self._build_cup_curve(
            left_idx=base_info.start_bar,
            bottom_idx=base_info.low_bar,
            right_idx=current_idx,
            left_high=left_high,
            bottom=bottom,
            right_high=right_high,
        )

        # RPV quality score: ratio of up-RPV to down-RPV during base
        score = self._rpv_score(base_info.start_bar, current_idx)

        cup = CupResult(
            base=base_info,
            left_high=left_high,
            left_bar=base_info.start_bar,
            bottom=bottom,
            bottom_bar=base_info.low_bar,
            right_high=right_high,
            right_bar=current_idx,
            curve_x=curve_x,
            curve_y=curve_y,
            pivot_price=left_high,  # buy point = left-side high breakout
            score=score,
        )

        # --- Try to attach a handle ---
        self._try_attach_handle(cup, current_idx)

        return cup

    def _try_attach_handle(self, cup: CupResult, current_idx: int):
        """
        Handle rules (O'Neil):
          - Begins after the right side of the cup has recovered near the prior high
          - Drifts downward no more than 8–12% over 1–5 weeks
          - Volume should be below its 10-week average (low-volume shakeout)
          - The low of the handle must be above the cup's midpoint
          - Pivot/buy point is the top of the handle (= right lip high)
        """
        left_high = cup.left_high
        bottom = cup.bottom
        midpoint = bottom + (left_high - bottom) * 0.5

        # The cup's right side must have reached ≥ 90% of the left high
        if cup.right_high < left_high * 0.90:
            return

        # Scan back up to handle_max_bars from current bar to find handle low
        handle_window = min(self.handle_max_bars, current_idx - cup.right_bar)
        if handle_window < 1:
            return

        start = max(cup.right_bar, current_idx - self.handle_max_bars)
        end = current_idx + 1

        window_lows = self.raw_low[start:end]
        window_highs = self.raw_high[start:end]
        window_vols = self.volume[start:end]
        avg_vols = self.avg_volume[start:end]

        if not window_lows:
            return

        handle_low = min(window_lows)
        handle_low_bar = start + window_lows.index(handle_low)
        handle_high = max(window_highs)

        # Handle low must be above the cup midpoint
        if handle_low < midpoint:
            return

        # Handle depth: drop from the right-lip high
        handle_depth = (handle_high - handle_low) / handle_high if handle_high > 0 else 0
        if handle_depth > self.handle_max_depth:
            return

        # Handle should drift slightly down (handle high ≤ left_high)
        if handle_high > left_high * 1.02:   # small tolerance
            return

        # Volume during handle should be below average (at least 50% of bars)
        low_vol_bars = sum(
            1 for v, av in zip(window_vols, avg_vols) if av > 0 and v < av
        )
        if low_vol_bars < len(window_vols) * 0.50:
            return

        cup.has_handle = True
        cup.handle_start_bar = start
        cup.handle_low_bar = handle_low_bar
        cup.handle_low_price = handle_low
        cup.handle_depth_pct = handle_depth
        cup.handle_length = end - start
        # Buy point = top of handle (slightly above the handle high)
        cup.pivot_price = handle_high

    def _upsert_cup(self, cup: CupResult):
        if self.cups and self.cups[-1].base.start_bar == cup.base.start_bar:
            self.cups[-1] = cup
        else:
            self.cups.append(cup)

    # ------------------------------------------------------------------
    # Double Bottom detection
    # ------------------------------------------------------------------

    def _try_double_bottom(
        self, base_info: BaseInfo, current_idx: int
    ) -> Optional[DoubleBottomResult]:
        """
        Double Bottom (W pattern) rules:
          - Need at least 2 pivot highs and 2 pivot lows within the base
          - First leg: down from the left-side high to the first bottom
          - Middle peak: rallies at least 35% of first-leg range but stays
            below the left-side high (< 101% of it)
          - Second leg: drops close to (within 3%) or below the first bottom
            but stays within 50% depth of the left-side high
          - The W must be at least 10 bars and no more than 65 bars total
          - Timing symmetry: each segment ≥ 3 bars; half-time ratio ≤ 4×
          - Buy point = middle peak price
        """
        if len(self.pivot_highs) < 2 or len(self.pivot_lows) < 2:
            return None

        # Collect pivots that fall inside the base window
        base_start = base_info.start_bar
        ph_in_base = [(b, p) for b, p in self.pivot_highs if base_start <= b <= current_idx]
        pl_in_base = [(b, p) for b, p in self.pivot_lows if base_start <= b <= current_idx]

        if len(ph_in_base) < 1 or len(pl_in_base) < 2:
            return None

        # We need: left_high → first_low → mid_high → second_low
        # The left-side high is base_info.start_price / start_bar
        left_bar = base_info.start_bar
        left_price = base_info.start_price

        # Find the first pivot low after left_bar
        pl_after_left = [(b, p) for b, p in pl_in_base if b > left_bar]
        if len(pl_after_left) < 2:
            return None

        first_low_bar, first_low_price = pl_after_left[-1]  # earliest = last in reverse list

        # Find a pivot high between first_low and current_idx
        ph_after_first_low = [(b, p) for b, p in ph_in_base if b > first_low_bar]
        if not ph_after_first_low:
            return None
        mid_high_bar, mid_high_price = ph_after_first_low[-1]  # earliest after first low

        # Find a pivot low after mid_high
        pl_after_mid = [(b, p) for b, p in pl_in_base if b > mid_high_bar]
        if not pl_after_mid:
            return None
        second_low_bar, second_low_price = pl_after_mid[-1]  # earliest after mid high

        # --- Price conditions ---
        first_leg_range = left_price - first_low_price
        if first_leg_range <= 0:
            return None

        # Prior uptrend: left high must be at least 20% above prior low
        look_back_start = max(0, left_bar - self.prior_uptrend_bars)
        prior_low = min(self.raw_low[look_back_start : left_bar]) if left_bar > 0 else first_low_price
        if left_price < prior_low * (1 + self.leg_up_min_pct):
            return None

        # Second low ≤ first low * 1.03 (close to or undercuts first low)
        if second_low_price > first_low_price * 1.03:
            return None
        # Second low must not be too deep
        if second_low_price < left_price * (1 - self.base_depth):
            return None
        # Second low below 90% of left price (some depth required)
        if second_low_price > left_price * 0.90:
            return None

        # Mid high must be at least 35% of the first leg's recovery
        recovery_min = first_low_price + first_leg_range * 0.35
        if mid_high_price < recovery_min:
            return None
        # Mid high must not exceed left-side high (+ 1% tolerance)
        if mid_high_price > left_price * 1.01:
            return None
        # Mid high must be at least 40% recovered from first low
        if mid_high_price < first_low_price + (left_price - first_low_price) * 0.40:
            return None

        # Leg depth symmetry: no leg should be more than 3× the other
        second_leg_range = mid_high_price - second_low_price
        if second_leg_range <= 0:
            return None
        ratio = max(first_leg_range, second_leg_range) / min(first_leg_range, second_leg_range)
        if ratio > 3.0:
            return None

        # --- Timing conditions ---
        total_bars = second_low_bar - left_bar
        if total_bars < 10 or total_bars > self.base_max_bars:
            return None

        seg1 = first_low_bar - left_bar
        seg2 = mid_high_bar - first_low_bar
        seg3 = second_low_bar - mid_high_bar

        if seg1 < 3 or seg2 < 3 or seg3 < 3:
            return None

        first_half = mid_high_bar - left_bar
        second_half = current_idx - mid_high_bar
        if first_half > 0 and second_half > 0:
            half_ratio = max(first_half, second_half) / min(first_half, second_half)
            if half_ratio > 4.0:
                return None

        # After the second low, price should not exceed mid_high (still forming)
        if second_low_bar < current_idx:
            post_low_high = max(self.raw_high[second_low_bar : current_idx + 1])
            if post_low_high > mid_high_price:
                return None

        score = self._rpv_score(left_bar, current_idx)

        return DoubleBottomResult(
            base=base_info,
            first_high_bar=left_bar,
            first_high_price=left_price,
            first_low_bar=first_low_bar,
            first_low_price=first_low_price,
            mid_high_bar=mid_high_bar,
            mid_high_price=mid_high_price,
            second_low_bar=second_low_bar,
            second_low_price=second_low_price,
            pivot_price=mid_high_price,
            score=score,
        )

    def _upsert_double_bottom(self, db: DoubleBottomResult):
        if self.double_bottoms and self.double_bottoms[-1].base.start_bar == db.base.start_bar:
            self.double_bottoms[-1] = db
        else:
            self.double_bottoms.append(db)

    # ------------------------------------------------------------------
    # Cup curve generation
    # ------------------------------------------------------------------

    def _build_cup_curve(
        self,
        left_idx: int,
        bottom_idx: int,
        right_idx: int,
        left_high: float,
        bottom: float,
        right_high: float,
    ) -> Tuple[List[int], List[float]]:
        """
        Left side: exponential decay from left_high down to bottom.
        Right side: exponential growth from bottom up to right_high.
        """
        x_pts: List[int] = []
        y_pts: List[float] = []

        length_left = bottom_idx - left_idx
        if length_left > 0:
            for i in range(length_left + 1):
                t = i / length_left
                y = bottom + (left_high - bottom) * math.exp(-6 * t)
                x_pts.append(left_idx + i)
                y_pts.append(y)

        length_right = right_idx - bottom_idx
        if length_right > 0:
            exp6 = math.exp(6) - 1
            for i in range(1, length_right + 1):   # skip duplicate bottom point
                t = i / length_right
                y = bottom + (right_high - bottom) * (math.exp(6 * t) - 1) / exp6
                x_pts.append(bottom_idx + i)
                y_pts.append(y)

        return x_pts, y_pts

    # ------------------------------------------------------------------
    # RPV quality score
    # ------------------------------------------------------------------

    def _rpv_score(self, start_bar: int, end_bar: int) -> float:
        """
        Ratio of mean positive RPV to mean absolute negative RPV.
        > 1.0 indicates more buying pressure than selling.
        Incorporates the R script's alpha/beta momentum metric.
        """
        window = self.rpv[start_bar : end_bar + 1]
        up_rpv = [v for v in window if v > 0]
        dn_rpv = [abs(v) for v in window if v <= 0]
        mean_up = sum(up_rpv) / len(up_rpv) if up_rpv else 0.0
        mean_dn = sum(dn_rpv) / len(dn_rpv) if dn_rpv else 1.0
        return mean_up / mean_dn if mean_dn > 0 else 0.0

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_base_info(self) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """Return (start_bar, start_price, low_price) of the active base, or Nones."""
        if self._in_base:
            return self._base_start_bar, self._base_start_price, self._base_low_price
        return None, None, None

    def get_cup_curve(self) -> Tuple[Optional[List[int]], Optional[List[float]]]:
        """Return curve x/y of the most recently detected cup."""
        if self.cups:
            c = self.cups[-1]
            return c.curve_x, c.curve_y
        return None, None

    def get_double_bottom_info(self):
        """Return key fields of the most recently detected double bottom."""
        if self.double_bottoms:
            db = self.double_bottoms[-1]
            return (
                db.first_high_bar, db.first_high_price,
                db.mid_high_bar, db.mid_high_price,
                db.second_low_bar, db.second_low_price,
            )
        return None, None, None, None, None, None


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random
    random.seed(42)

    # Simulate a cup-with-handle in weekly bars
    prices = []
    p = 50.0
    # Prior uptrend (30 weeks)
    for _ in range(30):
        p *= random.uniform(1.005, 1.025)
        prices.append(p)
    peak = p
    # Left side of cup: decline ~30% over 8 weeks
    for i in range(8):
        p *= random.uniform(0.955, 0.975)
        prices.append(p)
    # Bottom: 4 weeks
    for _ in range(4):
        p *= random.uniform(0.98, 1.005)
        prices.append(p)
    # Right side: recover over 8 weeks
    for i in range(8):
        p *= random.uniform(1.010, 1.030)
        prices.append(p)
    # Handle: drift down ~5% over 3 weeks with low volume
    handle_high = p
    for _ in range(3):
        p *= random.uniform(0.975, 0.992)
        prices.append(p)

    recognizer = PatternRecognizer(weekly=True)
    for i, close in enumerate(prices):
        high = close * random.uniform(1.005, 1.02)
        low = close * random.uniform(0.98, 0.995)
        vol = random.uniform(800_000, 1_200_000)
        recognizer.process_bar(high=high, low=low, close=close,
                               volume=vol, open_=close * 0.999, idx=i)

    results = recognizer.get_all_patterns()
    print("=== Pattern Recognition Results ===")
    for ptype, items in results.items():
        print(f"\n[{ptype.upper()}] — {len(items)} detected")
        for item in items:
            if hasattr(item, "base"):
                b = item.base
                print(f"  start_bar={b.start_bar}, depth={b.depth_pct:.1%}, "
                      f"length={b.length}w", end="")
            if hasattr(item, "has_handle"):
                print(f", handle={item.has_handle}, score={item.score:.2f}", end="")
            if hasattr(item, "pivot_price") and item.pivot_price:
                print(f", pivot_price={item.pivot_price:.2f}", end="")
            print()