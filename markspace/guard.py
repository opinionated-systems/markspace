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

import inspect
import logging
import math
import threading
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

from markspace.barrier import AgentBarrier, BarrierSnapshot
from markspace.budget import BudgetStatus, BudgetTracker, TokenBudget
from markspace.core import (
    Action,
    Agent,
    AnyMark,
    ConflictPolicy,
    DecayConfig,
    Intent,
    MarkType,
    Need,
    Observation,
    Scope,
    ScopeVisibility,
    Severity,
    Warning,
    hours,
    resolve_conflict,
    scope_contains,
    scope_root,
)
from markspace.envelope import EnvelopeVerdict, StatisticalEnvelope
from markspace.rate_limit import RateLimitTracker
from markspace.space import (
    IntentJudgement,
    MarkSpace,
    QuotaExceededError,
    ScopeError,
    ValidationError,
)
from markspace.telemetry import (
    METRIC_CONFLICTS_RESOLVED,
    METRIC_LEASES_EXPIRED,
    METRIC_LEASES_GRANTED,
    METRIC_LEASES_LOST,
    METRIC_MARKS_WRITTEN,
    METRIC_TOKENS_INPUT,
    METRIC_TOKENS_OUTPUT,
    NullSink,
    TelemetryEvent,
    TelemetrySink,
)
from markspace.write_budget import Burn, WriteBudget, WriteBudgetMeter


# ---------------------------------------------------------------------------
# Content policy types
# ---------------------------------------------------------------------------


class PolicyVerdict(str, Enum):
    """Result of evaluating a content policy."""

    ALLOW = "allow"  # Content passes, write as-is
    REWRITE = "rewrite"  # Content modified, write the rewritten version
    REJECT = "reject"  # Content blocked, mark not written
    FLAG = "flag"  # Content allowed but flagged (warning emitted)


@dataclass
class PolicyResult:
    """Outcome of a single content policy evaluation."""

    verdict: PolicyVerdict
    content: Any = None  # Rewritten content (REWRITE only). MarkPayload type.
    reason: str | None = None
    metadata: dict | None = None


class ContentPolicy(ABC):
    """Base class for content policies applied to Observation marks before write.

    Content policies are only evaluated for Observation marks in the
    ``write_mark()`` path. Warning, Need, Intent, and Action marks are
    not subject to content policies.

    Implementations must be thread-safe - multiple marks may be evaluated
    concurrently from different ``write_mark()`` calls. The ``evaluate()``
    method should be fast for the common case (ALLOW). Expensive checks
    (LLM calls) should use caching or async pre-computation.

    Note: ``mark.content`` is typed as ``MarkPayload`` (``Any``), so it may
    be a string, dict, list, number, or None. Policies that only handle
    string content should check the type and ALLOW non-string marks.
    """

    @abstractmethod
    def evaluate(
        self,
        agent: Agent,
        mark: Observation,
        scope: Scope,
    ) -> PolicyResult:
        """Evaluate observation content against this policy."""
        ...

    @property
    def name(self) -> str:
        """Policy name for telemetry and logging."""
        return self.__class__.__name__


class ContentPolicyError(ScopeError):
    """Raised when a content policy rejects a mark."""

    pass


# ---------------------------------------------------------------------------
# Guard verdict / decision types
# ---------------------------------------------------------------------------


class GuardVerdict(str, Enum):
    """Result of a guard check."""

    ALLOW = "allow"  # No conflict. Proceed.
    CONFLICT = "conflict"  # Another agent has priority. Yield.
    BLOCKED = "blocked"  # YIELD_ALL policy - need principal input.
    DENIED = "denied"  # Agent not authorized for this scope/action.


class NotGrantedError(Exception):
    """
    An action written through the guard that no live grant covers (Section
    6.5, G6): its lease expired or was lost, or a direct claim is refused.
    Nothing is written.
    """


@dataclass(frozen=True, order=True)
class FencingToken:
    """
    A fencing token (Section 6.5, G8). Tokens compare as (epoch, n): n grows
    with every grant on one resource in one scope tree, within one epoch. An
    outside system that rejects a token lower than one it has seen for the
    resource rejects a stale holder. Burrows, "The Chubby lock service"
    (sequencers), OSDI 2006.
    """

    epoch: int
    n: int


class ClaimState(str, Enum):
    """The claim state of an intent (Section 6.5). Every state but GRANTED is final."""

    GRANTED = "granted"  # holds a lease
    ACTED = "acted"  # its agent's action superseded it; the action holds (P39)
    RELEASED = "released"  # a failed action of its agent, or the principal
    EXPIRED = "expired"  # its lease reached its expiry
    LOST = "lost"  # refused or beaten; it never competes again


@dataclass
class _Claim:
    """An intent the lease table holds: granted, or closed. No entry: pending."""

    intent: Intent
    state: ClaimState
    token: FencingToken | None = None
    expires_at: float | None = None


def _competes(a: str, b: str) -> bool:
    """Whether claims in scopes *a* and *b* compete: one contains the other."""
    return scope_contains(a, b) or scope_contains(b, a)


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
    # Telemetry only: True when another agent's intent or action competed for
    # the resource, including marks in scopes this agent may not read.
    # Harnesses MUST NOT show it to the agent.
    conflict_found: bool = field(default=False, repr=False)
    # With ALLOW: the lease's fencing token and expiry (Section 6.5). The token
    # goes to the tool and the outside system; harnesses MUST NOT show it to
    # the agent (it counts grants on the resource, P25).
    fencing_token: FencingToken | None = field(default=None, repr=False)
    lease_expires_at: float | None = None


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


