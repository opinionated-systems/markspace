#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validation Experiment Runner — Stigmergic Coordination

Runs a factorial design across model × n_agents × visibility × temperature.
Supports sequential and concurrent execution, resume from interrupted runs,
and early stopping on consecutive failures.

Usage:
    # Pilot (Phase 1): 10 trials/cell, all factors, 12 cells in parallel
    python experiments/validation/run.py --trials-per-cell 10 --parallel-cells 12

    # Single cell test
    python experiments/validation/run.py --models gpt-oss-120b --agents 3 \\
        --visibility visible --temperature 0.0 --trials-per-cell 1

    # Phase 2b: concurrency
    python experiments/validation/run.py --execution-mode sequential concurrent \\
        --conflict-policy highest_confidence first_writer --trials-per-cell 50 \\
        --parallel-cells 12

    # Resume interrupted run
    python experiments/validation/run.py --resume --output results_pilot_20260227.jsonl \\
        --parallel-cells 12
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from markspace import (
    Agent,
    ConflictPolicy,
    DecayConfig,
    Guard,
    GuardVerdict,
    MarkSpace,
    MarkType,
    Scope,
    hours,
    minutes,
)
from markspace.llm import LLMClient, LLMConfig
from markspace.models import EXTERNAL_MODELS, resolve_model_id

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLOTS = [
    "mon-09",
    "mon-11",
    "mon-14",
    "tue-09",
    "tue-11",
    "tue-14",
    "wed-09",
    "wed-11",
    "wed-14",
    "thu-09",
    "thu-11",
    "thu-14",
    "fri-09",
    "fri-11",
    "fri-14",
]
PREFERRED_SLOT = "wed-14"
MAX_STEPS = 25


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

VIEW_CALENDAR_TOOL = {
    "type": "function",
    "function": {
        "name": "view_calendar",
        "description": (
            "View the current calendar. Returns all 15 time slots and whether "
            "they are available or booked. Call this BEFORE booking to see what's taken."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

BOOK_SLOT_TOOL_VISIBLE = {
    "type": "function",
    "function": {
        "name": "book_slot",
        "description": (
            "Book a time slot. If already taken, returns CONFLICT. "
            "You must then choose a DIFFERENT available slot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "description": f"One of: {', '.join(SLOTS)}",
                    "enum": SLOTS,
                },
            },
            "required": ["slot"],
        },
    },
}

BOOK_SLOT_TOOL_HIDDEN = {
    "type": "function",
    "function": {
        "name": "book_slot",
        "description": (
            "Book a time slot for your meeting. The slot must be one of: "
            f"{', '.join(SLOTS)}. "
            "If the slot is already claimed by another agent, this will FAIL "
            "with a CONFLICT response. You MUST then choose a DIFFERENT slot "
            "and try again. Do NOT retry the same slot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "description": f"One of: {', '.join(SLOTS)}",
                    "enum": SLOTS,
                },
            },
            "required": ["slot"],
        },
    },
}

TOOLS_VISIBLE = [VIEW_CALENDAR_TOOL, BOOK_SLOT_TOOL_VISIBLE]
TOOLS_HIDDEN = [BOOK_SLOT_TOOL_HIDDEN]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    model: str
    n_agents: int
    visibility: str  # "visible" or "hidden"
    temperature: float
    execution_mode: str = "sequential"
    conflict_policy: str = "highest_confidence"
    n_rounds: int = 1
    n_slots: int = 15
    block_self_rebook: bool = False

    def key(self) -> str:
        return (
            f"{self.model}|{self.n_agents}|{self.visibility}"
            f"|{self.temperature}|{self.execution_mode}|{self.conflict_policy}"
            f"|{self.n_rounds}|{self.n_slots}|{self.block_self_rebook}"
        )


@dataclass
class StepRecord:
    agent: str
    step: int
    tool: str
    args: dict = field(default_factory=dict)
    result: str = ""
    guard_verdict: str | None = None
    reasoning: str = ""


