# Learnings Index

| Date | Type | Tags | Summary | File |
|------|------|------|---------|------|
| 2026-03-04 | gotcha | deployment, launchd, server, debugging, performance | Server must be restarted after editing server.py — stale code causes invisible failures and 95%+ CPU | 20260304-server-restart-after-edit.md |
| 2026-03-08 | gotcha | gauge, context, jsonl, pid, tmux, matching | Per-window JSONL matching — text scoring with unique-highest-score lock, bootstrap via tmux capture | 20260308-gauge-jsonl-matching.md |
| 2026-03-10 | gotcha | gauge, cache, performance, jsonl, locks | Gauge lock eviction must not trigger full JSONL re-reads — "newer file" check caused 8s/cycle cache thrashing | 20260310-gauge-cache-eviction-perf.md |
| 2026-03-10 | gotcha | gauge, context, threshold, compression, auto-compact | GAUGE_THRESHOLD must be ~170k (just above empirical auto-compact ceiling), not 165k or 200k | 20260310-gauge-threshold-tuning.md |
| 2026-03-13 | gotcha | send, textarea, draft, async, race-condition, focusTab | Async send + focusTab draft save race — must backup text in tabStates before clearing textarea | 20260313-send-text-loss-race.md |
| 2026-03-13 | gotcha | async, event-loop, run_in_executor, css, sort, duplication | Recurring bug patterns: event-loop blocking on new endpoints, logic duplication, sort comparator sign errors | 20260313-quality-deep-clean.md |
| 2026-03-13 | gotcha | tmux, race-condition, new-window | tmux new-window -P output must be used directly — separate display-message has race condition | 20260313-new-window-race.md |
| 2026-03-16 | gotcha | drag, performance, polling, render, defer, DOM | Heavy DOM renders (renderOutput/renderSidebar) during drag cause stutter — defer with guard flags | 20260316-drag-render-deferral.md |
