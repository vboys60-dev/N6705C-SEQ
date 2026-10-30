"""Shell integration using a fake analysis server; no HTTP or window needed."""

import importlib.machinery
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest
import webview

from n6705c_seq import form, i18n, webgui, web_seq, web_list, list_form
from n6705c_seq.analysis import contracts as c
from n6705c_seq.listplan import ChannelList, ListPlan, Segment
from test_activity_gate import wait_for


class Server:
    def __init__(self):
        self.released = 0
        self.shutdowns = []
        self.check = True
        self.checks = 0

    def url(self, gen, lang):
        return f"http://127.0.0.1:123/viewer.html#t=secret&gen={gen}&lang={lang}"

    def release_resources(self):
        self.released += 1
        return True

    def shutdown(self, timeout):
        self.shutdowns.append(timeout)

    def self_check(self):
        self.checks += 1
        return self.check


@pytest.fixture
def api(tmp_path, monkeypatch):
    original = i18n.lang
    value = webgui.Api(tmp_path / "config.json")
    server = Server()
    module = ModuleType("n6705c_seq.analysis.server")
    module.__spec__ = importlib.machinery.ModuleSpec(module.__name__, loader=None)
    starts = []
    def start(gate):
        assert gate is value._gate
        starts.append(True)
        return server
    module.start_server = start
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(c, "ANALYSIS_ENABLED", True)
    destroyed = threading.Event()
    value._window = SimpleNamespace(create_confirmation_dialog=lambda *a: True,
        create_file_dialog=lambda *a, **kw: None, destroy=destroyed.set)
    value._fake_server = server
    value._starts = starts
    value._destroyed = destroyed
    yield value
    i18n.set_lang(original)


def test_flag_config_i18n_and_missing_server(api, monkeypatch):
    assert api.config()[c.CONFIG_KEY_ANALYSIS] is True
    assert all(i18n.STRINGS[k] == pair for k, pair in c.I18N_KEYS.items())
    monkeypatch.setattr(c, "ANALYSIS_ENABLED", False)
    assert api.analysis_enter()["code"] == "disabled"
    assert api._starts == []
    assert not api.config()[c.CONFIG_KEY_ANALYSIS]
    token = api._reserve("power")
    assert token and api._release(token)
    monkeypatch.setattr(c, "ANALYSIS_ENABLED", True)
    monkeypatch.setattr(webgui.importlib.util, "find_spec", lambda name: None)
    assert api.analysis_enter()["code"] == "server"
    assert api._gate.state() == (c.State.IDLE, 0)


def test_enter_start_failure_idempotence_and_admission(api, monkeypatch):
    start = sys.modules["n6705c_seq.analysis.server"].start_server
    def fail(gate):
        raise OSError("cannot bind")
    monkeypatch.setattr(sys.modules["n6705c_seq.analysis.server"], "start_server", fail)
    assert "cannot bind" in api.analysis_enter()["error"]
    assert api._gate.state() == (c.State.IDLE, 0)
    monkeypatch.setattr(sys.modules["n6705c_seq.analysis.server"], "start_server", start)
    response = api.analysis_enter()
    assert response["ok"] and response["gen"] == 1
    assert "&gen=1&lang=" in response["url"]
    assert api.analysis_enter() == response
    assert api._starts == [True]
    assert api.seq_run(form.default_raw(1), [], True, False, False)["error"] == i18n.tr("power_denied_analysis")
    assert api.test_connection([], True)["error"] == i18n.tr("power_denied_analysis")
    assert api.all_off(["x"], False)["error"] == i18n.tr("power_denied_analysis")
    plan = list_form.plan_to_dict(ListPlan([ChannelList(1, segments=[Segment("step", {
        "start": 0, "end": 1, "delay": 0.01, "end_time": 0.01})])]))
    settings = {"channel": 1, "enabled": True, "label": "", "delay": "0", "limit": "",
        "repeat": "1", "hold_last": False, "dwell_mode": "auto", "dwell": "0.00001024"}
    assert api.list_run(plan, settings, [], True, False)["error"] == i18n.tr("power_denied_analysis")
    assert api.analysis_leave(1) == {"ok": True, "gen": 1}
    wait_for(lambda: api._gate.state()[0] == c.State.IDLE)
    assert api.analysis_enter()["gen"] == 2
    assert api._starts == [True]


