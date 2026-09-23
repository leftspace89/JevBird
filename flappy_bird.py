import math
import os
import random
import sys

import pygame

from jev_pilot import JevPilot, build_questions, build_state, load_api_key, load_base_url, log_banner, log_event

WIDTH, HEIGHT = 400, 600
FPS = 60
GROUND_HEIGHT = 80
GRAVITY = 1800.0
FLAP_VELOCITY = -420.0
MAX_FALL_SPEED = 700.0
PIPE_WIDTH = 70
PIPE_GAP = 160
PIPE_SPEED = 160.0
PIPE_SPAWN_INTERVAL = 1.5
GROUND_SPEED = PIPE_SPEED
GAP_MARGIN = 60
SIM_DT = 1 / 60
JEV_STEP_FRAMES = 7
JEV_STEP = JEV_STEP_FRAMES * SIM_DT
JEV_PATH_TAIL = 0.6
JEV_REPLAN_LEAD = 1.3
JEV_REPLAN_LEAD_FRAMES = round(JEV_REPLAN_LEAD / SIM_DT)
JEV_MAX_LATE_FRAMES = 21
JEV_DIVERGENCE_PX = 35
JEV_EMERGENCY_FRAMES = 18
JEV_PATH_COLOR = (150, 90, 220)
JEV_ALT_COLOR = (255, 255, 255)
JEV_BAND_COLOR = (150, 90, 220, 40)
JEV_MAX_STEPS = 40
JEV_POLICY_BANDS = range(-50, 51, 10)
JEV_POLICY_SPEEDS = (-150, -50, 50, 150, 300)
JEV_RESTART_DELAY = 1.5
JEV_PURPLE = (150, 90, 220)

SKY = (112, 197, 206)
GROUND_TOP = (222, 216, 149)
GROUND_STRIPE = (200, 180, 100)
PIPE_GREEN = (86, 176, 60)
PIPE_DARK = (58, 128, 40)
BIRD_YELLOW = (250, 210, 50)
BIRD_ORANGE = (240, 130, 30)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

HIGHSCORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "highscore.txt")

START, PLAYING, GAME_OVER = range(3)


def random_gap_top():
    return random.randint(GAP_MARGIN, HEIGHT - GROUND_HEIGHT - PIPE_GAP - GAP_MARGIN)


class Bird:
    RADIUS = 16

    def __init__(self):
        self.x = WIDTH * 0.3
        self.y = HEIGHT * 0.45
        self.velocity = 0.0
        self.angle = 0.0
        self.time = 0.0

    def flap(self):
        self.velocity = FLAP_VELOCITY

    def update(self, dt):
        self.velocity = min(self.velocity + GRAVITY * dt, MAX_FALL_SPEED)
        self.y += self.velocity * dt
        target = max(-30.0, min(90.0, self.velocity / 8.0))
        self.angle += (target - self.angle) * min(1.0, dt * 10)

    def bob(self, dt):
        self.time += dt
        self.y = HEIGHT * 0.45 + math.sin(self.time * 4) * 8
        self.angle = 0.0

    @property
    def rect(self):
        r = self.RADIUS - 3
        return pygame.Rect(int(self.x - r), int(self.y - r), r * 2, r * 2)

    def draw(self, surface):
        size = self.RADIUS * 2 + 12
        temp = pygame.Surface((size, size), pygame.SRCALPHA)
        cx = cy = size // 2
        pygame.draw.circle(temp, BIRD_YELLOW, (cx, cy), self.RADIUS)
        pygame.draw.circle(temp, BLACK, (cx, cy), self.RADIUS, 2)
        pygame.draw.ellipse(temp, WHITE, (cx - 2, cy - 12, 14, 10))
        pygame.draw.circle(temp, BLACK, (cx + 6, cy - 7), 3)
        pygame.draw.polygon(temp, BIRD_ORANGE, [(cx + 10, cy - 1), (cx + 24, cy + 3), (cx + 10, cy + 7)])
        pygame.draw.ellipse(temp, BIRD_ORANGE, (cx - 16, cy - 2, 16, 10))
        rotated = pygame.transform.rotate(temp, -self.angle)
        surface.blit(rotated, rotated.get_rect(center=(int(self.x), int(self.y))))


