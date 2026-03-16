#!/usr/bin/env python3
"""Mobile web terminal for remote tmux control."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import asyncio
import urllib.request
from datetime import datetime
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
# Track last user interaction per window (key: "session:window" → epoch timestamp)
# Used instead of tmux window_activity for CC sessions (whose TUI refreshes constantly)
_last_interaction = {}
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
_notify_pending = {}   # "session:window" → {"window_name": str, "saw_busy": False}
_notify_sent = {}      # "session:window" → epoch (dedup window)

# Context gauge — JSONL-based context window utilization
# Inline implementation (no subprocess) — reads ~/.claude/projects/*/JSONL directly.
# Matching: front-loaded at session start. Once matched, locked for life of Claude PID.
# Easy case: single new instance → most recent JSONL. Hard case: text matching via api_send.
_gauge_cache = {}      # "session:window" → metrics dict
_gauge_cache_time = 0  # epoch of last refresh
_gauge_locks = {}      # "session:window" → {"stem", "pid", "path"} — permanent matches
_gauge_sent = {}       # "session:window" → [str] — recent texts sent via api_send (for matching)
GAUGE_CACHE_TTL = 30   # seconds
GAUGE_MATCH_TTL = 5    # seconds — faster poll while unmatched windows exist
GAUGE_THRESHOLD_200K = 170_000  # auto-compact ceiling for 200k context (~168k max observed)
GAUGE_THRESHOLD_1M = 1_000_000  # 1M context window (auto-compact ceiling TBD — using full window for now)
_GAUGE_LOCKS_FILE = Path.home() / ".mobile-terminal-gauge-locks.json"


def _gauge_save_locks():
    """Persist gauge locks to disk (atomic write)."""
    try:
        tmp = str(_GAUGE_LOCKS_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_gauge_locks, f)
        os.replace(tmp, _GAUGE_LOCKS_FILE)
    except Exception:
        pass


def _gauge_load_locks():
    """Load gauge locks from disk on startup."""
    global _gauge_locks
    try:
        with open(_GAUGE_LOCKS_FILE) as f:
            _gauge_locks = json.load(f)
    except Exception:
        _gauge_locks = {}


_gauge_load_locks()


def _gauge_extract_usage(jsonl_path: str) -> tuple:
    """Parse JSONL transcript, return (usage_list, last_message_ts_epoch, model).
    usage_list: [{total_input, output, timestamp}] from assistant turns.
    last_message_ts_epoch: epoch seconds of last user/assistant message (for activity age).
    model: model name string from the most recent assistant message (or None).
    """
    usage = []
    last_ts_str = None
    model = None
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            ts = entry.get("timestamp")
            if etype in ("user", "assistant") and ts:
                last_ts_str = ts
            if etype != "assistant":
                continue
            msg = entry.get("message", {})
            if msg.get("model"):
                model = msg["model"]
            u = msg.get("usage", {})
            if not u:
                continue
            total = (u.get("input_tokens", 0) or 0) + \
                    (u.get("cache_read_input_tokens", 0) or 0) + \
                    (u.get("cache_creation_input_tokens", 0) or 0)
            if total == 0:
                continue
            usage.append({"total_input": total, "output": u.get("output_tokens", 0) or 0,
                          "timestamp": ts})
    # Convert last ISO timestamp to epoch
    last_epoch = None
    if last_ts_str:
        try:
            dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
            last_epoch = int(dt.timestamp())
        except Exception:
            pass
    return usage, last_epoch, model


def _gauge_threshold_for_model(model: str) -> int:
    """Return the appropriate gauge threshold based on model context window size."""
    if not model:
        return GAUGE_THRESHOLD_200K
    m = model.lower()
    # Claude 4.6 models (opus-4-6, sonnet-4-6) have 1M context
    if "4-6" in m or "4.6" in m or "1m" in m:
        return GAUGE_THRESHOLD_1M
    return GAUGE_THRESHOLD_200K


def _gauge_compute(usage: list, last_ts: int = None, threshold: int = GAUGE_THRESHOLD_200K) -> dict:
    """Compute context gauge metrics from usage data."""
    if not usage:
        return None
    current = usage[-1]["total_input"]
    remaining = max(0, threshold - current)
    pct = current / threshold * 100
    burn = 0
    recent = usage[-10:]
    if len(recent) >= 2:
        burn = (recent[-1]["total_input"] - recent[0]["total_input"]) / (len(recent) - 1)
    est = int(remaining / burn) if burn > 0 else None
    compressed = any(usage[i]["total_input"] < usage[i-1]["total_input"] - 1000
                     for i in range(1, len(usage)))
    return {"current_size": current, "threshold": threshold, "remaining": remaining,
            "pct_used": round(pct, 1), "burn_rate": round(burn),
            "est_turns_remaining": est, "total_turns": len(usage),
            "compression_detected": compressed, "last_ts": last_ts}


def _gauge_jsonl_texts(jsonl_path):
    """Extract user and assistant message texts from a JSONL file for matching."""
    texts = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") not in ("user", "assistant"):
                    continue
                content = entry.get("message", {}).get("content", "")
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            texts.append(block.get("text", ""))
    except Exception:
        pass
    return texts


def _gauge_score_text_match(needles, jsonl_path):
    """Score how many needle texts appear in JSONL messages. Higher = better match."""
    jsonl_texts = _gauge_jsonl_texts(jsonl_path)
    score = 0
    for needle in needles:
        if len(needle) < 8:
            continue  # skip very short texts — not distinctive enough
        for jt in jsonl_texts:
            if needle in jt:
                score += 1
                break  # count each needle only once
    return score


def _gauge_extract_tmux_texts(session, window):
    """Extract distinctive text from tmux capture for bootstrap matching.
    Grabs user prompts, assistant content, and concatenates nearby lines
    into longer chunks for more distinctive matching."""
    try:
        r = _run(["tmux", "capture-pane", "-t", f"{session}:{window}", "-p", "-S", "-200"],
                 capture_output=True, text=True)
        if not r.stdout:
            return []
        # Clean lines
        cleaned = []
        for line in r.stdout.split("\n"):
            text = line.replace("\xa0", " ").strip()
            if not text:
                cleaned.append("")
                continue
            # Skip box-drawing / divider lines
            if all(c in "\u2500\u2501\u2550\u2502\u2503\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c\u256d\u256e\u2570\u256f\u2571\u2572 \u25aa" for c in text):
                cleaned.append("")
                continue
            # Skip CC status bar
            if text.startswith("\u23f5"):
                cleaned.append("")
                continue
            # Strip leading ❯ or ⏺
            for prefix in ("\u276f", "\u23fa"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            cleaned.append(text)
        # Build chunks: join consecutive non-empty lines into paragraphs
        texts = []
        seen = set()
        chunk = []
        for line in cleaned:
            if line:
                chunk.append(line)
            else:
                if chunk:
                    joined = " ".join(chunk)
                    if len(joined) >= 20 and joined not in seen:
                        seen.add(joined)
                        texts.append(joined)
                    chunk = []
        if chunk:
            joined = " ".join(chunk)
            if len(joined) >= 20 and joined not in seen:
                texts.append(joined)
        return texts
    except Exception:
        return []


def _gauge_cache_metrics(cache, key, path, session_id, matched_type):
    """Extract usage from JSONL and compute gauge metrics into cache."""
    usage, last_ts, model = _gauge_extract_usage(path)
    threshold = _gauge_threshold_for_model(model)
    metrics = _gauge_compute(usage, last_ts=last_ts, threshold=threshold)
    if metrics:
        metrics["session_id"] = session_id
        metrics["matched"] = matched_type
        cache[key] = metrics


def _refresh_gauge_cache():
    """Build per-window gauge metrics from locked JSONL matches.

    Matching pipeline (front-loaded, runs until all windows locked):
    1. tmux list-panes → shell PID per window
    2. ps process tree → find Claude child of each shell
    3. Locked windows: just refresh metrics from their JSONL
    4. Unlocked windows: attempt match:
       - Easy: only 1 unmatched window for a slug → most recent unclaimed JSONL
       - Hard: multiple unmatched → text-match via _gauge_sent texts
    Once locked, a match persists until the Claude PID changes.
    """
    global _gauge_cache, _gauge_cache_time, _gauge_locks
    now = time.time()
    # Use faster poll interval when unmatched windows might exist
    # (we can't know for sure without running the pipeline, but _gauge_sent
    # having entries is a strong signal that windows are waiting for text match)
    ttl = GAUGE_MATCH_TTL if _gauge_sent else GAUGE_CACHE_TTL
    if now - _gauge_cache_time < ttl:
        return
    _gauge_cache_time = now

    try:
        cache = {}

        # Step 1: Get tmux panes with pane_pid and cwd
        r = _run(["tmux", "list-panes", "-a", "-F",
                   "#{session_name}\t#{window_index}\t#{pane_pid}\t#{pane_current_path}"],
                  capture_output=True, text=True)
        if not r.stdout.strip():
            return
        pane_by_pid = {}   # shell_pid → (session, window, cwd)
        for line in r.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            sname, widx, spid, cwd = parts
            try:
                pane_by_pid[int(spid)] = (sname, int(widx), cwd)
            except ValueError:
                pass

        # Step 2: Build process tree, find Claude children
        r2 = _run(["ps", "-eo", "pid,ppid,comm"], capture_output=True, text=True)
        if not r2.stdout.strip():
            return
        child_map = {}   # ppid → [(child_pid, comm)]
        for line in r2.stdout.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 3:
                try:
                    child_map.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2]))
                except ValueError:
                    pass

        claude_pids = []   # (claude_pid, shell_pid)
        for spid in pane_by_pid:
            for cpid, comm in child_map.get(spid, []):
                if "claude" in comm.lower():
                    claude_pids.append((cpid, spid))
                    break

        if not claude_pids:
            _gauge_cache = cache
            return

        # Build slug → active JSONLs map
        projects_dir = Path.home() / ".claude" / "projects"
        cutoff = now - 7 * 86400  # 7 days — generous window for matching stale sessions
        slug_jsonls = {}   # slug → [(path, mtime, stem)]
        if projects_dir.is_dir():
            for pdir in projects_dir.iterdir():
                if not pdir.is_dir():
                    continue
                for jf in pdir.glob("*.jsonl"):
                    st = jf.stat()
                    if st.st_mtime >= cutoff:
                        slug_jsonls.setdefault(pdir.name, []).append(
                            (str(jf), st.st_mtime, jf.stem))

        # Prune stale locks (PID changed or window gone)
        active_keys = set()
        pid_for_key = {}
        for cpid, spid in claude_pids:
            pane_info = pane_by_pid.get(spid)
            if pane_info:
                sname, widx, cwd = pane_info
                key = f"{sname}:{widx}"
                active_keys.add(key)
                pid_for_key[key] = cpid
        for key in list(_gauge_locks.keys()):
            if key not in active_keys or _gauge_locks[key]["pid"] != pid_for_key.get(key):
                del _gauge_locks[key]

        # Pass 1: Refresh metrics for locked windows (no re-matching)
        for cpid, spid in claude_pids:
            pane_info = pane_by_pid.get(spid)
            if not pane_info:
                continue
            sname, widx, cwd = pane_info
            key = f"{sname}:{widx}"
            if key not in _gauge_locks:
                continue
            path = _gauge_locks[key]["path"]
            if not os.path.exists(path):
                del _gauge_locks[key]
                continue
            try:
                _gauge_cache_metrics(cache, key, path, _gauge_locks[key]["stem"], "locked")
                if key not in cache:
                    # No usage data — locked JSONL has no assistant messages.
                    # Likely a stale lock from /clear creating a new JSONL
                    # while PID stayed the same. Evict to allow re-matching.
                    del _gauge_locks[key]
            except FileNotFoundError:
                del _gauge_locks[key]  # JSONL deleted, unlock

        # Rebuild claimed_stems after Pass 1 evictions
        claimed_stems = {m["stem"] for m in _gauge_locks.values()}

        # Pass 2: Match unlocked windows
        # Group unmatched by slug to detect easy vs hard case
        unmatched = []  # [(key, cpid, slug, cwd)]
        for cpid, spid in claude_pids:
            pane_info = pane_by_pid.get(spid)
            if not pane_info:
                continue
            sname, widx, cwd = pane_info
            key = f"{sname}:{widx}"
            if key in _gauge_locks:
                continue
            slug = cwd.replace("/", "-")
            unmatched.append((key, cpid, slug, cwd))

        if unmatched:
            # Group by slug
            by_slug = {}
            for key, cpid, slug, cwd in unmatched:
                by_slug.setdefault(slug, []).append((key, cpid))

            for slug, windows in by_slug.items():
                jsonls = slug_jsonls.get(slug, [])
                if not jsonls:
                    continue
                available = [(p, mt, s) for p, mt, s in jsonls if s not in claimed_stems]
                if not available:
                    continue
                available.sort(key=lambda x: x[1], reverse=True)

                if len(windows) == 1:
                    # Easy case: single unmatched window → most recent unclaimed JSONL
                    key, cpid = windows[0]
                    path, mt, stem = available[0]
                    _gauge_locks[key] = {"stem": stem, "pid": cpid, "path": path}
                    claimed_stems.add(stem)
                    _gauge_cache_metrics(cache, key, path, stem, "mtime")
                else:
                    # Hard case: multiple unmatched windows for same slug
                    # Use sent texts (from api_send) or tmux capture (bootstrap)
                    for key, cpid in windows:
                        sent = _gauge_sent.get(key, [])
                        if not sent:
                            # Bootstrap: extract user texts from tmux screen
                            sn, wi = key.split(":", 1)
                            sent = _gauge_extract_tmux_texts(sn, int(wi))
                        if not sent:
                            continue  # no text available — stay unmatched (AC-5)
                        # Score each candidate by text matches — lock if one is clearly best
                        candidates_still = [(p, mt, s) for p, mt, s in available
                                            if s not in claimed_stems]
                        scored = []
                        for path, mt, stem in candidates_still:
                            score = _gauge_score_text_match(sent, path)
                            if score > 0:
                                scored.append((score, path, mt, stem))
                        if scored:
                            scored.sort(key=lambda x: x[0], reverse=True)
                            best_score = scored[0][0]
                            # Lock if best score is unique (no tie)
                            if len(scored) == 1 or scored[1][0] < best_score:
                                _, path, mt, stem = scored[0]
                                _gauge_locks[key] = {"stem": stem, "pid": cpid,
                                                     "path": path}
                                claimed_stems.add(stem)
                                _gauge_sent.pop(key, None)
                                _gauge_cache_metrics(cache, key, path, stem, "text")

        _gauge_cache = cache
        _gauge_save_locks()
    except Exception:
        pass  # Keep stale cache on error


ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'
    r'|\x1b\][^\x07]*\x07'
    r'|\x1b\([A-Z]'
    r'|\x1b[>=]'
    r'|\x0f'
)


TMUX_TIMEOUT = 5  # seconds — prevents requests from hanging if tmux is unresponsive


def _run(cmd, **kwargs):
    """Run a subprocess with a default timeout."""
    kwargs.setdefault('timeout', TMUX_TIMEOUT)
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        # Return a fake result so callers don't crash
        r = subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr='timeout')
        return r


DIM_SPAN_RE = re.compile(
    r'\x1b\[(?:[0-9;]*;)?2m'   # SGR with dim/faint attribute (code 2)
    r'(.*?)'                     # dim text to remove
    r'(?=\x1b\[0m|\x1b\[[0-9;]*[^m]|\Z)',  # until reset or new sequence
    re.DOTALL
)
REVERSE_CHAR_RE = re.compile(
    r'\x1b\[7m'                 # SGR reverse video (cursor char)
    r'(.)'                       # single cursor character
    r'\x1b\[(?:0m|27m)'         # reset or reverse-off
)


def strip_ghost_text(text: str) -> str:
    """Remove dim/faint text (ghost suggestions) and reverse-video cursor char from ANSI output.
    CC's TUI renders ghost suggestions with SGR 2 (dim) and the cursor char with SGR 7 (reverse).
    Must be called BEFORE stripping ANSI codes."""
    # Remove dim/faint spans (ghost suggestion text after cursor)
    # Pattern: \e[0;2m...text...\e[0m  or  \e[2m...text...\e[0m
    text = re.sub(r'\x1b\[0?;?2m[^\x1b]*', '', text)
    # Remove reverse-video cursor char — it's the first char of the ghost suggestion
    text = REVERSE_CHAR_RE.sub('', text)
    return text


def clean_terminal_text(text: str) -> str:
    """Strip ANSI escapes and control characters from terminal output."""
    text = strip_ghost_text(text)
    text = ANSI_RE.sub("", text)
    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', text)
    return text


def ensure_session():
    r = _run(["tmux", "has-session", "-t", _current_session], capture_output=True)
    if r.returncode != 0:
        work_dir = WORK_DIR if Path(WORK_DIR).is_dir() else str(Path.home())
        _run([
            "tmux", "new-session", "-d", "-s", _current_session,
            "-x", "80", "-y", "50", "-c", work_dir,
        ])


def _tmux_target(session=None, window=None):
    """Build a tmux target string like 'session:window' or just 'session'."""
    s = session or _current_session
    if window is not None:
        return f"{s}:{window}"
    return s


def send_keys(text: str, session=None, window=None) -> bool:
    """Send text to tmux pane. Returns True on success, False on failure."""
    target = _tmux_target(session, window)
    # Slash commands (/exit, /clear, etc.) must be TYPED not pasted for CC's TUI
    # to recognize them. send-keys -l sends literal keystrokes, which is reliable
    # for short single-line strings. paste-buffer inserts text as a paste event,
    # which CC treats differently (doesn't trigger slash command handling).
    is_slash_cmd = text.startswith("/") and "\n" not in text
    if is_slash_cmd:
        # Skip Escape+C-u for slash commands — Escape interferes with CC's TUI state.
        r = _run(["tmux", "send-keys", "-l", "-t", target, text])
        if r.returncode != 0:
            return False
        time.sleep(0.05)
        _run(["tmux", "send-keys", "-t", target, "Enter"])
        return True
    # Escape dismisses any active CC suggestion/autocomplete, C-u clears the line
    _run(["tmux", "send-keys", "-t", target, "Escape"])
    _run(["tmux", "send-keys", "-t", target, "C-u"])
    # Use load-buffer + paste-buffer for reliable delivery of normal text.
    # send-keys -l is unreliable for large text: special chars ($, \, `, ")
    # can be interpreted by tmux.
    # -p enables bracketed paste so TUI apps (CC) treat multiline text as a single paste.
    buf_name = "_mt_paste"
    r = _run(["tmux", "load-buffer", "-b", buf_name, "-"], input=text.encode())
    if r.returncode != 0:
        return False  # Don't send Enter if buffer load failed
    is_multiline = "\n" in text
    paste_cmd = ["tmux", "paste-buffer", "-d", "-b", buf_name, "-t", target]
    if is_multiline:
        paste_cmd.insert(3, "-p")  # Bracketed paste only for multiline
    r = _run(paste_cmd)
    if r.returncode != 0:
        return False
    time.sleep(0.05)  # Let TUI process paste before sending Enter
    _run(["tmux", "send-keys", "-t", target, "Enter"])
    return True


def send_special(key: str, session=None, window=None):
    target = _tmux_target(session, window)
    _run(["tmux", "send-keys", "-t", target, key])


def get_output(session=None, window=None) -> str:
    target = _tmux_target(session, window)
    r = _run(
        ["tmux", "capture-pane", "-t", target, "-e", "-p", "-S", "-200"],
        capture_output=True, text=True,
    )
    text = clean_terminal_text(r.stdout)
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def get_pane_preview(session: str, window: int, lines: int = 5) -> str:
    """Capture last N lines from a specific pane for preview."""
    target = f"{session}:{window}"
    r = _run(
        ["tmux", "capture-pane", "-t", target, "-e", "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return clean_terminal_text(r.stdout).strip()


def detect_cc_status(text: str) -> dict:
    """Detect if text is Claude Code output and its status.
    Returns dict with is_cc, status, context_pct, perm_mode.
    """
    is_cc = '\u276f' in text and ('\u23fa' in text or bool(re.search(r'Claude Code v\d', text)))
    if not is_cc:
        return {"is_cc": False, "status": None, "context_pct": None, "perm_mode": None, "fresh": False}

    lines = text.split('\n')

    # --- Text signals ---

    # 1. "esc to interrupt" on the status bar (line starting with ⏵)
    #    In current CC, this appears on the same line as the permissions bar:
    #    "⏵⏵ bypass permissions on (shift+tab to cycle) · 3 files · esc to interrupt"
    has_working = False
    context_pct = None
    perm_mode = None
    for line in reversed(lines[-5:]):
        if line.lstrip().startswith('\u23f5'):
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

    # 2. Thinking: · at START of any line in last 15 lines
    tail = '\n'.join(lines[-15:])
    has_thinking = bool(re.search(r'^\u00b7', tail, re.MULTILINE))

    # --- Determine status ---
    # Only rely on text signals (status bar + thinking indicator).
    # activity_age is NOT used — CC's TUI refreshes periodically (cursor blink,
    # status bar updates) which keeps activity_age low even when idle.
    if has_working:
        status = 'working'
    elif has_thinking:
        status = 'thinking'
    else:
        status = 'idle'

    fresh = '\u23fa' not in text
    return {"is_cc": True, "status": status, "context_pct": context_pct, "perm_mode": perm_mode, "fresh": fresh}


def get_dashboard() -> dict:
    """Get lightweight status for all sessions and windows."""
    now = time.time()
    # Single call to get all pane metadata including activity timestamp
    r = _run(
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
        # Get preview for CC detection (40 lines)
        preview = get_pane_preview(sname, int(widx), lines=40)
        cc = detect_cc_status(preview)
        # Always provide tmux window_activity as baseline fallback.
        # Client prefers gauge_last_ts (JSONL) when available.
        try:
            act_ts = int(wactivity)
        except (ValueError, TypeError):
            act_ts = None
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
            "cc_fresh": cc.get("fresh", False),
            "preview": preview,
            "activity_ts": act_ts,
        })
    # Enrich with gauge data (context window utilization from JSONL transcripts)
    _refresh_gauge_cache()
    if _gauge_cache:
        for s in sessions.values():
            for w in s["windows"]:
                key = f"{s['name']}:{w['index']}"
                gauge = _gauge_cache.get(key)
                if w.get("cc_fresh") and key in _gauge_locks:
                    del _gauge_locks[key]
                    _gauge_save_locks()
                    _gauge_cache.pop(key, None)
                    continue
                if gauge:
                    w["gauge_context_pct"] = round(100 - gauge["pct_used"], 1)  # remaining %
                    w["gauge_burn_rate"] = gauge["burn_rate"]
                    w["gauge_est_turns"] = gauge["est_turns_remaining"]
                    w["gauge_total_turns"] = gauge["total_turns"]
                    if gauge.get("last_ts"):
                        w["gauge_last_ts"] = gauge["last_ts"]
                    # Cross-validate: gauge remaining vs CC status bar remaining
                    cc_left = w.get("cc_context_pct")
                    if cc_left is not None:
                        drift = abs(w["gauge_context_pct"] - cc_left)
                        w["gauge_drift"] = round(drift, 1)

    return {"sessions": list(sessions.values())}


def list_sessions() -> list:
    """List all tmux sessions with their windows."""
    r = _run(
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
        wr = _run(
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
    r = _run(
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


def new_window(session=None, cwd=None, commands=None):
    target = session or _current_session
    work_dir = cwd or WORK_DIR
    if not Path(work_dir).is_dir():
        work_dir = str(Path.home())
    r = _run(["tmux", "new-window", "-t", target, "-c", work_dir, "-P", "-F", "#{window_index}"],
             capture_output=True, text=True)
    if r.returncode != 0:
        return None
    new_idx = r.stdout.strip()
    # Send startup commands if any
    if commands and new_idx:
        for cmd in commands:
            send_keys(cmd, session=target, window=new_idx)
            time.sleep(0.1)
    return int(new_idx) if new_idx else None


def select_window(index: int):
    _run(["tmux", "select-window", "-t", f"{_current_session}:{index}"])


def _send_notification(title: str, body: str, key: str = None):
    """Send macOS + ntfy notification with 10s dedup per key."""
    now = time.time()
    if key:
        last = _notify_sent.get(key, 0)
        if now - last < 10:
            return
        _notify_sent[key] = now
    # macOS notification
    _run(["osascript", "-e",
          f'display notification "{body}" with title "{title}"'],
         timeout=3)
    # ntfy.sh push
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode(),
                headers={"Title": title},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass


def _check_pending_notifications():
    """Synchronous work for notification monitor — runs in thread."""
    keys = list(_notify_pending.keys())
    for key in keys:
        entry = _notify_pending.get(key)
        if not entry:
            continue
        try:
            session, window = key.rsplit(":", 1)
            window = int(window)
            preview = get_pane_preview(session, window, lines=20)
            cc = detect_cc_status(preview)
            if not cc["is_cc"]:
                _notify_pending.pop(key, None)
                continue
            if cc["status"] in ("working", "thinking"):
                entry["saw_busy"] = True
            elif entry["saw_busy"]:
                name = entry["window_name"] or key
                _send_notification("Claude Code done", f"{name} finished", key)
                _notify_pending.pop(key, None)
        except Exception:
            _notify_pending.pop(key, None)


async def _notification_monitor():
    """Background task: poll CC status for pending notifications."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(5)
        if _notify_pending:
            await loop.run_in_executor(None, _check_pending_notifications)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_notification_monitor())


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
  --accent: #D97757; --accent2: #c4693e; --accent-dim: rgba(217,119,87,0.12);
  --accent-focus: rgba(217,119,87,0.5); --red: #e5534b;
  --green: #3fb950; --orange: #d29922;
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-bottom: env(safe-area-inset-bottom, 0px);
  --sidebar-w: 300px;
  --text-size: 15px; --code-size: 13px; --mono-size: 13px;
  --turn-pad-v: 16px; --turn-pad-h: 18px; --turn-gap: 12px;
  --turn-radius: 18px; --line-h: 1.7;
  --sb-name: 15px; --sb-detail: 13px; --sb-tiny: 12px;
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;
  font-weight:400;
  overflow:hidden; -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
  text-rendering:optimizeLegibility; font-feature-settings:'kern' 1; }

