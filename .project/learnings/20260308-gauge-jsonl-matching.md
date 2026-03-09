---
type: gotcha
tags: [gauge, context, jsonl, pid, lsof, tmux]
created: 2026-03-08
---

# Per-Window JSONL Matching for Context Gauge

## What
Matching each tmux window to its specific Claude JSONL transcript is non-trivial because Claude doesn't expose its session UUID externally.

## Context
Integrating context gauge (% used, burn rate, est turns) into mobile-terminal sidebar required per-window accuracy — all windows in the same project share a CWD, so CWD-based slug matching gives them all the same percentage.

## Symptoms & Fixes

| Symptom | Cause | Fix |
|---|---|---|
| All windows in same project show identical context % | CWD→slug mapping returns same JSONL for all | TTY→PID→UUID matching pipeline |
| `gauge.py --all --json` hangs for 10+ seconds | Session resolver runs `lsof -p` per Claude process | Inline JSONL parsing (~200 lines), batch `lsof` call |
| Birthtime correlation gives wrong matches | Claude processes are long-lived, create new JSONLs on `/clear` | Use activity-time correlation (tmux window_activity ↔ JSONL mtime) instead |
| Some windows show no gauge data | No tasks UUID in lsof + ambiguous activity matching | Expected fallback — show nothing rather than wrong data |

## What Works
Pipeline: tmux `list-panes -a` → `ps -eo pid,ppid,comm` (process tree) → `lsof -p <all_pids>` (single batch call, ~0.4s for 24 PIDs) → match fd0 TTY to pane, tasks UUID to JSONL. Fallback: sort unmatched by activity/mtime and pair up.

## Key Files
- `server.py` — `_refresh_gauge_cache()`, `_gauge_extract_usage()`, `_gauge_compute()`
