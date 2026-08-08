#!/usr/bin/env python3
"""
build_daily_screener.py

Builds a combined daily screener dataset that mirrors the IBD MarketSurge column
schema (IBD/marketsurge.csv, 158 columns) but computed from local cached data:

  - price / volume      : ticker_cache/<T>_1d.parquet   (daily OHLCV, Date index)
  - fundamentals        : ticker_cache/<T>_fund.json    (yfinance full dump)
  - ratings             : python/calc_ibd_ratings.py     (RS / EPS / SMR / A/D / Composite / RS 3M / RS 6M)
  - industry + company  : IBD_data.txt                   (ticker -> industry name, description)

The output keeps the 158-column MarketSurge schema and appends ~60 extra columns
(EXTRA_COLUMNS) derived from fund.json data that MarketSurge has no column for
(analyst targets, EPS estimate trend/revisions, recommendation breakdown, insider
activity, institutional ownership, balance sheet, margins, cash flow, ESG risk,
forward estimates) plus price/volume extras computed from the parquet (RSI 14,
5/10/20-day returns, raw moving averages, relative volume, 52-wk position,
volatility).

Data-dependent gaps (not bugs): the fund cache only carries ~4 fiscal years of
EPS, so "EPS % Growth 5 Yr" (and its Pct Rnk) reports the longest span available
(4-yr CAGR when only 4 years of history exist) rather than a true 5-yr figure;
"Capital Expenditure (mil)" depends on yfinance info populating
capitalExpenditures, which it often does not.

The group-wide MarketSurge columns are computed in a universe post-pass
(apply_group_columns) using the industry from IBD_data.txt: Ind Group RS (1-99 =
mean RS rating of the group's members), Number of Stocks, Ind Mkt Val, #/% New
Highs & New Lows in Group, P/E ranks, profit-margin-vs-industry, EPS 5-yr growth
rank, Earnings Stability, Index % Chg 5 Days.  "Ind Group Rank" is COMPUTED
live (1 = best industry by mean RS of its members as of the latest cached bars),
not taken from IBD, and "Ind Group RS" (1-99) is the mean RS of the group's
members.  Ind Grp Rnk Last Week / 3 Mo Ago / 6 Mo Ago are computed by replaying
the RS rating on truncated price series.

Every ticker in IBD/marketsurge.csv is covered (6,374 rows after dropping the one
source row that has an empty Symbol).  Tickers that have no cached parquet or fund
file are still emitted with blank values so the screener universe never silently
shrinks.  Any column that cannot be derived from local data (e.g. true MarketSurge
survey fields, 5-yr P/E history, S&P 500 P/E, 2-periods-ago short interest, short
volume) is left blank rather than guessed.

Outputs (one row per ticker, values as of the latest common trading day):
  output/daily_screener_<YYYY-MM-DD>.csv   dated snapshot
  output/daily_screener.csv                latest snapshot (same content)

Usage:
  python python/build_daily_screener.py                # full build
  python python/build_daily_screener.py --limit 100    # quick smoke test
  python python/build_daily_screener.py --no-ratings   # skip ratings pass (fast)
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_DIR / "ticker_cache"
OUTPUT_DIR = REPO_DIR / "output"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from calc_ibd_ratings import (
    apply_group_columns,
    apply_rating_percentiles,
    calc_ad_raw_score,
    calc_eps_rating,
    calc_pct_off_52w_high_snapshot,
    calc_rs_raw_score,
    calc_rs_sub_raw_score,
    calc_smr_raw_score,
    extract_eps_analyst_features,
    extract_eps_from_fundamentals,
    extract_info_features,
    extract_smr_inputs_from_fundamentals,
)

# extract_eps_analyst_features()'s return-tuple order -> calc_eps_rating()'s extra_features keys
_EPS_ANALYST_KEYS = ("EPS_StabilityCV", "EpsSurpriseMean", "EpsBeatRate", "EpsRevTrend",
                     "EstEPSGrowth_Q", "EstEPSGrowth_Y")


def _eps_extra_features(fund):
    """extra_features dict for calc_eps_rating(): analyst signals + info-dict fields."""
    analyst = dict(zip(_EPS_ANALYST_KEYS, extract_eps_analyst_features(fund))) if fund else {}
    info = extract_info_features(fund) if fund else {}
    return {**analyst, **info}

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── MarketSurge column order (kept verbatim from IBD/marketsurge.csv) ─────────
MS_COLUMNS = [
    "#", "Symbol", "Name",
    "EPS Est Next Yr %", "EPS Trl 4Q Gtr EPS 4 Yrs Ago", "EPS % Growth 5 Yr Pct Rnk",
    "Earnings Stability", "Fiscal EPS Lst Yr", "Fiscal EPS 1 Yr Ago", "Fiscal EPS 2 Yrs Ago",
    "Fiscal EPS 3 Yrs Ago", "Fiscal EPS 4 Yrs Ago", "Fiscal EPS 5 Yrs Ago", "Fiscal EPS 6 Yrs Ago",
    "Sustainable Growth %", "EPS Trailing 4 Qtrs", "EPS Lst Yr Gtr EPS 4 Yrs Ago",
    "EPS Trl 4Q Geq EPS Lst Fiscal Yr", "EPS Est Cur Yr %", "3-Yr EPS Growth Geq 5-Yr",
    "Ind Group Rank", "Industry Name", "Sector", "Shares in Float (1000s)", "Shares (1000s)",
    "Enterprise Val (mil)", "New CEO 12 Months", "Days Vol Short 1 Period Ago",
    "Days Vol Short Current", "Short Volume", "Shrt Int % of Float", "Shrt Int % Chg",
    "Days Vol Short 2 Periods Ago", "Ex-Dividend Date", "Expected X Dividend Amount",
    "A/D Rating - Pr Wk", "52-Wk Low", "52-Wk High", "Pr Wk High($)", "Number of Stocks",
    "Index % Chg 5 Days", "Ind Grp Rnk 6 Mo Ago", "Ind Grp Rnk 3 Mo Ago", "Ind Grp Rnk Last Week",
    "Ind Mkt Val (bil)", "% New Lows in Group", "# New Lows in Group", "% New Highs in Group",
    "# New Highs in Group", "Current Ratio", "Price to CF", "EV to FCF", "CF vs EPS % Last Yr",
    "CF vs EPS % Last Qtr", "Price to Book", "Price to Sales", "Dividend-Adjusted PEG", "PEG",
    "Forward P/E", "P/E Lss 5-Yr Avg", "P/E vs S&P 500 P/E (%)", "P/E Ratio Rank in Grp",
    "P/E Percent Rank", "P/E", "Prof Marg Geq Ind Median", "Beta", "Alpha", "50 Day ATR %",
    "30 Day ATR %", "21 Day ATR %", "Avg True Range",
    "Current day's Volume greater than previous 10 days' Volume",
    "Current day's Volume greater than previous 20 days' Volume",
    "Current day's Volume greater than previous 5 days' Volume",
    "50-Day Avg $ Vol (1000s)", "50-Day Avg Vol (1000s)", "Up/Down Vol", "Vol % Chg vs 10-Week",
    "Trl 26 Wk % Perf vs S&P 500", "% Chg YTD", "% Chg 6 Months", "% Chg 12 Months",
    "% Chg 3 Months", "% Chg 1 Month", "% Chg Cur Week", "RS 6-Month Rating", "RS 3-Month Rating",
    "RS Line New Low", "RS Line New High", "RS Line Within 5% of New High", "% Off High",
    "50-Day > 150-Day > 200-Day", "10 Day > 21 Day > 50 Day", "Price vs 200-Day",
    "Price vs 150-Day", "Price vs 50-Day", "Price vs 21-Day", "Price vs 10-Day",
    "Weekly Closing Range", "Daily Closing Range", "Funds % Increase", "Funds %", "Number of Funds",
    "AT Margin Accel", "Avg AT Margin 6Q", "Avg AT Margin 5Q", "Avg AT Margin 2Q",
    "Avg AT Margin 3Q", "Avg AT Margin 4Q", "AT Margin", "Pre-tax Margins", "ROE 5-Yr Avg",
    "ROE", "Sales Growth 5 Yr", "Sales Growth 3 Yr", "Sales % Chg Lst Yr", "Sales Accel 3 Qtrs",
    "Sales Accel 2 Qtrs", "Avg Sales % Chg 6Q", "Avg Sales % Chg 5Q", "Avg Sales % Chg 4Q",
    "Avg Sales % Chg 3Q", "Avg Sales % Chg 2Q", "Sales % Chg Lst Qtr", "Annual Sales (mil)",
    "EPS % Growth 5 Yr", "EPS % Growth 3 Yr", "EPS % Growth 1 Yr", "EPS % Chg 1 Yr Ago",
    "EPS % Chg Lst Yr", "EPS Lst Rptd", "EPS Due Date", "EPS Surprise", "EPS Est Cur Qtr %",
    "EPS % Chg Lst Q Gtr 3-Yr Growth", "Avg EPS % Chg 6Q", "Avg EPS % Chg 5Q", "Avg EPS % Chg 4Q",
    "Avg EPS % Chg 3Q", "Avg EPS % Chg 2Q", "EPS % Chg 3 Q Ago (-/+)", "EPS Accel 3 Qtrs",
    "EPS % Chg 2 Q Ago (-/+)", "EPS % Chg 1 Q Ago (-/+)", "EPS % Chg Last Qtr (-/+)",
    "Current Price", "Price % Chg", "Volume (1000s)", "Price $ Chg", "EPS Rating", "RS Rating",
    "Vol % Chg vs 50-Day", "Ind Group RS", "SMR Rating", "A/D Rating", "Comp Rating",
    "Market Cap (mil)", "Company Description",
]

# Columns we deliberately leave blank: these have no local equivalent at all
# (survey/qualitative fields, 5-yr P/E history, S&P 500 P/E, 2-periods-ago short
# interest, and short volume are not derivable from ticker_cache).
SKIP_COLUMNS = {
    "New CEO 12 Months", "Short Volume", "Days Vol Short 2 Periods Ago",
    "P/E Lss 5-Yr Avg", "P/E vs S&P 500 P/E (%)",
}

# Extra columns appended after the MarketSurge 158: additional fundamental data in
# fund.json that has no MarketSurge column (analyst targets, EPS trend/revisions,
# recommendation detail, insider/institutional ownership, balance sheet, margins,
# cash flow, ESG, forward estimates) plus price/volume derived extras.
EXTRA_COLUMNS = [
    # analyst targets
    "Analyst Target Mean", "Analyst Target High", "Analyst Target Low",
    "Analyst Target Median", "% Upside to Target",
    # analyst recommendation breakdown (current period)
    "Rec Strong Buy", "Rec Buy", "Rec Hold", "Rec Sell", "Rec Strong Sell",
    # EPS estimate trend / revisions (0y horizon)
    "EPS Est Trend 0y %", "EPS Rev Up 7D", "EPS Rev Down 7D",
    "EPS Rev Up 30D", "EPS Rev Down 30D",
    # revenue estimates / long-term growth
    "Rev Est Cur Qtr %", "Rev Est Cur Yr %", "Rev Est Next Yr %", "Long Term Growth %",
    # insider activity (6 months)
    "Insider Purchases 6M", "Insider Sales 6M", "Insider Net 6M", "Insider % Buy 6M",
    # institutional / major holder data
    "Inst % Held", "Inst Float % Held", "Inst Count",
    # balance sheet
    "Total Cash (mil)", "Total Debt (mil)", "Net Cash (mil)", "Debt to Equity",
    "Quick Ratio", "Book Value", "Total Cash/Share",
    # margins / cash flow
    "Gross Margin %", "Operating Margin %", "Net Profit Margin %", "ROA %",
    "EBITDA (mil)", "EBITDA Margin %", "Free Cash Flow (mil)",
    "Operating Cash Flow (mil)", "Capital Expenditure (mil)", "EV to EBITDA",
    # per-share / growth
    "Dividend Yield %", "Trailing EPS", "Forward EPS", "Earnings Growth %",
    "Revenue Growth %", "52-Week % Chg", "Avg Volume", "Avg Volume 10D",
    # ESG risk scores
    "ESG Overall Risk", "ESG Audit Risk", "ESG Board Risk",
    "ESG Compensation Risk", "ESG Shareholder Rights Risk",
    # earnings surprise history
    "Avg EPS Surprise 4Q",
    # price/volume extras computed from the parquet
    "RSI 14", "% Chg 5 Days", "% Chg 10 Days", "% Chg 20 Days",
    "50-Day Avg Price", "200-Day Avg Price", "10-Day Avg Vol (1000s)",
    "Relative Volume", "52-Week Position %", "Volatility 30D %",
    # listing-age proxy (first cached date, not a verified true IPO date)
    "First Cached Date", "Years Since First Cached",
]


# ── helpers ──────────────────────────────────────────────────────────────────

def _num(v):
    """Coerce to float, else None."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _pct_round(v, nd=1):
    f = _num(v)
    return round(f, nd) if f is not None else None


