---
name: cache-timer
description: Toggle the cache timer status line on/off for this session
---

<command-name>cache-timer</command-name>

Toggle the cache TTL status line visibility. The timer is **on by default** after install. Use this to hide it for a session when you don't need it.

## Usage
- `/cache-timer` — toggle off/on for this session
- `/cache-timer off` — turn off for this session
- `/cache-timer on` — turn back on for this session

Run the appropriate bash command based on the argument:

**Parse the argument** from the user's message. If they just typed `/cache-timer` with no argument, treat it as a toggle.

You need to determine the session ID. Read it from the most recent file in `~/.claude/.cache-timer/` that doesn't end in `.active`, `.timer-off`, or `.timer-on`:
```bash
SID=$(ls -t "$HOME/.claude/.cache-timer/" 2>/dev/null | grep -v -E '\.(active|timer-off|timer-on)$' | head -1)
```

Then based on the argument:

**Toggle (no arg):**
```bash
if [ -f "$HOME/.claude/.cache-timer/${SID}.timer-off" ]; then
  rm "$HOME/.claude/.cache-timer/${SID}.timer-off"
  echo "ON"
else
  touch "$HOME/.claude/.cache-timer/${SID}.timer-off"
  echo "OFF"
fi
```

**off:** `touch "$HOME/.claude/.cache-timer/${SID}.timer-off"` → report OFF
**on:** `rm -f "$HOME/.claude/.cache-timer/${SID}.timer-off"` → report ON

## Response format

Keep it to one line:
- ON: "Cache timer **on**. States: `◉ cached` → `◎ 1m30s` → `○ expired` → `☣ expired` (large sessions)"
- OFF: "Cache timer **off** for this session. Run `/cache-timer` to re-enable."
