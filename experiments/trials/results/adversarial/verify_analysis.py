#!/usr/bin/env python3
"""Verify figures in adversarial/analysis.md from raw JSONL data."""

import json
import math
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODELS = ["gpt-oss-120b", "mercury-2"]
MODES = ["confidence", "flood", "injection"]
PRICING = {"gpt-oss-120b": (0.15, 0.60), "mercury-2": (0.25, 0.75)}


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def trial_dir(model, mode):
    return HERE / model / mode


def load_trial(model, mode):
    return load_jsonl(trial_dir(model, mode) / "trial.jsonl")[0]


def load_steps(model, mode):
    return load_jsonl(trial_dir(model, mode) / "steps.jsonl")


def load_agents(model, mode):
    return load_jsonl(trial_dir(model, mode) / "agents.jsonl")


def load_rounds(model, mode):
    return load_jsonl(trial_dir(model, mode) / "rounds.jsonl")


def load_manifests(model, mode):
    return load_jsonl(trial_dir(model, mode) / "manifests.jsonl")


def compute_cost(tokens, model):
    p_in, p_out = PRICING[model]
    return tokens["prompt"] / 1e6 * p_in + tokens["completion"] / 1e6 * p_out


def avg(v):
    return sum(v) / len(v) if v else 0


def is_adversarial(agent_name):
    return agent_name.startswith("adv-")


