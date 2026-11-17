"""DLOG fallback lifecycle, preview and failure preservation with fake I/O only."""

from copy import deepcopy
import json
from pathlib import Path
import signal
from threading import Event, Thread, get_ident
import time
from types import SimpleNamespace

import numpy as np
import pytest

from n6705c.core import dlog, fake
from n6705c.core.dlog import DlogConfig, DlogSession
from n6705c.core.fake import FakeN6705C
from n6705c.core.instrument import N6705C
from n6705c_seq.analysis.core import parse_dlog
from n6705c_seq.record import backend as module, contracts as c
from n6705c_seq.record.backend import make_backend


@pytest.fixture(autouse=True)
def deadline():
    if not all(hasattr(signal, name) for name in ("SIGALRM", "setitimer", "ITIMER_REAL")):
        yield
        return
    def expired(signum, frame):
        raise TimeoutError("DLOG backend test exceeded 12 seconds")
    handler = signal.signal(signal.SIGALRM, expired)
    previous = signal.setitimer(signal.ITIMER_REAL, 12)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, *previous)


class Transport:
    def __init__(self):
        self.fake = FakeN6705C()
        self.calls = []
        self.download_error = None
        self.meas_errors = set()
        self.meas_count = 0
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def write(self, command):
        self.calls.append((command, get_ident()))
        self.fake.write(command)

    def query(self, command):
        self.calls.append((command, get_ident()))
        if command.startswith("MEAS:"):
            self.meas_count += 1
            if self.meas_count in self.meas_errors:
                raise OSError("injected MEAS failure")
        return self.fake.query(command)

    def query_binary(self, command):
        self.calls.append((command, get_ident()))
        if self.download_error is not None:
            raise self.download_error
        return self.fake.query_binary(command)

    def close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path):
    transport = Transport()
    inst = N6705C.from_transport(transport)
    transport.calls.clear()
    cfg = dict(machine_index=0, channels=[1, 2], sense_volt=True, sense_curr=True,
               period_s=0.001, duration_s=10.0, run_id="test-run")
    final = tmp_path / "record.dlog"
    callbacks = []

    def untimed():
        callbacks.append("untimed")
        transport.calls.append(("untimed", get_ident()))
        return True

    def finishing():
        callbacks.append("finishing")
        transport.calls.append(("finishing", get_ident()))

    def build(config=None):
        return make_backend(inst, config or cfg, final, untimed=untimed, finishing=finishing)

    yield SimpleNamespace(transport=transport, inst=inst, cfg=cfg, final=final,
                          callbacks=callbacks, build=build)
    inst.close()


@pytest.fixture
def clock(monkeypatch):
    class Clock(Event):
        now = 100.0
        def monotonic(self):
            return self.now
        def wait(self, timeout=None):
            self.now += timeout or 0
            return self.is_set()
    clock = Clock()
    for target in (module, dlog, fake):
        monkeypatch.setattr(target, "time", clock)
    return clock


def read_meta(rig, part=False):
    return json.loads(Path(str(rig.final) + (".json.part" if part else ".json")).read_text())


def test_factory_is_inert_and_copies_config(rig):
    original = deepcopy(rig.cfg)
    backend = rig.build()
    rig.cfg["channels"].clear()
    assert rig.transport.calls == []
    assert list(rig.final.parent.iterdir()) == []
    assert backend.preview is True
    snapshot = backend.snapshot(-1)
    assert snapshot["run_id"] == original["run_id"]
    assert snapshot["preview"] is True
    assert set(snapshot["keys"]) == {"V1", "I1", "V2", "I2"}
    with pytest.raises(AttributeError):
        backend.preview = False


