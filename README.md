# Forza Horizon 6 Telemetry

A self-hosted, Dockerised telemetry collector and mobile dashboard for
**Forza Horizon 6**. It listens for Forza's "Data Out" UDP stream, parses every
field of the **324-byte FH6 packet**, shows a live dashboard on your phone,
records sessions to disk, and analyses/compares runs — all on your own network,
with **no cloud dependencies**.

```
Xbox  ──UDP 9876──▶  Docker host  ──HTTP/WebSocket 8080──▶  Phone (same Wi-Fi)
(Data Out)          (this app)                              (dashboard PWA)
```

## Features

- **UDP receiver** — asyncio `DatagramProtocol` bound to `0.0.0.0:9876`.
  Accepts only 324-byte packets, parses the official FH6 field order
  (little-endian), and never crashes on malformed input. Tracks packets/sec,
  invalid/dropped packets and last-packet time.
- **Live mobile dashboard** — dark theme, large readouts, landscape mode,
  installable PWA, auto-reconnecting WebSocket, Wake Lock support. Browser is
  updated at ~18 Hz (configurable) rather than every game frame.
- **Recording** — auto-starts a session when `IsRaceOn` becomes 1, auto-ends
  after 5 s of silence, plus manual Start/Stop/Marker controls. Raw frames are
  stored with monotonic receive timestamps as CSV (or Parquet). Session
  metadata in SQLite. Rename sessions and download CSV from the phone.
- **Analysis** — per-session speed, acceleration, throttle/brake time, gear
  usage, shift RPM, tyre temps and slip time, and detected events (wheelspin,
  brake lock, bottom-out, over/understeer candidates, and more).
- **Comparison** — overlay two sessions' channels and draw an XY route trace
  from `PositionX`/`PositionZ`, coloured by speed or rear slip. Works for
  point-to-point runs (no `TrackOrdinal` assumed — FH6 has none).
- **Operations** — `/health` and `/api/status` endpoints, structured JSON
  logging, graceful shutdown, a packet-debug page, and a synthetic telemetry
  generator so you can test everything without an Xbox.

---

## The FH6 packet (324 bytes)

This project uses the **Forza Horizon 6** "Data Out" layout, **not** FH4/FH5 or
Forza Motorsport. The differences are load-bearing — the parser was built to the
FH6 spec and every offset is covered by unit tests
([`tests/test_packet.py`](tests/test_packet.py)):

| Property | FH4 / FH5 | Forza Motorsport 7 | **FH6 (this app)** |
| --- | --- | --- | --- |
| Packet size | 331 | 311 | **324** |
| `TireWear` (4×f32) | present | absent | **absent** |
| `TrackOrdinal` (s32) | present | absent | **absent** |
| `CarGroup`, `SmashableVelDiff`, `SmashableMass` | absent | absent | **present** |

FH6 inserts `CarGroup` (s32), `SmashableVelDiff` (f32) and `SmashableMass` (f32)
**immediately after `NumCylinders` and before `PositionX`** (followed by one
reserved/alignment byte so the block reconciles to the documented 324-byte
length). The full layout: **232-byte sled + 13-byte FH6 car-info block +
79-byte dash tail = 324**. See the module docstring in
[`app/packet.py`](app/packet.py) for the authoritative field table.

All scalars are decoded **little-endian**, validated by the round-trip and
explicit endianness unit tests before this is relied upon.

> If Microsoft revises the FH6 layout, adjust `_FIELD_TABLE` in `app/packet.py`
> — the import-time assertion and the offset tests will immediately tell you if
> the total size or any offset drifts.

---

## Quick start

### 1. Enable Data Out on the Xbox / PC

In **Forza Horizon 6**:

1. **Settings → HUD and Gameplay → Data Out** (sometimes under *Telemetry*).
2. Set **Data Out** = **ON**.
3. Set **Data Out IP Address** = the **IP of your Docker host** (e.g.
   `192.168.1.50`). Find it with `ip addr` (Linux) or `ipconfig` (Windows).
4. Set **Data Out IP Port** = **9876**.

The Xbox and the Docker host must be on the **same local network**.

### 2. Start the collector

```bash
git clone <this-repo> Forza-6-telemetry
cd Forza-6-telemetry
docker compose up -d --build
```

This publishes `9876/udp` (telemetry in) and `8080/tcp` (dashboard) and
persists data to `./data`.

### 3. Open the dashboard on your phone

On a phone connected to the **same Wi-Fi**, browse to:

```
http://<docker-host-ip>:8080
```

e.g. `http://192.168.1.50:8080`. Drive in-game and the dashboard comes alive.
Use your browser's **"Add to Home Screen"** to install it as a PWA.

### 4. Test without an Xbox (synthetic mode)

No console handy? Run the built-in generator, which emits real 324-byte FH6
packets to the UDP port:

