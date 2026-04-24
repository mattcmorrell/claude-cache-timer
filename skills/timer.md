---
name: timer
description: Toggle the cache timer status line on/off for this session, or set it to always-on
---

<command-name>timer</command-name>

Toggle the cache TTL status line visibility. Controls whether the cache timer appears in the status bar.

## Usage
- `/timer` — toggle on for this session (or off if already on)
- `/timer on` — turn on for this session
- `/timer off` — turn off for this session
- `/timer always` — keep on for all sessions
- `/timer default` — revert to off-by-default (removes always-on)

Run the appropriate bash command based on the argument:

**Parse the argument** from the user's message. If they just typed `/timer` with no argument, treat it as a toggle.

```bash
CACHE_DIR="$HOME/.claude/.cache-timer"
mkdir -p "$CACHE_DIR"
# Get session ID from the environment or recent state
SID="__SESSION_ID__"
```

You need to determine the session ID. Read it from the most recent file in `~/.claude/.cache-timer/` that doesn't end in `.active` or `.timer-on` and isn't named `always`:
```bash
SID=$(ls -t "$HOME/.claude/.cache-timer/" 2>/dev/null | grep -v -E '\.(active|timer-on)$|^always$' | head -1)
```

Then based on the argument:

**Toggle (no arg):**
```bash
if [ -f "$HOME/.claude/.cache-timer/${SID}.timer-on" ]; then
  rm "$HOME/.claude/.cache-timer/${SID}.timer-on"
  echo "OFF"
else
  touch "$HOME/.claude/.cache-timer/${SID}.timer-on"
  echo "ON"
fi
```

**on:** `touch "$HOME/.claude/.cache-timer/${SID}.timer-on"` → report ON
**off:** `rm -f "$HOME/.claude/.cache-timer/${SID}.timer-on"` → report OFF
**always:** `touch "$HOME/.claude/.cache-timer/always"` → report ALWAYS ON
**default:** `rm -f "$HOME/.claude/.cache-timer/always"` → report back to default (off unless toggled per-session)

## Response format

Keep it to one line:
- ON: "Cache timer **on** for this session. States: `◉ cached` → `◎ 1m30s` → `○ expired` → `☢ expired` (large sessions)"
- OFF: "Cache timer **off**."
- ALWAYS ON: "Cache timer **always on** across all sessions."
- DEFAULT: "Cache timer back to **off by default**. Use `/timer` to enable per-session."
