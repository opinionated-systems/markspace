# Stress Test: Office Coordination Week

## Context

The [validation](../validation/analysis.md) tested 1–10 agents on a single resource type (calendar booking) using only INTENT marks, ACTION marks, OPEN visibility, HIGHEST_CONFIDENCE, and FIRST_WRITER. That's ~30% of the protocol. This stress test exercises the full protocol surface: all 5 mark types, all 3 visibility levels, all 3 conflict policies, trust-weighted external agents, scope hierarchy, projected reads, warning invalidation, need escalation, and observation decay, all at 100-agent scale across 5 resource types over 10 rounds.

Single model: gpt-oss-120b. No model comparison; pure protocol stress test.

## Scenario

100 employees across 5 departments, plus 2 external system agents. Each employee has an AI personal assistant. No central scheduler. All coordination happens through the shared mark space. Work week: Mon–Fri, 2 blocks/day (AM, PM), 10 rounds.

Clock advances 4 hours between rounds. Intent TTL = 2 hours → intents expire between rounds. Action marks persist. Observations decay (half-life 6 hours → lose ~40% strength per round).

## Agents

### Department Agents (100, Source.FLEET, trust=1.0)

| Department | People | Head | Focus |
|-----------|--------|------|-------|
| **Engineering** | 20 | eng-lead | dept tasks, dept rooms, equipment |
| **Design** | 20 | design-lead | dept tasks, dept rooms, equipment |
| **Product** | 20 | product-lead | shared rooms (cross-dept), dept tasks |
| **Sales** | 20 | sales-lead | shared rooms (demos), parking |
| **Operations** | 20 | ops-lead | equipment, parking, lunch |

Each department head (5 total) gets elevated parking confidence (0.95 vs 0.5).

Manifest demand per department (approximate):

| Dept | Meetings/wk | Tasks | Equipment | Parking days | Lunches |
|------|-------------|-------|-----------|-------------|---------|
| Engineering | 2–3 | 1–2 | 0–1 | 2–3 | 4–5 |
| Design | 2–3 | 1–2 | 1–2 | 2–3 | 4–5 |
| Product | 3–4 | 0–1 | 0 | 2–3 | 4–5 |
| Sales | 2–3 | 0 | 0–1 | 3–4 | 3–4 |
| Operations | 1–2 | 0–1 | 1–2 | 3–4 | 4–5 |

### External Agents (2, no LLM, deterministic scripted actions)

**Parking Management Bot** (Source.EXTERNAL_VERIFIED, trust=0.7)
- Represents the building's parking garage system
- At each day boundary (rounds 1, 3, 5, 7, 9), pre-books 2–3 spots for visitors/contractors by writing ACTION marks
- Writes OBSERVATION marks about daily capacity ("27 of 30 spots remaining")
- Observations decay between rounds, so agents must check freshness
- Action marks are permanent but have trust-weighted strength (0.7)

**Building Operations Bot** (Source.EXTERNAL_UNVERIFIED, trust=0.3)
- Represents the building management system
- Between rounds, randomly issues room WARNING marks (~15% chance per round per shared room) simulating maintenance/AV issues
- Warnings reduce the target booking's effective strength via `effective_strength_with_warnings()`
- Low trust (0.3) means a single warning barely dents a fleet booking, but multiple warnings on the same room `reinforce()` to a meaningful signal
- Agents check warnings on their bookings via `my_bookings` and rebook if invalidated

## 5 Resource Types (Scopes)

### Visibility & Permission Architecture

```
┌──────────────────────────────────────────────────────┐
│                    OPEN scopes                       │
│  equipment, lunch                                    │
│  (all agents read full marks, all can book)          │
├──────────────────────────────────────────────────────┤
│                  PROTECTED scopes                    │
│  rooms/shared, parking                               │
│  (all agents see structure: "room booked at time X") │
│  (content redacted: who booked, meeting topic)       │
│  (dept members with read_scopes see full content)    │
├──────────────────────────────────────────────────────┤
│                  CLASSIFIED scopes                   │
│  rooms/{dept}, tasks/{dept}                          │
│  (only authorized dept agents can read at all)       │
│  (other depts don't know these resources exist)      │
└──────────────────────────────────────────────────────┘
```

