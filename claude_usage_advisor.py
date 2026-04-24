#!/usr/bin/env python3
"""
Claude Code Usage Advisor
=========================
Analyzes your Claude Code usage and recommends settings changes to reduce
costs — without changing how you work. Just parameter tuning.

Usage:
    python3 claude_usage_advisor.py                      # full text report
    python3 claude_usage_advisor.py --since 2026-04-01   # date filter
    python3 claude_usage_advisor.py --json                # machine-readable
    python3 claude_usage_advisor.py --html > report.html  # shareable report

Requirements: Python 3.7+, no external dependencies.
Data source:  ~/.claude/projects/ (Claude Code session JSONL logs)
"""

import json
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# ── Pricing ($/MTok, April 2026) ────────────────────────────────────────────

PRICING = {
    "opus": {
        "base_input": 5.00,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
        "output": 25.00,
    },
    "sonnet": {
        "base_input": 3.00,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
        "output": 15.00,
    },
    "haiku": {
        "base_input": 1.00,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.00,
        "cache_read": 0.10,
        "output": 5.00,
    },
}

MODEL_KEYWORDS = {
    "opus": ["opus"],
    "sonnet": ["sonnet"],
    "haiku": ["haiku"],
}

DEFAULT_TIER = "sonnet"


def model_tier(model_str):
    if not model_str:
        return DEFAULT_TIER
    m = model_str.lower()
    for tier, keywords in MODEL_KEYWORDS.items():
        if any(k in m for k in keywords):
            return tier
    return DEFAULT_TIER


def rates_for(model_str):
    return PRICING[model_tier(model_str)]


# ── Parsing ──────────────────────────────────────────────────────────────────

def find_session_files(path):
    p = Path(path)
    if p.is_file() and p.suffix == ".jsonl":
        return [str(p)]
    if p.is_dir():
        return [str(f) for f in p.rglob("*.jsonl") if f.stem != "history"]
    return []


def parse_session(filepath):
    session_id = Path(filepath).stem
    turns = []
    models_seen = set()
    first_ts = last_ts = None

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message", {})
            usage = msg.get("usage", {})
            if not usage:
                continue

            ts = None
            ts_str = entry.get("timestamp")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                except (ValueError, TypeError):
                    pass

            model = msg.get("model", "")
            models_seen.add(model)
            cc = usage.get("cache_creation", {})
            turns.append({
                "model": model,
                "timestamp": ts,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_write_5m": cc.get("ephemeral_5m_input_tokens", 0),
                "cache_write_1h": cc.get("ephemeral_1h_input_tokens", 0),
            })

    if not turns:
        return None
    duration = (last_ts - first_ts).total_seconds() / 60 if first_ts and last_ts else 0
    return {
        "session_id": session_id,
        "filepath": filepath,
        "turns": turns,
        "models": models_seen,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_min": duration,
    }


# ── Cost computation ─────────────────────────────────────────────────────────

