#!/usr/bin/env python3
"""Verify figures in composition_stress/analysis.md by running the simulation."""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    print("=" * 70)
    print("COMPOSITION STRESS TEST - ANALYSIS VERIFICATION")
    print("=" * 70)

    try:
        from experiments.composition_stress.run import run_stress_test
    except ImportError as e:
        print(f"\nCannot import stress test: {e}")
        print("Install markspace package first: pip install -e .")
        sys.exit(1)

    print("\nRunning simulation (~0.3s)...\n")

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        summary = run_stress_test()
    finally:
        sys.stdout = old_stdout

    # --- Summary table (analysis.md top table) ---
    # Some values are non-deterministic due to thread scheduling in the
    # concurrent pipeline.  Alerts, responses, total marks, and alerter-0
    # post-swap counts vary between runs.  We check those with ranges.
    print("## Summary Table\n")

    exact_checks = [
        ("Agents", "n_agents", 14),
        ("Ticks", "n_ticks", 20),
        ("Hot-swaps", "n_swaps", 4),
        ("Sensor observations", "sensor_observations", 100),
        ("Filtered observations", "filtered_observations", 141),
        ("Aggregated summaries", "aggregated_summaries", 104),
        ("Audit observations", "audit_observations", 18),
        ("Unique mark IDs checked", "unique_mark_ids", 363),
        ("Validation errors", "validation_errors", 0),
    ]
    range_checks = [
        # Non-deterministic due to concurrent thread scheduling.
        # Values land on discrete steps (marks +/-10, alerts +/-2,
        # responses +/-4).  Ranges based on 20-run empirical sweep
        # with one step of margin on each side.
        ("Total marks written", "total_marks_written", 833, 923),
        ("Alerts raised", "alerts_raised", 94, 112),
        ("Responses executed", "responses_executed", 188, 224),
    ]

    all_ok = True
    for label, key, expected in exact_checks:
        actual = summary[key]
        ok = actual == expected
        all_ok &= ok
        print(
            f"  {label}: {actual} (expected {expected}) [{'OK' if ok else 'MISMATCH'}]"
        )

    for label, key, lo, hi in range_checks:
        actual = summary[key]
        ok = lo <= actual <= hi
        all_ok &= ok
        print(
            f"  {label}: {actual} (expected {lo}-{hi}) [{'OK' if ok else 'OUT OF RANGE'}]"
        )

    # Duplicate mark IDs: unique_mark_ids counts distinct IDs across the
    # checked marks (sensors + filters + aggregators + audit).  The total
    # number of checked marks is the sum of those four stage counts.
    checked_marks = (
        summary["sensor_observations"]
        + summary["filtered_observations"]
        + summary["aggregated_summaries"]
        + summary["audit_observations"]
    )
    duplicates = checked_marks - summary["unique_mark_ids"]
    dup_ok = duplicates == 0
    all_ok &= dup_ok
    print(
        f"  Duplicate mark IDs: {duplicates} (expected 0) [{'OK' if dup_ok else 'MISMATCH'}]"
    )

    # --- Swap results (analysis.md Section 3) ---
    print("\n## Swap Results\n")
    exact_swap_checks = [
        ("filter-1 pre  (tick 7, 50->70)", "swap_filter1_pre", 18),
        ("filter-1 post (tick 7, 50->70)", "swap_filter1_post", 13),
        ("filter-0 pre  (tick 10, 50->30)", "swap_filter0_pre", 26),
        ("filter-0 post (tick 10, 50->30)", "swap_filter0_post", 34),
        ("aggregator-0 audit marks (tick 14)", "swap_aggregator0_audit_marks", 18),
    ]
    range_swap_checks = [
        (
            "alerter-0 post alerts (tick 10, 75->60)",
            "swap_alerter0_post_alerts",
            40,
            44,
        ),
    ]

    for label, key, expected in exact_swap_checks:
        actual = summary[key]
        ok = actual == expected
        all_ok &= ok
        print(
            f"  {label}: {actual} (expected {expected}) [{'OK' if ok else 'MISMATCH'}]"
        )

    for label, key, lo, hi in range_swap_checks:
        actual = summary[key]
        ok = lo <= actual <= hi
        all_ok &= ok
        print(
            f"  {label}: {actual} (expected {lo}-{hi}) [{'OK' if ok else 'OUT OF RANGE'}]"
        )

    # --- Stage throughput (analysis.md Section 2.1) ---
    # Values already validated in exact_checks / range_checks above;
    # this section prints the pipeline flow for quick visual inspection.
    print("\n## Stage Throughput\n")
    print(f"  Sensors:     {summary['sensor_observations']}")
    print(f"  Filters:     {summary['filtered_observations']}")
    print(f"  Aggregators: {summary['aggregated_summaries']}")
    print(f"  Audit:       {summary['audit_observations']}")
    print(f"  Alerters:    {summary['alerts_raised']}")
    print(f"  Actors:      {summary['responses_executed']}")

    print(f"\n{'All checks passed.' if all_ok else 'SOME CHECKS FAILED.'}")
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