### Resource Table

| Scope | Resources | Slots | Visibility | Policy | Contention |
|-------|-----------|-------|------------|--------|------------|
| `rooms/{dept}` (×5) | 3 rooms per dept × 10 blocks | 150 (30/dept) | CLASSIFIED | HIGHEST_CONFIDENCE | 1.5:1 within dept |
| `rooms/shared` | 5 rooms × 10 blocks | 50 | PROTECTED | HIGHEST_CONFIDENCE | 2:1 all depts |
| `rooms/exec` | 1 boardroom × 10 blocks | 10 | OPEN | YIELD_ALL → auto-resolve | variable |
| `tasks/{dept}` (×5) | 15 tasks per dept | 75 | CLASSIFIED | FIRST_WRITER | 1.5:1 within dept |
| `equipment` | 8 items × 10 blocks | 80 | OPEN | HIGHEST_CONFIDENCE | 0.6:1 |
| `parking` | 30 spots × 5 days | 150 | PROTECTED | HIGHEST_CONFIDENCE | 2:1 + external pre-alloc |
| `lunch` | 2 types × 4 windows × 5 days × capacity | 100/type/day | OPEN | FIRST_WRITER | preference competition |
| **Total** | | **815** | | | |

### Resource Details

**Department Rooms** (`rooms/eng`, `rooms/design`, etc.), CLASSIFIED
- 3 rooms per dept: `{dept}-huddle-1`, `{dept}-huddle-2`, `{dept}-huddle-3`
- Only that department's agents can see availability or book
- Uses `ScopeVisibility.CLASSIFIED` + `read_scopes` authorization
- Other departments literally cannot see these rooms' marks, which tests scope isolation at scale
- 30 slots per dept, ~45 demand/dept → some overflow to shared rooms

**Shared Conference Rooms** (`rooms/shared`), PROTECTED
- 5 rooms: `large-conf-1`, `large-conf-2`, `all-hands`, `presentation`, `client-demo`
- PROTECTED visibility: all agents see that a room is booked (structural metadata preserved)
- Content redacted unless reader has `read_scopes` for `rooms/shared`: booker identity hidden
- Tests `project_mark()`: agents decide based on projected (incomplete) information
- Cross-dept overflow lands here. Product and Sales generate most demand.

**Executive Boardroom** (`rooms/exec`), OPEN, YIELD_ALL with auto-resolution
- 1 room, 10 blocks
- YIELD_ALL policy: when multiple departments want the same slot, nobody wins immediately
- Guard creates NEED marks on conflict
- **Between rounds, simulated principal resolves**: highest-confidence claimant wins, `space.resolve()` called, winning agent books in next round
- Tests the full `YIELD_ALL → NEED → aggregate_needs() → resolve() → action` lifecycle
- Uncontested slots (single department) are allowed through immediately

**Department Tasks** (`tasks/eng`, etc.), CLASSIFIED, FIRST_WRITER
- 15 tasks per dept (75 total), labeled `{dept}/1` through `{dept}/15`
- CLASSIFIED: only department members see the board
- Dependencies per dept: task/6 requires task/2, task/10 requires task/5, task/15 requires task/10 (3-deep chain)
- First writer wins simultaneous claims
- Demand: ~25 claims per dept (20 agents × ~1.3 tasks each) for 15 tasks → 1.7:1
- Task claiming spreads across rounds 1–5 as dependencies unlock

**Equipment** (`equipment`), OPEN
- 8 shared items, bookable per time block
- OPEN: everyone sees full availability including who booked what
- Low contention; serves as baseline for "coordination works easily" comparison

**Parking** (`parking`), PROTECTED, HIGHEST_CONFIDENCE + external agent
- 30 spots per day
- PROTECTED: agents see remaining count but not individual booker identities
- **Priority hierarchy**:
  - Parking bot pre-books 2–3 spots/day at confidence=0.7 (EXTERNAL_VERIFIED, trust=0.7)
  - Department heads at confidence=0.95 (FLEET, trust=1.0)
  - Regular employees at confidence=0.5
  - Heads always win concurrent conflicts with regulars
- Parking bot writes OBSERVATION marks about daily capacity that decay (half-life 6h, ~40% per round)
- `view_parking` shows current availability + observation freshness indicator
- After pre-alloc: ~27 spots for 100 agents → ~65 want parking daily → 2.4:1

