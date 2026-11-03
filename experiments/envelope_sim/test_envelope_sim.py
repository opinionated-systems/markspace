# -*- coding: utf-8 -*-
"""
The envelope simulator: the replay reproduces the stored defense trials, the
driver calls the envelope as the guard does, the streams have the rates and
the timing they name, and the summaries count what they say.
"""

from __future__ import annotations

import random
import statistics
from typing import Any

import pytest

from markspace import Agent, MarkSpace, Observation, Scope, Warning
from markspace.core import DecayConfig, minutes
from markspace.write_budget import BurnTier, WriteBudget
from markspace.envelope import EnvelopeVerdict

from .driver import declared_rates, resampled_rounds, run_stream, run_write_budget
from .measure import (
    binomial_interval,
    refused_types,
    summarize_attack,
    summarize_normal,
)
from .replay import TRIALS, load_trial, replay_d0, step_events
from .streams import (
    CHECK,
    L,
    PROFILES,
    A,
    Event,
    I,
    N,
    O,
    W,
    Stream,
    burst,
    window_starts,
)


@pytest.mark.parametrize("name", TRIALS)
def test_replay_reproduces_the_stored_first_restrictions(name: str) -> None:
    trial = load_trial(name)
    first = replay_d0(trial)
    for agent, mode in trial.adversarial.items():
        if name == "slow_drift" and mode is not None:
            continue  # their rounds before the restriction are lost (ERROR steps)
        assert first.get(agent) == trial.stored_first.get(agent), agent


def test_step_events_follow_the_tool_rules() -> None:
    assert step_events("claim_task", "Successfully claimed design/8.") == [CHECK, I, A]
    assert step_events(
        "book_boardroom", "BLOCKED: Boardroom requests are mediated"
    ) == [
        CHECK,
        I,
    ]
    assert step_events(
        "claim_task", "denied: Agent 'x' restricted by statistical envelope"
    ) == [CHECK]
    assert (
        step_events(
            "claim_task", "denied: Agent 'x' blocked by barrier: action revoked"
        )
        == []
    )
    assert step_events("order_lunch", "FULL: no seats") == [CHECK, N]
    assert step_events("issue_warning", "Warning issued") == [CHECK, W]
    assert step_events("view_rooms", "...") == []


def _monitor_restriction_time(stream: Stream) -> float | None:
    """The time of the first write before which the envelope, attached to a
    space as a monitor by a write hook, says RESTRICTED: the check that the
    guard made before each write while it used the envelope."""
    clock = [0.0]
    space = MarkSpace(
        scopes=[
            Scope(
                name="s",
                decay=DecayConfig(
                    observation_half_life=minutes(60),
                    warning_half_life=minutes(60),
                    intent_ttl=minutes(30),
                ),
            )
        ]
    )
    monitor = L.envelope(lambda: clock[0])
    space.add_write_hook(lambda agent_id, mark: monitor.record(agent_id, mark))
    agent = Agent(name="a", scopes={"s": ["observation", "warning"]})
    for t, event, _ in stream:
        if event == CHECK:
            continue
        clock[0] = t
        space.set_clock(t)
        if monitor.check(agent.id) == EnvelopeVerdict.RESTRICTED:
            return t
        space.write(
            agent,
            (
                Observation(scope="s", topic="t", content="c")
                if event == O
                else Warning(scope="s", topic="t", reason="r")
            ),
        )
    return None


@pytest.mark.parametrize("seed", range(1, 7))
def test_run_stream_restricts_where_a_hooked_monitor_does(seed: int) -> None:
    """Generated streams with observations and warnings, through run_stream and
    through a monitor fed by a space's write hook: the first RESTRICTED
    verdict comes before the same write."""
    stream = PROFILES["steady_5"].generate(random.Random(seed), 600)
    result = run_stream(stream, L)
    assert result.restricted_at is not None
    assert result.restricted_at == _monitor_restriction_time(stream)


