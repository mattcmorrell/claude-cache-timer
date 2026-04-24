# Claude Cache Timer / Usage Advisor

## Pricing Model

Relative cost tiers (use these for all advisor calculations and recommendations):
- **Opus 4.7**: ~2x Sonnet cost
- **Opus 4.6**: ~1.67x Sonnet cost
- **Sonnet**: baseline

## Recommendation Rules

- **Never recommend switching from Opus to Sonnet.** The team uses Opus intentionally.
- **Do recommend switching from Opus 4.7 to Opus 4.6** when it would meaningfully reduce costs (same model family, smaller context window forces earlier compaction, ~17% cheaper).
- Recommendations should focus on parameter/settings changes, not workflow changes.
- The goal: keep working hard, spend time creating, don't worry about token-saving strategies.
