#!/usr/bin/env python3
"""Mobile web terminal for remote tmux control."""
import os
import re
import shutil
import subprocess
import sys
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


def detect_cc_status(text: str) -> tuple:
    """Detect if text is Claude Code output and its status.
    Returns (is_cc, status) where status is 'idle', 'working', or 'thinking'."""
    is_cc = '\u276f' in text and '\u23fa' in text
    if not is_cc:
        return False, None
    tail = '\n'.join(text.split('\n')[-10:])
    # Thinking: · at START of line (not mid-line like "· 7 files" in status bar)
    if re.search(r'^\u00b7\s+\w', tail, re.MULTILINE):
        return True, 'thinking'
    # Working: "esc to interrupt" on its OWN line or start of line (not inside permissions bar)
    for line in tail.split('\n'):
        stripped = line.strip()
        if 'esc to interrupt' in stripped and 'permissions' not in stripped and 'shift+tab' not in stripped:
            return True, 'working'
    return True, 'idle'


def get_dashboard() -> dict:
    """Get lightweight status for all sessions and windows."""
    # Single call to get all pane metadata
    r = subprocess.run(
        ["tmux", "list-panes", "-a", "-F",
         "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_current_path}\t#{pane_current_command}\t#{window_active}\t#{session_attached}"],
        capture_output=True, text=True,
    )
    sessions = {}
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        sname, widx, wname, cwd, cmd, wactive, sattached = parts
        if sname not in sessions:
            sessions[sname] = {
                "name": sname,
                "attached": sattached == "1",
                "windows": [],
            }
        # Get preview for CC detection
        preview = get_pane_preview(sname, int(widx), lines=10)
        is_cc, cc_status = detect_cc_status(preview)
        # Trim preview to last 5 lines for response
        preview_short = '\n'.join(preview.split('\n')[-5:])
        sessions[sname]["windows"].append({
            "index": int(widx),
            "name": wname,
            "active": wactive == "1",
            "cwd": cwd,
            "command": cmd,
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
.sb-win { display:flex; align-items:center; gap:8px; padding:7px 10px;
  border-radius:8px; cursor:pointer; transition:all .12s;
  -webkit-user-select:none; user-select:none; }
.sb-win:hover { background:var(--surface); }
.sb-win.has-tab { background:rgba(217,119,87,0.08); }
.sb-win.has-tab:hover { background:rgba(217,119,87,0.15); }
.sb-win-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
.sb-win-dot.idle { background:var(--green); }
.sb-win-dot.working { background:var(--orange); animation:pulse 1.5s ease-in-out infinite; }
.sb-win-dot.thinking { background:var(--orange); animation:pulse 1s ease-in-out infinite; }
.sb-win-dot.none { background:var(--text3); opacity:0.3; }
.sb-win-info { flex:1; min-width:0; }
.sb-win-name { font-size:13px; font-weight:500; color:var(--text);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sb-win-cwd { font-size:11px; color:var(--text3);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

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
  #split-btn { display:none !important; }
}

/* --- Main area --- */
#main { flex:1; display:flex; flex-direction:column; min-width:0;
  position:relative; }

/* --- Top bar --- */
#topbar { background:var(--bg); padding:calc(var(--safe-top) + 6px) 12px 0;
  display:flex; flex-direction:column; gap:0; flex-shrink:0; z-index:10; }
#topbar-row { display:flex; align-items:center; gap:8px; padding-bottom:8px; }
#hamburger { display:none; background:none; border:none; color:var(--text2);
  font-size:20px; cursor:pointer; padding:4px 8px; border-radius:8px;
  transition:all .15s; flex-shrink:0; }
#hamburger:active { transform:scale(0.92); }
@media (max-width:768px) { #hamburger { display:block; } }
.topbar-btn { height:32px; padding:0 12px; border-radius:16px;
  background:var(--surface); color:var(--text2); border:1px solid var(--border);
  font-size:12px; font-weight:500; font-family:inherit; cursor:pointer;
  transition:all .15s; -webkit-user-select:none; user-select:none;
  flex-shrink:0; white-space:nowrap; }
.topbar-btn:active { transform:scale(0.96); opacity:0.8; }

/* --- Tab bar --- */
#tab-bar { display:flex; align-items:center; gap:6px; flex:1; min-width:0;
  overflow-x:auto; -webkit-overflow-scrolling:touch;
  scrollbar-width:none; -ms-overflow-style:none; }
#tab-bar::-webkit-scrollbar { display:none; }
.tab { display:flex; align-items:center; gap:5px; padding:0 10px; height:30px;
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px 8px 0 0; font-size:12px; font-weight:500;
  color:var(--text2); cursor:pointer; transition:all .12s;
  white-space:nowrap; flex-shrink:0; max-width:180px;
  -webkit-user-select:none; user-select:none; position:relative; }
