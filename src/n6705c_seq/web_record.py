"""Recording bridge; the worker alone owns the instrument and filesystem work."""

from __future__ import annotations

from copy import deepcopy
import errno
import math
from pathlib import Path
import re
import stat
import threading
import uuid

import webview

from n6705c.core.group import InstrumentGroup

from .analysis.contracts import PathResult
from . import e36441a
from .execute import DEMO_SLOTS, connect_group
from .i18n import tr
from .record import contracts as c
from .record.dlogfile import _paths, _safe_paths


class _RecordFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(code, detail=""):
    return {"error": tr(c.CODE_I18N_KEY[code], error=detail), "code": code.value}


def _config_copy(config, resources, demo):
    fields = {"machine_index", "channels", "sense_volt", "sense_curr", "period_s", "duration_s"}
    if not isinstance(config, dict) or set(config) != fields:
        raise ValueError("config must contain exactly the recording fields")
    if type(demo) is not bool:
        raise ValueError("demo must be a bool")
    if not isinstance(resources, list) or any(not isinstance(r, str) for r in resources):
        raise ValueError("resources must be a list of strings")
    cfg, saved = dict(config), list(resources)
    index = cfg["machine_index"]
    if type(index) is not int or index not in (0, 1):
        raise ValueError("machine_index must be 0 or 1")
    channels = cfg["channels"]
    if (not isinstance(channels, list) or not channels
            or any(type(ch) is not int or not 1 <= ch <= c.MAX_CHANNELS for ch in channels)
            or len(set(channels)) != len(channels)):
        raise ValueError("channels must be distinct integers from 1 to 4")
    cfg["channels"] = sorted(channels)
    if any(type(cfg[key]) is not bool for key in ("sense_volt", "sense_curr")):
        raise ValueError("sense_volt and sense_curr must be bools")
    parameters = len(channels) * (cfg["sense_volt"] + cfg["sense_curr"])
    if not parameters:
        raise ValueError("select voltage or current")
    for key, lower, upper in (("period_s", c.PERIOD_FLOOR_TABLE[parameters], c.PERIOD_MAX_S),
                              ("duration_s", c.DURATION_MIN_S, c.DURATION_MAX_S)):
        value = cfg[key]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not lower <= value <= upper):
            raise ValueError(f"{key} must be finite and between {lower} and {upper} seconds")
        cfg[key] = float(value)
    if not demo and (index >= len(saved) or not saved[index].strip()):
        raise ValueError("the selected instrument needs a resource address")
    cfg["run_id"] = uuid.uuid4().hex
    return deepcopy(cfg), saved, demo


def _failure_code(exc, phase):
    from n6705c.core.elog import ElogFetchError, ElogSinkError
    from n6705c.core.session import ScpiError

    if isinstance(exc, _RecordFailure):
        return exc.code
    if isinstance(exc, c.NoRecordsError):
        return c.RecordCode.NO_RECORDING
    if isinstance(exc, ElogSinkError):
        return c.RecordCode.SINK
    if isinstance(exc, ElogFetchError):
        return c.RecordCode.FETCH
    if isinstance(exc, OSError) and (exc.filename or exc.errno in {
            errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EROFS, errno.EEXIST}):
        return c.RecordCode.SINK
    if isinstance(exc, ScpiError) and exc.code in (323, 310):
        return c.RecordCode.BAD_CONFIG if exc.code == 323 else c.RecordCode.ELOG_UNSUPPORTED
    scpi = re.search(r"(?:^|[\s:])[+-](323|310),", str(exc))  # SCPI error numbers are always signed
    if scpi:
        return c.RecordCode.BAD_CONFIG if scpi[1] == "323" else c.RecordCode.ELOG_UNSUPPORTED
    if phase == "connect":
        return c.RecordCode.CONNECT
    if phase == "config" and isinstance(exc, ValueError):
        return c.RecordCode.BAD_CONFIG
    return c.RecordCode.FETCH


def _status_values(status):
    """Detach backend numeric scalars before they reach JSON serialization."""
    conversions = {"elapsed_s": float, "records": int, "gaps": int, "preview": bool}
    return {key: convert(status[key]) for key, convert in conversions.items() if key in status}


