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


def send_keys(text: str):
    subprocess.run(["tmux", "send-keys", "-t", _current_session, "-l", text])
    subprocess.run(["tmux", "send-keys", "-t", _current_session, "Enter"])


def send_special(key: str):
    subprocess.run(["tmux", "send-keys", "-t", _current_session, key])


def get_output() -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", _current_session, "-p", "-S", "-200"],
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
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-bottom: env(safe-area-inset-bottom, 0px);
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;
  overflow:hidden; -webkit-font-smoothing:antialiased; }

/* --- Top bar --- */
#topbar { position:fixed; top:0; left:0; right:0; z-index:10;
  background:var(--bg); padding:calc(var(--safe-top) + 6px) 16px 10px;
  display:flex; align-items:center; gap:10px; }
#tmux-btn { height:36px; padding:0 14px; border-radius:18px;
  background:var(--surface); color:var(--text); border:1px solid var(--border);
  font-size:13px; font-weight:600; font-family:inherit; cursor:pointer;
  display:flex; align-items:center; gap:6px;
  transition:all .15s; -webkit-user-select:none; user-select:none; }
#tmux-btn.on { background:var(--accent); color:#fff; border-color:var(--accent); }
#tmux-btn .arrow { font-size:10px; opacity:0.6; transition:transform .2s; }
#tmux-btn.on .arrow { transform:rotate(180deg); }
#view-btn { height:36px; padding:0 14px; border-radius:18px;
  background:var(--surface); color:var(--text2); border:1px solid var(--border);
  font-size:13px; font-weight:500; font-family:inherit; cursor:pointer;
  transition:all .15s; -webkit-user-select:none; user-select:none; }
#view-btn:active { transform:scale(0.96); opacity:0.8; }
#top-title { color:var(--text2); font-size:12px; font-weight:500;
  letter-spacing:0.3px; text-transform:uppercase; flex:1; text-align:right; }

/* --- tmux panel --- */
#tmux-panel { position:fixed; top:0; left:0; right:0; z-index:9;
  background:var(--bg2); border-bottom:1px solid var(--border2);
  max-height:0; overflow:hidden; overflow-y:auto;
  transition:max-height .25s ease, padding .25s ease;
  padding:0 16px; }
#tmux-panel.open { max-height:60vh; padding:10px 16px 14px;
  padding-top:calc(var(--safe-top) + 52px); }
.tmux-session { margin-bottom:8px; }
.tmux-session:last-child { margin-bottom:0; }
.tmux-session-header { display:flex; align-items:center; gap:8px; padding:8px 0 4px;
  color:var(--text2); font-size:12px; font-weight:600; text-transform:uppercase;
  letter-spacing:0.5px; }
.tmux-session-header.current { color:var(--accent); }
.tmux-session-header .badge { font-size:10px; padding:1px 6px; border-radius:8px;
  background:var(--accent); color:#fff; font-weight:500; text-transform:none;
  letter-spacing:0; }
.tmux-windows { display:flex; flex-wrap:wrap; gap:6px; padding:4px 0; }
.tmux-win { height:32px; padding:0 12px; border-radius:16px;
  background:var(--surface); color:var(--text2); border:1px solid var(--border);
  font-size:12px; font-weight:500; font-family:inherit; cursor:pointer;
  display:flex; align-items:center; gap:4px;
  transition:all .15s; -webkit-user-select:none; user-select:none; }
.tmux-win:active { transform:scale(0.96); opacity:0.8; }
.tmux-win.active { background:var(--accent); color:#fff; border-color:var(--accent); }
.tmux-win .win-idx { opacity:0.5; }

/* --- Output area --- */
#out { position:absolute; left:0; right:0; overflow-y:auto;
  -webkit-overflow-scrolling:touch; }

/* Raw mode */
#out.raw { padding:20px 16px;
  font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace;
  font-size:13px; line-height:1.6; white-space:pre-wrap;
  word-break:break-word; color:#999; }

/* Chat mode — always the default */
#out.chat { display:flex; flex-direction:column; padding:12px 16px 24px; }

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
  max-width:85%; margin-left:auto; font-size:15.5px;
  line-height:1.55; word-break:break-word; }

/* --- Assistant card --- */
.turn.assistant .turn-body { background:var(--surface);
  padding:16px 18px; border-radius:4px 18px 18px 18px;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;
  font-size:16px; line-height:1.8; word-break:break-word; color:var(--text); }

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
  font-size:13px; line-height:1.55; color:#b0b0b0; }
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
  font-size:13px; line-height:1.6; white-space:pre-wrap; word-break:break-word;
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
#bar { position:fixed; left:0; right:0; bottom:0; z-index:10;
  background:var(--bg2);
  padding:12px 14px calc(var(--safe-bottom) + 12px);
  transition:bottom .1s; }
