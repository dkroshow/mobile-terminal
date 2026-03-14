# Current Work

## Recently Completed
- **2026-03-13**: Gauge 1M context support — per-model threshold (`GAUGE_THRESHOLD_200K`=170k, `GAUGE_THRESHOLD_1M`=1M); model extracted from JSONL `message.model`; `_gauge_threshold_for_model()` picks threshold based on `"1m"` in model name
- **2026-03-13**: Notepad per-tab visibility — notes panel closes on tab switch, reopens when switching back; `tabStates[id].notepadOpen` tracks state
- **2026-03-13**: Text loss bug fix — 3 bugs causing submitted text to vanish; `_sendingText` backup, inner try-catch, backend returns actual success/failure
- **2026-03-13**: New Window popup — modal with pre-populated directory, Claude Code checkbox, `--dangerously-skip-permissions` checkbox

See `.project/PAST_WORK.md` for older history.

## Active Work
- **Text loss monitoring**: Fix deployed, user should report if text loss recurs. `console.warn` added for client-side debugging

## Known Issues
- **Gauge lock stale on same-PID session restart**: When Claude Code starts a new JSONL without changing PID (e.g. `/clear`), the lock pointed at the old JSONL. Now handled: `cc_fresh` detection evicts the lock and suppresses stale gauge data. Remaining edge case: non-fresh JSONL switch (rare).
- **Sidebar timestamps occasionally blank on phone**: All elements render correctly in Playwright. May be iOS Safari caching. `htmlNoAge` cache optimization was removed but user still reported blanks — needs investigation on actual device.

## Session Notes

### Session 2026-03-13 (notepad per-tab + gauge 1M)
- Notepad panel now tracks open state per-tab via `tabStates[id].notepadOpen`; closes on tab switch, reopens when switching back
- Gauge updated for CC's 1M context window: `GAUGE_THRESHOLD` split into `GAUGE_THRESHOLD_200K` (170k) and `GAUGE_THRESHOLD_1M` (1M)
- Model detection via `message.model` field in assistant JSONL entries; `_gauge_threshold_for_model()` checks for `"1m"` substring
- 1M auto-compact ceiling TBD — using full 1M window as threshold until empirical data collected

### Session 2026-03-13 (new window popup + text loss fix)
- New window modal: `#nw-overlay` / `#nw-modal`, pre-populates cwd from `_dashboardData`, checkboxes default to checked
- Text loss root cause: async gap between `ta.value = ''` and `await fetch()` allows `focusTab` draft save to capture empty string
- Fix: `state._sendingText` holds backup text; draft save checks `_sendingText` first; `renderOutput` wrapped in inner try-catch; backend returns 500 on tmux failure

### Session 2026-03-13 (deploy perf fix)
- Server at 95.5% CPU — `run_in_executor` fixes from earlier saved to disk but never deployed (server running stale blocking code)
- Restarted via `launchctl unload/load` — CPU dropped to 1.1%, page load ~40ms, output poll ~30ms

## Up Next
- Investigate iOS Safari blank timestamps on actual device
- Gauge stale lock: find lightweight way to detect same-PID JSONL switch
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: batch N `capture-pane` calls into single tmux call
- Gauge metrics: incremental JSONL reads (file size check, seek to tail)
