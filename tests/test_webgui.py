"""Shared web bridge checks without starting a window."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import webview

from n6705c_seq import gui, i18n, webgui


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
    monkeypatch.setattr(gui, "CONFIG", tmp_path / "config.json")
    original_lang = i18n.lang
    api = webgui.Api()
    api._window = FakeWindow()
    yield api
    i18n.set_lang(original_lang)


def test_reserve_concurrently(core):
    barrier = Barrier(16)

    def reserve(_):
        barrier.wait()
        return core._reserve()

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(reserve, range(16))) == 1
    assert core.poll() == [["busy", True]]
    core._release()
    assert core.poll() == [["busy", False]]
    assert core._reserve()


def test_closing_veto_and_reservation(core):
    assert core._reserve()
    assert core._on_closing() is False
    assert core._closing is False
    assert core.poll() == [["busy", True], ["result", "warning", i18n.tr("busy_title"), i18n.tr("busy_body")]]
    core._release()
    assert core._on_closing() is True
    assert core._reserve() is False
    assert core._busy is False


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


def test_config_and_language(core):
    assert core.config() == {"resources": [], "demo": True, "lang": "zh"}
    core._save_config(["address"], False)
    core.set_lang("en")
    assert core._window.title == i18n.tr("title")
    assert json.loads(core._config_path.read_text()) == {"resources": ["address"], "demo": False, "lang": "en"}
    assert webgui.Api(core._config_path).config() == core.config()
    config = core.config()
    config["resources"].append("other")
    assert core.config()["resources"] == ["address"]
    with pytest.raises(TypeError):
        core.set_lang(None)


def test_strings_and_public_surface(core):
    translated = core.strings()
    assert translated == {"lang": i18n.lang, "strings": {key: list(pair) for key, pair in i18n.STRINGS.items()}}
    assert {name for name in dir(core) if not name.startswith("_")} == {
        "config", "strings", "set_lang", "poll", "selftest_report",
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
    paths = [Path(webgui.__file__), Path(__file__), root / "packaging" / "web_entry.py"]
    paths += list((root / "src" / "n6705c_seq" / "web").glob("*"))
    for path in paths:
        assert not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", path.read_text(encoding="utf-8")), path


def test_web_translation_keys():
    source = Path(webgui.__file__)
    keys = re.findall(r'tr\("([^"\n]+)"', source.read_text())
    for path in (source.parent / "web").glob("*"):
        text = path.read_text()
        keys += re.findall(r'data-i18n="([^"\n]+)"', text)
        keys += re.findall(r'\.t\("([^"\n]+)"', text)
    assert set(keys) <= i18n.STRINGS.keys()


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
    assert core._closing is True
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
