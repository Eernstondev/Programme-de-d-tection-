#!/usr/bin/env bash
set -euo pipefail

LABEL="com.surveillance.agent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Démarrage automatique désactivé."