**Lunch** (`lunch`), OPEN, FIRST_WRITER + preference competition
- Two types: **Type A** (hot meal, popular) and **Type B** (cold/salad, less popular)
- 4 daily windows: `11:00`, `11:30`, `12:00`, `12:30`
- Type A: 8 per window = 32/day. Type B: 17 per window = 68/day. Total: 100/day (everyone eats)
- OPEN: agents see remaining slots per type per window
- **Everyone gets lunch. The question is whether you get your preferred type.**
- ~65% of agents prefer Type A → 65 demand for 32 capacity → ~50% get preferred
- Agents order preferred type first; if full, get the other type automatically
- NEED marks fire when a department consistently misses preferred type: "Engineering got Type B 4 of 5 days, request more Type A allocation"
- Metric: **preference satisfaction rate** by department, not completion rate
- Tests whether departments with early-round activity (Product, Sales) lock out later depts from Type A

## Simulation Walkthrough (number validation)

**Adjusted supply vs demand:**

| Resource | Supply (week) | Demand (week) | Ratio | Unmet (%) |
|----------|--------------|---------------|-------|-----------|
| Dept rooms | 150 (30/dept) | ~220 | 1.5:1 | ~32% overflow to shared |
| Shared rooms | 50 | ~100 overflow | 2:1 | ~50% denied |
| Boardroom | 10 | ~15 | 1.5:1 | Contested → NEED marks |
| Tasks | 75 (15/dept) | ~110 | 1.5:1 | ~32% denied |
| Equipment | 80 | ~50 | 0.6:1 | ~0% |
| Parking | 150 minus ~12 pre-alloc = 138 | ~280 | 2:1 | ~51% denied |
| Lunch (Type A) | 160 (32/day) | ~325 (65/day) | 2:1 | ~51% miss preferred type |

**Round-by-round trace:**

Round 1 (Mon AM): 100 agents. Rush for Mon-AM rooms (~20 agents, 15+5+1=21 slots → fits). Task claiming begins (~40 agents claim ~40 tasks). Parking: 30 spots, ~65 requests, ~35 denied. Lunch: all 100 order; Type A fills fast (8/window), ~33 get preferred, ~67 fall back to Type B. Building ops: no action yet.

Round 2 (Mon PM): ~80 agents. Mon-PM rooms (~15 agents, fits). More tasks claimed. Lunch: remaining windows from Mon still have Type A capacity from unfilled slots. Building ops: 15% chance of warning on 1 of 5 shared rooms.

Rounds 3-4 (Tue): Fresh parking allocation. Parking bot pre-books 2-3 spots. ~65 agents want parking → ~38 denied. Task dependencies unlock: task/6 available after task/2 claimed in round 1-2. Building ops may warn a room → agent needs to rebook. Lunch preference patterns emerge as early-round depts (Product, Sales) may lock out later depts from Type A.

Rounds 5-6 (Wed): Most rooms booked for Mon-Tue are freed (different day). Fresh room availability. Tasks: ~50 of 75 claimed by now. Dependency chain: task/10 unlocking (requires task/5). Parking contention continues. Lunch NEED marks start accumulating if a dept consistently misses Type A.

Rounds 7-8 (Thu): Equipment demand peaks (design/ops). Most tasks claimed. Parking contention stable. Some boardroom YIELD_ALL conflicts resolved from earlier rounds, and winning agents book their slots. Lunch preference satisfaction diverges across departments.

Rounds 9-10 (Fri): Final bookings. Tasks: ~65 of 75 claimed. Last parking attempts. NEED marks accumulated: ~5-15 lunch (dept-level preference complaints) + ~5-10 boardroom. Observation marks from parking bot round 1 have decayed to ~15% strength (4 rounds × 40% decay).

**Mark space at end of trial:**
- ACTION marks: ~600 (rooms ~200, tasks ~65, equipment ~40, parking ~130, lunch ~165, since everyone books lunch)
- Expired INTENT marks: ~1000+ (all attempts including failures)
- OBSERVATION marks: ~10 (parking bot, mostly decayed)
- WARNING marks: ~3-5 (building ops, partially decayed)
- NEED marks: ~15-25 (lunch preference complaints ~10-15, boardroom ~5-10)
- Total live marks: ~630, total ever written: ~1800+