.tab:hover { color:var(--text); }
.tab.active { background:var(--bg); color:var(--text); border-bottom-color:var(--bg); }
.tab-name { overflow:hidden; text-overflow:ellipsis; }
.tab-close { display:flex; align-items:center; justify-content:center;
  width:16px; height:16px; border-radius:4px; font-size:14px; line-height:1;
  color:var(--text3); cursor:pointer; transition:all .1s; }
.tab-close:hover { background:rgba(255,255,255,0.1); color:var(--text); }
#split-btn { flex-shrink:0; }
#split-btn.on { background:var(--accent); color:#fff; border-color:var(--accent); }

/* --- Panes container --- */
#panes-container { flex:1; display:flex; overflow:hidden; position:relative; }
.tab-pane { flex:1; overflow-y:auto; -webkit-overflow-scrolling:touch;
  display:none; flex-direction:column; min-width:0; }
.tab-pane.visible { display:flex; }

/* Split pane headers */
.pane-header { display:none; padding:4px 10px; background:var(--bg2);
  border-bottom:1px solid var(--border); font-size:11px; font-weight:600;
  color:var(--text3); text-transform:uppercase; letter-spacing:0.3px;
  cursor:pointer; flex-shrink:0; }
.split-mode .pane-header { display:flex; align-items:center; justify-content:space-between; }
.tab-pane.focused .pane-header { border-top:2px solid var(--accent); color:var(--accent); }

/* Split layout */
.split-mode .tab-pane.visible { border-right:1px solid var(--border2); }
.split-mode .tab-pane.visible:last-child { border-right:none; }

/* Pane output area */
.pane-output { flex:1; overflow-y:auto; -webkit-overflow-scrolling:touch; }

