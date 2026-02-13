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
    # Clear any existing input on the line before sending (Ctrl-U + Ctrl-K)
    subprocess.run(["tmux", "send-keys", "-t", target, "C-u"])
    # For large text, use set-buffer + paste-buffer for reliable delivery
    # -p enables bracketed paste so TUI apps (CC) treat it as a single paste, not line-by-line input
    if len(text) > 500 or '\n' in text:
        buf_name = "_mt_paste"
        subprocess.run(["tmux", "load-buffer", "-b", buf_name, "-"], input=text.encode())
        subprocess.run(["tmux", "paste-buffer", "-d", "-p", "-b", buf_name, "-t", target])
        time.sleep(0.05)  # Let TUI process bracketed paste before sending Enter
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"])
    else:
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


def detect_cc_status(text: str, activity_age: float = None) -> dict:
    """Detect if text is Claude Code output and its status.
    Returns dict with is_cc, status, context_pct, perm_mode.
    """
    is_cc = '\u276f' in text and '\u23fa' in text
    if not is_cc:
        return {"is_cc": False, "status": None, "context_pct": None, "perm_mode": None}

    lines = text.split('\n')

    # --- Text signals ---

    # 1. "esc to interrupt" on the status bar (line starting with ⏵)
    #    In current CC, this appears on the same line as the permissions bar:
    #    "⏵⏵ bypass permissions on (shift+tab to cycle) · 3 files · esc to interrupt"
    has_working = False
    context_pct = None
    perm_mode = None
    for line in lines[-3:]:
        if '\u23f5' in line:
            if 'esc to interrupt' in line:
                has_working = True
            # Context remaining: "Context left until auto-compact: 9%"
            m = re.search(r'Context left[^:]*:\s*(\d+)%', line)
            if m:
                context_pct = int(m.group(1))
            # Permission mode: text between ⏵⏵ and (shift+tab or first ·
            pm = re.search(r'\u23f5\u23f5\s+(.+?)(?:\s*\(shift\+tab|\s*\u00b7)', line)
            if pm:
                perm_mode = pm.group(1).strip()
            elif '\u23f5\u23f5' in line:
                # Fallback: grab everything after ⏵⏵ up to first ·
                pm2 = re.search(r'\u23f5\u23f5\s+(.+?)(?:\s*\u00b7|$)', line)
                if pm2:
                    perm_mode = pm2.group(1).strip()
            break

    # 2. Thinking: · at START of any line in last 20 lines
    tail = '\n'.join(lines[-20:])
    has_thinking = bool(re.search(r'^\u00b7', tail, re.MULTILINE))

    # --- Determine status ---
    if has_working:
        status = 'working'
    elif has_thinking:
        status = 'thinking'
    elif activity_age is not None and activity_age < 5:
        status = 'working'
    else:
        status = 'idle'

    return {"is_cc": True, "status": status, "context_pct": context_pct, "perm_mode": perm_mode}


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
        # Get preview for CC detection and sidebar snippet (40 lines)
        preview = get_pane_preview(sname, int(widx), lines=40)
        cc = detect_cc_status(preview, activity_age=activity_age)
        # Send last 40 lines for sidebar snippet extraction
        preview_short = '\n'.join(preview.split('\n')[-40:])
        sessions[sname]["windows"].append({
            "index": int(widx),
            "name": wname,
            "active": wactive == "1",
            "cwd": cwd,
            "command": cmd,
            "pid": pid,
            "is_cc": cc["is_cc"],
            "cc_status": cc["status"],
            "cc_context_pct": cc["context_pct"],
            "cc_perm_mode": cc["perm_mode"],
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
#sidebar.collapsed + #sidebar-resize { display:none; }
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
.sb-session.sb-drag-over { border-top:2px solid var(--accent); }
.sb-session.dragging { opacity:0.4; }
.sb-session-header { display:flex; align-items:center; gap:6px; padding:8px 8px 4px;
  color:var(--text3); font-size:11px; font-weight:700; text-transform:uppercase;
  letter-spacing:0.5px; cursor:grab; }
.sb-session-header .sb-badge { font-size:9px; padding:1px 5px; border-radius:6px;
  background:var(--accent); color:#fff; font-weight:500; text-transform:none;
  letter-spacing:0; }
.sb-win { display:flex; align-items:center; gap:8px; padding:6px 8px;
  border-radius:8px; cursor:pointer; transition:all .12s;
  -webkit-user-select:none; user-select:none; }
.sb-win:hover { background:var(--surface); }
.sb-win.active { background:rgba(217,119,87,0.08); }
.sb-win.sb-drag-over { border-top:2px solid var(--accent); margin-top:-2px; }
.sb-win.dragging { opacity:0.4; }
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
.sb-ctx { display:inline-block; font-size:9px; color:var(--text3); margin-left:4px; }
.sb-ctx.low { color:var(--orange); font-weight:700; }
.sb-ctx.critical { color:var(--red); font-weight:700; }
.sb-perm { font-size:9px; color:var(--text3); margin-top:1px; }
.sb-perm.danger { color:var(--red); font-weight:600; }
.sb-snippet { font-size:10px; color:var(--text3); margin-top:2px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  max-width:180px; font-style:italic; }
.sb-memo { font-size:10px; color:var(--accent); margin-top:2px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  max-width:180px; cursor:text; min-height:14px; }
.sb-memo:empty::before { content:'+ note'; color:var(--text3); opacity:0.5;
  font-style:italic; }
.sb-memo-edit { font-size:10px; color:var(--text); background:var(--surface2);
  border:1px solid var(--accent); border-radius:4px; padding:2px 4px;
  width:100%; margin-top:2px; outline:none; font-family:inherit; }
.sb-win-detail-btn { background:none; border:none; color:var(--text3);
  font-size:16px; cursor:pointer; padding:2px 4px; border-radius:4px;
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
  border-right:1px solid var(--border2); position:relative; }
.pane:last-child { border-right:none; }
.pane.drag-over { outline:2px solid var(--accent); outline-offset:-2px; }
.pane-stack { display:flex; flex-direction:column; flex:1; min-width:0;
  overflow:hidden; border-right:1px solid var(--border2); }
.pane-stack:last-child { border-right:none; }
.pane-stack > .pane { border-right:none; border-bottom:none; flex:1; }
#sidebar-resize { width:4px; flex-shrink:0; cursor:col-resize; background:transparent;
  transition:background .15s; z-index:3; }
#sidebar-resize:hover, #sidebar-resize.active { background:var(--accent); }
@media (max-width:768px) { #sidebar-resize { display:none; } }

.pane-divider { flex-shrink:0; background:var(--border2); transition:background .15s; z-index:2; }
.pane-divider.col { width:4px; cursor:col-resize; }
.pane-divider.row { height:4px; cursor:row-resize; }
.pane-divider:hover, .pane-divider.active { background:var(--accent); }
.drop-indicator { position:absolute; left:0; right:0; pointer-events:none;
  background:rgba(217,119,87,0.15); border:2px solid var(--accent);
  z-index:5; display:none; box-sizing:border-box; }
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
.pane-notepad-btn { background:none; border:none; color:var(--text3);
  font-size:10px; cursor:pointer; padding:2px 6px; margin-left:auto;
  flex-shrink:0; border-radius:3px; font-weight:600; letter-spacing:0.5px; }
.pane-notepad-btn:hover { color:var(--accent); background:rgba(255,255,255,0.05); }
.pane-notepad-btn.active { color:var(--accent); }
.notepad-panel { position:absolute; top:30px; right:0; z-index:10;
  width:min(380px, 95%); max-height:70%; background:var(--bg2);
  border:1px solid var(--border2); border-radius:0 0 0 12px;
  display:flex; flex-direction:column; overflow:hidden;
  transform:translateY(-10px); opacity:0; pointer-events:none;
  transition:transform .15s ease, opacity .15s ease; }
.notepad-panel.open { transform:translateY(0); opacity:1; pointer-events:auto; }
.notepad-header { display:flex; align-items:center; justify-content:space-between;
  padding:8px 12px; border-bottom:1px solid var(--border); flex-shrink:0; }
.notepad-header span { font-size:12px; font-weight:600; color:var(--text2); }
.notepad-close { background:none; border:none; color:var(--text3); cursor:pointer;
  font-size:14px; padding:0 4px; }
.notepad-close:hover { color:var(--text); }
.notepad-panel textarea { flex:1; background:transparent; color:var(--text);
  border:none; padding:10px 12px; font-size:13px; font-family:inherit;
  resize:none; outline:none; line-height:1.5; min-height:180px; }
.notepad-resize { height:6px; cursor:ns-resize; flex-shrink:0;
  background:transparent; position:relative; }
.notepad-resize::after { content:''; position:absolute; left:50%; top:50%;
  transform:translate(-50%,-50%); width:30px; height:3px; border-radius:2px;
  background:var(--border2); }
.notepad-resize-left { position:absolute; left:0; top:30px; bottom:6px;
  width:6px; cursor:ew-resize; z-index:1; }
.notepad-resize-corner { position:absolute; left:0; bottom:0;
  width:14px; height:14px; cursor:nesw-resize; z-index:2; }
.notepad-resize-corner::after { content:''; position:absolute; left:3px; bottom:3px;
  width:8px; height:8px; border-left:2px solid var(--border2);
  border-bottom:2px solid var(--border2); border-radius:0 0 0 2px; }

/* Queue panel */
.pane-queue-btn { background:none; border:none; color:var(--text3);
  font-size:10px; cursor:pointer; padding:2px 6px;
  flex-shrink:0; border-radius:3px; font-weight:600; letter-spacing:0.5px; }
.pane-queue-btn:hover { color:var(--accent); background:rgba(255,255,255,0.05); }
.pane-queue-btn.active { color:var(--accent); }
.pane-queue-btn.playing { color:#4ecf6a; }
.pane-refresh-btn { background:none; border:none; color:var(--text3);
  font-size:14px; cursor:pointer; padding:2px 6px; border-radius:4px; flex-shrink:0; }
.pane-refresh-btn:hover { color:var(--accent); background:rgba(255,255,255,0.05); }
.queue-panel { position:absolute; top:30px; right:0; z-index:10;
  width:min(480px, 95%); max-height:70%; background:var(--bg2);
  border:1px solid var(--border2); border-radius:0 0 0 12px;
  display:flex; flex-direction:column; overflow:hidden;
  transform:translateY(-10px); opacity:0; pointer-events:none;
  transition:transform .15s ease, opacity .15s ease; }
.queue-panel.open { transform:translateY(0); opacity:1; pointer-events:auto; }
.queue-header { display:flex; align-items:center; gap:8px;
  padding:8px 12px; border-bottom:1px solid var(--border); flex-shrink:0; }
.queue-header span { font-size:12px; font-weight:600; color:var(--text2); }
.queue-play-btn { background:none; border:none; cursor:pointer;
  font-size:14px; padding:0 4px; color:var(--text3); }
.queue-play-btn:hover { color:var(--text); }
.queue-play-btn.playing { color:#4ecf6a; }
.queue-close { background:none; border:none; color:var(--text3); cursor:pointer;
  font-size:14px; padding:0 4px; margin-left:auto; }
.queue-close:hover { color:var(--text); }
.queue-list { flex:1; overflow-y:auto; padding:4px 0; min-height:40px; }
.queue-resize { height:6px; cursor:ns-resize; flex-shrink:0;
  position:relative; background:transparent; }
.queue-resize::after { content:''; position:absolute; left:50%; top:50%;
  transform:translate(-50%,-50%); width:32px; height:3px; border-radius:2px; background:var(--border2); }
.queue-item { display:flex; align-items:center; gap:6px; padding:6px 8px 6px 4px;
  font-size:13px; color:var(--text); border-left:3px solid transparent; position:relative; }
.queue-item.current { border-left-color:var(--accent); background:rgba(217,119,87,0.08); }
.queue-item.done { opacity:0.45; }
.queue-tabs { display:flex; gap:0; flex-shrink:0; }
.queue-tab { flex:1; background:none; border:none; border-bottom:2px solid transparent;
  color:var(--text3); font-size:11px; font-weight:600; padding:6px 8px; cursor:pointer;
  text-transform:uppercase; letter-spacing:0.3px; transition:color .15s, border-color .15s; }
.queue-tab:hover { color:var(--text2); }
.queue-tab.active { color:var(--text); border-bottom-color:var(--accent); }
.queue-item.qi-dragging { opacity:0.3; }
.queue-item.qi-over { box-shadow:0 -2px 0 var(--accent); }
.qi-grip { cursor:grab; color:var(--text3); font-size:11px; padding:4px 2px;
  flex-shrink:0; touch-action:none; user-select:none; line-height:1; }
.qi-grip:active { cursor:grabbing; }
.queue-item-text { flex:1; word-break:break-word; line-height:1.4; cursor:text; border-radius:4px;
  padding:1px 4px; margin:-1px -4px; }
.queue-item-text:hover { background:rgba(255,255,255,0.04); }
.qi-edit { flex:1; background:var(--surface); color:var(--text); border:1px solid var(--accent);
  border-radius:6px; padding:4px 8px; font-size:13px; font-family:inherit; outline:none;
  line-height:1.4; min-width:0; resize:none; min-height:24px; max-height:80px; overflow-y:auto;
  field-sizing:content; }
.queue-item-remove { background:none; border:none; color:var(--text3); cursor:pointer;
  font-size:14px; padding:0 4px; flex-shrink:0; }
.queue-item-remove:hover { color:var(--red); }
.queue-add { display:flex; gap:6px; padding:8px 10px;
  border-top:1px solid var(--border); flex-shrink:0; }
.queue-add textarea { flex:1; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:8px; padding:6px 10px;
  font-size:13px; font-family:inherit; outline:none; resize:none;
  line-height:1.4; min-height:30px; max-height:80px; overflow-y:auto; }
.queue-add textarea::placeholder { color:var(--text3); }
.queue-add textarea:focus { border-color:rgba(217,119,87,0.5); }
.queue-add button { background:var(--accent); color:#fff; border:none;
  border-radius:8px; padding:6px 10px; font-size:13px; font-weight:600;
  cursor:pointer; flex-shrink:0; }
.queue-add button:active { transform:scale(0.95); }

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
  overflow-y:auto; max-height:40vh; line-height:1.4; }
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
  resize:none; overflow-y:auto; max-height:40vh;
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
  border-radius:16px; padding:20px; width:min(340px, 85vw); position:relative; }
#wd-modal h3 { font-size:14px; font-weight:600; color:var(--text); margin-bottom:14px; }
.wd-close-x { position:absolute; top:12px; right:12px; width:28px; height:28px;
  border:none; border-radius:50%; background:rgba(229,83,75,0.15); color:var(--red);
  font-size:16px; line-height:28px; text-align:center; cursor:pointer; padding:0; }
.wd-close-x:active { background:rgba(229,83,75,0.3); }
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
<div id="sidebar-resize"></div>
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
    <button class="wd-close-x" onclick="closeWDWindow()" title="Close window">&times;</button>
    <h3 id="wd-title">Window Details</h3>
    <div id="wd-content"></div>
    <div id="wd-rename-row">
      <input id="wd-rename-input" type="text" placeholder="Window name..."
        autocorrect="off" autocapitalize="none" spellcheck="false">
      <button class="wd-save-btn" onclick="saveWDRename()">Save</button>
    </div>
    <div class="wd-btns">
      <button class="wd-btn-dismiss" onclick="closeWD()">Close</button>
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
let _queueStates = {}; // tabId -> { items: [{text, done}], playing: false, currentIdx: null, idleTimer: null }

const M = document.getElementById('msg');
const bar = document.getElementById('bar');
const panesContainer = document.getElementById('panes-container');
const SEND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>';

// === Utility ===
function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function md(s) {
  if (typeof marked !== 'undefined') {
    try {
      marked.setOptions({ breaks: false });
      // Wrap lines with box-drawing chars in fenced code blocks
      const boxRe = /[\u2500-\u257f\u2580-\u259f]/;
      const lines = s.split('\\n');
      const out = []; let inBox = false;
      for (const line of lines) {
        if (boxRe.test(line)) {
          if (!inBox) { out.push('```'); inBox = true; }
          out.push(line);
        } else {
          if (inBox) { out.push('```'); inBox = false; }
          out.push(line);
        }
      }
      if (inBox) out.push('```');
      return marked.parse(out.join('\\n'));
    } catch(e) { /* fall through to plain text */ }
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
function extractSnippet(preview, isCC) {
  // Extract a short snippet of the latest Claude response from terminal preview
  if (!preview) return '';
  const lines = preview.split('\\n');
  if (isCC) {
    // Find the last ⏺ line that isn't a tool call
    let lastIdx = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      const t = lines[i].replace(/\\u00a0/g, ' ').trim();
      if (/^\\u23fa/.test(t)) {
        const after = t.replace(/^\\u23fa\\s*/, '');
        if (!/^(Bash|Read|Write|Update|Edit|Fetch|Search|Glob|Grep|Task|Skill|NotebookEdit|Searched for|Wrote \\d)/.test(after)) {
          lastIdx = i; break;
        }
      }
    }
    if (lastIdx < 0) return '';
    // Collect text from this ⏺ line forward until we hit a stop marker
    let snippetLines = [];
    for (let i = lastIdx; i < lines.length; i++) {
      const raw = lines[i].replace(/\\u00a0/g, ' ');
      const t = raw.trim();
      if (/^\\u276f/.test(raw)) break;
      if (/^\\s*\\u23f5/.test(t)) break;
      if (/^[\\u2500\\u2501\\u2504\\u2508\\u2550]{3,}$/.test(t) && t.length > 20) break;
      if (/^[\\u2720-\\u273f]/.test(t)) break;
      if (/^\\u23bf/.test(t)) break;
      if (/^\\u23fa/.test(t)) {
        const after = t.replace(/^\\u23fa\\s*/, '');
        if (/^(Bash|Read|Write|Update|Edit|Fetch|Search|Glob|Grep|Task|Skill|NotebookEdit|Searched for|Wrote \\d)/.test(after)) break;
        if (after) snippetLines.push(after);
      } else if (t) snippetLines.push(t);
    }
    return snippetLines.join(' ').substring(0, 120);
  }
  return '';
}
function getMemo(session, windowIndex) {
  return localStorage.getItem('memo:' + session + ':' + windowIndex) || '';
}
function setMemo(session, windowIndex, text) {
  const key = 'memo:' + session + ':' + windowIndex;
  if (text) localStorage.setItem(key, text);
  else localStorage.removeItem(key);
}
function startMemoEdit(el, session, windowIndex) {
  if (el.querySelector('.sb-memo-edit')) return;
  const current = getMemo(session, windowIndex);
  const input = document.createElement('input');
  input.className = 'sb-memo-edit';
  input.value = current;
  input.placeholder = 'Add a note...';
  const memoEl = el;
  memoEl.textContent = '';
  memoEl.appendChild(input);
  input.focus();
  const save = () => {
    const val = input.value.trim();
    setMemo(session, windowIndex, val);
    memoEl.textContent = val;
  };
  input.addEventListener('blur', save);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { input.value = getMemo(session, windowIndex); input.blur(); }
    e.stopPropagation();
  });
  input.addEventListener('click', (e) => e.stopPropagation());
}
function detectCCStatus(text) {
  // Quick client-side CC status detection from output text
  // Returns {status, contextPct, permMode} or null
  if (!isClaudeCode(text)) return null;
  const lines = text.split('\\n');
  let status = 'idle', contextPct = null, permMode = null;
  // Check status bar (last line with ⏵) for "esc to interrupt", context %, and perm mode
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 5); i--) {
    if (/^\\s*\\u23f5/.test(lines[i])) {
      if (/esc to interrupt/.test(lines[i])) status = 'working';
      const m = lines[i].match(/Context left[^:]*:\\s*(\\d+)%/);
      if (m) contextPct = parseInt(m[1]);
      // Extract permission mode: text between ⏵⏵ and (shift+tab or first ·
      const pm = lines[i].match(/\\u23f5\\u23f5\\s+(.+?)(?:\\s*\\(shift\\+tab|\\s*\\u00b7)/);
      if (pm) permMode = pm[1].trim();
      else {
        const pm2 = lines[i].match(/\\u23f5\\u23f5\\s+(.+?)(?:\\s*\\u00b7|$)/);
        if (pm2) permMode = pm2[1].trim();
      }
      break;
    }
  }
  // Check for thinking indicator
  if (status === 'idle' && /^\\u00b7/m.test(lines.slice(-15).join('\\n'))) status = 'thinking';
  return { status, contextPct, permMode };
}
function updateSidebarStatus(session, windowIndex, ccStatus, contextPct, permMode) {
  const wid = session + ':' + windowIndex;
  const dot = document.querySelector('.sb-win-dot[data-wid="' + wid + '"]');
  const lbl = document.querySelector('.sb-win-status[data-wid="' + wid + '"]');
  if (dot) { dot.className = 'sb-win-dot ' + (ccStatus || 'idle'); }
  if (lbl) {
    lbl.className = 'sb-win-status ' + (ccStatus || 'idle');
    let html = '';
    if (contextPct != null) {
      const cls = contextPct <= 10 ? 'critical' : contextPct <= 25 ? 'low' : '';
      html = '<span class="sb-ctx ' + cls + '">' + contextPct + '%</span>';
    }
    lbl.innerHTML = html;
  }
  // Update perm mode label
  const permEl = document.querySelector('.sb-perm[data-wid="' + wid + '"]');
  if (permEl && permMode) {
    permEl.textContent = permMode;
    permEl.className = 'sb-perm' + (/dangerously|skip/i.test(permMode) ? ' danger' : '');
  }
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
function isClaudeCode(text) { return /\\u276f/.test(text) && (/\\u23fa/.test(text) || /Claude Code v\\d/.test(text)); }
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
  // Trim to last CC session — find last startup banner ("Claude Code v")
  // and only parse from there, so old session content / shell lines are excluded
  let lines = text.split('\\n');
  let bannerIdx = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (/Claude Code v\\d/.test(lines[i])) { bannerIdx = i; break; }
  }
  if (bannerIdx >= 0) {
    // Find the first ❯ after the banner (skip banner block)
    let startIdx = bannerIdx;
    for (let i = bannerIdx; i < lines.length; i++) {
      if (/^\\u276f/.test(lines[i].replace(/\\u00a0/g, ' '))) { startIdx = i; break; }
    }
    lines = lines.slice(startIdx);
  }
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
      // Skip CC slash commands (/clear, /help, etc.) — they're meta, not conversation
      if (msg.startsWith('/')) { cur = null; inTool = false; sawStatus = false; continue; }
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
function stripSuggestion(raw) {
  if (!/\\u276f/.test(raw) || !/\\u23fa/.test(raw)) return raw;
  const clean = cleanTerminal(raw);
  if (!isIdle(clean)) return raw;
  // Find the last ❯ prompt line and strip ghost suggestion text after it
  const lines = raw.split('\\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const s = lines[i].replace(/\\u00a0/g, ' ');
    if (/\\u276f/.test(s)) {
      lines[i] = lines[i].replace(/(\\u276f)\\s*.*/, '$1');
      break;
    }
  }
  return lines.join('\\n');
}
function renderOutput(raw, targetEl, state, tabId) {
  if (state.rawMode) {
    targetEl.className = 'pane-output raw';
    targetEl.textContent = stripSuggestion(raw);
    return;
  }
  targetEl.className = 'pane-output chat';
  const clean = cleanTerminal(raw);
  let html = '';
  if (isClaudeCode(clean)) {
    const turns = parseCCTurns(clean);
    if (state.pendingMsg) {
      const userTurns = turns.filter(t => t.role === 'user');
      const anyMatch = userTurns.some(u => u.lines.join(' ').includes(state.pendingMsg.substring(0, 20)));
      if (anyMatch) state.pendingMsg = null;
      // Safety timeout: 10s max for pendingMsg display
      else if (state.pendingTime && (Date.now() - state.pendingTime) > 10000)
        state.pendingMsg = null;
    }
    const wasAwaiting = state.awaitingResponse;
    if (state.awaitingResponse) {
      const elapsed = Date.now() - state.pendingTime;
      if (elapsed > 3000 && isIdle(clean)) state.awaitingResponse = false;
      // Staleness fallback for interactive menus (status bar still shows "esc to interrupt"
      // but CC is actually waiting for input). Only fire after 30s to avoid false positives
      // during extended thinking or long tool executions.
      const staleAge = state.lastOutputChange > 0 ? Date.now() - state.lastOutputChange : 0;
      if (elapsed > 5000 && staleAge > 30000) state.awaitingResponse = false;
      if (elapsed > 180000) state.awaitingResponse = false;
    }
    if (wasAwaiting && !state.awaitingResponse && tabId) {
      onQueueTaskCompleted(tabId);
    }
    let lastRole = '';
    for (const t of turns) {
      const text = t.lines.join('\\n').trim();
      if (!text) continue;
      if (t.role === 'user') {
        html += '<div class="turn user"><div class="turn-label">You</div><div class="turn-body">' + esc(text) + '</div></div>';
      } else {
        const label = lastRole !== 'assistant' ? '<div class="turn-label">Claude</div>' : '';
        html += '<div class="turn assistant">' + label + '<div class="turn-body">' + md(text) + '</div></div>';
      }
      lastRole = t.role;
    }
    if (state.pendingMsg)
      html += '<div class="turn user"><div class="turn-label">You</div><div class="turn-body">' + esc(state.pendingMsg) + '</div></div>';
    if (state.awaitingResponse || !isIdle(clean))
      html += '<div class="turn assistant"><div class="turn-label">Claude</div><div class="turn-body"><p class="thinking">Working\\u2026</p></div></div>';
    if (!html)
      html = '<div class="turn assistant"><div class="turn-label">Claude</div><div class="turn-body"><p style="color:var(--text3)">Ready</p></div></div>';
  } else {
    if (clean.trim())
      html = '<div class="turn assistant"><div class="turn-label">Terminal</div><div class="turn-body mono">' + esc(clean) + '</div></div>';
  }
  if (!html)
    html = '<div class="turn assistant"><div class="turn-label">Terminal</div><div class="turn-body"><p style="color:var(--text3)">Waiting for output...</p></div></div>';
  targetEl.innerHTML = html;
}

