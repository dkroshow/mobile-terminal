---
type: gotcha
tags: [gauge, context, threshold, compression, auto-compact]
created: 2026-03-10
---

# Gauge Threshold Must Match CC Auto-Compact Ceiling

## What
`GAUGE_THRESHOLD` must be set just above the empirical auto-compact trigger point (~170k), NOT the full model context window (200k) and NOT the mean compression point (165k).

## Context
User reported gauge showing 1% remaining when CC wasn't showing any warning (threshold was 165k, session at 164k tokens). Raised to 200k, then user reported gauge showing 17% remaining while CC was actively compacting.

## What Didn't Work
- **165k threshold**: Too tight. Sessions at 164k showed 0.6% remaining even though CC's status bar showed no context warning. Technically the session WAS about to compact, but the number felt misleading.
- **200k threshold**: Too loose. When CC actually compacted at ~167k tokens, the gauge showed 17% remaining — clearly wrong since context was at its limit.

## What Works
- **170k threshold**: Derived from 18 observed compression events. Max was 168,248, median 166,624. The 170k value means:
  - At 164k: ~3.5% remaining (appropriately low)
  - At 167k: ~1.8% remaining (matches compacting state)
  - At 100k: ~41% remaining (healthy mid-session)
- CC's "Context left until auto-compact: X%" in the status bar uses a similar denominator

## Also Fixed
- `cc_fresh` detection now evicts gauge locks: when `/clear` is used (same PID, new JSONL), the old lock is deleted and gauge data suppressed until a new conversation starts

## Key Files
- `server.py` line 43: `GAUGE_THRESHOLD = 170_000`
- `server.py` ~line 672: fresh eviction in `get_dashboard()`
