# mobile-terminal

A minimal web UI for controlling [tmux](https://github.com/tmux/tmux) sessions from your phone or any browser. One Python file, no WebSockets, no JavaScript frameworks.

## Why

Sometimes you need to check on a long-running process, restart a service, or run a quick command on your dev machine — but you only have your phone. This gives you a mobile-friendly terminal interface over HTTP.

## Features

- Send commands and see output in real time (1s polling)
- Multiple tmux windows (create, switch, close)
- Special key buttons (Ctrl-C, Up/Down arrows, Tab, Escape)
- Mobile-optimized: works with iOS keyboard, autocorrect, swipe typing
- Single file, minimal dependencies

## Requirements

- Python 3.8+
- tmux
- macOS or Linux

## Quick Start

```bash
# Install tmux (if you don't have it)
# macOS:
brew install tmux
# Ubuntu/Debian:
sudo apt install tmux

# Clone and run
git clone https://github.com/dkroshow/mobile-terminal.git
cd mobile-terminal
pip install -r requirements.txt
python server.py
```

Open `http://localhost:7681` in your browser.

## Configuration

All settings are via environment variables:

| Variable | Default | Description |
|---|---|---|
| `TMUX_SESSION` | `mobile` | tmux session name |
| `TMUX_WORK_DIR` | `~` (home) | Starting directory for new windows |
| `TERMINAL_TITLE` | `Mobile Terminal` | Browser tab title |
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `7681` | Port number |

Example:

```bash
TMUX_WORK_DIR=~/projects TERMINAL_TITLE="Dev Box" HOST=0.0.0.0 python server.py
```

## Security

This server gives full terminal access to anyone who can reach it. There is **no authentication**.

- The default bind address is `127.0.0.1` (localhost only)
- To access from other devices, set `HOST=0.0.0.0` — but only do this on a trusted network
- For remote access, put it behind a VPN (e.g., [Tailscale](https://tailscale.com)) or an SSH tunnel:

```bash
# SSH tunnel from your phone/laptop
ssh -L 7681:localhost:7681 user@your-server
```

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/api/output` | Current terminal output (JSON) |
| `POST` | `/api/send` | Send a command `{"cmd": "..."}` |
| `GET` | `/api/key/{key}` | Send a special key (C-c, Up, Down, Tab, etc.) |
| `GET` | `/api/windows` | List tmux windows |
| `POST` | `/api/windows/new` | Create a new window |
| `POST` | `/api/windows/{index}` | Switch to window |
| `DELETE` | `/api/windows/{index}` | Close a window |

## License

MIT