def compute_session_costs(session):
    mtok = 1_000_000
    totals = defaultdict(float)
    totals["turns"] = len(session["turns"])
    turn_costs = []
    by_tier = defaultdict(lambda: defaultdict(float))

    for i, t in enumerate(session["turns"]):
        r = rates_for(t["model"])
        tier = model_tier(t["model"])
        out_c = (t["output_tokens"] / mtok) * r["output"]
        read_c = (t["cache_read_tokens"] / mtok) * r["cache_read"]
        w5m_c = (t["cache_write_5m"] / mtok) * r["cache_write_5m"]
        w1h_c = (t["cache_write_1h"] / mtok) * r["cache_write_1h"]
        inp_c = (t["input_tokens"] / mtok) * r["base_input"]
        actual = out_c + read_c + w5m_c + w1h_c + inp_c

        # counterfactual: all writes at 5m
        cf_5m = out_c + read_c + (t["cache_write_tokens"] / mtok) * r["cache_write_5m"] + inp_c
        # counterfactual: all writes at 1h
        cf_1h = out_c + read_c + (t["cache_write_tokens"] / mtok) * r["cache_write_1h"] + inp_c

        # A "rebuild" = cache expired and had to be fully rewritten.
        # Normal turns write a small amount (new content). Rebuilds write most of the context.
        total_cache = t["cache_read_tokens"] + t["cache_write_tokens"]
        is_rebuild = (
            t["cache_write_tokens"] > 0
            and (t["cache_read_tokens"] < t["cache_write_tokens"] * 0.2)
            and total_cache > 5000
        )

        turn_costs.append({
            "actual": actual, "cf_5m": cf_5m, "cf_1h": cf_1h,
            "output_cost": out_c, "cache_read_cost": read_c,
            "cache_write_cost": w5m_c + w1h_c, "uncached_input_cost": inp_c,
            "is_rebuild": is_rebuild,
            "write_tokens": t["cache_write_tokens"],
            "read_tokens": t["cache_read_tokens"],
            "timestamp": t["timestamp"],
        })

        totals["actual"] += actual
        totals["cf_5m"] += cf_5m
        totals["cf_1h"] += cf_1h
        totals["output_cost"] += out_c
        totals["cache_read_cost"] += read_c
        totals["cache_write_cost"] += w5m_c + w1h_c
        totals["uncached_input_cost"] += inp_c
        totals["output_tokens"] += t["output_tokens"]
        totals["input_tokens"] += t["input_tokens"]
        totals["cache_read_tokens"] += t["cache_read_tokens"]
        totals["cache_write_tokens"] += t["cache_write_tokens"]
        totals["cache_write_5m_tokens"] += t["cache_write_5m"]
        totals["cache_write_1h_tokens"] += t["cache_write_1h"]

        by_tier[tier]["cost"] += actual
        by_tier[tier]["output_cost"] += out_c
        by_tier[tier]["turns"] += 1

        # If we priced this turn at sonnet instead of opus
        if tier == "opus":
            sr = PRICING["sonnet"]
            sonnet_cost = (
                (t["output_tokens"] / mtok) * sr["output"]
                + (t["cache_read_tokens"] / mtok) * sr["cache_read"]
                + (t["cache_write_5m"] / mtok) * sr["cache_write_5m"]
                + (t["cache_write_1h"] / mtok) * sr["cache_write_1h"]
                + (t["input_tokens"] / mtok) * sr["base_input"]
            )
            by_tier["opus"]["sonnet_counterfactual"] = by_tier["opus"].get("sonnet_counterfactual", 0) + sonnet_cost

    # Gap analysis
    gaps_5_60 = 0
    gaps_over_1h = 0
    avoidable_indices = []
    for i in range(1, len(turn_costs)):
        t1, t0 = turn_costs[i]["timestamp"], turn_costs[i - 1]["timestamp"]
        if t1 and t0:
            gap_s = (t1 - t0).total_seconds()
            if gap_s > 300:
                if gap_s <= 3600:
                    gaps_5_60 += 1
                    avoidable_indices.append(i)
                else:
                    gaps_over_1h += 1

    # counterfactual 1h with avoided misses
    cf_1h_avoided = 0.0
    for i, t in enumerate(session["turns"]):
        r = rates_for(t["model"])
        out_c = (t["output_tokens"] / mtok) * r["output"]
        inp_c = (t["input_tokens"] / mtok) * r["base_input"]
        if i in avoidable_indices and t["cache_write_tokens"] > 0:
            read_c = ((t["cache_read_tokens"] + t["cache_write_tokens"]) / mtok) * r["cache_read"]
            write_c = 0
        else:
            read_c = (t["cache_read_tokens"] / mtok) * r["cache_read"]
            write_c = (t["cache_write_tokens"] / mtok) * r["cache_write_1h"]
        cf_1h_avoided += out_c + inp_c + read_c + write_c

    rebuilds = sum(1 for tc in turn_costs if tc["is_rebuild"])
    cold_starts = 1 if turn_costs and turn_costs[0]["write_tokens"] > 0 else 0
    mid_session_rebuilds = max(0, rebuilds - cold_starts)
    ttl_5m = totals["cache_write_5m_tokens"]
    ttl_1h = totals["cache_write_1h_tokens"]

    # Max context per turn (proxy for session bloat)
    max_context = 0
    for t in session["turns"]:
        ctx = t["input_tokens"] + t["cache_read_tokens"] + t["cache_write_tokens"]
        if ctx > max_context:
            max_context = ctx

    return {
        **dict(totals),
        "by_tier": dict(by_tier),
        "turn_costs": turn_costs,
        "rebuilds": rebuilds,
        "mid_session_rebuilds": mid_session_rebuilds,
        "gaps_5_60": gaps_5_60,
        "gaps_over_1h": gaps_over_1h,
        "avoidable_misses": len(avoidable_indices),
        "cf_1h_avoided": cf_1h_avoided,
        "ttl_used": "1h" if ttl_1h > ttl_5m else "5m",
        "max_context_tokens": max_context,
    }


# ── Analysis ─────────────────────────────────────────────────────────────────

