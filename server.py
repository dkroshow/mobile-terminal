#!/usr/bin/env python3
"""Mobile web terminal for remote tmux control."""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI()
SESSION = os.environ.get("TMUX_SESSION", "mobile")
WORK_DIR = os.environ.get("TMUX_WORK_DIR", str(Path.home()))
_current_session = SESSION  # Mutable — can be switched at runtime
TITLE = os.environ.get("TERMINAL_TITLE", "Mobile Terminal")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7681"))


ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'
    r'|\x1b\][^\x07]*\x07'
    r'|\x1b\([A-Z]'
    r'|\x1b[>=]'
    r'|\x0f'
)


def ensure_session():
    r = subprocess.run(["tmux", "has-session", "-t", _current_session], capture_output=True)
    if r.returncode != 0:
        work_dir = WORK_DIR if Path(WORK_DIR).is_dir() else str(Path.home())
        subprocess.run([
            "tmux", "new-session", "-d", "-s", _current_session,
            "-x", "80", "-y", "50", "-c", work_dir,
        ])


def _tmux_target(session=None, window=None):
    """Build a tmux target string like 'session:window' or just 'session'."""
    s = session or _current_session
    if window is not None:
        return f"{s}:{window}"
    return s


def send_keys(text: str, session=None, window=None):
    target = _tmux_target(session, window)
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", text])
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"])


def send_special(key: str, session=None, window=None):
    target = _tmux_target(session, window)
    subprocess.run(["tmux", "send-keys", "-t", target, key])


def get_output(session=None, window=None) -> str:
    target = _tmux_target(session, window)
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", "-200"],
        capture_output=True, text=True,
    )
    text = ANSI_RE.sub("", r.stdout)
    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', text)
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def get_pane_preview(session: str, window: int, lines: int = 5) -> str:
    """Capture last N lines from a specific pane for preview."""
    target = f"{session}:{window}"
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    text = ANSI_RE.sub("", r.stdout)
    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', text)
    return text.strip()


def detect_cc_status(text: str, activity_age: float = None) -> tuple:
    """Detect if text is Claude Code output and its status.
    Returns (is_cc, status): idle, working, or thinking.
    """
    is_cc = '\u276f' in text and '\u23fa' in text
    if not is_cc:
        return False, None

    lines = text.split('\n')

    # --- Text signals ---

    # 1. "esc to interrupt" on the status bar (line starting with ⏵)
    #    In current CC, this appears on the same line as the permissions bar:
    #    "⏵⏵ bypass permissions on (shift+tab to cycle) · 3 files · esc to interrupt"
    has_working = False
    for line in lines[-3:]:
        if '\u23f5' in line and 'esc to interrupt' in line:
            has_working = True
            break

    # 2. Thinking: · at START of any line in last 20 lines
    tail = '\n'.join(lines[-20:])
    has_thinking = bool(re.search(r'^\u00b7', tail, re.MULTILINE))

    # --- Determine status ---
    # Note: ❯ prompt is always visible in CC TUI chrome (fixed layout),
    # so we can't use it to distinguish idle vs working.
    # Instead, activity_age is the primary "is something happening?" signal.
    if has_working:
        status = 'working'
    elif has_thinking:
        status = 'thinking'
    elif activity_age is not None and activity_age < 5:
        # Terminal is actively receiving output — Claude is doing something
        # (idle CC produces NO output, so activity_age grows quickly)
        status = 'working'
    else:
        status = 'idle'

    return True, status


def get_dashboard() -> dict:
    """Get lightweight status for all sessions and windows."""
    now = time.time()
    # Single call to get all pane metadata including activity timestamp
    r = subprocess.run(
        ["tmux", "list-panes", "-a", "-F",
         "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_current_path}\t#{pane_current_command}\t#{window_active}\t#{session_attached}\t#{pane_pid}\t#{window_activity}"],
        capture_output=True, text=True,
    )
    sessions = {}
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        sname, widx, wname, cwd, cmd, wactive, sattached, pid, wactivity = parts
        if sname not in sessions:
            sessions[sname] = {
                "name": sname,
                "attached": sattached == "1",
                "windows": [],
            }
        # Activity age: seconds since tmux last received output for this pane
        try:
            activity_age = now - int(wactivity)
        except (ValueError, TypeError):
            activity_age = None
        # Get preview for CC detection (20 lines for better signal coverage)
        preview = get_pane_preview(sname, int(widx), lines=20)
        is_cc, cc_status = detect_cc_status(preview, activity_age=activity_age)
        # Trim preview to last 5 lines for response
        preview_short = '\n'.join(preview.split('\n')[-5:])
        sessions[sname]["windows"].append({
            "index": int(widx),
            "name": wname,
            "active": wactive == "1",
            "cwd": cwd,
            "command": cmd,
            "pid": pid,
            "is_cc": is_cc,
            "cc_status": cc_status,
            "preview": preview_short,
        })
    return {"sessions": list(sessions.values())}


def list_sessions() -> list:
    """List all tmux sessions with their windows."""
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name} #{session_windows} #{session_attached}"],
        capture_output=True, text=True,
    )
    sessions = []
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(" ", 2)
        name = parts[0]
        # Get windows for this session
        wr = subprocess.run(
            ["tmux", "list-windows", "-t", name, "-F", "#{window_index} #{window_name} #{window_active}"],
            capture_output=True, text=True,
        )
        windows = []
        for wline in wr.stdout.strip().split("\n"):
            if not wline:
                continue
            wp = wline.split(" ", 2)
            windows.append({
                "index": int(wp[0]),
                "name": wp[1] if len(wp) > 1 else "",
                "active": wp[2] == "1" if len(wp) > 2 else False,
            })
        sessions.append({
            "name": name,
            "windows": windows,
            "attached": parts[2] == "1" if len(parts) > 2 else False,
        })
    return sessions


def list_windows() -> list:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", _current_session, "-F", "#{window_index} #{window_name} #{window_active}"],
        capture_output=True, text=True,
    )
    windows = []
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(" ", 2)
        windows.append({
            "index": int(parts[0]),
            "name": parts[1] if len(parts) > 1 else "",
            "active": parts[2] == "1" if len(parts) > 2 else False,
        })
    return windows


def new_window():
    work_dir = WORK_DIR if Path(WORK_DIR).is_dir() else str(Path.home())
    subprocess.run(["tmux", "new-window", "-t", _current_session, "-c", work_dir])


def select_window(index: int):
    subprocess.run(["tmux", "select-window", "-t", f"{_current_session}:{index}"])


HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#191a1b">
<title>__TITLE__</title>
<style>
:root {
  --bg: #191a1b; --bg2: #1e1f20; --surface: #232425;
  --border: rgba(255,255,255,0.07); --border2: rgba(255,255,255,0.12);
  --text: #e8e6e3; --text2: #8a8a8a; --text3: #5a5a5a;
  --accent: #D97757; --accent2: #c4693e; --red: #e5534b;
  --green: #3fb950; --orange: #d29922;
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-bottom: env(safe-area-inset-bottom, 0px);
  --sidebar-w: 260px;
  --text-size: 15px; --code-size: 12.5px; --mono-size: 12.5px;
  --turn-pad-v: 16px; --turn-pad-h: 18px; --turn-gap: 12px;
  --turn-radius: 18px; --line-h: 1.7;
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;
  overflow:hidden; -webkit-font-smoothing:antialiased; }

/* --- App layout --- */
#app { display:flex; height:100%; }

/* --- Sidebar --- */
#sidebar { width:var(--sidebar-w); min-width:0; background:var(--bg2);
  border-right:1px solid var(--border2); display:flex; flex-direction:column;
  transition:width .2s ease, min-width .2s ease; overflow:hidden;
  padding-top:var(--safe-top); }
#sidebar.collapsed { width:0; min-width:0; border-right:none; }
#sidebar-header { padding:12px 14px 8px; display:flex; align-items:center;
  justify-content:space-between; flex-shrink:0; }
#sidebar-header h2 { font-size:13px; font-weight:700; color:var(--text2);
  text-transform:uppercase; letter-spacing:0.5px; }
#collapse-btn { background:none; border:none; color:var(--text3); cursor:pointer;
  font-size:16px; padding:4px 6px; border-radius:6px; transition:all .15s; }
