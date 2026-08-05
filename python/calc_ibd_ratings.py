#!/usr/bin/env python3
"""
calc_ibd_ratings.py

Python implementation of the IBD-style Ratings Scanner from drw_ratings_scanner.pine.
Computes RS Rating, EPS Rating, A/D Rating, SMR Rating, and Composite Rating
using daily OHLCV data and fundamental data (EPS, ROE) from yfinance.

All formulas match the Pine Script exactly, using the same sigmoid function,
weightings, penalty logic, and composite formula coefficients.

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
# RS RATING (1-99) — sigmoid-based, no seed data needed
# ──────────────────────────────────────────────────────────────────────────────

def _f_sigmoid(score):
    """Pine Script f_sigmoid: d = score - 100, 50 + 49 * d/(|d|+22), clamped [1,99]."""
    d = score - 100.0
    return max(1.0, min(99.0, 50.0 + 49.0 * (d / (abs(d) + 22.0))))


def calc_rs_ratings(df, spy_df):
    """
    Calculate RS Rating (1-99), RS 3M, and RS 6M for each bar in `df`.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV data with datetime index, columns including 'Close'.
    spy_df : pd.DataFrame
        SPY/S&P 500 OHLCV data with datetime index, aligned to same dates.
        Must have 'Close' column.

    Returns
    -------
    pd.DataFrame with columns: rs_rating, rs_rating_3m, rs_rating_6m
    """
    close = df['Close'].values.astype(float)
    spy_close = spy_df['Close'].reindex(df.index).ffill().bfill().values.astype(float)

    n = len(close)
    rs_rating = np.full(n, np.nan)
    rs_3m = np.full(n, np.nan)
    rs_6m = np.full(n, np.nan)

    for i in range(n):
        n63 = min(i, 63)
        n126 = min(i, 126)
        n189 = min(i, 189)
        n252 = min(i, 252)

        i63 = i - n63
        i126 = i - n126
        i189 = i - n189
        i252 = i - n252

        # Stock performance (weighted: 40/20/20/20)
        perf_t = (0.4 * (close[i] / close[i63]) +
                  0.2 * (close[i] / close[i126]) +
                  0.2 * (close[i] / close[i189]) +
                  0.2 * (close[i] / close[i252]))

        # Benchmark performance (same weights)
        perf_c = (0.4 * (spy_close[i] / spy_close[i63]) +
                  0.2 * (spy_close[i] / spy_close[i126]) +
                  0.2 * (spy_close[i] / spy_close[i189]) +
                  0.2 * (spy_close[i] / spy_close[i252]))

        total_rs_score = (perf_t / perf_c) * 100.0 if perf_c > 0 else 100.0

        # 3M and 6M scores
        score_3m = ((close[i] / close[i63]) /
                    (spy_close[i] / spy_close[i63]) * 100.0
                    if spy_close[i63] > 0 else 100.0)
        score_6m = ((close[i] / close[i126]) /
                    (spy_close[i] / spy_close[i126]) * 100.0
                    if spy_close[i126] > 0 else 100.0)

        rs_rating[i] = _f_sigmoid(total_rs_score)
        rs_3m[i] = _f_sigmoid(score_3m)
        rs_6m[i] = _f_sigmoid(score_6m)

    return pd.DataFrame({
        'rs_rating': rs_rating,
        'rs_rating_3m': rs_3m,
        'rs_rating_6m': rs_6m,
    }, index=df.index)


# ──────────────────────────────────────────────────────────────────────────────
# % OFF 52-WEEK HIGH
# ──────────────────────────────────────────────────────────────────────────────

def calc_pct_off_52w_high(df):
    """Percentage below 52-week (252-day) high: (high52w - close) / high52w * 100."""
    high = df['High'].values.astype(float)
    close = df['Close'].values.astype(float)
    n = len(close)

    pct_off = np.full(n, np.nan)
    for i in range(n):
        start = max(0, i - 252)
        h52 = np.nanmax(high[start:i + 1])
        if h52 > 0:
            pct_off[i] = (h52 - close[i]) / h52 * 100.0
        else:
            pct_off[i] = 0.0

    return pd.Series(pct_off, index=df.index, name='pct_off_52w_high')


# ──────────────────────────────────────────────────────────────────────────────
# A/D RATING (0-99) — Accumulation/Distribution, from price & volume only
# ──────────────────────────────────────────────────────────────────────────────

def calc_ad_rating(df):
    """
    Money Flow-based A/D Rating (0-99) over 65-day window.
    Matches drw_ratings_scanner.pine exactly.
    """
    high = df['High'].values.astype(float)
    low = df['Low'].values.astype(float)
    close = df['Close'].values.astype(float)
    volume = df['Volume'].values.astype(float)

    n = len(close)
    ad_rating = np.full(n, np.nan)

    for i in range(65, n):
        # Money flow multiplier per bar
        hl_diff = high[i - 65:i + 1] - low[i - 65:i + 1]
        safe_hl = np.where(hl_diff == 0, 1.0, hl_diff)
        mf = np.where(hl_diff != 0,
                      ((close[i - 65:i + 1] - low[i - 65:i + 1]) -
                       (high[i - 65:i + 1] - close[i - 65:i + 1])) / safe_hl,
                      0.0)

        sum_mf_vol = np.sum(mf * volume[i - 65:i + 1])
        sum_vol = np.sum(volume[i - 65:i + 1])

        ad_ratio_val = sum_mf_vol / sum_vol if sum_vol != 0 else 0.0
        ad_rating[i] = max(0.0, min(99.0, 49.5 + ad_ratio_val * 49.5))

    # Fill first 65 bars
    if n >= 66:
        ad_rating[:65] = ad_rating[65]

    return pd.Series(ad_rating, index=df.index, name='ad_rating')


# ──────────────────────────────────────────────────────────────────────────────
# EPS RATING (1-99) — from EPS history arrays
# ──────────────────────────────────────────────────────────────────────────────

def calc_eps_rating(fy_eps, fq_eps, roe_val):
    """
    Calculate EPS Rating (1-99) from annual and quarterly EPS data.

    Parameters
    ----------
    fy_eps : list of float (length >= 2)
        Annual diluted EPS values, most recent first [FY0, FY-1, FY-2, ...].
        From yfinance: ticker.financials.loc['Diluted EPS'].
    fq_eps : list of float (length >= 5)
        Quarterly diluted EPS values, most recent first [Q0, Q-1, Q-2, ...].
        From yfinance: ticker.quarterly_financials.loc['Diluted EPS'].
    roe_val : float or None
        Return on Equity from the most recent quarter.
        From yfinance: ticker.info['returnOnEquity'] or derived from
        net income / total equity.

    Returns
    -------
    int : EPS Rating (1-99), or 1 if insufficient data.
    """
    # ── Short-term growth (QoQ YoY) ──
    n_fq = len(fq_eps)
    q0g = None
    q1g = None
    if n_fq > 4:
        if abs(fq_eps[4]) > 0:
            q0g = (fq_eps[0] - fq_eps[4]) / abs(fq_eps[4]) * 100.0
    if n_fq > 5:
        if abs(fq_eps[5]) > 0:
            q1g = (fq_eps[1] - fq_eps[5]) / abs(fq_eps[5]) * 100.0

    st_growth = None
    if q0g is not None:
        st_growth = q0g * 0.65 + q1g * 0.35 if q1g is not None else q0g

    # ── Long-term growth (annual, weighted recent) ──
    n_fy = len(fy_eps)
    lt_growth = None
    sum_g = 0.0
    sum_w = 0.0
    for j in range(min(n_fy - 1, 5)):
        if abs(fy_eps[j + 1]) > 0:
            gv = (fy_eps[j] - fy_eps[j + 1]) / abs(fy_eps[j + 1]) * 100.0
            w = 5 - j
            sum_g += gv * w
            sum_w += w

    if sum_w > 0:
        lt_growth = sum_g / sum_w

    # ── EPS acceleration ──
    eps_accel = q0g - q1g if (q0g is not None and q1g is not None) else 0.0

    # ── Blended growth rate ──
    if st_growth is None and lt_growth is None:
        blended = 0.0
    elif st_growth is None:
        blended = lt_growth
    elif lt_growth is None:
        blended = st_growth
    else:
        blended = st_growth * 0.50 + lt_growth * 0.35 + eps_accel * 0.15

    raw_eps_base = 50.0 + 49.0 * (blended / (abs(blended) + 40.0))

    # ── Negative quarter ratio ──
    neg_q = 0.0
    cnt_q = 0.0
    for j in range(min(n_fq - 4, 4)):
        if abs(fq_eps[j + 4]) > 0:
            gv = (fq_eps[j] - fq_eps[j + 4]) / abs(fq_eps[j + 4]) * 100.0
            cnt_q += 1
            if gv < 0:
                neg_q += 1
    neg_ratio = neg_q / cnt_q if cnt_q > 0 else 0.0

    # ── Penalties ──
    roe_pen = 0.0
    if roe_val is not None and roe_val < 0:
        roe_pen = min(22.0, abs(roe_val) * 0.05 + 5.0)

    lt_neg_pen = 0.0
    if lt_growth is not None and lt_growth < 0:
        lt_neg_pen = min(15.0, abs(lt_growth) * 0.4)

    eps = raw_eps_base - roe_pen - lt_neg_pen - neg_ratio * 10.0
    return max(1, min(99, round(eps)))


# ──────────────────────────────────────────────────────────────────────────────
# SMR RATING — ROE-driven, single-pillar (margin/sales not available via yfinance)
# ──────────────────────────────────────────────────────────────────────────────

def calc_smr_rating(roe_val):
    """
    SMR Score (0-99) and Grade (A-E), driven solely by ROE.
    Matches the Pine Script simplified approach.
    """
    if roe_val is None:
        roe_val = 15.0

    score = max(0.0, min(99.0, 50.0 + 49.0 * (roe_val / (abs(roe_val) + 17.0))))

    if score >= 80:
        grade = 'A'
    elif score >= 65:
        grade = 'B'
    elif score >= 50:
        grade = 'C'
    elif score >= 35:
        grade = 'D'
    else:
        grade = 'E'

    return score, grade


# ──────────────────────────────────────────────────────────────────────────────
# COMPOSITE RATING (1-99)
# ──────────────────────────────────────────────────────────────────────────────

def calc_composite_rating(rs_rating, eps_rating, smr_score, ad_rating):
    """
    Composite Rating = -15.94 + 0.5794*RS + 0.3766*EPS + 0.2166*SMR + 1.4080*AD_num
    where AD_num = 1.0 + (ad_rating / 99.0) * 12.0
    """
    ad_num = 1.0 + (ad_rating / 99.0) * 12.0
    comp_raw = (-15.94 + 0.5794 * rs_rating + 0.3766 * eps_rating +
                0.2166 * smr_score + 1.4080 * ad_num)
    return max(1, min(99, round(comp_raw)))


# ──────────────────────────────────────────────────────────────────────────────
# ALL-IN-ONE CALCULATION
# ──────────────────────────────────────────────────────────────────────────────

def calc_all_ratings(df, spy_df, fy_eps=None, fq_eps=None, roe_val=None):
    """
    Compute all IBD-style ratings for the last bar of `df`.

    Parameters
    ----------
    df : pd.DataFrame
        Daily OHLCV data (from ticker_cache), with columns
        'Open', 'High', 'Low', 'Close', 'Volume'.
    spy_df : pd.DataFrame
        SPY daily OHLCV data, aligned.
    fy_eps : list of float, optional
        Annual diluted EPS, most recent first.
    fq_eps : list of float, optional
        Quarterly diluted EPS, most recent first.
    roe_val : float, optional
        Return on Equity from most recent quarter.

    Returns
    -------
    dict with keys: ticker, rs_rating, rs_3m, rs_6m, pct_off_52w_high,
    eps_rating, smr_score, smr_grade, ad_rating, comp_rating
    """
    # RS Ratings
    rs = calc_rs_ratings(df, spy_df)
    rs_val = rs['rs_rating'].iloc[-1]
    rs_3m = rs['rs_rating_3m'].iloc[-1]
    rs_6m = rs['rs_rating_6m'].iloc[-1]

    # % Off 52W High
    pct_off = calc_pct_off_52w_high(df).iloc[-1]

    # A/D Rating
    ad = calc_ad_rating(df).iloc[-1]

    # EPS Rating
    if fy_eps and fq_eps and len(fy_eps) >= 2 and len(fq_eps) >= 5:
        eps = calc_eps_rating(fy_eps, fq_eps, roe_val)
    else:
        eps = 1  # minimum if insufficient data

    # SMR Rating
    smr_score, smr_grade = calc_smr_rating(roe_val)

    # Composite Rating
    comp = calc_composite_rating(rs_val, eps, smr_score, ad)

    return {
        'rs_rating': round(rs_val, 1),
        'rs_3m': round(rs_3m, 1),
        'rs_6m': round(rs_6m, 1),
        'pct_off_52w_high': round(pct_off, 2),
        'eps_rating': eps,
        'smr_score': round(smr_score, 1),
        'smr_grade': smr_grade,
        'ad_rating': round(ad, 1),
        'comp_rating': comp,
    }


# ──────────────────────────────────────────────────────────────────────────────
# BATCH RS RATING (vectorized for speed on many tickers with SPY pre-computed)
# ──────────────────────────────────────────────────────────────────────────────

def calc_rs_rating_snapshot(close_series, spy_close_series):
    """
    Calculate the RS Rating for the LAST bar given two pandas Series
    of closing prices (stock and SPY, aligned by index).

    Returns single float: the RS Rating (1-99) at the last bar.
    """
    close = close_series.values.astype(float)
    spy = spy_close_series.values.astype(float)

    n = len(close)
    if n < 4:
        return np.nan

    n63 = min(n - 1, 63)
    n126 = min(n - 1, 126)
    n189 = min(n - 1, 189)
    n252 = min(n - 1, 252)

    try:
        perf_t = (0.4 * (close[-1] / close[-(n63 + 1)]) +
                  0.2 * (close[-1] / close[-(n126 + 1)]) +
                  0.2 * (close[-1] / close[-(n189 + 1)]) +
                  0.2 * (close[-1] / close[-(n252 + 1)]))

        perf_c = (0.4 * (spy[-1] / spy[-(n63 + 1)]) +
                  0.2 * (spy[-1] / spy[-(n126 + 1)]) +
                  0.2 * (spy[-1] / spy[-(n189 + 1)]) +
                  0.2 * (spy[-1] / spy[-(n252 + 1)]))

        score = (perf_t / perf_c) * 100.0 if perf_c > 0 else 100.0
        return _f_sigmoid(score)
    except (IndexError, ZeroDivisionError):
        return np.nan


def calc_ad_rating_snapshot(df):
    """A/D Rating for the single last bar from a DataFrame with OHLCV."""
    high = df['High'].values.astype(float)
    low = df['Low'].values.astype(float)
    close = df['Close'].values.astype(float)
    volume = df['Volume'].values.astype(float)

    n = len(close)
    if n < 65:
        return np.nan

    w = slice(-65, None)
    hl_diff = high[w] - low[w]
    safe_hl = np.where(hl_diff == 0, 1.0, hl_diff)
    mf = np.where(hl_diff != 0,
                  ((close[w] - low[w]) - (high[w] - close[w])) / safe_hl,
                  0.0)

    sum_mf_vol = np.sum(mf * volume[w])
    sum_vol = np.sum(volume[w])

    ad_ratio_val = sum_mf_vol / sum_vol if sum_vol != 0 else 0.0
    return max(0.0, min(99.0, 49.5 + ad_ratio_val * 49.5))


def calc_pct_off_52w_high_snapshot(df):
    """% Off 52W High for the last bar."""
    high = df['High'].values.astype(float)
    close = df['Close'].values.astype(float)

    n = len(close)
    if n < 1:
        return np.nan

    start = max(0, n - 253)  # 252 + 1 for inclusive
    h52 = np.nanmax(high[start:n])
    if h52 > 0:
        return (h52 - close[-1]) / h52 * 100.0
    return 0.0


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
                    if v is not None:
                        try:
                            vals.append(float(v))
                        except (ValueError, TypeError):
                            pass
                if len(vals) >= 2:
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
                    if v is not None:
                        try:
                            vals.append(float(v))
                        except (ValueError, TypeError):
                            pass
                if len(vals) >= 2:
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
