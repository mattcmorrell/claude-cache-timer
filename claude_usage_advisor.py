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
# Per-token prices are the same across Opus versions.
# Opus 4.7's tokenizer produces ~30-35% more tokens → effective cost ~2x Sonnet.
# Opus 4.6 tokenizer is more efficient → effective cost ~1.67x Sonnet.
OPUS_47_TOKEN_INFLATION = 1.325  # midpoint of 30-35% more tokens

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

DEFAULT_TIER = "sonnet"


def model_tier(model_str):
    """Return pricing tier key."""
    if not model_str:
        return DEFAULT_TIER
    m = model_str.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return DEFAULT_TIER


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

        by_tier[tier]["cost"] = by_tier[tier].get("cost", 0) + actual
        by_tier[tier]["output_cost"] = by_tier[tier].get("output_cost", 0) + out_c
        by_tier[tier]["turns"] = by_tier[tier].get("turns", 0) + 1

        # Track Opus 4.7 tokenizer cost for version recommendation
        ov = opus_version(t["model"])
        if ov == "4.7":
            by_tier["opus"]["turns_4_7"] = by_tier["opus"].get("turns_4_7", 0) + 1
            by_tier["opus"]["cost_4_7"] = by_tier["opus"].get("cost_4_7", 0) + actual
            context = t["cache_read_tokens"] + t["cache_write_tokens"] + t["input_tokens"]
            by_tier["opus"]["peak_context"] = max(by_tier["opus"].get("peak_context", 0), context)
            if context > 200_000:
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

    # Opus 4.7 → 4.6 analysis (tokenizer efficiency)
    total_cost_4_7 = 0.0
    total_turns_4_7 = 0
    total_turns_4_6_compat = 0
    total_turns_over_200k = 0
    peak_context_all = 0
    for c in costs_list:
        od = c["by_tier"].get("opus", {})
        total_cost_4_7 += od.get("cost_4_7", 0)
        total_turns_4_7 += int(od.get("turns_4_7", 0))
        total_turns_4_6_compat += int(od.get("turns_4_6", 0))
        total_turns_over_200k += int(od.get("turns_over_200k", 0))
        peak_context_all = max(peak_context_all, od.get("peak_context", 0))
    # 4.6 tokenizer would produce fewer tokens → same work costs less
    tokenizer_savings = total_cost_4_7 * (1 - 1 / OPUS_47_TOKEN_INFLATION)

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

    # ── Build cost drivers (top 3 by spend) ─────────────────────────────
    drivers = []
    total_output_tok = sum(c["output_tokens"] for c in costs_list)

    driver_meta = {
        "cache_read": ("Context re-reads", "#3fb950", cat_cache_read),
        "cache_write": ("Cache rebuilds & writes", "#d29922", cat_cache_write),
        "output": ("Output generation", "#79c0ff", cat_output),
        "uncached_input": ("Uncached input", "#8b949e", cat_uncached),
    }

    # Cache reads
    if cat_cache_read > 1:
        desc = (
            f"Every turn re-reads your full conversation context. "
            f"Average peak context: {fmt_tokens(avg_max_context)}."
        )
        if sessions_over_200k > 0:
            desc += f" {sessions_over_200k} sessions exceeded 200K tokens."
        desc += " Larger contexts compound — each new turn re-processes everything."
        drivers.append({"id": "cache_read", "description": desc, "tip": None})

    # Cache writes
    if cat_cache_write > 1:
        desc = (
            f"{total_rebuilds} cache rebuilds over the period"
            f" ({total_mid_rebuilds} mid-session)."
        )
        if total_gaps_5_60 > 0 and current_ttl == "5m":
            desc += f" {total_gaps_5_60} idle gaps (5-60 min) triggered cache expiry."
        tip = None
        if current_ttl == "5m" and ttl_savings > 0 and total_avoidable > 0:
            tip = {
                "text": (
                    f"1h cache TTL would prevent {total_avoidable} avoidable rebuilds, "
                    f"saving ~${ttl_savings:.0f} over this period."
                ),
                "setting": '"CLAUDE_CODE_USE_EXTENDED_CACHE_TTL": "true"',
            }
        elif current_ttl == "5m" and total_avoidable > 0 and ttl_savings <= 0:
            desc += (
                f" 1h TTL would prevent {total_avoidable} of these, but the "
                f"60% write premium exceeds the savings — current 5m is optimal."
            )
        elif current_ttl == "1h" and total_avoidable == 0:
            cf_5m_total = sum(c["cf_5m"] for c in costs_list)
            savings_5m = total_spend - cf_5m_total
            if savings_5m > 1:
                tip = {
                    "text": (
                        f"You rarely have 5-60 min idle gaps. The 5m TTL's cheaper write "
                        f"rate would save ~${savings_5m:.0f} over this period."
                    ),
                    "setting": '"CLAUDE_CODE_USE_EXTENDED_CACHE_TTL": "false"',
                }
        drivers.append({"id": "cache_write", "description": desc, "tip": tip})

    # Output tokens
    if cat_output > 1:
        desc = (
            f"Claude's responses — the most expensive token type at "
            f"$25/MTok on Opus. {fmt_tokens(total_output_tok)} output tokens generated."
        )
        drivers.append({"id": "output", "description": desc, "tip": None})

    # Uncached input (only if > 2% of spend)
    if cat_uncached > total_spend * 0.02:
        desc = "Input tokens not served from cache — typically small."
        drivers.append({"id": "uncached_input", "description": desc, "tip": None})

    # Attach metadata and sort by cost
    for d in drivers:
        title, color, cost = driver_meta[d["id"]]
        d["title"] = title
        d["color"] = color
        d["cost"] = cost
        d["pct"] = cost / total_spend * 100
    drivers.sort(key=lambda d: d["cost"], reverse=True)

    # Tokenizer note (cross-cutting — shown separately if meaningful)
    tokenizer_note = None
    if total_turns_4_7 > 0 and tokenizer_savings > 1:
        monthly_tok_savings = (tokenizer_savings / days) * 30
        tokenizer_note = {
            "turns": total_turns_4_7,
            "cost_4_7": total_cost_4_7,
            "savings": tokenizer_savings,
            "monthly_savings": monthly_tok_savings,
            "pct_of_spend": tokenizer_savings / total_spend * 100,
            "description": (
                f"You have {total_turns_4_7} turns on Opus 4.7, whose tokenizer "
                f"produces ~30-35% more tokens than 4.6 for the same content. "
                f"Switching to 4.6 would reduce token counts by ~25%, "
                f"saving ~${tokenizer_savings:.0f} "
                f"(~${monthly_tok_savings:.0f}/mo)."
            ),
            "setting": "claude --model claude-opus-4-6",
        }

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
            "cost_4_7": total_cost_4_7,
            "tokenizer_savings": tokenizer_savings,
        },
        "drivers": drivers[:3],
        "tokenizer_note": tokenizer_note,
    }


