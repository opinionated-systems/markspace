#!/usr/bin/env python3
"""Verify figures in stress_test/analysis.md from raw JSONL data."""

import json
import math
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
DEPTS = ["eng", "design", "product", "sales", "ops"]
PRICING = {"gpt-oss-120b": (0.15, 0.60)}

TOOL_RESOURCE = {
    "order_lunch": "lunch",
    "book_dept_room": "dept_rooms",
    "book_shared_room": "shared_rooms",
    "reserve_equipment": "equipment",
    "claim_task": "tasks",
    "book_boardroom": "boardroom",
    "request_parking": "parking",
    "book_other_dept_room": "cross_dept",
    "issue_warning": "warning_inject",
    "view_dept_rooms": "view",
    "view_shared_rooms": "view",
    "view_equipment": "view",
    "view_tasks": "view",
    "view_all_rooms": "view",
    "my_status": "view",
}


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_trial():
    return load_jsonl(RESULTS_DIR / "trial.jsonl")[0]


def load_steps():
    return load_jsonl(RESULTS_DIR / "steps.jsonl")


def load_agents():
    return load_jsonl(RESULTS_DIR / "agents.jsonl")


def load_rounds():
    return load_jsonl(RESULTS_DIR / "rounds.jsonl")


def avg(v):
    return sum(v) / len(v) if v else 0


def is_adversarial(name):
    return name.startswith("adv-")


