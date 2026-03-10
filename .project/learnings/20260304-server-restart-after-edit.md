---
type: gotcha
tags: [deployment, launchd, server, debugging, performance]
created: 2026-03-04
updated: 2026-03-10
---

# Server Must Be Restarted After Editing server.py

## What
The mobile-terminal server runs as a launchd LaunchAgent. Editing `server.py` does NOT make changes live — the server must be restarted via `launchctl unload/load`. This applies to ALL changes: bug fixes, performance fixes, feature additions.

## Context
Hit this twice:
1. **2026-03-04**: Spent significant time debugging a scroll-to-bottom feature — added alerts, debug divs, multiple fix attempts, none appeared to work. Root cause: server was serving stale code the entire time.
2. **2026-03-10**: Applied performance fixes (`run_in_executor` for all 16 async endpoints) but didn't restart. Server stayed at 95.5% CPU for 2+ hours, reading 25+ MB of JSONL files synchronously on the event loop. Diagnosed as "still slow" when the fix was already on disk — just not deployed.

## Symptoms & Fixes

| Symptom | Cause | Fix |
|---|---|---|
| Code changes appear to have no effect | Server serving old code from memory | Restart via `launchctl unload/load` |
| Server at high CPU (50-100%) for extended time | Blocking sync calls on async event loop (old code) | Deploy `run_in_executor` fixes + restart |
| "Still slow after fixing" | Fix saved to disk but server not restarted | Always restart after editing server.py |
| Page load slow, UI laggy | Event loop blocked by subprocess/file I/O | Check if server code matches disk: `curl -s http://localhost:7681/ \| grep -c 'identifier'` |

## What Works

**After ANY edit to `server.py`, always restart:**
```bash
launchctl unload ~/Library/LaunchAgents/com.kd.mobile-terminal.plist && launchctl load ~/Library/LaunchAgents/com.kd.mobile-terminal.plist
```

**Verify new code is running:**
```bash
# Check response time (should be <50ms for page, <40ms for output)
curl -s -o /dev/null -w "%{time_total}s\n" http://localhost:7681/

# Check CPU (should be <5% when idle)
ps aux | grep server.py | grep -v grep | awk '{print $3"%"}'
```

**If diagnosing performance:**
```bash
# Quick CPU check
ps aux | grep server.py | grep -v grep

# Process sample (macOS)
sample <PID> 1
```

## Key Files
- `server.py` — the single-file server
- `~/Library/LaunchAgents/com.kd.mobile-terminal.plist` — launchd config
