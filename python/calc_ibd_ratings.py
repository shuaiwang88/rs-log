"""
calc_ibd_ratings.py

Python implementation of the IBD-style Ratings Scanner, computing RS Rating,
EPS Rating, A/D Rating, SMR Rating, and Composite Rating from daily OHLCV data
(ticker_cache/*_1d.parquet) and fundamental data (ticker_cache/*_fund.json).

Methodology (walk-forward calibrated in python/fit_production_ratings.py — fit on
the 2026-07-24 MarketSurge snapshot, forward-validated on 2026-08-07, no retraining
between them):

  * RS Rating   = percentile rank of a monotonic-recency-weighted blend of the
                  stock's own ABSOLUTE trailing returns (1M/3M/6M/9M/12M), against
                  the eligible universe. NOT SPY-relative: summing w_i * (stock_i /
                  spy_i) across windows with a DIFFERENT SPY divisor per window
                  distorts the cross-window weighting by whatever shape SPY's own
                  return took that period — it is not equivalent to ranking the
                  stock's own performance. The universe-wide percentile rank already
                  captures "beat the market," since every stock in the ranking pool
                  faced the same SPY backdrop that day.
  * A/D Rating  = percentile rank of a ridge-regularized multi-window Chaikin-money-
                  flow / heavy-volume-day / moving-average-distance blend, converted
                  to an A+..E grade via train-frozen grade-frequency boundaries.
  * EPS Rating  = direct-scale OLS blend of log-compressed EPS growth/ROE features
                  (no percentile step — percentile-ranking measurably hurt EPS in
                  calibration, likely because the underlying signal-to-noise ratio
                  from yfinance's shallow ~5-quarter window is already low).
  * SMR Rating  = percentile rank of an OLS blend of log-compressed sales-growth,
                  margin (level + trend), and ROE features, -> A-E grade.
  * Comp Rating = linear combination of the four components above (RS/A-D/SMR as
                  percentile ranks, EPS as its own direct 1-99 scale), matching
                  IBD's documented "combines the percentile rankings" approach.

RS / A-D / SMR / Comp Rating are inherently UNIVERSE computations — a single
ticker cannot be percentile-ranked in isolation. The per-ticker functions below
(calc_rs_raw_score, calc_ad_raw_score, calc_smr_raw_score) compute a RAW score
during a per-ticker pass; apply_rating_percentiles() is a universe post-pass (same
pattern as apply_group_columns) that ranks those raw scores against the CURRENT
eligible universe — price >= $4 and market cap >= $50M when known, IBD's own
junk/tiny-cap filter — and fills in the final ratings. Only EPS Rating is complete
after the per-ticker pass alone.

The module also hosts the IBD group-rank helpers: derive_ibd_asof() is a
standalone utility that detects the trading day the IBD_data.txt MarketSurge
snapshot reflects (kept for tooling that wants to date the IBD industry mapping;
the daily screener's group columns are now fully computed from live RS), and
apply_group_columns() is the universe post-pass that turns per-ticker RS
ratings into group stats (Ind Group RS 1-99, computed Ind Group Rank, rank
history, new-high/low breadth, P/E percentile ranks, Earnings Stability,
profit-margin-vs-industry).
"""

from pathlib import Path

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────────────────────
# FITTED PARAMETERS — walk-forward calibrated in python/fit_production_ratings.py
# (fit on MarketSurge 2026-07-24, forward-tested on 2026-08-07). See
# output/production_fitted_params.json for the full calibration record.
# ──────────────────────────────────────────────────────────────────────────────

RS_RAW_WINDOWS = {"1M": 21, "3M": 63, "6M": 126, "9M": 188, "12M": 249}
RS_RAW_WEIGHTS = {"1M": 0.2398615489255944, "3M": 0.2398597773981456, "6M": 0.23259603611327384,
                   "9M": 0.22243834909982305, "12M": 0.06524428846316314}
# Trend-confirmation gate: a stock's 9M/12M returns only get full weight when its 3M return is
# also positive (the longer-term strength is "confirmed" by recent price action). When 3M is
# negative, 9M/12M are scaled down by this factor before blending. Without this, a stock that
# rallied hard 9-12 months ago and has since reversed (e.g. down -9%/-17%/-34%/-61% over
# 1M/3M/6M/9M but still +412% over 12M) reads as falsely strong from the stale 12M number alone
# (raw percentile ~90 vs a true RS Rating of 22) - while a stock merely pausing after a genuine
# uptrend (one soft month, but 3M/6M/9M all solidly positive) gets penalized for the SAME reason
# the crash case needs penalizing. Gating on whether 3M confirms or contradicts the longer trend
# fixes both simultaneously: TEST R²=0.845->0.869, MAE=7.60->6.88, corr=0.940->0.951, with no
# metric regressing (see python/fit_production_ratings.py).
RS_TREND_GATE_REDUCTION = 0.50

AD_RAW_FEATURES = [
    "UpDnVol_65D", "HeavyNetRatio_65D", "NetHeavyIntensity_65D", "CMF_65D",
    "UpDnVol_130D", "NetHeavyIntensity_130D", "UpDnVol_30D", "NetHeavyIntensity_30D",
    "Dist_10MA", "Dist_21MA", "Dist_50MA", "Dist_150MA", "Dist_200MA", "PctOff52WHigh",
    "UpDnVol_5D", "HeavyNetRatio_5D", "NetHeavyIntensity_5D", "CMF_5D",
    "UpDnVol_10D", "HeavyNetRatio_10D", "NetHeavyIntensity_10D", "CMF_10D",
]
AD_RAW_COEFS = [
    0.34967480193560485, 2.1897638921253963, -0.06719042537158329, 5.181897158177819,
    0.27691148574671515, -0.008050180344146483, 0.000403154704968294, -0.3654821919756783,
    -0.13610908645042324, 0.111329192782794, 0.14673085403905506, 0.2152969282340527,
    -0.1889154560921097, 0.002750442381063726, -1.942157058715131e-08, 0.010478581415874712,
    1.573336648964523, 0.3726299491741793, 0.0011519686892018336, 0.5875381980017129,
    -0.967261206595406, 4.673391221438038,
]
AD_RAW_INTERCEPT = 3.9244287979135892
AD_LETTERS_ORDERED = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"]
AD_CUM_TOP = {
    "A+": 0.0738, "A": 0.1577, "A-": 0.1956, "B+": 0.2494, "B": 0.3391, "B-": 0.3896,
    "C+": 0.4203, "C": 0.4874, "C-": 0.5291, "D+": 0.5670, "D": 0.6639, "D-": 0.7040, "E": 1.0,
}

