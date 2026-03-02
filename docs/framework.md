# Stigmergic Coordination for Autonomous Agent Fleets

Autonomous agents are non-deterministic and fallible. A system that depends on their behavior inherits both properties. [Agents of Chaos](https://agentsofchaos.baulab.info/report.html) documents what happens when autonomous agents operate without structural constraints: unauthorized compliance with non-owners, sensitive data disclosure, resource exhaustion loops, identity spoofing, and cross-agent propagation of unsafe practices, covering 16 case studies in a red-teaming exercise with 6 fully autonomous agents. [Google's scaling research](https://arxiv.org/abs/2512.08296) (Kim et al., 2025) finds that multi-agent coordination degrades performance by 39-70% on sequential tasks and amplifies errors up to 17.2x, even when it helps on parallelizable work. In both cases, coordination is implemented as agent behavior rather than infrastructure. The coordination layer between agents determines system-level outcomes.

Consensus-based coordination, the dominant approach, makes the problem worse. Three structural problems:

1. **Model choice matters more than mechanism design.** Performance saturates or declines beyond 3-4 agents regardless of coordination architecture, and model capabilities interact with architecture choice ([Kim et al., 2025](https://arxiv.org/abs/2512.08296)).
2. **Honest-majority cliff edge.** Classical consensus mechanisms (voting, median, aggregation) work below the corruption threshold and fail at it. A Byzantine fault is any agent failure where the agent continues operating but produces incorrect, misleading, or adversarial output: the defining failure mode of LLM agents under prompt injection. Consensus tolerates up to *f* Byzantine agents out of *3f + 1* total, but at *f + 1* the system fails completely rather than degrading proportionally. Graceful degradation under Byzantine faults remains an open problem ([Lamport et al., 1982](https://doi.org/10.1145/357172.357176)).
3. **Behavioral detection creates attack surfaces.** The more sophisticated the coordination protocol, the more handles it exposes to an adversary ([Piet et al., 2025](https://doi.org/10.1016/j.cose.2025.104467)).

Consensus addresses agreement (state machine replication, total ordering, leader election). But the operative problem in agent fleets is coordination: running hundreds of agents across different tasks, time horizons, and trust boundaries without them stepping on each other, going stale, or requiring constant attention.

## Coordination Through Environment, Not Communication

Pierre-Paul Grassé coined "stigmergy" in 1959 studying termite mound construction. The word derives from Greek *stigma* (mark) and *ergon* (work), meaning coordination stimulated by marks left in the shared environment. The key observation: no termite knows the building plan. Each termite follows local rules: pick up mud, deposit where pheromone concentration is highest, move on. The structure emerges from the marks, not from any architect.

<p align="center"><img src="../figures/committee_vs_stigmergic.svg" alt="Committee vs Stigmergic model" width="900"></p>

Properties that matter for agent systems:

- **No direct agent-to-agent communication.** Agents read and write marks. They don't know how many other agents exist, what they're doing, or whether they're honest.
- **Scalable.** Going from 5 to 500 agents doesn't change any agent's logic or coordination overhead.
- **Robust to failure.** If an agent dies, its marks persist. Other agents continue without a single point of failure or quorum requirement.
- **Decomposable.** Each agent is a closed unit: task specification + mark protocol + local rules. You can test it in isolation.
- **Adaptive.** Marks decay over time, so outdated information fades and fresh marks dominate. The system tracks the world without explicit invalidation.

### Biological Foundations

| System | Mark Type | Decay Mechanism | Emergent Behavior | Scale |
|--------|-----------|-----------------|-------------------|-------|
| Ant foraging | Pheromone trail | Evaporation (minutes) | Shortest-path optimization | 10K-1M agents |
| Termite construction | Mud + pheromone | None (structural) | Complex architecture | 1M+ agents |
| Bee recruitment | Waggle dance (spatial mark) | Dancer stops | Resource allocation | 10K-50K agents |
| Slime mold | Chemical gradient | Diffusion | Network optimization | 10^9 cells |
| Immune system | Antigen presentation | Degradation | Distributed threat detection | 10^12 cells |

Common pattern: **no agent has global state.** Each agent reads local marks, applies local rules, and writes local marks. Global behavior emerges from these local interactions.

The immune system analogy applies here. It handles the adversarial problem (pathogens are actively hostile) with mostly local coordination: recognition is local (this antigen matches my receptor), response is local (activate, recruit nearby cells), memory is distributed (clonal expansion of successful responders). The system does have coordinating structures (the thymus selects T-cells, dendritic cells present antigens, cytokines create systemic signals) but there is no central authority deciding what to attack. Coordination emerges from local interactions at population scale.

Social insects have coordinated at scale through stigmergy since at least the Cretaceous ([Bonabeau et al., 1999](https://global.oup.com/academic/product/swarm-intelligence-9780195131598)), though some mechanisms (honeybee quorum sensing) do resemble voting ([Seeley, 2010](https://press.princeton.edu/books/paperback/9780691147215/honeybee-democracy)). The markspace protocol applies the same biological pattern to software agents.

> **Core property**: *when the coordination protocol enforces safety invariants structurally, agent quality becomes a performance variable rather than a safety concern.*
>
> This follows from a structural argument. Every agent action passes through a validation stack (scope check, schema check) that the agent itself cannot bypass. A compromised or low-quality agent can produce worse output, but it cannot violate the protocol's safety constraints. Quality affects the value of marks written; the protocol bounds what marks can be written at all.

## The Framework: Five Mark Types

An agent's entire coordination interface:

1. What marks it reads
2. What marks it writes
3. Its local rules for reacting to marks

If you can fully specify an agent by these three things plus its task specification, the agent is decomposable. You test it in isolation (mock the marks), deploy it independently, and coordination emerges.

### Shared types

```protobuf
enum MarkType {
  INTENT      = 0;
  ACTION      = 1;
  OBSERVATION = 2;
  WARNING     = 3;
  NEED        = 4;
}

enum Source {
  FLEET               = 0;
  EXTERNAL_VERIFIED   = 1;
  EXTERNAL_UNVERIFIED = 2;
}

enum Severity {
  INFO     = 0;
  CAUTION  = 1;
  CRITICAL = 2;
}
```

### Mark Type 1: Intent

**What**: "I am going to do X to resource R."

**Biological analog**: Ant leaving the nest on a foraging run. It hasn't found food yet, but its outbound path signals to nearby ants that this direction is being explored.

**Purpose**: Prevent conflicts between agents sharing mutable state. Agent A intends to book a Thursday flight. Agent B, about to reschedule Thursday, reads the intent and adjusts. No communication between A and B. The mark space mediates.

**Properties**:
```protobuf
message IntentMark {
  string agent_id    = 1;  // UUID, who wrote it
  string scope       = 2;  // resource domain ("calendar", "email", "finances")
  string resource_id = 3;  // specific resource ("meeting-2026-02-27-14:00")
  string action      = 4;  // planned action ("reschedule", "cancel", "book")
  float  confidence  = 5;  // [0,1] how committed (0.3 = considering, 0.9 = about to execute)
  double created_at  = 6;  // unix timestamp
  double ttl         = 7;  // seconds; evaporates if no action mark follows
}
```

**Decay**: Intent marks without a corresponding action mark evaporate after TTL. The agent died, changed its mind, or got interrupted. The mark disappears, freeing the resource for other agents. No cleanup logic needed; evaporation handles cleanup.

**Conflict rule (local)**: If an agent reads an intent mark on a resource it also intends to modify:
- If other intent confidence > own confidence: yield (delay own action).
- If equal: first-writer wins (timestamp comparison).
- If own confidence higher: proceed, but write own intent mark (other agent will see it and yield on next read).

Loosely similar to CAS (Compare-And-Swap) in concurrent programming (read state, compare, decide) but without CAS's atomicity or linearizability guarantees. The mechanism is asynchronous and soft; conflicts resolve through mark comparison rather than negotiation, and concurrent writes are possible.

**What it doesn't do**: Intent marks don't prevent all conflicts. Two agents could write intents simultaneously with high confidence. This creates a race condition resolved by the action mark: whoever writes the action first wins, the other agent reads the action mark and adapts. This behavior is by design: hard consistency requires locks (expensive, deadlock-prone). Soft consistency through marks + eventual convergence is cheaper and good enough for most agent coordination.

### Mark Type 2: Action

**What**: "I did X at time T. Result: Y."

**Biological analog**: Ant depositing food at the nest. Termite placing mud on the mound. The action changes the environment permanently.

**Purpose**: Shared knowledge of what has happened. The ground truth of the system. Other agents read action marks to update their world model without redoing work or making conflicting changes.

**Properties**:
```protobuf
message ActionMark {
  string          agent_id      = 1;  // UUID
  string          scope         = 2;
  string          resource_id   = 3;
  string          action        = 4;  // what was done
  google.protobuf.Struct result = 5;  // outcome (success/failure, details)
  double          created_at    = 6;  // unix timestamp
  optional string supersedes    = 7;  // mark UUID; if this action replaces a previous one
}
```

**Decay**: Action marks don't decay. They're historical facts. An agent booked a flight on Feb 27, and that happened. The mark persists indefinitely (or until explicitly superseded by another action mark).

**Chaining**: Action marks from one agent become the input conditions for another agent's rules. Any action mark can trigger any agent whose rules include "when I see action X on scope Y, consider doing Z."

### Mark Type 3: Observation

**What**: "I observed Y about the world. Confidence Z."

**Biological analog**: Ant detecting a predator and releasing alarm pheromone. The observation mark doesn't change the world; it shares a perception.

**Purpose**: Distribute information across the fleet without requiring agents to communicate directly. Agent A, researching Company X, discovers they announced a merger. Agent A writes an observation mark. Agent B, also researching Company X, reads the mark and incorporates it. Agent B didn't need to know Agent A exists.

**Properties**:
```protobuf
message ObservationMark {
  string                 agent_id   = 1;  // UUID
  string                 scope      = 2;
  string                 topic      = 3;  // what was observed about
  google.protobuf.Struct content    = 4;  // the observation itself
  float                  confidence = 5;  // [0,1]
  Source                 source     = 6;
  double                 created_at = 7;  // unix timestamp
  // freshness computed at read time: strength(t) = 0.5^(age / half_life)
}
```

**Decay**: Observations have a freshness half-life. E.g., an observation from 6 hours ago has half the weight of a fresh one. An observation from 3 days ago is near-zero. The world changes, so stale observations should fade rather than persist at full strength.

**Source trust**: Source trust introduces the adversarial model. Marks from fleet agents (`FLEET`) are trusted, since they're authenticated and from agents you built. Marks from external sources are categorized:
- `EXTERNAL_VERIFIED`: cross-referenced against a second source or known fact
- `EXTERNAL_UNVERIFIED`: single-source, could be adversarial

An agent's local rules weight observations by `source * freshness * confidence`. Fleet observations dominate while old external observations vanish. This mirrors the immune system's self vs non-self recognition, built into the mark protocol rather than added as a separate adversarial defense layer.

**Reinforcement**: When multiple agents independently write similar observation marks on the same topic, the effective strength increases. The mechanism is direct pheromone reinforcement: more ants on a trail produce a stronger signal, with no voting protocol needed. Convergent independent observations naturally strengthen while divergent observations naturally weaken through low reinforcement and normal decay.

### Mark Type 4: Warning

**What**: "X is no longer true" or "X failed."

**Biological analog**: Ant marking a depleted food source with repellent pheromone. Bees "stop signal" vibrating against a waggle dancer to suppress recruitment to a dangerous source.

**Purpose**: Invalidation. An agent discovers that a previously observed fact has changed, a previously successful action has been reversed, or a resource previously considered safe is now compromised.

**Properties**:
```protobuf
message WarningMark {
  string   agent_id    = 1;  // UUID
  string   scope       = 2;
  optional string invalidates = 3;  // mark UUID being contradicted
  string   reason      = 4;
  Severity severity    = 5;
  double   created_at  = 6;  // unix timestamp
  // exponential decay: strength(t) = initial * 0.5^(age / warning_half_life)
}
```

**Decay**: Warnings use the same exponential decay as observations, `0.5^(age / half_life)`, but with a shorter half-life (the spec recommends `warning_half_life <= observation_half_life`). A warning from 5 minutes ago is urgent. A warning from 2 days ago is context. The shorter half-life ensures warnings are noticed quickly but don't clutter the mark space permanently. The behavioral difference from observations is the invalidation mechanic: a warning referencing another mark reduces that mark's effective strength by the warning's current strength, suppressing it while the warning is fresh and releasing it as the warning decays.

**What it solves**: The "world changes while agent works" problem. No central invalidation service. No message broadcasting. Agent A discovers the merger announcement. Writes a `CRITICAL` warning that invalidates its own earlier observation mark ("Company X is independent"). All agents reading observation marks on Company X see the warning, reweight, adapt. Agents that have already completed work based on the invalidated observation won't retroactively change, but agents currently in progress will see the warning and adjust.

**Cascade prevention**: Warnings don't cascade automatically. Agent B reads Agent A's warning. If Agent B decides its own work is affected, Agent B writes its own warning about its own output. By design, cascade is a local decision rather than a global propagation. An agent might read a warning and decide it's irrelevant to its task. No forced invalidation.

### Mark Type 5: Need

**What**: "I need principal input on X."

**Biological analog**: Honeybee tremble dance. A forager returning to a congested hive performs a tremble dance that signals "I need more receivers." It doesn't address a specific bee. It marks a system-level need. Nearby bees respond if they can.

**Purpose**: Solve the principal-as-bottleneck problem without direct interruption.

**Properties**:
```protobuf
message NeedMark {
  string                         agent_id     = 1;  // UUID
  string                         scope        = 2;
  string                         question     = 3;  // what decision is needed
  google.protobuf.Struct         context      = 4;  // relevant information for the decision
  float                          priority     = 5;  // [0,1] urgency
  optional google.protobuf.Struct alternatives = 6; // options the agent has identified
  bool                           blocking     = 7;  // is the agent blocked waiting?
  double                         created_at   = 8;  // unix timestamp
  optional string                resolved_by  = 9;  // mark UUID of the resolving decision
  // no decay while unresolved; strength → 0 on resolution
}
```

**Decay**: Need marks don't decay while unresolved. They accumulate. A simple aggregator (not an agent, just a collector with no intelligence) batches them by priority and presents them to the principal when the principal is available.

**Resolution**: The principal responds by writing a **decision mark**, a special action mark scoped to the need. The agent polls for decision marks on its pending needs. When resolved, the need mark is closed.

**Emergent prioritization**: If 5 agents write need marks about the same topic (e.g., "should we proceed with deal X given the merger?"), the aggregator presents one consolidated need with effective priority = max(individual priorities) + density bonus. No ranking algorithm or priority negotiation required. Mark density serves as the priority signal.

**Consolidation rule (local, in the aggregator)**:
```
FOR each unresolved need mark:
  FIND other unresolved needs with overlapping scope + similar question
  IF cluster_size > 1:
    effective_priority = max(individual priorities) + log(cluster_size) * 0.1
    present as single consolidated question with all contexts
```

**What it solves**: The principal can be asleep, in a meeting, on vacation. Need marks accumulate without blocking the system. Agents that can continue without the decision do so. Agents that are blocked wait. When the principal returns, they see a prioritized batch, not a firehose of interrupts.

## Coordination Surfaces

The five mark types define what agents can say. The next question is where those marks interact and where coordination can fail. A deployed system faces several distinct situations:

- **Single agent** operating within its own validation and trust boundaries
- **Fleet of agents** under the same principal, coordinating without a central scheduler
- **Agents interacting with external content, APIs, or tools** where the environment may be adversarial (prompt injection, poisoned data, manipulated responses)
- **Principal oversight** of an agent fleet, where a human must stay informed without being overwhelmed
- **Cross-principal interaction**, where an external organization deploys its own agents into your system, or your agents consume marks from an external fleet

Each situation has distinct failure modes. The protocol addresses all five through the same environmental mechanism (marks + guard + decay) rather than requiring separate subsystems.

### Single-Agent Validation and Trust

Trust in agent output is established through defense-in-depth validation: scope-based authority ([CSA PARC model](https://cloudsecurityalliance.org/blog/2026/02/02/the-agentic-trust-framework-zero-trust-governance-for-ai-agents)), deterministic schema checks ([DO-178C](https://en.wikipedia.org/wiki/DO-178C) checkpoints), convergent redundancy ([Byzantine fault tolerance](https://doi.org/10.1145/357172.357176)), statistical anomaly detection ([Shewhart](https://en.wikipedia.org/wiki/Control_chart)/[CUSUM](https://en.wikipedia.org/wiki/CUSUM)), progressive scope restriction ([Lyapunov absorbing barriers](https://en.wikipedia.org/wiki/Lyapunov_stability)), and active probing ([NASA fault protection](https://ntrs.nasa.gov/citations/20150011958)). Each check becomes a local validation rule the agent follows.

| Check | Stigmergic Expression | Status |
|-------|----------------------|--------|
| Authority | What mark types and scopes this agent is allowed to write. Its "pheromone glands." | Implemented (guard + agent `can_write()`) |
| Validation | Schema validation on marks before writing. Malformed marks rejected locally. | Implemented (guard `_validate_mark()`) |
| Redundancy | Observation reinforcement: convergent independent observations, no voting protocol. | Implemented (`reinforce()`) |
| Statistical Envelope | Monitor this agent's mark patterns over time. Anomalous marking rate or content triggers scope restriction. | Design only |
| Absorbing Barrier | Reduce the agent's writable mark types. Narrow its scope. Increase need-mark requirements. | Design only |
| Diagnostic Probe | Inject a synthetic observation mark with known content. Verify the agent reads and reacts correctly. | Design only |

Of the three implemented checks, two are agent-level properties enforced at the mark space boundary (authority, validation) and one is a mark space aggregate (redundancy/reinforcement). Statistical envelope, absorbing barrier, and diagnostic probe are design goals not yet in the spec or implementation; they represent the adaptive and monitoring layers that a production deployment would add to the static enforcement the current system provides.

**Prompt injection resilience.** A prompt-injected agent is structurally equivalent to a low-quality agent: it may produce unexpected output, but the implemented validation stack still applies. Authority limits which mark types and scopes the agent can write. Schema validation rejects malformed marks regardless of the agent's internal state. The agent cannot disable these checks because they are enforced at the mark space boundary, not inside the agent. A production deployment adding statistical envelope and absorbing barrier would strengthen this further: the envelope would detect behavioral shifts (a sudden change in marking patterns) and the barrier would automatically narrow the agent's scope in response.

<p align="center"><img src="../figures/single_agent.svg" alt="Single agent architecture" width="700"></p>

**Redundancy** is already a mark space property rather than an agent-level one: multiple agents writing convergent observations on the same topic provides redundancy without a voting protocol. The mark space naturally aggregates independent signals via `reinforce()`.

The three unimplemented checks would operate at different levels:

- **Statistical Envelope** would monitor at the mark space boundary. The guard tracks per-agent mark patterns over time and detects anomalies. The agent carries the envelope (it's per-agent state), but enforcement happens at the guard, not inside the agent.
- **Absorbing Barrier** would modify agent-level state by narrowing an agent's writable mark types and increasing need-mark requirements. Triggered by the statistical envelope, applied to the agent's authority definition.
- **Diagnostic Probe** would be a system-level service: a lightweight monitor (not an agent, a simple process) that injects synthetic marks with known content and checks whether agents react correctly, operating through the mark space rather than through direct agent inspection.

### Intra-Fleet Coordination

**Fully stigmergic.** No coordinator, no message bus, no shared scheduler.

<p align="center"><img src="../figures/conflict_scenario.svg" alt="Conflict scenario" width="825"></p>

No negotiation, consensus, or coordinator decides who goes first. The intent mark provides the coordination. If both agents write intents simultaneously (a race condition), the action mark resolves it: whoever executes first wins, and the other adapts.

**Shared knowledge without duplication**: Agent A researches Topic X, writes observation marks. Agent B, starting research on the same topic, reads existing observations before doing its own work. If the observations are fresh and high-confidence, Agent B skips redundant work, following the same principle as ant trail optimization: don't explore paths that are already well-marked.

### Agent-Environment Interaction (Adversarial)

**Source authentication on marks.**

| Source | Weight | Condition |
|--------|--------|-----------|
| `FLEET` | 1.0 | authenticated, signed |
| `EXTERNAL_VERIFIED` | 0.7 | cross-referenced against 2+ sources |
| `EXTERNAL_UNVERIFIED` | 0.3 | single source |
| `CONTRADICTED` | weight - contradiction_weight | conflicts with higher-trust marks |

Effective weight: `trust_weight * freshness * confidence`

This handles the "hostile internet" problem. An agent scrapes a webpage that contains prompt injection. The agent writes an observation mark with `source=EXTERNAL_UNVERIFIED`. Other agents read it at 30% weight. If the observation contradicts fleet observations, the contradiction further reduces it. No separate adversarial detection framework. Trust levels are properties of marks, enforced by local rules.

The trust model is deliberately simple: three fixed levels with static weights. The computational trust literature offers far more sophisticated approaches. [Marsh (1994)](https://doi.org/10.13140/RG.2.1.1047.0961) formalized trust as a continuous value derived from experience, [Ramchurn et al. (2004)](https://doi.org/10.1017/S0269888904000116) developed trust and reputation mechanisms for multi-agent systems with dynamic updating, and [Burnett et al. (2010)](https://doi.org/10.1007/978-3-642-14059-7_4) applied trust models specifically to stigmergic environments. We avoid dynamic trust because the primary failure mode we target is LLM non-compliance, i.e. agents that ignore instructions, hallucinate actions, or drift from their intended behavior. Static source-based weighting (fleet > verified external > unverified external) is sufficient for this threat model, and its simplicity makes it auditable. This is a deliberate scope limitation, not a claim that LLM agents can't be strategic. Recent work on [scheming in frontier models](https://arxiv.org/abs/2311.07590) (Scheurer et al., 2024) and [sleeper agents](https://arxiv.org/abs/2401.05566) (Hubinger et al., 2024) demonstrates that LLMs can exhibit goal-directed deceptive behavior, including performing well during perceived evaluation while pursuing different objectives when "unmonitored." An agentic LLM with access to its trust system's rules via prompt context could in principle reason about gaming those rules. A deployment facing this threat model (adversarial agents within the fleet, not just broken ones) would need the dynamic trust models from Ramchurn et al. and Burnett et al., plus behavioral anomaly detection at the guard layer.

The immune system parallel is instructive. T-cells don't have a "threat detection committee." Each cell has receptors. If the presented antigen matches, the cell activates. If it doesn't, the cell ignores it. False positives (autoimmunity) and false negatives (immune evasion) exist, but the system works at population scale because no single cell's error is catastrophic. Same principle here: no single agent's bad observation mark is catastrophic because every other agent applies its own trust weighting.

### Principal-Fleet Interface

**Need marks, decision marks, and a simple aggregator.**

<p align="center"><img src="../figures/principal_attention.svg" alt="Principal attention model" width="812"></p>

The principal never directly messages an agent. The aggregator has no AI; it sorts, groups, and displays. Intelligence is in the agents (writing good need marks with context) and the principal (making decisions).

**Batch efficiency**: A principal responding to 5 grouped needs in one session is more efficient than being interrupted 5 times. The mark model naturally batches because needs accumulate during principal absence and are presented together.

### Cross-Principal Interaction

The adversarial surface (above) handles external data from web scraping and APIs; your agents tag what they find. This surface handles external fleets, where another principal's agents share marks with yours.

Each fleet has its own mark space. You cannot authenticate another fleet's marks. Only observations cross the boundary. Intent, action, and need marks are fleet-internal; they coordinate your resources and your principal's attention.

<p align="center"><img src="../figures/cross_principal.svg" alt="Cross-principal boundary" width="700"></p>

At this boundary, and only at this boundary, consensus mechanisms apply:

- **All incoming marks tagged `EXTERNAL_UNVERIFIED`** regardless of their source status in the originating fleet
- **Median aggregation** of claims from multiple external agents (robust below honest majority)
- **Peer-Agreement Drift (PAD) detection** to identify consistently deviating external agents
- **Per-item metrics** over distributional ones (resists adaptive evasion)
- **Cross-referencing** against fleet observations can upgrade to `EXTERNAL_VERIFIED`

Stigmergy handles coordination within a fleet. Consensus applies at exactly one boundary: between fleets, where you cannot trust the other party's marks.

## Mark Space Architecture

### Storage

The mark space is a shared data store. Three implementation options, from simplest to recommended:

#### Option A: Database table

Simple, durable, queryable. A single PostgreSQL table handles all mark types.

```sql
CREATE TABLE mark (
    id             UUID PRIMARY KEY,
    mark_type      TEXT NOT NULL,   -- 'intent', 'action', 'observation', 'warning', 'need'
    agent_id       UUID NOT NULL,
    scope          TEXT NOT NULL,
    resource_id    TEXT,
    content        JSONB NOT NULL,
    source         TEXT NOT NULL,   -- 'fleet', 'external_verified', 'external_unverified'
    confidence     FLOAT,
    strength       FLOAT NOT NULL DEFAULT 1.0,
    created_at     FLOAT NOT NULL,  -- unixtime
    ttl_seconds    INTEGER,         -- NULL = no expiry
    supersedes     UUID REFERENCES mark(id),
    invalidated_by UUID REFERENCES mark(id),
    resolved_by    UUID REFERENCES mark(id)
);

CREATE INDEX idx_mark_scope    ON mark(scope, mark_type, created_at DESC);
CREATE INDEX idx_mark_agent    ON mark(agent_id, created_at DESC);
CREATE INDEX idx_mark_resource ON mark(resource_id, mark_type) WHERE resource_id IS NOT NULL;
```

#### Option A': Document database

Marks are document-shaped: varied schemas per type, nested content, queried by scope and time. A document store (MongoDB, DynamoDB) is a natural alternative to PostgreSQL. Scope as partition key, `created_at` as sort key, TTL as a native expiry feature. Advantages over relational: no schema migration when mark types evolve, native document queries on `content` fields. Disadvantage: weaker transactional guarantees for the guard's atomic conflict checks (though scope-partitioned guards sidestep this). Replaces Option A in the hybrid (Option C) if chosen.

#### Option B: Redis streams

Fast, ephemeral, pub/sub capable. Mark types as separate streams keyed by scope:

```
mark:intent:{scope}
mark:action:{scope}
mark:observation:{scope}
mark:warning:{scope}
mark:need:{agent_id}
```

Agents subscribe to scope-relevant streams. TTL handled by Redis expiry. No durability for transient marks (intent, observation). Action marks mirrored to a database for persistence.

#### Option C: Hybrid (recommended)

| Layer | Handles | Store |
|-------|---------|-------|
| Real-time reads/writes | All marks | Redis |
| Durable record | Actions, decisions | PostgreSQL |
| Audit trail | All marks | Background sync: Redis → PostgreSQL |
| Recovery | Rebuild on restart | PostgreSQL → Redis |

Agents always read from Redis (low latency). The hybrid approach is standard: Redis for queues (transient state), PostgreSQL for records (durable state). Marks are a data type that spans both.

### Open Questions

**Unbounded mark growth.** Action marks are permanent (P2). In a long-running system, the mark space grows without bound. The spec recommends garbage collection for transient marks below a strength threshold (0.01), but action marks have no such mechanism. A production implementation needs a compaction or archival strategy for action marks, e.g. moving marks older than a configurable horizon to cold storage while preserving their supersession chains. The reference implementation does not address this because the stress test ran for 10 simulated rounds (one work week), not months.

**Distributed storage.** The spec assumes a single-node mark space with in-process locking. A distributed deployment (multiple mark space nodes) would need to maintain the guard's atomicity guarantees across nodes. The CALM theorem (see [Theoretical Grounding](#theoretical-grounding)) suggests this is feasible for monotonic read/write operations, but the guard's lock-based conflict check is not monotonic. It requires reading the current state and making a decision atomically. A distributed guard would likely need a [Calvin](https://doi.org/10.1145/2213836.2213838)-style deterministic ordering layer (Thomson et al., 2012) or partitioning by scope (each scope assigned to exactly one node).

**Deferred resolution.** Formalized in [spec.md Section 6.2](spec.md). The deferred resolution pattern (collect intents, resolve at batch boundary) is used in the stress test for parking and boardroom resources. It is the mechanism that makes HIGHEST_CONFIDENCE meaningful under lock-based serialization ([Section 6.3 of the stress test analysis](../experiments/stress_test/analysis.md#63-lock-based-guards-break-highest_confidence)). The spec defines three phases (claim collection, resolution boundary, batch resolution), three properties (P30-P32), and the relationship between deferred and immediate guard modes. Remaining open question: the resolution boundary trigger is "deployment-defined," so a production implementation would need to decide between timer-based, event-based, or principal-triggered boundaries.

**Need clustering for principal review.** The spec's [`aggregate_needs()` (Section 8.4)](spec.md#84-aggregate-needs) says needs with "similar questions" are clustered, but the similarity function is "implementation-defined." This is an intentional design boundary; the protocol delegates clustering semantics to the deployment rather than prescribing a single approach. When 12 agents write need marks about "Deal X," grouping them into one principal decision item is obvious. When 3 agents write needs about "vacation policy," "PTO balance," and "time-off request," whether those are one cluster or three separate items depends on semantic similarity, not string matching. Options: (1) scope-based grouping (all needs in the same scope cluster together, simple but misses cross-scope relationships), (2) embedding-based semantic similarity (compute embeddings of need `question` fields, cluster by cosine similarity above a threshold, accurate but adds an ML dependency to what should be a "zero intelligence" aggregator), (3) LLM-based clustering (ask an LLM to group needs, which defeats the purpose of keeping intelligence out of infrastructure). The right answer is probably scope-based grouping as the default with an optional semantic similarity layer for deployments that need it. The aggregator's design principle is "zero intelligence": it sorts, groups, and displays. Adding semantic clustering would violate that principle unless it's a clearly separated preprocessing step.

### Decay Functions

```python
def compute_strength(
    mark: AnyMark, now: float, decay_config: DecayConfig
) -> float:
    """Compute a mark's strength at time `now`. Pure function."""
    age = now - mark.created_at

    if mark.mark_type == MarkType.ACTION:
        # P2: Actions don't decay (they're facts)
        return mark.initial_strength

    if mark.mark_type == MarkType.INTENT:
        # P4: Hard cutoff at TTL
        if age > decay_config.intent_ttl:
            return 0.0
        return mark.initial_strength

    if mark.mark_type == MarkType.OBSERVATION:
        # P1: Exponential decay
        half_life = decay_config.observation_half_life
        return mark.initial_strength * (0.5 ** (age / half_life))

    if mark.mark_type == MarkType.WARNING:
        # P1: Exponential decay with warning-specific half-life
        half_life = decay_config.warning_half_life
        return mark.initial_strength * (0.5 ** (age / half_life))

    if mark.mark_type == MarkType.NEED:
        # P5: Full strength while unresolved, 0 when resolved
        assert isinstance(mark, Need)
        if mark.resolved_by is not None:
            return 0.0
        return mark.initial_strength

    raise ValueError(f"Unknown mark type: {mark.mark_type}")
```

Half-lives are configurable per scope. Financial observations decay faster than research observations. Calendar intents have shorter TTLs than project-level intents. The decay parameters are part of the scope definition, not the framework.

The decay model is informed by the [Age of Information](https://doi.org/10.1109/ISIT.2012.6283535) (AoI) literature (Kaul et al., 2012). AoI measures the freshness of information at a receiver as a function of time since the last update. Our exponential decay `strength(t) = initial * 0.5^(age / half_life)` is a monotonically decreasing freshness function consistent with AoI's insight that stale information should be discounted. (AoI theory primarily addresses optimal update *frequency* rather than decay curve shape; the exponential form is a design choice, not a theoretical optimum.) The half-life parameterization provides a natural knob: shorter half-lives for volatile state (warnings), longer for slower-changing state (observations).

### Reinforcement

When multiple agents write similar marks:

```python
REINFORCEMENT_FACTOR: float = 0.3
REINFORCEMENT_CAP: float = 2.0


def reinforce(strengths: list[float]) -> float:
    """
    Combine multiple mark strengths on the same scope+topic.
    Sublinear and bounded.

    P8:  aggregate < N * max_single for N > 1
    P9:  aggregate <= REINFORCEMENT_CAP
    P10: adding a positive strength cannot decrease aggregate
    """
    active = sorted([s for s in strengths if s > 0.0], reverse=True)
    if not active:
        return 0.0
    result = active[0]
    for s in active[1:]:
        result += s * REINFORCEMENT_FACTOR
    return min(result, REINFORCEMENT_CAP)
```

The reinforcement function is sublinear. Two observations are stronger than one, but ten observations aren't ten times stronger. This prevents mark flooding attacks (an adversary writing many weak marks to overwhelm legitimate ones) while still rewarding genuine convergence.

## Scope and Decomposability

### Scope Definition Language

Each scope defines:

```yaml
scope: "calendar"
  mark_permissions:
    intent:      [book, reschedule, cancel]
    action:      [booked, rescheduled, cancelled]
    observation: [availability_check, conflict_detected]
    warning:     [booking_failed, external_change]
    need:        [approval_required, conflict_resolution]
  decay:
    observation_half_life: 1h    # calendar state changes fast
    intent_ttl: 30m              # if you don't act in 30 min, intent expires
    warning_half_life: 4h
  conflict_resolution: first_writer_wins

scope: "research/company/{company_id}"
  mark_permissions:
    intent:      [investigate, deep_dive, summarize]
    action:      [investigation_complete, report_generated]
    observation: [financial_data, news_event, market_signal]
    warning:     [data_stale, source_unreliable, material_change]
    need:        [direction_needed, budget_approval]
  decay:
    observation_half_life: 12h   # research holds longer than calendar
    intent_ttl: 4h
    warning_half_life: 6h
  conflict_resolution: merge     # multiple agents can research same company
```

Scopes are hierarchical. An agent authorized for `research/company/*` can write marks on any company. An agent authorized for `research/company/ACME` can only write marks about ACME. Authority is scope-bounded, and scope is defined independently of the agents.

### Why This Decomposes

The core claim is that each component can be designed, tested, and validated independently.

| Component | Test in isolation | Depends on |
|---|---|---|
| Mark schema | Validate marks without agents | Nothing |
| Single agent | Mock marks, verify read/write/react | Mark schema |
| Decay functions | Unit test with synthetic timestamps | Nothing |
| Reinforcement | Unit test with synthetic marks | Decay functions |
| Conflict resolution | Two agents, one shared resource | Mark schema |
| Trust weighting | Synthetic marks with varied sources | Mark schema |
| Principal interface | Aggregator + synthetic need marks | Mark schema |
| Cross-principal | Boundary protocol + external marks | Trust weighting |
| Fleet coordination | N agents, shared mark space | All above |

Each row can be developed and tested without the rows below it. Fleet coordination is the last thing you build, and it emerges from the components above rather than requiring a separate mechanism.

In the committee model, individual algorithms (voting, aggregation) can be unit-tested with mock inputs, but their *interaction* under realistic conditions requires the full multi-agent setup. Testing whether a panel of agents reaches the right consensus requires the panel. Stigmergy decomposes more cleanly because the mark space is the only shared interface, and each component's contract is defined entirely by the marks it reads and writes.


## Evaluation

### Structural Prevention of Known Failure Modes

The [Agents of Chaos](https://agentsofchaos.baulab.info/report.html) case studies do not present a formal failure taxonomy; they provide existence proofs of what goes wrong. Drawing from their observations, we identify five coordination-layer failure categories that this protocol structurally prevents:

| Failure pattern | Protocol mitigation | Tested? |
|---|---|---|
| Unauthorized access / scope violations | Scope permissions, guard rejects unauthorized writes | Yes (0 violations) |
| Cascading state corruption | Intent TTL (2h) prevents stale signal accumulation; decay degrades observations | Yes (intents expired between every round pair) |
| Unconstrained goal pursuit | Fixed manifests per agent, constrained tool space | Yes (agents completed manifests, didn't improvise) |
| Cross-agent propagation | No direct agent-to-agent communication; marks only | Yes (all coordination through mark space) |
| Resource exhaustion | Guard serialization + conflict resolution policies | Yes (0 double bookings, 3 policies all functional) |

**What this doesn't cover.** The report documents failure modes that operate below the coordination layer. Sensitive data disclosure (an agent leaking SSNs in email summaries): this protocol controls mark visibility, not content within agent outputs. Agent corruption via prompt injection (CS10): the protocol constrains what marks agents write, not how agents reason internally. Disproportionate response (CS1, an agent deleting email infrastructure to protect a secret): a protocol-valid action can still be harmful. Identity spoofing (CS8): the Source/trust system provides identity at the mark level, but if an agent is compromised at a lower level, the protocol can't help.

The protocol is a coordination primitive, not a complete agent safety system. It guarantees that whatever agents do, the result is consistent. Making agents do safe things *within* their authorized scope requires agent-level safeguards (content filtering, reasoning verification, action proportionality checks) that sit alongside the protocol, not inside it.

### Coverage Against Intelligent Delegation

[Intelligent Delegation](https://arxiv.org/html/2602.11865v1) frames the broader challenge of safe delegation and identifies nine technical components (trust management, permissions and access control, monitoring, adaptive coordination, verifiable completion, security, delegation chains, accountability, and human oversight). Markspace is a coordination protocol, not a delegation framework, but it structurally satisfies five of the nine through environment design. The remaining four require agent-internal mechanisms at a different architectural level.

| Component | MarkSpace Coverage | Notes |
|---|---|---|
| 1. Trust management | Partial | Three static source levels (fleet, verified, unverified). No dynamic trust updating, reputation, or experience-based trust. Sufficient for LLM non-compliance; insufficient for rational adversaries. |
| 2. Permissions and access control | Yes | Scope-based write authorization, hierarchical scope inheritance, three visibility levels (OPEN/PROTECTED/CLASSIFIED). Guard enforces mechanically. |
| 3. Monitoring and observability | Partial | Action marks provide an audit trail. Guard decisions are logged. No agent-internal monitoring (reasoning traces, prompt compliance). |
| 4. Adaptive coordination | Partial | Decay and reinforcement adapt signal strength over time. Conflict resolution adapts to mark state. No runtime adaptation of coordination strategy itself (e.g., switching conflict policies based on load). |
| 5. Verifiable completion | Partial | Action marks record outcomes. Supersession chains track state transitions. No verification that the action's result is correct (the protocol records what happened, not whether it was right). |
| 6. Security | Yes | Guard enforces authorization independent of LLM compliance. Scope visibility prevents information leakage. Trust weighting attenuates untrusted sources. Adversarial robustness validated (171 denied attempts). |
| 7. Delegation chains | No | MarkSpace coordinates peer agents. It does not model agent-to-agent delegation, sub-task assignment, or hierarchical authority beyond the principal-fleet boundary. |
| 8. Accountability | Partial | Every mark has an `agent_id`. Actions are attributed. But there is no mechanism for post-hoc blame assignment, audit queries, or compliance reporting. |
| 9. Human oversight | Yes | Need marks + `aggregate_needs()` + principal resolution. YIELD_ALL policy escalates contested resources to the principal. The principal interface is the protocol's primary human-in-the-loop mechanism. |

The gaps (delegation chains, dynamic trust, agent-internal monitoring, verifiable correctness) are real but intentional. MarkSpace is a coordination primitive, not a complete delegation framework. The components it covers are the ones that can be enforced structurally through environment design. The components it omits require agent-internal mechanisms that operate at a different architectural level.

### Theoretical Grounding

The [CALM theorem](https://doi.org/10.1145/3369736) (Hellerstein & Alvaro, 2020) proves that monotonic programs (those that only accumulate information, never retract it) can achieve eventual consistency without coordination. Mark read/write operations are monotonic: marks are written (accumulated) and decay (weaken) but are never deleted or mutated in storage. Strength is computed at read time as a pure function. However, the guard's conflict resolution is not purely monotonic; it reads current state and makes an atomic decision (see [Open Questions](#open-questions)). The protocol minimizes the coordination surface rather than eliminating it entirely: monotonic operations proceed without synchronization, while the guard serializes only the conflict-check step.


## Related Work

### Computational Precedents

Stigmergy isn't new to computer science, though it has seen limited adoption for LLM agent coordination specifically.

**Tuple spaces** ([Gelernter, 1985](https://doi.org/10.1145/2363.2433)): the Linda coordination language. Processes communicate by writing tuples to a shared space and pattern-matching to read them. No process addresses another process directly. Coordination through shared state, and a direct computational analogue of stigmergy.

**Blackboard systems** ([Hayes-Roth, 1985](https://doi.org/10.1016/0004-3702(85)90016-6)): AI architecture where multiple knowledge sources write to a shared workspace. Each knowledge source monitors the blackboard for patterns that trigger its rules. No knowledge source communicates with any other. The blackboard itself provides the coordination. The [HEARSAY-II](https://doi.org/10.1016/0004-3702(80)90004-5) speech understanding system (Erman et al., 1980) is the canonical implementation: multiple knowledge sources (acoustic, phonetic, lexical, syntactic) coordinate exclusively through a shared blackboard, with no knowledge source aware of any other's existence. [Nii (1986)](https://doi.org/10.1609/aimag.v7i2.537) formalized the pattern. MarkSpace's typed marks, scoped namespaces, and strength-based reading are a direct descendant of this lineage, adapted for LLM agents.

**Hoare monitors** ([Hoare, 1974](https://doi.org/10.1145/355620.361161)): the guard layer ([Section 8b of the spec](spec.md#8b-guard-deterministic-enforcement-layer)) is a descendant of Hoare's monitor concept. A monitor wraps shared state with procedures that enforce mutual exclusion and preconditions. The guard wraps the mark space: every tool call passes through `pre_action` (precondition check) before execution, with a lock ensuring atomicity. The difference is that Hoare monitors enforce programmer-specified invariants, while the guard enforces protocol-specified invariants that the LLM agent cannot override.

**Bell-LaPadula model** ([Bell & LaPadula, 1973](https://apps.dtic.mil/sti/citations/AD0770768)): the OPEN/PROTECTED/CLASSIFIED visibility levels are a simplified Bell-LaPadula lattice. Bell-LaPadula's "no read up, no write down" becomes our "unauthorized readers see projections (PROTECTED) or nothing (CLASSIFIED)." The simplification is deliberate: Bell-LaPadula handles arbitrary lattices of security levels; we need exactly three levels because the use case is coordination visibility, not military classification. The projected read (structure visible, content redacted) is the novel element. Bell-LaPadula has no analogue for "you can see that a mark exists but not what it says."

**Environment as first-class abstraction** ([Weyns et al., 2007](https://doi.org/10.1007/s10458-006-5012-0)): Weyns, Omicini, and Viroli argued that multi-agent system environments should be first-class architectural elements with their own responsibilities, not just passive containers. The mark space follows this prescription: it computes strength at read time, enforces visibility, and mediates all agent interaction. Agents do not interact with each other; they interact with the environment.

**[Kubernetes reconciliation](https://kubernetes.io/docs/concepts/architecture/controller/)**: controllers don't talk to each other. They read desired state from etcd, compare to actual state, write corrections. The "mark space" is the Kubernetes API server. Adding a new controller changes nothing for existing controllers.

**CRDTs** ([Shapiro et al., 2011](https://doi.org/10.1007/978-3-642-24550-3_29)): Conflict-free Replicated Data Types guarantee eventual consistency without coordination. Multiple writers, no locks, convergent state. The data structure's merge semantics serve as the coordination protocol. Relevant for mark conflict resolution.

**Digital pheromones** ([Parunak, 2006](https://doi.org/10.1007/11678809_10)): explicit application of biological pheromone models to multi-agent systems. Agents deposit digital pheromones on a spatial grid, pheromones evaporate over time, agents follow gradients. Demonstrated for vehicle routing, task allocation, perimeter defense.

### LLM Multi-Agent Coordination (2024-2026)

Recent work has applied coordination protocols to LLM agent fleets, though most rely on direct messaging or centralized orchestration rather than environment-based coordination.

**Stigmergic Blackboard Protocol** ([AdviceNXT/sbp](https://github.com/AdviceNXT/sbp)): closest existing project. Agents deposit digital pheromones with intensity decay, sense the environment, and respond when conditions are met. Shares the core insight (coordinate through environment, not messages). Does not include typed mark taxonomy, formal properties, trust weighting, or deterministic guard enforcement.

**rescrv/markspace** ([rescrv/markspace](https://github.com/rescrv/markspace)): operational framework inspired by termite mound construction and ant foraging, aimed at CEO-scale orchestration. Focuses on workflow execution rather than formal coordination semantics.

**KeepALifeUS/autonomous-agents** ([KeepALifeUS/autonomous-agents](https://github.com/KeepALifeUS/autonomous-agents)): 4 Claude agents coordinating via file-based markspace (queue.json, active.json). Demonstrates the pattern works in practice; the project reports reduced token usage relative to direct agent communication. Uses files on disk as the mark space without decay or trust mechanics.

**LLM-Coordination** ([eric-ai-lab/llm_coordination](https://github.com/eric-ai-lab/llm_coordination), NAACL 2025): benchmark evaluating LLM coordination in pure coordination games. Introduces a Cognitive Architecture for Coordination (CAC) with plug-and-play LLM modules. Evaluates coordination ability but doesn't propose a coordination protocol. Direct agent interaction, not stigmergic.

**Langroid** ([langroid/langroid](https://github.com/langroid/langroid)): multi-agent LLM framework using direct message-passing between agents. Representative of the dominant paradigm where agents coordinate by talking to each other. In the general case, direct message-passing scales O(N²) in communication and requires agent addressing.

**CrewAI** ([crewai](https://github.com/crewAIInc/crewAI)): role-based multi-agent orchestration. Agents have roles, goals, and backstories; coordination is through a sequential or hierarchical process definition. The orchestrator decides execution order. CrewAI offers memory features, but coordination is primarily orchestrator-driven rather than environment-based.

**AutoGen** ([microsoft/autogen](https://github.com/microsoft/autogen)): Microsoft's multi-agent conversation framework. Agents coordinate through direct message exchanges in group chats. A GroupChatManager routes messages between agents. Coordination reliability depends in part on prompt compliance (agents must follow conversation conventions). AutoGen includes code execution sandboxing and tool-use constraints, but does not provide a coordination-level enforcement layer independent of LLM behavior.

**LangGraph** ([langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)): graph-based agent orchestration from LangChain. Coordination is defined as a state machine with explicit edges between nodes. Deterministic control flow; supports dynamic routing and conditional edges, but the graph topology must be defined upfront. Adding a new agent requires modifying the graph definition.

**AgentSpec** ([Wang et al., ICSE 2026](https://arxiv.org/abs/2503.18666)): domain-specific language for specifying and enforcing runtime constraints on LLM agents. Closest to our guard layer in spirit: both aim to make safety properties independent of LLM compliance. AgentSpec specifies what tools an agent may call and under what conditions; the guard layer specifies what mark-space operations are permitted and enforces conflict resolution. AgentSpec operates at the individual agent level; MarkSpace operates at the fleet coordination level.

**MARTI** ([TsinghuaC3I/MARTI](https://github.com/TsinghuaC3I/MARTI), ICLR 2026): multi-agent reinforced training and inference with tree search. Focuses on training agents to coordinate, not on runtime coordination protocols. Different problem (how to learn coordination vs how to enforce it).

**dfki-asr/markspace-demo** ([dfki-asr/markspace-demo](https://github.com/dfki-asr/markspace-demo)): stigmergic coordination in cyber-physical production scenarios. Not LLM-related but demonstrates the pattern in industrial multi-agent settings.

**Gap this work addresses.** Among the projects surveyed above, none combines (1) a typed mark taxonomy with formal decay/trust semantics, (2) a deterministic enforcement layer independent of LLM compliance, (3) systematic experimental validation across adversarial, concurrent, and multi-resource conditions, and (4) a formal specification with property tests covering normative guarantees.


## References

### Multi-agent coordination and failure analysis

- [Agents of Chaos](https://agentsofchaos.baulab.info/report.html). Bau Lab, Feb 2026. Red-teaming study of 6 autonomous agents documenting 16 failure case studies.
- Kim, Y. et al. (2025). "Towards a Science of Scaling Agent Systems: When and Why Agent Systems Work." [arXiv:2512.08296](https://arxiv.org/abs/2512.08296). [180 configurations, up to 4 agents, 4 benchmarks. Architecture-task alignment principle.]
- Tomasev, N., Franklin, M., & Osindero, S. (2026). "Intelligent AI Delegation." [arXiv:2602.11865](https://arxiv.org/abs/2602.11865). [5 pillars, 9 technical components for safe agent delegation in open markets.]
- Cemri, M., Pan, Y., Yang, Y. et al. (2025). "Why Do Multi-Agent LLM Systems Fail?" [arXiv:2503.13657](https://arxiv.org/abs/2503.13657). [MAST taxonomy: 14 failure modes, 1,600+ annotated traces across 7 MAS frameworks.]

### Adversarial robustness in multi-agent systems

- Bui, T. et al. (2025). "Adversarial Machine Learning Attacks and Defences in Multi-Agent Reinforcement Learning." *ACM Computing Surveys*. [doi:10.1145/3708320](https://doi.org/10.1145/3708320).
- Piet, J. et al. (2025). "From Prompt Injections to Protocol Exploits." *Computers & Security*. [doi:10.1016/j.cose.2025.104467](https://doi.org/10.1016/j.cose.2025.104467).

### Biology

- Grasse, P.P. (1959). "La reconstruction du nid et les coordinations interindividuelles chez Bellicositermes natalensis et Cubitermes sp." *Insectes Sociaux*, 6(1), 41-80. [doi:10.1007/BF02223791](https://doi.org/10.1007/BF02223791). [Original stigmergy paper.]
- Deneubourg, J.L., Aron, S., Goss, S., & Pasteels, J.M. (1990). "The self-organizing exploratory pattern of the Argentine ant." *Journal of Insect Behavior*, 3(2), 159-168. [doi:10.1007/BF01417909](https://doi.org/10.1007/BF01417909). [Pheromone trail dynamics, exponential decay.]
- Bonabeau, E., Dorigo, M., & Theraulaz, G. (1999). [*Swarm Intelligence: From Natural to Artificial Systems*](https://global.oup.com/academic/product/swarm-intelligence-9780195131598). Oxford University Press. [Comprehensive stigmergy treatment.]
- Theraulaz, G. & Bonabeau, E. (1999). "A brief history of stigmergy." *Artificial Life*, 5(2), 97-116. [doi:10.1162/106454699568700](https://doi.org/10.1162/106454699568700). [Historical review and formalization.]
- Seeley, T.D. (2010). [*Honeybee Democracy*](https://press.princeton.edu/books/paperback/9780691147215/honeybee-democracy). Princeton University Press. [Waggle dance, tremble dance, stop signal: all stigmergic coordination.]
- Detrain, C. & Deneubourg, J.L. (2006). "Self-organized structures in a superorganism: do ants behave like molecules?" *Physics of Life Reviews*, 3(3), 162-187. [doi:10.1016/j.plrev.2006.07.001](https://doi.org/10.1016/j.plrev.2006.07.001). [Statistical mechanics of ant coordination.]

### Computer Science

- Erman, L.D., Hayes-Roth, F., Lesser, V.R., & Reddy, D.R. (1980). "The Hearsay-II Speech-Understanding System: Integrating Knowledge to Resolve Uncertainty." *ACM Computing Surveys*, 12(2), 213-253. [doi:10.1016/0004-3702(80)90004-5](https://doi.org/10.1016/0004-3702(80)90004-5). [HEARSAY-II: canonical blackboard system.]
- Nii, H.P. (1986). "Blackboard Systems: The Blackboard Model of Problem Solving and the Evolution of Blackboard Architectures." *AI Magazine*, 7(2), 38-53. [doi:10.1609/aimag.v7i2.537](https://doi.org/10.1609/aimag.v7i2.537). [Formalization of the blackboard pattern.]
- Gelernter, D. (1985). "Generative communication in Linda." *ACM TOPLAS*, 7(1), 80-112. [doi:10.1145/2363.2433](https://doi.org/10.1145/2363.2433). [Tuple spaces: computational stigmergy.]
- Hayes-Roth, B. (1985). "A blackboard architecture for control." *Artificial Intelligence*, 26(3), 251-321. [doi:10.1016/0004-3702(85)90016-6](https://doi.org/10.1016/0004-3702(85)90016-6). [Blackboard systems: shared workspace coordination.]
- Hoare, C.A.R. (1974). "Monitors: An Operating System Structuring Concept." *Communications of the ACM*, 17(10), 549-557. [doi:10.1145/355620.361161](https://doi.org/10.1145/355620.361161). [Monitors: mutual exclusion + preconditions on shared state. Ancestor of the guard layer.]
- Bell, D.E. & LaPadula, L.J. (1973). "Secure Computer Systems: Mathematical Foundations." MITRE Technical Report MTR-2547. [DTIC:AD0770768](https://apps.dtic.mil/sti/citations/AD0770768). [Bell-LaPadula model: mandatory access control lattice. Ancestor of OPEN/PROTECTED/CLASSIFIED visibility.]
- Dorigo, M., Maniezzo, V., & Colorni, A. (1996). "Ant System: Optimization by a colony of cooperating agents." *IEEE Transactions on Systems, Man, and Cybernetics*, 26(1), 29-41. [doi:10.1109/3477.484436](https://doi.org/10.1109/3477.484436). [Ant Colony Optimization.]
- Parunak, H.V.D. (2006). "A survey of environments and mechanisms for human-human stigmergy." *Environments for Multi-Agent Systems II*, LNAI 3830, 163-186. [doi:10.1007/11678809_10](https://doi.org/10.1007/11678809_10). [Digital pheromones.]
- Shapiro, M., Preguica, N., Baquero, C., & Zawirski, M. (2011). "Conflict-free replicated data types." *SSS 2011*, LNCS 6976, 386-400. [doi:10.1007/978-3-642-24550-3_29](https://doi.org/10.1007/978-3-642-24550-3_29). [CRDTs: convergent shared state without coordination.]
- Heylighen, F. (2016). "Stigmergy as a universal coordination mechanism." *Cognitive Systems Research*, 38, 4-13. [doi:10.1016/j.cogsys.2015.12.002](https://doi.org/10.1016/j.cogsys.2015.12.002). [Stigmergy beyond biology: Wikipedia, open source, markets.]
- Marsh, L. & Onof, C. (2008). "Stigmergic epistemology, stigmergic cognition." *Cognitive Systems Research*, 9(1-2), 136-149. [doi:10.1016/j.cogsys.2007.06.009](https://doi.org/10.1016/j.cogsys.2007.06.009). [Theoretical foundations.]

### Trust and reputation

- Marsh, S.P. (1994). "Formalising Trust as a Computational Concept." PhD Thesis, University of Stirling. [doi:10.13140/RG.2.1.1047.0961](https://doi.org/10.13140/RG.2.1.1047.0961). [First formalization of computational trust.]
- Ramchurn, S.D., Huynh, D., & Jennings, N.R. (2004). "Trust in multi-agent systems." *The Knowledge Engineering Review*, 19(1), 1-25. [doi:10.1017/S0269888904000116](https://doi.org/10.1017/S0269888904000116). [Trust and reputation mechanisms for MAS.]
- Burnett, C., Norman, T.J., & Sycara, K. (2010). "Bootstrapping Trust Evaluations Through Stereotypes." *AAMAS 2010*, LNAI 6057, 241-255. [doi:10.1007/978-3-642-14059-7_4](https://doi.org/10.1007/978-3-642-14059-7_4). [Trust in stigmergic environments.]

### Multi-agent environments

- Weyns, D., Omicini, A., & Odell, J. (2007). "Environment as a first class abstraction in multiagent systems." *Autonomous Agents and Multi-Agent Systems*, 14(1), 5-30. [doi:10.1007/s10458-006-5012-0](https://doi.org/10.1007/s10458-006-5012-0). [Environment as active architectural element, not passive container.]

### Information theory and decay

- Kaul, S., Yates, R., & Gruteser, M. (2012). "Real-time status: How often should one update?" *IEEE ISIT 2012*. [doi:10.1109/ISIT.2012.6283535](https://doi.org/10.1109/ISIT.2012.6283535). [Age of Information: freshness functions for status-update systems.]
- Acemoglu, D., Makhdoumi, A., Malekian, A., & Ozdaglar, A. (2018). "Informational Braess' Paradox: The Effect of Information on Traffic Congestion." *American Economic Review*, 108(6), 1578-1617. [doi:10.1257/aer.20171898](https://doi.org/10.1257/aer.20171898). [More information can increase congestion in games with shared resources.]

### Concurrency and priority

- Kung, H.T. & Robinson, J.T. (1981). "On Optimistic Methods for Concurrency Control." *ACM TODS*, 6(2), 213-226. [doi:10.1145/319566.319567](https://doi.org/10.1145/319566.319567).
- Jones, M.B. (1997). ["What Really Happened on Mars?"](https://www.cs.cornell.edu/courses/cs614/1999sp/papers/pathfinder.html) (Mars Pathfinder priority inversion). Risks Digest.
- Thomson, A., Diamond, T., Weng, S.-C., et al. (2012). "Calvin: Fast Distributed Transactions for Partitioned Database Systems." *SIGMOD 2012*. [doi:10.1145/2213836.2213838](https://doi.org/10.1145/2213836.2213838). [Deterministic ordering for distributed transactions.]

### Consistency and distributed systems

- Hellerstein, J.M. & Alvaro, P. (2020). "Keeping CALM: When Distributed Consistency is Easy." *Communications of the ACM*, 63(9), 72-81. [doi:10.1145/3369736](https://doi.org/10.1145/3369736). [CALM theorem: monotonic programs don't need coordination.]
- Lamport, L., Shostak, R., & Pease, M. (1982). "The Byzantine Generals Problem." *ACM TOPLAS*, 4(3), 382-401. [doi:10.1145/357172.357176](https://doi.org/10.1145/357172.357176).
- Hampel, F.R. (1971). "A general qualitative definition of robustness." *Annals of Mathematical Statistics*, 42(6), 1887-1896. [doi:10.1214/aoms/1177693054](https://doi.org/10.1214/aoms/1177693054).

### Governance and safety

- Tomasev, N. et al. (2025). "Practices for Governing Agentic AI Systems." DeepMind. [arXiv:2401.13138](https://arxiv.org/abs/2401.13138). [Governance framework for agentic AI: oversight, monitoring, alignment.]

### Formal verification

- Lomuscio, A., Qu, H., & Raimondi, F. (2017). "MCMAS: An open-source model checker for the verification of multi-agent systems." *International Journal on Software Tools for Technology Transfer*, 19(1), 9-30. [doi:10.1007/s10009-015-0378-x](https://doi.org/10.1007/s10009-015-0378-x). [Model checking for MAS properties.]
- Kwiatkowska, M., Norman, G., & Parker, D. (2011). "PRISM 4.0: Verification of Probabilistic Real-Time Systems." *CAV 2011*, LNCS 6806, 585-591. [doi:10.1007/978-3-642-22110-1_47](https://doi.org/10.1007/978-3-642-22110-1_47). [Probabilistic model checker.]
- Lamport, L. (2002). *Specifying Systems: The TLA+ Language and Tools for Hardware and Software Engineers*. Addison-Wesley. [tla.html](https://lamport.azurewebsites.net/tla/tla.html). [TLA+ specification language.]