// === Pane management ===
function createPane(parentEl) {
  if (panes.length >= 6) return null;
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
    + '<button class="pane-send" aria-label="Send">' + SEND_SVG + '</button></div>'
    + '<div class="drop-indicator"></div>';
  (parentEl || panesContainer).appendChild(el);
  // Pane input handlers
  const ta = el.querySelector('.pane-input textarea');
  const sendBtn = el.querySelector('.pane-send');
  const paneResize = () => { const max=window.innerHeight*0.4; ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,max)+'px'; ta.style.overflowY=ta.scrollHeight>max?'auto':'hidden'; };
  ta.addEventListener('input', paneResize);
  ta.addEventListener('paste', () => setTimeout(paneResize, 0));
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (ta.value.trim()) sendToPane(id); else keyActive('Enter'); }
    if (!ta.value && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); keyActive(e.key === 'ArrowUp' ? 'Up' : 'Down'); }
    if (!ta.value && e.key === 'Escape') { e.preventDefault(); keyActive('Escape'); }
    if (!ta.value && e.key === 'Tab') { e.preventDefault(); keyActive('Tab'); }
  });
  sendBtn.addEventListener('click', () => sendToPane(id));
  // Click anywhere on pane to focus it
  el.addEventListener('mousedown', () => focusPane(id));
  // Drop target with vertical split detection
  el.addEventListener('dragover', e => {
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    const relY = (e.clientY - rect.top) / rect.height;
    const indicator = el.querySelector('.drop-indicator');
    if (relY > 0.65 && panes.length < 6) {
      indicator.style.top = '50%'; indicator.style.bottom = '0';
      indicator.style.display = 'block';
      el.classList.remove('drag-over');
      el._dropZone = 'bottom';
    } else if (relY < 0.35 && panes.length < 6 && el.parentElement.classList.contains('pane-stack')) {
      indicator.style.top = '0'; indicator.style.bottom = '50%';
      indicator.style.display = 'block';
      el.classList.remove('drag-over');
      el._dropZone = 'top';
    } else {
      indicator.style.display = 'none';
      el.classList.add('drag-over');
      el._dropZone = 'same';
    }
  });
  el.addEventListener('dragleave', () => {
    el.classList.remove('drag-over');
    el.querySelector('.drop-indicator').style.display = 'none';
  });
  el.addEventListener('drop', e => {
    e.preventDefault(); el.classList.remove('drag-over');
    el.querySelector('.drop-indicator').style.display = 'none';
    const tabId = parseInt(e.dataTransfer.getData('text/plain'));
    if (!tabId) return;
    if (el._dropZone === 'bottom') { splitPaneVertically(id, tabId, 'after'); }
    else if (el._dropZone === 'top') { splitPaneVertically(id, tabId, 'before'); }
    else { moveTabToPane(tabId, id); }
  });
  focusPane(id);
  updateLayout();
  return id;
}

