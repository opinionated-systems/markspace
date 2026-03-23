# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Core Algebra

This module defines the fundamental types and pure functions of the protocol.
Everything here is stateless. The MarkSpace (space.py) provides statefulness.

Types:  MarkType, Source, ConflictPolicy, DecayConfig, Scope, Agent,
        Mark, Intent, Action, Observation, Warning, Need
Functions: compute_strength, trust_weight, effective_strength,
           effective_strength_with_warnings, reinforce, resolve_conflict
"""

from __future__ import annotations

import uuid
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from markspace.budget import TokenBudget
from markspace.rate_limit import ScopeRateLimit

# Type alias for mark payload fields that carry arbitrary data (Action.result,
# Observation.content, Need.context). Named alias makes intent explicit and
# provides a single point to tighten the type in the future (e.g., to JsonValue).
MarkPayload = Any

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
# Scope helpers
# ---------------------------------------------------------------------------


def scope_contains(parent: str, child: str) -> bool:
    """Check if *child* is equal to or nested under *parent* in the scope hierarchy."""
    return child == parent or child.startswith(parent + "/")


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

    OPEN:       Any agent reads full marks. Default. (Original P20 behavior.)
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
    allowed_intent_verbs: tuple[str, ...] = ()
    allowed_action_verbs: tuple[str, ...] = ()
    observation_topics: tuple[str, ...] = ("*",)
    warning_topics: tuple[str, ...] = ("*",)
    decay: DecayConfig = DecayConfig(
        observation_half_life=hours(6),
        warning_half_life=hours(2),
        intent_ttl=minutes(30),
    )
    conflict_policy: ConflictPolicy = ConflictPolicy.HIGHEST_CONFIDENCE
    deferred: bool = False  # Spec Section 6.2: deferred resolution mode
    rate_limit: ScopeRateLimit | None = None  # Spec Section 9.12: optional rate limit

    def allows_intent_verb(self, action: str) -> bool:
        return action in self.allowed_intent_verbs

    def allows_action_verb(self, action: str) -> bool:
        return action in self.allowed_action_verbs

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

    scopes: mapping of scope_name -> list of writable MarkType names.
    read_scopes: set of scope names with full content read access.
    max_source: maximum trust source this agent can claim on observations.
    Both are hierarchical: permission for "a" implies permission for "a/b".

    Spec Section 11.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    scopes: dict[str, list[str]] = Field(default_factory=dict)
    read_scopes: frozenset[str] = Field(default_factory=frozenset)
    max_source: Source = Source.FLEET  # maximum trust level this agent can write

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

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Agent):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


# ---------------------------------------------------------------------------
# Watch patterns and agent manifests (composition)
# ---------------------------------------------------------------------------


class WatchPattern(BaseModel):
    """
    A pattern describing marks an agent is interested in.
    Used for subscription-based reactive activation.

    Spec Section 14.1.
    P55: matches() is a pure function with no side effects.
    """

    model_config = ConfigDict(frozen=True)

    scope: str  # required - which scope to watch (hierarchical matching)
    mark_type: MarkType | None = None  # optional - filter by mark type
    topic: str | None = None  # optional - filter observations/warnings by topic
    resource: str | None = None  # optional - filter intents/actions by resource

    def matches(self, mark: AnyMark) -> bool:
        """Check if a mark matches this pattern. Pure function (P55)."""
        # Scope: exact or hierarchical (consistent with P23)
        if not scope_contains(self.scope, mark.scope):
            return False
        if self.mark_type is not None and mark.mark_type != self.mark_type:
            return False
        if self.topic is not None:
            if isinstance(mark, (Observation, Warning)):
                if mark.topic != self.topic:
                    return False
            else:
                return False
        if self.resource is not None:
            if isinstance(mark, (Intent, Action)):
                if mark.resource != self.resource:
                    return False
            else:
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
    expected_activity: dict[str, float] | None = (
        None  # MarkType.value -> expected marks per hour
    )
    budget: TokenBudget | None = None  # optional token budget (Section 9.10)

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

# Mark types that carry a topic field (for WatchPattern filtering)
_TOPIC_MARK_TYPES = frozenset({MarkType.OBSERVATION, MarkType.WARNING})
# Mark types that carry a resource field
_RESOURCE_MARK_TYPES = frozenset({MarkType.INTENT, MarkType.ACTION})


class Mark(BaseModel):
    """
    Base mark. Not instantiated directly - use subtypes
    (Intent, Action, Observation, Warning, Need).

    Marks are immutable once written to the space. The MarkSpace.write()
    method creates a copy with assigned id/agent_id/created_at rather
    than mutating the original.
    """

    model_config = ConfigDict(frozen=True)

    def __init__(self, **data: Any) -> None:
        if type(self) is Mark:
            raise TypeError(
                "Mark cannot be instantiated directly. "
                "Use Intent, Action, Observation, Warning, or Need."
            )
        super().__init__(**data)

    scope: str = ""
    mark_type: MarkType = MarkType.INTENT
    agent_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    created_at: float = 0.0
    initial_strength: float = Field(default=1.0, ge=0.0)
    supersedes: uuid.UUID | None = None  # any mark can supersede a prior mark
    projected: bool = False  # True if content fields were redacted by a projected read


class Intent(Mark):
    """
    Declares that an agent plans to act on a resource.
    Spec Section 2.2.
    """

    mark_type: MarkType = MarkType.INTENT
    resource: str = ""
    action: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class Action(Mark):
    """
    Records that an agent did something. Actions are facts.
    Spec Section 2.3.
    """

    mark_type: MarkType = MarkType.ACTION
    resource: str = ""
    action: str = ""
    result: MarkPayload = None
    failed: bool = False  # True when the tool threw an exception


class Observation(Mark):
    """
    Records something an agent perceived about the world.
    Spec Section 2.4.
    """

    mark_type: MarkType = MarkType.OBSERVATION
    topic: str = ""
    content: MarkPayload = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Source = Source.FLEET


class Warning(Mark):
    """
    Declares that a previous mark or assumption is no longer valid.
    Spec Section 2.5.
    """

    mark_type: MarkType = MarkType.WARNING
    invalidates: uuid.UUID | None = None
    topic: str = ""
    reason: str = ""
    severity: Severity = Severity.INFO


class Need(Mark):
    """
    Requests input from the principal.
    Spec Section 2.6.
    """

    mark_type: MarkType = MarkType.NEED
    question: str = ""
    context: MarkPayload = None
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    blocking: bool = False
    resolved_by: uuid.UUID | None = None  # ID of the resolving Action mark
    resolved_by_agent: uuid.UUID | None = None  # ID of the agent that resolved it


# Union of all concrete mark types
AnyMark = Intent | Action | Observation | Warning | Need


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------

# Default weights. Conforming implementations MUST preserve the ordering:
# fleet >= external_verified >= external_unverified (P6 invariant).
# Immutable to prevent accidental mutation that would break the ordering.
TRUST_WEIGHTS: MappingProxyType[Source, float] = MappingProxyType(
    {
        Source.FLEET: 1.0,
        Source.EXTERNAL_VERIFIED: 0.7,
        Source.EXTERNAL_UNVERIFIED: 0.3,
    }
)


def trust_weight(source: Source) -> float:
    """
    Return the trust weight for a given source.
    Spec Section 4.1 - MUST preserve total order.
    """
    return TRUST_WEIGHTS[source]


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


def compute_strength(mark: AnyMark, now: float, decay_config: DecayConfig) -> float:
    """
    Compute a mark's strength at time `now`, based on its type and the scope's
    decay configuration. Pure function - no side effects.

    Spec Section 3.1.
    """
    age = now - mark.created_at
    if age < 0:
        # Mark is from the future - shouldn't happen, but handle gracefully.
        return mark.initial_strength

    if mark.mark_type == MarkType.ACTION:
        # P2: Action permanence - constant strength.
        return mark.initial_strength

    if mark.mark_type == MarkType.INTENT:
        # P4: Step function - full strength until TTL, then 0.
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
        if not isinstance(mark, Need):
            raise TypeError(
                f"Mark with mark_type=NEED must be a Need instance, got {type(mark).__name__}"
            )
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
    Observation marks carry a source field; all others are fleet-internal (trust = 1.0).

    Spec Section 4.2.
    """
    base = compute_strength(mark, now, decay_config)
    if isinstance(mark, Observation):
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
    P34: Result MUST NOT be < 0.
    P35: As warnings decay, invalidated mark's strength recovers.
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

