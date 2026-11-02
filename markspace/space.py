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
from collections.abc import Iterator
from functools import cmp_to_key
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

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
    intent_beats,
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

# Source level ordering for validation (strict total order).
_SOURCE_ORDER: list[Source] = [
    Source.EXTERNAL_UNVERIFIED,
    Source.EXTERNAL_VERIFIED,
    Source.FLEET,
]


@dataclass(frozen=True)
class IntentJudgement:
    """An intent judged against its competing set (Section 6.1)."""

    wins: bool
    yield_all: bool  # a competing pair is under YIELD_ALL: a principal decides
    winner: Intent | None  # when it loses: the strongest intent that beats it


class ClaimTable(Protocol):
    """
    The guard that holds a space's grants (Section 6.5). The space calls it
    under its own lock; the guard takes its lease-table lock and calls back
    only space methods that re-enter the lock its thread holds.
    """

    def on_supersede(self, mark: AnyMark) -> None:
        """*mark* was just stored and supersedes mark.supersedes."""

    def on_gc(self) -> None:
        """gc removed marks; the table may drop entries whose mark is gone."""

    def leases_and_closed(
        self, scope: str, resource: str
    ) -> tuple[list[uuid.UUID], set[uuid.UUID]]:
        """
        On *resource* in the competing set of *scope*: the intent ids of live
        leases (lowest token first), and the ids of intents that are granted or
        closed, which are no longer pending.
        """
        ...


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
        # created_at of the latest intent or action per (scope, resource): the
        # next one there gets a later time (P32).
        self._last_claim_at: dict[tuple[str, str], float] = {}
        # Per-agent write quota, a limit of this reference implementation
        # (the spec has none). None = unlimited.
        self._max_marks_per_agent: int | None = max_marks_per_agent
        # Per-agent count of current marks (stored and not superseded),
        # maintained on write, resolve and gc.
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
        # Subscription state for watch/subscribe (Section 13)
        self._subscriptions: dict[uuid.UUID, list[WatchPattern]] = {}
        self._pending_notifications: dict[uuid.UUID, list[uuid.UUID]] = {}
        # The guard that holds this space's grants (Section 6.5); at most one.
        self._claims: ClaimTable | None = None
        # Per thread: how deep in locked() it is, and the write hooks to fire
        # when the outermost locked() block ends (Section 8.6).
        self._local = threading.local()
        if scopes:
            for scope in scopes:
                self._scopes[scope.name] = scope

    def register_scope(self, scope: Scope) -> None:
        """Register a scope definition."""
        with self._lock:
            self._scopes[scope.name] = scope

    def attach_claim_table(self, table: ClaimTable) -> None:
        """
        Attach the guard that holds this space's grants (Section 6.5). A
        space has at most one: a second guard would grant the same resource
        again.
        """
        with self._lock:
            if self._claims is not None:
                raise ValueError("This MarkSpace already has a guard")
            self._claims = table

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

        # Source level enforcement: reject observations where the claimed
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

    def _decrement_mark_count(self, agent_id: uuid.UUID) -> None:
        """One current mark fewer for *agent_id*. Caller MUST hold self._lock."""
        count = self._agent_mark_counts.get(agent_id, 0)
        if count > 1:
            self._agent_mark_counts[agent_id] = count - 1
        else:
            self._agent_mark_counts.pop(agent_id, None)

    def _created_at_for(self, mark: AnyMark) -> float:
        """
        created_at for a mark about to be stored: now(). An intent or action
        gets the next representable time after the latest intent or action on
        the same resource in the same scope when the clock has not passed it,
        so equal claims in one scope resolve in write order (P32). Conflict
        resolution breaks ties only within one scope, and no other scope's
        writes show in this time. Caller MUST hold self._lock.
        """
        t = self.now()
        if not isinstance(mark, (Intent, Action)):
            return t
        key = (mark.scope, mark.resource)
        last = self._last_claim_at.get(key)
        if last is not None and t <= last:
            t = math.nextafter(last, math.inf)
        self._last_claim_at[key] = t
        return t

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

            # A write that supersedes a current mark replaces it: the
            # author's count stays the same, so the quota does not apply.
            replaces_current = (
                mark.supersedes is not None
                and mark.supersedes in self._marks
                and mark.supersedes not in self._superseded
            )

            # Enforce per-agent write quota
            if self._max_marks_per_agent is not None and not replaces_current:
                current = self._agent_mark_counts.get(agent.id, 0)
                if current >= self._max_marks_per_agent:
                    raise QuotaExceededError(
                        f"Agent '{agent.name}' has {current} current marks, "
                        f"its quota of {self._max_marks_per_agent}. "
                        f"gc() removes expired marks."
                    )

            # Only a mark's author may supersede it (Section 10): another
            # agent could otherwise erase an action, or the warning that
            # hides one, from an unrelated scope.
            if mark.supersedes is not None:
                target = self._marks.get(mark.supersedes)
                if target is not None and target.agent_id != agent.id:
                    raise ScopeError(
                        f"Agent '{agent.name}' may not supersede mark "
                        f"{mark.supersedes} of another agent"
                    )
            # A warning that hides an action needs warning rights in the
            # action's own scope (Section 9.5): otherwise an agent could hide
            # a booking from any scope it may warn in, and take the resource.
            if isinstance(mark, Warning) and mark.invalidates is not None:
                target = self._marks.get(mark.invalidates)
                if isinstance(target, Action) and not agent.can_write(
                    target.scope, MarkType.WARNING
                ):
                    raise ScopeError(
                        f"Agent '{agent.name}' may not write warnings in scope "
                        f"'{target.scope}', where action {target.id} lies"
                    )

            # Create an immutable copy with assigned metadata
            new_id = uuid.uuid4()
            stored = mark.model_copy(
                update={
                    "id": new_id,
                    "agent_id": agent.id,
                    "created_at": self._created_at_for(mark),
                }
            )

            # Handle supersession (any mark type can supersede a prior mark)
            # The superseded mark stays in storage but will be filtered at read time
            if stored.supersedes is not None:
                if replaces_current:
                    self._decrement_mark_count(self._marks[stored.supersedes].agent_id)
                self._superseded.add(stored.supersedes)

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
            if stored.supersedes is not None and self._claims is not None:
                self._claims.on_supersede(stored)
            self._write_seq += 1
            stored_mark = stored
            # Snapshot hooks to fire outside lock
            hooks_to_fire = list(self._write_hooks.values())

        # Fire post-write hooks outside the lock (P33: hook failure must not
        # affect stored mark). Marks are immutable, safe to pass references.
        # Inside locked(), the hooks wait until the outermost block ends.
        if stored_mark is not None:
            if getattr(self._local, "depth", 0) > 0:
                self._local.deferred.append((hooks_to_fire, stored_mark))
            else:
                self._fire_hooks(hooks_to_fire, stored_mark)

        return new_id

    @staticmethod
    def _fire_hooks(
        hooks: list[Callable[[uuid.UUID, AnyMark], None]], mark: AnyMark
    ) -> None:
        for hook in hooks:
            try:
                hook(mark.agent_id, mark)
            except Exception:
                logger.debug("Post-write hook failed", exc_info=True)

    def read(
        self,
        scope: str,
        resource: str | None = None,
        topic: str | None = None,
        mark_type: MarkType | None = None,
        min_strength: float = _DEFAULT_MIN_STRENGTH,
        reader: Agent | None = None,
        max_tokens: int | None = None,
        now: float | None = None,
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
        For CLASSIFIED scopes: no mark of the scope unless reader has read
            authorization.
        Each mark is judged and decayed by its own scope's definition, also
        when it is read through a parent scope.

        now: the time to evaluate at, as in get_intents(); default now().

        Returns marks sorted by effective_strength descending.
        """
        with self._lock:
            if now is None:
                now = self.now()
            self.get_scope(scope)  # raises for a scope with no definition

            # Each mark decays and is shown by its own scope's definition
            # (visible_form, P26), so a defined child scope keeps its own
            # settings when read through its parent.

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
                strength = effective_strength_with_warnings(
                    m,
                    self._warnings_on(m),
                    now,
                    self.get_scope(m.scope).decay,
                    warning_decay=self._own_decay,
                )

                if strength >= min_strength:
                    visible = self.visible_form(m, reader)
                    if visible is not None:
                        results.append((strength, visible))

            # Sort by strength descending
            results.sort(key=lambda pair: pair[0], reverse=True)

            marks = [m for _, m in results]

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
            self._decrement_mark_count(mark.agent_id)
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
                        m,
                        relevant_warnings,
                        now,
                        scope_def.decay,
                        warning_decay=self._own_decay,
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

    def _own_decay(self, mark: AnyMark) -> DecayConfig:
        """The decay settings of *mark*'s own scope."""
        return self.get_scope(mark.scope).decay

    @contextmanager
    def locked(self) -> Iterator[None]:
        """
        Hold the space: no write, gc or read by another thread runs until the
        block ends. A guard decision that reads several sets holds it, so that
        every read sees one state (Section 6.2.3). The write hooks of writes
        made inside the block wait as in hooks_deferred(). Lock order: a
        guard's resource lock, then a barrier lock, then this one, then the
        guard's lease-table lock (Section 6.5). Take it only inside a guard's
        resource lock or with no guard lock held. While holding it, the space
        calls only its guard's ClaimTable methods; never call another guard
        method or take a barrier lock while holding it.
        """
        with self.hooks_deferred(), self._lock:
            yield

    @contextmanager
    def hooks_deferred(self) -> Iterator[None]:
        """
        The write hooks of writes this thread makes inside the block fire when
        the outermost such block ends, after every lock taken inside it is
        released (Section 8.6), also when the block raises: a mark stored is
        stored. A guard enters it around its resource lock.
        """
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._local.deferred = []
        self._local.depth = depth + 1
        try:
            yield
        finally:
            self._local.depth = depth
            if depth == 0:
                deferred, self._local.deferred = self._local.deferred, []
                for hooks, mark in deferred:
                    self._fire_hooks(hooks, mark)

    def visible_form(self, mark: AnyMark, reader: Agent | None) -> AnyMark | None:
        """
        The form of *mark* that *reader* may see under the visibility of the
        mark's own scope (Section 7.4): the full mark, its projection, or None.
        reader=None is infrastructure and sees full marks (P20).
        """
        if reader is None or reader.can_read_content(mark.scope):
            return mark
        with self._lock:
            visibility = self.get_scope(mark.scope).visibility
        if visibility == ScopeVisibility.OPEN:
            return mark
        if visibility == ScopeVisibility.PROTECTED:
            return project_mark(mark)
        return None  # CLASSIFIED: not even projected (P22, P25)

    def get_mark(self, mark_id: uuid.UUID) -> AnyMark | None:
        """Retrieve a mark by id. Returns None if not found."""
        with self._lock:
            return self._marks.get(mark_id)

    def _warnings_on(self, mark: AnyMark) -> list[Warning]:
        """Current warnings that invalidate *mark*. Caller MUST hold self._lock."""
        return [
            w
            for wid in self._warnings_by_target.get(mark.id, set())
            if wid not in self._superseded
            and (w := self._marks.get(wid)) is not None
            and isinstance(w, Warning)
        ]

    def _competing_candidate_ids(self, scope: str) -> set[uuid.UUID]:
        """
        Ids of marks in *scope*, its ancestors and its descendants: the scopes
        whose claims on a resource compete with *scope*'s (Section 6.1). Sibling
        scopes do not compete. Caller MUST hold self._lock.
        """
        candidate_ids: set[uuid.UUID] = set()
        for s, ids in self._by_scope.items():
            if scope_contains(scope, s) or scope_contains(s, scope):
                candidate_ids.update(ids)
        return candidate_ids

    def _get_intents_unlocked(
        self, scope: str, resource: str, now: float | None = None
    ) -> list[Intent]:
        """Get active intents on a resource. Caller MUST hold self._lock."""
        if now is None:
            now = self.now()
        self.get_scope(scope)  # raises for a scope with no definition

        results: list[Intent] = []
        for mid in self._competing_candidate_ids(scope):
            m = self._marks.get(mid)
            if (
                m is not None
                and isinstance(m, Intent)
                and m.resource == resource
                and m.id not in self._superseded
                and compute_strength(m, now, self.get_scope(m.scope).decay) > 0
            ):
                results.append(m)
        return results

    def get_intents(
        self, scope: str, resource: str, *, now: float | None = None
    ) -> list[Intent]:
        """Get all active intent marks on a specific resource.

        Includes intents in the scope's ancestors and descendants, whose claims
        on the resource compete with the scope's (Section 6.1). now: the time
        to evaluate at, so that several reads see one snapshot; default now().
        """
        with self._lock:
            return self._get_intents_unlocked(scope, resource, now)

    def get_actions(
        self, scope: str, resource: str, *, now: float | None = None
    ) -> list[Action]:
        """
        Action marks that claim a resource: in the scope, its ancestors or its
        descendants (Section 6.1, P39), not superseded, with effective strength
        (warnings included) at or above read()'s default threshold. now: the
        time to evaluate at, as in get_intents(); default now().
        """
        with self._lock:
            if now is None:
                now = self.now()
            self.get_scope(scope)  # raises for a scope with no definition
            results: list[Action] = []
            for mid in self._competing_candidate_ids(scope):
                m = self._marks.get(mid)
                if (
                    not isinstance(m, Action)
                    or m.resource != resource
                    or m.id in self._superseded
                ):
                    continue
                strength = effective_strength_with_warnings(
                    m,
                    self._warnings_on(m),
                    now,
                    self._own_decay(m),
                    warning_decay=self._own_decay,
                )
                if strength >= _DEFAULT_MIN_STRENGTH:
                    results.append(m)
            return results

    def outranks(self, a: Intent, b: Intent) -> bool | None:
        """
        Whether intent *a* wins against competing intent *b* (Section 6.1):
        None if either scope uses YIELD_ALL (the principal decides); else the
        intent in the outer scope wins; inside one scope, that scope's policy
        decides. Every win points to an outer scope or follows one scope's
        total order, so wins never form a cycle.
        """
        with self._lock:
            pa = self.get_scope(a.scope).conflict_policy
            pb = self.get_scope(b.scope).conflict_policy
        if ConflictPolicy.YIELD_ALL in (pa, pb):
            return None
        if a.scope == b.scope:
            return intent_beats(a, b, pa)
        return scope_contains(a.scope, b.scope)

    def judge_intent(
        self, intent: Intent, competitors: list[Intent]
    ) -> IntentJudgement:
        """
        Judge *intent* against its competing set *competitors* (Section 6.1),
        as judge_all() does.
        """
        others = [c for c in competitors if c.id != intent.id]
        return self.judge_all([intent, *others])[intent.id]

    def judge_all(self, intents: list[Intent]) -> dict[uuid.UUID, IntentJudgement]:
        """
        Judge each intent against the intents in the list that compete with
        it (its own scope, its ancestors, its descendants; Section 6.1). The
        result equals judging every pair with outranks(): an intent wins only
        if it outranks every competitor; a competitor that beats it decides
        before a YIELD_ALL pair does, as no principal answer could let it
        proceed; a beaten intent names the beater in the outermost scope, the
        first in that scope's order. Judged at one time, two competing
        intents never both win, whichever scope's decision computes it.

        One sort per scope, as in multiple-granularity locking (Gray, Lorie,
        Putzolu, Traiger, "Granularity of Locks and Degrees of Consistency in
        a Shared Data Base", 1976): every intent in an ancestor scope beats
        every intent below it unless either scope is YIELD_ALL, so only the
        first intent of each scope's order needs comparing.
        """
        by_scope: dict[str, list[Intent]] = {}
        for i in intents:
            by_scope.setdefault(i.scope, []).append(i)
        with self._lock:
            policy = {s: self.get_scope(s).conflict_policy for s in by_scope}
        yielding = {s for s, p in policy.items() if p == ConflictPolicy.YIELD_ALL}
        ranked: dict[str, list[Intent]] = {}
        for s, group in by_scope.items():
            if s in yielding:
                ranked[s] = group
                continue

            def before(a: Intent, b: Intent, p: ConflictPolicy = policy[s]) -> int:
                return -1 if intent_beats(a, b, p) else 1

            ranked[s] = sorted(group, key=cmp_to_key(before))
        results: dict[uuid.UUID, IntentJudgement] = {}
        for s, group in ranked.items():
            above = sorted(
                (a for a in by_scope if a != s and scope_contains(a, s)),
                key=lambda a: a.count("/"),
            )
            below = [d for d in by_scope if d != s and scope_contains(s, d)]
            if s in yielding:
                # Every pair with this scope goes to the principal.
                has_others = len(group) > 1 or bool(above) or bool(below)
                for i in group:
                    results[i.id] = IntentJudgement(
                        wins=not has_others, yield_all=has_others, winner=None
                    )
                continue
            outer = [a for a in above if a not in yielding]
            pair_yields = any(a in yielding for a in above) or any(
                d in yielding for d in below
            )
            for rank, i in enumerate(group):
                if outer:
                    winner: Intent | None = ranked[outer[0]][0]
                elif rank > 0:
                    winner = group[0]
                else:
                    winner = None
                if winner is not None:
                    results[i.id] = IntentJudgement(
                        wins=False, yield_all=False, winner=winner
                    )
                else:
                    results[i.id] = IntentJudgement(
                        wins=not pair_yields, yield_all=pair_yields, winner=None
                    )
        return results

    def check_conflict(self, scope: str, resource: str) -> uuid.UUID | None:
        """
        Check for intent conflicts on a resource and resolve them.
        Returns the intent of a live lease in the competing set (the lowest
        token; Section 6.5), else the id of the pending intent that wins its
        own competing set, or None (YIELD_ALL, or no intent wins). Sibling
        scopes do not compete, so more than one intent can win; then the
        earliest is returned. With no guard attached, every intent is pending.
        """
        with self._lock:
            now = self.now()
            closed: set[uuid.UUID] = set()
            if self._claims is not None:
                leases, closed = self._claims.leases_and_closed(scope, resource)
                if leases:
                    return leases[0]
            intents = [
                i
                for i in self._get_intents_unlocked(scope, resource, now)
                if i.id not in closed
            ]
            if len(intents) <= 1:
                return intents[0].id if intents else None
            # Each intent's own competing set lies in the scope tree.
            tree: dict[uuid.UUID, Intent] = {}
            for s in {i.scope for i in intents}:
                tree.update(
                    (o.id, o)
                    for o in self._get_intents_unlocked(s, resource, now)
                    if o.id not in closed
                )
            judged = self.judge_all(list(tree.values()))
            winners = [i for i in intents if judged[i.id].wins]
            if not winners:
                return None
            return min(winners, key=lambda i: (i.created_at, str(i.id))).id

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

        Supersession: every superseded mark is collected at once. Removing
        a mark drops only that mark's own entry in the superseded set, after
        the mark count reads it: a superseded mark left the count when it
        was superseded, so gc() does not count it down again.
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
                removed = self._marks.pop(mid, None)
                if removed is not None:
                    # A superseded mark left the count when it was superseded.
                    if mid not in self._superseded:
                        self._decrement_mark_count(removed.agent_id)
                    scope_ids = self._by_scope.get(removed.scope)
                    if scope_ids is not None:
                        scope_ids.discard(mid)
                        if not scope_ids:
                            del self._by_scope[removed.scope]
                    # Clean up warning index entries
                    if isinstance(removed, Warning) and removed.invalidates is not None:
                        target_warnings = self._warnings_by_target.get(
                            removed.invalidates
                        )
                        if target_warnings is not None:
                            target_warnings.discard(mid)
                            if not target_warnings:
                                del self._warnings_by_target[removed.invalidates]
                    # Clean up orphaned target entries - if this mark was a
                    # warning target, remove its key from the index.
                    self._warnings_by_target.pop(mid, None)
                # Clean up supersession references
                self._superseded.discard(mid)

            # Keep write order only where an intent or action still lives.
            if to_remove:
                claimed = {
                    (m.scope, m.resource)
                    for m in self._marks.values()
                    if isinstance(m, (Intent, Action))
                }
                self._last_claim_at = {
                    k: t for k, t in self._last_claim_at.items() if k in claimed
                }
                if self._claims is not None:
                    self._claims.on_gc()

            return len(to_remove)

    # ------------------------------------------------------------------
    # Watch / Subscribe (Section 13)
    # ------------------------------------------------------------------

    def subscribe(self, agent: Agent, patterns: list[WatchPattern]) -> None:
        """
        Register an agent's interest in mark patterns.

        Subsequent writes matching any pattern will queue the mark
        for retrieval via get_watched_marks().

        Spec Section 13.3.
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

        Spec Section 13.4.
        P50: Returns only marks matching subscription patterns.
        P51: At-most-once delivery when clear=True.
        P52: Marks returned in write order.

        Note: This provides at-most-once delivery semantics. When clear=True,
        pending notifications are cleared on retrieval. If the caller crashes
        after retrieval but before processing, those marks will not be
        re-delivered. Production deployments needing at-least-once semantics
        should implement cursor-based delivery on top of the mark space.

        A mark that is superseded by the time of the poll is skipped, as in
        read().
        """
        with self._lock:
            pending_ids = self._pending_notifications.get(agent.id, [])
            marks: list[AnyMark] = []
            for mark_id in pending_ids:
                mark = self._marks.get(mark_id)
                if mark is None or mark_id in self._superseded:
                    continue
                # Same visibility rules as read() (Section 7.4)
                visible = self.visible_form(mark, agent)
                if visible is not None:
                    marks.append(visible)
            if clear:
                self._pending_notifications[agent.id] = []
            return marks

    def _notify_subscribers(self, mark: AnyMark) -> None:
        """
        Check all subscriptions and queue the mark for matching agents.
        Called by write() and resolve() after storing a mark.

        Agents are not notified about their own writes. A mark that matches
        no pattern is not queued, also when it supersedes a mark that does
        (P50); the subscriber sees the superseded mark disappear on its next
        read.
        """
        for agent_id, patterns in self._subscriptions.items():
            if mark.agent_id == agent_id:
                continue  # don't notify the writer about their own marks
            if any(pattern.matches(mark) for pattern in patterns):
                self._pending_notifications.setdefault(agent_id, []).append(mark.id)