def main():
    print("=" * 70)
    print("ADVERSARIAL ANALYSIS VERIFICATION")
    print("=" * 70)

    trials = {}
    for m in MODELS:
        for mode in MODES:
            trials[(m, mode)] = load_trial(m, mode)

    # ----------------------------------------------------------------
    # Summary table
    # ----------------------------------------------------------------
    print("\n## Summary\n")
    total_steps = sum(t["total_steps"] for t in trials.values())
    total_prompt = sum(t["tokens"]["prompt"] for t in trials.values())
    total_comp = sum(t["tokens"]["completion"] for t in trials.values())
    total_cost = sum(compute_cost(t["tokens"], m) for (m, _), t in trials.items())
    total_db = sum(t["double_bookings"] for t in trials.values())
    total_sv = sum(t["scope_violations"] for t in trials.values())

    print(f"  Safety invariant breaches: {total_db + total_sv}")
    print(f"  Double bookings: {total_db}")
    print(f"  Scope violations: {total_sv}")
    print(f"  Total steps: {total_steps:,}")
    print(
        f"  Total tokens: {total_prompt / 1e6:.1f}M prompt, {total_comp / 1e6:.2f}M completion"
    )
    print(f"  Total cost: ${total_cost:.2f}")

    print("\n  Per-trial breakdown:")
    print(f"  {'Trial':<30} {'Steps':>7} {'Prompt':>10} {'Cost':>8}")
    for model in MODELS:
        for mode in MODES:
            t = trials[(model, mode)]
            c = compute_cost(t["tokens"], model)
            print(
                f"  {model + ' / ' + mode:<30} {t['total_steps']:>7,} "
                f"{t['tokens']['prompt'] / 1e6:>9.1f}M ${c:>7.2f}"
            )

    # ----------------------------------------------------------------
    # Section 2.1: Cross-department booking denials
    # ----------------------------------------------------------------
    print("\n## 2.1 Cross-Department Booking Denials\n")
    for model in MODELS:
        for mode in MODES:
            steps = load_steps(model, mode)
            cross_dept = [s for s in steps if s["tool"] == "book_other_dept_room"]
            denied = [s for s in cross_dept if s.get("guard_verdict") == "denied"]
            print(
                f"  {model} / {mode}: {len(denied)} denied / {len(cross_dept)} attempts"
            )

    # ----------------------------------------------------------------
    # Section 2.1: Fake warning denials
    # ----------------------------------------------------------------
    print("\n## 2.1 Warning Injection Denials\n")
    for model in MODELS:
        for mode in MODES:
            steps = load_steps(model, mode)
            warnings = [s for s in steps if s["tool"] == "issue_warning"]
            denied = [s for s in warnings if s.get("guard_verdict") == "denied"]
            if warnings:
                print(
                    f"  {model} / {mode}: {len(denied)} denied / {len(warnings)} attempts"
                )

    # ----------------------------------------------------------------
    # Section 2.1: Parking share
    # ----------------------------------------------------------------
    print("\n## 2.1 Parking Share (adversarial vs expected)\n")
    for model in MODELS:
        for mode in MODES:
            steps = load_steps(model, mode)
            # Parking uses deferred resolution - look at rounds bot_log for allocations
            t = trials[(model, mode)]
            pbr = t.get("parking_by_role", {})
            print(f"  {model} / {mode}: parking_by_role = {pbr}")

    # Also compute from agents data - adversarial agent parking success
    print("\n  Adversarial parking from steps analysis:")
    for model in MODELS:
        for mode in ["confidence", "flood"]:
            steps = load_steps(model, mode)
            manifests = load_manifests(model, mode)
            # Find adversarial agents
            adv_agents = {
                m["agent"] for m in manifests if m.get("adversarial_mode") is not None
            }
            # Count parking requests by adversarial vs normal
            adv_park = sum(
                1
                for s in steps
                if s["tool"] == "request_parking" and s["agent"] in adv_agents
            )
            normal_park = sum(
                1
                for s in steps
                if s["tool"] == "request_parking" and s["agent"] not in adv_agents
            )
            total_allocated = trials[(model, mode)].get("parking_by_role", {})
            total_spots = sum(total_allocated.values())
            print(
                f"  {model} / {mode}: adv requests={adv_park}, "
                f"normal requests={normal_park}, total allocated={total_spots}"
            )

    # ----------------------------------------------------------------
    # Section 2.2: Flood behavior
    # ----------------------------------------------------------------
    print("\n## 2.2 Flood Behavior\n")
    for model in MODELS:
        mode = "flood"
        agents_data = load_agents(model, mode)
        manifests = load_manifests(model, mode)
        adv_agents = {
            m["agent"] for m in manifests if m.get("adversarial_mode") is not None
        }

        adv_steps_total = defaultdict(int)
        normal_steps_total = defaultdict(int)
        for a in agents_data:
            if a["agent"] in adv_agents:
                adv_steps_total[a["agent"]] += a["step_count"]
            else:
                normal_steps_total[a["agent"]] += a["step_count"]

        adv_avg = avg(list(adv_steps_total.values())) if adv_steps_total else 0
        normal_avg = avg(list(normal_steps_total.values())) if normal_steps_total else 0

        # Read-only percentage for adversarial
        steps = load_steps(model, mode)
        read_tools = {
            "view_dept_rooms",
            "view_shared_rooms",
            "view_equipment",
            "view_tasks",
            "view_all_rooms",
            "my_status",
        }
        adv_read = sum(
            1 for s in steps if s["agent"] in adv_agents and s["tool"] in read_tools
        )
        adv_errors = sum(
            1 for s in steps if s["agent"] in adv_agents and s["tool"] == "ERROR"
        )
        adv_total = sum(1 for s in steps if s["agent"] in adv_agents)
        read_pct = adv_read / adv_total * 100 if adv_total else 0

        print(f"  {model}:")
        print(f"    Adv avg steps/agent:    {adv_avg:.0f}")
        print(f"    Normal avg steps/agent: {normal_avg:.0f}")
        print(f"    Adv read-only %:        {read_pct:.1f}%")
        if adv_errors:
            print(f"    Adv API errors:         {adv_errors}")

    # ----------------------------------------------------------------
    # Section 3: Guard verdict distribution
    # ----------------------------------------------------------------
    print("\n## 3. Guard Verdict Distribution\n")
    for model in MODELS:
        for mode in MODES:
            steps = load_steps(model, mode)
            manifests = load_manifests(model, mode)
            adv_agents = {
                m["agent"] for m in manifests if m.get("adversarial_mode") is not None
            }

            for agent_type, label in [(True, "adversarial"), (False, "normal")]:
                filtered = [
                    s for s in steps if (s["agent"] in adv_agents) == agent_type
                ]
                total = len(filtered)
                if total == 0:
                    continue
                verdicts = defaultdict(int)
                read_only = 0
                errors = 0
                for s in filtered:
                    v = s.get("guard_verdict")
                    if v is None or v == "":
                        if s.get("tool") == "ERROR":
                            errors += 1
                        else:
                            read_only += 1
                    else:
                        verdicts[v] += 1
                print(f"  {model} / {mode} / {label} (n={total}):")
                for v in ["allow", "conflict", "blocked", "denied"]:
                    count = verdicts.get(v, 0)
                    pct = count / total * 100
                    print(f"    {v:>10}: {count:>5} ({pct:>5.1f}%)")
                pct_ro = read_only / total * 100
                print(f"    {'read-only':>10}: {read_only:>5} ({pct_ro:>5.1f}%)")
                if errors:
                    pct_err = errors / total * 100
                    print(f"    {'error':>10}: {errors:>5} ({pct_err:>5.1f}%)")

    # ----------------------------------------------------------------
    # Section 4: Normal agent completion rates
    # ----------------------------------------------------------------
    print("\n## 4. Overall Completion Rate (includes adversarial agents)\n")
    for model in MODELS:
        for mode in MODES:
            t = trials[(model, mode)]
            mc = t.get("manifest_completion", {})
            dm = t.get("dept_metrics", {})
            # From manifest_completion (avg of dept rates)
            mc_comp = avg(list(mc.values())) * 100 if mc else 0
            # From dept_metrics
            total_items = sum(d["total_items"] for d in dm.values())
            total_done = sum(d["completed"] for d in dm.values())
            dm_comp = total_done / total_items * 100 if total_items else 0
            print(
                f"  {model} / {mode}: manifest_completion={mc_comp:.1f}%, "
                f"dept_metrics={dm_comp:.1f}%"
            )

    # ----------------------------------------------------------------
    # Section 6: Mercury-2 API errors
    # ----------------------------------------------------------------
    print("\n## 6. Mercury-2 API Errors (injection trial)\n")
    if (HERE / "mercury-2" / "injection").exists():
        steps = load_steps("mercury-2", "injection")
        agents_data = load_agents("mercury-2", "injection")
        manifests = load_manifests("mercury-2", "injection")
        adv_agents = {
            m["agent"] for m in manifests if m.get("adversarial_mode") is not None
        }

        # Count errors per adversarial agent
        # Errors show up as steps with specific result patterns or missing verdicts
        # Check for agents with unusually low step counts or error results
        agent_rounds = defaultdict(set)
        for a in agents_data:
            if a["agent"] in adv_agents:
                agent_rounds[a["agent"]].add(a["round_num"])

        agent_total_steps = defaultdict(int)
        for a in agents_data:
            if a["agent"] in adv_agents:
                agent_total_steps[a["agent"]] += a["step_count"]

        print("  Adversarial agent activity (mercury-2/injection):")
        for agent in sorted(adv_agents):
            total = agent_total_steps[agent]
            rounds = len(agent_rounds[agent])
            print(f"    {agent}: {total} steps across {rounds} rounds")

        # Check trial-level for error info
        t = trials.get(("mercury-2", "injection"))
        if t:
            print(f"  Trial error field: {t.get('error', 'none')}")
            print(f"  Total agents: {t.get('total_agents', '?')}")
    else:
        print("  mercury-2/injection directory not found")


if __name__ == "__main__":
    main()
