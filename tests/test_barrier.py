# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Barrier Tests

Tests that AgentBarrier correctly enforces monotonic permission restriction,
principal-authenticated restore, wildcard revocation, and agent isolation.
"""

from __future__ import annotations

import uuid

import pytest

from markspace import (
    Agent,
    ConflictPolicy,
    DecayConfig,
    Guard,
    GuardVerdict,
    Intent,
    MarkSpace,
    Scope,
    hours,
    minutes,
)
from markspace.barrier import AgentBarrier, BarrierSnapshot


def _make_barrier() -> tuple[AgentBarrier, uuid.UUID, uuid.UUID]:
    """Create a barrier with fresh agent_id and principal_token."""
    agent_id = uuid.uuid4()
    token = uuid.uuid4()
    barrier = AgentBarrier(agent_id=agent_id, _principal_token=token)
    return barrier, agent_id, token


class TestAgentBarrier:
    """Test suite for AgentBarrier (Spec Section 9.8)."""

    def test_new_barrier_allows_everything(self) -> None:
        """A fresh barrier has no revocations - everything is allowed."""
        barrier, _, _ = _make_barrier()
        assert barrier.is_allowed("calendar", "intent") is True
        assert barrier.is_allowed("email", "observation") is True
        assert barrier.is_allowed_checked("calendar", "intent") is True
        assert barrier.is_allowed_checked("email", "observation") is True
        assert barrier.needs_required("calendar") is False

    def test_narrow_blocks(self) -> None:
        """After narrow(scope, type), is_allowed_checked returns False."""
        barrier, _, _ = _make_barrier()
        barrier.narrow("calendar", "intent")

        assert barrier.is_allowed_checked("calendar", "intent") is False
        assert barrier.is_allowed("calendar", "intent") is False
        # Other scope/type combos remain allowed
        assert barrier.is_allowed_checked("calendar", "observation") is True
        assert barrier.is_allowed_checked("email", "intent") is True

    def test_narrow_monotonic(self) -> None:
        """narrow() cannot be undone by calling is_allowed_checked or other
        non-principal methods. Only principal-authenticated restore can undo."""
        barrier, _, _ = _make_barrier()
        barrier.narrow("calendar", "intent")

        # Reading permission does not restore it
        assert barrier.is_allowed_checked("calendar", "intent") is False
        assert barrier.is_allowed("calendar", "intent") is False

        # Narrowing again is idempotent, not a toggle
        barrier.narrow("calendar", "intent")
        assert barrier.is_allowed_checked("calendar", "intent") is False

        # Other mutations don't affect existing revocations
        barrier.narrow("email", "observation")
        assert barrier.is_allowed_checked("calendar", "intent") is False
        barrier.require_need("calendar")
        assert barrier.is_allowed_checked("calendar", "intent") is False

    def test_restore_wrong_token_fails(self) -> None:
        """restore() with wrong token returns False and doesn't restore."""
        barrier, _, correct_token = _make_barrier()
        wrong_token = uuid.uuid4()

        barrier.narrow("calendar", "intent")
        result = barrier.restore("calendar", "intent", wrong_token)

        assert result is False
        assert barrier.is_allowed_checked("calendar", "intent") is False

    def test_restore_correct_token(self) -> None:
        """restore() with correct token re-grants the permission."""
        barrier, _, token = _make_barrier()

        barrier.narrow("calendar", "intent")
        assert barrier.is_allowed_checked("calendar", "intent") is False

        result = barrier.restore("calendar", "intent", token)
        assert result is True
        assert barrier.is_allowed_checked("calendar", "intent") is True

    def test_restore_all(self) -> None:
        """restore_all() clears all revocations and need requirements."""
        barrier, _, token = _make_barrier()

        barrier.narrow("calendar", "intent")
        barrier.narrow("email", "observation")
        barrier.require_need("calendar")

        assert barrier.is_allowed_checked("calendar", "intent") is False
        assert barrier.needs_required("calendar") is True

        result = barrier.restore_all(token)
        assert result is True
        assert barrier.is_allowed_checked("calendar", "intent") is True
        assert barrier.is_allowed_checked("email", "observation") is True
        assert barrier.needs_required("calendar") is False

    def test_restore_all_wrong_token(self) -> None:
        """restore_all() with wrong token fails and changes nothing."""
        barrier, _, _ = _make_barrier()
        wrong_token = uuid.uuid4()

        barrier.narrow("calendar", "intent")
        barrier.require_need("calendar")

        result = barrier.restore_all(wrong_token)
        assert result is False
        assert barrier.is_allowed_checked("calendar", "intent") is False
        assert barrier.needs_required("calendar") is True

    def test_require_need(self) -> None:
        """needs_required() returns True after require_need() for that scope."""
        barrier, _, _ = _make_barrier()

        assert barrier.needs_required("calendar") is False
        barrier.require_need("calendar")
        assert barrier.needs_required("calendar") is True
        # Other scopes unaffected
        assert barrier.needs_required("email") is False

    def test_require_need_all(self) -> None:
        """require_need_all() makes needs_required() True for any scope."""
        barrier, _, _ = _make_barrier()

        barrier.require_need_all()
        assert barrier.needs_required("calendar") is True
        assert barrier.needs_required("email") is True
        assert barrier.needs_required("anything") is True

    def test_narrow_all_wildcard(self) -> None:
        """narrow_all() blocks the mark type across all scopes via wildcard."""
        barrier, _, _ = _make_barrier()
        barrier.narrow_all("intent")

        assert barrier.is_allowed_checked("calendar", "intent") is False
        assert barrier.is_allowed_checked("email", "intent") is False
        assert barrier.is_allowed_checked("files", "intent") is False
        # Other types remain allowed
        assert barrier.is_allowed_checked("calendar", "observation") is True

    def test_narrow_all_restore_wildcard(self) -> None:
        """Restoring the wildcard entry re-grants the type across scopes."""
        barrier, _, token = _make_barrier()
        barrier.narrow_all("intent")

        assert barrier.is_allowed_checked("calendar", "intent") is False
        barrier.restore("*", "intent", token)
        assert barrier.is_allowed_checked("calendar", "intent") is True
        assert barrier.is_allowed_checked("email", "intent") is True

    def test_isolation_between_agents(self) -> None:
        """Different barriers (different agents) do not interfere."""
        barrier_a, _, token_a = _make_barrier()
        barrier_b, _, token_b = _make_barrier()

        barrier_a.narrow("calendar", "intent")
        barrier_b.narrow("email", "observation")
        barrier_a.require_need("calendar")

        # A's revocation does not affect B
        assert barrier_a.is_allowed_checked("calendar", "intent") is False
        assert barrier_b.is_allowed_checked("calendar", "intent") is True

        # B's revocation does not affect A
        assert barrier_b.is_allowed_checked("email", "observation") is False
        assert barrier_a.is_allowed_checked("email", "observation") is True

        # Need requirements are independent
        assert barrier_a.needs_required("calendar") is True
        assert barrier_b.needs_required("calendar") is False

        # Tokens are not interchangeable
        assert barrier_a.restore("calendar", "intent", token_b) is False
        assert barrier_a.restore("calendar", "intent", token_a) is True

    def test_narrow_hierarchical_scope(self) -> None:
        """Narrowing a parent scope blocks child scopes."""
        barrier, _, _ = _make_barrier()
        barrier.narrow("hr", "observation")

        # Parent scope blocked
        assert barrier.is_allowed_checked("hr", "observation") is False
        # Child scope also blocked
        assert barrier.is_allowed_checked("hr/payroll", "observation") is False
        assert barrier.is_allowed_checked("hr/payroll/bonuses", "observation") is False
        # Sibling scope unaffected
        assert barrier.is_allowed_checked("engineering", "observation") is True
        # Same scope, different type unaffected
        assert barrier.is_allowed_checked("hr/payroll", "intent") is True

    def test_narrow_child_does_not_block_parent(self) -> None:
        """Narrowing a child scope does not block the parent."""
        barrier, _, _ = _make_barrier()
        barrier.narrow("hr/payroll", "observation")

        assert barrier.is_allowed_checked("hr/payroll", "observation") is False
        assert barrier.is_allowed_checked("hr", "observation") is True

    def test_require_need_hierarchical(self) -> None:
        """require_need on parent scope applies to children."""
        barrier, _, _ = _make_barrier()
        barrier.require_need("hr")

        assert barrier.needs_required("hr") is True
        assert barrier.needs_required("hr/payroll") is True
        assert barrier.needs_required("engineering") is False

    def test_snapshot_answers_as_its_barrier(self) -> None:
        """The guard reads snapshots: they apply the same revocation rules."""
        barrier, _, _ = _make_barrier()
        barrier.narrow("hr", "intent")
        barrier.narrow_all("action")
        snap = barrier.snapshot()
        for scope in ("hr", "hr/payroll", "hrx", "eng", "eng/a"):
            for mark_type in ("intent", "action", "observation"):
                assert snap.is_allowed_checked(scope, mark_type) == (
                    barrier.is_allowed_checked(scope, mark_type)
                )
        assert not snap.is_allowed_checked("hr/payroll", "intent")
        assert not snap.is_allowed_checked("eng", "action")
        assert snap.is_allowed_checked("hrx", "intent")
        barrier.require_need("hr")
        snap = barrier.snapshot()
        for scope in ("hr", "hr/payroll", "hrx", "eng"):
            assert snap.needs_required(scope) == barrier.needs_required(scope)
        barrier.require_need_all()
        assert barrier.snapshot().needs_required("eng")


