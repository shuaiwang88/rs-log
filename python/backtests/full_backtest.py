#!/usr/bin/env python3
"""
Full Backtest — All Ticker Cache Tickers
========================================
Scans every ticker in ticker_cache/ with filters (price > $12, avg vol > 500K),
runs IBD pattern detection, tests all buy strategies + exit rules, and generates
a comprehensive HTML report.

Buy Strategies (8 core + Composite + Any):
  1. Pivot Breakout       — price crosses above base pivot
  2. Upside Reversal      — wide-range up bar within base
  3. Shakeout near Pivot  — undercut swing low then reclaim
  4. Volume Dry-Up        — volume < 55% of 20d avg near pivot
  5. MA Touch             — touches EMA10/EMA20/SMA50
  6. Pocket Pivot         — up day vol > max down-day vol in 10 bars
  7. RS New High          — RS makes new high within base
  8. SMA50 Bounce         — dips near SMA50 then reclaims

Exit Rules:
  - Stop-Loss: base low
  - Trailing Stop: ATR-based (2x, 3x ATR)
  - Time Stop: exit after 20/40/60 bars
  - Profit Target: R:R of 2:1, 3:1, 5:1

Risk Management:
  - Position sizing: quality-based (0.25x–1.0x)
  - Risk/Reward ratio tracking
  - Max drawdown per trade
"""

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import combinations
import glob
import time
import warnings
warnings.filterwarnings("ignore")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_DIR = Path(__file__).resolve().parent

# ── Filters ──
MIN_PRICE = 12.0
MIN_AVG_VOL_50 = 500_000

# ── Strategy lists ──
BUY_STRATEGIES = [
    'Pivot Breakout', 'Upside Reversal', 'Shakeout', 'Volume Dry-Up',
    'MA Touch', 'Pocket Pivot', 'RS New High', 'SMA50 Bounce'
]
EXIT_RULES = ['stop_loss', 'trail_2atr', 'trail_3atr', 'time_20', 'time_40',
              'time_60', 'target_2r', 'target_3r', 'target_5r']


# ══════════════════════════════════════════════════════════════════════════════
# Technical indicators (reused from backtest_base_quality.py)
# ══════════════════════════════════════════════════════════════════════════════

