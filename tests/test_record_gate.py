"""C3 transitions against the real activity gate and a stub recording backend."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from n6705c_seq.analysis import contracts as ac
from n6705c_seq.record import contracts as rc
from test_web_record import rig, wait_for


@pytest.fixture
def viewer(rig):
    # Exercise the real public analysis_enter/leave methods without a socket.
    rig.api._server = type("Server", (), {"url": lambda self, gen, lang: "http://localhost/stub",
                                         "release_resources": lambda self: True})()
    return rig.api


def test_analysis_draining_idle_then_record_obtains_power(rig, viewer):
    gen = viewer.analysis_enter()["gen"]
    job = viewer._gate.job_begin(gen)
    assert viewer.analysis_leave(gen) == {"ok": True, "gen": gen}
    assert viewer._gate.state()[0] == "draining"
    viewer._gate.job_end(job)
    wait_for(lambda: viewer._gate.state()[0] == "idle")
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert viewer._gate.state()[0] == "power"
    assert rig.finish()["state"] == "done"
    transitions = [event[1]["state"] for event in viewer.poll() if event[0] == "state"]
    assert transitions == ["idle", "analysis", "draining", "idle", "power", "idle"]


def test_start_rejected_in_analysis_and_draining(rig, viewer):
    gen = viewer.analysis_enter()["gen"]
    assert rig.start()["code"] == "analysis"
    job = viewer._gate.job_begin(gen)
    assert viewer.analysis_leave(gen)["ok"]
    assert rig.start()["code"] == "draining"
    assert not rig.calls
    viewer._gate.job_end(job)
    wait_for(lambda: viewer._gate.state()[0] == "idle")


def test_analysis_enter_rejected_during_recording_and_cleanup(rig, viewer):
    rig.resume.clear()
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert viewer.analysis_enter()["code"] == "power"
    assert viewer.record_stop() == {}
    assert rig.tail.wait(1)
    assert viewer.analysis_enter()["code"] == "power"
    assert rig.finish()["state"] == "done"
    assert viewer.analysis_enter()["ok"]


def test_window_close_veto_during_recording_and_cleanup(rig):
    rig.resume.clear()
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.api._on_closing() is False
    assert rig.api.record_stop() == {}
    assert rig.tail.wait(1)
    assert rig.api._on_closing() is False
    assert rig.api._gate.state()[0] == "power"
    assert rig.finish()["state"] == "done"
    assert rig.api._on_closing() is True


def test_stop_idle_enter_load_single_use_grant_and_old_generation(rig, viewer):
    initial = viewer.analysis_enter()["gen"]
    assert viewer.record_load(initial)["code"] == "no_recording"
    assert viewer.analysis_leave(initial)["ok"]
    wait_for(lambda: viewer._gate.state()[0] == "idle")
    rig.resume.clear()
    assert rig.start() == {}
    assert rig.entered.wait(1)
    # No grant while the backend is active or while its tail is incomplete.
    assert viewer.record_load(initial)["code"] == "stale"
    assert viewer.record_stop() == {}
    assert rig.tail.wait(1)
    assert viewer._record_last_path is None
    assert viewer.record_load(initial)["code"] == "stale"
    assert rig.finish()["state"] == "done"
    gen = viewer.analysis_enter()["gen"]
    assert gen > initial
    for stale in (initial, True, str(gen), gen + 1):
        assert viewer.record_load(stale)["code"] == "stale"
    first, second = viewer.record_load(gen), viewer.record_load(gen)
    assert set(first) == {"ok", "gen", "grant", "name"}
    assert first["ok"] and first["name"] == rig.path.name
    assert first["grant"] != second["grant"]
    assert viewer._gate.consume_grant(first["grant"], gen, "open", "dlog") == str(rig.path)
    assert viewer._gate.consume_grant(first["grant"], gen, "open", "dlog") is None
    assert viewer._gate.consume_grant(second["grant"], gen, "open", "dlog") == str(rig.path)
    assert viewer.analysis_leave(gen)["ok"]
    wait_for(lambda: viewer._gate.state()[0] == "idle")
    new_gen = viewer.analysis_enter()["gen"]
    assert viewer.record_load(gen)["code"] == "stale"
    assert viewer.record_load(new_gen)["ok"]


def test_new_accepted_run_clears_previous_completed_path_even_on_failure(rig, viewer):
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.finish()["state"] == "done"
    assert viewer._record_last_path == rig.path
    rig.entered.clear()
    rig.run_error = RuntimeError("second run failed")
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert viewer._record_last_path is None
    assert rig.finish()["state"] == "failed"
    gen = viewer.analysis_enter()["gen"]
    # The first run's existing file is not evidence that this run finalized.
    assert rig.path.exists()
    assert viewer.record_load(gen)["code"] == "no_recording"


def test_load_issue_grant_race_returns_stale(rig, viewer, monkeypatch):
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.finish()["state"] == "done"
    gen = viewer.analysis_enter()["gen"]
    monkeypatch.setattr(viewer._gate, "issue_grant", lambda *args: None)
    assert viewer.record_load(gen)["code"] == "stale"


def test_finishing_stuck_enters_fault_late_release_cannot_publish(rig, viewer, monkeypatch):
    assert ac.POWER_FINISH_TIMEOUT_S == 60
    monkeypatch.setattr(ac, "POWER_FINISH_TIMEOUT_S", 0.08)
    rig.resume.clear()
    assert rig.start() == {}
    assert rig.entered.wait(1)
    token = viewer._gate._owner
    # Stop is a cross-thread flag; the backend must enter finishing first.
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(viewer.record_stop).result(timeout=0.2) == {}
    assert rig.tail.wait(1)
    wait_for(lambda: viewer._gate.state()[0] == "fault")
    assert rig.start()["code"] == "fault"
    assert viewer._record_last_path is None
    status = rig.finish()
    assert status["state"] == "failed" and status["code"] == "stuck"
    assert viewer._gate.state()[0] == "fault"
    assert viewer._record_last_path is None
    assert not viewer._gate.release(token)
    assert viewer._gate.state()[0] == "fault"
    assert not any(event[0] == "record" and event[1]["state"] == "done" for event in viewer.poll())


def test_fault_wins_over_original_error_and_keeps_cause_in_notes(rig, monkeypatch):
    monkeypatch.setattr(ac, "POWER_FINISH_TIMEOUT_S", 0.08)
    rig.resume.clear()
    rig.run_error = RuntimeError("original download failure")
    assert rig.start() == {}
    assert rig.entered.wait(1)
    rig.api.record_stop()
    assert rig.tail.wait(1)
    wait_for(lambda: rig.api._gate.state()[0] == "fault")
    assert rig.finish()["code"] == "stuck"
    assert rig.api._record_exception is rig.run_error
    assert any("original download failure" in note for note in rig.api._record_notes)


def test_close_inherits_finish_deadline_without_rearming(rig, monkeypatch):
    # A controlled timer proves close does not get a fresh 60-second budget.
    timers = []
    monkeypatch.setattr(rig.api._gate, "_timer", lambda seconds, callback: timers.append(callback))
    closing, resume = threading.Event(), threading.Event()
    assert rig.start() == {}
    assert rig.entered.wait(1)
    def close():
        closing.set()
        assert resume.wait(2)
    rig.inst.close = close
    rig.api.record_stop()
    assert closing.wait(1)
    assert len(timers) == 3  # worker setup, backend setup, backend cleanup
    timers[-1]()
    assert rig.api._gate.state()[0] == "fault"
    resume.set()
    assert rig.finish()["code"] == "stuck"


def test_stop_does_not_start_timer_before_backend_observes_stop(rig, monkeypatch):
    timers = []
    monkeypatch.setattr(rig.api._gate, "_timer", lambda seconds, callback: timers.append(callback))
    rig.before_stop.clear()
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.api.record_stop() == {}
    assert len(timers) == 2
    # Capture is untimed: both earlier configuration timers are invalidated.
    for callback in list(timers):
        callback()
    assert rig.api._gate.state()[0] == "power"
    assert rig.finish()["state"] == "done"


def test_start_rejected_when_closing(rig):
    assert rig.api._on_closing() is True
    assert rig.start()["code"] == rc.RecordCode.CLOSING.value
    assert not rig.calls


def test_faulted_untimed_skips_capture_and_cannot_become_done(rig, monkeypatch):
    timers = []
    monkeypatch.setattr(rig.api._gate, "_timer", lambda seconds, callback: timers.append(callback))
    untimed = rig.api._untimed
    def expired_before_capture(token):
        timers[-1]()
        return untimed(token)
    monkeypatch.setattr(rig.api, "_untimed", expired_before_capture)
    assert rig.start() == {}
    assert rig.finish()["code"] == "stuck"
    assert not rig.entered.is_set()
    assert not rig.path.exists()
    assert any(call[0] == "close" for call in rig.calls)


def test_worker_exit_cannot_grant_an_existing_destination(rig, viewer):
    rig.path.write_bytes(b'OLD FILE')
    rig.run_error = SystemExit('worker exited')
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.finish()['state'] == 'failed'
    assert rig.path.read_bytes() == b'OLD FILE'
    assert viewer._gate.state()[0] == 'idle'
    gen = viewer.analysis_enter()['gen']
    assert viewer.record_load(gen)['code'] == 'no_recording'