/* --- App layout --- */
#app { display:flex; height:100%; }

/* --- Sidebar --- */
#sidebar { width:var(--sidebar-w); min-width:0; background:var(--bg2);
  border-right:1px solid var(--border2); display:flex; flex-direction:column;
  transition:width .2s ease, min-width .2s ease; overflow:hidden;
  padding-top:var(--safe-top); }
#sidebar.collapsed { width:0; min-width:0; border-right:none; }
#sidebar.collapsed + #sidebar-resize { display:none; }
#sidebar-expand { display:none; position:absolute; left:0; top:12px;
  z-index:50; background:var(--surface); border:1px solid var(--border2); border-left:none;
  color:var(--text3); cursor:pointer; padding:8px 6px; border-radius:0 8px 8px 0;
  font-size:16px; line-height:1; transition:all .15s; }
#sidebar-expand:hover { color:var(--text); background:var(--bg2); }
#sidebar.collapsed ~ main #sidebar-expand { display:block; }
#sidebar-header { padding:12px 14px 8px; display:flex; align-items:center;
  justify-content:space-between; flex-shrink:0; }
#sidebar-header h2 { font-size:var(--sb-name); font-weight:700; color:var(--text2);
  text-transform:uppercase; letter-spacing:0.5px; }
#sb-new-win-btn { background:none; border:none; color:var(--text3); cursor:pointer;
  font-size:18px; font-weight:300; padding:2px 6px; border-radius:6px; transition:all .15s; margin-left:auto; margin-right:2px; line-height:1; }
#sb-new-win-btn:hover { color:var(--text); background:var(--surface); }
#sb-expand-btn { background:none; border:none; color:var(--text3); cursor:pointer;
  font-size:14px; padding:4px 6px; border-radius:6px; transition:all .15s; margin-right:2px; }
#sb-expand-btn:hover { color:var(--text); background:var(--surface); }
#collapse-btn { background:none; border:none; color:var(--text3); cursor:pointer;
  font-size:16px; padding:4px 6px; border-radius:6px; transition:all .15s; }
#collapse-btn:hover { color:var(--text); background:var(--surface); }
#sidebar-content { flex:1; overflow-y:auto; padding:0 4px 8px;
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
.sb-session-header { display:flex; align-items:center; gap:6px; padding:8px 4px 4px;
  color:var(--text3); font-size:var(--sb-detail); font-weight:700; text-transform:uppercase;
  letter-spacing:0.5px; cursor:grab; }
.sb-session-header .sb-badge { font-size:var(--sb-tiny); padding:1px 5px; border-radius:6px;
  background:var(--accent); color:#fff; font-weight:500; text-transform:none;
  letter-spacing:0; }
.sb-session-header { position:relative; }
.sb-hide-btn { background:none; border:none; color:var(--text3); font-size:var(--sb-tiny);
  cursor:pointer; padding:1px 5px; border-radius:4px; opacity:0; transition:opacity .15s;
  text-transform:none; letter-spacing:0; font-weight:500; margin-left:auto; }
.sb-session-header:hover .sb-hide-btn { opacity:1; }
.sb-hide-btn:hover { color:var(--text); background:var(--surface); }
.sb-hidden-header { padding:12px 4px 4px; color:var(--text3); font-size:var(--sb-detail);
  font-weight:700; text-transform:uppercase; letter-spacing:0.5px; cursor:pointer;
  display:flex; align-items:center; gap:4px; user-select:none; }
.sb-hidden-header:hover { color:var(--text2); }
.sb-hidden-chevron { transition:transform .15s; font-size:10px; }
.sb-hidden-chevron.open { transform:rotate(90deg); }
.sb-win { display:flex; align-items:center; gap:8px; padding:6px 4px;
  border-radius:8px; cursor:pointer; transition:all .12s;
  -webkit-user-select:none; user-select:none; }
.sb-win:hover { background:var(--surface); }
.sb-win.active { background:var(--accent-dim); }
.sb-win.sb-drag-over { border-top:2px solid var(--accent); margin-top:-2px; }
.sb-win.dragging { opacity:0.4; }
.sb-win-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.sb-win-dot.idle { background:var(--green); }
.sb-win-dot.working { background:var(--orange); animation:pulse 1.5s ease-in-out infinite; }
.sb-win-dot.thinking { background:var(--orange); animation:pulse 1s ease-in-out infinite; }
.sb-win-dot.none { background:var(--text3); opacity:0.3; }
.sb-win-info { flex:1; min-width:0; }
.sb-win-name { font-size:var(--sb-name); font-weight:500; color:var(--text);
  max-width:120px; overflow-wrap:break-word; line-height:1.3; }
.sb-win-cwd { font-size:var(--sb-detail); color:var(--text3);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sb-perm { font-size:var(--sb-tiny); color:var(--text3); margin-top:1px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sb-activity { flex-shrink:0; font-size:var(--sb-name); color:var(--text3);
  white-space:nowrap; font-variant-numeric:tabular-nums; text-align:right; min-width:24px; }
.sb-ctx { flex-shrink:0; font-size:var(--sb-name); color:var(--text3);
  white-space:nowrap; font-variant-numeric:tabular-nums; text-align:right; min-width:30px; }
.sb-ctx.low { color:var(--orange); font-weight:700; }
.sb-ctx.critical { color:var(--red); font-weight:700; }
.sb-perm.danger { color:#a07070; font-weight:600; font-style:italic; }
.sb-standby { font-size:var(--sb-tiny); color:var(--blue, #5b9bd5); font-weight:600;
  text-transform:uppercase; letter-spacing:0.5px; }
.sb-fresh { font-size:var(--sb-tiny); color:var(--text3); font-weight:600;
  text-transform:uppercase; letter-spacing:0.5px; }
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
    width:var(--sidebar-w); min-width:260px; }
  #sidebar.open { transform:translateX(0); }
  #sidebar.collapsed { transform:translateX(-100%); }
  #sidebar-backdrop.open { display:block; }
  .sb-win-detail-btn { opacity:1; }
  .sb-hide-btn { opacity:1; }
  .ft-hide-btn { opacity:1; }
}

/* --- Main area --- */
#main { flex:1; display:flex; flex-direction:column; min-width:0; position:relative; }

/* --- Top bar --- */
#topbar { background:var(--bg); padding:calc(var(--safe-top) + 6px) 12px 0;
  display:flex; flex-direction:column; gap:0; flex-shrink:0; z-index:10; position:relative; }
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
@media (max-width:768px) { #add-pane-btn { font-size:12px; padding:2px 6px; } }
#settings-btn { position:relative; }
#settings-panel { display:none; position:absolute; top:100%; right:12px;
  background:var(--surface); border:1px solid var(--border); border-radius:12px;
  padding:12px; width:200px; z-index:20; box-shadow:0 8px 24px rgba(0,0,0,.4); }
.settings-section { margin-bottom:10px; }
.settings-section:last-child { margin-bottom:0; }
.settings-label { font-size:11px; color:var(--text3); margin-bottom:6px; font-weight:500; }
.settings-size-btns { display:flex; gap:4px; }
.settings-size-btn { flex:1; height:28px; border-radius:14px; border:1px solid var(--border);
  background:var(--bg); color:var(--text2); font-size:11px; font-weight:500;
  font-family:inherit; cursor:pointer; transition:all .15s; }
.settings-size-btn.active { background:var(--accent); color:#fff; border-color:var(--accent); }
.settings-size-btn:active { transform:scale(0.95); }
.settings-row { display:flex; align-items:center; justify-content:space-between; }
.settings-row span { font-size:12px; color:var(--text2); }
.settings-toggle { height:26px; padding:0 10px; border-radius:13px; border:1px solid var(--border);
  background:var(--bg); color:var(--text3); font-size:11px; font-weight:500;
  font-family:inherit; cursor:pointer; transition:all .15s; }
.settings-toggle.on { background:var(--accent); color:#fff; border-color:var(--accent); }

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
  transition:background .15s; z-index:3; position:relative; }
#sidebar-resize::after { content:''; position:absolute; top:0; bottom:0; left:-6px; right:-6px; }
#sidebar-resize:hover, #sidebar-resize.active { background:var(--accent); }
@media (max-width:768px) { #sidebar-resize { display:none; } }

.pane-divider { flex-shrink:0; background:var(--border2); transition:background .15s; z-index:2; position:relative; }
.pane-divider.col { width:4px; cursor:col-resize; }
.pane-divider.row { height:4px; cursor:row-resize; }
.pane-divider::after { content:''; position:absolute; z-index:1; }
.pane-divider.col::after { top:0; bottom:0; left:-8px; right:-8px; }
.pane-divider.row::after { left:0; right:0; top:-8px; bottom:-8px; }
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
.pane-tab.drag-over-left { position:relative; }
.pane-tab.drag-over-left::before { content:''; position:absolute; left:-2px; top:0; bottom:0; width:3px; background:var(--accent); border-radius:2px; z-index:1; }
.pane-tab.drag-over-right { position:relative; }
.pane-tab.drag-over-right::after { content:''; position:absolute; right:-2px; top:0; bottom:0; width:3px; background:var(--accent); border-radius:2px; z-index:1; }
.pane-tab-dot { width:6px; height:6px; border-radius:50%; flex-shrink:0; }
.pane-tab-dot.idle { background:var(--green); }
.pane-tab-dot.working { background:var(--orange); animation:pulse 1.5s ease-in-out infinite; }
.pane-tab-dot.thinking { background:var(--orange); animation:pulse 1s ease-in-out infinite; }
.pane-tab-dot.none { display:none; }
.pane-tab-name { overflow:hidden; text-overflow:ellipsis; }
.pane-tab-close { display:flex; align-items:center; justify-content:center;
  width:20px; height:20px; border-radius:4px; font-size:12px; line-height:1;
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
.master-notes-panel { position:absolute; top:0; left:0; right:0; z-index:20;
  max-width:700px; margin:0 auto; background:var(--bg2);
  border:1px solid var(--border2); border-top:none; border-radius:0 0 12px 12px;
  display:flex; flex-direction:column; overflow:hidden;
  transform:translateY(-10px); opacity:0; pointer-events:none;
  transition:transform .15s ease, opacity .15s ease; }
.master-notes-panel.open { transform:translateY(0); opacity:1; pointer-events:auto; }
.master-notes-panel .notepad-header span { font-size:12px; font-weight:600; color:var(--text2); }
.master-notes-panel textarea { flex:1; background:transparent; color:var(--text);
  border:none; padding:10px 12px; font-size:13px; font-family:inherit;
  resize:none; outline:none; line-height:1.5; min-height:200px; }
.master-notes-panel .notepad-resize { height:6px; cursor:ns-resize; flex-shrink:0;
  background:transparent; position:relative; }
.master-notes-panel .notepad-resize::after { content:''; position:absolute; left:50%; top:50%;
  transform:translate(-50%,-50%); width:30px; height:3px; border-radius:2px;
  background:var(--border2); }

/* Queue panel */
.pane-queue-btn { background:none; border:none; color:var(--text3);
  font-size:10px; cursor:pointer; padding:2px 6px;
  flex-shrink:0; border-radius:3px; font-weight:600; letter-spacing:0.5px; }
.pane-queue-btn:hover { color:var(--accent); background:rgba(255,255,255,0.05); }
.pane-queue-btn.active { color:var(--accent); }
.pane-queue-btn.playing { color:var(--green); }
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
.queue-play-btn.playing { color:var(--green); }
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
.queue-item.current { border-left-color:var(--accent); background:var(--accent-dim); }
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
.queue-add textarea:focus { border-color:var(--accent-focus); }
.queue-add button { background:var(--accent); color:#fff; border:none;
  border-radius:8px; padding:6px 10px; font-size:13px; font-weight:600;
  cursor:pointer; flex-shrink:0; }
.queue-add button:active { transform:scale(0.95); }

/* Pane output */
.pane-output { flex:1; overflow-y:auto; -webkit-overflow-scrolling:touch; }
.pane-output.raw { padding:16px 14px; background:#111112;
  font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace;
  font-size:var(--mono-size); line-height:1.6; white-space:pre-wrap;
  word-break:break-word; color:#999; }
.pane-output.chat { display:flex; flex-direction:column; padding:10px 14px 20px; }
.pane-gauge { display:none; font-size:11px; line-height:1.2; color:var(--text3);
  text-align:right; font-variant-numeric:tabular-nums; padding:0 4px 2px; }
.pane-gauge:not(:empty) { display:block; }
.pane-gauge .pg-pct { font-weight:600; color:var(--text); }
.pane-gauge .pg-pct.low { color:var(--orange); }
.pane-gauge .pg-pct.critical { color:var(--red); }
.pane-gauge .pg-detail { color:var(--text3); font-size:10px; }

/* Pane input */
.pane-input { display:none; padding:8px 10px; background:var(--bg2);
  border-top:1px solid var(--border); flex-shrink:0; flex-direction:column; gap:6px; }
.pane-input.visible { display:flex; }
.pane-input .pane-input-row { display:flex; gap:8px; align-items:flex-end; }
.pane-input textarea { flex:1; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:16px; padding:8px 14px;
  font-size:var(--text-size); font-family:inherit; outline:none; resize:none;
  overflow-y:auto; max-height:40vh; line-height:1.4; }
.pane-input textarea::placeholder { color:var(--text3); }
.pane-input textarea:focus { border-color:var(--accent-focus); }
.pane-input .pane-send { flex-shrink:0; width:32px; height:32px; border-radius:50%;
  background:var(--accent); border:none; color:#fff; cursor:pointer;
  display:flex; align-items:center; justify-content:center; }
.pane-input .pane-send svg { width:16px; height:16px; }
.pane-input .pane-send:active { transform:scale(0.92); }
.pane-toolbar { display:flex; gap:6px; }
.pane-tray { max-height:0; overflow:hidden; transition:max-height .25s ease, margin .25s ease;
  display:flex; flex-wrap:wrap; gap:4px; margin-top:0; }
.pane-tray.open { max-height:100px; margin-top:6px; }

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
a.file-link { color:var(--accent); text-decoration:underline; text-decoration-style:dotted;
  text-underline-offset:2px; cursor:pointer; }
a.file-link:active { opacity:0.6; }
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
.box-table-wrap { overflow-x:auto; margin:0.5em 0; }
.box-table { border-collapse:collapse; width:100%; white-space:normal; }
.box-table th, .box-table td { padding:6px 10px; text-align:left; vertical-align:top;
  border:1px solid var(--border); font-size:var(--mono-size); line-height:1.4; word-break:break-word; }
.box-table th { color:var(--text2); font-weight:600; background:rgba(255,255,255,0.03); }
.box-table td { color:var(--text); }
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
#msg:focus { border-color:var(--accent-focus);
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
  border:none; border-radius:50%; background:var(--surface); color:var(--text2);
  font-size:16px; line-height:28px; text-align:center; cursor:pointer; padding:0; }
.wd-close-x:active { background:var(--border); }
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
#wd-rename-input:focus { border-color:var(--accent-focus); }
.wd-save-btn { padding:8px 16px; background:var(--accent); color:#fff;
  border:none; border-radius:8px; font-size:13px; font-weight:500;
  font-family:inherit; cursor:pointer; }
.wd-btns { display:flex; gap:8px; margin-top:12px; }
.wd-btns button { flex:1; padding:9px; border:none; border-radius:8px;
  font-size:13px; font-weight:500; font-family:inherit; cursor:pointer; }
.wd-btn-dismiss { background:var(--surface); color:var(--text2); }
.wd-btn-dismiss.active { background:rgba(91,155,213,0.15); color:#5b9bd5; }

/* New window modal */
#nw-overlay { display:none; position:fixed; inset:0; z-index:100;
  background:rgba(0,0,0,0.6); align-items:center; justify-content:center; }
#nw-overlay.open { display:flex; }
#nw-modal { background:var(--bg2); border:1px solid var(--border2);
  border-radius:16px; padding:20px; width:min(340px, 85vw); position:relative; }
#nw-modal h3 { font-size:14px; font-weight:600; color:var(--text); margin-bottom:14px; }
#nw-dir-input { width:100%; background:var(--surface); color:var(--text);
  border:1px solid var(--border2); border-radius:8px; padding:8px 12px;
  font-size:13px; font-family:'SF Mono',ui-monospace,Menlo,monospace; outline:none;
  box-sizing:border-box; }
#nw-dir-input:focus { border-color:var(--accent-focus); }
.nw-check-row { display:flex; align-items:center; gap:8px; padding:8px 0; cursor:pointer; }
.nw-check-row input[type="checkbox"] { width:18px; height:18px; accent-color:var(--accent);
  cursor:pointer; flex-shrink:0; }
.nw-check-row label { color:var(--text2); font-size:13px; cursor:pointer; user-select:none; }
.nw-check-row .nw-sub { color:var(--text3); font-size:11px; }
#nw-create-btn { width:100%; padding:10px; margin-top:8px; background:var(--accent); color:#fff;
  border:none; border-radius:8px; font-size:14px; font-weight:500;
  font-family:inherit; cursor:pointer; }
#nw-create-btn:active { opacity:0.8; }

/* File browser overlay */
#fb-overlay { display:none; position:fixed; inset:0; z-index:100;
  background:var(--bg); flex-direction:column; }
#fb-overlay.open { display:flex; }
#fb-header { display:flex; align-items:center; gap:8px;
  padding:calc(var(--safe-top) + 10px) 12px 10px;
  border-bottom:1px solid var(--border); background:var(--bg2); min-height:44px; }
#fb-back { background:none; border:none; color:var(--accent); font-size:22px;
  cursor:pointer; padding:4px 8px; line-height:1; font-family:inherit; }
#fb-back:disabled { opacity:0.3; }
#fb-breadcrumbs { flex:1; display:flex; align-items:center; gap:2px;
  overflow-x:auto; white-space:nowrap; font-size:13px; color:var(--text3);
  -webkit-overflow-scrolling:touch; scrollbar-width:none; }
#fb-breadcrumbs::-webkit-scrollbar { display:none; }
.fb-crumb { background:none; border:none; color:var(--accent); font-size:13px;
  cursor:pointer; padding:2px 4px; font-family:inherit; white-space:nowrap; }
.fb-crumb-sep { color:var(--text3); font-size:11px; }
.fb-crumb-current { color:var(--text); font-weight:600; padding:2px 4px; white-space:nowrap; }
#fb-close { background:none; border:none; color:var(--text3); font-size:22px;
  cursor:pointer; padding:4px 8px; line-height:1; }
#fb-content { flex:1; overflow-y:auto; -webkit-overflow-scrolling:touch; padding:8px 0; }
.fb-roots-title { font-size:12px; font-weight:600; color:var(--text3);
  text-transform:uppercase; letter-spacing:0.5px; padding:12px 16px 8px; }
.fb-list { list-style:none; margin:0; padding:0; }
.fb-item { display:flex; align-items:center; gap:10px; padding:11px 16px;
  border-bottom:1px solid var(--border); cursor:pointer; -webkit-tap-highlight-color:transparent; }
.fb-item:active { background:var(--surface); }
.fb-icon { font-size:18px; width:24px; text-align:center; flex-shrink:0; }
.fb-name { flex:1; font-size:14px; color:var(--text); overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.fb-meta { font-size:11px; color:var(--text3); white-space:nowrap; }
.fb-empty { padding:40px 16px; text-align:center; color:var(--text3); font-size:14px; }
.fb-reader { max-width:700px; margin:0 auto; padding:16px 20px 40px; }
.fb-reader-title { font-size:15px; font-weight:600; color:var(--text3);
  margin-bottom:16px; padding-bottom:10px; border-bottom:1px solid var(--border); }
.fb-reader-body { color:var(--text); font-size:var(--text-size); line-height:1.65; }
.fb-reader-body h1,.fb-reader-body h2,.fb-reader-body h3,
.fb-reader-body h4,.fb-reader-body h5,.fb-reader-body h6 {
  color:var(--text); margin:1.2em 0 0.5em; font-weight:600; }
.fb-reader-body h1 { font-size:1.5em; } .fb-reader-body h2 { font-size:1.3em; }
.fb-reader-body h3 { font-size:1.15em; }
.fb-reader-body p { margin:0.6em 0; }
.fb-reader-body code { background:var(--surface); padding:2px 5px;
  border-radius:4px; font-size:0.9em; font-family:'SF Mono',ui-monospace,Menlo,monospace; }
.fb-reader-body pre { background:var(--surface); padding:12px 14px;
  border-radius:8px; overflow-x:auto; margin:0.8em 0; }
.fb-reader-body pre code { background:none; padding:0; font-size:0.85em; }
.fb-reader-body blockquote { border-left:3px solid var(--accent); margin:0.8em 0;
  padding:4px 14px; color:var(--text2); }
.fb-reader-body ul,.fb-reader-body ol { padding-left:1.5em; margin:0.5em 0; }
.fb-reader-body li { margin:0.3em 0; }
.fb-reader-body a { color:var(--accent); text-decoration:none; }
.fb-reader-body a:hover { text-decoration:underline; }
.fb-reader-body table { border-collapse:collapse; width:100%; margin:0.8em 0; }
.fb-reader-body th,.fb-reader-body td { border:1px solid var(--border2);
  padding:6px 10px; font-size:13px; text-align:left; }
.fb-reader-body th { background:var(--surface); font-weight:600; }
.fb-reader-body img { max-width:100%; border-radius:8px; }
.fb-reader-body hr { border:none; border-top:1px solid var(--border); margin:1.5em 0; }

/* Sidebar view tabs (Sessions / Files) */
#sb-view-tabs { display:flex; gap:0; flex-shrink:0; margin:0 10px 6px; border-radius:8px;
  background:var(--surface); padding:2px; }