/* Raw mode */
.pane-output.raw { padding:20px 16px;
  font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace;
  font-size:var(--mono-size); line-height:1.6; white-space:pre-wrap;
  word-break:break-word; color:#999; }

/* Chat mode */
.pane-output.chat { display:flex; flex-direction:column; padding:12px 16px 24px; }

/* --- Turn wrapper --- */
.turn { margin:0 0 6px; }
.turn + .turn { margin-top:12px; }
.turn.user + .turn.assistant,
.turn.assistant + .turn.user { margin-top:20px; }

/* Role label */
.turn-label { font-size:11px; font-weight:600; color:var(--text3);
  text-transform:uppercase; letter-spacing:0.5px; margin-bottom:5px;
  padding:0 4px; }
.turn.user .turn-label { text-align:right; padding-right:6px; }
.turn.assistant .turn-label { padding-left:2px; color:var(--accent); }

/* --- User bubble --- */
.turn.user .turn-body { background:var(--accent); color:#fff;
  padding:11px 16px; border-radius:18px 18px 4px 18px;
  max-width:85%; margin-left:auto; font-size:var(--text-size);
  line-height:1.55; word-break:break-word; }

/* --- Assistant card --- */
.turn.assistant .turn-body { background:var(--surface);
  padding:16px 18px; border-radius:4px 18px 18px 18px;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;
  font-size:var(--text-size); line-height:1.7; word-break:break-word; color:var(--text); }

/* --- Typography inside assistant cards --- */
.turn-body p { margin:0.5em 0; }
.turn-body p:first-child { margin-top:0; }
.turn-body p:last-child { margin-bottom:0; }
.turn-body strong { color:#fff; font-weight:600; }
.turn-body em { color:var(--text2); }
.turn-body h1 { font-size:1.35em; font-weight:700; margin:0.9em 0 0.4em;
  letter-spacing:-0.3px; }
.turn-body h2 { font-size:1.15em; font-weight:600; margin:0.7em 0 0.3em; }
.turn-body h3 { font-size:1em; font-weight:600; margin:0.6em 0 0.25em;
  color:var(--text2); }
.turn-body h1:first-child, .turn-body h2:first-child,
.turn-body h3:first-child { margin-top:0; }
.turn-body pre { background:var(--bg); border:1px solid rgba(255,255,255,0.06);
  border-radius:10px; padding:13px; margin:10px 0; overflow-x:auto;
  white-space:pre-wrap; word-break:break-word;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;
  font-size:var(--code-size); line-height:1.55; color:#b0b0b0; }
.turn-body code { background:rgba(255,255,255,0.07); padding:2px 6px;
  border-radius:5px; font-family:'SF Mono',ui-monospace,Menlo,monospace;
  font-size:0.84em; color:#ccc; }
.turn-body pre code { background:none; padding:0; font-size:inherit; color:inherit; }
.turn-body ul, .turn-body ol { padding-left:1.3em; margin:0.4em 0; }
.turn-body li { margin:0.3em 0; }
.turn-body li::marker { color:var(--text3); }
.turn-body blockquote { border-left:3px solid var(--border2); margin:0.5em 0;
  padding:4px 14px; color:var(--text2); }
.turn-body a { color:var(--accent); text-decoration:none; }
.turn-body.mono { font-family:'SF Mono',ui-monospace,Menlo,monospace;
  font-size:var(--mono-size); line-height:1.6; white-space:pre-wrap; word-break:break-word;
  color:#999; }
.turn-body .thinking { color:var(--text3); font-style:italic; animation:pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%,100%{ opacity:.4; } 50%{ opacity:1; } }
.turn-body hr { border:none; height:1px; background:var(--border2); margin:1.2em 0; }
.turn-body table { border-collapse:collapse; width:100%; margin:0.5em 0; font-size:14px; }
.turn-body th, .turn-body td { padding:6px 10px; text-align:left;
  border-bottom:1px solid var(--border); }
.turn-body th { color:var(--text2); font-weight:600; }
.turn-body details { background:var(--bg); border:1px solid var(--border);
  border-radius:10px; margin:8px 0; padding:0; overflow:hidden; }
.turn-body details summary { padding:10px 14px; cursor:pointer;
  color:var(--text2); font-size:13px; font-weight:500;
  font-family:'SF Mono',ui-monospace,Menlo,monospace;
  list-style:none; display:flex; align-items:center; gap:6px; }
.turn-body details summary::before { content:'\\25B6'; font-size:8px;
  color:var(--text3); transition:transform .15s; }
.turn-body details[open] summary::before { transform:rotate(90deg); }
.turn-body details summary::-webkit-details-marker { display:none; }

/* --- Bottom bar --- */
#bar { background:var(--bg2); flex-shrink:0;
  padding:12px 14px calc(var(--safe-bottom) + 12px);
  transition:bottom .1s; }
#input-row { display:flex; gap:10px; align-items:flex-end; }
#msg { flex:1; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:22px; padding:11px 18px;
  font-size:16px; font-family:inherit; outline:none;
  transition:border-color .2s, box-shadow .2s;
  resize:none; overflow-y:hidden; max-height:120px;
  line-height:1.4; }
#msg::placeholder { color:var(--text3); }
#msg:focus { border-color:rgba(217,119,87,0.5);
  box-shadow:0 0 0 3px rgba(217,119,87,0.1); }
#send-btn { flex-shrink:0; width:42px; height:42px; border-radius:50%;
  background:var(--accent); border:none; color:#fff; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  transition:transform .1s, background .15s; }
#send-btn:active { transform:scale(0.92); background:var(--accent2); }
#send-btn svg { width:20px; height:20px; }

/* Toolbar */
#toolbar { display:flex; gap:6px; margin-top:10px; }
.pill { padding:8px 16px; font-size:13px; font-weight:500;
  background:var(--surface); color:var(--text2); border:none;
  border-radius:100px; cursor:pointer; transition:all .15s;
  -webkit-user-select:none; user-select:none; }
.pill:active { transform:scale(0.96); opacity:0.8; }
.pill.on { background:var(--accent); color:#fff; }
.pill.danger { color:var(--red); }

/* Keys tray */
#keys, #cmds { max-height:0; overflow:hidden; transition:max-height .25s ease, margin .25s ease;
  display:flex; flex-wrap:wrap; gap:6px; margin-top:0; }
#keys.open, #cmds.open { max-height:100px; margin-top:10px; }

/* Rename modal */
#rename-overlay { display:none; position:fixed; inset:0; z-index:100;
  background:rgba(0,0,0,0.6); align-items:center; justify-content:center; }
#rename-overlay.open { display:flex; }
#rename-modal { background:var(--bg2); border:1px solid var(--border2);
  border-radius:16px; padding:20px; width:min(320px, 85vw); }
#rename-modal h3 { font-size:15px; font-weight:600; color:var(--text);
  margin-bottom:14px; }
#rename-input { width:100%; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:10px; padding:10px 14px;
  font-size:15px; font-family:inherit; outline:none; margin-bottom:14px; }
#rename-input:focus { border-color:rgba(217,119,87,0.5); }
#rename-modal .modal-btns { display:flex; gap:8px; }
#rename-modal .modal-btns button { flex:1; padding:10px; border:none;
  border-radius:10px; font-size:14px; font-weight:500; font-family:inherit;
  cursor:pointer; transition:opacity .15s; }
#rename-modal .modal-btns button:active { opacity:0.7; }
.btn-cancel { background:var(--surface); color:var(--text2); }
.btn-save { background:var(--accent); color:#fff; }
.btn-reset { background:transparent; color:var(--text3); font-size:13px !important;
  margin-top:8px; padding:8px !important; border:none; cursor:pointer;
  width:100%; text-align:center; font-family:inherit; transition:color .15s; }
.btn-reset:active { color:var(--text); }

/* Details popup */
#details-overlay { display:none; position:fixed; inset:0; z-index:100;
  background:rgba(0,0,0,0.6); align-items:center; justify-content:center; }
#details-overlay.open { display:flex; }
#details-modal { background:var(--bg2); border:1px solid var(--border2);
  border-radius:16px; padding:20px; width:min(340px, 85vw); }
