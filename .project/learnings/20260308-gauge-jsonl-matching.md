---
type: gotcha
tags: [gauge, context, jsonl, pid, tmux, matching]
created: 2026-03-08
updated: 2026-03-10
---

# Per-Window JSONL Matching for Context Gauge

## What
Matching each tmux window to its specific Claude JSONL transcript is non-trivial because Claude doesn't expose its session UUID externally. Multiple Claude instances in the same project all share a CWD → same slug → same pool of JSONLs.

## Context
Integrating context gauge (% used, burn rate, est turns) into mobile-terminal sidebar required per-window accuracy. With 8 galaxy windows and 78 recent JSONLs, heuristic approaches fail.

## Evolution of Approaches

| Approach | Problem |
|---|---|
| lsof UUID matching | Claude doesn't keep tasks files open — always falls through |
| Activity-time correlation | tmux `window_activity` artificially low on idle CC (TUI refresh) |
| Mtime heuristic (30s re-match) | With N windows same slug, picks wrong JSONL; re-matching causes flip-flopping |
| **Text scoring (current)** | Works — matches tmux capture paragraphs against user+assistant JSONL content |

## What Works (current approach)
1. **Easy case** (1 unmatched window per slug): most recent JSONL by mtime — fast, correct
2. **Hard case** (multiple same-slug): `_gauge_score_text_match()` counts how many text chunks from tmux/api_send appear in each candidate JSONL (both user AND assistant messages). Locks only when one JSONL scores uniquely highest (no ties)
3. **Bootstrap**: `_gauge_extract_tmux_texts()` chunks consecutive tmux lines into paragraphs — individual wrapped lines are too short/generic, but paragraphs are distinctive
4. **Ongoing**: `_gauge_sent` collects texts from `api_send()` for new sessions
5. **Permanent locks**: `_gauge_locks` persisted to `~/.mobile-terminal-gauge-locks.json` — survives server restart
6. **7-day cutoff**: JSONL candidates within 7 days (was 24h — stale sessions fell outside)

## Key Gotchas
- Generic phrases ("I'm picking up where a previous session left off") match 12+ JSONLs — text matching MUST require unique highest score, not just "any match found"
- Individual tmux lines (wrapped at terminal width) are too short to be distinctive — must chunk consecutive non-empty lines into paragraphs
- Matching only user messages misses windows showing assistant output — must score against both user AND assistant JSONL content
- 24h JSONL cutoff too tight — active sessions that haven't had a new turn in >24h fall outside. 7 days is safe (78 JSONLs for galaxy, still fast)

## Key Files
- `server.py` — `_refresh_gauge_cache()`, `_gauge_score_text_match()`, `_gauge_extract_tmux_texts()`, `_gauge_jsonl_texts()`
- `~/.mobile-terminal-gauge-locks.json` — persisted locks
