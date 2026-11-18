"""ELOG backend lifecycle, recorded samples and recovery using fake I/O only."""

from collections import deque
from copy import deepcopy
import errno
import json
from pathlib import Path
import signal
from threading import Event, get_ident
from types import SimpleNamespace

import numpy as np
import pytest

from n6705c.core import elog, fake
from n6705c.core.instrument import N6705C
from n6705c.core.session import ScpiError
from n6705c_seq.analysis.core import parse_dlog
from n6705c_seq.record import backend as module, contracts as c
from n6705c_seq.record.dlogfile import recover_part
from n6705c_seq.web_record import _failure_code


class FaultIO:
    """Keep Session and binary parsing real; inject faults at the fake wire."""

    def __init__(self, base):
        self.base = base
        self.actions = deque()
        self.exchanges = []
        self.responses = []
        self.after_fetch = None
        self.on_write = None
        self.on_query = None

    def __getattr__(self, name):
        return getattr(self.base, name)

    def write(self, command):
        self.exchanges.append((command, get_ident()))
        if self.on_write:
            self.on_write(command)
        return self.base.write(command)

    def query(self, command):
        self.exchanges.append((command, get_ident()))
        response = self.on_query(command) if self.on_query else None
        if isinstance(response, Exception):
            raise response
        if response is None:
            response = self.base.query(command)
        self.responses.append((command, response))
        return response

    def query_binary(self, command):
        self.exchanges.append((command, get_ident()))
        action = self.actions.popleft() if self.actions else None
        if isinstance(action, Exception):
            raise action
        response = (action(command) if callable(action) else
                    self.base.query_binary(command) if action is None else action)
        if self.after_fetch:
            self.after_fetch()
        return response


class StrictAfterAbort(FaultIO):
    """Model E.02.09's forbidden post-ABOR fetch and retain direct evidence."""

    def __init__(self, base):
        super().__init__(base)
        self.aborted = False
        self.fetches_after_abort = []

    def write(self, command):
        if command.startswith("ABOR:ELOG"):
            # Mark before dispatch, including an injected transport timeout.
            self.aborted = True
        elif command.startswith("INIT:ELOG"):
            self.aborted = False
        return super().write(command)

    def query_binary(self, command):
        if self.aborted and command.startswith("FETC:ELOG?"):
            self.fetches_after_abort.append(command)
            self.base.errors.extend([(303, "ELOG not running"), (-420, "Query UNTERMINATED")])
        return super().query_binary(command)


class Clock:
    now = 100.0
    on_wait = None

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now = round(self.now + seconds, 12)

    def is_set(self):
        return False

    def wait(self, timeout=None):
        self.advance(timeout or 0)
        if self.on_wait:
            self.on_wait()
        return False


@pytest.fixture(autouse=True)
def timeout():
    if not all(hasattr(signal, name) for name in ("SIGALRM", "setitimer", "ITIMER_REAL")):
        yield
        return
    def expired(signum, frame):
        raise TimeoutError("ELOG backend test exceeded 12 seconds")
    handler = signal.signal(signal.SIGALRM, expired)
    previous = signal.setitimer(signal.ITIMER_REAL, 12)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, *previous)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "RECORD_BACKEND", "elog")
    clock = Clock()
    for target in (module, elog, fake):
        monkeypatch.setattr(target, "time", clock)
    recorder_class = module.ElogRecorder

    def recorder(*args, **kwargs):
        result = recorder_class(*args, **kwargs)
        # Advance instrument time at the recorder's existing bounded waits.
        result._stop = clock
        return result

    monkeypatch.setattr(module, "ElogRecorder", recorder)
    transport = StrictAfterAbort(fake.FakeN6705C(slots=("N6781A",) * 4))
    inst = N6705C.from_transport(transport)
    transport.exchanges.clear()
    transport.responses.clear()
    for ch in transport.base.channels.values():
        ch.output_on = True
        ch.voltage = ch.number + 0.5
        ch.current_limit = ch.number / 10
    cfg = dict(machine_index=0, channels=[1, 2], sense_volt=True, sense_curr=True,
               period_s=0.001, duration_s=2.0, run_id="elog-test")
    final = tmp_path / "record.dlog"
    stop = Event()
    statuses = []

    def finishing():
        transport.exchanges.append(("finishing", get_ident()))

    def untimed():
        transport.exchanges.append(("untimed", get_ident()))
        return True

    def build(**kwargs):
        return module.make_backend(inst, cfg, final, untimed=kwargs.pop("untimed", untimed),
                                   finishing=kwargs.pop("finishing", finishing), **kwargs)

    yield SimpleNamespace(io=transport, inst=inst, cfg=cfg, final=final, clock=clock,
                          stop=stop, statuses=statuses, build=build)
    inst.close()


