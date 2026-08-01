import streamlit as st
from streamlit.components.v1 import html as st_html
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yfinance as yf
from pathlib import Path
import calendar
import glob
import subprocess
import sys
from datetime import datetime, timedelta
import json
import uuid
import os
import re
import html
import markdown as md_lib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pattern_recognition import PatternRecognizer
from pattern_painter import PatternPainter, build_lw_pattern_js

# ---------------------- Streamlit Compatibility ----------------------
def rerun_app():
    try:
        st.rerun()
    except AttributeError:
        st.experimental_rerun()

# ---------------------- Markers Persistence ----------------------
MARKERS_DIR = Path(__file__).resolve().parent / "markers"
MARKERS_DIR.mkdir(exist_ok=True)

def get_markers_file_path(ticker, chart_type="daily"):
    return MARKERS_DIR / f"{ticker}_{chart_type}_markers.json"

def load_markers(ticker, chart_type="daily"):
    filepath = get_markers_file_path(ticker, chart_type)
    if filepath.exists():
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_markers(ticker, markers, chart_type="daily"):
    filepath = get_markers_file_path(ticker, chart_type)
    try:
        with open(filepath, 'w') as f:
            json.dump(markers, f, indent=2)
        return True
    except Exception:
        return False

# ---------------------- IBD Live Summary Helpers ----------------------
IBD_LIVE_SUMMARY_DIR = Path(__file__).resolve().parent / "IBD" / "live_summaries"

@st.cache_data(ttl=300)
def load_ibd_live_summary_dates():
    """Return sorted list of date strings (YYYY-MM-DD) that have a summary (live and/or EOD) + json pair."""
    if not IBD_LIVE_SUMMARY_DIR.exists():
        return []
    dates = set()
    for p in IBD_LIVE_SUMMARY_DIR.glob("*.json"):
        stem = p.stem
        if stem.endswith("_eod"):
            stem = stem[:-4]
        dates.add(stem)
    return sorted(d for d in dates if len(d) == 10 and d[4] == "-" and d[7] == "-")

@st.cache_data(ttl=300)
def load_ibd_live_summary(date_str):
    """Load the (markdown_text, json_dict) pair for a given date string. Returns (None, None) if missing."""
    md_path = IBD_LIVE_SUMMARY_DIR / f"{date_str}.md"
    json_path = IBD_LIVE_SUMMARY_DIR / f"{date_str}.json"
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else None
    data = None
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
    return md_text, data

@st.cache_data(ttl=300)
def load_ibd_live_summary_headlines():
    """Map date_str -> market_summary headline, for calendar tooltips, without loading full markdown."""
    headlines = {}
    if not IBD_LIVE_SUMMARY_DIR.exists():
        return headlines
    for p in IBD_LIVE_SUMMARY_DIR.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            headlines[p.stem] = data.get("market_summary", "")
        except Exception:
            continue
    return headlines

@st.cache_data(ttl=300)
def load_all_ibd_live_sidecars():
    """Load every <date>.json sidecar. Returns dict date_str -> sidecar dict, sorted by date desc.
    Legacy summary_<date>.json files are skipped — every one of those dates now has a canonical
    <date>.json (with richer ticker_details), so including both would double-count shows."""
    sidecars = {}
    if not IBD_LIVE_SUMMARY_DIR.exists():
        return sidecars
    for p in IBD_LIVE_SUMMARY_DIR.glob("*.json"):
        if p.stem.startswith("summary_"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                sidecars[p.stem] = json.load(f)
        except Exception:
            continue
    return dict(sorted(sidecars.items(), reverse=True))

def get_ticker_transcript_mentions(ticker):
    """All (date, actionability, technical_action, story) rows across every show that mentioned
    ticker, newest first. Also includes shows where the ticker only appears in the consolidated
    ticker list (listed_only=True, no detail row) so recent appearances are never missed."""
    ticker = (ticker or "").strip().upper()
    mentions = []
    for date_str, sidecar in load_all_ibd_live_sidecars().items():
        detail = (sidecar.get("ticker_details") or {}).get(ticker)
        if detail:
            mentions.append({"date": date_str, **detail, "listed_only": False})
        elif ticker in (sidecar.get("tickers") or []):
            mentions.append({
                "date": date_str,
                "actionability": "",
                "technical_action": "",
                "story": "",
                "listed_only": True,
                "market_summary": sidecar.get("market_summary", ""),
            })
    return mentions

def get_all_ibd_live_tickers():
    """Union of every ticker mentioned across all synced IBD Live shows, alphabetically sorted."""
    tickers = set()
    for sidecar in load_all_ibd_live_sidecars().values():
        tickers.update(t.upper() for t in sidecar.get("tickers", []) if t)
    return sorted(tickers)

def sync_ibd_live_summaries_once():
    """Pull any new/updated Zoom summaries into IBD/live_summaries. Runs once per browser
    session (on launch or full refresh), not on every widget-triggered rerun."""
    if st.session_state.get("_ibd_live_synced"):
        return
    st.session_state._ibd_live_synced = True
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "python"))
        from sync_ibd_live_summaries import sync_ibd_live_summaries
        synced, skipped, synced_dates = sync_ibd_live_summaries(verbose=False)
        if synced:
            load_ibd_live_summary_dates.clear()
            load_ibd_live_summary.clear()
            load_ibd_live_summary_headlines.clear()
            load_all_ibd_live_sidecars.clear()
        st.session_state._ibd_live_sync_result = (synced, skipped, synced_dates)
    except Exception as e:
        st.session_state._ibd_live_sync_result = (0, 0, [])
        st.session_state._ibd_live_sync_error = str(e)

sync_ibd_live_summaries_once()

# ---------------------- IBD Live Ingest / End-of-Day Helpers ----------------------
def save_ibd_live_summary_from_text(date_str, md_text, suffix=""):
    """Write a pasted markdown summary to IBD/live_summaries and derive its JSON sidecar
    using the same parser as python/sync_ibd_live_summaries.py.

    suffix: '' for the intraday/live summary, '_eod' for the end-of-day summary.
    Returns (ok, message, sidecar).
    """
    md_text = (md_text or "").strip()
    if not md_text:
        return False, "Summary text is empty — nothing saved.", None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str or ""):
        return False, "Invalid date; expected YYYY-MM-DD.", None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "python"))
        from sync_ibd_live_summaries import build_sidecar
    except Exception as e:
        return False, f"Could not load parser from sync_ibd_live_summaries.py: {e}", None
    IBD_LIVE_SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    md_path = IBD_LIVE_SUMMARY_DIR / f"{date_str}{suffix}.md"
    json_path = IBD_LIVE_SUMMARY_DIR / f"{date_str}{suffix}.json"
    try:
        md_path.write_text(md_text + "\n", encoding="utf-8")
        sidecar = build_sidecar(date_str, md_path, None)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2)
    except Exception as e:
        return False, f"Failed to write/reformat summary: {e}", None
    load_ibd_live_summary_dates.clear()
    load_ibd_live_summary.clear()
    load_ibd_live_summary_headlines.clear()
    load_all_ibd_live_sidecars.clear()
    kind = "End of Day" if suffix == "_eod" else "Live"
    return True, f"{kind} summary for {date_str} saved & reformatted ({len(sidecar.get('tickers', []))} tickers).", sidecar

def load_ibd_live_eod_summary(date_str):
    """Load the end-of-day summary pair for a date. Returns (md_text, sidecar) or (None, None)."""
    md_path = IBD_LIVE_SUMMARY_DIR / f"{date_str}_eod.md"
    json_path = IBD_LIVE_SUMMARY_DIR / f"{date_str}_eod.json"
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else None
    data = None
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
    return md_text, data

def load_ibd_live_combined_sidecar(date_str):
    """Merge the intraday (<date>.json) and end-of-day (<date>_eod.json) sidecars for a date.
    EOD tickers are appended (de-duplicated) to the intraday ticker list so the plot view
    shows both; EOD ticker details win on key collisions."""
    md_text, sidecar = load_ibd_live_summary(date_str)
    eod_md, eod_sidecar = load_ibd_live_eod_summary(date_str)
    combined = dict(sidecar or {})
    if eod_sidecar:
        tickers = list(combined.get("tickers", []))
        for t in eod_sidecar.get("tickers", []):
            t = (t or "").strip().upper()
            if t and t not in tickers:
                tickers.append(t)
        combined["tickers"] = tickers
        details = dict(combined.get("ticker_details", {}))
        details.update(eod_sidecar.get("ticker_details", {}))
        combined["ticker_details"] = details
        if not combined.get("market_summary") and eod_sidecar.get("market_summary"):
            combined["market_summary"] = eod_sidecar["market_summary"]
    return md_text, combined, eod_md, eod_sidecar

# ---------------------- IBD Live Ticker Comments Persistence ----------------------
IBD_COMMENTS_PATH = Path(__file__).resolve().parent / "IBD" / "comments.json"

