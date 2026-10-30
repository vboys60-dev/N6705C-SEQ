"""pywebview shell and shared bridge state."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import queue
import sys
import threading
import time
from pathlib import Path

import webview

from . import form, i18n
from .analysis import contracts
from .analysis.gate import Gate
from .web_list import ListApi
from .web_seq import SeqApi
from .i18n import tr

class _Core:
    def __init__(self, config_path: Path | None = None):
        self._window = None
        self._config_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._server = None
        self._dialog_lock = threading.Lock()
        self._server_lock = threading.Lock()
        self._shutdown_done = False
        self._cleanup_done = False
        self._cleanup_started = False
        self._cleanup_lock = threading.Lock()
        self._gate = Gate(self._emit, fault=self._activity_fault, closing=self._start_cleanup)
        self._gate.set_release_resources(self._release_analysis_resources)
        self._kind = None
        self._config_path = form.CONFIG if config_path is None else Path(config_path)
        self._config = form.load_config(self._config_path)
        self._measured = {"seq": {}, "list": {}}
        self._selftest = False
        self._selftest_done = threading.Event()
        self._selftest_ok = False
        self._selftest_detail = ""
        i18n.set_lang(self._config["lang"])

    def _reserve(self, kind):
        return self._gate.reserve(kind)

    def _release(self, token):
        return self._gate.release(token)

    def _finishing(self, token):
        self._gate.finishing(token)

    def _untimed(self, token) -> bool:
        return self._gate.untimed(token)

    def _power_denied(self):
        state, _ = self._gate.state()
        key = "busy_body" if state == contracts.State.POWER else contracts.DENY_TEXT_KEY.get(
            state.value, "busy_body")
        return {"error": tr(key)}

    def _activity_fault(self):
        self._emit(["result", "error", tr("fault_title"), tr("fault_body")])

    def _analysis_error(self, code, **fmt):
        code = contracts.DenyCode(code)
        return {"ok": False, "code": code.value, "error": tr(contracts.DENY_TEXT_KEY[code], **fmt)}

    def _ensure_analysis_server(self):
        with self._server_lock:
            if self._server is None:
                if importlib.util.find_spec(__package__ + ".analysis.server") is None:
                    raise RuntimeError("analysis.server is unavailable")
                from .analysis.server import start_server
                self._server = start_server(self._gate)

    def _release_analysis_resources(self):
        return self._server is None or self._server.release_resources()

    def analysis_enter(self):
        if not contracts.ANALYSIS_ENABLED:
            return self._analysis_error("disabled")
        try:
            gen, denied = self._gate.enter(self._ensure_analysis_server)
            if denied:
                return self._analysis_error(denied)
            return {"ok": True, "gen": gen, "url": self._server.url(gen, i18n.lang)}
        except Exception as exc:
            return self._analysis_error("server", error=str(exc))

    def analysis_leave(self, gen):
        if not self._gate.leave(gen):
            return self._analysis_error("stale")
        return {"ok": True, "gen": gen}

    def _analysis_path(self, gen, purpose, suggested=None, *, save=False):
        try:
            purpose = (contracts.SavePurpose if save else contracts.OpenPurpose)(purpose)
        except (ValueError, TypeError):
            return self._analysis_error("bad_purpose")
        if save and (not isinstance(suggested, str) or len(suggested) > contracts.MAX_SUGGESTED_NAME
                     or any(char in suggested for char in "/\\:")):
            return self._analysis_error("bad_purpose")
        if type(gen) is not int or self._gate.state() != (contracts.State.ANALYSIS, gen):
            return self._analysis_error("stale")
        if not self._dialog_lock.acquire(blocking=False):  # one native dialog at a time
            return {"ok": False, "cancelled": True, "gen": gen}
        try:
            kwargs = {"file_types": (contracts.SAVE_FILE_TYPES if save else contracts.OPEN_FILE_TYPES)[purpose]}
            if save:
                ext = contracts.SAVE_EXT[purpose]
                kwargs["save_filename"] = suggested if suggested.lower().endswith(ext) else suggested + ext
            path = self._dialog_path(self._window.create_file_dialog(
                webview.FileDialog.SAVE if save else webview.FileDialog.OPEN, **kwargs))
            if not path:
                return {"ok": False, "cancelled": True, "gen": gen}
            if save and Path(path).suffix.lower() != contracts.SAVE_EXT[purpose]:
                path += contracts.SAVE_EXT[purpose]  # native dialog never saw this name: re-confirm overwrite
                if Path(path).exists() and not self._confirm(
                        tr("confirm"), f"{Path(path).name} already exists. Overwrite?"):
                    return {"ok": False, "cancelled": True, "gen": gen}
        finally:
            self._dialog_lock.release()
        grant = self._gate.issue_grant(gen, "save" if save else "open", purpose.value, path)
        if grant is None:
            return self._analysis_error("stale")
        return {"ok": True, "gen": gen, "grant": grant.id, "name": Path(path).name}

    def analysis_open_path(self, gen, purpose):
        return self._analysis_path(gen, purpose)

    def analysis_save_path(self, gen, purpose, suggested):
        return self._analysis_path(gen, purpose, suggested, save=True)

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
        with self._config_lock:
            self._config = {"resources": list(resources), "demo": demo, "lang": i18n.lang}
            form.save_config(self._config_path, self._config["resources"], demo, self._config["lang"])

    def _on_closing(self) -> bool:
        result = self._gate.on_closing()
        if result == "power":
            self._emit(["result", "warning", tr("busy_title"), tr("busy_body")])
            return False
        if result == "draining":
            self._emit(["result", "info", tr("analysis_tab"), tr("closing_wait")])
            return False
        if result == "closing":
            return self._cleanup_done
        if self._server is None and not self._gate.job_count():
            self._cleanup_done = True
            return True
        self._start_cleanup()
        return False

    def _shutdown_analysis(self, timeout=1.0):
        with self._server_lock:
            if self._server is None or self._shutdown_done:
                return
            self._shutdown_done = True
            for callback in (self._server.release_resources, lambda: self._server.shutdown(timeout)):
                try:
                    callback()
                except Exception:
                    pass

    def _start_cleanup(self):
        with self._cleanup_lock:
            if self._cleanup_started:
                return
            self._cleanup_started = True
        threading.Thread(target=self._cleanup_analysis, daemon=True).start()

    def _cleanup_analysis(self):
        deadline = time.monotonic() + contracts.CLOSING_CLEANUP_TIMEOUT_S
        finished = threading.Event()

        def expired():
            if finished.is_set():
                return
            # A stuck cleanup must not block the independent deadline watcher.
            threading.Thread(target=self._shutdown_analysis, daemon=True).start()
            if self._server is not None:
                try:
                    self._server.shutdown(1.0)
                except Exception:
                    pass
            os._exit(2)

        timer = threading.Timer(contracts.CLOSING_CLEANUP_TIMEOUT_S, expired)
        timer.daemon = True
        timer.start()
        try:
            while self._gate.job_count():
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.01)
            if self._server is not None:
                while not self._server.release_resources():
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.01)
                self._shutdown_analysis(max(0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                return
            finished.set()
            timer.cancel()
            self._cleanup_done = True
            if self._window is not None:
                self._window.destroy()
        except Exception:
            # Leave the independent timeout armed if cleanup fails.
            pass

    def config(self) -> dict:
        return {**self._config, "resources": list(self._config["resources"]),
                contracts.CONFIG_KEY_ANALYSIS: contracts.ANALYSIS_ENABLED,
                contracts.CONFIG_KEY_SELFTEST: self._selftest}

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


class Api(SeqApi, ListApi, _Core):
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
            if contracts.ANALYSIS_ENABLED:
                try:
                    api._ensure_analysis_server()
                    server_ok = api._server.self_check()
                except Exception:
                    server_ok = False
            else:
                server_ok = True
            api._selftest_done.wait(contracts.SELFTEST_REPORT_TIMEOUT_S)
            api._selftest_ok = api._selftest_ok and server_ok
            api._window.destroy()

    try:
        webview.start(started, gui="edgechromium" if sys.platform == "win32" else None, http_server=True)
    finally:
        api._shutdown_analysis()
    code = int(failed or (api._selftest and not api._selftest_ok))
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()
    # ponytail: pywebview non-daemon bridge threads can hang in evaluate_js after window destruction.
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