class TestBarrierReadsPerCall:
    """A guard call reads the barriers of the agents it judges, not every barrier."""

    def _guard_with_barriers(self, n: int) -> tuple[MarkSpace, Guard]:
        scope = Scope(
            name="s",
            allowed_intent_verbs=("do",),
            allowed_action_verbs=("did",),
            decay=DecayConfig(
                observation_half_life=hours(1),
                warning_half_life=minutes(30),
                intent_ttl=hours(1),
            ),
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        )
        space = MarkSpace(scopes=[scope])
        guard = Guard(space)
        for _ in range(n):
            guard.get_or_create_barrier(uuid.uuid4())
        return space, guard

    def _count_snapshots(self, monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
        reads: list[uuid.UUID] = []
        original = AgentBarrier.snapshot

        def counted(barrier: AgentBarrier) -> BarrierSnapshot:
            reads.append(barrier.agent_id)
            return original(barrier)

        monkeypatch.setattr(AgentBarrier, "snapshot", counted)
        return reads

    def test_pre_action_reads_no_barrier_of_an_agent_it_does_not_judge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, guard = self._guard_with_barriers(50)
        agent = Agent(name="a", scopes={"s": ["intent", "action"]})
        reads = self._count_snapshots(monkeypatch)

        decision = guard.pre_action(agent, "s", "r", "do", 0.5)
        assert decision.verdict == GuardVerdict.ALLOW
        guard.post_action(agent, "s", "r", "did", {}, intent_id=decision.intent_id)
        guard.post_action(agent, "s", "other", "did", {})

        assert reads == []

    def test_a_competitor_barrier_is_read_once_and_still_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        space, guard = self._guard_with_barriers(50)
        rival = Agent(name="rival", scopes={"s": ["intent", "action"]})
        agent = Agent(name="a", scopes={"s": ["intent", "action"]})
        # The rival's intents are pending: they reach the space without a grant.
        for _ in range(2):
            space.write(
                rival, Intent(scope="s", resource="r", action="do", confidence=0.9)
            )
        guard.get_or_create_barrier(rival.id).narrow("s", "intent")
        reads = self._count_snapshots(monkeypatch)

        decision = guard.pre_action(agent, "s", "r", "do", 0.5)

        assert decision.verdict == GuardVerdict.ALLOW
        assert reads == [rival.id]

    def test_a_boundary_reads_only_the_barriers_of_its_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        space, guard = self._guard_with_barriers(50)
        deferred = Scope(
            name="d",
            allowed_intent_verbs=("do",),
            allowed_action_verbs=("did",),
            decay=DecayConfig(
                observation_half_life=hours(1),
                warning_half_life=minutes(30),
                intent_ttl=hours(1),
            ),
            conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
            deferred=True,
        )
        space.register_scope(deferred)
        one = Agent(name="one", scopes={"d": ["intent", "action"]})
        two = Agent(name="two", scopes={"d": ["intent", "action"]})
        assert (
            guard.pre_action(one, "d", "r", "do", 0.5).verdict == GuardVerdict.BLOCKED
        )
        assert (
            guard.pre_action(two, "d", "r", "do", 0.9).verdict == GuardVerdict.BLOCKED
        )
        guard.get_or_create_barrier(two.id).narrow("d", "intent")
        reads = self._count_snapshots(monkeypatch)

        results = guard.resolve_deferred("d", "r")

        assert results[one.id].verdict == GuardVerdict.ALLOW
        assert results[two.id].verdict == GuardVerdict.DENIED
        assert reads == [two.id]
