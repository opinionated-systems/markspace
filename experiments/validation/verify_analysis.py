#!/usr/bin/env python3
"""Verify figures in validation/analysis.md from raw JSONL data."""

import json
import math
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent

MODEL_SHORT = {
    "gpt-oss-120b": "GPT-OSS",
    "deepseek-v3p2": "DeepSeek",
    "kimi-k2p5": "Kimi",
    "glm-5": "GLM-5",
    "mercury-2": "Mercury 2",
}
MODEL_ORDER = ["gpt-oss-120b", "deepseek-v3p2", "kimi-k2p5", "glm-5", "mercury-2"]
MODEL_PRICING = {
    "gpt-oss-120b": (0.15, 0.60),
    "deepseek-v3p2": (0.56, 1.68),
    "kimi-k2p5": (0.60, 3.00),
    "glm-5": (1.00, 3.20),
    "mercury-2": (0.25, 0.75),
}


def load_all():
    """Load all JSONL result files into a list of flat dicts."""
    rows = []
    for f in sorted(HERE.glob("results_*.jsonl")):
        for line in open(f):
            r = json.loads(line)
            if r.get("trial_id", 0) == -1:
                continue
            if r.get("error"):
                continue
            c = r["cell"]
            n_rounds = c.get("n_rounds", 1)
            rounds_data = r.get("rounds", [])
            row = {
                "phase": r["phase"],
                "file": f.name,
                "model": c["model"],
                "n_agents": c["n_agents"],
                "visibility": c["visibility"],
                "temperature": c["temperature"],
                "execution_mode": c["execution_mode"],
                "n_rounds": n_rounds,
                "n_slots": c.get("n_slots", 15),
                "block_self_rebook": c.get("block_self_rebook", False),
                "trial_id": r["trial_id"],
                "steps_per_agent": r["steps_per_agent"],
                "total_steps": r["total_steps"],
                "wasted_attempts": r["wasted_attempts"],
                "double_bookings": r["double_bookings"],
                "all_completed": r["all_completed"],
                "wall_seconds": r["wall_clock_seconds"],
                "prompt_tokens": r["tokens"]["prompt"],
                "completion_tokens": r["tokens"]["completion"],
            }
            pricing = MODEL_PRICING.get(c["model"], (0.50, 2.00))
            row["cost"] = (
                row["prompt_tokens"] / 1e6 * pricing[0]
                + row["completion_tokens"] / 1e6 * pricing[1]
            )
            row["waste_ratio"] = (
                r["wasted_attempts"] / r["total_steps"] if r["total_steps"] > 0 else 0.0
            )
            # Multi-phase round data
            if len(rounds_data) >= 2:
                row["r1_steps_per_agent"] = rounds_data[0]["steps_per_agent"]
                row["r2_steps_per_agent"] = rounds_data[1]["steps_per_agent"]
                row["r1_double_bookings"] = rounds_data[0]["double_bookings"]
                row["r2_double_bookings"] = rounds_data[1]["double_bookings"]
                row["r1_completed"] = rounds_data[0]["all_completed"]
                row["r2_completed"] = rounds_data[1]["all_completed"]
            else:
                row["r1_steps_per_agent"] = None
                row["r2_steps_per_agent"] = None
                row["r1_double_bookings"] = None
                row["r2_double_bookings"] = None
                row["r1_completed"] = None
                row["r2_completed"] = None
            rows.append(row)
    return rows


def avg(v):
    return sum(v) / len(v) if v else 0


def sd(v):
    m = avg(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0


def cohens_d(a, b):
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan")
    ma, mb = avg(a), avg(b)
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    pooled = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2))
    return (ma - mb) / pooled if pooled > 0 else 0