@dataclass
class AgentRecord:
    name: str
    preferred: str
    booked: str | None = None
    steps_count: int = 0
    wasted_attempts: int = 0
    viewed_calendar: bool = False
    tokens: dict[str, int] = field(
        default_factory=lambda: {"prompt": 0, "completion": 0}
    )
    steps: list[dict] = field(default_factory=list)


@dataclass
class TrialResult:
    phase: str
    cell: dict
    trial_id: int
    double_bookings: int = 0
    all_completed: bool = False
    total_steps: int = 0
    steps_per_agent: float = 0.0
    guard_invocations: int = 0
    wasted_attempts: int = 0
    tokens: dict[str, int] = field(
        default_factory=lambda: {"prompt": 0, "completion": 0}
    )
    wall_clock_seconds: float = 0.0
    agents: list[dict] = field(default_factory=list)
    error: str | None = None
    n_rounds: int = 1
    rounds: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Calendar environment (merged from S-001 + S-007)
# ---------------------------------------------------------------------------


def make_slots(n_slots: int = 15) -> list[str]:
    """Generate slot names. Default 15 = 5 days × 3 times. For 30, use 5 days × 6 times."""
    days = ["mon", "tue", "wed", "thu", "fri"]
    times_pool = ["09", "11", "14", "10", "13", "16"]
    slots: list[str] = []
    times_needed = (n_slots + len(days) - 1) // len(days)
    times = times_pool[:times_needed]
    for d in days:
        for t in times:
            slots.append(f"{d}-{t}")
            if len(slots) >= n_slots:
                return slots
    return slots


