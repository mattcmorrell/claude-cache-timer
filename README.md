# claude-cache-timer

A Claude Code plugin that shows the prompt cache TTL status in your status line. Know at a glance whether your 5-minute cache is warm, expiring, or gone.

## Why this matters

Claude Code caches your conversation context for 5 minutes after each response. While cached, input tokens cost ~$0.30/MTok instead of ~$3.75/MTok (Opus). On a large session, a cache miss can cost **$3-5+ just to rebuild context**. This plugin shows you the cache state so you can time your prompts or `/compact` before an expensive rebuild.

## States

```
◉ cached      green   — cache is warm, keep going
◎ 1m30s       amber   — expiring soon, 2 minutes or less remaining
○ expired 5m  dim/orange — cache gone. Large sessions show rebuild cost estimate
○ 2h stale    dim     — idle a long time, suggests /compact or new session
```

The timer pauses during active Claude operations (tool use, compacting, multi-turn writing) since the cache stays warm while Claude is working.

## Install

### Step 1: Add the plugin

From inside Claude Code:

```
/plugin marketplace add mattcmorrell/claude-cache-timer
/plugin install cache-timer
```

This gives you:
- **Stop hook** — records a timestamp each time Claude finishes responding
- **UserPromptSubmit hook** — marks session as active + warns if cache expired before sending
- **`/cache` skill** — on-demand cache status check

### Step 2: Enable the status line (optional but recommended)

The status line shows cache state persistently at the bottom of your terminal. Plugins can't configure this automatically, so run the installer:

```bash
git clone https://github.com/mattcmorrell/claude-cache-timer.git
cd claude-cache-timer
bash install.sh
```

Requires `jq` (`brew install jq` on macOS).

Then restart Claude Code.

## Uninstall

```bash
cd claude-cache-timer
bash uninstall.sh
```

And from Claude Code: `/plugin uninstall cache-timer`

## How it works

1. The **Stop hook** writes a Unix timestamp to `~/.claude/.cache-timer/{session_id}` each time Claude responds
2. The **UserPromptSubmit hook** writes an `.active` marker so the timer knows Claude is working
3. The **statusline script** runs every second, reads the timestamp, and calculates cache TTL remaining
4. Each Claude Code session tracks its own timer independently — no cross-session interference

## Cost display

When the cache expires on a session with >500k cached tokens, the status line shows the estimated rebuild cost:

```
○ expired 5m · ~$1.73 rebuild
```

This is the delta between cache-write and cache-read pricing (~$3.45/MTok for Opus).
