---
type: gotcha
tags: [deployment, launchd, server, debugging]
created: 2026-03-04
---

# Server Must Be Restarted After Editing server.py

## What
The mobile-terminal server runs as a launchd LaunchAgent. Editing `server.py` does NOT make changes live — the server must be restarted via `launchctl unload/load`.

## Context
Spent significant time debugging a scroll-to-bottom feature, adding alerts, debug divs, and multiple fix attempts — none appeared to work. The root cause: the server was serving the old code the entire time.

## What Didn't Work
- Adding `alert()`, `console.log()`, visible debug divs — none appeared because the server was serving stale code
- Multiple code fix iterations appeared to fail, leading to increasingly complex "fixes" for a problem that was already solved

## What Works
After editing `server.py`, always restart:
```bash
launchctl unload ~/Library/LaunchAgents/com.kd.mobile-terminal.plist && sleep 2 && launchctl load ~/Library/LaunchAgents/com.kd.mobile-terminal.plist
```

Then verify the new code is being served:
```bash
curl -s http://localhost:7681/ | grep -c 'some_new_identifier'
```

## Key Files
- `server.py` — the single-file server
- `~/Library/LaunchAgents/com.kd.mobile-terminal.plist` — launchd config
