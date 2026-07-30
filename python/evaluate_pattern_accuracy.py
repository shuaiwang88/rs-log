"""
Evaluate ibd_pattern_scanner accuracy against IBD Breakaway Gap event dates.
For each ticker+event_date in the CSV, runs the scanner logic and checks
whether the scanner detects the expected pattern on that date.
"""

import os
import sys
import csv
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
TICKER_CACHE_DIR = ROOT_DIR / "ticker_cache"


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


def find_pivots(highs, lows, left=5, right=5):
    n = len(highs)
    pivot_highs = {}
    pivot_lows = {}
    for i in range(left, n - right):
        h = highs[i]
        l = lows[i]
        is_ph = True
        for j in range(i - left, i + right + 1):
            if j != i and highs[j] >= h:
                is_ph = False
                break
        if is_ph:
            pivot_highs[i] = h
        is_pl = True
        for j in range(i - left, i + right + 1):
            if j != i and lows[j] <= l:
                is_pl = False
                break
        if is_pl:
            pivot_lows[i] = l
    return pivot_highs, pivot_lows


def normalize_pattern_name(name):
    m = {
        "Flat Base": "Flat Base",
        "Ascending Base": "Ascending Base",
        "Cup Without Handle": "Cup",
        "Cup With Handle": "Cup+Handle",
        "Double Bottom": "Dbl Bottom",
        "Cup without handle": "Cup",
        "Cup with handle": "Cup+Handle",
        "Consolidation": "Consolidation",
    }
    return m.get(name.strip(), name.strip())


def expected_pattern_code(name):
    m = {
        "Flat Base": 2,
        "Ascending Base": 8,
        "Cup": 3,
        "Cup+Handle": 4,
        "Dbl Bottom": 5,
        "Consolidation": 9,
    }
    return m.get(normalize_pattern_name(name), 0)


def parse_csv(csv_path):
    entries = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("Symbol", "").strip()
            event_date = row.get("Event Date", "").strip()
            pattern = row.get("Daily Base Type", "").strip()
            if symbol and event_date and pattern:
                try:
                    parts = event_date.split("/")
                    if len(parts) == 3:
                        month = parts[0].zfill(2)
                        day = parts[1].zfill(2)
                        year = parts[2]
                        if len(year) == 2:
                            year = "20" + year
                        formatted = f"{year}-{month}-{day}"
                        entries.append((symbol, formatted, pattern, row))
                except Exception:
                    pass
    return entries


