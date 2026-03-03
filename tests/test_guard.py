# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Guard Tests

Tests that the deterministic enforcement layer correctly prevents conflicts
without depending on the LLM agent to "remember" to check marks.

These tests prove the key architectural revision: coordination enforcement
lives in the harness (deterministic), not in the agent (unreliable).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from markspace import (
    Agent,
    ConflictPolicy,
    DecayConfig,
    Guard,
    GuardVerdict,
    Intent,
    MarkSpace,
    MarkType,
    Scope,
    hours,
    minutes,
)


@pytest.fixture
def calendar_scope() -> Scope:
    return Scope(
        name="calendar",
        intent_actions=("book", "reschedule", "cancel"),
        action_actions=("booked", "rescheduled", "cancelled"),
        decay=DecayConfig(
            observation_half_life=hours(1),
            warning_half_life=hours(4),
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


@pytest.fixture
def booker() -> Agent:
    return Agent(
        name="flight-booker",
        scopes={
            "calendar": ["intent", "action", "need"],
        },
    )


@pytest.fixture
def optimizer() -> Agent:
    return Agent(
        name="calendar-optimizer",
        scopes={
            "calendar": ["intent", "action", "need"],
        },
    )


# ---------------------------------------------------------------------------
# Core guarantee: guard prevents conflicts deterministically
# ---------------------------------------------------------------------------


class TestGuardPreventsConflicts:
    """The entire point: agents don't need to check marks. The guard does."""

    def test_first_agent_allowed(self, guard: Guard, booker: Agent) -> None:
        """No other intents — agent proceeds."""
        decision = guard.pre_action(
            booker, "calendar", "thu-14:00", "book", confidence=0.9
        )
        assert decision.verdict == GuardVerdict.ALLOW

    def test_higher_confidence_wins(
        self,
        guard: Guard,
        booker: Agent,
        optimizer: Agent,
    ) -> None:
        """Booker (0.9) gets ALLOW, optimizer (0.6) gets CONFLICT."""
        d1 = guard.pre_action(booker, "calendar", "thu-14:00", "book", confidence=0.9)
        assert d1.verdict == GuardVerdict.ALLOW

        d2 = guard.pre_action(
            optimizer, "calendar", "thu-14:00", "reschedule", confidence=0.6
        )
        assert d2.verdict == GuardVerdict.CONFLICT
        assert d2.winning_intent is not None
        assert d2.winning_intent.agent_id == booker.id

    def test_lower_confidence_yields_even_if_first(
        self,
        guard: Guard,
        booker: Agent,
        optimizer: Agent,
    ) -> None:
        """Even writing first doesn't help if confidence is lower."""
        d1 = guard.pre_action(
            optimizer, "calendar", "thu-14:00", "reschedule", confidence=0.4
        )
        assert d1.verdict == GuardVerdict.ALLOW  # first writer, no conflict yet

        d2 = guard.pre_action(booker, "calendar", "thu-14:00", "book", confidence=0.9)
        # Booker has higher confidence — booker wins
        assert d2.verdict == GuardVerdict.ALLOW

    def test_unauthorized_agent_denied(self, guard: Guard) -> None:
        """Agent without scope permissions is rejected."""
        hacker = Agent(name="unauthorized", scopes={})
        decision = guard.pre_action(
            hacker, "calendar", "thu-14:00", "book", confidence=1.0
        )
        assert decision.verdict == GuardVerdict.DENIED

    def test_different_resources_no_conflict(
        self,
        guard: Guard,
        booker: Agent,
        optimizer: Agent,
    ) -> None:
        """Intents on different resources don't conflict."""
        d1 = guard.pre_action(booker, "calendar", "thu-14:00", "book", confidence=0.9)
        d2 = guard.pre_action(
            optimizer, "calendar", "fri-10:00", "reschedule", confidence=0.9
        )
        assert d1.verdict == GuardVerdict.ALLOW
        assert d2.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# P28: Action precedence — completed actions block new intents
# ---------------------------------------------------------------------------


class TestActionPrecedence:
    """P28: An existing action mark on a resource MUST block new intents from other agents."""

    def test_completed_action_blocks_new_intent(
        self,
        guard: Guard,
        booker: Agent,
        optimizer: Agent,
        space: MarkSpace,
    ) -> None:
        """After agent A completes a booking (action mark), agent B is blocked."""
        # Agent A books and completes
        decision_a, result_a = guard.execute(
            agent=booker,
            scope="calendar",
            resource="thu-14:00",
            intent_action="book",
            result_action="booked",
            tool_fn=lambda: {"flight": "DL413"},
            confidence=0.9,
        )
        assert decision_a.verdict == GuardVerdict.ALLOW

        # Agent B tries to book the same resource
        decision_b = guard.pre_action(
            optimizer, "calendar", "thu-14:00", "reschedule", confidence=0.9
        )
        assert decision_b.verdict == GuardVerdict.CONFLICT

    def test_same_agent_not_blocked_by_own_action(
        self,
        guard: Guard,
        booker: Agent,
        space: MarkSpace,
    ) -> None:
        """An agent's own action on a resource does NOT block itself."""
        guard.execute(
            agent=booker,
            scope="calendar",
            resource="thu-14:00",
            intent_action="book",
            result_action="booked",
            tool_fn=lambda: {"ok": True},
            confidence=0.9,
        )
        # Same agent can still write intents on the same resource
        decision = guard.pre_action(
            booker, "calendar", "thu-14:00", "reschedule", confidence=0.9
        )
        assert decision.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Full guarded execution
# ---------------------------------------------------------------------------


class TestGuardedExecution:
    """guard.execute() wraps the full check-execute-record cycle."""

    def test_successful_execution(
        self, guard: Guard, booker: Agent, space: MarkSpace
    ) -> None:
        """Tool executes, action mark is written."""
        call_log: list[str] = []

        def book_flight() -> dict[str, str]:
            call_log.append("booked")
            return {"flight": "DL413"}

        decision, result = guard.execute(
            agent=booker,
            scope="calendar",
            resource="thu-14:00",
            intent_action="book",
            result_action="booked",
            tool_fn=book_flight,
            confidence=0.9,
        )

        assert decision.verdict == GuardVerdict.ALLOW
        assert result == {"flight": "DL413"}
        assert call_log == ["booked"]

        # Action mark should exist
        marks = space.read(
            scope="calendar", resource="thu-14:00", mark_type=MarkType.ACTION
        )
        assert len(marks) == 1
        assert marks[0].result == {"flight": "DL413"}

    def test_blocked_execution_never_calls_tool(
        self,
        guard: Guard,
        booker: Agent,
        optimizer: Agent,
    ) -> None:
        """If guard blocks, the tool function is never called."""
        # Booker claims the resource first
        guard.pre_action(booker, "calendar", "thu-14:00", "book", confidence=0.9)

        call_log: list[str] = []

        def reschedule() -> dict[str, str]:
            call_log.append("rescheduled")  # should never happen
            return {"new_time": "fri-10:00"}

        decision, result = guard.execute(
            agent=optimizer,
            scope="calendar",
            resource="thu-14:00",
            intent_action="reschedule",
            result_action="rescheduled",
            tool_fn=reschedule,
            confidence=0.6,
        )

        assert decision.verdict == GuardVerdict.CONFLICT
        assert result is None
        assert call_log == [], "Tool should never have been called"

    def test_action_supersedes_intent(
        self,
        guard: Guard,
        booker: Agent,
        space: MarkSpace,
    ) -> None:
        """After execution, intent is gone, action remains."""

        def book_flight() -> str:
            return "ok"

        guard.execute(
            agent=booker,
            scope="calendar",
            resource="thu-14:00",
            intent_action="book",
            result_action="booked",
            tool_fn=book_flight,
            confidence=0.9,
        )

        # Intent should be superseded (invisible)
        intents = space.get_intents("calendar", "thu-14:00")
        assert len(intents) == 0

        # Action should be visible
        actions = space.read(
            scope="calendar", resource="thu-14:00", mark_type=MarkType.ACTION
        )
        assert len(actions) == 1


# ---------------------------------------------------------------------------
# YIELD_ALL policy
# ---------------------------------------------------------------------------


class TestYieldAllPolicy:
    """YIELD_ALL creates need marks for principal resolution."""

    def test_yield_all_blocks_and_creates_need(self) -> None:
        scope = Scope(
            name="critical",
            intent_actions=("modify",),
            action_actions=("modified",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
            conflict_policy=ConflictPolicy.YIELD_ALL,
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)
        guard = Guard(space)

        agent_a = Agent(name="a", scopes={"critical": ["intent", "action", "need"]})
        agent_b = Agent(name="b", scopes={"critical": ["intent", "action", "need"]})

        # First agent is fine (no conflict yet)
        d1 = guard.pre_action(agent_a, "critical", "r1", "modify", confidence=0.9)
        assert d1.verdict == GuardVerdict.ALLOW

        # Second agent triggers YIELD_ALL
        d2 = guard.pre_action(agent_b, "critical", "r1", "modify", confidence=0.9)
        assert d2.verdict == GuardVerdict.BLOCKED

        # Need mark should exist
        clusters = space.aggregate_needs()
        assert len(clusters) == 1
        assert clusters[0].blocking_count == 1


# ---------------------------------------------------------------------------
# Generalized supersession
# ---------------------------------------------------------------------------


class TestGeneralizedSupersession:
    """Any mark type can supersede a prior mark, not just actions."""

    def test_observation_supersedes_observation(self) -> None:
        """New observation replaces old one on same topic."""
        scope = Scope(
            name="research",
            decay=DecayConfig(
                observation_half_life=hours(12),
                warning_half_life=hours(6),
                intent_ttl=hours(4),
            ),
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)

        agent = Agent(
            name="researcher",
            scopes={
                "research": ["observation"],
            },
        )

        from markspace import Observation, Source

        # First observation
        obs1_id = space.write(
            agent,
            Observation(
                scope="research",
                topic="price",
                content="$100",
                source=Source.FLEET,
                confidence=0.8,
            ),
        )

        # Updated observation supersedes the first
        space.write(
            agent,
            Observation(
                scope="research",
                topic="price",
                content="$120",
                source=Source.FLEET,
                confidence=0.9,
                supersedes=obs1_id,
            ),
        )

        marks = space.read(scope="research", topic="price")
        assert len(marks) == 1
        assert marks[0].content == "$120"

    def test_superseded_mark_invisible(self) -> None:
        """Superseded marks are filtered out of reads."""
        scope = Scope(
            name="test",
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)
        agent = Agent(name="a", scopes={"test": ["observation"]})

        from markspace import Observation, Source

        id1 = space.write(
            agent,
            Observation(
                scope="test",
                topic="x",
                content="old",
                source=Source.FLEET,
            ),
        )
        id2 = space.write(
            agent,
            Observation(
                scope="test",
                topic="x",
                content="mid",
                source=Source.FLEET,
                supersedes=id1,
            ),
        )
        space.write(
            agent,
            Observation(
                scope="test",
                topic="x",
                content="new",
                source=Source.FLEET,
                supersedes=id2,
            ),
        )

        marks = space.read(scope="test", topic="x")
        assert len(marks) == 1
        assert marks[0].content == "new"


# ---------------------------------------------------------------------------
# Deferred Resolution — P32, P33, P34
# Spec Section 6.2
# ---------------------------------------------------------------------------


class TestDeferredResolution:
    """Tests for the deferred resolution protocol (Spec Section 6.2)."""

    @pytest.fixture
    def deferred_scope(self) -> Scope:
        return Scope(
            name="parking",
            intent_actions=("reserve",),
            action_actions=("reserved",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
            conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
            deferred=True,
        )

    @pytest.fixture
    def deferred_space(self, deferred_scope: Scope) -> MarkSpace:
        s = MarkSpace(scopes=[deferred_scope])
        s.set_clock(1_000_000.0)
        return s

    @pytest.fixture
    def deferred_guard(self, deferred_space: MarkSpace) -> Guard:
        return Guard(deferred_space)

    @pytest.fixture
    def agent_low(self) -> Agent:
        return Agent(
            name="employee",
            scopes={"parking": ["intent", "action", "need"]},
        )

    @pytest.fixture
    def agent_high(self) -> Agent:
        return Agent(
            name="department-head",
            scopes={"parking": ["intent", "action", "need"]},
        )

    @pytest.fixture
    def agent_mid(self) -> Agent:
        return Agent(
            name="team-lead",
            scopes={"parking": ["intent", "action", "need"]},
        )

    # -- P32: Deferred Completeness --

    def test_p30_all_intents_considered(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
        agent_mid: Agent,
        agent_high: Agent,
    ) -> None:
        """P32: Batch resolution MUST consider ALL active intents at the boundary."""
        # Phase 1: All three agents write intents (all get BLOCKED)
        d1 = deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.3
        )
        assert d1.verdict == GuardVerdict.BLOCKED

        d2 = deferred_guard.pre_action(
            agent_mid, "parking", "spot-A", "reserve", confidence=0.6
        )
        assert d2.verdict == GuardVerdict.BLOCKED

        d3 = deferred_guard.pre_action(
            agent_high, "parking", "spot-A", "reserve", confidence=0.95
        )
        assert d3.verdict == GuardVerdict.BLOCKED

        # Phase 2+3: Resolution boundary
        results = deferred_guard.resolve_deferred("parking", "spot-A")

        # All three agents must appear in results
        assert len(results) == 3
        assert agent_low.id in results
        assert agent_mid.id in results
        assert agent_high.id in results

    def test_p30_late_intent_included(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
        agent_high: Agent,
    ) -> None:
        """P32: An intent written just before the boundary is included."""
        deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.3
        )

        # Advance clock slightly — still within TTL
        deferred_space.set_clock(1_000_060.0)

        deferred_guard.pre_action(
            agent_high, "parking", "spot-A", "reserve", confidence=0.95
        )

        results = deferred_guard.resolve_deferred("parking", "spot-A")
        assert len(results) == 2
        # Both intents must be considered
        assert agent_low.id in results
        assert agent_high.id in results

    def test_p30_expired_intent_excluded(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
        agent_high: Agent,
    ) -> None:
        """P32: An intent past its TTL is NOT considered (correctly excluded)."""
        deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.3
        )

        # Advance past TTL (30 min = 1800s)
        deferred_space.set_clock(1_000_000.0 + 2000.0)

        deferred_guard.pre_action(
            agent_high, "parking", "spot-A", "reserve", confidence=0.95
        )

        results = deferred_guard.resolve_deferred("parking", "spot-A")
        # Only agent_high's intent is active — agent_low's expired
        assert len(results) == 1
        assert agent_high.id in results

    # -- P33: Deferred Priority Fidelity --

    def test_p31_highest_confidence_wins(
        self,
        deferred_guard: Guard,
        agent_low: Agent,
        agent_mid: Agent,
        agent_high: Agent,
    ) -> None:
        """P33: Winner MUST be the highest-confidence intent, regardless of write order."""
        # Low-confidence agent writes first
        deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.3
        )
        # High-confidence agent writes second
        deferred_guard.pre_action(
            agent_high, "parking", "spot-A", "reserve", confidence=0.95
        )
        # Mid-confidence agent writes last
        deferred_guard.pre_action(
            agent_mid, "parking", "spot-A", "reserve", confidence=0.6
        )

        results = deferred_guard.resolve_deferred("parking", "spot-A")

        # High-confidence agent wins despite writing second
        assert results[agent_high.id].verdict == GuardVerdict.ALLOW
        assert results[agent_low.id].verdict == GuardVerdict.CONFLICT
        assert results[agent_mid.id].verdict == GuardVerdict.CONFLICT

    def test_p31_no_serialization_effect(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
        agent_high: Agent,
    ) -> None:
        """P33: Result identical to simultaneous evaluation — no first-writer advantage."""
        from markspace.core import resolve_conflict

        # Write intents to deferred guard
        deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.5
        )
        deferred_guard.pre_action(
            agent_high, "parking", "spot-A", "reserve", confidence=0.95
        )

        # Get deferred result
        deferred_results = deferred_guard.resolve_deferred("parking", "spot-A")

        # Compare with direct resolve_conflict on the same intents
        all_intents = deferred_space.get_intents("parking", "spot-A")
        simultaneous_winner = resolve_conflict(
            all_intents, ConflictPolicy.HIGHEST_CONFIDENCE
        )
        assert simultaneous_winner is not None

        # Find the agent_id of the simultaneous winner
        winner_intent = next(i for i in all_intents if i.id == simultaneous_winner)
        winner_agent_id = winner_intent.agent_id

        # Deferred and simultaneous must agree on the winner
        assert deferred_results[winner_agent_id].verdict == GuardVerdict.ALLOW

    def test_p31_tie_broken_by_created_at(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
        agent_high: Agent,
    ) -> None:
        """P33: Ties in confidence are broken by created_at (earliest wins)."""
        # Both agents write with same confidence
        deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.8
        )
        deferred_space.set_clock(1_000_001.0)  # 1 second later
        deferred_guard.pre_action(
            agent_high, "parking", "spot-A", "reserve", confidence=0.8
        )

        results = deferred_guard.resolve_deferred("parking", "spot-A")

        # Earlier intent wins the tie
        assert results[agent_low.id].verdict == GuardVerdict.ALLOW
        assert results[agent_high.id].verdict == GuardVerdict.CONFLICT

    # -- P34: Deferred Liveness --

    def test_p32_resolution_produces_exactly_one_winner(
        self,
        deferred_guard: Guard,
        agent_low: Agent,
        agent_mid: Agent,
        agent_high: Agent,
    ) -> None:
        """P34: Resolution boundary produces exactly one ALLOW verdict (progress)."""
        deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.3
        )
        deferred_guard.pre_action(
            agent_mid, "parking", "spot-A", "reserve", confidence=0.6
        )
        deferred_guard.pre_action(
            agent_high, "parking", "spot-A", "reserve", confidence=0.95
        )

        results = deferred_guard.resolve_deferred("parking", "spot-A")

        allow_count = sum(
            1 for d in results.values() if d.verdict == GuardVerdict.ALLOW
        )
        assert allow_count == 1, f"Expected exactly 1 ALLOW, got {allow_count}"

    def test_p32_empty_scope_returns_empty(
        self,
        deferred_guard: Guard,
    ) -> None:
        """P34: No intents → empty result (no deadlock, no phantom decisions)."""
        results = deferred_guard.resolve_deferred("parking", "nonexistent-spot")
        assert results == {}

    def test_p32_ttl_provides_safety_net(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
    ) -> None:
        """P34: Even without resolve_deferred, TTL expiry prevents indefinite accumulation."""
        deferred_guard.pre_action(
            agent_low, "parking", "spot-A", "reserve", confidence=0.5
        )

        # Advance past TTL
        deferred_space.set_clock(1_000_000.0 + 2000.0)

        # Intent expired — resolve_deferred finds nothing
        results = deferred_guard.resolve_deferred("parking", "spot-A")
        assert results == {}

    # -- Deferred mode does not affect immediate scopes --

    def test_immediate_scope_unaffected(self) -> None:
        """Deferred mode on one scope doesn't affect immediate scopes."""
        immediate_scope = Scope(
            name="calendar",
            intent_actions=("book",),
            action_actions=("booked",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
            conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
            deferred=False,
        )
        deferred_scope = Scope(
            name="parking",
            intent_actions=("reserve",),
            action_actions=("reserved",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
            conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
            deferred=True,
        )
        space = MarkSpace(scopes=[immediate_scope, deferred_scope])
        space.set_clock(1_000_000.0)
        guard = Guard(space)

        agent = Agent(
            name="worker",
            scopes={
                "calendar": ["intent", "action", "need"],
                "parking": ["intent", "action", "need"],
            },
        )

        # Immediate scope: gets ALLOW immediately
        d_cal = guard.pre_action(agent, "calendar", "mon-9:00", "book", confidence=0.9)
        assert d_cal.verdict == GuardVerdict.ALLOW

        # Deferred scope: gets BLOCKED
        d_park = guard.pre_action(agent, "parking", "spot-A", "reserve", confidence=0.9)
        assert d_park.verdict == GuardVerdict.BLOCKED
