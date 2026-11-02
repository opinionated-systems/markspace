# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Random Lease Histories

Random histories of guard operations on a scope tree with mixed policies and
deferred scopes: pre_action, boundaries, principal grants and releases,
post_action (with an intent, direct, failed), warnings, clock steps forward
and back, and gc. After every step, P71 to P75 (Spec Section 6.5) are checked
against the guard's lease table and the space's marks; then the same
operations run in several threads at once.
"""

from __future__ import annotations

import random
import threading
import uuid

import pytest

from markspace import (
    Action,
    Agent,
    ClaimState,
    ConflictPolicy,
    Guard,
    MarkSpace,
    NotGrantedError,
    Scope,
    ScopeError,
    Warning,
)
from markspace.core import scope_contains, scope_root

SCOPES = ("corp", "corp/a", "corp/b", "corp/a/x")
RESOURCES = ("r1", "r2")
T0 = 1_000_000.0
POLICIES = (
    ConflictPolicy.HIGHEST_CONFIDENCE,
    ConflictPolicy.FIRST_WRITER,
    ConflictPolicy.YIELD_ALL,
)


def competes(a: str, b: str) -> bool:
    return scope_contains(a, b) or scope_contains(b, a)


class History:
    """One random history and its checks."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.space = MarkSpace(
            scopes=[
                Scope(
                    name=n,
                    conflict_policy=self.rng.choice(POLICIES),
                    deferred=self.rng.random() < 0.5,
                )
                for n in SCOPES
            ]
        )
        self.now = T0
        self.space.set_clock(self.now)
        self.token = uuid.uuid4()
        self.guard = Guard(self.space, principal_token=self.token, epoch=1)
        self.agents = [
            Agent(
                name=f"a{i}", scopes={s: ["intent", "action", "need"] for s in SCOPES}
            )
            for i in range(5)
        ]
        self.by_id = {a.id: a for a in self.agents}
        self.ops = Agent(name="ops", scopes={s: ["warning"] for s in SCOPES})
        # Warning rights in corp/b only: it may hide actions there, nowhere else.
        self.outsider = Agent(name="outsider", scopes={"corp/b": ["warning"]})
        self.warnings: list[uuid.UUID] = []
        self.warnings_by_outsider = 0
        self.intents: list[tuple[Agent, str, str, uuid.UUID]] = []
        # Single thread: the view before an action is the guard's state.
        self.strict = True
        self.ended: set[uuid.UUID] = set()
        # (later action, earlier action) pairs P75 allows: the earlier one was
        # hidden by a warning when the later one was recorded.
        self.allowed_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        self.lock = threading.Lock()

    # -- operations ---------------------------------------------------------

    def step(self, rng: random.Random) -> None:
        op = rng.random()
        if op < 0.30:
            self.pre_action(rng)
        elif op < 0.45:
            self.boundary(rng)
        elif op < 0.52:
            self.principal(rng)
        elif op < 0.75:
            self.act(rng)
        elif op < 0.82:
            self.warn(rng)
        elif op < 0.95:
            self.tick(rng)
        else:
            self.space.gc()

    def pre_action(self, rng: random.Random) -> None:
        a, s, r = rng.choice(self.agents), rng.choice(SCOPES), rng.choice(RESOURCES)
        d = self.guard.pre_action(a, s, r, "use", rng.random())
        if d.intent_id is not None:
            with self.lock:
                self.intents.append((a, s, r, d.intent_id))

    def boundary(self, rng: random.Random) -> None:
        deferred = [s for s in SCOPES if self.space.get_scope(s).deferred]
        if deferred:
            agents = {a.id: a for a in self.agents if rng.random() < 0.9}
            self.guard.resolve_deferred(
                rng.choice(deferred), rng.choice(RESOURCES), agents=agents
            )

    def principal(self, rng: random.Random) -> None:
        with self.lock:
            if not self.intents:
                return
            _, _, _, iid = rng.choice(self.intents)
        if rng.random() < 0.6:
            self.guard.grant(iid, self.token)
        else:
            self.guard.release(iid, self.token)

    def act(self, rng: random.Random) -> None:
        with self.lock:
            if not self.intents:
                return
            a, s, r, iid = rng.choice(self.intents)
        roll = rng.random()
        failed = roll < 0.15
        direct = 0.15 <= roll < 0.30
        before = self.claim_view(iid)
        hidden = self.hidden_actions(r)
        try:
            mark_id = self.guard.post_action(
                a, s, r, "used", "ok", intent_id=None if direct else iid, failed=failed
            )
        except NotGrantedError:
            assert not failed, "a failed action never raises for a lease"
            return
        if failed:
            return
        if not direct and self.strict:
            # P72: recorded only under a live lease of its own agent, scope
            # and resource.
            assert before == ("granted", a.id, s, r), before
        with self.lock:
            for h in hidden:
                self.allowed_pairs.add((mark_id, h))

    def warn(self, rng: random.Random) -> None:
        held = self.held_actions(rng.choice(RESOURCES))
        roll = rng.random()
        if roll < 0.2 and self.warnings:
            # ops retracts one of its warnings: the action it hid is back.
            wid = self.warnings.pop(rng.randrange(len(self.warnings)))
            self.space.write(self.ops, Warning(scope="corp", supersedes=wid))
        elif roll < 0.4 and held:
            # A warning from corp/b on an action elsewhere is refused.
            target = rng.choice(held)
            mark = Warning(scope="corp/b", invalidates=target.id)
            if scope_contains("corp/b", target.scope):
                self.warnings_by_outsider += 1
                self.space.write(self.outsider, mark)
            else:
                with pytest.raises(ScopeError):
                    self.space.write(self.outsider, mark)
        elif held:
            target = rng.choice(held)
            self.warnings.append(
                self.space.write(
                    self.ops, Warning(scope=target.scope, invalidates=target.id)
                )
            )

    def tick(self, rng: random.Random) -> None:
        roll = rng.random()
        with self.lock:
            if roll < 0.1:
                self.now -= rng.uniform(1, 3600)  # the clock steps back
            elif roll < 0.3:
                self.now += rng.uniform(1800, 4000)  # past leases and intents
            else:
                self.now += rng.uniform(1, 300)
            self.space.set_clock(self.now)

    # -- views and checks ---------------------------------------------------

    def claim_view(self, iid: uuid.UUID) -> tuple[str, uuid.UUID, str, str] | None:
        with self.space.locked(), self.guard._claim_lock:
            claim = self.guard._claims.get(iid)
            if claim is None or claim.state != ClaimState.GRANTED:
                return None
            assert claim.expires_at is not None
            lease_now = max(self.guard._lease_now, self.space.now())
            if lease_now >= claim.expires_at:
                return None
            i = claim.intent
            return ("granted", i.agent_id, i.scope, i.resource)

    def held_actions(self, resource: str) -> list[Action]:
        return [a for a in self.space.get_actions("corp", resource) if not a.failed]

    def hidden_actions(self, resource: str) -> set[uuid.UUID]:
        with self.space.locked():
            held = {a.id for a in self.held_actions(resource)}
            return {
                m.id
                for m in self.space._marks.values()
                if isinstance(m, Action)
                and m.resource == resource
                and not m.failed
                and m.id not in self.space._superseded
                and m.id not in held
            }

    def check(self, *, warnings: bool = True) -> None:
        with self.space.locked(), self.guard._claim_lock:
            claims = dict(self.guard._claims)
            lease_now = max(self.guard._lease_now, self.space.now())
            live = [
                c
                for c in claims.values()
                if c.state == ClaimState.GRANTED
                and c.expires_at is not None
                and lease_now < c.expires_at
            ]
            # P71: no two live leases of different agents compete.
            for i, c in enumerate(live):
                for d in live[i + 1 :]:
                    assert not (
                        c.intent.agent_id != d.intent.agent_id
                        and c.intent.resource == d.intent.resource
                        and competes(c.intent.scope, d.intent.scope)
                    ), (c, d)
            # P74: an ended lease never becomes live again.
            for iid, c in claims.items():
                if c.state != ClaimState.GRANTED:
                    self.ended.add(iid)
                else:
                    assert iid not in self.ended, c
            # P75: two held actions of different agents compete only when a
            # warning hid the earlier one as the later one was recorded.
            for r in RESOURCES:
                held = sorted(self.held_actions(r), key=lambda a: a.created_at)
                for i, x in enumerate(held):
                    for y in held[i + 1 :]:
                        if x.agent_id == y.agent_id or not competes(x.scope, y.scope):
                            continue
                        assert warnings and (y.id, x.id) in self.allowed_pairs, (x, y)


