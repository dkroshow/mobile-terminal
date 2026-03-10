# Current Work

## Recently Completed
- **2026-03-10**: Gauge matching redesign — text-based scoring replaces mtime heuristic. `_gauge_locks` persisted to `~/.mobile-terminal-gauge-locks.json` (survives server restart). Bootstrap extracts chunked paragraphs from tmux capture (user + assistant text). Scores against both user and assistant JSONL messages. 7-day cutoff for stale sessions. 19/19 windows matched correctly, recon 2 drift 45% → 0.9%.
- **2026-03-09**: Gauge stability fix — replaced unstable activity-based JSONL matching with sticky match cache (`_gauge_match`). lsof UUID matching was broken (Claude doesn't keep tasks files open), causing all matches to fall through to heuristic pairing that flip-flopped every 30s. Now matches are locked per PID.
- **2026-03-09**: Context gauge UX — moved gauge from floating overlay to inside input bar (above send button); fixed % semantics (all "remaining" now, matching Claude's model); unified color coding via `_ctxCls()` (>25%=default, ≤25%=orange, ≤10%=red); added global bar gauge for single-pane mode
- **2026-03-08**: Context gauge integration — inlined JSONL-based context window utilization into server.py (avoids subprocess to gauge.py which hung due to session resolver). Per-window matching via TTY→Claude PID→tasks UUID→JSONL. Sidebar shows per-window context %, pane overlay shows "N% / ~M turns", details modal shows burn rate + est turns. Cross-validation (gauge drift) when both gauge and CC status bar data available. CLAUDE.md updated with cross-project role section.
- **2026-03-07**: Fix Raw view word-wrap rejoining (3 issues), file tab tooltip, mobile add-pane button
- **2026-03-04**: Window popup button swap, bare filename hyperlinks, pane limit 6→12, cross-pane drag fix, scroll-to-bottom on tab switch

See `.project/PAST_WORK.md` for older history.

## Active Work
None

## Session Notes

### Session 2026-03-10 (gauge matching redesign)
- Old approach: mtime heuristic every 30s — with 8 galaxy windows and 34 JSONLs, frequently picked wrong JSONL (45% drift on recon 2)
- New approach: text-based scoring with permanent locks persisted to disk
- Key insight: generic phrases ("I'm picking up where a previous session left off") match 12+ JSONLs — scoring by match count with unique-winner requirement is essential
- Individual wrapped tmux lines too short to be distinctive — chunking consecutive lines into paragraphs solved it (166 lines → fewer but longer/unique chunks)
- Matching both user AND assistant JSONL messages (not just user) dramatically improved bootstrap hit rate
- 7-day JSONL cutoff (was 24h) — catches stale sessions that haven't had new turns
- Disk persistence (`~/.mobile-terminal-gauge-locks.json`) — locks survive server restart, no re-matching needed
- Easy case (1 unmatched per slug) still uses mtime; hard case uses text scoring
- 19/19 windows matched on cold bootstrap. 2 initially unmatched (welcome screen only) resolved on next cycle
- Metrics polling redesign deferred — user wants to explore separately
- Spec/plan at `.project/active/gauge-matching/`
- Audit commands documented in conversation for verifying lock accuracy

## Up Next
- Consider adding basic authentication (API key or simple auth)
- Chat mode could show tool call summaries (collapsed details) instead of hiding them entirely
- Consider WebSocket for lower-latency updates (currently 1s polling)
- Dashboard performance: `get_dashboard()` runs N capture-pane calls (one per window) — could be slow with many windows
- Gauge metrics polling: currently re-reads full JSONL every 30s for all matched windows — could be optimized (e.g., incremental reads, file size check)
