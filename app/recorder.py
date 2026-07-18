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
from .packet import FIELD_NAMES, TelemetryFrame, sane_lap

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

    # Motion thresholds (m/s): start once genuinely rolling, treat
    # below-walking-pace as stationary for the auto-stop timer.
    MOVING_START_MS = 2.0
    STATIONARY_MS = 0.5
    # Event staging signals (validated on real captures): distance held at
    # exactly ~0 while stopped (route time attacks) or deep negative
    # (grid-start events). A snap drop ends the event.
    ZERO_HOLD_FRAMES = 60
    EVENT_SNAP_DROP_M = 50.0
    EVENT_SNAP_GRACE_S = 5.0   # a restart re-stages within this window

    MODES = ("event", "motion", "manual")

    def __init__(
        self,
        db: Database,
        sessions_dir: Path,
        idle_timeout_s: float = 30.0,
        raw_format: str = "csv",
        record_mode: str = "manual",
        stationary_timeout_s: float = 30.0,
        keep_min_s: float = 5.0,
        max_data_mb: float = 0.0,
    ):
        self.db = db
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.idle_timeout_s = idle_timeout_s
        self.raw_format = raw_format if raw_format in ("csv", "parquet") else "csv"
        self.record_mode = record_mode if record_mode in self.MODES else "manual"
        self.stationary_timeout_s = stationary_timeout_s
        self.keep_min_s = keep_min_s
        self.max_data_mb = max_data_mb
        # Called after a session is finalised: (session_id, raw_path, fmt).
        self.on_closed = None
        self._lock = threading.RLock()
        self._active: Optional[_ActiveSession] = None
        self._last_feed_mono: float = 0.0
        self._last_race_on: int = 0
        self._manual_requested: bool = False
        self._last_moving_mono: float = 0.0
        self._zero_hold: int = 0
        self._last_dist: Optional[float] = None
        self._pending_close_at: Optional[float] = None
        # Set after a manual stop so the very next frames don't instantly
        # auto-start a new session; re-armed at a natural boundary (race off
        # or the car coming to rest).
        self._auto_blocked: bool = False

    # -- Introspection -----------------------------------------------------
    @property
    def active_session_id(self) -> Optional[int]:
        with self._lock:
            return self._active.session_id if self._active else None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            base = {"record_mode": self.record_mode,
                    "stationary_timeout_s": self.stationary_timeout_s}
            if not self._active:
                return {"recording": False, "session_id": None,
                        "frame_count": 0, **base}
            return {
                "recording": True,
                "session_id": self._active.session_id,
                "frame_count": self._active.frame_count,
                "manual": self._active.started_manually,
                **base,
            }

    # -- Feed --------------------------------------------------------------
    def feed(self, frame: TelemetryFrame, t_mono: float) -> None:
        """Ingest one valid frame. Handles auto start/stop and raw capture."""
        with self._lock:
            self._last_feed_mono = t_mono
            race_on = int(frame.IsRaceOn)
            moving = frame.Speed >= self.STATIONARY_MS
            if moving:
                self._last_moving_mono = t_mono

            # Event-staging signals: distance pinned to ~0 while stopped
            # (route time attacks) or negative while stopped (grid starts).
            # The speed gate matters: merely driving PAST an event entry
            # point makes the game preview that event's route and push
            # DistanceTraveled negative — at road speed. A genuinely staged
            # car is stationary; every validated capture agrees.
            dist = float(frame.DistanceTraveled)
            if abs(dist) < 0.5 and frame.Speed < 1.0:
                self._zero_hold += 1
            else:
                self._zero_hold = 0
            staging = (
                (dist < -5.0 and frame.Speed < 3.0)
                or self._zero_hold >= self.ZERO_HOLD_FRAMES
            )
            in_event = race_on == 1 and (
                frame.CurrentLap > 0 or frame.LapNumber > 0 or staging
            )

            # Re-arm auto recording at a natural boundary after a manual stop.
            if self._auto_blocked and (
                race_on == 0
                or (t_mono - self._last_moving_mono) > self.stationary_timeout_s
            ):
                self._auto_blocked = False

            if self._active is None:
                if self._manual_requested:
                    self._open_session(frame, t_mono, manual=True)
                    self._manual_requested = False
                elif not self._auto_blocked and race_on == 1:
                    start = False
                    if self.record_mode == "motion":
                        start = frame.Speed >= self.MOVING_START_MS
                    elif self.record_mode == "event":
                        # Arm while staged so the launch is captured from
                        # frame one; also catch joining an event mid-lap.
                        start = in_event
                    if start:
                        self._open_session(frame, t_mono, manual=False)
                        self._pending_close_at = None
            self._last_race_on = race_on

            if self._active is not None:
                self._append(frame, t_mono)

                # Event end: a snap distance reset means the event finished —
                # unless staging reappears within the grace window (restart).
                if (
                    self.record_mode == "event"
                    and not self._active.started_manually
                ):
                    if (
                        self._last_dist is not None
                        and self._last_dist - dist > self.EVENT_SNAP_DROP_M
                    ):
                        self._pending_close_at = t_mono + self.EVENT_SNAP_GRACE_S
                    if self._pending_close_at is not None and staging:
                        self._pending_close_at = None  # restart, keep rolling
                    if (
                        self._pending_close_at is not None
                        and t_mono >= self._pending_close_at
                    ):
                        log.info(
                            "event ended, closing session",
                            extra={"extra": {"session_id": self._active.session_id}},
                        )
                        self._pending_close_at = None
                        self._close_session()

                # Stationary timeout applies to every session, manual
                # included — Stop remains instant, this is the walk-away net.
                # Being staged at a start line (zero-hold or negative
                # distance) never counts as stationary.
                if (
                    self._active is not None
                    and not moving
                    and not staging
                    and (t_mono - self._last_moving_mono) > self.stationary_timeout_s
                ):
                    log.info(
                        "vehicle stationary, closing session",
                        extra={"extra": {"session_id": self._active.session_id,
                                         "after_s": self.stationary_timeout_s}},
                    )
                    self._close_session()

            self._last_dist = dist

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
            # Don't let the very next frames auto-start a fresh session.
            self._auto_blocked = True
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
        # Display name uses the machine's LOCAL clock (what the player's
        # phone shows); created_at stays UTC ISO for machine handling.
        session_id = self.db.create_session(
            name=f"Session {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}",
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
        # The stationary clock starts at open — a session that begins while
        # staged at a start line must not instantly time out.
        self._last_moving_mono = t_mono
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

        # Roll up metadata — but never from zeroed menu/loading frames
        # (they would blank the car identity if the session ends in a menu).
        if int(frame.CarOrdinal) != 0:
            active.car_ordinal = int(frame.CarOrdinal)
            active.car_class = int(frame.CarClass)
            active.car_pi = int(frame.CarPerformanceIndex)
            active.drivetrain = int(frame.DrivetrainType)
            active.cylinders = int(frame.NumCylinders)
            active.car_group = int(frame.CarGroup)
        best = sane_lap(frame.BestLap)
        if best > 0:
            if active.best_lap is None or best < active.best_lap:
                active.best_lap = float(best)

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

        # Housekeeping: auto-recorded blips (a few seconds of rolling out of
        # a menu, a stationary timeout right after moving off) are junk —
        # discard them instead of accumulating. Manual recordings are the
        # user's explicit intent and are always kept.
        min_frames = int(self.keep_min_s * 60)
        if not active.started_manually and active.frame_count < min_frames:
            self.db.delete_session(active.session_id)
            try:
                active.raw_path.unlink(missing_ok=True)
            except OSError:
                pass
            log.info(
                "discarded blip session",
                extra={"extra": {"session_id": active.session_id,
                                 "frames": active.frame_count,
                                 "keep_min_s": self.keep_min_s}},
            )
            return

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
        self._enforce_data_cap(exclude_id=active.session_id)
        if self.on_closed is not None:
            try:
                self.on_closed(active.session_id, active.raw_path, active.raw_format)
            except Exception:  # pragma: no cover - hook must never break close
                log.exception("on_closed hook failed")

    def _enforce_data_cap(self, exclude_id: int) -> None:
        """Optional retention cap (FH6_MAX_DATA_MB, off by default).

        When enabled and the sessions folder exceeds the cap, prune oldest
        first — but only sessions that are clearly disposable: auto-recorded,
        never renamed, and without notes. Renamed or annotated sessions are
        treated as deliberately kept and are never touched.
        """
        if self.max_data_mb <= 0:
            return
        cap_bytes = self.max_data_mb * 1_000_000
        rows = self.db.list_sessions()  # newest first
        sized = []
        total = 0
        for row in rows:
            p = self.sessions_dir / (row.get("raw_path") or "")
            size = p.stat().st_size if p.exists() else 0
            total += size
            sized.append((row, p, size))
        if total <= cap_bytes:
            return
        for row, p, size in reversed(sized):  # oldest first
            if total <= cap_bytes:
                break
            protected = (
                row["id"] == exclude_id
                or row.get("started_manually")
                or (row.get("notes") or "").strip()
                or not str(row.get("name", "")).startswith("Session ")
            )
            if protected:
                continue
            self.db.delete_session(row["id"])
            try:
                p.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            log.info(
                "retention cap: pruned oldest session",
                extra={"extra": {"session_id": row["id"],
                                 "freed_mb": round(size / 1e6, 1),
                                 "cap_mb": self.max_data_mb}},
            )