EPS_RAW_FEATURES = ["EPS_Q0_YoY", "EPS_LT_Growth", "EPS_NegQRatio", "ROE",
                     "EPS_StabilityCV", "EpsSurpriseMean", "EpsBeatRate", "EpsRevTrend",
                     "EstEPSGrowth_Q", "EstEPSGrowth_Y"]
EPS_RAW_COEFS = [1.6670799437309827, 1.5727689904980522, -1.6688291454915354, 2.736583873896307,
                  -0.43052150673398204, 0.08072757657626828, 23.09505447850493, 0.2730948276664221,
                  0.808210542220797, 0.12539278151754665]
EPS_RAW_INTERCEPT = 35.004080630794206
# EpsBeatRate's coefficient (23.1) dwarfs the others - a company that consistently beats
# analyst estimates is a strong, largely independent EPS-Rating signal, found by comparing
# against IBD_rating_glm's parallel effort (R^2 0.345 vs our then-0.333) and confirmed by our
# own walk-forward refit (R^2 0.333 -> 0.416, same test population, no other metric regressed).
EPS_LOG_FEATURES = {"EPS_Q0_YoY", "EPS_LT_Growth", "ROE", "EpsSurpriseMean", "EpsRevTrend",
                     "EstEPSGrowth_Q", "EstEPSGrowth_Y"}
EPS_CLIP = {
    "EPS_Q0_YoY": (-300, 300), "EPS_LT_Growth": (-300, 300), "EpsSurpriseMean": (-300, 300),
    "EpsRevTrend": (-300, 300), "EstEPSGrowth_Q": (-300, 300), "EstEPSGrowth_Y": (-300, 300),
    "EPS_StabilityCV": (0, 10), "EPS_NegQRatio": (0, 1), "EpsBeatRate": (0, 1),
}
EPS_MEDIANS = {
    "EPS_Q0_YoY": 17.089216944801024, "EPS_LT_Growth": 11.005390522399962,
    "EPS_NegQRatio": 0.0, "ROE": 10.6835003,
    "EPS_StabilityCV": 1.4707799687755612, "EpsSurpriseMean": 6.9287499375,
    "EpsBeatRate": 0.75, "EpsRevTrend": -0.0724612846210726,
    "EstEPSGrowth_Q": 10.8150002, "EstEPSGrowth_Y": 13.01,
}

SMR_RAW_FEATURES = ["Sales_Q0_YoY", "Sales_LT_Growth", "Margin_Now", "Margin_Trend", "ROE"]
SMR_RAW_COEFS = [1.0836196928957507, 4.04823699701578, 3.078193914449797, -1.2385417551305598, 4.330382100668682]
SMR_RAW_INTERCEPT = 43.494514603313135
SMR_MEDIANS = {
    "Sales_Q0_YoY": 9.505376828065279, "Sales_LT_Growth": 6.816226945651465,
    "Margin_Now": 9.098851021951301, "Margin_Trend": 0.34718079468168384, "ROE": 10.7015,
}
SMR_LETTERS_ORDERED = ["A", "B", "C", "D", "E"]
SMR_CUM_TOP = {"A": 0.3014, "B": 0.5822, "C": 0.8143, "D": 0.9670, "E": 1.0}

COMPOSITE_COEFS = {"EPS": 0.29733326821052003, "RS": 0.5191465672786759,
                    "SMR": 0.22740995821369406, "AD": 0.2109262117191658}
COMPOSITE_INTERCEPT = -1.8129595138811236

# IBD-style eligibility filter for the RATING universe (the comparison pool that
# percentile ranks are computed against). Junk/illiquid/tiny-cap names are excluded
# from the pool and get every rating left blank, not scored.
RATING_MIN_PRICE = 4.0
RATING_MIN_MKTCAP_MIL = 50.0


def _log_compress(x):
    """Sign-preserving log compression: tames small-denominator YoY blowups (e.g. EPS
    growth off a near-zero base can read +28,600%) while preserving rank order, unlike
    a hard clip which throws away the difference between merely-large and absurdly-
    large values."""
    x = np.asarray(x, dtype=float)
    return np.sign(x) * np.log1p(np.abs(x))


# ──────────────────────────────────────────────────────────────────────────────
# RS RATING — raw score (per-ticker); apply_rating_percentiles() finishes the job
# ──────────────────────────────────────────────────────────────────────────────

def calc_rs_raw_score(close_series):
    """Weighted blend of the stock's own ABSOLUTE trailing returns (1M/3M/6M/9M/12M),
    with 9M/12M scaled down by RS_TREND_GATE_REDUCTION whenever the 3M return is
    negative (see RS_TREND_GATE_REDUCTION for why: a stale 12M gain from a rally
    that's already reversed shouldn't count the same as one still confirmed by
    recent price action).

    This is a RAW number, not a 1-99 rating — pass the whole universe's raw scores
    through apply_rating_percentiles() to get the final RS Rating. Returns NaN if
    the ticker doesn't have the full 12-month history the blend needs (matches the
    calibration: partial windows were never validated, so they're not guessed at
    here either).
    """
    close = close_series.values.astype(float) if hasattr(close_series, "values") else np.asarray(close_series, dtype=float)
    n = len(close)
    if n == 0:
        return np.nan
    latest = close[-1]
    rets = {}
    for label, days in RS_RAW_WINDOWS.items():
        if n <= days:
            return np.nan
        past = close[-(days + 1)]
        if not (past > 0):
            return np.nan
        rets[label] = (latest / past - 1.0) * 100.0

    gate = 1.0 if rets["3M"] >= 0 else RS_TREND_GATE_REDUCTION
    raw = (RS_RAW_WEIGHTS["1M"] * rets["1M"] + RS_RAW_WEIGHTS["3M"] * rets["3M"] +
           RS_RAW_WEIGHTS["6M"] * rets["6M"] + RS_RAW_WEIGHTS["9M"] * rets["9M"] * gate +
           RS_RAW_WEIGHTS["12M"] * rets["12M"] * gate)
    return float(raw)


