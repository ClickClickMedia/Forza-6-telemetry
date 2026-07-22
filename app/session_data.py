"""Load a recorded session's raw frames into column arrays.

Supports both CSV and Parquet raw files and returns a simple column-oriented
container backed by numpy arrays, which both the analysis and comparison code
consume. Numpy is the only heavy dependency and it is imported lazily so that
unit tests for pure parsing don't require it.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List

import numpy as np

from .packet import FIELD_NAMES

RAW_COLUMNS: List[str] = ["t_mono", "t_wall"] + FIELD_NAMES

# Columns that are textual (not converted to float).
_TEXT_COLUMNS = {"t_wall"}


class SessionData:
    """Column-oriented view of a recorded session."""

    def __init__(self, columns: Dict[str, np.ndarray], n: int):
        self.columns = columns
        self.n = n

    def __contains__(self, key: str) -> bool:
        return key in self.columns

    def col(self, name: str) -> np.ndarray:
        return self.columns[name]

    @property
    def t(self) -> np.ndarray:
        return self.columns["t_mono"]

    def dt(self) -> np.ndarray:
        """Per-frame time deltas (seconds), same length as data.

        Invariant: ``sum(dt) == t_mono[-1] - t_mono[0]`` (the true elapsed
        span). Frames whose timestamp does not advance contribute ZERO
        elapsed time. On Windows the monotonic clock resolution (~16 ms ≈
        one 60 Hz frame) stamps several frames at the same instant, so a
        real capture has ~6% duplicate timestamps; the previous version
        replaced those zero-deltas with the median frame length, which
        added phantom time and inflated EVERY time total (duration,
        distance, slide/wheelspin/braking seconds) by that ~6%. The
        duplicate-instant frames simply get no weight in weighted averages,
        which is correct — their time is carried by the following larger
        gap. A capture with no time advance at all falls back to uniform
        weights so weighted averages never divide by zero.
        """
        t = self.columns["t_mono"]
        n = len(t)
        if n < 2:
            return np.ones(n)
        d = np.diff(t, prepend=t[0])
        d[d < 0] = 0.0   # clock glitch: never negative elapsed time
        d[0] = 0.0       # first frame: no prior frame, no elapsed time
        if float(d.sum()) <= 0.0:  # degenerate (no timestamp advance)
            return np.full(n, 1.0 / n)
        return d


def load_session(raw_path: Path, raw_format: str = "csv") -> SessionData:
    if raw_format == "parquet":
        return _load_parquet(raw_path)
    return _load_csv(raw_path)


def _load_csv(path: Path) -> SessionData:
    cols: Dict[str, List] = {c: [] for c in RAW_COLUMNS}
    with open(path, "r", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return SessionData({c: np.array([]) for c in RAW_COLUMNS}, 0)
        idx = {name: i for i, name in enumerate(header)}
        for row in reader:
            if not row:
                continue
            for c in RAW_COLUMNS:
                i = idx.get(c)
                val = row[i] if i is not None and i < len(row) else ""
                cols[c].append(val)
    return _finalize(cols)


def _load_parquet(path: Path) -> SessionData:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    data = table.to_pydict()
    cols: Dict[str, List] = {c: list(data.get(c, [])) for c in RAW_COLUMNS}
    return _finalize(cols)


def _finalize(cols: Dict[str, List]) -> SessionData:
    n = len(cols["t_mono"])
    out: Dict[str, np.ndarray] = {}
    for c, values in cols.items():
        if c in _TEXT_COLUMNS:
            out[c] = np.array(values, dtype=object)
        else:
            out[c] = np.array(_to_float(values), dtype=float)
    return SessionData(out, n)


def _to_float(values: List) -> List[float]:
    result = []
    for v in values:
        try:
            result.append(float(v))
        except (TypeError, ValueError):
            result.append(0.0)
    return result
