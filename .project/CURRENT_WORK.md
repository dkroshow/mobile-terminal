# Current Work

## Recently Completed
- **2026-02-11**: Fixed working/thinking indicator — replaced terminal-parsing `sawStatus` with client-side `awaitingResponse` state; detects idle via bare `❯` prompt; "Working..." persists until Claude finishes
- **2026-02-11**: Improved paragraph rendering — blank lines preserved in assistant turn parsing for proper markdown paragraph breaks
- **2026-02-11**: Added "Wrap" button in Commands panel (prefills `/_my_wrap_up` into input field)
- **2026-02-11**: Tab-based window management — named pill tabs, rename (long-press), close button, scrollable tab row
- **2026-02-11**: Rewrote chat mode parser for Claude Code — proper ❯/⏺ detection, strips tool calls/diffs/status, CC vs plain terminal fallback
- **2026-02-11**: Instant message feedback — client-side pendingMsg injection before poll catches up
- **2026-02-11**: Added Enter button to Commands panel for mobile use
- **2026-02-10**: Added launchd LaunchAgent for always-on server

## Active Work
- **tmux navigation redesign** — user wants a "tmux" button at top that opens a panel to see/switch between tmux sessions and windows (currently has inline tab bar for windows only)

## Up Next
- tmux session/window navigation panel (in progress)
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