def calc_rs_sub_raw_score(close_series, days):
    """Single-window absolute return (raw) for the RS 3-Month / RS 6-Month sub-ratings.
    Same percentile-rank treatment as the main RS Rating, just one window."""
    close = close_series.values.astype(float) if hasattr(close_series, "values") else np.asarray(close_series, dtype=float)
    n = len(close)
    if n <= days:
        return np.nan
    past = close[-(days + 1)]
    if not (past > 0):
        return np.nan
    return float((close[-1] / past - 1.0) * 100.0)


# ──────────────────────────────────────────────────────────────────────────────
# % OFF 52-WEEK HIGH
# ──────────────────────────────────────────────────────────────────────────────

def calc_pct_off_52w_high(df):
    """Percentage below 52-week (252-day) high: (high52w - close) / high52w * 100."""
    high = df["High"].values.astype(float)
    close = df["Close"].values.astype(float)
    n = len(close)

    pct_off = np.full(n, np.nan)
    for i in range(n):
        start = max(0, i - 252)
        h52 = np.nanmax(high[start:i + 1])
        if h52 > 0:
            pct_off[i] = (h52 - close[i]) / h52 * 100.0
        else:
            pct_off[i] = 0.0

    return pd.Series(pct_off, index=df.index, name="pct_off_52w_high")


def calc_pct_off_52w_high_snapshot(df):
    """% Off 52W High for the last bar."""
    high = df["High"].values.astype(float)
    close = df["Close"].values.astype(float)

    n = len(close)
    if n < 1:
        return np.nan

    start = max(0, n - 253)  # 252 + 1 for inclusive
    h52 = np.nanmax(high[start:n])
    if h52 > 0:
        return (h52 - close[-1]) / h52 * 100.0
    return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# A/D RATING — raw score (per-ticker); apply_rating_percentiles() finishes the job
# ──────────────────────────────────────────────────────────────────────────────

def _window_ad_features(prices, vols, highs, lows, w):
    """Chaikin-money-flow / heavy-volume-day accumulation stats over a trailing window."""
    if len(prices) < w:
        return None
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
    net_heavy_intensity = (np.sum(p_rets[heavy_up] * vratio[heavy_up]) -
                            np.sum(np.abs(p_rets[heavy_dn]) * vratio[heavy_dn]))

    rng = np.maximum(1e-6, wh - wl)
    cls_rng = (wp - wl) / rng * 100.0
    vw_cls_rng = np.sum(cls_rng * wv) / max(1.0, np.sum(wv))
    mf_mult = ((wp - wl) - (wh - wp)) / rng
    cmf = np.sum(mf_mult * wv) / max(1.0, np.sum(wv))

    return {
        "UpDnVol": updn_ratio,
        "HeavyNetRatio": heavy_net_ratio,
        "NetHeavyIntensity": net_heavy_intensity,
        "VWClsRange": vw_cls_rng,
        "CMF": cmf,
    }


def calc_ad_raw_score(df):
    """Ridge-regularized blend of multi-window (5/10/30/65/130D) Chaikin-money-flow /
    heavy-volume-day features plus moving-average-distance and % off 52-week high.

    The short 5D/10D windows were added after A/B-testing against GLM's broader
    AD_WINDOWS and confirmed a real out-of-sample win (TEST within-1-grade accuracy
    53.8%->56.7%) — recent accumulation/distribution carries signal the 30D+ windows
    alone smooth away. CMF_130D and VWClsRange_65D were then dropped (VWClsRange_65D
    correlates 0.98 with CMF_65D - same signal; CMF_130D correlates 0.75 with CMF_65D,
    causing the ridge fit to split a large canceling coefficient pair across them - a
    collinearity artifact, not independent signal) and Dist_10MA/21MA added (short-
    horizon price position). Net: TEST exact 32.1%->36.3%, within-1 51.5%->57.2%
    (see python/fit_production_ratings.py).

    RAW number, not a grade — apply_rating_percentiles() percentile-ranks this
    against the eligible universe and converts to an A+..E grade. Returns NaN if
    the ticker doesn't have the ~250 days of history the feature set needs.
    """
    close = df["Close"].values.astype(float)
    high = df["High"].values.astype(float)
    low = df["Low"].values.astype(float)
    volume = df["Volume"].values.astype(float)
    n = len(close)
    if n < 250:
        return np.nan

    feats = {}
    for w in (5, 10, 30, 65, 130):
        wf = _window_ad_features(close, volume, high, low, w)
        if wf is None:
            return np.nan
        for k, v in wf.items():
            feats[f"{k}_{w}D"] = v

    latest = close[-1]
    for ma in (10, 21, 50, 150, 200):
        feats[f"Dist_{ma}MA"] = (latest / np.mean(close[-ma:]) - 1.0) * 100.0
    h52 = np.max(close[-253:])
    feats["PctOff52WHigh"] = (h52 - latest) / h52 * 100.0 if h52 > 0 else 0.0

    vals = np.array([feats.get(c, np.nan) for c in AD_RAW_FEATURES])
    if np.isnan(vals).any():
        return np.nan
    return float(AD_RAW_INTERCEPT + np.dot(vals, AD_RAW_COEFS))


# ──────────────────────────────────────────────────────────────────────────────
# EPS RATING (1-99) — direct scale, no percentile step needed
# ──────────────────────────────────────────────────────────────────────────────

