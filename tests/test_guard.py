# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Guard Tests

Tests that the deterministic enforcement layer correctly prevents conflicts
without depending on the LLM agent to "remember" to check marks.

These tests prove the key architectural revision: coordination enforcement
lives in the harness (deterministic), not in the agent (unreliable).
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from typing import Any

import pytest

from unittest.mock import MagicMock

from markspace import (
    Action,
    Agent,
    ConflictPolicy,
    ContentPolicy,
    ContentPolicyError,
    DecayConfig,
    Guard,
    NotGrantedError,
    GuardVerdict,
    InMemorySink,
    Intent,
    MarkSpace,
    MarkType,
    Need,
    Observation,
    PolicyResult,
    PolicyVerdict,
    QuotaExceededError,
    Scope,
    ScopeVisibility,
    Source,
    Warning,
    hours,
    minutes,
)
from markspace.barrier import BarrierSnapshot
from markspace.core import AnyMark, resolve_conflict, scope_contains


@pytest.fixture
def calendar_scope() -> Scope:
    return Scope(
        name="calendar",
        allowed_intent_verbs=("book", "reschedule", "cancel"),
        allowed_action_verbs=("booked", "rescheduled", "cancelled"),
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

    def test_a_granted_lease_is_not_overtaken_by_higher_confidence(
        self,
        guard: Guard,
        booker: Agent,
        optimizer: Agent,
    ) -> None:
        """In an immediate scope the first grant holds the resource (Section 6.5)."""
        d1 = guard.pre_action(
            optimizer, "calendar", "thu-14:00", "reschedule", confidence=0.4
        )
        assert d1.verdict == GuardVerdict.ALLOW  # first writer, no conflict yet

        d2 = guard.pre_action(booker, "calendar", "thu-14:00", "book", confidence=0.9)
        assert d2.verdict == GuardVerdict.CONFLICT
        assert d2.winning_intent is not None
        assert d2.winning_intent.id == d1.intent_id

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
# P39: Action precedence — completed actions block new intents
# ---------------------------------------------------------------------------


class TestActionPrecedence:
    """P39: An existing action mark on a resource MUST block new intents from other agents."""

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
        assert isinstance(marks[0], Action)
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
            allowed_intent_verbs=("modify",),
            allowed_action_verbs=("modified",),
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
        assert isinstance(marks[0], Observation)
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
        assert isinstance(marks[0], Observation)
        assert marks[0].content == "new"


# ---------------------------------------------------------------------------
# Deferred Resolution — P14, P15, P16
# Spec Section 6.2
# ---------------------------------------------------------------------------


class TestDeferredResolution:
    """Tests for the deferred resolution protocol (Spec Section 6.2)."""

    @pytest.fixture
    def deferred_scope(self) -> Scope:
        return Scope(
            name="parking",
            allowed_intent_verbs=("reserve",),
            allowed_action_verbs=("reserved",),
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

    # -- P14: Deferred Completeness --

    def test_p30_all_intents_considered(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
        agent_mid: Agent,
        agent_high: Agent,
    ) -> None:
        """P14: Batch resolution MUST consider ALL active intents at the boundary."""
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
        """P14: An intent written just before the boundary is included."""
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
        """P14: An intent past its TTL is NOT considered (correctly excluded)."""
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

    # -- P15: Deferred Priority Fidelity --

    def test_p31_highest_confidence_wins(
        self,
        deferred_guard: Guard,
        agent_low: Agent,
        agent_mid: Agent,
        agent_high: Agent,
    ) -> None:
        """P15: Winner MUST be the highest-confidence intent, regardless of write order."""
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
        """P15: Result identical to simultaneous evaluation — no first-writer advantage."""
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
        """P15: Ties in confidence are broken by created_at (earliest wins)."""
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

    # -- P16: Deferred Liveness --

    def test_p32_resolution_produces_exactly_one_winner(
        self,
        deferred_guard: Guard,
        agent_low: Agent,
        agent_mid: Agent,
        agent_high: Agent,
    ) -> None:
        """P16: Resolution boundary produces exactly one ALLOW verdict (progress)."""
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
        """P16: No intents → empty result (no deadlock, no phantom decisions)."""
        results = deferred_guard.resolve_deferred("parking", "nonexistent-spot")
        assert results == {}

    def test_p32_ttl_provides_safety_net(
        self,
        deferred_guard: Guard,
        deferred_space: MarkSpace,
        agent_low: Agent,
    ) -> None:
        """P16: Even without resolve_deferred, TTL expiry prevents indefinite accumulation."""
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
            allowed_intent_verbs=("book",),
            allowed_action_verbs=("booked",),
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
            allowed_intent_verbs=("reserve",),
            allowed_action_verbs=("reserved",),
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


# ---------------------------------------------------------------------------
# W23: Guard action write authorization check
# ---------------------------------------------------------------------------


class TestActionWriteAuthorization:
    def test_intent_only_agent_denied(self) -> None:
        """An agent with intent-only permission should be denied by the guard."""
        scope = Scope(
            name="calendar",
            allowed_intent_verbs=("book",),
            allowed_action_verbs=("booked",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)
        guard = Guard(space)

        # Agent can write intents but NOT actions
        intent_only = Agent(
            name="intent-only",
            scopes={"calendar": ["intent"]},
        )

        decision = guard.pre_action(
            intent_only, "calendar", "mon-9:00", "book", confidence=0.9
        )
        assert decision.verdict == GuardVerdict.DENIED
        assert "action" in decision.reason.lower()


# ---------------------------------------------------------------------------
# W24: tool_fn exception handling in guard.execute()
# ---------------------------------------------------------------------------


class TestToolFnExceptionHandling:
    def test_tool_exception_writes_failed_action(
        self, space: MarkSpace, guard: Guard, booker: Agent
    ) -> None:
        """If tool_fn throws, a failed action mark must supersede the intent."""

        def failing_tool():
            raise RuntimeError("API timeout")

        with pytest.raises(RuntimeError, match="API timeout"):
            guard.execute(
                booker,
                "calendar",
                "mon-9:00",
                "book",
                "booked",
                tool_fn=failing_tool,
                confidence=0.9,
            )

        # The failed action should exist and supersede the intent
        actions = space.read(
            scope="calendar", resource="mon-9:00", mark_type=MarkType.ACTION
        )
        assert len(actions) == 1
        assert isinstance(actions[0], Action)
        assert actions[0].result["status"] == "failed"
        assert "API timeout" in actions[0].result["error"]

        # The intent should be superseded (not visible)
        intents = space.get_intents("calendar", "mon-9:00")
        assert len(intents) == 0

    def test_resource_freed_after_tool_failure(
        self, space: MarkSpace, guard: Guard, booker: Agent, optimizer: Agent
    ) -> None:
        """After tool failure, another agent should be able to claim the resource."""

        def failing_tool():
            raise ValueError("fail")

        with pytest.raises(ValueError):
            guard.execute(
                booker,
                "calendar",
                "mon-9:00",
                "book",
                "booked",
                tool_fn=failing_tool,
                confidence=0.5,
            )

        # Another agent should see the failed action but should be able to
        # write their own intent (the resource has a failed action, which
        # the guard treats as claimed - this is correct behavior since the
        # action mark exists even though it failed)
        actions = space.read(
            scope="calendar", resource="mon-9:00", mark_type=MarkType.ACTION
        )
        assert len(actions) == 1


# ---------------------------------------------------------------------------
# W20: max_source enforcement at write boundary
# ---------------------------------------------------------------------------


class TestMaxSourceEnforcement:
    def test_external_agent_cannot_write_fleet_source(self) -> None:
        """An agent with max_source=EXTERNAL_VERIFIED cannot write FLEET observations."""
        scope = Scope(
            name="sensors",
            observation_topics=("*",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)

        external_agent = Agent(
            name="external-scraper",
            scopes={"sensors": ["observation"]},
            max_source=Source.EXTERNAL_VERIFIED,
        )

        # Writing FLEET source should be rejected
        from markspace.space import ScopeError

        with pytest.raises(ScopeError, match="max_source"):
            space.write(
                external_agent,
                Observation(
                    scope="sensors",
                    topic="temperature",
                    content={"value": 72},
                    source=Source.FLEET,
                ),
            )

    def test_external_agent_can_write_own_level(self) -> None:
        """An agent can write at or below its max_source level."""
        scope = Scope(
            name="sensors",
            observation_topics=("*",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)

        external_agent = Agent(
            name="external-scraper",
            scopes={"sensors": ["observation"]},
            max_source=Source.EXTERNAL_VERIFIED,
        )

        # Writing at own level should succeed
        mid = space.write(
            external_agent,
            Observation(
                scope="sensors",
                topic="temperature",
                content={"value": 72},
                source=Source.EXTERNAL_VERIFIED,
            ),
        )
        assert mid is not None

        # Writing below own level should succeed
        mid2 = space.write(
            external_agent,
            Observation(
                scope="sensors",
                topic="humidity",
                content={"value": 50},
                source=Source.EXTERNAL_UNVERIFIED,
            ),
        )
        assert mid2 is not None


# ---------------------------------------------------------------------------
# Failed actions must not permanently block resources
# ---------------------------------------------------------------------------


class TestFailedActionReleasesResource:
    """A failed tool execution should not block subsequent agents from the resource."""

    def test_failed_action_does_not_block_other_agent(
        self, space: MarkSpace, guard: Guard, booker: Agent, optimizer: Agent
    ) -> None:
        """After agent A's tool fails, agent B should be able to claim the resource."""

        def failing_tool() -> str:
            raise RuntimeError("booking API down")

        # Agent A attempts, tool fails
        with pytest.raises(RuntimeError, match="booking API down"):
            guard.execute(
                agent=booker,
                scope="calendar",
                resource="thu-14:00",
                intent_action="book",
                result_action="booked",
                tool_fn=failing_tool,
                confidence=0.9,
            )

        # Agent B should now be able to claim the resource
        decision, result = guard.execute(
            agent=optimizer,
            scope="calendar",
            resource="thu-14:00",
            intent_action="book",
            result_action="booked",
            tool_fn=lambda: "confirmed",
            confidence=0.8,
        )
        assert decision.verdict == GuardVerdict.ALLOW
        assert result == "confirmed"

    def test_failed_action_does_not_block_same_agent(
        self, space: MarkSpace, guard: Guard, booker: Agent
    ) -> None:
        """After an agent's tool fails, it should be able to retry the same resource."""

        attempt = 0

        def flaky_tool() -> str:
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise RuntimeError("transient failure")
            return "confirmed"

        # First attempt fails
        with pytest.raises(RuntimeError, match="transient failure"):
            guard.execute(
                agent=booker,
                scope="calendar",
                resource="thu-14:00",
                intent_action="book",
                result_action="booked",
                tool_fn=flaky_tool,
                confidence=0.9,
            )

        # Retry should succeed
        decision, result = guard.execute(
            agent=booker,
            scope="calendar",
            resource="thu-14:00",
            intent_action="book",
            result_action="booked",
            tool_fn=flaky_tool,
            confidence=0.9,
        )
        assert decision.verdict == GuardVerdict.ALLOW
        assert result == "confirmed"

    def test_pre_action_ignores_failed_actions(
        self, space: MarkSpace, guard: Guard, booker: Agent, optimizer: Agent
    ) -> None:
        """pre_action should not treat failed action marks as resource claims."""
        # Write a failed action mark directly
        space.write(
            booker,
            Action(
                scope="calendar",
                resource="thu-14:00",
                action="booked",
                result={"error": "timeout"},
                failed=True,
            ),
        )

        # Another agent's pre_action should get ALLOW, not CONFLICT
        decision = guard.pre_action(
            optimizer, "calendar", "thu-14:00", "book", confidence=0.8
        )
        assert decision.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Content policy enforcement
# ---------------------------------------------------------------------------


class _AllowPolicy(ContentPolicy):
    """Always allows."""

    def evaluate(self, agent, mark, scope):
        return PolicyResult(verdict=PolicyVerdict.ALLOW)


class _RejectPolicy(ContentPolicy):
    """Always rejects."""

    def __init__(self, reason="blocked"):
        self._reason = reason

    def evaluate(self, agent, mark, scope):
        return PolicyResult(verdict=PolicyVerdict.REJECT, reason=self._reason)


class _RewritePolicy(ContentPolicy):
    """Rewrites content to a fixed value."""

    def __init__(self, new_content="sanitized"):
        self._new_content = new_content

    def evaluate(self, agent, mark, scope):
        return PolicyResult(verdict=PolicyVerdict.REWRITE, content=self._new_content)


class _FlagPolicy(ContentPolicy):
    """Always flags."""

    def __init__(self, reason="suspicious"):
        self._reason = reason

    def evaluate(self, agent, mark, scope):
        return PolicyResult(verdict=PolicyVerdict.FLAG, reason=self._reason)


class _RecordingPolicy(ContentPolicy):
    """Records what it sees for assertions."""

    def __init__(self):
        self.seen: list = []

    def evaluate(self, agent, mark, scope):
        self.seen.append(mark)
        return PolicyResult(verdict=PolicyVerdict.ALLOW)


@pytest.fixture
def notes_scope() -> Scope:
    return Scope(
        name="notes",
        observation_topics=("*",),
        warning_topics=("*",),
        decay=DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=minutes(30),
        ),
    )


@pytest.fixture
def notes_space(notes_scope: Scope) -> MarkSpace:
    s = MarkSpace(scopes=[notes_scope])
    s.set_clock(1_000_000.0)
    return s


@pytest.fixture
def writer() -> Agent:
    return Agent(
        name="writer",
        scopes={"notes": ["observation", "warning", "need"]},
    )


class TestContentPolicies:
    """Content policy middleware in the Guard's write path."""

    def test_allow_passthrough(self, notes_space, writer):
        """ALLOW verdict writes mark unchanged."""
        guard = Guard(notes_space, content_policies=[_AllowPolicy()])
        mark_id = guard.write_mark(
            writer, Observation(scope="notes", topic="t", content="hello")
        )
        marks = notes_space.read(scope="notes")
        obs = [m for m in marks if isinstance(m, Observation)]
        assert len(obs) == 1
        assert obs[0].content == "hello"
        assert obs[0].id == mark_id

    def test_reject_blocks_write(self, notes_space, writer):
        """REJECT verdict raises ContentPolicyError, no mark stored."""
        guard = Guard(notes_space, content_policies=[_RejectPolicy("bad content")])
        with pytest.raises(ContentPolicyError, match="bad content"):
            guard.write_mark(
                writer, Observation(scope="notes", topic="t", content="evil")
            )
        obs = [m for m in notes_space.read(scope="notes") if isinstance(m, Observation)]
        assert len(obs) == 0

    def test_rewrite_modifies_content(self, notes_space, writer):
        """REWRITE verdict stores the rewritten content."""
        guard = Guard(notes_space, content_policies=[_RewritePolicy("cleaned")])
        guard.write_mark(
            writer,
            Observation(scope="notes", topic="t", content="WORLD RECORD"),
        )
        obs = [m for m in notes_space.read(scope="notes") if isinstance(m, Observation)]
        assert len(obs) == 1
        assert obs[0].content == "cleaned"

    def test_flag_allows_but_writes_warning(self, notes_space, writer):
        """FLAG verdict writes the mark and emits a Warning to _system scope."""
        guard = Guard(notes_space, content_policies=[_FlagPolicy("suspicious")])
        guard.write_mark(writer, Observation(scope="notes", topic="t", content="hmm"))
        obs = [m for m in notes_space.read(scope="notes") if isinstance(m, Observation)]
        assert len(obs) == 1
        assert obs[0].content == "hmm"

        # Warning is written to _system scope (where the guard agent has perms)
        warnings = [
            m for m in notes_space.read(scope="_system") if isinstance(m, Warning)
        ]
        flag_warnings = [w for w in warnings if w.topic == "content-policy-flag"]
        assert len(flag_warnings) == 1
        assert "suspicious" in flag_warnings[0].reason

    def test_pipeline_composition_rewrite_feeds_next(self, notes_space, writer):
        """REWRITE from policy A feeds the rewritten mark into policy B."""
        recorder = _RecordingPolicy()
        guard = Guard(
            notes_space,
            content_policies=[_RewritePolicy("step1"), recorder],
        )
        guard.write_mark(
            writer, Observation(scope="notes", topic="t", content="original")
        )
        assert len(recorder.seen) == 1
        assert recorder.seen[0].content == "step1"

    def test_first_reject_wins(self, notes_space, writer):
        """After first REJECT, subsequent policies are not called."""
        recorder = _RecordingPolicy()
        guard = Guard(
            notes_space,
            content_policies=[_RejectPolicy("nope"), recorder],
        )
        with pytest.raises(ContentPolicyError):
            guard.write_mark(
                writer,
                Observation(scope="notes", topic="t", content="x"),
            )
        assert len(recorder.seen) == 0

    def test_no_policies_passthrough(self, notes_space, writer):
        """Empty policy list has no effect on writes."""
        guard = Guard(notes_space, content_policies=[])
        guard.write_mark(writer, Observation(scope="notes", topic="t", content="fine"))
        obs = [m for m in notes_space.read(scope="notes") if isinstance(m, Observation)]
        assert len(obs) == 1
        assert obs[0].content == "fine"

    def test_non_string_content(self, notes_space, writer):
        """Policies receive marks with non-string content (dict)."""
        recorder = _RecordingPolicy()
        guard = Guard(notes_space, content_policies=[recorder])
        guard.write_mark(
            writer,
            Observation(scope="notes", topic="t", content={"key": "value"}),
        )
        assert recorder.seen[0].content == {"key": "value"}

    def test_flag_does_not_feed_envelope(self, notes_space, writer):
        """FLAG verdict does NOT call envelope.record_attempt (P70)."""
        envelope = MagicMock()
        from markspace.envelope import EnvelopeVerdict

        envelope.check.return_value = EnvelopeVerdict.NORMAL

        guard = Guard(
            notes_space,
            envelope=envelope,
            content_policies=[_FlagPolicy("sus")],
        )
        guard.write_mark(writer, Observation(scope="notes", topic="t", content="x"))
        envelope.record_attempt.assert_not_called()

    def test_policies_skip_warning_marks(self, notes_space, writer):
        """Content policies are NOT evaluated for Warning marks (P69)."""
        recorder = _RecordingPolicy()
        guard = Guard(notes_space, content_policies=[recorder])
        guard.write_mark(
            writer,
            Warning(scope="notes", topic="test", reason="something wrong"),
        )
        assert len(recorder.seen) == 0

    def test_policies_skip_need_marks(self, notes_space, writer):
        """Content policies are NOT evaluated for Need marks (P69)."""
        recorder = _RecordingPolicy()
        guard = Guard(notes_space, content_policies=[recorder])
        guard.write_mark(
            writer,
            Need(scope="notes", question="help?"),
        )
        assert len(recorder.seen) == 0

    def test_telemetry_emitted_per_policy(self, notes_space, writer):
        """Each policy evaluation emits a telemetry event."""
        sink = InMemorySink()
        guard = Guard(
            notes_space,
            telemetry=sink,
            content_policies=[_AllowPolicy(), _AllowPolicy()],
        )
        guard.write_mark(writer, Observation(scope="notes", topic="t", content="x"))
        policy_events = [e for e in sink.events if e.operation == "content_policy"]
        assert len(policy_events) == 2
        assert all(e.verdict == "allow" for e in policy_events)

    def test_content_policy_error_is_scope_error(self):
        """ContentPolicyError inherits from ScopeError for catch compatibility."""
        from markspace.space import ScopeError

        assert issubclass(ContentPolicyError, ScopeError)


class TestDeferredAuthorization:
    """Deferred resolution re-checks each intent's agent against the intent's
    own scope, not the scope whose boundary resolves it."""

    def test_child_scope_intent_keeps_its_authorization_at_a_parent_boundary(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/merger", deferred=True),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        insider = Agent(name="insider", scopes={"corp/merger": ["intent", "action"]})
        outsider = Agent(name="outsider", scopes={"corp": ["intent", "action"]})
        assert guard.pre_action(insider, "corp/merger", "acme", "buy", 0.9).verdict == (
            GuardVerdict.BLOCKED
        )
        guard.pre_action(outsider, "corp", "acme", "buy", 0.1)
        decisions = guard.resolve_deferred(
            "corp", "acme", agents={insider.id: insider, outsider.id: outsider}
        )
        # The insider is judged, not refused as "revoked"; the outer-scope
        # intent wins (Section 6.1).
        assert decisions[insider.id].verdict == GuardVerdict.CONFLICT
        assert decisions[outsider.id].verdict == GuardVerdict.ALLOW


class TestGuardDecisionVisibility:
    """P38 with P22: a decision names no intent or agent from a scope the
    deciding agent cannot read. The verdict itself still enforces mutual
    exclusion (Section 7.4, CLASSIFIED)."""

    @staticmethod
    def _space() -> MarkSpace:
        from markspace import ScopeVisibility

        s = MarkSpace(
            scopes=[
                Scope(name="corp", visibility=ScopeVisibility.PROTECTED),
                Scope(name="corp/merger", visibility=ScopeVisibility.CLASSIFIED),
            ]
        )
        s.set_clock(1000000.0)
        return s

    @staticmethod
    def _insider() -> Agent:
        return Agent(
            name="insider",
            scopes={"corp/merger": ["intent", "action"]},
            read_scopes=frozenset({"corp/merger"}),
        )

    @staticmethod
    def _vault() -> MarkSpace:
        """A CLASSIFIED outer scope over a PROTECTED inner one."""
        from markspace import ScopeVisibility

        s = MarkSpace(
            scopes=[
                Scope(name="vault", visibility=ScopeVisibility.CLASSIFIED),
                Scope(name="vault/desk", visibility=ScopeVisibility.PROTECTED),
            ]
        )
        s.set_clock(1000000.0)
        return s

    def test_conflict_withholds_unreadable_winning_intent(self):
        s = self._vault()
        guard = Guard(s)
        insider = Agent(
            name="insider",
            scopes={"vault": ["intent", "action"]},
            read_scopes=frozenset({"vault"}),
        )
        assert guard.pre_action(insider, "vault", "acme", "buy", 0.1).verdict == (
            GuardVerdict.ALLOW
        )
        outsider = Agent(name="outsider", scopes={"vault/desk": ["intent", "action"]})
        d = guard.pre_action(outsider, "vault/desk", "acme", "buy", 0.9)
        assert d.verdict == GuardVerdict.CONFLICT
        assert d.winning_intent is None
        assert all(i.scope != "vault" for i in d.conflicting_intents)
        assert "0.1" not in d.reason and str(insider.id) not in d.reason

    def test_conflict_names_readable_winning_intent(self):
        s = self._vault()
        guard = Guard(s)
        insider = Agent(
            name="insider",
            scopes={"vault": ["intent", "action"]},
            read_scopes=frozenset({"vault"}),
        )
        guard.pre_action(insider, "vault", "acme", "buy", 0.1)
        reader = Agent(
            name="reader",
            scopes={"vault/desk": ["intent", "action"]},
            read_scopes=frozenset({"vault"}),
        )
        d = guard.pre_action(reader, "vault/desk", "acme", "buy", 0.9)
        assert d.verdict == GuardVerdict.CONFLICT
        assert d.winning_intent is not None
        assert d.winning_intent.agent_id == insider.id

    def test_claimed_resource_reason_hides_unreadable_agent(self):
        s = self._space()
        guard = Guard(s)
        insider = self._insider()
        decision, _ = guard.execute(
            agent=insider,
            scope="corp/merger",
            resource="acme",
            intent_action="buy",
            result_action="bought",
            tool_fn=lambda: "ok",
        )
        assert decision.verdict == GuardVerdict.ALLOW
        outsider = Agent(name="outsider", scopes={"corp": ["intent", "action"]})
        d = guard.pre_action(outsider, "corp", "acme", "buy", 0.1)
        assert d.verdict == GuardVerdict.CONFLICT
        assert str(insider.id) not in d.reason

    @staticmethod
    def _space_with_waiting_merger() -> MarkSpace:
        """As _space(), with a deferred corp/merger: its intents wait (Section 6.5)."""
        from markspace import ScopeVisibility

        s = MarkSpace(
            scopes=[
                Scope(name="corp", visibility=ScopeVisibility.PROTECTED),
                Scope(
                    name="corp/merger",
                    visibility=ScopeVisibility.CLASSIFIED,
                    deferred=True,
                ),
            ]
        )
        s.set_clock(1000000.0)
        return s

    def test_allow_reason_does_not_reveal_a_hidden_competitor(self):
        s = self._space_with_waiting_merger()
        guard = Guard(s)
        guard.pre_action(self._insider(), "corp/merger", "acme", "buy", 0.1)
        outsider = Agent(name="outsider", scopes={"corp": ["intent", "action"]})
        d = guard.pre_action(outsider, "corp", "acme", "buy", 0.9)
        assert d.verdict == GuardVerdict.ALLOW
        assert d.conflicting_intents == []
        assert d.reason == "No conflict"  # what an agent alone is told

    def test_telemetry_counts_conflicts_the_agent_cannot_see(self):
        sink = InMemorySink()
        s = self._space()
        guard = Guard(s, telemetry=sink)
        guard.pre_action(self._insider(), "corp/merger", "acme", "buy", 0.9)
        outsider = Agent(name="outsider", scopes={"corp": ["intent", "action"]})
        guard.execute(
            agent=outsider,
            scope="corp",
            resource="acme",
            intent_action="buy",
            result_action="bought",
            tool_fn=lambda: "ok",
        )
        execute_events = [e for e in sink.events if e.operation == "execute"]
        assert execute_events and execute_events[-1].conflict_found is True

    @staticmethod
    def _deferred_space() -> MarkSpace:
        from markspace import ScopeVisibility

        s = MarkSpace(
            scopes=[
                Scope(
                    name="legal", visibility=ScopeVisibility.CLASSIFIED, deferred=True
                ),
                Scope(name="corp", visibility=ScopeVisibility.PROTECTED, deferred=True),
                Scope(name="corp/merger", visibility=ScopeVisibility.CLASSIFIED),
            ]
        )
        s.set_clock(1000000.0)
        return s

    def test_deferred_decision_withholds_unreadable_winner(self):
        s = self._deferred_space()
        guard = Guard(s)
        a = Agent(name="a", scopes={"legal": ["intent", "action"]})
        b = Agent(name="b", scopes={"legal": ["intent", "action"]})
        guard.pre_action(a, "legal", "case", "file", 0.9)
        guard.pre_action(b, "legal", "case", "file", 0.2)
        decisions = guard.resolve_deferred("legal", "case", agents={a.id: a, b.id: b})
        d = decisions[b.id]
        assert d.verdict == GuardVerdict.CONFLICT
        assert d.winning_intent is None
        assert d.conflicting_intents == []
        assert str(a.id) not in d.reason and "0.9" not in d.reason

    def test_deferred_decision_without_agents_withholds_non_open_intents(self):
        s = self._deferred_space()
        guard = Guard(s)
        insider = self._insider()
        guard.pre_action(insider, "corp/merger", "acme", "buy", 0.9)
        outsider = Agent(name="outsider", scopes={"corp": ["intent", "action"]})
        guard.pre_action(outsider, "corp", "acme", "buy", 0.1)
        d = guard.resolve_deferred("corp", "acme")[outsider.id]
        assert d.winning_intent is None
        assert all(i.agent_id != insider.id for i in d.conflicting_intents)
        assert str(insider.id) not in d.reason

    def test_allow_reason_with_hidden_competitor_equals_lone_agent_reason(self):
        s = self._space_with_waiting_merger()
        guard = Guard(s)
        outsider = Agent(name="outsider", scopes={"corp": ["intent", "action"]})
        alone = guard.pre_action(outsider, "corp", "solo", "buy", 0.9)
        guard.pre_action(self._insider(), "corp/merger", "acme", "buy", 0.1)
        beat_hidden = guard.pre_action(outsider, "corp", "acme", "buy", 0.9)
        assert alone.verdict == beat_hidden.verdict == GuardVerdict.ALLOW
        assert beat_hidden.reason == alone.reason

    def test_allow_names_a_visible_competitor(self):
        s = MarkSpace(scopes=[Scope(name="corp"), Scope(name="corp/a", deferred=True)])
        s.set_clock(1000000.0)
        guard = Guard(s)
        a = Agent(name="a", scopes={"corp/a": ["intent", "action"]})
        b = Agent(name="b", scopes={"corp": ["intent", "action"]})
        guard.pre_action(a, "corp/a", "acme", "buy", 0.1)  # waits for its boundary
        d = guard.pre_action(b, "corp", "acme", "buy", 0.9)
        assert d.verdict == GuardVerdict.ALLOW
        assert d.reason == "This agent wins the conflict"
        assert [i.agent_id for i in d.conflicting_intents] == [a.id]

    def test_deferred_decision_names_a_readable_winner(self):
        s = self._deferred_space()
        guard = Guard(s)
        a = Agent(
            name="a",
            scopes={"legal": ["intent", "action"]},
            read_scopes=frozenset({"legal"}),
        )
        b = Agent(
            name="b",
            scopes={"legal": ["intent", "action"]},
            read_scopes=frozenset({"legal"}),
        )
        guard.pre_action(a, "legal", "case", "file", 0.9)
        guard.pre_action(b, "legal", "case", "file", 0.2)
        d = guard.resolve_deferred("legal", "case", agents={a.id: a, b.id: b})[b.id]
        assert d.verdict == GuardVerdict.CONFLICT
        assert d.winning_intent is not None and d.winning_intent.agent_id == a.id
        assert [i.agent_id for i in d.conflicting_intents] == [a.id]

    def test_deferred_decision_without_agents_names_open_intents(self):
        s = MarkSpace(scopes=[Scope(name="desk", deferred=True)])
        s.set_clock(1000000.0)
        guard = Guard(s)
        a = Agent(name="a", scopes={"desk": ["intent", "action"]})
        b = Agent(name="b", scopes={"desk": ["intent", "action"]})
        guard.pre_action(a, "desk", "d1", "book", 0.9)
        guard.pre_action(b, "desk", "d1", "book", 0.2)
        d = guard.resolve_deferred("desk", "d1")[b.id]
        assert d.winning_intent is not None and d.winning_intent.agent_id == a.id
        assert [i.agent_id for i in d.conflicting_intents] == [a.id]


class TestConflictAcrossScopeHierarchy:
    """P39 and Section 6: intents and actions on one resource compete when one
    scope contains the other, in either order of arrival."""

    @staticmethod
    def _space() -> MarkSpace:
        s = MarkSpace(scopes=[Scope(name="corp"), Scope(name="corp/merger")])
        s.set_clock(1000000.0)
        return s

    @staticmethod
    def _agents() -> tuple[Agent, Agent]:
        parent = Agent(name="parent", scopes={"corp": ["intent", "action"]})
        child = Agent(name="child", scopes={"corp/merger": ["intent", "action"]})
        return parent, child

    def test_child_sees_parent_intent(self):
        guard = Guard(self._space())
        parent, child = self._agents()
        assert guard.pre_action(parent, "corp", "acme", "buy", 0.9).verdict == (
            GuardVerdict.ALLOW
        )
        d = guard.pre_action(child, "corp/merger", "acme", "buy", 0.1)
        assert d.verdict == GuardVerdict.CONFLICT

    def test_parent_outranks_child_intent(self):
        s = MarkSpace(
            scopes=[Scope(name="corp"), Scope(name="corp/merger", deferred=True)]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        parent, child = self._agents()
        guard.pre_action(child, "corp/merger", "acme", "buy", 0.9)
        d = guard.pre_action(parent, "corp", "acme", "buy", 0.1)
        assert d.verdict == GuardVerdict.ALLOW
        assert [i.agent_id for i in d.conflicting_intents] == [child.id]

    def test_parent_action_claims_resource_for_child(self):
        guard = Guard(self._space())
        parent, child = self._agents()
        decision, _ = guard.execute(
            agent=parent,
            scope="corp",
            resource="acme",
            intent_action="buy",
            result_action="bought",
            tool_fn=lambda: "ok",
        )
        assert decision.verdict == GuardVerdict.ALLOW
        d = guard.pre_action(child, "corp/merger", "acme", "buy", 0.9)
        assert d.verdict == GuardVerdict.CONFLICT

    def test_sibling_scopes_do_not_compete(self):
        s = MarkSpace(scopes=[Scope(name="corp/a"), Scope(name="corp/b")])
        s.set_clock(1000000.0)
        guard = Guard(s)
        a = Agent(name="a", scopes={"corp/a": ["intent", "action"]})
        b = Agent(name="b", scopes={"corp/b": ["intent", "action"]})
        guard.pre_action(a, "corp/a", "acme", "buy", 0.9)
        assert guard.pre_action(b, "corp/b", "acme", "buy", 0.1).verdict == (
            GuardVerdict.ALLOW
        )


class TestEqualTimeIntents:
    """Intents written at one clock reading keep their write order (P32)."""

    @pytest.mark.parametrize(
        "policy", [ConflictPolicy.FIRST_WRITER, ConflictPolicy.HIGHEST_CONFIDENCE]
    )
    def test_a_later_equal_intent_never_overtakes_an_allowed_one(
        self, policy: ConflictPolicy
    ) -> None:
        # The earlier intent's tool still runs: execute() releases the lock
        # around the tool. A random tie-break would let the later one win
        # about half the time; 50 runs with new ids would show it.
        for _ in range(50):
            s = MarkSpace(scopes=[Scope(name="desk", conflict_policy=policy)])
            s.set_clock(1000000.0)
            guard = Guard(s)
            a1 = Agent(name="a1", scopes={"desk": ["intent", "action"]})
            a2 = Agent(name="a2", scopes={"desk": ["intent", "action"]})
            assert guard.pre_action(a1, "desk", "d1", "book", 0.9).verdict == (
                GuardVerdict.ALLOW
            )
            assert guard.pre_action(a2, "desk", "d1", "book", 0.9).verdict == (
                GuardVerdict.CONFLICT
            )


class TestWriteOrderAfterGc:
    def test_gc_keeps_the_order_of_a_resource_with_a_live_claim(self) -> None:
        s = MarkSpace(
            scopes=[Scope(name="desk", conflict_policy=ConflictPolicy.FIRST_WRITER)]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        a0, a1, a2 = (
            Agent(name=n, scopes={"desk": ["intent", "action"]})
            for n in ("a0", "a1", "a2")
        )
        d0 = guard.pre_action(a0, "desk", "r", "book", 0.5)
        s.write(
            a0,
            Action(
                scope="desk",
                resource="r",
                action="booked",
                result=None,
                failed=True,
                supersedes=d0.intent_id,
            ),
        )
        d1 = guard.pre_action(a1, "desk", "r", "book", 0.5)
        assert d1.verdict == GuardVerdict.ALLOW  # a1's tool runs
        assert s.gc() >= 1  # a0's superseded intent goes; a1's intent stays
        assert guard.pre_action(a2, "desk", "r", "book", 0.5).verdict == (
            GuardVerdict.CONFLICT
        )


class TestQuotaCountsEachCurrentMarkOnce:
    """max_marks_per_agent counts the agent's stored marks that are not superseded."""

    @staticmethod
    def _space(quota: int) -> tuple[MarkSpace, Agent]:
        s = MarkSpace(
            scopes=[Scope(name="notes")],
            max_marks_per_agent=quota,
        )
        s.set_clock(1000000.0)
        return s, Agent(
            name="writer", scopes={"notes": ["observation", "need", "action"]}
        )

    @staticmethod
    def _obs(content: str, supersedes: uuid.UUID | None = None) -> Observation:
        return Observation(
            scope="notes", topic="t", content=content, supersedes=supersedes
        )

    def test_superseding_write_at_quota_replaces_the_mark(self) -> None:
        s, a = self._space(quota=2)
        first = s.write(a, self._obs("v1"))
        s.write(a, self._obs("other"))
        s.write(a, self._obs("v2", supersedes=first))  # still 2 current marks
        with pytest.raises(QuotaExceededError):
            s.write(a, self._obs("third"))

    def test_gc_of_a_superseded_mark_frees_no_second_slot(self) -> None:
        s, a = self._space(quota=2)
        first = s.write(a, self._obs("v1"))
        s.write(a, self._obs("v2", supersedes=first))
        assert s.gc() == 1  # v1, superseded
        s.write(a, self._obs("other"))  # 2 current marks: v2, other
        with pytest.raises(QuotaExceededError):
            s.write(a, self._obs("third"))

    def test_second_supersession_of_one_mark_counts_the_new_mark(self) -> None:
        s, a = self._space(quota=2)
        first = s.write(a, self._obs("v1"))
        s.write(a, self._obs("v2", supersedes=first))
        s.write(a, self._obs("v3", supersedes=first))  # 2 current marks: v2, v3
        with pytest.raises(QuotaExceededError):
            s.write(a, self._obs("v4"))

    def test_resolved_need_frees_its_slot_after_gc(self) -> None:
        s, a = self._space(quota=2)
        principal = Agent(name="principal", scopes={"notes": ["action"]})
        need = s.write(a, Need(scope="notes", question="which?"))
        done = s.write(
            principal,
            Action(scope="notes", resource="r", action="answered", result=None),
        )
        s.resolve(need, done)
        assert s.gc() == 2  # the need and its resolved copy
        s.write(a, self._obs("one"))
        s.write(a, self._obs("two"))
        with pytest.raises(QuotaExceededError):
            s.write(a, self._obs("three"))


class TestSystemScopeIsClassified:
    def test_agent_without_read_authorization_sees_no_system_mark(self) -> None:
        space = MarkSpace(scopes=[Scope(name="work")])
        space.set_clock(1000000.0)
        Guard(space)
        ops = Agent(name="ops", scopes={"_system": ["warning"]})
        reader = Agent(name="reader", scopes={"work": ["observation"]})
        space.write(ops, Warning(scope="_system", topic="t", reason="names a scope"))
        assert space.read(scope="_system", reader=reader) == []
        assert len(space.read(scope="_system")) == 1

    def test_an_open_system_scope_is_refused(self) -> None:
        space = MarkSpace(scopes=[Scope(name="_system")])
        with pytest.raises(ValueError):
            Guard(space)

    def test_a_protected_system_scope_is_refused(self) -> None:
        protected = Scope(name="_system", visibility=ScopeVisibility.PROTECTED)
        with pytest.raises(ValueError):
            Guard(MarkSpace(scopes=[protected]))


class TestPerIntentCompetingSets:
    """Section 6.1: each intent is judged against its own competing set. An
    intent in an outer scope beats one in an inner scope; inside one scope the
    scope's policy decides. No two competing intents are both allowed, and the
    verdict does not depend on which boundary computes it."""

    @staticmethod
    def _tree(policy: ConflictPolicy, deferred: bool) -> MarkSpace:
        s = MarkSpace(
            scopes=[
                Scope(name="corp", conflict_policy=policy, deferred=deferred),
                Scope(name="corp/a", conflict_policy=policy, deferred=deferred),
                Scope(name="corp/b", conflict_policy=policy, deferred=deferred),
            ]
        )
        s.set_clock(1000000.0)
        return s

    @staticmethod
    def _agent(name: str, scope: str) -> Agent:
        return Agent(name=name, scopes={scope: ["intent", "action"]})

    def test_deferred_boundaries_agree_and_the_outer_scope_wins(self):
        s = self._tree(ConflictPolicy.HIGHEST_CONFIDENCE, deferred=True)
        guard = Guard(s)
        a, p, b = (
            self._agent("a", "corp/a"),
            self._agent("p", "corp"),
            self._agent("b", "corp/b"),
        )
        guard.pre_action(a, "corp/a", "r", "use", 0.9)
        guard.pre_action(p, "corp", "r", "use", 0.5)
        guard.pre_action(b, "corp/b", "r", "use", 0.1)
        agents = {x.id: x for x in (a, p, b)}
        at_parent = guard.resolve_deferred("corp", "r", agents=agents)
        at_a = guard.resolve_deferred("corp/a", "r", agents=agents)
        for agent_id in at_a:
            assert at_a[agent_id].verdict == at_parent[agent_id].verdict
        assert at_parent[p.id].verdict == GuardVerdict.ALLOW
        for loser in (a, b):
            assert at_parent[loser.id].verdict == GuardVerdict.CONFLICT
            named = at_parent[loser.id].winning_intent
            assert named is not None and named.agent_id == p.id

    def test_outer_scope_wins_whatever_the_order_or_confidence(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", conflict_policy=ConflictPolicy.FIRST_WRITER),
                Scope(
                    name="corp/merger",
                    conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
                    deferred=True,
                ),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        p, c = self._agent("p", "corp"), self._agent("c", "corp/merger")
        assert guard.pre_action(c, "corp/merger", "r", "use", 0.9).verdict == (
            GuardVerdict.BLOCKED
        )
        s.set_clock(1000001.0)  # p writes later, with lower confidence
        assert guard.pre_action(p, "corp", "r", "use", 0.1).verdict == (
            GuardVerdict.ALLOW
        )
        assert s.check_conflict("corp", "r") == s.check_conflict("corp/merger", "r")

    def test_mixed_policies_cannot_cycle(self):
        s = MarkSpace(
            scopes=[
                Scope(
                    name="corp",
                    conflict_policy=ConflictPolicy.FIRST_WRITER,
                    deferred=True,
                ),
                Scope(
                    name="corp/a",
                    conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
                    deferred=True,
                ),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        a2, c, a1 = (
            self._agent("a2", "corp/a"),
            self._agent("c", "corp"),
            self._agent("a1", "corp/a"),
        )
        guard.pre_action(a2, "corp/a", "r", "use", 0.5)
        s.set_clock(1000001.0)
        guard.pre_action(c, "corp", "r", "use", 0.5)
        s.set_clock(1000002.0)
        guard.pre_action(a1, "corp/a", "r", "use", 0.9)
        # All three wait at one boundary: two policies, and no cycle.
        decisions = guard.resolve_deferred(
            "corp/a", "r", agents={x.id: x for x in (a2, c, a1)}
        )
        allowed = [i for i, d in decisions.items() if d.verdict == GuardVerdict.ALLOW]
        assert allowed == [c.id]
        assert s.check_conflict("corp/a", "r") == s.check_conflict("corp", "r")

    def test_an_outer_intent_does_not_overtake_an_inner_lease(self):
        s = self._tree(ConflictPolicy.HIGHEST_CONFIDENCE, deferred=False)
        guard = Guard(s)
        c, p = self._agent("c", "corp/a"), self._agent("p", "corp")
        dc = guard.pre_action(c, "corp/a", "r", "use", 0.9)
        dp = guard.pre_action(p, "corp", "r", "use", 0.1)
        assert dc.verdict == GuardVerdict.ALLOW
        assert dp.verdict == GuardVerdict.CONFLICT
        assert dp.winning_intent is not None and dp.winning_intent.id == dc.intent_id
        guard.post_action(c, "corp/a", "r", "used", "ok", intent_id=dc.intent_id)
        assert [a.agent_id for a in s.get_actions("corp", "r")] == [c.id]
        # c's action holds corp/a and corp, not sibling corp/b.
        b = self._agent("b", "corp/b")
        assert guard.pre_action(b, "corp/b", "r", "use", 0.9).verdict == (
            GuardVerdict.ALLOW
        )

    def test_an_action_without_a_live_lease_is_not_recorded(self):
        s = self._tree(ConflictPolicy.HIGHEST_CONFIDENCE, deferred=False)
        guard = Guard(s)
        c, p = self._agent("c", "corp/a"), self._agent("p", "corp")
        dc = guard.pre_action(c, "corp/a", "r", "use", 0.9)
        dp = guard.pre_action(p, "corp", "r", "use", 0.1)
        guard.post_action(c, "corp/a", "r", "used", "ok", intent_id=dc.intent_id)
        with pytest.raises(NotGrantedError):
            guard.post_action(p, "corp", "r", "used", "ok", intent_id=dp.intent_id)
        assert [a.agent_id for a in s.get_actions("corp", "r")] == [c.id]

    def test_an_intent_held_by_an_action_does_not_block_siblings(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a"),
                Scope(name="corp/b"),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        a, x, b = (
            self._agent("a", "corp/a"),
            self._agent("x", "corp"),
            self._agent("b", "corp/b"),
        )
        da = guard.pre_action(a, "corp/a", "r", "use", 0.5)
        assert da.verdict == GuardVerdict.ALLOW  # a's tool starts
        # x's deferred intent lands while a's tool runs; then a's action lands.
        assert guard.pre_action(x, "corp", "r", "use", 0.5).verdict == (
            GuardVerdict.BLOCKED
        )
        guard.post_action(a, "corp/a", "r", "used", "ok", intent_id=da.intent_id)
        # a's action holds x off for good: x is lost and holds sibling b off
        # no longer (Section 6.5, G3 rule 4).
        assert guard.pre_action(b, "corp/b", "r", "use", 0.5).verdict == (
            GuardVerdict.ALLOW
        )

    def test_an_outer_intent_waits_for_an_inner_lease(self):
        s = self._tree(ConflictPolicy.HIGHEST_CONFIDENCE, deferred=False)
        guard = Guard(s)
        b, x, a = (
            self._agent("b", "corp/b"),
            self._agent("x", "corp"),
            self._agent("a", "corp/a"),
        )
        db = guard.pre_action(b, "corp/b", "r", "use", 0.5)
        dx = guard.pre_action(x, "corp", "r", "use", 0.5)
        assert db.verdict == GuardVerdict.ALLOW
        assert dx.verdict == GuardVerdict.CONFLICT  # b's lease is not pre-empted
        guard.post_action(b, "corp/b", "r", "used", "ok", intent_id=db.intent_id)
        # b's action holds corp/b; corp/a competes with neither it nor a lease.
        assert guard.pre_action(a, "corp/a", "r", "use", 0.9).verdict == (
            GuardVerdict.ALLOW
        )

    def test_an_outer_intent_waits_at_a_boundary_until_the_lease_ends(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a", deferred=True),
                Scope(name="corp/b"),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        b, x, a = (
            self._agent("b", "corp/b"),
            self._agent("x", "corp"),
            self._agent("a", "corp/a"),
        )
        agents = {b.id: b, x.id: x, a.id: a}
        db = guard.pre_action(b, "corp/b", "r", "use", 0.5)
        assert db.verdict == GuardVerdict.ALLOW
        guard.pre_action(x, "corp", "r", "use", 0.5)
        assert guard.resolve_deferred("corp", "r", agents=agents)[x.id].verdict == (
            GuardVerdict.BLOCKED
        )  # b's lease holds x off; x waits
        guard.post_action(b, "corp/b", "r", "used", "ok", intent_id=db.intent_id)
        guard.pre_action(a, "corp/a", "r", "use", 0.5)
        # b's action holds x off for good; a competes with neither.
        at_a = guard.resolve_deferred("corp/a", "r", agents=agents)
        assert at_a[x.id].verdict == GuardVerdict.CONFLICT
        assert at_a[a.id].verdict == GuardVerdict.ALLOW

    def test_a_boundary_judges_sibling_intents_against_their_own_sets(self):
        s = self._tree(ConflictPolicy.HIGHEST_CONFIDENCE, deferred=True)
        guard = Guard(s)
        a, b, c = (
            self._agent("a", "corp/a"),
            self._agent("b", "corp/b"),
            self._agent("c", "corp/a"),
        )
        guard.pre_action(a, "corp/a", "r", "use", 0.4)
        da = guard.resolve_deferred("corp/a", "r", agents={a.id: a})[a.id]
        assert da.verdict == GuardVerdict.ALLOW
        guard.pre_action(b, "corp/b", "r", "use", 0.6)
        guard.pre_action(c, "corp/a", "r", "use", 0.5)  # while a's tool runs
        guard.post_action(a, "corp/a", "r", "used", "ok", intent_id=da.intent_id)
        # a's action holds corp/a only; b in sibling corp/b competes with none
        # of these intents and that action, at the corp boundary too.
        at_parent = guard.resolve_deferred("corp", "r", agents={b.id: b, c.id: c})
        assert at_parent[c.id].verdict == GuardVerdict.CONFLICT
        assert "already claimed" in at_parent[c.id].reason  # by a's action
        assert at_parent[b.id].verdict == GuardVerdict.ALLOW

    def test_an_agent_missing_from_the_mapping_still_holds_its_claim(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a", deferred=True),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        x, a = self._agent("x", "corp"), self._agent("a", "corp/a")
        guard.pre_action(x, "corp", "r", "use", 0.5)
        guard.pre_action(a, "corp/a", "r", "use", 0.5)
        # The mapping holds only a: x cannot be checked, is denied, and still
        # counts: a is not granted, and waits.
        at_a = guard.resolve_deferred("corp/a", "r", agents={a.id: a})
        assert at_a[x.id].verdict == GuardVerdict.DENIED
        assert at_a[a.id].verdict == GuardVerdict.BLOCKED

    def test_a_boundary_judges_every_intent_at_one_time(self):
        readings: list[float] = []
        action_reads: list[float | None] = []

        class SteppingSpace(MarkSpace):
            def now(self) -> float:
                return readings.pop(0) if readings else super().now()

            def get_actions(
                self, scope: str, resource: str, *, now: float | None = None
            ) -> list[Action]:
                action_reads.append(now)
                return super().get_actions(scope, resource, now=now)

        s = SteppingSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a", deferred=True),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        x, a = self._agent("x", "corp"), self._agent("a", "corp/a")
        guard.pre_action(x, "corp", "r", "use", 0.5)
        s.set_clock(1000001.0)
        guard.pre_action(a, "corp/a", "r", "use", 0.5)
        ttl = s.get_scope("corp").decay.intent_ttl
        # The first reading finds x live; every later one finds it expired.
        readings.append(1000000.0 + ttl - 1)
        s.set_clock(1000000.0 + ttl + 1)
        action_reads.clear()
        d = guard.resolve_deferred("corp/a", "r", agents={x.id: x, a.id: a})
        assert d[x.id].verdict == GuardVerdict.ALLOW
        assert d[a.id].verdict == GuardVerdict.CONFLICT
        # Actions are read at the same time as intents.
        assert action_reads and set(action_reads) == {1000000.0 + ttl - 1}

    def test_a_boundary_checks_each_intent_once(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a", deferred=True),
            ]
        )
        s.set_clock(1000000.0)
        x, a = self._agent("x", "corp"), self._agent("a", "corp/a")
        checks: dict[uuid.UUID, int] = {}

        class ChangingGuard(Guard):
            """x's barrier narrows right after the boundary first checks x."""

            def _deferred_refusal(
                self,
                intent: Intent,
                agents: dict[uuid.UUID, Agent] | None,
                barriers: dict[uuid.UUID, BarrierSnapshot | None],
                now: float,
            ) -> str | None:
                checks[intent.id] = checks.get(intent.id, 0) + 1
                if intent.agent_id == x.id and checks[intent.id] > 1:
                    return "blocked by barrier at resolution time"
                return super()._deferred_refusal(intent, agents, barriers, now)

        guard = ChangingGuard(s)
        guard.pre_action(x, "corp", "r", "use", 0.5)
        guard.pre_action(a, "corp/a", "r", "use", 0.5)
        d = guard.resolve_deferred("corp/a", "r", agents={x.id: x, a.id: a})
        assert d[x.id].verdict == GuardVerdict.ALLOW
        assert d[a.id].verdict == GuardVerdict.CONFLICT

    def test_no_gc_runs_during_a_boundary(self):
        """gc from another thread waits for the boundary (Section 6.2.3)."""
        gc_done: list[threading.Event] = []

        class GcDuringBoundary(MarkSpace):
            reads = 0

            def get_intents(
                self, scope: str, resource: str, *, now: float | None = None
            ) -> list[Intent]:
                GcDuringBoundary.reads += 1
                if GcDuringBoundary.reads == 2:  # between two set reads
                    done = threading.Event()
                    threading.Thread(
                        target=lambda: (self.gc(), done.set()), daemon=True
                    ).start()
                    done.wait(0.3)
                    gc_done.append(done)
                return super().get_intents(scope, resource, now=now)

        s = GcDuringBoundary(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a", deferred=True),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        x, a = self._agent("x", "corp"), self._agent("a", "corp/a")
        guard.pre_action(x, "corp", "r", "use", 0.5)
        guard.pre_action(a, "corp/a", "r", "use", 0.5)
        GcDuringBoundary.reads = 0
        guard.resolve_deferred("corp/a", "r", agents={x.id: x, a.id: a})
        assert gc_done and not gc_done[0].is_set()  # gc waited
        assert gc_done[0].wait(5)  # and ran after the boundary

    def test_a_boundary_reads_the_agents_map_once(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a", deferred=True),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        x, a = self._agent("x", "corp"), self._agent("a", "corp/a")
        guard.pre_action(x, "corp", "r", "use", 0.5)
        guard.pre_action(a, "corp/a", "r", "use", 0.5)

        class Registry(dict[uuid.UUID, Agent]):
            """x registers after the boundary looks it up."""

            def get(self, key: uuid.UUID, default: Any = None) -> Any:
                found = super().get(key, default)
                if key == x.id:  # x registers right after the lookup missed it
                    self[x.id] = x
                return found

        at_a = guard.resolve_deferred("corp/a", "r", agents=Registry({a.id: a}))
        # Read once: x stays missing, denied but counting; a waits behind it.
        assert at_a[x.id].verdict == GuardVerdict.DENIED
        assert at_a[a.id].verdict == GuardVerdict.BLOCKED

    def test_the_space_is_not_read_under_the_barrier_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        s = MarkSpace(scopes=[Scope(name="corp", deferred=True)])
        s.set_clock(1000000.0)
        guard = Guard(s)
        x = self._agent("x", "corp")
        barrier = guard.get_or_create_barrier(x.id)
        barrier.require_need("corp")
        held: list[bool] = []
        real_read = s.read

        def read(**query: Any) -> list[AnyMark]:
            held.append(guard._barrier_lock.locked())
            return real_read(**query)

        monkeypatch.setattr(s, "read", read)
        guard._check_barrier(x, "corp", MarkType.INTENT)
        assert held and not any(held)

    def test_an_intent_that_loses_is_not_sent_to_the_principal(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp"),
                Scope(name="corp/x"),
                Scope(name="corp/x/board", conflict_policy=ConflictPolicy.YIELD_ALL),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        p, c = self._agent("p", "corp"), self._agent("c", "corp/x/board")
        i = Agent(name="i", scopes={"corp/x": ["intent", "action", "need"]})
        dp = guard.pre_action(p, "corp", "r", "use", 0.5)
        assert dp.verdict == GuardVerdict.ALLOW
        guard.pre_action(c, "corp/x/board", "r", "use", 0.5)
        # p beats i, so no principal answer on the YIELD_ALL pair (i, c)
        # could let i proceed: i loses to p.
        d = guard.pre_action(i, "corp/x", "r", "use", 0.5)
        assert d.verdict == GuardVerdict.CONFLICT
        assert d.winning_intent is not None and d.winning_intent.id == dp.intent_id

    def test_every_intent_compared_at_a_boundary_is_checked_for_authorization(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/a", deferred=True),
                Scope(
                    name="corp/b",
                    conflict_policy=ConflictPolicy.YIELD_ALL,
                    deferred=True,
                ),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        b = self._agent("b", "corp/b")
        p, a = self._agent("p", "corp"), self._agent("a", "corp/a")
        guard.pre_action(b, "corp/b", "r", "use", 0.5)
        guard.pre_action(p, "corp", "r", "use", 0.5)
        guard.pre_action(a, "corp/a", "r", "use", 0.5)
        revoked_b = Agent(name="b", id=b.id, scopes={})
        agents = {revoked_b.id: revoked_b, p.id: p, a.id: a}
        at_a = guard.resolve_deferred("corp/a", "r", agents=agents)
        at_parent = guard.resolve_deferred("corp", "r", agents=agents)
        assert at_parent[b.id].verdict == GuardVerdict.DENIED
        assert at_a[p.id].verdict == GuardVerdict.ALLOW
        assert p.id not in at_parent  # granted at corp/a; holds its lease

    def test_deferred_boundary_respects_an_existing_action(self):
        s = MarkSpace(scopes=[Scope(name="desk", deferred=True)])
        s.set_clock(1000000.0)
        guard = Guard(s)
        a, b = self._agent("a", "desk"), self._agent("b", "desk")
        guard.pre_action(a, "desk", "d1", "book", 0.9)
        first = guard.resolve_deferred("desk", "d1", agents={a.id: a, b.id: b})
        assert first[a.id].verdict == GuardVerdict.ALLOW
        guard.pre_action(b, "desk", "d1", "book", 0.2)  # while a's tool runs
        guard.post_action(
            a, "desk", "d1", "booked", "ok", intent_id=first[a.id].intent_id
        )
        second = guard.resolve_deferred("desk", "d1", agents={a.id: a, b.id: b})
        assert second[b.id].verdict == GuardVerdict.CONFLICT
        assert "already claimed" in second[b.id].reason

    def test_competing_scopes_share_one_resource_lock(self):
        import threading

        guard = Guard(self._tree(ConflictPolicy.HIGHEST_CONFIDENCE, deferred=False))
        entered = threading.Event()

        def child() -> None:
            with guard._resource_lock("corp/a", "r"):
                entered.set()

        with guard._resource_lock("corp", "r"):
            t = threading.Thread(target=child)
            t.start()
            assert not entered.wait(0.2)
        t.join(2)
        assert entered.is_set()

    def test_first_writer_inside_one_scope(self):
        s = MarkSpace(
            scopes=[Scope(name="desk", conflict_policy=ConflictPolicy.FIRST_WRITER)]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        a, b = self._agent("a", "desk"), self._agent("b", "desk")
        da = guard.pre_action(a, "desk", "d1", "book", 0.1)
        s.set_clock(1000001.0)
        db = guard.pre_action(b, "desk", "d1", "book", 0.9)
        assert da.verdict == GuardVerdict.ALLOW
        assert db.verdict == GuardVerdict.CONFLICT
        assert s.check_conflict("desk", "d1") == da.intent_id

    def test_yield_all_on_either_side_blocks_the_pair(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE),
                Scope(name="corp/board", conflict_policy=ConflictPolicy.YIELD_ALL),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        p, c = self._agent("p", "corp"), self._agent("c", "corp/board")
        guard.pre_action(c, "corp/board", "r", "use", 0.1)
        d = guard.pre_action(p, "corp", "r", "use", 0.9)
        assert d.verdict == GuardVerdict.BLOCKED


class TestBatchJudging:
    """judge_all() gives the pairwise rule's result (outranks) on every input,
    with one sort per scope; the boundary reads barriers and needs as they
    stand when it runs."""

    @staticmethod
    def _pairwise(
        space: MarkSpace, intent: Intent, competitors: list[Intent]
    ) -> tuple[bool, bool, uuid.UUID | None]:
        beaters: list[Intent] = []
        yield_all = False
        for other in competitors:
            if other.id == intent.id:
                continue
            wins = space.outranks(intent, other)
            if wins is None:
                yield_all = True
            elif not wins:
                beaters.append(other)
        if not beaters:
            return (not yield_all, yield_all, None)
        outer = min((b.scope for b in beaters), key=lambda sc: sc.count("/"))
        first = [b for b in beaters if b.scope == outer]
        policy = space.get_scope(outer).conflict_policy
        return (False, False, resolve_conflict(first, policy))

    def test_batch_equals_pairwise_on_random_trees(self):
        rng = random.Random(1101)
        names = ["t", "t/a", "t/b", "t/a/x", "t/a/y", "t/b/z"]
        for _ in range(2000):
            space = MarkSpace(
                scopes=[
                    Scope(name=n, conflict_policy=rng.choice(list(ConflictPolicy)))
                    for n in names
                ]
            )
            intents = [
                Intent(
                    scope=rng.choice(names),
                    resource="r",
                    action="use",
                    confidence=rng.choice([0.2, 0.5, 0.9]),
                    agent_id=uuid.uuid4(),
                    created_at=rng.choice([1.0, 2.0, 3.0]),
                )
                for _ in range(rng.randint(1, 9))
            ]
            judged = space.judge_all(intents)
            for i in intents:
                competitors = [
                    o
                    for o in intents
                    if scope_contains(o.scope, i.scope)
                    or scope_contains(i.scope, o.scope)
                ]
                j = judged[i.id]
                got = (j.wins, j.yield_all, j.winner.id if j.winner else None)
                assert got == self._pairwise(space, i, competitors)

    def test_a_restriction_made_while_the_boundary_waits_counts(self):
        s = MarkSpace(scopes=[Scope(name="corp", deferred=True)])
        s.set_clock(1000000.0)
        guard = Guard(s)
        x = Agent(name="x", scopes={"corp": ["intent", "action"]})
        guard.pre_action(x, "corp", "r", "use", 0.5)
        result: dict[uuid.UUID, Any] = {}
        with guard._resource_lock("corp", "r"):
            t = threading.Thread(
                target=lambda: result.update(
                    guard.resolve_deferred("corp", "r", agents={x.id: x})
                )
            )
            t.start()
            time.sleep(0.2)  # the boundary now waits for the resource lock
            guard.get_or_create_barrier(x.id).narrow("corp", "intent")
        t.join()
        assert result[x.id].verdict == GuardVerdict.DENIED

    def test_the_need_read_uses_the_boundary_time(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        s = MarkSpace(scopes=[Scope(name="corp", deferred=True)])
        s.set_clock(1000000.0)
        guard = Guard(s)
        x = Agent(name="x", scopes={"corp": ["intent", "action", "need"]})
        guard.pre_action(x, "corp", "r", "use", 0.5)
        guard.get_or_create_barrier(x.id).require_need("corp")
        times: list[float | None] = []
        real_read = s.read

        def read(**query: Any) -> list[AnyMark]:
            times.append(query.get("now"))
            return real_read(**query)

        monkeypatch.setattr(s, "read", read)
        guard.resolve_deferred("corp", "r", agents={x.id: x})
        assert times == [1000000.0]

    def test_each_named_intent_is_checked_by_its_own_scope(self):
        s = MarkSpace(
            scopes=[
                Scope(name="corp", deferred=True),
                Scope(name="corp/pub", deferred=True),
                Scope(
                    name="corp/sec",
                    deferred=True,
                    visibility=ScopeVisibility.CLASSIFIED,
                ),
            ]
        )
        s.set_clock(1000000.0)
        guard = Guard(s)
        x = Agent(name="x", scopes={"corp": ["intent", "action"]})
        p = Agent(name="p", scopes={"corp/pub": ["intent", "action"]})
        c = Agent(name="c", scopes={"corp/sec": ["intent", "action"]})
        guard.pre_action(x, "corp", "r", "use", 0.5)
        dp = guard.pre_action(p, "corp/pub", "r", "use", 0.5)
        guard.pre_action(c, "corp/sec", "r", "use", 0.5)
        d = guard.resolve_deferred("corp", "r", agents={a.id: a for a in (x, p, c)})
        assert [i.id for i in d[x.id].conflicting_intents] == [dp.intent_id]

    def test_a_lone_intent_finds_no_conflict(self):
        s = MarkSpace(scopes=[Scope(name="corp", deferred=True)])
        s.set_clock(1000000.0)
        guard = Guard(s)
        x = Agent(name="x", scopes={"corp": ["intent", "action"]})
        guard.pre_action(x, "corp", "r", "use", 0.5)
        d = guard.resolve_deferred("corp", "r", agents={x.id: x})
        assert d[x.id].verdict == GuardVerdict.ALLOW and not d[x.id].conflict_found


class TestBoundaryDecisionLists:
    """Each decision lists every other competing intent once, never its own (P38)."""

    def test_a_decision_lists_each_other_competitor_once_and_never_itself(self) -> None:
        names = ["corp", "corp/a", "corp/b", "corp/a/x"]
        for _ in range(10):
            s = MarkSpace(scopes=[Scope(name=n, deferred=True) for n in names])
            s.set_clock(1000000.0)
            guard = Guard(s)
            agents: dict[uuid.UUID, Agent] = {}
            scope_of: dict[uuid.UUID, str] = {}
            for n in names:
                for k in range(3):
                    a = Agent(name=f"{n}-{k}", scopes={n: ["intent", "action"]})
                    agents[a.id] = a
                    d = guard.pre_action(a, n, "r", "use", 0.5)
                    assert d.intent_id is not None
                    scope_of[d.intent_id] = n
            for boundary in names:
                for d in guard.resolve_deferred(boundary, "r", agents=agents).values():
                    assert d.intent_id is not None
                    own = scope_of[d.intent_id]
                    listed = sorted(str(o.id) for o in d.conflicting_intents)
                    expected = sorted(
                        str(i)
                        for i, sc in scope_of.items()
                        if i != d.intent_id
                        and (scope_contains(sc, own) or scope_contains(own, sc))
                    )
                    assert listed == expected