#collapse-btn:hover { color:var(--text); background:var(--surface); }
#sidebar-content { flex:1; overflow-y:auto; padding:0 8px 8px;
  -webkit-overflow-scrolling:touch; }
#sidebar-footer { padding:8px; flex-shrink:0; border-top:1px solid var(--border); }
#new-win-btn { width:100%; padding:8px; background:var(--surface);
  color:var(--text3); border:1px dashed var(--border2); border-radius:8px;
  font-size:12px; font-weight:500; font-family:inherit; cursor:pointer;
  transition:all .15s; }
#new-win-btn:hover { color:var(--text); border-color:var(--text3); }

/* Sidebar session groups */
.sb-session { margin-bottom:4px; }
.sb-session-header { display:flex; align-items:center; gap:6px; padding:8px 8px 4px;
  color:var(--text3); font-size:11px; font-weight:700; text-transform:uppercase;
  letter-spacing:0.5px; }
.sb-session-header .sb-badge { font-size:9px; padding:1px 5px; border-radius:6px;
  background:var(--accent); color:#fff; font-weight:500; text-transform:none;
  letter-spacing:0; }
.sb-win { display:flex; align-items:center; gap:8px; padding:6px 8px;
  border-radius:8px; cursor:pointer; transition:all .12s;
  -webkit-user-select:none; user-select:none; }
.sb-win:hover { background:var(--surface); }
.sb-win.has-tab { background:rgba(217,119,87,0.08); }
.sb-win.has-tab:hover { background:rgba(217,119,87,0.15); }
.sb-win-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.sb-win-dot.idle { background:var(--green); }
.sb-win-dot.working { background:var(--orange); animation:pulse 1.5s ease-in-out infinite; }
.sb-win-dot.thinking { background:var(--orange); animation:pulse 1s ease-in-out infinite; }
.sb-win-dot.waiting { background:var(--accent); animation:pulse 2s ease-in-out infinite; }
.sb-win-dot.none { background:var(--text3); opacity:0.3; }
.sb-win-info { flex:1; min-width:0; }
.sb-win-name { font-size:12px; font-weight:500; color:var(--text);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sb-win-cwd { font-size:10px; color:var(--text3);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sb-win-status { font-size:10px; font-weight:500; color:var(--text3);
  white-space:nowrap; flex-shrink:0; text-align:right; min-width:50px; }
.sb-win-status.working, .sb-win-status.thinking { color:var(--orange); }
.sb-win-status.waiting { color:var(--accent); }
.sb-win-status.idle { color:var(--green); }
.sb-win-detail-btn { background:none; border:none; color:var(--text3);
  font-size:14px; cursor:pointer; padding:2px 4px; border-radius:4px;
  flex-shrink:0; opacity:0; transition:opacity .15s; line-height:1; }
.sb-win:hover .sb-win-detail-btn { opacity:1; }
.sb-win-detail-btn:hover { color:var(--text); }

/* Mobile sidebar */
#sidebar-backdrop { display:none; position:fixed; inset:0; z-index:19;
  background:rgba(0,0,0,0.5); opacity:0; transition:opacity .2s; }
#sidebar-backdrop.open { opacity:1; }

@media (max-width:768px) {
  #sidebar { position:fixed; left:0; top:0; bottom:0; z-index:20;
    transform:translateX(-100%); transition:transform .25s ease;
    width:280px; }
  #sidebar.open { transform:translateX(0); }
  #sidebar.collapsed { transform:translateX(-100%); }
  #sidebar-backdrop.open { display:block; }
  .sb-win-detail-btn { opacity:1; }
}

/* --- Main area --- */
#main { flex:1; display:flex; flex-direction:column; min-width:0; position:relative; }

/* --- Top bar --- */
#topbar { background:var(--bg); padding:calc(var(--safe-top) + 6px) 12px 0;
  display:flex; flex-direction:column; gap:0; flex-shrink:0; z-index:10; }
#topbar-row { display:flex; align-items:center; gap:8px; padding-bottom:6px; }
#hamburger { display:none; background:none; border:none; color:var(--text2);
  font-size:20px; cursor:pointer; padding:4px 8px; border-radius:8px;
  transition:all .15s; flex-shrink:0; }