#details-modal h3 { font-size:15px; font-weight:600; color:var(--text);
  margin-bottom:14px; }
.detail-row { display:flex; gap:10px; padding:10px 0;
  border-bottom:1px solid var(--border); }
.detail-row:last-child { border-bottom:none; }
.detail-label { color:var(--text3); font-size:12px; font-weight:600;
  text-transform:uppercase; letter-spacing:0.5px; min-width:70px; padding-top:1px; }
.detail-value { color:var(--text); font-size:14px; word-break:break-all;
  font-family:'SF Mono',ui-monospace,Menlo,monospace; }
#details-modal .btn-cancel { width:100%; margin-top:14px; padding:10px;
  border:none; border-radius:10px; font-size:14px; font-weight:500;
  font-family:inherit; cursor:pointer; }
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
      <div id="tab-bar"></div>
      <button class="topbar-btn" id="split-btn" onclick="toggleSplit()">Split</button>
      <button class="topbar-btn" id="details-btn" onclick="openDetails()">Details</button>
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
      <button id="send-btn" onclick="send()" aria-label="Send">
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
      <button class="pill" onclick="key('Enter')">Return</button>
      <button class="pill danger" onclick="key('C-c')">Ctrl-C</button>
      <button class="pill" onclick="key('Up')">Up</button>
      <button class="pill" onclick="key('Down')">Down</button>
      <button class="pill" onclick="key('Tab')">Tab</button>
      <button class="pill" onclick="key('Escape')">Esc</button>
    </div>
    <div id="cmds">
      <button class="pill" onclick="prefill('/_my_wrap_up')">Wrap Up</button>
      <button class="pill" onclick="prefill('/clear')">Clear</button>
      <button class="pill" onclick="prefill('/exit')">Exit</button>
      <button class="pill" onclick="sendResume()">Resume</button>
      <button class="pill" onclick="renameCurrentWin()">Rename</button>
    </div>
  </div>
</main>
</div>

<div id="details-overlay" onclick="if(event.target===this)closeDetails()">
  <div id="details-modal">
    <h3>Details</h3>
    <div id="details-content"><p style="color:var(--text3)">Loading...</p></div>
    <button class="btn-cancel" onclick="closeDetails()">Close</button>
  </div>
</div>

<div id="rename-overlay" onclick="if(event.target===this)closeRename()">
  <div id="rename-modal">
    <h3>Rename Window</h3>
    <input id="rename-input" type="text" autocorrect="off" autocapitalize="none" spellcheck="false">
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeRename()">Cancel</button>
      <button class="btn-save" onclick="confirmRename()">Save</button>
    </div>
    <button class="btn-reset" onclick="resetWindowName()">Reset to Original Name</button>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/lib/marked.umd.min.js"></script>
<script>
const M = document.getElementById('msg');
const bar = document.getElementById('bar');
const panesContainer = document.getElementById('panes-container');

// === Tab data model ===
let openTabs = [];        // [{ id, session, windowIndex, windowName }]
let activeTabId = null;
let tabStates = {};       // tabId -> { rawContent, last, rawMode, pendingMsg, pendingTime, awaitingResponse, lastOutputChange, pollInterval }
let splitMode = false;
let _nextTabId = 1;
let _dashboardData = null;

// === Sidebar state ===
let _sidebarCollapsed = false;

// === Utility ===
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s; return d.innerHTML;
}
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
function tabKey(session, windowIndex) {
  return session + ':' + windowIndex;
}