def run_analysis(sessions, costs_list):
    total_spend = sum(c["actual"] for c in costs_list)
    if total_spend == 0:
        total_spend = 0.001  # avoid division by zero

    # Period
    all_ts = [s["first_ts"] for s in sessions if s["first_ts"]]
    period_start = min(all_ts) if all_ts else None
    period_end = max([s["last_ts"] for s in sessions if s["last_ts"]] or [None])
    days = max(1, (period_end - period_start).days + 1) if period_start and period_end else 1

    # Spend by category
    cat_output = sum(c["output_cost"] for c in costs_list)
    cat_cache_write = sum(c["cache_write_cost"] for c in costs_list)
    cat_cache_read = sum(c["cache_read_cost"] for c in costs_list)
    cat_uncached = sum(c["uncached_input_cost"] for c in costs_list)

    # Spend by model tier
    tier_costs = defaultdict(float)
    tier_output = defaultdict(float)
    tier_turns = defaultdict(int)
    sonnet_cf_total = 0.0
    for c in costs_list:
        for tier, data in c["by_tier"].items():
            tier_costs[tier] += data.get("cost", 0)
            tier_output[tier] += data.get("output_cost", 0)
            tier_turns[tier] += int(data.get("turns", 0))
            sonnet_cf_total += data.get("sonnet_counterfactual", 0)

    # Cache analysis
    total_rebuilds = sum(c["rebuilds"] for c in costs_list)
    total_mid_rebuilds = sum(c["mid_session_rebuilds"] for c in costs_list)
    total_read_tok = sum(c["cache_read_tokens"] for c in costs_list)
    total_write_tok = sum(c["cache_write_tokens"] for c in costs_list)
    cache_hit_rate = (total_read_tok / (total_read_tok + total_write_tok) * 100) if (total_read_tok + total_write_tok) > 0 else 0
    total_gaps_5_60 = sum(c["gaps_5_60"] for c in costs_list)
    total_gaps_1h = sum(c["gaps_over_1h"] for c in costs_list)
    total_avoidable = sum(c["avoidable_misses"] for c in costs_list)
    ttl_5m_tokens = sum(c["cache_write_5m_tokens"] for c in costs_list)
    ttl_1h_tokens = sum(c["cache_write_1h_tokens"] for c in costs_list)
    current_ttl = "1h" if ttl_1h_tokens > ttl_5m_tokens else "5m"

    # TTL counterfactual savings
    total_cf_1h_avoided = sum(c["cf_1h_avoided"] for c in costs_list)
    ttl_savings = total_spend - total_cf_1h_avoided

    # Session ranking
    ranked = sorted(
        zip(sessions, costs_list),
        key=lambda sc: sc[1]["actual"],
        reverse=True,
    )
    top_sessions = []
    for s, c in ranked[:10]:
        primary_model = max(c["by_tier"], key=lambda t: c["by_tier"][t].get("cost", 0)) if c["by_tier"] else "unknown"
        top_sessions.append({
            "id": s["session_id"][:16],
            "date": s["first_ts"].strftime("%b %d") if s["first_ts"] else "?",
            "cost": c["actual"],
            "turns": int(c["turns"]),
            "duration_min": s["duration_min"],
            "model": primary_model,
            "max_context": c["max_context_tokens"],
        })

    # Short Opus sessions (candidates for Sonnet)
    short_opus = []
    short_opus_cost = 0.0
    short_opus_sonnet_cost = 0.0
    for s, c in zip(sessions, costs_list):
        opus_data = c["by_tier"].get("opus", {})
        if opus_data.get("turns", 0) == 0:
            continue
        is_primarily_opus = opus_data.get("cost", 0) > c["actual"] * 0.5
        is_short = c["turns"] < 25 and s["duration_min"] < 30
        if is_primarily_opus and is_short:
            short_opus.append(s["session_id"])
            short_opus_cost += opus_data.get("cost", 0)
            short_opus_sonnet_cost += opus_data.get("sonnet_counterfactual", 0)

    sonnet_switch_savings = short_opus_cost - short_opus_sonnet_cost
    all_opus_cost = tier_costs.get("opus", 0)
    all_opus_sonnet_cf = sonnet_cf_total

    # Context size analysis
    max_contexts = [c["max_context_tokens"] for c in costs_list]
    sessions_over_500k = sum(1 for ctx in max_contexts if ctx > 500_000)
    sessions_over_200k = sum(1 for ctx in max_contexts if ctx > 200_000)
    avg_max_context = sum(max_contexts) / len(max_contexts) if max_contexts else 0

    # ── Build recommendations ────────────────────────────────────────────
    recommendations = []

    # Rec 1: Cache TTL
    monthly_ttl_savings = (ttl_savings / days) * 30 if ttl_savings > 1 and current_ttl == "5m" else 0
    if total_avoidable > 0 and ttl_savings > 0 and current_ttl == "5m":
        recommendations.append({
            "id": "cache_ttl_1h",
            "title": "Switch to 1-hour cache TTL",
            "savings_monthly": monthly_ttl_savings,
            "detail": (
                f"You have {total_avoidable} idle gaps between 5-60 minutes that force "
                f"expensive cache rebuilds. The 1h TTL costs 60% more per cache write "
                f"but prevents these rebuilds entirely — a net win of ${ttl_savings:.2f} "
                f"over this period."
            ),
            "setting": (
                'Add to ~/.claude/settings.json:\n'
                '  "env": { "CLAUDE_CODE_USE_EXTENDED_CACHE_TTL": "true" }'
            ),
        })
    elif current_ttl == "1h" and total_avoidable == 0:
        # Check if switching to 5m would save
        cf_5m_total = sum(c["cf_5m"] for c in costs_list)
        savings_5m = total_spend - cf_5m_total
        if savings_5m > 1:
            monthly_5m = (savings_5m / days) * 30
            recommendations.append({
                "id": "cache_ttl_5m",
                "title": "Switch to 5-minute cache TTL",
                "savings_monthly": monthly_5m,
                "detail": (
                    f"You're on 1h TTL but rarely have 5-60 min idle gaps. "
                    f"The 5m TTL's cheaper write rate (1.25x vs 2x) would save "
                    f"${savings_5m:.2f} over this period."
                ),
                "setting": (
                    'Remove extended TTL from ~/.claude/settings.json env,\n'
                    'or set: "CLAUDE_CODE_USE_EXTENDED_CACHE_TTL": "false"'
                ),
            })

    # Rec 2/3: Model tier — show EITHER "default to Sonnet" (bigger opportunity)
    # or "Sonnet for quick tasks" (conservative), not both.
    full_savings = all_opus_cost - all_opus_sonnet_cf if all_opus_sonnet_cf > 0 else 0
    opus_pct = all_opus_cost / total_spend * 100 if total_spend > 0 else 0

    if all_opus_cost > 50 and full_savings > 20:
        monthly_full = (full_savings / days) * 30
        recommendations.append({
            "id": "model_sonnet_default",
            "title": "Make Sonnet your default model",
            "savings_monthly": monthly_full,
            "detail": (
                f"Opus is {opus_pct:.0f}% of your spend (${all_opus_cost:.2f}). "
                f"At Sonnet rates the same volume would be ${all_opus_sonnet_cf:.2f} "
                f"— a ${full_savings:.2f} difference over {days} days. "
                f"Sonnet 4.6 handles most coding tasks well. Use Opus only when you "
                f"need it: complex architecture, subtle multi-file refactors, or "
                f"tricky debugging. Start conservative — switch your quick sessions "
                f"first ({len(short_opus)} of yours were under 25 turns), then expand."
            ),
            "setting": (
                'Set default model globally:\n'
                '  claude config set model sonnet\n'
                'Upgrade for complex work:\n'
                '  /model opus  (within a session)\n'
                '  claude --model opus  (at start)'
            ),
        })
    elif len(short_opus) >= 3 and sonnet_switch_savings > 2:
        monthly_model = (sonnet_switch_savings / days) * 30
        recommendations.append({
            "id": "model_sonnet_short",
            "title": "Use Sonnet for quick tasks",
            "savings_monthly": monthly_model,
            "detail": (
                f"{len(short_opus)} of your sessions were short (<25 turns, <30 min) "
                f"but ran on Opus. If these were routine tasks (quick questions, small "
                f"edits, lookups), Sonnet handles them well at 40% lower cost — "
                f"saving ${sonnet_switch_savings:.2f} over this period."
            ),
            "setting": (
                'For quick tasks, start with:\n'
                '  claude --model sonnet\n'
                'Or set a per-project default in .claude/settings.json:\n'
                '  "model": "sonnet"'
            ),
        })

    # Rec 4: Session bloat warning
    if sessions_over_500k >= 3:
        recommendations.append({
            "id": "context_bloat",
            "title": "Enable auto-compact for large sessions",
            "savings_monthly": None,
            "detail": (
                f"{sessions_over_500k} sessions exceeded 500K context tokens. "
                f"Large contexts mean every turn processes more tokens, driving up "
                f"costs. Auto-compacting earlier keeps per-turn costs lower."
            ),
            "setting": (
                'Claude Code auto-compacts when context is nearly full.\n'
                'For earlier compaction, use /compact proactively when\n'
                'context feels large, or start a new session for new topics.'
            ),
        })

    # Sort recommendations by savings (None last)
    recommendations.sort(key=lambda r: r["savings_monthly"] or 0, reverse=True)
    total_potential = sum(r["savings_monthly"] for r in recommendations if r["savings_monthly"])

    # Top cost driver with contextual insight
    categories = [
        ("Output tokens", cat_output),
        ("Cache writes", cat_cache_write),
        ("Cache reads", cat_cache_read),
        ("Uncached input", cat_uncached),
    ]
    categories.sort(key=lambda x: x[1], reverse=True)
    top_driver = categories[0][0] if categories else "Unknown"

    # Generate actionable insight based on dominant cost
    if top_driver == "Cache reads":
        cost_insight = (
            f"Cache reads are your top cost ({categories[0][1]/total_spend*100:.0f}%) — "
            f"not because they're expensive per token (they're the cheapest at $0.50/MTok "
            f"on Opus) but because your sessions accumulate large contexts. Every turn "
            f"re-reads the full context. The biggest levers: use a cheaper model (Sonnet "
            f"reads at $0.30/MTok) or compact earlier to keep contexts smaller."
        )
    elif top_driver == "Cache writes":
        cost_insight = (
            f"Cache writes are your top cost ({categories[0][1]/total_spend*100:.0f}%). "
            f"This means you're frequently rebuilding the cache — either from cache expiry "
            f"(idle gaps), many short sessions, or tool-heavy workflows. Check your cache "
            f"TTL setting and idle patterns above."
        )
    elif top_driver == "Output tokens":
        cost_insight = (
            f"Output tokens are your top cost ({categories[0][1]/total_spend*100:.0f}%). "
            f"This is the most expensive token type ($25/MTok on Opus, $15 on Sonnet). "
            f"Model selection is your biggest lever — switching output-heavy sessions to "
            f"Sonnet saves 40% on your dominant cost."
        )
    else:
        cost_insight = ""

    return {
        "period": {
            "start": period_start,
            "end": period_end,
            "days": days,
        },
        "overview": {
            "total_spend": total_spend,
            "session_count": len(sessions),
            "daily_avg": total_spend / days,
            "per_session_avg": total_spend / max(1, len(sessions)),
            "per_session_max": max((c["actual"] for c in costs_list), default=0),
        },
        "spend_by_category": {
            "output": {"cost": cat_output, "pct": cat_output / total_spend * 100},
            "cache_write": {"cost": cat_cache_write, "pct": cat_cache_write / total_spend * 100},
            "cache_read": {"cost": cat_cache_read, "pct": cat_cache_read / total_spend * 100},
            "uncached_input": {"cost": cat_uncached, "pct": cat_uncached / total_spend * 100},
        },
        "spend_by_model": {
            tier: {"cost": tier_costs.get(tier, 0), "pct": tier_costs.get(tier, 0) / total_spend * 100}
            for tier in ["opus", "sonnet", "haiku"]
            if tier_costs.get(tier, 0) > 0
        },
        "cache": {
            "hit_rate": cache_hit_rate,
            "total_rebuilds": total_rebuilds,
            "mid_session_rebuilds": total_mid_rebuilds,
            "current_ttl": current_ttl,
            "gaps_5_60": total_gaps_5_60,
            "gaps_over_1h": total_gaps_1h,
            "avoidable_misses": total_avoidable,
            "ttl_savings": ttl_savings,
            "ttl_note": (
                f"1h TTL would prevent {total_avoidable} rebuilds from 5-60m gaps, "
                f"but the 60% write premium on ALL cache writes exceeds the savings. "
                f"Current 5m TTL is cost-optimal."
            ) if total_avoidable > 0 and ttl_savings <= 0 and current_ttl == "5m" else None,
        },
        "sessions": {
            "top": top_sessions,
            "short_opus_count": len(short_opus),
            "sessions_over_200k": sessions_over_200k,
            "sessions_over_500k": sessions_over_500k,
            "avg_max_context": avg_max_context,
        },
        "top_driver": top_driver,
        "cost_insight": cost_insight,
        "categories_ranked": categories,
        "recommendations": recommendations,
        "total_potential_savings": total_potential,
    }


