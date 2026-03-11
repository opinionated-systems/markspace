# Markspace Critical Review

Critical analysis of the protocol's central claims, followed by an implementation plan for the defense-in-depth checks that close the most important gap.

## Contents

- [1. Critical Analysis](#1-critical-analysis)
  - [1.1 The guard is a single point of failure](#11-the-guard-is-a-single-point-of-failure)
  - [1.2 The adversarial testing is weak](#12-the-adversarial-testing-is-weak)
  - [1.3 The scaling results don't support the scaling claims](#13-the-scaling-results-dont-support-the-scaling-claims)
  - [1.4 The biological analogy is misleading](#14-the-biological-analogy-is-misleading)
  - [1.5 HIGHEST_CONFIDENCE doesn't actually work](#15-highest_confidence-doesnt-actually-work)
  - [1.6 The 64% completion rate is buried as acceptable](#16-the-64-completion-rate-is-buried-as-acceptable)
  - [1.7 Half the defense-in-depth is unimplemented](#17-half-the-defense-in-depth-is-unimplemented)
  - [1.8 The message-passing critique is a straw man](#18-the-message-passing-critique-is-a-straw-man)
  - [1.9 Static trust is a major gap](#19-static-trust-is-a-major-gap)
  - [1.10 No comparison to existing systems](#110-no-comparison-to-existing-systems)
- [2. What Was Fixed](#2-what-was-fixed)
- [3. Implementation Plan: Defense-in-Depth](#3-implementation-plan-defense-in-depth)
  - [3.1 Build order](#31-build-order)
  - [3.2 Write hooks in space.py](#32-write-hooks-in-spacepy)
  - [3.3 StatisticalEnvelope](#33-statisticalenvelope)
  - [3.4 AgentBarrier](#34-agentbarrier)
  - [3.5 Guard integration](#35-guard-integration)
  - [3.6 DiagnosticProbe](#36-diagnosticprobe)
  - [3.7 Tests](#37-tests)
  - [3.8 Exports](#38-exports)
  - [3.9 Risks](#39-risks)
  - [3.10 Design decisions](#310-design-decisions-resolved)
  - [3.11 Missing items](#311-missing-items-resolved)
  - [3.12 Prerequisite refactor](#312-prerequisite-refactor-unify-the-write-path)

---

## 1. Critical Analysis

### 1.1 The guard is a single point of failure

The entire safety thesis rests on "deterministic guards at the boundary agents can't bypass." But the guard is a centralized component - the paper even acknowledges it's a single point of failure. This undercuts the core biological analogy. Ant colonies and immune systems have no central enforcement point. They achieve robustness *because* there's no single component whose failure breaks the system. Markspace achieves safety guarantees by reintroducing exactly the architectural bottleneck that distributed systems try to eliminate. The protocol is less "stigmergy" and more "a database with access control" wearing a biology costume.

### 1.2 The adversarial testing is weak

The adversarial experiments test the *guard*, not the *protocol*. All three adversarial modes (confidence manipulation, flooding, injection) are attacks that any competent authorization layer would reject - they're scope violations, not sophisticated attacks. Missing from the adversarial model:

- **Adversarial agents acting within their authorized scope** to subtly corrupt shared state (e.g., writing technically valid but semantically misleading observations at full FLEET trust weight)
- **Collusion** between multiple compromised agents reinforcing each other's bogus observations
- **Slow poisoning** - small, plausible-looking observations that accumulate over time

The framework explicitly admits the guard has "no semantic error detection," yet doesn't test the most dangerous attack class: well-formed lies from trusted agents. The reinforcement mechanism would actually *amplify* coordinated false observations from multiple FLEET agents.

Structurally, agents in the experiment cannot write observation marks (only the parking bot and building ops bot can), so the attack surface where a fleet agent writes technically valid but semantically false observations - and other agents trust and act on them - is not exercised at all.

### 1.3 The scaling results don't support the scaling claims

The proportional-scaling experiment shows:
- Completion drops from 59.9% to 43.2% at 10x scale
- Per-agent cost grows 4.25x at 10x agents
- Conflict rate rises from 26.1% to 37.8%

The paper frames per-agent steps being "nearly flat" (1.2x) as a win, but the meaningful metrics all degrade. Calling 4.25x cost growth "sub-linear" is technically true but misleading - that's still a severe cost multiplier. And completion dropping 17 percentage points while resources scale proportionally suggests the protocol introduces coordination overhead that worsens with scale, which is exactly what stigmergy supposedly avoids. The paper attributes the cost growth to "view-response-length" (larger state to read), but this is a fundamental architectural problem: the shared mark space grows with agent count, and every agent must read increasingly large state.

### 1.4 The biological analogy is misleading

The paper's own scale caveat admits biological stigmergy works because of "massive redundancy (10^4 to 10^6 agents) and statistical aggregation." LLM agents operate at 10-1000 scale with each agent being expensive and non-redundant. The biological analogy breaks down precisely where it matters:

- Ants are expendable; LLM agents are not (each costs tokens)
- Ant pheromones are simple scalars; marks are rich structured data requiring parsing
- Ant decision rules are hardwired; LLM agent "local rules" are probabilistic and exploitable
- Biological stigmergy has no guard - the robustness comes from population statistics, not enforcement

The paper uses biology to argue the approach is "proven at scale," then solves the actual coordination problem with a centralized guard that has nothing to do with biology.

### 1.5 HIGHEST_CONFIDENCE doesn't actually work

The paper admits that lock-based guards degenerate HIGHEST_CONFIDENCE into FIRST_WRITER. The proposed fix (deferred resolution via YIELD_ALL) requires a principal (human or simulated) to resolve conflicts - effectively punting the hard coordination problem to a human. Three conflict policies are advertised, but in practice you get two: first-come-first-served, or "ask a human." That's not a protocol innovation.

### 1.6 The 64% completion rate is buried as acceptable

Across all baseline runs, agents complete only ~64% of their assigned tasks. The paper treats this as a resource-scarcity artifact, not a protocol limitation. But the protocol's job is to coordinate efficient resource allocation. If agents with proportional resources still fail 36% of the time, the protocol isn't solving the coordination problem well. The paper doesn't compare against simpler baselines - what would a basic queue/lock system achieve? Without that comparison, "64% with a novel protocol" could be worse than "80% with a shared database and round-robin."

### 1.7 Half the defense-in-depth is unimplemented

The single-agent validation table lists six security checks. Three are "Design only" - statistical envelope monitoring, absorbing barriers, and diagnostic probes. These are exactly the adaptive, runtime-detection mechanisms that would catch the semantic attacks the guard misses (section 1.2 above). The paper claims defense-in-depth but has only implemented the static, easy-to-build layers.

### 1.8 The message-passing critique is a straw man

The paper positions itself against "message-passing" as the dominant paradigm. But modern multi-agent systems already use shared-state approaches (blackboard architectures, shared databases, event buses). The Related Work section actually surveys these thoroughly (blackboard systems, tuple spaces, Hoare monitors, CRDTs, Kubernetes reconciliation), but the opening argument sets up message-passing as the enemy and then the Related Work quietly concedes the landscape is more nuanced - without reconciling the two.

Three specific spots:

1. **The opening framing**: acknowledges shared-state alternatives exist in a parenthetical then proceeds as if they don't.
2. **Missing comparison**: no head-to-head with blackboard systems, tuple spaces, or shared-database approaches - only message-passing and consensus.
3. **The "gap" claim**: defines the gap as exactly what markspace provides, rather than asking whether the *problem* is better solved by simpler shared-state alternatives.

The actual novelty is the specific mark-type taxonomy, decay model, trust weighting, and the guard layer adapted for adversarially exploitable LLM agents - not the concept of environment-mediated coordination, which predates this work by decades.

**Status**: Partially addressed. The opening was rewritten to position against the right baselines (see [Section 2](#2-what-was-fixed)).

### 1.9 Static trust is a major gap

The three fixed trust levels (FLEET=1.0, EXTERNAL_VERIFIED=0.7, EXTERNAL_UNVERIFIED=0.3) assume fleet agents are trustworthy by definition. This is the weakest assumption in the system. If an agent is compromised via prompt injection *during execution* (not at deployment), it still operates at full trust weight. The paper acknowledges this is a limitation but doesn't quantify the damage a single compromised FLEET agent could cause - and with reinforcement mechanics, two compromised fleet agents could dominate the observation space on any topic.

### 1.10 No comparison to existing systems

There's no benchmark against existing multi-agent coordination frameworks (AutoGen, CrewAI, LangGraph, or even a simple shared-database baseline). All experiments compare Markspace against itself under different conditions. Without head-to-head comparison, the claims about superiority over message-passing remain theoretical arguments, not empirical findings.

---

## 2. What Was Fixed

Three changes to `docs/framework.md` and one to `experiments/trials/analysis.md`:

1. **Rewrote the opening argument** (framework.md, lines 22-38): Now explicitly acknowledges shared-state coordination is well-established. Positions the problems as what existing shared-state approaches lack for LLM agents specifically: a guard robust to adversarial agents, temporal decay modeling staleness, source-based trust weighting. Removes the false claim that shared-state superiority over message-passing "was made decades ago" - the two paradigms coexisted because they have different tradeoffs.

2. **Updated Context Accumulation** (framework.md, line 911): Added acknowledgment that shared-state systems face the same state-growth problem, not just message-passing.

3. **Rewrote the "Gap" claim in Related Work** (framework.md, line 977): Now opens with "Shared-state coordination is well-established" and explicitly states which components are borrowed from prior work (decay from ACO, trust from reputation models, enforcement from Hoare monitors, visibility from Bell-LaPadula) versus which are new contributions (the 5-type taxonomy, projected reads, empirical validation at scale).

4. **Added "Scope of adversarial testing" to trial analysis** (experiments/trials/analysis.md, line 83): Explicitly states all three adversarial modes test the guard's authorization layer, not semantic attacks. Identifies the structural reason (agents can't write observations) and points to unimplemented defenses.

5. **Rewrote defense-in-depth section** (framework.md, lines 305-322): Names the gap explicitly ("well-formed lies from authorized agents"), quantifies reinforcement amplification risk, adds specifics to each unimplemented check, and states honestly that a prompt-injected agent writing within scope will succeed today.

---

## 3. Implementation Plan: Defense-in-Depth

Implements the three "Design only" checks from the single-agent validation table: statistical envelope, absorbing barrier, and diagnostic probe. These close the gap identified in sections 1.2 and 1.7 - the protocol's inability to detect well-formed lies from authorized agents.

### 3.1 Build order

> **Superseded** by the revised build order in [section 3.12](#312-prerequisite-refactor-unify-the-write-path). The refactor adds `guard.write_mark()` as step 1, which changes the dependency chain. See section 3.12 for the current build order.

### 3.2 Write hooks in `space.py`

Add a post-write callback mechanism so the envelope can observe writes without tight coupling. ~15 lines changed, no new files. No pre-write gates - the guard handles gating via `write_mark()` (see [section 3.12](#312-prerequisite-refactor-unify-the-write-path)).

**New fields** in `MarkSpace.__init__`:
```python
self._write_hooks: dict[uuid.UUID, Callable[[uuid.UUID, AnyMark], None]] = {}
self._write_seq: int = 0
```

**New methods**:
```python
def add_write_hook(self, hook: Callable[[uuid.UUID, AnyMark], None]) -> uuid.UUID:
    """Register a post-write hook. Returns a handle for removal."""
    handle = uuid.uuid4()
    with self._lock:
        self._write_hooks[handle] = hook
    return handle

def remove_write_hook(self, handle: uuid.UUID) -> bool:
    """Remove a hook by handle. Returns True if found and removed."""
    with self._lock:
        return self._write_hooks.pop(handle, None) is not None
```

**Integration**: Increment `_write_seq` inside `self._lock`. Fire hooks **outside** the lock after `write()` completes, passing the sequence number. Marks are immutable, so safe to pass references outside the lock. A buggy hook cannot stall the space.

### 3.3 StatisticalEnvelope

New file: `markspace/envelope.py` (~470 lines).

**Architecture**: The envelope uses pluggable per-agent anomaly detectors. Each agent gets its own detector instance that learns that agent's individual baseline - no assumption that agents share the same activity pattern. Detection algorithms are swappable via the `AnomalyDetector` protocol.

**Types**:

```python
class EnvelopeVerdict(str, Enum):
    NORMAL = "normal"
    FLAGGED = "flagged"        # suspicious, not blocked
    RESTRICTED = "restricted"  # triggers barrier narrowing

class AnomalyDetector(ABC):
    """Per-agent anomaly detector. One instance per tracked agent."""
    @abstractmethod
    def observe(self, window_counts: dict[MarkType, int]) -> None: ...
    @abstractmethod
    def is_ready(self) -> bool: ...
    @abstractmethod
    def is_anomalous(self, current_counts: dict[MarkType, int]) -> bool: ...
    @abstractmethod
    def export_state(self) -> dict[str, Any]: ...
    @abstractmethod
    def import_state(self, data: dict[str, Any]) -> None: ...
    def seed_baseline(self, expected: dict[MarkType, float]) -> None: ...  # optional

@dataclass
class WelfordConfig:
    k_sigma: float = 3.5              # standard deviations for rate anomaly (accounts for bursty activity)
    min_samples: int = 10             # minimum windows before flagging
    type_shift_threshold: float = 0.5 # fraction deviation for type shift
    tracked_types: frozenset[MarkType] = frozenset({MarkType.OBSERVATION, MarkType.WARNING})

@dataclass
class EnvelopeConfig:
    window_seconds: float = 300.0    # tumbling window duration
    detector_factory: Callable[[uuid.UUID], AnomalyDetector] = ...  # receives agent_id, creates per-agent detector
    concentration_threshold: int = 3  # agents on same scope+topic = flag
    tracked_types: frozenset[MarkType] = frozenset({MarkType.OBSERVATION, MarkType.WARNING})
    global_escalation_threshold: int = 3
    exempt_agents: set[uuid.UUID] = field(default_factory=set)

@dataclass
class AgentStats:
    """Diagnostic view of per-agent envelope state (backwards-compatible)."""
    current_window: _WindowCounts
    previous_window: _WindowCounts
    welford_n: dict[MarkType, int]
    welford_mean: dict[MarkType, float]
    welford_m2: dict[MarkType, float]
    completed_windows: int
    restricted: bool
```

**Default detector**: `WelfordDetector` - streaming mean/variance using Welford's online algorithm. Establishes per-agent baseline from completed windows, flags when current window exceeds mean + k*stddev for any tracked mark type, or when type distribution shifts significantly.

**Class**: `StatisticalEnvelope`

```python
class StatisticalEnvelope:
    def __init__(self, config: EnvelopeConfig | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self._config = config or EnvelopeConfig()
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._agents: dict[uuid.UUID, _AgentState] = {}
        # Cross-agent: (scope, topic) -> set of agent_ids in current window
        self._recent_writers: dict[tuple[str, str], set[uuid.UUID]] = {}
```

**Key methods**:

| Method | Purpose |
|---|---|
| `record(agent_id, mark)` | Called via post-write hook on every `space.write()`. Filters by `tracked_types`. Increments per-type count in current window, tracks (scope, topic) for concentration. Rotates window when expired, feeding completed window into the agent's detector. Ignores exempt agents. |
| `check(agent_id) -> EnvelopeVerdict` | Called by `guard.write_mark()` and `guard.pre_action()`. Drains stale windows, then: (1) P40 monotonicity - sticky RESTRICTED, (2) P41 cold start - NORMAL if detector not ready, (3) delegate to detector's `is_anomalous()` for per-agent behavioral check, (4) cross-agent concentration check (FLAGGED only). |
| `get_stats(agent_id) -> AgentStats` | Read-only diagnostic view of per-agent state. |
| `add_exempt_agent(agent_id)` | Add an agent ID to the exempt set. `record()` ignores writes from exempt agents. Use sparingly - prefer `seed_baseline()` for external bots. |
| `seed_baseline(agent_id, expected_activity)` | Pre-seed detector from declared expected activity. Eliminates cold-start blind spot for agents with known behavior. |
| `reset(agent_id, principal_token)` | Clear RESTRICTED status. Principal-only. |
| `export_stats() / import_stats(data)` | Serialize/restore detector states for persistence. |

**Declared baselines**: Agents can declare expected activity in their manifest via `expected_activity: dict[str, float]` (mark type value to expected marks per hour). The rate is window-independent - the envelope converts to per-window counts using `window_seconds`, so agent creators don't need to know the window size. The envelope calls `detector.seed_baseline()` at registration, pre-loading the Welford accumulators. The synthetic variance is 25% of mean squared (stddev = 0.5 * mean), so small natural deviations don't false-positive. Real observations refine the seeded baseline over time. This is the recommended approach for external bots and integrations - they should be monitored with their own baselines, not exempted.

**Window rotation**: Tumbling windows with capped zero-drain. When a window expires, the completed window's counts are fed into the detector via `observe()`. If more than one window elapsed (agent was idle or clock jumped), at most one idle (zero-count) window is recorded to prevent overnight gaps from flooding the baseline with phantom data.

**Thread safety**: Own `threading.RLock`, separate from space lock. Only held during stats updates.

**What it catches vs. doesn't**:
- Catches: flood attacks (rate spike), collusion (concentration), behavioral pivots (type shift)
- Does not catch: slow semantic drift where rate and type distribution stay normal but content is subtly wrong. That's the diagnostic probe's job.

**Custom detectors**: Implement `AnomalyDetector` and pass a factory to `EnvelopeConfig.detector_factory`. The factory receives the agent's UUID, so it can return different detector types or configs per agent class. Potential alternatives: DBSCAN clustering, isolation forest, or any algorithm that can learn a per-agent baseline from streaming window counts.

### 3.4 AgentBarrier

New file: `markspace/barrier.py` (~80 lines).

Mutable permission restriction overlay on the frozen `Agent` model. No changes to `core.py`.

```python
@dataclass
class AgentBarrier:
    agent_id: uuid.UUID
    _revoked: set[tuple[str, str]]   # (scope_name, mark_type_value)
    _require_need: bool
    _principal_token: uuid.UUID
```

| Method | Purpose |
|---|---|
| `is_allowed(scope, mark_type) -> bool` | Check if (scope, mark_type) has NOT been revoked. |
| `narrow(scope, mark_type)` | Revoke a permission. Monotonic - no undo without principal. |
| `require_need()` | Force Need marks before any action in scope. |
| `needs_required() -> bool` | Check if need requirement is active. |
| `restore(scope, mark_type, principal_token) -> bool` | Re-grant a revoked permission. Requires correct principal token. Returns False on token mismatch. |
| `restore_all(principal_token) -> bool` | Re-grant all permissions. Principal-only. |

**Monotonicity guarantee**: `narrow()` is a set add. There is no `widen()` method. Only `restore()` and `restore_all()` can re-grant, and both require the principal token. A compromised agent cannot restore its own permissions.

### 3.5 Guard integration

Modifications to `markspace/guard.py` (~110 lines changed, including `write_mark()` from [section 3.12](#312-prerequisite-refactor-unify-the-write-path)).

**`Guard.__init__` gains**:
```python
def __init__(self, space: MarkSpace, block_self_rebook: bool = False,
             envelope: StatisticalEnvelope | None = None,
             principal_token: uuid.UUID | None = None) -> None:
    # ... existing init ...
    self.envelope = envelope
    self._barriers: dict[uuid.UUID, AgentBarrier] = {}
    self._barrier_lock = threading.Lock()
    self._principal_token = principal_token or uuid.uuid4()

    # System agent for guard-originated warnings/needs (envelope restriction notices, barrier audit trail).
    # Exempt from envelope monitoring to prevent feedback loops.
    self._system_agent = Agent(
        name="_guard",
        scopes={"*": ["warning", "need"]},
        read_scopes=frozenset(),
    )

    # Wire envelope to space write hooks
    if envelope is not None:
        envelope.add_exempt_agent(self._system_agent.id)
        space.add_write_hook(lambda agent_id, mark: envelope.record(agent_id, mark))
```

**`write_mark()` - the primary enforcement path** for all non-guard-originated writes (agents, external bots, system processes). See [section 3.12](#312-prerequisite-refactor-unify-the-write-path) for rationale.

```python
def write_mark(self, agent: Agent, mark: AnyMark) -> uuid.UUID:
    """Write a non-contested mark through the guard's enforcement stack."""
    # Reject intents/actions - those must go through pre_action()/execute()
    if mark.mark_type in (MarkType.INTENT, MarkType.ACTION):
        raise ValueError(
            f"Cannot write {mark.mark_type.value} via write_mark(). "
            f"Use execute() or pre_action()/post_action() for contested writes."
        )
    # Barrier check
    # Envelope check
    # Delegate to space.write()
```

**`_pre_action_inner()` gains two new checks** after existing authorization (lines 222-231), before the action-mark check (line 237):

1. **Barrier check**: If a barrier exists for this agent and revokes the requested (scope, mark_type), return DENIED. If `require_need` is set, check for an unresolved Need mark in this scope - if absent, return BLOCKED.

2. **Envelope check**: If `envelope.check()` returns RESTRICTED, auto-create/update a barrier via `_apply_restriction()`, return DENIED. If FLAGGED, allow but could emit a warning mark (defense in depth, not blocking).

Both `write_mark()` and `_pre_action_inner()` run the same barrier + envelope checks. The difference: `write_mark()` has no conflict resolution or resource locking. `_pre_action_inner()` adds those on top.

**New private method**:
```python
def _apply_restriction(self, agent: Agent, scope: str) -> None:
    """Create/update barrier in response to RESTRICTED verdict."""
    # Create barrier if none exists
    # narrow(scope, MarkType.ACTION)
    # narrow(scope, MarkType.OBSERVATION)
    # require_need(scope)
    # Write invalidation warnings via self.space.write(self._system_agent, ...)
    # Write audit Need via self.space.write(self._system_agent, ...)
    # Note: system agent writes go through space.write() directly (guard-originated)
    # and are exempt from envelope monitoring.
```

**New public methods**:
```python
def get_barrier(self, agent_id: uuid.UUID) -> AgentBarrier | None
def set_barrier(self, agent_id: uuid.UUID, barrier: AgentBarrier) -> None
```

**`resolve_deferred()` change**: Add barrier check to the authorization re-verification loop. If a barrier was applied between intent-writing and resolution time, the intent is denied at resolution.

### 3.6 DiagnosticProbe

New file: `markspace/probe.py` (~200 lines).

**Types**:
```python
class ProbeVerdict(str, Enum):
    HEALTHY = "healthy"
    SUSPICIOUS = "suspicious"
    COMPROMISED = "compromised"

@dataclass
class ProbeConfig:
    probe_scope: str = "diagnostics"
    canary_topic: str = "probe-canary"
    response_window: float = 30.0   # seconds to wait for agent response
    min_strength: float = 0.5

@dataclass
class ProbeResult:
    agent_id: uuid.UUID
    agent_name: str
    verdict: ProbeVerdict
    probe_mark_id: uuid.UUID
    details: str
    timestamp: float
```

**Class**: `DiagnosticProbe`

Not an agent - a system-level service. Creates a dedicated probe agent identity for writing synthetic marks, but does not participate in coordination.

```python
class DiagnosticProbe:
    def __init__(self, space: MarkSpace,
                 config: ProbeConfig | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self._space = space
        self._config = config or ProbeConfig()
        self._clock = clock or space.now
        self._probe_agent = Agent(
            name="_diagnostic_probe",
            scopes={self._config.probe_scope: ["observation", "warning"]},
            read_scopes=frozenset({self._config.probe_scope}),
        )
        self._results: list[ProbeResult] = []
```

| Method | Purpose |
|---|---|
| `inject_canary(target_scope, content) -> uuid.UUID` | Write a synthetic high-strength observation into the target scope. Returns the canary mark ID. |
| `check_agent_read(agent_id, canary_id, agent_marks_after) -> ProbeVerdict` | MVP: verify canary is visible in the agent's read set. Future: verify agent's subsequent marks reference or acknowledge canary content. |
| `run_probe(target_scope, target_agent_ids, content) -> list[ProbeResult]` | Full cycle: inject canary, check each agent, accumulate results. |
| `get_results() -> list[ProbeResult]` | Return all probe results. |

**Scope setup**: Scopes are implicit in MarkSpace - they exist when an agent declares them in its `scopes` dict. The probe agent's `scopes` declaration (above) is sufficient. No registration step needed. The experiment harness just needs to ensure target agents have `read_scopes` that include the diagnostic scope so canaries are visible.

**Write path**: The probe writes via `self._space.write()` directly, not `guard.write_mark()`. This is an intentional exemption: the probe is system infrastructure (like the guard's own writes), writes only to an isolated diagnostic scope, and must not be subject to envelope monitoring (which would create a feedback loop when the probe injects canaries). The probe agent is added to the envelope's `exempt_agents` set alongside the guard's system agent.

**MVP vs. full version**: The MVP checks mark visibility only (is the canary in the read set?). A full version would:
- Inject canary observations with specific content (e.g., "room X under maintenance")
- Wait for the target agent's next round
- Check whether the agent avoided booking room X (canary pattern)
- Or inject two contradictory observations and verify the agent doesn't accept both (consistency check)

The semantic response checking requires domain knowledge or embedding-based comparison and is deferred to a future iteration.

### 3.7 Tests

| File | Tests | Lines |
|---|---|---|
| `tests/test_envelope.py` | 9 tests: steady writes stay NORMAL, rate spike FLAGGED, sustained anomaly RESTRICTED, type shift detection, concentration detection, Welford non-negativity, window rotation, thread safety, new-agent no false positives | ~150 |
| `tests/test_barrier.py` | 7 tests: narrow blocks, monotonicity, restore wrong token fails, restore correct token works, require_need flag, isolation between agents, guard cycle integration | ~80 |
| `tests/test_defense_integration.py` | 4 tests: envelope triggers barrier, narrowed agent blocked, principal restores after false positive, probe + envelope together | ~100 |
| `tests/test_probe.py` | 7 tests: canary readable, HEALTHY when visible, COMPROMISED when removed, SUSPICIOUS when GC'd, full run_probe cycle, results accumulate, no interference with other scopes | ~100 |

### 3.8 Exports

Add to `markspace/__init__.py`:

```python
from markspace.envelope import (
    AnomalyDetector, EnvelopeConfig, EnvelopeVerdict,
    StatisticalEnvelope, WelfordConfig, WelfordDetector,
)
from markspace.barrier import AgentBarrier
from markspace.probe import DiagnosticProbe, ProbeVerdict, ProbeResult, ProbeConfig
```

### 3.9 Risks

| Risk | Mitigation |
|---|---|
| Lock contention: `envelope.record()` inside `space._lock` | `record()` is O(1) Welford update. Move hooks outside lock if profiling shows issue. |
| False positives on new agents | Per-agent detector readiness (`is_ready()`) - no flagging until enough baseline data collected. Default `WelfordDetector` uses `min_samples` threshold. |
| Barrier monotonicity bug allows re-widening | Property test: `narrow()` followed by any non-principal call never re-enables. |
| Probe scope conflicts with real coordination | Dedicated diagnostic scope, probe agent has no production-scope permissions. |
| Envelope tuning: wrong detector parameters | Detector config is per-deployment. Detection algorithm is swappable via `AnomalyDetector` protocol. Default `WelfordDetector` uses k=3.0 (standard for anomaly detection). |

### 3.10 Design decisions (resolved)

#### Critical flaw in the original plan

The original plan puts `envelope.check()` in `guard.pre_action()`. But `pre_action()` only wraps intent/action writes on contested resources. Observations and warnings are written directly via `space.write()`, bypassing the guard entirely (as the space.py docstring notes: "Direct writes are appropriate for observations, warnings, and needs which don't require conflict checks"). The entire attack surface we're defending - fleet agents writing well-formed false observations - goes through a path the guard never touches.

**Resolution**: Add `guard.write_mark()` as the single enforcement boundary for all non-guard-originated writes (see [section 3.12](#312-prerequisite-refactor-unify-the-write-path)). The envelope records via post-write hooks on `space.write()` (observation only), but gating happens in the guard - no pre-write gates on the space. The space stays a pure storage layer.

#### D1. Write hooks: outside the lock, with sequence numbers

Fire post-write hooks **outside** `self._lock`. Assign a monotonic `_write_seq` (incremented inside the lock) and pass it to hooks. The envelope uses this for ordering when needed (concentration detection).

Rationale: A buggy hook cannot stall the space. The `_write_seq` costs one integer increment per write - negligible. Hooks are handle-based (`add_write_hook` returns a UUID, `remove_write_hook` removes by UUID) for clean test isolation.

#### D2. Envelope is reactive for the first window, preventive after

The envelope cannot flag an agent until its detector reports ready (`is_ready()`). For the default `WelfordDetector`, this requires `min_samples` completed windows. The first anomalous window gets through entirely - this is inherent to statistical detection. Once the envelope sets an agent to RESTRICTED:

- The barrier blocks future intent/action writes via `guard.pre_action()`.
- The barrier blocks future observation/warning/need writes via `guard.write_mark()`.
- Both paths are covered by the guard. The only escape is principal restoration.

Document this explicitly: the defense has a cold-start window during which no behavioral detection operates (duration depends on the detector - for the default `WelfordDetector` with 10 samples and 300s windows, that's ~50 minutes). Acceptable for the experiment runner where rounds are discrete. Production deployments with tighter requirements should pre-seed detector state via `import_stats()` from warm storage (see D7).

#### D3. Tumbling windows with per-agent detectors

Use tumbling windows. Each agent's detector receives completed window counts via `observe()` and evaluates the current (in-progress) window via `is_anomalous()`. The default `WelfordDetector` checks both rate anomaly (current > mean + k*stddev) and boundary-straddling bursts (current + previous > 2*threshold).

Window rotation caps zero-drain to at most one idle window per gap, preventing overnight clock jumps (e.g., 20h gaps in AM/PM round schedules) from flooding the baseline with phantom zero-count data.

Memory: O(|MarkType|) per agent for detector state, plus O(|MarkType|) for the current window counts. This is O(1) per write and O(agents * |MarkType|) total memory. Scales to 10K+ agents without concern.

#### D4. Scope-scoped narrowing with escalation

The barrier narrows only the flagged scope. `require_need` becomes a `set[str]` of scopes (not a bool), so Need marks are required only in the flagged scope.

Escalation policy:
- **First flag on a scope**: `narrow(scope, MarkType.OBSERVATION)` + `narrow(scope, MarkType.ACTION)` + `require_need(scope)`. Agent can still read the scope and write in other scopes.
- **Second flag on a different scope** (same agent): same per-scope narrowing. Two scopes restricted independently.
- **Third flag (any scope)**: escalate to global. `narrow_all(MarkType.OBSERVATION)` + `narrow_all(MarkType.ACTION)` + `require_need_all()`. Write a Need mark to the principal: "Agent X flagged on 3+ scopes, global restriction applied, requires review."

This bounds false-positive blast radius (one scope) while ensuring that a truly compromised agent hitting multiple scopes gets progressively contained. The escalation count (3) is configurable via `EnvelopeConfig.global_escalation_threshold`.

#### D5. Probe ships two methods, not one

The probe is not a space health check - it tests agents. Ship two methods from the start:

1. `check_canary_visibility(scope, canary_id) -> bool` - verifies the canary is readable. Pure space test. Useful for smoke-testing scope/visibility configuration.

2. `check_agent_response(agent_id, canary_id, marks_after: list[AnyMark]) -> ProbeVerdict` - takes the agent's post-injection marks (provided by the harness, which already captures per-round message logs) and checks whether any reference the canary's scope+topic. Returns HEALTHY if the agent wrote at least one mark referencing the canary topic, SUSPICIOUS if the agent wrote marks but none reference it, COMPROMISED if the agent wrote no marks at all (suggesting it's stuck or ignoring its environment).

The harness passes `marks_after` because the space doesn't track per-agent read history and adding that would be a large change. The experiment runner already collects per-agent marks per round - it's a matter of filtering and passing them in.

#### D6. Filter by mark type + exempt agents

Two filtering mechanisms prevent false positives from guard-originated writes:

**Mark type filtering**: The guard's internal writes are intents and actions. The attack surface is observations and warnings. The envelope tracks only `tracked_types` (default: `{MarkType.OBSERVATION, MarkType.WARNING}`). Intent/action writes are invisible to the envelope, so guard-internal writes in `pre_action()`/`post_action()` never trigger it.

**Agent exemption**: The guard's `_system_agent` and the probe's `_probe_agent` write warnings and needs as system infrastructure (restriction notices, audit trail, canary injection). These writes go through `space.write()` directly and fire the post-write hook. Without exemption, a burst of restriction warnings could flag the system agent. `EnvelopeConfig.exempt_agents: set[uuid.UUID]` holds agent IDs that `record()` ignores entirely.

**Why not track needs?** Needs don't have a `topic` field (they have `question` + `context`), so concentration detection (which tracks `(scope, topic)` pairs) doesn't apply. A compromised agent flooding bogus needs is a real attack vector, but needs are principal-facing - the principal already reviews them, and `max_marks_per_agent` quota caps the volume. If need flooding becomes a concern, add `MarkType.NEED` to `tracked_types` and use `question` as the concentration key.

Configuration: `EnvelopeConfig.tracked_types: frozenset[MarkType]` defaulting to `{MarkType.OBSERVATION, MarkType.WARNING}`. `EnvelopeConfig.exempt_agents: set[uuid.UUID]` defaulting to empty (populated by guard/probe init). Detection-specific parameters (k_sigma, min_samples, type_shift_threshold) live on the detector config (e.g., `WelfordConfig`), not on `EnvelopeConfig`. Both extensible per deployment.

#### D7. In-memory for MVP, serialization interface for production

Acceptable for MVP. Add a serialization interface to `StatisticalEnvelope`:

```python
def export_stats(self) -> dict[str, Any]:
    """Export all agent stats as a serializable dict."""

def import_stats(self, data: dict[str, Any]) -> None:
    """Import previously exported stats. Additive - merges with existing."""
```

The experiment runner doesn't call these. A production deployment calls `export_stats()` on shutdown and `import_stats()` on startup, using whatever storage backend it has. The interface is JSON-serializable dicts - no dependency on PostgreSQL or Redis.

#### D8. Concentration detection: flag only, never auto-restrict

(New decision, resolves missing item #7.)

Concentration (3+ agents writing same scope+topic in one window) is structurally identical to legitimate corroboration. The reinforcement function is designed to reward convergent observations. The envelope must not fight it.

**Decision**: Concentration triggers FLAGGED only, never RESTRICTED. It cannot cause auto-narrowing on its own. It can contribute to escalation in combination with a per-agent rate or type anomaly (i.e., if an agent is individually anomalous AND part of a concentration cluster, that's more suspicious than either alone). But concentration alone only generates a warning mark for the principal.

This means the `check()` function returns:
- RESTRICTED: per-agent rate anomaly OR per-agent type shift (individual behavioral anomaly)
- FLAGGED: concentration detected (cross-agent pattern, not individually anomalous)
- NORMAL: nothing detected

A FLAGGED verdict writes a warning mark to the space ("concentration detected on scope X topic Y by agents [A, B, C]") but does not create a barrier.

### 3.11 Missing items (resolved)

#### M1. Spec properties

New properties for `docs/spec.md` Section 15, extending P56:

| ID | Property | Section | Status |
|---|---|---|---|
| P40 | Envelope Monotonicity | 9.7 | If `check(agent)` returns RESTRICTED at time t, it returns RESTRICTED for all t' > t until `reset(agent, principal_token)` is called. | MANDATORY |
| P41 | Envelope Cold Start Safety | 9.7 | `check(agent)` MUST return NORMAL for any agent whose detector reports `is_ready() == False`. | MANDATORY |
| P44 | Barrier Monotonicity | 9.8 | If `is_allowed(scope, mark_type)` returns False, it returns False for all subsequent calls until `restore(scope, mark_type, principal_token)` succeeds. | MANDATORY |
| P45 | Barrier Principal Exclusivity | 9.8 | `restore()` MUST reject any token that does not match the barrier's `_principal_token`. | MANDATORY |
| P42 | Write-Mark Atomicity | 9.7 | A `write_mark()` rejection (barrier or envelope) MUST prevent the mark from being stored. No partial writes. | MANDATORY |
| P33 | Hook Non-Interference | 8.6 | A post-write hook failure MUST NOT affect the stored mark or subsequent reads. | MANDATORY |
| P46 | Probe Mark Isolation | 9.9 | Probe-injected marks MUST be writable only to the configured diagnostic scope. The probe agent MUST NOT have write access to production scopes. | OPTIONAL |
| P43 | Escalation Boundedness | 9.7 | Automatic barrier escalation from envelope MUST NOT restrict more than one scope per envelope flag. Global restriction MUST require `global_escalation_threshold` flags. | MANDATORY |

New spec sections:
- **9.7 Statistical Envelope** - detection algorithm, cold start, escalation policy
- **9.8 Absorbing Barrier** - monotonicity, scope-scoped narrowing, principal restoration
- **9.9 Diagnostic Probe** - canary injection, response checking, scope isolation
- **8.6 Write Hooks** - post-write hook semantics, sequence numbers, ordering guarantees

#### M2. Experiment integration

Add to build order as step 12 (after full test suite):

- Add `poisoning`, `collusion`, `slow_drift` adversarial modes to `scenario.py`
- Add `report_observation` tool to adversarial agents (writes observation marks within scope)
- Wire envelope + barrier into `run.py`'s experiment harness
- Run adversarial experiments with new modes, capture results
- Update `experiments/trials/analysis.md` with new adversarial results

This is a separate PR after the core implementation lands. The modules must work in isolation first.

#### M3. Envelope-to-warning path

When `_apply_restriction()` creates a barrier, also write warning marks invalidating the flagged agent's recent observations in the flagged scope:

```python
def _apply_restriction(self, agent: Agent, scope: str) -> None:
    # ... create/update barrier ...

    # Invalidate recent observations from this agent in the flagged scope
    recent_obs = self.space.read(scope=scope, mark_type=MarkType.OBSERVATION)
    agent_obs = [m for m in recent_obs if m.agent_id == agent.id]
    for obs in agent_obs:
        self.space.write(
            self._system_agent,  # guard's own agent identity
            Warning(
                scope=scope,
                invalidates=obs.id,
                topic="envelope-restriction",
                reason=f"Agent '{agent.name}' flagged by statistical envelope",
                severity=Severity.CAUTION,
            ),
        )
```

This requires the guard to have a `_system_agent` identity with warning-write permission on all scopes. The guard already writes internal marks (intents, actions) using the agent's identity - the system agent is a separate identity for guard-originated marks. Add to `Guard.__init__`:

```python
self._system_agent = Agent(
    name="_guard",
    scopes={"*": ["warning", "need"]},
    read_scopes=frozenset(),
)
```

Other agents reading the scope see the warnings, which reduce the flagged agent's observation strength via the existing warning invalidation mechanism. No new machinery needed - the warning system already handles this.

Note: These `space.write()` calls from the system agent fire the post-write hook, but the system agent is in the envelope's `exempt_agents` set (see D6), so they don't affect envelope stats.

#### M4. Barrier audit trail

When a barrier is created or narrowed, write a Need mark from the system agent:

```python
self.space.write(
    self._system_agent,
    Need(
        scope=scope,
        question=f"Agent '{agent.name}' restricted: {mark_type.value} revoked in '{scope}'",
        context={"agent_id": str(agent.id), "trigger": "envelope", "scope": scope},
        priority=0.9,
        blocking=False,
    ),
)
```

Non-blocking Need (the system doesn't stop), but the principal sees it in aggregated needs with full context. The `aggregate_needs()` function already clusters by scope, so all barrier events in the same scope are grouped together.

#### M5. Probe feeds into barrier via guard callback

The probe doesn't auto-restrict. COMPROMISED verdict writes a Need mark. The probe writes via `space.write()` directly (not `guard.write_mark()`) because it is system infrastructure writing to an isolated diagnostic scope - same exemption as the guard's system agent. The probe agent is in the envelope's `exempt_agents` set.

```python
self._space.write(
    self._probe_agent,
    Need(
        scope=self._config.probe_scope,
        question=f"Probe: agent '{agent_name}' COMPROMISED - failed canary check",
        context={"agent_id": str(agent_id), "canary_id": str(canary_id)},
        priority=1.0,
        blocking=True,
    ),
)
```

Rationale: The probe's COMPROMISED verdict could be a false positive (the agent legitimately didn't reference the canary topic). Auto-restriction based on a single probe check is too aggressive. The principal sees the blocking Need, reviews, and decides whether to manually apply a barrier (`guard.set_barrier()`). This matches the YIELD_ALL philosophy - contested decisions escalate to the principal.

If the deployment wants auto-restriction, pass an optional `on_compromised: Callable[[uuid.UUID], None]` callback to the probe. The harness can wire this to `guard._apply_restriction()`. Not wired by default.

#### M6. Hook removal

`add_write_hook()` returns a UUID handle. `remove_write_hook(handle)` removes by handle. Hooks are stored in a `dict[uuid.UUID, Callable]` so removal is O(1). Defined in [section 3.2](#32-write-hooks-in-spacepy).

#### M7. Concentration stays FLAGGED-only

Resolved in D8 above. Concentration never auto-restricts. Only generates a warning mark for the principal. The reinforcement function and the envelope are not in conflict because the envelope doesn't suppress convergent observations - it only alerts the principal to review them.

### 3.12 Prerequisite refactor: unify the write path

#### The problem

The codebase has two write paths:

1. **Guarded**: `guard.pre_action()` -> tool -> `guard.post_action()`. Enforces conflict resolution, authorization, locking. Used for intents/actions on contested resources.
2. **Unguarded**: `space.write()` directly. Only schema validation + scope authorization. Used for observations, warnings, needs.

The defense-in-depth checks need to intercept observation and warning writes - the primary attack surface. Without refactoring, the plan bolts gates onto `space.write()`, creating two parallel enforcement stacks (guard for intents/actions, gates for observations/warnings). This is fragile: every future check must be wired into both paths.

#### The fix

Add a lightweight guard method for non-contested writes. The guard becomes the single enforcement boundary for all writes except its own internal bookkeeping (intent/action marks in `pre_action`/`post_action`). This includes LLM agents, external bots, and any other caller.

```python
class Guard:
    def write_mark(self, agent: Agent, mark: AnyMark) -> uuid.UUID:
        """
        Write a non-contested mark (observation, warning, need) through
        the guard's enforcement stack.

        No conflict resolution. No resource locking. But does run:
        - Barrier check (is this agent restricted?)
        - Envelope check (is this agent anomalous?)
        - Schema/scope validation (delegated to space.write())

        For intents and actions on contested resources, use execute()
        or pre_action()/post_action() instead.
        """
```

This is ~20 lines of new code in the guard. It checks the barrier and envelope, then delegates to `space.write()` for the actual storage. The space remains the storage layer; the guard remains the enforcement layer.

#### What changes

**`guard.py`** (+25 lines):
- New `write_mark(agent, mark)` method. Checks barrier, checks envelope, calls `space.write()`.
- Guard-internal writes (intent/action in `pre_action`/`post_action`) continue calling `space.write()` directly - they are guard-originated and should bypass the envelope.

**`space.py`** (+15 lines):
- Add post-write hooks only (no gates needed - the guard handles gating).
- `add_write_hook(hook) -> handle`, `remove_write_hook(handle) -> bool`.
- Hooks fire outside the lock with a sequence number for ordering.
- The envelope registers as a hook for recording (stats tracking), not gating.

**Experiment call sites** (changes in `stress_test/`, `composition_stress/`):
- **External bot writes go through `guard.write_mark()`**. The parking bot and building ops bot are external systems, not trusted infrastructure. Their writes carry external trust weight and need envelope monitoring like any other non-guard source.
- Agent-originated writes (lunch Need in `run.py:891`) also change to `guard.write_mark()`.
- Guard-internal writes (intent/action in `pre_action`/`post_action`) stay as `space.write()` - these are guard-originated.
- The distinction is clear: if the caller is the guard itself, use `space.write()`. Everything else - LLM agents, external bots, system processes - uses `guard.write_mark()`.

**Scenario bot definitions** (2 changes in `scenario.py`):
- `_make_parking_bot()` gains `max_source=Source.EXTERNAL_VERIFIED` (trust weight 0.7). Currently defaults to FLEET (1.0), which overstates trust in an external parking system.
- `_make_building_ops_bot()` gains `max_source=Source.EXTERNAL_VERIFIED` (trust weight 0.7). Same reasoning - building ops is external infrastructure reporting into the space, not a fleet agent.

**Composition stress experiment** (9 call sites in `composition_stress/run.py`):
- The composition stress experiment has no guard - it tests pipeline composition patterns (sensor -> filter -> aggregator -> alerter -> actor) with deterministic agents. All 9 `space.write()` calls stay as-is for now.
- When the defense-in-depth modules land, the composition stress experiment should wire up a guard and route agent writes through `guard.write_mark()`. This is part of M2 (experiment integration), not the core implementation. The pipeline agents are fleet agents (`source=Source.FLEET`) - they are deterministic code, not LLM-driven, so envelope monitoring adds safety margin but isn't critical.

**Tests** (103 call sites):
- Unit tests that test space behavior directly keep using `space.write()`. These test the storage layer in isolation.
- Tests that test guard behavior through agents should use `guard.write_mark()` for observations/warnings. In practice, most existing tests test intent/action via `guard.execute()` - observation/warning tests mostly test the space directly, which is correct.
- **No mass rename needed.** The refactor adds a new path; it doesn't remove the old one.

#### What doesn't change

- `space.write()` stays public. It's the storage API. Direct calls bypass the guard intentionally (for tests and guard-internal writes only).
- `guard.execute()` / `pre_action()` / `post_action()` stay unchanged. They handle contested resources.
- `core.py` stays untouched. No type changes.
- The guard's internal writes (intent in `pre_action`, action in `post_action`) keep using `space.write()` directly. They are guard-originated and should not trigger the envelope.
- The probe's writes (canary injection, COMPROMISED needs) also use `space.write()` directly - system infrastructure writing to an isolated diagnostic scope.
- These two (guard internals + probe) are the only production code that calls `space.write()` directly. Both have their agent IDs in the envelope's `exempt_agents` set.

#### Why this is better than gates

The original plan added pre-write gates to `space.py` - callable hooks that can reject writes. This has problems:

1. **The space becomes an enforcement layer.** It was designed as a storage layer. Adding rejection logic blurs the boundary.
2. **Two enforcement stacks.** The guard enforces for intents/actions, gates enforce for observations/warnings. Future checks must be wired into both.
3. **Gate ordering matters.** If multiple gates exist, their execution order affects behavior. The guard already handles ordering (barrier before envelope, both before conflict resolution).

With `guard.write_mark()`, the guard is the single enforcement boundary. The space is pure storage. One stack, one place to add checks, one place to audit.

#### Revised build order

```
 1. guard.py: add write_mark()                -- new lightweight write path
 2. space.py: add post-write hooks            -- recording only, no gates
 3. envelope.py                               -- hooks into space for recording, checked by guard
 4. test_envelope.py
 5. barrier.py
 6. test_barrier.py
 7. guard.py: wire envelope + barrier          -- into both write_mark() and pre_action()
 8. test_defense_integration.py
 9. probe.py
10. test_probe.py
11. Update experiment call sites              -- agent writes go through guard.write_mark()
12. __init__.py exports
13. spec.md updates (P40-P43, sections 8.6, 9.7-9.9)
14. Full test suite
```

Step 1 is the refactor. Steps 2-14 are the defense-in-depth implementation, now cleaner because there's one enforcement boundary.

### Revised file change summary

Reflects the resolved design decisions and prerequisite refactor. Key changes from original plan: guard gains `write_mark()` as single enforcement boundary for all agent writes, space gains post-write hooks only (no gates - guard handles gating), guard gains a system agent for warning/need writes, barrier gains scope-scoped require_need, envelope filters by mark type.

| File | Change | Estimated lines |
|---|---|---|
| `markspace/envelope.py` | NEW - tracking, Welford, concentration, exempt agents | ~200 |
| `markspace/barrier.py` | NEW - scope-scoped narrowing, escalation counter | ~100 |
| `markspace/probe.py` | NEW - canary injection, visibility + response checking | ~220 |
| `markspace/space.py` | MODIFY - post-write hooks with handles and sequence numbers (no gates) | ~25 |
| `markspace/guard.py` | MODIFY - `write_mark()`, barrier checks, system agent, warning writes on restriction | ~110 |
| `experiments/stress_test/run.py` | MODIFY - agent-originated writes use `guard.write_mark()` | ~5 |
| `markspace/__init__.py` | MODIFY - exports | ~15 |
| `docs/spec.md` | MODIFY - sections 8.6, 9.7, 9.8, 9.9; properties P40-P43 | ~150 |
| `tests/test_envelope.py` | NEW | ~180 |
| `tests/test_barrier.py` | NEW | ~100 |
| `tests/test_probe.py` | NEW | ~120 |
| `tests/test_defense_integration.py` | NEW | ~120 |
| **Total** | | **~1,335** |