**LLM call estimate:**
- ~650 agent-activations (100 agents × ~6.5 active rounds)
- ~5 LLM steps per activation (view + act + retry or next task)
- ~3,250 LLM calls total
- With max_concurrent=20: ~15 min wall clock

## Protocol Features Exercised

| Feature | How it's tested | Scope(s) |
|---------|----------------|----------|
| INTENT marks | All booking attempts | all |
| ACTION marks | All successful bookings | all |
| OBSERVATION marks | Parking bot writes capacity observations; decay between rounds | parking |
| WARNING marks | Building ops bot issues room maintenance warnings | rooms/shared |
| NEED marks | Lunch exhaustion + boardroom YIELD_ALL conflicts | lunch, rooms/exec |
| ScopeVisibility.OPEN | Equipment, lunch, exec boardroom | equipment, lunch, rooms/exec |
| ScopeVisibility.PROTECTED | Shared rooms, parking (structure visible, content redacted) | rooms/shared, parking |
| ScopeVisibility.CLASSIFIED | Dept rooms, dept tasks (invisible to outsiders) | rooms/{dept}, tasks/{dept} |
| read_scopes | Dept membership controls content access | rooms/*, tasks/* |
| Source.FLEET | All 100 department agents | all |
| Source.EXTERNAL_VERIFIED | Parking management bot | parking |
| Source.EXTERNAL_UNVERIFIED | Building operations bot | rooms/shared |
| trust_weight() | External marks have 0.7 or 0.3 trust, affects effective strength | parking, rooms/shared |
| effective_strength_with_warnings() | Room warnings reduce booking strength | rooms/shared |
| reinforce() | Multiple building warnings on same room compound | rooms/shared |
| HIGHEST_CONFIDENCE | Rooms, equipment, parking (with priority tiers) | rooms/*, equipment, parking |
| FIRST_WRITER | Tasks, lunch | tasks/*, lunch |
| YIELD_ALL | Executive boardroom (creates NEED marks) | rooms/exec |
| resolve_conflict() | All three policies in one trial | all |
| aggregate_needs() | Lunch + boardroom needs clustered for analysis | lunch, rooms/exec |
| project_mark() | PROTECTED scope reads return redacted marks | rooms/shared, parking |
| block_self_rebook | Prevents cross-round double bookings | all |
| Scope hierarchy | `rooms/eng` permission → access to all room sub-resources | rooms/*, tasks/* |
| compute_strength decay | Observations decay between rounds, warnings decay | parking, rooms/shared |

## Agent Permissions

```python
# Engineering agent (example)
Agent(
    name="eng-alice",
    scopes={
        "rooms/eng": ["intent", "action"],
        "rooms/shared": ["intent", "action"],
        "rooms/exec": ["intent", "action", "need"],
        "tasks/eng": ["intent", "action"],
        "equipment": ["intent", "action"],
        "parking": ["intent", "action"],
        "lunch": ["intent", "action", "need"],
    },
    read_scopes=frozenset({"rooms/eng", "tasks/eng", "rooms/shared"}),
)

# Sales agent: different scope access
Agent(
    name="sales-bob",
    scopes={
        "rooms/sales": ["intent", "action"],
        "rooms/shared": ["intent", "action"],
        "rooms/exec": ["intent", "action", "need"],
        "tasks/sales": ["intent", "action"],
        "parking": ["intent", "action"],
        "lunch": ["intent", "action", "need"],
        # No equipment scope: sales doesn't use lab equipment
    },
    read_scopes=frozenset({"rooms/sales", "tasks/sales", "rooms/shared"}),
)

# Parking management bot
Agent(
    name="parking-system",
    scopes={
        "parking": ["action", "observation"],
    },
    read_scopes=frozenset({"parking"}),
)

# Building ops bot
Agent(
    name="building-ops",
    scopes={
        "rooms/shared": ["warning"],
    },
    read_scopes=frozenset(),
)
```

## Round Structure

Each round = 1 time block. External bots act first (deterministic, no LLM), then department agents act (LLM-powered, concurrent).

| Round | Day/Block | External bot actions | Active dept agents |
|-------|-----------|---------------------|-------------------|
| 1 | Mon AM | Parking bot: pre-book 4 spots, write capacity obs | ~100 (initial rush) |
| 2 | Mon PM | Building ops: 15% chance warning on 1 shared room | ~80 |
| 3 | Tue AM | Parking bot: pre-book 3 spots | ~70 |
| 4 | Tue PM | Building ops: 15% chance warning | ~60 |
| 5 | Wed AM | Parking bot: pre-book 5 spots | ~70 |
| 6 | Wed PM | Building ops: 15% chance warning | ~50 |
| 7 | Thu AM | Parking bot: pre-book 3 spots | ~60 |
| 8 | Thu PM | Building ops: 15% chance warning | ~50 |
| 9 | Fri AM | Parking bot: pre-book 4 spots | ~70 |
| 10 | Fri PM | Building ops: 15% chance warning | ~40 |

Agent activation: agents with remaining manifest items for this round's day/block are active. Agents with nothing left to do are inactive.

## Tools (12)

| Tool | Scope | Type | Key args | Available to |
|------|-------|------|----------|-------------|
| `view_dept_rooms` | rooms/{dept} | read | n/a | Dept members only |
| `book_dept_room` | rooms/{dept} | guard | `room, day, block` | Dept members only |
| `view_shared_rooms` | rooms/shared | read | n/a | All (PROTECTED → projected for non-members) |
| `book_shared_room` | rooms/shared | guard | `room, day, block` | All |
| `book_boardroom` | rooms/exec | guard | `day, block` | All (YIELD_ALL) |
| `view_tasks` | tasks/{dept} | read | n/a | Dept members only |
| `claim_task` | tasks/{dept} | guard | `task_id` | Dept members only |
| `view_equipment` | equipment | read | `item?, day?` | All |
| `reserve_equipment` | equipment | guard | `item, day, block` | Eng, Design, Ops |
| `request_parking` | parking | guard | `day` | All (auto-assign spot) |
| `order_lunch` | lunch | guard | `day, window, preferred_type` | All (fallback to other type if preferred full) |
| `my_status` | all | read | n/a | All (own action marks + warnings on own bookings) |

Role-based tool filtering: agents only get tools for their authorized scopes. Sales agents don't see equipment tools. `my_status` returns the agent's bookings across all scopes AND any active warnings affecting them (critical for maintenance recovery).

## Prompts

Per-agent, per-round system prompt:

```
You are the AI assistant for {name} ({dept} department) at the company.
It is {day_name} {block_name}.

YOUR TASKS THIS ROUND:
{manifest_items_for_this_round}

YOUR CURRENT BOOKINGS:
{completed_from_action_marks}

WARNINGS:
{any_warnings_affecting_your_bookings}

CONTEXT:
- Your department has {dept_size} people.
- Department rooms are private to your team. Shared rooms are used by everyone.
- The boardroom requires approval when contested. You may be blocked.
- Parking is limited (30 spots, 100 people). {head_priority_note}
- Lunch: Type A (hot meal) is popular but limited. Type B (cold) is always available. Order your preferred type early. If your department consistently misses preferred type, report the need.
- If you see CONFLICT, try a different resource.
- If you see BLOCKED (boardroom), the conflict is being escalated. Move on.
- If a booking has a maintenance warning, rebook elsewhere.
- Call tools to accomplish your tasks, then stop when done.
```

## Implementation

### Files

All files go in `research/stigmergic-coordination/experiment/`:

```
research/stigmergic-coordination/experiment/
├── design.md       # This design document (moved from plan file)
├── run.py          # OfficeEnv, agent runner, round/trial orchestration, CLI
├── scenario.py     # Constants, manifest generation, prompts, tool schemas, bot logic
└── analyze.py      # Per-scope, per-dept, temporal analysis, need tracking
```

### `scenario.py`

```python
DEPTS = ["eng", "design", "product", "sales", "ops"]
DEPT_ROOMS = {
    dept: [f"{dept}-huddle-1", f"{dept}-huddle-2", f"{dept}-huddle-3"]
    for dept in DEPTS
}
SHARED_ROOMS = [
    "large-conf-1", "large-conf-2", "all-hands", "presentation", "client-demo",
]
EXEC_ROOM = "boardroom"
DAYS = ["mon", "tue", "wed", "thu", "fri"]
BLOCKS = ["AM", "PM"]
EQUIPMENT = [...]  # 8 items
PARKING_SPOTS = 30
LUNCH_WINDOWS = ["11:00", "11:30", "12:00", "12:30"]
LUNCH_TYPE_A_PER_WINDOW = 8  # Popular hot meal, 32/day total
LUNCH_TYPE_B_PER_WINDOW = 17  # Cold/salad, 68/day total (everyone eats)
PROJECTS = {dept: [f"{dept}/{i}" for i in range(1, 16)] for dept in DEPTS}
TASK_DEPS = {6: [2], 10: [5], 15: [10]}  # per-dept: task N requires task M

Role = Literal["engineer", "designer", "pm", "sales", "ops"]


@dataclass
class ManifestItem:
    scope: str
    description: str
    target: dict  # {"room": "large-conf", "day": "wed", "block": "AM"}
    earliest_round: int
    completed: bool = False
    failed: bool = False


@dataclass
class AgentProfile:
    name: str
    dept: str
    is_head: bool
    agent: Agent  # markspace Agent with scopes + read_scopes
    manifest: list[ManifestItem]


def generate_profiles(
    n_per_dept: int = 20, seed: int = 42
) -> list[AgentProfile]: ...
def make_tools_for_agent(profile: AgentProfile) -> list[dict]: ...
def make_prompt(
    profile: AgentProfile, round_num: int, env: OfficeEnv
) -> str: ...

# External bot logic (deterministic, no LLM)
def run_parking_bot(
    env: OfficeEnv, round_num: int, rng: Random
) -> list[str]: ...
def run_building_ops_bot(
    env: OfficeEnv, round_num: int, rng: Random
) -> list[str]: ...
```

### `run.py`

```python
class OfficeEnv:
    """Multi-scope environment with full protocol coverage."""

    def __init__(self) -> None:
        # Register all scopes with appropriate visibility + policy
        dept_scopes = [
            Scope(
                name=f"rooms/{d}",
                visibility=CLASSIFIED,
                policy=HIGHEST_CONFIDENCE,
                ...,
            )
            for d in DEPTS
        ]
        shared_scope = Scope(
            name="rooms/shared", visibility=PROTECTED, policy=HIGHEST_CONFIDENCE, ...
        )
        exec_scope = Scope(
            name="rooms/exec", visibility=OPEN, policy=YIELD_ALL, ...
        )
        task_scopes = [
            Scope(
                name=f"tasks/{d}", visibility=CLASSIFIED, policy=FIRST_WRITER, ...
            )
            for d in DEPTS
        ]
        equip_scope = Scope(
            name="equipment", visibility=OPEN, policy=HIGHEST_CONFIDENCE, ...
        )
        parking_scope = Scope(
            name="parking", visibility=PROTECTED, policy=HIGHEST_CONFIDENCE, ...
        )
        lunch_scope = Scope(
            name="lunch", visibility=OPEN, policy=FIRST_WRITER, ...
        )

        all_scopes = (
            dept_scopes
            + [shared_scope, exec_scope]
            + task_scopes
            + [equip_scope, parking_scope, lunch_scope]
        )
        self.space = MarkSpace(scopes=all_scopes)
        self.space.set_clock(1_000_000.0)
        self.guard = Guard(self.space, block_self_rebook=True)

    # View methods (respect visibility via reader agent)
    def view_dept_rooms(self, agent: Agent, dept: str) -> str: ...
    def view_shared_rooms(self, agent: Agent) -> str: ...
    def view_tasks(self, agent: Agent, dept: str) -> str: ...
    def view_equipment(
        self, agent: Agent, item: str | None = None, day: str | None = None
    ) -> str: ...
    def my_status(self, agent: Agent) -> str: ...

    # Guard-wrapped actions
    def book_dept_room(
        self, agent: Agent, dept: str, room: str, day: str, block: str,
        confidence: float = 0.8,
    ) -> tuple[bool, str]: ...
    def book_shared_room(
        self, agent: Agent, room: str, day: str, block: str,
        confidence: float = 0.8,
    ) -> tuple[bool, str]: ...
    def book_boardroom(
        self, agent: Agent, day: str, block: str, confidence: float = 0.8
    ) -> tuple[bool, str]: ...
    def claim_task(
        self, agent: Agent, dept: str, task_id: str, confidence: float = 0.8
    ) -> tuple[bool, str]: ...
    def reserve_equipment(
        self, agent: Agent, item: str, day: str, block: str,
        confidence: float = 0.8,
    ) -> tuple[bool, str]: ...
    def request_parking(
        self, agent: Agent, day: str, confidence: float = 0.5
    ) -> tuple[bool, str]: ...
    def order_lunch(
        self, agent: Agent, day: str, window: str, preferred_type: str = "A"
    ) -> tuple[bool, str]: ...

    # Metrics
    def snapshot_metrics(self) -> dict[str, Any]: ...
```

**Key implementation detail for YIELD_ALL boardroom**: When `guard.execute()` returns `BLOCKED`, the guard has already written a NEED mark. The environment returns: `"BLOCKED: Multiple departments want the boardroom at this time. The conflict has been escalated. Try a different time or use a shared room instead."`

**Key implementation detail for `my_status`**: Reads the agent's own action marks across all scopes, then checks for warnings targeting those marks. Returns structured output showing bookings and any warnings/invalidations.

### Round flow

```python
def run_round(
    profiles: list[AgentProfile],
    env: OfficeEnv,
    client: OpenAI,
    round_num: int,
    rng: Random,
    max_concurrent: int = 20,
) -> RoundResult:
    # 1. External bot actions (deterministic)
    parking_log = run_parking_bot(env, round_num, rng)
    building_log = run_building_ops_bot(env, round_num, rng)

    # 2. Filter active agents
    active = [p for p in profiles if has_remaining_work(p, round_num)]

    # 3. Run department agents (LLM, concurrent)
    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        futures = {
            pool.submit(run_agent, p, env, client, round_num): p
            for p in active
        }
        ...

    # 4. Collect metrics + mark space snapshot
    return RoundResult(...)
```

### CLI

```bash
# Smoke test
python experiments/stress_test/run.py --agents-per-dept 2 --rounds 3

# Full run
python experiments/stress_test/run.py --agents-per-dept 20 --rounds 10 --seed 42 \
    --adversarial 5 --phase stress_test --output-dir results --max-concurrent 20
```

## Metrics

### Safety
- Double bookings per scope (expect 0 for all guarded actions)
- Scope isolation violations (agent accessing CLASSIFIED scope without auth; expect 0)

### Protocol Coverage
- Mark type distribution: count of INTENT, ACTION, OBSERVATION, WARNING, NEED marks created
- Verdict distribution per scope: ALLOW, CONFLICT, BLOCKED, DENIED
- NEED marks created: by scope, by department
- Warnings issued and their effective strength impact
- Projected reads count (PROTECTED scope reads by non-members)

### Efficiency
- Steps per agent per round, by department and scope
- Wasted attempts (CONFLICT + BLOCKED + DENIED)
- Manifest completion rate by department and scope

### Temporal
- Resource exhaustion curves (when does parking fill? lunch windows?)
- Mark accumulation over rounds (total marks by type)
- Observation decay: parking bot observations' effective strength over time
- Warning recovery: how quickly do agents rebook after maintenance warnings?
- NEED accumulation: do needs cluster by department? by day?

### Cross-Department
- Shared room contention: which departments win most bookings? (tests confidence asymmetry)
- Parking allocation fairness: dept heads vs regular employees
- Lunch Type A preference satisfaction rate by department (who gets their preferred type?)
- Lunch ordering advantage: do early-round departments lock out later ones from Type A?

## Verification

1. **Smoke** (2 agents/dept = 10, 3 rounds): Zero safety violations, tools dispatch correctly, CLASSIFIED isolation works (eng can't see design rooms), PROTECTED projection works (shared room reads are redacted for non-bookers), YIELD_ALL creates NEED marks, external bots work, warnings reduce booking strength, parking priority hierarchy works
2. **Full** (20 agents/dept = 100, 10 rounds): All metrics collected, zero double bookings, analyze completion by department, verify every protocol feature produced non-trivial output