def commands(rig):
    return [command for command, _ in rig.io.exchanges]


def meta(rig, part=False):
    return json.loads(Path(str(rig.final) + (".json.part" if part else ".json")).read_text())


def run(rig, backend=None, emit=None):
    return (backend or rig.build()).run(rig.stop, emit or rig.statuses.append)


def stop_at_running(rig, seconds):
    def emit(status):
        rig.statuses.append(status)
        if status["state"] == "running" and not rig.stop.is_set():
            rig.clock.advance(seconds)
            rig.io.exchanges.append(("user_stop", get_ident()))
            rig.stop.set()
    return emit


def assert_abort_order(rig):
    calls = commands(rig)
    aborts = [i for i, cmd in enumerate(calls) if cmd.startswith("ABOR:ELOG")]
    assert len(aborts) == 1
    assert not any(cmd.startswith("FETC:ELOG?") for cmd in calls[aborts[0] + 1:])
    assert rig.io.fetches_after_abort == []
    assert not rig.io.base.errors
    return aborts[0]


def test_dispatch_follows_record_backend_constant(rig, monkeypatch):
    assert isinstance(rig.build(), module._ElogBackend)
    monkeypatch.setattr(c, "RECORD_BACKEND", "dlog")
    assert isinstance(rig.build(), module._DlogBackend)
    monkeypatch.setattr(c, "RECORD_BACKEND", "other")
    with pytest.raises(ValueError, match="unknown RECORD_BACKEND"):
        rig.build()
    assert rig.io.exchanges == []


def test_factory_is_inert_and_preview_false(rig):
    original = deepcopy(rig.cfg)
    backend = rig.build()
    rig.cfg["channels"].clear()
    snapshot = backend.snapshot(-1)
    assert backend.preview is False
    assert snapshot["preview"] is False and snapshot["seq"] == 0
    assert list(snapshot["keys"]) == ["V1", "I1", "V2", "I2"]
    assert all(series["min"] == [] for series in snapshot["keys"].values())
    assert snapshot["run_id"] == original["run_id"]
    assert rig.io.exchanges == []
    assert list(rig.final.parent.iterdir()) == []


def test_roundtrip_uses_instrument_period(rig):
    result = run(rig, emit=stop_at_running(rig, 2))
    side = meta(rig)
    period = 1.00352e-3
    parsed = parse_dlog(rig.final.read_bytes())
    assert side["actual"]["period_s"] == parsed["tint"] == period
    raw = rig.final.read_bytes()
    assert f"<tint>{period:.12g}</tint>".encode() in raw
    assert side["actual"]["ranges"] == {"voltage": "MAX", "current": "MAX"}
    assert side["requested"] == rig.cfg
    assert side["idn"] == ",".join((rig.inst.identity.manufacturer, rig.inst.identity.model,
                                    rig.inst.identity.serial, rig.inst.identity.firmware))
    assert result["records"] == side["records"] == parsed["nRecords"] == int(2 / period)
    assert side["complete"] is True and side["backend"] == "elog"
    assert side["preview"] is result["preview"] is False
    assert any("adjusted the ELOG period" in note for note in result["notes"])
    assert any("maximum voltage range" in note for note in result["notes"])
    for ch in rig.cfg["channels"]:
        channel = rig.io.base.channels[ch]
        times = np.arange(result["records"]) * period
        np.testing.assert_allclose(parsed["V"][ch],
                                   [rig.io.base._elog_voltage_at(channel, t) for t in times], rtol=1e-6)
        np.testing.assert_allclose(parsed["I"][ch],
                                   [rig.io.base._elog_current_at(channel, t) for t in times], rtol=1e-6)
    assert not list(rig.final.parent.glob("*.part"))
    assert "*IDN?" not in commands(rig)
    assert not any(":RANG?" in cmd or cmd == "*RST" for cmd in commands(rig))


