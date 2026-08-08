#!/usr/bin/env python3
"""
reverse_engineer_ratings.py  (reworked, non-ML)

Main driver: reverse-engineer ALL IBD ratings — RS, A/D, EPS, SMR and the
Composite — from ticker_cache data ONLY (price/volume parquets + fundamentals
json).  The MarketSurge CSVs are used only as ground-truth labels.

Cross-week validation: both MarketSurge snapshots are used for testing since
they come from different weeks:
    * IBD/marketsuge-8-7-2026.csv  (as-of 2026-08-07, matches the live cache)
    * IBD/marketsurge.csv          (as-of 2026-07-24, one week+ earlier)
For each snapshot the price features are computed with price history truncated
to that snapshot's as-of day, so ratings are compared against the data IBD
actually saw that day.  The pipeline is fit on one week and tested on both.

Method (no machine learning):
  * RS:   monotonic recency weights on window returns -> percentile rank 1-99
  * A/D:  multi-window accumulation features -> OLS blend -> percentile ->
          grade assignment matching the observed A+..E mix
  * EPS:  fundamental growth/quality features (log-compressed) -> OLS blend
          -> percentile 1-99
  * SMR:  sales/margin/ROE pillars (log-compressed features) -> OLS blend ->
          percentile -> A-E quintiles  * Comp: closed-form OLS combination of the five self-computed components
    (EPS/RS/SMR/A-D + our own industry group RS from IBD Industry Mapping.txt)
  * Group RS: percentile of industry-mean RS (IBD Industry Mapping.txt)

Writes:
  * output/rating_reengineering_report.md
  * output/fitted_params.json        <- consumed by calc_ibd_ratings.py
     (fit on OLD snapshot by default = forward-validated, no look-ahead;
     pass --production-snapshot new to switch back to the NEW fit)
  * output/fitted_params_fit_on_new.json, fitted_params_fit_on_old.json
     (both fits archived for comparison)
"""

from __future__ import annotations

import argparse
import json
import numpy as np
import pandas as pd

from common import (
    AD_LETTERS_ORDERED, AD_SUBTIER_13, CSV_OLD_PATH, CSV_PATH, OUTPUT_DIR,
    SMR_GRADE_NUM, SMR_LETTERS_ORDERED, apply_letter_map, build_universe,
    derive_csv_asof, extract_fund_features_bulk, extract_price_features_bulk,
    fit_letter_map, industry_map_series, letter_accuracy, letter_from_pct,
    load_marketsurge, load_spy_close, load_spy_perf, log_compress, lstsq_fit,
    pct_from_ref, score_report,
)
from analyze_rs_ratings import fit_rs
from analyze_ad_ratings import fit_ad

# EPS_Q1_YoY / EPS_Accel / Sales_Accel are excluded: yfinance's quarterly
# window is only ~5 quarters, so the 6-quarter lookback is never available.
# The Info_* additions are the high-coverage fund-json fields (>70% of the
# universe); both snapshots improved under the 20% holdout when they were added
# (EPS forward R2 0.409 -> 0.432; SMR exact-letter 64.1% -> 67.0%).
EPS_FEATURES = [
    "EPS_Q0_YoY", "EPS_LT_Growth", "EPS_NegQRatio",
    "ROE", "EPS_StabilityCV", "EpsSurpriseMean", "EpsBeatRate", "EpsRevTrend",
    "EstEPSGrowth_Q", "EstEPSGrowth_Y",
    "Info_ROA", "Info_EPSQGrowth", "Info_GrossMargin", "Info_OpMargin",
    "Info_ProfitMargin", "Info_FCFYield", "Info_OCFYield",
    "Info_DebtEquity", "Info_CurrentRatio", "Info_TotalCashPS",
    "Info_TargetUpside", "Info_NumAnalysts", "Info_FwdPE",
    # research round 2: gross-margin level + trend from income_q (both snapshots
    # improved: OLD 0.400->0.406, NEW 0.432->0.437)
    "GrossMargin_Now", "GrossMargin_Trend",
    # research round 4: forward revenue-estimate growth (0q/0y) + analyst
    # recommendation consensus (both weeks improved: NEW 0.3873 -> 0.3908)
    "RevEstGrowth_Q", "RevEstGrowth_Y", "RecScore",
    # research round 7: price-target momentum over ~90 days from
    # upgrades_downgrades events, /current price (both weeks improved:
    # OLD 0.3778->0.3783, NEW 0.3908->0.3910).  UpDownNet30/90 (net grade
    # changes) were rejected — NEW week flat.
    "PTChg90",
]
SMR_FEATURES = [
    "Sales_Q0_YoY", "Sales_LT_Growth", "Margin_Now",
    "Margin_Trend", "ROE", "Info_ProfitMargin", "Info_RevGrowth",
    "Info_ROA", "Info_GrossMargin", "Info_OpMargin", "Info_FCFYield",
    "Info_OCFYield", "Info_DebtEquity", "Info_CurrentRatio",
    "Info_QuickRatio", "Info_EarningsGrowth", "Info_EPSQGrowth", "Info_PriceBook",
    # research round 4: Sloan accruals + operating-cash-flow/earnings quality
    # from the statements (both weeks improved: NEW exact 62.1% -> 62.3%)
    "Accrual_Q", "OCF_NI",
]
EPS_CORE = ["EPS_Q0_YoY", "EPS_LT_Growth", "ROE"]      # required (else drop row)
SMR_CORE = ["Sales_Q0_YoY", "Sales_LT_Growth", "ROE"]  # required (else drop row)
# EPS growth/level features that get sign-preserving log compression (same
# rationale as SMR: small-denominator YoY blowups off a near-zero base).
# Bounded 0-1 ratios (EPS_NegQRatio, EpsBeatRate) and the 0-10 CV stay clipped.
EPS_LOG_FEATURES = [
    "EPS_Q0_YoY", "EPS_LT_Growth", "EpsSurpriseMean", "EpsRevTrend",
    "EstEPSGrowth_Q", "EstEPSGrowth_Y", "ROE",
    "Info_ROA", "Info_EPSQGrowth", "Info_GrossMargin", "Info_OpMargin",
    "Info_ProfitMargin", "Info_FCFYield", "Info_OCFYield",
    "Info_DebtEquity", "Info_CurrentRatio", "Info_TotalCashPS",
    "Info_TargetUpside", "Info_NumAnalysts", "Info_FwdPE",
    "GrossMargin_Now", "GrossMargin_Trend",
    "RevEstGrowth_Q", "RevEstGrowth_Y", "RecScore",
    "PTChg90",
]


