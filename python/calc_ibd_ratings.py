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
# RS Rating is a DUAL-MOMENTUM SIGMOID, not a percentile rank like A/D and SMR - the one
# rating in this module where percentile-of-a-linear-blend was tried and clearly lost.
# History: our own weighted-absolute-return blend (percentile-ranked) reached TEST R2 0.889;
# adding Dist_200MA to that SAME linear+percentile architecture only nudged MAE/corr, not R2
# (see git history). IBD_rating_glm's independent effort got to forward R^2 0.912 with a
# fundamentally different construction: (1) performance RELATIVE TO SPY per window (not
# absolute), (2) a fixed nonlinear sigmoid squashing the weighted relative-perf sum PLUS a
# k*Dist_200MA "dual momentum" term straight to the final 1-99 scale - no live percentile
# rank at all. Re-derived independently on our own walk-forward split (not copied): the
# optimizer converged to weights within rounding of GLM's own fit (further evidence this is
# a real, robust optimum, not overfitting) and reached TEST R^2=0.9208, MAE=5.16 - by far the
# single biggest improvement found in this whole ratings investigation (see
# python/fit_production_ratings.py's "RS - candidate: dual-momentum sigmoid" section).
# RS 3-Month/RS 6-Month sub-ratings are NOT changed - they stay on the single-window
# percentile-rank path (calc_rs_sub_raw_score), a smaller/secondary feature not worth the
# added complexity of a second sigmoid fit.
RS_SIGMOID_WEIGHTS = {"1M": 0.06712591568726939, "3M": 0.5565596218865313, "6M": 0.026800953619395013,
                       "9M": 0.13605021454758013, "12M": 0.21346329425922424}
RS_SIGMOID_K = 91.11316381977677


def _rs_sigmoid(score):
    """GLM's fixed monotone squash: 50 +/- 49 as z=score-100 -> +/-infinity, shape controlled
    by the 22 constant (smaller = steeper transition around z=0). Ported as-is (not refit) -
    it's a fixed transform shape, not a fitted parameter; only the weights/k feeding it were
    refit on our own data above."""
    z = np.asarray(score, dtype=float) - 100.0
    return np.clip(50.0 + 49.0 * (z / (np.abs(z) + 22.0)), 1, 99)


def spy_perf_windows(spy_close_series):
    """SPY's own growth factor (price_now / price_N_days_ago) per RS_RAW_WINDOWS window -
    computed ONCE per run (not per-ticker) and passed into calc_rs_raw_score()."""
    close = spy_close_series.values.astype(float) if hasattr(spy_close_series, "values") else np.asarray(spy_close_series, dtype=float)
    close = close[np.isfinite(close) & (close > 0)]
    n = len(close)
    latest = close[-1]
    return {label: (latest / close[-(days + 1)] if n > days else 1.0)
            for label, days in RS_RAW_WINDOWS.items()}

