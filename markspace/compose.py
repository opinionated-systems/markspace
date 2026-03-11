# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Composition Validation

Pure functions for validating agent composition pipelines.
Stateless - operates on manifests and agents, not on MarkSpace.

Spec Section 13.5.
"""

from __future__ import annotations

from markspace.core import Agent, MarkType, scope_contains


def validate_pipeline(agents: list[Agent]) -> list[str]:
    """
    Check that a sequence of agents forms a valid pipeline:
    each agent's outputs can feed the next agent's inputs.

    Returns a list of error messages (empty = valid).

    P53: Pipeline validation is purely structural - no runtime state needed.
    """
    errors: list[str] = []
    for i in range(len(agents) - 1):
        producer = agents[i]
        consumer = agents[i + 1]
        if producer.manifest is None:
            errors.append(f"Agent '{producer.name}' has no manifest (position {i})")
            continue
        if consumer.manifest is None:
            errors.append(f"Agent '{consumer.name}' has no manifest (position {i + 1})")
            continue

        # Check that at least one producer output matches a consumer input.
        # Uses hierarchical scope matching consistent with WatchPattern.matches():
        # an output to "hr/payroll" connects to an input watching "hr".
        connected = False
        for scope, mark_type in producer.manifest.outputs:
            for pattern in consumer.manifest.inputs:
                scope_matches = scope_contains(pattern.scope, scope)
                type_matches = (
                    pattern.mark_type is None or pattern.mark_type == mark_type
                )
                if scope_matches and type_matches:
                    connected = True
                    break
            if connected:
                break

        if not connected:
            errors.append(
                f"No connection: '{producer.name}' outputs do not match "
                f"'{consumer.name}' inputs"
            )

    return errors


def validate_manifest_permissions(agent: Agent) -> list[str]:
    """
    Check that an agent's manifest is consistent with its scope permissions.

    P54: Manifest outputs MUST be a subset of agent's write permissions.
    """
    errors: list[str] = []
    if agent.manifest is None:
        return errors

    for scope, mark_type in agent.manifest.outputs:
        if not agent.can_write(scope, mark_type):
            errors.append(
                f"Agent '{agent.name}' manifest declares output "
                f"({scope}, {mark_type.value}) but lacks write permission"
            )

    return errors