def test_judged_windows_skip_cold_start_and_count_the_restriction_window() -> None:
    """15 windows of 5 observations at fixed times, so each nominal window is
    one envelope window: the detector is ready once 10 windows have completed,
    so windows 11 to 15 and the next one's check are judged. A flood in window
    16 restricts there, and that window is judged too."""
    ws = L.window_seconds
    calm: Stream = []
    for i in range(15):
        for k in range(5):
            t = i * ws + 1.0 + k
            calm += [(t, CHECK, False), (t, O, False)]
    result = run_stream(calm + [(15 * ws + 1.0, CHECK, False)], L)
    assert result.restricted_at is None
    assert result.judged_windows == 6
    flood: Stream = []
    for k in range(40):
        t = 15 * ws + 1.0 + k
        flood += [(t, CHECK, False), (t, O, True)]
    result = run_stream(calm + flood, L)
    assert result.restricted_window == 16
    assert result.judged_windows == 6


def test_summaries_count_what_they_say() -> None:
    day = 86_400.0
    runs: list[dict[str, Any]] = [
        {"start": 0.0, "restricted_at": 0.5 * day},
        {"start": 0.0, "restricted_at": 3 * day},
        {"start": 0.0, "restricted_at": None},
        {"start": 100.0, "restricted_at": day + 100.0},
    ]
    s = summarize_normal(runs, [day, 7 * day])
    assert s["restricted_within_1_days"]["agents"] == 2
    assert s["restricted_within_7_days"]["agents"] == 3
    assert s["restricted_by_the_end"] == 3
    attack = {"attack_first": 1000.0, "attack_last": 1010.0, "attack_writes_before": 3}
    a = summarize_attack(
        [
            {**attack, "restricted_at": 500.0},  # before
            {**attack, "restricted_at": 1005.0},  # detected
            {**attack, "restricted_at": 1010.0 + 300.0},  # detected, one window after
            {**attack, "restricted_at": 1010.0 + 301.0},  # later
            {**attack, "restricted_at": None},  # never
        ],
        300.0,
    )
    counts = (
        a["restricted_before_the_attack"],
        a["detected"],
        a["restricted_later"],
        a["never_restricted"],
    )
    assert counts == (1, 2, 1, 1)


def test_resampled_rounds_draw_from_one_agents_rounds() -> None:
    x_rounds: list[list[Event]] = [[CHECK, I] * k for k in range(1, 6)]
    y_rounds: list[list[Event]] = [[CHECK, A] * 9]
    pool = {"x": x_rounds, "y": y_rounds}
    rounds = resampled_rounds(pool, random.Random(3), 50)
    assert len(rounds) == 50
    agent = "x" if rounds[0][1] == I else "y"
    assert all(r in pool[agent] for r in rounds)
    if agent == "x":
        assert len({len(r) for r in rounds}) > 1


def _window_of(starts: list[float], t: float) -> int:
    return max(i for i, s in enumerate(starts) if s <= t)


@pytest.mark.parametrize("seed", range(1, 6))
def test_first_use_burst_lands_in_one_window_at_its_time(seed: int) -> None:
    stream = PROFILES["first_use_100_burst_12"].generate(random.Random(seed), 300)
    times = [t for t, e, _ in stream if e == W]
    assert len(times) == 12
    # 1 s after the start of the first envelope window that ends after nominal
    # window 100 begins (the window that holds it, or the next if it is idle)
    ws, nominal = L.window_seconds, 100 * L.window_seconds
    starts = window_starts(stream, ws)
    i = min(range(len(starts)), key=lambda j: abs(starts[j] - (times[0] - 1.0)))
    assert abs(starts[i] - (times[0] - 1.0)) < 1e-6
    assert starts[i] + ws > nominal
    assert i == 0 or starts[i - 1] + ws <= nominal
    assert len({_window_of(starts, t) for t in times}) == 1


def test_floods_stay_in_one_envelope_window_and_the_boundary_burst_crosses() -> None:
    for seed in range(1, 6):
        stream = PROFILES["flood_10_over_1_at15"].generate(random.Random(seed), 60)
        attack = [t for t, _, a in stream if a]
        starts = window_starts(stream, L.window_seconds)
        assert len(attack) == 10
        assert (
            min(abs(s - (attack[0] - 1.0)) for s in starts) < 1e-6
        )  # 1 s after a start
        assert len({_window_of(starts, t) for t in attack}) == 1
        stream = PROFILES["boundary_burst_10_at15"].generate(random.Random(seed), 60)
        attack = [t for t, _, a in stream if a]
        starts = window_starts(stream, L.window_seconds)
        assert len({_window_of(starts, t) for t in attack}) == 2


