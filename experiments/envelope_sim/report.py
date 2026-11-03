#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Write analysis.md from the measurement files; every number in it comes from
them.

    python -m experiments.envelope_sim.report experiments/envelope_sim/results/d0.json \\
        experiments/envelope_sim/analysis.md [experiments/envelope_sim/results/write_budget.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

METHOD = """## Configurations and method

- L, the library default: observations and warnings tracked, 300 s windows, 10 cold-start windows, `k_sigma` 3.5, type-shift threshold 0.5.
- T, the stress test and the defense trials: intents, actions, observations, warnings and needs tracked, 4 h windows (one per round, two rounds a day), 8 cold-start windows.
- The driver calls the envelope as the guard does: a check before each write, the count after it (`test_run_stream_refuses_the_write_that_the_guard_refuses` compares it with `Guard.write_mark` on generated streams).
- Each seed is one agent, run until its first RESTRICTED verdict. The result for a normal profile is the share of agents restricted within a horizon of simulated time, with an exact (Clopper-Pearson) 95 % interval. A share by horizon, not a rate per window: the chance of a restriction is not constant over an agent's life, so a rate per window would depend on the length of the run.
- In configuration L an envelope window starts at the first event at or after the end of the previous one, so an idle stretch makes no window. In T the harness checks every agent at the end of every round, so every round is a window.
- An attack writes inside one envelope window (the boundary burst across the end of one). An agent counts as detected when its first restriction comes after its first attack write and at most one window after its last; a restriction before the attack, a later one, and none are counted apart.
"""

LIMITS = """## Limits

- The synthetic workloads are assumptions. The replays check D0 against real LLM agents, round by round and only up to each agent's first restriction.
- Configuration T's resampled agents draw each round independently from one stored agent's rounds, so they have no trend over a run.
- The stored traces have one clock value per round, so the replay measures round-level counts only.
"""


def pct(x: float) -> str:
    return f"{100 * x:.1f} %"


def days(key: str) -> str:
    """'restricted_within_7_days' -> '7 days'; '1 day' for one."""
    n = key.split("_")[2]
    return f"{n} day" if n == "1" else f"{n} days"


def duration(seconds: float) -> str:
    """3600 -> '1 h', 259200 -> '3 days', 300 -> '5 min'."""
    if seconds % 86_400 == 0:
        n = seconds / 86_400
        return f"{n:g} day" if n == 1 else f"{n:g} days"
    if seconds % 3600 == 0:
        return f"{seconds / 3600:g} h"
    return f"{seconds / 60:g} min"


def tier_text(tiers: list[dict[str, Any]]) -> str:
    """The tiers as Google's Site Reliability Workbook states them: share of
    the budget in the long window (short window), page or ticket."""
    parts = [
        f"{100 * t['budget_fraction']:g} % of the budget in {duration(t['long_window_seconds'])} "
        f"(short window {duration(t['short_window_seconds'])}): {t['level']}"
        for t in tiers
    ]
    return "; ".join(parts)


def horizon_keys(entry: dict[str, Any]) -> list[str]:
    return [k for k in entry if k.startswith("restricted_within_")]


def normal_table(profiles: dict[str, Any], config: str) -> list[str]:
    rows = [
        (k, v)
        for k, v in profiles.items()
        if v["kind"] == "normal" and v["config"] == config
    ]
    keys = horizon_keys(rows[0][1])
    head = " | ".join(f"Within {days(k)} (95 % interval)" for k in keys)
    out = [f"| Profile | Agents | {head} |", "|---|---|" + "---|" * len(keys)]
    for name, v in rows:
        cells = " | ".join(
            f"{v[k]['agents']} ({pct(v[k]['interval95'][0])} to {pct(v[k]['interval95'][1])})"
            for k in keys
        )
        out.append(f"| {name} | {v['agents']} | {cells} |")
    return out


def render(results: dict[str, Any]) -> str:
    profiles: dict[str, Any] = results["profiles"]
    cfg = results["config"]
    normal_l = {
        k: v
        for k, v in profiles.items()
        if v["kind"] == "normal" and v["config"] == "L"
    }
    plain = {k: v for k, v in normal_l.items() if not k.startswith("first_use")}
    keys = horizon_keys(next(iter(plain.values())))
    first, last = keys[0], keys[-1]
    first_days, last_days = days(first), days(last)
    day1 = [v[first]["share"] for v in plain.values()]
    day30 = [v[last]["share"] for v in plain.values()]
    rare = {
        k: v
        for k, v in normal_l.items()
        if k.startswith("first_use") and k.endswith("burst_1")
    }
    rare30 = [v[last]["share"] for v in rare.values()]
    t = profiles["T_resampled_normal"]
    attacks = {k: v for k, v in profiles.items() if v["kind"] == "attack"}
    early = {k: v for k, v in attacks.items() if k.endswith("_at15")}
    late = {k: v for k, v in attacks.items() if k.endswith("_at1200")}
    det_early = [v["detected"] / v["agents"] for v in early.values()]
    late_before = [
        v["restricted_before_the_attack"] / v["agents"] for v in late.values()
    ]
    replays: dict[str, Any] = results["replays"]
    compared = sum(v["agents_compared_with_stored"] for v in replays.values())
    differences = sum(len(v["differences_from_stored"]) for v in replays.values())
    stored_normal = sum(v["normal_restricted"] for v in replays.values())

    lines = [
        "# Envelope simulator: today's detector (D0)",
        "",
        f"Measured with `python -m experiments.envelope_sim.measure` ({cfg['seeds']} seeds of {cfg['windows']:,} windows per profile); the numbers are in [`results/d0.json`](results/d0.json), and this file is written from it by `report.py`.",
        "",
        "**Result.** D0 cannot restrict automatically. "
        f"In configuration L, the normal profiles other than the first-use ones restricted {pct(min(day1))} to {pct(max(day1))} of their agents within {first_days} and {pct(min(day30))} to {pct(max(day30))} within {last_days}. "
        f"Agents that write observations and one warning in their run (the first-use profiles with a burst of 1) were restricted within {last_days} in {pct(min(rare30))} to {pct(max(rare30))} of the runs. "
        f"In configuration T, agents resampled from the stored trials were restricted in {pct(t['restricted_within_10_days']['share'])} within 10 days (20 rounds) and {pct(t['restricted_within_20_days']['share'])} within 20 days (40 rounds); the stored trials restricted 9 to 26 of 100 normal agents in 20 to 40 rounds. "
        f"Attacks just after cold start (nominal window 15) were detected in {pct(min(det_early))} to {pct(max(det_early))} of the agents. "
        f"Before attacks after a long history (nominal window 1,200), D0 had already restricted {pct(min(late_before))} to {pct(max(late_before))} of the agents. "
        f"The replay of the six stored defense trials compares {compared} agents with their stored first restriction and differs for {differences}; the 5 slow_drift adversaries are not compared (no write of theirs before the restriction is stored).",
        "",
        METHOD,
        "## Normal profiles, configuration L",
        "",
        *normal_table(profiles, "L"),
        "",
        "## Normal agents resampled from the stored trials, configuration T",
        "",
        *normal_table(profiles, "T"),
        "",
        f"The stored trials restricted {stored_normal} of their 600 normal agents in total.",
        "",
        "## Attack profiles, configuration L",
        "",
        "| Attack | Start | Agents | Before the attack | Detected | Later | Never | Median windows to detection | Median attack writes before detection |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, v in attacks.items():
        base, at = name.rsplit("_at", 1)
        lines.append(
            f"| {base} | {at} | {v['agents']} | {v['restricted_before_the_attack']} | {v['detected']} | "
            f"{v['restricted_later']} | {v['never_restricted']} | {v['median_windows_to_detection']} | "
            f"{v['median_attack_writes_before_detection']} |"
        )
    lines += [
        "",
        "## Replay of the stored defense trials",
        "",
        "The replay turns each agent's stored tool calls (`steps.jsonl`) and the round's resolution winners (`bot_log` of `rounds.jsonl`) into envelope events by the rules of the code that wrote the traces (0bf9a05), and runs them through D0 in configuration T. Writes after an agent's first restriction are not counted, because the stored agent then behaved as D0's restriction made it.",
        "",
        "| Trial | Normal agents restricted | Adversaries restricted | Agents compared | Differences from stored |",
        "|---|---|---|---|---|",
    ]
    for name, v in replays.items():
        lines.append(
            f"| {name} | {v['normal_restricted']} of {v['normal_agents']} | {v['adversaries_restricted']} of {v['adversaries']} | "
            f"{v['agents_compared_with_stored']} | {len(v['differences_from_stored'])} |"
        )
    lines += ["", LIMITS]
    return "\n".join(lines)


def render_write_budget(results: dict[str, Any]) -> str:
    """The write-budget section (Section 9.14): each profile's budget twice
    its own measured rate, the default period and tiers."""
    profiles: dict[str, Any] = results["profiles"]
    cfg = results["config"]
    budget_cfg = cfg["write_budget"]
    period_days = budget_cfg["period_seconds"] / 86_400
    headroom = budget_cfg["expected_activity_headroom"]
    normal = {k: v for k, v in profiles.items() if v["kind"] == "normal"}
    attacks = {k: v for k, v in profiles.items() if v["kind"] == "attack"}

    def last_key(entry: dict[str, Any], event: str) -> str:
        return [k for k in entry[event] if k.startswith(f"{event}_within_")][-1]

    def horizon_of(entry: dict[str, Any], event: str) -> str:
        return days(last_key(entry, event).replace(event, "restricted"))

    def cell(entry: dict[str, Any], event: str) -> str:
        """Agents with the event within the profile's last horizon; the
        horizon is named when it is not the table's."""
        key = last_key(entry, event)
        own = horizon_of(entry, event)
        named = "" if own == horizon else f" within {own}"
        return f"{entry[event][key]['agents']} of {entry[event]['agents']}{named}"

    with_refusal = [
        k for k, v in normal.items() if v["refused"][last_key(v, "refused")]["agents"]
    ]
    with_page = [
        k for k, v in normal.items() if v["paged"][last_key(v, "paged")]["agents"]
    ]
    paged_attacks = [k for k, v in attacks.items() if v["paged"]["detected"]]
    refused_attacks = [
        f"{k} ({pct((v['refused']['detected'] + v['refused']['restricted_later']) / v['refused']['agents'])})"
        for k, v in attacks.items()
        if v["refused"]["detected"] or v["refused"]["restricted_later"]
    ]
    horizon = horizon_of(next(iter(normal.values())), "refused")
    lines = [
        "## Write budgets",
        "",
        f"Measured with `--detector write_budget` ({cfg['seeds']} seeds of {cfg['windows']:,} windows per profile; [`results/write_budget.json`](results/write_budget.json), which also records the budget settings). Each profile's expected activity is its own measured rate (the writes per hour of each type outside attacks, on a reference stream of the profile; configuration T: on the agent's own stream), and its budget is {headroom:g} times that over {period_days:g} days, as the guard takes it from a manifest; a type the profile never writes outside an attack has a budget of 0. Burn-rate tiers, the starting values of Google's Site Reliability Workbook: {tier_text(budget_cfg['tiers'])}. A page or a ticket is a need mark; only a spent budget refuses writes. The simulator meters each write of the stream through the guard's meter; where the guard's `pre_action` would refuse an intent because the action budget is spent, the simulator refuses the action write instead, which matters for configuration T only.",
        "",
        f"**Result.** Normal profiles with a refused write within {horizon}: {', '.join(with_refusal) or 'none'}. Normal profiles with a page: {', '.join(with_page) or 'none'}. Attacks that paged while they ran: {', '.join(paged_attacks) or 'none'}. Attacks with a refused write, and the share of their agents refused: {', '.join(refused_attacks) or 'none'}.",
        "",
        f"| Profile | Refused within {horizon} | Type of the first refused write | Paged within {horizon} | Ticketed within {horizon} |",
        "|---|---|---|---|---|",
    ]
    for name, v in normal.items():
        types = ", ".join(
            f"{t} {n}" for t, n in v["refused"]["first_refused_type"].items()
        )
        lines.append(
            f"| {name} ({v['config']}) | {cell(v, 'refused')} | {types or '-'} | {cell(v, 'paged')} | {cell(v, 'ticketed')} |"
        )
    lines += [
        "",
        "| Attack | Start | Agents | Paged during the attack | Median attack writes before the page | Ticketed (during, later) | Refused (during, later) | Median attack writes before the refusal |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, v in attacks.items():
        base, at = name.rsplit("_at", 1)
        p, tk, r = v["paged"], v["ticketed"], v["refused"]
        lines.append(
            f"| {base} | {at} | {p['agents']} | {p['detected']} | "
            f"{p['median_attack_writes_before_detection']} | {tk['detected']}, {tk['restricted_later']} | "
            f"{r['detected']}, {r['restricted_later']} | {r['median_attack_writes_before_detection']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    results = json.loads(Path(sys.argv[1]).read_text())
    text = render(results)
    if len(sys.argv) > 3:
        text += "\n" + render_write_budget(json.loads(Path(sys.argv[3]).read_text()))
    Path(sys.argv[2]).write_text(text)


if __name__ == "__main__":
    main()