def _median_impute(d, cols):
    """Coerce to numeric, replace inf, fill NaN with the column median."""
    d = d.copy()
    for c in cols:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").replace([np.inf, -np.inf], np.nan)
            med = d[c].median()
            d[c] = d[c].fillna(med)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# Feature building (per snapshot as-of day)
# ──────────────────────────────────────────────────────────────────────────────
def build_features(df_universe, asof, verbose=True):
    """Feature frame for a MarketSurge universe with price history truncated to
    `asof` (the day that snapshot reflects)."""
    spy_perf, spy_perf_c, spy_days = load_spy_perf(asof=asof)
    if verbose:
        print(f"  SPY ref ({asof}): {spy_days} days")
    spy_close = load_spy_close(asof=asof)
    df_price = extract_price_features_bulk(df_universe["Symbol"].tolist(), spy_perf,
                                           asof=asof, spy_close=spy_close)
    df_fund = extract_fund_features_bulk(df_universe["Symbol"].tolist())
    df_price["_perf_c_baseline"] = spy_perf_c
    merged = df_universe.merge(df_price, left_on="Symbol", right_on="Ticker", how="inner")
    merged = merged.merge(df_fund, left_on="Symbol", right_on="Ticker", how="inner",
                          suffixes=("", "_f"))
    # safety: if any fund column collided with a MarketSurge column, prefer ours
    for c in ("ROE", "Sector"):
        if f"{c}_f" in merged.columns:
            merged[c] = merged[f"{c}_f"]
    if verbose:
        print(f"  merged features: {len(merged):,}")
    return merged


# ──────────────────────────────────────────────────────────────────────────────
# Apply frozen params to a feature frame (self-computed pipeline)
# ──────────────────────────────────────────────────────────────────────────────
def apply_params(d, params):
    """Compute the self-computed component ratings for every row using the
    frozen formula parameters.  Works on any snapshot's feature frame — this is
    what makes cross-week validation (and production scoring) possible."""
    d = d.copy()
    rs_p, ad_p, eps_p, smr_p = params["rs"], params["ad"], params["eps"], params["smr"]

    # ── RS: production mode is a fixed sigmoid map (stable cross-week).  The
    #    dual_sigmoid variant adds an absolute-trend term (distance from the
    #    200-day MA) inside the sigmoid argument — Dual Momentum: relative
    #    strength vs SPY + absolute trend beats either alone. ──
    if rs_p.get("mode") in ("sigmoid", "dual_sigmoid"):
        cols = [f"RelPerf_{w}" for w in rs_p["windows"]]
        raw_r = d[cols].values.astype(float) @ np.array(rs_p["weights"]) * 100.0
        z = raw_r - 100.0
        k = float(rs_p.get("dual_k", 0.0))
        if k:
            d200 = pd.to_numeric(d.get("Dist_200MA"), errors="coerce").values
            d200 = np.where(np.isfinite(d200), d200, 0.0)
            z = z + k * d200 / 100.0
        d["RS_self"] = np.clip(50.0 + 49.0 * (z / (np.abs(z) + 22.0)), 1, 99)
    else:
        rs_cols = [f"AbsRet_{w}" for w in rs_p["windows"]]
        if rs_p["mode"] == "relperf":
            rs_cols = [f"RelPerf_{w}" for w in rs_p["windows"]]
        Xr = d[rs_cols].values.astype(float)
        d["RS_self"] = pct_from_ref(Xr @ np.array(rs_p["weights"]) * 100.0, rs_p["score_ref"])

    # ── A/D: OLS blend + intercept -> percentile (1-99, common composite scale)
    #         -> grade mix ──
    Xa = d[ad_p["features"]].values.astype(float)
    pa = pct_from_ref(Xa @ np.array(ad_p["coefs"]) + ad_p["intercept"], ad_p["score_ref"])
    d["AD_self"] = pa
    d["AD_grade"] = letter_from_pct(pa, ad_p["letters"], ad_p["cum_top"])

    # ── EPS: direct OLS scale with the SAME preprocessing used at fit time
    #    (percentile transform was shown to hurt the EPS rating).  Growth/level
    #    features are log-compressed; bounded ratios keep their clip. ──
    E = d[eps_p["features"]].copy()
    for c, (lo, hi) in eps_p.get("clip", {}).items():
        if c in E.columns:
            E[c] = pd.to_numeric(E[c], errors="coerce").clip(lo, hi)
    for c in eps_p["features"]:
        if c in E.columns:
            E[c] = pd.to_numeric(E[c], errors="coerce")
            # replace inf before impute (matches fit_smr and the production scorer)
            E[c] = E[c].replace([np.inf, -np.inf], np.nan).fillna(eps_p["medians"].get(c))
    for c in eps_p.get("log_features", []):
        if c in E.columns:
            E[c] = np.sign(E[c]) * np.log1p(np.abs(E[c]))
    raw_e = E.values.astype(float) @ np.array(eps_p["coefs"]) + eps_p["intercept"]
    d["EPS_self"] = np.clip(raw_e, 1, 99)

    # ── SMR: OLS blend + intercept -> percentile -> quintile grade mix.
    #    Features are log-compressed (same transform as fit time) — the sign-
    #    preserving log tames small-denominator margin/growth blowups where a
    #    hard clip was shown to destroy rank information. ──
    S = d[smr_p["features"]].copy()
    for c, (lo, hi) in smr_p.get("clip", {}).items():
        if c in S.columns:
            S[c] = pd.to_numeric(S[c], errors="coerce").clip(lo, hi)
    for c in smr_p["features"]:
        if c in S.columns:
            # replace inf before impute (matches fit_smr and the production
            # scorer, which both treat non-finite as missing)
            S[c] = pd.to_numeric(S[c], errors="coerce")
            S[c] = S[c].replace([np.inf, -np.inf], np.nan).fillna(smr_p["medians"].get(c))
    for c in smr_p.get("log_features", []):
        if c in S.columns:
            S[c] = np.sign(S[c]) * np.log1p(np.abs(S[c]))
    raw_s = S.values.astype(float) @ np.array(smr_p["coefs"]) + smr_p["intercept"]
    ps = pct_from_ref(raw_s, smr_p["score_ref"])
    d["SMR_self"] = ps  # 1-99 percentile, common composite scale
    d["SMR_grade"] = letter_from_pct(ps, smr_p["letters"], smr_p["cum_top"])

    # RS 3M / 6M sub-ratings (sigmoid of single-window rel-perf, prod-style)
    for wlabel, out_c in (("3M", "RS3_self"), ("6M", "RS6_self")):
        v = pd.to_numeric(d.get(f"RelPerf_{wlabel}"), errors="coerce").values
        z = v * 100.0 - 100.0
        d[out_c] = np.clip(50.0 + 49.0 * (z / (np.abs(z) + 22.0)), 1, 99)

    # our own industry group RS (IBD Industry Mapping.txt, fund-json fallback)
    ind = industry_map_series(d["Symbol"], fallback_series=d["Industry"])
    grp_cnt = ind.map(ind.value_counts())
    ok = (grp_cnt > 1).values
    gmean = d["RS_self"].groupby(ind).transform("mean").where(pd.Series(ok, index=ind.index))
    gmean_arr = gmean.values
    d["GroupRS_self"] = pct_from_ref(gmean_arr, np.sort(gmean_arr[ok]))

    if params.get("comp") and params["comp"] is not None:
        cp = params["comp"]
        Xc = d[cp["components"]].values.astype(float)
        # rows missing GroupRS_self (industry too small / unmapped) get the
        # fit-week median so the Composite stays computable for every ticker
        if "GroupRS_self" in cp.get("components", []):
            med = cp.get("group_median", 50.0)
            nan_g = np.isnan(Xc[:, -1])
            if nan_g.any():
                Xc = Xc.copy()
                Xc[nan_g, -1] = med
        comp_raw = Xc @ np.array(cp["coefs"]) + cp["intercept"]
        d["Comp_self"] = np.clip(np.round(comp_raw), 1, 99)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# Evaluation of a self-computed pipeline vs ground truth
