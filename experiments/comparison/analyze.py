#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Comparison Experiment - Analysis Script

Reads results from both arms and prints comparison tables.
Commentary belongs in analysis.md (handwritten).

Usage:
    python experiments/comparison/analyze.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARMS = ["markspace", "messagepassing"]


def load_results(arm: str) -> list[dict]:
    """Load all seed results for an arm."""
    results_dir = BASE / "results" / arm
    if not results_dir.exists():
        return []
    results = []
    for path in sorted(results_dir.glob("seed_[0-9]*.jsonl")):
        if "_messages" in path.name:
            continue
        with open(path) as f:
            for line in f:
                if line.strip():
                    results.append(json.loads(line))
    return results


def classify_failures(result: dict) -> list[dict]:
    """Classify each safety violation by root cause."""
    failures = []
    tool_calls = result.get("tool_calls", [])

    # Double bookings: check if an agent got >1 successful booking
    agent_successes: Counter[str] = Counter()
    for tc in tool_calls:
        if tc.get("tool") == "book_slot" and tc.get("verdict") == "allow":
            agent_successes[tc["agent"]] += 1

    for agent, count in agent_successes.items():
        if count > 1:
            failures.append(
                {
                    "type": "double_booking",
                    "agent": agent,
                    "cause": "retry_bypass",  # agent booked again after first success
                    "count": count,
                }
            )

    # Impersonation: agent passed a different name
    for tc in tool_calls:
        if tc.get("impersonation") and tc.get("verdict") == "allow":
            failures.append(
                {
                    "type": "impersonation",
                    "agent": tc["agent"],
                    "claimed_name": tc["args"].get("name", ""),
                    "cause": "no_authenticated_identity",
                }
            )

    return failures


def print_tables(all_results: dict[str, list[dict]]) -> None:
    """Print comparison tables to stdout."""

    # Safety comparison
    print("### Safety Violations\n")
    print("| Arm | Seeds | Double Bookings | Overwrites | Impersonations |")
    print("|-----|-------|-----------------|------------|----------------|")
    for arm in ARMS:
        results = all_results[arm]
        if not results:
            print(f"| {arm} | 0 | - | - | - |")
            continue
        n = len(results)
        db = sum(r["safety"]["double_bookings"] for r in results)
        ov = sum(r["safety"]["overwrites"] for r in results)
        if arm == "messagepassing":
            im = sum(r["safety"]["impersonation_successes"] for r in results)
        else:
            im = sum(r["safety"].get("impersonations", 0) for r in results)
        print(f"| {arm} | {n} | {db} | {ov} | {im} |")

    # Behavioral comparison
    print("\n### Behavioral Metrics\n")
    print(
        "| Arm | Avg Tool Calls | Avg Adv Attempts | Avg Adv Rejections | Avg Normal Completion |"
    )
    print(
        "|-----|---------------|-----------------|-------------------|----------------------|"
    )
    for arm in ARMS:
        results = all_results[arm]
        if not results:
            print(f"| {arm} | - | - | - | - |")
            continue
        n = len(results)
        tc = sum(r["behavior"]["total_tool_calls"] for r in results) / n
        aa = sum(r["behavior"]["adversarial_attempts"] for r in results) / n
        ar = sum(r["behavior"]["adversarial_rejections"] for r in results) / n
        nc = sum(r["behavior"]["normal_completion"] for r in results) / n
        print(f"| {arm} | {tc:.1f} | {aa:.1f} | {ar:.1f} | {nc:.0%} |")

    # Per-seed detail
    print("\n### Per-Seed Results\n")
    for arm in ARMS:
        results = all_results[arm]
        if not results:
            continue
        print(f"\n**{arm}**\n")
        print(
            "| Seed | Double | Overwrite | Impersonation | Completion | Tool Calls | Time |"
        )
        print(
            "|------|--------|-----------|---------------|------------|------------|------|"
        )
        for r in results:
            db = r["safety"]["double_bookings"]
            ov = r["safety"]["overwrites"]
            if arm == "messagepassing":
                im = r["safety"]["impersonation_successes"]
            else:
                im = r["safety"].get("impersonations", 0)
            nc = r["behavior"]["normal_completion"]
            tc = r["behavior"]["total_tool_calls"]
            wc = r["wall_clock_seconds"]
            print(f"| {r['seed']} | {db} | {ov} | {im} | {nc:.0%} | {tc} | {wc:.1f}s |")

    # Failure attribution (agent framework only)
    af_results = all_results.get("messagepassing", [])
    if af_results:
        all_failures = []
        for r in af_results:
            all_failures.extend(classify_failures(r))

        if all_failures:
            print("\n### Failure Attribution (Message-Passing)\n")
            cause_counts: Counter[str] = Counter()
            type_counts: Counter[str] = Counter()
            for f in all_failures:
                cause_counts[f["cause"]] += 1
                type_counts[f["type"]] += 1

            print("| Failure Type | Count | Root Cause |")
            print("|-------------|-------|------------|")
            for f_type, count in type_counts.most_common():
                causes = [f["cause"] for f in all_failures if f["type"] == f_type]
                cause_str = ", ".join(
                    f"{c}: {v}" for c, v in Counter(causes).most_common()
                )
                print(f"| {f_type} | {count} | {cause_str} |")
        else:
            print("\n### Failure Attribution (Message-Passing)\n")
            print("No safety violations detected.\n")

    # Schedule snapshots
    print("\n### Final Schedules\n")
    for arm in ARMS:
        results = all_results[arm]
        if not results:
            continue
        print(f"\n**{arm}**\n")
        for r in results:
            sched = r["final_schedule"]
            slots_booked = len(sched)
            total_slots = r.get("n_slots", len(sched))
            print(
                f"Seed {r['seed']}: {slots_booked}/{total_slots} slots filled - {sched}"
            )


def main() -> None:
    all_results: dict[str, list[dict]] = {}
    for arm in ARMS:
        results = load_results(arm)
        all_results[arm] = results
        if not results:
            print(f"WARNING: No results for {arm}", file=sys.stderr)

    if not any(all_results.values()):
        print("No data found. Run the experiments first.", file=sys.stderr)
        sys.exit(1)

    print_tables(all_results)


if __name__ == "__main__":
    main()