# ── Team insights ────────────────────────────────────────────────────────────

def compute_team_insights(sessions, costs_list):
    """Compute plain-English insights for the team-facing dashboard."""
    total_turns = 0
    rebuild_turns = 0
    rebuild_cost = 0.0
    normal_turns = 0
    normal_cost = 0.0
    gap_rebuilds = []

    for s, c in zip(sessions, costs_list):
        tcs = c["turn_costs"]
        for i, tc in enumerate(tcs):
            total_turns += 1
            if tc["is_rebuild"] and i > 0:
                rebuild_turns += 1
                rebuild_cost += tc["actual"]
                t1 = tc["timestamp"]
                t0 = tcs[i - 1]["timestamp"]
                if t1 and t0:
                    gap_s = (t1 - t0).total_seconds()
                    gap_rebuilds.append({"gap_s": gap_s, "cost": tc["actual"]})
            else:
                normal_turns += 1
                normal_cost += tc["actual"]

    total_cost = rebuild_cost + normal_cost
    avg_normal = normal_cost / max(1, normal_turns)
    avg_rebuild = rebuild_cost / max(1, rebuild_turns)
    multiplier = avg_rebuild / avg_normal if avg_normal > 0.001 else 0

    gaps_5_60 = [g for g in gap_rebuilds if 300 < g["gap_s"] <= 3600]
    gaps_over_1h = [g for g in gap_rebuilds if g["gap_s"] > 3600]

    small, medium, large = [], [], []
    for s, c in zip(sessions, costs_list):
        ctx = c["max_context_tokens"]
        cpt = c["actual"] / max(1, c["turns"])
        if ctx < 100_000:
            small.append(cpt)
        elif ctx < 200_000:
            medium.append(cpt)
        else:
            large.append(cpt)

    def avg_or_zero(lst):
        return sum(lst) / len(lst) if lst else 0

    small_cpt = avg_or_zero(small)
    medium_cpt = avg_or_zero(medium)
    large_cpt = avg_or_zero(large)
    baseline = small_cpt or medium_cpt or 0.001
    size_mult = large_cpt / baseline if large_cpt and baseline > 0.001 else 0

    return {
        "total_turns": total_turns,
        "total_cost": total_cost,
        "rebuild_turns": rebuild_turns,
        "rebuild_pct_turns": (rebuild_turns / max(1, total_turns)) * 100,
        "rebuild_cost": rebuild_cost,
        "rebuild_pct_cost": (rebuild_cost / max(0.001, total_cost)) * 100,
        "normal_avg_cost": avg_normal,
        "rebuild_avg_cost": avg_rebuild,
        "rebuild_multiplier": multiplier,
        "gaps_5_60": gaps_5_60,
        "gaps_over_1h": gaps_over_1h,
        "size_small": {"count": len(small), "avg_cpt": small_cpt},
        "size_medium": {"count": len(medium), "avg_cpt": medium_cpt},
        "size_large": {"count": len(large), "avg_cpt": large_cpt},
        "size_multiplier": size_mult,
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

    # ── Top cost drivers
    drivers = r["drivers"]
    print(f"\n  TOP COST DRIVERS")
    print(f"  {'─' * 50}")
    for i, d in enumerate(drivers, 1):
        print(f"  [{i}] {d['title']:<34} ${d['cost']:>8.2f}  ({d['pct']:.0f}%)")
        desc = d["description"]
        while len(desc) > 62:
            sp = desc[:62].rfind(" ")
            if sp == -1:
                sp = 62
            print(f"      {desc[:sp]}")
            desc = desc[sp:].strip()
        if desc:
            print(f"      {desc}")
        if d.get("tip"):
            print(f"      TIP: {d['tip']['text']}")
        print()

    tn = r.get("tokenizer_note")
    if tn:
        print(f"  NOTE: {tn['description']}")
        print(f"        Setting: {tn['setting']}")
        print()

    print(f"\n  Run with --html > report.html for a shareable version.")
    print()


# ── HTML report (slideshow) ──────────────────────────────────────────────────

def _driver_visual(driver):
    """Build a visual element for a cost driver slide."""
    pct = driver["pct"]
    color = driver["color"]
    return f"""
        <div class="visual driver-bar">
          <div class="driver-track"><div class="driver-fill" style="width:{pct:.1f}%;background:{color}"></div></div>
          <div class="driver-cost-line">${driver['cost']:.0f} of total spend</div>
        </div>"""


def generate_html(report):
    r = report
    p = r["period"]
    o = r["overview"]
    c = r["cache"]
    drivers = r["drivers"]
    tn = r.get("tokenizer_note")

    start_str = p["start"].strftime("%b %d") if p["start"] else "?"
    end_str = p["end"].strftime("%b %d, %Y") if p["end"] else "?"

    # ── Build slides ─────────────────────────────────────────────────────
    slides = []

    # Slide 0: Summary — top 3 cost drivers at a glance
    driver_bullets = ""
    for i, d in enumerate(drivers, 1):
        driver_bullets += (
            f'<div class="summary-driver">'
            f'<span class="sd-num">{i}.</span>'
            f'<span class="sd-title">{d["title"]}</span>'
            f'<span class="sd-pct" style="color:{d["color"]}">{d["pct"]:.0f}%</span>'
            f'</div>'
        )

    has_tips = any(d.get("tip") for d in drivers) or tn
    tip_line = '<div class="summary-sub">Settings tweaks available — see details.</div>' if has_tips else '<div class="summary-sub">Your settings look well-tuned for how you work.</div>'

    slides.append(f"""
      <div class="slide-inner summary">
        <div class="summary-q">Your top cost drivers</div>
        <div class="summary-drivers">{driver_bullets}</div>
        {tip_line}
      </div>""")

    # Slide 1: Spend overview
    slides.append(f"""
      <div class="slide-inner cover">
        <div class="cover-label">Your Usage</div>
        <div class="big-number">${o['total_spend']:.0f}</div>
        <div class="cover-period">{start_str} &ndash; {end_str}</div>
        <div class="cover-meta">{o['session_count']} sessions &middot; {p['days']} days &middot; ${o['daily_avg']:.0f}/day avg</div>
      </div>""")

    # Slides 2-4: Top 3 cost drivers
    for d in drivers:
        visual = _driver_visual(d)
        tip_html = ""
        if d.get("tip"):
            tip_html = f"""
            <div class="tip-block">
              <div class="tip-label">Tip</div>
              <div class="tip-text">{d['tip']['text']}</div>
              <code>{d['tip']['setting']}</code>
            </div>"""

        slides.append(f"""
      <div class="slide-inner driver">
        <div class="driver-pct" style="color:{d['color']}">{d['pct']:.0f}%</div>
        <h2>{d['title']}</h2>
        {visual}
        <p class="driver-detail">{d['description']}</p>
        {tip_html}
      </div>""")

    # Tokenizer note slide (if present)
    if tn:
        pct_46 = int(100 / OPUS_47_TOKEN_INFLATION)
        slides.append(f"""
      <div class="slide-inner driver">
        <div class="driver-pct" style="color:#bc8cff">{tn['pct_of_spend']:.1f}%</div>
        <h2>Opus 4.7 tokenizer overhead</h2>
        <div class="visual comparison">
          <div class="comp-row">
            <div class="comp-label">4.7 tokens</div>
            <div class="comp-track"><div class="comp-fill" style="width:100%;background:#bc8cff"></div></div>
            <div class="comp-value">~135</div>
          </div>
          <div class="comp-row">
            <div class="comp-label">4.6 tokens</div>
            <div class="comp-track"><div class="comp-fill" style="width:{pct_46}%;background:#79c0ff"></div></div>
            <div class="comp-value">~100</div>
          </div>
          <div class="comp-diff">same content, same price per token</div>
        </div>
        <p class="driver-detail">{tn['description']}</p>
        <div class="tip-block">
          <div class="tip-label">Setting</div>
          <code>{tn['setting']}</code>
        </div>
      </div>""")

    # Final slide: see full report
    slides.append(f"""
      <div class="slide-inner action">
        <h2>Full Report</h2>
        <div class="all-good-card">
          <div class="ag-text">Scroll down or click below for the detailed breakdown.</div>
        </div>
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

    # Build tokenizer note for appendix
    tn_html = ""
    if tn:
        tn_html = f'<div class="a-card"><h3>Opus 4.7 Tokenizer</h3><p>{tn["description"]}</p></div>'

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
    font-size: 28px; font-weight: 700; color: #f0f6fc;
    margin-bottom: 36px;
  }}
  .summary-drivers {{
    text-align: left; max-width: 420px; margin: 0 auto 32px;
  }}
  .summary-driver {{
    display: flex; align-items: baseline; gap: 10px;
    padding: 12px 0; border-bottom: 1px solid #21262d;
    font-size: 18px;
  }}
  .sd-num {{ color: #484f58; font-weight: 600; min-width: 28px; }}
  .sd-title {{ flex: 1; color: #c9d1d9; }}
  .sd-pct {{ font-size: 24px; font-weight: 800; }}
  .summary-sub {{
    font-size: 15px; color: #484f58;
  }}

  /* ── Driver slides ── */
  .driver .driver-pct {{
    font-size: 64px; font-weight: 800;
    line-height: 1; margin-bottom: 4px;
  }}
  .driver h2 {{
    font-size: 24px; font-weight: 600; color: #f0f6fc;
    margin: 0 0 20px;
  }}
  .driver .driver-detail {{
    font-size: 15px; color: #8b949e;
    margin: 16px 0 24px; text-align: left; line-height: 1.7;
  }}
  .driver-bar {{ margin: 16px 0; }}
  .driver-track {{
    height: 40px; background: #21262d; border-radius: 8px; overflow: hidden;
  }}
  .driver-fill {{
    height: 100%; border-radius: 8px;
    transition: width 0.8s cubic-bezier(.4,0,.2,1) 0.15s;
  }}
  .slide:not(.active) .driver-fill {{ width: 0 !important; }}
  .driver-cost-line {{
    font-size: 14px; color: #8b949e; margin-top: 6px; text-align: right;
  }}

  /* Tip block */
  .tip-block {{
    background: #0d1d31; border: 1px solid #1f6feb; border-radius: 10px;
    padding: 16px 20px; text-align: left; margin-top: 8px;
  }}
  .tip-label {{
    font-size: 13px; font-weight: 600; color: #58a6ff;
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px;
  }}
  .tip-text {{ font-size: 14px; color: #c9d1d9; margin-bottom: 8px; line-height: 1.5; }}
  .tip-block code {{ color: #79c0ff; font-size: 14px; }}

  /* Comparison bars (tokenizer slide) */
  .comparison {{ margin: 24px 0; }}
  .comp-row {{
    display: grid; grid-template-columns: 120px 1fr 80px;
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
    text-align: center; font-size: 14px; color: #8b949e;
    margin-top: 4px;
  }}

  /* ── Final slide ── */
  .action h2 {{
    font-size: 28px; font-weight: 600; color: #f0f6fc; margin: 0 0 32px;
  }}
  .all-good-card {{
    background: #0d1d31; border: 1px solid #1f6feb; border-radius: 10px;
    padding: 32px; text-align: center;
  }}
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
    .driver .driver-pct {{ font-size: 48px; }}
    .comp-row {{ grid-template-columns: 100px 1fr 64px; }}
    .summary-driver {{ font-size: 16px; }}
    .sd-pct {{ font-size: 20px; }}
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
  </div>

  <h2>Top Sessions</h2>
  <div class="a-card">
    <table>
      <tr><th>#</th><th>Cost</th><th>Date</th><th>Turns</th><th>Duration</th><th>Model</th><th>Ctx</th></tr>
      {session_rows}
    </table>
  </div>

  {tn_html}

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
</script>
</body>
</html>"""


# ── Team HTML report ─────────────────────────────────────────────────────────

def generate_team_html(team, report):
    """Generate a team-friendly HTML insights report."""
    t = team
    r = report
    p = r["period"]
    o = r["overview"]

    start_str = p["start"].strftime("%b %d") if p["start"] else "?"
    end_str = p["end"].strftime("%b %d, %Y") if p["end"] else "?"

    # ── Cache miss card ──────────────────────────────────────────────────
    if t["rebuild_turns"] == 0:
        miss_card = """
        <div class="good">
          <div class="good-icon">&check;</div>
          <div class="good-text">No mid-session cache misses detected. Your cache efficiency is excellent.</div>
        </div>"""
    else:
        miss_pct_turns = t["rebuild_pct_turns"]
        miss_pct_cost = t["rebuild_pct_cost"]
        miss_card = f"""
        <div class="hero-pair">
          <div class="hero-item">
            <div class="hero-num">{miss_pct_turns:.0f}%</div>
            <div class="hero-desc">of your turns</div>
          </div>
          <div class="hero-arrow">&rarr;</div>
          <div class="hero-item">
            <div class="hero-num accent-red">{miss_pct_cost:.0f}%</div>
            <div class="hero-desc">of your budget</div>
          </div>
        </div>
        <div class="bar-compare">
          <div class="bar-row">
            <span class="bar-label">Turns</span>
            <div class="bar-track"><div class="bar-fill neutral" style="width:{max(2, miss_pct_turns):.1f}%"></div></div>
            <span class="bar-pct">{miss_pct_turns:.0f}%</span>
          </div>
          <div class="bar-row">
            <span class="bar-label">Cost</span>
            <div class="bar-track"><div class="bar-fill hot" style="width:{max(2, miss_pct_cost):.1f}%"></div></div>
            <span class="bar-pct">{miss_pct_cost:.0f}%</span>
          </div>
        </div>
        <div class="miss-stats">
          {t["rebuild_turns"]} misses &middot; ${t["rebuild_cost"]:.0f} total &middot;
          <strong>{t["rebuild_multiplier"]:.0f}&times;</strong> normal turn cost
        </div>
        <p class="card-text">
          When you step away for 5+ minutes during a session, the prompt cache expires.
          Coming back means rebuilding it from scratch &mdash; and the bigger the session,
          the more expensive the reload.
        </p>"""

    # ── Idle gap card ────────────────────────────────────────────────────
    total_gaps = len(t["gaps_5_60"]) + len(t["gaps_over_1h"])
    total_gap_cost = (sum(g["cost"] for g in t["gaps_5_60"])
                      + sum(g["cost"] for g in t["gaps_over_1h"]))

    if total_gaps == 0:
        gap_card = """
        <div class="good">
          <div class="good-icon">&check;</div>
          <div class="good-text">Your work rhythm is cache-friendly. No expensive idle gaps detected.</div>
        </div>"""
    else:
        gap_hero = f"""
        <div class="hero-single">
          <span class="hero-num">{total_gaps}</span>
          <span class="hero-desc">idle break{"s" if total_gaps != 1 else ""} cost you <strong>${total_gap_cost:.0f}</strong></span>
        </div>"""

        gap_buckets = ""
        if t["gaps_5_60"]:
            cost_5_60 = sum(g["cost"] for g in t["gaps_5_60"])
            gap_buckets += f"""
            <div class="gap-bucket">
              <div class="gap-count">{len(t["gaps_5_60"])}</div>
              <div class="gap-detail">
                <div class="gap-range">break{"s" if len(t["gaps_5_60"]) != 1 else ""} between 5&ndash;60 min &mdash; ${cost_5_60:.0f}</div>
                <div class="gap-fix">Fix: Switch to 1-hour cache TTL. These breaks won&rsquo;t expire the cache.</div>
              </div>
            </div>"""
        if t["gaps_over_1h"]:
            cost_1h = sum(g["cost"] for g in t["gaps_over_1h"])
            gap_buckets += f"""
            <div class="gap-bucket">
              <div class="gap-count">{len(t["gaps_over_1h"])}</div>
              <div class="gap-detail">
                <div class="gap-range">break{"s" if len(t["gaps_over_1h"]) != 1 else ""} over 1 hour &mdash; ${cost_1h:.0f}</div>
                <div class="gap-fix">Fix: <code>/clear</code> before stepping away. Fresh start is cheaper than reloading.</div>
              </div>
            </div>"""

        gap_card = f"""{gap_hero}
        <div class="gap-buckets">{gap_buckets}</div>"""

    # ── Session size card ────────────────────────────────────────────────
    buckets = [
        ("&lt; 100K", t["size_small"]),
        ("100&ndash;200K", t["size_medium"]),
        ("&gt; 200K", t["size_large"]),
    ]
    nonempty = [(label, b) for label, b in buckets if b["count"] > 0]

    if t["size_multiplier"] > 1.5 and len(nonempty) > 1:
        size_hero = f"""
        <div class="hero-single">
          <span class="hero-num">{t["size_multiplier"]:.1f}&times;</span>
          <span class="hero-desc">more expensive per turn in your largest sessions</span>
        </div>"""
    else:
        size_hero = """
        <div class="hero-single">
          <span class="hero-desc" style="font-size:18px;color:#c9d1d9">Cost per turn by session size</span>
        </div>"""

    max_cpt = max((b["avg_cpt"] for _, b in nonempty), default=0.01)
    size_bars = ""
    for label, b in buckets:
        if b["count"] == 0:
            continue
        pct = (b["avg_cpt"] / max_cpt) * 100 if max_cpt > 0 else 0
        size_bars += f"""
        <div class="size-row">
          <span class="size-label">{label}</span>
          <div class="size-track"><div class="size-fill" style="width:{pct:.0f}%"></div></div>
          <span class="size-value">${b["avg_cpt"]:.2f}/turn</span>
          <span class="size-count">{b["count"]} session{"s" if b["count"] != 1 else ""}</span>
        </div>"""

    size_card = f"""{size_hero}
    <div class="size-bars">{size_bars}</div>
    <p class="card-text">
      Every turn re-reads your full conversation context. Bigger context = higher per-turn cost.
      Use <code>/clear</code> at natural breakpoints to keep sessions lean.
    </p>"""

    # ── Recommendation card ──────────────────────────────────────────────
    recs = r.get("recommendations", [])
    if recs:
        top_rec = recs[0]
        sav = f"~${top_rec['savings_monthly']:.0f}/mo" if top_rec.get("savings_monthly") else ""
        setting_html = top_rec["setting"].replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        rec_card = f"""
        <div class="rec-priority">#1 PRIORITY{f" &middot; saves {sav}" if sav else ""}</div>
        <h3 class="rec-title">{top_rec["title"]}</h3>
        <p class="rec-detail">{top_rec["detail"]}</p>
        <div class="setting-block">
          <code>{setting_html}</code>
          <button class="copy-btn" onclick="copyCmd(this)">Copy</button>
        </div>"""
        if len(recs) > 1:
            also_items = ""
            for rec in recs[1:]:
                also_sav = f" (~${rec['savings_monthly']:.0f}/mo)" if rec.get("savings_monthly") else ""
                also_items += f'<li><strong>{rec["title"]}</strong>{also_sav}</li>'
            rec_card += f"""
        <div class="also-consider">
          <div class="also-label">Also consider</div>
          <ul>{also_items}</ul>
        </div>"""
    else:
        rec_card = """
        <div class="good">
          <div class="good-icon">&check;</div>
          <div class="good-text">
            Your settings match your usage patterns. No changes needed &mdash; keep doing what you&rsquo;re doing.
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Usage Report &mdash; {start_str} &ndash; {end_str}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    background: #0d1117; color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 15px; line-height: 1.6;
  }}
  .container {{
    max-width: 700px; margin: 0 auto;
    padding: 48px 24px 80px;
  }}
  header {{
    text-align: center; margin-bottom: 48px;
    padding-bottom: 32px; border-bottom: 1px solid #21262d;
  }}
  header h1 {{
    font-size: 28px; font-weight: 700; color: #f0f6fc;
    margin: 0 0 8px;
  }}
  .period {{ font-size: 16px; color: #8b949e; margin-bottom: 4px; }}
  .summary-line {{ font-size: 14px; color: #656d76; }}

  /* Cards */
  .card {{
    background: #161b22; border: 1px solid #30363d;
    border-radius: 12px; padding: 32px;
    margin-bottom: 24px; border-left: 4px solid #30363d;
  }}
  .card.miss {{ border-left-color: #f85149; }}
  .card.gaps {{ border-left-color: #d29922; }}
  .card.size {{ border-left-color: #58a6ff; }}
  .card.rec {{ border-left-color: #3fb950; }}
  .card-label {{
    font-size: 12px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 2px;
    color: #8b949e; margin-bottom: 20px;
  }}
  .card-text {{
    font-size: 14px; color: #9ca3af; line-height: 1.7; margin: 16px 0 0;
  }}
  .card-text code {{
    background: #21262d; padding: 2px 6px; border-radius: 4px;
    font-size: 13px; color: #79c0ff;
  }}

  /* Good state */
  .good {{
    display: flex; align-items: center; gap: 16px; padding: 8px 0;
  }}
  .good-icon {{
    font-size: 28px; color: #3fb950; font-weight: 700;
    min-width: 36px; text-align: center;
  }}
  .good-text {{ font-size: 15px; color: #9ca3af; }}

  /* Hero pair (cache miss) */
  .hero-pair {{
    display: flex; align-items: center; justify-content: center;
    gap: 24px; margin-bottom: 24px;
  }}
  .hero-item {{ text-align: center; }}
  .hero-num {{
    font-size: 48px; font-weight: 800; color: #f0f6fc; line-height: 1;
  }}
  .hero-num.accent-red {{ color: #f85149; }}
  .hero-desc {{ font-size: 14px; color: #8b949e; margin-top: 4px; }}
  .hero-arrow {{ font-size: 28px; color: #484f58; margin-top: -20px; }}

  /* Bar comparison */
  .bar-compare {{ margin: 20px 0; }}
  .bar-row {{
    display: grid; grid-template-columns: 50px 1fr 44px;
    align-items: center; gap: 12px; margin-bottom: 8px;
  }}
  .bar-label {{ font-size: 13px; color: #8b949e; text-align: right; }}
  .bar-track {{
    height: 24px; background: #21262d; border-radius: 6px; overflow: hidden;
  }}
  .bar-fill {{
    height: 100%; border-radius: 6px; min-width: 4px;
    animation: grow 0.8s cubic-bezier(.4,0,.2,1) forwards;
  }}
  .bar-fill.neutral {{ background: #484f58; }}
  .bar-fill.hot {{ background: #f85149; }}
  @keyframes grow {{ from {{ width: 0; }} }}
  .bar-pct {{ font-size: 14px; color: #e6edf3; font-weight: 600; }}
  .miss-stats {{
    font-size: 15px; color: #c9d1d9; text-align: center; margin: 16px 0;
  }}
  .miss-stats strong {{ color: #f85149; }}

  /* Hero single */
  .hero-single {{ margin-bottom: 20px; }}
  .hero-single .hero-num {{
    font-size: 48px; font-weight: 800; color: #f0f6fc;
    line-height: 1; display: inline;
  }}
  .hero-single .hero-desc {{
    font-size: 20px; color: #9ca3af; display: inline; margin-left: 12px;
  }}
  .hero-single .hero-desc strong {{ color: #f0f6fc; }}

  /* Gap buckets */
  .gap-buckets {{ margin: 16px 0; }}
  .gap-bucket {{
    display: flex; gap: 16px; align-items: flex-start;
    padding: 16px 0; border-bottom: 1px solid #21262d;
  }}
  .gap-bucket:last-child {{ border-bottom: none; }}
  .gap-count {{
    font-size: 28px; font-weight: 800; color: #f0f6fc;
    min-width: 48px; text-align: center; line-height: 1; padding-top: 2px;
  }}
  .gap-detail {{ flex: 1; }}
  .gap-range {{ font-size: 15px; color: #c9d1d9; margin-bottom: 4px; }}
  .gap-fix {{ font-size: 14px; color: #3fb950; font-weight: 500; }}
  .gap-fix code {{
    background: #21262d; padding: 2px 6px; border-radius: 4px;
    font-size: 13px; color: #79c0ff;
  }}

  /* Size bars */
  .size-bars {{ margin: 16px 0; }}
  .size-row {{
    display: grid; grid-template-columns: 80px 1fr 90px 80px;
    align-items: center; gap: 12px; margin-bottom: 8px;
  }}
  .size-label {{ font-size: 13px; color: #8b949e; text-align: right; }}
  .size-track {{
    height: 20px; background: #21262d; border-radius: 5px; overflow: hidden;
  }}
  .size-fill {{
    height: 100%; background: #58a6ff; border-radius: 5px; min-width: 4px;
    animation: grow 0.8s cubic-bezier(.4,0,.2,1) forwards;
  }}
  .size-value {{ font-size: 14px; color: #e6edf3; font-weight: 600; }}
  .size-count {{ font-size: 13px; color: #656d76; }}

  /* Recommendation */
  .rec-priority {{
    font-size: 12px; font-weight: 700; color: #3fb950;
    letter-spacing: 2px; margin-bottom: 8px;
  }}
  .rec-title {{
    font-size: 22px; font-weight: 700; color: #f0f6fc; margin: 0 0 16px;
  }}
  .rec-detail {{
    font-size: 14px; color: #9ca3af; line-height: 1.7; margin: 0 0 20px;
  }}
  .setting-block {{
    background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px 20px; position: relative;
  }}
  .setting-block code {{
    color: #79c0ff; font-size: 13px; white-space: pre-wrap; line-height: 1.8;
  }}
  .copy-btn {{
    position: absolute; top: 10px; right: 10px;
    background: #21262d; border: 1px solid #30363d; color: #8b949e;
    border-radius: 6px; padding: 4px 12px; font-size: 12px; cursor: pointer;
  }}
  .copy-btn:hover {{ color: #e6edf3; border-color: #58a6ff; }}
  .also-consider {{
    margin-top: 20px; padding-top: 16px; border-top: 1px solid #21262d;
  }}
  .also-label {{
    font-size: 12px; font-weight: 600; color: #8b949e;
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px;
  }}
  .also-consider ul {{ margin: 0; padding-left: 20px; }}
  .also-consider li {{
    font-size: 14px; color: #9ca3af; margin-bottom: 6px; line-height: 1.5;
  }}
  .also-consider li strong {{ color: #c9d1d9; }}

  /* Explainer */
  details {{ margin-top: 32px; }}
  details summary {{
    font-size: 14px; color: #8b949e; cursor: pointer; padding: 8px 0;
  }}
  details summary:hover {{ color: #c9d1d9; }}
  .explainer {{
    font-size: 14px; color: #656d76; line-height: 1.7; padding: 16px 0;
  }}
  .explainer p {{ margin: 0 0 12px; }}
  .explainer strong {{ color: #8b949e; }}
  .explainer code {{
    background: #21262d; padding: 2px 6px; border-radius: 4px;
    font-size: 13px; color: #79c0ff;
  }}

  .footer {{
    text-align: center; color: #484f58; font-size: 13px;
    margin-top: 40px; padding-top: 16px; border-top: 1px solid #21262d;
  }}

  @media (max-width: 640px) {{
    .hero-num {{ font-size: 36px; }}
    .hero-pair {{ gap: 16px; }}
    .size-row {{ grid-template-columns: 70px 1fr 80px; }}
    .size-count {{ display: none; }}
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Your Claude Usage Report</h1>
    <div class="period">{start_str} &ndash; {end_str}</div>
    <div class="summary-line">${o["total_spend"]:.0f} total &middot; {o["session_count"]} sessions &middot; ${o["daily_avg"]:.0f}/day avg</div>
  </header>

  <div class="card miss">
    <div class="card-label">Cache Miss Tax</div>
    {miss_card}
  </div>

  <div class="card gaps">
    <div class="card-label">Idle Gap Penalty</div>
    {gap_card}
  </div>

  <div class="card size">
    <div class="card-label">Session Size Impact</div>
    {size_card}
  </div>

  <div class="card rec">
    <div class="card-label">Recommendation</div>
    {rec_card}
  </div>

  <details>
    <summary>How does this work?</summary>
    <div class="explainer">
      <p><strong>Prompt caching:</strong> Claude Code caches your conversation context so it doesn&rsquo;t
      need to re-process it every turn. This cache has a time-to-live (TTL) &mdash; 5 minutes by default,
      or 1 hour if you enable extended caching.</p>
      <p><strong>Cache miss:</strong> When the cache expires (you were idle too long), Claude has to
      rebuild the entire cache from scratch. This is expensive &mdash; writing tokens to cache costs
      12.5&times; more than reading from it.</p>
      <p><strong>Why big sessions cost more:</strong> Every turn reads your full conversation context.
      A session at 200K tokens reads 200K tokens per turn. At $0.50/MTok, that&rsquo;s $0.10 just
      in cache reads &mdash; before any output.</p>
      <p><strong>The 5-minute rule:</strong> If you&rsquo;re in a big session and need a break, either
      switch to 1-hour TTL so the cache survives longer breaks, or run <code>/clear</code> before
      stepping away so you don&rsquo;t pay to reload a huge context.</p>
    </div>
  </details>

  <div class="footer">
    Generated by Claude Code Usage Advisor &middot; {datetime.now().strftime("%Y-%m-%d %H:%M")}
  </div>
</div>

<script>
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
    parser.add_argument("--html", action="store_true", help="Output self-contained HTML report (slideshow)")
    parser.add_argument("--team", action="store_true", help="Output team-friendly HTML insights report")
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
    elif args.team:
        team = compute_team_insights(sessions, costs_list)
        print(generate_team_html(team, report))
    elif args.html:
        print(generate_html(report))
    else:
        print_text_report(report)


if __name__ == "__main__":
    main()
