"""Runtime configuration, sourced from environment variables with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class Settings:
    # --- Networking -------------------------------------------------------
    udp_host: str = os.environ.get("FH6_UDP_HOST", "0.0.0.0")
    udp_port: int = _env_int("FH6_UDP_PORT", 9876)
    http_host: str = os.environ.get("FH6_HTTP_HOST", "0.0.0.0")
    http_port: int = _env_int("FH6_HTTP_PORT", 8080)

    # --- Live push --------------------------------------------------------
    # Browser update rate. The game emits ~60 fps; we coalesce and push at this
    # rate so phones are not overwhelmed.
    push_hz: float = _env_float("FH6_PUSH_HZ", 18.0)

    # --- Recording --------------------------------------------------------
    data_dir: Path = Path(os.environ.get("FH6_DATA_DIR", "/app/data"))
    # Auto-end a session after this many seconds of silence.
    session_idle_timeout_s: float = _env_float("FH6_SESSION_IDLE_TIMEOUT", 5.0)
    # Storage format for raw frames: "csv" or "parquet".
    raw_format: str = os.environ.get("FH6_RAW_FORMAT", "csv").lower()

    # --- Synthetic generator ---------------------------------------------
    # When enabled, an internal generator feeds the pipeline so the dashboard
    # works without an Xbox. Enable with FH6_SYNTHETIC=1.
    synthetic: bool = os.environ.get("FH6_SYNTHETIC", "0") in ("1", "true", "True")
    synthetic_hz: float = _env_float("FH6_SYNTHETIC_HZ", 60.0)

    # --- Logging ----------------------------------------------------------
    log_level: str = os.environ.get("FH6_LOG_LEVEL", "INFO").upper()
    log_json: bool = os.environ.get("FH6_LOG_JSON", "1") in ("1", "true", "True")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sessions.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"


settings = Settings()
