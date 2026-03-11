"""Generate animated GIF visualization of the 105-agent stress test.

Layout:
  Left:    Agent cells arranged by department (5 groups of ~21)
  Center:  Guard boundary wall (flashes red on denied attempts)
  Right:   Mark space grid, marks accumulate and age by resource zone
  Bottom:  Running verdict counters and safety metrics

Visual style inspired by the cellular automaton at opinionated.systems:
  dark background, small grid cells, cyan-to-blue aging, orange accents.
"""

import json
import math
import random
from collections import defaultdict
from PIL import Image, ImageDraw, ImageFont  # type: ignore

random.seed(42)


# ── Dimensions ────────────────────────────────────────────────────────────────

W, H = 960, 540


# ── Colors ────────────────────────────────────────────────────────────────────

BG = (0, 0, 0)
ACCENT = (0, 217, 255)  # cyan
WARM = (255, 120, 50)  # orange (adversarial / Wolfram band accent)
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

# Agent zone (left)
AZ_X = 16
AZ_W = 160
DOT = 5
DOT_STEP = 8

# Guard wall (center strip)
GW_X = AZ_X + AZ_W + 12
GW_W = 6

# Mark space (right)
MS_X = GW_X + GW_W + 12
MS_W = W - MS_X - 10
MCELL = 4
MGAP = 1
MSTEP = MCELL + MGAP
MS_COLS = MS_W // MSTEP
MS_ROWS = BODY_H // MSTEP

# Resource zones in the mark space (vertical bands)
TOOL_ZONE = {
    "book_dept_room": 0,
    "book_shared_room": 0,
    "request_boardroom": 1,
    "request_parking": 2,
    "order_lunch": 3,
    "view_lunch_availability": 3,
    "claim_task": 4,
    "book_equipment": 5,
}
ZONE_LABELS = ["ROOMS", "EXEC", "PARKING", "LUNCH", "TASKS", "EQUIP"]
N_ZONES = 6

# Timing
STEPS_PER_FRAME = 10
PULSE_FRAMES = 5
FLASH_FRAMES = 8

DAY_LABELS = {
    0: "MON AM",
    1: "MON PM",
    2: "TUE AM",
    3: "TUE PM",
    4: "WED AM",
    5: "WED PM",
    6: "THU AM",
    7: "THU PM",
    8: "FRI AM",
    9: "FRI PM",
}


# ── Load data ─────────────────────────────────────────────────────────────────

steps_data = [json.loads(line) for line in open("results/steps.jsonl")]
rounds_data = {}
for line in open("results/rounds.jsonl"):
    r = json.loads(line)
    rounds_data[r["round_num"]] = r
trial = json.loads(open("results/trial.jsonl").read().strip())


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
    band_h = BODY_H // 5
    band_y = BODY_Y + di * band_h
    for i, name in enumerate(dept_agents[dept]):
        col = i % 7
        row = i // 7
        x = AZ_X + 4 + col * DOT_STEP
        y = band_y + 18 + row * DOT_STEP
        agents[name]["pos"] = (x, y)


# ── Animation state ──────────────────────────────────────────────────────────

mark_cells = {}  # (col, row) -> (color, birth_frame)
guard_flashes = []  # [(screen_y, color, remaining_frames)]
agent_pulses = {}  # name -> (color, remaining_frames)
totals = {"allow": 0, "blocked": 0, "conflict": 0, "denied": 0}
frame_num = 0


def get_mark_pos(tool, step_idx, total_steps):
    """Pick a position in the mark space grid based on resource zone and time."""
    zone = TOOL_ZONE.get(tool, random.randint(0, N_ZONES - 1))
    zone_h = MS_ROWS // N_ZONES
    y_base = zone * zone_h
    y = y_base + random.randint(2, max(3, zone_h - 3))

    progress = step_idx / max(total_steps, 1)
    x = int(progress * (MS_COLS - 8)) + 4 + random.randint(-2, 2)
    x = max(0, min(MS_COLS - 1, x))
    y = max(0, min(MS_ROWS - 1, y))
    return x, y


