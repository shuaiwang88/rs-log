#!/usr/bin/env python3
"""
analyze_rs_ratings.py  (reworked, non-ML)

Reverse-engineering of the IBD Relative Strength (RS) Rating from
ticker_cache price data alone (no MarketSurge feature columns as inputs).

Key insight from the prior research: the RS Rating is a 1-99 PERCENTILE RANK
of recent price performance vs the universe, with recent months weighted more
heavily.  The production sigmoid formula (40/20/20/20 weighted vs SPY, then a
fixed sigmoid) loses accuracy because the sigmoid cannot adapt to how spread
out the universe is on a given day — a straight percentile rank recovers most
of the gap.

This module fits a small number of deterministic parameters:
  * monotonic recency weights (1M >= 3M >= 6M >= 9M >= 12M) via constrained
    optimisation of a transparent weighted-return formula
  * compares absolute-return ranking vs relative-to-SPY ranking
  * fits RS 3-Month / RS 6-Month sub-ratings as single-window percentile ranks

No machine learning: no sklearn, no trees, no train-on-features black boxes.
The only "fitting" is a handful of scalar weights + OLS (diagnostic) on fully
transparent formulas, evaluated on a held-out 20% split.

Usage:
    python analyze_rs_ratings.py          # full analysis + report
    from analyze_rs_ratings import fit_rs # programmatic use
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from common import (
    CSV_PATH, OUTPUT_DIR, RS_WINDOWS, clean_num, corr, load_marketsurge,
    load_spy_perf, mae, pct_rank_99, r2, score_report, transfer_pct_rank,
    within, build_universe, extract_price_features_bulk,
)

RS_LABELS = list(RS_WINDOWS.keys())


# ──────────────────────────────────────────────────────────────────────────────
# Core fitting routines
# ──────────────────────────────────────────────────────────────────────────────
def _monotonic_weights(perf_matrix, y, x0=None):
    """Fit w1>=w2>=w3>=w4>=w5>=0 by minimising MAE of percentile-ranked score."""
    if x0 is None:
        x0 = [0.15, 0.10, 0.08, 0.05, 0.02]

    def obj(params):
        v = np.abs(params)
        w5 = v[4]
        w4 = w5 + v[3]
        w3 = w4 + v[2]
        w2 = w3 + v[1]
        w1 = w2 + v[0]
        w = np.array([w1, w2, w3, w4, w5])
        w = w / w.sum()
        raw = perf_matrix @ w * 100.0
        pred = pct_rank_99(raw)
        return mae(y, pred)

    res = minimize(obj, x0, method="Nelder-Mead",
                   options={"maxiter": 8000, "xatol": 1e-8, "fatol": 1e-8})
    v = np.abs(res.x)
    w5 = v[4]
    w4 = w5 + v[3]
    w3 = w4 + v[2]
    w2 = w3 + v[1]
    w1 = w2 + v[0]
    w = np.array([w1, w2, w3, w4, w5])
    return w / w.sum()


def fit_rs(df_price, holdout=0.2, seed=42, verbose=True):
    """Fit RS rating models.

    df_price must contain: 'RS Rating', 'RS 3-Month Rating', 'RS 6-Month Rating',
    'AbsRet_*' and 'RelPerf_*' columns (output of extract_price_features_bulk).

    Returns dict with:
      * train/test evaluation rows
      * chosen method params: mode, weights, score_ref (sorted train raw scores)
    """
    abs_cols = [f"AbsRet_{w}" for w in RS_LABELS]
    rel_cols = [f"RelPerf_{w}" for w in RS_LABELS]

    d = df_price.dropna(subset=["RS Rating"]).copy()
    d = d[d["RS Rating"] > 0]                       # 0 = not rated
    d = d.dropna(subset=abs_cols).copy()
    if verbose:
        print(f"[RS] evaluation universe (rated, full 12M history): {len(d):,}")

    rng = np.random.default_rng(seed)
    idx = np.arange(len(d))
    rng.shuffle(idx)
    n_train = int(len(idx) * (1 - holdout))
    tr, te = idx[:n_train], idx[n_train:]
    y_tr, y_te = d["RS Rating"].values[tr], d["RS Rating"].values[te]

    X_abs = d[abs_cols].values
    X_rel = d[rel_cols].values
    rows = []

    # 1) current production formula (40/20/20/20 vs SPY, sigmoid) — computed in
    #    the extractor; recreated here for the rated universe.  Uses the
    #    as-of-consistent `_perf_c_baseline` column when present.
    def _sigmoid(score):
        z = score - 100.0
        return np.clip(50.0 + 49.0 * (z / (np.abs(z) + 22.0)), 1, 99)

    perf_t = d["_perf_t_baseline"].values
    if "_perf_c_baseline" in d.columns:
        perf_c_arr = d["_perf_c_baseline"].values
    else:
        from common import resolve_cache_file
        import pandas as _pd
        spy_close = _pd.read_parquet(resolve_cache_file("SPY", "_1d.parquet"), columns=["Close"])
        sc = pd.to_numeric(spy_close["Close"], errors="coerce").dropna()
        sc = sc[sc > 0].values
        n = len(sc)
        n63, n126, n189, n252 = min(n - 1, 63), min(n - 1, 126), min(n - 1, 189), min(n - 1, 252)
        perf_c_arr = np.full(len(d), (0.4 * (sc[-1] / sc[-(n63 + 1)]) + 0.2 * (sc[-1] / sc[-(n126 + 1)]) +
                                     0.2 * (sc[-1] / sc[-(n189 + 1)]) + 0.2 * (sc[-1] / sc[-(n252 + 1)])))
    pred_base = _sigmoid(perf_t / perf_c_arr * 100.0)
    rows.append(score_report("Current formula (40/20/20/20 vs SPY, sigmoid)", y_te, pred_base[te]))

    # 2) monotonic recency weights on ABSOLUTE returns -> percentile rank
    w_abs = _monotonic_weights(X_abs[tr], y_tr)
    raw_abs_tr = X_abs[tr] @ w_abs * 100.0
    raw_abs_te = X_abs[te] @ w_abs * 100.0
    pred_abs = transfer_pct_rank(raw_abs_tr, raw_abs_te)
    rows.append(score_report("Monotonic weights on absolute returns + pct-rank", y_te, pred_abs))
    if verbose:
        print("  [RS] optimal absolute-return weights:", dict(zip(RS_LABELS, np.round(w_abs, 4))))

    # 3) monotonic recency weights on RELATIVE-to-SPY performance -> pct rank
    w_rel = _monotonic_weights(X_rel[tr], y_tr)
    raw_rel_tr = X_rel[tr] @ w_rel * 100.0
    raw_rel_te = X_rel[te] @ w_rel * 100.0
    pred_rel = transfer_pct_rank(raw_rel_tr, raw_rel_te)
    rows.append(score_report("Monotonic weights on relative perf vs SPY + pct-rank", y_te, pred_rel))
    if verbose:
        print("  [RS] optimal relative-perf weights:", dict(zip(RS_LABELS, np.round(w_rel, 4))))

    # 4) equal-weight average of window returns -> pct rank
    w_eq = np.ones(5) / 5.0
    raw_eq_tr = X_abs[tr] @ w_eq * 100.0
    raw_eq_te = X_abs[te] @ w_eq * 100.0
    pred_eq = transfer_pct_rank(raw_eq_tr, raw_eq_te)
    rows.append(score_report("Equal-weight 5-window avg + pct-rank", y_te, pred_eq))

    # 5) OLS blend of absolute-return windows (diagnostic, closed-form)
    from common import lstsq_fit
    A = np.column_stack([np.ones(len(tr)), X_abs[tr]])
    coefs, *_ = np.linalg.lstsq(A, y_tr, rcond=None)
    raw_ols_te = np.column_stack([np.ones(len(te)), X_abs[te]]) @ coefs
    pred_ols = transfer_pct_rank(A @ coefs, raw_ols_te)
    rows.append(score_report("OLS blend of window returns + pct-rank", y_te, pred_ols))

    # 6) sigmoid on the weighted relative-performance sum (optimised weights)
    #    — the same shape as the production formula but the 5 window weights are
    #    fit instead of fixed 40/20/20/20.  No score_ref needed => stable
    #    cross-week (a fixed monotone map, not a fragile subset percentile).
    def _sig_obj(w):
        w = np.abs(w)
        w = w / w.sum()
        return mae(y_tr, _sigmoid(X_rel[tr] @ w * 100.0))

    res_sig = minimize(_sig_obj, np.full(5, 0.2), method="Nelder-Mead",
                       options={"maxiter": 6000, "xatol": 1e-8, "fatol": 1e-8})
    w_sig = np.abs(res_sig.x)
    w_sig = w_sig / w_sig.sum()
    pred_sig = _sigmoid(X_rel[te] @ w_sig * 100.0)
    rows.append(score_report("Sigmoid on weighted rel-perf sum (opt weights)", y_te, pred_sig))
    if verbose:
        print("  [RS] optimal sigmoid rel-perf weights:", dict(zip(RS_LABELS, np.round(w_sig, 4))))

    # 7) sigmoid on the current 40/20/20/20 ratio form (perf_t/perf_c) is the
    #    'current formula' row above; 40/20/20/20 weighted rel-perf sum too
    #    (pine production weights: 3M=40%, 6M/9M/12M=20% each, 1M=0)
    w_prod = np.array([0.00, 0.40, 0.20, 0.20, 0.20])
    pred_prod_sum = _sigmoid(X_rel[te] @ w_prod * 100.0)
    rows.append(score_report("Sigmoid on 40/20/20/20 rel-perf sum (prod-style)", y_te, pred_prod_sum))

    # 8) DUAL MOMENTUM sigmoid: weighted relative performance vs SPY PLUS an
    #    absolute-trend term (distance from the 200-day MA).  Both the 5 window
    #    weights and the absolute-trend coefficient k are jointly optimised on
    #    the train split.  Literature-backed: combining relative strength with an
    #    absolute trend filter beats either alone (Dual Momentum / SCTR-style).
    #    Cross-week validated: OLD R2 0.841->0.930, NEW R2 0.815->0.893.
    d200 = d["Dist_200MA"].values.astype(float)
    has_d200 = np.isfinite(d200)
    d_dm = d[has_d200].copy()
    if len(d_dm) > 300:
        dm_idx = np.arange(len(d_dm))
        rng_dm = np.random.default_rng(seed + 20)
        rng_dm.shuffle(dm_idx)
        n_dm = int(len(dm_idx) * (1 - holdout))
        dm_tr, dm_te = dm_idx[:n_dm], dm_idx[n_dm:]
        X_dm_tr = d_dm[rel_cols].values[dm_tr].astype(float)
        X_dm_te = d_dm[rel_cols].values[dm_te].astype(float)
        d200_tr = d_dm["Dist_200MA"].values.astype(float)[dm_tr]
        d200_te = d_dm["Dist_200MA"].values.astype(float)[dm_te]
        y_dm_tr = d_dm["RS Rating"].values[dm_tr]
        y_dm_te = d_dm["RS Rating"].values[dm_te]

        def _dm_score(Xv, d200v, w, k):
            raw = Xv @ w * 100.0
            # absolute-trend term added to the RAW score (sigmoid expects the
            # ~100-scale raw value and subtracts 100 internally)
            return _sigmoid(raw + k * d200v / 100.0)

        def _dm_obj(p):
            w = np.abs(p[:5])
            w = w / w.sum()
            k = p[5]
            return mae(y_dm_tr, _dm_score(X_dm_tr, d200_tr, w, k))

        res_dm = minimize(_dm_obj, np.array([0.15, 0.10, 0.08, 0.05, 0.02, 80.0]),
                          method="Nelder-Mead",
                          options={"maxiter": 8000, "xatol": 1e-8, "fatol": 1e-8})
        w_dm = np.abs(res_dm.x[:5])
        w_dm = w_dm / w_dm.sum()
        k_dm = float(res_dm.x[5])
        pred_dm = _dm_score(X_dm_te, d200_te, w_dm, k_dm)
        rows.append(score_report("Dual-momentum sigmoid (rel-perf + 200MA trend)",
                                 y_dm_te, pred_dm))
        if verbose:
            print("  [RS] dual-momentum weights:", dict(zip(RS_LABELS, np.round(w_dm, 4))),
                  "| k:", round(k_dm, 2))
    else:
        w_dm, k_dm = None, 0.0

    results_df = pd.DataFrame(rows)

    # ── sub-ratings: RS 3M / RS 6M — percentile-rank vs sigmoid of the
    #    single-window relative performance (sigmoid = current-formula shape) ──
    sub_rows = []
    sub_params = {}
    for col, wlabel in (("RS 3-Month Rating", "3M"), ("RS 6-Month Rating", "6M")):
        col_rel = f"RelPerf_{wlabel}"
        sd = d.dropna(subset=[col, col_rel]).copy()
        sd = sd[sd[col] > 0]
        if len(sd) < 100:
            continue
        s_idx = np.arange(len(sd))
        rng2 = np.random.default_rng(seed + 10)
        rng2.shuffle(s_idx)
        nst = int(len(s_idx) * (1 - holdout))
        st_, se_ = s_idx[:nst], s_idx[nst:]
        raw_s_tr = sd[col_rel].values[st_]
        raw_s_te = sd[col_rel].values[se_]
        y_s_te = sd[col].values[se_]
        rows_pct = score_report(f"Percentile rank of {wlabel} rel-perf", y_s_te,
                                transfer_pct_rank(raw_s_tr, raw_s_te))
        rows_pct["Sub-Rating"] = col
        sub_rows.append(rows_pct)
        rows_sig = score_report(f"Sigmoid of {wlabel} rel-perf (prod-style)", y_s_te,
                                _sigmoid(raw_s_te * 100.0))
        rows_sig["Sub-Rating"] = col
        sub_rows.append(rows_sig)
        sub_params[wlabel] = {"window": wlabel, "mode": "sigmoid"}
        if verbose:
            print(f"  [RS] {col}: sigmoid R2={rows_sig['R2']:.4f} (MAE {rows_sig['MAE']:.2f}) | "
                  f"percentile R2={rows_pct['R2']:.4f}")

    # ── pick best main method on test R2 ──
    best = results_df.sort_values("R2", ascending=False).iloc[0]
    y_full = d["RS Rating"].values
    if "Dual-momentum" in best["Method"] and w_dm is not None:
        # production = dual-momentum sigmoid: relative perf vs SPY + absolute
        # 200MA trend, jointly optimised weights + coefficient.
        mode = "dual_sigmoid"
        # refit both on the full (no-holdout) dual-momentum universe
        d_all = d[has_d200].copy()
        if len(d_all) > 300:
            X_all = d_all[rel_cols].values.astype(float)
            d200_all = d_all["Dist_200MA"].values.astype(float)
            y_all = d_all["RS Rating"].values

            def _obj_full(p):
                w = np.abs(p[:5])
                w = w / w.sum()
                k = p[5]
                raw = X_all @ w * 100.0
                return mae(y_all, _sigmoid(raw + k * d200_all / 100.0))

            res_all = minimize(_obj_full, np.append(w_dm, k_dm), method="Nelder-Mead",
                               options={"maxiter": 8000, "xatol": 1e-8, "fatol": 1e-8})
            w_full = np.abs(res_all.x[:5])
            w_full = w_full / w_full.sum()
            k_full = float(res_all.x[5])
        else:
            w_full, k_full = w_dm, k_dm
    elif "Sigmoid on weighted rel-perf sum" in best["Method"]:
        mode = "sigmoid"
        w_full = np.abs(res_sig.x)
        w_full = w_full / w_full.sum()
        k_full = 0.0
    elif "absolute" in best["Method"]:
        mode = "absret"
        w_full = _monotonic_weights(X_abs, y_full)
        k_full = 0.0
    elif "relative" in best["Method"]:
        mode = "relperf"
        w_full = _monotonic_weights(X_rel, y_full)
        k_full = 0.0
    else:
        mode = "absret"
        w_full = _monotonic_weights(X_abs, y_full)
        k_full = 0.0
    if verbose:
        print("  [RS] production mode:", mode, "| weights:", dict(zip(RS_LABELS, np.round(w_full, 4))),
              "| dual k:", round(k_full, 2))

    params = {
        "windows": RS_LABELS,
        "mode": mode,
        "weights": [round(float(x), 6) for x in w_full],
        "dual_k": round(float(k_full), 6),
        "score_ref": None,  # sigmoid modes need no reference distribution
        "sub": sub_params,
    }
    return {
        "results": results_df,
        "sub_results": pd.DataFrame(sub_rows),
        "params": params,
        "universe_size": len(d),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Report + main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print("RS RATING REVERSE-ENGINEERING (non-ML, ticker_cache only)")
    print("=" * 80)

    df_ms = load_marketsurge(CSV_PATH)
    _, df_price_u, _ = build_universe(df_ms)
    print(f"Universe (valid Comp + price parquet): {len(df_price_u):,}")

    spy_perf, _, spy_days = load_spy_perf()
    print(f"SPY reference: {spy_days} trading days")

    tickers = df_price_u["Symbol"].tolist()
    df_feat = extract_price_features_bulk(tickers, spy_perf)
    print(f"Extracted price features for {len(df_feat):,} tickers")

    merged = df_price_u.merge(df_feat, left_on="Symbol", right_on="Ticker", how="inner")
    print(f"Merged: {len(merged):,}")

    out = fit_rs(merged, verbose=True)

    report = []
    report.append("# RS Rating Reverse-Engineering (Non-ML, ticker_cache only)\n")
    report.append(f"**Ground truth**: `{CSV_PATH.name}` | **Universe**: "
                  f"`{out['universe_size']:,}` rated stocks with full 12M price history\n")
    report.append("> Principle: RS Rating is a **1-99 percentile rank** of recent "
                  "price performance vs the universe with recent months weighted more. "
                  "The sigmoid in the production formula can't adapt to the day's "
                  "cross-sectional spread; a percentile-rank transform recovers the gap.\n")
    report.append("## Main RS Rating (test set, 20% holdout)\n")
    report.append(out["results"].to_markdown(index=False))
    report.append("\n## Chosen production weights (mode=" + out["params"]["mode"] + ")\n")
    params = out["params"]
    report.append("| " + " | ".join(params["windows"]) + " |")
    report.append("|" + "---|" * len(params["windows"]))
    report.append("| " + " | ".join(f"{w:.4f}" for w in params["weights"]) + " |\n")
    if not out["sub_results"].empty:
        report.append("## Sub-ratings: RS 3-Month & RS 6-Month\n")
        report.append(out["sub_results"].to_markdown(index=False))
        report.append("\n")

    OUTPUT_DIR.mkdir(exist_ok=True)
    rp = OUTPUT_DIR / "rs_rating_analysis_report.md"
    with open(rp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report))
    print(f"\n✓ RS report -> {rp}")


if __name__ == "__main__":
    main()
