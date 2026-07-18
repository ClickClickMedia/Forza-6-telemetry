# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-07-18

### Fixed
- **Packet layout corrected — this fixes wrong speed, wildly fluctuating tyre
  temps/torque, and garbage lap times.** v1.0.0 used an invented "FH6
  car-info block" with a pad byte before `PositionX`, shifting every
  dash-tail field one byte late. Live captures prove FH6 emits the exact
  FH4/FH5 324-byte layout (12-byte block at offsets 232–243, dash tail at
  244, one trailing byte at 323). Validated frame-by-frame: dash `Speed`
  equals sled `|Velocity|` to 3 decimal places.
- Tyre temperatures were Fahrenheit on the wire but displayed with a °C
  label. The parser now converts properly; the dashboard defaults to °C with
  a °C/°F toggle (and km/h ↔ mph on the speedo).

### Added
- **Automatic rescue of v1.0.x recordings** (`python -m app.rescue`, also
  runs at startup): losslessly re-decodes old CSVs under the corrected
  layout, validates every frame with the Speed-vs-velocity physics check,
  keeps originals as `*.v1bak`, and recomputes stored best laps.
- **Lap segmentation + tuning aggregates** (`/api/sessions/{id}/laps`): per
  lap and per session — tyre temps (°C) with front/rear balance, drift-aware
  understeer index (opposite-lock and handbrake frames excluded), per-axle
  slide times, wheelspin/brake-lock events, suspension travel and
  bottom-outs, shift RPM, time-on-limiter, and balance/temperature verdicts.
- **AI tuning exports**: one-tap "Copy tuning report" (Markdown with laps,
  verdicts, a current-setup fill-in block and an analysis prompt for
  Claude/ChatGPT), `.md` download, and a per-lap `.csv`
  (`/api/sessions/{id}/tuning.md`, `/laps.csv`).
- **MCP server** (`python -m app.mcp_server` or `fh6-telemetry.exe --mcp`):
  dependency-free stdio Model Context Protocol server so Claude Desktop /
  Claude Code can query live status, sessions, laps and tuning reports
  directly.
- Live payload now includes position, race time, distance, fuel and race
  position; the debug page shows a live Speed-vs-|Velocity| physics check.

### Changed
- **Dashboard rebuilt as a mobile-first pit instrument** ("sodium pit lane"
  design system): fixed-width tabular numerals with zero layout shift,
  per-channel EMA smoothing (no more flashing values), colour-by-temperature
  tyre pods around a car glyph, edge pedal ribbons (brake left, throttle
  right), smooth drift-corrected lap clock, landscape phone instrument mode,
  bottom tab bar, and `prefers-reduced-motion` support. Sessions became a
  card list with an export menu; analysis gained verdict cards and a laps
  table.
- Live WebSocket payload: `tire_temp` (ambiguous unit) replaced by
  `tire_temp_c` and `tire_temp_f`. Anyone consuming `/ws/live` should update.
- Synthetic generator now emits Fahrenheit tyre temps (as the real game does)
  and the corrected packet field set.

## [1.0.0] - 2026-07-18

Initial release.

### Added
- FH6 "Data Out" UDP receiver (asyncio `DatagramProtocol`) bound to
  `0.0.0.0:9876`, validating the exact 324-byte packet and decoding every field
  little-endian; per-field offsets covered by unit tests.
- Live mobile dashboard (dark theme, landscape mode, installable PWA, Wake Lock,
  auto-reconnecting WebSocket) updated at ~18 Hz. No external CDN dependencies;
  charts are hand-drawn on Canvas 2D.
- Recording: auto session on `IsRaceOn -> 1`, idle auto-end, manual
  start/stop/marker, raw CSV/Parquet capture with monotonic timestamps, SQLite
  metadata, session rename, and CSV download.
- Analysis: speed, acceleration, throttle/brake time, gear usage, shift RPM,
  tyre temps/slip time, and event detection (wheelspin, brake lock, suspension
  bottom-out/full-extension, over/understeer candidates, and more).
- Two-session comparison with overlaid channel charts and an XY route trace from
  `PositionX`/`PositionZ`, coloured by speed or rear slip; point-to-point runs
  supported (no `TrackOrdinal` assumed).
- Operations: `/health` and `/api/status`, structured JSON logging, graceful
  shutdown, a packet-debug page, and a synthetic telemetry generator.
- Packaging: `Dockerfile` + `docker-compose.yml`, and a standalone Windows
  executable built via PyInstaller in GitHub Actions.

[1.1.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v1.0.0