class Pipe:
    def __init__(self, x, gap_top, pipe_id):
        self.x = float(x)
        self.gap_top = gap_top
        self.pipe_id = pipe_id
        self.passed = False

    def update(self, dt):
        self.x -= PIPE_SPEED * dt

    @property
    def off_screen(self):
        return self.x + PIPE_WIDTH < 0

    @property
    def rects(self):
        x = int(self.x)
        top = pygame.Rect(x, 0, PIPE_WIDTH, self.gap_top)
        bottom_y = self.gap_top + PIPE_GAP
        bottom = pygame.Rect(x, bottom_y, PIPE_WIDTH, HEIGHT - GROUND_HEIGHT - bottom_y)
        return top, bottom

    def draw(self, surface):
        top, bottom = self.rects
        for body in (top, bottom):
            pygame.draw.rect(surface, PIPE_GREEN, body)
            pygame.draw.rect(surface, PIPE_DARK, body, 3)
        rim_h = 24
        top_rim = pygame.Rect(top.x - 4, top.bottom - rim_h, PIPE_WIDTH + 8, rim_h)
        bottom_rim = pygame.Rect(bottom.x - 4, bottom.y, PIPE_WIDTH + 8, rim_h)
        for rim in (top_rim, bottom_rim):
            pygame.draw.rect(surface, PIPE_GREEN, rim)
            pygame.draw.rect(surface, PIPE_DARK, rim, 3)


class Snapshot:
    def __init__(self, game):
        self.x = game.bird.x
        self.y = game.bird.y
        self.v = game.bird.velocity
        self.pipes = [[p.x, p.gap_top, p.pipe_id] for p in game.pipes]
        self.spawn_timer = game.spawn_timer
        self.next_gap_top = game.next_gap_top
        self.next_pipe_id = game.pipe_counter + 1

    def copy(self):
        snap = Snapshot.__new__(Snapshot)
        snap.__dict__.update(self.__dict__)
        snap.pipes = [list(p) for p in self.pipes]
        return snap

    def flap(self):
        self.v = FLAP_VELOCITY

    def step(self, dt):
        self.v = min(self.v + GRAVITY * dt, MAX_FALL_SPEED)
        self.y += self.v * dt
        self.spawn_timer += dt
        if self.spawn_timer >= PIPE_SPAWN_INTERVAL:
            self.spawn_timer -= PIPE_SPAWN_INTERVAL
            self.pipes.append([WIDTH + 10, self.next_gap_top, self.next_pipe_id])
            self.next_pipe_id += 1
            self.next_gap_top = (HEIGHT - GROUND_HEIGHT - PIPE_GAP) // 2
        for pipe in self.pipes:
            pipe[0] -= PIPE_SPEED * dt

    def target_pipe(self):
        for pipe in self.pipes:
            if pipe[0] + PIPE_WIDTH > self.x - Bird.RADIUS:
                return pipe
        return None

    def target_id(self):
        pipe = self.target_pipe()
        return self.next_pipe_id if pipe is None else pipe[2]

    def seconds_to_clear_target(self):
        pipe = self.target_pipe()
        if pipe is None:
            spawn_in = PIPE_SPAWN_INTERVAL - self.spawn_timer
            return spawn_in + (WIDTH + 10 + PIPE_WIDTH - (self.x - Bird.RADIUS)) / PIPE_SPEED
        return (pipe[0] + PIPE_WIDTH - (self.x - Bird.RADIUS)) / PIPE_SPEED

    def wants_safety_flap(self):
        return self.y > self.target_center() and self.v > 0

    def target_center(self):
        pipe = self.target_pipe()
        gap_top = self.next_gap_top if pipe is None else pipe[1]
        return gap_top + PIPE_GAP / 2

    def crashed(self):
        r = Bird.RADIUS - 3
        if self.y + Bird.RADIUS >= HEIGHT - GROUND_HEIGHT or self.y - Bird.RADIUS <= 0:
            return True
        for px, gap_top, _ in self.pipes:
            if self.x + r > px and self.x - r < px + PIPE_WIDTH:
                if self.y - r < gap_top or self.y + r > gap_top + PIPE_GAP:
                    return True
        return False