class CalendarEnv:
    def __init__(
        self,
        conflict_policy: ConflictPolicy = ConflictPolicy.HIGHEST_CONFIDENCE,
        n_slots: int = 15,
        block_self_rebook: bool = False,
    ) -> None:
        self.slots = make_slots(n_slots) if n_slots != 15 else SLOTS
        self.scope = Scope(
            name="calendar",
            allowed_intent_verbs=("book",),
            allowed_action_verbs=("booked",),
            decay=DecayConfig(
                observation_half_life=hours(1),
                warning_half_life=hours(1),
                intent_ttl=minutes(30),
            ),
            conflict_policy=conflict_policy,
        )
        self.space = MarkSpace(scopes=[self.scope])
        self.space.set_clock(1_000_000.0)
        self.guard = Guard(self.space, block_self_rebook=block_self_rebook)

        self._stats_lock = threading.Lock()
        self.guard_invocation_count = 0
        self.guard_conflict_count = 0

    def view_calendar(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for slot in self.slots:
            actions = self.space.read(
                scope="calendar",
                resource=slot,
                mark_type=MarkType.ACTION,
            )
            if actions:
                booker = "another agent"
                if hasattr(actions[0], "result") and isinstance(
                    actions[0].result, dict
                ):
                    booker = actions[0].result.get("booked_by", "another agent")
                result[slot] = f"BOOKED by {booker}"
            else:
                result[slot] = "available"
        return result

    def book_slot(
        self,
        agent: Agent,
        slot: str,
        confidence: float = 0.9,
    ) -> tuple[bool, str, str | None]:
        with self._stats_lock:
            self.guard_invocation_count += 1

        def do_book() -> dict[str, str]:
            return {"booked_by": agent.name, "slot": slot}

        decision, _result = self.guard.execute(
            agent=agent,
            scope="calendar",
            resource=slot,
            intent_action="book",
            result_action="booked",
            tool_fn=do_book,
            confidence=confidence,
        )

        if decision.verdict == GuardVerdict.CONFLICT:
            with self._stats_lock:
                self.guard_conflict_count += 1

        if decision.verdict == GuardVerdict.ALLOW:
            return True, f"Successfully booked {slot}.", decision.verdict.value
        elif decision.verdict == GuardVerdict.CONFLICT:
            return (
                False,
                f"CONFLICT: {slot} is already taken. Choose a DIFFERENT available slot.",
                decision.verdict.value,
            )
        else:
            return (
                False,
                f"{decision.verdict.value}: {decision.reason}",
                decision.verdict.value,
            )


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


def make_visible_prompt(
    name: str,
    preferred: str,
    n_agents: int,
    round_num: int = 1,
    n_slots: int = 15,
    slots: list[str] | None = None,
) -> str:
    base = (
        f"You are {name}, one of {n_agents} scheduling agents. "
        f"You need to book exactly ONE time slot for your meeting.\n\n"
        f"Your preferred slot is {preferred}. If it's taken, pick any available slot.\n\n"
        f"There are {n_slots} slots across the week. Other agents may have already booked some.\n\n"
    )
    if round_num > 1:
        base += (
            f"This is round {round_num} of a multi-round booking. "
            f"Some slots are already booked from a previous round "
            f"(you may have booked one yourself). "
            f"You need to book ONE additional slot.\n\n"
        )
    base += (
        f"Strategy:\n"
        f"1. Call view_calendar to see what's available.\n"
        f"2. Book an available slot (prefer {preferred} if free).\n"
        f"3. If your booking fails, call view_calendar AGAIN to get fresh state, "
        f"then pick a DIFFERENT available slot.\n\n"
        f"You MUST book exactly one slot. Do not give up."
    )
    return base


def make_hidden_prompt(
    name: str,
    preferred: str,
    n_agents: int,
    round_num: int = 1,
    n_slots: int = 15,
    slots: list[str] | None = None,
) -> str:
    slot_list = slots or SLOTS
    slots_str = ", ".join(slot_list)
    base = (
        f"You are {name}, one of {n_agents} scheduling agents. "
        f"You need to book exactly ONE time slot for your meeting.\n\n"
        f"Your preferred slot is {preferred}.\n\n"
        f"Available time slots are: {slots_str}\n\n"
    )
    if round_num > 1:
        base += (
            f"This is round {round_num}. Some slots are already booked. "
            f"If you get CONFLICT, try a different slot.\n\n"
        )
    base += (
        f"You cannot see the calendar. You must try to book a slot directly.\n"
        f"If the slot is already claimed by another agent, you will receive a "
        f"CONFLICT response. You MUST then choose a DIFFERENT slot and try again.\n"
        f"Do NOT retry a slot that gave CONFLICT.\n\n"
        f"You MUST book exactly one slot. Do not give up."
    )
    return base


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------


def run_agent(
    agent: Agent,
    env: CalendarEnv,
    preferred_slot: str,
    client: LLMClient,
    tools: list[dict],
    system_prompt: str,
    temperature: float,
) -> tuple[str | None, AgentRecord]:
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Please book your meeting slot now."},
    ]
    booked: str | None = None
    steps_count = 0
    wasted = 0
    viewed_cal = False
    total_tokens: dict[str, int] = {"prompt": 0, "completion": 0}
    step_records: list[dict] = []

    for step_num in range(MAX_STEPS):
        llm_resp = client.chat(messages, tools=tools, temperature=temperature)

        usage = llm_resp.get("usage", {})
        total_tokens["prompt"] += usage.get("prompt_tokens", 0)
        total_tokens["completion"] += usage.get("completion_tokens", 0)

        choice = llm_resp["choices"][0]
        message = choice["message"]
        reasoning = message.get("content", "") or ""
        messages.append(message)

        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            if booked or choice.get("finish_reason") == "stop":
                break
            continue

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args_str = tc["function"].get("arguments", "{}")
            try:
                fn_args = json.loads(fn_args_str) if fn_args_str else {}
            except json.JSONDecodeError:
                fn_args = {}

            tool_result = ""
            guard_verdict: str | None = None

            if fn_name == "view_calendar":
                cal = env.view_calendar()
                tool_result = json.dumps(cal, indent=2)
                viewed_cal = True
                steps_count += 1

            elif fn_name == "book_slot":
                slot = fn_args.get("slot", "")
                valid_slots = env.slots
                if slot not in valid_slots:
                    tool_result = f"Invalid slot '{slot}'. Must be one of: {', '.join(valid_slots)}"
                else:
                    success, msg, verdict_str = env.book_slot(agent, slot)
                    tool_result = msg
                    guard_verdict = verdict_str
                    if success:
                        booked = slot
                    else:
                        wasted += 1
                steps_count += 1

            else:
                tool_result = f"Unknown tool: {fn_name}"

            step_records.append(
                dataclasses.asdict(
                    StepRecord(
                        agent=agent.name,
                        step=steps_count,
                        tool=fn_name,
                        args=fn_args,
                        result=tool_result,
                        guard_verdict=guard_verdict,
                        reasoning=reasoning,
                    )
                )
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                }
            )

        if booked:
            break

    record = AgentRecord(
        name=agent.name,
        preferred=preferred_slot,
        booked=booked,
        steps_count=steps_count,
        wasted_attempts=wasted,
        viewed_calendar=viewed_cal,
        tokens=total_tokens,
        steps=step_records,
    )
    return booked, record