def load_ticker_comments():
    """Load user comments as {ticker: [{date, text}, ...]}."""
    if IBD_COMMENTS_PATH.exists():
        try:
            with open(IBD_COMMENTS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_ticker_comment(ticker, date_str, text):
    """Append a user comment for ticker and persist to disk."""
    ticker = (ticker or "").strip().upper()
    if not ticker or not text.strip():
        return False
    IBD_COMMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    all_comments = load_ticker_comments()
    all_comments.setdefault(ticker, []).append({"date": date_str, "text": text.strip()})
    try:
        with open(IBD_COMMENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(all_comments, f, indent=2)
        return True
    except Exception:
        return False

def delete_ticker_comment(ticker, index):
    """Remove the comment at index for ticker and persist."""
    ticker = (ticker or "").strip().upper()
    all_comments = load_ticker_comments()
    entries = all_comments.get(ticker, [])
    if 0 <= index < len(entries):
        entries.pop(index)
        all_comments[ticker] = entries
        with open(IBD_COMMENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(all_comments, f, indent=2)
        return True
    return False

def is_boilerplate_mention(entry):
    """Detect LLM-filler transcript rows: earlier summaries occasionally regurgitated the
    summarization prompt's template phrases verbatim (e.g. 'Testing key moving averages ...;
    base consolidation or breakout pattern' / 'Fundamental growth story, quarterly earnings
    beat/acceleration ...'), producing identical-looking mentions across many dates."""
    blob = " ".join([
        (entry.get("actionability") or ""),
        (entry.get("technical_action") or ""),
        (entry.get("story") or ""),
    ]).lower()
    markers = [
        "testing key moving averages",
        "base consolidation or breakout pattern",
        "fundamental growth story",
        "quarterly earnings beat/acceleration",
        "sector catalyst discussed on show",
        "revenue expansion, or sector",
    ]
    return sum(1 for m in markers if m in blob) >= 2

def get_ticker_comment_timeline(ticker):
    """Merge transcript mentions + user comments for ticker into one newest-first timeline."""
    ticker = (ticker or "").strip().upper()
    timeline = []
    for m in get_ticker_transcript_mentions(ticker):
        timeline.append({
            "date": m["date"],
            "source": "Transcript",
            "text": f"[{m.get('actionability', '')}] {m.get('technical_action', '')} — {m.get('story', '')}".strip(" —"),
            "actionability": m.get("actionability", ""),
            "technical_action": m.get("technical_action", ""),
            "story": m.get("story", ""),
            "listed_only": m.get("listed_only", False),
            "boilerplate": is_boilerplate_mention(m),
        })
    for i, c in enumerate(load_ticker_comments().get(ticker, [])):
        timeline.append({"date": c.get("date", ""), "source": "You", "text": c.get("text", ""), "_idx": i})
    timeline.sort(key=lambda e: e["date"], reverse=True)
    return timeline

def render_comment_timeline(timeline, comment_ticker, latest_show_date=None):
    """Render the merged transcript + user-comment timeline as styled cards.
    Boilerplate (LLM-filler) transcript rows are collapsed into a single dimmed card.
    Returns (n_real_transcript_mentions, n_user_comments, latest_mention_date, n_boilerplate)."""
    real_trans = [e for e in timeline
                  if e["source"] == "Transcript" and not e.get("listed_only") and not e.get("boilerplate")]
    listed_only = [e for e in timeline
                   if e["source"] == "Transcript" and e.get("listed_only")]
    boilerplate = [e for e in timeline
                   if e["source"] == "Transcript" and e.get("boilerplate")]
    user_entries = [e for e in timeline if e["source"] == "You"]
    n_trans = len(real_trans)
    n_you = len(user_entries)
    latest_mention = next((e["date"] for e in timeline if e["date"]), "")
    if n_trans and latest_show_date and latest_mention:
        current = latest_mention == latest_show_date
        badge_color = "#1f9d55" if current else "#e1a200"
        badge_text = (f"Up to date — mentioned on the latest show ({latest_show_date})"
                      if current
                      else f"Last seen {latest_mention} — latest show is {latest_show_date}")
        st.markdown(
            f"""
            <div style="display:flex; align-items:center; gap:10px; margin:0 0 10px 0; flex-wrap:wrap;">
                <span style="background:{badge_color}; color:#fff; font-size:12px; font-weight:700; padding:3px 12px; border-radius:20px;">🔵 {badge_text}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    for entry in real_trans:
        act = entry.get("actionability", "")
        pill_color = ('#1f9d55' if act == 'Actionable'
                      else '#1f77b4' if act == 'Watchlist' else '#6c757d')
        st.markdown(
            f"""
            <div style="background:#f4f8fd; border:1px solid #e2e7ee; border-left:4px solid #1f77b4;
                        border-radius:8px; padding:10px 14px; margin-bottom:8px;
                        font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
                <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:4px;">
                    <span style="font-size:12px; font-weight:700; color:#6c757d;">📅 {html.escape(entry['date'])}</span>
                    <span style="font-size:11px; font-weight:700; color:#1f77b4; text-transform:uppercase; letter-spacing:0.5px;">🎙️ Transcript</span>
                    <span style="background:{pill_color}; color:#fff; font-size:11px; font-weight:700; padding:1px 10px; border-radius:20px;">{html.escape(act) if act else '—'}</span>
                </div>
                <div style="font-size:14px; font-weight:700; color:#0a1f3d;">{html.escape(entry.get('technical_action', ''))}</div>
                <div style="font-size:13px; color:#1c2733; line-height:1.5;">{html.escape(entry.get('story', ''))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    for entry in listed_only:
        cols = st.columns([5, 1])
        with cols[0]:
            st.markdown(
                f"""
                <div style="background:#eef2f7; border:1px solid #e2e7ee; border-left:4px solid #8ea3bb;
                            border-radius:8px; padding:8px 14px; margin-bottom:8px;
                            font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
                    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:2px;">
                        <span style="font-size:12px; font-weight:700; color:#6c757d;">📅 {html.escape(entry['date'])}</span>
                        <span style="font-size:11px; font-weight:700; color:#5a7186; text-transform:uppercase; letter-spacing:0.5px;">🎙️ Transcript</span>
                        <span style="font-size:11px; font-weight:600; color:#5a7186;">Mentioned in the show's ticker list</span>
                    </div>
                    <div style="font-size:12px; color:#5a7186; line-height:1.4;">{html.escape(entry.get('market_summary', '')) or ''}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with cols[1]:
            if st.button("View show", key=f"ibd_live_view_show_{comment_ticker}_{entry['date']}",
                         help=f"Jump to the {entry['date']} show"):
                st.session_state.ibd_live_list_date = entry["date"]
                rerun_app()
    if boilerplate:
        bp_dates = ", ".join(e["date"] for e in boilerplate)
        st.markdown(
            f"""
            <div style="background:#fbf6ee; border:1px solid #efe2c8; border-left:4px solid #e1a200;
                        border-radius:8px; padding:8px 14px; margin-bottom:8px;
                        font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
                <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:2px;">
                    <span style="font-size:12px; font-weight:700; color:#b08900;">⚠️ {len(boilerplate)} low-detail generic mention{'s' if len(boilerplate) != 1 else ''}</span>
                    <span style="font-size:11px; font-weight:600; color:#a09000;">from summaries that reused template text</span>
                </div>
                <div style="font-size:12px; color:#7a6a00; line-height:1.4;">
                    Listed on: <b>{bp_dates}</b> — no real detail recorded. Re-run the summarizer for those dates to backfill.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    for entry in user_entries:
        cols = st.columns([5, 1])
        with cols[0]:
            st.markdown(
                f"""
                <div style="background:#fff7ef; border:1px solid #f2dfcf; border-left:4px solid #ff6b1a;
                            border-radius:8px; padding:10px 14px; margin-bottom:8px;
                            font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
                    <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
                        <span style="font-size:12px; font-weight:700; color:#6c757d;">📅 {html.escape(entry['date'])}</span>
                        <span style="font-size:11px; font-weight:700; color:#ff6b1a; text-transform:uppercase; letter-spacing:0.5px;">✍️ You</span>
                    </div>
                    <div style="font-size:13px; color:#1c2733; line-height:1.5;">{html.escape(entry['text'])}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with cols[1]:
            if st.button("🗑️", key=f"ibd_live_del_comment_{comment_ticker}_{entry['_idx']}",
                         help="Delete this comment"):
                delete_ticker_comment(comment_ticker, entry["_idx"])
                rerun_app()
    return n_trans, n_you, latest_mention, len(boilerplate)

def get_all_commented_or_mentioned_tickers():
    """Union of tickers with a user comment and tickers mentioned in any transcript summary."""
    tickers = set(load_ticker_comments().keys()) | set(get_all_ibd_live_tickers())
    return sorted(tickers)

def format_report_date(date_str):
    """Turn 'YYYY-MM-DD' into a friendly 'Thursday, July 30, 2026' label."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%A, %B %-d, %Y").replace(" %-d", f" {d.day}")
    except Exception:
        return date_str

def render_ibd_live_report(md_text, date_str="", market_summary="", report_title="DAILY MARKET REPORT"):
    """Render an IBD Live markdown summary as a styled, IBD-report-style HTML card."""
    if not md_text:
        return ""
    try:
        body_html = md_lib.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:
        body_html = f"<pre>{html.escape(md_text)}</pre>"
    body_html = re.sub(r"<h1[^>]*>.*?</h1>", "", body_html, flags=re.S).strip()

    tape_html = ""
    if market_summary:
        tape_html = f'<div class="ibd-tape">🎙️ <b>Today\'s Tape</b><br>{html.escape(market_summary)}</div>'

    return f"""
    <style>
    .ibd-report {{
        font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
        background: #ffffff;
        border: 1px solid #dfe3ea;
        border-radius: 10px;
        overflow: hidden;
        margin: 6px 0 14px 0;
        box-shadow: 0 1px 4px rgba(15, 40, 75, 0.08);
    }}
    .ibd-report .ibd-brand {{
        background: linear-gradient(120deg, #0a1f3d 0%, #143a63 100%);
        color: #ffffff;
        padding: 16px 22px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 8px;
    }}
    .ibd-report .ibd-brand .brand-name {{
        font-size: 18px;
        font-weight: 800;
        letter-spacing: 0.6px;
    }}
    .ibd-report .ibd-brand .brand-name span {{ color: #ffb347; }}
    .ibd-report .ibd-brand .brand-date {{
        font-size: 13px;
        font-weight: 600;
        background: rgba(255,255,255,0.14);
        padding: 5px 14px;
        border-radius: 20px;
        white-space: nowrap;
    }}
    .ibd-report .ibd-accent {{
        height: 5px;
        background: linear-gradient(90deg, #ff6b1a, #ffb347);
    }}
    .ibd-report .ibd-body {{
        padding: 18px 24px 24px 24px;
        color: #1c2733;
        line-height: 1.6;
        font-size: 14px;
    }}
    .ibd-report .ibd-tape {{
        background: #eef4fb;
        border-left: 5px solid #1f77b4;
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 4px;
        font-size: 14px;
        color: #0b2c4d;
    }}
    .ibd-report h2 {{
        font-size: 15px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        color: #0a1f3d;
        border-bottom: 2px solid #ff6b1a;
        padding-bottom: 6px;
        margin-top: 26px;
        margin-bottom: 10px;
    }}
    .ibd-report h3 {{
        font-size: 14px;
        font-weight: 700;
        color: #143a63;
        margin-top: 18px;
        margin-bottom: 8px;
    }}
    .ibd-report p {{ margin: 8px 0; }}
    .ibd-report table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
        margin: 10px 0;
    }}
    .ibd-report th {{
        background: #0a1f3d;
        color: #ffffff;
        text-align: left;
        padding: 8px 10px;
        font-weight: 700;
        border: 1px solid #0a1f3d;
    }}
    .ibd-report td {{
        padding: 7px 10px;
        border: 1px solid #e2e7ee;
        vertical-align: top;
    }}
    .ibd-report tr:nth-child(even) td {{ background: #f7f9fc; }}
    .ibd-report strong {{ color: #0a1f3d; }}
    .ibd-report hr {{
        border: none;
        border-top: 1px dashed #c9d3e0;
        margin: 20px 0;
    }}
    .ibd-report blockquote {{
        background: #fdf4ec;
        border-left: 4px solid #ff6b1a;
        margin: 10px 0;
        padding: 8px 14px;
        border-radius: 4px;
    }}
    .ibd-report blockquote p {{ margin: 0; }}
    .ibd-report li {{ margin: 3px 0; }}
    .ibd-report ul, .ibd-report ol {{ padding-left: 22px; }}
    </style>
    <div class="ibd-report">
        <div class="ibd-brand">
            <div class="brand-name">IBD LIVE <span>· {html.escape(report_title)}</span></div>
            <div class="brand-date">📅 {format_report_date(date_str)}</div>
        </div>
        <div class="ibd-accent"></div>
        <div class="ibd-body">
            {tape_html}
            {body_html}
        </div>
    </div>
    """

# ---------------------- IBD Live SPY Day-Picker Chart ----------------------
def build_spy_summary_chart(summary_dates, selected_date="", height=520):
    """SPY daily candlestick + volume chart. Black dots sit on top of candles whose day
    has an IBD Live summary. Every bar carries customdata=[date_str] so clicking/selecting
    a bar can be mapped back to that day's summary."""
    df = load_or_fetch_ticker("SPY", interval="1d", period="1y")
    if df is not None and not df.empty and getattr(df.index, 'tz', None) is not None:
        df.index = df.index.tz_localize(None)
    # Cache can lag up to ~2 days; top the chart up with fresh SPY bars so the most
    # recent summary days still appear (and stay clickable).
    try:
        if summary_dates:
            max_sum = max(pd.to_datetime(d) for d in summary_dates)
            if df is not None and not df.empty and df.index.max() < max_sum:
                recent = yf.Ticker("SPY").history(period="15d", interval="1d")
                if recent is not None and not recent.empty:
                    if getattr(recent.index, 'tz', None) is not None:
                        recent.index = recent.index.tz_localize(None)
                    df = pd.concat([df, recent])
                    df = df[~df.index.duplicated(keep='first')].sort_index()
                    try:
                        cache_path = get_ticker_cache_path("SPY", "1d")
                        if cache_path.parent.exists():
                            df.to_parquet(cache_path)
                    except Exception:
                        pass
    except Exception:
        pass
    if df is None or df.empty:
        return None, "Could not load SPY data."
    summary_set = set(summary_dates or [])
    sub = df.tail(252).copy()
    if len(sub) < 5:
        return None, "Not enough SPY data."
    if getattr(sub.index, 'tz', None) is not None:
        sub.index = sub.index.tz_localize(None)

    dates = [d.strftime("%Y-%m-%d") for d in sub.index]

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04,
        subplot_titles=("SPY Daily — click a bar to open that day's summary", "Volume"),
        row_heights=[0.72, 0.28],
    )
    fig.add_trace(go.Candlestick(
        x=sub.index, open=sub['Open'], high=sub['High'], low=sub['Low'], close=sub['Close'],
        name='SPY', showlegend=False,
        customdata=[[d] for d in dates],
    ), row=1, col=1)

    hi = sub['High']
    dot_offset = float(hi.max() - hi.min()) * 0.02 if len(hi) > 1 else 0.5
    dot_x, dot_y = [], []
    for i, d in enumerate(dates):
        if d in summary_set:
            dot_x.append(sub.index[i])
            dot_y.append(float(hi.iloc[i]) + dot_offset)
    if dot_x:
        fig.add_trace(go.Scatter(
            x=dot_x, y=dot_y, mode='markers',
            marker=dict(symbol='circle', size=5, color='black', line=dict(width=1, color='white')),
            name='Summary available',
            hovertemplate='%{customdata[0]} — summary available<extra></extra>',
            customdata=[[d] for d in dot_x],
        ), row=1, col=1)

    up = (sub['Close'] >= sub['Open']).astype(bool)
    vol_colors = ['#26a69a' if u else '#ef5350' for u in up]
    fig.add_trace(go.Bar(
        x=sub.index, y=sub['Volume'], marker=dict(color=vol_colors),
        name='Volume', showlegend=False,
        customdata=[[d] for d in dates],
    ), row=2, col=1)

    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_layout(
        xaxis_rangeslider_visible=False, height=height,
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        template="plotly_dark",
    )
    if selected_date:
        try:
            sel_dt = pd.to_datetime(selected_date)
            if sub.index.min() <= sel_dt <= sub.index.max():
                fig.add_vline(x=sel_dt, line_width=1, line_dash="dot",
                              line_color="#ff6b1a", opacity=0.6, row=1, col=1)
        except Exception:
            pass
    return fig, None

def process_spy_selection(event, available_set):
    """Read the plotly selection event; if a bar whose date has a summary was selected,
    jump the IBD Live view to that date."""
    if not event:
        return False
    if isinstance(event, dict):
        sel = event.get("selection") or event
        points = sel.get("points") or event.get("points") or []
    else:
        points = []
    for pt in points:
        if not isinstance(pt, dict):
            continue
        cd = pt.get("customdata")
        if not cd:
            continue
        d = cd[0] if isinstance(cd, list) else cd
        if isinstance(d, str) and d in available_set:
            if st.session_state.get("ibd_live_selected_date") != d:
                st.session_state.ibd_live_selected_date = d
                st.session_state.ibd_live_list_date = None
            return True
    return False

# ---------------------- Daily Report Card (DRC) & GMI PDF Helpers ----------------------
DAILY_DIR = Path(__file__).resolve().parent / "daily"
CHARTS_DIR = DAILY_DIR / "charts"
SCREENSHOTS_DIR = DAILY_DIR / "screenshots"

def ensure_daily_dirs():
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

def get_effective_pdf_date(selected_date):
    """
    Checks if selected_date is today in EST.
    If before 7:00 PM EST (19:00), fallback to previous trading day for PDF.
    """
    try:
        import zoneinfo
        est_tz = zoneinfo.ZoneInfo("America/New_York")
        now_est = datetime.now(est_tz)
    except Exception:
        now_est = datetime.utcnow() - timedelta(hours=4)
        
    today_est = now_est.date()
    
    if selected_date == today_est and now_est.hour < 19:
        yesterday = selected_date - timedelta(days=1)
        while yesterday.weekday() in (5, 6):
            yesterday -= timedelta(days=1)
        reason = f"Today's report will be published after 7:00 PM EST (current EST: {now_est.strftime('%I:%M %p')}). Showing report from previous trading day ({yesterday.strftime('%m/%d/%Y')})."
        return yesterday, True, reason
        
    return selected_date, False, ""

def fetch_daily_gmi_pdf(selected_date):
    ensure_daily_dirs()
    yyyy = selected_date.strftime("%Y")
    mm = selected_date.strftime("%m")
    dd = selected_date.strftime("%d")
    yy = selected_date.strftime("%y")
    
    pdf_filename = f"DailyGMI_{mm}{dd}{yy}.pdf"
    local_pdf_path = DAILY_DIR / pdf_filename
    url = f"https://www.investors.com/wp-content/uploads/{yyyy}/{mm}/{pdf_filename}"
    
    if local_pdf_path.exists() and local_pdf_path.stat().st_size > 1000:
        return str(local_pdf_path), url, True, "Loaded PDF from local daily folder."
        
    try:
        import requests
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200 and res.content.startswith(b"%PDF"):
            with open(local_pdf_path, 'wb') as f:
                f.write(res.content)
            return str(local_pdf_path), url, True, "Downloaded PDF successfully from Investors.com!"
        else:
            return str(local_pdf_path), url, False, f"HTTP {res.status_code}: File not found on Investors.com (may be a weekend or non-trading day)."
    except Exception as e:
        return str(local_pdf_path), url, False, f"Download error: {e}"

def generate_ticker_png_chart(ticker: str, date_str: str) -> str:
    ticker = ticker.strip().upper()
    if not ticker:
        return None
    
    ensure_daily_dirs()
    safe_date = date_str.replace("-", "")
    output_png = CHARTS_DIR / f"{ticker}_{safe_date}.png"
    
    df = load_or_fetch_ticker(ticker, interval="1d", period="1y")
    if df is None or df.empty:
        return None
    
    sub = df.tail(120).copy()
    if len(sub) < 5:
        return None
        
    close = sub['Close']
    sub['EMA10'] = close.ewm(span=10, adjust=False).mean()
    sub['EMA21'] = close.ewm(span=21, adjust=False).mean()
    sub['SMA50'] = close.rolling(50, min_periods=1).mean()
    sub['Vol_SMA50'] = sub['Volume'].rolling(50, min_periods=1).mean()
    
    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(9, 5.5), gridspec_kw={'height_ratios': [3, 1]}, sharex=True
    )
    fig.patch.set_facecolor('#1e1e1e')
    for ax in (ax_price, ax_vol):
        ax.set_facecolor('#1e1e1e')
        ax.tick_params(colors='#d1d4dc')
        ax.spines['bottom'].set_color('#363c4e')
        ax.spines['top'].set_color('#363c4e')
        ax.spines['left'].set_color('#363c4e')
        ax.spines['right'].set_color('#363c4e')
        ax.grid(True, color='#2a2e39', linestyle='--', alpha=0.5)

    dates = sub.index
    up_mask = sub['Close'] >= sub['Open']
    down_mask = sub['Close'] < sub['Open']
    
    ax_price.vlines(dates[up_mask], sub['Low'][up_mask], sub['High'][up_mask], color='#26a69a', linewidth=1)
    ax_price.vlines(dates[down_mask], sub['Low'][down_mask], sub['High'][down_mask], color='#ef5350', linewidth=1)
    
    body_bottom = np.where(up_mask, sub['Open'], sub['Close'])
    body_height = np.abs(sub['Close'] - sub['Open'])
    body_height = np.where(body_height == 0, 0.01, body_height)
    
    ax_price.bar(dates[up_mask], body_height[up_mask], bottom=body_bottom[up_mask], color='#26a69a', width=0.6, align='center')
    ax_price.bar(dates[down_mask], body_height[down_mask], bottom=body_bottom[down_mask], color='#ef5350', width=0.6, align='center')
    
    ax_price.plot(dates, sub['EMA10'], color='#FF9800', label='EMA 10', linewidth=1.5)
    ax_price.plot(dates, sub['EMA21'], color='#2196F3', label='EMA 21', linewidth=1.5)
    ax_price.plot(dates, sub['SMA50'], color='#F44336', label='SMA 50', linewidth=1.5)
    
    latest_close = sub['Close'].iloc[-1]
    ax_price.set_title(f"{ticker} Daily Chart - ${latest_close:.2f}", color='#ffffff', fontsize=13, fontweight='bold', pad=8)
    ax_price.legend(loc='upper left', facecolor='#2a2e39', edgecolor='none', labelcolor='#ffffff', fontsize=8)
    
    ax_vol.bar(dates[up_mask], sub['Volume'][up_mask], color='#26a69a', alpha=0.7, width=0.6)
    ax_vol.bar(dates[down_mask], sub['Volume'][down_mask], color='#ef5350', alpha=0.7, width=0.6)
    ax_vol.plot(dates, sub['Vol_SMA50'], color='#FF9800', label='Vol SMA 50', linewidth=1.2)
    ax_vol.set_ylabel('Volume', color='#d1d4dc', fontsize=8)
    
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
    fig.autofmt_xdate()
    
    plt.tight_layout()
    plt.savefig(output_png, dpi=150, facecolor=fig.get_facecolor(), bbox_inches='tight')
    plt.close(fig)
    
    return str(output_png)

def render_tradingview_ticker_chart(ticker, max_days=190, height=750):
    tk = ticker.strip().upper()
    df_daily = load_or_fetch_ticker(tk)
    if df_daily is None or df_daily.empty:
        st.warning(f"Could not load market data for ticker {tk}.")
        return

    # Trim to 9 months daily worth of data (~190 trading days)
    if len(df_daily) > max_days:
        df_daily = df_daily.tail(max_days).copy()
    else:
        df_daily = df_daily.copy()

    # Look up IBD Industry Group / Sector
    ibd_map = load_ibd_ticker_industry_mapping()
    ibd_sector = ibd_map.get(tk) or ibd_map.get(tk.replace(".", "").replace("-", "").replace("/", ""))
    
    # Look up RS Rating & Company metrics from main dataset if available
    rs_rating_str = "N/A"
    sector_str = ibd_sector or ""
    latest_price = df_daily['Close'].iloc[-1] if not df_daily.empty else 0.0
    
    try:
        main_df = globals().get('df') if 'df' in globals() else globals().get('filtered_df')
        if main_df is not None and not main_df.empty and 'Ticker' in main_df.columns:
            m_rows = main_df[main_df['Ticker'] == tk]
            if not m_rows.empty:
                r_row = m_rows.iloc[-1]
                p_val = r_row.get('Percentile') if 'Percentile' in r_row else r_row.get('Relative Strength')
                if pd.notna(p_val):
                    try:
                        rs_rating_str = f"{int(round(float(p_val)))}"
                    except Exception:
                        rs_rating_str = str(p_val)
                if not sector_str:
                    s_val = r_row.get('Sector') or r_row.get('Industry')
                    if pd.notna(s_val):
                        sector_str = str(s_val)
    except Exception:
        pass

    # Look up Company metrics from IBD Data Tables if available
    ibd_comp = "N/A"
    ibd_rs = rs_rating_str  # default to the local RS
    ibd_ind_rank = "N/A"
    ibd_eps_rating = "N/A"
    ibd_smr_rating = "N/A"
    ibd_last_qtr_eps = "N/A"
    ibd_last_qtr_sales = "N/A"
    ibd_curr_qtr_est = "N/A"
    ibd_curr_yr_est = "N/A"
    
    ibd_full_map = load_ibd_data_tables_full()
    ibd_info = ibd_full_map.get(tk) or ibd_full_map.get(tk_norm)
    if ibd_info:
        if pd.notna(ibd_info.get('IBD Comp. Rating')):
            ibd_comp = str(int(ibd_info['IBD Comp. Rating']))
        if pd.notna(ibd_info.get('RS Rating')):
            ibd_rs = str(int(ibd_info['RS Rating']))
        if pd.notna(ibd_info.get('Industry Group Rank')):
            ibd_ind_rank = str(int(ibd_info['Industry Group Rank']))
        if pd.notna(ibd_info.get('EPS Rating')):
            ibd_eps_rating = str(int(ibd_info['EPS Rating']))
        if pd.notna(ibd_info.get('SMR Rating')):
            ibd_smr_rating = str(ibd_info['SMR Rating'])
        if pd.notna(ibd_info.get('Last Qtr EPS % Chg.')):
            ibd_last_qtr_eps = f"{ibd_info['Last Qtr EPS % Chg.']}%"
        if pd.notna(ibd_info.get('Last Qtr Sales % Chg.')):
            ibd_last_qtr_sales = f"{ibd_info['Last Qtr Sales % Chg.']}%"
        if pd.notna(ibd_info.get('Curr Qtr EPS Est. % Chg.')):
            ibd_curr_qtr_est = f"{ibd_info['Curr Qtr EPS Est. % Chg.']}%"
        if pd.notna(ibd_info.get('Curr Yr EPS Est. % Chg.')):
            ibd_curr_yr_est = f"{ibd_info['Curr Yr EPS Est. % Chg.']}%"

    # Display RS metrics header banner
    m_col1, m_col2, m_col3, m_col4, m_col5, m_col6 = st.columns(6)
    with m_col1:
        st.metric("Ticker", tk)
    with m_col2:
        st.metric("Latest Price", f"${latest_price:.2f}")
    with m_col3:
        st.metric("IBD Comp Rating", f"{ibd_comp} / 99")
    with m_col4:
        st.metric("RS Rating", f"{ibd_rs} / 99")
    with m_col5:
        st.metric("Ind Grp Rank", f"{ibd_ind_rank} / 197")
    with m_col6:
        st.metric("IBD Industry / Sector", sector_str or "N/A")

    e_col1, e_col2, e_col3, e_col4, e_col5, e_col6 = st.columns(6)
    with e_col1:
        st.metric("EPS Rating", f"{ibd_eps_rating} / 99")
    with e_col2:
        st.metric("SMR Rating", ibd_smr_rating)
    with e_col3:
        st.metric("Last Qtr EPS Chg", ibd_last_qtr_eps)
    with e_col4:
        st.metric("Last Qtr Sales Chg", ibd_last_qtr_sales)
    with e_col5:
        st.metric("Curr Qtr EPS Est", ibd_curr_qtr_est)
    with e_col6:
        st.metric("Curr Yr EPS Est", ibd_curr_yr_est)

    # Look up Company Description from IBD
    desc_map = load_company_descriptions()
    tk_norm = tk.replace(".", "").replace("-", "").replace("/", "").replace(" ", "").upper()
    comp_desc = desc_map.get(tk) or desc_map.get(tk_norm)
    if comp_desc and comp_desc != 'nan':
        st.markdown(
            f"""
            <div style="background-color: #f0f2f6; border-left: 5px solid #1f77b4; padding: 12px 16px; border-radius: 4px; margin-bottom: 12px;">
                <span style="font-size: 16px; color: #000000; line-height: 1.5; display: inline-block;">{comp_desc}</span>
            </div>
            """,
            unsafe_allow_html=True
        )

    # Load SPY for RS calculation
    spy_df = load_or_fetch_ticker("SPY")
    if spy_df is not None and not spy_df.empty:
        spy_aligned = spy_df.reindex(df_daily.index).ffill().bfill()
        df_daily['rs_raw'] = (df_daily['Close'] / df_daily['Close'].iloc[0]) / (spy_aligned['Close'] / spy_aligned['Close'].iloc[0])
    else:
        df_daily['rs_raw'] = df_daily['Close'] / df_daily['Close'].iloc[0]

    df_daily['rs_ema_quick']     = df_daily['rs_raw'].ewm(span=21, adjust=False).mean()
    df_daily['rs_ema_quicksand'] = df_daily['rs_raw'].ewm(span=34, adjust=False).mean()
    df_daily['rs_ema_gd']        = df_daily['rs_raw'].ewm(span=50, adjust=False).mean()

    ema10 = df_daily['Close'].ewm(span=10, adjust=False).mean()
    ema21 = df_daily['Close'].ewm(span=21, adjust=False).mean()
    sma50 = df_daily['Close'].rolling(50).mean()
    sma200 = df_daily['Close'].rolling(200).mean()

    vol = df_daily['Volume'].astype(float)
    close = df_daily['Close'].astype(float)
    avg_vol = vol.rolling(50, min_periods=1).mean()
    avg_vol_safe = avg_vol.replace(0, np.nan)
    vol_ratio = (vol / avg_vol_safe * 100).fillna(0)

    dry_lvl1 = (vol_ratio < 50).astype(bool)
    dry_lvl2 = (vol_ratio < 30).astype(bool)
    up_day   = (close > close.shift(1)).astype(bool)

    vol_colors = [
        'rgba(247,153,2,0.7)' if dry_lvl2.loc[idx]
        else 'rgba(225,181,69,0.6)' if dry_lvl1.loc[idx]
        else '#26a69a' if up_day.loc[idx] else '#ef5350'
        for idx in df_daily.index
    ]

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03,
        subplot_titles=(f"{tk} Daily Price", 'Volume with Indicators', f'Relative Strength vs SPY (RS Rating: {rs_rating_str})'),
        row_heights=[0.5, 0.2, 0.3]
    )

    # Row 1: Price Candlesticks & Moving Averages
    fig.add_trace(go.Candlestick(
        x=df_daily.index, open=df_daily['Open'], high=df_daily['High'],
        low=df_daily['Low'], close=df_daily['Close'],
        name='Price', showlegend=False
    ), row=1, col=1)

    fig.add_trace(go.Scatter(x=df_daily.index, y=ema10, mode='lines', name='EMA(10)', line=dict(color='#FF9800', width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df_daily.index, y=ema21, mode='lines', name='EMA(21)', line=dict(color='#2196F3', width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df_daily.index, y=sma50, mode='lines', name='SMA(50)', line=dict(color='#4CAF50', width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df_daily.index, y=sma200, mode='lines', name='SMA(200)', line=dict(color='#E91E63', width=1.5)), row=1, col=1)

    # Row 2: Volume Bars & SMA50
    fig.add_trace(go.Bar(x=df_daily.index, y=df_daily['Volume'], marker=dict(color=vol_colors), name='Volume', showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_daily.index, y=avg_vol, name='Volume SMA(50)', line=dict(color='orange', width=1.5)), row=2, col=1)

    # Row 3: Relative Strength & EMAs
    fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_raw'], name='Raw RS', line=dict(color='blue', width=2)), row=3, col=1)
    fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_ema_quick'], name='Quick EMA (21)', line=dict(color='#56b8e6', width=2)), row=3, col=1)
    fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_ema_quicksand'], name='Quicksand EMA (34)', line=dict(color='#ff8c00', width=2)), row=3, col=1)
    fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_ema_gd'], name='GD EMA (50)', line=dict(color='#2ca02c', width=2)), row=3, col=1)

    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        height=height,
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5),
        template="plotly_dark"
    )

    st.plotly_chart(fig, use_container_width=True)

def load_daily_notes(date_obj):
    ensure_daily_dirs()
    date_str = date_obj.strftime("%Y%m%d")
    notes_path = DAILY_DIR / f"notes_{date_str}.json"
    
    empty_segments = [
        {"Segment": "Temp", "Grade": "", "PTD Only": "", "Sizing": "", "In My Favor": "", "Comments": ""},
        {"Segment": "9:30-11", "Grade": "", "PTD Only": "", "Sizing": "", "In My Favor": "", "Comments": ""},
        {"Segment": "11-12", "Grade": "", "PTD Only": "", "Sizing": "", "In My Favor": "", "Comments": ""},
        {"Segment": "12-2", "Grade": "", "PTD Only": "", "Sizing": "", "In My Favor": "", "Comments": ""},
        {"Segment": "2-4", "Grade": "", "PTD Only": "", "Sizing": "", "In My Favor": "", "Comments": ""}
    ]
    
    empty_data = {
        "date": date_obj.strftime("%Y-%m-%d"),
        "grade": "-",
        "pnl": "",
        "goal": "",
        "checklist": {
            "3_trades": False,
            "no_phone": False,
            "fill_drc": False,
            "afterhours": False
        },
        "segments": empty_segments,
        "learned": "",
        "changes": "",
        "overview": "",
        "easiest_50k": "",
        "tags": [],
        "trades": []
    }
    
    if notes_path.exists():
        try:
            with open(notes_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for k, v in empty_data.items():
                    if k not in saved:
                        saved[k] = v
                return saved
        except Exception as e:
            print(f"Error loading notes for {date_str}: {e}")
            
    return empty_data


def get_template_sample_notes(date_obj):
    return {
        "date": date_obj.strftime("%Y-%m-%d"),
        "grade": "A",
        "pnl": "+$50k",
        "goal": "Cement habit of selectivity aiming for 3 trades per segment unless truly exceptional. Think mindset of 'is this trade worth writing up at end of day?'",
        "checklist": {
            "3_trades": True,
            "no_phone": True,
            "fill_drc": True,
            "afterhours": True
        },
        "segments": [
            {"Segment": "Temp", "Grade": "-A", "PTD Only": "-", "Sizing": "-", "In My Favor": "-", "Comments": "Great sleep and feeling good although I think today might be less opportunistic than prior days. Will be taking it a bit more reserved and ultimately focus is on selectivity and the chart right now."},
            {"Segment": "9:30-11", "Grade": "-A", "PTD Only": "-A", "Sizing": "-A", "In My Favor": "-B", "Comments": "Pretty difficult segment, I think I rightly sized down on most things and relaly was fairly hands off. I think this remains the strategy as today definitely had potential to thus far be a -$50k day. Being near flat is great."},
            {"Segment": "11-12", "Grade": "-A", "PTD Only": "-A", "Sizing": "-A", "In My Favor": "-A", "Comments": "Really solid segment taking some big picture nibbles, then adding as things started to work. Never took much risk on and stayed selective."},
            {"Segment": "12-2", "Grade": "-A", "PTD Only": "-", "Sizing": "-", "In My Favor": "-", "Comments": "Very hands off, pretty slow segment. Going to stay selective into the close as today was slow as I had expected."},
            {"Segment": "2-4", "Grade": "-", "PTD Only": "-", "Sizing": "-", "In My Favor": "-", "Comments": "-"}
        ],
        "learned": "Researching NYSE halt print priority.\nWorking more on my home setup.",
        "changes": "I want to be more attentive.\nWant to keep on working on these ultra small swing trades and key is NOT GETTING TOO MUCH SIZE!\nBe more attentive to when a ticker starts to become a churn and the chart's reality is saying it won't have a good bounce (PBI prior day, BA today)",
        "overview": "Today I felt would be a bit less opportunistic for me and indeed it was. I had some decent SPY trades, however I also took some losses in BA and MAR.\n\nMy only meaningful trade in the end was CZR which made up most of my day.\n\nThere wasn't anything I got too aggressive in and I think that was fine for my playbook. Even retrospectively there was nothing that I LOVED or would have even put above an A- for me.\n\nAt this point the plan continues to be to avoid whammies while waiting for the stuff that screams out to me. It has been a strong stretch of many back-to-back solid days,",
        "easiest_50k": "Nothing so standout",
        "tags": ["A+ Day", "Selectivity", "Sample Template"],
        "trades": [
            {
                "ticker": "CZR",
                "pnl": "+$40k",
                "entry_price": "3.50",
                "exit_price": "5.50",
                "shares": "20,000",
                "tags": ["Buyout", "Reversal", "Win"],
                "notes": "CZR was just getting wrecked beyond belief. It is in a buyout w ERI which is also getting wrecked and was selling off hard.\n\nERI was bouncing, but CZR was still struggling big time. We were getting to the point where I was going to be okay holding a core so I bought 20k around $3.50 and was going to be a bit loose with that.\n\nOnce we finally broke trend though, I added bigger. Unfortunately, the setup wasn't so nice in a way that the risk was super small. I technically had to risk way back to prior bars, so I wanted to not get massive size.\n\nUnfortunately we really had issues gaining traction initially and the bids were shockingly poor. Some bounces just have the worst liquidity I've ever seen. We did eventually firm up and I was able to hold a chunk for a nice bounce. I think I did about as well as I could have expected here given everything."
            }
        ]
    }



def save_daily_notes(date_obj, data):
    ensure_daily_dirs()
    date_str = date_obj.strftime("%Y%m%d")
    notes_path = DAILY_DIR / f"notes_{date_str}.json"
    try:
        with open(notes_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving notes for {date_str}: {e}")
        return False

def get_all_drc_tags_and_entries():
    ensure_daily_dirs()
    all_day_tags = set()
    all_trade_tags = set()
    tag_to_dates = {}
    trade_tag_to_trades = {}
    
    for notes_file in DAILY_DIR.glob("notes_*.json"):
        try:
            with open(notes_file, "r", encoding="utf-8") as f:
                notes = json.load(f)
                d_str = notes.get("date", "")
                
                # Day-level tags
                tags = notes.get("tags", [])
                for t in tags:
                    clean_t = t.strip()
                    if clean_t:
                        all_day_tags.add(clean_t)
                        if clean_t not in tag_to_dates:
                            tag_to_dates[clean_t] = []
                        tag_to_dates[clean_t].append({
                            "date": d_str,
                            "grade": notes.get("grade", "-"),
                            "pnl": notes.get("pnl", "-"),
                            "file": notes_file.name
                        })
                        
                # Trade-level tags
                trades = notes.get("trades", [])
                for tr in trades:
                    tk = tr.get("ticker", "").upper()
                    tr_tags = tr.get("tags", [])
                    tr_pnl = tr.get("pnl", "")
                    tr_notes = tr.get("notes", "")
                    for tt in tr_tags:
                        clean_tt = tt.strip()
                        if clean_tt:
                            all_trade_tags.add(clean_tt)
                            if clean_tt not in trade_tag_to_trades:
                                trade_tag_to_trades[clean_tt] = []
                            trade_tag_to_trades[clean_tt].append({
                                "date": d_str,
                                "ticker": tk,
                                "pnl": tr_pnl,
                                "entry_price": tr.get("entry_price", ""),
                                "exit_price": tr.get("exit_price", ""),
                                "shares": tr.get("shares", ""),
                                "tags": tr_tags,
                                "notes": tr_notes,
                                "file": notes_file.name
                            })
        except Exception:
            continue
            
    return sorted(list(all_day_tags)), tag_to_dates, sorted(list(all_trade_tags)), trade_tag_to_trades


# ---------------------- Lightweight Charts Helper (3 panes) ----------------------
def create_lightweight_candlestick_html(df, title="", height=600, markers=None, rs_label=None,
                                        rs_raw=None, rs_quick=None, rs_quicksand=None, rs_gd=None,
                                        volume_data=None, volume_sma50=None,
                                        pp10_dates=None, pp5_dates=None,
                                        churn_dates=None, stall_dates=None, ll3_dates=None,
                                        pattern_js=""):
    if df is None or df.empty:
        return "<div style='padding:20px; color:#999;'>No data available</div>"

    candles = []
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp())
            candles.append({
                'time': ts,
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close'])
            })
        except Exception:
            continue

    if not candles:
        return "<div style='padding:20px; color:#999;'>No valid candle data</div>"

    df_clean = df[['Close']].copy()
    df_clean['MA10'] = df_clean['Close'].rolling(10).mean()
    df_clean['MA21'] = df_clean['Close'].rolling(21).mean()
    df_clean['MA50'] = df_clean['Close'].rolling(50).mean()
    ma10_data, ma21_data, ma50_data = [], [], []
    for idx, row in df_clean.iterrows():
        ts = int(idx.timestamp())
        if not pd.isna(row['MA10']):
            ma10_data.append({'time': ts, 'value': float(row['MA10'])})
        if not pd.isna(row['MA21']):
            ma21_data.append({'time': ts, 'value': float(row['MA21'])})
        if not pd.isna(row['MA50']):
            ma50_data.append({'time': ts, 'value': float(row['MA50'])})

    def prepare_rs(series):
        if series is None:
            return []
        if isinstance(series, pd.Series):
            return [{'time': int(idx.timestamp()), 'value': float(v)} for idx, v in series.items() if pd.notna(v)]
        return series

    rs_raw_data       = prepare_rs(rs_raw)
    rs_quick_data     = prepare_rs(rs_quick)
    rs_quicksand_data = prepare_rs(rs_quicksand)
    rs_gd_data        = prepare_rs(rs_gd)

    price_markers = list(markers) if markers else []
    if rs_label is not None and candles:
        price_markers.append({
            'time': candles[-1]['time'],
            'position': 'aboveBar',
            'color': '#636efa',
            'shape': 'circle',
            'text': str(rs_label)
        })
    price_markers_json = json.dumps(price_markers)

    volume_colored    = volume_data  if volume_data  else []
    volume_sma50_data = volume_sma50 if volume_sma50 else []

    pp10  = pp10_dates   if pp10_dates   else []
    pp5   = pp5_dates    if pp5_dates    else []
    churn = churn_dates  if churn_dates  else []
    stall = stall_dates  if stall_dates  else []
    ll3   = ll3_dates    if ll3_dates    else []

    price_id  = f"price_{uuid.uuid4().hex[:8]}"
    volume_id = f"vol_{uuid.uuid4().hex[:8]}"
    rs_id     = f"rs_{uuid.uuid4().hex[:8]}"

    price_h  = int(height * 0.5)
    volume_h = int(height * 0.2)
    rs_h     = height - price_h - volume_h

    html = f"""
    <div style="width:100%; font-family: system-ui, sans-serif;">
        <div id="{price_id}"  style="height:{price_h}px;  border:1px solid #ddd; border-bottom:none; border-radius:4px 4px 0 0;"></div>
        <div id="{volume_id}" style="height:{volume_h}px; border-left:1px solid #ddd; border-right:1px solid #ddd;"></div>
        <div id="{rs_id}"     style="height:{rs_h}px;     border:1px solid #ddd; border-top:none;   border-radius:0 0 4px 4px;"></div>
    </div>
    <script src="https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js"></script>
    <script>
    (function() {{
        const priceDiv  = document.getElementById('{price_id}');
        const volumeDiv = document.getElementById('{volume_id}');
        const rsDiv     = document.getElementById('{rs_id}');

        const priceChart = LightweightCharts.createChart(priceDiv, {{
            width: priceDiv.clientWidth, height: {price_h},
            layout: {{ backgroundColor: '#fff', textColor: '#333' }},
            timeScale: {{ timeVisible: true, secondsVisible: false }}
        }});
        const volumeChart = LightweightCharts.createChart(volumeDiv, {{
            width: volumeDiv.clientWidth, height: {volume_h},
            layout: {{ backgroundColor: '#fff', textColor: '#333' }},
            timeScale: {{ timeVisible: false }}
        }});
        const rsChart = LightweightCharts.createChart(rsDiv, {{
            width: rsDiv.clientWidth, height: {rs_h},
            layout: {{ backgroundColor: '#fff', textColor: '#333' }},
            timeScale: {{ timeVisible: false }}
        }});

        const masterScale = priceChart.timeScale();
        volumeChart.timeScale().subscribeVisibleLogicalRangeChange(r => masterScale.setVisibleLogicalRange(r));
        rsChart.timeScale().subscribeVisibleLogicalRangeChange(r => masterScale.setVisibleLogicalRange(r));
        masterScale.subscribeVisibleLogicalRangeChange(r => {{
            volumeChart.timeScale().setVisibleLogicalRange(r);
            rsChart.timeScale().setVisibleLogicalRange(r);
        }});

        // Candlesticks
        const candleSeries = priceChart.addCandlestickSeries({{
            upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
            wickUpColor: '#26a69a', wickDownColor: '#ef5350'
        }});
        candleSeries.setData({json.dumps(candles)});

        // Moving Averages
        const ma10 = priceChart.addLineSeries({{ color: '#FF9800', lineWidth: 2, title: 'MA10',
            crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false }});
        const ma21 = priceChart.addLineSeries({{ color: '#2196F3', lineWidth: 2, title: 'MA21',
            crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false }});
        const ma50 = priceChart.addLineSeries({{ color: '#F44336', lineWidth: 2, title: 'MA50',
            crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false }});
        if ({json.dumps(ma10_data)}.length) ma10.setData({json.dumps(ma10_data)});
        if ({json.dumps(ma21_data)}.length) ma21.setData({json.dumps(ma21_data)});
        if ({json.dumps(ma50_data)}.length) ma50.setData({json.dumps(ma50_data)});

        // Pattern overlays (injected by pattern_painter)
        {pattern_js}

        // Merge pattern price labels with user markers
        (function() {{
            const base = {price_markers_json};
            const patt = (typeof window._patternMarkers !== 'undefined') ? window._patternMarkers : [];
            const all  = base.concat(patt).map(m => ({{
                time: m.time, position: m.position || 'aboveBar',
                color: m.color, shape: m.shape || 'circle', text: m.text || '', size: m.size || 1
            }}));
            all.sort((a, b) => a.time - b.time);
            if (all.length) candleSeries.setMarkers(all);
        }})();

        // Volume
        const volumeSeries = volumeChart.addHistogramSeries({{
            priceFormat: {{ type: 'volume' }},
            priceScaleId: 'right'
        }});
        volumeSeries.setData({json.dumps(volume_colored)});

        if ({json.dumps(volume_sma50_data)}.length) {{
            const volSma = volumeChart.addLineSeries({{
                color: 'orange', lineWidth: 1.5, title: 'Vol SMA(50)',
                crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false
            }});
            volSma.setData({json.dumps(volume_sma50_data)});
        }}

        const volData = {json.dumps(volume_colored)};
        let maxVol = 0;
        volData.forEach(v => {{ if (v.value > maxVol) maxVol = v.value; }});
        const markerY = maxVol * 1.08;

        function addMarkers(chart, dates, color, shape, size, yValue) {{
            if (!dates.length) return;
            const series = chart.addLineSeries({{
                color: color, lineWidth: 0, lineVisible: false,
                pointMarkersVisible: true, pointMarkerType: shape, pointMarkerSize: size,
                priceLineVisible: false, lastValueVisible: false
            }});
            const points = dates.map(ts => ({{ time: ts, value: yValue }}));
            series.setData(points);
        }}

        addMarkers(volumeChart, {json.dumps(pp10)},  'yellow', 'diamond',      12, markerY);
        addMarkers(volumeChart, {json.dumps(pp5)},   'blue',   'diamond',      10, markerY);
        addMarkers(volumeChart, {json.dumps(churn)}, 'purple', 'x',            12, markerY);
        addMarkers(volumeChart, {json.dumps(stall)}, 'maroon', 'circle',       10, markerY);
        addMarkers(volumeChart, {json.dumps(ll3)},   'red',    'triangleUp',   12, markerY);

        // RS lines
        const rsRaw       = rsChart.addLineSeries({{ color: 'blue',    lineWidth: 2, title: 'Raw RS' }});
        const rsQuick     = rsChart.addLineSeries({{ color: '#56b8e6', lineWidth: 2, title: 'Quick EMA (21)' }});
        const rsQuicksand = rsChart.addLineSeries({{ color: '#ff8c00', lineWidth: 2, title: 'Quicksand EMA (34)' }});
        const rsGd        = rsChart.addLineSeries({{ color: '#2ca02c', lineWidth: 2, title: 'GD EMA (50)' }});
        if ({json.dumps(rs_raw_data)}.length)       rsRaw.setData({json.dumps(rs_raw_data)});
        if ({json.dumps(rs_quick_data)}.length)     rsQuick.setData({json.dumps(rs_quick_data)});
        if ({json.dumps(rs_quicksand_data)}.length) rsQuicksand.setData({json.dumps(rs_quicksand_data)});
        if ({json.dumps(rs_gd_data)}.length)        rsGd.setData({json.dumps(rs_gd_data)});

        masterScale.fitContent();

        // OHLC tooltip
        const tooltip = document.createElement('div');
        tooltip.style.cssText = 'position:absolute; background:rgba(0,0,0,0.8); color:#fff; padding:6px 10px; border-radius:6px; font-size:12px; pointer-events:none; z-index:1000; display:none;';
        priceDiv.parentElement.style.position = 'relative';
        priceDiv.parentElement.appendChild(tooltip);
        priceChart.subscribeCrosshairMove(function(param) {{
            if (!param.time || !param.point) {{ tooltip.style.display = 'none'; return; }}
            const data = param.seriesData.get(candleSeries);
            if (data && data.open !== undefined) {{
                tooltip.innerHTML = '<b>OHLC</b><br>O: $' + data.open.toFixed(2) + '<br>H: $' + data.high.toFixed(2) + '<br>L: $' + data.low.toFixed(2) + '<br>C: $' + data.close.toFixed(2);
                tooltip.style.display = 'block';
                tooltip.style.left = param.point.x + 15 + 'px';
                tooltip.style.top  = param.point.y - 30 + 'px';
            }} else {{ tooltip.style.display = 'none'; }}
        }});
        priceDiv.addEventListener('mouseleave', () => tooltip.style.display = 'none');

        window.addEventListener('resize', function() {{
            if (priceDiv.clientWidth) {{
                priceChart.applyOptions({{ width: priceDiv.clientWidth }});
                volumeChart.applyOptions({{ width: volumeDiv.clientWidth }});
                rsChart.applyOptions({{ width: rsDiv.clientWidth }});
            }}
        }});
    }})();
    </script>
    """
    return html

# ---------------------- EquiCharts (Equivolume) Helper ----------------------
def create_equicharts_html(df, height=650, title="", ticker="", rs_label=None):
    if df is None or df.empty:
        return "<div style='padding:20px; color:#999;'>No data available</div>"

    df_calc = df.copy()
    
    if 'MA10' not in df_calc.columns:
        df_calc['MA10'] = df_calc['Close'].ewm(span=10, adjust=False).mean()
    if 'MA21' not in df_calc.columns:
        df_calc['MA21'] = df_calc['Close'].ewm(span=21, adjust=False).mean()
    if 'MA50' not in df_calc.columns:
        df_calc['MA50'] = df_calc['Close'].ewm(span=50, adjust=False).mean()
    if 'AvgVol50' not in df_calc.columns:
        df_calc['AvgVol50'] = df_calc['Volume'].rolling(50, min_periods=1).mean()

    bars = []
    for idx, row in df_calc.iterrows():
        try:
            date_str = idx.strftime('%Y-%m-%d') if hasattr(idx, 'strftime') else str(idx)
            bars.append({
                'date': date_str,
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close']),
                'volume': float(row['Volume']),
                'avg_vol': float(row['AvgVol50']) if pd.notna(row.get('AvgVol50')) else float(row['Volume']),
                'ma10': float(row['MA10']) if pd.notna(row.get('MA10')) else None,
                'ma21': float(row['MA21']) if pd.notna(row.get('MA21')) else None,
                'ma50': float(row['MA50']) if pd.notna(row.get('MA50')) else None,
                'rs_raw': float(row['rs_raw']) if 'rs_raw' in row and pd.notna(row['rs_raw']) else None,
                'rs_quick': float(row['rs_ema_quick']) if 'rs_ema_quick' in row and pd.notna(row['rs_ema_quick']) else None,
                'rs_quicksand': float(row['rs_ema_quicksand']) if 'rs_ema_quicksand' in row and pd.notna(row['rs_ema_quicksand']) else None,
                'rs_gd': float(row['rs_ema_gd']) if 'rs_ema_gd' in row and pd.notna(row['rs_ema_gd']) else None,
            })
        except Exception:
            continue

    if not bars:
        return "<div style='padding:20px; color:#999;'>No valid bar data for EquiCharts</div>"

    bars_json = json.dumps(bars)
    chart_id = f"equichart_{uuid.uuid4().hex[:8]}"

    html = f"""
    <div id="{chart_id}_wrapper" style="width:100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#131722; color:#d1d4dc; padding:12px; border-radius:8px; box-sizing:border-box;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <div style="display:flex; align-items:center; gap:12px;">
                <span style="font-weight:bold; font-size:16px; color:#fff;">📊 EquiCharts (Equivolume) — {ticker}</span>
                <span style="font-size:12px; background:#2a2e39; padding:2px 8px; border-radius:4px; color:#2962ff;">Box Width = Volume</span>
                {f'<span style="font-size:12px; background:#636efa; padding:2px 8px; border-radius:4px; color:#fff;">RS Rating: {rs_label}</span>' if rs_label else ''}
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
                <button id="{chart_id}_zoom_in" style="background:#2a2e39; color:#fff; border:none; padding:4px 10px; border-radius:4px; cursor:pointer; font-weight:bold;">+ Zoom</button>
                <button id="{chart_id}_zoom_out" style="background:#2a2e39; color:#fff; border:none; padding:4px 10px; border-radius:4px; cursor:pointer; font-weight:bold;">- Zoom</button>
                <button id="{chart_id}_reset" style="background:#2a2e39; color:#fff; border:none; padding:4px 10px; border-radius:4px; cursor:pointer;">Reset</button>
            </div>
        </div>
        <div style="position:relative; width:100%; height:{height}px;">
            <canvas id="{chart_id}_canvas" style="width:100%; height:100%; display:block; cursor:crosshair;"></canvas>
            <div id="{chart_id}_tooltip" style="position:absolute; top:10px; left:10px; background:rgba(19, 23, 34, 0.9); border:1px solid #363c4e; border-radius:4px; padding:8px 12px; font-size:12px; pointer-events:none; display:none; z-index:100;"></div>
        </div>
    </div>

    <script>
    (function() {{
        const rawBars = {bars_json};
        const canvas = document.getElementById('{chart_id}_canvas');
        const ctx = canvas.getContext('2d');
        const tooltip = document.getElementById('{chart_id}_tooltip');

        let dpr = window.devicePixelRatio || 1;
        let baseWidth = 14;
        let scrollX = 0;
        let isDragging = false;
        let startMouseX = 0;
        let startScrollX = 0;
        let hoverIndex = -1;

        function resizeCanvas() {{
            const rect = canvas.getBoundingClientRect();
            canvas.width = rect.width * dpr;
            canvas.height = rect.height * dpr;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            draw();
        }}

        function computeLayout() {{
            let currentX = 10 + scrollX;
            const layoutBars = [];
            for (let i = 0; i < rawBars.length; i++) {{
                const b = rawBars[i];
                const volRatio = b.avg_vol > 0 ? (b.volume / b.avg_vol) : 1.0;
                const width = Math.max(4, Math.min(60, baseWidth * volRatio));
                const xStart = currentX;
                const xEnd = currentX + width;
                const xCenter = (xStart + xEnd) / 2;
                currentX += width + 3;
                layoutBars.push({{ ...b, xStart, xEnd, xCenter, width }});
            }}
            return layoutBars;
        }}

        function draw() {{
            const rect = canvas.getBoundingClientRect();
            const width = rect.width;
            const height = rect.height;
            ctx.clearRect(0, 0, width, height);

            const layoutBars = computeLayout();
            if (layoutBars.length === 0) return;

            const priceHeight = height * 0.7;
            const rsHeight = height * 0.25;
            const rsTop = height * 0.72;

            let minPrice = Infinity, maxPrice = -Infinity;
            let minRS = Infinity, maxRS = -Infinity;
            let hasVisible = false;

            layoutBars.forEach(b => {{
                if (b.xEnd >= 0 && b.xStart <= width) {{
                    hasVisible = true;
                    if (b.low < minPrice) minPrice = b.low;
                    if (b.high > maxPrice) maxPrice = b.high;
                    if (b.ma10 && b.ma10 < minPrice) minPrice = b.ma10;
                    if (b.ma50 && b.ma50 > maxPrice) maxPrice = b.ma50;

                    if (b.rs_raw !== null) {{
                        if (b.rs_raw < minRS) minRS = b.rs_raw;
                        if (b.rs_raw > maxRS) maxRS = b.rs_raw;
                    }}
                }}
            }});

            if (!hasVisible || minPrice === Infinity) {{
                minPrice = Math.min(...rawBars.map(b => b.low));
                maxPrice = Math.max(...rawBars.map(b => b.high));
            }}

            const pricePadding = (maxPrice - minPrice) * 0.05 || 1;
            minPrice -= pricePadding;
            maxPrice += pricePadding;

            if (minRS === Infinity) {{ minRS = 0; maxRS = 100; }}
            const rsPadding = (maxRS - minRS) * 0.05 || 1;
            minRS -= rsPadding;
            maxRS += rsPadding;

            function getYPrice(p) {{
                return priceHeight - 20 - ((p - minPrice) / (maxPrice - minPrice)) * (priceHeight - 40);
            }}

            function getYRS(r) {{
                return rsTop + rsHeight - 10 - ((r - minRS) / (maxRS - minRS)) * (rsHeight - 20);
            }}

            // Horizontal Grid Lines
            ctx.strokeStyle = '#2a2e39';
            ctx.lineWidth = 1;
            ctx.beginPath();
            for (let i = 1; i <= 5; i++) {{
                let y = (priceHeight / 6) * i;
                ctx.moveTo(0, y);
                ctx.lineTo(width, y);
                let pVal = maxPrice - ((maxPrice - minPrice) / 6) * i;
                ctx.fillStyle = '#787b86';
                ctx.font = '10px sans-serif';
                ctx.fillText(pVal.toFixed(2), width - 50, y - 4);
            }}
            ctx.stroke();

            // RS Subpane Divider
            ctx.strokeStyle = '#363c4e';
            ctx.beginPath();
            ctx.moveTo(0, priceHeight);
            ctx.lineTo(width, priceHeight);
            ctx.stroke();

            ctx.fillStyle = '#2962ff';
            ctx.font = 'bold 11px sans-serif';
            ctx.fillText('Relative Strength (Raw & EMAs)', 10, priceHeight + 15);

            // Draw EquiVolume Boxes
            layoutBars.forEach((b) => {{
                if (b.xEnd < -50 || b.xStart > width + 50) return;

                const isUp = b.close >= b.open;
                const strokeColor = isUp ? '#26a69a' : '#ef5350';
                const fillColor   = isUp ? 'rgba(38, 166, 154, 0.35)' : 'rgba(239, 83, 80, 0.35)';

                const yHigh = getYPrice(b.high);
                const yLow  = getYPrice(b.low);
                const yOpen = getYPrice(b.open);
                const yClose = getYPrice(b.close);

                // High-Low Wick
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                ctx.moveTo(b.xCenter, yHigh);
                ctx.lineTo(b.xCenter, yLow);
                ctx.stroke();

                // Box Body (Height: Open to Close, Width: Volume)
                const bodyTop = Math.min(yOpen, yClose);
                const bodyHeight = Math.max(2, Math.abs(yClose - yOpen));

                ctx.fillStyle = fillColor;
                ctx.fillRect(b.xStart, bodyTop, b.width, bodyHeight);

                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1.5;
                ctx.strokeRect(b.xStart, bodyTop, b.width, bodyHeight);

                // Open tick (left)
                ctx.beginPath();
                ctx.moveTo(b.xStart - 2, yOpen);
                ctx.lineTo(b.xStart + Math.min(6, b.width / 2), yOpen);
                ctx.stroke();

                // Close tick (right)
                ctx.beginPath();
                ctx.moveTo(b.xEnd - Math.min(6, b.width / 2), yClose);
                ctx.lineTo(b.xEnd + 2, yClose);
                ctx.stroke();
            }});

            // Moving Averages
            function drawMA(key, color, widthLine = 1.5) {{
                ctx.strokeStyle = color;
                ctx.lineWidth = widthLine;
                ctx.beginPath();
                let started = false;
                layoutBars.forEach(b => {{
                    if (b[key] !== null) {{
                        const y = getYPrice(b[key]);
                        if (!started) {{ ctx.moveTo(b.xCenter, y); started = true; }}
                        else {{ ctx.lineTo(b.xCenter, y); }}
                    }}
                }});
                ctx.stroke();
            }}

            drawMA('ma10', '#FF9800', 2);
            drawMA('ma21', '#2196F3', 2);
            drawMA('ma50', '#F44336', 2);

            // RS Lines
            function drawRS(key, color, widthLine = 1.5) {{
                ctx.strokeStyle = color;
                ctx.lineWidth = widthLine;
                ctx.beginPath();
                let started = false;
                layoutBars.forEach(b => {{
                    if (b[key] !== null) {{
                        const y = getYRS(b[key]);
                        if (!started) {{ ctx.moveTo(b.xCenter, y); started = true; }}
                        else {{ ctx.lineTo(b.xCenter, y); }}
                    }}
                }});
                ctx.stroke();
            }}

            drawRS('rs_raw',       '#2962ff', 2);
            drawRS('rs_quick',     '#56b8e6', 1.5);
            drawRS('rs_quicksand', '#ff8c00', 1.5);
            drawRS('rs_gd',        '#2ca02c', 1.5);

            // Hover crosshair & tooltip
            if (hoverIndex >= 0 && hoverIndex < layoutBars.length) {{
                const hb = layoutBars[hoverIndex];
                ctx.strokeStyle = 'rgba(255,255,255,0.4)';
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(hb.xCenter, 0);
                ctx.lineTo(hb.xCenter, height);
                ctx.stroke();
                ctx.setLineDash([]);

                const volPct = hb.avg_vol > 0 ? ((hb.volume / hb.avg_vol) * 100).toFixed(0) : 100;
                const changePct = (((hb.close - hb.open) / hb.open) * 100).toFixed(2);
                const sign = changePct >= 0 ? '+' : '';

                tooltip.style.display = 'block';
                tooltip.innerHTML = `
                    <div style="font-weight:bold; color:#fff; margin-bottom:4px;">${{hb.date}}</div>
                    <div style="color:${{hb.close >= hb.open ? '#26a69a' : '#ef5350'}};">Open: $${{hb.open.toFixed(2)}} | High: $${{hb.high.toFixed(2)}}</div>
                    <div style="color:${{hb.close >= hb.open ? '#26a69a' : '#ef5350'}};">Low: $${{hb.low.toFixed(2)}} | Close: $${{hb.close.toFixed(2)}} (${{sign}}${{changePct}}%)</div>
                    <div style="color:#e0e0e0; margin-top:2px;">Volume: ${{hb.volume.toLocaleString()}} (${{volPct}}% of 50d avg)</div>
                    ${{hb.ma10 ? `<div style="color:#FF9800;">MA(10): $${{hb.ma10.toFixed(2)}} | MA(21): $${{hb.ma21 ? hb.ma21.toFixed(2) : 'N/A'}} | MA(50): $${{hb.ma50 ? hb.ma50.toFixed(2) : 'N/A'}}</div>` : ''}}
                `;
            }} else {{
                tooltip.style.display = 'none';
            }}
        }}

        // Controls & Events
        canvas.addEventListener('mousedown', e => {{
            isDragging = true;
            startMouseX = e.clientX;
            startScrollX = scrollX;
        }});

        window.addEventListener('mousemove', e => {{
            const rect = canvas.getBoundingClientRect();
            if (isDragging) {{
                scrollX = startScrollX + (e.clientX - startMouseX);
                draw();
            }}
            if (e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom) {{
                const mouseX = e.clientX - rect.left;
                const layoutBars = computeLayout();
                let closestIdx = -1;
                let minDistance = Infinity;
                layoutBars.forEach((b, idx) => {{
                    const dist = Math.abs(b.xCenter - mouseX);
                    if (dist < minDistance && mouseX >= b.xStart - 5 && mouseX <= b.xEnd + 5) {{
                        minDistance = dist;
                        closestIdx = idx;
                    }}
                }});
                if (closestIdx !== hoverIndex) {{
                    hoverIndex = closestIdx;
                    draw();
                }}
            }} else if (hoverIndex !== -1) {{
                hoverIndex = -1;
                draw();
            }}
        }});

        window.addEventListener('mouseup', () => isDragging = false);

        canvas.addEventListener('wheel', e => {{
            e.preventDefault();
            const zoomFactor = e.deltaY < 0 ? 1.15 : 0.85;
            baseWidth = Math.max(4, Math.min(50, baseWidth * zoomFactor));
            draw();
        }}, {{ passive: false }});

        document.getElementById('{chart_id}_zoom_in').addEventListener('click', () => {{ baseWidth = Math.min(50, baseWidth * 1.25); draw(); }});
        document.getElementById('{chart_id}_zoom_out').addEventListener('click', () => {{ baseWidth = Math.max(4, baseWidth * 0.8); draw(); }});
        document.getElementById('{chart_id}_reset').addEventListener('click', () => {{ baseWidth = 14; scrollX = 0; draw(); }});

        window.addEventListener('resize', resizeCanvas);
        resizeCanvas();
    }})();
    </script>
    """
    return html
# ---------------------- Page configuration ----------------------
st.set_page_config(page_title="RS Analysis Dashboard", page_icon="📊", layout="wide", initial_sidebar_state="expanded")

if 'selected_ticker' not in st.session_state:
    st.session_state.selected_ticker = None

st.title("📊 Relative Strength Analysis Dashboard")
st.markdown("Daily RS Calculation Logs Analysis and Insights | Historical Data from Oct 2021 to Present")

if not st.session_state.get("_ibd_live_sync_toast_shown"):
    st.session_state._ibd_live_sync_toast_shown = True
    _synced, _skipped, _synced_dates = st.session_state.get("_ibd_live_sync_result", (0, 0, []))
    if _synced:
        st.toast(f"🎙️ Synced {_synced} new IBD Live summary(ies).", icon="✅")
    if st.session_state.get("_ibd_live_sync_error"):
        st.toast(f"⚠️ IBD Live sync failed: {st.session_state['_ibd_live_sync_error']}", icon="⚠️")

col1, col2 = st.columns([2, 8])
with col1:
    if st.button("🔁 Reload data from disk"):
        try:
            st.cache_data.clear()
        except Exception:
            pass
        rerun_app()
with col2:
    if st.button("⬇️ Pull Latest Data & Update"):
        with st.spinner("Pulling latest data from upstream and updating pipeline..."):
            try:
                repo_dir = str(Path(__file__).resolve().parent)
                branch_proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
                branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "main"
                subprocess.run(["git", "fetch", "upstream"], cwd=repo_dir, capture_output=True)
                subprocess.run(["git", "merge", f"upstream/{branch}"], cwd=repo_dir, capture_output=True)
                # Stash unstaged changes if any, to avoid git pull failing
                status_res = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)
                has_changes = bool(status_res.stdout.strip())
                if has_changes:
                    subprocess.run(["git", "stash"], cwd=repo_dir, capture_output=True)
                
                pull_res = subprocess.run(["git", "pull"], cwd=repo_dir, capture_output=True, text=True)
                
                if has_changes:
                    subprocess.run(["git", "stash", "pop"], cwd=repo_dir, capture_output=True)
                subprocess.run(["git", "push", "origin", branch], cwd=repo_dir, capture_output=True)
                if pull_res.returncode != 0:
                    st.error(f"Git pull failed:\n{pull_res.stderr}")
                else:
                    subprocess.run([sys.executable, "check_remote_and_append.py", "--force"], cwd=repo_dir, capture_output=True, text=True)
                    st.cache_data.clear()
                    rerun_app()
            except Exception as e:
                st.error(f"An error occurred: {e}")

# ---------------------- Data loading functions ----------------------
def compute_output_signature():
    output_dir = Path(__file__).parent / "output"
    if not output_dir.exists():
        return ""
    parts = []
    for fp in sorted(output_dir.glob('*.csv')):
        try:
            parts.append(f"{fp.name}:{int(fp.stat().st_mtime)}")
        except Exception:
            pass
    return '|'.join(parts)

@st.cache_data
def load_company_descriptions():
    ibd_path = Path(__file__).resolve().parent / "IBD_data.txt"
    if not ibd_path.exists():
        return {}
    try:
        df_ibd = pd.read_csv(ibd_path)
        df_ibd.columns = [c.strip() for c in df_ibd.columns]
        if 'Symbol' in df_ibd.columns and 'Company Description' in df_ibd.columns:
            df_ibd['Symbol'] = df_ibd['Symbol'].astype(str).str.strip('"').str.strip()
            df_ibd['Company Description'] = df_ibd['Company Description'].astype(str).str.strip('"').str.strip()
            df_ibd = df_ibd.dropna(subset=['Symbol', 'Company Description'])
            
            desc_map = {}
            for _, row in df_ibd.iterrows():
                sym = row['Symbol']
                desc = row['Company Description']
                if pd.isna(sym) or pd.isna(desc) or desc == 'nan':
                    continue
                # Map raw symbol (e.g. 'BRK.B')
                desc_map[sym] = desc
                # Map normalized symbol (e.g. 'BRKB')
                norm = sym.replace(".", "").replace("-", "").replace("/", "").replace(" ", "").upper()
                desc_map[norm] = desc
                # Map common hyphen/slash variations (e.g. 'BRK-B', 'BRK/B')
                desc_map[sym.replace(".", "-")] = desc
                desc_map[sym.replace(".", "/")] = desc
            return desc_map
    except Exception as e:
        print(f"Error loading company descriptions: {e}")
    return {}

@st.cache_data
def load_ibd_ticker_industry_mapping():
    ibd_path = Path(__file__).resolve().parent / "IBD Industry Mapping.txt"
    if not ibd_path.exists():
        return {}
    try:
        df_ibd = pd.read_csv(ibd_path)
        df_ibd.columns = [c.strip() for c in df_ibd.columns]
        if 'Symbol' in df_ibd.columns and 'Industry Name' in df_ibd.columns:
            df_ibd['Symbol'] = df_ibd['Symbol'].astype(str).str.strip('"').str.strip()
            df_ibd['Industry Name'] = df_ibd['Industry Name'].astype(str).str.strip('"').str.strip()
            df_ibd = df_ibd.dropna(subset=['Symbol', 'Industry Name'])
            
            mapping = {}
            for _, row in df_ibd.iterrows():
                sym = row['Symbol']
                ind = row['Industry Name']
                if pd.isna(sym) or pd.isna(ind) or ind == 'nan' or ind == '':
                    continue
                mapping[sym] = ind
                # Map normalized symbol too
                norm = sym.replace(".", "").replace("-", "").replace("/", "").replace(" ", "").upper()
                mapping[norm] = ind
            return mapping
    except Exception as e:
        print(f"Error loading IBD ticker-industry mapping: {e}")
    return {}


@st.cache_data
def load_ibd_data_tables_ranks():
    ibd_tables_path = Path(__file__).resolve().parent / "IBD" / "IBD Data Tables.csv"
    if not ibd_tables_path.exists():
        return {}
    try:
        df_ibd = pd.read_csv(ibd_tables_path)
        df_ibd.columns = [c.strip() for c in df_ibd.columns]
        if 'Symbol' in df_ibd.columns and 'Industry Group Rank' in df_ibd.columns:
            df_ibd['Symbol'] = df_ibd['Symbol'].astype(str).str.strip('"').str.strip()
            df_ibd['Industry Group Rank'] = pd.to_numeric(df_ibd['Industry Group Rank'], errors='coerce')
            df_ibd = df_ibd.dropna(subset=['Symbol', 'Industry Group Rank'])
            
            ranks = {}
            for _, row in df_ibd.iterrows():
                sym = row['Symbol']
                rank = row['Industry Group Rank']
                ranks[sym] = rank
                # Map normalized symbol too
                norm = sym.replace(".", "").replace("-", "").replace("/", "").replace(" ", "").upper()
                ranks[norm] = rank
            return ranks
    except Exception as e:
        print(f"Error loading IBD Data Tables ranks: {e}")
    return {}


@st.cache_data
def load_ibd_data_tables_full():
    ibd_tables_path = Path(__file__).resolve().parent / "IBD" / "IBD Data Tables.csv"
    if not ibd_tables_path.exists():
        return {}
    try:
        df_ibd = pd.read_csv(ibd_tables_path)
        df_ibd.columns = [c.strip() for c in df_ibd.columns]
        if 'Symbol' in df_ibd.columns:
            df_ibd['Symbol'] = df_ibd['Symbol'].astype(str).str.strip('"').str.strip()
            df_ibd = df_ibd.drop_duplicates(subset=['Symbol'])
            # Convert numeric columns where possible
            num_ibd_cols = ['IBD Comp. Rating', 'RS Rating', 'Industry Group Rank', 'EPS Rating', 'Price', 'Price % Change', 'Vol. % Change', 'Last Qtr EPS % Chg.', 'Last Qtr Sales % Chg.', 'Curr Yr EPS Est. % Chg.', 'Curr Qtr EPS Est. % Chg.', 'Pretax Margin']
            for col in num_ibd_cols:
                if col in df_ibd.columns:
                    df_ibd[col] = pd.to_numeric(df_ibd[col], errors='coerce')

            
            # create dictionary mapping symbol -> dict of stats
            df_ibd.set_index('Symbol', inplace=True)
            info_dict = df_ibd.to_dict(orient='index')
            
            # add normalized symbols
            expanded_dict = {}
            for sym, data in info_dict.items():
                expanded_dict[sym] = data
                norm = sym.replace(".", "").replace("-", "").replace("/", "").replace(" ", "").upper()
                expanded_dict[norm] = data
            return expanded_dict
    except Exception as e:
        print(f"Error loading IBD Data Tables full: {e}")
    return {}


@st.cache_data
def get_industry_ranks(ibd_industry_mapping, symbol_ranks):
    industry_ranks = {}
    for sym, rank in symbol_ranks.items():
        ind = ibd_industry_mapping.get(sym)
        if ind and ind != "Unknown Industry":
            if ind not in industry_ranks or rank < industry_ranks[ind]:
                industry_ranks[ind] = rank
    return industry_ranks


@st.cache_data
def load_csv_files(reload_sig: str):
    output_dir = Path(__file__).parent / "output"
    stocks_historical_file = output_dir / "rs_stocks_historical.csv"
    if stocks_historical_file.exists():
        try:
            df = pd.read_csv(stocks_historical_file)
            df = df.drop_duplicates(subset=['date', 'Ticker'])
            df['date'] = pd.to_datetime(df['date'])
            df['source_file'] = 'stocks_historical'
            unique_dates = df['date'].nunique() if 'date' in df.columns else 0
            st.success(f"✅ Loaded historical stock data ({len(df):,} records, {unique_dates} trading days)")
            return df
        except Exception as e:
            st.warning(f"Error loading stock historical data: {e}")

    historical_file = output_dir / "rs_historical_all.csv"
    if historical_file.exists():
        try:
            df = pd.read_csv(historical_file)
            df = df.drop_duplicates(subset=['date', 'Ticker'])
            df['date'] = pd.to_datetime(df['date'])
            df['source_file'] = 'historical_all'
            st.success("✅ Loaded historical data (5.87M records, Oct 2021-Present)")
            return df
        except Exception as e:
            st.warning(f"Error loading historical data: {e}")

    main_stock_file = output_dir / "rs_stocks.csv"
    if main_stock_file.exists():
        csv_files = [str(main_stock_file)]
    else:
        csv_files = sorted(glob.glob(str(output_dir / "rs_stocks_*.csv")))
    if not csv_files:
        return None
    dfs = []
    for file in csv_files:
        try:
            df = pd.read_csv(file)
            df['source_file'] = Path(file).name
            dfs.append(df)
        except Exception as e:
            st.warning(f"Error loading {file}: {e}")
    if dfs:
        return pd.concat(dfs, ignore_index=True)
    return None

@st.cache_data
def load_industry_data(reload_sig: str):
    output_dir = Path(__file__).parent / "output"
    historical_file = output_dir / "rs_industries_historical.csv"
    if historical_file.exists():
        try:
            df_hist = pd.read_csv(historical_file)
            if 'date' in df_hist.columns:
                df_hist['date'] = pd.to_datetime(df_hist['date'])
                latest_date = df_hist['date'].max()
                df_industry = df_hist[df_hist['date'] == latest_date].drop_duplicates(subset=['Industry']).copy()
                
                # Get unique dates in ascending order
                unique_dates = pd.Series(sorted(df_hist['date'].unique()))
                
                # Find the last date of the previous calendar ISO week
                latest_year = latest_date.isocalendar().year
                latest_week = latest_date.isocalendar().week
                
                past_weeks_dates = unique_dates[
                    (unique_dates.dt.isocalendar().year < latest_year) |
                    ((unique_dates.dt.isocalendar().year == latest_year) & (unique_dates.dt.isocalendar().week < latest_week))
                ]
                
                prev_week_last_date = None
                if not past_weeks_dates.empty:
                    prev_week_last_date = past_weeks_dates.max()
                else:
                    # Fallback to date closest to 7 days ago
                    target_date_1w = latest_date - pd.Timedelta(days=7)
                    past_dates = unique_dates[unique_dates < latest_date]
                    if not past_dates.empty:
                        prev_week_last_date = past_dates.iloc[(past_dates - target_date_1w).abs().argsort()[:1]].values[0]
                
                if prev_week_last_date is not None:
                    df_prev = df_hist[df_hist['date'] == prev_week_last_date][['Industry', 'Rank']].rename(columns={'Rank': '1W_RS_Rank'})
                    df_prev['1W_RS_Rank'] = pd.to_numeric(df_prev['1W_RS_Rank'], errors='coerce')
                    df_industry = df_industry.merge(df_prev, on='Industry', how='left')
            else:
                df_industry = df_hist.head(len(df_hist.groupby('Industry')))
            numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile', '1W_RS_Rank', '1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank']
            for col in numeric_cols:
                if col in df_industry.columns:
                    df_industry[col] = pd.to_numeric(df_industry[col], errors='coerce')
            return df_industry
        except Exception as e:
            st.warning(f"Error loading historical industry data: {e}")

    industry_file = output_dir / "rs_industries.csv"
    if industry_file.exists():
        try:
            df_industry = pd.read_csv(industry_file)
            numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile', '1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank']
            for col in numeric_cols:
                if col in df_industry.columns:
                    df_industry[col] = pd.to_numeric(df_industry[col], errors='coerce')
            return df_industry
        except Exception as e:
            st.warning(f"Error loading industry data: {e}")
    return None

# ---------------------- Main data load ----------------------
output_sig  = compute_output_signature()
df          = load_csv_files(output_sig + "_force_reload_1")
df_industry = load_industry_data(output_sig)
company_descriptions = load_company_descriptions()
ibd_industry_mapping = load_ibd_ticker_industry_mapping()
symbol_ranks = load_ibd_data_tables_ranks()
industry_ranks = get_industry_ranks(ibd_industry_mapping, symbol_ranks)

if df is None or df.empty:
    st.error("No data found. Please check the CSV files in the output directory.")
    st.stop()

numeric_cols = ['Rank', 'Relative Strength', 'Percentile', '1M_RS_Percentile',
                '3M_RS_Percentile', '6M_RS_Percentile', 'Close', 'Price', 'MarketCap',
                'Float', 'ShortFloatPct', 'PctFrom52WkHigh', 'AvgVol10',
                'AvgVol30', 'AvgVol50', 'RevenueGrowth']
for col in numeric_cols:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')

# ---------------------- Ticker Cache Update (sidebar) ----------------------
TICKER_CACHE_UPDATE_SCRIPT = Path(__file__).resolve().parent / "python" / "update_ticker_cache.py"
TICKER_CACHE_UPDATE_LOG = Path(__file__).resolve().parent / "logs" / "ticker_cache_update.log"

def is_ticker_cache_updater_running():
    """Return pids of any running update_ticker_cache.py process (or [] if none)."""
    try:
        out = subprocess.run(["pgrep", "-f", "update_ticker_cache.py"],
                             capture_output=True, text=True)
        return [p for p in out.stdout.split() if p.strip()]
    except Exception:
        return []

def start_ticker_cache_update():
    """Launch update_ticker_cache.py in the background. Returns (ok, message)."""
    pids = is_ticker_cache_updater_running()
    if pids:
        return False, f"Update already running (pid {', '.join(pids)})."
    try:
        TICKER_CACHE_UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(TICKER_CACHE_UPDATE_LOG, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [sys.executable, str(TICKER_CACHE_UPDATE_SCRIPT)],
                cwd=str(Path(__file__).resolve().parent),
                stdout=lf, stderr=lf, start_new_session=True)
        return True, f"Started ticker cache update (pid {proc.pid})."
    except Exception as e:
        return False, f"Failed to start update: {e}"

st.sidebar.subheader("🔄 Ticker Cache")
_tc_pids = is_ticker_cache_updater_running()
if _tc_pids:
    st.sidebar.warning(f"⚠️ Updating tickers… ({len(_tc_pids)} process running). "
                       f"Price charts may show stale data until it finishes.")
    if st.sidebar.button("🛑 Stop Update", key="stop_ticker_cache_update",
                         help="Kill the running update_ticker_cache.py process"):
        for p in _tc_pids:
            try:
                subprocess.run(["kill", p], capture_output=True)
            except Exception:
                pass
        rerun_app()
else:
    st.sidebar.caption("Ticker price cache is idle.")
if st.sidebar.button("▶️ Update Ticker Cache", key="run_ticker_cache_update",
                     help="Fetch the latest daily bars for every ticker in ticker_cache/ (yfinance)"):
    ok, msg = start_ticker_cache_update()
    if ok:
        st.sidebar.success(msg)
        rerun_app()
    else:
        st.sidebar.warning(msg)

# ---------------------- Sidebar filters (only data filters, no ticker selection) ----------------------
st.sidebar.header("🔍 Filters")

has_historical = 'date' in df.columns

if has_historical:
    min_date = df['date'].min().date()
    max_date = df['date'].max().date()
    st.sidebar.subheader("📅 Date Range")
    date_range = st.sidebar.date_input(
        "Select date range",
        value=(max(min_date, max_date - timedelta(days=30)), max_date),
        min_value=min_date, max_value=max_date, key="date_range")
    if len(date_range) == 2:
        start_date, end_date = date_range
        filtered_df = df[(df['date'].dt.date >= start_date) & (df['date'].dt.date <= end_date)].copy()
    else:
        filtered_df = df.copy()
else:
    selected_file = st.sidebar.selectbox(
        "Select Dataset",
        options=['All Files'] + sorted(df['source_file'].unique().tolist()))
    filtered_df = df[df['source_file'] == selected_file].copy() if selected_file != 'All Files' else df.copy()

sectors = ['All'] + sorted(filtered_df['Sector'].dropna().unique().tolist())
selected_sectors = st.sidebar.multiselect("Sector", sectors, default=['All'])
if 'All' not in selected_sectors:
    filtered_df = filtered_df[filtered_df['Sector'].isin(selected_sectors)]

min_percentile = st.sidebar.slider("Min Percentile", 0, 100, 0)
filtered_df    = filtered_df[filtered_df['Percentile'] >= min_percentile]

min_rs = st.sidebar.number_input("Min Relative Strength", value=0.0)
filtered_df    = filtered_df[filtered_df['Relative Strength'] >= min_rs]

# Build full ticker list sorted by industry strength (will be used in Company Details tab)
def get_all_tickers_sorted_by_industry(filtered_df, industry_df):
    if 'date' in filtered_df.columns:
        latest_snapshot = filtered_df[filtered_df['date'] == filtered_df['date'].max()].drop_duplicates(subset=['Ticker'])
    else:
        latest_snapshot = filtered_df.drop_duplicates(subset=['Ticker'])
    if industry_df is not None and not industry_df.empty:
        industry_rs_map = dict(zip(industry_df['Industry'], industry_df['Relative Strength']))
    else:
        industry_rs_map = {}
    latest_snapshot['Industry_RS'] = latest_snapshot['Industry'].map(industry_rs_map).fillna(0)
    sorted_df = latest_snapshot.sort_values(['Industry_RS', 'Relative Strength'], ascending=[False, False])
    return sorted_df['Ticker'].dropna().astype(str).tolist()

all_sorted_tickers = get_all_tickers_sorted_by_industry(filtered_df, df_industry)

# ---------------------- Tabs ----------------------
if has_historical:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11, tab12, tab13 = st.tabs(
        ["📈 Overview", "📊 Time Series", "🎯 Top Performers", "🔬 Deep Analysis",
         "📉 Trends", "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table", "🔍 Pattern Finder", "🏆 IBD Pattern", "📝 Daily Report Card", "📸 MarketSurge Screenshots", "🎙️ IBD Live Summary"])
else:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11, tab12 = st.tabs(
        ["📈 Overview", "🎯 Top Performers", "📊 Distributions", "🔬 Deep Analysis",
         "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table", "🔍 Pattern Finder", "🏆 IBD Pattern", "📝 Daily Report Card", "📸 MarketSurge Screenshots", "🎙️ IBD Live Summary"])

tab_ibd_pattern = tab10 if has_historical else tab9
tab_drc = tab11 if has_historical else tab10
tab_ms = tab12 if has_historical else tab11
tab_ibd_live = tab13 if has_historical else tab12


# ---------- TAB 1: Overview ----------
with tab1:
    col1, col2, col3, col4 = st.columns(4)
    with col1: st.metric("Total Stocks",   len(filtered_df))
    with col2: st.metric("Avg RS",         f"{filtered_df['Relative Strength'].mean():.1f}")
    with col3: st.metric("Avg Percentile", f"{filtered_df['Percentile'].mean():.1f}")
    price_col = 'Close' if 'Close' in filtered_df.columns else 'Price'
    with col4: st.metric("Avg Close",      f"${filtered_df[price_col].mean():.2f}")
    if has_historical:
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("📊 Data Coverage")
            if 'date' in filtered_df.columns:
                st.metric("Trading Days",  filtered_df['date'].nunique())
                st.metric("Unique Stocks", filtered_df['Ticker'].nunique())
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        sector_counts = filtered_df.drop_duplicates(subset=['Ticker'])['Sector'].value_counts().head(10)
        fig = px.bar(x=sector_counts.values, y=sector_counts.index, orientation='h',
                     title="Top 10 Sectors by Stock Count", labels={'x': 'Count', 'y': 'Sector'})
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        avg_rs_by_sector = filtered_df.drop_duplicates(subset=['Ticker']).groupby('Sector')['Relative Strength'].mean().sort_values(ascending=False).head(10)
        fig = px.bar(x=avg_rs_by_sector.values, y=avg_rs_by_sector.index, orientation='h',
                     title="Top 10 Sectors by Avg RS", labels={'x': 'Avg RS', 'y': 'Sector'},
                     color=avg_rs_by_sector.values, color_continuous_scale='Viridis')
        st.plotly_chart(fig, use_container_width=True)

# ---------- TAB 2: Time Series ----------
if has_historical:
    with tab2:
        st.subheader("📈 Time Series Analysis")
        col1, col2 = st.columns(2)
        with col1:
            daily_avg = filtered_df.groupby('date')['Relative Strength'].agg(['mean', 'median', 'max']).reset_index()
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=daily_avg['date'], y=daily_avg['mean'],   name='Mean RS',   mode='lines'))
            fig.add_trace(go.Scatter(x=daily_avg['date'], y=daily_avg['median'], name='Median RS', mode='lines'))
            fig.update_layout(title="Daily Average RS Trend", xaxis_title="Date", yaxis_title="RS Value", hovermode='x unified')
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            daily_percentile = filtered_df.groupby('date')['Percentile'].mean().reset_index()
            fig = px.line(daily_percentile, x='date', y='Percentile', title="Daily Average Percentile Trend")
            st.plotly_chart(fig, use_container_width=True)
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            daily_count         = filtered_df.groupby('date')['Ticker'].nunique().reset_index()
            daily_count.columns = ['date', 'stock_count']
            fig = px.line(daily_count, x='date', y='stock_count', title="Number of Stocks in Universe Over Time")
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            price_col = 'Close' if 'Close' in filtered_df.columns else 'Price'
            daily_price = filtered_df.groupby('date')[price_col].mean().reset_index()
            fig = px.line(daily_price, x='date', y=price_col, title="Average Stock Close Price Over Time")
            st.plotly_chart(fig, use_container_width=True)
        st.divider()
        st.subheader("📊 Sector RS Trends")
        selected_sector_trend = st.selectbox("Select sector for trend", filtered_df['Sector'].unique(), key="sector_trend")
        sector_trend_data = filtered_df[filtered_df['Sector'] == selected_sector_trend].groupby('date')['Relative Strength'].agg(['mean', 'min', 'max']).reset_index()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['mean'], name='Mean RS', mode='lines+markers'))
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['max'],  name='Max RS',  fill='tozeroy', mode='lines', opacity=0.2))
        fig.add_trace(go.Scatter(x=sector_trend_data['date'], y=sector_trend_data['min'],  name='Min RS',  fill='tonexty', mode='lines', opacity=0.2))
        fig.update_layout(title=f"{selected_sector_trend} - RS Trend", hovermode='x unified')
        st.plotly_chart(fig, use_container_width=True)