def calculate_atr(highs, lows, closes, length=14):
    n = len(closes)
    if n == 0:
        return np.zeros(0)
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    alpha = 1.0 / length
    atr = np.zeros(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    return atr


def find_pivots(highs, lows, left=5, right=5):
    n = len(highs)
    ph, pl = {}, {}
    for i in range(left, n - right):
        if all(highs[j] < highs[i] for j in range(i - left, i + right + 1) if j != i):
            ph[i] = highs[i]
        if all(lows[j] > lows[i] for j in range(i - left, i + right + 1) if j != i):
            pl[i] = lows[i]
    return ph, pl


# ══════════════════════════════════════════════════════════════════════════════
# Pattern detection (lightweight — reuses IBD pattern scanner logic)
# ══════════════════════════════════════════════════════════════════════════════

def scan_ticker_for_bases(df, spy_close_series=None):
    """Detect all base patterns in a ticker's history.
    Returns list of base events: {start_bar, end_bar, bTop, bLow, bDepPct, bCount,
                                   pattern_name, break_bar, pivot_price, history_state}
    """
    df = df.sort_index()
    if len(df) > 1500:
        df = df.iloc[-1500:]

    highs = df['High'].values
    lows = df['Low'].values
    closes = df['Close'].values
    volumes = df['Volume'].values
    opens = df['Open'].values
    n = len(df)

    pivLag = 5
    pivLen = 5
    bLenB = 325
    bdF = 0.50

    close_s = pd.Series(closes)
    ema10 = close_s.ewm(span=10, adjust=False).mean().values
    ema20 = close_s.ewm(span=20, adjust=False).mean().values
    sma50 = close_s.rolling(50, min_periods=10).mean().values
    sma20_vol = pd.Series(volumes).rolling(20, min_periods=5).mean().values
    atr14 = calculate_atr(highs, lows, closes, 14)

    # RS
    rs_raw = closes.copy()
    if spy_close_series is not None and not spy_close_series.empty:
        aligned_spy = spy_close_series.reindex(df.index).ffill().bfill().values
        if len(aligned_spy) == n and np.all(aligned_spy > 0):
            rs_raw = closes * 7.0 * 1000.0 / aligned_spy

    rs_s = pd.Series(rs_raw)
    rs_h1y = rs_s.shift(1).rolling(min(252, n), min_periods=30).max().values
    rs_h6m = rs_s.shift(1).rolling(min(126, n), min_periods=20).max().values
    rs_h3m = rs_s.shift(1).rolling(min(63, n), min_periods=10).max().values
    rs_nh_any = (rs_raw > rs_h1y) | (rs_raw > rs_h6m) | (rs_raw > rs_h3m)

    pivot_highs, pivot_lows = find_pivots(highs, lows, pivLen, pivLen)

    aHP_list = []
    aLP_list = []
    bTop = bLow = bStart = None
    isBase = False
    bCount = 0

    # State variables
    shakeLastSwingLow = None
    shakeUndercutBar = None
    shakeReclaimBar = None
    shakeReclaimHigh = None
    shakeSetupActive = False
    shakeEma3 = close_s.ewm(span=3, adjust=False).mean()

    down_vols = np.where(close_s.diff() < 0, volumes, 0.0)

    rsCount = 0
    bases_found = []

    for i in range(n):
        conf_bar = i - pivLen
        if conf_bar in pivot_highs:
            aHP_list.insert(0, (conf_bar, pivot_highs[conf_bar]))
        if conf_bar in pivot_lows:
            aLP_list.insert(0, (conf_bar, pivot_lows[conf_bar]))
            shakeLastSwingLow = pivot_lows[conf_bar]

        w25_start = max(0, i - pivLag + 1)
        H25 = np.max(highs[w25_start:i+1])
        L25 = np.min(lows[w25_start:i+1])

        w103_start = max(0, i - 130 + 1)
        L103 = np.min(lows[w103_start:i+1])

        shift_idx = i - pivLag - 1
        if shift_idx >= 0:
            w65_start = max(0, shift_idx - 65 + 1)
            H65s = np.max(highs[w65_start:shift_idx + 1])
        else:
            H65s = highs[i]

        # New base detection
        newBase = False
        if len(aHP_list) >= 3 and i >= pivLag:
            piv_h = highs[i - pivLag]
            recent_hp_prices = [p for _, p in aHP_list[:3]]
            bH = any(abs(piv_h - p) < 1e-4 for p in recent_hp_prices)
            bPH = (piv_h > H65s) or (bTop is not None and piv_h > bTop)
            lUp = L103 * 1.20 <= piv_h
            dep = piv_h * (1.0 - bdF) <= L25
            noAb = H25 <= piv_h
            newBase = bH and bPH and lUp and dep and noAb

        if newBase and not isBase:
            bTop = highs[i - pivLag]
            bLow = L25
            bStart = i - pivLag
            bCount = pivLag
            isBase = True
        elif not newBase and isBase:
            isBase = True

        if isBase:
            bCount += 1
            if bTop is not None and highs[i] > bTop and highs[i] <= bTop * 1.05:
                bTop = highs[i]
            if bLow is not None and lows[i] < bLow and bTop is not None and lows[i] >= bTop * (1.0 - bdF):
                bLow = lows[i]

        # Invalidation
        if isBase and bTop is not None:
            if lows[i] < bTop * (1.0 - bdF) or bCount > bLenB or closes[i] > bTop * 1.40:
                isBase = False

        # Base depth
        bDepPct = (bTop - bLow) / bTop * 100.0 if (bTop and bLow and bTop > 0) else None

        # Pattern classification (simplified)
        pattern_name = 'Base'
        isCup = isFlat = isDB = isCupH = isConsolidation = False
        cupHandlePivot = None
        dbMiddlePivot = None
        cupMid = bLow + (bTop - bLow) * 0.5 if (bTop and bLow) else None

        # Flat Base
        recent_win = min(i + 1, max(20, min(bCount, 65)))
        end_r = max(0, i)
        rTop = np.max(highs[max(0, end_r - recent_win + 1):end_r + 1])
        rLow = np.min(lows[max(0, end_r - recent_win + 1):end_r + 1])
        rDepPct = (rTop - rLow) / rTop * 100.0 if rTop > 0 else 999
        isFlat = isBase and rDepPct <= 18.0 and bCount >= 20

        # Cup
        if isBase and bTop and bLow and not isFlat and bCount >= 20 and bDepPct is not None:
            if 8.0 <= bDepPct <= 55.0:
                isCup = True

        # Cup+Handle
        if isBase and bTop and bLow and cupMid and bCount >= 20:
            handle_len = 15
            w12_start = max(0, i - handle_len)
            H12 = np.max(highs[w12_start:i+1]) if i >= w12_start else highs[i]
            L12 = np.min(lows[w12_start:i+1])
            hDep = (H12 - L12) / H12 * 100.0 if H12 > 0 else 999
            inTop = L12 >= cupMid * 0.85
            depOk_h = 2.0 <= hDep <= 25.0
            if inTop and depOk_h and H12 < bTop * 1.02:
                isCupH = True
                cupHandlePivot = H12

        # Double Bottom
        dbMaxBars = 75
        if isBase and bDepPct is not None and 15.0 <= bDepPct <= 40.0 and len(aHP_list) >= 2 and len(aLP_list) >= 2:
            for hp_i in range(min(5, len(aHP_list) - 1)):
                for hp_j in range(hp_i + 1, min(len(aHP_list), hp_i + 5)):
                    sH_t, sH = aHP_list[hp_i]
                    fH_t, fH = aHP_list[hp_j]
                    if fH_t < i - dbMaxBars:
                        continue
                    l1 = [p for p in aLP_list if fH_t < p[0] < sH_t]
                    l2 = [p for p in aLP_list if sH_t < p[0] <= i]
                    if l1 and l2:
                        fLt, fL = l1[0]
                        sLt, sL = l2[0]
                        peak = max(fH, sH)
                        cA = sL <= fL * 1.03 and sL >= fL * 0.85
                        cB = sL >= (1 - bdF) * peak
                        cC = sL <= peak * 0.95
                        cD = sH >= sL + (peak - sL) * 0.20
                        cTA = fH_t < fLt < sH_t < sLt
                        cTB = sLt - fH_t <= dbMaxBars
                        cTC = sLt - fH_t >= 5
                        if cA and cB and cC and cD and cTA and cTB and cTC:
                            isDB = True
                            dbMiddlePivot = sH
                            break
                if isDB:
                    break

        # Consolidation
        isConsolidation = isBase and bCount > 200 and not isCup and not isCupH

        # Assign pattern name
        if isFlat: pattern_name = 'Flat Base'
        elif isDB: pattern_name = 'Dbl Bottom'
        elif isCupH: pattern_name = 'Cup+Handle'
        elif isCup: pattern_name = 'Cup'
        elif isConsolidation: pattern_name = 'Consolidation'
        elif isBase and bDepPct and bDepPct > 18: pattern_name = 'Deep Base'

        # Breakout detection
        active_pivot = dbMiddlePivot if (isDB and dbMiddlePivot is not None) else (
            cupHandlePivot if (isCupH and cupHandlePivot is not None) else bTop)

        if isBase and active_pivot is not None and highs[i] > active_pivot:
            bases_found.append({
                'start_bar': bStart,
                'end_bar': i,
                'bTop': bTop,
                'bLow': bLow,
                'bDepPct': bDepPct,
                'bCount': bCount,
                'pattern_name': pattern_name,
                'break_bar': i,
                'pivot_price': active_pivot,
                'rs_count': rsCount,
            })
            isBase = False
            bTop = bLow = bStart = None
            bCount = 0
            aHP_list.clear()
            aLP_list.clear()

        if isBase:
            if rs_nh_any[i]:
                rsCount += 1
            if newBase:
                rsCount = 0

    return bases_found, highs, lows, closes, opens, volumes, ema10, ema20, sma50, sma20_vol, atr14, rs_raw


# ══════════════════════════════════════════════════════════════════════════════
# Buy signal detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_buy_signals(highs, lows, closes, opens, volumes,
                       ema10, ema20, sma50, sma20_vol, atr14,
                       pivot_price, bLow, search_start, search_end, rs_raw=None):
    """Detect all buy strategies within a base window."""
    signals = {}

    # 1. Pivot Breakout
    for i in range(search_start, search_end + 1):
        if i < len(highs) and highs[i] > pivot_price:
            signals['Pivot Breakout'] = (i, pivot_price)
            break

    # 2. Upside Reversal
    for i in range(search_start, search_end + 1):
        if i < 1 or i >= len(closes) or i >= len(atr14):
            continue
        bar_range = highs[i] - lows[i]
        if bar_range <= 0 or atr14[i] <= 0 or bar_range < atr14[i] * 0.8:
            continue
        if closes[i] <= (highs[i] + lows[i]) / 2.0 or closes[i] < opens[i]:
            continue
        if closes[i] < bLow or closes[i] > pivot_price:
            continue
        signals['Upside Reversal'] = (i, closes[i])
        break

    # 3. Shakeout
    ema3 = pd.Series(closes).ewm(span=3, adjust=False).mean().values
    pl, _ = find_pivots(highs, lows, 3, 3)
    swing_lows = [(b, p) for b, p in pl.items()
                  if search_start - 10 <= b <= search_end]
    for sl_bar, sl_price in swing_lows:
        for i in range(sl_bar + 1, min(sl_bar + 6, search_end + 1, len(lows))):
            if lows[i] < sl_price:
                for j in range(i, min(i + 4, search_end + 1, len(closes))):
                    if closes[j] > ema3[j] and ema3[j] > 0:
                        if pivot_price * 0.85 <= closes[j] <= pivot_price:
                            signals['Shakeout'] = (j, closes[j])
                            break
                break

    # 4. Volume Dry-Up
    for i in range(search_start, search_end + 1):
        if i >= len(volumes) or i >= len(sma20_vol) or sma20_vol[i] <= 0:
            continue
        if volumes[i] >= sma20_vol[i] * 0.55:
            continue
        if closes[i] < pivot_price * 0.95 or closes[i] > pivot_price * 1.01 or closes[i] < bLow:
            continue
        signals['Volume Dry-Up'] = (i, closes[i])
        break

    # 5. MA Touch
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
            signals['MA Touch'] = (i, closes[i])
            break

    # 6. Pocket Pivot
    down_vols = np.where(pd.Series(closes).diff() < 0, volumes, 0.0)
    for i in range(search_start, search_end + 1):
        if i < 10 or i >= len(closes) or i >= len(volumes):
            continue
        if closes[i] <= closes[i - 1]:
            continue
        if i >= len(sma20_vol) or sma20_vol[i] <= 0 or volumes[i] <= sma20_vol[i]:
            continue
        max_dn_vol = 0.0
        for j in range(i - 10, i):
            if j > 0 and closes[j] < closes[j - 1]:
                max_dn_vol = max(max_dn_vol, volumes[j])
        if max_dn_vol <= 0 or volumes[i] <= max_dn_vol:
            continue
        if closes[i] < pivot_price * 0.90 or closes[i] > pivot_price * 1.01 or closes[i] < bLow:
            continue
        signals['Pocket Pivot'] = (i, closes[i])
        break

    # 7. RS New High
    if rs_raw is not None and len(rs_raw) > 0:
        n_rs = len(rs_raw)
        for i in range(search_start, search_end + 1):
            if i < 63 or i >= n_rs:
                continue
            nh = (rs_raw[i] > np.max(rs_raw[max(0, i - 252):i]) or
                  rs_raw[i] > np.max(rs_raw[max(0, i - 126):i]) or
                  rs_raw[i] > np.max(rs_raw[max(0, i - 63):i]))
            if not nh:
                continue
            if closes[i] < pivot_price * 0.85 or closes[i] > pivot_price * 1.01 or closes[i] < bLow:
                continue
            signals['RS New High'] = (i, closes[i])
            break

    # 8. SMA50 Bounce
    for i in range(search_start, search_end + 1):
        if i < 2 or i >= len(closes) or i >= len(sma50):
            continue
        if np.isnan(sma50[i]) or sma50[i] <= 0:
            continue
        prev_tested = (lows[i - 1] <= sma50[i - 1] * 1.02
                       if not np.isnan(sma50[i - 1]) and sma50[i - 1] > 0 else False)
        if not prev_tested or closes[i] <= sma50[i]:
            continue
        if i < len(opens) and closes[i] < opens[i]:
            continue
        if closes[i] < pivot_price * 0.80 or closes[i] > pivot_price or closes[i] < bLow:
            continue
        signals['SMA50 Bounce'] = (i, closes[i])
        break

    # 9. Any Signal (first chronologically)
    if signals:
        first = min(signals.keys(), key=lambda s: signals[s][0])
        signals['Any Signal'] = signals[first]

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# Exit rules
# ══════════════════════════════════════════════════════════════════════════════

