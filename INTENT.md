# Claude Cache Timer — Intent

## Goal

Help Claude Code users understand and control their API costs. The core insight: **most people don't know what drives their spend**, and the answer is usually not what they'd guess (it's cache behavior, not output tokens). We want to surface this in a way that's actionable for people who don't understand prompt caching mechanics.

Primary audience: Matt's team at BambooHR — developers using Claude Code daily who see cost numbers but don't know why some sessions cost $200 and others cost $18.

## Current Direction

Two-tier approach:

### Tier 1: Power-user analysis tools (built, archived)
- `cache_ttl_analyzer.py` — moved to `archive/`. CLI tool for deep-dive cost breakdowns, TTL comparisons, cache miss analysis, interactive charts.
- These were the analytical foundation that informed the team-facing tool's design.

### Tier 2: Team-facing insights (built, iterating)
`claude_usage_advisor.py --team` generates a self-contained HTML report with 5 insight cards:
1. **Cache Miss Tax** — % of turns with mid-session cache misses vs % of cost they represent
2. **Idle Gap Penalty** — breaks of 5-60 min and >1h with cost impact and specific fix advice
3. **Session Size Impact** — cost per turn by context size bucket (<100K, 100-200K, >200K)
4. **Recommendation** — top priority setting change with copy-paste config and monthly savings estimate
5. **What's Not Inflating Your Costs** — MCP overhead and output token share (things people worry about that don't matter)

Key design principle: **speak in actions and consequences, not mechanisms.** Nobody needs to know what `cache_creation_input_tokens` means. They need to know "a 10-minute break at turn 500 costs $10."

### `/usage-report` skill
Slash command that generates a text-mode summary with 30-day default window. Available as a Claude Code skill.

### Statusline & hooks
- `scripts/statusline.sh` — real-time cache TTL countdown in the Claude Code status bar
- `scripts/cache-expiry-warn.sh` — warns before sending a prompt if cache expired (>5 min idle)

## What's Done

- Full usage advisor (`claude_usage_advisor.py`) with `--team`, `--html`, `--json`, `--since` modes
- Team report with 5 insight cards, conditional TTL recommendations, MCP overhead analysis
- **Date range presets** — team report now has a tab switcher: Last 7 days, 14 days, 30 days (default), 90 days, All time. Each preset pre-computes its own analysis independently. Refactored `generate_team_html` into `_build_range_content` (per-range cards) + `generate_team_html` (tabbed HTML shell).
- Opus 4.7 → 4.6 tokenizer inflation modeled correctly (same $/MTok, ~30-35% more tokens)
- Recommendation engine: prioritized settings changes based on usage patterns
- Top 3 cost drivers with visual breakdown
- Archived old power-user tools (`cache_ttl_analyzer.py`, `cost_curves.html`, `cost_breakdown.html`) to `archive/`
- `/usage-report` slash command with 30-day default
- Statusline timer and cache-expiry warning hook

## Rejected Approaches

- Using `cache_read_input_tokens` alone as "context size" — drops to near-zero during compaction, creating misleading bounce-backs. Replaced with full prompt size.
- Per-token price difference for Opus versions — the actual difference is tokenizer efficiency, not pricing. Same $/MTok, different token counts for same content.
- Recommending Sonnet over Opus — team uses Opus intentionally. Recommendations focus on Opus 4.7 → 4.6 and settings changes.

## Open Questions

- Should we detect and call out subagent spawning as a cost driver? (Agent tool launches are visible in the JSONL)
- Is there a way to estimate "optimal session length" from the data — the point where context growth makes it cheaper to /clear and start fresh?
- Custom date range picker in the team report (currently preset-only, custom requires re-running with `--since`)

## Next Steps

1. Polish team report UX based on feedback
2. Consider per-session scorecard view (drill into individual sessions)
3. Explore automated scheduling (daily/weekly report generation)
