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

## Recently Completed (cont. 3)
- **2026-02-12**: Sidebar resize — draggable 4px handle between sidebar and main area (col-resize cursor, accent highlight on hover/drag). Width persisted to `localStorage sidebar:width`. Hidden on mobile and when sidebar collapsed.
- **2026-02-12**: Task Queue enhancements — dispatched tasks prepend "please execute this task: "; drag reorder via grip handles (pointer events, works on mobile); inline edit (click text to edit, Enter/Escape/blur to save/cancel); re-render guard skips `renderQueuePanel` while `.qi-edit` input exists to prevent polling from destroying mid-edit.
- **2026-02-12**: Context remaining display — color-coded context % in sidebar (green/orange/red) and progress bar in details modal. Already plumbed end-to-end; CC only reports % when context is low.
- **2026-02-12**: Large text paste fix — `send_keys()` now uses `tmux set-buffer` + `paste-buffer` for text >500 chars or containing newlines, instead of `send-keys -l` which was unreliable for large blocks.

## Recently Completed (cont. 4)
- **2026-02-12**: Queue edit fix — `save()` was calling `renderQueuePanel()` while `.qi-edit` input still in DOM, guard blocked re-render permanently. Fixed by removing class before re-render + `_saved` double-fire guard. Added `enterKeyHint='done'` for iOS.
- **2026-02-12**: Queue UX improvements — panel width 380→480px; add-task input and inline edit converted from `<input>` to `<textarea>` with auto-grow (Shift+Enter for newlines, Enter submits/saves); queue list auto-expands to fit content (capped 60vh); bottom resize handle; once user manually resizes, auto-sizing stops (`panel._userResized`).
- **2026-02-12**: Permission mode in sidebar — `detect_cc_status()` extracts perm mode from `⏵⏵` status bar line. Shown per-window in sidebar and details modal. `--dangerously-skip-permissions` highlighted in red. Client-side `detectCCStatus()` also extracts for 1s live updates. Return type changed from tuple to dict.
- **2026-02-12**: Sidebar text labels removed — "Working"/"Standby"/"Thinking" labels dropped, dots-only for status. Context % still shown when available.
- **2026-02-12**: CC session boundary detection — `parseCCTurns()` finds last `Claude Code vX.X.X` banner in terminal output and only parses from the first `❯` after it. Fixes wonky formatting when CC restarts, after `/clear`, or when shell output (banner, "Resume this session", prompt) is mixed in.

## Recently Completed (cont. 5)
- **2026-02-12**: Queue Active/Past split — completed tasks move to "Completed (N)" section below active tasks. No strikethrough; dimmed with remove-only (no grip/edit). Active section retains full functionality.
- **2026-02-12**: QUEUE button remaining count — shows "QUEUE N" (undone count) when panel is collapsed. Updates on add/remove/complete.
- **2026-02-12**: Phantom text fix — `pendingMsg` was being cleared too aggressively on any output change. Now only clears via substring match against all parsed user turns or 10s timeout.
- **2026-02-12**: Hard refresh button — `↻` in pane tab bar. Clears cached state (`last`, `rawContent`, `pendingMsg`, `awaitingResponse`) and forces fresh fetch+render.
- **2026-02-12**: Desktop keyboard forwarding — when textarea is empty, Arrow Up/Down → tmux Up/Down, Enter → tmux Enter, Escape → tmux Escape, Tab → tmux Tab. Enables plan mode / interactive prompt navigation from desktop keyboard.
- **2026-02-12**: Large text input fix — textarea max-height 80px/120px → 40vh with `overflow-y:auto` scrolling. Backend `paste-buffer` uses `-p` (bracketed paste) so multiline text isn't split into separate inputs by CC's TUI.

## Recently Completed (cont. 6)
- **2026-02-13**: Queue panel tabs — completed items moved to separate "Completed" tab within queue (with count badge, "Clear completed" button), keeps main queue view clean
- **2026-02-13**: Raw view ghost filtering — `stripSuggestion()` strips suggestion text from last `❯` line in raw mode when CC is idle
- **2026-02-13**: Paste/send reliability — 50ms sleep between `paste-buffer -p` and `send-keys Enter` (race fix); `set-buffer` → `load-buffer -` (stdin, no ARG_MAX limit); `paste` event handlers for textarea auto-resize
- **2026-02-13**: Sidebar active window highlight — active tab's window gets subtle warm tint (`rgba(217,119,87,0.08)`); updates on pane focus and tab switch
- **2026-02-13**: Working indicator after hard refresh — "Working..." shows in Clean view when `!isIdle(clean)` (detects working CC from terminal output), not just when `awaitingResponse` flag is set

## Recently Completed (cont. 7)
- **2026-02-13**: Clean view formatting — `isClaudeCode()` now detects CC by banner ("Claude Code v\d") in addition to `⏺`, so fresh/cleared sessions render correctly. CC slash commands (`/clear`, `/help`) filtered from user turns. Empty CC state shows "Ready" card instead of raw banner dump.
- **2026-02-13**: Queue premature dispatch fix — output staleness threshold 5s→30s. Was clearing `awaitingResponse` too early during extended thinking or long tool executions, causing queue to dispatch next task while CC was still working.
- **2026-02-13**: Sidebar snippet — `extractSnippet()` finds last `⏺` block in 40-line preview, shows first line as italic truncated text below perm mode for idle CC windows.
- **2026-02-13**: Sidebar memo — per-window editable note (`localStorage memo:session:windowIndex`). Click "+ note" to add; inline edit with Enter/Escape/blur save. `renderSidebar()` guard skips re-render during edit.

