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

Requires `jq` (`brew install jq` on macOS).

```bash
git clone https://github.com/mattcmorrell/claude-cache-timer.git
cd claude-cache-timer
bash install.sh
```

Restart Claude Code. You'll see the cache indicator in your status line immediately.

The installer sets up:
- **Status line** — live cache TTL display at the bottom of your terminal
- **Stop hook** — records a timestamp each time Claude finishes responding
- **UserPromptSubmit hook** — marks session as active + warns if cache expired before sending

It backs up your `settings.json` before making any changes.

### Plugin marketplace (optional)

If you'd also like the `/cache` slash command for on-demand status checks, add the plugin from inside Claude Code:

```
/plugin marketplace add mattcmorrell/claude-cache-timer
/plugin install cache-timer
```

## Uninstall

```bash
cd claude-cache-timer
bash uninstall.sh
```

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
