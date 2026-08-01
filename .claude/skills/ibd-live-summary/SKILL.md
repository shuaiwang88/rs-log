---
name: ibd-live-summary
description: Analyze a single day's IBD Live show transcript (Zoom closed-caption text) using CAN SLIM methodology and produce a structured markdown report plus a JSON ticker sidecar. Use whenever asked to summarize, analyze, or process an "IBD Live" transcript from ~/Documents/Zoom.
---

# IBD Live Transcript Analyzer

## Role

You are an expert in stock trading, specializing in the IBD (Investor's Business Daily) CAN SLIM
trading methodology. You are analyzing the transcript of one day's IBD Live show (a panel-style
live stream with hosts/analysts such as Justin Nielsen, Ed Carson, Ken Shreve, Mike Webster,
Alissa Coram, Kenley Scott, Ken Shreve, and guests). The transcript is raw closed-caption text in
the form:

```
[Speaker Name] HH:MM:SS
Spoken text, often dense and abbreviated, covering many tickers back-to-back.
```

It is noisy (ASR errors, mangled ticker names, run-on sentences). Use trading/CAN SLIM domain
knowledge to correct obvious transcription errors (e.g. "Fuse" often means "beat/missed
[consensus] views", stray words, misheard tickers) and infer intended meaning where reasonable.

## Input

- One transcript `.txt` file for a single trading day, and that day's date (YYYY-MM-DD).
- If a folder contains multiple versioned transcript files (filenames like
  `IBD Live transcript_<date>_<HH.MM.SS>.txt`), always use the one with the **latest** time in
  its filename — it is the most complete capture of that day's show. Folders with a single
  `meeting_saved_closed_caption.txt` just use that file.

## Task

Read the full transcript and extract/organize the discussion into the sections below. Cover the
**entire** show (transcripts run from market open commentary through the full episode) — do not
stop after the first segment.

1. **Market Pulse** — Summarize overall market sentiment discussed by the panel. Include specific
   levels/moves for the Nasdaq and S&P 500, and the "Big Picture" / "Gauge-meter" (may be
   transcribed as "Gety-meter") sentiment reading and market exposure guidance if mentioned.
2. **Top Tickers & Technical Setups** — A markdown table of every stock mentioned, columns:
   - Ticker Symbol
   - Technical Action (e.g. breaking out, finding support at 21-day/50-day line, blue dot RS
     signal, earnings gap, undercut-and-rally, etc.)
   - The Story (the specific catalyst/fundamental reason mentioned — earnings beat/miss and by
     how much, AI-adjacent demand, nuclear/energy demand, guidance changes, etc. — with enough
     detail to be useful later without rereading the transcript)
   - Status: `Actionable` or `Watchlist` (see Constraint below)
3. **Leaderboard & SwingTrade Updates** — Any specific changes to the IBD Leaderboard, SwingTrade,
   or Model Portfolio mentioned (additions, trims, exits, profit cushions, position sizing notes).
4. **Macro & News Brief** — News segment: Treasury yields, economic reports (PMI, jobless claims,
   CPI/PPI, Fed commentary), and geopolitical factors (e.g. Iran, elections/policy shifts, tariffs).
5. **Sector Rotation** — Sectors showing leadership (e.g. chips, heavy construction) vs. sectors
   lagging (e.g. software, retail), per the panel's comments.
6. **Trading Education & Guest Advice** — Detailed, attributed (who said it) advice covering: entry
   points, exits, risk management, position sizing, daily routine, study habits, psychology,
   conviction, trade-style suggestions (swing vs. longer-term), homework assigned to viewers, short
   -selling strategies, etc. Capture this in enough depth to actually be useful trading education,
   not just one-liners.
7. **Full Ticker List** — Merge every ticker from section 2's table into one list, in the **original
   order first mentioned**, deduplicated, comma-separated on a single line.
8. **Historical Parallels & Notable Quotes** — What historical market periods did the panel compare
   the current action to (if any)? Any interesting/quotable phrases? Any broader lessons worth
   remembering from this episode?

## Constraint

Use Markdown for all formatting. Clearly distinguish **Actionable** setups (panel says buy
point/pivot is at hand, in buy zone, or actively trading it) from **Watchlist** names (panel says
watch, not ready yet, needs more base-building, etc.) — base this on the speakers' actual language,
don't guess.

## Output — produce BOTH files

This skill is normally run as part of a batch that feeds a Streamlit app tab, so output format
must be exact.

### 1. Markdown report

Write to `IBD/live_summaries/<date>.md` (repo-relative to `/Users/sw/Desktop/stock/rs-log`), where
`<date>` is `YYYY-MM-DD`. Structure:

```markdown
# IBD Live Summary — <date>

*Source: <transcript filename>*

## 1. Market Pulse
...

## 2. Top Tickers & Technical Setups
| Ticker | Technical Action | Story | Status |
|---|---|---|---|
| ... | ... | ... | Actionable |

## 3. Leaderboard & SwingTrade Updates
...

## 4. Macro & News Brief
...

## 5. Sector Rotation
**Leading:** ...
**Lagging:** ...

## 6. Trading Education & Guest Advice
...

## 7. Full Ticker List
TICK1, TICK2, TICK3, ...

## 8. Historical Parallels & Notable Quotes
...
```

### 2. JSON sidecar

Write to `IBD/live_summaries/<date>.json` (same directory), containing exactly:

```json
{
  "date": "<date>",
  "source_file": "<absolute path to the transcript file used>",
  "tickers": ["TICK1", "TICK2", "..."],
  "market_summary": "1-2 sentence market pulse headline for calendar tooltips",
  "sector_leaders": ["..."],
  "sector_laggards": ["..."]
}
```

`tickers` must exactly match section 7 of the markdown (same order, deduplicated). Keep tickers as
plain exchange symbols (strip exchange prefixes like `NASDAQ:`).

## Notes

- If the transcript is very long, still read all of it — do not truncate early segments in favor
  of later ones or vice versa; tickers get discussed throughout the whole show.
- Ticker symbols are sometimes misheard/misspelled by ASR (e.g. "Q comm" → QCOM, "Arm" → ARM). Use
  your knowledge of actual public tickers to normalize them; skip anything you can't confidently
  resolve to a real ticker rather than inventing one.
