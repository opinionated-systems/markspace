# -*- coding: utf-8 -*-
"""
Diagnostic Probe Tests

Tests that the canary injection and agent response checking logic
correctly classifies agents as HEALTHY, SUSPICIOUS, or COMPROMISED,
and that COMPROMISED verdicts trigger blocking Need marks.
"""

from __future__ import annotations

import uuid

import pytest

from markspace import (
    Agent,
    DecayConfig,
    MarkSpace,
    MarkType,
    Need,
    Observation,
    Scope,
    Source,
    hours,
    minutes,
)
from markspace.probe import DiagnosticProbe, ProbeConfig, ProbeVerdict


T0 = 1_000_000.0


@pytest.fixture
def diag_scope() -> Scope:
    return Scope(
        name="diagnostics",
        decay=DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=minutes(30),
        ),
    )


@pytest.fixture
def space(diag_scope: Scope) -> MarkSpace:
    return MarkSpace(scopes=[diag_scope], clock=T0)


@pytest.fixture
def probe(space: MarkSpace) -> DiagnosticProbe:
    return DiagnosticProbe(space, clock=lambda: T0)


@pytest.fixture
def agent_a() -> Agent:
    return Agent(
        name="agent-a",
        scopes={"diagnostics": ["observation", "warning", "need"]},
        read_scopes=frozenset({"diagnostics"}),
    )


@pytest.fixture
def agent_b() -> Agent:
    return Agent(
        name="agent-b",
        scopes={"diagnostics": ["observation", "warning", "need"]},
        read_scopes=frozenset({"diagnostics"}),
    )


def test_inject_canary(probe: DiagnosticProbe, space: MarkSpace) -> None:
    """inject_canary writes an observation to the probe scope."""
    canary_id = probe.inject_canary("some-target")

    marks = space.read(scope="diagnostics", topic="probe-canary")
    assert len(marks) == 1
    assert marks[0].id == canary_id
    assert isinstance(marks[0], Observation)
    assert marks[0].topic == "probe-canary"
    assert marks[0].content == {"canary": True, "target_scope": "some-target"}


def test_canary_visibility(probe: DiagnosticProbe) -> None:
    """check_canary_visibility returns True for a just-written canary."""
    canary_id = probe.inject_canary("some-target")
    assert probe.check_canary_visibility(canary_id) is True


def test_canary_visibility_missing(probe: DiagnosticProbe) -> None:
    """check_canary_visibility returns False for a nonexistent id."""
    assert probe.check_canary_visibility(uuid.uuid4()) is False


def test_healthy_verdict(probe: DiagnosticProbe) -> None:
    """Agent that wrote marks referencing the canary topic gets HEALTHY."""
    canary_id = probe.inject_canary("some-target")
    agent_id = uuid.uuid4()

    marks_after = [
        Observation(
            scope="diagnostics",
            topic="probe-canary",
            content={"ack": True},
            confidence=0.9,
            source=Source.FLEET,
        ),
    ]

    result = probe.check_agent_response(agent_id, "agent-a", canary_id, marks_after)

    assert result.verdict == ProbeVerdict.HEALTHY
    assert result.agent_id == agent_id
    assert result.agent_name == "agent-a"
    assert result.probe_mark_id == canary_id
    assert "acknowledged" in result.details.lower()


def test_suspicious_verdict(probe: DiagnosticProbe) -> None:
    """Agent that wrote marks but not referencing canary topic gets SUSPICIOUS."""
    canary_id = probe.inject_canary("some-target")
    agent_id = uuid.uuid4()

    marks_after = [
        Observation(
            scope="diagnostics",
            topic="unrelated-topic",
            content={"something": "else"},
            confidence=0.8,
            source=Source.FLEET,
        ),
    ]

    result = probe.check_agent_response(agent_id, "agent-b", canary_id, marks_after)

    assert result.verdict == ProbeVerdict.SUSPICIOUS
    assert "1 marks" in result.details or "1 mark" in result.details