def test_three_second_stop_roundtrip_and_concurrent_snapshot(rig):
    backend = rig.build()
    stop, running = Event(), Event()
    statuses, results, errors = [], [], []
    def emit(status):
        statuses.append((time.monotonic(), status))
        if status["state"] == "running":
            running.set()
    def work():
        try:
            results.append(backend.run(stop, emit))
        except BaseException as exc:
            errors.append(exc)
    worker = Thread(target=work, daemon=True)
    worker.start()
    assert running.wait(2)
    try:
        until = time.monotonic() + 3
        while time.monotonic() < until:
            snap = backend.snapshot(-1)
            assert snap["preview"] is True
            for series in snap["keys"].values():
                assert len(series["min"]) == len(series["max"])
            time.sleep(0.01)
    finally:
        stop.set()
        worker.join(4)
    assert not worker.is_alive()
    assert not errors
    result = results[0]
    parsed = parse_dlog(rig.final.read_bytes())
    assert result["records"] == parsed["nRecords"] >= 2900
    assert parsed["activeChannels"] == [1, 2]
    assert parsed["tint"] == pytest.approx(rig.cfg["period_s"])
    assert result["elapsed_s"] >= 3
    snap = backend.snapshot(-1)
    assert 80 <= len(snap["keys"]["V1"]["min"]) <= 150
    assert snap["run_id"] == result["run_id"] == rig.cfg["run_id"]
    snap["keys"]["V1"]["min"].clear()
    assert backend.snapshot(-1)["keys"]["V1"]["min"]
    assert backend.snapshot(backend.snapshot(-1)["seq"])["unchanged"]
    meta = read_meta(rig)
    required = {"schema_version", "producer", "run_id", "backend", "preview", "name",
                "requested", "actual", "idn", "models", "started_utc", "ended_utc", "elapsed_s",
                "records", "header_bytes", "record_bytes", "time_offset", "time_width", "gaps", "notes", "complete"}
    assert required <= meta.keys()
    assert meta["schema_version"] == 1 and meta["producer"] == "n6705c_seq.record"
    assert meta["backend"] == "dlog" and meta["preview"] and meta["complete"]
    assert meta["requested"] == rig.cfg
    assert meta["actual"]["period_s"] == parsed["tint"]
    assert meta["records"] == result["records"]
    assert meta["models"] == {"1": "N6781A", "2": "N6785A"}
    assert meta["started_utc"] and meta["ended_utc"] and meta["idn"]
    assert not Path(str(rig.final) + ".part").exists()
    assert not Path(str(rig.final) + ".json.part").exists()
    assert not rig.transport.fake.files
    assert not rig.transport.closed
    calls = [command for command, _ in rig.transport.calls]
    abort = calls.index("ABOR:DLOG")
    assert calls[abort - 1] == "finishing"
    download = next(i for i, cmd in enumerate(calls) if cmd.startswith("MMEM:DATA?"))
    assert calls[download - 1] == "untimed"
    assert "finishing" in calls[download + 1:]
    assert len({thread for _, thread in rig.transport.calls}) == 1
    assert [s["state"] for _, s in statuses][0] == "starting"
    assert statuses[-1][1]["state"] == "stopping"
    assert {s["state"] for _, s in statuses} == {"starting", "running", "stopping"}
    # Preview samples never masquerade as persisted records during capture.
    assert all(s["records"] == 0 for _, s in statuses)
    continuous = [t for t, s in statuses if s["state"] == "running"]
    assert all(b - a >= 0.48 for a, b in zip(continuous, continuous[1:]))


def test_download_failure_keeps_remote_and_part_and_original_cause(rig, clock):
    rig.cfg["duration_s"] = 1
    original = OSError("download failed")
    original.__cause__ = RuntimeError("transport cause")
    rig.transport.download_error = original
    with pytest.raises(OSError) as caught:
        rig.build().run(clock, lambda status: None)
    assert caught.value is original
    assert str(caught.value.__cause__) == "transport cause"
    assert rig.transport.fake.dlog_file in rig.transport.fake.files
    assert Path(str(rig.final) + ".part").exists()
    assert not rig.final.exists()
    assert read_meta(rig, True)["complete"] is False
    assert not any(cmd.startswith("MMEM:DEL") for cmd, _ in rig.transport.calls)
    from n6705c_seq.web_record import _failure_code
    assert original.filename is None
    assert _failure_code(original, "fetch") is c.RecordCode.FETCH


@pytest.mark.parametrize("change", [dict(duration_s=c.DURATION_MAX_S), dict(duration_s=float("nan")),
                                   dict(duration_s=0), dict(period_s=0)])