// === Clean/parse (unchanged logic) ===
function cleanTerminal(raw) {
  let lines = raw.split('\\n');
  lines = lines.filter(l => !/^\\s*[\\u256d\\u2570][\\u2500\\u2504\\u2501]+[\\u256e\\u256f]\\s*$/.test(l));
  lines = lines.map(l => l.replace(/^\\s*\\u2502\\s?/, '').replace(/\\s?\\u2502\\s*$/, ''));
  let text = lines.join('\\n');
  text = text.replace(/[\\u280b\\u2819\\u2839\\u2838\\u283c\\u2834\\u2826\\u2827\\u2807\\u280f]/g, '');
  text = text.replace(/\\n{3,}/g, '\\n\\n');
  return text.trim();
}
function isClaudeCode(text) {
  return /\\u276f/.test(text) && /\\u23fa/.test(text);
}
function isIdle(text) {
  const tail = text.split('\\n').slice(-10).join('\\n');
  if (/esc to interrupt/.test(tail)) return false;
  if (/^\\u00b7\\s+\\w/m.test(tail)) return false;
  return true;
}
function parseCCTurns(text) {
  const lines = text.split('\\n');
  const turns = [];
  let cur = null;
  let inTool = false;
  let sawStatus = false;
  for (const line of lines) {
    const raw = line.replace(/\\u00a0/g, ' ');
    const t = raw.trim();
    if (/^[\\u2500\\u2501\\u2504\\u2508\\u2550]{3,}$/.test(t) && t.length > 60) continue;
    if (/^[\\u23f5]/.test(t)) continue;
    if (/^\\u2026/.test(t)) continue;
    if (!t) {
      if (cur && cur.role === 'assistant' && !inTool) cur.lines.push('');
      continue;
    }
    if (/^[\\u2730-\\u273f]/.test(t)) { sawStatus = true; continue; }
    if (/^\\u00b7\\s+\\w/.test(t)) { sawStatus = true; continue; }
    if (/esc to interrupt/.test(t)) continue;
    if (/^\\u276f/.test(raw)) {
      if (cur) turns.push(cur);
      const msg = t.replace(/^\\u276f\\s*/, '').trim();
      cur = { role: 'user', lines: msg ? [msg] : [] };
      inTool = false; sawStatus = false; continue;
    }
    if (/^\\u23fa/.test(t)) {
      const after = t.replace(/^\\u23fa\\s*/, '');
      if (/^(Bash|Read|Write|Update|Edit|Fetch|Search|Glob|Grep|Task|Skill|NotebookEdit|Searched for|Wrote \\d)/.test(after)) {
        inTool = true;
        if (!cur || cur.role !== 'assistant') {
          if (cur) turns.push(cur);
          cur = { role: 'assistant', lines: [] };
        }
        continue;
      }
      inTool = false;
      if (!cur || cur.role !== 'assistant') {
        if (cur) turns.push(cur);
        cur = { role: 'assistant', lines: [] };
      }
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

// === Render output into a target element ===
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
      if (lastUser && lastUser.lines.join(' ').includes(state.pendingMsg.substring(0, 20))) {
        state.pendingMsg = null;
      }
    }
    if (state.awaitingResponse) {
      const elapsed = Date.now() - state.pendingTime;
      if (elapsed > 3000 && isIdle(clean)) state.awaitingResponse = false;
      if (elapsed > 3000 && state.lastOutputChange > 0 && (Date.now() - state.lastOutputChange) > 5000) state.awaitingResponse = false;
      if (elapsed > 180000) state.awaitingResponse = false;
    }
    for (const t of turns) {
      const text = t.lines.join('\\n').trim();
      if (!text) continue;
      if (t.role === 'user') {
        html += '<div class="turn user"><div class="turn-label">You</div>'
          + '<div class="turn-body">' + esc(text) + '</div></div>';
      } else {
        html += '<div class="turn assistant"><div class="turn-label">Claude</div>'
          + '<div class="turn-body">' + md(text) + '</div></div>';
      }
    }
    if (state.pendingMsg) {
      html += '<div class="turn user"><div class="turn-label">You</div>'
        + '<div class="turn-body">' + esc(state.pendingMsg) + '</div></div>';
    }
    if (state.awaitingResponse) {
      html += '<div class="turn assistant"><div class="turn-label">Claude</div>'
        + '<div class="turn-body"><p class="thinking">Working\\u2026</p></div></div>';
    }
  } else {
    if (clean.trim()) {
      html = '<div class="turn assistant"><div class="turn-label">Terminal</div>'
        + '<div class="turn-body mono">' + esc(clean) + '</div></div>';
    }
  }
  if (!html) {
    html = '<div class="turn assistant"><div class="turn-label">Terminal</div>'
      + '<div class="turn-body"><p style="color:var(--text3)">Waiting for output...</p></div></div>';
  }
  targetEl.innerHTML = html;
}

// === Tab management ===
function createTab(session, windowIndex, windowName) {
  // Check if tab already exists
  const existing = openTabs.find(t => t.session === session && t.windowIndex === windowIndex);
  if (existing) { focusTab(existing.id); return; }

  const id = _nextTabId++;
  openTabs.push({ id, session, windowIndex, windowName });
  tabStates[id] = {
    rawContent: '', last: '', rawMode: false,
    pendingMsg: null, pendingTime: 0,
    awaitingResponse: false, lastOutputChange: 0,
    pollInterval: null,
  };

  // Create DOM pane
  const pane = document.createElement('div');
  pane.className = 'tab-pane';
  pane.id = 'pane-' + id;
  pane.innerHTML = '<div class="pane-header" onclick="focusTab(' + id + ')">'
    + '<span>' + esc(windowName) + '</span></div>'
    + '<div class="pane-output chat"><div class="turn assistant"><div class="turn-label">Terminal</div>'
    + '<div class="turn-body"><p style="color:var(--text3)">Connecting...</p></div></div></div>';
  panesContainer.appendChild(pane);

  focusTab(id);
  renderTabBar();
  renderSidebar();
  startTabPolling(id);
}

function closeTab(id) {
  stopTabPolling(id);
  const idx = openTabs.findIndex(t => t.id === id);
  if (idx < 0) return;
  openTabs.splice(idx, 1);
  delete tabStates[id];
  const pane = document.getElementById('pane-' + id);
  if (pane) pane.remove();

  if (activeTabId === id) {
    if (openTabs.length > 0) {
      const next = openTabs[Math.min(idx, openTabs.length - 1)];
      focusTab(next.id);
    } else {
      activeTabId = null;
    }
  }
  renderTabBar();
  renderSidebar();
  updatePaneVisibility();
}

