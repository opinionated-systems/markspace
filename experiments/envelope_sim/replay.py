# -*- coding: utf-8 -*-
"""
Replay of the gpt-oss-120b defense trials that commit 0bf9a05 ran
(experiments/trials/results/superseded/defense-gpt-oss-120b/).

Each agent's envelope events come from its tool calls in steps.jsonl and from
the end-of-round resolutions in the bot_log of rounds.jsonl, by the rules of
the code that wrote the traces (commit 0bf9a05: experiments/stress_test/run.py
and markspace/guard.py):

- a guarded booking tool runs execute() or pre_action(): one check, then one
  intent (written, or counted by record_attempt on a held action), and one
  action when the booking succeeded;
- "denied ... restricted by statistical envelope" is a check that refused the
  write; a barrier or authorization refusal, and the harness's own refusals,
  reach no envelope;
- order_lunch: one execute() (intent and action), or a need when full;
- issue_warning and report_observation: one check, then the mark;
- a parking or boardroom winner in the bot_log gets one action.

The harness also checks every agent at the end of each round (run.py
snapshot_metrics). Every write of a round has the round's one clock value,
so the replay measures round-level counts only. The stored traces stop at
each agent's first restriction: after it, an agent's attempts are refused,
and its writes are not counted.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from markspace.envelope import EnvelopeVerdict

from .streams import CHECK, A, Event, I, N, O, T, W

DEFENSE = (
    Path(__file__).resolve().parent.parent
    / "trials/results/superseded/defense-gpt-oss-120b"
)
TRIALS = (
    "rate_spike",
    "type_shift",
    "escalation",
    "probe_evasion",
    "slow_drift",
    "defense_combined",
)
GUARDED = {
    "book_dept_room",
    "book_shared_room",
    "book_other_dept_room",
    "claim_task",
    "reserve_equipment",
    "book_boardroom",
    "request_parking",
}
WINNER = (
    re.compile(r"Parking \S+: (\S+) \(conf"),
    re.compile(r"Boardroom \S+: resolved → (\S+)$"),
)


def step_events(tool: str, result: str) -> list[Event]:
    """The envelope events of one stored tool call."""
    if result.startswith(
        "BLOCKED: You must call acknowledge_canary"
    ) or result.startswith("ERROR"):
        return []
    lower = result.lower()
    barrier = "blocked by barrier" in result or "must write a need mark" in lower
    if tool in GUARDED:
        harness_refusal = (
            "does not belong" in result
            or "Cannot use this tool" in result
            or ("requires" in result and result.startswith("BLOCKED"))
            or result.startswith("FULL")
            or "already have parking" in result
        )
        if harness_refusal:
            return []
        if lower.startswith("denied"):
            return [] if "not authorized" in result or barrier else [CHECK]
        events: list[Event] = [CHECK, I]
        if result.startswith("Successfully"):
            events.append(A)
        return events
    if tool == "order_lunch":
        if "(preferred)" in result or "(fallback)" in result:
            return [CHECK, I, A]
        if result.startswith("FULL"):
            return [CHECK, N]
        return []
    if tool in ("issue_warning", "report_observation"):
        if result.startswith("DENIED"):
            return [] if barrier else [CHECK]
        return [CHECK, W if tool == "issue_warning" else O]
    return []


@dataclass
class Trial:
    name: str
    adversarial: dict[str, str | None]  # agent -> adversarial mode, None if normal
    rounds: list[int]
    steps: dict[
        tuple[int, str], list[tuple[str, str]]
    ]  # (round, agent) -> (tool, result)
    bot_winners: dict[int, list[str]]  # round -> agents that got an action
    stored_first: dict[str, int]  # agent -> round of its first stored restriction
    lost: set[tuple[str, int]] = field(
        default_factory=set
    )  # agent-rounds with an ERROR step


def load_trial(name: str, root: Path = DEFENSE) -> Trial:
    d = root / name
    adversarial = {
        m["agent"]: m["adversarial_mode"]
        for m in map(json.loads, (d / "manifests.jsonl").read_text().splitlines())
    }
    steps: dict[tuple[int, str], list[tuple[str, str]]] = defaultdict(list)
    lost: set[tuple[str, int]] = set()
    for s in map(json.loads, (d / "steps.jsonl").read_text().splitlines()):
        if s["tool"] == "ERROR":
            lost.add((s["agent"], s["round_num"]))
        steps[(s["round_num"], s["agent"])].append((s["tool"], s["result"]))
    bot_winners: dict[int, list[str]] = {}
    rounds = []
    for r in map(json.loads, (d / "rounds.jsonl").read_text().splitlines()):
        rounds.append(r["round_num"])
        winners = []
        for line in r["bot_log"]:
            for pattern in WINNER:
                m = pattern.match(line)
                if m and m.group(1) in adversarial:
                    winners.append(m.group(1))
        bot_winners[r["round_num"]] = winners
    stored_first: dict[str, int] = {}
    for a in map(json.loads, (d / "agents.jsonl").read_text().splitlines()):
        if a.get("envelope_verdict") == "restricted":
            stored_first.setdefault(a["agent"], a["round_num"])
    return Trial(
        name, adversarial, sorted(rounds), dict(steps), bot_winners, stored_first, lost
    )


def round_events(trial: Trial, agent: str, round_num: int) -> list[Event]:
    """One agent's events in one round: its tool calls, then its resolution
    actions, then the harness's end-of-round check."""
    events: list[Event] = []
    for tool, result in trial.steps.get((round_num, agent), []):
        events += step_events(tool, result)
    events += [A] * trial.bot_winners[round_num].count(agent)
    return events + [CHECK]


def replay_d0(trial: Trial) -> dict[str, int]:
    """Each agent's first RESTRICTED round under the envelope's default detector (D0) with the
    trials' configuration, as the stress test ran it: all agents' tool calls of
    a round, then the resolutions, then the end-of-round checks."""
    clock = [0.0]
    env = T.envelope(lambda: clock[0])
    ids = {agent: uuid.uuid5(uuid.NAMESPACE_DNS, agent) for agent in trial.adversarial}
    first: dict[str, int] = {}

    def apply(agent: str, event: Event, r: int) -> None:
        if event == CHECK:
            if env.check(ids[agent]) == EnvelopeVerdict.RESTRICTED:
                first.setdefault(agent, r)
        elif agent not in first:
            env.record_attempt(ids[agent], event)

    for r in trial.rounds:
        clock[0] = r * T.window_seconds
        for (step_round, agent), calls in trial.steps.items():
            if step_round != r:
                continue
            for tool, result in calls:
                for event in step_events(tool, result):
                    apply(agent, event, r)
        for agent in trial.bot_winners[r]:
            apply(agent, A, r)
        for agent in trial.adversarial:
            apply(agent, CHECK, r)
    return first


def normal_rounds(trial: Trial) -> dict[str, list[list[Event]]]:
    """Each normal agent's rounds up to and including its stored first
    restriction (its writes there come before the refused check; after it,
    the stored round holds refused attempts, which count nothing), with no
    lost round: the material for configuration T's normal profile."""
    out: dict[str, list[list[Event]]] = {}
    for agent, mode in trial.adversarial.items():
        if mode is not None:
            continue
        stop = trial.stored_first.get(agent, max(trial.rounds) + 1)
        rounds = [
            round_events(trial, agent, r)
            for r in trial.rounds
            if r <= stop and (agent, r) not in trial.lost
        ]
        if rounds:
            out[f"{trial.name}/{agent}"] = rounds
    return out