def test_bad_config_refused_before_init(rig, change):
    rig.cfg.update(change)
    with pytest.raises(ValueError, match=r"\+323,"):
        rig.build().run(Event(), lambda status: None)
    assert not any(cmd.startswith("INIT:DLOG") for cmd, _ in rig.transport.calls)
    assert rig.callbacks == ["finishing"]
    assert not rig.final.exists()


@pytest.mark.parametrize("readback", ["nan", "0", "invalid"])
def test_actual_readback_is_validated_before_init(rig, monkeypatch, readback):
    query = rig.transport.query
    def altered(command):
        return readback if command == "SENS:DLOG:PER?" else query(command)
    monkeypatch.setattr(rig.transport, "query", altered)
    with pytest.raises(ValueError, match=r"\+323,"):
        rig.build().run(Event(), lambda status: None)
    assert not any(cmd.startswith("INIT:DLOG") for cmd, _ in rig.transport.calls)


def test_actual_size_estimate_is_checked_again_before_init(rig, monkeypatch):
    query = rig.transport.query
    def altered(command):
        return str(c.DURATION_MAX_S) if command == "SENS:DLOG:TIME?" else query(command)
    monkeypatch.setattr(rig.transport, "query", altered)
    with pytest.raises(ValueError, match="Estimated DLOG size"):
        rig.build().run(Event(), lambda status: None)
    assert not any(cmd.startswith("INIT:DLOG") for cmd, _ in rig.transport.calls)


def test_meas_failure_is_a_preview_gap_and_capture_recovers(rig, clock):
    rig.cfg["duration_s"] = 1
    rig.transport.meas_errors = {3, 10}
    backend = rig.build()
    result = backend.run(clock, lambda status: None)
    assert result["gaps"] == 2
    assert result["records"] == parse_dlog(rig.final.read_bytes())["nRecords"]
    assert result["records"] > len(backend.snapshot(-1)["keys"]["V1"]["min"])
    meta = read_meta(rig)
    assert all(gap["kind"] == "preview_meas_error" and gap["count"] is None for gap in meta["gaps"])
    assert sum("MEAS preview failed" in note for note in meta["notes"]) == 2


def test_repeated_meas_error_saves_part_and_raises_original(rig, clock, monkeypatch):
    error = RuntimeError("persistent MEAS failure")
    query = rig.transport.query
    def failure(command):
        if command.startswith("MEAS:") and clock.now >= 100.1:
            raise error
        return query(command)
    monkeypatch.setattr(rig.transport, "query", failure)
    with pytest.raises(RuntimeError) as caught:
        rig.build().run(clock, lambda status: None)
    assert caught.value is error
    assert not rig.final.exists()
    part = Path(str(rig.final) + ".part")
    assert parse_dlog(part.read_bytes())["nRecords"] > 0
    assert read_meta(rig, True)["complete"] is False
    assert len(read_meta(rig, True)["gaps"]) == c.MAX_FETCH_FAILURES
    assert rig.transport.fake.dlog_file in rig.transport.fake.files
    calls = [cmd for cmd, _ in rig.transport.calls]
    assert calls[calls.index("ABOR:DLOG") - 1] == "finishing"


def test_emit_exception_still_aborts_and_saves_without_publication(rig, clock):
    failure = RuntimeError("emit failed")
    def emit(status):
        if status["state"] == "running" and status["elapsed_s"] > 0.5:
            raise failure
    with pytest.raises(RuntimeError) as caught:
        rig.build().run(clock, emit)
    assert caught.value is failure
    assert not rig.final.exists()
    assert parse_dlog(Path(str(rig.final) + ".part").read_bytes())["nRecords"] > 0