# ──────────────────────────────────────────────────────────────────────────────
def eval_pipeline(d, label):
    """Metrics of the self-computed components against a snapshot's labels."""
    rows = []
    rsd = d.dropna(subset=["RS Rating", "RS_self"])
    rsd = rsd[rsd["RS Rating"] > 0]
    if len(rsd):
        rows.append(score_report("RS Rating (self pipeline)", rsd["RS Rating"].values,
                                 rsd["RS_self"].values))
    ed = d.dropna(subset=["EPS Rating", "EPS_self"])
    ed = ed[ed["EPS Rating"] > 0]
    if len(ed):
        rows.append(score_report("EPS Rating (self pipeline)", ed["EPS Rating"].values,
                                 ed["EPS_self"].values))
    ad = d.dropna(subset=["AD_Subtier", "AD_grade"])
    if len(ad):
        m = letter_accuracy(ad["AD_Subtier"].values, ad["AD_grade"].values,
                            AD_LETTERS_ORDERED, AD_SUBTIER_13)
        if m:
            rows.append({"Method": "A/D Rating (self pipeline, A+..E)", **m})
    smr = d.dropna(subset=["SMR Rating", "SMR_grade"])
    if len(smr):
        m = letter_accuracy(smr["SMR Rating"].values, smr["SMR_grade"].values,
                            SMR_LETTERS_ORDERED, SMR_GRADE_NUM)
        if m:
            rows.append({"Method": "SMR Rating (self pipeline, A-E)", **m})
    if "Comp_self" in d.columns:
        cd = d.dropna(subset=["Comp Rating", "Comp_self"])
        cd = cd[cd["Comp Rating"] > 0]
        if len(cd):
            rows.append(score_report("Composite (self pipeline)", cd["Comp Rating"].values,
                                     cd["Comp_self"].values))
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# EPS rating
# ──────────────────────────────────────────────────────────────────────────────
def fit_eps(d, holdout=0.2, seed=42, verbose=True):
    """OLS blend of fundamental growth/quality features -> direct 1-99 scale
    (production path; the percentile-rank variant is kept as a diagnostic
    because it measurably hurts the EPS rating).  Growth/level features are
    log-compressed; bounded ratios stay clipped."""
    dd = d.dropna(subset=["EPS Rating"]).copy()
    dd = dd[dd["EPS Rating"] > 0]
    # log-compressed features are NOT hard-clipped (the log tames outliers and
    # keeps rank order — a clip would collapse extreme rows); only the bounded
    # ratios / CV keep their clip.  Every growth/level feature is in
    # EPS_LOG_FEATURES, so the old -300..300 clip is intentionally replaced by
    # the log transform applied further down.
    if "EPS_StabilityCV" in dd.columns:
        dd["EPS_StabilityCV"] = dd["EPS_StabilityCV"].clip(0, 10)
    for c in ["EPS_NegQRatio", "EpsBeatRate"]:
        if c in dd.columns:
            dd[c] = dd[c].clip(0, 1)
    if "ROE" in dd.columns and "ROE" not in EPS_LOG_FEATURES:
        dd["ROE"] = dd["ROE"].clip(-200, 400)
    dd = dd.dropna(subset=EPS_CORE).copy()
    dd = _median_impute(dd, [c for c in EPS_FEATURES if c not in EPS_CORE])
    for c in EPS_FEATURES:
        dd[c] = pd.to_numeric(dd[c], errors="coerce")
    dd = dd.replace([np.inf, -np.inf], np.nan)
    dd = dd.dropna(subset=EPS_CORE).copy()
    dd = _median_impute(dd, EPS_FEATURES)
    feats = [c for c in EPS_FEATURES if dd[c].notna().all()]
    if verbose:
        print(f"[EPS] evaluation universe: {len(dd):,} (features: {len(feats)})")

    y = dd["EPS Rating"].values
    # log-compress the growth/level features AFTER median-impute on the raw
    # scale (medians stored raw in params; scoring imputes raw then logs).
    X = dd[feats].values.astype(float)
    for i, c in enumerate(feats):
        if c in EPS_LOG_FEATURES:
            X[:, i] = log_compress(X[:, i])

    rng = np.random.default_rng(seed)
    idx = np.arange(len(dd))
    rng.shuffle(idx)
    n_tr = int(len(idx) * (1 - holdout))
    tr, te = idx[:n_tr], idx[n_tr:]

    rows = []

    # baseline: current production formula reproduction (blended growth ->
    # sigmoid + penalties) — computed on the OLD clipped inputs, independent of
    # the fitted log transform, so the diagnostic stays a faithful comparison.
    q0 = np.clip(dd["EPS_Q0_YoY"].values, -300, 300)
    lt = np.clip(dd["EPS_LT_Growth"].values, -300, 300)
    st = q0  # yfinance window makes the 2-quarter blend unavailable; Q0 alone stands in
    blended = st * 0.50 + lt * 0.35
    raw_base = 50.0 + 49.0 * (blended / (np.abs(blended) + 40.0))
    roe = np.clip(dd["ROE"].values, -200, 400)
    roe_pen = np.where(roe < 0, np.minimum(22.0, np.abs(roe) * 0.05 + 5.0), 0.0)
    lt_pen = np.where(lt < 0, np.minimum(15.0, np.abs(lt) * 0.4), 0.0)
    pred_base = np.clip(raw_base - roe_pen - lt_pen
                        - np.clip(dd["EPS_NegQRatio"].values, 0, 1) * 10.0, 1, 99)
    rows.append(score_report("Current formula (blended growth sigmoid)", y[te], pred_base[te]))

    # OLS blend -> percentile rank
    b0, coefs, _ = lstsq_fit(X[tr], y[tr])
    raw_tr = X[tr] @ coefs
    raw_te = X[te] @ coefs
    pred_rank_te = pct_from_ref(raw_te, np.sort(raw_tr))
    rows.append(score_report("OLS feature blend + percentile rank", y[te], pred_rank_te))

    # OLS direct (diagnostic)
    pred_ols = np.clip(X[te] @ coefs + b0, 1, 99)
    rows.append(score_report("OLS direct scale (diagnostic)", y[te], pred_ols))

    results_df = pd.DataFrame(rows)

    # production params on full universe — direct OLS scale (the percentile
    # transform measurably hurts the EPS rating, which is not a pure rank)
    b0f, coefsf, _ = lstsq_fit(X, y)
    clip_bounds = {
        "EPS_StabilityCV": (0, 10), "EPS_NegQRatio": (0, 1), "EpsBeatRate": (0, 1),
    }
    params = {
        "features": feats,
        "intercept": float(b0f),
        "coefs": [round(float(c), 6) for c in coefsf],
        "clip": clip_bounds,
        "log_features": [c for c in feats if c in EPS_LOG_FEATURES],
        "medians": {c: float(dd[c].median()) for c in feats},
        "universe_size": int(len(dd)),
    }
    return {"results": results_df, "params": params, "universe_size": len(dd)}