@pytest.mark.parametrize("voltage,current", [(True, True), (True, False), (False, True)])
def test_channel_order_and_single_quantity(rig, voltage, current):
    rig.cfg.update(channels=[3, 1], sense_volt=voltage, sense_curr=current)
    result = run(rig)
    parsed = parse_dlog(rig.final.read_bytes())
    expected = [(ch, kind) for ch in [1, 3]
                for kind, enabled in (("V", voltage), ("I", current)) if enabled]
    assert parsed["fields"] == expected
    assert parsed["activeChannels"] == [1, 3]
    assert meta(rig)["record_bytes"] == len(expected) * 4
    for ch, kind in expected:
        sample = getattr(rig.io.base, "_elog_voltage_at" if kind == "V" else "_elog_current_at")
        assert parsed[kind][ch][0] == pytest.approx(sample(rig.io.base.channels[ch], 0))
        assert len(parsed[kind][ch]) == result["records"]


def test_four_channels_vi_at_floor_period(rig):
    rig.cfg.update(channels=[4, 2, 1, 3], period_s=819.2e-6)
    result = run(rig)
    parsed = parse_dlog(rig.final.read_bytes())
    assert result["records"] == int(2 / 819.2e-6)
    assert parsed["fields"] == [(ch, kind) for ch in [1, 2, 3, 4] for kind in ("V", "I")]
    assert meta(rig)["gaps"] == []
    for ch in range(1, 5):
        for kind in ("V", "I"):
            channel = rig.io.base.channels[ch]
            sample = getattr(rig.io.base, "_elog_voltage_at" if kind == "V" else "_elog_current_at")
            expected = [sample(channel, i * parsed["tint"]) for i in range(result["records"])]
            np.testing.assert_allclose(parsed[kind][ch], expected, rtol=1e-6)


def test_stop_drains_then_aborts_and_never_fetches_after_abort(rig):
    result = run(rig, emit=stop_at_running(rig, 2))
    abort = assert_abort_order(rig)
    fetches = [i for i, cmd in enumerate(commands(rig)) if cmd.startswith("FETC:ELOG?")]
    assert fetches and max(fetches) < abort
    assert len({owner for _, owner in rig.io.exchanges}) == 1
    assert not any("+303" in note or "-420" in note for note in result["notes"])


def test_error_queue_checked_after_every_step_and_ends_clean(rig):
    run(rig)
    calls = commands(rig)
    for i, command in enumerate(calls):
        if command.startswith(("FORM", "SENS:", "TRIG:", "INIT:", "ABOR:")):
            assert calls[i + 1] == "SYST:ERR?"
    errors = [response for cmd, response in rig.io.responses if cmd == "SYST:ERR?"]
    assert errors and int(errors[-1].split(",")[0]) == 0
    assert not rig.io.base.errors


def test_live_snapshot_is_recorded_data_not_preview(rig):
    backend = rig.build()
    live = []
    def emit(status):
        rig.statuses.append(status)
        if status["state"] == "running" and status["records"]:
            live.append(backend.snapshot(-1))
    result = run(rig, backend, emit)
    assert live and all(snap["seq"] > 0 and snap["preview"] is False for snap in live)
    assert all(snap["period_s"] == 1.00352e-3 for snap in live)
    parsed = parse_dlog(rig.final.read_bytes())
    for snap in live:
        for key, series in snap["keys"].items():
            np.testing.assert_array_equal(series["min"], parsed[key[0]][int(key[1])][:len(series["min"])])
    assert backend.snapshot(-1)["seq"] >= live[-1]["seq"]
    assert result["records"] > 0
    assert not any(cmd.startswith("MEAS:") for cmd in commands(rig))


