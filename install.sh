#!/bin/bash
# Install cache-timer plugin for Claude Code
# Sets up the statusLine config and hooks

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS_FILE="$HOME/.claude/settings.json"

echo "cache-timer: Installing..."

if ! command -v jq >/dev/null 2>&1; then
  echo "cache-timer: jq is required but not found. Install it first:"
  echo "  brew install jq    # macOS"
  echo "  apt install jq     # Linux"
  exit 1
fi

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "cache-timer: Creating $SETTINGS_FILE"
  echo '{}' > "$SETTINGS_FILE"
fi

# Backup
cp "$SETTINGS_FILE" "${SETTINGS_FILE}.bak"
echo "cache-timer: Backup saved to ${SETTINGS_FILE}.bak"

TMP=$(mktemp)

# Set statusLine
jq --arg cmd "$SCRIPT_DIR/scripts/statusline.sh" \
  '.statusLine = {"type": "command", "command": $cmd, "refreshInterval": 1, "padding": 0}' \
  "$SETTINGS_FILE" > "$TMP" && mv "$TMP" "$SETTINGS_FILE"
echo "cache-timer: Updated statusLine"

# Add Stop hook (writes timestamp on every Claude response)
STOP_CMD='TS=$(date +%s); echo $TS > $HOME/.claude/.last-claude-response; SID=$(cat | jq -r '"'"'.session_id // empty'"'"' 2>/dev/null); if [ -n "$SID" ]; then mkdir -p $HOME/.claude/.cache-timer && echo $TS > $HOME/.claude/.cache-timer/$SID; fi; find $HOME/.claude/.cache-timer -type f -mtime +1 -delete 2>/dev/null; true'

TMP=$(mktemp)
jq --arg cmd "$STOP_CMD" '
  .hooks.Stop //= [] |
  if (.hooks.Stop | map(.hooks[]?.command // "" | contains(".last-claude-response")) | any)
  then .
  else .hooks.Stop += [{"hooks": [{"type": "command", "command": $cmd, "timeout": 2}]}]
  end
' "$SETTINGS_FILE" > "$TMP" && mv "$TMP" "$SETTINGS_FILE"
echo "cache-timer: Added Stop hook"

# Add UserPromptSubmit hook (marks active state + warns on expired cache)
SUBMIT_CMD="TS=\$(date +%s); echo \$TS > \$HOME/.claude/.cache-timer-active; SID=\$(cat | jq -r '.session_id // empty' 2>/dev/null); if [ -n \"\$SID\" ]; then mkdir -p \$HOME/.claude/.cache-timer && echo \$TS > \$HOME/.claude/.cache-timer/\${SID}.active; fi; bash \"$SCRIPT_DIR/scripts/cache-expiry-warn.sh\""

TMP=$(mktemp)
jq --arg cmd "$SUBMIT_CMD" '
  .hooks.UserPromptSubmit //= [] |
  if (.hooks.UserPromptSubmit | map(.hooks[]?.command // "" | contains("cache-timer-active")) | any)
  then .
  else .hooks.UserPromptSubmit += [{"hooks": [{"type": "command", "command": $cmd, "timeout": 5}]}]
  end
' "$SETTINGS_FILE" > "$TMP" && mv "$TMP" "$SETTINGS_FILE"
echo "cache-timer: Added UserPromptSubmit hook"

# Create state directory
mkdir -p "$HOME/.claude/.cache-timer"

echo ""
echo "cache-timer: Installed! Restart Claude Code to see the cache timer in your status line."
echo ""
echo "  States:"
echo "    ◉ cached     — cache is warm (green)"
echo "    ◎ 1m30s      — expiring soon (amber)"
echo "    ○ expired 5m — cache gone, shows rebuild cost for large sessions"
echo "    ○ 2h stale   — suggests /compact or new session"
echo ""
echo "  To uninstall: bash $SCRIPT_DIR/uninstall.sh"
