# -*- coding: utf-8 -*-
"""
Mark streams for the envelope simulator.

A stream is one agent's envelope events in time order (spec 9.7): CHECK is
the envelope's check before a write, as the guard made it while it used the
envelope; a MarkType is the write that the envelope counts after it (a write
hook, or record_attempt). Each item also says
whether the write belongs to an attack, so that the driver can count the
harmful writes before a restriction.

Two configurations: L, the library default (observations and warnings, 300 s
windows, 10 cold-start windows); T, the stress test and the defense trials
(five tracked types, 4 h windows, 8 cold-start windows, one window per round;
experiments/stress_test/run.py).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from markspace.core import MarkType
from markspace.envelope import (
    EnvelopeConfig,
    StatisticalEnvelope,
    WelfordConfig,
    WelfordDetector,
)

O, W = MarkType.OBSERVATION, MarkType.WARNING
I, A, N = MarkType.INTENT, MarkType.ACTION, MarkType.NEED

CHECK: Literal["check"] = "check"
Event = Literal["check"] | MarkType
Item = tuple[float, Event, bool]  # (time, event, part of an attack)
Stream = list[Item]


@dataclass(frozen=True)
class SimConfig:
    """An envelope configuration that markspace uses."""

    name: str
    window_seconds: float
    tracked: frozenset[MarkType]
    min_samples: int

    def envelope(self, clock: Callable[[], float]) -> StatisticalEnvelope:
        """The envelope with its default detector (D0) in this configuration."""
        welford = WelfordConfig(
            k_sigma=3.5,
            min_samples=self.min_samples,
            type_shift_threshold=0.5,
            tracked_types=self.tracked,
        )
        return StatisticalEnvelope(
            config=EnvelopeConfig(
                window_seconds=self.window_seconds,
                detector_factory=lambda _agent_id: WelfordDetector(welford),
                tracked_types=self.tracked,
            ),
            clock=clock,
        )


L = SimConfig("L", 300.0, frozenset({O, W}), 10)
T = SimConfig("T", 4 * 3600.0, frozenset({I, A, O, W, N}), 8)
CONFIGS = {"L": L, "T": T}


def poisson(rng: random.Random, lam: float) -> int:
    """A Poisson draw by inversion (exact up to lam 500; above it the normal
    approximation, which only the ramp attack reaches)."""
    if lam <= 0.0:
        return 0
    if lam > 500.0:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    u = rng.random()
    k = 0
    p = math.exp(-lam)
    cdf = p
    while u > cdf:
        k += 1
        p *= lam / k
        cdf += p
        if p == 0.0 and cdf < u:  # underflow far in the tail
            break
    return k


def neg_binomial(rng: random.Random, mean: float, variance: float) -> int:
    """A negative binomial draw as a Gamma-Poisson mixture (variance > mean)."""
    shape = mean * mean / (variance - mean)
    return poisson(rng, rng.gammavariate(shape, mean / shape))


def window_items(
    rng: random.Random,
    start: float,
    length: float,
    counts: dict[MarkType, int],
    attack: dict[MarkType, int] | None = None,
) -> Stream:
    """The writes of one window at uniform random times, each checked first."""
    marks: list[tuple[float, MarkType, bool]] = []
    for mark_type, n in counts.items():
        marks += [(start + rng.random() * length, mark_type, False) for _ in range(n)]
    for mark_type, n in (attack or {}).items():
        marks += [(start + rng.random() * length, mark_type, True) for _ in range(n)]
    marks.sort(key=lambda m: m[0])
    items: Stream = []
    for t, mark_type, is_attack in marks:
        items.append((t, CHECK, False))
        items.append((t, mark_type, is_attack))
    return items


def window_starts(stream: Stream, window_seconds: float) -> list[float]:
    """The start of each envelope window in a stream: a window starts at the
    first event at or after the end of the previous one (half-open windows, as
    StatisticalEnvelope rotates them)."""
    starts: list[float] = []
    for t in sorted(item[0] for item in stream):
        if not starts or t >= starts[-1] + window_seconds:
            starts.append(t)
    return starts


def window_at(stream: Stream, window_seconds: float, nominal: float) -> float:
    """The start of the envelope window that holds time `nominal`, or of the
    next one if `nominal` falls in an idle gap."""
    starts = window_starts(stream, window_seconds)
    before = [s for s in starts if s <= nominal]
    if before and before[-1] + window_seconds > nominal:
        return before[-1]
    return min(s for s in starts if s > nominal)


def burst(
    start: float, mark_type: MarkType, n: int, is_attack: bool, gap: float = 2.0
) -> Stream:
    """n writes `gap` seconds apart from `start`, each checked first."""
    out: Stream = []
    for k in range(n):
        out += [
            (start + gap * k, CHECK, False),
            (start + gap * k, mark_type, is_attack),
        ]
    return out


def with_burst(stream: Stream, extra: Stream) -> Stream:
    return sorted(stream + extra, key=lambda item: item[0])


def burst_at(stream: Stream, nominal: float, make: Callable[[float], Stream]) -> Stream:
    """The stream with the burst make(start) added at the start of the envelope
    window that holds time `nominal` (or the next one); unchanged when the
    stream ends before `nominal` (a short --quick run)."""
    if not stream or nominal >= stream[-1][0]:
        return stream
    return with_burst(stream, make(window_at(stream, L.window_seconds, nominal)))


# ---------------------------------------------------------------------------
# Normal profiles, configuration L
# ---------------------------------------------------------------------------


def steady(obs: float, warn: float) -> Callable[[random.Random, int], Stream]:
    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        out: Stream = []
        for i in range(n_windows):
            counts = {O: poisson(rng, obs), W: poisson(rng, warn)}
            out += window_items(rng, i * ws, ws, counts)
        return out

    return gen


def overdispersed(rng: random.Random, n_windows: int) -> Stream:
    """Negative binomial counts, variance three times the mean."""
    ws = L.window_seconds
    out: Stream = []
    for i in range(n_windows):
        counts = {O: neg_binomial(rng, 5.0, 15.0), W: neg_binomial(rng, 1.0, 3.0)}
        out += window_items(rng, i * ws, ws, counts)
    return out


def batches(
    n_obs: int, n_warn: int, obs_gap: float, idle: tuple[float, float]
) -> Callable[[random.Random, int], Stream]:
    """Batches: observations about evenly spaced (each gap within 20 %),
    a pause of 5 to 60 s, the warnings 2 s apart, then idle."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        end = n_windows * L.window_seconds
        t = 0.0
        out: Stream = []
        while t < end:
            for _ in range(n_obs):
                t += obs_gap * rng.uniform(0.8, 1.2)
                out += [(t, CHECK, False), (t, O, False)]
            t += rng.uniform(5.0, 60.0)
            for k in range(n_warn):
                t += 2.0 if k else 0.0
                out += [(t, CHECK, False), (t, W, False)]
            t += rng.uniform(*idle)
        return out

    return gen