function focusTab(id) {
  activeTabId = id;
  // Update focused class on panes
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('focused'));
  const pane = document.getElementById('pane-' + id);
  if (pane) pane.classList.add('focused');
  renderTabBar();
  updatePaneVisibility();
  // Start/stop polling based on visibility
  updatePolling();
}

function renderTabBar() {
  const tabBar = document.getElementById('tab-bar');
  let html = '';
  for (const tab of openTabs) {
    const active = tab.id === activeTabId;
    html += '<div class="tab' + (active ? ' active' : '') + '" onclick="focusTab(' + tab.id + ')">'
      + '<span class="tab-name">' + esc(tab.windowName) + '</span>'
      + '<span class="tab-close" onclick="event.stopPropagation();closeTab(' + tab.id + ')">&times;</span>'
      + '</div>';
  }
  tabBar.innerHTML = html;
}

function updatePaneVisibility() {
  const panes = document.querySelectorAll('.tab-pane');
  if (splitMode) {
    // Show all open tabs (up to 3)
    panes.forEach(p => {
      const id = parseInt(p.id.replace('pane-', ''));
      const isOpen = openTabs.some(t => t.id === id);
      p.classList.toggle('visible', isOpen);
    });
    panesContainer.classList.add('split-mode');
  } else {
    // Single tab view: show only active
    panes.forEach(p => {
      const id = parseInt(p.id.replace('pane-', ''));
      p.classList.toggle('visible', id === activeTabId);
    });
    panesContainer.classList.remove('split-mode');
  }
}

// === Polling per tab ===
function startTabPolling(id) {
  const state = tabStates[id];
  if (!state || state.pollInterval) return;
  const tab = openTabs.find(t => t.id === id);
  if (!tab) return;

  // Immediate first fetch
  pollTab(id);

  state.pollInterval = setInterval(() => pollTab(id), 1000);
}

function stopTabPolling(id) {
  const state = tabStates[id];
  if (!state) return;
  if (state.pollInterval) {
    clearInterval(state.pollInterval);
    state.pollInterval = null;
  }
}

async function pollTab(id) {
  const tab = openTabs.find(t => t.id === id);
  const state = tabStates[id];
  if (!tab || !state) return;
  try {
    const r = await fetch('/api/output?session=' + encodeURIComponent(tab.session) + '&window=' + tab.windowIndex);
    const d = await r.json();
    if (d.output !== state.last) {
      state.lastOutputChange = Date.now();
      state.last = d.output;
      state.rawContent = d.output;
      const pane = document.getElementById('pane-' + id);
      if (!pane) return;
      const outputEl = pane.querySelector('.pane-output');
      if (!outputEl) return;
      const atBottom = outputEl.scrollHeight - outputEl.scrollTop - outputEl.clientHeight < 80;
      renderOutput(d.output, outputEl, state);
      if (atBottom) outputEl.scrollTop = outputEl.scrollHeight;
    }
  } catch(e) {}
}

function updatePolling() {
  for (const tab of openTabs) {
    const isVisible = splitMode || tab.id === activeTabId;
    if (isVisible) {
      startTabPolling(tab.id);
    } else {
      stopTabPolling(tab.id);
    }
  }
}

// === Split view ===
function toggleSplit() {
  splitMode = !splitMode;
  document.getElementById('split-btn').classList.toggle('on', splitMode);
  updatePaneVisibility();
  updatePolling();
}

// === View toggle (per active tab) ===
function toggleRaw() {
  if (!activeTabId) return;
  const state = tabStates[activeTabId];
  if (!state) return;
  state.rawMode = !state.rawMode;
  document.getElementById('view-label').textContent = state.rawMode ? 'Raw' : 'Clean';
  const pane = document.getElementById('pane-' + activeTabId);
  if (!pane) return;
  const outputEl = pane.querySelector('.pane-output');
  if (!outputEl) return;
  renderOutput(state.rawContent || state.last, outputEl, state);
  outputEl.scrollTop = outputEl.scrollHeight;
}

// === Text size ===
const TEXT_SIZES = [
  { label: 'A-', text: '13px', code: '11px', mono: '11px' },
  { label: 'A',  text: '15px', code: '12.5px', mono: '12.5px' },
  { label: 'A+', text: '17px', code: '14px', mono: '14px' },
];
let _textSizeIdx = 1; // default medium
function cycleTextSize() {
  _textSizeIdx = (_textSizeIdx + 1) % TEXT_SIZES.length;
  const s = TEXT_SIZES[_textSizeIdx];
  document.documentElement.style.setProperty('--text-size', s.text);
  document.documentElement.style.setProperty('--code-size', s.code);
  document.documentElement.style.setProperty('--mono-size', s.mono);
  document.getElementById('size-btn').textContent = s.label;
  try { localStorage.setItem('textSize', _textSizeIdx); } catch(e) {}
}
// Restore saved size
try {
  const saved = localStorage.getItem('textSize');
  if (saved !== null) {
    _textSizeIdx = parseInt(saved);
    const s = TEXT_SIZES[_textSizeIdx];
    document.documentElement.style.setProperty('--text-size', s.text);
    document.documentElement.style.setProperty('--code-size', s.code);
    document.documentElement.style.setProperty('--mono-size', s.mono);
    document.getElementById('size-btn').textContent = s.label;
  }
} catch(e) {}