# ---------------------------------------------------------------------------
# Trial runner
# ---------------------------------------------------------------------------


def _run_round(
    agents: list[Agent],
    agents_config: list[tuple[str, str]],
    env: CalendarEnv,
    client: LLMClient,
    tools: list[dict],
    prompt_fn: Callable[..., str],
    cell: Cell,
    round_num: int,
) -> tuple[list[AgentRecord], list[str]]:
    """Run one round of agent bookings. Returns (agent_records, booked_slots)."""
    agent_records: list[AgentRecord] = []
    all_booked: list[str] = []

    if cell.execution_mode == "sequential":
        for i, (name, preferred) in enumerate(agents_config):
            prompt = prompt_fn(
                name,
                preferred,
                cell.n_agents,
                round_num,
                n_slots=cell.n_slots,
                slots=env.slots,
            )
            booked, ar = run_agent(
                agents[i],
                env,
                preferred,
                client,
                tools,
                prompt,
                cell.temperature,
            )
            agent_records.append(ar)
            if booked:
                all_booked.append(booked)

    elif cell.execution_mode == "concurrent":
        thread_results: dict[str, tuple[str | None, AgentRecord]] = {}
        results_lock = threading.Lock()

        def run_thread(idx: int) -> None:
            agent = agents[idx]
            name, preferred = agents_config[idx]
            prompt = prompt_fn(
                name,
                preferred,
                cell.n_agents,
                round_num,
                n_slots=cell.n_slots,
                slots=env.slots,
            )
            booked, ar = run_agent(
                agent,
                env,
                preferred,
                client,
                tools,
                prompt,
                cell.temperature,
            )
            with results_lock:
                thread_results[name] = (booked, ar)

        with ThreadPoolExecutor(max_workers=cell.n_agents) as executor:
            futures = [executor.submit(run_thread, i) for i in range(cell.n_agents)]
            for f in as_completed(futures):
                f.result()

        for name, _ in agents_config:
            booked, ar = thread_results[name]
            agent_records.append(ar)
            if booked:
                all_booked.append(booked)

    return agent_records, all_booked


def _compute_round_metrics(
    agent_records: list[AgentRecord],
    all_booked: list[str],
    env: CalendarEnv,
    round_id: int,
    guard_invocations_before: int,
) -> dict:
    """Compute per-round metrics dict."""
    total_steps = sum(ar.steps_count for ar in agent_records)
    total_wasted = sum(ar.wasted_attempts for ar in agent_records)
    total_tokens: dict[str, int] = {"prompt": 0, "completion": 0}
    for ar in agent_records:
        total_tokens["prompt"] += ar.tokens["prompt"]
        total_tokens["completion"] += ar.tokens["completion"]

    slot_counts = Counter(all_booked)
    double_bookings = sum(c - 1 for c in slot_counts.values() if c > 1)
    n_agents = len(agent_records)

    return {
        "round_id": round_id,
        "steps_per_agent": total_steps / n_agents if n_agents > 0 else 0.0,
        "all_completed": all(ar.booked is not None for ar in agent_records),
        "wasted_attempts": total_wasted,
        "guard_invocations": env.guard_invocation_count - guard_invocations_before,
        "tokens": total_tokens,
        "double_bookings": double_bookings,
        "agents": [dataclasses.asdict(ar) for ar in agent_records],
    }