class PlanResult:
    def __init__(self, plan):
        self.plan = plan
        self.offsets = []
        self.ys = []
        self.worst_offset = 0.0
        self.end_velocity = 0.0
        self.crashed = False
        self.crash_step = None


def policy_plan(snapshot, steps, band, min_speed):
    snap = snapshot.copy()
    plan = []
    for _ in range(steps):
        offset = snap.y - snap.target_center()
        flap = offset > band and snap.v > min_speed
        plan.append(flap)
        if flap:
            snap.flap()
        for _ in range(JEV_STEP_FRAMES):
            snap.step(SIM_DT)
    return tuple(plan)


def candidate_plans(snapshot, steps):
    plans = set()
    for band in JEV_POLICY_BANDS:
        for min_speed in JEV_POLICY_SPEEDS:
            plans.add(policy_plan(snapshot, steps, band, min_speed))
    return plans


def simulate_plan(snapshot, plan):
    snap = snapshot.copy()
    result = PlanResult(plan)
    for step, flap in enumerate(plan):
        if flap:
            snap.flap()
        for _ in range(JEV_STEP_FRAMES):
            snap.step(SIM_DT)
            result.ys.append(snap.y)
            offset = snap.y - snap.target_center()
            if abs(offset) > abs(result.worst_offset):
                result.worst_offset = offset
            if not result.crashed and snap.crashed():
                result.crashed = True
                result.crash_step = step
        result.offsets.append(snap.y - snap.target_center())
    result.end_velocity = snap.v
    return result


def load_high_score():
    try:
        with open(HIGHSCORE_FILE) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def save_high_score(score):
    try:
        with open(HIGHSCORE_FILE, "w") as f:
            f.write(str(score))
    except OSError:
        pass


