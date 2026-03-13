# Research: Text Loss on Send

**Date:** 2026-03-13
**Status:** Complete
**Researcher:** Claude

---

## TL;DR

Found 3 bugs that can cause text to vanish after pressing Enter:

1. **`sendToPane` catches errors but `renderOutput` can throw** — the `renderOutput()` call at line 4633 runs inside the try block BEFORE the fetch. If it throws (parser error, DOM access on stale element, etc.), the catch block tries to restore `ta.value`, but if `ta` references a textarea from a pane that was concurrently removed or restructured, the restore writes to a detached DOM node. The user's text is gone.

2. **Backend silent success on tmux failure** — `send_keys()` can fail in multiple ways (load-buffer returns non-zero, paste-buffer fails, timeout) but `/api/send` always returns `{"ok": true}`. The client thinks delivery succeeded and doesn't restore text.

3. **(Primary suspect) Race between `sendToPane`/`sendGlobal` and the `focusTab` draft system** — When the user presses Enter, `sendToPane` clears `ta.value` at line 4626 (synchronous), then `await fetch(...)` yields. During the await, a `focusTab` call (from tab click, sidebar click, keyboard shortcut, or `loadDashboard` → `renderPaneTabs` flow) saves the textarea's current value as `draft` (line 3548: `tabStates[p.activeTabId].draft = ta ? ta.value : '';`). Since `ta.value` was already cleared by the send, the draft becomes `""`. When focus returns to the original tab, the empty draft overwrites any restored text. More importantly: if the focus switch happens AFTER `ta.value = ''` but BEFORE the fetch completes, and then the fetch fails, the catch restores `ta.value = text`, but a subsequent `focusTab` back will overwrite it with the empty draft.

## Root Cause Analysis

The core issue: **clearing the textarea is an irreversible side-effect done optimistically before the async operation completes**. The catch block's restoration is fragile — it races against the draft save system, pane removals, and any other code that reads `ta.value`.

## Fix

1. Save the text into `tabStates[tabId]._sendingText` as a backup before clearing
2. After successful send, clear the backup
3. In the draft save path, if `_sendingText` exists, use it instead of `ta.value`
4. Make the backend return actual success/failure for tmux operations
5. Add a `console.warn` on send failure so it's debuggable

---

## Recommendations

| # | Recommendation | Evidence |
|---|---|---|
| 1 | Save backup text in tabStates before clearing textarea | sendToPane:4626 clears ta.value; focusTab:3548 saves ta.value as draft |
| 2 | Return tmux command success from /api/send | send_keys:521 returns early on failure; api_send:5574 always returns ok:true |
| 3 | Add console.warn on catch | sendToPane:4639 catch block is silent |
