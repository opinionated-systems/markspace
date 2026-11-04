# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Lease and Fencing Tests

Tests for grants (Spec Section 6.5): the grant condition (G1), batch judging
at a boundary (G2, G4), immediate mode (G3), principal grant and release
(G5), fencing at the action (G6), lease length (G7), fencing tokens (G8),
supersession by its author (G10), the lease clock, gc, and properties P71 to
P76.
"""

from __future__ import annotations

import math
import threading
import uuid
import pytest

from markspace import (
    Action,
    Agent,
    ClaimState,
    ConflictPolicy,
    FencingToken,
    Guard,
    GuardVerdict,
    Intent,
    MarkSpace,
    Need,
    NotGrantedError,
    Observation,
    Scope,
    ScopeError,
    Source,
    Warning,
)
from markspace.telemetry import (
    METRIC_LEASES_EXPIRED,
    METRIC_LEASES_GRANTED,
    METRIC_LEASES_LOST,
    InMemorySink,
)

T0 = 1_000_000.0
TTL = 30 * 60  # the default intent TTL


def agent(name: str, *scopes: str) -> Agent:
    return Agent(name=name, scopes={s: ["intent", "action", "need"] for s in scopes})


def tree(
    *, deferred: tuple[str, ...] = (), yield_all: tuple[str, ...] = ()
) -> MarkSpace:
    names = ("corp", "corp/a", "corp/b", "corp/a/x")
    s = MarkSpace(
        scopes=[
            Scope(
                name=n,
                deferred=n in deferred,
                conflict_policy=(
                    ConflictPolicy.YIELD_ALL
                    if n in yield_all
                    else ConflictPolicy.HIGHEST_CONFIDENCE
                ),
            )
            for n in names
        ]
    )
    s.set_clock(T0)
    return s


def state(guard: Guard, intent_id: uuid.UUID | None) -> ClaimState | None:
    assert intent_id is not None
    claim = guard._claims.get(intent_id)
    return None if claim is None else claim.state


class TestGrantCondition:
    """G1: another agent's lease or held action holds competing scopes, not siblings."""

    def test_a_lease_holds_its_scope_ancestors_and_descendants(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp/a")
        assert guard.pre_action(a, "corp/a", "r", "use").verdict == GuardVerdict.ALLOW
        for name, scope in (("p", "corp"), ("c", "corp/a"), ("x", "corp/a/x")):
            d = guard.pre_action(agent(name, scope), scope, "r", "use", 0.99)
            assert d.verdict == GuardVerdict.CONFLICT, scope
        d = guard.pre_action(agent("b", "corp/b"), "corp/b", "r", "use")
        assert d.verdict == GuardVerdict.ALLOW  # a sibling scope

    def test_an_agents_own_lease_does_not_hold_it_off(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp", "corp/a")
        assert guard.pre_action(a, "corp/a", "r", "use").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(a, "corp", "r", "use").verdict == GuardVerdict.ALLOW


class TestImmediateMode:
    """G3: the order of the checks in an immediate scope."""

    def test_a_held_action_refuses_before_any_intent_is_written(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp/a")
        d = guard.pre_action(a, "corp", "r", "use")
        guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)
        before = len(s.get_intents("corp", "r"))
        assert (
            guard.pre_action(b, "corp/a", "r", "use").verdict == GuardVerdict.CONFLICT
        )
        assert len(s.get_intents("corp", "r")) == before

    def test_a_waiting_outer_candidate_beats_an_inner_intent(self) -> None:
        s = tree(deferred=("corp",))
        guard = Guard(s)
        p, a = agent("p", "corp"), agent("a", "corp/a")
        dp = guard.pre_action(p, "corp", "r", "use", 0.1)
        assert dp.verdict == GuardVerdict.BLOCKED
        d = guard.pre_action(a, "corp/a", "r", "use", 0.9)
        assert d.verdict == GuardVerdict.CONFLICT
        assert d.winning_intent is not None and d.winning_intent.id == dp.intent_id
        assert state(guard, d.intent_id) == ClaimState.LOST

    def test_a_waiting_inner_candidate_does_not_stop_an_outer_intent(self) -> None:
        s = tree(deferred=("corp/a",))
        guard = Guard(s)
        a, p = agent("a", "corp/a"), agent("p", "corp")
        da = guard.pre_action(a, "corp/a", "r", "use", 0.9)
        assert (
            guard.pre_action(p, "corp", "r", "use", 0.1).verdict == GuardVerdict.ALLOW
        )
        at_a = guard.resolve_deferred("corp/a", "r", agents={a.id: a, p.id: p})
        assert at_a[a.id].verdict == GuardVerdict.BLOCKED  # waits for p's lease
        assert state(guard, da.intent_id) is None  # still pending

    def test_a_yield_all_pair_with_a_lease_goes_to_the_principal(self) -> None:
        s = tree(yield_all=("corp/a",))
        guard = Guard(s)
        p, a = agent("p", "corp"), agent("a", "corp/a")
        assert guard.pre_action(p, "corp", "r", "use").verdict == GuardVerdict.ALLOW
        d = guard.pre_action(a, "corp/a", "r", "use")
        assert d.verdict == GuardVerdict.BLOCKED
        assert state(guard, d.intent_id) is None  # pending: the principal decides
        assert len(s.read(scope="corp/a", mark_type=None)) >= 1

    def test_a_lone_intent_in_a_yield_all_scope_is_granted(self) -> None:
        s = tree(yield_all=("corp/a",))
        guard = Guard(s)
        d = guard.pre_action(agent("a", "corp/a"), "corp/a", "r", "use")
        assert d.verdict == GuardVerdict.ALLOW
        assert d.fencing_token is not None


class TestBoundary:
    """G2 and G4: batch judging, waiting candidates and one grant per agent."""

    def test_a_candidate_beaten_only_by_a_waiting_one_stays_pending(self) -> None:
        s = tree(deferred=("corp", "corp/b"))
        guard = Guard(s)
        a, o, b = agent("a", "corp/a"), agent("o", "corp"), agent("b", "corp/b")
        agents = {x.id: x for x in (a, o, b)}
        da = guard.pre_action(a, "corp/a", "r", "use")
        assert da.verdict == GuardVerdict.ALLOW
        do = guard.pre_action(o, "corp", "r", "use")
        db = guard.pre_action(b, "corp/b", "r", "use")
        first = guard.resolve_deferred("corp", "r", agents=agents)
        assert first[o.id].verdict == GuardVerdict.BLOCKED  # a's lease
        assert first[b.id].verdict == GuardVerdict.BLOCKED  # o waits too
        assert state(guard, db.intent_id) is None
        guard.post_action(a, "corp/a", "r", "used", "ok", intent_id=da.intent_id)
        second = guard.resolve_deferred("corp", "r", agents=agents)
        assert second[o.id].verdict == GuardVerdict.CONFLICT  # a's action
        assert second[b.id].verdict == GuardVerdict.ALLOW
        assert state(guard, do.intent_id) == ClaimState.LOST

    def test_one_grant_per_agent_per_boundary_then_the_next(self) -> None:
        s = tree(deferred=("corp", "corp/a", "corp/b"))
        guard = Guard(s)
        x = agent("x", "corp/a", "corp/b")
        w1 = guard.pre_action(x, "corp/a", "r", "use")
        s.set_clock(T0 + 1)
        w2 = guard.pre_action(x, "corp/b", "r", "use")
        first = guard.resolve_deferred("corp", "r", agents={x.id: x})
        assert first[x.id].verdict == GuardVerdict.ALLOW
        assert first[x.id].intent_id == w1.intent_id  # the earliest
        assert state(guard, w2.intent_id) is None
        second = guard.resolve_deferred("corp", "r", agents={x.id: x})
        assert second[x.id].intent_id == w2.intent_id
        assert second[x.id].verdict == GuardVerdict.ALLOW

    def test_the_outermost_winner_of_an_agent_is_granted_first(self) -> None:
        s = tree(deferred=("corp", "corp/b"))
        guard = Guard(s)
        x = agent("x", "corp/b")
        y = agent("y", "corp/a/x")
        guard.pre_action(y, "corp/a/x", "r", "use")  # immediate: granted
        dx = guard.pre_action(x, "corp/b", "r", "use")
        out = guard.resolve_deferred("corp", "r", agents={x.id: x, y.id: y})
        assert out[x.id].verdict == GuardVerdict.ALLOW
        assert out[x.id].intent_id == dx.intent_id

    def test_a_refused_candidate_is_lost(self) -> None:
        s = tree(deferred=("corp",))
        guard = Guard(s)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        guard.get_or_create_barrier(a.id).narrow("corp", "intent")
        out = guard.resolve_deferred("corp", "r", agents={a.id: a})
        assert out[a.id].verdict == GuardVerdict.DENIED
        assert state(guard, d.intent_id) == ClaimState.LOST


class TestPrincipal:
    """G5: grant and release."""

    def test_the_principal_grants_an_escalated_intent(self) -> None:
        s = tree(yield_all=("corp/a",))
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token)
        a, b = agent("a", "corp/a"), agent("b", "corp/a")
        da = guard.pre_action(a, "corp/a", "r", "use")
        db = guard.pre_action(b, "corp/a", "r", "use")
        assert db.verdict == GuardVerdict.BLOCKED
        assert db.intent_id is not None and da.intent_id is not None
        assert guard.grant(db.intent_id, uuid.uuid4()).verdict == GuardVerdict.DENIED
        assert (
            guard.grant(db.intent_id, token).verdict == GuardVerdict.CONFLICT
        )  # a's lease
        assert guard.release(da.intent_id, token) is True
        granted = guard.grant(db.intent_id, token)
        assert granted.verdict == GuardVerdict.ALLOW
        assert granted.fencing_token is not None
        guard.post_action(b, "corp/a", "r", "used", "ok", intent_id=db.intent_id)
        assert guard.grant(db.intent_id, token).verdict == GuardVerdict.CONFLICT

    def test_release_closes_a_pending_intent_and_refuses_a_closed_one(self) -> None:
        s = tree(deferred=("corp",))
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        assert d.intent_id is not None
        assert guard.release(d.intent_id, uuid.uuid4()) is False
        assert guard.release(d.intent_id, token) is True
        assert guard.release(d.intent_id, token) is False
        assert guard.resolve_deferred("corp", "r", agents={a.id: a}) == {}


class TestFencingAtTheAction:
    """G6: an action is recorded only under its live lease."""

    def test_an_expired_lease_records_nothing(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        s.set_clock(T0 + TTL)  # the lease ends at its expiry
        assert guard.pre_action(b, "corp", "r", "use").verdict == GuardVerdict.ALLOW
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)
        assert s.get_actions("corp", "r") == []
        assert state(guard, d.intent_id) == ClaimState.EXPIRED

    def test_a_lease_covers_only_its_agent_scope_and_resource(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp/a"), agent("b", "corp/a", "corp/b")
        d = guard.pre_action(a, "corp/a", "r", "use")
        with pytest.raises(NotGrantedError):
            guard.post_action(b, "corp/a", "r", "used", "ok", intent_id=d.intent_id)
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp/a", "other", "used", "ok", intent_id=d.intent_id)

    def test_a_direct_claim_needs_an_uncontested_immediate_scope(self) -> None:
        s = tree(deferred=("corp/b",), yield_all=("corp/a/x",))
        guard = Guard(s)
        a = agent("a", "corp/a", "corp/b", "corp/a/x")
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp/b", "r", "used", "ok")  # deferred
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp/a/x", "r2", "used", "ok")  # YIELD_ALL
        guard.pre_action(agent("p", "corp"), "corp", "r3", "use")
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp/a", "r3", "used", "ok")  # p's lease
        guard.post_action(a, "corp/a", "r4", "used", "ok")  # uncontested
        assert len(s.get_actions("corp/a", "r4")) == 1

    def test_a_lease_does_not_cover_another_scope_of_its_agent(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp", "corp/a")
        d = guard.pre_action(a, "corp/a", "r", "use")
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)

    def test_a_direct_claim_waits_for_another_agents_pending_intent(self) -> None:
        s = tree(deferred=("corp/b",))
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp/b")
        guard.pre_action(b, "corp/b", "r", "use")  # pending at its boundary
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp", "r", "used", "ok")
        assert s.get_actions("corp", "r") == []

    def test_a_direct_claim_with_a_live_lease_acts_under_it(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        guard.post_action(a, "corp", "r", "used", "ok")
        assert state(guard, d.intent_id) == ClaimState.ACTED

    def test_a_failed_action_never_raises_and_ends_the_lease(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        s.set_clock(T0 + TTL + 5)
        guard.post_action(
            a, "corp", "r", "used", "x", intent_id=d.intent_id, failed=True
        )
        s.set_clock(T0)
        d2 = guard.pre_action(a, "corp", "r2", "use")
        guard.post_action(
            a, "corp", "r2", "used", "x", intent_id=d2.intent_id, failed=True
        )
        assert state(guard, d2.intent_id) == ClaimState.RELEASED
        assert guard.pre_action(b, "corp", "r2", "use").verdict == GuardVerdict.ALLOW

    def test_another_mark_of_its_agent_superseding_the_intent_releases_the_lease(
        self,
    ) -> None:
        # Spec 2.2, 6.5, P74: a mark that is not an action ends the lease too.
        for superseding in (
            lambda iid: Intent(
                scope="corp", resource="r", action="use", supersedes=iid
            ),
            lambda iid: Need(scope="corp", question="which room?", supersedes=iid),
        ):
            s = tree()
            guard = Guard(s)
            a = agent("a", "corp")
            d = guard.pre_action(a, "corp", "r", "use")
            assert state(guard, d.intent_id) == ClaimState.GRANTED
            s.write(a, superseding(d.intent_id))
            assert state(guard, d.intent_id) == ClaimState.RELEASED

    def test_a_failed_action_without_an_intent_id_ends_the_agents_lease(self) -> None:
        # G6: as a successful action without an intent id acts under the
        # agent's own lease, a failed one ends it.
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        guard.post_action(a, "corp", "r", "used", "x", failed=True)
        assert state(guard, d.intent_id) == ClaimState.RELEASED
        assert guard.pre_action(b, "corp", "r", "use").verdict == GuardVerdict.ALLOW

    def test_g1_is_checked_again_when_a_warning_has_decayed(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp")
        ops = Agent(name="ops", scopes={"corp": ["warning"]})
        da = guard.pre_action(a, "corp", "r", "use")
        action_id = guard.post_action(
            a, "corp", "r", "used", "ok", intent_id=da.intent_id
        )
        s.write(ops, Warning(scope="corp", invalidates=action_id, reason="freed"))
        db = guard.pre_action(b, "corp", "r", "use")
        assert db.verdict == GuardVerdict.ALLOW  # a's action is hidden
        s.set_clock(T0 + 3600)  # the warning has decayed: a's action holds again
        with pytest.raises(NotGrantedError):
            guard.post_action(b, "corp", "r", "used", "ok", intent_id=db.intent_id)


class TestExecute:
    """execute(): the token reaches the tool; a failure or a lost lease leaves a trace."""

    def test_the_tool_gets_the_fencing_token(self) -> None:
        s = tree()
        guard = Guard(s, epoch=7)
        seen: list[FencingToken | None] = []

        def tool(fencing_token: FencingToken | None) -> str:
            seen.append(fencing_token)
            return "ok"

        decision, result = guard.execute(
            agent("a", "corp"), "corp", "r", "use", "used", tool_fn=tool
        )
        assert result == "ok" and seen == [FencingToken(epoch=7, n=1)]
        assert decision.fencing_token == seen[0]

    def test_a_tool_failure_releases_the_lease_and_reraises(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp")

        def tool() -> str:
            raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            guard.execute(a, "corp", "r", "use", "used", tool_fn=tool)
        assert all(
            m.failed
            for m in s.read(scope="corp", resource="r")
            if isinstance(m, Action)
        )
        assert guard.pre_action(agent("b", "corp"), "corp", "r", "use").verdict == (
            GuardVerdict.ALLOW
        )

    def test_a_lease_lost_while_the_tool_runs_is_not_recorded(self) -> None:
        s = tree()
        guard = Guard(s)

        def slow_tool() -> str:
            s.set_clock(T0 + TTL + 1)  # the tool outlives its lease
            return "ok"

        decision, result = guard.execute(
            agent("a", "corp"), "corp", "r", "use", "used", tool_fn=slow_tool
        )
        assert decision.verdict == GuardVerdict.CONFLICT
        assert decision.reason == "lease lost"
        assert result is None
        actions = [
            m for m in s.read(scope="corp", resource="r") if isinstance(m, Action)
        ]
        assert actions and all(a.failed for a in actions)


class TestTokensAndClock:
    """G7, G8 and the lease clock."""

    def test_tokens_increase_per_tree_and_resource(self) -> None:
        s = tree()
        guard = Guard(s, epoch=3)
        ns = []
        for name, scope in (("a", "corp/a"), ("b", "corp/b")):
            ns.append(
                guard.pre_action(agent(name, scope), scope, "r", "use").fencing_token
            )
        other = guard.pre_action(
            agent("c", "corp/a"), "corp/a", "q", "use"
        ).fencing_token
        assert ns == [
            FencingToken(3, 1),
            FencingToken(3, 2),
        ]  # siblings share a counter
        assert other == FencingToken(3, 1)

    def test_the_token_is_not_in_the_decisions_repr_or_telemetry(self) -> None:
        sink = InMemorySink()
        s = tree()
        guard = Guard(s, telemetry=sink, epoch=424242)
        d = guard.pre_action(agent("a", "corp"), "corp", "r", "use")
        assert "424242" not in repr(d)
        assert all("424242" not in str(c) for c in sink.counters)
        assert any(c[0] == METRIC_LEASES_GRANTED for c in sink.counters)

    def test_a_lease_lasts_its_scopes_intent_ttl_from_the_grant(self) -> None:
        s = tree()
        guard = Guard(s)
        d = guard.pre_action(agent("a", "corp"), "corp", "r", "use")
        assert d.lease_expires_at == T0 + TTL
        s.set_clock(T0 + TTL - 1)
        assert guard.pre_action(agent("b", "corp"), "corp", "r", "use").verdict == (
            GuardVerdict.CONFLICT
        )

    def test_a_clock_step_back_does_not_revive_an_expired_lease(self) -> None:
        sink = InMemorySink()
        s = tree()
        guard = Guard(s, telemetry=sink)
        a, b = agent("a", "corp"), agent("b", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        s.set_clock(T0 + TTL + 1)
        assert s.check_conflict("corp", "r") is None  # expired: recorded for good
        s.set_clock(T0 + 10)
        assert state(guard, d.intent_id) == ClaimState.EXPIRED
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)
        assert any(c[0] == METRIC_LEASES_EXPIRED for c in sink.counters)
        assert guard.pre_action(b, "corp", "r", "use").verdict == GuardVerdict.ALLOW

    def test_a_later_time_read_elsewhere_still_ends_the_lease(self) -> None:
        s = tree()
        guard = Guard(s)
        guard.pre_action(agent("a", "corp"), "corp", "r", "use")
        s.set_clock(T0 + TTL + 1)
        guard.pre_action(agent("c", "corp"), "corp", "other", "use")  # reads the time
        s.set_clock(T0 + 10)  # the clock steps back before anyone checks r
        assert guard.pre_action(agent("b", "corp"), "corp", "r", "use").verdict == (
            GuardVerdict.ALLOW
        )

    def test_gc_keeps_a_lease_whose_intent_mark_is_gone(self) -> None:
        s = tree(deferred=("corp",))
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        s.set_clock(T0 + TTL - 1)
        assert guard.resolve_deferred("corp", "r", agents={a.id: a})[a.id].verdict == (
            GuardVerdict.ALLOW
        )
        s.set_clock(T0 + TTL + 5)
        assert s.gc() >= 1  # a's intent mark expires and goes
        assert state(guard, d.intent_id) == ClaimState.GRANTED
        guard.pre_action(b, "corp", "r", "use")
        assert guard.resolve_deferred("corp", "r", agents={b.id: b})[b.id].verdict == (
            GuardVerdict.BLOCKED
        )
        guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)
        assert state(guard, d.intent_id) == ClaimState.ACTED

    def test_gc_drops_closed_entries_whose_mark_is_gone(self) -> None:
        sink = InMemorySink()
        s = tree(deferred=("corp",))
        guard = Guard(s, telemetry=sink)
        a, b = agent("a", "corp"), agent("b", "corp")
        guard.pre_action(a, "corp", "r", "use", 0.9)
        db = guard.pre_action(b, "corp", "r", "use", 0.1)
        guard.resolve_deferred("corp", "r", agents={a.id: a, b.id: b})
        assert state(guard, db.intent_id) == ClaimState.LOST
        assert any(c[0] == METRIC_LEASES_LOST for c in sink.counters)
        s.set_clock(T0 + TTL + 1)
        s.gc()
        assert state(guard, db.intent_id) is None


class TestRound11:
    """Write hooks, the tool's signature, warnings on actions, escalated intents, release."""

    def test_write_hooks_run_with_the_space_released(self) -> None:
        s = tree()
        guard = Guard(s)
        free: list[bool] = []

        def hook(_agent_id: uuid.UUID, mark: object) -> None:
            if isinstance(mark, Action):
                reader = threading.Thread(target=lambda: s.read(scope="corp"))
                reader.start()
                reader.join(timeout=2)
                free.append(not reader.is_alive())

        s.add_write_hook(hook)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)
        assert free == [True]

    def test_a_clock_step_after_the_intent_write_does_not_break_the_decision(
        self,
    ) -> None:
        s = tree()
        guard = Guard(s)

        def jump(_agent_id: uuid.UUID, mark: object) -> None:
            if isinstance(mark, Intent):
                s.set_clock(s.now() + 2 * TTL)  # another thread moves the clock

        s.add_write_hook(jump)
        d = guard.pre_action(agent("a", "corp"), "corp", "r", "use")
        assert d.verdict == GuardVerdict.ALLOW

    def test_write_hooks_run_with_the_resource_lock_released(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp")
        done: list[bool] = []

        def hook(_agent_id: uuid.UUID, mark: object) -> None:
            if isinstance(mark, Action):
                other = threading.Thread(
                    target=lambda: guard.pre_action(b, "corp", "r", "use")
                )
                other.start()
                other.join(timeout=2)
                done.append(not other.is_alive())

        s.add_write_hook(hook)
        d = guard.pre_action(a, "corp", "r", "use")
        guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)
        assert done == [True]

    def test_a_tool_with_no_readable_signature_runs(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp")
        decision, result = guard.execute(
            a, "corp", "r", "use", "used", tool_fn=max, tool_args=(1, 2)
        )
        assert decision.verdict == GuardVerdict.ALLOW and result == 2
        assert [x.agent_id for x in s.get_actions("corp", "r")] == [a.id]

    def test_a_warning_on_an_action_needs_warning_rights_in_its_scope(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        action_id = guard.post_action(
            a, "corp", "r", "used", "ok", intent_id=d.intent_id
        )
        outsider = Agent(name="b", scopes={"corp/b": ["warning"]})
        with pytest.raises(ScopeError):
            guard.write_mark(outsider, Warning(scope="corp/b", invalidates=action_id))
        ops = Agent(name="ops", scopes={"corp": ["warning"], "corp/b": ["warning"]})
        guard.write_mark(ops, Warning(scope="corp/b", invalidates=action_id))
        assert s.get_actions("corp", "r") == []  # ops may hide it

    def test_a_restricted_agent_cannot_hide_an_action_from_another_scope(
        self,
    ) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp/a")
        d = guard.pre_action(a, "corp/a", "r", "use")
        action_id = guard.post_action(
            a, "corp/a", "r", "used", "ok", intent_id=d.intent_id
        )
        w = Agent(name="w", scopes={"corp": ["warning"], "corp/b": ["warning"]})
        guard.get_or_create_barrier(w.id).narrow("corp/a", "warning")
        with pytest.raises(ScopeError):
            guard.write_mark(w, Warning(scope="corp/b", invalidates=action_id))
        assert [x.agent_id for x in s.get_actions("corp/a", "r")] == [a.id]

    def _restricted_holder(
        self, revoked: str = "intent"
    ) -> tuple[MarkSpace, Guard, Agent, uuid.UUID, uuid.UUID]:
        """An agent that holds an action in corp/a and is then restricted there."""
        s = tree()
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token)
        a = Agent(
            name="a",
            scopes={"corp/a": ["intent", "action", "warning", "observation"]},
            max_source=Source.FLEET,
        )
        d = guard.pre_action(a, "corp/a", "r", "use")
        action_id = guard.post_action(
            a, "corp/a", "r", "used", "ok", intent_id=d.intent_id
        )
        guard.get_or_create_barrier(a.id).narrow("corp/a", revoked)
        return s, guard, a, action_id, token

    @pytest.mark.parametrize("revoked", ["intent", "action"])
    def test_a_restricted_agent_cannot_free_its_own_action_with_a_warning(
        self, revoked: str
    ) -> None:
        s, guard, a, action_id, _ = self._restricted_holder(revoked)
        with pytest.raises(ScopeError, match="hides an action"):
            guard.write_mark(a, Warning(scope="corp/a", invalidates=action_id))
        assert [x.id for x in s.get_actions("corp/a", "r")] == [action_id]

    def test_a_restricted_agent_cannot_free_its_own_action_with_a_supersede(
        self,
    ) -> None:
        s, guard, a, action_id, _ = self._restricted_holder()
        with pytest.raises(ScopeError, match="hides an action"):
            guard.write_mark(a, Warning(scope="corp/a", supersedes=action_id))
        with pytest.raises(ScopeError, match="hides an action"):
            guard.write_mark(
                a,
                Observation(
                    scope="corp/a",
                    topic="t",
                    content="c",
                    confidence=0.5,
                    source=Source.FLEET,
                    supersedes=action_id,
                ),
            )
        assert [x.id for x in s.get_actions("corp/a", "r")] == [action_id]

    def test_a_restricted_agent_cannot_free_its_action_from_another_scope(
        self,
    ) -> None:
        s = tree()
        guard = Guard(s)
        a = Agent(
            name="a",
            scopes={"corp/a": ["intent", "action", "warning"], "corp/b": ["warning"]},
        )
        d = guard.pre_action(a, "corp/a", "r", "use")
        action_id = guard.post_action(
            a, "corp/a", "r", "used", "ok", intent_id=d.intent_id
        )
        guard.get_or_create_barrier(a.id).narrow("corp/a", "intent")
        with pytest.raises(ScopeError, match="hides an action"):
            guard.write_mark(a, Warning(scope="corp/b", invalidates=action_id))
        assert [x.id for x in s.get_actions("corp/a", "r")] == [action_id]

    def test_a_restricted_agent_can_still_give_up_a_lease(self) -> None:
        s = tree()
        guard = Guard(s)
        a = Agent(name="a", scopes={"corp/a": ["intent", "action", "warning"]})
        b = agent("b", "corp/a")
        d = guard.pre_action(a, "corp/a", "r", "use")
        guard.get_or_create_barrier(a.id).narrow("corp/a", "intent")
        assert d.intent_id is not None
        guard.write_mark(a, Warning(scope="corp/a", supersedes=d.intent_id))
        assert state(guard, d.intent_id) == ClaimState.RELEASED
        assert guard.pre_action(b, "corp/a", "r", "use").verdict == GuardVerdict.ALLOW

    def test_a_restricted_agent_cannot_hide_another_agents_action(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp/a")
        d = guard.pre_action(a, "corp/a", "r", "use")
        action_id = guard.post_action(
            a, "corp/a", "r", "used", "ok", intent_id=d.intent_id
        )
        w = Agent(name="w", scopes={"corp/a": ["intent", "action", "warning"]})
        guard.get_or_create_barrier(w.id).narrow("corp/a", "action")
        with pytest.raises(ScopeError, match="hides an action"):
            guard.write_mark(w, Warning(scope="corp/a", invalidates=action_id))
        assert [x.id for x in s.get_actions("corp/a", "r")] == [action_id]

    def test_a_restricted_agent_can_warn_on_what_holds_nothing(self) -> None:
        s, guard, a, action_id, _ = self._restricted_holder()
        failed_id = guard.post_action(a, "corp/a", "other", "used", "boom", failed=True)
        guard.write_mark(a, Warning(scope="corp/a", invalidates=failed_id))
        guard.write_mark(a, Warning(scope="corp/a", reason="a plain warning"))
        assert [x.id for x in s.get_actions("corp/a", "r")] == [action_id]

    def test_a_restriction_elsewhere_does_not_stop_a_warning_on_this_action(
        self,
    ) -> None:
        s, guard, _, action_id, _ = self._restricted_holder()
        b = Agent(
            name="b",
            scopes={"corp/a": ["warning"], "corp/b": ["intent", "action"]},
        )
        guard.get_or_create_barrier(b.id).narrow("corp/b", "intent")
        guard.write_mark(b, Warning(scope="corp/a", invalidates=action_id))
        assert s.get_actions("corp/a", "r") == []

    def test_the_principal_ends_the_restriction_and_the_agent_can_warn_again(
        self,
    ) -> None:
        s, guard, a, action_id, token = self._restricted_holder()
        assert guard.get_or_create_barrier(a.id).restore("corp/a", "intent", token)
        guard.write_mark(a, Warning(scope="corp/a", invalidates=action_id))
        assert s.get_actions("corp/a", "r") == []

    def test_a_boundary_does_not_grant_an_intent_waiting_for_the_principal(
        self,
    ) -> None:
        s = tree(deferred=("corp",), yield_all=("corp/a",))
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token)
        p, q = agent("p", "corp/a"), agent("q", "corp/a")
        dp = guard.pre_action(p, "corp/a", "r", "use")
        dq = guard.pre_action(q, "corp/a", "r", "use")
        assert dq.verdict == GuardVerdict.BLOCKED  # the principal decides
        assert dp.intent_id is not None and dq.intent_id is not None
        guard.release(dp.intent_id, token)
        out = guard.resolve_deferred("corp", "r", agents={q.id: q})
        assert q.id not in out
        assert state(guard, dq.intent_id) is None  # still pending
        assert guard.grant(dq.intent_id, token).verdict == GuardVerdict.ALLOW

    def test_claim_state_reads_an_expired_lease_as_expired(self) -> None:
        s = tree()
        guard = Guard(s)
        d = guard.pre_action(agent("a", "corp"), "corp", "r", "use")
        assert d.intent_id is not None
        assert guard.claim_state(d.intent_id) == ClaimState.GRANTED
        s.set_clock(T0 + TTL)
        assert guard.claim_state(d.intent_id) == ClaimState.EXPIRED

    def test_a_failed_action_on_an_expired_lease_records_the_expiry(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        s.set_clock(T0 + TTL + 1)
        guard.post_action(
            a, "corp", "r", "used", "x", intent_id=d.intent_id, failed=True
        )
        assert state(guard, d.intent_id) == ClaimState.EXPIRED

    def test_a_refused_supersession_does_not_move_the_write_order(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b, c = agent("a", "corp"), agent("b", "corp"), agent("c", "corp")
        first = s.write(a, Intent(scope="corp", resource="r", action="use"))
        with pytest.raises(ScopeError):
            s.write(
                b, Intent(scope="corp", resource="r", action="use", supersedes=first)
            )
        later = s.write(c, Intent(scope="corp", resource="r", action="use"))
        mark = s.get_mark(later)
        assert mark is not None and mark.created_at == math.nextafter(T0, math.inf)
        assert guard.claim_state(later) is None

    def test_hooks_of_a_block_that_raised_still_fire(self) -> None:
        s = tree()
        fired: list[uuid.UUID] = []
        s.add_write_hook(lambda _agent_id, mark: fired.append(mark.id))
        a = Agent(name="a", scopes={"corp": ["observation"]})
        written: list[uuid.UUID] = []
        with pytest.raises(RuntimeError):
            with s.locked():
                written.append(
                    s.write(
                        a,
                        Observation(
                            scope="corp", topic="t", content="x", source=Source.FLEET
                        ),
                    )
                )
                raise RuntimeError("the block fails after the write")
        assert len(written) == 1 and fired == written

    def test_a_boundary_refuses_an_escalated_intent_of_a_revoked_agent(self) -> None:
        s = tree(deferred=("corp",), yield_all=("corp/a",))
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token)
        p, q = agent("p", "corp/a"), agent("q", "corp/a")
        dp = guard.pre_action(p, "corp/a", "r", "use")
        dq = guard.pre_action(q, "corp/a", "r", "use")
        assert dq.verdict == GuardVerdict.BLOCKED
        revoked = Agent(name="q", id=q.id, scopes={})
        out = guard.resolve_deferred("corp", "r", agents={q.id: revoked})
        assert out[q.id].verdict == GuardVerdict.DENIED
        assert state(guard, dq.intent_id) == ClaimState.LOST
        assert dp.intent_id is not None and dq.intent_id is not None
        guard.release(dp.intent_id, token)
        assert guard.grant(dq.intent_id, token).verdict == GuardVerdict.CONFLICT

    def test_release_of_an_expired_lease_records_the_expiry(self) -> None:
        sink = InMemorySink()
        s = tree()
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token, telemetry=sink)
        d = guard.pre_action(agent("a", "corp"), "corp", "r", "use")
        assert d.intent_id is not None
        s.set_clock(T0 + TTL + 1)
        assert guard.release(d.intent_id, token) is False
        assert state(guard, d.intent_id) == ClaimState.EXPIRED
        guard.pre_action(agent("c", "corp"), "corp", "other", "use")  # emits
        assert any(c[0] == METRIC_LEASES_EXPIRED for c in sink.counters)


class TestOneGuardAndAuthorship:
    """One guard per space; only a mark's author may supersede it (G10)."""

    def test_a_second_guard_on_one_space_is_refused(self) -> None:
        s = tree()
        Guard(s)
        with pytest.raises(ValueError):
            Guard(s)

    def test_another_agent_cannot_supersede_an_action_or_its_warning(self) -> None:
        s = tree()
        guard = Guard(s)
        a = agent("a", "corp")
        x = Agent(name="x", scopes={"corp/b": ["observation", "warning", "action"]})
        ops = Agent(name="ops", scopes={"corp": ["warning"]})
        d = guard.pre_action(a, "corp", "r", "use")
        action_id = guard.post_action(
            a, "corp", "r", "used", "ok", intent_id=d.intent_id
        )
        warning_id = s.write(ops, Warning(scope="corp", invalidates=action_id))
        for target in (action_id, warning_id):
            with pytest.raises(ScopeError):
                s.write(x, Warning(scope="corp/b", supersedes=target))
        s.write(ops, Warning(scope="corp", supersedes=warning_id))  # its author may


class TestProperties:
    """P71 to P76, each on a case built to break it."""

    def test_p71_no_two_live_leases_of_different_agents_compete(self) -> None:
        s = tree(deferred=("corp",))
        guard = Guard(s)
        agents = {x.id: x for x in (agent("p", "corp"), agent("a", "corp/a"))}
        p, a = list(agents.values())
        guard.pre_action(a, "corp/a", "r", "use")
        guard.pre_action(p, "corp", "r", "use", 0.99)
        guard.resolve_deferred("corp", "r", agents=agents)
        live = [c for c in guard._claims.values() if c.state == ClaimState.GRANTED]
        assert len(live) == 1

    def test_p72_an_action_needs_its_own_live_lease(self) -> None:
        s = tree(deferred=("corp",))
        guard = Guard(s)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")  # pending, not granted
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)

    def test_p73_tokens_increase_and_never_repeat_after_gc(self) -> None:
        s = tree()
        guard = Guard(s, epoch=1)
        seen = []
        for i in range(3):
            a = agent(f"a{i}", "corp")
            d = guard.pre_action(a, "corp", "r", "use")
            seen.append(d.fencing_token)
            guard.post_action(
                a, "corp", "r", "used", "x", intent_id=d.intent_id, failed=True
            )
            s.set_clock(T0 + (i + 1) * 2 * TTL)
            s.gc()
        assert seen == sorted(seen) and len(set(seen)) == 3

    def test_p74_an_ended_lease_never_becomes_live_again(self) -> None:
        s = tree()
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token)
        a = agent("a", "corp")
        d = guard.pre_action(a, "corp", "r", "use")
        assert d.intent_id is not None
        assert guard.release(d.intent_id, token) is True
        assert guard.grant(d.intent_id, token).verdict == GuardVerdict.CONFLICT
        with pytest.raises(NotGrantedError):
            guard.post_action(a, "corp", "r", "used", "ok", intent_id=d.intent_id)

    def test_p75_a_second_held_action_only_after_a_warning(self) -> None:
        s = tree()
        guard = Guard(s)
        a, b = agent("a", "corp"), agent("b", "corp/a")
        da = guard.pre_action(a, "corp", "r", "use")
        db = guard.pre_action(b, "corp/a", "r", "use")
        assert db.verdict == GuardVerdict.CONFLICT
        guard.post_action(a, "corp", "r", "used", "ok", intent_id=da.intent_id)
        with pytest.raises(NotGrantedError):
            guard.post_action(b, "corp/a", "r", "used", "ok")
        assert [x.agent_id for x in s.get_actions("corp", "r")] == [a.id]

    def test_p76_a_waiting_candidate_is_granted_when_the_lease_ends(self) -> None:
        s = tree(deferred=("corp",))
        token = uuid.uuid4()
        guard = Guard(s, principal_token=token)
        a, p = agent("a", "corp/a"), agent("p", "corp")
        da = guard.pre_action(a, "corp/a", "r", "use")
        guard.pre_action(p, "corp", "r", "use")
        assert guard.resolve_deferred("corp", "r", agents={p.id: p})[p.id].verdict == (
            GuardVerdict.BLOCKED
        )
        assert da.intent_id is not None
        guard.release(da.intent_id, token)
        assert guard.resolve_deferred("corp", "r", agents={p.id: p})[p.id].verdict == (
            GuardVerdict.ALLOW
        )
