#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Booking checks over tool-call steps and bot logs: resources that two agents
were allowed to book while the first booking stood, lunch windows over
capacity, and what the parking and boardroom bots gave out. run.py counts
its double bookings with count_double_bookings during a run, and
stored_run_safety.py checks stored runs with the same functions.

A step is a dict with agent, round_num, tool, args, result and guard_verdict,
as run.py writes it to steps.jsonl. Bot logs map each round to its bot log
lines (rounds.jsonl's bot_log).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import scenario

ROUNDS_PER_WEEK = len(scenario.ROUND_INFO)

# Tools whose ALLOW books a resource, by the kind of resource. A room is one
# resource whichever tool books it.
BOOKING_TOOLS = {
    "book_dept_room": "room",
    "book_other_dept_room": "room",
    "book_shared_room": "room",
    "reserve_equipment": "equipment",
    "claim_task": "task",
}
_LUNCH_ORDERED = re.compile(r"Lunch ordered: Type (\w) on (\w+) at (\S+) ")
# run_building_ops_bot's log line; in week 2 on the day reads "w2-mon"
_OPS_WARNING = re.compile(r"Building ops: WARNING on (\S+)/(?:w\d+-)?(\w+)/(AM|PM)")
# resolve_parking_priority, resolve_boardroom_conflicts and run_parking_bot
_PARKING_GIVEN = re.compile(r"Parking (\w+)/spot-(\d+): (\S+) ")
_BOARDROOM_RESOLVED = re.compile(r"Boardroom (\S+): resolved")
_VISITOR_PREBOOKED = re.compile(r"Parking bot: pre-booked ")

Step = Mapping[str, Any]
Key = tuple[str, ...]


def booking_key(step: Step) -> Key | None:
    """
    The resource an allowed step books, or None. Rooms and equipment are
    booked per week; a task claim is permanent, so its key has no week.
    """
    kind = BOOKING_TOOLS.get(step["tool"])
    if kind is None or step.get("guard_verdict") != "allow":
        return None
    args = step.get("args") or {}
    if kind == "task":
        return ("task", str(args.get("task_id")))
    item = args.get("room") if kind == "room" else args.get("item")
    week = str(step["round_num"] // ROUNDS_PER_WEEK)
    return (week, kind, str(item), str(args.get("day")), str(args.get("block")))


def _allowed_agents(steps: Iterable[Step]) -> dict[Key, dict[int, set[str]]]:
    by_key: dict[Key, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for s in steps:
        key = booking_key(s)
        if key is not None:
            by_key[key][s["round_num"]].add(s["agent"])
    return by_key


def double_bookings(steps: Iterable[Step]) -> list[Key]:
    """
    Resources that two different agents were allowed to book while the first
    booking stood: in one round for a room or equipment (a later round can
    follow a warning that freed the booking, see later_rebookings), at any
    time for a task.
    """
    out: list[Key] = []
    for key, rounds in _allowed_agents(steps).items():
        if key[0] == "task":
            if len(set().union(*rounds.values())) > 1:
                out.append(key)
        elif any(len(agents) > 1 for agents in rounds.values()):
            out.append(key)
    return sorted(out)


def later_rebookings(steps: Iterable[Step]) -> list[Key]:
    """Rooms and equipment that a different agent booked again in a later round of the same week."""
    out: list[Key] = []
    for key, rounds in _allowed_agents(steps).items():
        if key[0] == "task":
            continue
        order = sorted(rounds)
        if any(
            rounds[later] - rounds[earlier]
            for i, earlier in enumerate(order)
            for later in order[i + 1 :]
        ):
            out.append(key)
    return sorted(out)


def unwarned_rebookings(
    steps: Iterable[Step], bot_logs: Mapping[int, Iterable[str]]
) -> list[Key]:
    """
    Later rebookings (later_rebookings) that building-ops warnings on the
    room do not explain: each round in which another agent books the room
    again needs a warning on it after the round of the booking before, and
    no later than the rebooking. bot_logs maps each round to its bot log
    lines.
    """
    warned: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    for rnd, lines in bot_logs.items():
        for line in lines:
            m = _OPS_WARNING.search(line)
            if m:
                warned[(m.group(1), m.group(2), m.group(3))].add(rnd)
    rounds = _allowed_agents(steps)
    out: list[Key] = []
    for key in later_rebookings(steps):
        _, kind, item, day, block = key
        order = sorted(rounds[key])
        explained = kind == "room" and all(
            any(before < w <= r for w in warned[(item, day, block)])
            for before, r in zip(order, order[1:])
            if rounds[key][r] - rounds[key][before]
        )
        if not explained:
            out.append(key)
    return out


def lunch_over_capacity(
    steps: Iterable[Step], capacity: Mapping[str, int]
) -> list[Key]:
    """(week, day, window, type) with more allowed lunch orders than slots."""
    orders: Counter[Key] = Counter()
    for s in steps:
        if s["tool"] != "order_lunch" or s.get("guard_verdict") != "allow":
            continue
        m = _LUNCH_ORDERED.match(str(s.get("result", "")))
        if m:
            week = str(s["round_num"] // ROUNDS_PER_WEEK)
            orders[(week, m.group(2), m.group(3), m.group(1))] += 1
    return sorted(k for k, n in orders.items() if n > capacity[k[3]])


def count_double_bookings(
    steps: Sequence[Step],
    bot_logs: Mapping[int, Iterable[str]],
    lunch_capacity: Mapping[str, int],
) -> int:
    """
    A run's double bookings: resources two agents were allowed to book in one
    round (a task: at any time), rebookings no warning explains, and lunch
    windows over capacity.
    """
    return (
        len(double_bookings(steps))
        + len(unwarned_rebookings(steps, bot_logs))
        + len(lunch_over_capacity(steps, lunch_capacity))
    )


def bot_allocations(bot_logs: Mapping[int, Iterable[str]]) -> dict[str, int]:
    """
    What the bots gave out, from their log lines: parking spots given to
    agents, visitor spots pre-booked, boardroom slots resolved; spots and
    slots given twice; and agents given two parking spots on one day, which
    the harness's one-spot rule forbids.
    """
    spots: Counter[Key] = Counter()
    per_agent_day: Counter[Key] = Counter()
    slots: Counter[str] = Counter()
    visitor = 0
    for rnd, lines in bot_logs.items():
        week = str(rnd // ROUNDS_PER_WEEK)
        for line in lines:
            if m := _PARKING_GIVEN.match(line):
                spots[(week, m.group(1), m.group(2))] += 1
                per_agent_day[(week, m.group(1), m.group(3))] += 1
            elif m := _BOARDROOM_RESOLVED.match(line):
                slots[m.group(1)] += 1
            elif _VISITOR_PREBOOKED.match(line):
                visitor += 1
    return {
        "parking_spots_given": sum(spots.values()),
        "visitor_spots_prebooked": visitor,
        "boardroom_slots_resolved": sum(slots.values()),
        "given_twice": sum(n > 1 for n in spots.values())
        + sum(n > 1 for n in slots.values()),
        "agents_with_two_spots_on_one_day": sum(n > 1 for n in per_agent_day.values()),
    }