# ── Text report ──────────────────────────────────────────────────────────────

def bar(pct, width=30):
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def fmt_dur(minutes):
    if minutes < 60:
        return f"{minutes:.0f}m"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h{m:02d}m"


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))


def print_text_report(report):
    r = report
    p = r["period"]
    o = r["overview"]

    start_str = p["start"].strftime("%b %d") if p["start"] else "?"
    end_str = p["end"].strftime("%b %d, %Y") if p["end"] else "?"

    print()
    print("=" * 66)
    print("  CLAUDE CODE USAGE ADVISOR")
    print(f"  {start_str} - {end_str}  ({p['days']} days, {o['session_count']} sessions)")
    print("=" * 66)

    # ── Spend overview
    print(f"\n  WHERE YOUR MONEY GOES")
    print(f"  {'─' * 50}")
    print(f"  Total spend:    ${o['total_spend']:>10.2f}")
    print(f"  Daily average:  ${o['daily_avg']:>10.2f}")
    print(f"  Per session:    ${o['per_session_avg']:>10.2f} avg,  ${o['per_session_max']:.2f} max")

    print(f"\n  By category:")
    for label, key in [("Output tokens", "output"), ("Cache writes", "cache_write"),
                       ("Cache reads", "cache_read"), ("Uncached input", "uncached_input")]:
        d = r["spend_by_category"][key]
        print(f"    {label:<16} ${d['cost']:>8.2f}  {d['pct']:>4.0f}%  {bar(d['pct'])}")

    print(f"\n  By model:")
    for tier, d in r["spend_by_model"].items():
        print(f"    {tier.capitalize():<16} ${d['cost']:>8.2f}  {d['pct']:>4.0f}%  {bar(d['pct'])}")

    # ── Cache health
    c = r["cache"]
    print(f"\n  CACHE HEALTH")
    print(f"  {'─' * 50}")
    print(f"  Cache efficiency:      {c['hit_rate']:.1f}% (by token volume)")
    print(f"  Cache rebuilds:        {c['total_rebuilds']} total ({c['mid_session_rebuilds']} mid-session)")
    print(f"  Current TTL:           {c['current_ttl']}")
    if c["gaps_5_60"] > 0 or c["gaps_over_1h"] > 0:
        print(f"  Idle gaps 5-60m:       {c['gaps_5_60']}  (would be avoidable with 1h TTL)")
        print(f"  Idle gaps >1h:         {c['gaps_over_1h']}  (unavoidable)")
    if c.get("ttl_note"):
        print(f"\n  NOTE: {c['ttl_note']}")

    # ── Top sessions
    print(f"\n  TOP SESSIONS")
    print(f"  {'─' * 50}")
    for i, s in enumerate(r["sessions"]["top"][:5], 1):
        ctx = fmt_tokens(s["max_context"]) if s["max_context"] else "?"
        print(f"  #{i}  ${s['cost']:>7.2f}  {s['date']}  "
              f"({s['turns']} turns, {fmt_dur(s['duration_min'])}, "
              f"{s['model'].capitalize()}, {ctx} ctx)")

    top5_cost = sum(s["cost"] for s in r["sessions"]["top"][:5])
    print(f"\n  Top 5 = ${top5_cost:.2f} ({top5_cost / o['total_spend'] * 100:.0f}% of total)")

    # ── Recommendations
    recs = r["recommendations"]
    if recs:
        print(f"\n  RECOMMENDATIONS")
        print(f"  {'─' * 50}")
        print(f"  Sorted by estimated monthly savings:\n")
        for i, rec in enumerate(recs, 1):
            sav = f"~${rec['savings_monthly']:.0f}/mo" if rec["savings_monthly"] else "TBD"
            print(f"  [{i}] {rec['title']:<42} {sav}")
            print(f"  {'┌' + '─' * 62 + '┐'}")
            for line in rec["detail"].split(". "):
                line = line.strip()
                if not line:
                    continue
                if not line.endswith("."):
                    line += "."
                while len(line) > 60:
                    sp = line[:60].rfind(" ")
                    if sp == -1:
                        sp = 60
                    print(f"  │ {line[:sp]:<61}│")
                    line = line[sp:].strip()
                print(f"  │ {line:<61}│")
            print(f"  │{' ' * 62}│")
            for line in rec["setting"].split("\n"):
                print(f"  │ {line:<61}│")
            print(f"  {'└' + '─' * 62 + '┘'}")
            print()

        if r["total_potential_savings"] > 0:
            print(f"  ESTIMATED MONTHLY SAVINGS: ~${r['total_potential_savings']:.0f} "
                  f"({r['total_potential_savings'] / (o['daily_avg'] * 30) * 100:.0f}% of projected spend)")
    else:
        print(f"\n  No parameter changes recommended — your settings look well-tuned")
        print(f"  for your usage patterns. Keep doing what you're doing.")

    if r.get("cost_insight"):
        print(f"\n  KEY INSIGHT")
        print(f"  {'─' * 50}")
        insight = r["cost_insight"]
        while len(insight) > 64:
            sp = insight[:64].rfind(" ")
            if sp == -1:
                sp = 64
            print(f"  {insight[:sp]}")
            insight = insight[sp:].strip()
        if insight:
            print(f"  {insight}")

    print(f"\n  Run with --html > report.html for a shareable version.")
    print()


