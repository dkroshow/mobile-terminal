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
- **2026-02-14**: Fix Enter key across desktop and mobile — mobile Chrome fires `keydown` with `key:'Process'` instead of `'Enter'` when predictive text is active; `e.isComposing` broken on iOS Safari; `compositionstart/end` unreliable on Chrome; `beforeinput` with `insertParagraph` doesn't fire for `<textarea>`. Final solution: dual `keydown` (primary, works on desktop) + `beforeinput`/`insertLineBreak` (fallback for mobile Chrome). `shiftKey` tracked from `keydown` so Shift+Enter still inserts newlines. Also: fetch errors on `/api/send` now restore text to textarea instead of silently swallowing.

## Recently Completed (cont. 15)
- **2026-02-15**: Fix per-pane `/clear` button — per-pane Commands tray Clear button was sending `clear` (terminal screen clear) instead of `/clear` (CC context clear). Global bar had the correct command; per-pane copy was missing the `/`. Always verify per-pane command buttons match global bar commands.
- **2026-02-15**: Bracketed paste improvement — `send_keys()` now only uses `-p` (bracketed paste) for multiline text. Single-line text sent without bracketed paste, which is safer for slash commands.
- **2026-02-15**: Fix activity age always "now" for CC sessions — tmux `window_activity` unreliable for CC (TUI refreshes constantly keeping timestamp ~5-7s). Added server-side `_last_interaction` dict tracking actual `/api/send` and `/api/key` calls per `session:window`. Dashboard uses tracked time for CC sessions, tmux `window_activity` for non-CC. CC sessions with no tracked interaction show no age (null) instead of bogus "now".

## Recently Completed (cont. 16)
- **2026-02-15**: Fix new window creation targeting wrong session — `new_window()` always used server's `_current_session` instead of the active tab's session. Now `/api/windows/new` accepts `session` param; client passes active tab's session. Also fixed JS ReferenceError in `newWin()` (nonexistent `getActiveTabInfo()` → proper `activePaneId` lookup).
- **2026-02-15**: Fix send losing typed text — `sendToPane()` cleared textarea before validating tab/state existed; early return lost text silently. Also, try-catch only wrapped `fetch`, not pre-fetch code (`renderOutput`, `pauseQueue`), so any thrown error lost text. Fixed both: validation before clear, entire post-clear logic in try-catch. Same fix applied to `sendGlobal()`.
- **2026-02-15**: Fix slash commands (/exit, /clear) not working in CC — `send_keys()` used `paste-buffer` for all text, but CC's TUI only recognizes slash commands when chars are typed (keyboard events), not pasted. Slash commands now use `send-keys -l` (literal keystrokes) and skip Escape+C-u prefix which interfered with CC's TUI state. Normal text still uses `load-buffer`+`paste-buffer`.

## Recently Completed (cont. 17)
- **2026-02-15**: Status dots on pane tabs — colored CC status dots (green idle, orange pulse working, fast pulse thinking) to the left of each tab name. `ccStatus` stored in `tabStates`, updated in-place via output poll (no full tab bar re-render). Non-CC tabs hide the dot.
- **2026-02-15**: Sidebar click targets whole row — moved `onclick` from `.sb-win-info` to outer `.sb-win` div; clicking anywhere on a sidebar window row opens/focuses it. Added `cursor:pointer` to `.sb-win`.
- **2026-02-15**: Removed sidebar memo/notes — CSS (`.sb-memo`, `.sb-memo-edit`, `.sb-win-name-row`), JS functions (`getMemo`/`setMemo`/`startMemoEdit`), sidebar rendering, memo-edit re-render guard, and `memo:` stale cleanup all removed. Per-window notepad (NOTES button) still available.
- **2026-02-15**: Auto-unhide session on tab focus — `focusTab()` checks if the tab's session is in hidden list and calls `unhideSession()` if so, ensuring the sidebar shows the session when a tab is clicked.

## Recently Completed (cont. 18)
- **2026-02-15**: Master Notes — global notepad accessible from topbar "Notes" button, not tied to any window/session. Panel drops down below topbar with textarea, close button, and vertical resize handle. Content persisted to `prefs master-notepad`, panel height to `prefs master-notepad:size`. Visible on both mobile and desktop.
- **2026-02-15**: Fix plan mode text disappearing in Clean view — `parseCCTurns()` treated menu selection `❯` lines (plan approval, AskUserQuestion prompts) as user prompts, causing selected item text to either disappear (ghost-filtered) or show as wrong "You" card. Added pre-scan: only `❯` lines followed by a `⏺` response are real user prompts. Menu `❯` lines are treated as regular text in the current assistant turn (with `❯` prefix stripped).

