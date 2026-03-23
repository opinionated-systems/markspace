# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Scheduler

Deterministic infrastructure that reads agent manifests and determines
which agents are due for activation. Like the Guard, the Scheduler is
infrastructure - it wraps timing, not agent behavior.

The principal sets schedule_interval in the agent's manifest:
    AgentManifest(
        inputs=(...),
        outputs=(...),
        schedule_interval=minutes(5),
    )

The Scheduler reads registered agents' manifests and tracks timing.
No marks are involved - scheduling is a property of the agent, not
a signal in the environment.

Spec Section 14.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from markspace.core import Agent


@dataclass
class ScheduleEntry:
    """Internal record of an active schedule."""

    agent: Agent
    interval_seconds: float
    last_activation: float = 0.0


class Scheduler:
    """
    Deterministic infrastructure that reads agent manifests and determines
    which agents are due for activation.

    Like the Guard, the Scheduler has a deterministic core (register, due,
    tick_all) and a thin timer wrapper (start, stop). Tests call the core
    methods directly without timers.

    Usage:
        scheduler = Scheduler(space.now)  # or Scheduler(space) for compat

        # Principal creates agent with schedule
        agent = Agent(
            name="weather-poller",
            scopes={"weather": ["observation"]},
            manifest=AgentManifest(
                outputs=(("weather", MarkType.OBSERVATION),),
                schedule_interval=minutes(5),
            ),
        )
        scheduler.register(agent)

        # Deterministic check (for tests or manual harnesses)
        due_agents = scheduler.tick_all()

        # Or run the background timer
        scheduler.start()
        ...
        scheduler.stop()

    Spec Section 14.
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        pre_activation_check: Callable[[Agent], str | None] | None = None,
    ) -> None:
        # Accept a MarkSpace for backward compatibility (uses its .now() method).
        from markspace.space import MarkSpace

        if isinstance(clock, MarkSpace):
            self._clock = clock.now
        elif clock is not None:
            self._clock = clock
        else:
            self._clock = time.time

        # Optional pre-activation check (e.g., budget exhaustion).
        # If provided, agents that fail the check are skipped.
        self._pre_activation_check = pre_activation_check

        # Internal state: schedule entries keyed by agent UUID.
        self._entries: dict[uuid.UUID, ScheduleEntry] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def register(self, agent: Agent) -> bool:
        """
        Register an agent for scheduling. Reads schedule_interval from
        the agent's manifest. Agents without a manifest or without
        schedule_interval are silently skipped.

        Returns True if the agent was registered, False if skipped.
        """
        if agent.manifest is None:
            return False
        if agent.manifest.schedule_interval is None:
            return False
        if agent.manifest.schedule_interval <= 0:
            return False

        with self._lock:
            self._entries[agent.id] = ScheduleEntry(
                agent=agent,
                interval_seconds=agent.manifest.schedule_interval,
            )
            return True

    def unregister(self, agent: Agent) -> None:
        """Remove an agent from scheduling."""
        with self._lock:
            self._entries.pop(agent.id, None)

    def due(self) -> list[Agent]:
        """
        Return agents whose schedule interval has elapsed since their
        last activation. Newly registered agents (never activated) are
        immediately due. Agents that fail the pre-activation check
        (e.g., budget exhaustion) are excluded.

        P56: Minimum interval between activations >= schedule_interval.

        Pure read - does not modify activation times.
        """
        now = self._clock()
        result: list[Agent] = []
        with self._lock:
            for entry in self._entries.values():
                if now - entry.last_activation >= entry.interval_seconds:
                    if self._pre_activation_check is not None:
                        rejection = self._pre_activation_check(entry.agent)
                        if rejection is not None:
                            continue
                    result.append(entry.agent)
        return result

    def mark_activated(self, agent: Agent) -> None:
        """Record that an agent was activated at the current time."""
        with self._lock:
            entry = self._entries.get(agent.id)
            if entry is not None:
                entry.last_activation = self._clock()

    def tick_all(self) -> list[Agent]:
        """
        Return agents that are due and mark them all as activated.

        Holds the lock across the entire due-check + mark-activated sequence
        to prevent TOCTOU races where two concurrent tick_all() calls both
        see the same agents as due before either marks them activated.

        Agents that fail the pre-activation check (e.g., budget exhaustion)
        are excluded and NOT marked as activated.
        """
        now = self._clock()
        result: list[Agent] = []
        with self._lock:
            for entry in self._entries.values():
                if now - entry.last_activation >= entry.interval_seconds:
                    if self._pre_activation_check is not None:
                        rejection = self._pre_activation_check(entry.agent)
                        if rejection is not None:
                            continue
                    result.append(entry.agent)
                    entry.last_activation = now
        return result

    def update(self, agent: Agent) -> None:
        """
        Update an existing schedule entry with a new manifest.
        If the agent's schedule_interval changed, the new interval
        takes effect immediately. Preserves last_activation time.
        """
        if agent.manifest is None or agent.manifest.schedule_interval is None:
            self.unregister(agent)
            return
        if agent.manifest.schedule_interval <= 0:
            self.unregister(agent)
            return

        with self._lock:
            existing = self._entries.get(agent.id)
            last_activation = existing.last_activation if existing else 0.0
            self._entries[agent.id] = ScheduleEntry(
                agent=agent,
                interval_seconds=agent.manifest.schedule_interval,
                last_activation=last_activation,
            )

    def start(
        self,
        poll_interval: float = 1.0,
        on_due: Callable[[list[Agent]], None] | None = None,
    ) -> None:
        """
        Start the background timer loop. Calls tick_all() every
        poll_interval seconds.

        on_due: optional callback invoked with the list of due agents
            each tick. If not provided, tick_all() still marks agents
            as activated but no further action is taken.
        """
        with self._lock:
            if self._running:
                return

            self._stop_event.clear()
            self._running = True

            def _loop() -> None:
                while not self._stop_event.is_set():
                    due_agents = self.tick_all()
                    if on_due is not None and due_agents:
                        on_due(due_agents)
                    self._stop_event.wait(timeout=poll_interval)

            self._thread = threading.Thread(target=_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the background timer loop."""
        with self._lock:
            self._stop_event.set()
            self._running = False
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)