#input-row { display:flex; gap:10px; align-items:flex-end; }
#msg { flex:1; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:22px; padding:11px 18px;
  font-size:16px; font-family:inherit; outline:none;
  transition:border-color .2s, box-shadow .2s; }
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
</style>
</head>
<body>

<div id="topbar">
  <button id="tmux-btn" onclick="toggleTmux()">
    <span>tmux</span>
    <span class="arrow">&#9660;</span>
  </button>
  <button id="view-btn" onclick="toggleRaw()">
    <span id="view-label">Clean</span>
  </button>
  <span id="top-title">__TITLE__</span>
</div>
<div id="tmux-panel"></div>

<div id="out" class="chat">
  <div class="turn assistant"><div class="turn-label">Terminal</div>
  <div class="turn-body"><p style="color:var(--text3)">Connecting...</p></div></div>
</div>

<div id="bar">
  <div id="input-row">
    <input id="msg" type="text" placeholder="Enter command..."
      autocorrect="off" autocapitalize="none" autocomplete="off"
      spellcheck="false" enterkeyhint="send">
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

<script src="https://cdn.jsdelivr.net/npm/marked/lib/marked.umd.min.js"></script>
<script>
const O = document.getElementById('out');
const M = document.getElementById('msg');
const bar = document.getElementById('bar');
const topbar = document.getElementById('topbar');
let rawMode = false, rawContent = '', last = '';
let pendingMsg = null, pendingTime = 0;
let awaitingResponse = false;
let lastOutputChange = 0;

function layout() {
  O.style.top = topbar.offsetHeight + 'px';
  O.style.bottom = bar.offsetHeight + 'px';
}
function isNearBottom() {
  return O.scrollHeight - O.scrollTop - O.clientHeight < 80;
}

// --- Clean raw terminal output ---
function cleanTerminal(raw) {
  let lines = raw.split('\\n');
  lines = lines.filter(l => !/^\\s*[\\u256d\\u2570][\\u2500\\u2504\\u2501]+[\\u256e\\u256f]\\s*$/.test(l));
  lines = lines.map(l => l.replace(/^\\s*\\u2502\\s?/, '').replace(/\\s?\\u2502\\s*$/, ''));
  let text = lines.join('\\n');
  text = text.replace(/[\\u280b\\u2819\\u2839\\u2838\\u283c\\u2834\\u2826\\u2827\\u2807\\u280f]/g, '');
  text = text.replace(/\\n{3,}/g, '\\n\\n');
  return text.trim();
}

// --- Detect Claude Code output ---
function isClaudeCode(text) {
  return /\\u276f/.test(text) && /\\u23fa/.test(text);
}

// --- Detect if Claude Code is idle (done processing) ---
function isIdle(text) {
  const tail = text.split('\\n').slice(-10).join('\\n');
  // If there are active processing signals, not idle
  if (/esc to interrupt/.test(tail)) return false;
  if (/^\\u00b7\\s+\\w/m.test(tail)) return false;
  // No processing signals — Claude is idle
  return true;
}

// --- Parse Claude Code session into turns ---
function parseCCTurns(text) {
  const lines = text.split('\\n');
  const turns = [];
  let cur = null;
  let inTool = false;
  let sawStatus = false;

  for (const line of lines) {
    const raw = line.replace(/\\u00a0/g, ' ');
    const t = raw.trim();
    // Skip noise
    if (/^[\\u2500\\u2501\\u2504\\u2508\\u2550]{3,}$/.test(t)) continue;
    if (/^[\\u23f5]/.test(t)) continue;
    if (/^\\u2026/.test(t)) continue;
    // Blank line: preserve as paragraph break in assistant text
    if (!t) {
      if (cur && cur.role === 'assistant' && !inTool) cur.lines.push('');
      continue;
    }
    // Status lines (✻✳✹✽ etc.) — skip but track them
    if (/^[\\u2730-\\u273f]/.test(t)) { sawStatus = true; continue; }
    // Extended thinking indicator (· Sketching…, · Thinking…) — skip
    if (/^\\u00b7\\s+\\w/.test(t)) { sawStatus = true; continue; }
    // Status bar line (esc to interrupt, bypass permissions, etc.) — skip
    if (/esc to interrupt/.test(t)) continue;

    // User prompt: ❯ at column 0 only (not indented examples in assistant text)
    if (/^\\u276f/.test(raw)) {
      if (cur) turns.push(cur);
      const msg = t.replace(/^\\u276f\\s*/, '').trim();
      cur = { role: 'user', lines: msg ? [msg] : [] };
      inTool = false;
      sawStatus = false;
      continue;
    }

    // ⏺ marker: text or tool call
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
      cur.lines.push(after);
      continue;
    }

    // ⎿ tool output
    if (/^\\u23bf/.test(t)) { inTool = true; continue; }

    // Skip tool output lines
    if (inTool) continue;

    // Continuation of assistant text only
    if (cur && cur.role === 'assistant' && !inTool) {
      cur.lines.push(t);
    }
  }
  if (cur) turns.push(cur);
  const filtered = turns.filter(t => t.lines.some(l => l.trim()));
  // Remove trailing idle prompt (❯ with ghost suggestion text, no active processing)
  if (filtered.length > 0 && filtered[filtered.length - 1].role === 'user') {
    if (!sawStatus && isIdle(text)) {
      filtered.pop();
    }
  }
  return filtered;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s; return d.innerHTML;
}

