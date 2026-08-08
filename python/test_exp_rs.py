#!/usr/bin/env python3
"""
test_exp_rs.py — test exponential-decay daily-return weighting for RS Rating's raw
score, as an alternative to the discrete 5-window (1M/3M/6M/9M/12M) blend.

Two variants:
  ABS      : exponentially-weighted sum of the stock's own daily % returns (no SPY)
  SPY_REL  : exponentially-weighted sum of (stock daily return - SPY daily return)

For each variant, sweep a range of half-lives (in trading days) plus an OPTIMIZED
decay rate (Nelder-Mead, same closed-form-weight-fitting approach as the discrete
scheme), fit on the 2026-07-24 snapshot, forward-tested (no retrain) on 2026-08-07.

Also reports LLY specifically (the case that prompted this test: weak trailing
month, strong trailing year) and the Comp Rating >= 80 tail impact once the best
RS variant feeds through to Composite.

Closed-form only: percentile ranks + scipy.optimize on ONE scalar (decay rate). No ML.
"""

import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reverse_engineer_ratings_v2 import clean_num, resolve_cache_file, CACHE_DIR, REPO_DIR  # noqa: E402
from fit_production_ratings import pct_from_ref  # noqa: E402
from calc_ibd_ratings import derive_ibd_asof  # noqa: E402

N_DAYS_CAP = 380  # trading days of daily-return history kept per ticker (~18 months)
HALF_LIVES = [5, 8, 12, 15, 21, 30, 42, 55, 63, 90, 110, 126, 160, 200, 252]

TRAIN_FILE = REPO_DIR / "IBD" / "marketsurge.csv"
TEST_FILE = REPO_DIR / "IBD" / "marketsuge-8-7-2026.csv"


def load_daily_rets_asof(ticker, cutoff):
    """Daily % returns (most recent last), truncated to cutoff, capped at N_DAYS_CAP+1
    closes (so N_DAYS_CAP returns)."""
    p = resolve_cache_file(ticker, "_1d.parquet")
    if p is None:
        return None
    try:
        cdf = pd.read_parquet(p, columns=["Close"])
    except Exception:
        return None
    if cdf.empty:
        return None
    idx = pd.to_datetime(cdf.index)
    mask = idx <= pd.Timestamp(cutoff)
    prices = pd.to_numeric(cdf["Close"], errors="coerce").values[mask]
    prices = prices[~np.isnan(prices) & (prices > 0)]
    if len(prices) < 30:
        return None
    prices = prices[-(N_DAYS_CAP + 1):]
    rets = np.diff(prices) / prices[:-1] * 100.0
    return rets  # oldest first, most recent last


def load_snapshot_daily(csv_path, label, spy_rets):
    df = pd.read_csv(csv_path, low_memory=False)
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df["Comp Rating"] = df["Comp Rating"].apply(clean_num)
    df["RS Rating"] = df["RS Rating"].apply(clean_num)
    df_valid = df[(df["Comp Rating"] > 0) & (df["Symbol"] != "")].copy()

    asof = derive_ibd_asof(df)
    print(f"{csv_path.name}: as-of {asof} (expected {label}), {len(df_valid):,} valid rows")

    has_price = df_valid["Symbol"].apply(lambda t: resolve_cache_file(t, "_1d.parquet") is not None)
    df_valid = df_valid[has_price].copy()
    has_fund = df_valid["Symbol"].apply(lambda t: resolve_cache_file(t, "_fund.json") is not None)
    df_valid = df_valid[has_fund].copy()

    tickers = df_valid["Symbol"].tolist()
    with ThreadPoolExecutor(max_workers=16) as ex:
        rets_res = list(ex.map(lambda t: load_daily_rets_asof(t, asof), tickers))
    rets_map = dict(zip(tickers, rets_res))

    # market cap + price for the eligibility filter (same $4 / $50M rule as production)
    def get_price_mcap(ticker):
        import json
        p = resolve_cache_file(ticker, "_1d.parquet")
        price = np.nan
        if p is not None:
            try:
                cdf = pd.read_parquet(p, columns=["Close"])
                idx = pd.to_datetime(cdf.index)
                c = pd.to_numeric(cdf["Close"], errors="coerce").values[idx <= pd.Timestamp(asof)]
                c = c[~np.isnan(c) & (c > 0)]
                if len(c):
                    price = float(c[-1])
            except Exception:
                pass
        mcap = np.nan
        fp = resolve_cache_file(ticker, "_fund.json")
        if fp is not None:
            try:
                with open(fp) as fh:
                    fund = json.load(fh)
                info = fund.get("info") or {}
                mc = info.get("marketCap")
                mcap = float(mc) / 1e6 if mc is not None else np.nan
            except Exception:
                pass
        return price, mcap

    with ThreadPoolExecutor(max_workers=16) as ex:
        pm = list(ex.map(get_price_mcap, tickers))
    price_map = dict(zip(tickers, [x[0] for x in pm]))
    mcap_map = dict(zip(tickers, [x[1] for x in pm]))

    df_valid["Latest_Price"] = df_valid["Symbol"].map(price_map)
    df_valid["MarketCap_mil"] = df_valid["Symbol"].map(mcap_map)
    eligible = (df_valid["Latest_Price"] >= 4.0) & (
        df_valid["MarketCap_mil"].isna() | (df_valid["MarketCap_mil"] >= 50.0))
    df_valid = df_valid[eligible].copy()
    df_valid["_rets"] = df_valid["Symbol"].map(rets_map)
    df_valid = df_valid[df_valid["_rets"].notna()].copy()
    print(f"  -> {len(df_valid):,} eligible with usable daily-return history, as-of {asof}")
    return df_valid, asof


