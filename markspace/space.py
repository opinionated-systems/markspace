# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Mark Space

The mark space is the shared environment where marks are stored.
Agents interact only through the mark space, never directly.

This is the only stateful module. core.py is pure functions.

Spec Section 8.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, field

from markspace.core import (
    Action,
    Agent,
    AnyMark,
    ConflictPolicy,
    DecayConfig,
    Intent,
    Mark,
    MarkType,
    Need,
    Observation,
    Scope,
    ScopeVisibility,
    Warning,
    compute_strength,
    effective_strength,
    effective_strength_with_warnings,
    project_mark,
    reinforce,
    resolve_conflict,
)


class ScopeError(Exception):
    """Raised when an agent attempts an unauthorized write."""

    pass


class ValidationError(Exception):
    """Raised when a mark fails validation against its scope."""

    pass


@dataclass
class NeedCluster:
    """A group of related need marks for principal presentation."""

    scope: str
    needs: list[Need]
    effective_priority: float
    blocking_count: int
    contexts: list[object]


class MarkSpace:
    """
    The shared environment. Stores marks, provides read/write/query.

    Spec Section 8.

    This reference implementation uses in-memory storage. A production
    implementation would use a database, Redis, or similar. The properties
    (P17-P19) must hold regardless of storage backend.
    """

    def __init__(
        self, scopes: list[Scope] | None = None, clock: float | None = None
    ) -> None:
        self._lock = threading.RLock()
        self._marks: dict[uuid.UUID, AnyMark] = {}
        self._scopes: dict[str, Scope] = {}
        self._clock: float | None = clock  # None = use real time
        if scopes:
            for scope in scopes:
                self._scopes[scope.name] = scope

    def register_scope(self, scope: Scope) -> None:
        """Register a scope definition."""
        self._scopes[scope.name] = scope

    def set_clock(self, t: float) -> None:
        """Override the clock for testing. Set to None to use real time."""
        self._clock = t

    def now(self) -> float:
        """Current time. Uses override clock if set, else real time."""
        if self._clock is not None:
            return self._clock
        return time.time()

    def _get_scope(self, scope_name: str) -> Scope:
        """Find the scope definition for a given scope name, respecting hierarchy."""
        if scope_name in self._scopes:
            return self._scopes[scope_name]
        # Walk up the hierarchy
        parts = scope_name.split("/")
        for i in range(len(parts) - 1, 0, -1):
            parent = "/".join(parts[:i])
            if parent in self._scopes:
                return self._scopes[parent]
        raise ValidationError(f"No scope definition found for '{scope_name}'")

    def _validate_mark(self, agent: Agent, mark: AnyMark, scope: Scope) -> None:
        """Validate a mark against its scope and the agent's permissions."""
        # P14: Scope isolation — unauthorized agents MUST be rejected
        if not agent.can_write(mark.scope, mark.mark_type):
            raise ScopeError(
                f"Agent '{agent.name}' is not authorized to write {mark.mark_type.value} "
                f"to scope '{mark.scope}'"
            )

        # Validate action verbs
        if isinstance(mark, Intent) and scope.intent_actions:
            if not scope.allows_intent_action(mark.action):
                raise ValidationError(
                    f"Intent action '{mark.action}' not allowed in scope '{scope.name}'. "
                    f"Allowed: {scope.intent_actions}"
                )

        if isinstance(mark, Action) and scope.action_actions:
            if not scope.allows_action_action(mark.action):
                raise ValidationError(
                    f"Action '{mark.action}' not allowed in scope '{scope.name}'. "
                    f"Allowed: {scope.action_actions}"
                )

        if isinstance(mark, Observation):
            if not scope.allows_observation_topic(mark.topic):
                raise ValidationError(
                    f"Observation topic '{mark.topic}' not allowed in scope '{scope.name}'."
                )

        if isinstance(mark, Warning):
            if not scope.allows_warning_topic(mark.topic):
                raise ValidationError(
                    f"Warning topic '{mark.topic}' not allowed in scope '{scope.name}'."
                )

    def write(self, agent: Agent, mark: AnyMark) -> uuid.UUID:
        """
        Write a mark to the space.

        Spec Section 8.1.
        P17: mark is immediately visible to subsequent reads.

        Returns the mark's id.
        """
        with self._lock:
            scope = self._get_scope(mark.scope)
            self._validate_mark(agent, mark, scope)

            # Assign metadata
            mark.id = uuid.uuid4()
            mark.agent_id = agent.id
            mark.created_at = self.now()

            # Handle supersession (any mark type can supersede a prior mark)
            # The superseded mark stays in storage but will be filtered at read time

            self._marks[mark.id] = mark
            return mark.id

    def read(
        self,
        scope: str,
        resource: str | None = None,
        topic: str | None = None,
        mark_type: MarkType | None = None,
        min_strength: float = 0.01,
        reader: Agent | None = None,
    ) -> list[AnyMark]:
        """
        Read marks from the space, filtered and with effective strength computed.

        Spec Section 8.2.
        P18: Read purity — no side effects on stored marks.

        reader: The agent performing the read. Controls visibility:
          - None: full access (used by guard, internal infrastructure).
          - Agent with read_scopes: respects scope visibility rules.

        For OPEN scopes (default): full marks regardless of reader.
        For PROTECTED scopes: projected marks (content redacted) unless reader
            has content read authorization.
        For CLASSIFIED scopes: empty list unless reader has read authorization.

        Returns marks sorted by effective_strength descending.
        """
        with self._lock:
            now = self.now()
            scope_def = self._get_scope(scope)
            decay_config = scope_def.decay

            # Visibility check — determine read mode for this scope + reader
            visibility = scope_def.visibility
            if reader is not None and visibility == ScopeVisibility.CLASSIFIED:
                if not reader.can_read_content(scope):
                    return []

            needs_projection = (
                reader is not None
                and visibility == ScopeVisibility.PROTECTED
                and not reader.can_read_content(scope)
            )

            # Collect superseded mark ids (any mark type can supersede)
            superseded: set[uuid.UUID] = set()
            for m in self._marks.values():
                if m.supersedes is not None:
                    superseded.add(m.supersedes)

            # Collect warning marks for invalidation
            warnings = [m for m in self._marks.values() if isinstance(m, Warning)]

            results: list[tuple[float, AnyMark]] = []
            for m in self._marks.values():
                # Scope filter
                if m.scope != scope and not m.scope.startswith(scope + "/"):
                    continue

                # Supersession filter
                if m.id in superseded:
                    continue

                # Type filter
                if mark_type is not None and m.mark_type != mark_type:
                    continue

                # Resource filter — marks without a resource field are excluded
                # when a resource filter is specified
                if resource is not None:
                    if not hasattr(m, "resource") or m.resource != resource:
                        continue

                # Topic filter — marks without a topic field are excluded
                # when a topic filter is specified
                if topic is not None:
                    if not hasattr(m, "topic") or m.topic != topic:
                        continue

                # Compute effective strength with warning invalidation
                strength = effective_strength_with_warnings(
                    m, warnings, now, decay_config
                )

                if strength >= min_strength:
                    results.append((strength, m))

            # Sort by strength descending
            results.sort(key=lambda pair: pair[0], reverse=True)

            marks = [m for _, m in results]

            # Apply projection if needed (PROTECTED scope, unauthorized reader)
            if needs_projection:
                marks = [project_mark(m) for m in marks]

            return marks

    def resolve(self, need_mark_id: uuid.UUID, decision_mark_id: uuid.UUID) -> None:
        """
        Resolve a need mark by linking it to a decision mark.

        Spec Section 8.3.
        P19: effective strength immediately becomes 0.
        """
        with self._lock:
            mark = self._marks.get(need_mark_id)
            if mark is None or not isinstance(mark, Need):
                raise ValidationError(f"Mark {need_mark_id} is not a need mark")
            mark.resolved_by = decision_mark_id

    def aggregate_needs(self) -> list[NeedCluster]:
        """
        Group and prioritize unresolved need marks for principal presentation.

        Spec Section 8.4.
        The aggregator is deliberately simple: group, score, sort.
        """
        with self._lock:
            now = self.now()
            unresolved: list[Need] = []
            for m in self._marks.values():
                if isinstance(m, Need) and m.resolved_by is None:
                    scope_def = self._get_scope(m.scope)
                    strength = compute_strength(m, now, scope_def.decay)
                    if strength > 0:
                        unresolved.append(m)

            if not unresolved:
                return []

            # Group by scope
            by_scope: dict[str, list[Need]] = {}
            for need in unresolved:
                by_scope.setdefault(need.scope, []).append(need)

            clusters: list[NeedCluster] = []
            for scope_name, needs in by_scope.items():
                max_priority = max(n.priority for n in needs)
                cluster_bonus = math.log(len(needs)) * 0.1 if len(needs) > 1 else 0.0
                clusters.append(
                    NeedCluster(
                        scope=scope_name,
                        needs=needs,
                        effective_priority=min(1.0, max_priority + cluster_bonus),
                        blocking_count=sum(1 for n in needs if n.blocking),
                        contexts=[n.context for n in needs],
                    )
                )

            clusters.sort(key=lambda c: c.effective_priority, reverse=True)
            return clusters

    def get_mark(self, mark_id: uuid.UUID) -> AnyMark | None:
        """Retrieve a mark by id. Returns None if not found."""
        with self._lock:
            return self._marks.get(mark_id)

    def get_intents(self, scope: str, resource: str) -> list[Intent]:
        """Get all active intent marks on a specific resource."""
        with self._lock:
            now = self.now()
            scope_def = self._get_scope(scope)

            # Collect superseded mark ids
            superseded: set[uuid.UUID] = set()
            for m in self._marks.values():
                if m.supersedes is not None:
                    superseded.add(m.supersedes)

            results: list[Intent] = []
            for m in self._marks.values():
                if (
                    isinstance(m, Intent)
                    and m.scope == scope
                    and m.resource == resource
                    and m.id not in superseded
                    and compute_strength(m, now, scope_def.decay) > 0
                ):
                    results.append(m)
            return results

    def check_conflict(self, scope: str, resource: str) -> uuid.UUID | None:
        """
        Check for intent conflicts on a resource and resolve them.
        Returns the winning intent's id, or None (YIELD_ALL / no conflict).
        """
        with self._lock:
            scope_def = self._get_scope(scope)
            intents = self.get_intents(scope, resource)
            if len(intents) <= 1:
                return intents[0].id if intents else None
            return resolve_conflict(intents, scope_def.conflict_policy)
