#!/usr/bin/env python3
"""
Sync IBD Live show summaries from ~/Documents/Zoom into IBD/live_summaries/.

Each Zoom recording folder (e.g. "2026-07-30 10.18.28 IBD Live") contains a
summary_<date>.md file. This script copies that markdown as-is into
IBD/live_summaries/<date>.md and derives a <date>.json sidecar (ticker list,
per-ticker table rows, market pulse headline) by parsing the markdown, so the
Streamlit app's IBD Live tab has a fast local JSON to read without re-parsing
markdown on every run.

Safe to re-run: only copies/re-parses a summary if the source file is newer
than (or missing from) the destination.
"""
import json
import re
import shutil
from pathlib import Path

ZOOM_DIR = Path.home() / "Documents" / "Zoom"
REPO_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_DIR / "IBD" / "live_summaries"

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")


def find_summary_files():
    """Yield (date_str, summary_path, transcript_path_or_none) for each Zoom folder."""
    if not ZOOM_DIR.exists():
        return
    for folder in sorted(ZOOM_DIR.glob("* IBD Live")):
        if not folder.is_dir():
            continue
        summaries = sorted(folder.glob("summary_*.md"))
        if not summaries:
            continue
        summary_path = summaries[-1]
        m = DATE_RE.search(summary_path.stem)
        if not m:
            m = DATE_RE.search(folder.name)
        if not m:
            continue
        date_str = m.group(1)
        transcripts = sorted(
            [p for p in folder.glob("*.txt") if not p.name.startswith("summary_")],
            key=lambda p: p.name,
        )
        transcript_path = transcripts[-1] if transcripts else None
        yield date_str, summary_path, transcript_path


def parse_section(lines, heading_prefixes):
    """Return the lines belonging to the first section whose heading starts with
    any of heading_prefixes, up to (not including) the next '## ' heading."""
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and any(line[3:].strip().startswith(p) for p in heading_prefixes):
            start = i + 1
            break
    if start is None:
        return []
    end = start
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return lines[start:end]


def parse_market_summary(lines):
    section = parse_section(lines, ["1. Market Pulse", "Market Pulse"])
    for line in section:
        line = line.strip()
        if line.startswith("-"):
            text = line.lstrip("-").strip()
            text = re.sub(r"^\*\*(.+?)\*\*:?\s*", r"\1: ", text)
            return text.strip()
    for line in section:
        if line.strip():
            return line.strip()
    return ""


def parse_ticker_table(lines):
    """Parse the '2. Top Tickers & Technical Setups' markdown table into
    {ticker: {actionability, technical_action, story}}, preserving first-seen order."""
    section = parse_section(lines, ["2. Top Tickers", "Top Tickers"])
    details = {}
    header_cols = None
    for line in section:
        m = TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if header_cols is None:
            header_cols = [c.lower() for c in cells]
            continue
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue  # separator row
        row = dict(zip(header_cols, cells))
        ticker = (row.get("ticker symbol") or row.get("ticker") or "").upper().strip()
        if not ticker or ticker in details:
            continue
        details[ticker] = {
            "actionability": row.get("actionability") or row.get("status", ""),
            "technical_action": row.get("technical action", ""),
            "story": row.get("the story (catalyst & fundamental rationale)") or row.get("story", ""),
        }
    return details


def parse_ticker_list(lines, fallback_details):
    section = parse_section(lines, ["7. Consolidated Ticker List", "7. Full Ticker List",
                                     "Consolidated Ticker List", "Full Ticker List"])
    for line in section:
        line = line.strip()
        if line and "," in line or (line and not line.startswith("#")):
            if line:
                tickers = [t.strip().upper() for t in line.split(",") if t.strip()]
                if tickers:
                    return tickers
    return list(fallback_details.keys())


def build_sidecar(date_str, summary_path, transcript_path):
    text = summary_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    ticker_details = parse_ticker_table(lines)
    tickers = parse_ticker_list(lines, ticker_details)
    market_summary = parse_market_summary(lines)
    return {
        "date": date_str,
        "source_file": str(transcript_path) if transcript_path else str(summary_path),
        "market_summary": market_summary,
        "tickers": tickers,
        "ticker_details": ticker_details,
        "sector_leaders": [],
        "sector_laggards": [],
    }


def sync_ibd_live_summaries(verbose=False):
    """Copy/parse any new-or-updated Zoom summaries into IBD/live_summaries.

    Safe to call on every app launch: skipped entries are just an mtime check per
    folder, so a no-op run is cheap. Returns (synced_count, skipped_count, synced_dates).
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    synced, skipped, synced_dates = 0, 0, []
    for date_str, summary_path, transcript_path in find_summary_files():
        dest_md = OUT_DIR / f"{date_str}.md"
        dest_json = OUT_DIR / f"{date_str}.json"
        if dest_md.exists() and dest_md.stat().st_mtime >= summary_path.stat().st_mtime and dest_json.exists():
            skipped += 1
            continue
        shutil.copyfile(summary_path, dest_md)
        sidecar = build_sidecar(date_str, summary_path, transcript_path)
        with open(dest_json, "w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2)
        synced += 1
        synced_dates.append(date_str)
        if verbose:
            print(f"synced {date_str}  ({len(sidecar['tickers'])} tickers)")
    if verbose:
        print(f"\nDone. {synced} synced, {skipped} already up to date. Output: {OUT_DIR}")
    return synced, skipped, synced_dates


if __name__ == "__main__":
    sync_ibd_live_summaries(verbose=True)
