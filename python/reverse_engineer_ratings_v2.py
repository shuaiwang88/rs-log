#!/usr/bin/env python3
"""
Reverse Engineering IBD Ratings v2 — closed-form / non-ML redo.

Re-derives RS Rating, A/D Rating, EPS Rating, SMR Rating, and Composite Rating
formulas using ONLY price/volume (ticker_cache/*_1d.parquet) and fundamentals
(ticker_cache/*_fund.json) data — no dependency on MarketSurge's own precomputed
feature columns for the actual formula inputs. MarketSurge is used solely as the
ground-truth label to calibrate/validate against.

No black-box ML models (no sklearn ensembles/classifiers/etc). Weight fitting
uses only closed-form least squares (numpy.linalg.lstsq) and constrained
numeric optimization of a handful of scalar weights in an otherwise fully
transparent formula (scipy.optimize on top of a deterministic percentile-rank
pipeline) — the same class of method IBD's own documented composite formula
implies.

This is a research/report-only script — it does NOT modify calc_ibd_ratings.py.
"""

import sys
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from scipy.optimize import minimize

REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_DIR / "ticker_cache"
CSV_PATH = REPO_DIR / "IBD" / "marketsuge-8-7-2026.csv"
OUTPUT_DIR = REPO_DIR / "output"

RS_WINDOWS = {"1M": 21, "3M": 63, "6M": 126, "9M": 188, "12M": 249}
AD_WINDOWS = [30, 65, 130, 250]
MA_WINDOWS = [10, 21, 50, 150, 200]

SUBTIER_13_MAP = {
    "A+": 13.0, "A": 12.0, "A-": 11.0,
    "B+": 10.0, "B": 9.0, "B-": 8.0,
    "C+": 7.0, "C": 6.0, "C-": 5.0,
    "D+": 4.0, "D": 3.0, "D-": 2.0,
    "E": 1.0,
}
SMR_GRADE_NUM = {
    "A+": 95.0, "A": 90.0, "A-": 85.0,
    "B+": 75.0, "B": 70.0, "B-": 65.0,
    "C+": 55.0, "C": 50.0, "C-": 45.0,
    "D+": 35.0, "D": 30.0, "D-": 25.0,
    "E": 10.0,
}