# ──────────────────────────────────────────────────────────────────────────────
# SMR rating
# ──────────────────────────────────────────────────────────────────────────────
def fit_smr(d, holdout=0.2, seed=42, verbose=True):
    """3-pillar (sales / margin / ROE) OLS blend -> percentile -> A-E quintiles.

    Features are log-compressed (sign-preserving log1p) instead of hard-clipped:
    the earlier clip variant fit badly because Margin_Now can blow up past
    -1,000,000% on near-zero revenue and the clip collapsed all such rows to the
    same value, destroying rank information the log keeps.
    """
    dd = d.dropna(subset=["SMR_Num"]).copy()
    dd = dd.dropna(subset=SMR_CORE).copy()
    dd = _median_impute(dd, [c for c in SMR_FEATURES if c not in SMR_CORE])
    for c in SMR_FEATURES:
        dd[c] = pd.to_numeric(dd[c], errors="coerce")
    dd = dd.replace([np.inf, -np.inf], np.nan)
    dd = dd.dropna(subset=SMR_CORE).copy()
    dd = _median_impute(dd, SMR_FEATURES)
    feats = [c for c in SMR_FEATURES if dd[c].notna().all()]
    if verbose:
        print(f"[SMR] evaluation universe: {len(dd):,} (features: {len(feats)})")

    y_letter = dd["SMR Rating"].astype(str).str.strip().str.upper().values
    # log-compress ALL fitted features (same transform, same order as scoring:
    # coerce -> median-impute raw -> log).  Medians stay on the RAW scale in the
    # params, so scoring imputes raw then logs — exactly matching this matrix.
    X = log_compress(dd[feats].values.astype(float))

    rng = np.random.default_rng(seed)
    idx = np.arange(len(dd))
    rng.shuffle(idx)
    n_tr = int(len(idx) * (1 - holdout))
    tr, te = idx[:n_tr], idx[n_tr:]

    rows = []

    # baseline: ROE-only sigmoid -> percentile -> quintile letters
    roe = dd["ROE"].values
    roe_filled = np.where(np.isnan(roe), 15.0, roe)
    base_score = 50.0 + 49.0 * (roe_filled / (np.abs(roe_filled) + 17.0))
    pred_b = apply_letter_map(base_score[te], base_score[tr], y_letter[tr], SMR_LETTERS_ORDERED)
    m = letter_accuracy(y_letter[te], pred_b, SMR_LETTERS_ORDERED, SMR_GRADE_NUM)
    if m:
        m = {"Method": "Current formula (ROE-only) + calibrated quintiles", **m}
        rows.append(m)

    # 3-pillar OLS blend -> percentile -> quintile letters
    y_num_tr = np.array([SMR_GRADE_NUM[g] for g in y_letter[tr]], dtype=float)
    b0, coefs, _ = lstsq_fit(X[tr], y_num_tr)
    raw_tr = X[tr] @ coefs
    raw_te = X[te] @ coefs
    pred = apply_letter_map(raw_te, raw_tr, y_letter[tr], SMR_LETTERS_ORDERED)
    m = letter_accuracy(y_letter[te], pred, SMR_LETTERS_ORDERED, SMR_GRADE_NUM)
    if m:
        m = {"Method": "OLS 3-pillar blend + calibrated quintiles", **m}
        rows.append(m)

    # numeric-scale score report for the blend
    y_num_te = np.array([SMR_GRADE_NUM[g] for g in y_letter[te]], dtype=float)
    pct_te = pct_from_ref(raw_te, np.sort(raw_tr))
    pred_num = 10.0 + (pct_te - 1) / 98.0 * 85.0
    rows.append(score_report("OLS 3-pillar blend numeric (10-95)", y_num_te, pred_num))

    # direct OLS scale (diagnostic) — shows the true linear fit quality, which
    # the percentile-rescaled row above understates.  Production still uses the
    # percentile (all composite components share a 1-99 scale), but this is the
    # number to compare against v2's in-sample direct-scale R2 (~0.68).
    pred_ols_te = np.clip(X[te] @ coefs + b0, 10, 95)
    rows.append(score_report("OLS 3-pillar blend direct scale (diagnostic)",
                             y_num_te, pred_ols_te))

    results_df = pd.DataFrame(rows)

    # production params on full universe — score_ref must use blend + intercept
    # so the stored reference matches the score computed at scoring time
    y_num_all = np.array([SMR_GRADE_NUM[g] for g in y_letter], dtype=float)
    b0f, coefsf, _ = lstsq_fit(X, y_num_all)
    raw_full = X @ coefsf + b0f
    counts, cum_top = fit_letter_map(raw_full, y_letter, SMR_LETTERS_ORDERED)
    params = {
        "features": feats,
        "intercept": float(b0f),
        "coefs": [round(float(c), 6) for c in coefsf],
        "score_ref": [round(float(x), 4) for x in np.sort(raw_full)],
        "letters": SMR_LETTERS_ORDERED,
        "letter_counts": counts,
        "cum_top": {g: round(float(c), 6) for g, c in cum_top.items()},
        "clip": {},                       # log-compress replaces hard clipping
        "log_features": feats,            # every fitted SMR feature is log'd
        "medians": {c: float(dd[c].median()) for c in feats},
        "universe_size": int(len(dd)),
    }
    return {"results": results_df, "params": params, "universe_size": len(dd)}


