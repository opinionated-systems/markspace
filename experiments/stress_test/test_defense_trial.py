# -*- coding: utf-8 -*-
"""
A defense trial's harness code runs without an LLM: a client that answers
every call with a stop and no tool call makes every active agent miss the
probe's canary, so the COMPROMISED path, the per-round defense snapshot and
the bots' write budgets all run.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from markspace import Agent, GuardVerdict, MarkType, Need
from markspace.llm import LLMClient

# run.py imports its sibling modules (scenario) by name.
sys.path.insert(0, str(Path(__file__).resolve().parent))
run = importlib.import_module("run")
scenario = importlib.import_module("scenario")


class _StopClient:
    """Answers every call with a stop and no tool call."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }


def test_a_defense_trial_runs_without_an_llm(tmp_path: Path) -> None:
    result = run.run_trial(
        agents_per_dept=1,
        n_rounds=6,
        client=cast(LLMClient, _StopClient()),
        seed=42,
        phase="stub",
        model="stub",
        output_dir=tmp_path,
        max_concurrent=4,
        adversarial=1,
        adversarial_mode="rate_spike",
        probe_interval=5,
    )
    assert result.error is None
    assert result.n_rounds == 6
    # The probe ran at round 5: agents that missed the canary got a barrier.
    assert result.defense["barriers"]
    # The monitor's verdicts are recorded under their own keys.
    rounds = [
        json.loads(line)
        for line in (tmp_path / "rounds.jsonl").read_text().splitlines()
    ]
    assert all("monitor_restricted_count" in r["defense"] for r in rounds)
    assert not any("envelope_restricted_count" in r["defense"] for r in rounds)
    # The bots stayed within their write budgets: the guard wrote no need
    # mark for the principal in _system (an exhausted budget would).
    assert result.need_marks_by_scope.get("_system", 0) == 0


def test_the_monitor_counts_each_write_and_refused_attempt_once() -> None:
    """The defense harness feeds its monitor each stored mark through a write
    hook and each refused attempt that stores no mark (CONFLICT with no
    intent id); the guard's and the probe's agents are exempt."""
    env = run.OfficeEnv(defense_enabled=True)
    monitor, probe = env.envelope, env.probe
    assert monitor is not None and probe is not None
    wday = env._wday("mon")
    holder, agent, other = (
        Agent(name=n, scopes={"lunch": ["intent", "action", "need"]})
        for n in ("h", "a", "o")
    )
    held, leased = f"A/{wday}/12:00/1", f"A/{wday}/12:00/2"
    env.guard.execute(holder, "lunch", held, "order", "ordered", tool_fn=lambda: {})
    assert env.guard.pre_action(other, "lunch", leased, "order").intent_id is not None

    refused = env.guard.pre_action(agent, "lunch", held, "order")
    assert refused.verdict == GuardVerdict.CONFLICT and refused.intent_id is None
    beaten = env.guard.pre_action(agent, "lunch", leased, "order")
    assert beaten.verdict == GuardVerdict.CONFLICT and beaten.intent_id is not None
    stats = monitor.get_stats(agent.id)
    assert stats is not None
    # One refused attempt and one stored intent.
    assert stats.current_window.counts == {MarkType.INTENT: 2}

    env.guard.get_or_create_barrier(agent.id).narrow_all("action")
    need_id = env.guard.write_mark(agent, Need(scope="lunch", question="q"))
    need = env.space.get_mark(need_id)
    assert need is not None and need.agent_id == env.guard.system_agent_id
    probe.inject_canary("diagnostics", content={"canary": 1})
    assert monitor.get_stats(need.agent_id) is None
    assert monitor.get_stats(probe.probe_agent.id) is None
    assert monitor.reset(agent.id, env.principal_token) is False  # not restricted
    assert monitor._principal_token == env.principal_token


def test_the_bots_get_write_budgets_from_their_declared_rates() -> None:
    """Twice the declared rates over 30 days; the parking bot also has an
    action budget, or pre_action would refuse all its intents."""
    env = run.OfficeEnv(defense_enabled=True)
    _profiles, parking_bot, building_bot = scenario.generate_profiles(1, 42)
    env.defend_bots(parking_bot, building_bot)
    parking = env.guard._write_budget_for(parking_bot)
    building = env.guard._write_budget_for(building_bot)
    assert parking is not None and building is not None
    assert parking.per_hour == {
        MarkType.INTENT: 1.0,
        MarkType.OBSERVATION: 0.3,
        MarkType.ACTION: 1.0,
    }
    assert building.per_hour == {MarkType.WARNING: 0.5}


def test_execute_feeds_the_monitor_and_keeps_the_confidence() -> None:
    """Every booking tool calls execute(): a refused call is one attempt for
    the monitor, and the confidence reaches the intent."""
    env = run.OfficeEnv(defense_enabled=True)
    monitor = env.envelope
    assert monitor is not None
    wday = env._wday("mon")
    holder, agent = (
        Agent(name=n, scopes={"lunch": ["intent", "action"]}) for n in ("h", "a")
    )
    held = f"A/{wday}/12:00/1"
    env.guard.execute(holder, "lunch", held, "order", "ordered", tool_fn=lambda: {})
    for _ in range(5):
        decision, _result = env.guard.execute(
            agent, "lunch", held, "order", "ordered", tool_fn=lambda: {}
        )
        assert decision.verdict == GuardVerdict.CONFLICT
    stats = monitor.get_stats(agent.id)
    assert stats is not None and stats.current_window.counts == {MarkType.INTENT: 5}
    env.guard.execute(
        agent,
        "lunch",
        f"A/{wday}/12:00/2",
        "order",
        "ordered",
        tool_fn=lambda: {},
        confidence=0.9,
    )
    intents = [
        m
        for m in env.space._marks.values()
        if m.mark_type == MarkType.INTENT and m.agent_id == agent.id
    ]
    assert [getattr(m, "confidence") for m in intents] == [0.9]


def test_report_observation_denies_an_unknown_scope() -> None:
    """An observation in a scope the agent may write but the space does not
    register (the stored slow_drift adversaries wrote to 'rooms') is a
    refusal the agent sees; it does not end the agent's round."""
    env = run.OfficeEnv(defense_enabled=True)
    agent = Agent(name="drift", scopes={"rooms": ["observation"]})
    ok, msg = env.report_observation(agent, "rooms", "room empty")
    assert not ok and msg.startswith("DENIED")