# Content fields by mark type - redacted in projected reads.
# Structural fields (id, resource, topic, confidence, severity, etc.) are preserved.
CONTENT_FIELDS: dict[MarkType, tuple[str, ...]] = {
    MarkType.INTENT: (),  # no content fields
    MarkType.ACTION: ("result",),  # what happened
    MarkType.OBSERVATION: ("content",),  # the observation payload
    MarkType.WARNING: ("reason",),  # why something is invalid
    MarkType.NEED: ("question", "context"),  # what's being asked + supporting info
}


_REDACTED_DEFAULTS: dict[str, Any] = {
    "result": None,
    "content": None,
    "reason": "",
    "question": "",
    "context": None,
}


def project_mark(mark: AnyMark) -> AnyMark:
    """
    Create a projected copy of a mark with content fields redacted.
    Structural/coordination metadata is preserved. The projected flag is set.

    Spec Section 7.4 - PROTECTED scope projected reads.
    """
    updates: dict[str, Any] = {"projected": True}
    for field_name in CONTENT_FIELDS.get(mark.mark_type, ()):
        if field_name not in _REDACTED_DEFAULTS:
            raise KeyError(
                f"No redacted default for content field '{field_name}' "
                f"on mark type {mark.mark_type.value}. "
                f"Add it to _REDACTED_DEFAULTS."
            )
        updates[field_name] = _REDACTED_DEFAULTS[field_name]
    return mark.model_copy(update=updates)


def resolve_conflict(intents: list[Intent], policy: ConflictPolicy) -> uuid.UUID | None:
    """
    Given a set of competing intent marks on the same (scope, resource),
    determine which intent wins. Returns the winning intent's id, or None
    if the policy is YIELD_ALL (all agents must request principal input).

    Spec Section 6.1.
    P11: Deterministic - same inputs always produce same output.
    P12: Progress - at least one agent can proceed (unless YIELD_ALL).
    """
    if not intents:
        return None

    if policy == ConflictPolicy.YIELD_ALL:
        return None

    if policy == ConflictPolicy.FIRST_WRITER:
        winner = min(intents, key=lambda i: (i.created_at, str(i.id)))
        return winner.id

    if policy == ConflictPolicy.HIGHEST_CONFIDENCE:
        # Highest confidence first. Ties broken by *earliest* created_at
        # (first-mover wins), then id for full determinism.
        winner = max(intents, key=lambda i: (i.confidence, -i.created_at, str(i.id)))
        return winner.id

    raise ValueError(f"Unknown conflict policy: {policy}")
