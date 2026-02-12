# Current Work

## Recently Completed
- **2026-02-11**: tmux session navigator — dropdown panel to browse/switch all tmux sessions and windows; mutable session backend
- **2026-02-11**: Top bar redesign — tmux button, Clean/Raw view toggle, title
- **2026-02-11**: Commands panel — Wrap Up, Clear, Exit, Resume, Rename; Resume auto-switches to Raw view
- **2026-02-11**: Ghost suggestion fix — isIdle() checks absence of processing signals; output staleness detection (5s)
- **2026-02-11**: Fixed working/thinking indicator — client-side `awaitingResponse` state; detects idle via `isIdle()`
- **2026-02-11**: Improved paragraph rendering — blank lines preserved in assistant turns
- **2026-02-11**: Tab-based window management, chat parser rewrite, instant message feedback
- **2026-02-10**: Added launchd LaunchAgent for always-on server

## Active Work
None

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
