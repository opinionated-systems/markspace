# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Barrier Tests

Tests that AgentBarrier correctly enforces monotonic permission restriction,
principal-authenticated restore, wildcard revocation, and agent isolation.
"""

from __future__ import annotations

import uuid

import pytest

from markspace.barrier import AgentBarrier


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
        assert barrier.flag_count == 0
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
        barrier.increment_flags()
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
        """restore_all() clears all revocations, need requirements, and flags."""
        barrier, _, token = _make_barrier()

        barrier.narrow("calendar", "intent")
        barrier.narrow("email", "observation")
        barrier.require_need("calendar")
        barrier.increment_flags()
        barrier.increment_flags()

        assert barrier.is_allowed_checked("calendar", "intent") is False
        assert barrier.needs_required("calendar") is True
        assert barrier.flag_count == 2

        result = barrier.restore_all(token)
        assert result is True
        assert barrier.is_allowed_checked("calendar", "intent") is True
        assert barrier.is_allowed_checked("email", "observation") is True
        assert barrier.needs_required("calendar") is False
        assert barrier.flag_count == 0

    def test_restore_all_wrong_token(self) -> None:
        """restore_all() with wrong token fails and changes nothing."""
        barrier, _, _ = _make_barrier()
        wrong_token = uuid.uuid4()

        barrier.narrow("calendar", "intent")
        barrier.increment_flags()

        result = barrier.restore_all(wrong_token)
        assert result is False
        assert barrier.is_allowed_checked("calendar", "intent") is False
        assert barrier.flag_count == 1

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

    def test_escalation_counter(self) -> None:
        """increment_flags() increments and returns the running count."""
        barrier, _, _ = _make_barrier()

        assert barrier.flag_count == 0
        assert barrier.increment_flags() == 1
        assert barrier.increment_flags() == 2
        assert barrier.increment_flags() == 3
        assert barrier.flag_count == 3

    def test_isolation_between_agents(self) -> None:
        """Different barriers (different agents) do not interfere."""
        barrier_a, _, token_a = _make_barrier()
        barrier_b, _, token_b = _make_barrier()

        barrier_a.narrow("calendar", "intent")
        barrier_b.narrow("email", "observation")
        barrier_a.increment_flags()

        # A's revocation does not affect B
        assert barrier_a.is_allowed_checked("calendar", "intent") is False
        assert barrier_b.is_allowed_checked("calendar", "intent") is True

        # B's revocation does not affect A
        assert barrier_b.is_allowed_checked("email", "observation") is False
        assert barrier_a.is_allowed_checked("email", "observation") is True

        # Flag counts are independent
        assert barrier_a.flag_count == 1
        assert barrier_b.flag_count == 0

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