function splitPaneVertically(existingPaneId, tabId, position) {
  if (panes.length >= 6) return;
  const existingEl = document.getElementById('pane-' + existingPaneId);
  if (!existingEl) return;
  let stack = existingEl.parentElement;
  if (!stack.classList.contains('pane-stack')) {
    stack = document.createElement('div');
    stack.className = 'pane-stack';
    existingEl.parentElement.insertBefore(stack, existingEl);
    stack.appendChild(existingEl);
  }
  const newId = createPane(stack);
  if (newId === null) return;
  const newEl = document.getElementById('pane-' + newId);
  if (position === 'before' && newEl) stack.insertBefore(newEl, existingEl);
  moveTabToPane(tabId, newId);
}

function removePane(paneId) {
  const idx = panes.findIndex(p => p.id === paneId);
  if (idx < 0 || panes.length <= 1) return;
  const pane = panes[idx];
  // Close all tabs in this pane
  for (const tid of [...pane.tabIds]) closeTab(tid, true);
  panes.splice(idx, 1);
  const el = document.getElementById('pane-' + paneId);
  if (el) {
    const stack = el.parentElement;
    el.remove();
    // Unwrap stack if only one pane remains
    if (stack.classList.contains('pane-stack')) {
      const remaining = stack.querySelectorAll('.pane');
      if (remaining.length <= 1) {
        if (remaining.length === 1) stack.parentElement.insertBefore(remaining[0], stack);
        stack.remove();
      }
    }
  }
  if (activePaneId === paneId) focusPane(panes[0].id);
  updateLayout();
  renderSidebar();
  saveLayout();
}