## Recently Completed (cont. 8)
- **2026-02-13**: Architecture audit & cleanup — 5-agent parallel audit (backend, frontend JS, CSS/HTML, parser/CC detection, queue/layout). Fixed:
  - Backend CC detection aligned with frontend: banner fallback, 5-line status bar window (was 3), 15-line thinking window (was 20), line-start anchoring
  - `_run()` wrapper on all subprocess calls with 5s timeout (was no timeout)
  - `clean_terminal_text()` extracted from duplicated ANSI stripping logic
  - try/catch on all 12+ frontend fetch calls (queue dispatch pauses on failure)
  - Page Visibility API pauses all polling when tab is backgrounded
  - `--surface2` CSS variable fixed (was undefined), dead `.waiting` class removed
  - Touch targets improved: tab close 14→20px, dividers/resize handles get 16px invisible hit areas
  - CSS variables for accent colors (`--accent-dim`, `--accent-focus`), queue playing color uses `--green`
  - Mobile sidebar width uses `--sidebar-w` instead of hardcoded 280px
  - `pauseQueue()` now persists state via `saveQueue()`
  - `moveTabToPane()` updates queue panel
  - `cleanupStaleStorage()` on init prunes orphaned notepad/memo/queue/sidebar-order keys

## Recently Completed (cont. 9)
- **2026-02-13**: Sidebar snippet moved to right column — two-column layout with window info on left, snippet + context % on right. Default sidebar width 260→300px.
- **2026-02-13**: Sidebar text scales with text size — `--sb-name`/`--sb-detail`/`--sb-tiny` CSS variables scale across all 4 tiers (A-- through A+). All sidebar elements respond to text size toggle.
- **2026-02-13**: Fixed false positive status dots — removed `activity_age < 5` fallback from `detect_cc_status()`. CC's TUI refreshes periodically keeping activity_age low (~5-7s) even on idle sessions, causing false "working" orange dots. Text signals (`esc to interrupt`, `·` thinking) are sufficient.

## Recently Completed (cont. 10)
- **2026-02-13**: Per-pane Keys/Commands trays — multi-pane input bars now have collapsible Keys and Commands pill buttons (matching global bar pattern). Collapsed by default, mutually exclusive toggle. Includes Left/Right arrow keys for plan mode horizontal navigation.
- **2026-02-13**: Left/Right arrow keys added globally — backend ALLOWED set, global keys tray, per-pane keys tray, keyboard forwarding (ArrowLeft/Right when textarea empty) for both global and per-pane textareas.

## Recently Completed (cont. 11)
- **2026-02-13**: Tab reorder within pane — `dragover`/`dragleave`/`drop` listeners on `.pane-tab` elements enable drag-to-reorder tabs within same pane. Uses existing `.drag-over-tab` CSS. `stopPropagation()` on drop prevents pane-level handler from firing. Cross-pane drag still works (guard checks both tabs in same pane).
- **2026-02-13**: Hidden sessions — HIDE button on session header (hover-revealed, always visible on mobile). Hidden sessions collected into collapsible "Hidden (N)" section at bottom. SHOW button to unhide. Persisted to `localStorage hidden-sessions`. Refactored `renderSidebar()` → extracted `renderSidebarSession()` helper.
- **2026-02-13**: Activity age per window — dashboard API now includes `activity_age` (seconds since tmux `window_activity`). `formatAge()` formats as compact labels (now/3m/2h/1d). Shown per-window in sidebar right column as `.sb-activity`.

## Recently Completed (cont. 12)
- **2026-02-13**: Snippet in collapsed sidebar — moved snippet from `.sb-win-right` to `.sb-win-info` (below name row), visible in both collapsed and expanded sidebar views.
- **2026-02-13**: Queue draft preservation — `renderQueuePanel()` saves/restores add-task textarea value across re-renders so typed text survives CC session state changes.
- **2026-02-13**: Reliable tmux send — removed `send-keys -l` path entirely; all text now uses `load-buffer -` + `paste-buffer -d -p` for atomic delivery. Added `returncode` check to bail if buffer load fails.
- **2026-02-13**: Activity age blink fix — backend sends epoch timestamp (`activity_ts`) instead of computed age; client computes age via `ageFromTs()`; sidebar caches HTML (stripping age spans) to skip full DOM rebuild when only ages changed; `updateSidebarAges()` does in-place updates; 30s interval for smooth age progression.

## Recently Completed (cont. 13)
- **2026-02-14**: Server-side preferences persistence — `~/.mobile-terminal-prefs.json` backend with `GET/PUT /api/prefs` endpoints (atomic writes via tmp+rename). Frontend `prefs` JS object replaces `localStorage` for all synced keys (`textSize`, `sidebar:*`, `hidden-sessions`, `memo:*`, `queue:*`, `notepad:*`). In-memory cache with 500ms debounced flush, retry on failure. Auto-migration from localStorage on first run (first device seeds server). `layout` stays in localStorage (per-device). Early-executing reads deferred into `init()` after `await prefs.load()`. `cleanupStaleStorage()` iterates `prefs.keys()` instead of localStorage.

## Recently Completed (cont. 14)
- **2026-02-14**: Fix mobile send reliability — two bugs: (1) iOS `e.isComposing` race — predictive text fires Enter keydown with `isComposing=true` to confirm autocomplete, handler was intercepting and sending incomplete text; added `!e.isComposing` guard to both global and per-pane keydown handlers. (2) Silent fetch failure — `catch(e) {}` swallowed network errors after clearing textarea; now restores text to input and clears `pendingMsg`/`awaitingResponse` on failure.

## Active Work
None

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` runs N capture-pane calls (one per window) — could be slow with many windows
