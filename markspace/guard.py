# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Guard

The guard is a DETERMINISTIC enforcement layer that wraps tool execution.
It reads marks and enforces coordination constraints mechanically.
The agent does not need to "remember" to check marks — the guard does it.

This is the critical architectural insight: marks are WRITTEN by agents
(voluntary, through LLM reasoning) but ENFORCED by the guard (deterministic,
wrapping every tool call). Coordination reliability does not depend on the
LLM being reliable.

    BEFORE (unreliable):
        Agent reasons → agent reads marks → agent decides → agent calls tool
                         (may forget)       (may ignore)

    AFTER (deterministic):
        Agent reasons → agent calls tool → GUARD checks marks → tool executes
                                            (deterministic)
        → GUARD writes action mark

Spec Section 9.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from markspace.core import (
    Action,
    Agent,
    AnyMark,
    ConflictPolicy,
    Intent,
    MarkType,
    Need,
    resolve_conflict,
)
from markspace.space import MarkSpace, ScopeError


class GuardVerdict(str, Enum):
    """Result of a guard check."""

    ALLOW = "allow"  # No conflict. Proceed.
    CONFLICT = "conflict"  # Another agent has priority. Yield.
    BLOCKED = "blocked"  # YIELD_ALL policy — need principal input.
    DENIED = "denied"  # Agent not authorized for this scope/action.


@dataclass
class GuardDecision:
    """
    The guard's deterministic decision about whether an action can proceed.

    The agent receives this. It doesn't choose whether to follow it —
    the harness enforces it. The decision is informational for the agent's
    reasoning (e.g., "I was blocked because agent X has higher confidence
    on this resource, I'll try a different resource").
    """

    verdict: GuardVerdict
    reason: str
    winning_intent: Intent | None = None
    conflicting_intents: list[Intent] = field(default_factory=list)


# Type for the actual tool function the guard wraps
ToolFn = Callable[..., Any]


