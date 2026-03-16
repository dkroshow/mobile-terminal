---
type: gotcha
tags: drag, performance, polling, render, defer, DOM
created: 2026-03-16
---

# Heavy DOM Renders During Drag Cause Stutter

## What
Periodic polling (1s output poll, 3s dashboard poll) does heavy DOM work (`renderOutput`, `renderSidebar`, `renderPaneTabs`) that blocks the main thread and causes drag-and-drop to stutter.

## Context
Tab reorder drag on the left pane had noticeable lag. The sidebar already had `_sbDragging` to pause `renderSidebar()` during sidebar drags, but tab drags had no equivalent guard, and neither guard suppressed `renderOutput()` (the heaviest operation — CC parsing, markdown rendering, full innerHTML replacement).

## What Didn't Work
- Optimizing drag handlers alone (throttling `dragover`) — the lag came from unrelated periodic renders, not the drag event handlers themselves.

## What Works
- Check `_dragSrcTabId !== null || _sbDragging` before heavy DOM work in `pollTab` and `loadDashboard`
- Use `state._renderDeferred` flag on `tabStates` to ensure skipped renders are flushed on next poll after drag ends
- Flush `renderSidebar` / `renderFileTree` immediately in tab `dragend` handler
- Also: use closure-captured `pane` reference in `dragover` handler (`pane.tabIds.includes(src)`) instead of `panes.some(p => p.tabIds.includes(src) && p.tabIds.includes(dst))` — avoids scanning all panes on every dragover event

## Key Files
- server.py
