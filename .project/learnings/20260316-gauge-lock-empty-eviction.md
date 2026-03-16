---
type: gotcha
tags: gauge, lock, jsonl, eviction, clear, auto-compact
created: 2026-03-16
---

# Gauge Lock Stale When /clear Creates New JSONL (Same PID)

## What
Gauge lock points at wrong JSONL after `/clear` or auto-compact because the Claude PID doesn't change — only the JSONL file does. Lock persists, pointing at a tiny stub with no usage data, while the real conversation writes to a new unlocked file.

## Context
User reported "dash 4" window showing no gauge %. Locked JSONL was 1,115 bytes (2 user messages, no assistant responses). Real conversation was in a 5.4MB unlocked file.

## What Didn't Work
- PID-based eviction alone — PID stays the same across `/clear`
- `cc_fresh` eviction — only triggers when CC shows `❯` with no `⏺` (post-clear state). Once conversation starts, window is no longer "fresh" but lock still points at stub.

## What Works
In Pass 1 (locked refresh), after `_gauge_cache_metrics`: if the key is NOT in `cache` (meaning `_gauge_compute` returned None due to empty usage), evict the lock. Then rebuild `claimed_stems` after Pass 1 so the evicted stem is available for re-matching in Pass 2.

## Key Files
- server.py
