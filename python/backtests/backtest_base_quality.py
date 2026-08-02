"""
Backtest of Buy Point Strategies against Breakaway Gap.csv ground truth data.

Tests 10 distinct buy point strategies plus combined approaches:
  1. Pivot Breakout       – price crosses above the pivot/buy point
  2. Upside Reversal      – wide-range bar closing in upper half (within base)
  3. Shakeout near Pivot  – undercut swing low then reclaim near pivot area
  4. Volume Dry-Up        – volume < 55% of 20d avg when close to pivot
  5. MA Touch             – price touches EMA10 / EMA20 / SMA50 within base
  6. Pocket Pivot         – up day volume > max down-day volume in last 10 bars
  7. RS New High          – RS makes 1Y/6M/3M new high within base
  8. SMA50 Bounce         – price dips near/below SMA50 then reclaims
  9. Composite Score      – quality-weighted combination of all active signals
 10. Any Signal           – first signal chronologically among strategies 1-8

Also evaluates market context (SPY trend, SMA50/SMA200, market regime) and RS trend
at entry. All strategies include a stop-loss at the base low.

Position sizing: trade returns are scaled by base quality score (0-100), so higher
quality bases receive larger position sizes and vice versa.

Note: VIX regime filtering is planned but not yet implemented — no VIX data is
available in ticker_cache/. When added, it will classify volatility regimes as
low (<15), normal (15-25), elevated (25-35), or high (>35) for risk management.

Market condition filters:
  - uptrend_only: only take signals when SPY > SMA50 AND SPY > SMA200
  - not_bear:      skip signals when SPY < SMA200 (bear market)
  - all:           no filter (take all signals)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import combinations
import warnings
warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"

MARKET_FILTER_MODE = 'all'

# ── Position sizing configuration ──
# Enable quality-based position sizing: returns are scaled by base_quality_score / 100
# so high-quality bases get larger positions (up to 1.0x) and low-quality get smaller (down to 0.0x)
ENABLE_QUALITY_POSITION_SIZING = True


# ══════════════════════════════════════════════════════════════════════════════
# Core technical indicators
# ══════════════════════════════════════════════════════════════════════════════

def calculate_atr(highs, lows, closes, length=14):
    n = len(closes)
    if n == 0:
        return np.zeros(0)
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr1 = highs - lows
    tr2 = np.abs(highs - prev_close)
    tr3 = np.abs(lows - prev_close)
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    alpha = 1.0 / length
    atr = np.zeros(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    return atr


def calculate_volume_distribution_score(volumes, closes, start_bar, end_bar):
    if end_bar <= start_bar or start_bar < 0:
        return 0.0
    up_vol = 0.0
    down_vol = 0.0
    for i in range(max(1, start_bar), min(end_bar + 1, len(closes))):
        if closes[i] > closes[i - 1]:
            up_vol += volumes[i]
        elif closes[i] < closes[i - 1]:
            down_vol += volumes[i]
    total = up_vol + down_vol
    if total <= 0:
        return 0.0
    return (up_vol - down_vol) / total


def calculate_rs_trend_score(rs_raw, start_bar, end_bar):
    if end_bar - start_bar < 5 or start_bar < 0:
        return 0.0
    rs_slice = rs_raw[start_bar:end_bar + 1]
    n = len(rs_slice)
    if n < 5 or np.any(np.isnan(rs_slice)):
        return 0.0
    x = np.arange(n, dtype=float)
    xm = x - x.mean()
    rs_mean = rs_slice.mean()
    denominator = (xm * xm).sum()
    if denominator <= 0 or rs_mean <= 0:
        return 0.0
    slope = (xm * (rs_slice - rs_mean)).sum() / denominator
    normalized_slope = slope / rs_mean * 100.0
    return max(-1.0, min(1.0, normalized_slope))


def detect_volume_climax(volumes, closes, sma20_vol, bar, threshold=2.0):
    if bar < 1 or bar >= len(closes) or bar >= len(volumes):
        return False
    is_down_day = closes[bar] < closes[bar - 1]
    high_volume = volumes[bar] > sma20_vol[bar] * threshold if sma20_vol[bar] > 0 else False
    return is_down_day and high_volume


def calculate_base_depth_score(bDepPct):
    if bDepPct is None:
        return 7.5
    if 15.0 <= bDepPct <= 35.0:
        return 15.0
    elif 10.0 <= bDepPct <= 45.0:
        return 10.0
    elif 5.0 <= bDepPct <= 55.0:
        return 5.0
    else:
        return 0.0


def calculate_base_duration_score(bCount):
    if 20 <= bCount <= 150:
        return 10.0
    elif 15 <= bCount <= 200:
        return 7.0
    elif 10 <= bCount <= 250:
        return 4.0
    else:
        return 0.0


def calculate_base_quality_score(vol_dist_score, rs_trend_score, bDepPct, bCount,
                                  volume_climax_count, price_above_support_pct, vcp_ready=False):
    score = 0.0
    score += (vol_dist_score + 1.0) / 2.0 * 25.0
    score += (rs_trend_score + 1.0) / 2.0 * 25.0
    score += calculate_base_depth_score(bDepPct)
    score += calculate_base_duration_score(bCount)
    if volume_climax_count == 0:
        climax_score = 15.0
    elif volume_climax_count <= 2:
        climax_score = 12.0
    elif volume_climax_count <= 4:
        climax_score = 8.0
    elif volume_climax_count <= 6:
        climax_score = 4.0
    else:
        climax_score = 0.0
    score += climax_score
    score += price_above_support_pct * 10.0
    if vcp_ready:
        score += 5.0
    return min(100.0, max(0.0, score))


def calculate_pattern_failure_risk(vol_dist_score, rs_trend_score, volume_climax_count,
                                   price_above_support_pct, rs_decline_streak):
    risk = 10.0
    if vol_dist_score < -0.3:
        risk += 30.0
    elif vol_dist_score < -0.1:
        risk += 15.0
    if rs_trend_score < -0.3:
        risk += 30.0
    elif rs_trend_score < -0.1:
        risk += 15.0
    if volume_climax_count > 5:
        risk += 20.0
    elif volume_climax_count > 3:
        risk += 10.0
    if price_above_support_pct < 0.8:
        risk += 20.0
    elif price_above_support_pct < 0.9:
        risk += 10.0
    if rs_decline_streak > 15:
        risk += 15.0
    elif rs_decline_streak > 10:
        risk += 8.0
    return min(100.0, max(0.0, risk))


def find_pivots(highs, lows, left=5, right=5):
    n = len(highs)
    pivot_highs = {}
    pivot_lows = {}
    for i in range(left, n - right):
        h, l = highs[i], lows[i]
        is_ph = all(highs[j] < h for j in range(i - left, i + right + 1) if j != i)
        if is_ph:
            pivot_highs[i] = h
        is_pl = all(lows[j] > l for j in range(i - left, i + right + 1) if j != i)
        if is_pl:
            pivot_lows[i] = l
    return pivot_highs, pivot_lows


# ══════════════════════════════════════════════════════════════════════════════
# Market data loader
# ══════════════════════════════════════════════════════════════════════════════

def load_spy_data():
    """Load SPY data for RS calculation and market trend."""
    for fname in ['SPY_1d.parquet', 'SPY_250d.parquet']:
        spy_path = TICKER_CACHE_DIR / fname
        if spy_path.exists():
            try:
                return pd.read_parquet(spy_path)
            except Exception:
                pass
    return None


def load_market_caps():
    """Load market cap data from IBD marketsurge.csv (Market Cap in millions).
    Returns dict: ticker -> market_cap_millions."""
    mcap = {}
    ms_path = ROOT_DIR / "IBD" / "marketsurge.csv"
    if ms_path.exists():
        try:
            df = pd.read_csv(ms_path, encoding='utf-8-sig',
                             usecols=['Symbol', 'Market Cap (mil)'])
            for _, row in df.iterrows():
                t = str(row['Symbol']).strip()
                v = row['Market Cap (mil)']
                if pd.notna(v) and v > 0:
                    mcap[t] = float(v)
        except Exception:
            pass
    return mcap


def classify_market_cap(mcap_mil):
    """Classify market cap tier.
    Returns (tier: str, weight: float). Weight is for equal-volatility scaling.
    Mega  (>200B): 'mega',   0.6x (lower vol)
    Large (10-200B): 'large', 1.0x (baseline)
    Mid   (2-10B):  'mid',   1.0x
    Small (<2B):    'small', 0.7x (higher vol, reduce size)
    """
    if mcap_mil is None or mcap_mil <= 0:
        return 'unknown', 0.75
    if mcap_mil >= 200000:
        return 'mega', 0.6
    elif mcap_mil >= 10000:
        return 'large', 1.0
    elif mcap_mil >= 2000:
        return 'mid', 1.0
    else:
        return 'small', 0.7


# ══════════════════════════════════════════════════════════════════════════════
# Buy signal detection functions (strategies 1-8)
# Each returns (signal_bar, entry_price) or (None, None)
# ══════════════════════════════════════════════════════════════════════════════

def detect_pivot_breakout(highs, pivot_price, search_start, search_end):
    for i in range(search_start, search_end + 1):
        if i < len(highs) and highs[i] > pivot_price:
            return i, pivot_price
    return None, None


def detect_upside_reversal(highs, lows, closes, opens, atr,
                           search_start, search_end, pivot_price, bLow):
    for i in range(search_start, search_end + 1):
        if i < 1 or i >= len(closes) or i >= len(atr):
            continue
        bar_range = highs[i] - lows[i]
        if bar_range <= 0 or atr[i] <= 0 or bar_range < atr[i] * 0.8:
            continue
        midpoint = (highs[i] + lows[i]) / 2.0
        if closes[i] <= midpoint or closes[i] < opens[i]:
            continue
        if closes[i] < pivot_price * 0.85 or closes[i] > pivot_price or closes[i] < bLow:
            continue
        return i, closes[i]
    return None, None


def detect_shakeout_near_pivot(highs, lows, closes, pivot_price, bLow,
                                search_start, search_end):
    ema3 = pd.Series(closes).ewm(span=3, adjust=False).mean().values
    swing_lows = []
    for i in range(max(5, search_start - 10), search_end + 1):
        if i < 5 or i >= len(lows) - 5:
            continue
        is_swing_low = all(lows[j] > lows[i] for j in range(i - 3, i + 4) if j != i and j < len(lows))
        if is_swing_low:
            swing_lows.append((i, lows[i]))
    for sl_bar, sl_price in swing_lows:
        for i in range(sl_bar + 1, min(sl_bar + 6, search_end + 1, len(lows))):
            if lows[i] < sl_price:
                for j in range(i, min(i + 4, search_end + 1, len(closes))):
                    if closes[j] > ema3[j] and ema3[j] > 0:
                        if pivot_price * 0.85 <= closes[j] <= pivot_price:
                            return j, closes[j]
                break
    return None, None


def detect_volume_dryup_near_pivot(volumes, sma20_vol, closes,
                                    pivot_price, bLow, search_start, search_end):
    for i in range(search_start, search_end + 1):
        if i >= len(volumes) or i >= len(sma20_vol) or sma20_vol[i] <= 0:
            continue
        if volumes[i] >= sma20_vol[i] * 0.55:
            continue
        if closes[i] < pivot_price * 0.95 or closes[i] > pivot_price * 1.01 or closes[i] < bLow:
            continue
        return i, closes[i]
    return None, None


def detect_ma_touch(highs, lows, closes, ema10, ema20, sma50,
                    pivot_price, bLow, search_start, search_end):
    for i in range(search_start, search_end + 1):
        if i >= len(closes):
            continue
        if closes[i] < pivot_price * 0.80 or closes[i] > pivot_price or closes[i] < bLow:
            continue
        touched = any([
            ema10[i] > 0 and lows[i] <= ema10[i] * 1.025 and highs[i] >= ema10[i] * 0.975,
            ema20[i] > 0 and lows[i] <= ema20[i] * 1.025 and highs[i] >= ema20[i] * 0.975,
            not np.isnan(sma50[i]) and sma50[i] > 0 and lows[i] <= sma50[i] * 1.025 and highs[i] >= sma50[i] * 0.975,
        ])
        if touched:
            return i, closes[i]
    return None, None


# ── New Strategy 6: Pocket Pivot ─────────────────────────────────────────────

def detect_pocket_pivot(volumes, closes, sma20_vol, search_start, search_end,
                         pivot_price, bLow):
    """Buy when volume on an up day exceeds the maximum down-day volume in
    the last 10 bars (Pocket Pivot by Gil Morales/Chris Kacher).

    Conditions:
      - Close > previous close (up day)
      - Volume > max(down_day_volume) over last 10 bars
      - Volume > 20-day average volume (confirmation)
      - Near pivot area: within 10% below pivot, above base low
    """
    for i in range(search_start, search_end + 1):
        if i < 10 or i >= len(closes) or i >= len(volumes):
            continue
        # Up day
        if closes[i] <= closes[i - 1]:
            continue
        # Volume check
        if i >= len(sma20_vol) or sma20_vol[i] <= 0:
            continue
        if volumes[i] <= sma20_vol[i]:
            continue
        # Max down-day volume in last 10 bars
        max_dn_vol = 0.0
        for j in range(i - 10, i):
            if closes[j] < closes[j - 1] if j > 0 else False:
                max_dn_vol = max(max_dn_vol, volumes[j])
        if max_dn_vol <= 0 or volumes[i] <= max_dn_vol:
            continue
        # Near pivot area: within 10% below
        if closes[i] < pivot_price * 0.90 or closes[i] > pivot_price * 1.01:
            continue
        if closes[i] < bLow:
            continue
        return i, closes[i]
    return None, None


# ── New Strategy 7: RS New High ──────────────────────────────────────────────

def detect_rs_new_high(rs_raw, closes, search_start, search_end,
                        pivot_price, bLow):
    """Buy when RS makes a 1-year, 6-month, or 3-month new high within the base.

    RS new high means current RS exceeds the maximum RS over the lookback period.
    Must be near pivot area.
    """
    n_rs = len(rs_raw)
    for i in range(search_start, search_end + 1):
        if i < 63 or i >= n_rs:
            continue
        # RS 1Y new high (252 bars)
        nh_1y = rs_raw[i] > np.max(rs_raw[max(0, i - 252):i])
        # RS 6M new high (126 bars)
        nh_6m = rs_raw[i] > np.max(rs_raw[max(0, i - 126):i])
        # RS 3M new high (63 bars)
        nh_3m = rs_raw[i] > np.max(rs_raw[max(0, i - 63):i])
        if not (nh_1y or nh_6m or nh_3m):
            continue
        # Near pivot area: within 15% below pivot
        if closes[i] < pivot_price * 0.85 or closes[i] > pivot_price * 1.01:
            continue
        if closes[i] < bLow:
            continue
        return i, closes[i]
    return None, None


# ── New Strategy 8: SMA50 Bounce ─────────────────────────────────────────────

def detect_sma50_bounce(highs, lows, closes, opens, sma50, search_start, search_end,
                          pivot_price, bLow):
    """Buy when price dips near or below SMA50 then reclaims back above it.

    Conditions:
      - Previous bar's low was at or below SMA50 (dip/test)
      - Current bar closes above SMA50 (reclaim/bounce)
      - Near pivot area: within 20% below pivot
    """
    for i in range(search_start, search_end + 1):
        if i < 2 or i >= len(closes) or i >= len(sma50):
            continue
        if np.isnan(sma50[i]) or sma50[i] <= 0:
            continue
        # Previous bar tested SMA50 (low <= SMA50 * 1.02)
        prev_tested = (lows[i - 1] <= sma50[i - 1] * 1.02
                       if not np.isnan(sma50[i - 1]) and sma50[i - 1] > 0 else False)
        # Current bar reclaims above SMA50
        if not prev_tested or closes[i] <= sma50[i]:
            continue
        # Up day preferred but not required (if opens data available)
        if i < len(opens) and closes[i] < opens[i]:
            continue
        # Near pivot area: within 20% below
        if closes[i] < pivot_price * 0.80 or closes[i] > pivot_price:
            continue
        if closes[i] < bLow:
            continue
        return i, closes[i]
    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# Composite score: weights multiple active signals into a single quality metric
# ══════════════════════════════════════════════════════════════════════════════

def compute_composite_buy_score(signals, base_quality_score):
    """Compute a composite buy score (0-100) combining active signals.

    Each active signal contributes points. The base quality score provides
    the foundation weight. Higher score = stronger buy conviction.

    Weighting:
      - Base quality score:             0-30 points (scaled from 0-100)
      - Pivot Breakout:                 +15 points (the gold standard)
      - Pocket Pivot:                   +15 points (strong accumulation)
      - Shakeout:                       +15 points (professional shakeout)
      - RS New High:                    +15 points (momentum confirmation)
      - Upside Reversal:                +10 points
      - SMA50 Bounce:                   +10 points (institutional support)
      - Volume Dry-Up:                   +8 points (constructive)
      - MA Touch:                        +5 points (weakest signal alone)
    """
    score = base_quality_score * 0.30  # Foundation: up to 30 pts

    signal_weights = {
        'Pivot Breakout': 15,
        'Pocket Pivot': 15,
        'Shakeout': 15,
        'RS New High': 15,
        'Upside Reversal': 10,
        'SMA50 Bounce': 10,
        'Volume Dry-Up': 8,
        'MA Touch': 5,
    }

    for sig_name in signal_weights:
        if sig_name in signals:
            score += signal_weights[sig_name]

    return min(100.0, score)


# ══════════════════════════════════════════════════════════════════════════════
# Buy signal orchestrator
# ══════════════════════════════════════════════════════════════════════════════

def detect_all_buy_signals(highs, lows, closes, opens, volumes,
                            ema10, ema20, sma50, sma20_vol, atr,
                            pivot_price, bLow, search_start, search_end,
                            rs_raw=None):
    signals = {}

    # 1. Pivot Breakout
    bar, price = detect_pivot_breakout(highs, pivot_price, search_start, search_end)
    if bar is not None:
        signals['Pivot Breakout'] = (bar, price)

    # 2. Upside Reversal
    bar, price = detect_upside_reversal(highs, lows, closes, opens, atr,
                                         search_start, search_end, pivot_price, bLow)
    if bar is not None:
        signals['Upside Reversal'] = (bar, price)

    # 3. Shakeout
    bar, price = detect_shakeout_near_pivot(highs, lows, closes, pivot_price, bLow,
                                              search_start, search_end)
    if bar is not None:
        signals['Shakeout'] = (bar, price)

    # 4. Volume Dry-Up
    bar, price = detect_volume_dryup_near_pivot(volumes, sma20_vol, closes,
                                                  pivot_price, bLow,
                                                  search_start, search_end)
    if bar is not None:
        signals['Volume Dry-Up'] = (bar, price)

    # 5. MA Touch
    bar, price = detect_ma_touch(highs, lows, closes, ema10, ema20, sma50,
                                  pivot_price, bLow, search_start, search_end)
    if bar is not None:
        signals['MA Touch'] = (bar, price)

    # 6. Pocket Pivot
    bar, price = detect_pocket_pivot(volumes, closes, sma20_vol,
                                      search_start, search_end,
                                      pivot_price, bLow)
    if bar is not None:
        signals['Pocket Pivot'] = (bar, price)

    # 7. RS New High
    if rs_raw is not None:
        bar, price = detect_rs_new_high(rs_raw, closes, search_start, search_end,
                                         pivot_price, bLow)
        if bar is not None:
            signals['RS New High'] = (bar, price)

    # 8. SMA50 Bounce
    bar, price = detect_sma50_bounce(highs, lows, closes, opens, sma50,
                                       search_start, search_end,
                                       pivot_price, bLow)
    if bar is not None:
        signals['SMA50 Bounce'] = (bar, price)

    # 9. Any Signal: first signal chronologically (among 1-8)
    if signals:
        first_strategy = min(signals.keys(), key=lambda s: signals[s][0])
        signals['Any Signal'] = signals[first_strategy]

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# Market context
# ══════════════════════════════════════════════════════════════════════════════

def calculate_market_context(spy_df, signal_bar, df_dates):
    context = {
        'spy_trend': 'unknown', 'market_regime': 'unknown',
        'spy_above_sma50': None, 'spy_above_sma200': None,
        'spy_pct_from_sma50': None, 'spy_pct_from_sma200': None,
        'spy_ret_1m': None, 'spy_ret_3m': None,
        'spy_slope_20d': None, 'spy_slope_50d': None,
    }
    if spy_df is None:
        return context
    try:
        signal_date = df_dates[signal_bar] if signal_bar < len(df_dates) else None
        if signal_date is None:
            return context
        spy_dates = [str(d)[:10] for d in spy_df.index]
        spy_bar = next((i for i, d in enumerate(spy_dates) if d == signal_date), None)
        if spy_bar is None:
            spy_bar = next((i for i, d in enumerate(spy_dates) if d <= signal_date), None)
        if spy_bar is None or spy_bar < 50:
            return context

        spy_closes = spy_df['Close'].values
        spy_sma50_arr = pd.Series(spy_closes).rolling(50, min_periods=20).mean().values
        spy_sma200_arr = pd.Series(spy_closes).rolling(200, min_periods=50).mean().values

        sma50_v = spy_sma50_arr[spy_bar]
        sma200_v = spy_sma200_arr[spy_bar]

        if sma50_v > 0:
            context['spy_pct_from_sma50'] = (spy_closes[spy_bar] - sma50_v) / sma50_v * 100.0
            context['spy_above_sma50'] = spy_closes[spy_bar] > sma50_v
        if not np.isnan(sma200_v) and sma200_v > 0:
            context['spy_pct_from_sma200'] = (spy_closes[spy_bar] - sma200_v) / sma200_v * 100.0
            context['spy_above_sma200'] = spy_closes[spy_bar] > sma200_v

        lb1m = min(21, spy_bar)
        if lb1m > 0:
            context['spy_ret_1m'] = (spy_closes[spy_bar] - spy_closes[spy_bar - lb1m]) / spy_closes[spy_bar - lb1m] * 100.0
        lb3m = min(63, spy_bar)
        if lb3m > 0:
            context['spy_ret_3m'] = (spy_closes[spy_bar] - spy_closes[spy_bar - lb3m]) / spy_closes[spy_bar - lb3m] * 100.0

        for w, k in [(20, 'spy_slope_20d'), (50, 'spy_slope_50d')]:
            if spy_bar >= w:
                sl = spy_closes[spy_bar - w:spy_bar + 1]
                x = np.arange(len(sl), dtype=float)
                xm = x - x.mean()
                d = (xm * xm).sum()
                if d > 0 and sl.mean() > 0:
                    context[k] = ((xm * (sl - sl.mean())).sum() / d) / sl.mean() * 100.0

        a50 = context['spy_above_sma50']
        a200 = context['spy_above_sma200']
        s20 = context['spy_slope_20d']

        if a50 and a200 and s20 is not None and s20 > 0.02:
            regime = 'strong_bull'
        elif a50 and a200:
            regime = 'bull'
        elif a200 and not a50:
            regime = 'neutral'
        elif a200 is False and a50 is False:
            regime = 'strong_bear' if (s20 is not None and s20 < -0.02) else 'bear'
        elif a200 is False:
            regime = 'bear'
        elif a200 is True and a50 is None:
            regime = 'bull'
        elif s20 is not None and s20 > 0:
            regime = 'bull'
        elif s20 is not None and s20 < 0:
            regime = 'bear'
        else:
            regime = 'unknown'

        context['market_regime'] = context['spy_trend'] = regime
    except Exception:
        pass
    return context


# ══════════════════════════════════════════════════════════════════════════════
# Market filter
# ══════════════════════════════════════════════════════════════════════════════

def market_filter_pass(trade, mode='all'):
    if mode == 'all':
        return True, 'all_allowed'
    spy_trend = trade.get('market_regime', trade.get('spy_trend', 'unknown'))
    a50 = trade.get('spy_above_sma50')
    a200 = trade.get('spy_above_sma200')
    if spy_trend == 'unknown' or a50 is None or a200 is None:
        return True, 'no_market_data'
    if mode == 'uptrend_only':
        if a50 and a200:
            return True, 'uptrend_confirmed'
        elif a50 and not a200:
            return False, 'below_sma200'
        elif not a50 and a200:
            return False, 'below_sma50'
        else:
            return False, 'below_both_MAs'
    if mode == 'not_bear':
        return (True, 'above_sma200') if a200 else (False, 'bear_market')
    return True, 'all_allowed'


# ══════════════════════════════════════════════════════════════════════════════
# Forward returns with position sizing
# ══════════════════════════════════════════════════════════════════════════════

def calculate_position_size(base_quality_score):
    """Convert base quality score (0-100) to a position size multiplier (0.25-1.0).

    Excellent (80+): 1.0x    — full position
    Good (60-79):   0.75x   — 3/4 position
    Average (40-59): 0.50x  — half position
    Poor (20-39):   0.35x   — starter/test position
    Very Poor (<20): 0.25x  — minimal
    """
    if base_quality_score >= 80:
        return 1.0
    elif base_quality_score >= 60:
        return 0.75
    elif base_quality_score >= 40:
        return 0.50
    elif base_quality_score >= 20:
        return 0.35
    else:
        return 0.25


def calculate_forward_returns(highs, lows, closes, signal_bar, entry_price,
                               stop_loss, base_quality_score=50, max_bars=60):
    result = {
        'ret_5d': None, 'ret_10d': None, 'ret_20d': None, 'ret_60d': None,
        'max_gain': 0.0, 'max_dd': 0.0,
        'stopped_out': False, 'stop_bar': None,
        'win_5d': False, 'win_10d': False, 'win_20d': False, 'win_60d': False,
    }

    n = len(closes)
    holding_start = signal_bar + 1
    if holding_start >= n:
        return result

    highest_high = entry_price
    lowest_low = entry_price

    for bar in range(holding_start, min(signal_bar + max_bars + 1, n)):
        highest_high = max(highest_high, highs[bar])
        lowest_low = min(lowest_low, lows[bar])

        if lows[bar] <= stop_loss:
            result['stopped_out'] = True
            result['stop_bar'] = bar
            stop_ret = (lowest_low - entry_price) / entry_price * 100.0
            result['ret_5d'] = stop_ret if result['ret_5d'] is None else result['ret_5d']
            result['ret_10d'] = stop_ret if result['ret_10d'] is None else result['ret_10d']
            result['ret_20d'] = stop_ret if result['ret_20d'] is None else result['ret_20d']
            result['ret_60d'] = stop_ret if result['ret_60d'] is None else result['ret_60d']
            break

        bars_held = bar - signal_bar
        ret = (closes[bar] - entry_price) / entry_price * 100.0
        for h, k_r, k_w in [(5, 'ret_5d', 'win_5d'), (10, 'ret_10d', 'win_10d'),
                             (20, 'ret_20d', 'win_20d'), (60, 'ret_60d', 'win_60d')]:
            if bars_held == h and result[k_r] is None:
                result[k_r] = ret
                result[k_w] = ret > 0

    if not result['stopped_out']:
        final_bar = min(signal_bar + max_bars, n - 1)
        final_ret = (closes[final_bar] - entry_price) / entry_price * 100.0
        for k_r, k_w in [('ret_5d', 'win_5d'), ('ret_10d', 'win_10d'),
                          ('ret_20d', 'win_20d'), ('ret_60d', 'win_60d')]:
            if result[k_r] is None:
                result[k_r] = final_ret
                result[k_w] = final_ret > 0

    result['max_gain'] = (highest_high - entry_price) / entry_price * 100.0
    result['max_dd'] = (lowest_low - entry_price) / entry_price * 100.0

    # ── Position sizing: scale returns by quality-based size ──
    pos_size = calculate_position_size(base_quality_score)
    for k in ['ret_5d', 'ret_10d', 'ret_20d', 'ret_60d', 'max_gain', 'max_dd']:
        if result[k] is not None:
            result[k] = result[k] * pos_size

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Main analysis: analyze a single ticker
# ══════════════════════════════════════════════════════════════════════════════

def analyze_ticker(ticker, event_date, ground_truth_depth, ground_truth_length,
                   ground_truth_pattern, pivot_price, spy_df, mcap_mil=None):
    file_path = TICKER_CACHE_DIR / f"{ticker}_1d.parquet"
    if not file_path.exists():
        return None
    try:
        df = pd.read_parquet(file_path)
        if df.empty or len(df) < 60:
            return None
        df = df.sort_index()
        dates = [str(d)[:10] for d in df.index]
        event_str = event_date.strftime('%Y-%m-%d') if hasattr(event_date, 'strftime') else str(event_date)

        event_bar = next((i for i, d in enumerate(dates) if d == event_str), None)
        if event_bar is None:
            event_bar = next((i for i, d in enumerate(dates) if d <= event_str), None)
        if event_bar is None or event_bar < 100:
            return None

        lookahead = 65
        end_idx = min(event_bar + lookahead, len(df))
        highs = df['High'].values[:end_idx]
        lows = df['Low'].values[:end_idx]
        closes = df['Close'].values[:end_idx]
        volumes = df['Volume'].values[:end_idx]
        opens = df['Open'].values[:end_idx]

        bDepPct = ground_truth_depth
        bCount = ground_truth_length
        base_start = max(0, event_bar - bCount)

        # Indicators (computed up to event_bar for accuracy, then padded forward)
        cs = pd.Series(closes[:event_bar + 1])
        ema10 = cs.ewm(span=10, adjust=False).mean().values
        ema20 = cs.ewm(span=20, adjust=False).mean().values
        sma50 = cs.rolling(50, min_periods=20).mean().values

        def _pad(arr, length):
            out = np.zeros(length)
            out[:len(arr)] = arr
            if len(arr) > 0:
                out[len(arr):] = arr[-1]
            return out

        ema10_full = _pad(ema10, end_idx)
        ema20_full = _pad(ema20, end_idx)
        sma50_full = _pad(sma50, end_idx)

        sma20_vol = pd.Series(volumes).rolling(20, min_periods=5).mean().values
        atr = calculate_atr(highs, lows, closes, 14)

        # RS calculation vs SPY
        rs_raw_full = closes.copy()
        if spy_df is not None:
            try:
                aligned_spy = spy_df['Close'].reindex(df.index).ffill().bfill().values
                if len(aligned_spy) > event_bar and np.all(aligned_spy[max(0, base_start):event_bar + 1] > 0):
                    rs_raw_full = closes * 7.0 * 1000.0 / aligned_spy
            except Exception:
                pass

        rs_raw = rs_raw_full.copy()

        event_lows = lows[base_start:event_bar + 1]
        bLow = np.min(event_lows) if len(event_lows) > 0 else pivot_price * 0.7

        # Base quality metrics
        vol_dist_score = calculate_volume_distribution_score(volumes, closes, base_start, event_bar)
        rs_trend_score = calculate_rs_trend_score(rs_raw, base_start, event_bar)
        price_above_support_pct = np.sum(closes[base_start:event_bar + 1] >= bLow) / bCount if bCount > 0 else 1.0

        vol_climax_count = sum(1 for i in range(base_start + 1, event_bar + 1)
                               if detect_volume_climax(volumes, closes, sma20_vol, i))

        rs_decline_streak = 0
        streak = 0
        for i in range(max(1, base_start), event_bar + 1):
            streak = streak + 1 if rs_raw[i] < rs_raw[i - 1] else 0
            rs_decline_streak = max(rs_decline_streak, streak)

        base_quality_score = calculate_base_quality_score(
            vol_dist_score, rs_trend_score, bDepPct, bCount,
            vol_climax_count, price_above_support_pct)
        pattern_failure_risk = calculate_pattern_failure_risk(
            vol_dist_score, rs_trend_score, vol_climax_count,
            price_above_support_pct, rs_decline_streak)

        search_start = base_start
        search_end = min(event_bar + 5, end_idx - 1)

        buy_signals = detect_all_buy_signals(
            highs, lows, closes, opens, volumes,
            ema10_full, ema20_full, sma50_full, sma20_vol, atr,
            pivot_price, bLow, search_start, search_end,
            rs_raw=rs_raw_full)

        # ── Composite Score strategy ──
        composite_score = compute_composite_buy_score(
            {k: v for k, v in buy_signals.items() if k != 'Any Signal'},
            base_quality_score)
        # Composite entry: earliest signal among the real strategies, at its price
        if buy_signals and composite_score >= 30:
            # Pick the strongest signal (highest weighted) as the composite entry
            real_sigs = {k: v for k, v in buy_signals.items() if k != 'Any Signal'}
            if real_sigs:
                best_sig = max(real_sigs.keys(),
                               key=lambda s: (15 if s in ('Pivot Breakout', 'Pocket Pivot',
                                                          'Shakeout', 'RS New High')
                                              else 10 if s in ('Upside Reversal', 'SMA50 Bounce')
                                              else 8 if s == 'Volume Dry-Up' else 5))
                buy_signals['Composite Score'] = real_sigs[best_sig]

        def _build_trade(strategy_name, sig_bar, entry_price):
            sig_rs_trend = calculate_rs_trend_score(rs_raw, max(0, sig_bar - 30), sig_bar)
            mkt_ctx = calculate_market_context(spy_df, sig_bar, dates)
            fwd = calculate_forward_returns(highs, lows, closes, sig_bar, entry_price,
                                             bLow, base_quality_score)
            mcap_tier, mcap_weight = classify_market_cap(mcap_mil)
            trade = {
                'ticker': ticker, 'pattern': ground_truth_pattern,
                'depth': bDepPct, 'length': bCount,
                'pivot_price': pivot_price, 'base_low': bLow,
                'strategy': strategy_name,
                'signal_bar': sig_bar, 'entry_price': entry_price,
                'entry_date': dates[sig_bar] if sig_bar < len(dates) else None,
                'rs_trend_at_entry': sig_rs_trend,
                'spy_trend': mkt_ctx['spy_trend'],
                'spy_above_sma50': mkt_ctx['spy_above_sma50'],
                'spy_above_sma200': mkt_ctx['spy_above_sma200'],
                'market_regime': mkt_ctx['market_regime'],
                'spy_pct_from_sma50': mkt_ctx['spy_pct_from_sma50'],
                'spy_pct_from_sma200': mkt_ctx['spy_pct_from_sma200'],
                'spy_ret_1m': mkt_ctx['spy_ret_1m'],
                'spy_ret_3m': mkt_ctx['spy_ret_3m'],
                'spy_slope_20d': mkt_ctx['spy_slope_20d'],
                'spy_slope_50d': mkt_ctx['spy_slope_50d'],
                'base_quality_score': base_quality_score,
                'pattern_failure_risk': pattern_failure_risk,
                'vol_dist_score': vol_dist_score, 'rs_trend_score': rs_trend_score,
                'composite_buy_score': composite_score,
                'position_size': calculate_position_size(base_quality_score),
                'market_cap_mil': mcap_mil,
                'market_cap_tier': mcap_tier,
                'market_cap_weight': mcap_weight,
                'num_signals': 1,  # default for single strategies
            }
            trade.update(fwd)
            for k in ['ret_5d', 'ret_10d', 'ret_20d', 'ret_60d', 'max_gain', 'max_dd']:
                if k in trade and trade[k] is not None:
                    trade[k] = trade[k] * mcap_weight
            return trade

        trades = []
        for strategy_name, (sig_bar, entry_price) in buy_signals.items():
            trades.append(_build_trade(strategy_name, sig_bar, entry_price))

        # ── Strategy Combinations (pairs, triples) ──
        real_sigs = {k: v for k, v in buy_signals.items()
                     if k not in ('Any Signal', 'Composite Score')}
        sig_names = sorted(real_sigs.keys())

        # Pairs: all 2-strategy combinations
        for combo in combinations(sig_names, 2):
            bars = [real_sigs[s][0] for s in combo]
            prices = [real_sigs[s][1] for s in combo]
            earliest_idx = bars.index(min(bars))
            combo_name = '+'.join(combo)
            trades.append(_build_trade(combo_name, bars[earliest_idx], prices[earliest_idx]))
            trades[-1]['num_signals'] = 2

        # Triples: all 3-strategy combinations
        for combo in combinations(sig_names, 3):
            bars = [real_sigs[s][0] for s in combo]
            prices = [real_sigs[s][1] for s in combo]
            earliest_idx = bars.index(min(bars))
            combo_name = '+'.join(combo)
            trades.append(_build_trade(combo_name, bars[earliest_idx], prices[earliest_idx]))
            trades[-1]['num_signals'] = 3

        return trades
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Strategy table printer
# ══════════════════════════════════════════════════════════════════════════════

def print_strategy_table(df, strategies, label="ALL TRADES"):
    print(f"\n📊 STRATEGY COMPARISON TABLE — {label}")
    print("=" * 120)
    header = (f"{'Strategy':<20} {'Count':>6} {'Win 5d%':>8} {'Win 10d%':>8} "
              f"{'Win 20d%':>8} {'Win 60d%':>8} {'Avg 10d':>8} {'Avg 20d':>8} "
              f"{'Avg 60d':>8} {'Max Gain':>8} {'Max DD':>8} {'W/L':>7}")
    print(header)
    print("-" * 120)

    summary = []
    for s in strategies:
        sdf = df[df['strategy'] == s]
        n = len(sdf)
        if n == 0:
            continue
        w5 = sdf['win_5d'].mean() * 100
        w10 = sdf['win_10d'].mean() * 100
        w20 = sdf['win_20d'].mean() * 100
        w60 = sdf['win_60d'].mean() * 100
        a10 = sdf['ret_10d'].mean()
        a20 = sdf['ret_20d'].mean()
        a60 = sdf['ret_60d'].mean()
        mg = sdf['max_gain'].mean()
        md = sdf['max_dd'].mean()
        wl = mg / abs(md) if abs(md) > 0 else float('inf')

        print(f"{s:<20} {n:>6} {w5:>7.1f}% {w10:>7.1f}% {w20:>7.1f}% {w60:>7.1f}% "
              f"{a10:>8.2f}% {a20:>8.2f}% {a60:>8.2f}% {mg:>8.2f}% {md:>8.2f}% {wl:>6.2f}")

        summary.append({
            'strategy': s, 'count': n,
            'win_5d_pct': round(w5, 1), 'win_10d_pct': round(w10, 1),
            'win_20d_pct': round(w20, 1), 'win_60d_pct': round(w60, 1),
            'avg_ret_10d': round(a10, 2), 'avg_ret_20d': round(a20, 2),
            'avg_ret_60d': round(a60, 2),
            'avg_max_gain': round(mg, 2), 'avg_max_dd': round(md, 2),
            'win_loss_ratio': round(wl, 2),
        })
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# Backtest runner
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest():
    csv_path = ROOT_DIR / "IBD" / "Breakaway Gap.csv"
    if not csv_path.exists():
        print(f"❌ Breakaway Gap.csv not found at {csv_path}")
        return

    spy_df = load_spy_data()
    if spy_df is not None:
        print(f"✅ Loaded SPY data ({len(spy_df)} bars) for market context")
    else:
        print("⚠️  No SPY data found – market context will be unavailable")

    mcap_dict = load_market_caps()

    csv_df = pd.read_csv(csv_path, encoding='utf-8-sig')
    mcap_found = sum(1 for t in csv_df['Symbol'].unique() if t in mcap_dict) if not csv_df.empty else 0
    print(f"✅ Loaded market cap data for {mcap_found} tickers")

    def parse_date(d):
        try:
            d = str(d).strip()
            parts = d.split('/')
            if len(parts) == 3:
                return pd.to_datetime(f"{parts[2]}-{parts[0]}-{parts[1]}")
        except Exception:
            pass
        return None

    csv_df['Parsed_Date'] = csv_df['Event Date'].apply(parse_date)
    csv_df = csv_df.dropna(subset=['Parsed_Date'])
    csv_df = csv_df[csv_df['Daily Base Type'] != 'Ascending Base']

    print(f"\n🔍 Analyzing {len(csv_df)} events across 10 strategies...")
    print(f"   Market filter: {MARKET_FILTER_MODE}")
    print(f"   Position sizing: quality (0.25x–1.0x) × market-cap (0.6x–1.0x)")
    print("=" * 120)

    all_trades = []
    for idx, row in csv_df.iterrows():
        ticker = row['Symbol']
        event_date = row['Parsed_Date']
        depth = row['Depth'] if pd.notna(row['Depth']) else 20
        length = row['Length'] if pd.notna(row['Length']) else 50
        pattern = row['Daily Base Type']
        pivot = row['Pivot Price'] if pd.notna(row['Pivot Price']) else 0

        if isinstance(depth, str):
            try:
                depth = float(depth.split(',')[0])
            except Exception:
                depth = 20
        if pivot <= 0 or pd.isna(pivot):
            continue

        trades = analyze_ticker(ticker, event_date, depth, length, pattern, pivot, spy_df,
                                mcap_mil=mcap_dict.get(ticker))
        if trades:
            all_trades.extend(trades)
        if (idx + 1) % 50 == 0:
            print(f"  Processed {idx + 1}/{len(csv_df)} events, {len(all_trades)} trades found...")

    if not all_trades:
        print("❌ No trades to analyze")
        return

    df = pd.DataFrame(all_trades)
    strategies = ['Pivot Breakout', 'Upside Reversal', 'Shakeout',
                   'Volume Dry-Up', 'MA Touch', 'Pocket Pivot',
                   'RS New High', 'SMA50 Bounce', 'Composite Score', 'Any Signal']

    print(f"\n📊 RESULTS: {len(all_trades)} total trades from {len(csv_df)} events")

    # ── 1. Full strategy comparison ──
    print_strategy_table(df, strategies, "ALL TRADES (position-sized)")

    # ── 2. Position size distribution ──
    print(f"\n{'='*120}")
    print("📊 POSITION SIZE DISTRIBUTION (quality-based)")
    print("=" * 120)
    pos_dist = df.groupby('position_size').agg(
        n=('strategy', 'count'),
        avg_ret_20d=('ret_20d', 'mean'),
        win_rate_20d=('win_20d', 'mean'),
    ).reset_index()
    pos_dist['win_rate_20d'] = pos_dist['win_rate_20d'] * 100
    print(pos_dist.to_string(index=False))

    # ── 3. Market filter ──
    filter_reasons = []
    for _, trade in df.iterrows():
        passed, reason = market_filter_pass(trade, MARKET_FILTER_MODE)
        filter_reasons.append((passed, reason))
    df['filter_passed'] = [r[0] for r in filter_reasons]
    df['filter_reason'] = [r[1] for r in filter_reasons]

    if MARKET_FILTER_MODE != 'all':
        df_f = df[df['filter_passed']].copy()
        df_r = df[~df['filter_passed']].copy()
        print(f"\n{'='*120}")
        print(f"🔍 MARKET FILTER APPLIED: {MARKET_FILTER_MODE}")
        print(f"   Passed: {len(df_f)}/{len(df)} ({len(df_f)/len(df)*100:.1f}%)")
        print(f"   Rejected: {len(df_r)}/{len(df)}")
        for reason, count in df_r['filter_reason'].value_counts().items():
            print(f"     - {reason}: {count}")
        print_strategy_table(df_f, strategies, f"FILTERED ({MARKET_FILTER_MODE})")

    # ── 4. Market regime breakdown ──
    print(f"\n{'='*120}")
    print("📊 PERFORMANCE BY MARKET REGIME (20-day Win Rate)")
    print("=" * 120)
    for regime in [r for r in ['strong_bull', 'bull', 'neutral', 'bear', 'strong_bear', 'unknown']
                   if r in df['market_regime'].unique()]:
        rdf = df[df['market_regime'] == regime]
        if len(rdf) < 3:
            continue
        print(f"\n  ▸ {regime.upper()} ({len(rdf)} trades)")
        for s in strategies[:8]:  # core strategies only
            sdf = rdf[rdf['strategy'] == s]
            if len(sdf) < 3:
                continue
            print(f"    {s:<20}: n={len(sdf):>3}  "
                  f"Win20d={sdf['win_20d'].mean()*100:>5.1f}%  "
                  f"Avg20d={sdf['ret_20d'].mean():>6.2f}%")

    # ── 5. Composite Score effectiveness ──
    print(f"\n{'='*120}")
    print("📊 COMPOSITE BUY SCORE vs RETURNS")
    print("=" * 120)
    for score_range, label in [(80, '80-100 (Excellent)'), (60, '60-79 (Good)'),
                                (40, '40-59 (Average)'), (0, '0-39 (Poor)')]:
        if score_range == 0:
            cdf = df[df['composite_buy_score'] < 40]
        elif score_range == 80:
            cdf = df[df['composite_buy_score'] >= 80]
        else:
            cdf = df[(df['composite_buy_score'] >= score_range) &
                      (df['composite_buy_score'] < score_range + 20)]
        if len(cdf) < 5:
            continue
        print(f"  {label}: n={len(cdf):>4}  "
              f"Win20d={cdf['win_20d'].mean()*100:>5.1f}%  "
              f"Avg20d={cdf['ret_20d'].mean():>6.2f}%  "
              f"Avg MaxGain={cdf['max_gain'].mean():>6.2f}%  "
              f"Avg MaxDD={cdf['max_dd'].mean():>6.2f}%")

    # ── 6. RS Trend Analysis ──
    print(f"\n{'='*120}")
    print("📊 PERFORMANCE BY RS TREND AT ENTRY")
    print("=" * 120)
    for s in strategies[:8]:
        sdf = df[df['strategy'] == s]
        if len(sdf) < 5:
            continue
        imp = sdf[sdf['rs_trend_at_entry'] >= 0]
        dec = sdf[sdf['rs_trend_at_entry'] < 0]
        print(f"\n  {s}:")
        if len(imp) > 0:
            print(f"    RS Improving:  n={len(imp):>3}  "
                  f"Win20d={imp['win_20d'].mean()*100:>5.1f}%  "
                  f"Avg20d={imp['ret_20d'].mean():>6.2f}%")
        if len(dec) > 0:
            print(f"    RS Declining:  n={len(dec):>3}  "
                  f"Win20d={dec['win_20d'].mean()*100:>5.1f}%  "
                  f"Avg20d={dec['ret_20d'].mean():>6.2f}%")

    # ── 7. Stop-Loss ──
    print(f"\n{'='*120}")
    print("📊 STOP-LOSS ANALYSIS")
    print("=" * 120)
    for s in strategies:
        sdf = df[df['strategy'] == s]
        if len(sdf) == 0:
            continue
        stopped = int(sdf['stopped_out'].sum())
        sr = stopped / len(sdf) * 100
        if stopped > 0:
            sdf_s = sdf[sdf['stopped_out'] == 1]
            ab = (sdf_s['stop_bar'] - sdf_s['signal_bar']).mean()
        else:
            ab = 0
        print(f"  {s:<20}: {stopped}/{len(sdf)} stopped ({sr:.1f}%)  Avg bars: {ab:.1f}")

    # ── 8. Combined Bull + RS ──
    print(f"\n{'='*120}")
    print("📊 COMBINED MARKET + RS FILTER (SPY > SMA200 AND RS Improving)")
    print("=" * 120)
    df_br = df[(df['spy_above_sma200'] == True) & (df['rs_trend_at_entry'] >= 0)]
    if len(df_br) > 0:
        print(f"  Trades: {len(df_br)} / {len(df)}")
        print_strategy_table(df_br, strategies, "BULL + RS IMPROVING")
    else:
        print("  No trades matching both conditions")

    # ── 10. Market-Cap Tier Breakdown ──
    print(f"\n{'='*120}")
    print("📊 PERFORMANCE BY MARKET CAP TIER (20-day Win Rate)")
    print("=" * 120)
    for tier in ['mega', 'large', 'mid', 'small', 'unknown']:
        tdf = df[df['market_cap_tier'] == tier]
        if len(tdf) < 5:
            continue
        print(f"\n  ▸ {tier.upper()} CAP ({len(tdf)} trades)")
        for s in strategies[:8]:
            sdf = tdf[tdf['strategy'] == s]
            if len(sdf) < 3:
                continue
            print(f"    {s:<20}: n={len(sdf):>3}  "
                  f"Win20d={sdf['win_20d'].mean()*100:>5.1f}%  "
                  f"Avg20d={sdf['ret_20d'].mean():>6.2f}%")

    # ── 11. Side-by-side Bull/Bear/Sideways Regime Comparison ──
    print(f"\n{'='*120}")
    print("📊 SIDE-BY-SIDE REGIME COMPARISON — BULL vs BEAR vs SIDEWAYS (all strategies)")
    print("=" * 120)
    regime_map = {'strong_bull': 'Bull', 'bull': 'Bull',
                  'neutral': 'Sideways',
                  'bear': 'Bear', 'strong_bear': 'Bear',
                  'unknown': 'Unknown'}
    df['regime_cat'] = df['market_regime'].map(regime_map)

    print(f"\n  {'Strategy':<20} {'Regime':<12} {'Count':>6} {'Win20d':>8} {'Avg20d':>9} {'MaxGain':>9} {'MaxDD':>9} {'W/L':>7}")
    print("  " + "-" * 80)
    for cat in ['Bull', 'Sideways', 'Bear']:
        for s in strategies[:8]:  # all core strategies
            sdf = df[(df['strategy'] == s) & (df['regime_cat'] == cat)]
            if len(sdf) < 3:
                continue
            w20 = sdf['win_20d'].mean() * 100
            a20 = sdf['ret_20d'].mean()
            mg = sdf['max_gain'].mean()
            md = sdf['max_dd'].mean()
            wl = mg / abs(md) if abs(md) > 0 else float('inf')
            print(f"  {s:<20} {cat:<12} {len(sdf):>6} {w20:>7.1f}% {a20:>8.2f}% {mg:>8.2f}% {md:>8.2f}% {wl:>6.2f}")

    # Regime aggregate row
    print("  " + "-" * 80)
    for cat in ['Bull', 'Sideways', 'Bear']:
        cdf = df[df['regime_cat'] == cat]
        if len(cdf) < 5:
            continue
        w20 = cdf['win_20d'].mean() * 100
        a20 = cdf['ret_20d'].mean()
        mg = cdf['max_gain'].mean()
        md = cdf['max_dd'].mean()
        wl = mg / abs(md) if abs(md) > 0 else float('inf')
        print(f"  {'[ALL STRATEGIES]':<20} {cat:<12} {len(cdf):>6} {w20:>7.1f}% {a20:>8.2f}% {mg:>8.2f}% {md:>8.2f}% {wl:>6.2f}")

    # ── 12. Strategy Combinations (Pairs & Triples) ──
    print(f"\n{'='*120}")
    print("📊 STRATEGY COMBINATIONS — TOP PAIRS (by 20-day Win Rate, min 5 trades)")
    print("=" * 120)
    combo_strategies = [s for s in df['strategy'].unique()
                        if '+' in s and s.count('+') == 1]  # pairs only
    combo_stats = []
    for cs in combo_strategies:
        cdf = df[df['strategy'] == cs]
        n = len(cdf)
        if n < 5:
            continue
        combo_stats.append({
            'name': cs, 'count': n,
            'win_20d': cdf['win_20d'].mean() * 100,
            'avg_20d': cdf['ret_20d'].mean(),
            'max_gain': cdf['max_gain'].mean(),
            'max_dd': cdf['max_dd'].mean(),
            'wl': cdf['max_gain'].mean() / abs(cdf['max_dd'].mean()) if abs(cdf['max_dd'].mean()) > 0 else float('inf'),
        })
    combo_stats.sort(key=lambda x: x['win_20d'], reverse=True)

    if combo_stats:
        print(f"\n  {'Rank':<5} {'Combination':<45} {'Count':>6} {'Win20d':>8} {'Avg20d':>9} {'W/L':>7}")
        print("  " + "-" * 75)
        for i, cs in enumerate(combo_stats[:20], 1):
            print(f"  {i:<5} {cs['name']:<45} {cs['count']:>6} {cs['win_20d']:>7.1f}% {cs['avg_20d']:>8.2f}% {cs['wl']:>6.2f}")
    else:
        print("  No pairs with ≥5 trades")

    # Top triples
    triple_strategies = [s for s in df['strategy'].unique()
                         if '+' in s and s.count('+') == 2]
    triple_stats = []
    for cs in triple_strategies:
        cdf = df[df['strategy'] == cs]
        n = len(cdf)
        if n < 3:
            continue
        triple_stats.append({
            'name': cs, 'count': n,
            'win_20d': cdf['win_20d'].mean() * 100,
            'avg_20d': cdf['ret_20d'].mean(),
            'wl': cdf['max_gain'].mean() / abs(cdf['max_dd'].mean()) if abs(cdf['max_dd'].mean()) > 0 else float('inf'),
        })
    triple_stats.sort(key=lambda x: x['win_20d'], reverse=True)

    if triple_stats:
        print(f"\n📊 STRATEGY COMBINATIONS — TOP TRIPLES (by 20-day Win Rate, min 3 trades)")
        print("=" * 120)
        print(f"\n  {'Rank':<5} {'Combination':<55} {'Count':>6} {'Win20d':>8} {'Avg20d':>9} {'W/L':>7}")
        print("  " + "-" * 85)
        for i, cs in enumerate(triple_stats[:15], 1):
            print(f"  {i:<5} {cs['name']:<55} {cs['count']:>6} {cs['win_20d']:>7.1f}% {cs['avg_20d']:>8.2f}% {cs['wl']:>6.2f}")

    # ── 13. Signal Confirmation Summary (1 vs 2 vs 3 signals) ──
    print(f"\n{'='*120}")
    print("📊 SIGNAL CONFIRMATION EFFECT — Win Rate vs Number of Confirming Signals")
    print("=" * 120)
    print(f"\n  {'Signals':<12} {'Trades':>7} {'Win5d':>8} {'Win10d':>8} {'Win20d':>8} {'Win60d':>8} {'Avg10d':>9} {'Avg20d':>9} {'Avg60d':>9} {'MaxDD':>9} {'W/L':>7}")
    print("  " + "-" * 95)
    for n_sig in [1, 2, 3]:
        ndf = df[df['num_signals'] == n_sig]
        if len(ndf) < 3:
            continue
        w5 = ndf['win_5d'].mean() * 100
        w10 = ndf['win_10d'].mean() * 100
        w20 = ndf['win_20d'].mean() * 100
        w60 = ndf['win_60d'].mean() * 100
        a10 = ndf['ret_10d'].mean()
        a20 = ndf['ret_20d'].mean()
        a60 = ndf['ret_60d'].mean()
        md = ndf['max_dd'].mean()
        mg = ndf['max_gain'].mean()
        wl = mg / abs(md) if abs(md) > 0 else float('inf')
        label = {1: '1 (Single)', 2: '2 (Pair)', 3: '3+ (Triple)'}[n_sig]
        print(f"  {label:<12} {len(ndf):>7} {w5:>7.1f}% {w10:>7.1f}% {w20:>7.1f}% {w60:>7.1f}% {a10:>8.2f}% {a20:>8.2f}% {a60:>8.2f}% {md:>8.2f}% {wl:>6.2f}")

    # Save files
    summary_path = ROOT_DIR / "python" / "backtests" / "backtest_strategies_summary.csv"
    summary_rows = []
    for s in strategies:
        sdf = df[df['strategy'] == s]
        n = len(sdf)
        if n == 0:
            continue
        summary_rows.append({
            'strategy': s, 'count': n,
            'win_5d_pct': round(sdf['win_5d'].mean() * 100, 1),
            'win_10d_pct': round(sdf['win_10d'].mean() * 100, 1),
            'win_20d_pct': round(sdf['win_20d'].mean() * 100, 1),
            'win_60d_pct': round(sdf['win_60d'].mean() * 100, 1),
            'avg_ret_10d': round(sdf['ret_10d'].mean(), 2),
            'avg_ret_20d': round(sdf['ret_20d'].mean(), 2),
            'avg_ret_60d': round(sdf['ret_60d'].mean(), 2),
            'avg_max_gain': round(sdf['max_gain'].mean(), 2),
            'avg_max_dd': round(sdf['max_dd'].mean(), 2),
            'win_loss_ratio': round(sdf['max_gain'].mean() / abs(sdf['max_dd'].mean()), 2)
            if abs(sdf['max_dd'].mean()) > 0 else None,
        })
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
    print(f"\n💾 Strategy summary saved to {summary_path}")

    output_path = ROOT_DIR / "python" / "backtests" / "backtest_strategies_results.csv"
    df_save = df.copy()
    for col in ['win_5d', 'win_10d', 'win_20d', 'win_60d', 'stopped_out',
                'filter_passed', 'spy_above_sma50', 'spy_above_sma200']:
        if col in df_save.columns and df_save[col].dtype == bool:
            df_save[col] = df_save[col].astype(int)
    df_save.to_csv(output_path, index=False)
    print(f"💾 Detailed trade log saved to {output_path}")

    # Top/worst
    print(f"\n{'='*120}")
    print("📊 TOP 10 BEST TRADES (by 20-day return)")
    print("=" * 120)
    top10 = df.nlargest(10, 'ret_20d')[
        ['ticker', 'pattern', 'strategy', 'entry_date', 'ret_20d', 'ret_60d',
         'max_gain', 'base_quality_score', 'market_regime']
    ]
    print(top10.to_string(index=False))

    print(f"\n📊 WORST 10 TRADES (by 20-day return)")
    print("=" * 120)
    worst10 = df.nsmallest(10, 'ret_20d')[
        ['ticker', 'pattern', 'strategy', 'entry_date', 'ret_20d', 'ret_60d',
         'max_dd', 'base_quality_score', 'market_regime']
    ]
    print(worst10.to_string(index=False))

    print(f"\n✅ Backtest complete! Filter: {MARKET_FILTER_MODE} | "
          f"Position sizing: {'ON' if ENABLE_QUALITY_POSITION_SIZING else 'OFF'}")


if __name__ == "__main__":
    run_backtest()
