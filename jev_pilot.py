import os
import sys
import threading
import time
from collections import deque

from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeAPIError, TypeSafeClient

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
SAFE_MARGIN_PX = 55
MAX_OPTIONS = 10
REQUEST_TIMEOUT = 4.0

PATH_INSTRUCTIONS = (
    "You are piloting the bird in Flappy Bird. Each option is a complete flight path through the "
    "upcoming pipe gap: a sequence of actions taken a fixed interval apart, where F means flap and "
    ". means wait. Code has already simulated every path exactly, including the pipe that comes "
    "after this one. For each path it lists where the bird is at every step relative to the center "
    "of the gap it must fly through at that moment (minus means above, plus means below), the "
    "largest distance from center along the way, and how the path ends relative to the next gap. "
    "Choose the path with the smallest farthest distance from center while passing the pipe. If "
    "several are close, prefer the one that ends nearest the next gap center at a moderate speed. "
    "Never choose a path that crashes if any path avoids crashing. Being above the center is just "
    "as dangerous as being below it."
)

DANGER_QUESTION = Noul(
    instructions=(
        "Looking at all the simulated paths together, is this pipe hard to get through, meaning "
        "only a few paths avoid crashing or every path strays far from the gap center?"
    ),
    criteria={
        "true": "Most paths crash or leave the safe band; the pipe is tight for the bird's current position.",
        "false": "Plenty of paths pass comfortably inside the safe band.",
    },
)


LOG_ENABLED = os.environ.get("JEV_LOG", "1") != "0"
LOG_WIDTH = 78


def _console_handles(text, encoding):
    try:
        text.encode(encoding or "ascii")
        return True
    except (LookupError, UnicodeEncodeError):
        return False


RULE, DOT = (
    ("─", "·")
    if _console_handles("─·", getattr(sys.stdout, "encoding", None))
    else ("-", "|")
)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def _enable_color():
    if not LOG_ENABLED or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            ctypes.windll.kernel32.SetConsoleMode(handle, 7)
        except Exception:
            return False
    return True


class _Palette:
    def __init__(self, enabled):
        codes = {
            "reset": "\033[0m",
            "bold": "\033[1m",
            "dim": "\033[2m",
            "gray": "\033[90m",
            "cyan": "\033[96m",
            "purple": "\033[95m",
            "green": "\033[92m",
            "yellow": "\033[93m",
            "red": "\033[91m",
        }
        for name, code in codes.items():
            setattr(self, name, code if enabled else "")


P = _Palette(_enable_color())


def log_banner(model="jev-latest"):
    if not LOG_ENABLED:
        return
    print(
        f"\n{P.purple}{P.bold}  JEV IS FLYING{P.reset}  {P.dim}TypeSafe System One {DOT} model {model} {DOT} "
        f"one request per pipe{P.reset}\n",
        flush=True,
    )


def _path_mark(criteria):
    if "crashes" in criteria:
        return f"{P.red}crashes {criteria['crashes']}{P.reset}"
    if criteria["inside_safe_band"]:
        return f"{P.green}safe{P.reset}"
    return f"{P.yellow}wide{P.reset}"


def log_request(state, questions, tag):
    if not LOG_ENABLED:
        return
    criteria = questions["path"].criteria
    bird = state["bird_at_path_start"]
    lines = [
        f"{P.gray}{RULE * LOG_WIDTH}{P.reset}",
        f"{P.cyan}{P.bold}SEND{P.reset} {P.dim}request {tag['request']} {DOT} game {tag['time']:.1f}s {DOT} "
        f"score {tag['score']}{P.reset}",
        f"  {P.dim}bird {P.reset} {bird['position']}, {bird['motion']}",
        f"  {P.dim}gap  {P.reset} {state['gap']}, {state['upcoming']}",
        f"  {P.dim}plan {P.reset} {len(criteria)} candidate paths out of {tag['simulated']} simulated, "
        f"{state['timing']}",
    ]
    for key, c in criteria.items():
        lines.append(
            f"    {P.dim}{key:<7}{P.reset}{c['actions']:<34} {c['farthest']:>13}  {_path_mark(c)}"
        )
    print("\n".join(lines), flush=True)