else:
    with tab2:
        col1, col2 = st.columns(2)
        with col1:
            fig = px.histogram(filtered_df, x='Relative Strength', nbins=50, title="Distribution of Relative Strength")
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            fig = px.histogram(filtered_df, x='Percentile', nbins=50, title="Distribution of Percentile Rank")
            st.plotly_chart(fig, use_container_width=True)

# ---------- TAB 3: Top Performers ----------
with tab3:
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🏆 Top 15 by Relative Strength")
        price_col = 'Close' if 'Close' in filtered_df.columns else 'Price'
        top_rs = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'Relative Strength')[['Rank', 'Ticker', 'Sector', 'Relative Strength', 'Percentile', price_col]].copy()
        st.dataframe(top_rs.reset_index(drop=True), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("⭐ Top 15 by Percentile")
        top_percentile = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'Percentile')[['Rank', 'Ticker', 'Sector', 'Percentile', 'Relative Strength', price_col]].copy()
        st.dataframe(top_percentile.reset_index(drop=True), use_container_width=True, hide_index=True)
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("💰 Top 15 by Market Cap")
        top_mcap = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, 'MarketCap')[['Ticker', 'Sector', 'MarketCap', 'Relative Strength', 'Percentile']].copy()
        top_mcap['MarketCap'] = top_mcap['MarketCap'].apply(lambda x: f"${x/1e9:.2f}B" if pd.notna(x) else "N/A")
        st.dataframe(top_mcap.reset_index(drop=True), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("📈 Highest 6M")
        top_6m = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').nlargest(15, '6M_RS_Percentile')[['Ticker', '6M_RS_Percentile', '3M_RS_Percentile', '1M_RS_Percentile']].copy()
        top_6m = top_6m.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
        st.dataframe(top_6m.reset_index(drop=True), use_container_width=True, hide_index=True)

# ---------- TAB 4: Deep Analysis ----------
with tab4:
    col1, col2 = st.columns(2)
    with col1:
        price_col = 'Close' if 'Close' in filtered_df.columns else 'Price'
        fig = px.scatter(filtered_df, x=price_col, y='Relative Strength', color='Percentile',
                         hover_data=['Ticker', 'Sector'], title="Relative Strength vs Close Price",
                         color_continuous_scale='Viridis')
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        percentile_data = filtered_df[['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile']].mean()
        fig = go.Figure(data=[
            go.Bar(name='1M', x=['1M'], y=[percentile_data['1M_RS_Percentile']]),
            go.Bar(name='3M', x=['3M'], y=[percentile_data['3M_RS_Percentile']]),
            go.Bar(name='6M', x=['6M'], y=[percentile_data['6M_RS_Percentile']]),
        ])
        fig.update_layout(title="Average RS Percentile Comparison", barmode='group')
        st.plotly_chart(fig, use_container_width=True)
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        fig = px.histogram(filtered_df.dropna(subset=['PctFrom52WkHigh']), x='PctFrom52WkHigh',
                           nbins=40, title="Distribution of % from 52W High")
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        fig = px.scatter(filtered_df.dropna(subset=['RevenueGrowth']), x='RevenueGrowth',
                         y='Relative Strength', color='Percentile', hover_data=['Ticker', 'Sector'],
                         title="Revenue Growth vs Relative Strength", color_continuous_scale='Viridis')
        st.plotly_chart(fig, use_container_width=True)
    st.divider()
    st.subheader("Key Statistics Summary")
    summary_stats = filtered_df[['Relative Strength', 'Percentile', '1M_RS_Percentile', '3M_RS_Percentile',
                                  '6M_RS_Percentile', 'Close' if 'Close' in filtered_df.columns else 'Price', 'AvgVol10', 'AvgVol30', 'AvgVol50',
                                  'ShortFloatPct', 'PctFrom52WkHigh', 'RevenueGrowth']].describe()
    st.dataframe(summary_stats, use_container_width=True)

# ---------- TAB 5: Trends ----------
if has_historical:
    with tab5:
        st.subheader("📉 Trend Analysis")
        col1, col2 = st.columns(2)
        latest_date = filtered_df['date'].max()
        oldest_date = filtered_df['date'].min()
        with col1:
            latest_rs  = filtered_df[filtered_df['date'] == latest_date].groupby('Sector')['Relative Strength'].mean()
            oldest_rs  = filtered_df[filtered_df['date'] == oldest_date].groupby('Sector')['Relative Strength'].mean()
            momentum   = (latest_rs - oldest_rs).sort_values(ascending=False)
            fig = px.bar(x=momentum.values, y=momentum.index, orientation='h',
                         title=f"RS Change by Sector ({oldest_date.date()} to {latest_date.date()})",
                         color=momentum.values, color_continuous_scale='RdYlGn')
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            latest_stocks  = filtered_df[filtered_df['date'] == latest_date][['Ticker', 'Sector', 'Relative Strength']].drop_duplicates(subset=['Ticker'], keep='first').copy()
            oldest_stocks  = filtered_df[filtered_df['date'] == oldest_date][['Ticker', 'Relative Strength']].drop_duplicates(subset=['Ticker'], keep='first').copy()
            if not latest_stocks.empty and not oldest_stocks.empty:
                merged = latest_stocks.merge(oldest_stocks, on='Ticker', suffixes=('_latest', '_oldest'))
                merged['RS_change'] = merged['Relative Strength_latest'] - merged['Relative Strength_oldest']
                top_gainers = merged.nlargest(10, 'RS_change')[['Ticker', 'Sector', 'RS_change']]
                fig = px.bar(top_gainers, x='RS_change', y='Ticker', orientation='h',
                             title="Top 10 RS Gainers", color='RS_change', color_continuous_scale='Greens')
                st.plotly_chart(fig, use_container_width=True)

# ---------- TAB 6: Industry Rotation (corrected delta calculations) ----------
with (tab6 if has_historical else tab5):
    st.subheader("🏭 Industry Rotation & Analysis")
    if df_industry is not None and not df_industry.empty:
        ind_display  = df_industry.copy()
        has_ranks    = all(col in ind_display.columns for col in ['1M_RS_Rank', '3M_RS_Rank', '6M_RS_Rank'])
        has_percentiles = all(col in ind_display.columns for col in ['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile'])

        if has_ranks and 'Rank' in ind_display.columns:
            # Deltas: positive = improvement (current rank better, i.e., lower number)
            if '1W_RS_Rank' in ind_display.columns:
                ind_display['Delta Rank-1W'] = ind_display['1W_RS_Rank'] - ind_display['Rank']
            ind_display['Delta Rank-1M'] = ind_display['1M_RS_Rank'] - ind_display['Rank']
            ind_display['Delta Rank-3M-1M'] = ind_display['3M_RS_Rank'] - ind_display['1M_RS_Rank']
            ind_display['Delta Rank-6M-3M'] = ind_display['6M_RS_Rank'] - ind_display['3M_RS_Rank']
            
            if '1W_RS_Rank' in ind_display.columns:
                ind_display = ind_display.rename(columns={'1W_RS_Rank': '1W'})
            ind_display = ind_display.rename(columns={'1M_RS_Rank': '1M', '3M_RS_Rank': '3M', '6M_RS_Rank': '6M'})
            
            convert_cols = ['Rank', '1M', '3M', '6M', 'Delta Rank-1M', 'Delta Rank-3M-1M', 'Delta Rank-6M-3M']
            if '1W' in ind_display.columns:
                convert_cols.extend(['1W', 'Delta Rank-1W'])
                
            for c in convert_cols:
                if c in ind_display.columns:
                    ind_display[c] = pd.to_numeric(ind_display[c], errors='coerce').round().astype('Int64')
            
            cols_to_show = ['Rank', 'Industry']
            if '1W' in ind_display.columns:
                cols_to_show.extend(['1W', 'Delta Rank-1W'])
            cols_to_show.extend(['1M', '3M', '6M', 'Delta Rank-1M', 'Delta Rank-3M-1M', 'Delta Rank-6M-3M', 'Top 10 Tickers'])
        elif has_percentiles and 'Rank' in ind_display.columns:
            # For percentiles, we cannot do rank comparison – skip deltas or compute differently
            ind_display = ind_display.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
            cols_to_show = ['Rank', 'Industry', '1M', '3M', '6M', 'Top 10 Tickers']
        else:
            cols_to_show = ['Rank', 'Industry', 'Top 10 Tickers']

        if 'date' in df.columns:
            latest_snapshot = df[df['date'] == df['date'].max()].drop_duplicates(subset=['Ticker'])
        else:
            latest_snapshot = filtered_df.drop_duplicates(subset=['Ticker'])

        top_tickers = []
        for _, ind_row in ind_display.iterrows():
            industry_name = ind_row.get('Industry', '')
            tickers_df    = latest_snapshot[latest_snapshot['Industry'] == industry_name][['Ticker', 'Relative Strength']].dropna()
            top10 = tickers_df.sort_values('Relative Strength', ascending=False).head(10)['Ticker'].tolist() if not tickers_df.empty else []
            top_tickers.append(', '.join(top10))
        ind_display['Top 10 Tickers'] = top_tickers

        available_cols = [c for c in cols_to_show if c in ind_display.columns]
        if 'Rank' in ind_display.columns:
            ind_display = ind_display.sort_values('Rank', ascending=True)
        display_data = ind_display[available_cols].reset_index(drop=True)

        # ----------------- Filter & Sort Controls -----------------
        st.markdown("### 🔍 Filter & Sort Industries")
        
        numerical_cols = [c for c in available_cols if c not in ['Industry', 'Top 10 Tickers']]
        
        # Expander for specific range filters on each numeric column
        with st.expander("🎯 Filter by Numerical Column Ranges"):
            cols_f = st.columns(3)
            filtered_data = display_data.copy()
            for idx, c in enumerate(numerical_cols):
                col_ui = cols_f[idx % 3]
                with col_ui:
                    series_clean = display_data[c].dropna()
                    if not series_clean.empty:
                        min_val = int(series_clean.min())
                        max_val = int(series_clean.max())
                        if min_val < max_val:
                            val_range = st.slider(f"{c} Range", min_val, max_val, (min_val, max_val), key=f"filter_slider_{c}")
                            filtered_data = filtered_data[(filtered_data[c] >= val_range[0]) & (filtered_data[c] <= val_range[1])]
                        else:
                            st.write(f"{c}: {min_val} (single value)")
        
        col_top_1, col_top_2 = st.columns(2)
        with col_top_1:
            top_x = st.number_input("Show Top X Rows", min_value=1, value=len(filtered_data), key="top_x_industries_filter")
        with col_top_2:
            sort_col = st.selectbox("Sort by Column for Top X", options=numerical_cols, index=0, key="top_x_sort_col")
            default_asc = not ("Delta" in sort_col)
            ascending = st.checkbox("Sort Ascending (uncheck for Descending)", value=default_asc, key="top_x_ascending")
            
        filtered_data = filtered_data.sort_values(by=sort_col, ascending=ascending)
        filtered_data = filtered_data.head(top_x).reset_index(drop=True)

        def color_delta(val):
            if pd.isna(val): return ''
            try:
                v = float(val)
                return f'color: {"green" if v > 0 else "red" if v < 0 else "gray"}'
            except: return ''

        styled_df    = filtered_data.style
        delta_cols   = [c for c in ['Delta Rank-1W', 'Delta Rank-1M', 'Delta Rank-3M-1M', 'Delta Rank-6M-3M'] if c in available_cols]
        if delta_cols:
            if hasattr(styled_df, 'map'):
                styled_df = styled_df.map(color_delta, subset=delta_cols)
            else:
                styled_df = styled_df.applymap(color_delta, subset=delta_cols)
        st.dataframe(styled_df, hide_index=True)

        # ----------------- Combined Copy/Paste Ticker List -----------------
        st.divider()
        st.subheader("📋 Tickers from Selected Industries")
        
        max_tickers_per_ind = st.number_input(
            "Max tickers per industry in list", 
            min_value=1, 
            max_value=100, 
            value=10, 
            key="ind_rot_max_tickers_per_ind"
        )
        
        combined_tickers = []
        tv_combined_lines = []
        ibkr_combined_lines = []
        for industry in filtered_data['Industry']:
            tickers_df = latest_snapshot[latest_snapshot['Industry'] == industry][['Ticker', 'Relative Strength']].dropna()
            top_tickers = tickers_df.sort_values('Relative Strength', ascending=False).head(max_tickers_per_ind)['Ticker'].tolist()
            combined_tickers.extend(top_tickers)
            if top_tickers:
                tv_combined_lines.append(f"###{industry}")
                tv_combined_lines.extend(top_tickers)
                for t in top_tickers:
                    ibkr_combined_lines.append(f"SYM, {t.upper()}, SMART/ARCA")
            
        combined_tickers_str = ','.join(combined_tickers)
        tv_combined_str = "\n".join(tv_combined_lines)
        ibkr_combined_str = "\n".join(ibkr_combined_lines)
        
        st.write(f"**Industries Combined (Sorted by Table Order First, then by Strongest Ticker)** ({len(combined_tickers)} tickers)")
        col1_cp, col2_cp = st.columns([4, 1])
        with col1_cp:
            st.code(combined_tickers_str, language="text")
        with col2_cp:
            st.download_button(
                "Copy/Download All", 
                combined_tickers_str,
                "selected_industries_tickers.txt", 
                "text/plain",
                key="download_selected_ind_tickers"
            )
            st.download_button(
                "TradingView", 
                tv_combined_str,
                "selected_industries_tv_watchlist.txt", 
                "text/plain",
                key="download_selected_ind_tv_watchlist"
            )
            st.download_button(
                "IBKR", 
                ibkr_combined_str,
                "selected_industries_ibkr_watchlist.txt", 
                "text/plain",
                key="download_selected_ind_ibkr_watchlist"
            )
    else:
        st.warning("Industry RS data not found.")

# ---------------------- Ticker Data Caching (with error handling) ----------------------
TICKER_CACHE_DIR = Path(__file__).resolve().parent / "ticker_cache"
TICKER_CACHE_DIR.mkdir(exist_ok=True)

def get_ticker_cache_path(ticker, interval):
    return TICKER_CACHE_DIR / f"{ticker}_{interval}.parquet"

def load_or_fetch_ticker(ticker, interval="1d", period="2y"):
    cache_path = get_ticker_cache_path(ticker, interval)
    today      = pd.Timestamp.now().normalize()

    def _strip_tz(df):
        if df is not None and not df.empty and getattr(df.index, 'tz', None) is not None:
            df.index = df.index.tz_localize(None)
        return df

    if cache_path.exists():
        try:
            df = pd.read_parquet(cache_path)
            df.index = pd.to_datetime(df.index)
            if getattr(df.index, 'tz', None) is not None:
                df.index = df.index.tz_localize(None)
            last_date = df.index.max()
            if hasattr(last_date, 'tz_localize') and last_date.tzinfo is not None:
                last_date = last_date.tz_localize(None)
            last_date = last_date.normalize()
            if (today - last_date).days <= 2:
                return df
            start    = last_date + pd.Timedelta(days=1)
            end      = today + pd.Timedelta(days=1)
            new_data = yf.Ticker(ticker).history(start=start, end=end, interval=interval)
            if not new_data.empty:
                new_data = _strip_tz(new_data)
                df = pd.concat([df, new_data])
                df = df[~df.index.duplicated(keep='first')].sort_index()
                df.to_parquet(cache_path)
            return df
        except Exception as e:
            print(f"Error loading cache for {ticker} ({interval}): {e}")
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval)
        if not df.empty:
            df = _strip_tz(df)
            if cache_path.parent.exists():
                df.to_parquet(cache_path)
        return df
    except Exception as e:
        print(f"Failed to fetch ticker {ticker}: {e}")
        return pd.DataFrame()

