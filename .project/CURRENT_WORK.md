# Current Work

## Recently Completed
- **2026-03-09**: Context gauge UX — moved gauge from floating overlay to inside input bar (above send button); fixed % semantics (all "remaining" now, matching Claude's model); unified color coding via `_ctxCls()` (>25%=default, ≤25%=orange, ≤10%=red); added global bar gauge for single-pane mode
- **2026-03-08**: Context gauge integration — inlined JSONL-based context window utilization into server.py (avoids subprocess to gauge.py which hung due to session resolver). Per-window matching via TTY→Claude PID→tasks UUID→JSONL. Sidebar shows per-window context %, pane overlay shows "N% / ~M turns", details modal shows burn rate + est turns. Cross-validation (gauge drift) when both gauge and CC status bar data available. CLAUDE.md updated with cross-project role section.
- **2026-03-07**: Fix Raw view word-wrap rejoining (3 issues), file tab tooltip, mobile add-pane button
- **2026-03-04**: Window popup button swap, bare filename hyperlinks, pane limit 6→12, cross-pane drag fix, scroll-to-bottom on tab switch

See `.project/PAST_WORK.md` for older history.

## Active Work
None

## Session Notes

### Session 2026-03-09 (gauge UX + % consistency)
- `gauge_context_pct` was "% used" (0=fresh, 100=full) but `cc_context_pct` was "% remaining" (100=fresh, 0=full) — inverted at server level (`100 - pct_used`) so both are "remaining"
- Color coding was inconsistent: gauge path used ≥75/≥50 thresholds (treating value as "used"), CC path used ≤10/≤25 (treating as "remaining"). Unified via `_ctxCls(pct)` helper
- Gauge moved from `.pane-gauge` absolute overlay to inside `.pane-input` div (before input-row). Also added `#global-gauge` to `#bar` for single-pane mode
- Details modal bar now fills to show remaining (was filling to show used), labels say "X% left"

### Session 2026-03-08 (gauge integration)
- Gauge data inlined (~200 lines) rather than subprocess to `gauge.py` — session resolver's `lsof -p` per process hangs with many panes
- Per-window JSONL matching: TTY matching (tmux pane_tty ↔ Claude PID fd0) + tasks UUID (lsof) for exact match; activity-time correlation as fallback
- Birthtime matching attempted but abandoned — Claude processes are long-lived and create new JSONLs on `/clear`, so process start time doesn't match JSONL birthtime
- Activity-based fallback sorts unmatched windows by tmux activity and unmatched JSONLs by mtime, then pairs them up — best-effort, may misassign in multi-window same-project setups
- `updateSidebarStatus` fixed to not wipe gauge context % on 1s poll (targeted DOM update instead of innerHTML)
- Gauge cache refreshes every 30s via `_refresh_gauge_cache()`, keyed by `session:window`
- Cross-validation: `gauge_drift` = |gauge_pct_used - (100 - cc_remaining)|, shown as "!" in sidebar and detailed in modal

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` runs N capture-pane calls (one per window) — could be slow with many windows
- Gauge accuracy: activity-based fallback matching may misassign JSONLs for multi-window same-project setups
