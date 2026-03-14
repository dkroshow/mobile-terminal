---
type: gotcha
tags: [gauge, context, threshold, compression, auto-compact, 1m]
created: 2026-03-10
---

# Gauge Threshold Must Match CC Auto-Compact Ceiling (Per-Model)

## What
Gauge threshold is per-model, not a single constant. `_gauge_extract_usage` returns the model name from JSONL; `_gauge_threshold_for_model()` picks the right threshold. Currently: 170k for 200k-context models, 1M for 1M-context models.

## Context
User reported gauge showing 1% remaining when CC wasn't showing any warning (threshold was 165k, session at 164k tokens). Raised to 200k, then user reported gauge showing 17% remaining while CC was actively compacting. Fixed with 170k for 200k models. Later, CC added 1M context window support — threshold split into per-model constants.

## What Didn't Work
- **165k threshold**: Too tight. Sessions at 164k showed 0.6% remaining even though CC's status bar showed no context warning.
- **200k threshold**: Too loose. When CC actually compacted at ~167k tokens, the gauge showed 17% remaining.
- **Single global threshold**: Wrong for mixed-model environments (200k + 1M sessions).

## What Works
- **GAUGE_THRESHOLD_200K = 170k**: Derived from 18 observed compression events. Max was 168,248, median 166,624.
- **GAUGE_THRESHOLD_1M = 1M**: Full window for now — auto-compact ceiling TBD (needs empirical observation).
- **Model detection**: `message.model` field in assistant JSONL entries; checks for `"4-6"` (Claude 4.6 family = 1M context), `"4.6"`, or `"1m"` substring. Original `"1m"` check was broken — actual model IDs (`claude-opus-4-6`, `claude-sonnet-4-6`) don't contain "1m".

## Key Files
- `server.py` lines 43-44: `GAUGE_THRESHOLD_200K` / `GAUGE_THRESHOLD_1M`
- `server.py` `_gauge_threshold_for_model()`: model → threshold mapping
- `server.py` `_gauge_extract_usage()`: returns `(usage, last_ts, model)` tuple
- `server.py` `_gauge_cache_metrics()`: passes model-aware threshold to `_gauge_compute()`
