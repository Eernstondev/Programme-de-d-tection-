#!/usr/bin/env bash
# Démarre la surveillance automatiquement à l'ouverture de session et la relance si elle plante.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.surveillance.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -x "$DIR/.venv/bin/python" ] || { echo "Environnement .venv introuvable dans $DIR"; exit 1; }
[ -f "$DIR/config.yaml" ] || { echo "config.yaml introuvable dans $DIR"; exit 1; }
(cd "$DIR" && .venv/bin/python -c "from app.config import load_config; load_config('config.yaml')") \
  || { echo "Corrige config.yaml / .env avant l'installation"; exit 1; }

mkdir -p "$DIR/data/logs" "$HOME/Library/LaunchAgents"
sed "s|__DIR__|$DIR|g" "$DIR/deploy/macos/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ Surveillance installée : elle démarre maintenant et à chaque ouverture de session."
echo "   Logs   : tail -f \"$DIR/data/logs/surveillance.log\""
echo "   Arrêt  : bash \"$DIR/deploy/uninstall_macos.sh\""
