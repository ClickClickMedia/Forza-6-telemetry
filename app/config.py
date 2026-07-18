"""Runtime configuration, sourced from environment variables with sane defaults."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _is_frozen() -> bool:
    """True when running as a PyInstaller-bundled executable."""
    return getattr(sys, "frozen", False)


def _default_data_dir() -> Path:
    """Where to store SQLite + raw sessions when FH6_DATA_DIR is unset.

    * Explicit ``FH6_DATA_DIR`` always wins (Docker Compose sets ``/app/data``).
    * A bundled ``.exe`` writes to a ``data`` folder next to the executable, so
      a portable install keeps its recordings alongside it.
    * Otherwise (source checkout / container WORKDIR) use a relative ``data``.
    """
    env = os.environ.get("FH6_DATA_DIR")
    if env:
        return Path(env)
    if _is_frozen():
        return Path(sys.executable).resolve().parent / "data"
    return Path("data")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def lan_ip() -> str:
    """Best-effort primary LAN IP (no packets are sent — connecting a UDP
    socket just selects the outbound interface)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@dataclass(frozen=True)
class Settings:
    # --- Networking -------------------------------------------------------
    udp_host: str = os.environ.get("FH6_UDP_HOST", "0.0.0.0")
    udp_port: int = _env_int("FH6_UDP_PORT", 9876)
    http_host: str = os.environ.get("FH6_HTTP_HOST", "0.0.0.0")
    http_port: int = _env_int("FH6_HTTP_PORT", 8080)

    # --- Forwarding -------------------------------------------------------
    # Mirror every raw Data Out packet to a second consumer (e.g. SimHub)
    # as "ip:port". Forza only allows ONE Data Out target, so this lets
    # this app and another tool coexist. Changeable from the Debug page.
    forward_to: str = os.environ.get("FH6_FORWARD", "").strip()

    # --- Live push --------------------------------------------------------
    # Browser update rate. The game emits ~60 fps; we coalesce and push at this
    # rate so phones are not overwhelmed.
    push_hz: float = _env_float("FH6_PUSH_HZ", 18.0)

    # --- Recording --------------------------------------------------------
    data_dir: Path = _default_data_dir()
    # Auto-end a session after this many seconds of silence (pause menus
    # stop the stream; 30 s means a quick map check doesn't split sessions).
    session_idle_timeout_s: float = _env_float("FH6_SESSION_IDLE_TIMEOUT", 30.0)
    # Storage format for raw frames: "csv" or "parquet".
    raw_format: str = os.environ.get("FH6_RAW_FORMAT", "csv").lower()
    # Recording mode: "manual" (default) records only via the ● Record
    # button — nothing is written until the user asks; "event" starts when
    # a timed event begins (staged at a start line or lap fields live);
    # "motion" records any driving. Changeable from the Live page
    # (persisted); this env sets the initial default.
    record_mode: str = os.environ.get("FH6_RECORD_MODE", "manual").lower()
    # Every session (manual included) closes after this long stationary —
    # the walk-away net. Staging at a start line doesn't count as stationary.
    stationary_timeout_s: float = _env_float("FH6_STATIONARY_TIMEOUT", 30.0)
    # Auto-recorded blips shorter than this are discarded at close (manual
    # recordings are always kept).
    keep_min_s: float = _env_float("FH6_KEEP_MIN_S", 5.0)
    # Optional retention cap for the sessions folder, in MB. 0 = unlimited
    # (default — nothing is ever deleted without you asking). When set, the
    # oldest auto-recorded, un-renamed, un-annotated sessions are pruned
    # after each session closes until usage is back under the cap.
    max_data_mb: float = _env_float("FH6_MAX_DATA_MB", 0.0)

    # --- Synthetic generator ---------------------------------------------
    # When enabled, an internal generator feeds the pipeline so the dashboard
    # works without an Xbox. Enable with FH6_SYNTHETIC=1.
    synthetic: bool = os.environ.get("FH6_SYNTHETIC", "0") in ("1", "true", "True")
    synthetic_hz: float = _env_float("FH6_SYNTHETIC_HZ", 60.0)

    # --- Logging ----------------------------------------------------------
    log_level: str = os.environ.get("FH6_LOG_LEVEL", "INFO").upper()
    # The exe's console window is user-facing: default it to the human
    # format. Containers keep JSON by default (compose sets it explicitly).
    log_json: bool = os.environ.get(
        "FH6_LOG_JSON", "0" if _is_frozen() else "1"
    ) in ("1", "true", "True")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sessions.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"


settings = Settings()
