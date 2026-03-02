# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Reference Implementation

Usage:
    from markspace import (
        MarkSpace, Scope, Agent, DecayConfig,
        Intent, Action, Observation, Warning, Need,
        ConflictPolicy, Source, Severity,
        hours, minutes,
    )

Spec: ../docs/spec.md
"""

from markspace.core import (
    CONTENT_FIELDS,
    REINFORCEMENT_CAP,
    REINFORCEMENT_FACTOR,
    TRUST_WEIGHTS,
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
    Severity,
    Source,
    Warning,
    compute_strength,
    effective_strength,
    effective_strength_with_warnings,
    hours,
    minutes,
    project_mark,
    reinforce,
    resolve_conflict,
    trust_weight,
)
from markspace.guard import Guard, GuardDecision, GuardVerdict
from markspace.llm import LLMClient, LLMConfig
from markspace.models import DEFAULT_MODELS, FIREWORKS_MODELS, resolve_model_id
from markspace.space import MarkSpace, NeedCluster, ScopeError, ValidationError

__all__ = [
    # Types
    "Action",
    "Agent",
    "AnyMark",
    "ConflictPolicy",
    "DecayConfig",
    "Intent",
    "Mark",
    "MarkType",
    "Need",
    "NeedCluster",
    "Observation",
    "Scope",
    "ScopeError",
    "ScopeVisibility",
    "Severity",
    "Source",
    "ValidationError",
    "Warning",
    # Functions
    "compute_strength",
    "effective_strength",
    "effective_strength_with_warnings",
    "hours",
    "minutes",
    "project_mark",
    "reinforce",
    "resolve_conflict",
    "trust_weight",
    # Constants
    "CONTENT_FIELDS",
    "REINFORCEMENT_CAP",
    "REINFORCEMENT_FACTOR",
    "TRUST_WEIGHTS",
    # Stateful
    "MarkSpace",
    # Guard
    "Guard",
    "GuardDecision",
    "GuardVerdict",
    # LLM
    "LLMClient",
    "LLMConfig",
    # Models
    "DEFAULT_MODELS",
    "FIREWORKS_MODELS",
    "resolve_model_id",
]