class Guard:
    """
    Deterministic enforcement layer for mark-based coordination.

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

    def __init__(self, space: MarkSpace, block_self_rebook: bool = False) -> None:
        self.space = space
        self.block_self_rebook = block_self_rebook
        self._lock = threading.RLock()

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

        The agent does NOT call this — the harness does, automatically,
        before every tool call that modifies a resource.

        Thread safety: The guard lock serializes the entire read→write→read→decide
        sequence. Without it, two concurrent pre_action calls can interleave and
        miss each other's intents. Lock ordering: guard._lock → space._lock.
        """
        with self._lock:
            # Check authorization
            if not agent.can_write(scope, MarkType.INTENT):
                return GuardDecision(
                    verdict=GuardVerdict.DENIED,
                    reason=f"Agent '{agent.name}' not authorized for intent in scope '{scope}'",
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
            if self.block_self_rebook:
                blocking_actions = existing_actions
            else:
                blocking_actions = [
                    a for a in existing_actions if a.agent_id != agent.id
                ]
            if blocking_actions:
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
            scope_def = self.space._get_scope(scope)
            if scope_def.deferred:
                return GuardDecision(
                    verdict=GuardVerdict.BLOCKED,
                    reason="Deferred resolution — pending resolution boundary",
                )

            # Check for conflicts with other intents (concurrent planning phase)
            all_intents = self.space.get_intents(scope, resource)
            if len(all_intents) <= 1:
                # No conflict — this agent is the only one
                return GuardDecision(verdict=GuardVerdict.ALLOW, reason="No conflict")

            # Resolve conflict
            winner_id = resolve_conflict(all_intents, scope_def.conflict_policy)

            if winner_id is None:
                # YIELD_ALL — everyone must wait for principal
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
                    reason="YIELD_ALL policy — principal decision required",
                    conflicting_intents=[i for i in all_intents if i.id != intent_id],
                )

            if winner_id == intent_id:
                return GuardDecision(
                    verdict=GuardVerdict.ALLOW,
                    reason="This agent wins the conflict",
                    conflicting_intents=[i for i in all_intents if i.id != intent_id],
                )
            else:
                winner = next(i for i in all_intents if i.id == winner_id)
                return GuardDecision(
                    verdict=GuardVerdict.CONFLICT,
                    reason=f"Agent '{agent.name}' yields to intent {winner_id} "
                    f"(confidence={winner.confidence}, created_at={winner.created_at})",
                    winning_intent=winner,
                    conflicting_intents=[i for i in all_intents if i.id != intent_id],
                )

    def resolve_deferred(
        self,
        scope: str,
        resource: str,
    ) -> dict[uuid.UUID, GuardDecision]:
        """
        Deferred resolution boundary. Collects all active intents on
        (scope, resource) and applies the scope's conflict policy to the
        full set. Returns a mapping of agent_id → GuardDecision.

        Spec Section 6.2 (Phase 3: Batch resolution).

        P30: MUST consider ALL active intents at the resolution boundary.
        P31: Winner MUST be identical to simultaneous HIGHEST_CONFIDENCE evaluation.
        P32: Caller is responsible for triggering this method (liveness).
        """
        with self._lock:
            scope_def = self.space._get_scope(scope)
            all_intents = self.space.get_intents(scope, resource)

            if not all_intents:
                return {}

            # P30: Consider ALL active intents (get_intents already filters
            # by strength > 0 and respects TTL/supersession).
            winner_id = resolve_conflict(all_intents, scope_def.conflict_policy)

            results: dict[uuid.UUID, GuardDecision] = {}
            for intent in all_intents:
                if winner_id is not None and intent.id == winner_id:
                    results[intent.agent_id] = GuardDecision(
                        verdict=GuardVerdict.ALLOW,
                        reason="Deferred resolution — this agent wins",
                        conflicting_intents=[
                            i for i in all_intents if i.id != intent.id
                        ],
                    )
                elif winner_id is None:
                    # YIELD_ALL — all blocked
                    results[intent.agent_id] = GuardDecision(
                        verdict=GuardVerdict.BLOCKED,
                        reason="YIELD_ALL — principal decision required",
                        conflicting_intents=[
                            i for i in all_intents if i.id != intent.id
                        ],
                    )
                else:
                    winner = next(i for i in all_intents if i.id == winner_id)
                    results[intent.agent_id] = GuardDecision(
                        verdict=GuardVerdict.CONFLICT,
                        reason=f"Deferred resolution — yields to agent "
                        f"'{winner.agent_id}' (confidence={winner.confidence})",
                        winning_intent=winner,
                        conflicting_intents=[
                            i for i in all_intents if i.id != intent.id
                        ],
                    )

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

        The agent does NOT call this — the harness does, automatically,
        after every successful tool call.
        """
        return self.space.write(
            agent,
            Action(
                scope=scope,
                resource=resource,
                action=action,
                result=result,
                supersedes=intent_id,
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
        Full guarded execution: pre_action → tool_fn → post_action.
        Returns (decision, result). If decision is not ALLOW, tool_fn is never called.

        This is the primary API for the harness. One call wraps the entire
        check-execute-record cycle.

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

        # Hold the lock across the entire pre_action → tool_fn → post_action
        # cycle. Without this, two concurrent execute() calls can both pass
        # pre_action (no action marks yet) before either writes its action mark,
        # causing double bookings. The RLock allows pre_action to re-enter.
        with self._lock:
            decision = self.pre_action(
                agent, scope, resource, intent_action, confidence
            )

            if decision.verdict != GuardVerdict.ALLOW:
                return decision, None

            # Execute the tool
            result = tool_fn(*tool_args, **tool_kwargs)

            # Find the intent we wrote in pre_action
            intents = self.space.get_intents(scope, resource)
            our_intent = next(
                (
                    i
                    for i in intents
                    if i.agent_id == agent.id and i.action == intent_action
                ),
                None,
            )
            intent_id = our_intent.id if our_intent else None

            # Record the action
            self.post_action(agent, scope, resource, result_action, result, intent_id)

            return decision, result
