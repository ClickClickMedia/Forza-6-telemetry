# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project follows
[Semantic Versioning](https://semver.org/).

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

[2.0.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v2.0.0