#hamburger:active { transform:scale(0.92); }
@media (max-width:768px) { #hamburger { display:block; } }
.topbar-btn { height:30px; padding:0 10px; border-radius:15px;
  background:var(--surface); color:var(--text2); border:1px solid var(--border);
  font-size:11px; font-weight:500; font-family:inherit; cursor:pointer;
  transition:all .15s; -webkit-user-select:none; user-select:none;
  flex-shrink:0; white-space:nowrap; }
.topbar-btn:active { transform:scale(0.96); opacity:0.8; }
.topbar-btn.on { background:var(--accent); color:#fff; border-color:var(--accent); }
@media (max-width:768px) { #add-pane-btn { display:none !important; } }

/* --- Panes container --- */
#panes-container { flex:1; display:flex; overflow:hidden; position:relative; }

/* --- Pane --- */
.pane { display:flex; flex-direction:column; min-width:0; overflow:hidden; flex:1;
  border-right:1px solid var(--border2); }
.pane:last-child { border-right:none; }
.pane.drag-over { outline:2px solid var(--accent); outline-offset:-2px; }
.pane.focused .pane-tab-bar { border-bottom-color:var(--accent); }

/* Pane tab bar */
.pane-tab-bar { display:flex; align-items:center; gap:2px;
  padding:4px 6px 0; background:var(--bg2);
  border-bottom:2px solid transparent; overflow-x:auto; flex-shrink:0;
  scrollbar-width:none; -ms-overflow-style:none; min-height:30px; }
.pane-tab-bar::-webkit-scrollbar { display:none; }
.pane-tab { display:flex; align-items:center; gap:4px; padding:3px 8px; height:24px;
  background:var(--surface); border:1px solid var(--border);
  border-radius:6px 6px 0 0; font-size:11px; font-weight:500;
  color:var(--text2); cursor:pointer; transition:all .12s;
  white-space:nowrap; flex-shrink:0; max-width:150px;
  -webkit-user-select:none; user-select:none; }
.pane-tab:hover { color:var(--text); }
.pane-tab.active { background:var(--bg); color:var(--text); border-bottom-color:var(--bg); }
.pane-tab.drag-over-tab { border-color:var(--accent); }
.pane-tab-name { overflow:hidden; text-overflow:ellipsis; }
.pane-tab-close { display:flex; align-items:center; justify-content:center;
  width:14px; height:14px; border-radius:3px; font-size:12px; line-height:1;
  color:var(--text3); cursor:pointer; transition:all .1s; }
.pane-tab-close:hover { background:rgba(255,255,255,0.1); color:var(--text); }
.pane-close-btn { background:none; border:none; color:var(--text3);
  font-size:14px; cursor:pointer; padding:2px 4px; margin-left:auto;
  flex-shrink:0; border-radius:3px; }
.pane-close-btn:hover { color:var(--red); background:rgba(255,255,255,0.05); }

/* Pane output */
.pane-output { flex:1; overflow-y:auto; -webkit-overflow-scrolling:touch; }
.pane-output.raw { padding:16px 14px;
  font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace;
  font-size:var(--mono-size); line-height:1.6; white-space:pre-wrap;
  word-break:break-word; color:#999; }
.pane-output.chat { display:flex; flex-direction:column; padding:10px 14px 20px; }

/* Pane input */
.pane-input { display:none; padding:8px 10px; background:var(--bg2);
  border-top:1px solid var(--border); flex-shrink:0; }
.pane-input.visible { display:flex; gap:8px; align-items:flex-end; }
.pane-input textarea { flex:1; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:16px; padding:8px 14px;
  font-size:var(--text-size); font-family:inherit; outline:none; resize:none;
  overflow-y:hidden; max-height:80px; line-height:1.4; }
.pane-input textarea::placeholder { color:var(--text3); }
.pane-input textarea:focus { border-color:rgba(217,119,87,0.5); }
.pane-input .pane-send { flex-shrink:0; width:32px; height:32px; border-radius:50%;
  background:var(--accent); border:none; color:#fff; cursor:pointer;
  display:flex; align-items:center; justify-content:center; }
.pane-input .pane-send svg { width:16px; height:16px; }
.pane-input .pane-send:active { transform:scale(0.92); }

/* Pane placeholder */
.pane-placeholder { flex:1; display:flex; align-items:center; justify-content:center;
  color:var(--text3); font-size:13px; padding:20px; text-align:center; }

/* --- Turn wrapper --- */
.turn { margin:0 0 4px; }
.turn + .turn { margin-top:var(--turn-gap); }
.turn.user + .turn.assistant,
.turn.assistant + .turn.user { margin-top:calc(var(--turn-gap) + 6px); }

/* Role label */
.turn-label { font-size:10px; font-weight:600; color:var(--text3);
  text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px;
  padding:0 4px; }
.turn.user .turn-label { text-align:right; padding-right:6px; }
.turn.assistant .turn-label { padding-left:2px; color:var(--accent); }

/* --- User bubble --- */
.turn.user .turn-body { background:var(--accent); color:#fff;
  padding:var(--turn-pad-v) var(--turn-pad-h);
  border-radius:var(--turn-radius) var(--turn-radius) 4px var(--turn-radius);
  max-width:85%; margin-left:auto; font-size:var(--text-size);
  line-height:1.55; word-break:break-word; }

/* --- Assistant card --- */
.turn.assistant .turn-body { background:var(--surface);
  padding:var(--turn-pad-v) var(--turn-pad-h);
  border-radius:4px var(--turn-radius) var(--turn-radius) var(--turn-radius);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;
  font-size:var(--text-size); line-height:var(--line-h); word-break:break-word;
  color:var(--text); }

/* --- Typography inside assistant cards --- */
.turn-body p { margin:0.5em 0; }
.turn-body p:first-child { margin-top:0; }
.turn-body p:last-child { margin-bottom:0; }
.turn-body strong { color:#fff; font-weight:600; }
.turn-body em { color:var(--text2); }
.turn-body h1 { font-size:1.3em; font-weight:700; margin:0.8em 0 0.3em;
  letter-spacing:-0.3px; }
.turn-body h2 { font-size:1.1em; font-weight:600; margin:0.6em 0 0.25em; }
.turn-body h3 { font-size:1em; font-weight:600; margin:0.5em 0 0.2em;
  color:var(--text2); }
.turn-body h1:first-child, .turn-body h2:first-child,
.turn-body h3:first-child { margin-top:0; }
.turn-body pre { background:var(--bg); border:1px solid rgba(255,255,255,0.06);
  border-radius:10px; padding:12px; margin:8px 0; overflow-x:auto;
  white-space:pre-wrap; word-break:break-word;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;
  font-size:var(--code-size); line-height:1.55; color:#b0b0b0; }
.turn-body code { background:rgba(255,255,255,0.07); padding:2px 5px;
  border-radius:5px; font-family:'SF Mono',ui-monospace,Menlo,monospace;
  font-size:0.84em; color:#ccc; }
.turn-body pre code { background:none; padding:0; font-size:inherit; color:inherit; }
.turn-body ul, .turn-body ol { padding-left:1.3em; margin:0.4em 0; }
.turn-body li { margin:0.25em 0; }
.turn-body li::marker { color:var(--text3); }
.turn-body blockquote { border-left:3px solid var(--border2); margin:0.5em 0;
  padding:4px 14px; color:var(--text2); }
.turn-body a { color:var(--accent); text-decoration:none; }
.turn-body.mono { font-family:'SF Mono',ui-monospace,Menlo,monospace;
  font-size:var(--mono-size); line-height:1.6; white-space:pre-wrap; word-break:break-word;
  color:#999; }
.turn-body .thinking { color:var(--text3); font-style:italic; animation:pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%,100%{ opacity:.4; } 50%{ opacity:1; } }
.turn-body hr { border:none; height:1px; background:var(--border2); margin:1em 0; }
.turn-body table { border-collapse:collapse; width:100%; margin:0.5em 0; font-size:13px; }
.turn-body th, .turn-body td { padding:5px 8px; text-align:left;
  border-bottom:1px solid var(--border); }
.turn-body th { color:var(--text2); font-weight:600; }
.turn-body details { background:var(--bg); border:1px solid var(--border);
  border-radius:10px; margin:8px 0; padding:0; overflow:hidden; }
.turn-body details summary { padding:10px 14px; cursor:pointer;
  color:var(--text2); font-size:12px; font-weight:500;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;
  list-style:none; display:flex; align-items:center; gap:6px; }
.turn-body details summary::before { content:'\\25B6'; font-size:8px;
  color:var(--text3); transition:transform .15s; }
.turn-body details[open] summary::before { transform:rotate(90deg); }
.turn-body details summary::-webkit-details-marker { display:none; }

/* --- Global bottom bar (single-pane mode) --- */
#bar { background:var(--bg2); flex-shrink:0;
  padding:10px 14px calc(var(--safe-bottom) + 10px);
  transition:bottom .1s; }
#bar.hidden { display:none; }
#input-row { display:flex; gap:10px; align-items:flex-end; }
#msg { flex:1; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:22px; padding:10px 16px;
  font-size:var(--text-size); font-family:inherit; outline:none;
  transition:border-color .2s, box-shadow .2s;
  resize:none; overflow-y:hidden; max-height:120px;
  line-height:1.4; }
#msg::placeholder { color:var(--text3); }
#msg:focus { border-color:rgba(217,119,87,0.5);
  box-shadow:0 0 0 3px rgba(217,119,87,0.1); }
#send-btn { flex-shrink:0; width:40px; height:40px; border-radius:50%;
  background:var(--accent); border:none; color:#fff; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  transition:transform .1s, background .15s; }
#send-btn:active { transform:scale(0.92); background:var(--accent2); }
#send-btn svg { width:18px; height:18px; }

/* Toolbar */
#toolbar { display:flex; gap:6px; margin-top:8px; }
.pill { padding:7px 14px; font-size:12px; font-weight:500;
  background:var(--surface); color:var(--text2); border:none;
  border-radius:100px; cursor:pointer; transition:all .15s;
  -webkit-user-select:none; user-select:none; }
.pill:active { transform:scale(0.96); opacity:0.8; }
.pill.on { background:var(--accent); color:#fff; }
.pill.danger { color:var(--red); }

/* Keys tray */
#keys, #cmds { max-height:0; overflow:hidden; transition:max-height .25s ease, margin .25s ease;
  display:flex; flex-wrap:wrap; gap:6px; margin-top:0; }
#keys.open, #cmds.open { max-height:100px; margin-top:8px; }

/* Window details modal */
#wd-overlay { display:none; position:fixed; inset:0; z-index:100;
  background:rgba(0,0,0,0.6); align-items:center; justify-content:center; }
#wd-overlay.open { display:flex; }
#wd-modal { background:var(--bg2); border:1px solid var(--border2);
  border-radius:16px; padding:20px; width:min(340px, 85vw); }
#wd-modal h3 { font-size:14px; font-weight:600; color:var(--text); margin-bottom:14px; }
.wd-row { display:flex; gap:10px; padding:8px 0; border-bottom:1px solid var(--border); }
.wd-row:last-child { border-bottom:none; }
.wd-label { color:var(--text3); font-size:11px; font-weight:600;
  text-transform:uppercase; letter-spacing:0.5px; min-width:60px; padding-top:1px; }
.wd-value { color:var(--text); font-size:13px; word-break:break-all;
  font-family:'SF Mono',ui-monospace,Menlo,monospace; }
#wd-rename-row { display:flex; gap:8px; margin-top:12px; }
#wd-rename-input { flex:1; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:8px; padding:8px 12px;
  font-size:13px; font-family:inherit; outline:none; }
#wd-rename-input:focus { border-color:rgba(217,119,87,0.5); }
.wd-save-btn { padding:8px 16px; background:var(--accent); color:#fff;
  border:none; border-radius:8px; font-size:13px; font-weight:500;
  font-family:inherit; cursor:pointer; }
.wd-btns { display:flex; gap:8px; margin-top:12px; }
.wd-btns button { flex:1; padding:9px; border:none; border-radius:8px;
  font-size:13px; font-weight:500; font-family:inherit; cursor:pointer; }
.wd-btn-close-win { background:rgba(229,83,75,0.15); color:var(--red); }
.wd-btn-dismiss { background:var(--surface); color:var(--text2); }
</style>
</head>
<body>

<div id="app">
<aside id="sidebar">
  <div id="sidebar-header">
    <h2>Sessions</h2>
    <button id="collapse-btn" onclick="toggleSidebar()" title="Collapse sidebar">&laquo;</button>
  </div>
  <div id="sidebar-content"></div>
  <div id="sidebar-footer">
    <button id="new-win-btn" onclick="newWin()">+ New Window</button>
  </div>
</aside>
<div id="sidebar-backdrop" onclick="closeMobileSidebar()"></div>

<main id="main">
  <div id="topbar">
    <div id="topbar-row">
      <button id="hamburger" onclick="openMobileSidebar()">&#9776;</button>
      <span style="flex:1"></span>
      <button class="topbar-btn" id="add-pane-btn" onclick="addPane()">+ Pane</button>
      <button class="topbar-btn" id="size-btn" onclick="cycleTextSize()">A</button>
      <button class="topbar-btn" id="view-btn" onclick="toggleRaw()">
        <span>View: </span><span id="view-label">Clean</span>
      </button>
    </div>
  </div>

  <div id="panes-container"></div>

  <div id="bar">
    <div id="input-row">
      <textarea id="msg" rows="1" placeholder="Enter command..."
        autocorrect="off" autocapitalize="none" autocomplete="off"
        spellcheck="false" enterkeyhint="send"></textarea>
      <button id="send-btn" onclick="sendGlobal()" aria-label="Send">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
          stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="19" x2="12" y2="5"></line>
          <polyline points="5 12 12 5 19 12"></polyline>
        </svg>
      </button>
    </div>
    <div id="toolbar">
      <button class="pill" id="keysBtn" onclick="toggleKeys()">Keys</button>
      <button class="pill" id="commandsBtn" onclick="toggleCmds()">Commands</button>
    </div>
    <div id="keys">
      <button class="pill" onclick="keyActive('Enter')">Return</button>
      <button class="pill danger" onclick="keyActive('C-c')">Ctrl-C</button>
      <button class="pill" onclick="keyActive('Up')">Up</button>
      <button class="pill" onclick="keyActive('Down')">Down</button>
      <button class="pill" onclick="keyActive('Tab')">Tab</button>
      <button class="pill" onclick="keyActive('Escape')">Esc</button>
    </div>
    <div id="cmds">
      <button class="pill" onclick="prefill('/_my_wrap_up')">Wrap Up</button>
      <button class="pill" onclick="prefill('/clear')">Clear</button>
      <button class="pill" onclick="prefill('/exit')">Exit</button>
      <button class="pill" onclick="sendResumeActive()">Resume</button>
    </div>
  </div>
</main>
</div>

<div id="wd-overlay" onclick="if(event.target===this)closeWD()">
  <div id="wd-modal">
    <h3 id="wd-title">Window Details</h3>
    <div id="wd-content"></div>
    <div id="wd-rename-row">
      <input id="wd-rename-input" type="text" placeholder="Window name..."
        autocorrect="off" autocapitalize="none" spellcheck="false">
      <button class="wd-save-btn" onclick="saveWDRename()">Save</button>
    </div>
    <div class="wd-btns">
      <button class="wd-btn-dismiss" onclick="closeWD()">Close</button>
      <button class="wd-btn-close-win" onclick="closeWDWindow()">Close Window</button>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/lib/marked.umd.min.js"></script>
<script>
// === Data model ===
let panes = [];         // [{ id, tabIds, activeTabId }]
let activePaneId = null;
let allTabs = {};       // tabId -> { session, windowIndex, windowName }
let tabStates = {};     // tabId -> { rawContent, last, rawMode, pendingMsg, pendingTime, awaitingResponse, lastOutputChange, pollInterval }
let _nextPaneId = 1;
let _nextTabId = 1;
let _dashboardData = null;
let _sidebarCollapsed = false;
let _wdSession = null, _wdWindow = null; // window details modal context

const M = document.getElementById('msg');
const bar = document.getElementById('bar');
const panesContainer = document.getElementById('panes-container');
const SEND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>';

// === Utility ===
function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function md(s) {
  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: false });
    return marked.parse(s);
  }
  return '<p>' + esc(s) + '</p>';
}
function abbreviateCwd(cwd) {
  if (!cwd) return '';
  const home = '/Users/';
  let p = cwd;
  if (p.startsWith(home)) {
    const afterHome = p.substring(home.length);
    const slash = afterHome.indexOf('/');
    p = slash >= 0 ? '~' + afterHome.substring(slash) : '~';
  }
  const parts = p.split('/').filter(Boolean);
  if (parts.length <= 2) return p;
  return parts.slice(-2).join('/');
}
function statusLabel(cc_status) {
  if (cc_status === 'working') return 'Working';
  if (cc_status === 'thinking') return 'Thinking';
  if (cc_status === 'idle') return 'Standby';
  return '';
}
function detectCCStatus(text) {
  // Quick client-side CC status detection from output text
  if (!isClaudeCode(text)) return null;
  const lines = text.split('\\n');
  // Check status bar (last line with ⏵) for "esc to interrupt"
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 5); i--) {
    if (/^\\s*\\u23f5/.test(lines[i])) {
      if (/esc to interrupt/.test(lines[i])) return 'working';
      break;
    }
  }
  // Check for thinking indicator
  if (/^\\u00b7/m.test(lines.slice(-15).join('\\n'))) return 'thinking';
  return 'idle';
}
function updateSidebarStatus(session, windowIndex, ccStatus) {
  const wid = session + ':' + windowIndex;
  const dot = document.querySelector('.sb-win-dot[data-wid="' + wid + '"]');
  const lbl = document.querySelector('.sb-win-status[data-wid="' + wid + '"]');
  if (dot) { dot.className = 'sb-win-dot ' + (ccStatus || 'idle'); }
  if (lbl) { lbl.className = 'sb-win-status ' + (ccStatus || 'idle'); lbl.textContent = statusLabel(ccStatus); }
}