// === Send ===
async function send() {
  const t = M.value; if (!t) return;
  if (!activeTabId) return;
  const tab = openTabs.find(tb => tb.id === activeTabId);
  const state = tabStates[activeTabId];
  if (!tab || !state) return;

  M.value = '';
  M.style.height = 'auto';
  state.pendingMsg = t;
  state.pendingTime = Date.now();
  state.awaitingResponse = true;

  const pane = document.getElementById('pane-' + activeTabId);
  if (pane) {
    const outputEl = pane.querySelector('.pane-output');
    if (outputEl) {
      renderOutput(state.rawContent || state.last, outputEl, state);
      outputEl.scrollTop = outputEl.scrollHeight;
    }
  }

  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: t, session: tab.session, window: tab.windowIndex })
  });
}

async function key(k) {
  if (!activeTabId) return;
  const tab = openTabs.find(t => t.id === activeTabId);
  if (!tab) return;
  await fetch('/api/key/' + k + '?session=' + encodeURIComponent(tab.session) + '&window=' + tab.windowIndex);
}

// === Keys/Commands trays ===
function prefill(text) {
  M.value = M.value ? M.value + ' ' + text : text;
  M.focus();
}

async function sendResume() {
  if (!activeTabId) return;
  const state = tabStates[activeTabId];
  const tab = openTabs.find(t => t.id === activeTabId);
  if (!state || !tab) return;
  if (!state.rawMode) {
    state.rawMode = true;
    document.getElementById('view-label').textContent = 'Raw';
  }
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: '/resume', session: tab.session, window: tab.windowIndex })
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

// === Rename (operates on active tab's window) ===
let _currentWindows = [];
function renameCurrentWin() { openRename(); }
function openRename() {
  if (!activeTabId) return;
  const tab = openTabs.find(t => t.id === activeTabId);
  if (!tab) return;
  const input = document.getElementById('rename-input');
  input.value = tab.windowName;
  document.getElementById('rename-overlay').classList.add('open');
  setTimeout(() => { input.focus(); input.select(); }, 100);
}
function closeRename() {
  document.getElementById('rename-overlay').classList.remove('open');
}
function confirmRename() {
  if (!activeTabId) return;
  const tab = openTabs.find(t => t.id === activeTabId);
  if (!tab) return;
  const name = document.getElementById('rename-input').value.trim();
  if (!name) return;
  fetch('/api/windows/' + tab.windowIndex, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name})
  }).then(() => {
    tab.windowName = name;
    renderTabBar();
    closeRename();
    loadDashboard();
  });
}
function resetWindowName() {
  if (!activeTabId) return;
  const tab = openTabs.find(t => t.id === activeTabId);
  if (!tab) return;
  fetch('/api/windows/current/reset-name', {method:'POST'}).then(() => {
    closeRename();
    loadDashboard();
  });
}

document.getElementById('rename-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); confirmRename(); }
  if (e.key === 'Escape') { e.preventDefault(); closeRename(); }
});

// === Details popup ===
function openDetails() {
  document.getElementById('details-overlay').classList.add('open');
  document.getElementById('details-content').innerHTML = '<p style="color:var(--text3)">Loading...</p>';
  fetch('/api/pane-info').then(r => r.json()).then(d => {
    let html = '';
    html += '<div class="detail-row"><span class="detail-label">Directory</span>'
      + '<span class="detail-value">' + esc(d.cwd) + '</span></div>';
    html += '<div class="detail-row"><span class="detail-label">Session</span>'
      + '<span class="detail-value">' + esc(d.session) + '</span></div>';
    html += '<div class="detail-row"><span class="detail-label">Window</span>'
      + '<span class="detail-value">' + esc(d.window) + '</span></div>';
    html += '<div class="detail-row"><span class="detail-label">PID</span>'
      + '<span class="detail-value">' + esc(d.pid) + '</span></div>';
    document.getElementById('details-content').innerHTML = html;
  });
}
function closeDetails() {
  document.getElementById('details-overlay').classList.remove('open');
}

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
  let html = '';
  for (const s of data.sessions) {
    html += '<div class="sb-session">';
    html += '<div class="sb-session-header">'
      + esc(s.name)
      + (s.attached ? ' <span class="sb-badge">attached</span>' : '')
      + '</div>';
    for (const w of s.windows) {
      const key = tabKey(s.name, w.index);
      const hasTab = openTabs.some(t => t.session === s.name && t.windowIndex === w.index);
      const dotClass = w.is_cc ? (w.cc_status || 'idle') : 'none';
      html += '<div class="sb-win' + (hasTab ? ' has-tab' : '') + '"'
        + ' onclick="openTab(\\'' + esc(s.name).replace(/'/g, "\\\\'") + '\\',' + w.index + ',\\'' + esc(w.name).replace(/'/g, "\\\\'") + '\\')">'
        + '<div class="sb-win-dot ' + dotClass + '"></div>'
        + '<div class="sb-win-info">'
        + '<div class="sb-win-name">' + esc(w.name) + '</div>'
        + '<div class="sb-win-cwd">' + esc(abbreviateCwd(w.cwd)) + '</div>'
        + '</div></div>';
    }
    html += '</div>';
  }
  content.innerHTML = html;
}

