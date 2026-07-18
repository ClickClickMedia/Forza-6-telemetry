# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

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

[1.0.0]: https://github.com/ClickClickMedia/Forza-6-telemetry/releases/tag/v1.0.0