- **2026-02-15**: Fix tab dots not updating dynamically on mobile — `updatePolling()` only polled active tab per pane; background tab dots never updated. Now `loadDashboard()` (3s interval) also updates `ccStatus` and dot class for all open tabs from dashboard data.

- **2026-02-15**: Links open in new tab — custom marked.js renderer adds `target="_blank"` and `rel="noopener noreferrer"` to all `<a>` tags in Clean view markdown output.

## Recently Completed (cont. 19)
- **2026-02-16**: Notifications when CC finishes — Two paths: (1) client-initiated via `renderOutput()` working→idle transition → browser Notification (when tab hidden) + POST `/api/notify` → macOS osascript + ntfy.sh; (2) server background monitor (`_notification_monitor()` async task, 5s poll) for tab-closed scenarios. `/api/send` registers `_notify_pending` entries; `/api/notify` removes them (prevents double-fire). 10s dedup window per `session:window`. `NTFY_TOPIC` env var enables ntfy.sh push. `windowName` added to all send bodies.
- **2026-02-16**: Raw view darker background — `.pane-output.raw` background changed from default `--bg` (#191a1b) to #111112.
- **2026-02-16**: Fix notification monitor async — extracted sync subprocess work into `_check_pending_notifications()` run via `run_in_executor` (prevents event loop blocking). `/api/notify` also uses executor. `get_event_loop()` → `get_running_loop()` (fixes deprecation warnings).
- **2026-02-16**: Fix tab drag reorder — insertion line indicator (3px accent `::before`/`::after` pseudo-elements) replaces whole-tab highlight; `stopPropagation()` on tab `dragover` prevents pane-level outline; `parseInt()` on `srcTabId`/`dstTabId` fixes type mismatch (tabIds are numbers, dataset returns strings).

## Recently Completed (cont. 20)
- **2026-02-21**: Fix AskUserQuestion/plan option text invisible on caret line — three fixes:
  - `stripSuggestion` was stripping selected option text (indented `❯`) instead of only the prompt `❯` ghost text. Now checks chars before `❯` (after removing `│` borders) — only strips near column 0 (prompt), skips indented options. Limited to last 10 lines.
  - `cleanTerminal` expanded to filter `├───┤` dividers (added `├`/`┤` to border filter), plus general filter removing lines where >60% chars are box-drawing with >20 box-drawing chars total (catches labeled dividers like `── memory ──`).
  - `parseCCTurns` box-drawing filter expanded from specific chars (`─━┄┈═`) to full range `[\u2500-\u257f]` with lower threshold (20 instead of 60).

## Recently Completed (cont. 21)
- **2026-02-22**: File Browser → Sidebar Integration — replaced fullscreen `#fb-overlay` with dual-tab sidebar (Sessions | Files). VS Code-like compact file tree (22px rows, lazy-loaded via `/api/files`, cached in `_ftTreeCache`). Files open as file tabs in panes (`allTabs[id].type='file'`). Markdown files get Formatted/Raw toggle (rendered markdown via `mdFile()` or syntax-highlighted raw view). All file types supported — code files get keyword-based syntax highlighting (Python/JS/Shell/JSON/TOML), binary files dimmed and non-clickable. File tabs skip polling/send/keys/queue/notepad. Layout persistence saves/restores file tabs. Input bar hidden for file tabs. Old overlay HTML/CSS/JS fully removed.

## Recently Completed (cont. 22)
- **2026-02-22**: Ghost text filtering via ANSI codes — `capture-pane -e` captures with escape codes, server-side `strip_ghost_text()` removes dim text (SGR 2 = ghost suggestions) and reverse-video cursor char (SGR 7) before stripping ANSI. Replaces old heuristic JS `stripSuggestion()`. Fixes both ghost suggestions and text typed directly into tmux showing in UI.
- **2022-02-22**: File tree enhancements — hidden files/folders now shown (only `.git`, `__pycache__`, `node_modules`, `.DS_Store` excluded); markdown defaults to raw view; text size uses `--sb-name`/`--sb-detail`/`--sb-tiny` CSS vars (matches Sessions); drag reorder + HIDE/SHOW for root directories (persisted to `ft:root-order`, `ft:hidden-roots` prefs).
- **2026-02-22**: Font rendering — added `text-rendering:optimizeLegibility`, `-moz-osx-font-smoothing:grayscale`, `font-feature-settings:'kern' 1`.

## Recently Completed (cont. 23)
- **2026-02-23**: File Editor with Auto-Refresh — file tabs now editable. Edit/View toggle in toolbar, monospace `<textarea>` editor, dirty state tracking (accent dot on tab + toolbar), Save button + Cmd/Ctrl+S shortcut, Tab key inserts 2 spaces. Backend `PUT /api/files/write` with mtime-based conflict detection (409 response, overwrite confirm). `GET /api/files/mtime` lightweight endpoint for 5s polling — auto-reloads clean files, shows warning bar for dirty files. Guards: closeTab/hardRefresh/setFileTabView confirm on dirty, `beforeunload` prevents accidental navigation.

## Recently Completed (cont. 24)
- **2026-02-24**: File tree refresh button — ↻ in sidebar header (visible only in Files view via `.sb-action-files` CSS toggle). `ftRefresh()` clears `_ftTreeCache` + `_ftExpanded` and re-renders.
- **2026-02-24**: Pane close layout fix — `removePane()` now clears inline `flex`/`width`/`height` on all remaining `.pane` and `.pane-stack` elements, so they revert to `flex:1` and evenly fill freed space (divider drag sets `flex:none` + px sizes which persisted after removal).
- **2026-02-24**: Defensive `file-tab-active` sync — `showActiveTabOutput()` now also toggles `file-tab-active` class on pane element, ensuring input bar visibility is always correct after tab switches.

## Recently Completed (cont. 25)
- **2026-02-24**: Fix submitted prompt appearing in Claude's last response — `parseCCTurns()` was absorbing unacknowledged `❯` lines (user's just-submitted text before CC responds with `⏺`) into the current assistant turn. Now the last `❯` line that isn't a `realPrompt` is skipped (pendingMsg handles display). Also fixed `pendingMsg` clearing: now checks all turns + raw terminal text (was only checking user turns, missing text absorbed into assistant turns).
- **2026-02-24**: Raw/Clean view persistence — `rawMode` per-tab now saved in layout data (`savePaneData`) and restored on browser refresh (`restorePaneTabs`). Previously only persisted within a session (in-memory `tabStates`), lost on page reload.