// === Clean/parse (unchanged core logic) ===
function cleanTerminal(raw) {
  let lines = raw.split('\\n');
  lines = lines.filter(l => !/^\\s*[\\u256d\\u2570][\\u2500\\u2504\\u2501]+[\\u256e\\u256f]\\s*$/.test(l));
  lines = lines.map(l => l.replace(/^\\s*\\u2502\\s?/, '').replace(/\\s?\\u2502\\s*$/, ''));
  let text = lines.join('\\n');
  text = text.replace(/[\\u280b\\u2819\\u2839\\u2838\\u283c\\u2834\\u2826\\u2827\\u2807\\u280f]/g, '');
  text = text.replace(/\\n{3,}/g, '\\n\\n');
  return text.trim();
}
function isClaudeCode(text) { return /\\u276f/.test(text) && /\\u23fa/.test(text); }
function isIdle(text) {
  const lines = text.split('\\n');
  // Check status bar (last line starting with ⏵) for "esc to interrupt"
  // Only check the status bar line, not conversation content
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 5); i--) {
    if (/^\\s*\\u23f5/.test(lines[i])) {
      if (/esc to interrupt/.test(lines[i])) return false;
      break;
    }
  }
  // Check last 15 lines for thinking indicator (· at start of line)
  const tail = lines.slice(-15).join('\\n');
  if (/^\\u00b7/m.test(tail)) return false;
  return true;
}
function parseCCTurns(text) {
  const lines = text.split('\\n');
  const turns = []; let cur = null, inTool = false, sawStatus = false;
  for (const line of lines) {
    const raw = line.replace(/\\u00a0/g, ' ');
    const t = raw.trim();
    if (/^[\\u2500\\u2501\\u2504\\u2508\\u2550]{3,}$/.test(t) && t.length > 60) continue;
    if (/^[\\u23f5]/.test(t)) continue;
    if (/^\\u2026/.test(t)) continue;
    if (!t) { if (cur && cur.role === 'assistant' && !inTool) cur.lines.push(''); continue; }
    if (/^[\\u2720-\\u273f]/.test(t)) { sawStatus = true; continue; }
    if (/^\\u00b7/.test(t)) { sawStatus = true; continue; }
    if (/esc to interrupt/.test(t)) continue;
    if (/^\\u276f/.test(raw)) {
      if (cur) turns.push(cur);
      const msg = t.replace(/^\\u276f\\s*/, '').trim();
      cur = { role: 'user', lines: msg ? [msg] : [] }; inTool = false; sawStatus = false; continue;
    }
    if (/^\\u23fa/.test(t)) {
      const after = t.replace(/^\\u23fa\\s*/, '');
      if (/^(Bash|Read|Write|Update|Edit|Fetch|Search|Glob|Grep|Task|Skill|NotebookEdit|Searched for|Wrote \\d)/.test(after)) {
        // Tool call: close current card before entering tool mode
        if (cur && cur.lines && cur.lines.some(l => l.trim())) { turns.push(cur); cur = null; }
        inTool = true;
        continue;
      }
      // Regular assistant text — start new card if coming out of tool call
      if (inTool || !cur || cur.role !== 'assistant') {
        if (cur && cur.lines && cur.lines.some(l => l.trim())) turns.push(cur);
        cur = { role: 'assistant', lines: [] };
      }
      inTool = false;
      cur.lines.push(after); continue;
    }
    if (/^\\u23bf/.test(t)) { inTool = true; continue; }
    if (inTool) continue;
    if (cur && !inTool) {
      if (cur.role === 'assistant') cur.lines.push(t);
      else if (cur.role === 'user') cur.lines.push(t);
    }
  }
  if (cur) turns.push(cur);
  const filtered = turns.filter(t => t.lines.some(l => l.trim()));
  if (filtered.length > 0 && filtered[filtered.length - 1].role === 'user') {
    if (!sawStatus && isIdle(text)) filtered.pop();
  }
  return filtered;
}

