#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Thread safety tests for the markspace coordination protocol.

Validates that MarkSpace and Guard maintain correctness under concurrent access.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from markspace import (
    Action,
    Agent,
    ConflictPolicy,
    DecayConfig,
    Guard,
    GuardVerdict,
    Intent,
    MarkSpace,
    MarkType,
    Observation,
    Scope,
    Source,
    hours,
    minutes,
)


@pytest.fixture
def calendar_scope() -> Scope:
    return Scope(
        name="calendar",
        allowed_intent_verbs=("book",),
        allowed_action_verbs=("booked",),
        observation_topics=("status",),
        decay=DecayConfig(
            observation_half_life=hours(1),
            warning_half_life=hours(1),
            intent_ttl=minutes(30),
        ),
        conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
    )


@pytest.fixture
def space(calendar_scope: Scope) -> MarkSpace:
    s = MarkSpace(scopes=[calendar_scope])
    s.set_clock(1_000_000.0)
    return s


@pytest.fixture
def guard(space: MarkSpace) -> Guard:
    return Guard(space)


def make_agent(name: str) -> Agent:
    return Agent(
        name=name,
        scopes={"calendar": ["intent", "action", "observation"]},
    )


class TestConcurrentWritesNoCorruption:
    """100 agents write simultaneously. All marks must be visible afterward."""

    def test_all_marks_visible(self, space: MarkSpace) -> None:
        n_agents = 100
        agents = [make_agent(f"agent-{i:03d}") for i in range(n_agents)]
        written_ids: list[str | None] = [None] * n_agents
        errors: list[str] = []

        def write_observation(idx: int) -> None:
            try:
                agent = agents[idx]
                mark_id = space.write(
                    agent,
                    Observation(
                        scope="calendar",
                        topic="status",
                        content=f"observation from {agent.name}",
                        source=Source.FLEET,
                        confidence=0.8,
                    ),
                )
                written_ids[idx] = str(mark_id)
            except Exception as e:
                errors.append(f"agent-{idx:03d}: {e}")

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(write_observation, i) for i in range(n_agents)]
            for f in as_completed(futures):
                f.result()  # re-raise any exceptions

        assert not errors, f"Write errors: {errors}"

        # All marks should be visible
        all_marks = space.read(scope="calendar", topic="status")
        assert (
            len(all_marks) == n_agents
        ), f"Expected {n_agents} marks, got {len(all_marks)}"

        # All IDs should be unique and non-None
        ids = [mid for mid in written_ids if mid is not None]
        assert len(ids) == n_agents
        assert len(set(ids)) == n_agents, "Duplicate mark IDs detected"

    def test_interleaved_write_read(self, space: MarkSpace) -> None:
        """Writes and reads interleaved — reads always return consistent state."""
        n_writers = 50
        n_readers = 50
        agents = [make_agent(f"writer-{i:03d}") for i in range(n_writers)]
        read_results: list[int] = []
        errors: list[str] = []

        def write_obs(idx: int) -> None:
            try:
                space.write(
                    agents[idx],
                    Observation(
                        scope="calendar",
                        topic="status",
                        content=f"obs-{idx}",
                        source=Source.FLEET,
                        confidence=0.8,
                    ),
                )
            except Exception as e:
                errors.append(f"write-{idx}: {e}")

        def read_obs() -> None:
            try:
                marks = space.read(scope="calendar", topic="status")
                read_results.append(len(marks))
            except Exception as e:
                errors.append(f"read: {e}")

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = []
            for i in range(n_writers):
                futures.append(executor.submit(write_obs, i))
                if i % (n_writers // n_readers) == 0:
                    futures.append(executor.submit(read_obs))
            for f in as_completed(futures):
                f.result()

        assert not errors, f"Errors: {errors}"

        # Read results should be monotonically non-decreasing within each
        # reader's observation, and final count should be n_writers
        final_marks = space.read(scope="calendar", topic="status")
        assert len(final_marks) == n_writers


class TestConcurrentGuardNoDoubleBooking:
    """10 agents try to book the same slot. Exactly 1 succeeds."""

    def test_single_winner(self, space: MarkSpace, guard: Guard) -> None:
        n_agents = 10
        agents = [make_agent(f"booker-{i:03d}") for i in range(n_agents)]
        results: list[tuple[int, str]] = []  # (agent_idx, verdict)
        lock = threading.Lock()

        def try_book(idx: int) -> None:
            agent = agents[idx]

            def do_book() -> dict[str, str]:
                return {"booked_by": agent.name, "slot": "wed-14"}

            decision, result = guard.execute(
                agent=agent,
                scope="calendar",
                resource="wed-14",
                intent_action="book",
                result_action="booked",
                tool_fn=do_book,
                confidence=0.9,
            )
            with lock:
                results.append((idx, decision.verdict.value))

        with ThreadPoolExecutor(max_workers=n_agents) as executor:
            futures = [executor.submit(try_book, i) for i in range(n_agents)]
            for f in as_completed(futures):
                f.result()

        # Exactly 1 ALLOW, rest CONFLICT
        allows = [r for r in results if r[1] == "allow"]
        conflicts = [r for r in results if r[1] == "conflict"]

        assert (
            len(allows) == 1
        ), f"Expected exactly 1 allow, got {len(allows)}: {allows}"
        assert (
            len(conflicts) == n_agents - 1
        ), f"Expected {n_agents - 1} conflicts, got {len(conflicts)}"

        # Verify single action mark exists
        actions = space.read(
            scope="calendar",
            resource="wed-14",
            mark_type=MarkType.ACTION,
        )
        assert len(actions) == 1, f"Expected 1 action mark, got {len(actions)}"
        assert isinstance(actions[0], Action)
        assert actions[0].result["booked_by"] == agents[allows[0][0]].name

    def test_different_resources_all_succeed(
        self, space: MarkSpace, guard: Guard
    ) -> None:
        """Each agent books a different slot — all should succeed."""
        slots = ["mon-09", "tue-10", "wed-14", "thu-09", "fri-15"]
        agents = [make_agent(f"booker-{i}") for i in range(len(slots))]
        results: list[tuple[int, str]] = []
        lock = threading.Lock()

        def try_book(idx: int) -> None:
            agent = agents[idx]
            slot = slots[idx]

            def do_book() -> dict[str, str]:
                return {"booked_by": agent.name, "slot": slot}

            decision, _ = guard.execute(
                agent=agent,
                scope="calendar",
                resource=slot,
                intent_action="book",
                result_action="booked",
                tool_fn=do_book,
                confidence=0.9,
            )
            with lock:
                results.append((idx, decision.verdict.value))

        with ThreadPoolExecutor(max_workers=len(slots)) as executor:
            futures = [executor.submit(try_book, i) for i in range(len(slots))]
            for f in as_completed(futures):
                f.result()

        allows = [r for r in results if r[1] == "allow"]
        assert len(allows) == len(
            slots
        ), f"Expected all {len(slots)} to succeed, got {len(allows)} allows"


class TestConcurrentReadsDuringWrites:
    """Reads return consistent state while writes happen."""

    def test_reads_consistent(self, space: MarkSpace) -> None:
        n_writers = 30
        n_readers = 100
        agents = [make_agent(f"writer-{i:03d}") for i in range(n_writers)]
        read_counts: list[int] = []
        lock = threading.Lock()
        errors: list[str] = []

        barrier = threading.Barrier(n_writers + n_readers)

        def write_action(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                space.write(
                    agents[idx],
                    Action(
                        scope="calendar",
                        resource=f"slot-{idx:03d}",
                        action="booked",
                        result={"booked_by": agents[idx].name},
                    ),
                )
            except Exception as e:
                errors.append(f"write-{idx}: {e}")

        def read_actions() -> None:
            try:
                barrier.wait(timeout=5)
                marks = space.read(scope="calendar", mark_type=MarkType.ACTION)
                with lock:
                    read_counts.append(len(marks))
            except Exception as e:
                errors.append(f"read: {e}")

        with ThreadPoolExecutor(max_workers=n_writers + n_readers) as executor:
            futures = []
            for i in range(n_writers):
                futures.append(executor.submit(write_action, i))
            for _ in range(n_readers):
                futures.append(executor.submit(read_actions))
            for f in as_completed(futures):
                f.result()

        assert not errors, f"Errors: {errors}"

        # Each read should return between 0 and n_writers marks (consistent snapshot)
        for count in read_counts:
            assert 0 <= count <= n_writers, f"Invalid read count: {count}"

        # Final read should see all marks
        final = space.read(scope="calendar", mark_type=MarkType.ACTION)
        assert len(final) == n_writers
