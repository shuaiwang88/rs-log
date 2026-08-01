# IBD Live Summaries to Re-Run

_Generated 2026-07-31_

## Summary

- **36 show dates** contain boilerplate/LLM-filler `ticker_details` rows
  (the summarizer regurgitated prompt-template phrases instead of real content).
- **1715 boilerplate rows** total.

## How to re-run

```bash
export GEMINI_API_KEY="your-key"   # or ANTHROPIC_API_KEY / OPENAI_API_KEY
# One at a time:
python python/batch_summarize_transcripts.py --date 2026-04-01
# ...or reprocess everything (--force bypasses the checkpoint):
python python/batch_summarize_transcripts.py --force
```

The script reads the raw transcript from `~/Documents/Zoom`, regenerates `IBD/live_summaries/<date>.md` + `<date>.json`, and the app picks up the new data on next run.

## Dates to re-run

| # | Date | Boilerplate rows | Transcript source |
|---|------|------------------|-------------------|
| 1 | `2026-04-01` | 17 | `~/Documents/Zoom/2026-04-01 10.18.04 IBD Live/meeting_saved_closed_caption.txt` |
| 2 | `2026-04-02` | 56 | `~/Documents/Zoom/2026-04-02 10.59.28 IBD Live/meeting_saved_closed_caption.txt` |
| 3 | `2026-04-06` | 56 | `~/Documents/Zoom/2026-04-06 10.44.35 IBD Live/meeting_saved_closed_caption.txt` |
| 4 | `2026-04-07` | 57 | `~/Documents/Zoom/2026-04-07 10.49.53 IBD Live/meeting_saved_closed_caption.txt` |
| 5 | `2026-04-08` | 10 | `~/Documents/Zoom/2026-04-08 11.19.55 IBD Live/meeting_saved_closed_caption.txt` |
| 6 | `2026-04-09` | 39 | `~/Documents/Zoom/2026-04-09 11.02.07 IBD Live/meeting_saved_closed_caption.txt` |
| 7 | `2026-04-10` | 57 | `~/Documents/Zoom/2026-04-10 10.55.22 IBD Live/meeting_saved_closed_caption.txt` |
| 8 | `2026-04-13` | 29 | `~/Documents/Zoom/2026-04-13 10.45.57 IBD Live/meeting_saved_closed_caption.txt` |
| 9 | `2026-04-17` | 12 | `~/Documents/Zoom/2026-04-17 11.00.30 IBD Live/meeting_saved_closed_caption.txt` |
| 10 | `2026-04-20` | 68 | `~/Documents/Zoom/2026-04-20 10.55.10 IBD Live/meeting_saved_closed_caption.txt` |
| 11 | `2026-04-21` | 69 | `~/Documents/Zoom/2026-04-21 10.11.39 IBD Live/meeting_saved_closed_caption.txt` |
| 12 | `2026-04-22` | 43 | `~/Documents/Zoom/2026-04-22 10.33.33 IBD Live/meeting_saved_closed_caption.txt` |
| 13 | `2026-04-23` | 53 | `~/Documents/Zoom/2026-04-23 10.23.48 IBD Live/meeting_saved_closed_caption.txt` |
| 14 | `2026-04-24` | 67 | `~/Documents/Zoom/2026-04-24 09.41.54 IBD Live/meeting_saved_closed_caption.txt` |
| 15 | `2026-04-27` | 58 | `~/Documents/Zoom/2026-04-27 09.58.51 IBD Live/meeting_saved_closed_caption.txt` |
| 16 | `2026-04-28` | 59 | `~/Documents/Zoom/2026-04-28 09.47.18 IBD Live/meeting_saved_closed_caption.txt` |
| 17 | `2026-04-29` | 45 | `~/Documents/Zoom/2026-04-29 09.53.08 IBD Live/meeting_saved_closed_caption.txt` |
| 18 | `2026-04-30` | 41 | `~/Documents/Zoom/2026-04-30 10.06.17 IBD Live/meeting_saved_closed_caption.txt` |
| 19 | `2026-05-01` | 38 | `~/Documents/Zoom/2026-05-01 10.09.35 IBD Live/meeting_saved_closed_caption.txt` |
| 20 | `2026-05-04` | 55 | `~/Documents/Zoom/2026-05-04 10.36.34 IBD Live/meeting_saved_closed_caption.txt` |
| 21 | `2026-05-05` | 68 | `~/Documents/Zoom/2026-05-05 09.22.21 IBD Live/meeting_saved_closed_caption.txt` |
| 22 | `2026-05-06` | 35 | `~/Documents/Zoom/2026-05-06 10.36.23 IBD Live/meeting_saved_closed_caption.txt` |
| 23 | `2026-05-07` | 49 | `~/Documents/Zoom/2026-05-07 10.54.14 IBD Live/meeting_saved_closed_caption.txt` |
| 24 | `2026-05-08` | 50 | `~/Documents/Zoom/2026-05-08 09.29.39 IBD Live/meeting_saved_closed_caption.txt` |
| 25 | `2026-05-11` | 58 | `~/Documents/Zoom/2026-05-11 09.41.53 IBD Live/meeting_saved_closed_caption.txt` |
| 26 | `2026-05-12` | 44 | `~/Documents/Zoom/2026-05-12 09.21.23 IBD Live/meeting_saved_closed_caption.txt` |
| 27 | `2026-05-13` | 30 | `~/Documents/Zoom/2026-05-13 09.32.58 IBD Live/meeting_saved_closed_caption.txt` |
| 28 | `2026-05-14` | 48 | `~/Documents/Zoom/2026-05-14 09.56.20 IBD Live/meeting_saved_closed_caption.txt` |
| 29 | `2026-05-15` | 47 | `~/Documents/Zoom/2026-05-15 10.56.25 IBD Live/meeting_saved_closed_caption.txt` |
| 30 | `2026-05-18` | 55 | `~/Documents/Zoom/2026-05-18 09.36.00 IBD Live/meeting_saved_closed_caption.txt` |
| 31 | `2026-05-20` | 52 | `~/Documents/Zoom/2026-05-20 09.22.18 IBD Live/meeting_saved_closed_caption.txt` |
| 32 | `2026-05-21` | 31 | `~/Documents/Zoom/2026-05-21 09.52.01 IBD Live/meeting_saved_closed_caption.txt` |
| 33 | `2026-05-22` | 50 | `~/Documents/Zoom/2026-05-22 09.25.50 IBD Live/meeting_saved_closed_caption.txt` |
| 34 | `2026-05-26` | 72 | `~/Documents/Zoom/2026-05-26 09.22.02 IBD Live/meeting_saved_closed_caption.txt` |
| 35 | `2026-05-27` | 52 | `~/Documents/Zoom/2026-05-27 09.32.36 IBD Live/meeting_saved_closed_caption.txt` |
| 36 | `2026-07-10` | 45 | `~/Documents/Zoom/2026-07-10 10.17.01 IBD Live/IBD Live transcript_2026-07-10_11.09.16.txt` |

## Note

`--force` will also regenerate dates that are already clean (checkpoint bypassed);
use `--date <YYYY-MM-DD>` to only redo specific shows.
