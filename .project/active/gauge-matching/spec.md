# Gauge JSONL Matching Redesign

## Problem
The context gauge matches tmux windows to Claude JSONL transcripts using a 30s polling cycle with mtime heuristics. When multiple Claude instances share a project directory (e.g., 8 windows all in `/Users/kd/Code/galaxy`), the mtime-based guess frequently picks the wrong JSONL — observed 45-point drift (gauge: 45%, Claude: 0%).

## Business Goal
Accurate, reliable context gauge that correctly tracks each Claude instance's context usage, even with many concurrent sessions in the same project.

## Requirements

### FR-1: Front-loaded matching
Match effort happens at session start (when a new Claude PID appears). Once a tmux window is matched to a JSONL, the match is permanent for the life of that Claude PID.

### FR-2: Easy case — single new instance
When only one unmatched Claude instance exists for a project slug, match it to the most recently modified unclaimed JSONL. No text comparison needed.

### FR-3: Hard case — multiple new instances
When multiple unmatched Claude instances share a slug, use text matching: compare text sent through mobile-terminal (tmux capture) with JSONL user message content to resolve ambiguity.

### FR-4: Locked matches never revisited
Once matched, the gauge only re-reads the JSONL for updated metrics (usage/tokens). It never re-evaluates the match itself. The match persists until the Claude PID disappears.

### FR-5: Gauge metrics refresh
Matched windows still need periodic metric updates (read latest usage from their locked JSONL). This is the only periodic work — not re-matching.

## Acceptance Criteria

### AC-1: Correct match with concurrent sessions
With 8 Claude instances in the same project, each window's gauge should track its own JSONL (drift < 5% vs Claude's status bar).

### AC-2: Fast match for single instance
When one new Claude instance starts alone, match should resolve on the next gauge refresh cycle (within seconds, not minutes).

### AC-3: Text-based match resolves ambiguity
When multiple instances start near-simultaneously, the system should resolve matches once user messages flow through, by comparing tmux capture text with JSONL content.

### AC-4: No match churn
Once matched, gauge values should be stable (no flip-flopping between JSONLs).

### AC-5: Graceful unmatched state
Windows with unresolved matches show no gauge data (rather than wrong data).

## Out of Scope
- `_gauge_extract_usage` / `_gauge_compute` internals
- Dashboard API response shape
- Client-side gauge rendering
- Non-CC windows (they already don't get gauge data)
