#!/bin/bash
# Uninstall cache-timer plugin for Claude Code

set -e

SETTINGS_FILE="$HOME/.claude/settings.json"

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "cache-timer: No settings.json found, nothing to uninstall."
  exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "cache-timer: jq is required to uninstall. Remove hooks manually from $SETTINGS_FILE"
  exit 1
fi

cp "$SETTINGS_FILE" "${SETTINGS_FILE}.bak"

TMP=$(mktemp)

# Remove statusLine if it points to our script
jq '
  if (.statusLine.command // "" | contains("cache-timer")) then del(.statusLine) else . end |
  .hooks.Stop = [.hooks.Stop[]? | select(.hooks[]?.command // "" | contains(".last-claude-response") | not)] |
  .hooks.UserPromptSubmit = [.hooks.UserPromptSubmit[]? | select(.hooks[]?.command // "" | contains("cache-timer-active") | not)] |
  if (.hooks.Stop | length) == 0 then del(.hooks.Stop) else . end |
  if (.hooks.UserPromptSubmit | length) == 0 then del(.hooks.UserPromptSubmit) else . end |
  if (.hooks | length) == 0 then del(.hooks) else . end
' "$SETTINGS_FILE" > "$TMP" && mv "$TMP" "$SETTINGS_FILE"

echo "cache-timer: Uninstalled. Restart Claude Code to apply."
echo "cache-timer: Backup at ${SETTINGS_FILE}.bak"
