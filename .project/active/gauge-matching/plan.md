# Gauge Matching Redesign — Plan

## TL;DR
Replace the 30s re-matching cycle with a state machine: new Claude PIDs start as `unmatched`, get resolved via mtime (single instance) or text matching (multiple instances), then lock permanently. Metrics-only refresh continues for matched windows.

## Phase 1: Core matching state machine

**Goal:** Replace `_refresh_gauge_cache` matching logic with permanent-lock approach.

**Changes:**
- `server.py`: New globals — `_gauge_locks` (session:window → {stem, pid, path}), `_gauge_pending` (session:window → {pid, slug, candidates, texts[]})
- `server.py`: Remove `_gauge_match` (replaced by `_gauge_locks`)
- `server.py`: Rewrite `_refresh_gauge_cache()`:
  1. Discover Claude PIDs (same as now — tmux list-panes + ps tree)
  2. Prune locks where PID changed or window gone
  3. For locked windows: just re-read JSONL metrics (no matching)
  4. For unlocked windows: attempt match (Phase 2 logic)
  5. Build cache from locked matches only

**Validation:** Locked windows produce stable gauge values across refreshes.

## Phase 2: Match resolution logic

**Goal:** Implement the easy-case and hard-case matching.

**Changes:**
- `server.py` in `_refresh_gauge_cache()`:
  - **Easy case:** Count unmatched windows per slug. If only 1 unmatched for a slug, assign most-recent unclaimed JSONL → lock immediately.
  - **Hard case:** Multiple unmatched for same slug → check `_gauge_pending[key]["texts"]` against JSONL user messages. Match when text found in exactly one candidate JSONL.
  - **Fallback:** If no texts collected yet (fresh session, nothing sent), remain unmatched (show no gauge — AC-5).

**Validation:** Single new instance matches immediately. Multiple instances match after first user message.

## Phase 3: Text collection hook

**Goal:** Capture sent text for matching.

**Changes:**
- `server.py`: New global `_gauge_sent_texts` — dict of `session:window` → list of recent sent strings
- `server.py` in `api_send()`: Append `cmd` to `_gauge_sent_texts[key]` (cap at ~20 entries)
- `server.py` in match resolution: Compare `_gauge_sent_texts[key]` against JSONL user messages
- `server.py`: New helper `_gauge_match_by_text(texts, jsonl_path)` — reads last N user messages from JSONL, checks if any sent text appears as substring

**Validation:** Sending a message through mobile-terminal triggers correct match for ambiguous cases.

## Implementation Notes

### Phase 1-3 completed
- `_gauge_match` → `_gauge_locks` (permanent) + `_gauge_sent` (text collection)
- `_gauge_match_by_text` → `_gauge_score_text_match` (returns count, not boolean)
- Added `_gauge_extract_tmux_texts()` for bootstrap matching (reads ❯ prompts from tmux capture)
- Added `_gauge_jsonl_user_texts()` helper (shared by scoring)
- Hard case uses scoring: best unique score wins. Tied scores → stay unmatched (AC-5)
- Bootstrap (tmux capture) + api_send hook covers both restart and new-session scenarios
- `GAUGE_MATCH_TTL = 5` for faster poll when `_gauge_sent` has entries
- Verified: recon 2 drift went from 45% to 0.9%

### Deferred: Metrics polling redesign
- Currently refreshes all JSONL metrics every 30s (`GAUGE_CACHE_TTL`)
- User wants to explore improvements separately
