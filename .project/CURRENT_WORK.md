# Current Work

## Recently Completed
- **2026-03-10**: Sidebar timestamps & simplification — JSONL-based `gauge_last_ts` replaces unreliable tmux `window_activity` for sidebar age; 3-column layout (name, age, ctx%); removed snippet/italic text; fresh/cleared session detection ("CLEAR" label + gray dot); perf fix for gauge lock eviction that was busting cache
- **2026-03-10**: Performance & code quality sweep — all async endpoint handlers now use `run_in_executor`; `_get_session_cwds()` cached with 5s TTL; extracted `_gauge_cache_metrics()` and `paneForTab()` helpers; removed dead code
- **2026-03-10**: Gauge matching redesign — text-based scoring replaces mtime heuristic. `_gauge_locks` persisted to disk. 19/19 windows matched correctly.
- **2026-03-09**: Gauge stability fix — sticky match cache replaces unstable activity-based JSONL matching

See `.project/PAST_WORK.md` for older history.

## Active Work
None

## Known Issues
- **Gauge lock stale on same-PID session restart**: When Claude Code starts a new JSONL without changing PID, the gauge lock points at the old JSONL. Newer-JSONL eviction was tried but caused cache thrashing (8s/cycle). Locks only evict on PID change or file deletion. `activity_ts` (tmux) serves as fallback.
- **Sidebar timestamps occasionally blank on phone**: All elements render correctly in Playwright. May be iOS Safari caching. `htmlNoAge` cache optimization was removed but user still reported blanks — needs investigation on actual device.

## Session Notes

### Session 2026-03-10 (sidebar timestamps)
- `_gauge_extract_usage()` returns `(usage_list, last_epoch)` tuple; `_gauge_compute()` includes `last_ts`
- Dashboard exposes `gauge_last_ts` per window; client uses `w.gauge_last_ts || w.activity_ts`
- Removed `extractSnippet()`, `.sb-snippet`, `sb-win-status` wrapper, `sb-win-right` wrapper
- Sidebar: `[dot] [name] [age] [ctx%] [kebab]` — direct flex children of `.sb-win`
- `detect_cc_status()` returns `fresh` when no `⏺` in output; gray dot in sidebar + tab
- Newer-JSONL eviction added then reverted — caused cache invalidation every cycle
- Removed `htmlNoAge` sidebar cache — was source of blank timestamps
- `activity_ts` always populated from tmux `window_activity` (removed CC-specific conditional)

## Up Next
- Investigate iOS Safari blank timestamps on actual device
- Gauge stale lock: find lightweight way to detect same-PID JSONL switch
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: batch N `capture-pane` calls into single tmux call
- Gauge metrics: incremental JSONL reads (file size check, seek to tail)