def calc_eps_rating(fy_eps, fq_eps, roe_val, eps_stability_cv=None, eps_surprise_mean=None,
                     eps_beat_rate=None, eps_rev_trend=None, est_eps_growth_q=None,
                     est_eps_growth_y=None):
    """Calculate EPS Rating (1-99) from annual/quarterly EPS data plus analyst signals.

    Direct-scale OLS blend of log-compressed growth/ROE/analyst features (percentile-
    ranking was tested in calibration and measurably hurt EPS — likely because
    the signal is already low-SNR from yfinance's shallow ~5-quarter window, and
    percentile-ranking a noisy raw score just re-orders the noise). This IS the
    final EPS Rating already; no universe pass required.

    The 6 keyword args (from extract_eps_analyst_features()) are optional and
    median-imputed when unavailable — a ticker with no earnings-history/estimate
    data still gets a rating from the original 4 fundamentals-only features.

    Parameters
    ----------
    fy_eps : list of float (length >= 2)
        Annual diluted EPS values, most recent first [FY0, FY-1, FY-2, ...].
    fq_eps : list of float (length >= 5)
        Quarterly diluted EPS values, most recent first [Q0, Q-1, Q-2, ...].
    roe_val : float or None
        Return on Equity from the most recent quarter (as a percent, e.g. 15.0).

    Returns
    -------
    int : EPS Rating (1-99), or a data-poor fallback near the population median
    if insufficient EPS history is available.
    """
    # fq_eps/fy_eps may now contain None at a calendar-correct position (a quarter/
    # year with no reported EPS) rather than being dropped, so every offset compare
    # below must guard BOTH sides of the subtraction, not just the divisor side.
    q0g = None
    if (fq_eps and len(fq_eps) > 4 and fq_eps[0] is not None and fq_eps[4] is not None
            and abs(fq_eps[4]) > 1e-9):
        q0g = (fq_eps[0] - fq_eps[4]) / abs(fq_eps[4]) * 100.0

    lt_growth = None
    if fy_eps and len(fy_eps) > 1:
        sum_g, sum_w = 0.0, 0.0
        for j in range(min(len(fy_eps) - 1, 5)):
            if fy_eps[j] is not None and fy_eps[j + 1] is not None and abs(fy_eps[j + 1]) > 1e-9:
                gv = (fy_eps[j] - fy_eps[j + 1]) / abs(fy_eps[j + 1]) * 100.0
                w = 5 - j
                sum_g += gv * w
                sum_w += w
        if sum_w > 0:
            lt_growth = sum_g / sum_w

    neg_q, cnt_q = 0, 0
    if fq_eps and len(fq_eps) > 4:
        for j in range(min(len(fq_eps) - 4, 4)):
            if fq_eps[j] is not None and fq_eps[j + 4] is not None and abs(fq_eps[j + 4]) > 1e-9:
                gv = (fq_eps[j] - fq_eps[j + 4]) / abs(fq_eps[j + 4]) * 100.0
                cnt_q += 1
                if gv < 0:
                    neg_q += 1
    neg_ratio = neg_q / cnt_q if cnt_q > 0 else 0.0

    raw_by_feature = {
        "EPS_Q0_YoY": q0g, "EPS_LT_Growth": lt_growth, "EPS_NegQRatio": neg_ratio, "ROE": roe_val,
        "EPS_StabilityCV": eps_stability_cv, "EpsSurpriseMean": eps_surprise_mean,
        "EpsBeatRate": eps_beat_rate, "EpsRevTrend": eps_rev_trend,
        "EstEPSGrowth_Q": est_eps_growth_q, "EstEPSGrowth_Y": est_eps_growth_y,
    }
    vals = []
    for feat in EPS_RAW_FEATURES:
        v = raw_by_feature[feat]
        v = v if v is not None else EPS_MEDIANS[feat]
        lo, hi = EPS_CLIP.get(feat, (-np.inf, np.inf))
        v = max(lo, min(hi, v))
        vals.append(_log_compress(v) if feat in EPS_LOG_FEATURES else v)

    raw = EPS_RAW_INTERCEPT + float(np.dot(np.array(vals), EPS_RAW_COEFS))
    return int(max(1, min(99, round(raw))))


# ──────────────────────────────────────────────────────────────────────────────
# SMR RATING — raw score (per-ticker); apply_rating_percentiles() finishes the job
# ──────────────────────────────────────────────────────────────────────────────

def _series_from_label(block, labels):
    """block[label] = {date_str: value}; returns (dates_desc, values) of first matching label.

    dates/values stay in lockstep with one entry per calendar period - a period with
    no reported value keeps a None in `values` rather than being dropped, so vals[4]
    stays "4 periods back" even when an earlier period is missing (see the identical
    fix in extract_eps_from_fundamentals for why dropping instead of preserving
    silently misaligns every offset-based comparison downstream)."""
    if not isinstance(block, dict):
        return None
    for lbl in labels:
        col = block.get(lbl)
        if isinstance(col, dict) and col:
            dates = sorted(col.keys(), reverse=True)
            vals = []
            for d in dates:
                v = col[d]
                if v is None:
                    vals.append(None)
                    continue
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    vals.append(None)
            if sum(1 for x in vals if x is not None) >= 2:
                return dates, vals
    return None


def extract_smr_inputs_from_fundamentals(fund):
    """Extract SMR's sales-growth + margin inputs from the fundamentals cache dict.

    Returns (sales_q0_yoy, sales_lt_growth, margin_now, margin_trend) — Nones where
    the underlying quarterly/annual data isn't available; calc_smr_raw_score()
    median-imputes any that come back None.
    """
    if not fund or fund.get("error"):
        return None, None, None, None

    rev_q = _series_from_label(fund.get("income_q"), ("Total Revenue",))
    rev_a = _series_from_label(fund.get("income_a"), ("Total Revenue",))
    ni_q = _series_from_label(fund.get("income_q"), ("Net Income", "Net Income Common Stockholders"))

    sales_q0_yoy = None
    if rev_q:
        _, vals = rev_q
        if len(vals) > 4 and vals[0] is not None and vals[4] is not None and abs(vals[4]) > 1e-9:
            sales_q0_yoy = (vals[0] - vals[4]) / abs(vals[4]) * 100.0

    sales_lt_growth = None
    if rev_a:
        _, vals = rev_a
        sum_g, sum_w = 0.0, 0.0
        for j in range(min(len(vals) - 1, 5)):
            if vals[j] is not None and vals[j + 1] is not None and abs(vals[j + 1]) > 1e-9:
                gv = (vals[j] - vals[j + 1]) / abs(vals[j + 1]) * 100.0
                w = 5 - j
                sum_g += gv * w
                sum_w += w
        if sum_w > 0:
            sales_lt_growth = sum_g / sum_w

    margin_now = margin_trend = None
    if rev_q and ni_q:
        rdates, rvals = rev_q
        ndates, nvals = ni_q
        rmap, nmap = dict(zip(rdates, rvals)), dict(zip(ndates, nvals))
        margins = [nmap[d] / rmap[d] * 100.0 for d in rdates
                   if nmap.get(d) is not None and rmap.get(d) is not None and abs(rmap[d]) > 1e-6]
        if margins:
            margin_now = margins[0]
            if len(margins) >= 3:
                margin_trend = float(np.mean(margins[:2]) - np.mean(margins[-2:]))

    return sales_q0_yoy, sales_lt_growth, margin_now, margin_trend


