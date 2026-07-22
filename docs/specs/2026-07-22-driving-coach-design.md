# Driving Coach — design spec

**Date:** 2026-07-22
**Status:** approved shape, pending spec review

## Goal

A post-race read that (1) summarises the run, (2) points out where the
*driver* can improve, and (3) flags when the *car* needs work — with an
honest split between the two. It answers the question a driver actually has
after a session: *"was that me or the car, and what do I fix first?"*

## Load-bearing constraint: local and deterministic

The coach generates its own words, in-tool, from telemetry already
computed. **No AI call. No network.** This is non-negotiable: the tool's
entire trust story is "nothing leaves your phone except the update check,"
and an AI-backed coach would break it. The coach is a rule-and-template
layer over existing metrics, unit-tested like `tuning_export.py`. (A
copy-to-AI "Coach" export mode was considered and deferred — see Out of
scope.)

## The one thing that makes this ours: driver-vs-car triage

Every other telemetry overlay either coaches the driver *or* analyses the
car. This tool already separates driver inputs from chassis response, so
the coach can tag each finding:

- 🧍 **you** — a habit you can change next lap (late throttle, lockups).
- 🔧 **car** — something no line or input fixes (won't rotate, out of grip).
- **both / unclear** — when a driver symptom is actually caused by the car,
  say so ("you're holding full lock — but that's because the car won't
  rotate; fix the car first"), and when the data can't tell, admit it
  rather than guess.

The triage is the spine of the feature, not a garnish.

## UX / placement

Chosen: **both** surfaces, one shared read.

- **Card at the top of the Analysis page** — a "Coach's read" card pinned
  above the existing verdict/lap/route cards. The TL;DR you see first when
  you open a session, with the detail below it as the evidence.
- **A "Coach" nav tab** — new top-level entry (peer of Garage) that jumps
  to the coach read for your **most recent** session, with the standard
  session picker to look back. Finish a race → tap Coach → get your read.

Both render the *same* structured output from the same endpoint; the nav
tab is a thin entry point that resolves "latest session" and shows the
card. No logic is duplicated.

## Content model

The read is a small structured object with three blocks, rendered in this
order:

### 1. Your race, in one line
- lap count, best lap;
- **consistency** — spread of valid lap times (tight / loose);
- **pace trend** — first-third vs last-third median lap (improving /
  steady / fading).
- Free-roam session with no timed laps: replace with distance/time and
  "no timed laps — free roam," then skip straight to technique flags.

### 2. Fix this first 🧍 (driver)
The 1–3 most significant driver habits, each as `{title, detail, metric,
value, tag}`. Ranking is a **severity heuristic** — how far past threshold
and how frequent — deliberately *not* a claim of exact seconds lost (the
tool never fabricates a number it can't measure). Detail always carries the
real value and a concrete instruction.

### 3. That's the car 🔧
Only present when the chassis telemetry shows something no driving fixes.
Same shape. When a car flag *gates* a driver flag, it is ordered first and
the driver flag references it.

A `headline` field names the single most important thing to work on. A
`data_sufficient` flag gates the whole read: too few clean corners/laps →
"not enough clean running to coach yet — a few more laps."

## Signals, thresholds and triage

All inputs already exist in the `analyse()` / `lap_report()` output. Initial
thresholds below are starting points, to be calibrated against real
captures (see Validation). Every rule degrades gracefully when its input is
missing.

| Flag | Source metric | Initial rule | Base tag | Triage cross-check |
| --- | --- | --- | --- | --- |
| Lockups | `events.brake_lock` count, per lap | > ~4 / lap | 🧍 | stays 🧍 (input-driven) |
| Over-slowing entries | section medians `entry_kmh`/`min_kmh`/`exit_kmh`, `braking_s` | min speed low vs exit + long braking | 🧍 | 🧍 |
| Late to throttle | section median `throttle_reapply_s` | > ~1.2 s | 🧍 | → 🔧/both if `slide_power_on_s` high (car won't accept throttle) |
| Sawing / over-driving | `full_lock_pct_of_cornering` | > ~40 % | 🧍 | → 🔧/both if `understeer_index` high (car won't turn, so you hold lock) |
| Persistent understeer/oversteer | `understeer_index` + per-phase | \|index\| high and consistent across phases | 🔧 | tune job |
| At the grip ceiling | `both_axles_saturated` | true | 🔧 | build (tyres/power), not tune/driving |
| Nervous / oscillating | `reversal_rate_per_min` | ≥ ~10 | 🔧 | unstable car, not you |
| Can't put power down | `slide_power_on_s` vs `slide_off_throttle_s`, `four_wheel_slide_pct` | power-on ≫ off-throttle | 🔧 | diff/gearing; gates "late throttle" |

The two cross-checks (steering-saturation and late-throttle) are the
feature's showcase: identical driver symptoms get opposite triage depending
on what the chassis is doing. Those must be explicit in code and covered by
tests.

## Architecture

- **`app/coach.py`** — new module, one pure function:
  `coach_report(analysis: dict) -> dict`. Input is the existing analysis
  output (session balance/traction/tyres, section aggregates, laps). Output
  is the structured read: `{headline, summary, flags: [...],
  data_sufficient}`. All prose lives here (templated with real numbers), so
  it is deterministic and unit-tested — the frontend never composes
  sentences.
- **`GET /api/sessions/{id}/coach`** in `main.py` — load session → run the
  existing `analyse()`/`lap_report()` → `coach_report()` → return the
  structured object. No new storage.
- **Frontend** — `analysis.html` renders a "Coach's read" card at the top
  from the endpoint; `app.js` gains a "Coach" nav entry that resolves the
  latest session and shows the same card. Rendering only: tags → 🧍/🔧
  chips, flags → rows. Zero business logic in JS.

Boundaries: `coach.py` depends only on the analysis dict (not on
`SessionData` or the DB), so it is testable in isolation with hand-built
dicts and reusable by any caller.

## Tone

Blunt, specific, one clear priority. No "great job!" filler, no reassurance
devices — the same honesty rule as the rest of the tool. Every claim
carries its number. Constructive, not harsh: name the fix, not just the
fault.

## Testing

`tests/test_coach.py`, synthetic analysis dicts engineered to trigger each
path:
- understeer-heavy across phases → 🔧 understeer, no false driver blame;
- lockup-heavy → 🧍 braking;
- `both_axles_saturated` → 🔧 build-ceiling, and it suppresses balance
  nagging;
- neutral balance + high `full_lock_pct` → 🧍 over-driving;
- high understeer + high `full_lock_pct` → triage **flips** to 🔧/both;
- power-on slide ≫ off-throttle + late reapply → car gates the driver flag;
- too few corners → `data_sufficient` false;
- free-roam (no timed laps) → summary degrades, technique flags still run.

## Validation against real captures

Before shipping, run the coach on known sessions and confirm the verdicts
match reality:
- the "psychotic" 628 kW AWD BMW → nervous/oscillating + build-ceiling 🔧;
- the Cayman 151 → car understeer 🔧 (front 0.94 / rear 0.44), and whatever
  driver flags the inputs support;
- a clean, fast session → few or no flags, not manufactured criticism.

## Out of scope (YAGNI)

- Copy-to-AI "Coach" export mode — easy to add later as a prompt variant;
  not needed for the local read.
- Per-corner annotations on the route map (a v2 visual).
- Cross-session progress tracking ("you've improved braking since last
  week").
- Voice / live in-session coaching.