def main():
    print("=" * 70)
    print("STRESS TEST ANALYSIS VERIFICATION")
    print("=" * 70)

    trial = load_trial()
    steps = load_steps()
    agents = load_agents()
    rounds = load_rounds()

    model = trial.get("model", "gpt-oss-120b")
    p_in, p_out = PRICING.get(model, (0.15, 0.60))

    # ----------------------------------------------------------------
    # Summary
    # ----------------------------------------------------------------
    print("\n## Summary\n")
    print(f"  Total agents: {trial['total_agents']}")
    print(f"  Rounds: {trial['n_rounds']}")
    print(f"  Wall clock: {trial['wall_clock_seconds']:.0f}s")
    print(f"  Total steps: {trial['total_steps']:,}")

    # ----------------------------------------------------------------
    # Section 1: Safety
    # ----------------------------------------------------------------
    print("\n## 1. Safety\n")
    vc = trial.get("verdict_counts", {})
    print(f"  Double bookings:   {trial['double_bookings']}")
    print(f"  Scope violations:  {trial['scope_violations']}")
    print(f"  Projected reads:   {trial.get('projected_reads', 0):,}")
    denied = vc.get("denied", 0)
    print(f"  DENIED verdicts:   {denied}")

    # Count adversarial denials
    adv_steps = [s for s in steps if is_adversarial(s["agent"])]
    adv_denied = sum(1 for s in adv_steps if s.get("guard_verdict") == "denied")
    cross_dept = sum(1 for s in steps if s["tool"] == "book_other_dept_room")
    cross_dept_denied = sum(
        1
        for s in steps
        if s["tool"] == "book_other_dept_room" and s.get("guard_verdict") == "denied"
    )
    warn_inject = sum(1 for s in steps if s["tool"] == "issue_warning")
    warn_denied = sum(
        1
        for s in steps
        if s["tool"] == "issue_warning" and s.get("guard_verdict") == "denied"
    )
    # Count denied verdicts by tool instead of using a fragile residual
    denied_by_tool = defaultdict(int)
    for s in steps:
        if s.get("guard_verdict") == "denied":
            denied_by_tool[s["tool"]] += 1

    print(f"  Cross-dept booking denied: {cross_dept_denied} / {cross_dept}")
    print(f"  Warning injection denied:  {warn_denied} / {warn_inject}")
    print(f"  Denied by tool:")
    for tool in sorted(denied_by_tool, key=lambda t: denied_by_tool[t], reverse=True):
        print(f"    {tool:<25} {denied_by_tool[tool]}")

    # ----------------------------------------------------------------
    # Section 2: Protocol coverage - mark types
    # ----------------------------------------------------------------
    print("\n## 2. Protocol Coverage\n")
    mc = trial.get("mark_type_counts", {})
    print("  Mark type distribution:")
    for mt in ["intent", "action", "observation", "warning", "need"]:
        print(f"    {mt:>12}: {mc.get(mt, 0):,}")

    print("\n  Verdict distribution:")
    for v in ["allow", "conflict", "blocked", "denied"]:
        print(f"    {v:>10}: {vc.get(v, 0):,}")

    print(f"\n  Projected reads: {trial.get('projected_reads', 0):,}")
    print(f"  Need marks by scope: {trial.get('need_marks_by_scope', {})}")

    # ----------------------------------------------------------------
    # Section 3.1: Resource allocation overview
    # ----------------------------------------------------------------
    print("\n## 3.1 Resource Allocation Overview\n")
    dm = trial.get("dept_metrics", {})
    # Aggregate by resource scope
    scope_totals = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})
    for dept_data in dm.values():
        sb = dept_data.get("scope_breakdown", {})
        for scope, data in sb.items():
            key = scope
            if scope.startswith("rooms/"):
                key = "dept_rooms"
            elif scope.startswith("tasks/"):
                key = "tasks"
            scope_totals[key]["total"] += data.get("total", 0)
            scope_totals[key]["completed"] += data.get("completed", 0)
            scope_totals[key]["failed"] += data.get("failed", 0)

    print(f"  {'Resource':>15} {'Supply':>8} {'Demand':>8} {'Success':>8} {'Rate':>8}")
    for resource in sorted(scope_totals):
        d = scope_totals[resource]
        demand = d["completed"] + d["failed"]
        rate = d["completed"] / demand * 100 if demand else 0
        print(f"  {resource:>15} {'':>8} {demand:>8} {d['completed']:>8} {rate:>7.0f}%")

    # Count demand from steps (tool calls)
    print("\n  Tool call counts (demand from steps.jsonl):")
    tool_counts = defaultdict(int)
    tool_success = defaultdict(int)
    for s in steps:
        tool_counts[s["tool"]] += 1
        if s.get("guard_verdict") == "allow":
            tool_success[s["tool"]] += 1
    print(f"  {'Tool':<25} {'Calls':>7} {'Success':>8} {'Rate':>6}")
    for tool in sorted(tool_counts, key=lambda t: tool_counts[t], reverse=True):
        calls = tool_counts[tool]
        success = tool_success[tool]
        rate = success / calls * 100 if calls else 0
        # Skip view tools for rate
        rate_str = (
            f"{rate:.0f}%"
            if success > 0
            or tool
            not in {
                "view_dept_rooms",
                "view_shared_rooms",
                "view_equipment",
                "view_tasks",
                "view_all_rooms",
                "my_status",
            }
            else "-"
        )
        print(f"  {tool:<25} {calls:>7} {success:>8} {rate_str:>6}")

    # ----------------------------------------------------------------
    # Section 3.2: Department completion rates
    # ----------------------------------------------------------------
    print("\n## 3.2 Department Completion Rates\n")
    for dept in DEPTS:
        d = dm.get(dept, {})
        total = d.get("total_items", 0)
        done = d.get("completed", 0)
        rate = done / total * 100 if total else 0
        print(f"  {dept:>10}: {rate:.1f}% ({done}/{total})")

    # ----------------------------------------------------------------
    # Section 3.3: Parking
    # ----------------------------------------------------------------
    print("\n## 3.3 Parking by Role\n")
    pbr = trial.get("parking_by_role", {})
    for role in ["head", "regular", "visitor"]:
        print(f"  {role:>10}: {pbr.get(role, 0)}")

    # ----------------------------------------------------------------
    # Section 3.4: Lunch preference satisfaction
    # ----------------------------------------------------------------
    print("\n## 3.4 Lunch Preference Satisfaction\n")
    lps = trial.get("lunch_preference_satisfaction", {})
    for dept in DEPTS:
        val = lps.get(dept, 0)
        print(f"  {dept:>10}: {val * 100:.1f}%")
    vals = [lps.get(d, 0) * 100 for d in DEPTS if d in lps]
    if vals:
        spread = max(vals) - min(vals)
        print(f"  {'spread':>10}: {spread:.1f}pp")

    # ----------------------------------------------------------------
    # Section 3.5: Shared rooms by department
    # ----------------------------------------------------------------
    print("\n## 3.5 Shared Rooms by Department\n")
    # Need to aggregate shared room attempts and success by department from steps
    shared_by_dept = defaultdict(lambda: {"attempts": 0, "success": 0})
    for s in steps:
        if s["tool"] == "book_shared_room":
            # Determine dept from agent name
            agent = s["agent"]
            dept = None
            for d in DEPTS:
                if agent.startswith(d + "-") or agent.startswith("adv-" + d):
                    dept = d
                    break
            if dept:
                shared_by_dept[dept]["attempts"] += 1
                if s.get("guard_verdict") == "allow":
                    shared_by_dept[dept]["success"] += 1
    for dept in DEPTS:
        d = shared_by_dept[dept]
        rate = d["success"] / d["attempts"] * 100 if d["attempts"] else 0
        print(f"  {dept:>10}: {d['success']}/{d['attempts']} ({rate:.0f}%)")

    # ----------------------------------------------------------------
    # Section 3.7: Tasks by department
    # ----------------------------------------------------------------
    print("\n## 3.7 Tasks by Department\n")
    tasks_by_dept = defaultdict(lambda: {"attempts": 0, "success": 0})
    for s in steps:
        if s["tool"] == "claim_task":
            agent = s["agent"]
            dept = None
            for d in DEPTS:
                if agent.startswith(d + "-") or agent.startswith("adv-" + d):
                    dept = d
                    break
            if dept:
                tasks_by_dept[dept]["attempts"] += 1
                if s.get("guard_verdict") == "allow":
                    tasks_by_dept[dept]["success"] += 1
    for dept in DEPTS:
        d = tasks_by_dept[dept]
        rate = d["success"] / d["attempts"] * 100 if d["attempts"] else 0
        if d["attempts"] > 0:
            print(f"  {dept:>10}: {d['success']}/{d['attempts']} ({rate:.0f}%)")

    # ----------------------------------------------------------------
    # Section 4.2: Steps per agent by department
    # ----------------------------------------------------------------
    print("\n## 4.2 Steps Per Agent by Department\n")
    agent_totals: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"total": 0, "wasted": 0}
    )
    agent_rounds: dict[tuple[str, str], set[int]] = defaultdict(set)
    for a in agents:
        name = a["agent"]
        if is_adversarial(name):
            continue
        dept = a["dept"]
        key = (dept, name)
        agent_totals[key]["total"] += a["step_count"]
        agent_totals[key]["wasted"] += a["wasted_attempts"]
        agent_rounds[key].add(a["round_num"])

    dept_avgs = defaultdict(lambda: {"steps": [], "wasted": [], "rounds": []})
    for (dept, name), data in agent_totals.items():
        dept_avgs[dept]["steps"].append(data["total"])
        dept_avgs[dept]["wasted"].append(data["wasted"])
        dept_avgs[dept]["rounds"].append(len(agent_rounds[(dept, name)]))

    print(
        f"  {'Dept':>10} {'Avg steps':>10} {'Avg wasted':>11} {'Efficiency':>11} {'Avg rounds':>11}"
    )
    for dept in DEPTS:
        d = dept_avgs[dept]
        s = avg(d["steps"])
        w = avg(d["wasted"])
        eff = (s - w) / s * 100 if s else 0
        r = avg(d["rounds"])
        print(f"  {dept:>10} {s:>10.1f} {w:>11.1f} {eff:>10.1f}% {r:>11.1f}")

    # ----------------------------------------------------------------
    # Section 4.3: Most and least active agents
    # ----------------------------------------------------------------
    print("\n## 4.3 Most and Least Active Agents\n")
    all_agent_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"steps": 0, "wasted": 0}
    )
    all_agent_rounds: dict[str, set[int]] = defaultdict(set)
    for a in agents:
        name = a["agent"]
        all_agent_totals[name]["steps"] += a["step_count"]
        all_agent_totals[name]["wasted"] += a["wasted_attempts"]
        all_agent_rounds[name].add(a["round_num"])

    sorted_agents = sorted(all_agent_totals.items(), key=lambda x: -x[1]["steps"])
    print("  Most active:")
    print(f"  {'Agent':<20} {'Steps':>6} {'Rounds':>7} {'Wasted':>7} {'Eff':>5}")
    for name, data in sorted_agents[:3]:
        eff = (
            (data["steps"] - data["wasted"]) / data["steps"] * 100
            if data["steps"]
            else 0
        )
        print(
            f"  {name:<20} {data['steps']:>6} {len(all_agent_rounds[name]):>7} "
            f"{data['wasted']:>7} {eff:>4.0f}%"
        )
    print("\n  Least active:")
    for name, data in sorted_agents[-3:]:
        eff = (
            (data["steps"] - data["wasted"]) / data["steps"] * 100
            if data["steps"]
            else 0
        )
        print(
            f"  {name:<20} {data['steps']:>6} {len(all_agent_rounds[name]):>7} "
            f"{data['wasted']:>7} {eff:>4.0f}%"
        )

    # ----------------------------------------------------------------
    # Section 4.4: Waste rate by round
    # ----------------------------------------------------------------
    print("\n## 4.4 Waste Rate by Round\n")
    for r in rounds:
        waste_rate = r["wasted_attempts"] / r["steps"] * 100 if r["steps"] else 0
        print(
            f"  Round {r['round_num']:>2} ({r['day']} {r['block']}): "
            f"{waste_rate:.0f}% ({r['wasted_attempts']}/{r['steps']})"
        )

    # ----------------------------------------------------------------
    # Section 4.5: Heads vs regular vs adversarial
    # ----------------------------------------------------------------
    print("\n## 4.5 Heads vs Regular vs Adversarial\n")
    groups = {"heads": [], "regulars": [], "adversarial": []}
    agent_group_steps = defaultdict(lambda: {"steps": 0, "wasted": 0})
    agent_group_map = {}
    for a in agents:
        name = a["agent"]
        if is_adversarial(name):
            group = "adversarial"
        elif a.get("is_head", False):
            group = "heads"
        else:
            group = "regulars"
        agent_group_map[name] = group
        agent_group_steps[name]["steps"] += a["step_count"]
        agent_group_steps[name]["wasted"] += a["wasted_attempts"]

    for name, data in agent_group_steps.items():
        group = agent_group_map[name]
        groups[group].append(data)

    print(f"  {'Group':>15} {'Count':>6} {'Avg steps':>10} {'Efficiency':>11}")
    for group in ["heads", "regulars", "adversarial"]:
        items = groups[group]
        count = len(items)
        avg_s = avg([d["steps"] for d in items])
        avg_w = avg([d["wasted"] for d in items])
        eff = (avg_s - avg_w) / avg_s * 100 if avg_s else 0
        print(f"  {group:>15} {count:>6} {avg_s:>10.1f} {eff:>10.1f}%")

    # ----------------------------------------------------------------
    # Section 5.1: Mark accumulation
    # ----------------------------------------------------------------
    print("\n## 5.1 Mark Accumulation (cumulative)\n")
    print(
        f"  {'Round':>6} {'Actions':>8} {'Intents':>8} {'Needs':>6} {'Obs':>5} {'Warn':>5}"
    )
    for r in rounds:
        mc_ = r.get("mark_counts", {})
        print(
            f"  {r['round_num']:>6} {mc_.get('action', 0):>8,} {mc_.get('intent', 0):>8,} "
            f"{mc_.get('need', 0):>6,} {mc_.get('observation', 0):>5} {mc_.get('warning', 0):>5}"
        )

    # ----------------------------------------------------------------
    # Section 6.8: Token economics
    # ----------------------------------------------------------------
    print("\n## 6.8 Token Economics\n")
    tok = trial["tokens"]
    total_tok = tok["prompt"] + tok["completion"]
    cost = tok["prompt"] / 1e6 * p_in + tok["completion"] / 1e6 * p_out
    mark_counts = trial.get("mark_type_counts", {})
    action_count = mark_counts.get("action", 0)
    per_action = total_tok / action_count if action_count else 0
    per_agent = total_tok / trial["total_agents"] if trial["total_agents"] else 0
    per_round = total_tok / trial["n_rounds"] if trial["n_rounds"] else 0

    print(f"  Total tokens:        {total_tok:,}")
    print(
        f"  Prompt tokens:       {tok['prompt']:,} ({tok['prompt'] / total_tok * 100:.1f}%)"
    )
    print(
        f"  Completion tokens:   {tok['completion']:,} ({tok['completion'] / total_tok * 100:.1f}%)"
    )
    print(f"  Per agent (avg):     {per_agent:,.0f}")
    print(f"  Per action mark:     {per_action:,.0f}")
    print(f"  Per round (avg):     {per_round:,.0f}")
    print(f"  Cost:                ${cost:.2f}")

    # Per-round token consumption
    print("\n  Per-round prompt/agent:")
    for r in rounds:
        active = r["active_agents"]
        prompt_per_agent = r["tokens"]["prompt"] / active if active else 0
        steps_per_agent = r["steps"] / active if active else 0
        print(
            f"  Round {r['round_num']:>2}: prompt/agent={prompt_per_agent:,.0f}, "
            f"steps/agent={steps_per_agent:.1f}"
        )

    # ----------------------------------------------------------------
    # Section 7: Adversarial robustness
    # ----------------------------------------------------------------
    print("\n## 7. Adversarial Robustness\n")
    adv_agents_list = set()
    for a in agents:
        if is_adversarial(a["agent"]):
            adv_agents_list.add(a["agent"])
    print(f"  Adversarial agents: {len(adv_agents_list)}")

    adv_total_steps = sum(1 for s in steps if is_adversarial(s["agent"]))
    print(f"  Total adversarial steps: {adv_total_steps}")

    # Adversarial verdicts
    adv_verdicts = defaultdict(int)
    for s in steps:
        if is_adversarial(s["agent"]):
            v = s.get("guard_verdict")
            if v:
                adv_verdicts[v] += 1
    print("\n  Adversarial verdicts:")
    for v in ["denied", "conflict", "blocked", "allow"]:
        print(f"    {v:>10}: {adv_verdicts.get(v, 0)}")

    print(f"\n  Cross-dept attempts: {cross_dept} (all denied: {cross_dept_denied})")
    print(f"  Warning injections:  {warn_inject} (all denied: {warn_denied})")


if __name__ == "__main__":
    main()