def _yes_no(v):
    """Map a truthy/falsy value to Yes/No (marketsurge style), else ''."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return "Yes" if bool(v) else "No"


def _cagr(series, years, allow_shorter=False, min_eff=1):
    """CAGR % over `years` for a most-recent-first numeric list.

    With allow_shorter=True, uses the longest span actually available instead of
    requiring `years + 1` data points, so e.g. a 4-year EPS history still yields a
    growth rate for a 5-yr column.  `min_eff` is the smallest span (in years) we
    will accept from the shorter path - a bare 1-yr change is not presented as a
    multi-year growth rate.
    """
    if len(series) < 2:
        return None
    eff = min(years, len(series) - 1) if allow_shorter else years
    if eff < min_eff or len(series) <= eff:
        return None
    newest, oldest = series[0], series[eff]
    if newest is None or oldest is None or newest <= 0 or oldest <= 0:
        return None
    try:
        return (newest / oldest) ** (1.0 / eff) - 1.0
    except Exception:
        return None


def _row(fund, table, labels):
    """Numeric row from a fund table, most recent first. fund tables are stored as
    {line_item: {period: value, ...}}."""
    t = fund.get(table) if isinstance(fund, dict) else None
    if not isinstance(t, dict):
        return []
    for lab in labels:
        col = t.get(lab)
        if isinstance(col, dict):
            vals = []
            for d in sorted(col.keys(), reverse=True):
                v = _num(col.get(d))
                if v is not None:
                    vals.append(v)
            if vals:
                return vals
    return []


def _q_yoy(series, j, span=4):
    """YoY % growth of quarter `j` (0 = most recent) vs `span` quarters earlier."""
    if len(series) <= j + span:
        return None
    newer, older = series[j], series[j + span]
    if newer is None or older is None or older == 0:
        return None
    return (newer / older - 1.0) * 100.0


def _avg_growth(series, n, span=4):
    """Average YoY % growth over the last `n` quarters. Requires a full window:
    if the series is too short for n+span values it returns None rather than
    averaging a partial window and mislabeling it as an N-quarter average."""
    if len(series) < n + span:
        return None
    vals = [_q_yoy(series, j, span) for j in range(n)]
    vals = [v for v in vals if v is not None]
    return float(np.mean(vals)) if vals else None


def _est_growth(fund, horizon, metric="growth"):
    """Forward estimate growth fraction for a horizon ('0q','+1q','0y','+1y')."""
    ee = fund.get("earnings_estimate")
    if isinstance(ee, dict):
        h = ee.get(horizon)
        if isinstance(h, dict):
            g = _num(h.get(metric))
            if g is not None:
                return g
    ge = fund.get("growth_estimates")
    if isinstance(ge, dict):
        h = ge.get(horizon)
        if isinstance(h, dict):
            g = _num(h.get("stockTrend"))
            if g is not None:
                return g
    return None


def _roe_percent(fund):
    """ROE as a percentage (consistently). info['returnOnEquity'] is a ratio."""
    info = fund.get("info") if isinstance(fund, dict) else None
    if isinstance(info, dict):
        roe = _num(info.get("returnOnEquity"))
        if roe is not None:
            return roe * 100.0
    ni = _row(fund, "income_q", ("Net Income", "Net Income Common Stockholders"))
    eq = _row(fund, "balance_q", ("Stockholders Equity", "Total Stockholder Equity",
                                  "Common Stock Equity", "Total Equity Gross Minority Interest"))
    if ni and eq and _num(eq[0]):
        return ni[0] / eq[0] * 100.0
    return None


# ── technical metrics (price / volume from parquet) ──────────────────────────

def compute_technical_metrics(df, spy_close):
    """All price/volume-derived columns for the last bar."""
    m = {}
    if df is None or len(df) < 2:
        return m

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)
    n = len(df)

    c = float(close.iloc[-1])
    if not np.isfinite(c) or c <= 0:
        return m

    # ── prices / volume ──
    m["Current Price"] = round(c, 2)
    m["Price $ Chg"] = round(c - float(close.iloc[-2]), 2)
    if float(close.iloc[-2]) > 0:
        m["Price % Chg"] = round((c / float(close.iloc[-2]) - 1) * 100, 2)
    m["Volume (1000s)"] = round(float(volume.iloc[-1]) / 1000.0, 1)

    # ── listing-age proxy (first date our cache has for this ticker; not a true IPO
    # date, but ticker_cache generally carries a stock's full available history, so
    # this is a reasonable stand-in - used for the IPO Leaders screener section) ──
    m["First Cached Date"] = df.index[0].strftime("%Y-%m-%d")
    m["Years Since First Cached"] = round((df.index[-1] - df.index[0]).days / 365.25, 1)

    # ── 52-week window ──
    w52 = df.tail(252)
    m["52-Wk Low"] = round(float(w52["Low"].min()), 2)
    m["52-Wk High"] = round(float(w52["High"].max()), 2)
    m["% Off High"] = _pct_round(calc_pct_off_52w_high_snapshot(df), 2)

    # ── previous-week high ──
    weeks = df.index.to_period("W")
    cur_week = weeks[-1]
    prev = weeks < cur_week
    if prev.any():
        m["Pr Wk High($)"] = round(float(df.loc[prev, "High"].max()), 2)

    # ── moving averages ──
    sma10 = close.rolling(10).mean().iloc[-1]
    ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    sma150 = close.rolling(150).mean().iloc[-1]
    sma200 = close.rolling(200).mean().iloc[-1]

    for name, ma in (("10", sma10), ("21", ema21), ("50", sma50), ("150", sma150), ("200", sma200)):
        if _num(ma):
            m[f"Price vs {name}-Day"] = _pct_round((c / ma - 1) * 100, 2)

    m["10 Day > 21 Day > 50 Day"] = _yes_no(
        _num(sma10) and _num(ema21) and _num(sma50) and sma10 > ema21 > sma50)
    m["50-Day > 150-Day > 200-Day"] = _yes_no(
        _num(sma50) and _num(sma150) and _num(sma200) and sma50 > sma150 > sma200)

    # ── ATR ──
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    for k, label in ((14, "Avg True Range"), (21, "21 Day ATR %"), (30, "30 Day ATR %"), (50, "50 Day ATR %")):
        if n >= k:
            atr = float(tr.rolling(k).mean().iloc[-1])
            if label == "Avg True Range":
                m[label] = round(atr, 2)
            else:
                m[label] = _pct_round(atr / c * 100, 2)

    # ── volume ratios ──
    vol50 = float(volume.rolling(50).mean().iloc[-1]) if n >= 50 else np.nan
    if np.isfinite(vol50) and vol50 > 0:
        vchg = (float(volume.iloc[-1]) / vol50 - 1) * 100
        m["Vol % Chg vs 50-Day"] = round(vchg, 1)
        m["Vol % Chg vs 10-Week"] = round(vchg, 1)
    for days, label in ((5, "5"), (10, "10"), (20, "20")):
        if n >= days + 1:
            avg_prev = float(volume.iloc[-(days + 1):-1].mean())
            m[f"Current day's Volume greater than previous {label} days' Volume"] = _yes_no(
                float(volume.iloc[-1]) > avg_prev and avg_prev > 0)
    if n >= 50:
        m["50-Day Avg Vol (1000s)"] = round(vol50 / 1000.0, 1)
        m["50-Day Avg $ Vol (1000s)"] = round(float((close * volume).rolling(50).mean().iloc[-1]) / 1000.0, 1)

        up = close.diff() > 0
        dn = close.diff() < 0
        up_vol = float((volume * up).tail(50).sum())
        dn_vol = float((volume * dn).tail(50).sum())
        m["Up/Down Vol"] = round(up_vol / dn_vol, 2) if dn_vol > 0 else 1.0

    # ── returns ──
    def _ret(k, label):
        if n >= k + 1:
            base = float(close.iloc[-(k + 1)])
            if base > 0:
                m[label] = _pct_round((c / base - 1) * 100, 2)

    _ret(21, "% Chg 1 Month")
    _ret(63, "% Chg 3 Months")
    _ret(126, "% Chg 6 Months")
    _ret(252, "% Chg 12 Months")

    cur_year = df.index[-1].year
    prior = df[df.index.year < cur_year]
    if len(prior):
        base = float(prior["Close"].iloc[-1])
        if base > 0:
            m["% Chg YTD"] = _pct_round((c / base - 1) * 100, 2)
    if n >= 6:
        base = float(close.iloc[-6])
        if base > 0:
            m["% Chg Cur Week"] = _pct_round((c / base - 1) * 100, 2)

    if n >= 127:
        sb = float(close.iloc[-127])
        if sb > 0:
            stock_26 = c / sb - 1
            spy_base = float(spy_close.iloc[-127])
            spy_26 = float(spy_close.iloc[-1]) / spy_base - 1 if spy_base > 0 else np.nan
            if np.isfinite(spy_26):
                m["Trl 26 Wk % Perf vs S&P 500"] = round((stock_26 - spy_26) * 100, 2)

    # ── weekly / daily closing range ──
    wmask = weeks == cur_week
    wh = float(df.loc[wmask, "High"].max())
    wl = float(df.loc[wmask, "Low"].min())
    if wh > wl:
        m["Weekly Closing Range"] = round((c - wl) / (wh - wl) * 100, 1)
    h, l = float(high.iloc[-1]), float(low.iloc[-1])
    if h > l:
        m["Daily Closing Range"] = round((c - l) / (h - l) * 100, 1)

    # ── beta / alpha vs SPY ──
    rs = close / spy_close
    r_line = rs.astype(float)
    if n >= 2 and np.isfinite(r_line.iloc[-1]):
        w252 = r_line.tail(252)
        cur_line = float(r_line.iloc[-1])
        mx, mn = float(w252.max()), float(w252.min())
        if mx > 0:
            prev = w252.iloc[:-1]
            p_max, p_min = float(prev.max()), float(prev.min())
            # "New high/low" means today's RS line exceeds every one of the prior 251
            # values (not just that it equals the window max).
            m["RS Line New High"] = _yes_no(cur_line > p_max and np.isfinite(p_max))
            m["RS Line New Low"] = _yes_no(cur_line < p_min and np.isfinite(p_min))
            m["RS Line Within 5% of New High"] = _yes_no(cur_line >= 0.95 * mx)

    ret_s = close.pct_change().dropna()
    ret_m = spy_close.pct_change().dropna()
    idx = ret_s.index.intersection(ret_m.index)
    if len(idx) > 30:
        rs_ = ret_s.loc[idx].tail(252)
        rm_ = ret_m.loc[idx].tail(252)
        var_m = float(rm_.var())
        if var_m > 0:
            beta = float(rs_.cov(rm_)) / var_m
            m["Beta"] = round(beta, 2)
            m["Alpha"] = round((float(rs_.mean()) - beta * float(rm_.mean())) * 252 * 100, 2)

    # ── extra price/volume columns (beyond the MarketSurge 158) ──
    if n >= 30:
        r30 = close.pct_change().dropna().tail(30)
        if len(r30) >= 20:
            m["Volatility 30D %"] = round(float(r30.std()) * np.sqrt(252) * 100, 1)
    if n >= 15:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        ag = gain.ewm(alpha=1 / 14, adjust=False).mean()
        al = loss.ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 - 100 / (1 + ag / al.replace(0, np.nan))
        v = rsi.iloc[-1]
        if _num(v):
            m["RSI 14"] = round(float(v), 1)
    for k, label in ((5, "% Chg 5 Days"), (10, "% Chg 10 Days"), (20, "% Chg 20 Days")):
        _ret(k, label)
    if _num(sma50):
        m["50-Day Avg Price"] = round(float(sma50), 2)
    if _num(sma200):
        m["200-Day Avg Price"] = round(float(sma200), 2)
    if n >= 10:
        vol10 = float(volume.rolling(10).mean().iloc[-1])
        m["10-Day Avg Vol (1000s)"] = round(vol10 / 1000.0, 1)
        if vol10 > 0:
            m["Relative Volume"] = round(float(volume.iloc[-1]) / vol10, 2)
    h52 = float(w52["High"].max())
    l52 = float(w52["Low"].min())
    if h52 > l52:
        m["52-Week Position %"] = round((c - l52) / (h52 - l52) * 100, 1)
    return m


# ── ratings via calc_ibd_ratings.py ──────────────────────────────────────────

def compute_rating_metrics(df, fund, fy_eps=None, fq_eps=None, roe=None):
    """Per-ticker RAW scores for RS / A-D / SMR, plus the already-final EPS Rating.

    RS / A-D / SMR / Comp Rating are inherently universe computations (percentile
    ranks can't be produced for one ticker in isolation) — apply_rating_percentiles()
    finishes the job in a post-pass over the whole assembled `out` DataFrame, the
    same pattern apply_group_columns() already uses for the group columns.
    """
    m = {}
    close = df["Close"].astype(float)
    n = len(df)
    c = float(close.iloc[-1])
    if not np.isfinite(c) or c <= 0:
        return m

    m["_rs_raw"] = calc_rs_raw_score(close)
    m["_rs3m_raw"] = calc_rs_sub_raw_score(close, 63)
    m["_rs6m_raw"] = calc_rs_sub_raw_score(close, 126)

    # Historical RS raw scores used for the group-rank-history columns (Ind Grp Rnk
    # Last Week / 3 Mo Ago / 6 Mo Ago). These feed apply_group_columns()'s per-industry
    # MEAN + industry-vs-industry rank, not a per-ticker rating, so a raw (not
    # independently percentile-ranked) score is a reasonable proxy here — a full
    # historical universe re-rank at 3 extra cutoff dates would ~4x this pass's
    # runtime for what is a secondary/display feature, not one of the 5 core ratings.
    for _drop, _key in ((5, "_rs_1w_ago"), (63, "_rs_3m_ago"), (126, "_rs_6m_ago")):
        if n > _drop + 249:
            m[_key] = calc_rs_raw_score(close.iloc[:-_drop])

    m["_ad_raw"] = calc_ad_raw_score(df)
    if n >= 66:
        weeks = df.index.to_period("W")
        prev = weeks < weeks[-1]
        if prev.any():
            end = df.index[prev][-1]
            sub = df.iloc[:df.index.get_loc(end) + 1]
            m["_ad_prev_raw"] = calc_ad_raw_score(sub)

    # EPS / SMR from fundamentals.  EPS Rating is only emitted when it was actually
    # computed from real EPS data - otherwise it is left blank ("skip it if you cannot
    # find related information") rather than showing a data-poor fallback as genuine.
    if fund and not fund.get("error"):
        if fy_eps is None:
            fy_eps, fq_eps, _ = extract_eps_from_fundamentals(fund)
        if roe is None:
            roe = _roe_percent(fund)
    fund_eps_ok = bool(fy_eps and fq_eps and len(fy_eps) >= 2 and len(fq_eps) >= 5)
    if fund_eps_ok:
        m["EPS Rating"] = calc_eps_rating(fy_eps, fq_eps, roe, _eps_extra_features(fund))

    if fund and not fund.get("error"):
        sales_q0_yoy, sales_lt_growth, margin_now, margin_trend = extract_smr_inputs_from_fundamentals(fund)
        m["_smr_raw"] = calc_smr_raw_score(sales_q0_yoy, sales_lt_growth, margin_now, margin_trend, roe,
                                            extract_info_features(fund))

    return m


# ── fundamentals from fund.json ──────────────────────────────────────────────

def compute_fundamental_metrics(fund, close_price=None, fy_eps=None, fq_eps=None, roe=None):
    m = {}
    if not fund or fund.get("error"):
        return m
    info = fund.get("info")
    if not isinstance(info, dict):
        info = {}

    if fy_eps is None or fq_eps is None:
        fy_eps, fq_eps, _ = extract_eps_from_fundamentals(fund)
    if roe is None:
        roe = _roe_percent(fund)
    revenue_a = _row(fund, "income_a", ("Total Revenue", "Operating Revenue"))
    revenue_q = _row(fund, "income_q", ("Total Revenue", "Operating Revenue"))
    ni_q = _row(fund, "income_q", ("Net Income", "Net Income Common Stockholders",
                                   "Net Income Continuous Operations"))
    pretax_q = _row(fund, "income_q", ("Pretax Income",))
    ni_a = _row(fund, "income_a", ("Net Income", "Net Income Common Stockholders"))
    equity_a = _row(fund, "balance_a", ("Stockholders Equity", "Total Stockholder Equity",
                                        "Common Stock Equity", "Total Equity Gross Minority Interest"))
    ocf_q = _row(fund, "cashflow_q", ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"))

    def i(key):
        return _num(info.get(key))

    # ── fiscal EPS history ──
    # fy_eps/fq_eps may hold None at a calendar-correct position (a period with no
    # reported EPS) rather than being dropped - see extract_eps_from_fundamentals().
    # Every direct use below must skip (not just index-guard) a None entry.
    if fy_eps:
        for j, col in enumerate(("Fiscal EPS Lst Yr", "Fiscal EPS 1 Yr Ago", "Fiscal EPS 2 Yrs Ago",
                                 "Fiscal EPS 3 Yrs Ago", "Fiscal EPS 4 Yrs Ago", "Fiscal EPS 5 Yrs Ago",
                                 "Fiscal EPS 6 Yrs Ago")):
            if j < len(fy_eps) and fy_eps[j] is not None:
                m[col] = round(fy_eps[j], 2)

    # ── trailing EPS ──
    if fq_eps and len(fq_eps) >= 4 and all(v is not None for v in fq_eps[:4]):
        m["EPS Trailing 4 Qtrs"] = round(float(np.sum(fq_eps[:4])), 2)
    if fq_eps and fq_eps[0] is not None:
        m["EPS Lst Rptd"] = round(fq_eps[0], 2)

    # ── EPS growth (annual) ──
    # NOTE: with annual-only EPS in the cache, "EPS % Growth 1 Yr" and
    # "EPS % Chg Lst Yr" both resolve to last fiscal year's change.
    if fy_eps:
        if len(fy_eps) >= 2 and fy_eps[0] is not None and fy_eps[1]:
            m["EPS % Growth 1 Yr"] = _pct_round((fy_eps[0] / fy_eps[1] - 1) * 100, 1)
            m["EPS % Chg Lst Yr"] = _pct_round((fy_eps[0] / fy_eps[1] - 1) * 100, 1)
        if len(fy_eps) >= 3 and fy_eps[1] is not None and fy_eps[2]:
            m["EPS % Chg 1 Yr Ago"] = _pct_round((fy_eps[1] / fy_eps[2] - 1) * 100, 1)
        g3 = _cagr(fy_eps, 3)
        # 5-yr growth uses the longest span available (the fund cache only holds
        # ~4 fiscal years, so with 4 years the 5-yr column reports the 4-yr CAGR
        # rather than staying blank).  A floor of 2 years keeps a bare 1-yr change
        # from being labeled a multi-year growth rate.
        g5 = _cagr(fy_eps, 5, allow_shorter=True, min_eff=2)
        if g3 is not None:
            m["EPS % Growth 3 Yr"] = round(g3 * 100, 1)
        if g5 is not None:
            m["EPS % Growth 5 Yr"] = round(g5 * 100, 1)
        if g3 is not None and g5 is not None:
            m["3-Yr EPS Growth Geq 5-Yr"] = _yes_no(g3 >= g5)

    # ── quarterly EPS growth ──
    if fq_eps and len(fq_eps) >= 5:
        for j, col in zip(range(4), ("EPS % Chg Last Qtr (-/+)", "EPS % Chg 1 Q Ago (-/+)",
                                     "EPS % Chg 2 Q Ago (-/+)", "EPS % Chg 3 Q Ago (-/+)")):
            g = _q_yoy(fq_eps, j)
            if g is not None:
                m[col] = round(g, 1)
        for nq, col in ((6, "Avg EPS % Chg 6Q"), (5, "Avg EPS % Chg 5Q"), (4, "Avg EPS % Chg 4Q"),
                        (3, "Avg EPS % Chg 3Q"), (2, "Avg EPS % Chg 2Q")):
            v = _avg_growth(fq_eps, nq)
            if v is not None:
                m[col] = round(v, 1)
        last = _q_yoy(fq_eps, 0)
        prevs = [_q_yoy(fq_eps, j) for j in range(1, 4)]
        prevs = [v for v in prevs if v is not None]
        if last is not None and prevs:
            m["EPS Accel 3 Qtrs"] = _pct_round(last - float(np.mean(prevs)), 1)
            g3 = _cagr(fy_eps, 3) if fy_eps else None
            if g3 is not None:
                m["EPS % Chg Lst Q Gtr 3-Yr Growth"] = _yes_no(last > g3 * 100)

    # ── comparisons of EPS levels ──
    if fq_eps and len(fq_eps) >= 4 and fy_eps and all(v is not None for v in fq_eps[:4]):
        t4 = float(np.sum(fq_eps[:4]))
        if len(fy_eps) >= 5 and fy_eps[4]:
            m["EPS Trl 4Q Gtr EPS 4 Yrs Ago"] = _yes_no(t4 > fy_eps[4])
        if fy_eps[0]:
            m["EPS Trl 4Q Geq EPS Lst Fiscal Yr"] = _yes_no(t4 >= fy_eps[0])
        if fy_eps[0] and len(fy_eps) >= 5 and fy_eps[4]:
            m["EPS Lst Yr Gtr EPS 4 Yrs Ago"] = _yes_no(fy_eps[0] > fy_eps[4])

    # ── forward estimates ──
    g_next = _est_growth(fund, "+1y")
    g_cur = _est_growth(fund, "0y")
    g_q = _est_growth(fund, "0q")
    if g_next is not None:
        m["EPS Est Next Yr %"] = round(g_next * 100, 1)
    if g_cur is not None:
        m["EPS Est Cur Yr %"] = round(g_cur * 100, 1)
    if g_q is not None:
        m["EPS Est Cur Qtr %"] = round(g_q * 100, 1)

    # ── earnings date / surprise ──
    cal = fund.get("calendar")
    if isinstance(cal, dict):
        ed = cal.get("Earnings Date")
        if isinstance(ed, list) and ed:
            m["EPS Due Date"] = str(ed[0])[:10]
        xdiv = cal.get("Ex-Dividend Date")
        if xdiv:
            m["Ex-Dividend Date"] = str(xdiv)[:10]
    eh = fund.get("earnings_history")
    if isinstance(eh, dict):
        dates = sorted([k for k in eh if not k.startswith("_")], reverse=True)
        if dates:
            sp = _num(eh[dates[0]].get("surprisePercent"))
            if sp is not None:
                m["EPS Surprise"] = round(sp * 100, 1)

    # ── sales / revenue ──
    if revenue_q and len(revenue_q) >= 5:
        # avg sales YoY growth over 2..6 quarters
        for nq, col in ((6, "Avg Sales % Chg 6Q"), (5, "Avg Sales % Chg 5Q"),
                        (4, "Avg Sales % Chg 4Q"), (3, "Avg Sales % Chg 3Q"), (2, "Avg Sales % Chg 2Q")):
            v = _avg_growth(revenue_q, nq)
            if v is not None:
                m[col] = round(v, 1)
        last = _q_yoy(revenue_q, 0)
        if last is not None:
            m["Sales % Chg Lst Qtr"] = round(last, 1)
        prevs = [_q_yoy(revenue_q, j) for j in range(1, 4)]
        prevs = [v for v in prevs if v is not None]
        if last is not None and len(prevs) >= 2:
            m["Sales Accel 3 Qtrs"] = _pct_round(last - float(np.mean(prevs)), 1)
        if last is not None and len(prevs) >= 1:
            m["Sales Accel 2 Qtrs"] = _pct_round(last - float(np.mean(prevs[:1])), 1)

    if revenue_a:
        if len(revenue_a) >= 2 and revenue_a[1]:
            m["Sales % Chg Lst Yr"] = _pct_round((revenue_a[0] / revenue_a[1] - 1) * 100, 1)
        if len(revenue_a) >= 4:
            g3 = _cagr(revenue_a, 3)
            if g3 is not None:
                m["Sales Growth 3 Yr"] = round(g3 * 100, 1)
        if len(revenue_a) >= 6:
            g5 = _cagr(revenue_a, 5)
            if g5 is not None:
                m["Sales Growth 5 Yr"] = round(g5 * 100, 1)
        m["Annual Sales (mil)"] = round(revenue_a[0] / 1e6, 1)

    # ── margins ──
    if revenue_q and ni_q:
        def _margin(r, ni):
            if r and ni and r[0]:
                return ni[0] / r[0] * 100.0
            return None
        for nq, col in ((6, "Avg AT Margin 6Q"), (5, "Avg AT Margin 5Q"), (2, "Avg AT Margin 2Q"),
                        (3, "Avg AT Margin 3Q"), (4, "Avg AT Margin 4Q")):
            vals = []
            for j in range(min(nq, len(revenue_q), len(ni_q))):
                if revenue_q[j] and ni_q[j] and revenue_q[j] > 0:
                    vals.append(ni_q[j] / revenue_q[j] * 100.0)
            if vals:
                m[col] = round(float(np.mean(vals)), 1)
        m["AT Margin"] = _pct_round(_margin(revenue_q, ni_q), 1)
        margins = []
        for j in range(min(3, len(revenue_q), len(ni_q))):
            if revenue_q[j] and ni_q[j] and revenue_q[j] > 0:
                margins.append(ni_q[j] / revenue_q[j] * 100.0)
        last_m = _margin(revenue_q, ni_q)
        if last_m is not None and len(margins) >= 2:
            m["AT Margin Accel"] = _pct_round(last_m - float(np.mean(margins[1:])), 1)
    if revenue_q and pretax_q and revenue_q[0] and revenue_q[0] > 0:
        m["Pre-tax Margins"] = _pct_round(pretax_q[0] / revenue_q[0] * 100, 1)

    # ── ROE ──
    if roe is not None:
        m["ROE"] = round(roe, 1)
    if ni_a and equity_a:
        roes = []
        for j in range(min(5, len(ni_a), len(equity_a))):
            if ni_a[j] and equity_a[j] and equity_a[j] > 0:
                roes.append(ni_a[j] / equity_a[j] * 100.0)
        if roes:
            m["ROE 5-Yr Avg"] = round(float(np.mean(roes)), 1)

    payout = i("payoutRatio")
    if roe is not None and payout is not None:
        retention = 1.0 - payout
        if retention > 0:
            m["Sustainable Growth %"] = round(roe * retention, 1)

    # ── valuation ──
    m["Current Ratio"] = i("currentRatio")
    m["Price to Book"] = i("priceToBook")
    m["Price to Sales"] = i("priceToSalesTrailing12Months")
    m["PEG"] = i("pegRatio")
    m["Dividend-Adjusted PEG"] = i("trailingPegRatio") if i("trailingPegRatio") is not None else i("pegRatio")
    m["Forward P/E"] = i("forwardPE")
    m["P/E"] = i("trailingPE")

    pcf = i("priceToCashFlow")
    if pcf is None:
        pcf = i("priceToFreeCashFlows")
    if pcf is None:
        ocf = i("operatingCashflow")
        ev = i("enterpriseValue")
        if ocf and ev and ocf > 0:
            pcf = ev / ocf
    m["Price to CF"] = pcf

    ev = i("enterpriseValue")
    fcf = i("freeCashflow")
    if ev and fcf and fcf > 0:
        m["EV to FCF"] = round(ev / fcf, 1)

    # ── CF vs EPS ──
    shares = i("sharesOutstanding")
    if shares and shares > 0:
        ocf = i("operatingCashflow")
        if ocf is not None and fy_eps and fy_eps[0]:
            cf_ps = ocf / shares
            if fy_eps[0] > 0:
                m["CF vs EPS % Last Yr"] = _pct_round((cf_ps / fy_eps[0] - 1) * 100, 1)
        if ocf_q and fq_eps and fq_eps[0]:
            cf_ps_q = ocf_q[0] / shares
            if fq_eps[0] > 0:
                m["CF vs EPS % Last Qtr"] = _pct_round((cf_ps_q / fq_eps[0] - 1) * 100, 1)

    # ── size / shares ──
    m["Shares in Float (1000s)"] = round(i("floatShares") / 1000.0, 1) if i("floatShares") else None
    m["Shares (1000s)"] = round(i("sharesOutstanding") / 1000.0, 1) if shares else None
    if ev:
        m["Enterprise Val (mil)"] = round(ev / 1e6, 1)

    mcap = i("marketCap")
    if mcap is None and close_price and shares:
        mcap = close_price * shares
    if mcap:
        m["Market Cap (mil)"] = round(mcap / 1e6, 1)

    # ── dividends ──
    dr = i("dividendRate")
    if dr is None:
        dr = i("trailingAnnualDividendRate")
    m["Expected X Dividend Amount"] = dr

    # ── short interest ──
    m["Days Vol Short Current"] = i("shortRatio")
    avg_vol = i("averageVolume")
    sf = i("shortPercentOfFloat")
    if sf is not None:
        m["Shrt Int % of Float"] = round(sf * 100, 1)
    ss = i("sharesShort")
    ss_prev = i("sharesShortPriorMonth")
    if ss is not None and ss_prev:
        m["Shrt Int % Chg"] = _pct_round((ss - ss_prev) / abs(ss_prev) * 100, 1)
    if ss_prev and avg_vol and avg_vol > 0:
        m["Days Vol Short 1 Period Ago"] = _pct_round(ss_prev / avg_vol, 1)

    # ── mutual fund sponsorship (top-10 approximation) ──
    mf = fund.get("mutualfund_holders")
    if isinstance(mf, dict):
        rows = [v for k, v in mf.items() if not k.startswith("_") and isinstance(v, dict)]
        if rows:
            m["Number of Funds"] = len(rows)
            held = sum(_num(r.get("pctHeld")) or 0 for r in rows)
            m["Funds %"] = round(held * 100, 1) if held > 0 else None
            # Weighted-average pctChange of the top holders ≈ quarter-over-quarter
            # change in fund sponsorship ("Funds % Increase").
            w_sum = sum(_num(r.get("pctHeld")) or 0 for r in rows)
            chg_sum = sum((_num(r.get("pctHeld")) or 0) * (_num(r.get("pctChange")) or 0) for r in rows)
            if w_sum > 0:
                m["Funds % Increase"] = _pct_round(chg_sum / w_sum * 100, 1)

    return m


# ── extra fundamentals beyond the MarketSurge schema (from fund.json) ─────────

def compute_extra_fundamental_metrics(fund, close_price=None):
    """Columns in EXTRA_COLUMNS that come from fund.json tables info has no
    MarketSurge equivalent for: analyst targets, recommendation detail, EPS
    estimate trend/revisions, insider activity, institutional ownership, balance
    sheet, margins, cash flow, ESG, dividend yield, forward estimates."""
    m = {}
    if not fund or fund.get("error"):
        return m
    info = fund.get("info")
    if not isinstance(info, dict):
        info = {}

    def i(key):
        return _num(info.get(key))

    def _tbl(name):
        t = fund.get(name)
        return t if isinstance(t, dict) else None

    # ── analyst targets ──
    at = _tbl("analyst_targets")
    if at:
        for k, col in (("mean", "Analyst Target Mean"), ("high", "Analyst Target High"),
                       ("low", "Analyst Target Low"), ("median", "Analyst Target Median")):
            v = _num(at.get(k))
            if v is not None:
                m[col] = round(v, 2)
        mean = _num(at.get("mean"))
        if mean and close_price:
            m["% Upside to Target"] = _pct_round((mean / close_price - 1) * 100, 1)

    # ── recommendation breakdown (current period) ──
    rec = _tbl("recommendations")
    if rec and rec.get("0"):
        r0 = rec["0"] if isinstance(rec["0"], dict) else {}
        for k, col in (("strongBuy", "Rec Strong Buy"), ("buy", "Rec Buy"), ("hold", "Rec Hold"),
                       ("sell", "Rec Sell"), ("strongSell", "Rec Strong Sell")):
            v = _num(r0.get(k))
            if v is not None:
                m[col] = int(v)

    # ── EPS estimate trend / revisions (0y horizon) ──
    trend = _tbl("eps_trend")
    if trend and trend.get("0y"):
        t0 = trend["0y"] if isinstance(trend["0y"], dict) else {}
        cur = _num(t0.get("current"))
        ago = _num(t0.get("90daysAgo"))
        if cur is not None and ago:
            m["EPS Est Trend 0y %"] = _pct_round((cur / ago - 1) * 100, 1)
    rev = _tbl("eps_revisions")
    if rev and rev.get("0y"):
        r0 = rev["0y"] if isinstance(rev["0y"], dict) else {}
        for k, col in (("upLast7days", "EPS Rev Up 7D"), ("downLast7Days", "EPS Rev Down 7D"),
                       ("upLast30days", "EPS Rev Up 30D"), ("downLast30days", "EPS Rev Down 30D")):
            v = _num(r0.get(k))
            if v is not None:
                m[col] = int(v)

    # ── revenue estimates / long-term growth ──
    re = _tbl("revenue_estimate")
    if re:
        for h, col in (("0q", "Rev Est Cur Qtr %"), ("0y", "Rev Est Cur Yr %"),
                       ("+1y", "Rev Est Next Yr %")):
            hd = re.get(h)
            if isinstance(hd, dict):
                g = _num(hd.get("growth"))
                if g is not None:
                    m[col] = round(g * 100, 1)
    ge = _tbl("growth_estimates")
    if ge and ge.get("LTG"):
        ltg = ge["LTG"] if isinstance(ge["LTG"], dict) else {}
        g = _num(ltg.get("stockTrend"))
        if g is not None:
            m["Long Term Growth %"] = round(g * 100, 1)

    # ── insider activity (6 months) ──
    ip = _tbl("insider_purchases")
    if ip:
        def _ip(idx):
            r = ip.get(str(idx))
            return r if isinstance(r, dict) else {}
        for idx, col in ((0, "Insider Purchases 6M"), (1, "Insider Sales 6M"),
                         (2, "Insider Net 6M")):
            v = _num(_ip(idx).get("Shares"))
            if v is not None:
                m[col] = round(v, 0)
        pct_buy = _num(_ip(5).get("Shares"))
        if pct_buy is not None:
            m["Insider % Buy 6M"] = round(pct_buy * 100, 1)

    # ── institutional / major holder data ──
    mh = _tbl("major_holders")
    if mh and isinstance(mh.get("Value"), dict):
        v = mh["Value"]
        ih = _num(v.get("institutionsPercentHeld"))
        if ih is not None:
            m["Inst % Held"] = round(ih * 100, 1)
        fh = _num(v.get("institutionsFloatPercentHeld"))
        if fh is not None:
            m["Inst Float % Held"] = round(fh * 100, 1)
        ic = _num(v.get("institutionsCount"))
        if ic is not None:
            m["Inst Count"] = int(ic)

    # ── balance sheet ──
    m["Total Cash (mil)"] = round(i("totalCash") / 1e6, 1) if i("totalCash") else None
    m["Total Debt (mil)"] = round(i("totalDebt") / 1e6, 1) if i("totalDebt") else None
    tc = i("totalCash")
    td = i("totalDebt")
    if tc is not None and td is not None:
        m["Net Cash (mil)"] = round((tc - td) / 1e6, 1)
    m["Debt to Equity"] = i("debtToEquity")
    m["Quick Ratio"] = i("quickRatio")
    m["Book Value"] = i("bookValue")
    m["Total Cash/Share"] = i("totalCashPerShare")

    # ── margins ──
    for k, col in (("grossMargins", "Gross Margin %"), ("operatingMargins", "Operating Margin %"),
                   ("profitMargins", "Net Profit Margin %"), ("returnOnAssets", "ROA %"),
                   ("ebitdaMargins", "EBITDA Margin %")):
        v = i(k)
        if v is not None:
            m[col] = round(v * 100, 1)
    eb = i("ebitda")
    if eb:
        m["EBITDA (mil)"] = round(eb / 1e6, 1)
    fcf = i("freeCashflow")
    if fcf:
        m["Free Cash Flow (mil)"] = round(fcf / 1e6, 1)
    ocf = i("operatingCashflow")
    if ocf:
        m["Operating Cash Flow (mil)"] = round(ocf / 1e6, 1)
    m["Capital Expenditure (mil)"] = round(i("capitalExpenditures") / 1e6, 1) if i("capitalExpenditures") else None
    m["EV to EBITDA"] = i("enterpriseToEbitda")

    # ── per-share / growth ──
    # Dividend yield %: trailingAnnualDividendYield is unambiguously a decimal
    # fraction; dividendYield is percent in this yfinance version (but was a
    # decimal in older ones), so prefer the annual yield and keep dividendYield
    # only as a fallback, without re-normalizing it.
    dy = i("trailingAnnualDividendYield")
    if dy is not None:
        dy = dy * 100
    else:
        dy = i("dividendYield")
    m["Dividend Yield %"] = dy
    m["Trailing EPS"] = i("trailingEps")
    m["Forward EPS"] = i("forwardEps")
    eg = i("earningsGrowth")
    if eg is not None:
        m["Earnings Growth %"] = round(eg * 100, 1)
    rg = i("revenueGrowth")
    if rg is not None:
        m["Revenue Growth %"] = round(rg * 100, 1)
    chg = i("52WeekChange")
    if chg is not None:
        m["52-Week % Chg"] = round(chg * 100, 1)
    m["Avg Volume"] = i("averageVolume")
    m["Avg Volume 10D"] = i("averageVolume10days")

    # ── ESG risk scores ──
    for k, col in (("overallRisk", "ESG Overall Risk"), ("auditRisk", "ESG Audit Risk"),
                   ("boardRisk", "ESG Board Risk"), ("compensationRisk", "ESG Compensation Risk"),
                   ("shareHolderRightsRisk", "ESG Shareholder Rights Risk")):
        v = i(k)
        if v is not None:
            m[col] = int(v)

    # ── earnings surprise history (avg of last 4 quarters) ──
    eh = _tbl("earnings_history")
    if eh:
        dates = sorted([k for k in eh if not k.startswith("_")], reverse=True)[:4]
        sups = []
        for d in dates:
            r = eh.get(d)
            if isinstance(r, dict):
                sp = _num(r.get("surprisePercent"))
                if sp is not None:
                    sups.append(sp * 100)
        if sups:
            m["Avg EPS Surprise 4Q"] = round(float(np.mean(sups)), 1)

    return m


# ── assembly ─────────────────────────────────────────────────────────────────

def build_screener(limit=None, with_ratings=True):
    print("Reading IBD/marketsurge.csv ...")
    ms = pd.read_csv(REPO_DIR / "IBD" / "marketsurge.csv", encoding="utf-8-sig",
                     low_memory=False)
    ms["Symbol"] = ms["Symbol"].astype(str).str.strip()
    # A row with an empty Symbol (e.g. a stray line in the source export) has nothing
    # to screen on, so it is dropped rather than emitted.
    ms = ms[ms["Symbol"] != ""]
    ms = ms[ms["Symbol"] != "nan"]
    if limit:
        ms = ms.head(limit)

    print("Reading IBD_data.txt ...")
    ibd = pd.read_csv(REPO_DIR / "IBD_data.txt", encoding="utf-8-sig", low_memory=False)
    ibd["Symbol"] = ibd["Symbol"].astype(str).str.strip()
    ibd_map = {
        str(r["Symbol"]): {
            "industry": r.get("Industry Name") if pd.notna(r.get("Industry Name")) else None,
            "description": r.get("Company Description") if pd.notna(r.get("Company Description")) else None,
        }
        for _, r in ibd.iterrows()
    }
    sector_map = {
        str(r["Symbol"]): r.get("Sector") if pd.notna(r.get("Sector")) else None
        for _, r in ms.iterrows()
    }

    print("Loading SPY benchmark ...")
    spy = pd.read_parquet(CACHE_DIR / "SPY_1d.parquet")
    spy.index = pd.to_datetime(spy.index)
    spy_close = spy["Close"].astype(float)

    def cache_path(ticker):
        base = CACHE_DIR / f"{ticker}_1d.parquet"
        if base.exists():
            return base
        alt = CACHE_DIR / f"{ticker.replace('.', '-')}_1d.parquet"
        return alt if alt.exists() else None

    rows = []
    n_ok = 0
    last_dates = []
    total = len(ms)
    for i, r in enumerate(ms.iterrows()):
        r = r[1]
        sym = str(r["Symbol"])
        row = {c: None for c in MS_COLUMNS}
        row["#"] = i + 1
        row["Symbol"] = sym
        row["Name"] = r.get("Name") if pd.notna(r.get("Name")) else None
        row["Industry Name"] = ibd_map.get(sym, {}).get("industry")
        row["Company Description"] = ibd_map.get(sym, {}).get("description")
        row["Sector"] = sector_map.get(sym) or None

        fp = cache_path(sym)
        fund = None
        ffp = CACHE_DIR / f"{sym}_fund.json"
        if not ffp.exists():
            ffp = CACHE_DIR / f"{sym.replace('.', '-')}_fund.json"
        if ffp.exists():
            try:
                with open(ffp) as fh:
                    fund = json.load(fh)
            except Exception:
                fund = None

        # fundamentals parsed once per ticker and shared by the rating/fundamental passes
        fy_eps = fq_eps = None
        roe = None
        if fund and not fund.get("error"):
            fy_eps, fq_eps, _ = extract_eps_from_fundamentals(fund)
            roe = _roe_percent(fund)

        try:
            if fp is not None:
                df = pd.read_parquet(fp)
                df.index = pd.to_datetime(df.index)
                if getattr(df.index, "tz", None) is not None:
                    df.index = df.index.tz_localize(None)
                df = df.sort_index()
                if "Close" in df.columns and len(df):
                    spy_al = spy_close.reindex(df.index).ffill().bfill()
                    tech = compute_technical_metrics(df, spy_al)
                    row.update(tech)
                    if with_ratings:
                        row.update(compute_rating_metrics(df, fund,
                                                          fy_eps=fy_eps, fq_eps=fq_eps, roe=roe))
                    row.update(compute_fundamental_metrics(fund, close_price=tech.get("Current Price"),
                                                           fy_eps=fy_eps, fq_eps=fq_eps, roe=roe))
                    row.update(compute_extra_fundamental_metrics(
                        fund, close_price=tech.get("Current Price")))
                    n_ok += 1
                    last_dates.append(str(df.index[-1].date()))
            elif fund:
                row.update(compute_fundamental_metrics(fund, fy_eps=fy_eps, fq_eps=fq_eps, roe=roe))
                row.update(compute_extra_fundamental_metrics(fund))
        except Exception as e:
            print(f"  ! {sym}: {e}")

        # hidden per-ticker fields used by the universe passes (apply_rating_percentiles,
        # apply_group_columns). _rs_cur is set from the FINAL RS Rating after
        # apply_rating_percentiles runs (see below) - "RS Rating" doesn't exist yet here.
        row["_rs_raw"] = row.get("_rs_raw")
        row["_rs_1w_ago"] = row.get("_rs_1w_ago")
        row["_rs_3m_ago"] = row.get("_rs_3m_ago")
        row["_rs_6m_ago"] = row.get("_rs_6m_ago")
        row["_mcap"] = row.get("Market Cap (mil)")
        row["_pe"] = row.get("P/E")
        row["_at_margin"] = row.get("AT Margin")
        row["_eps_g5"] = row.get("EPS % Growth 5 Yr")
        row["_nh"] = 1 if row.get("RS Line New High") == "Yes" else 0
        row["_nl"] = 1 if row.get("RS Line New Low") == "Yes" else 0
        # Earnings Stability input: CV of quarterly EPS (more stable -> lower CV)
        if fq_eps and len(fq_eps) >= 5:
            vals = [v for v in fq_eps if v is not None]
            if len(vals) >= 5:
                arr = np.array(vals, dtype=float)
                mean = float(arr.mean())
                if abs(mean) > 1e-9:
                    row["_eps_cv"] = float(arr.std()) / abs(mean)

        if (i + 1) % 500 == 0 or i == total - 1:
            print(f"  processed {i + 1:,}/{total:,} ({n_ok:,} with price data)")

        # ratings columns from fundamentals only (no price data): EPS Rating is complete
        # on its own; SMR only gets its raw score here (still needs apply_rating_percentiles
        # for the final letter grade, same as every other ticker).
        if fund and not fp and with_ratings and not fund.get("error"):
            if fy_eps and fq_eps and len(fy_eps) >= 2 and len(fq_eps) >= 5:
                row["EPS Rating"] = calc_eps_rating(fy_eps, fq_eps, roe, _eps_extra_features(fund))
            sales_q0_yoy, sales_lt_growth, margin_now, margin_trend = extract_smr_inputs_from_fundamentals(fund)
            row["_smr_raw"] = calc_smr_raw_score(sales_q0_yoy, sales_lt_growth, margin_now, margin_trend, roe,
                                                  extract_info_features(fund))

        rows.append(row)

    out = pd.DataFrame(rows, columns=MS_COLUMNS + EXTRA_COLUMNS +
                       ["_rs_raw", "_rs3m_raw", "_rs6m_raw", "_ad_raw", "_ad_prev_raw", "_smr_raw",
                        "_rs_cur", "_rs_1w_ago", "_rs_3m_ago", "_rs_6m_ago",
                        "_mcap", "_pe", "_at_margin", "_eps_g5",
                        "_eps_cv", "_nh", "_nl"])

    # RS / A-D / SMR / Composite Rating: percentile-ranked against the current eligible
    # universe (needs the whole universe, same reason apply_group_columns does).  Must run
    # BEFORE apply_group_columns, since "Ind Group RS" is the group-mean of the FINAL RS
    # Rating (_rs_cur), which doesn't exist until this pass fills it in.
    out = apply_rating_percentiles(out)
    out["_rs_cur"] = out["RS Rating"]

    # group-wide / percentile-rank columns (needs the whole universe)
    out = apply_group_columns(out)

    # SPY 5-day change: same value for every row ("Index % Chg 5 Days")
    if len(spy_close) > 5 and float(spy_close.iloc[-6]) > 0:
        s5 = float(spy_close.iloc[-1]) / float(spy_close.iloc[-6]) - 1
        out["Index % Chg 5 Days"] = round(s5 * 100, 2)

    # date snapshot = most common last trading day
    asof = ""
    if last_dates:
        from collections import Counter
        asof = Counter(last_dates).most_common(1)[0][0]

    # drop the hidden per-ticker helper fields used by the group pass
    hidden = ["_rs_cur", "_rs_1w_ago", "_rs_3m_ago", "_rs_6m_ago",
              "_mcap", "_pe", "_at_margin", "_eps_g5", "_eps_cv", "_nh", "_nl"]
    out = out.drop(columns=[c for c in hidden if c in out.columns])

    # numeric coercion for the value columns we know are numeric
    _non_numeric = ("#", "Symbol", "Name", "Industry Name", "Sector",
                    "Company Description", "Ex-Dividend Date", "EPS Due Date",
                    "EPS Trl 4Q Gtr EPS 4 Yrs Ago", "EPS Lst Yr Gtr EPS 4 Yrs Ago",
                    "EPS Trl 4Q Geq EPS Lst Fiscal Yr", "3-Yr EPS Growth Geq 5-Yr",
                    "EPS % Chg Lst Q Gtr 3-Yr Growth", "Prof Marg Geq Ind Median",
                    "Current day's Volume greater than previous 10 days' Volume",
                    "Current day's Volume greater than previous 20 days' Volume",
                    "Current day's Volume greater than previous 5 days' Volume",
                    "50-Day > 150-Day > 200-Day", "10 Day > 21 Day > 50 Day",
                    "RS Line New Low", "RS Line New High", "RS Line Within 5% of New High",
                    "A/D Rating", "A/D Rating - Pr Wk", "SMR Rating", "Expected X Dividend Amount",
                    "First Cached Date")
    numeric_cols = [c for c in (MS_COLUMNS + EXTRA_COLUMNS)
                    if c not in SKIP_COLUMNS and c not in _non_numeric]
    for c in numeric_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out = out[MS_COLUMNS + EXTRA_COLUMNS]
    return out, asof, n_ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-ratings", action="store_true")
    args = ap.parse_args()

    out, asof, n_ok = build_screener(limit=args.limit, with_ratings=not args.no_ratings)
    total = len(out)

    print(f"\n✓ Built {total:,} rows "
          f"({n_ok:,} with cached price data, {total - n_ok:,} emitted blank) as of {asof or 'n/a'}")
    n_rating = int(out["RS Rating"].notna().sum())
    n_eps = int(out["EPS Rating"].notna().sum())
    n_fund = int(out["Market Cap (mil)"].notna().sum())
    print(f"  RS Rating filled: {n_rating:,} | EPS Rating filled: {n_eps:,} | "
          f"Market Cap filled: {n_fund:,}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    latest = OUTPUT_DIR / "daily_screener.csv"
    out.to_csv(latest, index=False)
    print(f"  saved {latest}")
    if asof:
        dated = OUTPUT_DIR / f"daily_screener_{asof}.csv"
        out.to_csv(dated, index=False)
        print(f"  saved {dated}")


if __name__ == "__main__":
    main()
