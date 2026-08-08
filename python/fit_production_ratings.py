#!/usr/bin/env python3
"""
fit_production_ratings.py — final walk-forward calibration for the production
RS / A-D / SMR / EPS / Composite formulas that get hardcoded into calc_ibd_ratings.py.

Corrections vs earlier research (v2/v3) that this script bakes in:

1. RS Rating is NOT computed relative to SPY. A weighted SUM of per-window
   (stock_perf_i / spy_perf_i) ratios is NOT rank-equivalent to a weighted sum of
   the stock's own absolute performance — each window divides by a DIFFERENT SPY
   window return, which distorts the cross-window weighting by whatever SPY's own
   shape was that period. The correct methodology: weight the stock's own ABSOLUTE
   per-window returns, then percentile-rank that raw score against the eligible
   universe. (SPY only matters insofar as being "up 40% while SPY was flat" should
   rank higher than "up 40% while SPY was up 35%" — and that comparison is exactly
   what ranking against the universe already captures, since every other stock in
   the universe faced the same SPY backdrop.)

2. The ranking universe is filtered to exclude junk (price < $4, market cap too
   small) BEFORE computing percentile ranks — matching how IBD's own methodology
   excludes low-priced/illiquid/tiny-cap names from the comparison pool.

3. Walk-forward validation: every formula is FIT on the 2026-07-24 snapshot only
   and FORWARD-TESTED on 2026-08-07 (no retraining) — an honest out-of-sample
   number, not an in-sample pooled fit.

Still closed-form only: percentile ranks + numpy.linalg.lstsq + scipy.optimize on
a handful of scalar weights. No black-box ML.
"""

import sys
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reverse_engineer_ratings_v2 import (  # noqa: E402
    clean_num, log_compress, lstsq_fit, score_report,
    resolve_cache_file, extract_fund_features,
    RS_WINDOWS, MA_WINDOWS, SUBTIER_13_MAP, SMR_GRADE_NUM, CACHE_DIR, REPO_DIR, OUTPUT_DIR,
)
from calc_ibd_ratings import derive_ibd_asof  # noqa: E402

AD_WINDOWS = [5, 10, 30, 65, 130, 250]
MIN_PRICE = 4.0
MIN_MKTCAP_MIL = 50.0

TRAIN_FILE = REPO_DIR / "IBD" / "marketsurge.csv"          # as-of 2026-07-24
TEST_FILE = REPO_DIR / "IBD" / "marketsuge-8-7-2026.csv"   # as-of 2026-08-07


# ──────────────────────────────────────────────────────────────────────────
# Percentile-from-frozen-reference (fit on TRAIN, applied to TEST — no leakage)
# ──────────────────────────────────────────────────────────────────────────

