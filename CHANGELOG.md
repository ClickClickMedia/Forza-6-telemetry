# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project follows
[Semantic Versioning](https://semver.org/).

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

[2.1.3]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.3
[2.1.2]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.2
[2.1.1]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.1
[2.1.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.1.0
[2.0.1]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.0.1
[2.0.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.0.0
