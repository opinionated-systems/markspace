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
        assert "weather-poller" in scheduler._entries

    def test_ignores_agent_without_manifest(self, scheduler: Scheduler) -> None:
        agent = Agent(name="no-manifest", scopes={})
        scheduler.register(agent)
        assert "no-manifest" not in scheduler._entries

    def test_ignores_agent_without_interval(self, scheduler: Scheduler) -> None:
        agent = make_agent("no-interval", interval=None)
        scheduler.register(agent)
        assert "no-interval" not in scheduler._entries

    def test_ignores_zero_interval(self, scheduler: Scheduler) -> None:
        agent = make_agent("bad", interval=0.0)
        scheduler.register(agent)
        assert "bad" not in scheduler._entries

    def test_ignores_negative_interval(self, scheduler: Scheduler) -> None:
        agent = make_agent("bad", interval=-10.0)
        scheduler.register(agent)
        assert "bad" not in scheduler._entries

    def test_unregister(self, scheduler: Scheduler) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.unregister(agent)
        assert "weather-poller" not in scheduler._entries


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

    def test_due_after_interval(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
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
    def test_returns_due_agents(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
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

    def test_interval_honored(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        """P43: Minimum interval between activations >= schedule_interval."""
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
    def test_update_interval(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        # Principal creates new agent config with shorter interval
        updated = make_agent("weather-poller", interval=60.0)
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

        updated = make_agent("weather-poller", interval=300.0)
        scheduler.update(updated)

        # Should not re-fire - last_activation preserved
        assert len(scheduler.tick_all()) == 0

    def test_update_removes_when_no_interval(
        self, space: MarkSpace, scheduler: Scheduler
    ) -> None:
        agent = make_agent("weather-poller", interval=300.0)
        scheduler.register(agent)

        updated = make_agent("weather-poller", interval=None)
        scheduler.update(updated)
        assert "weather-poller" not in scheduler._entries
