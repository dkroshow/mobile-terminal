# Current Work

## Recently Completed
- **2026-03-16**: Fix tab drag reorder lag — defer `renderOutput`/`renderSidebar`/`renderPaneTabs` during drag via `_dragSrcTabId`/`_sbDragging` guards; `_renderDeferred` flag flushes on next poll; optimized dragover handler (`pane.tabIds.includes()` replaces `panes.some()` scan)
- **2026-03-13**: Fixed focusTab crash + new_window race — `state` used before declaration crashed init (→ "Connecting..."); `new_window()` ignored `-P` output and used separate `display-message` (race sent commands to wrong window)
- **2026-03-13**: Code quality deep-clean — fixed `--accent-focus` CSS self-ref, 6 async endpoints blocking event loop, sort comparator bug; 5 major deduplications

See `.project/PAST_WORK.md` for older history.

## Active Work
None currently active.

## Known Issues
- **Gauge lock stale on same-PID session restart**: `cc_fresh` detection evicts the lock. Remaining edge case: non-fresh JSONL switch (rare).
- **Sidebar timestamps occasionally blank on phone**: May be iOS Safari caching — needs investigation on actual device.

## Session Notes

### Session 2026-03-16 (tab drag reorder lag fix)
- Root cause: 1s output poll (`renderOutput`) and 3s dashboard poll (`renderSidebar`/`renderPaneTabs`) do heavy DOM work that blocks main thread during drag
- Fix: defer all heavy renders when `_dragSrcTabId !== null || _sbDragging`; `_renderDeferred` flag on `tabStates` ensures catch-up on next poll; `dragend` flushes sidebar render immediately
- Also optimized `dragover` handler: closure-captured `pane` reference avoids `panes.some()` scan on every event

### Session 2026-03-13 (raw view wrap-rejoin fix)
- Wrap-rejoin regex required exactly 2-space indent — CC TUI bullet continuations with 3+ spaces weren't joined, leaving broken lines
- Changed `^  \\S` → `^ {2,}\\S` and `^  [a-zA-Z]` → `^ {2,}[a-zA-Z]`

### Session 2026-03-13 (focusTab crash + new_window race)
- `focusTab` crash: `state` used before `const` declaration in notepadOpen check → crashed init → "Connecting..." on page load
- `new_window()` race: ignored `-P -F` output from `tmux new-window`, ran separate `display-message` — race between calls sent startup commands to wrong window (current pane instead of new one)

## Up Next
- Investigate iOS Safari blank timestamps on actual device
- Gauge stale lock: find lightweight way to detect same-PID JSONL switch
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: batch N `capture-pane` calls into single tmux call
- Gauge metrics: incremental JSONL reads (file size check, seek to tail)
