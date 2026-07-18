"""SQLite persistence for session metadata and markers.

Raw telemetry frames are NOT stored here (they live in per-session CSV/Parquet
files); SQLite only holds session metadata and manual markers, which keeps the
database small and the raw data trivially exportable.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,      -- ISO8601 UTC
    ended_at          TEXT,
    started_manually  INTEGER NOT NULL DEFAULT 0,
    raw_path          TEXT,                  -- relative path to raw frames file
    raw_format        TEXT    NOT NULL DEFAULT 'csv',
    frame_count       INTEGER NOT NULL DEFAULT 0,
    car_ordinal       INTEGER,
    car_class         INTEGER,
    car_pi            INTEGER,
    drivetrain        INTEGER,
    cylinders         INTEGER,
    car_group         INTEGER,
    best_lap          REAL,
    notes             TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS markers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    t_mono      REAL    NOT NULL,            -- monotonic receive time within session
    label       TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_markers_session ON markers(session_id);

-- User-maintained car names: Forza broadcasts only a numeric ordinal, so
-- the player names each car once and it applies everywhere thereafter.
CREATE TABLE IF NOT EXISTS cars (
    ordinal INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
);

-- App settings changed from the UI (override env defaults across restarts).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Saved tune setups, versioned per car ordinal. `data` is a JSON object of
-- tuning-screen values (user-entered; telemetry cannot read the garage).
CREATE TABLE IF NOT EXISTS setups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    car_ordinal INTEGER NOT NULL,
    label       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    data        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_setups_ordinal ON setups(car_ordinal);
"""


class Database:
    """Thin threadsafe wrapper around a single SQLite connection.

    A module-level lock serialises writes because the connection is shared
    across the asyncio loop and (potentially) worker threads used for blocking
    file IO. SQLite handles the actual durability.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- Sessions ----------------------------------------------------------
    def create_session(
        self,
        name: str,
        created_at: str,
        started_manually: bool,
        raw_path: str,
        raw_format: str,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO sessions
                   (name, created_at, started_manually, raw_path, raw_format)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, created_at, int(started_manually), raw_path, raw_format),
            )
            return int(cur.lastrowid)

    def finalize_session(
        self,
        session_id: int,
        ended_at: str,
        frame_count: int,
        meta: Dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE sessions SET
                     ended_at = ?, frame_count = ?, car_ordinal = ?, car_class = ?,
                     car_pi = ?, drivetrain = ?, cylinders = ?, car_group = ?, best_lap = ?
                   WHERE id = ?""",
                (
                    ended_at,
                    frame_count,
                    meta.get("car_ordinal"),
                    meta.get("car_class"),
                    meta.get("car_pi"),
                    meta.get("drivetrain"),
                    meta.get("cylinders"),
                    meta.get("car_group"),
                    meta.get("best_lap"),
                    session_id,
                ),
            )

    def set_raw_path(self, session_id: int, raw_path: str, raw_format: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET raw_path = ?, raw_format = ? WHERE id = ?",
                (raw_path, raw_format, session_id),
            )

    def rename_session(self, session_id: int, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET name = ? WHERE id = ?", (name, session_id)
            )
            return cur.rowcount > 0

    def set_notes(self, session_id: int, notes: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET notes = ? WHERE id = ?", (notes, session_id)
            )
            return cur.rowcount > 0

    def update_frame_count(self, session_id: int, frame_count: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET frame_count = ? WHERE id = ?",
                (frame_count, session_id),
            )

    def get_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: int) -> Optional[str]:
        """Delete a session row; return its raw_path so the caller can unlink."""
        with self._lock:
            row = self._conn.execute(
                "SELECT raw_path FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return row["raw_path"]

    # -- Car names ---------------------------------------------------------
    def set_car_name(self, ordinal: int, name: str) -> None:
        with self._lock:
            if name.strip():
                self._conn.execute(
                    """INSERT INTO cars (ordinal, name) VALUES (?, ?)
                       ON CONFLICT(ordinal) DO UPDATE SET name = excluded.name""",
                    (ordinal, name.strip()),
                )
            else:
                self._conn.execute("DELETE FROM cars WHERE ordinal = ?", (ordinal,))

    def get_car_name(self, ordinal: Optional[int]) -> Optional[str]:
        if ordinal is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM cars WHERE ordinal = ?", (ordinal,)
            ).fetchone()
        return row["name"] if row else None

    def car_names(self) -> Dict[int, str]:
        with self._lock:
            rows = self._conn.execute("SELECT ordinal, name FROM cars").fetchall()
        return {int(r["ordinal"]): r["name"] for r in rows}

    # -- Settings ----------------------------------------------------------
    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    def update_best_lap(self, session_id: int, best_lap: Optional[float]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET best_lap = ? WHERE id = ?",
                (best_lap, session_id),
            )

    # -- Setups ------------------------------------------------------------
    def add_setup(self, car_ordinal: int, label: str, created_at: str,
                  data_json: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO setups (car_ordinal, label, created_at, data)
                   VALUES (?, ?, ?, ?)""",
                (car_ordinal, label, created_at, data_json),
            )
            return int(cur.lastrowid)

    def list_setups(self, car_ordinal: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, car_ordinal, label, created_at, data
                   FROM setups WHERE car_ordinal = ? ORDER BY id DESC""",
                (car_ordinal,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_setup(self, setup_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, car_ordinal, label, created_at, data FROM setups WHERE id = ?",
                (setup_id,),
            ).fetchone()
        return dict(row) if row else None

    def count_setups(self, car_ordinal: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM setups WHERE car_ordinal = ?",
                (car_ordinal,),
            ).fetchone()
        return int(row["n"])

    # -- Markers -----------------------------------------------------------
    def add_marker(self, session_id: int, t_mono: float, label: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO markers (session_id, t_mono, label) VALUES (?, ?, ?)",
                (session_id, t_mono, label),
            )
            return int(cur.lastrowid)

    def list_markers(self, session_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, t_mono, label FROM markers WHERE session_id = ? ORDER BY t_mono",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]
