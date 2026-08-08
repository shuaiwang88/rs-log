#!/usr/bin/env python3
"""
common.py — shared deterministic (non-ML) toolkit for the IBD rating work in
IBD_rating_glm.

Everything here is transparent and closed-form:
  * percentile ranks on IBD's 1-99 scale
  * OLS via numpy.linalg.lstsq (a plain linear model, not a black box)
  * constrained scalar-weight optimisation (scipy.optimize.minimize) on fully
    transparent formulas
  * letter / threshold calibration by matching the ground-truth grade
    distribution (frequency mapping) — no classifiers.

No sklearn, no trees, no boosting, no neural nets.

The MarketSurge CSV is used ONLY as the ground-truth label for calibration and
validation.  Every model input comes from ticker_cache:
  * price/volume:  {SYMBOL}_1d.parquet
  * fundamentals:  {SYMBOL}_fund.json
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sstats
from scipy.optimize import minimize

# ──────────────────────────────────────────────────────────────────────────────
# Paths (folder lives at repo root; raw data is read from the repo, never written)
# ──────────────────────────────────────────────────────────────────────────────
FOLDER = Path(__file__).resolve().parent
REPO_DIR = FOLDER.parent
CACHE_DIR = REPO_DIR / "ticker_cache"
IBD_DIR = REPO_DIR / "IBD"
CSV_PATH = IBD_DIR / "marketsuge-8-7-2026.csv"     # new snapshot (primary ground truth)
CSV_OLD_PATH = IBD_DIR / "marketsurge.csv"          # older snapshot (robustness check)
INDUSTRY_MAP_PATH = REPO_DIR / "IBD Industry Mapping.txt"  # Symbol -> Industry
OUTPUT_DIR = FOLDER / "output"

# ──────────────────────────────────────────────────────────────────────────────
# Window / universe constants
# ──────────────────────────────────────────────────────────────────────────────
RS_WINDOWS = {"1M": 21, "3M": 63, "6M": 126, "9M": 188, "12M": 249}
AD_WINDOWS = [5, 10, 30, 65, 130, 250]
MA_WINDOWS = [10, 21, 50, 150, 200]

AD_LETTERS_ORDERED = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-",
                      "D+", "D", "D-", "E"]
AD_SUBTIER_13 = {g: 13 - i for i, g in enumerate(AD_LETTERS_ORDERED)}   # A+=13 .. E=1
SMR_LETTERS_ORDERED = ["A", "B", "C", "D", "E"]
SMR_GRADE_NUM = {g: 90 - i * 20 for i, g in enumerate(SMR_LETTERS_ORDERED)}  # A=90 .. E=10

MAX_WORKERS = 16


# ──────────────────────────────────────────────────────────────────────────────
# Small numeric helpers
# ──────────────────────────────────────────────────────────────────────────────
def clean_num(val):
    """Coerce '%'/','/'$'-laden CSV cells to float (NaN on failure)."""
    if pd.isna(val):
        return np.nan
    s = str(val).replace("%", "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return np.nan


def log_compress(x):
    """Sign-preserving log compression: tames small-denominator YoY blowups
    (e.g. EPS/Margin growth off a near-zero base can read +28,600% or
    -1,000,000%) while preserving rank order — unlike a hard clip which throws
    away the difference between merely-large and absurdly-large values.
    Applied identically at fit time and at scoring time (transparent, closed
    form; not a model).  log1p(0) = 0 is preserved.
    """
    x = np.asarray(x, dtype=float)
    return np.sign(x) * np.log1p(np.abs(x))


def pct_rank_99(raw):
    """Percentile-rank an array to IBD's 1-99 scale (average method)."""
    r = pd.Series(raw).rank(pct=True, method="average").values * 99
    return np.clip(r, 1, 99)


def transfer_pct_rank(train_vals, test_vals):
    """Map test values onto the train distribution's percentile scale.

    Deterministic, no leakage: each test value gets the percentile it would
    occupy within the train sample.
    """
    train_vals = np.asarray(train_vals, dtype=float)
    test_vals = np.asarray(test_vals, dtype=float)
    out = np.full(len(test_vals), np.nan)
    for i, v in enumerate(test_vals):
        if np.isnan(v):
            continue
        out[i] = sstats.percentileofscore(train_vals, v, kind="mean") / 100.0 * 99.0
    return np.clip(out, 1, 99)