@pytest.mark.parametrize("operation", ["seq", "list", "off"])
def test_confirmation_holds_power_against_analysis(api, monkeypatch, operation):
    entered, proceed = threading.Event(), threading.Event()
    def confirm(*args):
        entered.set()
        assert proceed.wait(2)
        return False
    api._window.create_confirmation_dialog = confirm
    def run():
        if operation == "seq":
            return api.seq_run(form.default_raw(1), [], True, False, False)
        if operation == "off":
            return api.all_off(["x"], False)
        plan = list_form.plan_to_dict(ListPlan([ChannelList(1, segments=[Segment("step", {
            "start": 0, "end": 1, "delay": 0.01, "end_time": 0.01})])]))
        settings = {"channel": 1, "enabled": True, "label": "", "delay": "0", "limit": "",
            "repeat": "1", "hold_last": False, "dwell_mode": "auto", "dwell": "0.00001024"}
        return api.list_run(plan, settings, [], True, False)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run)
        try:
            assert entered.wait(1)
            assert api.analysis_enter()["code"] == "power"
            assert api._on_closing() is False
        finally:
            proceed.set()
        assert future.result() == {"cancelled": True}
    assert api._gate.state()[0] == c.State.IDLE
    assert api._starts == []


@pytest.mark.parametrize("save,purpose,ext", [(False, "dlog", ""), (False, "markers", ""),
    (True, "png", ".png"), (True, "html", ".html"), (True, "markers", ".json"), (True, "csv", ".csv")])
def test_native_path_grants(api, save, purpose, ext):
    gen = api.analysis_enter()["gen"]
    calls = []
    def dialog(kind, **kw):
        calls.append((kind, kw))
        return ("/private/sensitive/report.other",)
    api._window.create_file_dialog = dialog
    result = (api.analysis_save_path(gen, purpose, "report") if save
              else api.analysis_open_path(gen, purpose))
    assert result["ok"] and result["name"] == "report.other" + ext
    assert "/private" not in repr(result)
    assert api._gate.consume_grant(result["grant"], gen, "save" if save else "open", purpose) == "/private/sensitive/report.other" + ext
    if save:
        assert calls == [(webview.FileDialog.SAVE, {"file_types": c.SAVE_FILE_TYPES[purpose], "save_filename": "report"})]
    else:
        assert calls == [(webview.FileDialog.OPEN, {"file_types": c.OPEN_FILE_TYPES[purpose]})]


def test_path_validation_cancel_and_case_extension(api):
    gen = api.analysis_enter()["gen"]
    for invalid in (None, [], {}, True, "unknown"):
        assert api.analysis_open_path(gen, invalid)["code"] == "bad_purpose"
        assert api.analysis_save_path(gen, invalid, "a")["code"] == "bad_purpose"
    for invalid in (None, [], 12, "x" * 201, "a/b", "a\\b", "a:b"):
        assert api.analysis_save_path(gen, "png", invalid)["code"] == "bad_purpose"
    for invalid in (None, True, "1", [], {}, gen + 1):
        assert api.analysis_leave(invalid)["code"] == "stale"
        assert api.analysis_open_path(invalid, "dlog")["code"] == "stale"
    assert api.analysis_open_path(gen, "dlog") == {"ok": False, "cancelled": True, "gen": gen}
    api._window.create_file_dialog = lambda *a, **kw: "/tmp/REPORT.PNG"
    assert api.analysis_save_path(gen, "png", "report")["name"] == "REPORT.PNG"


@pytest.mark.parametrize("close", [False, True])
def test_late_dialog_returns_no_grant(api, close):
    gen = api.analysis_enter()["gen"]
    started, resume = threading.Event(), threading.Event()
    def dialog(*a, **kw):
        started.set()
        assert resume.wait(2)
        return "/private/path.dlog"
    api._window.create_file_dialog = dialog
    with ThreadPoolExecutor(1) as pool:
        response = pool.submit(api.analysis_open_path, gen, "dlog")
        try:
            assert started.wait(1)
            if close:
                assert api._on_closing() is False
            else:
                api.analysis_leave(gen)
                wait_for(lambda: api._gate.state()[0] == c.State.IDLE)
                assert api.analysis_enter()["gen"] == gen + 1
        finally:
            resume.set()
        assert response.result()["code"] == "stale"


