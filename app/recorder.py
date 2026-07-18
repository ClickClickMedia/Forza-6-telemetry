"""Session recording: raw-frame capture, lifecycle, and CSV/Parquet storage.

Every received (valid) frame is handed to :meth:`Recorder.feed`. The recorder
decides whether a session should be open based on ``IsRaceOn`` transitions and
an idle timeout, and streams raw frames to disk on a dedicated writer thread so
the asyncio event loop never blocks on file IO.

Storage layout::

    data/
      sessions.db                 (SQLite metadata)
      sessions/
        session_000123.csv        (raw frames, one row per received packet)

Raw columns are ``t_mono``, ``t_wall`` followed by every FH6 wire field.
"""

from __future__ import annotations

import csv
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .database import Database
from .packet import FIELD_NAMES, TelemetryFrame

log = logging.getLogger(__name__)

RAW_COLUMNS: List[str] = ["t_mono", "t_wall"] + FIELD_NAMES

# Sentinel pushed onto the writer queue to signal shutdown of a session file.
_CLOSE = object()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _ActiveSession:
    session_id: int
    raw_path: Path
    raw_format: str
    started_manually: bool
    t_start_mono: float
    q: "queue.Queue[Any]" = field(default_factory=queue.Queue)
    writer_thread: Optional[threading.Thread] = None
    frame_count: int = 0
    # Rolling metadata captured from frames.
    car_ordinal: Optional[int] = None
    car_class: Optional[int] = None
    car_pi: Optional[int] = None
    drivetrain: Optional[int] = None
    cylinders: Optional[int] = None
    car_group: Optional[int] = None
    best_lap: Optional[float] = None
    # For Parquet, frames are buffered and written on close.
    buffer: List[List[Any]] = field(default_factory=list)