def test_ramp_and_daily_peak_have_their_shape() -> None:
    stream = PROFILES["ramp_10pct_at15"].generate(random.Random(4), 60)
    per_nominal = [0] * 60
    for t, _, a in stream:
        if a:
            per_nominal[int(t // L.window_seconds)] += 1
    assert sum(per_nominal[:15]) == 0
    assert statistics.mean(per_nominal[50:60]) > 30  # 1.1**40 is about 45
    stream = PROFILES["ramp_10pct_at15"].generate(random.Random(4), 120)
    last_attack = max(t for t, _, a in stream if a)
    assert int(last_attack // L.window_seconds) == 15 + 81  # step 82 is the last
    stream = PROFILES["daily_start_in_peak"].generate(random.Random(4), 288)
    obs = [0] * 288
    for t, e, _ in stream:
        if e == O:
            obs[int(t // L.window_seconds)] += 1
    assert statistics.mean(obs[:48]) > 2.5 * statistics.mean(obs[48:])


@pytest.mark.parametrize("name", ["steady_5", "steady_20"])
def test_steady_profiles_have_their_rates(name: str) -> None:
    rate = {"steady_5": 5.0, "steady_20": 20.0}[name]
    stream = PROFILES[name].generate(random.Random(1), 2000)
    counts = [0] * 2000
    for t, event, _ in stream:
        if event == O:
            counts[int(t // L.window_seconds)] += 1
    assert abs(statistics.mean(counts) - rate) < 4 * (rate / 2000) ** 0.5
    assert abs(statistics.variance(counts) - rate) < 0.15 * rate


def test_attack_writes_are_counted_until_the_restriction() -> None:
    stream = PROFILES["new_type_burst_20_at15"].generate(random.Random(3), 400)
    result = run_stream(stream, L)
    assert result.attack_first is not None and result.restricted_at is not None
    assert 0 < result.attack_writes_before < 20


def test_binomial_interval() -> None:
    lo, hi = binomial_interval(0, 400)
    assert lo == 0.0
    assert hi == pytest.approx(1 - 0.025 ** (1 / 400), rel=1e-6)
    lo, hi = binomial_interval(400, 400)
    assert hi == 1.0
    assert lo == pytest.approx(0.025 ** (1 / 400), rel=1e-6)
    lo, hi = binomial_interval(200, 400)
    assert lo < 0.5 < hi


def test_declared_rates_leave_out_attack_writes() -> None:
    stream: Stream = [
        (0.0, CHECK, False),
        (0.0, O, False),
        (3600.0, CHECK, False),
        (3600.0, W, True),
        (7200.0, CHECK, False),
        (7200.0, O, False),
    ]
    assert declared_rates(stream) == {O: 1.0}  # 2 observations over 2 hours


def test_run_write_budget_records_the_first_page_and_refusal() -> None:
    """12 warnings an hour over a day (budget 288), a page tier at burn rate 1
    (thresholds 12 per hour, 1 per 300 s): steady writes raise nothing; a
    flood pages at its 13th write and is refused from its 289th, and the
    refused writes do not count as attack writes."""
    page = BurnTier(
        level="page",
        long_window_seconds=3600,
        short_window_seconds=300,
        budget_fraction=1 / 24,
    )
    budget = WriteBudget(per_hour={W: 12.0}, period_seconds=86_400.0, tiers=(page,))
    steady = burst(0.0, W, 48, False, gap=300.0)
    result = run_write_budget(steady, budget)
    assert (result["refused_at"], result["paged_at"]) == (None, None)
    flood = burst(0.0, W, 300, True, gap=1.0)
    result = run_write_budget(flood, budget)
    assert result["paged_at"] == 12.0 and result["attack_writes_before_page"] == 12
    assert result["refused_at"] == 288.0 and result["refused_type"] == "warning"
    assert result["attack_writes_before_refusal"] == 288


def test_refused_types_counts_first_refusals_within_the_horizon() -> None:
    runs: list[dict[str, Any]] = [
        {"start": 0.0, "refused_at": 10.0, "refused_type": "need"},
        {"start": 0.0, "refused_at": 50.0, "refused_type": "intent"},
        {"start": 0.0, "refused_at": None, "refused_type": None},
        {"start": 5.0, "refused_at": 35.0, "refused_type": "need"},
    ]
    assert refused_types(runs, 30.0) == {"need": 2}
