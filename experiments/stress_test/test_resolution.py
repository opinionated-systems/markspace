# -*- coding: utf-8 -*-
"""
The stress test's between-round resolution of parking and the boardroom: the
harness grants winners as the principal and releases the other intents, and
every booking is recorded under a lease (Spec 6.5).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from markspace import Action, Agent, ClaimState, Intent

# run.py imports its sibling modules (scenario) by name.
sys.path.insert(0, str(Path(__file__).resolve().parent))
run = importlib.import_module("run")
scenario = importlib.import_module("scenario")


def profile(name: str, scope: str, target: dict[str, str]) -> SimpleNamespace:
    agent = Agent(name=name, scopes={scope: ["intent", "action"]})
    item = run.ManifestItem(
        scope=scope, description="", target=target, earliest_round=0
    )
    return SimpleNamespace(agent=agent, manifest=[item])


def actions(env: Any, scope: str) -> list[Action]:
    return [
        m for m in env.space.read(scope=scope) if isinstance(m, Action) and not m.failed
    ]


def assert_every_intent_closed(env: Any) -> None:
    """Every intent the bots judged ended acted or released, none pending."""
    intents = [m for m in env.space._marks.values() if isinstance(m, Intent)]
    assert intents
    for i in intents:
        assert env.guard.claim_state(i.id) in (ClaimState.ACTED, ClaimState.RELEASED)


def test_parking_winners_book_their_spots_under_grants() -> None:
    env = run.OfficeEnv()
    profiles = [profile(f"p{i}", "parking", {"day": "mon"}) for i in range(3)]
    for i, p in enumerate(profiles):
        ok, msg = env.request_parking(p.agent, "mon", 0.5 + i / 10)
        assert not ok and msg.startswith("BLOCKED")
    run.resolve_parking_priority(env, profiles, "mon")
    booked = actions(env, "parking")
    assert sorted(a.resource for a in booked) == [f"mon/spot-{n}" for n in (1, 2, 3)]
    assert all(p.manifest[0].completed for p in profiles)
    assert_every_intent_closed(env)


def test_a_refused_spot_claim_passes_the_spot_on() -> None:
    env = run.OfficeEnv()
    first, second = (profile(n, "parking", {"day": "mon"}) for n in ("first", "second"))
    env.request_parking(first.agent, "mon", 0.9)
    env.request_parking(second.agent, "mon", 0.4)
    env.guard.get_or_create_barrier(first.agent.id).narrow("parking", "intent")
    run.resolve_parking_priority(env, [first, second], "mon")
    assert first.manifest[0].failed and not first.manifest[0].completed
    assert second.manifest[0].completed
    assert [(a.agent_id, a.resource) for a in actions(env, "parking")] == [
        (second.agent.id, "mon/spot-1")
    ]


def test_parking_never_books_past_its_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scenario, "PARKING_SPOTS", 1)
    env = run.OfficeEnv()
    profiles = [profile(f"p{i}", "parking", {"day": "mon"}) for i in range(3)]
    for i, p in enumerate(profiles):
        env.request_parking(p.agent, "mon", 0.1 + i / 10)
    run.resolve_parking_priority(env, profiles, "mon")
    assert [a.agent_id for a in actions(env, "parking")] == [profiles[2].agent.id]
    assert [p.manifest[0].failed for p in profiles] == [True, True, False]


def test_the_boardroom_winner_is_granted_and_the_rest_released() -> None:
    env = run.OfficeEnv()
    target = {"day": "mon", "block": "AM"}
    low, high = (profile(n, "rooms/exec", target) for n in ("low", "high"))
    for p, conf in ((low, 0.3), (high, 0.9)):
        ok, msg = env.book_boardroom(p.agent, "mon", "AM", conf)
        assert not ok and msg.startswith("BLOCKED")
    run.resolve_boardroom_conflicts(env, [low, high])
    assert [a.agent_id for a in actions(env, "rooms/exec")] == [high.agent.id]
    assert high.manifest[0].completed and not low.manifest[0].completed
    later = env.book_boardroom(low.agent, "mon", "AM", 0.99)
    assert later[1].startswith("CONFLICT")
    assert_every_intent_closed(env)


def test_the_boardroom_holder_asking_again_is_not_granted_again() -> None:
    env = run.OfficeEnv()
    holder = profile("holder", "rooms/exec", {"day": "mon", "block": "AM"})
    env.book_boardroom(holder.agent, "mon", "AM", 0.9)
    run.resolve_boardroom_conflicts(env, [holder])
    ok, msg = env.book_boardroom(holder.agent, "mon", "AM", 0.9)
    assert not ok and msg.startswith("ALREADY YOURS")
    assert run.resolve_boardroom_conflicts(env, [holder]) == []
    assert [a.agent_id for a in actions(env, "rooms/exec")] == [holder.agent.id]


def test_a_second_resolution_on_one_day_books_only_new_requests() -> None:
    env = run.OfficeEnv()
    first, second = (profile(n, "parking", {"day": "mon"}) for n in ("a", "b"))
    env.request_parking(first.agent, "mon", 0.9)
    run.resolve_parking_priority(env, [first, second], "mon")
    env.request_parking(second.agent, "mon", 0.5)
    run.resolve_parking_priority(env, [first, second], "mon")
    assert sorted((a.resource, a.agent_id) for a in actions(env, "parking")) == [
        ("mon/spot-1", first.agent.id),
        ("mon/spot-2", second.agent.id),
    ]
    assert_every_intent_closed(env)


def test_no_spot_left_fails_the_item_and_releases_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scenario, "PARKING_SPOTS", 1)
    env = run.OfficeEnv()
    late = profile("b", "parking", {"day": "mon"})
    ok, msg = env.request_parking(late.agent, "mon", 0.5)
    assert msg.startswith("BLOCKED")
    # The only spot goes to another agent before the resolution runs.
    holder = Agent(name="holder", scopes={"parking": ["intent", "action"]})
    claim = env.guard.pre_action(holder, "parking", "mon/spot-1", "book", 0.5)
    assert claim.intent_id is not None
    env.guard.grant(claim.intent_id, env.principal_token)
    env.guard.post_action(
        holder, "parking", "mon/spot-1", "booked", "ok", intent_id=claim.intent_id
    )
    run.resolve_parking_priority(env, [late], "mon")
    assert late.manifest[0].failed
    assert_every_intent_closed(env)


def test_winners_get_the_lowest_free_spots() -> None:
    env = run.OfficeEnv()
    # Another agent's action holds spot 2, and a third agent's lease holds
    # spot 3: the two winners get spots 1 and 4.
    for name, spot, act in (
        ("booker", "mon/spot-2", True),
        ("leaser", "mon/spot-3", False),
    ):
        holder = Agent(name=name, scopes={"parking": ["intent", "action"]})
        claim = env.guard.pre_action(holder, "parking", spot, "book", 0.5)
        assert claim.intent_id is not None
        env.guard.grant(claim.intent_id, env.principal_token)
        if act:
            env.guard.post_action(
                holder, "parking", spot, "booked", "ok", intent_id=claim.intent_id
            )
    first, second = (profile(n, "parking", {"day": "mon"}) for n in ("w1", "w2"))
    env.request_parking(first.agent, "mon", 0.9)
    env.request_parking(second.agent, "mon", 0.8)
    run.resolve_parking_priority(env, [first, second], "mon")
    got = {a.agent_id: a.resource for a in actions(env, "parking")}
    assert got[first.agent.id] == "mon/spot-1"
    assert got[second.agent.id] == "mon/spot-4"
    assert first.manifest[0].completed and second.manifest[0].completed
    mine = [
        m
        for m in env.space._marks.values()
        if isinstance(m, Intent) and m.agent_id == second.agent.id
    ]
    assert {env.guard.claim_state(m.id) for m in mine} == {
        ClaimState.ACTED,
        ClaimState.RELEASED,
    }


def test_a_boardroom_grant_that_fails_records_nothing() -> None:
    env = run.OfficeEnv()
    holder = Agent(name="holder", scopes={"rooms/exec": ["intent", "action"]})
    resource = f"{run.EXEC_ROOM}/mon/AM"
    claim = env.guard.pre_action(holder, "rooms/exec", resource, "book", 0.5)
    assert claim.intent_id is not None
    env.guard.grant(claim.intent_id, env.principal_token)  # holder's lease
    target = {"day": "mon", "block": "AM"}
    low, high = (profile(n, "rooms/exec", target) for n in ("low", "high"))
    env.book_boardroom(low.agent, "mon", "AM", 0.3)
    env.book_boardroom(high.agent, "mon", "AM", 0.9)
    log = run.resolve_boardroom_conflicts(env, [low, high])
    assert actions(env, "rooms/exec") == []
    assert any("not granted" in line for line in log)
    waiting = [
        m
        for m in env.space._marks.values()
        if isinstance(m, Intent) and m.agent_id in (low.agent.id, high.agent.id)
    ]
    assert len(waiting) == 2
    assert all(env.guard.claim_state(m.id) is None for m in waiting)
    assert not high.manifest[0].completed


def test_a_barred_top_bidder_is_released_and_the_next_one_wins() -> None:
    """A barrier that revokes actions makes grant() DENIED for the top
    bidder; its intent is released and the next bidder gets the room."""
    env = run.OfficeEnv()
    target = {"day": "mon", "block": "AM"}
    low, high = (profile(n, "rooms/exec", target) for n in ("low", "high"))
    for p, conf in ((low, 0.3), (high, 0.9)):
        ok, msg = env.book_boardroom(p.agent, "mon", "AM", conf)
        assert not ok and msg.startswith("BLOCKED")
    env.guard.get_or_create_barrier(high.agent.id).narrow("rooms/exec", "action")
    log = run.resolve_boardroom_conflicts(env, [low, high])
    assert [a.agent_id for a in actions(env, "rooms/exec")] == [low.agent.id]
    assert low.manifest[0].completed and not high.manifest[0].completed
    assert any("not granted to high" in line for line in log)
    assert_every_intent_closed(env)


def test_a_restricted_agent_s_lunch_order_reports_the_refusal() -> None:
    """The lunch tool returns the guard's DENIED reason and writes no false
    "fully booked" need."""
    env = run.OfficeEnv()
    agent = Agent(name="eng-1", scopes={"lunch": ["intent", "action", "need"]})
    barrier = env.guard.get_or_create_barrier(agent.id)
    barrier.narrow_all("action")
    barrier.require_need_all()
    ok, msg = env.order_lunch(agent, "mon", "12:00")
    assert not ok and msg.startswith("DENIED") and "action revoked" in msg
    needs = [m for m in env.space._marks.values() if m.mark_type.value == "need"]
    assert needs == []


def test_a_lost_lunch_race_writes_no_fully_booked_need(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other agents hold every free slot: the order fails as a race, and
    only a lunch with every slot ordered is fully booked."""
    monkeypatch.setattr(scenario, "LUNCH_TYPE_A_PER_WINDOW", 1)
    monkeypatch.setattr(scenario, "LUNCH_TYPE_B_PER_WINDOW", 1)
    env = run.OfficeEnv()
    wday = env._wday("mon")
    for label in ("A", "B"):
        holder = Agent(name=f"h{label}", scopes={"lunch": ["intent", "action"]})
        held = env.guard.pre_action(holder, "lunch", f"{label}/{wday}/12:00/1", "order")
        assert held.intent_id is not None
    agent = Agent(name="eng-1", scopes={"lunch": ["intent", "action", "need"]})
    ok, msg = env.order_lunch(agent, "mon", "12:00")
    assert not ok and msg.startswith("CONFLICT")
    needs = [m for m in env.space._marks.values() if m.mark_type.value == "need"]
    assert needs == []