class Recorder:
    """Owns session lifecycle and raw-frame persistence.

    Thread-safety: :meth:`feed`, control methods and the writer thread all touch
    ``_active`` under ``_lock``. Disk writes happen only on the writer thread.
    """

    def __init__(
        self,
        db: Database,
        sessions_dir: Path,
        idle_timeout_s: float = 5.0,
        raw_format: str = "csv",
    ):
        self.db = db
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.idle_timeout_s = idle_timeout_s
        self.raw_format = raw_format if raw_format in ("csv", "parquet") else "csv"
        self._lock = threading.RLock()
        self._active: Optional[_ActiveSession] = None
        self._last_feed_mono: float = 0.0
        self._last_race_on: int = 0
        self._manual_requested: bool = False

    # -- Introspection -----------------------------------------------------
    @property
    def active_session_id(self) -> Optional[int]:
        with self._lock:
            return self._active.session_id if self._active else None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            if not self._active:
                return {"recording": False, "session_id": None, "frame_count": 0}
            return {
                "recording": True,
                "session_id": self._active.session_id,
                "frame_count": self._active.frame_count,
                "manual": self._active.started_manually,
            }

    # -- Feed --------------------------------------------------------------
    def feed(self, frame: TelemetryFrame, t_mono: float) -> None:
        """Ingest one valid frame. Handles auto start/stop and raw capture."""
        with self._lock:
            self._last_feed_mono = t_mono
            race_on = int(frame.IsRaceOn)

            # Auto-start when IsRaceOn transitions 0 -> 1 (unless already open).
            if self._active is None:
                if self._manual_requested or (self._last_race_on == 0 and race_on == 1):
                    self._open_session(frame, t_mono, manual=self._manual_requested)
                    self._manual_requested = False
            self._last_race_on = race_on

            if self._active is not None:
                self._append(frame, t_mono)

    def check_idle(self, now_mono: float) -> None:
        """Auto-end the active session if no data has arrived recently.

        Called periodically by the app's housekeeping loop.
        """
        with self._lock:
            if self._active is None:
                return
            if now_mono - self._last_feed_mono > self.idle_timeout_s:
                log.info(
                    "session idle timeout, closing",
                    extra={"extra": {"session_id": self._active.session_id}},
                )
                self._close_session()

    # -- Manual control ----------------------------------------------------
    def start_manual(self) -> Dict[str, Any]:
        with self._lock:
            if self._active is not None:
                return {"ok": False, "reason": "already recording",
                        "session_id": self._active.session_id}
            # Arm: the next frame opens the session (so we capture real car meta).
            self._manual_requested = True
            return {"ok": True, "armed": True}

    def stop_manual(self) -> Dict[str, Any]:
        with self._lock:
            self._manual_requested = False
            if self._active is None:
                return {"ok": False, "reason": "not recording"}
            sid = self._active.session_id
            self._close_session()
            return {"ok": True, "session_id": sid}

    def add_marker(self, label: str, now_mono: float) -> Dict[str, Any]:
        with self._lock:
            if self._active is None:
                return {"ok": False, "reason": "not recording"}
            t_rel = now_mono - self._active.t_start_mono
            marker_id = self.db.add_marker(self._active.session_id, t_rel, label)
            return {"ok": True, "marker_id": marker_id, "t_mono": t_rel}

    def shutdown(self) -> None:
        """Flush and close any open session (graceful shutdown)."""
        with self._lock:
            if self._active is not None:
                self._close_session()

    # -- Internal ----------------------------------------------------------
    def _open_session(self, frame: TelemetryFrame, t_mono: float, manual: bool) -> None:
        created = _utc_now_iso()
        # Temporary name; final row id is known after insert.
        raw_name_placeholder = "pending"
        session_id = self.db.create_session(
            name=f"Session {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            created_at=created,
            started_manually=manual,
            raw_path=raw_name_placeholder,
            raw_format=self.raw_format,
        )
        ext = "csv" if self.raw_format == "csv" else "parquet"
        raw_name = f"session_{session_id:06d}.{ext}"
        raw_path = self.sessions_dir / raw_name
        # Persist the real relative path now that we know the row id.
        self.db.set_raw_path(session_id, raw_name, self.raw_format)

        active = _ActiveSession(
            session_id=session_id,
            raw_path=raw_path,
            raw_format=self.raw_format,
            started_manually=manual,
            t_start_mono=t_mono,
        )
        if self.raw_format == "csv":
            active.writer_thread = threading.Thread(
                target=self._csv_writer_loop,
                args=(active,),
                name=f"rec-writer-{session_id}",
                daemon=True,
            )
            active.writer_thread.start()
        self._active = active
        log.info(
            "session started",
            extra={"extra": {"session_id": session_id, "manual": manual,
                             "format": self.raw_format}},
        )

    def _append(self, frame: TelemetryFrame, t_mono: float) -> None:
        active = self._active
        assert active is not None
        t_rel = t_mono - active.t_start_mono
        row: List[Any] = [round(t_rel, 6), _utc_now_iso()]
        d = frame.as_dict()
        row.extend(d[name] for name in FIELD_NAMES)

        if active.raw_format == "csv":
            active.q.put(row)
        else:
            active.buffer.append(row)

        active.frame_count += 1

        # Roll up metadata.
        active.car_ordinal = int(frame.CarOrdinal)
        active.car_class = int(frame.CarClass)
        active.car_pi = int(frame.CarPerformanceIndex)
        active.drivetrain = int(frame.DrivetrainType)
        active.cylinders = int(frame.NumCylinders)
        active.car_group = int(frame.CarGroup)
        if frame.BestLap and frame.BestLap > 0:
            if active.best_lap is None or frame.BestLap < active.best_lap:
                active.best_lap = float(frame.BestLap)

    def _csv_writer_loop(self, active: _ActiveSession) -> None:
        """Dedicated thread: drain the queue into a CSV file."""
        try:
            with open(active.raw_path, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(RAW_COLUMNS)
                pending = 0
                while True:
                    item = active.q.get()
                    if item is _CLOSE:
                        break
                    writer.writerow(item)
                    pending += 1
                    if pending >= 120:  # flush ~ every 2s at 60Hz
                        fh.flush()
                        pending = 0
        except Exception:  # pragma: no cover - writer must never take down app
            log.exception("csv writer failed",
                          extra={"extra": {"session_id": active.session_id}})

    def _write_parquet(self, active: _ActiveSession) -> None:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except Exception:
            log.error("pyarrow not available; falling back to CSV for session %s",
                      active.session_id)
            # Fallback: dump the buffer to CSV alongside.
            fallback = active.raw_path.with_suffix(".csv")
            with open(fallback, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(RAW_COLUMNS)
                writer.writerows(active.buffer)
            active.raw_path = fallback
            self.db.set_raw_path(active.session_id, fallback.name, "csv")
            return
        cols: Dict[str, List[Any]] = {c: [] for c in RAW_COLUMNS}
        for row in active.buffer:
            for c, v in zip(RAW_COLUMNS, row):
                cols[c].append(v)
        table = pa.table(cols)
        pq.write_table(table, active.raw_path)

    def _close_session(self) -> None:
        active = self._active
        if active is None:
            return
        self._active = None  # release the slot before slow IO

        if active.raw_format == "csv":
            active.q.put(_CLOSE)
            if active.writer_thread is not None:
                active.writer_thread.join(timeout=10)
        else:
            self._write_parquet(active)

        meta = {
            "car_ordinal": active.car_ordinal,
            "car_class": active.car_class,
            "car_pi": active.car_pi,
            "drivetrain": active.drivetrain,
            "cylinders": active.cylinders,
            "car_group": active.car_group,
            "best_lap": active.best_lap,
        }
        self.db.finalize_session(
            active.session_id, _utc_now_iso(), active.frame_count, meta
        )
        log.info(
            "session ended",
            extra={"extra": {"session_id": active.session_id,
                             "frames": active.frame_count}},
        )
