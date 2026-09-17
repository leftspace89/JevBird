# How Jev Plays Flappy Bird

This document explains what Jev actually does when it pilots the bird in real time: what it receives, what it decides, what the game decides for it, and why the design ended up this way.

## The short version

Jev never sees the screen, the pixels, or the game code. It reads a short description of the situation and answers typed questions. It is a System One model: it returns a choice and a probability distribution in a few hundred milliseconds, rather than generating text or reasoning step by step.

The game plays at full speed, 60 frames per second, and does **not** wait on the network. That works because of a split:

- **Code owns the physics.** It simulates the future exactly, generates candidate flight paths, discards the ones that crash, and executes the chosen one frame by frame.
- **Jev owns the judgment.** Given ten viable paths through the next gap, it picks which one the bird should fly.

One request covers an entire pipe, so the bird acts on a plan rather than on a reflex. That is about one API call per pipe, roughly 0.6 per second.

## Why not ask every frame

The obvious design is to ask "flap or wait?" on every frame. It cannot work in real time. A round trip takes 300 to 900 ms and the bird falls at up to 700 px/s, so the answer describes a world that no longer exists by the time it arrives.

An earlier version of this project did exactly that and had to pause the physics while waiting, which made the game run in slow motion at about a quarter speed. Planning a whole pipe at once is what makes full speed possible.

## The loop

```
   new pipe becomes the target
            |
            v
   1. project the bird forward to the frame the answer will arrive
            |
            v
   2. run 55 flap policies through the exact simulator
      -> about 20 distinct candidate paths
            |
            v
   3. drop the crashers, rank the rest, keep the best 10
            |
            v
   4. one request: Choice over the 10 paths + Noul asking if this pipe is dangerous
            |
            v         (game keeps running at 60 fps the whole time)
   5. schedule every flap in the chosen path on exact frames
            |
            v
   6. each frame: check the bird is still on the path
      drifted > 35 px  -> throw the plan away, ask again
      crash within 0.3 s -> safety flap, override the plan
```

## Step 1: predicting the future

Two things make the simulation exact.

**The pipe gaps are decided in advance.** The game generates the next gap height when the previous pipe spawns and stores it, so a simulation can place a pipe that has not yet appeared on screen. This is what lets Jev see a pipe before it arrives.

**The physics runs on a fixed timestep.** The game accumulates real elapsed time and steps the world at exactly 1/60 s per step, so frame numbers are the unit of time everywhere. A flap scheduled for frame 1,204 happens on frame 1,204, and the simulator agrees with the live game to the pixel.

`Snapshot` in `flappy_bird.py` is a copy of the world that can be stepped forward without touching the real game.

## Step 2: latency compensation

A plan that starts now is useless, because the answer will not arrive for several hundred milliseconds. So the game asks: where will the bird be when the answer lands?

`JevPilot.expected_delay()` keeps a rolling estimate from the last six round trips, takes the larger of a smoothed average and the recent worst case, adds a small margin, and clamps the result between 0.25 s and 0.95 s. The game then advances a private copy of the world by exactly that many frames, replaying any flaps already scheduled in that window, and builds the candidate paths from **that** projected state.

The answer therefore describes the world at the moment it arrives, not the world at the moment it was asked.

## Step 3: generating candidate paths

Jev is not asked to invent a flap pattern. Code produces realistic ones by running a family of simple autopilots through the simulator:

- 11 target bands, from 50 px above the gap center to 50 px below.
- 5 falling-speed thresholds, from rising at 150 px/s to falling at 300 px/s.

Each of the 55 combinations flies the bird with the rule "flap if I am below my band and falling faster than my threshold", producing one flap pattern per policy. Many coincide, so about 20 distinct paths survive.

Each path runs one step every 7 frames, about 117 ms, and is long enough to clear the pipe plus 0.6 s beyond it, up to 40 steps. Including the tail matters: it makes Jev prefer paths that leave the bird well placed for the pipe after this one, rather than paths that scrape through and then fall.

Every path is then simulated exactly, recording the bird's offset from whichever gap it must fly at each step, the worst deviation, the ending velocity, and whether and where it crashes.

## Step 4: pruning before asking

Code removes what it can judge on its own. Paths that crash are dropped, unless fewer than two survive, in which case the crashers stay so Jev still has a choice. The rest are ranked by worst deviation from the gap center and the best ten go to Jev.

This is the division of labor. Code answers "is this path survivable?", which is arithmetic. Jev answers "which survivable path is the better line to fly?", which is judgment.

## Step 5: the request

Each request carries a state object and two questions.

The state looks like this:

```json
{
  "arena": "screen 400x600 px, flyable height 520 px between ceiling and ground, pipe gap 160 px tall, bird 32 px wide, pipes move 160 px/s and are 240 px apart",
  "bird_at_path_start": {
    "position": "23 px below the gap center",
    "motion": "falling at 180 px/s"
  },
  "gap": "the pipe is 214 px ahead",
  "upcoming": "the pipe after that has its gap 64 px higher",
  "safe_band": "within 55 px above or below the gap center",
  "timing": "the path starts 400 ms from now, has 24 steps 117 ms apart, and covers the whole pipe plus a moment after it"
}
```

