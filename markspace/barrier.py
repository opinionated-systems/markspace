# -*- coding: utf-8 -*-
"""
Absorbing Barrier - Monotonic permission restriction for agents.

Mutable permission restriction overlay on the frozen Agent model.
No changes to core.py. Narrowing is monotonic (set add); only
principal-authenticated restore can re-grant permissions.

Spec Section 9.8.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from markspace.core import scope_contains


@dataclass(frozen=True)
class BarrierSnapshot:
    """Immutable snapshot of barrier state for external inspection.

    Returned by Guard.get_barrier() so callers cannot mutate internal state.
    """

    agent_id: uuid.UUID
    flag_count: int
    flagged_scopes: frozenset[str]
    revoked: frozenset[tuple[str, str]]
    require_need_scopes: frozenset[str]

    def is_allowed_checked(self, scope: str, mark_type_value: str) -> bool:
        """Check with wildcard and hierarchical scope support."""
        if ("*", mark_type_value) in self.revoked:
            return False
        for revoked_scope, revoked_type in self.revoked:
            if revoked_type == mark_type_value and scope_contains(revoked_scope, scope):
                return False
        return True


@dataclass
class AgentBarrier:
    """
    Per-agent permission restriction overlay.

    P44: Barrier Monotonicity - narrow() is irreversible without principal.
    P45: Barrier Principal Exclusivity - restore() requires correct token.
    """

    agent_id: uuid.UUID
    _principal_token: uuid.UUID
    _revoked: set[tuple[str, str]] = field(default_factory=set)
    _require_need_scopes: set[str] = field(default_factory=set)
    _flag_count: int = 0  # escalation counter
    flagged_scopes: set[str] = field(default_factory=set)  # scopes that triggered flags

    def is_allowed(self, scope: str, mark_type_value: str) -> bool:
        """Check if (scope, mark_type) has NOT been revoked."""
        return (scope, mark_type_value) not in self._revoked

    def narrow(self, scope: str, mark_type_value: str) -> None:
        """Revoke a permission. Monotonic - no undo without principal."""
        self._revoked.add((scope, mark_type_value))

    def narrow_all(self, mark_type_value: str) -> None:
        """Revoke a mark type across all scopes. Uses '*' wildcard."""
        self._revoked.add(("*", mark_type_value))

    def require_need(self, scope: str) -> None:
        """Force Need marks before any action in scope."""
        self._require_need_scopes.add(scope)

    def require_need_all(self) -> None:
        """Force Need marks before any action in any scope."""
        self._require_need_scopes.add("*")

    def needs_required(self, scope: str) -> bool:
        """Check if need requirement is active for this scope.

        Hierarchical: require_need("hr") also requires needs for "hr/payroll".
        """
        if "*" in self._require_need_scopes:
            return True
        for required_scope in self._require_need_scopes:
            if scope_contains(required_scope, scope):
                return True
        return False

    def increment_flags(self) -> int:
        """Increment and return the flag count for escalation."""
        self._flag_count += 1
        return self._flag_count

    @property
    def flag_count(self) -> int:
        return self._flag_count

    def restore(
        self, scope: str, mark_type_value: str, principal_token: uuid.UUID
    ) -> bool:
        """Re-grant a revoked permission. Requires correct principal token."""
        if principal_token != self._principal_token:
            return False
        self._revoked.discard((scope, mark_type_value))
        return True

    def restore_all(self, principal_token: uuid.UUID) -> bool:
        """Re-grant all permissions. Principal-only."""
        if principal_token != self._principal_token:
            return False
        self._revoked.clear()
        self._require_need_scopes.clear()
        self._flag_count = 0
        return True

    def is_allowed_checked(self, scope: str, mark_type_value: str) -> bool:
        """Check with wildcard and hierarchical scope support. Used by guard.

        A revocation for scope "hr" also blocks "hr/payroll", consistent
        with how Agent.can_write() handles scope hierarchy.
        """
        if ("*", mark_type_value) in self._revoked:
            return False
        for revoked_scope, revoked_type in self._revoked:
            if revoked_type == mark_type_value and scope_contains(revoked_scope, scope):
                return False
        return True

    def snapshot(self) -> BarrierSnapshot:
        """Return a frozen snapshot for external inspection."""
        return BarrierSnapshot(
            agent_id=self.agent_id,
            flag_count=self._flag_count,
            flagged_scopes=frozenset(self.flagged_scopes),
            revoked=frozenset(self._revoked),
            require_need_scopes=frozenset(self._require_need_scopes),
        )