.sb-view-tab { flex:1; padding:5px 8px; background:none; border:none; color:var(--text3);
  font-size:12px; font-weight:600; font-family:inherit; cursor:pointer;
  border-radius:6px; transition:all .15s; text-transform:uppercase; letter-spacing:0.3px; }
.sb-view-tab.active { background:var(--bg); color:var(--text); }
#sidebar.files-view .sb-action-sessions { display:none; }
.sb-action-files { display:none; }
#sidebar.files-view .sb-action-files { display:inline-flex; }

/* File tree in sidebar (VS Code compact style) */
.ft-tree { list-style:none; margin:0; padding:0; }
.ft-node { list-style:none; }
.ft-row { display:flex; align-items:center; gap:4px; padding:1px 6px;
  cursor:pointer; -webkit-tap-highlight-color:transparent;
  white-space:nowrap; overflow:hidden; height:22px; }
.ft-row:hover { background:rgba(255,255,255,0.05); }
.ft-row:active { background:rgba(255,255,255,0.08); }
.ft-chevron { width:16px; font-size:var(--sb-tiny); color:var(--text3); flex-shrink:0;
  text-align:center; display:inline-flex; align-items:center; justify-content:center; }
.ft-icon { font-size:var(--sb-name); flex-shrink:0; width:16px; text-align:center; opacity:0.7; }
.ft-name { font-size:var(--sb-name); color:var(--text); overflow:hidden;
  text-overflow:ellipsis; flex:1; line-height:22px; }
