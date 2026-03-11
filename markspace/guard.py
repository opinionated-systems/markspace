# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Guard

The guard is a DETERMINISTIC enforcement layer that wraps tool execution.
It reads marks and enforces coordination constraints mechanically.
The agent does not need to "remember" to check marks - the guard does it.

This is the critical architectural insight: marks are WRITTEN by agents
(voluntary, through LLM reasoning) but ENFORCED by the guard (deterministic,
wrapping every tool call). Coordination reliability does not depend on the
LLM being reliable.

    BEFORE (unreliable):
        Agent reasons -> agent reads marks -> agent decides -> agent calls tool
                         (may forget)         (may ignore)

    AFTER (deterministic):
        Agent reasons -> agent calls tool -> GUARD checks marks -> tool executes
                                             (deterministic)
        -> GUARD writes action mark

Spec Section 9.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

from markspace.barrier import AgentBarrier
from markspace.core import (
    Action,
    Agent,
    AnyMark,
    ConflictPolicy,
    Intent,
    MarkType,
    Need,
    Observation,
    Severity,
    Warning,
    resolve_conflict,
)
from markspace.envelope import EnvelopeVerdict, StatisticalEnvelope
from markspace.space import MarkSpace, ScopeError


class GuardVerdict(str, Enum):
    """Result of a guard check."""

    ALLOW = "allow"  # No conflict. Proceed.
    CONFLICT = "conflict"  # Another agent has priority. Yield.
    BLOCKED = "blocked"  # YIELD_ALL policy - need principal input.
    DENIED = "denied"  # Agent not authorized for this scope/action.


@dataclass
class GuardDecision:
    """
    The guard's deterministic decision about whether an action can proceed.

    The agent receives this. It doesn't choose whether to follow it -
    the harness enforces it. The decision is informational for the agent's
    reasoning (e.g., "I was blocked because agent X has higher confidence
    on this resource, I'll try a different resource").
    """

    verdict: GuardVerdict
    reason: str
    intent_id: uuid.UUID | None = None  # ID of the intent written by this pre_action
    winning_intent: Intent | None = None
    conflicting_intents: list[Intent] = field(default_factory=list)


