# Current Work

## Recently Completed
- **2026-03-16**: Fix gauge lock stale on same-PID JSONL switch — evict lock when locked JSONL has no assistant messages (empty usage); rebuild `claimed_stems` after Pass 1 evictions
- **2026-03-16**: Fix tab drag reorder lag — defer `renderOutput`/`renderSidebar`/`renderPaneTabs` during drag; `_renderDeferred` flag flushes on next poll; optimized dragover handler
- **2026-03-13**: Fixed focusTab crash + new_window race

See `.project/PAST_WORK.md` for older history.

## Active Work
None currently active.

## Known Issues
- **Gauge lock stale on same-PID session restart**: MOSTLY FIXED — empty-usage eviction handles /clear and auto-compact. `cc_fresh` also evicts. Remaining edge: locked JSONL has some usage but isn't the active one (very rare).
- **Sidebar timestamps occasionally blank on phone**: May be iOS Safari caching — needs investigation on actual device.

## Session Notes

### Session 2026-03-16 (drag lag + gauge lock fix)
- **Drag lag**: 1s/3s polls doing heavy DOM during drag → defer renders when `_dragSrcTabId || _sbDragging`; `_renderDeferred` flag catches up after drag
- **Gauge lock**: `/clear` creates new JSONL but PID unchanged → lock points at empty stub. Fix: evict lock when JSONL has no assistant messages; rebuild `claimed_stems` after evictions so Pass 2 re-matches

### Session 2026-03-13 (raw view wrap-rejoin fix)
- Wrap-rejoin regex required exactly 2-space indent — CC TUI bullet continuations with 3+ spaces weren't joined
- Changed `^  \\S` → `^ {2,}\\S` and `^  [a-zA-Z]` → `^ {2,}[a-zA-Z]`

### Session 2026-03-13 (focusTab crash + new_window race)
- `focusTab` crash: `state` used before `const` declaration → crashed init
- `new_window()` race: ignored `-P -F` output, ran separate `display-message` → commands to wrong window

## Up Next
- Investigate iOS Safari blank timestamps on actual device
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: batch N `capture-pane` calls into single tmux call
- Gauge metrics: incremental JSONL reads (file size check, seek to tail)