@pytest.mark.parametrize("fail_sidecar", [False, True])
@pytest.mark.parametrize("overwrite", [False, True])
def test_publication_failure_keeps_part_and_remote(rig, clock, monkeypatch, fail_sidecar, overwrite):
    rig.cfg["duration_s"] = 1
    old_data = b"previous recording"
    old_meta = {"complete": True, "run_id": "previous-run"}
    if overwrite:
        rig.final.write_bytes(old_data)
        Path(str(rig.final) + ".json").write_text(json.dumps(old_meta))
    replace = module.os.replace
    target = Path(str(rig.final) + ".json") if fail_sidecar else rig.final
    failure = OSError("injected rename failure")
    def failing_replace(source, destination):
        if destination == target:
            raise failure
        replace(source, destination)
    monkeypatch.setattr(module.os, "replace", failing_replace)
    with pytest.raises(OSError) as caught:
        rig.build().run(clock, lambda status: None)
    assert caught.value is failure
    if overwrite:
        assert rig.final.read_bytes() == old_data
    else:
        assert not rig.final.exists()
    if not fail_sidecar:
        assert read_meta(rig)["complete"] is False
        assert read_meta(rig)["run_id"] == rig.cfg["run_id"]
    elif overwrite:
        assert read_meta(rig) == old_meta
    assert Path(str(rig.final) + ".part").exists()
    assert read_meta(rig, True)["complete"] is False
    assert rig.transport.fake.dlog_file in rig.transport.fake.files
    # The bridge can recognize even an OSError raised only from a descriptor.
    from n6705c_seq.web_record import _failure_code
    assert _failure_code(caught.value, "fetch") is c.RecordCode.SINK


def test_download_exception_survives_finishing_callback_failure(rig, clock):
    rig.cfg["duration_s"] = 1
    failure = OSError("primary download failure")
    rig.transport.download_error = failure
    count = 0
    def finishing():
        nonlocal count
        count += 1
        if count == 3:
            raise RuntimeError("secondary finishing failure")
    backend = make_backend(rig.inst, rig.cfg, rig.final, untimed=lambda: True, finishing=finishing)
    with pytest.raises(OSError) as caught:
        backend.run(clock, lambda status: None)
    assert caught.value is failure
    assert any("secondary finishing failure" in note for note in failure.__notes__)
    assert rig.transport.fake.dlog_file in rig.transport.fake.files
    assert not rig.final.exists()


def test_raw_bytes_published_before_remote_cleanup(rig, clock, monkeypatch):
    rig.cfg["duration_s"] = 1
    write = rig.transport.write
    observations = []
    def checked_delete(command):
        if command.startswith("MMEM:DEL"):
            observations.append((rig.final.read_bytes(),
                                 rig.transport.fake.files[rig.transport.fake.dlog_file],
                                 read_meta(rig)["complete"]))
        return write(command)
    monkeypatch.setattr(rig.transport, "write", checked_delete)
    rig.build().run(clock, lambda status: None)
    assert len(observations) == 1
    local, remote, complete = observations[0]
    assert local == remote
    assert complete is True
    assert any(cmd.startswith("MMEM:DEL") for cmd, _ in rig.transport.calls)


def test_snapshot_and_new_run_identity_do_not_do_io(rig, clock, monkeypatch):
    rig.cfg["duration_s"] = 1
    first = rig.build()
    first.run(clock, lambda status: None)
    rig.cfg["run_id"] = "another-run"
    second = rig.build()
    def unexpected(*args, **kwargs):
        pytest.fail("snapshot performed I/O")
    monkeypatch.setattr(rig.transport, "query", unexpected)
    monkeypatch.setattr(Path, "open", unexpected)
    assert first.snapshot(-1)["keys"]["V1"]["min"]
    assert second.snapshot(-1)["seq"] == 0
    assert second.snapshot(-1)["run_id"] == "another-run"
    assert second.snapshot(-1)["keys"]["V1"]["min"] == []


def test_sink_fsync_error_preserves_original_and_is_mapped(rig, clock, monkeypatch):
    failure = OSError("fsync failed")
    def fail(fd):
        raise failure
    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError) as caught:
        rig.build().run(clock, lambda status: None)
    assert caught.value is failure
    assert "fsync failed" in str(failure)
    from n6705c_seq.web_record import _failure_code
    assert _failure_code(failure, "fetch") is c.RecordCode.SINK
    assert not rig.final.exists()
    assert not Path(str(rig.final) + ".part").exists()
    assert not Path(str(rig.final) + ".json.part").exists()