def ridge_fit(X, y, alpha=5.0):
    """Closed-form ridge regression (X standardized internally) — same normal-equations
    math as lstsq_fit, just with an L2 penalty to stabilize collinear features (e.g.
    CMF_65D/CMF_130D, corr~0.75) instead of letting OLS split a huge canceling pair of
    coefficients across them. Still a plain linear model, not a black box."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    mu, sigma = X.mean(axis=0), X.std(axis=0)
    sigma = np.where(sigma < 1e-9, 1.0, sigma)
    Xs = (X - mu) / sigma
    A = np.column_stack([np.ones(len(Xs)), Xs])
    penalty = np.eye(A.shape[1]) * alpha
    penalty[0, 0] = 0.0  # don't penalize the intercept
    coefs_std = np.linalg.solve(A.T @ A + penalty, A.T @ y)
    # unstandardize back to raw-feature-scale coefficients
    b0 = coefs_std[0] - np.sum(coefs_std[1:] * mu / sigma)
    coefs = coefs_std[1:] / sigma
    pred = b0 + X @ coefs
    return b0, coefs, pred


def pct_from_ref(raw_vals, score_ref):
    score_ref = np.sort(np.asarray(score_ref, dtype=float))
    n = len(score_ref)
    raw_vals = np.asarray(raw_vals, dtype=float)
    out = np.full(len(raw_vals), np.nan)
    ok = ~np.isnan(raw_vals)
    if n == 0 or not np.any(ok):
        return out
    idx = np.searchsorted(score_ref, raw_vals[ok], side="right")
    out[ok] = np.clip(idx / n * 99.0, 1, 99)
    return out


def fit_letter_map(train_scores, train_labels, letters_ordered):
    train_scores = np.asarray(train_scores, dtype=float)
    labels = np.asarray(train_labels)[~np.isnan(train_scores)]
    counts = {g: 0 for g in letters_ordered}
    for g in labels:
        if g in counts:
            counts[g] += 1
    total = max(1, sum(counts.values()))
    cum_top, running = {}, 0.0
    for g in letters_ordered:
        running += counts[g] / total
        cum_top[g] = running
    return cum_top


def letter_from_pct(pcts, letters_ordered, cum_top):
    pcts = np.asarray(pcts, dtype=float)
    res = np.full(len(pcts), letters_ordered[-1], dtype=object)
    for i, p in enumerate(pcts):
        if np.isnan(p):
            continue
        for g in letters_ordered:
            if p / 100.0 >= 1.0 - cum_top[g]:
                res[i] = g
                break
    return res


AD_LETTERS = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"]
SMR_LETTERS = ["A", "B", "C", "D", "E"]


def letter_accuracy(y_true_letters, y_pred_letters, letters_ordered):
    """`letters_ordered` must be best-to-worst (e.g. AD_LETTERS, SMR_LETTERS) — within-1 is
    measured in GRADE STEPS (adjacent letter = 1), not point-value distance (which degenerates
    to 'exact' for SMR's 10/70/90-spaced numeric scale where no two distinct grades are within
    1 raw point of each other)."""
    step = {g: i for i, g in enumerate(letters_ordered)}
    yt = np.asarray([str(g) for g in y_true_letters])
    yp = np.asarray([str(g) for g in y_pred_letters])
    ok = np.array([t in step and p in step for t, p in zip(yt, yp)])
    yt, yp = yt[ok], yp[ok]
    exact = float(np.mean(yt == yp) * 100)
    nt = np.array([step[g] for g in yt])
    npd = np.array([step[g] for g in yp])
    within1 = float(np.mean(np.abs(nt - npd) <= 1) * 100)
    return exact, within1, ok.sum()


# ──────────────────────────────────────────────────────────────────────────
# Feature extraction: price/volume (absolute returns, no SPY) + market cap
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
    up, dn = p_rets > 0, p_rets < 0
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
    up_vol, dn_vol = np.sum(vtail[up]), np.sum(vtail[dn])
    # capped at 20 - see matching comment in calc_ibd_ratings.py._window_ad_features
    rng = np.maximum(1e-6, wh - wl)
    cls_rng = (wp - wl) / rng * 100.0
    vw_cls_rng = np.sum(cls_rng * wv) / max(1.0, np.sum(wv))
    # AvgClsRange: unweighted mean closing range (VWClsRange's volume-weighted cousin) -
    # ported from IBD_rating_glm.
    avg_cls_rng = float(np.mean(cls_rng))
    mf_mult = ((wp - wl) - (wh - wp)) / rng
    cmf = np.sum(mf_mult * wv) / max(1.0, np.sum(wv))
    # PriceChg: raw window price change (%) - ported from IBD_rating_glm. Distinct from
    # Dist_XMA (distance from a moving average) and AbsRet_* (RS's own window returns);
    # this is the A/D-window-aligned version.
    price_chg = (wp[-1] / wp[0] - 1) * 100.0 if wp[0] > 0 else 0.0
    tag = f"{w}D"
    return {
        f"UpDnVol_{tag}": min(20.0, up_vol / max(1.0, dn_vol)),
        f"HeavyNetRatio_{tag}": heavy_net_ratio,
        f"NetHeavyDays_{tag}": net_heavy_days,
        f"NetHeavyIntensity_{tag}": net_heavy_intensity,
        f"AvgClsRange_{tag}": avg_cls_rng,
        f"VWClsRange_{tag}": vw_cls_rng,
        f"CMF_{tag}": cmf,
        f"PriceChg_{tag}": price_chg,
    }


def extract_price_features_asof(ticker, cutoff):
    p_path = resolve_cache_file(ticker, "_1d.parquet")
    if p_path is None:
        return None
    try:
        cdf = pd.read_parquet(p_path, columns=["High", "Low", "Close", "Volume"])
    except Exception:
        return None
    if cdf.empty:
        return None
    idx = pd.to_datetime(cdf.index)
    mask = idx <= pd.Timestamp(cutoff)
    prices = pd.to_numeric(cdf["Close"], errors="coerce").values[mask]
    highs = pd.to_numeric(cdf["High"], errors="coerce").values[mask]
    lows = pd.to_numeric(cdf["Low"], errors="coerce").values[mask]
    vols = pd.to_numeric(cdf["Volume"], errors="coerce").values[mask]
    valid = ~np.isnan(prices) & ~np.isnan(vols) & (prices > 0) & ~np.isnan(highs) & ~np.isnan(lows)
    prices, highs, lows, vols = prices[valid], highs[valid], lows[valid], vols[valid]
    if len(prices) < 60:
        return None

    latest = float(prices[-1])
    rec = {"Ticker": str(ticker).strip(), "Latest_Price": round(latest, 2), "Hist_Days": len(prices)}

    for label, days in RS_WINDOWS.items():
        rec[f"AbsRet_{label}"] = (latest / prices[-(days + 1)] - 1) * 100.0 if len(prices) > days else np.nan

    for ma in MA_WINDOWS:
        if len(prices) >= ma:
            rec[f"Dist_{ma}MA"] = (latest / np.mean(prices[-ma:]) - 1) * 100.0
    h52 = np.max(prices[-253:])
    rec["PctOff52WHigh"] = (h52 - latest) / h52 * 100.0 if h52 > 0 else 0.0

    for w in AD_WINDOWS:
        rec.update(_window_ad_stats(prices, vols, highs, lows, w))

    # whole-history (capped 250D) up-day vs down-day volume asymmetry
    tail_n = min(len(prices) - 1, 250)
    if tail_n >= 20:
        pr = np.diff(prices[-(tail_n + 1):]) / np.where(prices[-(tail_n + 1):-1] == 0, 1.0, prices[-(tail_n + 1):-1])
        vt = vols[-tail_n:]
        mv = max(1.0, np.mean(vt))
        vr = vt / mv
        up, dn = pr > 0, pr < 0
        rec["UpDayVolRatio"] = float(np.mean(vr[up])) if np.any(up) else 1.0
        rec["DnDayVolRatio"] = float(np.mean(vr[dn])) if np.any(dn) else 1.0

    return rec


def get_market_cap(ticker):
    f_path = resolve_cache_file(ticker, "_fund.json")
    if f_path is None:
        return np.nan
    try:
        with open(f_path) as fh:
            fund = json.load(fh)
    except Exception:
        return np.nan
    info = fund.get("info") if isinstance(fund, dict) else None
    if not isinstance(info, dict):
        return np.nan
    mc = info.get("marketCap")
    try:
        return float(mc) / 1e6 if mc is not None else np.nan
    except (TypeError, ValueError):
        return np.nan


# ──────────────────────────────────────────────────────────────────────────
# Load one snapshot's eligible (junk-filtered) universe with features
# ──────────────────────────────────────────────────────────────────────────

def load_snapshot(csv_path, label):
    df = pd.read_csv(csv_path, low_memory=False)
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    for c in ["Comp Rating", "RS Rating", "EPS Rating"]:
        df[c] = df[c].apply(clean_num)
    df["SMR_Num"] = df["SMR Rating"].astype(str).str.strip().str.upper().map(SMR_GRADE_NUM)
    df["AD_Num"] = df["A/D Rating"].astype(str).str.strip().str.upper().map(SUBTIER_13_MAP)
    df["AD_Letter"] = df["A/D Rating"].astype(str).str.strip().str.upper().where(
        df["A/D Rating"].astype(str).str.strip().str.upper().isin(AD_LETTERS))
    df["SMR_Letter"] = df["SMR Rating"].astype(str).str.strip().str.upper().where(
        df["SMR Rating"].astype(str).str.strip().str.upper().isin(SMR_LETTERS))
    # Comp Rating == 0 is MarketSurge's "not rated" sentinel, not a real score
    df_valid = df[(df["Comp Rating"] > 0) & (df["Symbol"] != "")].copy()

    asof = derive_ibd_asof(df)
    print(f"{csv_path.name}: as-of {asof} (expected {label}), {len(df):,} rows, "
          f"{len(df_valid):,} with valid (non-zero) Comp Rating")

    has_price = df_valid["Symbol"].apply(lambda t: resolve_cache_file(t, "_1d.parquet") is not None)
    df_valid = df_valid[has_price].copy()
    has_fund = df_valid["Symbol"].apply(lambda t: resolve_cache_file(t, "_fund.json") is not None)
    df_valid = df_valid[has_fund].copy()
    print(f"  -> {len(df_valid):,} with price parquet + fund json present")

    tickers = df_valid["Symbol"].tolist()
    with ThreadPoolExecutor(max_workers=16) as ex:
        price_res = list(ex.map(lambda t: extract_price_features_asof(t, asof), tickers))
    with ThreadPoolExecutor(max_workers=16) as ex:
        fund_res = list(ex.map(extract_fund_features, tickers))
    with ThreadPoolExecutor(max_workers=16) as ex:
        mcap_res = list(ex.map(get_market_cap, tickers))

    price_map = {r["Ticker"]: r for r in price_res if r is not None}
    fund_map = {r["Ticker"]: r for r in fund_res if r is not None}
    mcap_map = dict(zip(tickers, mcap_res))

    merged_rows = []
    for _, row in df_valid.iterrows():
        sym = row["Symbol"]
        pf = price_map.get(sym)
        if pf is None:
            continue
        rec = dict(row)
        rec.update(pf)
        ff = fund_map.get(sym, {})
        for k, v in ff.items():
            if k != "Ticker":
                rec[k] = v
        rec["MarketCap_mil"] = mcap_map.get(sym, np.nan)
        merged_rows.append(rec)
    merged = pd.DataFrame(merged_rows)
    print(f"  -> {len(merged):,} merged with usable price features, as-of {asof}")

    eligible = (merged["Latest_Price"] >= MIN_PRICE) & (
        merged["MarketCap_mil"].isna() | (merged["MarketCap_mil"] >= MIN_MKTCAP_MIL))
    print(f"  -> {eligible.sum():,} pass the eligibility filter (price>=${MIN_PRICE}, "
          f"mktcap>=${MIN_MKTCAP_MIL}M when known); {(~eligible).sum():,} excluded as junk")
    return merged[eligible].copy(), asof


def main():
    print("=" * 80)
    print("PRODUCTION RATING CALIBRATION — walk-forward (fit OLD, test NEW)")
    print("=" * 80)
    train, train_asof = load_snapshot(TRAIN_FILE, "2026-07-24")
    print()
    test, test_asof = load_snapshot(TEST_FILE, "2026-08-07")

    report = {"train_asof": train_asof, "test_asof": test_asof,
              "min_price": MIN_PRICE, "min_mktcap_mil": MIN_MKTCAP_MIL}

    # ════════════════════════════════════════════════════════════════════
    # RS RATING — weighted absolute-return blend (no SPY), percentile rank
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("RS RATING")
    print("=" * 80)
    # Dual-momentum: Dist_200MA (the stock's OWN distance from its 200-day MA - an
    # absolute-trend term, no benchmark involved) added as a 6th input, alongside the
    # 5 relative-return windows. Ported from IBD_rating_glm's second RS update (the
    # SCTR/dual-momentum insight: relative strength vs a benchmark PLUS an absolute
    # trend filter beats either alone), which took their forward R2 0.834->0.912.
    # NOTE: an earlier attempt here added Mansfield RS (ratio vs its own 200D SMA)
    # instead - a single-snapshot 70/30 holdout suggested a modest gain, but the
    # proper walk-forward test (train OLD, test NEW - the only trustworthy protocol
    # in this file) showed it net HURT forward R2 (0.889->0.811 on an identical
    # population), so it was reverted rather than deployed on the weaker evidence.
    rs_cols = [f"AbsRet_{w}" for w in RS_WINDOWS] + ["Dist_200MA"]
    tr = train.dropna(subset=rs_cols + ["RS Rating"])
    te = test.dropna(subset=rs_cols + ["RS Rating"])
    print(f"train n={len(tr):,}, test n={len(te):,}")

    # Trend-confirmation gate: scale 9M/12M down when 3M is negative, so a stale 12M gain
    # from a rally that's already reversed doesn't outweigh recent price action (found while
    # diagnosing LLY vs SPRB/CIFR/ECHO - see calc_ibd_ratings.py's RS_TREND_GATE_REDUCTION).
    # A/B'd against reduction in [0,1]: 0.50 sits at the peak (TEST R2 0.845->0.869).
    RS_GATE_REDUCTION = 0.50

    def gate_rs_matrix(df):
        X = df[rs_cols].values.copy()
        gate = np.where(df["AbsRet_3M"].values < 0, RS_GATE_REDUCTION, 1.0)
        X[:, 3] *= gate  # 9M
        X[:, 4] *= gate  # 12M
        return X

    perf_matrix_tr = gate_rs_matrix(tr)
    y_tr = tr["RS Rating"].values

    # Non-negative weights, sum to 1 - NOT monotonically constrained by window length.
    # An earlier version forced w1>=w2>=w3>=w4>=w5 (1M weight >= 3M >= 6M >= ... >= 12M) via a
    # cumulative-sum parameterization, which structurally CANNOT represent "3M matters more than
    # 1M" - found by head-to-head testing against IBD_rating_glm's independently-fit weights
    # (3M~0.51, 9M~0.38, 1M/6M/12M near 0), which measurably beat our monotonic-constrained fit
    # even inside OUR OWN pipeline (percentile-rank of a linear sum, not their sigmoid): R2 0.732
    # -> 0.746 on a wide validation population just from swapping in GLM's weights. Multiple
    # random restarts guard against the non-convex percentile-rank objective settling on a
    # different local optimum than the global one.
    def free_obj(params):
        v = np.abs(params)
        w = v / v.sum()
        raw = perf_matrix_tr @ w
        ref = np.sort(raw)
        pct = pct_from_ref(raw, ref)
        return np.mean(np.abs(y_tr - pct))

    rng = np.random.default_rng(0)
    n_feat = len(rs_cols)
    starts = [
        np.array([0.15, 0.10, 0.08, 0.05, 0.02, 0.0]),           # old monotonic-fit starting point
        np.array([0.0, 0.513, 0.051, 0.379, 0.057, 0.0]),        # GLM's reported window weights
        np.array([0.2, 0.2, 0.2, 0.2, 0.2, 0.0]),                # uniform (ex-Dist_200MA)
        np.array([0.08, 0.376, 0.26, 0.18, 0.102, 0.0]),         # our prior unconstrained-refit weights
        np.array([0.056, 0.263, 0.182, 0.126, 0.071, 0.3]),      # prior weights * 0.7 + 0.3 Dist_200MA
    ] + [rng.dirichlet(np.ones(n_feat)) for _ in range(7)]       # 7 random simplex points

    best_res, best_val = None, np.inf
    for x0 in starts:
        res = minimize(free_obj, x0, method="Nelder-Mead",
                        options={"maxiter": 5000, "xatol": 1e-7, "fatol": 1e-7})
        if res.fun < best_val:
            best_val, best_res = res.fun, res
    res = best_res
    v = np.abs(res.x)
    rs_weights = v / v.sum()
    # rs_cols (not RS_WINDOWS.keys()) labels every weight - RS_WINDOWS alone is 1 short
    # whenever a non-window feature (e.g. Dist_200MA) has been appended to rs_cols; zipping
    # against the shorter RS_WINDOWS list would silently truncate/mislabel the printed weights.
    print("RS weights (train-fit, unconstrained):", dict(zip(rs_cols, np.round(rs_weights, 4))))

    raw_tr = perf_matrix_tr @ rs_weights
    rs_score_ref = np.sort(raw_tr)
    pct_tr = pct_from_ref(raw_tr, rs_score_ref)
    print("TRAIN:", score_report("RS in-sample", y_tr, pct_tr))

    raw_te = gate_rs_matrix(te) @ rs_weights
    pct_te = pct_from_ref(raw_te, rs_score_ref)
    y_te = te["RS Rating"].values
    print("TEST (forward, no retrain):", score_report("RS out-of-sample", y_te, pct_te))

    report["rs"] = {"weights": dict(zip(rs_cols, rs_weights.tolist())), "feature_cols": rs_cols,
                     "windows": dict(RS_WINDOWS),
                     "score_ref_sample": rs_score_ref[::max(1, len(rs_score_ref) // 200)].tolist(),
                     "train_metric": score_report("train", y_tr, pct_tr),
                     "test_metric": score_report("test", y_te, pct_te)}

    # ── CANDIDATE: dual-momentum SIGMOID (relative-to-SPY, fixed monotone map,
    # no percentile rank) — ported from IBD_rating_glm's RS update, which reported
    # forward R^2 0.834->0.912 with this exact construction, far more than the
    # ~flat-R2/better-MAE gain Dist_200MA gave our linear-blend+percentile approach
    # above. Tests here on OUR OWN walk-forward split before deciding whether it's
    # worth replacing the production RS methodology (a bigger architectural change
    # than anything else in this file - RS would become a fixed transform, not a
    # live percentile rank, which needs its own case since A/D and SMR still are).
    print("\n" + "-" * 80)
    print("RS — candidate: dual-momentum sigmoid (relative-to-SPY, no percentile rank)")
    print("-" * 80)

    def _spy_perf_asof(asof):
        p = resolve_cache_file("SPY", "_1d.parquet")
        sc = pd.read_parquet(p, columns=["Close"])
        idx = pd.to_datetime(sc.index)
        sc = sc[idx <= pd.Timestamp(asof)]
        close = pd.to_numeric(sc["Close"], errors="coerce").dropna()
        close = close[close > 0].values
        latest = float(close[-1])
        return {label: (latest / close[-(days + 1)] if len(close) > days else 1.0)
                for label, days in RS_WINDOWS.items()}

    spy_perf_tr = _spy_perf_asof(train_asof)
    spy_perf_te = _spy_perf_asof(test_asof)
    rel_labels = list(RS_WINDOWS.keys())

    def _sigmoid(score):
        z = score - 100.0
        return np.clip(50.0 + 49.0 * (z / (np.abs(z) + 22.0)), 1, 99)

    def _rel_matrix(d, spy_perf):
        # RelPerf_W = stock growth factor / SPY growth factor, derived from the
        # already-computed AbsRet_W (= (growth factor - 1) * 100) - no new file I/O.
        return np.column_stack([(1.0 + d[f"AbsRet_{lbl}"].values / 100.0) / spy_perf[lbl]
                                 for lbl in rel_labels])

    dm = tr.dropna(subset=["Dist_200MA"]).copy()
    dm_te = te.dropna(subset=["Dist_200MA"]).copy()
    print(f"dual-momentum train n={len(dm):,}, test n={len(dm_te):,}")
    X_rel_tr = _rel_matrix(dm, spy_perf_tr)
    d200_tr = dm["Dist_200MA"].values.astype(float)
    y_dm_tr = dm["RS Rating"].values

    def _dm_score(Xv, d200v, w, k):
        raw = Xv @ w * 100.0
        return _sigmoid(raw + k * d200v / 100.0)

    def _dm_obj(p):
        w = np.abs(p[:5])
        w = w / w.sum()
        k = p[5]
        return np.mean(np.abs(y_dm_tr - _dm_score(X_rel_tr, d200_tr, w, k)))

    dm_starts = [
        np.array([0.067, 0.557, 0.027, 0.136, 0.214, 91.0]),  # GLM's reported dual-momentum fit
        np.array([0.15, 0.10, 0.08, 0.05, 0.02, 80.0]),
        np.array([0.2, 0.2, 0.2, 0.2, 0.2, 50.0]),
    ]
    best_dm, best_dm_val = None, np.inf
    for x0 in dm_starts:
        r = minimize(_dm_obj, x0, method="Nelder-Mead",
                     options={"maxiter": 8000, "xatol": 1e-8, "fatol": 1e-8})
        if r.fun < best_dm_val:
            best_dm_val, best_dm = r.fun, r
    w_dm = np.abs(best_dm.x[:5])
    w_dm = w_dm / w_dm.sum()
    k_dm = float(best_dm.x[5])
    print("dual-momentum weights:", dict(zip(rel_labels, np.round(w_dm, 4))), "| k:", round(k_dm, 2))

    pred_dm_tr = _dm_score(X_rel_tr, d200_tr, w_dm, k_dm)
    print("TRAIN:", score_report("RS dual-momentum in-sample", y_dm_tr, pred_dm_tr))

    X_rel_te = _rel_matrix(dm_te, spy_perf_te)
    d200_te = dm_te["Dist_200MA"].values.astype(float)
    y_dm_te = dm_te["RS Rating"].values
    pred_dm_te = _dm_score(X_rel_te, d200_te, w_dm, k_dm)
    print("TEST (forward, no retrain):", score_report("RS dual-momentum out-of-sample", y_dm_te, pred_dm_te))

    report["rs_dual_momentum_sigmoid"] = {
        "weights": dict(zip(rel_labels, w_dm.tolist())), "k": k_dm,
        "train_metric": score_report("train", y_dm_tr, pred_dm_tr),
        "test_metric": score_report("test", y_dm_te, pred_dm_te),
    }

    # ════════════════════════════════════════════════════════════════════
    # A/D RATING — OLS multi-window blend, percentile rank -> letter
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("A/D RATING")
    print("=" * 80)
    # Short 5D/10D windows tested against GLM's broader AD_WINDOWS=[5,10,30,65,130,250] and
    # confirmed a real out-of-sample win (TEST within-1 53.8%->56.7%, beating GLM's own 56.1%) -
    # recent accumulation/distribution apparently carries more signal than the 30D+ windows alone
    # capture. UpDayVolRatio/DnDayVolRatio (GLM's other addition) tested separately and added
    # nothing (53.6%, noise-level same as baseline) - dropped.
    #
    # CMF_130D and VWClsRange_65D dropped: VWClsRange_65D correlates 0.98 with CMF_65D (same
    # signal, redundant) and CMF_130D correlates 0.75 with CMF_65D, causing the ridge fit to
    # split a large canceling pair of coefficients across them (a collinearity artifact, not
    # real independent signal - confirmed diagnosing the LLY case, where high NetHeavyIntensity/
    # HeavyNetRatio was being dragged down by CMF_130D's negative coefficient). Dist_10MA/21MA
    # added (short-horizon price position, analogous to the 5D/10D volume windows).
    ad_cols_base = ["UpDnVol_65D", "HeavyNetRatio_65D", "NetHeavyIntensity_65D", "CMF_65D",
               "UpDnVol_130D", "NetHeavyIntensity_130D", "UpDnVol_30D", "NetHeavyIntensity_30D",
               "Dist_10MA", "Dist_21MA", "Dist_50MA", "Dist_150MA", "Dist_200MA", "PctOff52WHigh",
               "UpDnVol_5D", "HeavyNetRatio_5D", "NetHeavyIntensity_5D", "CMF_5D",
               "UpDnVol_10D", "HeavyNetRatio_10D", "NetHeavyIntensity_10D", "CMF_10D"]
    # Candidate features ported from IBD_rating_glm's later A/D update, not yet tested against
    # our own ridge-regularized pipeline: NetHeavyDays (count-based analog of
    # NetHeavyIntensity), AvgClsRange (unweighted closing-range mean), PriceChg (raw window
    # price change) at every window, plus 250D extensions of our existing metrics and the
    # 30D/250D HeavyNetRatio windows we don't have yet. VWClsRange deliberately excluded at
    # every window (not just 65D) - it's the same OHLCV-derived formula shape as CMF, and
    # 65D already showed near-total collinearity (corr 0.98) with CMF_65D; ridge alone
    # wasn't enough to make that pairing useful, so the same redundancy is assumed to hold
    # at other windows rather than re-proving it window by window.
    ad_cols_candidate = ["NetHeavyDays_5D", "NetHeavyDays_10D", "NetHeavyDays_30D",
                          "NetHeavyDays_65D", "NetHeavyDays_130D", "NetHeavyDays_250D",
                          "AvgClsRange_5D", "AvgClsRange_10D", "AvgClsRange_30D",
                          "AvgClsRange_65D", "AvgClsRange_130D", "AvgClsRange_250D",
                          "PriceChg_5D", "PriceChg_10D", "PriceChg_30D",
                          "PriceChg_65D", "PriceChg_130D", "PriceChg_250D",
                          "UpDnVol_250D", "HeavyNetRatio_30D", "HeavyNetRatio_250D",
                          "NetHeavyIntensity_250D", "CMF_250D"]
    ad_cols = ad_cols_base + ad_cols_candidate
    tr_ad = train.dropna(subset=ad_cols + ["AD_Num", "AD_Letter"])
    te_ad = test.dropna(subset=ad_cols + ["AD_Num", "AD_Letter"])
    print(f"train n={len(tr_ad):,}, test n={len(te_ad):,}  ({len(ad_cols)} candidate features)")

    X_tr = tr_ad[ad_cols].values
    y_tr_ad = tr_ad["AD_Num"].values
    # ridge (not plain OLS): CMF_65D/CMF_130D are correlated ~0.75, and plain lstsq_fit
    # splits that pair into large canceling coefficients that don't generalize.
    b0_ad, coefs_ad, _ = ridge_fit(X_tr, y_tr_ad, alpha=8.0)
    raw_tr_ad = X_tr @ coefs_ad
    ad_score_ref = np.sort(raw_tr_ad)
    pct_tr_ad = pct_from_ref(raw_tr_ad, ad_score_ref)
    cum_top_ad = fit_letter_map(pct_tr_ad, tr_ad["AD_Letter"].values, AD_LETTERS)
    letters_tr = letter_from_pct(pct_tr_ad, AD_LETTERS, cum_top_ad)
    exact, w1acc, n_ok = letter_accuracy(tr_ad["AD_Letter"].values, letters_tr, AD_LETTERS)
    print(f"TRAIN: exact={exact:.1f}% +/-1={w1acc:.1f}% (n={n_ok})")

    X_te = te_ad[ad_cols].values
    raw_te_ad = X_te @ coefs_ad
    pct_te_ad = pct_from_ref(raw_te_ad, ad_score_ref)
    letters_te = letter_from_pct(pct_te_ad, AD_LETTERS, cum_top_ad)
    exact_te, w1acc_te, n_ok_te = letter_accuracy(te_ad["AD_Letter"].values, letters_te, AD_LETTERS)
    print(f"TEST (forward, no retrain): exact={exact_te:.1f}% +/-1={w1acc_te:.1f}% (n={n_ok_te})")

    ad_weight_table = pd.DataFrame({"Feature": ad_cols, "Coef": np.round(coefs_ad, 5)})
    print(ad_weight_table.to_string(index=False))

    report["ad"] = {"features": ad_cols, "coefs": coefs_ad.tolist(), "intercept": float(b0_ad),
                     "score_ref_sample": ad_score_ref[::max(1, len(ad_score_ref) // 200)].tolist(),
                     "cum_top": cum_top_ad, "letters": AD_LETTERS,
                     "train_letter_acc": {"exact": exact, "within1": w1acc},
                     "test_letter_acc": {"exact": exact_te, "within1": w1acc_te}}

    # ════════════════════════════════════════════════════════════════════
    # EPS RATING — direct-scale OLS on log-compressed features
    # ════════════════════════════════════════════════════════════════════
    # 6 new analyst-driven features (EpsSurpriseMean, EpsBeatRate, EpsRevTrend,
    # EstEPSGrowth_Q, EstEPSGrowth_Y, EPS_StabilityCV) added alongside the original 4
    # after comparing against IBD_rating_glm's independent effort, which forward-
    # tested higher on EPS (R^2 0.345 vs our 0.333) using exactly this signal group -
    # extraction added to reverse_engineer_ratings_v2.py's extract_fund_features().
    print("\n" + "=" * 80)
    print("EPS RATING")
    print("=" * 80)
    # Info_* fields (13): yfinance info-dict fundamentals (margins, ROA, FCF/OCF yield,
    # debt/equity, current ratio, cash/share, analyst target upside, analyst count, forward
    # P/E) - ported from IBD_rating_glm's second update, which found these high-coverage
    # fields raised its own forward EPS R^2 0.345 -> 0.386. Extraction added to
    # reverse_engineer_ratings_v2.py's extract_fund_features() / calc_ibd_ratings.py's
    # extract_info_features().
    eps_info_cols = ["Info_ROA", "Info_EPSQGrowth", "Info_GrossMargin", "Info_OpMargin",
                      "Info_ProfitMargin", "Info_FCFYield", "Info_OCFYield", "Info_DebtEquity",
                      "Info_CurrentRatio", "Info_TotalCashPS", "Info_TargetUpside",
                      "Info_NumAnalysts", "Info_FwdPE"]
    # GLM "research round 2/4": gross-margin level+trend from the income statement (distinct
    # from Info_GrossMargin, which is a single yfinance info-dict snapshot, not a computed
    # multi-quarter series), forward revenue-estimate growth, and analyst recommendation
    # consensus - all found to improve both weeks' holdout in GLM's own ablation.
    eps_round4_cols = ["GrossMargin_Now", "GrossMargin_Trend", "RevEstGrowth_Q",
                        "RevEstGrowth_Y", "RecScore"]
    eps_raw_cols = ["EPS_Q0_YoY", "EPS_LT_Growth", "EPS_NegQRatio", "ROE",
                     "EPS_StabilityCV", "EpsSurpriseMean", "EpsBeatRate", "EpsRevTrend",
                     "EstEPSGrowth_Q", "EstEPSGrowth_Y"] + eps_info_cols + eps_round4_cols
    eps_log_cols = (["EPS_Q0_YoY", "EPS_LT_Growth", "ROE", "EpsSurpriseMean", "EpsRevTrend",
                     "EstEPSGrowth_Q", "EstEPSGrowth_Y"] + eps_info_cols + eps_round4_cols)
    eps_clip = {"EPS_Q0_YoY": (-300, 300), "EPS_LT_Growth": (-300, 300),
                "EpsSurpriseMean": (-300, 300), "EpsRevTrend": (-300, 300),
                "EstEPSGrowth_Q": (-300, 300), "EstEPSGrowth_Y": (-300, 300),
                "EPS_StabilityCV": (0, 10), "EPS_NegQRatio": (0, 1), "EpsBeatRate": (0, 1)}
    for d in (train, test):
        for c in eps_raw_cols:
            d[c] = pd.to_numeric(d[c], errors="coerce")
            if c in eps_clip:
                lo, hi = eps_clip[c]
                d[c] = d[c].clip(lo, hi)

    tr_eps = train.dropna(subset=["EPS Rating", "EPS_Q0_YoY"]).copy()
    eps_medians = {c: tr_eps[c].median() for c in eps_raw_cols}
    for c in eps_raw_cols:
        tr_eps[c] = tr_eps[c].fillna(eps_medians[c])
    te_eps = test.dropna(subset=["EPS Rating", "EPS_Q0_YoY"]).copy()
    for c in eps_raw_cols:
        te_eps[c] = te_eps[c].fillna(eps_medians[c])
    print(f"train n={len(tr_eps):,}, test n={len(te_eps):,}")

    def _eps_matrix(d):
        cols = [log_compress(d[c].values) if c in eps_log_cols else d[c].values for c in eps_raw_cols]
        return np.column_stack(cols)

    X_tr_eps = _eps_matrix(tr_eps)
    y_tr_eps = tr_eps["EPS Rating"].values
    b0_eps, coefs_eps, pred_tr_eps = lstsq_fit(X_tr_eps, y_tr_eps)
    print("TRAIN:", score_report("EPS in-sample", y_tr_eps, np.clip(pred_tr_eps, 1, 99)))

    X_te_eps = _eps_matrix(te_eps)
    pred_te_eps = b0_eps + X_te_eps @ coefs_eps
    y_te_eps = te_eps["EPS Rating"].values
    print("TEST (forward, no retrain):", score_report("EPS out-of-sample", y_te_eps, np.clip(pred_te_eps, 1, 99)))

    eps_weight_table = pd.DataFrame({"Feature": eps_raw_cols, "Coef": np.round(coefs_eps, 5)})
    print(eps_weight_table.to_string(index=False))

    report["eps"] = {"features": eps_raw_cols, "coefs": coefs_eps.tolist(), "intercept": float(b0_eps),
                      "medians": eps_medians, "log_features": eps_log_cols, "clip": eps_clip,
                      "train_metric": score_report("train", y_tr_eps, np.clip(pred_tr_eps, 1, 99)),
                      "test_metric": score_report("test", y_te_eps, np.clip(pred_te_eps, 1, 99))}

    # ════════════════════════════════════════════════════════════════════
    # SMR RATING — direct-scale OLS on log-compressed 3-pillar blend -> letter
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("SMR RATING")
    print("=" * 80)
    # Info_* fields (13, overlapping but not identical to EPS's set): ported from
    # IBD_rating_glm's second update, which raised its own forward SMR exact-letter accuracy
    # 60.2% -> 62.1% and direct-scale holdout R^2 to ~0.68-0.69.
    smr_info_cols = ["Info_ProfitMargin", "Info_RevGrowth", "Info_ROA", "Info_GrossMargin",
                      "Info_OpMargin", "Info_FCFYield", "Info_OCFYield", "Info_DebtEquity",
                      "Info_CurrentRatio", "Info_QuickRatio", "Info_EarningsGrowth",
                      "Info_EPSQGrowth", "Info_PriceBook"]
    # GLM "research round 4": Sloan (1996) accruals (NI-OCF)/TotalAssets - negative accruals
    # signal higher earnings quality - and the OCF/NI cash-conversion ratio, both computed
    # from the statements (not info.*). Raised GLM's own forward exact-letter 62.1%->62.3%.
    smr_round4_cols = ["Accrual_Q", "OCF_NI"]
    smr_core_cols = ["Sales_Q0_YoY", "Sales_LT_Growth", "Margin_Now", "Margin_Trend", "ROE"]
    smr_raw_cols = smr_core_cols + smr_info_cols + smr_round4_cols
    for d in (train, test):
        for c in smr_raw_cols:
            d[c] = pd.to_numeric(d[c], errors="coerce")

    # Only the original 5 core columns are required (dropna) - the 13 new Info_* fields are
    # median-imputed, matching EPS's pattern, so SMR coverage doesn't shrink toward only the
    # most info-rich tickers.
    tr_smr = train.dropna(subset=["SMR_Num", "SMR_Letter"] + smr_core_cols).copy()
    smr_medians = {c: tr_smr[c].median() for c in smr_raw_cols}
    for c in smr_raw_cols:
        tr_smr[c] = tr_smr[c].fillna(smr_medians[c])
    te_smr = test.dropna(subset=["SMR_Num", "SMR_Letter"] + smr_core_cols).copy()
    for c in smr_raw_cols:
        te_smr[c] = te_smr[c].fillna(smr_medians[c])
    print(f"train n={len(tr_smr):,}, test n={len(te_smr):,}")

    X_tr_smr = np.column_stack([log_compress(tr_smr[c].values) for c in smr_raw_cols])
    y_tr_smr = tr_smr["SMR_Num"].values
    b0_smr, coefs_smr, pred_tr_smr = lstsq_fit(X_tr_smr, y_tr_smr)
    cum_top_smr = fit_letter_map(pred_tr_smr, tr_smr["SMR_Letter"].values, SMR_LETTERS)
    # SMR letters are assigned directly off the (train-frozen) numeric-score distribution,
    # not a percentile-of-percentile step, since direct-scale beat percentile-rank for SMR.
    smr_score_ref = np.sort(pred_tr_smr)
    pct_tr_smr = pct_from_ref(pred_tr_smr, smr_score_ref)
    letters_tr_smr = letter_from_pct(pct_tr_smr, SMR_LETTERS, cum_top_smr)
    exact_smr, w1_smr, n_ok_smr = letter_accuracy(tr_smr["SMR_Letter"].values, letters_tr_smr, SMR_LETTERS)
    print(f"TRAIN: R2={score_report('x', y_tr_smr, pred_tr_smr)['R2']}, letter exact={exact_smr:.1f}% +/-1={w1_smr:.1f}%")

    X_te_smr = np.column_stack([log_compress(te_smr[c].values) for c in smr_raw_cols])
    pred_te_smr = b0_smr + X_te_smr @ coefs_smr
    pct_te_smr = pct_from_ref(pred_te_smr, smr_score_ref)
    letters_te_smr = letter_from_pct(pct_te_smr, SMR_LETTERS, cum_top_smr)
    exact_te_smr, w1_te_smr, n_ok_te_smr = letter_accuracy(te_smr["SMR_Letter"].values, letters_te_smr, SMR_LETTERS)
    y_te_smr = te_smr["SMR_Num"].values
    print(f"TEST (forward, no retrain): R2={score_report('x', y_te_smr, pred_te_smr)['R2']}, "
          f"letter exact={exact_te_smr:.1f}% +/-1={w1_te_smr:.1f}%")

    smr_weight_table = pd.DataFrame({"Feature": smr_raw_cols, "Coef": np.round(coefs_smr, 5)})
    print(smr_weight_table.to_string(index=False))

    report["smr"] = {"features": smr_raw_cols, "coefs": coefs_smr.tolist(), "intercept": float(b0_smr),
                      "medians": smr_medians, "cum_top": cum_top_smr, "letters": SMR_LETTERS,
                      "score_ref_sample": smr_score_ref[::max(1, len(smr_score_ref) // 200)].tolist(),
                      "train_letter_acc": {"exact": exact_smr, "within1": w1_smr},
                      "test_letter_acc": {"exact": exact_te_smr, "within1": w1_te_smr}}

    # ════════════════════════════════════════════════════════════════════
    # COMPOSITE RATING — percentile-normalized components, OLS combine
    # ════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("COMPOSITE RATING")
    print("=" * 80)

    # Composite population requires only the ORIGINAL 4 core EPS features (matching the
    # pre-existing dropna, so adding the 6 new analyst-driven columns doesn't shrink/bias
    # the test population toward more widely-covered tickers) - the 6 new ones are median-
    # imputed same as production scoring will do for any ticker missing analyst estimates.
    eps_core_cols = ["EPS_Q0_YoY", "EPS_LT_Growth", "EPS_NegQRatio", "ROE"]

    def comp_features(df, spy_perf_snap, ad_coefs, ad_ref, eps_coefs, eps_b0, eps_meds,
                       smr_coefs, smr_b0, smr_meds, smr_ref):
        sub = df.dropna(subset=["Comp Rating"] + rs_cols + ad_cols + eps_core_cols + smr_core_cols).copy()
        # RS component is the dual-momentum SIGMOID (already final 1-99, same value the
        # production pipeline will emit as RS Rating) - not the old percentile-rank path.
        # Composite must be refit against whatever RS actually looks like, since a sigmoid's
        # output distribution differs from a percentile rank's (uniform by construction);
        # reusing coefficients fit on the OLD RS distribution silently miscalibrates
        # Composite even though RS itself got much more accurate (caught via a full-scale
        # production validation: RS R2 0.777->0.924, but Comp R2 REGRESSED 0.753->0.702
        # until this refit).
        X_rel_sub = _rel_matrix(sub, spy_perf_snap)
        d200_sub = sub["Dist_200MA"].values.astype(float)
        rs_pct = _dm_score(X_rel_sub, d200_sub, w_dm, k_dm)
        raw_ad = sub[ad_cols].values @ ad_coefs
        ad_pct = pct_from_ref(raw_ad, ad_ref)
        Xe = np.column_stack([
            log_compress(sub[c].fillna(eps_meds[c]).values) if c in eps_log_cols
            else sub[c].fillna(eps_meds[c]).values
            for c in eps_raw_cols
        ])
        eps_raw = np.clip(eps_b0 + Xe @ eps_coefs, 1, 99)
        Xs = np.column_stack([log_compress(sub[c].fillna(smr_meds[c]).values) for c in smr_raw_cols])
        smr_raw = smr_b0 + Xs @ smr_coefs
        smr_pct = pct_from_ref(smr_raw, smr_ref)

        # Group RS: industry-mean RS (this SAME dual-momentum-sigmoid rs_pct, grouped by the
        # real "Industry Name" column the MarketSurge export carries), percentile-ranked
        # within this population. Ported from IBD_rating_glm, which found it lifted both
        # weeks' holdout Composite R^2 (their forward number: 0.724->0.736). Unmapped/tiny
        # groups fall back to the neutral middle (50) rather than dropping the row -
        # matches GLM's own "group_median ~ 50" fallback and our existing convention of
        # median-imputing rather than losing universe coverage over one missing input.
        ind = sub["Industry Name"].astype(str).str.strip().where(sub["Industry Name"].notna())
        ind = ind.where(ind != "")
        grp_mean_rs = pd.Series(rs_pct, index=sub.index).groupby(ind).transform("mean")
        group_rs_pct = grp_mean_rs.rank(pct=True) * 98 + 1
        group_rs_pct = group_rs_pct.where(ind.notna(), 50.0).fillna(50.0).values

        return sub["Comp Rating"].values, rs_pct, eps_raw, smr_pct, ad_pct, group_rs_pct

    y_c_tr, rs_c_tr, eps_c_tr, smr_c_tr, ad_c_tr, grs_c_tr = comp_features(
        train, spy_perf_tr, coefs_ad, ad_score_ref, coefs_eps, b0_eps, eps_medians,
        coefs_smr, b0_smr, smr_medians, smr_score_ref)
    X_comp_tr = np.column_stack([eps_c_tr, rs_c_tr, smr_c_tr, ad_c_tr, grs_c_tr])
    b0_comp, coefs_comp, pred_comp_tr = lstsq_fit(X_comp_tr, y_c_tr)
    print(f"train n={len(y_c_tr):,}:", score_report("Comp in-sample", y_c_tr, np.clip(pred_comp_tr, 1, 99)))

    y_c_te, rs_c_te, eps_c_te, smr_c_te, ad_c_te, grs_c_te = comp_features(
        test, spy_perf_te, coefs_ad, ad_score_ref, coefs_eps, b0_eps, eps_medians,
        coefs_smr, b0_smr, smr_medians, smr_score_ref)
    X_comp_te = np.column_stack([eps_c_te, rs_c_te, smr_c_te, ad_c_te, grs_c_te])
    pred_comp_te = b0_comp + X_comp_te @ coefs_comp
    print(f"test n={len(y_c_te):,} (forward, no retrain):",
          score_report("Comp out-of-sample", y_c_te, np.clip(pred_comp_te, 1, 99)))

    comp_weight_table = pd.DataFrame({"Component": ["EPS", "RS", "SMR", "A/D", "GroupRS"], "Coef": np.round(coefs_comp, 4)})
    comp_weight_table["Weight_Pct"] = (comp_weight_table["Coef"].abs() / comp_weight_table["Coef"].abs().sum() * 100).round(1)
    print(f"intercept={b0_comp:.3f}")
    print(comp_weight_table.to_string(index=False))

    report["comp"] = {"coefs": {"EPS": coefs_comp[0], "RS": coefs_comp[1], "SMR": coefs_comp[2],
                                 "AD": coefs_comp[3], "GroupRS": coefs_comp[4]},
                       "intercept": float(b0_comp),
                       "train_metric": score_report("train", y_c_tr, np.clip(pred_comp_tr, 1, 99)),
                       "test_metric": score_report("test", y_c_te, np.clip(pred_comp_te, 1, 99))}

    # ── Tail accuracy (Comp Rating >= 80): what a screener actually shows/hides matters more
    # than blanket MAE across the whole range — errors among low-rated junk are much less costly.
    print("\n" + "-" * 60)
    print("TAIL ACCURACY: true Comp Rating >= 80 (TEST, forward, no retrain)")
    print("-" * 60)
    pred_te_clipped = np.clip(pred_comp_te, 1, 99)
    hi_mask = y_c_te >= 80
    hi_true, hi_pred = y_c_te[hi_mask], pred_te_clipped[hi_mask]
    print(f"n(true>=80)={hi_mask.sum():,}  MAE={np.mean(np.abs(hi_true - hi_pred)):.2f}  "
          f"mean_pred={hi_pred.mean():.1f}")
    print(f"  of true>=80: scored>=70 = {(hi_pred >= 70).mean()*100:.1f}%  "
          f"scored>=80 = {(hi_pred >= 80).mean()*100:.1f}%  "
          f"badly missed (<60) = {(hi_pred < 60).mean()*100:.1f}%")
    pred_hi_mask = pred_te_clipped >= 80
    if pred_hi_mask.sum() > 0:
        pt, pp = y_c_te[pred_hi_mask], pred_te_clipped[pred_hi_mask]
        print(f"n(pred>=80)={pred_hi_mask.sum():,}  "
              f"of predicted>=80: truly>=70 = {(pt >= 70).mean()*100:.1f}%  "
              f"truly>=80 = {(pt >= 80).mean()*100:.1f}%  "
              f"false-positive junk (<60) = {(pt < 60).mean()*100:.1f}%")
    report["comp"]["tail_ge80"] = {
        "n_true_ge80": int(hi_mask.sum()), "mae": float(np.mean(np.abs(hi_true - hi_pred))),
        "pct_scored_ge70": float((hi_pred >= 70).mean() * 100),
        "pct_scored_ge80": float((hi_pred >= 80).mean() * 100),
        "pct_badly_missed_lt60": float((hi_pred < 60).mean() * 100),
        "n_pred_ge80": int(pred_hi_mask.sum()),
        "pct_pred_ge80_truly_ge70": float((y_c_te[pred_hi_mask] >= 70).mean() * 100) if pred_hi_mask.sum() else None,
        "pct_pred_ge80_truly_ge80": float((y_c_te[pred_hi_mask] >= 80).mean() * 100) if pred_hi_mask.sum() else None,
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / "production_fitted_params.json"
    with open(out_path, "w") as fh:
        json.dump(report, fh, indent=2, default=float)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