def test_duration_expiry_uses_same_stop_path(rig):
    result = run(rig)
    assert rig.stop.is_set() is False
    assert result["elapsed_s"] == pytest.approx(2)
    assert result["records"] == int(2 / 1.00352e-3)
    abort = assert_abort_order(rig)
    assert any(cmd.startswith("FETC:ELOG?") for cmd in commands(rig)[:abort])


def test_stop_before_init_cancels_without_init_or_abort(rig):
    rig.stop.set()
    with pytest.raises(c.NoRecordsError, match="before INIT:ELOG"):
        run(rig)
    assert not any(cmd.startswith(("INIT:ELOG", "ABOR:ELOG")) for cmd in commands(rig))
    assert list(rig.final.parent.iterdir()) == []
    rig.stop.clear()
    assert run(rig)["records"] > 0


def test_zero_records_not_published(rig):
    with pytest.raises(c.NoRecordsError) as caught:
        run(rig, emit=stop_at_running(rig, 0))
    assert _failure_code(caught.value, "fetch") == c.RecordCode.NO_RECORDING
    assert list(rig.final.parent.iterdir()) == []
    assert_abort_order(rig)


@pytest.mark.parametrize("mode", ["overflow", "empty_backlog"])
def test_overflow_and_empty_backlog_become_gaps_but_publish(rig, mode):
    if mode == "empty_backlog":
        def discard(command):
            rig.io.base.query_binary(command)
            return b",".join([b"#10"] * 2) + b"\n"
        rig.io.actions.append(discard)
        # Let the next pump deliver samples while retaining the first gap.
        def jump():
            rig.clock.on_wait = None
            rig.clock.advance(1)
        rig.clock.on_wait = jump
        result = run(rig)
    else:
        result = run(rig, emit=stop_at_running(rig, 12))
    side = meta(rig)
    assert side["complete"] is True and result["records"] > 0
    kinds = [gap["kind"] for gap in side["gaps"]]
    assert mode in kinds and "tail_incomplete" in kinds
    # Empty backlog and incomplete tail remain separate events.
    assert all(type(gap["count"]) is int for gap in side["gaps"])
    assert result["gaps"] == len(side["gaps"]) == rig.statuses[-1]["gaps"]
    assert_abort_order(rig)


def test_fetch_error_is_note_not_gap(rig):
    rig.io.actions.append(TimeoutError("one transient fetch"))
    result = run(rig)
    assert any("one transient fetch" in note for note in result["notes"])
    assert any("fetch_error" in note for note in result["notes"])
    assert meta(rig)["gaps"] == []
    assert meta(rig)["complete"] is True
    assert_abort_order(rig)


def test_tail_limit_and_incomplete_are_separate_gaps(rig, monkeypatch):
    monkeypatch.setattr(c, "ELOG_FETCH_RECORDS", 1)
    result = run(rig, emit=stop_at_running(rig, 2))
    side = meta(rig)
    assert result["records"] == 16
    assert [gap["kind"] for gap in side["gaps"]] == ["tail_limit", "tail_incomplete"]
    assert side["complete"] is True
    assert_abort_order(rig)


def test_three_fetch_failures_abort_and_keep_recoverable_part(rig):
    errors = [TimeoutError(f"fetch failure {i}") for i in range(3)]
    def fail_after_data():
        rig.io.after_fetch = None
        rig.io.actions.extend(errors)
    rig.io.after_fetch = fail_after_data
    with pytest.raises(elog.ElogFetchError) as caught:
        run(rig)
    assert caught.value.__cause__ is errors[-1]
    assert_abort_order(rig)
    side = meta(rig, part=True)
    assert side["complete"] is False and side["records"] > 0
    assert not rig.final.exists()
    assert recover_part(Path(str(rig.final) + ".part")) == rig.final
    assert parse_dlog(rig.final.read_bytes())["nRecords"] == side["records"]


