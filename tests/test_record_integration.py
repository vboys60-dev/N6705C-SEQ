"""Real demo recording -> finalized DLOG -> authenticated analysis HTTP open."""

import json
from threading import Condition, Event
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from n6705c.core import dlog, elog, fake
from n6705c.core.group import InstrumentGroup
from n6705c_seq.record import backend, contracts as c

# Reuse the real server/core/storage fixture and Viewer wire protocol checks.
from test_analysis_integration import api, enter, wait_state


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "record worker did not reach the expected state"
        time.sleep(0.002)


@pytest.fixture
def recording(api, tmp_path, monkeypatch):
    monkeypatch.setattr(c, "RECORD_BACKEND", "dlog")

    class Clock:
        # Nonzero origin also exercises DlogSession's elapsed-time handling.
        now = 100.0

        def monotonic(self):
            return self.now

    clock = Clock()
    # Replace module references, never the process-wide time.monotonic: gate
    # timers, HTTP and test deadlines must continue to use real time.
    for module in (backend, dlog, elog, fake):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(c, "RECORD_ENABLED", True)
    path = tmp_path / "capture.dlog"
    api._window.path = str(path)
    cfg = dict(machine_index=0, channels=[1, 2], sense_volt=True,
               sense_curr=True, period_s=0.001, duration_s=10)
    yield SimpleNamespace(api=api, clock=clock, path=path, cfg=cfg)
    # Also unblock and join a worker when an assertion or socket setup fails.
    api.record_stop()
    worker = api._record_thread
    if worker is not None:
        worker.join(2)
        assert not worker.is_alive()


def start(recording):
    assert recording.api.record_start(recording.cfg, [], True) == {}
    wait_for(lambda: recording.api.record_status()["state"] == "running")
    assert recording.api._gate.state()[0] == "power"


def stop(recording):
    assert recording.api.record_stop() == {}
    recording.api._record_thread.join(2)
    assert not recording.api._record_thread.is_alive()
    assert recording.api._gate.state()[0] == "idle"
    return recording.api.record_status()


class StrictAfterAbort:
    """Wrap only fake I/O, rejecting the fetches forbidden by E.02.09."""

    def __init__(self, base):
        self.base = base
        self.commands = []
        self.aborted = False
        self.fetches_after_abort = []
        self.periods = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def write(self, command):
        self.commands.append(command)
        if command.startswith("ABOR:ELOG"):
            self.aborted = True
        elif command.startswith("INIT:ELOG"):
            self.aborted = False
        return self.base.write(command)

    def query(self, command):
        self.commands.append(command)
        response = self.base.query(command)
        if command.startswith("SENS:ELOG:PER?"):
            self.periods.extend(float(value) for value in response.split(","))
        return response

    def query_binary(self, command):
        self.commands.append(command)
        if self.aborted and command.startswith("FETC:ELOG?"):
            self.fetches_after_abort.append(command)
            self.base.errors.extend([(303, "ELOG not running"), (-420, "Query UNTERMINATED")])
        return self.base.query_binary(command)


@pytest.fixture
def elog_recording(recording, monkeypatch):
    monkeypatch.setattr(c, "RECORD_BACKEND", "elog")
    r = recording

    def demo(frames, slots):
        assert frames == 1
        r.io = StrictAfterAbort(fake.FakeN6705C(slots))
        return InstrumentGroup.from_transports([r.io])

    monkeypatch.setattr(InstrumentGroup, "demo", demo)
    # Advance only after the worker has calculated its next fetch deadline.
    # A condition keeps the fake clock deterministic without changing real
    # gate/HTTP deadlines or auto-advancing the recording to completion.
    condition = Condition()
    waiting = False

    class RecorderWait(Event):
        def wait(self, timeout=None):
            nonlocal waiting
            with condition:
                before = r.clock.now
                waiting = True
                condition.notify_all()
                try:
                    condition.wait_for(lambda: self.is_set() or r.clock.now != before, timeout)
                finally:
                    waiting = False
                return self.is_set()

    recorder_class = backend.ElogRecorder

    def recorder(*args, **kwargs):
        result = recorder_class(*args, **kwargs)
        result._stop = RecorderWait()
        return result

    def advance(seconds):
        with condition:
            assert condition.wait_for(lambda: waiting, timeout=2), "recorder did not wait"
            r.clock.now += seconds
            condition.notify_all()

    monkeypatch.setattr(backend, "ElogRecorder", recorder)
    r.advance = advance
    return r


