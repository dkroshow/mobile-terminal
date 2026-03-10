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
Integrating context gauge (% used, burn rate, est turns) into mobile-terminal sidebar required per-window accuracy. With 8 galaxy windows and 34 JSONLs, heuristic approaches fail.

## Evolution of Approaches

| Approach | Problem |
|---|---|
| lsof UUID matching | Claude doesn't keep tasks files open — always falls through |
| Activity-time correlation | tmux `window_activity` artificially low on idle CC (TUI refresh) |
| Mtime heuristic (30s re-match) | With N windows same slug, picks wrong JSONL; re-matching causes flip-flopping |
| **Text scoring (current)** | Works — matches user message text between tmux capture and JSONL content |

## What Works (current approach)
1. **Easy case** (1 unmatched window per slug): most recent JSONL by mtime — fast, correct
2. **Hard case** (multiple same-slug): `_gauge_score_text_match()` counts how many user texts from tmux/api_send appear in each candidate JSONL. Locks only when one JSONL scores uniquely highest (no ties)
3. **Bootstrap**: `_gauge_extract_tmux_texts()` reads ❯ prompts from tmux capture for matching after server restart
4. **Ongoing**: `_gauge_sent` collects texts from `api_send()` for new sessions
5. **Permanent locks**: `_gauge_locks` persists match until Claude PID changes

## Key Gotcha
Generic phrases like "I'm picking up where a previous session left off" match 12+ JSONLs — text matching MUST require unique highest score, not just "any match found." Without this, the first window to match a generic phrase steals another window's JSONL.

## Key Files
- `server.py` — `_refresh_gauge_cache()`, `_gauge_score_text_match()`, `_gauge_extract_tmux_texts()`, `_gauge_jsonl_user_texts()`
