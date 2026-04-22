---
name: cache
description: Check prompt cache TTL status — how long until the 5-minute cache expires
---

<command-name>cache</command-name>

Check the cache TTL status by reading `~/.claude/.last-claude-response` and calculating time remaining.

Run this bash command and report the result to the user:
```bash
TIMESTAMP_FILE="$HOME/.claude/.last-claude-response"
if [ -f "$TIMESTAMP_FILE" ]; then
  LAST=$(cat "$TIMESTAMP_FILE")
  NOW=$(date +%s)
  ELAPSED=$((NOW - LAST))
  REMAINING=$((300 - ELAPSED))
  echo "LAST_RESPONSE=$LAST NOW=$NOW ELAPSED=${ELAPSED}s REMAINING=${REMAINING}s"
else
  echo "NO_TIMESTAMP"
fi
```

Format the response:
- If REMAINING > 0: "Cache is **warm** — {minutes}m {seconds}s remaining. Prompt cache pricing applies."
- If REMAINING <= 0: "Cache **expired** {elapsed} ago. Next turn will rebuild context at write pricing. Consider running `/compact` first to reduce cost."
- If NO_TIMESTAMP: "No response timestamp found — cache status unknown."

Keep the response to 1-2 lines. Don't add extra explanation unless the user asks.
