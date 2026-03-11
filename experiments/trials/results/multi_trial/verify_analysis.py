#!/usr/bin/env python3
"""Verify figures in multi_trial/analysis.md from raw JSONL data."""

import json
import math
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODELS = ["gpt-oss-120b", "mercury-2"]
SEEDS = [42, 43, 44, 45, 46]
DEPTS = ["eng", "design", "product", "sales", "ops"]
PRICING = {"gpt-oss-120b": (0.15, 0.60), "mercury-2": (0.25, 0.75)}

TOOL_RESOURCE = {
    "order_lunch": "lunch",
    "book_dept_room": "dept_rooms",
    "book_shared_room": "shared_rooms",
    "reserve_equipment": "equipment",
    "claim_task": "tasks",
    "book_boardroom": "boardroom",
    "request_parking": "parking",
}


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def trial_dir(model, seed):
    return HERE / model / f"seed_{seed}"


def load_trial(model, seed):
    return load_jsonl(trial_dir(model, seed) / "trial.jsonl")[0]


def load_steps(model, seed):
    return load_jsonl(trial_dir(model, seed) / "steps.jsonl")


def load_agents(model, seed):
    return load_jsonl(trial_dir(model, seed) / "agents.jsonl")


def load_rounds(model, seed):
    return load_jsonl(trial_dir(model, seed) / "rounds.jsonl")


def compute_cost(tokens, model):
    p_in, p_out = PRICING[model]
    return tokens["prompt"] / 1e6 * p_in + tokens["completion"] / 1e6 * p_out


def avg(v):
    return sum(v) / len(v) if v else 0


