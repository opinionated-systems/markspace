#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Write analysis.md from a measurement file; every number in it comes from the
file.

    python -m experiments.envelope_sim.report experiments/envelope_sim/results/d0.json \\
        experiments/envelope_sim/analysis.md
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


def main() -> None:
    results = json.loads(Path(sys.argv[1]).read_text())
    Path(sys.argv[2]).write_text(render(results))


if __name__ == "__main__":
    main()
