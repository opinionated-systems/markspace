# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Core Algebra

This module defines the fundamental types and pure functions of the protocol.
Everything here is stateless. The MarkSpace (space.py) provides statefulness.

Types:  MarkType, Source, ConflictPolicy, DecayConfig, Scope, Agent,
        Mark, Intent, Action, Observation, Warning, Need
Functions: compute_strength, trust_weight, effective_strength,
           effective_strength_with_warnings, reinforce, resolve_conflict
"""

from __future__ import annotations

import math
import uuid
from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def hours(n: float) -> float:
    """Convert hours to seconds."""
    return n * 3600.0


def minutes(n: float) -> float:
    """Convert minutes to seconds."""
    return n * 60.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MarkType(str, Enum):
    INTENT = "intent"
    ACTION = "action"
    OBSERVATION = "observation"
    WARNING = "warning"
    NEED = "need"


class Source(str, Enum):
    """Trust source levels, in strict total order: FLEET > EXTERNAL_VERIFIED > EXTERNAL_UNVERIFIED."""

    FLEET = "fleet"
    EXTERNAL_VERIFIED = "external_verified"
    EXTERNAL_UNVERIFIED = "external_unverified"


class ConflictPolicy(str, Enum):
    FIRST_WRITER = "first_writer"
    HIGHEST_CONFIDENCE = "highest_confidence"
    YIELD_ALL = "yield_all"


class ScopeVisibility(str, Enum):
    """Controls who can read marks in a scope.

    OPEN:       Any agent reads full marks. Default. (Original P15 behavior.)
    PROTECTED:  Any agent sees structural/coordination metadata. Content fields
                are redacted unless the agent has explicit read authorization.
    CLASSIFIED: No reads without explicit read authorization. Not even projected.
    """

    OPEN = "open"
    PROTECTED = "protected"
    CLASSIFIED = "classified"


class Severity(str, Enum):
    INFO = "info"
    CAUTION = "caution"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class DecayConfig(BaseModel):
    """Decay parameters for a scope. All values in seconds."""

    model_config = ConfigDict(frozen=True)

    observation_half_life: float = Field(gt=0)
    warning_half_life: float = Field(gt=0)
    intent_ttl: float = Field(gt=0)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class Scope(BaseModel):
    """
    A namespace that defines what marks can exist, how they decay, and how
    conflicts are resolved.

    Spec Section 7.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    visibility: ScopeVisibility = ScopeVisibility.OPEN
    intent_actions: tuple[str, ...] = ()
    action_actions: tuple[str, ...] = ()
    observation_topics: tuple[str, ...] = ("*",)
    warning_topics: tuple[str, ...] = ("*",)
    decay: DecayConfig = DecayConfig(
        observation_half_life=hours(6),
        warning_half_life=hours(2),
        intent_ttl=minutes(30),
    )
    conflict_policy: ConflictPolicy = ConflictPolicy.HIGHEST_CONFIDENCE
    deferred: bool = False  # Spec Section 6.2: deferred resolution mode

    def allows_intent_action(self, action: str) -> bool:
        return action in self.intent_actions

    def allows_action_action(self, action: str) -> bool:
        return action in self.action_actions

    def allows_observation_topic(self, topic: str) -> bool:
        return "*" in self.observation_topics or topic in self.observation_topics

    def allows_warning_topic(self, topic: str) -> bool:
        return "*" in self.warning_topics or topic in self.warning_topics


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent(BaseModel):
    """
    An identity with scope permissions.

    scopes: mapping of scope_name → list of writable MarkType names.
    read_scopes: set of scope names with full content read access.
    Both are hierarchical: permission for "a" implies permission for "a/b".

    Spec Section 11.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    scopes: dict[str, list[str]] = Field(default_factory=dict)
    read_scopes: frozenset[str] = Field(default_factory=frozenset)

    manifest: AgentManifest | None = None  # optional composition contract (Section 14)

    def can_write(self, scope_name: str, mark_type: MarkType) -> bool:
        """Check if this agent is authorized to write mark_type in scope_name."""
        for authorized_scope, allowed_types in self.scopes.items():
            if scope_name == authorized_scope or scope_name.startswith(
                authorized_scope + "/"
            ):
                if mark_type.value in allowed_types:
                    return True
        return False

    def can_read_content(self, scope_name: str) -> bool:
        """Check if this agent has full content read access for a scope.

        For OPEN scopes, this doesn't matter - everyone sees everything.
        For PROTECTED scopes, this controls content vs projected reads.
        For CLASSIFIED scopes, this controls any read access at all.
        Hierarchical: read access for "hr" implies access for "hr/compensation".
        """
        for authorized_scope in self.read_scopes:
            if scope_name == authorized_scope or scope_name.startswith(
                authorized_scope + "/"
            ):
                return True
        return False


# ---------------------------------------------------------------------------
# Watch patterns and agent manifests (composition)
# ---------------------------------------------------------------------------


class WatchPattern(BaseModel):
    """
    A pattern describing marks an agent is interested in.
    Used for subscription-based reactive activation.

    Spec Section 14.1.
    P42: matches() is a pure function with no side effects.
    """

    model_config = ConfigDict(frozen=True)

    scope: str  # required - which scope to watch (hierarchical matching)
    mark_type: MarkType | None = None  # optional - filter by mark type
    topic: str | None = None  # optional - filter observations/warnings by topic
    resource: str | None = None  # optional - filter intents/actions by resource

    def matches(self, mark: AnyMark) -> bool:
        """Check if a mark matches this pattern. Pure function (P42)."""
        # Scope: exact or hierarchical (consistent with P18)
        if mark.scope != self.scope and not mark.scope.startswith(self.scope + "/"):
            return False
        if self.mark_type is not None and mark.mark_type != self.mark_type:
            return False
        if self.topic is not None:
            if not hasattr(mark, "topic") or getattr(mark, "topic") != self.topic:
                return False
        if self.resource is not None:
            if not hasattr(mark, "resource") or getattr(mark, "resource") != self.resource:
                return False
        return True


class AgentManifest(BaseModel):
    """
    Declared input/output contract for an agent.

    Makes agent interfaces explicit, enabling composition validation.
    inputs:  WatchPatterns describing marks this agent reads.
    outputs: (scope, MarkType) pairs this agent writes.
    schedule_interval: seconds between activations (set by principal).
                       None means the agent is not scheduled.

    Spec Sections 13.2 and 14.
    """

    model_config = ConfigDict(frozen=True)

    inputs: tuple[WatchPattern, ...] = ()
    outputs: tuple[tuple[str, MarkType], ...] = ()
    schedule_interval: float | None = None  # seconds; set by principal

    def produces(self, scope: str, mark_type: MarkType) -> bool:
        """Check if this manifest declares production of a mark type in a scope."""
        return (scope, mark_type) in self.outputs

    def consumes_pattern(self, pattern: WatchPattern) -> bool:
        """Check if this manifest declares consumption matching a pattern."""
        return pattern in self.inputs


# Resolve forward reference: Agent.manifest -> AgentManifest
Agent.model_rebuild()


# ---------------------------------------------------------------------------
# Mark types
# ---------------------------------------------------------------------------


class Mark(BaseModel):
    """Base mark. Not instantiated directly — use subtypes."""

    model_config = ConfigDict(
        frozen=False
    )  # space.py mutates id/agent_id/created_at on write

    scope: str = ""
    mark_type: MarkType = MarkType.INTENT  # overridden by subtypes in model_post_init
    agent_id: uuid.UUID = Field(default_factory=uuid.uuid4)  # set by MarkSpace on write
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    created_at: float = 0.0  # set by MarkSpace on write
    initial_strength: float = Field(default=1.0, ge=0.0)
    supersedes: uuid.UUID | None = None  # any mark can supersede a prior mark
    projected: bool = False  # True if content fields were redacted by a projected read


class Intent(Mark):
    """
    Declares that an agent plans to act on a resource.
    Spec Section 2.2.
    """

    resource: str = ""
    action: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    def model_post_init(self, __context: Any) -> None:
        self.mark_type = MarkType.INTENT


class Action(Mark):
    """
    Records that an agent did something. Actions are facts.
    Spec Section 2.3.
    """

    resource: str = ""
    action: str = ""
    result: Any = None

    def model_post_init(self, __context: Any) -> None:
        self.mark_type = MarkType.ACTION


class Observation(Mark):
    """
    Records something an agent perceived about the world.
    Spec Section 2.4.
    """

    topic: str = ""
    content: Any = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Source = Source.FLEET

    def model_post_init(self, __context: Any) -> None:
        self.mark_type = MarkType.OBSERVATION


class Warning(Mark):
    """
    Declares that a previous mark or assumption is no longer valid.
    Spec Section 2.5.
    """

    invalidates: uuid.UUID | None = None
    topic: str = ""
    reason: str = ""
    severity: Severity = Severity.INFO

    def model_post_init(self, __context: Any) -> None:
        self.mark_type = MarkType.WARNING


class Need(Mark):
    """
    Requests input from the principal.
    Spec Section 2.6.
    """

    question: str = ""
    context: Any = None
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    blocking: bool = False
    resolved_by: uuid.UUID | None = None

    def model_post_init(self, __context: Any) -> None:
        self.mark_type = MarkType.NEED


# Union of all concrete mark types
AnyMark = Union[Intent, Action, Observation, Warning, Need]


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------

# Default weights. Conforming implementations may change values but MUST
# preserve the ordering: fleet >= external_verified >= external_unverified.
TRUST_WEIGHTS: dict[Source, float] = {
    Source.FLEET: 1.0,
    Source.EXTERNAL_VERIFIED: 0.7,
    Source.EXTERNAL_UNVERIFIED: 0.3,
}


def trust_weight(source: Source) -> float:
    """
    Return the trust weight for a given source.
    Spec Section 4.1 — MUST preserve total order.
    """
    return TRUST_WEIGHTS[source]


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


def compute_strength(mark: AnyMark, now: float, decay_config: DecayConfig) -> float:
    """
    Compute a mark's strength at time `now`, based on its type and the scope's
    decay configuration. Pure function — no side effects.

    Spec Section 3.1.
    """
    age = now - mark.created_at
    if age < 0:
        # Mark is from the future — shouldn't happen, but handle gracefully.
        return mark.initial_strength

    if mark.mark_type == MarkType.ACTION:
        # P2: Action permanence — constant strength.
        return mark.initial_strength

    if mark.mark_type == MarkType.INTENT:
        # P4: Step function — full strength until TTL, then 0.
        if age > decay_config.intent_ttl:
            return 0.0
        return mark.initial_strength

    if mark.mark_type == MarkType.OBSERVATION:
        # P1: Exponential decay.
        half_life = decay_config.observation_half_life
        return mark.initial_strength * (0.5 ** (age / half_life))

    if mark.mark_type == MarkType.WARNING:
        # P1: Exponential decay with warning-specific half-life.
        half_life = decay_config.warning_half_life
        return mark.initial_strength * (0.5 ** (age / half_life))

    if mark.mark_type == MarkType.NEED:
        # P5: Full strength while unresolved, 0 when resolved.
        assert isinstance(mark, Need)
        if mark.resolved_by is not None:
            return 0.0
        return mark.initial_strength

    raise ValueError(f"Unknown mark type: {mark.mark_type}")


# ---------------------------------------------------------------------------
# Effective strength (decay + trust)
# ---------------------------------------------------------------------------


def effective_strength(mark: AnyMark, now: float, decay_config: DecayConfig) -> float:
    """
    Effective strength = decay strength * trust weight.
    Observation and warning marks carry a source field.
    All other mark types are fleet-internal (trust = 1.0).

    Spec Section 4.2.
    """
    base = compute_strength(mark, now, decay_config)
    if hasattr(mark, "source"):
        return base * trust_weight(mark.source)
    return base


def effective_strength_with_warnings(
    mark: AnyMark,
    warnings: list[Warning],
    now: float,
    decay_config: DecayConfig,
) -> float:
    """
    Effective strength after warning invalidation.
    Warnings targeting this mark reduce its effective strength.

    Spec Section 9.5.
    P22: Result MUST NOT be < 0.
    P23: As warnings decay, invalidated mark's strength recovers.
    """
    base = effective_strength(mark, now, decay_config)
    total_warning_strength = sum(
        effective_strength(w, now, decay_config)
        for w in warnings
        if w.invalidates == mark.id
    )
    return max(0.0, base - total_warning_strength)


# ---------------------------------------------------------------------------
# Reinforcement
# ---------------------------------------------------------------------------

REINFORCEMENT_FACTOR: float = 0.3
REINFORCEMENT_CAP: float = 2.0


def reinforce(strengths: list[float]) -> float:
    """
    Combine multiple mark strengths on the same scope+topic into an
    aggregate signal. Sublinear and bounded.

    Spec Section 5.1.
    P8:  aggregate < N * max_single for N > 1
    P9:  aggregate <= REINFORCEMENT_CAP
    P10: adding a positive strength cannot decrease aggregate
    """
    active = sorted([s for s in strengths if s > 0.0], reverse=True)
    if not active:
        return 0.0
    result = active[0]
    for s in active[1:]:
        result += s * REINFORCEMENT_FACTOR
    return min(result, REINFORCEMENT_CAP)


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Projection (read access control)
# ---------------------------------------------------------------------------

# Content fields by mark type — redacted in projected reads.
# Structural fields (id, resource, topic, confidence, severity, etc.) are preserved.
CONTENT_FIELDS: dict[MarkType, tuple[str, ...]] = {
    MarkType.INTENT: (),  # no content fields
    MarkType.ACTION: ("result",),  # what happened
    MarkType.OBSERVATION: ("content",),  # the observation payload
    MarkType.WARNING: ("reason",),  # why something is invalid
    MarkType.NEED: ("question", "context"),  # what's being asked + supporting info
}


def project_mark(mark: AnyMark) -> AnyMark:
    """
    Create a projected copy of a mark with content fields redacted.
    Structural/coordination metadata is preserved. The projected flag is set.

    Spec Section 7.4 — PROTECTED scope projected reads.
    """
    updates: dict[str, Any] = {"projected": True}
    for field_name in CONTENT_FIELDS.get(mark.mark_type, ()):
        # Use type-appropriate defaults: None for Any, "" for str
        field_info = mark.model_fields.get(field_name)
        if field_info and field_info.annotation is str:
            updates[field_name] = ""
        else:
            updates[field_name] = None
    return mark.model_copy(update=updates)


def resolve_conflict(intents: list[Intent], policy: ConflictPolicy) -> uuid.UUID | None:
    """
    Given a set of competing intent marks on the same (scope, resource),
    determine which intent wins. Returns the winning intent's id, or None
    if the policy is YIELD_ALL (all agents must request principal input).

    Spec Section 6.1.
    P11: Deterministic — same inputs always produce same output.
    P12: Progress — at least one agent can proceed (unless YIELD_ALL).
    """
    if not intents:
        return None

    if policy == ConflictPolicy.YIELD_ALL:
        return None

    if policy == ConflictPolicy.FIRST_WRITER:
        winner = min(intents, key=lambda i: (i.created_at, str(i.id)))
        return winner.id

    if policy == ConflictPolicy.HIGHEST_CONFIDENCE:
        # Highest confidence first, ties broken by earliest created_at, then id
        winner = max(intents, key=lambda i: (i.confidence, -i.created_at, str(i.id)))
        return winner.id

    raise ValueError(f"Unknown conflict policy: {policy}")