def evaluate():
    csv_path = ROOT_DIR / "IBD" / "Breakaway Gap.csv"
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return

    entries = parse_csv(csv_path)
    print(f"Parsed {len(entries)} entries from CSV")

    # Load SPY for RS calculation
    spy_path = TICKER_CACHE_DIR / "SPY_1d.parquet"
    spy_close = None
    if spy_path.exists():
        try:
            spy_df = pd.read_parquet(spy_path)
            spy_close = spy_df["Close"]
        except Exception:
            pass

    results = []
    ticker_entries = {}
    for sym, date, pat, row in entries:
        ticker_entries.setdefault(sym, []).append((date, pat, row))

    tickers_processed = 0
    tickers_missing = 0
    tickers_skipped = 0

    for ticker, evts in ticker_entries.items():
        file_path = TICKER_CACHE_DIR / f"{ticker}_1d.parquet"
        if not file_path.exists():
            tickers_missing += 1
            for date, pat, row in evts:
                results.append(
                    {
                        "ticker": ticker,
                        "event_date": date,
                        "expected_pattern": pat,
                        "detected_pattern": None,
                        "detected_code": None,
                        "expected_code": expected_pattern_code(pat),
                        "match": False,
                        "error": "No data file found",
                    }
                )
            continue

        try:
            df = pd.read_parquet(file_path)
        except Exception as e:
            tickers_skipped += 1
            for date, pat, row in evts:
                results.append(
                    {
                        "ticker": ticker,
                        "event_date": date,
                        "expected_pattern": pat,
                        "detected_pattern": None,
                        "detected_code": None,
                        "expected_code": expected_pattern_code(pat),
                        "match": False,
                        "error": f"Read error: {e}",
                    }
                )
            continue

        if df.empty or len(df) < 60:
            tickers_skipped += 1
            for date, pat, row in evts:
                results.append(
                    {
                        "ticker": ticker,
                        "event_date": date,
                        "expected_pattern": pat,
                        "detected_pattern": None,
                        "detected_code": None,
                        "expected_code": expected_pattern_code(pat),
                        "match": False,
                        "error": "Insufficient data",
                    }
                )
            continue

        # Run scanner
        state_by_date = run_scanner(df, ticker, spy_close)
        tickers_processed += 1

        for date, pat, row in evts:
            evt_dt = pd.Timestamp(date)
            detected = None
            detected_code = None
            match = False
            detected_pivot = None
            csv_pivot = None

            try:
                csv_pivot = float(row.get("Pivot Price", "").replace(",", "").strip())
            except (ValueError, AttributeError):
                pass

            if state_by_date:
                dates = sorted(state_by_date.keys())
                closest = min(dates, key=lambda d: abs((d - evt_dt).total_seconds()))
                day_diff = abs((closest - evt_dt).days)
                if day_diff <= 5:
                    st = state_by_date[closest]
                    detected = st["pName"]
                    detected_code = st["pCode"]
                    detected_pivot = st["pivRef"]
                    exp_code = expected_pattern_code(pat)
                    match = detected_code == exp_code

            pivot_match_pct = None
            if csv_pivot is not None and detected_pivot is not None and detected_pivot > 0:
                pivot_match_pct = abs(detected_pivot - csv_pivot) / csv_pivot * 100

            results.append(
                {
                    "ticker": ticker,
                    "event_date": date,
                    "expected_pattern": pat,
                    "detected_pattern": detected,
                    "detected_code": detected_code,
                    "expected_code": expected_pattern_code(pat),
                    "match": match,
                    "csv_pivot": csv_pivot,
                    "detected_pivot": detected_pivot,
                    "pivot_diff_pct": round(pivot_match_pct, 2) if pivot_match_pct is not None else None,
                }
            )

    # Summary
    total = len(results)
    matched = sum(1 for r in results if r["match"])
    unmatched = total - matched
    pct = matched / total * 100 if total > 0 else 0

    print(f"\n{'='*70}")
    print(f"PATTERN ACCURACY: {matched}/{total} matched ({pct:.1f}%)")
    print(f"{'='*70}")

    # Pivot accuracy
    pivot_diffs = [r["pivot_diff_pct"] for r in results if r["pivot_diff_pct"] is not None]
    if pivot_diffs:
        pivot_within_1pct = sum(1 for d in pivot_diffs if d <= 1.0)
        pivot_within_3pct = sum(1 for d in pivot_diffs if d <= 3.0)
        pivot_within_5pct = sum(1 for d in pivot_diffs if d <= 5.0)
        pivot_within_10pct = sum(1 for d in pivot_diffs if d <= 10.0)
        pivot_median = sorted(pivot_diffs)[len(pivot_diffs)//2]
        print(f"\nPIVOT ACCURACY ({len(pivot_diffs)} entries with pivot data):")
        print(f"  Within  1%: {pivot_within_1pct:>3}/{len(pivot_diffs):>3} ({pivot_within_1pct/len(pivot_diffs)*100:.1f}%)")
        print(f"  Within  3%: {pivot_within_3pct:>3}/{len(pivot_diffs):>3} ({pivot_within_3pct/len(pivot_diffs)*100:.1f}%)")
        print(f"  Within  5%: {pivot_within_5pct:>3}/{len(pivot_diffs):>3} ({pivot_within_5pct/len(pivot_diffs)*100:.1f}%)")
        print(f"  Within 10%: {pivot_within_10pct:>3}/{len(pivot_diffs):>3} ({pivot_within_10pct/len(pivot_diffs)*100:.1f}%)")
        print(f"  Median diff: {pivot_median:.2f}%")
        large_pivots = [(r["ticker"], r["event_date"], r["csv_pivot"], r["detected_pivot"], r["pivot_diff_pct"]) for r in results if r["pivot_diff_pct"] is not None and r["pivot_diff_pct"] > 10]
        if large_pivots:
            print(f"\n  Entries with >10% pivot diff ({len(large_pivots)}):")
            for t, d, cp, dp, diff in sorted(large_pivots, key=lambda x: -x[4])[:10]:
                print(f"    {t:6} {d}  CSV pivot={cp:>8}  Detected pivot={dp:>8}  diff={diff:.1f}%")

    # By pattern type
    from collections import Counter

    by_pattern = {}
    for r in results:
        pat = r["expected_pattern"]
        by_pattern.setdefault(pat, {"total": 0, "matched": 0, "details": []})
        by_pattern[pat]["total"] += 1
        by_pattern[pat]["details"].append(r)
        if r["match"]:
            by_pattern[pat]["matched"] += 1

    print(f"\nResults by pattern type:")
    print(f"{'Pattern':<25} {'Total':>6} {'Matched':>8} {'Rate':>8}")
    print("-" * 50)
    for pat, d in sorted(by_pattern.items(), key=lambda x: -x[1]["total"]):
        rate = d["matched"] / d["total"] * 100 if d["total"] > 0 else 0
        print(f"{pat:<25} {d['total']:>6} {d['matched']:>8} {rate:>7.1f}%")

    # Show unmatched
    print(f"\nUnmatched entries ({unmatched}):")
    print(f"{'Ticker':<8} {'Event Date':<14} {'Expected':<20} {'Detected':<20}")
    print("-" * 65)
    for r in results:
        if not r["match"]:
            det = r["detected_pattern"] or "N/A"
            print(
                f"{r['ticker']:<8} {r['event_date']:<14} {r['expected_pattern']:<20} {det:<20}"
            )

    print(f"\nTickers: {tickers_processed} processed, {tickers_missing} missing, {tickers_skipped} skipped")
    return results


def run_scanner(df, ticker, spy_close_series=None):
    """Run scanner and return dict of date -> state for all bars."""
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    for col in required_cols:
        if col not in df.columns:
            return None

    df = df.sort_index()
    if len(df) > 1500:
        df = df.iloc[-1500:]

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    volumes = df["Volume"].values
    opens = df["Open"].values
    n = len(df)

    barsPerWeek = 5
    pivLag = 5
    win13wk = 65
    win20wk = 100
    bLenB = 325
    bdF = 0.50
    pivLen = 5

    close_series = pd.Series(closes)
    ema10 = close_series.ewm(span=10, adjust=False).mean().values
    ema20 = close_series.ewm(span=20, adjust=False).mean().values
    sma50 = close_series.rolling(50, min_periods=10).mean().values
    sma20_vol = pd.Series(volumes).rolling(20, min_periods=5).mean().values
    atr14 = calculate_atr(highs, lows, closes, 14)

    rs_raw = None
    if spy_close_series is not None and not spy_close_series.empty:
        aligned_spy = spy_close_series.reindex(df.index).ffill().bfill().values
        if len(aligned_spy) == n and np.all(aligned_spy > 0):
            rs_raw = closes * 7.0 * 1000.0 / aligned_spy
    if rs_raw is None:
        rs_raw = closes.copy()

    rs_s = pd.Series(rs_raw, index=df.index)
    rs_h1y = rs_s.shift(1).rolling(min(252, n), min_periods=30).max().values
    rs_h6m = rs_s.shift(1).rolling(min(126, n), min_periods=20).max().values
    rs_h3m = rs_s.shift(1).rolling(min(63, n), min_periods=10).max().values

    rs_nh_1y = (rs_raw > rs_h1y) & (~np.isnan(rs_h1y))
    rs_nh_6m = (rs_raw > rs_h6m) & (~np.isnan(rs_h6m))
    rs_nh_3m = (rs_raw > rs_h3m) & (~np.isnan(rs_h3m))
    rs_nh_any = rs_nh_1y | rs_nh_6m | rs_nh_3m

    pivot_highs, pivot_lows = find_pivots(highs, lows, pivLen, pivLen)

    aHP_list = []
    aLP_list = []

    bTop = None
    bLow = None
    bStart = None
    isBase = False
    bCount = 0
    lastBTop = None

    boPivot = None
    boBar = None
    boPatternCode = 0
    boPatternName = "None"



    rsCount = 0

    history_state = []

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

    down_vols = np.where(pd.Series(closes).diff() < 0, volumes, 0.0)

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

    shakeTrendEMA = sma50
    shakeEma3 = close_series.ewm(span=3, adjust=False).mean().values
    shakeLastSwingLow = None
    shakeUndercutBar = None
    shakeReclaimBar = None
    shakeReclaimHigh = None
    shakeSetupActive = False

    for i in range(n):
        current_bar = i
        conf_bar = i - pivLen
        if conf_bar in pivot_highs:
            aHP_list.insert(0, (conf_bar, pivot_highs[conf_bar]))
        if conf_bar in pivot_lows:
            aLP_list.insert(0, (conf_bar, pivot_lows[conf_bar]))
            shakeLastSwingLow = pivot_lows[conf_bar]

        w25_start = max(0, i - pivLag + 1)
        H25 = np.max(highs[w25_start : i + 1]) if i >= w25_start else highs[i]
        L25 = np.min(lows[w25_start : i + 1]) if i >= w25_start else lows[i]

        w103_start = max(0, i - 103 + 1)
        L103 = np.min(lows[w103_start : i + 1])

        shift_idx = i - pivLag - 1
        if shift_idx >= 0:
            w65_start = max(0, shift_idx - 65 + 1)
            H65s = np.max(highs[w65_start : shift_idx + 1])
        else:
            H65s = highs[i]

        newBase = False
        if len(aHP_list) >= 3 and i >= pivLag:
            piv_idx = i - pivLag
            piv_h = highs[piv_idx]

            recent_hp_prices = [p for b, p in aHP_list[:3]]
            bH = any(abs(piv_h - p) < 1e-4 for p in recent_hp_prices)
            lUp = L103 * 1.20 <= piv_h
            dep = piv_h * (1.0 - bdF) <= L25
            noAb = H25 <= piv_h

            noNe = True
            if len(history_state) >= pivLag:
                noNe = not history_state[i - pivLag]["inBase"]

            bPH = (piv_h > H65s) or (bTop is not None and piv_h > bTop)
            newBase = bH and bPH and lUp and dep and noAb and noNe

        prev_isBase = history_state[-1]["inBase"] if history_state else False

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

        if isBase and bTop is not None:
            if lows[i] < bTop * (1.0 - bdF) or bCount > bLenB:
                isBase = False

        bDepPct = (bTop - bLow) / bTop * 100.0 if (bTop and bLow and bTop > 0) else None

        recent_win = min(i + 1, max(20, min(bCount, 65)))
        rTop = np.max(highs[max(0, i - recent_win + 1) : i + 1])
        rLow = np.min(lows[max(0, i - recent_win + 1) : i + 1])
        rDepPct = (rTop - rLow) / rTop * 100.0 if rTop > 0 else 0.0

        isFlatBase = isBase and (rDepPct <= 18.0) and (bCount >= 25)
        isDeepBase = isBase and not isFlatBase
        is6WkFlat = isFlatBase and (25 <= recent_win <= 35)

        isDB = False
        dbMiddlePivot = None
        dbMaxBars = 85
        if isBase and len(aHP_list) >= 2 and len(aLP_list) >= 2:
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
                        prL250 = np.min(lows[max(0, i - 250) : i + 1])
                        cPT = (fH >= prL250 * 1.15) or (sH >= prL250 * 1.15)
                        cPH = (bTop is None) or (abs(fH - bTop) < bTop * 0.15) or (abs(sH - bTop) < bTop * 0.15)
                        cA = (sL <= fL * 1.03) and (sL >= fL * 0.85)
                        cB = sL >= (1 - bdF) * peak
                        cC = sL <= peak * 0.95
                        cD = sH >= sL + (peak - sL) * 0.20
                        cE = sH <= fH * 1.10
                        cF = sH >= fL + (fH - fL) * 0.40

                        fd = fH - fL
                        sd = sH - sL
                        cG = (fd > 0 and sd > 0)

                        cTA = (fH_t < fLt < sH_t < sLt)
                        cTB = (sLt - fH_t <= dbMaxBars) and (i - fH_t <= dbMaxBars)
                        cTC = (sLt - fH_t >= 5)

                        highest_since_2nd_low = np.max(highs[sLt:i]) if sLt < i else highs[i - 1]
                        cSh = highest_since_2nd_low <= sH * 1.02

                        if cPT and cPH and cA and cB and cC and cD and cE and cF and cG and cTA and cTB and cTC and cSh:
                            isDB = True
                            dbMiddlePivot = sH
                            break
                if isDB:
                    break

        cupHandlePivot = None
        isCup = False
        isCupH = False
        cupMid = bLow + (bTop - bLow) * 0.5 if (bTop and bLow) else None

        if isBase and bTop and bLow and bCount >= 20 and not isDB:
            depOk = (bLow >= 0.50 * bTop) and (bLow <= 0.92 * bTop)
            lenOk = bCount <= 200
            if depOk and lenOk:
                isCup = True

        if isCup and bTop and bLow and cupMid and bCount > 10:
            handle_len = min(30, max(5, bCount // 3))
            end_h = i
            w12_start = max(0, end_h - handle_len)
            H12 = np.max(highs[w12_start:end_h+1]) if end_h >= w12_start else highs[i]
            L12 = np.min(lows[w12_start:end_h+1])
            hDep = (H12 - L12) / H12 * 100.0 if H12 > 0 else 999.0
            inTop = L12 >= cupMid * 0.85
            depOk_h = 2.0 <= hDep <= 25.0
            if inTop and depOk_h and H12 < bTop * 1.02:
                isCupH = True
                cupHandlePivot = H12

        isAscendingBase = False
        if isBase and not isCup and not isCupH and not isDB and len(aHP_list) >= 3 and len(aLP_list) >= 3:
            recent_hps = [p for p in aHP_list if p[0] >= i - 90][:3]
            recent_lps = [p for p in aLP_list if p[0] >= i - 90][:3]
            if len(recent_hps) == 3 and len(recent_lps) == 3:
                recent_hps.sort(key=lambda x: x[0])
                recent_lps.sort(key=lambda x: x[0])
                h1, h2, h3 = recent_hps[0][1], recent_hps[1][1], recent_hps[2][1]
                l1, l2, l3 = recent_lps[0][1], recent_lps[1][1], recent_lps[2][1]
                hh = (h1 < h2 < h3) or (h3 >= h1 * 1.01 and h2 >= h1 * 0.98)
                hl = (l1 < l2 < l3) or (l3 >= l1 * 1.01 and l2 >= l1 * 0.98)
                pb1, pb2, pb3 = (h1 - l1) / h1, (h2 - l2) / h2, (h3 - l3) / h3
                pb_ok = all(0.05 <= p <= 0.25 for p in [pb1, pb2, pb3])
                if hh and hl and pb_ok:
                    isAscendingBase = True

        isConsolidation = isBase and not isCup and not isCupH and not isFlatBase and not isDB and not isAscendingBase and (bDepPct is not None and 18.1 <= bDepPct <= 50.0) and (25 <= bCount <= 300)

        # --- Breakout Check ---
        active_pivot = dbMiddlePivot if (isDB and dbMiddlePivot is not None) else (cupHandlePivot if (isCupH and cupHandlePivot is not None) else bTop)
        if isBase and active_pivot is not None and highs[i] > active_pivot:
            isBase = False
            boPivot = active_pivot
            boBar = i
            if isAscendingBase: boPatternCode, boPatternName = 8, 'Ascending Base'
            elif is6WkFlat: boPatternCode, boPatternName = 7, '6-Wk Flat'
            elif isFlatBase: boPatternCode, boPatternName = 2, 'Flat Base'
            elif isDB: boPatternCode, boPatternName = 5, 'Dbl Bottom'
            elif isCupH: boPatternCode, boPatternName = 4, 'Cup+Handle'
            elif isCup: boPatternCode, boPatternName = 3, 'Cup'
            elif isConsolidation: boPatternCode, boPatternName = 9, 'Consolidation'
            else: boPatternCode, boPatternName = 1, 'Base'

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

        # --- HTF ---
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
            lower_close = (closes[i] < closes[i - 1]) if i > 0 else False

            if (highs[i] < htf_flag_baseHigh and findDepth <= i_htfRet) or (highs[i] == htf_flag_baseHigh and lower_close):
                htf_flag_flagLength += 1
            else:
                htf_flag_flagLength = 0

            if not htf_flag_flagBool or highs[i] == htf_flag_baseHigh:
                searchBars = min(i_htfPB, i - 1)
                if searchBars >= i_htfPBMin:
                    minLow = lows[i - 1]
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

        inBase = isBase or isDB or isCup or isCupH or isHTF or isAscendingBase or isConsolidation
        barsSBO = (i - boBar) if boBar is not None else None

        pName = "None"
        pCode = 0
        pOn = False

        if inBase:
            if isHTF:
                pName, pCode, pOn = "HTF", 6, True
            elif isAscendingBase:
                pName, pCode, pOn = "Ascending Base", 8, True
            elif isCupH:
                pName, pCode, pOn = "Cup+Handle", 4, True
            elif isDB:
                pName, pCode, pOn = "Dbl Bottom", 5, True
            elif is6WkFlat:
                pName, pCode, pOn = "6-Wk Flat", 7, True
            elif isFlatBase:
                pName, pCode, pOn = "Flat Base", 2, True
            elif isCup:
                pName, pCode, pOn = "Cup", 3, True
            elif isConsolidation:
                pName, pCode, pOn = "Consolidation", 9, True
            elif isDeepBase:
                pName, pCode, pOn = "Base", 1, True
        else:
            if barsSBO is not None and barsSBO <= 15:
                pCode = boPatternCode
                pName = boPatternName
                pOn = pCode > 0

        if inBase:
            if isHTF and htf_flag_baseHigh:
                pivRef = htf_flag_baseHigh
            elif isCupH and cupHandlePivot:
                pivRef = cupHandlePivot
            elif isDB and dbMiddlePivot:
                pivRef = dbMiddlePivot
            else:
                pivRef = bTop
        else:
            pivRef = boPivot
        distPct = (closes[i] - pivRef) / pivRef * 100.0 if (pivRef and pivRef > 0) else None

        volDryUp1 = (volumes[i] < sma20_vol[i] * 0.55) if (sma20_vol[i] > 0) else False

        pp10 = False
        pp5 = False
        if i >= 10 and closes[i] > closes[i - 1]:
            max_dn_10 = np.max(down_vols[i - 10 : i])
            if max_dn_10 > 0 and volumes[i] > max_dn_10:
                pp10 = True
            max_dn_5 = np.max(down_vols[i - 5 : i])
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
            if ppAny:
                scorePP_pre = True
            if shakeoutEntry:
                scoreShake_pre = True
            if touchedMA:
                scoreTouch_pre = True
            if volDryUp1:
                scoreVDU_pre = True
            if rs_nh_any[i]:
                scoreRS_pre = True
            if upsideReversal:
                scoreUpRev_pre = True

        postBOWindowScore = (not inBase) and (barsSBO is not None and barsSBO <= 15)
        if postBOWindowScore:
            if ppAny:
                scorePP_post = True
            if shakeoutEntry:
                scoreShake_post = True
            if touchedMA:
                scoreTouch_post = True
            if volDryUp1:
                scoreVDU_post = True
            if rs_nh_any[i]:
                scoreRS_post = True
            if upsideReversal:
                scoreUpRev_post = True

        beforeBOScore = sum([scorePP_pre, scoreShake_pre, scoreTouch_pre, scoreVDU_pre, scoreRS_pre, scoreUpRev_pre])
        postBOScore = sum([scorePP_post, scoreShake_post, scoreTouch_post, scoreVDU_post, scoreRS_post, scoreUpRev_post])
        compositeScore = beforeBOScore + postBOScore

        state = {
            "bar": i,
            "date": str(df.index[i])[:10],
            "close": float(closes[i]),
            "inBase": inBase,
            "isHTF": isHTF,
            "pName": pName,
            "pCode": pCode,
            "pOn": pOn,
            "bCount": bCount if inBase else None,
            "barsSBO": barsSBO,
            "distPct": float(distPct) if distPct is not None else None,
            "beforeBOScore": beforeBOScore,
            "postBOScore": postBOScore,
            "compositeScore": compositeScore,
            "rsCount": rsCount,
            "volDryUp": volDryUp1,
            "ppAny": ppAny,
            "touchedMA": touchedMA,
            "shakeoutEntry": shakeoutEntry,
            "upsideReversal": upsideReversal,
            "rsNH": bool(rs_nh_any[i]),
            "pivRef": float(pivRef) if pivRef is not None and pivRef > 0 else None,
        }
        history_state.append(state)

    # Return dict date->state
    return {pd.Timestamp(s["date"]): s for s in history_state}


if __name__ == "__main__":
    evaluate()