// === Render output into target element ===
function renderOutput(raw, targetEl, state) {
  if (state.rawMode) {
    targetEl.className = 'pane-output raw';
    targetEl.textContent = raw;
    return;
  }
  targetEl.className = 'pane-output chat';
  const clean = cleanTerminal(raw);
  let html = '';
  if (isClaudeCode(clean)) {
    const turns = parseCCTurns(clean);
    if (state.pendingMsg) {
      const userTurns = turns.filter(t => t.role === 'user');
      const lastUser = userTurns[userTurns.length - 1];
      if (lastUser && lastUser.lines.join(' ').includes(state.pendingMsg.substring(0, 20)))
        state.pendingMsg = null;
    }
    if (state.awaitingResponse) {
      const elapsed = Date.now() - state.pendingTime;
      if (elapsed > 3000 && isIdle(clean)) state.awaitingResponse = false;
      if (elapsed > 3000 && state.lastOutputChange > 0 && (Date.now() - state.lastOutputChange) > 5000) state.awaitingResponse = false;
      if (elapsed > 180000) state.awaitingResponse = false;
    }
    let lastRole = '';
    for (const t of turns) {
      const text = t.lines.join('\\n').trim();
      if (!text) continue;
      if (t.role === 'user') {
        html += '<div class="turn user"><div class="turn-label">You</div><div class="turn-body">' + esc(text) + '</div></div>';
      } else {
        const label = lastRole !== 'assistant' ? 'Claude' : '';
        html += '<div class="turn assistant"><div class="turn-label">' + label + '</div><div class="turn-body">' + md(text) + '</div></div>';
      }
      lastRole = t.role;
    }
    if (state.pendingMsg)
      html += '<div class="turn user"><div class="turn-label">You</div><div class="turn-body">' + esc(state.pendingMsg) + '</div></div>';
    if (state.awaitingResponse)
      html += '<div class="turn assistant"><div class="turn-label">Claude</div><div class="turn-body"><p class="thinking">Working\\u2026</p></div></div>';
  } else {
    if (clean.trim())
      html = '<div class="turn assistant"><div class="turn-label">Terminal</div><div class="turn-body mono">' + esc(clean) + '</div></div>';
  }
  if (!html)
    html = '<div class="turn assistant"><div class="turn-label">Terminal</div><div class="turn-body"><p style="color:var(--text3)">Waiting for output...</p></div></div>';
  targetEl.innerHTML = html;
}

// === Pane management ===
function createPane() {
  if (panes.length >= 3) return null;
  const id = _nextPaneId++;
  panes.push({ id, tabIds: [], activeTabId: null });
  const el = document.createElement('div');
  el.className = 'pane';
  el.id = 'pane-' + id;
  el.innerHTML = '<div class="pane-tab-bar"></div>'
    + '<div class="pane-placeholder">Open a window from the sidebar</div>'
    + '<div class="pane-input"><textarea rows="1" placeholder="Enter command..."'
    + ' autocorrect="off" autocapitalize="none" autocomplete="off"'
    + ' spellcheck="false" enterkeyhint="send"></textarea>'
    + '<button class="pane-send" aria-label="Send">' + SEND_SVG + '</button></div>';
  panesContainer.appendChild(el);
  // Pane input handlers
  const ta = el.querySelector('.pane-input textarea');
  const sendBtn = el.querySelector('.pane-send');
  ta.addEventListener('input', () => { ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,80)+'px'; });
  ta.addEventListener('keydown', e => { if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); sendToPane(id); } });
  sendBtn.addEventListener('click', () => sendToPane(id));
  // Drop target
  el.addEventListener('dragover', e => { e.preventDefault(); el.classList.add('drag-over'); });
  el.addEventListener('dragleave', () => el.classList.remove('drag-over'));
  el.addEventListener('drop', e => {
    e.preventDefault(); el.classList.remove('drag-over');
    const tabId = parseInt(e.dataTransfer.getData('text/plain'));
    if (!tabId) return;
    moveTabToPane(tabId, id);
  });
  focusPane(id);
  updateLayout();
  return id;
}

function removePane(paneId) {
  const idx = panes.findIndex(p => p.id === paneId);
  if (idx < 0 || panes.length <= 1) return;
  const pane = panes[idx];
  // Close all tabs in this pane
  for (const tid of [...pane.tabIds]) closeTab(tid, true);
  panes.splice(idx, 1);
  const el = document.getElementById('pane-' + paneId);
  if (el) el.remove();
  if (activePaneId === paneId) focusPane(panes[0].id);
  updateLayout();
  renderSidebar();
}

function focusPane(paneId) {
  activePaneId = paneId;
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('focused'));
  const el = document.getElementById('pane-' + paneId);
  if (el) el.classList.add('focused');
}

function addPane() {
  createPane();
}

// === Tab management ===
function createTab(session, windowIndex, windowName, targetPaneId) {
  // Check if tab already exists in any pane
  for (const tid in allTabs) {
    const t = allTabs[tid];
    if (t.session === session && t.windowIndex === windowIndex) {
      focusTab(parseInt(tid));
      return;
    }
  }
  const paneId = targetPaneId || activePaneId || panes[0]?.id;
  if (!paneId) return;
  const pane = panes.find(p => p.id === paneId);
  if (!pane) return;

  const id = _nextTabId++;
  allTabs[id] = { session, windowIndex, windowName };
  tabStates[id] = {
    rawContent: '', last: '', rawMode: false,
    pendingMsg: null, pendingTime: 0,
    awaitingResponse: false, lastOutputChange: 0,
    pollInterval: null,
  };
  pane.tabIds.push(id);
  pane.activeTabId = id;

  // Create output element
  const paneEl = document.getElementById('pane-' + paneId);
  const placeholder = paneEl.querySelector('.pane-placeholder');
  if (placeholder) placeholder.remove();
  const outEl = document.createElement('div');
  outEl.className = 'pane-output chat';
  outEl.id = 'tab-output-' + id;
  outEl.style.display = 'none';
  outEl.innerHTML = '<div class="turn assistant"><div class="turn-label">Terminal</div>'
    + '<div class="turn-body"><p style="color:var(--text3)">Connecting...</p></div></div>';
  paneEl.querySelector('.pane-input').before(outEl);

  focusTab(id);
  renderPaneTabs(paneId);
  renderSidebar();
  startTabPolling(id);
}