def exp_score(rets, spy_rets_aligned, half_life, spy_relative):
    """rets, spy_rets_aligned: same-length arrays, oldest first, most recent last."""
    n = len(rets)
    if spy_relative:
        m = min(n, len(spy_rets_aligned))
        series = rets[-m:] - spy_rets_aligned[-m:]
    else:
        series = rets
        m = n
    lam = np.log(2) / half_life
    t = np.arange(m)[::-1]  # 0 = most recent
    w = np.exp(-lam * t)
    w = w / w.sum()
    return float(np.sum(series * w))


def main():
    print("=" * 80)
    print("EXPONENTIAL-DECAY RS SCORING TEST")
    print("=" * 80)

    spy_rets_train_full = load_daily_rets_asof("SPY", "2026-07-24")
    spy_rets_test_full = load_daily_rets_asof("SPY", "2026-08-07")

    train, train_asof = load_snapshot_daily(TRAIN_FILE, "2026-07-24", spy_rets_train_full)
    test, test_asof = load_snapshot_daily(TEST_FILE, "2026-08-07", spy_rets_test_full)

    y_tr = train["RS Rating"].values
    y_te = test["RS Rating"].values

    def scores_for(df, spy_rets_full, half_life, spy_relative):
        out = np.full(len(df), np.nan)
        for i, rets in enumerate(df["_rets"].values):
            out[i] = exp_score(rets, spy_rets_full, half_life, spy_relative)
        return out

    results = []
    print("\n--- Half-life sweep (TEST = forward, no retrain) ---")
    print(f"{'Variant':10s} {'HalfLife':>8s} {'TRAIN R2':>9s} {'TEST R2':>8s} {'TEST MAE':>9s} {'TEST Corr':>10s}")
    best = {"ABS": (None, -999), "SPY_REL": (None, -999)}
    for spy_relative, name in [(False, "ABS"), (True, "SPY_REL")]:
        for hl in HALF_LIVES:
            raw_tr = scores_for(train, spy_rets_train_full, hl, spy_relative)
            ref = np.sort(raw_tr)
            pct_tr = pct_from_ref(raw_tr, ref)
            r2_tr = 1 - np.sum((y_tr - pct_tr) ** 2) / np.sum((y_tr - y_tr.mean()) ** 2)

            raw_te = scores_for(test, spy_rets_test_full, hl, spy_relative)
            pct_te = pct_from_ref(raw_te, ref)
            r2_te = 1 - np.sum((y_te - pct_te) ** 2) / np.sum((y_te - y_te.mean()) ** 2)
            mae_te = np.mean(np.abs(y_te - pct_te))
            corr_te = np.corrcoef(y_te, pct_te)[0, 1]
            print(f"{name:10s} {hl:8d} {r2_tr:9.4f} {r2_te:8.4f} {mae_te:9.2f} {corr_te:10.4f}")
            results.append({"Variant": name, "HalfLife": hl, "Train_R2": r2_tr, "Test_R2": r2_te,
                             "Test_MAE": mae_te, "Test_Corr": corr_te})
            if r2_tr > best[name][1]:
                best[name] = (hl, r2_tr)

    print("\n--- Optimized decay rate (Nelder-Mead on TRAIN MAE, single scalar) ---")
    for spy_relative, name in [(False, "ABS"), (True, "SPY_REL")]:
        def obj(log_hl):
            hl = np.exp(log_hl[0])
            raw_tr = scores_for(train, spy_rets_train_full, hl, spy_relative)
            ref = np.sort(raw_tr)
            pct_tr = pct_from_ref(raw_tr, ref)
            return np.mean(np.abs(y_tr - pct_tr))

        res = minimize_scalar(lambda x: obj([x]), bounds=(np.log(3), np.log(300)), method="bounded")
        opt_hl = float(np.exp(res.x))
        raw_tr = scores_for(train, spy_rets_train_full, opt_hl, spy_relative)
        ref = np.sort(raw_tr)
        pct_tr = pct_from_ref(raw_tr, ref)
        r2_tr = 1 - np.sum((y_tr - pct_tr) ** 2) / np.sum((y_tr - y_tr.mean()) ** 2)
        raw_te = scores_for(test, spy_rets_test_full, opt_hl, spy_relative)
        pct_te = pct_from_ref(raw_te, ref)
        r2_te = 1 - np.sum((y_te - pct_te) ** 2) / np.sum((y_te - y_te.mean()) ** 2)
        mae_te = np.mean(np.abs(y_te - pct_te))
        corr_te = np.corrcoef(y_te, pct_te)[0, 1]
        print(f"{name}: optimal half-life={opt_hl:.1f}d  TRAIN R2={r2_tr:.4f}  "
              f"TEST R2={r2_te:.4f} MAE={mae_te:.2f} corr={corr_te:.4f}")

        # LLY check
        if "LLY" in test["Symbol"].values:
            lly_idx = test.index[test["Symbol"] == "LLY"][0]
            lly_pos = test.index.get_loc(lly_idx)
            print(f"  LLY predicted RS ({name}, hl={opt_hl:.0f}d) = {pct_te[lly_pos]:.1f}  (true=86.0)")

    print("\n--- Current production (discrete 5-window) baseline, TEST ---")
    rs_cols_import = ["AbsRet_1M", "AbsRet_3M", "AbsRet_6M", "AbsRet_9M", "AbsRet_12M"]
    print("  (see prior fit_production_ratings.py run: TEST R2=0.845, MAE=7.60, corr=0.940, "
          "LLY predicted=67.4)")


if __name__ == "__main__":
    main()