AD_RAW_FEATURES = [
    "UpDnVol_65D", "HeavyNetRatio_65D", "NetHeavyIntensity_65D", "CMF_65D",
    "UpDnVol_130D", "NetHeavyIntensity_130D", "UpDnVol_30D", "NetHeavyIntensity_30D",
    "Dist_10MA", "Dist_21MA", "Dist_50MA", "Dist_150MA", "Dist_200MA", "PctOff52WHigh",
    "UpDnVol_5D", "HeavyNetRatio_5D", "NetHeavyIntensity_5D", "CMF_5D",
    "UpDnVol_10D", "HeavyNetRatio_10D", "NetHeavyIntensity_10D", "CMF_10D",
    "NetHeavyDays_5D", "NetHeavyDays_10D", "NetHeavyDays_30D", "NetHeavyDays_65D",
    "NetHeavyDays_130D", "NetHeavyDays_250D",
    "AvgClsRange_5D", "AvgClsRange_10D", "AvgClsRange_30D", "AvgClsRange_65D",
    "AvgClsRange_130D", "AvgClsRange_250D",
    "PriceChg_5D", "PriceChg_10D", "PriceChg_30D", "PriceChg_65D", "PriceChg_130D",
    "PriceChg_250D",
    "UpDnVol_250D", "HeavyNetRatio_30D", "HeavyNetRatio_250D", "NetHeavyIntensity_250D",
    "CMF_250D",
]
# NetHeavyDays/AvgClsRange/PriceChg (every window) + the 250D window itself: ported from
# IBD_rating_glm's later A/D update, re-derived on our own ridge pipeline (not copied
# coefficients). TEST exact 36.3%->38.0%, within-1 57.2%->59.8%, beating GLM's own reported
# 37.4%/58.5%. VWClsRange stays excluded at every window (see calc_ad_raw_score docstring).
AD_RAW_COEFS = [
    0.25277333960862597, 1.960263329353397, -0.032502233291333435, 3.3046114933248685,
    0.4411713548539264, -0.027600943380462043, -0.019088787728331114, -0.24555902037253827,
    0.03654986954087032, 0.13399608927395426, 0.14317843288157744, 0.14863312008355178,
    -0.13751174136941488, -0.0009331129623431693, -2.1308936230151334e-08, 0.0278096334064309,
    2.1574663473859874, -1.0542324430216827, 0.0015031422052019375, 0.5564848209580533,
    -1.080901500215005, 3.1265746148425193,
    0.02358767673666194, 0.03694339061040645, -0.003473574879546994, -0.001992956982178694,
    0.013771286333304055, -0.01713031622135956,
    0.014222861904597045, 0.015045938752217367, 0.13817871225802514, 0.10477531081223604,
    0.06720556219715584, -0.3229415770605083,
    -0.04463846378407028, -0.0637123463142196, -0.02915582165832346, 0.005973293405562801,
    0.0018346442233607176, 0.00043824187104783306,
    0.11670719698309567, 1.1877134749068827, -3.3722765562842816, -0.0003992982160668205,
    -4.575455336320584,
]
AD_RAW_INTERCEPT = 4.529657930595425
AD_LETTERS_ORDERED = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"]
AD_CUM_TOP = {
    "A+": 0.0744, "A": 0.1588, "A-": 0.1970, "B+": 0.2511, "B": 0.3415, "B-": 0.3924,
    "C+": 0.4234, "C": 0.4902, "C-": 0.5316, "D+": 0.5695, "D": 0.6657, "D-": 0.7055, "E": 1.0,
}
# Sorted, ~200-point-thinned sample of the TRAIN population's raw A/D score
# (calc_ad_raw_score() output, INCLUDING AD_RAW_INTERCEPT - same convention the
# function actually returns) as of the AD_RAW_COEFS fitting date. AD_CUM_TOP's
# boundaries were fit against percentiles computed against THIS reference, not
# against whatever universe happens to be in a given run. apply_rating_percentiles()
# must rank against this frozen array (via pct_from_ref), not a live self-referential
# rank: ranking A/D raw scores against each day's current universe instead of this
# frozen reference was found to cost ~4x the error (true Comp>=70 tail MAE 2.01 vs
# 1.59, bias -1.33 vs -0.17) - not a staleness problem in practice (validated on data
# 2 weeks past the reference date), and refreshed automatically whenever
# AD_RAW_COEFS/AD_CUM_TOP are re-fit. Must include the intercept: an earlier version
# of this array was built from fit_production_ratings.py's ad_score_ref, which omits
# the intercept (X_tr @ coefs, no +b0) - ranking calc_ad_raw_score()'s WITH-intercept
# output against that WITHOUT-intercept array silently shifted every percentile by
# ~4.5 points and made the live bug's symptom (tail bias +3.7, over-scoring) worse
# than doing nothing. Caught by end-to-end verification against real ground truth
# before this shipped - see the ratings-tail-population-trap memory for why that
# verification step matters.
AD_SCORE_REF = [
    -7.0144, -4.2407, -2.9968, -2.2002, -1.7923, -1.3142, -1.0410, -0.7984,
    -0.4373, -0.1702, 0.0302, 0.1895, 0.3939, 0.5387, 0.7068, 0.8430,
    0.9957, 1.1292, 1.2602, 1.3755, 1.4969, 1.5977, 1.6860, 1.7812,
    1.8749, 1.9930, 2.0589, 2.1668, 2.2507, 2.3057, 2.4164, 2.4908,
    2.5701, 2.6441, 2.6954, 2.7809, 2.8509, 2.9211, 2.9545, 3.0057,
    3.0649, 3.1114, 3.1843, 3.2244, 3.2848, 3.3455, 3.4070, 3.4803,
    3.5495, 3.6019, 3.6548, 3.7053, 3.7634, 3.8034, 3.8552, 3.9226,
    3.9803, 4.0230, 4.0727, 4.1224, 4.1628, 4.1979, 4.2381, 4.2881,
    4.3320, 4.3697, 4.4044, 4.4615, 4.5134, 4.5440, 4.5926, 4.6398,
    4.6873, 4.7672, 4.8182, 4.8639, 4.9018, 4.9618, 4.9949, 5.0337,
    5.0792, 5.1188, 5.1698, 5.2161, 5.2735, 5.3259, 5.3891, 5.4225,
    5.4564, 5.5123, 5.5415, 5.5868, 5.6216, 5.6386, 5.6726, 5.7191,
    5.7797, 5.8070, 5.8335, 5.8709, 5.9167, 5.9517, 5.9708, 6.0155,
    6.0370, 6.0688, 6.1224, 6.1503, 6.1835, 6.2190, 6.2456, 6.2982,
    6.3378, 6.3898, 6.4123, 6.4466, 6.5052, 6.5499, 6.6016, 6.6492,
    6.6944, 6.7231, 6.7541, 6.7830, 6.8240, 6.8574, 6.8876, 6.9180,
    6.9480, 6.9747, 6.9969, 7.0373, 7.0647, 7.1047, 7.1685, 7.2263,
    7.2612, 7.3034, 7.3183, 7.3629, 7.3824, 7.4235, 7.4699, 7.5101,
    7.5527, 7.5856, 7.6249, 7.6866, 7.7312, 7.7970, 7.8474, 7.8861,
    7.9438, 7.9879, 8.0300, 8.0809, 8.1212, 8.1925, 8.2441, 8.2908,
    8.3368, 8.3596, 8.4061, 8.4614, 8.5096, 8.5890, 8.6624, 8.7119,
    8.7469, 8.8042, 8.8822, 8.9391, 9.0255, 9.1052, 9.1528, 9.2176,
    9.2911, 9.3270, 9.3793, 9.4536, 9.5094, 9.5861, 9.6539, 9.7121,
    9.7544, 9.8439, 9.9566, 10.0320, 10.1883, 10.3076, 10.4310, 10.5512,
    10.6854, 10.8072, 11.0123, 11.1628, 11.2471, 11.5140, 11.6725, 11.9533,
    12.2662, 12.6490, 13.1272, 13.9493, 15.5515,
]

EPS_RAW_FEATURES = ["EPS_Q0_YoY", "EPS_LT_Growth", "EPS_NegQRatio", "ROE",
                     "EPS_StabilityCV", "EpsSurpriseMean", "EpsBeatRate", "EpsRevTrend",
                     "EstEPSGrowth_Q", "EstEPSGrowth_Y", "Info_ROA", "Info_EPSQGrowth",
                     "Info_GrossMargin", "Info_OpMargin", "Info_ProfitMargin", "Info_FCFYield",
                     "Info_OCFYield", "Info_DebtEquity", "Info_CurrentRatio", "Info_TotalCashPS",
                     "Info_TargetUpside", "Info_NumAnalysts", "Info_FwdPE",
                     "GrossMargin_Now", "GrossMargin_Trend", "RevEstGrowth_Q", "RevEstGrowth_Y",
                     "RecScore"]
