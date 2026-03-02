#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pilot Analysis — Variance Classification & Power Calculations

Reads pilot JSONL, classifies cells as deterministic/stochastic,
computes power-based sample sizes for Phase 2, and estimates costs.

Usage:
    python experiments/validation/analyze_pilot.py results_pilot_*.jsonl
    python experiments/validation/analyze_pilot.py results_pilot_*.jsonl --min-delta 0.5
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

# Pricing per 1M tokens (input, output)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-oss-120b": (0.15, 0.60),
    "deepseek-v3p2": (0.56, 1.68),
    "kimi-k2p5": (0.60, 3.00),
    "glm-5": (1.00, 3.20),
}


def load_results(paths: list[str]) -> list[dict]:
    results: list[dict] = []
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("trial_id", 0) == -1:
                    continue  # skip early-stop sentinels
                results.append(record)
    return results


def cell_key(record: dict) -> str:
    c = record["cell"]
    n_rounds = c.get("n_rounds", 1)
    return (
        f"{c['model']}|{c['n_agents']}|{c['visibility']}"
        f"|{c['temperature']}|{c['execution_mode']}|{c['conflict_policy']}"
        f"|{n_rounds}"
    )


def cell_short(record: dict) -> str:
    c = record["cell"]
    n_rounds = c.get("n_rounds", 1)
    rounds_tag = f" R={n_rounds}" if n_rounds > 1 else ""
    return f"{c['model']} N={c['n_agents']} {c['visibility']} t={c['temperature']}{rounds_tag}"


