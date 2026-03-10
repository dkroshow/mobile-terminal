---
type: gotcha
tags: gauge, cache, performance, jsonl, locks
created: 2026-03-10
---

# Gauge Lock Eviction Must Not Trigger Full JSONL Re-reads

## What
Adding "smart" lock eviction logic (checking if newer JSONLs exist in same project dir) caused the gauge cache to be invalidated every 30s cycle, turning a one-time 8-10s cold start into a per-request cost.

## Context
When Claude Code restarts a session without changing PID, a new JSONL is created but the gauge lock still points to the old one. Attempted fix: in Pass 1, evict lock if a newer JSONL exists in the same slug directory.

## What Didn't Work
Checking `jmt > locked_mtime + 60` for any other JSONL in the same slug dir. Problem: old JSONL files from previous sessions often have mtimes newer than the locked one (e.g., they were written to more recently during their active session). This causes every locked session to get evicted, forcing full re-reads of all unlocked JSONLs every cycle.

## What Works
PID-based eviction only (`_gauge_locks[key]["pid"] != pid_for_key.get(key)`). Simple, stable, no cache thrashing. Accepts that same-PID JSONL switches are rare and `activity_ts` from tmux serves as age fallback.

## Key Files
- `server.py` lines ~308-350 (lock pruning and Pass 1)
- `_refresh_gauge_cache()` — the main gauge pipeline
