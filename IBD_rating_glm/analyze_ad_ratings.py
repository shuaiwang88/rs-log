#!/usr/bin/env python3
"""
analyze_ad_ratings.py  (reworked, non-ML)

Reverse-engineering of the IBD Accumulation / Distribution (A/D) Rating
(A+ .. E, 13 sub-tiers) from ticker_cache price/volume + fundamentals alone.

Method (fully deterministic):
  1. multi-window accumulation/distribution features (up/down volume ratio,
     heavy-volume net intensity, CMF, volume-weighted closing range, ...)
     across 10D/30D/65D/130D windows, plus price-position features
  2. a closed-form OLS blend of those features (a plain linear model)
  3. percentile-rank transform of the blend onto the universe
  4. grade assignment by matching the ground-truth A+..E grade mix
     (frequency mapping — no classifier)

Compares against the production 65D-Chaikin-money-flow formula.  No sklearn.

Usage:
    python analyze_ad_ratings.py
    from analyze_ad_ratings import fit_ad
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (
    AD_LETTERS_ORDERED, AD_SUBTIER_13, CSV_PATH, OUTPUT_DIR,
    apply_letter_map, build_universe, extract_fund_features_bulk,
    extract_price_features_bulk, fit_letter_map, letter_accuracy,
    load_marketsurge, load_spy_perf, lstsq_fit, pct_rank_99, transfer_pct_rank,
)

# Curated, mostly-orthogonal accumulation feature set
AD_FEATURES = [
    "UpDnVol_10D", "UpDnVol_30D", "UpDnVol_65D", "UpDnVol_130D",
    "HeavyNetRatio_65D", "NetHeavyDays_65D", "NetHeavyIntensity_65D",
    "NetHeavyIntensity_130D", "NetHeavyIntensity_30D",
    "VWClsRange_65D", "CMF_30D", "CMF_65D", "CMF_130D",
    "AvgClsRange_65D", "UpDayVolRatio", "DnDayVolRatio",
    "Dist_50MA", "Dist_150MA", "Dist_200MA", "PctOff52WHigh",
    "PriceChg_5D", "InstTop5Pct", "InstAvgChg",
]


def _clip_df(d, cols, lo, hi):
    for c in cols:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").clip(lo, hi)
    return d


def fit_ad(d, holdout=0.2, seed=42, verbose=True):
    """Fit A/D rating models.

    d must contain 'AD_Subtier' + the AD_FEATURES columns (some may be absent;
    they are filtered).  Returns dict with results, params (for production).
    """
    feat_cols = [c for c in AD_FEATURES if c in d.columns]
    dd = d.dropna(subset=["AD_Subtier"]).copy()
    dd = dd[dd["AD_Subtier"].isin(AD_LETTERS_ORDERED)]
    dd = dd.dropna(subset=feat_cols).copy()
    if verbose:
        print(f"[AD] evaluation universe: {len(dd):,} (features: {len(feat_cols)})")

    y_true = dd["AD_Subtier"].values
    X = dd[feat_cols].values.astype(float)

    rng = np.random.default_rng(seed)
    idx = np.arange(len(dd))
    rng.shuffle(idx)
    n_tr = int(len(idx) * (1 - holdout))
    tr, te = idx[:n_tr], idx[n_tr:]

    rows = []

    # 1) production baseline: 65D CMF -> percentile -> letters
    base_scores = dd["AD_baseline"].values.astype(float)
    ok_base = ~np.isnan(base_scores)
    if np.any(ok_base):
        # subset to rows where the baseline score exists, re-split
        bd = dd[ok_base].copy()
        b_scores = base_scores[ok_base]
        b_true = y_true[ok_base]
        b_idx_all = np.arange(len(bd))
        rng2 = np.random.default_rng(seed + 1)
        rng2.shuffle(b_idx_all)
        nb_tr = int(len(b_idx_all) * (1 - holdout))
        b_tr_, b_te_ = b_idx_all[:nb_tr], b_idx_all[nb_tr:]
        pred_b = apply_letter_map(b_scores[b_te_], b_scores[b_tr_], b_true[b_tr_],
                                  AD_LETTERS_ORDERED)
        m = letter_accuracy(b_true[b_te_], pred_b, AD_LETTERS_ORDERED, AD_SUBTIER_13)
        if m:
            m = {"Method": "Current formula (65D CMF) + calibrated letters", **m}
            rows.append(m)

    # 2) OLS blend -> percentile -> letters (train fit, test eval)
    b0, coefs, _ = lstsq_fit(X[tr], np.array([AD_SUBTIER_13[g] for g in y_true[tr]]))
    raw_tr = X[tr] @ coefs
    raw_te = X[te] @ coefs
    pred = apply_letter_map(raw_te, raw_tr, y_true[tr], AD_LETTERS_ORDERED)
    m = letter_accuracy(y_true[te], pred, AD_LETTERS_ORDERED, AD_SUBTIER_13)
    if m:
        m = {"Method": "OLS multi-window accumulation blend + calibrated letters", **m}
        rows.append(m)

    # 3) numeric-scale evaluation of the same blend (13-pt scale)
    y_num_te = np.array([AD_SUBTIER_13[g] for g in y_true[te]], dtype=float)
    pct_te = transfer_pct_rank(raw_tr, raw_te)
    pred_num = 1.0 + (pct_te / 99.0) * 12.0
    from common import score_report
    rows.append(score_report("OLS blend numeric (1-13 scale)", y_num_te, pred_num))

    results_df = pd.DataFrame(rows)

    # final params (fit on full universe for production)
    b0f, coefsf, _ = lstsq_fit(X, np.array([AD_SUBTIER_13[g] for g in y_true]))
    # IMPORTANT: score_ref must be built on the SAME score definition used at
    # scoring time (blend + intercept), otherwise a constant shift compresses
    # the percentile mapping and wrecks letter assignment.
    raw_full = X @ coefsf + b0f
    counts, cum_top = fit_letter_map(raw_full, y_true, AD_LETTERS_ORDERED)
    params = {
        "features": feat_cols,
        "intercept": float(b0f),
        "coefs": [round(float(c), 6) for c in coefsf],
        "score_ref": [round(float(x), 4) for x in np.sort(raw_full)],
        "letters": AD_LETTERS_ORDERED,
        "letter_counts": counts,
        "cum_top": {g: round(float(c), 6) for g, c in cum_top.items()},
        "universe_size": int(len(dd)),
    }
    return {"results": results_df, "params": params, "universe_size": len(dd)}


def main():
    print("=" * 80)
    print("A/D RATING REVERSE-ENGINEERING (non-ML, ticker_cache only)")
    print("=" * 80)

    df_ms = load_marketsurge(CSV_PATH)
    _, _, df_fund_u = build_universe(df_ms)
    print(f"Universe (valid Comp + price + fund): {len(df_fund_u):,}")

    spy_perf, _, _ = load_spy_perf()
    df_price = extract_price_features_bulk(df_fund_u["Symbol"].tolist(), spy_perf)
    df_fund = extract_fund_features_bulk(df_fund_u["Symbol"].tolist())
    merged = df_fund_u.merge(df_price, left_on="Symbol", right_on="Ticker", how="inner")
    merged = merged.merge(df_fund, left_on="Symbol", right_on="Ticker", how="inner",
                          suffixes=("", "_f"))
    print(f"Merged: {len(merged):,}")

    out = fit_ad(merged, verbose=True)

    report = []
    report.append("# A/D Rating Reverse-Engineering (Non-ML, ticker_cache only)\n")
    report.append(f"**Ground truth**: `{CSV_PATH.name}` | **Universe**: "
                  f"`{out['universe_size']:,}` stocks with A+..E ratings\n")
    report.append("> Method: multi-window accumulation features -> closed-form OLS "
                  "blend -> percentile rank -> grade assignment matching the observed "
                  "A+..E grade mix. No classifiers.\n")
    report.append("## Test-set performance (20% holdout)\n")
    report.append(out["results"].to_markdown(index=False))
    report.append("\n## Production OLS feature weights\n")
    params = out["params"]
    wt = pd.DataFrame({"Feature": params["features"], "OLS_Coef": params["coefs"]})
    wt["Abs_Weight_Pct"] = (wt["OLS_Coef"].abs() / wt["OLS_Coef"].abs().sum() * 100).round(1)
    wt = wt.sort_values("Abs_Weight_Pct", ascending=False)
    report.append(wt.to_markdown(index=False))
    report.append("\n## Ground-truth grade mix (used for calibration)\n")
    gc = pd.DataFrame({"Grade": list(params["letter_counts"].keys()),
                       "Count": list(params["letter_counts"].values())})
    gc["Share"] = (gc["Count"] / gc["Count"].sum()).round(3)
    report.append(gc.to_markdown(index=False))
    report.append("")

    OUTPUT_DIR.mkdir(exist_ok=True)
    rp = OUTPUT_DIR / "ad_rating_analysis_report.md"
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report))
    print(f"\n✓ A/D report -> {rp}")


if __name__ == "__main__":
    main()