def f_oneway(*groups):
    """One-way ANOVA F-statistic and p-value (pure Python)."""
    groups = [g for g in groups if len(g) > 0]
    k = len(groups)
    if k < 2:
        return 0, 1
    ns = [len(g) for g in groups]
    n_total = sum(ns)
    grand_mean = sum(sum(g) for g in groups) / n_total
    ss_between = sum(n * (avg(g) - grand_mean) ** 2 for n, g in zip(ns, groups))
    ss_within = sum(sum((x - avg(g)) ** 2 for x in g) for g in groups)
    df_between = k - 1
    df_within = n_total - k
    if df_within <= 0 or ss_within == 0:
        return float("inf"), 0
    f_stat = (ss_between / df_between) / (ss_within / df_within)
    # Approximate p-value using F-distribution survival function
    try:
        from scipy.stats import f as f_dist

        p_val = f_dist.sf(f_stat, df_between, df_within)
    except ImportError:
        p_val = None  # scipy not available
    return f_stat, p_val


def main():
    print("=" * 70)
    print("VALIDATION ANALYSIS VERIFICATION")
    print("=" * 70)

    all_rows = load_all()
    print(
        f"\n  Loaded {len(all_rows)} trial records from "
        f"{len(set(r['file'] for r in all_rows))} files"
    )

    # ----------------------------------------------------------------
    # Overview: total trials, double bookings
    # ----------------------------------------------------------------
    print("\n## Overview\n")
    total_db = sum(r["double_bookings"] for r in all_rows)
    n = len(all_rows)
    upper_ci = 3.0 / n if n > 0 and total_db == 0 else None
    models_seen = sorted(set(r["model"] for r in all_rows))
    n_agents_seen = sorted(set(r["n_agents"] for r in all_rows))
    print(f"  Completed trials: {n}")
    print(
        f"  Models: {len(models_seen)} ({', '.join(MODEL_SHORT.get(m, m) for m in models_seen)})"
    )
    print(f"  Agent counts: {n_agents_seen}")
    print(f"  Double bookings: {total_db}")
    if upper_ci is not None:
        print(f"  95% CI on failure rate: [0, {upper_ci * 100:.2f}%]")

    # Phase breakdown
    phases = defaultdict(list)
    for r in all_rows:
        phases[r["phase"]].append(r)
    print(f"\n  By phase:")
    for phase in sorted(phases):
        rows = phases[phase]
        db = sum(r["double_bookings"] for r in rows)
        cost = sum(r["cost"] for r in rows)
        print(f"    {phase:<30} {len(rows):>5} trials, {db} dbl bookings, ${cost:.2f}")

    # ----------------------------------------------------------------
    # Single-phase: filter to n_rounds=1 or phases pilot/phase2b/phase3
    # ----------------------------------------------------------------
    single = [
        r
        for r in all_rows
        if r["phase"] in ("pilot", "phase2b", "phase3", "phase3_safety_fixed")
        or (
            r["n_rounds"] == 1
            and r["phase"]
            not in (
                "multi_phase",
                "block_self_rebook",
                "large_calendar",
                "exhaustion_5round",
            )
        )
    ]
    # Also include mercury-2 single-phase trials
    mercury_single = [
        r for r in all_rows if r["model"] == "mercury-2" and r["n_rounds"] == 1
    ]
    seen_ids = {id(r) for r in single}
    single = single + [r for r in mercury_single if id(r) not in seen_ids]

    seq = [r for r in single if r["execution_mode"] == "sequential"]
    conc = [r for r in single if r["execution_mode"] == "concurrent"]

    # ----------------------------------------------------------------
    # Section 2: Sequential ANOVA
    # ----------------------------------------------------------------
    print("\n## 2. Sequential ANOVA\n")
    if seq:
        _run_anova(seq, "Sequential")

    # ----------------------------------------------------------------
    # Section 3: Concurrent ANOVA
    # ----------------------------------------------------------------
    print("\n## 3. Concurrent ANOVA\n")
    if conc:
        _run_anova(conc, "Concurrent")

    # ----------------------------------------------------------------
    # Section 4: Concurrent steps/agent table
    # ----------------------------------------------------------------
    print("\n## 4. Concurrent Mode Steps/Agent\n")
    if conc:
        conditions = [
            (3, "visible"),
            (3, "hidden"),
            (5, "visible"),
            (5, "hidden"),
            (10, "visible"),
            (10, "hidden"),
        ]
        header = f"  {'Model':<12}"
        for n_ag, vis in conditions:
            header += f" N={n_ag} {vis[:3]:>4}"
        print(header)
        for model in MODEL_ORDER:
            row = f"  {MODEL_SHORT.get(model, model):<12}"
            for n_ag, vis in conditions:
                vals = [
                    r["steps_per_agent"]
                    for r in conc
                    if r["model"] == model
                    and r["n_agents"] == n_ag
                    and r["visibility"] == vis
                ]
                if vals:
                    row += f" {avg(vals):>8.2f}"
                else:
                    row += f" {'':>8}"
            print(row)

    # ----------------------------------------------------------------
    # Section 5: Model efficiency
    # ----------------------------------------------------------------
    print("\n## 5. Model Efficiency\n")
    print(
        f"  {'Model':<12} {'$/M in':>7} {'$/M out':>8} {'Steps/agent':>12} "
        f"{'$/trial':>8} {'Tokens/trial':>12}"
    )
    for model in MODEL_ORDER:
        model_rows = [r for r in single if r["model"] == model]
        if not model_rows:
            continue
        p_in, p_out = MODEL_PRICING.get(model, (0.50, 2.00))
        steps = avg([r["steps_per_agent"] for r in model_rows])
        cost_trial = avg([r["cost"] for r in model_rows])
        tokens_trial = avg(
            [r["prompt_tokens"] + r["completion_tokens"] for r in model_rows]
        )
        print(
            f"  {MODEL_SHORT.get(model, model):<12} ${p_in:>6.2f} ${p_out:>7.2f} "
            f"{steps:>11.2f} ${cost_trial:>7.3f} {tokens_trial:>11,.0f}"
        )

    # ----------------------------------------------------------------
    # Section 6: Scaling behavior
    # ----------------------------------------------------------------
    print("\n## 6. Scaling Behavior\n")
    conditions = [
        ("visible", "sequential"),
        ("hidden", "sequential"),
        ("visible", "concurrent"),
        ("hidden", "concurrent"),
    ]
    header = f"  {'N':>3}"
    for vis, mode in conditions:
        header += f" {vis[:3]}_{mode[:3]:>8}"
    print(header)
    for n_ag in sorted(set(r["n_agents"] for r in single)):
        row = f"  {n_ag:>3}"
        for vis, mode in conditions:
            vals = [
                r["steps_per_agent"]
                for r in single
                if r["n_agents"] == n_ag
                and r["visibility"] == vis
                and r["execution_mode"] == mode
            ]
            if vals:
                lo, hi = min(vals), max(vals)
                if abs(hi - lo) < 0.01:
                    row += f" {avg(vals):>12.2f}"
                else:
                    row += f" {lo:.2f}-{hi:.2f}"
            else:
                row += f" {'':>12}"
        print(row)

    # ----------------------------------------------------------------
    # Section 7: Waste ratio
    # ----------------------------------------------------------------
    print("\n## 7. Waste Ratio\n")
    for mode in ["sequential", "concurrent"]:
        for vis in ["visible", "hidden"]:
            for n_ag in sorted(set(r["n_agents"] for r in single)):
                vals = [
                    r["waste_ratio"]
                    for r in single
                    if r["execution_mode"] == mode
                    and r["visibility"] == vis
                    and r["n_agents"] == n_ag
                ]
                if vals:
                    lo, hi = min(vals), max(vals)
                    print(
                        f"  {mode[:3]} {vis[:3]} N={n_ag:>2}: "
                        f"{avg(vals):.2f} (range {lo:.2f}-{hi:.2f})"
                    )

    # ----------------------------------------------------------------
    # Cost breakdown
    # ----------------------------------------------------------------
    print("\n## Cost Breakdown\n")
    phase_map = {
        "pilot": "Pilot (sequential)",
        "phase2b": "Phase 2b (concurrent)",
        "phase3": "Phase 3 (stress test)",
        "phase3_safety_fixed": "Phase 3 (stress test)",
    }
    cost_by_phase = defaultdict(lambda: {"count": 0, "cost": 0})
    for r in all_rows:
        phase_label = phase_map.get(r["phase"], r["phase"])
        cost_by_phase[phase_label]["count"] += r.get("n_agents", 1)  # proxy
        cost_by_phase[phase_label]["cost"] += r["cost"]
    # Group by actual phase
    phase_trials = defaultdict(lambda: {"n": 0, "cost": 0})
    for r in all_rows:
        phase_label = phase_map.get(r["phase"], r["phase"])
        phase_trials[phase_label]["n"] += 1
        phase_trials[phase_label]["cost"] += r["cost"]
    print(f"  {'Phase':<35} {'Trials':>7} {'Cost':>8}")
    total_cost = 0
    for phase, data in sorted(phase_trials.items()):
        print(f"  {phase:<35} {data['n']:>7} ${data['cost']:>7.2f}")
        total_cost += data["cost"]
    print(f"  {'TOTAL':<35} {len(all_rows):>7} ${total_cost:>7.2f}")

    # ----------------------------------------------------------------
    # Model-level step efficiency
    # ----------------------------------------------------------------
    print("\n## Agentic Cost: Model Step Efficiency\n")
    print(
        f"  {'Model':<12} {'Mean steps':>11} {'SD':>6} {'Waste%':>7} "
        f"{'Mean wall(s)':>13}"
    )
    for model in MODEL_ORDER:
        model_rows = [r for r in single if r["model"] == model]
        if not model_rows:
            continue
        steps = [r["steps_per_agent"] for r in model_rows]
        waste = [r["waste_ratio"] * 100 for r in model_rows]
        wall = [r["wall_seconds"] for r in model_rows]
        print(
            f"  {MODEL_SHORT.get(model, model):<12} {avg(steps):>11.2f} "
            f"{sd(steps):>5.2f} {avg(waste):>6.1f}% {avg(wall):>12.2f}"
        )

    # Hardest condition: N=10, visible, concurrent, t=0.0
    print("\n  Steps/agent under hardest condition (N=10, visible, concurrent, t=0.0):")
    for model in MODEL_ORDER:
        vals = [
            r["steps_per_agent"]
            for r in conc
            if r["model"] == model
            and r["n_agents"] == 10
            and r["visibility"] == "visible"
            and r["temperature"] == 0.0
        ]
        if vals:
            print(f"    {MODEL_SHORT.get(model, model):<12} {avg(vals):.2f}")

    # ----------------------------------------------------------------
    # Multi-phase results
    # ----------------------------------------------------------------
    multi = [r for r in all_rows if r["r1_steps_per_agent"] is not None]
    if multi:
        print("\n## Multi-Phase Results\n")

        # Section 10: Cross-round safety (sequential 2-round)
        seq_multi = [
            r
            for r in multi
            if r["execution_mode"] == "sequential"
            and r["n_rounds"] == 2
            and not r.get("block_self_rebook", False)
            and r.get("n_slots", 15) == 15
        ]
        if seq_multi:
            print("  Section 10: Sequential 2-round results")
            _print_multi_table(seq_multi)

        # Section 11: Block-self-rebook
        bsr = [r for r in multi if r["phase"] == "block_self_rebook"]
        if bsr:
            print("\n  Section 11: Block-self-rebook results")
            bsr_on = [r for r in bsr if r.get("block_self_rebook", False)]
            bsr_off = [r for r in bsr if not r.get("block_self_rebook", False)]
            if bsr_on:
                print("    block_self_rebook=True:")
                _print_multi_table(bsr_on)
            if bsr_off:
                print("    block_self_rebook=False:")
                _print_multi_table(bsr_off)

        # Section 12: Concurrent multi-phase
        conc_multi = [
            r
            for r in multi
            if r["execution_mode"] == "concurrent"
            and r["n_rounds"] == 2
            and r["phase"] not in ("block_self_rebook",)
            and r.get("n_slots", 15) == 15
        ]
        if conc_multi:
            print("\n  Section 12: Concurrent 2-round results")
            _print_multi_table(conc_multi)

        # Section 13: Large calendar
        large_cal = [r for r in multi if r["phase"] == "large_calendar"]
        if large_cal:
            print("\n  Section 13: 30-slot calendar results")
            _print_multi_table(large_cal)

        # Section 14: 5-round exhaustion
        exhaust = [r for r in multi if r["phase"] == "exhaustion_5round"]
        if exhaust:
            print("\n  Section 14: 5-round exhaustion results")
            _print_multi_table(exhaust)