function focusPane(paneId) {
  const changed = activePaneId !== paneId;
  activePaneId = paneId;
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('focused'));
  const el = document.getElementById('pane-' + paneId);
  if (el) el.classList.add('focused');
  if (changed) renderSidebar();
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
  saveLayout();
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
  // Clean up queue state
  if (_queueStates[tabId]) {
    if (_queueStates[tabId].idleTimer) clearTimeout(_queueStates[tabId].idleTimer);
    delete _queueStates[tabId];
  }
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
  saveLayout();
}

function focusTab(tabId) {
  // Find pane
  for (const p of panes) {
    if (p.tabIds.includes(tabId)) {
      const tabChanged = p.activeTabId !== tabId;
      p.activeTabId = tabId;
      focusPane(p.id);
      if (tabChanged) renderSidebar();
      renderPaneTabs(p.id);
      showActiveTabOutput(p.id);
      updateNotepadContent(p.id);
      updateQueueContent(p.id);
      updatePolling();
      // Update view label
      const state = tabStates[tabId];
      if (state) document.getElementById('view-label').textContent = state.rawMode ? 'Raw' : 'Clean';
      saveLayout();
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
  saveLayout();
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
  // Notepad toggle button (only if pane has an active tab)
  if (pane.activeTabId) {
    html += '<button class="pane-notepad-btn' + (paneEl.querySelector('.notepad-panel.open') ? ' active' : '') + '" onclick="toggleNotepad(' + paneId + ')" title="Notepad">NOTES</button>';
    const qs = _queueStates[pane.activeTabId];
    const qOpen = paneEl.querySelector('.queue-panel.open');
    const qPlaying = qs && qs.playing;
    const qRemaining = qs ? qs.items.filter(i => !i.done).length : 0;
    html += '<button class="pane-queue-btn' + (qOpen ? ' active' : '') + (qPlaying ? ' playing' : '') + '" onclick="toggleQueue(' + paneId + ')" title="Task Queue">QUEUE' + (qRemaining > 0 ? ' ' + qRemaining : '') + '</button>';
    html += '<button class="pane-refresh-btn" onclick="hardRefresh(' + paneId + ')" title="Refresh">&#x21bb;</button>';
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

// === Layout persistence ===
let _restoringLayout = false;
function savePaneData(p) {
  return {
    tabIds: p.tabIds.map(tid => {
      const t = allTabs[tid];
      return t ? { session: t.session, windowIndex: t.windowIndex, windowName: t.windowName } : null;
    }).filter(Boolean),
    activeTab: p.activeTabId ? (() => {
      const t = allTabs[p.activeTabId];
      return t ? { session: t.session, windowIndex: t.windowIndex } : null;
    })() : null,
  };
}
function saveLayout() {
  if (_restoringLayout) return;
  try {
    const layout = [];
    for (const child of panesContainer.children) {
      if (child.classList.contains('pane-stack')) {
        const stackPanes = [];
        for (const pEl of child.querySelectorAll('.pane')) {
          const p = panes.find(x => x.id === parseInt(pEl.id.replace('pane-','')));
          if (p) stackPanes.push(savePaneData(p));
        }
        if (stackPanes.length) layout.push({ stack: stackPanes });
      } else if (child.classList.contains('pane')) {
        const p = panes.find(x => x.id === parseInt(child.id.replace('pane-','')));
        if (p) layout.push(savePaneData(p));
      }
    }
    localStorage.setItem('layout', JSON.stringify(layout));
  } catch(e) {}
}

// === Notepad ===
function notepadKey(tabId) {
  const tab = allTabs[tabId];
  if (!tab) return null;
  return 'notepad:' + tab.session + ':' + tab.windowIndex;
}

function toggleNotepad(paneId) {
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  let panel = paneEl.querySelector('.notepad-panel');
  if (panel && panel.classList.contains('open')) {
    panel.classList.remove('open');
    paneEl.querySelector('.pane-notepad-btn')?.classList.remove('active');
    return;
  }
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  // Close queue if open
  const qp = paneEl.querySelector('.queue-panel.open');
  if (qp) { qp.classList.remove('open'); paneEl.querySelector('.pane-queue-btn')?.classList.remove('active'); }
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'notepad-panel';
    panel.innerHTML = '<div class="notepad-header"><span>Notes</span>'
      + '<button class="notepad-close" onclick="toggleNotepad(' + paneId + ')">&times;</button></div>'
      + '<textarea placeholder="Jot notes for this window..."></textarea>'
      + '<div class="notepad-resize"></div>'
      + '<div class="notepad-resize-left"></div>'
      + '<div class="notepad-resize-corner"></div>';
    panel.querySelector('textarea').addEventListener('input', function() {
      const pn = panes.find(x => x.id === paneId);
      if (!pn || !pn.activeTabId) return;
      const key = notepadKey(pn.activeTabId);
      if (key) try { localStorage.setItem(key, this.value); } catch(e) {}
    });
    // Drag-to-resize (bottom=vertical, left=horizontal, corner=both)
    function setupResize(handle, mode) {
      let sx, sy, sw, sh;
      function onMove(e) {
        if (mode !== 'h') panel.style.height = Math.max(120, sh + (e.clientY - sy)) + 'px';
        if (mode !== 'v') panel.style.width = Math.max(200, sw - (e.clientX - sx)) + 'px';
      }
      function onUp() {
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        try { localStorage.setItem('notepad:size', JSON.stringify({
          w: panel.style.width, h: panel.style.height
        })); } catch(e) {}
      }
      handle.addEventListener('pointerdown', function(e) {
        e.preventDefault();
        sx = e.clientX; sy = e.clientY;
        sw = panel.offsetWidth; sh = panel.offsetHeight;
        document.addEventListener('pointermove', onMove);
        document.addEventListener('pointerup', onUp);
      });
    }
    setupResize(panel.querySelector('.notepad-resize'), 'v');
    setupResize(panel.querySelector('.notepad-resize-left'), 'h');
    setupResize(panel.querySelector('.notepad-resize-corner'), 'both');
    // Restore saved size
    try {
      const saved = localStorage.getItem('notepad:size');
      if (saved) {
        const sz = JSON.parse(saved);
        if (sz.w) panel.style.width = sz.w;
        if (sz.h) panel.style.height = sz.h;
      }
    } catch(e) {}
    paneEl.appendChild(panel);
  }
  // Load content for active tab
  const key = notepadKey(pane.activeTabId);
  const ta = panel.querySelector('textarea');
  if (key) {
    try { ta.value = localStorage.getItem(key) || ''; } catch(e) { ta.value = ''; }
  } else { ta.value = ''; }
  panel.classList.add('open');
  paneEl.querySelector('.pane-notepad-btn')?.classList.add('active');
  ta.focus();
}

function updateNotepadContent(paneId) {
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const panel = paneEl.querySelector('.notepad-panel');
  if (!panel || !panel.classList.contains('open')) return;
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const key = notepadKey(pane.activeTabId);
  const ta = panel.querySelector('textarea');
  if (key) {
    try { ta.value = localStorage.getItem(key) || ''; } catch(e) { ta.value = ''; }
  } else { ta.value = ''; }
}

// === Task Queue ===
function queueKey(tabId) {
  const tab = allTabs[tabId];
  if (!tab) return null;
  return 'queue:' + tab.session + ':' + tab.windowIndex;
}

function getQueueState(tabId) {
  if (!_queueStates[tabId]) {
    let items = [];
    const key = queueKey(tabId);
    if (key) {
      try {
        const saved = localStorage.getItem(key);
        if (saved) items = JSON.parse(saved);
      } catch(e) {}
    }
    _queueStates[tabId] = { items: items, playing: false, currentIdx: null, idleTimer: null };
  }
  return _queueStates[tabId];
}

function saveQueue(tabId) {
  const key = queueKey(tabId);
  if (!key) return;
  const qs = _queueStates[tabId];
  if (!qs) return;
  try { localStorage.setItem(key, JSON.stringify(qs.items)); } catch(e) {}
}

function toggleQueue(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  let panel = paneEl.querySelector('.queue-panel');
  if (panel && panel.classList.contains('open')) {
    panel.classList.remove('open');
    paneEl.querySelector('.pane-queue-btn')?.classList.remove('active');
    return;
  }
  // Close notepad if open
  const np = paneEl.querySelector('.notepad-panel.open');
  if (np) { np.classList.remove('open'); paneEl.querySelector('.pane-notepad-btn')?.classList.remove('active'); }
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'queue-panel';
    paneEl.appendChild(panel);
  }
  getQueueState(pane.activeTabId);
  renderQueuePanel(paneId);
  panel.classList.add('open');
  paneEl.querySelector('.pane-queue-btn')?.classList.add('active');
  const inp = panel.querySelector('.queue-add textarea');
  if (inp) inp.focus();
}

function switchQueueTab(paneId, tab) {
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const panel = paneEl.querySelector('.queue-panel');
  if (!panel) return;
  panel._activeTab = tab;
  renderQueuePanel(paneId);
}

function renderQueuePanel(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const panel = paneEl.querySelector('.queue-panel');
  if (!panel) return;
  if (panel.querySelector('.qi-edit')) return;
  // Save user-set list height before rebuild
  const prevList = panel.querySelector('.queue-list');
  const savedMaxH = panel._userResized && prevList ? prevList.style.maxHeight : null;
  const qs = getQueueState(pane.activeTabId);
  const activeTab = panel._activeTab || 'queue';
  const active = [], past = [];
  for (let i = 0; i < qs.items.length; i++) {
    if (qs.items[i].done) past.push(i); else active.push(i);
  }
  const playIcon = qs.playing ? '\\u23f8' : '\\u25b6';
  let html = '<div class="queue-header">'
    + '<button class="queue-play-btn' + (qs.playing ? ' playing' : '') + '" onclick="toggleQueuePlay(' + paneId + ')" title="' + (qs.playing ? 'Pause' : 'Play') + '">' + playIcon + '</button>'
    + '<span>Queue' + (active.length ? ' (' + active.length + ')' : '') + '</span>'
    + '<button class="queue-close" onclick="toggleQueue(' + paneId + ')">&times;</button>'
    + '</div>'
    + '<div class="queue-tabs">'
    + '<button class="queue-tab' + (activeTab === 'queue' ? ' active' : '') + '" onclick="switchQueueTab(' + paneId + ',\\'queue\\')">Queue</button>'
    + '<button class="queue-tab' + (activeTab === 'completed' ? ' active' : '') + '" onclick="switchQueueTab(' + paneId + ',\\'completed\\')">Completed' + (past.length ? ' (' + past.length + ')' : '') + '</button>'
    + '</div><div class="queue-list">';
  if (activeTab === 'queue') {
    if (!active.length) {
      html += '<div style="padding:16px 12px;color:var(--text3);font-size:12px;text-align:center;">No tasks yet</div>';
    }
    for (const i of active) {
      const item = qs.items[i];
      const cls = i === qs.currentIdx ? ' current' : '';
      html += '<div class="queue-item' + cls + '" data-qi="' + i + '">'
        + '<span class="qi-grip" data-qi-grip="' + i + '">&#9776;</span>'
        + '<span class="queue-item-text" onclick="editQueueItem(' + pane.activeTabId + ',' + i + ',this)">' + esc(item.text) + '</span>'
        + '<button class="queue-item-remove" onclick="removeQueueItem(' + pane.activeTabId + ',' + i + ')">&times;</button>'
        + '</div>';
    }
    html += '</div><div class="queue-add">'
      + '<textarea rows="1" placeholder="Add a task\\u2026" onkeydown="if(event.key===\\'Enter\\'&&!event.shiftKey){event.preventDefault();addQueueItem(' + paneId + ');}" oninput="this.style.height=\\'auto\\';this.style.height=Math.min(this.scrollHeight,80)+\\'px\\'"></textarea>'
      + '<button onclick="addQueueItem(' + paneId + ')">+</button>'
      + '</div>';
  } else {
    if (!past.length) {
      html += '<div style="padding:16px 12px;color:var(--text3);font-size:12px;text-align:center;">No completed tasks</div>';
    }
    for (const i of past) {
      html += '<div class="queue-item done" data-qi="' + i + '">'
        + '<span class="queue-item-text" style="cursor:default">' + esc(qs.items[i].text) + '</span>'
        + '<button class="queue-item-remove" onclick="removeQueueItem(' + pane.activeTabId + ',' + i + ')">&times;</button>'
        + '</div>';
    }
    if (past.length) {
      html += '<div style="padding:8px 10px;border-top:1px solid var(--border);">'
        + '<button onclick="clearCompletedQueue(' + paneId + ')" style="background:none;border:1px solid var(--border2);color:var(--text3);border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer;width:100%;">Clear completed</button>'
        + '</div>';
    }
    html += '</div>';
  }
  html += '<div class="queue-resize"></div>';
  panel.innerHTML = html;
  if (savedMaxH) {
    const newList = panel.querySelector('.queue-list');
    if (newList) newList.style.maxHeight = savedMaxH;
  }
  if (activeTab === 'queue') setupQueueDrag(paneId, panel);
  setupQueueResize(paneId, panel);
  autoFitQueue(panel);
}

function autoFitQueue(panel) {
  if (panel._userResized) return;
  const list = panel.querySelector('.queue-list');
  if (!list) return;
  // Remove any fixed max-height so we can measure natural height
  list.style.maxHeight = 'none';
  const natural = list.scrollHeight;
  // Cap at 60% of viewport
  const cap = window.innerHeight * 0.6;
  list.style.maxHeight = Math.min(natural, cap) + 'px';
}

function setupQueueResize(paneId, panel) {
  const handle = panel.querySelector('.queue-resize');
  if (!handle) return;
  handle.addEventListener('pointerdown', e => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    const list = panel.querySelector('.queue-list');
    if (!list) return;
    const startY = e.clientY;
    const startH = list.getBoundingClientRect().height;
    function onMove(ev) {
      const newH = Math.max(40, startH + (ev.clientY - startY));
      list.style.maxHeight = newH + 'px';
    }
    function onUp() {
      panel._userResized = true;
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', onUp);
    }
    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', onUp);
  });
}

function setupQueueDrag(paneId, panel) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const tabId = pane.activeTabId;
  const list = panel.querySelector('.queue-list');
  if (!list) return;
  let dragIdx = null, overIdx = null;

  list.addEventListener('pointerdown', e => {
    const grip = e.target.closest('[data-qi-grip]');
    if (!grip) return;
    e.preventDefault();
    dragIdx = parseInt(grip.dataset.qiGrip);
    const dragEl = list.querySelector('[data-qi="' + dragIdx + '"]');
    if (dragEl) dragEl.classList.add('qi-dragging');
    grip.setPointerCapture(e.pointerId);

    function getOverIdx(ev) {
      const items = [...list.querySelectorAll('.queue-item')];
      for (const it of items) {
        const r = it.getBoundingClientRect();
        if (ev.clientY < r.top + r.height / 2) return parseInt(it.dataset.qi);
      }
      return items.length > 0 ? parseInt(items[items.length - 1].dataset.qi) + 1 : 0;
    }

    function onMove(ev) {
      const newOver = getOverIdx(ev);
      if (newOver !== overIdx) {
        list.querySelectorAll('.qi-over').forEach(el => el.classList.remove('qi-over'));
        overIdx = newOver;
        const target = list.querySelector('[data-qi="' + overIdx + '"]');
        if (target && overIdx !== dragIdx && overIdx !== dragIdx + 1) target.classList.add('qi-over');
      }
    }

    function onUp() {
      list.querySelectorAll('.qi-dragging,.qi-over').forEach(el => el.classList.remove('qi-dragging', 'qi-over'));
      if (dragIdx !== null && overIdx !== null && overIdx !== dragIdx && overIdx !== dragIdx + 1) {
        const qs = _queueStates[tabId];
        if (qs) {
          const [moved] = qs.items.splice(dragIdx, 1);
          const insertAt = overIdx > dragIdx ? overIdx - 1 : overIdx;
          qs.items.splice(insertAt, 0, moved);
          // Adjust currentIdx
          if (qs.currentIdx !== null) {
            if (qs.currentIdx === dragIdx) qs.currentIdx = insertAt;
            else {
              if (dragIdx < qs.currentIdx && insertAt >= qs.currentIdx) qs.currentIdx--;
              else if (dragIdx > qs.currentIdx && insertAt <= qs.currentIdx) qs.currentIdx++;
            }
          }
          saveQueue(tabId);
          renderQueuePanel(paneId);
        }
      }
      dragIdx = null; overIdx = null;
      grip.removeEventListener('pointermove', onMove);
      grip.removeEventListener('pointerup', onUp);
      grip.removeEventListener('pointercancel', onUp);
    }

    grip.addEventListener('pointermove', onMove);
    grip.addEventListener('pointerup', onUp);
    grip.addEventListener('pointercancel', onUp);
  });
}