def sd(v):
    m = avg(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0


def coeff_var(v):
    return sd(v) / avg(v) * 100 if avg(v) else 0


def completion_from_dept_metrics(t):
    """Overall completion: total completed / total items across departments."""
    dm = t["dept_metrics"]
    total = sum(d["total_items"] for d in dm.values())
    done = sum(d["completed"] for d in dm.values())
    return done / total * 100 if total else 0


def completion_from_manifest(t):
    """Overall completion: average of per-department manifest_completion rates."""
    mc = t.get("manifest_completion", {})
    vals = [mc[d] for d in DEPTS if d in mc]
    return avg(vals) * 100 if vals else 0


def dept_completion(t, dept):
    dm = t["dept_metrics"].get(dept, {})
    if dm and dm["total_items"] > 0:
        return dm["completed"] / dm["total_items"] * 100
    return 0


def resource_completed_from_scopes(t, scope_prefixes):
    """Sum completed items matching scope prefixes across departments."""
    total = 0
    for dept_data in t["dept_metrics"].values():
        sb = dept_data.get("scope_breakdown", {})
        for scope, data in sb.items():
            if any(scope == p or scope.startswith(p + "/") for p in scope_prefixes):
                total += data.get("completed", 0)
    return total


def main():
    print("=" * 70)
    print("MULTI-TRIAL ANALYSIS VERIFICATION")
    print("=" * 70)

    trials = {}
    for m in MODELS:
        for s in SEEDS:
            trials[(m, s)] = load_trial(m, s)

    # ----------------------------------------------------------------
    # Summary table
    # ----------------------------------------------------------------
    print("\n## Summary Table\n")
    for model in MODELS:
        ts = [trials[(model, s)] for s in SEEDS]
        comps_dm = [completion_from_dept_metrics(t) for t in ts]
        comps_mc = [completion_from_manifest(t) for t in ts]
        steps = [t["total_steps"] for t in ts]
        costs = [compute_cost(t["tokens"], model) for t in ts]
        io_ratios = [
            t["tokens"]["prompt"] / max(t["tokens"]["completion"], 1) for t in ts
        ]
        safety = sum(t["double_bookings"] + t["scope_violations"] for t in ts)

        print(f"  {model}:")
        print(f"    total_agents (first trial): {ts[0].get('total_agents', '?')}")
        print(f"    Safety violations: {safety}")
        print(
            f"    Completion (dept_metrics): {avg(comps_dm):.1f}% (CV {coeff_var(comps_dm):.1f}%)"
        )
        print(
            f"    Completion (manifest_completion avg): {avg(comps_mc):.1f}% (CV {coeff_var(comps_mc):.1f}%)"
        )
        print(f"    Avg steps/trial: {avg(steps):,.0f}")
        print(f"    Avg cost/trial: ${avg(costs):.2f}")
        print(f"    Input:output ratio: {avg(io_ratios):.1f}:1")
        print(f"    Total cost (5 trials): ${sum(costs):.2f}")

    # ----------------------------------------------------------------
    # Section 2: Safety
    # ----------------------------------------------------------------
    print("\n## 2. Safety\n")
    for model in MODELS:
        for seed in SEEDS:
            t = trials[(model, seed)]
            print(
                f"  {model} seed={seed}: double_bookings={t['double_bookings']}, "
                f"scope_violations={t['scope_violations']}"
            )

    # ----------------------------------------------------------------
    # Section 3.1: Overall completion by seed
    # ----------------------------------------------------------------
    print("\n## 3.1 Overall Completion (from dept_metrics)\n")
    for model in MODELS:
        print(f"  {model}:")
        hdr = f"    {'Seed':>6} {'Overall':>8}"
        for d in DEPTS:
            hdr += f"  {d:>8}"
        print(hdr)
        comps = []
        for seed in SEEDS:
            t = trials[(model, seed)]
            comp = completion_from_dept_metrics(t)
            comps.append(comp)
            row = f"    {seed:>6} {comp:>7.1f}%"
            for d in DEPTS:
                row += f"  {dept_completion(t, d):>7.1f}%"
            print(row)
        print(
            f"    Mean={avg(comps):.1f}% Stdev={sd(comps):.1f}% CV={coeff_var(comps):.1f}%"
        )

    print("\n## 3.1b Overall Completion (from manifest_completion)\n")
    for model in MODELS:
        print(f"  {model}:")
        comps = []
        for seed in SEEDS:
            t = trials[(model, seed)]
            mc = t.get("manifest_completion", {})
            comp = avg([mc.get(d, 0) for d in DEPTS]) * 100
            comps.append(comp)
            parts = "  ".join(f"{d}={mc.get(d, 0) * 100:.1f}%" for d in DEPTS)
            print(f"    Seed {seed}: {comp:.1f}%  {parts}")
        print(
            f"    Mean={avg(comps):.1f}% Stdev={sd(comps):.1f}% CV={coeff_var(comps):.1f}%"
        )

    # ----------------------------------------------------------------
    # Section 3.2: By resource type
    # ----------------------------------------------------------------
    print("\n## 3.2 Resource Completion (avg successful actions per trial)\n")
    # Discover scopes
    for model in MODELS:
        all_scopes = set()
        for seed in SEEDS:
            t = trials[(model, seed)]
            for dept_data in t["dept_metrics"].values():
                all_scopes.update(dept_data.get("scope_breakdown", {}).keys())
        print(f"  {model} scopes: {sorted(all_scopes)}")

    for model in MODELS:
        print(f"\n  {model}:")
        # Aggregate per scope across departments
        scope_totals = defaultdict(lambda: {"completed": [], "total": []})
        for seed in SEEDS:
            t = trials[(model, seed)]
            seed_scope = defaultdict(lambda: {"completed": 0, "total": 0})
            for dept_data in t["dept_metrics"].values():
                sb = dept_data.get("scope_breakdown", {})
                for scope, data in sb.items():
                    seed_scope[scope]["completed"] += data.get("completed", 0)
                    seed_scope[scope]["total"] += data.get("total", 0)
            for scope, data in seed_scope.items():
                scope_totals[scope]["completed"].append(data["completed"])
                scope_totals[scope]["total"].append(data["total"])
        for scope in sorted(scope_totals):
            c = avg(scope_totals[scope]["completed"])
            t_ = avg(scope_totals[scope]["total"])
            rate = c / t_ * 100 if t_ else 0
            print(
                f"    {scope:>20}: {c:>6.0f} completed / {t_:>5.0f} total ({rate:.0f}%)"
            )

    # ----------------------------------------------------------------
    # Section 3.3: Wasted attempts by resource
    # ----------------------------------------------------------------
    print("\n## 3.3 Wasted Attempts by Resource (avg per trial)\n")
    for model in MODELS:
        print(f"  {model}:")
        waste_totals = defaultdict(list)
        for seed in SEEDS:
            steps = load_steps(model, seed)
            seed_waste = defaultdict(int)
            for s in steps:
                resource = TOOL_RESOURCE.get(s["tool"])
                if resource and s.get("guard_verdict") in ("conflict", "denied"):
                    seed_waste[resource] += 1
            for r in TOOL_RESOURCE.values():
                waste_totals[r].append(seed_waste[r])
        for r in [
            "shared_rooms",
            "dept_rooms",
            "tasks",
            "boardroom",
            "equipment",
            "lunch",
            "parking",
        ]:
            print(f"    {r:>15}: {avg(waste_totals[r]):>6.0f}")

    # ----------------------------------------------------------------
    # Section 4: Stability across seeds
    # ----------------------------------------------------------------
    print("\n## 4. Stability Across Seeds\n")
    for model in MODELS:
        ts = [trials[(model, s)] for s in SEEDS]
        sv = [t["total_steps"] for t in ts]
        wv = [t["total_wasted"] for t in ts]
        cv_ = [compute_cost(t["tokens"], model) for t in ts]
        cpv = [completion_from_dept_metrics(t) for t in ts]
        print(f"  {model}:")
        print(f"    Steps/trial:  {avg(sv):,.0f} +/- {sd(sv):,.0f}")
        print(f"    Wasted/trial: {avg(wv):,.0f} +/- {sd(wv):,.0f}")
        print(f"    Cost/trial:   ${avg(cv_):.2f} +/- ${sd(cv_):.2f}")
        print(f"    Completion:   {avg(cpv):.1f}% +/- {sd(cpv):.1f}%")

    # ----------------------------------------------------------------
    # Section 5: Round dynamics
    # ----------------------------------------------------------------
    print("\n## 5. Round Dynamics\n")
    for model in MODELS:
        am_steps, pm_steps = [], []
        am_agents, pm_agents = [], []
        am_prompt, pm_prompt = [], []
        for seed in SEEDS:
            for r in load_rounds(model, seed):
                is_am = r["block"] == "AM"
                (am_steps if is_am else pm_steps).append(r["steps"])
                (am_agents if is_am else pm_agents).append(r["active_agents"])
                (am_prompt if is_am else pm_prompt).append(r["tokens"]["prompt"])
        print(f"  {model}:")
        print(f"    AM avg steps:  {avg(am_steps):.0f}")
        print(f"    PM avg steps:  {avg(pm_steps):.0f}")
        print(f"    AM avg agents: {avg(am_agents):.0f}")
        print(f"    PM avg agents: {avg(pm_agents):.0f}")
        print(f"    AM avg prompt: {avg(am_prompt) / 1e6:.2f}M")
        print(f"    PM avg prompt: {avg(pm_prompt) / 1e6:.2f}M")

    # ----------------------------------------------------------------
    # Section 6: Token economics
    # ----------------------------------------------------------------
    print("\n## 6. Token Economics\n")
    for model in MODELS:
        ts = [trials[(model, s)] for s in SEEDS]
        avg_prompt = avg([t["tokens"]["prompt"] for t in ts])
        avg_comp = avg([t["tokens"]["completion"] for t in ts])
        p_in, p_out = PRICING[model]
        avg_c = avg([compute_cost(t["tokens"], model) for t in ts])
        in_cost = avg_prompt / 1e6 * p_in
        out_cost = avg_comp / 1e6 * p_out
        pct_in = in_cost / (in_cost + out_cost) * 100 if (in_cost + out_cost) else 0
        print(f"  {model}:")
        print(f"    Avg prompt/trial:     {avg_prompt / 1e6:.1f}M")
        print(f"    Avg completion/trial: {avg_comp / 1e6:.2f}M")
        print(f"    Input:output ratio:   {avg_prompt / max(avg_comp, 1):.1f}:1")
        print(f"    Price (in/out per M): ${p_in:.2f} / ${p_out:.2f}")
        print(f"    Avg cost/trial:       ${avg_c:.2f}")
        print(
            f"    Breakdown: ${in_cost:.2f} input ({pct_in:.0f}%) "
            f"+ ${out_cost:.2f} output ({100 - pct_in:.0f}%)"
        )

    # ----------------------------------------------------------------
    # Section 7: Model comparison
    # ----------------------------------------------------------------
    print("\n## 7. Model Comparison\n")
    for model in MODELS:
        ts = [trials[(model, s)] for s in SEEDS]
        comp = avg([completion_from_dept_metrics(t) for t in ts])
        steps_avg = avg([t["total_steps"] for t in ts])
        wasted_avg = avg([t["total_wasted"] for t in ts])
        waste_pct = wasted_avg / steps_avg * 100 if steps_avg else 0
        c = avg([compute_cost(t["tokens"], model) for t in ts])
        out_tok = avg([t["tokens"]["completion"] for t in ts])
        safety = sum(t["double_bookings"] + t["scope_violations"] for t in ts)
        # Also compute avg steps per agent
        agents_data = []
        for seed in SEEDS:
            agents = load_agents(model, seed)
            agent_steps = defaultdict(int)
            for a in agents:
                agent_steps[a["agent"]] += a["step_count"]
            agents_data.append(avg(list(agent_steps.values())) if agent_steps else 0)

        print(f"  {model}:")
        print(f"    Completion:       {comp:.1f}%")
        print(f"    Steps/trial:      {steps_avg:,.0f}")
        print(f"    Wasted:           {wasted_avg:,.0f} ({waste_pct:.1f}%)")
        print(f"    Cost/trial:       ${c:.2f}")
        print(f"    Output tokens:    {out_tok / 1e6:.2f}M")
        print(f"    Safety violations: {safety}")
        print(f"    Avg steps/agent:  {avg(agents_data):.1f}")


if __name__ == "__main__":
    main()