def calc_smr_raw_score(sales_q0_yoy, sales_lt_growth, margin_now, margin_trend, roe_val):
    """Raw SMR blend: log-compressed OLS combination of sales growth (short + long
    term), margin (level + trend), and ROE.

    RAW number, not a grade — apply_rating_percentiles() percentile-ranks this
    against the eligible universe (the percentile value is also SMR's numeric
    contribution to Composite Rating) and converts to an A-E grade.
    """
    def _f(v, med):
        return v if (v is not None and np.isfinite(v)) else med

    vals = np.array([
        _log_compress(_f(sales_q0_yoy, SMR_MEDIANS["Sales_Q0_YoY"])),
        _log_compress(_f(sales_lt_growth, SMR_MEDIANS["Sales_LT_Growth"])),
        _log_compress(_f(margin_now, SMR_MEDIANS["Margin_Now"])),
        _log_compress(_f(margin_trend, SMR_MEDIANS["Margin_Trend"])),
        _log_compress(_f(roe_val, SMR_MEDIANS["ROE"])),
    ])
    return float(SMR_RAW_INTERCEPT + np.dot(vals, SMR_RAW_COEFS))


# ──────────────────────────────────────────────────────────────────────────────
# UNIVERSE POST-PASS — percentile-ranks raw scores, finalizes RS/A-D/SMR/Composite
# ──────────────────────────────────────────────────────────────────────────────

def letter_from_pct(pct, letters_ordered, cum_top):
    """Assign a letter grade from a 1-99 percentile using train-frozen grade-frequency
    boundaries (cum_top[g] = cumulative population share from the best grade down
    through g). `letters_ordered` must be best-to-worst."""
    if pct is None or (isinstance(pct, float) and np.isnan(pct)):
        return None
    for g in letters_ordered:
        if pct / 100.0 >= 1.0 - cum_top[g]:
            return g
    return letters_ordered[-1]


def apply_rating_percentiles(out, min_price=None, min_mktcap_mil=None):
    """Universe post-pass (same pattern as apply_group_columns): turns the raw
    per-ticker scores already on `out` into final RS Rating / RS 3-Month Rating /
    RS 6-Month Rating / A/D Rating / SMR Rating / Composite Rating, percentile-
    ranked LIVE against the CURRENT eligible universe.

    Eligibility = price >= min_price AND (market cap unknown OR market cap >=
    min_mktcap_mil) — IBD's own junk/tiny-cap filter. Ineligible tickers get every
    rating left blank (None/NaN), not scored, matching this pipeline's existing
    "leave blank rather than guess" convention.

    Ranking against the LIVE current universe (not a frozen historical reference)
    is deliberate: percentile rank is supposed to self-normalize to today's overall
    market dispersion, which a frozen reference would undermine as it goes stale.
    Both production call sites (build_daily_screener.py, app.py's Ratings Scanner)
    already loop over the full universe per run, so this is always available.

    Requires hidden per-ticker fields _rs_raw, _rs3m_raw, _rs6m_raw, _ad_raw,
    _smr_raw, plus 'Current Price', 'Market Cap (mil)', and 'EPS Rating' to
    already be on `out`.
    """
    min_price = RATING_MIN_PRICE if min_price is None else min_price
    min_mktcap_mil = RATING_MIN_MKTCAP_MIL if min_mktcap_mil is None else min_mktcap_mil

    price = pd.to_numeric(out.get("Current Price"), errors="coerce")
    mktcap = pd.to_numeric(out.get("Market Cap (mil)"), errors="coerce")
    eligible = (price >= min_price) & (mktcap.isna() | (mktcap >= min_mktcap_mil))
    out["_rating_eligible"] = eligible

    def _pct_rank_99(raw_col):
        s = pd.to_numeric(out.get(raw_col), errors="coerce")
        pool = s[eligible].dropna()
        ranks = pd.Series(np.nan, index=s.index)
        if len(pool) > 0:
            ranks.loc[pool.index] = np.clip(pool.rank(pct=True, method="average") * 99, 1, 99)
        return ranks

    rs_pct = _pct_rank_99("_rs_raw")
    out["RS Rating"] = rs_pct.round(1)
    out["RS 3-Month Rating"] = _pct_rank_99("_rs3m_raw").round(1)
    out["RS 6-Month Rating"] = _pct_rank_99("_rs6m_raw").round(1)

    ad_pct = _pct_rank_99("_ad_raw")
    out["A/D Score"] = ad_pct.round(1)
    out["A/D Rating"] = [letter_from_pct(p, AD_LETTERS_ORDERED, AD_CUM_TOP) for p in ad_pct]
    if "_ad_prev_raw" in out.columns:
        ad_prev_pct = _pct_rank_99("_ad_prev_raw")
        out["A/D Rating - Pr Wk"] = [letter_from_pct(p, AD_LETTERS_ORDERED, AD_CUM_TOP) for p in ad_prev_pct]

    smr_pct = _pct_rank_99("_smr_raw")
    out["SMR Score"] = smr_pct.round(1)
    out["SMR Rating"] = [letter_from_pct(p, SMR_LETTERS_ORDERED, SMR_CUM_TOP) for p in smr_pct]

    eps = pd.to_numeric(out.get("EPS Rating"), errors="coerce").where(eligible)
    out["EPS Rating"] = eps

    comp_ok = eligible & eps.notna() & rs_pct.notna() & smr_pct.notna() & ad_pct.notna()
    comp_raw = (COMPOSITE_INTERCEPT + COMPOSITE_COEFS["EPS"] * eps + COMPOSITE_COEFS["RS"] * rs_pct +
                COMPOSITE_COEFS["SMR"] * smr_pct + COMPOSITE_COEFS["AD"] * ad_pct)
    out["Comp Rating"] = comp_raw.where(comp_ok).clip(1, 99).round(0)

    return out


# ──────────────────────────────────────────────────────────────────────────────
# EXTRACT EPS / ROE FROM COMPREHENSIVE FUNDAMENTALS CACHE
# ──────────────────────────────────────────────────────────────────────────────