function addQueueItem(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const inp = paneEl.querySelector('.queue-add textarea');
  if (!inp) return;
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  inp.style.height = 'auto';
  const qs = getQueueState(pane.activeTabId);
  qs.items.push({ text: text, done: false });
  saveQueue(pane.activeTabId);
  renderQueuePanel(paneId);
  renderPaneTabs(paneId);
  paneEl.querySelector('.queue-add textarea')?.focus();
}

function removeQueueItem(tabId, idx) {
  const qs = _queueStates[tabId];
  if (!qs || idx < 0 || idx >= qs.items.length) return;
  // Adjust currentIdx if needed
  if (qs.currentIdx !== null) {
    if (idx < qs.currentIdx) qs.currentIdx--;
    else if (idx === qs.currentIdx) qs.currentIdx = null;
  }
  qs.items.splice(idx, 1);
  saveQueue(tabId);
  // Re-render in the pane that has this tab
  for (const p of panes) {
    if (p.activeTabId === tabId) { renderQueuePanel(p.id); renderPaneTabs(p.id); break; }
  }
}

function clearCompletedQueue(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const qs = _queueStates[pane.activeTabId];
  if (!qs) return;
  // Adjust currentIdx for removed items
  let removed = 0;
  qs.items = qs.items.filter((item, i) => {
    if (item.done) {
      if (qs.currentIdx !== null && i < qs.currentIdx) removed++;
      return false;
    }
    return true;
  });
  if (qs.currentIdx !== null) qs.currentIdx -= removed;
  saveQueue(pane.activeTabId);
  renderQueuePanel(paneId);
  renderPaneTabs(paneId);
}

