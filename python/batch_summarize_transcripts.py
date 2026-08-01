#!/usr/bin/env python3
"""
Batch-process all IBD Live transcripts from ~/Documents/Zoom into structured
markdown + JSON summaries using the ibd-live-summary skill methodology.

Uses Google Gemini API (free tier, large context window). Requires:
    pip install google-generativeai
    export GEMINI_API_KEY="your-key"

Also supports Anthropic Claude and OpenAI as fallback providers.

Usage:
    python python/batch_summarize_transcripts.py           # process all new/updated
    python python/batch_summarize_transcripts.py --force   # reprocess ALL
    python python/batch_summarize_transcripts.py --dry-run  # preview only
    python python/batch_summarize_transcripts.py --date 2026-07-31  # single date
    python python/batch_summarize_transcripts.py --provider anthropic  # use Claude
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ZOOM_DIR = Path.home() / "Documents" / "Zoom"
REPO_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_DIR / "IBD" / "live_summaries"
CHECKPOINT_PATH = OUT_DIR / "._batch_checkpoint.json"

# Model configuration per provider
PROVIDER_CONFIG = {
    "gemini": {
        "model": "gemini-2.5-pro-exp-03-25",
        "fallback": "gemini-2.0-flash",
        "env_key": "GEMINI_API_KEY",
        "install": "pip install google-generativeai",
    },
    "anthropic": {
        "model": "claude-sonnet-4-20250514",
        "env_key": "ANTHROPIC_API_KEY",
        "install": "pip install anthropic",
    },
    "openai": {
        "model": "gpt-4o",
        "env_key": "OPENAI_API_KEY",
        "install": "pip install openai",
    },
}

# ---------------------------------------------------------------------------
# System prompt: ibd-live-summary skill (embedded)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert in stock trading, specializing in the IBD (Investor's Business Daily) CAN SLIM
trading methodology. You are analyzing the transcript of one day's IBD Live show (a panel-style
live stream with hosts/analysts such as Justin Nielsen, Ed Carson, Ken Shreve, Mike Webster,
Alissa Coram, Kenley Scott, and guests). The transcript is raw closed-caption text in the form:

```
[Speaker Name] HH:MM:SS
Spoken text, often dense and abbreviated, covering many tickers back-to-back.
```

It is noisy (ASR errors, mangled ticker names, run-on sentences). Use trading/CAN SLIM domain
knowledge to correct obvious transcription errors (e.g. "Fuse" often means "beat/missed
[consensus] views", stray words, misheard tickers) and infer intended meaning where reasonable.

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

## Output Format — produce BOTH sections in your response

### Part A: Markdown Report

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

### Part B: JSON Sidecar (wrapped in ```json fence)

```json
{
  "date": "<date>",
  "source_file": "<transcript path>",
  "market_summary": "1-2 sentence market pulse headline for calendar tooltips",
  "tickers": ["TICK1", "TICK2", "..."],
  "ticker_details": {
    "TICK1": {"actionability": "Actionable", "technical_action": "...", "story": "..."},
    ...
  },
  "sector_leaders": ["..."],
  "sector_laggards": ["..."]
}
```

`tickers` must exactly match section 7 of the markdown (same order, deduplicated). the
`ticker_details` dict must correspond exactly to the rows in section 2's table. Keep tickers as
plain exchange symbols (strip exchange prefixes like `NASDAQ:`).

## Notes

- If the transcript is very long, still read all of it — do not truncate early segments.
- Ticker symbols are sometimes misheard by ASR (e.g. "Q comm" → QCOM, "Arm" → ARM). Use
  your knowledge of actual public tickers to normalize them; skip anything you can't confidently
  resolve to a real ticker rather than inventing one.
- **CRITICAL**: Your response must contain BOTH the markdown report AND the JSON sidecar.
  The JSON must be wrapped in a ```json code fence for easy parsing.
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_transcripts():
    """Yield (date_str, transcript_path) for each Zoom IBD Live folder, using
    the latest/largest transcript file per folder."""
    if not ZOOM_DIR.exists():
        return
    for folder in sorted(ZOOM_DIR.glob("*IBD Live*")):
        if not folder.is_dir():
            continue
        # Extract date from folder name
        m = re.match(r"(\d{4}-\d{2}-\d{2})", folder.name)
        if not m:
            continue
        date_str = m.group(1)

        # Find all .txt files (exclude summary_*.md)
        txt_files = sorted(
            [p for p in folder.glob("*.txt") if not p.name.startswith("summary_")],
            key=lambda p: p.name,  # sort by filename (contains timestamp like _11.19.23)
        )
        if not txt_files:
            continue
        transcript = txt_files[-1]  # Use latest timestamp in filename per skill spec
        yield date_str, transcript


def load_checkpoint():
    """Return set of dates already processed."""
    if CHECKPOINT_PATH.exists():
        try:
            return set(json.loads(CHECKPOINT_PATH.read_text()))
        except Exception:
            return set()
    return set()


def save_checkpoint(processed_dates):
    """Persist set of processed dates."""
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(sorted(processed_dates), indent=2))


def parse_llm_response(text):
    """Extract markdown report and JSON sidecar from LLM response."""
    markdown = text
    sidecar = None

    # Try to extract JSON from ```json fence
    json_match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if json_match:
        try:
            sidecar = json.loads(json_match.group(1))
            # Remove the JSON fence from markdown
            markdown = text[:json_match.start()].strip() + "\n\n" + text[json_match.end():].strip()
        except json.JSONDecodeError as e:
            print(f"    Warning: JSON parse from fence failed: {e}")

    # Fallback: find balanced JSON object containing "date" and "tickers"
    if sidecar is None:
        for match in re.finditer(r'\{', text):
            start = match.start()
            depth = 0
            end = start
            for i, ch in enumerate(text[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    candidate = json.loads(text[start:end])
                    if isinstance(candidate, dict) and "date" in candidate and "tickers" in candidate:
                        sidecar = candidate
                        markdown = text[:start].strip() + "\n\n" + text[end:].strip()
                        break
                except json.JSONDecodeError:
                    continue

    return markdown.strip(), sidecar


def validate_sidecar(sidecar, date_str):
    """Ensure required fields exist; add defaults for missing optional fields."""
    if sidecar is None:
        return None
    sidecar.setdefault("date", date_str)
    sidecar.setdefault("source_file", "")
    sidecar.setdefault("market_summary", "")
    sidecar.setdefault("tickers", [])
    sidecar.setdefault("ticker_details", {})
    sidecar.setdefault("sector_leaders", [])
    sidecar.setdefault("sector_laggards", [])
    return sidecar


# ---------------------------------------------------------------------------
# LLM Client abstraction
# ---------------------------------------------------------------------------

class GeminiClient:
    """Google Gemini API client."""

    def __init__(self, api_key, model, fallback_model):
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        self.client = genai
        self.model_name = model
        self.fallback_model = fallback_model

    def generate(self, system_prompt, user_prompt):
        """Send prompt to Gemini, return text response."""
        # Gemini doesn't separate system/user as cleanly — combine them
        full_prompt = f"{system_prompt}\n\n---\n\nTranscript to analyze:\n\n{user_prompt}"

        models_to_try = [self.model_name, self.fallback_model]
        last_error = None

        for model_name in models_to_try:
            try:
                model = self.client.GenerativeModel(model_name)
                response = model.generate_content(full_prompt)
                return response.text
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    print(f"  Rate limited on {model_name}, waiting 30s...")
                    time.sleep(30)
                    continue
                # Log non-rate-limit errors before trying fallback
                print(f"  {model_name} failed: {err_str[:120]} — trying fallback...")
                continue

        raise RuntimeError(f"All models failed. Last error: {last_error}")


class AnthropicClient:
    """Anthropic Claude API client."""

    def __init__(self, api_key, model, **_):
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key)
        self.model_name = model

    def generate(self, system_prompt, user_prompt):
        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=180,
        )
        return response.content[0].text


class OpenAIClient:
    """OpenAI API client."""

    def __init__(self, api_key, model, **_):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model_name = model

    def generate(self, system_prompt, user_prompt):
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=8000,
            timeout=180,
        )
        return response.choices[0].message.content


CLIENT_CLASSES = {
    "gemini": GeminiClient,
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
}


def get_client(provider):
    """Create LLM client based on provider and available API key."""
    if provider not in PROVIDER_CONFIG:
        print(f"Unknown provider '{provider}'. Available: {list(PROVIDER_CONFIG)}")
        sys.exit(1)

    config = PROVIDER_CONFIG[provider]
    api_key = os.environ.get(config["env_key"])

    if not api_key:
        print(f"ERROR: {config['env_key']} environment variable not set.")
        print(f"Install: {config['install']}")
        print(f"Then: export {config['env_key']}=<your-key>")
        sys.exit(1)

    extra = {}
    if provider == "gemini":
        extra = {"fallback_model": config["fallback"]}

    return CLIENT_CLASSES[provider](api_key, config["model"], **extra)


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_transcript(client, date_str, transcript_path, dry_run=False):
    """Process a single transcript and write outputs. Returns True on success."""
    dest_md = OUT_DIR / f"{date_str}.md"
    dest_json = OUT_DIR / f"{date_str}.json"

    transcript_size = transcript_path.stat().st_size
    print(f"  [{date_str}] Reading {transcript_path.name} ({transcript_size:,} bytes)...")

    if dry_run:
        print(f"  [{date_str}] DRY RUN — would process transcript")
        return False

    # Read transcript
    try:
        transcript_text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  [{date_str}] ERROR reading transcript: {e}")
        return False

    # Truncate if extremely large (Gemini context limit ~1M tokens, ~2.5MB)
    max_chars = 400_000  # ~100K tokens — safe for all providers
    if len(transcript_text) > max_chars:
        print(f"  [{date_str}] Truncating transcript from {len(transcript_text):,} to {max_chars:,} chars")
        transcript_text = transcript_text[:max_chars]

    # Build user prompt
    user_prompt = f"Date: {date_str}\nTranscript file: {transcript_path.name}\n\n{transcript_text}"

    # Call LLM
    print(f"  [{date_str}] Sending to LLM ({len(user_prompt):,} chars)...")
    try:
        response_text = client.generate(SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        print(f"  [{date_str}] LLM ERROR: {e}")
        return False

    # Parse response
    markdown, sidecar = parse_llm_response(response_text)
    sidecar = validate_sidecar(sidecar, date_str)

    # Guard against parse failure: create minimal sidecar from markdown parsing
    if sidecar is None:
        print(f"  [{date_str}] WARNING: Could not extract JSON sidecar — creating minimal fallback")
        sidecar = {
            "date": date_str,
            "source_file": str(transcript_path),
            "market_summary": "",
            "tickers": [],
            "ticker_details": {},
            "sector_leaders": [],
            "sector_laggards": [],
        }

    sidecar["source_file"] = str(transcript_path)

    # Write outputs
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        dest_md.write_text(markdown, encoding="utf-8")
        with open(dest_json, "w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2)
    except Exception as e:
        print(f"  [{date_str}] ERROR writing output: {e}")
        return False

    ticker_count = len(sidecar.get("tickers", []))
    print(f"  [{date_str}] ✓ Done — {len(markdown):,} chars markdown, {ticker_count} tickers")
    return True


def main():
    parser = argparse.ArgumentParser(description="Batch-summarize IBD Live transcripts")
    parser.add_argument("--force", action="store_true", help="Reprocess ALL transcripts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without processing")
    parser.add_argument("--date", type=str, help="Process only a single date (YYYY-MM-DD)")
    parser.add_argument("--provider", type=str, default="gemini",
                        choices=["gemini", "anthropic", "openai"],
                        help="LLM provider (default: gemini)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max transcripts to process (0 = unlimited)")
    args = parser.parse_args()

    # Setup
    client = get_client(args.provider)
    processed = set() if args.force else load_checkpoint()
    transcripts = list(find_transcripts())

    if args.date:
        transcripts = [(d, p) for d, p in transcripts if d == args.date]
        if not transcripts:
            print(f"No transcript found for date {args.date}")
            sys.exit(1)

    total = len(transcripts)
    new_count = sum(1 for d, _ in transcripts if d not in processed)

    print(f"Found {total} transcripts, {new_count} new to process")
    print(f"Output directory: {OUT_DIR}")
    print(f"Provider: {args.provider}")
    print()

    if args.dry_run:
        print("=== DRY RUN ===\n")

    success_count = 0
    for i, (date_str, transcript_path) in enumerate(transcripts):
        if date_str in processed:
            continue
        if args.limit and success_count >= args.limit:
            break

        print(f"[{i+1}/{total}] Processing {date_str}...")
        ok = process_transcript(client, date_str, transcript_path, dry_run=args.dry_run)
        if ok:
            success_count += 1
            processed.add(date_str)
            save_checkpoint(processed)

        # Rate limiting pause between requests
        if not args.dry_run and i < len(transcripts) - 1:
            time.sleep(2)

    print(f"\nDone. {success_count} transcripts processed successfully.")
    if processed:
        save_checkpoint(processed)
        print(f"Checkpoint saved: {len(processed)} dates processed total.")


if __name__ == "__main__":
    main()