## Recently Completed (cont. 26)
- **2026-02-26**: Fix prompt text leaking into Claude's last response (3 layers) — `parseCCTurns()` truncates lines at last unacknowledged `❯`; defensive scrub strips pendingMsg text from last assistant turn; pendingMsg only cleared on user-turn match (not raw text match which was too aggressive)
- **2026-02-26**: Fix empty pane can't be closed — `restoreLayout()` cleans up panes with no valid tabs; `updateLayout()` renders tab bars for all panes (including empty ones) so close button always appears
- **2026-02-26**: Fix queue not dispatching — `awaitingResponse`/`onQueueTaskCompleted`/`notifyDone` logic was inside `isClaudeCode` block and after rawMode early return; moved to top of `renderOutput` so it runs in all view modes. Fixed `notifyDone` crash (referenced undefined `openTabs` instead of `allTabs`)
- **2026-02-26**: Per-tab draft text — textarea content saved to `tabStates[id].draft`/`globalDraft` on tab switch, restored in `focusTab`
- **2026-02-26**: Pane dividers scale with window resize — divider drag converts px to % on pointerup so panes maintain proportional sizes
- **2026-02-26**: Text size alignment — mono/code sizes step down one tier from text size; file viewer CSS (`.code-view`, `.md-raw`) aligned with terminal raw (same font-family, line-height, padding); `.fb-reader-body` fixed from undefined `--fs-text` to `--text-size`
- **2026-02-26**: Default view is Raw — new terminal tabs default to `rawMode: true`; per-tab view persists across tab switches and browser refreshes

## Recently Completed (cont. 27)
- **2026-02-27**: Fix text selection jumping in Raw/Clean view — 1s poll was replacing DOM content while user was selecting text. Added `window.getSelection()` guard in `pollTab()` to skip `renderOutput()` when an active Range selection exists inside the output element. State still updates in background; next poll after selection release renders latest content.

## Recently Completed (cont. 28)
- **2026-03-02**: Markdown File Browser overlay — "Files" button in topbar opens fullscreen overlay showing session working directories as entry points. Navigate into dirs (shows subdirs + `.md` files only), tap a `.md` file to read with full markdown rendering. Backend: `_get_session_cwds()` (single `tmux list-panes` call, no capture-pane), `_is_path_allowed()` (realpath + prefix check), `GET /api/files` (dir listing), `GET /api/files/read` (file content, 1MB limit). Security: paths restricted to session cwds, hidden files excluded. JS uses event delegation throughout (no inline onclick), navigation history stack with scroll restore, breadcrumbs with `data-fb-action` delegation.

## Recently Completed (cont. 29)
- **2026-03-02**: Fix per-tab draft text not persisting across tab switches — `createTab` (both terminal and file tab versions) was setting `pane.activeTabId = id` before `focusTab()`, causing `tabChanged = false` and skipping draft save. Removed premature assignment so `focusTab` properly saves old tab's textarea content. Also added draft restoration in `closeTab` when active tab is closed and next tab becomes active.