@dataclass(frozen=True)
class _WriteReservation:
    """A write reserved against an agent's write budget (Section 9.14)."""

    agent: Agent
    scope: str
    mark_type: MarkType
    budget: WriteBudget
    at: float


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
            guard.post_action(
                agent, "calendar", "thu-14:00", "booked", result, decision.intent_id
            )

        # Or use the wrapped executor:
        result = guard.execute(agent, "calendar", "thu-14:00", "book", "booked",
                               confidence=0.9, tool_fn=actual_tool_function)
    """

    def __init__(
        self,
        space: MarkSpace,
        envelope: StatisticalEnvelope | None = None,
        principal_token: uuid.UUID | None = None,
        telemetry: TelemetrySink | None = None,
        content_policies: list[ContentPolicy] | None = None,
        epoch: int | None = None,
        write_budgets: dict[uuid.UUID, WriteBudget] | None = None,
    ) -> None:
        self.space = space
        self._envelope = envelope
        self._principal_token = principal_token or uuid.uuid4()
        self._telemetry: TelemetrySink = telemetry or NullSink()
        self._content_policies: list[ContentPolicy] = content_policies or []
        # Per-resource locks: keyed by (scope, resource).
        # A global lock protects the _resource_locks dict itself.
        self._resource_locks: dict[tuple[str, str], _ResourceLock] = {}
        self._meta_lock = threading.Lock()
        # Barriers: per-agent permission restrictions
        self._barriers: dict[uuid.UUID, AgentBarrier] = {}
        self._barrier_lock = threading.Lock()
        # Budget trackers: per-agent token budget state
        self._budget_trackers: dict[uuid.UUID, BudgetTracker] = {}
        self._budget_lock = threading.Lock()
        # Rate limit tracker: shared across all scopes, protected by its own lock
        self._rate_limit_tracker = RateLimitTracker()
        self._rate_limit_lock = threading.Lock()
        # Write budgets (Section 9.14): the principal's table and the meter,
        # under one lock. Never held while taking the space lock.
        self._write_budgets: dict[uuid.UUID, WriteBudget] = dict(write_budgets or {})
        self._write_budget_meter = WriteBudgetMeter()
        self._write_budget_lock = threading.Lock()
        # Need marks the guard writes in _system for the principal: (trigger,
        # agent, ...) -> the last one written for that key; a new one is
        # written only when that one is no longer open. The need lock is held
        # across the check and the write; never taken under the space lock.
        self._open_needs: dict[tuple[Any, ...], uuid.UUID] = {}
        self._need_lock = threading.Lock()
        # System agent for guard-originated warnings/needs.
        # Exempt from envelope monitoring to prevent feedback loops.
        self._system_agent = Agent(
            name="_guard",
            scopes={"*": ["warning", "need"], "_system": ["warning", "need"]},
            read_scopes=frozenset(),
        )
        # Register the _system scope for guard-originated marks (budget
        # warnings, audit needs) that don't belong to any user-defined scope.
        # It is CLASSIFIED: its marks name agents and scopes, also
        # CLASSIFIED ones.
        system_scope = space._scopes.get("_system")
        if (
            system_scope is not None
            and system_scope.visibility != ScopeVisibility.CLASSIFIED
        ):
            raise ValueError("the _system scope must be CLASSIFIED (spec 9.13)")
        if system_scope is None:
            space.register_scope(
                Scope(
                    name="_system",
                    visibility=ScopeVisibility.CLASSIFIED,
                    decay=DecayConfig(
                        observation_half_life=hours(24),
                        warning_half_life=hours(12),
                        intent_ttl=hours(1),
                    ),
                )
            )
        # Grants (Section 6.5): the lease table, one fencing counter per
        # (scope-tree root, resource), and the lease clock. The table's lock
        # is taken inside the space lock, or alone for a single read; under it
        # the guard calls only space methods that re-enter the space lock its
        # thread holds.
        # shortcut: the epoch defaults to the wall clock at construction; a
        # clock that reads lower after a restart makes an outside system
        # reject new tokens until it passes the old epoch. Pass an epoch from
        # a persisted counter to fence across restarts.
        self.epoch: int = time.time_ns() if epoch is None else epoch
        self._claims: dict[uuid.UUID, _Claim] = {}
        self._claims_by_resource: dict[tuple[str, str], set[uuid.UUID]] = {}
        self._granted: dict[tuple[str, str], set[uuid.UUID]] = {}
        # shortcut: one integer per (root, resource) ever granted stays in
        # memory; drop counters below a persisted high-water mark when a space
        # must run with unbounded resource names.
        self._counters: dict[tuple[str, str], int] = {}
        self._lease_now = -math.inf
        self._claim_lock = threading.RLock()
        # Grant telemetry (metric, labels), emitted after the locks are
        # released: a sink is outside code.
        self._lease_events: list[tuple[str, dict[str, str]]] = []
        # One principal token for the whole stack (P40): refuse another token
        # before the space is attached, bind once the space has accepted this
        # guard, so that neither refusal leaves the other bound. The check and
        # the bind are two steps: build guards that share one envelope one at
        # a time.
        if envelope is not None and not envelope.accepts_principal(
            self._principal_token
        ):
            raise ValueError("the statistical envelope has another principal token")
        space.attach_claim_table(self)
        if envelope is not None:
            envelope.bind_principal(self._principal_token)
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
        # Competing scopes (one contains the other) share their root, so they
        # take the same lock (Section 6.1).
        key = (scope_root(scope), resource)
        with self._meta_lock:
            if key not in self._resource_locks:
                self._resource_locks[key] = _ResourceLock()
            entry = self._resource_locks[key]
            entry.active += 1
        try:
            # Write hooks of writes made under this lock fire after it is
            # released (Section 8.6).
            with self.space.hooks_deferred(), entry.lock:
                yield
        finally:
            with self._meta_lock:
                entry.active -= 1

    # ------------------------------------------------------------------
    # Grants: the lease table (Section 6.5)
    # ------------------------------------------------------------------

    def on_supersede(self, mark: AnyMark) -> None:
        """
        The space stored *mark*, which supersedes mark.supersedes. Only the
        intent's own agent may supersede it (Section 10), so a granted intent
        superseded by its agent's action ends its lease: acted, or released
        when the action failed or the mark is not an action.
        """
        if mark.supersedes is None:
            return
        with self._claim_lock:
            claim = self._claims.get(mark.supersedes)
            if claim is None:
                return
            if isinstance(mark, Action) and not mark.failed:
                # post_action checked the lease under the space lock this
                # write still holds: the action holds the resource.
                if claim.state == ClaimState.GRANTED:
                    self._end(claim, ClaimState.ACTED)
                return
            if self._live(claim, self._tick()):
                self._end(claim, ClaimState.RELEASED)

    def on_gc(self) -> None:
        """gc ran: drop closed entries whose intent mark is gone (never a granted one)."""
        with self._claim_lock:
            gone = [
                iid
                for iid, c in self._claims.items()
                if c.state != ClaimState.GRANTED and self.space.get_mark(iid) is None
            ]
            for iid in gone:
                claim = self._claims.pop(iid)
                key = (scope_root(claim.intent.scope), claim.intent.resource)
                ids = self._claims_by_resource.get(key)
                if ids is not None:
                    ids.discard(iid)
                    if not ids:
                        del self._claims_by_resource[key]

    def leases_and_closed(
        self, scope: str, resource: str
    ) -> tuple[list[uuid.UUID], set[uuid.UUID]]:
        """See ClaimTable.leases_and_closed (markspace.space)."""
        with self._claim_lock:
            lease_now = self._tick()
            leases = [
                c.intent.id for c in self._live_leases(scope, resource, lease_now)
            ]
            closed = {
                iid
                for iid in self._claims_by_resource.get(
                    (scope_root(scope), resource), ()
                )
                if _competes(self._claims[iid].intent.scope, scope)
            }
            return leases, closed

    def claim_state(self, intent_id: uuid.UUID) -> ClaimState | None:
        """
        The claim state of an intent (Section 6.5): GRANTED, or how it was
        closed; None while it is pending, or for an intent this guard has no
        entry for. A lease past its expiry reads as EXPIRED.
        """
        with self.space.locked(), self._claim_lock:
            claim = self._claims.get(intent_id)
            if claim is None:
                return None
            self._live(claim, self._tick())
            state = claim.state
        self._emit_lease_events()
        return state

    def _tick(self) -> float:
        """
        The lease clock: never goes back, so a clock step back cannot make an
        expired lease live again (Gray, Cheriton, "Leases", SOSP 1989).
        Caller MUST hold _claim_lock.
        """
        self._lease_now = max(self._lease_now, self.space.now())
        return self._lease_now

    def _end(self, claim: _Claim, state: ClaimState) -> None:
        """Close a granted claim. Caller MUST hold _claim_lock."""
        key = (scope_root(claim.intent.scope), claim.intent.resource)
        ids = self._granted.get(key)
        if ids is not None:
            ids.discard(claim.intent.id)
            if not ids:
                del self._granted[key]
        claim.state = state
        if state == ClaimState.EXPIRED:
            self._lease_event(METRIC_LEASES_EXPIRED, claim.intent)

    def _store(self, claim: _Claim) -> None:
        """Record a new entry. Caller MUST hold _claim_lock."""
        key = (scope_root(claim.intent.scope), claim.intent.resource)
        self._claims[claim.intent.id] = claim
        self._claims_by_resource.setdefault(key, set()).add(claim.intent.id)
        if claim.state == ClaimState.GRANTED:
            self._granted.setdefault(key, set()).add(claim.intent.id)

    def _lose(self, intent: Intent) -> None:
        """A pending intent was refused or beaten. Caller MUST hold _claim_lock."""
        if intent.id not in self._claims:
            self._store(_Claim(intent=intent, state=ClaimState.LOST))
            self._lease_event(METRIC_LEASES_LOST, intent)

    def _is_pending(self, intent: Intent) -> bool:
        """No entry: written, not granted, not closed. Caller MUST hold _claim_lock."""
        return intent.id not in self._claims

    def _live(self, claim: _Claim, lease_now: float) -> bool:
        """
        Whether *claim* holds a live lease; one past its expiry is recorded as
        expired, for good. Caller MUST hold _claim_lock.
        """
        if claim.state != ClaimState.GRANTED:
            return False
        assert claim.expires_at is not None  # a granted claim has an expiry
        if lease_now >= claim.expires_at:
            self._end(claim, ClaimState.EXPIRED)
            return False
        return True

    def _live_leases(self, scope: str, resource: str, lease_now: float) -> list[_Claim]:
        """
        Live leases on *resource* in *scope*, its ancestors and its
        descendants, lowest token first. Caller MUST hold _claim_lock.
        """
        live: list[_Claim] = []
        for iid in list(self._granted.get((scope_root(scope), resource), ())):
            claim = self._claims[iid]
            if self._live(claim, lease_now) and _competes(claim.intent.scope, scope):
                live.append(claim)
        return sorted(
            live, key=lambda c: (c.token.n if c.token else 0, str(c.intent.id))
        )

    def _grant(self, intent: Intent, lease_now: float) -> _Claim:
        """
        Grant a lease to *intent* for its scope's intent TTL, with the next
        fencing token of its scope tree and resource. Caller MUST hold
        _claim_lock and have checked G1.
        """
        key = (scope_root(intent.scope), intent.resource)
        n = self._counters.get(key, 0) + 1
        self._counters[key] = n
        ttl = self.space.get_scope(intent.scope).decay.intent_ttl
        claim = _Claim(
            intent=intent,
            state=ClaimState.GRANTED,
            token=FencingToken(self.epoch, n),
            expires_at=lease_now + ttl,
        )
        self._store(claim)
        self._lease_event(METRIC_LEASES_GRANTED, intent)
        return claim

    def _held_action(
        self, agent_id: uuid.UUID, scope: str, resource: str, now: float
    ) -> Action | None:
        """Another agent's held action on *resource* in a competing scope (P39)."""
        return next(
            (
                a
                for a in self.space.get_actions(scope, resource, now=now)
                if not a.failed and a.agent_id != agent_id
            ),
            None,
        )

    def _lease_event(self, name: str, intent: Intent) -> None:
        """Queue a lease metric: scope and resource, never the token."""
        self._lease_events.append(
            (name, {"scope": intent.scope, "resource": intent.resource})
        )

    def _emit_lease_events(self) -> None:
        """Emit the grant telemetry queued under the locks."""
        with self._claim_lock:
            events, self._lease_events = self._lease_events, []
        for name, labels in events:
            try:
                self._telemetry.record_counter(name, 1, labels)
            except Exception:
                logger.debug("Metric emission failed", exc_info=True)

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
    # Telemetry helpers
    # ------------------------------------------------------------------

    def _emit_telemetry(
        self,
        agent: Agent,
        operation: str,
        scope: str,
        mark_type: str,
        verdict: str,
        **kwargs: Any,
    ) -> None:
        """Emit a telemetry event. Failures are swallowed (P57)."""
        try:
            event = TelemetryEvent(
                agent_id=str(agent.id),
                operation=operation,
                scope=scope,
                mark_type=mark_type,
                verdict=verdict,
                **kwargs,
            )
            self._telemetry.emit_event(event)
        except Exception:
            logger.debug("Telemetry emission failed", exc_info=True)

    def _emit_write_metric(
        self, agent: Agent, scope: str, mark_type: str, verdict: str
    ) -> None:
        """Emit markspace.marks.written counter. Failures swallowed (P57)."""
        try:
            self._telemetry.record_counter(
                METRIC_MARKS_WRITTEN,
                1,
                {
                    "agent_id": str(agent.id),
                    "scope": scope,
                    "mark_type": mark_type,
                    "verdict": verdict,
                },
            )
        except Exception:
            logger.debug("Metric emission failed", exc_info=True)

    # ------------------------------------------------------------------
    # Rate limit enforcement
    # ------------------------------------------------------------------

    def _check_rate_limit(self, agent: Agent, scope: str) -> str | None:
        """Check scope rate limit. Returns rejection reason or None.

        Thread-safe: acquires _rate_limit_lock to serialize
        check_and_record (read-check-write is non-atomic).
        """
        scope_def = self.space.get_scope(scope)
        if scope_def.rate_limit is None:
            return None
        with self._rate_limit_lock:
            return self._rate_limit_tracker.check_and_record(
                scope, agent.id, scope_def.rate_limit, self.space.now()
            )

    # ------------------------------------------------------------------
    # Content policy enforcement
    # ------------------------------------------------------------------

    def _check_content_policies(
        self, agent: Agent, mark: Observation, scope: Scope
    ) -> tuple[Observation, str | None]:
        """Run content policies on an Observation mark.

        Returns ``(possibly_rewritten_mark, rejection_reason)``.

        Policies compose as a pipeline: a REWRITE from policy A feeds the
        rewritten mark into policy B.  First REJECT wins.  FLAGS accumulate
        and emit warnings to ``_system`` scope.

        Only called for Observation marks (P69).
        """
        current_mark = mark
        flags: list[PolicyResult] = []

        for policy in self._content_policies:
            result = policy.evaluate(agent, current_mark, scope)

            self._emit_telemetry(
                agent,
                "content_policy",
                scope.name,
                current_mark.mark_type.value,
                result.verdict.value,
                reason=result.reason or "",
                extra={"policy": policy.name},
            )

            if result.verdict == PolicyVerdict.REJECT:
                return current_mark, result.reason or f"Rejected by {policy.name}"

            elif result.verdict == PolicyVerdict.REWRITE:
                current_mark = current_mark.model_copy(
                    update={"content": result.content}
                )

            elif result.verdict == PolicyVerdict.FLAG:
                flags.append(result)

        if flags:
            reasons = "; ".join(f.reason for f in flags if f.reason)
            detail = f": {reasons}" if reasons else ""
            try:
                self.space.write(
                    self._system_agent,
                    Warning(
                        scope="_system",
                        topic="content-policy-flag",
                        reason=(
                            f"Content flagged for agent '{agent.name}' "
                            f"in scope '{scope.name}'{detail}"
                        ),
                        severity=Severity.CAUTION,
                    ),
                )
            except Exception:
                logger.debug(
                    "Failed to write content-policy-flag warning", exc_info=True
                )

        return current_mark, None

    # ------------------------------------------------------------------
    # Budget enforcement
    # ------------------------------------------------------------------

    def get_budget_status(self, agent_id: uuid.UUID) -> tuple[int, int, bool] | None:
        """Snapshot of an agent's budget consumption: (input, output, exhausted).

        Returns None if the agent has no budget tracker.
        Returns a tuple, not the mutable tracker, to prevent races.
        """
        with self._budget_lock:
            tracker = self._budget_trackers.get(agent_id)
            if tracker is None:
                return None
            return (
                tracker.total_input_consumed,
                tracker.total_output_consumed,
                tracker.exhausted,
            )

    def check_budget_activation(self, agent: Agent) -> str | None:
        """Check if an agent's budget allows activation.

        Returns rejection reason if budget is exhausted, None if OK.
        Wired into the scheduler via pre_activation_check for scheduled
        agents. For subscription-activated agents, the caller must check
        this before each round.

        P59: agents without a budget always return None.
        P61: exhausted agents are rejected deterministically.
        """
        budget = self._get_agent_budget(agent)
        if budget is None:
            return None
        with self._budget_lock:
            tracker = self._budget_trackers.get(agent.id)
            if tracker is None:
                return None
            if tracker.is_exhausted(budget):
                return (
                    f"Agent '{agent.name}' budget exhausted "
                    f"(input: {tracker.total_input_consumed}, "
                    f"output: {tracker.total_output_consumed})"
                )
        return None

    def record_round_tokens(
        self, agent: Agent, input_tokens: int, output_tokens: int
    ) -> BudgetStatus:
        """Record token usage for a completed round.

        Called after each agent round. Tracks cumulative usage, checks
        warning/exhaustion thresholds, and writes Need marks when
        thresholds are crossed.

        Returns the most severe budget status after recording.

        P59: no-op for agents without a budget.
        P60: emits exactly one Need per dimension at warning threshold.
        P63: monotonically non-decreasing tracking.
        """
        # Emit token counters for all agents, regardless of budget
        try:
            self._telemetry.record_counter(
                METRIC_TOKENS_INPUT,
                input_tokens,
                {"agent_id": str(agent.id)},
            )
            self._telemetry.record_counter(
                METRIC_TOKENS_OUTPUT,
                output_tokens,
                {"agent_id": str(agent.id)},
            )
        except Exception:
            logger.debug("Token metric emission failed", exc_info=True)

        budget = self._get_agent_budget(agent)
        if budget is None:
            return BudgetStatus.OK

        # Hold the lock for the entire check-and-flag sequence so that
        # concurrent calls for the same agent cannot both see
        # warning_emitted as False and emit duplicate Need marks.
        warn_input = False
        warn_output = False
        input_consumed = 0
        output_consumed = 0

        with self._budget_lock:
            tracker = self._budget_trackers.get(agent.id)
            if tracker is None:
                tracker = BudgetTracker()
                self._budget_trackers[agent.id] = tracker

            tracker.record_input(input_tokens)
            tracker.record_output(output_tokens)
            status = tracker.check_lifetime(budget)

            # Mark warnings as emitted inside the lock to prevent races.
            # These are mutually exclusive (check_lifetime returns one status),
            # but written as independent checks for clarity.
            if status == BudgetStatus.WARNING_INPUT:
                warn_input = True
                tracker.warning_emitted_input = True
            if status == BudgetStatus.WARNING_OUTPUT:
                warn_output = True
                tracker.warning_emitted_output = True

            # Snapshot values for telemetry outside the lock
            input_consumed = tracker.total_input_consumed
            output_consumed = tracker.total_output_consumed

        # Write Need marks outside the lock (space.write acquires its own)
        if warn_input:
            self._emit_budget_warning(agent, budget, "input", input_consumed)
        if warn_output:
            self._emit_budget_warning(agent, budget, "output", output_consumed)
        if status in (BudgetStatus.EXHAUSTED_INPUT, BudgetStatus.EXHAUSTED_OUTPUT):
            dimension = "input" if status == BudgetStatus.EXHAUSTED_INPUT else "output"
            logger.info("Agent '%s' %s budget exhausted", agent.name, dimension)

        # Emit budget remaining gauges
        try:
            if budget.max_input_tokens_total is not None:
                self._telemetry.record_gauge(
                    "markspace.agent.budget.remaining",
                    max(0, budget.max_input_tokens_total - input_consumed),
                    {"agent_id": str(agent.id), "dimension": "input"},
                )
            if budget.max_output_tokens_total is not None:
                self._telemetry.record_gauge(
                    "markspace.agent.budget.remaining",
                    max(0, budget.max_output_tokens_total - output_consumed),
                    {"agent_id": str(agent.id), "dimension": "output"},
                )
        except Exception:
            logger.debug("Budget metric emission failed", exc_info=True)

        return status

    def update_budget(
        self, agent: Agent, new_budget: TokenBudget, principal_token: uuid.UUID
    ) -> bool:
        """Update an agent's budget (principal action).

        P62: if the new budget exceeds consumption, the agent resumes.
        Returns True if the update was applied.
        """
        if principal_token != self._principal_token:
            return False
        # Update the manifest on the agent (requires creating a new agent
        # since Agent is frozen). The caller is responsible for using the
        # returned agent going forward. Here we just update the tracker.
        with self._budget_lock:
            tracker = self._budget_trackers.get(agent.id)
            if tracker is not None:
                tracker.try_clear_exhaustion(new_budget)
        return True

    def _get_agent_budget(self, agent: Agent) -> TokenBudget | None:
        """Extract budget from agent's manifest, if any."""
        if agent.manifest is None:
            return None
        return agent.manifest.budget

    def _emit_budget_warning(
        self,
        agent: Agent,
        budget: TokenBudget,
        dimension: str,
        consumed: int,
    ) -> None:
        """Write a non-blocking Need mark when budget warning threshold is crossed.

        P60: exactly one Need per dimension.
        """
        total = (
            budget.max_input_tokens_total
            if dimension == "input"
            else budget.max_output_tokens_total
        )

        try:
            self.space.write(
                self._system_agent,
                Need(
                    scope="_system",
                    question=(
                        f"Agent '{agent.name}' has consumed "
                        f"{consumed}/{total} {dimension} tokens "
                        f"({budget.warning_fraction:.0%} of budget). "
                        f"Continue, increase, or stop?"
                    ),
                    context={
                        "agent_id": str(agent.id),
                        "agent_name": agent.name,
                        "dimension": dimension,
                        "consumed": consumed,
                        "total": total,
                        "warning_fraction": budget.warning_fraction,
                    },
                    priority=0.7,
                    blocking=False,
                ),
            )
        except Exception:
            logger.debug("Failed to write budget warning Need", exc_info=True)

    # ------------------------------------------------------------------
    # write_mark: single enforcement boundary for non-contested writes
    # ------------------------------------------------------------------

    def write_mark(self, agent: Agent, mark: AnyMark) -> uuid.UUID:
        """
        Write a non-contested mark (observation, warning, need) through
        the guard's enforcement stack.

        No conflict resolution. No resource locking. But does run:
        - Type validation (rejects intents/actions)
        - Barrier check (is this agent restricted?). A need mark of an agent
          restricted in the mark's scope goes to the principal: the guard
          writes it in _system, one open per agent, and returns its id
          (Section 9.8).
        - Envelope check (is this agent anomalous?)
        - Rate limit check (is this scope rate-limited?)
        - Content policy check (Observation marks only, P67-P70)
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

        # Barrier check. A warning that hides an action must also pass the
        # barrier of the action's own scope (Section 9.5): a restricted agent
        # could otherwise hide a booking there from another scope.
        barrier_result = self._check_barrier(agent, mark.scope, mark.mark_type)
        if (
            barrier_result is None
            and isinstance(mark, Warning)
            and mark.invalidates is not None
        ):
            target = self.space.get_mark(mark.invalidates)
            if target is not None and target.scope == "_system":
                # The guard's marks for the principal: no agent hides them
                # (Section 9.8).
                barrier_result = (
                    f"Agent '{agent.name}' may not warn on a mark in '_system'"
                )
            elif isinstance(target, Action) and target.scope != mark.scope:
                barrier_result = self._check_barrier(
                    agent, target.scope, MarkType.WARNING
                )
        if barrier_result is not None:
            self._emit_write_metric(agent, mark.scope, mark.mark_type.value, "rejected")
            self._emit_telemetry(
                agent,
                "write_mark",
                mark.scope,
                mark.mark_type.value,
                "rejected",
                reason=barrier_result,
                barrier_restricted=True,
            )
            raise ScopeError(barrier_result)

        # Envelope check
        envelope_result = self._check_envelope(agent, mark.scope)
        if envelope_result is not None:
            self._emit_write_metric(agent, mark.scope, mark.mark_type.value, "rejected")
            self._emit_telemetry(
                agent,
                "write_mark",
                mark.scope,
                mark.mark_type.value,
                "rejected",
                reason=envelope_result,
                envelope_status="restricted",
            )
            raise ScopeError(envelope_result)

        # Rate limit check (P64, P65)
        rate_limit_result = self._check_rate_limit(agent, mark.scope)
        if rate_limit_result is not None:
            # Make rate limit rejections visible to the envelope
            if self.envelope is not None:
                self.envelope.record_attempt(agent.id, mark.mark_type)
            self._emit_write_metric(agent, mark.scope, mark.mark_type.value, "rejected")
            self._emit_telemetry(
                agent,
                "write_mark",
                mark.scope,
                mark.mark_type.value,
                "rejected",
                reason=rate_limit_result,
            )
            raise ScopeError(rate_limit_result)

        # Content policy check (Observation marks only - P69)
        if self._content_policies and isinstance(mark, Observation):
            scope_def = self.space.get_scope(mark.scope)
            mark, rejection = self._check_content_policies(agent, mark, scope_def)
            if rejection:
                self._emit_write_metric(
                    agent, mark.scope, mark.mark_type.value, "content_rejected"
                )
                self._emit_telemetry(
                    agent,
                    "write_mark",
                    mark.scope,
                    mark.mark_type.value,
                    "rejected",
                    reason=rejection,
                )
                raise ContentPolicyError(rejection)

        # Write budget (Section 9.14), last before the write: the write is
        # reserved here and released if the space refuses it.
        write_budget_result, reservation = (
            self._reserve_write(agent, mark.scope, mark.mark_type)
            if agent.can_write(mark.scope, mark.mark_type)
            else (None, None)
        )
        if write_budget_result is not None:
            self._emit_write_metric(agent, mark.scope, mark.mark_type.value, "rejected")
            self._emit_telemetry(
                agent,
                "write_mark",
                mark.scope,
                mark.mark_type.value,
                "rejected",
                reason=write_budget_result,
            )
            raise ScopeError(write_budget_result)

        try:
            if isinstance(mark, Need) and self._revoked_to_act(
                self.get_barrier(agent.id), mark.scope
            ):
                mark_id = self._ask_principal(agent, mark)
            else:
                mark_id = self.space.write(agent, mark)
        except BaseException as exc:
            self._release_write(reservation)
            if not isinstance(exc, (ScopeError, ValidationError, QuotaExceededError)):
                raise
            self._emit_write_metric(agent, mark.scope, mark.mark_type.value, "denied")
            self._emit_telemetry(
                agent,
                "write_mark",
                mark.scope,
                mark.mark_type.value,
                "denied",
                reason=str(exc),
            )
            raise

        self._confirm_write(reservation)

        # Telemetry: successful write (P58)
        self._emit_write_metric(agent, mark.scope, mark.mark_type.value, "accepted")
        self._emit_telemetry(
            agent,
            "write_mark",
            mark.scope,
            mark.mark_type.value,
            "accepted",
        )

        return mark_id

    # ------------------------------------------------------------------
    # Write budgets (Section 9.14)
    # ------------------------------------------------------------------

    def set_write_budget(
        self,
        agent_id: uuid.UUID,
        budget: WriteBudget | None,
        principal_token: uuid.UUID,
    ) -> bool:
        """Set or remove (None) an agent's write budget (principal action). It
        takes precedence over the manifest's expected_activity, and it starts
        a new count: the agent's counted writes are cleared. Returns False for
        a wrong token."""
        if principal_token != self._principal_token:
            return False
        with self._write_budget_lock:
            if budget is None:
                self._write_budgets.pop(agent_id, None)
            else:
                self._write_budgets[agent_id] = budget
            self._write_budget_meter.forget(agent_id)
        with self._need_lock:
            for key in [
                k
                for k in self._open_needs
                if k[0] == "write_budget" and k[1] == agent_id
            ]:
                del self._open_needs[key]
        return True

    def _write_budget_for(self, agent: Agent) -> WriteBudget | None:
        """The principal's write budget for the agent, else one from its
        manifest's expected_activity (WriteBudget.from_expected_activity),
        else None (not metered)."""
        with self._write_budget_lock:
            budget = self._write_budgets.get(agent.id)
        if budget is not None:
            return budget
        if agent.manifest is not None and agent.manifest.expected_activity:
            return WriteBudget.from_expected_activity(
                {
                    MarkType(name): rate
                    for name, rate in agent.manifest.expected_activity.items()
                }
            )
        return None

    def _reserve_write(
        self,
        agent: Agent,
        scope: str,
        mark_type: MarkType,
        force: bool = False,
        also_spent: MarkType | None = None,
    ) -> tuple[str | None, _WriteReservation | None]:
        """Reserve one write against the agent's write budget (Section 9.14).
        Returns the refusal reason when the budget of the type (or of
        `also_spent`, which is checked but not counted) is spent, else the
        reservation to confirm once the write is made or release if it is not.
        `force` counts a write that has already happened also over the budget.
        The first refusal of an exhaustion writes a need mark."""
        budget = self._write_budget_for(agent)
        if budget is None:
            return None, None
        meter = self._write_budget_meter
        with self._write_budget_lock:
            now = self.space.now()
            if also_spent is not None and meter.spent(
                agent.id, budget, also_spent, now
            ):
                spent_type: MarkType = also_spent
                started = meter.note_refusal(agent.id, also_spent)
                reservation = None
            else:
                spent_type = mark_type
                reservation = meter.reserve(
                    agent.id, budget, mark_type, now, force=force
                )
                started = reservation.exhaustion_started
        # Actions are recorded also over the budget; only intents are refused.
        refused_writes = (
            "intents" if spent_type == MarkType.ACTION else "writes of this type"
        )
        if started:
            self._notify_write_budget(
                agent,
                spent_type,
                "exhausted",
                question=(
                    f"Agent '{agent.name}' has spent its write budget for "
                    f"{spent_type.value}; its further {refused_writes} are refused"
                ),
                context={
                    "scope": scope,
                    "budget": budget.budget(spent_type),
                    "period_seconds": budget.period_seconds,
                },
                priority=0.9,
            )
        if reservation is None or reservation.refused:
            return (
                f"Agent '{agent.name}' has spent its write budget for "
                f"{spent_type.value} over {budget.period_seconds / 86_400:g} days; "
                f"its further {refused_writes} are refused until it is back within "
                f"the budget"
            ), None
        assert reservation.at is not None  # noqa: S101
        return None, _WriteReservation(agent, scope, mark_type, budget, reservation.at)

    def _confirm_write(self, reservation: _WriteReservation | None) -> None:
        """The reserved write was made: tiers that start firing on it write
        need marks for the principal."""
        if reservation is None:
            return
        r = reservation
        with self._write_budget_lock:
            burns = self._write_budget_meter.confirm(
                r.agent.id, r.budget, r.mark_type, r.at
            )
        for burn in burns:
            self._notify_write_budget(
                r.agent,
                r.mark_type,
                burn.tier.level,
                question=(
                    f"Agent '{r.agent.name}' spends its write budget for "
                    f"{r.mark_type.value} fast ({burn.tier.level}): {burn.long_count} "
                    f"in {burn.tier.long_window_seconds:g} s"
                ),
                context={
                    "scope": r.scope,
                    "budget_fraction": burn.tier.budget_fraction,
                    "long_window_seconds": burn.tier.long_window_seconds,
                    "long_count": burn.long_count,
                    "long_threshold": burn.long_threshold,
                    "short_window_seconds": burn.tier.short_window_seconds,
                    "short_count": burn.short_count,
                    "short_threshold": burn.short_threshold,
                },
                priority=0.9 if burn.tier.level == "page" else 0.5,
            )

    def _release_write(self, reservation: _WriteReservation | None) -> None:
        """The reserved write was not made: it no longer counts."""
        if reservation is None:
            return
        with self._write_budget_lock:
            self._write_budget_meter.release(
                reservation.agent.id, reservation.mark_type, reservation.at
            )

    def _notify_write_budget(
        self,
        agent: Agent,
        mark_type: MarkType,
        kind: str,
        question: str,
        context: dict[str, Any],
        priority: float,
    ) -> None:
        """A write-budget need mark for the principal (Section 9.14), one open
        per agent, mark type and kind."""
        self._write_principal_need(
            ("write_budget", agent.id, mark_type, kind),
            question,
            {
                "agent_id": str(agent.id),
                "trigger": "write_budget",
                "level": kind,
                "mark_type": mark_type.value,
                **context,
            },
            priority,
        )

    def _write_principal_need(
        self,
        key: tuple[Any, ...],
        question: str,
        context: dict[str, Any],
        priority: float,
        blocking: bool = False,
        raise_errors: bool = False,
    ) -> uuid.UUID | None:
        """
        A need mark in _system for the principal, written by the guard's
        system agent, unless the last one written for *key* is still open
        (current: not resolved, Section 8.3). Returns the id of the new need
        mark, or None when an open one exists. A write that the space refuses
        is logged and returns None, or raises when *raise_errors*. The write
        hooks fire after the lock is released (Section 8.6).
        """
        with self.space.hooks_deferred(), self._need_lock:
            previous = self._open_needs.get(key)
            if previous is not None and self.space.is_current(previous):
                return None
            try:
                need_id = self.space.write(
                    self._system_agent,
                    Need(
                        scope="_system",
                        question=question,
                        context=context,
                        priority=priority,
                        blocking=blocking,
                    ),
                )
            except Exception:
                if raise_errors:
                    raise
                logger.warning(
                    "Failed to write a need mark for the principal (%s)",
                    context.get("trigger"),
                    exc_info=True,
                )
                return None
            self._open_needs[key] = need_id
            return need_id

    def _ask_principal(self, agent: Agent, need: Need) -> uuid.UUID:
        """
        The need mark of an agent restricted in its scope goes to the
        principal: the guard writes it in _system, with the agent's id, the
        scope and the agent's question and context, one open per agent
        (Section 9.8). No other agent reads it. Raises ScopeError while the
        agent's last one is open.
        """
        self.space.get_scope(need.scope)  # raises for an unknown scope
        if not agent.can_write(need.scope, MarkType.NEED):
            raise ScopeError(
                f"Agent '{agent.name}' is not authorized to write need in scope "
                f"'{need.scope}'"
            )
        need_id = self._write_principal_need(
            ("restricted_agent", agent.id),
            need.question,
            {
                "agent_id": str(agent.id),
                "trigger": "restricted_agent",
                "scope": need.scope,
                "agent_context": need.context,
            },
            priority=need.priority,
            blocking=need.blocking,
            raise_errors=True,
        )
        if need_id is None:
            raise ScopeError(
                f"Agent '{agent.name}' is restricted in scope '{need.scope}' and "
                f"has an open need mark for the principal; the principal resolves "
                f"it first"
            )
        return need_id

    def _asked_principal(self, agent: Agent, scope: str) -> bool:
        """Whether the agent holds an open need mark for the principal that it
        wrote while restricted in *scope* or a scope inside it
        (_ask_principal): the one the guard's record names, while current,
        as the one-open rule reads it. A dict read: no lock, because a
        boundary calls this under the space lock."""
        need_id = self._open_needs.get(("restricted_agent", agent.id))
        if need_id is None or not self.space.is_current(need_id):
            return False
        need = self.space.get_mark(need_id)
        return isinstance(need, Need) and scope_contains(
            scope, str(need.context.get("scope"))
        )

    # ------------------------------------------------------------------
    # Barrier management
    # ------------------------------------------------------------------

    @property
    def envelope(self) -> StatisticalEnvelope | None:
        """The statistical envelope, fixed when the guard is built (P40)."""
        return self._envelope

    def get_barrier(self, agent_id: uuid.UUID) -> BarrierSnapshot | None:
        """Get a frozen snapshot of an agent's barrier, if any.

        Returns an immutable BarrierSnapshot, not the mutable internal
        AgentBarrier, so callers cannot mutate guard state through the
        returned reference.
        """
        with self._barrier_lock:
            barrier = self._barriers.get(agent_id)
            if barrier is None:
                return None
            return barrier.snapshot()

    def get_or_create_barrier(self, agent_id: uuid.UUID) -> AgentBarrier:
        """Get or create the mutable barrier for an agent.

        For guard-internal and experiment code that needs to modify
        barrier state directly (narrow, require_need, etc.).
        Callers that only need to inspect state should use get_barrier().
        """
        with self._barrier_lock:
            barrier = self._barriers.get(agent_id)
            if barrier is None:
                barrier = AgentBarrier(
                    agent_id=agent_id,
                    _principal_token=self._principal_token,
                )
                self._barriers[agent_id] = barrier
            return barrier

    def set_barrier(
        self,
        agent_id: uuid.UUID,
        barrier: AgentBarrier | BarrierSnapshot,
        principal_token: uuid.UUID,
    ) -> bool:
        """
        Set the barrier of an agent (principal action, P45): an AgentBarrier
        with the guard's principal token, or a BarrierSnapshot from
        get_barrier(), for example one saved before a restart, which the
        guard turns into an AgentBarrier with its token. A wrong principal
        token changes nothing and returns False.
        """
        if principal_token != self._principal_token:
            return False
        if isinstance(barrier, BarrierSnapshot):
            barrier = AgentBarrier(
                agent_id=barrier.agent_id,
                _principal_token=self._principal_token,
                _revoked=set(barrier.revoked),
                _require_need_scopes=set(barrier.require_need_scopes),
                _flag_count=barrier.flag_count,
                flagged_scopes=set(barrier.flagged_scopes),
            )
        elif not isinstance(barrier, AgentBarrier):
            raise TypeError(f"not a barrier: {type(barrier).__name__}")
        elif barrier._principal_token != self._principal_token:
            raise ValueError("the barrier has another principal token")
        if barrier.agent_id != agent_id:
            raise ValueError(f"barrier of agent {barrier.agent_id}, not {agent_id}")
        with self._barrier_lock:
            self._barriers[agent_id] = barrier
        return True

    def _check_barrier(
        self, agent: Agent, scope: str, mark_type: MarkType
    ) -> str | None:
        """Check barrier. Returns error message if blocked, None if allowed."""
        return self._barrier_refusal(
            agent, scope, mark_type, self.get_barrier(agent.id)
        )

    def _barrier_refusal(
        self,
        agent: Agent,
        scope: str,
        mark_type: MarkType,
        barrier: BarrierSnapshot | None,
        now: float | None = None,
    ) -> str | None:
        """
        Why *barrier* blocks *agent* from writing *mark_type* in *scope*, or
        None. It reads the space without the barrier lock: the barrier lock is
        never held while the space is read, so a resolution boundary can hold
        the space while it judges barriers it read before. now: the time of
        the need read, as in read(); a boundary passes its own.
        """
        if barrier is None:
            return None
        if not barrier.is_allowed_checked(scope, mark_type.value):
            return (
                f"Agent '{agent.name}' blocked by barrier: "
                f"{mark_type.value} revoked in scope '{scope}'"
            )
        if barrier.needs_required(scope) and mark_type != MarkType.NEED:
            # Check for unresolved Need in this scope; while the agent is
            # restricted there, its need for the principal counts too.
            needs = self.space.read(scope=scope, mark_type=MarkType.NEED, now=now)
            has_unresolved = any(
                isinstance(n, Need) and n.agent_id == agent.id and n.resolved_by is None
                for n in needs
            ) or (
                self._revoked_to_act(barrier, scope) is not None
                and self._asked_principal(agent, scope)
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
                        scope="_system",
                        topic="envelope-flag",
                        reason=(
                            f"Concentration detected involving agent "
                            f"'{agent.name}' in scope '{scope}'"
                        ),
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
                        scope="_system",
                        invalidates=obs.id,
                        topic="envelope-restriction",
                        reason=(
                            f"Agent '{agent.name}' flagged by statistical "
                            f"envelope in scope '{scope}'"
                        ),
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
                    scope="_system",
                    question=(
                        f"Agent '{agent.name}' restricted: "
                        f"observation/action revoked in '{scope}'"
                    ),
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
        2. Does another agent's action or lease hold the resource?
        3. Does a competing intent win?

        For deferred scopes (Spec Section 6.2): writes intent and returns BLOCKED
        (pending resolution). The agent must wait for resolve_deferred().

        Writes an intent mark if allowed. ALLOW grants it a lease with a
        fencing token (Spec Section 6.5). Returns decision.

        The agent does NOT call this - the harness does, automatically,
        before every tool call that modifies a resource.

        WARNING: When using pre_action()/post_action() instead of execute(),
        the caller is responsible for calling post_action(..., intent_id,
        failed=True) if the tool throws. Without this, the lease holds the
        resource until it expires, blocking other agents. Prefer execute()
        which handles this automatically.

        Thread safety: Per-resource locks serialize the read->write->read->decide
        sequence for each (scope, resource) pair. Different resources proceed
        concurrently. Lock ordering: resource_lock -> space._lock -> the lease
        table's lock.
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

        # Barrier check: an intent exists to act, so a barrier that revokes
        # intents or actions refuses it (Section 9.8). The revocations come
        # first: a need cannot lift them, so the reason must not ask for one.
        barrier = self.get_barrier(agent.id)
        revoked = self._revoked_to_act(barrier, scope)
        barrier_msg = (
            f"Agent '{agent.name}' blocked by barrier: {revoked.value} revoked "
            f"in scope '{scope}'"
            if revoked is not None
            else self._barrier_refusal(agent, scope, MarkType.INTENT, barrier)
        )
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

        # Rate limit check (P64, P65)
        rate_limit_msg = self._check_rate_limit(agent, scope)
        if rate_limit_msg is not None:
            if self.envelope is not None:
                self.envelope.record_attempt(agent.id, MarkType.INTENT)
            return GuardDecision(
                verdict=GuardVerdict.DENIED,
                reason=rate_limit_msg,
            )

        # Check for existing action marks on this resource.
        # If the resource is already claimed (action mark exists from another agent),
        # reject immediately. The intent-vs-intent conflict resolution only applies
        # when two agents are both still in the planning phase.
        # Includes actions in ancestor and descendant scopes (P39).
        existing_actions = self.space.get_actions(scope, resource)
        # Filter out failed actions - they record that a tool threw but
        # don't claim the resource. Without this, a failed tool execution
        # permanently blocks the resource for all agents.
        successful_actions = [
            a for a in existing_actions if not (isinstance(a, Action) and a.failed)
        ]
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
            # Name the claimant only if this agent may see its mark (P22).
            claimant = blocking_actions[0]
            claimed_by = (
                f" (agent_id={claimant.agent_id})"
                if self.space.visible_form(claimant, agent) is not None
                else ""
            )
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                reason=f"Resource '{resource}' already claimed by action mark"
                f"{claimed_by}",
                conflict_found=True,
            )

        # Write this agent's intent and decide under one hold of the space: no
        # write, gc or clock step runs between them (an intent the clock
        # passed in between would be judged dead). Its write hooks fire when
        # the hold ends.
        scope_def = self.space.get_scope(scope)
        need = False
        # Write budget (Section 9.14): the intent is reserved after every
        # check that refuses it, and released if the space refuses its write.
        write_budget_msg, reservation = self._reserve_write(
            agent, scope, MarkType.INTENT, also_spent=MarkType.ACTION
        )
        if write_budget_msg is not None:
            return GuardDecision(verdict=GuardVerdict.DENIED, reason=write_budget_msg)
        # Every barrier, before the space hold: a competitor restricted in its
        # intent's scope does not beat or block this intent (Section 9.8),
        # and no barrier lock is taken under the space lock.
        # shortcut: one snapshot of every barrier per call (about 1.4 ms at
        # 1,000 barriers); read only the competitors' barriers when a
        # deployment holds many barriers.
        barriers = self._all_barriers()
        stored = False
        try:
            with self.space.locked():
                intent_id = self.space.write(
                    agent,
                    Intent(
                        scope=scope,
                        resource=resource,
                        action=action,
                        confidence=confidence,
                    ),
                )
                stored = True
                if scope_def.deferred:
                    # Deferred resolution (Spec Section 6.2): BLOCKED until
                    # resolve_deferred() is called.
                    return GuardDecision(
                        verdict=GuardVerdict.BLOCKED,
                        intent_id=intent_id,
                        reason="Deferred resolution - pending resolution boundary",
                    )
                # Immediate mode (Section 6.5, G3), with one time for marks and
                # one for leases.
                now = self.space.now()
                with self._claim_lock:
                    decision, need = self._decide_immediate(
                        agent, scope, resource, intent_id, now, barriers
                    )
        finally:
            if stored:
                self._confirm_write(reservation)
            else:
                self._release_write(reservation)
        if need and agent.can_write(scope, MarkType.NEED):
            # The Need is informational for the principal; BLOCKED holds
            # without it.
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
        self._emit_lease_events()
        return decision

    def _decide_immediate(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        intent_id: uuid.UUID,
        now: float,
        barriers: dict[uuid.UUID, BarrierSnapshot | None],
    ) -> tuple[GuardDecision, bool]:
        """
        G3 rules 4 to 7 for the intent just written: a competitor that beats
        it, a YIELD_ALL pair, another agent's lease, else a grant. A pending
        intent of an agent restricted in its scope takes no part (Section
        9.8). Returns the decision and whether a Need mark is due. Caller
        MUST hold the resource lock, the space lock and _claim_lock.
        """
        lease_now = self._tick()
        all_intents = self.space.get_intents(scope, resource, now=now)
        own = next((i for i in all_intents if i.id == intent_id), None)
        if own is None:
            raise ValueError(f"Intent {intent_id} not active on {scope}/{resource}")
        # Intents this agent may see (P22): a decision names no intent from a
        # scope the agent cannot read. The verdict still counts every intent.
        others = [
            i for i in all_intents if i.id != intent_id and self._visible_to(i, agent)
        ]
        leases = [
            c
            for c in self._live_leases(scope, resource, lease_now)
            if c.intent.agent_id != agent.id
        ]
        pending_others = [
            o
            for o in all_intents
            if o.agent_id != agent.id
            and o.id != intent_id
            and self._is_pending(o)
            and self._revoked_to_act(barriers.get(o.agent_id), o.scope) is None
        ]
        competed = bool(leases or pending_others)

        def emit(outcome: str) -> None:
            policy = self.space.get_scope(scope).conflict_policy.value
            self._lease_events.append(
                (
                    METRIC_CONFLICTS_RESOLVED,
                    {"scope": scope, "policy": policy, "outcome": outcome},
                )
            )

        # Rule 4: a live lease, or a pending candidate in an ancestor deferred
        # scope (outer scope first among waiting claims), that beats it. A
        # candidate that a held action holds off can never be granted: it is
        # lost and does not count.
        beaters = [c.intent for c in leases if self.space.outranks(c.intent, own)]
        for o in pending_others:
            if (
                o.scope == scope
                or not scope_contains(o.scope, scope)
                or not self.space.get_scope(o.scope).deferred
            ):
                continue
            if self._held_action(o.agent_id, o.scope, resource, now) is not None:
                self._lose(o)
                continue
            if self.space.outranks(o, own):
                beaters.append(o)
        if beaters:
            self._lose(own)
            emit("resolved")
            return (
                self._yield_decision(agent, intent_id, self._first(beaters), others),
                False,
            )

        # Rule 5: a YIELD_ALL pair with another agent's pending intent or
        # lease goes to the principal; the intent stays pending.
        pending_others = [o for o in pending_others if self._is_pending(o)]
        if any(
            self.space.outranks(own, o) is None
            for o in [*pending_others, *(c.intent for c in leases)]
        ):
            emit("yield_all")
            return (
                GuardDecision(
                    verdict=GuardVerdict.BLOCKED,
                    intent_id=intent_id,
                    reason="YIELD_ALL policy - principal decision required",
                    conflicting_intents=others,
                    conflict_found=True,
                ),
                True,
            )

        # Rule 6: a granted lease is not pre-empted.
        if leases:
            self._lose(own)
            emit("resolved")
            return (
                self._yield_decision(agent, intent_id, leases[0].intent, others),
                False,
            )

        # Rule 7: grant.
        claim = self._grant(own, lease_now)
        if competed or len(all_intents) > 1:
            emit("resolved")
        return (
            GuardDecision(
                verdict=GuardVerdict.ALLOW,
                intent_id=intent_id,
                # Name the conflict only if this agent can see a competitor;
                # else the same reason as an agent alone, so nothing hidden
                # shows.
                reason="This agent wins the conflict" if others else "No conflict",
                conflicting_intents=others,
                conflict_found=competed or len(all_intents) > 1,
                fencing_token=claim.token,
                lease_expires_at=claim.expires_at,
            ),
            False,
        )

    def _first(self, beaters: list[Intent]) -> Intent:
        """
        The beater to name: in the outermost scope, the first in that scope's
        order. Beaters lie on one chain of scopes, one scope per depth.
        """
        depth = min(b.scope.count("/") for b in beaters)
        group = [b for b in beaters if b.scope.count("/") == depth]
        policy = self.space.get_scope(group[0].scope).conflict_policy
        winner_id = resolve_conflict(group, policy)
        return next((b for b in group if b.id == winner_id), group[0])

    def _yield_decision(
        self,
        agent: Agent,
        intent_id: uuid.UUID,
        winner: Intent,
        others: list[Intent],
    ) -> GuardDecision:
        """CONFLICT for an intent that yields to *winner*, named only if visible."""
        if not self._visible_to(winner, agent):
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                intent_id=intent_id,
                reason=f"Agent '{agent.name}' yields to an intent in a scope "
                f"it cannot read",
                conflicting_intents=others,
                conflict_found=True,
            )
        return GuardDecision(
            verdict=GuardVerdict.CONFLICT,
            intent_id=intent_id,
            reason=f"Agent '{agent.name}' yields to intent {winner.id} "
            f"(confidence={winner.confidence}, created_at={winner.created_at})",
            winning_intent=winner,
            conflicting_intents=others,
            conflict_found=True,
        )

    def _all_barriers(self) -> dict[uuid.UUID, BarrierSnapshot | None]:
        """Snapshots of every barrier, by agent id."""
        with self._barrier_lock:
            return {a: b.snapshot() for a, b in self._barriers.items()}

    @staticmethod
    def _revoked_to_act(barrier: BarrierSnapshot | None, scope: str) -> MarkType | None:
        """The type, intent or action, that *barrier* revokes in *scope*, or
        None: either one stops an agent from acting there (Section 9.8)."""
        if barrier is None:
            return None
        for mark_type in (MarkType.INTENT, MarkType.ACTION):
            if not barrier.is_allowed_checked(scope, mark_type.value):
                return mark_type
        return None

    def _deferred_refusal(
        self,
        intent: Intent,
        agents: dict[uuid.UUID, Agent] | None,
        barriers: dict[uuid.UUID, BarrierSnapshot | None],
        now: float,
    ) -> str | None:
        """
        Why a deferred intent may not take part at a resolution boundary, or
        None. The agent must not be restricted in the intent's scope (its
        barrier, read by agent id, revokes intents or actions there). With
        the agents mapping, also: the agent must be known, still authorized
        for the intent's own scope, and hold a need mark that its barrier
        requires there.
        """
        revoked = self._revoked_to_act(barriers.get(intent.agent_id), intent.scope)
        if revoked is not None:
            known = agents.get(intent.agent_id) if agents is not None else None
            name = f"'{known.name}'" if known is not None else str(intent.agent_id)
            return (
                f"Agent {name} blocked by barrier at resolution time: "
                f"{revoked.value} revoked in scope '{intent.scope}'"
            )
        if agents is None:
            return None
        agent = agents.get(intent.agent_id)
        if agent is None:
            return "Agent not found at resolution time"
        if not agent.can_write(intent.scope, MarkType.INTENT) or not agent.can_write(
            intent.scope, MarkType.ACTION
        ):
            return f"Agent '{agent.name}' authorization revoked since pre_action"
        refusal = self._barrier_refusal(
            agent, intent.scope, MarkType.INTENT, barriers.get(agent.id), now
        )
        if refusal is not None:
            return f"{refusal} (at resolution time)"
        return None

    def _visible_to(self, mark: AnyMark, agent: Agent | None) -> bool:
        """
        Whether a decision may name *mark* to *agent* (P22, P38). An unknown
        agent (None) may see only marks in OPEN scopes.
        """
        if agent is None:
            return self.space.get_scope(mark.scope).visibility == ScopeVisibility.OPEN
        return self.space.visible_form(mark, agent) is not None

    def resolve_deferred(
        self,
        scope: str,
        resource: str,
        agents: dict[uuid.UUID, Agent] | None = None,
    ) -> dict[uuid.UUID, GuardDecision]:
        """
        Deferred resolution boundary. Collects the active intents on
        (scope, resource), its ancestors and its descendants, and judges each
        against its own competing set (Section 6.1), all at one time. Returns
        a mapping of agent_id -> GuardDecision.

        If an agent has multiple active intents on the same resource, the
        best verdict is kept (ALLOW > BLOCKED > CONFLICT > DENIED).

        agents: optional mapping of agent_id -> Agent for re-verifying
            authorization at resolution time. If provided, agents whose
            permissions have been revoked since pre_action will receive
            DENIED and be excluded from conflict resolution. An agent missing
            from the mapping also receives DENIED, but its intents stay pending
            and still count against the intents they beat: it cannot be
            checked. With or without the mapping, the boundary reads each
            agent's barrier by its id: an intent whose agent is restricted in
            its scope (Section 9.8) receives DENIED and is lost.

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
        # The caller's agents map is copied once. Each agent's barrier is read
        # once, inside the resource lock, so a restriction applied while the
        # boundary waits for that lock counts, and before the space is held:
        # the barrier lock is never held while the space is held (Section
        # 6.2.3). Every barrier is read: an agent missing from the mapping
        # can still be restricted.
        if agents is not None:
            agents = dict(agents)
        with self._resource_lock(scope, resource):
            barriers = self._all_barriers()
            with self.space.locked():
                results = self._resolve_locked(scope, resource, agents, barriers)
        self._emit_lease_events()
        return results

    def _resolve_locked(
        self,
        scope: str,
        resource: str,
        agents: dict[uuid.UUID, Agent] | None,
        barriers: dict[uuid.UUID, BarrierSnapshot | None],
    ) -> dict[uuid.UUID, GuardDecision]:
        """
        resolve_deferred() under its resource lock and the space lock. The
        boundary is atomic: no write, gc or read by another thread runs until
        it returns, so every competing set it reads shows one state.
        """
        # One time for every read of marks, one for leases: an intent that
        # expires during the boundary must not count in one competing set and
        # not in another.
        now = self.space.now()
        all_intents = self.space.get_intents(scope, resource, now=now)

        if not all_intents:
            return {}

        with self._claim_lock:
            lease_now = self._tick()
            return self._judge_boundary(
                resource, agents, barriers, now, lease_now, all_intents
            )

    def _judge_boundary(
        self,
        resource: str,
        agents: dict[uuid.UUID, Agent] | None,
        barriers: dict[uuid.UUID, BarrierSnapshot | None],
        now: float,
        lease_now: float,
        all_intents: list[Intent],
    ) -> dict[uuid.UUID, GuardDecision]:
        """
        G4 (Section 6.5) on the boundary's candidates: the pending intents in
        its scope, its ancestors and its descendants. Caller MUST hold the
        resource lock, the space lock and _claim_lock.
        """
        results: dict[uuid.UUID, GuardDecision] = {}

        # Each intent is checked once per boundary: a barrier or a
        # permission that changes during the boundary must not make one
        # intent eligible to win and absent from other intents' sets.
        refusals: dict[uuid.UUID, str | None] = {}

        def refusal_of(o: Intent) -> str | None:
            if o.id not in refusals:
                refusals[o.id] = self._deferred_refusal(o, agents, barriers, now)
            return refusals[o.id]

        # An intent whose agent fails a check takes no part in any
        # comparison (P14), also outside this boundary's set. An agent
        # missing from the mapping cannot be checked, so its intents still
        # count, and stay pending, unless its barrier restricts it there.
        def is_refused(o: Intent) -> bool:
            return refusal_of(o) is not None and (
                agents is None
                or o.agent_id in agents
                or self._revoked_to_act(barriers.get(o.agent_id), o.scope) is not None
            )

        candidates: list[Intent] = []
        for intent in all_intents:
            if not self._is_pending(intent):
                continue
            # An intent pending in an immediate scope went to the principal
            # (G3 rule 5): it counts here and only the principal grants it,
            # but an agent that fails a check loses it here too.
            deferred = self.space.get_scope(intent.scope).deferred
            refusal = refusal_of(intent)
            if refusal is None:
                if deferred:
                    candidates.append(intent)
                continue
            if is_refused(intent):
                self._lose(intent)
            elif not deferred:
                continue  # its agent cannot be checked: it keeps waiting
            _update_best_decision(
                results,
                intent.agent_id,
                GuardDecision(
                    verdict=GuardVerdict.DENIED,
                    intent_id=intent.id,
                    reason=refusal,
                ),
            )

        if not candidates:
            return results

        # Each candidate's competing set, read once per scope: every intent
        # (for the names a decision lists), and the pending ones that take
        # part in judging. A pending intent that a held action holds off can
        # never be granted: it is lost and takes no part.
        competing: dict[str, list[Intent]] = {}
        held: dict[tuple[uuid.UUID, str], Action | None] = {}

        def held_off(o: Intent) -> Action | None:
            key = (o.agent_id, o.scope)
            if key not in held:
                held[key] = self._held_action(o.agent_id, o.scope, resource, now)
            return held[key]

        tree: dict[uuid.UUID, Intent] = {}
        for intent in candidates:
            if intent.scope in competing:
                continue
            competing[intent.scope] = [
                o
                for o in self.space.get_intents(intent.scope, resource, now=now)
                if not is_refused(o)
            ]
            for o in competing[intent.scope]:
                if o.id in tree or not self._is_pending(o):
                    continue
                if held_off(o) is not None:
                    self._lose(o)
                    continue
                tree[o.id] = o
        judged = self.space.judge_all(list(tree.values()))

        # Classify: held off by an action, beaten, to the principal, waiting
        # behind a lease, or a winner.
        leases_of: dict[str, list[_Claim]] = {}

        def leases_for(o: Intent) -> list[_Claim]:
            if o.scope not in leases_of:
                leases_of[o.scope] = self._live_leases(o.scope, resource, lease_now)
            return [c for c in leases_of[o.scope] if c.intent.agent_id != o.agent_id]

        winners: dict[uuid.UUID, list[Intent]] = {}
        for intent in candidates:
            if intent.id in tree and judged[intent.id].wins and not leases_for(intent):
                winners.setdefault(intent.agent_id, []).append(intent)
        granted: dict[uuid.UUID, _Claim] = {}
        for agent_id, own_winners in winners.items():
            # One grant per agent per boundary: the outermost, then the earliest.
            first = min(
                own_winners,
                key=lambda i: (i.scope.count("/"), i.created_at, str(i.id)),
            )
            granted[first.id] = self._grant(first, lease_now)

        # A decision names the other intents its receiver may see: what a
        # receiver may see depends only on the mark's scope, so each
        # (receiver, scope) is checked once, and each scope's intents are
        # listed once and sliced around the intent itself.
        visible: dict[tuple[uuid.UUID | None, str], bool] = {}
        listed: dict[str, dict[str, list[Intent]]] = {}
        # An intent's index in each listing: the listings of two scopes
        # can order the same intents differently.
        position: dict[tuple[str, uuid.UUID], int] = {}
        for intent in candidates:
            # Each decision names only intents its receiver may see (P22,
            # P38). Without the agents mapping the receiver is unknown,
            # and only intents in OPEN scopes are named.
            receiver = agents.get(intent.agent_id) if agents is not None else None
            own_set = competing[intent.scope]
            if intent.scope not in listed:
                listed[intent.scope] = {}
                for o in own_set:
                    group = listed[intent.scope].setdefault(o.scope, [])
                    position[(intent.scope, o.id)] = len(group)
                    group.append(o)
            visible_others: list[Intent] = []
            for sc, group in listed[intent.scope].items():
                key = (receiver.id if receiver is not None else None, sc)
                if key not in visible:
                    visible[key] = self._visible_to(group[0], receiver)
                if not visible[key]:
                    continue
                if sc == intent.scope:
                    k = position[(intent.scope, intent.id)]
                    visible_others += group[:k] + group[k + 1 :]
                else:
                    visible_others += group
            has_others = len(own_set) > 1
            claimant = held_off(intent)
            if claimant is not None:
                # P39 at the boundary: a completed action holds the resource.
                claimed_by = (
                    f" (agent_id={claimant.agent_id})"
                    if self._visible_to(claimant, receiver)
                    else ""
                )
                decision = GuardDecision(
                    verdict=GuardVerdict.CONFLICT,
                    intent_id=intent.id,
                    reason=f"Deferred resolution - resource already claimed "
                    f"by action mark{claimed_by}",
                    conflicting_intents=visible_others,
                    conflict_found=True,
                )
                _update_best_decision(results, intent.agent_id, decision)
                continue
            judgement = judged[intent.id]
            winner = judgement.winner
            if intent.id in granted:
                claim = granted[intent.id]
                decision = GuardDecision(
                    verdict=GuardVerdict.ALLOW,
                    intent_id=intent.id,
                    reason="Deferred resolution - this agent wins",
                    conflicting_intents=visible_others,
                    conflict_found=has_others,
                    fencing_token=claim.token,
                    lease_expires_at=claim.expires_at,
                )
            elif winner is not None and winner.id in granted:
                # Beaten by a candidate granted now: lost.
                self._lose(intent)
                if self._visible_to(winner, receiver):
                    decision = GuardDecision(
                        verdict=GuardVerdict.CONFLICT,
                        intent_id=intent.id,
                        reason=f"Deferred resolution - yields to agent "
                        f"'{winner.agent_id}' (confidence={winner.confidence})",
                        winning_intent=winner,
                        conflicting_intents=visible_others,
                        conflict_found=True,
                    )
                else:
                    decision = GuardDecision(
                        verdict=GuardVerdict.CONFLICT,
                        intent_id=intent.id,
                        reason="Deferred resolution - yields to an intent in a "
                        "scope this agent cannot read",
                        conflicting_intents=visible_others,
                        conflict_found=True,
                    )
            elif judgement.yield_all:
                # YIELD_ALL - the principal decides; no mark is written here.
                decision = GuardDecision(
                    verdict=GuardVerdict.BLOCKED,
                    intent_id=intent.id,
                    reason="YIELD_ALL - principal decision required",
                    conflicting_intents=visible_others,
                    conflict_found=has_others,
                )
            else:
                # Waits: behind a waiting beater, a live lease, or its own
                # agent's grant at this boundary. It stays pending.
                decision = GuardDecision(
                    verdict=GuardVerdict.BLOCKED,
                    intent_id=intent.id,
                    reason="Deferred resolution - waits for a claim that holds "
                    "or precedes it",
                    conflicting_intents=visible_others,
                    conflict_found=True,
                )
            _update_best_decision(results, intent.agent_id, decision)

        return results

    # ------------------------------------------------------------------
    # Principal grant and release (Section 6.5, G5)
    # ------------------------------------------------------------------

    def grant(self, intent_id: uuid.UUID, principal_token: uuid.UUID) -> GuardDecision:
        """
        The principal grants a pending intent whose mark is live, if no other
        agent holds the resource in a competing scope (G1): ALLOW with the
        token and expiry, or CONFLICT naming the holder. A wrong principal
        token gets DENIED, and so does an intent of an agent restricted in
        its scope (Section 9.8: the principal restores the barrier first);
        an intent that is not pending gets CONFLICT.
        """
        if principal_token != self._principal_token:
            return GuardDecision(
                verdict=GuardVerdict.DENIED, reason="Wrong principal token"
            )
        mark = self.space.get_mark(intent_id)
        if not isinstance(mark, Intent):
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                intent_id=intent_id,
                reason="No live intent with this id",
            )
        with self._resource_lock(mark.scope, mark.resource):
            # A lease needs the barrier's leave to act (Section 9.8); read
            # before the space lock, as pre_action reads it.
            revoked = self._revoked_to_act(self.get_barrier(mark.agent_id), mark.scope)
            if revoked is not None:
                return GuardDecision(
                    verdict=GuardVerdict.DENIED,
                    intent_id=intent_id,
                    reason=f"Barrier revokes {revoked.value} in scope "
                    f"'{mark.scope}'; restore it first",
                )
            with self.space.locked():
                now = self.space.now()
                live = any(
                    i.id == intent_id
                    for i in self.space.get_intents(mark.scope, mark.resource, now=now)
                )
                with self._claim_lock:
                    lease_now = self._tick()
                    decision = self._grant_checked(mark, live, now, lease_now)
        self._emit_lease_events()
        return decision

    def _grant_checked(
        self, intent: Intent, live: bool, now: float, lease_now: float
    ) -> GuardDecision:
        """grant() under its locks. Caller MUST hold all three."""
        if not live or not self._is_pending(intent):
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                intent_id=intent.id,
                reason="The intent is not pending",
            )
        action = self._held_action(intent.agent_id, intent.scope, intent.resource, now)
        if action is not None:
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                intent_id=intent.id,
                reason=f"Resource '{intent.resource}' already claimed by action "
                f"mark {action.id}",
                conflict_found=True,
            )
        leases = [
            c
            for c in self._live_leases(intent.scope, intent.resource, lease_now)
            if c.intent.agent_id != intent.agent_id
        ]
        if leases:
            return GuardDecision(
                verdict=GuardVerdict.CONFLICT,
                intent_id=intent.id,
                reason=f"Intent {leases[0].intent.id} holds a lease on "
                f"'{intent.resource}'",
                winning_intent=leases[0].intent,
                conflict_found=True,
            )
        claim = self._grant(intent, lease_now)
        return GuardDecision(
            verdict=GuardVerdict.ALLOW,
            intent_id=intent.id,
            reason="Granted by the principal",
            fencing_token=claim.token,
            lease_expires_at=claim.expires_at,
        )

    def release(self, intent_id: uuid.UUID, principal_token: uuid.UUID) -> bool:
        """
        The principal ends a lease or closes a pending intent (released).
        Returns False for a wrong token, an unknown intent or a closed one.
        """
        if principal_token != self._principal_token:
            return False
        with self._claim_lock:
            claim = self._claims.get(intent_id)
        if claim is not None:
            scope, resource = claim.intent.scope, claim.intent.resource
        else:
            mark = self.space.get_mark(intent_id)
            if not isinstance(mark, Intent):
                return False
            scope, resource = mark.scope, mark.resource
        try:
            with self._resource_lock(scope, resource):
                with self.space.locked():
                    with self._claim_lock:
                        claim = self._claims.get(intent_id)
                        if claim is None:
                            mark = self.space.get_mark(intent_id)
                            if not isinstance(mark, Intent):
                                return False
                            self._store(_Claim(intent=mark, state=ClaimState.RELEASED))
                            return True
                        if not self._live(claim, self._tick()):
                            return False  # closed, or found expired just now
                        self._end(claim, ClaimState.RELEASED)
                        return True
        finally:
            self._emit_lease_events()

    def post_action(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        action: str,
        result: Any,
        intent_id: uuid.UUID | None = None,
        failed: bool = False,
    ) -> uuid.UUID:
        """
        Called after tool execution. Writes an action mark that supersedes
        the intent, only under a live grant (Section 6.5, G6):

        - failed=True: writes the failed action, which holds nothing, and
          ends the lease if it is live: intent_id's, when that intent is
          this agent's own on this scope and resource, or without intent_id
          this agent's live lease on this scope and resource. It never
          raises for a lease.
        - With intent_id: only if that intent holds a live lease of this
          agent, scope and resource, and no other agent holds the resource
          (G1; a warning on another agent's action may have decayed).
        - Without intent_id: under this agent's live lease on this scope and
          resource if it has one; else a direct claim, only in an immediate
          scope that is not YIELD_ALL, when no other agent holds the
          resource and no pending intent of another agent that may act
          competes. A direct claim that the agent's barrier refuses is
          recorded, with a need mark for the principal, one open per agent
          and registered scope (Section 9.8).

        Otherwise raises NotGrantedError and writes nothing.

        Each action it records counts against the agent's write budget
        (Section 9.14); it records an action also over the budget, because
        its tool has run.

        The agent does NOT call this - the harness does, automatically,
        after every tool call. execute() calls it with failed=True when the
        tool throws.
        """
        reservation = None
        if agent.can_write(scope, MarkType.ACTION):
            _, reservation = self._reserve_write(
                agent, scope, MarkType.ACTION, force=True
            )
        try:
            action_id = self._record_action(
                agent, scope, resource, action, result, intent_id, failed
            )
        except BaseException:
            self._release_write(reservation)
            raise
        self._confirm_write(reservation)
        return action_id

    def _record_action(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        action: str,
        result: Any,
        intent_id: uuid.UUID | None,
        failed: bool,
    ) -> uuid.UUID:
        """post_action without the write budget (Section 9.14)."""
        try:
            with self._resource_lock(scope, resource):
                # A direct claim that the barrier refuses is still recorded,
                # because its tool has run, and the principal gets a need
                # mark (Section 9.8). Read before the space lock, as
                # pre_action reads it.
                breach = (
                    None
                    if failed or intent_id is not None
                    else self._direct_claim_refusal(agent, scope)
                )
                # A competitor restricted in its intent's scope does not
                # block a direct claim (Section 9.8).
                barriers = (
                    self._all_barriers() if intent_id is None and not failed else {}
                )
                with self.space.locked():
                    now = self.space.now()
                    with self._claim_lock:
                        lease_now = self._tick()
                        if not failed:
                            supersedes = self._covering_intent(
                                agent,
                                scope,
                                resource,
                                intent_id,
                                now,
                                lease_now,
                                barriers,
                            )
                        elif intent_id is not None:
                            # Only this agent's intent on this scope and
                            # resource: a failed action must not hide another
                            # mark of the agent.
                            target = self.space.get_mark(intent_id)
                            supersedes = (
                                intent_id
                                if isinstance(target, Intent)
                                and target.agent_id == agent.id
                                and target.scope == scope
                                and target.resource == resource
                                else None
                            )
                        else:
                            supersedes = self._own_lease(
                                agent, scope, resource, lease_now
                            )
                    action_id = self.space.write(
                        agent,
                        Action(
                            scope=scope,
                            resource=resource,
                            action=action,
                            result=result,
                            failed=failed,
                            supersedes=supersedes,
                        ),
                    )
            if breach is not None and supersedes is None:
                self._report_breach(agent, scope, resource, action_id, breach)
            return action_id
        finally:
            # Also when it raises: an expiry it recorded is reported.
            self._emit_lease_events()

    def _direct_claim_refusal(self, agent: Agent, scope: str) -> str | None:
        """Why the agent's barrier refuses a direct claim in *scope*: the
        agent is restricted there (Section 9.8), or it lacks a need mark
        that the barrier requires; None if the barrier allows it."""
        barrier = self.get_barrier(agent.id)
        revoked = self._revoked_to_act(barrier, scope)
        if revoked is not None:
            return (
                f"Agent '{agent.name}' blocked by barrier: {revoked.value} revoked "
                f"in scope '{scope}'"
            )
        return self._barrier_refusal(agent, scope, MarkType.ACTION, barrier)

    def _report_breach(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        action_id: uuid.UUID,
        reason: str,
    ) -> None:
        """A need mark for the principal: a direct claim went against the
        agent's barrier (Section 9.8). One open per agent and registered
        scope (a scope counts as the nearest registered scope that contains
        it); the actions of
        later breaches are recorded and the open need covers them."""
        self._write_principal_need(
            ("barrier_breach", agent.id, self.space.get_scope(scope).name),
            (
                f"Agent '{agent.name}' acted on '{resource}' in '{scope}' "
                f"against its barrier: {reason}"
            ),
            {
                "agent_id": str(agent.id),
                "trigger": "barrier_breach",
                "action_id": str(action_id),
                "scope": scope,
                "resource": resource,
            },
            priority=0.9,
        )

    def _own_lease(
        self, agent: Agent, scope: str, resource: str, lease_now: float
    ) -> uuid.UUID | None:
        """
        The intent of this agent's live lease on this scope and resource, if
        it holds one. Caller MUST hold _claim_lock.
        """
        own = [
            c
            for c in self._live_leases(scope, resource, lease_now)
            if c.intent.agent_id == agent.id and c.intent.scope == scope
        ]
        return own[0].intent.id if own else None

    def _covering_intent(
        self,
        agent: Agent,
        scope: str,
        resource: str,
        intent_id: uuid.UUID | None,
        now: float,
        lease_now: float,
        barriers: dict[uuid.UUID, BarrierSnapshot | None],
    ) -> uuid.UUID | None:
        """
        The intent whose lease covers this action, None for an allowed direct
        claim; raises NotGrantedError otherwise. A pending intent of an agent
        restricted in its scope does not compete with a direct claim (Section
        9.8). Caller MUST hold the resource lock, the space lock and
        _claim_lock.
        """
        if intent_id is None:
            intent_id = self._own_lease(agent, scope, resource, lease_now)
        holder = self._held_action(agent.id, scope, resource, now)
        other_leases = [
            c
            for c in self._live_leases(scope, resource, lease_now)
            if c.intent.agent_id != agent.id
        ]
        if intent_id is not None:
            claim = self._claims.get(intent_id)
            if claim is None or not self._live(claim, lease_now):
                raise NotGrantedError(f"Intent {intent_id} holds no live lease")
            if (
                claim.intent.agent_id != agent.id
                or claim.intent.scope != scope
                or claim.intent.resource != resource
            ):
                raise NotGrantedError(
                    f"The lease of intent {intent_id} is not this agent's on "
                    f"{scope}/{resource}"
                )
            if holder is not None or other_leases:
                raise NotGrantedError(
                    f"Another agent holds '{resource}' in a competing scope"
                )
            return intent_id
        scope_def = self.space.get_scope(scope)
        if scope_def.deferred or scope_def.conflict_policy == ConflictPolicy.YIELD_ALL:
            raise NotGrantedError(
                f"A direct claim needs a grant in scope '{scope}' (deferred or "
                f"YIELD_ALL)"
            )
        if holder is not None or other_leases:
            raise NotGrantedError(
                f"Another agent holds '{resource}' in a competing scope"
            )
        if any(
            o.agent_id != agent.id
            and self._is_pending(o)
            and self._revoked_to_act(barriers.get(o.agent_id), o.scope) is None
            for o in self.space.get_intents(scope, resource, now=now)
        ):
            raise NotGrantedError(
                f"Another agent's pending intent competes for '{resource}'"
            )
        return None

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
        blocking other agents on the same resource. The lease granted by
        pre_action holds the resource while the lock is released, and
        post_action records the action only under it (Spec Section 6.5).

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
        # Whether the tool takes the fencing token, read before a lease is
        # granted: a callable with no readable signature takes none.
        try:
            takes_token = "fencing_token" in inspect.signature(tool_fn).parameters
        except (TypeError, ValueError):
            takes_token = False

        # Phase 1: pre_action under resource lock (declare intent, check conflicts)
        with self._resource_lock(scope, resource):
            decision = self._pre_action_inner(
                agent, scope, resource, intent_action, confidence
            )

        # Telemetry for pre_action decision (P58)
        self._emit_telemetry(
            agent,
            "execute",
            scope,
            intent_action,
            decision.verdict.value,
            conflict_check=True,
            conflict_found=decision.conflict_found,
            reason=decision.reason,
        )

        if decision.verdict != GuardVerdict.ALLOW:
            return decision, None

        intent_id = decision.intent_id

        # Phase 2: execute tool WITHOUT holding the resource lock. The lease
        # holds the resource; the tool gets its fencing token when it takes a
        # parameter named fencing_token (Section 6.5, G8).
        if takes_token:
            tool_kwargs = {**tool_kwargs, "fencing_token": decision.fencing_token}
        try:
            result = tool_fn(*tool_args, **tool_kwargs)
        except Exception as exc:
            # A failed action ends the lease and holds nothing.
            self.post_action(
                agent,
                scope,
                resource,
                result_action,
                {"error": str(exc), "status": "failed"},
                intent_id,
                failed=True,
            )
            raise

        # Phase 3: record the action, only under the live lease (G6).
        try:
            self.post_action(agent, scope, resource, result_action, result, intent_id)
        except NotGrantedError as exc:
            # The lease was lost while the tool ran: a failed action is the
            # trace for readers and the principal, and the agent yields. The
            # refused post_action released its count, so this one counts the
            # tool run.
            self.post_action(
                agent,
                scope,
                resource,
                result_action,
                {"error": "lease lost", "status": "failed", "detail": str(exc)},
                intent_id,
                failed=True,
            )
            return (
                GuardDecision(
                    verdict=GuardVerdict.CONFLICT,
                    intent_id=intent_id,
                    reason="lease lost",
                ),
                None,
            )

        # Telemetry for completed execution
        self._emit_write_metric(agent, scope, "action", "accepted")

        return decision, result
