# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Mark Space

The mark space is the shared environment where marks are stored.
Agents interact only through the mark space, never directly.

This is the only stateful module. core.py is pure functions.

Spec Section 8.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

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
    Source,
    Warning,
    WatchPattern,
    compute_strength,
    effective_strength,
    effective_strength_with_warnings,
    project_mark,
    reinforce,
    resolve_conflict,
    scope_contains,
)

# Epsilon below which decayed marks are considered effectively dead for GC.
_GC_EPSILON: float = 1e-10

# Logarithmic bonus factor for clustering related need marks.
# When multiple needs share a scope, their aggregate priority gets a
# bonus of log(count) * _CLUSTER_BONUS_FACTOR, capped at 1.0.
_CLUSTER_BONUS_FACTOR: float = 0.1

# Default minimum effective strength for read(). Marks below this threshold
# are filtered out. Low enough to include nearly-expired marks but high enough
# to exclude effectively-dead ones that haven't been GC'd yet.
_DEFAULT_MIN_STRENGTH: float = 0.01

# Trust source ordering for validation (strict total order).
_SOURCE_ORDER: list[Source] = [
    Source.EXTERNAL_UNVERIFIED,
    Source.EXTERNAL_VERIFIED,
    Source.FLEET,
]


class ScopeError(Exception):
    """Raised when an agent attempts an unauthorized write."""


class ValidationError(Exception):
    """Raised when a mark fails validation against its scope."""


class QuotaExceededError(Exception):
    """Raised when an agent exceeds its per-agent write quota."""


@dataclass
class NeedCluster:
    """A group of related need marks for principal presentation."""

    scope: str
    needs: list[Need]
    effective_priority: float
    blocking_count: int
    contexts: list[Any]