def test_compromised_verdict(probe: DiagnosticProbe) -> None:
    """Agent that wrote no marks gets COMPROMISED."""
    canary_id = probe.inject_canary("some-target")
    agent_id = uuid.uuid4()

    result = probe.check_agent_response(agent_id, "agent-c", canary_id, [])

    assert result.verdict == ProbeVerdict.COMPROMISED
    assert "no marks" in result.details.lower()


def test_compromised_writes_need(space: MarkSpace, diag_scope: Scope) -> None:
    """COMPROMISED verdict triggers a blocking Need write via run_probe."""
    probe = DiagnosticProbe(space, clock=lambda: T0)
    agent_id = uuid.uuid4()

    agents = {agent_id: ("compromised-agent", [])}
    results = probe.run_probe("some-target", agents)

    assert len(results) == 1
    assert results[0].verdict == ProbeVerdict.COMPROMISED

    needs = space.read(scope="diagnostics", mark_type=MarkType.NEED)
    assert len(needs) == 1
    need = needs[0]
    assert isinstance(need, Need)
    assert need.blocking is True
    assert need.priority == 1.0
    assert "compromised-agent" in need.question
    assert "COMPROMISED" in need.question


def test_run_probe_full_cycle(space: MarkSpace) -> None:
    """run_probe injects canary and checks all agents."""
    probe = DiagnosticProbe(space, clock=lambda: T0)

    healthy_id = uuid.uuid4()
    suspicious_id = uuid.uuid4()
    compromised_id = uuid.uuid4()

    canary_topic = ProbeConfig().canary_topic

    agents = {
        healthy_id: (
            "healthy-agent",
            [
                Observation(
                    scope="diagnostics",
                    topic=canary_topic,
                    content={"ack": True},
                    confidence=0.9,
                    source=Source.FLEET,
                ),
            ],
        ),
        suspicious_id: (
            "suspicious-agent",
            [
                Observation(
                    scope="diagnostics",
                    topic="something-else",
                    content={},
                    confidence=0.5,
                    source=Source.FLEET,
                ),
            ],
        ),
        compromised_id: ("compromised-agent", []),
    }

    results = probe.run_probe("target-scope", agents)

    assert len(results) == 3
    by_name = {r.agent_name: r for r in results}
    assert by_name["healthy-agent"].verdict == ProbeVerdict.HEALTHY
    assert by_name["suspicious-agent"].verdict == ProbeVerdict.SUSPICIOUS
    assert by_name["compromised-agent"].verdict == ProbeVerdict.COMPROMISED

    # All results share the same canary mark id
    canary_ids = {r.probe_mark_id for r in results}
    assert len(canary_ids) == 1

    # Canary observation should be readable in the space
    marks = space.read(scope="diagnostics", topic=canary_topic)
    assert len(marks) == 1


def test_results_accumulate(space: MarkSpace) -> None:
    """get_results() returns all historical results across multiple probes."""
    probe = DiagnosticProbe(space, clock=lambda: T0)

    agent_1 = uuid.uuid4()
    agent_2 = uuid.uuid4()

    # First probe run
    probe.run_probe("scope-a", {agent_1: ("agent-1", [])})
    assert len(probe.get_results()) == 1

    # Second probe run
    probe.run_probe(
        "scope-b",
        {
            agent_2: (
                "agent-2",
                [
                    Observation(
                        scope="diagnostics",
                        topic="probe-canary",
                        content={},
                        confidence=0.9,
                        source=Source.FLEET,
                    ),
                ],
            ),
        },
    )
    all_results = probe.get_results()
    assert len(all_results) == 2
    assert all_results[0].agent_name == "agent-1"
    assert all_results[0].verdict == ProbeVerdict.COMPROMISED
    assert all_results[1].agent_name == "agent-2"
    assert all_results[1].verdict == ProbeVerdict.HEALTHY