def test_sink_disk_error_aborts_and_maps_sink(rig, monkeypatch):
    append = module.DlogWriter.append
    original = OSError(errno.ENOSPC, "disk full")
    def fail(self, block):
        if self._records:
            raise original
        append(self, block)
    monkeypatch.setattr(module.DlogWriter, "append", fail)
    with pytest.raises(elog.ElogSinkError) as caught:
        run(rig)
    assert caught.value.__cause__ is original
    assert _failure_code(caught.value, "fetch") == c.RecordCode.SINK
    assert_abort_order(rig)
    assert meta(rig, part=True)["complete"] is False
    assert recover_part(Path(str(rig.final) + ".part")) == rig.final


def test_init_323_is_bad_config_without_abort(rig):
    def inject(command):
        if command.startswith("INIT:ELOG"):
            rig.io.base.errors.append((323, "injected INIT failure"))
    rig.io.on_write = inject
    with pytest.raises(ScpiError) as caught:
        run(rig)
    assert caught.value.code == 323
    assert _failure_code(caught.value, "fetch") == c.RecordCode.BAD_CONFIG
    assert not any(cmd.startswith("ABOR:ELOG") for cmd in commands(rig))
    assert not rig.io.base.errors
    assert list(rig.final.parent.iterdir()) == []


def test_trigger_failure_after_init_aborts_once(rig):
    def inject(command):
        if command.startswith("TRIG:ELOG "):
            rig.io.base.errors.append((-113, "injected trigger failure"))
    rig.io.on_write = inject
    with pytest.raises(ScpiError):
        run(rig)
    assert_abort_order(rig)
    assert not any(cmd.startswith("FETC:ELOG?") for cmd in commands(rig))
    assert list(rig.final.parent.iterdir()) == []


@pytest.mark.parametrize("period", [0.002, float("nan"), float("inf"), 0, -0.001])
def test_period_readback_beyond_one_grid_step_is_bad_config(rig, period):
    rig.cfg["channels"] = [1]
    rig.io.on_query = lambda command: str(period) if command.startswith("SENS:ELOG:PER?") else None
    with pytest.raises(ValueError) as caught:
        run(rig)
    assert _failure_code(caught.value, "fetch") == c.RecordCode.BAD_CONFIG
    assert not any(cmd.startswith(("INIT:ELOG", "ABOR:ELOG")) for cmd in commands(rig))
    assert list(rig.final.parent.iterdir()) == []


def test_rate_mismatch_adds_note_only(rig):
    rig.cfg["duration_s"] = 20
    rig.io.on_query = lambda command: "0.001,0.001" if command.startswith("SENS:ELOG:PER?") else None
    result = run(rig)
    assert result["elapsed_s"] == pytest.approx(20)
    assert any("measured rate" in note and "1000 records/s" in note for note in result["notes"])
    assert meta(rig)["gaps"] == [] and meta(rig)["complete"] is True


def test_uint32_limit_preflight(rig):
    rig.cfg["duration_s"] = (2 ** 32) * rig.cfg["period_s"]
    with pytest.raises(ValueError, match="UInt32") as caught:
        run(rig)
    assert _failure_code(caught.value, "fetch") == c.RecordCode.BAD_CONFIG
    assert commands(rig) == ["finishing"]
    assert list(rig.final.parent.iterdir()) == []


def test_elog_is_not_subject_to_native_dlog_size_limit(rig):
    rig.cfg["duration_s"] = 100_000
    estimated = rig.cfg["duration_s"] / rig.cfg["period_s"] * 4 * 4
    assert estimated > c.DLOG_FALLBACK_MAX_BYTES
    result = run(rig, emit=stop_at_running(rig, 2))
    assert result["records"] > 0
    assert meta(rig)["complete"] is True


def test_owner_refusal_skips_init(rig):
    with pytest.raises(RuntimeError, match="owner is no longer active"):
        run(rig, backend=rig.build(untimed=lambda: False))
    assert not any(cmd.startswith(("INIT:ELOG", "ABOR:ELOG")) for cmd in commands(rig))
    assert list(rig.final.parent.iterdir()) == []


