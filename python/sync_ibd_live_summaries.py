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
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
EMPHASIS_RE = re.compile(r"[*_`]+")
SEPARATOR_CELL_RE = re.compile(r":?-{2,}:?")
TICKER_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,6}")


def strip_md(text):
    """Drop markdown emphasis/backticks and collapse whitespace."""
    return re.sub(r"\s+", " ", EMPHASIS_RE.sub("", text or "")).strip()


def parse_heading(line):
    """(level, plain title) for an ATX heading, else None. Summaries pasted into the
    app's ingest box use '###' where Zoom-synced ones use '##', so accept any level."""
    m = HEADING_RE.match(line.strip())
    return (len(m.group(1)), strip_md(m.group(2))) if m else None


def clean_ticker(cell, strict=False):
    """'**GOOGL** (Alphabet)' -> 'GOOGL'. Returns '' when the text isn't a symbol.
    strict=True also rejects anything with leftover words, so prose lines in the
    ticker-list section can't masquerade as symbols."""
    text = re.sub(r"\(.*?\)", " ", strip_md(cell)).replace("/", " ").strip()
    words = text.split()
    if not words or (strict and len(words) > 1):
        return ""
    token = words[0].upper().strip(".,;:")
    return token if TICKER_TOKEN_RE.fullmatch(token) else ""


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


def _folder_time_slug(folder):
    """Recording time from a Zoom folder name ('2026-07-30 10.18.28 IBD Live' ->
    '10_18_28'), or '' when the name has no time. Used to give later recordings of the
    same date their own source slot instead of overwriting the primary one."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", folder.name)
    if m:
        return f"{m.group(1)}_{m.group(2)}_{m.group(3)}"
    m = re.search(r"(\d{2})\.(\d{2})", folder.name)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    return ""


def parse_section(lines, keywords):
    """Return the lines belonging to the first section whose heading contains any of
    keywords (lowercase, matched after stripping a '3.'-style number prefix), up to
    the next heading at the same or shallower level so sub-headings stay inside."""
    start = start_level = None
    for i, line in enumerate(lines):
        heading = parse_heading(line)
        if not heading:
            continue
        level, title = heading
        title = re.sub(r"^\d+[.)]\s*", "", title.lower())
        if any(k in title for k in keywords):
            start, start_level = i + 1, level
            break
    if start is None:
        return []
    end = start
    while end < len(lines):
        heading = parse_heading(lines[end])
        if heading and heading[0] <= start_level:
            break
        end += 1
    return lines[start:end]


def parse_market_summary(lines):
    section = parse_section(lines, ["market pulse"])
    for line in section:
        line = line.strip()
        if line.startswith("-"):
            text = line.lstrip("-").strip()
            # '**Overall Sentiment:** Cautiously bullish' -> 'Overall Sentiment: Cautiously
            # bullish' — the colon sits inside the bold as often as outside it.
            text = re.sub(r"^\*\*(.+?)\*\*:?\s*", lambda m: m.group(1).rstrip(":") + ": ", text)
            return text.strip()
    for line in section:
        if line.strip():
            return line.strip()
    return ""


def find_column(header_cols, *keywords):
    """Index of the header cell matching the earliest-listed keyword, else None. Column
    titles drift between shows ('Story' vs 'The Story / Catalyst'), so match on
    substrings — and try keywords in priority order, since a loose one like 'action'
    would otherwise claim an 'Actionability' column ahead of 'Technical Action'."""
    for keyword in keywords:
        for i, col in enumerate(header_cols):
            if keyword in col:
                return i
    return None


def parse_ticker_table(lines):
    """Parse the '2. Top Tickers & Technical Setups' markdown table into
    {ticker: {actionability, technical_action, story}}, preserving first-seen order."""
    section = parse_section(lines, ["top tickers"])
    details = {}
    cols = None
    for line in section:
        m = TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        cells = [strip_md(c) for c in m.group(1).split("|")]
        if cells and all(SEPARATOR_CELL_RE.fullmatch(c) for c in cells if c):
            continue  # |---|---| separator row
        if cols is None:
            lower = [c.lower() for c in cells]
            cols = {
                "ticker": find_column(lower, "ticker", "symbol"),
                "technical_action": find_column(lower, "technical", "action"),
                "story": find_column(lower, "story", "catalyst"),
                "actionability": find_column(lower, "actionab", "status"),
            }
            continue

        def cell(name):
            i = cols.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        ticker = clean_ticker(cell("ticker"))
        if not ticker or ticker in details:
            continue
        details[ticker] = {
            "actionability": cell("actionability"),
            "technical_action": cell("technical_action"),
            "story": cell("story"),
        }
    return details


def parse_ticker_list(lines, fallback_details):
    """Symbols from the '7. … Ticker List' section, in order of first mention. The
    heading is variously 'Full', 'Consolidated' or 'Merged', and the list can wrap
    across several lines, so collect every comma-separated line in the section."""
    section = parse_section(lines, ["ticker list"])
    tickers = []
    for line in section:
        line = strip_md(line).lstrip("-•* ").strip()
        if not line or line.startswith("|") or line.startswith("#"):
            continue
        parts = [p for p in line.split(",") if p.strip()]
        found = [t for t in (clean_ticker(p, strict=True) for p in parts) if t]
        if len(found) >= max(1, len(parts) // 2):  # a real list, not a prose sentence
            tickers.extend(found)
    tickers = list(dict.fromkeys(tickers))
    return tickers or list(fallback_details.keys())


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

    Folders are grouped by date: the first (earliest) recording of a date keeps the
    primary slot (<date>.md/.json), and any later recordings of the same date become
    named sources (<date>__<recording-time>.md/.json) so they never overwrite each
    other — mirroring the multi-source layout the app's ingest box uses.

    Safe to call on every app launch: skipped entries are just an mtime check per
    file, so a no-op run is cheap. Returns (synced_count, skipped_count, synced_dates).
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    synced, skipped, synced_dates = 0, 0, []
    by_date = {}
    for date_str, summary_path, transcript_path in find_summary_files():
        by_date.setdefault(date_str, []).append((summary_path, transcript_path))
    for date_str in sorted(by_date):
        for i, (summary_path, transcript_path) in enumerate(by_date[date_str]):
            if i == 0:
                suffix = ""
            else:
                slug = _folder_time_slug(summary_path.parent)
                suffix = "__" + (slug or f"recording_{i + 1}")
            dest_md = OUT_DIR / f"{date_str}{suffix}.md"
            dest_json = OUT_DIR / f"{date_str}{suffix}.json"
            if dest_md.exists() and dest_md.stat().st_mtime >= summary_path.stat().st_mtime and dest_json.exists():
                skipped += 1
                continue
            shutil.copyfile(summary_path, dest_md)
            sidecar = build_sidecar(date_str, summary_path, transcript_path)
            sidecar["source"] = suffix[2:] if suffix else ""
            sidecar["kind"] = "live"
            with open(dest_json, "w", encoding="utf-8") as f:
                json.dump(sidecar, f, indent=2)
            synced += 1
            if date_str not in synced_dates:
                synced_dates.append(date_str)  # one entry per date, even with several sources
            if verbose:
                print(f"synced {date_str}{suffix or ''}  ({len(sidecar['tickers'])} tickers)")
    if verbose:
        print(f"\nDone. {synced} synced, {skipped} already up to date. Output: {OUT_DIR}")
    return synced, skipped, synced_dates


if __name__ == "__main__":
    sync_ibd_live_summaries(verbose=True)