# ── HTML report ──────────────────────────────────────────────────────────────

def generate_html(report):
    r = report
    p = r["period"]
    o = r["overview"]

    start_str = p["start"].strftime("%b %d") if p["start"] else "?"
    end_str = p["end"].strftime("%b %d, %Y") if p["end"] else "?"

    # Build recommendation cards
    rec_cards = ""
    for i, rec in enumerate(r["recommendations"], 1):
        sav = f"~${rec['savings_monthly']:.0f}/mo" if rec["savings_monthly"] else ""
        setting_html = rec["setting"].replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        rec_cards += f"""
        <div class="card rec-card">
          <div class="rec-header">
            <span class="rec-num">#{i}</span>
            <span class="rec-title">{rec['title']}</span>
            <span class="rec-savings">{sav}</span>
          </div>
          <p class="rec-detail">{rec['detail']}</p>
          <div class="rec-setting"><code>{setting_html}</code></div>
        </div>"""

    # Build category bars
    cat_bars = ""
    for label, key in [("Output tokens", "output"), ("Cache writes", "cache_write"),
                       ("Cache reads", "cache_read"), ("Uncached input", "uncached_input")]:
        d = r["spend_by_category"][key]
        color = {"output": "#79c0ff", "cache_write": "#d29922",
                 "cache_read": "#3fb950", "uncached_input": "#8b949e"}[key]
        cat_bars += f"""
        <div class="bar-row">
          <span class="bar-label">{label}</span>
          <div class="bar-track">
            <div class="bar-fill" style="width:{d['pct']:.1f}%;background:{color}"></div>
          </div>
          <span class="bar-value">${d['cost']:.2f}</span>
          <span class="bar-pct">{d['pct']:.0f}%</span>
        </div>"""

    # Build model bars
    model_bars = ""
    for tier, d in r["spend_by_model"].items():
        color = {"opus": "#bc8cff", "sonnet": "#79c0ff", "haiku": "#3fb950"}.get(tier, "#8b949e")
        model_bars += f"""
        <div class="bar-row">
          <span class="bar-label">{tier.capitalize()}</span>
          <div class="bar-track">
            <div class="bar-fill" style="width:{d['pct']:.1f}%;background:{color}"></div>
          </div>
          <span class="bar-value">${d['cost']:.2f}</span>
          <span class="bar-pct">{d['pct']:.0f}%</span>
        </div>"""

    # Build sessions table
    session_rows = ""
    for i, s in enumerate(r["sessions"]["top"][:10], 1):
        ctx = fmt_tokens(s["max_context"]) if s["max_context"] else "?"
        session_rows += f"""
        <tr>
          <td>#{i}</td>
          <td>${s['cost']:.2f}</td>
          <td>{s['date']}</td>
          <td>{s['turns']}</td>
          <td>{fmt_dur(s['duration_min'])}</td>
          <td>{s['model'].capitalize()}</td>
          <td>{ctx}</td>
        </tr>"""

    # Cache health
    c = r["cache"]
    ttl_note_html = f'<p style="color:#8b949e;font-size:13px;margin-top:12px">{c.get("ttl_note","")}</p>' if c.get("ttl_note") else ""
    cache_section = f"""
    <div class="card">
      <h2>Cache Health</h2>
      <div class="stat-row">
        <div class="stat">
          <div class="stat-value">{c['hit_rate']:.1f}%</div>
          <div class="stat-label">Cache Efficiency</div>
        </div>
        <div class="stat">
          <div class="stat-value">{c['current_ttl']}</div>
          <div class="stat-label">Current TTL</div>
        </div>
        <div class="stat">
          <div class="stat-value">{c['total_rebuilds']}</div>
          <div class="stat-label">Cache Rebuilds</div>
        </div>
        <div class="stat">
          <div class="stat-value">{c['gaps_5_60']}</div>
          <div class="stat-label">Avoidable Gaps (5-60m)</div>
        </div>
      </div>
      {ttl_note_html}
    </div>"""

    # Savings banner
    if r["total_potential_savings"] > 0:
        projected = o["daily_avg"] * 30
        pct = r["total_potential_savings"] / projected * 100 if projected > 0 else 0
        savings_banner = f"""
        <div class="savings-banner">
          Estimated savings: <strong>~${r['total_potential_savings']:.0f}/month</strong>
          ({pct:.0f}% of projected ${projected:.0f}/mo spend) — no workflow changes needed
        </div>"""
    else:
        savings_banner = """
        <div class="savings-banner good">
          Your settings look well-tuned for your usage patterns. Keep it up.
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Code Usage Advisor — {end_str}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    background: #0d1117; color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 14px; line-height: 1.5;
  }}
  .container {{ max-width: 860px; margin: 0 auto; }}
  h1 {{
    font-size: 24px; font-weight: 600; margin: 0 0 4px;
    color: #f0f6fc;
  }}
  .subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; }}
  h2 {{ font-size: 18px; font-weight: 600; margin: 0 0 16px; color: #f0f6fc; }}

  /* Stat cards */
  .stat-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }}
  .stat-card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; text-align: center;
  }}
  .stat-card .stat-value {{ font-size: 28px; font-weight: 700; color: #f0f6fc; }}
  .stat-card .stat-label {{ font-size: 13px; color: #8b949e; margin-top: 4px; }}

  /* Cards */
  .card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 20px; margin-bottom: 16px;
  }}

  /* Savings banner */
  .savings-banner {{
    background: #0b2e13; border: 1px solid #238636; border-radius: 8px;
    padding: 16px 20px; margin-bottom: 24px; font-size: 16px;
    color: #3fb950; text-align: center;
  }}
  .savings-banner.good {{
    background: #0d1d31; border-color: #1f6feb; color: #58a6ff;
  }}
  .savings-banner strong {{ font-size: 20px; }}

  /* Recommendation cards */
  .rec-card {{ border-left: 3px solid #3fb950; }}
  .rec-header {{
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 12px;
  }}
  .rec-num {{
    background: #238636; color: #fff; border-radius: 4px;
    padding: 2px 8px; font-size: 13px; font-weight: 600;
  }}
  .rec-title {{ font-size: 16px; font-weight: 600; color: #f0f6fc; flex: 1; }}
  .rec-savings {{
    font-size: 16px; font-weight: 700; color: #3fb950;
  }}
  .rec-detail {{ color: #c9d1d9; margin: 0 0 12px; font-size: 14px; }}
  .rec-setting {{
    background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    padding: 12px 16px; font-size: 13px;
  }}
  .rec-setting code {{ color: #79c0ff; white-space: pre-wrap; }}

  /* Bar charts */
  .bar-row {{
    display: grid; grid-template-columns: 130px 1fr 80px 44px;
    align-items: center; gap: 8px; margin-bottom: 8px;
  }}
  .bar-label {{ font-size: 13px; color: #c9d1d9; }}
  .bar-track {{
    height: 20px; background: #21262d; border-radius: 4px; overflow: hidden;
  }}
  .bar-fill {{ height: 100%; border-radius: 4px; min-width: 2px; }}
  .bar-value {{ font-size: 14px; color: #e6edf3; text-align: right; font-weight: 500; }}
  .bar-pct {{ font-size: 13px; color: #8b949e; text-align: right; }}

  /* Stat row (inline stats) */
  .stat-row {{
    display: flex; gap: 24px; flex-wrap: wrap;
  }}
  .stat {{ text-align: center; flex: 1; min-width: 100px; }}
  .stat .stat-value {{ font-size: 24px; font-weight: 700; color: #f0f6fc; }}
  .stat .stat-label {{ font-size: 13px; color: #8b949e; }}

  /* Table */
  table {{
    width: 100%; border-collapse: collapse; font-size: 14px;
  }}
  th {{
    text-align: left; font-size: 13px; color: #8b949e;
    padding: 8px; border-bottom: 1px solid #30363d; font-weight: 500;
  }}
  td {{ padding: 8px; border-bottom: 1px solid #21262d; color: #c9d1d9; }}
  tr:hover td {{ background: #1c2128; }}
  td:nth-child(2) {{ font-weight: 600; color: #f0f6fc; }}

  .footer {{
    text-align: center; color: #484f58; font-size: 13px;
    margin-top: 32px; padding-top: 16px; border-top: 1px solid #21262d;
  }}
  .footer a {{ color: #58a6ff; text-decoration: none; }}
</style>
</head>
<body>
<div class="container">
  <h1>Claude Code Usage Advisor</h1>
  <div class="subtitle">{start_str} &ndash; {end_str} &middot; {p['days']} days &middot; {o['session_count']} sessions</div>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-value">${o['total_spend']:.2f}</div>
      <div class="stat-label">Total Spend</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${o['daily_avg']:.2f}</div>
      <div class="stat-label">Daily Average</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${o['per_session_avg']:.2f}</div>
      <div class="stat-label">Per Session Avg</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{r['top_driver']}</div>
      <div class="stat-label">Top Cost Driver</div>
    </div>
  </div>

  {savings_banner}

  {"<h2>Recommendations</h2>" if r['recommendations'] else ""}
  {rec_cards}

  <div class="card">
    <h2>Spend by Category</h2>
    {cat_bars}
  </div>

  <div class="card">
    <h2>Spend by Model</h2>
    {model_bars}
  </div>

  {cache_section}

  <div class="card">
    <h2>Top Sessions</h2>
    <table>
      <tr><th>#</th><th>Cost</th><th>Date</th><th>Turns</th><th>Duration</th><th>Model</th><th>Max Ctx</th></tr>
      {session_rows}
    </table>
  </div>

  {"<div class='card'><h2>Key Insight</h2><p style=\"color:#c9d1d9\">" + r.get("cost_insight","") + "</p></div>" if r.get("cost_insight") else ""}

  <div class="footer">
    Generated by Claude Code Usage Advisor &middot; {datetime.now().strftime("%Y-%m-%d %H:%M")}
  </div>
</div>
</body>
</html>"""


