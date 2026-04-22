#!/bin/bash
# Warns before a prompt if Claude's cache has likely expired (>5 min idle)
# Runs as a UserPromptSubmit hook

TIMESTAMP_FILE="$HOME/.claude/.last-claude-response"

if [ ! -f "$TIMESTAMP_FILE" ]; then
  exit 0
fi

LAST=$(cat "$TIMESTAMP_FILE" 2>/dev/null)
if [ -z "$LAST" ] || ! [[ "$LAST" =~ ^[0-9]+$ ]]; then
  exit 0
fi

NOW=$(date +%s)
ELAPSED=$((NOW - LAST))

if [ $ELAPSED -gt 300 ]; then
  MINUTES=$((ELAPSED / 60))
  if [ $MINUTES -lt 60 ]; then
    IDLE_STR="${MINUTES}min"
  else
    HOURS=$((MINUTES / 60))
    MINS_REM=$((MINUTES % 60))
    IDLE_STR="${HOURS}h ${MINS_REM}min"
  fi
  echo "⚠️  Cache expired (${IDLE_STR} idle) — this turn will rebuild full context from scratch at write pricing. Run /compact first to reduce cost, or proceed." >&2
fi

exit 0
