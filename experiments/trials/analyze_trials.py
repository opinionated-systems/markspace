#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze experiment results from orchestrate.py runs.

Reads trial.jsonl files from the results/ directory and produces
markdown tables, CSV summaries, and (for scaling) matplotlib plots.

Usage:
    python experiments/trials/analyze_trials.py --experiment multi_trial
    python experiments/trials/analyze_trials.py --experiment adversarial
    python experiments/trials/analyze_trials.py --experiment scaling
    python experiments/trials/analyze_trials.py --experiment all
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

# The step-based safety checks (stored_run_safety.py) live with the stress test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stress_test"))
from stored_run_safety import check_run  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Pricing ($ per million tokens)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-oss-120b": (0.15, 0.60),
    "mercury-2": (0.25, 0.75),
    "glm-5p3-flash": (0.15, 0.50),
}

# Cached input ($ per million tokens), for models whose provider prices it
# apart (Fireworks model pages, read 2026-11-03).
CACHED_INPUT_PRICING: dict[str, float] = {
    "gpt-oss-120b": 0.015,
    "glm-5p3-flash": 0.03,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def envelope_restrictions(defense: dict) -> tuple[int, str]:
    """The envelope's restriction count and its source. Runs from before the
    envelope left the guard path record the guard's restrictions
    (envelope_restricted_count); later runs record the verdicts of the
    envelope as a monitor (monitor_restricted_count), on which nothing
    acts."""
    if "envelope_restricted_count" in defense:
        return defense["envelope_restricted_count"], "guard"
    return defense.get("monitor_restricted_count", 0), "monitor"


def double_bookings(record: dict) -> int:
    """A run's double bookings, from its stored steps (stored_run_safety.py)."""
    return check_run(Path(record["_path"]).parent)["double_bookings"]


def scope_violations(record: dict) -> int | str:
    """
    The run's scope violations, or "not measured": runs that measured them
    record recovered_bookings; earlier ones report 0 whatever happened.
    """
    if "recovered_bookings" in record:
        return record["scope_violations"]
    return "not measured"


def load_trials(experiment: str) -> list[dict]:
    """Load all trial.jsonl records for an experiment."""
    base = RESULTS_DIR / experiment
    if not base.exists():
        return []
    records: list[dict] = []
    for trial_file in sorted(base.rglob("trial.jsonl")):
        with open(trial_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    # Add path info
                    rel = trial_file.relative_to(base)
                    parts = list(rel.parent.parts)
                    if len(parts) >= 1:
                        record["_model"] = parts[0]
                    if len(parts) >= 2:
                        record["_variant"] = parts[1]
                    record["_path"] = str(trial_file)
                    records.append(record)
                except json.JSONDecodeError:
                    continue
    return records


def token_cost(tokens: dict, model: str) -> float:
    """Estimated cost of a run's tokens. Cached prompt tokens are priced at
    the model's cached-input rate where the run recorded them; a run that
    did not record them is priced at the full input rate (an upper bound)."""
    p_in, p_out = MODEL_PRICING.get(model, (0.50, 2.00))
    prompt = tokens["prompt"]
    if model in CACHED_INPUT_PRICING:
        cached, p_cached = tokens.get("cached_prompt", 0), CACHED_INPUT_PRICING[model]
    else:
        cached, p_cached = 0, 0.0
    completion = tokens["completion"]
    return ((prompt - cached) * p_in + cached * p_cached + completion * p_out) / 1e6


def compute_cost(record: dict) -> float:
    """Compute estimated cost from token counts."""
    model = record.get("_model", record.get("model", ""))
    return token_cost(record["tokens"], model)


def rule_of_three(n_trials: int, n_events: int) -> str:
    """Rule of Three upper bound for rare events."""
    if n_events > 0:
        rate = n_events / n_trials
        return f"{rate:.4f}"
    if n_trials == 0:
        return "N/A"
    upper = 3.0 / n_trials
    return f"0 (upper bound < {upper:.4f})"


def mean_ci(values: list[float]) -> tuple[float, float, float]:
    """Compute mean and 95% CI (t-distribution)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    m = sum(values) / n
    if n == 1:
        return m, m, m
    variance = sum((x - m) ** 2 for x in values) / (n - 1)
    se = math.sqrt(variance / n)
    # t critical value for 95% CI (approximation for small n)
    t_crit = {2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306}
    t = t_crit.get(n, 1.96)
    return m, m - t * se, m + t * se


# ---------------------------------------------------------------------------
# Multi-trial analysis
# ---------------------------------------------------------------------------


def analyze_multi_trial() -> None:
    """Analyze multi-trial results: aggregate safety metrics with CIs."""
    records = load_trials("multi_trial")
    if not records:
        print("No multi_trial results found.")
        return

    # Group by model
    by_model: dict[str, list[dict]] = {}
    for r in records:
        model = r.get("_model", "unknown")
        by_model.setdefault(model, []).append(r)

    print("\n# Multi-Trial Analysis\n")

    for model, trials in sorted(by_model.items()):
        n = len(trials)
        print(f"\n## {model} ({n} trials)\n")

        # Safety
        total_double = sum(double_bookings(t) for t in trials)
        scope = [scope_violations(t) for t in trials]
        print(f"| Metric | Value |")
        print(f"|--------|-------|")
        print(
            f"| Double bookings (from the steps) | {rule_of_three(n, total_double)} |"
        )
        counted = [v for v in scope if isinstance(v, int)]
        if len(counted) == len(scope):
            print(f"| Scope violations | {rule_of_three(n, sum(counted))} |")
        else:
            print("| Scope violations | not measured |")

        # Manifest completion
        completions: dict[str, list[float]] = {}
        for t in trials:
            for dept, rate in t.get("manifest_completion", {}).items():
                completions.setdefault(dept, []).append(rate)

        if completions:
            print(f"\n| Dept | Mean | 95% CI |")
            print(f"|------|------|--------|")
            for dept in sorted(completions):
                m, lo, hi = mean_ci(completions[dept])
                print(f"| {dept} | {m:.1%} | [{lo:.1%}, {hi:.1%}] |")

        # Cost and tokens
        costs = [compute_cost(t) for t in trials]
        m_cost, lo_cost, hi_cost = mean_ci(costs)
        total_cost = sum(costs)
        print(f"\n| Cost | Per trial | Total |")
        print(f"|------|-----------|-------|")
        print(f"| Mean | ${m_cost:.2f} | ${total_cost:.2f} |")

        # Wall clock
        times = [t.get("wall_clock_seconds", 0) for t in trials]
        m_time, _, _ = mean_ci(times)
        print(f"\nMean wall clock: {m_time:.0f}s per trial")

    # Save CSV
    csv_path = RESULTS_DIR / "multi_trial" / "summary.csv"
    _save_trials_csv(records, csv_path)
    print(f"\nCSV saved to {csv_path}")


# ---------------------------------------------------------------------------
# Adversarial analysis
# ---------------------------------------------------------------------------


def analyze_adversarial() -> None:
    """Analyze adversarial results: compare modes against baseline."""
    records = load_trials("adversarial")
    if not records:
        print("No adversarial results found.")
        return

    print("\n# Adversarial Analysis\n")

    # Group by model, then mode
    by_model: dict[str, dict[str, dict]] = {}
    for r in records:
        model = r.get("_model", "unknown")
        mode = r.get("_variant", "unknown")
        by_model.setdefault(model, {})[mode] = r

    print(f"| Model | Mode | Double Book | Scope Viol | Steps | Wasted | Completion |")
    print(f"|-------|------|-------------|------------|-------|--------|------------|")

    for model in sorted(by_model):
        for mode in ("confidence", "flood", "injection"):
            t = by_model[model].get(mode)
            if not t:
                continue
            db = double_bookings(t)
            sv = scope_violations(t)
            steps = t.get("total_steps", 0)
            wasted = t.get("total_wasted", 0)
            # Average completion across depts
            mc = t.get("manifest_completion", {})
            avg_comp = sum(mc.values()) / len(mc) if mc else 0
            print(
                f"| {model} | {mode} | {db} | {sv} | {steps} | {wasted} | {avg_comp:.1%} |"
            )

    # Verdict breakdown
    print(f"\n### Verdict Breakdown\n")
    print(f"| Model | Mode | ALLOW | DENY | YIELD_ALL | BLOCKED |")
    print(f"|-------|------|-------|------|-----------|---------|")
    for model in sorted(by_model):
        for mode in ("confidence", "flood", "injection"):
            t = by_model[model].get(mode)
            if not t:
                continue
            v = t.get("verdict_counts", {})
            print(
                f"| {model} | {mode} | "
                f"{v.get('ALLOW', 0)} | {v.get('DENY', 0)} | "
                f"{v.get('YIELD_ALL', 0)} | {v.get('BLOCKED', 0)} |"
            )


# ---------------------------------------------------------------------------
# Scaling analysis
# ---------------------------------------------------------------------------


def analyze_scaling() -> None:
    """Analyze scaling results: tables and plots."""
    records = load_trials("scaling")
    if not records:
        print("No scaling results found.")
        return

    print("\n# Scaling Analysis\n")

    # Group by model
    by_model: dict[str, list[dict]] = {}
    for r in records:
        model = r.get("_model", "unknown")
        by_model.setdefault(model, []).append(r)

    for model in sorted(by_model):
        trials = sorted(by_model[model], key=lambda t: t.get("total_agents", 0))
        print(f"\n## {model}\n")
        print(
            f"| N | Rounds | Steps | Wasted | Double Book | Scope Viol | Completion | Wall Clock | Cost |"
        )
        print(
            f"|---|--------|-------|--------|-------------|------------|------------|------------|------|"
        )

        xs, ys_safety, ys_completion, ys_time, ys_cost = [], [], [], [], []
        for t in trials:
            n = t.get("total_agents", 0)
            rounds = t.get("n_rounds", 0)
            steps = t.get("total_steps", 0)
            wasted = t.get("total_wasted", 0)
            db = double_bookings(t)
            sv = scope_violations(t)
            mc = t.get("manifest_completion", {})
            avg_comp = sum(mc.values()) / len(mc) if mc else 0
            wall = t.get("wall_clock_seconds", 0)
            cost = compute_cost(t)

            print(
                f"| {n} | {rounds} | {steps} | {wasted} | {db} | {sv} | "
                f"{avg_comp:.1%} | {wall:.0f}s | ${cost:.2f} |"
            )

            xs.append(n)
            ys_safety.append(db)
            ys_completion.append(avg_comp)
            ys_time.append(wall)
            ys_cost.append(cost)

        # Try to generate plots
        _try_plot(model, xs, ys_safety, ys_completion, ys_time, ys_cost)


def _try_plot(
    model: str,
    xs: list[int],
    safety: list[int],
    completion: list[float],
    wall_time: list[float],
    cost: list[float],
) -> None:
    """Generate scaling plots if matplotlib is available."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  (matplotlib not available, skipping plots)")
        return

    if len(xs) < 2:
        return

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.suptitle(f"Scaling Curves - {model}", fontsize=14)

    # Safety violations vs N
    ax = axes[0][0]
    ax.plot(xs, safety, "ro-", linewidth=2, markersize=8)
    ax.set_xlabel("Number of Agents")
    ax.set_ylabel("Double bookings (from the steps)")
    ax.set_title("Double bookings vs Scale")
    ax.set_xscale("log")
    ax.grid(True, alpha=0.3)

    # Completion rate vs N
    ax = axes[0][1]
    ax.plot(xs, [c * 100 for c in completion], "bs-", linewidth=2, markersize=8)
    ax.set_xlabel("Number of Agents")
    ax.set_ylabel("Manifest Completion (%)")
    ax.set_title("Utilization vs Scale")
    ax.set_xscale("log")
    ax.grid(True, alpha=0.3)

    # Wall time vs N
    ax = axes[1][0]
    ax.plot(xs, wall_time, "g^-", linewidth=2, markersize=8)
    ax.set_xlabel("Number of Agents")
    ax.set_ylabel("Wall Clock (s)")
    ax.set_title("Latency vs Scale")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.grid(True, alpha=0.3)

    # Cost vs N
    ax = axes[1][1]
    ax.plot(xs, cost, "mD-", linewidth=2, markersize=8)
    ax.set_xlabel("Number of Agents")
    ax.set_ylabel("Cost ($)")
    ax.set_title("Cost vs Scale")
    ax.set_xscale("log")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plot_dir = RESULTS_DIR / "scaling"
    plot_path = plot_dir / f"scaling_{model}.png"
    plt.savefig(plot_path, dpi=150)
    plt.close()
    print(f"  Plot saved to {plot_path}")


# ---------------------------------------------------------------------------
# Defense analysis
# ---------------------------------------------------------------------------


def analyze_defense() -> None:
    """Analyze defense-in-depth trial results."""
    records = load_trials("defense")
    if not records:
        print("No defense results found.")
        return

    print("\n# Defense-in-Depth Analysis\n")

    # Group by model, then scenario
    by_model: dict[str, dict[str, dict]] = {}
    for r in records:
        model = r.get("_model", "unknown")
        variant = r.get("_variant", "unknown")
        by_model.setdefault(model, {})[variant] = r

    # Safety invariants table
    print("## Safety Invariants\n")
    print("| Model | Scenario | Double Book | Scope Viol | Steps | Completion |")
    print("|-------|----------|-------------|------------|-------|------------|")

    for model in sorted(by_model):
        for scenario in (
            "rate_spike",
            "type_shift",
            "escalation",
            "probe_evasion",
            "slow_drift",
            "defense_combined",
        ):
            t = by_model[model].get(scenario)
            if not t:
                continue
            db = double_bookings(t)
            sv = scope_violations(t)
            steps = t.get("total_steps", 0)
            mc = t.get("manifest_completion", {})
            avg_comp = sum(mc.values()) / len(mc) if mc else 0
            print(
                f"| {model} | {scenario} | {db} | {sv} | " f"{steps} | {avg_comp:.1%} |"
            )

    # Defense metrics table
    print("\n## Defense Metrics\n")
    print(
        "| Model | Scenario | Envelope Restricted (source) | Barriers | "
        "Probe Healthy | Probe Suspicious | Probe Compromised |"
    )
    print(
        "|-------|----------|--------------------|---------"
        "|--------------|-----------------|-------------------|"
    )

    for model in sorted(by_model):
        for scenario in (
            "rate_spike",
            "type_shift",
            "escalation",
            "probe_evasion",
            "slow_drift",
            "defense_combined",
        ):
            t = by_model[model].get(scenario)
            if not t:
                continue
            defense = t.get("defense", {})
            n_restricted, source = envelope_restrictions(defense)
            barriers = defense.get("barriers", {})
            n_barriers = len(barriers)

            # Probe results
            probe_results = defense.get("probe_results", [])
            probe_healthy = sum(
                1 for r in probe_results if r.get("verdict") == "healthy"
            )
            probe_suspicious = sum(
                1 for r in probe_results if r.get("verdict") == "suspicious"
            )
            probe_compromised = sum(
                1 for r in probe_results if r.get("verdict") == "compromised"
            )

            print(
                f"| {model} | {scenario} | {n_restricted} ({source}) | {n_barriers} | "
                f"{probe_healthy} | {probe_suspicious} | {probe_compromised} |"
            )

    # Detection latency (for rate_spike, type_shift, escalation)
    print("\n## Detection Details\n")
    for model in sorted(by_model):
        print(f"\n### {model}\n")
        for scenario in (
            "rate_spike",
            "type_shift",
            "escalation",
            "probe_evasion",
            "slow_drift",
            "defense_combined",
        ):
            t = by_model[model].get(scenario)
            if not t:
                continue
            defense = t.get("defense", {})
            barriers = defense.get("barriers", {})
            if not barriers:
                print(f"**{scenario}**: No barriers triggered")
                continue

            print(f"**{scenario}**:")
            for agent_id, barrier_info in barriers.items():
                revoked = barrier_info.get("revoked_count", 0)
                # Runs from before the envelope left the guard path also
                # record its escalation counter.
                flags = (
                    f"{barrier_info['flag_count']} flags, "
                    f"scopes: {barrier_info.get('flagged_scopes', [])}, "
                    if "flag_count" in barrier_info
                    else ""
                )
                print(f"  - Agent {agent_id[:8]}...: {flags}{revoked} revoked")

    # Verdict breakdown
    print("\n## Verdict Breakdown\n")
    print("| Model | Scenario | allow | blocked | conflict | denied |")
    print("|-------|----------|-------|---------|----------|--------|")

    for model in sorted(by_model):
        for scenario in (
            "rate_spike",
            "type_shift",
            "escalation",
            "probe_evasion",
            "slow_drift",
            "defense_combined",
        ):
            t = by_model[model].get(scenario)
            if not t:
                continue
            v = t.get("verdict_counts", {})
            print(
                f"| {model} | {scenario} | "
                f"{v.get('allow', 0)} | {v.get('blocked', 0)} | "
                f"{v.get('conflict', 0)} | {v.get('denied', 0)} |"
            )

    # Save CSV
    csv_path = RESULTS_DIR / "defense" / "summary.csv"
    _save_defense_csv(records, csv_path)
    print(f"\nCSV saved to {csv_path}")


def _save_defense_csv(records: list[dict], path: Path) -> None:
    """Save defense trial records to CSV with defense-specific fields."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "model",
        "scenario",
        "seed",
        "total_agents",
        "n_rounds",
        "wall_clock_seconds",
        "total_steps",
        "total_wasted",
        "double_bookings",
        "scope_violations",
        "completion",
        "envelope_restricted",
        "envelope_restricted_source",
        "barriers",
        "probe_healthy",
        "probe_suspicious",
        "probe_compromised",
        "cost",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in records:
            defense = r.get("defense", {})
            probe_results = defense.get("probe_results", [])
            mc = r.get("manifest_completion", {})
            avg_comp = sum(mc.values()) / len(mc) if mc else 0
            n_restricted, source = envelope_restrictions(defense)
            row = {
                "model": r.get("_model", r.get("model", "")),
                "scenario": r.get("_variant", ""),
                "seed": r.get("seed", ""),
                "total_agents": r.get("total_agents", ""),
                "n_rounds": r.get("n_rounds", ""),
                "wall_clock_seconds": r.get("wall_clock_seconds", ""),
                "total_steps": r.get("total_steps", ""),
                "total_wasted": r.get("total_wasted", ""),
                "double_bookings": r.get("double_bookings", ""),
                "scope_violations": r.get("scope_violations", ""),
                "completion": f"{avg_comp:.4f}",
                "envelope_restricted": n_restricted,
                "envelope_restricted_source": source,
                "barriers": len(defense.get("barriers", {})),
                "probe_healthy": sum(
                    1 for p in probe_results if p.get("verdict") == "healthy"
                ),
                "probe_suspicious": sum(
                    1 for p in probe_results if p.get("verdict") == "suspicious"
                ),
                "probe_compromised": sum(
                    1 for p in probe_results if p.get("verdict") == "compromised"
                ),
                "cost": f"{compute_cost(r):.4f}",
            }
            writer.writerow(row)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def _save_trials_csv(records: list[dict], path: Path) -> None:
    """Save trial records to CSV."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "model",
        "seed",
        "total_agents",
        "n_rounds",
        "wall_clock_seconds",
        "total_steps",
        "total_wasted",
        "double_bookings",
        "scope_violations",
        "prompt_tokens",
        "completion_tokens",
        "cost",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in records:
            tokens = r.get("tokens", {})
            row = {
                "model": r.get("_model", r.get("model", "")),
                "seed": r.get("seed", ""),
                "total_agents": r.get("total_agents", ""),
                "n_rounds": r.get("n_rounds", ""),
                "wall_clock_seconds": r.get("wall_clock_seconds", ""),
                "total_steps": r.get("total_steps", ""),
                "total_wasted": r.get("total_wasted", ""),
                "double_bookings": r.get("double_bookings", ""),
                "scope_violations": r.get("scope_violations", ""),
                "prompt_tokens": tokens.get("prompt", ""),
                "completion_tokens": tokens.get("completion", ""),
                "cost": f"{compute_cost(r):.4f}",
            }
            writer.writerow(row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze experiment trial results")
    parser.add_argument(
        "--experiment",
        choices=["multi_trial", "adversarial", "scaling", "defense", "all"],
        required=True,
    )
    args = parser.parse_args()

    analyzers = {
        "multi_trial": analyze_multi_trial,
        "adversarial": analyze_adversarial,
        "scaling": analyze_scaling,
        "defense": analyze_defense,
    }

    if args.experiment == "all":
        for name, fn in analyzers.items():
            fn()
    else:
        analyzers[args.experiment]()


if __name__ == "__main__":
    main()
