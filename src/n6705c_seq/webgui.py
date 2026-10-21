"""pywebview shell and shared bridge state."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
from pathlib import Path

import webview

from . import gui, i18n
from .i18n import tr

_CONFIG_LOCK = threading.Lock()


class _Core:
    def __init__(self, config_path: Path | None = None):
        self._window = None
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._closing = False
        self._kind = None
        self._config_path = gui.CONFIG if config_path is None else Path(config_path)
        with _CONFIG_LOCK:
            original = gui.CONFIG
            try:
                gui.CONFIG = self._config_path
                self._config = gui._load_config()
            finally:
                gui.CONFIG = original
        self._measured = {"seq": {}, "list": {}}
        self._selftest = False
        self._selftest_done = threading.Event()
        self._selftest_ok = False
        self._selftest_detail = ""
        i18n.set_lang(self._config["lang"])

    def _reserve(self) -> bool:
        with self._lock:
            if self._busy or self._closing:
                return False
            self._busy = True
            self._emit(["busy", True])
            return True

    def _release(self) -> None:
        with self._lock:
            self._busy = False
            self._emit(["busy", False])

    def _emit(self, event: list) -> None:
        self._queue.put(event)

    def _confirm(self, title: str, text: str) -> bool:
        return self._window.create_confirmation_dialog(title, text)

    def _dialog_path(self, result) -> str | None:
        if not result:
            return None
        return result if isinstance(result, str) else result[0] or None

    def _open_path(self, filetypes: tuple[str, ...]) -> str | None:
        return self._dialog_path(self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=filetypes))

    def _save_path(self, filetypes: tuple[str, ...], ext: str) -> str | None:
        path = self._dialog_path(self._window.create_file_dialog(webview.FileDialog.SAVE, file_types=filetypes))
        if path and not Path(path).suffix:
            path += ext
        return path

    def _save_config(self, resources: list[str], demo: bool) -> None:
        self._config = {"resources": list(resources), "demo": demo, "lang": i18n.lang}
        self._config_path.write_text(json.dumps(self._config))

    def _on_closing(self) -> bool:
        with self._lock:
            if self._busy:
                self._emit(["result", "warning", tr("busy_title"), tr("busy_body")])
                return False
            self._closing = True
            return True

    def config(self) -> dict:
        return {**self._config, "resources": list(self._config["resources"])}

    def strings(self) -> dict:
        return {"lang": i18n.lang, "strings": {key: list(pair) for key, pair in i18n.STRINGS.items()}}

    def set_lang(self, lang: str) -> None:
        if not isinstance(lang, str):
            raise TypeError("lang must be a string")
        i18n.set_lang(lang)
        self._save_config(self._config["resources"], self._config["demo"])
        if self._window is not None:
            self._window.set_title(tr("title"))

    def poll(self) -> list[list]:
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    def selftest_report(self, ok: bool, detail: str) -> None:
        if not isinstance(ok, bool) or not isinstance(detail, str):
            raise TypeError("selftest report requires a bool and a string")
        if self._selftest and not self._selftest_done.is_set():
            self._selftest_ok = ok
            self._selftest_detail = detail
            self._selftest_done.set()


class Api(_Core):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    api = Api()
    api._selftest = args.selftest
    screens = webview.screens
    width, height = (min(1180, screens[0].width), min(900, screens[0].height)) if screens else (1180, 900)
    api._window = webview.create_window(
        tr("title"), str(Path(__file__).parent / "web" / "index.html"), js_api=api,
        width=width, height=height, min_size=(min(640, width), min(480, height)), confirm_close=False,
    )
    api._window.events.closing += api._on_closing
    failed = False

    def started():
        nonlocal failed
        if sys.platform == "win32" and webview.renderer != "edgechromium":
            failed = True
            ctypes.windll.user32.MessageBoxW(0, tr("webview2_missing"), tr("title"), 0x10)
            api._window.destroy()
            return
        if api._selftest:
            api._selftest_done.wait(30)
            api._window.destroy()

    webview.start(started, gui="edgechromium" if sys.platform == "win32" else None, http_server=True)
    code = int(failed or (api._selftest and not api._selftest_ok))
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()
    # ponytail: pywebview non-daemon bridge threads can hang in evaluate_js after window destruction.
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
