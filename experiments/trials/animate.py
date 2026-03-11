"""Generate animated GIF visualization of a stress test trial.

Adapted from experiments/stress_test/animate.py to handle arbitrary agent counts.
Takes a results directory as argument.

Usage:
    python animate.py results/scaling_proportional/gpt-oss-120b/n_500
"""

import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont  # type: ignore

random.seed(42)


# ── Dimensions ────────────────────────────────────────────────────────────────

W, H = 1280, 720


# ── Colors ────────────────────────────────────────────────────────────────────

BG = (0, 0, 0)
ACCENT = (0, 217, 255)  # cyan
WARM = (255, 120, 50)  # orange (adversarial)
FG = (180, 190, 200)
DIM = (35, 40, 50)
FAINT = (18, 22, 30)
GREEN = (63, 185, 80)

DEPT_COLORS = {
    "eng": (0, 217, 255),
    "design": (80, 210, 240),
    "product": (0, 175, 215),
    "sales": (40, 195, 235),
    "ops": (0, 150, 190),
}

DEPT_ORDER = ["eng", "design", "product", "sales", "ops"]

VERDICT_COLORS = {
    "allow": GREEN,
    "blocked": (210, 153, 34),
    "conflict": (240, 136, 62),
    "denied": (248, 81, 73),
}

# Resource panel - warm/purple palette distinct from cyan dept colors
RESOURCE_ORDER = [
    "dept_rooms",
    "shared_rooms",
    "exec",
    "parking",
    "lunch",
    "tasks",
    "equip",
]
RESOURCE_COLORS = {
    "dept_rooms": (180, 130, 220),  # purple
    "shared_rooms": (160, 110, 210),  # lighter purple
    "exec": (140, 100, 195),  # deep violet
    "parking": (210, 110, 170),  # rose
    "lunch": (200, 165, 100),  # warm amber
    "tasks": (165, 195, 110),  # lime
    "equip": (130, 160, 210),  # steel blue
}
RESOURCE_LABELS = {
    "dept_rooms": "DEPT ROOMS",
    "shared_rooms": "SHARED ROOMS",
    "exec": "BOARDROOM",
    "parking": "PARKING",
    "lunch": "LUNCH",
    "tasks": "TASKS",
    "equip": "EQUIPMENT",
}
TOOL_RESOURCE = {
    "book_dept_room": "dept_rooms",
    "book_shared_room": "shared_rooms",
    "book_other_dept_room": "dept_rooms",
    "book_boardroom": "exec",
    "request_parking": "parking",
    "order_lunch": "lunch",
    "claim_task": "tasks",
    "reserve_equipment": "equip",
    "issue_warning": "exec",
}


# ── Fonts ─────────────────────────────────────────────────────────────────────

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

font = ImageFont.truetype(FONT_PATH, 11)
font_b = ImageFont.truetype(FONT_BOLD, 12)
font_t = ImageFont.truetype(FONT_BOLD, 18)
font_xl = ImageFont.truetype(FONT_BOLD, 28)


# ── Layout constants ─────────────────────────────────────────────────────────

HDR_H = 34
FTR_H = 38
BODY_Y = HDR_H + 2
BODY_H = H - HDR_H - FTR_H - 4

# Guard wall (center strip)
GW_W = 6

# Mark space grid cell
MCELL = 4
MGAP = 1
MSTEP = MCELL + MGAP

# Resource zones in the mark space (vertical bands)
N_DEPT_ZONES = 5  # mark space rows align with dept bands

# Timing
PULSE_FRAMES = 5
FLASH_FRAMES = 8

DAY_LABELS = {}
for i, (day, block) in enumerate(
    [(d, b) for d in ["MON", "TUE", "WED", "THU", "FRI"] for b in ["AM", "PM"]]
):
    DAY_LABELS[i] = f"{day} {block}"


# ── Load data ─────────────────────────────────────────────────────────────────

if len(sys.argv) < 2:
    print("Usage: python animate.py <results_dir>")
    sys.exit(1)

results_dir = Path(sys.argv[1])
steps_data = [json.loads(line) for line in open(results_dir / "steps.jsonl")]
rounds_data = {}
for line in open(results_dir / "rounds.jsonl"):
    r = json.loads(line)
    rounds_data[r["round_num"]] = r
trial = json.loads(open(results_dir / "trial.jsonl").read().strip())

total_agents = trial["total_agents"]
n_rounds = trial["n_rounds"]