```bash
# One-off: flip the compose env var and restart
FH6_SYNTHETIC=1 docker compose up -d
# ...or run the generator standalone against a running collector:
python -m app.synthetic --host 127.0.0.1 --port 9876 --hz 60
```

Set `FH6_SYNTHETIC=0` again to go back to real telemetry.

---

## Running on Windows (Docker Desktop)

Step-by-step for a Windows PC with **Docker Desktop** installed. Commands are
shown for **PowerShell**; notes call out where **WSL** differs. This assumes
Docker Desktop is running (the default WSL2 backend is fine).

### 1. Verify Docker is ready

```powershell
docker version
docker compose version
```

Both should print versions with no error. If `docker` isn't found, start
**Docker Desktop** and wait for the whale icon to report "running".

### 2. Get the code

```powershell
cd $HOME
git clone https://github.com/ClickClickMedia/Forza-6-telemetry.git
cd Forza-6-telemetry
```

> **WSL:** identical, but clone into the Linux filesystem (`~/`) rather than
> `/mnt/c/...` for much faster bind-mount performance.

### 3. Build and start

```powershell
docker compose up -d --build
```

The first build takes a few minutes. When it finishes:

```powershell
docker compose ps                                  # should show "running"
Invoke-RestMethod http://localhost:8080/health     # PowerShell-friendly
```

You should see `status : ok` and `packet_size : 324`.

> **WSL / cmd:** use `curl http://localhost:8080/health` instead of
> `Invoke-RestMethod`.

### 4. Test it works — before touching the Xbox

Run the built-in synthetic generator to confirm the dashboard end-to-end. Drop
a small override file into the project folder:

```powershell
@'
services:
  fh6-telemetry:
    environment:
      FH6_SYNTHETIC: "1"
'@ | Set-Content docker-compose.override.yml

docker compose up -d          # picks up the override automatically
```

Open **http://localhost:8080** in your PC browser — you should see a car
lapping. Check `/debug` too: it should show `received size 324`.

When you're ready for real telemetry, remove the override and restart:

```powershell
Remove-Item docker-compose.override.yml
docker compose up -d
```

### 5. Find your PC's LAN IP (for the phone and Xbox)

```powershell
ipconfig
```

Under your active **Wi-Fi** (or Ethernet) adapter, read the **IPv4 Address**,
e.g. `192.168.1.50` — that's your `<HOST-IP>`.

> With Docker Desktop the published ports live on the **Windows host IP** — use
> this address, not any `172.x` WSL/Docker address.

### 6. Open the Windows firewall

Run PowerShell **as Administrator**:

```powershell
New-NetFirewallRule -DisplayName "FH6 Telemetry UDP" -Direction Inbound -Protocol UDP -LocalPort 9876 -Action Allow
New-NetFirewallRule -DisplayName "FH6 Dashboard TCP" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

If Windows prompts that "Docker Desktop Backend wants to accept connections",
click **Allow**.

### 7. Turn on Data Out in Forza Horizon 6

On the Xbox (or the PC running FH6):

1. **Settings → HUD and Gameplay → Data Out** → **ON**
2. **Data Out IP Address** → your `<HOST-IP>` from step 5.
   (If FH6 runs on the **same PC** as Docker, use `127.0.0.1`.)
3. **Data Out IP Port** → **9876**

The Xbox and PC must be on the **same Wi-Fi/LAN**.

### 8. Confirm packets are arriving

Start driving, then on the PC:

```powershell
Invoke-RestMethod http://localhost:8080/api/status | ConvertTo-Json -Depth 5
```

Under `receiver`, `pps` should be **> 0** and `connected : True`.

### 9. Open the dashboard on your phone

On a phone on the **same Wi-Fi**, browse to `http://<HOST-IP>:8080` (e.g.
`http://192.168.1.50:8080`), then use **"Add to Home Screen"** to install the
PWA. Rotate to landscape for the full dashboard.

### Handy commands

```powershell
docker compose logs -f            # live logs (Ctrl+C to stop viewing)
docker compose restart            # restart the app
docker compose down               # stop and remove the container
docker compose up -d --build      # rebuild after pulling code changes
```

### Windows troubleshooting

- **`pps` stays 0 with Data Out on** → wrong IP in FH6, missing firewall rule,
  or the devices are on a **guest network / "AP isolation"** that blocks
  device-to-device traffic. Put everything on the same normal Wi-Fi.
- **Phone can't load `:8080`** → confirm the TCP firewall rule and that you're
  using the PC's LAN IP, not `localhost`.
- **`/debug` shows a size other than 324** → a different Forza title is
  sending; this build is FH6-only by design.
- **Port already in use** → change the `ports:` mapping in
  `docker-compose.yml` (e.g. `8081:8080`) and use that port from the phone.

---

## Firewall notes

The Docker host must allow **inbound UDP 9876** (from the Xbox) and **inbound
TCP 8080** (from your phone).

**Windows (PowerShell as Administrator):**