function md(s) {
  return typeof marked !== 'undefined' ? marked.parse(s) : '<p>' + esc(s) + '</p>';
}

// --- Render output ---
function renderOutput(raw) {
  if (rawMode) {
    O.className = 'raw';
    O.textContent = raw;
    layout();
    return;
  }

  O.className = 'chat';
  const clean = cleanTerminal(raw);
  let html = '';

  if (isClaudeCode(clean)) {
    const turns = parseCCTurns(clean);

    // Check if parser caught up with our pending sent message
    if (pendingMsg) {
      const userTurns = turns.filter(t => t.role === 'user');
      const lastUser = userTurns[userTurns.length - 1];
      if (lastUser && lastUser.lines.join(' ').includes(pendingMsg.substring(0, 20))) {
        pendingMsg = null;  // Parser has our message, stop injecting it
      }
    }
    // Check if Claude is done working
    if (awaitingResponse) {
      const elapsed = Date.now() - pendingTime;
      // Clear when Claude is idle and enough time has passed
      if (elapsed > 3000 && isIdle(clean)) {
        awaitingResponse = false;
      }
      // Clear when output hasn't changed for 5s (waiting for user input, e.g. menu)
      if (elapsed > 3000 && lastOutputChange > 0 && (Date.now() - lastOutputChange) > 5000) {
        awaitingResponse = false;
      }
      // Safety timeout: 3 minutes
      if (elapsed > 180000) awaitingResponse = false;
    }

    for (const t of turns) {
      const text = t.lines.join('\\n').trim();
      if (!text) continue;
      if (t.role === 'user') {
        html += '<div class="turn user">'
          + '<div class="turn-label">You</div>'
          + '<div class="turn-body">' + esc(text) + '</div></div>';
      } else {
        html += '<div class="turn assistant">'
          + '<div class="turn-label">Claude</div>'
          + '<div class="turn-body">' + md(text) + '</div></div>';
      }
    }

    // Inject sent message if parser hasn't caught up yet
    if (pendingMsg) {
      html += '<div class="turn user">'
        + '<div class="turn-label">You</div>'
        + '<div class="turn-body">' + esc(pendingMsg) + '</div></div>';
    }
    // Show thinking indicator while awaiting response
    if (awaitingResponse) {
      html += '<div class="turn assistant">'
        + '<div class="turn-label">Claude</div>'
        + '<div class="turn-body"><p class="thinking">Working\\u2026</p></div></div>';
    }
  } else {
    // Plain terminal — show as monospace
    if (clean.trim()) {
      html = '<div class="turn assistant"><div class="turn-label">Terminal</div>'
        + '<div class="turn-body mono">' + esc(clean) + '</div></div>';
    }
  }

  if (!html) {
    html = '<div class="turn assistant">'
      + '<div class="turn-label">Terminal</div>'
      + '<div class="turn-body"><p style="color:var(--text3)">Waiting for output...</p></div></div>';
  }

  O.innerHTML = html;
  layout();
}

function toggleRaw() {
  rawMode = !rawMode;
  document.getElementById('view-label').textContent = rawMode ? 'Raw' : 'Clean';
  renderOutput(rawContent || last);
  O.scrollTop = O.scrollHeight;
}

// --- Polling ---
setInterval(async () => {
  try {
    const r = await fetch('/api/output');
    const d = await r.json();
    if (d.output !== last) {
      lastOutputChange = Date.now();
      const atBottom = isNearBottom();
      last = d.output; rawContent = d.output;
      renderOutput(d.output);
      if (atBottom) O.scrollTop = O.scrollHeight;
    }
  } catch(e) {}
}, 1000);

// --- Send ---
async function send() {
  const t = M.value; if (!t) return;
  M.value = '';
  pendingMsg = t;
  pendingTime = Date.now();
  awaitingResponse = true;
  renderOutput(rawContent || last);
  O.scrollTop = O.scrollHeight;
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: t})
  });
}
async function key(k) { await fetch('/api/key/' + k); }

// --- Keys tray ---
// --- Prefill input (user must press Enter to send) ---
function prefill(text) {
  M.value = text;
  M.focus();
}