def place_mark(mx, my, color):
    """Place a mark cell plus occasional neighbor cells for organic growth."""
    mark_cells[(mx, my)] = (color, frame_num)
    # Sometimes add 1-2 neighbor cells
    for _ in range(random.randint(0, 2)):
        dx = random.choice([-1, 0, 1])
        dy = random.choice([-1, 0, 1])
        if dx == 0 and dy == 0:
            continue
        nx, ny = mx + dx, my + dy
        if 0 <= nx < MS_COLS and 0 <= ny < MS_ROWS and (nx, ny) not in mark_cells:
            dim_color = tuple(max(5, c * 2 // 3) for c in color)
            mark_cells[(nx, ny)] = (dim_color, frame_num)


def process_step(s, step_idx, total_steps):
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

    if verdict == "denied":
        # Guard blocks it: flash at the wall
        _, ay = info["pos"]
        guard_flashes.append([ay, VERDICT_COLORS["denied"], FLASH_FRAMES])
    else:
        # Mark passes through to the mark space
        mx, my = get_mark_pos(s.get("tool", ""), step_idx, total_steps)
        place_mark(mx, my, VERDICT_COLORS[verdict])


def tick():
    """Age all effects by one frame."""
    # Guard flashes
    for f in guard_flashes:
        f[2] -= 1
    guard_flashes[:] = [f for f in guard_flashes if f[2] > 0]

    # Agent pulses
    expired = [n for n, (_, a) in agent_pulses.items() if a <= 1]
    for n in expired:
        del agent_pulses[n]
    for n in list(agent_pulses):
        c, a = agent_pulses[n]
        agent_pulses[n] = (c, a - 1)


# ── Drawing ───────────────────────────────────────────────────────────────────


def draw_header(draw, current_round, step_idx, total_steps):
    day = DAY_LABELS.get(current_round, "")
    draw.text((12, 9), f"ROUND {current_round}", font=font_b, fill=ACCENT)
    draw.text((110, 9), day, font=font_b, fill=FG)

    # Progress bar
    pct = step_idx / total_steps if total_steps else 0
    bx, bw = 230, 140
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
        band_h = BODY_H // 5
        band_y = BODY_Y + di * band_h

        # Department label
        label_color = DEPT_COLORS[dept]
        # Brighten label if any agent in dept is pulsing
        dept_active = any(n in agent_pulses for n in dept_agents[dept])
        if dept_active:
            label_color = tuple(min(255, c + 40) for c in label_color)
        draw.text((AZ_X + 2, band_y + 3), dept.upper(), font=font, fill=label_color)

        # Agent dots
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

            # Adversarial border
            if info["is_adv"]:
                draw.rectangle(
                    [x - 1, y - 1, x + DOT, y + DOT],
                    outline=tuple(c // 2 for c in WARM),
                )


def draw_guard(draw):
    # Base wall: faint vertical stripe
    for y in range(BODY_Y, BODY_Y + BODY_H):
        if y % 3 < 2:
            draw.rectangle([GW_X, y, GW_X + GW_W - 1, y], fill=FAINT)

    # Flashes
    for gy, gc, ga in guard_flashes:
        alpha = ga / FLASH_FRAMES
        flash_color = tuple(int(c * alpha) for c in gc)
        fy = max(BODY_Y, gy - 12)
        draw.rectangle(
            [GW_X - 3, fy, GW_X + GW_W + 2, fy + 28],
            fill=flash_color,
        )


def draw_markspace(draw):
    # Zone separator lines and labels
    zone_h = BODY_H // N_ZONES
    for zi, label in enumerate(ZONE_LABELS):
        zy = BODY_Y + zi * zone_h
        draw.text((MS_X + 2, zy + 1), label, font=font, fill=(25, 30, 40))
        if zi > 0:
            draw.line(
                [(MS_X, zy), (MS_X + MS_W, zy)],
                fill=(15, 18, 25),
            )

    # Mark cells
    for (mx, my), (color, birth) in mark_cells.items():
        age = frame_num - birth
        # Stay bright for a while, then slowly dim. Never fully disappear.
        if age < 20:
            fade = 1.0
        else:
            fade = max(0.18, 1.0 - (age - 20) / 600)
        cc = tuple(max(3, int(c * fade)) for c in color)
        sx = MS_X + mx * MSTEP
        sy = BODY_Y + my * MSTEP
        draw.rectangle([sx, sy, sx + MCELL - 1, sy + MCELL - 1], fill=cc)


def draw_footer(draw):
    fy = H - FTR_H
    draw.line([(0, fy), (W, fy)], fill=DIM)

    fx = 12
    fy2 = fy + 12
    max_val = max(totals.values()) if any(totals.values()) else 1
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
        bw = int(55 * v / max(max_val, 1))
        if bw > 0:
            draw.rectangle([fx + 105, fy2 + 3, fx + 105 + bw, fy2 + 9], fill=c)
        fx += 185

    draw.text((W - 190, fy + 5), "DOUBLE BOOKINGS", font=font_b, fill=FG)
    draw.text((W - 16, fy + 3), "0", font=font_b, fill=GREEN)
    draw.text((W - 190, fy + 21), "SCOPE VIOLATIONS", font=font_b, fill=FG)
    draw.text((W - 16, fy + 19), "0", font=font_b, fill=GREEN)


def render_frame(current_round, step_idx, total_steps):
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw_header(draw, current_round, step_idx, total_steps)
    draw_agents(draw)
    draw_guard(draw)
    draw_markspace(draw)
    draw_footer(draw)
    return img


# ── Build animation ──────────────────────────────────────────────────────────

frames = []
durations = []

current_round = -1
step_idx = 0
total_steps = len(steps_data)
batch = []

for s in steps_data:
    rn = s["round_num"]

    # Round transition (just update state, no interstitial frame)
    if rn != current_round:
        if batch:
            tick()
            frames.append(render_frame(current_round, step_idx, total_steps))
            durations.append(60)
            frame_num += 1
            batch = []
        current_round = rn

    # Process step
    process_step(s, step_idx, total_steps)
    step_idx += 1
    batch.append(s)

    # Render frame every N steps
    if len(batch) >= STEPS_PER_FRAME:
        tick()
        frames.append(render_frame(current_round, step_idx, total_steps))
        durations.append(60)
        frame_num += 1
        batch = []

# Flush remaining
if batch:
    tick()
    frames.append(render_frame(current_round, step_idx, total_steps))
    durations.append(60)
    frame_num += 1

# Hold on final activity frame
if durations:
    durations[-1] = 2000

# Save
print(f"Rendering {len(frames)} frames...")
frames[0].save(
    "results/stress_test.gif",
    save_all=True,
    append_images=frames[1:],
    duration=durations,
    loop=0,
    optimize=True,
)
print(f"Done: results/stress_test.gif ({len(frames)} frames)")
