---
type: gotcha
tags: async, event-loop, run_in_executor, css, sort, duplication
created: 2026-03-13
---

# Quality Audit — Recurring Bug Patterns

## What
Three classes of bugs keep recurring in this codebase due to structural patterns.

## Symptoms & Fixes

| Symptom | Cause | Fix |
|---|---|---|
| Server at 95%+ CPU, page loads take seconds | New async endpoint does sync file I/O or subprocess calls without `run_in_executor` | Every new endpoint with file/subprocess I/O must use `loop.run_in_executor(None, sync_fn)`. Extract sync work into `_*_sync()` helpers returning `(data, status_code)` tuples |
| Same bug needs fixing in multiple places, fix works in one context but not another | Core logic duplicated across functions (e.g. `sendToPane`/`sendGlobal` were 90% identical) | Extract shared logic immediately when pattern appears. Key extractions done: `_sendCmd()`, `_initMarked()`, `setupTextareaInput()` |
| Custom sidebar/file-tree ordering doesn't work | Sort comparator returns wrong sign: `if (ib < 0) return 1` should be `return -1` | Always test sort comparators with all 4 cases: both in order, both not, a-only, b-only |

## Context
Audit triggered by user reporting "the same bugs keep popping up." Root cause was structural: duplication meant fixes needed to be applied N times, and the event-loop blocking pattern wasn't caught on new endpoints because the original batch was fixed but the pattern wasn't documented as a rule.

## Key Files
- `server.py` — all endpoints, all JS functions