def _elog_capture(recording):
    r = recording
    start(r)
    actual = r.io.periods[0]
    assert r.io.periods == [actual, actual]
    assert actual == pytest.approx(1.00352e-3)
    assert actual != r.cfg["period_s"]
    assert r.api.record_status()["preview"] is False
    seq = 0
    for seconds in (0.5, 0.5):
        r.advance(seconds)
        wait_for(lambda: r.api.record_snapshot(-1)["seq"] > seq)
        snapshot = r.api.record_snapshot(seq)
        assert snapshot["seq"] > seq
        assert snapshot["preview"] is False
        assert snapshot["period_s"] == actual
        assert snapshot["run_id"] == r.api.record_status()["run_id"]
        assert set(snapshot["keys"]) == {"V1", "I1", "V2", "I2"}
        assert all(series["min"] and series["max"] for series in snapshot["keys"].values())
        assert r.api.record_status()["state"] == "running"
        seq = snapshot["seq"]
    r.advance(1.0)
    status = stop(r)
    assert status["state"] == "done", status
    assert status["preview"] is False
    assert status["elapsed_s"] == pytest.approx(2.0)
    assert status["records"] == int(2.0 / actual)
    sidecar = json.loads(Path(str(r.path) + ".json").read_text())
    assert sidecar["backend"] == "elog"
    assert sidecar["preview"] is False
    assert sidecar["complete"] is True
    assert sidecar["actual"]["period_s"] == actual
    assert sidecar["records"] == status["records"]
    assert sidecar["run_id"] == status["run_id"]
    assert sidecar["gaps"] == []
    assert not Path(str(r.path) + ".part").exists()
    assert not Path(str(r.path) + ".json.part").exists()

    events = r.api.poll()
    assert all(event[1]["preview"] is False for event in events if event[0] == "record")
    idle = max(i for i, event in enumerate(events)
               if event[0] == "state" and event[1]["state"] == "idle")
    done = next(i for i, event in enumerate(events)
                if event[0] == "record" and event[1]["state"] == "done")
    assert idle < done
    return status, actual


def test_elog_demo_record_stop_load_and_real_server_open(elog_recording):
    r = elog_recording
    status, actual = _elog_capture(r)
    entered, viewer = enter(r.api)
    loaded = r.api.record_load(entered["gen"])
    assert set(loaded) == {"ok", "gen", "grant", "name"}
    assert loaded["ok"] is True and loaded["name"] == r.path.name
    assert loaded["gen"] == entered["gen"]
    meta = viewer.open(loaded["grant"])
    assert meta["fileName"] == r.path.name
    assert len(meta["handle"]) == 40
    assert meta["tint"] == actual
    assert meta["tint"] != r.cfg["period_s"]
    assert meta["nRecords"] == status["records"]
    assert meta["activeChannels"] == r.cfg["channels"]
    assert viewer.req("GET", f"/samples?h={meta['handle']}&lo=0&hi=9")[0] == 200
    assert r.api.analysis_leave(entered["gen"])["ok"] is True
    wait_state(r.api, "idle", timeout=2)


def test_elog_hardware_description_still_no_meas_and_no_fetch_after_abort(elog_recording, monkeypatch):
    monkeypatch.setattr(fake.FakeN6705C, "description", "VISA (backend=ni)")
    r = elog_recording
    _elog_capture(r)
    assert not any(command.startswith("MEAS:") for command in r.io.commands)
    aborts = [i for i, command in enumerate(r.io.commands) if command.startswith("ABOR:ELOG")]
    fetches = [i for i, command in enumerate(r.io.commands) if command.startswith("FETC:ELOG?")]
    assert len(aborts) == 1
    assert fetches and max(fetches) < aborts[0]
    assert r.io.fetches_after_abort == []
    assert not r.io.base.errors


def test_elog_zero_records_return_no_recording(elog_recording):
    _check_zero_records_return_no_recording(elog_recording)
    assert elog_recording.api.record_status()["preview"] is False


@pytest.mark.parametrize("kind", ["elog", "dlog"])
def test_record_api_rejects_gui_backend_selection(recording, kind):
    r = recording
    assert r.api.record_start({**r.cfg, "backend": kind}, [], True)["code"] == "bad_config"
    assert r.api._gate.state()[0] == "idle"
    assert r.api._record_thread is None
    assert not r.path.exists()