.ft-name-dir { color:var(--text); }
.ft-name-md { color:#519aba; }
.ft-name-bin { color:var(--text3); opacity:0.5; }
.ft-row.ft-binary { cursor:default; opacity:0.4; }
.ft-row.ft-binary:hover { background:none; }
.ft-root-group { position:relative; }
.ft-root-group.dragging { opacity:0.4; }
.ft-root-group.ft-drag-over { border-top:2px solid var(--accent); }
.ft-root-label { font-size:var(--sb-detail); font-weight:600; color:var(--text3); text-transform:uppercase;
  letter-spacing:0.3px; margin-top:6px; padding:4px 6px 2px; display:flex; align-items:center; gap:4px;
  overflow:hidden; white-space:nowrap; cursor:grab; }
.ft-root-label:first-child { margin-top:0; }
.ft-root-name { overflow:hidden; text-overflow:ellipsis; flex:1; }
.ft-hide-btn { background:none; border:none; color:var(--text3); font-size:var(--sb-tiny);
  cursor:pointer; opacity:0; padding:0 4px; flex-shrink:0; transition:opacity 0.15s; }
.ft-root-label:hover .ft-hide-btn { opacity:1; }
.ft-hide-btn:hover { color:var(--text); }
.ft-hidden-header { padding:12px 4px 4px; color:var(--text3); font-size:var(--sb-detail);
  cursor:pointer; user-select:none; }
.ft-hidden-header:hover { color:var(--text); }
.ft-hidden-chevron { display:inline-block; transition:transform 0.15s; font-size:8px; }
.ft-hidden-chevron.open { transform:rotate(90deg); }
.ft-empty { padding:24px 12px; text-align:center; color:var(--text3); font-size:var(--sb-detail); }
.ft-loading { color:var(--text3); font-size:12px; padding:2px 6px; padding-left:36px; height:22px;
  line-height:22px; }
.ft-children { list-style:none; margin:0; padding:0; }

/* Raw markdown syntax highlighting */
.md-raw { margin:0; padding:16px 14px; color:var(--text); font-size:var(--mono-size);
  font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace; white-space:pre-wrap;
  word-break:break-word; line-height:1.6; counter-reset:line; }
.md-raw .md-h { color:#569cd6; font-weight:bold; }
.md-raw .md-hm { color:#569cd6; opacity:0.6; }
.md-raw .md-bold { color:#ce9178; font-weight:bold; }
.md-raw .md-bm { color:#ce9178; opacity:0.5; }
.md-raw .md-italic { color:#c586c0; font-style:italic; }
.md-raw .md-im { color:#c586c0; opacity:0.5; }
.md-raw .md-code { color:#4ec9b0; background:rgba(78,201,176,0.08);
  padding:1px 3px; border-radius:3px; }
.md-raw .md-cm { color:#4ec9b0; opacity:0.4; }
.md-raw .md-fence { color:#808080; }
.md-raw .md-fenced { color:#d4d4d4; background:rgba(255,255,255,0.03);
  display:inline; }
.md-raw .md-link-text { color:#569cd6; }
.md-raw .md-link-url { color:#808080; }
.md-raw .md-link-bracket { color:#569cd6; opacity:0.5; }
.md-raw .md-bullet { color:#d7ba7d; }
.md-raw .md-blockquote { color:#608b4e; }
.md-raw .md-pipe { color:#808080; }
.md-raw .md-hr { color:#808080; }

/* Code file syntax highlighting */
.code-view { margin:0; padding:16px 14px; color:#d4d4d4; font-size:var(--mono-size);
  font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace; white-space:pre-wrap;
  word-break:break-word; line-height:1.6; }
.code-view .code-kw { color:#569cd6; }
.code-view .code-builtin { color:#4ec9b0; }
.code-view .code-string { color:#ce9178; }
.code-view .code-comment { color:#6a9955; font-style:italic; }
.code-view .code-num { color:#b5cea8; }
.code-view .code-decorator { color:#d7ba7d; }

/* File tab toolbar */
.file-tab-toolbar { display:flex; align-items:center; gap:8px; padding:8px 12px;
  border-bottom:1px solid var(--border); background:var(--bg2); flex-shrink:0; }
.file-tab-path { flex:1; font-size:11px; color:var(--text3); overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; font-family:'SF Mono',ui-monospace,Menlo,monospace; }
.file-tab-toggle { display:flex; gap:0; background:var(--surface); border-radius:6px; padding:2px; }
.file-tab-toggle button { padding:3px 10px; background:none; border:none; color:var(--text3);
  font-size:11px; font-weight:600; font-family:inherit; cursor:pointer;
  border-radius:4px; transition:all .15s; }
.file-tab-toggle button.active { background:var(--bg); color:var(--text); }
.pane-tab.file-tab .pane-tab-name { font-style:italic; }
.pane-tab.file-tab .pane-tab-dot { background:var(--text3); opacity:0.3; }

/* File tab hides input bar */
.pane.file-tab-active .pane-input { display:none; }

/* File editor */
.file-editor-wrap { flex:1; display:flex; flex-direction:column; min-height:0; }
.file-editor { flex:1; width:100%; margin:0; padding:12px 16px; border:none; outline:none;
  background:transparent; color:#d4d4d4; font-size:var(--mono-size);
  font-family:'SF Mono',ui-monospace,Menlo,monospace; white-space:pre-wrap;
  word-break:break-word; line-height:1.5; resize:none; box-sizing:border-box; }
.file-dirty-dot { width:6px; height:6px; border-radius:50%; background:var(--accent);
  display:inline-block; margin-right:2px; flex-shrink:0; }
.file-save-btn { padding:3px 10px; border:none; border-radius:4px; font-size:11px;
  font-weight:600; font-family:inherit; cursor:pointer; transition:all .15s; }
.file-save-btn.dirty { background:var(--accent); color:#fff; }
.file-save-btn:not(.dirty) { background:var(--surface); color:var(--text3); }
.file-save-btn.saving { opacity:0.5; pointer-events:none; }
.file-edit-btn { padding:3px 10px; background:none; border:1px solid var(--border2);
  border-radius:4px; font-size:11px; font-weight:600; font-family:inherit;
  color:var(--text3); cursor:pointer; transition:all .15s; }
.file-edit-btn.active { border-color:var(--accent); color:var(--accent); }
.file-external-change { display:flex; align-items:center; gap:8px; padding:6px 12px;
  background:rgba(217,119,87,0.12); border-bottom:1px solid var(--accent);
  font-size:12px; color:var(--accent); }
.file-external-change button { padding:2px 8px; background:var(--accent); color:#fff;
  border:none; border-radius:4px; font-size:11px; font-weight:600; cursor:pointer;
  font-family:inherit; }
.pane-tab.file-dirty .pane-tab-dot { background:var(--accent) !important; opacity:1 !important; }
</style>
</head>
<body>

<div id="app">
<aside id="sidebar">
  <div id="sidebar-header">
    <div id="sb-view-tabs">
      <button class="sb-view-tab active" data-view="sessions" onclick="switchSidebarView('sessions')">Sessions</button>
      <button class="sb-view-tab" data-view="files" onclick="switchSidebarView('files')">Files</button>
    </div>
    <button id="sb-new-win-btn" class="sb-action-sessions" onclick="newWin()" title="New window">+</button>
    <button id="sb-expand-btn" class="sb-action-sessions" onclick="toggleSidebarExpand()" title="Toggle detail level">&#9656;</button>
    <button id="sb-ft-refresh-btn" class="sb-action-files" onclick="ftRefresh()" title="Refresh file tree">&#8635;</button>
    <button id="collapse-btn" onclick="toggleSidebar()" title="Collapse sidebar">&laquo;</button>
  </div>
  <div id="sidebar-content"></div>
  <div id="sidebar-footer" class="sb-action-sessions">
    <button id="new-win-btn" onclick="newWin()">+ New Window</button>
  </div>
</aside>
<div id="sidebar-resize"></div>
<div id="sidebar-backdrop" onclick="closeMobileSidebar()"></div>

<main id="main">
  <button id="sidebar-expand" onclick="toggleSidebar()" title="Open sidebar">&raquo;</button>
  <div id="topbar">
    <div id="topbar-row">
      <button id="hamburger" onclick="openMobileSidebar()">&#9776;</button>
      <span style="flex:1"></span>
      <button class="topbar-btn" id="master-notes-btn" onclick="toggleMasterNotes()">Notes</button>
      <button class="topbar-btn" id="fb-btn" onclick="toggleFileBrowser()">Files</button>
      <button class="topbar-btn" id="add-pane-btn" onclick="addPane()">+ Pane</button>
      <button class="topbar-btn" id="settings-btn" onclick="toggleSettings()">&#9881;</button>
      <button class="topbar-btn" id="view-btn" onclick="toggleRaw()">
        <span>View: </span><span id="view-label">Raw</span>
      </button>
    </div>
    <div id="settings-panel">
      <div class="settings-section">
        <div class="settings-label">Text Size</div>
        <div class="settings-size-btns">
          <button class="settings-size-btn" onclick="applyTextSize(0)">A--</button>
          <button class="settings-size-btn" onclick="applyTextSize(1)">A-</button>
          <button class="settings-size-btn active" onclick="applyTextSize(2)">A</button>
          <button class="settings-size-btn" onclick="applyTextSize(3)">A+</button>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-row">
          <span>File Links</span>
          <button class="settings-toggle on" id="file-links-toggle" onclick="toggleFileLinks()">ON</button>
        </div>
      </div>
    </div>
  </div>

  <div id="master-notes-container" style="position:relative"></div>
  <div id="panes-container"></div>

  <div id="bar">
    <div id="global-gauge" class="pane-gauge"></div>
    <div id="input-row">
      <textarea id="msg" rows="1" placeholder="Enter command..."
        autocomplete="off" enterkeyhint="send"></textarea>
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
      <button class="pill" onclick="keyActive('Left')">Left</button>
      <button class="pill" onclick="keyActive('Right')">Right</button>
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
    <button class="wd-close-x" onclick="closeWD()" title="Close">&times;</button>
    <h3 id="wd-title">Window Details</h3>
    <div id="wd-content"></div>
    <div id="wd-rename-row">
      <input id="wd-rename-input" type="text" placeholder="Window name..."
        autocorrect="off" autocapitalize="none" spellcheck="false">
      <button class="wd-save-btn" onclick="saveWDRename()">Save</button>
    </div>
    <div class="wd-btns">
      <button id="wd-standby-btn" class="wd-btn-dismiss" onclick="toggleWDStandby()">Set Standby</button>
      <button class="wd-btn-dismiss" onclick="closeWDWindow()" style="color:var(--red)">Close Window</button>
    </div>
  </div>
</div>

<div id="nw-overlay" onclick="if(event.target===this)closeNewWin()">
  <div id="nw-modal">
    <button class="wd-close-x" onclick="closeNewWin()" title="Close">&times;</button>
    <h3>New Window</h3>
    <div style="margin-bottom:8px">
      <div style="color:var(--text3);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Directory</div>
      <input id="nw-dir-input" type="text" placeholder="/path/to/directory"
        autocorrect="off" autocapitalize="none" spellcheck="false"
        onkeydown="if(event.key==='Enter'){event.preventDefault();submitNewWin()}">
    </div>
    <div class="nw-check-row" onclick="document.getElementById('nw-claude-cb').click()">
      <input type="checkbox" id="nw-claude-cb" checked onclick="event.stopPropagation(); toggleNwDsp()">
      <label onclick="event.stopPropagation()">Open Claude Code</label>
    </div>
    <div class="nw-check-row" id="nw-dsp-row" onclick="document.getElementById('nw-dsp-cb').click()">
      <input type="checkbox" id="nw-dsp-cb" checked onclick="event.stopPropagation()">
      <label onclick="event.stopPropagation()">--dangerously-skip-permissions</label>
    </div>
    <button id="nw-create-btn" onclick="submitNewWin()">Create</button>
  </div>
</div>

<div id="fb-overlay">
  <div id="fb-header">
    <button id="fb-back" onclick="fbBack()" disabled>&lsaquo;</button>
    <div id="fb-breadcrumbs"><span class="fb-crumb-current">Working Directories</span></div>
    <button id="fb-close" onclick="closeFileBrowser()">&times;</button>
  </div>
  <div id="fb-content"></div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/lib/marked.umd.min.js"></script>
<script>
// === Prefs (server-synced preferences) ===
const prefs = {
  _cache: {},
  _dirty: {},
  _timer: null,
  _localKeys: new Set(['layout']),
  getItem(key) {
    if (this._localKeys.has(key)) return localStorage.getItem(key);
    const v = this._cache[key];
    return v === undefined ? null : v;
  },
  setItem(key, val) {
    if (this._localKeys.has(key)) { localStorage.setItem(key, val); return; }
    this._cache[key] = val;
    this._dirty[key] = val;
    this._scheduleFlush();
  },
  removeItem(key) {
    if (this._localKeys.has(key)) { localStorage.removeItem(key); return; }
    delete this._cache[key];
    this._dirty[key] = null;
    this._scheduleFlush();
  },
  _scheduleFlush() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this._flush(), 500);
  },
  async _flush() {
    this._timer = null;
    const batch = this._dirty;
    this._dirty = {};
    try {
      await fetch('/api/prefs', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(batch) });
    } catch(e) {
      // Re-queue on failure
      for (const k in batch) { if (!(k in this._dirty)) this._dirty[k] = batch[k]; }
      this._scheduleFlush();
    }
  },
  async load() {
    try {
      const r = await fetch('/api/prefs');
      const data = await r.json();
      if (Object.keys(data).length > 0) {
        this._cache = data;
      } else {
        // First run: migrate synced keys from localStorage to server
        const batch = {};
        for (let i = 0; i < localStorage.length; i++) {
          const key = localStorage.key(i);
          if (key && !this._localKeys.has(key)) {
            batch[key] = localStorage.getItem(key);
            this._cache[key] = batch[key];
          }
        }
        if (Object.keys(batch).length > 0) {
          await fetch('/api/prefs', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(batch) });
        }
      }
    } catch(e) {
      // Offline fallback: use localStorage
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key && !this._localKeys.has(key)) this._cache[key] = localStorage.getItem(key);
      }
    }
  },
  keys() { return Object.keys(this._cache); }
};

// === Data model ===
let panes = [];         // [{ id, tabIds, activeTabId }]
let activePaneId = null;
let _dragSrcTabId = null;
let allTabs = {};       // tabId -> { session, windowIndex, windowName }
let tabStates = {};     // tabId -> { rawContent, last, rawMode, pendingMsg, pendingTime, awaitingResponse, lastOutputChange, pollInterval }
let _nextPaneId = 1;
let _nextTabId = 1;
let _dashboardData = null;
let _sidebarCollapsed = false;
let _sidebarExpanded = false;
let _wdSession = null, _wdWindow = null; // window details modal context
let _queueStates = {}; // tabId -> { items: [{text, done}], playing: false, currentIdx: null, idleTimer: null }
let _sidebarView = 'sessions'; // 'sessions' | 'files'
let _ftTreeCache = {}; // path -> {items, expanded}
let _ftExpanded = {}; // path -> true
let _ftRootOrder = []; // persisted root order
let _ftHiddenRoots = []; // hidden root paths
let _ftHiddenExpanded = false;

const M = document.getElementById('msg');
const bar = document.getElementById('bar');
const panesContainer = document.getElementById('panes-container');
const SEND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>';

// === Utility ===
function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
const _tblStartRe = /^\s*\u250c[\u2500\u252c]+\u2510/m;
const _tblEndRe = /^\s*\u2514[\u2500\u2534]+\u2518/m;
const _tblSepRe = /^[\u250c\u251c\u2514][\u2500\u252c\u253c\u2534\u2510\u2524\u2518]+$/;
function boxTableToHtml(tableLines) {
  const sections = []; let cur = [];
  for (const line of tableLines) {
    if (_tblSepRe.test(line.trim())) {
      if (cur.length) { sections.push(cur); cur = []; }
    } else { cur.push(line); }
  }
  if (cur.length) sections.push(cur);
  if (!sections.length) return esc(tableLines.join('\\n'));
  let html = '<div class="box-table-wrap"><table class="box-table">';
  sections.forEach((rows, sIdx) => {
    const isHdr = sIdx === 0, tag = isHdr ? 'th' : 'td';
    const cells = [];
    for (const line of rows) {
      const parts = line.split('\\u2502');
      if (parts.length < 3) continue;
      const ct = parts.slice(1, -1).map(p => p.trim());
      if (!cells.length) ct.forEach(t => cells.push([t]));
      else ct.forEach((t, i) => { if (i < cells.length) cells[i].push(t); else cells.push([t]); });
    }
    if (isHdr) html += '<thead>';
    html += '<tr>';
    for (const cell of cells) {
      html += '<' + tag + '>' + cell.filter(Boolean).map(t => esc(t)).join('<br>') + '</' + tag + '>';
    }
    html += '</tr>';
    if (isHdr) html += '</thead>';
  });
  html += '</table></div>';
  return html;
}
function renderRawWithTables(raw) {
  const lines = raw.split('\\n');
  const parts = []; let buf = []; let i = 0;
  while (i < lines.length) {
    if (_tblStartRe.test(lines[i])) {
      if (buf.length) { parts.push(esc(buf.join('\\n'))); buf = []; }
      const tl = []; let j = i, found = false;
      while (j < lines.length) { tl.push(lines[j]); if (_tblEndRe.test(lines[j]) && j > i) { found = true; j++; break; } j++; }
      if (found) parts.push(boxTableToHtml(tl)); else buf.push(...tl);
      i = j;
    } else { buf.push(lines[i]); i++; }
  }
  if (buf.length) parts.push(esc(buf.join('\\n')));
  return parts.join('');
}
function _initMarked() {
  if (typeof marked === 'undefined') return false;
  const renderer = new marked.Renderer();
  renderer.link = function(href, title, text) {
    const h = typeof href === 'object' ? href.href : href;
    const t = typeof href === 'object' ? href.title : title;
    const tx = typeof href === 'object' ? href.text : text;
    return '<a href="' + h + '" target="_blank" rel="noopener noreferrer"' + (t ? ' title="' + t + '"' : '') + '>' + tx + '</a>';
  };
  marked.setOptions({ breaks: false, renderer: renderer });
  return true;
}
function md(s) {
  if (_initMarked()) {
    try {
      // Split into table blocks and text blocks
      const allLines = s.split('\\n');
      const segments = []; let textBuf = []; let li = 0;
      while (li < allLines.length) {
        if (_tblStartRe.test(allLines[li])) {
          if (textBuf.length) { segments.push({t:'text', l:textBuf}); textBuf = []; }
          const tl = []; let j = li, found = false;
          while (j < allLines.length) { tl.push(allLines[j]); if (_tblEndRe.test(allLines[j]) && j > li) { found = true; j++; break; } j++; }
          if (found) segments.push({t:'table', l:tl}); else textBuf.push(...tl);
          li = j;
        } else { textBuf.push(allLines[li]); li++; }
      }
      if (textBuf.length) segments.push({t:'text', l:textBuf});
      // Render each segment
      const boxRe = /[\u2500-\u257f\u2580-\u259f]/;
      let html = '';
      for (const seg of segments) {
        if (seg.t === 'table') { html += boxTableToHtml(seg.l); continue; }
        const out = []; let inBox = false;
        for (const line of seg.l) {
          if (boxRe.test(line)) { if (!inBox) { out.push('```'); inBox = true; } out.push(line); }
          else { if (inBox) { out.push('```'); inBox = false; } out.push(line); }
        }
        if (inBox) out.push('```');
        const escaped = out.join('\\n').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        html += marked.parse(escaped);
      }
      return html;
    } catch(e) { /* fall through to plain text */ }
  }
  return '<p>' + esc(s) + '</p>';
}
function getTabCwd(tabId) {
  if (!_dashboardData) return null;
  const tab = allTabs[tabId];
  if (!tab || tab.type === 'file') return null;
  const sess = _dashboardData.sessions.find(s => s.name === tab.session);
  if (!sess) return null;
  const win = sess.windows.find(w => w.index === tab.windowIndex);
  return win ? win.cwd : null;
}
const _fileExtPat = '(?:py|js|ts|tsx|jsx|mjs|cjs|md|json|ya?ml|toml|css|scss|html|sh|rb|go|rs|c|h|cpp|hpp|java|kt|swift|vue|svelte|sql|xml|conf|cfg|ini|txt|env|lock|plist|log|csv)';
const _filePathRe = new RegExp('((?:\\\\.{0,2}\\\\/)?(?:[\\\\w@.+-]+\\\\/)*[\\\\w@.+-]+\\\\.(?:' + _fileExtPat + ')(?![\\\\w]))(?::(\\\\d+))?', 'g');
function linkifyFilePaths(el, cwd) {
  if (!el || !cwd) return;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
  const nodes = [];
  let n;
  while (n = walker.nextNode()) {
    // Skip text inside <a>, <pre>, <textarea>, <input>
    if (n.parentElement.closest('a,pre,textarea,input')) continue;
    nodes.push(n);
  }
  for (const textNode of nodes) {
    const text = textNode.textContent;
    _filePathRe.lastIndex = 0;
    if (!_filePathRe.test(text)) continue;
    _filePathRe.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let lastIdx = 0, match;
    while ((match = _filePathRe.exec(text)) !== null) {
      const filePath = match[1], lineNum = match[2], full = match[0];
      if (match.index > lastIdx)
        frag.appendChild(document.createTextNode(text.substring(lastIdx, match.index)));
      const absPath = filePath.startsWith('/') ? filePath : cwd + '/' + filePath;
      const a = document.createElement('a');
      a.className = 'file-link';
      a.textContent = full;
      a.dataset.path = absPath;
      if (lineNum) a.dataset.line = lineNum;
      a.href = 'javascript:void(0)';
      a.onclick = function(e) { e.preventDefault(); e.stopPropagation();
        // Open in another pane if multiple panes exist, so user sees both output and file
        let tgt = null;
        if (panes.length >= 2) { const o = panes.find(p => p.id !== activePaneId); if (o) tgt = o.id; }
        ftOpenFile(this.dataset.path, tgt);
      };
      frag.appendChild(a);
      lastIdx = match.index + full.length;
    }
    if (lastIdx > 0) {
      if (lastIdx < text.length)
        frag.appendChild(document.createTextNode(text.substring(lastIdx)));
      textNode.parentNode.replaceChild(frag, textNode);
    }
  }
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

// === File Browser (overlay) ===
let _fbHistory = []; // [{mode, path, scrollTop}]
let _fbCurrentPath = null;
let _fbMode = 'roots'; // 'roots' | 'list' | 'read'

function toggleFileBrowser() {
  const ov = document.getElementById('fb-overlay');
  if (ov.classList.contains('open')) closeFileBrowser();
  else openFileBrowser();
}
function openFileBrowser() {
  _fbHistory = [];
  _fbMode = 'roots';
  _fbCurrentPath = null;
  document.getElementById('fb-overlay').classList.add('open');
  fbShowRoots();
  fbUpdateBreadcrumbs();
}
function closeFileBrowser() {
  document.getElementById('fb-overlay').classList.remove('open');
}

function fbShowRoots() {
  _fbMode = 'roots';
  _fbCurrentPath = null;
  const content = document.getElementById('fb-content');
  document.getElementById('fb-back').disabled = true;
  const cwds = new Set();
  if (_dashboardData) {
    for (const s of _dashboardData.sessions) {
      for (const w of s.windows) {
        if (w.cwd) cwds.add(w.cwd);
      }
    }
  }
  if (cwds.size === 0) {
    content.innerHTML = '<div class="fb-empty">No active sessions found</div>';
    fbUpdateBreadcrumbs();
    return;
  }
  const sorted = [...cwds].sort();
  let html = '<div class="fb-roots-title">Working Directories</div><ul class="fb-list">';
  for (const cwd of sorted) {
    html += '<li class="fb-item" data-path="' + esc(cwd) + '" data-type="dir">'
      + '<span class="fb-icon">\\ud83d\\udcc2</span>'
      + '<span class="fb-name">' + esc(abbreviateCwd(cwd)) + '</span>'
      + '<span class="fb-meta" style="font-family:\\'SF Mono\\',monospace;font-size:10px;color:var(--text3)">' + esc(cwd) + '</span>'
      + '</li>';
  }
  html += '</ul>';
  content.innerHTML = html;
  content.querySelector('.fb-list').addEventListener('click', function(e) {
    const item = e.target.closest('.fb-item');
    if (!item) return;
    fbNavigate(item.dataset.path);
  });
  fbUpdateBreadcrumbs();
}

function fbNavigate(dirPath) {
  const scrollTop = document.getElementById('fb-content').scrollTop;
  _fbHistory.push({ mode: _fbMode, path: _fbCurrentPath, scrollTop });
  fbLoadDir(dirPath);
}

async function fbLoadDir(dirPath) {
  _fbMode = 'list';
  _fbCurrentPath = dirPath;
  const content = document.getElementById('fb-content');
  document.getElementById('fb-back').disabled = false;
  content.innerHTML = '<div class="fb-empty">Loading...</div>';
  fbUpdateBreadcrumbs();
  try {
    const r = await fetch('/api/files?path=' + encodeURIComponent(dirPath));
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      content.innerHTML = '<div class="fb-empty">' + esc(err.detail || 'Access denied') + '</div>';
      return;
    }
    const data = await r.json();
    const filtered = data.items.filter(i => i.type === 'dir' || i.name.toLowerCase().endsWith('.md'));
    if (filtered.length === 0) {
      content.innerHTML = '<div class="fb-empty">No directories or markdown files here</div>';
      return;
    }
    let html = '<ul class="fb-list">';
    for (const item of filtered) {
      const icon = item.type === 'dir' ? '\\ud83d\\udcc1' : '\\ud83d\\udcc4';
      let meta = '';
      if (item.type === 'file') {
        const kb = item.size < 1024 ? item.size + ' B' : (item.size / 1024).toFixed(1) + ' KB';
        meta = kb;
      }
      html += '<li class="fb-item" data-path="' + esc(item.path) + '" data-type="' + item.type + '">'
        + '<span class="fb-icon">' + icon + '</span>'
        + '<span class="fb-name">' + esc(item.name) + '</span>'
        + (meta ? '<span class="fb-meta">' + meta + '</span>' : '')
        + '</li>';
    }
    html += '</ul>';
    content.innerHTML = html;
    content.querySelector('.fb-list').addEventListener('click', function(e) {
      const item = e.target.closest('.fb-item');
      if (!item) return;
      if (item.dataset.type === 'dir') fbNavigate(item.dataset.path);
      else fbOpenFile(item.dataset.path);
    });
  } catch(e) {
    content.innerHTML = '<div class="fb-empty">Error loading directory</div>';
  }
}

async function fbOpenFile(filePath) {
  const scrollTop = document.getElementById('fb-content').scrollTop;
  _fbHistory.push({ mode: _fbMode, path: _fbCurrentPath, scrollTop });
  _fbMode = 'read';
  _fbCurrentPath = filePath;
  const content = document.getElementById('fb-content');
  document.getElementById('fb-back').disabled = false;
  content.innerHTML = '<div class="fb-empty">Loading...</div>';
  fbUpdateBreadcrumbs();
  try {
    const r = await fetch('/api/files/read?path=' + encodeURIComponent(filePath));
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      content.innerHTML = '<div class="fb-empty">' + esc(err.detail || 'Cannot read file') + '</div>';
      return;
    }
    const data = await r.json();
    content.innerHTML = '<div class="fb-reader">'
      + '<div class="fb-reader-title">' + esc(data.name) + '</div>'
      + '<div class="fb-reader-body">' + mdFile(data.content) + '</div>'
      + '</div>';
    content.scrollTop = 0;
  } catch(e) {
    content.innerHTML = '<div class="fb-empty">Error reading file</div>';
  }
}

function fbBack() {
  if (_fbHistory.length === 0) return;
  const prev = _fbHistory.pop();
  if (prev.mode === 'roots') {
    fbShowRoots();
  } else if (prev.mode === 'list') {
    fbLoadDir(prev.path).then(() => {
      document.getElementById('fb-content').scrollTop = prev.scrollTop || 0;
    });
  }
  document.getElementById('fb-back').disabled = _fbHistory.length === 0 && prev.mode === 'roots';
  fbUpdateBreadcrumbs();
}

function fbUpdateBreadcrumbs() {
  const bc = document.getElementById('fb-breadcrumbs');
  if (_fbMode === 'roots') {
    bc.innerHTML = '<span class="fb-crumb-current">Working Directories</span>';
    return;
  }
  let html = '<button class="fb-crumb" data-fb-action="roots">Roots</button>';
  if (_fbCurrentPath) {
    const parts = _fbCurrentPath.split('/').filter(Boolean);
    const display = abbreviateCwd(_fbCurrentPath);
    if (_fbMode === 'read') {
      const dirPath = _fbCurrentPath.substring(0, _fbCurrentPath.lastIndexOf('/'));
      const fileName = parts[parts.length - 1];
      const dirDisplay = abbreviateCwd(dirPath);
      html += '<span class="fb-crumb-sep">/</span>';
      html += '<button class="fb-crumb" data-fb-action="dir" data-fb-path="' + esc(dirPath) + '">' + esc(dirDisplay) + '</button>';
      html += '<span class="fb-crumb-sep">/</span>';
      html += '<span class="fb-crumb-current">' + esc(fileName) + '</span>';
    } else {
      html += '<span class="fb-crumb-sep">/</span>';
      html += '<span class="fb-crumb-current">' + esc(display) + '</span>';
    }
  }
  bc.innerHTML = html;
  bc.scrollLeft = bc.scrollWidth;
}
document.getElementById('fb-breadcrumbs').addEventListener('click', function(e) {
  const btn = e.target.closest('[data-fb-action]');
  if (!btn) return;
  if (btn.dataset.fbAction === 'roots') {
    _fbHistory = [];
    fbShowRoots();
    fbUpdateBreadcrumbs();
  } else if (btn.dataset.fbAction === 'dir') {
    _fbHistory = [{ mode: 'roots', path: null, scrollTop: 0 }];
    fbLoadDir(btn.dataset.fbPath);
    fbUpdateBreadcrumbs();
  }
});

// === File Browser (sidebar-integrated) ===
function mdFile(s) {
  if (_initMarked()) {
    try { return marked.parse(s); } catch(e) {}
  }
  return '<pre>' + s.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</pre>';
}

function switchSidebarView(view) {
  if (_sidebarView === view) return;
  _sidebarView = view;
  document.querySelectorAll('.sb-view-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  document.getElementById('sidebar').classList.toggle('files-view', view === 'files');
  if (view === 'files') {
    renderFileTree();
  } else {
    renderSidebar();
  }
}

function updateFileTreeRoots() {
  // Gather unique cwds from dashboard data for root nodes
  const cwds = new Set();
  if (_dashboardData) {
    for (const s of _dashboardData.sessions) {
      for (const w of s.windows) { if (w.cwd) cwds.add(w.cwd); }
    }
  }
  let all = [...cwds].sort();
  // Apply persisted order
  if (_ftRootOrder.length) {
    const ordered = _ftRootOrder.filter(p => all.includes(p));
    const rest = all.filter(p => !_ftRootOrder.includes(p));
    all = [...ordered, ...rest];
  }
  return all;
}

function getHiddenRoots() {
  return _ftHiddenRoots;
}
function setHiddenRoots(arr) {
  _ftHiddenRoots = arr;
  prefs.setItem('ft:hidden-roots', JSON.stringify(arr));
}
function hideFtRoot(path) {
  const h = getHiddenRoots();
  if (!h.includes(path)) { h.push(path); setHiddenRoots(h); }
  renderFileTree();
}
function unhideFtRoot(path) {
  setHiddenRoots(getHiddenRoots().filter(p => p !== path));
  renderFileTree();
}
function saveFtRootOrder() {
  prefs.setItem('ft:root-order', JSON.stringify(_ftRootOrder));
}
function ftRefresh() {
  _ftTreeCache = {};
  _ftExpanded = {};
  renderFileTree();
}

function renderFileTree() {
  const content = document.getElementById('sidebar-content');
  const allRoots = updateFileTreeRoots();
  const hidden = getHiddenRoots();
  const roots = allRoots.filter(r => !hidden.includes(r));
  const hiddenRoots = allRoots.filter(r => hidden.includes(r));
  if (roots.length === 0 && hiddenRoots.length === 0) {
    content.innerHTML = '<div class="ft-empty">No active sessions found</div>';
    content._lastHTML = null;
    return;
  }
  let html = '';
  for (const root of roots) {
    html += renderFtRootGroup(root, false);
  }
  if (hiddenRoots.length > 0) {
    html += '<div class="ft-hidden-header" onclick="_ftHiddenExpanded=!_ftHiddenExpanded;renderFileTree()">'
      + '<span class="ft-hidden-chevron' + (_ftHiddenExpanded ? ' open' : '') + '">&#9654;</span>'
      + ' Hidden (' + hiddenRoots.length + ')</div>';
    if (_ftHiddenExpanded) {
      for (const root of hiddenRoots) {
        html += renderFtRootGroup(root, true);
      }
    }
  }
  content.innerHTML = html;
  content._lastHTML = null;
}

function renderFtRootGroup(root, isHidden) {
  let html = '<div class="ft-root-group" draggable="true" data-ft-root-path="' + esc(root) + '">';
  html += '<div class="ft-root-label" title="' + esc(root) + '">';
  html += '<span class="ft-root-name">' + esc(abbreviateCwd(root)) + '</span>';
  html += '<button class="ft-hide-btn" data-ft-hide-action="' + (isHidden ? 'show' : 'hide') + '" data-ft-hide-path="' + esc(root) + '">' + (isHidden ? 'SHOW' : 'HIDE') + '</button>';
  html += '</div>';
  html += '<ul class="ft-tree">';
  html += renderFtNode(root, root.split('/').filter(Boolean).pop() || root, 0, true, root);
  html += '</ul>';
  html += '</div>';
  return html;
}

function ftFileIcon(name) {
  const ext = name.lastIndexOf('.') >= 0 ? name.substring(name.lastIndexOf('.') + 1).toLowerCase() : '';
  // VS Code-like file type colors
  if (ext === 'md') return '<span class="ft-icon" style="color:#519aba">M</span>';
  if (ext === 'py') return '<span class="ft-icon" style="color:#4584b6">\\u03c0</span>';
  if (ext === 'js') return '<span class="ft-icon" style="color:#e8d44d">J</span>';
  if (ext === 'ts' || ext === 'tsx') return '<span class="ft-icon" style="color:#3178c6">T</span>';
  if (ext === 'json') return '<span class="ft-icon" style="color:#cbcb41">{}</span>';
  if (ext === 'toml' || ext === 'yml' || ext === 'yaml') return '<span class="ft-icon" style="color:#6d8086">\\u2699</span>';
  if (ext === 'html') return '<span class="ft-icon" style="color:#e44d26">&lt;&gt;</span>';
  if (ext === 'css') return '<span class="ft-icon" style="color:#42a5f5">#</span>';
  if (ext === 'sh' || ext === 'bash' || ext === 'zsh') return '<span class="ft-icon" style="color:#89e051">$</span>';
  if (ext === 'plist') return '<span class="ft-icon" style="color:#6d8086">P</span>';
  if (ext === 'txt' || ext === 'log') return '<span class="ft-icon" style="color:#6d8086">\\u2261</span>';
  if (name === '.env' || name.startsWith('.env.')) return '<span class="ft-icon" style="color:#ecd53f">\\u26a1</span>';
  if (name === '.gitignore') return '<span class="ft-icon" style="color:#f14e32">G</span>';
  return '<span class="ft-icon" style="color:#6d8086">\\u25a1</span>';
}
function renderFtNode(path, name, depth, isDir, rootPath) {
  const expanded = _ftExpanded[path];
  let html = '<li class="ft-node">';
  const bin = !isDir && isBinary(name);
  html += '<div class="ft-row' + (bin ? ' ft-binary' : '') + '" data-ft-path="' + esc(path) + '" data-ft-dir="' + (isDir ? '1' : '0') + '" data-ft-root="' + esc(rootPath) + '" style="padding-left:' + (6 + depth * 16) + 'px">';
  if (isDir) {
    html += '<span class="ft-chevron">' + (expanded ? '\\u25be' : '\\u25b8') + '</span>';
    html += '<span class="ft-icon" style="color:#c09553">\\ud83d\\udcc1</span>';
    html += '<span class="ft-name ft-name-dir">' + esc(name) + '</span>';
  } else {
    html += '<span class="ft-chevron"></span>';
    html += ftFileIcon(name);
    const isMd = name.endsWith('.md');
    html += '<span class="ft-name' + (isMd ? ' ft-name-md' : bin ? ' ft-name-bin' : '') + '">' + esc(name) + '</span>';
  }
  html += '</div>';
  if (isDir && expanded) {
    const cached = _ftTreeCache[path];
    if (cached) {
      html += '<ul class="ft-children">';
      for (const item of cached) {
        html += renderFtNode(item.path, item.name, depth + 1, item.type === 'dir', rootPath);
      }
      if (cached.length === 0) html += '<li class="ft-loading">Empty</li>';
      html += '</ul>';
    } else {
      html += '<ul class="ft-children"><li class="ft-loading">Loading...</li></ul>';
    }
  }
  html += '</li>';
  return html;
}

async function ftToggleDir(path) {
  if (_ftExpanded[path]) {
    delete _ftExpanded[path];
    renderFileTree();
    return;
  }
  _ftExpanded[path] = true;
  renderFileTree(); // show loading state
  if (!_ftTreeCache[path]) {
    try {
      const r = await fetch('/api/files?path=' + encodeURIComponent(path));
      if (r.ok) {
        const data = await r.json();
        _ftTreeCache[path] = data.items || [];
      } else {
        _ftTreeCache[path] = [];
      }
    } catch(e) {
      _ftTreeCache[path] = [];
    }
  }
  renderFileTree();
}

// Event delegation for file tree clicks
document.getElementById('sidebar-content').addEventListener('click', function(e) {
  if (_sidebarView !== 'files') return;
  // Hide/show button
  const hideBtn = e.target.closest('.ft-hide-btn');
  if (hideBtn) {
    e.stopPropagation();
    const path = hideBtn.dataset.ftHidePath;
    if (hideBtn.dataset.ftHideAction === 'hide') hideFtRoot(path);
    else unhideFtRoot(path);
    return;
  }
  const row = e.target.closest('.ft-row');
  if (!row) return;
  const path = row.dataset.ftPath;
  const isDir = row.dataset.ftDir === '1';
  if (isDir) {
    ftToggleDir(path);
  } else {
    ftOpenFile(path);
    closeMobileSidebar();
  }
});

// === File Tabs ===
function ftOpenFile(filePath, targetPaneId) {
  const fileName = filePath.split('/').filter(Boolean).pop() || filePath;
  if (isBinary(fileName)) return; // Skip binary files
  // Dedup: if file already open, focus existing tab
  for (const tid in allTabs) {
    const t = allTabs[tid];
    if (t.type === 'file' && t.filePath === filePath) {
      focusTab(parseInt(tid));
      return;
    }
  }
  const paneId = targetPaneId || activePaneId || (panes[0] && panes[0].id);
  if (!paneId) return;
  const pane = panes.find(p => p.id === paneId);
  if (!pane) return;

  const id = _nextTabId++;
  allTabs[id] = { type: 'file', filePath, fileName, session: null, windowIndex: null, windowName: fileName };
  tabStates[id] = {
    rawContent: '', last: '', rawMode: false,
    pendingMsg: null, pendingTime: 0,
    awaitingResponse: false, lastOutputChange: 0,
    pollInterval: null, ccStatus: null,
    fileContent: null, fileLoaded: false, fileRawView: true,
    fileMtime: null, fileEditing: false, fileDirty: false,
    fileSaving: false, fileMtimeInterval: null,
  };
  pane.tabIds.push(id);

  // Create output element (before focusTab so the output div exists)
  const paneEl = document.getElementById('pane-' + paneId);
  const placeholder = paneEl.querySelector('.pane-placeholder');
  if (placeholder) placeholder.remove();
  const outEl = document.createElement('div');
  outEl.className = 'pane-output chat';
  outEl.id = 'tab-output-' + id;
  outEl.style.display = 'none';
  outEl.innerHTML = '<div class="turn assistant"><div class="turn-body"><p style="color:var(--text3)">Loading...</p></div></div>';
  paneEl.querySelector('.pane-input').before(outEl);

  focusTab(id);
  renderPaneTabs(paneId);
  loadFileTabContent(id);
  saveLayout();
}

async function loadFileTabContent(tabId) {
  const tab = allTabs[tabId];
  const state = tabStates[tabId];
  if (!tab || tab.type !== 'file' || !state) return;
  try {
    const r = await fetch('/api/files/read?path=' + encodeURIComponent(tab.filePath));
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      state.fileContent = null; state.fileLoaded = true;
      renderFileTabOutput(tabId, '<div style="padding:24px;color:var(--text3)">' + esc(err.detail || 'Cannot read file') + '</div>');
      return;
    }
    const data = await r.json();
    state.fileContent = data.content;
    state.fileMtime = data.mtime;
    state.fileLoaded = true;
    state.fileDirty = false;
    state.fileEditing = false;
    renderFileTabFormatted(tabId);
    startMtimePolling(tabId);
  } catch(e) {
    state.fileContent = null; state.fileLoaded = true;
    renderFileTabOutput(tabId, '<div style="padding:24px;color:var(--text3)">Error loading file</div>');
  }
}

function highlightMdRaw(text) {
  // VS Code-like syntax highlighting for raw markdown
  const lines = text.split('\\n');
  const out = [];
  let inFence = false;
  for (const line of lines) {
    const e = esc(line);
    // Fenced code blocks
    if (/^\\s*```/.test(line)) {
      inFence = !inFence;
      out.push('<span class="md-fence">' + e + '</span>');
      continue;
    }
    if (inFence) {
      out.push('<span class="md-fenced">' + e + '</span>');
      continue;
    }
    // Headings
    const hm = line.match(/^(#{1,6})\\s/);
    if (hm) {
      const marker = esc(hm[1]);
      const rest = esc(line.slice(hm[0].length));
      out.push('<span class="md-hm">' + marker + '</span> <span class="md-h">' + rest + '</span>');
      continue;
    }
    // Horizontal rules
    if (/^(---+|\\*\\*\\*+|___+)\\s*$/.test(line)) {
      out.push('<span class="md-hr">' + e + '</span>');
      continue;
    }
    // Blockquotes
    if (/^>/.test(line)) {
      out.push('<span class="md-blockquote">' + e + '</span>');
      continue;
    }
    // List items (bullet)
    const bm = line.match(/^(\\s*)([-*+])\\s/);
    if (bm) {
      const indent = esc(bm[1]);
      const rest = esc(line.slice(bm[0].length));
      out.push(indent + '<span class="md-bullet">' + esc(bm[2]) + '</span> ' + highlightMdInline(rest));
      continue;
    }
    // Numbered list
    const nm = line.match(/^(\\s*)(\\d+\\.)\\s/);
    if (nm) {
      const indent = esc(nm[1]);
      const rest = esc(line.slice(nm[0].length));
      out.push(indent + '<span class="md-bullet">' + esc(nm[2]) + '</span> ' + highlightMdInline(rest));
      continue;
    }
    // Table rows
    if (/^\\|/.test(line)) {
      out.push(e.replace(/\\|/g, '<span class="md-pipe">|</span>'));
      continue;
    }
    // Normal line — apply inline highlighting
    out.push(highlightMdInline(e));
  }
  return out.join('\\n');
}
function highlightMdInline(escaped) {
  // Bold **text** or __text__
  let s = escaped.replace(/\\*\\*(.+?)\\*\\*/g, '<span class="md-bm">**</span><span class="md-bold">$1</span><span class="md-bm">**</span>');
  s = s.replace(/__(.+?)__/g, '<span class="md-bm">__</span><span class="md-bold">$1</span><span class="md-bm">__</span>');
  // Italic *text* or _text_ (not inside bold markers)
  s = s.replace(/(?<![*_])\\*([^*]+?)\\*(?![*_])/g, '<span class="md-im">*</span><span class="md-italic">$1</span><span class="md-im">*</span>');
  s = s.replace(/(?<![*_])_([^_]+?)_(?![*_])/g, '<span class="md-im">_</span><span class="md-italic">$1</span><span class="md-im">_</span>');
  // Code `text`
  s = s.replace(/`([^`]+?)`/g, '<span class="md-cm">`</span><span class="md-code">$1</span><span class="md-cm">`</span>');
  // Links [text](url)
  s = s.replace(/\\[([^\\]]+?)\\]\\(([^)]+?)\\)/g, '<span class="md-link-bracket">[</span><span class="md-link-text">$1</span><span class="md-link-bracket">](</span><span class="md-link-url">$2</span><span class="md-link-bracket">)</span>');
  return s;
}
function fileExt(name) {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.substring(i + 1).toLowerCase() : '';
}
function isMarkdown(name) { return fileExt(name) === 'md'; }
function isBinary(name) {
  const bin = ['png','jpg','jpeg','gif','bmp','ico','webp','svg','mp3','mp4','wav','zip','gz','tar','dmg','exe','dll','so','dylib','o','a','pyc','pyo','class','woff','woff2','ttf','eot','pdf'];
  return bin.includes(fileExt(name));
}
function highlightCode(text, ext) {
  // Simple token-based syntax highlighter for code files
  const lines = text.split('\\n');
  const out = [];
  const kwSets = {
    py: { kw: /\\b(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|raise|yield|lambda|pass|break|continue|not|and|or|is|in|True|False|None|async|await|self)\\b/g,
          builtin: /\\b(print|len|range|str|int|float|list|dict|set|tuple|bool|open|type|isinstance|getattr|setattr|hasattr|super|enumerate|zip|map|filter|sorted|reversed|any|all|min|max|sum|abs|round|format|input|id|dir|vars|globals|locals|staticmethod|classmethod|property)\\b/g },
    js: { kw: /\\b(function|const|let|var|return|if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw|new|delete|typeof|instanceof|class|extends|import|export|from|default|async|await|yield|this|super|true|false|null|undefined|of|in|void)\\b/g,
          builtin: /\\b(console|document|window|fetch|Promise|Array|Object|String|Number|Boolean|RegExp|Map|Set|JSON|Math|Date|Error|setTimeout|setInterval|clearTimeout|clearInterval|parseInt|parseFloat|encodeURIComponent|decodeURIComponent)\\b/g },
    sh: { kw: /\\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|local|export|source|alias|unset|set|shift|exit|break|continue|in|until|select)\\b/g,
          builtin: /\\b(echo|cd|ls|rm|cp|mv|mkdir|cat|grep|sed|awk|find|xargs|sort|uniq|wc|head|tail|cut|tr|tee|chmod|chown|curl|wget|ssh|scp|git|docker|npm|pip|brew|apt|sudo|kill|ps|env|which|test|read)\\b/g },
    toml: { kw: /\\b(true|false)\\b/g, builtin: null },
    json: { kw: /\\b(true|false|null)\\b/g, builtin: null },
  };
  const alias = { ts: 'js', tsx: 'js', jsx: 'js', mjs: 'js', cjs: 'js', bash: 'sh', zsh: 'sh', yml: 'toml', yaml: 'toml', cfg: 'toml', ini: 'toml', conf: 'toml' };
  const lang = alias[ext] || ext;
  const kws = kwSets[lang] || null;
  // Comment patterns per language
  const lineComment = (lang === 'py' || lang === 'sh' || lang === 'toml') ? '#' : (lang === 'js') ? '//' : null;

  for (const line of lines) {
    let e = esc(line);
    // Comment detection (simple: line-level only)
    if (lineComment) {
      const stripped = line.trimStart();
      if (stripped.startsWith(lineComment)) {
        out.push('<span class="code-comment">' + e + '</span>');
        continue;
      }
    }
    // Strings — highlight quoted segments
    e = e.replace(/(&quot;)(.*?)(&quot;)/g, '<span class="code-string">$1$2$3</span>');
    e = e.replace(/(&#x27;|\\x27)(.*?)(&#x27;|\\x27)/g, '<span class="code-string">$1$2$3</span>');
    // Apply keyword highlighting if we have patterns
    if (kws) {
      if (kws.kw) e = e.replace(kws.kw, '<span class="code-kw">$&</span>');
      if (kws.builtin) e = e.replace(kws.builtin, '<span class="code-builtin">$&</span>');
    }
    // Numbers
    e = e.replace(/\\b(\\d+\\.?\\d*)\\b/g, function(m) {
      // Avoid highlighting numbers inside already-highlighted spans
      return '<span class="code-num">' + m + '</span>';
    });
    // Decorators (Python)
    if (lang === 'py') {
      e = e.replace(/^(\\s*)(@\\w+)/, '$1<span class="code-decorator">$2</span>');
    }
    out.push(e);
  }
  return out.join('\\n');
}
function renderFileTabFormatted(tabId) {
  const tab = allTabs[tabId];
  const state = tabStates[tabId];
  if (!tab || !state || state.fileContent == null) return;
  const md = isMarkdown(tab.fileName);
  let bodyHtml;
  if (state.fileEditing) {
    // Edit mode — textarea
    bodyHtml = '<div class="file-editor-wrap">'
      + '<textarea class="file-editor" id="file-editor-' + tabId + '" spellcheck="false">' + esc(state.fileContent) + '</textarea>'
      + '</div>';
  } else if (md && !state.fileRawView) {
    bodyHtml = '<div class="fb-reader"><div class="fb-reader-body">' + mdFile(state.fileContent) + '</div></div>';
  } else if (md && state.fileRawView) {
    bodyHtml = '<pre class="md-raw">' + highlightMdRaw(state.fileContent) + '</pre>';
  } else {
    const ext = fileExt(tab.fileName);
    bodyHtml = '<pre class="code-view">' + highlightCode(state.fileContent, ext) + '</pre>';
  }
  // Toolbar
  let toolbar = '<div class="file-tab-toolbar">'
    + '<span class="file-tab-path" title="' + esc(tab.filePath) + '">'
    + (state.fileDirty ? '<span class="file-dirty-dot"></span>' : '')
    + esc(tab.filePath) + '</span>';
  // View toggle (only in view mode for markdown)
  if (!state.fileEditing && md) {
    toolbar += '<div class="file-tab-toggle">'
      + '<button class="' + (state.fileRawView ? '' : 'active') + '" onclick="setFileTabView(' + tabId + ',false)">Formatted</button>'
      + '<button class="' + (state.fileRawView ? 'active' : '') + '" onclick="setFileTabView(' + tabId + ',true)">Raw</button>'
      + '</div>';
  }
  // Save button (when editing)
  if (state.fileEditing) {
    toolbar += '<button class="file-save-btn' + (state.fileDirty ? ' dirty' : '') + (state.fileSaving ? ' saving' : '')
      + '" onclick="fileSave(' + tabId + ')">' + (state.fileSaving ? 'Saving...' : 'Save') + '</button>';
  }
  // Edit/View toggle
  toolbar += '<button class="file-edit-btn' + (state.fileEditing ? ' active' : '')
    + '" onclick="fileToggleEdit(' + tabId + ')">' + (state.fileEditing ? 'View' : 'Edit') + '</button>';
  toolbar += '</div>';
  // External change warning bar
  let warningBar = '';
  if (state._externalChange) {
    warningBar = '<div class="file-external-change">'
      + '<span>File changed on disk</span>'
      + '<button onclick="fileReloadFromDisk(' + tabId + ')">Reload</button>'
      + '<button onclick="fileDismissWarning(' + tabId + ')" style="background:var(--surface);color:var(--text3)">Dismiss</button>'
      + '</div>';
  }
  renderFileTabOutput(tabId, toolbar + warningBar + bodyHtml);
  // Attach event handlers for editor textarea
  if (state.fileEditing) {
    const ta = document.getElementById('file-editor-' + tabId);
    if (ta) {
      ta.addEventListener('input', function() {
        state.fileContent = ta.value;
        state.fileDirty = true;
        updateFileTabDirtyDot(tabId);
        // Update save button
        const saveBtn = ta.closest('.pane-output').querySelector('.file-save-btn');
        if (saveBtn && !saveBtn.classList.contains('dirty')) saveBtn.classList.add('dirty');
      });
      ta.addEventListener('keydown', function(e) {
        if ((e.metaKey || e.ctrlKey) && e.key === 's') {
          e.preventDefault();
          fileSave(tabId);
        }
        // Tab key inserts tab character
        if (e.key === 'Tab' && !e.metaKey && !e.ctrlKey) {
          e.preventDefault();
          const start = ta.selectionStart;
          const end = ta.selectionEnd;
          ta.value = ta.value.substring(0, start) + '  ' + ta.value.substring(end);
          ta.selectionStart = ta.selectionEnd = start + 2;
          ta.dispatchEvent(new Event('input'));
        }
      });
      ta.focus();
    }
  }
}

function renderFileTabOutput(tabId, html) {
  const outEl = document.getElementById('tab-output-' + tabId);
  if (outEl) { outEl.innerHTML = html; outEl.scrollTop = 0; }
}

function setFileTabView(tabId, raw) {
  const state = tabStates[tabId];
  if (!state) return;
  if (state.fileDirty) {
    if (!confirm('Discard unsaved changes?')) return;
  }
  if (state.fileEditing) {
    state.fileEditing = false;
    state.fileDirty = false;
    updateFileTabDirtyDot(tabId);
  }
  state.fileRawView = raw;
  renderFileTabFormatted(tabId);
}

function fileToggleEdit(tabId) {
  const state = tabStates[tabId];
  if (!state) return;
  if (state.fileEditing) {
    // Exit edit mode
    if (state.fileDirty && !confirm('Discard unsaved changes?')) return;
    state.fileEditing = false;
    state.fileDirty = false;
    updateFileTabDirtyDot(tabId);
  } else {
    // Enter edit mode
    state.fileEditing = true;
  }
  renderFileTabFormatted(tabId);
}

async function fileSave(tabId) {
  const tab = allTabs[tabId];
  const state = tabStates[tabId];
  if (!tab || !state || !state.fileEditing || state.fileSaving) return;
  state.fileSaving = true;
  // Update save button UI
  const outEl = document.getElementById('tab-output-' + tabId);
  const saveBtn = outEl && outEl.querySelector('.file-save-btn');
  if (saveBtn) { saveBtn.textContent = 'Saving...'; saveBtn.classList.add('saving'); }
  try {
    const r = await fetch('/api/files/write', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: tab.filePath, content: state.fileContent, mtime: state.fileMtime })
    });
    if (r.status === 409) {
      const err = await r.json();
      if (confirm('File was modified on disk. Overwrite anyway?')) {
        // Retry without mtime check
        const r2 = await fetch('/api/files/write', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ path: tab.filePath, content: state.fileContent })
        });
        if (r2.ok) {
          const d2 = await r2.json();
          state.fileMtime = d2.mtime;
          state.fileDirty = false;
          state._externalChange = false;
          updateFileTabDirtyDot(tabId);
          if (saveBtn) { saveBtn.textContent = 'Save'; saveBtn.classList.remove('saving','dirty'); }
        }
      }
    } else if (r.ok) {
      const data = await r.json();
      state.fileMtime = data.mtime;
      state.fileDirty = false;
      state._externalChange = false;
      updateFileTabDirtyDot(tabId);
      if (saveBtn) { saveBtn.textContent = 'Save'; saveBtn.classList.remove('saving','dirty'); }
      // Remove warning bar if present
      const warn = outEl && outEl.querySelector('.file-external-change');
      if (warn) warn.remove();
    } else {
      const err = await r.json().catch(() => ({}));
      alert('Save failed: ' + (err.detail || 'Unknown error'));
    }
  } catch(e) {
    alert('Save failed: network error');
  }
  state.fileSaving = false;
  if (saveBtn) { saveBtn.textContent = 'Save'; saveBtn.classList.remove('saving'); }
}

function updateFileTabDirtyDot(tabId) {
  const state = tabStates[tabId];
  // Update pane tab dot
  const tabEl = document.querySelector('[data-tab-id="' + tabId + '"].pane-tab');
  if (tabEl) tabEl.classList.toggle('file-dirty', state && state.fileDirty);
  // Update toolbar dirty dot
  const outEl = document.getElementById('tab-output-' + tabId);
  if (outEl) {
    const dot = outEl.querySelector('.file-dirty-dot');
    if (state && state.fileDirty && !dot) {
      const pathEl = outEl.querySelector('.file-tab-path');
      if (pathEl) pathEl.insertAdjacentHTML('afterbegin', '<span class="file-dirty-dot"></span>');
    } else if ((!state || !state.fileDirty) && dot) {
      dot.remove();
    }
  }
}

function startMtimePolling(tabId) {
  const state = tabStates[tabId];
  if (!state) return;
  stopMtimePolling(tabId);
  state.fileMtimeInterval = setInterval(function() { checkFileMtime(tabId); }, 5000);
}

function stopMtimePolling(tabId) {
  const state = tabStates[tabId];
  if (!state || !state.fileMtimeInterval) return;
  clearInterval(state.fileMtimeInterval);
  state.fileMtimeInterval = null;
}

async function checkFileMtime(tabId) {
  const tab = allTabs[tabId];
  const state = tabStates[tabId];
  if (!tab || !state || tab.type !== 'file' || state.fileMtime == null) return;
  try {
    const r = await fetch('/api/files/mtime?path=' + encodeURIComponent(tab.filePath));
    if (!r.ok) return;
    const data = await r.json();
    if (Math.abs(data.mtime - state.fileMtime) > 0.01) {
      if (state.fileDirty || state.fileEditing) {
        // Show warning bar
        state._externalChange = true;
        const outEl = document.getElementById('tab-output-' + tabId);
        if (outEl && !outEl.querySelector('.file-external-change')) {
          const toolbar = outEl.querySelector('.file-tab-toolbar');
          if (toolbar) {
            toolbar.insertAdjacentHTML('afterend',
              '<div class="file-external-change">'
              + '<span>File changed on disk</span>'
              + '<button onclick="fileReloadFromDisk(' + tabId + ')">Reload</button>'
              + '<button onclick="fileDismissWarning(' + tabId + ')" style="background:var(--surface);color:var(--text3)">Dismiss</button>'
              + '</div>');
          }
        }
      } else {
        // Auto-reload
        fileReloadFromDisk(tabId);
      }
    }
  } catch(e) {}
}

async function fileReloadFromDisk(tabId) {
  const tab = allTabs[tabId];
  const state = tabStates[tabId];
  if (!tab || !state) return;
  state._externalChange = false;
  state.fileDirty = false;
  state.fileEditing = false;
  updateFileTabDirtyDot(tabId);
  try {
    const r = await fetch('/api/files/read?path=' + encodeURIComponent(tab.filePath));
    if (!r.ok) return;
    const data = await r.json();
    state.fileContent = data.content;
    state.fileMtime = data.mtime;
    renderFileTabFormatted(tabId);
  } catch(e) {}
}

function fileDismissWarning(tabId) {
  const state = tabStates[tabId];
  if (state) state._externalChange = false;
  const outEl = document.getElementById('tab-output-' + tabId);
  if (outEl) {
    const warn = outEl.querySelector('.file-external-change');
    if (warn) warn.remove();
  }
}

function statusLabel(cc_status) {
  if (cc_status === 'working') return 'Working';
  if (cc_status === 'thinking') return 'Thinking';
  if (cc_status === 'idle') return 'Standby';
  return '';
}
// === Hidden sessions ===
function getHiddenSessions() {
  try { return JSON.parse(prefs.getItem('hidden-sessions') || '[]'); } catch { return []; }
}
function setHiddenSessions(arr) {
  prefs.setItem('hidden-sessions', JSON.stringify(arr));
}
function hideSession(name) {
  const h = getHiddenSessions();
  if (!h.includes(name)) { h.push(name); setHiddenSessions(h); }
  renderSidebar();
}
function unhideSession(name) {
  setHiddenSessions(getHiddenSessions().filter(s => s !== name));
  renderSidebar();
}
let _hiddenExpanded = false;

// === Activity age formatting ===
function formatAge(seconds) {
  if (seconds == null || seconds < 0) return '';
  if (seconds < 60) return 'now';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
  return Math.floor(seconds / 86400) + 'd';
}
function ageFromTs(ts) {
  if (ts == null) return '';
  return formatAge(Math.floor(Date.now() / 1000) - ts);
}
// Update all .sb-activity spans in-place without full sidebar re-render
function updateSidebarAges() {
  if (!_dashboardData) return;
  for (const s of _dashboardData.sessions) {
    for (const w of s.windows) {
      const wid = s.name + ':' + w.index;
      const el = document.querySelector('.sb-activity[data-wid="' + wid + '"]');
      if (el) el.textContent = ageFromTs(w.gauge_last_ts || w.activity_ts);
    }
  }
}

function detectCCStatus(text) {
  // Quick client-side CC status detection from output text
  // Returns {status, contextPct, permMode, fresh} or null
  if (!isClaudeCode(text)) return null;
  const fresh = text.indexOf('\\u23fa') < 0;
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
  return { status, contextPct, permMode, fresh };
}
function updateSidebarStatus(session, windowIndex, ccStatus, contextPct, permMode) {
  const wid = session + ':' + windowIndex;
  const dot = document.querySelector('.sb-win-dot[data-wid="' + wid + '"]');
  if (dot) { dot.className = 'sb-win-dot ' + (ccStatus == null ? 'none' : (ccStatus || 'idle')); }
  // Update context % from status bar if we have it and no gauge data present
  const winEl = dot && dot.closest('.sb-win');
  if (winEl && contextPct != null) {
    let ctx = winEl.querySelector('.sb-ctx');
    if (!ctx) {
      ctx = document.createElement('div'); ctx.className = 'sb-ctx';
      const btn = winEl.querySelector('.sb-win-detail-btn');
      winEl.insertBefore(ctx, btn);
    }
    const cls = _ctxCls(contextPct);
    ctx.className = 'sb-ctx' + (cls ? ' ' + cls : '');
    ctx.textContent = contextPct + '%';
  }
  // Update perm mode label
  const permEl = document.querySelector('.sb-perm[data-wid="' + wid + '"]');
  if (permEl && permMode) {
    permEl.textContent = permMode;
    permEl.className = 'sb-perm' + (/dangerously|skip|bypass/i.test(permMode) ? ' danger' : '');
  }
}

// === Clean/parse (unchanged core logic) ===
function cleanTerminal(raw) {
  let lines = raw.split('\\n');
  // Mark lines inside box-drawing tables (┌...┘) — protect from TUI chrome stripping
  const inTbl = new Array(lines.length).fill(false);
  for (let i = 0; i < lines.length; i++) {
    if (_tblStartRe.test(lines[i])) {
      inTbl[i] = true;
      for (let j = i + 1; j < lines.length; j++) { inTbl[j] = true; if (_tblEndRe.test(lines[j])) break; }
    }
  }
  const result = [];
  for (let i = 0; i < lines.length; i++) {
    if (inTbl[i]) { result.push(lines[i]); continue; }
    const l = lines[i];
    // Remove rounded border lines: ╭───╮, ╰───╯, ├───┤
    if (/^\\s*[\\u256d\\u2570\\u251c][\\u2500\\u2504\\u2501]+[\\u256e\\u256f\\u2524]\\s*$/.test(l)) continue;
    // Strip │ borders from line start/end
    let cleaned = l.replace(/^\\s*\\u2502\\s?/, '').replace(/\\s?\\u2502\\s*$/, '');
    // Remove TUI divider lines: pure box-drawing or labeled dividers
    const t = cleaned.trim();
    if (t) { const bc = (t.match(/[\\u2500-\\u257f]/g) || []).length; if (bc > 20 && bc > t.length * 0.6) continue; }
    result.push(cleaned);
  }
  let text = result.join('\\n');
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
  // Pre-scan: identify real user prompts (❯ followed by ⏺ before next ❯).
  // Menu selection ❯ lines (plan approval, AskUserQuestion) have no ⏺ after them.
  const realPrompts = new Set();
  for (let i = 0; i < lines.length; i++) {
    const r = lines[i].replace(/\\u00a0/g, ' ');
    if (/^\\u276f/.test(r)) {
      for (let j = i + 1; j < lines.length; j++) {
        const jr = lines[j].replace(/\\u00a0/g, ' ');
        if (/^\\u276f/.test(jr)) break;
        if (/^\\u23fa/.test(jr.trim())) { realPrompts.add(i); break; }
      }
    }
  }
  // Find last ❯ line index — if not a realPrompt, it's the user's just-submitted input
  // (not yet acknowledged by CC with ⏺). Truncate lines there so the user's pending
  // input (which may span multiple lines) never leaks into assistant turns.
  let lastPromptIdx = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (/^\\u276f/.test(lines[i].replace(/\\u00a0/g, ' '))) { lastPromptIdx = i; break; }
  }
  if (lastPromptIdx >= 0 && !realPrompts.has(lastPromptIdx)) {
    lines = lines.slice(0, lastPromptIdx);
  }
  const turns = []; let cur = null, inTool = false, sawStatus = false;
  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    const raw = line.replace(/\\u00a0/g, ' ');
    const t = raw.trim();
    if (/^[\\u2500-\\u257f]{3,}$/.test(t) && t.length > 20 && !/[\\u250c\\u2510\\u2514\\u2518\\u252c\\u253c\\u2534]/.test(t)) continue;
    if (/^[\\u23f5]/.test(t)) continue;
    if (/^\\u2026/.test(t)) continue;
    if (!t) { if (cur && cur.role === 'assistant' && !inTool) cur.lines.push(''); continue; }
    if (/^[\\u2720-\\u273f]/.test(t)) { sawStatus = true; continue; }
    if (/^\\u00b7/.test(t)) { sawStatus = true; continue; }
    if (/esc to interrupt/.test(t)) continue;
    if (/^\\u276f/.test(raw) && realPrompts.has(li)) {
      if (cur) turns.push(cur);
      const msg = t.replace(/^\\u276f\\s*/, '').trim();
      // Skip CC slash commands (/clear, /help, etc.) — they're meta, not conversation
      if (msg.startsWith('/')) { cur = null; inTool = false; sawStatus = false; continue; }
      cur = { role: 'user', lines: msg ? [msg] : [] }; inTool = false; sawStatus = false; continue;
    }
    // Non-prompt ❯ line (menu selection) — treat as regular text, strip the ❯
    if (/^\\u276f/.test(raw) && !realPrompts.has(li)) {
      const menuText = t.replace(/^\\u276f\\s*/, '').trim();
      if (cur && !inTool && menuText) cur.lines.push(menuText);
      continue;
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
function renderOutput(raw, targetEl, state, tabId) {
  // Process awaitingResponse/pendingMsg regardless of view mode (queue, notifications depend on this)
  const clean = cleanTerminal(raw);
  const wasAwaiting = state.awaitingResponse;
  if (state.awaitingResponse) {
    const elapsed = Date.now() - state.pendingTime;
    if (elapsed > 3000 && isIdle(clean)) state.awaitingResponse = false;
    const staleAge = state.lastOutputChange > 0 ? Date.now() - state.lastOutputChange : 0;
    if (elapsed > 5000 && staleAge > 30000) state.awaitingResponse = false;
    if (elapsed > 180000) state.awaitingResponse = false;
  }
  if (wasAwaiting && !state.awaitingResponse && tabId) {
    onQueueTaskCompleted(tabId);
    notifyDone(tabId);
  }
  if (state.rawMode) {
    targetEl.className = 'pane-output raw';
    // Clean up for mobile readability: trim trailing whitespace per line,
    // collapse excessive blank lines, truncate long horizontal dividers,
    // rejoin CC TUI word-wrapped prose lines
    let dLines = raw.split('\\n').map(l => {
      l = l.trimEnd();
      if (l.length > 40 && /^\\u2500+$/.test(l)) l = '\\u2500'.repeat(40);
      return l;
    });
    // Join CC TUI word-wrap continuations: lines near the terminal wrap width
    // followed by indented continuation = same sentence split at wrap point.
    // Use 85% of max line length as threshold — CC word-wraps at word boundaries,
    // so lines can end well short of terminal width (up to longest-word gap).
    const wrapW = Math.max(...dLines.map(l => l.length)) * 0.85;
    let jLines = [dLines[0]];
    for (let k = 1; k < dLines.length; k++) {
      const prev = jLines[jLines.length - 1];
      const cur = dLines[k];
      if (prev.length >= wrapW && /^( {2,}\\S|\\u23FA|\\u276F)/.test(prev) && /^ {2,}[a-zA-Z]/.test(cur)) {
        jLines[jLines.length - 1] = prev + ' ' + cur.trimStart();
      } else { jLines.push(cur); }
    }
    let display = jLines.join('\\n').replace(/\\n{4,}/g, '\\n\\n\\n');
    if (_tblStartRe.test(display)) { targetEl.innerHTML = renderRawWithTables(display); }
    else { targetEl.textContent = display; }
    if (_fileLinksEnabled) linkifyFilePaths(targetEl, getTabCwd(tabId));
    return;
  }
  targetEl.className = 'pane-output chat';
  let html = '';
  if (isClaudeCode(clean)) {
    const turns = parseCCTurns(clean);
    if (state.pendingMsg) {
      const snippet = state.pendingMsg.substring(0, 20);
      const userTurns = turns.filter(t => t.role === 'user');
      const inUserTurn = userTurns.some(u => u.lines.join(' ').includes(snippet));
      if (inUserTurn) state.pendingMsg = null;
      // Safety timeout: 10s max for pendingMsg display
      else if (state.pendingTime && (Date.now() - state.pendingTime) > 10000)
        state.pendingMsg = null;
      // Scrub: if pending text leaked into the last assistant turn, strip those lines
      if (state.pendingMsg && turns.length > 0) {
        const last = turns[turns.length - 1];
        if (last.role === 'assistant') {
          last.lines = last.lines.filter(l => !l.includes(snippet));
        }
      }
    }
    let lastRole = '';
    for (const t of turns) {
      const text = t.lines.join('\\n').trim();
      if (!text) continue;
      if (t.role === 'user') {
        html += '<div class="turn user"><div class="turn-label">You</div><div class="turn-body">' + esc(text) + '</div></div>';
      } else {
        const label = lastRole !== 'assistant' ? '<div class="turn-label">Claude</div>' : '';
        // Interactive prompts (AskUserQuestion/plan approval) have ❯ in text —
        // render as plain text with line breaks to avoid markdown list mangling
        const body = /\\u276f/.test(text) ? esc(text).replace(/\\n/g, '<br>') : md(text);
        html += '<div class="turn assistant">' + label + '<div class="turn-body">' + body + '</div></div>';
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
  if (_fileLinksEnabled) linkifyFilePaths(targetEl, getTabCwd(tabId));
}

// === Pane management ===
function createPane(parentEl) {
  if (panes.length >= 12) return null;
  const id = _nextPaneId++;
  panes.push({ id, tabIds: [], activeTabId: null });
  const el = document.createElement('div');
  el.className = 'pane';
  el.id = 'pane-' + id;
  el.innerHTML = '<div class="pane-tab-bar"></div>'
    + '<div class="pane-placeholder">Open a window from the sidebar</div>'
    + '<div class="pane-input">'
    + '<div class="pane-gauge" data-pane-gauge="' + id + '"></div>'
    + '<div class="pane-input-row"><textarea rows="1" placeholder="Enter command..."'
    + ' autocomplete="off" enterkeyhint="send"></textarea>'
    + '<button class="pane-send" aria-label="Send">' + SEND_SVG + '</button></div>'
    + '<div class="pane-toolbar">'
    + '<button class="pill" onclick="togglePaneTray(this,\\'keys\\')">Keys</button>'
    + '<button class="pill" onclick="togglePaneTray(this,\\'cmds\\')">Commands</button>'
    + '</div>'
    + '<div class="pane-tray pane-tray-keys">'
    + '<button class="pill" onclick="keyActive(\\'Enter\\')">Return</button>'
    + '<button class="pill danger" onclick="keyActive(\\'C-c\\')">Ctrl-C</button>'
    + '<button class="pill" onclick="keyActive(\\'Up\\')">Up</button>'
    + '<button class="pill" onclick="keyActive(\\'Down\\')">Down</button>'
    + '<button class="pill" onclick="keyActive(\\'Left\\')">Left</button>'
    + '<button class="pill" onclick="keyActive(\\'Right\\')">Right</button>'
    + '<button class="pill" onclick="keyActive(\\'Tab\\')">Tab</button>'
    + '<button class="pill" onclick="keyActive(\\'Escape\\')">Esc</button>'
    + '</div>'
    + '<div class="pane-tray pane-tray-cmds">'
    + '<button class="pill" onclick="panePrefill(this,\\'/_my_wrap_up\\')">Wrap Up</button>'
    + '<button class="pill" onclick="panePrefill(this,\\'/clear\\')">Clear</button>'
    + '<button class="pill" onclick="panePrefill(this,\\'/exit\\')">Exit</button>'
    + '<button class="pill" onclick="sendResumeActive()">Resume</button>'
    + '</div>'
    + '</div>'
    + '<div class="drop-indicator"></div>';
  (parentEl || panesContainer).appendChild(el);
  // Pane input handlers
  const ta = el.querySelector('.pane-input textarea');
  const sendBtn = el.querySelector('.pane-send');
  setupTextareaInput(ta, () => sendToPane(id));
  sendBtn.addEventListener('click', () => sendToPane(id));
  // Click anywhere on pane to focus it
  el.addEventListener('mousedown', () => focusPane(id));
  // Drop target with vertical split detection
  el.addEventListener('dragover', e => {
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    const relY = (e.clientY - rect.top) / rect.height;
    const indicator = el.querySelector('.drop-indicator');
    if (relY > 0.65 && panes.length < 12) {
      indicator.style.top = '50%'; indicator.style.bottom = '0';
      indicator.style.display = 'block';
      el.classList.remove('drag-over');
      el._dropZone = 'bottom';
    } else if (relY < 0.35 && panes.length < 12 && el.parentElement.classList.contains('pane-stack')) {
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
  if (panes.length >= 12) return;
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
  // Reset inline sizes so remaining panes fill freed space (divider drag sets flex:none + px sizes)
  document.querySelectorAll('.pane, .pane-stack').forEach(el => {
    el.style.flex = ''; el.style.width = ''; el.style.height = '';
  });
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
    rawContent: '', last: '', rawMode: true,
    pendingMsg: null, pendingTime: 0,
    awaitingResponse: false, lastOutputChange: 0,
    pollInterval: null, ccStatus: null,
    _scrollToBottom: true,
  };
  pane.tabIds.push(id);

  // Create output element (before focusTab so the output div exists)
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
  // Warn if file tab has unsaved changes
  const _cState = tabStates[tabId];
  if (_cState && _cState.fileDirty) {
    if (!confirm('This file has unsaved changes. Close anyway?')) return;
  }
  stopTabPolling(tabId);
  stopMtimePolling(tabId);
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
      // Restore draft text for newly active tab
      const pe2 = document.getElementById('pane-' + pane.id);
      const ta2 = pe2 && pe2.querySelector('.pane-input textarea');
      if (ta2 && pane.activeTabId && tabStates[pane.activeTabId]) {
        ta2.value = tabStates[pane.activeTabId].draft || '';
        ta2.style.height = 'auto'; ta2.dispatchEvent(new Event('input'));
      } else if (ta2) {
        ta2.value = ''; ta2.style.height = 'auto'; ta2.dispatchEvent(new Event('input'));
      }
      if (M && pane.activeTabId && tabStates[pane.activeTabId]) {
        M.value = tabStates[pane.activeTabId].globalDraft || '';
        M.style.height = 'auto'; M.dispatchEvent(new Event('input'));
      } else if (M) {
        M.value = ''; M.style.height = 'auto'; M.dispatchEvent(new Event('input'));
      }
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
      // Update file-tab-active class on pane
      const pe = document.getElementById('pane-' + pane.id);
      const newActive = pane.activeTabId ? allTabs[pane.activeTabId] : null;
      if (pe) pe.classList.toggle('file-tab-active', newActive && newActive.type === 'file');
      renderPaneTabs(pane.id);
      showActiveTabOutput(pane.id);
      // Update view label for new active tab
      if (pane.activeTabId) {
        const st = tabStates[pane.activeTabId];
        if (newActive && newActive.type === 'file') {
          document.getElementById('view-label').textContent = isMarkdown(newActive.fileName) ? (st && st.fileRawView ? 'Raw' : 'Formatted') : 'Source';
        } else if (st) {
          document.getElementById('view-label').textContent = st.rawMode ? 'Raw' : 'Clean';
        }
      }
    }
  }
  if (!skipRender) { renderSidebar(); updatePolling(); updateLayout(); }
  saveLayout();
}

function focusTab(tabId) {
  // Unhide session if tab's session is hidden
  const ft = allTabs[tabId];
  if (ft && ft.type !== 'file' && getHiddenSessions().includes(ft.session)) unhideSession(ft.session);
  // Find pane
  for (const p of panes) {
    if (p.tabIds.includes(tabId)) {
      const tabChanged = p.activeTabId !== tabId;
      // Save draft text + scroll position from old tab before switching
      if (tabChanged && p.activeTabId && tabStates[p.activeTabId]) {
        const paneEl = document.getElementById('pane-' + p.id);
        const ta = paneEl && paneEl.querySelector('.pane-input textarea');
        // If a send is in-flight, textarea was cleared optimistically — use backup text, not empty textarea
        const _st = tabStates[p.activeTabId];
        tabStates[p.activeTabId].draft = _st._sendingText || (ta ? ta.value : '');
        // Also save global textarea draft
        if (M) tabStates[p.activeTabId].globalDraft = _st._sendingText || M.value;
      }
      p.activeTabId = tabId;
      focusPane(p.id);
      if (tabChanged && _sidebarView === 'sessions') renderSidebar();
      renderPaneTabs(p.id);
      showActiveTabOutput(p.id, true);
      // Toggle file-tab-active class on pane element (hides per-pane input bar)
      const paneEl = document.getElementById('pane-' + p.id);
      if (paneEl) paneEl.classList.toggle('file-tab-active', ft && ft.type === 'file');
      updateLayout(); // also hides global bar for file tabs in single-pane mode
      if (ft && ft.type !== 'file') {
        // Close/reopen notepad based on per-tab state
        const npPanel = document.getElementById('pane-' + p.id)?.querySelector('.notepad-panel');
        if (tabChanged) {
          if (npPanel && npPanel.classList.contains('open')) {
            npPanel.classList.remove('open');
            document.getElementById('pane-' + p.id)?.querySelector('.pane-notepad-btn')?.classList.remove('active');
          }
          if (tabStates[tabId] && tabStates[tabId].notepadOpen) {
            toggleNotepad(p.id);
          }
        } else {
          updateNotepadContent(p.id);
        }
        updateQueueContent(p.id);
      }
      updatePolling();
      // Update view label
      const state = tabStates[tabId];
      if (ft && ft.type === 'file') {
        if (isMarkdown(ft.fileName)) {
          document.getElementById('view-label').textContent = state && state.fileRawView ? 'Raw' : 'Formatted';
        } else {
          document.getElementById('view-label').textContent = 'Source';
        }
      } else if (state) {
        document.getElementById('view-label').textContent = state.rawMode ? 'Raw' : 'Clean';
      }
      // Restore draft text for new tab
      if (tabChanged && state) {
        const paneEl = document.getElementById('pane-' + p.id);
        const ta = paneEl && paneEl.querySelector('.pane-input textarea');
        if (ta) { ta.value = state.draft || ''; ta.style.height = 'auto'; ta.dispatchEvent(new Event('input')); }
        if (M) { M.value = state.globalDraft || ''; M.style.height = 'auto'; M.dispatchEvent(new Event('input')); }
      }
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
  updateQueueContent(targetPaneId);
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
    const st = tabStates[tid];
    const isFile = tab.type === 'file';
    const dotClass = isFile ? 'none' : (st && st.ccStatus ? st.ccStatus : 'none');
    html += '<div class="pane-tab' + (active ? ' active' : '') + (isFile ? ' file-tab' : '') + '" draggable="true"'
      + ' data-tab-id="' + tid + '"'
      + ' onclick="focusTab(' + tid + ')">'
      + '<span class="pane-tab-dot ' + dotClass + '" data-tab-dot="' + tid + '"></span>'
      + '<span class="pane-tab-name"' + (isFile ? ' title="' + esc(tab.filePath) + '"' : '') + '>' + esc(tab.windowName) + '</span>'
      + '<span class="pane-tab-close" onclick="event.stopPropagation();closeTab(' + tid + ')">&times;</span>'
      + '</div>';
  }
  // Notepad/Queue/Refresh buttons — only for terminal tabs
  const activeIsFile = pane.activeTabId && allTabs[pane.activeTabId] && allTabs[pane.activeTabId].type === 'file';
  if (pane.activeTabId && !activeIsFile) {
    html += '<button class="pane-notepad-btn' + (paneEl.querySelector('.notepad-panel.open') ? ' active' : '') + '" onclick="toggleNotepad(' + paneId + ')" title="Notepad">NOTES</button>';
    const qs = _queueStates[pane.activeTabId];
    const qOpen = paneEl.querySelector('.queue-panel.open');
    const qPlaying = qs && qs.playing;
    const qRemaining = qs ? qs.items.filter(i => !i.done).length : 0;
    html += '<button class="pane-queue-btn' + (qOpen ? ' active' : '') + (qPlaying ? ' playing' : '') + '" onclick="toggleQueue(' + paneId + ')" title="Task Queue">QUEUE' + (qRemaining > 0 ? ' ' + qRemaining : '') + '</button>';
    html += '<button class="pane-refresh-btn" onclick="hardRefresh(' + paneId + ')" title="Refresh">&#x21bb;</button>';
  } else if (pane.activeTabId && activeIsFile) {
    html += '<button class="pane-refresh-btn" onclick="hardRefresh(' + paneId + ')" title="Reload file">&#x21bb;</button>';
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
      _dragSrcTabId = parseInt(tab.dataset.tabId);
    });
    tab.addEventListener('dragend', () => {
      tab.style.opacity = '';
      _dragSrcTabId = null;
      tabBar.querySelectorAll('.drag-over-left,.drag-over-right').forEach(t => { t.classList.remove('drag-over-left','drag-over-right'); });
      // Flush deferred renders after drag completes
      if (_sidebarView === 'sessions') renderSidebar();
      else renderFileTree();
    });
    tab.addEventListener('dragover', e => {
      e.preventDefault();
      // pane (from closure) already contains this tab — just check if src is also here
      if (_dragSrcTabId && pane.tabIds.includes(_dragSrcTabId)) {
        e.stopPropagation();
        e.dataTransfer.dropEffect = 'move';
        tabBar.querySelectorAll('.drag-over-left,.drag-over-right').forEach(t => { t.classList.remove('drag-over-left','drag-over-right'); });
        const rect = tab.getBoundingClientRect();
        const onLeft = e.clientX < rect.left + rect.width / 2;
        tab.classList.add(onLeft ? 'drag-over-left' : 'drag-over-right');
      }
    });
    tab.addEventListener('dragleave', () => { tab.classList.remove('drag-over-left','drag-over-right'); });
    tab.addEventListener('drop', e => {
      const onLeft = tab.classList.contains('drag-over-left');
      tab.classList.remove('drag-over-left','drag-over-right');
      const srcTabId = parseInt(e.dataTransfer.getData('text/plain'));
      const dstTabId = parseInt(tab.dataset.tabId);
      if (srcTabId === dstTabId) return;
      const pane = panes.find(p => p.tabIds.includes(srcTabId) && p.tabIds.includes(dstTabId));
      if (!pane) return; // cross-pane drops bubble to pane-level handler
      e.preventDefault();
      e.stopPropagation();
      const srcIdx = pane.tabIds.indexOf(srcTabId);
      pane.tabIds.splice(srcIdx, 1);
      let dstIdx = pane.tabIds.indexOf(dstTabId);
      if (!onLeft) dstIdx += 1;
      pane.tabIds.splice(dstIdx, 0, srcTabId);
      renderPaneTabs(pane.id);
      saveLayout();
    });
  });
}

function showActiveTabOutput(paneId, scrollToBottom) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  paneEl.querySelectorAll('.pane-output').forEach(o => o.style.display = 'none');
  if (pane.activeTabId) {
    const outEl = document.getElementById('tab-output-' + pane.activeTabId);
    if (outEl) {
      outEl.style.display = '';
      if (scrollToBottom) {
        const st = tabStates[pane.activeTabId];
        if (st) st._scrollToBottom = true;
        outEl.scrollTop = outEl.scrollHeight;
      }
    }
  }
  // Ensure file-tab-active class matches active tab type
  const at = pane.activeTabId && allTabs[pane.activeTabId];
  paneEl.classList.toggle('file-tab-active', at && at.type === 'file');
  updatePaneGauge(paneId);
}

// Context % remaining: high=green, mid=orange, low=red
function _ctxCls(pct) { return pct <= 10 ? 'critical' : pct <= 25 ? 'low' : ''; }

function _gaugeHtml(tab) {
  if (!tab || tab.type === 'file' || !_dashboardData) return '';
  const sess = _dashboardData.sessions.find(s => s.name === tab.session);
  const win = sess && sess.windows.find(w => w.index === tab.windowIndex);
  if (!win || win.gauge_context_pct == null) return '';
  const pct = Math.round(win.gauge_context_pct);
  const cls = _ctxCls(pct);
  let html = '<span class="pg-pct' + (cls ? ' ' + cls : '') + '">' + pct + '% context left</span>';
  if (win.gauge_est_turns != null) {
    html += ' <span class="pg-detail">~' + win.gauge_est_turns + ' turns left</span>';
  }
  return html;
}

function updatePaneGauge(paneId) {
  const el = document.querySelector('[data-pane-gauge="' + paneId + '"]');
  if (!el) return;
  const pane = panes.find(p => p.id === paneId);
  el.innerHTML = (pane && pane.activeTabId) ? _gaugeHtml(allTabs[pane.activeTabId]) : '';
}

function updateAllPaneGauges() {
  for (const p of panes) updatePaneGauge(p.id);
  // Update global bar gauge (single-pane mode)
  const gg = document.getElementById('global-gauge');
  if (gg && panes.length === 1 && panes[0].activeTabId) {
    gg.innerHTML = _gaugeHtml(allTabs[panes[0].activeTabId]);
  } else if (gg) { gg.innerHTML = ''; }
}

// === Layout persistence ===
let _restoringLayout = false;
function savePaneData(p) {
  return {
    tabIds: p.tabIds.map(tid => {
      const t = allTabs[tid];
      if (!t) return null;
      if (t.type === 'file') return { type: 'file', filePath: t.filePath, fileName: t.fileName };
      const st = tabStates[tid];
      return { session: t.session, windowIndex: t.windowIndex, windowName: t.windowName, rawMode: st ? st.rawMode : true };
    }).filter(Boolean),
    activeTab: p.activeTabId ? (() => {
      const t = allTabs[p.activeTabId];
      if (!t) return null;
      if (t.type === 'file') return { type: 'file', filePath: t.filePath };
      return { session: t.session, windowIndex: t.windowIndex };
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
    const pane = panes.find(p => p.id === paneId);
    if (pane && pane.activeTabId && tabStates[pane.activeTabId]) tabStates[pane.activeTabId].notepadOpen = false;
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
      if (key) try { prefs.setItem(key, this.value); } catch(e) {}
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
        try { prefs.setItem('notepad:size', JSON.stringify({
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
      const saved = prefs.getItem('notepad:size');
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
    try { ta.value = prefs.getItem(key) || ''; } catch(e) { ta.value = ''; }
  } else { ta.value = ''; }
  panel.classList.add('open');
  paneEl.querySelector('.pane-notepad-btn')?.classList.add('active');
  if (tabStates[pane.activeTabId]) tabStates[pane.activeTabId].notepadOpen = true;
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
    try { ta.value = prefs.getItem(key) || ''; } catch(e) { ta.value = ''; }
  } else { ta.value = ''; }
}

// === Master Notes ===
function toggleMasterNotes() {
  const container = document.getElementById('master-notes-container');
  let panel = container.querySelector('.master-notes-panel');
  const btn = document.getElementById('master-notes-btn');
  if (panel && panel.classList.contains('open')) {
    panel.classList.remove('open');
    btn?.classList.remove('active');
    return;
  }
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'master-notes-panel';
    panel.innerHTML = '<div class="notepad-header"><span>Master Notes</span>'
      + '<button class="notepad-close" onclick="toggleMasterNotes()">&times;</button></div>'
      + '<textarea placeholder="Global notes across all sessions..."></textarea>'
      + '<div class="notepad-resize"></div>';
    panel.querySelector('textarea').addEventListener('input', function() {
      try { prefs.setItem('master-notepad', this.value); } catch(e) {}
    });
    // Resize handle (vertical only)
    const handle = panel.querySelector('.notepad-resize');
    let sy, sh;
    handle.addEventListener('pointerdown', function(e) {
      e.preventDefault();
      sy = e.clientY; sh = panel.offsetHeight;
      function onMove(ev) {
        panel.style.height = Math.max(120, sh + (ev.clientY - sy)) + 'px';
      }
      function onUp() {
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        try { prefs.setItem('master-notepad:size', panel.style.height); } catch(e) {}
      }
      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp);
    });
    // Restore saved size
    try {
      const saved = prefs.getItem('master-notepad:size');
      if (saved) panel.style.height = saved;
    } catch(e) {}
    container.appendChild(panel);
  }
  // Load content
  const ta = panel.querySelector('textarea');
  try { ta.value = prefs.getItem('master-notepad') || ''; } catch(e) { ta.value = ''; }
  panel.classList.add('open');
  btn?.classList.add('active');
  ta.focus();
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
        const saved = prefs.getItem(key);
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
  try { prefs.setItem(key, JSON.stringify(qs.items)); } catch(e) {}
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
  // Save draft text and user-set list height before rebuild
  const prevAdd = panel.querySelector('.queue-add textarea');
  const draftText = prevAdd ? prevAdd.value : '';
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
  // Restore draft text in add-task textarea
  if (draftText) {
    const newAdd = panel.querySelector('.queue-add textarea');
    if (newAdd) { newAdd.value = draftText; newAdd.style.height = 'auto'; newAdd.style.height = Math.min(newAdd.scrollHeight, 80) + 'px'; }
  }
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
  const _p = paneForTab(tabId);
  if (_p) { renderQueuePanel(_p.id); renderPaneTabs(_p.id); }
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
    const _p = paneForTab(tabId);
    if (_p) renderQueuePanel(_p.id);
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
  saveQueue(tabId);
  const _p = paneForTab(tabId);
  if (_p) { renderQueuePanel(_p.id); renderPaneTabs(_p.id); }
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

async function tryDispatchNext(tabId) {
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
    const _p = paneForTab(tabId);
    if (_p) { renderQueuePanel(_p.id); renderPaneTabs(_p.id); }
    return;
  }
  qs.currentIdx = nextIdx;
  const text = 'please execute this task: ' + qs.items[nextIdx].text;
  state.pendingMsg = text; state.pendingTime = Date.now(); state.awaitingResponse = true;
  const outEl = document.getElementById('tab-output-' + tabId);
  if (outEl) { renderOutput(state.rawContent || state.last, outEl, state, tabId); outEl.scrollTop = outEl.scrollHeight; }
  try {
    await fetch('/api/send', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ cmd: text, session: tab.session, window: tab.windowIndex, windowName: tab.windowName || '' })
    });
  } catch(e) { pauseQueue(tabId); }
  const _p2 = paneForTab(tabId);
  if (_p2) renderQueuePanel(_p2.id);
}

function notifyDone(tabId) {
  const tab = allTabs[tabId];
  if (!tab) return;
  // Browser notification (desktop, when tab not visible)
  if (typeof Notification !== 'undefined' && Notification.permission === 'granted' && document.hidden) {
    try { new Notification('Claude Code done', { body: (tab.windowName || tabId) + ' finished' }); } catch(e) {}
  }
  // Server notification (macOS osascript + ntfy)
  try {
    fetch('/api/notify', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ session: tab.session, window: tab.windowIndex, windowName: tab.windowName || '' })
    });
  } catch(e) {}
}

function onQueueTaskCompleted(tabId) {
  const qs = _queueStates[tabId];
  if (!qs || !qs.playing) return;
  if (qs.currentIdx !== null && qs.currentIdx < qs.items.length) {
    qs.items[qs.currentIdx].done = true;
    qs.currentIdx = null;
    saveQueue(tabId);
  }
  const _p = paneForTab(tabId);
  if (_p) renderQueuePanel(_p.id);
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
  // Hide global bar if multi-pane OR if single-pane active tab is a file
  const activeFileTab = !multiPane && activePaneId && (() => {
    const p = panes.find(x => x.id === activePaneId);
    return p && p.activeTabId && allTabs[p.activeTabId] && allTabs[p.activeTabId].type === 'file';
  })();
  bar.classList.toggle('hidden', multiPane || !!activeFileTab);
  document.querySelectorAll('.pane-input').forEach(pi => {
    pi.classList.toggle('visible', multiPane);
  });
  // Ensure all panes have tab bars rendered (empty panes need close button)
  for (const p of panes) renderPaneTabs(p.id);
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
    // Use px during drag for smooth feel
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
      // Convert px to % so panes scale with window resize
      if (prevEl && nextEl) {
        const parent = prevEl.parentElement;
        if (parent) {
          const total = dir === 'col' ? parent.clientWidth : parent.clientHeight;
          const pPx = dir === 'col' ? prevEl.getBoundingClientRect().width : prevEl.getBoundingClientRect().height;
          const nPx = dir === 'col' ? nextEl.getBoundingClientRect().width : nextEl.getBoundingClientRect().height;
          const pPct = (pPx / total * 100).toFixed(2) + '%';
          const nPct = (nPx / total * 100).toFixed(2) + '%';
          if (dir === 'col') {
            prevEl.style.width = pPct; nextEl.style.width = nPct;
          } else {
            prevEl.style.height = pPct; nextEl.style.height = nPct;
          }
        }
      }
    }
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
  });
}

// === Polling ===
function startTabPolling(tabId) {
  const tab = allTabs[tabId];
  if (tab && tab.type === 'file') return; // No polling for file tabs
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
  // File tab: re-fetch file content
  if (tab.type === 'file') {
    if (state.fileDirty && !confirm('Discard unsaved changes and reload?')) return;
    state.fileLoaded = false; state.fileEditing = false; state.fileDirty = false;
    state._externalChange = false;
    updateFileTabDirtyDot(tabId);
    loadFileTabContent(tabId);
    return;
  }
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
    if (live) {
      updateSidebarStatus(tab.session, tab.windowIndex, live.fresh ? null : live.status, live.contextPct, live.permMode);
      const effectiveStatus = live.fresh ? null : live.status;
      if (state.ccStatus !== effectiveStatus) {
        state.ccStatus = effectiveStatus;
        const dot = document.querySelector('[data-tab-dot="' + tabId + '"]');
        if (dot) dot.className = 'pane-tab-dot ' + (effectiveStatus || 'none');
      }
    } else if (state.ccStatus !== null) {
      state.ccStatus = null;
      const dot = document.querySelector('[data-tab-dot="' + tabId + '"]');
      if (dot) dot.className = 'pane-tab-dot none';
    }
    const contentChanged = d.output !== state.last;
    if (contentChanged) {
      state.lastOutputChange = Date.now();
      state.last = d.output; state.rawContent = d.output;
    }
    if (contentChanged || state._scrollToBottom || state._renderDeferred) {
      // Defer heavy DOM work during drag to prevent stutter
      if (_dragSrcTabId !== null || _sbDragging) { state._renderDeferred = true; return; }
      const outEl = document.getElementById('tab-output-' + tabId);
      if (!outEl) return;
      // Skip DOM update while user is selecting text (prevents selection jumping)
      const sel = window.getSelection();
      if (sel && sel.type === 'Range' && outEl.contains(sel.anchorNode)) return;
      const atBottom = state._scrollToBottom || (outEl.scrollHeight - outEl.scrollTop - outEl.clientHeight < 80);
      if (contentChanged || state._renderDeferred) renderOutput(d.output, outEl, state, tabId);
      if (atBottom) outEl.scrollTop = outEl.scrollHeight;
      state._scrollToBottom = false;
      state._renderDeferred = false;
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
function paneForTab(tabId) {
  return panes.find(p => p.activeTabId === tabId) || null;
}
function getActiveTab() {
  if (!activePaneId) return null;
  const pane = panes.find(p => p.id === activePaneId);
  if (!pane || !pane.activeTabId) return null;
  return { tabId: pane.activeTabId, tab: allTabs[pane.activeTabId], state: tabStates[pane.activeTabId] };
}

async function _sendCmd(tabId, text, ta) {
  // Shared send logic — ta is the textarea to clear/restore
  const tab = allTabs[tabId]; const state = tabStates[tabId];
  if (!tab || !state || tab.type === 'file') return;
  // Save backup BEFORE clearing — protects against draft system race
  state._sendingText = text;
  ta.value = ''; ta.style.height = 'auto'; ta.style.overflowY = 'hidden';
  try {
    state.pendingMsg = text; state.pendingTime = Date.now(); state.awaitingResponse = true;
    const qs = _queueStates[tabId];
    if (qs && qs.playing) pauseQueue(tabId);
    try {
      const outEl = document.getElementById('tab-output-' + tabId);
      if (outEl) { renderOutput(state.rawContent || state.last, outEl, state, tabId); outEl.scrollTop = outEl.scrollHeight; }
    } catch(re) {} // renderOutput failure must not abort send
    const resp = await fetch('/api/send', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ cmd: text, session: tab.session, window: tab.windowIndex, windowName: tab.windowName || '' })
    });
    if (!resp.ok) throw new Error('send failed: ' + resp.status);
    state._sendingText = null;
  } catch(e) {
    console.warn('_sendCmd failed:', e);
    state._sendingText = null;
    ta.value = text; ta.dispatchEvent(new Event('input'));
    state.pendingMsg = null; state.awaitingResponse = false;
  }
}

async function sendToPane(paneId) {
  const pane = panes.find(p => p.id === paneId);
  if (!pane || !pane.activeTabId) return;
  const paneEl = document.getElementById('pane-' + paneId);
  if (!paneEl) return;
  const ta = paneEl.querySelector('.pane-input textarea');
  if (!ta || !ta.value) return;
  await _sendCmd(pane.activeTabId, ta.value, ta);
}

async function sendGlobal() {
  if (!M.value) return;
  const active = getActiveTab(); if (!active) return;
  await _sendCmd(active.tabId, M.value, M);
}

async function keyActive(k) {
  const active = getActiveTab(); if (!active) return;
  if (active.tab && active.tab.type === 'file') return;
  try { await fetch('/api/key/' + k + '?session=' + encodeURIComponent(active.tab.session) + '&window=' + active.tab.windowIndex); } catch(e) {}
}

function prefill(text) { M.value = M.value ? M.value + ' ' + text : text; M.focus(); }

async function sendResumeActive() {
  const active = getActiveTab(); if (!active) return;
  if (active.tab && active.tab.type === 'file') return;
  active.state.rawMode = true;
  document.getElementById('view-label').textContent = 'Raw';
  try {
    await fetch('/api/send', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ cmd: '/resume', session: active.tab.session, window: active.tab.windowIndex, windowName: active.tab.windowName || '' })
    });
  } catch(e) {}
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

function togglePaneTray(btn, which) {
  const pi = btn.closest('.pane-input');
  const tray = pi.querySelector('.pane-tray-' + which);
  const on = tray.classList.toggle('open');
  btn.classList.toggle('on', on);
  const other = which === 'keys' ? 'cmds' : 'keys';
  const otherTray = pi.querySelector('.pane-tray-' + other);
  const otherBtn = [...pi.querySelectorAll('.pane-toolbar .pill')].find(b => b.textContent === (other === 'keys' ? 'Keys' : 'Commands'));
  if (on && otherTray) { otherTray.classList.remove('open'); if (otherBtn) otherBtn.classList.remove('on'); }
}

function panePrefill(btn, text) {
  const ta = btn.closest('.pane-input').querySelector('textarea');
  if (ta) { ta.value = ta.value ? ta.value + ' ' + text : text; ta.focus(); }
}

// === View toggle ===
function toggleRaw() {
  const active = getActiveTab(); if (!active) return;
  const tab = allTabs[active.tabId];
  // File tab: toggle formatted/raw (markdown only)
  if (tab && tab.type === 'file') {
    if (!isMarkdown(tab.fileName)) return; // non-md files have no toggle
    active.state.fileRawView = !active.state.fileRawView;
    document.getElementById('view-label').textContent = active.state.fileRawView ? 'Raw' : 'Formatted';
    renderFileTabFormatted(active.tabId);
    return;
  }
  active.state.rawMode = !active.state.rawMode;
  document.getElementById('view-label').textContent = active.state.rawMode ? 'Raw' : 'Clean';
  const outEl = document.getElementById('tab-output-' + active.tabId);
  if (outEl) { renderOutput(active.state.rawContent || active.state.last, outEl, active.state, active.tabId); outEl.scrollTop = outEl.scrollHeight; }
}

// === Text size ===
const TEXT_SIZES = [
  { label: 'A--', text: '11px', code: '9.5px', mono: '9.5px', padV: '6px', padH: '8px', gap: '3px', radius: '10px', lineH: '1.4', sbName: '11px', sbDetail: '10px', sbTiny: '9.5px' },
  { label: 'A-',  text: '13px', code: '11px', mono: '11px', padV: '8px', padH: '12px', gap: '6px', radius: '14px', lineH: '1.55', sbName: '13px', sbDetail: '11.5px', sbTiny: '11px' },
  { label: 'A',   text: '15px', code: '13px', mono: '13px', padV: '16px', padH: '18px', gap: '12px', radius: '18px', lineH: '1.7', sbName: '15px', sbDetail: '13px', sbTiny: '12px' },
  { label: 'A+',  text: '17px', code: '15px', mono: '15px', padV: '18px', padH: '20px', gap: '14px', radius: '20px', lineH: '1.8', sbName: '17px', sbDetail: '15px', sbTiny: '13px' },
];
let _textSizeIdx = 2;
let _fileLinksEnabled = true;
function applyTextSize(idx) {
  _textSizeIdx = idx;
  const s = TEXT_SIZES[idx];
  const r = document.documentElement.style;
  r.setProperty('--text-size', s.text); r.setProperty('--code-size', s.code);
  r.setProperty('--mono-size', s.mono); r.setProperty('--turn-pad-v', s.padV);
  r.setProperty('--turn-pad-h', s.padH); r.setProperty('--turn-gap', s.gap);
  r.setProperty('--turn-radius', s.radius); r.setProperty('--line-h', s.lineH);
  r.setProperty('--sb-name', s.sbName); r.setProperty('--sb-detail', s.sbDetail);
  r.setProperty('--sb-tiny', s.sbTiny);
  document.getElementById('settings-btn').innerHTML = '&#9881;';
  document.querySelectorAll('.settings-size-btn').forEach((b, i) => b.classList.toggle('active', i === idx));
  try { prefs.setItem('textSize', idx); } catch(e) {}
}
function toggleSettings() {
  const p = document.getElementById('settings-panel');
  const open = p.style.display === 'block';
  p.style.display = open ? 'none' : 'block';
}
document.addEventListener('click', function(e) {
  const p = document.getElementById('settings-panel');
  if (!p || p.style.display !== 'block') return;
  if (p.contains(e.target) || e.target.closest('#settings-btn')) return;
  p.style.display = 'none';
});
function toggleFileLinks() {
  _fileLinksEnabled = !_fileLinksEnabled;
  const btn = document.getElementById('file-links-toggle');
  btn.textContent = _fileLinksEnabled ? 'ON' : 'OFF';
  btn.classList.toggle('on', _fileLinksEnabled);
  try { prefs.setItem('fileLinks', _fileLinksEnabled ? '1' : '0'); } catch(e) {}
  // Re-render all visible tabs to apply/remove links
  for (const p of panes) {
    if (!p.activeTabId) continue;
    const st = tabStates[p.activeTabId];
    const outEl = document.getElementById('tab-output-' + p.activeTabId);
    if (st && outEl) {
      const scrollPos = outEl.scrollTop;
      renderOutput(st.rawContent || st.last, outEl, st, p.activeTabId);
      outEl.scrollTop = scrollPos;
    }
  }
}

// === Sidebar ===
function toggleSidebar() {
  _sidebarCollapsed = !_sidebarCollapsed;
  document.getElementById('sidebar').classList.toggle('collapsed', _sidebarCollapsed);
  document.getElementById('collapse-btn').textContent = _sidebarCollapsed ? '\\u00bb' : '\\u00ab';
}
function toggleSidebarExpand() {
  _sidebarExpanded = !_sidebarExpanded;
  prefs.setItem('sidebar:expanded', _sidebarExpanded);
  document.getElementById('sb-expand-btn').innerHTML = _sidebarExpanded ? '&#9662;' : '&#9656;';
  renderSidebar();
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
      prefs.setItem('sidebar:width', w);
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
    }
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
  });
})();

