"""Structured logging setup.

Emits either line-delimited JSON (default, container-friendly) or a compact
human format for local dev. A single call to :func:`configure` wires the root
logger; everything else uses ``logging.getLogger(__name__)``.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict


def _display_name(name: str) -> str:
    """uvicorn's main logger is literally NAMED 'uvicorn.error' even for
    routine INFO messages — rendered verbatim it makes healthy logs look
    broken. Show it as plain 'uvicorn'."""
    return "uvicorn" if name.startswith("uvicorn.error") else name


class JsonFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": _display_name(record.name),
            "msg": record.getMessage(),
        }
        # Attach any structured extras passed via logger.info(..., extra={"extra": {...}})
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """Compact single-line format for the console window."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        head = f"{ts}  {record.levelname:<7} {_display_name(record.name)}: {record.getMessage()}"
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict) and extra:
            head += "  (" + ", ".join(f"{k}={v}" for k, v in extra.items()) + ")"
        if record.exc_info:
            head += "\n" + self.formatException(record.exc_info)
        return head


def configure(level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if as_json else HumanFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    # Quieten noisy libraries. uvicorn's per-connection WebSocket chatter
    # (open/closed for every phone reconnect) drowns the session lifecycle
    # messages that actually matter in the console.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


def log_extra(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Helper to log with structured extra fields."""
    logger.log(level, msg, extra={"extra": fields})
