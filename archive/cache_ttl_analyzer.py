#!/usr/bin/env python3
"""
Claude Code Cache TTL Analyzer
===============================
Parses Claude Code session JSONL logs and compares what you actually paid
vs. what you would have paid under the other TTL strategy.

Usage:
    python cache_ttl_analyzer.py                          # scan default ~/.claude/projects/
    python cache_ttl_analyzer.py /path/to/projects/       # scan a specific directory
    python cache_ttl_analyzer.py session.jsonl             # analyze a single file
    python cache_ttl_analyzer.py --csv                     # output CSV summary
    python cache_ttl_analyzer.py --top 10                  # show top 10 most expensive sessions

Pricing (per million tokens, as of April 2026):
    Opus 4.7/4.6:  base=$5.00  cache_write_5m=$6.25  cache_write_1h=$10.00  cache_read=$0.50  output=$25.00
    Sonnet 4.6:    base=$3.00  cache_write_5m=$3.75  cache_write_1h=$6.00   cache_read=$0.30  output=$15.00
    Haiku 4.5:     base=$1.00  cache_write_5m=$1.25  cache_write_1h=$2.00   cache_read=$0.10  output=$5.00
"""

import json
import os
import sys
import glob
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# ── Pricing table (per million tokens) ──────────────────────────────────────
PRICING = {
    # model substring → { rate_name: $/MTok }
    "opus-4-7": {
        "base_input": 5.00,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
        "output": 25.00,
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
    "sonnet-4-6": {
        "base_input": 3.00,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
        "output": 15.00,
    },
    "sonnet-4-5": {
        "base_input": 3.00,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
        "output": 15.00,
    },
    "haiku-4-5": {
        "base_input": 1.00,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.00,
        "cache_read": 0.10,
        "output": 5.00,
    },
}

# Fallback pricing if model string doesn't match
DEFAULT_PRICING = PRICING["sonnet-4-6"]


def get_pricing(model_str: str) -> dict:
    """Match a model string like 'claude-opus-4-6-20260201' to a pricing tier."""
    if not model_str:
        return DEFAULT_PRICING
    model_lower = model_str.lower()
    for key, rates in PRICING.items():
        if key in model_lower:
            return rates
    return DEFAULT_PRICING


def parse_session(filepath: str) -> dict:
    """Parse a single JSONL session file and extract cache/token metrics."""
    session_id = Path(filepath).stem
    turns = []
    models_seen = set()
    first_ts = None
    last_ts = None

    clear_before_next_turn = False
    clear_turns = []

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Detect /clear commands
            if entry.get("type") == "user":
                content = str(entry.get("message", {}).get("content", ""))
                if "/clear" in content[:500]:
                    clear_before_next_turn = True

            # Only care about assistant messages with usage data
            if entry.get("type") != "assistant":
                continue

            msg = entry.get("message", {})
            usage = msg.get("usage", {})
            if not usage:
                continue

            if clear_before_next_turn:
                clear_turns.append(len(turns))
                clear_before_next_turn = False

            # Track timestamps
            ts_str = entry.get("timestamp")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                except (ValueError, TypeError):
                    ts = None
            else:
                ts = None

            model = msg.get("model", "")
            models_seen.add(model)

            cache_creation = usage.get("cache_creation", {})
            turn_data = {
                "model": model,
                "timestamp": ts,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "ephemeral_5m_input_tokens": cache_creation.get("ephemeral_5m_input_tokens", 0),
                "ephemeral_1h_input_tokens": cache_creation.get("ephemeral_1h_input_tokens", 0),
            }
            turns.append(turn_data)

    if not turns:
        return None

    return {
        "session_id": session_id,
        "filepath": filepath,
        "turns": turns,
        "clear_turns": clear_turns,
        "models": models_seen,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_min": (last_ts - first_ts).total_seconds() / 60 if first_ts and last_ts else 0,
    }


def compute_costs(session: dict) -> dict:
    """
    For each turn, compute:
      - actual_cost: what was actually billed based on the TTL tier used
      - cost_if_5m:  what it would cost if ALL cache writes were 5m
      - cost_if_1h:  what it would cost if ALL cache writes were 1h

    This is the "same miss pattern" comparison — it answers:
    "If I had the same number of misses, what would each TTL cost?"

    The script also estimates "avoided misses" for the counterfactual scenario.
    """
    actual_cost = 0.0
    cost_if_all_5m = 0.0
    cost_if_all_1h = 0.0

    total_5m_write_tokens = 0
    total_1h_write_tokens = 0
    total_cache_read_tokens = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_creation_tokens = 0

    turn_details = []

    for turn in session["turns"]:
        rates = get_pricing(turn["model"])
        mtok = 1_000_000

        inp = turn["input_tokens"]
        out = turn["output_tokens"]
        cr = turn["cache_read_input_tokens"]
        cc = turn["cache_creation_input_tokens"]
        w5m = turn["ephemeral_5m_input_tokens"]
        w1h = turn["ephemeral_1h_input_tokens"]

        total_input_tokens += inp
        total_output_tokens += out
        total_cache_read_tokens += cr
        total_cache_creation_tokens += cc
        total_5m_write_tokens += w5m
        total_1h_write_tokens += w1h

        # Actual cost
        output_cost = (out / mtok) * rates["output"]
        input_cost = (inp / mtok) * rates["base_input"]
        read_cost = (cr / mtok) * rates["cache_read"]
        write_5m_cost = (w5m / mtok) * rates["cache_write_5m"]
        write_1h_cost = (w1h / mtok) * rates["cache_write_1h"]
        turn_actual = output_cost + input_cost + read_cost + write_5m_cost + write_1h_cost

        # Counterfactual: all writes at 5m rate
        turn_if_5m = output_cost + input_cost + read_cost + (cc / mtok) * rates["cache_write_5m"]

        # Counterfactual: all writes at 1h rate
        turn_if_1h = output_cost + input_cost + read_cost + (cc / mtok) * rates["cache_write_1h"]

        actual_cost += turn_actual
        cost_if_all_5m += turn_if_5m
        cost_if_all_1h += turn_if_1h

        turn_details.append({
            "actual": turn_actual,
            "if_5m": turn_if_5m,
            "if_1h": turn_if_1h,
            "is_miss": cc > 0,
            "write_tokens": cc,
            "read_tokens": cr,
            "used_5m": w5m > 0 and w1h == 0,
            "used_1h": w1h > 0 and w5m == 0,
            "timestamp": turn["timestamp"],
        })

    # Count misses and identify gaps
    misses = sum(1 for t in turn_details if t["is_miss"])
    hits = sum(1 for t in turn_details if t["read_tokens"] > 0 and not t["is_miss"])

    # Detect gaps > 5 min between turns (potential avoidable misses with 1h TTL)
    gaps_over_5m = 0
    gaps_over_1h = 0
    avoidable_miss_indices = []
    for i in range(1, len(turn_details)):
        if turn_details[i]["timestamp"] and turn_details[i - 1]["timestamp"]:
            gap = (turn_details[i]["timestamp"] - turn_details[i - 1]["timestamp"]).total_seconds()
            if gap > 300:  # > 5 min
                gaps_over_5m += 1
                if gap <= 3600:  # 5-60 min range: 1h would avoid this miss
                    avoidable_miss_indices.append(i)
            if gap > 3600:  # > 1 hr
                gaps_over_1h += 1

    # Counterfactual: estimate cost if 1h TTL prevented avoidable misses
    # For turns right after a 5-60 min gap, those cache writes would become reads instead
    counterfactual_1h = 0.0
    for i, turn in enumerate(session["turns"]):
        rates = get_pricing(turn["model"])
        mtok = 1_000_000
        inp = turn["input_tokens"]
        out = turn["output_tokens"]
        cr = turn["cache_read_input_tokens"]
        cc = turn["cache_creation_input_tokens"]

        output_cost = (out / mtok) * rates["output"]
        input_cost = (inp / mtok) * rates["base_input"]

        if i in avoidable_miss_indices and cc > 0:
            # This miss would be avoided: write becomes read, at 1h rates for existing writes
            read_cost = ((cr + cc) / mtok) * rates["cache_read"]
            write_cost = 0
        else:
            # Normal turn, but all writes at 1h rate
            read_cost = (cr / mtok) * rates["cache_read"]
            write_cost = (cc / mtok) * rates["cache_write_1h"]

        counterfactual_1h += output_cost + input_cost + read_cost + write_cost

    return {
        "actual_cost": actual_cost,
        "cost_if_all_5m": cost_if_all_5m,
        "cost_if_all_1h": cost_if_all_1h,
        "counterfactual_1h": counterfactual_1h,
        "total_turns": len(turn_details),
        "total_misses": misses,
        "total_hits": hits,
        "total_5m_write_tokens": total_5m_write_tokens,
        "total_1h_write_tokens": total_1h_write_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cache_creation_tokens": total_cache_creation_tokens,
        "gaps_over_5m": gaps_over_5m,
        "gaps_over_1h": gaps_over_1h,
        "avoidable_misses": len(avoidable_miss_indices),
        "turn_details": turn_details,
        "ttl_used": "1h" if total_1h_write_tokens > total_5m_write_tokens else "5m",
    }


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def print_session_report(session: dict, costs: dict):
    """Print a detailed report for a single session."""
    s = session
    c = costs
    print(f"\n{'=' * 70}")
    print(f"  Session: {s['session_id'][:24]}...")
    print(f"  Models:  {', '.join(m for m in s['models'] if m)}")
    if s["first_ts"]:
        print(f"  Time:    {s['first_ts'].strftime('%Y-%m-%d %H:%M')} → {s['last_ts'].strftime('%H:%M')} ({s['duration_min']:.0f} min)")
    print(f"  Turns:   {c['total_turns']}  ({c['total_misses']} misses, {c['total_hits']} hits)")
    print(f"  TTL:     mostly {c['ttl_used']}")
    print(f"{'=' * 70}")

    print(f"\n  Token breakdown:")
    print(f"    Input (uncached):   {format_tokens(c['total_input_tokens']):>10}")
    print(f"    Cache reads:        {format_tokens(c['total_cache_read_tokens']):>10}")
    print(f"    Cache writes (5m):  {format_tokens(c['total_5m_write_tokens']):>10}")
    print(f"    Cache writes (1h):  {format_tokens(c['total_1h_write_tokens']):>10}")
    print(f"    Output:             {format_tokens(c['total_output_tokens']):>10}")

    print(f"\n  Cost comparison (same miss pattern):")
    print(f"    Actual cost:        ${c['actual_cost']:>8.2f}")
    print(f"    If all 5m writes:   ${c['cost_if_all_5m']:>8.2f}")
    print(f"    If all 1h writes:   ${c['cost_if_all_1h']:>8.2f}")

    diff = c['cost_if_all_5m'] - c['cost_if_all_1h']
    if abs(diff) > 0.001:
        cheaper = "5m" if diff < 0 else "1h"
        print(f"    → {cheaper} saves ${abs(diff):.2f} with the same miss pattern")
    else:
        print(f"    → Effectively the same")

    if c["gaps_over_5m"] > 0:
        print(f"\n  Gap analysis:")
        print(f"    Gaps > 5 min:  {c['gaps_over_5m']}  (these cause 5m cache misses)")
        print(f"    Gaps > 1 hr:   {c['gaps_over_1h']}  (these cause 1h cache misses too)")
        avoidable = c["gaps_over_5m"] - c["gaps_over_1h"]
        if avoidable > 0:
            print(f"    Avoidable:     {avoidable}  (misses 1h TTL would prevent)")

    if "counterfactual_1h" in c:
        cf = c["counterfactual_1h"]
        print(f"\n  ┌──────────────────────────────────────────────────┐")
        if c["gaps_over_5m"] > 0:
            print(f"  │  What-if: 1h TTL avoids {avoidable} misses              │")
        print(f"  │  Estimated cost with 1h TTL:  ${cf:>8.2f}           │")
        diff = c["actual_cost"] - cf
        if diff > 0:
            print(f"  │  Estimated savings:            ${diff:>8.2f}           │")
        else:
            print(f"  │  Extra cost:                   ${abs(diff):>8.2f}           │")
        print(f"  └──────────────────────────────────────────────────┘")


def print_aggregate_report(all_sessions: list, all_costs: list):
    """Print an aggregate summary across all sessions."""
    total_actual = sum(c["actual_cost"] for c in all_costs)
    total_if_5m = sum(c["cost_if_all_5m"] for c in all_costs)
    total_if_1h = sum(c["cost_if_all_1h"] for c in all_costs)
    total_turns = sum(c["total_turns"] for c in all_costs)
    total_misses = sum(c["total_misses"] for c in all_costs)
    total_hits = sum(c["total_hits"] for c in all_costs)
    total_gaps_5m = sum(c["gaps_over_5m"] for c in all_costs)
    total_gaps_1h = sum(c["gaps_over_1h"] for c in all_costs)
    total_5m_tokens = sum(c["total_5m_write_tokens"] for c in all_costs)
    total_1h_tokens = sum(c["total_1h_write_tokens"] for c in all_costs)
    total_read_tokens = sum(c["total_cache_read_tokens"] for c in all_costs)
    total_cc_tokens = sum(c["total_cache_creation_tokens"] for c in all_costs)

    hit_rate = (total_read_tokens / (total_read_tokens + total_cc_tokens) * 100) if (total_read_tokens + total_cc_tokens) > 0 else 0

    print(f"\n{'#' * 70}")
    print(f"  AGGREGATE SUMMARY — {len(all_sessions)} sessions")
    print(f"{'#' * 70}")
    print(f"\n  Total turns:       {total_turns}")
    print(f"  Total misses:      {total_misses}")
    print(f"  Total hits:        {total_hits}")
    print(f"  Cache hit rate:    {hit_rate:.1f}%")
    print(f"\n  Cache writes:      5m={format_tokens(total_5m_tokens)}  1h={format_tokens(total_1h_tokens)}")
    print(f"  Cache reads:       {format_tokens(total_read_tokens)}")

    print(f"\n  ┌────────────────────────────────────────┐")
    print(f"  │  Cost comparison (same miss pattern)    │")
    print(f"  ├────────────────────────────────────────┤")
    print(f"  │  Actual total:       ${total_actual:>10.2f}        │")
    print(f"  │  If all 5m writes:   ${total_if_5m:>10.2f}        │")
    print(f"  │  If all 1h writes:   ${total_if_1h:>10.2f}        │")
    print(f"  └────────────────────────────────────────┘")

    diff_5m = total_actual - total_if_5m
    diff_1h = total_actual - total_if_1h
    if abs(diff_5m) > 0.01:
        print(f"\n  Switching to all-5m would {'save' if diff_5m > 0 else 'cost'} ${abs(diff_5m):.2f}")
    if abs(diff_1h) > 0.01:
        print(f"  Switching to all-1h would {'save' if diff_1h > 0 else 'cost'} ${abs(diff_1h):.2f}")

    print(f"\n  Gap analysis (across all sessions):")
    print(f"    Total gaps > 5 min:  {total_gaps_5m}")
    print(f"    Total gaps > 1 hr:   {total_gaps_1h}")
    avoidable = total_gaps_5m - total_gaps_1h
    print(f"    Avoidable by 1h:     {avoidable}")

    # Counterfactual aggregate
    total_counterfactual = sum(c.get("counterfactual_1h", c["actual_cost"]) for c in all_costs)
    total_avoidable = sum(c.get("avoidable_misses", 0) for c in all_costs)
    if total_avoidable > 0:
        cf_diff = total_actual - total_counterfactual
        print(f"\n  ┌────────────────────────────────────────────────────┐")
        print(f"  │  What-if: 1h TTL prevents {total_avoidable} avoidable misses     │")
        print(f"  ├────────────────────────────────────────────────────┤")
        print(f"  │  Current total:              ${total_actual:>10.2f}        │")
        print(f"  │  Estimated with 1h TTL:      ${total_counterfactual:>10.2f}        │")
        if cf_diff > 0:
            print(f"  │  Estimated savings:          ${cf_diff:>10.2f}        │")
        else:
            print(f"  │  Extra cost:                 ${abs(cf_diff):>10.2f}        │")
        print(f"  └────────────────────────────────────────────────────┘")

    if total_misses > 0:
        print(f"\n  ┌────────────────────────────────────────────────────┐")
        print(f"  │  RECOMMENDATION                                    │")
        print(f"  ├────────────────────────────────────────────────────┤")
        cf_savings = total_actual - total_counterfactual if total_avoidable > 0 else 0
        if cf_savings > 0 and total_avoidable > 0:
            pct = (cf_savings / total_actual * 100) if total_actual > 0 else 0
            print(f"  │  → Set ENABLE_PROMPT_CACHING_1H=1                  │")
            print(f"  │    1h TTL would save ~${cf_savings:.2f} ({pct:.0f}%) by preventing  │")
            print(f"  │    {total_avoidable} cache misses from 5-60 min gaps.         │")
        elif total_1h_tokens > total_5m_tokens and cf_savings <= 0:
            print(f"  │  → Consider FORCE_PROMPT_CACHING_5M=1              │")
            print(f"  │    You're on 1h TTL but rarely have 5-60 min gaps. │")
            print(f"  │    The cheaper 1.25x writes would save you money.  │")
        else:
            print(f"  │  → Current TTL setting looks optimal.              │")
            print(f"  │    Your gap pattern matches your TTL tier.         │")
        print(f"  └────────────────────────────────────────────────────┘")


def output_csv(all_sessions: list, all_costs: list):
    """Output session-level CSV for further analysis."""
    print("session_id,date,duration_min,models,turns,misses,hits,ttl_used,"
          "actual_cost,cost_if_5m,cost_if_1h,gaps_over_5m,gaps_over_1h,"
          "cache_read_tokens,cache_write_5m_tokens,cache_write_1h_tokens")
    for s, c in zip(all_sessions, all_costs):
        date = s["first_ts"].strftime("%Y-%m-%d") if s["first_ts"] else "unknown"
        models = "|".join(m for m in s["models"] if m)
        print(f"{s['session_id']},{date},{s['duration_min']:.0f},{models},"
              f"{c['total_turns']},{c['total_misses']},{c['total_hits']},{c['ttl_used']},"
              f"{c['actual_cost']:.4f},{c['cost_if_all_5m']:.4f},{c['cost_if_all_1h']:.4f},"
              f"{c['gaps_over_5m']},{c['gaps_over_1h']},"
              f"{c['total_cache_read_tokens']},{c['total_5m_write_tokens']},{c['total_1h_write_tokens']}")


def output_curves(all_sessions: list, all_costs: list, top_n: int = 15):
    """Output per-turn curve data as JSON for chart visualization."""
    ranked = sorted(range(len(all_costs)),
                    key=lambda i: all_costs[i]["actual_cost"], reverse=True)[:top_n]

    curves = []
    for idx in ranked:
        s = all_sessions[idx]
        c = all_costs[idx]
        turns = s["turns"]
        details = c["turn_details"]

        cumulative = []
        context_size = []
        running = 0.0
        step = max(1, len(turns) // 500)

        for i, (turn, det) in enumerate(zip(turns, details)):
            running += det["actual"]
            if i % step == 0 or i == len(turns) - 1:
                cumulative.append(round(running, 4))
                context_size.append(
                    turn["input_tokens"]
                    + turn["cache_read_input_tokens"]
                    + turn["cache_creation_input_tokens"]
                )

        date_str = s["first_ts"].strftime("%b %d") if s["first_ts"] else "unknown"
        is_opus = any("opus" in m.lower() for m in s["models"] if m)
        clears = [t // step for t in s.get("clear_turns", []) if t > 0]

        curves.append({
            "id": s["session_id"][:24],
            "date": date_str,
            "total": round(c["actual_cost"], 2),
            "num_turns": len(turns),
            "is_opus": is_opus,
            "cumulative": cumulative,
            "context_size": context_size,
            "clear_turns": clears,
            "step": step,
        })

    print(json.dumps(curves, separators=(",", ":")))


def find_session_files(path: str) -> list:
    """Find all JSONL session files under a path."""
    p = Path(path)
    if p.is_file() and p.suffix == ".jsonl":
        return [str(p)]
    if p.is_dir():
        # Recurse into projects directories
        files = list(p.rglob("*.jsonl"))
        # Filter out history.jsonl and other non-session files
        return [str(f) for f in files if f.stem != "history"]
    return []


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Claude Code cache TTL costs from session JSONL logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=os.path.expanduser("~/.claude/projects"),
        help="Path to projects directory or a single .jsonl file (default: ~/.claude/projects/)"
    )
    parser.add_argument("--csv", action="store_true", help="Output CSV instead of text report")
    parser.add_argument("--curves", action="store_true", help="Output per-turn JSON curve data for chart visualization")
    parser.add_argument("--top", type=int, default=5, help="Show top N most expensive sessions (default: 5)")
    parser.add_argument("--all", action="store_true", help="Show details for every session")
    parser.add_argument("--since", type=str, help="Only include sessions after this date (YYYY-MM-DD)")
    args = parser.parse_args()

    # Find files
    files = find_session_files(args.path)
    if not files:
        print(f"No .jsonl files found in {args.path}", file=sys.stderr)
        print(f"\nExpected path: ~/.claude/projects/<project>/sessions/<uuid>.jsonl", file=sys.stderr)
        print(f"Or pass a specific .jsonl file as an argument.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} JSONL files...", file=sys.stderr)

    # Parse all sessions
    all_sessions = []
    all_costs = []
    skipped = 0

    since_date = None
    if args.since:
        since_date = datetime.fromisoformat(args.since).replace(tzinfo=None)

    for filepath in files:
        session = parse_session(filepath)
        if session is None:
            skipped += 1
            continue

        # Date filter
        if since_date and session["first_ts"]:
            ts_naive = session["first_ts"].replace(tzinfo=None)
            if ts_naive < since_date:
                skipped += 1
                continue

        costs = compute_costs(session)
        if costs["total_turns"] == 0:
            skipped += 1
            continue

        all_sessions.append(session)
        all_costs.append(costs)

    if not all_sessions:
        print("No sessions with cache data found.", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzed {len(all_sessions)} sessions ({skipped} skipped)\n", file=sys.stderr)

    # CSV mode
    if args.csv:
        output_csv(all_sessions, all_costs)
        return

    # Curves mode
    if args.curves:
        output_curves(all_sessions, all_costs, top_n=args.top)
        return

    # Text report
    # Show top N expensive sessions
    if not args.all:
        sorted_indices = sorted(range(len(all_costs)), key=lambda i: all_costs[i]["actual_cost"], reverse=True)
        top_n = min(args.top, len(sorted_indices))
        print(f"\n  Top {top_n} most expensive sessions:")
        for idx in sorted_indices[:top_n]:
            print_session_report(all_sessions[idx], all_costs[idx])
    else:
        for s, c in zip(all_sessions, all_costs):
            print_session_report(s, c)

    # Aggregate
    print_aggregate_report(all_sessions, all_costs)


if __name__ == "__main__":
    main()
