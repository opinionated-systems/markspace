# Markspace Coordination Protocol: Specification

**Version**: 2026-03-23-draft
**Status**: Working draft

This document is the formal specification for the markspace protocol. For the ideas and motivation behind it, see [framework.md](framework.md).

## Contents

- [Abstract](#abstract)
- [1. Terminology](#1-terminology)
- [2. Mark Types](#2-mark-types)
  - [2.1 Common Fields](#21-common-fields)
  - [2.2 Intent](#22-intent)
  - [2.3 Action](#23-action)
  - [2.4 Observation](#24-observation)
  - [2.5 Warning](#25-warning)
  - [2.6 Need](#26-need)
- [3. Decay](#3-decay)
  - [3.1 Decay Rules](#31-decay-rules)
  - [3.2 Formal Properties](#32-formal-properties)
- [4. Trust](#4-trust)
  - [4.1 Source Levels](#41-source-levels)
  - [4.2 Effective Strength](#42-effective-strength)
  - [4.3 Formal Properties](#43-formal-properties)
- [5. Reinforcement](#5-reinforcement)
  - [5.1 Combination Rule](#51-combination-rule)
  - [5.2 Grouping](#52-grouping)
  - [5.3 Formal Properties](#53-formal-properties)
- [6. Conflict Resolution](#6-conflict-resolution)
  - [6.1 Resolution Protocol](#61-resolution-protocol)
  - [6.2 Deferred Resolution](#62-deferred-resolution)
  - [6.3 Post-Action Resolution](#63-post-action-resolution)
  - [6.4 Formal Properties](#64-formal-properties)
- [7. Scope](#7-scope)
  - [7.1 Scope Definition](#71-scope-definition)
  - [7.2 DecayConfig](#72-decayconfig)
  - [7.3 Scope Authorization](#73-scope-authorization)
  - [7.4 Scope Visibility](#74-scope-visibility)
  - [7.5 Hierarchical Scopes](#75-hierarchical-scopes)
  - [7.6 Formal Properties](#76-formal-properties)
- [8. Mark Space](#8-mark-space)
  - [8.1 Write](#81-write)
  - [8.2 Read](#82-read)
  - [8.3 Resolve](#83-resolve)
  - [8.4 Aggregate Needs](#84-aggregate-needs)
  - [8.5 Formal Properties](#85-formal-properties)
  - [8.6 Write Hooks](#86-write-hooks)
- [9. Guard](#9-guard-deterministic-enforcement-layer)
  - [9.1 Principle](#91-principle)
  - [9.2 Guard Operations](#92-guard-operations)
  - [9.3 GuardDecision](#93-guarddecision)
  - [9.4 What the Agent Still Does](#94-what-the-agent-still-does)
  - [9.5 Warning Invalidation](#95-warning-invalidation)
  - [9.6 Formal Properties](#96-formal-properties)
  - [9.7 Statistical Envelope](#97-statistical-envelope)
  - [9.8 Absorbing Barrier](#98-absorbing-barrier)
  - [9.9 Diagnostic Probe](#99-diagnostic-probe)
  - [9.10 Token Budgets](#910-token-budgets)
  - [9.11 Telemetry](#911-telemetry)
  - [9.12 Scope Rate Limits](#912-scope-rate-limits)
- [10. Generalized Supersession](#10-generalized-supersession)
  - [10.1 Formal Properties](#101-formal-properties)
- [11. Agent](#11-agent)
  - [11.1 Agent Definition](#111-agent-definition)
  - [11.2 Agent-Local Rules](#112-agent-local-rules)
  - [11.3 Agent Manifests](#113-agent-manifests)
- [12. Reference Implementation](#12-reference-implementation)
  - [12.1 DSL Usage](#121-dsl-usage)
- [13. Agent Composition](#13-agent-composition)
  - [13.1 Watch Patterns](#131-watch-patterns)
  - [13.2 Agent Manifests](#132-agent-manifests)
  - [13.3 Subscription](#133-subscription)
  - [13.4 Watched Marks](#134-watched-marks)
  - [13.5 Composition Validation](#135-composition-validation)
  - [13.6 Formal Properties](#136-formal-properties)
- [14. Scheduling](#14-scheduling)
  - [14.1 Schedule Configuration](#141-schedule-configuration)
  - [14.2 Scheduler](#142-scheduler)
  - [14.3 Formal Properties](#143-formal-properties)
- [15. Properties Summary](#15-properties-summary)
- [16. Conformance](#16-conformance)

## Abstract

This document specifies a coordination protocol for autonomous agent fleets based on indirect coordination through marks left in a shared environment (stigmergy). The protocol defines five mark types, their lifecycle semantics, and the operations agents perform on them. A conforming implementation enables N agents to coordinate without direct communication, central scheduling, or consensus protocols.

The specification is accompanied by a Python reference implementation and property tests. Every normative statement (MUST, SHOULD, MAY) maps to an executable test. The reference implementation demonstrates that these properties hold together consistently; any language can reimplement it by satisfying the same test suite.

**Verification level.** The 47 mandatory properties (P1-P47, P40-P43) are verified empirically through Python property-based tests (pytest + [Hypothesis](https://github.com/HypothesisWorks/hypothesis)), not through formal model checking (e.g., [MCMAS](https://doi.org/10.1007/s10009-015-0378-x), [PRISM](https://doi.org/10.1007/978-3-642-22110-1_47), or [TLA+](https://lamport.azurewebsites.net/tla/tla.html)). The tests exercise each property across randomized inputs and edge cases, and the [stress test](../experiments/stress_test/analysis.md) validates them under realistic concurrent load - up to 1,050 agents across 20 rounds with adversarial participants, zero safety violations across all 21 trial runs and 4,010 agent instances ([trial results](../experiments/trials/analysis.md)). This provides high confidence that the properties hold in practice but does not constitute a formal proof. A formal verification effort would strengthen the safety guarantees, particularly for P11 (Determinism) and P12 (Progress), which make claims about all possible states.

## 1. Terminology

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

**Mark**: A typed record written to a shared space by an agent. The fundamental unit of coordination.

**Mark Space**: The shared environment where marks are stored. Agents interact only through the mark space, never directly with each other.

**Agent**: An autonomous process with an identity and a set of scope permissions. Agents read and write marks. Agents do not communicate with other agents.

**Scope**: A namespace that defines what kinds of marks can exist within it, their decay parameters, and conflict resolution rules.

**Strength**: A non-negative real number representing a mark's current influence. Computed at read time from the mark's initial strength, age, decay function, and trust source.

**Principal**: The human (or system) that agents serve. The principal interacts with the system by writing action marks that resolve needs.

**Freshness**: The time-dependent component of strength. All marks except actions and unresolved needs decay over time.

## 2. Mark Types

There are exactly five mark types. An implementation MUST support all five. An implementation MUST NOT define additional mark types at the protocol level. (Domain-specific semantics are expressed through scope definitions, not new mark types.)

### 2.1 Common Fields

All definitions in this specification use [Protocol Buffers proto3](https://protobuf.dev/programming-guides/proto3/) syntax in the `markspace` package.

Every mark MUST contain:

```protobuf
syntax = "proto3";

package markspace;

import "google/protobuf/struct.proto";

enum MarkType {
  INTENT      = 0;
  ACTION      = 1;
  OBSERVATION = 2;
  WARNING     = 3;
  NEED        = 4;
}

message Mark {
  string          id               = 1;  // UUID, assigned by the mark space on write
  MarkType        mark_type        = 2;
  string          agent_id         = 3;  // UUID
  string          scope            = 4;  // scope namespace this mark belongs to
  double          created_at       = 5;  // unix timestamp
  double          initial_strength = 6;  // default 1.0, MUST be >= 0.0
  optional string supersedes       = 7;  // MAY: UUID of a prior mark this replaces (Section 10)
}
```

Ref: [`markspace/core.py::Mark`](../markspace/core.py)

### 2.2 Intent

An intent mark declares that an agent plans to act on a resource.

Additional fields (all MUST):

```protobuf
message IntentMark {
  Mark   base       = 1;
  string resource   = 2;  // the resource the agent intends to modify
  string action     = 3;  // planned action (must be in scope's intent actions)
  float  confidence = 4;  // [0.0, 1.0] how committed the agent is
}
```

**Lifecycle**:
- An intent mark MUST have a TTL, defined by the scope.
- An intent mark MUST be removed (strength → 0) when its TTL expires.
- An intent mark SHOULD be superseded by a corresponding action mark when the agent executes the planned action.
- An intent mark that is not followed by an action mark before TTL expiry indicates the agent abandoned the plan. No cleanup is required; expiry handles cleanup.

**Conflict semantics**: See [Section 6](#6-conflict-resolution).

Ref: [`markspace/core.py::Intent`](../markspace/core.py)

### 2.3 Action

An action mark records that an agent did something. Actions are facts.

Additional fields:

```protobuf
message ActionMark {
  Mark                     base       = 1;
  optional string          resource   = 2;  // MAY: the resource that was modified
  string                   action     = 3;  // MUST: what was done
  google.protobuf.Struct   result     = 4;  // MUST: outcome of the action
  bool                     failed     = 5;  // MAY: true when the tool threw an exception
  // Note: supersedes is on the base Mark (field 7), not duplicated here.
  // All mark types support supersession (Section 10).
}
```

**Lifecycle**:
- Action marks MUST NOT decay. Their strength is constant for all time.
- Action marks are historical facts. They are the ground truth of the system.
- An action mark MAY supersede a prior mark (action or intent). A superseded mark's effective strength becomes 0.
- An action mark with `failed = true` records that the tool threw an exception. Failed actions MUST NOT claim the resource - the guard treats them as non-blocking so other agents can proceed ([Section 9](#9-guard-deterministic-enforcement-layer)).

Ref: [`markspace/core.py::Action`](../markspace/core.py)

### 2.4 Observation

An observation mark records something an agent perceived about the world.

Additional fields (all MUST):

```protobuf
enum Source {
  FLEET               = 0;
  EXTERNAL_VERIFIED   = 1;
  EXTERNAL_UNVERIFIED = 2;
}

message ObservationMark {
  Mark                   base       = 1;
  string                 topic      = 2;  // what was observed
  google.protobuf.Struct content    = 3;  // the observation itself
  float                  confidence = 4;  // [0.0, 1.0]
  Source                 source     = 5;
}
```

**Lifecycle**:
- Observation marks MUST decay over time according to the scope's `observation_half_life`.
- Decay MUST be exponential: `strength(t) = initial_strength * 0.5^(age / half_life)`.
- An observation mark with strength below a threshold (RECOMMENDED: 0.01) MAY be garbage collected.

**Reinforcement**: Multiple observation marks on the same scope + topic combine via the reinforcement function ([Section 5](#5-reinforcement)). Independent convergent observations strengthen the signal.

Ref: [`markspace/core.py::Observation`](../markspace/core.py)

### 2.5 Warning

A warning mark declares that a previous mark or assumption is no longer valid.

Additional fields:

```protobuf
enum Severity {
  INFO     = 0;
  CAUTION  = 1;
  CRITICAL = 2;
}

message WarningMark {
  Mark            base        = 1;
  optional string invalidates = 2;  // MAY: UUID of the mark being contradicted
  string          topic       = 3;  // MUST: what the warning is about
  string          reason      = 4;  // MUST: why the prior information is invalid
  Severity        severity    = 5;  // MUST
}
```

**Lifecycle**:
- Warning marks MUST decay over time with a shorter half-life than observations (RECOMMENDED: scope's `warning_half_life`, typically < `observation_half_life`).
- Decay MUST be exponential, same formula as observations.
- A warning mark that references `invalidates` MUST reduce the effective strength of the target mark. The reduction amount equals the warning's current strength.

Ref: [`markspace/core.py::Warning`](../markspace/core.py)

### 2.6 Need

A need mark requests input from the principal.

Additional fields:

```protobuf
message NeedMark {
  Mark                   base        = 1;
  string                 question    = 2;  // MUST: what input is needed
  google.protobuf.Struct context     = 3;  // MUST: information relevant to the question
  float                  priority    = 4;  // MUST: [0.0, 1.0] urgency
  bool                   blocking    = 5;  // MUST: whether the agent is blocked waiting
  optional string        resolved_by = 6;  // MAY: UUID of the action mark that resolved this
}
```

**Lifecycle**:
- Unresolved need marks (resolved_by is None) MUST NOT decay. They persist at full strength indefinitely.
- Resolved need marks (resolved_by is set) MUST immediately have strength 0.
- Need marks are resolved when the principal writes an action mark scoped to the need.

Ref: [`markspace/core.py::Need`](../markspace/core.py)

## 3. Decay

Decay is the mechanism that keeps the mark space current. It is the stigmergic equivalent of pheromone evaporation. The exponential decay model has theoretical grounding in the [Age of Information](https://doi.org/10.1109/ISIT.2012.6283535) (AoI) literature (Kaul et al., 2012), which shows that monotonically decreasing freshness functions are optimal for status-update systems.

### 3.1 Decay Rules

An implementation MUST compute mark strength at **read time**, not write time. Marks are not modified in storage; their effective strength is a pure function of the mark's properties and the current time.

```
strength(mark, now) → float
```

The function MUST satisfy:

| Mark Type | Decay Rule |
|-----------|------------|
| `intent` | Full strength until TTL, then 0. Step function. |
| `action` | Constant (non-superseded). `strength(t) = initial_strength` for all t. Superseded actions have effective strength 0 (Section 10). |
| `observation` | Exponential. `strength(t) = initial * 0.5^(age / half_life)` |
| `warning` | Exponential. Same formula, with the scope's `warning_half_life`. |
| `need` | Full strength while unresolved. 0 when resolved. Step function. |

Ref: [`markspace/core.py::compute_strength`](../markspace/core.py)

### 3.2 Formal Properties

**P1: Decay Monotonicity**: For observation and warning marks, strength MUST be a monotonically non-increasing function of time. For all `t2 > t1`: `strength(mark, t2) <= strength(mark, t1)`.

**P2: Action Permanence**: For non-superseded action marks, base strength MUST be constant. For all `t1, t2`: `compute_strength(action_mark, t1) == compute_strength(action_mark, t2)`. A superseded action's effective strength is 0 (Section 10), but its base strength remains unchanged - supersession affects visibility, not the underlying record.

**P3: Convergence**: Given no new marks, the total strength of all transient marks (observations + warnings) in the space MUST converge to 0 as t -> infinity.

**P4: Intent Expiry**: An intent mark's strength MUST be 0 for all `t > created_at + ttl`.

**P5: Need Persistence**: An unresolved need mark's strength MUST equal its initial strength for all t.

Ref: [`tests/test_properties.py::TestDecayProperties`](../tests/test_properties.py)

## 4. Trust

Trust is source-based weighting applied to marks when computing their effective strength.

### 4.1 Source Levels

Three source levels, in strict total order:

```
fleet  >  external_verified  >  external_unverified
```

An implementation MUST assign trust weights that preserve this order. The RECOMMENDED weights:

| Source | Weight |
|--------|--------|
| `fleet` | 1.0 |
| `external_verified` | 0.7 |
| `external_unverified` | 0.3 |

### 4.2 Effective Strength

The effective strength of a mark, incorporating both decay and trust:

```
effective_strength(mark, now) = compute_strength(mark, now) * trust_weight(mark.source)
```

For mark types that don't have a `source` field (intent, action, need), trust weight MUST be 1.0 (these marks are always fleet-internal).

### 4.3 Formal Properties

**P6: Trust Ordering**: For two marks with identical properties except source, a mark with a higher-trust source MUST have higher effective strength. For all t: `effective_strength(fleet_mark, t) >= effective_strength(verified_mark, t) >= effective_strength(unverified_mark, t)`.

**P7: Trust Bounds**: Effective strength MUST be in [0.0, max_strength * 1.0]. Trust weighting MUST NOT amplify strength beyond the base decay value.

Ref: [`markspace/core.py::trust_weight`](../markspace/core.py), [`tests/test_properties.py::TestTrustProperties`](../tests/test_properties.py)

## 5. Reinforcement

When multiple marks exist on the same scope + topic, they combine to produce an aggregate signal. The reinforcement function is a utility: `read()` returns individual marks with their effective strengths; callers wanting an aggregate signal call `reinforce()` on the returned strengths. This separation keeps reads simple and lets callers choose whether to aggregate.

### 5.1 Combination Rule

Reinforcement MUST be **sublinear**. N identical marks MUST produce aggregate strength less than N times a single mark's strength. This prevents mark flooding.

The RECOMMENDED combination:

```
Given marks [m1, m2, ..., mn] on the same scope+topic, sorted by effective_strength descending:

  aggregate = effective_strength(m1)
  for each subsequent mi:
    aggregate += effective_strength(mi) * REINFORCEMENT_FACTOR

  return min(aggregate, REINFORCEMENT_CAP)
```

Where:
- `REINFORCEMENT_FACTOR` MUST be in (0.0, 1.0). RECOMMENDED: 0.3.
- `REINFORCEMENT_CAP` MUST be finite. RECOMMENDED: 2.0 * max_single_mark_strength.

### 5.2 Grouping

Marks are grouped for reinforcement by `(scope, topic)` for observations and warnings, and by `(scope, resource)` for intents and actions. Need marks are not reinforced (each need is unique to an agent).

### 5.3 Formal Properties

**P8: Sublinearity**: For N identical marks, `aggregate_strength(N marks) < N * single_mark_strength` for all N > 1.

**P9: Boundedness**: `aggregate_strength(any number of marks) <= REINFORCEMENT_CAP`.

**P10: Monotonic Addition**: Adding a positive-strength mark MUST NOT decrease aggregate strength. `aggregate_strength(marks + [new_mark]) >= aggregate_strength(marks)` when `effective_strength(new_mark) > 0`.

Ref: [`markspace/core.py::reinforce`](../markspace/core.py), [`tests/test_properties.py::TestReinforcementProperties`](../tests/test_properties.py)

## 6. Conflict Resolution

When two or more agents write intent marks targeting the same resource in the same scope, a conflict exists. Conflict resolution is deterministic and local: each agent resolves conflicts independently by reading the mark space.

### 6.1 Resolution Protocol

When an agent reads intent marks on a resource it also intends to modify:

1. Collect all active intent marks (strength > 0) on `(scope, resource)`.
2. If no other intents exist, proceed.
3. If other intents exist, apply the scope's conflict policy:

**FIRST_WRITER**: The intent with the earliest `created_at` wins. All other agents MUST yield.

**HIGHEST_CONFIDENCE**: The intent with the highest `confidence` wins. Ties broken by `created_at` (earliest wins). Losing agents MUST yield.

**YIELD_ALL**: All agents with conflicting intents MUST write a need mark requesting principal resolution. No agent proceeds until the principal decides.

"Yield" means: the agent MUST NOT write an action mark on the contested resource. The agent MAY write a new intent on an alternative resource, wait and retry after the winning intent's TTL expires, or write a need mark.

### 6.2 Deferred Resolution

Lock-based guards ([Section 9](#9-guard-deterministic-enforcement-layer)) serialize access: at most one agent enters `pre_action` at a time per scope. Under serialization, HIGHEST_CONFIDENCE degenerates to FIRST_WRITER because the second agent finds an already-committed action, not a competing intent. The guard returns CONFLICT (resource taken), never performing the confidence comparison. This is [priority inversion](https://www.cs.cornell.edu/courses/cs614/1999sp/papers/pathfinder.html): a low-priority agent holding the lock blocks a higher-priority agent.

Deferred resolution fixes this by separating claim collection from allocation.

#### 6.2.1 Protocol

The deferred resolution protocol has three phases:

**Phase 1: Claim collection.** Agents write intent marks to the mark space. The guard operates in advisory mode during this phase: it MAY acquire a lock for serializing writes (implementation-defined), but no resource allocation or ALLOW/CONFLICT verdicts are issued. Each intent carries the agent's confidence (priority) for the resource.

**Phase 2: Resolution boundary.** A resolution boundary is triggered by an external event: end of a scheduling round, a timer, a principal action, or an explicit `resolve_deferred(scope, resource)` call. The trigger mechanism is deployment-defined; the spec only requires that a boundary eventually occurs for any scope with pending intents.

**Phase 3: Batch resolution.** At the boundary, the guard:

1. Collects all active intent marks on `(scope, resource)` with strength > 0.
2. Applies the scope's conflict policy to the full set (not pairwise). For HIGHEST_CONFIDENCE, the intent with the highest confidence wins; ties broken by `created_at`.
3. The winning agent's intent is converted to an action mark (or the agent is notified to proceed).
4. All losing agents receive BLOCKED verdicts. Losing intents remain in the mark space until TTL expiry (they are not forcibly removed).

```
Timeline:

  Agent A writes Intent(resource=R, confidence=0.5)      ──┐
  Agent B writes Intent(resource=R, confidence=0.95)     ──┤ Phase 1: collection
  Agent C writes Intent(resource=R, confidence=0.7)      ──┘

  Resolution boundary fires                              ── Phase 2: trigger

  Guard reads all intents on R                           ──┐
  Guard applies HIGHEST_CONFIDENCE → B wins              ──┤ Phase 3: resolution
  B proceeds, A and C get BLOCKED                        ──┘
```

#### 6.2.2 Applicability

Deferred resolution is REQUIRED for scopes that use HIGHEST_CONFIDENCE with a lock-based guard. Without it, HIGHEST_CONFIDENCE is operationally equivalent to FIRST_WRITER (whichever thread acquires the lock first wins).

Deferred resolution is OPTIONAL for FIRST_WRITER scopes (serialized first-come-first-served is already the intended semantics) and for YIELD_ALL scopes (all intents are escalated to the principal regardless).

#### 6.2.3 Relationship to Guard

During Phase 1, the guard operates in **advisory mode**: it writes intent marks but does not acquire a resource lock or return ALLOW/CONFLICT. Agents receive BLOCKED (pending resolution) instead. During Phase 3, the guard operates normally: it acquires the lock, checks the winner, and returns ALLOW for the winner only.

An implementation MAY combine deferred and immediate resolution within the same mark space. Some scopes (e.g., room bookings with FIRST_WRITER) resolve immediately; others (e.g., parking with HIGHEST_CONFIDENCE) defer. The scope's conflict policy and a per-scope `deferred: bool` flag determine which path is taken.

**Verdict delivery.** `resolve_deferred(scope, resource)` returns a mapping of agent_id to verdict (ALLOW for the winner, BLOCKED for losers). The delivery mechanism is deployment-defined: the harness MAY use callbacks, polling, subscription notifications (Section 13.3), or direct return values. The spec requires only that (a) exactly one agent receives ALLOW per (scope, resource) per resolution boundary, and (b) all losing agents eventually receive their BLOCKED verdict.

**Wildcard topic matching.** When a scope's `observation_topics` or `warning_topics` contains `"*"`, any topic string matches. The `"*"` is a literal wildcard (not a glob or regex) - it means "this scope accepts any topic." Named topics in the same list are redundant when `"*"` is present but not an error.

### 6.3 Post-Action Resolution

If two agents both write action marks on the same resource (race condition where both executed before reading the other's intent), the **later action supersedes the earlier one**. The agent whose action was superseded SHOULD be notified via a warning mark. Intent marks are the primary conflict prevention mechanism; post-action resolution is the fallback.

### 6.4 Formal Properties

**P11: Determinism**: Given the same set of intent marks, all agents MUST reach the same conclusion about which intent wins. The resolution function is pure: `resolve(intents) → winner_id`.

**P12: Progress**: At least one agent MUST be able to proceed on each contested (scope, resource). The protocol MUST NOT deadlock. *Proof sketch*: Under HIGHEST_CONFIDENCE and FIRST_WRITER, exactly one intent wins per (scope, resource) - the resolution function is a total order on intents, so a winner always exists. Under YIELD_ALL, agents are blocked pending principal input - progress depends on the principal responding (P16 provides the liveness bound via TTL expiry). Circular dependency deadlock cannot occur because conflict resolution operates on individual (scope, resource) pairs independently, not across resources.

**P13: Consistency (Yield Recovery)**: If agent A yields to agent B's intent, and agent B later abandons (intent expires), agent A MUST NOT be permanently prevented from acting on that resource. Specifically, once no higher-priority intent or action exists on (scope, resource), a new intent from A MUST be evaluable by the guard without reference to the expired yielding history.

**P14: Deferred Completeness**: Under deferred resolution, the batch resolution step MUST consider ALL active intents on `(scope, resource)` at the resolution boundary. An intent written before the boundary and still within its TTL MUST NOT be excluded from the comparison.

**P15: Deferred Priority Fidelity**: Under deferred resolution with HIGHEST_CONFIDENCE, the winning intent MUST be the one with the highest confidence among all candidates at the resolution boundary. The result MUST be identical to what HIGHEST_CONFIDENCE would produce if all intents were evaluated simultaneously (no serialization effects).

**P16: Deferred Liveness**: For any scope with `deferred: true` and at least one active intent, a resolution boundary MUST eventually occur. The protocol MUST NOT allow intents to accumulate indefinitely without resolution (intents still expire via TTL as a safety net, but the resolution mechanism SHOULD fire before TTL expiry under normal operation).

**P17: Deferred Mutual Exclusion**: Under deferred resolution, at most one agent MUST receive ALLOW per (scope, resource) per resolution boundary. If N agents have active intents on the same (scope, resource), exactly one wins and N-1 receive BLOCKED.

**P18: Deferred Inclusion**: All active intents (strength > 0) on (scope, resource) at the resolution boundary MUST be included in the comparison. An implementation MUST NOT exclude an intent that was written before the boundary and is still within its TTL. Note: perpetual outbidding (an agent always writing a higher-confidence intent just before each boundary) is not prevented by the protocol - it is an acknowledged limitation analogous to priority inversion in real-time systems. TTL expiry provides the safety net: an outbidding agent that never executes will eventually have its intent expire.

Ref: [`markspace/core.py::resolve_conflict`](../markspace/core.py), [`tests/test_properties.py::TestConflictProperties`](../tests/test_properties.py)

## 7. Scope

A scope defines a namespace, the allowed mark actions within it, decay parameters, and conflict resolution policy.

### 7.1 Scope Definition

```protobuf
enum ScopeVisibility {
  OPEN       = 0;
  PROTECTED  = 1;
  CLASSIFIED = 2;
}

enum ConflictPolicy {
  HIGHEST_CONFIDENCE = 0;
  FIRST_WRITER       = 1;
  YIELD_ALL          = 2;
}

message Scope {
  string          name               = 1;  // hierarchical namespace ("calendar", "research/topic/X")
  ScopeVisibility visibility         = 2;  // default: OPEN (Section [7.4](#74-scope-visibility))
  repeated string allowed_intent_verbs     = 3;  // allowed intent action verbs
  repeated string allowed_action_verbs     = 4;  // allowed action verbs
  repeated string observation_topics = 5;  // allowed observation topics ("*" for any)
  repeated string warning_topics     = 6;  // allowed warning topics ("*" for any)
  DecayConfig     decay              = 7;  // half-lives and TTLs for this scope
  ConflictPolicy  conflict_policy    = 8;  // how intent conflicts are resolved
}
```

### 7.2 DecayConfig

```protobuf
message DecayConfig {
  double observation_half_life = 1;  // seconds. MUST be > 0.
  double warning_half_life     = 2;  // seconds. MUST be > 0. SHOULD be <= observation_half_life.
  double intent_ttl            = 3;  // seconds. MUST be > 0.
}
```

### 7.3 Scope Authorization

An agent's permissions are defined as:
- **Write permissions**: a set of (scope, mark_type) pairs. An agent MUST NOT write a mark to a scope it is not authorized for. An implementation MUST reject unauthorized writes.
- **Read permissions**: a set of scope names granting full content access. Only relevant for PROTECTED and CLASSIFIED scopes (see [Section 7.4](#74-scope-visibility)).

For OPEN scopes, reads are unrestricted and any agent reads full marks. This mirrors biological stigmergy where any ant can smell any pheromone. For PROTECTED and CLASSIFIED scopes, read authorization controls content access.

### 7.4 Scope Visibility

Scopes declare a **visibility level** that controls read access. The three levels are a simplified [Bell-LaPadula](https://apps.dtic.mil/sti/citations/AD0770768) lattice (Bell & LaPadula, 1973), reduced from arbitrary security levels to exactly three because the use case is coordination visibility, not military classification. The novel element is the projected read (PROTECTED): Bell-LaPadula has no analogue for "you can see that a mark exists but not what it says." Three levels, in order of increasing restriction:

```
OPEN  >  PROTECTED  >  CLASSIFIED
```

| Visibility | Unauthorized Reader Sees | Authorized Reader Sees |
|------------|--------------------------|----------------------|
| `OPEN` | Full marks | Full marks |
| `PROTECTED` | Projected marks (content redacted) | Full marks |
| `CLASSIFIED` | Nothing (empty list) | Full marks |

**Default**: OPEN. Scopes that don't specify visibility are OPEN.

**Projected reads** (PROTECTED scopes, unauthorized reader):

A projected mark preserves all **structural/coordination metadata** but redacts **content fields**. This allows coordination (conflict avoidance, resource tracking) while hiding sensitive business data.

| Category | Fields | Projected? |
|----------|--------|-----------|
| Structural | id, mark_type, agent_id, scope, created_at, initial_strength, supersedes, projected | Preserved |
| Coordination | resource, action, topic, confidence, severity, priority, blocking, source, invalidates, resolved_by | Preserved |
| Content | result (Action), content (Observation), reason (Warning), question (Need), context (Need) | **Redacted** |

A projected mark MUST have `projected = true` so downstream code can distinguish it from a mark that genuinely has empty content fields.

**Infrastructure access**: Internal components (guard, aggregator) that pass `reader=None` to the read operation MUST receive full marks regardless of scope visibility. The guard cannot enforce coordination if it can't see marks.

**Examples**:

```python
# OPEN scope (team calendar): everyone sees everything
calendar = Scope(name="calendar", visibility=ScopeVisibility.OPEN)

# PROTECTED scope (HR data): agents see "there's a salary observation
# on employee-X at strength 0.95" but not the salary amount
hr = Scope(name="hr", visibility=ScopeVisibility.PROTECTED)

# CLASSIFIED scope (legal investigations): unauthorized agents see
# nothing, can't even tell that marks exist
legal = Scope(name="legal", visibility=ScopeVisibility.CLASSIFIED)
```

**When to use PROTECTED vs CLASSIFIED**:
- PROTECTED: the existence of marks is not sensitive, but their content is. Agents can still coordinate around protected resources (see marks exist, avoid conflicts) without seeing the data.
- CLASSIFIED: even the existence of marks is sensitive. An agent shouldn't know that a legal investigation mark exists for employee-X. Coordination in classified scopes is limited to authorized agents. Unauthorized agents cannot avoid conflicts they can't see, so the guard becomes the sole coordination mechanism.

### 7.5 Hierarchical Scopes

Scope names MAY use `/` as a hierarchy separator. Authorization for a parent scope (e.g., `research`) implies authorization for all child scopes (e.g., `research/topic/X`). An implementation MUST support this inheritance.

### 7.6 Formal Properties

**P19: Scope Isolation**: An agent without authorization for scope S MUST be unable to write any mark to S. `write(unauthorized_agent, mark_in_S) → Error`.

**P20: Structural Visibility**: For OPEN scopes, any agent MUST be able to read full marks regardless of authorization. For PROTECTED scopes, any agent MUST be able to read projected marks (structural metadata preserved, content redacted). `reader=None` (infrastructure) MUST receive full marks regardless of visibility.

**P21: Content Access**: For PROTECTED scopes, an agent MUST have read authorization to access content fields. Without read authorization, an implementation MUST return projected marks with content fields redacted and `projected=true`.

**P22: Classified Opacity**: For CLASSIFIED scopes, an agent without read authorization MUST receive an empty result. CLASSIFIED scopes MUST NOT fall back to projected reads. It is all-or-nothing.

**P23: Hierarchy**: Write authorization for scope `"a"` MUST imply write authorization for `"a/b"` and `"a/b/c"` for all b, c. Read authorization MUST follow the same rule.

**P24: Projection Preservation**: A projected mark MUST retain all structural and coordination metadata (id, mark_type, agent_id, scope, created_at, initial_strength, resource, action, topic, confidence, severity, priority, blocking, source, invalidates, resolved_by, supersedes). Only content fields (result, content, reason, question, context) are redacted. `projected` MUST be `true`. *Note: P24 refines P20 (Structural Visibility) by specifying exactly which fields are structural vs content.*

**P25: Classified No Fallback**: CLASSIFIED scopes MUST NOT provide projected reads as a fallback. An unauthorized reader MUST receive an empty list, not projected marks. This prevents existence leakage. *Note: P25 strengthens P22 (Classified Opacity) by explicitly ruling out projected reads as a fallback path.*

**P26: Visibility Hierarchy Inheritance**: A child scope without its own definition MUST inherit the parent scope's visibility level. Read authorization for a parent scope MUST imply read authorization for all child scopes.

Ref: [`markspace/core.py::Scope, Agent, ScopeVisibility, project_mark`](../markspace/core.py), [`tests/test_properties.py::TestScopeVisibility`](../tests/test_properties.py)

## 8. Mark Space

The mark space is the shared environment. It stores marks and provides read/write operations.

### 8.1 Write

```
write(agent, mark) → mark_id

Preconditions:
  - agent MUST be authorized for mark.scope ([Section 7.3](#73-scope-authorization))
  - mark.action MUST be in scope's allowed actions for mark.mark_type (if applicable)
  - mark MUST satisfy type-specific field requirements ([Section 2](#2-mark-types))

Postconditions:
  - mark is stored with a new unique id and created_at = now
  - mark is immediately visible to subsequent reads (subject to scope visibility rules, Section 7.4)
```

### 8.2 Read

```
read(scope, resource=None, topic=None, mark_type=None, min_strength=0.01, reader=None) → list[Mark]

  - Returns all marks in the given scope matching the filters
  - Each mark's effective_strength is computed at read time (Section 3 + 4)
  - Marks with effective_strength < min_strength are excluded
  - Results SHOULD be sorted by effective_strength descending
  - reader: the agent performing the read (controls visibility per Section 7.4)
    - None: full access (used by guard, aggregator, internal infrastructure)
    - Agent: respects scope visibility rules (OPEN/PROTECTED/CLASSIFIED)
```

Read is a pure query with no side effects. Reading marks MUST NOT modify them. For PROTECTED scopes with an unauthorized reader, the returned marks are projections (content redacted, `projected=true`). For CLASSIFIED scopes with an unauthorized reader, the result is an empty list.

### 8.3 Resolve

```
resolve(need_mark_id, resolving_action_id) → mark_id

  - Creates a resolved copy with resolved_by = resolving_action_id
  - The resolved copy supersedes the original need mark
  - The need mark's effective strength immediately becomes 0
  - Returns the new mark's id
```

### 8.4 Aggregate Needs

```
aggregate_needs() → repeated NeedCluster
```

```protobuf
message NeedCluster {
  string                          scope              = 1;
  repeated NeedMark               needs              = 2;
  float                           effective_priority = 3;
  int32                           blocking_count     = 4;
  repeated google.protobuf.Struct contexts           = 5;
}
```

Need aggregation:
1. Group unresolved need marks by scope.
2. Within each scope, cluster needs with similar questions (implementation-defined similarity).
3. For each cluster: `effective_priority = max(individual priorities) + log(cluster_size) * 0.1`.
4. Sort clusters by effective_priority descending.

The aggregator is deliberately simple. It groups, scores, and sorts. It has no intelligence.

### 8.5 Formal Properties

**P27: Write Visibility**: A mark written at time t MUST be visible to authorized reads at any time t' > t (within the mark's active lifetime), subject to scope visibility rules (Section 7.4). For CLASSIFIED scopes, visibility is limited to authorized readers; for PROTECTED scopes, unauthorized readers see projected marks.

**P28: Read Purity**: Reading marks MUST NOT change any mark's stored state. *Note: trivially true by the compute-at-read-time design (Section 3.1), but stated explicitly to prevent implementations from introducing write-on-read side effects (e.g., eagerly pruning expired marks during reads).*

**P29: Resolution Immediacy**: Resolving a need mark MUST immediately reduce its effective strength to 0 on the next read.

**P30: Mark Immutability**: A mark's fields MUST NOT be mutated after write. All marks are frozen. Need resolution creates a new mark (with `resolved_by` set) that supersedes the original, consistent with the supersession mechanism used for all mark updates. All other state changes (decay, warning invalidation, supersession) are computed at read time from immutable data.

**P31: Mark ID Uniqueness**: Every call to write() MUST assign a unique id. No two marks in the same mark space MUST share an id. Broken uniqueness would corrupt supersession chains and warning invalidation references.

**P32: Total Write Ordering**: Marks MUST be totally ordered by (created_at, id). The mark space MUST assign monotonically non-decreasing created_at timestamps. When two marks share the same created_at (concurrent writes within clock resolution), id serves as the tiebreaker. This ordering is used by conflict resolution (FIRST_WRITER) and write-order delivery (P52).

Ref: [`markspace/space.py::MarkSpace`](../markspace/space.py), [`tests/test_properties.py::TestMarkSpaceProperties`](../tests/test_properties.py)

### 8.6 Write Hooks

The mark space supports post-write hooks - callbacks invoked after every successful `write()` call. Hooks enable the adaptive monitoring layer (Section 9.7) to observe all writes without modifying the write path itself.

```
add_write_hook(callback) -> handle_uuid
remove_write_hook(handle_uuid) -> bool
```

Hooks receive `(agent_id, stored_mark)` after the mark is committed. Key semantics:

1. **Fire outside lock.** Hooks execute after the space's write lock is released, so a slow or failing hook cannot block subsequent writes.
2. **Handle-based removal.** `add_write_hook()` returns a UUID handle. `remove_write_hook(handle)` removes by handle in O(1).
3. **Silent failure.** If a hook raises an exception, it is silently caught. The stored mark and all subsequent reads are unaffected (P33).
4. **Snapshot semantics.** The set of hooks to fire is snapshotted inside the lock, so hooks added or removed during firing do not affect the current write's hook dispatch.

The primary consumer is the statistical envelope (Section 9.7), which registers a hook to record every write for anomaly detection.

**P33: Hook Non-Interference**: A post-write hook failure MUST NOT affect the stored mark or subsequent reads. Hooks are informational observers, not gatekeepers.

Ref: [`markspace/space.py::MarkSpace.add_write_hook`](../markspace/space.py)

## 9. Guard (Deterministic Enforcement Layer)

Agents cannot be trusted to voluntarily read marks before acting. LLMs are non-deterministic and may forget or ignore instructions. The guard moves coordination enforcement from the agent (unreliable) to the harness (deterministic). The guard is a descendant of [Hoare monitors](https://doi.org/10.1145/355620.361161) (1974): a monitor wraps shared state with procedures that enforce mutual exclusion and preconditions. Here, the guard wraps the mark space, ensuring every tool call passes through `pre_action` before execution.

```
Agent reasons → agent calls tool → GUARD checks marks → tool executes
                                    (deterministic)
→ GUARD writes action mark
```

### 9.1 Principle

Marks are WRITTEN by agents (voluntary, through LLM reasoning) but ENFORCED by the guard (deterministic, wrapping every tool call). The agent writes intents and observations as part of its reasoning. The guard reads intents and enforces conflict resolution mechanically before any tool executes.

Coordination reliability MUST NOT depend on the LLM being reliable.

**Architectural note.** The guard is a wrapper around the mark space, not a mandatory gateway. Code with direct access to the MarkSpace can call `write()` without passing through the guard, bypassing conflict resolution entirely. This is by design - the guard is infrastructure that the harness wires in, not a security boundary enforced by the mark space itself. Safety properties (P36-P39) hold only when the harness routes all tool-triggered writes through the guard. Implementations SHOULD document this constraint and MAY add an optional enforcement flag if the deployment requires it.

### 9.2 Guard Operations

```
pre_action(agent, scope, resource, action, confidence) → GuardDecision

  1. Check agent authorization (scope + mark type)
  2. Check for existing action marks on (scope, resource) from OTHER agents
     - If found: return CONFLICT (the resource is already claimed)
  3. Write an intent mark on behalf of the agent
  4. Read all active intents on (scope, resource)
  5. If no conflict: return ALLOW
  6. If conflict: resolve per scope's conflict_policy
     - Winner: return ALLOW
     - Loser: return CONFLICT (with winning_intent for agent's context)
     - YIELD_ALL: return BLOCKED, auto-write need mark for principal

post_action(agent, scope, resource, action, result, intent_id?) → mark_id

  1. Write an action mark recording the result
  2. If intent_id provided: action supersedes the intent

execute(agent, scope, resource, intent_action, result_action, tool_fn, confidence) → (decision, result)

  1. Call pre_action
  2. If not ALLOW: return (decision, None). tool_fn is never called.
  3. If ALLOW: call tool_fn, then post_action
  4. Return (decision, result)
```

### 9.3 GuardDecision

```protobuf
enum Verdict {
  ALLOW    = 0;
  CONFLICT = 1;
  BLOCKED  = 2;
  DENIED   = 3;
}

message GuardDecision {
  Verdict              verdict             = 1;
  string               reason              = 2;  // human-readable explanation
  optional UUID        intent_id           = 3;  // id of the intent written by this pre_action
  optional IntentMark  winning_intent      = 4;  // if CONFLICT, the intent that won
  repeated IntentMark  conflicting_intents = 5;  // other intents on this resource
}
```

The decision is informational for the agent's reasoning ("I was blocked because another agent has higher priority on this resource, I'll try a different one"). The agent cannot override the verdict; the harness enforces it.

### 9.4 What the Agent Still Does

The guard handles conflict enforcement. The agent still voluntarily:
- Writes observation marks (sharing knowledge with the fleet)
- Writes warning marks (invalidating stale information)
- Writes need marks (requesting principal input for non-conflict decisions)
- Chooses which resources to target (the guard only checks, it doesn't choose)

The division: **the agent decides WHAT to do. The guard decides WHETHER it's allowed.**

### 9.5 Warning Invalidation

When a warning mark references `invalidates: mark_id`, it reduces the target mark's effective strength.

```
effective_strength_with_warnings(mark, now) =
  max(0, effective_strength(mark, now) - sum(effective_strength(w, now) for w in warnings_targeting(mark)))
```

A warning at full strength completely cancels a mark of equal base strength. As the warning decays, the invalidated mark's effective strength recovers. This models the biological pattern: a repellent pheromone suppresses an attractant, but as the repellent evaporates, the attractant becomes detectable again (if it hasn't also evaporated).

**Warning vs action marks.** Warnings MAY target action marks. While actions are "facts" (Section 2.3), a warning targeting an action does not erase the fact - it signals that the fact is contested or its consequences are problematic. The action's base strength remains constant (P2), but its effective-strength-with-warnings may be temporarily reduced. This allows agents to express "this action happened, but something went wrong" without requiring supersession. If a deployment requires actions to be immune to warning invalidation, the guard SHOULD reject warnings where `invalidates` references an action mark.

Ref: [`markspace/core.py::effective_strength_with_warnings`](../markspace/core.py), [`tests/test_properties.py::TestWarningProperties`](../tests/test_properties.py)

### 9.6 Formal Properties

**P34: Invalidation Bound**: A warning MUST NOT reduce a mark's effective strength below 0.

**P35: Invalidation Decay**: As a warning decays, the invalidated mark's effective strength MUST recover (assuming the mark itself hasn't fully decayed).

**P36: Guard Determinism**: Given the same mark space state, `pre_action` MUST return the same verdict for the same inputs.

**P37: Guard Atomicity**: If `pre_action` returns CONFLICT or BLOCKED, `tool_fn` MUST NOT be called. The tool function is never invoked unless the guard explicitly allows it.

**P38: Guard Transparency**: The `GuardDecision` MUST contain sufficient information for the agent to reason about why it was blocked and choose an alternative. Specifically: a CONFLICT verdict MUST include `winning_intent` (the intent that won the resource); a BLOCKED verdict MUST include a non-empty `reason` string; a DENIED verdict MUST include `reason` specifying which authorization check failed.

**P39: Action Precedence**: If an action mark exists on a resource from agent X, the guard MUST return CONFLICT for any other agent's intent on that resource. Completed actions take precedence over new intents. Without this, intent-only conflict resolution would miss resources already claimed by executed actions.

Ref: [`markspace/guard.py::Guard`](../markspace/guard.py), [`tests/test_guard.py`](../tests/test_guard.py)

### 9.7 Statistical Envelope

The statistical envelope provides behavioral anomaly detection for agent write patterns. It tracks per-agent write rates using [Welford's online algorithm](https://en.wikipedia.org/wiki/Welford%27s_online_algorithm) ([Welford, 1962](https://doi.org/10.1007/BF02289263)) for streaming mean and variance, detecting three categories of anomaly: rate spikes, type distribution shifts, and cross-agent concentration.

**Detection algorithm.** The envelope divides time into tumbling windows (default 300 seconds). At each window boundary, the window's per-type counts are fed into Welford accumulators. Three checks run on every `check(agent)` call:

1. **Rate anomaly** (per-agent, per-type): Current window count exceeds `mean + k * stddev` (default k=3.5). Also checks boundary-straddling bursts where `current + previous > 2 * threshold`.
2. **Type shift** (per-agent): The fraction of mark types in the current window deviates from the historical distribution by more than `type_shift_threshold` (default 0.5).
3. **Concentration** (cross-agent): Three or more agents wrote to the same `(scope, topic)` in the current window.

**Verdicts.** `check(agent)` returns one of:

| Verdict | Trigger | Consequence |
|---------|---------|-------------|
| NORMAL | No anomaly detected | No action |
| FLAGGED | Concentration detected (cross-agent pattern) | Warning mark written; no barrier |
| RESTRICTED | Individual anomaly (rate spike or type shift) | Barrier created/narrowed on the flagged scope |

**Escalation policy.** When the guard receives a RESTRICTED verdict, it applies scope-scoped barrier narrowing via `_apply_restriction()`:

1. First flag on a scope: revoke the triggering mark type in that scope only.
2. Subsequent flags on different scopes: each narrows one additional scope.
3. After `global_escalation_threshold` total flags (default 3): require Need marks before any action in any scope.
4. Each restriction writes invalidation warnings on the agent's recent observations in the flagged scope, plus an audit Need mark for the principal.

**Declared baselines.** Agents can declare expected activity in their manifest via `expected_activity: dict[str, float]` (mark type value to expected marks per hour). The rate is window-independent - the envelope converts to per-window counts using `window_seconds`, so agent creators don't need to know the window size. When present, the envelope calls `detector.seed_baseline()` at registration time, pre-loading the detector with the declared baseline. This eliminates the cold-start blind spot for agents with known behavior - deviations from the declared baseline are flagged immediately. Real observations refine the seeded baseline over time. The synthetic variance is set to 25% of the mean squared (stddev = 0.5 * mean), so small natural deviations from the declared rates do not trigger false positives.

**Exempt agents.** The guard's system agent and the diagnostic probe agent (Section 9.9) are added to the envelope's `exempt_agents` set. Their writes are invisible to the envelope, preventing feedback loops where the guard's own warning/need marks trigger further anomalies. Exemption should be used sparingly - external bots and integrations should be monitored with their own baselines (using `expected_activity` declarations), not exempted.

**`write_mark()` as single enforcement boundary.** All agent writes pass through `Guard.write_mark()`, which checks the barrier and envelope before delegating to `space.write()`. Intent and Action marks are rejected by `write_mark()` with a ValueError - they flow through `pre_action()`/`post_action()` instead. This ensures every observation, warning, and need write is monitored.

**P40: Envelope Monotonicity**: If `check(agent)` returns RESTRICTED at time t, it MUST return RESTRICTED for all t' > t until `reset(agent, principal_token)` is called. The `restricted` flag is sticky.

**P41: Envelope Cold Start Safety**: `check(agent)` MUST return NORMAL for any agent whose detector reports `is_ready() == False`. For the default `WelfordDetector`, this means fewer than `min_samples` completed windows. New agents are not flagged while the baseline is being established.

**P42: Write-Mark Atomicity**: A `write_mark()` rejection (barrier check or envelope restriction) MUST prevent the mark from being stored. No partial writes.

**P43: Escalation Boundedness**: Automatic barrier escalation from envelope MUST NOT restrict more than one scope per envelope flag. Global restriction (require-need-all) MUST require `global_escalation_threshold` flags.

Ref: [`markspace/envelope.py::StatisticalEnvelope`](../markspace/envelope.py), [`tests/test_envelope.py`](../tests/test_envelope.py), [`tests/test_defense_integration.py`](../tests/test_defense_integration.py)

### 9.8 Absorbing Barrier

The absorbing barrier provides monotonic permission restriction for agents. It is a mutable overlay on the frozen Agent model - agents are immutable (their scope permissions are set at creation), but barriers track runtime revocations without modifying the agent.

**Barrier operations:**

```
narrow(scope, mark_type)           -- revoke one permission
narrow_all(mark_type)              -- revoke across all scopes (wildcard "*")
require_need(scope)                -- force Need marks before actions in scope
require_need_all()                 -- force Need marks before any action
restore(scope, mark_type, token)   -- re-grant (principal-only)
restore_all(token)                 -- clear all restrictions (principal-only)
```

**Monotonicity.** `narrow()` only adds to the revocation set. There is no `un-narrow()`. The only way to re-grant a revoked permission is `restore()` with the correct principal token. This makes barriers safe under concurrent access - the set of revocations can only grow, never shrink, without principal intervention.

**Wildcard support.** `narrow_all(mark_type)` stores `("*", mark_type)` in the revocation set. `is_allowed_checked()` checks both the exact `(scope, mark_type)` and the wildcard `("*", mark_type)` entries.

**Escalation counter.** Each barrier tracks a `flag_count` that increments on every envelope flag. The guard uses this counter to decide escalation level (scope-scoped narrowing vs global need requirement).

**P44: Barrier Monotonicity**: If `is_allowed(scope, mark_type)` returns False, it MUST return False for all subsequent calls until `restore(scope, mark_type, principal_token)` succeeds. Narrowing is irreversible without principal authentication.

**P45: Barrier Principal Exclusivity**: `restore()` MUST reject any token that does not match the barrier's `_principal_token`. Only the principal who created the barrier can lift restrictions.

Ref: [`markspace/barrier.py::AgentBarrier`](../markspace/barrier.py), [`tests/test_barrier.py`](../tests/test_barrier.py)

### 9.9 Diagnostic Probe

The diagnostic probe verifies agent health through canary injection and acknowledgment gating. It writes a synthetic observation mark to an isolated diagnostic scope; the harness reads this mark back from the space when building each agent's system prompt, and gates all tool calls on acknowledgment.

**Probe cycle:**

1. **Inject canary.** Write an observation mark with topic `probe-canary` to the diagnostic scope via `space.write()`. Returns the mark's UUID.
2. **Build prompt from space.** The harness reads the diagnostic scope for active canary marks and embeds the canary ID in the agent's system prompt. The canary originates from the mark space - not a separate parameter - matching production deployments where agent context is derived from shared state.
3. **Gate tool calls.** All tool calls except `acknowledge_canary` are blocked until the agent calls `acknowledge_canary` with the correct canary ID. An agent that does not acknowledge gets zero actions that round.
4. **Check visibility.** Verify the canary mark is readable from the space (sanity check on the space itself).

**Verdicts:**

| Verdict | Condition | Response |
|---------|-----------|----------|
| HEALTHY | Agent called `acknowledge_canary` with correct ID | No action |
| COMPROMISED | Agent was active but did not acknowledge | Blocking Need + barrier applied |

**Scope isolation.** The probe writes only to a dedicated diagnostic scope (default `"diagnostics"`). The probe agent's permissions are restricted to this scope - it has no write access to production scopes. This prevents probe infrastructure from polluting production mark data.

**Envelope exemption.** The probe agent is in the envelope's `exempt_agents` set. Probe writes (which can be frequent during health checks) do not contribute to the envelope's anomaly detection statistics.

**Auto-restriction on COMPROMISED.** A COMPROMISED verdict means the agent's system prompt was overridden - it ignored a trusted-channel instruction. The probe writes a blocking Need for principal review and the guard applies a global barrier: observation and action permissions revoked, only Need marks remain writable until the principal restores access. Single miss, immediate lockdown - no grace period, because a compromised agent that gets even one action can cause harm.

**P46: Probe Mark Isolation**: Probe-injected marks MUST be writable only to the configured diagnostic scope. The probe agent MUST NOT have write access to production scopes.

Ref: [`markspace/probe.py::DiagnosticProbe`](../markspace/probe.py), [`tests/test_probe.py`](../tests/test_probe.py)

### 9.10 Token Budgets

An agent MAY carry a `TokenBudget` in its manifest that limits token consumption. All fields are optional - omitted fields mean no limit. The guard tracks cumulative usage and enforces limits at activation time.

```protobuf
message TokenBudget {
  optional uint64 max_input_tokens_per_round  = 1;  // read budget per activation
  optional uint64 max_output_tokens_per_round = 2;  // generation budget per activation
  optional uint64 max_input_tokens_total      = 3;  // lifetime input budget
  optional uint64 max_output_tokens_total     = 4;  // lifetime output budget
  double warning_fraction = 5;                       // default 4/5; see derivation below
}

message AgentManifest {
  // ... existing fields ...
  optional TokenBudget budget = 5;
}
```

**Enforcement rules:**

1. **Per-round input budget.** When `max_input_tokens_per_round` is set, the mark space returns marks ranked by effective strength (which incorporates recency through decay) and truncates at the token limit.
2. **Per-round output budget.** When `max_output_tokens_per_round` is set, the LLM call is configured with a matching `max_tokens` parameter.
3. **Lifetime tracking.** The guard maintains running totals for input and output tokens across all rounds. Usage is monotonically non-decreasing (P63).
4. **Warning at threshold.** When lifetime usage crosses `warning_fraction` of the total budget in either dimension, the guard writes a non-blocking Need mark (`blocking=False`). Exactly one warning per dimension (P60). The structurally correct value is `warning_fraction = 1 - (R * T / B)`, where R is the expected rounds before the principal responds, T is tokens per round, and B is the total budget. Default 4/5 assumes approximately one round of runway.
5. **Hard stop at limit.** When lifetime usage reaches the total budget in either dimension, the guard rejects all further activations (P61). The agent's existing marks persist.
6. **Principal resumption.** The principal MAY increase the budget by updating the manifest. If the new total exceeds consumption, the agent resumes (P62).

Input and output budgets are separate because they have different cost profiles. Input tokens are the dominant cost factor and are driven by mark space size. Output tokens reflect agent verbosity and model choice. The protocol controls tokens - the measurable quantity at the guard boundary. Cost depends on model pricing, which varies across providers and changes over time. Operators derive cost externally from telemetry using their own pricing tables.

**P57: Telemetry Non-Interference**: Telemetry emission MUST NOT affect guard verdicts, mark storage, or coordination semantics. A telemetry sink failure MUST NOT block writes or reads.

**P58: Telemetry Completeness**: Every `write_mark()` and `execute()` call that passes through the guard MUST emit a structured log event, regardless of whether the operation was accepted or rejected.

**P59: Budget Backward Compatibility**: An AgentManifest with no `budget` field MUST behave identically to the pre-budget protocol. No enforcement, no tracking.

**P60: Budget Warning Threshold**: When lifetime token usage crosses `warning_fraction` of the total budget in any dimension, the guard MUST write exactly one non-blocking Need mark for that dimension. The Need mark MUST have `blocking=False`.

**P61: Budget Hard Stop**: When lifetime token usage reaches or exceeds the total budget in any dimension, the guard MUST reject all further activations for that agent. Once stopped, the agent stays stopped until the principal increases the budget.

**P62: Budget Resumption**: A principal MAY increase an agent's budget by updating the manifest. After a budget increase that makes the new total exceed cumulative usage, the agent MUST be eligible for activation again.

**P63: Budget Tracking Accuracy**: The guard's cumulative token tracking MUST be monotonically non-decreasing. Token usage once recorded MUST NOT be decremented. A principal increases the budget ceiling, not resets consumption - the agent resumes with its full history intact.

Ref: [`markspace/budget.py`](../markspace/budget.py), [`markspace/guard.py::record_round_tokens`](../markspace/guard.py), [`tests/test_budget.py`](../tests/test_budget.py)

### 9.11 Telemetry

The guard MAY emit structured telemetry on every decision. The telemetry interface is [OpenTelemetry](https://opentelemetry.io/)-compatible: metrics as instruments, structured logs as log records, optional trace context propagation.

Telemetry goes to a configurable sink - not into the mark space. Marks are coordination signals between agents; telemetry is operational data for humans.

**Metrics:**

| Metric | Type | Labels |
|--------|------|--------|
| `markspace.marks.written` | Counter | `agent_id`, `scope`, `mark_type`, `verdict` |
| `markspace.marks.read` | Counter | `agent_id`, `scope` |
| `markspace.tokens.input` | Counter | `agent_id` |
| `markspace.tokens.output` | Counter | `agent_id` |
| `markspace.conflicts.resolved` | Counter | `scope`, `policy`, `outcome` |
| `markspace.space.active_marks` | Gauge | `scope`, `mark_type` |
| `markspace.space.total_marks` | Gauge | `scope`, `mark_type` |
| `markspace.agent.budget.remaining` | Gauge | `agent_id`, `dimension` |
| `markspace.agent.round.duration` | Histogram | `agent_id` |
| `markspace.needs.pending` | Gauge | `scope` |

**Sink implementations:** `NullSink` (no-op, default), `StructuredLogSink` (JSON via Python logging), `InMemorySink` (testing), `FailingSink` (P57 verification). Deployments MAY provide custom sinks (e.g., OTel SDK wrapper).

Ref: [`markspace/telemetry.py`](../markspace/telemetry.py), [`tests/test_telemetry.py`](../tests/test_telemetry.py)

### 9.12 Scope Rate Limits

A scope MAY define per-agent and fleet-wide write rate limits. The guard enforces these at write time, independently of the statistical envelope and token budgets (P66).

```protobuf
message ScopeRateLimit {
  optional uint32 max_writes_per_agent_per_window = 1;
  optional uint32 max_total_writes_per_window = 2;
  double window_seconds = 3;  // MUST be > 0
}
```

The `rate_limit` field is added to the Scope definition. When omitted, no rate limiting applies.

**P64: Rate Limit Enforcement**: When a scope has a rate limit and an agent exceeds `max_writes_per_agent_per_window`, the write MUST be rejected. The rejection MUST be visible to the envelope via `record_attempt()`.

**P65: Rate Limit Fleet Cap**: When total writes to a scope exceed `max_total_writes_per_window`, ALL further writes MUST be rejected until the window rotates, regardless of per-agent usage.

**P66: Rate Limit Independence**: Rate limits operate independently of the statistical envelope and token budgets. An agent within its rate limit but flagged by the envelope is still subject to envelope restrictions; an agent within its envelope baseline but exceeding its rate limit is still rejected.

Ref: [`markspace/rate_limit.py`](../markspace/rate_limit.py), [`markspace/guard.py::_check_rate_limit`](../markspace/guard.py), [`tests/test_rate_limit.py`](../tests/test_rate_limit.py)

## 10. Generalized Supersession

All mark types MAY carry a `supersedes` field. An observation can supersede a prior observation on the same topic. An intent can supersede the same agent's prior intent on the same resource. This provides explicit versioning alongside continuous decay.

Any mark type MAY include an `optional string supersedes` field. When set, the referenced mark becomes invisible:

- A superseded mark's effective strength is 0 on all subsequent reads.
- The superseding mark's own lifecycle (decay, TTL) applies normally.
- Supersession chains: if A supersedes B, and C supersedes A, then both A and B are invisible.

**When to use supersession vs warnings**: Supersession replaces a mark quietly (the old version vanishes). Warnings loudly declare that something is wrong (the invalidation is itself a mark other agents can read). Use supersession for routine updates ("new price observation"), warnings for notable corrections ("previous data was wrong").

### 10.1 Formal Properties

**P47: Supersession Transitivity**: If mark C supersedes B, and B supersedes A, then A, B are both invisible and C is the only visible mark.

Ref: [`tests/test_guard.py::TestGeneralizedSupersession`](../tests/test_guard.py)

## 11. Agent

An agent is an identity with scope permissions and local rules. The protocol does not specify what agents do internally, only how they interact with the mark space.

### 11.1 Agent Definition

```protobuf
message ScopePermission {
  string            scope      = 1;  // scope name (hierarchical matching)
  repeated MarkType mark_types = 2;  // which mark types this agent can write
}

message Agent {
  string                   id          = 1;  // UUID
  string                   name        = 2;
  repeated ScopePermission permissions = 3;  // write permissions
  repeated string          read_scopes = 4;  // scopes with full content read access
  optional AgentManifest   manifest    = 5;  // composition contract ([Section 13](#13-agent-composition))
}
```

`read_scopes` controls content access for PROTECTED and CLASSIFIED scopes ([Section 7.4](#74-scope-visibility)). For OPEN scopes, `read_scopes` is irrelevant. Both `permissions` and `read_scopes` support hierarchical matching.

### 11.2 Agent-Local Rules

The protocol does not prescribe agent-internal logic. An agent's behavior is defined by:

1. **Which marks it reads**: filtered by scope, topic, resource.
2. **How it reacts**: implementation-defined. The spec only constrains interactions with the mark space (authorization, conflict resolution).
3. **Which marks it writes**: constrained by its permissions.

The decomposability guarantee follows from this. You can test an agent in isolation by mocking the mark space. You can add new agents without changing existing ones. The mark space is the only coupling point.

### 11.3 Agent Manifests

An agent MAY carry an `AgentManifest` that declares its input/output contract. The manifest makes the agent's interface explicit - what marks it reads and what marks it writes - enabling composition validation without runtime state. See [Section 13](#13-agent-composition) for details.

## 12. Reference Implementation

The reference implementation is in Python (3.11+), with pydantic and httpx as runtime dependencies. It is structured as:

```
markspace/
  core.py          -- types, decay, trust, reinforcement, conflict resolution
  space.py         -- MarkSpace (stateful read/write/query, watch/subscribe, write hooks)
  guard.py         -- Guard (deterministic enforcement layer, write_mark, barrier/envelope wiring)
  envelope.py      -- StatisticalEnvelope (pluggable per-agent anomaly detection)
  barrier.py       -- AgentBarrier (monotonic permission restriction)
  probe.py         -- DiagnosticProbe (canary injection and response checking)
  compose.py       -- composition validation (pipeline, manifest-permission checks)
  schedule.py      -- mark-driven scheduling (reads configs, writes activations)
  llm.py           -- provider-agnostic LLM client (OpenAI-compatible)
  models.py        -- model registry
  __init__.py      -- DSL re-exports

tests/
  test_properties.py         -- property tests (P1-P13, P19-P35)
  test_guard.py              -- guard enforcement, supersession, deferred resolution (P14-P16, P36-P47)
  test_envelope.py           -- envelope property tests (P40-P41, P42, P43)
  test_barrier.py            -- barrier property tests (P44-P45)
  test_probe.py              -- probe property tests (P46)
  test_defense_integration.py -- defense-in-depth integration tests
  test_composition.py        -- composition property tests (P48-P55)
  test_schedule.py           -- scheduling property tests (P56)
  test_scenarios.py          -- end-to-end coordination scenarios
  test_concurrent.py         -- thread-safety tests
  test_hypothesis.py         -- hypothesis property-based tests with randomized inputs
```

### 12.1 DSL Usage

```python
from markspace import (
    Action,
    Agent,
    ConflictPolicy,
    DecayConfig,
    Intent,
    MarkSpace,
    Need,
    Observation,
    Scope,
    Source,
    Warning,
    hours,
    minutes,
)

# Define a scope
calendar = Scope(
    name="calendar",
    allowed_intent_verbs=["book", "reschedule", "cancel"],
    allowed_action_verbs=["booked", "rescheduled", "cancelled"],
    decay=DecayConfig(
        observation_half_life=hours(1),
        warning_half_life=hours(4),
        intent_ttl=minutes(30),
    ),
    conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
)

# Define the environment with the scope registered
space = MarkSpace(scopes=[calendar])

# Define agents
booker = Agent(
    name="flight-booker",
    scopes={"calendar": ["intent", "action"]},
)
optimizer = Agent(
    name="calendar-opt",
    scopes={"calendar": ["intent", "action", "observation"]},
)

# Agent writes an intent
intent_id = space.write(
    booker,
    Intent(
        scope="calendar",
        resource="thu-14:00",
        action="book",
        confidence=0.9,
    ),
)

# Another agent reads, sees the intent, applies local rules
marks = space.read(scope="calendar", resource="thu-14:00")
# → [Intent(agent=booker, action="book", confidence=0.9, strength=1.0)]

# Agent executes and writes action
action_id = space.write(
    booker,
    Action(
        scope="calendar",
        resource="thu-14:00",
        action="booked",
        result={"flight": "DL413", "departure": "14:30"},
        supersedes=intent_id,
    ),
)

# Other agents see the action, update their world model
marks = space.read(scope="calendar", resource="thu-14:00")
# → [Action(agent=booker, action="booked", result={...}, strength=1.0)]
# Intent is gone (superseded)
```

## 13. Agent Composition

The protocol follows the Unix philosophy: small agents that do one thing well, composed through the shared mark space. Marks are the universal interface (like stdin/stdout), the mark space is the composition mechanism (like pipes), and scopes are the namespaces (like the filesystem). Agents don't know about each other - they read from the mark space and write to it. The principal composes them by defining scopes and permissions.

This section defines two mechanisms that make composition explicit and reactive: **watch patterns** for declaring interest in marks, and **agent manifests** for declaring input/output contracts.

### 13.1 Watch Patterns

A watch pattern describes a set of marks an agent is interested in.

```protobuf
message WatchPattern {
  string              scope     = 1;  // required. hierarchical matching (P23)
  optional MarkType   mark_type = 2;  // optional filter
  optional string     topic     = 3;  // optional filter (observations, warnings)
  optional string     resource  = 4;  // optional filter (intents, actions)
}
```

Pattern matching: a mark matches a WatchPattern if all specified fields match. Unspecified optional fields match any value. Scope matching is hierarchical: a pattern with scope "sensors" matches marks in "sensors", "sensors/temperature", "sensors/pressure", etc.

### 13.2 Agent Manifests

An agent manifest declares the agent's input/output contract.

```protobuf
message ManifestOutput {
  string   scope     = 1;
  MarkType mark_type = 2;
}

message AgentManifest {
  repeated WatchPattern  inputs            = 1;  // marks this agent reads (triggers)
  repeated ManifestOutput outputs          = 2;  // marks this agent writes
  optional double        schedule_interval = 3;  // seconds between activations ([Section 14](#14-scheduling))
  map<string, double>    expected_activity = 4;  // MarkType.value -> expected marks per hour ([Section 9.7](#97-statistical-envelope))
  optional TokenBudget   budget            = 5;  // token budget ([Section 9.10](#910-token-budgets))
}
```

The manifest is purely declarative. It does not execute anything. It is used by:
- **Composition validation**: check that a sequence of agents forms a connected pipeline.
- **Permission validation**: check that declared outputs are consistent with the agent's write permissions.
- **Anomaly detection**: `expected_activity` pre-seeds the statistical envelope's baseline, eliminating the cold-start blind spot for agents with known behavior ([Section 9.7](#97-statistical-envelope)).
- **Documentation**: understand what an agent does by reading its manifest, without reading its code.

### 13.3 Subscription

An agent registers interest in mark patterns via `subscribe()`.

```
subscribe(agent, patterns: list[WatchPattern]) -> void

Preconditions:
  - agent is a valid Agent
  - patterns is a non-empty list of WatchPatterns

Postconditions:
  - Agent's subscription is set to the given patterns (P48: replaces any prior subscription)
  - Marks written after this call that match any pattern will be queued for the agent (P49: not retroactive)
```

```
unsubscribe(agent) -> void

Postconditions:
  - Agent's subscription and pending queue are removed
  - No further marks are queued for this agent
```

### 13.4 Watched Marks

An agent retrieves queued marks via polling.

```
get_watched_marks(agent, clear=true) -> list[Mark]

Postconditions:
  - Returns marks written since the last poll that match the agent's subscription patterns (P50)
  - If clear=true: the queue is emptied; the same marks will not be returned again (P51)
  - If clear=false: the queue is preserved for re-reading
  - Marks are returned in write order (P52)
```

Agents are not notified about their own writes. If agent A writes a mark and agent A is subscribed to a matching pattern, the mark is not queued for A.

### 13.5 Composition Validation

Two validation functions operate on manifests without runtime state.

**Pipeline validation**: given a sequence of agents `[A, B, C]`, check that A's outputs can feed B's inputs, and B's outputs can feed C's inputs. A connection exists when at least one output `(scope, mark_type)` matches an input WatchPattern (scope match, mark_type match or wildcard).

**Manifest-permission validation**: check that every output `(scope, mark_type)` in an agent's manifest is covered by the agent's write permissions. This is a static check - if it fails, the agent's manifest promises something it cannot deliver.

### 13.6 Formal Properties

**P48: Subscription Idempotency**: Calling `subscribe(agent, patterns)` MUST replace any previous subscription for that agent. Re-subscribing MUST NOT cause duplicate deliveries.

**P49: Subscription Prospective**: `subscribe()` MUST NOT retroactively deliver marks written before the subscription. Only marks written after `subscribe()` are candidates for delivery.

**P50: Watch Subset**: `get_watched_marks()` MUST return only marks that match at least one of the agent's subscribed patterns. Marks that do not match any pattern MUST NOT be delivered.

**P51: At-Most-Once Delivery**: When `get_watched_marks(agent, clear=true)` is called, each mark MUST be delivered at most once. Subsequent calls MUST NOT return the same marks.

**P52: Write-Order Delivery**: Marks returned by `get_watched_marks()` MUST be ordered by write time (the order in which `write()` was called). Within a single poll, earlier writes appear before later writes.

**P53: Pipeline Structural Validation**: Pipeline validation MUST depend only on agent manifests, not on runtime mark space state. It is a pure function of the agents' declared inputs and outputs.

**P54: Manifest-Permission Consistency**: Every output declared in an agent's manifest MUST be a subset of the agent's write permissions. An agent MUST NOT declare an output it is not authorized to produce.

**P55: Pattern Match Purity**: `WatchPattern.matches(mark)` MUST be a pure function. It MUST NOT modify the mark, the pattern, or any external state. *Note: trivially true by construction (the implementation is a field comparison), but stated to prevent implementations from introducing stateful matching (e.g., rate limiting or deduplication inside the matcher).*

Ref: [`markspace/core.py::WatchPattern, AgentManifest`](../markspace/core.py), [`markspace/space.py::subscribe, get_watched_marks`](../markspace/space.py), [`markspace/compose.py`](../markspace/compose.py), [`tests/test_composition.py`](../tests/test_composition.py)

**Note on source agents.** The subscription mechanism activates downstream agents reactively when upstream marks appear. Source agents - those at the start of a pipeline with no upstream marks - need a different trigger. [Section 14 (Scheduling)](#14-scheduling) provides this through the agent manifest: the principal sets `schedule_interval` on the agent's manifest, and the Scheduler infrastructure determines when the agent is due for activation. No marks are involved - scheduling is a property of the agent, not a signal in the environment.


## 14. Scheduling

Downstream agents activate reactively through subscription ([Section 13.3](#133-subscription)). Source agents - agents with no upstream marks - need an external trigger. Scheduling provides this trigger through the agent manifest: the principal sets `schedule_interval` when creating or configuring an agent, and the Scheduler infrastructure determines when each agent is due.

### 14.1 Schedule Configuration

A schedule is a property of the agent manifest. The principal sets `schedule_interval` (in seconds) when creating or updating an agent:

```python
agent = Agent(
    name="weather-poller",
    scopes={"weather": ["observation"]},
    manifest=AgentManifest(
        outputs=(("weather", MarkType.OBSERVATION),),
        schedule_interval=minutes(5),  # principal sets this
    ),
)
```

- `schedule_interval` (float, > 0, or None): seconds between activations.
- The principal can change the schedule by creating a new agent configuration with a different interval.
- Agents without `schedule_interval` (or with None) are not scheduled.

### 14.2 Scheduler

The Scheduler is infrastructure, like the Guard. It reads agent manifests and tracks activation timing. No marks are involved - scheduling is a property of the agent, not a signal in the environment.

**`register(agent)`**: Register an agent for scheduling. Reads `schedule_interval` from the agent's manifest. Agents without a manifest or interval are ignored.

**`due()`**: Return agents whose `schedule_interval` has elapsed since their last activation. Newly registered agents (never activated) are immediately due.

**`tick_all()`**: Return due agents and mark them all as activated. Convenience method combining `due()` + `mark_activated()`.

**`update(agent)`**: Update an existing schedule with a new manifest. Preserves last activation time so the agent is not prematurely re-triggered.

**`start(poll_interval, on_due=None)` / `stop()`**: Background thread that calls `tick_all()` periodically. The optional `on_due` callback is invoked with the list of due agents each tick. The timer is the only non-deterministic component; all other methods are testable without real time.

### 14.3 Formal Properties

**P56: Schedule Interval**: For each scheduled agent, the minimum time between consecutive activations MUST be at least `schedule_interval`. If activation A occurs at time `t1` and the next activation B occurs at time `t2`, then `t2 - t1 >= schedule_interval`.

Ref: [`markspace/schedule.py`](../markspace/schedule.py), [`tests/test_schedule.py`](../tests/test_schedule.py)


## 15. Properties Summary

All normative properties, collected. A conforming implementation MUST satisfy P1-P39 and P47. Properties P40-P45 (adaptive layer), P46 (diagnostic probe), P48-P55 (composition), P56 (scheduling), P57-P58 (telemetry), P59-P63 (token budgets), and P64-P66 (scope rate limits) are OPTIONAL - required only if the implementation supports that feature. The reference implementation's test suite verifies each one.

Properties are numbered sequentially by section.

| ID | Property | Section |
|----|----------|---------|
| | **Decay** | |
| P1 | Decay Monotonicity | 3.2 |
| P2 | Action Permanence | 3.2 |
| P3 | Convergence | 3.2 |
| P4 | Intent Expiry | 3.2 |
| P5 | Need Persistence | 3.2 |
| | **Trust** | |
| P6 | Trust Ordering | 4.3 |
| P7 | Trust Bounds | 4.3 |
| | **Reinforcement** | |
| P8 | Sublinearity | 5.3 |
| P9 | Boundedness | 5.3 |
| P10 | Monotonic Addition | 5.3 |
| | **Conflict Resolution** | |
| P11 | Determinism | 6.4 |
| P12 | Progress | 6.4 |
| P13 | Consistency (Yield Recovery) | 6.4 |
| P14 | Deferred Completeness | 6.4 |
| P15 | Deferred Priority Fidelity | 6.4 |
| P16 | Deferred Liveness | 6.4 |
| P17 | Deferred Mutual Exclusion | 6.4 |
| P18 | Deferred Inclusion | 6.4 |
| | **Scope & Visibility** | |
| P19 | Scope Isolation | 7.6 |
| P20 | Structural Visibility | 7.6 |
| P21 | Content Access | 7.6 |
| P22 | Classified Opacity | 7.6 |
| P23 | Hierarchy | 7.6 |
| P24 | Projection Preservation | 7.6 |
| P25 | Classified No Fallback | 7.6 |
| P26 | Visibility Hierarchy Inheritance | 7.6 |
| | **Mark Space** | |
| P27 | Write Visibility | 8.5 |
| P28 | Read Purity | 8.5 |
| P29 | Resolution Immediacy | 8.5 |
| P30 | Mark Immutability | 8.5 |
| P31 | Mark ID Uniqueness | 8.5 |
| P32 | Total Write Ordering | 8.5 |
| P33 | Hook Non-Interference | 8.6 |
| | **Guard** | |
| P34 | Invalidation Bound | 9.6 |
| P35 | Invalidation Decay | 9.6 |
| P36 | Guard Determinism | 9.6 |
| P37 | Guard Atomicity | 9.6 |
| P38 | Guard Transparency | 9.6 |
| P39 | Action Precedence | 9.6 |
| | **Statistical Envelope** (optional) | |
| P40 | Envelope Monotonicity | 9.7 |
| P41 | Envelope Cold Start Safety | 9.7 |
| P42 | Write-Mark Atomicity | 9.7 |
| P43 | Escalation Boundedness | 9.7 |
| | **Absorbing Barrier** (optional) | |
| P44 | Barrier Monotonicity | 9.8 |
| P45 | Barrier Principal Exclusivity | 9.8 |
| | **Diagnostic Probe** (optional) | |
| P46 | Probe Mark Isolation | 9.9 |
| | **Supersession** | |
| P47 | Supersession Transitivity | 10.1 |
| | **Composition** (optional) | |
| P48 | Subscription Idempotency | 13.6 |
| P49 | Subscription Prospective | 13.6 |
| P50 | Watch Subset | 13.6 |
| P51 | At-Most-Once Delivery | 13.6 |
| P52 | Write-Order Delivery | 13.6 |
| P53 | Pipeline Structural Validation | 13.6 |
| P54 | Manifest-Permission Consistency | 13.6 |
| P55 | Pattern Match Purity | 13.6 |
| | **Scheduling** (optional) | |
| P56 | Schedule Interval | 14.3 |
| | **Telemetry** (optional) | |
| P57 | Telemetry Non-Interference | 9.11 |
| P58 | Telemetry Completeness | 9.11 |
| | **Token Budgets** (optional) | |
| P59 | Budget Backward Compatibility | 9.10 |
| P60 | Budget Warning Threshold | 9.10 |
| P61 | Budget Hard Stop | 9.10 |
| P62 | Budget Resumption | 9.10 |
| P63 | Budget Tracking Accuracy | 9.10 |
| | **Scope Rate Limits** (optional) | |
| P64 | Rate Limit Enforcement | 9.12 |
| P65 | Rate Limit Fleet Cap | 9.12 |
| P66 | Rate Limit Independence | 9.12 |

## 16. Conformance

An implementation is conformant if:

1. It supports all five mark types with the required fields ([Section 2](#2-mark-types)).
2. It computes strength at read time according to the decay rules ([Section 3](#3-decay)).
3. It applies trust weighting that preserves the source total order ([Section 4](#4-trust)).
4. It implements sublinear, bounded reinforcement ([Section 5](#5-reinforcement)).
5. It resolves intent conflicts deterministically according to the scope's policy ([Section 6](#6-conflict-resolution)), including deferred resolution for HIGHEST_CONFIDENCE under lock-based serialization ([Section 6.2](#62-deferred-resolution)).
6. It enforces scope-based write authorization ([Section 7](#7-scope)).
7. It provides a deterministic guard layer that enforces coordination without depending on agent compliance ([Section 9](#9-guard-deterministic-enforcement-layer)).
8. It supports generalized supersession across all mark types ([Section 10](#10-generalized-supersession)).
9. It enforces scope visibility (OPEN/PROTECTED/CLASSIFIED) with projected reads and content redaction ([Section 7.4](#74-scope-visibility)).
10. It supports post-write hooks ([Section 8.6](#86-write-hooks)).
11. It satisfies all mandatory formal properties P1-P39 and P47 ([Section 15](#15-properties-summary)).

An implementation MAY differ from the reference implementation in storage backend, concurrency model, garbage collection strategy, or internal data structures, provided the properties hold.

An implementation MAY additionally support the adaptive monitoring layer: statistical envelope ([Section 9.7](#97-statistical-envelope)) and absorbing barrier ([Section 9.8](#98-absorbing-barrier)). If it does, it MUST satisfy P40-P45. Adaptive layer support is OPTIONAL for conformance.

An implementation MAY additionally support diagnostic probes ([Section 9.9](#99-diagnostic-probe)). If it does, it MUST satisfy P46. Probe support is OPTIONAL for conformance.

An implementation MAY additionally support telemetry ([Section 9.11](#911-telemetry)). If it does, it MUST satisfy P57-P58. Telemetry support is OPTIONAL for conformance.

An implementation MAY additionally support token budgets ([Section 9.10](#910-token-budgets)). If it does, it MUST satisfy P59-P63. Token budget support is OPTIONAL for conformance.

An implementation MAY additionally support scope rate limits ([Section 9.12](#912-scope-rate-limits)). If it does, it MUST satisfy P64-P66. Rate limit support is OPTIONAL for conformance.

An implementation MAY additionally support agent composition ([Section 13](#13-agent-composition)). If it does, it MUST satisfy P48-P55. Composition support is OPTIONAL for conformance.

An implementation MAY additionally support manifest-based scheduling ([Section 14](#14-scheduling)). If it does, it MUST satisfy P56. Scheduling support is OPTIONAL for conformance.
