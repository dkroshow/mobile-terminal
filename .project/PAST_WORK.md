# Past Work

Items moved from CURRENT_WORK.md on 2026-03-10.

## 2026-03-09
- Context gauge UX — moved to input bar, fixed % remaining semantics, unified `_ctxCls()` color coding
- Context gauge integration — inlined JSONL-based context window utilization, per-window matching, sidebar/pane/modal display

## 2026-03-08
- Context gauge integration — inlined JSONL-based context window utilization into server.py

## 2026-03-07
- Fix Raw view word-wrap rejoining — `[a-z]` → `[a-zA-Z]`, `max - 4` → `max * 0.85` threshold, `(  \S|⏺|❯)` on prev-line check
- File tab tooltip — hovering shows full filepath via `title` attribute
- Add-pane button on mobile — visible with compact sizing

## 2026-03-04
- Window popup button swap — X closes popup, "Close Window" does tmux close
- Bare filename hyperlinks — match filenames without path separators
- Pane limit raised 6 → 12
- Cross-pane vertical split drag fix — `_dragSrcTabId` for same-pane vs cross-pane
- Scroll-to-bottom on tab switch — `_scrollToBottom` flag

## 2026-03-03
- Settings panel — gear icon dropdown with text size pills and file links toggle
- Scroll to bottom on tab focus — defaults to bottom on refresh/switch
- Raw view mobile formatting — trim trailing whitespace, collapse blanks, truncate dividers
- CC TUI word-wrap rejoining in Raw view — dynamic wrap width detection
- File hyperlinks open in other pane — side-by-side viewing

## 2026-03-02
- File path hyperlinks in output — clickable links to file tabs, regex detection
- Box-drawing table rendering — ASCII box art → responsive HTML `<table>`
- Fix per-tab draft text not persisting across tab switches

## 2026-02-27
- Fix text selection jumping — `getSelection()` guard skips DOM update during active selection

## 2026-02-26
- Fix prompt text leaking into Claude's last response (3 layers)
- Fix empty pane can't be closed
- Fix queue not dispatching in raw mode
- Per-tab draft text persistence
- Pane dividers scale with window resize
- Text size alignment — mono/code sizes step down one tier
- Default view is Raw

## 2026-02-24
- Fix submitted prompt appearing in Claude's last response — `parseCCTurns()` truncation
- Raw/Clean view persistence across browser refresh
- Pane close layout fix — clear inline flex/width/height
- Defensive `file-tab-active` sync
- File tree refresh button

## 2026-02-23
- File Editor with Auto-Refresh — edit/save/conflict detection/mtime polling

## 2026-02-22
- File Browser sidebar integration — dual-tab sidebar (Sessions | Files), lazy file tree
- Ghost text filtering via ANSI codes — server-side `strip_ghost_text()`
- File tree enhancements — hidden files shown, drag reorder roots
- Font rendering improvements

## 2026-02-21
- Fix AskUserQuestion/plan option text invisible — `stripSuggestion`, `cleanTerminal`, `parseCCTurns` fixes

## 2026-02-16
- Notifications when CC finishes — client + server monitor dual paths
- Raw view darker background
- Fix notification monitor async — `run_in_executor`
- Fix tab drag reorder — insertion line indicator

## 2026-02-15
- Fix `/clear` button, bracketed paste, activity age, new window session targeting
- Fix send losing typed text, slash commands in CC
- Status dots on pane tabs, sidebar click targets whole row
- Removed sidebar memo, auto-unhide session on tab focus
- Master Notes — global notepad in topbar
- Fix plan mode text disappearing, tab dots not updating, links in new tab

## 2026-02-14
- Server-side preferences persistence — `~/.mobile-terminal-prefs.json`
- Fix Enter key across desktop and mobile — dual keydown + beforeinput

## 2026-02-13
- Queue panel tabs, raw view ghost filtering, paste/send reliability
- Sidebar active highlight, working indicator after hard refresh
- Clean view formatting, queue premature dispatch fix
- Sidebar snippet, sidebar memo
- Architecture audit & cleanup (5-agent parallel audit)
- Sidebar snippet layout, text scaling, false positive status dot fix
- Per-pane Keys/Commands trays, Left/Right arrow keys
- Tab reorder within pane, hidden sessions, activity age per window
- Snippet in collapsed sidebar, queue draft preservation
- Reliable tmux send, activity age blink fix

## 2026-02-12
- CC status detection rewrite, multi-pane system, dashboard view
- Combined tmux button, details popup, rename modal
- Notepad, layout persistence, batch UI improvements (9 items)
- Task Queue + enhancements, sidebar resize
- Context remaining display, large text paste fix
- Queue UX improvements, permission mode, sidebar text labels
- CC session boundary detection, queue Active/Past split
- Phantom text fix, hard refresh, desktop keyboard forwarding, large text input

## 2026-02-11
- Prefill appends, session switch clears stale output
- tmux session navigator, top bar redesign, Commands panel
- Tab-based window management, chat parser rewrite, instant message feedback

## 2026-02-10
- Added launchd LaunchAgent for always-on server