let _sidebarOrder = { sessions: [], windows: {} };
let _sbDragging = false;

function renderSidebar() {
  if (_sbDragging) return;
  const data = _dashboardData;
  if (!data) return;
  const content = document.getElementById('sidebar-content');
  // Sort sessions by custom order
  const sessions = [...data.sessions].sort((a, b) => {
    const ia = _sidebarOrder.sessions.indexOf(a.name);
    const ib = _sidebarOrder.sessions.indexOf(b.name);
    if (ia < 0 && ib < 0) return 0;
    if (ia < 0) return 1;
    if (ib < 0) return -1;
    return ia - ib;
  });
  // Determine the active window for highlighting
  const activePn = panes.find(p => p.id === activePaneId);
  const activeTab = activePn && activePn.activeTabId ? allTabs[activePn.activeTabId] : null;
  const hidden = getHiddenSessions();
  const visibleSessions = sessions.filter(s => !hidden.includes(s.name));
  const hiddenSessions = sessions.filter(s => hidden.includes(s.name));
  let html = '';
  for (const s of visibleSessions) {
    html += renderSidebarSession(s, activeTab, false);
  }
  // Hidden sessions section
  if (hiddenSessions.length > 0) {
    html += '<div class="sb-hidden-header" onclick="_hiddenExpanded=!_hiddenExpanded;renderSidebar()">'
      + '<span class="sb-hidden-chevron' + (_hiddenExpanded ? ' open' : '') + '">&#9654;</span>'
      + ' Hidden (' + hiddenSessions.length + ')</div>';
    if (_hiddenExpanded) {
      for (const s of hiddenSessions) {
        html += renderSidebarSession(s, activeTab, true);
      }
    }
  }
  content.innerHTML = html;
}
function renderSidebarSession(s, activeTab, isHidden) {
  let html = '<div class="sb-session" draggable="true" data-session="' + esc(s.name) + '">';
  const winOrder = _sidebarOrder.windows[s.name] || [];
  const windows = [...s.windows].sort((a, b) => {
    const ia = winOrder.indexOf(a.index);
    const ib = winOrder.indexOf(b.index);
    if (ia < 0 && ib < 0) return 0;
    if (ia < 0) return 1;
    if (ib < 0) return -1;
    return ia - ib;
  });
  const fsEsc = esc(s.name).replace(/'/g, "\\\\'");
  const hideBtn = isHidden
    ? '<button class="sb-hide-btn" onclick="event.stopPropagation();unhideSession(\\'' + fsEsc + '\\')">SHOW</button>'
    : '<button class="sb-hide-btn" onclick="event.stopPropagation();hideSession(\\'' + fsEsc + '\\')">HIDE</button>';
  if (windows.length > 0) {
    const firstWin = windows[0];
    const fwEsc = esc(firstWin.name).replace(/'/g, "\\\\'");
    html += '<div class="sb-session-header" onclick="openTab(\\'' + fsEsc + '\\',' + firstWin.index + ',\\'' + fwEsc + '\\')" style="cursor:pointer">' + esc(s.name)
      + (s.attached ? ' <span class="sb-badge">attached</span>' : '') + hideBtn + '</div>';
  } else {
    html += '<div class="sb-session-header">' + esc(s.name)
      + (s.attached ? ' <span class="sb-badge">attached</span>' : '') + hideBtn + '</div>';
  }
  for (const w of windows) {
    const dotClass = w.cc_fresh ? 'none' : w.is_cc ? (w.cc_status || 'idle') : 'none';
    let ctxHtml = '';
    if (w.gauge_context_pct != null) {
      const pct = Math.round(w.gauge_context_pct);
      const cls = _ctxCls(pct);
      ctxHtml = '<div class="sb-ctx ' + (cls || '') + '">' + pct + '%' + (w.gauge_drift > 10 ? '!' : '') + '</div>';
    } else if (w.cc_context_pct != null) {
      const cls = _ctxCls(w.cc_context_pct);
      ctxHtml = '<div class="sb-ctx ' + (cls || '') + '">' + w.cc_context_pct + '%</div>';
    }
    const ageTs = w.gauge_last_ts || w.activity_ts;
    const age = ageFromTs(ageTs);
    const wid_age = esc(s.name) + ':' + w.index;
    const ageHtml = '<div class="sb-activity" data-wid="' + wid_age + '">' + age + '</div>';
    const wid = esc(s.name) + ':' + w.index;
    const isActive = activeTab && activeTab.session === s.name && activeTab.windowIndex === w.index;
    const sEsc = esc(s.name).replace(/'/g, "\\\\'");
    const wEsc = esc(w.name).replace(/'/g, "\\\\'");
    html += '<div class="sb-win' + (isActive ? ' active' : '') + '" draggable="true" data-session="' + esc(s.name) + '" data-widx="' + w.index + '" onclick="openTab(\\'' + sEsc + '\\',' + w.index + ',\\'' + wEsc + '\\')">'
      + '<div class="sb-win-dot ' + dotClass + '" data-wid="' + wid + '"></div>'
      + '<div class="sb-win-info">'
      + '<div class="sb-win-name">' + esc(w.name) + '</div>'
      + (getStandby(s.name, w.index) ? '<div class="sb-standby">Standby</div>' : w.cc_fresh ? '<div class="sb-fresh">CLEAR</div>' : '')
      + (_sidebarExpanded ? '<div class="sb-win-cwd">' + esc(abbreviateCwd(w.cwd)) + '</div>' : '')
      + (_sidebarExpanded && w.is_cc ? '<div class="sb-perm' + (w.cc_perm_mode && /dangerously|skip|bypass/i.test(w.cc_perm_mode) ? ' danger' : '') + '" data-wid="' + wid + '">' + (w.cc_perm_mode ? esc(w.cc_perm_mode) : '') + '</div>' : '')
      + '</div>'
      + ageHtml + ctxHtml
      + '<button class="sb-win-detail-btn" onclick="event.stopPropagation();openWD(\\'' + sEsc + '\\',' + w.index + ')" title="Details">&#8942;</button>'
      + '</div>';
  }
  html += '</div>';
  return html;
}

function openTab(session, windowIndex, windowName) {
  closeMobileSidebar();
  createTab(session, windowIndex, windowName);
}

// === Sidebar drag reorder ===
(function() {
  const sbContent = document.getElementById('sidebar-content');
  let dragType = null, dragSession = null, dragWidx = null, dragFtRoot = null;

  sbContent.addEventListener('dragstart', e => {
    if (_sidebarView === 'files') {
      const rootGroup = e.target.closest('.ft-root-group[draggable]');
      if (rootGroup) {
        dragType = 'ft-root';
        dragFtRoot = rootGroup.dataset.ftRootPath;
        rootGroup.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', 'ft-root');
      }
      return;
    }
    if (_sidebarView !== 'sessions') return;
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
    sbContent.querySelectorAll('.sb-drag-over,.ft-drag-over').forEach(el => el.classList.remove('sb-drag-over', 'ft-drag-over'));
    if (dragType === 'ft-root') {
      const rg = e.target.closest('.ft-root-group');
      if (rg && rg.dataset.ftRootPath !== dragFtRoot) rg.classList.add('ft-drag-over');
    } else if (dragType === 'window') {
      const win = e.target.closest('.sb-win');
      if (win && win.dataset.session === dragSession) win.classList.add('sb-drag-over');
    } else if (dragType === 'session') {
      const sess = e.target.closest('.sb-session');
      if (sess && sess.dataset.session !== dragSession) sess.classList.add('sb-drag-over');
    }
  });

  sbContent.addEventListener('dragleave', e => {
    const el = e.target.closest('.sb-drag-over,.ft-drag-over');
    if (el) el.classList.remove('sb-drag-over', 'ft-drag-over');
  });

  sbContent.addEventListener('drop', e => {
    e.preventDefault();
    sbContent.querySelectorAll('.sb-drag-over,.ft-drag-over,.dragging').forEach(el => {
      el.classList.remove('sb-drag-over', 'ft-drag-over', 'dragging');
    });
    if (dragType === 'ft-root') {
      const target = e.target.closest('.ft-root-group');
      if (target && target.dataset.ftRootPath !== dragFtRoot) {
        const allGroups = [...sbContent.querySelectorAll('.ft-root-group')].map(el => el.dataset.ftRootPath);
        const fromIdx = allGroups.indexOf(dragFtRoot);
        const toIdx = allGroups.indexOf(target.dataset.ftRootPath);
        if (fromIdx >= 0 && toIdx >= 0) {
          allGroups.splice(fromIdx, 1);
          allGroups.splice(toIdx, 0, dragFtRoot);
          _ftRootOrder = allGroups;
          saveFtRootOrder();
          renderFileTree();
        }
      }
    } else if (dragType === 'session') {
      const target = e.target.closest('.sb-session');
      if (target && target.dataset.session !== dragSession) {
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
    dragType = null; dragSession = null; dragWidx = null; dragFtRoot = null;
  });

  sbContent.addEventListener('dragend', () => {
    sbContent.querySelectorAll('.sb-drag-over,.ft-drag-over,.dragging').forEach(el => {
      el.classList.remove('sb-drag-over', 'ft-drag-over', 'dragging');
    });
    _sbDragging = false;
    dragType = null; dragSession = null; dragWidx = null; dragFtRoot = null;
  });
})();

function saveSidebarOrder() {
  try { prefs.setItem('sidebar:order', JSON.stringify(_sidebarOrder)); } catch(e) {}
}

// === Standby ===
function getStandby(session, windowIndex) {
  return prefs.getItem('standby:' + session + ':' + windowIndex) === '1';
}
function setStandby(session, windowIndex, on) {
  if (on) prefs.setItem('standby:' + session + ':' + windowIndex, '1');
  else prefs.removeItem('standby:' + session + ':' + windowIndex);
}
function toggleWDStandby() {
  if (!_wdSession || _wdWindow === null) return;
  const on = !getStandby(_wdSession, _wdWindow);
  setStandby(_wdSession, _wdWindow, on);
  const btn = document.getElementById('wd-standby-btn');
  if (btn) { btn.textContent = on ? 'Remove Standby' : 'Set Standby'; btn.className = 'wd-btn-dismiss' + (on ? ' active' : ''); }
  loadDashboard();
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
      const isDanger = /dangerously|skip|bypass/i.test(win.cc_perm_mode);
      html += '<div class="wd-row"><span class="wd-label">Permissions</span><span class="wd-value' + (isDanger ? '" style="color:var(--red);font-weight:600' : '') + '">' + esc(win.cc_perm_mode) + '</span></div>';
    }
    if (win.gauge_context_pct != null) {
      const gp = win.gauge_context_pct;
      const pctRemaining = Math.round(gp);
      const barColor = pctRemaining <= 10 ? 'var(--red)' : pctRemaining <= 25 ? 'var(--orange)' : 'var(--green)';
      html += '<div class="wd-row"><span class="wd-label">Context</span><span class="wd-value">'
        + '<span style="margin-right:8px">' + pctRemaining + '% left</span>'
        + '<span style="display:inline-block;width:80px;height:6px;background:var(--surface);border-radius:3px;vertical-align:middle">'
        + '<span style="display:block;width:' + pctRemaining + '%;height:100%;background:' + barColor + ';border-radius:3px"></span>'
        + '</span></span></div>';
      if (win.gauge_burn_rate > 0) {
        html += '<div class="wd-row"><span class="wd-label">Burn rate</span><span class="wd-value">~' + win.gauge_burn_rate.toLocaleString() + ' tok/turn</span></div>';
      }
      if (win.gauge_est_turns != null) {
        html += '<div class="wd-row"><span class="wd-label">Est. turns left</span><span class="wd-value">~' + win.gauge_est_turns + '</span></div>';
      }
      if (win.gauge_drift != null) {
        const driftColor = win.gauge_drift > 10 ? 'var(--red)' : 'var(--orange)';
        html += '<div class="wd-row"><span class="wd-label">Gauge drift</span><span class="wd-value" style="color:' + driftColor + '">'
          + win.gauge_drift + ' pts (gauge ' + pctRemaining + '% vs CC ' + win.cc_context_pct + '%)</span></div>';
      }
    } else {
      const pct = win.cc_context_pct;
      const ctxLabel = pct != null ? pct + '% left' : 'Healthy';
      const barColor = pct != null && pct <= 10 ? 'var(--red)' : pct != null && pct <= 25 ? 'var(--orange)' : 'var(--green)';
      const barWidth = pct != null ? pct : 0;
      html += '<div class="wd-row"><span class="wd-label">Context</span><span class="wd-value">'
        + '<span style="margin-right:8px">' + ctxLabel + '</span>'
        + '<span style="display:inline-block;width:80px;height:6px;background:var(--surface);border-radius:3px;vertical-align:middle">'
        + '<span style="display:block;width:' + barWidth + '%;height:100%;background:' + barColor + ';border-radius:3px"></span>'
        + '</span></span></div>';
    }
  }
  document.getElementById('wd-content').innerHTML = html;
  const isStandby = getStandby(session, windowIndex);
  const sbBtn = document.getElementById('wd-standby-btn');
  sbBtn.textContent = isStandby ? 'Remove Standby' : 'Set Standby';
  if (isStandby) sbBtn.classList.add('active'); else sbBtn.classList.remove('active');
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
  }).catch(() => {}).then(() => {
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
  fetch('/api/windows/' + _wdWindow + '?session=' + encodeURIComponent(_wdSession), {method:'DELETE'}).catch(() => {}).then(() => {
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
    const dragging = _dragSrcTabId !== null || _sbDragging;
    if (!dragging) {
      if (_sidebarView === 'sessions') renderSidebar();
      else renderFileTree();
    }
    // Update tab names and status dots from dashboard
    for (const tid in allTabs) {
      const tab = allTabs[tid];
      if (tab.type === 'file') continue;
      const sess = _dashboardData.sessions.find(s => s.name === tab.session);
      if (sess) {
        const win = sess.windows.find(w => w.index === tab.windowIndex);
        if (win) {
          if (win.name !== tab.windowName) {
            tab.windowName = win.name;
            if (!dragging) {
              for (const p of panes) {
                if (p.tabIds.includes(parseInt(tid))) renderPaneTabs(p.id);
              }
            }
          }
          // Update tab dot from dashboard CC status (covers background tabs)
          const st = tabStates[tid];
          if (st) {
            const newStatus = win.cc_fresh ? null : win.is_cc ? (win.cc_status || 'idle') : null;
            if (st.ccStatus !== newStatus) {
              st.ccStatus = newStatus;
              const dot = document.querySelector('[data-tab-dot="' + tid + '"]');
              if (dot) dot.className = 'pane-tab-dot ' + (newStatus || 'none');
            }
          }
        }
      }
    }
    updateAllPaneGauges();
  } catch(e) {}
}

function _nwGetSession() {
  let sessName = null;
  const ap = panes.find(p => p.id === activePaneId);
  if (ap && ap.activeTabId != null && allTabs[ap.activeTabId]) sessName = allTabs[ap.activeTabId].session;
  if (!sessName) { for (const tid in allTabs) { sessName = allTabs[tid].session; break; } }
  if (!sessName && _dashboardData && _dashboardData.sessions.length > 0) sessName = _dashboardData.sessions[0].name;
  return sessName;
}
function _nwGetCwd() {
  // Get cwd from active tab's session — use the most common cwd among session windows
  const sessName = _nwGetSession();
  if (!sessName || !_dashboardData) return '';
  const sess = _dashboardData.sessions.find(s => s.name === sessName);
  if (!sess || !sess.windows.length) return '';
  // Use active tab's cwd if available, otherwise most common cwd in session
  const ap = panes.find(p => p.id === activePaneId);
  if (ap && ap.activeTabId != null) {
    const tab = allTabs[ap.activeTabId];
    if (tab && tab.type !== 'file') {
      const win = sess.windows.find(w => w.index === tab.windowIndex);
      if (win && win.cwd) return win.cwd;
    }
  }
  return sess.windows[0].cwd || '';
}
function newWin() {
  // Pre-populate and show modal
  document.getElementById('nw-dir-input').value = _nwGetCwd();
  document.getElementById('nw-claude-cb').checked = true;
  document.getElementById('nw-dsp-cb').checked = true;
  toggleNwDsp();
  document.getElementById('nw-overlay').classList.add('open');
}
function closeNewWin() {
  document.getElementById('nw-overlay').classList.remove('open');
}
function toggleNwDsp() {
  const show = document.getElementById('nw-claude-cb').checked;
  document.getElementById('nw-dsp-row').style.display = show ? 'flex' : 'none';
}
async function submitNewWin() {
  const sessName = _nwGetSession();
  const cwd = document.getElementById('nw-dir-input').value.trim();
  const openClaude = document.getElementById('nw-claude-cb').checked;
  const dsp = document.getElementById('nw-dsp-cb').checked;
  closeNewWin();
  const commands = [];
  if (openClaude) {
    let cmd = 'claude';
    if (dsp) cmd += ' --dangerously-skip-permissions';
    commands.push(cmd);
  }
  let newIdx = null;
  try {
    const resp = await fetch('/api/windows/new', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session: sessName, cwd: cwd || undefined, commands: commands.length ? commands : undefined })
    });
    const data = await resp.json();
    newIdx = data.index;
  } catch(e) { return; }
  await loadDashboard();
  // Focus the new window tab using returned index
  if (_dashboardData && sessName && newIdx != null) {
    const sess = _dashboardData.sessions.find(s => s.name === sessName);
    if (sess) {
      const w = sess.windows.find(w => w.index === newIdx);
      if (w) { createTab(sessName, w.index, w.name); return; }
    }
  }
  // Fallback: focus last window in session
  if (_dashboardData && sessName) {
    const sess = _dashboardData.sessions.find(s => s.name === sessName);
    if (sess && sess.windows.length > 0) {
      const w = sess.windows[sess.windows.length - 1];
      createTab(sessName, w.index, w.name);
    }
  }
}

// === Input ===
// Shared textarea setup: auto-resize, Enter/key-forwarding, mobile beforeinput fallback
function setupTextareaInput(ta, sendFn) {
  const resize = () => { const max=window.innerHeight*0.4; ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,max)+'px'; ta.style.overflowY=ta.scrollHeight>max?'auto':'hidden'; };
  ta.addEventListener('input', resize);
  ta.addEventListener('paste', () => setTimeout(resize, 0));
  let _enterHandled = false, _shift = false;
  ta.addEventListener('keydown', e => {
    _shift = e.shiftKey;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _enterHandled = true; if (ta.value.trim()) sendFn(); else keyActive('Enter'); }
    if (!ta.value && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); keyActive(e.key === 'ArrowUp' ? 'Up' : 'Down'); }
    if (!ta.value && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { e.preventDefault(); keyActive(e.key === 'ArrowLeft' ? 'Left' : 'Right'); }
    if (!ta.value && e.key === 'Escape') { e.preventDefault(); keyActive('Escape'); }
    if (!ta.value && e.key === 'Tab') { e.preventDefault(); keyActive('Tab'); }
  });
  ta.addEventListener('beforeinput', e => {
    if (e.inputType === 'insertLineBreak' && !_enterHandled && !_shift) { e.preventDefault(); if (ta.value.trim()) sendFn(); else keyActive('Enter'); }
    _enterHandled = false;
  });
}
setupTextareaInput(M, sendGlobal);

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
    if (t.type === 'file') {
      ftOpenFile(t.filePath, paneId);
    } else if (windowExists(t.session, t.windowIndex)) {
      createTab(t.session, t.windowIndex, t.windowName, paneId);
      // Restore saved rawMode (default is true, so restore false when user chose Clean)
      if (t.rawMode === false) {
        const pane = panes.find(p => p.id === paneId);
        if (pane) {
          const tid = pane.tabIds[pane.tabIds.length - 1];
          if (tid != null && tabStates[tid]) tabStates[tid].rawMode = false;
        }
      }
    }
  }
  if (paneData.activeTab) {
    const pane = panes.find(p => p.id === paneId);
    if (pane) {
      for (const tid of pane.tabIds) {
        const tab = allTabs[tid];
        if (paneData.activeTab.type === 'file') {
          if (tab && tab.type === 'file' && tab.filePath === paneData.activeTab.filePath) { focusTab(tid); break; }
        } else {
          if (tab && tab.type !== 'file' && tab.session === paneData.activeTab.session
              && tab.windowIndex === paneData.activeTab.windowIndex) { focusTab(tid); break; }
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
    const anyValid = allPaneData.some(p => p.tabIds && p.tabIds.some(t => t.type === 'file' || windowExists(t.session, t.windowIndex)));
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
    // Remove empty panes left over when saved tabs no longer exist
    for (let i = panes.length - 1; i >= 0; i--) {
      if (panes[i].tabIds.length === 0 && panes.length > 1) {
        const el = document.getElementById('pane-' + panes[i].id);
        if (el) {
          const stack = el.parentElement;
          el.remove();
          if (stack && stack.classList.contains('pane-stack')) {
            const remaining = stack.querySelectorAll('.pane');
            if (remaining.length <= 1) {
              if (remaining.length === 1) stack.parentElement.insertBefore(remaining[0], stack);
              stack.remove();
            }
          }
        }
        panes.splice(i, 1);
      }
    }
    _restoringLayout = false;
    return panes.some(p => p.tabIds.length > 0);
  } catch(e) { _restoringLayout = false; return false; }
}

function cleanupStaleStorage() {
  // Remove prefs keys for windows/sessions that no longer exist
  if (!_dashboardData) return;
  const validKeys = new Set();
  for (const s of _dashboardData.sessions) {
    for (const w of s.windows) validKeys.add(s.name + ':' + w.index);
  }
  const prefixes = ['notepad:', 'queue:', 'standby:'];
  try {
    for (const key of prefs.keys()) {
      for (const pfx of prefixes) {
        if (key.startsWith(pfx)) {
          const rest = key.slice(pfx.length);
          if (!validKeys.has(rest)) prefs.removeItem(key);
        }
      }
    }
    // Prune sidebar order: remove deleted sessions/windows
    if (_sidebarOrder.sessions.length) {
      const validSessions = new Set(_dashboardData.sessions.map(s => s.name));
      _sidebarOrder.sessions = _sidebarOrder.sessions.filter(s => validSessions.has(s));
      for (const sn in _sidebarOrder.windows) {
        if (!validSessions.has(sn)) delete _sidebarOrder.windows[sn];
      }
      saveSidebarOrder();
    }
  } catch(e) {}
}

async function init() {
  await prefs.load();
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    Notification.requestPermission();
  }
  // Restore deferred prefs
  _sidebarExpanded = prefs.getItem('sidebar:expanded') === 'true';
  if (_sidebarExpanded) document.getElementById('sb-expand-btn').innerHTML = '&#9662;';
  try { const saved = prefs.getItem('textSize'); if (saved !== null) applyTextSize(parseInt(saved)); } catch(e) {}
  try { const fl = prefs.getItem('fileLinks'); if (fl === '0') { _fileLinksEnabled = false; const b = document.getElementById('file-links-toggle'); b.textContent = 'OFF'; b.classList.remove('on'); } } catch(e) {}
  try { const sw = prefs.getItem('sidebar:width'); if (sw) document.documentElement.style.setProperty('--sidebar-w', sw + 'px'); } catch(e) {}
  try { const so = JSON.parse(prefs.getItem('sidebar:order')); if (so) _sidebarOrder = so; } catch(e) {}
  try { const fo = JSON.parse(prefs.getItem('ft:root-order')); if (fo) _ftRootOrder = fo; } catch(e) {}
  try { const fh = JSON.parse(prefs.getItem('ft:hidden-roots')); if (fh) _ftHiddenRoots = fh; } catch(e) {}
  await loadDashboard();
  cleanupStaleStorage();
  if (!restoreLayout()) {
    createPane();
    if (_dashboardData && _dashboardData.sessions.length > 0) {
      const sess = _dashboardData.sessions[0];
      const activeWin = sess.windows.find(w => w.active) || sess.windows[0];
      if (activeWin) createTab(sess.name, activeWin.index, activeWin.name);
    }
  }
  setInterval(() => { if (!document.hidden) loadDashboard(); }, 3000);
  // Update activity ages in-place every 30s (between dashboard polls)
  setInterval(() => { if (!document.hidden) updateSidebarAges(); }, 30000);
}
// Pause/resume polling when page visibility changes
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // Stop all tab polls when page is hidden
    for (const tid in tabStates) stopTabPolling(parseInt(tid));
  } else {
    // Resume visible tab polls + refresh dashboard
    updatePolling();
    loadDashboard();
  }
});
window.addEventListener('beforeunload', function(e) {
  for (const tid in tabStates) {
    if (tabStates[tid].fileDirty) { e.preventDefault(); return; }
  }
});
init();
</script>
</body>
</html>"""



@app.get("/")
async def index():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ensure_session)
    html = HTML.replace("__TITLE__", TITLE)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/output")
async def api_output(session: str = None, window: int = None):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, get_output, session, window)
    return JSONResponse({"output": output})


@app.post("/api/send")
async def api_send(body: dict):
    cmd = body.get("cmd", "")
    session = body.get("session", None)
    window = body.get("window", None)
    window_name = body.get("windowName", "")
    if cmd:
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, send_keys, cmd, session, window)
        if not ok:
            return JSONResponse({"ok": False, "error": "tmux send failed"}, status_code=500)
        s = session or _current_session
        w = window if window is not None else 0
        key = f"{s}:{w}"
        _last_interaction[key] = time.time()
        _notify_pending[key] = {"window_name": window_name, "saw_busy": False}
        # Collect sent text for gauge matching (only while unmatched)
        if key not in _gauge_locks:
            texts = _gauge_sent.setdefault(key, [])
            texts.append(cmd)
            if len(texts) > 20:
                texts[:] = texts[-20:]
    return JSONResponse({"ok": True})


@app.get("/api/key/{key}")
async def api_key(key: str, session: str = None, window: int = None):
    ALLOWED = {"C-c", "C-d", "C-l", "C-z", "Up", "Down", "Left", "Right", "Tab", "Enter", "Escape"}
    if key in ALLOWED:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_special, key, session, window)
        s = session or _current_session
        w = window if window is not None else 0
        _last_interaction[f"{s}:{w}"] = time.time()
    return JSONResponse({"ok": True})


@app.get("/api/dashboard")
async def api_dashboard():
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, get_dashboard)
    return JSONResponse(data)


@app.post("/api/notify")
async def api_notify(body: dict):
    session = body.get("session", "")
    window = body.get("window", 0)
    window_name = body.get("windowName", "")
    key = f"{session}:{window}"
    # Remove from pending so background monitor won't double-fire
    _notify_pending.pop(key, None)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, _send_notification, "Claude Code done", f"{window_name or key} finished", key)
    return JSONResponse({"ok": True})


@app.get("/api/windows")
async def api_windows():
    loop = asyncio.get_running_loop()
    windows = await loop.run_in_executor(None, list_windows)
    return JSONResponse({"windows": windows})


@app.post("/api/windows/new")
async def api_new_window(body: dict = {}):
    loop = asyncio.get_running_loop()
    idx = await loop.run_in_executor(None, lambda: new_window(
        session=body.get("session"),
        cwd=body.get("cwd"),
        commands=body.get("commands"),
    ))
    return JSONResponse({"ok": True, "index": idx})


@app.post("/api/windows/{index}")
async def api_select_window(index: int):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, select_window, index)
    return JSONResponse({"ok": True})


@app.put("/api/windows/current")
async def api_rename_current_window(body: dict):
    name = body.get("name", "").strip()
    if name:
        def _do():
            target = _current_session
            _run(["tmux", "rename-window", "-t", target, name])
            _run(["tmux", "set-window-option", "-t", target, "allow-rename", "off"])
            _run(["tmux", "set-window-option", "-t", target, "automatic-rename", "off"])
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do)
    return JSONResponse({"ok": True})


@app.post("/api/windows/current/reset-name")
async def api_reset_window_name():
    def _do():
        target = _current_session
        _run(["tmux", "set-window-option", "-t", target, "automatic-rename", "on"])
        _run(["tmux", "set-window-option", "-t", target, "allow-rename", "on"])
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _do)
    return JSONResponse({"ok": True})


@app.put("/api/windows/{index}")
async def api_rename_window(index: int, body: dict):
    name = body.get("name", "").strip()
    session = body.get("session", _current_session)
    if name:
        def _do():
            target = f"{session}:{index}"
            _run(["tmux", "rename-window", "-t", target, name])
            _run(["tmux", "set-window-option", "-t", target, "allow-rename", "off"])
            _run(["tmux", "set-window-option", "-t", target, "automatic-rename", "off"])
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do)
    return JSONResponse({"ok": True})


@app.delete("/api/windows/{index}")
async def api_close_window(index: int, session: str = None):
    sess = session or _current_session
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _run, ["tmux", "kill-window", "-t", f"{sess}:{index}"])
    return JSONResponse({"ok": True})


@app.get("/api/sessions")
async def api_sessions():
    loop = asyncio.get_running_loop()
    sessions = await loop.run_in_executor(None, list_sessions)
    return JSONResponse({
        "current": _current_session,
        "sessions": sessions,
    })


@app.get("/api/pane-info")
async def api_pane_info():
    def _do():
        r = _run(
            ["tmux", "display-message", "-t", _current_session, "-p",
             "#{pane_current_path}\n#{pane_pid}\n#{window_name}\n#{session_name}"],
            capture_output=True, text=True,
        )
        parts = r.stdout.strip().split("\n")
        return {
            "cwd": parts[0] if len(parts) > 0 else "",
            "pid": parts[1] if len(parts) > 1 else "",
            "window": parts[2] if len(parts) > 2 else "",
            "session": parts[3] if len(parts) > 3 else "",
        }
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _do)
    return JSONResponse(data)


@app.post("/api/sessions/{name}")
async def api_switch_session(name: str):
    global _current_session
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(
        None, _run, ["tmux", "has-session", "-t", name])
    if r.returncode != 0:
        return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
    _current_session = name
    return JSONResponse({"ok": True})


@app.put("/api/sessions/{name}")
async def api_rename_session(name: str, body: dict):
    new_name = body.get("name", "").strip()
    if not new_name:
        return JSONResponse({"ok": False, "error": "Name required"}, status_code=400)
    def _do():
        r = _run(["tmux", "has-session", "-t", name], capture_output=True)
        if r.returncode != 0:
            return False
        _run(["tmux", "rename-session", "-t", name, new_name])
        return True
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, _do)
    if not ok:
        return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
    global _current_session
    if _current_session == name:
        _current_session = new_name
    return JSONResponse({"ok": True})


PREFS_FILE = Path.home() / ".mobile-terminal-prefs.json"


def _load_prefs() -> dict:
    try:
        return json.loads(PREFS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_prefs(data: dict):
    tmp = PREFS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(PREFS_FILE)


@app.get("/api/prefs")
async def api_get_prefs():
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _load_prefs)
    return JSONResponse(data)


@app.put("/api/prefs")
async def api_put_prefs(body: dict):
    def _do():
        data = _load_prefs()
        for k, v in body.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        _save_prefs(data)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _do)
    return JSONResponse({"ok": True})


# --- File browser helpers ---

_session_cwds_cache = None
_session_cwds_time = 0
_SESSION_CWDS_TTL = 5  # seconds — cwds rarely change


def _get_session_cwds():
    """Get unique working directories from all tmux panes (cached, 5s TTL)."""
    global _session_cwds_cache, _session_cwds_time
    now = time.time()
    if _session_cwds_cache is not None and now - _session_cwds_time < _SESSION_CWDS_TTL:
        return _session_cwds_cache
    r = _run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_current_path}"],
        capture_output=True, text=True,
    )
    cwds = set()
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if line:
            cwds.add(os.path.realpath(line))
    _session_cwds_cache = cwds
    _session_cwds_time = now
    return cwds


def _is_path_allowed(path, roots):
    """Check if resolved path is under at least one allowed root."""
    resolved = os.path.realpath(path)
    for root in roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


def _list_files_sync(path):
    roots = _get_session_cwds()
    resolved = os.path.realpath(path)
    if not _is_path_allowed(resolved, roots):
        return {"detail": "Access denied: path outside session directories"}, 403
    if not os.path.isdir(resolved):
        return {"detail": "Not a directory"}, 404
    items = []
    try:
        entries = sorted(os.scandir(resolved), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return {"detail": "Permission denied"}, 403
    for entry in entries:
        if entry.name in ('.', '..', '.git', '__pycache__', 'node_modules', '.DS_Store'):
            continue
        if entry.is_dir(follow_symlinks=False):
            items.append({"name": entry.name, "type": "dir", "path": os.path.join(resolved, entry.name)})
        elif entry.is_file():
            try:
                st = entry.stat()
                items.append({"name": entry.name, "type": "file", "path": os.path.join(resolved, entry.name), "size": st.st_size})
            except OSError:
                pass
    parent = os.path.dirname(resolved)
    if not _is_path_allowed(parent, roots) or parent == resolved:
        parent = None
    return {"path": resolved, "parent": parent, "items": items, "allowed_roots": sorted(roots)}, 200


@app.get("/api/files")
async def api_list_files(path: str):
    loop = asyncio.get_running_loop()
    data, status = await loop.run_in_executor(None, _list_files_sync, path)
    return JSONResponse(data, status_code=status)


def _read_file_sync(path):
    roots = _get_session_cwds()
    resolved = os.path.realpath(path)
    if not _is_path_allowed(resolved, roots):
        return {"detail": "Access denied: path outside session directories"}, 403
    if not os.path.isfile(resolved):
        return {"detail": "File not found"}, 404
    try:
        size = os.path.getsize(resolved)
    except OSError:
        return {"detail": "Cannot access file"}, 403
    if size > 1_048_576:
        return {"detail": "File too large (>1MB)"}, 413
    try:
        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (OSError, PermissionError):
        return {"detail": "Cannot read file"}, 403
    name = os.path.basename(resolved)
    mtime = os.path.getmtime(resolved)
    return {"path": resolved, "name": name, "content": content, "size": size, "mtime": mtime}, 200


@app.get("/api/files/read")
async def api_read_file(path: str):
    loop = asyncio.get_running_loop()
    data, status = await loop.run_in_executor(None, _read_file_sync, path)
    return JSONResponse(data, status_code=status)


def _file_mtime_sync(path):
    roots = _get_session_cwds()
    resolved = os.path.realpath(path)
    if not _is_path_allowed(resolved, roots):
        return {"detail": "Access denied"}, 403
    if not os.path.isfile(resolved):
        return {"detail": "File not found"}, 404
    try:
        mtime = os.path.getmtime(resolved)
    except OSError:
        return {"detail": "Cannot access file"}, 403
    return {"path": resolved, "mtime": mtime}, 200


@app.get("/api/files/mtime")
async def api_file_mtime(path: str):
    loop = asyncio.get_running_loop()
    data, status = await loop.run_in_executor(None, _file_mtime_sync, path)
    return JSONResponse(data, status_code=status)


def _write_file_sync(file_path, content, expected_mtime):
    roots = _get_session_cwds()
    resolved = os.path.realpath(file_path)
    if not _is_path_allowed(resolved, roots):
        return {"detail": "Access denied: path outside session directories"}, 403
    if not os.path.exists(resolved):
        return {"detail": "File not found"}, 404
    if expected_mtime is not None:
        try:
            current_mtime = os.path.getmtime(resolved)
        except OSError:
            return {"detail": "Cannot access file"}, 403
        if abs(current_mtime - expected_mtime) > 0.01:
            return {"detail": "File modified on disk since last read", "mtime": current_mtime}, 409
    try:
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(content)
        new_mtime = os.path.getmtime(resolved)
    except (OSError, PermissionError) as e:
        return {"detail": "Cannot write file: " + str(e)}, 403
    return {"ok": True, "mtime": new_mtime}, 200


@app.put("/api/files/write")
async def api_write_file(body: dict):
    loop = asyncio.get_running_loop()
    data, status = await loop.run_in_executor(
        None, _write_file_sync,
        body.get("path", ""), body.get("content", ""), body.get("mtime"))
    return JSONResponse(data, status_code=status)


if __name__ == "__main__":
    if not shutil.which("tmux"):
        print("Error: tmux is not installed. Install it first:")
        print("  macOS:  brew install tmux")
        print("  Ubuntu: sudo apt install tmux")
        sys.exit(1)
    uvicorn.run(app, host=HOST, port=PORT)