@pytest.mark.parametrize("operation", ["write", "close", "fsync"])
def test_download_sink_error_preserves_original_and_is_mapped(rig, clock, monkeypatch, operation):
    rig.cfg["duration_s"] = 1
    part = Path(str(rig.final) + ".part")
    failure = OSError(f"download {operation} failed")
    failure.__cause__ = RuntimeError("local sink cause")
    injected = []
    open_path, fsync = Path.open, module.os.fsync

    def fail():
        assert any(cmd.startswith("MMEM:DATA?") for cmd, _ in rig.transport.calls)
        injected.append(operation)
        raise failure

    class Sink:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def write(self, payload):
            if operation == "write":
                fail()
            return self.stream.write(payload)
        def close(self):
            self.stream.close()
            if operation == "close":
                fail()
        def __exit__(self, *args):
            self.close()

    def open_sink(path, mode="r", *args, **kwargs):
        stream = open_path(path, mode, *args, **kwargs)
        if path == part and mode == "wb":
            return Sink(stream)
        return stream

    def sync(fd):
        if (operation == "fsync" and not injected
                and any(cmd.startswith("MMEM:DATA?") for cmd, _ in rig.transport.calls)):
            fail()
        return fsync(fd)

    monkeypatch.setattr(Path, "open", open_sink)
    monkeypatch.setattr(module.os, "fsync", sync)
    with pytest.raises(OSError) as caught:
        rig.build().run(clock, lambda status: None)
    assert injected == [operation]
    assert caught.value is failure
    assert str(failure.__cause__) == "local sink cause"
    assert failure.filename == str(part)
    from n6705c_seq.web_record import _failure_code
    assert _failure_code(failure, "fetch") is c.RecordCode.SINK
    assert not rig.final.exists()
    assert part.exists()
    assert read_meta(rig, True)["complete"] is False
    assert rig.transport.fake.dlog_file in rig.transport.fake.files
    assert not any(cmd.startswith("MMEM:DEL") for cmd, _ in rig.transport.calls)


def test_preflight_error_is_mapped_bad_config_during_bridge_fetch_phase(rig):
    rig.cfg["duration_s"] = c.DURATION_MAX_S
    with pytest.raises(ValueError) as caught:
        rig.build().run(Event(), lambda status: None)
    from n6705c_seq.web_record import _failure_code
    assert _failure_code(caught.value, "fetch") is c.RecordCode.BAD_CONFIG


def test_autorange_310_is_not_fatal_and_preserves_actual_ranges(rig, clock, monkeypatch):
    rig.cfg["duration_s"] = 1
    write, query = rig.transport.write, rig.transport.query
    def autorange(command):
        write(command)
        if command.startswith("SENS:DLOG:VOLT:RANG:AUTO ON"):
            rig.transport.fake.errors.append((310, "Voltage autorange unsupported"))
    def ranges(command):
        if command == "SENS:DLOG:VOLT:RANG? (@1,2)":
            return "20,20"
        return query(command)
    monkeypatch.setattr(rig.transport, "write", autorange)
    monkeypatch.setattr(rig.transport, "query", ranges)
    result = rig.build().run(clock, lambda status: None)
    assert any("+310," in note for note in result["notes"])
    assert read_meta(rig)["actual"]["ranges"]["VOLT"] == [20, 20]


@pytest.mark.parametrize("raises", [False, True])
def test_owner_refusal_skips_init(rig, clock, raises):
    def refuse():
        if raises:
            raise RuntimeError("record owner refusal")
        return False
    backend = make_backend(rig.inst, rig.cfg, rig.final, untimed=refuse,
                           finishing=lambda: rig.callbacks.append("finishing"))
    with pytest.raises(RuntimeError, match="owner"):
        backend.run(Event(), lambda status: None)
    assert not any(cmd.startswith("INIT:DLOG") for cmd, _ in rig.transport.calls)
    assert not rig.final.exists()
    assert not Path(str(rig.final) + ".part").exists()
    assert not Path(str(rig.final) + ".json.part").exists()
    rig.cfg["duration_s"] = 1
    assert rig.build().run(clock, lambda status: None)["records"] > 0