def _run_anova(rows, label):
    """Run Type II factorial ANOVA (matches analysis.md)."""
    print(f"  {label} ({len(rows)} trials):")
    try:
        import pandas as pd
        import statsmodels.api as sm
        from statsmodels.formula.api import ols

        df = pd.DataFrame(
            [
                {
                    "steps": r["steps_per_agent"],
                    "model": r["model"],
                    "n_agents": str(r["n_agents"]),
                    "visibility": r["visibility"],
                    "temperature": str(r["temperature"]),
                }
                for r in rows
            ]
        )
        formula = (
            "steps ~ C(model) + C(n_agents) + C(visibility) + C(temperature)"
            " + C(visibility):C(n_agents)"
        )
        model = ols(formula, data=df).fit()
        table = sm.stats.anova_lm(model, typ=2)
        name_map = {
            "C(model)": "model",
            "C(n_agents)": "n_agents",
            "C(visibility)": "visibility",
            "C(temperature)": "temperature",
            "C(visibility):C(n_agents)": "vis x n_agents",
        }
        for idx in table.index:
            if idx == "Residual":
                continue
            name = name_map.get(idx, idx)
            f_val = table.loc[idx, "F"]
            p_val = table.loc[idx, "PR(>F)"]
            sig = "Yes" if p_val < 0.05 else "No"
            print(f"    {name:<15} F={f_val:>8.2f}  p={p_val:.6f}  Sig={sig}")
    except ImportError:
        print("    WARNING: statsmodels not installed, falling back to one-way ANOVA.")
        print(
            "    One-way F-values will NOT match analysis.md (which used Type II factorial)."
        )
        # Fallback: one-way ANOVAs per factor
        factors = {
            "model": lambda r: r["model"],
            "n_agents": lambda r: r["n_agents"],
            "visibility": lambda r: r["visibility"],
            "temperature": lambda r: r["temperature"],
        }
        for name, keyfn in factors.items():
            groups = defaultdict(list)
            for r in rows:
                groups[keyfn(r)].append(r["steps_per_agent"])
            group_lists = list(groups.values())
            if len(group_lists) < 2:
                continue
            f_stat, p_val = f_oneway(*group_lists)
            if p_val is not None:
                sig = "Yes" if p_val < 0.05 else "No"
            else:
                sig = "N/A"
            p_str = f"{p_val:.6f}" if p_val is not None else "N/A"
            print(f"    {name:<15} F={f_stat:>8.2f}  p={p_str}  Sig={sig} (one-way)")

    # Cohen's d: visible vs hidden at each N
    print(f"\n    Effect sizes (Cohen's d, visible vs hidden):")
    for n_ag in sorted(set(r["n_agents"] for r in rows)):
        vis = [
            r["steps_per_agent"]
            for r in rows
            if r["n_agents"] == n_ag and r["visibility"] == "visible"
        ]
        hid = [
            r["steps_per_agent"]
            for r in rows
            if r["n_agents"] == n_ag and r["visibility"] == "hidden"
        ]
        if vis and hid:
            d = cohens_d(vis, hid)
            print(f"      N={n_ag}: d={d:.2f}")


def _print_multi_table(rows):
    """Print multi-phase condition summary."""
    conditions = defaultdict(list)
    for r in rows:
        key = (r["n_agents"], r["visibility"])
        conditions[key].append(r)

    print(
        f"    {'Condition':<25} {'Steps/agent':>12} {'Dbl bookings':>13} {'R2 comp':>8}"
    )
    for (n_ag, vis), recs in sorted(conditions.items()):
        steps = avg([r["steps_per_agent"] for r in recs])
        db = sum(r["double_bookings"] for r in recs)
        r2_comp = [r["r2_completed"] for r in recs if r["r2_completed"] is not None]
        r2_pct = sum(r2_comp) / len(r2_comp) * 100 if r2_comp else 0
        cond = f"N={n_ag} {vis}"
        print(f"    {cond:<25} {steps:>12.2f} {db:>13} {r2_pct:>7.0f}%")


if __name__ == "__main__":
    main()