def load_or_fetch_weekly(ticker):
    return load_or_fetch_ticker(ticker, interval="1wk", period="3y")

# ---------------------- RS Signal Detection ----------------------
def compute_rs_signals(df, spy_series, scaling_factor=7.0):
    rs_raw            = df['Close'] * scaling_factor * 1000 / spy_series
    rs_ema_quick      = rs_raw.ewm(span=21, adjust=False).mean()
    rs_ema_quicksand  = rs_raw.ewm(span=34, adjust=False).mean()
    rs_ema_gd         = rs_raw.ewm(span=50, adjust=False).mean()
    quick_break       = (rs_raw < rs_ema_quick) & (rs_raw.shift(1) >= rs_ema_quick.shift(1))
    gd_break          = (rs_raw < rs_ema_gd)    & (rs_raw.shift(1) >= rs_ema_gd.shift(1))
    rs_reclaim        = (rs_raw > rs_ema_quick)  & (rs_raw.shift(1) <= rs_ema_quick.shift(1))
    quicksand         = pd.Series(False, index=df.index)
    last_break_level  = None
    for i in range(1, len(df)):
        if quick_break.iloc[i]:
            last_break_level = rs_raw.iloc[i]
        if last_break_level is not None and rs_raw.iloc[i] < rs_ema_quick.iloc[i] and rs_raw.iloc[i] < last_break_level:
            quicksand.iloc[i] = True
    lookbacks = {'1Y': 252, '6M': 126, '3M': 63}
    rs_new_high   = {}
    rs_new_low    = {}
    price_new_high = {}
    for name, lb in lookbacks.items():
        if len(df) > lb:
            rs_new_high[name]    = rs_raw  > rs_raw.shift(1).rolling(lb, min_periods=lb).max()
            rs_new_low[name]     = rs_raw  < rs_raw.shift(1).rolling(lb, min_periods=lb).min()
            price_new_high[name] = df['High'] > df['High'].shift(1).rolling(lb, min_periods=lb).max()
        else:
            rs_new_high[name]    = pd.Series(False, index=df.index)
            rs_new_low[name]     = pd.Series(False, index=df.index)
            price_new_high[name] = pd.Series(False, index=df.index)
    rs_nh_any = rs_new_high['1Y'] | rs_new_high['6M'] | rs_new_high['3M']
    rs_leads  = pd.Series(False, index=df.index)
    for i in range(len(df)):
        for k in ['1Y', '6M', '3M']:
            if rs_new_high[k].iloc[i] and not price_new_high[k].iloc[i]:
                rs_leads.iloc[i] = True; break
    return pd.DataFrame({
        'rs_raw': rs_raw, 'rs_ema_quick': rs_ema_quick,
        'rs_ema_quicksand': rs_ema_quicksand, 'rs_ema_gd': rs_ema_gd,
        'quick_break': quick_break, 'gd_break': gd_break,
        'rs_reclaim': rs_reclaim, 'quicksand': quicksand,
        'rs_new_high_any': rs_nh_any, 'rs_leads_price': rs_leads,
        'rs_new_low': rs_new_low['1Y'],
    })

