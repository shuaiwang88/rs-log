"""
ibd_pattern_scanner.py

Python implementation of the TradingView IBD Pattern Scanner (drw_pattern_scanner.pine).
Scans all ticker parquet data in `ticker_cache/` to detect MarketSmith / IBD patterns:
  1. Base (Deep Base)
  2. Flat Base
  3. Cup
  4. Cup + Handle
  5. Double Bottom
  6. High Tight Flag (HTF)
  7. 6-Wk Flat Base

Calculates technical metrics & scoring matching drw_pattern_scanner.pine:
  - In-Base / Post-BO status & Days in Base / Bars Since Breakout
  - Distance to Pivot %
  - % Off 52W High
  - RS New High count & signals
  - Before-BO Score (0-6) & Post-BO Score (0-6) & Composite Score (0-12)
    (Pocket Pivot, Shakeout, MA Touch, Volume Dry-Up, RS New High, Upside Reversal)
"""

import os
import sys
import glob
import warnings
warnings.filterwarnings("ignore")
import json
import time
import math
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import pickle

# Root project directory
ROOT_DIR = Path(__file__).resolve().parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_JSON_PATH = ROOT_DIR / "python" / "ibd_pattern_results.json"
MODEL_PATH = ROOT_DIR / "python" / "pattern_model.pkl"

PATTERN_MODEL = None
if MODEL_PATH.exists():
    try:
        with open(MODEL_PATH, "rb") as f:
            PATTERN_MODEL = pickle.load(f)
    except Exception:
        PATTERN_MODEL = None


def calculate_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, length: int = 14) -> np.ndarray:
    """Calculate Average True Range (ATR)."""
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
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    return atr


def find_pivots(highs: np.ndarray, lows: np.ndarray, left: int = 5, right: int = 5):
    """Find pivot highs and pivot lows."""
    n = len(highs)
    pivot_highs = {} # bar_idx -> price
    pivot_lows = {}  # bar_idx -> price
    
    for i in range(left, n - right):
        h = highs[i]
        l = lows[i]
        
        # Check pivot high
        is_ph = True
        for j in range(i - left, i + right + 1):
            if j != i and highs[j] >= h:
                is_ph = False
                break
        if is_ph:
            pivot_highs[i] = h
            
        # Check pivot low
        is_pl = True
        for j in range(i - left, i + right + 1):
            if j != i and lows[j] <= l:
                is_pl = False
                break
        if is_pl:
            pivot_lows[i] = l
            
    return pivot_highs, pivot_lows


