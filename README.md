# mobile-terminal

A minimal web UI for controlling tmux sessions from your phone or any browser. One Python file, no WebSockets, no JavaScript frameworks.

Initiate new claude sessions on your phone. Or, initiate via the tmux window on your machine and continue them via your phone. 

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

## Run as a Background Service (macOS)

To keep the server running permanently (auto-starts on login, restarts on crash):

```bash
./install.sh
```

This creates a macOS LaunchAgent that runs in the background. To remove it:

```bash
./uninstall.sh
```

Manage the service:

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.kd.mobile-terminal.plist

# Start
launchctl load ~/Library/LaunchAgents/com.kd.mobile-terminal.plist

# Logs
tail -f /tmp/mobile-terminal.log
```

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

## Accessing from Your Phone

By default the server only listens on localhost. To access it from another device, set `HOST=0.0.0.0`:

```bash
HOST=0.0.0.0 python server.py
```

**Same network (home Wi-Fi):** Open `http://<your-computer's-local-ip>:7681` on your phone. You can find your local IP with `ifconfig` (macOS/Linux) or `ip addr`.

**Away from home:** You'll need a VPN like [Tailscale](https://tailscale.com) (free) to reach your machine. Install Tailscale on both your computer and phone, then use your Tailscale IP instead.

```bash
# Example: access via Tailscale IP
http://100.x.x.x:7681
```

Alternatively, use an SSH tunnel:

```bash
ssh -L 7681:localhost:7681 user@your-server
```

## Security

This server gives full terminal access to anyone who can reach it. There is **no authentication**.

- The default bind address is `127.0.0.1` (localhost only) — no one else can connect
- Setting `HOST=0.0.0.0` exposes it to your network — only do this on a network you trust
- For access over the internet, **always** use a VPN (Tailscale) or SSH tunnel — never expose the port directly

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