function closeTab(tabId, skipRender) {
  stopTabPolling(tabId);
  // Find which pane has this tab
  let pane = null;
  for (const p of panes) {
    const idx = p.tabIds.indexOf(tabId);
    if (idx >= 0) { pane = p; p.tabIds.splice(idx, 1); break; }
  }
  delete allTabs[tabId];
  delete tabStates[tabId];
  const outEl = document.getElementById('tab-output-' + tabId);
  if (outEl) outEl.remove();

  if (pane) {
    if (pane.activeTabId === tabId) {
      pane.activeTabId = pane.tabIds[0] || null;
    }
    if (!pane.tabIds.length) {
      // Show placeholder
      const paneEl = document.getElementById('pane-' + pane.id);
      if (paneEl && !paneEl.querySelector('.pane-placeholder')) {
        const ph = document.createElement('div');
        ph.className = 'pane-placeholder';
        ph.textContent = 'Open a window from the sidebar';
        paneEl.querySelector('.pane-input').before(ph);
      }
    }
    if (!skipRender) {
      renderPaneTabs(pane.id);
      showActiveTabOutput(pane.id);
    }
  }
  if (!skipRender) { renderSidebar(); updatePolling(); }
}

function focusTab(tabId) {
  // Find pane
  for (const p of panes) {
    if (p.tabIds.includes(tabId)) {
      p.activeTabId = tabId;
      focusPane(p.id);
      renderPaneTabs(p.id);
      showActiveTabOutput(p.id);
      updatePolling();
      // Update view label
      const state = tabStates[tabId];
      if (state) document.getElementById('view-label').textContent = state.rawMode ? 'Raw' : 'Clean';
      return;
    }
  }
}

function moveTabToPane(tabId, targetPaneId) {
  // Remove from current pane
  let sourcePaneId = null;
  for (const p of panes) {
    const idx = p.tabIds.indexOf(tabId);
    if (idx >= 0) {
      sourcePaneId = p.id;
      p.tabIds.splice(idx, 1);
      if (p.activeTabId === tabId) p.activeTabId = p.tabIds[0] || null;
      break;
    }
  }
  if (sourcePaneId === targetPaneId) return;

  // Add to target pane
  const target = panes.find(p => p.id === targetPaneId);
  if (!target) return;
  // Move output element
  const outEl = document.getElementById('tab-output-' + tabId);
  const targetEl = document.getElementById('pane-' + targetPaneId);
  if (outEl && targetEl) {
    const placeholder = targetEl.querySelector('.pane-placeholder');
    if (placeholder) placeholder.remove();
    targetEl.querySelector('.pane-input').before(outEl);
  }
  target.tabIds.push(tabId);
  target.activeTabId = tabId;

  // Add placeholder back to source if empty
  if (sourcePaneId) {
    const srcPane = panes.find(p => p.id === sourcePaneId);
    const srcEl = document.getElementById('pane-' + sourcePaneId);
    if (srcPane && !srcPane.tabIds.length && srcEl && !srcEl.querySelector('.pane-placeholder')) {
      const ph = document.createElement('div');
      ph.className = 'pane-placeholder';
      ph.textContent = 'Open a window from the sidebar';
      srcEl.querySelector('.pane-input').before(ph);
    }
    renderPaneTabs(sourcePaneId);
    showActiveTabOutput(sourcePaneId);
  }
  focusPane(targetPaneId);
  renderPaneTabs(targetPaneId);
  showActiveTabOutput(targetPaneId);
  renderSidebar();
  updatePolling();
}

function renderPaneTabs(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const tabBar = paneEl.querySelector('.pane-tab-bar');
  let html = '';
  for (const tid of pane.tabIds) {
    const tab = allTabs[tid];
    if (!tab) continue;
    const active = tid === pane.activeTabId;
    html += '<div class="pane-tab' + (active ? ' active' : '') + '" draggable="true"'
      + ' data-tab-id="' + tid + '"'
      + ' onclick="focusTab(' + tid + ')">'
      + '<span class="pane-tab-name">' + esc(tab.windowName) + '</span>'
      + '<span class="pane-tab-close" onclick="event.stopPropagation();closeTab(' + tid + ')">&times;</span>'
      + '</div>';
  }
  // Close pane button (only if >1 pane)
  if (panes.length > 1) {
    html += '<button class="pane-close-btn" onclick="removePane(' + paneId + ')" title="Close pane">&times;</button>';
  }
  tabBar.innerHTML = html;
  // Setup drag events on tabs
  tabBar.querySelectorAll('.pane-tab[draggable]').forEach(tab => {
    tab.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/plain', tab.dataset.tabId);
      e.dataTransfer.effectAllowed = 'move';
      tab.style.opacity = '0.5';
    });
    tab.addEventListener('dragend', () => { tab.style.opacity = ''; });
  });
}

function showActiveTabOutput(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  paneEl.querySelectorAll('.pane-output').forEach(o => o.style.display = 'none');
  if (pane.activeTabId) {
    const outEl = document.getElementById('tab-output-' + pane.activeTabId);
    if (outEl) outEl.style.display = '';
  }
}

// === Layout ===
function updateLayout() {
  const multiPane = panes.length > 1;
  bar.classList.toggle('hidden', multiPane);
  document.querySelectorAll('.pane-input').forEach(pi => {
    pi.classList.toggle('visible', multiPane);
  });
}

// === Polling ===
function startTabPolling(tabId) {
  const state = tabStates[tabId];
  if (!state || state.pollInterval) return;
  pollTab(tabId);
  state.pollInterval = setInterval(() => pollTab(tabId), 1000);
}
function stopTabPolling(tabId) {
  const state = tabStates[tabId];
  if (!state || !state.pollInterval) return;
  clearInterval(state.pollInterval);
  state.pollInterval = null;
}
async function pollTab(tabId) {
  const tab = allTabs[tabId]; const state = tabStates[tabId];
  if (!tab || !state) return;
  try {
    const r = await fetch('/api/output?session=' + encodeURIComponent(tab.session) + '&window=' + tab.windowIndex);
    const d = await r.json();
    // Update sidebar status on every poll (1s latency vs 3s dashboard)
    const clean = cleanTerminal(d.output);
    const liveStatus = detectCCStatus(clean);
    if (liveStatus) updateSidebarStatus(tab.session, tab.windowIndex, liveStatus);
    if (d.output !== state.last) {
      state.lastOutputChange = Date.now();
      state.last = d.output; state.rawContent = d.output;
      const outEl = document.getElementById('tab-output-' + tabId);
      if (!outEl) return;
      const atBottom = outEl.scrollHeight - outEl.scrollTop - outEl.clientHeight < 80;
      renderOutput(d.output, outEl, state);
      if (atBottom) outEl.scrollTop = outEl.scrollHeight;
    }
  } catch(e) {}
}
function updatePolling() {
  // Poll all visible tabs (active tab in each pane)
  const visibleTabs = new Set();
  for (const p of panes) { if (p.activeTabId) visibleTabs.add(p.activeTabId); }
  for (const tid in allTabs) {
    const id = parseInt(tid);
    if (visibleTabs.has(id)) startTabPolling(id);
    else stopTabPolling(id);
  }
}

// === Send ===
function getActiveTab() {
  if (!activePaneId) return null;
  const pane = panes.find(p => p.id === activePaneId);
  if (!pane || !pane.activeTabId) return null;
  return { tabId: pane.activeTabId, tab: allTabs[pane.activeTabId], state: tabStates[pane.activeTabId] };
}

async function sendToPane(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const ta = paneEl.querySelector('.pane-input textarea');
  if (!ta) return;
  const text = ta.value; if (!text) return;
  ta.value = ''; ta.style.height = 'auto';
  const tab = allTabs[pane.activeTabId];
  const state = tabStates[pane.activeTabId];
  if (!tab || !state) return;
  state.pendingMsg = text; state.pendingTime = Date.now(); state.awaitingResponse = true;
  const outEl = document.getElementById('tab-output-' + pane.activeTabId);
  if (outEl) { renderOutput(state.rawContent || state.last, outEl, state); outEl.scrollTop = outEl.scrollHeight; }
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: text, session: tab.session, window: tab.windowIndex })
  });
}

async function sendGlobal() {
  const text = M.value; if (!text) return;
  const active = getActiveTab(); if (!active) return;
  M.value = ''; M.style.height = 'auto';
  active.state.pendingMsg = text; active.state.pendingTime = Date.now(); active.state.awaitingResponse = true;
  const outEl = document.getElementById('tab-output-' + active.tabId);
  if (outEl) { renderOutput(active.state.rawContent || active.state.last, outEl, active.state); outEl.scrollTop = outEl.scrollHeight; }
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: text, session: active.tab.session, window: active.tab.windowIndex })
  });
}