EPS_RAW_COEFS = [1.4430034117371948, 1.3531651754756555, -1.162047800702229, 0.6470592206198515,
                  -0.40345062823051747, 0.14692590697026586, 20.19153893775772, 0.2384789946175763,
                  0.5147992087408103, -0.0120405310172303, 3.4078304369099945, 0.0993645971035024,
                  -1.357656105279868, -0.018840445850038506, 1.2706836896547302, -0.2963560542656617,
                  -0.630722305446072, -0.2323606828006485, -0.10890038634101147, 0.5523784990835927,
                  -0.5471166569857393, 0.5574820819083941, 0.7148620906580537, 2.2990154409643906,
                  0.8995265916171405, 0.17205990558273135, 0.7848279747558563, 4.924851626129315]
EPS_RAW_INTERCEPT = 26.364075527614006
# EpsBeatRate's coefficient (~20-23) dwarfs the others - a company that consistently beats
# analyst estimates is a strong, largely independent EPS-Rating signal, found by comparing
# against IBD_rating_glm's parallel effort and confirmed by our own walk-forward refits:
# R^2 0.333 (4 fundamentals-only) -> 0.416 (+6 analyst-history) -> 0.444 (+13 yfinance
# info-dict fields) -> 0.456 (+gross-margin trend, forward revenue-estimate growth, analyst
# recommendation consensus), each step validated on the same test population, no regressions.
EPS_LOG_FEATURES = {"EPS_Q0_YoY", "EPS_LT_Growth", "ROE", "EpsSurpriseMean", "EpsRevTrend",
                     "EstEPSGrowth_Q", "EstEPSGrowth_Y", "Info_ROA", "Info_EPSQGrowth",
                     "Info_GrossMargin", "Info_OpMargin", "Info_ProfitMargin", "Info_FCFYield",
                     "Info_OCFYield", "Info_DebtEquity", "Info_CurrentRatio", "Info_TotalCashPS",
                     "Info_TargetUpside", "Info_NumAnalysts", "Info_FwdPE",
                     "GrossMargin_Now", "GrossMargin_Trend", "RevEstGrowth_Q", "RevEstGrowth_Y",
                     "RecScore"}
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
    "Info_ROA": 3.7049998, "Info_EPSQGrowth": 17.1, "Info_GrossMargin": 38.07950099999999,
    "Info_OpMargin": 14.2809995, "Info_ProfitMargin": 8.876000000000001,
    "Info_FCFYield": 3.3823657765832724, "Info_OCFYield": 6.509364222585143,
    "Info_DebtEquity": 64.3, "Info_CurrentRatio": 1.707, "Info_TotalCashPS": 4.555,
    "Info_TargetUpside": 13.228671837396222, "Info_NumAnalysts": 9.0, "Info_FwdPE": 14.3565285,
    "GrossMargin_Now": 41.86893712757904, "GrossMargin_Trend": -0.022063950731123327,
    "RevEstGrowth_Q": 7.29, "RevEstGrowth_Y": 7.91, "RecScore": 0.7777777777777778,
}

# Same 13 yfinance info-dict fields as EPS_RAW_FEATURES's Info_* group (overlapping but not
# identical set), ported from IBD_rating_glm - raised its own forward exact-letter accuracy
# 60.2%->62.1%; our own walk-forward refit went 62.6%->66.1% exact, R^2 0.645->0.698, same
# test population, no regressions. Plus Sloan (1996) accruals + OCF/NI cash-conversion ratio
# (GLM's later update) -> 66.1%->66.3% exact, R^2 0.698->0.701. Every SMR_RAW_FEATURES entry
# is log-compressed (no exceptions, unlike EPS) - matches the original 5-feature convention.
SMR_RAW_FEATURES = ["Sales_Q0_YoY", "Sales_LT_Growth", "Margin_Now", "Margin_Trend", "ROE",
                     "Info_ProfitMargin", "Info_RevGrowth", "Info_ROA", "Info_GrossMargin",
                     "Info_OpMargin", "Info_FCFYield", "Info_OCFYield", "Info_DebtEquity",
                     "Info_CurrentRatio", "Info_QuickRatio", "Info_EarningsGrowth",
                     "Info_EPSQGrowth", "Info_PriceBook", "Accrual_Q", "OCF_NI"]
SMR_RAW_COEFS = [0.7022767039998594, 3.730370704506224, 1.7277394295373407, -0.9097149573267861,
                  2.1876706404434647, 2.918130687769683, 0.19986478839446659, 1.3247072452669433,
                  -0.9097611860066741, 0.842554764726071, -0.6734046195914816, -0.07218055273867977,
                  -0.9413433916673032, -2.0530027952712704, 1.616032292378697, -0.7943103163687157,
                  0.6889537809805001, 2.7177953094369154, -1.2680066140483437, -0.5238646141710075]
