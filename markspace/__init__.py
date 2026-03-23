# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Reference Implementation

Usage:
    from markspace import (
        MarkSpace, Scope, Agent, DecayConfig,
        Intent, Action, Observation, Warning, Need,
        ConflictPolicy, Source, Severity,
        hours, minutes,
    )

Spec: https://github.com/opinionated-systems/markspace/blob/main/docs/spec.md
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("markspace")
except PackageNotFoundError:
    __version__ = "0.1.3-beta"

from markspace.budget import BudgetStatus, BudgetTracker, TokenBudget
from markspace.compose import validate_manifest_permissions, validate_pipeline
from markspace.core import (
    CONTENT_FIELDS,
    REINFORCEMENT_CAP,
    REINFORCEMENT_FACTOR,
    TRUST_WEIGHTS,
    Action,
    Agent,
    MarkPayload,
    AgentManifest,
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
    Severity,
    Source,
    Warning,
    WatchPattern,
    compute_strength,
    effective_strength,
    effective_strength_with_warnings,
    hours,
    minutes,
    project_mark,
    reinforce,
    resolve_conflict,
    scope_contains,
    trust_weight,
)
from markspace.barrier import AgentBarrier, BarrierSnapshot
from markspace.envelope import (
    AgentStats,
    AnomalyDetector,
    EnvelopeConfig,
    EnvelopeVerdict,
    StatisticalEnvelope,
    WelfordConfig,
    WelfordDetector,
)
from markspace.guard import Guard, GuardDecision, GuardVerdict
from markspace.llm import LLMClient, LLMConfig
from markspace.probe import DiagnosticProbe, ProbeConfig, ProbeResult, ProbeVerdict
from markspace.rate_limit import RateLimitTracker, ScopeRateLimit
from markspace.schedule import Scheduler
from markspace.models import DEFAULT_MODELS, FIREWORKS_MODELS, resolve_model_id
from markspace.telemetry import (
    FailingSink,
    InMemorySink,
    NullSink,
    StructuredLogSink,
    TelemetryEvent,
    TelemetrySink,
)
from markspace.space import (
    MarkSpace,
    NeedCluster,
    QuotaExceededError,
    ScopeError,
    ValidationError,
)

__all__ = [
    # Types
    "Action",
    "Agent",
    "AgentManifest",
    "AnyMark",
    "ConflictPolicy",
    "DecayConfig",
    "Intent",
    "Mark",
    "MarkPayload",
    "MarkType",
    "Need",
    "NeedCluster",
    "Observation",
    "QuotaExceededError",
    "Scope",
    "ScopeError",
    "ScopeVisibility",
    "Severity",
    "Source",
    "ValidationError",
    "Warning",
    "WatchPattern",
    # Functions
    "compute_strength",
    "effective_strength",
    "effective_strength_with_warnings",
    "hours",
    "minutes",
    "project_mark",
    "reinforce",
    "resolve_conflict",
    "scope_contains",
    "trust_weight",
    "validate_manifest_permissions",
    "validate_pipeline",
    # Constants
    "CONTENT_FIELDS",
    "REINFORCEMENT_CAP",
    "REINFORCEMENT_FACTOR",
    "TRUST_WEIGHTS",
    # Stateful
    "MarkSpace",
    # Scheduler
    "Scheduler",
    # Guard
    "Guard",
    "GuardDecision",
    "GuardVerdict",
    # Defense-in-depth
    "AgentBarrier",
    "BarrierSnapshot",
    "AgentStats",
    "AnomalyDetector",
    "DiagnosticProbe",
    "EnvelopeConfig",
    "EnvelopeVerdict",
    "WelfordConfig",
    "WelfordDetector",
    "ProbeConfig",
    "ProbeResult",
    "ProbeVerdict",
    "StatisticalEnvelope",
    # LLM
    "LLMClient",
    "LLMConfig",
    # Models
    "DEFAULT_MODELS",
    "FIREWORKS_MODELS",
    "resolve_model_id",
    # Budget
    "BudgetStatus",
    "BudgetTracker",
    "TokenBudget",
    # Rate limit
    "RateLimitTracker",
    "ScopeRateLimit",
    # Telemetry
    "FailingSink",
    "InMemorySink",
    "NullSink",
    "StructuredLogSink",
    "TelemetryEvent",
    "TelemetrySink",
]
