"""C2 bounded snapshots, bucket alignment, ownership and concurrent access."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import numpy as np
import pytest

from n6705c_seq.record.contracts import SNAPSHOT_MAX_POINTS, Snapshot
from n6705c_seq.record.snapshot import Envelope


KEYS = ("V1", "I1", "V2", "I2", "V3", "I3", "V4", "I4")


def _assert_buckets(snapshot, rows, period):
    assert set(snapshot) == Snapshot.__required_keys__
    assert snapshot["period_s"] == period
    for column, series in enumerate(snapshot["keys"].values()):
        width = round(series["dt"] / period)
        assert width >= 1 and width & (width - 1) == 0
        assert series["t0"] == 0.0
        assert len(series["min"]) == len(series["max"]) <= SNAPSHOT_MAX_POINTS
        starts = np.arange(0, len(rows), width)
        np.testing.assert_array_equal(series["min"], np.minimum.reduceat(rows[:, column], starts))
        np.testing.assert_array_equal(series["max"], np.maximum.reduceat(rows[:, column], starts))
        assert min(series["min"]) == float(rows[:, column].min())
        assert max(series["max"]) == float(rows[:, column].max())


def test_initial_snapshot_and_unchanged_sequence():
    envelope = Envelope(["V1", "I4"], 0.001)
    initial = envelope.snapshot(-1)
    assert initial == {
        "run_id": "", "seq": 0, "preview": False, "period_s": 0.001,
        "keys": {key: {"t0": 0.0, "dt": 0.001, "min": [], "max": []} for key in ("V1", "I4")},
    }
    assert envelope.snapshot(0) == {"unchanged": True, "seq": 0}
    envelope.append(np.empty((0, 2), dtype=np.float32))
    assert envelope.snapshot(0) == {"unchanged": True, "seq": 0}
    envelope.append(np.array([[1, -2]], dtype=np.float32))
    full = envelope.snapshot(0)
    assert full["seq"] == 1
    assert envelope.snapshot(1) == {"unchanged": True, "seq": 1}
    assert envelope.snapshot(99) == full
    assert envelope.snapshot(-1) == full


@pytest.mark.parametrize("block_size", [1_000_000, 137])
def test_million_rows_keep_each_series_bounded_and_preserve_every_bucket(block_size):
    rows = np.random.default_rng(42).standard_normal((1_000_000, len(KEYS))).astype(np.float32)
    envelope = Envelope(KEYS, 0.00098303996, run_id="million")
    for start in range(0, len(rows), block_size):
        envelope.append(rows[start:start + block_size])
    snapshot = envelope.snapshot(-1)
    assert snapshot["run_id"] == "million"
    assert snapshot["seq"] == (len(rows) + block_size - 1) // block_size
    _assert_buckets(snapshot, rows, 0.00098303996)


def test_partial_buckets_survive_odd_merges_and_later_appends():
    rows = np.arange(16_003 * 2, dtype=np.float32).reshape(-1, 2)
    rows[2000] = [-1e6, 1e6]
    rows[8000] = [1e7, -1e7]
    envelope = Envelope(["V2", "I2"], 0.25)
    offset = 0
    for end in (2000, 2001, 2002, 4001, 4002, 8001, 8002, len(rows)):
        envelope.append(rows[offset:end])
        _assert_buckets(envelope.snapshot(-1), rows[:end], 0.25)
        offset = end


def test_snapshots_and_input_are_detached():
    keys = ["V1"]
    envelope = Envelope(keys, 0.01)
    keys[0] = "I1"
    rows = np.array([[1], [2]], dtype=np.float32)
    original = rows.copy()
    envelope.append(rows)
    np.testing.assert_array_equal(rows, original)
    rows[:] = 999
    first = envelope.snapshot(-1)
    first["keys"]["V1"]["min"][0] = -999
    first["keys"]["V1"]["max"].clear()
    first["keys"]["extra"] = {}
    first["run_id"] = "other"
    assert envelope.snapshot(-1)["keys"]["V1"]["min"] == [1.0, 2.0]
    saved = envelope.snapshot(-1)
    envelope.append(np.array([[3]], dtype=np.float32))
    assert saved["keys"]["V1"]["max"] == [1.0, 2.0]


def test_preview_and_run_id_are_carried_in_snapshots_and_new_runs_reset_seq():
    envelope = Envelope(["V1"], 0.025, run_id="preview-run")
    envelope.preview = True
    assert envelope.preview is True
    empty = envelope.snapshot(-1)
    assert empty["seq"] == 0 and empty["preview"] is True
    envelope.append(np.ones((5, 1), dtype=np.float32))
    snapshot = envelope.snapshot(-1)
    assert snapshot["run_id"] == "preview-run"
    assert snapshot["preview"] is True and snapshot["period_s"] == 0.025
    assert envelope.snapshot(snapshot["seq"]) == {"unchanged": True, "seq": snapshot["seq"]}
    envelope.preview = False
    assert envelope.snapshot(snapshot["seq"])["preview"] is False
    assert Envelope(["V1"], 0.001, run_id="next-run").snapshot(-1)["seq"] == 0


def test_nonfinite_extrema_are_json_null_without_changing_input():
    rows = np.array([[np.nan, np.inf], [-np.inf, 1], [2, -3]], dtype=np.float32)
    original = rows.copy()
    envelope = Envelope(["V1", "I1"], 0.1)
    envelope.append(rows)
    snapshot = envelope.snapshot(-1)
    assert snapshot["keys"]["V1"]["min"] == [None, None, 2.0]
    assert snapshot["keys"]["I1"]["max"] == [None, 1.0, -3.0]
    assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot
    np.testing.assert_array_equal(rows, original)


def test_nan_alongside_finite_samples_keeps_finite_bucket_extrema():
    rows = np.full((2001, 1), np.nan, dtype=np.float32)
    rows[1, 0] = -3
    envelope = Envelope(["V1"], 0.1)
    envelope.append(rows)
    envelope.append(np.array([[5]], dtype=np.float32))
    series = envelope.snapshot(-1)["keys"]["V1"]
    assert series["min"][0] == series["max"][0] == -3
    assert series["min"][1] is None and series["max"][1] is None
    assert series["min"][-1] == series["max"][-1] == 5


def test_twenty_threads_append_and_snapshot_atomically():
    envelope = Envelope(["V1", "I1"], 0.001, run_id="concurrent")
    barrier = Barrier(20)

    def work(worker):
        barrier.wait(timeout=10)
        last_seq = -1
        for index in range(50):
            values = np.arange(5, dtype=np.float32) + 5 * (worker * 50 + index)
            envelope.append(np.column_stack((values, -values)))
            snapshot = envelope.snapshot(last_seq)
            assert snapshot["seq"] >= last_seq
            last_seq = snapshot["seq"]
            assert snapshot["run_id"] == "concurrent"
            series = snapshot["keys"]["V1"]
            width = round(series["dt"] / snapshot["period_s"])
            assert len(series["min"]) == (last_seq * 5 + width - 1) // width
            assert len(series["min"]) <= SNAPSHOT_MAX_POINTS
            np.testing.assert_array_equal(snapshot["keys"]["V1"]["min"],
                                          -np.array(snapshot["keys"]["I1"]["max"]))
            snapshot["keys"]["V1"]["min"].clear()
        return last_seq

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(work, range(20)))
    snapshot = envelope.snapshot(-1)
    assert snapshot["seq"] == 1000
    assert min(snapshot["keys"]["V1"]["min"]) == 0
    assert max(snapshot["keys"]["V1"]["max"]) == 4999
    assert envelope.snapshot(1000) == {"unchanged": True, "seq": 1000}


@pytest.mark.parametrize("keys", [[], ["P1"], ["V0"], ["I5"], ["V1", "V1"]])
def test_invalid_keys_are_rejected(keys):
    with pytest.raises(ValueError):
        Envelope(keys, 0.001)


@pytest.mark.parametrize("period", [0, -1, np.nan, np.inf, True])
def test_invalid_period_is_rejected(period):
    with pytest.raises(ValueError):
        Envelope(["V1"], period)


def test_invalid_shape_does_not_change_envelope():
    envelope = Envelope(["V1"], 0.001)
    for rows in (np.ones(3), np.ones((3, 2)), np.ones((3, 1, 1))):
        with pytest.raises(ValueError):
            envelope.append(rows)
    assert envelope.snapshot(0) == {"unchanged": True, "seq": 0}