# ---------------------- Shared Ticker Plotly Chart (Company Details style) ----------------------
def build_ticker_price_chart(ticker, filtered_df=None, height=800):
    """
    Build the same 'Plotly (Advanced)' daily candlestick + pattern + volume + RS chart used in
    the Company Details tab, for an arbitrary ticker. Self-contained (no markers/weekly/chart-type
    UI) so it can be reused from other tabs (e.g. IBD Live) without touching Company Details' logic.

    Returns (fig, error_message). fig is None if data could not be fetched/plotted.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None, "No ticker specified."
    try:
        df_daily_full = load_or_fetch_ticker(ticker, interval="1d", period="2y")
        if df_daily_full.empty:
            return None, f"No daily data available for {ticker}. Please check the ticker symbol."
        df_daily = df_daily_full.iloc[-252:] if len(df_daily_full) > 252 else df_daily_full

        spy = yf.Ticker("^GSPC")
        try:
            spy_daily_full = spy.history(period="2y", interval="1d")
            if not spy_daily_full.empty and getattr(spy_daily_full.index, 'tz', None) is not None:
                spy_daily_full.index = spy_daily_full.index.tz_localize(None)
            spy_daily = spy_daily_full.iloc[-252:] if len(spy_daily_full) > 252 else spy_daily_full
        except Exception:
            spy_daily = pd.DataFrame()

        common_idx = df_daily.index.intersection(spy_daily.index) if not spy_daily.empty else []
        if len(common_idx) > 0:
            df_daily = df_daily.loc[common_idx]
            spy_daily = spy_daily.loc[common_idx]

        daily_rec_vis = PatternRecognizer(weekly=False, base_depth=0.50, pivot_length=9, volume_period=50)
        for i, (_, row) in enumerate(df_daily.iterrows()):
            daily_rec_vis.process_bar(row['High'], row['Low'], row['Close'], row['Volume'], row['Open'], i)
        daily_painter = PatternPainter(df_daily, daily_rec_vis, label_prices=True)

        if not spy_daily.empty and len(df_daily) == len(spy_daily):
            signals = compute_rs_signals(df_daily, spy_daily['Close'], scaling_factor=7.0)
            df_daily = pd.concat([df_daily, signals], axis=1)
        else:
            for col in ['rs_raw', 'rs_ema_quick', 'rs_ema_quicksand', 'rs_ema_gd',
                        'quick_break', 'gd_break', 'rs_reclaim', 'quicksand',
                        'rs_new_high_any', 'rs_leads_price', 'rs_new_low']:
                df_daily[col] = np.nan

        vol = df_daily['Volume'].astype(float)
        close = df_daily['Close'].astype(float)
        avg_vol = vol.rolling(50, min_periods=1).mean()
        avg_vol_safe = avg_vol.replace(0, np.nan)
        vol_ratio = (vol / avg_vol_safe * 100).fillna(0)
        dry_lvl1 = (vol_ratio < 55).astype(bool)
        dry_lvl2 = (vol_ratio < 40).astype(bool)
        up_day = (close > close.shift(1)).astype(bool)
        day_range = df_daily['High'] - df_daily['Low']
        close_chg_pct = ((close - close.shift(1)) / close.shift(1) * 100).replace([np.inf, -np.inf], np.nan).fillna(0)
        in_lower_27 = (close <= df_daily['Low'] + 0.27 * day_range).astype(bool)
        stall_day = ((close_chg_pct >= 0) & (close_chg_pct < 0.1) & in_lower_27).astype(bool)
        churn_vol = (vol >= avg_vol * 1.2).astype(bool)
        churn_pct = (np.abs(close_chg_pct) < 0.25).astype(bool)
        in_lower_50 = (close <= df_daily['Low'] + 0.5 * day_range).astype(bool)
        churn_day = (churn_vol & churn_pct & in_lower_50 & (~stall_day)).astype(bool)

        def pocket_pivot_bool(vol_series, close_series, window):
            result = pd.Series(False, index=vol_series.index)
            for i in range(window, len(vol_series)):
                up_vols = vol_series.iloc[i-window:i][close_series.iloc[i-window:i] > close_series.iloc[i-window:i].shift(1)]
                if len(up_vols) > 0:
                    result.iloc[i] = (vol_series.iloc[i] > up_vols.max()) and (close_series.iloc[i] > close_series.iloc[i-1])
            return result

        pp10 = pocket_pivot_bool(vol, close, 10)
        pp5 = pocket_pivot_bool(vol, close, 5)
        pp5_only = pp5 & ~pp10
        lower_low = (close < close.shift(1)).astype(bool)
        rising_vol = (vol > vol.shift(1)).astype(bool)
        ll3 = (lower_low & rising_vol).astype(bool)
        ll3_signal = (ll3 & ll3.shift(1) & ll3.shift(2)).astype(bool)
        marker_y = vol * 1.05

        percentile = None
        if filtered_df is not None and 'Ticker' in filtered_df.columns:
            trows = filtered_df[filtered_df['Ticker'] == ticker]
            if not trows.empty:
                trow = trows.sort_values('date').iloc[-1] if 'date' in trows.columns else trows.iloc[0]
                percentile = trow.get('Percentile')

        snapshot_text = f"<b>{ticker}</b>"
        if percentile is not None and not pd.isna(percentile):
            snapshot_text += f"<br>Pctl: {int(round(float(percentile)))}"

        ema10 = df_daily['Close'].ewm(span=10, adjust=False).mean()
        ema21 = df_daily['Close'].ewm(span=21, adjust=False).mean()
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                            subplot_titles=(f"{ticker} Daily", 'Volume with Indicators', 'Raw RS & QGDRS EMAs'),
                            row_heights=[0.5, 0.2, 0.3])
        fig.add_trace(go.Candlestick(x=df_daily.index, open=df_daily['Open'], high=df_daily['High'],
                                     low=df_daily['Low'], close=df_daily['Close'],
                                     name='Price', showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=df_daily.index, y=ema10, mode='lines', name='EMA(10)',
                                 line=dict(color='#FF9800', width=2)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df_daily.index, y=ema21, mode='lines', name='EMA(21)',
                                 line=dict(color='#2196F3', width=2)), row=1, col=1)

        for tr in daily_painter.get_plotly_traces():
            fig.add_trace(tr, row=1, col=1)
        pattern_annotations = daily_painter.get_pending_annotations()

        vol_colors = [
            'rgba(247,153,2,0.7)' if dry_lvl2.loc[idx]
            else 'rgba(225,181,69,0.6)' if dry_lvl1.loc[idx]
            else '#26a69a' if up_day.loc[idx] else '#ef5350'
            for idx in df_daily.index
        ]
        fig.add_trace(go.Bar(x=df_daily.index, y=df_daily['Volume'], marker=dict(color=vol_colors),
                             name='Volume', showlegend=False), row=2, col=1)
        fig.add_trace(go.Scatter(x=df_daily.index, y=avg_vol, name='Volume SMA(50)',
                                 line=dict(color='orange', width=1.5)), row=2, col=1)
        if not pp10.loc[pp10].empty:
            fig.add_trace(go.Scatter(x=pp10[pp10].index, y=marker_y[pp10], mode='markers',
                                     marker=dict(symbol='diamond', size=12, color='yellow'),
                                     name='Pocket Pivot (10d)'), row=2, col=1)
        if not pp5_only.loc[pp5_only].empty:
            fig.add_trace(go.Scatter(x=pp5_only[pp5_only].index, y=marker_y[pp5_only]*0.98, mode='markers',
                                     marker=dict(symbol='diamond', size=10, color='blue'),
                                     name='Pocket Pivot (5d)'), row=2, col=1)
        if not churn_day.loc[churn_day].empty:
            fig.add_trace(go.Scatter(x=churn_day[churn_day].index, y=marker_y[churn_day], mode='markers',
                                     marker=dict(symbol='x', size=12, color='purple'),
                                     name='Churning Day'), row=2, col=1)
        if not stall_day.loc[stall_day].empty:
            fig.add_trace(go.Scatter(x=stall_day[stall_day].index, y=marker_y[stall_day], mode='markers',
                                     marker=dict(symbol='circle', size=10, color='maroon',
                                                 line=dict(width=2, color='black')),
                                     name='Stall Day'), row=2, col=1)
        if not ll3_signal.loc[ll3_signal].empty:
            fig.add_trace(go.Scatter(x=ll3_signal[ll3_signal].index, y=marker_y[ll3_signal], mode='markers',
                                     marker=dict(symbol='triangle-up', size=12, color='red'),
                                     name='3LL Signal'), row=2, col=1)

        if 'rs_raw' in df_daily.columns:
            fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_raw'], name='Raw RS',
                                     line=dict(color='blue', width=2)), row=3, col=1)
            fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_ema_quick'], name='Quick EMA (21)',
                                     line=dict(color='#56b8e6', width=2)), row=3, col=1)
            fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_ema_quicksand'], name='Quicksand EMA (34)',
                                     line=dict(color='#ff8c00', width=2)), row=3, col=1)
            fig.add_trace(go.Scatter(x=df_daily.index, y=df_daily['rs_ema_gd'], name='GD EMA (50)',
                                     line=dict(color='#2ca02c', width=2)), row=3, col=1)

            for sig, sym, col, label in [
                ('quick_break', 'x', 'yellow', 'Quick Break'),
                ('quicksand', 'x', 'orange', 'Quicksand'),
                ('gd_break', 'x', 'red', 'GD Break'),
                ('rs_reclaim', 'triangle-up', 'lime', 'RS Reclaim'),
                ('rs_new_high_any', 'circle', '#0000FF', 'RS New High'),
                ('rs_leads_price', 'circle', '#00ffd9', 'RS Leads Price'),
                ('rs_new_low', 'circle', '#FF0000', 'RS New Low')
            ]:
                sub = df_daily[df_daily[sig] == True]
                if not sub.empty:
                    y_col = 'rs_ema_gd' if sig == 'gd_break' else 'rs_ema_quick' if sig in ('quick_break', 'quicksand', 'rs_reclaim') else 'rs_raw'
                    fig.add_trace(go.Scatter(x=sub.index, y=sub[y_col], mode='markers',
                                             marker=dict(symbol=sym, size=8 if 'new' in sig else 10, color=col, opacity=0.7),
                                             name=label, hovertemplate=f'{label}<extra></extra>'), row=3, col=1)

        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
        if percentile is not None and not pd.isna(percentile):
            fig.add_annotation(x=df_daily.index[-1], y=float(df_daily['Close'].iloc[-1]),
                               text=f"{int(round(float(percentile)))}",
                               showarrow=True, arrowhead=2, arrowsize=1.5, arrowcolor='#636efa',
                               ax=40, ay=-40, bgcolor="rgba(255,255,255,0.85)",
                               bordercolor="#636efa", borderwidth=1.5,
                               font=dict(size=13, color="#636efa"))
        fig.add_annotation(x=0, y=0.98, xref="paper", yref="paper",
                           text=snapshot_text, showarrow=False, align="left",
                           bgcolor="rgba(255,255,255,0.85)",
                           font=dict(color="#1e1e1e", size=11, family="monospace"),
                           bordercolor="#cccccc", borderwidth=1, borderpad=8,
                           xanchor="left", yanchor="top")
        fig.update_layout(annotations=fig.layout.annotations + tuple(pattern_annotations),
                          xaxis_rangeslider_visible=False, height=height,
                          margin=dict(l=20, r=20, t=40, b=20),
                          legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5))
        return fig, None
    except Exception as e:
        return None, f"Error building chart for {ticker}: {e}"

# ========================================================================================
# TAB 7: Company Details — with its own ticker filter (search + selectbox)
# ========================================================================================
with (tab7 if has_historical else tab6):
    st.subheader("💼 Company Details")

    st.markdown("### 📋 Select a Ticker")
    search_term = st.text_input("🔍 Search ticker", key="company_search_input").strip().upper()
    if ":" in search_term:
        search_term = search_term.split(":")[-1].strip()
    if search_term:
        filtered_tickers = [t for t in all_sorted_tickers if isinstance(t, str) and search_term in t]
    else:
        filtered_tickers = all_sorted_tickers

    if filtered_tickers:
        current_ticker = st.session_state.selected_ticker
        if current_ticker in filtered_tickers:
            idx = filtered_tickers.index(current_ticker)
        else:
            idx = 0
        chosen_ticker = st.selectbox("Ticker", filtered_tickers, index=idx, key="company_ticker_selector")
        if chosen_ticker != st.session_state.selected_ticker:
            st.session_state.selected_ticker = chosen_ticker
            rerun_app()
    else:
        st.warning("No tickers match your search.")
        chosen_ticker = None

    selected_ticker_company = st.session_state.selected_ticker
    if selected_ticker_company is None:
        st.info("👆 Use the search and select a ticker above to view details.")
    else:
        st.write(f"Showing details for **{selected_ticker_company}**")
        show_log_price = st.checkbox("Show log(Price) instead of Price", key="log_price_toggle")
        ticker_rows    = filtered_df[filtered_df['Ticker'] == selected_ticker_company]
        if ticker_rows.empty:
            st.warning(f"No data found for {selected_ticker_company}")
        else:
            if 'date' in ticker_rows.columns:
                latest_row = ticker_rows.sort_values('date').iloc[-1]
            else:
                latest_row = ticker_rows.iloc[0]

            def get_display_price(price_value):
                if pd.isna(price_value): return 'N/A'
                return f"{np.log(price_value):.4f}" if show_log_price and price_value > 0 else f"${price_value:.2f}"

            label_map = {
                'Sector': 'Sector', 'Industry': 'Industry', 'Close': 'Close', 'Price': 'Price',
                'MarketCap': 'Mkt Cap', 'AvgVol30': 'Avg Vol',
                'PctFrom52WkHigh': '52W High %', 'Relative Strength': 'RS',
                'Percentile': 'Pctl', '1M_RS_Percentile': '1M RS',
                '3M_RS_Percentile': '3M RS', '6M_RS_Percentile': '6M RS',
            }
            items = []
            for k, display in label_map.items():
                if k in latest_row.index:
                    val = latest_row[k]
                    if pd.isna(val): val = 'N/A'
                    elif k == 'MarketCap':
                        try: val = f"${float(val)/1e9:.1f}B"
                        except: val = str(val)
                    elif k in ['Close', 'Price']:
                        val = get_display_price(val)
                        display = "log Price" if show_log_price else "Close"
                    elif k in ['AvgVol30', 'AvgVol50', 'AvgVol10']:
                        try: val = f"{int(val):,}"
                        except: val = str(val)
                    elif k in ['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile']:
                        try: val = f"{int(round(float(val)))}"
                        except: val = str(val)
                    else: val = str(val)
                    items.append((display, val))

            import textwrap
            # Lookup description by matching either exact or normalized ticker
            ticker_raw = selected_ticker_company
            ticker_norm = ticker_raw.replace(".", "").replace("-", "").replace("/", "").replace(" ", "").upper()
            desc = company_descriptions.get(ticker_raw) or company_descriptions.get(ticker_norm)
            
            desc_text = ""
            if desc and desc != 'nan':
                wrapped_desc = "<br>".join(textwrap.wrap(desc, width=60))
                desc_text = f"<br>Desc: {wrapped_desc}"

            snapshot_text = f"<b>{selected_ticker_company}</b><br>" + "".join(f"{d}: {v}<br>" for d, v in items).rstrip("<br>") + desc_text

            if desc and desc != 'nan':
                st.info(f"**Company Description:** {desc}")

            st.divider()
            st.subheader("📈 Price & Volume History (2 Years)")
            with st.spinner(f"Fetching historical data for {selected_ticker_company}..."):
                try:
                    df_daily_full = load_or_fetch_ticker(selected_ticker_company, interval="1d", period="2y")
                    if df_daily_full.empty:
                        st.warning(f"No daily data available for {selected_ticker_company}. Please check the ticker symbol.")
                    else:
                        df_weekly = load_or_fetch_weekly(selected_ticker_company)
                        if df_weekly.empty:
                            st.warning(f"No weekly data available for {selected_ticker_company}.")
                        else:
                            df_daily = df_daily_full.iloc[-252:] if len(df_daily_full) > 252 else df_daily_full

                            spy = yf.Ticker("^GSPC")
                            try:
                                spy_daily_full = spy.history(period="2y", interval="1d")
                                if not spy_daily_full.empty and getattr(spy_daily_full.index, 'tz', None) is not None:
                                    spy_daily_full.index = spy_daily_full.index.tz_localize(None)
                                if spy_daily_full.empty:
                                    st.warning("Unable to fetch S&P 500 data. RS calculations may be affected.")
                                    spy_daily = pd.DataFrame()
                                else:
                                    spy_daily = spy_daily_full.iloc[-252:] if len(spy_daily_full) > 252 else spy_daily_full
                            except Exception as e:
                                st.warning(f"Error fetching SPY data: {e}")
                                spy_daily = pd.DataFrame()
                            spy_weekly = spy.history(period="3y", interval="1wk")
                            if not spy_weekly.empty and getattr(spy_weekly.index, 'tz', None) is not None:
                                spy_weekly.index = spy_weekly.index.tz_localize(None)

                            common_idx = df_daily.index.intersection(spy_daily.index) if not spy_daily.empty else []
                            if len(common_idx) > 0:
                                df_daily   = df_daily.loc[common_idx]
                                spy_daily  = spy_daily.loc[common_idx]

                            daily_rec = PatternRecognizer(weekly=False, base_depth=0.50, pivot_length=9, volume_period=50)
                            for i, (_, row) in enumerate(df_daily_full.iterrows()):
                                daily_rec.process_bar(row['High'], row['Low'], row['Close'], row['Volume'], row['Open'], i)

                            weekly_rec = PatternRecognizer(weekly=True, base_depth=0.50, pivot_length=5, volume_period=50)
                            for i, (_, row) in enumerate(df_weekly.iterrows()):
                                weekly_rec.process_bar(row['High'], row['Low'], row['Close'], row['Volume'], row['Open'], i)

                            daily_rec_vis = PatternRecognizer(weekly=False, base_depth=0.50, pivot_length=9, volume_period=50)
                            for i, (_, row) in enumerate(df_daily.iterrows()):
                                daily_rec_vis.process_bar(row['High'], row['Low'], row['Close'], row['Volume'], row['Open'], i)
                            daily_painter  = PatternPainter(df_daily, daily_rec_vis, label_prices=True)
                            weekly_painter = PatternPainter(df_weekly, weekly_rec, label_prices=True)

                            if not spy_daily.empty and len(df_daily) == len(spy_daily):
                                signals  = compute_rs_signals(df_daily, spy_daily['Close'], scaling_factor=7.0)
                                df_daily = pd.concat([df_daily, signals], axis=1)
                            else:
                                for col in ['rs_raw','rs_ema_quick','rs_ema_quicksand','rs_ema_gd',
                                            'quick_break','gd_break','rs_reclaim','quicksand',
                                            'rs_new_high_any','rs_leads_price','rs_new_low']:
                                    df_daily[col] = np.nan

                            vol = df_daily['Volume'].astype(float)
                            close = df_daily['Close'].astype(float)
                            avg_vol = vol.rolling(50, min_periods=1).mean()
                            avg_vol_safe = avg_vol.replace(0, np.nan)
                            vol_ratio = (vol / avg_vol_safe * 100).fillna(0)
                            dry_lvl1 = (vol_ratio < 55).astype(bool)
                            dry_lvl2 = (vol_ratio < 40).astype(bool)
                            up_day = (close > close.shift(1)).astype(bool)
                            down_day = (close < close.shift(1)).astype(bool)
                            day_range = df_daily['High'] - df_daily['Low']
                            close_chg_pct = ((close - close.shift(1)) / close.shift(1) * 100).replace([np.inf, -np.inf], np.nan).fillna(0)
                            in_lower_27 = (close <= df_daily['Low'] + 0.27 * day_range).astype(bool)
                            stall_day = ((close_chg_pct >= 0) & (close_chg_pct < 0.1) & in_lower_27).astype(bool)
                            churn_vol = (vol >= avg_vol * 1.2).astype(bool)
                            churn_pct = (np.abs(close_chg_pct) < 0.25).astype(bool)
                            in_lower_50 = (close <= df_daily['Low'] + 0.5 * day_range).astype(bool)
                            churn_day = (churn_vol & churn_pct & in_lower_50 & (~stall_day)).astype(bool)

                            def pocket_pivot_bool(vol_series, close_series, window):
                                result = pd.Series(False, index=vol_series.index)
                                for i in range(window, len(vol_series)):
                                    up_vols = vol_series.iloc[i-window:i][close_series.iloc[i-window:i] > close_series.iloc[i-window:i].shift(1)]
                                    if len(up_vols) > 0:
                                        result.iloc[i] = (vol_series.iloc[i] > up_vols.max()) and (close_series.iloc[i] > close_series.iloc[i-1])
                                return result

                            pp10 = pocket_pivot_bool(vol, close, 10)
                            pp5 = pocket_pivot_bool(vol, close, 5)
                            pp5_only = pp5 & ~pp10
                            lower_low = (close < close.shift(1)).astype(bool)
                            rising_vol = (vol > vol.shift(1)).astype(bool)
                            ll3 = (lower_low & rising_vol).astype(bool)
                            ll3_signal = (ll3 & ll3.shift(1) & ll3.shift(2)).astype(bool)
                            marker_y = vol * 1.05

                            rs_value = latest_row.get('Percentile')
                            rs_label = f"{int(round(rs_value))}" if rs_value and not np.isnan(rs_value) else None

                            chart_type = st.radio("📊 Daily Chart Type",
                                                  ["Plotly (Advanced)", "TradingView Lightweight", "EquiCharts (Equivolume)"],
                                                  horizontal=True, key="daily_chart_type")

                            if chart_type == "EquiCharts (Equivolume)":
                                st.subheader(f"📈 {selected_ticker_company} Daily EquiCharts (Equivolume)")
                                st_html(create_equicharts_html(
                                    df_daily, height=650, ticker=selected_ticker_company, rs_label=rs_label
                                ), height=710)
                            elif chart_type == "TradingView Lightweight":
                                st.subheader(f"📈 {selected_ticker_company} Daily (TradingView Lightweight Charts)")
                                with st.expander("🎯 Add Chart Markers", expanded=False):
                                    col_m1, col_m2 = st.columns(2)
                                    with col_m1: marker_date = st.date_input("Marker Date", key="daily_marker_date")
                                    with col_m2: marker_price = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="daily_marker_price")
                                    col_m3, col_m4, col_m5 = st.columns(3)
                                    with col_m3: marker_position = st.selectbox("Position", ["aboveBar", "belowBar"], key="daily_marker_pos")
                                    with col_m4: marker_shape    = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="daily_marker_shape")
                                    with col_m5: marker_color    = st.color_picker("Color", "#FF0000", key="daily_marker_color")
                                    marker_text = st.text_input("Marker Label", "", key="daily_marker_text")
                                    add_marker = st.button("➕ Add Marker", key="daily_add_marker")
                                    session_key = f"daily_markers_{selected_ticker_company}"
                                    if session_key not in st.session_state:
                                        st.session_state[session_key] = load_markers(selected_ticker_company, "daily")
                                    if add_marker and marker_date and marker_price > 0:
                                        import datetime as dt
                                        ts = int(dt.datetime.combine(marker_date, dt.time()).timestamp())
                                        st.session_state[session_key].append({'time': ts, 'position': marker_position,
                                                                               'color': marker_color, 'shape': marker_shape,
                                                                               'text': marker_text or f"${marker_price:.2f}"})
                                        save_markers(selected_ticker_company, st.session_state[session_key], "daily")
                                        st.success(f"✅ Marker saved for {selected_ticker_company} at {marker_date}")
                                        rerun_app()
                                    current_markers = st.session_state.get(session_key, [])
                                    if current_markers:
                                        st.write(f"**Active Markers for {selected_ticker_company}:**")
                                        for i, m in enumerate(current_markers):
                                            col_a, col_b = st.columns([4, 1])
                                            with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                            with col_b:
                                                if st.button("❌", key=f"delete_daily_marker_{i}"):
                                                    st.session_state[session_key].pop(i)
                                                    save_markers(selected_ticker_company, st.session_state[session_key], "daily")
                                                    rerun_app()

                                vol_colored = []
                                for idx, row in df_daily.iterrows():
                                    ts = int(idx.timestamp())
                                    val = float(row['Volume'])
                                    col = ('rgba(247,153,2,0.7)' if dry_lvl2.loc[idx]
                                           else 'rgba(225,181,69,0.6)' if dry_lvl1.loc[idx]
                                           else '#26a69a' if up_day.loc[idx] else '#ef5350')
                                    vol_colored.append({'time': ts, 'value': val, 'color': col})
                                df_daily['volume_sma50'] = df_daily['Volume'].rolling(50).mean()
                                volume_sma50_data = [{'time': int(idx.timestamp()), 'value': float(v)}
                                                     for idx, v in df_daily['volume_sma50'].items() if pd.notna(v)]
                                pp10_dates  = [int(idx.timestamp()) for idx in df_daily.index if pp10.loc[idx]]
                                pp5_dates   = [int(idx.timestamp()) for idx in df_daily.index if pp5_only.loc[idx]]
                                churn_dates = [int(idx.timestamp()) for idx in df_daily.index if churn_day.loc[idx]]
                                stall_dates = [int(idx.timestamp()) for idx in df_daily.index if stall_day.loc[idx]]
                                ll3_dates   = [int(idx.timestamp()) for idx in df_daily.index if ll3_signal.loc[idx]]
                                lw_pattern_data = daily_painter.get_lightweight_data()
                                patt_js = build_lw_pattern_js(lw_pattern_data, chart_var="priceChart")

                                st_html(create_lightweight_candlestick_html(
                                    df_daily, height=600,
                                    markers=st.session_state.get(session_key, []),
                                    rs_label=rs_label,
                                    rs_raw=df_daily['rs_raw'],
                                    rs_quick=df_daily['rs_ema_quick'],
                                    rs_quicksand=df_daily['rs_ema_quicksand'],
                                    rs_gd=df_daily['rs_ema_gd'],
                                    volume_data=vol_colored,
                                    volume_sma50=volume_sma50_data,
                                    pp10_dates=pp10_dates, pp5_dates=pp5_dates,
                                    churn_dates=churn_dates, stall_dates=stall_dates,
                                    ll3_dates=ll3_dates,
                                    pattern_js=patt_js,
                                ), height=620)

                            def create_daily_chart_with_patterns(df, percentile, snapshot_text):
                                ema10 = df['Close'].ewm(span=10, adjust=False).mean()
                                ema21 = df['Close'].ewm(span=21, adjust=False).mean()
                                fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                                                    subplot_titles=(f"{selected_ticker_company} Daily", 'Volume with Indicators', 'Raw RS & QGDRS EMAs'),
                                                    row_heights=[0.5, 0.2, 0.3])
                                fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'],
                                                             low=df['Low'], close=df['Close'],
                                                             name='Price', showlegend=False), row=1, col=1)
                                fig.add_trace(go.Scatter(x=df.index, y=ema10, mode='lines', name='EMA(10)',
                                                         line=dict(color='#FF9800', width=2)), row=1, col=1)
                                fig.add_trace(go.Scatter(x=df.index, y=ema21, mode='lines', name='EMA(21)',
                                                         line=dict(color='#2196F3', width=2)), row=1, col=1)

                                for tr in daily_painter.get_plotly_traces():
                                    fig.add_trace(tr, row=1, col=1)
                                pattern_annotations = daily_painter.get_pending_annotations()

                                vol_colors = [
                                    'rgba(247,153,2,0.7)' if dry_lvl2.loc[idx]
                                    else 'rgba(225,181,69,0.6)' if dry_lvl1.loc[idx]
                                    else '#26a69a' if up_day.loc[idx] else '#ef5350'
                                    for idx in df.index
                                ]
                                fig.add_trace(go.Bar(x=df.index, y=df['Volume'], marker=dict(color=vol_colors),
                                                     name='Volume', showlegend=False), row=2, col=1)
                                fig.add_trace(go.Scatter(x=df.index, y=avg_vol, name='Volume SMA(50)',
                                                         line=dict(color='orange', width=1.5)), row=2, col=1)
                                if not pp10.loc[pp10].empty:
                                    fig.add_trace(go.Scatter(x=pp10[pp10].index, y=marker_y[pp10], mode='markers',
                                                             marker=dict(symbol='diamond', size=12, color='yellow'),
                                                             name='Pocket Pivot (10d)'), row=2, col=1)
                                if not pp5_only.loc[pp5_only].empty:
                                    fig.add_trace(go.Scatter(x=pp5_only[pp5_only].index, y=marker_y[pp5_only]*0.98, mode='markers',
                                                             marker=dict(symbol='diamond', size=10, color='blue'),
                                                             name='Pocket Pivot (5d)'), row=2, col=1)
                                if not churn_day.loc[churn_day].empty:
                                    fig.add_trace(go.Scatter(x=churn_day[churn_day].index, y=marker_y[churn_day], mode='markers',
                                                             marker=dict(symbol='x', size=12, color='purple'),
                                                             name='Churning Day'), row=2, col=1)
                                if not stall_day.loc[stall_day].empty:
                                    fig.add_trace(go.Scatter(x=stall_day[stall_day].index, y=marker_y[stall_day], mode='markers',
                                                             marker=dict(symbol='circle', size=10, color='maroon',
                                                                         line=dict(width=2, color='black')),
                                                             name='Stall Day'), row=2, col=1)
                                if not ll3_signal.loc[ll3_signal].empty:
                                    fig.add_trace(go.Scatter(x=ll3_signal[ll3_signal].index, y=marker_y[ll3_signal], mode='markers',
                                                             marker=dict(symbol='triangle-up', size=12, color='red'),
                                                             name='3LL Signal'), row=2, col=1)

                                fig.add_trace(go.Scatter(x=df.index, y=df['rs_raw'], name='Raw RS',
                                                         line=dict(color='blue', width=2)), row=3, col=1)
                                fig.add_trace(go.Scatter(x=df.index, y=df['rs_ema_quick'], name='Quick EMA (21)',
                                                         line=dict(color='#56b8e6', width=2)), row=3, col=1)
                                fig.add_trace(go.Scatter(x=df.index, y=df['rs_ema_quicksand'], name='Quicksand EMA (34)',
                                                         line=dict(color='#ff8c00', width=2)), row=3, col=1)
                                fig.add_trace(go.Scatter(x=df.index, y=df['rs_ema_gd'], name='GD EMA (50)',
                                                         line=dict(color='#2ca02c', width=2)), row=3, col=1)

                                for sig, sym, col, label in [
                                    ('quick_break','x','yellow','Quick Break'),
                                    ('quicksand','x','orange','Quicksand'),
                                    ('gd_break','x','red','GD Break'),
                                    ('rs_reclaim','triangle-up','lime','RS Reclaim'),
                                    ('rs_new_high_any','circle','#0000FF','RS New High'),
                                    ('rs_leads_price','circle','#00ffd9','RS Leads Price'),
                                    ('rs_new_low','circle','#FF0000','RS New Low')
                                ]:
                                    sub = df[df[sig]]
                                    if not sub.empty:
                                        y_col = 'rs_ema_gd' if sig == 'gd_break' else 'rs_ema_quick' if sig in ('quick_break','quicksand','rs_reclaim') else 'rs_raw'
                                        fig.add_trace(go.Scatter(x=sub.index, y=sub[y_col], mode='markers',
                                                                 marker=dict(symbol=sym, size=8 if 'new' in sig else 10, color=col, opacity=0.7),
                                                                 name=label, hovertemplate=f'{label}<extra></extra>'), row=3, col=1)

                                fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
                                if percentile is not None and not np.isnan(float(percentile)):
                                    fig.add_annotation(x=df.index[-1], y=float(df['Close'].iloc[-1]),
                                                       text=f"{int(round(float(percentile)))}",
                                                       showarrow=True, arrowhead=2, arrowsize=1.5, arrowcolor='#636efa',
                                                       ax=40, ay=-40, bgcolor="rgba(255,255,255,0.85)",
                                                       bordercolor="#636efa", borderwidth=1.5,
                                                       font=dict(size=13, color="#636efa"))
                                fig.add_annotation(x=0, y=0.98, xref="paper", yref="paper",
                                                   text=snapshot_text, showarrow=False, align="left",
                                                   bgcolor="rgba(255,255,255,0.85)",
                                                   font=dict(color="#1e1e1e", size=11, family="monospace"),
                                                   bordercolor="#cccccc", borderwidth=1, borderpad=8,
                                                   xanchor="left", yanchor="top")
                                fig.update_layout(annotations=fig.layout.annotations + tuple(pattern_annotations),
                                                  xaxis_rangeslider_visible=False, height=800,
                                                  margin=dict(l=20, r=20, t=40, b=20),
                                                  legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5))
                                return fig

                            if chart_type == "TradingView Lightweight":
                                st.subheader(f"📈 {selected_ticker_company} Daily (TradingView Lightweight Charts)")
                                with st.expander("🎯 Add Chart Markers", expanded=False):
                                    col_m1, col_m2 = st.columns(2)
                                    with col_m1: marker_date = st.date_input("Marker Date", key="daily_marker_date")
                                    with col_m2: marker_price = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="daily_marker_price")
                                    col_m3, col_m4, col_m5 = st.columns(3)
                                    with col_m3: marker_position = st.selectbox("Position", ["aboveBar", "belowBar"], key="daily_marker_pos")
                                    with col_m4: marker_shape    = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="daily_marker_shape")
                                    with col_m5: marker_color    = st.color_picker("Color", "#FF0000", key="daily_marker_color")
                                    marker_text = st.text_input("Marker Label", "", key="daily_marker_text")
                                    add_marker = st.button("➕ Add Marker", key="daily_add_marker")
                                    session_key = f"daily_markers_{selected_ticker_company}"
                                    if session_key not in st.session_state:
                                        st.session_state[session_key] = load_markers(selected_ticker_company, "daily")
                                    if add_marker and marker_date and marker_price > 0:
                                        import datetime as dt
                                        ts = int(dt.datetime.combine(marker_date, dt.time()).timestamp())
                                        st.session_state[session_key].append({'time': ts, 'position': marker_position,
                                                                               'color': marker_color, 'shape': marker_shape,
                                                                               'text': marker_text or f"${marker_price:.2f}"})
                                        save_markers(selected_ticker_company, st.session_state[session_key], "daily")
                                        st.success(f"✅ Marker saved for {selected_ticker_company} at {marker_date}")
                                        rerun_app()
                                    current_markers = st.session_state.get(session_key, [])
                                    if current_markers:
                                        st.write(f"**Active Markers for {selected_ticker_company}:**")
                                        for i, m in enumerate(current_markers):
                                            col_a, col_b = st.columns([4, 1])
                                            with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                            with col_b:
                                                if st.button("❌", key=f"delete_daily_marker_{i}"):
                                                    st.session_state[session_key].pop(i)
                                                    save_markers(selected_ticker_company, st.session_state[session_key], "daily")
                                                    rerun_app()

                                vol_colored = []
                                for idx, row in df_daily.iterrows():
                                    ts = int(idx.timestamp())
                                    val = float(row['Volume'])
                                    col = ('rgba(247,153,2,0.7)' if dry_lvl2.loc[idx]
                                           else 'rgba(225,181,69,0.6)' if dry_lvl1.loc[idx]
                                           else '#26a69a' if up_day.loc[idx] else '#ef5350')
                                    vol_colored.append({'time': ts, 'value': val, 'color': col})
                                df_daily['volume_sma50'] = df_daily['Volume'].rolling(50).mean()
                                volume_sma50_data = [{'time': int(idx.timestamp()), 'value': float(v)}
                                                     for idx, v in df_daily['volume_sma50'].items() if pd.notna(v)]
                                pp10_dates  = [int(idx.timestamp()) for idx in df_daily.index if pp10.loc[idx]]
                                pp5_dates   = [int(idx.timestamp()) for idx in df_daily.index if pp5_only.loc[idx]]
                                churn_dates = [int(idx.timestamp()) for idx in df_daily.index if churn_day.loc[idx]]
                                stall_dates = [int(idx.timestamp()) for idx in df_daily.index if stall_day.loc[idx]]
                                ll3_dates   = [int(idx.timestamp()) for idx in df_daily.index if ll3_signal.loc[idx]]
                                lw_pattern_data = daily_painter.get_lightweight_data()
                                patt_js = build_lw_pattern_js(lw_pattern_data, chart_var="priceChart")

                                st_html(create_lightweight_candlestick_html(
                                    df_daily, height=600,
                                    markers=st.session_state.get(session_key, []),
                                    rs_label=rs_label,
                                    rs_raw=df_daily['rs_raw'],
                                    rs_quick=df_daily['rs_ema_quick'],
                                    rs_quicksand=df_daily['rs_ema_quicksand'],
                                    rs_gd=df_daily['rs_ema_gd'],
                                    volume_data=vol_colored,
                                    volume_sma50=volume_sma50_data,
                                    pp10_dates=pp10_dates, pp5_dates=pp5_dates,
                                    churn_dates=churn_dates, stall_dates=stall_dates,
                                    ll3_dates=ll3_dates,
                                    pattern_js=patt_js,
                                ), height=620)

                            else:
                                st.subheader(f"📈 {selected_ticker_company} Daily (Plotly)")
                                with st.expander("🎯 Add Chart Markers", expanded=False):
                                    col_m1, col_m2 = st.columns(2)
                                    with col_m1: marker_date = st.date_input("Marker Date", key="plotly_daily_marker_date")
                                    with col_m2: marker_price = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="plotly_daily_marker_price")
                                    col_m3, col_m4, col_m5 = st.columns(3)
                                    with col_m3: marker_position = st.selectbox("Position", ["aboveBar", "belowBar"], key="plotly_daily_marker_pos")
                                    with col_m4: marker_shape    = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="plotly_daily_marker_shape")
                                    with col_m5: marker_color    = st.color_picker("Color", "#FF0000", key="plotly_daily_marker_color")
                                    marker_text = st.text_input("Marker Label", "", key="plotly_daily_marker_text")
                                    add_marker = st.button("➕ Add Marker", key="plotly_daily_add_marker")
                                    session_key = f"daily_markers_{selected_ticker_company}"
                                    if session_key not in st.session_state:
                                        st.session_state[session_key] = load_markers(selected_ticker_company, "daily")
                                    if add_marker and marker_date and marker_price > 0:
                                        import datetime as dt
                                        ts = int(dt.datetime.combine(marker_date, dt.time()).timestamp())
                                        st.session_state[session_key].append({'time': ts, 'position': marker_position,
                                                                               'color': marker_color, 'shape': marker_shape,
                                                                               'text': marker_text or f"${marker_price:.2f}"})
                                        save_markers(selected_ticker_company, st.session_state[session_key], "daily")
                                        rerun_app()
                                    current_markers = st.session_state.get(session_key, [])
                                    if current_markers:
                                        st.write(f"**Active Markers for {selected_ticker_company}:**")
                                        for i, m in enumerate(current_markers):
                                            col_a, col_b = st.columns([4, 1])
                                            with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                            with col_b:
                                                if st.button("❌", key=f"delete_plotly_daily_marker_{i}"):
                                                    st.session_state[session_key].pop(i)
                                                    save_markers(selected_ticker_company, st.session_state[session_key], "daily")
                                                    rerun_app()
                                st.plotly_chart(create_daily_chart_with_patterns(df_daily, latest_row.get('Percentile'), snapshot_text),
                                                use_container_width=True)

                            # Weekly chart
                            st.divider()
                            st.subheader("📊 Weekly Chart")
                            chart_type_weekly = st.radio("Weekly Chart Type",
                                                         ["Plotly (Advanced)", "TradingView Lightweight", "EquiCharts (Equivolume)"],
                                                         horizontal=True, key="weekly_chart_type")

                            if chart_type_weekly == "EquiCharts (Equivolume)":
                                st.subheader(f"📊 {selected_ticker_company} Weekly EquiCharts (Equivolume)")
                                st_html(create_equicharts_html(
                                    df_weekly, height=550, ticker=selected_ticker_company
                                ), height=600)
                            vol_w = df_weekly['Volume'].astype(float)
                            close_w = df_weekly['Close'].astype(float)
                            avg_vol_w = vol_w.rolling(10, min_periods=1).mean()
                            avg_vol_safe_w = avg_vol_w.replace(0, np.nan)
                            vol_ratio_w = (vol_w / avg_vol_safe_w * 100).fillna(0)
                            dry_lvl1_w = (vol_ratio_w < 55).astype(bool)
                            dry_lvl2_w = (vol_ratio_w < 40).astype(bool)
                            up_day_w = (close_w > close_w.shift(1)).astype(bool)
                            down_day_w = (close_w < close_w.shift(1)).astype(bool)

                            if not spy_weekly.empty and len(spy_weekly) == len(df_weekly):
                                df_weekly['rs_raw'] = (df_weekly['Close'] / df_weekly['Close'].iloc[0]) / (spy_weekly['Close'] / spy_weekly['Close'].iloc[0])
                            else:
                                df_weekly['rs_raw'] = df_weekly['Close'] / df_weekly['Close'].iloc[0]
                            df_weekly['rs_ema_quick']     = df_weekly['rs_raw'].ewm(span=8,  adjust=False).mean()
                            df_weekly['rs_ema_quicksand'] = df_weekly['rs_raw'].ewm(span=13, adjust=False).mean()
                            df_weekly['rs_ema_gd']        = df_weekly['rs_raw'].ewm(span=21, adjust=False).mean()

                            def create_weekly_chart_plotly():
                                fig_w = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                                                      subplot_titles=(f"{selected_ticker_company} Weekly", 'Volume', 'Raw RS & QGDRS EMAs'),
                                                      row_heights=[0.5, 0.2, 0.3])
                                fig_w.add_trace(go.Candlestick(x=df_weekly.index, open=df_weekly['Open'], high=df_weekly['High'],
                                                               low=df_weekly['Low'], close=df_weekly['Close'],
                                                               name='Price', showlegend=False), row=1, col=1)
                                for tr in weekly_painter.get_plotly_traces():
                                    fig_w.add_trace(tr, row=1, col=1)
                                w_annotations = weekly_painter.get_pending_annotations()
                                vol_colors_w = [
                                    'rgba(247,153,2,0.7)' if dry_lvl2_w.loc[idx]
                                    else 'rgba(225,181,69,0.6)' if dry_lvl1_w.loc[idx]
                                    else '#26a69a' if up_day_w.loc[idx] else '#ef5350'
                                    for idx in df_weekly.index
                                ]
                                fig_w.add_trace(go.Bar(x=df_weekly.index, y=df_weekly['Volume'],
                                                       marker=dict(color=vol_colors_w),
                                                       name='Volume', showlegend=False), row=2, col=1)
                                fig_w.add_trace(go.Scatter(x=df_weekly.index, y=avg_vol_w,
                                                           name='Volume SMA(10)',
                                                           line=dict(color='orange', width=1.5)), row=2, col=1)
                                fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_raw'],
                                                           name='Raw RS', line=dict(color='blue', width=2)), row=3, col=1)
                                fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_ema_quick'],
                                                           name='Quick EMA (8)', line=dict(color='#56b8e6', width=2)), row=3, col=1)
                                fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_ema_quicksand'],
                                                           name='Quicksand EMA (13)', line=dict(color='#ff8c00', width=2)), row=3, col=1)
                                fig_w.add_trace(go.Scatter(x=df_weekly.index, y=df_weekly['rs_ema_gd'],
                                                           name='GD EMA (21)', line=dict(color='#2ca02c', width=2)), row=3, col=1)
                                fig_w.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
                                if latest_row.get('Percentile') is not None and not np.isnan(float(latest_row.get('Percentile'))):
                                    fig_w.add_annotation(x=df_weekly.index[-1], y=float(df_weekly['Close'].iloc[-1]),
                                                         text=f"{int(round(float(latest_row.get('Percentile'))))}",
                                                         showarrow=True, arrowhead=2, arrowsize=1.5, arrowcolor='#636efa',
                                                         ax=40, ay=-40, bgcolor="rgba(255,255,255,0.85)",
                                                         bordercolor="#636efa", borderwidth=1.5,
                                                         font=dict(size=13, color="#636efa"))
                                fig_w.update_layout(annotations=fig_w.layout.annotations + tuple(w_annotations),
                                                    xaxis_rangeslider_visible=False, height=750,
                                                    margin=dict(l=20, r=20, t=40, b=20),
                                                    legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5))
                                return fig_w

                            if chart_type_weekly == "TradingView Lightweight":
                                st.subheader(f"📊 {selected_ticker_company} Weekly (TradingView Lightweight Charts)")
                                with st.expander("🎯 Add Weekly Chart Markers", expanded=False):
                                    col_m1, col_m2 = st.columns(2)
                                    with col_m1: marker_date_w  = st.date_input("Marker Date", key="weekly_marker_date")
                                    with col_m2: marker_price_w = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="weekly_marker_price")
                                    col_m3, col_m4, col_m5 = st.columns(3)
                                    with col_m3: marker_position_w = st.selectbox("Position", ["aboveBar", "belowBar"], key="weekly_marker_pos")
                                    with col_m4: marker_shape_w    = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="weekly_marker_shape")
                                    with col_m5: marker_color_w    = st.color_picker("Color", "#00FF00", key="weekly_marker_color")
                                    marker_text_w = st.text_input("Marker Label", "", key="weekly_marker_text")
                                    add_marker_w  = st.button("➕ Add Marker", key="weekly_add_marker")
                                    session_key_w = f"weekly_markers_{selected_ticker_company}"
                                    if session_key_w not in st.session_state:
                                        st.session_state[session_key_w] = load_markers(selected_ticker_company, "weekly")
                                    if add_marker_w and marker_date_w and marker_price_w > 0:
                                        import datetime as dt
                                        ts_w = int(dt.datetime.combine(marker_date_w, dt.time()).timestamp())
                                        st.session_state[session_key_w].append({'time': ts_w, 'position': marker_position_w,
                                                                                 'color': marker_color_w, 'shape': marker_shape_w,
                                                                                 'text': marker_text_w or f"${marker_price_w:.2f}"})
                                        save_markers(selected_ticker_company, st.session_state[session_key_w], "weekly")
                                        rerun_app()
                                    current_markers_w = st.session_state.get(session_key_w, [])
                                    if current_markers_w:
                                        st.write(f"**Active Markers for {selected_ticker_company}:**")
                                        for i, m in enumerate(current_markers_w):
                                            col_a, col_b = st.columns([4, 1])
                                            with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                            with col_b:
                                                if st.button("❌", key=f"delete_weekly_marker_{i}"):
                                                    st.session_state[session_key_w].pop(i)
                                                    save_markers(selected_ticker_company, st.session_state[session_key_w], "weekly")
                                                    rerun_app()

                                vol_colored_w = [
                                    {'time': int(idx.timestamp()), 'value': float(row['Volume']),
                                     'color': ('rgba(247,153,2,0.7)' if dry_lvl2_w.loc[idx]
                                               else 'rgba(225,181,69,0.6)' if dry_lvl1_w.loc[idx]
                                               else '#26a69a' if up_day_w.loc[idx] else '#ef5350')}
                                    for idx, row in df_weekly.iterrows()
                                ]
                                lw_w_data = weekly_painter.get_lightweight_data()
                                patt_js_w = build_lw_pattern_js(lw_w_data, chart_var="priceChart")

                                st_html(create_lightweight_candlestick_html(
                                    df_weekly, height=450,
                                    markers=st.session_state.get(session_key_w, []),
                                    rs_raw=df_weekly['rs_raw'],
                                    rs_quick=df_weekly['rs_ema_quick'],
                                    rs_quicksand=df_weekly['rs_ema_quicksand'],
                                    rs_gd=df_weekly['rs_ema_gd'],
                                    volume_data=vol_colored_w,
                                    pattern_js=patt_js_w,
                                ), height=500)
                            else:
                                st.subheader(f"📊 {selected_ticker_company} Weekly (Plotly)")
                                with st.expander("🎯 Add Weekly Chart Markers", expanded=False):
                                    col_m1, col_m2 = st.columns(2)
                                    with col_m1: marker_date_w  = st.date_input("Marker Date", key="plotly_weekly_marker_date")
                                    with col_m2: marker_price_w = st.number_input("Marker Price ($)", min_value=0.0, step=0.01, key="plotly_weekly_marker_price")
                                    col_m3, col_m4, col_m5 = st.columns(3)
                                    with col_m3: marker_position_w = st.selectbox("Position", ["aboveBar", "belowBar"], key="plotly_weekly_marker_pos")
                                    with col_m4: marker_shape_w    = st.selectbox("Shape", ["circle", "square", "arrowUp", "arrowDown"], key="plotly_weekly_marker_shape")
                                    with col_m5: marker_color_w    = st.color_picker("Color", "#00FF00", key="plotly_weekly_marker_color")
                                    marker_text_w = st.text_input("Marker Label", "", key="plotly_weekly_marker_text")
                                    add_marker_w  = st.button("➕ Add Marker", key="plotly_weekly_add_marker")
                                    session_key_w = f"weekly_markers_{selected_ticker_company}"
                                    if session_key_w not in st.session_state:
                                        st.session_state[session_key_w] = load_markers(selected_ticker_company, "weekly")
                                    if add_marker_w and marker_date_w and marker_price_w > 0:
                                        import datetime as dt
                                        ts_w = int(dt.datetime.combine(marker_date_w, dt.time()).timestamp())
                                        st.session_state[session_key_w].append({'time': ts_w, 'position': marker_position_w,
                                                                                 'color': marker_color_w, 'shape': marker_shape_w,
                                                                                 'text': marker_text_w or f"${marker_price_w:.2f}"})
                                        save_markers(selected_ticker_company, st.session_state[session_key_w], "weekly")
                                        rerun_app()
                                    current_markers_w = st.session_state.get(session_key_w, [])
                                    if current_markers_w:
                                        st.write(f"**Active Markers for {selected_ticker_company}:**")
                                        for i, m in enumerate(current_markers_w):
                                            col_a, col_b = st.columns([4, 1])
                                            with col_a: st.caption(f"🎯 {m['text']} ({m['shape']}) - {m['position']}")
                                            with col_b:
                                                if st.button("❌", key=f"delete_plotly_weekly_marker_{i}"):
                                                    st.session_state[session_key_w].pop(i)
                                                    save_markers(selected_ticker_company, st.session_state[session_key_w], "weekly")
                                                    rerun_app()
                                st.plotly_chart(create_weekly_chart_plotly(), use_container_width=True)

                except Exception as e:
                    st.error(f"Error fetching data: {e}")
                    import traceback; st.text(traceback.format_exc())

# ---------- TAB 8: Data Table ----------
with (tab8 if has_historical else tab7):
    if has_historical:
        latest_date = df['date'].max()
        table_df    = df[df['date'] == latest_date].copy()
        st.caption(f"Showing data for the latest date: **{latest_date.date()}**")
    else:
        table_df = filtered_df.copy()
    table_df = table_df.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
    all_cols = [c for c in table_df.columns if c not in ('date', 'source_file')]
    default_cols = ['Rank', 'Ticker', 'Industry', '1M', '3M', '6M', 'Close' if 'Close' in table_df.columns else 'Price', 'MarketCap', 'AvgVol30', 'PctFrom52WkHigh']
    selected_cols = st.multiselect("Select columns to display", all_cols, default=[c for c in default_cols if c in all_cols])
    sort_options  = selected_cols if selected_cols else all_cols
    default_sort_index = sort_options.index('Rank') if 'Rank' in sort_options else 0
    sort_col      = st.selectbox("Sort by", sort_options, index=default_sort_index)
    sort_ascending = st.checkbox("Ascending", value=True)
    display_df    = table_df[selected_cols].sort_values(by=sort_col, ascending=sort_ascending, na_position='last').reset_index(drop=True)
    ticker_filter = st.text_input("🔍 Search by Ticker", "").strip().upper()
    if ":" in ticker_filter:
        ticker_filter = ticker_filter.split(":")[-1].strip()
    if ticker_filter and 'Ticker' in display_df.columns:
        display_df = display_df[display_df['Ticker'].astype(str).str.upper().str.contains(ticker_filter)]
    filter_cols = st.columns(3)
    filter_conditions = []
    numeric_cols_available = [col for col in display_df.columns if display_df[col].dtype in ['float64', 'int64']]
    for idx, col in enumerate(numeric_cols_available[:9]):
        with filter_cols[idx % 3]:
            col_min = display_df[col].min()
            col_max = display_df[col].max()
            if pd.notna(col_min) and pd.notna(col_max) and col_min < col_max:
                st.write(f"**{col}**")
                c1, c2 = st.columns(2)
                if np.issubdtype(display_df[col].dtype, np.integer):
                    val_min = int(col_min)
                    val_max = int(col_max)
                    mn = c1.number_input("Min", min_value=val_min, max_value=val_max, value=val_min, step=1, key=f"filter_min_{col}")
                    mx = c2.number_input("Max", min_value=val_min, max_value=val_max, value=val_max, step=1, key=f"filter_max_{col}")
                else:
                    val_min = float(col_min)
                    val_max = float(col_max)
                    step = 0.01 if (val_max - val_min) < 10 else None
                    mn = c1.number_input("Min", min_value=val_min, max_value=val_max, value=val_min, step=step, key=f"filter_min_{col}")
                    mx = c2.number_input("Max", min_value=val_min, max_value=val_max, value=val_max, step=step, key=f"filter_max_{col}")
                filter_conditions.append((col, (mn, mx)))
    for col, (mn, mx) in filter_conditions:
        display_df = display_df[(display_df[col] >= mn) & (display_df[col] <= mx)]
    display_df = display_df.reset_index(drop=True)
    filtered_table_df = table_df.copy()
    if ticker_filter and 'Ticker' in filtered_table_df.columns:
        filtered_table_df = filtered_table_df[filtered_table_df['Ticker'].astype(str).str.upper().str.contains(ticker_filter)]
    for col, (mn, mx) in filter_conditions:
        filtered_table_df = filtered_table_df[(filtered_table_df[col] >= mn) & (filtered_table_df[col] <= mx)]
    display_df_with_rank = pd.DataFrame({'Rank': range(1, len(display_df)+1)})
    for col in display_df.columns:
        display_df_with_rank[col] = display_df[col].values
    st.dataframe(display_df_with_rank, use_container_width=True, hide_index=True)
    st.download_button("Download filtered data as CSV", display_df_with_rank.to_csv(index=False),
                       "rs_analysis_filtered.csv", "text/csv")

    st.divider()
    st.subheader("📋 Custom Watchlist Generator (TradingView & IBKR)")
    st.markdown("Paste a list of tickers below to generate a Watchlist in **TradingView** or official **IBKR TWS** format (`.txt`).")
    
    col_input1, col_input2 = st.columns([3, 1])
    with col_input1:
        pasted_input = st.text_area("Paste tickers here (separated by commas, spaces, or newlines):", value="", key="custom_pasted_watchlist_tickers")
    with col_input2:
        ibkr_exchange = st.text_input("IBKR Exchange", value="SMART/ARCA", key="custom_ibkr_exchange").strip().upper()
        if not ibkr_exchange:
            ibkr_exchange = "SMART/ARCA"
        ibkr_fmt = st.selectbox("IBKR Line Format", ["SYM (SYM, TICKER, EXCHANGE)", "DES (DES, TICKER, STK, EXCHANGE,,,,)"], index=0, key="custom_ibkr_fmt")

    if pasted_input.strip():
        # Parse the tickers using regex to handle multiple delimiters
        import re
        raw_tokens = [t.strip().upper() for t in re.split(r'[\s,;\n]+', pasted_input) if t.strip()]
        pasted_tickers = []
        for t in raw_tokens:
            if ":" in t:
                t = t.split(":")[-1].strip()
            if t:
                pasted_tickers.append(t)
        
        # De-duplicate while preserving input order
        seen = set()
        pasted_tickers = [t for t in pasted_tickers if not (t in seen or seen.add(t))]
        
        if pasted_tickers:
            # Group tickers by industry
            custom_industry_groups = {}
            for ticker in pasted_tickers:
                # Lookup in IBD mapping
                ind = ibd_industry_mapping.get(ticker)
                
                # If not in IBD mapping, lookup in the loaded stock database
                if not ind and 'Ticker' in table_df.columns and 'Industry' in table_df.columns:
                    match = table_df[table_df['Ticker'] == ticker]
                    if not match.empty:
                        ind = match.iloc[0]['Industry']
                
                if not ind or pd.isna(ind) or ind == 'nan' or ind == '':
                    ind = "Unknown Industry"
                
                if ind not in custom_industry_groups:
                    custom_industry_groups[ind] = []
                custom_industry_groups[ind].append(ticker)
            
            # Sort the groups by their industry group rank ascending
            def get_group_rank(ind_group, tickers):
                if ind_group == "Unknown Industry":
                    return 9999
                if ind_group in industry_ranks:
                    return industry_ranks[ind_group]
                # Fallback to symbol ranks in that group
                for t in tickers:
                    r = symbol_ranks.get(t)
                    if r is not None:
                        return r
                return 9999

            sorted_groups = sorted(
                custom_industry_groups.keys(),
                key=lambda g: get_group_rank(g, custom_industry_groups[g])
            )

            # Construct TradingView & IBKR Watchlist formats in sorted order
            custom_tv_lines = []
            custom_ibkr_lines = []
            use_des = "DES" in ibkr_fmt

            for ind_group in sorted_groups:
                ind_tickers = custom_industry_groups[ind_group]
                custom_tv_lines.append(f"###{ind_group}")
                custom_tv_lines.extend(ind_tickers)
                
                for t in ind_tickers:
                    sym = t.upper()
                    if use_des:
                        custom_ibkr_lines.append(f"DES, {sym}, STK, {ibkr_exchange},,,,")
                    else:
                        custom_ibkr_lines.append(f"SYM, {sym}, {ibkr_exchange}")
            
            custom_tv_watchlist_str = "\n".join(custom_tv_lines)
            custom_ibkr_watchlist_str = "\n".join(custom_ibkr_lines)
            
            st.write(f"**Custom Watchlist** ({len(pasted_tickers)} tickers in {len(custom_industry_groups)} industries)")
            
            preview_mode = st.radio("Format Preview", ["TradingView", "IBKR"], horizontal=True, key="custom_watchlist_preview_mode")
            selected_str = custom_tv_watchlist_str if preview_mode == "TradingView" else custom_ibkr_watchlist_str
            
            lines = selected_str.split("\n")
            preview_str = selected_str
            if len(lines) > 15:
                preview_str = "\n".join(lines[:15]) + "\n... (truncated, download to get the full list)"
            
            c1, c2 = st.columns([3, 1])
            with c1:
                st.code(preview_str, language="text")
            with c2:
                st.download_button(
                    "TradingView",
                    custom_tv_watchlist_str,
                    "custom_tradingview_watchlist.txt",
                    "text/plain",
                    key="download_custom_tv_watchlist"
                )
                st.download_button(
                    "IBKR",
                    custom_ibkr_watchlist_str,
                    "custom_ibkr_watchlist.txt",
                    "text/plain",
                    key="download_custom_ibkr_watchlist"
                )

    st.divider()
    st.subheader("📋 Top Tickers by Relative Strength (Grouped by Strongest Industry)")
    max_top_val = max(1, len(filtered_table_df))
    default_top_val = min(150, max_top_val)
    num_tickers = st.number_input("Number of top tickers to display", min_value=1,
                                  max_value=max_top_val, value=default_top_val, step=10, key="num_top_tickers")
    max_per_industry = st.number_input("Max tickers per industry group", min_value=1, max_value=50, value=10, step=1, key="max_per_industry")

    top_n = filtered_table_df.drop_duplicates(subset=['Ticker']).nlargest(int(num_tickers), 'Relative Strength')[['Ticker', 'Industry', 'Relative Strength']].copy()
    if df_industry is not None and not df_industry.empty:
        industry_rs_map = dict(zip(df_industry['Industry'], df_industry['Relative Strength']))
    else:
        industry_rs_map = {}
    top_n['Industry_RS'] = top_n['Industry'].map(industry_rs_map)
    industries_sorted = top_n.groupby('Industry')['Industry_RS'].first().sort_values(ascending=False).index.tolist()

    all_tickers_by_industry = []
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        if max_per_industry > 0:
            industry_tickers = industry_tickers[:max_per_industry]
        all_tickers_by_industry.extend(industry_tickers)

    all_tickers_string = ','.join(all_tickers_by_industry)
    
    # Construct TradingView & IBKR Watchlist format
    tv_lines = []
    ibkr_lines = []
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        if max_per_industry > 0:
            industry_tickers = industry_tickers[:max_per_industry]
        if industry_tickers:
            tv_lines.append(f"###{industry}")
            tv_lines.extend(industry_tickers)
            
            for t in industry_tickers:
                ibkr_lines.append(f"SYM, {t.upper()}, {ibkr_exchange}")
    tv_watchlist_string = "\n".join(tv_lines)
    ibkr_watchlist_string = "\n".join(ibkr_lines)

    st.write(f"**All Industries Combined (Sorted by Strongest Industry First)** ({len(all_tickers_by_industry)} tickers)")
    col1, col2 = st.columns([4, 1])
    with col1:
        st.code(all_tickers_string, language="text")
    with col2:
        st.download_button("Copy All", all_tickers_string,
                           f"top{int(num_tickers)}_tickers_by_industry.txt", "text/plain",
                           key="download_all_tickers")
        st.download_button("TradingView", tv_watchlist_string,
                           f"top{int(num_tickers)}_tickers_tv_watchlist.txt", "text/plain",
                           key="download_tv_watchlist")
        st.download_button("IBKR", ibkr_watchlist_string,
                           f"top{int(num_tickers)}_tickers_ibkr_watchlist.txt", "text/plain",
                           key="download_ibkr_watchlist")

    st.divider()
    st.write("**By Industry (Strongest First):**")
    for industry in industries_sorted:
        industry_tickers = top_n[top_n['Industry'] == industry]['Ticker'].tolist()
        if max_per_industry > 0:
            industry_tickers = industry_tickers[:max_per_industry]
        ticker_string = ','.join(industry_tickers)
        industry_rs = industry_rs_map.get(industry, 'N/A')
        st.write(f"**{industry}** ({len(industry_tickers)} tickers) | RS: {industry_rs}")
        col1, col2 = st.columns([4, 1])
        with col1:
            st.code(ticker_string, language="text")
        with col2:
            st.download_button(f"Copy {industry}", ticker_string,
                               f"{industry.replace(' ','_')}_tickers.txt", "text/plain",
                               key=f"download_{industry}")

# ---------- TAB 9 / 8: Pattern Finder ----------
with (tab9 if has_historical else tab8):
    st.subheader("🔍 Tweevest Pattern Finder")
    st.markdown("Extract and view the latest pattern tickers from Tweevest.")
    
    # Run script button
    if st.button("🔄 Rerun Extraction Script", key="run_tweevest_extract"):
        with st.spinner("Extracting latest pattern tickers from Tweevest..."):
            try:
                script_path = Path(__file__).resolve().parent / "python" / "fetch_vcp_tickers.py"
                result = subprocess.run([sys.executable, str(script_path)], cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True)
                if result.returncode == 0:
                    st.success("✅ Extraction completed successfully!")
                    st.cache_data.clear()
                    rerun_app()
                else:
                    st.error(f"Extraction failed:\n{result.stderr}")
            except Exception as e:
                st.error(f"Error running extraction script: {e}")
                
    # Load and display JSON results
    patterns_json_path = Path(__file__).resolve().parent / "python" / "all_patterns_tickers.json"
    if patterns_json_path.exists():
        try:
            with open(patterns_json_path, 'r', encoding='utf-8') as f:
                pattern_data = json.load(f)
            
            # Load IBD Data Tables full map for Composite Rating lookup & filtering
            ibd_full_map = load_ibd_data_tables_full()
            
            # Expander for IBD Column Filters
            with st.expander("🎛️ Filter Patterns by IBD Data Columns", expanded=False):
                f_col1, f_col2, f_col3, f_col4 = st.columns(4)
                with f_col1:
                    pf_min_comp = st.slider("Min IBD Comp Rating", 0, 99, 0, key="pf_min_comp")
                    pf_min_eps  = st.slider("Min EPS Rating", 0, 99, 0, key="pf_min_eps")
                    pf_min_rs   = st.slider("Min RS Rating", 0, 99, 0, key="pf_min_rs")
                with f_col2:
                    pf_max_ind_rank = st.slider("Max Industry Group Rank", 1, 197, 197, key="pf_max_ind_rank")
                    pf_min_vol_chg  = st.number_input("Min Vol % Change", value=-999.0, key="pf_min_vol_chg")
                    pf_min_price_chg = st.number_input("Min Price % Change", value=-999.0, key="pf_min_price_chg")
                with f_col3:
                    pf_min_lq_eps   = st.number_input("Min Last Qtr EPS % Chg", value=-999.0, key="pf_min_lq_eps")
                    pf_min_lq_sales = st.number_input("Min Last Qtr Sales % Chg", value=-999.0, key="pf_min_lq_sales")
                    pf_min_cy_eps   = st.number_input("Min Curr Yr EPS Est % Chg", value=-999.0, key="pf_min_cy_eps")
                with f_col4:
                    pf_acc_dis = st.multiselect("Acc/Dis Rating", ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"], default=[], key="pf_acc_dis")
                    pf_smr     = st.multiselect("SMR Rating", ["A", "B", "C", "D", "E"], default=[], key="pf_smr")
                    pf_ind_rs  = st.multiselect("Ind Grp RS", ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"], default=[], key="pf_ind_rs")

            def passes_ibd_filter(t_sym):
                if not isinstance(ibd_full_map, dict): return True
                t_info = ibd_full_map.get(t_sym, {})
                if not t_info:
                    if pf_min_comp > 0 or pf_min_eps > 0 or pf_min_rs > 0 or pf_max_ind_rank < 197 or pf_acc_dis or pf_smr or pf_ind_rs:
                        return False
                    return True
                
                comp = t_info.get('IBD Comp. Rating', 0)
                if pd.isna(comp): comp = 0
                if comp < pf_min_comp: return False
                
                eps = t_info.get('EPS Rating', 0)
                if pd.isna(eps): eps = 0
                if eps < pf_min_eps: return False
                
                rs = t_info.get('RS Rating', 0)
                if pd.isna(rs): rs = 0
                if rs < pf_min_rs: return False
                
                ind_rank = t_info.get('Industry Group Rank', 197)
                if pd.isna(ind_rank): ind_rank = 197
                if ind_rank > pf_max_ind_rank: return False
                
                if pf_min_vol_chg > -999.0:
                    v_chg = t_info.get('Vol. % Change', -999.0)
                    if pd.isna(v_chg) or v_chg < pf_min_vol_chg: return False
                    
                if pf_min_price_chg > -999.0:
                    p_chg = t_info.get('Price % Change', -999.0)
                    if pd.isna(p_chg) or p_chg < pf_min_price_chg: return False

                if pf_min_lq_eps > -999.0:
                    lq_e = t_info.get('Last Qtr EPS % Chg.', -999.0)
                    if pd.isna(lq_e) or lq_e < pf_min_lq_eps: return False

                if pf_min_lq_sales > -999.0:
                    lq_s = t_info.get('Last Qtr Sales % Chg.', -999.0)
                    if pd.isna(lq_s) or lq_s < pf_min_lq_sales: return False

                if pf_min_cy_eps > -999.0:
                    cy_e = t_info.get('Curr Yr EPS Est. % Chg.', -999.0)
                    if pd.isna(cy_e) or cy_e < pf_min_cy_eps: return False

                if pf_acc_dis:
                    acc = str(t_info.get('Acc/Dis Rating', '')).strip()
                    if acc not in pf_acc_dis: return False

                if pf_smr:
                    smr = str(t_info.get('SMR Rating', '')).strip()
                    if smr not in pf_smr: return False

                if pf_ind_rs:
                    ind_r = str(t_info.get('Ind Grp RS', '')).strip()
                    if ind_r not in pf_ind_rs: return False

                return True

            # Process patterns and sort tickers by Industry Group Rank (asc) then IBD Comp Rating (desc)
            processed_patterns = []
            total_headers = 0
            
            for p_name, tickers in pattern_data.items():
                if tickers:
                    filtered_tickers = [t for t in tickers if passes_ibd_filter(t)]
                    if filtered_tickers:
                        section_title = p_name.replace('-', ' ').title()
                        scored_tickers = []
                        for t in filtered_tickers:
                            t_info = ibd_full_map.get(t, {}) if isinstance(ibd_full_map, dict) else {}
                            comp_val = t_info.get('IBD Comp. Rating', 0)
                            if pd.isna(comp_val):
                                comp_val = 0
                            ind_rank = t_info.get('Industry Group Rank', 999)
                            if pd.isna(ind_rank):
                                ind_rank = 999
                            scored_tickers.append((t, float(ind_rank), float(comp_val)))
                        
                        # Sort tickers within pattern: Industry Group Rank (asc), then Comp Rating (desc)
                        scored_tickers.sort(key=lambda x: (x[1], -x[2]))
                        processed_patterns.append((section_title, scored_tickers))
                        total_headers += 1

            # TradingView allows maximum 1000 lines (section headers like ###Bull Flag count as 1 line)
            MAX_TV_LINES = 1000
            max_allowed_tickers = max(0, MAX_TV_LINES - total_headers)
            
            # Collect all candidate (ind_rank, comp_rating, section_title, ticker) to pick top max_allowed_tickers overall
            all_candidates = []
            for section_title, scored_tickers in processed_patterns:
                for t, ind_rank, comp_val in scored_tickers:
                    all_candidates.append((ind_rank, comp_val, section_title, t))
            
            # Sort globally by Industry Group Rank (asc) then Comp Rating (desc)
            all_candidates.sort(key=lambda x: (x[0], -x[1]))
            selected_candidates = set((sec, t) for ind_rank, comp_val, sec, t in all_candidates[:max_allowed_tickers])
            
            # Build final tv_lines, ibkr_lines, and all_tickers_list
            tv_lines = []
            ibkr_lines = []
            all_tickers_list = []
            
            for section_title, scored_tickers in processed_patterns:
                # Include selected tickers maintaining Industry Rank & Comp Rating order
                sec_selected_tickers = [t for t, ind_rank, comp in scored_tickers if (section_title, t) in selected_candidates]
                if sec_selected_tickers:
                    tv_lines.append(f"###{section_title}")
                    tv_lines.extend(sec_selected_tickers)
                    for t in sec_selected_tickers:
                        ibkr_lines.append(f"SYM, {t.upper()}, SMART")
                        if t not in all_tickers_list:
                            all_tickers_list.append(t)
            
            if tv_lines:
                tv_watchlist_string = "\n".join(tv_lines)
                ibkr_watchlist_string = "\n".join(ibkr_lines)
                all_tickers_string = ",".join(all_tickers_list)
                
                preview_tv_lines = tv_lines[:10]
                tv_preview_string = "\n".join(preview_tv_lines)
                if len(tv_lines) > 10:
                    tv_preview_string += f"\n... ({len(tv_lines) - 10} more lines - use buttons to copy/download full list)"
                
                today_str = datetime.today().strftime('%m%d%y')
                tv_filename = f"DrW Pattern_{today_str}.txt"
                ibkr_filename = f"DrW Pattern_IBKR_{today_str}.txt"
                all_filename = f"DrW Pattern_All_{today_str}.txt"
                
                st.write(f"**All Patterns Combined (TradingView & IBKR Watchlist - Capped at 1,000 Lines by Industry Group Rank & Comp Rating)** ({len(all_tickers_list)} unique tickers | {len(tv_lines)} TV lines)")
                col1, col2 = st.columns([4, 1])
                with col1:
                    st.code(tv_preview_string, language="text")
                with col2:
                    st.download_button("TradingView", tv_watchlist_string,
                                       tv_filename, "text/plain",
                                       key="download_tv_pattern_watchlist")
                    st.download_button("IBKR", ibkr_watchlist_string,
                                       ibkr_filename, "text/plain",
                                       key="download_ibkr_pattern_watchlist")
                    st.download_button("Copy All Tickers", all_tickers_string,
                                       all_filename, "text/plain",
                                       key="download_all_pattern_tickers")

                st.divider()

            # Displays the individual patterns sorted by Industry Group Rank & IBD Comp. Rating
            for section_title, scored_tickers in processed_patterns:
                tickers_sorted = [t for t, ind_rank, comp in scored_tickers]
                col_title, col_count = st.columns([6, 1])
                with col_title:
                    st.markdown(f"### {section_title}")
                with col_count:
                    st.markdown(f"**Count: {len(tickers_sorted)}**")
                
                if tickers_sorted:
                    ticker_str = ",".join(tickers_sorted)
                    st.code(ticker_str, language="text")
                else:
                    st.info("No tickers found for this pattern.")


        except Exception as e:
            st.error(f"Error loading pattern data: {e}")
    else:
        st.warning("No patterns data found. Click the button above to run the extraction script for the first time.")

# ---------- TAB: IBD Pattern Scanner ----------
with tab_ibd_pattern:
    st.subheader("🏆 IBD Pattern Scanner")
    st.markdown("Automated MarketSmith / IBD pattern scanner logic (`drw_pattern_scanner.pine`). Categorizes active base patterns (Cup+Handle, Cup, Double Bottom, High Tight Flag, Flat Base, 6-Wk Flat, Base) from `ticker_cache` data.")
    
    col_scan1, col_scan2 = st.columns([4, 6])
    with col_scan1:
        if st.button("🔄 Run / Rerun IBD Pattern Scanner", key="run_ibd_pattern_scanner_btn"):
            with st.spinner("Scanning 7,000+ ticker cache files for IBD patterns..."):
                try:
                    script_path = Path(__file__).resolve().parent / "python" / "ibd_pattern_scanner.py"
                    result = subprocess.run([sys.executable, str(script_path)], cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True)
                    if result.returncode == 0:
                        st.success("✅ Pattern scan completed successfully!")
                        st.cache_data.clear()
                        rerun_app()
                    else:
                        st.error(f"Scan failed:\n{result.stderr}")
                except Exception as e:
                    st.error(f"Error running pattern scanner: {e}")
    with col_scan2:
        search_ticker = st.text_input("🔍 Ticker Search", value="", placeholder="Type ticker (e.g. CMT, NVDA, AAPL)...", key="ibd_quick_ticker_search").strip().upper()
                    
    ibd_json_path = Path(__file__).resolve().parent / "python" / "ibd_pattern_results.json"
    if ibd_json_path.exists():
        try:
            with open(ibd_json_path, 'r', encoding='utf-8') as f:
                ibd_results = json.load(f)
                
            df_ibd_patterns = pd.DataFrame(ibd_results)
            
            if df_ibd_patterns.empty:
                st.info("No active pattern signals found in current ticker cache.")
            else:
                # Merge stock dataset metrics (RS Rating / Percentile, 30D Avg Volume, Sector, Industry)
                if 'df' in locals() and df is not None and not df.empty:
                    if 'date' in df.columns:
                        latest_stocks = df.sort_values('date').groupby('Ticker').last().reset_index()
                    else:
                        latest_stocks = df.drop_duplicates(subset=['Ticker']).copy()
                        
                    merge_fields = ['Ticker', 'Percentile', 'Relative Strength', 'AvgVol30', 'Sector', 'Industry']
                    avail_m = [c for c in merge_fields if c in latest_stocks.columns]
                    
                    if len(avail_m) > 1:
                        df_ibd_patterns = df_ibd_patterns.merge(
                            latest_stocks[avail_m],
                            left_on='ticker',
                            right_on='Ticker',
                            how='left'
                        )
                
                # Format numeric merged columns
                if 'Percentile' in df_ibd_patterns.columns:
                    df_ibd_patterns['Percentile'] = pd.to_numeric(df_ibd_patterns['Percentile'], errors='coerce')
                if 'AvgVol30' in df_ibd_patterns.columns:
                    df_ibd_patterns['AvgVol30'] = pd.to_numeric(df_ibd_patterns['AvgVol30'], errors='coerce')

                # Top Overview Metrics
                m_col1, m_col2, m_col3, m_col4, m_col5 = st.columns(5)
                with m_col1: st.metric("Total Pattern Tickers", len(df_ibd_patterns))
                with m_col2: st.metric("In Base", len(df_ibd_patterns[df_ibd_patterns['status'] == 'In Base']))
                with m_col3: st.metric("Post-BO", len(df_ibd_patterns[df_ibd_patterns['status'] == 'Post-BO']))
                with m_col4: st.metric("Top Score (>=5)", len(df_ibd_patterns[df_ibd_patterns['composite_score'] >= 5]))
                with m_col5: st.metric("Avg % Off 52W High", f"{df_ibd_patterns['pct_off_52w_high'].mean():.1f}%")
                
                st.divider()
                
                # Interactive Filters Expander
                with st.expander("🎛️ Filter & Refine IBD Pattern Tickers", expanded=False):
                    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
                    with f_col1:
                        avail_patterns = sorted(df_ibd_patterns['pattern_name'].unique().tolist())
                        sel_patterns = st.multiselect("Pattern Name", avail_patterns, default=avail_patterns, key="ibd_p_sel")
                        sel_status = st.radio("Base Status", ["All", "In Base", "Post-BO"], horizontal=True, key="ibd_status_sel")
                    with f_col2:
                        min_rs_rating = st.slider("Min RS Rating (1-99)", 0, 99, 0, key="ibd_min_rs")
                        min_price     = st.number_input("Min Price ($)", value=0.0, step=1.0, key="ibd_min_price")
                        max_price     = st.number_input("Max Price ($)", value=10000.0, step=10.0, key="ibd_max_price")
                        min_avg_vol30 = st.number_input("Min 30D Avg Vol", value=0, step=50000, key="ibd_min_vol30")
                    with f_col3:
                        min_comp_score = st.slider("Min Composite Score (0-12)", 0, 12, 0, key="ibd_min_comp")
                        min_pre_score  = st.slider("Min Before-BO Score (0-6)", 0, 6, 0, key="ibd_min_pre")
                        min_post_score = st.slider("Min Post-BO Score (0-6)", 0, 6, 0, key="ibd_min_post")
                    with f_col4:
                        max_dist_pivot = st.number_input("Max Distance to Pivot %", value=100.0, key="ibd_max_dist")
                        max_off_52w    = st.number_input("Max % Off 52W High", value=100.0, key="ibd_max_52w")
                        sub_filter     = st.multiselect("Require Sub-signals", ["Volume Dry-Up", "Pocket Pivot", "Touched MA", "Shakeout Entry", "Upside Reversal", "RS New High"], default=[], key="ibd_sub_sig")

                # Apply Filters
                filtered_ibd = df_ibd_patterns.copy()
                if search_ticker:
                    filtered_ibd = filtered_ibd[filtered_ibd['ticker'].str.upper().str.contains(search_ticker, na=False)]
                if sel_patterns:
                    filtered_ibd = filtered_ibd[filtered_ibd['pattern_name'].isin(sel_patterns)]
                if sel_status != "All":
                    filtered_ibd = filtered_ibd[filtered_ibd['status'] == sel_status]
                    
                if 'Percentile' in filtered_ibd.columns and min_rs_rating > 0:
                    filtered_ibd = filtered_ibd[filtered_ibd['Percentile'].fillna(0) >= min_rs_rating]
                if min_price > 0:
                    filtered_ibd = filtered_ibd[filtered_ibd['close'] >= min_price]
                if max_price < 10000.0:
                    filtered_ibd = filtered_ibd[filtered_ibd['close'] <= max_price]
                if 'AvgVol30' in filtered_ibd.columns and min_avg_vol30 > 0:
                    filtered_ibd = filtered_ibd[filtered_ibd['AvgVol30'].fillna(0) >= min_avg_vol30]
                    
                filtered_ibd = filtered_ibd[
                    (filtered_ibd['composite_score'] >= min_comp_score) &
                    (filtered_ibd['before_bo_score'] >= min_pre_score) &
                    (filtered_ibd['post_bo_score'] >= min_post_score) &
                    (filtered_ibd['pct_off_52w_high'] <= max_off_52w)
                ]
                if max_dist_pivot < 100.0:
                    filtered_ibd = filtered_ibd[filtered_ibd['dist_pct'].fillna(999.0) <= max_dist_pivot]
                    
                if "Volume Dry-Up" in sub_filter: filtered_ibd = filtered_ibd[filtered_ibd['vol_dry_up']]
                if "Pocket Pivot" in sub_filter: filtered_ibd = filtered_ibd[filtered_ibd['pocket_pivot']]
                if "Touched MA" in sub_filter: filtered_ibd = filtered_ibd[filtered_ibd['touched_ma']]
                if "Shakeout Entry" in sub_filter: filtered_ibd = filtered_ibd[filtered_ibd['shakeout_entry']]
                if "Upside Reversal" in sub_filter: filtered_ibd = filtered_ibd[filtered_ibd['upside_reversal']]
                if "RS New High" in sub_filter: filtered_ibd = filtered_ibd[filtered_ibd['rs_nh']]

                # Output Views: Tabs for "Category View", "Data Table", and "Watchlist Export"
                sub_tab1, sub_tab2, sub_tab3 = st.tabs(["📂 Tickers by Pattern Category", "📋 Detailed Data Table", "📤 Export Watchlists"])
                
                # --- Sub-tab 1: Categorized View ---
                with sub_tab1:
                    pattern_order = ["Cup+Handle", "Cup", "Dbl Bottom", "HTF", "6-Wk Flat", "Flat Base", "Base"]
                    for pat in pattern_order:
                        pat_df = filtered_ibd[filtered_ibd['pattern_name'] == pat]
                        count_p = len(pat_df)
                        
                        with st.expander(f"📌 {pat} ({count_p} tickers)", expanded=(count_p > 0 and count_p < 50)):
                            if count_p > 0:
                                t_list = pat_df['ticker'].tolist()
                                t_str = ",".join(t_list)
                                col_t1, col_t2 = st.columns([8, 2])
                                with col_t1:
                                    st.code(t_str, language="text")
                                with col_t2:
                                    st.download_button(f"Download {pat}", t_str, f"{pat.replace('+', 'Plus').replace(' ', '_')}_tickers.txt", "text/plain", key=f"dl_{pat}")
                                
                                # Show summary table inside expander
                                mini_cols = ['ticker', 'status', 'Percentile', 'close', 'AvgVol30', 'dist_pct', 'pct_off_52w_high', 'composite_score', 'before_bo_score', 'post_bo_score', 'rs_nh_count']
                                mini_avail = [c for c in mini_cols if c in pat_df.columns]
                                display_mini = pat_df[mini_avail].copy()
                                display_mini = display_mini.rename(columns={'Percentile': 'RS Rating', 'close': 'Price ($)', 'AvgVol30': '30D Avg Vol'})
                                st.dataframe(display_mini, hide_index=True, use_container_width=True)
                            else:
                                st.info(f"No tickers found matching filters for {pat}.")

                # --- Sub-tab 2: Full Data Table ---
                with sub_tab2:
                    st.markdown(f"Showing **{len(filtered_ibd)}** pattern signals matching filters.")
                    
                    # Columns ordering
                    table_cols = ['ticker', 'pattern_name', 'status', 'Percentile', 'close', 'AvgVol30', 'dist_pct', 'pct_off_52w_high', 'composite_score', 'before_bo_score', 'post_bo_score', 'rs_nh_count', 'days_in_base', 'bars_sbo', 'Sector', 'Industry', 'vol_dry_up', 'pocket_pivot', 'touched_ma', 'shakeout_entry', 'upside_reversal', 'rs_nh']
                    table_cols = [c for c in table_cols if c in filtered_ibd.columns]
                    
                    renamed_cols = {
                        'ticker': 'Ticker',
                        'pattern_name': 'Pattern',
                        'status': 'Status',
                        'Percentile': 'RS Rating',
                        'close': 'Close ($)',
                        'AvgVol30': '30D Avg Vol',
                        'dist_pct': 'Pivot Dist %',
                        'pct_off_52w_high': '% Off 52W High',
                        'composite_score': 'Comp Score',
                        'before_bo_score': 'Pre Score',
                        'post_bo_score': 'Post Score',
                        'rs_nh_count': 'RS NH Count',
                        'days_in_base': 'Days in Base',
                        'bars_sbo': 'Bars Post-BO',
                        'vol_dry_up': 'VDU',
                        'pocket_pivot': 'PP',
                        'touched_ma': 'MA Touch',
                        'shakeout_entry': 'Shakeout',
                        'upside_reversal': 'UpRev',
                        'rs_nh': 'RS NH'
                    }
                    disp_df = filtered_ibd[table_cols].rename(columns=renamed_cols)
                    st.dataframe(disp_df, hide_index=True, use_container_width=True)

                # --- Sub-tab 3: Export Watchlists ---
                with sub_tab3:
                    st.subheader("📤 Export Categorized Watchlists")
                    
                    all_filtered_tickers = filtered_ibd['ticker'].unique().tolist()
                    st.write(f"**Total Filtered Tickers:** {len(all_filtered_tickers)}")
                    
                    # TradingView format with section headers
                    tv_lines = []
                    ibkr_lines = []
                    for pat in pattern_order:
                        p_df = filtered_ibd[filtered_ibd['pattern_name'] == pat]
                        if not p_df.empty:
                            tv_lines.append(f"###{pat}")
                            for t in p_df['ticker']:
                                tv_lines.append(t)
                                ibkr_lines.append(f"SYM, {t.upper()}, SMART/ARCA")
                                
                    tv_export_str = "\n".join(tv_lines)
                    ibkr_export_str = "\n".join(ibkr_lines)
                    csv_export_str = ",".join(all_filtered_tickers)
                    
                    col_ex1, col_ex2, col_ex3 = st.columns(3)
                    with col_ex1:
                        st.markdown("##### 📄 Plain CSV / List")
                        st.code(csv_export_str, language="text")
                        st.download_button("Download CSV List", csv_export_str, "ibd_patterns_list.txt", "text/plain", key="dl_ibd_csv")
                    with col_ex2:
                        st.markdown("##### 📈 TradingView Watchlist")
                        st.code(tv_export_str, language="text")
                        st.download_button("Download TradingView Watchlist", tv_export_str, "ibd_patterns_tv.txt", "text/plain", key="dl_ibd_tv")
                    with col_ex3:
                        st.markdown("##### 💼 IBKR Watchlist")
                        st.code(ibkr_export_str, language="text")
                        st.download_button("Download IBKR Watchlist", ibkr_export_str, "ibd_patterns_ibkr.txt", "text/plain", key="dl_ibd_ibkr")

        except Exception as e:
            st.error(f"Error loading IBD pattern results: {e}")
    else:
        st.info("No IBD pattern results JSON found. Click the button above to run the pattern scanner.")


# ---------- TAB: Daily Report Card ----------
with tab_drc:
    st.subheader("📝 Daily Report Card (DRC) & GMI Journal")
    
    # Top Control Bar: Date selection & Tag Explorer
    existing_day_tags, tag_map, existing_trade_tags, trade_tag_map = get_all_drc_tags_and_entries()

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([2, 2, 2])
    with ctrl_col1:
        drc_date = st.date_input("📅 Select Date", value=datetime.today().date(), key="drc_date_picker")
    
    with ctrl_col2:
        selected_tag_filter = st.selectbox("🏷️ Filter Reports by Day Tag", ["All Tags"] + existing_day_tags, key="drc_tag_filter_select")
        
    with ctrl_col3:
        if selected_tag_filter != "All Tags" and selected_tag_filter in tag_map:
            matching_items = tag_map[selected_tag_filter]
            matching_dates = [item["date"] for item in matching_items if item.get("date")]
            if matching_dates:
                jump_date_str = st.selectbox("📅 Jump to Tagged Date", matching_dates, key="drc_jump_date_select")
                if jump_date_str:
                    try:
                        parsed_d = datetime.strptime(jump_date_str, "%Y-%m-%d").date()
                        if parsed_d != drc_date:
                            st.session_state["drc_date_picker"] = parsed_d
                            rerun_app()
                    except Exception:
                        pass
        else:
            st.caption("Select a tag to filter past DRC entries.")

    with st.expander("🔍 Search & Filter Trade Execution Logs (by Trade Tag or Ticker)", expanded=False):
        t_col1, t_col2 = st.columns([1, 1])
        with t_col1:
            sel_tr_tag = st.selectbox("🎯 Select Trade Tag", ["All Trade Tags"] + existing_trade_tags, key="drc_tr_tag_search")
        with t_col2:
            sel_tr_ticker = st.text_input("🔍 Search Ticker Name", value="", key="drc_tr_ticker_search").strip().upper()
            
        matched_trades = []
        if sel_tr_tag != "All Trade Tags" and sel_tr_tag in trade_tag_map:
            matched_trades = trade_tag_map[sel_tr_tag]
            if sel_tr_ticker:
                matched_trades = [item for item in matched_trades if item.get("ticker") == sel_tr_ticker]
        elif sel_tr_ticker:
            for tt_list in trade_tag_map.values():
                for item in tt_list:
                    if item.get("ticker") == sel_tr_ticker and item not in matched_trades:
                        matched_trades.append(item)
                        
        if matched_trades:
            st.markdown(f"**Found {len(matched_trades)} trade log(s):**")
            for mt in matched_trades:
                m_date = mt.get("date", "")
                m_tk = mt.get("ticker", "")
                m_pnl = mt.get("pnl", "")
                m_tags = ", ".join(mt.get("tags", []))
                m_notes = mt.get("notes", "")
                c_m1, c_m2 = st.columns([4, 1])
                with c_m1:
                    st.markdown(f"- **{m_date}** | Ticker: **{m_tk}** | PnL: `{m_pnl}` | Tags: `{m_tags}` — *{m_notes[:80]}...*")
                with c_m2:
                    if st.button(f"Jump to {m_date}", key=f"jump_{m_date}_{m_tk}_{uuid.uuid4().hex[:4]}"):
                        try:
                            parsed_d = datetime.strptime(m_date, "%Y-%m-%d").date()
                            st.session_state["drc_date_picker"] = parsed_d
                            rerun_app()
                        except Exception:
                            pass
        elif sel_tr_tag != "All Trade Tags" or sel_tr_ticker:
            st.info("No trade execution logs match your search criteria.")

    st.divider()

    # Load DRC data for selected date
    drc_data = load_daily_notes(drc_date)
    drc_date_str = drc_date.strftime("%Y-%m-%d")

    # ------------------ TOP SECTION: Daily GMI PDF Report ------------------
    st.subheader("📄 Daily GMI Report (PDF)")
    effective_pdf_date, is_fallback, fallback_msg = get_effective_pdf_date(drc_date)
    if is_fallback:
        st.info(f"ℹ️ {fallback_msg}")

    pdf_path, pdf_url, is_pdf_ok, pdf_msg = fetch_daily_gmi_pdf(effective_pdf_date)
    
    if is_pdf_ok and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        import base64
        b64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
        
        st.download_button(
            "⬇️ Download PDF",
            pdf_bytes,
            file_name=Path(pdf_path).name,
            mime="application/pdf",
            key=f"dl_pdf_{drc_date_str}"
        )
            
        pdf_js_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
            <style>
                html, body {{
                    margin: 0;
                    padding: 0;
                    background-color: #1e1e1e;
                    color: #ffffff;
                    font-family: system-ui, -apple-system, sans-serif;
                    height: 100%;
                    overflow: hidden;
                }}
                #toolbar {{
                    padding: 8px 16px;
                    background: #2a2a2a;
                    border-bottom: 1px solid #3a3a3a;
                    display: flex;
                    gap: 12px;
                    align-items: center;
                    justify-content: center;
                }}
                .zoom-btn {{
                    background: #3a3a3a;
                    color: #fff;
                    border: 1px solid #555;
                    padding: 5px 14px;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 13px;
                    font-weight: 600;
                    transition: background 0.2s;
                }}
                .zoom-btn:hover {{
                    background: #4a4a4a;
                }}
                #pdf-scroll-container {{
                    width: 100%;
                    height: 1250px;
                    overflow-y: auto;
                    overflow-x: auto;
                    box-sizing: border-box;
                    padding: 12px;
                    text-align: center;
                }}
                .pdf-page-canvas {{
                    display: block;
                    margin: 14px auto;
                    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.7);
                    border-radius: 4px;
                    max-width: 100%;
                    height: auto;
                }}
                #loading-info {{
                    padding: 30px;
                    color: #aaa;
                    font-size: 15px;
                }}
            </style>
        </head>
        <body>
            <div id="toolbar">
                <button class="zoom-btn" onclick="zoomIn()">🔍+ Zoom In</button>
                <button class="zoom-btn" onclick="zoomOut()">🔍- Zoom Out</button>
                <button class="zoom-btn" onclick="resetZoom()">🔄 Reset Zoom</button>
                <span id="zoom-val" style="font-size:13px; color:#4CAF50; font-weight:bold;">200%</span>
            </div>
            <div id="pdf-scroll-container">
                <div id="loading-info">⏳ Rendering PDF pages...</div>
            </div>
            <script>
                pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
                const base64Data = "{b64_pdf}";
                const raw = atob(base64Data);
                const uint8Array = new Uint8Array(raw.length);
                for (let i = 0; i < raw.length; i++) {{
                    uint8Array[i] = raw.charCodeAt(i);
                }}

                let pdfDoc = null;
                let currentScale = 2.0;

                function renderAllPages() {{
                    const container = document.getElementById('pdf-scroll-container');
                    container.innerHTML = '';
                    document.getElementById('zoom-val').textContent = Math.round(currentScale * 100) + '%';

                    for (let pageNum = 1; pageNum <= pdfDoc.numPages; pageNum++) {{
                        (function(num) {{
                            pdfDoc.getPage(num).then(function(page) {{
                                const viewport = page.getViewport({{ scale: currentScale }});
                                const canvas = document.createElement('canvas');
                                canvas.className = 'pdf-page-canvas';
                                const context = canvas.getContext('2d');
                                canvas.height = viewport.height;
                                canvas.width = viewport.width;
                                container.appendChild(canvas);

                                page.render({{
                                    canvasContext: context,
                                    viewport: viewport
                                }});
                            }});
                        }})(pageNum);
                    }}
                }}

                function zoomIn() {{
                    currentScale += 0.25;
                    renderAllPages();
                }}

                function zoomOut() {{
                    if (currentScale > 0.8) {{
                        currentScale -= 0.25;
                        renderAllPages();
                    }}
                }}

                function resetZoom() {{
                    currentScale = 2.0;
                    renderAllPages();
                }}

                pdfjsLib.getDocument({{ data: uint8Array }}).promise.then(function(pdf) {{
                    document.getElementById('loading-info').style.display = 'none';
                    pdfDoc = pdf;
                    renderAllPages();
                }}).catch(function(err) {{
                    document.getElementById('pdf-scroll-container').innerHTML = '<span style="color:#ef5350; padding:20px;">Failed to render PDF: ' + err.message + '</span>';
                }});
            </script>
        </body>
        </html>
        """
        st_html(pdf_js_html, height=1300)
    else:
        st.warning(f"⚠️ {pdf_msg}")
        st.info("You can manually upload a PDF for this date below if available:")
        uploaded_pdf = st.file_uploader("Upload Daily PDF", type=["pdf"], key=f"upload_pdf_{drc_date_str}")
        if uploaded_pdf is not None:
            ensure_daily_dirs()
            with open(pdf_path, "wb") as f:
                f.write(uploaded_pdf.getbuffer())
            st.success("✅ PDF uploaded and saved into daily folder!")
            rerun_app()

    st.divider()

    # ------------------ BOTTOM SECTION: Daily Journal & Notes ------------------
    st.subheader("📋 Daily Journal & Notes")
        
    h_c1, h_c2, h_c3, h_c4 = st.columns([1, 1, 1, 1])
    with h_c1:
        grade_options = ["-", "A+", "A", "-A", "B+", "B", "-B", "C+", "C", "D", "F"]
        saved_grade = drc_data.get("grade", "-")
        idx_g = grade_options.index(saved_grade) if saved_grade in grade_options else 0
        curr_grade = st.selectbox("Overall Grade", grade_options, index=idx_g, key=f"drc_grade_{drc_date_str}")
    with h_c2:
        curr_pnl = st.text_input("Daily PnL / Summary", value=drc_data.get("pnl", ""), key=f"drc_pnl_{drc_date_str}")
    with h_c3:
        st.markdown("<br>", unsafe_allow_html=True)
        save_btn = st.button("💾 Save", key=f"save_drc_{drc_date_str}", type="primary", use_container_width=True)
    with h_c4:
        st.markdown("<br>", unsafe_allow_html=True)
        load_tpl_btn = st.button("📋 Template", key=f"load_tpl_{drc_date_str}", help="Fill form with sample template from 3/17/2020", use_container_width=True)

    if load_tpl_btn:
        sample_data = get_template_sample_notes(drc_date)
        save_daily_notes(drc_date, sample_data)
        st.success("✅ Loaded sample template notes!")
        rerun_app()

    header_parts = [drc_date.strftime('%m/%d/%Y')]
    if curr_grade and curr_grade != "-":
        header_parts.append(f"Grade: {curr_grade}")
    if curr_pnl and curr_pnl.strip():
        header_parts.append(curr_pnl.strip())

    st.markdown(f"### `{' | '.join(header_parts)}`")

    # Goal & Checklist
    st.markdown("**🎯 GOAL:**")
    curr_goal = st.text_area(
        "Goal Statement",
        value=drc_data.get("goal", ""),
        height=65,
        key=f"drc_goal_{drc_date_str}",
        label_visibility="collapsed"
    )

    chk = drc_data.get("checklist", {})
    c_c1, c_c2 = st.columns(2)
    with c_c1:
        chk_3trades = st.checkbox("-3 Trades per Segment, stay selective.", value=chk.get("3_trades", False), key=f"chk_3trades_{drc_date_str}")
        chk_nophone = st.checkbox("-No Risk Monitor / phone.", value=chk.get("no_phone", False), key=f"chk_nophone_{drc_date_str}")
    with c_c2:
        chk_filldrc = st.checkbox("-Each break must consciously fill out DRC.", value=chk.get("fill_drc", False), key=f"chk_filldrc_{drc_date_str}")
        chk_afterhours = st.checkbox("-Afterhours schedule", value=chk.get("afterhours", False), key=f"chk_afterhours_{drc_date_str}")

    st.divider()

    # Segment Table
    st.markdown("**📊 Segment Performance Table:**")
    seg_data = drc_data.get("segments", [])
    seg_df = pd.DataFrame(seg_data)
    edited_seg_df = st.data_editor(
        seg_df,
        num_rows="fixed",
        use_container_width=True,
        key=f"drc_seg_editor_{drc_date_str}",
        column_config={
            "Segment": st.column_config.TextColumn("Segment", disabled=True, width="medium"),
            "Grade": st.column_config.TextColumn("Grade", width="small"),
            "PTD Only": st.column_config.TextColumn("PTD Only", width="small"),
            "Sizing": st.column_config.TextColumn("Sizing", width="small"),
            "In My Favor": st.column_config.TextColumn("In My Favor", width="small"),
            "Comments": st.column_config.TextColumn("Comments", width="large")
        }
    )

    st.divider()

    # Daily Reflections
    st.markdown("**💡 WHAT I LEARNED / IMPROVED UPON TODAY:**")
    curr_learned = st.text_area(
        "What I learned",
        value=drc_data.get("learned", ""),
        height=70,
        key=f"drc_learned_{drc_date_str}",
        label_visibility="collapsed"
    )

    st.markdown("**🔄 CHANGES I NEED TO MAKE FROM TODAY:**")
    curr_changes = st.text_area(
        "Changes to make",
        value=drc_data.get("changes", ""),
        height=90,
        key=f"drc_changes_{drc_date_str}",
        label_visibility="collapsed"
    )

    st.markdown("**📝 OVERVIEW:**")
    curr_overview = st.text_area(
        "Overview",
        value=drc_data.get("overview", ""),
        height=110,
        key=f"drc_overview_{drc_date_str}",
        label_visibility="collapsed"
    )

    st.markdown("**⚡ EASIEST $50K:**")
    curr_easiest = st.text_input(
        "Easiest $50k",
        value=drc_data.get("easiest_50k", ""),
        key=f"drc_easiest_{drc_date_str}",
        label_visibility="collapsed"
    )

    st.divider()

    # Tag Manager
    st.markdown("**🏷️ TAGS FOR THIS DAY:**")
    curr_tags = drc_data.get("tags", [])
    all_avail_tags = sorted(list(set(curr_tags + existing_day_tags + ["A+ Day", "Trend Day", "Breakout", "Choppy", "Reversal", "FOMC", "Loss Day"])))
    selected_tags = st.multiselect("Select Tags", all_avail_tags, default=curr_tags, key=f"drc_tags_{drc_date_str}")
    new_custom_tag = st.text_input("➕ Add custom tag", key=f"drc_new_tag_{drc_date_str}").strip()
    if new_custom_tag and new_custom_tag not in selected_tags:
        selected_tags.append(new_custom_tag)

    st.divider()

    # Trading Execution Log & Ticker PNG Chart Auto-Insertion
    st.subheader("📈 Trading Execution Log")
    st.caption("Type ticker symbols to auto-generate and include basic PNG charts from Company Details.")
    
    with st.expander("📊 Quick Chart Viewer (Interactive 9-Month TradingView Chart - No Saved Images)", expanded=False):
        q_ticker = st.text_input("🔍 Enter ticker to review (e.g. NVDA, TSLA, AAPL)", value="", key=f"drc_quick_chart_{drc_date_str}").strip().upper()
        if q_ticker:
            st.markdown(f"**Interactive 9-Month TradingView Chart for {q_ticker}:**")
            render_tradingview_ticker_chart(q_ticker, max_days=190, height=550)

    typed_ticker_input = st.text_input(
        "🔍 Type ticker(s) for auto PNG chart (comma separated, e.g. CZR, SPY, NVDA)",
        value="",
        key=f"drc_typed_tickers_{drc_date_str}"
    )
    
    trades_list = drc_data.get("trades", [])
    existing_trade_tickers = [tr.get("ticker", "").upper() for tr in trades_list]
    
    if typed_ticker_input:
        new_tickers = [t.strip().upper() for t in typed_ticker_input.split(",") if t.strip()]
        for nt in new_tickers:
            if nt not in existing_trade_tickers:
                trades_list.append({
                    "ticker": nt,
                    "pnl": "",
                    "tags": [],
                    "notes": ""
                })
                existing_trade_tickers.append(nt)

    updated_trades = []
    for i, trade_item in enumerate(trades_list):
        tk = trade_item.get("ticker", "").upper()
        if not tk:
            continue
        
        exp_header = f"📌 Trade: **{tk}**"
        if trade_item.get('pnl'):
            exp_header += f" | PnL: `{trade_item.get('pnl')}`"
        if trade_item.get('entry_price'):
            exp_header += f" | Entry: `{trade_item.get('entry_price')}`"
        if trade_item.get('exit_price'):
            exp_header += f" | Exit: `{trade_item.get('exit_price')}`"
        if trade_item.get('shares'):
            exp_header += f" | Shares: `{trade_item.get('shares')}`"

        with st.expander(exp_header, expanded=True):
            r1_c1, r1_c2, r1_c3, r1_c4, r1_c5 = st.columns([1.2, 1, 1, 1, 1])
            with r1_c1:
                tk_pnl = st.text_input(f"PnL / Realized", value=trade_item.get("pnl", ""), key=f"tr_pnl_{tk}_{i}_{drc_date_str}")
            with r1_c2:
                tk_entry = st.text_input(f"Entry Price ($)", value=trade_item.get("entry_price", ""), key=f"tr_entry_{tk}_{i}_{drc_date_str}")
            with r1_c3:
                tk_exit = st.text_input(f"Exit Price ($)", value=trade_item.get("exit_price", ""), key=f"tr_exit_{tk}_{i}_{drc_date_str}")
            with r1_c4:
                tk_shares = st.text_input(f"Shares / Size", value=trade_item.get("shares", ""), key=f"tr_shares_{tk}_{i}_{drc_date_str}")
            with r1_c5:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button(f"🗑️ Remove Log", key=f"del_tr_{tk}_{i}_{drc_date_str}"):
                    drc_data["trades"] = [t for t in drc_data.get("trades", []) if t.get("ticker", "").upper() != tk]
                    save_daily_notes(drc_date, drc_data)
                    st.success(f"🗑️ Removed trade log entry for {tk}!")
                    rerun_app()
            
            # Trade-level tags
            tk_tags = trade_item.get("tags", [])
            all_tr_tag_options = sorted(list(set(tk_tags + existing_trade_tags + ["Breakout", "EP", "Pullback", "VCP", "H&S", "Flag", "Reversal", "Win", "Loss"])))
            selected_tr_tags = st.multiselect(f"🏷️ Trade Tags for {tk}", all_tr_tag_options, default=tk_tags, key=f"tr_tags_{tk}_{i}_{drc_date_str}")
            new_tr_tag = st.text_input(f"➕ Add custom trade tag for {tk}", key=f"tr_new_tag_{tk}_{i}_{drc_date_str}").strip()
            if new_tr_tag and new_tr_tag not in selected_tr_tags:
                selected_tr_tags.append(new_tr_tag)
                
            tk_notes = st.text_area(f"Notes for {tk}", value=trade_item.get("notes", ""), height=100, key=f"tr_notes_{tk}_{i}_{drc_date_str}")

            st.markdown(f"**Auto-generated PNG Chart for {tk}:**")
            png_chart_path = generate_ticker_png_chart(tk, drc_date_str)
            if png_chart_path and os.path.exists(png_chart_path):
                st.image(png_chart_path, use_container_width=True, caption=f"{tk} Daily Chart (PNG)")
            else:
                st.warning(f"Could not generate PNG chart for {tk}. Ensure market data is available.")

            user_img = st.file_uploader(f"Upload Execution Screenshot for {tk}", type=["png", "jpg", "jpeg"], key=f"upload_sc_{tk}_{i}_{drc_date_str}")
            saved_sc_path = trade_item.get("screenshot", "")
            if user_img is not None:
                ensure_daily_dirs()
                sc_filename = f"{tk}_{drc_date_str}_{uuid.uuid4().hex[:6]}.png"
                sc_full_path = SCREENSHOTS_DIR / sc_filename
                with open(sc_full_path, "wb") as f:
                    f.write(user_img.getbuffer())
                saved_sc_path = str(sc_full_path)
                st.success("✅ Screenshot uploaded!")
            
            if saved_sc_path and os.path.exists(saved_sc_path):
                st.image(saved_sc_path, use_container_width=True, caption=f"{tk} Execution Screenshot")

            updated_trades.append({
                "ticker": tk,
                "pnl": tk_pnl,
                "entry_price": tk_entry,
                "exit_price": tk_exit,
                "shares": tk_shares,
                "tags": selected_tr_tags,
                "notes": tk_notes,
                "screenshot": saved_sc_path
            })

    if save_btn:
        new_notes_data = {
            "date": drc_date_str,
            "grade": curr_grade,
            "pnl": curr_pnl,
            "goal": curr_goal,
            "checklist": {
                "3_trades": chk_3trades,
                "no_phone": chk_nophone,
                "fill_drc": chk_filldrc,
                "afterhours": chk_afterhours
            },
            "segments": edited_seg_df.to_dict(orient="records"),
            "learned": curr_learned,
            "changes": curr_changes,
            "overview": curr_overview,
            "easiest_50k": curr_easiest,
            "tags": selected_tags,
            "trades": updated_trades
        }
        if save_daily_notes(drc_date, new_notes_data):
            st.success(f"✅ Daily Report Card saved successfully for {drc_date_str}!")
        else:
            st.error("Failed to save Daily Report Card.")