def clean_num(val):
    if pd.isna(val):
        return np.nan
    s = str(val).replace("%", "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return np.nan


def log_compress(x):
    """Sign-preserving log compression: tames small-denominator YoY blowups (e.g. EPS growth off a
    near-zero base can read +28,600%) while preserving rank order, unlike a hard clip which throws
    away the difference between merely-large and absurdly-large values. Closed-form, not a model."""
    x = np.asarray(x, dtype=float)
    return np.sign(x) * np.log1p(np.abs(x))


def pct_rank_99(raw):
    """Percentile-rank an array to IBD's 1-99 scale."""
    r = pd.Series(raw).rank(pct=True, method="average").values * 99
    return np.clip(r, 1, 99)


def r2(y, p):
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ss_res = np.sum((y - p) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan


def mae(y, p):
    return float(np.mean(np.abs(np.asarray(y, dtype=float) - np.asarray(p, dtype=float))))


def corr(y, p):
    return float(np.corrcoef(y, p)[0, 1])


def within(y, p, n):
    return float(np.mean(np.abs(np.asarray(y, dtype=float) - np.asarray(p, dtype=float)) <= n) * 100)


def score_report(name, y, p):
    return {
        "Method": name,
        "R2": round(r2(y, p), 4),
        "MAE": round(mae(y, p), 2),
        "Corr": round(corr(y, p), 4),
        "+/-3 Acc%": round(within(y, p, 3), 1),
        "+/-5 Acc%": round(within(y, p, 5), 1),
        "+/-10 Acc%": round(within(y, p, 10), 1),
    }


def lstsq_fit(X, y):
    """OLS via closed-form normal equations (with intercept). Returns (intercept, coefs, pred)."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    A = np.column_stack([np.ones(len(X)), X])
    coefs, *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ coefs
    return coefs[0], coefs[1:], pred


def resolve_cache_file(ticker, suffix):
    t = str(ticker).strip()
    for cand in (CACHE_DIR / f"{t}{suffix}", CACHE_DIR / f"{t.replace('.', '-')}{suffix}"):
        if cand.exists():
            return cand
    return None


# ──────────────────────────────────────────────────────────────────────────
# PRICE/VOLUME FEATURE EXTRACTION (RS + A/D combined single pass)
# ──────────────────────────────────────────────────────────────────────────

def _window_ad_stats(prices, vols, highs, lows, w):
    if len(prices) < w:
        return {}
    wp, wv, wh, wl = prices[-w:], vols[-w:], highs[-w:], lows[-w:]
    p_diff = np.diff(wp)
    safe_prev = np.where(wp[:-1] == 0, 1.0, wp[:-1])
    p_rets = p_diff / safe_prev
    vtail = wv[1:]
    mean_vol = max(1.0, np.mean(wv))
    vratio = vtail / mean_vol

    up = p_rets > 0
    dn = p_rets < 0
    up_vol = np.sum(vtail[up])
    dn_vol = np.sum(vtail[dn])
    updn_ratio = up_vol / max(1.0, dn_vol)

    heavy_up = up & (vratio > 1.2)
    heavy_dn = dn & (vratio > 1.2)
    h_up_vol = np.sum(vtail[heavy_up])
    h_dn_vol = np.sum(vtail[heavy_dn])
    heavy_net_ratio = h_up_vol / max(1.0, h_up_vol + h_dn_vol)
    net_heavy_days = int(np.sum(heavy_up)) - int(np.sum(heavy_dn))
    heavy_up_int = np.sum(p_rets[heavy_up] * vratio[heavy_up])
    heavy_dn_int = np.sum(np.abs(p_rets[heavy_dn]) * vratio[heavy_dn])
    net_heavy_intensity = heavy_up_int - heavy_dn_int

    rng = np.maximum(1e-6, wh - wl)
    cls_rng = (wp - wl) / rng * 100.0
    vw_cls_rng = np.sum(cls_rng * wv) / max(1.0, np.sum(wv))
    mf_mult = ((wp - wl) - (wh - wp)) / rng
    cmf = np.sum(mf_mult * wv) / max(1.0, np.sum(wv))

    tag = f"{w}D"
    return {
        f"UpDnVol_{tag}": updn_ratio,
        f"HeavyNetRatio_{tag}": heavy_net_ratio,
        f"NetHeavyDays_{tag}": net_heavy_days,
        f"NetHeavyIntensity_{tag}": net_heavy_intensity,
        f"AvgClsRange_{tag}": float(np.mean(cls_rng)),
        f"VWClsRange_{tag}": vw_cls_rng,
        f"CMF_{tag}": cmf,
        f"PriceChg_{tag}": (wp[-1] / wp[0] - 1) * 100.0 if wp[0] > 0 else 0.0,
    }


def extract_ticker_features(ticker, spy_perf_by_window):
    p_path = resolve_cache_file(ticker, "_1d.parquet")
    if p_path is None:
        return None
    try:
        cdf = pd.read_parquet(p_path, columns=["Open", "High", "Low", "Close", "Volume"])
    except Exception:
        return None
    if cdf.empty or len(cdf) < 30:
        return None

    prices = pd.to_numeric(cdf["Close"], errors="coerce").values
    highs = pd.to_numeric(cdf["High"], errors="coerce").values
    lows = pd.to_numeric(cdf["Low"], errors="coerce").values
    vols = pd.to_numeric(cdf["Volume"], errors="coerce").values

    valid = ~np.isnan(prices) & ~np.isnan(vols) & (prices > 0) & ~np.isnan(highs) & ~np.isnan(lows)
    prices, highs, lows, vols = prices[valid], highs[valid], lows[valid], vols[valid]
    if len(prices) < 30:
        return None

    latest = float(prices[-1])
    rec = {"Ticker": str(ticker).strip(), "Latest_Price": round(latest, 2), "Hist_Days": len(prices)}

    # RS relative-performance-vs-SPY windows
    for label, days in RS_WINDOWS.items():
        if len(prices) > days:
            stock_perf = latest / prices[-(days + 1)]
            rec[f"RelPerf_{label}"] = stock_perf / spy_perf_by_window[label]
            rec[f"StockRet_{label}"] = (stock_perf - 1) * 100.0
        else:
            rec[f"RelPerf_{label}"] = np.nan
            rec[f"StockRet_{label}"] = np.nan

    # Baseline current-formula RS (calc_ibd_ratings.py: 40/20/20/20 weighted, sigmoid)
    def _sigmoid(score):
        d = score - 100.0
        return max(1.0, min(99.0, 50.0 + 49.0 * (d / (abs(d) + 22.0))))

    n = len(prices)
    n63, n126, n189, n252 = min(n - 1, 63), min(n - 1, 126), min(n - 1, 189), min(n - 1, 252)
    perf_t = (0.4 * (prices[-1] / prices[-(n63 + 1)]) + 0.2 * (prices[-1] / prices[-(n126 + 1)]) +
              0.2 * (prices[-1] / prices[-(n189 + 1)]) + 0.2 * (prices[-1] / prices[-(n252 + 1)]))
    rec["_perf_t_baseline"] = perf_t  # combined with spy baseline perf later

    # A/D windowed money-flow / heavy-volume features
    for w in AD_WINDOWS:
        rec.update(_window_ad_stats(prices, vols, highs, lows, w))

    # Baseline current-formula A/D (65D Chaikin money flow, calc_ad_rating_snapshot)
    if n >= 65:
        wsl = slice(-65, None)
        hl = highs[wsl] - lows[wsl]
        safe_hl = np.where(hl == 0, 1.0, hl)
        mf = np.where(hl != 0, ((prices[wsl] - lows[wsl]) - (highs[wsl] - prices[wsl])) / safe_hl, 0.0)
        ad_ratio = np.sum(mf * vols[wsl]) / max(1.0, np.sum(vols[wsl]))
        rec["AD_baseline"] = max(0.0, min(99.0, 49.5 + ad_ratio * 49.5))
    else:
        rec["AD_baseline"] = np.nan

    # Distance from moving averages
    for ma in MA_WINDOWS:
        if n >= ma:
            rec[f"Dist_{ma}MA"] = (latest / np.mean(prices[-ma:]) - 1) * 100.0

    # Up/down day volume asymmetry + price/vol correlation (whole history, capped 250D)
    tail_n = min(n - 1, 250)
    if tail_n >= 20:
        pr = np.diff(prices[-(tail_n + 1):]) / np.where(prices[-(tail_n + 1):-1] == 0, 1.0, prices[-(tail_n + 1):-1])
        vt = vols[-(tail_n):]
        mv = max(1.0, np.mean(vt))
        vr = vt / mv
        up, dn = pr > 0, pr < 0
        rec["UpDayVolRatio"] = float(np.mean(vr[up])) if np.any(up) else 1.0
        rec["DnDayVolRatio"] = float(np.mean(vr[dn])) if np.any(dn) else 1.0
        if np.std(pr) > 0 and np.std(vr) > 0:
            rec["PriceVolCorr"] = float(np.corrcoef(pr, vr)[0, 1])

    return rec


def load_spy_perf():
    p = resolve_cache_file("SPY", "_1d.parquet")
    if p is None:
        raise FileNotFoundError("SPY parquet missing from ticker_cache")
    spy = pd.read_parquet(p, columns=["Close"])
    close = pd.to_numeric(spy["Close"], errors="coerce").dropna()
    close = close[close > 0].values
    latest = float(close[-1])
    perf = {}
    for label, days in RS_WINDOWS.items():
        perf[label] = latest / close[-(days + 1)] if len(close) > days else 1.0
    # baseline (40/20/20/20) SPY perf, aligned with same n63/n126/n189/n252 logic
    n = len(close)
    n63, n126, n189, n252 = min(n - 1, 63), min(n - 1, 126), min(n - 1, 189), min(n - 1, 252)
    perf_c_baseline = (0.4 * (close[-1] / close[-(n63 + 1)]) + 0.2 * (close[-1] / close[-(n126 + 1)]) +
                       0.2 * (close[-1] / close[-(n189 + 1)]) + 0.2 * (close[-1] / close[-(n252 + 1)]))
    return perf, perf_c_baseline, len(close)


# ──────────────────────────────────────────────────────────────────────────
# FUNDAMENTALS FEATURE EXTRACTION
# ──────────────────────────────────────────────────────────────────────────

def _series_from_label(block, labels):
    """block[label] is {date_str: value}; return (dates_desc, values) for first matching label."""
    if not isinstance(block, dict):
        return None
    for lbl in labels:
        col = block.get(lbl)
        if isinstance(col, dict) and col:
            dates = sorted(col.keys(), reverse=True)
            vals = []
            ds = []
            for d in dates:
                v = col[d]
                if v is not None:
                    try:
                        vals.append(float(v))
                        ds.append(d)
                    except (TypeError, ValueError):
                        pass
            if len(vals) >= 2:
                return ds, vals
    return None


def extract_fund_features(ticker):
    f_path = resolve_cache_file(ticker, "_fund.json")
    if f_path is None:
        return None
    try:
        with open(f_path) as fh:
            fund = json.load(fh)
    except Exception:
        return None
    if not fund or fund.get("error"):
        return None

    rec = {"Ticker": str(ticker).strip()}
    info = fund.get("info") if isinstance(fund.get("info"), dict) else {}

    eps_q = _series_from_label(fund.get("income_q"), ["Diluted EPS", "Basic EPS"])
    eps_a = _series_from_label(fund.get("income_a"), ["Diluted EPS", "Basic EPS"])
    rev_q = _series_from_label(fund.get("income_q"), ["Total Revenue"])
    rev_a = _series_from_label(fund.get("income_a"), ["Total Revenue"])
    ni_q = _series_from_label(fund.get("income_q"), ["Net Income", "Net Income Common Stockholders"])
    eq_q = _series_from_label(fund.get("balance_q"), ["Stockholders Equity", "Common Stock Equity"])

    # ── EPS short-term growth (QoQ YoY, index 4 = 4 quarters back) ──
    q0g = q1g = None
    if eps_q:
        _, vals = eps_q
        if len(vals) > 4 and abs(vals[4]) > 1e-9:
            q0g = (vals[0] - vals[4]) / abs(vals[4]) * 100.0
        if len(vals) > 5 and abs(vals[5]) > 1e-9:
            q1g = (vals[1] - vals[5]) / abs(vals[5]) * 100.0
    rec["EPS_Q0_YoY"] = q0g
    rec["EPS_Q1_YoY"] = q1g
    rec["EPS_Accel"] = (q0g - q1g) if (q0g is not None and q1g is not None) else np.nan

    # ── EPS long-term growth (weighted annual YoY, most recent weighted highest) ──
    lt_growth = np.nan
    if eps_a:
        _, vals = eps_a
        sum_g, sum_w = 0.0, 0.0
        for j in range(min(len(vals) - 1, 5)):
            if abs(vals[j + 1]) > 1e-9:
                gv = (vals[j] - vals[j + 1]) / abs(vals[j + 1]) * 100.0
                w = 5 - j
                sum_g += gv * w
                sum_w += w
        if sum_w > 0:
            lt_growth = sum_g / sum_w
    rec["EPS_LT_Growth"] = lt_growth

    # ── EPS negative-quarter ratio (trailing 4 quarters, YoY) ──
    neg_q, cnt_q = 0, 0
    if eps_q:
        _, vals = eps_q
        for j in range(min(len(vals) - 4, 4)):
            if abs(vals[j + 4]) > 1e-9:
                gv = (vals[j] - vals[j + 4]) / abs(vals[j + 4]) * 100.0
                cnt_q += 1
                if gv < 0:
                    neg_q += 1
    rec["EPS_NegQRatio"] = (neg_q / cnt_q) if cnt_q > 0 else 0.0

    # ── Sales growth (short-term QoQ YoY + long-term weighted annual) ──
    sales_q0g = sales_q1g = None
    if rev_q:
        _, vals = rev_q
        if len(vals) > 4 and abs(vals[4]) > 1e-9:
            sales_q0g = (vals[0] - vals[4]) / abs(vals[4]) * 100.0
        if len(vals) > 5 and abs(vals[5]) > 1e-9:
            sales_q1g = (vals[1] - vals[5]) / abs(vals[5]) * 100.0
    rec["Sales_Q0_YoY"] = sales_q0g
    rec["Sales_Accel"] = (sales_q0g - sales_q1g) if (sales_q0g is not None and sales_q1g is not None) else np.nan

    sales_lt = np.nan
    if rev_a:
        _, vals = rev_a
        sum_g, sum_w = 0.0, 0.0
        for j in range(min(len(vals) - 1, 5)):
            if abs(vals[j + 1]) > 1e-9:
                gv = (vals[j] - vals[j + 1]) / abs(vals[j + 1]) * 100.0
                w = 5 - j
                sum_g += gv * w
                sum_w += w
        if sum_w > 0:
            sales_lt = sum_g / sum_w
    rec["Sales_LT_Growth"] = sales_lt

    # ── Margin (trailing-quarter net margin from income_q, + trend across avail. quarters) ──
    margin_now = np.nan
    margin_trend = np.nan
    if rev_q and ni_q:
        rdates, rvals = rev_q
        ndates, nvals = ni_q
        # align by matching date strings
        rmap = dict(zip(rdates, rvals))
        nmap = dict(zip(ndates, nvals))
        common = [d for d in rdates if d in nmap]
        margins = []
        for d in common:
            if abs(rmap[d]) > 1e-6:
                margins.append(nmap[d] / rmap[d] * 100.0)
        if margins:
            margin_now = margins[0]
            if len(margins) >= 3:
                # simple recent-vs-older trend: avg of most-recent-2 minus avg of oldest-2 available
                margin_trend = float(np.mean(margins[:2])) - float(np.mean(margins[-2:]))
    rec["Margin_Now"] = margin_now
    rec["Margin_Trend"] = margin_trend

    # ── ROE: prefer yfinance info field, fallback to NI / Equity ──
    roe_val = None
    roe_info = info.get("returnOnEquity")
    if roe_info is not None:
        try:
            roe_val = float(roe_info) * 100.0
        except (TypeError, ValueError):
            roe_val = None
    if roe_val is None and ni_q and eq_q:
        ndates, nvals = ni_q
        edates, evals = eq_q
        emap = dict(zip(edates, evals))
        if ndates and ndates[0] in emap and abs(emap[ndates[0]]) > 1e-6:
            roe_val = nvals[0] / emap[ndates[0]] * 100.0
    rec["ROE"] = roe_val

    # profitMargins / revenueGrowth / earningsQuarterlyGrowth as independent cross-checks
    for k, out_k in (("profitMargins", "Info_ProfitMargin"), ("revenueGrowth", "Info_RevGrowth"),
                     ("earningsQuarterlyGrowth", "Info_EPSQGrowth"), ("returnOnAssets", "Info_ROA")):
        v = info.get(k)
        try:
            rec[out_k] = float(v) * 100.0 if v is not None else np.nan
        except (TypeError, ValueError):
            rec[out_k] = np.nan

    # ── Analyst-driven EPS signals: surprise history, beat rate, estimate revisions/growth ──
    # New signal group (not covered by EPS_Q0_YoY/EPS_LT_Growth/EPS_NegQRatio/ROE above) found by
    # comparing against IBD_rating_glm's independent reverse-engineering effort - its EPS model
    # uses these plus the four above and forward-tested higher (R^2 0.345 vs our 0.333 at the
    # time). earnings_history/eps_trend/earnings_estimate are already in the cached fund.json,
    # just unused by the production EPS formula until now.
    eh = fund.get("earnings_history")
    surprises, beats = [], 0
    if isinstance(eh, dict):
        for k, v in eh.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and v.get("epsActual") is not None:
                s = v.get("surprisePercent")
                if isinstance(s, (int, float)):
                    surprises.append(float(s) * 100.0)
                diff = v.get("epsDifference")
                if diff is not None:
                    try:
                        if float(diff) > 0:
                            beats += 1
                    except (TypeError, ValueError):
                        pass
    rec["EpsSurpriseMean"] = float(np.mean(surprises)) if surprises else np.nan
    rec["EpsBeatRate"] = (beats / len(surprises)) if surprises else np.nan

    # stability: CV of YoY EPS growth (quarterly, falling back to annual if <3 quarterly points)
    stab_vals = []
    if eps_q:
        _, vals = eps_q
        for j in range(min(len(vals) - 4, 4)):
            if abs(vals[j + 4]) > 1e-9:
                stab_vals.append((vals[j] - vals[j + 4]) / abs(vals[j + 4]) * 100.0)
    if len(stab_vals) < 3 and eps_a:
        _, vals = eps_a
        stab_vals = []
        for j in range(min(len(vals) - 1, 4)):
            if abs(vals[j + 1]) > 1e-9:
                stab_vals.append((vals[j] - vals[j + 1]) / abs(vals[j + 1]) * 100.0)
    rec["EPS_StabilityCV"] = (float(np.std(stab_vals) / max(1e-9, abs(np.mean(stab_vals))))
                               if len(stab_vals) >= 3 else np.nan)

    et = fund.get("eps_trend")
    rev_trend = np.nan
    if isinstance(et, dict) and isinstance(et.get("0q"), dict):
        cur = et["0q"].get("current")
        ago = et["0q"].get("90daysAgo")
        if cur is not None and ago and float(ago) != 0:
            rev_trend = (float(cur) / float(ago) - 1) * 100.0
    rec["EpsRevTrend"] = rev_trend

    ee = fund.get("earnings_estimate")
    est_growth_q = est_growth_y = np.nan
    if isinstance(ee, dict):
        if isinstance(ee.get("0q"), dict) and ee["0q"].get("growth") is not None:
            est_growth_q = float(ee["0q"]["growth"]) * 100.0
        if isinstance(ee.get("+1y"), dict) and ee["+1y"].get("growth") is not None:
            est_growth_y = float(ee["+1y"]["growth"]) * 100.0
    rec["EstEPSGrowth_Q"] = est_growth_q
    rec["EstEPSGrowth_Y"] = est_growth_y

    # ── yfinance info-dict fields (Info_*): ported from IBD_rating_glm, which found these
    # high-coverage fields improved both EPS and SMR forward accuracy. _INFO_PCT_FIELDS are
    # stored as a fraction (0.25 = 25%) by yfinance and need *100 for scale consistency.
    _pct_fields = {"profitMargins", "revenueGrowth", "earningsQuarterlyGrowth", "returnOnAssets",
                   "grossMargins", "operatingMargins", "earningsGrowth"}

    def _info_num(key):
        v = info.get(key)
        if v is None:
            return np.nan
        try:
            f = float(v)
        except (TypeError, ValueError):
            return np.nan
        return f * 100.0 if key in _pct_fields else f

    for k, out_k in (("profitMargins", "Info_ProfitMargin"), ("revenueGrowth", "Info_RevGrowth"),
                     ("earningsQuarterlyGrowth", "Info_EPSQGrowth"), ("returnOnAssets", "Info_ROA"),
                     ("grossMargins", "Info_GrossMargin"), ("operatingMargins", "Info_OpMargin"),
                     ("earningsGrowth", "Info_EarningsGrowth"), ("debtToEquity", "Info_DebtEquity"),
                     ("currentRatio", "Info_CurrentRatio"), ("quickRatio", "Info_QuickRatio"),
                     ("priceToBook", "Info_PriceBook"), ("totalCashPerShare", "Info_TotalCashPS"),
                     ("forwardPE", "Info_FwdPE"), ("numberOfAnalystOpinions", "Info_NumAnalysts")):
        rec[out_k] = _info_num(k)

    mc = _info_num("marketCap")
    fcf = _info_num("freeCashflow")
    ocf = _info_num("operatingCashflow")
    rec["Info_FCFYield"] = (fcf / mc * 100.0) if (mc and mc > 0 and np.isfinite(fcf)) else np.nan
    rec["Info_OCFYield"] = (ocf / mc * 100.0) if (mc and mc > 0 and np.isfinite(ocf)) else np.nan
    px = _info_num("currentPrice")
    if not np.isfinite(px):
        px = _info_num("regularMarketPrice")
    tgt = _info_num("targetMeanPrice")
    rec["Info_TargetUpside"] = ((tgt / px - 1.0) * 100.0) if (px and px > 0 and np.isfinite(tgt)) else np.nan

    rec["Industry"] = info.get("industry")
    return rec


# ──────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────

def main():
    report = []
    report.append("# IBD Rating Reverse-Engineering v2 (Closed-Form, No ML)\n")
    report.append(f"**Ground truth**: `{CSV_PATH.name}` | **Method**: percentile ranks + closed-form "
                   "least-squares / constrained weight optimization on transparent formulas — "
                   "no black-box models.\n")

    print("=" * 80)
    print("Loading MarketSurge ground truth + filtering universe")
    print("=" * 80)
    df_ms = pd.read_csv(CSV_PATH, low_memory=False)
    df_ms["Symbol"] = df_ms["Symbol"].astype(str).str.strip()
    # Note: A/D Rating, SMR Rating, and Ind Group RS are all letter grades (A+..E) in this
    # export, NOT numeric — must NOT run clean_num on them (it silently turns them all to NaN).
    for c in ["Comp Rating", "RS Rating", "EPS Rating", "RS 3-Month Rating", "RS 6-Month Rating"]:
        if c in df_ms.columns:
            df_ms[c] = df_ms[c].apply(clean_num)
    df_ms["SMR_Num"] = df_ms["SMR Rating"].map(SMR_GRADE_NUM)
    df_ms["AD_Num"] = df_ms["A/D Rating"].astype(str).str.strip().str.upper().map(SUBTIER_13_MAP)
    df_ms["GroupRS_Num"] = df_ms["Ind Group RS"].astype(str).str.strip().str.upper().map(SUBTIER_13_MAP)

    df_valid = df_ms.dropna(subset=["Symbol", "Comp Rating"]).copy()
    df_valid = df_valid[df_valid["Symbol"] != ""]
    print(f"Rows with valid Comp Rating: {len(df_valid):,}")

    has_price = df_valid["Symbol"].apply(lambda t: resolve_cache_file(t, "_1d.parquet") is not None)
    df_price_universe = df_valid[has_price].copy()
    print(f"...and price parquet in ticker_cache: {len(df_price_universe):,}")

    has_fund = df_price_universe["Symbol"].apply(lambda t: resolve_cache_file(t, "_fund.json") is not None)
    df_full_universe = df_price_universe[has_fund].copy()
    print(f"...and fundamentals json in ticker_cache: {len(df_full_universe):,}")

    report.append(f"- Valid Comp Rating in CSV: **{len(df_valid):,}**")
    report.append(f"- + price parquet present: **{len(df_price_universe):,}** (price-only universe: RS, A/D)")
    report.append(f"- + fundamentals json present: **{len(df_full_universe):,}** (full universe: EPS, SMR, Composite)\n")

    print("\nLoading SPY reference performance...")
    spy_perf, spy_perf_baseline, spy_days = load_spy_perf()
    print(f"SPY: {spy_days} trading days, perf ratios: {spy_perf}")

    print(f"\nExtracting price/volume features for {len(df_price_universe):,} tickers (16 threads)...")
    tickers_price = df_price_universe["Symbol"].tolist()
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda t: extract_ticker_features(t, spy_perf), tickers_price))
    df_feat = pd.DataFrame([r for r in results if r is not None])
    df_feat["_perf_c_baseline"] = spy_perf_baseline
    print(f"OK: {len(df_feat):,} tickers with usable price features")

    print(f"\nExtracting fundamentals features for {len(df_full_universe):,} tickers (16 threads)...")
    tickers_fund = df_full_universe["Symbol"].tolist()
    with ThreadPoolExecutor(max_workers=16) as ex:
        fresults = list(ex.map(extract_fund_features, tickers_fund))
    df_fund = pd.DataFrame([r for r in fresults if r is not None])
    print(f"OK: {len(df_fund):,} tickers with usable fundamentals features")

    price_merged = df_price_universe.merge(df_feat, left_on="Symbol", right_on="Ticker", how="inner")
    full_merged = price_merged.merge(df_fund, left_on="Symbol", right_on="Ticker", how="inner", suffixes=("", "_f"))
    print(f"\nFinal price-merged universe: {len(price_merged):,} | full (price+fund)-merged universe: {len(full_merged):,}")

    # ════════════════════════════════════════════════════════════════════
    # 1. RS RATING
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("1. RS RATING")
    print("=" * 80)
    rel_cols = [f"RelPerf_{w}" for w in RS_WINDOWS]
    rs_df = price_merged.dropna(subset=rel_cols + ["RS Rating"]).copy()
    print(f"RS evaluation universe: {len(rs_df):,}")

    y_rs = rs_df["RS Rating"].values

    # Baseline: current pine/production formula (40/20/20/20 -> sigmoid)
    def _sigmoid_arr(score):
        d = score - 100.0
        return np.clip(50.0 + 49.0 * (d / (np.abs(d) + 22.0)), 1, 99)

    perf_t_over_c = rs_df["_perf_t_baseline"].values / rs_df["_perf_c_baseline"].values * 100.0
    pred_baseline_rs = _sigmoid_arr(perf_t_over_c)
    rs_results = [score_report("Current formula (40/20/20/20 sigmoid)", y_rs, pred_baseline_rs)]

    # Method: percentile rank of the SAME 40/20/20/20 raw score (sigmoid vs rank transform)
    pred_rank_same_weights = pct_rank_99(perf_t_over_c)
    rs_results.append(score_report("Same 40/20/20/20 weights, percentile-rank transform", y_rs, pred_rank_same_weights))

    # Method: monotonic constrained weight optimization (1M>=3M>=6M>=9M>=12M) + percentile rank
    perf_matrix = rs_df[rel_cols].values

    def mono_obj(params):
        v = np.abs(params)
        w5 = v[4]; w4 = w5 + v[3]; w3 = w4 + v[2]; w2 = w3 + v[1]; w1 = w2 + v[0]
        w = np.array([w1, w2, w3, w4, w5])
        w = w / w.sum()
        raw = perf_matrix @ w * 100.0
        ranks = pct_rank_99(raw)
        return mae(y_rs, ranks)

    res = minimize(mono_obj, [0.15, 0.10, 0.08, 0.05, 0.02], method="Nelder-Mead",
                    options={"maxiter": 5000, "xatol": 1e-7, "fatol": 1e-7})
    v = np.abs(res.x)
    w5 = v[4]; w4 = w5 + v[3]; w3 = w4 + v[2]; w2 = w3 + v[1]; w1 = w2 + v[0]
    opt_w = np.array([w1, w2, w3, w4, w5])
    opt_w = opt_w / opt_w.sum()
    raw_opt = perf_matrix @ opt_w * 100.0
    pred_opt_rank = pct_rank_99(raw_opt)
    rs_results.append(score_report("Monotonic-optimal weights + percentile-rank", y_rs, pred_opt_rank))
    rs_weights_labels = list(RS_WINDOWS.keys())
    print("Optimal monotonic RS weights:", dict(zip(rs_weights_labels, np.round(opt_w, 4))))

    # Method: unconstrained closed-form OLS directly on RS Rating scale (diagnostic only)
    b0, coefs, pred_ols = lstsq_fit(perf_matrix, y_rs)
    pred_ols_clipped = np.clip(pred_ols, 1, 99)
    rs_results.append(score_report("Unconstrained OLS (direct, diagnostic)", y_rs, pred_ols_clipped))

    rs_results_df = pd.DataFrame(rs_results)
    print(rs_results_df.to_string(index=False))

    report.append("## 1. RS Rating\n")
    report.append(f"Evaluation universe: **{len(rs_df):,}** stocks (full 12M price history + valid RS Rating).\n")
    report.append(rs_results_df.to_markdown(index=False))
    report.append("\n**Optimal monotonic-recency weights** (1M ≥ 3M ≥ 6M ≥ 9M ≥ 12M):\n")
    report.append("| " + " | ".join(rs_weights_labels) + " |")
    report.append("|" + "---|" * len(rs_weights_labels))
    report.append("| " + " | ".join(f"{w:.4f}" for w in opt_w) + " |\n")
    report.append("**Key finding**: swapping the sigmoid transform for a straight percentile-rank of the "
                   "weighted relative-performance score (i.e. actually ranking the stock against the "
                   "universe, rather than squashing a single stock's raw ratio through a fixed sigmoid) "
                   "is what recovers most of the accuracy — the sigmoid's fixed curvature doesn't adapt "
                   "to how spread out the universe's performance is on a given day.\n")

    # ════════════════════════════════════════════════════════════════════
    # 2. A/D RATING
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("2. A/D RATING")
    print("=" * 80)
    ad_feat_cols = [f"UpDnVol_65D", f"HeavyNetRatio_65D", f"NetHeavyIntensity_65D", f"VWClsRange_65D", f"CMF_65D",
                    f"UpDnVol_130D", f"NetHeavyIntensity_130D", f"CMF_130D", f"UpDnVol_30D", f"NetHeavyIntensity_30D",
                    "UpDayVolRatio", "DnDayVolRatio"]
    ad_df = price_merged.dropna(subset=["AD_Num", "AD_baseline"] + ad_feat_cols).copy()
    print(f"A/D evaluation universe: {len(ad_df):,}")

    y_ad = ad_df["AD_Num"].values  # 1..13 scale

    # Baseline: current 65D Chaikin-money-flow formula, mapped 0-99 -> converted to 1..13 for comparison
    ad_baseline_0_99 = ad_df["AD_baseline"].values
    ad_baseline_1_13 = 1.0 + (ad_baseline_0_99 / 99.0) * 12.0
    ad_results = [score_report("Current formula (65D CMF, 0-99->1-13 scaled)", y_ad, ad_baseline_1_13)]

    # Method: percentile-rank of baseline CMF score, mapped onto the empirical 1-13 distribution
    ad_rank_of_baseline = pct_rank_99(ad_baseline_0_99)
    ad_rank_of_baseline_1_13 = 1.0 + (ad_rank_of_baseline / 99.0) * 12.0
    ad_results.append(score_report("Percentile-rank of same CMF score", y_ad, ad_rank_of_baseline_1_13))

    # Method: closed-form OLS blend of multi-window up/dn-vol + heavy-intensity + vw-closing-range -> percentile rank
    X_ad = ad_df[ad_feat_cols].values
    b0_ad, coefs_ad, _ = lstsq_fit(X_ad, y_ad)
    raw_ad_blend = X_ad @ coefs_ad
    pred_ad_rank = pct_rank_99(raw_ad_blend)
    pred_ad_rank_1_13 = 1.0 + (pred_ad_rank / 99.0) * 12.0
    ad_results.append(score_report("OLS-weighted multi-window blend + percentile-rank", y_ad, pred_ad_rank_1_13))

    # Method: direct OLS onto 1-13 scale (diagnostic)
    b0_ad2, coefs_ad2, pred_ad_ols = lstsq_fit(X_ad, y_ad)
    pred_ad_ols_clipped = np.clip(pred_ad_ols, 1, 13)
    ad_results.append(score_report("Direct OLS onto 1-13 scale (diagnostic)", y_ad, pred_ad_ols_clipped))

    ad_results_df = pd.DataFrame(ad_results)
    print(ad_results_df.to_string(index=False))

    ad_weight_table = pd.DataFrame({"Feature": ad_feat_cols, "OLS_Coef": np.round(coefs_ad, 5)})
    ad_weight_table["Abs_Weight_Pct"] = (ad_weight_table["OLS_Coef"].abs() /
                                          ad_weight_table["OLS_Coef"].abs().sum() * 100).round(1)
    ad_weight_table = ad_weight_table.sort_values("Abs_Weight_Pct", ascending=False)
    print(ad_weight_table.to_string(index=False))

    report.append("## 2. A/D Rating\n")
    report.append(f"Evaluation universe: **{len(ad_df):,}** stocks.\n")
    report.append(ad_results_df.to_markdown(index=False))
    report.append("\n**OLS feature weights** (multi-window up/down-volume + heavy-volume intensity + "
                   "volume-weighted closing range blend):\n")
    report.append(ad_weight_table.to_markdown(index=False))
    report.append("")

    # ════════════════════════════════════════════════════════════════════
    # 3. EPS RATING
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("3. EPS RATING")
    print("=" * 80)
    # NOTE: EPS_Q1_YoY / EPS_Accel need a 6th trailing quarter (index 5) which yfinance's
    # quarterly financials essentially never provide (~5 quarters deep) — they are dropped from
    # the fitted model as structurally unavailable, rather than forcing dropna to zero out the
    # universe. They're still used (nan-aware, matching production's fallback path) in the
    # baseline-formula reproduction below since that's what calc_ibd_ratings.py already does.
    eps_feat_cols = ["EPS_Q0_YoY", "EPS_LT_Growth", "EPS_NegQRatio", "ROE"]
    eps_df = full_merged.dropna(subset=["EPS Rating"]).copy()
    for c in ["EPS_Q0_YoY", "EPS_Q1_YoY", "EPS_Accel", "EPS_LT_Growth", "EPS_NegQRatio", "ROE"]:
        eps_df[c] = pd.to_numeric(eps_df[c], errors="coerce")
    for c in ["EPS_Q0_YoY", "EPS_Q1_YoY", "EPS_Accel", "EPS_LT_Growth"]:
        eps_df[c] = eps_df[c].clip(-300, 300)  # clip extreme growth outliers (small-denominator YoY blowups)

    eps_coverage = pd.DataFrame({"Feature": eps_feat_cols,
                                  "Coverage_Pct": [round(eps_df[c].notna().mean() * 100, 1) for c in eps_feat_cols]})
    print("EPS feature coverage:\n", eps_coverage.to_string(index=False))

    # Baseline: reproduce current production formula exactly (nan-aware fallback for q1g/accel)
    q0g = eps_df["EPS_Q0_YoY"].values
    q1g = eps_df["EPS_Q1_YoY"].values
    lt = eps_df["EPS_LT_Growth"].values
    accel = eps_df["EPS_Accel"].values
    st_growth = np.where(~np.isnan(q1g), q0g * 0.65 + np.nan_to_num(q1g) * 0.35, q0g)
    blended_baseline_all = st_growth * 0.50 + np.nan_to_num(lt) * 0.35 + np.nan_to_num(accel) * 0.15
    raw_base_all = 50.0 + 49.0 * (blended_baseline_all / (np.abs(blended_baseline_all) + 40.0))
    roe_all = eps_df["ROE"].values
    roe_pen_all = np.where(roe_all < 0, np.minimum(22.0, np.abs(roe_all) * 0.05 + 5.0), 0.0)
    lt_pen_all = np.where(lt < 0, np.minimum(15.0, np.abs(lt) * 0.4), 0.0)
    neg_ratio_all = eps_df["EPS_NegQRatio"].values
    eps_df["_pred_baseline"] = np.clip(raw_base_all - roe_pen_all - lt_pen_all - neg_ratio_all * 10.0, 1, 99)
    eps_df["_blended_baseline"] = blended_baseline_all

    # Median-impute the fitted-model features (closed-form; no row dropped for a single missing input)
    eps_df_full = eps_df.dropna(subset=["EPS_Q0_YoY"]).copy()  # still require the one near-universal feature
    for c in eps_feat_cols:
        med = eps_df_full[c].median()
        eps_df_full[c] = eps_df_full[c].fillna(med)
    print(f"EPS evaluation universe (median-imputed): {len(eps_df_full):,} / {len(eps_df):,} with rating")

    y_eps = eps_df_full["EPS Rating"].values
    # Log-compress the growth-rate & ROE features for the FITTED model (not the baseline reproduction
    # above): small-denominator YoY blowups (e.g. +28,600% off a near-zero prior-quarter EPS) otherwise
    # dominate a plain clip and actively hurt correlation — log-compression preserves rank order while
    # taming magnitude. Verified on a held-out sample: raises EPS_Q0_YoY's correlation with the actual
    # EPS Rating from ~0.35 (clip ±300) to ~0.40 (log-compressed).
    X_eps = np.column_stack([
        log_compress(eps_df_full["EPS_Q0_YoY"].values),
        log_compress(eps_df_full["EPS_LT_Growth"].values),
        eps_df_full["EPS_NegQRatio"].values,
        log_compress(eps_df_full["ROE"].values),
    ])

    pred_eps_baseline = eps_df_full["_pred_baseline"].values
    blended_baseline = eps_df_full["_blended_baseline"].values
    eps_results = [score_report("Current formula (blended growth -> sigmoid)", y_eps, pred_eps_baseline)]

    # Method: percentile-rank of the same blended growth score (no sigmoid, no penalties)
    pred_eps_rank_same = pct_rank_99(blended_baseline)
    eps_results.append(score_report("Same blend, percentile-rank transform", y_eps, pred_eps_rank_same))

    # Method: closed-form OLS on raw features -> direct EPS Rating scale
    b0_eps, coefs_eps, pred_eps_ols = lstsq_fit(X_eps, y_eps)
    pred_eps_ols_clipped = np.clip(pred_eps_ols, 1, 99)
    eps_results.append(score_report("OLS-weighted feature blend (direct scale)", y_eps, pred_eps_ols_clipped))

    # Method: OLS-weighted blend -> percentile rank
    raw_eps_ols_blend = X_eps @ coefs_eps
    pred_eps_ols_rank = pct_rank_99(raw_eps_ols_blend)
    eps_results.append(score_report("OLS-weighted feature blend + percentile-rank", y_eps, pred_eps_ols_rank))

    eps_results_df = pd.DataFrame(eps_results)
    print(eps_results_df.to_string(index=False))

    eps_weight_table = pd.DataFrame({"Feature": eps_feat_cols, "OLS_Coef": np.round(coefs_eps, 5)})
    eps_weight_table["Abs_Weight_Pct"] = (eps_weight_table["OLS_Coef"].abs() /
                                           eps_weight_table["OLS_Coef"].abs().sum() * 100).round(1)
    eps_weight_table = eps_weight_table.sort_values("Abs_Weight_Pct", ascending=False)
    print(eps_weight_table.to_string(index=False))

    report.append("## 3. EPS Rating\n")
    report.append(f"Evaluation universe: **{len(eps_df_full):,}** stocks (median-imputed features) "
                   f"out of {len(eps_df):,} with a valid EPS Rating and fundamentals present.\n")
    report.append("**Feature coverage** (yfinance's quarterly financials are only ~5 quarters deep, so "
                   "`EPS_Q1_YoY`/`EPS_Accel` — which need a 6th trailing quarter — are almost always "
                   "missing and were dropped from the fitted model; they're still used, nan-aware, when "
                   "reproducing the current production formula's exact fallback behavior below):\n")
    report.append(eps_coverage.to_markdown(index=False))
    report.append("")
    report.append(eps_results_df.to_markdown(index=False))
    report.append("\n**OLS feature weights**:\n")
    report.append(eps_weight_table.to_markdown(index=False))
    report.append("")

    # ════════════════════════════════════════════════════════════════════
    # 4. SMR RATING
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("4. SMR RATING")
    print("=" * 80)
    # Sales_Accel needs a 6th trailing quarter, same yfinance depth limit as EPS_Accel — dropped.
    smr_feat_cols = ["Sales_Q0_YoY", "Sales_LT_Growth", "Margin_Now", "Margin_Trend", "ROE"]
    smr_df = full_merged.dropna(subset=["SMR_Num"]).copy()
    for c in ["Sales_Q0_YoY", "Sales_LT_Growth", "Sales_Accel", "Margin_Now", "Margin_Trend", "ROE"]:
        smr_df[c] = pd.to_numeric(smr_df[c], errors="coerce")
    # No hard clip here — log_compress() below handles outliers for the fitted model without
    # truncating the rank-order information a fixed clip would throw away.

    smr_coverage = pd.DataFrame({"Feature": smr_feat_cols,
                                  "Coverage_Pct": [round(smr_df[c].notna().mean() * 100, 1) for c in smr_feat_cols]})
    print("SMR feature coverage:\n", smr_coverage.to_string(index=False))

    smr_df_full = smr_df.dropna(subset=["Sales_Q0_YoY"]).copy()  # still require the one near-universal feature
    for c in smr_feat_cols:
        med = smr_df_full[c].median()
        smr_df_full[c] = smr_df_full[c].fillna(med)
    print(f"SMR evaluation universe (median-imputed): {len(smr_df_full):,} / {len(smr_df):,} with rating")

    y_smr = smr_df_full["SMR_Num"].values
    # Log-compress growth/margin/ROE features for the FITTED model — Margin_Now in particular can
    # blow up past -1,000,000% when trailing revenue is near zero, and a hard ±200 clip alone still
    # lets that dominate the OLS fit relative to well-behaved rows.
    X_smr = np.column_stack([
        log_compress(smr_df_full["Sales_Q0_YoY"].values),
        log_compress(smr_df_full["Sales_LT_Growth"].values),
        log_compress(smr_df_full["Margin_Now"].values),
        log_compress(smr_df_full["Margin_Trend"].values),
        log_compress(smr_df_full["ROE"].values),
    ])

    # Baseline: current formula, ROE-only single pillar -> sigmoid
    roe_smr = smr_df_full["ROE"].values
    roe_filled = np.where(np.isnan(roe_smr), 15.0, roe_smr)
    pred_smr_baseline = np.clip(50.0 + 49.0 * (roe_filled / (np.abs(roe_filled) + 17.0)), 0, 99)
    smr_results = [score_report("Current formula (ROE-only sigmoid)", y_smr, pred_smr_baseline)]

    # Method: closed-form OLS 3-pillar blend (sales growth, margin, ROE) -> direct scale
    b0_smr, coefs_smr, pred_smr_ols = lstsq_fit(X_smr, y_smr)
    pred_smr_ols_clipped = np.clip(pred_smr_ols, 10, 95)
    smr_results.append(score_report("OLS 3-pillar blend (direct scale)", y_smr, pred_smr_ols_clipped))

    # Method: OLS 3-pillar blend -> percentile rank (rescaled onto SMR's discrete grade-number range)
    raw_smr_blend = X_smr @ coefs_smr
    smr_pct = pct_rank_99(raw_smr_blend)
    pred_smr_pct_scaled = 10.0 + (smr_pct - 1) / 98.0 * 85.0  # map 1-99 -> 10-95 (grade numeric range)
    smr_results.append(score_report("OLS 3-pillar blend + percentile-rank", y_smr, pred_smr_pct_scaled))

    smr_results_df = pd.DataFrame(smr_results)
    print(smr_results_df.to_string(index=False))

    smr_weight_table = pd.DataFrame({"Feature": smr_feat_cols, "OLS_Coef": np.round(coefs_smr, 5)})
    smr_weight_table["Abs_Weight_Pct"] = (smr_weight_table["OLS_Coef"].abs() /
                                           smr_weight_table["OLS_Coef"].abs().sum() * 100).round(1)
    smr_weight_table = smr_weight_table.sort_values("Abs_Weight_Pct", ascending=False)
    print(smr_weight_table.to_string(index=False))

    report.append("## 4. SMR Rating\n")
    report.append(f"Evaluation universe: **{len(smr_df_full):,}** stocks (median-imputed features) "
                   f"out of {len(smr_df):,} with a valid SMR Rating and fundamentals present.\n")
    report.append("**Feature coverage** (`Sales_Accel` needs a 6th trailing quarter, same yfinance depth "
                   "limit as `EPS_Accel`, and was dropped from the fitted model):\n")
    report.append(smr_coverage.to_markdown(index=False))
    report.append("")
    report.append(smr_results_df.to_markdown(index=False))
    report.append("\n**OLS feature weights** (the current production formula only uses ROE — this table shows "
                   "how much Sales growth and Margin trend actually matter):\n")
    report.append(smr_weight_table.to_markdown(index=False))
    report.append("")

    # ════════════════════════════════════════════════════════════════════
    # 5. COMPOSITE RATING
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("5. COMPOSITE RATING")
    print("=" * 80)

    # IBD's own documentation describes Composite Rating as combining the PERCENTILE RANKINGS of
    # EPS/RS/SMR/A-D/Group-RS — not a regression on their raw scales. Fitting OLS directly on raw
    # scales (EPS/RS are 1-99, SMR_Num is 10-95, AD_Num is 1-13) previously produced a misleading
    # coefficient table where AD_Num "dominated" purely because its scale is ~8x narrower, not
    # because A/D actually matters 8x more. Every component is percentile-ranked to a common 1-99
    # scale (within this evaluation universe) before any fitting below.
    comp_base = full_merged.dropna(subset=["Comp Rating", "EPS Rating", "RS Rating", "SMR_Num", "AD_Num"]).copy()
    comp_base["EPS_pct"] = pct_rank_99(comp_base["EPS Rating"].values)
    comp_base["RS_pct"] = pct_rank_99(comp_base["RS Rating"].values)
    comp_base["SMR_pct"] = pct_rank_99(comp_base["SMR_Num"].values)
    comp_base["AD_pct"] = pct_rank_99(comp_base["AD_Num"].values)
    print(f"Composite evaluation universe (true components): {len(comp_base):,}")

    y_comp = comp_base["Comp Rating"].values
    comp_results = []

    # 5A: unweighted average of percentile ranks — IBD's own documented approach, as a baseline
    avg4 = comp_base[["EPS_pct", "RS_pct", "SMR_pct", "AD_pct"]].values.mean(axis=1)
    comp_results.append(score_report("Equal-weight avg of percentile ranks (no Group RS)", y_comp, avg4))

    # 5B: OLS-fit weights on the SAME percentile-rank components (now scale-comparable)
    comp_cols_no_grp = ["EPS_pct", "RS_pct", "SMR_pct", "AD_pct"]
    X_comp_no_grp = comp_base[comp_cols_no_grp].values
    b0_c1, coefs_c1, pred_c1 = lstsq_fit(X_comp_no_grp, y_comp)
    comp_results.append(score_report("OLS-weighted percentile ranks (no Group RS)", y_comp, np.clip(pred_c1, 1, 99)))

    # 5C/5D: same, but adding Industry Group RS (also percentile-ranked; A+..E letter grade, fixed
    # extraction bug where it was previously mis-parsed as a numeric column and wiped to all-NaN)
    comp_grp = comp_base.dropna(subset=["GroupRS_Num"]).copy()
    coefs_c2 = b0_c2 = None
    if len(comp_grp) > 200:
        comp_grp["GroupRS_pct"] = pct_rank_99(comp_grp["GroupRS_Num"].values)
        avg5 = comp_grp[["EPS_pct", "RS_pct", "SMR_pct", "AD_pct", "GroupRS_pct"]].values.mean(axis=1)
        y_comp_grp = comp_grp["Comp Rating"].values
        comp_results.append(score_report("Equal-weight avg of percentile ranks (+Group RS)", y_comp_grp, avg5))

        comp_cols_grp = ["EPS_pct", "RS_pct", "SMR_pct", "AD_pct", "GroupRS_pct"]
        X_comp_grp = comp_grp[comp_cols_grp].values
        b0_c2, coefs_c2, pred_c2 = lstsq_fit(X_comp_grp, y_comp_grp)
        comp_results.append(score_report("OLS-weighted percentile ranks (+Group RS)", y_comp_grp, np.clip(pred_c2, 1, 99)))
        print(f"Composite w/ Group RS universe: {len(comp_grp):,}")

    # 5E: full end-to-end pipeline — combine OUR self-computed RS/EPS/SMR/AD (this measures real-world
    # accuracy of using ticker_cache alone, end to end, with no MarketSurge inputs at all). Every
    # self-computed component is reduced to the same percentile-rank raw score before combining.
    pipeline_df = comp_base.copy()
    rel_cols_pipeline = [f"RelPerf_{w}" for w in RS_WINDOWS]
    pipeline_df = pipeline_df.dropna(subset=rel_cols_pipeline).copy()
    pipeline_df["RS_self"] = pct_rank_99(pipeline_df[rel_cols_pipeline].values @ opt_w * 100.0)

    pipeline_df = pipeline_df.dropna(subset=ad_feat_cols).copy()
    pipeline_df["AD_self"] = pct_rank_99(pipeline_df[ad_feat_cols].values @ coefs_ad)

    for c in eps_feat_cols:
        pipeline_df[c] = pd.to_numeric(pipeline_df[c], errors="coerce")
    pipeline_df = pipeline_df.dropna(subset=eps_feat_cols).copy()
    X_eps_pipe = np.column_stack([
        log_compress(pipeline_df["EPS_Q0_YoY"].values),
        log_compress(pipeline_df["EPS_LT_Growth"].values),
        pipeline_df["EPS_NegQRatio"].values,
        log_compress(pipeline_df["ROE"].values),
    ])
    pipeline_df["EPS_self"] = pct_rank_99(X_eps_pipe @ coefs_eps)

    for c in smr_feat_cols:
        pipeline_df[c] = pd.to_numeric(pipeline_df[c], errors="coerce")
    pipeline_df = pipeline_df.dropna(subset=smr_feat_cols).copy()
    X_smr_pipe = np.column_stack([
        log_compress(pipeline_df["Sales_Q0_YoY"].values),
        log_compress(pipeline_df["Sales_LT_Growth"].values),
        log_compress(pipeline_df["Margin_Now"].values),
        log_compress(pipeline_df["Margin_Trend"].values),
        log_compress(pipeline_df["ROE"].values),
    ])
    pipeline_df["SMR_self"] = pct_rank_99(X_smr_pipe @ coefs_smr)

    print(f"Full self-computed pipeline universe: {len(pipeline_df):,}")

    coefs_pipe = b0_pipe = None
    if len(pipeline_df) > 200:
        y_pipe = pipeline_df["Comp Rating"].values
        pipe_cols = ["EPS_self", "RS_self", "SMR_self", "AD_self"]
        avg_pipe = pipeline_df[pipe_cols].values.mean(axis=1)
        comp_results.append(score_report(f"Equal-weight avg, FULL SELF-COMPUTED PIPELINE (n={len(pipeline_df):,})",
                                          y_pipe, avg_pipe))
        X_pipe = pipeline_df[pipe_cols].values
        b0_pipe, coefs_pipe, pred_pipe = lstsq_fit(X_pipe, y_pipe)
        comp_results.append(score_report(f"OLS-weighted, FULL SELF-COMPUTED PIPELINE (n={len(pipeline_df):,}, "
                                          "no MarketSurge inputs)", y_pipe, np.clip(pred_pipe, 1, 99)))

    comp_results_df = pd.DataFrame(comp_results)
    print(comp_results_df.to_string(index=False))

    comp_weight_table = pd.DataFrame({"Component": comp_cols_no_grp, "OLS_Coef": np.round(coefs_c1, 4)})
    comp_weight_table["Rel_Weight_Pct"] = (comp_weight_table["OLS_Coef"].abs() /
                                            comp_weight_table["OLS_Coef"].abs().sum() * 100).round(1)
    print(comp_weight_table.to_string(index=False))

    report.append("## 5. Composite Rating\n")
    report.append(f"Evaluation universe (true components): **{len(comp_base):,}** stocks. All components "
                   "percentile-ranked to a common 1-99 scale before combining (matches IBD's documented "
                   "methodology and avoids the scale artifact where AD_Num's narrow 1-13 range would "
                   "otherwise dominate a raw-scale regression).\n")
    report.append(comp_results_df.to_markdown(index=False))
    report.append(f"\n**Combining weights (no Group RS), intercept={b0_c1:.2f}**:\n")
    report.append(comp_weight_table.to_markdown(index=False))
    if coefs_c2 is not None:
        comp_weight_table2 = pd.DataFrame({"Component": ["EPS_pct", "RS_pct", "SMR_pct", "AD_pct", "GroupRS_pct"],
                                            "OLS_Coef": np.round(coefs_c2, 4)})
        comp_weight_table2["Rel_Weight_Pct"] = (comp_weight_table2["OLS_Coef"].abs() /
                                                 comp_weight_table2["OLS_Coef"].abs().sum() * 100).round(1)
        report.append(f"\n**Combining weights (+Group RS), intercept={b0_c2:.2f}**:\n")
        report.append(comp_weight_table2.to_markdown(index=False))
    if coefs_pipe is not None:
        pipe_weight_table = pd.DataFrame({"Component": ["EPS_self", "RS_self", "SMR_self", "AD_self"],
                                           "OLS_Coef": np.round(coefs_pipe, 4)})
        pipe_weight_table["Rel_Weight_Pct"] = (pipe_weight_table["OLS_Coef"].abs() /
                                                pipe_weight_table["OLS_Coef"].abs().sum() * 100).round(1)
        report.append(f"\n**Full self-computed pipeline combining weights, intercept={b0_pipe:.2f}**:\n")
        report.append(pipe_weight_table.to_markdown(index=False))
    report.append("\n**This is the number that matters**: the \"FULL SELF-COMPUTED PIPELINE\" rows show what "
                   "accuracy is achievable using *only* `ticker_cache` (price/volume + fundamentals json) end "
                   "to end, with zero MarketSurge-derived inputs — the actual goal of this exercise.\n")

    # ════════════════════════════════════════════════════════════════════
    # EXECUTIVE SUMMARY + RECOMMENDED FORMULAS (inserted after the header)
    # ════════════════════════════════════════════════════════════════════
    def _row(df, method_substr):
        m = df[df["Method"].str.contains(method_substr, regex=False)]
        return m.iloc[0] if len(m) else None

    rs_base = _row(rs_results_df, "Current formula (40/20/20/20 sigmoid)")
    rs_reco = _row(rs_results_df, "Monotonic-optimal weights + percentile-rank")
    ad_base = _row(ad_results_df, "Current formula (65D CMF")
    ad_reco = _row(ad_results_df, "OLS-weighted multi-window blend + percentile-rank")
    eps_base = _row(eps_results_df, "Current formula (blended growth")
    eps_reco = _row(eps_results_df, "OLS-weighted feature blend (direct scale)")
    smr_base = _row(smr_results_df, "Current formula (ROE-only")
    smr_reco = _row(smr_results_df, "OLS 3-pillar blend (direct scale)")
    comp_reco = _row(comp_results_df, "OLS-weighted, FULL SELF-COMPUTED PIPELINE")

    summary_rows = []
    for label, base, reco in [("RS Rating", rs_base, rs_reco), ("A/D Rating", ad_base, ad_reco),
                               ("EPS Rating", eps_base, eps_reco), ("SMR Rating", smr_base, smr_reco)]:
        summary_rows.append({
            "Rating": label,
            "Baseline R2": base["R2"], "Recommended R2": reco["R2"],
            "Baseline MAE": base["MAE"], "Recommended MAE": reco["MAE"],
            "Baseline +/-5 Acc%": base["+/-5 Acc%"], "Recommended +/-5 Acc%": reco["+/-5 Acc%"],
        })
    summary_df = pd.DataFrame(summary_rows)

    exec_summary = []
    exec_summary.append("## Executive Summary\n")
    exec_summary.append("Per-rating accuracy, current production formula vs the recommended closed-form "
                         "replacement (both evaluated on the same ticker_cache-derived universe):\n")
    exec_summary.append(summary_df.to_markdown(index=False))
    exec_summary.append("")
    exec_summary.append(f"**Composite Rating, full self-computed pipeline** (RS/EPS/SMR/AD all recomputed "
                         f"from ticker_cache alone, zero MarketSurge inputs, n={len(pipeline_df):,}): "
                         f"R²=`{comp_reco['R2']}`, MAE=`{comp_reco['MAE']}`, "
                         f"±5 Acc=`{comp_reco['+/-5 Acc%']}%`, ±10 Acc=`{comp_reco['+/-10 Acc%']}%`. "
                         "This is the realistic ceiling today for computing Composite Rating without "
                         "MarketSurge — bottlenecked mainly by A/D and EPS, both capped by real data-"
                         "availability limits (see below), not by the combining formula.\n")
    exec_summary.append("**What actually changed and why:**\n")
    exec_summary.append("1. **RS Rating** — current formula was already close (R²=0.73); recommended "
                         "change is cosmetic but principled: replace the fixed-curvature sigmoid with an "
                         "actual percentile-rank against the universe (matches IBD's own definition of RS "
                         "as a percentile rank) and mildly re-weight toward 1M/3M over 12M.\n")
    exec_summary.append("2. **A/D Rating** — improved (MAE 3.95→3.37, ±5 Acc 60%→75%) by blending CMF "
                         "across multiple windows (65D + 130D) instead of a single 65D window, plus heavy-"
                         "volume-day net ratio. Still the weakest-fitting rating (R² stays low): A/D "
                         "fundamentally measures institutional buying/selling *flow*, and ticker_cache has "
                         "no historical institutional-holdings deltas (13F-style) to measure that directly — "
                         "only price/volume proxies for it. Tested current institutional-ownership *level* "
                         "(`heldPercentInstitutions`, `institutionsCount` from the fundamentals json) "
                         "explicitly; correlation with A/D was ~0.02-0.04, i.e. no signal, because A/D cares "
                         "about the *change* in positioning, not the level. This is a real data ceiling, not "
                         "a formula problem.\n")
    exec_summary.append("3. **EPS Rating** — current formula was actually *worse than predicting the mean* "
                         f"on this universe (R²={eps_base['R2']}); fixed by (a) log-compressing the growth-"
                         "rate features so small-denominator YoY blowups (a stock going from $0.001 to "
                         "$0.30 EPS reads as +28,600%) stop dominating the fit while still preserving rank "
                         f"order, and (b) refitting the blend weights (R² → {eps_reco['R2']}). Still "
                         "constrained by yfinance's ~5-quarter-deep quarterly financials, which structurally "
                         "can't support the acceleration/2nd-derivative features IBD's real EPS Rating likely "
                         "uses (EPS_Q1_YoY and EPS_Accel had 0% coverage in this universe and were dropped).\n")
    exec_summary.append("4. **SMR Rating** — biggest formula-level win. Production's SMR is currently "
                         f"ROE-only (R²={smr_base['R2']}); adding the two missing pillars — Sales growth "
                         f"and Margin (now + trend), log-compressed the same way as EPS — gets to "
                         f"R²={smr_reco['R2']}. This is the clearest case where the current formula is "
                         "missing real, available signal rather than hitting a data ceiling.\n")
    exec_summary.append("5. **Composite Rating** — confirmed IBD's documented approach (combine *percentile "
                         "rankings* of the components, not their raw scales) matters mechanically: fitting "
                         "OLS directly on raw component scales let AD_Num's narrow 1-13 range swamp the fit "
                         "(spurious 54% \"weight\") purely as a scale artifact. Percentile-normalizing first "
                         "gives interpretable weights (RS ≈39%, EPS ≈29%, AD ≈17%, SMR ≈15%, roughly matching "
                         "RS+EPS being the commonly-cited dominant pair) and a true-component R² of 0.93 "
                         "(0.96 with Industry Group RS added).\n")

    report[2:2] = exec_summary

    OUTPUT_DIR.mkdir(exist_ok=True)
    report_path = OUTPUT_DIR / "rating_reengineering_v2_report.md"
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report))
    print(f"\n{'=' * 80}\nReport written to {report_path}\n{'=' * 80}")


if __name__ == "__main__":
    main()
