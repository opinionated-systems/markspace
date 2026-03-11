#!/usr/bin/env python3
"""Verify figures in trials/analysis.md from raw JSONL data.

This is the cross-experiment analysis covering:
- Multi-trial repeatability
- Adversarial robustness
- Scaling proportional
- Scaling contention
- Cross-experiment safety
"""

import json
import math
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
MODELS = ["gpt-oss-120b", "mercury-2"]
SEEDS = [42, 43, 44, 45, 46]
DEPTS = ["eng", "design", "product", "sales", "ops"]
PRICING = {"gpt-oss-120b": (0.15, 0.60), "mercury-2": (0.25, 0.75)}


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_all_trials(experiment):
    """Load all trial.jsonl files under results/{experiment}/."""
    base = RESULTS_DIR / experiment
    if not base.exists():
        return []
    records = []
    for tf in sorted(base.rglob("trial.jsonl")):
        for line in open(tf):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            rel = tf.relative_to(base)
            parts = list(rel.parent.parts)
            r["_model"] = parts[0] if len(parts) >= 1 else "unknown"
            r["_variant"] = parts[1] if len(parts) >= 2 else "unknown"
            r["_path"] = str(tf)
            records.append(r)
    return records


def compute_cost(tokens, model):
    p_in, p_out = PRICING.get(model, (0.50, 2.00))
    return tokens["prompt"] / 1e6 * p_in + tokens["completion"] / 1e6 * p_out


def avg(v):
    return sum(v) / len(v) if v else 0


