# -*- coding: utf-8 -*-
"""
Runs a stream through today's envelope (D0) as the guard calls it, and
configuration T's normal agents, resampled from the stored trials.

The driver reads the envelope's per-agent state (_agents) to know when a
window starts and whether the detector is ready; the envelope has no public
reader for either.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from markspace.core import MarkType
from markspace.envelope import EnvelopeVerdict, StatisticalEnvelope

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
    """One agent in today's envelope, with the window bookkeeping."""

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
        """The guard's check before a write; True when the envelope refuses."""
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