The `arena` line gives Jev the resolution and scale, so distances in pixels mean something: it can tell that 55 px of margin in a 160 px gap is tight, and that 240 px between pipes is about a second and a half of flying.

Each option in the Choice question is one candidate path:

```json
{
  "actions": "....F..F...F...F..F.",
  "px_from_center_each_step": "-12,+18,+44,+9,-21,...",
  "farthest": "44 px below",
  "inside_safe_band": true,
  "ends": "6 px above the next gap center, rising 210 px/s"
}
```

`F` means flap and `.` means wait. The instructions tell Jev to take the smallest worst deviation, break ties by where the path ends and how fast, and never choose a crashing path when a safe one exists.

A second question rides along in the same request, a Noul asking whether this pipe is dangerous given how the candidates look. It costs nothing extra in round trips because independent questions are evaluated in parallel, and it drives the danger meter in the HUD.

## Step 6: what Jev returns

| Field | Meaning |
| --- | --- |
| `choice` | The path with the highest probability |
| `probabilities` | The full distribution over all ten paths |
| `confidence` | How concentrated that distribution is |
| `noul` | Probability that this pipe is dangerous |

The game flies `choice`. The distribution drives the visualization, drawing each rejected path at an opacity set by the probability Jev gave it, so you can see the alternatives it weighed. Confidence and danger are displayed but do not change behavior.

## Step 7: executing, and checking the execution

The chosen path is converted into a list of frame numbers where the bird must flap. Those flaps fire on exact frames. Nothing else in the loop blocks.

Two guards run every frame, because a plan made 400 ms ago can be wrong:

**Divergence detection.** The simulated y position for the current frame is stored alongside the plan. If the bird is more than 35 px away from where the plan says it should be, the plan is discarded and a new request goes out immediately. Drift happens when a reply arrives late and its first flaps are skipped.

**Emergency flap.** The game simulates 0.3 s ahead using the flaps already scheduled. If that ends in a crash and the bird is falling, it flaps now and abandons the plan. This overrides Jev.

Both events are logged to the console so you can see when they fire.

### The bug these guards fix

The first real-time version had neither. A reply that arrived late lost its opening flaps, so the bird sat lower than the plan assumed, and the remaining "wait" steps were correct for a bird that was higher. Since the game would not replan until 1.3 s before the path ended, the bird fell with no input for the rest of the plan. On a three-second path that is enough to reach the ground.

The symptom was distinctive: around score 30 the bird would simply stop flapping and drop. The fix is not to make the plan better but to notice when reality has left the plan behind.

## What Jev does not know

- It has no memory between requests. Each one is self contained.
- It never sees raw coordinates, only distances relative to the gap it must fly.
- It does not know the gravity constant, the flap strength, or the score. Code folds those into the simulated paths.
- It cannot invent a flap pattern. It can only choose among the paths it is shown, so candidate coverage is a design responsibility of the code.
- It does not learn. A crash changes nothing about the next request.

## Timing budget

| Quantity | Typical |
| --- | --- |
| Round trip | 0.30 s |
| Worst round trip seen | 1.2 s |
| Request timeout | 4.0 s, one retry |
| Path length | 2 to 3 s of flying |
| Replan lead | 1.3 s before the path ends |
| Input tokens per request | 1,800 to 2,500 |

The replan lead is the safety margin. As long as a reply comes back faster than 1.3 s, the next plan is ready before the current one runs out, and the bird never flies unplanned.

## Where to look in the code

`jev_pilot.py`

- `PATH_INSTRUCTIONS` and `DANGER_QUESTION` hold the two questions.
- `build_state()` writes the state object, including the arena description.
- `describe_path()` turns a simulated path into one Choice option.
- `select_candidates()` drops crashers and keeps the best ten.
- `JevPilot.expected_delay()` is the latency estimator.
- `log_request()` and `log_response()` produce the console output.

`flappy_bird.py`

- `Snapshot` is the forward-simulatable copy of the world.
- `policy_plan()` and `candidate_plans()` generate the flap patterns.
- `simulate_plan()` evaluates one path exactly.
- `Game.jev_request()` projects for latency and sends.
- `Game.jev_apply()` and `jev_promote_plan()` schedule the flaps.
- `Game.jev_check_plan()` and `jev_emergency()` are the two guards.
- `Game.draw_jev_paths()` draws the overlay.

## Ideas worth trying

- **Two pipes per request.** Extend the horizon and plan two gaps at once, halving the call rate again.
- **Let Jev set the style.** A Score question asking how aggressively to fly could pick the candidate band, turning one judgment into a policy the code reuses for several pipes.
- **Use the danger signal.** When the Noul reads high, shorten the replan lead so the bird gets a fresh plan sooner through tight pipes.