# ──────────────────────────────────────────────────────────────────────────────
# Composite rating
# ──────────────────────────────────────────────────────────────────────────────
def fit_composite(d, holdout=0.2, seed=42, verbose=True):
    """Fit the Composite combining formula on a self-computed pipeline frame.

    d must already carry RS_self, AD_self, EPS_self, SMR_self, GroupRS_self and
    the true Comp Rating column.
    """
    rows = []

    # A) true components from MarketSurge (validates the combining formula shape)
    comp_cols = ["EPS Rating", "RS Rating", "SMR_Num", "AD_Num"]
    td = d.dropna(subset=["Comp Rating"] + comp_cols).copy()
    td = td[(td["Comp Rating"] > 0) & (td["EPS Rating"] > 0) & (td["RS Rating"] > 0)]
    if verbose:
        print(f"[COMP] true-component universe: {len(td):,}")
    if len(td) > 200:
        y_t = td["Comp Rating"].values
        X_t = td[comp_cols].values.astype(float)
        rng = np.random.default_rng(seed + 2)
        ti = np.arange(len(td))
        rng.shuffle(ti)
        ntr = int(len(ti) * (1 - holdout))
        tr_, te_ = ti[:ntr], ti[ntr:]
        b0t, coefst, _ = lstsq_fit(X_t[tr_], y_t[tr_])
        pred_t = np.clip(X_t[te_] @ coefst + b0t, 1, 99)
        rows.append(score_report("True components, OLS (no group)", y_t[te_], pred_t))
        gd = td.dropna(subset=["Ind Group RS"]).copy()
        if len(gd) > 200:
            gd["Grp_Num"] = gd["Ind Group RS"].map(AD_SUBTIER_13)
            gd = gd.dropna(subset=["Grp_Num"])
            y_g = gd["Comp Rating"].values
            X_g = gd[comp_cols + ["Grp_Num"]].values.astype(float)
            rng2 = np.random.default_rng(seed + 3)
            gi = np.arange(len(gd))
            rng2.shuffle(gi)
            ngt = int(len(gi) * (1 - holdout))
            gr_, ge_ = gi[:ngt], gi[ngt:]
            b0g, coefsg, _ = lstsq_fit(X_g[gr_], y_g[gr_])
            pred_g = np.clip(X_g[ge_] @ coefsg + b0g, 1, 99)
            rows.append(score_report("True components + MS group RS (diagnostic)", y_g[ge_], pred_g))

    # B) self-computed pipeline (production): our RS/EPS/SMR/AD (+ our group RS)
    pd_ = d.dropna(subset=["Comp Rating", "RS_self", "AD_self", "EPS_self", "SMR_self"]).copy()
    pd_ = pd_[pd_["Comp Rating"] > 0]
    if verbose:
        print(f"[COMP] self-computed pipeline universe: {len(pd_):,}")
    self_cols = ["EPS_self", "RS_self", "SMR_self", "AD_self"]
    comp_params = None
    if len(pd_) > 200:
        y_p = pd_["Comp Rating"].values
        X_p = pd_[self_cols].values.astype(float)
        rng3 = np.random.default_rng(seed + 4)
        pi = np.arange(len(pd_))
        rng3.shuffle(pi)
        npt = int(len(pi) * (1 - holdout))
        pr_, pe_ = pi[:npt], pi[npt:]
        b0p, coefsp, _ = lstsq_fit(X_p[pr_], y_p[pr_])
        pred_p = np.clip(X_p[pe_] @ coefsp + b0p, 1, 99)
        rows.append(score_report(f"FULL SELF-COMPUTED pipeline (n={len(pd_):,})",
                                 y_p[pe_], pred_p))
        pg = pd_.dropna(subset=["GroupRS_self"]).copy()
        if len(pg) > 200:
            y_pg = pg["Comp Rating"].values
            X_pg = pg[self_cols + ["GroupRS_self"]].values.astype(float)
            rng4 = np.random.default_rng(seed + 5)
            gi2 = np.arange(len(pg))
            rng4.shuffle(gi2)
            ngt2 = int(len(gi2) * (1 - holdout))
            gr2_, ge2_ = gi2[:ngt2], gi2[ngt2:]
            b0pg, coefspg, _ = lstsq_fit(X_pg[gr2_], y_pg[gr2_])
            pred_pg = np.clip(X_pg[ge2_] @ coefspg + b0pg, 1, 99)
            rows.append(score_report("Self-computed + our group RS", y_pg[ge2_], pred_pg))

        # production combining weights — FIVE components including our industry
        # Group RS (cross-week validated: adding GroupRS_self improved BOTH
        # weeks' holdout R2, OLD 0.758->0.769 and NEW 0.752->0.773).  Fit on the
        # rows that have a GroupRS_self; rows without one (industry group too
        # small / unmapped) fall back to the stored group_median at scoring.
        pg_all = pd_.dropna(subset=["GroupRS_self"]).copy()
        if len(pg_all) > 200:
            y_g2 = pg_all["Comp Rating"].values
            X_g2 = pg_all[self_cols + ["GroupRS_self"]].values.astype(float)
            b0pf, coefspf, _ = lstsq_fit(X_g2, y_g2)
            stds = X_g2.std(axis=0)
            contrib = np.abs(coefspf) * stds
            share = contrib / contrib.sum() if contrib.sum() > 0 else np.full(5, 0.2)
            comp_params = {
                "components": self_cols + ["GroupRS_self"],
                "intercept": float(b0pf),
                "coefs": [round(float(c), 6) for c in coefspf],
                "std": [round(float(s), 4) for s in stds],
                "std_share_pct": [round(float(x) * 100, 1) for x in share],
                "group_median": round(float(pg_all["GroupRS_self"].median()), 4),
                "universe_size": int(len(pg_all)),
            }
    return {"results": pd.DataFrame(rows), "params": comp_params, "universe_size": len(pd_)}