def rare_warning(rng: random.Random, n_windows: int) -> Stream:
    """One warning in every fifth window."""
    ws = L.window_seconds
    out: Stream = []
    for i in range(n_windows):
        counts = {O: poisson(rng, 5.0), W: 1 if i % 5 == 4 else 0}
        out += window_items(rng, i * ws, ws, counts)
    return out


def intermittent(rng: random.Random, n_windows: int) -> Stream:
    """Six warnings in 40 % of the windows (a cold-start case)."""
    ws = L.window_seconds
    out: Stream = []
    for i in range(n_windows):
        counts = {O: poisson(rng, 10.0), W: 6 if rng.random() < 0.4 else 0}
        out += window_items(rng, i * ws, ws, counts)
    return out


def first_use(after: int, size: int) -> Callable[[random.Random, int], Stream]:
    """No warning until nominal window `after`, then `size` warnings 2 s apart
    inside the envelope window that holds it."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        out: Stream = []
        for i in range(n_windows):
            out += window_items(rng, i * ws, ws, {O: poisson(rng, 5.0)})
        return burst_at(out, after * ws, lambda s: burst(s + 1.0, W, size, False))

    return gen


def idle_gaps(rng: random.Random, n_windows: int) -> Stream:
    """Steady writes with idle stretches of 1 to 20 windows."""
    ws = L.window_seconds
    out: Stream = []
    i = 0
    while i < n_windows:
        if rng.random() < 0.05:
            i += rng.randint(1, 20)
            continue
        out += window_items(
            rng, i * ws, ws, {O: poisson(rng, 5.0), W: poisson(rng, 1.0)}
        )
        i += 1
    return out


def daily(start_in_peak: bool) -> Callable[[random.Random, int], Stream]:
    """5 observations and 1 warning per window for 20 h, three times that in a
    4 h peak; cold start in or out of the peak."""
    per_day = int(24 * 3600 / L.window_seconds)
    peak = int(4 * 3600 / L.window_seconds)

    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        offset = 0 if start_in_peak else peak
        out: Stream = []
        for i in range(n_windows):
            scale = 3.0 if (i + offset) % per_day < peak else 1.0
            counts = {O: poisson(rng, 5.0 * scale), W: poisson(rng, 1.0 * scale)}
            out += window_items(rng, i * ws, ws, counts)
        return out

    return gen


# ---------------------------------------------------------------------------
# Attack profiles, configuration L; observations stay at 5 per window. Each
# attack starts at nominal window `at`: ATTACK_STARTS has one just after cold
# start and one after a long history. A one-window attack is placed inside one
# envelope window (window_at), because the envelope's windows start at the
# first event after the previous one ended, not on the 300 s grid.
# ---------------------------------------------------------------------------

ATTACK_STARTS = (15, 1200)
Generator = Callable[[random.Random, int], Stream]


def _base(rng: random.Random, n_windows: int, warn: float) -> Stream:
    ws = L.window_seconds
    out: Stream = []
    for i in range(n_windows):
        out += window_items(
            rng, i * ws, ws, {O: poisson(rng, 5.0), W: poisson(rng, warn)}
        )
    return out


def flood(extra: int, own: float, at: int) -> Generator:
    """`extra` warnings above the agent's own rate, 2 s apart, inside the
    envelope window that holds nominal window `at`."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        out = _base(rng, n_windows, own)
        return burst_at(
            out, at * L.window_seconds, lambda s: burst(s + 1.0, W, extra, True)
        )

    return gen


