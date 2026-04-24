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
# Relative cost: Opus 4.7 ~2x Sonnet, Opus 4.6 ~1.67x Sonnet

PRICING = {
    "opus-4-7": {
        "base_input": 6.00,
        "cache_write_5m": 7.50,
        "cache_write_1h": 12.00,
        "cache_read": 0.60,
        "output": 30.00,
    },
    "opus-4-6": {
        "base_input": 5.00,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
        "output": 25.00,
    },
    "opus-4-5": {
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

DEFAULT_TIER = "sonnet"


def model_tier(model_str):
    """Return pricing tier key. Distinguishes Opus versions."""
    if not model_str:
        return DEFAULT_TIER
    m = model_str.lower()
    if "opus-4-7" in m or "opus-4.7" in m:
        return "opus-4-7"
    if "opus-4-6" in m or "opus-4.6" in m:
        return "opus-4-6"
    if "opus-4-5" in m or "opus-4.5" in m:
        return "opus-4-5"
    if "opus" in m:
        return "opus-4-6"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return DEFAULT_TIER


def tier_family(tier):
    """Return 'opus', 'sonnet', or 'haiku' for display grouping."""
    if tier.startswith("opus"):
        return "opus"
    return tier


def opus_version(model_str):
    """Return '4.7', '4.6', '4.5', or None."""
    if not model_str:
        return None
    m = model_str.lower()
    if "opus-4-7" in m or "opus-4.7" in m:
        return "4.7"
    if "opus-4-6" in m or "opus-4.6" in m:
        return "4.6"
    if "opus-4-5" in m or "opus-4.5" in m:
        return "4.5"
    if "opus" in m:
        return "4.6"
    return None


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

        family = tier_family(tier)
        by_tier[family]["cost"] = by_tier[family].get("cost", 0) + actual
        by_tier[family]["output_cost"] = by_tier[family].get("output_cost", 0) + out_c
        by_tier[family]["turns"] = by_tier[family].get("turns", 0) + 1

        # Track Opus version and context excess for 4.7 → 4.6 recommendation
        ov = opus_version(t["model"])
        if ov == "4.7":
            by_tier["opus"]["turns_4_7"] = by_tier["opus"].get("turns_4_7", 0) + 1
            context = t["cache_read_tokens"] + t["cache_write_tokens"] + t["input_tokens"]
            by_tier["opus"]["peak_context"] = max(by_tier["opus"].get("peak_context", 0), context)

            # Per-token savings: what this turn would cost on 4.6 pricing
            r46 = PRICING["opus-4-6"]
            cost_4_7 = actual
            cost_4_6 = (
                (t["output_tokens"] / mtok) * r46["output"]
                + (t["cache_read_tokens"] / mtok) * r46["cache_read"]
                + (t["cache_write_5m"] / mtok) * r46["cache_write_5m"]
                + (t["cache_write_1h"] / mtok) * r46["cache_write_1h"]
                + (t["input_tokens"] / mtok) * r46["base_input"]
            )
            by_tier["opus"]["price_diff_4_7"] = by_tier["opus"].get("price_diff_4_7", 0) + (cost_4_7 - cost_4_6)

            if context > 200_000:
                excess = context - 200_000
                excess_read_cost = (excess / mtok) * r["cache_read"]
                by_tier["opus"]["excess_4_7_cost"] = by_tier["opus"].get("excess_4_7_cost", 0) + excess_read_cost
                by_tier["opus"]["turns_over_200k"] = by_tier["opus"].get("turns_over_200k", 0) + 1
        elif ov:
            by_tier["opus"]["turns_4_6"] = by_tier["opus"].get("turns_4_6", 0) + 1

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

    # Spend by model family (opus/sonnet/haiku)
    tier_costs = defaultdict(float)
    tier_output = defaultdict(float)
    tier_turns = defaultdict(int)
    for c in costs_list:
        for tier, data in c["by_tier"].items():
            tier_costs[tier] += data.get("cost", 0)
            tier_output[tier] += data.get("output_cost", 0)
            tier_turns[tier] += int(data.get("turns", 0))

    # Opus 4.7 → 4.6 analysis
    total_excess_4_7_cost = 0.0
    total_price_diff_4_7 = 0.0
    total_turns_4_7 = 0
    total_turns_4_6_compat = 0
    total_turns_over_200k = 0
    peak_context_all = 0
    for c in costs_list:
        od = c["by_tier"].get("opus", {})
        total_excess_4_7_cost += od.get("excess_4_7_cost", 0)
        total_price_diff_4_7 += od.get("price_diff_4_7", 0)
        total_turns_4_7 += int(od.get("turns_4_7", 0))
        total_turns_4_6_compat += int(od.get("turns_4_6", 0))
        total_turns_over_200k += int(od.get("turns_over_200k", 0))
        peak_context_all = max(peak_context_all, od.get("peak_context", 0))

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

    # Rec 2: Opus 4.7 → 4.6 (same family, ~17% cheaper per token, 200K context cap)
    total_4_7_savings = total_price_diff_4_7 + total_excess_4_7_cost
    if total_turns_4_7 > 0 and total_4_7_savings > 5:
        monthly_4_6 = (total_4_7_savings / days) * 30
        peak_str = fmt_tokens(peak_context_all) if peak_context_all else "?"
        pct_cheaper = (total_price_diff_4_7 / max(0.01, total_price_diff_4_7 + sum(
            c["by_tier"].get("opus", {}).get("cost", 0) for c in costs_list
            if c["by_tier"].get("opus", {}).get("turns_4_7", 0) > 0
        ))) * 100 if total_price_diff_4_7 > 0 else 0

        detail_parts = [
            f"You have {total_turns_4_7} turns on Opus 4.7. "
            f"Opus 4.6 is the same model family at ~1.67x Sonnet cost "
            f"vs 4.7's ~2x — about 17% cheaper per token."
        ]
        if total_price_diff_4_7 > 0:
            detail_parts.append(
                f"Per-token savings alone: ${total_price_diff_4_7:.2f} over {days} days."
            )
        if total_turns_over_200k > 0:
            detail_parts.append(
                f"Plus {total_turns_over_200k} turns exceeded 200K context "
                f"(peak: {peak_str}) — 4.6's smaller window forces earlier "
                f"compaction, saving an additional ${total_excess_4_7_cost:.2f}."
            )
        detail_parts.append(
            f"Total estimated savings: ${total_4_7_savings:.2f} over {days} days."
        )

        recommendations.append({
            "id": "opus_4_6",
            "title": "Switch from Opus 4.7 to 4.6",
            "savings_monthly": monthly_4_6,
            "detail": " ".join(detail_parts),
            "setting": (
                'Set your model version:\n'
                '  claude --model claude-opus-4-6\n'
                'Or in settings.json:\n'
                '  "model": "claude-opus-4-6"'
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
            f"not because they're expensive per token (they're the cheapest input type) "
            f"but because your sessions accumulate large contexts. Every turn "
            f"re-reads the full context. The biggest lever is keeping contexts smaller "
            f"through earlier compaction — Opus 4.6's 200K window does this automatically."
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
            f"This is the most expensive token type ($25-30/MTok on Opus). The biggest lever "
            f"is context size — smaller contexts mean fewer tokens processed per turn, "
            f"leaving more budget for output."
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
            "sessions_over_200k": sessions_over_200k,
            "sessions_over_500k": sessions_over_500k,
            "avg_max_context": avg_max_context,
        },
        "opus_versions": {
            "turns_4_7": total_turns_4_7,
            "turns_4_6": total_turns_4_6_compat,
            "turns_over_200k": total_turns_over_200k,
            "peak_context": peak_context_all,
            "excess_cost": total_excess_4_7_cost,
            "price_diff": total_price_diff_4_7,
            "total_savings": total_4_7_savings,
        },
        "top_driver": top_driver,
        "cost_insight": cost_insight,
        "categories_ranked": categories,
        "recommendations": recommendations,
        "total_potential_savings": total_potential,
        "_raw_costs": costs_list,
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


# ── HTML report (slideshow) ──────────────────────────────────────────────────

def _rec_visual(rec, report):
    """Build a simple visual element for a recommendation slide."""
    rid = rec["id"]
    if rid == "opus_4_6":
        ov = report.get("opus_versions", {})
        price_diff = ov.get("price_diff", 0)
        excess = ov.get("excess_cost", 0)
        turns_4_7 = ov.get("turns_4_7", 0)
        turns_over = ov.get("turns_over_200k", 0)
        peak = ov.get("peak_context", 0)
        peak_k = peak // 1000 if peak else 0

        return f"""
        <div class="visual comparison">
          <div class="comp-row">
            <div class="comp-label">Opus 4.7 (~2x)</div>
            <div class="comp-track"><div class="comp-fill" style="width:100%;background:#bc8cff"></div></div>
            <div class="comp-value">$30/MTok</div>
          </div>
          <div class="comp-row">
            <div class="comp-label">Opus 4.6 (~1.67x)</div>
            <div class="comp-track"><div class="comp-fill" style="width:83%;background:#79c0ff"></div></div>
            <div class="comp-value">$25/MTok</div>
          </div>
          <div class="comp-diff">{turns_4_7} turns &middot; 17% cheaper per token{f" &middot; {turns_over} turns over 200K" if turns_over > 0 else ""}</div>
        </div>"""
    elif rid == "cache_ttl_1h":
        n = report["cache"]["avoidable_misses"]
        return f"""
        <div class="visual stat-block">
          <div class="stat-big">{n}</div>
          <div class="stat-desc">avoidable cache rebuilds</div>
          <div class="stat-sub">from 5-60 min idle gaps — each one re-writes your full context</div>
        </div>"""
    elif rid == "cache_ttl_5m":
        days = report["period"]["days"]
        sav = (rec["savings_monthly"] or 0) / 30 * days
        return f"""
        <div class="visual stat-block">
          <div class="stat-big">${sav:.0f}</div>
          <div class="stat-desc">saved with cheaper 5m write rate</div>
          <div class="stat-sub">1.25x vs 2x per cache write token</div>
        </div>"""
    elif rid == "context_bloat":
        n = report["sessions"]["sessions_over_500k"]
        return f"""
        <div class="visual stat-block">
          <div class="stat-big">{n}</div>
          <div class="stat-desc">sessions over 500K context tokens</div>
          <div class="stat-sub">large contexts drive up per-turn read costs</div>
        </div>"""
    return ""


def generate_html(report):
    r = report
    p = r["period"]
    o = r["overview"]
    c = r["cache"]
    recs = r["recommendations"]

    start_str = p["start"].strftime("%b %d") if p["start"] else "?"
    end_str = p["end"].strftime("%b %d, %Y") if p["end"] else "?"

    # ── Build slides ─────────────────────────────────────────────────────
    slides = []

    # Slide 0: Summary — answer the question upfront
    if recs:
        n = len(recs)
        summary_answer = "Yes."
        summary_answer_class = "summary-yes"
        summary_detail = (
            f'{n} easy setting{"s" if n != 1 else ""} change{"s" if n != 1 else ""} '
            f'could save ~${r["total_potential_savings"]:.0f}/mo'
        )
        summary_sub = "No workflow changes — just parameter tuning."
    else:
        summary_answer = "Nope, you're good."
        summary_answer_class = "summary-no"
        summary_detail = "Your Claude is configured well for how you work."
        summary_sub = "No easy wins found — your settings match your usage patterns."

    slides.append(f"""
      <div class="slide-inner summary">
        <div class="summary-q">Is there anything you can easily change<br>to make your tokens go further?</div>
        <div class="{summary_answer_class}">{summary_answer}</div>
        <div class="summary-detail">{summary_detail}</div>
        <div class="summary-sub">{summary_sub}</div>
      </div>""")

    # Slide 1: Spend overview
    slides.append(f"""
      <div class="slide-inner cover">
        <div class="cover-label">Your Usage</div>
        <div class="big-number">${o['total_spend']:.0f}</div>
        <div class="cover-period">{start_str} &ndash; {end_str}</div>
        <div class="cover-meta">{o['session_count']} sessions &middot; {p['days']} days &middot; ${o['daily_avg']:.0f}/day avg</div>
      </div>""")

    # Slides 1-N: Recommendations
    for rec in recs:
        sav = f"~${rec['savings_monthly']:.0f}/mo" if rec["savings_monthly"] else ""
        visual = _rec_visual(rec, r)
        setting_html = rec["setting"].replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")

        slides.append(f"""
      <div class="slide-inner rec">
        <div class="rec-savings">{sav}</div>
        <h2>{rec['title']}</h2>
        {visual}
        <p class="rec-detail">{rec['detail']}</p>
        <div class="setting-block">
          <code>{setting_html}</code>
          <button class="copy-btn" onclick="copyCmd(this)">Copy</button>
        </div>
      </div>""")

    # Action plan slide
    action_items = ""
    for i, rec in enumerate(recs, 1):
        sav = f"~${rec['savings_monthly']:.0f}/mo" if rec["savings_monthly"] else ""
        lines = rec["setting"].strip().split("\n")
        cmd = next((l.strip() for l in lines if l.strip() and not l.strip().startswith(("#", "Or", "Upgrade", "Switch", "Remove", "or set", "For"))), lines[0].strip())
        action_items += f"""
          <div class="action-item">
            <div class="action-num">{i}</div>
            <div class="action-body">
              <div class="action-title">{rec['title']}</div>
              <code>{cmd}</code>
            </div>
            <div class="action-sav">{sav}</div>
          </div>"""

    if not recs:
        action_items = """
          <div class="all-good-card">
            <div class="ag-label">All clear</div>
            <div class="ag-text">Your Claude Code settings match your usage patterns.<br>No changes needed.</div>
          </div>"""

    total_sav = r["total_potential_savings"]
    projected = o["daily_avg"] * 30
    sav_pct = (total_sav / projected * 100) if projected > 0 else 0
    savings_line = f'<div class="total-savings">~${total_sav:.0f}/mo estimated savings ({sav_pct:.0f}% of projected spend)</div>' if total_sav > 0 else ""

    slides.append(f"""
      <div class="slide-inner action">
        <h2>{"Your Action Plan" if recs else "Summary"}</h2>
        <div class="action-list">{action_items}</div>
        {savings_line}
        <button class="see-report" onclick="document.getElementById('appendix').scrollIntoView({{behavior:'smooth'}})">See full report</button>
      </div>""")

    # Build slides HTML
    slides_html = ""
    for i, s in enumerate(slides):
        active = " active" if i == 0 else ""
        slides_html += f'<div class="slide{active}" data-idx="{i}">{s}</div>\n'

    dots_html = " ".join(
        f'<button class="dot{" active" if i == 0 else ""}" data-idx="{i}"></button>'
        for i in range(len(slides))
    )

    # ── Build appendix ───────────────────────────────────────────────────
    cat_bars = ""
    for label, key in [("Output tokens", "output"), ("Cache writes", "cache_write"),
                       ("Cache reads", "cache_read"), ("Uncached input", "uncached_input")]:
        d = r["spend_by_category"][key]
        color = {"output": "#79c0ff", "cache_write": "#d29922",
                 "cache_read": "#3fb950", "uncached_input": "#8b949e"}[key]
        cat_bars += f"""
          <div class="a-bar-row">
            <span class="a-bar-label">{label}</span>
            <div class="a-bar-track"><div class="a-bar-fill" style="width:{d['pct']:.1f}%;background:{color}"></div></div>
            <span class="a-bar-value">${d['cost']:.2f}</span>
            <span class="a-bar-pct">{d['pct']:.0f}%</span>
          </div>"""

    model_bars = ""
    for tier, d in r["spend_by_model"].items():
        color = {"opus": "#bc8cff", "sonnet": "#79c0ff", "haiku": "#3fb950"}.get(tier, "#8b949e")
        model_bars += f"""
          <div class="a-bar-row">
            <span class="a-bar-label">{tier.capitalize()}</span>
            <div class="a-bar-track"><div class="a-bar-fill" style="width:{d['pct']:.1f}%;background:{color}"></div></div>
            <span class="a-bar-value">${d['cost']:.2f}</span>
            <span class="a-bar-pct">{d['pct']:.0f}%</span>
          </div>"""

    session_rows = ""
    for i, s in enumerate(r["sessions"]["top"][:10], 1):
        ctx = fmt_tokens(s["max_context"]) if s["max_context"] else "?"
        session_rows += f"""
          <tr><td>#{i}</td><td>${s['cost']:.2f}</td><td>{s['date']}</td>
          <td>{s['turns']}</td><td>{fmt_dur(s['duration_min'])}</td>
          <td>{s['model'].capitalize()}</td><td>{ctx}</td></tr>"""

    ttl_note = f'<p class="a-note">{c.get("ttl_note","")}</p>' if c.get("ttl_note") else ""
    insight = f'<div class="a-card"><h3>Key Insight</h3><p>{r.get("cost_insight","")}</p></div>' if r.get("cost_insight") else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Code Usage Advisor</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    background: #0d1117; color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 16px; line-height: 1.5;
    overflow-x: hidden;
  }}

  /* ── Slides ── */
  .slide {{
    min-height: 100vh; min-height: 100dvh;
    display: none;
    align-items: center; justify-content: center;
    padding: 48px 24px 100px;
  }}
  .slide.active {{ display: flex; }}
  .slide-inner {{
    max-width: 600px; width: 100%; text-align: center;
  }}

  /* ── Cover ── */
  .cover-label {{
    font-size: 14px; color: #8b949e;
    text-transform: uppercase; letter-spacing: 3px;
    margin-bottom: 32px;
  }}
  .big-number {{
    font-size: 80px; font-weight: 800;
    color: #f0f6fc; line-height: 1; margin-bottom: 8px;
  }}
  .cover-period {{ font-size: 18px; color: #8b949e; }}
  .cover-meta {{ font-size: 14px; color: #484f58; margin-bottom: 40px; }}
  .cover-sub {{ font-size: 22px; color: #c9d1d9; margin-bottom: 8px; }}
  /* ── Summary (first slide) ── */
  .summary-q {{
    font-size: 22px; color: #8b949e; line-height: 1.5;
    margin-bottom: 40px;
  }}
  .summary-yes {{
    font-size: 72px; font-weight: 800; color: #3fb950;
    line-height: 1; margin-bottom: 16px;
  }}
  .summary-no {{
    font-size: 48px; font-weight: 800; color: #58a6ff;
    line-height: 1; margin-bottom: 16px;
  }}
  .summary-detail {{
    font-size: 22px; color: #e6edf3; margin-bottom: 8px;
  }}
  .summary-sub {{
    font-size: 16px; color: #484f58;
  }}

  /* ── Rec slides ── */
  .rec .rec-savings {{
    font-size: 44px; font-weight: 800; color: #3fb950;
    margin-bottom: 4px; line-height: 1;
  }}
  .rec h2 {{
    font-size: 26px; font-weight: 600; color: #f0f6fc;
    margin: 0 0 24px;
  }}
  .rec .rec-detail {{
    font-size: 15px; color: #8b949e;
    margin: 20px 0 28px; text-align: left; line-height: 1.7;
  }}

  /* Comparison bars */
  .comparison {{ margin: 24px 0; }}
  .comp-row {{
    display: grid; grid-template-columns: 140px 1fr 80px;
    align-items: center; gap: 12px; margin-bottom: 10px;
  }}
  .comp-label {{ font-size: 14px; color: #c9d1d9; text-align: right; }}
  .comp-track {{
    height: 36px; background: #21262d; border-radius: 6px; overflow: hidden;
  }}
  .comp-fill {{
    height: 100%; border-radius: 6px;
    transition: width 0.8s cubic-bezier(.4,0,.2,1) 0.15s;
  }}
  .slide:not(.active) .comp-fill {{ width: 0 !important; }}
  .comp-value {{ font-size: 20px; font-weight: 700; color: #f0f6fc; }}
  .comp-diff {{
    text-align: right; font-size: 14px; color: #3fb950;
    margin-top: 4px; font-weight: 600;
  }}

  /* Stat block */
  .stat-block {{ margin: 28px 0; }}
  .stat-block .stat-big {{
    font-size: 72px; font-weight: 800; color: #f0f6fc; line-height: 1;
    opacity: 1; transform: translateY(0);
    transition: opacity 0.5s ease 0.1s, transform 0.5s ease 0.1s;
  }}
  .slide:not(.active) .stat-big {{ opacity: 0; transform: translateY(24px); }}
  .stat-block .stat-desc {{ font-size: 20px; color: #8b949e; margin-top: 8px; }}
  .stat-block .stat-sub {{ font-size: 14px; color: #484f58; margin-top: 4px; }}

  /* Setting block */
  .setting-block {{
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 20px 24px; text-align: left; position: relative;
  }}
  .setting-block code {{
    color: #79c0ff; font-size: 14px; white-space: pre-wrap; line-height: 1.8;
  }}
  .copy-btn {{
    position: absolute; top: 12px; right: 12px;
    background: #21262d; border: 1px solid #30363d; color: #8b949e;
    border-radius: 6px; padding: 4px 14px; font-size: 13px; cursor: pointer;
  }}
  .copy-btn:hover {{ color: #e6edf3; border-color: #58a6ff; }}

  /* ── Action plan ── */
  .action h2 {{
    font-size: 28px; font-weight: 600; color: #f0f6fc; margin: 0 0 32px;
  }}
  .action-list {{ text-align: left; }}
  .action-item {{
    display: flex; align-items: center; gap: 16px;
    background: #161b22; border: 1px solid #30363d;
    border-left: 3px solid #3fb950; border-radius: 10px;
    padding: 20px 24px; margin-bottom: 12px;
  }}
  .action-num {{
    font-size: 28px; font-weight: 800; color: #3fb950; min-width: 36px;
  }}
  .action-body {{ flex: 1; }}
  .action-title {{ font-size: 16px; font-weight: 600; color: #f0f6fc; margin-bottom: 4px; }}
  .action-body code {{ font-size: 13px; color: #79c0ff; }}
  .action-sav {{ font-size: 18px; font-weight: 700; color: #3fb950; white-space: nowrap; }}
  .total-savings {{
    font-size: 22px; font-weight: 600; color: #3fb950;
    margin: 28px 0 16px; text-align: center;
  }}
  .all-good-card {{
    background: #0d1d31; border: 1px solid #1f6feb; border-radius: 10px;
    padding: 32px; text-align: center;
  }}
  .ag-label {{ font-size: 28px; font-weight: 700; color: #58a6ff; margin-bottom: 8px; }}
  .ag-text {{ font-size: 16px; color: #8b949e; }}
  .see-report {{
    background: none; border: 1px solid #30363d; color: #8b949e;
    border-radius: 8px; padding: 12px 28px; font-size: 15px;
    cursor: pointer; margin-top: 20px;
  }}
  .see-report:hover {{ color: #e6edf3; border-color: #58a6ff; }}

  /* ── Nav ── */
  .slide-nav {{
    position: fixed; bottom: 0; left: 0; right: 0;
    display: flex; align-items: center; justify-content: center;
    gap: 20px; padding: 16px;
    background: rgba(13,17,23,0.92);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border-top: 1px solid #21262d; z-index: 100;
  }}
  .nav-btn {{
    background: #21262d; border: 1px solid #30363d; color: #e6edf3;
    border-radius: 8px; width: 44px; height: 44px; font-size: 20px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
  }}
  .nav-btn:hover {{ background: #30363d; }}
  .nav-btn:disabled {{ opacity: 0.25; cursor: default; }}
  .dots {{ display: flex; gap: 8px; }}
  .dot {{
    width: 10px; height: 10px; border-radius: 50%;
    border: none; background: #30363d; cursor: pointer; padding: 0;
    transition: background 0.2s;
  }}
  .dot.active {{ background: #58a6ff; }}
  .slide-counter {{
    font-size: 14px; color: #484f58; min-width: 50px; text-align: center;
  }}

  /* ── Appendix ── */
  .appendix {{
    max-width: 800px; margin: 0 auto;
    padding: 64px 24px 120px;
    border-top: 1px solid #21262d;
  }}
  .appendix h2 {{
    font-size: 14px; font-weight: 600; color: #8b949e;
    text-transform: uppercase; letter-spacing: 2px;
    margin: 48px 0 8px;
  }}
  .appendix h2:first-child {{ margin-top: 0; }}
  .a-card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 20px; margin-bottom: 12px;
  }}
  .a-card h3 {{ font-size: 16px; color: #f0f6fc; margin: 0 0 12px; }}
  .a-card p {{ font-size: 14px; color: #8b949e; margin: 0; line-height: 1.6; }}
  .a-bar-row {{
    display: grid; grid-template-columns: 120px 1fr 72px 40px;
    align-items: center; gap: 8px; margin-bottom: 6px;
  }}
  .a-bar-label {{ font-size: 13px; color: #c9d1d9; }}
  .a-bar-track {{ height: 18px; background: #21262d; border-radius: 4px; overflow: hidden; }}
  .a-bar-fill {{ height: 100%; border-radius: 4px; min-width: 2px; }}
  .a-bar-value {{ font-size: 14px; color: #e6edf3; text-align: right; font-weight: 500; }}
  .a-bar-pct {{ font-size: 13px; color: #8b949e; text-align: right; }}
  .a-stat-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 8px; }}
  .a-stat {{ text-align: center; flex: 1; min-width: 90px; }}
  .a-stat .val {{ font-size: 22px; font-weight: 700; color: #f0f6fc; }}
  .a-stat .lbl {{ font-size: 13px; color: #8b949e; }}
  .a-note {{ font-size: 13px; color: #8b949e; margin-top: 8px; }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 8px;
  }}
  th {{
    text-align: left; font-size: 13px; color: #8b949e;
    padding: 8px; border-bottom: 1px solid #30363d; font-weight: 500;
  }}
  td {{ padding: 8px; border-bottom: 1px solid #21262d; color: #c9d1d9; }}
  td:nth-child(2) {{ font-weight: 600; color: #f0f6fc; }}
  .a-footer {{
    text-align: center; color: #484f58; font-size: 13px;
    margin-top: 48px; padding-top: 16px; border-top: 1px solid #21262d;
  }}

  @media (max-width: 640px) {{
    .big-number {{ font-size: 56px; }}
    .rec .rec-savings {{ font-size: 36px; }}
    .stat-block .stat-big {{ font-size: 48px; }}
    .comp-row {{ grid-template-columns: 100px 1fr 64px; }}
    .action-item {{ flex-wrap: wrap; }}
    .action-sav {{ width: 100%; text-align: left; padding-left: 52px; }}
  }}
</style>
</head>
<body>

{slides_html}

<nav class="slide-nav">
  <button class="nav-btn" id="prev" disabled>&larr;</button>
  <div class="dots">{dots_html}</div>
  <span class="slide-counter" id="counter">1 / {len(slides)}</span>
  <button class="nav-btn" id="next">&rarr;</button>
</nav>

<div class="appendix" id="appendix">
  <h2>Full Report</h2>

  <div class="a-card">
    <h3>Spend by Category</h3>
    {cat_bars}
  </div>
  <div class="a-card">
    <h3>Spend by Model</h3>
    {model_bars}
  </div>
  <div class="a-card">
    <h3>Cache Health</h3>
    <div class="a-stat-row">
      <div class="a-stat"><div class="val">{c['hit_rate']:.1f}%</div><div class="lbl">Efficiency</div></div>
      <div class="a-stat"><div class="val">{c['current_ttl']}</div><div class="lbl">TTL</div></div>
      <div class="a-stat"><div class="val">{c['total_rebuilds']}</div><div class="lbl">Rebuilds</div></div>
      <div class="a-stat"><div class="val">{c['gaps_5_60']}</div><div class="lbl">Gaps 5-60m</div></div>
      <div class="a-stat"><div class="val">{c['gaps_over_1h']}</div><div class="lbl">Gaps &gt;1h</div></div>
    </div>
    {ttl_note}
  </div>

  <h2>Top Sessions</h2>
  <div class="a-card">
    <table>
      <tr><th>#</th><th>Cost</th><th>Date</th><th>Turns</th><th>Duration</th><th>Model</th><th>Ctx</th></tr>
      {session_rows}
    </table>
  </div>

  {insight}

  <div class="a-footer">
    Generated by Claude Code Usage Advisor &middot; {datetime.now().strftime("%Y-%m-%d %H:%M")}
  </div>
</div>

<script>
(function() {{
  const slides = document.querySelectorAll('.slide');
  const dots = document.querySelectorAll('.dot');
  const counter = document.getElementById('counter');
  const prev = document.getElementById('prev');
  const next = document.getElementById('next');
  let cur = 0;
  const total = slides.length;

  function go(i) {{
    if (i < 0 || i >= total) return;
    slides[cur].classList.remove('active');
    dots[cur].classList.remove('active');
    cur = i;
    slides[cur].classList.add('active');
    dots[cur].classList.add('active');
    prev.disabled = cur === 0;
    next.disabled = cur === total - 1;
    counter.textContent = (cur + 1) + ' / ' + total;
    window.scrollTo(0, 0);
  }}

  prev.addEventListener('click', function() {{ go(cur - 1); }});
  next.addEventListener('click', function() {{ go(cur + 1); }});
  dots.forEach(function(d) {{
    d.addEventListener('click', function() {{ go(+d.dataset.idx); }});
  }});

  document.addEventListener('keydown', function(e) {{
    if (e.key === 'ArrowRight' || e.key === ' ') {{ e.preventDefault(); go(cur + 1); }}
    if (e.key === 'ArrowLeft') {{ e.preventDefault(); go(cur - 1); }}
  }});

  var startX = 0;
  document.addEventListener('touchstart', function(e) {{ startX = e.touches[0].clientX; }});
  document.addEventListener('touchend', function(e) {{
    var dx = e.changedTouches[0].clientX - startX;
    if (Math.abs(dx) > 50) go(dx < 0 ? cur + 1 : cur - 1);
  }});
}})();

function copyCmd(btn) {{
  var code = btn.parentElement.querySelector('code');
  var text = code.textContent || code.innerText;
  navigator.clipboard.writeText(text).then(function() {{
    btn.textContent = 'Copied!';
    setTimeout(function() {{ btn.textContent = 'Copy'; }}, 2000);
  }});
}}
</script>
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