function editQueueItem(tabId, idx, span) {
  const qs = _queueStates[tabId];
  if (!qs || idx < 0 || idx >= qs.items.length) return;
  const item = qs.items[idx];
  const inp = document.createElement('textarea');
  inp.className = 'qi-edit';
  inp.enterKeyHint = 'done';
  inp.rows = 1;
  inp.value = item.text;
  span.replaceWith(inp);
  inp.style.height = 'auto';
  inp.style.height = Math.min(inp.scrollHeight, 80) + 'px';
  inp.focus();
  inp.select();
  function save() {
    if (inp._saved) return;
    inp._saved = true;
    const text = inp.value.trim();
    if (text && text !== item.text) {
      item.text = text;
      saveQueue(tabId);
    }
    inp.classList.remove('qi-edit');
    for (const p of panes) {
      if (p.activeTabId === tabId) { renderQueuePanel(p.id); break; }
    }
  }
  inp.addEventListener('blur', save);
  inp.addEventListener('input', () => { inp.style.height = 'auto'; inp.style.height = Math.min(inp.scrollHeight, 80) + 'px'; });
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); inp.blur(); }
    if (e.key === 'Escape') { inp.value = item.text; inp.blur(); }
  });
}

function toggleQueuePlay(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const qs = getQueueState(pane.activeTabId);
  if (qs.playing) {
    pauseQueue(pane.activeTabId);
  } else {
    startQueue(pane.activeTabId);
  }
  renderQueuePanel(paneId);
  renderPaneTabs(paneId);
}

function startQueue(tabId) {
  const qs = getQueueState(tabId);
  qs.playing = true;
  scheduleQueueCheck(tabId);
}

function pauseQueue(tabId) {
  const qs = _queueStates[tabId];
  if (!qs) return;
  qs.playing = false;
  if (qs.idleTimer) { clearTimeout(qs.idleTimer); qs.idleTimer = null; }
  // Re-render in the pane that has this tab
  for (const p of panes) {
    if (p.activeTabId === tabId) { renderQueuePanel(p.id); renderPaneTabs(p.id); break; }
  }
}

function scheduleQueueCheck(tabId) {
  const qs = _queueStates[tabId];
  if (!qs || !qs.playing) return;
  if (qs.idleTimer) clearTimeout(qs.idleTimer);
  qs.idleTimer = setTimeout(() => {
    qs.idleTimer = null;
    tryDispatchNext(tabId);
  }, 2000);
}

function tryDispatchNext(tabId) {
  const qs = _queueStates[tabId];
  if (!qs || !qs.playing) return;
  const state = tabStates[tabId];
  const tab = allTabs[tabId];
  if (!state || !tab) return;
  // Don't dispatch if already awaiting a response
  if (state.awaitingResponse) { scheduleQueueCheck(tabId); return; }
  // Find next undone item
  let nextIdx = -1;
  for (let i = 0; i < qs.items.length; i++) {
    if (!qs.items[i].done) { nextIdx = i; break; }
  }
  if (nextIdx < 0) {
    // All done
    qs.playing = false; qs.currentIdx = null;
    for (const p of panes) {
      if (p.activeTabId === tabId) { renderQueuePanel(p.id); renderPaneTabs(p.id); break; }
    }
    return;
  }
  qs.currentIdx = nextIdx;
  const text = 'please execute this task: ' + qs.items[nextIdx].text;
  state.pendingMsg = text; state.pendingTime = Date.now(); state.awaitingResponse = true;
  const outEl = document.getElementById('tab-output-' + tabId);
  if (outEl) { renderOutput(state.rawContent || state.last, outEl, state, tabId); outEl.scrollTop = outEl.scrollHeight; }
  fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: text, session: tab.session, window: tab.windowIndex })
  });
  for (const p of panes) {
    if (p.activeTabId === tabId) { renderQueuePanel(p.id); break; }
  }
}

function onQueueTaskCompleted(tabId) {
  const qs = _queueStates[tabId];
  if (!qs || !qs.playing) return;
  if (qs.currentIdx !== null && qs.currentIdx < qs.items.length) {
    qs.items[qs.currentIdx].done = true;
    qs.currentIdx = null;
    saveQueue(tabId);
  }
  // Re-render and schedule next
  for (const p of panes) {
    if (p.activeTabId === tabId) { renderQueuePanel(p.id); break; }
  }
  scheduleQueueCheck(tabId);
}

function updateQueueContent(paneId) {
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const panel = paneEl.querySelector('.queue-panel');
  if (!panel || !panel.classList.contains('open')) return;
  renderQueuePanel(paneId);
}

// === Layout ===
function updateLayout() {
  const multiPane = panes.length > 1;
  bar.classList.toggle('hidden', multiPane);
  document.querySelectorAll('.pane-input').forEach(pi => {
    pi.classList.toggle('visible', multiPane);
  });
  updateDividers();
}

