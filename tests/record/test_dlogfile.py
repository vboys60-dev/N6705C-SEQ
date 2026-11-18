"""C6 writer/recovery format and publication guarantees, without hardware."""

import ast
import json
from pathlib import Path
import stat
import struct
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from n6705c_seq.analysis.core import parse_dlog
from n6705c_seq.record import dlogfile
from n6705c_seq.record.dlogfile import DlogWriter, elog_block, recover_part


def _paths(final):
    return Path(str(final) + ".part"), Path(str(final) + ".json"), Path(str(final) + ".json.part")


def _writer(tmp_path, **kwargs):
    options = dict(channels=[1, 3], sense_volt=True, sense_curr=True,
                   tint=0.00098303996, models={1: "N6762A", 3: "N6781A"}, sidecar={})
    options.update(kwargs)
    return DlogWriter(tmp_path / "record.dlog", **options)


def _metadata(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _change_metadata(path, change):
    meta = _metadata(path)
    change(meta)
    path.write_text(json.dumps(meta), encoding="utf-8")


def _snapshot(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def _permission_error(winerror):
    error = PermissionError("injected Windows lock")
    error.winerror = winerror
    return error


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_replace_retries_two_windows_locks_then_succeeds(monkeypatch, winerror):
    calls, sleeps = [], []

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) <= 2:
            raise _permission_error(winerror)

    monkeypatch.setattr(dlogfile, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(dlogfile.time, "sleep", sleeps.append)
    dlogfile.replace_with_retry("source", "destination")
    assert calls == [("source", "destination")] * 3
    assert sleeps == [0.05, 0.1]


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_replace_retry_exhaustion_preserves_last_error(monkeypatch, winerror):
    errors, sleeps = [], []

    def replace(src, dst):
        errors.append(_permission_error(winerror))
        raise errors[-1]

    monkeypatch.setattr(dlogfile, "os", SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(dlogfile.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as caught:
        dlogfile.replace_with_retry("source", "destination")
    assert len(errors) == 5
    assert caught.value is errors[-1]
    assert caught.value.winerror == winerror
    assert sleeps == [0.05, 0.1, 0.2, 0.4]


@pytest.mark.parametrize("platform,error", [
    ("nt", _permission_error(2)),
    ("nt", PermissionError("no winerror")),
    ("nt", FileNotFoundError("missing source")),
    ("nt", OSError("disk failure")),
    ("posix", _permission_error(32)),
])
def test_replace_does_not_retry_other_failures(monkeypatch, platform, error):
    calls, sleeps = [], []

    def replace(src, dst):
        calls.append((src, dst))
        raise error

    monkeypatch.setattr(dlogfile, "os", SimpleNamespace(name=platform, replace=replace))
    monkeypatch.setattr(dlogfile.time, "sleep", sleeps.append)
    with pytest.raises(type(error)) as caught:
        dlogfile.replace_with_retry("source", "destination")
    assert caught.value is error
    assert calls == [("source", "destination")]
    assert sleeps == []


def test_replace_non_windows_replaces_existing_file(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    monkeypatch.setattr(dlogfile, "os", SimpleNamespace(**dict(vars(dlogfile.os), name="posix")))
    sleeps = []
    monkeypatch.setattr(dlogfile.time, "sleep", sleeps.append)
    dlogfile.replace_with_retry(source, destination)
    assert destination.read_bytes() == b"new"
    assert not source.exists()
    assert sleeps == []


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("stage", ["sidecar_temp", "incomplete_sidecar", "data", "complete_sidecar"])
def test_publication_retries_windows_locks_to_completion(tmp_path, monkeypatch, recover, stage):
    writer = _writer(tmp_path)
    writer.append(np.ones((2, 4), dtype=np.float32))
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    if recover:
        writer.abort(keep_part=True)
    real_replace = dlogfile.os.replace
    attempts, sleeps = [], []

    def replace(src, dst):
        target = {
            "sidecar_temp": Path(dst) == side_part,
            "incomplete_sidecar": Path(src) == side_part and Path(dst) == side,
            "data": Path(dst) == final,
            "complete_sidecar": Path(dst) == side and _metadata(Path(src))["complete"],
        }[stage]
        if target:
            attempts.append((src, dst))
            if len(attempts) <= 2:
                raise _permission_error(32)
        if Path(dst) == final:
            assert _metadata(side)["complete"] is False
        return real_replace(src, dst)

    monkeypatch.setattr(dlogfile, "os", SimpleNamespace(**dict(vars(dlogfile.os), name="nt", replace=replace)))
    monkeypatch.setattr(dlogfile.time, "sleep", sleeps.append)
    assert (recover_part(part) if recover else writer.finalize()) == final
    assert len(attempts) == 3
    assert sleeps == [0.05, 0.1]
    assert _metadata(side)["complete"] is True
    assert parse_dlog(final.read_bytes())["nRecords"] == 2
    assert set(_snapshot(tmp_path)) == {final.name, side.name}


def test_writer_imports_only_stdlib_and_numpy():
    tree = ast.parse(Path(dlogfile.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names | {"numpy"} for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module.split(".")[0] in sys.stdlib_module_names | {"numpy"}


@pytest.mark.parametrize("sense_volt,sense_curr", [(True, True), (True, False), (False, True)])
def test_roundtrip_raw_values_field_order_time_and_sidecar(tmp_path, sense_volt, sense_curr):
    sidecar = dict(run_id="run-123", backend="elog", preview=False,
                   started_utc="2026-11-17T01:02:03Z",
                   requested={"channels": [3, 1], "duration_s": 10},
                   actual={"ranges": {"1": {"V": 20}}}, idn="test instrument",
                   elapsed_s=1.5, gaps=[], notes=["configuration read back"])
    fields = [(ch, kind) for ch in (1, 3) for kind, enabled in (("V", sense_volt), ("I", sense_curr)) if enabled]
    rows = np.array([[0, -0., -5, -100], [np.nan, np.inf, -np.inf, -1e30],
                     [1e30, 2, -3, 4], [7, 8, 9, 10]], dtype=np.float32)[:, :len(fields)]
    writer = _writer(tmp_path, channels=[3, 1], sense_volt=sense_volt, sense_curr=sense_curr,
                     sidecar=sidecar, models={1: 'N<&>"\'model', 3: "N6781A"})
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    initial = _metadata(side_part)
    assert initial["complete"] is False and initial["records"] == 0
    assert initial["requested"] == sidecar["requested"]
    assert initial["actual"]["channels"] == [1, 3]
    assert initial["actual"]["ranges"] == {"1": {"V": 20}}
    assert initial["time_width"] == 24
    assert initial["ended_utc"] is None
    assert struct.unpack_from(">II", part.read_bytes(), initial["header_bytes"]) == (0, 0)
    writer.append(rows[:1])
    # The metadata dictionary remains live until publication; gaps never add rows.
    sidecar["gaps"].append(dict(kind="overflow", index=1, count=None))
    writer.append(rows[1:])
    assert writer.finalize() == final
    assert set(_snapshot(tmp_path)) == {final.name, side.name}
    assert not part.exists() and not side_part.exists()
    raw = final.read_bytes()
    meta = _metadata(side)
    assert raw[:meta["header_bytes"]].endswith(b"</dlog>\n")
    assert b"\r\n" not in raw[:meta["header_bytes"]]
    assert b'<ident>\n\t\t\t<model>N&lt;&amp;&gt;"\'model</model>' in raw
    assert b'<date>"2026-11-17T01:02:03Z"</date>' in raw
    assert b'<base_filename>"record.dlog"</base_filename>' in raw
    assert b"&quot;" not in raw and b"&apos;" not in raw
    assert b"Internal:" not in raw
    assert b"<sense_minmax>0</sense_minmax>" in raw
    assert b"<!-- record_run_id=run-123 -->" in raw
    assert struct.unpack_from(">II", raw, meta["header_bytes"]) == (0, len(rows))
    assert raw[meta["time_offset"]:meta["time_offset"] + 24] == f"{len(rows) * initial['actual']['period_s']:024.9f}".encode()
    assert raw[meta["header_bytes"] + 8:] == rows.astype(">f4").tobytes()
    parsed = parse_dlog(raw)
    assert parsed["fields"] == fields
    assert parsed["tint"] == initial["actual"]["period_s"]
    assert parsed["minmax"] is False
    for column, (ch, kind) in enumerate(fields):
        # Compare bits too: signed zero and all raw float32 values survive.
        np.testing.assert_array_equal(parsed[kind][ch].view(np.uint32), rows[:, column].view(np.uint32))
    assert meta["complete"] is True and meta["records"] == len(rows)
    assert meta["gaps"] == sidecar["gaps"]
    assert meta["elapsed_s"] == 1.5
    assert meta["ended_utc"].endswith("Z")


@pytest.mark.parametrize("fields", [
    ("I_avg", "I_min", "I_max", "V_avg", "V_min", "V_max"),
    ("V_avg",), ("I_avg",), ("I_avg", "V_avg"),
])
def test_elog_block_reorders_each_channel_and_only_averages(fields):
    data = {ch: (np.arange(3 * len(fields)).reshape(3, len(fields)) + ch * 100).astype(np.float32) for ch in (1, 4)}
    block = elog_block(data, fields, [4, 1])
    expected = np.column_stack([data[ch][:, fields.index(field)] for ch in (1, 4)
                                for field in ("V_avg", "I_avg") if field in fields])
    assert block.dtype == np.float32
    np.testing.assert_array_equal(block, expected)
    assert elog_block({ch: array[:0] for ch, array in data.items()}, fields, [4, 1]).shape == (0, expected.shape[1])


@pytest.mark.parametrize("data,fields,channels", [
    ({1: np.ones((2, 1))}, ("I_min",), [1]),
    ({1: np.ones((2, 1))}, ("V_avg",), [1, 2]),
    ({1: np.ones((2, 1)), 2: np.ones((3, 1))}, ("V_avg",), [1, 2]),
    ({1: np.ones((2, 2))}, ("V_avg",), [1]),
    ({1: np.ones((2, 2))}, ("V_avg", "V_avg"), [1]),
])
def test_elog_block_rejects_missing_or_unaligned_data(data, fields, channels):
    with pytest.raises(ValueError):
        elog_block(data, fields, channels)


def test_write_zero_part_parses_and_half_record_recovers(tmp_path):
    writer = _writer(tmp_path)
    rows = np.arange(28, dtype=np.float32).reshape(7, 4) - 10
    writer.append(rows)
    writer.abort()
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    meta = _metadata(side_part)
    assert struct.unpack_from(">II", part.read_bytes(), meta["header_bytes"]) == (0, 0)
    parsed = parse_dlog(part.read_bytes())
    np.testing.assert_array_equal(parsed["I"][1], rows[:, 1])
    with part.open("ab") as stream:
        stream.write(b"\x12\x34\x56\x78\x90")  # Less than one 16-byte row.
    assert recover_part(part) == final
    assert set(_snapshot(tmp_path)) == {final.name, side.name}
    parsed = parse_dlog(final.read_bytes())
    assert parsed["nRecords"] == 7
    assert parsed["declaredTime"] == float(f"{7 * meta['actual']['period_s']:.9f}")
    for column, (ch, kind) in enumerate(parsed["fields"]):
        np.testing.assert_array_equal(parsed[kind][ch], rows[:, column])
    recovered = _metadata(side)
    assert recovered["recovered"] is True and recovered["complete"] is True
    assert recovered["records"] == 7 and recovered["notes"]


def test_active_writer_is_excluded_from_recovery_and_other_writer(tmp_path):
    writer = _writer(tmp_path)
    try:
        writer.append(np.ones((1, 4), dtype=np.float32))
        before = _snapshot(tmp_path)
        result = []
        worker = threading.Thread(target=lambda: result.append(recover_part(tmp_path / "record.dlog.part")))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive() and result == [None]
        with pytest.raises(FileExistsError):
            _writer(tmp_path)
        assert _snapshot(tmp_path) == before
    finally:
        writer.abort()


def test_zero_record_cannot_finalize_or_recover(tmp_path):
    writer = _writer(tmp_path)
    writer.append(np.empty((0, 4), dtype=np.float32))
    with pytest.raises(ValueError, match="zero-record"):
        writer.finalize()
    writer.abort()
    before = _snapshot(tmp_path)
    assert recover_part(tmp_path / "record.dlog.part") is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("name", ["foreign.dlog.part", "ordinary.dlog", "foreign.part"])
def test_foreign_without_sidecar_and_ordinary_files_untouched(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"foreign file, including a partial row")
    before = _snapshot(tmp_path)
    assert recover_part(path) is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("change", [
    lambda m: m.update(producer="other.app"),
    lambda m: m.update(schema_version=2),
    lambda m: m.update(run_id="different-run"),
    lambda m: m.update(name="../external.dlog"),
    lambda m: m.update(header_bytes=m["header_bytes"] + 1),
    lambda m: m.update(record_bytes=4),
    lambda m: m.update(time_offset=m["time_offset"] + 1),
    lambda m: m.update(time_width=23),
    lambda m: m["actual"].update(channels=[3, 1]),
    lambda m: m["actual"].update(sense_curr=False),
    lambda m: m["actual"].update(period_s=0),
])
def test_recovery_rejects_mismatched_sidecar_unchanged(tmp_path, change):
    writer = _writer(tmp_path)
    writer.append(np.ones((2, 4), dtype=np.float32))
    writer.abort()
    part, _, side_part = _paths(tmp_path / "record.dlog")
    _change_metadata(side_part, change)
    before = _snapshot(tmp_path)
    assert recover_part(part) is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("damage", ["trigger", "minmax", "run_id", "time", "short", "json"])
def test_recovery_rejects_malformed_header_or_json_unchanged(tmp_path, damage):
    writer = _writer(tmp_path, sidecar={"run_id": "original-run"})
    writer.append(np.ones((2, 4), dtype=np.float32))
    writer.abort()
    part, _, side_part = _paths(tmp_path / "record.dlog")
    meta = _metadata(side_part)
    raw = bytearray(part.read_bytes())
    if damage == "trigger":
        struct.pack_into(">I", raw, meta["header_bytes"], 1)
    elif damage == "minmax":
        raw = raw.replace(b"<sense_minmax>0", b"<sense_minmax>1")
    elif damage == "run_id":
        raw = raw.replace(b"original-run", b"impostor-run")
    elif damage == "time":
        raw[meta["time_offset"]] = ord("x")
    elif damage == "short":
        raw = raw[:meta["header_bytes"] + 7]
    elif damage == "json":
        side_part.write_text("{", encoding="utf-8")
    part.write_bytes(raw)
    before = _snapshot(tmp_path)
    assert recover_part(part) is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("which", ["data", "side", "final", "final_side"])
def test_recovery_does_not_follow_symlinks(tmp_path, which):
    directory = tmp_path / "real"
    directory.mkdir()
    writer = _writer(directory)
    writer.append(np.ones((1, 4), dtype=np.float32))
    writer.abort()
    final = directory / "record.dlog"
    part, side, side_part = _paths(final)
    target = {"data": part, "side": side_part, "final": final, "final_side": side}[which]
    moved = target.with_name(target.name + ".original")
    if target.exists():
        target.rename(moved)
    else:
        moved.write_bytes(b"existing published file")
    target.symlink_to(moved)
    before = _snapshot(directory)
    assert recover_part(part) is None
    assert _snapshot(directory) == before


@pytest.mark.parametrize("recover", [False, True])
def test_directory_symlinks_allow_roundtrip_and_share_active_registry(tmp_path, recover):
    real = tmp_path / "real"
    (real / "nested").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    directory = alias / "nested"
    writer = _writer(directory)
    rows = np.arange(8, dtype=np.float32).reshape(2, 4)
    try:
        writer.append(rows)
        assert recover_part(real / "nested" / "record.dlog.part") is None
        with pytest.raises(FileExistsError):
            _writer(real / "nested")
    finally:
        if recover:
            writer.abort()
        else:
            writer.finalize()
    if recover:
        assert recover_part(directory / "record.dlog.part") == directory / "record.dlog"
    parsed = parse_dlog((directory / "record.dlog").read_bytes())
    for column, (ch, kind) in enumerate(parsed["fields"]):
        np.testing.assert_array_equal(parsed[kind][ch], rows[:, column])
    # Finalization/recovery must release the canonical registry key too.
    replacement = _writer(real / "nested")
    replacement.abort(keep_part=False)


@pytest.mark.parametrize("index", range(4))
def test_writer_rejects_symlink_leaf_files_unchanged(tmp_path, index):
    final = tmp_path / "record.dlog"
    target = (final, *_paths(final))[index]
    original = tmp_path / "original"
    original.write_bytes(b"keep existing file")
    target.symlink_to(original)
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="Unsafe DLOG path"):
        _writer(tmp_path)
    assert target.is_symlink()
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("records", [10, 26])
@pytest.mark.parametrize("recover", [False, True])
def test_binary_counts_roundtrip_and_recovery_open_flags(tmp_path, monkeypatch, records, recover):
    writer = _writer(tmp_path)
    rows = np.arange(records * 4, dtype=np.float32).reshape(records, 4) - 13.5
    writer.append(rows)
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    if recover:
        writer.abort()
    native_binary = getattr(dlogfile.os, "O_BINARY", 0)
    binary = native_binary or (1 << 29)
    real_open = dlogfile.os.open
    opened = {}

    def binary_open(path, flags, *args, **kwargs):
        if Path(path) in (part, side_part, tmp_path):
            assert flags & binary
            opened[Path(path)] = flags
        # The injected flag tests the call without passing a fake OS flag.
        return real_open(path, flags if native_binary else flags & ~binary, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(dlogfile.os, "O_BINARY", binary, raising=False)
        patch.setattr(dlogfile.os, "open", binary_open)
        assert (recover_part(part) if recover else writer.finalize()) == final
    if recover:
        assert {part, side_part} <= opened.keys()
        assert opened[part] & dlogfile.os.O_RDWR
    if dlogfile.os.name != "nt":
        assert tmp_path in opened
    raw = final.read_bytes()
    meta = _metadata(side)
    assert struct.unpack_from(">II", raw, meta["header_bytes"]) == (0, records)
    assert raw[meta["header_bytes"] + 8:] == rows.astype(">f4").tobytes()
    parsed = parse_dlog(raw)
    assert parsed["nRecords"] == records
    for column, (ch, kind) in enumerate(parsed["fields"]):
        np.testing.assert_array_equal(parsed[kind][ch], rows[:, column])


def test_abort_false_only_removes_own_parts_and_preserves_old_finals(tmp_path):
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    final.write_bytes(b"old data")
    side.write_text("old metadata")
    writer = _writer(tmp_path)
    writer.append(np.ones((1, 4), dtype=np.float32))
    writer.abort(keep_part=False)
    writer.abort(keep_part=False)
    assert final.read_bytes() == b"old data" and side.read_text() == "old metadata"
    assert not part.exists() and not side_part.exists()
    # A preexisting sidecar must survive constructor cleanup.
    side_part.write_text("belongs to someone else")
    with pytest.raises(FileExistsError):
        _writer(tmp_path)
    assert side_part.read_text() == "belongs to someone else" and not part.exists()
    part.write_bytes(b"another writer")
    with pytest.raises(FileExistsError):
        _writer(tmp_path)
    assert part.read_bytes() == b"another writer"


def test_finalize_closes_handle_fsyncs_and_publishes_sidecar_first(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    writer.append(np.ones((1, 4), dtype=np.float32))
    real_replace, real_fsync = dlogfile.os.replace, dlogfile.os.fsync
    events = []

    def replace(source, dest):
        if Path(dest).name in ("record.dlog.json", "record.dlog"):
            assert writer._stream.closed
            events.append(Path(dest).name)
        return real_replace(source, dest)

    def fsync(fd):
        events.append("directory_fsync" if stat.S_ISDIR(dlogfile.os.fstat(fd).st_mode) else "fsync")
        return real_fsync(fd)

    monkeypatch.setattr(dlogfile.os, "replace", replace)
    monkeypatch.setattr(dlogfile.os, "fsync", fsync)
    writer.finalize()
    expected = ["fsync", "fsync", "record.dlog.json", "record.dlog",
                "fsync", "record.dlog.json"]
    if dlogfile.os.name != "nt":
        expected.append("directory_fsync")
    assert events == expected
    with pytest.raises(RuntimeError):
        writer.append(np.ones((1, 4), dtype=np.float32))


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_publication_marks_complete_only_after_matching_data(tmp_path, monkeypatch, overwrite, recover):
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    if overwrite:
        previous = _writer(tmp_path, sidecar={"run_id": "previous-run"})
        previous.append(np.zeros((1, 4), dtype=np.float32))
        previous.finalize()
    writer = _writer(tmp_path, sidecar={"run_id": "new-run"})
    rows = np.arange(12, dtype=np.float32).reshape(3, 4)
    writer.append(rows)
    if recover:
        writer.abort()
    real_replace = dlogfile.os.replace
    events = []

    def replace(source, dest):
        if Path(dest) == side:
            meta = _metadata(Path(source))
            events.append(meta["complete"])
            assert meta["run_id"] == "new-run"
            if meta["complete"]:
                assert b"record_run_id=new-run" in final.read_bytes()
                assert meta["ended_utc"] is not None
            else:
                assert meta["ended_utc"] is None
        elif Path(dest) == final:
            assert _metadata(side)["complete"] is False
            events.append("data")
        return real_replace(source, dest)

    monkeypatch.setattr(dlogfile.os, "replace", replace)
    assert (recover_part(part) if recover else writer.finalize()) == final
    assert events == [False, "data", True]
    np.testing.assert_array_equal(parse_dlog(final.read_bytes())["V"][1], rows[:, 0])
    assert not part.exists() and not side_part.exists()


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("failure", ["data", "complete_sidecar"])
def test_sharing_violation_keeps_incomplete_sidecar_and_correct_data_location(
        tmp_path, monkeypatch, overwrite, recover, failure):
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    if overwrite:
        previous = _writer(tmp_path, sidecar={"run_id": "previous-run"})
        previous.append(np.zeros((1, 4), dtype=np.float32))
        previous.finalize()
    old_data = final.read_bytes() if overwrite else None
    writer = _writer(tmp_path, sidecar={"run_id": "new-run"})
    rows = np.arange(12, dtype=np.float32).reshape(3, 4)
    writer.append(rows)
    if recover:
        writer.abort()
    real_replace = dlogfile.os.replace

    def replace(source, dest):
        if ((failure == "data" and Path(dest) == final)
                or (failure == "complete_sidecar" and Path(dest) == side
                    and _metadata(Path(source))["complete"])):
            raise PermissionError("Windows sharing violation")
        return real_replace(source, dest)

    with monkeypatch.context() as patch:
        patch.setattr(dlogfile.os, "replace", replace)
        with pytest.raises(PermissionError, match="sharing violation") as error:
            recover_part(part) if recover else writer.finalize()
    assert not side_part.exists()
    meta = _metadata(side)
    assert meta["complete"] is False and meta["ended_utc"] is None
    assert meta["run_id"] == "new-run"
    if failure == "data":
        assert part.exists()
        assert (final.read_bytes() if final.exists() else None) == old_data
    else:
        assert not part.exists()
        assert b"record_run_id=new-run" in final.read_bytes()
        np.testing.assert_array_equal(parse_dlog(final.read_bytes())["V"][1], rows[:, 0])
        assert any("data is already in place" in note and "completion marker" in note
                   for note in error.value.__notes__)
    if not recover:
        assert writer._closed and writer._stream.closed
        with pytest.raises(RuntimeError, match="closed"):
            writer.finalize()
    if failure == "data":
        # The claim is released, and the retained source is still safe to recover.
        assert recover_part(part) == final
        assert _metadata(side)["complete"] is True
    else:
        before = _snapshot(tmp_path)
        assert recover_part(part) is None
        assert _snapshot(tmp_path) == before
    assert not part.exists() and not side_part.exists()


@pytest.mark.parametrize("matching_run", [False, True])
def test_recovery_from_incomplete_formal_sidecar_still_checks_run_id(tmp_path, matching_run):
    writer = _writer(tmp_path, sidecar={"run_id": "new-run"})
    writer.append(np.ones((2, 4), dtype=np.float32))
    writer.abort()
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    side_part.rename(side)  # Crash after publishing the incomplete sidecar.
    assert _metadata(side)["complete"] is False
    assert _metadata(side)["ended_utc"] is None
    if not matching_run:
        _change_metadata(side, lambda meta: meta.update(run_id="bad-run"))
    before = _snapshot(tmp_path)
    if matching_run:
        assert recover_part(part) == final
        assert _metadata(side)["complete"] is True
        assert _metadata(side)["recovered"] is True
    else:
        assert recover_part(part) is None
        assert _snapshot(tmp_path) == before


@pytest.mark.skipif(dlogfile.os.name == "nt", reason="Windows skips directory fsync")
@pytest.mark.parametrize("failure", ["open", "fsync"])
@pytest.mark.parametrize("recover", [False, True])
def test_directory_sync_failure_is_best_effort_after_publication(tmp_path, monkeypatch, failure, recover):
    writer = _writer(tmp_path)
    rows = np.arange(8, dtype=np.float32).reshape(2, 4)
    writer.append(rows)
    if recover:
        writer.abort()
    final = tmp_path / "record.dlog"
    part, side, side_part = _paths(final)
    real_open, real_fsync, real_close = dlogfile.os.open, dlogfile.os.fsync, dlogfile.os.close
    directory_fds, closed = [], []
    attempted = []

    def open_directory(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            # Both public names must already be present before directory sync.
            assert final.exists() and side.exists()
            assert not part.exists() and not side_part.exists()
            attempted.append(Path(path))
            if failure == "open":
                raise OSError("directory open unsupported")
            fd = real_open(path, flags, *args, **kwargs)
            directory_fds.append(fd)
            return fd
        return real_open(path, flags, *args, **kwargs)

    def fsync(fd):
        if fd in directory_fds:
            raise OSError("directory fsync unsupported")
        return real_fsync(fd)

    def close(fd):
        if fd in directory_fds:
            closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(dlogfile.os, "open", open_directory)
    monkeypatch.setattr(dlogfile.os, "fsync", fsync)
    monkeypatch.setattr(dlogfile.os, "close", close)
    assert (recover_part(part) if recover else writer.finalize()) == final
    assert attempted == [tmp_path]
    assert closed == directory_fds
    assert len(directory_fds) == (failure == "fsync")
    parsed = parse_dlog(final.read_bytes())
    for column, (ch, kind) in enumerate(parsed["fields"]):
        np.testing.assert_array_equal(parsed[kind][ch], rows[:, column])


@pytest.mark.parametrize("failure", ["sidecar", "data", "fsync"])
def test_publication_failure_keeps_recoverable_source(tmp_path, monkeypatch, failure):
    final = tmp_path / "record.dlog"
    final.write_bytes(b"previous run must not imply success")
    writer = _writer(tmp_path)
    writer.append(np.ones((3, 4), dtype=np.float32))
    part, side, side_part = _paths(final)
    real_replace = dlogfile.os.replace

    def replace(source, dest):
        if Path(dest) == (side if failure == "sidecar" else final):
            raise OSError("injected rename failure")
        return real_replace(source, dest)

    with monkeypatch.context() as patch:
        if failure == "fsync":
            patch.setattr(dlogfile.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("injected fsync failure")))
        else:
            patch.setattr(dlogfile.os, "replace", replace)
        with pytest.raises(OSError, match="injected"):
            writer.finalize()
    assert part.exists()
    assert final.read_bytes() == b"previous run must not imply success"
    if failure == "data":
        assert side.exists() and not side_part.exists()
    writer.abort()  # Does not recreate a published temporary sidecar.
    assert recover_part(part) == final
    assert parse_dlog(final.read_bytes())["nRecords"] == 3
    assert not part.exists() and not side_part.exists()


def test_recovery_io_failure_is_raised_and_source_remains(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    writer.append(np.ones((2, 4), dtype=np.float32))
    writer.abort()
    part, _, _ = _paths(tmp_path / "record.dlog")
    with monkeypatch.context() as patch:
        patch.setattr(dlogfile.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            recover_part(part)
    assert part.exists()
    assert recover_part(part) == tmp_path / "record.dlog"


@pytest.mark.parametrize("block", [np.zeros((1, 4), dtype=np.float64), np.zeros(4, dtype=np.float32), np.zeros((1, 3), dtype=np.float32)])
def test_append_rejects_wrong_dtype_or_shape(tmp_path, block):
    writer = _writer(tmp_path)
    try:
        with pytest.raises(ValueError, match="float32"):
            writer.append(block)
    finally:
        writer.abort(keep_part=False)


def test_noncontiguous_big_endian_block_is_written_in_row_order(tmp_path):
    rows = np.arange(32, dtype=">f4").reshape(4, 8)[:, ::2]
    writer = _writer(tmp_path)
    writer.append(rows)
    final = writer.finalize()
    parsed = parse_dlog(final.read_bytes())
    for col, (ch, kind) in enumerate(parsed["fields"]):
        np.testing.assert_array_equal(parsed[kind][ch], rows[:, col])


def test_uint32_limit_and_time_width_are_checked_before_publishing(tmp_path):
    writer = _writer(tmp_path)
    try:
        writer._records = 2**32 - 1
        with pytest.raises(ValueError, match="UInt32"):
            writer.append(np.ones((1, 4), dtype=np.float32))
    finally:
        writer.abort(keep_part=False)
    writer = _writer(tmp_path, tint=1e20)
    writer.append(np.ones((1, 4), dtype=np.float32))
    with pytest.raises(ValueError, match="24-byte"):
        writer.finalize()
    writer.abort()
    before = _snapshot(tmp_path)
    assert recover_part(tmp_path / "record.dlog.part") is None
    assert _snapshot(tmp_path) == before


def test_short_write_is_fatal_but_complete_rows_can_be_recovered(tmp_path):
    writer = _writer(tmp_path)
    original = writer._stream
    rows = np.arange(20, dtype=np.float32).reshape(5, 4)

    class ShortWrite:
        def write(self, payload):
            return original.write(payload[:3 * 16 + 2])

        def __getattr__(self, name):
            return getattr(original, name)

    writer._stream = ShortWrite()
    with pytest.raises(OSError, match="Short DLOG write"):
        writer.append(rows)
    with pytest.raises(RuntimeError, match="failed"):
        writer.finalize()
    writer.abort()
    final = recover_part(tmp_path / "record.dlog.part")
    parsed = parse_dlog(final.read_bytes())
    assert parsed["nRecords"] == 3
    for col, (ch, kind) in enumerate(parsed["fields"]):
        np.testing.assert_array_equal(parsed[kind][ch], rows[:3, col])


def test_abort_sidecar_failure_does_not_truncate_only_valid_metadata(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    writer.append(np.ones((2, 4), dtype=np.float32))
    part, _, side_part = _paths(tmp_path / "record.dlog")
    before = side_part.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(dlogfile, "_json_bytes", lambda meta: (_ for _ in ()).throw(OSError("metadata write failure")))
        with pytest.raises(OSError, match="metadata write failure"):
            writer.abort()
    assert side_part.read_bytes() == before
    assert recover_part(part) == tmp_path / "record.dlog"


@pytest.mark.parametrize("sidecar", [{"backend": "foreign"}, {"preview": 1}, {"elapsed_s": float("nan")},
                                    {"run_id": "bad-->comment"}, {"gaps": [{"kind": "lost", "index": -1, "count": None}]}])
def test_invalid_metadata_is_rejected_before_creating_files(tmp_path, sidecar):
    with pytest.raises(ValueError):
        _writer(tmp_path, sidecar=sidecar)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["missing", "stale", "hardlink"])
def test_own_part_requires_matching_safe_sidecar(tmp_path, kind):
    writer = _writer(tmp_path)
    writer.append(np.ones((2, 4), dtype=np.float32))
    writer.abort()
    part, side, side_part = _paths(tmp_path / "record.dlog")
    if kind == "missing":
        side_part.unlink()
    elif kind == "stale":
        side_part.rename(side)
        _change_metadata(side, lambda meta: meta.update(run_id="previous-recording"))
    else:
        (tmp_path / "external-data").hardlink_to(part)
    before = _snapshot(tmp_path)
    assert recover_part(part) is None
    assert _snapshot(tmp_path) == before


def test_recovery_accepts_large_notes_sidecar_without_an_undocumented_size_limit(tmp_path):
    writer = _writer(tmp_path, sidecar={"notes": ["x" * (1024**2)]})
    writer.append(np.ones((1, 4), dtype=np.float32))
    writer.abort()
    assert recover_part(tmp_path / "record.dlog.part") == tmp_path / "record.dlog"


def test_recovery_derives_count_from_length_even_if_write_index_was_interrupted(tmp_path):
    writer = _writer(tmp_path)
    rows = np.arange(16, dtype=np.float32).reshape(4, 4)
    writer.append(rows)
    writer.abort()
    part, _, side_part = _paths(tmp_path / "record.dlog")
    meta = _metadata(side_part)
    with part.open("r+b") as stream:
        stream.seek(meta["header_bytes"] + 4)
        stream.write(struct.pack(">I", 2**24))
    final = recover_part(part)
    parsed = parse_dlog(final.read_bytes())
    assert parsed["nRecords"] == 4
    np.testing.assert_array_equal(parsed["V"][1], rows[:, 0])


def test_abort_false_does_not_delete_replaced_temporary_file(tmp_path):
    writer = _writer(tmp_path)
    part, _, side_part = _paths(tmp_path / "record.dlog")
    other = tmp_path / "other-data"
    other.write_bytes(b"different inode owned by another writer")
    dlogfile.os.replace(other, side_part)
    writer.abort(keep_part=False)
    assert side_part.read_bytes() == b"different inode owned by another writer"
    assert not part.exists()
