"""Standalone launcher for the FH6 telemetry app.

This is the entry point used by the PyInstaller build to produce a single
double-clickable executable (``fh6-telemetry.exe`` on Windows). It starts the
same FastAPI app that Docker runs, but binds directly to the host — so there is
no container/NAT layer between Forza's UDP stream and the receiver.

Run behaviour:
    * Prints the dashboard URLs (localhost + best-guess LAN IP) so you know
      what to open on your phone.
    * Starts uvicorn on 0.0.0.0:8080 (configurable via the same FH6_* env vars).
    * Ctrl+C triggers the app's graceful shutdown (flushes any open session).

All configuration still comes from the FH6_* environment variables documented
in the README; sensible defaults apply when they are unset.
"""

from __future__ import annotations

import socket
import sys

import uvicorn

from app.config import settings
from app.logging_config import configure


def _lan_ip() -> str:
    """Best-effort primary LAN IP of this machine (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket doesn't send anything; it just selects the
        # outbound interface the OS would use to reach a public address.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _banner() -> None:
    lan = _lan_ip()
    port = settings.http_port
    line = "=" * 60
    print(line)
    print("  Forza Horizon 6 Telemetry")
    print(line)
    print(f"  Dashboard (this PC) : http://localhost:{port}")
    print(f"  Dashboard (phone)   : http://{lan}:{port}")
    print(f"  Forza Data Out      : send UDP to {lan} : {settings.udp_port}")
    print("")
    print("  In Forza: Settings > HUD and Gameplay > Data Out")
    print(f"    IP Address = {lan}   Port = {settings.udp_port}   Data Out = ON")
    if settings.synthetic:
        print("")
        print("  [synthetic generator ON — dashboard works without an Xbox]")
    print("")
    print("  Press Ctrl+C to stop.")
    print(line, flush=True)


def _cleanup_stale_bundles() -> None:
    """Remove orphaned PyInstaller extraction dirs from previous runs.

    Onefile builds unpack to %TEMP%\\_MEIxxxxxx and normally clean up on
    exit — but a kill/crash/power loss leaves the folder behind (~100 MB
    each). Only dirs that are provably ours (contain our manifest), are not
    the currently running bundle, and are older than 48 h are removed.
    """
    if not getattr(sys, "frozen", False):
        return
    import shutil
    import tempfile
    import time
    from pathlib import Path

    own = getattr(sys, "_MEIPASS", None)
    tmp = Path(tempfile.gettempdir())
    for d in tmp.glob("_MEI*"):
        try:
            if own and d.resolve() == Path(own).resolve():
                continue
            if not (d / "app" / "static" / "manifest.webmanifest").exists():
                continue  # some other application's bundle — leave it alone
            if time.time() - d.stat().st_mtime < 48 * 3600:
                continue  # possibly another live instance
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def main() -> None:
    _cleanup_stale_bundles()
    if "--mcp" in sys.argv:
        # MCP stdio mode: expose the running dashboard's data to Claude
        # (Desktop/Code). Point FH6_URL at the dashboard if not default.
        # No banner, no server — stdout belongs to the MCP protocol.
        from app.mcp_server import main as mcp_main
        mcp_main()
        return

    _banner()
    configure(settings.log_level, settings.log_json)
    # Import the app object directly (rather than an "app.main:app" string):
    # this avoids any import-path resolution quirks inside the frozen bundle,
    # and we use neither reload nor workers (which would require the string).
    from app.main import app

    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,      # we configure logging ourselves
        access_log=False,
    )


if __name__ == "__main__":
    main()
