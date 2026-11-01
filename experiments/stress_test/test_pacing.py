# -*- coding: utf-8 -*-
"""
AdaptivePacer: callers start in arrival order, at least 1/rate apart and never
before the Retry-After floor; a 429 or over-limit signal cuts once per
generation; the rate steps up slowly, only in a window with a 200, no 429,
over-limit, 5xx or timeout, callers using about the whole rate, and room under
the reported limit.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from stress_test.pacing import (
    GENERATED_LIMIT_HEADER,
    OVER_LIMIT_HEADER,
    AdaptivePacer,
)

T0 = 1000.0
LIMIT = {GENERATED_LIMIT_HEADER: "10000"}  # target: 0.6 x 10000 = 6000 tokens/min
OVER = {**LIMIT, OVER_LIMIT_HEADER: "yes"}


class FakeTime:
    """
    A clock that moves only while the head of the line waits (single-threaded
    tests). An event scheduled with at() runs when a wait reaches its time, as
    a response on another thread would, and ends that wait early. A test that
    reads the clock far more often than any test needs fails: a pacer that
    stops moving time would otherwise loop forever.
    """

    def __init__(self) -> None:
        self.now = T0
        self.events: list[tuple[float, Callable[[], None]]] = []
        self.readings = 0

    def clock(self) -> float:
        self.readings += 1
        assert self.readings < 100_000, "the clock stopped moving"
        return self.now

    def at(self, when: float, action: Callable[[], None]) -> None:
        self.events.append((when, action))
        self.events.sort(key=lambda event: event[0])

    def wait(self, condition: threading.Condition, timeout: float | None) -> None:
        assert timeout is not None, "a lone caller is always the head"
        wake = self.now + timeout
        if self.events and self.events[0][0] <= wake:
            when, action = self.events.pop(0)
            self.now = max(self.now, when)
            condition.release()  # as Condition.wait does
            try:
                action()
            finally:
                condition.acquire()
        else:
            self.now = wake


class SteppedTime:
    """A clock that the test moves by hand while real threads wait in line."""

    def __init__(self) -> None:
        self.now = T0

    def clock(self) -> float:
        return self.now

    def wait(self, condition: threading.Condition, timeout: float | None) -> None:
        # The head reads the clock again every 2 ms of real time.
        condition.wait(None if timeout is None else 0.002)


def _pacer(
    t: FakeTime | SteppedTime,
    start_rps: float = 1.0,
    cooldown_seconds: float = 300.0,
    on_change: Callable[[dict[str, Any]], None] | None = None,
) -> AdaptivePacer:
    return AdaptivePacer(
        start_rps=start_rps,
        step_rps=0.5,
        step_seconds=60.0,
        max_rps=2.0,
        min_rps=0.25,
        target_fraction=0.6,
        cooldown_seconds=cooldown_seconds,
        clock=t.clock,
        wait=t.wait,
        on_change=on_change,
    )


def _serve(
    pacer: AdaptivePacer,
    t: FakeTime,
    until: float,
    status: int = 200,
    headers: dict[str, str] = LIMIT,
    tokens: int = 10,
) -> None:
    """
    Start requests back to back, each with its response at once, until a
    start is at or after *until*. A response belongs to the step window that
    its start closes or falls in.
    """
    while t.now < until:
        generation = pacer.acquire()
        pacer.on_response(
            status, headers, {"completion_tokens": tokens}, generation, None
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _settings(
    start_rps: float = 1.0,
    step_rps: float = 0.5,
    step_seconds: float = 60.0,
    max_rps: float = 2.0,
    min_rps: float = 0.25,
    target_fraction: float = 0.6,
    cooldown_seconds: float = 300.0,
) -> AdaptivePacer:
    return AdaptivePacer(
        start_rps=start_rps,
        step_rps=step_rps,
        step_seconds=step_seconds,
        max_rps=max_rps,
        min_rps=min_rps,
        target_fraction=target_fraction,
        cooldown_seconds=cooldown_seconds,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"min_rps": 0.0},
        {"min_rps": 1.5},  # above start_rps
        {"start_rps": 3.0},  # above max_rps
        {"step_rps": 0.0},
        {"step_seconds": 0.0},
        {"cooldown_seconds": -1.0},
        {"target_fraction": 0.0},
        {"target_fraction": 1.5},
    ],
)
def test_invalid_settings_are_refused(changes: dict[str, float]) -> None:
    _settings()  # the defaults are valid
    with pytest.raises(ValueError):
        _settings(**changes)


# ---------------------------------------------------------------------------
# Spacing, the Retry-After floor and cuts (single thread, fake clock)
# ---------------------------------------------------------------------------


def test_starts_are_one_interval_apart() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=2.0)
    starts: list[float] = []
    for _ in range(3):
        pacer.acquire()
        starts.append(t.now)
    assert starts == [T0, T0 + 0.5, T0 + 1.0]  # the first starts at once


def test_cut_moves_the_next_start_one_interval_at_the_new_rate() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    generation = pacer.acquire()
    t.now = T0 + 0.5
    pacer.on_response(429, {}, None, generation, 1.0)  # floor at T0 + 1.5
    pacer.acquire()
    assert t.now == T0 + 2.5  # one interval at 0.5 req/s after the 429


def test_no_start_before_the_retry_after_floor() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    generation = pacer.acquire()
    t.now = T0 + 0.5
    pacer.on_response(429, {}, None, generation, 5.0)
    pacer.acquire()
    assert t.now == T0 + 5.5


def test_429_without_retry_after_still_delays_the_next_start() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=2.0)
    generation = pacer.acquire()
    t.now = T0 + 10  # the next start is long due when the 429 arrives
    pacer.on_response(429, {}, None, generation, None)
    pacer.acquire()
    assert t.now == T0 + 11  # one interval at the new 1 req/s


def test_a_429_during_the_heads_wait_moves_its_start_past_the_floor() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=2.0)
    first = pacer.acquire()  # starts at once
    # While the second caller waits for T0 + 0.5, the first gets a 429.
    t.at(T0 + 0.2, lambda: pacer.on_response(429, {}, None, first, 30.0))
    second = pacer.acquire()
    assert t.now == T0 + 30.2
    assert second == first + 1  # it started in the new generation


def test_late_429_from_an_older_generation_raises_the_floor_without_a_cut() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=2.0)
    old = pacer.acquire()
    pacer.on_response(200, OVER, None, old, None)  # cut: 1 req/s, generation 1
    assert pacer.acquire() == 1  # starts at T0 + 1
    t.at(T0 + 1.5, lambda: pacer.on_response(429, {}, None, old, 10.0))
    assert pacer.acquire() == 1  # it waited for T0 + 2 when the 429 arrived
    assert t.now == T0 + 11.5
    assert pacer.rate == 1.0  # a signal from the older generation did not cut


def test_no_start_before_the_floor_when_a_later_over_limit_cut_happens() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=2.0)
    x = pacer.acquire()  # T0
    y = pacer.acquire()  # T0 + 0.5
    t.now = T0 + 1
    pacer.on_response(200, OVER, None, x, None)  # cut: 1 req/s, generation 1
    z = pacer.acquire()  # T0 + 2
    assert (x, y, z) == (0, 0, 1)
    t.now = T0 + 2.5
    pacer.on_response(429, {}, None, y, 30.0)  # older generation: floor T0 + 32.5
    t.now = T0 + 3
    pacer.on_response(200, OVER, None, z, None)  # cut: 0.5 req/s, next T0 + 5
    pacer.acquire()
    assert t.now == T0 + 32.5
    assert pacer.rate == 0.5


def test_a_shorter_later_retry_after_does_not_lower_the_floor() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    generation = pacer.acquire()
    pacer.on_response(429, {}, None, generation, 30.0)  # floor T0 + 30
    pacer.on_response(429, {}, None, generation, 1.0)
    pacer.acquire()
    assert t.now == T0 + 30


def test_late_429_from_an_older_generation_never_moves_the_schedule_earlier() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=2.0)
    old = pacer.acquire()
    pacer.on_response(429, {}, None, old, None)  # cut: 1 req/s, next T0 + 1
    for _ in range(5):
        pacer.acquire()  # T0 + 1 to T0 + 5
    pacer.on_response(429, {}, None, old, 0.0)  # late, from the old generation
    pacer.acquire()
    assert t.now == T0 + 6


def test_429s_from_one_generation_cut_once() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=2.0)
    generations = [pacer.acquire() for _ in range(4)]
    for generation in generations:
        pacer.on_response(429, {}, None, generation, None)
    assert pacer.rate == 1.0
    newer = pacer.acquire()
    assert newer == 1
    pacer.on_response(429, {}, None, newer, None)
    assert pacer.rate == 0.5


def test_over_limit_header_cuts_without_a_429() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    pacer.on_response(200, OVER, None, 0, None)
    assert pacer.rate == 0.5
    pacer.on_response(200, {**LIMIT, OVER_LIMIT_HEADER: "no"}, None, 1, None)
    assert pacer.rate == 0.5


def test_a_503_does_not_cut() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    pacer.on_response(503, LIMIT, None, 0, None)
    pacer.on_failure()
    assert pacer.rate == 1.0
    pacer.acquire()
    pacer.acquire()
    assert t.now == T0 + 1


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def test_step_when_rate_is_used_and_usage_is_below_target() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 61)
    assert pacer.rate == 1.5


def test_no_step_before_a_limit_is_reported() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 61, headers={})
    assert pacer.rate == 1.0


def test_no_step_when_callers_use_less_than_the_rate() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 10)  # 10 starts in a 61 s window at 1 req/s
    t.now = T0 + 61
    pacer.acquire()
    assert pacer.rate == 1.0


def test_an_under_used_window_after_a_full_one_does_not_step() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 60)  # T0 + 60 closes a full window: a step to 1.5
    assert pacer.rate == 1.5
    # About 31 starts in the next window: below 0.9 x 1.5 x 61 = 82 alone,
    # above it with the first window's 60 counted again.
    _serve(pacer, t, T0 + 80)
    t.now = T0 + 121
    pacer.acquire()
    assert pacer.rate == 1.5


def test_usage_without_completion_tokens_is_ignored() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    pacer.on_response(200, LIMIT, {"prompt_tokens": 5}, 0, None)
    pacer.on_response(200, LIMIT, {"completion_tokens": None}, 0, None)
    _serve(pacer, t, T0 + 61)
    assert pacer.rate == 1.5


@pytest.mark.parametrize("tokens, rate", [(99, 1.5), (100, 1.0)])
def test_step_only_below_the_target_share_of_the_limit(
    tokens: int, rate: float
) -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 61, tokens=tokens)  # 60 responses in the last minute
    assert pacer.rate == rate  # 6000 tokens reach 0.6 x 10000; 5940 do not


def test_tokens_older_than_a_minute_do_not_block_a_step() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    pacer.on_response(200, LIMIT, {"completion_tokens": 9000}, 0, None)
    t.now = T0 + 70  # the 9000 tokens are now outside the last minute
    _serve(pacer, t, T0 + 131)  # the first start closes the empty first window
    assert pacer.rate == 1.5


@pytest.mark.parametrize("status, headers", [(429, LIMIT), (200, OVER)])
def test_rate_limit_in_window_blocks_step_even_after_cooldown(
    status: int, headers: dict[str, str]
) -> None:
    t = FakeTime()
    pacer = _pacer(t, cooldown_seconds=0.0)
    generation = pacer.acquire()
    pacer.on_response(status, headers, None, generation, None)
    assert pacer.rate == 0.5
    _serve(pacer, t, T0 + 61)
    assert pacer.rate == 0.5


@pytest.mark.parametrize("status, headers", [(429, LIMIT), (200, OVER)])
def test_a_cut_halves_and_holds_through_cooldown(
    status: int, headers: dict[str, str]
) -> None:
    t = FakeTime()
    pacer = _pacer(t)
    generation = pacer.acquire()
    pacer.on_response(status, headers, None, generation, None)
    assert pacer.rate == 0.5
    _serve(pacer, t, T0 + 298)  # windows close at T0 + 60, 120, 180, 240
    assert pacer.rate == 0.5
    _serve(pacer, t, T0 + 300)  # the cooldown ends at T0 + 300
    assert pacer.rate == 1.0


def test_no_step_with_only_503s_for_twenty_minutes() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 1200, status=503)
    assert pacer.rate == 1.0


@pytest.mark.parametrize("status", [500, 503])
def test_a_5xx_in_a_window_of_200s_blocks_the_step(status: int) -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 30)
    pacer.on_response(status, LIMIT, None, pacer.acquire(), None)
    _serve(pacer, t, T0 + 61)
    assert pacer.rate == 1.0


def test_no_step_with_only_timeouts() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 59)  # 200s at T0 to T0 + 59
    # T0 + 60 closes the first window (a step to 1.5), T0 + 120 the second.
    while t.now < T0 + 125:
        pacer.acquire()
        pacer.on_failure()
    assert pacer.rate == 1.5


def test_a_timeout_in_a_window_of_200s_blocks_the_step() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 30)
    pacer.acquire()
    pacer.on_failure()
    _serve(pacer, t, T0 + 61)
    assert pacer.rate == 1.0


@pytest.mark.parametrize("status", [None, 400])
def test_no_step_in_a_window_without_a_200(status: int | None) -> None:
    t = FakeTime()
    pacer = _pacer(t)
    _serve(pacer, t, T0 + 59)  # 200s at T0 to T0 + 59
    # T0 + 60 closes the first window (a step to 1.5), T0 + 120 the second,
    # whose requests are still running (None) or were refused (400).
    while t.now < T0 + 125:
        generation = pacer.acquire()
        if status is not None:
            pacer.on_response(status, LIMIT, None, generation, None)
    assert pacer.rate == 1.5


def test_rate_stays_within_bounds() -> None:
    t = FakeTime()
    pacer = _pacer(t, start_rps=0.25)
    pacer.on_response(429, {}, None, 0, None)
    assert pacer.rate == 0.25  # floor
    t = FakeTime()
    events: list[dict[str, Any]] = []
    pacer = _pacer(t, start_rps=2.0, on_change=events.append)
    _serve(pacer, t, T0 + 61)
    assert pacer.rate == 2.0  # cap
    assert events == []  # no step reported at the cap
    t = FakeTime()
    pacer = _pacer(t, start_rps=1.75)
    _serve(pacer, t, T0 + 61)
    assert pacer.rate == 2.0  # a step that would pass the cap stops at it


def test_cuts_and_steps_are_reported() -> None:
    t = FakeTime()
    events: list[dict[str, Any]] = []
    pacer = _pacer(t, cooldown_seconds=0.0, on_change=events.append)
    pacer.on_response(429, {}, None, pacer.acquire(), None)
    _serve(pacer, t, T0 + 121)  # T0 + 60 closes the 429's window; T0 + 120 steps
    pacer.on_response(200, OVER, None, pacer.acquire(), None)
    assert [(event["event"], event["rate"]) for event in events] == [
        ("rate_limited", 0.5),
        ("step", 1.0),
        ("over_limit", 0.5),
    ]
    assert events[0]["generated_last_minute"] == 0
    assert events[0]["generated_limit_per_minute"] is None
    assert events[1]["time"] == T0 + 120
    assert events[1]["generated_last_minute"] == 300  # 30 responses of 10 tokens
    assert events[1]["generated_limit_per_minute"] == 10000


# ---------------------------------------------------------------------------
# The limit header
# ---------------------------------------------------------------------------


def test_limit_header_as_float_and_malformed() -> None:
    t = FakeTime()
    pacer = _pacer(t)
    pacer.on_response(200, {GENERATED_LIMIT_HEADER: "10000.0"}, None, 0, None)
    pacer.on_response(200, {GENERATED_LIMIT_HEADER: "n/a"}, None, 0, None)
    _serve(pacer, t, T0 + 61, headers={})
    assert pacer.rate == 1.5  # the parsed limit of 10000 was kept


@pytest.mark.parametrize(
    "raw, tokens, rate",
    [
        # Taken as the limit, these would allow a step under any usage.
        ("inf", 100, 1.0),
        ("1e309", 100, 1.0),
        ("nan", 100, 1.0),
        # Taken as the limit, these would block every step.
        ("0", 10, 1.5),
        ("-5", 10, 1.5),
        ("n/a", 10, 1.5),
    ],
)
def test_unusable_limit_header_keeps_the_previous_limit(
    caplog: pytest.LogCaptureFixture, raw: str, tokens: int, rate: float
) -> None:
    t = FakeTime()
    pacer = _pacer(t)
    pacer.on_response(200, LIMIT, None, 0, None)
    with caplog.at_level(logging.WARNING):
        pacer.on_response(200, {GENERATED_LIMIT_HEADER: raw}, None, 0, None)
    assert GENERATED_LIMIT_HEADER in caplog.text
    _serve(pacer, t, T0 + 61, headers={}, tokens=tokens)
    assert pacer.rate == rate


# ---------------------------------------------------------------------------
# Arrival order (real threads)
# ---------------------------------------------------------------------------


def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5.0
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.001)


def _join_all(threads: list[threading.Thread]) -> None:
    deadline = time.monotonic() + 10.0
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads)


def _caller(
    pacer: AdaptivePacer,
    t: SteppedTime,
    name: str,
    started: queue.Queue[tuple[str, float]],
) -> threading.Thread:
    """Start a thread that acquires once and reports its start; return when it waits in line."""
    in_line = len(pacer._line) + 1

    def run() -> None:
        pacer.acquire()
        started.put((name, t.now))

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    _until(lambda: len(pacer._line) >= in_line)
    return thread


def test_after_a_cut_callers_start_in_arrival_order() -> None:
    t = SteppedTime()
    pacer = _pacer(t)  # 1 req/s
    first = pacer.acquire()  # caller A starts at T0
    started: queue.Queue[tuple[str, float]] = queue.Queue()
    threads = [_caller(pacer, t, name, started) for name in ("B", "C", "D")]
    pacer.on_response(429, {}, None, first, None)  # cut to 0.5 req/s
    # A retries at once and E arrives after the cut: both queue behind D.
    threads += [_caller(pacer, t, name, started) for name in ("A", "E")]
    t.now = T0 + 1.5  # past B's start time before the cut
    with pytest.raises(queue.Empty):
        started.get(timeout=0.05)
    order: list[tuple[str, float]] = []
    for k in range(1, 6):
        t.now = T0 + 2.0 * k
        order.append(started.get(timeout=5.0))
    assert order == [
        ("B", T0 + 2),
        ("C", T0 + 4),
        ("D", T0 + 6),
        ("A", T0 + 8),
        ("E", T0 + 10),
    ]
    for thread in threads:
        thread.join(timeout=5.0)


def test_a_caller_that_wakes_before_its_turn_keeps_its_place() -> None:
    t = SteppedTime()

    def wake_early(condition: threading.Condition, timeout: float | None) -> None:
        condition.wait(0.002)  # every caller, head or not, wakes every 2 ms

    pacer = AdaptivePacer(
        start_rps=1.0,
        step_rps=0.5,
        step_seconds=60.0,
        max_rps=2.0,
        min_rps=0.25,
        target_fraction=0.6,
        cooldown_seconds=300.0,
        clock=t.clock,
        wait=wake_early,
    )
    pacer.acquire()  # starts at T0
    started: queue.Queue[tuple[str, float]] = queue.Queue()
    threads = [_caller(pacer, t, name, started) for name in ("B", "C", "D", "E")]
    order: list[tuple[str, float]] = []
    for k in range(1, 5):
        t.now = T0 + k
        order.append(started.get(timeout=5.0))
    assert order == [("B", T0 + 1), ("C", T0 + 2), ("D", T0 + 3), ("E", T0 + 4)]
    _join_all(threads)


class Interrupted(Exception):
    pass


def test_a_caller_that_leaves_the_line_lets_the_next_one_start() -> None:
    t = SteppedTime()
    interrupt = threading.Event()

    def wait(condition: threading.Condition, timeout: float | None) -> None:
        if interrupt.is_set() and threading.current_thread().name == "A":
            raise Interrupted
        t.wait(condition, timeout)

    pacer = AdaptivePacer(
        start_rps=1.0,
        step_rps=0.5,
        step_seconds=60.0,
        max_rps=2.0,
        min_rps=0.25,
        target_fraction=0.6,
        cooldown_seconds=300.0,
        clock=t.clock,
        wait=wait,
    )
    pacer.acquire()  # starts at T0; the next start is T0 + 1
    left: queue.Queue[str] = queue.Queue()

    def run_a() -> None:
        try:
            pacer.acquire()
        except Interrupted:
            left.put("A")

    a = threading.Thread(target=run_a, name="A", daemon=True)
    a.start()
    _until(lambda: len(pacer._line) == 1)
    started: queue.Queue[tuple[str, float]] = queue.Queue()
    b = _caller(pacer, t, "B", started)  # waits behind A
    interrupt.set()
    assert left.get(timeout=5.0) == "A"
    t.now = T0 + 1
    assert started.get(timeout=5.0) == ("B", T0 + 1)
    a.join(timeout=5.0)
    b.join(timeout=5.0)


def test_threads_start_in_arrival_order_one_interval_apart_under_cuts() -> None:
    rate = 200.0
    local = threading.local()
    arrivals: list[str] = []  # in the order callers joined the line
    last_clock: dict[int, float] = {}
    waits_behind_the_head = [0]

    # The pacer calls clock() or wait() under its lock as soon as a caller
    # joins the line, so the first such call of an acquire marks its arrival,
    # and the head's last clock() reading is its start time.
    def note_arrival() -> None:
        label = getattr(local, "arriving", None)
        if label is not None:
            arrivals.append(label)
            local.arriving = None

    def clock() -> float:
        now = time.monotonic()
        note_arrival()
        last_clock[threading.get_ident()] = now
        return now

    def wait(condition: threading.Condition, timeout: float | None) -> None:
        note_arrival()
        if timeout is None:
            waits_behind_the_head[0] += 1
        condition.wait(timeout)

    pacer = AdaptivePacer(
        start_rps=rate,
        step_rps=1.0,
        step_seconds=60.0,
        max_rps=rate,
        min_rps=rate,
        target_fraction=0.6,
        cooldown_seconds=0.0,
        clock=clock,
        wait=wait,
    )
    starts: list[tuple[float, str]] = []

    def run(name: str) -> None:
        for i in range(5):
            local.arriving = f"{name}.{i}"
            generation = pacer.acquire()
            starts.append((last_clock[threading.get_ident()], f"{name}.{i}"))
            # Every response is over the limit, so every generation is cut:
            # the signal that let one caller take every slot before.
            pacer.on_response(200, OVER, None, generation, None)

    threads = [
        threading.Thread(target=run, args=(f"caller{n}",), daemon=True)
        for n in range(8)
    ]
    for thread in threads:
        thread.start()
    _join_all(threads)
    assert len(starts) == 40
    assert waits_behind_the_head[0] > 0  # a line formed
    starts.sort()
    assert [label for _, label in starts] == arrivals
    gaps = [later - earlier for (earlier, _), (later, _) in zip(starts, starts[1:])]
    assert min(gaps) >= 1.0 / rate - 1e-9


def test_default_clock_and_wait_pace_real_threads() -> None:
    rate = 200.0
    pacer = AdaptivePacer(
        start_rps=rate,
        step_rps=1.0,
        step_seconds=60.0,
        max_rps=rate,
        min_rps=rate,
        target_fraction=0.6,
        cooldown_seconds=0.0,
    )

    def run() -> None:
        for _ in range(3):
            pacer.acquire()

    began = time.monotonic()
    threads = [threading.Thread(target=run, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    _join_all(threads)
    # 12 starts at least 1/rate apart span at least 11 intervals.
    assert time.monotonic() - began >= 11 / rate


def test_a_retry_after_from_another_thread_holds_the_waiting_head() -> None:
    pacer = AdaptivePacer(
        start_rps=10.0,
        step_rps=1.0,
        step_seconds=60.0,
        max_rps=10.0,
        min_rps=1.0,
        target_fraction=0.6,
        cooldown_seconds=0.0,
    )  # default clock and wait
    first = pacer.acquire()
    returned: queue.Queue[float] = queue.Queue()
    head = threading.Thread(
        target=lambda: (pacer.acquire(), returned.put(time.monotonic())),
        daemon=True,
    )
    head.start()
    _until(lambda: len(pacer._line) == 1)  # it waits for its start 0.1 s out
    sent = time.monotonic()
    pacer.on_response(429, {}, None, first, 0.3)
    assert returned.get(timeout=5.0) >= sent + 0.3
    head.join(timeout=5.0)


def test_a_failing_on_change_neither_stops_the_line_nor_loses_a_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = FakeTime()
    calls: list[str] = []

    def broken(event: dict[str, Any]) -> None:
        calls.append(event["event"])
        raise OSError(28, "No space left on device")

    pacer = _pacer(t, cooldown_seconds=0.0, on_change=broken)
    # A cut reports from on_response: the response must still be recorded.
    pacer.on_response(429, {}, None, pacer.acquire(), None)
    # A step reports from inside acquire: the line must keep moving.
    _serve(pacer, t, T0 + 121)
    pacer.acquire()
    assert calls == ["rate_limited", "step"]
    assert pacer.rate == 1.0
    assert "on_change" in caplog.text


def test_an_error_while_starting_still_wakes_the_callers_in_line() -> None:
    import threading
    import time as real_time

    pacer = AdaptivePacer(
        start_rps=200.0,
        step_rps=0.5,
        step_seconds=60.0,
        max_rps=400.0,
        min_rps=1.0,
        target_fraction=0.6,
        cooldown_seconds=0.0,
    )
    # The first caller waits at the head behind a short Retry-After floor while
    # three more callers queue behind it; then its start raises.
    pacer.on_response(200, {}, None, 0, 0.3)
    real_step = pacer._maybe_step
    raised: list[bool] = []

    def flaky_step(now: float) -> None:
        if not raised:
            raised.append(True)
            raise RuntimeError("unexpected")
        real_step(now)

    pacer._maybe_step = flaky_step  # type: ignore[method-assign]
    outcomes: list[str] = []

    def caller() -> None:
        try:
            pacer.acquire()
            outcomes.append("started")
        except RuntimeError:
            outcomes.append("error")

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(4)]
    threads[0].start()
    real_time.sleep(0.05)  # the first caller is the head
    for thread in threads[1:]:
        thread.start()
    for thread in threads:
        thread.join(3.0)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["error", "started", "started", "started"]