# Use only week 1 (rounds 0-9) to fit 5-day mark space layout.
# Week 2 rounds would overflow the day columns and cause clipping.
MAX_ROUND = 9
steps_data = [s for s in steps_data if s["round_num"] <= MAX_ROUND]
rounds_data = {k: v for k, v in rounds_data.items() if k <= MAX_ROUND}

# Compute steps per frame to keep GIF manageable (~300-500 frames)
total_steps = len(steps_data)
target_frames = 400
STEPS_PER_FRAME = max(1, total_steps // target_frames)


# ── Compute agent layout dimensions ─────────────────────────────────────────

agents_per_dept = total_agents // 5
# Dot size scales with agent count
if total_agents <= 120:
    DOT = 5
    DOT_STEP = 8
elif total_agents <= 300:
    DOT = 4
    DOT_STEP = 6
else:
    DOT = 3
    DOT_STEP = 4

# Square-ish agent block that fits under dept labels
band_h = BODY_H // 5
agent_cols = math.ceil(math.sqrt(agents_per_dept))
agent_rows = math.ceil(agents_per_dept / agent_cols)
LABEL_X = 2  # dept labels flush left
LABEL_H = 14  # vertical space for dept label above dots
AZ_X = LABEL_X + 2  # dots start just under the label
AZ_W = agent_cols * DOT_STEP + 4

# Resource panel on the right
RP_W = 150
RP_X = W - RP_W - 2
RP_ROW_H = BODY_H // len(RESOURCE_ORDER)
RP_LABEL_H = 14 + 2 * MSTEP  # extra rows of padding below label
RP_GRID_COLS = (RP_W - 4) // MSTEP
RP_GRID_ROWS = (RP_ROW_H - RP_LABEL_H - 2) // MSTEP

GW_X = AZ_X + AZ_W + 8
MS_X = GW_X + GW_W + 12
MS_W = RP_X - MS_X - 8
MS_COLS = MS_W // MSTEP
MS_ROWS = BODY_H // MSTEP


# ── Build agent registry ─────────────────────────────────────────────────────

agents = {}  # name -> {dept, is_adv, pos}
dept_agents = defaultdict(list)

for s in steps_data:
    name = s["agent"]
    if name in agents:
        continue
    dept = next(
        (d for d in DEPT_ORDER if name.startswith(d) or name.startswith(f"adv-{d}")),
        "ops",
    )
    is_adv = name.startswith("adv-")
    agents[name] = {"dept": dept, "is_adv": is_adv, "pos": (0, 0)}
    dept_agents[dept].append(name)

# Sort each department: normal agents first (alphabetical), adversarial last
for dept in DEPT_ORDER:
    dept_agents[dept].sort(key=lambda n: (agents[n]["is_adv"], n))

# Assign screen positions
for di, dept in enumerate(DEPT_ORDER):
    band_y = BODY_Y + di * band_h
    for i, name in enumerate(dept_agents[dept]):
        col = i % agent_cols
        row = i // agent_cols
        x = AZ_X + col * DOT_STEP
        y = band_y + LABEL_H + row * DOT_STEP
        agents[name]["pos"] = (x, y)


# ── Animation state ──────────────────────────────────────────────────────────

mark_cells = {}  # (col, row) -> (color, birth_frame)
guard_flashes = []  # [(screen_y, color, remaining_frames)]
agent_pulses = {}  # name -> (color, remaining_frames)
totals = {"allow": 0, "blocked": 0, "conflict": 0, "denied": 0}
frame_num = 0

# Resource panel state: mini grids per resource
res_cells = {r: {} for r in RESOURCE_ORDER}  # resource -> {(col,row): (color, birth)}
res_counts = {r: 0 for r in RESOURCE_ORDER}


def _resource_key(step):
    """Extract a stable key identifying the specific resource instance from a step."""
    tool = step.get("tool", "")
    args = step.get("args", {})
    if tool in ("book_dept_room", "book_other_dept_room", "book_shared_room"):
        return args.get("room", "")
    if tool == "book_boardroom":
        return "boardroom"
    if tool == "request_parking":
        return step.get("agent", "")
    if tool == "order_lunch":
        return f"{args.get('preferred_type', '')}-{args.get('window', '')}"
    if tool == "claim_task":
        return args.get("task_id", "")
    if tool == "reserve_equipment":
        return args.get("item", "")
    if tool == "issue_warning":
        return args.get("room", "warning")
    return ""


# Pre-scan steps to collect all resource instance keys, then sort for grouping
_res_keys = {r: set() for r in RESOURCE_ORDER}
for _s in steps_data:
    _tool = _s.get("tool", "")
    _res = TOOL_RESOURCE.get(_tool)
    if _res and _s.get("guard_verdict"):
        _key = _resource_key(_s)
        if _key:
            _res_keys[_res].add(_key)

# Assign stable grid positions from sorted keys (groups related instances)
res_index = {r: {} for r in RESOURCE_ORDER}
_grid_cap = RP_GRID_COLS * max(1, RP_GRID_ROWS)
for _res, _keys in _res_keys.items():
    for idx, _key in enumerate(sorted(_keys)):
        if idx >= _grid_cap:
            break  # grid full; count still tracked
        res_index[_res][_key] = (idx % RP_GRID_COLS, idx // RP_GRID_COLS)


def get_mark_pos(dept, current_round):
    """Pick a position in the mark space grid aligned with the agent's dept band."""
    dept_idx = DEPT_ORDER.index(dept) if dept in DEPT_ORDER else 0
    zone_h = MS_ROWS // N_DEPT_ZONES
    y_base = dept_idx * zone_h
    y = y_base + random.randint(2, max(3, zone_h - 3))

    # Place marks within the current day's column band
    day_idx = current_round // 2  # 0=MON, 1=TUE, ..., 4=FRI
    is_pm = current_round % 2
    day_col_w = (MS_COLS - 6) // 5  # 5 day columns
    x_base = 3 + day_idx * day_col_w
    # AM fills left half of day column, PM fills right half
    half_w = max(2, day_col_w // 2 - 1)
    x_offset = (half_w if is_pm else 0) + random.randint(0, max(1, half_w - 1))
    x = x_base + x_offset
    x = max(0, min(MS_COLS - 1, x))
    y = max(0, min(MS_ROWS - 1, y))
    return x, y


def place_mark(mx, my, color):
    """Place a mark cell plus occasional neighbor cells for organic growth."""
    mark_cells[(mx, my)] = (color, frame_num)
    for _ in range(random.randint(0, 2)):
        dx = random.choice([-1, 0, 1])
        dy = random.choice([-1, 0, 1])
        if dx == 0 and dy == 0:
            continue
        nx, ny = mx + dx, my + dy
        if 0 <= nx < MS_COLS and 0 <= ny < MS_ROWS and (nx, ny) not in mark_cells:
            dim_color = tuple(max(5, c * 2 // 3) for c in color)
            mark_cells[(nx, ny)] = (dim_color, frame_num)


def process_step(s, step_idx):
    """Process one step: create visual effects."""
    name = s["agent"]
    verdict = s.get("guard_verdict")
    if name not in agents:
        return

    info = agents[name]

    # Agent pulse
    if verdict:
        pulse_color = VERDICT_COLORS[verdict]
    else:
        pulse_color = DEPT_COLORS.get(info["dept"], ACCENT)
    agent_pulses[name] = (pulse_color, PULSE_FRAMES)

    if verdict is None:
        return  # read-only tool, no mark

    totals[verdict] += 1

    # Track resource activity
    tool = s.get("tool", "")
    resource = TOOL_RESOURCE.get(tool)
    if resource and verdict:
        res_counts[resource] += 1
        key = _resource_key(s)
        pos = res_index[resource].get(key)
        if pos:
            res_cells[resource][pos] = (VERDICT_COLORS[verdict], frame_num)

    if verdict == "denied":
        _, ay = info["pos"]
        guard_flashes.append([ay, VERDICT_COLORS["denied"], FLASH_FRAMES])
    else:
        mx, my = get_mark_pos(info["dept"], s.get("round_num", 0))
        place_mark(mx, my, VERDICT_COLORS[verdict])


def tick():
    """Age all effects by one frame."""
    for f in guard_flashes:
        f[2] -= 1
    guard_flashes[:] = [f for f in guard_flashes if f[2] > 0]

    expired = [n for n, (_, a) in agent_pulses.items() if a <= 1]
    for n in expired:
        del agent_pulses[n]
    for n in list(agent_pulses):
        c, a = agent_pulses[n]
        agent_pulses[n] = (c, a - 1)


# ── Drawing ───────────────────────────────────────────────────────────────────


def draw_header(draw, current_round, step_idx):
    day = DAY_LABELS.get(current_round, "")
    draw.text((12, 9), f"ROUND {current_round}", font=font_b, fill=ACCENT)
    draw.text((110, 9), day, font=font_b, fill=FG)

    # Agent count
    draw.text((210, 9), f"{total_agents} AGENTS", font=font_b, fill=ACCENT)

    # Progress bar
    pct = step_idx / total_steps if total_steps else 0
    bx, bw = 360, 160
    draw.rectangle([bx, 13, bx + bw, 21], outline=DIM)
    fw = int((bw - 2) * pct)
    if fw > 0:
        draw.rectangle([bx + 1, 14, bx + 1 + fw, 20], fill=ACCENT)
    draw.text((bx + bw + 8, 9), f"{step_idx}/{total_steps}", font=font, fill=FG)

    # Safety badge
    draw.text((W - 208, 9), "SAFETY VIOLATIONS", font=font_b, fill=FG)
    draw.text((W - 18, 7), "0", font=font_b, fill=GREEN)

    draw.line([(0, HDR_H), (W, HDR_H)], fill=DIM)


def draw_agents(draw):
    for di, dept in enumerate(DEPT_ORDER):
        band_y = BODY_Y + di * band_h

        label_color = DEPT_COLORS[dept]
        dept_active = any(n in agent_pulses for n in dept_agents[dept])
        if dept_active:
            label_color = tuple(min(255, c + 40) for c in label_color)
        draw.text((LABEL_X, band_y + 1), dept.upper(), font=font, fill=label_color)

        for name in dept_agents[dept]:
            info = agents[name]
            x, y = info["pos"]

            if name in agent_pulses:
                color, age = agent_pulses[name]
                brightness = age / PULSE_FRAMES
                color = tuple(
                    min(255, int(c * (0.3 + 0.7 * brightness) + 80 * brightness))
                    for c in color
                )
            elif info["is_adv"]:
                color = tuple(c // 4 for c in WARM)
            else:
                color = tuple(c // 6 for c in DEPT_COLORS[dept])

            draw.rectangle([x, y, x + DOT - 1, y + DOT - 1], fill=color)

            if info["is_adv"]:
                draw.rectangle(
                    [x - 1, y - 1, x + DOT, y + DOT],
                    outline=tuple(c // 2 for c in WARM),
                )


def draw_guard(draw):
    for y in range(BODY_Y, BODY_Y + BODY_H):
        if y % 3 < 2:
            draw.rectangle([GW_X, y, GW_X + GW_W - 1, y], fill=FAINT)

    for gy, gc, ga in guard_flashes:
        alpha = ga / FLASH_FRAMES
        flash_color = tuple(int(c * alpha) for c in gc)
        fy = max(BODY_Y, gy - 12)
        draw.rectangle(
            [GW_X - 3, fy, GW_X + GW_W + 2, fy + 28],
            fill=flash_color,
        )


def draw_markspace(draw, current_round):
    day_col_w = (MS_COLS - 6) // 5

    # Day column dividers (drawn under marks)
    day_names = ["MON", "TUE", "WED", "THU", "FRI"]
    current_day = current_round // 2
    for di, day_name in enumerate(day_names):
        dx = MS_X + (3 + di * day_col_w) * MSTEP
        if di > 0:
            draw.line(
                [(dx - 2, BODY_Y), (dx - 2, BODY_Y + BODY_H)],
                fill=(20, 24, 32),
            )

    # Dept-aligned horizontal separators (match left-side bands)
    for di in range(1, N_DEPT_ZONES):
        sy = BODY_Y + di * band_h
        draw.line(
            [(MS_X, sy), (MS_X + MS_W, sy)],
            fill=(15, 18, 25),
        )

    # Mark cells with gentle decay for subtle time progression
    for (mx, my), (color, birth) in mark_cells.items():
        age = frame_num - birth
        # Bright for 10 frames, then slow exponential fade to a visible floor
        if age < 10:
            fade = 1.0
        else:
            fade = max(0.20, 0.92 ** ((age - 10) / 5))
        cc = tuple(max(2, int(c * fade)) for c in color)
        sx = MS_X + mx * MSTEP
        sy = BODY_Y + my * MSTEP
        draw.rectangle([sx, sy, sx + MCELL - 1, sy + MCELL - 1], fill=cc)

    # Day labels drawn ON TOP of marks so they stay readable
    for di, day_name in enumerate(day_names):
        dx = MS_X + (3 + di * day_col_w) * MSTEP
        lx, ly = dx + 2, BODY_Y + 1
        # Dark backing strip behind the label
        draw.rectangle([lx - 1, ly - 1, lx + 30, ly + 12], fill=BG)
        if di == current_day:
            draw.text((lx, ly), day_name, font=font_b, fill=ACCENT)
        elif di < current_day:
            draw.text((lx, ly), day_name, font=font_b, fill=(60, 70, 85))
        else:
            draw.text((lx, ly), day_name, font=font, fill=(30, 36, 46))


def draw_resources(draw):
    """Draw the resource panel on the right side."""
    # Faint vertical separator
    draw.line([(RP_X - 4, BODY_Y), (RP_X - 4, BODY_Y + BODY_H)], fill=DIM)

    for ri, res in enumerate(RESOURCE_ORDER):
        ry = BODY_Y + ri * RP_ROW_H
        rc = RESOURCE_COLORS[res]
        label = RESOURCE_LABELS[res]
        count = res_counts[res]

        # Horizontal separator between resource rows
        if ri > 0:
            draw.line([(RP_X, ry), (RP_X + RP_W, ry)], fill=(15, 18, 25))

        # Mini grid of resource activity cells (drawn first, under labels)
        grid_y = ry + RP_LABEL_H
        cells = res_cells[res]
        for (cx, cy), (color, birth) in cells.items():
            age = frame_num - birth
            if age < 6:
                fade = 1.0
            else:
                fade = max(0.15, 0.88 ** ((age - 6) / 4))
            cc = tuple(max(2, int(c * fade)) for c in color)
            sx = RP_X + 2 + cx * MSTEP
            sy = grid_y + cy * MSTEP
            draw.rectangle([sx, sy, sx + MCELL - 1, sy + MCELL - 1], fill=cc)

        # Dark backing strip over grid to keep labels crisp
        draw.rectangle([RP_X, ry, RP_X + RP_W, ry + RP_LABEL_H], fill=BG)

        # Label and count
        draw.text((RP_X + 2, ry + 1), label, font=font, fill=rc)
        count_str = str(count)
        # Right-align count
        draw.text(
            (RP_X + RP_W - len(count_str) * 7 - 2, ry + 1),
            count_str,
            font=font,
            fill=tuple(c * 2 // 3 for c in rc),
        )


def draw_footer(draw):
    fy = H - FTR_H
    draw.line([(0, fy), (W, fy)], fill=DIM)

    fx = 12
    fy2 = fy + 12
    max_val = max(totals.values()) if any(totals.values()) else 1
    spacing = (W - 24) // 4
    for label, key in [
        ("ALLOW", "allow"),
        ("BLOCKED", "blocked"),
        ("CONFLICT", "conflict"),
        ("DENIED", "denied"),
    ]:
        c = VERDICT_COLORS[key]
        v = totals[key]
        draw.text((fx, fy2), label, font=font, fill=c)
        draw.text((fx + 70, fy2), str(v), font=font_b, fill=c)
        bw = int(80 * v / max(max_val, 1))
        if bw > 0:
            draw.rectangle([fx + 115, fy2 + 3, fx + 115 + bw, fy2 + 9], fill=c)
        fx += spacing


def render_frame(current_round, step_idx):
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw_header(draw, current_round, step_idx)
    draw_agents(draw)
    draw_guard(draw)
    draw_markspace(draw, current_round)
    draw_resources(draw)
    draw_footer(draw)
    return img


# ── Build animation ──────────────────────────────────────────────────────────

frames = []
durations = []

current_round = -1
step_idx = 0
batch = []

print(f"Processing {total_steps} steps ({total_agents} agents, {n_rounds} rounds)")
print(f"Steps per frame: {STEPS_PER_FRAME}")

for s in steps_data:
    rn = s["round_num"]

    if rn != current_round:
        if batch:
            tick()
            frames.append(render_frame(current_round, step_idx))
            durations.append(60)
            frame_num += 1
            batch = []
        # Smooth transition between rounds
        if current_round >= 0 and rn > current_round:
            is_day_change = current_round % 2 == 1  # PM -> AM
            if is_day_change:
                # Day change: more frames, ease in/out
                trans_durations = [70, 90, 120, 150, 150, 120, 90, 70]
            else:
                # AM -> PM: gentle pause
                trans_durations = [70, 100, 120, 100, 70]
            for td in trans_durations:
                tick()
                frame_num += 1
                frames.append(render_frame(current_round, step_idx))
                durations.append(td)
        current_round = rn

    process_step(s, step_idx)
    step_idx += 1
    batch.append(s)

    if len(batch) >= STEPS_PER_FRAME:
        tick()
        frames.append(render_frame(current_round, step_idx))
        durations.append(60)
        frame_num += 1
        batch = []

# Flush remaining
if batch:
    tick()
    frames.append(render_frame(current_round, step_idx))
    durations.append(60)
    frame_num += 1

# Hold on final frame
if durations:
    durations[-1] = 2000

# Output path
out_path = results_dir / "stress_test.gif"
print(f"Rendering {len(frames)} frames to {out_path}...")
frames[0].save(
    str(out_path),
    save_all=True,
    append_images=frames[1:],
    duration=durations,
    loop=0,
    optimize=True,
)
print(f"Done: {out_path} ({len(frames)} frames)")