SMR_RAW_INTERCEPT = 46.73321955393634
SMR_MEDIANS = {
    "Sales_Q0_YoY": 9.505376828065279, "Sales_LT_Growth": 6.82156508892308,
    "Margin_Now": 9.123252858958068, "Margin_Trend": 0.3618103646649491, "ROE": 10.741,
    "Info_ProfitMargin": 9.086, "Info_RevGrowth": 9.6, "Info_ROA": 3.7165,
    "Info_GrossMargin": 38.794996999999995, "Info_OpMargin": 14.642,
    "Info_FCFYield": 3.4572954544341394, "Info_OCFYield": 6.671291852054251,
    "Info_DebtEquity": 65.049, "Info_CurrentRatio": 1.699, "Info_QuickRatio": 1.104,
    "Info_EarningsGrowth": 17.45, "Info_EPSQGrowth": 16.900000000000002,
    "Info_PriceBook": 2.3680876499999997,
    "Accrual_Q": -0.5341737055260303, "OCF_NI": 1.15783621140764,
}
SMR_LETTERS_ORDERED = ["A", "B", "C", "D", "E"]
SMR_CUM_TOP = {"A": 0.3033465275278877, "B": 0.586541921554516, "C": 0.8179201151493343,
                "D": 0.9701331414177762, "E": 1.0}
# Frozen TRAIN reference for SMR percentile ranking - see AD_SCORE_REF's comment
# for why this is needed (Composite's fit assumes SMR's percentile is computed
# against this same frozen reference, never a live population). Unlike AD_SCORE_REF,
# this one never had an intercept-omission bug: SMR's fit used lstsq_fit()'s returned
# `pred` (which folds the intercept in via the ones-column), not a separately
# recomputed X @ coefs - confirmed by rebuilding this array from calc_smr_raw_score()
# directly and finding it matches to within thinning noise. Standalone SMR accuracy is
# also insensitive to this choice either way (5 letter grades is coarse enough that
# live vs frozen ranking barely moves it) - kept for consistency with A/D and because
# Composite's own fit assumes it.
SMR_SCORE_REF = [
    -16.7911, 1.1311, 9.1094, 12.1888, 14.2092, 17.8770, 19.5393, 21.0959,
    22.5612, 23.7109, 24.9649, 25.7364, 26.4436, 27.5993, 28.5152, 29.3937,
    29.9705, 31.1646, 31.6743, 32.7816, 33.5460, 34.5228, 35.4266, 36.0631,
    36.6611, 37.1729, 37.5127, 38.1872, 38.7518, 39.0222, 39.7958, 40.3978,
    41.0981, 41.6388, 42.3310, 42.9122, 43.5706, 44.0003, 44.7389, 45.2065,
    45.7328, 46.3098, 46.8898, 47.2621, 47.6261, 48.1095, 48.6367, 49.0909,
    49.5692, 49.8987, 50.5947, 50.9804, 51.4462, 51.8848, 52.3864, 52.7191,
    53.0380, 53.3776, 53.6028, 54.0070, 54.5312, 54.8582, 55.0376, 55.4696,
    55.8502, 56.1308, 56.5351, 56.7561, 57.0299, 57.4055, 57.7714, 58.1807,
    58.4580, 58.7181, 59.0486, 59.4969, 59.7460, 59.9729, 60.2001, 60.3949,
    60.7220, 61.0099, 61.2925, 61.7329, 62.1362, 62.5203, 62.6908, 63.0622,
    63.2728, 63.6627, 63.8686, 64.2991, 64.6016, 64.8225, 65.0433, 65.2882,
    65.5346, 65.7758, 65.9766, 66.2366, 66.4724, 66.7504, 67.0376, 67.3626,
    67.5738, 67.7995, 68.0299, 68.1520, 68.4117, 68.6387, 68.8314, 69.0737,
    69.3047, 69.5042, 69.7245, 69.9418, 70.1159, 70.3072, 70.5063, 70.7555,
    70.9897, 71.1878, 71.2990, 71.4289, 71.5739, 71.6220, 71.8279, 71.9795,
    72.1101, 72.3061, 72.4935, 72.6559, 72.7565, 72.9414, 73.1106, 73.2644,
    73.3985, 73.5429, 73.6857, 73.9421, 74.1010, 74.3390, 74.4552, 74.5880,
    74.7260, 74.8842, 75.0434, 75.1550, 75.3795, 75.5495, 75.6466, 75.7783,
    75.9409, 76.0293, 76.1356, 76.2565, 76.4727, 76.6408, 76.7714, 76.9314,
    77.1656, 77.2906, 77.4428, 77.6110, 77.7748, 77.9358, 78.0516, 78.2291,
    78.3249, 78.5407, 78.6858, 78.8672, 79.0071, 79.1510, 79.2879, 79.4676,
    79.6869, 79.7671, 79.9004, 80.0424, 80.2422, 80.4758, 80.6348, 80.8148,
    81.1340, 81.3053, 81.4878, 81.7517, 82.0661, 82.2930, 82.6707, 82.8663,
    83.1314, 83.3759, 83.6791, 84.0671, 84.4023, 84.8127, 85.1874, 85.5544,
    86.1408, 86.6256, 87.2285, 87.9833, 88.8111, 89.8148, 90.9240, 92.7947,
    94.7716, 98.0609, 103.8314,
]

# Refit after RS switched from percentile-rank to the dual-momentum sigmoid (see
# RS_SIGMOID_WEIGHTS above) - reusing coefficients fit against the OLD RS distribution
# silently miscalibrated Composite even though RS itself got much more accurate (a sigmoid's
# output distribution isn't uniform like a percentile rank's): caught via full-scale
# production validation, Comp R2 regressed 0.753->0.702 until refitting restored it to 0.770.
# GroupRS (percentile of "Ind Group RS", the industry-mean RS Rating apply_group_columns()
# computes - see apply_rating_percentiles()) added as a 5th component, ported from
# IBD_rating_glm (their own forward Composite R^2: 0.724->0.736 from the same addition):
# our own walk-forward refit went 0.772->0.794, tail (Comp>=80) MAE 6.05->5.65 and
# scored>=80 75.3%->77.1% - both now beat GLM's own reported 5.99/76.8% on this metric.
COMPOSITE_COEFS = {"EPS": 0.35016379288315086, "RS": 0.5163478815816657,
                    "SMR": 0.19376609511257614, "AD": 0.1979711011559106,
                    "GroupRS": 0.1355923447468574}