class Game:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        pygame.display.set_caption("Flappy Bird")
        self.clock = pygame.time.Clock()
        self.big_font = pygame.font.SysFont("arial", 48, bold=True)
        self.font = pygame.font.SysFont("arial", 24, bold=True)
        self.small_font = pygame.font.SysFont("arial", 16, bold=True)
        self.high_score = load_high_score()
        self.ground_offset = 0.0
        api_key = load_api_key()
        base_url = load_base_url()
        self.pilot = JevPilot(api_key, base_url) if api_key or base_url else None
        self.jev_mode = False
        self.jev_error = None
        self.life = 0
        self.reset()

    def reset(self):
        self.life += 1
        self.bird = Bird()
        self.pipes = []
        self.score = 0
        self.spawn_timer = 0.0
        self.next_gap_top = random_gap_top()
        self.pipe_counter = 0
        self.state = START
        self.frame = 0
        self.accumulator = 0.0
        self.flap_schedule = []
        self.plan_end = 0
        self.plan_steps = ()
        self.plan_start = 0
        self.pending_plan = None
        self.path_start = 0
        self.path_ys = []
        self.alt_paths = []
        self.jev_restart_timer = 0.0
        self.fallback_flaps = 0
        self.stale_plans = 0
        self.emergency_flaps = 0
        self.abandoned_plans = 0

    def toggle_jev(self):
        if self.pilot is None:
            self.jev_error = "No API key: set TYPESAFE_API_KEY or JEV_BASE_URL"
            return
        self.jev_mode = not self.jev_mode
        self.jev_error = None
        self.flap_schedule = []
        if self.jev_mode:
            log_banner(f"local {self.pilot.base_url}" if self.pilot.base_url else "jev-latest")
        else:
            log_event("  Jev mode off, back to manual control", "gray")
        if self.jev_mode and self.state == START:
            self.handle_action()

    def handle_action(self):
        if self.state == START:
            self.state = PLAYING
            self.bird.flap()
        elif self.state == PLAYING:
            self.bird.flap()
        else:
            self.reset()

    def handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return False
                if event.key == pygame.K_j:
                    self.toggle_jev()
                elif event.key in (pygame.K_SPACE, pygame.K_UP):
                    self.handle_action()
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self.handle_action()
        return True

    @property
    def game_time(self):
        return self.frame * SIM_DT

    def jev_request(self):
        delay_frames = math.ceil(self.pilot.expected_delay() / SIM_DT)
        delay = delay_frames * SIM_DT
        snap = Snapshot(self)
        pending = [f for f in self.flap_schedule if self.frame <= f < self.frame + delay_frames]
        for f in range(self.frame, self.frame + delay_frames):
            if pending and pending[0] == f:
                pending.pop(0)
                snap.flap()
            elif f >= self.plan_end and snap.wants_safety_flap():
                snap.flap()
            snap.step(SIM_DT)
        horizon = snap.seconds_to_clear_target()
        if horizon < JEV_REPLAN_LEAD:
            horizon += PIPE_SPAWN_INTERVAL
        horizon += JEV_PATH_TAIL
        steps = max(4, min(JEV_MAX_STEPS, math.ceil(horizon / JEV_STEP)))
        results = [simulate_plan(snap, plan) for plan in candidate_plans(snap, steps)]
        target = snap.target_pipe()
        distance = None if target is None else max(0.0, target[0] - snap.x)
        target_gap = snap.next_gap_top if target is None else target[1]
        following = None
        if target is not None:
            index = snap.pipes.index(target)
            if index + 1 < len(snap.pipes):
                following = snap.pipes[index + 1][1] - target_gap
            else:
                following = snap.next_gap_top - target_gap
        arena = (
            f"screen {WIDTH}x{HEIGHT} px, flyable height {HEIGHT - GROUND_HEIGHT} px between ceiling and "
            f"ground, pipe gap {PIPE_GAP} px tall, bird {Bird.RADIUS * 2} px wide, pipes move "
            f"{round(PIPE_SPEED)} px/s and are {round(PIPE_SPEED * PIPE_SPAWN_INTERVAL)} px apart"
        )
        state = build_state(
            snap.y - snap.target_center(), snap.v, distance, following, JEV_STEP, delay, steps, arena
        )
        questions, candidates = build_questions(results)
        tag = {
            "life": self.life,
            "score": self.score,
            "time": self.game_time,
            "simulated": len(results),
            "target": snap.target_id(),
            "start": self.frame + delay_frames,
            "sent": self.frame,
            "y": snap.y,
            "v": snap.v,
            "offset": snap.y - snap.target_center(),
        }
        self.pilot.request(state, questions, candidates, tag)

    def jev_apply(self, decision):
        start = decision.tag["start"]
        if decision.tag["life"] != self.life or start < self.frame - JEV_MAX_LATE_FRAMES:
            self.stale_plans += 1
            return
        self.flap_schedule = [f for f in self.flap_schedule if f < start]
        for i, flap in enumerate(decision.plan):
            if flap:
                self.flap_schedule.append(start + i * JEV_STEP_FRAMES)
        self.flap_schedule.sort()
        self.pending_plan = (start, decision.plan)
        self.path_start = start
        self.path_ys = decision.candidates[decision.key].ys
        self.alt_paths = [
            (result.ys, decision.probabilities.get(key, 0.0))
            for key, result in decision.candidates.items()
            if key != decision.key
        ]
        self.jev_promote_plan()

    def jev_promote_plan(self):
        if self.pending_plan is not None and self.frame >= self.pending_plan[0]:
            self.plan_start, self.plan_steps = self.pending_plan
            self.plan_end = self.plan_start + len(self.plan_steps) * JEV_STEP_FRAMES
            self.pending_plan = None

    def jev_fallback(self):
        if Snapshot(self).wants_safety_flap():
            self.bird.flap()
            self.fallback_flaps += 1

    def planned_y(self):
        index = self.frame - self.path_start - 1
        if 0 <= index < len(self.path_ys):
            return self.path_ys[index]
        return None

    def abandon_plan(self):
        self.flap_schedule = []
        self.pending_plan = None
        self.plan_steps = ()
        self.plan_end = self.frame
        self.path_ys = []
        self.alt_paths = []
        self.abandoned_plans += 1

    def jev_check_plan(self):
        planned = self.planned_y()
        if planned is not None and abs(self.bird.y - planned) > JEV_DIVERGENCE_PX:
            drift = round(self.bird.y - planned)
            log_event(f"  drifted {abs(drift)} px off the planned path, asking Jev again", "yellow")
            self.abandon_plan()

    def jev_emergency(self):
        if self.bird.velocity <= 0:
            return False
        snap = Snapshot(self)
        upcoming = [f for f in self.flap_schedule if f < self.frame + JEV_EMERGENCY_FRAMES]
        for f in range(self.frame, self.frame + JEV_EMERGENCY_FRAMES):
            if upcoming and upcoming[0] == f:
                upcoming.pop(0)
                snap.flap()
            snap.step(SIM_DT)
            if snap.crashed():
                return True
        return False

    def jev_tick(self):
        error = self.pilot.take_error()
        if error:
            self.jev_error = error
        for decision in sorted(self.pilot.take_results(), key=lambda d: d.tag["start"]):
            self.jev_apply(decision)
        self.jev_promote_plan()
        self.jev_check_plan()
        flapped = False
        while self.flap_schedule and self.flap_schedule[0] <= self.frame:
            due = self.flap_schedule.pop(0)
            if self.frame - due < JEV_STEP_FRAMES:
                self.bird.flap()
                flapped = True
        if not flapped and self.jev_emergency():
            log_event("  safety flap, a crash was coming before the next plan", "yellow")
            self.bird.flap()
            self.emergency_flaps += 1
            self.abandon_plan()
            flapped = True
        if not flapped and self.frame >= self.plan_end:
            self.jev_fallback()
        needs_plan = self.pending_plan is None and self.frame >= self.plan_end - JEV_REPLAN_LEAD_FRAMES
        if needs_plan and not self.pilot.busy:
            self.jev_request()

    def update(self, dt):
        if self.state != GAME_OVER:
            self.ground_offset = (self.ground_offset + GROUND_SPEED * dt) % 40

        if self.state == START:
            self.bird.bob(dt)
            return
        if self.state == GAME_OVER:
            if self.jev_mode:
                self.jev_restart_timer += dt
                if self.jev_restart_timer >= JEV_RESTART_DELAY:
                    self.reset()
                    self.handle_action()
            return

        self.accumulator += dt
        while self.accumulator >= SIM_DT and self.state == PLAYING:
            self.accumulator -= SIM_DT
            self.physics_step()

    def physics_step(self):
        if self.jev_mode:
            self.jev_tick()

        self.frame += 1
        self.bird.update(SIM_DT)
        self.spawn_timer += SIM_DT
        if self.spawn_timer >= PIPE_SPAWN_INTERVAL:
            self.spawn_timer -= PIPE_SPAWN_INTERVAL
            self.pipe_counter += 1
            self.pipes.append(Pipe(WIDTH + 10, self.next_gap_top, self.pipe_counter))
            self.next_gap_top = random_gap_top()

        for pipe in self.pipes:
            pipe.update(SIM_DT)
            if not pipe.passed and pipe.x + PIPE_WIDTH < self.bird.x:
                pipe.passed = True
                self.score += 1
                if self.jev_mode:
                    log_event(f"  pipe cleared, score {self.score}", "green")
        self.pipes = [p for p in self.pipes if not p.off_screen]

        self.check_collisions()

    def check_collisions(self):
        bird_rect = self.bird.rect
        hit = self.bird.y + Bird.RADIUS >= HEIGHT - GROUND_HEIGHT or self.bird.y - Bird.RADIUS <= 0
        if not hit:
            hit = any(bird_rect.colliderect(r) for p in self.pipes for r in p.rects)
        if hit:
            self.state = GAME_OVER
            if self.jev_mode:
                log_event(
                    f"  crashed with {self.score} pipes after {self.pilot.requests} Jev requests", "red"
                )
            if self.score > self.high_score:
                self.high_score = self.score
                save_high_score(self.high_score)

    def draw_text(self, text, font, y, color=WHITE):
        shadow = font.render(text, True, BLACK)
        self.screen.blit(shadow, shadow.get_rect(center=(WIDTH // 2 + 2, int(y) + 2)))
        surf = font.render(text, True, color)
        self.screen.blit(surf, surf.get_rect(center=(WIDTH // 2, int(y))))

    def draw_ground(self):
        top = HEIGHT - GROUND_HEIGHT
        pygame.draw.rect(self.screen, GROUND_TOP, (0, top, WIDTH, GROUND_HEIGHT))
        pygame.draw.line(self.screen, PIPE_DARK, (0, top), (WIDTH, top), 3)
        x = -int(self.ground_offset)
        while x < WIDTH + 40:
            pygame.draw.polygon(
                self.screen,
                GROUND_STRIPE,
                [(x, top + 4), (x + 20, top + 4), (x + 10, top + 18), (x - 10, top + 18)],
            )
            x += 40

    def path_points(self, ys, start):
        points = []
        for i, y in enumerate(ys):
            x = self.bird.x + PIPE_SPEED * (start + i + 1 - self.frame) * SIM_DT
            if x < self.bird.x - 4:
                continue
            if x > WIDTH + 20:
                break
            points.append((int(x), int(y)))
        return points

    def draw_jev_paths(self):
        snap = Snapshot(self)
        pipe = snap.target_pipe()
        center = snap.target_center()
        band_x = int(pipe[0]) - 30 if pipe is not None else WIDTH - 60
        band = pygame.Surface((PIPE_WIDTH + 60, 2 * 55), pygame.SRCALPHA)
        band.fill(JEV_BAND_COLOR)
        self.screen.blit(band, (band_x, int(center) - 55))
        pygame.draw.line(self.screen, JEV_PATH_COLOR, (band_x, int(center)), (band_x + PIPE_WIDTH + 60, int(center)), 1)

        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        for ys, probability in self.alt_paths:
            points = self.path_points(ys, self.path_start)
            if len(points) > 1:
                alpha = 40 + int(160 * probability)
                pygame.draw.lines(overlay, (*JEV_ALT_COLOR, alpha), False, points, 1)
        self.screen.blit(overlay, (0, 0))

        points = self.path_points(self.path_ys, self.path_start)
        if len(points) > 1:
            pygame.draw.lines(self.screen, JEV_PATH_COLOR, False, points, 3)
        for f in self.flap_schedule:
            x = self.bird.x + PIPE_SPEED * (f - self.frame) * SIM_DT
            i = f - self.path_start - 1
            if 0 <= i < len(self.path_ys) and self.bird.x <= x <= WIDTH:
                y = int(self.path_ys[i])
                pygame.draw.polygon(self.screen, WHITE, [(int(x), y - 10), (int(x) - 5, y - 2), (int(x) + 5, y - 2)])

    def blit_text(self, text, color, left=None, right=None, top=0):
        surf = self.small_font.render(text, True, color)
        rect = surf.get_rect(top=top)
        if right is None:
            rect.left = left
        else:
            rect.right = right
        self.screen.blit(surf, rect)

    def draw_jev_panel(self):
        top = HEIGHT - GROUND_HEIGHT
        panel = pygame.Rect(8, top + 14, WIDTH - 16, GROUND_HEIGHT - 22)
        pygame.draw.rect(self.screen, (40, 30, 60), panel, border_radius=6)
        pygame.draw.rect(self.screen, JEV_PURPLE, panel, 2, border_radius=6)
        decision = self.pilot.last_decision
        muted = (200, 190, 210)

        self.blit_text("JEV plans each pipe", JEV_PURPLE, left=panel.x + 8, top=panel.y + 3)
        counters = (
            f"calls {self.pilot.requests}  saves {self.fallback_flaps + self.emergency_flaps}"
            f"  replans {self.abandoned_plans}"
        )
        self.blit_text(counters, muted, right=panel.right - 8, top=panel.y + 3)

        cell, gap = 6, 2
        steps = self.plan_steps[:JEV_MAX_STEPS]
        current = (self.frame - self.plan_start) // JEV_STEP_FRAMES if steps else -1
        for i, flap in enumerate(steps):
            box = pygame.Rect(panel.x + 8 + i * (cell + gap), panel.y + 23, cell, 11)
            pygame.draw.rect(self.screen, JEV_PURPLE if flap else (80, 70, 100), box)
            if i == current:
                pygame.draw.rect(self.screen, WHITE, box, 1)

        if decision is None:
            status = "planning the first pipe..."
        else:
            status = (
                f"p={decision.probability:.2f} conf={decision.confidence:.2f} "
                f"{round(decision.latency * 1000)} ms"
            )
        self.blit_text(status, WHITE, left=panel.x + 8, top=panel.y + 40)
        danger = 0.0 if decision is None else decision.danger
        bar = pygame.Rect(panel.right - 88, panel.y + 45, 80, 8)
        pygame.draw.rect(self.screen, (80, 70, 100), bar)
        pygame.draw.rect(
            self.screen,
            (230, 80, 80) if danger > 0.5 else JEV_PURPLE,
            pygame.Rect(bar.x, bar.y, int(bar.width * danger), bar.height),
        )
        self.blit_text("danger", muted, right=bar.x - 6, top=panel.y + 40)

    def draw(self):
        self.screen.fill(SKY)
        for pipe in self.pipes:
            pipe.draw(self.screen)
        self.draw_ground()
        if self.jev_mode and self.pilot is not None and self.state == PLAYING:
            self.draw_jev_paths()
        self.bird.draw(self.screen)

        if self.state == START:
            self.draw_text("Flappy Bird", self.big_font, HEIGHT * 0.22)
            self.draw_text("Press SPACE or click to start", self.font, HEIGHT * 0.65)
            self.draw_text("Press J to let Jev play", self.font, HEIGHT * 0.71)
            self.draw_text(f"High score: {self.high_score}", self.font, HEIGHT * 0.78)
        elif self.state == PLAYING:
            self.draw_text(str(self.score), self.big_font, 60)
        else:
            self.draw_text("Game Over", self.big_font, HEIGHT * 0.3)
            self.draw_text(f"Score: {self.score}", self.font, HEIGHT * 0.42)
            self.draw_text(f"High score: {self.high_score}", self.font, HEIGHT * 0.48)
            if self.jev_mode:
                self.draw_text("Jev will retry shortly", self.font, HEIGHT * 0.62)
            else:
                self.draw_text("Press SPACE to restart", self.font, HEIGHT * 0.62)

        if self.jev_mode and self.pilot is not None:
            self.draw_jev_panel()
        if self.jev_error:
            self.draw_text(self.jev_error, self.small_font, HEIGHT * 0.9, color=(255, 120, 120))

        pygame.display.flip()

    def run(self):
        running = True
        while running:
            dt = min(self.clock.tick(FPS) / 1000.0, 0.05)
            running = self.handle_events()
            self.update(dt)
            self.draw()
        if self.pilot is not None:
            self.pilot.close()
        pygame.quit()
        sys.exit()


if __name__ == "__main__":
    Game().run()
