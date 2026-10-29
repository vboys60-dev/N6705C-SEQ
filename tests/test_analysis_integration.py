"""End-to-end analysis checks: real webgui bridge + real gate + real server + real core (no window)."""

import http.client
import json
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import webview

from n6705c_seq import form, i18n, webgui
from n6705c_seq.analysis import contracts, core, storage
from test_web_list import _plan, _settings
from test_web_seq import _raw

SAMPLE = Path(__file__).parent / "analysis" / "fixtures" / "default_20260803_092740.dlog"


class Window:
    def __init__(self):
        self.path = None
        self.destroyed = 0

    def create_file_dialog(self, kind, **kwargs):
        return self.path

    def create_confirmation_dialog(self, title, text):
        return True

    def set_title(self, title):
        pass

    def destroy(self):
        self.destroyed += 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(contracts, "ANALYSIS_ENABLED", True)
    monkeypatch.setattr(form, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(storage, "default_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(storage, "default_prefs_path", lambda: tmp_path / "prefs.json")
    exits = []
    monkeypatch.setattr(webgui.os, "_exit", exits.append)
    lang = i18n.lang
    api = webgui.Api()
    api._window = Window()
    api.exits = exits
    yield api
    api._shutdown_analysis()
    for entry in list(core._open_cache.values()):
        while entry["_refs"]:
            core.release_handle(entry)
    assert core.close_all()
    assert exits == []
    i18n.set_lang(lang)


def wait_state(api, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while api._gate.state()[0] != state:
        assert time.monotonic() < deadline, api._gate.state()
        time.sleep(0.01)


class Viewer:
    """What the iframe does: read the fragment, then talk HTTP with token + gen headers."""

    def __init__(self, url):
        parts = urlsplit(url)
        fragment = {k: v[0] for k, v in parse_qs(parts.fragment).items()}
        self.port, self.token, self.gen = parts.port, fragment["t"], int(fragment["gen"])
        self.lang = fragment["lang"]
        assert parts.hostname == contracts.HOST and parts.path == "/viewer.html" and not parts.query

    def req(self, method, path, body=None, headers=None, gen=None):
        hdr = {contracts.HEADER_TOKEN: self.token, contracts.HEADER_GEN: str(self.gen if gen is None else gen),
               **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            hdr["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(contracts.HOST, self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=hdr)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def open(self, grant):
        status, body = self.req("POST", "/open", {"grant": grant})
        assert status == 200, body
        (n,) = struct.unpack("<I", body[:4])
        return json.loads(body[4:4 + n])


def enter(api):
    r = api.analysis_enter()
    assert r["ok"] is True, r
    assert api._gate.state() == ("analysis", r["gen"])
    return r, Viewer(r["url"])


def grant(api, gen, path, purpose="dlog", save=False):
    api._window.path = str(path)
    r = api.analysis_save_path(gen, purpose, Path(path).name) if save else api.analysis_open_path(gen, purpose)
    assert r["ok"] is True and r["name"] == Path(path).name and "/" not in r["name"], r
    return r["grant"]


def test_end_to_end_open_samples_save_csv_and_leave(api, tmp_path):
    r, viewer = enter(api)
    assert viewer.lang == i18n.lang and viewer.gen == r["gen"] == 1
    assert viewer.req("GET", "/viewer.html")[0] == 200
    meta = viewer.open(grant(api, r["gen"], SAMPLE))
    assert meta["fileName"] == SAMPLE.name and len(meta["handle"]) == 40
    status, body = viewer.req("GET", f"/samples?h={meta['handle']}&lo=0&hi=99")
    assert status == 200 and len(body) > 4
    png = tmp_path / "out.png"
    status, body = viewer.req("POST", "/save", b"\x89PNG", {
        contracts.HEADER_GRANT: grant(api, r["gen"], png, "png", save=True), contracts.HEADER_PURPOSE: "png"})
    assert status == 200 and png.read_bytes() == b"\x89PNG"
    csv = tmp_path / "out.csv"
    ch = meta["activeChannels"][0]
    status, body = viewer.req("POST", "/export/csv", {"grant": grant(api, r["gen"], csv, "csv", save=True),
                                                       "h": meta["handle"], "lo": 0, "hi": 9, "cols": f"p:V{ch}"})
    assert status == 200, body
    assert csv.read_text().count("\n") >= 10
    # Leaving releases every handle and memmap before returning to idle.
    assert api.analysis_leave(r["gen"]) == {"ok": True, "gen": r["gen"]}
    wait_state(api, "idle")
    assert core.close_all()
    assert viewer.req("GET", f"/samples?h={meta['handle']}&lo=0&hi=9")[0] == 423
    assert api.test_connection(["", ""], True) == {}


def test_analysis_blocks_every_power_entry_and_power_blocks_analysis(api):
    r, viewer = enter(api)
    denied = {"error": i18n.tr("power_denied_analysis")}
    assert api.seq_run(_raw(), [], True, False, False) == denied
    assert api.test_connection(["", ""], True) == denied
    assert api.all_off(["USB::X"], False) == denied
    assert api.list_run(_plan(), _settings(), [], True, False) == denied
    assert api._reserve("power") is None
    assert api._gate.state() == ("analysis", r["gen"])
    assert api.analysis_leave(r["gen"])["ok"]
    wait_state(api, "idle")
    token = api._reserve("power")
    try:
        assert api.analysis_enter()["code"] == "power"
        assert api.analysis_open_path(r["gen"], "dlog")["code"] == "stale"
        assert viewer.req("GET", "/prefs")[0] == 423
    finally:
        assert api._release(token)


def test_tab_switch_generations_and_direct_bridge_calls(api, tmp_path):
    r1, v1 = enter(api)
    assert api.analysis_enter()["gen"] == r1["gen"]  # idempotent while held
    old = grant(api, r1["gen"], SAMPLE)
    api.analysis_leave(r1["gen"])
    wait_state(api, "idle")
    r2, v2 = enter(api)
    assert r2["gen"] == r1["gen"] + 1 and v2.token == v1.token
    # Old generation: leave, dialogs, grants and requests are all refused.
    assert api.analysis_leave(r1["gen"])["code"] == "stale"
    assert api.analysis_open_path(r1["gen"], "dlog")["code"] == "stale"
    assert v2.req("POST", "/open", {"grant": old})[0] == 403
    assert v1.req("GET", "/prefs")[0] == 409
    # Malformed bridge arguments never raise and never open a dialog.
    api._window.path = str(SAMPLE)
    for result in (api.analysis_open_path(str(r2["gen"]), "dlog"), api.analysis_open_path(r2["gen"], "csv"),
                   api.analysis_save_path(r2["gen"], "exe", "x"),
                   api.analysis_save_path(r2["gen"], "png", "../x.png"), api.analysis_save_path(r2["gen"], "png", None),
                   api.analysis_leave("1")):
        assert result["ok"] is False and result["code"] in ("stale", "bad_purpose"), result
    # A grant is single-use and bound to its purpose.
    png = grant(api, r2["gen"], tmp_path / "a.png", "png", save=True)
    assert v2.req("POST", "/open", {"grant": png})[0] == 403
    assert v2.req("POST", "/save", b"x", {contracts.HEADER_GRANT: png, contracts.HEADER_PURPOSE: "png"})[0] == 403
    # Cancelled dialog: no grant.
    api._window.path = None
    assert api.analysis_open_path(r2["gen"], "dlog") == {"ok": False, "cancelled": True, "gen": r2["gen"]}
    assert api.analysis_leave(r2["gen"])["ok"]
    wait_state(api, "idle")


def test_draining_refuses_and_close_window_while_analysis(api):
    r, viewer = enter(api)
    viewer.open(grant(api, r["gen"], SAMPLE))
    assert api.analysis_leave(r["gen"])["ok"]
    if api._gate.state()[0] == "draining":
        assert api.analysis_enter()["code"] == "draining"
    wait_state(api, "idle")
    r, viewer = enter(api)
    viewer.open(grant(api, r["gen"], SAMPLE))
    assert api._on_closing() is False  # server running: async cleanup, window destroyed afterwards
    assert api._gate.state()[0] == "closing"
    assert api.analysis_enter()["code"] == "closing"
    assert api.test_connection(["", ""], True) == {"error": i18n.tr("analysis_denied_closing")}
    deadline = time.monotonic() + 5
    while not api._window.destroyed:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert core.close_all()
    assert api._on_closing() is True  # second closing event from destroy is let through


def test_fault_blocks_analysis_and_power(api, monkeypatch):
    monkeypatch.setattr(contracts, "POWER_FINISH_TIMEOUT_S", 0.05)
    token = api._reserve("power")
    api._finishing(token)
    wait_state(api, "fault")
    assert api.analysis_enter()["code"] == "fault"
    assert api.test_connection(["", ""], True) == {"error": i18n.tr("fault_body")}
    assert api._release(token) is False
    assert api._gate.state()[0] == "fault"


def test_selftest_with_real_server(api, monkeypatch):
    window = api._window
    window.events = SimpleNamespace(closing=webview.Event(window, should_lock=True))
    monkeypatch.setattr(webgui.sys, "argv", ["webgui", "--selftest"])
    monkeypatch.setattr(webgui, "Api", lambda: api)
    monkeypatch.setattr(webview, "screens", [])
    monkeypatch.setattr(webview, "create_window", lambda *a, **k: window)
    monkeypatch.setattr(api._selftest_done, "wait", lambda seconds: True)
    codes = []
    monkeypatch.setattr(webgui.os, "_exit", codes.append)

    def start(func, **kwargs):
        api.selftest_report(True, "")
        func()

    monkeypatch.setattr(webview, "start", start)
    webgui.main()
    assert codes == [0] and window.destroyed == 1
    assert api._server is not None and api._shutdown_done


def test_selftest_fails_when_server_check_fails(api, monkeypatch):
    window = api._window
    window.events = SimpleNamespace(closing=webview.Event(window, should_lock=True))
    monkeypatch.setattr(webgui.sys, "argv", ["webgui", "--selftest"])
    monkeypatch.setattr(webgui, "Api", lambda: api)
    monkeypatch.setattr(webview, "screens", [])
    monkeypatch.setattr(webview, "create_window", lambda *a, **k: window)
    monkeypatch.setattr(api._selftest_done, "wait", lambda seconds: True)
    api._ensure_analysis_server()
    monkeypatch.setattr(api._server, "self_check", lambda: False)
    codes = []
    monkeypatch.setattr(webgui.os, "_exit", codes.append)
    monkeypatch.setattr(webview, "start", lambda func, **kwargs: (api.selftest_report(True, ""), func()))
    webgui.main()
    assert codes == [1]