def extract_eps_from_fundamentals(fund):
    """
    Extract FY EPS list, FQ EPS list, and ROE from the comprehensive
    fundamentals dict produced by fetch_fundamentals.fetch_all_fundamentals().

    Returns (fy_eps, fq_eps, roe_val) where:
      - fy_eps: list of annual diluted EPS (most recent first), or None
      - fq_eps: list of quarterly diluted EPS (most recent first), or None
      - roe_val: float ROE, or None
    """
    if not fund or fund.get('error'):
        return None, None, None

    fy_eps = None
    fq_eps = None
    roe_val = None

    # ── ROE from info dict ──
    info = fund.get('info')
    if isinstance(info, dict):
        roe = info.get('returnOnEquity')
        if roe is not None:
            try:
                roe_val = float(roe)
            except (ValueError, TypeError):
                pass

    # ── Quarterly EPS from income statement ──
    # Positions must stay calendar-aligned (index 4 = exactly 4 quarters back) since
    # calc_eps_rating() does fixed-offset YoY math (fq_eps[0] - fq_eps[4]). A quarter
    # with no reported Diluted EPS (common: anti-dilutive losses, late filings) must
    # keep its slot as None rather than being dropped - dropping it would silently
    # shift every earlier quarter into the wrong position instead of just leaving a
    # gap, turning "missing data" into "wrong data" for any ticker with a gap.
    income_q = fund.get('income_q')
    if isinstance(income_q, dict):
        for label in ('Diluted EPS', 'Diluted Earnings Per Share', 'Basic EPS'):
            col = income_q.get(label)
            if isinstance(col, dict) and col:
                # Values are { '2025-09-30': 1.85, '2025-06-30': 2.02, ... }
                # Sort by date descending (most recent first)
                sorted_dates = sorted(col.keys(), reverse=True)
                vals = []
                for d in sorted_dates:
                    v = col[d]
                    if v is None:
                        vals.append(None)
                        continue
                    try:
                        vals.append(float(v))
                    except (ValueError, TypeError):
                        vals.append(None)
                if sum(1 for x in vals if x is not None) >= 2:
                    fq_eps = vals
                    break

    # ── Annual EPS from income statement ──
    income_a = fund.get('income_a')
    if isinstance(income_a, dict):
        for label in ('Diluted EPS', 'Diluted Earnings Per Share', 'Basic EPS'):
            col = income_a.get(label)
            if isinstance(col, dict) and col:
                sorted_dates = sorted(col.keys(), reverse=True)
                vals = []
                for d in sorted_dates:
                    v = col[d]
                    if v is None:
                        vals.append(None)
                        continue
                    try:
                        vals.append(float(v))
                    except (ValueError, TypeError):
                        vals.append(None)
                if sum(1 for x in vals if x is not None) >= 2:
                    fy_eps = vals
                    break

    # ── Fallback: derive ROE from balance sheet + income statement ──
    if roe_val is None:
        balance_q = fund.get('balance_q')
        if isinstance(balance_q, dict) and isinstance(income_q, dict):
            for ni_label in ('Net Income', 'Net Income Common Stockholders'):
                ni_col = income_q.get(ni_label)
                if isinstance(ni_col, dict) and ni_col:
                    ni_first = sorted(ni_col.keys(), reverse=True)
                    if ni_first:
                        ni = ni_col[ni_first[0]]
                        break
            else:
                ni = None
            for eq_label in ('Stockholders Equity', 'Total Stockholder Equity',
                             'Total Equity Gross Minority Interest'):
                eq_col = balance_q.get(eq_label)
                if isinstance(eq_col, dict) and eq_col:
                    eq_first = sorted(eq_col.keys(), reverse=True)
                    if eq_first:
                        equity = eq_col[eq_first[0]]
                        break
            else:
                equity = None
            if ni is not None and equity is not None:
                try:
                    roe_val = round(float(ni) / float(equity) * 100.0, 2)
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

    return fy_eps, fq_eps, roe_val


def extract_eps_analyst_features(fund):
    """Extract the 6 analyst-driven EPS features calc_eps_rating() takes beyond the
    original fundamentals-only 4 (EPS_Q0_YoY/EPS_LT_Growth/EPS_NegQRatio/ROE).

    Ported from IBD_rating_glm's independent reverse-engineering effort, which
    forward-tested EPS higher (R^2 0.345 vs our then-0.333) using exactly this
    signal group; our own walk-forward refit with these added reached R^2 0.416
    on the same test population. Source tables (earnings_history, eps_trend,
    earnings_estimate) are already in the cached fund.json, just unused before.

    Returns (eps_stability_cv, eps_surprise_mean, eps_beat_rate, eps_rev_trend,
    est_eps_growth_q, est_eps_growth_y) — any of which may be None if that table
    isn't present for this ticker; calc_eps_rating() median-imputes them.
    """
    if not fund or fund.get('error'):
        return None, None, None, None, None, None

    eh = fund.get('earnings_history')
    surprises, beats = [], 0
    if isinstance(eh, dict):
        for k, v in eh.items():
            if k.startswith('_'):
                continue
            if isinstance(v, dict) and v.get('epsActual') is not None:
                s = v.get('surprisePercent')
                if isinstance(s, (int, float)):
                    surprises.append(float(s) * 100.0)
                diff = v.get('epsDifference')
                if diff is not None:
                    try:
                        if float(diff) > 0:
                            beats += 1
                    except (TypeError, ValueError):
                        pass
    eps_surprise_mean = float(np.mean(surprises)) if surprises else None
    eps_beat_rate = (beats / len(surprises)) if surprises else None

    # EPS stability: CV of YoY EPS growth (quarterly; falls back to annual if <3 quarterly points)
    income_q = fund.get('income_q')
    income_a = fund.get('income_a')
    stab_vals = []
    if isinstance(income_q, dict):
        for label in ('Diluted EPS', 'Diluted Earnings Per Share', 'Basic EPS'):
            col = income_q.get(label)
            if isinstance(col, dict) and col:
                dates = sorted(col.keys(), reverse=True)
                vals = [col[d] for d in dates]
                for j in range(min(len(vals) - 4, 4)):
                    a, b = vals[j], vals[j + 4]
                    if a is not None and b is not None and abs(float(b)) > 1e-9:
                        stab_vals.append((float(a) - float(b)) / abs(float(b)) * 100.0)
                break
    if len(stab_vals) < 3 and isinstance(income_a, dict):
        for label in ('Diluted EPS', 'Diluted Earnings Per Share', 'Basic EPS'):
            col = income_a.get(label)
            if isinstance(col, dict) and col:
                dates = sorted(col.keys(), reverse=True)
                vals = [col[d] for d in dates]
                stab_vals = []
                for j in range(min(len(vals) - 1, 4)):
                    a, b = vals[j], vals[j + 1]
                    if a is not None and b is not None and abs(float(b)) > 1e-9:
                        stab_vals.append((float(a) - float(b)) / abs(float(b)) * 100.0)
                break
    eps_stability_cv = (float(np.std(stab_vals) / max(1e-9, abs(np.mean(stab_vals))))
                        if len(stab_vals) >= 3 else None)

    et = fund.get('eps_trend')
    eps_rev_trend = None
    if isinstance(et, dict) and isinstance(et.get('0q'), dict):
        cur = et['0q'].get('current')
        ago = et['0q'].get('90daysAgo')
        if cur is not None and ago and float(ago) != 0:
            eps_rev_trend = (float(cur) / float(ago) - 1) * 100.0

    ee = fund.get('earnings_estimate')
    est_eps_growth_q = est_eps_growth_y = None
    if isinstance(ee, dict):
        if isinstance(ee.get('0q'), dict) and ee['0q'].get('growth') is not None:
            est_eps_growth_q = float(ee['0q']['growth']) * 100.0
        if isinstance(ee.get('+1y'), dict) and ee['+1y'].get('growth') is not None:
            est_eps_growth_y = float(ee['+1y']['growth']) * 100.0

    return (eps_stability_cv, eps_surprise_mean, eps_beat_rate, eps_rev_trend,
            est_eps_growth_q, est_eps_growth_y)