def log_response(decision):
    if not LOG_ENABLED:
        return
    d = decision.description
    others = sorted(
        ((k, v) for k, v in decision.probabilities.items() if k != decision.key),
        key=lambda item: item[1],
        reverse=True,
    )[:3]
    runners = "  ".join(f"{k} {v:.2f}" for k, v in others) or "none"
    danger_color = P.red if decision.danger > 0.5 else P.green
    lines = [
        f"{P.purple}{P.bold}JEV {P.reset} picks {P.bold}{decision.key}{P.reset} "
        f"{P.dim}(ranked {decision.rank + 1} of {decision.option_count} by the simulator){P.reset}",
        f"  {P.dim}plan {P.reset} {P.purple}{d['actions']}{P.reset}   {P.dim}F = flap{P.reset}",
        f"  {P.dim}path {P.reset} farthest {d['farthest']}, {d['ends']}",
        f"  {P.dim}odds {P.reset} p={P.bold}{decision.probability:.2f}{P.reset} "
        f"confidence={decision.confidence:.2f} "
        f"danger={danger_color}{decision.danger:.2f}{P.reset}   {P.dim}runners up: {runners}{P.reset}",
        f"  {P.dim}cost {P.reset} {round(decision.latency * 1000)} ms, {decision.tokens} input tokens",
    ]
    print("\n".join(lines), flush=True)


def log_event(text, color=None):
    if not LOG_ENABLED:
        return
    tint = getattr(P, color, "") if color else ""
    print(f"{tint}{text}{P.reset}", flush=True)


def load_api_key():
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    try:
        with open(ENV_FILE) as f:
            for line in f:
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == "TYPESAFE_API_KEY":
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def plan_string(plan):
    return "".join("F" if f else "." for f in plan)


def describe_path(result):
    steps = ",".join(f"{round(o):+d}" for o in result.offsets)
    worst = result.worst_offset
    side = "above" if worst < 0 else "below"
    end = result.offsets[-1]
    motion = "falling" if result.end_velocity > 0 else "rising"
    description = {
        "actions": plan_string(result.plan),
        "px_from_center_each_step": steps,
        "farthest": f"{abs(round(worst))} px {side}",
        "inside_safe_band": abs(worst) <= SAFE_MARGIN_PX,
        "ends": f"{abs(round(end))} px {'above' if end < 0 else 'below'} the next gap center, "
        f"{motion} {abs(round(result.end_velocity))} px/s",
    }
    if result.crashed:
        description["crashes"] = f"step {result.crash_step + 1}"
    return description


def select_candidates(results):
    safe = [r for r in results if not r.crashed]
    pool = safe if len(safe) >= 2 else results
    ranked = sorted(pool, key=lambda r: abs(r.worst_offset))
    return ranked[:MAX_OPTIONS]


def build_questions(results):
    candidates = {f"path_{i}": r for i, r in enumerate(select_candidates(results), 1)}
    criteria = {key: describe_path(r) for key, r in candidates.items()}
    questions = {
        "path": Choice(instructions=PATH_INSTRUCTIONS, criteria=criteria),
        "danger": DANGER_QUESTION,
    }
    return questions, candidates


