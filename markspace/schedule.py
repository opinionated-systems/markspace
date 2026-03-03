# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Scheduler

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
from dataclasses import dataclass

from markspace.core import Agent
from markspace.space import MarkSpace


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
        scheduler = Scheduler(space)

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

    def __init__(self, space: MarkSpace) -> None:
        self.space = space

        # Internal state: schedule entries keyed by agent name.
        self._entries: dict[str, ScheduleEntry] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def register(self, agent: Agent) -> None:
        """
        Register an agent for scheduling. Reads schedule_interval from
        the agent's manifest. Agents without a manifest or without
        schedule_interval are ignored.
        """
        if agent.manifest is None:
            return
        if agent.manifest.schedule_interval is None:
            return
        if agent.manifest.schedule_interval <= 0:
            return

        with self._lock:
            self._entries[agent.name] = ScheduleEntry(
                agent=agent,
                interval_seconds=agent.manifest.schedule_interval,
            )

    def unregister(self, agent: Agent) -> None:
        """Remove an agent from scheduling."""
        with self._lock:
            self._entries.pop(agent.name, None)

    def due(self) -> list[Agent]:
        """
        Return agents whose schedule interval has elapsed since their
        last activation. Newly registered agents (never activated) are
        immediately due.

        P43: Minimum interval between activations >= schedule_interval.

        Pure read - does not modify activation times.
        """
        now = self.space.now()
        result: list[Agent] = []
        with self._lock:
            for entry in self._entries.values():
                if now - entry.last_activation >= entry.interval_seconds:
                    result.append(entry.agent)
        return result

    def mark_activated(self, agent: Agent) -> None:
        """Record that an agent was activated at the current time."""
        with self._lock:
            entry = self._entries.get(agent.name)
            if entry is not None:
                entry.last_activation = self.space.now()

    def tick_all(self) -> list[Agent]:
        """
        Return agents that are due and mark them all as activated.

        A convenience method that combines due() + mark_activated().
        """
        agents = self.due()
        for agent in agents:
            self.mark_activated(agent)
        return agents

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
            existing = self._entries.get(agent.name)
            last_activation = existing.last_activation if existing else 0.0
            self._entries[agent.name] = ScheduleEntry(
                agent=agent,
                interval_seconds=agent.manifest.schedule_interval,
                last_activation=last_activation,
            )

    def start(self, poll_interval: float = 1.0) -> None:
        """
        Start the background timer loop. Calls tick_all() every
        poll_interval seconds.
        """
        if self._running:
            return

        self._stop_event.clear()
        self._running = True

        def _loop() -> None:
            while not self._stop_event.is_set():
                self.tick_all()
                self._stop_event.wait(timeout=poll_interval)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background timer loop."""
        self._stop_event.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