# ── JSON output ──────────────────────────────────────────────────────────────

def serialize_report(report):
    """Make the report JSON-serializable."""
    r = dict(report)
    p = r["period"]
    r["period"] = {
        "start": p["start"].isoformat() if p["start"] else None,
        "end": p["end"].isoformat() if p["end"] else None,
        "days": p["days"],
    }
    return r


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyze Claude Code usage and recommend cost-saving settings changes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "path", nargs="?",
        default=os.path.expanduser("~/.claude/projects"),
        help="Path to projects dir or single .jsonl file (default: ~/.claude/projects/)",
    )
    parser.add_argument("--since", help="Only include sessions after YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument("--html", action="store_true", help="Output self-contained HTML report")
    args = parser.parse_args()

    files = find_session_files(args.path)
    if not files:
        print(f"No .jsonl files found in {args.path}", file=sys.stderr)
        print(f"\nExpected: ~/.claude/projects/<project>/sessions/<uuid>.jsonl", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {len(files)} files...", file=sys.stderr)

    since_date = None
    if args.since:
        since_date = datetime.fromisoformat(args.since).replace(tzinfo=None)

    sessions = []
    costs_list = []
    skipped = 0

    for filepath in files:
        session = parse_session(filepath)
        if not session:
            skipped += 1
            continue
        if since_date and session["first_ts"]:
            if session["first_ts"].replace(tzinfo=None) < since_date:
                skipped += 1
                continue
        costs = compute_session_costs(session)
        if costs["turns"] == 0:
            skipped += 1
            continue
        sessions.append(session)
        costs_list.append(costs)

    if not sessions:
        print("No sessions with usage data found.", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzed {len(sessions)} sessions ({skipped} skipped)\n", file=sys.stderr)

    report = run_analysis(sessions, costs_list)

    if args.json:
        print(json.dumps(serialize_report(report), indent=2, default=str))
    elif args.html:
        print(generate_html(report))
    else:
        print_text_report(report)


if __name__ == "__main__":
    main()