def build_state(bird_offset, bird_velocity, gap_distance, upcoming_gap_shift, step_seconds, start_delay, steps, arena):
    if gap_distance is None:
        gap = "the next pipe has not appeared on screen yet; until it does, the middle of the screen counts as the gap center"
    else:
        gap = f"the pipe is {round(gap_distance)} px ahead"
    if upcoming_gap_shift is None:
        upcoming = "the pipe after that is not known yet"
    else:
        direction = "higher" if upcoming_gap_shift < 0 else "lower"
        upcoming = f"the pipe after that has its gap {abs(round(upcoming_gap_shift))} px {direction}"
    return {
        "arena": arena,
        "bird_at_path_start": {
            "position": f"{abs(round(bird_offset))} px {'above' if bird_offset < 0 else 'below'} the gap center",
            "motion": f"{'falling' if bird_velocity > 0 else 'rising'} at {abs(round(bird_velocity))} px/s",
        },
        "gap": gap,
        "upcoming": upcoming,
        "safe_band": f"within {SAFE_MARGIN_PX} px above or below the gap center",
        "timing": (
            f"the path starts {round(start_delay * 1000)} ms from now, has {steps} steps "
            f"{round(step_seconds * 1000)} ms apart, and covers the whole pipe plus a moment after it"
        ),
    }


class PathDecision:
    def __init__(self, plan, key, probabilities, confidence, danger, latency, tag, description, rank, option_count):
        self.plan = plan
        self.key = key
        self.probabilities = probabilities
        self.probability = probabilities.get(key, 0.0)
        self.confidence = confidence
        self.danger = danger
        self.latency = latency
        self.tag = tag
        self.description = description
        self.rank = rank
        self.option_count = option_count


class JevPilot:
    def __init__(self, api_key):
        self.client = TypeSafeClient(
            api_key=api_key,
            retry=RetryPolicy(max_retries=1, timeout=REQUEST_TIMEOUT),
            timeout=REQUEST_TIMEOUT,
        )
        self.results = []
        self.error = None
        self.last_decision = None
        self.requests = 0
        self.sent = 0
        self.tokens = 0
        self.inflight = 0
        self.latency = 0.4
        self.recent = deque([0.4], maxlen=6)
        self.lock = threading.Lock()

    @property
    def busy(self):
        with self.lock:
            return self.inflight > 0

    def request(self, state, questions, candidates, tag):
        with self.lock:
            if self.inflight > 0:
                return False
            self.inflight += 1
            self.sent += 1
            tag["request"] = self.sent
        log_request(state, questions, tag)
        threading.Thread(target=self._ask, args=(state, questions, candidates, tag), daemon=True).start()
        return True

    def _ask(self, state, questions, candidates, tag):
        started = time.perf_counter()
        try:
            response = self.client.system_one(state, questions)
            latency = time.perf_counter() - started
            answer = response.choices["path"]
            options = list(questions["path"].criteria)
            decision = PathDecision(
                candidates[answer.choice].plan,
                answer.choice,
                dict(answer.probabilities),
                answer.confidence,
                response.nouls["danger"].noul,
                latency,
                tag,
                questions["path"].criteria[answer.choice],
                options.index(answer.choice),
                len(options),
            )
            decision.candidates = candidates
            decision.tokens = response.usage.input_tokens
            log_response(decision)
            with self.lock:
                self.latency = 0.7 * self.latency + 0.3 * latency
                self.recent.append(latency)
                self.requests += 1
                self.tokens += response.usage.input_tokens
                self.last_decision = decision
                self.results.append(decision)
        except TypeSafeAPIError as exc:
            log_event(f"JEV  api error: {exc}", "red")
            with self.lock:
                self.error = f"Jev API error: {exc}"
        except Exception as exc:
            log_event(f"JEV  request failed: {exc}", "red")
            with self.lock:
                self.error = f"Jev request failed: {exc}"
        finally:
            with self.lock:
                self.inflight -= 1

    def take_results(self):
        with self.lock:
            decisions, self.results = self.results, []
            return decisions

    def take_error(self):
        with self.lock:
            error, self.error = self.error, None
            return error

    def expected_delay(self):
        with self.lock:
            estimate = max(self.latency * 1.2, max(self.recent) * 1.05) + 0.05
            return min(0.95, max(0.25, estimate))

    def close(self):
        self.client.close()
