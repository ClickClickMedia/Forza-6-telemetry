# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project follows
[Semantic Versioning](https://semver.org/).

## [2.8.0] - 2026-07-22

### Added — Driving Coach (local, deterministic)
A post-race read that summarises the run, names the driver habits to fix,
and flags when the car needs work — each tagged **you** (driver) or **car**
(tune/build). The honest driver-vs-car triage is the point: identical
symptoms get opposite verdicts depending on the chassis — holding full lock
reads as over-driving on a balanced car, but as "the car won't rotate" when
it understeers. Computed on your device from metrics the app already derives
(understeer index, oscillation, wheelspin split, brake locks, steering
saturation, power-on slide) — **no AI, no network**. Surfaces as a card at
the top of Analysis and a new **Coach** nav tab.

Validated against the real Cayman before/after: it reads session 151 as a
car that *understeers* (you fight it) and session 152 — after a broad tune
that made it worse — as *nervous* (axle flipping 41.7×/min) and unable to
put its power down. The oscillation threshold is calibrated so 151's
11.8×/min stays quiet while 152's 41.7×/min trips the "nervous" flag. The
race summary excludes scruffy laps (spins, or parked/paused "laps" outside
15% of your best) before reading consistency, so one bad lap can't mask a
consistent stint or invent a fake trend. See
[docs/specs/2026-07-22-driving-coach-design.md](docs/specs/2026-07-22-driving-coach-design.md).

## [2.7.0] - 2026-07-22

### Changed — AI prompts hardened against context rot
A real case drove this: a broad, confident tune (the kind that touches
alignment, gearing, brakes, springs, aero and the diffs at once) took a
mild-understeer AWD Cayman and made it 0.48 s slower with 26× the oversteer
moments and a cooking rear axle — while the telemetry supported exactly one
lever. The exports now push back on that failure mode:
- **First-tune mode reframed** — "complete" now means a verdict per
  subsystem (`CHANGE` / `RETAIN` / `CANNOT ASSESS` / `RANGE REQUIRED`), not
  change-everything. Retaining most of the car is stated as a complete,
  valid answer, and the prompt tells the AI to drop any change the report
  shows no failure mode for.
- **Cross-car transfer ban** — every analysis mode now carries an evidence
  precedence + a rule not to carry tune values or ratios over from another
  car or an earlier conversation. Current-session telemetry wins. This is
  the direct antidote to a long chat importing another car's tune.
- **Contradiction check** — the default tune-advice prompt tells the AI to
  test each change against the evidence (no brake change without a braking
  fault, no alignment change without tyre evidence, and so on).
- New [worked first-tune example](docs/example-first-tune.md) from the
  Cayman case.

### Fixed
- **Setup versions progress on change only.** Re-copying an unchanged tune
  no longer mints a duplicate version; an identical re-save reuses the
  current one (guarded at the `/api/setups` write, so the UI and MCP paths
  are both covered).
- **Session lineage compares only against earlier runs.** Re-opening an
  older session's report compared it against runs recorded *after* it,
  reversing the "since last session" delta. `sessions_for_car()` now
  restricts to strictly-earlier sessions, as its contract always said.

### Docs
- README hero refreshed with current phone screenshots, including the new
  Garage page and a Compare / detected-events row.

## [2.6.0] - 2026-07-22

### Added — diagnostic depth (from a six-agent teardown of a hard AWD case)
A multi-agent review of a "psychotic" 628 kW AWD drift-converted BMW —
where a near-zero understeer index hid a car sliding all four wheels —
surfaced what the summary buried. New metrics, all computed from channels
already on the wire (no fabricated sensors):
- **Grip-deficit banner** — when both axles are past the grip limit the
  report says so plainly and states the understeer index is *not a tuning
  target* (tuning redistributes grip, it can't add it; the fix is the
  build). This was the single insight every analyst had to reconstruct by
  hand.
- **Balance-oscillation rate** — how often the limiting axle flips
  front↔rear per minute. A near-zero index can sit on a signal whipsawing
  1–2×/second, which is what "psychotic/nervous" actually is.
- **Slide split by throttle state** — power-on wheelspin (diff/throttle
  fix) vs off-throttle lateral slide (alignment/ARB fix); the report was
  blending two problems that need opposite levers.
- **Slide overlap %, four-wheel fraction and event-duration shape** —
  front and rear slide totals now read as one overlapping event, with a
  spiky-vs-sustained median.
- **Tyre-temperature trend** — per-axle °C/min slope, %-time over window,
  and a warming/steady/runaway tag (peak alone hid a rear in thermal
  runaway).
- **Body-control** — squat, dive and roll-p95 (the pitch/roll the avg/max
  travel numbers don't show).
- **Steering saturation** — %-cornering at ≥95% lock (an over-driving
  signal).
- **First-tune mode** — a third "Report goal" option beside Tune advice
  and Experiment: a complete one-shot coordinated setup for a new/unsorted
  car, opt-in.
- Prompt hardening: both-axles-saturated → index isn't a target; separate
  power-on spin from off-throttle slide; read temp *trend*; you don't know
  each slider's min/max, so don't assume headroom. Setup-provenance
  warning promoted into the report by the setup table.

### Fixed
- Lap-table "Avg km/h" now matches distance/time (the v2.5.0 duplicate-
  timestamp fix resolved this too — the same inflated `dt` had skewed
  `route_m`). Lap distances labelled "km driven" (driven path, not track
  length). `README.txt` in the package no longer overclaims that
  `sections.json` holds every instance.

## [2.5.0] - 2026-07-22

### Fixed — time-metric inflation (found via a raw-data forensic review)
- **Every time total was inflated ~6% by duplicate timestamps.** On
  Windows the monotonic clock resolution (~16 ms ≈ one 60 Hz frame)
  stamps several frames at the same instant, so a real capture has ~6%
  duplicate-timestamp frames. `dt()` replaced those zero-deltas with the
  median frame length, adding phantom time. Now duplicate-instant frames
  contribute zero elapsed time and `sum(dt)` equals the true span exactly.
  On the reference BMW session this corrects **duration 337→318 s,
  distance 13.61→12.8 km**, and trims every "seconds of X" metric
  (wheelspin, slide time, braking) to its true value. No weighted-average
  regressions (redundant same-instant frames simply get no weight).
- **`sections.json` wording corrected.** The report said the export was
  the "full instance list"; it actually carries per-category counts,
  medians and representative samples. Wording now matches what the file
  contains (exporting every instance is a tracked enhancement, issue #6).

### Added
- **Garage** — a new tab listing every car you've driven or named, with
  session count, saved tune versions, best lap and when last driven,
  aggregated from your local sessions and tunes (no new storage, nothing
  leaves the machine).

## [2.4.1] - 2026-07-22

### Changed — tune-advice prompt calibration
- **Interventions now scale to the evidence.** The default tune-advice
  prompt asked for "the smallest tune change"; it now asks for a
  coordinated multi-setting tune when the telemetry shows a fundamental
  imbalance (one axle far past its grip limit while the other is
  underused) and a single targeted change when the car is already close —
  so an obviously-wrong baseline gets a proper first-tune overhaul, not a
  timid nudge.
- **Safe vehicle context, no fabrication.** The AI may use general
  knowledge of a car's real-world layout/drivetrain as clearly-labelled
  context, but must not invent Forza-specific facts (upgrades, in-game
  ranges, "meta" tunes), must not let context override measured
  telemetry, and must not claim to have researched anything without live
  web access. (A full "research the FH6 model + cite sources" instruction
  was deliberately declined — it invites hallucinated FH6 facts, which
  the whole evidence-first design exists to prevent.)

## [2.4.0] - 2026-07-21

### Added — experiment mode (opt-in)
- **Design-an-experiment prompt** on the engineering brief: a "Report
  goal" toggle (Tune advice / Design an experiment) in the setup sheet.
  Experiment mode deliberately flips the cautious framing — it asks the AI
  to design **one decisive, reversible, single-variable test** pushed to a
  near-extreme to maximise what the next run teaches, with a falsifiable
  prediction and a pass/fail rule, instead of nudging several systems at
  once. Every Forza change is reversible, so a bold test costs one lap you
  can undo — worth it for the knowledge.
  - If the report shows a change since the previous setup, the prompt first
    asks the AI to evaluate that as a completed experiment (supported /
    rejected / inconclusive) against the matched-section evidence before
    proposing the next one.
  - The default tune-advice, quick and copy-evidence prompts are
    **unchanged** — the honest, cautious framing stays the default for the
    "why am I slow?" case; boldness is always a deliberate choice.

## [2.3.2] - 2026-07-20

### Added — comparison-quality & causality round (blind-context feedback)
- **Inside vs outside driven-wheel flare**: single-wheel wheelspin while
  turning is now split by whether the spinning wheel is on the inside or
  outside of the corner (from steer direction) — inside-wheel flare on
  exit is the classic more-diff-accel-lock signal, and it makes diff
  advice defensible. On real captures it's a strong, clean signal
  (Ford GT: 50 s inside vs 3 s outside).
- **Repeatability line for two valid laps**: a tight two-lap spread is
  reported as evidence a session-to-session gain is repeatable, not a
  single-lap outlier (previously needed 3+ laps).
- **Causality guardrail** in the engineering prompt: when several tune
  settings changed at once, a faster result proves the whole tune was
  faster, not which setting caused it — measured outcome, explanation and
  single-setting causal claims are kept separate.

### Fixed
- **Partial laps no longer look like the fastest lap**: an incomplete
  lap's shorter elapsed time is labelled "incomplete (… elapsed — not a
  lap time)" instead of sitting in the Time column as if comparable.
- **Since-last-session deltas are honest about their basis**: wheelspin
  is flagged as a whole-session total (sensitive to non-timed driving),
  and a comparison-basis line states route match is confirmed while
  build/tune/conditions are not verifiable from telemetry.
- Power-utilisation reads against route type — a low value on a tight
  touge route is no longer implied to be a gearing problem.

### Changed
- "Engineering analysis" button → **"Engineering brief"**, with a line
  under the export bar: *No AI runs inside the tool — paste it into an AI
  yourself if you want interpretation, or just read it.*

## [2.3.1] - 2026-07-20

### Fixed
- **Lap table numbers laps sequentially (1, 2, 3)** instead of echoing the
  wire lap number — on point-to-point/touge events FH6 keeps LapNumber at
  0, so every lap used to display as "Lap 0". The times and validity were
  always correct; only the label was confusing.

### Added
- **[docs/example-report.md](docs/example-report.md)** — a real, full
  session report you can read without running anything, linked from the
  README. Makes plain that the tool produces a human-readable report, not
  AI output.
- README reframed: the tool is a telemetry analyser you read yourself; an
  AI is an optional companion for interpretation, never a replacement and
  never a tuner. It does not invent setup values.

## [2.3.0] - 2026-07-20

### Changed — blind-context round (a fresh AI must understand a session
### with no chat history)
- **Rewind-affected laps are identified by name**: a lap spanning a
  rewind snap is flagged in the lap table's new Validity column
  (valid / partial / rewind-affected), excluded from the best lap, and
  explained in one line — no more inferring validity from prose.
- **Every lap carries session-relative t_start/t_end**, in the report
  data, laps.csv and package metadata — evidence attribution by
  timestamp instead of guesswork.
- **Section samples come from timed running**: instances outside the
  timed laps/runs are labelled and excluded from medians and
  representative samples (a staging shuffle can no longer be a
  category's "lowest"); headings show how many were excluded.
- **Quick analysis inherits saved non-tune context** (discipline,
  assists, gearbox, car name) from the car's saved profile, labelled
  "Context loaded from the saved car profile; tune values omitted" —
  TCS-on now correctly frames wheelspin in quick copies while setup
  still reads "not supplied".
- **laps.csv renames max lateral G to `raw_max_lat_g` and adds
  `sustained_lat_g`**, plus `rewind_affected` and `t_start_s`/`t_end_s`
  columns — machine readers can no longer mistake a 6.99 g impact spike
  for grip.
- **package.zip metadata now carries** discipline, assists, per-lap
  validity, timed windows, section-evidence scope and honest setup
  provenance flags (`setup_source: saved_latest`,
  `setup_snapshot_at_recording: false`); the README states the setup
  may postdate the recording. A recording-time setup snapshot remains
  issue #2.
- Copy-evidence exports end with "contains evidence only and makes no
  request for analysis"; tyre steady-state wording states the real
  limitation (compound and prior running unknown); shift-point spread
  no longer over-claims driver variance.

## [2.2.8] - 2026-07-20

### Changed
- Prompt refinements: findings are ranked strongest-to-weakest with
  strong/moderate/weak evidence labels; driver observations are kept
  separate from setup hypotheses (a setup direction needs telemetry
  support independent of likely driver variation); and both prompts
  prefer "collect more evidence" over speculative tuning when the
  telemetry doesn't justify a change.

## [2.2.7] - 2026-07-19

### Changed — progressive disclosure round (community UX feedback)
- **The Analysis page shows four understandable actions**: ⚡ Quick
  analysis · Engineering analysis · Copy evidence (renamed from "Data
  only" — it copies the interpreted report without the AI prompt, not
  raw telemetry) · **Export files ▾**, which opens a sheet with the
  individual downloads (report .md, lap summary .csv, section data
  .json, raw telemetry .csv) plus a new **complete package (.zip)** —
  report, laps, sections, raw capture, metadata and the latest saved
  tune in one attachment.
- **Setup status is three-state**: none / partial (with the tunable
  count — a drivetrain-only save reads "context only", never "yes") /
  full. Context-only engineering copies carry an explicit
  telemetry-level note so they never look more authoritative than a
  quick analysis.
- **Section evidence declares its scope**: when timed running is a
  fraction of the recording, the report says so ("entire 167-second
  recording, including driving OUTSIDE the timed running… total 34 s")
  so an analyst never attributes whole-session cornering evidence to a
  short run.
- Samples whose combined slip sits at the 2.5 analysis ceiling are
  flagged "slip saturated" so clipped rankings read as clipped.

## [2.2.6] - 2026-07-19

### Changed — quick analysis polish (field feedback)
- **Quick analysis has its own purpose-built prompt**: identify where
  performance is lost by section, which domain the evidence points at
  (driving / traction / balance / suspension / braking / gearing),
  which setup areas to inspect without inventing values, and the
  confidence per conclusion. The engineering prompt is unchanged.
- **No more mandatory identity gate on quick copies**: instead of
  stopping to ask year/make/model first, the AI is told to analyse
  immediately with what's here and ask for car/build details at the end
  only if they would materially refine the next step. Engineering mode
  keeps the strict Step-0 gate.
- **Two-instance categories label samples lower/higher** — no invented
  "median" member from an even pair.
- Hairpin sections are headed **"Hairpin / switchback"** (the ≥150°
  below 90 km/h clause catches fast switchbacks that aren't classic
  sub-60 km/h hairpins).
- **Undeclared assists read neutrally**: the lock-threshold line says
  interpretation depends on ABS use, instead of assuming ABS is on.

## [2.2.5] - 2026-07-19

### Changed — two intents, two workflows (community product feedback)
Field use settled into two distinct modes: ~80% engineering sessions
(iterating small setup changes) and ~20% "why was I slow?" exploration.
One copy action was serving both badly.

- **⚡ Quick analysis** (new primary button): copies immediately — full
  evidence packet plus a prompt that says setup was not supplied, so the
  AI locates the problem and names setup *areas* to investigate with its
  confidence, never inventing values.
- **Engineering analysis** (the existing sheet flow, renamed): the
  report now leads the setup section with **"Changes since previous
  setup"** — a field-level diff against the prior revision, telling the
  AI exactly which variables this session is testing instead of making
  it rediscover them from two full tables.
- The setup sheet shows **tune status** on open: current tune name, how
  long ago it was saved, and "✓ same as previous" or "● N changes vs
  previous" — confidence that the tool already knows the car.
- "Copy data only" remains for pasting evidence into an ongoing
  conversation without re-sending the prompt.

## [2.2.4] - 2026-07-19

### Fixed — section edge cases (field report on a low-power baseline run)
- **Throttle semantics are now self-consistent**: `never_lifted` only
  when throttle stayed ≥50% for the whole section; otherwise the time
  from the deepest lift to reapplication (the old `already_on` could
  contradict a 0% minimum).
- **Impact contamination filtered**: per-section lateral G is now the
  95th percentile (single-frame spikes excluded) and any section with
  p95 above 3 g is dropped — a 5.11 g "transfer" containing a collision
  no longer reaches the evidence table. Transfers additionally must
  never drop below 40 km/h.
- **Launches split from flying straights**: a straight beginning below
  30 km/h is classified as a launch — standing-start acceleration no
  longer pollutes flying-straight gearing evidence.
- **Single-instance categories print once** ("Only one qualifying
  instance detected") instead of identical lowest/median/highest rows.

## [2.2.3] - 2026-07-19

### Fixed — CRITICAL data-integrity bug
- **The traction analyser could watch the wrong axle.** Drivetrain was
  sampled from a single frame, and loading/menu/results frames zero
  every identity field — DrivetrainType 0 means FWD, so an RWD race
  whose recording began on a loading frame reported "driven wheels FL,
  FR · 0.2 s wheelspin" while the rears spun for seconds per hairpin
  (caught by a community analyst as a Car-section/traction-section
  contradiction). Drivetrain is now the **modal value over
  identity-valid frames**, shared by the traction analyser and the
  section engine — on the affected capture the report corrects from
  0.2 s to 50.8 s of rear wheelspin.
- **Defence in depth**: if car metadata and the analyser ever disagree
  again, the report prints a loud drivetrain-mismatch error and
  suppresses every driven-wheel traction finding instead of publishing
  convincing-but-wrong evidence.

## [2.2.2] - 2026-07-19

### Added — analysis-context round (feedback on the setup export)
- **Analysis context block near the top of every report**: discipline
  (new selector in the setup form: Circuit/Touge/Dirt/Drag), driver
  objective, this session's driver note (with a pointer when empty),
  declared assists, setup-supplied flag and conditions — the same
  telemetry means different things on circuit vs touge, so the framing
  now precedes the evidence.
- **Setup relationships**: factual derived ratios (ARB F:R, springs,
  aero, rebound:bump per axle, diff accel/decel with an
  "decel exceeds accel" flag) — unusual configurations become visible
  without prescribing anything. Units note under the setup table.
- **Assist caveats beside the numbers they qualify**: TCS-on prints with
  the wheelspin figures, ABS-on with the lock-threshold line.
- **Compact report style**: a Detailed/Compact toggle in the setup sheet
  (persisted) — compact keeps every number, section sample and the
  prompt, but drops the repeated methodology paragraphs for quick
  phone pasting. Detailed remains the default.

## [2.2.1] - 2026-07-19

### Fixed — evidence-quality round (community field-testing of v2.2.0)
- **Cross-route lineage deltas suppressed**: performance deltas are
  calculated only when route equivalence is established (same timing
  kind, median timed-loop lengths within 5%); otherwise the report says
  so and calculates nothing. A confidently wrong "-35 s improvement"
  between different events is worse than no comparison.
- **Slip values clipped at 2.5 before any section statistic** — one
  collision frame can no longer put a "+18.84 slip delta" in an evidence
  table — and **low-speed "transfers" filtered** (avg ≥ 60 km/h;
  staging/spin recovery is not a chassis flick).
- **Sample labels are factual**: lowest / median / highest on the stated
  metric (in the report and sections.json) — whether low was good is the
  analyst's call, not a "best/worst" judgement.
- **Category exclusivity stated**: classifications are mutually
  exclusive; one event spans contiguous same-direction cornering and may
  cover linked bends.
- **Hairpin rule widened**: heading ≥ 150° below 90 km/h also counts
  (switchbacks previously landed in "turn").
- **Throttle semantics**: `throttle_at_entry_pct`, `throttle_min_pct`
  and `throttle_reapply_s` (with `already_on` / `not_reached`) replace
  the ambiguous zero-valued pickup field.
- **Report order**: section evidence now comes before the session-wide
  aggregate, which is titled as a summary of the sections — facts before
  any aggregate framing.
- **Short sessions draw no tyre-window conclusion**: under 5 minutes the
  report gives the recorded averages and says steady-state may not have
  been reached.

## [2.2.0] - 2026-07-19

### Changed — the report is now an evidence packet, not a diagnosis
Field experience showed verdict language ("severe understeer", "the tune
worked", "coach the driving first") steered AI analysis before the data
was examined — and session-wide averages hid how differently a car behaves
in a hairpin versus a sweeper versus a chicane. The export now organises
evidence; the analysis layer draws conclusions.

- **Section evidence**: every cornering event is classified — hairpin,
  turn, sweeper, transfer (flick/chicane) and straight, with documented
  thresholds, per-category medians and best/median/worst representative
  samples (timestamped so the AI can find them in the raw CSV). A new
  **sections.json** export carries every instance for machine reading.
- **Verdict-free wording throughout**: the handling block is now "Balance
  evidence (session-wide)" — facts with stated provenance; lineage keeps
  factual deltas and drops verdict sentences; no line in the report
  prescribes a setting change any more.
- **The AI prompt is half the length** and evidence-first: prioritise lap
  time and like-for-like comparisons, the driver's described problem,
  section behaviour and representative samples; the smallest change that
  tests the strongest hypothesis; "no setup change recommended" stays a
  valid answer; never invent data.
- Balance evidence now ships in the data-only export too (it is factual).

## [2.1.12] - 2026-07-19

### Added — "race engineer" report round (community feedback)
- **Corner-phase ranking**: entry/mid/exit/lift ordered worst-first so
  fixes target the biggest contributor.
- **Evidence quality line**: cornering sample size (thin/adequate/rich),
  drift-excluded share, and declared conditions — the AI is told to weight
  its advice by it.
- **Conditions awareness, honestly**: Forza broadcasts no weather or
  time-of-day (verified at packet level through a rain-to-dry session,
  including the one unmapped byte — it never moves). Declare conditions in
  the session note; "rain/night" words reduce stated temperature and grip
  confidence throughout the report.
- **Since-last-session deltas** with a clock-first verdict: a faster
  session with worse balance metrics reads as "the tune is working — do
  not revert", never as a regression.
- **Lap consistency line** (3+ complete laps): time spread as % of the
  median lap plus throttle/brake ranges — above ~2%, the report says
  consistency beats setup changes.
- **Wheelspin pattern classification**: mostly-single-wheel vs all-wheel,
  corners vs straights, with the matching tuning direction.
- **Shift-point spread** (p10–p90 upshift RPM) flagged as a driver signal,
  and **time at ≥90% of observed peak power** (relative to the session's
  own demonstrated peak, never garage figures).
- AI prompt now states plainly: **"no setup change recommended" is a
  valid answer** — prefer it when the clock improves, evidence is weak,
  or driver variance dominates.

## [2.1.11] - 2026-07-18

### Fixed
- **Lap splitting now works on every staged circuit, not just ones whose
  grid sits on the racing loop.** The gate is discovered from the
  trajectory itself (the earliest point the car revisits travelling the
  same direction) instead of being anchored at the launch frame — staged
  events broadcast Position (0,0) while the world loads, which used to
  strand the gate kilometres off-circuit and silently fall back to one
  whole-event "run". Verified on a real 5-lap event: partial opening
  segment + four complete laps, with the reconstructed final lap landing
  within half a second of the driver's manually-read time.

### Added
- **Crossing times are interpolated between telemetry frames** (the
  along-track coordinate's zero-crossing), not snapped to the nearest
  frame.
- **Leading partials**: the segment before the first gate pass (run-in
  spur, or a recording that starts mid-lap) is reported as a partial lap
  and never ranked.
- **Finish-phased boundaries**: when the discovered gate phase strands a
  large untimed tail, the gate re-anchors near the event end — the one
  point the game pins — so the real final lap is captured whole.
- Reports show the staged event's **total time alongside the laps**, and
  say how many partial segments exist.

## [2.1.10] - 2026-07-18

### Fixed
- **The "Rear slip" route colouring works now.** Slip is normalized (1.0 =
  grip limit) but spikes past 15 on kerb strikes, and the colour scale
  auto-ranged to the biggest spike — so 92% of a real session's route
  painted as "low" while a minute of genuine sliding hid in the bottom
  colour band. The scale is now fixed to the physics (blue = grip, green
  = at the limit, red = sliding past it) and downsampling takes the max
  per road segment so sub-second slides stay visible.

### Added
- **Delete all sessions** on the Debug page: one button wipes every
  recorded session and its raw files (with size shown and a confirm).
  Car names, tune setups and settings are kept; refused while recording.

## [2.1.9] - 2026-07-18

### Added
- **Real lap times for circuit events that broadcast no lap data.** When a
  staged run repeatedly returns to its own start point travelling the same
  direction, those returns are start-line crossings and the report now
  splits the event into laps (times, routes and full per-lap analytics).
  Validated on a real 3-lap race: the two "runs" the old detector showed
  (split by a mid-race rewind snapping DistanceTraveled) became laps of
  1:36.547 / 1:48.860 / 1:36.125 — position is continuous truth, so
  rewinds cannot break the splits. Point-to-point runs are untouched;
  loop-length consistency is required before any split is trusted.

## [2.1.8] - 2026-07-18

### Added
- **Tune lineage**: reports now include a before/after table of earlier
  sessions with the same car (best time, understeer index, wheelspin,
  locks, temps, max speed, shifts), built from a compact summary stored
  per session. Existing sessions are backfilled in the background on
  first launch. Times from staged events are marked "(run)" so whole-race
  times never get compared against single laps.
- **Result note** button on the Analysis page: record the game's official
  result time and the tune version tested; it rides along in every AI
  copy and in the lineage table of later sessions.
- The report now tells the AI to **judge tune changes by the clock
  first** — balance metrics describe the car's character, and a faster
  session with worse balance numbers is a successful tune whose limit
  moved, never a reason to revert.

### Fixed
- **"Copy data only" is now genuinely data-only**: no AI prompt, no
  handling headline, no coaching — telemetry, derived values, lineage and
  data-quality notes only.

## [2.1.7] - 2026-07-18

### Added
- **Braking is now told apart as three states, not one**: sustained lock
  (wheel-speed deficit), braking at the lock threshold (deep slip with the
  wheels still turning — the ABS-modulation signature), and normal braking.
  Real captures show 53-79% of braking time at the threshold with under 1%
  sustained lock: braking at the grip ceiling with ABS working, not a
  setup fault. The report prints both numbers with that framing.
- **ABS and traction-control declarations** in the setup form (the wire
  does not broadcast assist states). With ABS declared on, the report
  tells the AI to judge brake pressure only on sustained locks or
  instability — never on threshold time alone. With TCS on, wheelspin is
  framed as what the assist could not contain.

## [2.1.6] - 2026-07-18

### Fixed
- **Brake-lock detection rebuilt on wheel-speed deficit** (wheel rotation
  vs road speed, per-wheel calibrated on coasting frames). Forza's
  normalized slip ratio crosses -0.5 during ordinary hard braking with no
  lockup — verified on a real capture where the old detector reported
  5-6 s of "lock" and the wheels never stopped. Real sessions drop from
  "84% of braking" to honest fractions; the detector name is printed
  beside the number.
- **Wheelspin buckets are now mutually exclusive and reconcile exactly**:
  per-wheel-only + multiple-wheels = total, turning + straight = total
  (previously mixed gap-merged and raw durations).
- A setup that names the car now **registers the car name automatically**
  — reports and session lists pick it up with no separate "name car" step,
  and the report uses the setup's car identity instead of asking the AI to
  ask.

### Added
- Setup export lists core settings left blank as "Not provided".
- When the game broadcasts identical rear tyre temperatures (verified at
  packet level on multiple cars — Forza models some rear axles jointly),
  the report says so instead of leaving a suspicious symmetry.
- Sustained-grip wording notes banking/compressions may contribute.

## [2.1.5] - 2026-07-18

### Fixed
- **Negative values are now enterable on phones.** Mobile numeric keypads
  have no minus key, which made camber and toe impossible to type — the
  signed fields (camber F/R, toe F/R) now carry a ± button inside the
  input that flips the sign without leaving the number pad.

## [2.1.4] - 2026-07-18

### Changed
- **Auto-record now defaults to Off (manual)** — nothing is written until
  the player presses ● Record. The mode selector lists Off first, then
  Events, then Any driving. Existing installs keep whatever mode they had
  chosen; installs that never touched the selector move to manual.

## [2.1.3] - 2026-07-18

### Fixed
- **Event-mode recording no longer triggers from driving past event entry
  points** (race markers, speed traps, drift zones). Passing one makes the
  game preview that event's route — pushing `DistanceTraveled` negative at
  road speed — which looked like grid staging. Staging now also requires
  the car to be near-stationary, matching every validated capture; the
  analysis-side run detector applies the same guard so drive-pasts can't
  produce phantom runs. All four real reference captures still detect
  their runs correctly.

## [2.1.2] - 2026-07-18

### Added
- **Per-wheel wheelspin split** (driven wheels only): time per wheel, time
  with both driven wheels spinning together, and turning-vs-straight split
  — the numbers that decide the FWD/AWD diff question (one-wheel flare →
  more diff lock; both-wheel spin → less power or more tyre).
- **Build-level signal**: a FWD session with sustained driven-wheel
  wheelspin and high observed power gets an explicit note that the build,
  not the tune, may be the limiter.

## [2.1.1] - 2026-07-18

### Fixed
- **Run route lengths are now physically consistent.** A real circuit
  capture showed `DistanceTraveled` advancing ~2.9x faster than the car
  moved, producing an impossible "12.1 km route in a 4.2 km session". Wire
  distance is now used only for start/end boundary detection; route length
  integrates speed over the run window.
- Runs are no longer labelled "free-roam time attack" — some circuit event
  types broadcast **no lap fields at all** (verified: LapNumber/CurrentLap/
  LastLap/BestLap all zero for an entire 2-lap race), so a detected run may
  be a full multi-lap race timed as one. The report says so, and per-lap
  splitting for those events is tracked on the roadmap (#5).
- **AWD setups now show the full differential set** — front accel/decel,
  rear accel/decel, and centre balance — with the fields switching to match
  the selected drivetrain (FWD: front only; RWD: rear only). Legacy saved
  setups map their single diff pair onto the correct axle.

## [2.1.0] - 2026-07-18

### Added
- **Saved setups (Tune Profiles, phone-first).** "Copy for AI + my setup"
  opens a thumb-friendly form — number-pad inputs for every tuning-screen
  value, a drivetrain selector (FWD/RWD/AWD, pre-filled from telemetry,
  driving which diff fields appear), gearbox type, car/build text and a
  goal line. Saving stores the setup **versioned per car** (v1, v2, … or
  your own name); next time you pick it from a dropdown, tweak, and save a
  new version. The copied report embeds your actual values instead of a
  blank fill-in block.
- **"Copy data only"** — the telemetry-only report, one tap, for when the
  AI already knows your setup.
- Setups API: `GET/POST /api/setups`, and
  `/api/sessions/{id}/tuning.md?setup_id=N` / `?mode=data`.

### Fixed
- Sessions ending on a menu/loading frame no longer blank their car
  metadata ("ordinal 0"): zeroed frames are excluded from the metadata
  roll-up.

## [2.0.1] - 2026-07-18

### Fixed
- **Route maps auto-zoom to the driven route.** A single stray point from
  before an event teleport could set the map's bounds, shrinking a touge
  route to a corner thumbnail — bounds now ignore trivial segments (<1% of
  driven length), so the route fills the canvas. Applies to analysis and
  comparison maps.
- The Analysis page's events grid showed different wheelspin/brake-lock
  numbers than the verdict card above it (two detector generations on one
  page); both now read from the drivetrain-aware grouped detector.

## [2.0.0] - 2026-07-18

First public release.

### The core

- **Live pit instrument** (installable PWA): portrait, landscape-mount and
  desktop layouts; fixed-width numerals with zero layout shift; smoothed
  tyre pods coloured by the temperature window; pedal ribbons on the
  screen edges; drift-corrected lap clock; Wake Lock;
  `prefers-reduced-motion` respected.
- **Recording, three modes** (switchable on the Live page, remembered):
  *Auto: events* (default — arms when you're staged at a start line or lap
  timing goes live, ends with the event, restart-safe), *Auto: any
  driving*, and *Manual*. Every session closes after 30 s stationary or in
  menus; staging at a line never counts as stationary.
- **Laps AND free-roam time-attack runs**: the packet carries no lap data
  in free-roam events, so runs are detected from the start-line staging
  signature (validated against real captures of both staging variants) and
  timed to the millisecond, with best times on the session list.
- **Tuning-grade analysis**: drivetrain-aware wheelspin (driven wheels
  follow FWD/RWD/AWD), per-axle brake locking as a share of braking time,
  slide events with hysteresis, sustained cornering grip (0.4 s held
  windows; collisions/kerbs excluded), corner-phase balance
  (entry/mid/exit/lift) with a severity+confidence handling summary,
  active-driving tyre temperatures, suspension bottom-out detail, and
  observed power/torque peaks from valid pulls only.
- **AI tuning export**: one tap copies a Markdown report built for
  Claude/ChatGPT — data-provenance labels (telemetry / estimated /
  user-entered), the analysis prompt, a fill-in block for your setup, and
  when the car is unknown the AI is instructed to ask you what it is
  before analysing. Per-lap CSV and raw CSV downloads too.
- **Car identity**: Forza broadcasts only a numeric ordinal — name a car
  once ("✎ name car") and it applies everywhere; a versioned community
  ordinal database ships in `app/data/car_ordinals.json`
  (verified-in-game entries only; contributions welcome).
- **Coexistence**: mirror the raw 60 Hz stream to a second tool
  (SimHub etc.) from the Debug page — Forza's single Data Out target
  stops being a choice.
- **Claude MCP server** (optional): `fh6-telemetry.exe --mcp` /
  `python -m app.mcp_server` lets Claude query sessions and tuning
  reports directly ([docs/CLAUDE-MCP.md](docs/CLAUDE-MCP.md)).
- **Respectful by design**: one visible `data\` folder (~250 MB per hour
  of driving, sizes shown in the UI), sub-5 s blips discarded, optional
  retention cap, stale temp-folder cleanup, no accounts, no analytics —
  the only possible network call is the user-initiated update check.
- **Validated packet layout**: FH6 emits the FH4/FH5 324-byte packet;
  every offset is pinned by tests and the `/debug` page shows a live
  physics cross-check (`Speed` must equal `|Velocity|`). Recordings made
  with early mis-decoded builds are rescued automatically.

[2.1.6]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.6
[2.1.5]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.5
[2.1.4]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.4
[2.1.3]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.3
[2.1.2]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.2
[2.1.1]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.1
[2.1.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.0
[2.0.1]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.0.1
[2.0.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.0.0