async function sendResume() {
  // Switch to raw view so user can see the interactive menu
  if (!rawMode) {
    rawMode = true;
    document.getElementById('view-label').textContent = 'Raw';
  }
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: '/resume'})
  });
}

function toggleKeys() {
  const on = document.getElementById('keys').classList.toggle('open');
  document.getElementById('keysBtn').classList.toggle('on', on);
  if (on) { document.getElementById('cmds').classList.remove('open'); document.getElementById('commandsBtn').classList.remove('on'); }
  requestAnimationFrame(layout);
  setTimeout(layout, 300);
}
function toggleCmds() {
  const on = document.getElementById('cmds').classList.toggle('open');
  document.getElementById('commandsBtn').classList.toggle('on', on);
  if (on) { document.getElementById('keys').classList.remove('open'); document.getElementById('keysBtn').classList.remove('on'); }
  requestAnimationFrame(layout);
  setTimeout(layout, 300);
}
function renameCurrentWin() {
  const name = prompt('Rename window:');
  if (name === null || name.trim() === '') return;
  fetch('/api/windows/current', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name.trim()})
  }).then(() => loadSessions());
}

// --- Send a preset command ---
async function sendCmd(cmd) {
  pendingMsg = cmd;
  pendingTime = Date.now();
  awaitingResponse = true;
  renderOutput(rawContent || last);
  O.scrollTop = O.scrollHeight;
  await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: cmd})
  });
}

// --- tmux navigation ---
let _tmuxOpen = false;

function toggleTmux() {
  _tmuxOpen = !_tmuxOpen;
  document.getElementById('tmux-btn').classList.toggle('on', _tmuxOpen);
  document.getElementById('tmux-panel').classList.toggle('open', _tmuxOpen);
  if (_tmuxOpen) loadSessions();
  requestAnimationFrame(layout);
  setTimeout(layout, 300);
}

async function loadSessions() {
  const r = await fetch('/api/sessions');
  const d = await r.json();
  const panel = document.getElementById('tmux-panel');
  // Current session shown in panel via "active" badge
  let html = '';
  for (const s of d.sessions) {
    const isCurrent = s.name === d.current;
    html += '<div class="tmux-session">';
    html += '<div class="tmux-session-header' + (isCurrent ? ' current' : '') + '">'
      + esc(s.name)
      + (isCurrent ? ' <span class="badge">active</span>' : '')
      + '</div>';
    html += '<div class="tmux-windows">';
    for (const w of s.windows) {
      const active = isCurrent && w.active;
      html += '<button class="tmux-win' + (active ? ' active' : '') + '"'
        + ' onclick="switchSession(\\'' + esc(s.name) + '\\', ' + w.index + ')">'
        + '<span class="win-idx">' + w.index + ':</span> ' + esc(w.name)
        + '</button>';
    }
    if (isCurrent) {
      html += '<button class="tmux-win" onclick="newWin()"'
        + ' style="color:var(--text3);border-style:dashed">+ new</button>';
    }
    html += '</div></div>';
  }
  panel.innerHTML = html;
}

async function switchSession(name, winIndex) {
  await fetch('/api/sessions/' + encodeURIComponent(name), {method:'POST'});
  if (winIndex !== undefined) {
    await fetch('/api/windows/' + winIndex, {method:'POST'});
  }
  last = '';
  toggleTmux();
  loadSessions();
}

async function newWin() {
  await fetch('/api/windows/new', {method:'POST'});
  last = ''; loadSessions();
}

// --- Input ---
M.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); send(); }
});

// --- iOS keyboard ---
if (window.visualViewport) {
  const adjust = () => {
    bar.style.bottom = (window.innerHeight - window.visualViewport.height) + 'px';
    layout();
  };
  window.visualViewport.addEventListener('resize', adjust);
  window.visualViewport.addEventListener('scroll', adjust);
}

// --- Init ---
loadSessions();
requestAnimationFrame(layout);
new ResizeObserver(layout).observe(bar);
new ResizeObserver(layout).observe(topbar);
</script>
</body>
</html>"""


@app.get("/")
async def index():
    ensure_session()
    html = HTML.replace("__TITLE__", TITLE)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/output")
async def api_output():
    return JSONResponse({"output": get_output()})


@app.post("/api/send")
async def api_send(body: dict):
    cmd = body.get("cmd", "")
    if cmd:
        send_keys(cmd)
    return JSONResponse({"ok": True})


@app.get("/api/key/{key}")
async def api_key(key: str):
    ALLOWED = {"C-c", "C-d", "C-l", "C-z", "Up", "Down", "Tab", "Enter", "Escape"}
    if key in ALLOWED:
        send_special(key)
    return JSONResponse({"ok": True})


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