def _make_tools(slots: list[str], visibility: str) -> list[dict]:
    """Build tool definitions for the given slot list and visibility."""
    slots_str = ", ".join(slots)
    if visibility == "visible":
        return [
            {
                "type": "function",
                "function": {
                    "name": "view_calendar",
                    "description": (
                        f"View the current calendar. Returns all {len(slots)} time slots and whether "
                        "they are available or booked. Call this BEFORE booking to see what's taken."
                    ),
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "book_slot",
                    "description": (
                        "Book a time slot. If already taken, returns CONFLICT. "
                        "You must then choose a DIFFERENT available slot."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "slot": {
                                "type": "string",
                                "description": f"One of: {slots_str}",
                                "enum": slots,
                            },
                        },
                        "required": ["slot"],
                    },
                },
            },
        ]
    else:
        return [
            {
                "type": "function",
                "function": {
                    "name": "book_slot",
                    "description": (
                        "Book a time slot for your meeting. The slot must be one of: "
                        f"{slots_str}. "
                        "If the slot is already claimed by another agent, this will FAIL "
                        "with a CONFLICT response. You MUST then choose a DIFFERENT slot "
                        "and try again. Do NOT retry the same slot."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "slot": {
                                "type": "string",
                                "description": f"One of: {slots_str}",
                                "enum": slots,
                            },
                        },
                        "required": ["slot"],
                    },
                },
            },
        ]


def run_trial(cell: Cell, trial_id: int, client: LLMClient, phase: str) -> TrialResult:
    policy = ConflictPolicy(cell.conflict_policy)
    env = CalendarEnv(
        conflict_policy=policy,
        n_slots=cell.n_slots,
        block_self_rebook=cell.block_self_rebook,
    )

    tools = _make_tools(env.slots, cell.visibility)
    if cell.visibility == "visible":
        prompt_fn = make_visible_prompt
    else:
        prompt_fn = make_hidden_prompt

    # Use wed-14 as preferred if it exists in the slot list, otherwise first slot
    preferred = PREFERRED_SLOT if PREFERRED_SLOT in env.slots else env.slots[0]
    agents_config = [(f"agent-{i + 1:02d}", preferred) for i in range(cell.n_agents)]

    # Create Agent objects once — persistent identity across rounds
    agents = [
        Agent(
            name=name,
            scopes={"calendar": ["intent", "action", "need"]},
        )
        for name, _ in agents_config
    ]

    result = TrialResult(
        phase=phase,
        cell=dataclasses.asdict(cell),
        trial_id=trial_id,
        n_rounds=cell.n_rounds,
    )

    try:
        t_start = time.monotonic()
        all_round_booked: list[str] = []
        all_round_records: list[AgentRecord] = []
        round_details: list[dict] = []

        for round_num in range(1, cell.n_rounds + 1):
            if round_num > 1:
                # Advance clock by 1 hour — expires Round 1 intents (TTL=30min),
                # action marks (permanent) survive
                env.space.set_clock(env.space.now() + 3600)

            guard_before = env.guard_invocation_count
            agent_records, booked = _run_round(
                agents,
                agents_config,
                env,
                client,
                tools,
                prompt_fn,
                cell,
                round_num,
            )
            round_metrics = _compute_round_metrics(
                agent_records,
                booked,
                env,
                round_num,
                guard_before,
            )
            round_details.append(round_metrics)
            all_round_booked.extend(booked)
            all_round_records.extend(agent_records)

        t_end = time.monotonic()

        # Aggregate metrics across all rounds
        total_steps = sum(ar.steps_count for ar in all_round_records)
        total_wasted = sum(ar.wasted_attempts for ar in all_round_records)
        total_tokens: dict[str, int] = {"prompt": 0, "completion": 0}
        for ar in all_round_records:
            total_tokens["prompt"] += ar.tokens["prompt"]
            total_tokens["completion"] += ar.tokens["completion"]

        # Double bookings across ALL rounds combined
        slot_counts = Counter(all_round_booked)
        double_bookings = sum(c - 1 for c in slot_counts.values() if c > 1)

        result.double_bookings = double_bookings
        result.all_completed = all(ar.booked is not None for ar in all_round_records)
        result.total_steps = total_steps
        result.steps_per_agent = (
            total_steps / (cell.n_agents * cell.n_rounds) if cell.n_agents > 0 else 0.0
        )
        result.guard_invocations = env.guard_invocation_count
        result.wasted_attempts = total_wasted
        result.tokens = total_tokens
        result.wall_clock_seconds = t_end - t_start
        result.agents = [dataclasses.asdict(ar) for ar in all_round_records]
        result.rounds = round_details

    except Exception as e:
        result.error = str(e)

    return result


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------