# ---------- TAB 11: Chart Gallery ----------
with tab_ms:
    st.header("📸 Chart Gallery")
    st.markdown("Generate interactive Plotly charts for a custom list of tickers.")
    
    ms_tickers_input = st.text_area("Tickers (comma or newline separated)", "AAPL, WAB")
    
    if st.button("Generate Gallery", type="primary"):
        import re
        tickers_list = [t.strip().upper() for t in re.split(r'[,\n]+', ms_tickers_input) if t.strip()]
        
        if not tickers_list:
            st.warning("Please enter at least one ticker.")
        else:
            for ticker in tickers_list:
                st.divider()
                render_tradingview_ticker_chart(ticker)

# ---------- TAB 13: IBD Live Summary ----------
with tab_ibd_live:
    st.header("🎙️ IBD Live Summary")

    # ---- Create / Ingest Summary ----
    st.subheader("✍️ Create / Ingest Summary")
    with st.expander("📝 Paste an IBD Live markdown summary to reformat & save", expanded=False):
        st.caption("Follow the IBD Live markdown format (# title, ## 1. Market Pulse, ## 2. Top Tickers & Technical Setups table, ## 7. Full Ticker List). "
                   "It is reformatted via `python/sync_ibd_live_summaries.py` so the ticker list & details drive the plot view below.")
        in_col1, in_col2 = st.columns([1, 2])
        with in_col1:
            ingest_date = st.date_input("Date", value=datetime.now().date(), key="ibd_live_ingest_date")
            ingest_kind = st.radio("Type", ["Live / Intraday", "End of Day"], horizontal=True, key="ibd_live_ingest_kind")
        with in_col2:
            ingest_text = st.text_area(
                "Markdown summary",
                height=260,
                key="ibd_live_ingest_text",
                placeholder="# IBD Live Summary — YYYY-MM-DD\n\n## 1. Market Pulse\n<market overview>\n\n## 2. Top Tickers & Technical Setups\n| Ticker | Technical Action | Story | Status |\n|---|---|---|---|\n| AAPL | ... | ... | Watchlist |\n\n## 7. Full Ticker List\nAAPL, MSFT, NVDA, ...")
        if st.button("💾 Save & Reformat", type="primary", key="ibd_live_ingest_save"):
            date_str = ingest_date.isoformat()
            suffix = "_eod" if ingest_kind == "End of Day" else ""
            ok, msg, _sc = save_ibd_live_summary_from_text(date_str, ingest_text, suffix=suffix)
            if ok:
                st.success(msg)
                st.session_state.ibd_live_selected_date = date_str
                st.session_state.ibd_live_list_date = None
                rerun_app()
            else:
                st.error(msg)

    available_dates = load_ibd_live_summary_dates()
    if not available_dates:
        st.info("No IBD Live summaries found yet. Run `python/sync_ibd_live_summaries.py` to pull them in from ~/Documents/Zoom.")
    else:
        headlines = load_ibd_live_summary_headlines()
        available_set = set(available_dates)

        if "ibd_live_selected_date" not in st.session_state:
            st.session_state.ibd_live_selected_date = available_dates[-1]
        if "ibd_live_cal_month" not in st.session_state:
            last_dt = datetime.strptime(available_dates[-1], "%Y-%m-%d")
            st.session_state.ibd_live_cal_month = (last_dt.year, last_dt.month)

        selected_date = st.session_state.ibd_live_selected_date
        cal_col, main_col = st.columns([1, 3], gap="large")

        with cal_col:
            # ---- Calendar (left panel) ----
            st.subheader("📅 Calendar")
            cal_year, cal_month = st.session_state.ibd_live_cal_month
            nav_prev, nav_title, nav_next = st.columns([1, 4, 1])
            with nav_prev:
                if st.button("◀", key="ibd_live_cal_prev"):
                    cal_month -= 1
                    if cal_month < 1:
                        cal_month = 12
                        cal_year -= 1
                    st.session_state.ibd_live_cal_month = (cal_year, cal_month)
                    rerun_app()
            with nav_title:
                st.markdown(f"<div style='text-align:center;font-weight:600;'>{calendar.month_name[cal_month]} {cal_year}</div>", unsafe_allow_html=True)
            with nav_next:
                if st.button("▶", key="ibd_live_cal_next"):
                    cal_month += 1
                    if cal_month > 12:
                        cal_month = 1
                        cal_year += 1
                    st.session_state.ibd_live_cal_month = (cal_year, cal_month)
                    rerun_app()

            dow_cols = st.columns(7)
            for c, name in zip(dow_cols, ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]):
                c.markdown(f"<div style='text-align:center;color:#888;font-size:0.8em;'>{name}</div>", unsafe_allow_html=True)

            for week in calendar.Calendar(firstweekday=0).monthdayscalendar(cal_year, cal_month):
                week_cols = st.columns(7)
                for col, day in zip(week_cols, week):
                    if day == 0:
                        continue
                    day_str = f"{cal_year:04d}-{cal_month:02d}-{day:02d}"
                    with col:
                        if day_str in available_set:
                            is_selected = day_str == selected_date
                            if st.button(str(day), key=f"ibd_live_cal_{day_str}",
                                         type="primary" if is_selected else "secondary",
                                         help=headlines.get(day_str, "")[:200]):
                                st.session_state.ibd_live_selected_date = day_str
                                st.session_state.ibd_live_list_date = None
                                rerun_app()
                        else:
                            st.markdown(f"<div style='text-align:center;color:#444;padding:6px 0;'>{day}</div>", unsafe_allow_html=True)

            st.caption("Days with a summary are selectable; hover for the headline.")

        with main_col:
            # ---- SPY Day Picker ----
            st.subheader("📊 SPY Day Picker")
            st.caption("Black dot = an IBD Live summary exists for that day. Click a bar to open its summary.")
            spy_fig, spy_err = build_spy_summary_chart(available_dates, selected_date=selected_date)
            if spy_err:
                st.warning(spy_err)
            else:
                spy_event = st.plotly_chart(spy_fig, on_select="rerun", selection_mode="points",
                                            key="ibd_live_spy_chart", use_container_width=True)
                if process_spy_selection(spy_event, available_set):
                    rerun_app()

            st.divider()

            # ---- Summary for selected date ----
            st.subheader(f"📋 Summary — {selected_date}")
            md_text, sidecar, eod_md, eod_sidecar = load_ibd_live_combined_sidecar(selected_date)
            if md_text:
                report_html = render_ibd_live_report(md_text, date_str=selected_date,
                                                     market_summary=(sidecar or {}).get("market_summary", ""))
                with st.expander("📄 Full Market Report", expanded=True):
                    st.markdown(report_html, unsafe_allow_html=True)
            else:
                st.warning("No markdown summary found for this date.")

            if eod_md:
                st.subheader(f"🌙 End of Day Summary — {selected_date}")
                eod_html = render_ibd_live_report(eod_md, date_str=selected_date,
                                                  market_summary=(eod_sidecar or {}).get("market_summary", ""),
                                                  report_title="END OF DAY REPORT")
                with st.expander("📄 End of Day Market Report", expanded=True):
                    st.markdown(eod_html, unsafe_allow_html=True)
            else:
                st.info("No End of Day summary yet for this date — use the **Create / Ingest Summary** box above and pick **End of Day**.")

            day_tickers = (sidecar or {}).get("tickers", [])
            ticker_details = (sidecar or {}).get("ticker_details", {})

            st.divider()
            st.subheader("📈 Tickers & Chart")

            if not day_tickers:
                st.info("No tickers were captured for this date.")
            else:
                if (st.session_state.get("ibd_live_list_date") != selected_date):
                    st.session_state.ibd_live_ticker_idx = 0
                    st.session_state.ibd_live_list_date = selected_date
                    st.session_state.ibd_live_active_ticker = day_tickers[0]

                list_col, chart_col = st.columns([1, 3])
                with list_col:
                    st.caption("Click a ticker, or press **Space** to advance.")
                    for i, tk in enumerate(day_tickers):
                        is_active = (tk == st.session_state.get("ibd_live_active_ticker"))
                        if st.button(tk, key=f"ibd_live_ticker_btn_{selected_date}_{tk}",
                                     type="primary" if is_active else "secondary",
                                     use_container_width=True):
                            st.session_state.ibd_live_ticker_idx = i
                            st.session_state.ibd_live_active_ticker = tk
                            rerun_app()

                    next_clicked = st.button("⏭️ Next Ticker (Space)", key="ibd_live_next_ticker_btn", use_container_width=True)
                    if next_clicked:
                        idx = (st.session_state.get("ibd_live_ticker_idx", 0) + 1) % len(day_tickers)
                        st.session_state.ibd_live_ticker_idx = idx
                        st.session_state.ibd_live_active_ticker = day_tickers[idx]
                        rerun_app()

                    st_html("""
                    <script>
                    (function() {
                        var doc = window.parent.document;
                        function findNextButton() {
                            var buttons = doc.querySelectorAll('button');
                            for (var i = 0; i < buttons.length; i++) {
                                if (buttons[i].innerText.trim() === '⏭️ Next Ticker (Space)') return buttons[i];
                            }
                            return null;
                        }
                        if (window.parent.__ibdLiveSpaceHandler) {
                            doc.removeEventListener('keydown', window.parent.__ibdLiveSpaceHandler, true);
                        }
                        window.parent.__ibdLiveSpaceHandler = function(e) {
                            var active = doc.activeElement;
                            var tag = active ? active.tagName.toLowerCase() : '';
                            if (tag === 'input' || tag === 'textarea' || (active && active.isContentEditable)) return;
                            if (e.code === 'Space' || e.key === ' ') {
                                var btn = findNextButton();
                                if (btn) { e.preventDefault(); btn.click(); }
                            }
                        };
                        doc.addEventListener('keydown', window.parent.__ibdLiveSpaceHandler, true);
                    })();
                    </script>
                    """, height=1)

                with chart_col:
                    active_ticker = st.session_state.get("ibd_live_active_ticker", day_tickers[0])
                    detail = ticker_details.get(active_ticker)
                    if detail:
                        actionability = detail.get('actionability', '')
                        tech_action = detail.get('technical_action', '')
                        story = detail.get('story', '')
                        pill_color = ('#1f9d55' if actionability == 'Actionable'
                                      else '#1f77b4' if actionability == 'Watchlist' else '#6c757d')
                        st.markdown(
                            f"""
                            <div style="background:#ffffff; border:1px solid #e2e7ee; border-left:5px solid #ff6b1a;
                                        border-radius:8px; padding:14px 18px; margin-bottom:10px;
                                        font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
                                <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:6px;">
                                    <span style="font-size:22px; font-weight:800; color:#0a1f3d;">{html.escape(active_ticker)}</span>
                                    <span style="background:{pill_color}; color:#ffffff; font-size:12px; font-weight:700;
                                                 padding:3px 12px; border-radius:20px; text-transform:uppercase; letter-spacing:0.5px;">{html.escape(actionability)}</span>
                                    <span style="font-size:16px; font-weight:700; color:#143a63;">{html.escape(tech_action)}</span>
                                </div>
                                <div style="font-size:16px; color:#1c2733; line-height:1.55;">{html.escape(story)}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    with st.spinner(f"Loading chart for {active_ticker}..."):
                        fig, err = build_ticker_price_chart(active_ticker, filtered_df)
                    if err:
                        st.warning(err)
                    elif fig:
                        st.plotly_chart(fig, use_container_width=True, key=f"ibd_live_chart_{active_ticker}")

            # ---- Ticker Comments ----
            st.divider()
            st.subheader("💬 Ticker Comments")
            all_known_tickers = get_all_commented_or_mentioned_tickers()
            if all_known_tickers:
                default_ticker = st.session_state.get("ibd_live_active_ticker", all_known_tickers[0])
                default_idx = all_known_tickers.index(default_ticker) if default_ticker in all_known_tickers else 0
                comment_ticker = st.selectbox("Select a ticker",
                                              all_known_tickers, index=default_idx,
                                              key="ibd_live_comment_ticker_select",
                                              help="Every ticker you commented on or the show mentioned.")
                if comment_ticker != st.session_state.get("ibd_live_active_ticker"):
                    st.session_state.ibd_live_active_ticker = comment_ticker

                timeline = get_ticker_comment_timeline(comment_ticker)
                summary_dates = load_ibd_live_summary_dates()
                latest_show_date = summary_dates[-1] if summary_dates else None
                n_trans, n_you, _, n_boilerplate = render_comment_timeline(timeline, comment_ticker, latest_show_date)
                if n_trans or n_you:
                    st.markdown(
                        f"""
                        <div style="display:flex; gap:12px; margin:2px 0 8px 0; flex-wrap:wrap;">
                            <span style="background:#1f77b4; color:#fff; font-size:12px; font-weight:700; padding:3px 12px; border-radius:20px;">🎙️ {n_trans} show mention{'s' if n_trans != 1 else ''}</span>
                            <span style="background:#ff6b1a; color:#fff; font-size:12px; font-weight:700; padding:3px 12px; border-radius:20px;">✍️ {n_you} comment{'s' if n_you != 1 else ''}</span>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("No comments or transcript mentions yet for this ticker.")

                with st.form(key="ibd_live_add_comment_form", clear_on_submit=True):
                    st.markdown(f"**➕ Add a comment for {comment_ticker}**")
                    c_date, c_text = st.columns([1, 3])
                    with c_date:
                        comment_date = st.date_input("Date", value=datetime.now().date(), key="ibd_live_comment_date")
                    with c_text:
                        comment_text = st.text_area("Comment", key="ibd_live_comment_text", height=80,
                                                    placeholder="Why is this ticker worth watching? What's the setup?")
                    if st.form_submit_button("💾 Save Comment"):
                        if comment_text.strip():
                            save_ticker_comment(comment_ticker, comment_date.isoformat(), comment_text)
                            st.success(f"Saved comment for {comment_ticker}.")
                            rerun_app()
                        else:
                            st.warning("Comment text can't be empty.")
            else:
                st.caption("No tickers with comments or transcript mentions yet.")

# Footer
st.divider()
footer_text = "**Daily Relative Strength Analysis Dashboard**"
if has_historical:
    footer_text += " | Historical Data: Oct 2021 - Present"
footer_text += " | Built with Streamlit"
st.markdown(footer_text)