def test_closing_async_waits_jobs_and_destroy_reentry(api):
    gen = api.analysis_enter()["gen"]
    job = api._gate.job_begin(gen)
    before = time.monotonic()
    assert api._on_closing() is False
    assert time.monotonic() - before < 0.2
    assert job.cancel.is_set()
    assert not api._cleanup_done
    assert api._fake_server.released == 0
    assert api._on_closing() is False
    api._gate.job_end(job)
    assert api._destroyed.wait(1)
    assert api._on_closing() is True
    api._shutdown_analysis()
    api._shutdown_analysis()
    assert len(api._fake_server.shutdowns) == 1
    assert api.analysis_enter()["code"] == "closing"
    assert api.analysis_leave(gen)["code"] == "stale"


@pytest.mark.parametrize("stuck", ["job", "resources", "shutdown"])
def test_closing_timeout_exits_even_if_cleanup_stuck(api, monkeypatch, stuck):
    monkeypatch.setattr(c, "CLOSING_CLEANUP_TIMEOUT_S", 0.03)
    gen = api.analysis_enter()["gen"]
    unblock, exited = threading.Event(), threading.Event()
    exits = []
    def exit_process(code):
        exits.append(code)
        exited.set()
    monkeypatch.setattr(webgui.os, "_exit", exit_process)
    if stuck == "job":
        job = api._gate.job_begin(gen)
    elif stuck == "resources":
        api._fake_server.release_resources = lambda: unblock.wait(1)
    else:
        def shutdown(timeout):
            api._fake_server.shutdowns.append(timeout)
            if timeout != 1.0:
                unblock.wait(1)
        api._fake_server.shutdown = shutdown
    try:
        assert api._on_closing() is False
        assert exited.wait(1)
        assert exits == [2]
        assert 1.0 in api._fake_server.shutdowns
        assert api._gate.state()[0] == c.State.CLOSING
    finally:
        unblock.set()
        if stuck == "job":
            api._gate.job_end(job)


@pytest.mark.parametrize("operation", ["test", "off"])
def test_blocked_instrument_call_times_out_and_late_release_keeps_fault(api, monkeypatch, operation):
    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.03)
    entered, resume = threading.Event(), threading.Event()
    def detect(resource):
        entered.set()
        assert resume.wait(2)
        return "E36441A"
    monkeypatch.setattr(web_seq, "detect_model", detect)
    monkeypatch.setattr(web_seq, "off_resources", lambda resources: None)
    token = api._reserve("power")
    worker = threading.Thread(target=api._test if operation == "test" else api._off_work,
        args=(["x"], False, token) if operation == "test" else (["x"], token))
    worker.start()
    try:
        assert entered.wait(1)
        wait_for(lambda: api._gate.state()[0] == c.State.FAULT)
        assert api.analysis_enter()["code"] == "fault"
        assert api.test_connection([], True)["error"] == i18n.tr("fault_body")
        assert any(e[:2] == ["result", "error"] and e[2] == i18n.tr("fault_title") for e in api.poll())
    finally:
        resume.set()
        worker.join(1)
    assert not worker.is_alive()
    assert api._gate.state()[0] == c.State.FAULT
    assert api._gate.worker_done
    assert api._on_closing() is True