# ──────────────────────────────────────────────────────────────────────────────
# Evaluation metrics
# ──────────────────────────────────────────────────────────────────────────────
def r2(y, p):
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ss_res = np.sum((y - p) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    return float(1.0 - ss_res / ss_tot) if ss_tot > 0 else np.nan


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
    """OLS with intercept via closed-form normal equations (numpy lstsq)."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    A = np.column_stack([np.ones(len(X)), X])
    coefs, *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ coefs
    return coefs[0], coefs[1:], pred


# ──────────────────────────────────────────────────────────────────────────────
# Cache file resolution + MarketSurge ground truth
# ──────────────────────────────────────────────────────────────────────────────
def resolve_cache_file(ticker, suffix, cache_dir=None):
    t = str(ticker).strip()
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
    for cand in (cache_dir / f"{t}{suffix}", cache_dir / f"{t.replace('.', '-')}{suffix}"):
        if cand.exists():
            return cand
    return None


def load_marketsurge(csv_path=None):
    """Load a MarketSurge CSV and normalise the rating columns.

    Rating conventions:
      * Comp / RS / EPS / RS-3M / RS-6M are numeric 1-99 (0 = not rated)
      * SMR is a single letter A-E
      * A/D and Ind Group RS are 13 sub-tiers A+ .. E
    """
    if csv_path is None:
        csv_path = CSV_PATH
    df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    # drop MarketSurge-only columns whose names collide with our ticker_cache
    # feature names (they would otherwise shadow the fund-json values)
    df = df.drop(columns=[c for c in ("ROE", "Sector") if c in df.columns])
    for c in ["Comp Rating", "RS Rating", "EPS Rating", "RS 3-Month Rating",
              "RS 6-Month Rating"]:
        if c in df.columns:
            df[c] = df[c].apply(clean_num)
    df["SMR_Num"] = df["SMR Rating"].astype(str).str.strip().str.upper().map(SMR_GRADE_NUM)
    df["AD_Subtier"] = df["A/D Rating"].astype(str).str.strip().str.upper()
    df["AD_Num"] = df["AD_Subtier"].map(AD_SUBTIER_13)
    df["AD_Tier"] = df["AD_Subtier"].str[0].where(df["AD_Subtier"].isin(AD_LETTERS_ORDERED))
    return df


def load_industry_map(path=None):
    """Load the Symbol -> Industry mapping (IBD Industry Mapping.txt).

    Returns a dict {SYMBOL: Industry Name}.  Missing entries are allowed; callers
    may fall back to the fund-json `info.industry`.
    """
    p = Path(path) if path else INDUSTRY_MAP_PATH
    if not p.exists():
        return {}
    mapping = {}
    with open(p, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line or line.startswith("Symbol,") or "," not in line:
                continue
            parts = line.split(",", 1)
            sym = parts[0].strip()
            ind = parts[1].strip()
            if sym and ind:
                mapping[sym] = ind
    return mapping


def industry_map_series(tickers, mapping=None, fallback_series=None):
    """Series of Industry per ticker: mapping file first, fund-json fallback."""
    if mapping is None:
        mapping = load_industry_map()
    tickers = [str(t).strip() for t in tickers]
    out = pd.Series([mapping.get(t) for t in tickers], index=range(len(tickers)))
    if fallback_series is not None:
        fb = fallback_series.reset_index(drop=True)
        miss = out.isna()
        if miss.any():
            out[miss] = fb[miss]
    return out


def build_universe(df=None, require_fund=True):
    """Universe = tickers with a real (>0) Comp Rating AND cache files.

    Returns (df_valid, price_universe_df, fund_universe_df).
    Comp=0 is treated as 'not rated' (every Comp=0 row also has EPS=0).
    """
    if df is None:
        df = load_marketsurge()
    df_valid = df[(df["Comp Rating"] > 0) & df["Symbol"].ne("")].copy()
    has_price = df_valid["Symbol"].apply(lambda t: resolve_cache_file(t, "_1d.parquet") is not None)
    df_price = df_valid[has_price].copy()
    if require_fund:
        has_fund = df_price["Symbol"].apply(lambda t: resolve_cache_file(t, "_fund.json") is not None)
        df_fund = df_price[has_fund].copy()
    else:
        df_fund = df_price.copy()
    return df_valid, df_price, df_fund


# ──────────────────────────────────────────────────────────────────────────────
# SPY reference performance
# ──────────────────────────────────────────────────────────────────────────────
def load_spy_perf(cache_dir=None, asof=None):
    """Return (window_perf_dict, baseline_perf_c, n_days).

    `asof` (YYYY-MM-DD) truncates SPY history to that trading day, which is
    needed when validating against an older MarketSurge snapshot.
    """
    p = resolve_cache_file("SPY", "_1d.parquet", cache_dir)
    if p is None:
        raise FileNotFoundError("SPY parquet missing from ticker_cache")
    spy = pd.read_parquet(p, columns=["Close"])
    if asof is not None:
        try:
            idx = pd.to_datetime(spy.index)
            spy = spy[idx <= pd.Timestamp(asof)]
        except Exception:
            pass
    close = pd.to_numeric(spy["Close"], errors="coerce").dropna()
    close = close[close > 0].values
    latest = float(close[-1])
    perf = {}
    for label, days in RS_WINDOWS.items():
        perf[label] = latest / close[-(days + 1)] if len(close) > days else 1.0
    # baseline 40/20/20/20 SPY perf (same offsets as calc_ibd_ratings.calc_rs_rating_snapshot)
    n = len(close)
    n63, n126, n189, n252 = min(n - 1, 63), min(n - 1, 126), min(n - 1, 189), min(n - 1, 252)
    perf_c = (0.4 * (close[-1] / close[-(n63 + 1)]) +
              0.2 * (close[-1] / close[-(n126 + 1)]) +
              0.2 * (close[-1] / close[-(n189 + 1)]) +
              0.2 * (close[-1] / close[-(n252 + 1)]))
    return perf, float(perf_c), int(n)


def load_spy_close(cache_dir=None, asof=None):
    """SPY close as a pandas Series (index = date), truncated to `asof`.

    Needed for RS-line computations (price/SPY ratio, R² of the RS-line
    regression, RS momentum) — the window-perf dict alone can't build the line.
    """
    p = resolve_cache_file("SPY", "_1d.parquet", cache_dir)
    if p is None:
        return None
    spy = pd.read_parquet(p, columns=["Close"])
    if asof is not None:
        try:
            idx = pd.to_datetime(spy.index)
            spy = spy[idx <= pd.Timestamp(asof)]
        except Exception:
            pass
    close = pd.to_numeric(spy["Close"], errors="coerce").dropna()
    close = close[close > 0]
    return close if len(close) else None


def derive_csv_asof(csv_path, sample=None):
    """Find the trading day a MarketSurge CSV reflects by price-matching a sample
    of liquid tickers against the price cache (most common matching day).
    """
    from collections import Counter
    if sample is None:
        sample = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "XOM",
                  "JNJ", "WMT", "CAT", "DIS", "HD", "MCD", "PEP", "T", "VZ",
                  "CSCO", "PFE", "MRK", "BA", "GE", "UNH", "V", "MA", "ORCL",
                  "CRM", "NFLX", "TSLA"]
    df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    by_sym = {r["Symbol"]: r for _, r in df.iterrows()}
    hits = Counter()
    for s in sample:
        r = by_sym.get(s)
        if r is None or pd.isna(r.get("Current Price")):
            continue
        try:
            px = float(r["Current Price"])
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        fp = resolve_cache_file(s, "_1d.parquet")
        if fp is None:
            continue
        try:
            d = pd.read_parquet(fp, columns=["Close"])
            close = pd.to_numeric(d["Close"], errors="coerce").astype(float)
            diff = (close - px).abs() / px
            cand = diff[diff < 0.005]
            if len(cand):
                hits[str(pd.to_datetime(cand.index)[-1].date())] += 1
        except Exception:
            continue
    return hits.most_common(1)[0][0] if hits else ""


# ──────────────────────────────────────────────────────────────────────────────
# Price / volume feature extraction (RS + A/D in one pass)
# ──────────────────────────────────────────────────────────────────────────────
def _window_stats(prices, vols, highs, lows, w):
    """Accumulation/distribution window statistics (deterministic)."""
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


def _rs_line_series(cdf, spy_close, n_max=260):
    """Aligned price/SPY ratio (RS line) over the trailing `n_max` days.

    Both series are truncated to the same as-of day by the caller.  Aligns on
    the DATE INDEX (tickers can have gaps/halts), keeping the last `n_max`
    shared trading days.  Returns a numpy array or None if < 60 aligned days.
    """
    if spy_close is None or len(spy_close) < 60:
        return None
    dates = pd.to_datetime(cdf.index)
    t = pd.Series(pd.to_numeric(cdf["Close"], errors="coerce").values, index=dates)
    s = spy_close.astype(float)
    s.index = pd.to_datetime(s.index)
    common = t.index.intersection(s.index)
    if len(common) < 60:
        return None
    common = common[-n_max:]
    t = t.loc[common].values
    s = s.loc[common].values
    ok = (s > 0) & np.isfinite(s) & (t > 0) & np.isfinite(t)
    if ok.sum() < 60:
        return None
    rs = t / np.where(ok, s, np.nan)
    return rs[np.isfinite(rs)]


def extract_price_features(ticker, spy_perf, cache_dir=None, asof=None, spy_close=None):
    """One ticker -> RS + A/D feature dict (or None if unusable).

    `asof` (YYYY-MM-DD) truncates the price history to that day, for
    cross-week validation against older MarketSurge snapshots.
    `spy_close` (pd.Series indexed by date, truncated to `asof`) enables the
    RS-line features: relative volatility, vol-adjusted relative performance
    (Moreira-Muir Sharpe-style), StockCharts R² of the RS-line regression, and
    the RRG JdK RS-Momentum analog (RS-line % change).
    """
    p_path = resolve_cache_file(ticker, "_1d.parquet", cache_dir)
    if p_path is None:
        return None
    try:
        cdf = pd.read_parquet(p_path)
    except Exception:
        return None
    if asof is not None:
        try:
            idx = pd.to_datetime(cdf.index)
            cdf = cdf[idx <= pd.Timestamp(asof)]
        except Exception:
            return None
    if cdf.empty or len(cdf) < 30:
        return None

    prices = pd.to_numeric(cdf["Close"], errors="coerce").values
    vols = pd.to_numeric(cdf["Volume"], errors="coerce").values
    if "High" in cdf.columns and "Low" in cdf.columns:
        highs = pd.to_numeric(cdf["High"], errors="coerce").values
        lows = pd.to_numeric(cdf["Low"], errors="coerce").values
    else:
        highs = lows = None

    valid = ~np.isnan(prices) & ~np.isnan(vols) & (prices > 0)
    if highs is not None and lows is not None:
        valid &= ~np.isnan(highs) & ~np.isnan(lows)
    prices, vols = prices[valid], vols[valid]
    if highs is not None and lows is not None:
        highs, lows = highs[valid], lows[valid]
    if len(prices) < 30:
        return None

    latest = float(prices[-1])
    rec = {"Ticker": str(ticker).strip(), "Latest_Price": round(latest, 2),
           "Hist_Days": len(prices)}

    # ---- RS-line features (price/SPY ratio) — TradingView/StockCharts/RRG ----
    # DIAGNOSTIC ONLY: computed so future RS research can reuse them without
    # re-reading parquets.  NO production formula consumes RS_RSq_*/RS_Mom_*/
    # RS_RSI_14/RS_RSMA_20D/RelVol_*/SharpeRel_*/RS_Ratio_Now — an ablation
    # showed none beat the production dual-momentum RS on both weeks.
    rs_line = _rs_line_series(cdf, spy_close)
    if rs_line is not None and len(rs_line) >= 60:
        # realized vol of stock vs SPY over each RS window (Moreira-Muir
        # vol-management / Sharpe-style momentum uses return / vol)
        log_rs = np.log(rs_line)
        # R² of the linear regression of log(RS line) over 65/126 days — the
        # StockCharts 'R-squared adjustment': a noisy RS line (low R²) is
        # penalised relative to a clean trend.
        for wd, tag in ((65, "65D"), (126, "126D"), (250, "250D")):
            if len(log_rs) >= wd:
                seg = log_rs[-wd:]
                x = np.arange(wd)
                slope, intercept = np.polyfit(x, seg, 1)
                fit = intercept + slope * x
                ss_res = float(np.sum((seg - fit) ** 2))
                ss_tot = float(np.sum((seg - seg.mean()) ** 2))
                rec[f"RS_RSq_{tag}"] = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
        # RS momentum (RRG JdK RS-Momentum analog): % change of the RS line
        # over 20/65 trading days — the second RRG axis.
        for wd, tag in ((21, "20D"), (66, "65D")):
            if len(rs_line) > wd:
                rec[f"RS_Mom_{tag}"] = (rs_line[-1] / rs_line[-(wd + 1)] - 1.0) * 100.0
        rec["RS_Ratio_Now"] = float(rs_line[-1])
        # TradingView rs()/RSMA crossover: distance of the RS line from its own
        # 20-day moving average (the classic TV relative-strength indicator).
        # Convention: current RS line vs the mean of the PRIOR 20 days (excludes
        # today, avoiding self-correlation).
        if len(rs_line) >= 21:
            rec["RS_RSMA_20D"] = (rs_line[-1] / np.mean(rs_line[-21:-1]) - 1.0) * 100.0
        # TradingView rs()/RSI-of-RS: RSI(14) of the RS line (Wilder)
        d = np.diff(rs_line)
        if len(d) >= 15:
            up = np.clip(d, 0, None)
            dn = np.clip(-d, 0, None)
            avg_up = up[:14].mean()
            avg_dn = dn[:14].mean()
            for i in range(14, len(d)):
                avg_up = (avg_up * 13 + up[i]) / 14.0
                avg_dn = (avg_dn * 13 + dn[i]) / 14.0
            rec["RS_RSI_14"] = 100.0 - 100.0 / (1.0 + avg_up / max(avg_dn, 1e-12))

    # ---- RS: absolute returns + relative performance vs SPY per window ----
    for label, days in RS_WINDOWS.items():
        if len(prices) > days:
            perf = latest / prices[-(days + 1)]
            rec[f"AbsRet_{label}"] = (perf - 1) * 100.0
            rec[f"RelPerf_{label}"] = perf / spy_perf[label]
        else:
            rec[f"AbsRet_{label}"] = np.nan
            rec[f"RelPerf_{label}"] = np.nan

    # ---- volatility + vol-adjusted relative performance per RS window ----
    if spy_close is not None and len(spy_close) > 30:
        sc = spy_close.astype(float).values
        for label, days in RS_WINDOWS.items():
            if len(prices) > days and len(sc) > days:
                tr = np.diff(prices[-(days + 1):]) / np.where(prices[-(days + 1):-1] == 0, 1.0, prices[-(days + 1):-1])
                sr = np.diff(sc[-(days + 1):]) / np.where(sc[-(days + 1):-1] == 0, 1.0, sc[-(days + 1):-1])
                vol_t = float(np.std(tr)) * np.sqrt(252.0) * 100.0
                vol_s = float(np.std(sr)) * np.sqrt(252.0) * 100.0
                if vol_s > 1e-6 and np.isfinite(vol_s) and np.isfinite(vol_t):
                    relvol = vol_t / vol_s
                    perf_w = latest / prices[-(days + 1)]
                    rec[f"RelVol_{label}"] = relvol
                    # vol-adjusted relative performance, kept in RATIO space (~1.0)
                    # like RelPerf so the sigmoid raw = X @ w * 100 stays on the
                    # same ~100 scale: 1 + (relperf - 1)/relvol (Moreira-Muir
                    # vol-management: high relative volatility discounts excess)
                    rec[f"SharpeRel_{label}"] = 1.0 + (perf_w / spy_perf[label] - 1.0) / relvol

    # current-production baseline RS (40/20/20/20 weighted vs SPY, sigmoid)
    n = len(prices)
    n63, n126, n189, n252 = min(n - 1, 63), min(n - 1, 126), min(n - 1, 189), min(n - 1, 252)
    perf_t = (0.4 * (prices[-1] / prices[-(n63 + 1)]) +
              0.2 * (prices[-1] / prices[-(n126 + 1)]) +
              0.2 * (prices[-1] / prices[-(n189 + 1)]) +
              0.2 * (prices[-1] / prices[-(n252 + 1)]))
    rec["_perf_t_baseline"] = perf_t

    # ---- A/D: multi-window accumulation stats ----
    if highs is not None and lows is not None:
        for w in AD_WINDOWS:
            rec.update(_window_stats(prices, vols, highs, lows, w))

        # current-production baseline A/D (65D CMF -> 0-99)
        if n >= 65:
            wsl = slice(-65, None)
            hl = highs[wsl] - lows[wsl]
            safe_hl = np.where(hl == 0, 1.0, hl)
            mf = np.where(hl != 0,
                          ((prices[wsl] - lows[wsl]) - (highs[wsl] - prices[wsl])) / safe_hl, 0.0)
            ad_ratio = np.sum(mf * vols[wsl]) / max(1.0, np.sum(vols[wsl]))
            rec["AD_baseline"] = max(0.0, min(99.0, 49.5 + ad_ratio * 49.5))
        else:
            rec["AD_baseline"] = np.nan

    # ---- trend / position features ----
    for ma in MA_WINDOWS:
        if n >= ma:
            rec[f"Dist_{ma}MA"] = (latest / np.mean(prices[-ma:]) - 1) * 100.0

    h52 = np.max(prices[-253:])
    rec["PctOff52WHigh"] = (h52 - latest) / h52 * 100.0 if h52 > 0 else 0.0

    # (OBV trend/divergence + volume z-score were tested in the research round
    # but hurt A/D on the matched-universe holdout — left out intentionally.)

    tail_n = min(n - 1, 250)
    if tail_n >= 20:
        pr = np.diff(prices[-(tail_n + 1):]) / np.where(prices[-(tail_n + 1):-1] == 0, 1.0,
                                                        prices[-(tail_n + 1):-1])
        vt = vols[-tail_n:]
        mv = max(1.0, np.mean(vt))
        vr = vt / mv
        up, dn = pr > 0, pr < 0
        rec["UpDayVolRatio"] = float(np.mean(vr[up])) if np.any(up) else 1.0
        rec["DnDayVolRatio"] = float(np.mean(vr[dn])) if np.any(dn) else 1.0
        if np.std(pr) > 0 and np.std(vr) > 0:
            rec["PriceVolCorr"] = float(np.corrcoef(pr, vr)[0, 1])
    for k, v in list(rec.items()):
        if isinstance(v, float) and not np.isfinite(v):
            rec[k] = np.nan
    return rec


def extract_price_features_bulk(tickers, spy_perf, max_workers=MAX_WORKERS, asof=None, spy_close=None):
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(
            lambda t: extract_price_features(t, spy_perf, asof=asof, spy_close=spy_close), tickers))
    return pd.DataFrame([r for r in results if r is not None])


# ──────────────────────────────────────────────────────────────────────────────
# Fundamentals feature extraction (EPS / SMR / group identity)
# ──────────────────────────────────────────────────────────────────────────────
def _series_from_label(block, labels):
    """block[label] = {date_str: value}; returns (dates_desc, values) of first match."""
    if not isinstance(block, dict):
        return None
    for lbl in labels:
        col = block.get(lbl)
        if isinstance(col, dict) and col:
            dates = sorted(col.keys(), reverse=True)
            vals, ds = [], []
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


# info.* fields that are stored as fractions (0.25 = 25%) and must be
# converted to percent for consistency with the rest of the feature set.
# (returnOnEquity is NOT here: ROE is extracted separately in
# extract_fund_features with its own *100 conversion and info fallback.)
_INFO_PCT_FIELDS = {"profitMargins", "revenueGrowth", "earningsQuarterlyGrowth",
                    "returnOnAssets", "grossMargins", "operatingMargins",
                    "earningsGrowth", "heldPercentInstitutions",
                    "heldPercentInsiders"}


def _info_num(info, key):
    v = info.get(key)
    if v is None:
        return np.nan
    try:
        return float(v) * 100.0 if key in _INFO_PCT_FIELDS else float(v)
    except (TypeError, ValueError):
        return np.nan


def extract_fund_features(ticker, cache_dir=None):
    """One ticker -> fundamentals feature dict (EPS/SMR/group)."""
    f_path = resolve_cache_file(ticker, "_fund.json", cache_dir)
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

    # ── EPS short-term growth (YoY, 4 quarters back) + acceleration ──
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

    # ── EPS long-term growth (recent-weighted annual YoY) ──
    lt_growth = np.nan
    if eps_a:
        _, vals = eps_a
        sum_g, sum_w = 0.0, 0.0
        for j in range(min(len(vals) - 1, 5)):
            if abs(vals[j + 1]) > 1e-9:
                gv = (vals[j] - vals[j + 1]) / abs(vals[j + 1]) * 100.0
                sum_g += gv * (5 - j)
                sum_w += (5 - j)
        if sum_w > 0:
            lt_growth = sum_g / sum_w
    rec["EPS_LT_Growth"] = lt_growth

    # ── EPS negative-quarter ratio + stability (CV of YoY growth) ──
    neg_q, cnt_q = 0, 0
    yoy_vals = []
    if eps_q:
        _, vals = eps_q
        for j in range(min(len(vals) - 4, 4)):
            if abs(vals[j + 4]) > 1e-9:
                gv = (vals[j] - vals[j + 4]) / abs(vals[j + 4]) * 100.0
                cnt_q += 1
                if gv < 0:
                    neg_q += 1
                yoy_vals.append(gv)
    rec["EPS_NegQRatio"] = (neg_q / cnt_q) if cnt_q > 0 else 0.0
    # stability: CV of YoY growth.  yfinance's quarterly window is only ~5
    # quarters, so fall back to annual EPS YoY growths when < 3 quarterly values.
    stab_vals = yoy_vals
    if len(stab_vals) < 3 and eps_a:
        _, vals = eps_a
        a_yoy = []
        for j in range(min(len(vals) - 1, 4)):
            if abs(vals[j + 1]) > 1e-9:
                a_yoy.append((vals[j] - vals[j + 1]) / abs(vals[j + 1]) * 100.0)
        stab_vals = a_yoy
    rec["EPS_StabilityCV"] = (float(np.std(stab_vals) / max(1e-9, abs(np.mean(stab_vals))))
                              if len(stab_vals) >= 3 else np.nan)

    # ── Analyst blocks: surprise, beat rate, estimate revisions ──
    eh = fund.get("earnings_history")
    surprises, beats = [], 0
    if isinstance(eh, dict):
        for k, v in eh.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and v.get("epsActual") is not None:
                s = v.get("surprisePercent")
                if isinstance(s, (int, float)):
                    surprises.append(float(s))
                if v.get("epsDifference") is not None and float(v.get("epsDifference")) > 0:
                    beats += 1
    rec["EpsSurpriseMean"] = float(np.mean(surprises)) if surprises else np.nan
    rec["EpsBeatRate"] = beats / len(surprises) if surprises else np.nan

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

    # ── Sales growth (short + long term) and acceleration ──
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
                sum_g += gv * (5 - j)
                sum_w += (5 - j)
        if sum_w > 0:
            sales_lt = sum_g / sum_w
    rec["Sales_LT_Growth"] = sales_lt

    # ── Margin level + trend (quarterly net margin) ──
    margin_now = margin_trend = np.nan
    if rev_q and ni_q:
        rdates, rvals = rev_q
        ndates, nvals = ni_q
        rmap, nmap = dict(zip(rdates, rvals)), dict(zip(ndates, nvals))
        margins = [nmap[d] / rmap[d] * 100.0 for d in rdates
                   if d in nmap and abs(rmap[d]) > 1e-6]
        if margins:
            margin_now = margins[0]
            if len(margins) >= 3:
                margin_trend = float(np.mean(margins[:2]) - np.mean(margins[-2:]))
    rec["Margin_Now"] = margin_now
    rec["Margin_Trend"] = margin_trend

    # ── Gross margin level + trend (quarterly gross profit / revenue) — the
    #    SMR 'profit margins' pillar benefits from a second margin series with
    #    its own trend direction (research-backed: margin trend > level). ──
    gp_q = _series_from_label(fund.get("income_q"), ["Gross Profit"])
    gm_now = gm_trend = np.nan
    if gp_q and rev_q:
        gdates, gvals = gp_q
        rdates2, rvals2 = rev_q
        gmap = dict(zip(gdates, gvals))
        rmap2 = dict(zip(rdates2, rvals2))
        gms = [gmap[d] / rmap2[d] * 100.0 for d in gdates
               if d in rmap2 and abs(rmap2[d]) > 1e-6 and abs(gmap[d]) > 1e-6]
        if gms:
            gm_now = gms[0]
            if len(gms) >= 3:
                gm_trend = float(np.mean(gms[:2]) - np.mean(gms[-2:]))
    rec["GrossMargin_Now"] = gm_now
    rec["GrossMargin_Trend"] = gm_trend

    # (EPS/Sales acceleration proxies were tested in the research round but were
    # neutral-to-worse for both ratings — left out intentionally.)

    # ── ROE (info preferred, NI/equity fallback) ──
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

    for k, out_k in (("profitMargins", "Info_ProfitMargin"),
                     ("revenueGrowth", "Info_RevGrowth"),
                     ("earningsQuarterlyGrowth", "Info_EPSQGrowth"),
                     ("returnOnAssets", "Info_ROA"),
                     ("grossMargins", "Info_GrossMargin"),
                     ("operatingMargins", "Info_OpMargin"),
                     ("earningsGrowth", "Info_EarningsGrowth"),
                     ("heldPercentInstitutions", "Info_InstHeld"),
                     ("heldPercentInsiders", "Info_InsiderHeld"),
                     ("beta", "Info_Beta"),
                     ("debtToEquity", "Info_DebtEquity"),
                     ("currentRatio", "Info_CurrentRatio"),
                     ("quickRatio", "Info_QuickRatio"),
                     ("priceToBook", "Info_PriceBook"),
                     ("totalCashPerShare", "Info_TotalCashPS"),
                     ("forwardPE", "Info_FwdPE"),
                     ("numberOfAnalystOpinions", "Info_NumAnalysts")):
        rec[out_k] = _info_num(info, k)

    # ── Cash-flow yields and analyst target upside (fund-json only) ──
    mc = _info_num(info, "marketCap")
    fcf = _info_num(info, "freeCashflow")
    ocf = _info_num(info, "operatingCashflow")
    rec["Info_FCFYield"] = (fcf / mc * 100.0) if (mc and mc > 0 and np.isfinite(fcf)) else np.nan
    rec["Info_OCFYield"] = (ocf / mc * 100.0) if (mc and mc > 0 and np.isfinite(ocf)) else np.nan
    px = _info_num(info, "currentPrice")
    if not np.isfinite(px):
        px = _info_num(info, "regularMarketPrice")
    tgt = _info_num(info, "targetMeanPrice")
    rec["Info_TargetUpside"] = ((tgt / px - 1.0) * 100.0) if (px and px > 0 and np.isfinite(tgt)) else np.nan

    # ── Institutional footprint (proxy for fund accumulation) ──
    ih = fund.get("institutional_holders")
    inst_pct = inst_chg = np.nan
    if isinstance(ih, dict):
        pcts, chgs = [], []
        for k, v in ih.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict):
                if v.get("pctHeld") is not None:
                    pcts.append(float(v["pctHeld"]))
                if v.get("pctChange") is not None:
                    chgs.append(float(v["pctChange"]))
        if pcts:
            inst_pct = sum(pcts[:5]) * 100.0
        if chgs:
            inst_chg = float(np.mean(chgs)) * 100.0
    rec["InstTop5Pct"] = inst_pct
    rec["InstAvgChg"] = inst_chg

    rec["Industry"] = info.get("industry")  # fallback only; mapping file is authoritative
    rec["Sector"] = info.get("sector")
    for k, v in list(rec.items()):
        if isinstance(v, float) and not np.isfinite(v):
            rec[k] = np.nan
    return rec


def extract_fund_features_bulk(tickers, max_workers=MAX_WORKERS):
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(extract_fund_features, tickers))
    return pd.DataFrame([r for r in results if r is not None])


# ──────────────────────────────────────────────────────────────────────────────
# Letter calibration by frequency matching (no classifiers)
# ──────────────────────────────────────────────────────────────────────────────
def fit_letter_map(train_scores, train_labels, letters_ordered):
    """Learn the grade mix of the ground truth on the train sample.

    Returns (counts, cum_top) where cum_top[g] is the cumulative fraction of
    grades from the best grade down through g.  Assignment: a score whose
    percentile (0-100, higher = better) in the train distribution is >= cum_top[g]
    gets grade g.  Deterministic, preserves the observed grade mix exactly.
    """
    train_scores = np.asarray(train_scores, dtype=float)
    labels = np.asarray(train_labels)
    valid = ~np.isnan(train_scores)
    labels = labels[valid]
    counts = {g: 0 for g in letters_ordered}
    for g in labels:
        if g in counts:
            counts[g] += 1
    total = sum(counts.values())
    cum_top = {}
    running = 0.0
    for g in letters_ordered:  # best grade first
        running += counts[g] / total
        cum_top[g] = running
    return counts, cum_top


def apply_letter_map(test_scores, train_scores, train_labels, letters_ordered):
    """Assign letters to test scores using the train grade mix + score distribution.

    No leakage: the test score's percentile is computed against the TRAIN score
    distribution; the grade thresholds come from the TRAIN grade mix.
    """
    _, cum_top = fit_letter_map(train_scores, train_labels, letters_ordered)
    test_scores = np.asarray(test_scores, dtype=float)
    pct = transfer_pct_rank(train_scores, test_scores)  # 1..99, higher = better
    res = np.full(len(test_scores), letters_ordered[-1], dtype=object)
    for i, p in enumerate(pct):
        if np.isnan(p):
            continue
        # grade g owns the top `cum_top[g]` share of the distribution, so a score
        # at fraction p (0-1, higher = better) gets g when p >= 1 - cum_top[g]
        for g in letters_ordered:
            if p / 100.0 >= 1.0 - cum_top[g]:
                res[i] = g
                break
    return res


def pct_from_ref(raw_vals, score_ref):
    """Map raw scores to the 1-99 percentile scale using a stored reference
    distribution (sorted train raw scores) — for production single/batch scoring.
    """
    score_ref = np.asarray(score_ref, dtype=float)
    n = len(score_ref)
    raw_vals = np.asarray(raw_vals, dtype=float)
    out = np.full(len(raw_vals), np.nan)
    ok = ~np.isnan(raw_vals)
    if n == 0 or not np.any(ok):
        return out
    idx = np.searchsorted(score_ref, raw_vals[ok], side="right")
    out[ok] = np.clip(idx / n * 99.0, 1, 99)
    return out


def letter_from_pct(pcts, letters_ordered, cum_top):
    """Assign letters from 1-99 percentiles given the trained grade mix."""
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


def letters_to_num(labels, grade_num_map):
    """Map a letter series to its numeric scale (for correlation metrics)."""
    return np.array([grade_num_map.get(str(g), np.nan) for g in labels], dtype=float)


def letter_accuracy(y_true, y_pred, letters_ordered, grade_num_map):
    """Exact, +/-1-step and rank-correlation metrics for letter predictions."""
    y_true = np.asarray([str(g) for g in y_true])
    y_pred = np.asarray([str(g) for g in y_pred])
    ok = np.array([t in grade_num_map and p in grade_num_map for t, p in zip(y_true, y_pred)])
    if not np.any(ok):
        return None
    yt = y_true[ok]
    yp = y_pred[ok]
    exact = float(np.mean(yt == yp) * 100)
    nt = letters_to_num(yt, grade_num_map)
    npd = letters_to_num(yp, grade_num_map)
    within1 = float(np.mean(np.abs(nt - npd) <= 1) * 100)
    return {
        "Exact Acc%": round(exact, 1),
        "+/-1 Acc%": round(within1, 1),
        "Corr": round(corr(nt, npd), 4),
        "MAE(grade pts)": round(mae(nt, npd), 2),
    }