function openTab(session, windowIndex, windowName) {
  closeMobileSidebar();
  createTab(session, windowIndex, windowName);
}

async function loadDashboard() {
  try {
    const r = await fetch('/api/dashboard');
    _dashboardData = await r.json();
    renderSidebar();
    // Update tab names from dashboard data
    for (const tab of openTabs) {
      const sess = _dashboardData.sessions.find(s => s.name === tab.session);
      if (sess) {
        const win = sess.windows.find(w => w.index === tab.windowIndex);
        if (win && win.name !== tab.windowName) {
          tab.windowName = win.name;
        }
      }
    }
    renderTabBar();
  } catch(e) {}
}

async function newWin() {
  await fetch('/api/windows/new', {method:'POST'});
  await loadDashboard();
  // Open a tab for the new window (highest index in current session)
  if (_dashboardData) {
    // Find session that matches the first open tab, or first session
    let sessName = openTabs.length > 0 ? openTabs[0].session : null;
    if (!sessName && _dashboardData.sessions.length > 0) sessName = _dashboardData.sessions[0].name;
    const sess = _dashboardData.sessions.find(s => s.name === sessName);
    if (sess && sess.windows.length > 0) {
      const w = sess.windows[sess.windows.length - 1];
      createTab(sessName, w.index, w.name);
    }
  }
}

// === Send a preset command ===
async function sendCmd(cmd) {
  if (!activeTabId) return;
  const tab = openTabs.find(t => t.id === activeTabId);
  const state = tabStates[activeTabId];
  if (!tab || !state) return;
  state.pendingMsg = cmd;
  state.pendingTime = Date.now();
  state.awaitingResponse = true;
  const pane = document.getElementById('pane-' + activeTabId);
  if (pane) {
    const outputEl = pane.querySelector('.pane-output');
    if (outputEl) {
      renderOutput(state.rawContent || state.last, outputEl, state);
      outputEl.scrollTop = outputEl.scrollHeight;
    }
  }
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: cmd, session: tab.session, window: tab.windowIndex })
  });
}

// === Input ===
function autoResize() {
  M.style.height = 'auto';
  M.style.height = Math.min(M.scrollHeight, 120) + 'px';
  M.style.overflowY = M.scrollHeight > 120 ? 'auto' : 'hidden';
}
M.addEventListener('input', autoResize);
M.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

// === iOS keyboard ===
if (window.visualViewport) {
  const adjust = () => {
    bar.style.bottom = (window.innerHeight - window.visualViewport.height) + 'px';
  };
  window.visualViewport.addEventListener('resize', adjust);
  window.visualViewport.addEventListener('scroll', adjust);
}

// === Keyboard shortcuts ===
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  // Cmd+1/2/3 to switch tabs
  if ((e.metaKey || e.ctrlKey) && e.key >= '1' && e.key <= '9') {
    const idx = parseInt(e.key) - 1;
    if (idx < openTabs.length) {
      e.preventDefault();
      focusTab(openTabs[idx].id);
    }
  }
  // Cmd+\\ to toggle sidebar
  if ((e.metaKey || e.ctrlKey) && e.key === '\\\\') {
    e.preventDefault();
    toggleSidebar();
  }
});

// === Init ===
async function init() {
  await loadDashboard();
  // Open first tab for the first session's active window
  if (_dashboardData && _dashboardData.sessions.length > 0) {
    const sess = _dashboardData.sessions[0];
    const activeWin = sess.windows.find(w => w.active) || sess.windows[0];
    if (activeWin) {
      createTab(sess.name, activeWin.index, activeWin.name);
    }
  }
  // Dashboard polling every 4s
  setInterval(loadDashboard, 4000);
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




if __name__ == "__main__":
    if not shutil.which("tmux"):
        print("Error: tmux is not installed. Install it first:")
        print("  macOS:  brew install tmux")
        print("  Ubuntu: sudo apt install tmux")
        sys.exit(1)
    uvicorn.run(app, host=HOST, port=PORT)
