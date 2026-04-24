#!/bin/bash
# Cache TTL status line — calm indicator of prompt cache state
# Receives JSON on stdin from Claude Code, outputs ANSI-formatted status

CACHE_DIR="$HOME/.claude/.cache-timer"
TTL_SECONDS=300

# Capture stdin for passthrough
INPUT=$(cat)

# Extract session ID for per-session timestamp tracking
SESSION_ID=""
CACHED_TOKENS=0
if command -v jq >/dev/null 2>&1; then
  SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
  CACHED_TOKENS=$(echo "$INPUT" | jq -r '
    .tokens.cached // .cached_tokens // .totalCachedTokens // 0
  ' 2>/dev/null)
  [ -z "$CACHED_TOKENS" ] && CACHED_TOKENS=0
fi

# On by default — check if explicitly disabled for this session
if [ -n "$SESSION_ID" ] && [ -f "$CACHE_DIR/${SESSION_ID}.timer-off" ]; then
  # Pass through to existing ccstatusline only
  if command -v ccstatusline >/dev/null 2>&1; then
    echo "$INPUT" | ccstatusline 2>/dev/null
  fi
  exit 0
fi

# Per-session timestamp file, fallback to shared file
TIMESTAMP_FILE=""
if [ -n "$SESSION_ID" ] && [ -f "$CACHE_DIR/$SESSION_ID" ]; then
  TIMESTAMP_FILE="$CACHE_DIR/$SESSION_ID"
elif [ -f "$HOME/.claude/.last-claude-response" ]; then
  TIMESTAMP_FILE="$HOME/.claude/.last-claude-response"
fi

# Check if Claude is actively working (prompt submitted but no Stop yet)
ACTIVE_FILE=""
if [ -n "$SESSION_ID" ] && [ -f "$CACHE_DIR/${SESSION_ID}.active" ]; then
  ACTIVE_FILE="$CACHE_DIR/${SESSION_ID}.active"
elif [ -f "$HOME/.claude/.cache-timer-active" ]; then
  ACTIVE_FILE="$HOME/.claude/.cache-timer-active"
fi

CLAUDE_ACTIVE=false
if [ -n "$ACTIVE_FILE" ] && [ -n "$TIMESTAMP_FILE" ]; then
  ACTIVE_TS=$(cat "$ACTIVE_FILE" 2>/dev/null)
  STOP_TS=$(cat "$TIMESTAMP_FILE" 2>/dev/null)
  if [ -n "$ACTIVE_TS" ] && [ -n "$STOP_TS" ] && [ "$ACTIVE_TS" -gt "$STOP_TS" ] 2>/dev/null; then
    CLAUDE_ACTIVE=true
  fi
elif [ -n "$ACTIVE_FILE" ] && [ -z "$TIMESTAMP_FILE" ]; then
  CLAUDE_ACTIVE=true
fi

# Pass through to existing ccstatusline
EXISTING=""
if command -v ccstatusline >/dev/null 2>&1; then
  EXISTING=$(echo "$INPUT" | ccstatusline 2>/dev/null)
fi

# Colors
GREEN="\033[38;5;78m"
AMBER="\033[38;5;179m"
DIM="\033[38;5;242m"
WARN="\033[38;5;209m"
DANGER="\033[38;5;196m"
RESET="\033[0m"

# Danger thresholds — big session + expired cache = expensive rebuild
DANGER_TOKEN_THRESHOLD=1000000

# Calculate cache status
CACHE_DISPLAY=""

if [ "$CLAUDE_ACTIVE" = true ]; then
  CACHE_DISPLAY="${GREEN}◉ cached${RESET}"

elif [ -n "$TIMESTAMP_FILE" ]; then
  LAST=$(cat "$TIMESTAMP_FILE" 2>/dev/null)
  if [ -n "$LAST" ] && [[ "$LAST" =~ ^[0-9]+$ ]]; then
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST))
    REMAINING=$((TTL_SECONDS - ELAPSED))

    if [ $REMAINING -gt 120 ]; then
      CACHE_DISPLAY="${GREEN}◉ cached${RESET}"

    elif [ $REMAINING -gt 0 ]; then
      MINS=$((REMAINING / 60))
      SECS=$((REMAINING % 60))
      CACHE_DISPLAY="${AMBER}◎ ${MINS}m${SECS}s${RESET}"

    else
      OVER=$((-REMAINING))

      if [ $OVER -gt 3600 ]; then
        HOURS=$((OVER / 3600))
        if [ "$CACHED_TOKENS" -gt "$DANGER_TOKEN_THRESHOLD" ] 2>/dev/null; then
          MTK=$(echo "scale=2; $CACHED_TOKENS / 1000000" | bc 2>/dev/null)
          COST=$(echo "scale=2; $MTK * 3.45" | bc 2>/dev/null)
          STALE_COST=""
          if [ -n "$COST" ] && [ "$COST" != "0" ]; then
            STALE_COST=" ~\$${COST} rebuild ·"
          fi
          CACHE_DISPLAY="${DANGER}☣ ${HOURS}h stale ·${STALE_COST} /clear or new session${RESET}"
        else
          CACHE_DISPLAY="${DIM}○ ${HOURS}h stale · /compact or new session${RESET}"
        fi

      else
        if [ $OVER -lt 60 ]; then
          AGO_STR="${OVER}s"
        else
          AGO_STR="$((OVER / 60))m"
        fi

        COST_NOTE=""
        if [ "$CACHED_TOKENS" -gt 500000 ] 2>/dev/null; then
          MTK=$(echo "scale=2; $CACHED_TOKENS / 1000000" | bc 2>/dev/null)
          COST=$(echo "scale=2; $MTK * 3.45" | bc 2>/dev/null)
          if [ -n "$COST" ] && [ "$COST" != "0" ]; then
            COST_NOTE=" · ~\$${COST} rebuild"
          fi
        fi

        if [ "$CACHED_TOKENS" -gt "$DANGER_TOKEN_THRESHOLD" ] 2>/dev/null && [ -n "$COST_NOTE" ]; then
          CACHE_DISPLAY="${DANGER}☣ expired ${AGO_STR}${COST_NOTE}${RESET}"
        elif [ -n "$COST_NOTE" ]; then
          CACHE_DISPLAY="${WARN}○ expired ${AGO_STR}${COST_NOTE}${RESET}"
        else
          CACHE_DISPLAY="${DIM}○ expired ${AGO_STR}${RESET}"
        fi
      fi
    fi
  fi
else
  CACHE_DISPLAY="${DIM}○ no cache${RESET}"
fi

# Combine: cache state on left, existing statusline follows
if [ -n "$EXISTING" ]; then
  printf "%b  %s" "$CACHE_DISPLAY" "$EXISTING"
else
  printf "%b" "$CACHE_DISPLAY"
fi
