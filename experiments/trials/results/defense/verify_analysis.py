#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Defense-in-Depth Trial Data Extraction

Reads rounds.jsonl and trial.jsonl ground truth, computes metrics,
and prints tables to stdout. Commentary belongs in analysis.md (handwritten).

Usage:
    python experiments/trials/results/defense/verify_analysis.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
SCENARIOS = [
    "rate_spike",
    "type_shift",
    "escalation",
    "probe_evasion",
    "slow_drift",
    "defense_combined",
]
MODEL = "gpt-oss-120b"
N_ADVERSARIAL = 5
N_NORMAL = 100  # from manifests.jsonl: 105 total - 5 adv


def load_rounds(scenario: str) -> list[dict]:
    path = BASE / MODEL / scenario / "rounds.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in open(path)]


def load_trial(scenario: str) -> dict | None:
    path = BASE / MODEL / scenario / "trial.jsonl"
    if not path.exists():
        return None
    return json.loads(open(path).readline())


def analyze_scenario(scenario: str) -> dict:
    """Extract all metrics for one scenario from ground truth JSONL."""
    rounds = load_rounds(scenario)
    trial = load_trial(scenario)
    if not rounds or trial is None:
        return {}

    n_rounds = len(rounds)
    total_agents = rounds[0].get("active_agents", 0)

    doubles = sum(r.get("double_bookings", 0) for r in rounds)
    scope_viol = sum(r.get("scope_violations", 0) for r in rounds)

    probe_rounds = []
    all_probe_verdicts: Counter[str] = Counter()
    adv_probe_verdicts: Counter[str] = Counter()
    canary_visible_all = True
    for r in rounds:
        d = r.get("defense", {})
        pr = d.get("probe_results", [])
        if pr:
            probe_rounds.append(r["round_num"])
            if not d.get("canary_visible", False):
                canary_visible_all = False
            for p in pr:
                all_probe_verdicts[p["verdict"]] += 1
                if "adv-" in p["agent"]:
                    adv_probe_verdicts[p["verdict"]] += 1

    last_d = rounds[-1].get("defense", {})
    restricted_agents = last_d.get("envelope_restricted_agents", [])
    adv_restricted = [a for a in restricted_agents if "adv-" in a]
    normal_restricted = [a for a in restricted_agents if "adv-" not in a]
    fp_rate = len(normal_restricted) / N_NORMAL if N_NORMAL > 0 else 0.0
    tp_rate = len(adv_restricted) / N_ADVERSARIAL

    first_restricted_round = None
    for r in rounds:
        d = r.get("defense", {})
        if d.get("envelope_restricted_count", 0) > 0:
            first_restricted_round = r["round_num"]
            break

    guard_verdicts: Counter[str] = Counter()
    for r in rounds:
        for k, v in r.get("verdicts", {}).items():
            guard_verdicts[k] += v

    mc = trial.get("manifest_completion", {})
    avg_completion = sum(mc.values()) / len(mc) if mc else 0.0

    return {
        "scenario": scenario,
        "n_rounds": n_rounds,
        "total_agents": total_agents,
        "doubles": doubles,
        "scope_viol": scope_viol,
        "probe_rounds": probe_rounds,
        "all_probe_verdicts": dict(all_probe_verdicts),
        "adv_probe_verdicts": dict(adv_probe_verdicts),
        "canary_visible_all": canary_visible_all,
        "adv_restricted": adv_restricted,
        "tp_count": len(adv_restricted),
        "tp_rate": tp_rate,
        "fp_count": len(normal_restricted),
        "fp_rate": fp_rate,
        "first_restricted_round": first_restricted_round,
        "guard_verdicts": dict(guard_verdicts),
        "avg_completion": avg_completion,
        "wall_clock": trial.get("wall_clock_seconds", 0),
        "total_steps": trial.get("total_steps", 0),
    }