@pytest.mark.parametrize("check", [True, False])
def test_selftest_server_callsite(api, monkeypatch, check):
    api._fake_server.check = check
    window = api._window
    window.events = SimpleNamespace(closing=webview.Event(window, should_lock=True))
    monkeypatch.setattr(webgui, "Api", lambda: api)
    monkeypatch.setattr(webgui.sys, "argv", ["webgui", "--selftest"])
    monkeypatch.setattr(webgui.sys, "platform", "darwin")
    monkeypatch.setattr(webview, "screens", [])
    monkeypatch.setattr(webview, "create_window", lambda *a, **kw: window)
    monkeypatch.setattr(webview, "start", lambda callback, **kw: (api.selftest_report(True, ""), callback()))
    def exit_process(code):
        raise SystemExit(code)
    monkeypatch.setattr(webgui.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as result:
        webgui.main()
    assert result.value.code == int(not check)
    assert api._fake_server.checks == 1
    assert api._starts == [True]
    assert api._fake_server.shutdowns == [1.0]


@pytest.fixture
def power_worker(api, monkeypatch):
    """Wire all three main calls to fakes while retaining actual worker cleanup."""
    def setup(kind, main=lambda *a, **kw: None, *, demo=False):
        closed = []
        group = SimpleNamespace(instruments=[SimpleNamespace(close=lambda: closed.append(True))])
        model = "E36441A" if kind == "resources" else "N6705C"
        monkeypatch.setattr(web_seq, "detect_model", lambda r: model)
        monkeypatch.setattr(web_list.e36441a, "detect_model", lambda r: model)
        monkeypatch.setattr(web_seq, "connect_group", lambda r: group)
        monkeypatch.setattr(web_list.execute, "connect_group", lambda r: group)
        monkeypatch.setattr(web_seq.InstrumentGroup, "demo", lambda **kw: group)
        monkeypatch.setattr(web_seq, "run_resources", main)
        monkeypatch.setattr(web_seq, "run_plan", main)
        monkeypatch.setattr(web_list.execute, "run_functions", main)
        monkeypatch.setattr(web_list.synth, "to_functions", lambda *a: {})
        token = api._reserve("power")
        assert token is not None
        def run():
            if kind == "functions":
                api._list_work(SimpleNamespace(hold_last=False), None, 1, ["x"], demo, False, token)
            else:
                api._work([], 1, ["x"], demo, False, False, token)
        return run, token, closed
    return setup


@pytest.mark.parametrize("kind,stage", [
    ("resources", "detect"), ("plan", "detect"), ("plan", "connect"),
    ("functions", "detect"), ("functions", "connect"),
])
def test_worker_frontend_timeout_skips_main_and_cleans_up(api, monkeypatch, power_worker, kind, stage):
    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.03)
    calls = []
    run, token, closed = power_worker(kind, lambda *a, **kw: calls.append(True))
    entered, resume = threading.Event(), threading.Event()
    module = web_list.e36441a if kind == "functions" else web_seq
    name = "detect_model"
    if stage == "connect":
        module = web_list.execute if kind == "functions" else web_seq
        name = "connect_group"
    original = getattr(module, name)
    def blocked(*args):
        entered.set()
        assert resume.wait(2)
        return original(*args)
    monkeypatch.setattr(module, name, blocked)
    releases = []
    release = api._release
    monkeypatch.setattr(api, "_release", lambda t: releases.append(release(t)))
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        wait_for(lambda: api._gate.state()[0] == c.State.FAULT)
        assert api._untimed(token) is False
    finally:
        resume.set()
        worker.join(1)
    assert not worker.is_alive()
    assert calls == []
    # E36441A opens and closes sessions inside run_resources, which was skipped.
    assert closed == ([] if kind == "resources" else [True])
    assert releases == [False] and api._gate.worker_done
    assert api._gate.state()[0] == c.State.FAULT


@pytest.mark.parametrize("kind,demo", [
    ("resources", False), ("plan", False), ("functions", False),
    ("plan", True), ("functions", True),
])
def test_main_work_is_untimed_beyond_timeout(api, monkeypatch, power_worker, kind, demo):
    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.03)
    def main(*args, **kwargs):
        # Let the real initial timer fire while the main call is still running.
        time.sleep(0.09)
        assert api._gate.state()[0] == c.State.POWER
    run, token, closed = power_worker(kind, main, demo=demo)
    run()
    assert api._gate.state()[0] == c.State.IDLE
    assert closed == ([] if kind == "resources" else [True])


@pytest.mark.parametrize("kind", ["resources", "plan", "functions"])
@pytest.mark.parametrize("raises", [False, True])
def test_main_exit_rearms_before_blocked_result_or_except(api, monkeypatch, power_worker, kind, raises):
    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.03)
    def main(*args, **kwargs):
        if raises:
            raise RuntimeError("main failed")
    run, token, closed = power_worker(kind, main)
    entered, resume = threading.Event(), threading.Event()
    emit = api._emit
    def blocked_result(event):
        # Keep fault notification free to run under the gate lock.
        if event[0] == "result" and event[2] == i18n.tr("run_result"):
            assert event[1] == ("error" if raises else "info")
            entered.set()
            assert resume.wait(2)
        emit(event)
    monkeypatch.setattr(api, "_emit", blocked_result)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        wait_for(lambda: api._gate.state()[0] == c.State.FAULT)
    finally:
        resume.set()
        worker.join(1)
    assert not worker.is_alive()
    assert api._gate.state()[0] == c.State.FAULT
    assert api._gate.worker_done
    assert closed == ([] if kind == "resources" else [True])


