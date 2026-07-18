# Running with Docker

> Most Windows users should use the **[one-file .exe](../README.md#get-started-windows-5-minutes)**
> instead — nothing to install. Docker is the right choice on **Linux**, a
> **home server**, or if you want the optional Parquet storage backend. It
> runs the exact same app.

## Quick start

```bash
git clone https://github.com/ClickClickMedia/Forza-6-telemetry.git
cd Forza-6-telemetry
docker compose up -d --build
```

This publishes `9876/udp` (telemetry in) and `8080/tcp` (dashboard) and
persists data to `./data`. Then:

1. Point Forza's **Data Out** at your Docker host's LAN IP, port **9876**
   (in-game steps in the [README](../README.md#get-started-windows-5-minutes) —
   step 4 applies to any install method).
2. Open `http://<docker-host-ip>:8080` on a phone on the same Wi-Fi.

### Test it without the game

```bash
FH6_SYNTHETIC=1 docker compose up -d     # simulated car drives the dashboard
# set FH6_SYNTHETIC=0 (or remove it) and re-up to go back to real telemetry
```

## Docker Desktop on Windows, step by step

Commands are for **PowerShell**; notes call out where **WSL** differs.
Assumes Docker Desktop is running (default WSL2 backend is fine).

1. **Verify Docker is ready**

   ```powershell
   docker version
   docker compose version
   ```

   Both should print versions with no error. If `docker` isn't found, start
   **Docker Desktop** and wait for the whale icon to report "running".

2. **Get the code**

   ```powershell
   cd $HOME
   git clone https://github.com/ClickClickMedia/Forza-6-telemetry.git
   cd Forza-6-telemetry
   ```

   > **WSL:** identical, but clone into the Linux filesystem (`~/`) rather
   > than `/mnt/c/...` for much faster bind-mount performance.

3. **Build and start**

   ```powershell
   docker compose up -d --build
   docker compose ps                                  # should show "running"
   Invoke-RestMethod http://localhost:8080/health     # status: ok, packet_size: 324
   ```

   > **WSL / cmd:** use `curl http://localhost:8080/health`.

4. **Try the synthetic generator first** (optional but recommended):

   ```powershell
   @'
   services:
     fh6-telemetry:
       environment:
         FH6_SYNTHETIC: "1"
   '@ | Set-Content docker-compose.override.yml

   docker compose up -d
   ```

   Open **http://localhost:8080** — you should see a car lapping. When ready
   for real telemetry:

   ```powershell
   Remove-Item docker-compose.override.yml
   docker compose up -d
   ```

5. **Find your PC's LAN IP** (for the phone and the game): `ipconfig` →
   IPv4 Address under your active adapter, e.g. `192.168.1.50`.

   > With Docker Desktop the published ports live on the **Windows host
   > IP** — use that address, not any `172.x` WSL/Docker address.

6. **Open the Windows firewall** (PowerShell **as Administrator**):

   ```powershell
   New-NetFirewallRule -DisplayName "FH6 Telemetry UDP" -Direction Inbound -Protocol UDP -LocalPort 9876 -Action Allow
   New-NetFirewallRule -DisplayName "FH6 Dashboard TCP" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
   ```

7. **Turn on Data Out in FH6** (Settings → HUD and Gameplay → Data Out:
   ON, IP = your host IP, port 9876), drive, and check packets arrive:

   ```powershell
   Invoke-RestMethod http://localhost:8080/api/status | ConvertTo-Json -Depth 5
   ```

   Under `receiver`, `pps` should be **> 0** and `connected : True`.

## Linux firewall

```bash
# ufw
sudo ufw allow 9876/udp
sudo ufw allow 8080/tcp

# firewalld
sudo firewall-cmd --permanent --add-port=9876/udp
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

## Handy commands

```powershell
docker compose logs -f            # live logs (Ctrl+C to stop viewing)
docker compose restart            # restart the app
docker compose down               # stop and remove the container
docker compose up -d --build      # rebuild after pulling code changes
```

## Parquet storage (Docker-only option)

The container image includes the optional Parquet backend for raw frames
(`FH6_RAW_FORMAT: "parquet"` in the compose environment). The Windows exe
uses CSV only, to keep the binary small. CSV is the default everywhere and
is what the automatic v1.0.x rescue supports.

## Troubleshooting

- **`pps` stays 0 with Data Out on** → wrong IP in the game, missing
  firewall rule, or the devices are on a **guest network / "AP isolation"**
  that blocks device-to-device traffic. Put everything on the same normal
  Wi-Fi.
- **Phone can't load `:8080`** → confirm the TCP firewall rule and that
  you're using the PC's LAN IP, not `localhost`.
- **`/debug` shows a size other than 324** → a different Forza title is
  sending; this build is FH6/FH5-layout only by design.
- **Port already in use** → change the `ports:` mapping in
  `docker-compose.yml` (e.g. `8081:8080`) and use that port from the phone.
