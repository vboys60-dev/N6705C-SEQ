"""Recording bridge checks with a worker-owned stub instrument and backend."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import inspect
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest
import webview
import numpy as np

from n6705c_seq import e36441a, i18n, web_record, webgui
from n6705c_seq.record import contracts as c


@pytest.mark.parametrize("code,expected", [
    (323, c.RecordCode.BAD_CONFIG),
    (310, c.RecordCode.ELOG_UNSUPPORTED),
    (-113, c.RecordCode.FETCH),
])
def test_failure_code_maps_core_scpi_errors(code, expected):
    from n6705c.core.session import ScpiError

    exc = ScpiError(code, "instrument-reported error", "INIT:ELOG")
    assert web_record._failure_code(exc, "fetch") is expected


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.002)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    old_lang = i18n.lang
    api = webgui.Api(tmp_path / "config.json")
    target = tmp_path / "capture.dlog"
    r = SimpleNamespace(api=api, path=target, calls=[], backends=[],
                        entered=threading.Event(), tail=threading.Event(), resume=threading.Event(),
                        before_stop=threading.Event(),
                        run_error=None, close_error=None, factory_error=None, connect_error=None,
                        records=3, notes=[], model="N6705C", dialog=None, confirmations=[])
    r.resume.set()
    r.before_stop.set()
    r.cfg = {"machine_index": 0, "channels": [1], "sense_volt": True, "sense_curr": True,
             "period_s": 0.001, "duration_s": 3600}

    class Window:
        def create_file_dialog(self, kind, **kwargs):
            # Keep the stub honest about the installed pywebview signature.
            inspect.signature(webview.Window.create_file_dialog).bind(None, kind, **kwargs)
            r.calls.append(("dialog", threading.get_ident(), kind, kwargs))
            if r.dialog:
                return r.dialog()
            return [str(target)]

        def create_confirmation_dialog(self, title, text):
            r.confirmations.append((title, text))
            return True

    def connect(resources=None, **kwargs):
        r.calls.append(("connect", threading.get_ident(), resources, kwargs))
        if r.connect_error:
            raise r.connect_error
        def close():
            r.calls.append(("close", threading.get_ident()))
            if r.close_error:
                raise r.close_error
        r.inst = SimpleNamespace(identity=SimpleNamespace(model=r.model), close=close)
        return SimpleNamespace(instruments=[r.inst])

    class Backend:
        preview = False

        def __init__(self, inst, cfg, path, *, untimed, finishing, target_state=None):
            r.calls.append(("factory", threading.get_ident()))
            self.cfg, self.path = cfg, path
            self.target_state = target_state
            self.untimed, self.finishing = untimed, finishing
            if r.factory_error:
                raise r.factory_error
            r.backends.append(self)

        def run(self, stop, emit):
            r.calls.append(("run", threading.get_ident()))
            self.finishing()
            try:
                if not self.untimed():
                    raise RuntimeError("record owner is no longer active")
                emit({"run_id": self.cfg["run_id"], "state": "running", "elapsed_s": 0.1,
                      "records": 0, "gaps": 0, "preview": False, "path": self.path})
                r.entered.set()
                assert r.before_stop.wait(3)
                assert stop.wait(3)
            finally:
                self.finishing()
            emit({"run_id": self.cfg["run_id"], "state": "stopping", "elapsed_s": 0.2,
                  "records": r.records, "gaps": 0, "preview": False})
            r.tail.set()
            assert r.resume.wait(3)
            if r.run_error:
                raise r.run_error
            self.path.write_bytes(b"stub finalized data")
            return {"run_id": self.cfg["run_id"], "final_path": self.path, "elapsed_s": 0.2,
                    "records": r.records, "gaps": 0, "preview": False, "notes": list(r.notes)}

        def snapshot(self, since_seq):
            r.calls.append(("snapshot", threading.get_ident(), since_seq))
            if since_seq == 1:
                return {"unchanged": True, "seq": 1}
            return {"run_id": self.cfg["run_id"], "seq": 1, "preview": False, "period_s": 0.001,
                    "keys": {"V1": {"t0": 0, "dt": 0.001, "min": [1.0], "max": [2.0]}}}

    module = ModuleType("n6705c_seq.record.backend")
    module.make_backend = Backend
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(c, "RECORD_ENABLED", True)
    monkeypatch.setattr(web_record, "connect_group", connect)
    monkeypatch.setattr(e36441a, "detect_model", lambda resource: r.model)
    monkeypatch.setattr(web_record.InstrumentGroup, "demo", connect)
    api._window = Window()
    r.start = lambda **kw: api.record_start(kw.get("config", r.cfg), kw.get("resources", []), kw.get("demo", True))

    def finish():
        api.record_stop()
        r.before_stop.set()
        r.resume.set()
        if api._record_thread:
            api._record_thread.join(3)
            assert not api._record_thread.is_alive()
        return api.record_status()
    r.finish = finish
    yield r
    finish()
    i18n.set_lang(old_lang)


def test_readonly_defaults_and_flag_precedence(rig, monkeypatch):
    api = rig.api
    assert api.record_status() == {"run_id": "", "state": "idle", "elapsed_s": 0,
                                   "records": 0, "gaps": 0, "preview": False}
    assert api.record_snapshot(-1) == {"run_id": "", "seq": 0, "preview": False, "period_s": 0, "keys": {}}
    meta = api.record_meta()
    assert set(meta) == set(c.RecordMeta.__annotations__)
    assert meta["period_floor_table"] == {str(k): v for k, v in c.PERIOD_FLOOR_TABLE.items()}
    meta["period_floor_table"]["1"] = -1
    assert api.record_meta()["period_floor_table"]["1"] > 0
    monkeypatch.setattr(c, "RECORD_ENABLED", False)
    assert api.record_meta()["enabled"] is False
    assert api.record_start(None, None, None)["code"] == "disabled"
    assert api.record_stop()["code"] == "no_recording"
    assert not rig.calls


@pytest.mark.parametrize("change", [
    {"unknown": 1}, {"run_id": "injected"}, {"machine_index": True}, {"machine_index": 2},
    {"channels": []}, {"channels": [1, 1]}, {"channels": [0]}, {"channels": [5]},
    {"channels": [True]}, {"channels": (1,)}, {"sense_volt": 1}, {"sense_curr": "true"},
    {"sense_volt": False, "sense_curr": False}, {"period_s": True}, {"period_s": float("nan")},
    {"period_s": float("inf")}, {"period_s": 0.0001}, {"period_s": 61},
    {"duration_s": False}, {"duration_s": float("nan")}, {"duration_s": 0.9},
    {"duration_s": c.DURATION_MAX_S + 1},
])
def test_config_rejected_before_reservation_or_dialog(rig, change):
    assert rig.start(config={**rig.cfg, **change})["code"] == "bad_config"
    assert not rig.calls
    assert rig.api._gate.state()[0] == "idle"


@pytest.mark.parametrize("config,resources,demo", [
    ({}, [], True), ([], [], True), ("default", [], True),
    (None, [], 1), (None, "address", False), (None, [None], False),
    (None, [], False), (None, ["  "], False),
])
def test_invalid_arguments(rig, config, resources, demo):
    assert rig.start(config=config if config is not None else rig.cfg,
                     resources=resources, demo=demo)["code"] == "bad_config"
    assert not rig.calls


def test_worker_owns_connection_factory_run_close_and_copies_inputs(rig):
    rig.cfg.update(machine_index=1, channels=[4, 2])
    resources = ["DO NOT CONNECT", " selected "]
    caller = threading.get_ident()
    assert rig.start(resources=resources, demo=False) == {}
    assert rig.entered.wait(1)
    resources[1] = "changed"
    rig.cfg["channels"].append(3)
    cfg = rig.backends[0].cfg
    assert cfg["channels"] == [2, 4]
    assert cfg["run_id"] and "run_id" not in rig.cfg
    assert rig.api.record_status()["run_id"] == cfg["run_id"]
    assert "path" not in rig.api.record_status()
    rig.notes = ["test note"]
    assert rig.finish()["state"] == "done"
    io = [call for call in rig.calls if call[0] in {"connect", "factory", "run", "close"}]
    assert len({call[1] for call in io}) == 1
    assert io[0][1] != caller
    assert io[0][2] == ["selected"]
    events = rig.api.poll()
    states = [event[1]["state"] for event in events if event[0] == "record"]
    assert states == ["starting", "running", "stopping", "done"]
    idle = max(i for i, event in enumerate(events) if event[0] == "state" and event[1]["state"] == "idle")
    done = next(i for i, event in enumerate(events) if event[0] == "record" and event[1]["state"] == "done")
    assert idle < done
    assert any(event[0] == "result" and "test note" in event[-1] for event in events)
    assert str(rig.path) not in str(events)


def test_dialog_reserves_without_timer_and_cancel_releases(rig, monkeypatch):
    monkeypatch.setattr(rig.api, "_finishing", lambda token: pytest.fail("dialog must be untimed"))
    def cancel():
        assert rig.api._gate.state()[0] == "power"
        assert rig.api._gate._finish_epoch == 0
        assert rig.start()["code"] == "power"
        return None
    rig.dialog = cancel
    assert rig.start() == {"cancelled": True}
    assert rig.api._gate.state()[0] == "idle"
    assert not rig.backends


def test_dialog_lock_busy_and_exception_release(rig):
    rig.api._dialog_lock.acquire()
    try:
        assert rig.start() == {"cancelled": True}
    finally:
        rig.api._dialog_lock.release()
    def broken():
        raise OSError("dialog unavailable")
    rig.dialog = broken
    assert rig.start()["code"] == "connect"
    assert rig.api._gate.state()[0] == "idle"
    assert rig.api._dialog_lock.acquire(blocking=False)
    rig.api._dialog_lock.release()


@pytest.mark.parametrize("suffix", ["", ".txt", ".DLOG"])
def test_suffix_and_confirmation_of_data_and_sidecar(rig, suffix):
    selected = rig.path.with_suffix(suffix)
    rig.dialog = lambda: str(selected)
    expected = selected.with_suffix(".dlog") if suffix.lower() == ".dlog" else Path(str(selected) + ".dlog")
    expected.write_bytes(b"old")
    Path(str(expected) + ".json").write_text("old sidecar")
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.backends[0].path == expected
    assert len(rig.confirmations) == 2
    assert rig.finish()["state"] == "done"


@pytest.mark.parametrize("suffix", ["", ".json"])
def test_overwrite_declined_preserves_existing_files(rig, suffix):
    existing = Path(str(rig.path) + suffix)
    existing.write_bytes(b"keep")
    rig.api._window.create_confirmation_dialog = lambda *args: False
    assert rig.start() == {"cancelled": True}
    assert existing.read_bytes() == b"keep"
    assert rig.api._gate.state()[0] == "idle"


@pytest.mark.parametrize("suffix", [".part", ".json.part"])
def test_existing_part_is_never_overwritten(rig, suffix):
    part = Path(str(rig.path) + suffix)
    part.write_bytes(b"keep partial")
    result = rig.start()
    assert result["code"] == "sink"
    assert str(rig.path) not in result["error"]
    assert part.read_bytes() == b"keep partial"
    assert rig.api._gate.state()[0] == "idle"


def test_stop_is_only_a_flag_and_works_when_feature_disabled(rig, monkeypatch):
    rig.resume.clear()
    assert rig.start() == {}
    assert rig.entered.wait(1)
    monkeypatch.setattr(c, "RECORD_ENABLED", False)
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(rig.api.record_stop).result(timeout=0.2) == {}
    assert rig.tail.wait(1)
    assert rig.api.record_status()["state"] == "stopping"
    assert rig.api._gate.state()[0] == "power"
    assert rig.api.record_stop() == {}
    assert not any(call[0] == "close" for call in rig.calls)
    assert rig.finish()["state"] == "done"
    assert rig.api.record_stop()["code"] == "no_recording"


def test_snapshots_are_memory_only_and_status_is_detached(rig, monkeypatch):
    assert rig.start() == {}
    assert rig.entered.wait(1)
    def forbidden(*args, **kwargs):
        pytest.fail("read-only API performed I/O")
    with monkeypatch.context() as patch:
        patch.setattr(web_record, "connect_group", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(Path, "stat", forbidden)
        snap = rig.api.record_snapshot(-1)
        assert snap == rig.api.record_snapshot(1000)
        snap["keys"]["V1"]["min"][0] = 99
        assert rig.api.record_snapshot(-1)["keys"]["V1"]["min"] == [1.0]
        assert rig.api.record_snapshot(1) == {"unchanged": True, "seq": 1}
        status = rig.api.record_status()
        status["state"] = "changed"
        assert rig.api.record_status()["state"] == "running"
    assert rig.finish()["state"] == "done"
    assert rig.api.record_snapshot(-1)["keys"]


@pytest.mark.parametrize("value,exception", [(True, TypeError), (1.0, TypeError), (None, TypeError), (-2, ValueError)])
def test_invalid_snapshot_cursor(rig, value, exception):
    with pytest.raises(exception):
        rig.api.record_snapshot(value)


@pytest.mark.parametrize("where,exc,code", [
    ("connect_error", RuntimeError("connection failed"), "connect"),
    ("factory_error", ValueError("invalid configuration"), "bad_config"),
    ("run_error", RuntimeError('+323,"invalid period"'), "bad_config"),
    ("run_error", RuntimeError('+310,"unsupported ELOG"'), "elog_unsupported"),
    ("run_error", RuntimeError("download failed"), "fetch"),
    ("run_error", ValueError("malformed fetched data"), "fetch"),
    ("run_error", OSError(28, "disk full"), "sink"),
    ("close_error", RuntimeError("close failed"), "fetch"),
])
def test_worker_errors_close_release_and_never_publish(rig, where, exc, code):
    setattr(rig, where, exc)
    assert rig.start() == {}
    if where not in {"connect_error", "factory_error"}:
        assert rig.entered.wait(1)
    status = rig.finish()
    assert status["state"] == "failed" and status["code"] == code
    assert rig.api._record_last_path is None
    assert rig.api._record_exception is exc
    assert rig.api._gate.state()[0] == "idle"
    if where != "connect_error":
        assert any(call[0] == "close" for call in rig.calls)


def test_core_error_types_keep_their_cause(rig):
    from n6705c.core.elog import ElogFetchError, ElogSinkError
    cause = OSError("original failure")
    for error_type, code in ((ElogFetchError, "fetch"), (ElogSinkError, "sink")):
        exc = error_type("wrapped failure")
        exc.__cause__ = cause
        rig.run_error = exc
        assert rig.start() == {}
        assert rig.entered.wait(1)
        assert rig.finish()["code"] == code
        assert rig.api._record_exception.__cause__ is cause
        rig.entered.clear()


def test_reject_e36441a_and_zero_record_result(rig):
    rig.model = "E36441A"
    assert rig.start() == {}
    assert rig.finish()["code"] == "bad_config"
    assert not rig.backends
    rig.model, rig.records = "N6705C", 0
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.finish()["code"] == "no_recording"
    assert rig.api._record_last_path is None


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_start_failure_releases_and_preserves_terminal_order(rig, monkeypatch, phase):
    def fail_start(*args):
        raise RuntimeError("cannot start worker")
    if phase == "start":
        monkeypatch.setattr(threading.Thread, "start", fail_start)
    else:
        monkeypatch.setattr(threading, "Thread", fail_start)
    assert rig.start()["code"] == "connect"
    assert rig.api._gate.state()[0] == "idle"
    assert rig.api.record_status()["state"] == "failed"
    assert rig.api.record_stop()["code"] == "no_recording"
    events = rig.api.poll()
    assert events[-1][0] == "record" and events[-2] == ["busy", False]
    assert rig.api._record_thread is None


def test_only_one_concurrent_start_opens_a_dialog(rig):
    with ThreadPoolExecutor(8) as pool:
        replies = list(pool.map(lambda _: rig.start(), range(8)))
    assert replies.count({}) == 1
    assert sum(reply.get("code") == "power" for reply in replies) == 7
    assert sum(call[0] == "dialog" for call in rig.calls) == 1
    assert rig.entered.wait(1)
    assert rig.finish()["state"] == "done"


def test_demo_uses_one_fake_even_with_second_machine_index(rig):
    rig.cfg["machine_index"] = 1
    assert rig.start() == {}
    assert rig.entered.wait(1)
    connect = next(call for call in rig.calls if call[0] == "connect")
    assert connect[2] is None and connect[3]["frames"] == 1
    assert rig.finish()["state"] == "done"


def test_disk_error_does_not_expose_absolute_paths(rig):
    rig.run_error = OSError(28, "disk full", str(rig.path) + ".json.part")
    assert rig.start() == {}
    assert rig.entered.wait(1)
    status = rig.finish()
    assert status["code"] == "sink"
    assert str(rig.path.parent) not in status["error"]
    assert str(rig.path.parent) not in str(rig.api.poll())


def test_cancel_keeps_previous_terminal_and_completed_file(rig):
    assert rig.start() == {}
    assert rig.entered.wait(1)
    terminal = rig.finish()
    rig.dialog = lambda: None
    assert rig.start() == {"cancelled": True}
    assert rig.api.record_status() == terminal
    assert rig.api._record_last_path == rig.path


def test_garbled_identity_model_still_completes(rig):
    rig.model = '\u00ff\x01\u00feinvalid-model'
    assert rig.start(resources=[' selected '], demo=False) == {}
    assert rig.entered.wait(1)
    assert rig.finish()['state'] == 'done'


def test_base_exception_never_publishes_existing_file(rig, monkeypatch):
    rig.path.write_bytes(b'OLD FILE')
    backend = sys.modules['n6705c_seq.record.backend']
    class ExitingBackend:
        def __init__(self, *args, **kwargs):
            pass
        def run(self, *args):
            raise SystemExit('worker exited')
    monkeypatch.setattr(backend, 'make_backend', ExitingBackend)
    cfg, resources, demo = web_record._config_copy(rig.cfg, [], True)
    token = rig.api._reserve('power')
    rig.api._record_state.update(run_id=cfg['run_id'], state='starting')
    # Call synchronously so a regression does not produce a thread warning.
    try:
        rig.api._record_work(cfg, resources, demo, rig.path, threading.Event(), token)
    except SystemExit:
        pass
    assert rig.path.read_bytes() == b'OLD FILE'
    assert rig.api.record_status()['state'] == 'failed'
    assert rig.api._record_last_path is None
    assert rig.api._gate.state()[0] == 'idle'


@pytest.mark.parametrize('bad_result', [None, [], {},
    {'run_id': 'RUN', 'final_path': 'PATH', 'elapsed_s': 1, 'records': 3, 'gaps': 0, 'preview': False}])
def test_invalid_backend_result_fails_and_releases(rig, monkeypatch, bad_result):
    backend = sys.modules['n6705c_seq.record.backend'].make_backend
    def run(self, stop, emit):
        if isinstance(bad_result, dict):
            return {**bad_result, 'run_id': self.cfg['run_id'], 'final_path': self.path}
        return bad_result
    monkeypatch.setattr(backend, 'run', run)
    assert rig.start() == {}
    status = rig.finish()
    assert status['state'] == 'failed'
    assert rig.api._record_last_path is None
    assert rig.api._record_exception is not None
    assert rig.api._gate.state()[0] == 'idle'
    assert rig.api.record_stop()['code'] == 'no_recording'


@pytest.mark.parametrize('exc,code', [
    (OSError(28, 'disk full', '/data/run323.dlog.part'), 'sink'),
    (RuntimeError('fetch timed out after 310 ms'), 'fetch'),
    (RuntimeError('fetch 323 samples failed'), 'fetch'),
    (RuntimeError('SCPI: +323,"bad period"'), 'bad_config'),
    (RuntimeError('-310,"unsupported"'), 'elog_unsupported'),
    (RuntimeError('1310,"other"'), 'fetch'),
    (RuntimeError('run310,"filename"'), 'fetch'),
])
def test_scpi_error_classification_is_not_a_number_search(exc, code):
    assert web_record._failure_code(exc, 'fetch').value == code


def test_live_e36441a_is_detected_before_connect_group(rig, monkeypatch):
    probes = []
    def detect(resource):
        probes.append(resource)
        return 'E36441A'
    monkeypatch.setattr(e36441a, 'detect_model', detect)
    rig.connect_error = RuntimeError('N6705C connect must not be attempted')
    rig.cfg['machine_index'] = 1
    assert rig.start(resources=['not selected', ' selected '], demo=False) == {}
    assert rig.finish()['code'] == 'bad_config'
    assert probes == ['selected']
    assert not any(call[0] == 'connect' for call in rig.calls)


def test_demo_does_not_probe_model(rig, monkeypatch):
    monkeypatch.setattr(e36441a, 'detect_model', lambda *args: pytest.fail('demo must not probe'))
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.finish()['state'] == 'done'


@pytest.mark.parametrize('terminal', ['done', 'failed'])
def test_late_emit_cannot_revive_terminal_state(rig, terminal):
    rig.api._record_state.update(run_id='finished', state=terminal)
    before = rig.api.record_status()
    rig.api._record_emit({'state': 'running', 'records': 99}, 'finished')
    assert rig.api.record_status() == before
    assert not any(event[0] == 'record' for event in rig.api.poll())


def test_status_and_events_use_python_scalars_for_numpy_input(rig, monkeypatch):
    backend = sys.modules['n6705c_seq.record.backend'].make_backend
    original = backend.run
    values = {'elapsed_s': np.float32(0.25), 'records': np.int64(3),
              'gaps': np.int32(2), 'preview': np.bool_(True)}
    def run(self, stop, emit):
        emit({'state': 'running', **values})
        return {**original(self, stop, emit), **values}
    monkeypatch.setattr(backend, 'run', run)
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.finish()['state'] == 'done'
    for event in rig.api.poll():
        if event[0] != 'record':
            continue
        for key, expected_type in [('elapsed_s', (int, float)), ('records', (int,)),
                                   ('gaps', (int,)), ('preview', (bool,))]:
            assert type(event[1][key]) in expected_type


def test_recursive_invalid_config_is_rejected_without_deepcopy(rig):
    nested = []
    for _ in range(2000):
        nested = [nested]
    assert rig.start(config={**rig.cfg, 'channels': nested})['code'] == 'bad_config'
    assert not rig.calls


@pytest.mark.parametrize('kind', ['directory', 'dangling_symlink', 'directory_symlink'])
def test_invalid_final_path_is_sink_before_worker(rig, kind):
    if kind == 'directory':
        rig.path.mkdir()
    elif kind == 'directory_symlink':
        rig.path.symlink_to(rig.path.parent, target_is_directory=True)
    else:
        rig.path.symlink_to(rig.path.parent / 'missing')
    assert rig.start()['code'] == 'sink'
    assert not rig.backends
    assert not rig.confirmations
    assert rig.api._gate.state()[0] == 'idle'


@pytest.mark.parametrize('missing', ['elapsed_s', 'records', 'gaps', 'notes', 'preview'])
def test_missing_result_field_cannot_leave_state_stopping(rig, monkeypatch, missing):
    backend = sys.modules['n6705c_seq.record.backend'].make_backend
    original = backend.run
    def run(self, stop, emit):
        result = original(self, stop, emit)
        del result[missing]
        return result
    monkeypatch.setattr(backend, 'run', run)
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.finish()['state'] == 'failed'
    assert rig.api._record_last_path is None
    assert rig.api._record_exception is not None
    assert rig.api._gate.state()[0] == 'idle'


@pytest.mark.parametrize('suffix', ['.DLOG', '.DlOg'])
def test_dialog_extension_is_normalized_before_worker(rig, suffix):
    rig.dialog = lambda: [str(rig.path.with_suffix(suffix))]
    def confirm(title, text):
        assert not any(call[0] == 'connect' for call in rig.calls)
        return True
    rig.path.write_bytes(b'old')
    rig.api._window.create_confirmation_dialog = confirm
    assert rig.start() == {}
    assert rig.entered.wait(1)
    assert rig.backends[0].path == rig.path
    assert rig.backends[0].target_state[rig.path].st_size == 3
    assert rig.finish()['state'] == 'done'


def test_target_snapshot_is_taken_after_confirmation_before_connect(rig):
    rig.path.write_bytes(b'old')
    def confirm(title, text):
        assert not any(call[0] == 'connect' for call in rig.calls)
        rig.path.write_bytes(b'confirmed version')
        return True
    rig.api._window.create_confirmation_dialog = confirm
    assert rig.start() == {}
    assert rig.entered.wait(1)
    targets = rig.backends[0].target_state
    assert targets[rig.path].st_size == len(b'confirmed version')
    assert targets[Path(str(rig.path) + '.json')] is None
    assert rig.finish()['state'] == 'done'
