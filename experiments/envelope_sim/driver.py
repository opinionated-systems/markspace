# -*- coding: utf-8 -*-
"""
Runs a stream through the envelope's detector (D0), checked before each
write as the guard checked it while it used the envelope, and configuration
T's normal agents, resampled from the gpt-oss-120b defense trials. The envelope is now a
monitor (spec 9.7); D0's verdicts restrict nothing.

The driver reads the envelope's per-agent state (_agents) to know when a
window starts and whether the detector is ready; the envelope has no public
reader for either.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Any

from markspace.core import MarkType
from markspace.envelope import EnvelopeVerdict, StatisticalEnvelope
from markspace.write_budget import WriteBudget, WriteBudgetMeter

from .streams import CHECK, Event, SimConfig, Stream, T


@dataclass
class RunResult:
    """One agent's run until its first RESTRICTED verdict, or the stream's end.
    Times are the stream's simulated seconds."""

    start: float | None  # time of the agent's first event
    restricted_at: float | None  # time of the first RESTRICTED verdict, None if never
    restricted_window: int | None  # ordinal of that envelope window
    judged_windows: int  # windows with a check while the detector was ready
    attack_first: float | None  # time of the stream's first attack write
    attack_last: float | None  # time of the stream's last attack write
    attack_writes_before: (
        int  # attack writes the envelope counted before the restriction
    )


class Run:
    """One agent in the envelope, with the window bookkeeping."""

    def __init__(self, config: SimConfig) -> None:
        self.clock = [0.0]
        self.env: StatisticalEnvelope = config.envelope(lambda: self.clock[0])
        self.agent = uuid.uuid4()
        self.window = 0  # ordinal of the current envelope window (1 = first)
        self._start: float | None = None
        self._judged: set[int] = set()
        self.first_event: float | None = None
        self.attack_writes = 0
        self.restricted_at: float | None = None
        self.restricted_window: int | None = None

    def _track(self) -> None:
        state = self.env._agents.get(self.agent)
        if state is not None and state.current_window.start != self._start:
            self._start = state.current_window.start
            self.window += 1

    def check(self, t: float) -> bool:
        """The check before a write; True when the envelope says RESTRICTED."""
        self.clock[0] = t
        if self.first_event is None:
            self.first_event = t
        verdict = self.env.check(self.agent)
        self._track()
        state = self.env._agents.get(self.agent)
        if state is not None and state.detector.is_ready():
            self._judged.add(self.window)
        if verdict == EnvelopeVerdict.RESTRICTED:
            self.restricted_at = t
            self.restricted_window = self.window
            return True
        return False

    def write(self, t: float, mark_type: MarkType, is_attack: bool) -> None:
        self.clock[0] = t
        if self.first_event is None:
            self.first_event = t
        self.env.record_attempt(self.agent, mark_type)
        self._track()
        if is_attack:
            self.attack_writes += 1

    def result(
        self, attack_first: float | None = None, attack_last: float | None = None
    ) -> RunResult:
        return RunResult(
            self.first_event,
            self.restricted_at,
            self.restricted_window,
            len(self._judged),
            attack_first,
            attack_last,
            self.attack_writes,
        )


def run_stream(stream: Stream, config: SimConfig) -> RunResult:
    attack_times = [t for t, event, is_attack in stream if is_attack]
    run = Run(config)
    for t, event, is_attack in stream:
        if event == CHECK:
            if run.check(t):
                break
        else:
            run.write(t, event, is_attack)
    return run.result(
        min(attack_times) if attack_times else None,
        max(attack_times) if attack_times else None,
    )


def round_time(r: int) -> float:
    """The stress test's clock: two rounds a day, at 8 h and 12 h (run.py)."""
    return (r // 2) * 24 * 3600.0 + (8 if r % 2 == 0 else 12) * 3600.0


def resampled_rounds(
    pool: dict[str, list[list[Event]]], rng: random.Random, n_rounds: int
) -> list[list[Event]]:
    """Configuration T: one stored normal agent, chosen at random, and n_rounds
    of its own rounds drawn with replacement, so the synthetic agent keeps that
    agent's rates."""
    agent_rounds = pool[rng.choice(sorted(pool))]
    return [rng.choice(agent_rounds) for _ in range(n_rounds)]


def run_resampled_agent(
    pool: dict[str, list[list[Event]]], rng: random.Random, n_rounds: int
) -> RunResult:
    run = Run(T)
    for r, events in enumerate(resampled_rounds(pool, rng, n_rounds)):
        t = round_time(r)
        for event in events:
            if event == CHECK:
                if run.check(t):
                    return run.result()
            else:
                run.write(t, event, False)
    return run.result()


def declared_rates(stream: Stream) -> dict[MarkType, float]:
    """The writes per hour of each type outside attacks, over the stream's
    span: what a principal who measured the agent would declare."""
    times = [t for t, event, _ in stream if event != CHECK]
    hours = max((max(times) - min(times)) / 3600.0, 1.0) if times else 1.0
    counts: dict[MarkType, int] = {}
    for _, event, is_attack in stream:
        if event != CHECK and not is_attack:
            assert isinstance(event, MarkType)  # noqa: S101
            counts[event] = counts.get(event, 0) + 1
    return {mark_type: n / hours for mark_type, n in counts.items()}


def run_write_budget(stream: Stream, budget: WriteBudget) -> dict[str, Any]:
    """One agent's writes through the write-budget meter (Section 9.14), as
    the guard meters them: a write over the spent budget is refused and not
    counted, and the agent goes on. Returns the times of the first refused
    write, the first page and the first ticket, and the attack writes
    counted before each, and the type of the first refused write."""
    meter = WriteBudgetMeter()
    agent = uuid.uuid4()
    attack_times = [t for t, _, is_attack in stream if is_attack]
    out: dict[str, Any] = {
        "start": None,
        "refused_at": None,
        "refused_type": None,
        "paged_at": None,
        "ticketed_at": None,
        "attack_first": min(attack_times) if attack_times else None,
        "attack_last": max(attack_times) if attack_times else None,
        "attack_writes_before_refusal": 0,
        "attack_writes_before_page": 0,
        "attack_writes_before_ticket": 0,
    }
    attack_writes = 0
    for t, event, is_attack in stream:
        if event == CHECK:
            continue
        assert isinstance(event, MarkType)  # noqa: S101
        if out["start"] is None:
            out["start"] = t
        reservation = meter.reserve(agent, budget, event, t)
        if reservation.refused:
            if out["refused_at"] is None:
                out["refused_at"] = t
                out["refused_type"] = event.value
                out["attack_writes_before_refusal"] = attack_writes
            continue
        for burn in meter.confirm(agent, budget, event, t):
            level = burn.tier.level  # "page" or "ticket"
            at_key = "paged_at" if level == "page" else "ticketed_at"
            if out[at_key] is None:
                out[at_key] = t
                out[f"attack_writes_before_{level}"] = attack_writes
        if is_attack:
            attack_writes += 1
    return out


def resampled_stream(
    pool: dict[str, list[list[Event]]], rng: random.Random, n_rounds: int
) -> Stream:
    """Configuration T's resampled agent as a stream (one clock value per round)."""
    out: Stream = []
    for r, events in enumerate(resampled_rounds(pool, rng, n_rounds)):
        t = round_time(r)
        for event in events:
            out.append((t, event, False))
    return out
