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

def focus_ibd_live_date(date_str):
    """Point the whole IBD Live tab at date_str — calendar month, selected day, ticker
    list, chart and the comments panel — so a freshly ingested summary lands fully
    loaded instead of leaving the old day's tickers on screen."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return
    st.session_state.ibd_live_selected_date = date_str
    st.session_state.ibd_live_cal_month = (dt.year, dt.month)
    st.session_state.ibd_live_list_date = None  # re-seeds the ticker list on next render
    st.session_state.ibd_live_ticker_idx = 0
    _, combined, _, _ = load_ibd_live_combined_sidecar(date_str)
    tickers = (combined or {}).get("tickers") or []
    if tickers:
        st.session_state.ibd_live_active_ticker = tickers[0]
        st.session_state.ibd_live_comment_ticker_select = tickers[0]
        st.session_state._ibd_live_comment_synced_to = tickers[0]

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

@st.cache_data(ttl=300, show_spinner=False)
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

@st.cache_data(ttl=28800, show_spinner=False)
def render_ibd_live_report(md_text, date_str="", market_summary="", report_title="DAILY MARKET REPORT"):
    """Render an IBD Live markdown summary as a styled, IBD-report-style HTML card."""
    if not md_text:
        return ""
    # Pasted summaries head their sections with '###' where Zoom-synced ones use '##'.
    # Promote them so both get the same styled section headers instead of small h3s.
    if not re.search(r"(?m)^##\s", md_text) and re.search(r"(?m)^###\s", md_text):
        md_text = re.sub(r"(?m)^#(#{2,5}\s)", r"\1", md_text)
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
IBD_BAR_UP_COLOR = "#2736e9"
IBD_BAR_DOWN_COLOR = "#de32ae"
IBD_BAR_NEUTRAL_COLOR = "#787b86"

def make_ibd_ohlc_traces(x, open_values, high_values, low_values, close_values,
                         name="Price", showlegend=False, customdata=None):
    """Build OHLC traces colored by close versus the previous close.

    Plotly's OHLC trace colors bars by close versus open. Splitting the data into
    colored traces lets these charts use the IBD rule instead: blue when the close
    is at/above the previous close, magenta otherwise, and neutral for the first
    bar where no previous close exists.
    """
    x_values = list(x)
    opens = list(open_values)
    highs = list(high_values)
    lows = list(low_values)
    closes = list(close_values)
    custom_values = list(customdata) if customdata is not None else None
    directions = [
        0 if (
            i == 0
            or pd.isna(closes[i])
            or pd.isna(closes[i - 1])
        ) else 1 if closes[i] >= closes[i - 1] else -1
        for i in range(len(closes))
    ]
    traces = []

    for direction, color, legend in (
        (0, IBD_BAR_NEUTRAL_COLOR, False),
        (1, IBD_BAR_UP_COLOR, showlegend),
        (-1, IBD_BAR_DOWN_COLOR, False),
    ):
        indices = [i for i, value in enumerate(directions) if value == direction]
        if not indices:
            continue
        kwargs = dict(
            x=[x_values[i] for i in indices],
            open=[opens[i] for i in indices],
            high=[highs[i] for i in indices],
            low=[lows[i] for i in indices],
            close=[closes[i] for i in indices],
            name=name,
            showlegend=legend,
            increasing_line_color=color,
            decreasing_line_color=color,
        )
        if custom_values is not None:
            kwargs["customdata"] = [custom_values[i] for i in indices]
        traces.append(go.Ohlc(**kwargs))
    return traces

@st.cache_data(ttl=28800, show_spinner=False)
def build_spy_summary_chart(summary_dates, selected_date="", height=520):
    """SPY daily OHLC bars + volume chart. Black dots sit on top of bars whose day
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
    for trace in make_ibd_ohlc_traces(
        sub.index, sub['Open'], sub['High'], sub['Low'], sub['Close'],
        name='SPY', showlegend=False, customdata=[[d] for d in dates],
    ):
        fig.add_trace(trace, row=1, col=1)

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

    previous_close = sub['Close'].shift(1)
    vol_colors = [
        IBD_BAR_NEUTRAL_COLOR if pd.isna(previous_close.iloc[i])
        else IBD_BAR_UP_COLOR if sub['Close'].iloc[i] >= previous_close.iloc[i]
        else IBD_BAR_DOWN_COLOR
        for i in range(len(sub))
    ]
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
    previous_close = sub['Close'].shift(1)
    neutral_mask = previous_close.isna()
    up_mask = previous_close.notna() & (sub['Close'] >= previous_close)
    down_mask = previous_close.notna() & (sub['Close'] < previous_close)
    
    ax_price.vlines(dates[neutral_mask], sub['Low'][neutral_mask], sub['High'][neutral_mask], color=IBD_BAR_NEUTRAL_COLOR, linewidth=1)
    ax_price.vlines(dates[up_mask], sub['Low'][up_mask], sub['High'][up_mask], color=IBD_BAR_UP_COLOR, linewidth=1)
    ax_price.vlines(dates[down_mask], sub['Low'][down_mask], sub['High'][down_mask], color=IBD_BAR_DOWN_COLOR, linewidth=1)
    
    body_bottom = np.minimum(sub['Open'], sub['Close'])
    body_height = np.abs(sub['Close'] - sub['Open'])
    body_height = np.where(body_height == 0, 0.01, body_height)
    
    ax_price.bar(dates[neutral_mask], body_height[neutral_mask], bottom=body_bottom[neutral_mask], color=IBD_BAR_NEUTRAL_COLOR, width=0.6, align='center')
    ax_price.bar(dates[up_mask], body_height[up_mask], bottom=body_bottom[up_mask], color=IBD_BAR_UP_COLOR, width=0.6, align='center')
    ax_price.bar(dates[down_mask], body_height[down_mask], bottom=body_bottom[down_mask], color=IBD_BAR_DOWN_COLOR, width=0.6, align='center')
    
    ax_price.plot(dates, sub['EMA10'], color='#FF9800', label='EMA 10', linewidth=1.5)
    ax_price.plot(dates, sub['EMA21'], color='#2196F3', label='EMA 21', linewidth=1.5)
    ax_price.plot(dates, sub['SMA50'], color='#F44336', label='SMA 50', linewidth=1.5)
    
    latest_close = sub['Close'].iloc[-1]
    ax_price.set_title(f"{ticker} Daily Chart - ${latest_close:.2f}", color='#ffffff', fontsize=13, fontweight='bold', pad=8)
    ax_price.legend(loc='upper left', facecolor='#2a2e39', edgecolor='none', labelcolor='#ffffff', fontsize=8)
    
    ax_vol.bar(dates[neutral_mask], sub['Volume'][neutral_mask], color=IBD_BAR_NEUTRAL_COLOR, alpha=0.7, width=0.6)
    ax_vol.bar(dates[up_mask], sub['Volume'][up_mask], color=IBD_BAR_UP_COLOR, alpha=0.7, width=0.6)
    ax_vol.bar(dates[down_mask], sub['Volume'][down_mask], color=IBD_BAR_DOWN_COLOR, alpha=0.7, width=0.6)
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
        # Ind Group Rank is CALCULATED live by the daily screener (1 = best industry
        # by mean RS); only fall back to IBD Data Tables when the ticker is absent
        # from the screener snapshot.
        _calc_map = load_calculated_group_map()
        _calc_grp = _calc_map.get(tk) or _calc_map.get(tk_norm) or {}
        _calc_rank = _calc_grp.get('Ind Group Rank')
        if _calc_rank is not None:
            ibd_ind_rank = str(int(_calc_rank))
        elif pd.notna(ibd_info.get('Industry Group Rank')):
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

    # Row 1: IBD OHLC Bars & Moving Averages
    for trace in make_ibd_ohlc_traces(
        df_daily.index, df_daily['Open'], df_daily['High'], df_daily['Low'], df_daily['Close'],
        name='Price', showlegend=False,
    ):
        fig.add_trace(trace, row=1, col=1)

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
def create_lightweight_ohlc_html(df, title="", height=600, markers=None, rs_label=None,
                                        rs_raw=None, rs_quick=None, rs_quicksand=None, rs_gd=None,
                                        volume_data=None, volume_sma50=None,
                                        pp10_dates=None, pp5_dates=None,
                                        churn_dates=None, stall_dates=None, ll3_dates=None,
                                        pattern_js=""):
    if df is None or df.empty:
        return "<div style='padding:20px; color:#999;'>No data available</div>"

    candles = []
    previous_close = None
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp())
            close_value = float(row['Close'])
            candles.append({
                'time': ts,
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': close_value,
                'color': IBD_BAR_NEUTRAL_COLOR if previous_close is None else IBD_BAR_UP_COLOR if close_value >= previous_close else IBD_BAR_DOWN_COLOR,
            })
            previous_close = close_value
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

        // IBD-style OHLC bars: color each bar by close versus the previous close.
        const barSeries = priceChart.addBarSeries({{
            upColor: '{IBD_BAR_UP_COLOR}', downColor: '{IBD_BAR_DOWN_COLOR}',
            openVisible: true, thinBars: false
        }});
        barSeries.setData({json.dumps(candles)});

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
            if (all.length) barSeries.setMarkers(all);
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
            const data = param.seriesData.get(barSeries);
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
                const previousClose = i > 0 ? rawBars[i - 1].close : null;
                const ibdColor = previousClose === null
                    || !Number.isFinite(b.close)
                    || !Number.isFinite(previousClose)
                    ? '#787b86'
                    : b.close >= previousClose ? '#2736e9' : '#de32ae';
                currentX += width + 3;
                layoutBars.push({{ ...b, xStart, xEnd, xCenter, width, previousClose, ibdColor }});
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

                const strokeColor = b.ibdColor;
                const fillColor = b.ibdColor === '#2736e9'
                    ? 'rgba(39, 54, 233, 0.35)'
                    : b.ibdColor === '#de32ae'
                        ? 'rgba(222, 50, 174, 0.35)'
                        : 'rgba(120, 123, 134, 0.35)';

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
                const changeBase = hb.previousClose === null ? hb.open : hb.previousClose;
                const changePct = (((hb.close - changeBase) / changeBase) * 100).toFixed(2);
                const sign = changePct >= 0 ? '+' : '';

                tooltip.style.display = 'block';
                tooltip.innerHTML = `
                    <div style="font-weight:bold; color:#fff; margin-bottom:4px;">${{hb.date}}</div>
                    <div style="color:${{hb.ibdColor}};">Open: $${{hb.open.toFixed(2)}} | High: $${{hb.high.toFixed(2)}}</div>
                    <div style="color:${{hb.ibdColor}};">Low: $${{hb.low.toFixed(2)}} | Close: $${{hb.close.toFixed(2)}} (${{sign}}${{changePct}}%)</div>
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

# ---------------------- Ticker Cache Update (top bar) ----------------------
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

def start_ticker_cache_update(mode="price"):
    """Launch update_ticker_cache.py in the background. Returns (ok, message).

    mode: "price"         — OHLCV price bars only
          "price+fund"    — OHLCV bars + EPS/ROE fundamentals (CLI-only now;
                            no UI button uses it since the two are separate buttons)
          "fundamentals"  — EPS/ROE fundamentals only (fast, skips the price pass)
    """
    pids = is_ticker_cache_updater_running()
    if pids:
        return False, f"Update already running (pid {', '.join(pids)})."
    try:
        TICKER_CACHE_UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(TICKER_CACHE_UPDATE_SCRIPT)]
        if mode == "price+fund":
            cmd.append("--with-fundamentals")
        elif mode == "fundamentals":
            # All yfinance calls inside update_ticker_cache.py go through
            # yf_ratelimit (429 backoff 60/180/420s + global cooling gate), so
            # both buttons are rate-limit safe. Fundamentals additionally runs
            # with a small parallel pool and a wall-clock cap so a background
            # click can never hammer the metadata endpoint or run forever.
            cmd += ["--fundamentals-only",
                    "--fund-workers", "4",
                    "--fund-delay", "0.2",
                    "--fund-timeout", "60"]
        labels = {"price": "[OHLCV only]",
                  "price+fund": "[OHLCV + fundamentals]",
                  "fundamentals": "[fundamentals only]"}
        label = labels.get(mode, "[OHLCV only]")
        with open(TICKER_CACHE_UPDATE_LOG, "a", encoding="utf-8") as lf:
            lf.write(f"\n{'='*60}\n{datetime.now().isoformat()} {label}\n")
            proc = subprocess.Popen(
                cmd,
                cwd=str(Path(__file__).resolve().parent),
                stdout=lf, stderr=lf, start_new_session=True)
        return True, f"Started ticker cache update {label} (pid {proc.pid})."
    except Exception as e:
        return False, f"Failed to start update: {e}"

col1, col2, col3 = st.columns([2, 6, 4])
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
                    subprocess.run([sys.executable, "check_remote_and_append.py", "--force", "--skip-ticker-cache"], cwd=repo_dir, capture_output=True, text=True)
                    st.cache_data.clear()
                    rerun_app()
            except Exception as e:
                st.error(f"An error occurred: {e}")
with col3:
    st.subheader("🔄 Ticker Cache")
    _tc_pids = is_ticker_cache_updater_running()
    if _tc_pids:
        st.warning(f"⚠️ Updating tickers… ({len(_tc_pids)} process running). "
                   f"Data may be stale until it finishes.")
        if st.button("🛑 Stop Update", key="stop_ticker_cache_update",
                     help="Kill the running update_ticker_cache.py process"):
            for p in _tc_pids:
                try:
                    subprocess.run(["kill", p], capture_output=True)
                except Exception:
                    pass
            rerun_app()
    else:
        st.caption("Ticker cache is idle.")
    b1, b2 = st.columns(2)
    with b1:
        if st.button("▶️ Price Cache", key="run_ticker_cache_update",
                     help="Fetch the latest daily OHLCV bars for every ticker (yfinance)"):
            ok, msg = start_ticker_cache_update(mode="price")
            if ok:
                st.success(msg)
                rerun_app()
            else:
                st.warning(msg)
    with b2:
        if st.button("📊 Fundamentals", key="run_fundamentals_update",
                     help="Fetch EPS/ROE fundamentals for every ticker (yfinance)"):
            ok, msg = start_ticker_cache_update(mode="fundamentals")
            if ok:
                st.success(msg)
                rerun_app()
            else:
                st.warning(msg)

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
def _grade_to_num(val):
    """IBD letter grade (A+, A, A-, ..., E) -> 1-99 numeric, else None.
    Used only as a last-resort fallback when a ticker is not in the daily
    screener snapshot."""
    if val is None:
        return None
    s = str(val).strip().upper()
    if not s:
        return None
    base = {'A': 95, 'B': 80, 'C': 60, 'D': 40, 'E': 20}.get(s[0])
    if base is None:
        return None
    if len(s) > 1 and s[1] == '+':
        return base + 4
    if len(s) > 1 and s[1] == '-':
        return base - 4
    return base


@st.cache_data
def load_calculated_group_map():
    """Map symbol -> {'Ind Group Rank': float, 'Ind Group RS': float} from the
    daily screener output (CALCULATED live from RS ratings - NOT IBD letter
    grades).  Falls back to the raw IBD letter grades only when a ticker is not
    in the screener snapshot."""
    sc_path = Path(__file__).resolve().parent / "output" / "daily_screener.csv"
    if not sc_path.exists():
        return {}
    try:
        df = pd.read_csv(sc_path, low_memory=False)
        if 'Symbol' not in df.columns or 'Ind Group Rank' not in df.columns:
            return {}
        out = {}
        for _, r in df.iterrows():
            sym = str(r['Symbol']).strip().upper()
            grp = r.get('Ind Group Rank')
            grs = r.get('Ind Group RS')
            entry = {
                'Ind Group Rank': float(grp) if pd.notna(grp) else None,
                'Ind Group RS': float(grs) if pd.notna(grs) else None,
            }
            out[sym] = entry
            # normalized variant (same convention as load_ibd_data_tables_full) so
            # pattern tickers like "BRK-B" hit the screener's "BRK.B" entry
            norm = sym.replace(".", "").replace("-", "").replace("/", "").replace(" ", "")
            out[norm] = entry
        return out
    except Exception as e:
        print(f"Error loading calculated group map: {e}")
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

# ---------------------- Analysis helpers ----------------------
RS_COL = 'Relative Strength'
PRICE_COL = 'Close' if 'Close' in filtered_df.columns else 'Price'

has_col = lambda sn, c: c in sn.columns

def _n(sn, c):
    return pd.to_numeric(sn[c], errors='coerce') if has_col(sn, c) else pd.Series(np.nan, index=sn.index)

# Rating/percentile-style columns that appear across both the rs_ranking.py world (Overview /
# Top Performers / Trends / Data Table - "Relative Strength", "Percentile", "1M_RS_Percentile",
# etc.) and the calc_ibd_ratings.py world (Ratings Scanner / Daily Screener / Weekly Screener -
# "RS Rating", "EPS Rating", "Comp Rating", etc.). Real IBD ratings are always whole numbers in
# MarketSurge itself, so these get rounded to a clean integer before display rather than showing
# float noise like "99.00000".
RATING_COLS = [
    'Relative Strength', 'Percentile', '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile',
    '1M', '3M', '6M', 'RS Rating', 'EPS Rating', 'Comp Rating', 'RS 3-Month Rating',
    'RS 6-Month Rating', 'RS 3M', 'RS 6M', 'A/D Score', 'SMR Score', '_g250_score',
    'Ind Group RS', 'Earnings Stability', 'P/E Percent Rank', 'EPS % Growth 5 Yr Pct Rnk',
    'rs_score',
]

def _int_ratings(df, cols=None):
    """Round known rating/percentile columns to whole numbers (nullable Int64, so NaN survives
    as <NA> instead of crashing or silently becoming 0) for clean display."""
    cols = cols if cols is not None else RATING_COLS
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce').round().astype('Int64')
    return df

# Latest cross-sectional snapshot (one row per ticker) via the analysis blocks.
if has_historical and 'date' in filtered_df.columns:
    _maxd = filtered_df['date'].max()
    snap = filtered_df[filtered_df['date'] == _maxd].drop_duplicates(subset=['Ticker'], keep='first').copy()
else:
    snap = filtered_df.drop_duplicates(subset=['Ticker'], keep='first').copy()

RS_SER = _n(snap, RS_COL)
PCT_SER = _n(snap, 'Percentile')
RS_BASE = RS_SER.copy()
DELTA_S = RS_SER - RS_SER.mean()

def rs_bucket(rs):
    if rs >= 80: return 'Leader (≥80)'
    if rs >= 65: return 'Strong (65-79)'
    if rs >= 50: return 'Broad (50-64)'
    return 'Laggard (<50)'

def leader_score(row):
    s = 0.0
    if not np.isnan(row.get('Percentile', np.nan)): s += row['Percentile'] / 100.0 * 2
    if not np.isnan(row.get('6M_RS_Percentile', np.nan)): s += (row['6M_RS_Percentile'] / 100.0)
    if not np.isnan(row.get('3M_RS_Percentile', np.nan)): s += (row['3M_RS_Percentile'] / 100.0) * 0.5
    if not np.isnan(row.get('PctFrom52WkHigh', np.nan)): s += max(0.0, (1 - row['PctFrom52WkHigh'] / 100.0)) * 0.5
    if not np.isnan(row.get('RevenueGrowth', np.nan)): s += min(1.0, max(0.0, row['RevenueGrowth'] / 50.0)) * 0.5
    return round(s, 2)

# ---------------------- Tabs ----------------------
if has_historical:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11, tab12, tab13, tab14, tab15, tab16, tab17 = st.tabs(
        ["📈 Overview", "🎯 Top Performers",
         "📉 Trends", "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table", "🔍 Pattern Finder", "🏆 IBD Pattern", "📝 Daily Report Card", "📸 MarketSurge Screenshots", "🎙️ IBD Live Summary", "🧪 Backtests", "🔍 Scans & Leads", "📐 TV Pattern", "📊 Ratings Scanner", "📋 Daily Screener", "📅 Weekly Screener"])
else:
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11, tab12, tab13, tab14, tab15, tab16, tab17 = st.tabs(
        ["📈 Overview", "🎯 Top Performers", "📊 Distributions",
         "🏭 Industry Rotation", "💼 Company Details", "📋 Data Table", "🔍 Pattern Finder", "🏆 IBD Pattern", "📝 Daily Report Card", "📸 MarketSurge Screenshots", "🎙️ IBD Live Summary", "🧪 Backtests", "🔍 Scans & Leads", "📐 TV Pattern", "📊 Ratings Scanner", "📋 Daily Screener", "📅 Weekly Screener"])

tab_ibd_pattern = tab8
tab_tv_pattern = tab14
tab_drc = tab9
tab_backtests = tab12
tab_scans = tab13
tab_ms = tab10
tab_ibd_live = tab11
tab_ratings = tab15
tab_screener = tab16
tab_weekly_screener = tab17


# ---------- TAB 1: Overview ----------
with tab1:
    # ---- SPY benchmark (most recent 9 months) + volume ----
    from plotly.subplots import make_subplots
    _mk_dir = Path(__file__).resolve().parent / 'ticker_cache'
    _spy_path = _mk_dir / 'SPY_1d.parquet'
    _spy = None
    if _spy_path.exists():
        try:
            _spy = pd.read_parquet(_spy_path).reset_index()
            _spy.columns = [str(c) for c in _spy.columns]
            if 'Date' not in _spy.columns:
                _spy = _spy.rename(columns={_spy.columns[0]: 'Date'})
            _spy['Date'] = pd.to_datetime(_spy['Date'])
            _spy = _spy.sort_values('Date').drop_duplicates('Date', keep='last')
            _cut = _spy['Date'].max() - pd.DateOffset(months=9)
            _spy = _spy[_spy['Date'] >= _cut]
        except Exception:
            _spy = None
    if _spy is not None and not _spy.empty:
        st.subheader("🇺🇸 SPY — S&P 500 ETF (last 9 months)")
        _spy = _spy.set_index('Date')
        _cl = _spy['Close'].astype(float)
        _spy['MA50'] = _cl.rolling(50).mean()
        _spy['MA200'] = _cl.rolling(200).mean()
        _volc = 'Volume' if 'Volume' in _spy.columns else None

        # ---- IBD-style distribution days (independent of sidebar range) ----
        _dd = _spy.copy()
        _dd['Close'] = _dd['Close'].astype(float)
        _dd['Volume'] = _dd['Volume'].astype(float)
        _dd['ret'] = _dd['Close'].pct_change() * 100.0
        _dd['is_dd'] = (_dd['ret'] <= -0.2) & (_dd['Volume'] > _dd['Volume'].shift(1))
        _WINDOW = 25
        _close_arr = _dd['Close'].to_numpy()
        _act_arr = np.zeros(len(_dd), dtype=bool)
        for _i in range(len(_dd)):
            if not bool(_dd['is_dd'].iloc[_i]):
                continue
            _later = _close_arr[_i + 1:]
            if _later.size and _later.max() >= 1.05 * _close_arr[_i]:
                continue
            _act_arr[_i] = True
        _dd['active_dd'] = _act_arr
        _active_count = int(_dd['active_dd'].iloc[-_WINDOW:].sum())
        if _active_count <= 4:
            _status = 'Confirmed uptrend'
        elif _active_count <= 6:
            _status = 'Uptrend under pressure'
        else:
            _status = 'Market in correction'

        # ---- IBD Follow-Through Day + Accumulation Day ----
        if _volc is not None:
            _v = _spy[_volc].astype(float)
            _retdd = _dd['ret'] if 'ret' in _dd else _cl.pct_change() * 100.0
            _dd['volup'] = _v > _v.shift(1)
            _dd['ft'] = (_retdd >= 1.25) & _dd['volup'].fillna(False)
            _dd['acc'] = (_retdd >= 1.2) & _dd['volup'].fillna(False) & ~np.asarray(_dd['ft'])
            # rally-attempt day counting: # sessions since the most recent N-day low
            _low_arr = _dd['Low'].astype(float).to_numpy()
            _dayN = np.zeros(len(_dd), dtype=int)
            for _k in range(len(_dd)):
                _win = _low_arr[max(0, _k - 49):_k + 1]
                _idx = max(0, _k - 49) + int(np.argmin(_win))
                _dayN[_k] = _k - _idx
            _dd['ftd'] = _dd['ft'] & (_dayN >= 4) & (_dayN <= 12)
        else:
            _dd['ftd'] = False
            _dd['acc'] = False

        _regime_color = {'Confirmed uptrend': '#2f9e5f', 'Uptrend under pressure': '#c9a227',
                          'Market in correction': '#d33'}.get(_status, '#888')

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.75, 0.25])
        for trace in make_ibd_ohlc_traces(
            _spy.index, _spy['Open'], _spy['High'], _spy['Low'], _cl,
            name='SPY', showlegend=True,
        ):
            fig.add_trace(trace, row=1, col=1)
        _spy['EMA21'] = _cl.ewm(span=21, adjust=False).mean()
        fig.add_trace(go.Scatter(x=_spy.index, y=_spy['EMA21'], name='21 EMA', line=dict(width=1, color='#26a69a')), row=1, col=1)
        fig.add_trace(go.Scatter(x=_spy.index, y=_spy['MA50'], name='50 MA', line=dict(width=1, color='red')), row=1, col=1)
        fig.add_trace(go.Scatter(x=_spy.index, y=_spy['MA200'], name='200d', line=dict(width=1, color='blue')), row=1, col=1)

        # ---- Webby / IBD Power Trend (Mike Webster) ----
        _e21 = _spy['EMA21'].to_numpy(float)
        _s50 = _spy['MA50'].to_numpy(float)
        _c = _cl.to_numpy(float)
        _o = _spy['Open'].astype(float).to_numpy(float)
        _l = _spy['Low'].astype(float).to_numpy(float)
        _h = _spy['High'].astype(float).to_numpy(float)
        _ns = len(_spy)
        _low_ok = (_spy['Low'] > _spy['EMA21']).rolling(10, min_periods=10).min().fillna(False).to_numpy()
        _ema_ok = (_spy['EMA21'] > _spy['MA50']).rolling(5, min_periods=5).min().fillna(False).to_numpy()
        _ma50_ma = _spy['MA50'].rolling(5, min_periods=5).mean()
        _ma50up = (_spy['MA50'] > _ma50_ma).fillna(False).to_numpy()
        _rh = _spy['High'].rolling(63, min_periods=1).max().to_numpy(float)
        _recent_hi = float(_h[-252:].max())
        _cond = _low_ok.astype(bool) & _ema_ok.astype(bool) & _ma50up.astype(bool) & (_c >= _o)
        _pt = np.zeros(_ns, dtype=bool)
        _on = False
        for _i in range(_ns):
            if not _on:
                if _cond[_i]:
                    _on = True
                    _pt[_i] = True
            else:
                if _e21[_i] < _s50[_i]:
                    _on = False
                elif (_c[_i] < 0.9 * _rh[_i]) and (_c[_i] < _s50[_i]):
                    _on = False
                else:
                    _pt[_i] = True
        _pt_on = bool(_pt[-1])

        if _volc is not None:
            _up = _cl >= _spy['Open'].astype(float)
            _vcols = ['#26a69a' if d else '#ef5350' for d in _up]
            fig.add_trace(go.Bar(x=_spy.index, y=_spy[_volc].astype(float), name='Volume',
                                 marker_color=_vcols), row=2, col=1)
        _sub = _dd[_dd['is_dd']]
        if not _sub.empty:
            fig.add_trace(go.Scatter(
                x=_sub.index, y=_sub['Low'] * 0.992,
                mode='markers', name='Distribution day',
                marker=dict(symbol='circle', size=9,
                            color=['#d33' if a else 'rgba(221,51,51,0.35)' for a in _sub['active_dd']],
                            line=dict(color='#8b0000' if _sub['active_dd'].any() else '#d33', width=0.6)),
                hoverinfo='text',
                text=[d.strftime('%Y-%m-%d') + (f'<br>Active' if a else '<br>expired') for d, a in zip(_sub.index, _sub['active_dd'])]
            ), row=1, col=1)
        _subf = _dd[_dd['ftd']] if 'ftd' in _dd else _dd.iloc[0:0]
        if not _subf.empty:
            fig.add_trace(go.Scatter(
                x=_subf.index, y=_subf['High'] * 1.015,
                mode='markers', name='Follow-through',
                marker=dict(symbol='triangle-up', size=9, color='#2f9e5f',
                            line=dict(color='#14532d', width=0.6)),
                hoverinfo='text',
                text=[d.strftime('%Y-%m-%d') + f" ({(r):+.1f}%)" for d, r in zip(_subf.index, _subf.get('ret', 0))],
            ), row=1, col=1)
        _suba = _dd[_dd['acc']] if 'acc' in _dd else _dd.iloc[0:0]
        if not _suba.empty:
            fig.add_trace(go.Scatter(
                x=_suba.index, y=_suba['High'] * 1.015,
                mode='markers', name='Accumulation',
                marker=dict(symbol='triangle-up', size=7, color='#1d6fd8',
                            line=dict(color='#0b3a75', width=0.6)),
                hoverinfo='text',
                text=[d.strftime('%Y-%m-%d') + f"<br>{r:+.1f}%" for d, r in zip(_suba.index, _suba.get('ret', 0))],
            ), row=1, col=1)
        fig.update_layout(
            height=470, xaxis_rangeslider_visible=False, hovermode='x unified',
            legend=dict(orientation='h', y=1.05, x=0),
annotations=[dict(x=1, y=1.02, xref='paper', yref='paper', xanchor='right', showarrow=False,
                              text=f'{_active_count} distribution days · {_status}',
                              font=dict(size=13, color=_regime_color)),
                         dict(x=0, y=1.02, xref='paper', xanchor='left', showarrow=False,
                              text=f'Power Trend: {"ON" if _pt_on else "OFF"} · recent high {_recent_hi:.0f}',
                              font=dict(size=11, color='#2f9e5f' if _pt_on else '#888'))])
        for _ax in ('xaxis', 'xaxis2'):
            fig.layout[_ax].update(type='date', rangebreaks=[dict(bounds=['sat', 'mon'])])
        fig.update_xaxes(rangeslider_visible=False, nticks=10, row=1, col=1)
        fig.update_xaxes(rangeslider_visible=False, dtick='M1', tickformat='%b %y', row=2, col=1)
        _y_pmin = float((_spy['Low'] * 0.992).min())
        _y_pmax = float((_spy['High'] * 1.015).max())
        _y_pad = (_y_pmax - _y_pmin) * 0.03
        fig.update_layout(yaxis=dict(range=[_y_pmin - _y_pad, _y_pmax + _y_pad]))
        if _volc is not None:
            _v_max = float(_spy['Volume'].astype(float).max())
            fig.update_layout(yaxis2=dict(range=[0, _v_max * 1.05]))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            f"**IBD distribution days (S&P/SPY):** a day that closes **≥0.2% lower on higher volume than the prior day**. "
            f"Active count over the last 25 sessions (expiring after a rally ≥5% above that day's close) = "
            f"**{_active_count}** → **{_status}**."
        )
        st.divider()

    st.divider()

    st.subheader("🎯 Market Pulse")
    rs_ok = RS_SER.dropna()
    n_total = len(snap)
    n_lead = int((RS_SER >= 80).sum()); n_str = int(((RS_SER >= 65) & (RS_SER < 80)).sum())
    n_broad = int(((RS_SER >= 50) & (RS_SER < 65)).sum()); n_lag = int((RS_SER < 50).sum())
    rs_std = float(RS_SER.std()) if len(RS_SER) else 0.0

    if has_historical and 'date' in filtered_df.columns:
        cream_dates = sorted(filtered_df['date'].dropna().unique())
        last_date = cream_dates[-1] if cream_dates else None
    else:
        last_date = None

    if not snap.empty:
        top_sector = snap.groupby('Sector')['Relative Strength'].mean().sort_values(ascending=False).head(1)
        bot_sector = snap.groupby('Sector')['Relative Strength'].mean().sort_values(ascending=True).head(1)
        ts = f"{top_sector.index[0]} ({top_sector.iloc[0]:.0f})" if not top_sector.empty else "n/a"
        bs = f"{bot_sector.index[0]} ({bot_sector.iloc[0]:.0f})" if not bot_sector.empty else "n/a"
    else:
        ts, bs = "n/a", "n/a"

    meta_cols = st.columns(4)
    with meta_cols[0]: st.metric("Total Stocks", n_total)
    with meta_cols[1]: st.metric("Avg RS", f"{RS_SER.mean():.1f}")
    with meta_cols[2]: st.metric("Breadth (≥80)", f"{n_lead}", help="% of universe with RS ≥ 80")
    with meta_cols[3]: st.metric("Median RS", f"{RS_SER.median():.1f}")

    if rs_ok is not None:
        bucket = RS_SER.apply(rs_bucket)
        bucket_counts = bucket.value_counts()
        bre_lead = float((RS_SER >= 80).mean() * 100) if len(RS_SER) else 0.0
        bre_str = float(((RS_SER >= 65) & (RS_SER < 80)).mean() * 100) if len(RS_SER) else 0.0
        bre_broad = float(((RS_SER >= 50) & (RS_SER < 65)).mean() * 100) if len(RS_SER) else 0.0
        bre_lag = float((RS_SER < 50).mean() * 100) if len(RS_SER) else 0.0

    st.caption(
        f"Latest ({last_date}): {n_lead} leaders (RS≥80), {n_str} strong, {n_broad} broad, {n_lag} laggards. "
        f"Strongest sector today: {ts}. Weakest: {bs}."
    )

    ma_breadth = {}
    for key, col in [('above200', 'Price vs 200-Day'), ('above50', 'Price vs 50-Day')]:
        if has_col(snap, col):
            _ser = pd.to_numeric(snap[col], errors='coerce')
            ma_breadth[key] = (_ser <= 0)
    if ma_breadth:
        bcols = st.columns(len(ma_breadth) + 1)
        j = 0
        for key, mask in ma_breadth.items():
            lbl = "Above 200-Day" if key == 'above200' else "Above 50-Day"
            with bcols[j]:
                st.metric(lbl, f"{float(mask.mean() * 100):.0f}%")
            j += 1
        if 'above200' in ma_breadth and 'above50' in ma_breadth:
            with bcols[j]:
                st.metric("Above Both MAs", f"{float((ma_breadth['above200'] & ma_breadth['above50']).mean() * 100):.0f}%")
            j += 1
        st.caption("Trend-breadth: % of the universe that has reclaimed its key moving averages — a rising reading confirms broad participation; a falling one signals narrowing leadership.")

    st.divider()
    st.subheader("📊 Sector Rotation Timeline (RS percentile, top sectors)")
    if has_historical and 'date' in filtered_df.columns:
        _daily = filtered_df.copy()
        _daily['date'] = pd.to_datetime(_daily['date'])
        _dsector = _daily.groupby(['date', 'Sector'])['Relative Strength'].mean().reset_index()
        _top_secs = _dsector.groupby('Sector')['Relative Strength'].mean().nlargest(8).index.tolist()
        _pivot = _dsector[_dsector['Sector'].isin(_top_secs)].pivot(index='date', columns='Sector', values='Relative Strength').sort_index()
        if not _pivot.empty:
            fig = go.Figure()
            for _s in _pivot.columns:
                fig.add_trace(go.Scatter(x=_pivot.index, y=_pivot[_s], mode='lines', name=_s))
            fig.update_layout(title="Avg RS by Leading Sector (zoomed)", hovermode='x unified', yaxis_title="RS Value")
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Sector rotation timeline needs historical data (with a 'date' column).")

    # ── Notable Strategies & Actionable Tickers ──
    st.divider()
    st.subheader("🎯 Notable Strategies & Actionable Tickers")

    # Load watchlist for actionable tickers
    WL_CSV = Path(__file__).resolve().parent / "python" / "backtests" / "unified_watchlist.csv"
    if WL_CSV.exists():
        wl_action = pd.read_csv(WL_CSV)
        golden = wl_action[(wl_action.get("combo_SMA50_Shakeout", pd.Series([False]*len(wl_action))) == True) & (wl_action["tf_flags_on"] >= 3)]

        ac1, ac2 = st.columns([1, 2])

        with ac1:
            st.markdown("**🏆 Top Verified Strategies**")
            top_strats = {
                "SMA50 Bounce+Shakeout × 2:1": "77.3% win, +6.66%, Sharpe 0.56",
                "PB+SMA50 Bounce × 5:1": "66.4% win, +4.92%, Sharpe 0.40",
                "SMA50 Bounce+Shakeout × 3:1": "74.8% win, +7.47%, Sharpe 0.54",
                "Shakeout+Upside Reversal × 2:1": "76.3% win, +5.86%, Sharpe 0.53",
            }
            for name, stats in top_strats.items():
                st.markdown(f"- **{name}**: {stats}")

            st.markdown("---")
            st.markdown("**🔑 Quality Filter**")
            st.markdown("depth ≤ 25% + len ≤ 150d → **86-100% win rate**")
            st.markdown("*From both-phase deep dive analysis*")

        with ac2:
            st.markdown(f"**🔥 Actionable Golden-Tier Tickers** ({len(golden)} total)")
            if len(golden) > 0:
                top5 = golden.head(5)
                for _, r in top5.iterrows():
                    reasons = []
                    reasons.append(f"{r['pattern']}, {r['depth']:.1f}% depth, {int(r['length'])}d")
                    if r.get("combo_SMA50_Shakeout", False): reasons.append("SMA50+Shakeout firing")
                    if r.get("combo_PB_SMA50", False): reasons.append("PB+SMA50 Bounce (both engines!)")
                    if r.get("near_52w_high", False): reasons.append("near 52W high")
                    if r.get("above_sma200", False): reasons.append("above SMA200")
                    reason_str = " | ".join(reasons)
                    st.markdown(f"- **{r['ticker']}** (comp {r['composite']:.1f}): {reason_str}")

                if len(golden) > 5:
                    st.caption(f"+ {len(golden)-5} more golden-tier tickers — see 🔍 Scans & Leads tab")
            else:
                st.info("No golden-tier tickers right now. Run unified_watchlist.py to refresh.")

            st.markdown("---")
            st.markdown("**💡 Strategy Insight**")
            st.markdown("- Mid-base signals (SMA50+Shakeout) + Breakout signals (PB+SMA50) = **two-phase edge**")
            st.markdown("- 42 golden-tier tickers today — dominated by Flat Base utilities/defensives")
            st.markdown("- KO is the only ticker with **both engines firing** right now")
    st.divider()

    # ---- Advance / Decline (tracked universe, daily) ----
    st.subheader("🛞 Advance / Decline — daily breadth (latest cached data)")
    if has_historical:
        try:
            ad = df.copy()
            ad['date'] = pd.to_datetime(ad['date'])
            ad = ad.sort_values(['Ticker', 'date'])
            _close_col = 'Close' if 'Close' in ad.columns else 'Price'
            _vol_col = 'Volume' if 'Volume' in ad.columns else None
            ad['prev_close'] = ad.groupby('Ticker')[_close_col].shift(1)
            ad = ad[ad['prev_close'].notna()].copy()
            ad['delta'] = ad[_close_col].astype(float) - ad['prev_close'].astype(float)
            ad['adv'] = (ad['delta'] > 1e-8).astype(int)
            ad['dcl'] = (ad['delta'] < -1e-8).astype(int)
            ad['unch'] = (1 - ad['adv'] - ad['dcl']).astype(int)
            if _vol_col is not None:
                _vol = ad[_vol_col].fillna(0).astype(float)
            else:
                _vol = 0.0
            ad['v_adv'] = _vol * ad['adv']
            ad['v_dcl'] = _vol * ad['dcl']
            ad['v_unch'] = _vol * ad['unch']
            g = ad.groupby('date').agg(adv=('adv', 'sum'), dcl=('dcl', 'sum'), unch=('unch', 'sum'),
                                       v_adv=('v_adv', 'sum'), v_dcl=('v_dcl', 'sum'), v_unch=('v_unch', 'sum')).sort_index()
            g['cum_adl'] = (g['adv'] - g['dcl']).cumsum()

            def _vfmt(val):
                val = float(val)
                if val >= 1e9: return f"{val/1e9:.2f}B"
                if val >= 1e6: return f"{val/1e6:.1f}M"
                if val >= 1e3: return f"{val/1e3:.0f}K"
                return f"{val:.0f}"

            if len(g):
                _last = g.iloc[-1]
                _last_date = _last.name.date() if hasattr(_last.name, 'date') else _last.name
                st.info(
                    f"**Latest cached day ({_last_date}): {int(_last['adv'])}** stocks up on "
                    f"**{_vfmt(_last['v_adv'])}** volume, "
                    f"**{int(_last['dcl'])}** stocks down on **{_vfmt(_last['v_dcl'])}** volume, "
                    f"**{int(_last['unch'])}** unchanged on **{_vfmt(_last['v_unch'])}** volume.")
                _c1, _c2 = st.columns(2)
                with _c1:
                    fig = px.line(g, x=g.index, y='cum_adl', title='Cumulative Advance/Decline Line (all cached data)')
                    fig.update_layout(height=360, hovermode='x unified')
                    st.plotly_chart(fig, use_container_width=True)
                with _c2:
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=g.index, y=g['adv'], name='Advances', marker_color='#2ca02c'))
                    fig.add_trace(go.Bar(x=g.index, y=g['dcl'], name='Declines', marker_color='#d62728'))
                    fig.update_layout(barmode='group', height=360, hovermode='x unified', title='Daily Advances vs Declines')
                    st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No advance/decline data in the selected date range.")
        except Exception as _e:
            st.warning(f"Advance/Decline computation failed: {_e}")
    else:
        st.info("Advance/Decline breadth requires historical data (with 'date' column).")
    st.divider()

    st.caption("Overview ratings reflect the latest recorded snapshot. Breadth band = RS≥80 (lead), 65–79 (strong), "
               "50–64 (broad), <50 (laggard). A/D uses the app's tracked universe as a proxy for NYSE.")

# ---------- TAB 2: Top Performers ----------
with tab2:
    st.subheader("🌟 Composite Leaders (momentum + quality)")
    lw = snap.copy()
    for c in ['Relative Strength', 'Percentile', '6M_RS_Percentile', '3M_RS_Percentile', '1M_RS_Percentile',
              'RevenueGrowth', 'PctFrom52WkHigh', 'Avg EPS % Chg 4Q', 'ROE', 'MarketCap', 'ShortFloatPct']:
        if has_col(lw, c):
            lw[c] = pd.to_numeric(lw[c], errors='coerce')
    lw['LeaderScore'] = lw.apply(leader_score, axis=1)
    lw['RS ≥ 70'] = (lw['Relative Strength'] >= 70).astype(int)
    leader_cols = [c for c in ['Ticker', 'LeaderScore', 'Relative Strength', 'Percentile', '6M_RS_Percentile',
                               '3M_RS_Percentile', '1M_RS_Percentile', 'RevenueGrowth', 'PctFrom52WkHigh'] if has_col(lw, c)]
    if not lw.empty:
        top_leader = lw.sort_values('LeaderScore', ascending=False).head(15)[leader_cols]
        st.dataframe(_int_ratings(top_leader).reset_index(drop=True), use_container_width=True, hide_index=True)
    else:
        st.info("No leader data available.")
    st.caption("Composite blends RS percentile, 6M/3M momentum, revenue growth, near-52-wk-high proximity and profitability.")

    st.divider()
    st.subheader("🚀 Accelerating Leaders (6M-RS building > 1M-RS)")
    if has_col(snap, '6M_RS_Percentile') and has_col(snap, '1M_RS_Percentile'):
        acc = snap.copy()
        for c in ['1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile', 'Relative Strength']:
            if has_col(acc, c):
                acc[c] = pd.to_numeric(acc[c], errors='coerce')
        acc['Accelerating'] = (acc['6M_RS_Percentile'] >= acc['3M_RS_Percentile']) & (acc['3M_RS_Percentile'] >= acc['1M_RS_Percentile'])
        acc_f = acc[acc['Accelerating']].sort_values('6M_RS_Percentile', ascending=False).head(15)
        if not acc_f.empty:
            m_cols = [c for c in ['Ticker', 'Sector', 'Relative Strength', '1M_RS_Percentile', '3M_RS_Percentile', '6M_RS_Percentile'] if has_col(acc, c)]
            st.dataframe(_int_ratings(acc_f[m_cols].rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})).reset_index(drop=True),
                         use_container_width=True, hide_index=True)
            st.caption("Stocks whose RS percentile keeps climbing 1M → 3M → 6M: momentum is building rather than fading.")
        else:
            st.info("No stocks currently show clean accelerating RS momentum in this snapshot.")
        st.divider()
        rs_num = _n(snap, 'Relative Strength')
        top_rs = snap.assign(RSn=rs_num).nlargest(15, 'RSn')
        show = ['Rank', 'Ticker', 'Sector', 'Relative Strength', 'Percentile'] if has_col(snap, 'Rank') else ['Ticker', 'Sector', 'Relative Strength', 'Percentile']
        top_rs = top_rs[[c for c in show + [PRICE_COL] if has_col(top_rs, c)]].drop(columns='RSn', errors='ignore')
        st.dataframe(_int_ratings(top_rs).reset_index(drop=True), use_container_width=True, hide_index=True)
    st.divider()

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🏆 Top 15 by Relative Strength")
        rs_num = _n(snap, 'Relative Strength')
        top_rs = snap.assign(RSn=rs_num).nlargest(15, 'RSn')
        show = ['Rank', 'Ticker', 'Sector', 'Relative Strength', 'Percentile'] if has_col(snap, 'Rank') else ['Ticker', 'Sector', 'Relative Strength', 'Percentile']
        top_rs = top_rs[[c for c in show + [PRICE_COL] if has_col(top_rs, c)]].drop(columns='RSn', errors='ignore')
        st.dataframe(_int_ratings(top_rs).reset_index(drop=True), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("⭐ Top 15 by 6-Month Momentum")
        if has_col(snap, '6M_RS_Percentile'):
            top_6m = snap.assign(tmp=_n(snap, '6M_RS_Percentile')).nlargest(15, 'tmp').drop(columns=['tmp'])[['Ticker', '6M_RS_Percentile', '3M_RS_Percentile', '1M_RS_Percentile']].copy()
            top_6m = top_6m.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
            st.dataframe(_int_ratings(top_6m).reset_index(drop=True), use_container_width=True, hide_index=True)

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🚀 Top 15 by Earnings Growth (EPS)")
        if has_col(snap, 'Avg EPS % Chg 4Q'):
            top_eps = snap.assign(tmp=_n(snap, 'Avg EPS % Chg 4Q')).nlargest(15, 'tmp').drop(columns=['tmp'])
            eps_cols = [c for c in ['Ticker', 'Sector', 'Avg EPS % Chg 4Q', 'ROE', 'RevenueGrowth'] if has_col(top_eps, c)]
            st.dataframe(top_eps[eps_cols].reset_index(drop=True), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("💰 Top 15 by Market Cap")
        if has_col(snap, 'MarketCap'):
            mcol = _n(snap, 'MarketCap')
            top_mcap = snap.assign(tmp=mcol).nlargest(15, 'tmp')[['Ticker', 'Sector', 'MarketCap', 'Relative Strength', 'Percentile']].copy()
            top_mcap['MarketCap'] = top_mcap['MarketCap'].apply(lambda x: f"${x/1e9:.2f}B" if pd.notna(x) else "N/A")
            st.dataframe(top_mcap.reset_index(drop=True), use_container_width=True, hide_index=True)

# ---------- TAB 3: Trends ----------
if has_historical:
    with tab3:
        st.subheader("📉 Trend Analysis (rotation & momentum)")
        daily = filtered_df.copy()
        daily['date'] = pd.to_datetime(daily['date'])
        latest_date = daily['date'].max()
        oldest_date = daily['date'].min()
        N = daily['date'].nunique()
        window = max(10, N // 4) if N else 10

        sec_over = daily.groupby(['date', 'Sector'])['Relative Strength'].mean().reset_index()
        piv = sec_over.pivot(index='date', columns='Sector', values='Relative Strength').sort_index()
        latest_sec = piv.iloc[-1]
        if len(piv) >= 2 and window < len(piv):
            old_sec = piv.iloc[-(window + 1)]
        else:
            old_sec = piv.iloc[0]
        momentum = (latest_sec - old_sec).dropna().sort_values(ascending=False)

        col1, col2 = st.columns(2)
        with col1:
            fig = px.bar(x=momentum.values, y=momentum.index, orientation='h',
                         title=f"Sector RS Change (last {min(window, len(piv)-1 if len(piv)>1 else 1)} sessions)",
                         color=momentum.values, color_continuous_scale='RdYlGn')
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            daily_breadth = daily.groupby('date')['Relative Strength'].apply(lambda s: float((s >= 80).mean() * 100)).reset_index()
            daily_breadth.columns = ['date', 'lead']
            daily_breadth['lead60'] = daily_breadth['lead'].rolling(60).mean()
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=daily_breadth['date'], y=daily_breadth['lead'], name='% leaders ≥80', mode='lines'))
            fig.add_trace(go.Scatter(x=daily_breadth['date'], y=daily_breadth['lead60'], name='60-day avg', mode='lines', line=dict(dash='dash')))
            fig.update_layout(title="Leadership Breadth over time", hovermode='x unified')
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("🔄 Sector Rotation Heatmap (RS rank over time)")
        top_s = sec_over.groupby('Sector')['Relative Strength'].mean().nlargest(10).index.tolist()
        hm = sec_over[sec_over['Sector'].isin(top_s)].pivot(index='date', columns='Sector', values='Relative Strength').sort_index()
        if not hm.empty:
            fig = go.Figure(data=[go.Heatmap(z=hm.T.values,
                                            x=hm.index,
                                            y=hm.columns,
                                            colorscale='RdYlGn')])
            fig.update_layout(title="Sector RS Heatmap (rows = 10 leading sectors, cols = date)", xaxis_title="Date")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("🚀 Top Individual RS Gainers (window)")
            latest_stocks = daily[daily['date'] == latest_date][['Ticker', 'Sector', 'Relative Strength']].drop_duplicates(subset='Ticker').copy()
            # use the start of the window instead
            window_start = piv.index[-min(window, len(piv))] if len(piv) else oldest_date
            old_stocks = daily[daily['date'] == window_start][['Ticker', 'Relative Strength']].drop_duplicates(subset='Ticker').copy()
            merged = latest_stocks.merge(old_stocks, on='Ticker', suffixes=('_latest', '_oldest'))
            merged['RS_change'] = merged['Relative Strength_latest'] - merged['Relative Strength_oldest']
            merged.columns = [c.replace(' ', '_') for c in merged.columns]
            top_gainers = merged.nlargest(12, 'RS_change')[['Ticker', 'Sector', 'RS_change']]
            fig = px.bar(top_gainers, x='RS_change', y='Ticker', orientation='h',
                         title="Biggest RS Improvers", color='RS_change', color_continuous_scale='Greens')
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            st.subheader("📉 Most Falling RS (window)")
            if 'RS_change' in merged.columns:
                top_losers = merged.nsmallest(12, 'RS_change')[['Ticker', 'Sector', 'RS_change']]
                fig = px.bar(top_losers, x='RS_change', y='Ticker', orientation='h',
                             title="Biggest RS Drops", color='RS_change', color_continuous_scale='Reds')
                st.plotly_chart(fig, use_container_width=True)

        st.markdown(
            f"**Interpretation:** over the last **{min(window, len(piv))} trades**, the leading sectors are "
            f"**{', '.join(momentum.head(3).index)}**; lagging are **{', '.join(momentum.tail(3).index)}**. "
            f"Watch for whether leadership breadth (chart above) is expanding or contracting to gauge market "
            f"tone."
        )

# ---------- TAB 4: Industry Rotation (corrected delta calculations) ----------
with tab4:
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

@st.cache_data(ttl=28800, show_spinner=False)
def load_ticker_daily_cached(ticker: str):
    """Cache-load a ticker's 2y daily frame so switching tickers doesn't hit the
    parquet (or yfinance) on every rerun."""
    ticker = (ticker or "").strip().upper()
    df = load_or_fetch_ticker(ticker, interval="1d", period="2y")
    if df is not None and not df.empty and getattr(df.index, 'tz', None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df

@st.cache_data(ttl=28800, show_spinner=False)
def load_spx_index_daily():
    """S&P 500 index history for RS signals — network-fetched at most once per TTL.
    The per-ticker chart used to hit yfinance for ^GSPC on every single rerun."""
    df = load_or_fetch_ticker("^GSPC", interval="1d", period="2y")
    if df is not None and not df.empty and getattr(df.index, 'tz', None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df

@st.cache_data(ttl=28800, show_spinner=False)
def load_spx_index_weekly():
    """Weekly S&P 500 index history for the Company Details weekly RS pane."""
    df = load_or_fetch_ticker("^GSPC", interval="1wk", period="3y")
    if df is not None and not df.empty and getattr(df.index, 'tz', None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df

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
@st.cache_data(ttl=3600, show_spinner=False)
def build_ticker_price_chart(ticker, percentile=None, height=800):
    """
    Build the same 'Plotly (Advanced)' daily OHLC-bar + pattern + volume + RS chart used in
    the Company Details tab, for an arbitrary ticker. Self-contained (no markers/weekly/chart-type
    UI) so it can be reused from other tabs (e.g. IBD Live) without touching Company Details' logic.

    Cached per (ticker, percentile, height) — the figure only changes when the price data
    changes, so clicking Next/Prev Ticker is instant once a ticker has been built.

    Returns (fig, error_message). fig is None if data could not be fetched/plotted.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None, "No ticker specified."
    try:
        df_daily_full = load_ticker_daily_cached(ticker)
        if df_daily_full.empty:
            return None, f"No daily data available for {ticker}. Please check the ticker symbol."
        df_daily = df_daily_full.iloc[-504:] if len(df_daily_full) > 504 else df_daily_full

        try:
            spy_daily_full = load_spx_index_daily()
            spy_daily = spy_daily_full.iloc[-504:] if len(spy_daily_full) > 504 else spy_daily_full
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

        percentile = float(percentile) if percentile is not None and not pd.isna(percentile) else None

        snapshot_text = f"<b>{ticker}</b>"
        if percentile is not None and not pd.isna(percentile):
            snapshot_text += f"<br>Pctl: {int(round(float(percentile)))}"

        ema10 = df_daily['Close'].ewm(span=10, adjust=False).mean()
        ema21 = df_daily['Close'].ewm(span=21, adjust=False).mean()
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                            subplot_titles=(f"{ticker} Daily", 'Volume with Indicators', 'Raw RS & QGDRS EMAs'),
                            row_heights=[0.5, 0.2, 0.3])
        for trace in make_ibd_ohlc_traces(
            df_daily.index, df_daily['Open'], df_daily['High'], df_daily['Low'], df_daily['Close'],
            name='Price', showlegend=False,
        ):
            fig.add_trace(trace, row=1, col=1)
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
# TAB 5: Company Details — with its own ticker filter (search + selectbox)
# ========================================================================================
with tab5:
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

            # Fall back to the ticker cache for OHLCV if the snapshot row is missing them
            def _is_missing(v):
                try:
                    return v is None or pd.isna(v) or (isinstance(v, str) and not v.strip())
                except Exception:
                    return False
            if _is_missing(latest_row.get('Close')):
                try:
                    _cache_df = load_or_fetch_ticker(selected_ticker_company, interval="1d", period="2y")
                    if not _cache_df.empty:
                        _cache_last = _cache_df.iloc[-1]
                        latest_row = latest_row.copy()
                        for _c in ['Open', 'High', 'Low', 'Close', 'Volume']:
                            if _c in _cache_last.index and _is_missing(latest_row.get(_c)):
                                latest_row[_c] = _cache_last[_c]
                except Exception:
                    pass

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
                            df_daily = df_daily_full.iloc[-504:] if len(df_daily_full) > 504 else df_daily_full

                            try:
                                spy_daily_full = load_spx_index_daily()
                                if spy_daily_full.empty:
                                    st.warning("Unable to fetch S&P 500 data. RS calculations may be affected.")
                                    spy_daily = pd.DataFrame()
                                else:
                                    spy_daily = spy_daily_full.iloc[-504:] if len(spy_daily_full) > 504 else spy_daily_full
                            except Exception as e:
                                st.warning(f"Error fetching SPY data: {e}")
                                spy_daily = pd.DataFrame()
                            spy_weekly = load_spx_index_weekly()
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

                                st_html(create_lightweight_ohlc_html(
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
                                for trace in make_ibd_ohlc_traces(
                                    df.index, df['Open'], df['High'], df['Low'], df['Close'],
                                    name='Price', showlegend=False,
                                ):
                                    fig.add_trace(trace, row=1, col=1)
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

                                st_html(create_lightweight_ohlc_html(
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
                                for trace in make_ibd_ohlc_traces(
                                    df_weekly.index, df_weekly['Open'], df_weekly['High'],
                                    df_weekly['Low'], df_weekly['Close'],
                                    name='Price', showlegend=False,
                                ):
                                    fig_w.add_trace(trace, row=1, col=1)
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

                                st_html(create_lightweight_ohlc_html(
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

# ---------- TAB 6: Data Table ----------
with tab6:
    if has_historical:
        latest_date = df['date'].max()
        table_df    = df[df['date'] == latest_date].copy()
        st.caption(f"Showing data for the latest date: **{latest_date.date()}**")
    else:
        table_df = filtered_df.copy()
    table_df = table_df.rename(columns={'1M_RS_Percentile': '1M', '3M_RS_Percentile': '3M', '6M_RS_Percentile': '6M'})
    table_df = _int_ratings(table_df)
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

# ---------- TAB 7: Pattern Finder ----------
with tab7:
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
            
            # Load IBD Data Tables full map for Composite Rating lookup & filtering,
            # plus the CALCULATED group rank/RS map from the daily screener (these
            # replace IBD's letter-grade Ind Grp RS and Industry Group Rank).
            ibd_full_map = load_ibd_data_tables_full()
            calc_group_map = load_calculated_group_map()
            
            # Expander for IBD Column Filters
            with st.expander("🎛️ Filter Patterns by IBD Data Columns", expanded=False):
                f_col1, f_col2, f_col3, f_col4 = st.columns(4)
                with f_col1:
                    pf_min_comp = st.slider("Min IBD Comp Rating", 0, 99, 0, key="pf_min_comp")
                    pf_min_eps  = st.slider("Min EPS Rating", 0, 99, 0, key="pf_min_eps")
                    pf_min_rs   = st.slider("Min RS Rating", 0, 99, 0, key="pf_min_rs")
                with f_col2:
                    pf_max_ind_rank = st.slider("Max Industry Group Rank (Calc)", 1, 200, 200, key="pf_max_ind_rank")
                    pf_min_ind_rs   = st.slider("Min Ind Group RS (Calc)", 1, 99, 1, key="pf_min_ind_rs")
                    pf_min_vol_chg  = st.number_input("Min Vol % Change", value=-999.0, key="pf_min_vol_chg")
                with f_col3:
                    pf_min_price_chg = st.number_input("Min Price % Change", value=-999.0, key="pf_min_price_chg")
                    pf_min_lq_eps   = st.number_input("Min Last Qtr EPS % Chg", value=-999.0, key="pf_min_lq_eps")
                    pf_min_lq_sales = st.number_input("Min Last Qtr Sales % Chg", value=-999.0, key="pf_min_lq_sales")
                with f_col4:
                    pf_min_cy_eps   = st.number_input("Min Curr Yr EPS Est % Chg", value=-999.0, key="pf_min_cy_eps")
                    pf_acc_dis = st.multiselect("Acc/Dis Rating", ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"], default=[], key="pf_acc_dis")
                    pf_smr     = st.multiselect("SMR Rating", ["A", "B", "C", "D", "E"], default=[], key="pf_smr")

            def passes_ibd_filter(t_sym):
                if not isinstance(ibd_full_map, dict) and not calc_group_map:
                    return True
                t_sym_up = t_sym.strip().upper()
                t_sym_norm = t_sym_up.replace(".", "").replace("-", "").replace("/", "").replace(" ", "")
                # Calculated live group rank/RS from the daily screener, falling back
                # to IBD Data Tables only when the ticker is absent from the snapshot.
                t_calc = (calc_group_map.get(t_sym_up) or calc_group_map.get(t_sym_norm) or {}) if calc_group_map else {}
                t_info = ibd_full_map.get(t_sym, {}) if isinstance(ibd_full_map, dict) else {}
                
                # Only enforce the group rank/RS filters when the ticker actually
                # has group data; tickers absent from both maps pass the group
                # filters (other filters below still apply).
                if t_calc or t_info:
                    ind_rank = t_calc.get('Ind Group Rank')
                    if ind_rank is None:
                        ind_rank = t_info.get('Industry Group Rank', 999)
                        if pd.isna(ind_rank):
                            ind_rank = 999
                    if ind_rank > pf_max_ind_rank: return False
                    
                    ind_rs = t_calc.get('Ind Group RS')
                    if ind_rs is None:
                        # no calculated value -> letter grade from IBD as last resort
                        gr = str(t_info.get('Ind Grp RS', '')).strip()
                        ind_rs = _grade_to_num(gr) if gr else None
                    if ind_rs is not None and ind_rs < pf_min_ind_rs: return False
                
                if not t_info:
                    if pf_min_comp > 0 or pf_min_eps > 0 or pf_min_rs > 0 or pf_acc_dis or pf_smr:
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

                return True

            # Process patterns and sort tickers by CALCULATED Industry Group Rank
            # (asc) then IBD Comp Rating (desc)
            processed_patterns = []
            total_headers = 0
            
            for p_name, tickers in pattern_data.items():
                if tickers:
                    filtered_tickers = [t for t in tickers if passes_ibd_filter(t)]
                    if filtered_tickers:
                        section_title = p_name.replace('-', ' ').title()
                        scored_tickers = []
                        for t in filtered_tickers:
                            t_up = t.strip().upper()
                            t_norm = t_up.replace(".", "").replace("-", "").replace("/", "").replace(" ", "")
                            t_calc = (calc_group_map.get(t_up) or calc_group_map.get(t_norm) or {}) if calc_group_map else {}
                            t_info = ibd_full_map.get(t, {}) if isinstance(ibd_full_map, dict) else {}
                            comp_val = t_info.get('IBD Comp. Rating', 0)
                            if pd.isna(comp_val):
                                comp_val = 0
                            ind_rank = t_calc.get('Ind Group Rank')
                            if ind_rank is None:
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
@st.cache_data(ttl=1800, show_spinner=False)
def _ibd_chart_payload(ticker: str, bars: int = 300):
    """Bars + a freshly-scanned result for ONE ticker, for the pattern chart.

    The pattern shapes need the scanner's per-bar `history` (bTop/bLow/inBase/isCupH), which
    is deliberately not written to ibd_pattern_results.json - it was 99.8% of an 8.9 GB file.
    Rescanning the single selected ticker costs ~15 ms, so the chart pays that instead of
    every consumer of the results carrying history for all 6,000 signals.
    """
    import importlib.util
    root = Path(__file__).resolve().parent
    fp = root / "ticker_cache" / f"{str(ticker).strip().replace('.', '-')}_1d.parquet"
    if not fp.exists():
        return None, None
    spec = importlib.util.spec_from_file_location(
        "_ibd_scanner_chart", root / "python" / "ibd_pattern_scanner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    res = mod.scan_single_ticker(ticker, str(fp))
    if not res:
        return None, None
    return pd.read_parquet(fp).sort_index(), res


@st.cache_data(ttl=3600, show_spinner=False)
def _trend_table(mtime: float):
    """The whole universe's SMA50 / SMA200 / 50-day volume, precomputed by the TV scan.

    `tv_pattern_scanner.py` already opens every parquet in ticker_cache, so it writes these
    three numbers out to `python/trend_metrics.parquet` on the way past. One 7k-row read
    instead of 7k reads.
    """
    fp = Path(__file__).resolve().parent / "python" / "trend_metrics.parquet"
    if not fp.exists():
        return None
    try:
        return pd.read_parquet(fp).set_index("ticker")
    except Exception:
        return None


def _trend_from_cache(tickers: tuple):
    """Fallback: derive the metrics by opening one parquet per ticker, as this used to."""
    cache_dir = Path(__file__).resolve().parent / "ticker_cache"
    rows = []
    for t in tickers:
        fp = cache_dir / f"{str(t).strip().replace('.', '-')}_1d.parquet"
        if not fp.exists():
            continue
        try:
            d = pd.read_parquet(fp, columns=["Close", "Volume"]).sort_index()
        except Exception:
            continue
        if len(d) < 50:
            continue
        c = pd.to_numeric(d["Close"], errors="coerce")
        v = pd.to_numeric(d["Volume"], errors="coerce")
        # min_periods keeps a young listing from silently scoring NaN and being filtered out
        # for the wrong reason; sma200 is simply absent when there is not enough history.
        s50 = c.rolling(50, min_periods=50).mean().iloc[-1]
        s200 = c.rolling(200, min_periods=200).mean().iloc[-1] if len(d) >= 200 else np.nan
        av50 = v.rolling(50, min_periods=25).mean().iloc[-1]
        rows.append({"ticker": str(t), "sma50": s50, "sma200": s200, "avg_vol50": av50})
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600, show_spinner=False)
def _ibd_trend_metrics(tickers: tuple):
    """SMA50 / SMA200 / 50-day average volume per ticker.

    The pattern scanner computes sma50 internally but never emits it, and has no sma200 or
    50-day volume at all, so the trend filters below have nothing to read. Deriving them here
    keeps the promoted production scanner untouched — the alternative was adding three fields
    to a live file, which is not something to do as a side effect of a dashboard filter.

    Served from the precomputed table when it is there. It used to open one parquet per ticker
    on every cold load, and with two tabs asking for it that was ~8,200 files and 19 of the
    ~24 seconds the app took to appear. Anything the table does not cover still falls back to
    the per-ticker read, so a ticker added since the last scan is missing numbers, never wrong
    ones — and the fallback computes them identically.
    """
    fp = Path(__file__).resolve().parent / "python" / "trend_metrics.parquet"
    tbl = _trend_table(fp.stat().st_mtime) if fp.exists() else None
    if tbl is None:
        return _trend_from_cache(tickers)
    want = [str(t) for t in tickers]
    have = tbl.reindex([t for t in want if t in tbl.index]).reset_index()
    missing = tuple(t for t in want if t not in tbl.index)
    if missing:
        extra = _trend_from_cache(missing)
        if not extra.empty:
            have = pd.concat([have, extra], ignore_index=True)
    return have.dropna(subset=["sma50"], how="all") if not have.empty else have


# --- Shared column formatting for the IBD pattern tables --------------------------------
# Both the per-pattern mini tables and the detailed table render through this, so a column
# means the same thing and is formatted the same way in both rather than drifting apart.
#
# The six sub-signals are what the two scores are BUILT from - before_bo_score is how many of
# them fired while in the base and within 15% of the pivot, post_bo_score how many fired in
# the 15 bars after the breakout - so showing the individual ticks next to the score says
# WHICH ones, not just how many.
IBD_SIGNAL_COLS = {
    'vol_dry_up': 'VDU', 'pocket_pivot': 'PP', 'touched_ma': 'MA Touch',
    'shakeout_entry': 'Shakeout', 'upside_reversal': 'UpRev', 'rs_nh': 'RS NH',
    'is_double_bottom': 'Dbl Btm',
}
# Percentages to one decimal. Rounded rather than string-formatted so the columns stay
# numeric and the table still sorts by value.
IBD_ROUND1_COLS = ('dist_pct', 'pct_off_52w_high', 'conservative_dist_pct')
IBD_RENAME = {
    'ticker': 'Ticker', 'pattern_name': 'Pattern', 'status': 'Status',
    'Percentile': 'RS Rating', 'close': 'Close ($)', 'AvgVol30': '30D Avg Vol',
    'pivot': 'Buy Point', 'dist_pct': 'Pivot Dist %',
    'pct_off_52w_high': '% Off 52W High', 'composite_score': 'Comp Score',
    'before_bo_score': 'Pre Score', 'post_bo_score': 'Post Score',
    'rs_nh_count': 'RS NH Count', 'days_in_base': 'Days in Base',
    'bars_sbo': 'Bars Post-BO', **IBD_SIGNAL_COLS,
}


def _fmt_ibd_table(df, cols):
    """Select `cols` that exist, round the percentages, tick the booleans, rename."""
    cols = [c for c in cols if c in df.columns]
    out = df[cols].copy()
    for c in IBD_ROUND1_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors='coerce').round(1)
    if 'pivot' in out.columns:
        out['pivot'] = pd.to_numeric(out['pivot'], errors='coerce').round(2)
    for c in IBD_SIGNAL_COLS:
        if c in out.columns:
            # '·' rather than blank for a miss, so an empty cell reads as "signal absent"
            # instead of "column not populated".
            out[c] = out[c].fillna(False).astype(bool).map({True: '✓', False: '·'})
    return out.rename(columns=IBD_RENAME)


# Column order shared by both tables. The mini table drops Sector/Industry for width.
IBD_TABLE_COLS = ['ticker', 'pattern_name', 'status', 'Percentile', 'close', 'AvgVol30',
                  'pivot', 'dist_pct', 'pct_off_52w_high',
                  'before_bo_score', 'post_bo_score', 'composite_score',
                  'vol_dry_up', 'pocket_pivot', 'touched_ma', 'shakeout_entry',
                  'upside_reversal', 'rs_nh', 'is_double_bottom',
                  'rs_nh_count', 'days_in_base', 'bars_sbo']


with tab_ibd_pattern:
    st.subheader("🏆 IBD Pattern Scanner")
    st.markdown("Automated MarketSmith / IBD pattern scanner logic (`drw_pattern_scanner.pine`). "
                "Categorizes active bases into four patterns — **Cup+Handle, Cup, Flat Base, Consolidation** — "
                "from `ticker_cache` data. **Double Bottom** is detected separately and can occur inside any "
                "of them, so it has its own category that overlaps the four (and its own Sub-signals filter). "
                "High Tight Flag is reported as enclosing context.")
    
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
            # A truncated results file used to surface as a raw "Expecting value: line 48
            # column 22" and take the whole tab down. It happens when a scanner run is
            # interrupted mid-write, which leaves valid JSON up to the cut and nothing after.
            # Say that plainly instead, because the fix is simply to rerun the scan.
            try:
                with open(ibd_json_path, 'r', encoding='utf-8') as f:
                    ibd_results = json.load(f)
            except json.JSONDecodeError as je:
                sz = ibd_json_path.stat().st_size
                mt = datetime.fromtimestamp(ibd_json_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
                st.error(
                    f"The IBD pattern results file is incomplete and could not be read "
                    f"(JSON ends at line {je.lineno}). It is {sz:,} bytes, last written "
                    f"{mt} — a scan was almost certainly interrupted part-way through saving."
                )
                st.info("Press **🔄 Run / Rerun IBD Pattern Scanner** above to regenerate it.")
                st.stop()

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
                
                # Trend/liquidity metrics the scanner does not emit (see _ibd_trend_metrics).
                try:
                    _tm = _ibd_trend_metrics(tuple(sorted(df_ibd_patterns['ticker'].astype(str).unique())))
                    if not _tm.empty:
                        df_ibd_patterns = df_ibd_patterns.merge(_tm, on='ticker', how='left')
                except Exception as _e:
                    st.caption(f"Trend metrics unavailable ({_e}); trend filters disabled.")

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
                        # Defaults are a liquidity/trend floor, not zero: sub-$12 or thin names
                        # produce patterns that are real on the chart but untradeable, and a
                        # base below the 200-day is a downtrend pause rather than a setup.
                        min_price     = st.number_input("Min Price ($)", value=12.0, step=1.0, key="ibd_min_price")
                        max_price     = st.number_input("Max Price ($)", value=10000.0, step=10.0, key="ibd_max_price")
                        min_avg_vol50 = st.number_input("Min 50D Avg Vol", value=400000, step=50000, key="ibd_min_vol50")
                        min_avg_vol30 = st.number_input("Min 30D Avg Vol", value=0, step=50000, key="ibd_min_vol30")
                    with f_col3:
                        min_comp_score = st.slider("Min Composite Score (0-12)", 0, 12, 0, key="ibd_min_comp")
                        min_pre_score  = st.slider("Min Before-BO Score (0-6)", 0, 6, 0, key="ibd_min_pre")
                        min_post_score = st.slider("Min Post-BO Score (0-6)", 0, 6, 0, key="ibd_min_post")
                    with f_col4:
                        max_dist_pivot = st.number_input("Max Distance to Pivot %", value=100.0, key="ibd_max_dist")
                        max_off_52w    = st.number_input("Max % Off 52W High", value=100.0, key="ibd_max_52w")
                        sub_filter     = st.multiselect("Require Sub-signals", ["Volume Dry-Up", "Pocket Pivot", "Touched MA", "Shakeout Entry", "Upside Reversal", "RS New High", "Double Bottom"], default=[], key="ibd_sub_sig")
                        _has_trend = {'sma50', 'sma200'} <= set(df_ibd_patterns.columns)
                        req_above_200 = st.checkbox("Price > 200D SMA", value=True,
                                                    disabled=not _has_trend, key="ibd_above_200")
                        req_50_over_200 = st.checkbox("50D SMA > 200D SMA", value=True,
                                                      disabled=not _has_trend, key="ibd_50o200")
                        if not _has_trend:
                            st.caption("Trend data unavailable — rerun with ticker_cache present.")

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
                if 'avg_vol50' in filtered_ibd.columns and min_avg_vol50 > 0:
                    filtered_ibd = filtered_ibd[filtered_ibd['avg_vol50'].fillna(0) >= min_avg_vol50]

                # Trend gates. NaN means the ticker has under 200 bars of history, so the test
                # cannot be evaluated rather than being failed - dropping those silently would
                # quietly exclude every recent IPO, which is not what "above the 200-day"
                # means. They are excluded here because the filter is opt-in and checked by
                # default; unchecking it brings them back.
                if req_above_200 and {'sma200'} <= set(filtered_ibd.columns):
                    filtered_ibd = filtered_ibd[
                        filtered_ibd['sma200'].notna() &
                        (filtered_ibd['close'] > filtered_ibd['sma200'])]
                if req_50_over_200 and {'sma50', 'sma200'} <= set(filtered_ibd.columns):
                    filtered_ibd = filtered_ibd[
                        filtered_ibd['sma50'].notna() & filtered_ibd['sma200'].notna() &
                        (filtered_ibd['sma50'] > filtered_ibd['sma200'])]


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
                # Double Bottom is a sub-pattern, so it filters here rather than appearing as
                # its own bucket. `.get` guards a results file written before the field existed.
                if "Double Bottom" in sub_filter and 'is_double_bottom' in filtered_ibd.columns:
                    filtered_ibd = filtered_ibd[filtered_ibd['is_double_bottom'].fillna(False)]

                # Output Views: Tabs for "Category View", "Data Table", and "Watchlist Export"
                sub_chart, sub_tab1, sub_tab2, sub_tab3 = st.tabs(
                    ["📈 Chart", "📂 Tickers by Pattern Category", "📋 Detailed Data Table", "📤 Export Watchlists"])

                # --- Sub-tab: pattern chart ---
                with sub_chart:
                    if filtered_ibd.empty:
                        st.info("No tickers match the current filters.")
                    else:
                        ch_c1, ch_c2, ch_c3 = st.columns([3, 1, 1])
                        _opts = filtered_ibd.sort_values('composite_score', ascending=False)['ticker'].tolist()
                        _pre = search_ticker if search_ticker in _opts else _opts[0]
                        with ch_c1:
                            ch_tkr = st.selectbox("Ticker", _opts, index=_opts.index(_pre),
                                                  key="ibd_chart_tkr")
                        with ch_c2:
                            ch_bars = st.select_slider("Bars", [120, 200, 300, 450, 700],
                                                       value=300, key="ibd_chart_bars")
                        with ch_c3:
                            st.caption("Shapes follow pine/drw_pattern.pine")
                        try:
                            _cdf, _cres = _ibd_chart_payload(ch_tkr, ch_bars)
                            if _cdf is None or _cres is None:
                                st.warning(f"No cached price data for {ch_tkr}.")
                            else:
                                sys.path.insert(0, str(Path(__file__).resolve().parent / "python"))
                                from pattern_chart import build_pattern_figure
                                st.plotly_chart(
                                    build_pattern_figure(ch_tkr, _cdf, _cres, bars=ch_bars),
                                    use_container_width=True)
                                _r = filtered_ibd[filtered_ibd['ticker'] == ch_tkr]
                                if not _r.empty:
                                    _r = _r.iloc[0]
                                    q1, q2, q3, q4, q5 = st.columns(5)
                                    with q1: st.metric("Pattern", _r.get('pattern_name', '-'))
                                    with q2: st.metric("Buy point", f"{_r.get('pivot', float('nan')):,.2f}")
                                    with q3: st.metric("Dist to pivot", f"{_r.get('dist_pct', float('nan')):+.1f}%")
                                    with q4: st.metric("Before/Post", f"{_r.get('before_bo_score',0)} / {_r.get('post_bo_score',0)}")
                                    with q5: st.metric("Composite", f"{_r.get('composite_score',0)}/12")
                                _ctx = _cres.get('htf_context')
                                if _ctx:
                                    _pf = _ctx.get('pole_from')
                                    st.caption(
                                        f"**HTF context** — pole {_ctx['pole_low']:,.2f} → "
                                        f"{_ctx['flag_high']:,.2f} (+{_ctx['pole_gain_pct']:.0f}% in "
                                        f"{_ctx['pole_bars']} bars)"
                                        + (f", out of a {_pf['pattern']}" if _pf else "")
                                        + f" · flag {_ctx['flag_bars']} bars, "
                                          f"{_ctx['flag_depth_pct']:.1f}% deep")
                        except Exception as _ce:
                            st.error(f"Could not draw the chart: {_ce}")

                
                # --- Sub-tab 1: by pattern, table beside the chart (as the TV tab does) ---
                with sub_tab1:
                    # The state machine settles on one of four names. "Double Bottom" is a
                    # FIFTH category here and deliberately OVERLAPS them: it is detected
                    # independently and can occur inside any of the four, so a base that is a
                    # Cup and also a double bottom is listed under both. That is the whole
                    # point of holding it separately rather than making it compete for the
                    # label - it stopped setting the buy point, it did not stop existing.
                    # "HTF" is enclosing context (in_htf_flag / htf_context); "6-Wk Flat",
                    # "Ascending Base" and "Base" are retired and were permanently empty.
                    pattern_order = ["Cup+Handle", "Cup", "Flat Base", "Consolidation"]
                    _has_db = ('is_double_bottom' in filtered_ibd.columns
                               and bool(filtered_ibd['is_double_bottom'].fillna(False).any()))

                    def _cat_slice(_name):
                        if _name == "Double Bottom":
                            return filtered_ibd[filtered_ibd['is_double_bottom'].fillna(False)]
                        return filtered_ibd[filtered_ibd['pattern_name'] == _name]

                    _cats = [p for p in pattern_order if not _cat_slice(p).empty]
                    if _has_db:
                        _cats.append("Double Bottom")

                    if not _cats:
                        st.info("No tickers match the current filters.")
                    else:
                        cat_pat = st.radio(
                            "Pattern", _cats, horizontal=True, key="ibd_cat_pat",
                            format_func=lambda p: f"{p} ({len(_cat_slice(p))})")
                        pat_df = _cat_slice(cat_pat).sort_values(
                            'dist_pct', key=lambda s: s.abs()).reset_index(drop=True)
                        _tick = pat_df['ticker'].tolist()
                        if cat_pat == "Double Bottom":
                            st.caption("A double bottom forms *inside* a base, so these tickers "
                                       "also appear under their own pattern above. The buy point "
                                       "shown is that base's — the W's middle peak is reported as "
                                       "`db_middle_peak` but is not quoted as a buy point.")

                        # Cursor kept per pattern so switching away and back does not lose your
                        # place, and clamped rather than reset when a filter shrinks the list.
                        _ick = f"ibd_cat_i_{cat_pat}"
                        i = min(st.session_state.get(_ick, 0), len(_tick) - 1)
                        n1, n2, n3 = st.columns([1, 1, 6])
                        with n1:
                            if st.button("◀ Prev", key="ibd_cat_prev", use_container_width=True):
                                i = (i - 1) % len(_tick)
                        with n2:
                            if st.button("Next ▶", key="ibd_cat_next", use_container_width=True):
                                i = (i + 1) % len(_tick)
                        st.session_state[_ick] = i
                        cur = _tick[i]
                        with n3:
                            st.caption(f"**{cur}** — {i + 1} of {len(_tick)}")

                        lc, rc = st.columns([2, 3])
                        with lc:
                            _sel = st.dataframe(
                                _fmt_ibd_table(pat_df, [c for c in IBD_TABLE_COLS
                                                        if c != 'pattern_name']),
                                hide_index=True, use_container_width=True, height=470,
                                on_select="rerun", selection_mode="single-row",
                                key=f"ibd_cat_tbl_{cat_pat}")
                            # Only a NEW click moves the cursor. Streamlit keeps the selected
                            # row across reruns, so comparing it to `i` instead would make the
                            # last-clicked row fight every press of Next.
                            _rows = (_sel.get("selection") or {}).get("rows") or []
                            _seen = f"{_ick}_seen"
                            _clicked = _rows[0] if _rows else None
                            if _clicked is not None and _clicked != st.session_state.get(_seen):
                                st.session_state[_seen] = _clicked
                                st.session_state[_ick] = _clicked
                                st.rerun()
                            st.session_state[_seen] = _clicked
                            t_str = ",".join(_tick)
                            st.download_button(
                                f"Download {cat_pat} ({len(_tick)})", t_str,
                                f"{cat_pat.replace('+', 'Plus').replace(' ', '_')}_tickers.txt",
                                "text/plain", key=f"dl_{cat_pat}", use_container_width=True)
                        with rc:
                            try:
                                _cdf2, _cres2 = _ibd_chart_payload(cur, 300)
                                if _cdf2 is None or _cres2 is None:
                                    st.warning(f"No cached price data for {cur}.")
                                else:
                                    sys.path.insert(0, str(Path(__file__).resolve().parent / "python"))
                                    from pattern_chart import build_pattern_figure
                                    st.plotly_chart(
                                        build_pattern_figure(cur, _cdf2, _cres2,
                                                             bars=300, height=560),
                                        use_container_width=True,
                                        key=f"ibd_cat_fig_{cat_pat}")
                            except Exception as _ce:
                                st.error(f"Could not draw the chart: {_ce}")

                # --- Sub-tab 2: Full Data Table ---
                with sub_tab2:
                    st.markdown(f"Showing **{len(filtered_ibd)}** pattern signals matching filters.")
                    
                    # Same column set and formatting as the per-pattern mini tables, plus
                    # Sector/Industry which only this wider table has room for.
                    st.dataframe(
                        _fmt_ibd_table(filtered_ibd, IBD_TABLE_COLS + ['Sector', 'Industry']),
                        hide_index=True, use_container_width=True)
                    st.caption("✓ = signal fired · Pre/Post Score count how many of the six "
                               "sub-signals fired before and after the breakout · Pivot Dist % "
                               "is negative below the buy point.")

                # --- Sub-tab 3: Export Watchlists ---
                with sub_tab3:
                    st.subheader("📤 Export Categorized Watchlists")
                    
                    all_filtered_tickers = filtered_ibd['ticker'].unique().tolist()
                    st.write(f"**Total Filtered Tickers:** {len(all_filtered_tickers)}")
                    
                    # TradingView format with section headers
                    tv_lines = []
                    ibkr_lines = []
                    # Double Bottom gets its own ###section, same as the four primary names.
                    # Its tickers also appear under their own pattern - the sections overlap
                    # because the pattern does, and TradingView is happy with a repeat.
                    # IBKR lines are de-duplicated, since importing a symbol twice is an error
                    # there rather than a second row.
                    _export_cats = list(pattern_order) + (["Double Bottom"] if _has_db else [])
                    _ibkr_seen = set()
                    for pat in _export_cats:
                        p_df = (filtered_ibd[filtered_ibd['is_double_bottom'].fillna(False)]
                                if pat == "Double Bottom"
                                else filtered_ibd[filtered_ibd['pattern_name'] == pat])
                        if not p_df.empty:
                            tv_lines.append(f"###{pat}")
                            for t in p_df['ticker']:
                                tv_lines.append(t)
                                if t not in _ibkr_seen:
                                    _ibkr_seen.add(t)
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


# ---------- TAB: TradingView Pattern (exact drw_pattern.pine port) ----------
# Double bottoms are switched off in the scanner (DETECT_DOUBLE_BOTTOM); a "Dbl Bottom" can no
# longer appear in the results file, so it is not an option here either.
TV_PATTERN_ORDER = ["Cup+Handle", "Cup", "HTF", "Base"]
# How a non-cup base is drawn - the scanner's `base_shape`, describing the same channel the
# Pine paints for all of them (lines 984-985).
TV_SHAPE_ORDER = ["Flat Base", "Consolidation"]


@st.cache_data(ttl=28800, show_spinner=False)
def _tv_load_results(mtime: float):
    """Results keyed on the file's mtime, so a rerun of the scanner invalidates the cache."""
    with open(Path(__file__).resolve().parent / "python" / "tv_pattern_results.json",
              "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=28800, show_spinner=False)
def _tv_load_history(mtime: float):
    """Every base the scanner has ever seen, ~36k rows / 17MB — loaded only when asked for."""
    fp = Path(__file__).resolve().parent / "python" / "tv_pattern_history.json"
    if not fp.exists():
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


TV_FAV_PATH = Path(__file__).resolve().parent / "python" / "tv_favorites.json"


def _tv_load_favs():
    """Favourites survive a restart, so ctrl+space is worth pressing."""
    try:
        with open(TV_FAV_PATH, "r", encoding="utf-8") as f:
            return list(dict.fromkeys(json.load(f)))
    except Exception:
        return []


def _tv_save_favs(favs):
    try:
        TV_FAV_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TV_FAV_PATH, "w", encoding="utf-8") as f:
            json.dump(list(dict.fromkeys(favs)), f, indent=1)
    except Exception as e:
        st.warning(f"Could not save favourites: {e}")


def _tv_keyboard_nav(next_label: str, prev_label: str, fav_label: str):
    """Space / shift+space / ctrl+space, wired to the three nav buttons.

    Streamlit has no keyboard API, so this listens on the parent document from inside the
    component's iframe (same origin) and clicks the button whose text matches. Typing in the
    search box must keep working, so the handler bails out whenever the focus is in an input,
    a textarea or anything contenteditable - otherwise a space in a ticker name would page
    through the list instead.
    """
    st_html(f"""
<script>
const doc = window.parent.document;
const hit = (label) => {{
  for (const b of doc.querySelectorAll('button')) {{
    if ((b.innerText || '').trim() === label) {{ b.click(); return true; }}
  }}
  return false;
}};
const typing = () => {{
  const a = doc.activeElement;
  if (!a) return false;
  const tag = (a.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || a.isContentEditable;
}};
if (!window.parent.__tvNavBound) {{
  window.parent.__tvNavBound = true;
  doc.addEventListener('keydown', (e) => {{
    if (e.code !== 'Space' || e.altKey || e.metaKey) return;
    if (typing()) return;
    const label = e.ctrlKey ? {fav_label!r} : (e.shiftKey ? {prev_label!r} : {next_label!r});
    if (hit(label)) e.preventDefault();
  }}, true);
}}
</script>""", height=0)


@st.cache_data(ttl=1800, show_spinner=False)
def _tv_chart_bars(ticker: str):
    """Price bars for the pattern chart.

    Unlike the IBD tab this does NOT rescan the ticker: tv_pattern_scanner writes the drawing
    anchors (cup arcs, base channel, handle box, flag pole, trade boxes) into each record, so
    the chart only needs OHLC bars.
    """
    fp = (Path(__file__).resolve().parent / "ticker_cache"
          / f"{str(ticker).strip().replace('.', '-')}_1d.parquet")
    if not fp.exists():
        return None
    return pd.read_parquet(fp).sort_index()


with tab_tv_pattern:
    st.subheader("📐 TradingView Pattern Scan — `pine/drw_pattern.pine`")
    st.markdown(
        "Bar-by-bar port of the **MarketSmith Indicator**'s Chart Pattern Recognition block, "
        "replayed over `ticker_cache`. Same pivots, same 25-bar detection lag, same base / cup "
        "/ high-tight-flag state machine and the same trade boxes — so a hit here is what the "
        "indicator is drawing on that chart right now. Double bottoms are switched off. "
        "(The 🏆 IBD Pattern tab ports a *different* script, `drw_pattern_scanner.pine`, with "
        "its own thresholds and scoring; the two are expected to disagree.)")

    tv_col1, tv_col2 = st.columns([4, 6])
    with tv_col1:
        if st.button("🔄 Run / Rerun TV Pattern Scan", key="run_tv_pattern_scanner_btn"):
            with st.spinner("Replaying drw_pattern.pine over 7,000+ tickers (~30s)..."):
                try:
                    script_path = Path(__file__).resolve().parent / "python" / "tv_pattern_scanner.py"
                    result = subprocess.run([sys.executable, str(script_path)],
                                            cwd=str(Path(__file__).resolve().parent),
                                            capture_output=True, text=True)
                    if result.returncode == 0:
                        st.success("✅ Scan complete.")
                        st.cache_data.clear()
                        rerun_app()
                    else:
                        st.error(f"Scan failed:\n{result.stderr[-2000:]}")
                except Exception as e:
                    st.error(f"Error running the TV pattern scanner: {e}")
    with tv_col2:
        tv_search = st.text_input("🔍 Ticker Search", value="", placeholder="e.g. NVDA, XOM...",
                                  key="tv_quick_ticker_search").strip().upper()

    tv_json_path = Path(__file__).resolve().parent / "python" / "tv_pattern_results.json"
    if not tv_json_path.exists():
        st.info("No `tv_pattern_results.json` yet — press **🔄 Run / Rerun TV Pattern Scan**.")
    else:
        try:
            try:
                tv_payload = _tv_load_results(tv_json_path.stat().st_mtime)
            except json.JSONDecodeError as je:
                st.error(f"`tv_pattern_results.json` is incomplete (JSON ends at line "
                         f"{je.lineno}); a scan was interrupted mid-write. Rerun the scan.")
                tv_payload = None

            if tv_payload:
                tv_results = tv_payload.get("results", [])
                gen_at = str(tv_payload.get("generated_at", ""))[:16].replace("T", " ")
                df_tv = pd.DataFrame(tv_results)

                if df_tv.empty:
                    st.info("The scan found no live patterns.")
                else:
                    # Same enrichment as the IBD tab: RS/sector from the stocks dataset, and
                    # trend/liquidity from ticker_cache (the scanner emits neither).
                    if 'df' in locals() and df is not None and not df.empty:
                        if 'date' in df.columns:
                            latest_stocks = df.sort_values('date').groupby('Ticker').last().reset_index()
                        else:
                            latest_stocks = df.drop_duplicates(subset=['Ticker']).copy()
                        merge_fields = ['Ticker', 'Percentile', 'Relative Strength', 'AvgVol30',
                                        'Sector', 'Industry']
                        avail_m = [c for c in merge_fields if c in latest_stocks.columns]
                        if len(avail_m) > 1:
                            df_tv = df_tv.merge(latest_stocks[avail_m], left_on='ticker',
                                                right_on='Ticker', how='left')
                    try:
                        _tm = _ibd_trend_metrics(tuple(sorted(df_tv['ticker'].astype(str).unique())))
                        if not _tm.empty:
                            df_tv = df_tv.merge(_tm, on='ticker', how='left')
                    except Exception as _e:
                        st.caption(f"Trend metrics unavailable ({_e}); trend filters disabled.")

                    for c in ('Percentile', 'AvgVol30'):
                        if c in df_tv.columns:
                            df_tv[c] = pd.to_numeric(df_tv[c], errors='coerce')

                    m1, m2, m3, m4, m5 = st.columns(5)
                    with m1: st.metric("Live patterns", len(df_tv))
                    with m2: st.metric("In Base", int((df_tv['status'] == 'In Base').sum()))
                    with m3: st.metric("Post-BO", int((df_tv['status'] == 'Post-BO').sum()))
                    with m4: st.metric("In Flag (HTF)", int((df_tv['status'] == 'In Flag').sum()))
                    with m5: st.metric("Within 5% of pivot",
                                       int((df_tv['dist_pct'].abs() <= 5).sum()))
                    st.caption(f"Scanned {tv_payload.get('universe', '?')} tickers · generated {gen_at} · "
                               f"Pine quirk settings: {tv_payload.get('pine_quirks', {})}")
                    st.divider()

                    with st.expander("🎛️ Filter & Refine", expanded=False):
                        g1, g2, g3, g4 = st.columns(4)
                        with g1:
                            avail_pat = [p for p in TV_PATTERN_ORDER
                                         if p in set(df_tv['pattern_name'])]
                            tv_pats = st.multiselect("Pattern", avail_pat, default=avail_pat,
                                                     key="tv_pat_sel")
                            avail_shape = [s for s in TV_SHAPE_ORDER
                                           if s in set(df_tv.get('base_shape', pd.Series(dtype=object)))]
                            tv_shapes = st.multiselect(
                                "Base shape", avail_shape, default=avail_shape,
                                key="tv_shape_sel",
                                help="Only applies to Base rows: a flat base is ≤15% deep over "
                                     "5+ weeks, anything else is a consolidation.")
                            tv_status = st.radio("Status", ["All", "In Base", "Post-BO", "In Flag"],
                                                 horizontal=True, key="tv_status_sel")
                        with g2:
                            tv_min_rs = st.slider("Min RS Rating (1-99)", 0, 99, 0, key="tv_min_rs")
                            tv_min_price = st.number_input("Min Price ($)", value=12.0, step=1.0,
                                                           key="tv_min_price")
                            tv_max_price = st.number_input("Max Price ($)", value=10000.0, step=10.0,
                                                           key="tv_max_price")
                            tv_min_vol50 = st.number_input("Min 50D Avg Vol", value=400000,
                                                           step=50000, key="tv_min_vol50")
                        with g3:
                            tv_max_dist = st.number_input("Max |distance to pivot| %", value=100.0,
                                                          key="tv_max_dist")
                            tv_max_52w = st.number_input("Max % Off 52W High", value=100.0,
                                                         key="tv_max_52w")
                            tv_depth = st.slider("Base depth % range", 0, 60, (0, 60),
                                                 key="tv_depth_rng")
                            tv_min_days = st.number_input("Min days in base", value=0, step=5,
                                                          key="tv_min_days")
                        with g4:
                            _has_trend = {'sma50', 'sma200'} <= set(df_tv.columns)
                            tv_above200 = st.checkbox("Price > 200D SMA", value=True,
                                                      disabled=not _has_trend, key="tv_above200")
                            tv_50o200 = st.checkbox("50D SMA > 200D SMA", value=True,
                                                    disabled=not _has_trend, key="tv_50o200")
                            tv_rs_nh = st.checkbox("RS line at a new high", value=False,
                                                   key="tv_rs_nh")
                            # The Pine's cup test is a per-bar snapshot that decays as the right
                            # side of the cup completes, so "how many bars of this base ever
                            # passed it" is the real strength signal. 1 bar is a marginal cup,
                            # 20+ is unambiguous; median is 11.
                            tv_min_cup = st.slider("Min cup evidence (bars)", 0, 30, 0,
                                                   key="tv_min_cup",
                                                   help="Bars of this base that passed the cup "
                                                        "test. Raise it to drop marginal cups.")

                    f_tv = df_tv.copy()
                    if tv_search:
                        f_tv = f_tv[f_tv['ticker'].str.upper().str.contains(tv_search, na=False)]
                    if tv_pats:
                        f_tv = f_tv[f_tv['pattern_name'].isin(tv_pats)]
                    # Shape is a property of Base rows only; a cup or a flag has none, so they
                    # pass this filter rather than being dropped by a control about bases.
                    if tv_shapes and 'base_shape' in f_tv.columns and set(tv_shapes) != set(avail_shape):
                        f_tv = f_tv[f_tv['base_shape'].isna()
                                    | f_tv['base_shape'].isin(tv_shapes)]
                    if tv_status != "All":
                        f_tv = f_tv[f_tv['status'] == tv_status]
                    if 'Percentile' in f_tv.columns and tv_min_rs > 0:
                        f_tv = f_tv[f_tv['Percentile'].fillna(0) >= tv_min_rs]
                    if tv_min_price > 0:
                        f_tv = f_tv[f_tv['close'] >= tv_min_price]
                    if tv_max_price < 10000.0:
                        f_tv = f_tv[f_tv['close'] <= tv_max_price]
                    if 'avg_vol50' in f_tv.columns and tv_min_vol50 > 0:
                        f_tv = f_tv[f_tv['avg_vol50'].fillna(0) >= tv_min_vol50]
                    if tv_max_dist < 100.0:
                        f_tv = f_tv[f_tv['dist_pct'].abs().fillna(999.0) <= tv_max_dist]
                    if tv_max_52w < 100.0:
                        f_tv = f_tv[f_tv['pct_off_52w_high'].fillna(999.0) <= tv_max_52w]
                    if tv_depth != (0, 60):
                        _d = f_tv['base_depth_pct'].fillna(-1)
                        f_tv = f_tv[(_d >= tv_depth[0]) & (_d <= tv_depth[1])]
                    if tv_min_days > 0:
                        f_tv = f_tv[f_tv['days_in_base'].fillna(0) >= tv_min_days]
                    # NaN sma200 means under 200 bars of history, i.e. the test cannot be
                    # evaluated rather than failed - same treatment as the IBD tab.
                    if tv_above200 and 'sma200' in f_tv.columns:
                        f_tv = f_tv[f_tv['sma200'].notna() & (f_tv['close'] > f_tv['sma200'])]
                    if tv_50o200 and {'sma50', 'sma200'} <= set(f_tv.columns):
                        f_tv = f_tv[f_tv['sma50'].notna() & f_tv['sma200'].notna()
                                    & (f_tv['sma50'] > f_tv['sma200'])]
                    if tv_rs_nh and 'rs_nh' in f_tv.columns:
                        f_tv = f_tv[f_tv['rs_nh'] == True]  # noqa: E712
                    # Applies to cup readings only: a Base or HTF has no cup evidence by
                    # definition and would otherwise be filtered out by a control about cups.
                    if tv_min_cup > 0 and 'cup_bars_in_base' in f_tv.columns:
                        f_tv = f_tv[(f_tv['pattern_name'] != 'Cup')
                                    | (f_tv['cup_bars_in_base'].fillna(0) >= tv_min_cup)]

                    tv_chart_tab, tv_cat_tab, tv_hist_tab, tv_tbl_tab, tv_exp_tab = st.tabs(
                        ["📈 Chart", "📂 By Pattern", "🕘 History", "📋 Data Table", "📤 Export"])

                    # --- Chart: the Pine's own shapes ---
                    with tv_chart_tab:
                        if f_tv.empty:
                            st.info("No tickers match the current filters.")
                        else:
                            c1, c2, c3 = st.columns([3, 1, 1])
                            _opts = f_tv.sort_values(
                                'dist_pct', key=lambda s: s.abs())['ticker'].tolist()
                            _pre = tv_search if tv_search in _opts else _opts[0]
                            with c1:
                                tv_tkr = st.selectbox("Ticker", _opts, index=_opts.index(_pre),
                                                      key="tv_chart_tkr")
                            with c2:
                                tv_bars = st.select_slider("Bars", [120, 200, 300, 450, 700],
                                                           value=300, key="tv_chart_bars")
                            with c3:
                                # The Pine ships this detector's input off (line 409) and only
                                # runs it on weekly bars, so it is opt-out here rather than
                                # silently on top of a chart the user compares to TradingView.
                                tv_tight = st.checkbox("3-week tight boxes", value=True,
                                                       key="tv_show_tight",
                                                       help="The Pine's Tight Closes Detector "
                                                            "(weekly), drawn on the daily chart.")
                            try:
                                _bars_df = _tv_chart_bars(tv_tkr)
                                _row = f_tv[f_tv['ticker'] == tv_tkr].iloc[0].to_dict()
                                if _bars_df is None:
                                    st.warning(f"No cached price data for {tv_tkr}.")
                                else:
                                    sys.path.insert(0, str(Path(__file__).resolve().parent / "python"))
                                    from tv_pattern_chart import build_tv_pattern_figure
                                    st.plotly_chart(
                                        build_tv_pattern_figure(tv_tkr, _bars_df, _row,
                                                                bars=tv_bars,
                                                                show_tight=tv_tight),
                                        use_container_width=True)
                                    q1, q2, q3, q4, q5 = st.columns(5)
                                    with q1: st.metric("Pattern", _row.get('base_shape')
                                                                  or _row.get('pattern_name', '-'))
                                    with q2: st.metric("Buy point", f"{_row.get('pivot') or float('nan'):,.2f}")
                                    with q3: st.metric("Dist to pivot", f"{_row.get('dist_pct') or 0:+.1f}%")
                                    with q4: st.metric("Base", f"{_row.get('days_in_base') or 0}d / "
                                                               f"{_row.get('base_depth_pct') or 0:.0f}%")
                                    with q5: st.metric("Acc / Dis days",
                                                       f"{_row.get('acc_days', 0)} / {_row.get('dis_days', 0)}")

                                    _ctx = _row.get('htf_context')
                                    if isinstance(_ctx, dict) and _ctx:
                                        st.caption(
                                            f"**High tight flag** — pole {_ctx.get('pole_low')} → "
                                            f"{_ctx.get('flag_high')} (+{_ctx.get('pole_gain_pct') or 0:.0f}% "
                                            f"in {_ctx.get('pole_bars')} bars), flag "
                                            f"{_ctx.get('flag_bars')} bars / "
                                            f"{_ctx.get('flag_depth_pct') or 0:.1f}% deep")

                                    # Why a cup base is not reported as cup-with-handle. The
                                    # Pine's handle branch is effectively unreachable on daily
                                    # bars, so show how far this base got rather than leaving
                                    # the absence unexplained.
                                    _hg = _row.get('handle_gates')
                                    if isinstance(_hg, dict) and _hg:
                                        _fail = [n for n, ok in (("bars 5-25", _hg.get('bars_ok')),
                                                                 ("depth ≤12%", _hg.get('depth_ok')),
                                                                 ("above base mid", _hg.get('mid_ok')),
                                                                 ("volume ≤50% of MA", _hg.get('vol_ok')),
                                                                 ("cup present", _hg.get('cup_ok')))
                                                 if not ok]
                                        st.caption(
                                            f"**Handle test** — {_hg.get('bars')} bars since the "
                                            f"locked peak {_hg.get('peak_locked')}, low "
                                            f"{_hg.get('low')} vs base mid {_hg.get('base_mid')}, "
                                            f"volume {_hg.get('vol_pct_of_ma')}% of the 50-day MA"
                                            + (f" · fails: {', '.join(_fail)}" if _fail
                                               else " · passes every gate"))
                            except Exception as _ce:
                                st.error(f"Could not draw the chart: {_ce}")

                    # --- By pattern, with the chart alongside and keyboard paging ---
                    with tv_cat_tab:
                        _cat_pats = [p for p in TV_PATTERN_ORDER
                                     if p in set(f_tv['pattern_name'])]
                        if not _cat_pats:
                            st.info("No tickers match the current filters.")
                        else:
                            tv_cat_pat = st.radio(
                                "Pattern", _cat_pats, horizontal=True, key="tv_cat_pat",
                                format_func=lambda p: f"{p} "
                                                      f"({int((f_tv['pattern_name'] == p).sum())})")
                            pat_df = f_tv[f_tv['pattern_name'] == tv_cat_pat].sort_values(
                                'dist_pct', key=lambda s: s.abs()).reset_index(drop=True)
                            _tick = pat_df['ticker'].tolist()

                            # The cursor is stored per pattern, so switching pattern and coming
                            # back does not lose your place, and is clamped rather than reset
                            # when a filter shrinks the list under it.
                            _ck = f"tv_cat_i_{tv_cat_pat}"
                            i = min(st.session_state.get(_ck, 0), len(_tick) - 1)
                            if 'tv_favs' not in st.session_state:
                                st.session_state['tv_favs'] = _tv_load_favs()

                            NEXT_L, PREV_L, FAV_L = "Next ▶", "◀ Prev", "☆ Favourite"
                            n1, n2, n3, n4 = st.columns([1, 1, 1.4, 5])
                            with n1:
                                if st.button(PREV_L, key="tv_cat_prev",
                                             use_container_width=True):
                                    i = (i - 1) % len(_tick)
                            with n2:
                                if st.button(NEXT_L, key="tv_cat_next",
                                             use_container_width=True):
                                    i = (i + 1) % len(_tick)
                            with n3:
                                if st.button(FAV_L, key="tv_cat_fav",
                                             use_container_width=True):
                                    _f = list(st.session_state['tv_favs'])
                                    _t = _tick[i]
                                    if _t in _f:
                                        _f.remove(_t)
                                    else:
                                        _f.append(_t)
                                    st.session_state['tv_favs'] = _f
                                    _tv_save_favs(_f)
                            st.session_state[_ck] = i
                            cur = _tick[i]
                            with n4:
                                st.caption(
                                    f"**{cur}** — {i + 1} of {len(_tick)} · "
                                    "`space` next · `shift+space` previous · "
                                    "`ctrl+space` favourite"
                                    + ("  ·  ⭐ on your list" if cur in st.session_state['tv_favs']
                                       else ""))
                            _tv_keyboard_nav(NEXT_L, PREV_L, FAV_L)

                            lc, rc = st.columns([2, 3])
                            with lc:
                                mini = ['ticker', 'base_shape', 'status', 'Percentile', 'close',
                                        'pivot', 'dist_pct', 'base_depth_pct', 'days_in_base',
                                        'cup_bars_in_base', 'acc_days', 'dis_days',
                                        'pct_off_52w_high']
                                mini = [c for c in mini if c in pat_df.columns]
                                _shown = pat_df[mini].rename(columns={
                                    'ticker': 'Ticker', 'base_shape': 'Shape',
                                    'Percentile': 'RS Rating', 'close': 'Price ($)',
                                    'pivot': 'Buy pt', 'dist_pct': 'Dist %',
                                    'base_depth_pct': 'Depth %', 'days_in_base': 'Days',
                                    'cup_bars_in_base': 'Cup bars',
                                    'acc_days': 'Acc', 'dis_days': 'Dis',
                                    'pct_off_52w_high': '% off 52W'})
                                _sel = st.dataframe(
                                    _shown, hide_index=True, use_container_width=True,
                                    height=470, on_select="rerun",
                                    selection_mode="single-row", key=f"tv_cat_tbl_{tv_cat_pat}")
                                # Only a NEW click on the table moves the cursor. Streamlit keeps
                                # the selected row across reruns, so comparing it to `i` instead
                                # would make the row you last clicked fight every press of Next
                                # and snap the cursor straight back to it.
                                _rows = (_sel.get("selection") or {}).get("rows") or []
                                _seen = f"{_ck}_seen"
                                _clicked = _rows[0] if _rows else None
                                if _clicked is not None and _clicked != st.session_state.get(_seen):
                                    st.session_state[_seen] = _clicked
                                    st.session_state[_ck] = _clicked
                                    st.rerun()
                                st.session_state[_seen] = _clicked
                                t_str = ",".join(_tick)
                                st.download_button(
                                    f"Download {tv_cat_pat} ({len(_tick)})", t_str,
                                    f"tv_{tv_cat_pat.replace('+', 'Plus').replace(' ', '_')}.txt",
                                    "text/plain", key=f"tv_dl_{tv_cat_pat}",
                                    use_container_width=True)
                                if st.session_state['tv_favs']:
                                    with st.expander(
                                            f"⭐ Favourites ({len(st.session_state['tv_favs'])})",
                                            expanded=False):
                                        _fs = ",".join(st.session_state['tv_favs'])
                                        st.code(_fs, language="text")
                                        fb1, fb2 = st.columns(2)
                                        with fb1:
                                            st.download_button("Download", _fs,
                                                               "tv_favorites.txt", "text/plain",
                                                               key="tv_dl_favs",
                                                               use_container_width=True)
                                        with fb2:
                                            if st.button("Clear", key="tv_clear_favs",
                                                         use_container_width=True):
                                                st.session_state['tv_favs'] = []
                                                _tv_save_favs([])
                                                st.rerun()
                            with rc:
                                try:
                                    _bdf = _tv_chart_bars(cur)
                                    if _bdf is None:
                                        st.warning(f"No cached price data for {cur}.")
                                    else:
                                        sys.path.insert(
                                            0, str(Path(__file__).resolve().parent / "python"))
                                        from tv_pattern_chart import build_tv_pattern_figure
                                        st.plotly_chart(
                                            build_tv_pattern_figure(
                                                cur, _bdf, pat_df.iloc[i].to_dict(),
                                                bars=300, height=560),
                                            use_container_width=True,
                                            key=f"tv_cat_fig_{tv_cat_pat}")
                                except Exception as _ce:
                                    st.error(f"Could not draw the chart: {_ce}")

                    # --- History: every base the scanner has seen, not just today's ---
                    with tv_hist_tab:
                        _hp = Path(__file__).resolve().parent / "python" / "tv_pattern_history.json"
                        if not _hp.exists():
                            st.info("No history file yet — press **🔄 Run / Rerun TV Pattern "
                                    "Scan** above to build `tv_pattern_history.json`.")
                        else:
                            _hpay = _tv_load_history(_hp.stat().st_mtime)
                            df_h = pd.DataFrame((_hpay or {}).get("patterns", []))
                            if df_h.empty:
                                st.info("The history file is empty.")
                            else:
                                st.markdown(
                                    f"**{len(df_h):,}** finished bases across "
                                    f"{df_h['ticker'].nunique():,} tickers, back "
                                    f"{'~6 years' if len(df_h) else ''} to "
                                    f"`{df_h['end_date'].min()}`. A base is recorded the day it "
                                    "ends — at a breakout, or when it broke its 35% depth limit "
                                    "or ran past 325 bars.")
                                _bo = df_h[df_h['ended'] == 'Breakout']
                                _res = _bo[_bo['outcome'].isin(['Target', 'Stop'])]
                                h1, h2, h3, h4, h5 = st.columns(5)
                                with h1: st.metric("Breakouts", f"{len(_bo):,}")
                                with h2: st.metric("Breakdowns",
                                                   f"{int((df_h['ended'] == 'Breakdown').sum()):,}")
                                with h3: st.metric("Hit +20% first",
                                                   f"{int((_bo['outcome'] == 'Target').sum()):,}")
                                with h4: st.metric("Hit −8% first",
                                                   f"{int((_bo['outcome'] == 'Stop').sum()):,}")
                                with h5: st.metric(
                                    "Resolved win rate",
                                    f"{(_bo['outcome'] == 'Target').sum() / len(_res) * 100:.0f}%"
                                    if len(_res) else "—")
                                # The metric above divides by RESOLVED breakouts only. Saying
                                # "hit +20%" over all breakouts would count every still-open one
                                # as a loss, and counting "ever reached +20%" would count a
                                # breakout that stopped out first as a win. Both flatter or
                                # punish the scan for the wrong reason - so the denominator is
                                # stated, and Open is shown separately rather than folded in.
                                st.caption(
                                    f"Win rate is over the **{len(_res):,} resolved** breakouts "
                                    f"only (+20% or −8% touched within "
                                    f"{(_hpay or {}).get('hold_bars', 60)} bars, whichever came "
                                    f"**first**); {int((_bo['outcome'] == 'Open').sum()):,} are "
                                    "still open and are not counted either way. A bar that spans "
                                    "both levels is scored a stop.")

                                e1, e2, e3, e4 = st.columns(4)
                                with e1:
                                    h_pat = st.multiselect(
                                        "Pattern", sorted(df_h['pattern'].dropna().unique()),
                                        default=sorted(df_h['pattern'].dropna().unique()),
                                        key="tv_h_pat")
                                with e2:
                                    h_end = st.multiselect(
                                        "Ended as", ["Breakout", "Breakdown"],
                                        default=["Breakout", "Breakdown"], key="tv_h_end")
                                    h_out = st.multiselect(
                                        "Outcome", ["Target", "Stop", "Open"],
                                        default=["Target", "Stop", "Open"], key="tv_h_out")
                                with e3:
                                    h_from = st.text_input("From date (YYYY-MM-DD)", "",
                                                           key="tv_h_from")
                                    h_tkr = st.text_input("Ticker contains", "",
                                                          key="tv_h_tkr").strip().upper()
                                with e4:
                                    h_min_days = st.number_input("Min days in base", value=0,
                                                                 step=5, key="tv_h_days")
                                    h_favs = st.checkbox("Favourites only", value=False,
                                                         key="tv_h_favs")

                                f_h = df_h.copy()
                                if h_pat:
                                    f_h = f_h[f_h['pattern'].isin(h_pat)]
                                if h_end:
                                    f_h = f_h[f_h['ended'].isin(h_end)]
                                # Breakdowns have no outcome; the filter is about breakouts and
                                # must not silently delete them.
                                if h_out:
                                    f_h = f_h[f_h['outcome'].isna() | f_h['outcome'].isin(h_out)]
                                if h_from:
                                    f_h = f_h[f_h['end_date'] >= h_from]
                                if h_tkr:
                                    f_h = f_h[f_h['ticker'].str.contains(h_tkr, na=False)]
                                if h_min_days > 0:
                                    f_h = f_h[f_h['days'].fillna(0) >= h_min_days]
                                if h_favs:
                                    f_h = f_h[f_h['ticker'].isin(
                                        st.session_state.get('tv_favs', _tv_load_favs()))]

                                st.markdown(f"Showing **{len(f_h):,}** of {len(df_h):,}.")
                                hcols = ['ticker', 'pattern', 'base_shape', 'ended',
                                         'start_date', 'end_date', 'days', 'pivot',
                                         'base_top', 'base_low', 'depth_pct', 'cup_bars',
                                         'outcome', 'bars_to_outcome', 'max_gain_pct',
                                         'max_drawdown_pct', 'bars_forward', 'acc_days',
                                         'dis_days']
                                hcols = [c for c in hcols if c in f_h.columns]
                                st.dataframe(
                                    f_h[hcols].head(3000).rename(columns={
                                        'ticker': 'Ticker', 'pattern': 'Pattern',
                                        'base_shape': 'Shape', 'ended': 'Ended',
                                        'start_date': 'Started', 'end_date': 'Ended on',
                                        'days': 'Days', 'pivot': 'Buy point',
                                        'base_top': 'Base top', 'base_low': 'Base low',
                                        'depth_pct': 'Depth %', 'cup_bars': 'Cup bars',
                                        'outcome': 'Outcome',
                                        'bars_to_outcome': 'Bars to outcome',
                                        'max_gain_pct': 'Max gain %',
                                        'max_drawdown_pct': 'Max DD %',
                                        'bars_forward': 'Window (bars)',
                                        'acc_days': 'Acc', 'dis_days': 'Dis'}),
                                    hide_index=True, use_container_width=True, height=430)
                                if len(f_h) > 3000:
                                    st.caption("Table capped at 3,000 rows — the CSV below has "
                                               "all of them.")
                                st.download_button("Download filtered history CSV",
                                                   f_h[hcols].to_csv(index=False),
                                                   "tv_pattern_history.csv", "text/csv",
                                                   key="tv_dl_hist")

                                # Per-pattern scoreboard, same denominator rule as above.
                                _r = f_h[f_h['outcome'].isin(['Target', 'Stop'])]
                                if len(_r):
                                    _g = _r.groupby('pattern').agg(
                                        resolved=('outcome', 'size'),
                                        target=('outcome', lambda s: int((s == 'Target').sum())),
                                        med_gain=('max_gain_pct', 'median')).reset_index()
                                    _g['win %'] = (_g['target'] / _g['resolved'] * 100).round(1)
                                    _g['med_gain'] = _g['med_gain'].round(1)
                                    st.markdown("**By pattern** — resolved breakouts only")
                                    st.dataframe(
                                        _g.rename(columns={
                                            'pattern': 'Pattern', 'resolved': 'Resolved',
                                            'target': 'Hit +20% first',
                                            'med_gain': 'Median max gain %'}),
                                        hide_index=True, use_container_width=True)

                    # --- Data table ---
                    with tv_tbl_tab:
                        st.markdown(f"Showing **{len(f_tv)}** of {len(df_tv)} live patterns.")
                        cols = ['ticker', 'pattern_name', 'base_shape', 'status', 'Percentile', 'rs_score',
                                'close', 'pivot', 'dist_pct', 'base_top', 'base_low',
                                'base_depth_pct', 'days_in_base', 'bars_sbo', 'acc_days',
                                'dis_days', 'neu_days', 'pct_off_52w_high', 'cup_bars_in_base',
                                'rs_nh', 'rs_nh_period', 'AvgVol30', 'Sector', 'Industry']
                        cols = [c for c in cols if c in f_tv.columns]
                        st.dataframe(
                            f_tv[cols].rename(columns={
                                'ticker': 'Ticker', 'pattern_name': 'Pattern',
                                'base_shape': 'Shape', 'status': 'Status',
                                'Percentile': 'RS Rating', 'rs_score': 'RS Score',
                                'close': 'Close ($)', 'pivot': 'Buy point',
                                'dist_pct': 'Dist to pivot %', 'base_top': 'Base top',
                                'base_low': 'Base low', 'base_depth_pct': 'Depth %',
                                'days_in_base': 'Days in base', 'bars_sbo': 'Bars post-BO',
                                'acc_days': 'Acc', 'dis_days': 'Dis', 'neu_days': 'Neu',
                                'pct_off_52w_high': '% off 52W high',
                                'cup_bars_in_base': 'Cup bars', 'rs_nh': 'RS NH',
                                'rs_nh_period': 'RS NH period', 'AvgVol30': '30D Avg Vol'}),
                            hide_index=True, use_container_width=True)
                        st.download_button("Download full CSV",
                                           f_tv[cols].to_csv(index=False),
                                           "tv_pattern_scan.csv", "text/csv", key="tv_dl_csv")

                    # --- Export ---
                    with tv_exp_tab:
                        all_t = f_tv['ticker'].unique().tolist()
                        st.write(f"**Filtered tickers:** {len(all_t)}")
                        tv_lines, ibkr_lines = [], []
                        for pat in TV_PATTERN_ORDER:
                            p_df = f_tv[f_tv['pattern_name'] == pat]
                            if p_df.empty:
                                continue
                            tv_lines.append(f"###{pat}")
                            for t in p_df['ticker']:
                                tv_lines.append(t)
                                ibkr_lines.append(f"SYM, {t.upper()}, SMART/ARCA")
                        e1, e2, e3 = st.columns(3)
                        with e1:
                            st.markdown("##### 📄 Plain list")
                            st.code(",".join(all_t), language="text")
                            st.download_button("Download list", ",".join(all_t),
                                               "tv_patterns_list.txt", "text/plain",
                                               key="tv_dl_list")
                        with e2:
                            st.markdown("##### 📈 TradingView")
                            st.code("\n".join(tv_lines), language="text")
                            st.download_button("Download TV watchlist", "\n".join(tv_lines),
                                               "tv_patterns_tv.txt", "text/plain", key="tv_dl_tv")
                        with e3:
                            st.markdown("##### 💼 IBKR")
                            st.code("\n".join(ibkr_lines), language="text")
                            st.download_button("Download IBKR watchlist", "\n".join(ibkr_lines),
                                               "tv_patterns_ibkr.txt", "text/plain",
                                               key="tv_dl_ibkr")
        except Exception as e:
            st.error(f"Error loading TV pattern results: {e}")


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

# ---------- TAB 10: Chart Gallery ----------
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

# ---------- TAB 11: IBD Live Summary ----------
with tab_ibd_live:
    st.header("🎙️ IBD Live Summary")

    ingest_msg = st.session_state.pop("ibd_live_ingest_msg", None)
    if ingest_msg:
        {"success": st.success, "warning": st.warning, "error": st.error}[ingest_msg[0]](ingest_msg[1])

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
            ok, msg, sc = save_ibd_live_summary_from_text(date_str, ingest_text, suffix=suffix)
            if ok:
                focus_ibd_live_date(date_str)
                n_tickers = len((sc or {}).get("tickers") or [])
                # The message has to survive the rerun that repaints the tab below.
                st.session_state.ibd_live_ingest_msg = (
                    ("success", msg) if n_tickers else
                    ("warning", msg + " No tickers were parsed — check that the summary has a "
                                      "'2. Top Tickers & Technical Setups' table and a "
                                      "'7. … Ticker List' section (any heading level works).")
                )
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
                        percentile = None
                        if 'Ticker' in filtered_df.columns:
                            trows = filtered_df[filtered_df['Ticker'] == active_ticker]
                            if not trows.empty:
                                trow = trows.sort_values('date').iloc[-1] if 'date' in trows.columns else trows.iloc[0]
                                pctl = trow.get('Percentile')
                                if pctl is not None and not pd.isna(pctl):
                                    percentile = float(pctl)
                        fig, err = build_ticker_price_chart(active_ticker, percentile=percentile)
                    if err:
                        st.warning(err)
                    elif fig:
                        st.plotly_chart(fig, use_container_width=True, key=f"ibd_live_chart_{active_ticker}")

            # ---- Ticker Comments ----
            st.divider()
            st.subheader("💬 Ticker Comments")
            all_known_tickers = get_all_commented_or_mentioned_tickers()
            if all_known_tickers:
                # Follow the chart's ticker. A widget key outranks index= on every rerun,
                # so the value has to be written here, before the selectbox is created —
                # and only when the active ticker actually changed, or picking a ticker
                # from this dropdown would be yanked straight back to the chart's.
                active_tk = st.session_state.get("ibd_live_active_ticker")
                if active_tk in all_known_tickers and active_tk != st.session_state.get("_ibd_live_comment_synced_to"):
                    st.session_state.ibd_live_comment_ticker_select = active_tk
                    st.session_state._ibd_live_comment_synced_to = active_tk
                elif st.session_state.get("ibd_live_comment_ticker_select") not in all_known_tickers:
                    st.session_state.pop("ibd_live_comment_ticker_select", None)

                default_ticker = active_tk if active_tk in all_known_tickers else all_known_tickers[0]
                default_idx = all_known_tickers.index(default_ticker)
                comment_ticker = st.selectbox("Select a ticker",
                                              all_known_tickers, index=default_idx,
                                              key="ibd_live_comment_ticker_select",
                                              help="Every ticker you commented on or the show mentioned.")
                if comment_ticker != st.session_state.get("ibd_live_active_ticker"):
                    st.session_state.ibd_live_active_ticker = comment_ticker
                    st.session_state._ibd_live_comment_synced_to = comment_ticker

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
                            get_ticker_comment_timeline.clear()
                            st.success(f"Saved comment for {comment_ticker}.")
                            rerun_app()
                        else:
                            st.warning("Comment text can't be empty.")
            else:
                st.caption("No tickers with comments or transcript mentions yet.")

# --- Backtests Tab ---
with tab_backtests:
    st.header("🧪 Strategy Backtests")
    st.markdown("Run backtest scripts and view results directly from the dashboard.")

    with st.expander("📖 How to Run Backtests", expanded=False):
        st.markdown("""
### Backtest Quick Start

There are **3 backtest engines** available, each testing a different strategy class:

| Script | What it tests | Time (7K tickers) |
|---|---|---|
| `scanner_universe_backtest.py` | All IBDB patterns + buy signal combos × 9 exit rules | ~5 min |
| `full_backtest.py` | Every signal combination (2→N) across 13K+ bases | ~6 min |
| `trend_following_backtest.py` | 48 Turtle/Seykota trend-following strategies | ~3 min |
| `two_phase_backtest.py` | Phase 1 (SMA50+Shakeout) + Phase 2 (PB+SMA50 Bounce) | ~1 min |
| `unified_watchlist.py` | Live scan — IBD+TF+Buy signals for current date | ~5 min |

**Top verified strategies (by Sharpe):**

| Strategy | Engine | Win% | Avg Ret | Sharpe |
|---|---|---|---|---|
| **SMA50 Bounce+Shakeout × target_2r** | full_backtest | 77.3% | +6.66% | 0.56 |
| **PB+SMA50 Bounce × target_5r** | scanner_universe | 66.4% | +4.92% | 0.40 |
| **SMA50 Bounce+Shakeout × target_3r** | full_backtest | 74.8% | +7.47% | 0.54 |

**Two-phase strategy:** P1=SMA50+Shakeout (½ pos, 2:1) + P2=PB+SMA50 Bounce (full, 5:1) — combined **60.3% win, +3.28% avg**

**Quality filter:** depth ≤ 25% AND length ≤ 150d boosts combined win rate to **86-100%**.

**Daily automation:** Cron runs `unified_watchlist.py` at 5pm weekdays → diffs golden-tier changes → logs to `watchlist_history.log`.
        """)

    BACKTESTS_DIR = Path(__file__).resolve().parent / "python" / "backtests"

    subtab_run, subtab_results, subtab_reports = st.tabs(["▶️ Run Backtest", "📊 View Summary", "📈 HTML Reports"])

    with subtab_run:
        st.subheader("Run a Backtest")
        available_scripts = sorted([p.name for p in BACKTESTS_DIR.glob("*_backtest.py")])
        if not available_scripts:
            st.info("No backtest scripts found in python/backtests/.")
        else:
            script = st.selectbox("Select backtest script", available_scripts, key="bt_script")
            max_tickers = st.number_input("Max tickers (0 = all)", 0, 10000, 100, 100, key="bt_max")
            if st.button(f"🚀 Run {script}", type="primary", key="bt_run"):
                cmd = ["python3", str(BACKTESTS_DIR / script)]
                if max_tickers > 0:
                    cmd.extend(["--max-tickers", str(max_tickers)])
                with st.spinner(f"Running {script}... (may take several minutes)"):
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=str(BACKTESTS_DIR.parent.parent))
                        st.session_state.bt_output = result.stdout
                        st.session_state.bt_error = result.stderr
                        st.session_state.bt_script = script
                    except subprocess.TimeoutExpired:
                        st.error("Backtest timed out after 15 minutes.")
                    except Exception as e:
                        st.error(f"Error: {e}")
            if "bt_output" in st.session_state and st.session_state.get("bt_script") == script:
                with st.expander("📋 Output", expanded=True):
                    st.code(st.session_state.bt_output[-5000:] if st.session_state.bt_output else "(no output)")
                if st.session_state.get("bt_error"):
                    with st.expander("⚠️ Errors"):
                        st.code(st.session_state.bt_error[-2000:])

    with subtab_results:
        st.subheader("Summary CSVs")
        summary_files = sorted(BACKTESTS_DIR.glob("*_summary.csv")) + sorted(BACKTESTS_DIR.glob("*_results.csv"))
        summary_files = [p for p in summary_files if p.exists()]
        if not summary_files:
            st.info("No summary or results CSVs found. Run a backtest first.")
        else:
            chosen = st.selectbox("Select CSV", [p.name for p in summary_files], key="bt_csv")
            if chosen:
                try:
                    df = pd.read_csv(BACKTESTS_DIR / chosen)
                    st.metric("Rows", f"{len(df):,}")
                    st.dataframe(df.head(100), use_container_width=True)
                    if len(df) > 100:
                        st.caption(f"Showing 100 of {len(df):,} rows")
                except Exception as e:
                    st.error(f"Failed to load: {e}")

    with subtab_reports:
        st.subheader("HTML Reports")
        report_files = sorted(BACKTESTS_DIR.glob("*_report.html"))
        if not report_files:
            st.info("No HTML reports found. Run a backtest with --report or generate one.")
        else:
            chosen_r = st.selectbox("Select report", [p.name for p in report_files], key="bt_html")
            if chosen_r:
                try:
                    html_content = (BACKTESTS_DIR / chosen_r).read_text()
                    st_html(html_content, height=800, scrolling=True)
                except Exception as e:
                    st.error(f"Failed to load: {e}")

# --- Scans & Leads Tab ---
with tab_scans:
    st.header("🔍 Scans & Leads")
    st.markdown("Potential leads, scan results, and backtest performance — all in one view.")

    BACKTESTS_DIR_SL = Path(__file__).resolve().parent / "python" / "backtests"
    WATCHLIST_CSV = BACKTESTS_DIR_SL / "unified_watchlist.csv"

    lead_tab, scan_tab, perf_tab = st.tabs(["🎯 Potential Leads", "📊 All Scan Results", "📈 Performance"])

    # ── Helper to load watchlist ──
    @st.cache_data(ttl=3600)
    def load_watchlist():
        if WATCHLIST_CSV.exists():
            return pd.read_csv(WATCHLIST_CSV)
        return pd.DataFrame()

    wl = load_watchlist()

    with lead_tab:
        st.subheader("🎯 Golden-Tier Potential Leads")
        if wl.empty:
            st.warning("No watchlist data. Run unified_watchlist.py first.")
        else:
            golden = wl[(wl["combo_SMA50_Shakeout"] == True) & (wl["tf_flags_on"] >= 3)].copy()
            golden = golden.sort_values("composite", ascending=False)
            quality_n = golden["quality_filter"].sum() if "quality_filter" in golden.columns else 0

            c1, c2, c3, c4 = st.columns(4)
            with c1: st.metric("Golden Tier", len(golden))
            with c2: st.metric("Quality Pass (≤25%, ≤150d)", quality_n)
            with c3: st.metric("Both Engines", (golden.get("combo_PB_SMA50", pd.Series([False]*len(golden))) == True).sum())
            with c4: st.metric("Avg Composite", f"{golden['composite'].mean():.1f}" if len(golden) > 0 else "-")

            if len(golden) > 0:
                show_cols = ["ticker", "pattern", "depth", "length", "dist_to_pivot",
                             "composite", "combo_PB_SMA50", "tf_flags_on", "rsi"]
                show_cols = [c for c in show_cols if c in golden.columns]
                st.dataframe(
                    golden[show_cols].style
                    .background_gradient(subset=["composite"], cmap="RdYlGn")
                    .format({"depth": "{:.1f}%", "dist_to_pivot": "{:.1f}%", "composite": "{:.1f}"}),
                    use_container_width=True, height=400
                )

                # Per-ticker drill-down
                st.subheader("🔬 Ticker Drill-Down")
                ticker_list = sorted(golden["ticker"].unique())
                chosen_ticker = st.selectbox("Select ticker for technical details", ticker_list, key="scan_ticker")
                if chosen_ticker:
                    row = golden[golden["ticker"] == chosen_ticker].iloc[0]
                    dc1, dc2, dc3, dc4 = st.columns(4)
                    with dc1:
                        st.metric("Pattern", row.get("pattern", "-"))
                        st.metric("Depth", f"{row['depth']:.1f}%")
                    with dc2:
                        st.metric("Length", f"{int(row['length'])}d")
                        st.metric("Dist to Pivot", f"{row.get('dist_to_pivot', 0):+.1f}%")
                    with dc3:
                        tf_on = int(row.get("tf_flags_on", 0))
                        st.metric("TF Flags", f"{tf_on}/4")
                        st.metric("RSI", f"{row.get('rsi', '-'):.0f}" if pd.notna(row.get('rsi')) else "-")
                    with dc4:
                        st.metric("Composite", f"{row['composite']:.1f}")
                        pb_flag = "✅" if row.get("combo_PB_SMA50", False) else "—"
                        st.metric("PB+SMA50", pb_flag)

                    # TF flags detail
                    flags_detail = []
                    for flag in ["above_sma200", "ema_bullish", "near_52w_high", "rsi_bullish"]:
                        if flag in row.index and row[flag]:
                            flags_detail.append(flag.replace("_", " ").title())
                    st.caption(f"Active TF signals: {', '.join(flags_detail) if flags_detail else 'none'}")

                    # Mid-base / Breakout combo detail
                    combo_cols = [c for c in golden.columns if c.startswith("combo_") and c != "combo_count"]
                    active_combos = [c.replace("combo_", "").replace("_", " ") for c in combo_cols if row.get(c, False)]
                    if active_combos:
                        st.caption(f"Active buy combos: {', '.join(active_combos)}")

    with scan_tab:
        st.subheader("📊 All Scan Results")
        if wl.empty:
            st.warning("No watchlist data. Run unified_watchlist.py first.")
        else:
            # Filters
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                patterns = ["All"] + sorted(wl["pattern"].dropna().unique().tolist())
                pat_filter = st.selectbox("Pattern", patterns, key="scan_pat")
            with fc2:
                min_depth = st.slider("Min Depth %", 0.0, 50.0, 8.0, 0.5, key="scan_dmin")
            with fc3:
                max_depth = st.slider("Max Depth %", 0.0, 50.0, 25.0, 0.5, key="scan_dmax")

            fc4, fc5 = st.columns(2)
            with fc4:
                min_len = st.slider("Min Length (days)", 0, 400, 100, 10, key="scan_lmin")
            with fc5:
                max_len = st.slider("Max Length (days)", 0, 400, 200, 10, key="scan_lmax")

            ticker_search = st.text_input("Ticker search", "", key="scan_tsearch")

            filtered = wl.copy()
            if pat_filter != "All":
                filtered = filtered[filtered["pattern"] == pat_filter]
            filtered = filtered[(filtered["depth"] >= min_depth) & (filtered["depth"] <= max_depth)]
            filtered = filtered[(filtered["length"] >= min_len) & (filtered["length"] <= max_len)]
            if ticker_search:
                filtered = filtered[filtered["ticker"].str.upper().str.contains(ticker_search.upper())]

            st.metric("Results", f"{len(filtered):,} tickers")

            display_cols = ["ticker", "pattern", "depth", "length", "dist_to_pivot",
                           "composite", "combo_SMA50_Shakeout", "tf_flags_on", "rsi"]
            display_cols = [c for c in display_cols if c in filtered.columns]
            st.dataframe(
                filtered[display_cols].sort_values("composite", ascending=False)
                .style.background_gradient(subset=["composite"], cmap="RdYlGn")
                .format({"depth": "{:.1f}%", "dist_to_pivot": "{:.1f}%", "composite": "{:.1f}"}),
                use_container_width=True, height=500
            )

            # Export
            csv_export = filtered.to_csv(index=False)
            st.download_button("📥 Download CSV", csv_export, "filtered_scans.csv", "text/csv")

    with perf_tab:
        st.subheader("📈 Backtest Performance Summary")
        perf_files = (sorted(BACKTESTS_DIR_SL.glob("*_summary.csv"))
                      + sorted(BACKTESTS_DIR_SL.glob("*_results.csv")))
        if not perf_files:
            st.info("No backtest summary or results CSVs found. Run a backtest first.")
        else:
            chosen_perf = st.selectbox("Select summary or results", [p.name for p in perf_files], key="scan_perf")
            if chosen_perf:
                try:
                    pdf = pd.read_csv(BACKTESTS_DIR_SL / chosen_perf)
                    st.metric("Rows", f"{len(pdf):,}")

                    # Two-phase results: show phase-by-phase breakdown
                    if "p1_ret" in pdf.columns and "p2_ret" in pdf.columns:
                        st.markdown("**Two-Phase Stats**")
                        col_a, col_b, col_c = st.columns(3)
                        with col_a:
                            p1 = pdf[pdf["p1_entry_bar"] >= 0]
                            st.metric("P1 Events", f"{len(p1):,}", f"{(p1['p1_ret']>0).mean()*100:.0f}% win" if len(p1) else "-")
                        with col_b:
                            p2 = pdf[pdf["p2_entry_bar"] >= 0]
                            st.metric("P2 Events", f"{len(p2):,}", f"{(p2['p2_ret']>0).mean()*100:.0f}% win" if len(p2) else "-")
                        with col_c:
                            both = pdf[pdf["both_fired"] == True]
                            st.metric("Both Fired", f"{len(both):,}", f"{(both['combined_ret']>0).mean()*100:.0f}% win" if len(both) else "-")

                    if "sharpe" in pdf.columns:
                        pdf = pdf.sort_values("sharpe", ascending=False)
                        sort_label = "Sharpe"
                    elif "combined_ret" in pdf.columns:
                        pdf = pdf.sort_values("combined_ret", ascending=False)
                        sort_label = "combined return"
                    else:
                        sort_label = "file order"
                    st.dataframe(pdf.head(40), use_container_width=True)
                    if len(pdf) > 40:
                        st.caption(f"Showing top 40 of {len(pdf):,} rows — sorted by {sort_label}")
                except Exception as e:
                    st.error(f"Failed to load: {e}")

# ---------- TAB 15: Ratings Scanner ----------
with tab15:
    st.subheader("📊 IBD-Style Ratings Scanner")
    st.markdown("Computes **RS Rating, EPS Rating, A/D Rating, SMR Rating, Composite Rating, % Off 52W High** using the same formulas as the TradingView `Ratings Scanner` indicator.")

    sys.path.insert(0, str(Path(__file__).resolve().parent / "python"))
    from calc_ibd_ratings import (
        apply_rating_percentiles, calc_ad_raw_score,
        calc_pct_off_52w_high_snapshot, calc_eps_rating, calc_rs_raw_score,
        calc_rs_sub_raw_score, calc_smr_raw_score, spy_perf_windows,
        extract_eps_from_fundamentals, extract_smr_inputs_from_fundamentals,
    )
    from fetch_fundamentals import get_cached_fundamentals, fetch_and_cache_fundamentals

    CACHE_DIR = Path(__file__).resolve().parent / "ticker_cache"
    _spy_rs = pd.read_parquet(CACHE_DIR / "SPY_1d.parquet", columns=["Close"])
    _spy_rs.index = pd.to_datetime(_spy_rs.index)
    _spy_perf = spy_perf_windows(_spy_rs["Close"].astype(float).sort_index())

    # ── Mode selection ──
    mode = st.radio("Scope", ["Quick Scan (OHLCV-only ratings)", "Full Scan (incl. fundamentals)"],
                    horizontal=True, index=0,
                    help="Quick Scan computes RS, A/D, and % off 52W High from cached price data. Full Scan also fetches EPS/ROE from yfinance for EPS, SMR, and Composite ratings.")

    # ── Button to run scan ──
    col_btn, col_count = st.columns([2, 3])
    with col_btn:
        if st.button(f"🔍 Run {'Full' if 'Full' in mode else 'Quick'} Scan", type="primary", key="run_ratings_scan"):
            with st.spinner("Scanning tickers..." if "Quick" in mode else "Fetching fundamentals & scanning..."):
                results = []
                ticker_files = sorted(CACHE_DIR.glob("*_1d.parquet"))
                total = len(ticker_files)
                progress = st.progress(0, text="Scanning...")

                # Track fundamentals fetch stats
                n_fundamental_fetched = 0

                for i, fp in enumerate(ticker_files):
                    ticker = fp.stem.replace("_1d", "")
                    if ticker in ("SPY", "QQQ", "IWM", "DIA", "VTI"):
                        continue

                    try:
                        df = pd.read_parquet(fp)
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        df.index = pd.to_datetime(df.index)
                        if df.index.tz is not None:
                            df.index = df.index.tz_localize(None)

                        if df.empty or len(df) < 65 or 'Close' not in df.columns:
                            continue

                        # RS is a dual-momentum sigmoid - already the final 1-99 rating, not a
                        # raw score. A-D and the RS 3M/6M sub-ratings are still percentile-ranked
                        # against the whole scanned universe by apply_rating_percentiles() after
                        # the loop (a single ticker can't be percentile-ranked in isolation).
                        rs_final = calc_rs_raw_score(df['Close'], _spy_perf)
                        rs3m_raw = calc_rs_sub_raw_score(df['Close'], 63)
                        rs6m_raw = calc_rs_sub_raw_score(df['Close'], 126)
                        ad_raw = calc_ad_raw_score(df)

                        # % Off 52W High
                        pct_off = calc_pct_off_52w_high_snapshot(df)

                        # Latest price
                        latest = float(df['Close'].iloc[-1])

                        # Fundamentals (Full Scan only)
                        eps_rating = None
                        smr_raw = None
                        market_cap_mil = None

                        if "Full" in mode:
                            fund = fetch_and_cache_fundamentals(ticker, max_age_days=30, delay=0.15)
                            if fund and not fund.get('error'):
                                n_fundamental_fetched += 1
                                fy_eps, fq_eps, _ = extract_eps_from_fundamentals(fund)
                                info = fund.get('info') if isinstance(fund.get('info'), dict) else {}
                                roe_frac = info.get('returnOnEquity')
                                roe_val = float(roe_frac) * 100.0 if roe_frac is not None else None
                                mc = info.get('marketCap')
                                market_cap_mil = float(mc) / 1e6 if mc is not None else None

                                if fy_eps and fq_eps and len(fy_eps) >= 2 and len(fq_eps) >= 5:
                                    eps_rating = calc_eps_rating(fy_eps, fq_eps, roe_val)
                                sales_q0_yoy, sales_lt_growth, margin_now, margin_trend = \
                                    extract_smr_inputs_from_fundamentals(fund)
                                smr_raw = calc_smr_raw_score(sales_q0_yoy, sales_lt_growth,
                                                              margin_now, margin_trend, roe_val)

                        results.append({
                            'Ticker': ticker,
                            'Current Price': round(latest, 2),
                            'Market Cap (mil)': market_cap_mil,
                            '% Off 52W High': round(pct_off, 2) if not np.isnan(pct_off) else None,
                            'RS Rating': rs_final,
                            '_rs3m_raw': rs3m_raw,
                            '_rs6m_raw': rs6m_raw,
                            '_ad_raw': ad_raw,
                            '_smr_raw': smr_raw,
                            'EPS Rating': eps_rating,
                        })
                    except Exception:
                        pass

                    if (i + 1) % 100 == 0 or i == total - 1:
                        progress.progress((i + 1) / total,
                                          text=f"Scanned {i + 1}/{total} tickers...")

                progress.empty()

                if results:
                    result_df = pd.DataFrame(results)
                    # universe post-pass: percentile-ranks the raw scores against the
                    # eligible (price >= $4, mktcap >= $50M when known) scanned universe
                    # and finalizes RS/RS-3M/RS-6M/A-D/SMR/Comp Rating
                    result_df = apply_rating_percentiles(result_df)
                    result_df = result_df.rename(columns={
                        'Current Price': 'Close',
                        'RS 3-Month Rating': 'RS 3M',
                        'RS 6-Month Rating': 'RS 6M',
                        'SMR Rating': 'SMR Grade',
                    })
                    st.session_state.ratings_df = result_df
                    st.session_state.ratings_mode = mode
                    st.session_state.ratings_scanned = True
                    st.success(f"✅ Scanned {len(result_df):,} tickers" +
                               (f" (fetched fundamentals for {n_fundamental_fetched})" if "Full" in mode else ""))
                else:
                    st.warning("No results. Check that ticker_cache has data.")

    # ── Display results if available ──
    if st.session_state.get("ratings_scanned") and st.session_state.get("ratings_df") is not None:
        result_df = st.session_state.ratings_df
        mode = st.session_state.get("ratings_mode", "Quick")
        st.divider()

        # Filters
        col_f1, col_f2, col_f3, col_f4 = st.columns(4)
        with col_f1:
            min_rs = st.slider("Min RS Rating", 0, 99, 0, key="rs_min_filter")
        with col_f2:
            min_ad = st.slider("Min A/D Score", 0, 99, 0, key="ad_min_filter",
                               help="A/D Rating is a letter grade (A+..E); this filters on its "
                                    "underlying 1-99 percentile score.")
        with col_f3:
            max_pct_off = st.slider("Max % Off 52W High", 0.0, 100.0, 100.0, key="pct_off_filter")
        with col_f4:
            search_ticker = st.text_input("Search Ticker", "", key="rating_search").strip().upper()

        filtered = _int_ratings(result_df)
        if min_rs > 0:
            filtered = filtered[filtered['RS Rating'].notna() & (filtered['RS Rating'] >= min_rs)]
        if min_ad > 0:
            filtered = filtered[filtered['A/D Score'].notna() & (filtered['A/D Score'] >= min_ad)]
        if max_pct_off < 100:
            filtered = filtered[filtered['% Off 52W High'].notna() & (filtered['% Off 52W High'] <= max_pct_off)]
        if search_ticker:
            filtered = filtered[filtered['Ticker'].str.upper().str.contains(search_ticker)]

        # Sort by Comp Rating (if available) then RS Rating
        if 'Comp Rating' in filtered.columns and filtered['Comp Rating'].notna().any():
            filtered = filtered.sort_values('Comp Rating', ascending=False, na_position='last')
        else:
            filtered = filtered.sort_values('RS Rating', ascending=False, na_position='last')

        st.metric("Matching", f"{len(filtered):,} / {len(result_df):,}")

        # Color-scale RS and Comp Rating columns
        def color_rating(val):
            if pd.isna(val):
                return ''
            val = float(val)
            if val >= 90:
                return 'background-color: #14532d; color: #4ade80'
            elif val >= 80:
                return 'background-color: #1a3a1a; color: #86efac'
            elif val >= 70:
                return 'background-color: #1c3a1c; color: #bbf7d0'
            elif val < 30:
                return 'background-color: #450a0a; color: #fca5a5'
            return ''

        def color_pct_off(val):
            if pd.isna(val):
                return ''
            val = float(val)
            if val <= 3:
                return 'background-color: #14532d; color: #4ade80'
            elif val <= 10:
                return 'background-color: #1a3a1a; color: #86efac'
            elif val >= 30:
                return 'background-color: #450a0a; color: #fca5a5'
            return ''

        styled = filtered.style.map(color_rating, subset=['RS Rating', 'RS 3M', 'RS 6M', 'A/D Score'])
        if 'Comp Rating' in filtered.columns:
            styled = styled.map(color_rating, subset=['Comp Rating'])
        if 'EPS Rating' in filtered.columns:
            styled = styled.map(color_rating, subset=['EPS Rating'])
        if 'SMR Score' in filtered.columns:
            styled = styled.map(color_rating, subset=['SMR Score'])
        styled = styled.map(color_pct_off, subset=['% Off 52W High'])

        st.dataframe(styled, use_container_width=True, height=600,
                     column_config={
                         'Ticker': st.column_config.TextColumn('Ticker', width='small'),
                         'Close': st.column_config.NumberColumn('Close', format='$%.2f'),
                         'RS Rating': st.column_config.NumberColumn('RS Rating', format='%d'),
                     })

        # Export option
        csv = filtered.to_csv(index=False)
        st.download_button("📥 Download CSV", csv, f"ratings_scan_{datetime.now().strftime('%Y%m%d')}.csv", "text/csv")


# ---------- TAB 16: Daily Screener ----------
with tab16:
    st.subheader("📋 Daily Screener")
    st.markdown(
        "MarketSurge-style snapshot (158 columns + ~60 extras) computed from **ticker_cache** "
        "price/volume + fundamentals, with all ratings from `calc_ibd_ratings.py`, industry/company "
        "info from `IBD_data.txt`, and group ranks computed across the whole universe. "
        "Rebuild runs `python/build_daily_screener.py` (~20s) whenever the cache updates."
    )

    SCREENER_PATH = Path(__file__).resolve().parent / "output" / "daily_screener.csv"

    col_btn, col_info = st.columns([2, 3])
    with col_btn:
        if st.button("🔄 Rebuild Screener", type="primary", key="run_daily_screener"):
            with st.spinner("Rebuilding daily screener from ticker_cache (~15s)..."):
                script_path = Path(__file__).resolve().parent / "python" / "build_daily_screener.py"
                try:
                    result = subprocess.run([sys.executable, str(script_path)],
                                            cwd=str(Path(__file__).resolve().parent),
                                            capture_output=True, text=True, timeout=600)
                    if result.returncode == 0:
                        st.success("✅ Screener rebuilt from ticker_cache.")
                        st.cache_data.clear()
                        rerun_app()
                    else:
                        st.error(f"Rebuild failed:\n{result.stderr[-2000:]}")
                except subprocess.TimeoutExpired:
                    st.error("Rebuild timed out after 10 minutes — check python/build_daily_screener.py.")
                except Exception as e:
                    st.error(f"Rebuild failed: {e}")
    with col_info:
        if SCREENER_PATH.exists():
            _st_mtime = datetime.fromtimestamp(SCREENER_PATH.stat().st_mtime)
            st.caption(f"📄 `output/daily_screener.csv` • {SCREENER_PATH.stat().st_size / 1e6:.1f} MB • "
                       f"built {_st_mtime:%Y-%m-%d %H:%M}")
        else:
            st.caption("No snapshot yet — click **Rebuild Screener** to generate it.")

    @st.cache_data(ttl=600, show_spinner=False)
    def load_daily_screener():
        return pd.read_csv(SCREENER_PATH, low_memory=False)

    if not SCREENER_PATH.exists():
        st.info("Snapshots land in `output/daily_screener_<date>.csv` and `output/daily_screener.csv`.")
    else:
        sdf = load_daily_screener()
        # Streamlit runs every tab block on each rerun, so a stale/missing column in the
        # screener CSV must never raise and take down the whole app.
        _need_cols = ['Symbol', 'Name', 'Current Price', 'RS Rating', 'EPS Rating', 'Comp Rating',
                      '% Off High', 'Price vs 50-Day', 'Market Cap (mil)', 'RS Line New High',
                      'Price % Chg', 'Volume (1000s)']
        _missing_cols = [c for c in _need_cols if c not in sdf.columns]
        if _missing_cols:
            st.warning(f"`daily_screener.csv` is missing columns {_missing_cols}. "
                       "Click **🔄 Rebuild Screener** to regenerate it from the current schema.")
            st.stop()
        n_total = len(sdf)
        n_price = int(sdf['Current Price'].notna().sum())

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tickers", f"{n_total:,}")
        c2.metric("With Price Data", f"{n_price:,}")
        c3.metric("RS Rating ≥ 80", int((sdf['RS Rating'] >= 80).sum()))
        c4.metric("Comp Rating ≥ 80", int((sdf['Comp Rating'] >= 80).sum()))

        st.divider()
        st.markdown("**Filters**")
        col_p, col_t, col_s = st.columns(3)
        with col_p:
            preset = st.selectbox("Quick preset", [
                "Custom",
                "🏆 Top Leaders (RS ≥ 80)",
                "💪 Strong (RS ≥ 65)",
                "🎯 In Buy Zone (RS ≥ 65, ≤10% off high, >50-day)",
                "🚀 New RS Highs",
                "💰 Full Fundamentals (EPS ≥ 60)",
                "🔻 Near Highs (≤5% off high)",
            ], key="scr_preset")
        with col_t:
            search = st.text_input("Search ticker / name", "", key="scr_search").strip().upper()
        with col_s:
            sort_col = st.selectbox("Sort by", [
                "Comp Rating", "RS Rating", "RS 6-Month Rating", "% Chg 12 Months",
                "% Off High", "Price % Chg", "Market Cap (mil)",
            ], key="scr_sort")

        col1, col2, col3 = st.columns(3)
        with col1:
            min_rs = st.slider("Min RS Rating", 1, 99, 1, key="scr_min_rs")
            min_eps = st.slider("Min EPS Rating", 1, 99, 1, key="scr_min_eps")
        with col2:
            min_comp = st.slider("Min Comp Rating", 1, 99, 1, key="scr_min_comp")
            max_off = st.slider("Max % Off High", 0, 100, 100, key="scr_max_off")
        with col3:
            min_p50 = st.slider("Min Price vs 50-Day (%)", -100, 100, -100, key="scr_min_p50")
            min_mcap = st.slider("Min Market Cap ($B)", 0, 500, 0, key="scr_min_mcap")

        f = sdf.copy()
        if "Top Leaders" in preset:
            min_rs = max(min_rs, 80)
        if "Strong" in preset:
            min_rs = max(min_rs, 65)
        if "In Buy Zone" in preset:
            min_rs = max(min_rs, 65)
            max_off = min(max_off, 10)
            min_p50 = max(min_p50, 0)
        if "Near Highs" in preset:
            max_off = min(max_off, 5)
        if "Full Fundamentals" in preset:
            min_eps = max(min_eps, 60)
        if "New RS Highs" in preset:
            f = f[f['RS Line New High'] == 'Yes']

        if min_rs > 1:
            f = f[f['RS Rating'].notna() & (f['RS Rating'] >= min_rs)]
        if min_eps > 1:
            f = f[f['EPS Rating'].notna() & (f['EPS Rating'] >= min_eps)]
        if min_comp > 1:
            f = f[f['Comp Rating'].notna() & (f['Comp Rating'] >= min_comp)]
        if max_off < 100:
            f = f[f['% Off High'].notna() & (f['% Off High'] <= max_off)]
        if min_p50 > -100:
            f = f[f['Price vs 50-Day'].notna() & (f['Price vs 50-Day'] >= min_p50)]
        if min_mcap > 0:
            f = f[f['Market Cap (mil)'].notna() & (f['Market Cap (mil)'] >= min_mcap * 1000)]
        if search:
            f = f[f['Symbol'].str.upper().str.contains(search, na=False) |
                  f['Name'].astype(str).str.upper().str.contains(search, na=False)]

        if sort_col and sort_col in f.columns:
            f = f.sort_values(sort_col, ascending=False, na_position='last')

        default_cols = ['Symbol', 'Name', 'Industry Name', 'Ind Group Rank', 'Ind Group RS',
                        'Current Price', 'Price % Chg',
                        'RS Rating', 'EPS Rating', 'SMR Rating', 'A/D Rating', 'Comp Rating',
                        '% Off High', 'Price vs 50-Day', '% Chg 3 Months', '% Chg 12 Months',
                        '50-Day > 150-Day > 200-Day', 'Vol % Chg vs 50-Day', 'Volume (1000s)',
                        'Market Cap (mil)', 'EPS Due Date', 'Analyst Target Mean', '% Upside to Target']
        avail_cols = [c for c in default_cols if c in f.columns]
        _col_options = sorted(c for c in f.columns if c != "#")
        show_cols = st.multiselect("Columns", _col_options, default=avail_cols, key="scr_cols")
        if show_cols:
            show = f[show_cols]
        else:
            st.caption("No columns selected — showing Symbol / Name only.")
            show = f[['Symbol', 'Name']] if {'Symbol', 'Name'} <= set(f.columns) else f.iloc[:, :2]

        st.markdown(f"**{len(f):,} of {n_total:,} tickers match**")

        def _rate_style(val):
            if pd.isna(val):
                return ''
            try:
                v = float(val)
            except (TypeError, ValueError):
                return ''
            if v >= 90:
                return 'background-color: #14532d; color: #4ade80'
            if v >= 80:
                return 'background-color: #1a3a1a; color: #86efac'
            if v >= 70:
                return 'background-color: #1c3a1c; color: #bbf7d0'
            if v < 30:
                return 'background-color: #450a0a; color: #fca5a5'
            return ''

        styled = show.style.map(
            _rate_style,
            subset=[c for c in ['RS Rating', 'EPS Rating', 'Comp Rating',
                                'RS 3-Month Rating', 'RS 6-Month Rating'] if c in show.columns])
        st.dataframe(styled, use_container_width=True, height=620,
                     column_config={'Current Price': st.column_config.NumberColumn('Current Price', format='$%.2f')})
        st.download_button("📥 Download filtered CSV", show.to_csv(index=False),
                           f"daily_screener_{datetime.now().strftime('%Y%m%d')}.csv", "text/csv")


# ---------- TAB 17: Weekly Screener ----------
with tab17:
    st.subheader("📅 Weekly Screener — Quality Growth Stocks")
    st.markdown(
        "Multi-section screener for weekly review, built on the same `calc_ibd_ratings.py` pipeline "
        "as the Daily Screener. The **Growth 250 Style** thresholds are reverse-engineered from real "
        "MarketSurge Growth 250 exports (Feb/Mar/Apr 2026 snapshots), not guessed. Other sections "
        "draw on classic IBD/O'Neil screening themes (the ~30-screen \"Bill\" list — 3 Weeks Tight, "
        "Top ROE, EPS Accel Surprise, Recent IPO, etc.) and Jeff Sun's momentum/VCP routine "
        "([jfsrev.substack.com](https://jfsrev.substack.com/p/my-trading-tools-process-routine)). "
        "Every section starts from a quality/liquidity floor — the goal is leaders, not junk."
    )

    _WEEKLY_PATH = Path(__file__).resolve().parent / "output" / "daily_screener.csv"
    if not _WEEKLY_PATH.exists():
        st.info("Build the Daily Screener first (📋 Daily Screener tab) — this reads from "
                "`output/daily_screener.csv`.")
    else:
        @st.cache_data(ttl=600, show_spinner=False)
        def load_weekly_screener_data():
            return pd.read_csv(_WEEKLY_PATH, low_memory=False)

        wdf = load_weekly_screener_data()
        _w_need = ['Symbol', 'Name', 'Current Price', 'Market Cap (mil)', 'RS Rating', 'EPS Rating',
                   'Comp Rating', 'SMR Rating', 'A/D Rating', 'A/D Rating - Pr Wk', 'Ind Group Rank',
                   'Industry Name', '50-Day Avg Vol (1000s)', 'ROE', 'AT Margin', 'Pre-tax Margins',
                   'EPS % Chg Last Qtr (-/+)', 'EPS Surprise', 'Avg EPS Surprise 4Q', 'Funds % Increase',
                   'Inst Count', 'Inst % Held', '% Off High', 'Price vs 50-Day', '21 Day ATR %', '30 Day ATR %',
                   '50 Day ATR %', 'Years Since First Cached', '% Chg 5 Days', '% Chg 1 Month',
                   '% Chg 3 Months', '% Chg 6 Months', 'Shrt Int % of Float', 'Shrt Int % Chg']
        _w_missing = [c for c in _w_need if c not in wdf.columns]
        if _w_missing:
            st.warning(f"`daily_screener.csv` is missing {_w_missing} — rebuild it from the "
                       "📋 Daily Screener tab to pick up the new columns.")
        else:
            _AD_ORDER = {"A+": 13, "A": 12, "A-": 11, "B+": 10, "B": 9, "B-": 8, "C+": 7, "C": 6,
                         "C-": 5, "D+": 4, "D": 3, "D-": 2, "E": 1}
            _SMR_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1, "E": 0}

            base = wdf.copy()
            _num_cols = ['RS Rating', 'EPS Rating', 'Comp Rating', 'Current Price', 'Market Cap (mil)',
                         '50-Day Avg Vol (1000s)', 'ROE', 'AT Margin', 'Pre-tax Margins',
                         'EPS % Chg Last Qtr (-/+)', 'EPS Surprise', 'Avg EPS Surprise 4Q', 'Funds % Increase',
                         'Inst Count', 'Inst % Held', '% Off High', 'Price vs 50-Day', '21 Day ATR %',
                         '30 Day ATR %', '50 Day ATR %', 'Years Since First Cached', 'Ind Group Rank',
                         '% Chg 5 Days', '% Chg 1 Month', '% Chg 3 Months', '% Chg 6 Months',
                         'Shrt Int % of Float', 'Shrt Int % Chg']
            for c in _num_cols:
                base[c] = pd.to_numeric(base[c], errors='coerce')
            base['_AD_n'] = base['A/D Rating'].map(_AD_ORDER)
            base['_SMR_n'] = base['SMR Rating'].map(_SMR_ORDER)

            # Universal quality/liquidity floor, applied up front so no individual section can
            # accidentally surface a penny stock or ghost-liquidity name.
            quality_gate = (
                (base['Current Price'] >= 10) &
                (base['50-Day Avg Vol (1000s)'].isna() | (base['50-Day Avg Vol (1000s)'] >= 100)) &
                base['Comp Rating'].notna()
            )
            base = base[quality_gate].copy()
            st.caption(f"Universe after quality/liquidity floor (price ≥ $10, 50-day avg vol ≥ 100K "
                       f"shares when known): **{len(base):,}** of {len(wdf):,} tickers")

            def _weekly_rate_style(val):
                if pd.isna(val):
                    return ''
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    return ''
                if v >= 90:
                    return 'background-color: #14532d; color: #4ade80'
                if v >= 80:
                    return 'background-color: #1a3a1a; color: #86efac'
                if v >= 70:
                    return 'background-color: #1c3a1c; color: #bbf7d0'
                if v < 30:
                    return 'background-color: #450a0a; color: #fca5a5'
                return ''

            def show_section(df_, cols, sort_col, key, n=40, ascending=False, caption=None):
                if df_.empty:
                    st.info("No tickers currently pass this section's criteria.")
                    return
                d = df_.sort_values(sort_col, ascending=ascending).head(n)
                show_cols = [c for c in cols if c in d.columns]
                d_show = _int_ratings(d[show_cols])
                style_cols = [c for c in ['RS Rating', 'EPS Rating', 'Comp Rating'] if c in show_cols]
                styled = d_show.style.map(_weekly_rate_style, subset=style_cols) if style_cols else d_show
                st.dataframe(styled, use_container_width=True, height=420,
                             column_config={'Current Price': st.column_config.NumberColumn('Current Price', format='$%.2f')}
                             if 'Current Price' in show_cols else None)
                if caption:
                    st.caption(caption)
                st.download_button("📥 Download CSV", d_show.to_csv(index=False),
                                   f"weekly_screener_{key}_{datetime.now():%Y%m%d}.csv", "text/csv",
                                   key=f"dl_weekly_{key}")

            sec_tabs = st.tabs([
                "🏆 Growth 250 Style", "💪 CANSLIM Leaders", "🏭 Quality Fundamentals",
                "📈 Accumulation Leaders", "⚡ EPS Growth & Surprise", "🚀 IPO Leaders",
                "🎯 Volatility Contraction", "🔥 Strongest Movers",
                "🌪️ High ADR% (Hottest)", "🩳 High Short Float",
            ])

            # ── 1. Growth 250 Style ──────────────────────────────────────────────
            with sec_tabs[0]:
                st.markdown(
                    "Reverse-engineered from real MarketSurge Growth 250 exports (900 ticker-month "
                    "rows across Feb/Mar/Apr 2026 snapshots, `~/Desktop/stock/List/`, cross-checked "
                    "against a fresh 350-ticker export). A single blended score with one RS floor "
                    "under-covers the real list, because the real list isn't one screen — it's a "
                    "**union of distinct qualifying paths** (the real methodology is a merge of ~30 "
                    "named sub-screens). Splitting real members by what got them in surfaced three "
                    "clear, reusable patterns, each scored independently below:"
                )
                gA = base[base['RS Rating'] >= 90].copy()
                gB = base[(base['EPS Rating'] >= 75) & (base['_SMR_n'] >= 3) &
                          (base['_AD_n'] >= 6) & (base['Comp Rating'] >= 75) &
                          (base['RS Rating'] >= 40)].copy()
                gC = base[(base['Market Cap (mil)'].between(300, 2000)) &
                          (base['EPS Rating'] >= 70) & (base['RS Rating'] >= 70) &
                          (base['Comp Rating'] >= 70)].copy()
                n_union = pd.concat([gA['Symbol'], gB['Symbol'], gC['Symbol']]).nunique()
                st.caption(f"**{n_union:,}** distinct tickers qualify via at least one path "
                           f"(A: {len(gA):,} · B: {len(gB):,} · C: {len(gC):,}).")

                st.markdown("##### Path A — Momentum / Story (RS Rating ≥ 90, EPS not required)")
                show_section(gA, ['Symbol', 'Name', 'RS Rating', 'Comp Rating', 'EPS Rating',
                                   'SMR Rating', 'A/D Rating', 'Ind Group Rank', 'Industry Name',
                                   'Current Price'],
                             'RS Rating', 'growth250_a', n=50,
                             caption="Real analogue: clinical-stage biotech running on catalyst "
                                     "momentum, not earnings (e.g. RS Rating 95-99 with EPS Rating "
                                     "in the single digits).")

                st.divider()
                st.markdown("##### Path B — Quality Compounder (EPS ≥75, SMR A/B, A/D C-or-better, "
                             "Comp ≥75 — RS Rating not required)")
                show_section(gB, ['Symbol', 'Name', 'Comp Rating', 'EPS Rating', 'SMR Rating',
                                   'A/D Rating', 'RS Rating', 'Industry Name', 'Current Price',
                                   'Market Cap (mil)'],
                             'Comp Rating', 'growth250_b', n=50,
                             caption="Real analogue: mega-cap compounders (MSFT, AMZN, BRK.B, V) that "
                                     "qualify on business quality despite RS Rating as low as the 50s.")

                st.divider()
                st.markdown("##### Path C — Cheap-Quality Small-Cap ($300M–$2B, EPS/RS/Comp all ≥70)")
                show_section(gC, ['Symbol', 'Name', 'Comp Rating', 'RS Rating', 'EPS Rating',
                                   'SMR Rating', 'A/D Rating', 'Industry Name', 'Current Price',
                                   'Market Cap (mil)'],
                             'Comp Rating', 'growth250_c', n=50,
                             caption="Real analogue: small, financially sound names strong on every "
                                     "factor at once (e.g. RELL, FCCO, FVCB), not just riding a hot "
                                     "sector.")

            # ── 2. CANSLIM Leaders ───────────────────────────────────────────────
            with sec_tabs[1]:
                st.markdown("**Bill 04 / Bill 30 style**: Comp Rating and RS Rating both above the "
                            "same round threshold — the simplest two-factor screen on the list.")
                b8080 = base[(base['Comp Rating'] > 80) & (base['RS Rating'] > 80)].copy()
                b9090 = base[(base['Comp Rating'] > 90) & (base['RS Rating'] > 90)].copy()
                col_8080, col_9090 = st.columns(2)
                with col_8080:
                    st.markdown("##### 80 / 80 (Comp > 80 and RS > 80)")
                    show_section(b8080, ['Symbol', 'Name', 'Comp Rating', 'RS Rating', 'EPS Rating',
                                          'Current Price'],
                                 'Comp Rating', '8080', n=25)
                with col_9090:
                    st.markdown("##### 90 / 90 (Comp > 90 and RS > 90)")
                    show_section(b9090, ['Symbol', 'Name', 'Comp Rating', 'RS Rating', 'EPS Rating',
                                          'Current Price'],
                                 'Comp Rating', '9090', n=25)

                st.divider()
                st.markdown(
                    "**Bill 12/13/15/16 style** (Strong Group, Top EPS, Top Comp, Top RS): all four "
                    "pillars genuinely strong at once, not just a high composite masking one weak leg."
                )
                c = base[(base['Comp Rating'] >= 90) & (base['RS Rating'] >= 90) &
                         (base['EPS Rating'] >= 80)].copy()
                show_section(c, ['Symbol', 'Name', 'Comp Rating', 'RS Rating', 'EPS Rating',
                                  'SMR Rating', 'A/D Rating', 'Ind Group Rank', 'Industry Name',
                                  'Current Price'],
                             'Comp Rating', 'canslim', n=50)

            # ── 3. Quality Fundamentals ──────────────────────────────────────────
            with sec_tabs[2]:
                st.markdown(
                    "**Bill 11/14/22/24/25 style — avoid the junk.** O'Neil's classic ROE ≥17% "
                    "quality bar, A/B SMR (sales + margins + ROE all sound), and EPS Rating >1 "
                    "(excludes negative/no-earnings names). Deliberately doesn't require high RS — "
                    "this is about business quality independent of whether the stock is moving now."
                )
                # ROE/margins are NI-over-(equity|revenue) ratios: a company with near-zero or
                # negative book equity/revenue (heavy buybacks, micro-caps) blows these up to
                # thousands of percent - real, not a display bug (e.g. MAS showed ROE=5862.5%
                # from Masco's buyback-shrunk equity base). The core RS/EPS/SMR/Comp ratings
                # already guard against this via log-compression inside calc_ibd_ratings.py, but
                # this screen filters on the RAW figures directly, so it needs its own sanity caps
                # - a real "quality" company's ROE/margins essentially never legitimately exceed
                # a couple hundred percent.
                q = base[(base['ROE'] >= 17) & (base['ROE'] <= 200) &
                         (base['AT Margin'].between(0, 100)) &
                         (base['Pre-tax Margins'].between(0, 100)) &
                         (base['_SMR_n'] >= 3) & (base['EPS Rating'] > 1)].copy()
                show_section(q, ['Symbol', 'Name', 'ROE', 'AT Margin', 'Pre-tax Margins', 'SMR Rating',
                                  'EPS Rating', 'Comp Rating', 'RS Rating', 'Current Price'],
                             'ROE', 'quality', n=50)

            # ── 4. Accumulation Leaders ──────────────────────────────────────────
            with sec_tabs[3]:
                st.markdown(
                    "**Bill 21 style**: strongest institutional accumulation (A/D Rating A- or "
                    "better) with rising fund sponsorship — buyers actually stepping in, not just a "
                    "price move."
                )
                # Funds % Increase is new-position-count over old-position-count - a fund going
                # from a handful of shares to a few more reads as a multi-million-percent
                # "increase" (max observed: 2,872,824%). Same near-zero-denominator pattern as
                # ROE/margins above; capped so the ranking reflects real sponsorship growth.
                #
                # "Number of Funds" is NOT a real holder count - it's len(yfinance's
                # mutualfund_holders), which is Yahoo's top-10-holders *snapshot table*, hard-capped
                # at 10 rows. Any stock with >=10 tracked mutual-fund holders shows exactly 10, and
                # since this section already filters to A- or better (large, liquid, well-covered
                # names), 575/650 (88%) of the eligible universe hits that cap - hence "all 10".
                # "Inst Count" (yfinance's institutionsCount, from the separate major_holders table)
                # is the real aggregate count of institutional 13F filers - not top-10-capped - and
                # actually differentiates within this group (3 to 1700+ in spot checks). "Inst % Held"
                # (institutionsPercentHeld, same table) is the matching aggregate ownership percent -
                # a handful of tickers have corrupted upstream values (>100%, one as high as 96,525%),
                # so it's capped at a generous 200% for display; genuine heavy ETF/fund overlap can
                # push a name slightly past 100% without being bad data.
                a = base[base['_AD_n'] >= 11].copy()
                a['Funds % Increase'] = pd.to_numeric(a['Funds % Increase'], errors='coerce').clip(-95, 500)
                a['Inst Count'] = pd.to_numeric(a['Inst Count'], errors='coerce').astype('Int64')
                a['Inst % Held'] = pd.to_numeric(a['Inst % Held'], errors='coerce').clip(0, 200)
                show_section(a, ['Symbol', 'Name', 'A/D Rating', 'A/D Rating - Pr Wk',
                                  'Funds % Increase', 'Inst Count', 'Inst % Held', 'Comp Rating',
                                  'RS Rating', 'Current Price'],
                             'Funds % Increase', 'accumulation', n=50)

            # ── 5. EPS Growth & Surprise ─────────────────────────────────────────
            with sec_tabs[4]:
                st.markdown(
                    "**Bill 19/20/27 style**: recent-quarter EPS growth plus a track record of "
                    "beating analyst estimates — the earnings-side confirmation CANSLIM's \"E\" is "
                    "built on. Originally titled \"EPS Accel\", but `EPS Accel 3 Qtrs` (and every "
                    "multi-quarter-comparison column feeding it) turned out to be **100% empty** "
                    "in `daily_screener.csv` — it needs 6+ trailing quarters and yfinance only "
                    "carries ~5, the same depth limit documented in the ratings work. This uses "
                    "`EPS % Chg Last Qtr` (most recent quarter's YoY growth) instead, winsorized "
                    "at ±300% — like `ROE` above, a quarter's growth off a near-zero prior-year "
                    "base can otherwise read as thousands of percent."
                )
                e = base.copy()
                e['EPS % Chg Last Qtr (-/+)'] = pd.to_numeric(
                    e['EPS % Chg Last Qtr (-/+)'], errors='coerce').clip(-300, 300)
                e['EPS Surprise'] = pd.to_numeric(e['EPS Surprise'], errors='coerce').clip(-100, 300)
                e['Avg EPS Surprise 4Q'] = pd.to_numeric(
                    e['Avg EPS Surprise 4Q'], errors='coerce').clip(-100, 300)
                e = e[(e['EPS % Chg Last Qtr (-/+)'] > 0) & (e['EPS Rating'] >= 70)]
                show_section(e, ['Symbol', 'Name', 'EPS % Chg Last Qtr (-/+)', 'EPS Surprise',
                                  'Avg EPS Surprise 4Q', 'EPS Rating', 'Comp Rating', 'RS Rating',
                                  'Current Price'],
                             'EPS % Chg Last Qtr (-/+)', 'epsgrowth', n=50)

            # ── 6. IPO Leaders ───────────────────────────────────────────────────
            with sec_tabs[5]:
                st.markdown(
                    "**Bill 03/07/08/09 style.** Recent IPOs often run on RS/story since they rarely "
                    "have full earnings history yet; the \"8-Year Club\" reflects O'Neil's "
                    "observation that many all-time-great winners hit stride 6-10 years post-listing "
                    "as institutional sponsorship builds. `Years Since First Cached` is a proxy for "
                    "listing age (first date in `ticker_cache`), not a verified IPO date."
                )
                recent = base[(base['Years Since First Cached'] <= 3) & (base['RS Rating'] >= 80) &
                              (base['Comp Rating'] >= 70)].copy()
                veteran = base[(base['Years Since First Cached'] >= 6) &
                               (base['Years Since First Cached'] <= 10) & (base['RS Rating'] >= 85) &
                               (base['Comp Rating'] >= 85)].copy()
                st.markdown("##### Recent IPO Leaders (≤3 yrs listed, RS ≥80, Comp ≥70)")
                show_section(recent, ['Symbol', 'Name', 'Years Since First Cached', 'RS Rating',
                                       'Comp Rating', 'EPS Rating', 'Industry Name', 'Current Price'],
                             'RS Rating', 'ipo_recent', n=30)
                st.markdown("##### 8-Year Club (6-10 yrs listed, RS ≥85, Comp ≥85)")
                show_section(veteran, ['Symbol', 'Name', 'Years Since First Cached', 'RS Rating',
                                        'Comp Rating', 'EPS Rating', 'SMR Rating', 'Industry Name',
                                        'Current Price'],
                             'Comp Rating', 'ipo_veteran', n=30)

            # ── 7. Volatility Contraction ────────────────────────────────────────
            with sec_tabs[6]:
                st.markdown(
                    "**Jeff Sun / Minervini VCP style**: current volatility (21-day ATR%) "
                    "meaningfully below its own 50-day baseline, price still holding near the "
                    "50-day MA and within 25% of the 52-week high — a tightening base near highs "
                    "(the pre-breakout signature), not a stock that's simply gone quiet in a "
                    "downtrend."
                )
                v = base.copy()
                v['_contraction_ratio'] = (v['21 Day ATR %'] / v['50 Day ATR %']).round(3)
                v = v[(v['_contraction_ratio'] < 0.75) & (v['% Off High'] < 25) &
                      (v['Price vs 50-Day'] > -5) & (v['RS Rating'] >= 70)]
                show_section(v, ['Symbol', 'Name', '_contraction_ratio', '21 Day ATR %',
                                  '50 Day ATR %', '% Off High', 'Price vs 50-Day', 'RS Rating',
                                  'Comp Rating', 'Current Price'],
                             '_contraction_ratio', 'vcp', n=50, ascending=True,
                             caption="Sorted tightest-first (lowest 21D/50D ATR% ratio).")

            # ── 8. Strongest Movers ──────────────────────────────────────────────
            with sec_tabs[7]:
                st.markdown(
                    "**Jeff Sun style momentum leaderboard**, quality-gated first (Comp ≥70) so "
                    "this surfaces genuine leaders extending, not penny-stock spikes."
                )
                m_ = base[base['Comp Rating'] >= 70].copy()
                mv_tabs = st.tabs(["1 Week", "1 Month", "3 Month", "6 Month"])
                mv_cols = ['% Chg 5 Days', '% Chg 1 Month', '% Chg 3 Months', '% Chg 6 Months']
                for mv_tab, mv_col in zip(mv_tabs, mv_cols):
                    with mv_tab:
                        show_section(m_, ['Symbol', 'Name', mv_col, 'RS Rating', 'Comp Rating',
                                           'Industry Name', 'Current Price'],
                                     mv_col, f'movers_{mv_col}', n=30)

            # ── 9. High ADR% (Hottest Stocks) ────────────────────────────────────
            with sec_tabs[8]:
                st.markdown(
                    "**Jeff Sun's \"High ADR% Hottest Stocks\" screener** — the opposite framing "
                    "from Volatility Contraction: stocks with the widest daily trading ranges, "
                    "quality-gated (Comp ≥70, RS ≥70) so this is aggressive momentum candidates "
                    "among real leaders, not random high-beta junk. Jeff Sun's exact numeric "
                    "thresholds live in linked X/Twitter posts I couldn't retrieve (404s — likely "
                    "JS-rendered or auth-walled); this reproduces the *theme* using `21 Day ATR %`, "
                    "not a verified replica of his exact filter."
                )
                adr = base[(base['Comp Rating'] >= 70) & (base['RS Rating'] >= 70)].copy()
                show_section(adr, ['Symbol', 'Name', '21 Day ATR %', '30 Day ATR %', 'RS Rating',
                                    'Comp Rating', 'Industry Name', 'Current Price'],
                             '21 Day ATR %', 'high_adr', n=40,
                             caption="Sorted highest-ATR%-first.")

            # ── 10. High Short Float ─────────────────────────────────────────────
            with sec_tabs[9]:
                st.markdown(
                    "**Jeff Sun's weekly \"High Short Float\" screener** — heavily-shorted names "
                    "among quality growth stocks (Comp ≥70), a classic squeeze-candidate list: "
                    "strong fundamentals/technicals that a crowded short book could accelerate on "
                    "a breakout. Exact thresholds from the source screener weren't retrievable "
                    "(same X/Twitter access limitation as the High ADR% section above); this uses "
                    "`Shrt Int % of Float` directly."
                )
                short_ = base[(base['Comp Rating'] >= 70) & base['Shrt Int % of Float'].notna()].copy()
                show_section(short_, ['Symbol', 'Name', 'Shrt Int % of Float', 'Shrt Int % Chg',
                                       'RS Rating', 'Comp Rating', 'Industry Name', 'Current Price'],
                             'Shrt Int % of Float', 'short_float', n=40)


# Footer
st.divider()
footer_text = "**Daily Relative Strength Analysis Dashboard**"
if has_historical:
    footer_text += " | Historical Data: Oct 2021 - Present"
footer_text += " | Built with Streamlit"
st.markdown(footer_text)