class RecordApi:
    def __init__(self, *args, **kwargs):
        self._record_lock = threading.Lock()
        self._record_backend = None
        self._record_stop_event = None
        self._record_thread = None
        self._record_last_path = None
        self._record_exception = None
        self._record_notes = []
        self._record_state = {"run_id": "", "state": "idle", "elapsed_s": 0,
                              "records": 0, "gaps": 0, "preview": False}
        super().__init__(*args, **kwargs)

    def record_meta(self) -> c.RecordMeta:
        return {"enabled": c.RECORD_ENABLED, "snapshot_poll_ms": c.SNAPSHOT_POLL_MS,
                "snapshot_poll_min_ms": c.SNAPSHOT_POLL_MIN_MS,
                "snapshot_max_points": c.SNAPSHOT_MAX_POINTS, "max_channels": c.MAX_CHANNELS,
                "period_floor_table": {str(k): v for k, v in c.PERIOD_FLOOR_TABLE.items()},
                "period_max_s": c.PERIOD_MAX_S, "period_granularity_s": c.PERIOD_GRANULARITY_S,
                "duration_min_s": c.DURATION_MIN_S, "duration_max_s": c.DURATION_MAX_S,
                "dlog_fallback_max_bytes": c.DLOG_FALLBACK_MAX_BYTES}

    def record_start(self, config: dict, resources: list[str], demo: bool) -> dict:
        if not c.RECORD_ENABLED:
            return _error(c.RecordCode.DISABLED)
        try:
            cfg, resources, demo = _config_copy(config, resources, demo)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            return _error(c.RecordCode.BAD_CONFIG, str(exc))
        token = self._reserve("power")
        if token is None:
            state, _ = self._gate.state()
            code = c.RecordCode(state.value) if state.value != "idle" else c.RecordCode.POWER
            return _error(code)
        handed_off = False
        start_error = None
        path = None
        try:
            path = self._record_save_path()
            if path is None:
                return {"cancelled": True}
            target_state = {}
            for target in (path, Path(str(path) + ".json")):
                try:
                    target_state[target] = target.stat(follow_symlinks=False)
                except FileNotFoundError:
                    target_state[target] = None
            stop = threading.Event()
            with self._record_lock:
                self._record_backend = None
                self._record_last_path = None
                self._record_exception = None
                self._record_notes = []
                self._record_stop_event = stop
                self._record_state = {"run_id": cfg["run_id"], "state": "starting", "elapsed_s": 0,
                                      "records": 0, "gaps": 0, "preview": False, "name": path.name}
                self._emit([c.EVENT_RECORD, deepcopy(self._record_state)])
                try:
                    worker = threading.Thread(target=self._record_work,
                                              args=(cfg, resources, demo, path, stop, token, target_state), daemon=True)
                    self._record_thread = worker
                    worker.start()
                except Exception as exc:
                    start_error = exc
                    raise
                handed_off = True
            return {}
        except Exception as exc:
            return _error(_failure_code(exc, "connect"), self._record_detail(exc, path))
        finally:
            if not handed_off:
                if start_error is not None:
                    with self._record_lock:
                        released = self._release(token)
                        self._record_exception = start_error
                        self._record_stop_event = None
                        self._record_thread = None
                        code = c.RecordCode.CONNECT if released else c.RecordCode.STUCK
                        self._record_state.update(state="failed", **_error(code, str(start_error)))
                        self._emit([c.EVENT_RECORD, deepcopy(self._record_state)])
                else:
                    self._release(token)

    def _record_save_path(self):
        if not self._dialog_lock.acquire(blocking=False):
            raise _RecordFailure(c.RecordCode.SINK, "another file dialog is open")
        try:
            path = self._dialog_path(self._window.create_file_dialog(
                webview.FileDialog.SAVE, file_types=(tr("record_file_type"),)))
            if not path:
                return None
            if Path(path).suffix.lower() == ".dlog":
                path = str(Path(path).with_suffix(".dlog"))
            else:
                path += ".dlog"
            final = Path(path)
            try:
                info = final.lstat()
            except FileNotFoundError:
                pass
            else:
                if (stat.S_ISDIR(info.st_mode) or (stat.S_ISLNK(info.st_mode)
                        and (not final.exists() or final.is_dir()))):
                    raise _RecordFailure(c.RecordCode.SINK, f"{final.name} is not a regular file")
            # Part files belong to interrupted writers; never overwrite or delete them.
            for part in (Path(str(final) + ".part"), Path(str(final) + ".json.part")):
                if part.exists() or part.is_symlink():
                    raise _RecordFailure(c.RecordCode.SINK, f"{part.name} already exists")
            for existing in (final, Path(str(final) + ".json")):
                if existing.exists() and not self._confirm(
                        tr("confirm"), tr("overwrite_confirm", name=existing.name)):
                    return None
            try:
                paths = _paths(final)
            except ValueError as exc:
                raise _RecordFailure(c.RecordCode.SINK, str(exc)) from exc
            if not _safe_paths(paths):
                raise _RecordFailure(c.RecordCode.SINK, f"Unsafe DLOG path: {final.name}")
            return final
        finally:
            self._dialog_lock.release()

    def record_stop(self) -> dict:
        with self._record_lock:
            if self._record_stop_event is None:
                return _error(c.RecordCode.NO_RECORDING)
            self._record_stop_event.set()
        return {}

    def record_status(self) -> c.RecordStatus:
        with self._record_lock:
            return deepcopy(self._record_state)

    def record_snapshot(self, since_seq: int) -> c.SnapshotResult:
        if type(since_seq) is not int:
            raise TypeError("since_seq must be an int")
        if since_seq < -1:
            raise ValueError("since_seq must be at least -1")
        with self._record_lock:
            backend = self._record_backend
        if backend is None:
            return {"run_id": "", "seq": 0, "preview": False, "period_s": 0, "keys": {}}
        return deepcopy(backend.snapshot(since_seq))

    def record_load(self, gen: int) -> PathResult:
        if type(gen) is not int or self._gate.state() != ("analysis", gen):
            return {"ok": False, **_error(c.RecordCode.STALE)}
        with self._record_lock:
            path = self._record_last_path
            if path is None or self._record_stop_event is not None:
                return {"ok": False, **_error(c.RecordCode.NO_RECORDING)}
            grant = self._gate.issue_grant(gen, "open", "dlog", path)
        if grant is None:
            return {"ok": False, **_error(c.RecordCode.STALE)}
        return {"ok": True, "gen": gen, "grant": grant.id, "name": path.name}

    @staticmethod
    def _record_detail(exc, path):
        detail = str(exc)
        paths = [path] if path is not None else []
        if isinstance(exc, OSError):
            paths.extend(Path(value) for value in (exc.filename, exc.filename2) if value)
        for item in paths:
            detail = detail.replace(str(item), item.name)
            detail = detail.replace(repr(str(item))[1:-1], item.name)
        return detail

    def _record_emit(self, status, run_id):
        # Whitelist fields so backend-private paths/notes never cross the bridge.
        with self._record_lock:
            if (self._record_state["run_id"] != run_id
                    or self._record_state["state"] not in c.ACTIVE_STATES
                    or status.get("state") not in c.ACTIVE_STATES):
                return
            update = {"state": status["state"], **_status_values(status)}
            self._record_state.update(deepcopy(update))
            self._emit([c.EVENT_RECORD, deepcopy(self._record_state)])

    def _record_work(self, cfg, resources, demo, final_path, stop, token, target_state=None):
        inst = None
        result = error = None
        phase = "connect"
        released = False
        try:
            self._finishing(token)
            # Import only at execution time: Wave 2 has no real backend yet.
            from .record.backend import make_backend

            if not demo and e36441a.kind_of([
                    e36441a.detect_model(resources[cfg["machine_index"]].strip())]) == "E36441A":
                raise _RecordFailure(c.RecordCode.BAD_CONFIG, "E36441A cannot record DLOG")
            group = (InstrumentGroup.demo(frames=1, slots=DEMO_SLOTS) if demo
                     else connect_group([resources[cfg["machine_index"]].strip()]))
            inst = group.instruments[0]
            if e36441a.kind_of([inst.identity.model]) == "E36441A":
                raise _RecordFailure(c.RecordCode.BAD_CONFIG, "E36441A cannot record DLOG")
            phase = "config"
            target_options = {} if target_state is None else {"target_state": target_state}
            backend = make_backend(inst, cfg, final_path, **target_options,
                                   untimed=lambda: self._untimed(token),
                                   finishing=lambda: self._finishing(token))
            with self._record_lock:
                self._record_backend = backend
            phase = "fetch"
            result = backend.run(stop, lambda status: self._record_emit(status, cfg["run_id"]))
            required = {"run_id", "final_path", "elapsed_s", "records", "gaps", "notes", "preview"}
            if not isinstance(result, dict) or not required.issubset(result):
                raise ValueError("backend returned an incomplete recording result")
            if (not isinstance(result["notes"], list)
                    or any(not isinstance(note, str) for note in result["notes"])):
                raise ValueError("backend notes must be a list of strings")
            result = {**result, **_status_values(result), "notes": list(result["notes"])}
            if result["run_id"] != cfg["run_id"] or Path(result["final_path"]) != final_path:
                raise _RecordFailure(c.RecordCode.SINK, "backend returned a different recording")
            if result["records"] < 1:
                raise _RecordFailure(c.RecordCode.NO_RECORDING, "recording contains no complete records")
        except BaseException as exc:
            result = None
            error = exc
        finally:
            # The backend armed the finish timer BEFORE its tail/ABOR/finalize.
            # Keep that deadline through close; never reset it here.
            try:
                if inst is not None:
                    inst.close()
            except BaseException as exc:
                if error is None:
                    error = exc
                else:
                    error.add_note(f"instrument close failed: {exc!r}")
            finally:
                # Serialize terminal publication with the next accepted run.
                with self._record_lock:
                    released = self._release(token)
                    self._record_exception = error
                    self._record_stop_event = None
                    self._record_notes = list(result["notes"]) if result is not None else []
                    if error is not None:
                        self._record_notes.append(repr(error))
                        self._record_notes.extend(getattr(error, "__notes__", []))
                    status = self._record_state
                    if result is not None:
                        status.update({key: result[key] for key in
                                       ("elapsed_s", "records", "gaps", "preview")})
                    if error is None and result is not None and released:
                        self._record_last_path = final_path
                        status["state"] = "done"
                    else:
                        self._record_last_path = None
                        code = c.RecordCode.STUCK if not released else _failure_code(error, phase)
                        status.update(state="failed", **_error(code, self._record_detail(error, final_path)))
                    self._emit([c.EVENT_RECORD, deepcopy(status)])
                    for note in result["notes"] if result is not None else []:
                        self._emit(["result", "warning", tr("record_title"),
                                    tr("record_note", note=self._record_detail(note, final_path))])
