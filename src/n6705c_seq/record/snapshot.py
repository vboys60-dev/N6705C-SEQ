"""Bounded, in-memory min/max envelopes for live recording snapshots."""

from collections.abc import Iterable
import math
from threading import Lock

import numpy as np

from .contracts import SNAPSHOT_MAX_POINTS, Snapshot, SnapshotResult


class Envelope:
    """Keep adjacent power-of-two buckets, including a partial final bucket.

    Set preview before publishing a MEAS envelope to callers. Later changes also
    invalidate nonempty snapshots. NaNs are ignored alongside numeric samples;
    all-NaN extrema and infinities are serialized as None only in snapshot().
    """

    def __init__(self, keys: Iterable[str], period: float, *, run_id: str = "") -> None:
        selected = tuple(keys)
        allowed = {f"{kind}{channel}" for channel in range(1, 5) for kind in ("V", "I")}
        if not selected or any(key not in allowed for key in selected) or len(set(selected)) != len(selected):
            raise ValueError("keys must be distinct V1/I1 through V4/I4 series")
        if isinstance(period, bool) or not math.isfinite(period) or period <= 0:
            raise ValueError("period must be finite and positive")
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string")
        self._keys = selected
        self._period = float(period)
        self._run_id = run_id
        self._lock = Lock()
        self._preview = False
        self._seq = 0
        self._records = 0
        self._width = 1
        self._min = np.empty((0, len(selected)), dtype=np.float32)
        self._max = self._min.copy()

    @property
    def preview(self) -> bool:
        with self._lock:
            return self._preview

    @preview.setter
    def preview(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise TypeError("preview must be a bool")
        with self._lock:
            if value != self._preview:
                self._preview = value
                if self._records:
                    self._seq += 1

    def append(self, block: np.ndarray) -> None:
        """Append rows in key order; a nonempty block advances seq once.

        Consume at most one bucket beyond the limit before merging. Persistent
        storage and reduction scratch space stay bounded even for a large block.
        Caller-owned input is never modified or retained.
        """
        rows = np.asarray(block, dtype=np.float32)
        if rows.ndim != 2 or rows.shape[1] != len(self._keys):
            raise ValueError("block must have shape (n, len(keys))")
        if not len(rows):
            return
        with self._lock:
            offset = 0
            while offset < len(rows):
                partial = self._records % self._width
                if partial:
                    count = min(self._width - partial, len(rows) - offset)
                    tail = rows[offset:offset + count]
                    self._min[-1] = np.fmin(self._min[-1], np.fmin.reduce(tail, axis=0))
                    self._max[-1] = np.fmax(self._max[-1], np.fmax.reduce(tail, axis=0))
                else:
                    capacity = SNAPSHOT_MAX_POINTS + 1 - len(self._min)
                    count = min(capacity * self._width, len(rows) - offset)
                    chunk = rows[offset:offset + count]
                    starts = np.arange(0, count, self._width)
                    self._min = np.concatenate((self._min, np.fmin.reduceat(chunk, starts, axis=0)))
                    self._max = np.concatenate((self._max, np.fmax.reduceat(chunk, starts, axis=0)))
                offset += count
                self._records += count
                if len(self._min) > SNAPSHOT_MAX_POINTS:
                    pairs = len(self._min) // 2
                    low = self._min[::2].copy()
                    high = self._max[::2].copy()
                    low[:pairs] = np.fmin(low[:pairs], self._min[1::2])
                    high[:pairs] = np.fmax(high[:pairs], self._max[1::2])
                    self._min, self._max = low, high
                    self._width *= 2
            self._seq += 1

    def snapshot(self, since_seq: int) -> SnapshotResult:
        """Return a detached JSON-safe copy, or the exact unchanged response."""
        with self._lock:
            if since_seq == self._seq:
                return {"unchanged": True, "seq": self._seq}
            result: Snapshot = {
                "run_id": self._run_id,
                "seq": self._seq,
                "preview": self._preview,
                "period_s": self._period,
                "keys": {},
            }
            for column, key in enumerate(self._keys):
                result["keys"][key] = {
                    "t0": 0.0,
                    "dt": self._width * self._period,
                    "min": [float(value) if math.isfinite(value) else None for value in self._min[:, column]],
                    "max": [float(value) if math.isfinite(value) else None for value in self._max[:, column]],
                }
            return result