def _capture_and_check_preview(recording, monkeypatch, hardware):
    meas_calls = []
    if hardware:
        # Exercise the hardware policy through fake I/O; never connect to VISA.
        monkeypatch.setattr(fake.FakeN6705C, 'description', 'VISA (backend=ni)')
        query = fake.FakeN6705C.query

        def no_meas(self, command):
            if command.startswith('MEAS:'):
                meas_calls.append(command)
            return query(self, command)

        monkeypatch.setattr(fake.FakeN6705C, 'query', no_meas)
    wall_started = time.monotonic()
    r = recording
    start(r)
    assert r.api.record_status()["preview"] is (not hardware)
    if hardware:
        snapshot = r.api.record_snapshot(-1)
        assert snapshot["preview"] is False and snapshot["seq"] == 0
        assert snapshot["period_s"] == r.cfg["period_s"]
    else:
        # Preview is real MEAS through the demo instrument, not synthetic samples.
        wait_for(lambda: r.api.record_snapshot(-1)["seq"] > 0)
        assert r.api.record_snapshot(-1)["preview"] is True
    r.clock.now += 2.0
    status = stop(r)
    assert status["state"] == "done", status
    assert meas_calls == []
    assert status["elapsed_s"] == pytest.approx(2.0)
    assert status["records"] == 2000
    assert status["preview"] is (not hardware)
    if hardware:
        assert r.api.record_snapshot(-1) == snapshot
    assert r.path.is_file()
    assert not Path(str(r.path) + ".part").exists()
    sidecar = json.loads(Path(str(r.path) + ".json").read_text())
    assert sidecar["backend"] == "dlog"
    assert sidecar["complete"] is True
    assert sidecar["records"] == status["records"]
    assert sidecar["run_id"] == status["run_id"]
    assert sidecar["preview"] is (not hardware)

    events = r.api.poll()
    if hardware:
        assert all(event[1]["preview"] is False for event in events if event[0] == "record")
    states = [event[1]["state"] for event in events if event[0] == "state"]
    assert states[-2:] == ["power", "idle"]
    idle = max(i for i, event in enumerate(events)
               if event[0] == "state" and event[1]["state"] == "idle")
    done = next(i for i, event in enumerate(events)
                if event[0] == "record" and event[1]["state"] == "done")
    assert idle < done

    return status, wall_started


@pytest.mark.parametrize('hardware', [False, True])
def test_record_preview_policy_through_bridge(recording, monkeypatch, hardware):
    _capture_and_check_preview(recording, monkeypatch, hardware)


def test_demo_record_stop_load_and_real_server_open(recording, monkeypatch):
    status, wall_started = _capture_and_check_preview(recording, monkeypatch, False)
    r = recording
    entered, viewer = enter(r.api)
    loaded = r.api.record_load(entered["gen"])
    assert set(loaded) == {"ok", "gen", "grant", "name"}
    assert loaded["ok"] is True and loaded["name"] == r.path.name
    assert loaded["gen"] == entered["gen"]
    meta = viewer.open(loaded["grant"])
    assert meta["fileName"] == r.path.name
    assert len(meta["handle"]) == 40
    assert meta["nRecords"] == status["records"]
    assert meta["tint"] == pytest.approx(r.cfg["period_s"])
    assert meta["activeChannels"] == r.cfg["channels"]
    assert viewer.req("GET", f"/samples?h={meta['handle']}&lo=0&hi=9")[0] == 200
    assert r.api.analysis_leave(entered["gen"])["ok"] is True
    wait_state(r.api, "idle", timeout=2)
    assert time.monotonic() - wall_started < 10


def test_analysis_enter_is_rejected_during_demo_recording(recording):
    start(recording)
    reply = recording.api.analysis_enter()
    assert reply["ok"] is False and reply["code"] == "power"
    assert recording.api._gate.state()[0] == "power"
    assert recording.api._server is None
    recording.clock.now += 2.0
    assert stop(recording)["state"] == "done"


def _check_zero_records_return_no_recording(recording):
    start(recording)
    # No fake instrument time has elapsed, so neither backend has a record.
    status = stop(recording)
    assert status["state"] == "failed"
    assert status["code"] == "no_recording"
    assert status["records"] == 0
    assert recording.api._record_last_path is None
    assert not recording.path.exists()
    assert not Path(str(recording.path) + ".json").exists()
    assert not Path(str(recording.path) + ".part").exists()
    assert not Path(str(recording.path) + ".json.part").exists()
    assert recording.api.record_stop()["code"] == "no_recording"


def test_zero_records_return_no_recording_without_final_file(recording):
    _check_zero_records_return_no_recording(recording)


def test_record_start_disabled_before_dialog_or_worker(api, monkeypatch):
    monkeypatch.setattr(c, "RECORD_ENABLED", False)

    def forbidden(*args, **kwargs):
        pytest.fail("disabled recording must not reserve power or show a dialog")

    monkeypatch.setattr(api, "_reserve", forbidden)
    monkeypatch.setattr(api._window, "create_file_dialog", forbidden)
    assert api.record_meta()["enabled"] is False
    assert api.record_start(None, None, None)["code"] == "disabled"
    assert api._gate.state()[0] == "idle"
    assert api._record_thread is None