def print_tables(results: list[dict]) -> None:
    """Print data tables to stdout. No commentary."""

    # Safety
    print("### Safety Invariants\n")
    print("| Scenario | Rounds | Agents | Double Bookings | Scope Violations |")
    print("|----------|--------|--------|-----------------|------------------|")
    for r in results:
        print(
            f"| {r['scenario']} | {r['n_rounds']} | {r['total_agents']} "
            f"| {r['doubles']} | {r['scope_viol']} |"
        )
    total_doubles = sum(r["doubles"] for r in results)
    total_scope = sum(r["scope_viol"] for r in results)
    total_rounds = sum(r["n_rounds"] for r in results)
    print(
        f"| **Total** | **{total_rounds}** | | **{total_doubles}** | **{total_scope}** |"
    )

    # Probe
    print("\n### Diagnostic Probe\n")
    print(
        "| Scenario | Probe Rounds | Canary Visible | Healthy | Compromised | Adv Verdicts |"
    )
    print(
        "|----------|-------------|----------------|---------|-------------|--------------|"
    )
    for r in results:
        cv = "all True" if r["canary_visible_all"] else "SOME FALSE"
        h = r["all_probe_verdicts"].get("healthy", 0)
        c = r["all_probe_verdicts"].get("compromised", 0)
        adv = r["adv_probe_verdicts"]
        adv_str = ", ".join(f"{k}: {v}" for k, v in sorted(adv.items())) if adv else "-"
        print(
            f"| {r['scenario']} | {r['probe_rounds']} | {cv} | {h} | {c} | {adv_str} |"
        )
    total_h = sum(r["all_probe_verdicts"].get("healthy", 0) for r in results)
    total_c = sum(r["all_probe_verdicts"].get("compromised", 0) for r in results)
    print(f"\nTotals: {total_h} healthy, {total_c} compromised")

    # Envelope
    print("\n### Statistical Envelope\n")
    print(
        "| Scenario | Adv Detected | TP Rate | FP Count | FP Rate | First Restriction |"
    )
    print(
        "|----------|-------------|---------|----------|---------|-------------------|"
    )
    for r in results:
        adv_list = ", ".join(r["adv_restricted"]) if r["adv_restricted"] else "none"
        fr = r["first_restricted_round"]
        fr_str = f"round {fr}" if fr is not None else "-"
        print(
            f"| {r['scenario']} | {r['tp_count']}/5 ({adv_list}) "
            f"| {r['tp_rate']:.0%} | {r['fp_count']}/{N_NORMAL} "
            f"| {r['fp_rate']:.1%} | {fr_str} |"
        )

    # Guard verdicts
    print("\n### Guard Verdicts\n")
    print("| Scenario | allow | blocked | conflict | denied |")
    print("|----------|-------|---------|----------|--------|")
    for r in results:
        gv = r["guard_verdicts"]
        print(
            f"| {r['scenario']} | {gv.get('allow', 0)} | {gv.get('blocked', 0)} "
            f"| {gv.get('conflict', 0)} | {gv.get('denied', 0)} |"
        )

    # Completion
    print("\n### Manifest Completion\n")
    print("| Scenario | Rounds | Completion |")
    print("|----------|--------|------------|")
    for r in results:
        print(f"| {r['scenario']} | {r['n_rounds']} | {r['avg_completion']:.1%} |")

    # Summary row
    print("\n### Summary\n")
    print("| Layer | Result |")
    print("|-------|--------|")
    print(
        f"| Static | {total_doubles + total_scope} safety violations / {total_rounds} rounds |"
    )
    print(f"| Probe | {total_c} compromised / {total_h + total_c} probed |")
    envelope_detected = sum(1 for r in results if r["tp_count"] > 0)
    print(f"| Envelope | adversaries detected in {envelope_detected}/6 scenarios |")


def main() -> None:
    results = []
    for sc in SCENARIOS:
        r = analyze_scenario(sc)
        if r:
            results.append(r)
        else:
            print(f"WARNING: {sc} data missing", file=sys.stderr)

    if not results:
        print("No data found.", file=sys.stderr)
        sys.exit(1)

    print_tables(results)


if __name__ == "__main__":
    main()
