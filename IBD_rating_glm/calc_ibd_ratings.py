#!/usr/bin/env python3
"""
calc_ibd_ratings.py  (reworked, non-ML production scorer)

Computes IBD-style ratings — RS Rating, A/D Rating, EPS Rating, SMR Rating,
Composite Rating (+ our own industry Group RS) — from ticker_cache data ONLY:

  * price/volume : ticker_cache/{SYMBOL}_1d.parquet
  * fundamentals : ticker_cache/{SYMBOL}_fund.json

The formulas are the transparent, non-ML models reverse-engineered by
reverse_engineer_ratings.py and frozen into output/fitted_params.json:

  * RS   = percentile rank of a monotonic-recency-weighted return blend
  * A/D  = percentile of an OLS accumulation-feature blend -> A+..E grade
  * EPS  = direct OLS fundamental-feature blend (1-99, log-compressed
           growth/level features)
  * SMR  = percentile of an OLS sales/margin/ROE blend (log-compressed
           features) -> A-E grade
  * GrpRS= percentile of industry-mean RS (industry mapping file)
  * Comp = linear combination of the five self-computed components
           (EPS/RS/SMR/A-D + Group RS) — rows missing a group fall back to
           the fit-week group median

No MarketSurge input is needed to *score* — MarketSurge was only used once, to
calibrate the formulas (ground truth) and is stored in fitted_params.json.

Quick use:
    from calc_ibd_ratings import score_universe
    df = score_universe(["AAPL", "MSFT", "NVDA", "AMZN"])   # full ratings table
    row = df.set_index("Symbol").loc["AAPL"]                # one ticker
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from common import (
    AD_LETTERS_ORDERED, CACHE_DIR, FOLDER, SMR_LETTERS_ORDERED,
    extract_fund_features_bulk, extract_price_features_bulk,
    industry_map_series, letter_from_pct, load_spy_close, load_spy_perf,
    pct_from_ref, resolve_cache_file,
)

PARAMS_PATH = FOLDER / "output" / "fitted_params.json"


def load_params(path=None):
    """Load the frozen formula parameters (fitted_params.json)."""
    p = Path(path) if path else PARAMS_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"fitted_params.json not found at {p}. Run "
            "`python reverse_engineer_ratings.py` first to generate it."
        )
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# ──────────────────────────────────────────────────────────────────────────────
# Scoring engines (deterministic, from ticker_cache only)
# ──────────────────────────────────────────────────────────────────────────────
def _sigmoid(score):
    """IBD-style sigmoid used for the RS rating (fixed monotone map)."""
    z = np.asarray(score, dtype=float) - 100.0
    return np.clip(50.0 + 49.0 * (z / (np.abs(z) + 22.0)), 1, 99)


def _score_rs(row, rs_p):
    """RS Rating + sub-ratings from a features row.

    Supports the production modes:
      * sigmoid        — fixed sigmoid of the weighted relative-perf sum
      * dual_sigmoid   — + absolute-trend term (distance from 200-day MA)
                         inside the sigmoid argument (Dual Momentum)
      * relperf/absret — percentile rank against the stored score_ref
    """
    windows = rs_p["windows"]
    mode = rs_p.get("mode", "sigmoid")
    col = "RelPerf_" if mode in ("sigmoid", "dual_sigmoid", "relperf") else "AbsRet_"
    vals = []
    for w in windows:
        v = row.get(f"{col}{w}")
        vals.append(np.nan if (v is None or not np.isfinite(float(v))) else float(v))
    if np.isnan(vals).any():
        rs_rating = np.nan
    else:
        raw = float(np.dot(vals, np.array(rs_p["weights"])) * 100.0)
        if mode in ("sigmoid", "dual_sigmoid"):
            z = raw
            k = float(rs_p.get("dual_k", 0.0))
            if k and mode == "dual_sigmoid":
                v = row.get("Dist_200MA")
                d200 = np.nan if (v is None or not np.isfinite(float(v))) else float(v)
                if np.isnan(d200):
                    d200 = 0.0
                z = raw + k * d200 / 100.0
            rs_rating = float(_sigmoid(z))  # _sigmoid subtracts 100 internally
        else:
            rs_rating = float(pct_from_ref(np.array([raw]), rs_p["score_ref"])[0])

    rs_3m = rs_6m = np.nan
    for sub_key, out in (("3M", "rs_3m"), ("6M", "rs_6m")):
        v = row.get(f"RelPerf_{sub_key}")
        if v is not None and np.isfinite(float(v)):
            pct = float(_sigmoid(float(v) * 100.0))
            if out == "rs_3m":
                rs_3m = pct
            else:
                rs_6m = pct
    return rs_rating, rs_3m, rs_6m


def _score_ad(row, ad_p):
    feats = ad_p["features"]
    vals = []
    for c in feats:
        v = row.get(c)
        vals.append(np.nan if (v is None or not np.isfinite(float(v))) else float(v))
    if np.isnan(vals).any():
        return np.nan, np.nan
    coefs = np.array(ad_p["coefs"])
    raw = float(np.dot(vals, coefs) + ad_p.get("intercept", 0.0))
    pct = float(pct_from_ref(np.array([raw]), ad_p["score_ref"])[0])
    grade = str(letter_from_pct(np.array([pct]), ad_p["letters"], ad_p["cum_top"])[0])
    return pct, grade


def _score_eps(row, eps_p):
    """EPS Rating = direct OLS scale with the same preprocessing used at fit
    time (the percentile transform measurably hurt EPS).  Growth/level features
    are log-compressed; bounded ratios keep their clip.  Order matches fit:
    coerce -> clip -> median-impute raw -> log."""
    feats = eps_p["features"]
    medians = eps_p.get("medians", {})
    clip = eps_p.get("clip", {})
    log_feats = set(eps_p.get("log_features", []))
    vals = []
    for c in feats:
        v = row.get(c)
        vv = np.nan if (v is None or not np.isfinite(float(v))) else float(v)
        lo, hi = clip.get(c, (-np.inf, np.inf))
        if not np.isnan(vv):
            vv = max(lo, min(hi, vv))
        if np.isnan(vv):
            vv = medians.get(c, 0.0)
        if c in log_feats:
            vv = float(np.sign(vv) * np.log1p(np.abs(vv)))
        vals.append(vv)
    raw = float(np.dot(vals, np.array(eps_p["coefs"])) + eps_p["intercept"])
    return float(np.clip(raw, 1, 99))


def _score_smr(row, smr_p):
    """SMR Rating = OLS blend -> percentile -> A-E quintile.

    Preprocessing order matches fit time exactly: coerce -> clip (empty for the
    current params) -> median-impute on the RAW scale -> log-compress.
    """
    feats = smr_p["features"]
    medians = smr_p.get("medians", {})
    clip = smr_p.get("clip", {})
    log_feats = set(smr_p.get("log_features", []))
    vals = []
    for c in feats:
        v = row.get(c)
        vv = np.nan if (v is None or not np.isfinite(float(v))) else float(v)
        lo, hi = clip.get(c, (-np.inf, np.inf))
        if not np.isnan(vv):
            vv = max(lo, min(hi, vv))
        if np.isnan(vv):
            vv = medians.get(c, 0.0)
        if c in log_feats:
            vv = float(np.sign(vv) * np.log1p(np.abs(vv)))
        vals.append(vv)
    raw = float(np.dot(vals, np.array(smr_p["coefs"])) + smr_p["intercept"])
    pct = float(pct_from_ref(np.array([raw]), smr_p["score_ref"])[0])
    grade = str(letter_from_pct(np.array([pct]), smr_p["letters"], smr_p["cum_top"])[0])
    return pct, grade


def _score_comp(eps, rs, smr_pct, ad_pct, group_rs, comp_p):
    """Composite = linear combination of the self-computed components (all on
    a common 1-99 scale, so weights are directly comparable).  Production
    formula is 5 components including our industry Group RS; a ticker missing
    GroupRS (too-small/unmapped industry) falls back to the fit-week median so
    the Composite stays computable.  Backwards-compatible with 4-component
    params (pre-GroupRS fits)."""
    comps = comp_p.get("components", ["EPS_self", "RS_self", "SMR_self", "AD_self"])
    vals = [eps, rs, smr_pct, ad_pct]
    if "GroupRS_self" in comps:
        if group_rs is None or not np.isfinite(float(group_rs)):
            group_rs = comp_p.get("group_median", 50.0)
        vals.append(float(group_rs))
    vals = np.array(vals, dtype=float)
    if np.isnan(vals).any():
        return np.nan
    coefs = np.array(comp_p["coefs"])
    comp_raw = float(np.dot(vals, coefs) + comp_p["intercept"])
    return max(1, min(99, round(comp_raw)))


def _features_frame(symbols, cache_dir=None):
    """Build one feature row per symbol (price + fund features merged)."""
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
    syms = [str(s).strip() for s in symbols if str(s).strip()]
    spy_perf, _, _ = load_spy_perf(cache_dir)
    spy_close = load_spy_close(cache_dir)
    df_price = extract_price_features_bulk(syms, spy_perf, spy_close=spy_close)
    df_fund = extract_fund_features_bulk(syms)
    if df_price.empty:
        return pd.DataFrame()
    merged = df_price.merge(df_fund, left_on="Ticker", right_on="Ticker", how="left")
    return merged


def score_universe(symbols, cache_dir=None, params=None):
    """Score every symbol with the frozen formulas.

    Parameters
    ----------
    symbols : list[str]
        Universe to rank against (RS/EPS/SMR/A-D are percentile ranks, so a
        meaningful universe is required — pass the full screener list, or use
        score_all_cached() for every ticker in the cache).
    cache_dir : str or Path, optional
        Where ticker_cache lives (defaults to repo ticker_cache).
    params : dict, optional
        Pre-loaded fitted_params.json (defaults to loading it).

    Returns
    -------
    pd.DataFrame with columns: Symbol, RS Rating, RS 3M, RS 6M, EPS Rating,
    SMR Score, SMR Rating, A/D Score, A/D Rating, Comp Rating, Group RS,
    % Off 52W High, Latest Price, Hist Days.
    """
    if params is None:
        params = load_params()
    feats = _features_frame(symbols, cache_dir)
    if feats.empty:
        return pd.DataFrame()

    rs_p, ad_p, eps_p, smr_p, comp_p = params["rs"], params["ad"], params["eps"], params["smr"], params["comp"]
    if comp_p is None:
        raise ValueError("fitted_params.json has no composite params — rerun reverse_engineer_ratings.py")

    out_rows = []
    for _, r in feats.iterrows():
        sym = r["Ticker"]
        rs, rs3, rs6 = _score_rs(r, rs_p)
        ad_pct, ad_grade = _score_ad(r, ad_p)
        eps = _score_eps(r, eps_p)
        smr_pct, smr_grade = _score_smr(r, smr_p)
        out_rows.append({
            "Symbol": sym,
            "RS Rating": round(rs, 1) if not np.isnan(rs) else np.nan,
            "RS 3M": round(rs3, 1) if not np.isnan(rs3) else np.nan,
            "RS 6M": round(rs6, 1) if not np.isnan(rs6) else np.nan,
            "EPS Rating": round(eps, 1) if not np.isnan(eps) else np.nan,
            "SMR Score": round(smr_pct, 1) if not np.isnan(smr_pct) else np.nan,
            "SMR Rating": smr_grade if not np.isnan(smr_pct) else np.nan,
            "A/D Score": round(ad_pct, 1) if not np.isnan(ad_pct) else np.nan,
            "A/D Rating": ad_grade if not np.isnan(ad_pct) else np.nan,
            "_rs_raw": rs, "_eps_raw": eps,
            "_smr_raw": smr_pct, "_ad_raw": ad_pct,
            "% Off 52W High": round(float(r.get("PctOff52WHigh", np.nan)), 2),
            "Latest Price": r.get("Latest_Price"),
            "Hist Days": r.get("Hist_Days"),
        })
    out = pd.DataFrame(out_rows)

    # our own industry group RS: percentile of industry-mean RS rating
    # Industry comes from IBD Industry Mapping.txt (authoritative); the fund-json
    # `info.industry` is only a fallback for symbols missing from the map.
    ind = industry_map_series(feats["Ticker"], fallback_series=feats["Industry"])
    grp_cnt = ind.map(ind.value_counts())
    ok_grp = (grp_cnt > 1).values
    out["Group RS"] = np.nan
    if ok_grp.any():
        rs_s = out["_rs_raw"].astype(float)
        m = rs_s.groupby(ind).transform("mean").where(pd.Series(ok_grp, index=ind.index))
        ref = np.sort(m[ok_grp].values)
        out.loc[ok_grp, "Group RS"] = pct_from_ref(m[ok_grp].values, ref)

    # Composite — computed AFTER Group RS so the 5-component production formula
    # (EPS/RS/SMR/A-D + our Group RS) can use it.  Rows without a group fall back
    # to the fit-week group median inside _score_comp.  Uses the RAW component
    # values (the rounded display columns are only for output).
    out["Comp Rating"] = [
        _score_comp(e, r_, s, a, g, comp_p)
        for e, r_, s, a, g in zip(out["_eps_raw"], out["_rs_raw"],
                                  out["_smr_raw"], out["_ad_raw"], out["Group RS"])
    ]
    out = out.drop(columns=["_rs_raw", "_eps_raw", "_smr_raw", "_ad_raw"])
    return out


def score_ticker(ticker, peers=None, cache_dir=None, params=None):
    """Score a single ticker.

    Note: RS/EPS/SMR/A-D are percentile ranks, so `peers` (the universe the
    ticker is ranked against) matters.  Defaults to every symbol present in the
    cache (the natural screener universe).  Returns the row as a dict.
    """
    if params is None:
        params = load_params()
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
    if peers is None:
        peers = sorted(p.name[: -len("_1d.parquet")] for p in cache_dir.glob("*_1d.parquet"))
    if str(ticker).strip() not in {str(p).strip() for p in peers}:
        peers = list(peers) + [ticker]
    df = score_universe(peers, cache_dir=cache_dir, params=params)
    if df.empty:
        return {}
    row = df[df["Symbol"].astype(str).str.strip() == str(ticker).strip()]
    if row.empty:
        return {}
    return row.iloc[0].to_dict()


def score_all_cached(cache_dir=None, params=None):
    """Score every symbol in ticker_cache (the full screener universe)."""
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
    syms = sorted(p.name[: -len("_1d.parquet")] for p in cache_dir.glob("*_1d.parquet"))
    return score_universe(syms, cache_dir=cache_dir, params=params)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] or ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]
    df = score_universe(syms)
    print(df.to_string(index=False))