# ──────────────────────────────────────────────────────────────────────────────
# EXTRACT KEY METRICS FROM FUNDAMENTALS FOR DISPLAY
# ──────────────────────────────────────────────────────────────────────────────

def extract_key_metrics(fund):
    """
    Extract key display metrics from the comprehensive fundamentals dict.
    Returns a flat dict of human-readable metrics.
    """
    info = fund.get('info') if fund else {}
    if not isinstance(info, dict):
        info = {}

    def _num(key, default=None):
        v = info.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    return {
        # Earnings
        'eps_ttm': _num('trailingEps'),
        'eps_forward': _num('forwardEps'),
        'eps_current_year': _num('epsCurrentYear'),
        'eps_growth_q': _num('earningsQuarterlyGrowth'),
        'eps_growth_y': _num('earningsGrowth'),
        # Revenue
        'revenue': _num('totalRevenue'),
        'revenue_growth': _num('revenueGrowth'),
        'revenue_per_share': _num('revenuePerShare'),
        # Profitability
        'roe': _num('returnOnEquity'),
        'roa': _num('returnOnAssets'),
        'profit_margin': _num('profitMargins'),
        'gross_margin': _num('grossMargins'),
        'operating_margin': _num('operatingMargins'),
        'ebitda': _num('ebitda'),
        # Valuation
        'pe_trailing': _num('trailingPE'),
        'pe_forward': _num('forwardPE'),
        'peg_ratio': _num('pegRatio'),
        'price_to_book': _num('priceToBook'),
        'price_to_sales': _num('priceToSalesTrailing12Months'),
        # Financial health
        'debt_to_equity': _num('debtToEquity'),
        'current_ratio': _num('currentRatio'),
        'quick_ratio': _num('quickRatio'),
        'free_cashflow': _num('freeCashflow'),
        'operating_cashflow': _num('operatingCashflow'),
        # Ownership
        'insider_pct': _num('heldPercentInsiders'),
        'institution_pct': _num('heldPercentInstitutions'),
        'short_pct_float': _num('shortPercentOfFloat'),
        'short_ratio': _num('shortRatio'),
        # Growth & targets
        'revenue_growth_yoy': _num('revenueGrowth'),
        'target_mean': _num('targetMeanPrice'),
        'recommendation': info.get('recommendationKey'),
        # Size
        'market_cap': _num('marketCap'),
        'enterprise_value': _num('enterpriseValue'),
        'employees': info.get('fullTimeEmployees'),
        'sector': info.get('sector'),
        'industry': info.get('industry'),
    }


# ──────────────────────────────────────────────────────────────────────────────
# IBD GROUP RANK — IBD_data.txt as-of-date detection + universe group post-pass
# ──────────────────────────────────────────────────────────────────────────────
#
# IBD_data.txt is an export of the MarketSurge screener made on a specific day;
# it supplies the industry mapping used to group tickers.  derive_ibd_asof() is
# a standalone utility that finds the snapshot's trading day by price-matching a
# sample of liquid tickers against the price cache.  apply_group_columns() is
# the universe-level pass that turns per-ticker RS ratings into group stats: the
# primary "Ind Group Rank" is COMPUTED live from RS ratings (1 = best), "Ind
# plus rank history, new-high/low breadth, P/E percentile ranks, Earnings
# Stability, profit-margin-vs-industry.


# Liquid tickers used to detect the trading day IBD_data.txt's prices reflect.
IBD_ASOF_SAMPLE = ["SPY", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
                   "JPM", "XOM", "JNJ", "PG", "KO", "WMT", "A", "AA", "ABBV",
                   "CAT", "DIS", "HD", "MCD", "NKE", "PEP", "T", "VZ", "INTC",
                   "CSCO", "PFE", "MRK", "BA", "GE", "UNH", "V", "MA", "ORCL",
                   "CRM", "ADBE", "NFLX", "TSLA"]


def derive_ibd_asof(ibd_df, cache_dir=None):
    """Return the date (YYYY-MM-DD) the IBD_data.txt snapshot reflects.

    Strategy: for a sample of liquid tickers present in both IBD_data.txt and
    the price cache, find the trading day whose close is within 0.5% of the IBD
    Current Price, then take the most common matching day.  Falls back to the
    most common last-cache-date among the sample if nothing matches.

    Parameters
    ----------
    ibd_df : pd.DataFrame
        IBD_data.txt (must have 'Symbol' and 'Current Price' columns).
    cache_dir : str or Path, optional
        Directory holding the ``{SYMBOL}_1d.parquet`` price files.  Defaults to
        ``<repo>/ticker_cache`` next to this module.
    """
    from collections import Counter
    if cache_dir is None:
        cache_dir = Path(__file__).resolve().parent.parent / "ticker_cache"
    cache_dir = Path(cache_dir)
    ibd_by_sym = {str(r["Symbol"]): r for _, r in ibd_df.iterrows()}
    hits = Counter()
    for sym in IBD_ASOF_SAMPLE:
        r = ibd_by_sym.get(sym)
        if r is None or pd.isna(r.get("Current Price")):
            continue
        px = float(r["Current Price"])
        if px <= 0:
            continue
        fp = cache_dir / f"{sym}_1d.parquet"
        if not fp.exists():
            continue
        try:
            df = pd.read_parquet(fp)
            df.index = pd.to_datetime(df.index)
            close = df["Close"].astype(float)
            diff = (close - px).abs() / px
            cand = diff[diff < 0.005]
            if len(cand):
                hits[str(cand.index[-1].date())] += 1
        except Exception:
            continue
    if hits:
        best = hits.most_common(1)[0][0]
        return best
    # Fallback: the IBD snapshot is older than the cache, so use the most common
    # last-cache-date among the sample tickers as a sane anchor.
    last_dates = []
    for sym in IBD_ASOF_SAMPLE:
        fp = cache_dir / f"{sym}_1d.parquet"
        if not fp.exists():
            continue
        try:
            df = pd.read_parquet(fp, columns=["Close"])
            last_dates.append(str(pd.to_datetime(df.index)[-1].date()))
        except Exception:
            continue
    if last_dates:
        return Counter(last_dates).most_common(1)[0][0]
    return ""