def group_by_cell(results: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        groups[cell_key(r)].append(r)
    return dict(sorted(groups.items()))


def z_value(p: float) -> float:
    """Approximate inverse normal CDF for common values."""
    # Abramowitz & Stegun approximation
    table = {0.025: 1.960, 0.05: 1.645, 0.10: 1.282, 0.20: 0.842}
    if p in table:
        return table[p]
    try:
        from scipy.stats import norm

        return norm.ppf(1 - p)
    except ImportError:
        # Linear interpolation fallback
        if p <= 0.025:
            return 1.960
        if p <= 0.05:
            return 1.645
        if p <= 0.10:
            return 1.282
        return 0.842


def sample_size_two_group(
    sd1: float,
    sd2: float,
    delta: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """Required n per group for two-sample t-test."""
    if delta <= 0:
        return 999  # can't detect zero effect
    z_a = z_value(alpha / 2)
    z_b = z_value(1 - power)
    n = math.ceil((z_a + z_b) ** 2 * (sd1**2 + sd2**2) / delta**2)
    return max(n, 3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pilot Analysis")
    parser.add_argument("files", nargs="+", help="JSONL result files")
    parser.add_argument(
        "--min-delta",
        type=float,
        default=1.0,
        help="Minimum interesting difference in steps/agent for power calc",
    )
    args = parser.parse_args()

    results = load_results(args.files)
    if not results:
        print("No results found.")
        return

    cells = group_by_cell(results)

    # =====================================================================
    # Section 1: Per-cell variance classification
    # =====================================================================
    print("=" * 90)
    print("PER-CELL VARIANCE CLASSIFICATION")
    print("=" * 90)
    header = f"{'Cell':<55} {'N':>3} {'Mean':>5} {'SD':>6} {'Min':>4} {'Max':>4} {'Type':>6} {'Comp%':>5} {'Dbl':>3}"
    print(header)
    print("-" * 90)

    cell_stats: dict[str, dict] = {}
    deterministic_count = 0
    stochastic_count = 0
    failed_count = 0

    for key, trials in cells.items():
        ok_trials = [t for t in trials if t.get("error") is None]
        steps_vals = [t["steps_per_agent"] for t in ok_trials]
        n = len(steps_vals)

        if n == 0:
            short = cell_short(trials[0])
            print(f"{short:<55} {'FAILED':>3}")
            failed_count += 1
            cell_stats[key] = {"type": "failed", "n": 0}
            continue

        mean = statistics.mean(steps_vals)
        sd = statistics.stdev(steps_vals) if n > 1 else 0.0
        mn = min(steps_vals)
        mx = max(steps_vals)
        completion = (
            sum(1 for t in ok_trials if t["all_completed"]) / len(ok_trials)
            if ok_trials
            else 0
        )
        doubles = sum(t["double_bookings"] for t in ok_trials)
        classification = "det" if sd == 0 else "stoch"

        if sd == 0:
            deterministic_count += 1
        else:
            stochastic_count += 1

        short = cell_short(trials[0])
        print(
            f"{short:<55} {n:>3} {mean:>5.1f} {sd:>6.3f} {mn:>4.1f} {mx:>4.1f} {classification:>6} {completion * 100:>4.0f}% {doubles:>3}"
        )

        cell_stats[key] = {
            "type": classification,
            "n": n,
            "mean": mean,
            "sd": sd,
            "completion": completion,
            "doubles": doubles,
        }

    print(
        f"\nDeterministic: {deterministic_count}  Stochastic: {stochastic_count}  Failed: {failed_count}"
    )

    # =====================================================================
    # Section 2: Model viability
    # =====================================================================
    print(f"\n{'=' * 90}")
    print("MODEL VIABILITY")
    print("=" * 90)

    models: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        models[r["cell"]["model"]].append(r)

    model_viable: dict[str, bool] = {}
    for model in sorted(models.keys()):
        model_results = models[model]
        total = len(model_results)
        ok = sum(
            1 for r in model_results if r["all_completed"] and r.get("error") is None
        )
        errors = sum(1 for r in model_results if r.get("error") is not None)
        doubles = sum(r["double_bookings"] for r in model_results)

        model_cells = group_by_cell(model_results)
        low_cells = sum(
            1
            for trials in model_cells.values()
            if sum(1 for t in trials if t["all_completed"]) / max(len(trials), 1) < 0.5
        )

        viable = low_cells < len(model_cells) * 0.5
        model_viable[model] = viable

        status = "OK" if viable else "DROP"
        print(
            f"  {model:<20} {ok}/{total} completed  {errors} errors  {doubles} double-bookings  {low_cells} low cells  [{status}]"
        )

    # =====================================================================
    # Section 3: Temperature effect on variance
    # =====================================================================
    print(f"\n{'=' * 90}")
    print("TEMPERATURE EFFECT ON VARIANCE")
    print("=" * 90)

    # Group by (model, n_agents, visibility) and compare temp=0.0 vs temp=0.7
    temp_groups: dict[str, dict[float, dict]] = defaultdict(dict)
    for key, stats in cell_stats.items():
        parts = key.split("|")
        triple = f"{parts[0]}|{parts[1]}|{parts[2]}"
        temp = float(parts[3])
        temp_groups[triple][temp] = stats

    print(f"{'Triple':<45} {'SD@0.0':>7} {'SD@0.7':>7} {'Effect':>10}")
    print("-" * 75)
    for triple in sorted(temp_groups.keys()):
        temps = temp_groups[triple]
        sd_0 = temps.get(0.0, {}).get("sd", None)
        sd_7 = temps.get(0.7, {}).get("sd", None)
        if sd_0 is not None and sd_7 is not None:
            sd_0_s = f"{sd_0:.3f}"
            sd_7_s = f"{sd_7:.3f}"
            if sd_0 == 0 and sd_7 == 0:
                effect = "none"
            elif sd_0 == 0 and sd_7 > 0:
                effect = "INTRODUCES"
            elif sd_7 > sd_0 * 1.5:
                effect = "increases"
            elif sd_7 < sd_0 * 0.67:
                effect = "decreases"
            else:
                effect = "~same"
            print(f"{triple:<45} {sd_0_s:>7} {sd_7_s:>7} {effect:>10}")

    # =====================================================================
    # Section 4: Power calculations
    # =====================================================================
    print(f"\n{'=' * 90}")
    print(f"POWER CALCULATIONS (80% power, alpha=0.05, min delta={args.min_delta})")
    print("=" * 90)

    # Compare visible vs hidden within each (model, n_agents, temperature)
    print(f"\n--- Visible vs Hidden ---")
    print(f"{'Comparison':<50} {'delta':>6} {'SD_vis':>7} {'SD_hid':>7} {'n/grp':>6}")
    vis_comparisons: list[tuple[str, str, str]] = []
    for key, stats in cell_stats.items():
        if stats["type"] == "failed":
            continue
        parts = key.split("|")
        if parts[2] == "visible":
            hidden_key = "|".join(parts[:2] + ["hidden"] + parts[3:])
            if hidden_key in cell_stats and cell_stats[hidden_key]["type"] != "failed":
                vis_comparisons.append(
                    (key, hidden_key, "|".join(parts[:2] + parts[3:]))
                )

    for vis_key, hid_key, label in sorted(vis_comparisons):
        vs = cell_stats[vis_key]
        hs = cell_stats[hid_key]
        delta = abs(vs["mean"] - hs["mean"])
        if delta < 0.01:
            delta = args.min_delta
        n_req = sample_size_two_group(vs["sd"], hs["sd"], delta)
        print(f"{label:<50} {delta:>6.2f} {vs['sd']:>7.3f} {hs['sd']:>7.3f} {n_req:>6}")

    # Compare models within each (n_agents, visibility, temperature)
    print(f"\n--- Model Comparisons (pairwise) ---")
    model_groups: dict[str, dict[str, dict]] = defaultdict(dict)
    for key, stats in cell_stats.items():
        if stats["type"] == "failed":
            continue
        parts = key.split("|")
        condition = "|".join(parts[1:])
        model_groups[condition][parts[0]] = stats

    max_n_needed = 0
    for condition in sorted(model_groups.keys()):
        model_stats = model_groups[condition]
        model_names = sorted(model_stats.keys())
        for i in range(len(model_names)):
            for j in range(i + 1, len(model_names)):
                m1, m2 = model_names[i], model_names[j]
                s1, s2 = model_stats[m1], model_stats[m2]
                delta = abs(s1["mean"] - s2["mean"])
                if delta < 0.01:
                    delta = args.min_delta
                n_req = sample_size_two_group(s1["sd"], s2["sd"], delta)
                if n_req > max_n_needed:
                    max_n_needed = n_req

    print(f"  Max n/group needed across all model pairs: {max_n_needed}")

    # =====================================================================
    # Section 5: Token usage
    # =====================================================================
    print(f"\n{'=' * 90}")
    print("TOKEN USAGE BY MODEL")
    print("=" * 90)

    for model in sorted(models.keys()):
        model_results = models[model]
        ok_results = [r for r in model_results if r.get("error") is None]
        if not ok_results:
            print(f"  {model}: no successful trials")
            continue

        prompt_tokens = [r["tokens"]["prompt"] for r in ok_results]
        completion_tokens = [r["tokens"]["completion"] for r in ok_results]
        total_tokens = [p + c for p, c in zip(prompt_tokens, completion_tokens)]

        mean_total = statistics.mean(total_tokens)
        mean_prompt = statistics.mean(prompt_tokens)
        mean_completion = statistics.mean(completion_tokens)

        pricing = MODEL_PRICING.get(model, (0.50, 2.00))
        cost_per_trial = (
            mean_prompt / 1_000_000 * pricing[0]
            + mean_completion / 1_000_000 * pricing[1]
        )

        print(
            f"  {model:<20} avg tokens/trial: {mean_total:,.0f} (prompt: {mean_prompt:,.0f} + completion: {mean_completion:,.0f})  ~${cost_per_trial:.4f}/trial"
        )

    # =====================================================================
    # Section 6: Recommended n per cell
    # =====================================================================
    print(f"\n{'=' * 90}")
    print("RECOMMENDED SAMPLE SIZES FOR PHASE 2")
    print("=" * 90)

    total_additional = 0
    additional_by_model: dict[str, int] = defaultdict(int)

    for key in sorted(cell_stats.keys()):
        stats = cell_stats[key]
        model = key.split("|")[0]
        if stats["type"] == "det":
            recommended_n = 10  # pilot data is final
            additional = 0
        elif stats["type"] == "stoch":
            # Use min_delta for power calc
            recommended_n = sample_size_two_group(
                stats["sd"], stats["sd"], args.min_delta
            )
            recommended_n = min(recommended_n, 70)  # cap
            recommended_n = max(recommended_n, stats["n"])  # at least pilot n
            additional = max(0, recommended_n - stats["n"])
        else:
            recommended_n = 0
            additional = 0

        total_additional += additional
        additional_by_model[model] += additional

    print(
        f"  Deterministic cells: {deterministic_count} (keep at pilot n, no additional trials)"
    )
    print(f"  Stochastic cells: {stochastic_count} (top up to powered n, capped at 70)")
    print(f"  Failed cells: {failed_count}")
    print(f"\n  Total additional trials needed: {total_additional}")
    print(f"\n  By model:")
    for model in sorted(additional_by_model.keys()):
        additional = additional_by_model[model]
        pricing = MODEL_PRICING.get(model, (0.50, 2.00))
        # Rough cost: assume 10k tokens/trial average
        est_cost = additional * 10_000 / 1_000_000 * (pricing[0] + pricing[1])
        print(f"    {model:<20} +{additional} trials  ~${est_cost:.2f}")

    # =====================================================================
    # Section 7: Cross-round analysis (multi-phase trials)
    # =====================================================================
    multi_round_results = [
        r for r in results if r.get("n_rounds", 1) > 1 and r.get("rounds")
    ]
    if multi_round_results:
        print(f"\n{'=' * 90}")
        print("CROSS-ROUND ANALYSIS (multi-phase trials)")
        print("=" * 90)

        multi_cells = group_by_cell(multi_round_results)
        print(
            f"{'Cell':<55} {'R1 s/a':>7} {'R2 s/a':>7} {'R1 comp':>7} {'R2 comp':>7} {'R2 dbl':>6}"
        )
        print("-" * 90)

        for key, trials in multi_cells.items():
            short = cell_short(trials[0])
            r1_steps: list[float] = []
            r2_steps: list[float] = []
            r1_comp: list[bool] = []
            r2_comp: list[bool] = []
            r2_doubles: list[int] = []

            for t in trials:
                rounds = t.get("rounds", [])
                if len(rounds) >= 2:
                    r1_steps.append(rounds[0]["steps_per_agent"])
                    r2_steps.append(rounds[1]["steps_per_agent"])
                    r1_comp.append(rounds[0]["all_completed"])
                    r2_comp.append(rounds[1]["all_completed"])
                    r2_doubles.append(rounds[1]["double_bookings"])

            if r1_steps:
                r1_mean = statistics.mean(r1_steps)
                r2_mean = statistics.mean(r2_steps)
                r1_comp_pct = sum(r1_comp) / len(r1_comp) * 100
                r2_comp_pct = sum(r2_comp) / len(r2_comp) * 100
                r2_dbl_total = sum(r2_doubles)
                print(
                    f"{short:<55} {r1_mean:>7.2f} {r2_mean:>7.2f} {r1_comp_pct:>6.0f}% {r2_comp_pct:>6.0f}% {r2_dbl_total:>6}"
                )

        # Summary stats
        all_r1 = [
            r["rounds"][0]["steps_per_agent"]
            for r in multi_round_results
            if len(r.get("rounds", [])) >= 2
        ]
        all_r2 = [
            r["rounds"][1]["steps_per_agent"]
            for r in multi_round_results
            if len(r.get("rounds", [])) >= 2
        ]
        if all_r1 and all_r2:
            print(
                f"\n  Overall Round 1 steps/agent: {statistics.mean(all_r1):.2f} (SD={statistics.stdev(all_r1):.3f})"
                if len(all_r1) > 1
                else f"\n  Overall Round 1 steps/agent: {statistics.mean(all_r1):.2f}"
            )
            print(
                f"  Overall Round 2 steps/agent: {statistics.mean(all_r2):.2f} (SD={statistics.stdev(all_r2):.3f})"
                if len(all_r2) > 1
                else f"  Overall Round 2 steps/agent: {statistics.mean(all_r2):.2f}"
            )
            penalty = statistics.mean(all_r2) - statistics.mean(all_r1)
            print(f"  Round 2 penalty: +{penalty:.2f} steps/agent")

    # =====================================================================
    # Section 8: Safety summary
    # =====================================================================
    print(f"\n{'=' * 90}")
    print("SAFETY SUMMARY")
    print("=" * 90)

    total_trials = len(results)
    total_doubles = sum(r["double_bookings"] for r in results)
    print(f"  Total trials: {total_trials}")
    print(f"  Double bookings: {total_doubles}")
    if total_doubles == 0 and total_trials > 0:
        upper_bound = 1 - 0.05 ** (1 / total_trials)
        print(f"  95% CI on failure rate: [0, {upper_bound:.4%}]")
        pooled = total_trials + 350  # include discovery trials
        upper_pooled = 1 - 0.05 ** (1 / pooled)
        print(f"  Pooled with discovery ({pooled} trials): [0, {upper_pooled:.4%}]")
    elif total_doubles > 0:
        print(f"  WARNING: {total_doubles} safety violations detected!")


if __name__ == "__main__":
    main()
