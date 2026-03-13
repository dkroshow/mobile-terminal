---
type: gotcha
tags: send, textarea, draft, async, race-condition, focusTab
created: 2026-03-13
---

# Send Text Loss — Async/Draft Race Condition

## What
Submitted text can vanish when `sendToPane`/`sendGlobal` clears the textarea optimistically before `await fetch()`, and `focusTab` runs during the await (saving the empty textarea as the tab's draft).

## Context
User reported text disappearing on desktop — typed text, pressed Enter, text vanished without being delivered. Happened multiple times.

## What Didn't Work
- Looking only at the catch/restore path — the restore itself was correct
- Assuming single-threaded JS means no races — async/await yields create windows for other code to run

## What Works
Three-part fix:
1. **`_sendingText` backup**: Before clearing textarea, save text in `tabStates[tabId]._sendingText`. Draft save in `focusTab` checks this first (`_st._sendingText || ta.value`). Cleared after fetch completes (success or failure).
2. **Inner try-catch on `renderOutput`**: The `renderOutput()` call was inside the same try block as `fetch()`. If parser threw, send never happened but textarea was cleared. Now wrapped in its own try-catch.
3. **Backend failure propagation**: `send_keys()` returns bool, `/api/send` returns 500 on tmux failure. Previously always returned `{"ok": true}`.

## Key Files
- `server.py` — `sendToPane()`, `sendGlobal()`, `focusTab()` draft save, `send_keys()`, `api_send()`
- `.project/research/text-loss-bug.md` — full analysis
