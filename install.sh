#!/bin/bash
# Install mobile-terminal as a macOS LaunchAgent (always-on background service)
set -e

LABEL="com.kd.mobile-terminal"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVER="$(cd "$(dirname "$0")" && pwd)/server.py"
PYTHON="$(which python3)"
TMUX="$(which tmux)"

if [ ! -f "$SERVER" ]; then
    echo "Error: server.py not found at $SERVER"
    exit 1
fi

if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found in PATH"
    exit 1
fi

if [ -z "$TMUX" ]; then
    echo "Error: tmux not found in PATH. Install it first:"
    echo "  macOS:  brew install tmux"
    echo "  Ubuntu: sudo apt install tmux"
    exit 1
fi

# Check dependencies
if ! "$PYTHON" -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "Error: Missing Python dependencies. Run:"
    echo "  pip3 install -r requirements.txt"
    exit 1
fi

# Unload existing agent if present
if launchctl list "$LABEL" &>/dev/null; then
    echo "Stopping existing service..."
    launchctl unload "$PLIST" 2>/dev/null || true
fi

# Get the directory containing tmux (and other tools) for PATH
TMUX_DIR="$(dirname "$TMUX")"

echo "Creating LaunchAgent plist..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SERVER</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$(dirname "$SERVER")</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HOST</key>
        <string>0.0.0.0</string>
        <key>PATH</key>
        <string>$TMUX_DIR:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/tmp/mobile-terminal.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/mobile-terminal.log</string>
</dict>
</plist>
EOF

echo "Loading service..."
launchctl load "$PLIST"

# Verify
sleep 2
if curl -s -o /dev/null -w "" "http://127.0.0.1:7681/" 2>/dev/null; then
    echo ""
    echo "mobile-terminal is running at http://0.0.0.0:7681"
    echo ""
    echo "It will start automatically on login and restart if it crashes."
    echo "Logs: /tmp/mobile-terminal.log"
    echo ""
    echo "To stop:   launchctl unload $PLIST"
    echo "To start:  launchctl load $PLIST"
    echo "To remove: bash $(dirname "$SERVER")/uninstall.sh"
else
    echo ""
    echo "Warning: Service loaded but not responding yet. Check logs:"
    echo "  tail -f /tmp/mobile-terminal.log"
fi