## Recently Completed (cont. 30)
- **2026-03-02**: Box-drawing table rendering — CC renders markdown tables as ASCII box art (┌─┬─┐ / │ / └─┴─┘) that's 270+ chars wide, unreadable on mobile. Now detected and converted to responsive HTML `<table>` in both views. Six changes: (1) `.box-table` CSS with word-break/vertical-align, (2) `boxTableToHtml()` parses separator/content sections, handles multi-line cells, (3) `renderRawWithTables()` for raw view (falls back to textContent when no tables), (4) `cleanTerminal()` pre-marks table blocks so rules 2/3 skip them, (5) `parseCCTurns()` box-drawing line filter exempts lines with table corner/intersection chars (┌┐└┘┬┼┴), (6) `md()` extracts table blocks before code-block wrapping.

## Recently Completed (cont. 31)
- **2026-03-02**: File path hyperlinks in output — file paths in Claude's responses (e.g. `server.py:42`, `src/components/App.tsx`) become clickable links that open in file tabs. Regex detects paths with directory separators or `:line` suffixes; word boundary prevents partial extension matches (`js` vs `json`). Relative paths resolved against tab's cwd from dashboard. Works in both Raw and Clean views. DOM tree walker skips `<a>`, `<pre>`, `<textarea>`, `<input>` nodes.

## Recently Completed (cont. 32)
- **2026-03-03**: Settings panel — gear icon (⚙) in topbar opens dropdown with text size pill buttons (4 tiers, active highlighted) and file links ON/OFF toggle. `_fileLinksEnabled` flag guards `linkifyFilePaths()` calls; toggling re-renders all visible tabs immediately preserving scroll position. Persisted to `prefs fileLinks`. Click-outside dismisses panel. Replaces old standalone text size cycling button.
- **2026-03-03**: Scroll to bottom on tab focus — output defaults to bottom on page refresh and tab switch. Per-tab scroll position saved (`tabStates[id].savedScrollTop`) when switching away, restored when switching back. `#topbar` got `position:relative` for settings panel absolute positioning.

## Recently Completed (cont. 33)
- **2026-03-03**: Raw view mobile formatting — three fixes: (1) trim trailing whitespace per line (removes tmux padding), (2) collapse 4+ consecutive blank lines to 3 (CC TUI fills content-to-status-bar gap with empty lines), (3) truncate `─` (U+2500) horizontal dividers >40 chars to 40 (273-char dividers wrapped into multiple rows on mobile). Also fixed box-drawing table detection in Raw view — `_tblStartRe`/`_tblEndRe` were missing the `m` (multiline) flag, so `.test(display)` only checked the first line and missed tables in the middle of output.

## Recently Completed (cont. 34)
- **2026-03-03**: CC TUI word-wrap rejoining in Raw view — CC's TUI wraps prose at the terminal width, creating hard line breaks mid-sentence that double-wrap on mobile. Raw view now dynamically detects the wrap width (`max(line lengths) - 4`) and joins continuation lines (2-space indent + lowercase start) that hit the boundary. Only lines at the actual wrap width are joined — shorter lines (commands, bullet points) stay separate.
- **2026-03-03**: File hyperlinks open in other pane — clicking a file path hyperlink in Claude output now opens the file tab in a different pane when 2+ panes exist, enabling side-by-side viewing. Single-pane layout still opens in the same pane.

## Recently Completed (cont. 35)
- **2026-03-04**: Window popup button swap — X button closes popup (neutral gray, was red/destructive), "Close Window" button at bottom does the tmux window close (red text). Clearer separation of dismiss vs destructive action.
- **2026-03-04**: Bare filename hyperlinks — file links now match filenames without path separators (e.g. `server.py`, `CLAUDE.md`), resolved against tab's cwd. Previously required `/` in path or `:line` suffix.
- **2026-03-04**: Pane limit raised 6 → 12 for large-screen layouts with many terminals.
- **2026-03-04**: Cross-pane vertical split drag fix — tab-level `dragover`/`drop` handlers were calling `stopPropagation()` unconditionally, blocking pane-level split detection for cross-pane drags. Now uses `_dragSrcTabId` global (set on `dragstart`) to only stop propagation for same-pane reorder; cross-pane drags bubble through to pane handler for top/bottom split zones.
- **2026-03-04**: Scroll-to-bottom on tab switch — `_scrollToBottom` flag in `tabStates`, set on tab creation and every `focusTab` call. `pollTab` honors the flag even when content hasn't changed, ensuring scroll happens after the element is visible and laid out. Removed per-tab scroll position save/restore (always scrolls to bottom now).

## Active Work
None

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` runs N capture-pane calls (one per window) — could be slow with many windows
