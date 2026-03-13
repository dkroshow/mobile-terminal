# Current Work

## Recently Completed
- **2026-03-13**: Text loss bug fix — 3 bugs causing submitted text to vanish: (1) `renderOutput` throw aborting send after textarea cleared, (2) backend `send_keys` failures silent (always returned ok:true), (3) draft system race where `focusTab` saved empty textarea as draft during async send. Fix: `_sendingText` backup in tabStates, inner try-catch around renderOutput, backend returns actual success/failure
- **2026-03-13**: New Window popup — modal with pre-populated directory, Claude Code checkbox, `--dangerously-skip-permissions` checkbox. Backend accepts `cwd`/`commands` params, returns new window index for reliable tab focus
- **2026-03-10**: Gauge threshold & fresh eviction — GAUGE_THRESHOLD changed from 165k to 170k (empirically derived from 18 compression points, max 168,248); fresh CC sessions (`cc_fresh`) now evict gauge locks and suppress stale metrics
- **2026-03-10**: Sidebar timestamps & simplification — JSONL-based `gauge_last_ts` replaces unreliable tmux `window_activity` for sidebar age; 3-column layout (name, age, ctx%); removed snippet/italic text; fresh/cleared session detection ("CLEAR" label + gray dot); perf fix for gauge lock eviction that was busting cache
- **2026-03-10**: Performance & code quality sweep — all async endpoint handlers now use `run_in_executor`; `_get_session_cwds()` cached with 5s TTL; extracted `_gauge_cache_metrics()` and `paneForTab()` helpers; removed dead code
- **2026-03-10**: Gauge matching redesign — text-based scoring replaces mtime heuristic. `_gauge_locks` persisted to disk. 19/19 windows matched correctly.
- **2026-03-09**: Gauge stability fix — sticky match cache replaces unstable activity-based JSONL matching

See `.project/PAST_WORK.md` for older history.

## Active Work
- **Text loss monitoring**: Fix deployed, user should report if text loss recurs. `console.warn` added for client-side debugging

## Known Issues
- **Gauge lock stale on same-PID session restart**: When Claude Code starts a new JSONL without changing PID (e.g. `/clear`), the lock pointed at the old JSONL. Now handled: `cc_fresh` detection evicts the lock and suppresses stale gauge data. Remaining edge case: non-fresh JSONL switch (rare).
- **Sidebar timestamps occasionally blank on phone**: All elements render correctly in Playwright. May be iOS Safari caching. `htmlNoAge` cache optimization was removed but user still reported blanks — needs investigation on actual device.

## Session Notes

### Session 2026-03-13 (deploy perf fix)
- Server at 95.5% CPU — `run_in_executor` fixes from earlier saved to disk but never deployed (server running stale blocking code)
- Restarted via `launchctl unload/load` — CPU dropped to 1.1%, page load ~40ms, output poll ~30ms
- User's text entry and rename issues confirmed resolved (were caused by blocked event loop queueing requests for 10+ seconds)
- Updated CLAUDE.md with 2 critical constraints (must restart after edit, must use run_in_executor)
- Updated restart learning with performance diagnosis angle, created file-knowledge-map.md

### Session 2026-03-13 (new window popup + text loss fix)
- New window modal: `#nw-overlay` / `#nw-modal`, pre-populates cwd from `_dashboardData`, checkboxes default to checked
- Backend `new_window()` now accepts `cwd`, `commands` params; returns new window index via `tmux display-message`
- Text loss root cause: async gap between `ta.value = ''` and `await fetch()` allows `focusTab` draft save to capture empty string
- Fix: `state._sendingText` holds backup text; draft save checks `_sendingText` first; `renderOutput` wrapped in inner try-catch; backend returns 500 on tmux failure
- Research doc: `.project/research/text-loss-bug.md`

### Session 2026-03-10 (gauge threshold & fresh eviction)
- GAUGE_THRESHOLD: 165k too low (showed 1% when CC not worried), 200k too high (showed 17% during compaction), 170k is the sweet spot based on 18 observed compression points (max 168,248, median 166,624)
- `cc_fresh` windows now evict their gauge lock via `_gauge_save_locks()` and skip gauge enrichment in `get_dashboard()`
- Compression typically triggers at 163-168k tokens; CC's "Context left until auto-compact: X%" matches this range

## Up Next
- Investigate iOS Safari blank timestamps on actual device
- Gauge stale lock: find lightweight way to detect same-PID JSONL switch
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: batch N `capture-pane` calls into single tmux call
- Gauge metrics: incremental JSONL reads (file size check, seek to tail)
