# Stigmergic Coordination Protocol: Specification

**Version**: 0.1.0-draft
**Status**: Working draft
**Date**: 2026-02-26

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
- [9. Guard](#9-guard-deterministic-enforcement-layer)
  - [9.1 Principle](#91-principle)
  - [9.2 Guard Operations](#92-guard-operations)
  - [9.3 GuardDecision](#93-guarddecision)
  - [9.4 What the Agent Still Does](#94-what-the-agent-still-does)
  - [9.5 Warning Invalidation](#95-warning-invalidation)
  - [9.6 Formal Properties](#96-formal-properties)
- [10. Generalized Supersession](#10-generalized-supersession)
  - [10.1 Formal Properties](#101-formal-properties)
- [11. Agent](#11-agent)
  - [11.1 Agent Definition](#111-agent-definition)
  - [11.2 Agent-Local Rules](#112-agent-local-rules)
- [12. Properties Summary](#12-properties-summary)
- [13. Reference Implementation](#13-reference-implementation)
  - [13.1 DSL Usage](#131-dsl-usage)
- [14. Conformance](#14-conformance)

## Abstract

This document specifies a coordination protocol for autonomous agent fleets based on stigmergy (indirect coordination through marks left in a shared environment). The protocol defines five mark types, their lifecycle semantics, and the operations agents perform on them. A conforming implementation enables N agents to coordinate without direct communication, central scheduling, or consensus protocols.

The specification is accompanied by a Python reference implementation and property tests. Every normative statement (MUST, SHOULD, MAY) maps to an executable test. The reference implementation demonstrates that these properties hold together consistently; any language can reimplement it by satisfying the same test suite.

**Verification level.** The 32 properties are verified empirically through Python property-based tests (pytest + [Hypothesis](https://github.com/HypothesisWorks/hypothesis)), not through formal model checking (e.g., [MCMAS](https://doi.org/10.1007/s10009-015-0378-x), [PRISM](https://doi.org/10.1007/978-3-642-22110-1_47), or [TLA+](https://lamport.azurewebsites.net/tla/tla.html)). The tests exercise each property across randomized inputs and edge cases, and the stress test validates them under realistic concurrent load (105 agents, 10 rounds). This provides high confidence that the properties hold in practice but does not constitute a formal proof. A formal verification effort would strengthen the safety guarantees, particularly for P11 (Determinism) and P12 (Progress), which make claims about all possible states.

## 1. Terminology

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in RFC 2119.

**Mark**: A typed record written to a shared space by an agent. The fundamental unit of coordination.

**Mark Space**: The shared environment where marks are stored. Agents interact only through the mark space, never directly with each other.

**Agent**: An autonomous process with an identity and a set of scope permissions. Agents read and write marks. Agents do not communicate with other agents.

**Scope**: A namespace that defines what kinds of marks can exist within it, their decay parameters, and conflict resolution rules.

**Strength**: A non-negative real number representing a mark's current influence. Computed at read time from the mark's initial strength, age, decay function, and trust source.

**Principal**: The human (or system) that agents serve. The principal interacts with the system through decision marks.

**Freshness**: The time-dependent component of strength. All marks except actions and unresolved needs decay over time.

## 2. Mark Types

There are exactly five mark types. An implementation MUST support all five. An implementation MUST NOT define additional mark types at the protocol level. (Domain-specific semantics are expressed through scope definitions, not new mark types.)

### 2.1 Common Fields

Every mark MUST contain:

```protobuf
enum MarkType {
  INTENT      = 0;
  ACTION      = 1;
  OBSERVATION = 2;
  WARNING     = 3;
  NEED        = 4;
}

message Mark {
  string   id         = 1;  // UUID, assigned by the mark space on write
  MarkType mark_type  = 2;
  string   agent_id   = 3;  // UUID
  string   scope      = 4;  // scope namespace this mark belongs to
  double   created_at = 5;  // unix timestamp
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
  optional string          supersedes = 5;  // MAY: UUID of a prior mark this replaces
}
```

**Lifecycle**:
- Action marks MUST NOT decay. Their strength is constant for all time.
- Action marks are historical facts. They are the ground truth of the system.
- An action mark MAY supersede a prior mark (action or intent). A superseded mark's effective strength becomes 0.

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
  string                 question    = 2;   // MUST: what decision is needed
  google.protobuf.Struct context     = 3;   // MUST: information relevant to the decision
  float                  priority    = 4;   // MUST: [0.0, 1.0] urgency
  bool                   blocking    = 5;   // MUST: whether the agent is blocked waiting
  optional string        resolved_by = 6;   // MAY: UUID of the decision mark that resolved this
}
```

**Lifecycle**:
- Unresolved need marks (resolved_by is None) MUST NOT decay. They persist at full strength indefinitely.
- Resolved need marks (resolved_by is set) MUST immediately have strength 0.
- Need marks are resolved when the principal writes a decision mark (an action mark scoped to the need).

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
| `action` | Constant. `strength(t) = initial_strength` for all t. |
| `observation` | Exponential. `strength(t) = initial * 0.5^(age / half_life)` |
| `warning` | Exponential. Same formula, with the scope's `warning_half_life`. |
| `need` | Full strength while unresolved. 0 when resolved. Step function. |

Ref: [`markspace/core.py::compute_strength`](../markspace/core.py)

### 3.2 Formal Properties

**P1: Decay Monotonicity**: For observation and warning marks, strength MUST be a monotonically non-increasing function of time. For all `t2 > t1`: `strength(mark, t2) <= strength(mark, t1)`.

**P2: Action Permanence**: Action mark strength MUST be constant. For all `t1, t2`: `strength(action_mark, t1) == strength(action_mark, t2)`.

**P3: Convergence**: Given no new marks, the total strength of all transient marks (observations + warnings) in the space MUST converge to 0 as t → ∞.

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

When multiple marks exist on the same scope + topic, they combine to produce an aggregate signal.

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

Lock-based guards (Section 9) serialize access: at most one agent enters `pre_action` at a time per scope. Under serialization, HIGHEST_CONFIDENCE degenerates to FIRST_WRITER because the second agent finds an already-committed action, not a competing intent. The guard returns CONFLICT (resource taken), never performing the confidence comparison. This is [priority inversion](https://www.cs.cornell.edu/courses/cs614/1999sp/papers/pathfinder.html): a low-priority agent holding the lock blocks a higher-priority agent.

Deferred resolution fixes this by separating claim collection from allocation.

#### 6.2.1 Protocol

The deferred resolution protocol has three phases:

**Phase 1: Claim collection.** Agents write intent marks to the mark space. The guard does NOT acquire a lock during this phase; intents are written freely. Each intent carries the agent's confidence (priority) for the resource.

**Phase 2: Resolution boundary.** A resolution boundary is triggered by an external event: end of a scheduling round, a timer, a principal action, or an explicit `resolve_deferred(scope, resource)` call. The trigger mechanism is deployment-defined; the spec only requires that a boundary eventually occurs for any scope with pending intents.

**Phase 3: Batch resolution.** At the boundary, the guard:

1. Collects all active intent marks on `(scope, resource)` with strength > 0.
2. Applies the scope's conflict policy to the full set (not pairwise). For HIGHEST_CONFIDENCE, the intent with the highest confidence wins; ties broken by `created_at`.
3. The winning agent's intent is converted to an action mark (or the agent is notified to proceed).
4. All losing agents receive BLOCKED verdicts. Losing intents remain in the mark space until TTL expiry (they are not forcibly removed).

```
Timeline:

  Agent A writes Intent(resource=R, confidence=0.5)     ──┐
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

### 6.3 Post-Action Resolution

If two agents both write action marks on the same resource (race condition where both executed before reading the other's intent), the **later action supersedes the earlier one**. The agent whose action was superseded SHOULD be notified via a warning mark. Intent marks are the primary conflict prevention mechanism; post-action resolution is the fallback.

### 6.4 Formal Properties

**P11: Determinism**: Given the same set of intent marks, all agents MUST reach the same conclusion about which intent wins. The resolution function is pure: `resolve(intents) → winner_id`.

**P12: Progress**: At least one agent MUST be able to proceed. The protocol MUST NOT deadlock (no circular yielding).

**P13: Consistency**: If agent A yields to agent B's intent, and agent B later abandons (intent expires), agent A MAY re-enter its intent on the next read cycle.

**P30: Deferred Completeness**: Under deferred resolution, the batch resolution step MUST consider ALL active intents on `(scope, resource)` at the resolution boundary. An intent written before the boundary and still within its TTL MUST NOT be excluded from the comparison.

**P31: Deferred Priority Fidelity**: Under deferred resolution with HIGHEST_CONFIDENCE, the winning intent MUST be the one with the highest confidence among all candidates at the resolution boundary. The result MUST be identical to what HIGHEST_CONFIDENCE would produce if all intents were evaluated simultaneously (no serialization effects).

**P32: Deferred Liveness**: For any scope with `deferred: true` and at least one active intent, a resolution boundary MUST eventually occur. The protocol MUST NOT allow intents to accumulate indefinitely without resolution (intents still expire via TTL as a safety net, but the resolution mechanism SHOULD fire before TTL expiry under normal operation).

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
  ScopeVisibility visibility         = 2;  // default: OPEN (Section 7.4)
  repeated string intent_actions     = 3;  // allowed intent action verbs
  repeated string action_actions     = 4;  // allowed action verbs
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

**P14: Scope Isolation**: An agent without authorization for scope S MUST be unable to write any mark to S. `write(unauthorized_agent, mark_in_S) → Error`.

**P15a: Structural Visibility**: For OPEN scopes, any agent MUST be able to read full marks regardless of authorization. For PROTECTED scopes, any agent MUST be able to read projected marks (structural metadata preserved, content redacted). `reader=None` (infrastructure) MUST receive full marks regardless of visibility.

**P15b: Content Access**: For PROTECTED scopes, an agent MUST have read authorization to access content fields. Without read authorization, an implementation MUST return projected marks with content fields redacted and `projected=true`.

**P15c: Classified Opacity**: For CLASSIFIED scopes, an agent without read authorization MUST receive an empty result. CLASSIFIED scopes MUST NOT fall back to projected reads. It is all-or-nothing.

**P16: Hierarchy**: Write authorization for scope `"a"` MUST imply write authorization for `"a/b"` and `"a/b/c"` for all b, c. Read authorization MUST follow the same rule.

**P27: Projection Preservation**: A projected mark MUST retain all structural and coordination metadata (id, mark_type, agent_id, scope, created_at, initial_strength, resource, action, topic, confidence, severity, priority, blocking, source, invalidates, resolved_by, supersedes). Only content fields (result, content, reason, question, context) are redacted. `projected` MUST be `true`.

**P28: Classified No Fallback**: CLASSIFIED scopes MUST NOT provide projected reads as a fallback. An unauthorized reader MUST receive an empty list, not projected marks. This prevents existence leakage.

**P29: Visibility Hierarchy Inheritance**: A child scope without its own definition MUST inherit the parent scope's visibility level. Read authorization for a parent scope MUST imply read authorization for all child scopes.

Ref: [`markspace/core.py::Scope, Agent, ScopeVisibility, project_mark`](../markspace/core.py), [`tests/test_properties.py::TestScopeVisibility`](../tests/test_properties.py)

## 8. Mark Space

The mark space is the shared environment. It stores marks and provides read/write operations.

### 8.1 Write

```
write(agent, mark) → mark_id

Preconditions:
  - agent MUST be authorized for mark.scope (Section 7.3)
  - mark.action MUST be in scope's allowed actions for mark.mark_type (if applicable)
  - mark MUST satisfy type-specific field requirements (Section 2)

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
resolve(need_mark_id, decision_mark_id) → void

  - Sets need_mark.resolved_by = decision_mark_id
  - The need mark's effective strength immediately becomes 0
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

**P17: Write Visibility**: A mark written at time t MUST be visible to reads at any time t' > t (within the mark's active lifetime).

**P18: Read Purity**: Reading marks MUST NOT change any mark's stored state.

**P19: Resolution Immediacy**: Resolving a need mark MUST immediately reduce its effective strength to 0 on the next read.

Ref: [`markspace/space.py::MarkSpace`](../markspace/space.py), [`tests/test_properties.py::TestMarkSpaceProperties`](../tests/test_properties.py)

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
  optional IntentMark  winning_intent      = 3;  // if CONFLICT, the intent that won
  repeated IntentMark  conflicting_intents = 4;  // other intents on this resource
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

Ref: [`markspace/core.py::effective_strength_with_warnings`](../markspace/core.py), [`tests/test_properties.py::TestWarningProperties`](../tests/test_properties.py)

### 9.6 Formal Properties

**P20: Invalidation Bound**: A warning MUST NOT reduce a mark's effective strength below 0.

**P21: Invalidation Decay**: As a warning decays, the invalidated mark's effective strength MUST recover (assuming the mark itself hasn't fully decayed).

**P22: Guard Determinism**: Given the same mark space state, `pre_action` MUST return the same verdict for the same inputs.

**P23: Guard Atomicity**: If `pre_action` returns CONFLICT or BLOCKED, `tool_fn` MUST NOT be called. The tool function is never invoked unless the guard explicitly allows it.

**P24: Guard Transparency**: The `GuardDecision` MUST contain enough information for the agent to reason about why it was blocked and choose an alternative.

**P26: Action Precedence**: If an action mark exists on a resource from agent X, the guard MUST return CONFLICT for any other agent's intent on that resource. Completed actions take precedence over new intents. Without this, intent-only conflict resolution would miss resources already claimed by executed actions.

Ref: [`markspace/guard.py::Guard`](../markspace/guard.py), [`tests/test_guard.py`](../tests/test_guard.py)

## 10. Generalized Supersession

All mark types MAY carry a `supersedes` field. An observation can supersede a prior observation on the same topic. An intent can supersede the same agent's prior intent on the same resource. This provides explicit versioning alongside continuous decay.

Any mark type MAY include an `optional string supersedes` field. When set, the referenced mark becomes invisible:

- A superseded mark's effective strength is 0 on all subsequent reads.
- The superseding mark's own lifecycle (decay, TTL) applies normally.
- Supersession chains: if A supersedes B, and C supersedes A, then both A and B are invisible.

**When to use supersession vs warnings**: Supersession replaces a mark quietly (the old version vanishes). Warnings loudly declare that something is wrong (the invalidation is itself a mark other agents can read). Use supersession for routine updates ("new price observation"), warnings for notable corrections ("previous data was wrong").

### 10.1 Formal Properties

**P25: Supersession Transitivity**: If mark C supersedes B, and B supersedes A, then A, B are both invisible and C is the only visible mark.

Ref: [`tests/test_guard.py::TestGeneralizedSupersession`](../tests/test_guard.py)

## 11. Agent

An agent is an identity with scope permissions and local rules. The protocol does not specify what agents do internally, only how they interact with the mark space.

### 11.1 Agent Definition

```protobuf
message ScopePermission {
  string          scope      = 1;  // scope name (hierarchical matching)
  repeated MarkType mark_types = 2;  // which mark types this agent can write
}

message Agent {
  string                   id          = 1;  // UUID
  string                   name        = 2;
  repeated ScopePermission permissions = 3;  // write permissions
  repeated string          read_scopes = 4;  // scopes with full content read access
}
```

`read_scopes` controls content access for PROTECTED and CLASSIFIED scopes ([Section 7.4](#74-scope-visibility)). For OPEN scopes, `read_scopes` is irrelevant. Both `permissions` and `read_scopes` support hierarchical matching.

### 11.2 Agent-Local Rules

The protocol does not prescribe agent-internal logic. An agent's behavior is defined by:

1. **Which marks it reads**: filtered by scope, topic, resource.
2. **How it reacts**: implementation-defined. The spec only constrains interactions with the mark space (authorization, conflict resolution).
3. **Which marks it writes**: constrained by its permissions.

The decomposability guarantee follows from this. You can test an agent in isolation by mocking the mark space. You can add new agents without changing existing ones. The mark space is the only coupling point.

## 12. Properties Summary

All normative properties, collected. A conforming implementation MUST satisfy all of these. The reference implementation's test suite verifies each one.

| ID | Property | Section |
|----|----------|---------|
| P1 | Decay Monotonicity | 3.2 |
| P2 | Action Permanence | 3.2 |
| P3 | Convergence | 3.2 |
| P4 | Intent Expiry | 3.2 |
| P5 | Need Persistence | 3.2 |
| P6 | Trust Ordering | 4.3 |
| P7 | Trust Bounds | 4.3 |
| P8 | Sublinearity | 5.3 |
| P9 | Boundedness | 5.3 |
| P10 | Monotonic Addition | 5.3 |
| P11 | Determinism | 6.3 |
| P12 | Progress | 6.3 |
| P13 | Consistency | 6.3 |
| P14 | Scope Isolation | 7.6 |
| P15a | Structural Visibility | 7.6 |
| P15b | Content Access | 7.6 |
| P15c | Classified Opacity | 7.6 |
| P16 | Hierarchy | 7.6 |
| P17 | Write Visibility | 8.5 |
| P18 | Read Purity | 8.5 |
| P19 | Resolution Immediacy | 8.5 |
| P20 | Invalidation Bound | 9.6 |
| P21 | Invalidation Decay | 9.6 |
| P22 | Guard Determinism | 9.6 |
| P23 | Guard Atomicity | 9.6 |
| P24 | Guard Transparency | 9.6 |
| P25 | Supersession Transitivity | 10.1 |
| P26 | Action Precedence | 9.6 |
| P27 | Projection Preservation | 7.4 |
| P28 | Classified No Fallback | 7.4 |
| P29 | Visibility Hierarchy | 7.4 |
| P30 | Deferred Completeness | 6.4 |
| P31 | Deferred Priority Fidelity | 6.4 |
| P32 | Deferred Liveness | 6.4 |

## 13. Reference Implementation

The reference implementation is in Python (3.11+), with pydantic and httpx as runtime dependencies. It is structured as:

```
markspace/
  core.py          -- types, decay, trust, reinforcement, conflict resolution
  space.py         -- MarkSpace (stateful read/write/query)
  guard.py         -- Guard (deterministic enforcement layer)
  llm.py           -- provider-agnostic LLM client (OpenAI-compatible)
  models.py        -- model registry
  __init__.py      -- DSL re-exports

tests/
  test_properties.py  -- property tests (P1-P21, P27-P29)
  test_guard.py       -- guard enforcement, supersession, deferred resolution (P22-P26, P30-P32)
  test_scenarios.py   -- end-to-end coordination scenarios
  test_concurrent.py  -- thread-safety tests
  test_hypothesis.py  -- hypothesis property-based tests with randomized inputs
```

### 13.1 DSL Usage

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
    intent_actions=["book", "reschedule", "cancel"],
    action_actions=["booked", "rescheduled", "cancelled"],
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

## 14. Conformance

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
10. It satisfies all 32 formal properties ([Section 12](#12-properties-summary)).

An implementation MAY differ from the reference implementation in storage backend, concurrency model, garbage collection strategy, or internal data structures, provided the properties hold.
