# Claude Cache Timer / Usage Advisor

## Pricing Model

Per-token prices are the same for all Opus versions. The effective cost difference comes from the tokenizer:
- **Opus 4.7**: same $/MTok as 4.6, but its tokenizer produces **~30-35% more tokens** for the same content → effective cost **~2x Sonnet**
- **Opus 4.6**: baseline Opus pricing → effective cost **~1.67x Sonnet**
- **Sonnet**: baseline

## Recommendation Rules

- **Never recommend switching from Opus to Sonnet.** The team uses Opus intentionally.
- **Do recommend switching from Opus 4.7 to Opus 4.6** when it would meaningfully reduce costs (same model family, smaller context window forces earlier compaction, ~17% cheaper).
- Recommendations should focus on parameter/settings changes, not workflow changes.
- The goal: keep working hard, spend time creating, don't worry about token-saving strategies.
