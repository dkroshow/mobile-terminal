#!/bin/bash
# Remove mobile-terminal LaunchAgent
set -e

LABEL="com.kd.mobile-terminal"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST" ]; then
    echo "Stopping service..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm "$PLIST"
    echo "LaunchAgent removed."
else
    echo "No LaunchAgent found at $PLIST"
fi
