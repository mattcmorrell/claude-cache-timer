# Claude Cache Timer — Intent

## Goal

Help Claude Code users understand and control their API costs. The core insight: **most people don't know what drives their spend**, and the answer is usually not what they'd guess (it's cache behavior, not output tokens). We want to surface this in a way that's actionable for people who don't understand prompt caching mechanics.

Primary audience: Matt's team at BambooHR — developers using Claude Code daily who see cost numbers but don't know why some sessions cost $200 and others cost $18.

## Current Direction

Two-tier approach:

### Tier 1: Power-user analysis tools (built)
- `cache_ttl_analyzer.py` — CLI tool that parses session JSONL logs and computes cost breakdowns, TTL comparisons, cache miss analysis
- `cost_curves.html` — interactive charts: cumulative cost, per-turn cost, context size over turns with compaction/clear markers
- `cost_breakdown.html` — where-the-money-goes dashboard: category donuts, per-session stacked bars, cache miss impact

These are useful for someone who already understands caching. They're the analytical foundation for the team-facing tool.

### Tier 2: Team-facing insights (next)
A report that non-experts can read and act on. Two concepts under consideration:

**Option A — Summary dashboard (preferred starting point)**
Single-page report a developer runs against their own `~/.claude/projects/`. Shows 3-4 cards with their biggest cost levers in plain English:
- "Idle gap penalty — you stepped away for 12 min and it cost $8 to reload context"
- "Long session tax — your context hit 130k tokens, making every turn 3x more expensive"
- "Your sessions are efficient, no action needed"
Ends with a single prioritized recommendation.

**Option B — Per-session scorecard**
Post-session receipt: "This session cost $43. $31 was normal work. $12 was from 3 cache reloads. Context hit 130k before compacting twice." Could be triggered automatically or run on-demand.

Key design principle: **speak in actions and consequences, not mechanisms.** Nobody needs to know what `cache_creation_input_tokens` means. They need to know "a 10-minute break at turn 500 costs $10."

## What's Done

- Cache TTL analyzer with `--csv`, `--curves`, `--top`, `--since` flags
- `--curves` outputs per-turn JSON with cumulative cost, context_size (full prompt tokens), clear_turns
- Context size chart uses actual prompt size (input + cache_read + cache_creation) instead of just cache reads
- Compaction events detected and marked (triangle + dashed line)
- `/clear` events detected from JSONL and marked differently (blue square + dotted line)
- Zero-token turns (no API call) carried forward instead of dropping to 0
- Cost breakdown page with category donuts, miss vs normal comparison, stacked bars
- Key stats surfaced: cache miss turns are ~2% of turns but ~20-27% of cost; avg miss turn costs ~14x a normal turn; 68% of spend is cache reads (the baseline cost of context growth)
- Disabled claude-mem plugin (wasn't being used, ~1k tokens/turn overhead)

## Rejected Approaches

- Using `cache_read_input_tokens` alone as "context size" — it drops to near-zero during compaction (tokens shift to cache writes) and on no-API-call turns, creating misleading instant bounce-backs. Replaced with full prompt size.

## Open Questions

- Should the team-facing report be a standalone HTML file or integrate into the existing CLI output?
- How to handle multi-model sessions (Opus + Sonnet) in the plain-English framing — do we just show dollar impact or also flag model choice as a lever?
- Should we detect and call out subagent spawning as a cost driver? (Agent tool launches are visible in the JSONL)
- Is there a way to estimate "optimal session length" from the data — the point where context growth makes it cheaper to /clear and start fresh?

## Next Steps

1. Build the team-facing summary dashboard (Option A)
2. Define the 4-5 "cost driver" categories in plain English with detection logic
3. Add recommendation engine: given someone's usage pattern, what's the single highest-ROI behavior change?
