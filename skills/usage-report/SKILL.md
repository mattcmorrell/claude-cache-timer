---
name: usage-report
description: Analyze your Claude Code usage and show cost-saving recommendations
disable-model-invocation: true
---

<command-name>usage-report</command-name>

Here is the raw usage analysis from the past 30 days:

!`python3 ~/.claude/plugins/cache-timer/claude_usage_advisor.py --since $(date -v-30d +%Y-%m-%d 2>/dev/null || date -d '30 days ago' +%Y-%m-%d) 2>/dev/null`

Summarize the report for the user in a few short paragraphs:
1. Their total spend and what's driving it (top cost drivers)
2. Any recommended setting changes — explain what each setting does and how to change it
3. Whether their work rhythm is cache-friendly or if breaks are costing them

Keep it conversational and jargon-free. If there's a recommended setting change, give them the exact steps to apply it.