async function keyActive(k) {
  const active = getActiveTab(); if (!active) return;
  await fetch('/api/key/' + k + '?session=' + encodeURIComponent(active.tab.session) + '&window=' + active.tab.windowIndex);
}

function prefill(text) { M.value = M.value ? M.value + ' ' + text : text; M.focus(); }

async function sendResumeActive() {
  const active = getActiveTab(); if (!active) return;
  active.state.rawMode = true;
  document.getElementById('view-label').textContent = 'Raw';
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: '/resume', session: active.tab.session, window: active.tab.windowIndex })
  });
}

function toggleKeys() {
  const on = document.getElementById('keys').classList.toggle('open');
  document.getElementById('keysBtn').classList.toggle('on', on);
  if (on) { document.getElementById('cmds').classList.remove('open'); document.getElementById('commandsBtn').classList.remove('on'); }
}
function toggleCmds() {
  const on = document.getElementById('cmds').classList.toggle('open');
  document.getElementById('commandsBtn').classList.toggle('on', on);
  if (on) { document.getElementById('keys').classList.remove('open'); document.getElementById('keysBtn').classList.remove('on'); }
}

// === View toggle ===
function toggleRaw() {
  const active = getActiveTab(); if (!active) return;
  active.state.rawMode = !active.state.rawMode;
  document.getElementById('view-label').textContent = active.state.rawMode ? 'Raw' : 'Clean';
  const outEl = document.getElementById('tab-output-' + active.tabId);
  if (outEl) { renderOutput(active.state.rawContent || active.state.last, outEl, active.state); outEl.scrollTop = outEl.scrollHeight; }
}

// === Text size ===
const TEXT_SIZES = [
  { label: 'A--', text: '11px', code: '10px', mono: '10px', padV: '8px', padH: '10px', gap: '4px', radius: '12px', lineH: '1.4' },
  { label: 'A-',  text: '13px', code: '11px', mono: '11px', padV: '12px', padH: '14px', gap: '8px', radius: '14px', lineH: '1.55' },
  { label: 'A',   text: '15px', code: '12.5px', mono: '12.5px', padV: '16px', padH: '18px', gap: '12px', radius: '18px', lineH: '1.7' },
  { label: 'A+',  text: '17px', code: '14px', mono: '14px', padV: '18px', padH: '20px', gap: '14px', radius: '20px', lineH: '1.8' },
];
let _textSizeIdx = 2;
function applyTextSize(idx) {
  _textSizeIdx = idx;
  const s = TEXT_SIZES[idx];
  const r = document.documentElement.style;
  r.setProperty('--text-size', s.text); r.setProperty('--code-size', s.code);
  r.setProperty('--mono-size', s.mono); r.setProperty('--turn-pad-v', s.padV);
  r.setProperty('--turn-pad-h', s.padH); r.setProperty('--turn-gap', s.gap);
  r.setProperty('--turn-radius', s.radius); r.setProperty('--line-h', s.lineH);
  document.getElementById('size-btn').textContent = s.label;
  try { localStorage.setItem('textSize', idx); } catch(e) {}
}
function cycleTextSize() { applyTextSize((_textSizeIdx + 1) % TEXT_SIZES.length); }
try {
  const saved = localStorage.getItem('textSize');
  if (saved !== null) applyTextSize(parseInt(saved));
} catch(e) {}

// === Sidebar ===
function toggleSidebar() {
  _sidebarCollapsed = !_sidebarCollapsed;
  document.getElementById('sidebar').classList.toggle('collapsed', _sidebarCollapsed);
  document.getElementById('collapse-btn').textContent = _sidebarCollapsed ? '\\u00bb' : '\\u00ab';
}
function openMobileSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sidebar').classList.remove('collapsed');
  document.getElementById('sidebar-backdrop').classList.add('open');
}
function closeMobileSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
}

function renderSidebar() {
  const data = _dashboardData;
  if (!data) return;
  const content = document.getElementById('sidebar-content');
  // Check which tabs are open
  const openSet = new Set();
  for (const tid in allTabs) { const t = allTabs[tid]; openSet.add(t.session + ':' + t.windowIndex); }
  let html = '';
  for (const s of data.sessions) {
    html += '<div class="sb-session">';
    html += '<div class="sb-session-header">' + esc(s.name)
      + (s.attached ? ' <span class="sb-badge">attached</span>' : '') + '</div>';
    for (const w of s.windows) {
      const hasTab = openSet.has(s.name + ':' + w.index);
      const dotClass = w.is_cc ? (w.cc_status || 'idle') : 'none';
      const status = w.is_cc ? statusLabel(w.cc_status) : '';
      const statusClass = w.is_cc ? (w.cc_status || 'idle') : '';
      const wid = esc(s.name) + ':' + w.index;
      html += '<div class="sb-win' + (hasTab ? ' has-tab' : '') + '">'
        + '<div class="sb-win-dot ' + dotClass + '" data-wid="' + wid + '" onclick="event.stopPropagation();openTab(\\'' + esc(s.name).replace(/'/g, "\\\\'") + '\\',' + w.index + ',\\'' + esc(w.name).replace(/'/g, "\\\\'") + '\\')"></div>'
        + '<div class="sb-win-info" onclick="openTab(\\'' + esc(s.name).replace(/'/g, "\\\\'") + '\\',' + w.index + ',\\'' + esc(w.name).replace(/'/g, "\\\\'") + '\\')">'
        + '<div class="sb-win-name">' + esc(w.name) + '</div>'
        + '<div class="sb-win-cwd">' + esc(abbreviateCwd(w.cwd)) + '</div>'
        + '</div>'
        + '<div class="sb-win-status ' + statusClass + '" data-wid="' + wid + '">' + status + '</div>'
        + '<button class="sb-win-detail-btn" onclick="event.stopPropagation();openWD(\\'' + esc(s.name).replace(/'/g, "\\\\'") + '\\',' + w.index + ')" title="Details">&#9432;</button>'
        + '</div>';
    }
    html += '</div>';
  }
  content.innerHTML = html;
}

function openTab(session, windowIndex, windowName) {
  closeMobileSidebar();
  createTab(session, windowIndex, windowName);
}

// === Window details modal ===
function openWD(session, windowIndex) {
  closeMobileSidebar();
  _wdSession = session; _wdWindow = windowIndex;
  const overlay = document.getElementById('wd-overlay');
  overlay.classList.add('open');
  // Populate from dashboard data
  const data = _dashboardData;
  if (!data) return;
  const sess = data.sessions.find(s => s.name === session);
  if (!sess) return;
  const win = sess.windows.find(w => w.index === windowIndex);
  if (!win) return;
  document.getElementById('wd-title').textContent = session + ' : ' + win.name;
  let html = '';
  html += '<div class="wd-row"><span class="wd-label">Session</span><span class="wd-value">' + esc(session) + '</span></div>';
  html += '<div class="wd-row"><span class="wd-label">Window</span><span class="wd-value">' + esc(win.name) + '</span></div>';
  html += '<div class="wd-row"><span class="wd-label">CWD</span><span class="wd-value">' + esc(win.cwd) + '</span></div>';
  html += '<div class="wd-row"><span class="wd-label">PID</span><span class="wd-value">' + esc(win.pid || '') + '</span></div>';
  html += '<div class="wd-row"><span class="wd-label">Command</span><span class="wd-value">' + esc(win.command) + '</span></div>';
  if (win.is_cc) html += '<div class="wd-row"><span class="wd-label">Status</span><span class="wd-value">' + statusLabel(win.cc_status) + '</span></div>';
  document.getElementById('wd-content').innerHTML = html;
  document.getElementById('wd-rename-input').value = win.name;
  setTimeout(() => document.getElementById('wd-rename-input').focus(), 100);
}
function closeWD() {
  document.getElementById('wd-overlay').classList.remove('open');
  _wdSession = null; _wdWindow = null;
}
function saveWDRename() {
  if (!_wdSession || _wdWindow === null) return;
  const name = document.getElementById('wd-rename-input').value.trim();
  if (!name) return;
  fetch('/api/windows/' + _wdWindow, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name})
  }).then(() => {
    // Update tab name if open
    for (const tid in allTabs) {
      const t = allTabs[tid];
      if (t.session === _wdSession && t.windowIndex === _wdWindow) {
        t.windowName = name;
        // Re-render pane tabs
        for (const p of panes) {
          if (p.tabIds.includes(parseInt(tid))) renderPaneTabs(p.id);
        }
      }
    }
    closeWD();
    loadDashboard();
  });
}
function closeWDWindow() {
  if (!_wdSession || _wdWindow === null) return;
  if (!confirm('Close this window?')) return;
  fetch('/api/windows/' + _wdWindow, {method:'DELETE'}).then(() => {
    // Close tab if open
    for (const tid in allTabs) {
      const t = allTabs[tid];
      if (t.session === _wdSession && t.windowIndex === _wdWindow) {
        closeTab(parseInt(tid));
        break;
      }
    }
    closeWD();
    loadDashboard();
  });
}
document.getElementById('wd-rename-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); saveWDRename(); }
  if (e.key === 'Escape') { e.preventDefault(); closeWD(); }
});

