#!/usr/bin/env python3
"""
defeatbeta_source.py

Wrapper around defeatbeta-api, used as first-choice source for the financial
statements (fetch_fundamentals.py). Daily OHLCV price data is NO LONGER fetched
from defeatbeta-api: its snapshot lags the live session (verified 2026-08-10 -
the last bar was the prior Friday while yfinance carried the current day), so
ticker_cache updates use yfinance exclusively.

Returns None for: ETFs/benchmarks (defeatbeta-api has ~no fund coverage -
every sector SPDR, SMH/IGV/XBI/KRE/GDX/XRT/XOP/XHB, VTI/VOO/ARKK all returned
zero rows in testing, only SPY/QQQ/IWM/DIA worked), and the ~7% of stock/ADR
tickers it has no data for. get_price() is kept for ad-hoc use/tests but is not
wired into any cache-update path.

Import is wrapped to swallow defeatbeta-api's banner + nltk-download noise,
which fire unconditionally at package import time.
"""
import contextlib
import io

import pandas as pd

_TICKER_CLS = None
_IMPORT_FAILED = False


def _ticker_cls():
    global _TICKER_CLS, _IMPORT_FAILED
    if _TICKER_CLS is None and not _IMPORT_FAILED:
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                from defeatbeta_api.data.ticker import Ticker
            _TICKER_CLS = Ticker
        except Exception:
            _IMPORT_FAILED = True
    return _TICKER_CLS


def _clean_symbol(ticker):
    """BF.B / TAP.A class shares must be queried as BF-B / TAP-A - defeatbeta-api
    returns zero rows for the dotted form (verified 2026-08-09)."""
    return str(ticker).strip().replace(".", "-")


def _make_ticker(ticker):
    cls = _ticker_cls()
    if cls is None:
        return None
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return cls(_clean_symbol(ticker), log_level=30)  # WARNING: silence per-call cache logs
    except Exception:
        return None


REQ_COLS = ["Open", "High", "Low", "Close", "Volume"]


def get_price(ticker):
    """Full daily OHLCV history, shaped like <TICKER>_1d.parquet (Date index,
    Open/High/Low/Close/Volume columns). None if unavailable - caller falls
    back to yfinance."""
    t = _make_ticker(ticker)
    if t is None:
        return None
    try:
        p = t.price()
    except Exception:
        return None
    if p is None or p.empty or "report_date" not in p.columns:
        return None
    try:
        df = p.rename(columns={"report_date": "Date", "open": "Open", "high": "High",
                                "low": "Low", "close": "Close", "volume": "Volume"})
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df[[c for c in REQ_COLS if c in df.columns]]
        return df if not df.empty else None
    except Exception:
        return None


# ── financial statements ────────────────────────────────────────────────────

_STATEMENT_METHODS = {
    "income_q": "quarterly_income_statement",
    "income_a": "annual_income_statement",
    "balance_q": "quarterly_balance_sheet",
    "balance_a": "annual_balance_sheet",
    "cashflow_q": "quarterly_cash_flow",
    "cashflow_a": "annual_cash_flow",
}


def _canon_label(label):
    """Match yfinance's line-item spelling (Title Case, no apostrophes), since
    calc_ibd_ratings.py etc. look items up by exact yfinance-spelled string
    (e.g. "Stockholders Equity", not defeatbeta-api's "Stockholders' Equity")."""
    label = label.replace("’", "").replace("'", "")
    words = label.split(" ")
    out = [w if (w.isupper() and len(w) > 1) else (w[:1].upper() + w[1:]) for w in words]
    return " ".join(out)


def get_statement(ticker, kind):
    """One of income_q/income_a/balance_q/balance_a/cashflow_q/cashflow_a, in
    the {line_item: {date_str: value}, "_shape", "_columns"} shape
    fetch_fundamentals.py's _df_to_dict() produces from yfinance. None if
    unavailable - caller falls back to yfinance."""
    method = _STATEMENT_METHODS.get(kind)
    if method is None:
        return None
    t = _make_ticker(ticker)
    if t is None:
        return None
    try:
        df = getattr(t, method)().df()
    except Exception:
        return None
    if df is None or df.empty or "Breakdown" not in df.columns:
        return None
    # TTM is a defeatbeta-only convenience column; drop it so the remaining
    # period columns match what yfinance's quarterly/annual statements give.
    period_cols = [c for c in df.columns if c not in ("Breakdown", "TTM")]
    if not period_cols:
        return None
    try:
        out = {}
        for _, row in df.iterrows():
            label = _canon_label(str(row["Breakdown"]))
            vals = {}
            for col in period_cols:
                v = row[col]
                if isinstance(v, str):          # "*" placeholder for suppressed/missing data
                    v = None
                elif pd.isna(v):
                    v = None
                else:
                    v = float(v)
                vals[str(col)] = v
            out[label] = vals
        if not out:
            return None
        out["_shape"] = [len(out), len(period_cols)]
        out["_columns"] = [str(c) for c in period_cols]
        return out
    except Exception:
        return None