function updateDividers() {
  document.querySelectorAll('.pane-divider').forEach(d => d.remove());
  addDividers(panesContainer, 'col');
  document.querySelectorAll('.pane-stack').forEach(s => addDividers(s, 'row'));
}
function addDividers(parent, dir) {
  const kids = [...parent.children].filter(c =>
    c.classList.contains('pane') || c.classList.contains('pane-stack'));
  for (let i = 1; i < kids.length; i++) {
    const d = document.createElement('div');
    d.className = 'pane-divider ' + dir;
    parent.insertBefore(d, kids[i]);
    setupDivider(d, dir);
  }
}
function setupDivider(div, dir) {
  let startPos, prevEl, nextEl, prevSize, nextSize;
  div.addEventListener('pointerdown', e => {
    e.preventDefault();
    prevEl = div.previousElementSibling;
    nextEl = div.nextElementSibling;
    if (!prevEl || !nextEl) return;
    div.classList.add('active');
    if (dir === 'col') {
      startPos = e.clientX;
      prevSize = prevEl.getBoundingClientRect().width;
      nextSize = nextEl.getBoundingClientRect().width;
    } else {
      startPos = e.clientY;
      prevSize = prevEl.getBoundingClientRect().height;
      nextSize = nextEl.getBoundingClientRect().height;
    }
    prevEl.style.flex = 'none'; nextEl.style.flex = 'none';
    if (dir === 'col') {
      prevEl.style.width = prevSize + 'px'; nextEl.style.width = nextSize + 'px';
    } else {
      prevEl.style.height = prevSize + 'px'; nextEl.style.height = nextSize + 'px';
    }
    function onMove(ev) {
      const delta = dir === 'col' ? ev.clientX - startPos : ev.clientY - startPos;
      const p = Math.max(80, prevSize + delta);
      const n = Math.max(80, nextSize - delta);
      if (dir === 'col') { prevEl.style.width = p + 'px'; nextEl.style.width = n + 'px'; }
      else { prevEl.style.height = p + 'px'; nextEl.style.height = n + 'px'; }
    }
    function onUp() {
      div.classList.remove('active');
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
    }
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
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
async function hardRefresh(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const tabId = pane.activeTabId;
  const tab = allTabs[tabId]; const state = tabStates[tabId];
  if (!tab || !state) return;
  // Clear all cached state
  state.last = null; state.rawContent = null; state.pendingMsg = null;
  state.awaitingResponse = false;
  try {
    const r = await fetch('/api/output?session=' + encodeURIComponent(tab.session) + '&window=' + tab.windowIndex);
    const d = await r.json();
    state.lastOutputChange = Date.now();
    state.last = d.output; state.rawContent = d.output;
    const outEl = document.getElementById('tab-output-' + tabId);
    if (outEl) { renderOutput(d.output, outEl, state, tabId); outEl.scrollTop = outEl.scrollHeight; }
  } catch(e) {}
}

async function pollTab(tabId) {
  const tab = allTabs[tabId]; const state = tabStates[tabId];
  if (!tab || !state) return;
  try {
    const r = await fetch('/api/output?session=' + encodeURIComponent(tab.session) + '&window=' + tab.windowIndex);
    const d = await r.json();
    // Update sidebar status on every poll (1s latency vs 3s dashboard)
    const clean = cleanTerminal(d.output);
    const live = detectCCStatus(clean);
    if (live) updateSidebarStatus(tab.session, tab.windowIndex, live.status, live.contextPct, live.permMode);
    if (d.output !== state.last) {
      state.lastOutputChange = Date.now();
      state.last = d.output; state.rawContent = d.output;
      const outEl = document.getElementById('tab-output-' + tabId);
      if (!outEl) return;
      const atBottom = outEl.scrollHeight - outEl.scrollTop - outEl.clientHeight < 80;
      renderOutput(d.output, outEl, state, tabId);
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
  ta.value = ''; ta.style.height = 'auto'; ta.style.overflowY = 'hidden';
  const tab = allTabs[pane.activeTabId];
  const state = tabStates[pane.activeTabId];
  if (!tab || !state) return;
  state.pendingMsg = text; state.pendingTime = Date.now(); state.awaitingResponse = true;
  // Pause queue on manual send
  const qs = _queueStates[pane.activeTabId];
  if (qs && qs.playing) pauseQueue(pane.activeTabId);
  const outEl = document.getElementById('tab-output-' + pane.activeTabId);
  if (outEl) { renderOutput(state.rawContent || state.last, outEl, state, pane.activeTabId); outEl.scrollTop = outEl.scrollHeight; }
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cmd: text, session: tab.session, window: tab.windowIndex })
  });
}

async function sendGlobal() {
  const text = M.value; if (!text) return;
  const active = getActiveTab(); if (!active) return;
  M.value = ''; M.style.height = 'auto'; M.style.overflowY = 'hidden';
  active.state.pendingMsg = text; active.state.pendingTime = Date.now(); active.state.awaitingResponse = true;
  // Pause queue on manual send
  const qsg = _queueStates[active.tabId];
  if (qsg && qsg.playing) pauseQueue(active.tabId);
  const outEl = document.getElementById('tab-output-' + active.tabId);
  if (outEl) { renderOutput(active.state.rawContent || active.state.last, outEl, active.state, active.tabId); outEl.scrollTop = outEl.scrollHeight; }
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
  if (outEl) { renderOutput(active.state.rawContent || active.state.last, outEl, active.state, active.tabId); outEl.scrollTop = outEl.scrollHeight; }
}

// === Text size ===
const TEXT_SIZES = [
  { label: 'A--', text: '11px', code: '10px', mono: '10px', padV: '6px', padH: '8px', gap: '3px', radius: '10px', lineH: '1.4' },
  { label: 'A-',  text: '13px', code: '11px', mono: '11px', padV: '8px', padH: '12px', gap: '6px', radius: '14px', lineH: '1.55' },
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

// --- Sidebar resize ---
(function() {
  const handle = document.getElementById('sidebar-resize');
  const sidebar = document.getElementById('sidebar');
  let startX, startW;
  const saved = localStorage.getItem('sidebar:width');
  if (saved) document.documentElement.style.setProperty('--sidebar-w', saved + 'px');
  handle.addEventListener('pointerdown', e => {
    if (_sidebarCollapsed) return;
    e.preventDefault();
    handle.classList.add('active');
    startX = e.clientX;
    startW = sidebar.getBoundingClientRect().width;
    sidebar.style.transition = 'none';
    function onMove(ev) {
      const w = Math.max(160, Math.min(500, startW + ev.clientX - startX));
      document.documentElement.style.setProperty('--sidebar-w', w + 'px');
    }
    function onUp(ev) {
      handle.classList.remove('active');
      sidebar.style.transition = '';
      const w = Math.max(160, Math.min(500, startW + ev.clientX - startX));
      localStorage.setItem('sidebar:width', w);
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
    }
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
  });
})();

let _sidebarOrder = { sessions: [], windows: {} };
try { const so = JSON.parse(localStorage.getItem('sidebar:order')); if (so) _sidebarOrder = so; } catch(e) {}
let _sbDragging = false;

function renderSidebar() {
  if (_sbDragging) return;
  const data = _dashboardData;
  if (!data) return;
  const content = document.getElementById('sidebar-content');
  // Don't re-render while editing a memo
  if (content.querySelector('.sb-memo-edit')) return;
  // Sort sessions by custom order
  const sessions = [...data.sessions].sort((a, b) => {
    const ia = _sidebarOrder.sessions.indexOf(a.name);
    const ib = _sidebarOrder.sessions.indexOf(b.name);
    if (ia < 0 && ib < 0) return 0;
    if (ia < 0) return 1;
    if (ib < 0) return 1;
    return ia - ib;
  });
  // Determine the active window for highlighting
  const activePn = panes.find(p => p.id === activePaneId);
  const activeTab = activePn && activePn.activeTabId ? allTabs[activePn.activeTabId] : null;
  let html = '';
  for (const s of sessions) {
    html += '<div class="sb-session" draggable="true" data-session="' + esc(s.name) + '">';
    html += '<div class="sb-session-header">' + esc(s.name)
      + (s.attached ? ' <span class="sb-badge">attached</span>' : '') + '</div>';
    // Sort windows by custom order
    const winOrder = _sidebarOrder.windows[s.name] || [];
    const windows = [...s.windows].sort((a, b) => {
      const ia = winOrder.indexOf(a.index);
      const ib = winOrder.indexOf(b.index);
      if (ia < 0 && ib < 0) return 0;
      if (ia < 0) return 1;
      if (ib < 0) return 1;
      return ia - ib;
    });
    for (const w of windows) {
      const dotClass = w.is_cc ? (w.cc_status || 'idle') : 'none';
      let status = '';
      if (w.cc_context_pct != null) {
        const cls = w.cc_context_pct <= 10 ? 'critical' : w.cc_context_pct <= 25 ? 'low' : '';
        status = '<span class="sb-ctx ' + cls + '">' + w.cc_context_pct + '%</span>';
      }
      const statusClass = w.is_cc ? (w.cc_status || 'idle') : '';
      const wid = esc(s.name) + ':' + w.index;
      const isActive = activeTab && activeTab.session === s.name && activeTab.windowIndex === w.index;
      const snippet = (w.is_cc && w.cc_status === 'idle') ? extractSnippet(w.preview, true) : '';
      const memo = getMemo(s.name, w.index);
      const sEsc = esc(s.name).replace(/'/g, "\\\\'");
      const wEsc = esc(w.name).replace(/'/g, "\\\\'");
      html += '<div class="sb-win' + (isActive ? ' active' : '') + '" draggable="true" data-session="' + esc(s.name) + '" data-widx="' + w.index + '">'
        + '<div class="sb-win-dot ' + dotClass + '" data-wid="' + wid + '" onclick="event.stopPropagation();openTab(\\'' + sEsc + '\\',' + w.index + ',\\'' + wEsc + '\\')"></div>'
        + '<div class="sb-win-info" onclick="openTab(\\'' + sEsc + '\\',' + w.index + ',\\'' + wEsc + '\\')">'
        + '<div class="sb-win-name">' + esc(w.name) + '</div>'
        + '<div class="sb-win-cwd">' + esc(abbreviateCwd(w.cwd)) + '</div>'
        + (w.is_cc ? '<div class="sb-perm' + (w.cc_perm_mode && /dangerously|skip/i.test(w.cc_perm_mode) ? ' danger' : '') + '" data-wid="' + wid + '">' + (w.cc_perm_mode ? esc(w.cc_perm_mode) : '') + '</div>' : '')
        + (snippet ? '<div class="sb-snippet" title="' + esc(snippet) + '">' + esc(snippet) + '</div>' : '')
        + '<div class="sb-memo" data-wid="' + wid + '" onclick="event.stopPropagation();startMemoEdit(this,\\'' + sEsc + '\\',' + w.index + ')">' + esc(memo) + '</div>'
        + '</div>'
        + '<div class="sb-win-status ' + statusClass + '" data-wid="' + wid + '">' + status + '</div>'
        + '<button class="sb-win-detail-btn" onclick="event.stopPropagation();openWD(\\'' + sEsc + '\\',' + w.index + ')" title="Details">&#8942;</button>'
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

// === Sidebar drag reorder ===
(function() {
  const sbContent = document.getElementById('sidebar-content');
  let dragType = null, dragSession = null, dragWidx = null;

  sbContent.addEventListener('dragstart', e => {
    const win = e.target.closest('.sb-win[draggable]');
    const sess = e.target.closest('.sb-session[draggable]');
    if (win) {
      dragType = 'window';
      dragSession = win.dataset.session;
      dragWidx = parseInt(win.dataset.widx);
      win.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', 'sb-win');
    } else if (sess) {
      dragType = 'session';
      dragSession = sess.dataset.session;
      sess.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', 'sb-session');
    }
    _sbDragging = true;
  });

  sbContent.addEventListener('dragover', e => {
    e.preventDefault();
    sbContent.querySelectorAll('.sb-drag-over').forEach(el => el.classList.remove('sb-drag-over'));
    if (dragType === 'window') {
      const win = e.target.closest('.sb-win');
      if (win && win.dataset.session === dragSession) win.classList.add('sb-drag-over');
    } else if (dragType === 'session') {
      const sess = e.target.closest('.sb-session');
      if (sess && sess.dataset.session !== dragSession) sess.classList.add('sb-drag-over');
    }
  });

  sbContent.addEventListener('dragleave', e => {
    const el = e.target.closest('.sb-drag-over');
    if (el) el.classList.remove('sb-drag-over');
  });

  sbContent.addEventListener('drop', e => {
    e.preventDefault();
    sbContent.querySelectorAll('.sb-drag-over,.dragging').forEach(el => {
      el.classList.remove('sb-drag-over', 'dragging');
    });
    if (dragType === 'session') {
      const target = e.target.closest('.sb-session');
      if (target && target.dataset.session !== dragSession) {
        // Build full session order
        const allSessions = [...sbContent.querySelectorAll('.sb-session')].map(el => el.dataset.session);
        const fromIdx = allSessions.indexOf(dragSession);
        const toIdx = allSessions.indexOf(target.dataset.session);
        if (fromIdx >= 0 && toIdx >= 0) {
          allSessions.splice(fromIdx, 1);
          allSessions.splice(toIdx, 0, dragSession);
          _sidebarOrder.sessions = allSessions;
          saveSidebarOrder();
          renderSidebar();
        }
      }
    } else if (dragType === 'window') {
      const target = e.target.closest('.sb-win');
      if (target && target.dataset.session === dragSession) {
        const targetWidx = parseInt(target.dataset.widx);
        const wins = [...sbContent.querySelectorAll('.sb-win[data-session="' + CSS.escape(dragSession) + '"]')]
          .map(el => parseInt(el.dataset.widx));
        const fromIdx = wins.indexOf(dragWidx);
        const toIdx = wins.indexOf(targetWidx);
        if (fromIdx >= 0 && toIdx >= 0 && fromIdx !== toIdx) {
          wins.splice(fromIdx, 1);
          wins.splice(toIdx, 0, dragWidx);
          _sidebarOrder.windows[dragSession] = wins;
          saveSidebarOrder();
          renderSidebar();
        }
      }
    }
    _sbDragging = false;
    dragType = null; dragSession = null; dragWidx = null;
  });

  sbContent.addEventListener('dragend', () => {
    sbContent.querySelectorAll('.sb-drag-over,.dragging').forEach(el => {
      el.classList.remove('sb-drag-over', 'dragging');
    });
    _sbDragging = false;
    dragType = null; dragSession = null; dragWidx = null;
  });
})();

function saveSidebarOrder() {
  try { localStorage.setItem('sidebar:order', JSON.stringify(_sidebarOrder)); } catch(e) {}
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
  if (win.is_cc) {
    html += '<div class="wd-row"><span class="wd-label">Status</span><span class="wd-value">' + statusLabel(win.cc_status) + '</span></div>';
    if (win.cc_perm_mode) {
      const isDanger = /dangerously|skip/i.test(win.cc_perm_mode);
      html += '<div class="wd-row"><span class="wd-label">Permissions</span><span class="wd-value' + (isDanger ? '" style="color:var(--red);font-weight:600' : '') + '">' + esc(win.cc_perm_mode) + '</span></div>';
    }
    const pct = win.cc_context_pct;
    const ctxLabel = pct != null ? pct + '%' : 'Healthy';
    const barColor = pct != null && pct <= 10 ? 'var(--red)' : pct != null && pct <= 25 ? 'var(--orange)' : 'var(--green)';
    const barWidth = pct != null ? pct : 100;
    html += '<div class="wd-row"><span class="wd-label">Context</span><span class="wd-value">'
      + '<span style="margin-right:8px">' + ctxLabel + '</span>'
      + '<span style="display:inline-block;width:80px;height:6px;background:var(--surface);border-radius:3px;vertical-align:middle">'
      + '<span style="display:block;width:' + barWidth + '%;height:100%;background:' + barColor + ';border-radius:3px"></span>'
      + '</span></span></div>';
  }
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
    body: JSON.stringify({name: name, session: _wdSession})
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
  fetch('/api/windows/' + _wdWindow + '?session=' + encodeURIComponent(_wdSession), {method:'DELETE'}).then(() => {
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
  const max = window.innerHeight * 0.4;
  M.style.height = 'auto';
  M.style.height = Math.min(M.scrollHeight, max) + 'px';
  M.style.overflowY = M.scrollHeight > max ? 'auto' : 'hidden';
}
M.addEventListener('input', autoResize);
M.addEventListener('paste', () => setTimeout(autoResize, 0));
M.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (M.value.trim()) sendGlobal(); else keyActive('Enter'); }
  if (!M.value && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); keyActive(e.key === 'ArrowUp' ? 'Up' : 'Down'); }
  if (!M.value && e.key === 'Escape') { e.preventDefault(); keyActive('Escape'); }
  if (!M.value && e.key === 'Tab') { e.preventDefault(); keyActive('Tab'); }
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
function windowExists(session, windowIndex) {
  if (!_dashboardData) return false;
  const sess = _dashboardData.sessions.find(s => s.name === session);
  if (!sess) return false;
  return sess.windows.some(w => w.index === windowIndex);
}

function restorePaneTabs(paneId, paneData) {
  for (const t of paneData.tabIds) {
    if (windowExists(t.session, t.windowIndex)) {
      createTab(t.session, t.windowIndex, t.windowName, paneId);
    }
  }
  if (paneData.activeTab) {
    const pane = panes.find(p => p.id === paneId);
    if (pane) {
      for (const tid of pane.tabIds) {
        const tab = allTabs[tid];
        if (tab && tab.session === paneData.activeTab.session
            && tab.windowIndex === paneData.activeTab.windowIndex) {
          focusTab(tid);
          break;
        }
      }
    }
  }
}
function restoreLayout() {
  _restoringLayout = true;
  try {
    const saved = JSON.parse(localStorage.getItem('layout'));
    if (!saved || !saved.length) { _restoringLayout = false; return false; }
    // Validate: check both flat panes and stacked panes
    function flatPanes(layout) {
      const out = [];
      for (const item of layout) {
        if (item.stack) out.push(...item.stack);
        else out.push(item);
      }
      return out;
    }
    const allPaneData = flatPanes(saved);
    const anyValid = allPaneData.some(p => p.tabIds && p.tabIds.some(t => windowExists(t.session, t.windowIndex)));
    if (!anyValid) { _restoringLayout = false; return false; }
    for (const item of saved) {
      if (item.stack) {
        // Create a vertical stack
        const stack = document.createElement('div');
        stack.className = 'pane-stack';
        panesContainer.appendChild(stack);
        for (const pd of item.stack) {
          const paneId = createPane(stack);
          if (!paneId) break;
          restorePaneTabs(paneId, pd);
        }
      } else {
        const paneId = createPane();
        if (!paneId) break;
        restorePaneTabs(paneId, item);
      }
    }
    _restoringLayout = false;
    return panes.some(p => p.tabIds.length > 0);
  } catch(e) { _restoringLayout = false; return false; }
}

async function init() {
  await loadDashboard();
  if (!restoreLayout()) {
    createPane();
    if (_dashboardData && _dashboardData.sessions.length > 0) {
      const sess = _dashboardData.sessions[0];
      const activeWin = sess.windows.find(w => w.active) || sess.windows[0];
      if (activeWin) createTab(sess.name, activeWin.index, activeWin.name);
    }
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
    session = body.get("session", _current_session)
    if name:
        target = f"{session}:{index}"
        subprocess.run(["tmux", "rename-window", "-t", target, name])
        subprocess.run(["tmux", "set-window-option", "-t", target, "allow-rename", "off"])
        subprocess.run(["tmux", "set-window-option", "-t", target, "automatic-rename", "off"])
    return JSONResponse({"ok": True})


@app.delete("/api/windows/{index}")
async def api_close_window(index: int, session: str = None):
    sess = session or _current_session
    subprocess.run(["tmux", "kill-window", "-t", f"{sess}:{index}"])
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
