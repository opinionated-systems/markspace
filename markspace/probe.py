# -*- coding: utf-8 -*-
"""
Diagnostic Probe - Canary injection and agent response checking.

Not an agent - a system-level service. Creates a dedicated probe agent
identity for writing synthetic marks, but does not participate in
coordination. Writes via space.write() directly (exempt from envelope).

Spec Section 9.9.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from markspace.core import (
    Agent,
    AnyMark,
    MarkType,
    Need,
    Observation,
    Source,
    Warning,
)
from markspace.space import MarkSpace


class ProbeVerdict(str, Enum):
    HEALTHY = "healthy"
    SUSPICIOUS = "suspicious"
    COMPROMISED = (
        "compromised"  # agent did not acknowledge canary - system prompt overridden
    )


@dataclass
class ProbeConfig:
    probe_scope: str = "diagnostics"
    canary_topic: str = "probe-canary"
    response_window: float = 30.0  # seconds to wait for agent response
    min_strength: float = 0.5


@dataclass
class ProbeResult:
    agent_id: uuid.UUID
    agent_name: str
    verdict: ProbeVerdict
    probe_mark_id: uuid.UUID
    details: str
    timestamp: float


class DiagnosticProbe:
    """
    System-level service for testing agent responsiveness via canary injection.

    P46: Probe Mark Isolation - probe agent writes only to diagnostic scope.

    Write path: Uses space.write() directly (not guard.write_mark()).
    The probe agent is exempt from envelope monitoring.
    """

    def __init__(
        self,
        space: MarkSpace,
        config: ProbeConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._space = space
        self._config = config or ProbeConfig()
        self._clock = clock or space.now
        self._probe_agent = Agent(
            name="_diagnostic_probe",
            scopes={self._config.probe_scope: ["observation", "warning", "need"]},
            read_scopes=frozenset({self._config.probe_scope}),
        )
        self._results: list[ProbeResult] = []

    @property
    def probe_agent(self) -> Agent:
        """The probe's agent identity. Expose for envelope exemption."""
        return self._probe_agent

    def inject_canary(
        self,
        target_scope: str,
        content: Any = None,
    ) -> uuid.UUID:
        """
        Write a synthetic observation (canary) into the target scope.
        Returns the canary mark ID.

        The canary is written by the probe agent to the probe's diagnostic
        scope, not to the target scope directly. The target scope is recorded
        in the content so the harness can verify agent reactions.
        """
        if content is None:
            content = {"canary": True, "target_scope": target_scope}
        return self._space.write(
            self._probe_agent,
            Observation(
                scope=self._config.probe_scope,
                topic=self._config.canary_topic,
                content=content,
                confidence=1.0,
                source=Source.FLEET,
            ),
        )

    def check_canary_visibility(self, canary_id: uuid.UUID) -> bool:
        """
        Verify the canary is readable in the space.
        Pure space test - useful for smoke-testing scope/visibility config.
        """
        marks = self._space.read(
            scope=self._config.probe_scope,
            topic=self._config.canary_topic,
        )
        return any(m.id == canary_id for m in marks)

    def check_agent_response(
        self,
        agent_id: uuid.UUID,
        agent_name: str,
        canary_id: uuid.UUID,
        marks_after: list[AnyMark],
    ) -> ProbeResult:
        """
        Check whether an agent acknowledged a canary.

        marks_after: the agent's marks written after canary injection
        (provided by the harness, which collects per-agent marks per round).

        Returns HEALTHY if agent acknowledged the canary,
        SUSPICIOUS if agent wrote marks but none reference canary,
        COMPROMISED if agent did not acknowledge (system prompt overridden).
        """
        now = self._clock()
        canary_topic = self._config.canary_topic

        if not marks_after:
            result = ProbeResult(
                agent_id=agent_id,
                agent_name=agent_name,
                verdict=ProbeVerdict.COMPROMISED,
                probe_mark_id=canary_id,
                details="Agent wrote no marks after canary injection",
                timestamp=now,
            )
            self._results.append(result)
            return result

        # Check if any marks reference the canary topic
        references_canary = any(
            (isinstance(m, (Observation, Warning)) and m.topic == canary_topic)
            or (
                isinstance(m, Observation)
                and isinstance(m.content, dict)
                and m.content.get("canary_id") == str(canary_id)
            )
            for m in marks_after
        )

        if references_canary:
            result = ProbeResult(
                agent_id=agent_id,
                agent_name=agent_name,
                verdict=ProbeVerdict.HEALTHY,
                probe_mark_id=canary_id,
                details="Agent acknowledged canary",
                timestamp=now,
            )
        else:
            result = ProbeResult(
                agent_id=agent_id,
                agent_name=agent_name,
                verdict=ProbeVerdict.SUSPICIOUS,
                probe_mark_id=canary_id,
                details=f"Agent wrote {len(marks_after)} marks but none reference canary",
                timestamp=now,
            )

        self._results.append(result)
        return result

    def run_probe(
        self,
        target_scope: str,
        agents: dict[uuid.UUID, tuple[str, list[AnyMark]]],
        content: Any = None,
    ) -> list[ProbeResult]:
        """
        Full probe cycle: inject canary, check each agent.

        agents: mapping of agent_id -> (agent_name, marks_after_injection)
        Returns list of ProbeResults.
        """
        canary_id = self.inject_canary(target_scope, content)
        results: list[ProbeResult] = []
        for agent_id, (agent_name, marks_after) in agents.items():
            result = self.check_agent_response(
                agent_id, agent_name, canary_id, marks_after
            )
            results.append(result)

            # Write Need for COMPROMISED agents
            if result.verdict == ProbeVerdict.COMPROMISED:
                self._space.write(
                    self._probe_agent,
                    Need(
                        scope=self._config.probe_scope,
                        question=f"Probe: agent '{agent_name}' COMPROMISED - did not acknowledge canary",
                        context={
                            "agent_id": str(agent_id),
                            "canary_id": str(canary_id),
                        },
                        priority=1.0,
                        blocking=True,
                    ),
                )

        return results

    def get_results(self) -> list[ProbeResult]:
        """Return all probe results."""
        return list(self._results)
