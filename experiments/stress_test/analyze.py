#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Office Coordination Week — Analysis

Reads decomposed result directories:
    results_dir/
    ├── trial.jsonl    — 1 line per trial (aggregates, safety, coverage)
    ├── rounds.jsonl   — 1 line per round (verdicts, marks, tokens, bot log)
    ├── agents.jsonl   — 1 line per agent-round (tokens, step count, wasted)
    └── steps.jsonl    — 1 line per tool call (args, result, verdict)

Usage:
    # Full report from a results directory
    python experiments/stress_test/analyze.py results_stress_v1_20260228/

    # With figures
    python experiments/stress_test/analyze.py results_stress_v1_20260228/ --figures

    # Multiple directories
    python experiments/stress_test/analyze.py results_*/

    # Drill into specific agent
    python experiments/stress_test/analyze.py results_stress_v1_20260228/ --agent eng-lead

    # Show step-level detail for a round
    python experiments/stress_test/analyze.py results_stress_v1_20260228/ --round 0 --steps
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    HAS_PLOT = True
except ImportError:
    HAS_PLOT = False

DEPTS = ["eng", "design", "product", "sales", "ops"]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    """Load all lines from a JSONL file."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def load_trial(results_dir: Path) -> list[dict]:
    return _load_jsonl(results_dir / "trial.jsonl")


def load_rounds(results_dir: Path) -> list[dict]:
    return _load_jsonl(results_dir / "rounds.jsonl")


def load_agents(results_dir: Path) -> list[dict]:
    return _load_jsonl(results_dir / "agents.jsonl")


def load_steps(results_dir: Path) -> list[dict]:
    return _load_jsonl(results_dir / "steps.jsonl")


def find_result_dirs(paths: list[str]) -> list[Path]:
    """Resolve input paths to result directories."""
    dirs: list[Path] = []
    for p_str in paths:
        if "*" in p_str:
            for p in sorted(Path(".").glob(p_str)):
                if p.is_dir() and (p / "trial.jsonl").exists():
                    dirs.append(p)
        else:
            p = Path(p_str)
            if p.is_dir() and (p / "trial.jsonl").exists():
                dirs.append(p)
    return dirs


# ---------------------------------------------------------------------------
# Safety report
# ---------------------------------------------------------------------------


def print_safety_report(trials: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("SAFETY REPORT")
    print("=" * 60)

    total = len(trials)
    double_booking_trials = sum(1 for t in trials if t.get("double_bookings", 0) > 0)
    total_doubles = sum(t.get("double_bookings", 0) for t in trials)
    scope_violation_trials = sum(1 for t in trials if t.get("scope_violations", 0) > 0)
    total_scope = sum(t.get("scope_violations", 0) for t in trials)

    print(f"Trials: {total}")
    print(f"Double bookings: {total_doubles} across {double_booking_trials} trials")
    print(f"Scope violations: {total_scope} across {scope_violation_trials} trials")
    print(
        "ALL SAFETY CHECKS PASSED"
        if total_doubles == 0 and total_scope == 0
        else "SAFETY VIOLATIONS DETECTED"
    )


# ---------------------------------------------------------------------------
# Protocol coverage
# ---------------------------------------------------------------------------


def print_protocol_coverage(trials: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("PROTOCOL COVERAGE")
    print("=" * 60)

    mark_totals: dict[str, int] = {}
    for t in trials:
        for mt, count in t.get("mark_type_counts", {}).items():
            mark_totals[mt] = mark_totals.get(mt, 0) + count

    print("\nMark type distribution:")
    for mt in ["intent", "action", "observation", "warning", "need"]:
        print(f"  {mt:15s}: {mark_totals.get(mt, 0):6d}")

    verdict_totals: dict[str, int] = {}
    for t in trials:
        for v, count in t.get("verdict_counts", {}).items():
            verdict_totals[v] = verdict_totals.get(v, 0) + count

    print("\nVerdict distribution:")
    for v in ["allow", "conflict", "blocked", "denied"]:
        print(f"  {v:15s}: {verdict_totals.get(v, 0):6d}")

    need_totals: dict[str, int] = {}
    for t in trials:
        for scope, count in t.get("need_marks_by_scope", {}).items():
            need_totals[scope] = need_totals.get(scope, 0) + count

    if need_totals:
        print("\nNeed marks by scope:")
        for scope, count in sorted(need_totals.items(), key=lambda x: -x[1]):
            print(f"  {scope:25s}: {count:4d}")

    total_projected = sum(t.get("projected_reads", 0) for t in trials)
    print(f"\nProjected reads (PROTECTED scope): {total_projected}")

    features = {
        "INTENT marks": mark_totals.get("intent", 0) > 0,
        "ACTION marks": mark_totals.get("action", 0) > 0,
        "OBSERVATION marks": mark_totals.get("observation", 0) > 0,
        "WARNING marks": mark_totals.get("warning", 0) > 0,
        "NEED marks": mark_totals.get("need", 0) > 0,
        "ALLOW verdicts": verdict_totals.get("allow", 0) > 0,
        "CONFLICT verdicts": verdict_totals.get("conflict", 0) > 0,
        "BLOCKED verdicts": verdict_totals.get("blocked", 0) > 0,
        "Projected reads": total_projected > 0,
    }

    covered = sum(1 for v in features.values() if v)
    print(f"\nFeature coverage:")
    for feat, hit in features.items():
        print(f"  {feat:25s}: {'YES' if hit else 'NO'}")
    print(f"\n  Coverage: {covered}/{len(features)} ({covered/len(features):.0%})")


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def print_efficiency_report(trials: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("EFFICIENCY METRICS")
    print("=" * 60)

    for t in trials:
        total_steps = t.get("total_steps", 0)
        total_wasted = t.get("total_wasted", 0)
        tokens = t.get("tokens", {})
        tok_total = tokens.get("prompt", 0) + tokens.get("completion", 0)
        waste_rate = total_wasted / total_steps if total_steps > 0 else 0

        print(f"\nTrial (seed={t.get('seed', '?')}, model={t.get('model', '?')}):")
        print(f"  Steps: {total_steps} | Wasted: {total_wasted} ({waste_rate:.1%})")
        print(f"  Steps/agent/round: {t.get('steps_per_agent_per_round', 0):.2f}")
        print(
            f"  Tokens: {tok_total:,} ({tokens.get('prompt', 0):,} prompt, {tokens.get('completion', 0):,} completion)"
        )
        print(f"  Wall clock: {t.get('wall_clock_seconds', 0):.1f}s")


# ---------------------------------------------------------------------------
# Department metrics
# ---------------------------------------------------------------------------


def print_dept_report(trials: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("DEPARTMENT METRICS")
    print("=" * 60)

    for t in trials:
        dept_metrics = t.get("dept_metrics", {})
        manifest_completion = t.get("manifest_completion", {})

        print(f"\nTrial (seed={t.get('seed', '?')}):")
        print(f"  {'Dept':<10s} {'Items':>6s} {'Done':>6s} {'Failed':>6s} {'Rate':>8s}")
        print(f"  {'-'*40}")
        for dept in DEPTS:
            dm = dept_metrics.get(dept, {})
            total = dm.get("total_items", 0)
            done = dm.get("completed", 0)
            failed = dm.get("failed", 0)
            rate = manifest_completion.get(dept, 0)
            print(f"  {dept:<10s} {total:6d} {done:6d} {failed:6d} {rate:7.1%}")

        # Scope breakdown
        print(f"\n  Per-scope completion:")
        all_scopes: set[str] = set()
        for dm in dept_metrics.values():
            for scope in dm.get("scope_breakdown", {}).keys():
                all_scopes.add(scope)

        for scope in sorted(all_scopes):
            print(f"\n    {scope}:")
            for dept in DEPTS:
                sb = (
                    dept_metrics.get(dept, {}).get("scope_breakdown", {}).get(scope, {})
                )
                total = sb.get("total", 0)
                if total > 0:
                    done = sb.get("completed", 0)
                    failed = sb.get("failed", 0)
                    print(f"      {dept}: {done}/{total} completed, {failed} failed")


# ---------------------------------------------------------------------------
# Cross-department fairness
# ---------------------------------------------------------------------------


def print_fairness_report(trials: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("CROSS-DEPARTMENT FAIRNESS")
    print("=" * 60)

    for t in trials:
        lunch = t.get("lunch_preference_satisfaction", {})
        if lunch:
            print(f"\nTrial (seed={t.get('seed', '?')}):")
            print(f"  Lunch preference satisfaction (got preferred type):")
            for dept in DEPTS:
                print(f"    {dept:<10s}: {lunch.get(dept, 0):.1%}")
            rates = [lunch.get(d, 0) for d in DEPTS]
            if rates:
                print(f"    Spread (max-min): {max(rates) - min(rates):.1%}")

        parking = t.get("parking_by_role", {})
        if parking:
            print(f"\n  Parking allocation:")
            for role, count in parking.items():
                print(f"    {role}: {count}")


# ---------------------------------------------------------------------------
# Temporal analysis (from rounds.jsonl)
# ---------------------------------------------------------------------------


def print_temporal_report(rounds: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("TEMPORAL ANALYSIS")
    print("=" * 60)

    if not rounds:
        print("  No round data.")
        return

    print(
        f"\n  {'Round':>5s} {'Day':>4s} {'Block':>5s} {'Active':>7s} {'Steps':>6s} "
        f"{'Wasted':>7s} {'Marks':>6s} {'Tokens':>10s}"
    )
    print(f"  {'-'*55}")

    for rr in rounds:
        round_num = rr.get("round_num", "?")
        day = rr.get("day", "?")
        block = rr.get("block", "?")
        active = rr.get("active_agents", 0)
        steps = rr.get("steps", 0)
        wasted = rr.get("wasted_attempts", 0)
        mc = rr.get("mark_counts", {})
        total_marks = sum(mc.values())
        tokens = rr.get("tokens", {})
        tok = tokens.get("prompt", 0) + tokens.get("completion", 0)
        print(
            f"  {round_num:5d} {day:>4s} {block:>5s} {active:7d} {steps:6d} "
            f"{wasted:7d} {total_marks:6d} {tok:10,}"
        )

    print(f"\n  Mark accumulation:")
    for rr in rounds:
        mc = rr.get("mark_counts", {})
        parts = [f"{k}={v}" for k, v in sorted(mc.items())]
        print(f"    Round {rr.get('round_num', '?')}: {', '.join(parts)}")


# ---------------------------------------------------------------------------
# Agent drill-down (from agents.jsonl)
# ---------------------------------------------------------------------------


def print_agent_report(agents: list[dict], agent_filter: str | None = None) -> None:
    print("\n" + "=" * 60)
    print("AGENT DETAIL")
    print("=" * 60)

    if agent_filter:
        agents = [a for a in agents if a.get("agent") == agent_filter]
        if not agents:
            print(f"  No data for agent '{agent_filter}'.")
            return

    # Group by agent
    by_agent: dict[str, list[dict]] = {}
    for a in agents:
        name = a.get("agent", "?")
        by_agent.setdefault(name, []).append(a)

    for name in sorted(by_agent.keys()):
        records = by_agent[name]
        total_steps = sum(r.get("step_count", 0) for r in records)
        total_wasted = sum(r.get("wasted_attempts", 0) for r in records)
        total_prompt = sum(r.get("tokens", {}).get("prompt", 0) for r in records)
        total_comp = sum(r.get("tokens", {}).get("completion", 0) for r in records)
        dept = records[0].get("dept", "?")
        is_head = records[0].get("is_head", False)
        head_tag = " (HEAD)" if is_head else ""

        print(f"\n  {name} [{dept}]{head_tag}")
        print(f"    Rounds active: {len(records)}")
        print(f"    Total steps: {total_steps} | Wasted: {total_wasted}")
        print(
            f"    Tokens: {total_prompt + total_comp:,} ({total_prompt:,}p + {total_comp:,}c)"
        )

        if agent_filter:
            # Show per-round breakdown
            print(f"    Per-round:")
            for r in sorted(records, key=lambda x: x.get("round_num", 0)):
                rn = r.get("round_num", "?")
                sc = r.get("step_count", 0)
                wa = r.get("wasted_attempts", 0)
                tok = r.get("tokens", {})
                print(
                    f"      Round {rn}: {sc} steps, {wa} wasted, "
                    f"{tok.get('prompt', 0) + tok.get('completion', 0):,} tokens"
                )


# ---------------------------------------------------------------------------
# Step drill-down (from steps.jsonl)
# ---------------------------------------------------------------------------


def print_step_report(
    steps: list[dict],
    round_filter: int | None = None,
    agent_filter: str | None = None,
) -> None:
    print("\n" + "=" * 60)
    print("STEP DETAIL")
    print("=" * 60)

    if round_filter is not None:
        steps = [s for s in steps if s.get("round_num") == round_filter]
    if agent_filter:
        steps = [s for s in steps if s.get("agent") == agent_filter]

    if not steps:
        print("  No matching steps.")
        return

    print(f"  {len(steps)} steps")

    # Tool usage summary
    tool_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    for s in steps:
        tool = s.get("tool", "?")
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        v = s.get("guard_verdict")
        if v:
            verdict_counts[v] = verdict_counts.get(v, 0) + 1

    print(f"\n  Tool usage:")
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        print(f"    {tool:25s}: {count}")

    print(f"\n  Verdicts:")
    for v, count in sorted(verdict_counts.items(), key=lambda x: -x[1]):
        print(f"    {v:15s}: {count}")

    # Show individual steps if filtered to a specific agent
    if agent_filter:
        print(f"\n  Steps for {agent_filter}:")
        for s in steps:
            rn = s.get("round_num", "?")
            tool = s.get("tool", "?")
            args = s.get("args", {})
            result = s.get("result", "")
            verdict = s.get("guard_verdict", "")
            args_str = ", ".join(f"{k}={v}" for k, v in args.items()) if args else ""
            result_preview = result[:80].replace("\n", " ") if result else ""
            verdict_tag = f" [{verdict}]" if verdict else ""
            print(f"    R{rn} {tool}({args_str}){verdict_tag}")
            if result_preview:
                print(f"         → {result_preview}")


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def generate_figures(
    trials: list[dict], all_rounds: list[dict], output_dir: Path
) -> None:
    if not HAS_PLOT:
        print("matplotlib not available, skipping figures.")
        return

    for t in trials:
        seed = t.get("seed", 0)
        # Filter rounds for this trial's seed
        rounds = [r for r in all_rounds if r.get("seed") == seed]
        if not rounds:
            continue

        # --- Figure 1: Round progression ---
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle(f"Stress Test — Round Progression (seed={seed})", fontsize=14)

        round_nums = [rr["round_num"] + 1 for rr in rounds]
        labels = [f"{rr['day'].upper()}\n{rr['block']}" for rr in rounds]

        ax = axes[0, 0]
        ax.bar(round_nums, [rr["active_agents"] for rr in rounds], color="#4C72B0")
        ax.set_xlabel("Round")
        ax.set_ylabel("Active Agents")
        ax.set_title("Active Agents per Round")
        ax.set_xticks(round_nums)
        ax.set_xticklabels(labels, fontsize=8)

        ax = axes[0, 1]
        ax.bar(
            round_nums, [rr["steps"] for rr in rounds], color="#55A868", label="Steps"
        )
        ax.bar(
            round_nums,
            [rr["wasted_attempts"] for rr in rounds],
            color="#C44E52",
            label="Wasted",
        )
        ax.set_xlabel("Round")
        ax.set_ylabel("Count")
        ax.set_title("Steps & Wasted Attempts")
        ax.set_xticks(round_nums)
        ax.set_xticklabels(labels, fontsize=8)
        ax.legend()

        ax = axes[1, 0]
        mark_types = ["intent", "action", "observation", "warning", "need"]
        colors_map = {
            "intent": "#4C72B0",
            "action": "#55A868",
            "observation": "#CCB974",
            "warning": "#C44E52",
            "need": "#8172B2",
        }
        for mt in mark_types:
            values = [rr.get("mark_counts", {}).get(mt, 0) for rr in rounds]
            ax.plot(round_nums, values, marker="o", label=mt, color=colors_map[mt])
        ax.set_xlabel("Round")
        ax.set_ylabel("Cumulative Marks")
        ax.set_title("Mark Accumulation")
        ax.set_xticks(round_nums)
        ax.set_xticklabels(labels, fontsize=8)
        ax.legend(fontsize=8)

        ax = axes[1, 1]
        verdict_types = ["allow", "conflict", "blocked"]
        v_colors = {"allow": "#55A868", "conflict": "#C44E52", "blocked": "#8172B2"}
        bottom = [0] * len(round_nums)
        for vt in verdict_types:
            values = [rr.get("verdicts", {}).get(vt, 0) for rr in rounds]
            ax.bar(round_nums, values, bottom=bottom, label=vt, color=v_colors[vt])
            bottom = [b + v for b, v in zip(bottom, values)]
        ax.set_xlabel("Round")
        ax.set_ylabel("Count")
        ax.set_title("Verdict Distribution")
        ax.set_xticks(round_nums)
        ax.set_xticklabels(labels, fontsize=8)
        ax.legend()

        plt.tight_layout()
        fig_path = output_dir / f"fig_round_progression_seed{seed}.png"
        fig.savefig(fig_path, dpi=150)
        plt.close(fig)
        print(f"Saved: {fig_path}")

        # --- Figure 2: Department metrics ---
        dept_metrics = t.get("dept_metrics", {})
        if dept_metrics:
            fig, axes = plt.subplots(1, 2, figsize=(14, 5))
            fig.suptitle(f"Stress Test — Department Metrics (seed={seed})", fontsize=14)

            ax = axes[0]
            depts = list(dept_metrics.keys())
            rates = [dept_metrics[d].get("completion_rate", 0) for d in depts]
            bars = ax.bar(depts, rates, color="#4C72B0")
            ax.set_ylabel("Completion Rate")
            ax.set_title("Manifest Completion by Department")
            ax.set_ylim(0, 1)
            for bar, rate in zip(bars, rates):
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 0.02,
                    f"{rate:.0%}",
                    ha="center",
                    fontsize=9,
                )

            ax = axes[1]
            lunch = t.get("lunch_preference_satisfaction", {})
            if lunch:
                lunch_rates = [lunch.get(d, 0) for d in depts]
                bars = ax.bar(depts, lunch_rates, color="#CCB974")
                ax.set_ylabel("Preference Satisfaction")
                ax.set_title("Lunch Type A Preference Satisfaction")
                ax.set_ylim(0, 1)
                for bar, rate in zip(bars, lunch_rates):
                    ax.text(
                        bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + 0.02,
                        f"{rate:.0%}",
                        ha="center",
                        fontsize=9,
                    )

            plt.tight_layout()
            fig_path = output_dir / f"fig_dept_metrics_seed{seed}.png"
            fig.savefig(fig_path, dpi=150)
            plt.close(fig)
            print(f"Saved: {fig_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Office Stress Test — Analysis")
    parser.add_argument("dirs", nargs="+", help="Result directories (supports globs)")
    parser.add_argument("--figures", action="store_true", help="Generate figures")
    parser.add_argument(
        "--agent", type=str, default=None, help="Filter to specific agent"
    )
    parser.add_argument(
        "--round", type=int, default=None, help="Filter to specific round"
    )
    parser.add_argument("--steps", action="store_true", help="Show step-level detail")
    args = parser.parse_args()

    result_dirs = find_result_dirs(args.dirs)
    if not result_dirs:
        print("No result directories found.")
        return

    # Load data from all directories
    all_trials: list[dict] = []
    all_rounds: list[dict] = []
    all_agents: list[dict] = []
    all_steps: list[dict] = []

    for d in result_dirs:
        all_trials.extend(load_trial(d))
        all_rounds.extend(load_rounds(d))
        if args.agent or args.steps:
            all_agents.extend(load_agents(d))
        if args.steps:
            all_steps.extend(load_steps(d))

    valid = [t for t in all_trials if not t.get("error")]
    errored = [t for t in all_trials if t.get("error")]

    print(
        f"Loaded {len(all_trials)} trials from {len(result_dirs)} directories "
        f"({len(valid)} valid, {len(errored)} errored)"
    )
    for d in result_dirs:
        sizes = {}
        for fname in ["trial.jsonl", "rounds.jsonl", "agents.jsonl", "steps.jsonl"]:
            fpath = d / fname
            if fpath.exists():
                sz = fpath.stat().st_size
                sizes[fname] = (
                    f"{sz/1024:.0f}K" if sz < 1024 * 1024 else f"{sz/1024/1024:.1f}M"
                )
        print(f"  {d.name}: {', '.join(f'{k}={v}' for k, v in sizes.items())}")

    if valid:
        print_safety_report(valid)
        print_protocol_coverage(valid)
        print_efficiency_report(valid)
        print_dept_report(valid)
        print_fairness_report(valid)
        print_temporal_report(all_rounds)

    if args.agent or (all_agents and not args.steps):
        # Lazy-load agents if not already loaded
        if not all_agents:
            for d in result_dirs:
                all_agents.extend(load_agents(d))
        print_agent_report(all_agents, args.agent)

    if args.steps:
        if not all_steps:
            for d in result_dirs:
                all_steps.extend(load_steps(d))
        print_step_report(all_steps, args.round, args.agent)

    if errored:
        print(f"\n{'=' * 60}")
        print(f"ERRORS ({len(errored)} trials)")
        print("=" * 60)
        for t in errored:
            print(f"  Seed {t.get('seed', '?')}: {t.get('error', 'unknown')[:200]}")

    if args.figures and valid:
        output_dir = result_dirs[0]
        generate_figures(valid, all_rounds, output_dir)


if __name__ == "__main__":
    main()
