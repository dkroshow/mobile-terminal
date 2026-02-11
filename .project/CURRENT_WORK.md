# Current Work

## Recently Completed
- **2026-02-11**: Tab-based window management — named pill tabs, rename (long-press), close button, scrollable tab row
- **2026-02-11**: Rewrote chat mode parser for Claude Code — proper ❯/⏺ detection, strips tool calls/diffs/status, CC vs plain terminal fallback
- **2026-02-11**: Instant message feedback — client-side pendingMsg injection before poll catches up, pulsing "Thinking..." indicator
- **2026-02-11**: Suggestion filtering — idle prompt ghost text removed via sawStatus heuristic + column-0 ❯ detection
- **2026-02-11**: Added Enter button to Commands panel for mobile use
- **2026-02-10**: Added launchd LaunchAgent for always-on server
- **2026-02-09**: Made repo public-ready — env var config, removed personal refs, added README/requirements/LICENSE/.gitignore

## Active Work
None

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