// === Dashboard ===
async function loadDashboard() {
  try {
    const r = await fetch('/api/dashboard');
    _dashboardData = await r.json();
    renderSidebar();
    // Update tab names
    for (const tid in allTabs) {
      const tab = allTabs[tid];
      const sess = _dashboardData.sessions.find(s => s.name === tab.session);
      if (sess) {
        const win = sess.windows.find(w => w.index === tab.windowIndex);
        if (win && win.name !== tab.windowName) {
          tab.windowName = win.name;
          for (const p of panes) {
            if (p.tabIds.includes(parseInt(tid))) renderPaneTabs(p.id);
          }
        }
      }
    }
  } catch(e) {}
}

async function newWin() {
  await fetch('/api/windows/new', {method:'POST'});
  await loadDashboard();
  if (_dashboardData) {
    let sessName = null;
    for (const tid in allTabs) { sessName = allTabs[tid].session; break; }
    if (!sessName && _dashboardData.sessions.length > 0) sessName = _dashboardData.sessions[0].name;
    const sess = _dashboardData.sessions.find(s => s.name === sessName);
    if (sess && sess.windows.length > 0) {
      const w = sess.windows[sess.windows.length - 1];
      createTab(sessName, w.index, w.name);
    }
  }
}

// === Input ===
function autoResize() {
  M.style.height = 'auto';
  M.style.height = Math.min(M.scrollHeight, 120) + 'px';
  M.style.overflowY = M.scrollHeight > 120 ? 'auto' : 'hidden';
}
M.addEventListener('input', autoResize);
M.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendGlobal(); }
});

// === iOS keyboard ===
if (window.visualViewport) {
  const adjust = () => { bar.style.bottom = (window.innerHeight - window.visualViewport.height) + 'px'; };
  window.visualViewport.addEventListener('resize', adjust);
  window.visualViewport.addEventListener('scroll', adjust);
}

// === Keyboard shortcuts ===
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if ((e.metaKey || e.ctrlKey) && e.key >= '1' && e.key <= '9') {
    e.preventDefault();
    // Switch between panes
    const idx = parseInt(e.key) - 1;
    if (idx < panes.length) focusPane(panes[idx].id);
  }
  if ((e.metaKey || e.ctrlKey) && e.key === '\\\\') {
    e.preventDefault(); toggleSidebar();
  }
});

// === Init ===
async function init() {
  await loadDashboard();
  createPane();
  if (_dashboardData && _dashboardData.sessions.length > 0) {
    const sess = _dashboardData.sessions[0];
    const activeWin = sess.windows.find(w => w.active) || sess.windows[0];
    if (activeWin) createTab(sess.name, activeWin.index, activeWin.name);
  }
  setInterval(loadDashboard, 3000);
}
init();
</script>
</body>
</html>"""



@app.get("/")
async def index():
    ensure_session()
    html = HTML.replace("__TITLE__", TITLE)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/output")
async def api_output(session: str = None, window: int = None):
    return JSONResponse({"output": get_output(session, window)})


@app.post("/api/send")
async def api_send(body: dict):
    cmd = body.get("cmd", "")
    session = body.get("session", None)
    window = body.get("window", None)
    if cmd:
        send_keys(cmd, session, window)
    return JSONResponse({"ok": True})


@app.get("/api/key/{key}")
async def api_key(key: str, session: str = None, window: int = None):
    ALLOWED = {"C-c", "C-d", "C-l", "C-z", "Up", "Down", "Tab", "Enter", "Escape"}
    if key in ALLOWED:
        send_special(key, session, window)
    return JSONResponse({"ok": True})


@app.get("/api/dashboard")
async def api_dashboard():
    return JSONResponse(get_dashboard())


@app.get("/api/windows")
async def api_windows():
    return JSONResponse({"windows": list_windows()})


@app.post("/api/windows/new")
async def api_new_window():
    new_window()
    return JSONResponse({"ok": True})


@app.post("/api/windows/{index}")
async def api_select_window(index: int):
    select_window(index)
    return JSONResponse({"ok": True})


@app.put("/api/windows/current")
async def api_rename_current_window(body: dict):
    name = body.get("name", "").strip()
    if name:
        target = _current_session
        subprocess.run(["tmux", "rename-window", "-t", target, name])
        subprocess.run(["tmux", "set-window-option", "-t", target, "allow-rename", "off"])
        subprocess.run(["tmux", "set-window-option", "-t", target, "automatic-rename", "off"])
    return JSONResponse({"ok": True})


@app.post("/api/windows/current/reset-name")
async def api_reset_window_name():
    target = _current_session
    subprocess.run(["tmux", "set-window-option", "-t", target, "automatic-rename", "on"])
    subprocess.run(["tmux", "set-window-option", "-t", target, "allow-rename", "on"])
    return JSONResponse({"ok": True})


@app.put("/api/windows/{index}")
async def api_rename_window(index: int, body: dict):
    name = body.get("name", "").strip()
    if name:
        target = f"{_current_session}:{index}"
        subprocess.run(["tmux", "rename-window", "-t", target, name])
        subprocess.run(["tmux", "set-window-option", "-t", target, "allow-rename", "off"])
        subprocess.run(["tmux", "set-window-option", "-t", target, "automatic-rename", "off"])
    return JSONResponse({"ok": True})


@app.delete("/api/windows/{index}")
async def api_close_window(index: int):
    subprocess.run(["tmux", "kill-window", "-t", f"{_current_session}:{index}"])
    return JSONResponse({"ok": True})


@app.get("/api/sessions")
async def api_sessions():
    return JSONResponse({
        "current": _current_session,
        "sessions": list_sessions(),
    })


@app.get("/api/pane-info")
async def api_pane_info():
    r = subprocess.run(
        ["tmux", "display-message", "-t", _current_session, "-p",
         "#{pane_current_path}\n#{pane_pid}\n#{window_name}\n#{session_name}"],
        capture_output=True, text=True,
    )
    parts = r.stdout.strip().split("\n")
    return JSONResponse({
        "cwd": parts[0] if len(parts) > 0 else "",
        "pid": parts[1] if len(parts) > 1 else "",
        "window": parts[2] if len(parts) > 2 else "",
        "session": parts[3] if len(parts) > 3 else "",
    })


@app.post("/api/sessions/{name}")
async def api_switch_session(name: str):
    global _current_session
    # Verify session exists
    r = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
    if r.returncode != 0:
        return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
    _current_session = name
    return JSONResponse({"ok": True})


@app.put("/api/sessions/{name}")
async def api_rename_session(name: str, body: dict):
    new_name = body.get("name", "").strip()
    if not new_name:
        return JSONResponse({"ok": False, "error": "Name required"}, status_code=400)
    r = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
    if r.returncode != 0:
        return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
    subprocess.run(["tmux", "rename-session", "-t", name, new_name])
    global _current_session
    if _current_session == name:
        _current_session = new_name
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    if not shutil.which("tmux"):
        print("Error: tmux is not installed. Install it first:")
        print("  macOS:  brew install tmux")
        print("  Ubuntu: sudo apt install tmux")
        sys.exit(1)
    uvicorn.run(app, host=HOST, port=PORT)