def test_finishing_before_tail_and_abort(rig):
    run(rig, emit=stop_at_running(rig, 2))
    calls = commands(rig)
    stop = calls.index("user_stop")
    finishing = calls.index("finishing", stop)
    fetch = next(i for i in range(stop, len(calls)) if calls[i].startswith("FETC:ELOG?"))
    assert stop < finishing < fetch < assert_abort_order(rig)


@pytest.mark.parametrize("replace_existing", [False, True])
def test_target_changed_during_capture_is_preserved(rig, replace_existing):
    if replace_existing:
        rig.final.write_bytes(b"original")
    def replace():
        rig.io.after_fetch = None
        rig.final.write_bytes(b"another writer")
    rig.io.after_fetch = replace
    with pytest.raises(FileExistsError, match="Target created or changed"):
        run(rig)
    assert rig.final.read_bytes() == b"another writer"
    assert Path(str(rig.final) + ".part").exists()
    assert meta(rig, part=True)["complete"] is False
    assert_abort_order(rig)


@pytest.mark.parametrize("drain_fails", [False, True])
def test_abort_failure_logs_best_effort_error_drain(rig, drain_fails):
    original = TimeoutError("ABOR timed out")
    original.__cause__ = OSError("transport disconnected")
    def fail(command):
        if command.startswith("ABOR:ELOG"):
            raise original
    rig.io.on_write = fail
    checks = 0
    def after_abort(command):
        nonlocal checks
        if rig.io.aborted and command == "SYST:ERR?":
            checks += 1
            if checks == 1:
                # Core's strict check succeeds, preserving the ABOR timeout.
                # An error arriving later is drained by backend cleanup.
                rig.io.base.errors.append((-200, "late abort transport error"))
                return '+0,"No error"'
            if drain_fails:
                raise OSError("error queue disconnected")
        return None
    rig.io.on_query = after_abort
    with pytest.raises(TimeoutError) as caught:
        run(rig)
    assert caught.value is original
    assert str(caught.value.__cause__) == "transport disconnected"
    side = meta(rig, part=True)
    assert side["complete"] is False
    assert any("best-effort error drain" in note for note in side["notes"])
    assert any(("disconnected" if drain_fails else "-200") in note for note in side["notes"])
    assert rig.io.fetches_after_abort == []
    calls = commands(rig)
    abort = next(i for i, cmd in enumerate(calls) if cmd.startswith("ABOR:ELOG"))
    assert not any(cmd.startswith("FETC:ELOG?") for cmd in calls[abort + 1:])
    assert sum(cmd.startswith("ABOR:ELOG") for cmd in calls) == 1
    if not drain_fails:
        assert not rig.io.base.errors


def test_tail_constant_guard(rig, monkeypatch):
    monkeypatch.setattr(c, "ELOG_TAIL", "abort_then_drain")
    with pytest.raises(ValueError, match="requires drain_then_abort"):
        rig.build()
    assert rig.io.exchanges == []


def test_primary_sink_failure_survives_abort_and_writer_cleanup_failures(rig, monkeypatch):
    append = module.DlogWriter.append
    abort = module.DlogWriter.abort
    original = OSError(errno.ENOSPC, "primary disk failure")
    def fail_append(self, block):
        if self._records:
            raise original
        append(self, block)
    def fail_abort(command):
        if command.startswith("ABOR:ELOG"):
            raise TimeoutError("secondary ABOR timeout")
    def fail_cleanup(self, keep_part=True):
        abort(self, keep_part=keep_part)
        raise OSError("secondary writer cleanup failure")
    monkeypatch.setattr(module.DlogWriter, "append", fail_append)
    monkeypatch.setattr(module.DlogWriter, "abort", fail_cleanup)
    rig.io.on_write = fail_abort
    with pytest.raises(elog.ElogSinkError) as caught:
        run(rig)
    assert caught.value.__cause__ is original
    detail = " ".join(caught.value.__notes__)
    assert "secondary ABOR timeout" in detail and "secondary writer cleanup failure" in detail
    assert_abort_order(rig)
    assert meta(rig, part=True)["complete"] is False
    assert recover_part(Path(str(rig.final) + ".part")) == rig.final