COMPOSITE_INTERCEPT = -12.956721005509605

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

def calc_rs_raw_score(close_series, spy_perf):
    """Dual-momentum sigmoid RS Rating (already final, 1-99 - despite the name kept for
    call-site continuity, this is NOT a raw score needing apply_rating_percentiles(); RS
    Rating is set directly from this, the same way EPS Rating is).

    score = sum_i(w_i * (stock_perf_i / spy_perf_i)) * 100 + k * Dist_200MA / 100, then
    squashed through _rs_sigmoid(). Returns NaN if the ticker doesn't have the full
    12-month history the blend needs (matches the calibration: partial windows were never
    validated, so they're not guessed at here either).

    Parameters
    ----------
    close_series : the ticker's own Close price series.
    spy_perf : dict from spy_perf_windows(SPY's Close series) - SAME dict reused across
        every ticker in a run (SPY doesn't change per-ticker), computed once by the caller.
    """
    close = close_series.values.astype(float) if hasattr(close_series, "values") else np.asarray(close_series, dtype=float)
    n = len(close)
    if n == 0:
        return np.nan
    latest = close[-1]
    rel = {}
    for label, days in RS_RAW_WINDOWS.items():
        if n <= days:
            return np.nan
        past = close[-(days + 1)]
        if not (past > 0):
            return np.nan
        stock_perf = latest / past
        spy_p = spy_perf.get(label) or 1.0
        rel[label] = stock_perf / spy_p

    # Dist_200MA: absolute-trend term (no benchmark) - n > 249 (checked above via the 12M
    # window) guarantees at least 250 bars, so the 200-day SMA always has a full window.
    ma200 = float(np.mean(close[-200:]))
    dist_200ma = (latest / ma200 - 1.0) * 100.0 if ma200 > 0 else 0.0

    score = sum(RS_SIGMOID_WEIGHTS[label] * rel[label] for label in RS_RAW_WINDOWS) * 100.0
    score += RS_SIGMOID_K * dist_200ma / 100.0
    return float(_rs_sigmoid(score))


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
    # Capped at 20: short windows (5D/10D) routinely have near-zero down-volume,
    # and up_vol/max(1.0, dn_vol) then explodes into the tens of millions (e.g. a
    # genuine ratio of ~200M was seen for a 5-day all-up window) - a share-count
    # artifact of the 1.0 floor, not real signal. 20 sits above every well-behaved
    # window's natural max (250D's observed max was ~20.6) so real strong-accumulation
    # readings aren't clipped, only the divide-by-near-zero blowups are.
    updn_ratio = min(20.0, up_vol / max(1.0, dn_vol))

    heavy_up = up & (vratio > 1.2)
    heavy_dn = dn & (vratio > 1.2)
    h_up_vol = np.sum(vtail[heavy_up])
    h_dn_vol = np.sum(vtail[heavy_dn])
    heavy_net_ratio = h_up_vol / max(1.0, h_up_vol + h_dn_vol)
    net_heavy_intensity = (np.sum(p_rets[heavy_up] * vratio[heavy_up]) -
                            np.sum(np.abs(p_rets[heavy_dn]) * vratio[heavy_dn]))
    # NetHeavyDays: count-based analog of NetHeavyIntensity (# heavy-up days minus
    # # heavy-down days, unweighted by move size) - ported from IBD_rating_glm.
    net_heavy_days = int(np.sum(heavy_up)) - int(np.sum(heavy_dn))

    rng = np.maximum(1e-6, wh - wl)
    cls_rng = (wp - wl) / rng * 100.0
    vw_cls_rng = np.sum(cls_rng * wv) / max(1.0, np.sum(wv))
    # AvgClsRange: unweighted mean closing range (VWClsRange's volume-weighted cousin) -
    # ported from IBD_rating_glm. VWClsRange itself stays display-only (not in
    # AD_RAW_FEATURES): it correlates 0.98 with CMF at the same window (same OHLCV-derived
    # shape), a redundancy already confirmed at 65D and assumed to hold at every window.
    avg_cls_rng = float(np.mean(cls_rng))
    mf_mult = ((wp - wl) - (wh - wp)) / rng
    cmf = np.sum(mf_mult * wv) / max(1.0, np.sum(wv))
    # PriceChg: raw window price change (%) - ported from IBD_rating_glm. Distinct from
    # Dist_XMA (distance from a moving average) and RS's own AbsRet_* windows.
    price_chg = (wp[-1] / wp[0] - 1) * 100.0 if wp[0] > 0 else 0.0

    return {
        "UpDnVol": updn_ratio,
        "HeavyNetRatio": heavy_net_ratio,
        "NetHeavyDays": net_heavy_days,
        "NetHeavyIntensity": net_heavy_intensity,
        "AvgClsRange": avg_cls_rng,
        "VWClsRange": vw_cls_rng,
        "CMF": cmf,
        "PriceChg": price_chg,
    }