def flood_then_silence(at: int) -> Generator:
    """10 warnings above a rate of 1, 2 s apart, inside one envelope window,
    then no write for 5 nominal windows."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        out = _base(rng, n_windows, 1.0)
        if at * ws >= out[-1][0]:
            return out
        start = window_at(out, ws, at * ws)
        quiet = [item for item in out if not (start < item[0] <= start + 6 * ws)]
        return with_burst(quiet, burst(start + 1.0, W, 10, True))

    return gen


def sustained(factor: float, own: float, at: int) -> Generator:
    """From nominal window `at` on, warnings at `factor` times the own rate;
    the writes above the own rate are the attack."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        out: Stream = []
        for i in range(n_windows):
            counts = {O: poisson(rng, 5.0), W: poisson(rng, own)}
            attack = {W: poisson(rng, (factor - 1.0) * own)} if i >= at else None
            out += window_items(rng, i * ws, ws, counts, attack)
        return out

    return gen


def ramp(at: int) -> Generator:
    """From nominal window `at` on, the warning rate grows 10 % per window up to
    1,000 per window (reached at the 73rd window), stays there for 10 windows,
    then the attack ends; the writes above the own rate of 1 are the attack."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        out: Stream = []
        for i in range(n_windows):
            counts = {O: poisson(rng, 5.0), W: poisson(rng, 1.0)}
            attack = None
            step = i - at + 1
            if 1 <= step <= 82:
                rate = 1.1**step if step < 73 else 1000.0
                attack = {W: poisson(rng, max(rate - 1.0, 0.0))}
            out += window_items(rng, i * ws, ws, counts, attack)
        return out

    return gen


def new_type_burst(size: int, at: int) -> Generator:
    """An agent that never wrote a warning writes `size` of them, 2 s apart,
    inside the envelope window that holds nominal window `at`."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        out = _base(rng, n_windows, 0.0)
        return burst_at(
            out, at * L.window_seconds, lambda s: burst(s + 1.0, W, size, True)
        )

    return gen


