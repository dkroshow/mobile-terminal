#!/usr/bin/env python3
"""Mobile terminal for Big Mac — simple form-based version."""
import re
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI()
SESSION = "mobile"
WORK_DIR = str(Path.home() / "Code" / "galaxy")

ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'
    r'|\x1b\][^\x07]*\x07'
    r'|\x1b\([A-Z]'
    r'|\x1b[>=]'
    r'|\x0f'
)


def ensure_session():
    r = subprocess.run(["tmux", "has-session", "-t", SESSION], capture_output=True)
    if r.returncode != 0:
        subprocess.run([
            "tmux", "new-session", "-d", "-s", SESSION,
            "-x", "80", "-y", "50", "-c", WORK_DIR,
        ])


def send_keys(text: str):
    subprocess.run(["tmux", "send-keys", "-t", SESSION, "-l", text])
    subprocess.run(["tmux", "send-keys", "-t", SESSION, "Enter"])


def send_special(key: str):
    subprocess.run(["tmux", "send-keys", "-t", SESSION, key])


def get_output() -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", SESSION, "-p", "-S", "-200"],
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


def list_windows() -> list:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", SESSION, "-F", "#{window_index} #{window_name} #{window_active}"],
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
    subprocess.run(["tmux", "new-window", "-t", SESSION, "-c", WORK_DIR])


def select_window(index: int):
    subprocess.run(["tmux", "select-window", "-t", f"{SESSION}:{index}"])


HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Big Mac</title>
<style>
body { margin:0; padding:0; background:#0d1117; color:#c9d1d9;
  font-family:-apple-system,system-ui,sans-serif; }
#out { padding:12px; font-family:Menlo,monospace; font-size:13px;
  line-height:1.5; white-space:pre-wrap; word-break:break-word;
  max-height:60vh; overflow-y:auto; }
#bar { padding:8px 12px; background:#161b22; border-top:1px solid #30363d;
  position:fixed; bottom:0; left:0; right:0; }
input[type=text] { width:100%; background:#0d1117; color:#c9d1d9;
  border:1px solid #30363d; border-radius:12px; padding:10px;
  font-size:16px; font-family:-apple-system,sans-serif;
  box-sizing:border-box; }
.btns { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
button { background:#21262d; color:#c9d1d9; border:1px solid #30363d;
  border-radius:8px; padding:8px 14px; font-size:14px; cursor:pointer; }
.send { background:#238636; border-color:#238636; color:#fff; }
.danger { color:#f85149; border-color:#f85149; }
</style>
</head>
<body>
<div id="out">Loading...</div>
<div id="bar">
  <input id="msg" type="text" placeholder="Type here..."
    autocorrect="on" autocapitalize="sentences" enterkeyhint="send">
  <div class="btns">
    <button class="send" onclick="send()">Send</button>
    <button onclick="showCmds()">Commands</button>
    <button onclick="showWins()">Windows</button>
  </div>
  <div id="cmds" style="display:none; margin-top:6px;">
    <div class="btns">
      <button class="danger" onclick="key('C-c')">^C</button>
      <button onclick="key('Up')">Up</button>
      <button onclick="key('Down')">Down</button>
      <button onclick="key('Tab')">Tab</button>
      <button onclick="key('Escape')">Esc</button>
      <button onclick="txt('claude')">claude</button>
      <button onclick="txt('/exit')">/exit</button>
    </div>
  </div>
  <div id="wins" class="btns" style="display:none; margin-top:6px;"></div>
</div>
<script>
const O = document.getElementById('out');
const M = document.getElementById('msg');
let last = '';

// Poll for output every second
setInterval(async () => {
  try {
    const r = await fetch('/api/output');
    const d = await r.json();
    if (d.output !== last) {
      last = d.output;
      O.textContent = d.output;
      O.scrollTop = O.scrollHeight;
    }
  } catch(e) {}
}, 1000);

async function send() {
  const t = M.value;
  if (!t) return;
  M.value = '';
  await fetch('/api/send', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: t})
  });
}

async function txt(t) {
  await fetch('/api/send', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: t})
  });
}

async function key(k) {
  await fetch('/api/key/' + k);
}

function showCmds() {
  const c = document.getElementById('cmds');
  c.style.display = c.style.display === 'none' ? 'block' : 'none';
  document.getElementById('wins').style.display = 'none';
}

async function showWins() {
  const w = document.getElementById('wins');
  const show = w.style.display === 'none';
  document.getElementById('cmds').style.display = 'none';
  w.style.display = show ? 'flex' : 'none';
  if (show) loadWindows();
}

async function loadWindows() {
  const r = await fetch('/api/windows');
  const d = await r.json();
  const el = document.getElementById('wins');
  el.innerHTML = d.windows.map(w =>
    '<button style="' + (w.active ? 'border-color:#58a6ff;color:#58a6ff;' : '') + '"'
    + ' onclick="switchWin(' + w.index + ')">' + w.index + ':' + w.name + '</button>'
    + (d.windows.length > 1 ? '<button class="danger" style="padding:8px 10px;" onclick="closeWin(' + w.index + ')">x</button>' : '')
  ).join('') + '<button onclick="newWin()">+ New</button>';
}

async function switchWin(i) {
  await fetch('/api/windows/' + i, {method:'POST'});
  last = '';
  loadWindows();
}

async function newWin() {
  await fetch('/api/windows/new', {method:'POST'});
  last = '';
  loadWindows();
}

async function closeWin(i) {
  if (confirm('Close window ' + i + '?')) {
    await fetch('/api/windows/' + i, {method:'DELETE'});
    last = '';
    loadWindows();
  }
}

M.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); send(); }
});

// Keep input bar glued above iOS keyboard
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', () => {
    const bar = document.getElementById('bar');
    const offset = window.innerHeight - window.visualViewport.height;
    bar.style.bottom = offset + 'px';
  });
  window.visualViewport.addEventListener('scroll', () => {
    const bar = document.getElementById('bar');
    const offset = window.innerHeight - window.visualViewport.height;
    bar.style.bottom = offset + 'px';
  });
}
</script>
</body>
</html>"""


@app.get("/")
async def index():
    ensure_session()
    return HTMLResponse(HTML, headers={"Cache-Control": "no-store"})


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


@app.delete("/api/windows/{index}")
async def api_close_window(index: int):
    subprocess.run(["tmux", "kill-window", "-t", f"{SESSION}:{index}"])
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7681)
