# Current Work

## Recently Completed
- **2026-03-10**: Performance & code quality sweep — all async endpoint handlers now use `run_in_executor` (was blocking event loop with sync subprocess calls); `_get_session_cwds()` cached with 5s TTL; extracted `_gauge_cache_metrics()` helper (3 duplicated blocks); extracted `paneForTab()` JS helper (7 duplicated blocks); removed dead `activity_age` param, unused `timezone` import, redundant `preview_short` computation
- **2026-03-10**: Gauge matching redesign — text-based scoring replaces mtime heuristic. `_gauge_locks` persisted to `~/.mobile-terminal-gauge-locks.json`. 19/19 windows matched correctly.
- **2026-03-09**: Gauge stability fix — sticky match cache replaces unstable activity-based JSONL matching

See `.project/PAST_WORK.md` for older history.

## Active Work
None

## Session Notes

### Session 2026-03-10 (deploy perf fixes)
- Server was at 95.5% CPU — the `run_in_executor` fixes from earlier session were saved to disk but never deployed (server still running old blocking code)
- Restarted via `launchctl unload/load` — CPU dropped to 1.1%, page load ~40ms, output poll ~30ms
- Gauge system was reading 25+ MB of JSONL files synchronously on the event loop every 30s — now runs in executor thread pool

### Session 2026-03-10 (performance & code quality)
- Ran `/simplify` with 3 parallel review agents (reuse, quality, efficiency)
- Root cause of lag: ALL async handlers called `subprocess.run()` synchronously, blocking the event loop. With 1s output polls per tab + 3s dashboard polls, the loop was constantly blocked
- Fixed 16 endpoints with `run_in_executor`
- `_get_session_cwds()` was spawning a subprocess on every file browser request — added 5s TTL cache
- Extracted `_gauge_cache_metrics()` (3 copy-pasted blocks) and `paneForTab()` JS helper (7 instances)
- Removed dead code: unused `timezone` import, `activity_age` param (never used in function body), redundant `preview_short` (slicing 40 lines from 40-line string)

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` still runs N `capture-pane` calls (one per window) — now non-blocking via executor, but could batch into single tmux call
- Gauge metrics polling: currently re-reads full JSONL every 30s for all matched windows — could be optimized (e.g., incremental reads, file size check)
