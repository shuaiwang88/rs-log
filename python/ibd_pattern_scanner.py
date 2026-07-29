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
import json
import time
import math
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

# Root project directory
ROOT_DIR = Path(__file__).resolve().parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"
OUTPUT_JSON_PATH = ROOT_DIR / "python" / "ibd_pattern_results.json"


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
        pivLag = 25  # 5 * barsPerWeek
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
                lastBTop = bTop
                if lows[i] < bLow and bTop is not None and lows[i] >= bTop * (1.0 - bdF):
                    bLow = lows[i]
                    
            # Invalidation: too deep or too long
            if isBase and bTop is not None:
                if lows[i] < bTop * (1.0 - bdF) or bCount > bLenB:
                    isBase = False
                    
            # Breakout: price clears the base top (matching drw_pattern_scanner.pine lines 280-282)
            if isBase and bTop is not None and highs[i] > bTop:
                isBase = False
                boPivot = bTop
                boBar = i
                boPatternCode = history_state[-1]['pCode'] if history_state else 0
                boPatternName = history_state[-1]['pName'] if history_state else 'None'
                
            # Breakout tracking flag
            was_in_base = prev_isBase
            activeBTop = flag_baseHigh_prev if 'flag_baseHigh_prev' in locals() and history_state and history_state[-1]['isHTF'] else lastBTop
            
            if was_in_base and not isBase and boBar != i and activeBTop is not None and highs[i] > activeBTop:
                boPivot = activeBTop
                boBar = i
                boPatternCode = history_state[-1]['pCode'] if history_state else 0
                boPatternName = history_state[-1]['pName'] if history_state else 'None'
                
            if newBase:
                boPivot = None
                boBar = None
                boPatternCode = 0
                boPatternName = 'None'
                
            # Depth Pct
            bDepPct = (bTop - bLow) / bTop * 100.0 if (bTop and bLow and bTop > 0) else None
            isFlatBase = isBase and (bDepPct is not None and bDepPct <= 15.0)
            isDeepBase = isBase and (bDepPct is not None and bDepPct > 15.0)
            is6WkFlat = isFlatBase and (25 <= bCount <= 35)
            
            # Double Bottom Detection
            isDB = False
            if isBase and len(aHP_list) >= 2 and len(aLP_list) >= 2 and bTop is not None:
                fH = aHP_list[1][1]
                sH = aHP_list[0][1]
                fL = aLP_list[1][1]
                sL = aLP_list[0][1]
                fHt = aHP_list[1][0]
                sHt = aHP_list[0][0]
                fLt = aLP_list[1][0]
                sLt = aLP_list[0][0]
                
                cPT = (fH >= np.min(lows[max(0, i-250):i+1]) * 1.20)
                cPH = abs(fH - bTop) < bTop * 0.01
                cA = (sL <= fL * 1.03)
                cB = (sL >= (1 - bdF) * fH)
                cC = (sL <= fH * 0.90)
                cD = (sH >= sL + (fH - sL) * 0.35)
                cE = (sH < fH * 1.01)
                cF = (sH >= fL + (fH - fL) * 0.40)
                fd = fH - fL
                sd = sH - sL
                cG = (fd > 0 and sd > 0 and fd/sd <= 3.0 and sd/fd <= 3.0)
                
                cTA = (fHt < sLt < sHt < fLt)
                cTB = (fLt - fHt <= bLenB)
                cTC = (fLt - fHt >= 10)
                cTD = ((sLt - fHt) >= 3 and (sHt - sLt) >= 3 and (fLt - sHt) >= 3)
                cTE = (1.0/4.0 <= (sHt - fHt) / max(1, i - sHt) <= 4.0) if (i - sHt) > 0 else False
                
                highest_since_2nd_low = np.max(highs[sLt:i+1]) if sLt <= i else highs[i]
                cSh = (highest_since_2nd_low <= sH)
                
                if cPT and cPH and cA and cB and cC and cD and cE and cF and cG and cTA and cTB and cTC and cTD and cTE and cSh:
                    isDB = True
                    
            # Cup & Cup with Handle Detection
            isCup = False
            isCupH = False
            cupMid = bLow + (bTop - bLow) * 0.5 if (bTop and bLow) else None
            
            if isBase and not isDB and bTop and bLow and bCount >= 30:
                depOk = (bLow >= (1.0 - bdF) * bTop) and (bLow <= 0.92 * bTop)
                lenOk = (bCount <= bLenB)
                posOk = (cupMid <= highs[i]) if cupMid else False
                
                tier = max(1, bCount // 3)
                start_base_bar = i - bCount + 1
                if start_base_bar >= 0:
                    base_closes = closes[start_base_bar:i+1]
                    if len(base_closes) >= bCount:
                        v1_closes = base_closes[:tier]
                        v2_closes = base_closes[tier:2*tier]
                        
                        cT2 = (np.sum(v1_closes >= cupMid) / float(tier)) >= 0.30 if cupMid else False
                        cT = (np.sum(v2_closes <= cupMid) / float(tier)) >= 0.85 if cupMid else False
                        
                        if depOk and lenOk and posOk and cT and cT2:
                            isCup = True
                            
            if isCup and bTop and bLow and cupMid and bCount > 12:
                w12_start = max(0, i - 12 + 1)
                H12 = np.max(highs[w12_start:i+1])
                L12 = np.min(lows[w12_start:i+1])
                hDep = (H12 - L12) / H12 * 100.0 if H12 > 0 else 999.0
                inTop = (L12 >= cupMid)
                depOk_h = (hDep <= 15.0)
                if inTop and depOk_h:
                    isCupH = True
                    
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
            inBase = isBase or isDB or isCup or isCupH or isHTF
            barsSBO = (i - boBar) if boBar is not None else None
            
            pName = 'None'
            pCode = 0
            pOn = False
            
            if inBase:
                if isHTF: pName, pCode, pOn = 'HTF', 6, True
                elif isCupH: pName, pCode, pOn = 'Cup+Handle', 4, True
                elif isDB: pName, pCode, pOn = 'Dbl Bottom', 5, True
                elif isCup: pName, pCode, pOn = 'Cup', 3, True
                elif is6WkFlat: pName, pCode, pOn = '6-Wk Flat', 7, True
                elif isFlatBase: pName, pCode, pOn = 'Flat Base', 2, True
                elif isDeepBase: pName, pCode, pOn = 'Base', 1, True
            else:
                if barsSBO is not None and barsSBO <= 15:
                    pCode = boPatternCode
                    pName = boPatternName
                    pOn = (pCode > 0)
                    
            # PivRef & Distance %
            pivRef = (htf_flag_baseHigh if isHTF else bTop) if inBase else boPivot
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
