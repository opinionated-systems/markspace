# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Scheduler Tests

Tests for manifest-based scheduling (Spec Section 14).
All tests use space.set_clock() - no real timers.
"""

from __future__ import annotations

import pytest

from markspace import (
    Agent,
    AgentManifest,
    MarkSpace,
    MarkType,
)
from markspace.schedule import Scheduler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def space() -> MarkSpace:
    space = MarkSpace()
    space.set_clock(1_000_000.0)
    return space


@pytest.fixture
def scheduler(space: MarkSpace) -> Scheduler:
    return Scheduler(space)


def make_agent(name: str, interval: float | None = None) -> Agent:
    """Helper: create an agent with optional schedule_interval."""
    manifest = AgentManifest(
        outputs=(("weather", MarkType.OBSERVATION),),
        schedule_interval=interval,
    )
    return Agent(
        name=name,
        scopes={"weather": ["observation"]},
        manifest=manifest,
    )


# ---------------------------------------------------------------------------
# register() - reads schedule_interval from manifest
# ---------------------------------------------------------------------------


class TestRegister:
    def test_registers_agent_with_interval(self, scheduler: Scheduler) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        assert agent.id in scheduler._entries

    def test_ignores_agent_without_manifest(self, scheduler: Scheduler) -> None:
        agent = Agent(name="no-manifest", scopes={})
        scheduler.register(agent)
        assert agent.id not in scheduler._entries

    def test_ignores_agent_without_interval(self, scheduler: Scheduler) -> None:
        agent = make_agent("no-interval", interval=None)
        scheduler.register(agent)
        assert agent.id not in scheduler._entries

    def test_ignores_zero_interval(self, scheduler: Scheduler) -> None:
        agent = make_agent("bad", interval=0.0)
        scheduler.register(agent)
        assert agent.id not in scheduler._entries

    def test_ignores_negative_interval(self, scheduler: Scheduler) -> None:
        agent = make_agent("bad", interval=-10.0)
        scheduler.register(agent)
        assert agent.id not in scheduler._entries

    def test_unregister(self, scheduler: Scheduler) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.unregister(agent)
        assert agent.id not in scheduler._entries


# ---------------------------------------------------------------------------
# due() - returns agents whose interval has elapsed
# ---------------------------------------------------------------------------


class TestDue:
    def test_new_agent_immediately_due(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        due = scheduler.due()
        assert len(due) == 1
        assert due[0].name == "weather-poller"

    def test_not_due_before_interval(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        space.set_clock(1_000_100.0)  # 100s later, interval is 300s
        due = scheduler.due()
        assert len(due) == 0

    def test_due_after_interval(self, space: MarkSpace, scheduler: Scheduler) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        space.set_clock(1_000_300.0)  # exactly 300s later
        due = scheduler.due()
        assert len(due) == 1

    def test_multiple_agents_different_intervals(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        fast = make_agent("fast", interval=60.0)
        slow = make_agent("slow", interval=600.0)
        scheduler.register(fast)
        scheduler.register(slow)
        scheduler.tick_all()  # both activate immediately

        space.set_clock(1_000_060.0)  # 60s later
        due = scheduler.due()
        assert len(due) == 1  # only "fast" is due
        assert due[0].name == "fast"

    def test_empty_scheduler(self, scheduler: Scheduler) -> None:
        assert scheduler.due() == []


# ---------------------------------------------------------------------------
# tick_all() - returns due agents and marks activated
# ---------------------------------------------------------------------------


class TestTickAll:
    def test_returns_due_agents(self, space: MarkSpace, scheduler: Scheduler) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        agents = scheduler.tick_all()
        assert len(agents) == 1
        assert agents[0].name == "weather-poller"

    def test_idempotent_no_double_fire(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        agents1 = scheduler.tick_all()
        agents2 = scheduler.tick_all()  # same time, should not fire again
        assert len(agents1) == 1
        assert len(agents2) == 0

    def test_interval_honored(self, space: MarkSpace, scheduler: Scheduler) -> None:
        """P56: Minimum interval between activations >= schedule_interval."""
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        space.set_clock(1_000_299.0)  # 299s later - not yet due
        assert len(scheduler.tick_all()) == 0

        space.set_clock(1_000_300.0)  # exactly 300s - due
        agents = scheduler.tick_all()
        assert len(agents) == 1


# ---------------------------------------------------------------------------
# update() - principal changes schedule
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_interval(self, space: MarkSpace, scheduler: Scheduler) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        # Principal creates new agent config with shorter interval (same id)
        updated = Agent(
            name="weather-poller",
            id=agent.id,
            scopes={"weather": ["observation"]},
            manifest=AgentManifest(
                outputs=(("weather", MarkType.OBSERVATION),),
                schedule_interval=60.0,
            ),
        )
        scheduler.update(updated)

        space.set_clock(1_000_060.0)  # 60s later - due under new interval
        agents = scheduler.tick_all()
        assert len(agents) == 1

    def test_update_preserves_last_activation(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        updated = Agent(
            name="weather-poller",
            id=agent.id,
            scopes={"weather": ["observation"]},
            manifest=AgentManifest(
                outputs=(("weather", MarkType.OBSERVATION),),
                schedule_interval=300.0,
            ),
        )
        scheduler.update(updated)

        # Should not re-fire - last_activation preserved
        assert len(scheduler.tick_all()) == 0

    def test_update_removes_when_no_interval(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)

        updated = Agent(
            name="weather-poller",
            id=agent.id,
            scopes={"weather": ["observation"]},
            manifest=AgentManifest(
                outputs=(("weather", MarkType.OBSERVATION),),
                schedule_interval=None,
            ),
        )
        scheduler.update(updated)
        assert agent.id not in scheduler._entries


# ---------------------------------------------------------------------------
# Concurrent tick_all() - TOCTOU race fix (W22)
# ---------------------------------------------------------------------------


class TestConcurrentTickAll:
    def test_no_double_activation_under_concurrency(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        """Two concurrent tick_all() calls must not both return the same agent."""
        import threading

        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)

        results: list[list] = [[], []]

        def tick(index: int) -> None:
            results[index] = scheduler.tick_all()

        t1 = threading.Thread(target=tick, args=(0,))
        t2 = threading.Thread(target=tick, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one of the two calls should return the agent
        total = len(results[0]) + len(results[1])
        assert total == 1, (
            f"Expected exactly 1 activation, got {total} "
            f"(thread0={len(results[0])}, thread1={len(results[1])})"
        )


# ---------------------------------------------------------------------------
# start() / stop() - background timer
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_stop_lifecycle(self) -> None:
        """start() launches a background thread that fires due agents."""
        space = MarkSpace()
        space.set_clock(1_000_000.0)
        scheduler = Scheduler(space)

        agent = make_agent("poller", interval=0.05)
        scheduler.register(agent)

        activated: list[list[Agent]] = []

        def on_due(agents: list[Agent]) -> None:
            activated.append(agents)

        scheduler.start(poll_interval=0.02, on_due=on_due)
        assert scheduler._running is True
        assert scheduler._thread is not None and scheduler._thread.is_alive()

        # Allow a few ticks - advance clock so agent becomes due again
        import time

        time.sleep(0.1)
        # The agent fired on the first tick; advance clock for another
        space.set_clock(1_000_000.1)
        time.sleep(0.1)

        scheduler.stop()
        assert scheduler._running is False
        assert scheduler._thread is None

        # At least one activation should have occurred
        assert len(activated) >= 1
        assert activated[0][0].name == "poller"

    def test_stop_is_idempotent(self) -> None:
        """Calling stop() when not running should not raise."""
        space = MarkSpace()
        scheduler = Scheduler(space)
        scheduler.stop()  # no-op
        assert scheduler._running is False

    def test_start_is_idempotent(self) -> None:
        """Calling start() twice should not spawn a second thread."""
        space = MarkSpace()
        space.set_clock(1_000_000.0)
        scheduler = Scheduler(space)

        scheduler.start(poll_interval=0.05)
        thread1 = scheduler._thread
        scheduler.start(poll_interval=0.05)  # no-op
        thread2 = scheduler._thread
        assert thread1 is thread2

        scheduler.stop()

    def test_register_returns_bool(self) -> None:
        """register() returns True on success, False when skipped."""
        space = MarkSpace()
        scheduler = Scheduler(space)

        assert scheduler.register(make_agent("ok", interval=60.0)) is True
        assert scheduler.register(Agent(name="no-manifest", scopes={})) is False
        assert scheduler.register(make_agent("no-interval", interval=None)) is False
        assert scheduler.register(make_agent("zero", interval=0.0)) is False
        assert scheduler.register(make_agent("neg", interval=-1.0)) is False


class TestConcurrentStartStop:
    """Concurrent start/stop must not spawn duplicate threads."""

    def test_concurrent_start_single_thread(self) -> None:
        """Multiple threads calling start() concurrently should only spawn one timer."""
        import threading

        space = MarkSpace()
        space.set_clock(1_000_000.0)
        scheduler = Scheduler(space)
        scheduler.register(make_agent("poller", interval=60.0))

        barrier = threading.Barrier(4)
        threads_seen: list[threading.Thread | None] = []
        lock = threading.Lock()

        def try_start() -> None:
            barrier.wait()
            scheduler.start(poll_interval=0.1)
            with lock:
                threads_seen.append(scheduler._thread)

        starters = [threading.Thread(target=try_start) for _ in range(4)]
        for t in starters:
            t.start()
        for t in starters:
            t.join(timeout=5.0)

        scheduler.stop()

        # All callers should see the same thread (only one was spawned)
        non_none = [t for t in threads_seen if t is not None]
        assert len(set(id(t) for t in non_none)) == 1
