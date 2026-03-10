# Mobile Terminal — Claude Context

## What This Is

A single-file web UI (`server.py`) for controlling tmux sessions from a phone. Python + FastAPI backend, inline HTML/CSS/JS frontend. No build step, no frameworks.

## Current State

Working and deployed as a macOS LaunchAgent (`com.kd.mobile-terminal`). Runs on port 7681.

### Architecture
- `server.py` — everything: FastAPI app, tmux subprocess calls, inline HTML template
- HTML is a string constant (`HTML`) with `__TITLE__` placeholder
- Frontend: vanilla JS, 1-second polling for output, no WebSocket
- Dark theme (custom colors: `#191a1b` bg, `#e8e6e3` text, `#D97757` accent)
- Chat mode: Claude Code-aware parser renders ❯/⏺ as conversation turns
- Raw mode: plain terminal output

### Key Features
- Send commands, see output (1s poll)
- Multi-pane layout (up to 3 panes, each with own tab bar and input)
- Drag-and-drop tabs between panes
- Sidebar with session/window nav, CC status indicators (Standby/Working/Thinking), per-window details popup
- Named tmux window tabs (create/switch/close/rename)
- Special keys: Ctrl-C, Up/Down, Tab, Escape, Enter
- iOS keyboard handling (visualViewport API to keep input above keyboard)
- Chat mode for Claude Code output (detects ❯/⏺, strips tool calls/diffs/status, renders markdown)
- Instant message feedback (client-side injection before poll catches up)
- Thinking indicator (pulsing "Thinking..." while Claude processes)
- Suggestion filtering (idle prompt ghost text hidden via sawStatus heuristic)
- Plain terminal fallback (monospace card for non-CC sessions)
- 4-tier text size (A-- through A+)

### API
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/` | Serve the HTML UI |
| GET | `/api/output` | Get current pane content (last 200 lines) |
| POST | `/api/send` | Send command `{"cmd": "..."}` |
| GET | `/api/key/{key}` | Send special key (C-c, Up, Down, Tab, Enter, Escape) |
| GET | `/api/windows` | List tmux windows |
| POST | `/api/windows/new` | Create new window |
| POST | `/api/windows/{index}` | Switch to window |
| PUT | `/api/windows/current` | Rename active window `{"name": "..."}` |
| POST | `/api/windows/current/reset-name` | Reset window to auto-naming |
| PUT | `/api/windows/{index}` | Rename window `{"name": "..."}` |
| DELETE | `/api/windows/{index}` | Close window |
| GET | `/api/sessions` | List all sessions with windows |
| POST | `/api/sessions/{name}` | Switch to session |
| PUT | `/api/sessions/{name}` | Rename session `{"name": "..."}` |
| GET | `/api/pane-info` | Get cwd, PID, session, window of active pane |
| GET | `/api/dashboard` | All sessions/windows with CC status (sidebar) |
| GET | `/api/files` | List directory contents (file tree) |
| GET | `/api/files/read` | Read file content + mtime |
| GET | `/api/files/mtime` | Lightweight mtime check (for polling) |
| PUT | `/api/files/write` | Write file `{path, content, mtime}` with conflict detection |

### Config (env vars)
- `TMUX_SESSION` — session name (default: `mobile`)
- `TMUX_WORK_DIR` — starting dir (default: `~`)
- `TERMINAL_TITLE` — browser tab title
- `HOST` / `PORT` — bind address (default: `127.0.0.1:7681`)

### Service Management
```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.kd.mobile-terminal.plist

# Start
launchctl load ~/Library/LaunchAgents/com.kd.mobile-terminal.plist

# Logs
tail -f /tmp/mobile-terminal.log
```

## Cross-Project Role

mobile-terminal provides **HTTP APIs** consumed by other repos. See `~/Code/james/docs/VISION.md` → "Cross-Project Architecture" for the full repo boundary contract.

Key APIs consumed externally:
- `GET /api/dashboard` — CC session state (status, context %, CWD, gauge metrics) — used by james
- `POST /api/send` — send commands to CC sessions — used by james Telegram bot bridge
- `GET /api/output` — read CC session output — used by james Telegram bot bridge

## Constraints
- Single file is fine (current pattern), or can split if needed
- No JS frameworks — keep it vanilla or minimal
- Must work well on iPhone (primary use case)
- Service is running in production — be careful with changes
- **CRITICAL: After editing `server.py`, you MUST restart the server** — `launchctl unload ~/Library/LaunchAgents/com.kd.mobile-terminal.plist && launchctl load ~/Library/LaunchAgents/com.kd.mobile-terminal.plist`. The server runs from memory; edits on disk have zero effect until restart. This has caused multi-hour debugging sessions twice (stale code looks like "my fix didn't work" or "server is still slow"). Verify with `curl -s -o /dev/null -w "%{time_total}s" http://localhost:7681/` (should be <50ms)
- **All async endpoint handlers MUST use `run_in_executor` for subprocess/file I/O** — sync calls block the event loop and cause 95%+ CPU. The gauge system reads 25+ MB of JSONL; dashboard does N capture-pane calls. Without executor offloading, every poll blocks every other request
