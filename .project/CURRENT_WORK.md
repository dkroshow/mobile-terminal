# Current Work

## Recently Completed
- **2026-03-09**: Gauge stability fix — replaced unstable activity-based JSONL matching with sticky match cache (`_gauge_match`). lsof UUID matching was broken (Claude doesn't keep tasks files open), causing all matches to fall through to heuristic pairing that flip-flopped every 30s. Now matches are locked per PID.
- **2026-03-09**: Context gauge UX — moved gauge from floating overlay to inside input bar (above send button); fixed % semantics (all "remaining" now, matching Claude's model); unified color coding via `_ctxCls()` (>25%=default, ≤25%=orange, ≤10%=red); added global bar gauge for single-pane mode
- **2026-03-08**: Context gauge integration — inlined JSONL-based context window utilization into server.py (avoids subprocess to gauge.py which hung due to session resolver). Per-window matching via TTY→Claude PID→tasks UUID→JSONL. Sidebar shows per-window context %, pane overlay shows "N% / ~M turns", details modal shows burn rate + est turns. Cross-validation (gauge drift) when both gauge and CC status bar data available. CLAUDE.md updated with cross-project role section.
- **2026-03-07**: Fix Raw view word-wrap rejoining (3 issues), file tab tooltip, mobile add-pane button
- **2026-03-04**: Window popup button swap, bare filename hyperlinks, pane limit 6→12, cross-pane drag fix, scroll-to-bottom on tab switch

See `.project/PAST_WORK.md` for older history.

## Active Work
None

## Session Notes

### Session 2026-03-09 (gauge stability fix)
- lsof UUID matching was completely broken — Claude never keeps `~/.claude/tasks/` files open, so lsof never finds them
- ALL matching fell through to activity-based heuristic which re-paired windows and JSONLs every 30s by sorting both by recency
- With 12 active JSONLs for galaxy project, the pairing flip-flopped constantly — windows got matched to different conversations' metrics each refresh
- Fix: `_gauge_match` sticky cache maps `"session:window"` → `{stem, pid}`. Match persists until Claude PID changes (process restart = new session)
- Removed lsof step entirely, simplified to two-pass: cached matches first, then mtime-based assignment for new (unclaimed) windows
- Verified: numbers now hold steady across cache refreshes (only active conversations show natural drift from new tokens)

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` runs N capture-pane calls (one per window) — could be slow with many windows
- Gauge matching: initial assignment for multi-window same-project setups may still pick wrong JSONL on first match (most-recent mtime heuristic), but at least stays consistent once assigned
