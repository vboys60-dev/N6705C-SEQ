"""Real demo recording -> finalized DLOG -> authenticated analysis HTTP open."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from n6705c.core import dlog, fake
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
    class Clock:
        # Nonzero origin also exercises DlogSession's elapsed-time handling.
        now = 100.0

        def monotonic(self):
            return self.now

    clock = Clock()
    # Replace module references, never the process-wide time.monotonic: gate
    # timers, HTTP and test deadlines must continue to use real time.
    for module in (backend, dlog, fake):
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


def test_demo_record_stop_load_and_real_server_open(recording):
    wall_started = time.monotonic()
    r = recording
    start(r)
    # Preview is real MEAS through the demo instrument, not synthetic samples.
    wait_for(lambda: r.api.record_snapshot(-1)["seq"] > 0)
    r.clock.now += 2.0
    status = stop(r)
    assert status["state"] == "done", status
    assert status["elapsed_s"] == pytest.approx(2.0)
    assert status["records"] == 2000
    assert status["preview"] is True
    assert r.path.is_file()
    assert not Path(str(r.path) + ".part").exists()
    sidecar = json.loads(Path(str(r.path) + ".json").read_text())
    assert sidecar["complete"] is True
    assert sidecar["records"] == status["records"]
    assert sidecar["run_id"] == status["run_id"]

    events = r.api.poll()
    states = [event[1]["state"] for event in events if event[0] == "state"]
    assert states[-2:] == ["power", "idle"]
    idle = max(i for i, event in enumerate(events)
               if event[0] == "state" and event[1]["state"] == "idle")
    done = next(i for i, event in enumerate(events)
                if event[0] == "record" and event[1]["state"] == "done")
    assert idle < done

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


def test_zero_records_return_no_recording_without_final_file(recording):
    start(recording)
    # The instrument clock never advances, so even a preview round cannot
    # create a complete on-instrument DLOG record before the real stop call.
    status = stop(recording)
    assert status["state"] == "failed"
    assert status["code"] == "no_recording"
    assert status["records"] == 0
    assert recording.api._record_last_path is None
    assert not recording.path.exists()
    assert not Path(str(recording.path) + ".json").exists()
    # Failed native downloads retain partial evidence without publishing it.
    partial = Path(str(recording.path) + ".json.part")
    assert json.loads(partial.read_text())["complete"] is False
    assert recording.api.record_stop()["code"] == "no_recording"


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
