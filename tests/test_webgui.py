"""Shared web bridge checks without starting a window."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import Mock

import pytest
import webview

from n6705c_seq import form, i18n, webgui
from n6705c_seq.analysis import contracts


def _legacy_events(api):
    return [event for event in api.poll() if event[0] != "state"]


class FakeWindow:
    def __init__(self, result=None):
        self.result = result
        self.calls = []
        self.title = None

    def create_file_dialog(self, kind, **kwargs):
        self.calls.append((kind, kwargs))
        return self.result

    def create_confirmation_dialog(self, title, text):
        self.calls.append((title, text))
        return self.result

    def set_title(self, title):
        self.title = title


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setattr(form, "CONFIG", tmp_path / "config.json")
    original_lang = i18n.lang
    api = webgui.Api()
    api._window = FakeWindow()
    yield api
    i18n.set_lang(original_lang)


def test_reserve_concurrently(core):
    barrier = Barrier(16)

    def reserve(_):
        barrier.wait()
        return core._reserve("power")

    with ThreadPoolExecutor(max_workers=16) as pool:
        tokens = [token for token in pool.map(reserve, range(16)) if token is not None]
        assert len(tokens) == 1
        token = tokens[0]
    assert _legacy_events(core) == [["busy", True]]
    core._release(token)
    assert _legacy_events(core) == [["busy", False]]
    token = core._reserve("power")
    assert token


def test_closing_veto_and_reservation(core):
    token = core._reserve("power")
    assert token
    assert core._on_closing() is False
    assert (core._gate.state()[0] == "closing") is False
    assert _legacy_events(core) == [["busy", True], ["result", "warning", i18n.tr("busy_title"), i18n.tr("busy_body")]]
    core._release(token)
    assert core._on_closing() is True
    token = core._reserve("power")
    assert token is None
    assert core._gate.state()[0] == "closing"


def test_closing_vetoed_in_fault_while_worker_alive(core, monkeypatch):
    monkeypatch.setattr(contracts, "POWER_FINISH_TIMEOUT_S", 0.02)
    token = core._reserve("power")
    core._gate.finishing(token)
    deadline = time.monotonic() + 2
    while core._gate.state()[0] != "fault":
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert core._on_closing() is False  # worker not returned: keep the window
    assert core._gate.state()[0] == "fault"
    core._release(token)  # late return marks the worker done, never unlocks
    assert core._gate.state()[0] == "fault"
    assert core._on_closing() is True


@pytest.mark.parametrize("result, expected", [(None, None), ("", None), ((), None), ([], None),
                                               ("/tmp/test", "/tmp/test"), (("/tmp/test",), "/tmp/test"),
                                               (["/tmp/test", "/tmp/other"], "/tmp/test")])
def test_dialog_normalization(core, result, expected):
    core._window.result = result
    types = ("CSV (*.csv)", "All files (*.*)")
    assert core._open_path(types) == expected
    assert core._window.calls[-1] == (webview.FileDialog.OPEN, {"file_types": types})
    assert core._save_path(types, ".csv") == (expected + ".csv" if expected else None)
    assert core._window.calls[-1] == (webview.FileDialog.SAVE, {"file_types": types})


def test_dialog_extension_and_confirmation(core):
    core._window.result = "/tmp/test.txt"
    assert core._save_path(("All files (*.*)",), ".csv") == "/tmp/test.txt"
    core._window.result = False
    assert core._confirm("title", "text") is False
    assert core._window.calls[-1] == ("title", "text")


@pytest.mark.parametrize("confirmed", [False, True])
def test_save_path_confirms_existing_appended_extension(core, tmp_path, monkeypatch, confirmed):
    path = tmp_path / "plan.csv"
    path.write_text("existing", encoding="utf-8")
    core._window.result = str(tmp_path / "plan")
    confirm = Mock(return_value=confirmed)
    monkeypatch.setattr(core, "_confirm", confirm)

    assert core._save_path(("CSV (*.csv)",), ".csv") == (str(path) if confirmed else None)
    confirm.assert_called_once_with(
        i18n.tr("confirm"), i18n.tr("overwrite_confirm", name="plan.csv"))


@pytest.mark.parametrize("filename, exists", [("plan", False), ("plan.csv", True), ("plan.txt", True)])
def test_save_path_without_confirmation(core, tmp_path, monkeypatch, filename, exists):
    path = tmp_path / filename
    if exists:
        path.write_text("existing", encoding="utf-8")
    core._window.result = str(path)
    confirm = Mock()
    monkeypatch.setattr(core, "_confirm", confirm)

    expected = path if path.suffix else path.with_suffix(".csv")
    assert core._save_path(("CSV (*.csv)",), ".csv") == str(expected)
    confirm.assert_not_called()


def test_config_and_language(core):
    assert core.config() == {"resources": [], "demo": True, "lang": "zh", "theme": None,
                             "analysis_enabled": True, "selftest_mode": False}
    assert set(core.config()) == set(contracts.CONFIG_KEYS)
    core._save_config(["address"], False)
    core.set_lang("en")
    assert core._window.title == i18n.tr("title")
    assert json.loads(core._config_path.read_text()) == {"resources": ["address"], "demo": False, "lang": "en", "theme": None}
    assert webgui.Api(core._config_path).config() == core.config()
    config = core.config()
    config["resources"].append("other")
    assert core.config()["resources"] == ["address"]
    core._selftest = True
    assert core.config()[contracts.CONFIG_KEY_SELFTEST] is True
    core._save_config(["address"], False)
    assert json.loads(core._config_path.read_text()) == {"resources": ["address"], "demo": False, "lang": "en", "theme": None}
    with pytest.raises(TypeError):
        core.set_lang(None)


def test_strings_and_public_surface(core):
    translated = core.strings()
    assert translated == {"lang": i18n.lang, "strings": {key: list(pair) for key, pair in i18n.STRINGS.items()}}
    assert {name for name in dir(core) if not name.startswith("_")} == {
        "config", "strings", "set_lang", "set_theme", "poll", "selftest_report",
        "analysis_enter", "analysis_leave", "analysis_open_path", "analysis_save_path",
        "seq_meta", "seq_preview", "seq_open_csv", "seq_save_csv", "seq_run", "seq_stop", "test_connection", "all_off", "scan_instruments",
        "list_meta", "list_draft", "list_compile", "list_open_json", "list_save_json", "list_run",
        "record_meta", "record_start", "record_stop", "record_status", "record_snapshot", "record_load",
    }
    translated["strings"]["title"][0] = "changed"
    assert core.strings()["strings"]["title"][0] == i18n.STRINGS["title"][0]


def test_selftest_report(core):
    core.selftest_report(True, "ignored")
    assert not core._selftest_done.is_set()
    core._selftest = True
    core.selftest_report(False, "failure")
    assert core._selftest_done.is_set()
    assert core._selftest_ok is False
    assert core._selftest_detail == "failure"
    core.selftest_report(True, "late")
    assert core._selftest_ok is False
    with pytest.raises(TypeError):
        core.selftest_report(1, "wrong")


def test_web_sources_have_no_cjk_text():
    root = Path(__file__).resolve().parents[1]
    paths = [Path(webgui.__file__), Path(__file__), root / "packaging" / "gui_entry.py"]
    source = root / "src" / "n6705c_seq"
    paths += [source / name for name in ("form.py", "list_form.py", "synth.py", "listplan.py")]
    paths += list(source.glob("web_*.py")) + list((source / "web").glob("*"))
    for path in paths:
        assert not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", path.read_text(encoding="utf-8")), path


def test_web_translation_keys():
    from n6705c_seq import form
    from n6705c_seq.listplan import FIELDS

    source = Path(webgui.__file__).parent
    paths = [source / name for name in ("webgui.py", "form.py", "list_form.py")]
    paths += list(source.glob("web_*.py")) + list((source / "web").glob("*.js"))
    paths += list((source / "web").glob("*.html"))
    keys = {"frame_channel", "enabled", "color"} | set(form.TITLES.values()) | {key for fields in FIELDS.values() for key in fields}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        keys.update(re.findall(r'tr\("([^"\n]+)"', text))
        keys.update(re.findall(r'\.t\("([^"\n]+)"', text))
        keys.update(re.findall(r'data-i18n="([^"\n]+)"', text))
        if path.suffix == ".js":
            keys.update(re.findall(r'(?:el|element)\("[^"\n]+", [^,\n]+, "([a-z_]+)"', text))
            keys.update(re.findall(r'(?:button|input|checkbox)\([^,\n]+, "([a-z_]+)"', text))
            keys.update(re.findall(r'result\(.*?, "([a-z_]+)"\)', text))
    assert keys <= i18n.STRINGS.keys()
    assert all(isinstance(pair, tuple) and len(pair) == 2 and
               all(isinstance(text, str) and text for text in pair) for pair in i18n.STRINGS.values())


@pytest.mark.parametrize("platform, renderer, selftest, report, expected", [
    ("darwin", "cocoa", True, True, 0),
    ("darwin", "cocoa", True, False, 1),
    ("darwin", "cocoa", True, None, 1),
    ("win32", "edgechromium", True, True, 0),
    ("win32", "mshtml", True, True, 1),
    ("darwin", "cocoa", False, None, 0),
])
@pytest.mark.parametrize("missing_streams", [(), ("stdout",), ("stderr",), ("stdout", "stderr")])
def test_main_with_fake_window(core, monkeypatch, platform, renderer, selftest, report, expected, missing_streams):
    from types import SimpleNamespace

    window = core._window
    window.events = SimpleNamespace(closing=webview.Event(window, should_lock=True))
    destroyed = []
    window.destroy = lambda: destroyed.append(True)
    messages = []
    monkeypatch.setattr(contracts, "ANALYSIS_ENABLED", False)  # shell-only test: do not start the analysis server
    monkeypatch.setattr(webgui.sys, "argv", ["webgui", "--selftest"] if selftest else ["webgui"])
    monkeypatch.setattr(webgui.sys, "platform", platform)
    monkeypatch.setattr(webgui, "Api", lambda: core)
    monkeypatch.setattr(webview, "screens", [SimpleNamespace(width=1000, height=700)])
    monkeypatch.setattr(webview, "renderer", renderer)
    monkeypatch.setattr(webgui.ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(
        MessageBoxW=lambda *args: messages.append(args))), raising=False)
    waits = []
    monkeypatch.setattr(core._selftest_done, "wait", lambda seconds: waits.append(seconds))
    created = []
    shutdown = []
    for name in ("stdout", "stderr"):
        if name in missing_streams:
            monkeypatch.setattr(webgui.sys, name, None)
        else:
            monkeypatch.setattr(getattr(webgui.sys, name), "flush", lambda name=name: shutdown.append(name))

    def exit_process(code):
        shutdown.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(webgui.os, "_exit", exit_process)

    def create_window(title, url, **kwargs):
        assert kwargs["js_api"].config()[contracts.CONFIG_KEY_SELFTEST] is selftest
        assert set(kwargs["js_api"].config()) == set(contracts.CONFIG_KEYS)
        created.append((title, url, kwargs))
        return window

    def start(func, **kwargs):
        assert kwargs == {"gui": "edgechromium" if platform == "win32" else None, "http_server": True}
        if report is not None:
            core.selftest_report(report, "")
        func()
        shutdown.append("start returned")

    monkeypatch.setattr(webview, "create_window", create_window)
    monkeypatch.setattr(webview, "start", start)
    with pytest.raises(SystemExit) as exc:
        webgui.main()
    assert exc.value.code == expected
    assert shutdown == ["start returned", *[name for name in ("stdout", "stderr") if name not in missing_streams], expected]
    assert destroyed == ([True] if selftest else [])
    assert len(window.events.closing) == 1
    assert window.events.closing.set() is False
    assert (core._gate.state()[0] == "closing") is True
    title, url, kwargs = created[0]
    assert title == i18n.tr("title")
    assert url == str(Path(webgui.__file__).parent / "web" / "index.html")
    assert kwargs == {"js_api": core, "width": 1000, "height": 700, "min_size": (640, 480), "confirm_close": False}
    if platform == "win32" and renderer != "edgechromium":
        assert messages == [(0, i18n.tr("webview2_missing"), i18n.tr("title"), 0x10)]
        assert not waits
    else:
        assert not messages
        assert waits == ([30] if selftest else [])


def test_config_writes_are_serialized_without_blocking_reserve(core, monkeypatch):
    from time import sleep

    barrier = Barrier(8)
    writing = Event()
    guard = Lock()
    active = maximum = 0
    save = form.save_config

    def slow_save(*args):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        writing.set()
        sleep(0.02)
        save(*args)
        with guard:
            active -= 1

    def write(index):
        barrier.wait()
        core._save_config([str(index) * 1000], bool(index % 2))

    monkeypatch.setattr(form, "save_config", slow_save)
    with ThreadPoolExecutor(max_workers=9) as pool:
        jobs = [pool.submit(write, index) for index in range(8)]
        assert writing.wait(2)
        token = pool.submit(core._reserve, "power").result(timeout=0.1)
        assert token
        core._release(token)
        for job in jobs:
            job.result(timeout=3)
    assert maximum == 1
    assert json.loads(core._config_path.read_text()) == {
        k: v for k, v in core.config().items() if k not in ("analysis_enabled", "selftest_mode")}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_survives_language_and_resource_writes(core, theme):
    core._save_config(["address"], False)
    core.set_theme(theme)
    assert core.config()["theme"] == theme
    core.set_lang("en")
    assert json.loads(core._config_path.read_text()) == dict(
        resources=["address"], demo=False, lang="en", theme=theme)
    core._save_config(["new address"], True)
    assert json.loads(core._config_path.read_text()) == dict(
        resources=["new address"], demo=True, lang="en", theme=theme)
    assert webgui.Api(core._config_path).config() == core.config()


@pytest.mark.parametrize("theme", [None, True, 1, [], {}, "", "soft-dark", "DARK"])
def test_set_theme_rejects_invalid_values_without_writing(core, theme):
    core.set_theme("dark")
    before = core._config_path.read_text()
    with pytest.raises(ValueError if isinstance(theme, str) else TypeError):
        core.set_theme(theme)
    assert core.config()["theme"] == "dark"
    assert core._config_path.read_text() == before


@pytest.mark.parametrize("theme", [None, "light", "dark"])
def test_analysis_url_receives_saved_theme(core, monkeypatch, theme):
    from urllib.parse import parse_qs, urlsplit
    from n6705c_seq.analysis.server import Handle

    if theme is not None:
        core.set_theme(theme)
    server = Mock()
    server.url.side_effect = Handle(Mock(token="test-token"), None, 12345).url
    core._server = server
    monkeypatch.setattr(core, "_ensure_analysis_server", lambda: None)
    monkeypatch.setattr(contracts, "ANALYSIS_ENABLED", True)
    reply = core.analysis_enter()
    assert reply["ok"] is True
    fragment = parse_qs(urlsplit(reply["url"]).fragment)
    assert fragment.get("theme") == (None if theme is None else [theme])
    if theme is None:
        server.url.assert_called_once_with(reply["gen"], i18n.lang)
    else:
        server.url.assert_called_once_with(reply["gen"], i18n.lang, theme)


def test_theme_and_language_saves_never_revert_concurrent_resource_saves(core):
    import threading

    start = threading.Barrier(2)

    def toggle():
        start.wait()
        for i in range(200):
            core.set_theme("dark" if i % 2 else "light")
            core.set_lang("en" if i % 2 else "zh")

    worker = threading.Thread(target=toggle)
    worker.start()
    start.wait()
    core._save_config(["new address"], False)
    worker.join()
    saved = json.loads(core._config_path.read_text())
    assert saved["resources"] == ["new address"] and saved["demo"] is False
    assert core.config()["resources"] == ["new address"]