class MarkSpace:
    """
    The shared environment. Stores marks, provides read/write/query.

    Spec Section 8.

    This reference implementation uses in-memory storage. A production
    implementation would use a database, Redis, or similar. The properties
    (P27-P29) must hold regardless of storage backend.
    """

    def __init__(
        self,
        scopes: list[Scope] | None = None,
        clock: float | None = None,
        max_marks_per_agent: int | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._marks: dict[uuid.UUID, AnyMark] = {}
        self._scopes: dict[str, Scope] = {}
        self._clock: float | None = clock  # None = use real time
        # Per-agent write quota. None = unlimited.
        self._max_marks_per_agent: int | None = max_marks_per_agent
        # Per-agent live mark count, maintained incrementally on write/gc.
        self._agent_mark_counts: dict[uuid.UUID, int] = {}
        # Scope index: maintained incrementally on write() for O(scope) reads
        self._by_scope: dict[str, set[uuid.UUID]] = {}
        # Superseded marks: maintained incrementally on write()
        self._superseded: set[uuid.UUID] = set()
        # Warning invalidation index: target_mark_id -> set of warning mark ids.
        # Maintained incrementally on write() for O(1) warning lookup per mark.
        self._warnings_by_target: dict[uuid.UUID, set[uuid.UUID]] = {}
        # Post-write hooks: fired outside the lock after each write.
        self._write_hooks: dict[uuid.UUID, Callable[[uuid.UUID, AnyMark], None]] = {}
        self._write_seq: int = 0
        # Subscription state for watch/subscribe (Section 14)
        self._subscriptions: dict[uuid.UUID, list[WatchPattern]] = {}
        self._pending_notifications: dict[uuid.UUID, list[uuid.UUID]] = {}
        if scopes:
            for scope in scopes:
                self._scopes[scope.name] = scope

    def register_scope(self, scope: Scope) -> None:
        """Register a scope definition."""
        with self._lock:
            self._scopes[scope.name] = scope

    def add_write_hook(self, hook: Callable[[uuid.UUID, AnyMark], None]) -> uuid.UUID:
        """Register a post-write hook. Returns a handle for removal."""
        handle = uuid.uuid4()
        with self._lock:
            self._write_hooks[handle] = hook
        return handle

    def remove_write_hook(self, handle: uuid.UUID) -> bool:
        """Remove a hook by handle. Returns True if found and removed."""
        with self._lock:
            return self._write_hooks.pop(handle, None) is not None

    def set_clock(self, t: float) -> None:
        """Override the clock for testing. Set to None to use real time."""
        with self._lock:
            self._clock = t

    def now(self) -> float:
        """Current time. Uses override clock if set, else real time."""
        if self._clock is not None:
            return self._clock
        return time.time()

    def get_scope(self, scope_name: str) -> Scope:
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
        # P19: Scope isolation - unauthorized agents MUST be rejected
        if not agent.can_write(mark.scope, mark.mark_type):
            raise ScopeError(
                f"Agent '{agent.name}' is not authorized to write {mark.mark_type.value} "
                f"to scope '{mark.scope}'"
            )

        # Validate action verbs
        if isinstance(mark, Intent) and scope.allowed_intent_verbs:
            if not scope.allows_intent_verb(mark.action):
                raise ValidationError(
                    f"Intent action '{mark.action}' not allowed in scope '{scope.name}'. "
                    f"Allowed: {scope.allowed_intent_verbs}"
                )

        if isinstance(mark, Action) and scope.allowed_action_verbs:
            if not scope.allows_action_verb(mark.action):
                raise ValidationError(
                    f"Action '{mark.action}' not allowed in scope '{scope.name}'. "
                    f"Allowed: {scope.allowed_action_verbs}"
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

        # Trust source enforcement: reject observations where the claimed
        # source exceeds the agent's max_source. Prevents external agents
        # from writing Source.FLEET observations.
        if isinstance(mark, Observation):
            mark_level = _SOURCE_ORDER.index(mark.source)
            agent_level = _SOURCE_ORDER.index(agent.max_source)
            if mark_level > agent_level:
                raise ScopeError(
                    f"Agent '{agent.name}' (max_source={agent.max_source.value}) "
                    f"cannot write observations with source={mark.source.value}"
                )

    def write(self, agent: Agent, mark: AnyMark) -> uuid.UUID:
        """
        Write a mark to the space.

        Spec Section 8.1.
        P27: mark is immediately visible to subsequent reads.

        Creates an immutable copy of the mark with assigned metadata
        (id, agent_id, created_at). The original mark object is not modified.

        WARNING: Direct calls to write() bypass the Guard's enforcement stack
        (barrier, envelope). Production code should route writes through
        Guard.write_mark() for observations/warnings/needs, or
        Guard.execute()/pre_action()/post_action() for intents/actions.
        Direct write() is for guard-internal writes, probe writes, and tests.

        Returns the mark's id.
        """
        hooks_to_fire: list[Callable[[uuid.UUID, AnyMark], None]] = []
        stored_mark: AnyMark | None = None
        new_id: uuid.UUID

        with self._lock:
            scope = self.get_scope(mark.scope)
            self._validate_mark(agent, mark, scope)

            # Enforce per-agent write quota
            if self._max_marks_per_agent is not None:
                current = self._agent_mark_counts.get(agent.id, 0)
                if current >= self._max_marks_per_agent:
                    raise QuotaExceededError(
                        f"Agent '{agent.name}' has {current} live marks, "
                        f"exceeding quota of {self._max_marks_per_agent}. "
                        f"Wait for GC to reclaim expired marks."
                    )

            # Create an immutable copy with assigned metadata
            new_id = uuid.uuid4()
            stored = mark.model_copy(
                update={
                    "id": new_id,
                    "agent_id": agent.id,
                    "created_at": self.now(),
                }
            )

            # Handle supersession (any mark type can supersede a prior mark)
            # The superseded mark stays in storage but will be filtered at read time
            if stored.supersedes is not None:
                self._superseded.add(stored.supersedes)
                # Decrement quota for the superseded mark's agent so supersession
                # doesn't cause quota self-DoS under tight limits.
                superseded_mark = self._marks.get(stored.supersedes)
                if superseded_mark is not None:
                    sup_count = self._agent_mark_counts.get(superseded_mark.agent_id, 0)
                    if sup_count > 1:
                        self._agent_mark_counts[superseded_mark.agent_id] = (
                            sup_count - 1
                        )
                    else:
                        self._agent_mark_counts.pop(superseded_mark.agent_id, None)

            self._marks[new_id] = stored
            # Maintain per-agent mark count
            self._agent_mark_counts[stored.agent_id] = (
                self._agent_mark_counts.get(stored.agent_id, 0) + 1
            )
            # Maintain scope index
            self._by_scope.setdefault(stored.scope, set()).add(new_id)
            # Maintain warning invalidation index
            if isinstance(stored, Warning) and stored.invalidates is not None:
                self._warnings_by_target.setdefault(stored.invalidates, set()).add(
                    new_id
                )
            self._notify_subscribers(stored)
            self._write_seq += 1
            stored_mark = stored
            # Snapshot hooks to fire outside lock
            hooks_to_fire = list(self._write_hooks.values())

        # Fire post-write hooks outside the lock (P33: hook failure must not
        # affect stored mark). Marks are immutable, safe to pass references.
        if stored_mark is not None:
            for hook in hooks_to_fire:
                try:
                    hook(stored_mark.agent_id, stored_mark)
                except Exception:
                    logger.debug("Post-write hook failed", exc_info=True)

        return new_id

    def read(
        self,
        scope: str,
        resource: str | None = None,
        topic: str | None = None,
        mark_type: MarkType | None = None,
        min_strength: float = _DEFAULT_MIN_STRENGTH,
        reader: Agent | None = None,
        max_tokens: int | None = None,
    ) -> list[AnyMark]:
        """
        Read marks from the space, filtered and with effective strength computed.

        Spec Section 8.2.
        P28: Read purity - no side effects on stored marks.

        reader: The agent performing the read. Controls visibility:
          - None: full access (used by guard, internal infrastructure).
          - Agent with read_scopes: respects scope visibility rules.

        max_tokens: Optional token budget for this read. Marks are returned
            in strength-descending order and truncated when cumulative
            estimated token count exceeds the limit. Token count is
            estimated as len(str(mark.model_dump())) // 4. Marks beyond
            the limit are not lost - they stay in the space for future reads.

        For OPEN scopes (default): full marks regardless of reader.
        For PROTECTED scopes: projected marks (content redacted) unless reader
            has content read authorization.
        For CLASSIFIED scopes: empty list unless reader has read authorization.

        Returns marks sorted by effective_strength descending.
        """
        with self._lock:
            now = self.now()
            scope_def = self.get_scope(scope)
            decay_config = scope_def.decay

            # Visibility check - determine read mode for this scope + reader
            visibility = scope_def.visibility
            if reader is not None and visibility == ScopeVisibility.CLASSIFIED:
                if not reader.can_read_content(scope):
                    return []

            needs_projection = (
                reader is not None
                and visibility == ScopeVisibility.PROTECTED
                and not reader.can_read_content(scope)
            )

            # Use scope index for O(scope) reads instead of O(total) full scan
            # Collect candidates from the scope index. This is O(registered_scopes)
            # for hierarchical reads due to the linear scan over _by_scope keys.
            # A trie-based index would give O(depth) lookups but adds complexity
            # not warranted for a reference implementation.
            candidate_ids: set[uuid.UUID] = set()
            for s, ids in self._by_scope.items():
                if scope_contains(scope, s):
                    candidate_ids.update(ids)

            results: list[tuple[float, AnyMark]] = []
            for mid in candidate_ids:
                m = self._marks.get(mid)
                if m is None:
                    continue

                # Supersession filter (using incremental index)
                if m.id in self._superseded:
                    continue

                # Type filter
                if mark_type is not None and m.mark_type != mark_type:
                    continue

                # Resource filter - use isinstance for type-safe field access
                if resource is not None:
                    if not isinstance(m, (Intent, Action)) or m.resource != resource:
                        continue

                # Topic filter - use isinstance for type-safe field access
                if topic is not None:
                    if not isinstance(m, (Observation, Warning)) or m.topic != topic:
                        continue

                # Compute effective strength with warning invalidation.
                # Uses the warning index for O(1) lookup per mark instead of
                # scanning all warnings.
                warning_ids = self._warnings_by_target.get(m.id, set())
                relevant_warnings = [
                    w
                    for wid in warning_ids
                    if wid not in self._superseded
                    and (w := self._marks.get(wid)) is not None
                    and isinstance(w, Warning)
                ]
                strength = effective_strength_with_warnings(
                    m, relevant_warnings, now, decay_config
                )

                if strength >= min_strength:
                    results.append((strength, m))

            # Sort by strength descending
            results.sort(key=lambda pair: pair[0], reverse=True)

            marks = [m for _, m in results]

            # Apply projection if needed (PROTECTED scope, unauthorized reader)
            if needs_projection:
                marks = [project_mark(m) for m in marks]

            # Token budget truncation: keep the strongest marks that fit.
            # Estimate: 4 characters per token (standard rough estimate
            # across tokenizers, not a tuned constant).
            if max_tokens is not None:
                truncated: list[AnyMark] = []
                tokens_used = 0
                for m in marks:
                    est = len(str(m.model_dump())) // 4
                    if tokens_used + est > max_tokens:
                        break
                    truncated.append(m)
                    tokens_used += est
                marks = truncated

            return marks

    def resolve(
        self,
        need_mark_id: uuid.UUID,
        resolving_action_id: uuid.UUID,
        agent: Agent | None = None,
    ) -> uuid.UUID:
        """
        Resolve a need mark by creating a resolved copy that supersedes it.

        Spec Section 8.3.
        P29: effective strength immediately becomes 0.

        Uses supersession for consistency with the rest of the protocol:
        the original need mark is marked as superseded, and a new resolved
        copy is stored. Returns the new mark's ID.
        """
        with self._lock:
            mark = self._marks.get(need_mark_id)
            if mark is None or not isinstance(mark, Need):
                raise ValidationError(f"Mark {need_mark_id} is not a need mark")
            if need_mark_id in self._superseded:
                raise ValidationError(
                    f"Need {need_mark_id} has already been resolved (superseded)"
                )
            # Validate that the resolving action exists and is an Action mark
            resolving = self._marks.get(resolving_action_id)
            if resolving is None:
                raise ValidationError(
                    f"Resolving action {resolving_action_id} not found in mark space"
                )
            # Validate agent authorization for the need's scope
            if agent is not None and not agent.can_write(mark.scope, MarkType.ACTION):
                raise ScopeError(
                    f"Agent '{agent.name}' is not authorized to resolve "
                    f"needs in scope '{mark.scope}'"
                )
            if not isinstance(resolving, Action):
                raise ValidationError(
                    f"Resolving mark {resolving_action_id} is not an Action "
                    f"(got {resolving.mark_type.value})"
                )
            if resolving.failed:
                raise ValidationError(
                    f"Resolving action {resolving_action_id} is a failed action "
                    f"and cannot resolve a need"
                )
            # Create a resolved copy that supersedes the original.
            # Records both the resolving action and the agent that performed it
            # so audit trails can trace who resolved the need.
            new_id = uuid.uuid4()
            resolved = mark.model_copy(
                update={
                    "id": new_id,
                    "resolved_by": resolving_action_id,
                    "resolved_by_agent": resolving.agent_id,
                    "supersedes": need_mark_id,
                }
            )
            self._marks[new_id] = resolved
            self._superseded.add(need_mark_id)
            # Maintain scope index
            self._by_scope.setdefault(resolved.scope, set()).add(new_id)
            # Maintain per-agent mark count
            self._agent_mark_counts[resolved.agent_id] = (
                self._agent_mark_counts.get(resolved.agent_id, 0) + 1
            )
            # Notify subscribers about the resolved need (supersession)
            self._notify_subscribers(resolved)
            return new_id

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
                if (
                    isinstance(m, Need)
                    and m.resolved_by is None
                    and m.id not in self._superseded
                ):
                    scope_def = self.get_scope(m.scope)
                    # Use warning index for efficient lookup
                    warning_ids = self._warnings_by_target.get(m.id, set())
                    relevant_warnings = [
                        w
                        for wid in warning_ids
                        if wid not in self._superseded
                        and (w := self._marks.get(wid)) is not None
                        and isinstance(w, Warning)
                    ]
                    strength = effective_strength_with_warnings(
                        m, relevant_warnings, now, scope_def.decay
                    )
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
                cluster_bonus = (
                    math.log(len(needs)) * _CLUSTER_BONUS_FACTOR
                    if len(needs) > 1
                    else 0.0
                )
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

    def _get_intents_unlocked(self, scope: str, resource: str) -> list[Intent]:
        """Get active intents on a resource. Caller MUST hold self._lock."""
        now = self.now()
        scope_def = self.get_scope(scope)

        candidate_ids: set[uuid.UUID] = set()
        for s, ids in self._by_scope.items():
            if scope_contains(scope, s):
                candidate_ids.update(ids)

        results: list[Intent] = []
        for mid in candidate_ids:
            m = self._marks.get(mid)
            if (
                m is not None
                and isinstance(m, Intent)
                and m.resource == resource
                and m.id not in self._superseded
                and compute_strength(m, now, scope_def.decay) > 0
            ):
                results.append(m)
        return results

    def get_intents(self, scope: str, resource: str) -> list[Intent]:
        """Get all active intent marks on a specific resource.

        Uses hierarchical scope matching (consistent with read()) so that
        intents in child scopes are included when querying a parent scope.
        """
        with self._lock:
            return self._get_intents_unlocked(scope, resource)

    def check_conflict(self, scope: str, resource: str) -> uuid.UUID | None:
        """
        Check for intent conflicts on a resource and resolve them.
        Returns the winning intent's id, or None (YIELD_ALL / no conflict).
        """
        with self._lock:
            scope_def = self.get_scope(scope)
            intents = self._get_intents_unlocked(scope, resource)
            if len(intents) <= 1:
                return intents[0].id if intents else None
            return resolve_conflict(intents, scope_def.conflict_policy)

    # ------------------------------------------------------------------
    # Garbage Collection
    # ------------------------------------------------------------------

    def gc(self, grace_period: float = 0.0) -> int:
        """
        Remove marks with zero effective strength, freeing memory.

        grace_period: additional seconds after a mark reaches zero strength
            before it becomes eligible for collection. Applied consistently
            to all mark types:
            - Intents: collected when age >= TTL + grace_period.
            - Observations/warnings: collected when decayed below epsilon
              AND age >= time_to_epsilon + grace_period.
            - Needs: collected immediately when resolved (grace_period
              does not apply - resolution is an explicit action).
            - Superseded marks: collected immediately (already replaced).

        Returns the number of marks removed.

        Note: GC uses effective_strength() (without warning invalidation),
        not effective_strength_with_warnings(). This is intentional: a mark
        invalidated by warnings still exists and may recover as warnings
        decay (P35). Only marks that are intrinsically dead (expired TTL,
        resolved needs, superseded) are collected.

        Handles supersession chains: if A supersedes B, and A is collected,
        B's supersession entry is cleaned up (but B itself may also be
        collected if it has zero strength).
        """
        if grace_period < 0:
            raise ValueError(f"grace_period must be non-negative, got {grace_period}")
        with self._lock:
            now = self.now()
            to_remove: set[uuid.UUID] = set()

            for mid, m in self._marks.items():
                scope_def = self.get_scope(m.scope)

                # Superseded marks always have effective strength 0
                if mid in self._superseded:
                    to_remove.add(mid)
                    continue

                strength = effective_strength(m, now, scope_def.decay)

                # Intent: step function decay - strength drops to exactly 0
                # when age exceeds TTL. Grace period extends the collection
                # window beyond TTL expiry.
                if isinstance(m, Intent):
                    if strength <= 0.0:
                        age = now - m.created_at
                        if age >= scope_def.decay.intent_ttl + grace_period:
                            to_remove.add(mid)
                    continue

                # Action: permanent (P2). Never collected by GC.
                if isinstance(m, Action):
                    continue

                # Need: full strength until resolved, then 0.
                # Resolved needs are collected immediately (resolution is
                # an explicit action, no grace period).
                if isinstance(m, Need):
                    if strength <= 0.0 and m.resolved_by is not None:
                        to_remove.add(mid)
                    continue

                # Observation/Warning: exponential decay - strength
                # asymptotically approaches 0 but never reaches it.
                # Collect when strength drops below epsilon.
                if strength < _GC_EPSILON:
                    if grace_period <= 0.0:
                        to_remove.add(mid)
                    else:
                        # Compute time-to-epsilon: how long after created_at
                        # the mark's strength first dropped below epsilon.
                        # For exponential decay: t = h * log2(s0 / eps).
                        half_life = (
                            scope_def.decay.observation_half_life
                            if isinstance(m, Observation)
                            else scope_def.decay.warning_half_life
                        )
                        if m.initial_strength > _GC_EPSILON:
                            time_to_epsilon = half_life * math.log2(
                                m.initial_strength / _GC_EPSILON
                            )
                        else:
                            time_to_epsilon = 0.0
                        age = now - m.created_at
                        if age >= time_to_epsilon + grace_period:
                            to_remove.add(mid)

            # Remove marks and clean up indices
            for mid in to_remove:
                m = self._marks.pop(mid, None)
                if m is not None:
                    # Decrement per-agent mark count
                    count = self._agent_mark_counts.get(m.agent_id, 0)
                    if count > 1:
                        self._agent_mark_counts[m.agent_id] = count - 1
                    else:
                        self._agent_mark_counts.pop(m.agent_id, None)
                    scope_ids = self._by_scope.get(m.scope)
                    if scope_ids is not None:
                        scope_ids.discard(mid)
                        if not scope_ids:
                            del self._by_scope[m.scope]
                    # Clean up warning index entries
                    if isinstance(m, Warning) and m.invalidates is not None:
                        target_warnings = self._warnings_by_target.get(m.invalidates)
                        if target_warnings is not None:
                            target_warnings.discard(mid)
                            if not target_warnings:
                                del self._warnings_by_target[m.invalidates]
                    # Clean up orphaned target entries - if this mark was a
                    # warning target, remove its key from the index.
                    self._warnings_by_target.pop(mid, None)
                # Clean up supersession references
                self._superseded.discard(mid)

            return len(to_remove)

    # ------------------------------------------------------------------
    # Watch / Subscribe (Section 14)
    # ------------------------------------------------------------------

    def subscribe(self, agent: Agent, patterns: list[WatchPattern]) -> None:
        """
        Register an agent's interest in mark patterns.

        Subsequent writes matching any pattern will queue the mark
        for retrieval via get_watched_marks().

        Spec Section 14.3.
        P48: Idempotent - re-subscribing replaces patterns.
        P49: Prospective - does not retroactively deliver existing marks.
        """
        with self._lock:
            self._subscriptions[agent.id] = list(patterns)
            if agent.id not in self._pending_notifications:
                self._pending_notifications[agent.id] = []

    def unsubscribe(self, agent: Agent) -> None:
        """Remove all subscriptions for an agent."""
        with self._lock:
            self._subscriptions.pop(agent.id, None)
            self._pending_notifications.pop(agent.id, None)

    def get_watched_marks(self, agent: Agent, clear: bool = True) -> list[AnyMark]:
        """
        Retrieve marks matching an agent's subscriptions since the last poll.

        Spec Section 14.4.
        P50: Returns only marks matching subscription patterns.
        P51: At-most-once delivery when clear=True.
        P52: Marks returned in write order.

        Note: This provides at-most-once delivery semantics. When clear=True,
        pending notifications are cleared on retrieval. If the caller crashes
        after retrieval but before processing, those marks will not be
        re-delivered. Production deployments needing at-least-once semantics
        should implement cursor-based delivery on top of the mark space.
        """
        with self._lock:
            pending_ids = self._pending_notifications.get(agent.id, [])
            marks: list[AnyMark] = []
            for mark_id in pending_ids:
                mark = self._marks.get(mark_id)
                if mark is not None:
                    marks.append(mark)
            if clear:
                self._pending_notifications[agent.id] = []
            return marks

    def _notify_subscribers(self, mark: AnyMark) -> None:
        """
        Check all subscriptions and queue the mark for matching agents.
        Called internally by write() after storing a mark.

        Agents are not notified about their own writes.

        When a mark supersedes another, subscribers watching the superseded
        mark's scope are also notified about the new mark so they can
        detect the supersession (the new mark's .supersedes field points
        to the now-dead mark).
        """
        # Collect the superseded mark (if any) to expand notification scope.
        superseded_mark: AnyMark | None = None
        if mark.supersedes is not None:
            superseded_mark = self._marks.get(mark.supersedes)

        for agent_id, patterns in self._subscriptions.items():
            if mark.agent_id == agent_id:
                continue  # don't notify the writer about their own marks
            matched = False
            for pattern in patterns:
                if pattern.matches(mark):
                    matched = True
                    break
            # If no pattern matched the new mark directly, check whether
            # any pattern matches the superseded mark. This ensures
            # subscribers watching the old mark's scope learn about the
            # supersession regardless of pattern ordering.
            if not matched and superseded_mark is not None:
                for pattern in patterns:
                    if pattern.matches(superseded_mark):
                        matched = True
                        break
            if matched:
                self._pending_notifications.setdefault(agent_id, []).append(mark.id)