@pytest.mark.parametrize("kind", ["resources", "plan", "functions"])
def test_main_entry_timer_race_has_single_winner(api, monkeypatch, power_worker, kind):
    calls, timers, admissions = [], [], []
    run, token, closed = power_worker(kind, lambda *a, **kw: calls.append(True))
    monkeypatch.setattr(api._gate, "_timer", lambda seconds, callback: timers.append(callback))
    untimed = api._untimed
    def expire_before_admission(t):
        timers[-1]()
        accepted = untimed(t)
        admissions.append(accepted)
        return accepted
    monkeypatch.setattr(api, "_untimed", expire_before_admission)
    run()
    assert admissions == [False] and calls == []
    assert closed == ([] if kind == "resources" else [True])
    assert api._release(token) is False
    assert api._gate.state()[0] == c.State.FAULT
    assert api._gate.worker_done


@pytest.mark.parametrize("kind,blocked_stage", [
    ("plan", "main"), ("plan", "cleanup"),
    ("functions", "main"), ("functions", "cleanup"),
    ("resources", "main"), ("resources", "cleanup"), ("resources", "close"),
])
def test_real_runner_main_untimed_and_internal_cleanup_timed(api, monkeypatch, kind, blocked_stage):
    from n6705c_seq import execute, e36441a
    from n6705c_seq.plan import SeqRow, to_functions
    from test_e36441a import _FakeSession

    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.1)
    entered, resume = threading.Event(), threading.Event()
    def blocked(*args):
        entered.set()
        assert resume.wait(3)
    rows = [SeqRow(1, True, "A", 0, 0, 1, 0.01, 0.01, 0.01)]
    if kind == "resources":
        session = _FakeSession()
        monkeypatch.setattr(e36441a, "_sessions", lambda *a: [SimpleNamespace(
            close=blocked if blocked_stage == "close" else lambda: None)])
        monkeypatch.setattr(e36441a, "Session", lambda t: session)
        # Replace the module's time reference, preserving threading's real clock.
        monkeypatch.setattr(e36441a, "time", SimpleNamespace(
            sleep=blocked if blocked_stage == "main" else lambda seconds: None))
        if blocked_stage == "cleanup":
            monkeypatch.setattr(e36441a, "_restore", blocked)
        monkeypatch.setattr(web_seq, "detect_model", lambda r: "E36441A")
    else:
        group = execute.InstrumentGroup.demo(frames=1, slots=execute.DEMO_SLOTS)
        monkeypatch.setattr(web_seq, "detect_model", lambda r: "N6705C")
        monkeypatch.setattr(e36441a, "detect_model", lambda r: "N6705C")
        monkeypatch.setattr(web_seq, "connect_group", lambda r: group)
        monkeypatch.setattr(execute, "connect_group", lambda r: group)
        monkeypatch.setattr(execute, "_wait", blocked if blocked_stage == "main" else lambda runner: None)
        if blocked_stage == "cleanup":
            monkeypatch.setattr(execute, "outputs_off", blocked)
        monkeypatch.setattr(web_list.synth, "to_functions", lambda *a: to_functions(rows))
    token = api._reserve("power")
    def run():
        if kind == "functions":
            api._list_work(SimpleNamespace(hold_last=False), None, 1, ["x"], False, False, token)
        else:
            api._work(rows, 1, ["x"], False, False, False, token)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        if blocked_stage == "main":
            time.sleep(0.3)
            assert api._gate.state()[0] == c.State.POWER
        else:
            wait_for(lambda: api._gate.state()[0] == c.State.FAULT)
    finally:
        resume.set()
        worker.join(1)
    assert not worker.is_alive()
    assert api._gate.state()[0] == (c.State.IDLE if blocked_stage == "main" else c.State.FAULT)
    assert api._gate.worker_done