```powershell
New-NetFirewallRule -DisplayName "FH6 Telemetry UDP" -Direction Inbound -Protocol UDP -LocalPort 9876 -Action Allow
New-NetFirewallRule -DisplayName "FH6 Dashboard TCP" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

**Linux (ufw):**

```bash
sudo ufw allow 9876/udp
sudo ufw allow 8080/tcp
```

**Linux (firewalld):**

```bash
sudo firewall-cmd --permanent --add-port=9876/udp
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

If packets aren't arriving, open the **Debug** page (`/debug`) — it shows the
last received packet size. A size other than 324 means a different Forza title
or a truncated packet; a `pps` of 0 on `/api/status` with Data Out enabled
usually means a firewall or wrong IP/port.

---

## Pages & API

### Pages

| Path | Purpose |
| --- | --- |
| `/` | Live dashboard |
| `/sessions` | Session list, rename, notes, CSV download, delete |
| `/analysis?session=ID` | Per-session analysis + route trace |
| `/compare` | Two-session comparison + overlaid route |
| `/debug` | Live parsed packet field table |

### Key HTTP endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness + basic config |
| GET | `/api/status` | Receiver stats, recording state |
| WS | `/ws/live` | Live telemetry (~18 Hz JSON frames) |
| GET | `/api/sessions` | List sessions |
| GET | `/api/sessions/{id}` | Session detail + markers |
| PATCH | `/api/sessions/{id}` | Rename / edit notes |
| DELETE | `/api/sessions/{id}` | Delete session + raw file |
| GET | `/api/sessions/{id}/analysis` | Full analysis |
| GET | `/api/sessions/{id}/route?colour_by=speed\|rear_slip` | Route trace |
| GET | `/api/sessions/{id}/download.csv` | Download raw CSV |
| POST | `/api/recording/start` \| `/stop` \| `/marker` | Manual recording control |
| GET | `/api/compare?a=ID&b=ID&colour_by=...` | Compare two sessions |
| GET | `/api/debug/last` \| `/api/debug/spec` | Packet debug |

---

## Configuration

All configuration is via environment variables (see `docker-compose.yml`):

| Variable | Default | Description |
| --- | --- | --- |
| `FH6_UDP_HOST` | `0.0.0.0` | UDP bind address |
| `FH6_UDP_PORT` | `9876` | UDP telemetry port |
| `FH6_HTTP_HOST` | `0.0.0.0` | HTTP bind address |
| `FH6_HTTP_PORT` | `8080` | Dashboard/API port |
| `FH6_PUSH_HZ` | `18` | Browser live-update rate (Hz) |
| `FH6_SESSION_IDLE_TIMEOUT` | `5` | Auto-end session after N s of silence |
| `FH6_RAW_FORMAT` | `csv` | Raw storage: `csv` or `parquet` |
| `FH6_DATA_DIR` | `/app/data` | Data directory (SQLite + raw files) |
| `FH6_SYNTHETIC` | `0` | `1` to enable the synthetic generator |
| `FH6_SYNTHETIC_HZ` | `60` | Synthetic packet rate |
| `FH6_LOG_JSON` | `1` | `1` = JSON logs, `0` = human-readable |
| `FH6_LOG_LEVEL` | `INFO` | Log level |

> Note: `FH6_HTTP_PORT` sets the port **inside** the container. The published
> host port is controlled by the `ports:` mapping in `docker-compose.yml`
> (`8080:8080` by default). Change both if you want a different host port.

### Data layout

```
data/
  sessions.db                  SQLite: session metadata + markers
  sessions/
    session_000001.csv         raw frames (t_mono, t_wall, + all 88 FH6 fields)
```

Raw files are portable CSV — open them in Excel, pandas, or any tool.

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Run the test suite (packet offsets, analysis, recording, synthetic)
pytest

# Run locally without Docker (synthetic telemetry on)
FH6_SYNTHETIC=1 FH6_DATA_DIR=./data \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
# then open http://localhost:8080
```

### Project structure

```
app/
  packet.py         FH6 324-byte spec, parser, packer, debug (the core)
  udp_receiver.py   asyncio DatagramProtocol UDP listener
  telemetry_hub.py  stats, latest frame, 18 Hz WebSocket broadcast
  recorder.py       session lifecycle + raw CSV/Parquet capture (threaded writer)
  database.py       SQLite metadata + markers
  analysis.py       per-session metrics + event detection
  comparison.py     two-session compare + XY route tracing
  session_data.py   raw-file loader (numpy column arrays)
  synthetic.py      synthetic FH6 packet generator
  main.py           FastAPI app, WebSocket, routes, lifespan
  static/           dashboard, analysis, compare, debug pages + PWA assets
tests/              pytest suite
```

## Building and testing

```bash
pytest                    # unit + integration tests
docker compose build      # build the image
docker compose up -d      # run it
```

## License

Provided as-is for personal, local use. Not affiliated with or endorsed by
Microsoft, Turn 10, or Playground Games. "Forza Horizon" is a trademark of
Microsoft.