def boundary_burst(at: int) -> Generator:
    """10 warnings above a rate of 1, 2 s apart: 5 in the last 10 s of the
    envelope window that holds nominal window `at`, 5 from its end on."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        out = _base(rng, n_windows, 1.0)
        return burst_at(out, at * ws, lambda s: burst(s + ws - 10.0, W, 10, True))

    return gen


def poisoned_cold_start(windows: tuple[int, ...] | None, at: int) -> Generator:
    """Bursts of 60 observations in three cold-start windows (given, or three
    random ones of the first ten), then 20 observations above the rate of 5,
    1 s apart, inside the envelope window that holds nominal window `at`."""

    def gen(rng: random.Random, n_windows: int) -> Stream:
        ws = L.window_seconds
        poisoned = set(windows) if windows else set(rng.sample(range(10), 3))
        out: Stream = []
        for i in range(n_windows):
            n_obs = 60 if i in poisoned else poisson(rng, 5.0)
            out += window_items(rng, i * ws, ws, {O: n_obs, W: poisson(rng, 1.0)})
        return burst_at(out, at * ws, lambda s: burst(s + 1.0, O, 20, True, gap=1.0))

    return gen


@dataclass(frozen=True)
class Profile:
    name: str
    config: SimConfig
    kind: Literal["normal", "attack"]
    generate: Callable[[random.Random, int], Stream]


def _profiles() -> dict[str, Profile]:
    normal: dict[str, Callable[[random.Random, int], Stream]] = {
        "steady_1": steady(1.0, 0.2),
        "steady_5": steady(5.0, 1.0),
        "steady_20": steady(20.0, 4.0),
        "overdispersed_3": overdispersed,
        "batches_40_4": batches(40, 4, 7.0, (300.0, 900.0)),
        "batches_20_6": batches(20, 6, 14.0, (300.0, 900.0)),
        "batches_12_12": batches(12, 12, 20.0, (300.0, 900.0)),
        "batches_40_4_back_to_back": batches(40, 4, 7.0, (0.0, 60.0)),
        "rare_warning": rare_warning,
        "intermittent_warnings": intermittent,
        "idle_gaps": idle_gaps,
        "daily_start_in_peak": daily(True),
        "daily_start_out_of_peak": daily(False),
    }
    for after in (100, 1000, 5000):
        for size in (1, 4, 12):
            normal[f"first_use_{after}_burst_{size}"] = first_use(after, size)
    attack: dict[str, Generator] = {}
    for at in ATTACK_STARTS:
        attack[f"flood_then_silence_at{at}"] = flood_then_silence(at)
        attack[f"ramp_10pct_at{at}"] = ramp(at)
        attack[f"boundary_burst_10_at{at}"] = boundary_burst(at)
        attack[f"poisoned_cold_start_1_3_at{at}"] = poisoned_cold_start((0, 1, 2), at)
        attack[f"poisoned_cold_start_random_at{at}"] = poisoned_cold_start(None, at)
        for extra in (5, 10, 20):
            for own in (1.0, 3.5, 12.0):
                attack[f"flood_{extra}_over_{own:g}_at{at}"] = flood(extra, own, at)
            attack[f"new_type_burst_{extra}_at{at}"] = new_type_burst(extra, at)
        for factor in (2.0, 3.0):
            for own in (1.0, 3.5):
                attack[f"sustained_{factor:g}x_of_{own:g}_at{at}"] = sustained(
                    factor, own, at
                )
    out = {name: Profile(name, L, "normal", gen) for name, gen in normal.items()}
    out.update({name: Profile(name, L, "attack", gen) for name, gen in attack.items()})
    return out


PROFILES = _profiles()