def test_two_requests_of_one_agent_get_one_spot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scenario, "PARKING_SPOTS", 2)
    env = run.OfficeEnv()
    twice, other = (profile(n, "parking", {"day": "mon"}) for n in ("twice", "other"))
    env.request_parking(twice.agent, "mon", 0.9)
    env.request_parking(twice.agent, "mon", 0.8)  # a second request, still waiting
    env.request_parking(other.agent, "mon", 0.5)
    run.resolve_parking_priority(env, [twice, other], "mon")
    holders = [a.agent_id for a in actions(env, "parking")]
    assert sorted(map(str, holders)) == sorted(
        map(str, [twice.agent.id, other.agent.id])
    )
    assert twice.manifest[0].completed and other.manifest[0].completed
    assert_every_intent_closed(env)


def test_an_agent_holding_a_spot_gets_no_second_one() -> None:
    env = run.OfficeEnv()
    p = profile("p", "parking", {"day": "mon"})
    env.request_parking(p.agent, "mon", 0.9)
    # p gets a spot another way before the resolution runs.
    claim = env.guard.pre_action(p.agent, "parking", "mon/spot-7", "book", 0.5)
    assert claim.intent_id is not None
    env.guard.grant(claim.intent_id, env.principal_token)
    env.guard.post_action(
        p.agent, "parking", "mon/spot-7", "booked", "ok", intent_id=claim.intent_id
    )
    run.resolve_parking_priority(env, [p], "mon")
    assert [a.resource for a in actions(env, "parking")] == ["mon/spot-7"]
    assert_every_intent_closed(env)