def scan_single_ticker(ticker: str, file_path: str, spy_close_series: pd.Series = None):
    """
    Scan a single ticker parquet file for patterns & metrics matching drw_pattern_scanner.pine.
    """
    try:
        df = pd.read_parquet(file_path)
        if df.empty or len(df) < 60:
            return None
            
        # Standardize columns
        required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in required_cols:
            if col not in df.columns:
                return None
                
        df = df.sort_index()
        # Keep full df (or at least 1500 bars) for accurate base transition tracking from inception
        if len(df) > 1500:
            df = df.iloc[-1500:]
            
        highs = df['High'].values
        lows = df['Low'].values
        closes = df['Close'].values
        volumes = df['Volume'].values
        opens = df['Open'].values
        n = len(df)
        
        # Parameters (Daily timeframe -> barsPerWeek = 5)
        barsPerWeek = 5
        pivLag = 5  # 5 daily bars (matching drw_pattern_scanner.pine i_pivot = 5)
        win13wk = 65
        win20wk = 100
        bLenB = 325  # 65 weeks
        bdF = 0.50
        pivLen = 5
        
        # Fast Moving Averages
        close_series = pd.Series(closes)
        ema10 = close_series.ewm(span=10, adjust=False).mean().values
        ema20 = close_series.ewm(span=20, adjust=False).mean().values
        sma50 = close_series.rolling(50, min_periods=10).mean().values
        sma20_vol = pd.Series(volumes).rolling(20, min_periods=5).mean().values
        atr14 = calculate_atr(highs, lows, closes, 14)
        
        # Raw RS Calculation if SPY provided
        rs_raw = None
        if spy_close_series is not None and not spy_close_series.empty:
            aligned_spy = spy_close_series.reindex(df.index).ffill().bfill().values
            if len(aligned_spy) == n and np.all(aligned_spy > 0):
                rs_raw = closes * 7.0 * 1000.0 / aligned_spy
                
        if rs_raw is None:
            rs_raw = closes.copy() # fallback
            
        # RS New High lookbacks (1Y=252, 6M=126, 3M=63)
        rs_s = pd.Series(rs_raw, index=df.index)
        rs_h1y = rs_s.shift(1).rolling(min(252, n), min_periods=30).max().values
        rs_h6m = rs_s.shift(1).rolling(min(126, n), min_periods=20).max().values
        rs_h3m = rs_s.shift(1).rolling(min(63, n), min_periods=10).max().values
        
        rs_nh_1y = (rs_raw > rs_h1y) & (~np.isnan(rs_h1y))
        rs_nh_6m = (rs_raw > rs_h6m) & (~np.isnan(rs_h6m))
        rs_nh_3m = (rs_raw > rs_h3m) & (~np.isnan(rs_h3m))
        rs_nh_any = rs_nh_1y | rs_nh_6m | rs_nh_3m
        
        # Pre-compute Pivots
        pivot_highs, pivot_lows = find_pivots(highs, lows, pivLen, pivLen)
        
        # We track base state bar-by-bar
        aHP_list = [] # list of (bar_idx, price) for highs
        aLP_list = [] # list of (bar_idx, price) for lows
        
        # Base variables
        bTop = None
        bLow = None
        bStart = None
        isBase = False
        bCount = 0
        lastBTop = None
        
        boPivot = None
        boBar = None
        boPatternCode = 0
        boPatternName = 'None'
        
        rsCount = 0
        
        # Track active state per bar
        history_state = []
        
        # HTF state variables
        htf_flag_baseHigh = None
        htf_flag_startIndex = None
        htf_flag_flagLength = 0
        htf_flag_baseLow = None
        htf_flag_lowIndex = None
        htf_flag_flagBool = False
        htf_poleLow = None
        htf_poleLowIndex = None
        htf_boBar = None
        htf_history_is_flag = []
        
        # Sub-signals
        down_vols = np.where(pd.Series(closes).diff() < 0, volumes, 0.0)
        
        # Score flags pre-BO / post-BO
        scorePP_pre = False
        scoreShake_pre = False
        scoreTouch_pre = False
        scoreVDU_pre = False
        scoreRS_pre = False
        scoreUpRev_pre = False
        
        scorePP_post = False
        scoreShake_post = False
        scoreTouch_post = False
        scoreVDU_post = False
        scoreRS_post = False
        scoreUpRev_post = False
        
        # Shakeout state variables
        shakeTrendEMA = sma50
        shakeEma3 = close_series.ewm(span=3, adjust=False).mean().values
        shakeLastSwingLow = None
        shakeUndercutBar = None
        shakeReclaimBar = None
        shakeReclaimHigh = None
        shakeSetupActive = False
        
        for i in range(n):
            current_bar = i
            # Check if pivot high or low confirmed at i - pivLen
            conf_bar = i - pivLen
            if conf_bar in pivot_highs:
                aHP_list.insert(0, (conf_bar, pivot_highs[conf_bar]))
            if conf_bar in pivot_lows:
                aLP_list.insert(0, (conf_bar, pivot_lows[conf_bar]))
                shakeLastSwingLow = pivot_lows[conf_bar]
                
            # Highest / Lowest windows at bar i
            w25_start = max(0, i - pivLag + 1)
            H25 = np.max(highs[w25_start:i+1]) if i >= w25_start else highs[i]
            L25 = np.min(lows[w25_start:i+1]) if i >= w25_start else lows[i]
            
            w103_start = max(0, i - 103 + 1)
            L103 = np.min(lows[w103_start:i+1])
            
            # H65s: 65-bar high shifted by 26 bars (i - pivLag - 1)
            shift_idx = i - pivLag - 1
            if shift_idx >= 0:
                w65_start = max(0, shift_idx - 65 + 1)
                H65s = np.max(highs[w65_start:shift_idx+1])
            else:
                H65s = highs[i]
                
            # Base start check (newBase)
            newBase = False
            if len(aHP_list) >= 3 and i >= pivLag:
                piv_idx = i - pivLag
                piv_h = highs[piv_idx]
                
                recent_hp_prices = [p for b, p in aHP_list[:3]]
                bH = any(abs(piv_h - p) < 1e-4 for p in recent_hp_prices)
                bPH = (piv_h > H65s) or (bTop is not None and piv_h > bTop)
                lUp = (L103 * 1.20 <= piv_h)
                dep = (piv_h * (1.0 - bdF) <= L25)
                noAb = (H25 <= piv_h)
                
                noNe = True
                if len(history_state) >= pivLag:
                    noNe = not history_state[i - pivLag]['inBase']
                    
                newBase = bH and bPH and lUp and dep and noAb and noNe
                
            prev_isBase = history_state[-1]['inBase'] if history_state else False
            
            if newBase and not prev_isBase:
                piv_idx = i - pivLag
                bTop = highs[piv_idx]
                bLow = L25
                bStart = piv_idx
                bCount = pivLag
                isBase = True
                lastBTop = bTop
                scorePP_pre = False
                scoreShake_pre = False
                scoreTouch_pre = False
                scoreVDU_pre = False
                scoreRS_pre = False
                scoreUpRev_pre = False
            elif not newBase and prev_isBase:
                isBase = True
                
            if isBase:
                bCount += 1
                if bTop is not None and highs[i] > bTop and highs[i] <= bTop * 1.05:
                    bTop = highs[i]
                lastBTop = bTop
                if bLow is not None and lows[i] < bLow and bTop is not None and lows[i] >= bTop * (1.0 - bdF):
                    bLow = lows[i]
                    
            # Invalidation: too deep or too long
            if isBase and bTop is not None:
                if lows[i] < bTop * (1.0 - bdF) or bCount > bLenB:
                    isBase = False
            
            # Reset pattern flags when no active base (prevent stale detections)
            if not isBase:
                isCupH = False
                isCup = False
                isDB = False
                isAscendingBase = False
                    
            # Depth Pct & Base Types (Evaluated while in base BEFORE breakout check)
            bDepPct = (bTop - bLow) / bTop * 100.0 if (bTop and bLow and bTop > 0) else None
            recent_win = min(i + 1, max(20, min(bCount, 65)))
            rTop = np.max(highs[max(0, i - recent_win + 1) : i + 1])
            rLow = np.min(lows[max(0, i - recent_win + 1) : i + 1])
            rDepPct = (rTop - rLow) / rTop * 100.0 if rTop > 0 else 0.0

            isFlatBase = isBase and (rDepPct <= 18.0) and (20 <= bCount <= 130)
            # Additional flat base check: recent 25-bar depth
            if isBase and not isFlatBase:
                rTop25 = np.max(highs[max(0, i - 24) : i + 1])
                rLow25 = np.min(lows[max(0, i - 24) : i + 1])
                rDep25 = (rTop25 - rLow25) / rTop25 * 100.0 if rTop25 > 0 else 0.0
                isFlatBase = (rDep25 <= 15.0) and (20 <= bCount <= 300)
            isDeepBase = isBase and not isFlatBase
            is6WkFlat = isFlatBase and (25 <= recent_win <= 35)
            
            # 2. Ascending Base Detection (Strict 3 stair-step pullbacks spaced apart)
            isAscendingBase = False
            if isBase and len(aHP_list) >= 3 and len(aLP_list) >= 3:
                recent_hps = [p for p in aHP_list if p[0] >= i - 90][:3]
                recent_lps = [p for p in aLP_list if p[0] >= i - 90][:3]
                if len(recent_hps) == 3 and len(recent_lps) == 3:
                    recent_hps.sort(key=lambda x: x[0])
                    recent_lps.sort(key=lambda x: x[0])
                    h1, h2, h3 = recent_hps[0][1], recent_hps[1][1], recent_hps[2][1]
                    l1, l2, l3 = recent_lps[0][1], recent_lps[1][1], recent_lps[2][1]
                    t_spaced = (recent_hps[1][0] - recent_hps[0][0] >= 8) and (recent_hps[2][0] - recent_hps[1][0] >= 8)
                    hh = (h1 < h2 < h3) or (h3 >= h1 * 1.01 and h2 >= h1 * 0.98)
                    hl = (l1 < l2 < l3) or (l3 >= l1 * 1.01 and l2 >= l1 * 0.98)
                    pb1, pb2, pb3 = (h1 - l1) / h1, (h2 - l2) / h2, (h3 - l3) / h3
                    pb_ok = all(0.04 <= p <= 0.25 for p in [pb1, pb2, pb3])
                    if t_spaced and hh and hl and pb_ok:
                        isAscendingBase = True
            
            # 3. Double Bottom Detection (W-shape symmetry)
            isDB = False
            dbMiddlePivot = None
            dbMaxBars = 85
            if isBase and not isFlatBase and len(aHP_list) >= 2 and len(aLP_list) >= 2:
                for hp_i in range(min(5, len(aHP_list) - 1)):
                    for hp_j in range(hp_i + 1, min(len(aHP_list), hp_i + 5)):
                        sH_t, sH = aHP_list[hp_i]
                        fH_t, fH = aHP_list[hp_j]
                        if fH_t < i - dbMaxBars:
                            continue
                        
                        l1_candidates = [p for p in aLP_list if fH_t < p[0] < sH_t]
                        l2_candidates = [p for p in aLP_list if sH_t < p[0] <= i]
                        
                        if l1_candidates and l2_candidates:
                            fLt, fL = l1_candidates[0]
                            sLt, sL = l2_candidates[0]
                            
                            peak = max(fH, sH)
                            prL250 = np.min(lows[max(0, i-250):i+1])
                            cPT = (fH >= prL250 * 1.10) or (sH >= prL250 * 1.10)
                            cA = (sL <= fL * 1.04) and (sL >= fL * 0.85)
                            cB = (sL >= (1 - bdF) * peak)
                            cC = (sL <= peak * 0.95)
                            cD = (sH >= fL + (fH - fL) * 0.30) and (sH >= sL + (peak - sL) * 0.30)
                            cE = (sH <= fH * 1.08) and (sH >= fH * 0.75)
                            cTA = (fH_t < fLt < sH_t < sLt)
                            cTB = (sLt - fH_t <= dbMaxBars) and (i - fH_t <= dbMaxBars)
                            cTC = (sLt - fH_t >= 5)
                            highest_since_2nd_low = np.max(highs[sLt:i]) if sLt < i else highs[i-1]
                            cSh = (highest_since_2nd_low <= sH * 1.10)
                            
                            if cPT and cA and cB and cC and cD and cE and cTA and cTB and cTC and cSh:
                                isDB = True
                                dbMiddlePivot = sH
                                break
                    if isDB:
                        break
                        
            # 5. Cup Without Handle
            isCup = False
            isCupH = False
            cupMid = bLow + (bTop - bLow) * 0.5 if (bTop and bLow) else None
            cupHandlePivot = None

            if isBase and bTop and bLow and (25 <= bCount <= 130):
                depOk = (bDepPct is not None and 12.0 <= bDepPct <= 50.0)
                if depOk:
                    isCup = True

            # 6. Cup With Handle (independent of cup detection — allows handles on long cups)
            if bTop and bLow and cupMid and bCount >= 20 and not isFlatBase and (bDepPct is not None and 12.0 <= bDepPct <= 50.0):
                handle_len = min(25, max(5, bCount // 4))
                end_h_idx = max(1, i - 1)
                w12_start = max(0, end_h_idx - handle_len)
                H12 = np.max(highs[w12_start:end_h_idx + 1]) if end_h_idx >= w12_start else highs[i]
                L12 = np.min(lows[w12_start:end_h_idx + 1])
                hDep = (H12 - L12) / H12 * 100.0 if H12 > 0 else 999.0
                inTop = (L12 >= cupMid * 0.80)
                depOk_h = (2.0 <= hDep <= 25.0)
                if inTop and depOk_h and H12 < bTop * 1.02:
                    hdRatio = hDep / bDepPct if bDepPct and bDepPct > 0 else 1.0
                    if hdRatio < 0.75:
                        isCupH = True
                        cupHandlePivot = H12
            # 7. Consolidation: Long bases (> 130 daily bars) or general consolidation
            isConsolidation = isBase and (
                (bCount > 130) or 
                (bDepPct is not None and 10.0 <= bDepPct <= 50.0 and not isCup and not isCupH and not isFlatBase and not isDB and not isAscendingBase)
            )

            # Determine active base pattern name BEFORE breakout check
            currPName = 'Base'
            currPCode = 1
            if isCupH: currPName, currPCode = 'Cup+Handle', 4
            elif isFlatBase: currPName, currPCode = 'Flat Base', 2
            elif isCup: currPName, currPCode = 'Cup', 3
            elif is6WkFlat: currPName, currPCode = '6-Wk Flat', 7
            elif isAscendingBase: currPName, currPCode = 'Ascending Base', 8
            elif isDB: currPName, currPCode = 'Dbl Bottom', 5
            elif isConsolidation: currPName, currPCode = 'Consolidation', 9

            if False and isBase and PATTERN_MODEL is not None:
                lookback65 = min(i+1, 65)
                h65_f = np.max(highs[i+1-lookback65:i+1])
                l65_f = np.min(lows[i+1-lookback65:i+1])
                dep65_f = (h65_f - l65_f) / h65_f * 100.0 if h65_f > 0 else 0.0

                lookback30 = min(i+1, 30)
                h30_f = np.max(highs[i+1-lookback30:i+1])
                l30_f = np.min(lows[i+1-lookback30:i+1])
                dep30_f = (h30_f - l30_f) / h30_f * 100.0 if h30_f > 0 else 0.0

                lookback90 = min(i+1, 90)
                h90_f = np.max(highs[i+1-lookback90:i+1])
                l90_f = np.min(lows[i+1-lookback90:i+1])
                dep90_f = (h90_f - l90_f) / h90_f * 100.0 if h90_f > 0 else 0.0

                lookback12 = min(i+1, 12)
                h12_f = np.max(highs[i+1-lookback12:i+1])
                l12_f = np.min(lows[i+1-lookback12:i+1])
                dep12_f = (h12_f - l12_f) / h12_f * 100.0 if h12_f > 0 else 0.0

                handle_pos_f = (l12_f - l65_f) / (h65_f - l65_f) if (h65_f > l65_f) else 0.0
                has_w_shape_f = 1 if isDB else 0
                has_asc_base_f = 1 if isAscendingBase else 0

                feat_vec = np.array([[dep65_f, dep30_f, dep90_f, dep12_f, handle_pos_f, has_w_shape_f, has_asc_base_f]])
                try:
                    pred_label = PATTERN_MODEL.predict(feat_vec)[0]
                    if pred_label == 'Cup Without Handle': currPName, currPCode = 'Cup', 3
                    elif pred_label == 'Cup With Handle': currPName, currPCode = 'Cup+Handle', 4
                    elif pred_label == 'Flat Base': currPName, currPCode = 'Flat Base', 2
                    elif pred_label == 'Double Bottom': currPName, currPCode = 'Dbl Bottom', 5
                    elif pred_label == 'Ascending Base': currPName, currPCode = 'Ascending Base', 8
                    elif pred_label == 'Consolidation': currPName, currPCode = 'Consolidation', 9
                except Exception:
                    pass

            # Breakout: price clears the base top or middle pivot
            active_pivot = dbMiddlePivot if (isDB and dbMiddlePivot is not None) else (cupHandlePivot if (isCupH and cupHandlePivot is not None) else bTop)
            if isBase and active_pivot is not None and highs[i] > active_pivot:
                isBase = False
                boPivot = active_pivot
                boBar = i
                boPatternCode = currPCode
                boPatternName = currPName
                
            # Breakout tracking flag
            was_in_base = prev_isBase
            activeBTop = flag_baseHigh_prev if 'flag_baseHigh_prev' in locals() and history_state and history_state[-1]['isHTF'] else lastBTop
            
            if was_in_base and not isBase and boBar != i and activeBTop is not None and highs[i] > activeBTop:
                boPivot = activeBTop
                boBar = i
                boPatternCode = history_state[-1]['pCode'] if history_state and history_state[-1]['pCode'] > 0 else 1
                boPatternName = history_state[-1]['pName'] if history_state and history_state[-1]['pName'] != 'None' else 'Base'
                
            if newBase:
                boPivot = None
                boBar = None
                boPatternCode = 0
                boPatternName = 'None'

            # --- HTF Detection (drw_pattern_scanner.pine state machine engine) ---
            i_htfPole = 80.0
            i_htfPB = 60
            i_htfPBMin = 5
            i_htfRet = 28.0
            i_htfFMin = 1
            i_htfFMax = 50
            i_bsoMax = 15

            prev_htf_flag_baseHigh = htf_flag_baseHigh
            
            if i > 30:
                if htf_flag_baseHigh is None or highs[i] > htf_flag_baseHigh:
                    htf_flag_baseHigh = highs[i]
                    htf_flag_startIndex = i
                    htf_flag_flagLength = 0
                    htf_flag_baseLow = lows[i]
                    htf_flag_lowIndex = i
                    
                if highs[i] <= htf_flag_baseHigh and (htf_flag_baseLow is None or lows[i] < htf_flag_baseLow):
                    htf_flag_baseLow = lows[i]
                    htf_flag_lowIndex = i
                    
                if highs[i] <= htf_flag_baseHigh and htf_flag_lowIndex == htf_flag_startIndex:
                    htf_flag_baseLow = lows[i]
                    htf_flag_lowIndex = i
                    
                findDepth = abs(((htf_flag_baseLow / htf_flag_baseHigh) - 1.0) * 100.0) if (htf_flag_baseHigh and htf_flag_baseHigh > 0) else 0.0
                lower_close = (closes[i] < closes[i-1]) if i > 0 else False
                
                if (highs[i] < htf_flag_baseHigh and findDepth <= i_htfRet) or (highs[i] == htf_flag_baseHigh and lower_close):
                    htf_flag_flagLength += 1
                else:
                    htf_flag_flagLength = 0
                    
                if not htf_flag_flagBool or highs[i] == htf_flag_baseHigh:
                    searchBars = min(i_htfPB, i - 1)
                    if searchBars >= i_htfPBMin:
                        minLow = lows[i-1]
                        minLowIdx = i - 1
                        for k in range(1, searchBars + 1):
                            if lows[i - k] < minLow:
                                minLow = lows[i - k]
                                minLowIdx = i - k
                        if minLow > 0 and ((htf_flag_baseHigh / minLow) - 1.0) * 100.0 >= i_htfPole:
                            htf_flag_flagBool = True
                            htf_poleLow = minLow
                            htf_poleLowIndex = minLowIdx
                            
                if findDepth >= i_htfRet or htf_flag_flagLength > i_htfFMax:
                    htf_flag_flagBool = False
                    htf_flag_flagLength = 0
                    htf_flag_baseHigh = None
                    htf_flag_startIndex = None
                    htf_flag_lowIndex = None
                    htf_flag_baseLow = None
                    
                if prev_htf_flag_baseHigh is not None and highs[i] > prev_htf_flag_baseHigh and htf_flag_flagLength < i_htfFMin:
                    htf_flag_baseHigh = highs[i]
                    htf_flag_flagLength = 0
                    htf_flag_startIndex = i
                    htf_flag_lowIndex = i
                    htf_flag_baseLow = lows[i]
                    
                is_flag = (htf_flag_flagBool == True) and (htf_flag_flagLength <= i_htfFMax) and (findDepth < i_htfRet) and (htf_flag_flagLength >= i_htfFMin) and (htf_flag_startIndex is not None and htf_poleLowIndex is not None and (htf_flag_startIndex - htf_poleLowIndex) <= i_htfPB)
                
                prev_is_flag = htf_history_is_flag[-1] if htf_history_is_flag else False
                breakout = (prev_htf_flag_baseHigh is not None and highs[i] > prev_htf_flag_baseHigh) and (htf_flag_flagLength >= i_htfFMin) and (htf_flag_flagBool == True)
                plotBO = prev_is_flag and (prev_htf_flag_baseHigh is not None and highs[i] > prev_htf_flag_baseHigh) and (htf_flag_flagBool == True)
                
                if plotBO:
                    htf_boBar = i
                    
                htfPostBOActive = (htf_boBar is not None) and ((i - htf_boBar) <= i_bsoMax)
                isHTF = plotBO or is_flag or htfPostBOActive
                
                if breakout:
                    htf_flag_flagLength = 0
                    htf_flag_baseHigh = highs[i]
                    htf_flag_startIndex = i
                    htf_flag_lowIndex = i
                    htf_flag_baseLow = lows[i]
            else:
                is_flag = False
                isHTF = False
                
            htf_history_is_flag.append(is_flag)
            flag_baseHigh_prev = htf_flag_baseHigh
            
            # Active pattern evaluation
            inBase = isBase or isDB or isCup or isCupH or isHTF or isAscendingBase or isConsolidation
            barsSBO = (i - boBar) if boBar is not None else None
            
            pName = 'None'
            pCode = 0
            pOn = False
            
            if inBase:
                if isHTF: pName, pCode, pOn = 'HTF', 6, True
                elif isAscendingBase: pName, pCode, pOn = 'Ascending Base', 8, True
                elif is6WkFlat: pName, pCode, pOn = '6-Wk Flat', 7, True
                elif isFlatBase: pName, pCode, pOn = 'Flat Base', 2, True
                elif isDB: pName, pCode, pOn = 'Dbl Bottom', 5, True
                elif isCupH: pName, pCode, pOn = 'Cup+Handle', 4, True
                elif isCup: pName, pCode, pOn = 'Cup', 3, True
                elif isConsolidation: pName, pCode, pOn = 'Consolidation', 9, True
                elif isDeepBase: pName, pCode, pOn = 'Base', 1, True
            else:
                if barsSBO is not None and barsSBO <= 15:
                    pCode = boPatternCode
                    pName = boPatternName
                    pOn = (pCode > 0)
                    
            # PivRef & Distance %
            if inBase:
                if isHTF: pivRef = htf_flag_baseHigh
                elif isCupH and cupHandlePivot is not None: pivRef = cupHandlePivot
                elif isDB and dbMiddlePivot is not None: pivRef = dbMiddlePivot
                else: pivRef = bTop
            else:
                pivRef = boPivot
            distPct = (closes[i] - pivRef) / pivRef * 100.0 if (pivRef and pivRef > 0) else None
            
            # Sub-signal evaluations
            volDryUp1 = (volumes[i] < sma20_vol[i] * 0.55) if (sma20_vol[i] > 0) else False
            
            pp10 = False
            pp5 = False
            if i >= 10 and closes[i] > closes[i-1]:
                max_dn_10 = np.max(down_vols[i-10:i])
                if max_dn_10 > 0 and volumes[i] > max_dn_10:
                    pp10 = True
                max_dn_5 = np.max(down_vols[i-5:i])
                if max_dn_5 > 0 and volumes[i] > max_dn_5:
                    pp5 = True
            ppAny = pp10 or pp5
            
            touchMA1 = (lows[i] <= ema10[i] * 1.025 and highs[i] >= ema10[i] * 0.975) if ema10[i] > 0 else False
            touchMA2 = (lows[i] <= ema20[i] * 1.025 and highs[i] >= ema20[i] * 0.975) if ema20[i] > 0 else False
            touchMA3 = (lows[i] <= sma50[i] * 1.025 and highs[i] >= sma50[i] * 0.975) if (not np.isnan(sma50[i]) and sma50[i] > 0) else False
            touchedMA = touchMA1 or touchMA2 or touchMA3
            
            shakeoutEntry = False
            if not np.isnan(shakeTrendEMA[i]) and closes[i] > shakeTrendEMA[i]:
                if shakeLastSwingLow and lows[i] < shakeLastSwingLow and not shakeSetupActive:
                    shakeUndercutBar = i
                    shakeSetupActive = True
                if shakeSetupActive and shakeUndercutBar and (i - shakeUndercutBar <= 3) and closes[i] > shakeEma3[i] and not shakeReclaimBar:
                    shakeReclaimBar = i
                    shakeReclaimHigh = highs[i]
                if shakeSetupActive and shakeReclaimBar and i > shakeReclaimBar and highs[i] > shakeReclaimHigh:
                    shakeoutEntry = True
                    shakeSetupActive = False
                    shakeUndercutBar = None
                    shakeReclaimBar = None
            if shakeSetupActive and shakeUndercutBar and (i - shakeUndercutBar > 3):
                shakeSetupActive = False
                
            upsideReversal = ((highs[i] - lows[i]) > atr14[i]) and (closes[i] > (highs[i] + lows[i]) / 2.0)
            
            rsWindow = inBase or (not inBase and boBar is not None and barsSBO is not None and barsSBO <= 15)
            if newBase:
                rsCount = 1 if rs_nh_any[i] else 0
            elif rsWindow and rs_nh_any[i]:
                rsCount += 1
                
            nearPivotScore = inBase and (distPct is not None and abs(distPct) <= 15.0)
            if newBase or not inBase:
                scorePP_post = False
                scoreShake_post = False
                scoreTouch_post = False
                scoreVDU_post = False
                scoreRS_post = False
                scoreUpRev_post = False
                
            if inBase:
                if ppAny: scorePP_pre = True
                if shakeoutEntry: scoreShake_pre = True
                if touchedMA: scoreTouch_pre = True
                if volDryUp1: scoreVDU_pre = True
                if rs_nh_any[i]: scoreRS_pre = True
                if upsideReversal: scoreUpRev_pre = True
                
            postBOWindowScore = (not inBase) and (barsSBO is not None and barsSBO <= 15)
            if postBOWindowScore:
                if ppAny: scorePP_post = True
                if shakeoutEntry: scoreShake_post = True
                if touchedMA: scoreTouch_post = True
                if volDryUp1: scoreVDU_post = True
                if rs_nh_any[i]: scoreRS_post = True
                if upsideReversal: scoreUpRev_post = True
                
            beforeBOScore = sum([scorePP_pre, scoreShake_pre, scoreTouch_pre, scoreVDU_pre, scoreRS_pre, scoreUpRev_pre])
            postBOScore = sum([scorePP_post, scoreShake_post, scoreTouch_post, scoreVDU_post, scoreRS_post, scoreUpRev_post])
            compositeScore = beforeBOScore + postBOScore
            
            state = {
                'bar': i,
                'date': str(df.index[i])[:10],
                'close': float(closes[i]),
                'inBase': inBase,
                'isHTF': isHTF,
                'pName': pName,
                'pCode': pCode,
                'pOn': pOn,
                'bCount': bCount if inBase else None,
                'barsSBO': barsSBO,
                'distPct': float(distPct) if distPct is not None else None,
                'beforeBOScore': beforeBOScore,
                'postBOScore': postBOScore,
                'compositeScore': compositeScore,
                'rsCount': rsCount,
                'volDryUp': volDryUp1,
                'ppAny': ppAny,
                'touchedMA': touchedMA,
                'shakeoutEntry': shakeoutEntry,
                'upsideReversal': upsideReversal,
                'rsNH': bool(rs_nh_any[i])
            }
            history_state.append(state)
            
        latest = history_state[-1]
        
        # Calculate % Off 52W High on latest bar
        high252 = np.max(highs[max(0, n-252):n])
        pctOff52wHigh = (high252 - closes[-1]) / high252 * 100.0 if high252 > 0 else 0.0
        
        # Filter for active tickers: either currently in pattern base or post-breakout within 15 bars
        if latest['pOn'] and (latest['pCode'] > 0):
            result = {
                'ticker': str(ticker),
                'date': str(latest['date']),
                'close': float(round(latest['close'], 2)),
                'pattern_name': str(latest['pName']),
                'pattern_code': int(latest['pCode']),
                'status': 'In Base' if latest['inBase'] else 'Post-BO',
                'days_in_base': int(latest['bCount']) if latest['bCount'] is not None else None,
                'bars_sbo': int(latest['barsSBO']) if latest['barsSBO'] is not None else None,
                'dist_pct': float(round(latest['distPct'], 2)) if latest['distPct'] is not None else None,
                'pct_off_52w_high': float(round(pctOff52wHigh, 2)),
                'before_bo_score': int(latest['beforeBOScore']),
                'post_bo_score': int(latest['postBOScore']),
                'composite_score': int(latest['compositeScore']),
                'rs_nh_count': int(latest['rsCount']),
                'vol_dry_up': bool(latest['volDryUp']),
                'pocket_pivot': bool(latest['ppAny']),
                'touched_ma': bool(latest['touchedMA']),
                'shakeout_entry': bool(latest['shakeoutEntry']),
                'upside_reversal': bool(latest['upsideReversal']),
                'rs_nh': bool(latest['rsNH'])
            }
            return result
        return None
        
    except Exception as e:
        return None


def run_ibd_pattern_scan(max_workers: int = None):
    """Run full scan over all parquet files in ticker_cache/."""
    if max_workers is None:
        max_workers = os.cpu_count() or 8
        
    start_time = time.time()
    files = glob.glob(str(TICKER_CACHE_DIR / "*_1d.parquet"))
    print(f"🔍 Found {len(files)} ticker cache files in {TICKER_CACHE_DIR}")
    
    # Load SPY close series if available
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    spy_close = None
    if spy_path.exists():
        try:
            spy_df = pd.read_parquet(spy_path)
            spy_close = spy_df['Close']
        except Exception:
            pass

    results = []
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for f in files:
            ticker = Path(f).name.split("_1d.parquet")[0]
            if ticker in ["SPY", "QQQ", "IWM"]:
                continue
            fut = executor.submit(scan_single_ticker, ticker, f, spy_close)
            futures[fut] = ticker
            
        count = 0
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                results.append(res)
            count += 1

    # Sort results by composite_score desc, pattern_code desc, ticker asc
    results.sort(key=lambda x: (-x['composite_score'], x['pattern_code'], x['ticker']))
    
    elapsed = time.time() - start_time
    print(f"✅ Scan completed in {elapsed:.2f} seconds! Found {len(results)} pattern signals.")
    
    # Save to JSON
    OUTPUT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        
    print(f"💾 Results saved to {OUTPUT_JSON_PATH}")
    return results


if __name__ == "__main__":
    run_ibd_pattern_scan()