def apply_exit_rules(highs, lows, closes, signal_bar, entry_price, base_low, atr):
    """Apply multiple exit rules and return the exit that triggers first."""
    n = len(closes)
    results = {}

    # Stop-loss at base low
    for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
        if lows[bar] <= base_low:
            ret = (base_low - entry_price) / entry_price * 100.0
            results['stop_loss'] = {'exit_bar': bar, 'exit_price': base_low, 'ret': ret}
            break
    else:
        ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
        results['stop_loss'] = {'exit_bar': min(signal_bar + 60, n - 1),
                                'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    # Trailing stop (2x ATR)
    highest_since = entry_price
    for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
        highest_since = max(highest_since, highs[bar])
        trail = highest_since - 2 * atr[bar] if bar < len(atr) else highest_since * 0.92
        if lows[bar] <= trail:
            ret = (trail - entry_price) / entry_price * 100.0
            results['trail_2atr'] = {'exit_bar': bar, 'exit_price': trail, 'ret': ret}
            break
    else:
        ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
        results['trail_2atr'] = {'exit_bar': min(signal_bar + 60, n - 1),
                                 'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    # Trailing stop (3x ATR)
    highest_since = entry_price
    for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
        highest_since = max(highest_since, highs[bar])
        trail = highest_since - 3 * atr[bar] if bar < len(atr) else highest_since * 0.88
        if lows[bar] <= trail:
            ret = (trail - entry_price) / entry_price * 100.0
            results['trail_3atr'] = {'exit_bar': bar, 'exit_price': trail, 'ret': ret}
            break
    else:
        ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
        results['trail_3atr'] = {'exit_bar': min(signal_bar + 60, n - 1),
                                 'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    # Time stops
    for t_bars, key in [(20, 'time_20'), (40, 'time_40'), (60, 'time_60')]:
        exit_bar = min(signal_bar + t_bars, n - 1)
        ret = (closes[exit_bar] - entry_price) / entry_price * 100.0
        results[key] = {'exit_bar': exit_bar, 'exit_price': closes[exit_bar], 'ret': ret}

    # Profit targets (R:R ratios based on risk = entry - base_low)
    risk = entry_price - base_low
    if risk > 0:
        for rr, key in [(2, 'target_2r'), (3, 'target_3r'), (5, 'target_5r')]:
            target_price = entry_price + risk * rr
            for bar in range(signal_bar + 1, min(signal_bar + 61, n)):
                if highs[bar] >= target_price:
                    ret = (target_price - entry_price) / entry_price * 100.0
                    results[key] = {'exit_bar': bar, 'exit_price': target_price, 'ret': ret}
                    break
            else:
                ret = (closes[min(signal_bar + 60, n - 1)] - entry_price) / entry_price * 100.0
                results[key] = {'exit_bar': min(signal_bar + 60, n - 1),
                                'exit_price': closes[min(signal_bar + 60, n - 1)], 'ret': ret}

    return results


# ══════════════════════════════════════════════════════════════════════════════
# Base quality scoring
# ══════════════════════════════════════════════════════════════════════════════

def calc_base_quality(bDepPct, bCount):
    """Simplified base quality score (0-100)."""
    score = 50.0
    if bDepPct and 15 <= bDepPct <= 35:
        score += 15
    elif bDepPct and 10 <= bDepPct <= 45:
        score += 8
    if 20 <= bCount <= 150:
        score += 15
    elif 15 <= bCount <= 200:
        score += 8
    if bDepPct and 18.1 <= bDepPct <= 50.0:
        score += 10  # deeper bases get bonus
    return min(100, max(0, score))


def pos_size(quality):
    if quality >= 80: return 1.0
    elif quality >= 60: return 0.75
    elif quality >= 40: return 0.50
    elif quality >= 20: return 0.35
    return 0.25


# ══════════════════════════════════════════════════════════════════════════════
# Main backtest
# ══════════════════════════════════════════════════════════════════════════════

def run_full_backtest():
    start_time = time.time()

    # Load SPY
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    spy_close = None
    if spy_path.exists():
        try:
            spy_df = pd.read_parquet(spy_path)
            spy_close = spy_df['Close']
            print(f"✅ Loaded SPY data ({len(spy_df)} bars)")
        except Exception:
            pass

    # Find all ticker files
    files = sorted(glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet")))
    print(f"📂 Found {len(files)} ticker files")

    # ── Filter tickers ──
    qualified = []
    for f in files:
        ticker = Path(f).name.replace("_1d.parquet", "")
        if ticker in ("SPY", "QQQ", "IWM"):
            continue
        try:
            df = pd.read_parquet(f)
            if df.empty or len(df) < 100:
                continue
            last_close = df['Close'].iloc[-1]
            avg_vol = df['Volume'].tail(50).mean()
            if last_close >= MIN_PRICE and avg_vol >= MIN_AVG_VOL_50:
                qualified.append((ticker, f, last_close, avg_vol))
        except Exception:
            continue

    print(f"✅ {len(qualified)} tickers pass filters (price > ${MIN_PRICE}, vol > {MIN_AVG_VOL_50:,})")

    all_trades = []
    bases_total = 0
    tickers_processed = 0

    for ticker, fpath, lc, av in qualified:
        try:
            df = pd.read_parquet(fpath)
            if df.empty:
                continue
            bases, highs, lows, closes, opens, volumes, ema10, ema20, sma50, sma20_vol, atr14, rs_raw = \
                scan_ticker_for_bases(df, spy_close)

            if not bases:
                continue

            bases_total += len(bases)
            tickers_processed += 1

            for base in bases:
                bq = calc_base_quality(base['bDepPct'], base['bCount'])
                ps = pos_size(bq)

                # Search window: from base start to break bar + 5
                search_start = max(0, base['start_bar'])
                search_end = min(base['break_bar'] + 5, len(closes) - 1)

                buy_signals = detect_buy_signals(
                    highs, lows, closes, opens, volumes,
                    ema10, ema20, sma50, sma20_vol, atr14,
                    base['pivot_price'], base['bLow'],
                    search_start, search_end, rs_raw)

                if not buy_signals:
                    continue

                # Composite Score
                sig_names_real = {k: v for k, v in buy_signals.items()
                                  if k not in ('Any Signal',)}
                composite_score = min(100, bq * 0.3 + len(sig_names_real) * 10)
                if composite_score >= 30 and sig_names_real:
                    best = max(sig_names_real.keys(),
                               key=lambda s: (15 if s in ('Pivot Breakout', 'Pocket Pivot', 'Shakeout', 'RS New High')
                                              else 10 if s in ('Upside Reversal', 'SMA50 Bounce')
                                              else 8 if s == 'Volume Dry-Up' else 5))
                    buy_signals['Composite Score'] = sig_names_real[best]

                for strategy, (sig_bar, entry_price) in buy_signals.items():
                    exit_results = apply_exit_rules(
                        highs, lows, closes, sig_bar, entry_price, base['bLow'], atr14)

                    for exit_rule, exit_data in exit_results.items():
                        ret_raw = exit_data['ret']
                        ret = ret_raw * ps  # position-size the return
                        risk = entry_price - base['bLow']
                        rr = abs(ret_raw / (risk / entry_price * 100)) if risk > 0 else 0

                        all_trades.append({
                            'ticker': ticker,
                            'pattern': base['pattern_name'],
                            'depth': base['bDepPct'],
                            'length': base['bCount'],
                            'pivot_price': base['pivot_price'],
                            'base_low': base['bLow'],
                            'strategy': strategy,
                            'exit_rule': exit_rule,
                            'entry_bar': sig_bar,
                            'entry_price': entry_price,
                            'exit_bar': exit_data['exit_bar'],
                            'exit_price': exit_data['exit_price'],
                            'ret': ret,
                            'ret_raw': ret_raw,
                            'base_quality': bq,
                            'pos_size': ps,
                            'risk_amount': risk,
                            'rr_ratio': rr,
                            'win': ret > 0,
                            'last_close': lc,
                            'avg_vol_50d': av,
                        })

                # Generate ALL combinations (pairs through all-N).
                # Each combo enters on the EARLIEST bar among its signals — the idea
                # is that multiple signals firing close together is higher-confidence.
                real_sigs = {k: v for k, v in buy_signals.items()
                             if k not in ('Any Signal', 'Composite Score')}
                sig_names = sorted(real_sigs.keys())
                for combo_size in range(2, len(sig_names) + 1):
                    for combo in combinations(sig_names, combo_size):
                        bars = [real_sigs[s][0] for s in combo]
                        prices = [real_sigs[s][1] for s in combo]
                        ei = bars.index(min(bars))
                        combo_name = '+'.join(combo)
                        exit_results = apply_exit_rules(
                            highs, lows, closes, bars[ei], prices[ei], base['bLow'], atr14)
                        for exit_rule, exit_data in exit_results.items():
                            ret_raw = exit_data['ret']
                            ret = ret_raw * ps
                            risk = prices[ei] - base['bLow']
                            rr = abs(ret_raw / (risk / prices[ei] * 100)) if risk > 0 else 0
                            all_trades.append({
                                'ticker': ticker, 'pattern': base['pattern_name'],
                                'depth': base['bDepPct'], 'length': base['bCount'],
                                'pivot_price': base['pivot_price'], 'base_low': base['bLow'],
                                'strategy': combo_name, 'exit_rule': exit_rule,
                                'entry_bar': bars[ei], 'entry_price': prices[ei],
                                'exit_bar': exit_data['exit_bar'], 'exit_price': exit_data['exit_price'],
                                'ret': ret, 'ret_raw': ret_raw,
                                'base_quality': bq, 'pos_size': ps,
                                'risk_amount': risk, 'rr_ratio': rr, 'win': ret > 0,
                                'last_close': lc, 'avg_vol_50d': av,
                            })

        except Exception:
            continue

    if not all_trades:
        print("❌ No trades generated")
        return

    df = pd.DataFrame(all_trades)
    elapsed = time.time() - start_time

    print(f"\n{'='*100}")
    print(f"📊 FULL BACKTEST COMPLETE — {elapsed:.0f}s")
    print(f"   Tickers processed: {tickers_processed}")
    print(f"   Bases detected: {bases_total}")
    print(f"   Total trades (buy×exit×combo): {len(df):,}")
    print(f"{'='*100}\n")

    # ── Save results ──
    results_path = OUTPUT_DIR / "full_backtest_results.csv"
    df.to_csv(results_path, index=False)
    print(f"💾 Full results saved to {results_path} ({results_path.stat().st_size:,} bytes)")

    # ── Summary by buy strategy × exit rule (all strategies including combos) ──
    all_buy_sigs = sorted(df['strategy'].unique())
    print(f"\n📊 BUY STRATEGY × EXIT RULE SUMMARY (top 40 by Sharpe)")
    print(f"{'='*100}")
    print(f"{'Buy Strategy':<42} {'Exit Rule':<14} {'Trades':>7} {'Win%':>7} {'Avg Ret':>8} {'Avg R:R':>8} {'Sharpe':>8}")
    print(f"{'-'*100}")

    summary_rows = []
    for buy_s in all_buy_sigs:
        for exit_r in EXIT_RULES:
            sdf = df[(df['strategy'] == buy_s) & (df['exit_rule'] == exit_r)]
            n = len(sdf)
            if n < 5:
                continue
            win_pct = sdf['win'].mean() * 100
            avg_ret = sdf['ret'].mean()
            avg_rr = sdf['rr_ratio'].mean()
            sharpe = sdf['ret'].mean() / sdf['ret'].std() if sdf['ret'].std() > 0 else 0
            summary_rows.append({
                'buy_strategy': buy_s, 'exit_rule': exit_r, 'trades': n,
                'win_pct': round(win_pct, 1), 'avg_ret': round(avg_ret, 2),
                'avg_rr': round(avg_rr, 2), 'sharpe': round(sharpe, 2),
            })

    summary_df = pd.DataFrame(summary_rows)
    summary_df = summary_df.sort_values('sharpe', ascending=False)
    summary_path = OUTPUT_DIR / "full_backtest_summary.csv"
    summary_df.to_csv(summary_path, index=False)

    # Print top 40
    for _, r in summary_df.head(40).iterrows():
        print(f"{r['buy_strategy']:<42} {r['exit_rule']:<14} {int(r['trades']):>7,} {r['win_pct']:>6.1f}% {r['avg_ret']:>7.2f}% {r['avg_rr']:>7.2f} {r['sharpe']:>7.2f}")

    print(f"\n💾 Summary saved to {summary_path} ({len(summary_df)} rows)")

    return df, summary_df


if __name__ == "__main__":
    run_full_backtest()