def test_every_configuration_write_is_checked_and_positive_errors_are_fatal(rig, monkeypatch):
    write = rig.transport.write
    def rejected(command):
        write(command)
        if command == "SENS:DLOG:OFFS 0":
            rig.transport.fake.errors.extend([(323, "Invalid timing"), (222, "Also invalid")])
    monkeypatch.setattr(rig.transport, "write", rejected)
    with pytest.raises(RuntimeError, match=r"\+323,"):
        rig.build().run(Event(), lambda status: None)
    commands = [cmd for cmd, _ in rig.transport.calls]
    for i, command in enumerate(commands):
        if command.startswith("SENS:DLOG:") and "?" not in command:
            assert commands[i + 1] == "SYST:ERR?"
    assert not rig.transport.fake.errors
    assert not any(cmd.startswith("INIT:DLOG") for cmd in commands)


@pytest.mark.parametrize("source", ["BUS", "IMM"])
@pytest.mark.parametrize("minmax", [False, True])
def test_fake_native_file_on_expiry_is_frozen_and_parseable(rig, clock, source, minmax):
    session = DlogSession(rig.inst, DlogConfig(channels=[1, 2], duration=1, period=0.01,
                                             minmax=minmax, trigger_source=source))
    session.start()
    clock.now += 1.5
    raw = session.transfer(session.config.filename, rig.final).read_bytes()
    parsed = parse_dlog(raw)
    assert parsed["nRecords"] == 100
    assert parsed["activeChannels"] == [1, 2]
    assert not rig.transport.fake.dlog_running
    clock.now += 5
    session.abort()
    assert session.transfer(session.config.filename, rig.final).read_bytes() == raw
    np.testing.assert_allclose(parsed["V"][1], 0)


@pytest.mark.parametrize("suffix", [".part", ".json.part"])
def test_existing_parts_are_not_overwritten(rig, clock, suffix):
    part = Path(str(rig.final) + suffix)
    part.write_bytes(b"foreign")
    with pytest.raises(FileExistsError):
        rig.build().run(clock, lambda status: None)
    assert part.read_bytes() == b"foreign"
    other = ".json.part" if suffix == ".part" else ".part"
    assert not Path(str(rig.final) + other).exists()
    assert not any(cmd.startswith("INIT:DLOG") for cmd, _ in rig.transport.calls)


@pytest.mark.parametrize("phase", ["connect", "config", "fetch"])
def test_zero_complete_records_are_not_published_and_bridge_maps_no_recording(rig, clock, phase):
    rig.cfg.update(duration_s=1, period_s=60)
    with pytest.raises(c.NoRecordsError, match="no complete records") as caught:
        rig.build().run(clock, lambda status: None)
    from n6705c_seq.web_record import _failure_code
    assert _failure_code(caught.value, phase) is c.RecordCode.NO_RECORDING
    assert not rig.final.exists()
    assert Path(str(rig.final) + ".part").exists()
    assert read_meta(rig, True)["complete"] is False
    assert rig.transport.fake.dlog_file in rig.transport.fake.files


def test_bridge_worker_reports_no_recording_from_real_backend(rig, clock, monkeypatch):
    from n6705c_seq import web_record, webgui

    rig.cfg.update(duration_s=1, period_s=60)
    monkeypatch.setattr(web_record.InstrumentGroup, "demo",
                        lambda **kwargs: SimpleNamespace(instruments=[rig.inst]))
    api = webgui.Api(rig.final.parent / "config.json")
    token = api._reserve("power")
    assert token is not None
    api._record_state.update(run_id=rig.cfg["run_id"], state="starting")

    api._record_work(rig.cfg, [], True, rig.final, clock, token)

    status = api.record_status()
    assert isinstance(api._record_exception, c.NoRecordsError)
    assert status["state"] == "failed"
    assert status["code"] == c.RecordCode.NO_RECORDING.value
    assert api._record_last_path is None
    assert not rig.final.exists()
    assert read_meta(rig, True)["complete"] is False