@pytest.mark.parametrize("seed", range(12))
def test_random_histories_keep_p71_to_p75(seed: int) -> None:
    h = History(seed)
    for _ in range(400):
        h.step(h.rng)
        h.check()


def test_every_grant_gets_a_new_token_per_tree_and_resource() -> None:
    for seed in range(6):
        h = History(100 + seed)
        issued: dict[tuple[str, str], list[int]] = {}
        known: set[uuid.UUID] = set()
        for _ in range(400):
            h.step(h.rng)
            for iid, c in list(h.guard._claims.items()):
                if c.token is not None and iid not in known:
                    known.add(iid)
                    key = (scope_root(c.intent.scope), c.intent.resource)
                    issued.setdefault(key, []).append(c.token.n)
        for ns in issued.values():
            assert ns == sorted(ns) and len(set(ns)) == len(ns)


@pytest.mark.parametrize("seed", range(4))
def test_concurrent_histories_keep_p71_p74_and_p75(seed: int) -> None:
    h = History(1000 + seed)
    h.strict = False
    errors: list[BaseException] = []
    stop = threading.Event()

    def worker(k: int) -> None:
        rng = random.Random(seed * 100 + k)
        try:
            for _ in range(250):
                op = rng.random()
                if op < 0.4:
                    h.pre_action(rng)
                elif op < 0.6:
                    h.boundary(rng)
                elif op < 0.7:
                    h.principal(rng)
                elif op < 0.9:
                    h.act(rng)
                elif op < 0.95:
                    h.tick(rng)
                else:
                    h.space.gc()
        except BaseException as exc:  # reported below
            errors.append(exc)

    def checker() -> None:
        # Pauses between checks, so the workers get the locks.
        while not stop.wait(0.002):
            try:
                h.check(warnings=False)
            except BaseException as exc:  # reported below
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    watch = threading.Thread(target=checker)
    watch.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    stop.set()
    watch.join(timeout=60)
    assert not any(t.is_alive() for t in [*threads, watch]), "a thread is stuck"
    h.check(warnings=False)
    assert not errors, errors[0]
