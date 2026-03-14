---
type: gotcha
tags: [tmux, race-condition, new-window]
created: 2026-03-13
---

# tmux new-window: use -P output directly, not separate display-message

## What
`tmux new-window -P -F "#{window_index}"` prints the new window's index to stdout. Using a separate `tmux display-message -t session -p "#{window_index}"` to get the index introduces a race — another operation can change the active window between the two commands.

## Context
`new_window()` was ignoring the `-P` output and running `display-message` to get the active window index. This caused startup commands (`claude --dangerously-skip-permissions`) to be sent to the wrong window (the previously-active window instead of the new one).

## What Didn't Work
```python
_run(["tmux", "new-window", "-t", target, "-c", work_dir, "-P", "-F", "#{window_index}"],
     capture_output=True, text=True)  # output ignored!
r = _run(["tmux", "display-message", "-t", target, "-p", "#{window_index}"],
         capture_output=True, text=True)
new_idx = r.stdout.strip()  # returns CURRENT active window, not necessarily the new one
```

## What Works
```python
r = _run(["tmux", "new-window", "-t", target, "-c", work_dir, "-P", "-F", "#{window_index}"],
         capture_output=True, text=True)
if r.returncode != 0:
    return None
new_idx = r.stdout.strip()  # guaranteed to be the new window's index
```

## Key Files
- server.py
