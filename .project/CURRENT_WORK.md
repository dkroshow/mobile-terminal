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
- **2026-02-12**: Per-window notepad — "NOTES" button in pane tab bar, dropdown panel from top-right with drag-to-resize (size persisted). Textarea persists to localStorage keyed by `notepad:session:windowIndex`. Updates on tab switch. Stays open on click-outside; toggle or X to close.
- **2026-02-12**: Layout persistence — pane/tab layout saved to localStorage on every mutation (open/close/switch tab, add/remove pane, drag tab). Restored on init with validation against live tmux sessions. Guard flag prevents save during restore.
- **2026-02-12**: Batch UI improvements (9 items):
  - Notepad bidirectional + corner resize (left edge, bottom edge, bottom-left corner; both dimensions saved as JSON)
  - Click anywhere on pane to focus (mousedown handler)
  - Fix rename: backend was using `_current_session` instead of the window's actual session; now passes session from details popup
  - Claude label shown only on first card after user turn (skip empty label div to avoid gap)
  - Reduced bubble padding for A- (padV 8, padH 12, gap 6) and A-- (padV 6, padH 8, gap 3) tiers
  - Vertical pane splitting: drag tab to bottom half of pane creates top/bottom split via `.pane-stack` wrapper; layout save/restore handles stacks; max 6 panes
  - Sidebar ⓘ replaced with ⋮ (kebab menu icon)
  - Sidebar drag reordering: sessions and windows reorderable via HTML5 DnD; custom order persisted to `localStorage sidebar:order`; `_sbDragging` flag pauses re-renders
  - Draggable pane dividers: `.pane-divider` between siblings (col-resize / row-resize), pointer events for resize, auto-updated on layout changes
  - ASCII table rendering fix: box-drawing chars (U+2500-U+257F) wrapped in fenced code blocks before marked.js parsing, preserving monospace alignment

## Recently Completed (cont. 2)
- **2026-02-12**: Task Queue — per-pane "QUEUE" button (next to NOTES) opens dropdown panel. Add tasks as list items, play/pause toggle auto-dispatches to CC session as it goes idle (2s buffer). Tasks get strikethrough when done, X to remove. Auto-pauses on manual send. localStorage persistence (`queue:session:windowIndex`), play state resets to paused on reload. No backend changes — entirely client-side using existing `/api/send` and `awaitingResponse` idle detection.

## Active Work
None

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` runs N capture-pane calls (one per window) — could be slow with many windows
