# Current Work

## Recently Completed
- **2026-03-13**: Fixed focusTab crash + new_window race — `state` used before declaration crashed init (→ "Connecting..."); `new_window()` ignored `-P` output and used separate `display-message` (race sent commands to wrong window)
- **2026-03-13**: Code quality deep-clean — fixed self-referencing `--accent-focus` CSS var, 6 async endpoints blocking event loop (`run_in_executor`), sidebar sort comparator bug; eliminated 5 major duplications (`_sendCmd`, `_initMarked`, `setupTextareaInput`, duplicate CSS, duplicate `cursor:pointer`)
- **2026-03-13**: Gauge 1M context support — per-model threshold (`GAUGE_THRESHOLD_200K`=170k, `GAUGE_THRESHOLD_1M`=1M); model extracted from JSONL `message.model`; fixed model detection (checked for `"1m"` but model IDs like `claude-opus-4-6` don't contain it — now checks `"4-6"`)

See `.project/PAST_WORK.md` for older history.

## Active Work
None currently active.

## Known Issues
- **Gauge lock stale on same-PID session restart**: `cc_fresh` detection evicts the lock. Remaining edge case: non-fresh JSONL switch (rare).
- **Sidebar timestamps occasionally blank on phone**: May be iOS Safari caching — needs investigation on actual device.

## Session Notes

### Session 2026-03-13 (raw view wrap-rejoin fix)
- Wrap-rejoin regex required exactly 2-space indent — CC TUI bullet continuations with 3+ spaces weren't joined, leaving broken lines
- Changed `^  \\S` → `^ {2,}\\S` and `^  [a-zA-Z]` → `^ {2,}[a-zA-Z]`

### Session 2026-03-13 (focusTab crash + new_window race)
- `focusTab` crash: `state` used before `const` declaration in notepadOpen check → crashed init → "Connecting..." on page load
- `new_window()` race: ignored `-P -F` output from `tmux new-window`, ran separate `display-message` — race between calls sent startup commands to wrong window (current pane instead of new one)

### Session 2026-03-13 (code quality deep-clean)
- Thorough audit of all 5,973 lines of `server.py`
- **Critical fixes**: `--accent-focus` CSS self-reference (focus rings broken), 6 file I/O endpoints missing `run_in_executor` (same bug class as 95% CPU incident), sidebar sort comparator returning wrong sign for `ib < 0` case (2 occurrences)
- **Deduplication**: `sendToPane`/`sendGlobal` → shared `_sendCmd(tabId, text, ta)`; `md()`/`mdFile()` renderer → shared `_initMarked()`; Enter key handling x3 → shared `setupTextareaInput(ta, sendFn)`; removed duplicate `.fb-reader-body` CSS block (28 lines); removed duplicate `cursor:pointer` on `.sb-win`
- Net: 128 insertions, 181 deletions. Server restarted and verified (18ms page load, all endpoints 200)

## Up Next
- Investigate iOS Safari blank timestamps on actual device
- Gauge stale lock: find lightweight way to detect same-PID JSONL switch
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: batch N `capture-pane` calls into single tmux call
- Gauge metrics: incremental JSONL reads (file size check, seek to tail)