def apply_group_columns(out):
    """Fill the MarketSurge group/percentile columns that need the whole universe:
    Number of Stocks, Ind Mkt Val (bil), Ind Group RS + Rank (+ history), new
    high/low counts per group, P/E percentile ranks, profit-margin-vs-industry,
    EPS 5-yr growth percentile rank, Earnings Stability.

    Requires hidden per-ticker fields (_rs_cur, _rs_1w_ago, _rs_3m_ago, _rs_6m_ago,
    _eps_cv, _eps_g5, _mcap, _pe, _at_margin, _nh, _nl) to already be on `out`.
    """
    out["Number of Stocks"] = None
    out["Ind Mkt Val (bil)"] = None
    out["Ind Grp Rnk Last Week"] = None
    out["Ind Grp Rnk 3 Mo Ago"] = None
    out["Ind Grp Rnk 6 Mo Ago"] = None
    # Ind Group Rank and Ind Group RS are both COMPUTED from live RS (see below).
    out["# New Highs in Group"] = None
    out["% New Highs in Group"] = None
    out["# New Lows in Group"] = None
    out["% New Lows in Group"] = None
    out["P/E Percent Rank"] = None
    out["P/E Ratio Rank in Grp"] = None
    out["Prof Marg Geq Ind Median"] = ""
    out["EPS % Growth 5 Yr Pct Rnk"] = None
    out["Earnings Stability"] = None

    # IMPORTANT: keep missing industries as NaN.  `.astype(str)` turns NaN into the
    # literal string "nan", which would otherwise create a phantom "nan" industry
    # group that receives real group statistics.
    raw_ind = out["Industry Name"]
    ind = raw_ind.astype(str).str.strip().where(raw_ind.notna())
    ind = ind.where(ind != "")

    def _gsum(field, require=0):
        """Group sum; NaN unless the group has > `require` non-null values."""
        cnt = out.groupby(ind)[field].transform("count")
        s = out.groupby(ind)[field].transform("sum")
        return s.where(cnt > require)

    # group size / market value / new-high-new-low tallies (group = industry)
    size = ind.map(ind.value_counts())
    out["Number of Stocks"] = size.where(ind.notna())
    mcap_sum = _gsum("_mcap", require=1)
    out["Ind Mkt Val (bil)"] = (mcap_sum / 1000.0).round(1)
    out["# New Highs in Group"] = _gsum("_nh", require=0).where(ind.notna())
    out["# New Lows in Group"] = _gsum("_nl", require=0).where(ind.notna())
    grp_size = size.where(size > 0, np.nan)
    out["% New Highs in Group"] = (out["# New Highs in Group"] / grp_size * 100).round(1)
    out["% New Lows in Group"] = (out["# New Lows in Group"] / grp_size * 100).round(1)

    # group RS rating + rank, current and historical (1 = best group)
    def _grp_rank(field):
        gmean = out.groupby(ind)[field].transform("mean")
        rank_map = (out.groupby(ind)[field].mean()
                    .rank(ascending=False, method="min"))
        rank_series = ind.map(rank_map)  # industry name -> its rank
        return gmean, rank_series

    grs, grs_r = _grp_rank("_rs_cur")
    # Ind Group RS: 1-99 numeric = mean RS rating of the group's members as of the
    # latest bar (live group strength for screening).
    out["Ind Group RS"] = grs.round(1).where(grs.notna())

    # Ind Group Rank: COMPUTED live rank of the industry (1 = best group) from the
    # mean RS rating of its members as of the latest cached bars - NOT the rank
    # carried in IBD_data.txt.  Every member of an industry shares the same rank;
    # rows without an industry stay blank.
    out["Ind Group Rank"] = grs_r
    for field, col in (("_rs_1w_ago", "Ind Grp Rnk Last Week"),
                       ("_rs_3m_ago", "Ind Grp Rnk 3 Mo Ago"),
                       ("_rs_6m_ago", "Ind Grp Rnk 6 Mo Ago")):
        _, r = _grp_rank(field)
        out[col] = r

    # percentile ranks (1-99) across the universe / within group
    def _pct_rank_99(s):
        valid = s.notna()
        out_ = pd.Series(np.nan, index=s.index)
        if valid.any():
            r = s[valid].rank(pct=True) * 99 + 1
            out_[valid] = r.round(0).clip(1, 99)
        return out_

    out["P/E Percent Rank"] = _pct_rank_99(out["_pe"])
    out["EPS % Growth 5 Yr Pct Rnk"] = _pct_rank_99(out["_eps_g5"])
    # Earnings Stability (IBD convention): 99 = LEAST stable, 1 = most stable.
    # Rank the coefficient of variation of quarterly EPS ascending, so a low CV
    # (stable earnings) gets a low number.
    cv = out["_eps_cv"]
    stable = cv.rank(pct=True) * 99 + 1
    out["Earnings Stability"] = stable.where(cv.notna()).round(0).clip(1, 99)

    # P/E rank within industry, profit margin vs industry median
    for gname, idx in out.groupby(ind).groups.items():
        idx = out.index[idx]
        sub = out.loc[idx]
        pe_sub = sub["_pe"].dropna()
        if len(pe_sub):
            r = pe_sub.rank(pct=True) * 99 + 1
            out.loc[pe_sub.index, "P/E Ratio Rank in Grp"] = r.round(0).clip(1, 99)
        marg_med = sub["_at_margin"].median()
        if pd.notna(marg_med):
            m = sub["_at_margin"]
            ok = m.notna()
            out.loc[ok.index[ok], "Prof Marg Geq Ind Median"] = \
                np.where(m[ok] >= marg_med, "Yes", "No")

    return out