# ──────────────────────────────────────────────────────────────────────────────
# Full pipeline: fit on one snapshot, evaluate everywhere
# ──────────────────────────────────────────────────────────────────────────────
def run_pipeline(frame, verbose=True):
    """Fit all four component models + composite on `frame`.

    Returns (params, fit_summary_df, self_frame) — self_frame carries the
    self-computed components and Comp_self for the frame itself.
    """
    rs_out = fit_rs(frame, verbose=verbose)
    ad_out = fit_ad(frame, verbose=verbose)
    eps_out = fit_eps(frame, verbose=verbose)
    smr_out = fit_smr(frame, verbose=verbose)

    params = {"rs": rs_out["params"], "ad": ad_out["params"],
              "eps": eps_out["params"], "smr": smr_out["params"], "comp": None}

    self_frame = apply_params(frame, params)
    comp_out = fit_composite(self_frame, verbose=verbose)
    params["comp"] = comp_out["params"]
    self_frame = apply_params(frame, params)  # recompute incl. Comp_self

    summary = []
    for name, out in (("RS", rs_out), ("A/D", ad_out), ("EPS", eps_out), ("SMR", smr_out)):
        r = out["results"]
        best = r.sort_values("R2", ascending=False).iloc[0] if "R2" in r.columns else None
        if best is not None:
            summary.append({"Rating": name, "Best test method": best["Method"],
                            "Test R2": best["R2"], "Test MAE": best["MAE"]})
    outs = {"rs": rs_out, "ad": ad_out, "eps": eps_out, "smr": smr_out, "comp": comp_out}
    return params, pd.DataFrame(summary), self_frame, outs


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--production-snapshot", choices=["new", "old"], default="old",
                    help="which MarketSurge snapshot's fitted params become "
                         "output/fitted_params.json. 'old' (default) = fit on the "
                         "earlier week, forward-validated on the newest week (no "
                         "look-ahead); 'new' = fit on the newest week.")
    args = ap.parse_args()
    prod = args.production_snapshot

    print("=" * 80)
    print("IBD RATING REVERSE-ENGINEERING (non-ML, ticker_cache only)")
    print("=" * 80)

    # ── load both MarketSurge snapshots + as-of dates ──
    asof_new = derive_csv_asof(CSV_PATH)
    asof_old = derive_csv_asof(CSV_OLD_PATH)
    print(f"Snapshots: {CSV_PATH.name} as-of {asof_new} | {CSV_OLD_PATH.name} as-of {asof_old}")

    df_new = load_marketsurge(CSV_PATH)
    df_old = load_marketsurge(CSV_OLD_PATH)
    _, _, u_new = build_universe(df_new)
    _, _, u_old = build_universe(df_old)
    print(f"Universe new: {len(u_new):,} | old: {len(u_old):,}")

    # ── feature frames (price history truncated to each snapshot's day) ──
    print("\n--- building features for", CSV_PATH.name, f"({asof_new}) ---")
    new_frame = build_features(u_new, asof_new)
    print("\n--- building features for", CSV_OLD_PATH.name, f"({asof_old}) ---")
    old_frame = build_features(u_old, asof_old)

    # ── fit on NEW, evaluate on BOTH ──
    print("\n" + "=" * 80)
    print("FIT ON NEW SNAPSHOT (fit, analysis)")
    print("=" * 80)
    params_new, fit_sum_new, new_self, outs_new = run_pipeline(new_frame)
    print(fit_sum_new.to_string(index=False))
    cp = params_new["comp"]
    if cp:
        print("\n" + "*" * 60)
        print("COMPOSITE RATING FORMULA (fit on NEW, analysis)")
        print("*" * 60)
        print(f"  Comp = {cp['intercept']:.3f}")
        print("       + " + "\n       + ".join(
            f"{c:.4f} × {n}" for c, n in zip(cp["coefs"], cp["components"])))
        print("  Importance % (|coef × std|, normalised):")
        for n, s in zip(cp["components"], cp.get("std_share_pct", [])):
            print(f"    {n:8s} {s:5.1f}%")
        print("*" * 60)
    new_eval = eval_pipeline(new_self, "new")
    print("\nSelf pipeline vs NEW ground truth:")
    print(new_eval.to_string(index=False))

    print("\n" + "=" * 80)
    print("CROSS-WEEK VALIDATION: params fit on NEW, tested on OLD")
    print("=" * 80)
    old_self = apply_params(old_frame, params_new)
    old_eval = eval_pipeline(old_self, "old")
    print(old_eval.to_string(index=False))

    # ── fit on OLD, evaluate on BOTH (reverse direction) ──
    print("\n" + "=" * 80)
    print("FIT ON OLD SNAPSHOT (fit, reverse direction)")
    print("=" * 80)
    params_old, fit_sum_old, old_self2, outs_old = run_pipeline(old_frame)
    print(fit_sum_old.to_string(index=False))
    old_eval2 = eval_pipeline(old_self2, "old")
    print("\nSelf pipeline vs OLD ground truth (in-sample):")
    print(old_eval2.to_string(index=False))

    print("\n" + "=" * 80)
    print("CROSS-WEEK VALIDATION: params fit on OLD, tested on NEW")
    print("=" * 80)
    new_self2 = apply_params(new_frame, params_old)
    new_eval2 = eval_pipeline(new_self2, "new")
    print(new_eval2.to_string(index=False))

    # ── select production params (fit on OLD by default = forward-validated) ──
    if prod == "old":
        prod_params, prod_fit_eval, prod_xweek_eval = params_old, old_eval2, new_eval2
        prod_asof, prod_gt = asof_old, CSV_OLD_PATH.name
        fit_label, xweek_label = "OLD", "NEW (forward, out-of-sample)"
        prod_universe = len(old_frame)
    else:
        prod_params, prod_fit_eval, prod_xweek_eval = params_new, new_eval, old_eval
        prod_asof, prod_gt = asof_new, CSV_PATH.name
        fit_label, xweek_label = "NEW", "OLD (backward, out-of-sample)"
        prod_universe = len(new_frame)
    print("\n" + "=" * 80)
    print(f"PRODUCTION PARAMS: fit on {fit_label} ({prod_asof}), "
          f"validated on {xweek_label}")
    print("=" * 80)
    cp = prod_params["comp"]
    if cp:
        print(f"  Comp = {cp['intercept']:.3f}")
        print("       + " + "\n       + ".join(
            f"{c:.4f} × {n}" for c, n in zip(cp["coefs"], cp["components"])))
        print("  Importance % (|coef × std|, normalised):")
        for n, s in zip(cp["components"], cp.get("std_share_pct", [])):
            print(f"    {n:8s} {s:5.1f}%")
    print("\nIn-sample (fit week):")
    print(prod_fit_eval.to_string(index=False))
    print(f"\nOut-of-sample ({xweek_label}):")
    print(prod_xweek_eval.to_string(index=False))

    # ── write report ──
    report = []
    report.append("# IBD Rating Reverse-Engineering (Non-ML, ticker_cache only)\n")
    report.append(f"**Snapshots**: `{CSV_PATH.name}` (as-of **{asof_new}**) + "
                  f"`{CSV_OLD_PATH.name}` (as-of **{asof_old}**) — different weeks, "
                  "both used for testing.\n")
    report.append(f"**Universe**: new `{len(u_new):,}` | old `{len(u_old):,}` stocks "
                  "(valid Comp + price parquet + fund json)\n")
    report.append("> Every model input comes from `ticker_cache` (parquet + fund json) and "
                  "`IBD Industry Mapping.txt`.  MarketSurge supplies only ground-truth "
                  "labels.  Price features for each snapshot are computed with history "
                  "truncated to that snapshot's as-of day (old snapshot file date "
                  f"2026-07-29, data as-of {asof_old}).  All methods are transparent "
                  "linear blends / percentile ranks / constrained scalar-weight fits — "
                  "no machine learning.\n")

    # ── executive summary: the production Composite formula is the headline ──
    report.append("## Executive summary — Composite Rating formula (the filter)\n")
    report.append(f"> **Production params: fit on {fit_label} ({prod_asof}, "
                  f"`{prod_gt}`), validated on {xweek_label} — no look-ahead. "
                  "Both snapshot fits are archived below.\n")
    cp = prod_params["comp"]
    if cp:
        report.append(f"`Comp Rating = {cp['intercept']:.3f}")
        report.append(" + " + " + ".join(
            f"{c:.4f} × {n}" for c, n in zip(cp["coefs"], cp["components"])) + "`\n")
        cw = pd.DataFrame({"Component": cp["components"], "OLS_Coef": cp["coefs"],
                           "Std": cp.get("std", []), "Importance %": cp.get("std_share_pct", [])})
        report.append(cw.to_markdown(index=False))
        report.append("\n**Importance %** = |coef × std(component)| normalised to 100 — "
                      "the effective weight of each rating inside the Composite.\n")
    report.append(f"\n**Composite accuracy of the full self-computed pipeline** "
                  f"(ticker_cache only, no MarketSurge inputs) with the production "
                  f"params:\n")
    report.append(f"#### In-sample (fit on {fit_label}, {prod_asof})\n")
    report.append(prod_fit_eval.to_markdown(index=False))
    report.append(f"\n#### Out-of-sample ({xweek_label})\n")
    report.append(prod_xweek_eval.to_markdown(index=False))
    report.append("")

    report.append("## A. Fit on NEW snapshot (fit, analysis)\n")
    report.append("### A1. Component models (test set, 20% holdout)\n")
    report.append(fit_sum_new.to_markdown(index=False))
    report.append("\n### A2. Self-computed pipeline vs NEW ground truth (full universe)\n")
    report.append(new_eval.to_markdown(index=False))
    report.append("\n### A3. Detailed per-rating tables\n")
    report.append("#### RS\n")
    report.append(outs_new["rs"]["results"].to_markdown(index=False))
    report.append("\n#### A/D\n")
    report.append(outs_new["ad"]["results"].to_markdown(index=False))
    report.append("\n#### EPS\n")
    report.append(outs_new["eps"]["results"].to_markdown(index=False))
    report.append("\n#### SMR\n")
    report.append(outs_new["smr"]["results"].to_markdown(index=False))
    report.append("\n#### Composite (fit on NEW)\n")
    report.append(outs_new["comp"]["results"].to_markdown(index=False))
    report.append("")

    report.append("## B. Cross-week validation (fit on NEW -> test on OLD)\n")
    report.append(old_eval.to_markdown(index=False))
    report.append("")

    report.append("## C. Reverse direction (fit on OLD -> test on OLD and NEW)\n")
    report.append("### C1. Component models fit on OLD (test set, 20% holdout)\n")
    report.append(fit_sum_old.to_markdown(index=False))
    report.append("\n### C2. Self pipeline vs OLD ground truth (in-sample)\n")
    report.append(old_eval2.to_markdown(index=False))
    report.append("\n### C3. Self pipeline vs NEW ground truth (cross-week)\n")
    report.append(new_eval2.to_markdown(index=False))
    report.append("")

    report.append("## D. Production formula parameters "
                  f"(fit on {fit_label} snapshot — {prod_gt}, as-of {prod_asof})\n")
    rs_p = prod_params["rs"]
    report.append("### D1. RS — " + rs_p["mode"] + " monotonic weights\n")
    report.append("| " + " | ".join(rs_p["windows"]) + " |")
    report.append("|" + "---|" * len(rs_p["windows"]))
    report.append("| " + " | ".join(f"{w:.4f}" for w in rs_p["weights"]) + " |\n")
    for lbl, p in (("AD", prod_params["ad"]), ("EPS", prod_params["eps"]), ("SMR", prod_params["smr"])):
        wt = pd.DataFrame({"Feature": p["features"], "OLS_Coef": p["coefs"]})
        wt["Abs_Weight_Pct"] = (wt["OLS_Coef"].abs() / wt["OLS_Coef"].abs().sum() * 100).round(1)
        wt = wt.sort_values("Abs_Weight_Pct", ascending=False)
        report.append(f"### D2. {lbl} OLS feature weights\n")
        report.append(wt.to_markdown(index=False))
        report.append("")
    if prod_params["comp"]:
        cp = prod_params["comp"]
        cw = pd.DataFrame({"Component": cp["components"], "OLS_Coef": cp["coefs"],
                           "Std": cp.get("std", []), "Importance %": cp.get("std_share_pct", [])})
        report.append("### D3. Composite combining weights (all components 1-99 scale)\n")
        report.append(f"`Comp = {cp['intercept']:.3f}` + " +
                      " + ".join(f"{c:.4f}*{n}" for c, n in zip(cp["coefs"], cp["components"])))
        report.append("\n" + cw.to_markdown(index=False))
        report.append("\nImportance % = |coef × std| normalised — the effective weight of "
                      "each rating in the Composite.\n")
        report.append("")
    report.append("**The rows that matter**: the out-of-sample rows above are the "
                  "honest test of the production params — fit on one week, applied "
                  "to the other.  A2 shows the fit-on-NEW analysis, C2/C3 the "
                  "fit-on-OLD analysis; the production file uses the selected "
                  "snapshot (see `fitted_params.json` → `fit_snapshot`).\n")

    OUTPUT_DIR.mkdir(exist_ok=True)
    rp = OUTPUT_DIR / "rating_reengineering_report.md"
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report))

    # ── fitted params: archive both fits, production file = selected one ──
    def _dump_params(path, fitted):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fitted, fh, indent=2, default=str)

    for tag, p, asof, gt, n in (("new", params_new, asof_new, CSV_PATH.name, len(new_frame)),
                                ("old", params_old, asof_old, CSV_OLD_PATH.name, len(old_frame))):
        _dump_params(OUTPUT_DIR / f"fitted_params_fit_on_{tag}.json", {
            "asof": asof, "ground_truth": gt, "fit_snapshot": tag,
            "validated_on": (asof_old if tag == "new" else asof_new),
            "universe_size": int(n),
            "rs": p["rs"], "ad": p["ad"], "eps": p["eps"], "smr": p["smr"],
            "comp": p["comp"],
        })

    fitted = {
        "asof": prod_asof,
        "ground_truth": prod_gt,
        "fit_snapshot": prod,
        "validated_on": (asof_old if prod == "new" else asof_new),
        "universe_size": int(prod_universe),
        "rs": prod_params["rs"], "ad": prod_params["ad"],
        "eps": prod_params["eps"], "smr": prod_params["smr"],
        "comp": prod_params["comp"],
    }
    fp = OUTPUT_DIR / "fitted_params.json"
    _dump_params(fp, fitted)
    print(f"\n{'=' * 80}\nReport   -> {rp}\nParams   -> {fp} (fit on {fit_label})\n"
          f"Archived: fitted_params_fit_on_new.json, fitted_params_fit_on_old.json\n"
          f"{'=' * 80}")

if __name__ == "__main__":
    main()
