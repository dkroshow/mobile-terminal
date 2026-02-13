# Current Work

## Recently Completed
- **2026-02-12**: CC status detection rewrite — activity timestamp from tmux (`window_activity`), status bar parsing (`⏵` line for `esc to interrupt`), 1s live sidebar updates from output poll, tool-call card splitting in chat parser, ghost suggestion fix (expanded `sawStatus` range, `isIdle()` checks only status bar). Dashboard poll 3s. Textarea text size follows selected tier. Claude label only on first card after user turn.
- **2026-02-12**: Multi-pane system — each pane has own tab bar (draggable tabs), output area, and input bar (when 2+ panes). Drag-and-drop tabs between panes. Sidebar details popup (ⓘ per window with rename, close). Session rename endpoint. Extra compact text size (A-- 11px, 4 tiers). Sidebar status column (Standby green / Working orange pulse / Thinking fast pulse).
- **2026-02-12**: Dashboard view — sidebar (session/window nav with CC status dots), tab system (per-tab state/polling), split view (side-by-side up to 3), parameterized backend endpoints (`/api/dashboard`, session/window params on output/send/key), mobile drawer, keyboard shortcuts (Cmd+1-3, Cmd+\)
- **2026-02-12**: Combined tmux button (`tmux: session | window`), Details popup (`/api/pane-info`), window rename modal with Reset to Original, client-side duplicate name check
- **2026-02-12**: Rename modal, details popup, textarea input (multiline), parser fixes (multi-line user turns, horizontal rule filtering), marked.js breaks:true
- **2026-02-11**: Prefill appends to existing input; session switch clears stale output and fetches immediately
- **2026-02-11**: tmux session navigator — dropdown panel to browse/switch all tmux sessions and windows; mutable session backend
- **2026-02-11**: Top bar redesign, Commands panel, Ghost suggestion fix, working indicator, paragraph rendering
- **2026-02-11**: Tab-based window management, chat parser rewrite, instant message feedback
- **2026-02-10**: Added launchd LaunchAgent for always-on server

## Recently Completed (cont.)
- **2026-02-12**: Per-window notepad — dropdown panel in pane tab bar (pencil ✎ icon), slides down from top-right with transition. Textarea persists to localStorage keyed by `notepad:session:windowIndex`. Updates on tab switch. Click-outside-to-close.

## Active Work
None

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` runs N capture-pane calls (one per window) — could be slow with many windows