def load_completed(path: Path) -> dict[str, set[int]]:
    completed: dict[str, set[int]] = {}
    if not path.exists():
        return completed
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            c = record["cell"]
            n_rounds = c.get("n_rounds", 1)
            n_slots = c.get("n_slots", 15)
            block_self_rebook = c.get("block_self_rebook", False)
            key = (
                f"{c['model']}|{c['n_agents']}|{c['visibility']}"
                f"|{c['temperature']}|{c['execution_mode']}|{c['conflict_policy']}"
                f"|{n_rounds}|{n_slots}|{block_self_rebook}"
            )
            tid = record["trial_id"]
            if tid == -1:
                # Early-stop sentinel: mark all trials as completed
                completed.setdefault(key, set()).update(range(10000))
            elif record.get("error"):
                # Errored trials are NOT marked completed — they'll be retried
                pass
            else:
                completed.setdefault(key, set()).add(tid)
    return completed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Validation Experiment Runner")
    parser.add_argument(
        "--models",
        nargs="+",
        default=["kimi-k2p5", "deepseek-v3p2", "glm-5", "gpt-oss-120b"],
        help="Model short names",
    )
    parser.add_argument("--agents", nargs="+", type=int, default=[3, 5, 10])
    parser.add_argument("--visibility", nargs="+", default=["visible", "hidden"])
    parser.add_argument("--temperature", nargs="+", type=float, default=[0.0, 0.7])
    parser.add_argument("--trials-per-cell", type=int, default=10)
    parser.add_argument("--phase", default="pilot")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--execution-mode", nargs="+", default=["sequential"])
    parser.add_argument("--conflict-policy", nargs="+", default=["highest_confidence"])
    parser.add_argument("--n-rounds", nargs="+", type=int, default=[1])
    parser.add_argument("--n-slots", nargs="+", type=int, default=[15])
    parser.add_argument(
        "--block-self-rebook",
        nargs="+",
        type=str,
        default=["false"],
        help="Whether to block agents from re-booking their own slots (true/false)",
    )
    parser.add_argument("--max-consecutive-failures", type=int, default=10)
    parser.add_argument(
        "--parallel-cells",
        type=int,
        default=12,
        help="Number of cells to run concurrently (default: 12)",
    )
    args = parser.parse_args()

    block_self_rebook_vals = [
        v.lower() in ("true", "1", "yes") for v in args.block_self_rebook
    ]

    cells = [
        Cell(
            model=m,
            n_agents=n,
            visibility=v,
            temperature=t,
            execution_mode=e,
            conflict_policy=p,
            n_rounds=r,
            n_slots=s,
            block_self_rebook=b,
        )
        for m in args.models
        for n in args.agents
        for v in args.visibility
        for t in args.temperature
        for e in args.execution_mode
        for p in args.conflict_policy
        for r in args.n_rounds
        for s in args.n_slots
        for b in block_self_rebook_vals
    ]

    output_dir = Path(__file__).parent
    output_file = output_dir / (
        args.output or f"results_{args.phase}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    )

    completed = load_completed(output_file) if args.resume else {}

    # Create one LLMClient per model
    base_config = LLMConfig.from_env()
    clients: dict[str, LLMClient] = {}
    for model_short in args.models:
        if model_short in EXTERNAL_MODELS:
            entry = EXTERNAL_MODELS[model_short]
            api_key = os.environ.get(entry.api_key_env, "")
            if not api_key:
                raise RuntimeError(
                    f"Model '{model_short}' requires {entry.api_key_env} env var"
                )
            clients[model_short] = LLMClient(
                LLMConfig(
                    base_url=entry.base_url,
                    api_key=api_key,
                    model=entry.model_id,
                )
            )
        else:
            clients[model_short] = LLMClient(
                LLMConfig(
                    base_url=base_config.base_url,
                    api_key=base_config.api_key,
                    model=resolve_model_id(model_short),
                )
            )

    total_cells = len(cells)
    total_trials = total_cells * args.trials_per_cell
    completed_count = sum(len(v) for v in completed.values())

    print(f"Validation Runner — Phase: {args.phase}")
    print(
        f"Cells: {total_cells}, Trials/cell: {args.trials_per_cell}, Total: {total_trials}"
    )
    if completed_count:
        print(f"Resuming: {completed_count} trials already completed")
    print("-" * 60)

    safety_failures = 0
    trial_count = 0
    write_lock = threading.Lock()
    counter_lock = threading.Lock()

    def run_cell(cell_idx: int, cell: Cell) -> tuple[int, int]:
        """Run all trials for a single cell. Returns (trials_run, safety_failures)."""
        nonlocal trial_count
        cell_key = cell.key()
        completed_trials = completed.get(cell_key, set())
        client = clients[cell.model]
        consecutive_failures = 0
        local_trials = 0
        local_safety = 0

        for trial_id in range(args.trials_per_cell):
            if trial_id in completed_trials:
                continue

            with counter_lock:
                trial_count += 1

            extra_tags = ""
            if cell.n_rounds > 1:
                extra_tags += f" rounds={cell.n_rounds}"
            if cell.n_slots != 15:
                extra_tags += f" slots={cell.n_slots}"
            if cell.block_self_rebook:
                extra_tags += " block_self_rebook"
            label = (
                f"[cell {cell_idx + 1}/{total_cells} | trial {trial_id + 1}/{args.trials_per_cell}] "
                f"{cell.model} N={cell.n_agents} {cell.visibility} "
                f"t={cell.temperature} {cell.execution_mode} {cell.conflict_policy}{extra_tags}"
            )

            result = run_trial(cell, trial_id, client, args.phase)

            with write_lock:
                with open(output_file, "a") as f:
                    f.write(json.dumps(dataclasses.asdict(result)) + "\n")

            status = "OK" if result.all_completed and not result.error else "FAIL"
            if result.double_bookings > 0:
                status = "SAFETY_VIOLATION"
                local_safety += 1

            tokens_total = result.tokens["prompt"] + result.tokens["completion"]
            print(
                f"\n{label}\n"
                f"  {status} | steps/agent={result.steps_per_agent:.1f} "
                f"guard={result.guard_invocations} wasted={result.wasted_attempts} "
                f"tokens={tokens_total} time={result.wall_clock_seconds:.1f}s"
            )
            if result.error:
                print(f"  ERROR: {result.error}")

            if result.error:
                consecutive_failures += 1
                if consecutive_failures >= args.max_consecutive_failures:
                    print(
                        f"  EARLY STOP: {consecutive_failures} consecutive failures in {cell_key}"
                    )
                    sentinel = TrialResult(
                        phase=args.phase,
                        cell=dataclasses.asdict(cell),
                        trial_id=-1,
                        error=f"early_stopped_after_{consecutive_failures}_failures",
                    )
                    with write_lock:
                        with open(output_file, "a") as f:
                            f.write(json.dumps(dataclasses.asdict(sentinel)) + "\n")
                    break
            else:
                consecutive_failures = 0

            local_trials += 1

        return local_trials, local_safety

    parallel = args.parallel_cells
    if parallel <= 1:
        # Sequential fallback
        for cell_idx, cell in enumerate(cells):
            _t, _s = run_cell(cell_idx, cell)
            safety_failures += _s
    else:
        print(f"Running up to {parallel} cells in parallel")
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(run_cell, idx, cell): idx
                for idx, cell in enumerate(cells)
            }
            for f in as_completed(futures):
                _t, _s = f.result()
                safety_failures += _s

    print(f"\n{'=' * 60}")
    print(f"Done. {trial_count} trials completed.")
    print(f"Safety violations: {safety_failures}")
    print(f"Results: {output_file}")


if __name__ == "__main__":
    main()