@dataclass
class _ResourceLock:
    """A reference-counted lock for a (scope, resource) pair."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    active: int = 0


# Type for the actual tool function the guard wraps
ToolFn = Callable[..., Any]

# Verdict priority for resolve_deferred: when an agent has multiple intents,
# keep the best (lowest index = highest priority).
_VERDICT_PRIORITY: dict[GuardVerdict, int] = {
    GuardVerdict.ALLOW: 0,
    GuardVerdict.BLOCKED: 1,
    GuardVerdict.CONFLICT: 2,
    GuardVerdict.DENIED: 3,
}


def _update_best_decision(
    results: dict[uuid.UUID, GuardDecision],
    agent_id: uuid.UUID,
    decision: GuardDecision,
) -> None:
    """Keep the best verdict per agent (ALLOW > BLOCKED > CONFLICT > DENIED)."""
    existing = results.get(agent_id)
    if (
        existing is None
        or _VERDICT_PRIORITY[decision.verdict] < _VERDICT_PRIORITY[existing.verdict]
    ):
        results[agent_id] = decision


class Guard:
    """
    Deterministic enforcement layer for mark-based coordination.

    Uses per-resource locking: operations on different (scope, resource)
    pairs proceed concurrently, while operations on the same resource are
    serialized. This prevents double bookings without creating a global
    bottleneck.

    Usage:
        guard = Guard(space)

        # Before tool execution: declare intent + check conflicts
        decision = guard.pre_action(agent, "calendar", "thu-14:00", "book", confidence=0.9)
        if decision.verdict == GuardVerdict.ALLOW:
            result = actual_tool_function()
            guard.post_action(agent, "calendar", "thu-14:00", "booked", result)

        # Or use the wrapped executor:
        result = guard.execute(agent, "calendar", "thu-14:00", "book", "booked",
                               confidence=0.9, tool_fn=actual_tool_function)
    """

    def __init__(
        self,
        space: MarkSpace,
        block_self_rebook: bool = False,
        envelope: StatisticalEnvelope | None = None,
        principal_token: uuid.UUID | None = None,
    ) -> None:
        self.space = space
        self.block_self_rebook = block_self_rebook
        self.envelope = envelope
        self._principal_token = principal_token or uuid.uuid4()
        # Per-resource locks: keyed by (scope, resource).
        # A global lock protects the _resource_locks dict itself.
        self._resource_locks: dict[tuple[str, str], _ResourceLock] = {}
        self._meta_lock = threading.Lock()
        # Barriers: per-agent permission restrictions
        self._barriers: dict[uuid.UUID, AgentBarrier] = {}
        self._barrier_lock = threading.Lock()
        # System agent for guard-originated warnings/needs.
        # Exempt from envelope monitoring to prevent feedback loops.
        self._system_agent = Agent(
            name="_guard",
            scopes={"*": ["warning", "need"]},
            read_scopes=frozenset(),
        )
        # Wire envelope to space write hooks
        if envelope is not None:
            envelope.add_exempt_agent(self._system_agent.id)
            space.add_write_hook(lambda agent_id, mark: envelope.record(agent_id, mark))

    @contextmanager
    def _resource_lock(self, scope: str, resource: str) -> Iterator[None]:
        """Acquire a per-resource lock with reference counting.

        Increments active count on entry, decrements on exit. Entries
        with active == 0 are eligible for cleanup via cleanup_locks().
        """
        key = (scope, resource)
        with self._meta_lock:
            if key not in self._resource_locks:
                self._resource_locks[key] = _ResourceLock()
            entry = self._resource_locks[key]
            entry.active += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._meta_lock:
                entry.active -= 1

    def cleanup_locks(self) -> int:
        """Remove resource locks with no active users. Returns count removed.

        Safe because _resource_lock's finally block decrements active AFTER
        releasing entry.lock, so active <= 0 guarantees the lock is free.
        """
        with self._meta_lock:
            to_remove = [
                key for key, entry in self._resource_locks.items() if entry.active <= 0
            ]
            for key in to_remove:
                del self._resource_locks[key]
            return len(to_remove)

    # ------------------------------------------------------------------
    # write_mark: single enforcement boundary for non-contested writes
    # ------------------------------------------------------------------

    def write_mark(self, agent: Agent, mark: AnyMark) -> uuid.UUID:
        """
        Write a non-contested mark (observation, warning, need) through
        the guard's enforcement stack.

        No conflict resolution. No resource locking. But does run:
        - Type validation (rejects intents/actions)
        - Barrier check (is this agent restricted?)
        - Envelope check (is this agent anomalous?)
        - Schema/scope validation (delegated to space.write())

        For intents and actions on contested resources, use execute()
        or pre_action()/post_action() instead.

        P42: Write-Mark Atomicity - rejection prevents storage.
        """
        # Reject intents/actions - those must go through pre_action()/execute()
        if mark.mark_type in (MarkType.INTENT, MarkType.ACTION):
            raise ValueError(
                f"Cannot write {mark.mark_type.value} via write_mark(). "
                f"Use execute() or pre_action()/post_action() for contested writes."
            )

        # Barrier check
        barrier_result = self._check_barrier(agent, mark.scope, mark.mark_type)
        if barrier_result is not None:
            raise ScopeError(barrier_result)

        # Envelope check
        envelope_result = self._check_envelope(agent, mark.scope)
        if envelope_result is not None:
            raise ScopeError(envelope_result)

        return self.space.write(agent, mark)

    # ------------------------------------------------------------------
    # Barrier management
    # ------------------------------------------------------------------

    def get_barrier(self, agent_id: uuid.UUID) -> AgentBarrier | None:
        """Get the barrier for an agent, if any."""
        with self._barrier_lock:
            return self._barriers.get(agent_id)

    def set_barrier(self, agent_id: uuid.UUID, barrier: AgentBarrier) -> None:
        """Set a barrier for an agent (principal action)."""
        with self._barrier_lock:
            self._barriers[agent_id] = barrier

    def _check_barrier(
        self, agent: Agent, scope: str, mark_type: MarkType
    ) -> str | None:
        """Check barrier. Returns error message if blocked, None if allowed."""
        with self._barrier_lock:
            barrier = self._barriers.get(agent.id)
            if barrier is None:
                return None
            if not barrier.is_allowed_checked(scope, mark_type.value):
                return (
                    f"Agent '{agent.name}' blocked by barrier: "
                    f"{mark_type.value} revoked in scope '{scope}'"
                )
            if barrier.needs_required(scope) and mark_type != MarkType.NEED:
                # Check for unresolved Need in this scope
                needs = self.space.read(scope=scope, mark_type=MarkType.NEED)
                has_unresolved = any(
                    isinstance(n, Need)
                    and n.agent_id == agent.id
                    and n.resolved_by is None
                    for n in needs
                )
                if not has_unresolved:
                    return (
                        f"Agent '{agent.name}' must write a Need mark in "
                        f"scope '{scope}' before other writes (barrier requirement)"
                    )
        return None

    def _check_envelope(self, agent: Agent, scope: str) -> str | None:
        """Check envelope. Returns error message if RESTRICTED, None otherwise."""
        if self.envelope is None:
            return None
        verdict = self.envelope.check(agent.id)
        if verdict == EnvelopeVerdict.RESTRICTED:
            self._apply_restriction(agent, scope)
            return (
                f"Agent '{agent.name}' restricted by statistical envelope "
                f"in scope '{scope}'"
            )
        if verdict == EnvelopeVerdict.FLAGGED:
            # Emit warning but don't block
            try:
                self.space.write(
                    self._system_agent,
                    Warning(
                        scope=scope,
                        topic="envelope-flag",
                        reason=f"Concentration detected involving agent '{agent.name}'",
                        severity=Severity.CAUTION,
                    ),
                )
            except Exception:
                logger.debug("Failed to write envelope-flag warning", exc_info=True)
        return None

    def _apply_restriction(self, agent: Agent, scope: str) -> None:
        """Create/update barrier in response to RESTRICTED verdict."""
        with self._barrier_lock:
            barrier = self._barriers.get(agent.id)
            if barrier is None:
                barrier = AgentBarrier(
                    agent_id=agent.id,
                    _principal_token=self._principal_token,
                )
                self._barriers[agent.id] = barrier

            # Scope-scoped narrowing
            barrier.narrow(scope, MarkType.OBSERVATION.value)
            barrier.narrow(scope, MarkType.ACTION.value)
            barrier.require_need(scope)
            flag_count = barrier.increment_flags()
            barrier.flagged_scopes.add(scope)

        # Escalation: global restriction on 3+ flags
        if (
            flag_count >= self.envelope.global_escalation_threshold
            if self.envelope
            else 3
        ):
            with self._barrier_lock:
                barrier.narrow_all(MarkType.OBSERVATION.value)
                barrier.narrow_all(MarkType.ACTION.value)
                barrier.require_need_all()

        # Write invalidation warnings for the agent's recent observations
        try:
            recent_obs = self.space.read(scope=scope, mark_type=MarkType.OBSERVATION)
            agent_obs = [m for m in recent_obs if m.agent_id == agent.id]
            for obs in agent_obs:
                self.space.write(
                    self._system_agent,
                    Warning(
                        scope=scope,
                        invalidates=obs.id,
                        topic="envelope-restriction",
                        reason=f"Agent '{agent.name}' flagged by statistical envelope",
                        severity=Severity.CAUTION,
                    ),
                )
        except Exception:
            logger.debug("Failed to write invalidation warnings", exc_info=True)

        # Write audit Need
        try:
            self.space.write(
                self._system_agent,
                Need(
                    scope=scope,
                    question=f"Agent '{agent.name}' restricted: observation/action revoked in '{scope}'",
                    context={
                        "agent_id": str(agent.id),
                        "trigger": "envelope",
                        "scope": scope,
                        "flag_count": flag_count,
                    },
                    priority=0.9,
                    blocking=False,
                ),
            )
        except Exception:
            logger.debug("Failed to write audit Need", exc_info=True)

    def pre_action(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        action: str,
        confidence: float = 0.5,
    ) -> GuardDecision:
        """
        Called before tool execution. Deterministic check:
        1. Is the agent authorized? (scope + mark type)
        2. Are there conflicting intents on this resource?
        3. If so, who wins?

        For deferred scopes (Spec Section 6.2): writes intent and returns BLOCKED
        (pending resolution). The agent must wait for resolve_deferred().

        Writes an intent mark if allowed. Returns decision.

        The agent does NOT call this - the harness does, automatically,
        before every tool call that modifies a resource.

        WARNING: When using pre_action()/post_action() instead of execute(),
        the caller is responsible for writing a failed Action mark (with
        failed=True, supersedes=intent_id) if the tool throws. Without this,
        the intent remains as a zombie until TTL expiry, blocking other agents.
        Prefer execute() which handles this automatically.

        Thread safety: Per-resource locks serialize the read->write->read->decide
        sequence for each (scope, resource) pair. Different resources proceed
        concurrently. Lock ordering: resource_lock -> space._lock.
        """
        with self._resource_lock(scope, resource):
            return self._pre_action_inner(agent, scope, resource, action, confidence)

    def _pre_action_inner(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        action: str,
        confidence: float,
    ) -> GuardDecision:
        """Core pre_action logic. Caller MUST hold _resource_lock(scope, resource)."""
        # Check authorization for both intent and action writes.
        # Without the action check, an agent with intent-only permission
        # could trigger tool execution but post_action would fail, leaving
        # an orphaned intent with no action mark.
        if not agent.can_write(scope, MarkType.INTENT):
            return GuardDecision(
                verdict=GuardVerdict.DENIED,
                reason=f"Agent '{agent.name}' not authorized for intent in scope '{scope}'",
            )
        if not agent.can_write(scope, MarkType.ACTION):
            return GuardDecision(
                verdict=GuardVerdict.DENIED,
                reason=f"Agent '{agent.name}' not authorized for action in scope '{scope}'",
            )

        # Barrier check
        barrier_msg = self._check_barrier(agent, scope, MarkType.INTENT)
        if barrier_msg is not None:
            return GuardDecision(
                verdict=GuardVerdict.DENIED,
                reason=barrier_msg,
            )

        # Envelope check
        envelope_msg = self._check_envelope(agent, scope)
        if envelope_msg is not None:
            return GuardDecision(
                verdict=GuardVerdict.DENIED,
                reason=envelope_msg,
            )

        # Check for existing action marks on this resource.
        # If the resource is already claimed (action mark exists from another agent),
        # reject immediately. The intent-vs-intent conflict resolution only applies
        # when two agents are both still in the planning phase.
        existing_actions = self.space.read(
            scope=scope,
            resource=resource,
            mark_type=MarkType.ACTION,
        )
        # Filter out failed actions - they record that a tool threw but
        # don't claim the resource. Without this, a failed tool execution
        # permanently blocks the resource for all agents.
        successful_actions = [
            a for a in existing_actions if not (isinstance(a, Action) and a.failed)
        ]
        if self.block_self_rebook:
            blocking_actions = successful_actions
        else:
            blocking_actions = [a for a in successful_actions if a.agent_id != agent.id]
        if blocking_actions:
            # Record the attempt so conflict-spam is visible to the envelope.
            # This return path produces 0 marks (the Intent write is below,
            # at line ~489). Without record_attempt, an adversary can spam
            # already-taken resources indefinitely with no signal reaching
            # the detector. Any guard early-return before a mark write is
            # a potential detection blind spot - audit accordingly.
            if self.envelope is not None:
                self.envelope.record_attempt(agent.id, MarkType.INTENT)
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                reason=f"Resource '{resource}' already claimed by action mark "
                f"(agent_id={blocking_actions[0].agent_id})",
            )

        # Write this agent's intent
        intent_id = self.space.write(
            agent,
            Intent(
                scope=scope,
                resource=resource,
                action=action,
                confidence=confidence,
            ),
        )

        # Deferred resolution (Spec Section 6.2): write intent but don't resolve yet.
        # Agent gets BLOCKED until resolve_deferred() is called.
        scope_def = self.space.get_scope(scope)
        if scope_def.deferred:
            return GuardDecision(
                verdict=GuardVerdict.BLOCKED,
                intent_id=intent_id,
                reason="Deferred resolution - pending resolution boundary",
            )

        # Check for conflicts with other intents (concurrent planning phase)
        all_intents = self.space.get_intents(scope, resource)
        if len(all_intents) <= 1:
            # No conflict - this agent is the only one
            return GuardDecision(
                verdict=GuardVerdict.ALLOW,
                intent_id=intent_id,
                reason="No conflict",
            )

        # Resolve conflict
        winner_id = resolve_conflict(all_intents, scope_def.conflict_policy)

        if winner_id is None:
            # YIELD_ALL - everyone must wait for principal.
            # Only write the Need mark if the agent has permission. The
            # BLOCKED verdict is returned regardless - the Need is
            # informational for the principal, not required for blocking.
            if agent.can_write(scope, MarkType.NEED):
                self.space.write(
                    agent,
                    Need(
                        scope=scope,
                        question=f"Conflict on {resource}: multiple agents want to {action}. Who proceeds?",
                        context={
                            "resource": resource,
                            "action": action,
                            "agent": agent.name,
                        },
                        priority=0.8,
                        blocking=True,
                    ),
                )
            return GuardDecision(
                verdict=GuardVerdict.BLOCKED,
                intent_id=intent_id,
                reason="YIELD_ALL policy - principal decision required",
                conflicting_intents=[i for i in all_intents if i.id != intent_id],
            )

        if winner_id == intent_id:
            return GuardDecision(
                verdict=GuardVerdict.ALLOW,
                intent_id=intent_id,
                reason="This agent wins the conflict",
                conflicting_intents=[i for i in all_intents if i.id != intent_id],
            )
        else:
            winner = next((i for i in all_intents if i.id == winner_id), None)
            if winner is None:
                raise ValueError(
                    f"resolve_conflict returned winner_id={winner_id} "
                    f"not found in intents for {scope}/{resource}"
                )
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                intent_id=intent_id,
                reason=f"Agent '{agent.name}' yields to intent {winner_id} "
                f"(confidence={winner.confidence}, created_at={winner.created_at})",
                winning_intent=winner,
                conflicting_intents=[i for i in all_intents if i.id != intent_id],
            )

    def resolve_deferred(
        self,
        scope: str,
        resource: str,
        agents: dict[uuid.UUID, Agent] | None = None,
    ) -> dict[uuid.UUID, GuardDecision]:
        """
        Deferred resolution boundary. Collects all active intents on
        (scope, resource) and applies the scope's conflict policy to the
        full set. Returns a mapping of agent_id -> GuardDecision.

        If an agent has multiple active intents on the same resource, the
        best verdict is kept (ALLOW > BLOCKED > CONFLICT > DENIED).

        agents: optional mapping of agent_id -> Agent for re-verifying
            authorization at resolution time. If provided, agents whose
            permissions have been revoked since pre_action will receive
            DENIED and be excluded from conflict resolution.

        Spec Section 6.2 (Phase 3: Batch resolution).

        P14: MUST consider ALL active intents at the resolution boundary.
        P15: Winner MUST be identical to simultaneous HIGHEST_CONFIDENCE evaluation.
        P16: Caller is responsible for triggering this method (liveness).

        Note: If all intents on the resource have expired and been GC'd before
        this method is called, it returns an empty dict - agents that wrote
        intents receive no verdict. Callers should ensure the resolution
        boundary fires before intent TTLs expire, or handle the empty-result
        case by re-issuing intents.
        """
        with self._resource_lock(scope, resource):
            scope_def = self.space.get_scope(scope)
            all_intents = self.space.get_intents(scope, resource)

            if not all_intents:
                return {}

            results: dict[uuid.UUID, GuardDecision] = {}

            # Re-verify authorization if agent objects are provided.
            # Filter out intents from agents whose permissions were revoked
            # between pre_action (deferred) and resolution.
            eligible_intents = all_intents
            if agents is not None:
                eligible_intents = []
                for intent in all_intents:
                    agent = agents.get(intent.agent_id)
                    if agent is None:
                        # Unknown agent - deny
                        _update_best_decision(
                            results,
                            intent.agent_id,
                            GuardDecision(
                                verdict=GuardVerdict.DENIED,
                                intent_id=intent.id,
                                reason="Agent not found at resolution time",
                            ),
                        )
                    elif not agent.can_write(
                        scope, MarkType.INTENT
                    ) or not agent.can_write(scope, MarkType.ACTION):
                        _update_best_decision(
                            results,
                            intent.agent_id,
                            GuardDecision(
                                verdict=GuardVerdict.DENIED,
                                intent_id=intent.id,
                                reason=f"Agent '{agent.name}' authorization revoked since pre_action",
                            ),
                        )
                    elif self._check_barrier(agent, scope, MarkType.INTENT) is not None:
                        _update_best_decision(
                            results,
                            intent.agent_id,
                            GuardDecision(
                                verdict=GuardVerdict.DENIED,
                                intent_id=intent.id,
                                reason=f"Agent '{agent.name}' blocked by barrier at resolution time",
                            ),
                        )
                    else:
                        eligible_intents.append(intent)

            if not eligible_intents:
                return results

            # P14: Consider ALL eligible active intents (get_intents already
            # filters by strength > 0 and respects TTL/supersession).
            winner_id = resolve_conflict(eligible_intents, scope_def.conflict_policy)

            # Pre-index for O(1) lookup and per-intent conflict lists.
            intent_by_id = {i.id: i for i in eligible_intents}
            others_by_id = {
                i.id: [o for o in eligible_intents if o.id != i.id]
                for i in eligible_intents
            }
            winner = intent_by_id.get(winner_id) if winner_id is not None else None
            if winner_id is not None and winner is None:
                raise ValueError(
                    f"resolve_conflict returned winner_id={winner_id} "
                    f"not found in eligible intents for {scope}/{resource}"
                )

            for intent in eligible_intents:
                others = others_by_id[intent.id]
                if winner_id is not None and intent.id == winner_id:
                    decision = GuardDecision(
                        verdict=GuardVerdict.ALLOW,
                        intent_id=intent.id,
                        reason="Deferred resolution - this agent wins",
                        conflicting_intents=others,
                    )
                elif winner_id is None:
                    # YIELD_ALL - all blocked
                    decision = GuardDecision(
                        verdict=GuardVerdict.BLOCKED,
                        intent_id=intent.id,
                        reason="YIELD_ALL - principal decision required",
                        conflicting_intents=others,
                    )
                else:
                    decision = GuardDecision(
                        verdict=GuardVerdict.CONFLICT,
                        intent_id=intent.id,
                        reason=f"Deferred resolution - yields to agent "
                        f"'{winner.agent_id}' (confidence={winner.confidence})",
                        winning_intent=winner,
                        conflicting_intents=others,
                    )
                _update_best_decision(results, intent.agent_id, decision)

            return results

    def post_action(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        action: str,
        result: Any,
        intent_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """
        Called after tool execution succeeds. Writes an action mark
        (and supersedes the intent if provided).

        Section 6.3 (Post-Action Resolution): If another agent already wrote
        a successful action on this resource (race condition where both got
        ALLOW before either wrote its action), the later action supersedes
        the earlier one. The intent expires via TTL. The agent whose action
        was superseded SHOULD be notified via a warning mark (not implemented
        here - left to the harness).

        The agent does NOT call this - the harness does, automatically,
        after every successful tool call.

        WARNING: If the tool threw an exception, call this with a failed
        Action mark instead (failed=True) to free the resource. See
        execute() for the reference pattern. Failing to do so leaves
        the intent as a zombie until TTL expiry.

        Limitation (three-way race): If 3+ agents all get ALLOW and finish
        their tools concurrently, each post_action supersedes at most one
        prior action (the first it finds via read()). With agents A, B, C
        finishing in that order: B supersedes A, C supersedes B - correct.
        But if B and C both run post_action before either's write is visible,
        both supersede A, leaving two unsuperseded actions. The mark's
        ``supersedes`` field is a single UUID, so chaining is not possible
        without a second pass. This is unlikely in practice (requires 3+
        agents racing on the same resource with overlapping tool calls) and
        the harness can detect it via multiple visible actions on a resource.
        """
        # Section 6.3: check for a prior successful action from another
        # agent on this resource. This handles the race where both agents
        # got ALLOW (via conflict resolution on intents) while the other's
        # tool was still running. The later action supersedes the earlier.
        existing_actions = self.space.read(
            scope=scope, resource=resource, mark_type=MarkType.ACTION
        )
        prior_action = next(
            (
                a
                for a in existing_actions
                if isinstance(a, Action) and not a.failed and a.agent_id != agent.id
            ),
            None,
        )

        if prior_action is not None:
            # Later action supersedes the earlier one (Section 6.3).
            # The intent (if any) expires via TTL.
            supersedes_id = prior_action.id
        else:
            # Note: if the intent expired and was GC'd before this call,
            # intent_id references a missing mark. This is harmless - the
            # supersession adds a stale UUID to _superseded which is a no-op
            # (GC already removed the mark, and read() filters by _superseded).
            supersedes_id = intent_id

        return self.space.write(
            agent,
            Action(
                scope=scope,
                resource=resource,
                action=action,
                result=result,
                supersedes=supersedes_id,
            ),
        )

    def execute(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        intent_action: str,
        result_action: str,
        tool_fn: ToolFn,
        confidence: float = 0.5,
        tool_args: tuple[Any, ...] = (),
        tool_kwargs: dict[str, Any] | None = None,
    ) -> tuple[GuardDecision, Any]:
        """
        Full guarded execution: pre_action -> tool_fn -> post_action.
        Returns (decision, result). If decision is not ALLOW, tool_fn is never called.

        This is the primary API for the harness. One call wraps the entire
        check-execute-record cycle.

        Note: For scopes using DEFERRED conflict resolution, pre_action always
        returns BLOCKED (the intent is recorded but resolution is deferred to
        resolve_deferred()). This means execute() will never call tool_fn and
        always returns (BLOCKED, None). Use the three-phase pattern instead:
        pre_action -> resolve_deferred -> tool_fn -> post_action.

        Thread safety: The per-resource lock is held for pre_action and
        post_action separately, but released during tool_fn execution.
        This prevents slow tool calls (network, LLM inference) from
        blocking other agents on the same resource. The intent mark
        written during pre_action protects the resource via conflict
        resolution while the lock is released.

            decision, result = guard.execute(
                agent=booker,
                scope="calendar",
                resource="thu-14:00",
                intent_action="book",
                result_action="booked",
                tool_fn=book_flight,
                confidence=0.9,
                tool_kwargs={"flight": "DL413"},
            )
        """
        if tool_kwargs is None:
            tool_kwargs = {}

        # Phase 1: pre_action under resource lock (declare intent, check conflicts)
        with self._resource_lock(scope, resource):
            decision = self._pre_action_inner(
                agent, scope, resource, intent_action, confidence
            )

        if decision.verdict != GuardVerdict.ALLOW:
            return decision, None

        intent_id = decision.intent_id

        # Phase 2: execute tool WITHOUT holding the resource lock.
        # The intent mark protects the resource via conflict resolution.
        try:
            result = tool_fn(*tool_args, **tool_kwargs)
        except Exception as exc:
            # Write a failed action that supersedes the intent
            with self._resource_lock(scope, resource):
                self.space.write(
                    agent,
                    Action(
                        scope=scope,
                        resource=resource,
                        action=result_action,
                        result={"error": str(exc), "status": "failed"},
                        failed=True,
                        supersedes=intent_id,
                    ),
                )
            raise

        # Phase 3: post_action under resource lock (record action mark)
        with self._resource_lock(scope, resource):
            self.post_action(agent, scope, resource, result_action, result, intent_id)

        return decision, result