def calc_ad_raw_score(df):
    """Ridge-regularized blend of multi-window (5/10/30/65/130/250D) Chaikin-money-flow /
    heavy-volume-day features plus moving-average-distance and % off 52-week high.

    The short 5D/10D windows were added after A/B-testing against GLM's broader
    AD_WINDOWS and confirmed a real out-of-sample win (TEST within-1-grade accuracy
    53.8%->56.7%) — recent accumulation/distribution carries signal the 30D+ windows
    alone smooth away. CMF_130D and VWClsRange (every window) were then dropped
    (VWClsRange_65D correlates 0.98 with CMF_65D - same OHLCV-derived shape, redundant;
    CMF_130D correlates 0.75 with CMF_65D, causing the ridge fit to split a large
    canceling coefficient pair across them - a collinearity artifact, not independent
    signal) and Dist_10MA/21MA added (short-horizon price position). Net: TEST exact
    32.1%->36.3%, within-1 51.5%->57.2%. A later GLM update added NetHeavyDays (count-
    based analog of NetHeavyIntensity), AvgClsRange (unweighted closing-range mean),
    and PriceChg (raw window return) at every window plus the 250D window itself -
    re-derived and refit on our own ridge pipeline (not copied): TEST exact 36.3%->38.0%,
    within-1 57.2%->59.8%, beating GLM's own reported 37.4%/58.5% (see
    python/fit_production_ratings.py).

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
    for w in (5, 10, 30, 65, 130, 250):
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

def calc_eps_rating(fy_eps, fq_eps, roe_val, extra_features=None):
    """Calculate EPS Rating (1-99) from annual/quarterly EPS data plus analyst/info signals.

    Direct-scale OLS blend of log-compressed growth/ROE/analyst/info features
    (percentile-ranking was tested in calibration and measurably hurt EPS — likely
    because the signal is already low-SNR from yfinance's shallow ~5-quarter window,
    and percentile-ranking a noisy raw score just re-orders the noise). This IS the
    final EPS Rating already; no universe pass required.

    Parameters
    ----------
    fy_eps : list of float (length >= 2)
        Annual diluted EPS values, most recent first [FY0, FY-1, FY-2, ...].
    fq_eps : list of float (length >= 5)
        Quarterly diluted EPS values, most recent first [Q0, Q-1, Q-2, ...].
    roe_val : float or None
        Return on Equity from the most recent quarter (as a percent, e.g. 15.0).
    extra_features : dict or None
        Any of EPS_RAW_FEATURES beyond EPS_Q0_YoY/EPS_LT_Growth/EPS_NegQRatio/ROE
        (from extract_eps_analyst_features() + extract_info_features()). Optional
        and median-imputed per-key when missing/absent — a ticker with no earnings-
        history/estimate/info data still gets a rating from the core 4 features.

    Returns
    -------
    int : EPS Rating (1-99), or a data-poor fallback near the population median
    if insufficient EPS history is available.
    """
    extra_features = extra_features or {}
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
        **extra_features,
    }
    vals = []
    for feat in EPS_RAW_FEATURES:
        v = raw_by_feature.get(feat)
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


def calc_smr_raw_score(sales_q0_yoy, sales_lt_growth, margin_now, margin_trend, roe_val,
                        extra_features=None):
    """Raw SMR blend: log-compressed OLS combination of sales growth (short + long
    term), margin (level + trend), ROE, plus 13 yfinance info-dict quality fields.

    RAW number, not a grade — apply_rating_percentiles() percentile-ranks this
    against the eligible universe (the percentile value is also SMR's numeric
    contribution to Composite Rating) and converts to an A-E grade.

    extra_features : dict or None
        Any of SMR_RAW_FEATURES beyond the core 5 (from extract_info_features()).
        Optional and median-imputed per-key when missing/absent.
    """
    raw_by_feature = {
        "Sales_Q0_YoY": sales_q0_yoy, "Sales_LT_Growth": sales_lt_growth,
        "Margin_Now": margin_now, "Margin_Trend": margin_trend, "ROE": roe_val,
        **(extra_features or {}),
    }
    vals = []
    for feat in SMR_RAW_FEATURES:
        v = raw_by_feature.get(feat)
        v = v if (v is not None and np.isfinite(v)) else SMR_MEDIANS[feat]
        vals.append(_log_compress(v))
    return float(SMR_RAW_INTERCEPT + np.dot(np.array(vals), SMR_RAW_COEFS))


# ──────────────────────────────────────────────────────────────────────────────
# UNIVERSE POST-PASS — percentile-ranks raw scores, finalizes RS/A-D/SMR/Composite
# ──────────────────────────────────────────────────────────────────────────────

def pct_from_ref(raw_vals, score_ref):
    """1-99 percentile of raw scores against a FROZEN reference distribution
    (searchsorted/ECDF) - see AD_SCORE_REF's comment for why A/D and SMR must use
    this instead of ranking against whatever universe happens to be in the current
    run. Mirrors fit_production_ratings.py's pct_from_ref exactly (that's what
    AD_CUM_TOP/SMR_CUM_TOP were calibrated against). Vectorized: raw_vals may be a
    scalar or array-like."""
    score_ref = np.sort(np.asarray(score_ref, dtype=float))
    n = len(score_ref)
    scalar_in = np.isscalar(raw_vals) or (isinstance(raw_vals, np.ndarray) and raw_vals.ndim == 0)
    raw_vals = np.atleast_1d(np.asarray(raw_vals, dtype=float))
    out = np.full(len(raw_vals), np.nan)
    ok = ~np.isnan(raw_vals)
    if n > 0 and np.any(ok):
        idx = np.searchsorted(score_ref, raw_vals[ok], side="right")
        out[ok] = np.clip(idx / n * 99.0, 1, 99)
    return float(out[0]) if scalar_in else out


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
    per-ticker scores already on `out` into final RS 3-Month Rating / RS 6-Month
    Rating / A/D Rating / SMR Rating / Composite Rating, percentile-ranked LIVE
    against the CURRENT eligible universe. RS Rating itself is NOT percentile-ranked
    here - calc_rs_raw_score() already returns the final 1-99 dual-momentum-sigmoid
    value (same pattern as EPS Rating), so this just applies the eligibility mask.

    Eligibility = price >= min_price AND (market cap unknown OR market cap >=
    min_mktcap_mil) — IBD's own junk/tiny-cap filter. Ineligible tickers get every
    rating left blank (None/NaN), not scored, matching this pipeline's existing
    "leave blank rather than guess" convention.

    RS 3/6-Month Rating and Group RS rank LIVE against the current eligible universe
    (both production call sites loop over the full universe per run, so this is
    always available) - there's no calibration step downstream of them that assumes
    a particular reference shape, so self-normalizing to today's dispersion is fine.

    A/D Rating and SMR Rating are different: AD_CUM_TOP/SMR_CUM_TOP were fit against
    percentiles computed against a FROZEN train-period reference (AD_SCORE_REF/
    SMR_SCORE_REF), and Composite's own coefficients were fit assuming ad_pct/smr_pct
    are computed the same way (see fit_production_ratings.py's comp_features()).
    Ranking them LIVE instead was tried and measurably worse - on the real
    Comp Rating>=70 tail, A/D's bias went from -0.17 (frozen reference) to -1.20
    (live), because AD_CUM_TOP's cutoffs no longer matched the shape of whatever
    population the live rank was computed against. So A/D/SMR use pct_from_ref()
    against the frozen arrays; only RS3M/RS6M/GroupRS still self-normalize live.

    Requires 'RS Rating' (already final), hidden per-ticker fields _rs3m_raw,
    _rs6m_raw, _ad_raw, _smr_raw, plus 'Current Price', 'Market Cap (mil)', and
    'EPS Rating' to already be on `out`. 'Ind Group RS' (from apply_group_columns(),
    which must run BEFORE this function - see build_daily_screener.py) is optional:
    if present, its percentile feeds Composite's 5th component; if absent (e.g. a
    lighter call site with no industry mapping), Group RS falls back to a neutral
    50 for every row rather than the whole pass failing.
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

    def _pct_rank_ref(raw_col, score_ref):
        s = pd.to_numeric(out.get(raw_col), errors="coerce").where(eligible)
        return pd.Series(pct_from_ref(s.values, score_ref), index=s.index)

    rs_val = pd.to_numeric(out.get("RS Rating"), errors="coerce").where(eligible)
    out["RS Rating"] = rs_val.round(1)
    out["RS 3-Month Rating"] = _pct_rank_99("_rs3m_raw").round(1)
    out["RS 6-Month Rating"] = _pct_rank_99("_rs6m_raw").round(1)

    # Group RS: percentile of "Ind Group RS" (the industry-mean RS Rating,
    # apply_group_columns()'s job) - Composite's 5th component. Neutral 50 fallback
    # when the column is missing entirely or a specific row has no mapped industry,
    # so Composite stays computable everywhere rather than losing coverage over one
    # optional input (matches EPS_MEDIANS/SMR_MEDIANS-style imputation elsewhere).
    group_rs_pct = _pct_rank_99("Ind Group RS").fillna(50.0) if "Ind Group RS" in out.columns \
        else pd.Series(50.0, index=out.index)

    ad_pct = _pct_rank_ref("_ad_raw", AD_SCORE_REF)
    out["A/D Score"] = ad_pct.round(1)
    out["A/D Rating"] = [letter_from_pct(p, AD_LETTERS_ORDERED, AD_CUM_TOP) for p in ad_pct]
    if "_ad_prev_raw" in out.columns:
        ad_prev_pct = _pct_rank_ref("_ad_prev_raw", AD_SCORE_REF)
        out["A/D Rating - Pr Wk"] = [letter_from_pct(p, AD_LETTERS_ORDERED, AD_CUM_TOP) for p in ad_prev_pct]

    smr_pct = _pct_rank_ref("_smr_raw", SMR_SCORE_REF)
    out["SMR Score"] = smr_pct.round(1)
    out["SMR Rating"] = [letter_from_pct(p, SMR_LETTERS_ORDERED, SMR_CUM_TOP) for p in smr_pct]

    eps = pd.to_numeric(out.get("EPS Rating"), errors="coerce").where(eligible)
    out["EPS Rating"] = eps

    # group_rs_pct is never NaN (fillna(50.0) above), so it's deliberately left out of
    # comp_ok - a missing/unmapped industry shouldn't block Comp Rating from computing off
    # the other 4 components, it just contributes a neutral offset.
    comp_ok = eligible & eps.notna() & rs_val.notna() & smr_pct.notna() & ad_pct.notna()
    comp_raw = (COMPOSITE_INTERCEPT + COMPOSITE_COEFS["EPS"] * eps + COMPOSITE_COEFS["RS"] * rs_val +
                COMPOSITE_COEFS["SMR"] * smr_pct + COMPOSITE_COEFS["AD"] * ad_pct +
                COMPOSITE_COEFS["GroupRS"] * group_rs_pct)
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


# info.* fields yfinance stores as a fraction (0.25 = 25%) - converted to percent for
# consistency with every other percent-scale feature in this module.
_INFO_PCT_FIELDS = {"profitMargins", "revenueGrowth", "earningsQuarterlyGrowth",
                    "returnOnAssets", "grossMargins", "operatingMargins", "earningsGrowth"}


def _info_num(info, key):
    v = info.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f * 100.0 if key in _INFO_PCT_FIELDS else f


def extract_info_features(fund):
    """Extract the 17 yfinance info-dict fields used by the EPS/SMR Info_* feature
    group (ported from IBD_rating_glm's independent effort, which found these
    high-coverage fund-json fields improved both EPS and SMR forward accuracy).

    Returns a dict keyed by the same Info_* names calc_eps_rating()/
    calc_smr_raw_score() expect - any value may be None if that info field isn't
    populated for this ticker; both raw-score functions median-impute.
    """
    if not fund or fund.get('error'):
        return {}
    info = fund.get('info')
    if not isinstance(info, dict):
        return {}

    rec = {}
    for key, out_key in (
        ("profitMargins", "Info_ProfitMargin"), ("revenueGrowth", "Info_RevGrowth"),
        ("earningsQuarterlyGrowth", "Info_EPSQGrowth"), ("returnOnAssets", "Info_ROA"),
        ("grossMargins", "Info_GrossMargin"), ("operatingMargins", "Info_OpMargin"),
        ("earningsGrowth", "Info_EarningsGrowth"), ("debtToEquity", "Info_DebtEquity"),
        ("currentRatio", "Info_CurrentRatio"), ("quickRatio", "Info_QuickRatio"),
        ("priceToBook", "Info_PriceBook"), ("totalCashPerShare", "Info_TotalCashPS"),
        ("forwardPE", "Info_FwdPE"), ("numberOfAnalystOpinions", "Info_NumAnalysts"),
    ):
        rec[out_key] = _info_num(info, key)

    mc = _info_num(info, "marketCap")
    fcf = _info_num(info, "freeCashflow")
    ocf = _info_num(info, "operatingCashflow")
    rec["Info_FCFYield"] = (fcf / mc * 100.0) if (mc and mc > 0 and fcf is not None) else None
    rec["Info_OCFYield"] = (ocf / mc * 100.0) if (mc and mc > 0 and ocf is not None) else None
    px = _info_num(info, "currentPrice") or _info_num(info, "regularMarketPrice")
    tgt = _info_num(info, "targetMeanPrice")
    rec["Info_TargetUpside"] = ((tgt / px - 1.0) * 100.0) if (px and px > 0 and tgt is not None) else None
    return rec


def extract_quality_features(fund):
    """GLM "research round 2/4" features (ported): gross-margin level+trend from the income
    statement, forward revenue-estimate growth, analyst recommendation consensus (all 3 for
    EPS_RAW_FEATURES), plus Sloan (1996) accruals and the OCF/NI cash-conversion ratio (for
    SMR_RAW_FEATURES) - both weeks improved in GLM's own ablation for each.

    Returns a dict with keys GrossMargin_Now, GrossMargin_Trend, RevEstGrowth_Q, RevEstGrowth_Y,
    RecScore, Accrual_Q, OCF_NI - any may be None if the underlying fund.json table is missing.
    """
    if not fund or fund.get('error'):
        return {}
    rec = {}

    rev_q = _series_from_label(fund.get('income_q'), ('Total Revenue',))
    gp_q = _series_from_label(fund.get('income_q'), ('Gross Profit',))
    gm_now = gm_trend = None
    if gp_q and rev_q:
        gdates, gvals = gp_q
        rmap = dict(zip(*rev_q))
        # _series_from_label preserves None at calendar-gap positions (see its docstring) -
        # both the gross-profit value and the matched revenue value must be real numbers.
        gms = [gvals[i] / rmap[gdates[i]] * 100.0 for i in range(len(gdates))
               if gvals[i] is not None and rmap.get(gdates[i]) is not None
               and abs(rmap[gdates[i]]) > 1e-6 and abs(gvals[i]) > 1e-6]
        if gms:
            gm_now = gms[0]
            if len(gms) >= 3:
                gm_trend = float(np.mean(gms[:2]) - np.mean(gms[-2:]))
    rec['GrossMargin_Now'] = gm_now
    rec['GrossMargin_Trend'] = gm_trend

    re = fund.get('revenue_estimate')
    for k, out_key in (('0q', 'RevEstGrowth_Q'), ('0y', 'RevEstGrowth_Y')):
        gv = None
        if isinstance(re, dict) and isinstance(re.get(k), dict):
            g = re[k].get('growth')
            if g is not None:
                try:
                    gv = float(g) * 100.0
                except (TypeError, ValueError):
                    pass
        rec[out_key] = gv

    rc = fund.get('recommendations')
    rec_score = None
    if isinstance(rc, dict):
        for k in sorted(rc.keys()):
            if k.startswith('_'):
                continue
            v = rc[k]
            if isinstance(v, dict) and v.get('strongBuy') is not None:
                sb, b = float(v['strongBuy']), float(v.get('buy', 0) or 0)
                h, s = float(v.get('hold', 0) or 0), float(v.get('sell', 0) or 0)
                ss = float(v.get('strongSell', 0) or 0)
                tot = sb + b + h + s + ss
                if tot > 0:
                    rec_score = (2 * sb + b - s - 2 * ss) / tot
                break
    rec['RecScore'] = rec_score

    def _latest_q(section, labels):
        blk = fund.get(section)
        if not isinstance(blk, dict):
            return {}
        for lbl in labels:
            col = blk.get(lbl)
            if isinstance(col, dict) and col:
                return {d: v for d, v in col.items() if v is not None}
        return {}

    ta_q = _latest_q('balance_q', ('Total Assets',))
    ni_q2 = _latest_q('income_q', ('Net Income',))
    ocf_q = _latest_q('cashflow_q', ('Operating Cash Flow',))
    accrual_q = ocf_ni = None
    common_dates = sorted(set(ta_q) & set(ni_q2) & set(ocf_q), reverse=True)
    if common_dates:
        d0 = common_dates[0]
        ta0, ni0, ocf0 = ta_q[d0], ni_q2[d0], ocf_q[d0]
        if abs(ta0 or 0) > 1e-6 and ni0 is not None and ocf0 is not None:
            accrual_q = (float(ni0) - float(ocf0)) / float(ta0) * 100.0
            ocf_ni = float(ocf0) / float(ni0) if abs(ni0) > 1e-6 else None
    rec['Accrual_Q'] = accrual_q
    rec['OCF_NI'] = ocf_ni

    return rec


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
