"""
pattern_recognition.py
Exact replication of the Pine Script pattern recognition logic (pivots, base, cup, double bottom).
"""

import numpy as np


class PatternRecognizer:
    """
    Replicates the exact logic from the provided Pine Script:
    - Pivot high/low detection (length 9)
    - Base detection (consolidation 6-65 weeks, depth ≤50%)
    - Cup detection (exponential curve + close distribution)
    - Double Bottom detection (W pattern)
    """
    def __init__(self, base_depth=0.50, base_length=65, db_depth=0.50, db_length=65, pivot_length=9):
        self.base_depth = base_depth
        self.base_length = base_length
        self.db_depth = db_depth
        self.db_length = db_length
        self.pivot_length = pivot_length

        # Data storage
        self.high = []
        self.low = []
        self.close = []

        # Pivot arrays
        self.pivot_high_prices = []
        self.pivot_low_prices = []
        self.pivot_high_bars = []
        self.pivot_low_bars = []
        self.low_of_pivot_high_prices = []

        # Base storage
        self.start_base_price = []
        self.low_base_price = []
        self.low_of_pivot_high = []
        self.start_base_bar = -1
        self.lower_base_bar = -1
        self.base_closes = []
        self.is_base = False
        self.base_count = 0

        # Cup storage
        self.is_cup = False
        self.cup_left_high = None
        self.cup_bottom = None
        self.cup_right_high = None
        self.cup_left_idx = -1
        self.cup_bottom_idx = -1
        self.cup_right_idx = -1
        self.cup_curve_x = None
        self.cup_curve_y = None

        # Double Bottom storage
        self.is_double_bottom = False
        self.db_high_price = None
        self.db_low_price = None
        self.db_top_bar = -1
        self.db_bottom_bar = -1
        self.db_mid_high_bar = -1
        self.db_mid_high_price = None

    def _find_pivots(self, idx):
        if idx < self.pivot_length or idx >= len(self.high) - self.pivot_length:
            return
        h = self.high[idx]
        l = self.low[idx]
        # pivot high
        is_high = True
        for i in range(1, self.pivot_length + 1):
            if h <= self.high[idx - i] or h <= self.high[idx + i]:
                is_high = False
                break
        if is_high:
            self.pivot_high_prices.insert(0, h)
            self.pivot_high_bars.insert(0, idx)
            self.low_of_pivot_high_prices.insert(0, self.low[idx])
        # pivot low
        is_low = True
        for i in range(1, self.pivot_length + 1):
            if l >= self.low[idx - i] or l >= self.low[idx + i]:
                is_low = False
                break
        if is_low:
            self.pivot_low_prices.insert(0, l)
            self.pivot_low_bars.insert(0, idx)

    def _check_base_detection(self, idx):
        if len(self.pivot_high_prices) < 3 or idx < 25:
            return False
        high_25 = self.high[idx - 25]
        bool_highbase = high_25 in self.pivot_high_prices[:3]
        if idx >= 65 + 26:
            highest_65_shifted = max(self.high[idx - 65 - 26 : idx - 26])
        else:
            highest_65_shifted = self.high[0]
        bool_13wk_high = high_25 > highest_65_shifted
        prior_top = self.start_base_price[0] if self.start_base_price else None
        prior_base_bo = (prior_top is not None and high_25 > prior_top)
        bool_higher_piv = bool_13wk_high or prior_base_bo
        lowest_103 = min(self.low[max(0, idx-103):idx+1])
        leg_up_cond = high_25 >= lowest_103 * 1.20
        lowest_25 = min(self.low[idx-25:idx+1])
        first_base_depth = lowest_25 >= high_25 * (1 - self.base_depth)
        highest_25 = max(self.high[idx-25:idx+1])
        no_candle_above = highest_25 <= high_25
        no_base_in_base = not self.is_base
        base_cond = bool_highbase and bool_higher_piv and leg_up_cond and first_base_depth and no_candle_above and no_base_in_base
        if base_cond:
            self.start_base_price.insert(0, high_25)
            self.start_base_bar = idx - 25
            self.low_of_pivot_high.insert(0, self.low[idx-25])
            self.low_base_price.insert(0, lowest_25)
            for i in range(26):
                if idx - i >= 0 and self.low[idx - i] == lowest_25:
                    self.lower_base_bar = idx - i
                    break
            self.base_closes = [self.close[idx - i] for i in range(26) if idx - i >= 0][::-1]
            self.is_base = True
            self.base_count = 0
        return base_cond

    def _update_base(self, idx):
        if not self.is_base:
            return False
        self.base_closes.append(self.close[idx])
        self.base_count = idx - self.start_base_bar
        if self.low[idx] < self.low_base_price[0]:
            self.low_base_price[0] = self.low[idx]
            self.lower_base_bar = idx
        high_start = self.start_base_price[0]
        if self.low[idx] < high_start * (1 - self.base_depth) or self.base_count > self.base_length:
            self.is_base = False
            self.is_cup = False
            self.is_double_bottom = False
            return False

        # Cup detection
        high_cup = high_start
        low_cup = self.low_base_price[0]
        middle_of_cup = low_cup + (high_cup - low_cup) * 0.5
        depth_ok = low_cup >= (1 - self.base_depth) * high_cup and low_cup <= 0.92 * high_cup
        length_ok = self.base_count >= 30 and self.base_count <= self.base_length
        absolute_pos_ok = (high_cup - low_cup) * 0.5 + low_cup <= self.high[idx]
        base_tier = self.base_count // 3
        base_fourth = self.base_count // 4
        cup_form = False
        if base_tier > 0 and base_fourth > 0:
            first_tier_closes = self.base_closes[:base_tier]
            above_mid = sum(1 for c in first_tier_closes if c >= middle_of_cup)
            cond_third_two = above_mid / base_tier >= 0.30
            last_tier_closes = self.base_closes[-base_tier:]
            below_mid = sum(1 for c in last_tier_closes if c <= middle_of_cup)
            cond_third = below_mid / base_tier >= 0.85
            first_q_closes = self.base_closes[:base_fourth]
            above_mid_q = sum(1 for c in first_q_closes if c >= middle_of_cup)
            cond_fourth_two = above_mid_q / base_fourth >= 0.30
            last_half_closes = self.base_closes[-2*base_fourth:]
            below_mid_half = sum(1 for c in last_half_closes if c <= middle_of_cup)
            cond_fourth = below_mid_half / (2*base_fourth) >= 0.85
            cup_form = (cond_third and cond_third_two) or (cond_fourth and cond_fourth_two)
        if depth_ok and length_ok and absolute_pos_ok and cup_form:
            self.is_cup = True
            self.cup_left_high = high_cup
            self.cup_bottom = low_cup
            self.cup_right_high = self.high[idx]
            self.cup_left_idx = self.start_base_bar
            self.cup_bottom_idx = self.lower_base_bar
            self.cup_right_idx = idx
            self._generate_cup_curve(idx)
            return True

        # Double Bottom detection
        if len(self.pivot_high_prices) > 1 and len(self.pivot_low_prices) > 1:
            first_piv_high = self.pivot_high_prices[1]
            second_piv_high = self.pivot_high_prices[0]
            first_piv_low = self.pivot_low_prices[1]
            second_piv_low = self.pivot_low_prices[0]
            prior_low = min(self.low[:self.start_base_bar]) if self.start_base_bar > 0 else self.low[0]
            cond_prior_trend = first_piv_high >= prior_low * 1.20
            cond_prices_a = second_piv_low <= first_piv_low * 1.03
            cond_prices_b = second_piv_low >= (1 - self.db_depth) * first_piv_high
            cond_prices_c = second_piv_low <= first_piv_high * 0.90
            cond_prices_d = second_piv_high >= second_piv_low + (first_piv_high - second_piv_low) * 0.35
            cond_prices_e = second_piv_high < first_piv_high * 1.01
            cond_prices_f = second_piv_high >= first_piv_low + (first_piv_high - first_piv_low) * 0.40
            first_leg_depth = first_piv_high - first_piv_low
            second_leg_depth = second_piv_high - second_piv_low
            cond_prices_g = first_leg_depth > 0 and second_leg_depth > 0 and \
                           max(first_leg_depth, second_leg_depth) / min(first_leg_depth, second_leg_depth) <= 3.0
            first_piv_time = self.pivot_high_bars[1]
            second_piv_time = self.pivot_low_bars[1]
            third_piv_time = self.pivot_high_bars[0]
            fourth_piv_time = self.pivot_low_bars[0]
            cond_time_a = first_piv_time < second_piv_time < third_piv_time < fourth_piv_time
            cond_time_b = fourth_piv_time - first_piv_time <= self.db_length
            cond_time_c = fourth_piv_time - first_piv_time >= 10
            cond_time_d = (second_piv_time - first_piv_time) >= 3 and (third_piv_time - second_piv_time) >= 3 and (fourth_piv_time - third_piv_time) >= 3
            first_half = third_piv_time - first_piv_time
            second_half = idx - third_piv_time
            cond_time_e = first_half > 0 and second_half > 0 and max(first_half, second_half) / min(first_half, second_half) <= 4.0
            cond_time_f = idx - fourth_piv_time <= (third_piv_time - first_piv_time) * 2
            cond_both_a = max(self.high[fourth_piv_time:idx+1]) <= second_piv_high
            if cond_prior_trend and cond_prices_a and cond_prices_b and cond_prices_c and \
               cond_prices_d and cond_prices_e and cond_prices_f and cond_prices_g and \
               cond_time_a and cond_time_b and cond_time_c and cond_time_d and cond_time_e and cond_time_f and cond_both_a:
                self.is_double_bottom = True
                self.db_high_price = first_piv_high
                self.db_low_price = second_piv_low
                self.db_top_bar = first_piv_time
                self.db_bottom_bar = fourth_piv_time
                self.db_mid_high_bar = third_piv_time
                self.db_mid_high_price = second_piv_high
                return True
        return False

    def _generate_cup_curve(self, current_idx):
        left_idx = self.cup_left_idx
        bottom_idx = self.cup_bottom_idx
        right_idx = self.cup_right_idx
        left_high = self.cup_left_high
        bottom = self.cup_bottom
        right_high = self.cup_right_high

        length_left = bottom_idx - left_idx
        length_right = current_idx - bottom_idx

        x_left = []
        y_left = []
        if length_left > 0:
            for i in range(length_left + 1):
                t = i / length_left
                y = bottom + (left_high - bottom) * np.exp(-6 * t)
                x = left_idx + i
                x_left.append(x)
                y_left.append(y)
        x_right = []
        y_right = []
        if length_right > 0:
            for i in range(length_right + 1):
                t = i / length_right
                y = bottom + (right_high - bottom) * (np.exp(6 * t) - 1) / (np.exp(6) - 1)
                x = bottom_idx + i
                x_right.append(x)
                y_right.append(y)
        if x_left and x_right:
            self.cup_curve_x = x_left[:-1] + x_right
            self.cup_curve_y = y_left[:-1] + y_right
        elif x_left:
            self.cup_curve_x = x_left
            self.cup_curve_y = y_left
        else:
            self.cup_curve_x = x_right
            self.cup_curve_y = y_right

    def process_bar(self, high, low, close, idx):
        self.high.append(high)
        self.low.append(low)
        self.close.append(close)
        self._find_pivots(idx)
        self._check_base_detection(idx)
        self._update_base(idx)

    def get_base_info(self):
        if self.is_base and self.start_base_price and self.low_base_price:
            return self.start_base_bar, self.start_base_price[0], self.low_base_price[0]
        return None, None, None

    def get_cup_curve(self):
        if self.is_cup and hasattr(self, 'cup_curve_x') and self.cup_curve_x is not None:
            return self.cup_curve_x, self.cup_curve_y
        return None, None

    def get_double_bottom_info(self):
        if self.is_double_bottom:
            return (self.db_top_bar, self.db_high_price,
                    self.db_mid_high_bar, self.db_mid_high_price,
                    self.db_bottom_bar, self.db_low_price)
        return None, None, None, None, None, None