def sd(v):
    m = avg(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0


def _t_crit_95(df):
    """Compute 95% two-tailed t critical value for given degrees of freedom."""
    try:
        from scipy.stats import t

        return t.ppf(0.975, df)
    except ImportError:
        pass
    # Hill's approximation diverges for very small df; use exact values there
    _small = {1: 12.706, 2: 4.303}
    if df in _small:
        return _small[df]
    # Hill's approximation for inverse-t from inverse-normal z and df
    z = 1.959964  # inverse normal(0.975)
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    return z + g1 / df + g2 / df**2 + g3 / df**3


def mean_ci(values):
    n = len(values)
    if n < 2:
        return avg(values), 0
    m = avg(values)
    se = sd(values) / math.sqrt(n)
    tc = _t_crit_95(n - 1)
    return m, tc * se


def completion_rate(trial):
    """Overall completion: average of per-dept manifest_completion."""
    mc = trial.get("manifest_completion", {})
    vals = list(mc.values())
    return avg(vals) * 100 if vals else 0


def completion_rate_dm(trial):
    """Overall completion from dept_metrics: completed/total."""
    dm = trial.get("dept_metrics", {})
    total = sum(d["total_items"] for d in dm.values())
    done = sum(d["completed"] for d in dm.values())
    return done / total * 100 if total else 0


def main():
    print("=" * 70)
    print("CROSS-EXPERIMENT TRIALS ANALYSIS VERIFICATION")
    print("=" * 70)

    # ================================================================
    # Section 1: Multi-Trial Repeatability
    # ================================================================
    print("\n## 1. Multi-Trial Repeatability\n")
    mt_records = load_all_trials("multi_trial")
    by_model = defaultdict(list)
    for r in mt_records:
        by_model[r["_model"]].append(r)

    print("  Completion rates:")
    print(f"  {'Model':<15} {'Mean':>7} {'95% CI':>12} {'Range':>16}")
    for model in MODELS:
        trials = by_model.get(model, [])
        if not trials:
            continue
        comps = [completion_rate(t) for t in trials]
        comps_dm = [completion_rate_dm(t) for t in trials]
        m, ci = mean_ci(comps)
        m_dm, ci_dm = mean_ci(comps_dm)
        print(
            f"  {model:<15} {m:>6.1f}% {'+/- ' + f'{ci:.1f}%':>12} "
            f"{min(comps):.1f}%-{max(comps):.1f}%"
        )
        print(
            f"    (dept_metrics) {m_dm:>6.1f}% {'+/- ' + f'{ci_dm:.1f}%':>12} "
            f"{min(comps_dm):.1f}%-{max(comps_dm):.1f}%"
        )

    # Per-seed breakdown
    print("\n  Per-seed breakdown:")
    for model in MODELS:
        trials = sorted(by_model.get(model, []), key=lambda t: t.get("seed", 0))
        print(f"\n  {model}:")
        hdr = f"    {'Seed':>6} {'Comp':>7}"
        for d in DEPTS:
            hdr += f"  {d:>8}"
        print(hdr)
        for t in trials:
            comp = completion_rate(t)
            mc = t.get("manifest_completion", {})
            row = f"    {t.get('seed', '?'):>6} {comp:>6.1f}%"
            for d in DEPTS:
                row += f"  {mc.get(d, 0) * 100:>7.1f}%"
            print(row)

    # Token efficiency
    print("\n  Token efficiency:")
    print(
        f"  {'Model':<15} {'Avg prompt/agent':>17} {'Avg comp/agent':>15} {'Avg steps/agent':>16}"
    )
    for model in MODELS:
        trials = by_model.get(model, [])
        if not trials:
            continue
        prompt_per_agent = avg(
            [t["tokens"]["prompt"] / t["total_agents"] for t in trials]
        )
        comp_per_agent = avg(
            [t["tokens"]["completion"] / t["total_agents"] for t in trials]
        )
        steps_per_agent = avg([t["total_steps"] / t["total_agents"] for t in trials])
        print(
            f"  {model:<15} {prompt_per_agent:>16,.0f} {comp_per_agent:>14,.0f} "
            f"{steps_per_agent:>15.1f}"
        )

    # ================================================================
    # Section 2: Adversarial Robustness
    # ================================================================
    print("\n## 2. Adversarial Robustness\n")
    adv_records = load_all_trials("adversarial")
    print(
        f"  {'Mode':<12} {'Model':<15} {'Comp':>7} {'Dbl':>5} {'Scope':>6} {'Denied':>7}"
    )
    for r in sorted(adv_records, key=lambda x: (x["_variant"], x["_model"])):
        comp = completion_rate(r)
        vc = r.get("verdict_counts", {})
        print(
            f"  {r['_variant']:<12} {r['_model']:<15} {comp:>6.1f}% "
            f"{r['double_bookings']:>5} {r['scope_violations']:>6} "
            f"{vc.get('denied', 0):>7}"
        )

    # Comparison to baseline
    print("\n  Comparison to multi-trial baseline:")
    for model in MODELS:
        baseline_trials = by_model.get(model, [])
        baseline_comp = (
            avg([completion_rate(t) for t in baseline_trials]) if baseline_trials else 0
        )
        adv_trials = [r for r in adv_records if r["_model"] == model]
        adv_comp = avg([completion_rate(r) for r in adv_trials]) if adv_trials else 0
        print(
            f"  {model}: baseline={baseline_comp:.1f}%, adversarial avg={adv_comp:.1f}%, "
            f"delta={adv_comp - baseline_comp:+.1f}pp"
        )

    # ================================================================
    # Section 3: Scaling Proportional
    # ================================================================
    print("\n## 3. Scaling: Proportional Resources\n")
    sp_records = load_all_trials("scaling_proportional")
    scope_map = {
        "lunch": ["lunch"],
        "equipment": ["equipment"],
        "parking": ["parking"],
        "rooms": ["rooms"],
        "tasks": ["tasks"],
    }
    if sp_records:
        sp_records.sort(key=lambda r: r.get("total_agents", 0))
        print(
            f"  {'N':>6} {'Comp':>7} {'$/agent':>8} {'Prompt/agent':>13} "
            f"{'Steps/agent':>12} {'Conflict%':>10}"
        )
        for r in sp_records:
            n = r["total_agents"]
            comp = completion_rate(r)
            cost_total = compute_cost(r["tokens"], r["_model"])
            cost_per_agent = cost_total / n if n else 0
            prompt_per_agent = r["tokens"]["prompt"] / n if n else 0
            steps_per_agent = r["total_steps"] / n if n else 0
            vc = r.get("verdict_counts", {})
            total_verdicts = sum(vc.values())
            conflict_rate = (
                vc.get("conflict", 0) / total_verdicts * 100 if total_verdicts else 0
            )
            print(
                f"  {n:>6} {comp:>6.1f}% ${cost_per_agent:>7.3f} "
                f"{prompt_per_agent / 1000:>12.0f}K {steps_per_agent:>11.1f} "
                f"{conflict_rate:>9.1f}%"
            )

        # Scaling factors
        if len(sp_records) >= 2:
            base = sp_records[0]
            base_n = base["total_agents"]
            base_prompt = base["tokens"]["prompt"] / base_n
            base_steps = base["total_steps"] / base_n
            base_cost = compute_cost(base["tokens"], base["_model"]) / base_n
            print(f"\n  Scaling factors (normalized to N={base_n}):")
            print(
                f"  {'N':>6} {'Scale':>6} {'Prompt/agent':>13} {'Steps/agent':>12} {'Cost/agent':>11}"
            )
            for r in sp_records:
                n = r["total_agents"]
                scale = n / base_n
                prompt_f = (r["tokens"]["prompt"] / n) / base_prompt
                steps_f = (r["total_steps"] / n) / base_steps
                cost_f = (compute_cost(r["tokens"], r["_model"]) / n) / base_cost
                print(
                    f"  {n:>6} {scale:>5.1f}x {prompt_f:>12.2f}x "
                    f"{steps_f:>11.2f}x {cost_f:>10.2f}x"
                )

        # Completion by resource type
        print("\n  Completion by resource type:")
        for r in sp_records:
            n = r["total_agents"]
            dm = r.get("dept_metrics", {})
            print(f"\n  N={n}:")
            resource_stats = defaultdict(lambda: {"total": 0, "completed": 0})
            for dept_data in dm.values():
                sb = dept_data.get("scope_breakdown", {})
                for scope, data in sb.items():
                    for rname, prefixes in scope_map.items():
                        if any(
                            scope == p or scope.startswith(p + "/") for p in prefixes
                        ):
                            resource_stats[rname]["total"] += data.get("total", 0)
                            resource_stats[rname]["completed"] += data.get(
                                "completed", 0
                            )
                            break
            for rname in ["lunch", "equipment", "parking", "rooms", "tasks"]:
                s = resource_stats[rname]
                rate = s["completed"] / s["total"] * 100 if s["total"] else 0
                print(f"    {rname:>12}: {rate:.1f}%")

        # Verdict breakdown
        print("\n  Verdict breakdown:")
        print(f"  {'N':>6} {'Allow':>7} {'Blocked':>8} {'Conflict':>9} {'Denied':>7}")
        for r in sp_records:
            vc = r.get("verdict_counts", {})
            print(
                f"  {r['total_agents']:>6} {vc.get('allow', 0):>7,} "
                f"{vc.get('blocked', 0):>8,} {vc.get('conflict', 0):>9,} "
                f"{vc.get('denied', 0):>7,}"
            )

        # Token breakdown
        print("\n  Token breakdown:")
        print(f"  {'N':>6} {'Total prompt':>14} {'Total comp':>12} {'Total cost':>11}")
        for r in sp_records:
            c = compute_cost(r["tokens"], r["_model"])
            print(
                f"  {r['total_agents']:>6} {r['tokens']['prompt'] / 1e6:>13.1f}M "
                f"{r['tokens']['completion'] / 1e6:>11.1f}M ${c:>10.2f}"
            )
    else:
        print("  No scaling_proportional results found.")

    # ================================================================
    # Section 4: Scaling Contention
    # ================================================================
    print("\n## 4. Scaling: Fixed Resources (Contention)\n")
    sc_records = load_all_trials("scaling_contention")
    if sc_records:
        sc_records.sort(key=lambda r: r.get("total_agents", 0))
        print(
            f"  {'N':>6} {'Comp':>7} {'$/agent':>8} {'Prompt/agent':>13} "
            f"{'Steps/agent':>12} {'Conflict%':>10}"
        )
        for r in sc_records:
            n = r["total_agents"]
            comp = completion_rate(r)
            cost_total = compute_cost(r["tokens"], r["_model"])
            cost_per_agent = cost_total / n if n else 0
            prompt_per_agent = r["tokens"]["prompt"] / n if n else 0
            steps_per_agent = r["total_steps"] / n if n else 0
            vc = r.get("verdict_counts", {})
            total_verdicts = sum(vc.values())
            conflict_rate = (
                vc.get("conflict", 0) / total_verdicts * 100 if total_verdicts else 0
            )
            print(
                f"  {n:>6} {comp:>6.1f}% ${cost_per_agent:>7.3f} "
                f"{prompt_per_agent / 1000:>12.0f}K {steps_per_agent:>11.1f} "
                f"{conflict_rate:>9.1f}%"
            )

        # Completion by resource type
        print("\n  Completion by resource type:")
        for r in sc_records:
            n = r["total_agents"]
            dm = r.get("dept_metrics", {})
            print(f"\n  N={n}:")
            resource_stats = defaultdict(lambda: {"total": 0, "completed": 0})
            for dept_data in dm.values():
                sb = dept_data.get("scope_breakdown", {})
                for scope, data in sb.items():
                    for rname, prefixes in scope_map.items():
                        if any(
                            scope == p or scope.startswith(p + "/") for p in prefixes
                        ):
                            resource_stats[rname]["total"] += data.get("total", 0)
                            resource_stats[rname]["completed"] += data.get(
                                "completed", 0
                            )
                            break
            for rname in ["lunch", "equipment", "parking", "rooms", "tasks"]:
                s = resource_stats[rname]
                rate = s["completed"] / s["total"] * 100 if s["total"] else 0
                print(f"    {rname:>12}: {rate:.1f}%")
    else:
        print("  No scaling_contention results found.")

    # ================================================================
    # Section 5: Cross-Experiment Safety
    # ================================================================
    print("\n## 5. Cross-Experiment Safety\n")
    experiments = {
        "Multi-trial (gpt-oss-120b)": [
            r for r in mt_records if r["_model"] == "gpt-oss-120b"
        ],
        "Multi-trial (mercury-2)": [
            r for r in mt_records if r["_model"] == "mercury-2"
        ],
        "Adversarial (gpt-oss-120b)": [
            r for r in adv_records if r["_model"] == "gpt-oss-120b"
        ],
        "Adversarial (mercury-2)": [
            r for r in adv_records if r["_model"] == "mercury-2"
        ],
        "Scaling contention": sc_records,
        "Scaling proportional": sp_records,
    }
    total_runs = 0
    total_agents = 0
    total_db = 0
    total_sv = 0
    print(f"  {'Experiment':<30} {'Runs':>5} {'Agents':>8} {'Dbl':>5} {'Scope':>6}")
    for name, records in experiments.items():
        runs = len(records)
        agents_sum = sum(r.get("total_agents", 0) for r in records)
        db = sum(r.get("double_bookings", 0) for r in records)
        sv = sum(r.get("scope_violations", 0) for r in records)
        total_runs += runs
        total_agents += agents_sum
        total_db += db
        total_sv += sv
        print(f"  {name:<30} {runs:>5} {agents_sum:>8,} {db:>5} {sv:>6}")
    print(
        f"  {'TOTAL':<30} {total_runs:>5} {total_agents:>8,} {total_db:>5} {total_sv:>6}"
    )


if __name__ == "__main__":
    main()
