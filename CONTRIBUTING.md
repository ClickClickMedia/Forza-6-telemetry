# Contributing

Thanks for helping improve FH6 Telemetry! This project is small, dependency-light,
and easy to hack on. The single most valuable contribution is **confirming or
correcting the FH6 packet layout** against a real game — see below.

## Getting set up

```bash
git clone https://github.com/ClickClickMedia/Forza-6-telemetry.git
cd Forza-6-telemetry
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest                                               # should be all green
```

Run it locally with fake telemetry (no Xbox needed):

```bash
FH6_SYNTHETIC=1 FH6_DATA_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8080
# open http://localhost:8080
```

## Project layout

```
app/
  packet.py         FH6 324-byte spec + parser + packer   <-- the heart of it
  udp_receiver.py   asyncio UDP listener
  telemetry_hub.py  stats + 18 Hz WebSocket broadcast
  recorder.py       session lifecycle + CSV/Parquet capture
  database.py       SQLite metadata + markers
  analysis.py       per-session metrics + event detection
  comparison.py     two-session compare + route tracing
  session_data.py   raw-file loader (numpy)
  synthetic.py      synthetic packet generator
  main.py           FastAPI app + routes + WebSocket
  static/           dashboard, analysis, compare, debug pages (vanilla HTML/JS)
tests/              pytest suite
```

## Validating or fixing the packet layout ⭐

The whole tool depends on the byte offsets in `app/packet.py`. If a value on the
dashboard or `/debug` page looks wrong for your car/game, here's the workflow:

1. **Capture the truth.** Open **`/debug`** while the game streams telemetry. It
   lists every field with its **byte offset**, wire **type**, and **decoded
   value**. Compare a few you can sanity-check (speed, RPM, gear, throttle).
2. **Locate the field.** All fields are defined in one ordered table,
   `_FIELD_TABLE` in [`app/packet.py`](app/packet.py). Each row is
   `(name, struct_code)` where the code is standard `struct` syntax
   (`i`=int32, `I`=uint32, `f`=float32, `H`=uint16, `B`=uint8, `b`=int8,
   `x`=one reserved byte).
3. **Make the change.** Reorder/retype the row(s). The module asserts the total
   is exactly **324 bytes** at import, so if your edit breaks the size you'll
   know immediately.
4. **Update the offset tests.** `tests/test_packet.py` pins the offset of every
   anchor field. Adjust the expected offsets to match your fix and run `pytest`.
5. **Open a PR** (or an issue with a `/debug` screenshot if you'd rather we make
   the change). Please include the car and game build you tested with.

> Guiding rules for this project, established by live-capture validation:
> - The packet is **exactly 324 bytes** and decoded **little-endian** — the
>   FH4/FH5 "Horizon" layout, confirmed against real FH6 telemetry.
> - The Horizon 12-byte block (`CarGroup`, `Unknown1`, `Unknown2`) sits at
>   offsets **232–243**; the dash tail starts with `PositionX` at **244**; one
>   trailing byte (`Unknown3`) closes the packet at **323**.
> - The strongest validation is physics, not vibes: dash `Speed` must equal
>   sled `|Velocity|` on every moving frame. A one-byte layout error breaks
>   that instantly (see `app/rescue.py` for the machinery).
> - FH6 has **no** `TireWear` and **no** `TrackOrdinal` — don't add them back
>   from a Forza Motorsport (2023) parser without a real capture proving
>   otherwise.

## Coding conventions

- Match the surrounding style; keep comments where they explain *why*.
- No new runtime dependencies without discussion — the point is a light,
  self-contained tool. The frontend uses **zero** external libraries (all charts
  are hand-drawn on Canvas 2D); please keep it that way.
- Add or update a test for any behaviour change. `pytest` must stay green.

## Reporting bugs / requesting features

Use the [issue templates](.github/ISSUE_TEMPLATE). For packet problems, the
**Packet mismatch** template asks for exactly the info needed to fix it fast.

By contributing, you agree your contributions are licensed under the project's
[MIT License](LICENSE).
