"""Single-worker internal DLOG capture with a separate MEAS preview."""

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import math
import os
from pathlib import Path
import re
import struct
from threading import Event
import time
from types import SimpleNamespace
import uuid

import numpy as np

from n6705c.core.dlog import DlogConfig, DlogSession
from n6705c.core.parsing import parse_definite_block
from n6705c.core.scpi import format_channel_list

from . import contracts as c
from .dlogfile import (
    _claim, _fsync_directory, _paths, _release, _safe_paths, _write_sidecar,
    replace_with_retry,
)
from .snapshot import Envelope

_PREVIEW_PERIOD = 0.025
_PERIOD_QUANTUM_S = 10.24e-6  # one DLOG time-grid step
_HEADER_LIMIT = 1024 ** 2


def _utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bad_config(message):
    # The bridge enters its fetch phase before run(). Its SCPI-code mapping
    # recognizes +323 even here; a plain ValueError would become fetch.
    return ValueError(f"+323,{message}")


@contextmanager
def _disk(path):
    """Keep the original OSError while identifying descriptor-only sink errors."""
    try:
        yield
    except OSError as exc:
        if exc.filename is None:
            if exc.strerror is None:
                exc.strerror = str(exc)
            exc.filename = os.fspath(path)
        raise


def _bounds(period, duration, fields):
    for name, value, low, high in (
        ("period", period, c.PERIOD_FLOOR_TABLE[fields], c.PERIOD_MAX_S),
        ("duration", duration, c.DURATION_MIN_S, c.DURATION_MAX_S),
    ):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not low <= value <= high):
            raise _bad_config(f"Invalid {name}: {value!r}")
    size = math.ceil(duration / period) * fields * 4 + _HEADER_LIMIT + 8
    if size > c.DLOG_FALLBACK_MAX_BYTES:
        raise _bad_config(f"Estimated DLOG size {size} exceeds {c.DLOG_FALLBACK_MAX_BYTES} bytes")


def _transfer(session, remote, part):
    # Keep transport/parsing outside the sink boundary. Core transfer combines
    # these with write_bytes, whose write/close errors may lack a filename.
    raw = session.query_binary(f'MMEM:DATA? "{remote}"')
    payload, _ = parse_definite_block(raw)
    with _disk(part):
        part.write_bytes(payload)


def _target_state(paths):
    result = {}
    for path in paths:
        try:
            result[path] = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            result[path] = None
    return result


def _same_target(before, after):
    if before is None or after is None:
        return before is after
    # stat fields are portable; fall back to mtime/size without inode support.
    fields = ("st_dev", "st_ino", "st_mtime_ns", "st_size")
    return all(getattr(before, field, None) == getattr(after, field, None)
               for field in fields) and before.st_mtime == after.st_mtime


class _CheckedSession:
    """Add strict, bounded per-command checks without changing core signatures."""

    def __init__(self, session, notes, ranges):
        self.raw = session
        self.notes = notes
        self.ranges = ranges

    def _errors(self):
        errors = []
        for _ in range(50):
            response = self.raw.query("SYST:ERR?")
            match = re.fullmatch(r"\s*([+-]?\d+)\s*,(.*)", response, re.DOTALL)
            if match is None:
                raise RuntimeError(f"Malformed SYST:ERR? response: {response!r}")
            code = int(match[1])
            if code == 0:
                return errors
            errors.append((code, match[2].strip()))
        raise RuntimeError(f"SYST:ERR? did not reach zero: {errors!r}")

    def check_errors(self, context=""):
        errors = self._errors()
        if errors:
            raise RuntimeError("; ".join(f"{code:+d},{text} (after {context})" for code, text in errors))

    def write(self, command):
        self.raw.write(command)
        errors = self._errors()
        if not errors:
            return
        if command == "ABOR:DLOG":
            self.notes.extend(f"{code:+d},{text} (after {command})" for code, text in errors)
            return
        if ":RANG:AUTO ON," in command and all(code == 310 for code, _ in errors):
            quantity = "VOLT" if ":VOLT:" in command else "CURR"
            channels = command.split(",", 1)[1]
            values = self.query_floats(f"SENS:DLOG:{quantity}:RANG? {channels}")
            expected = len(channels.strip("(@)").split(","))
            if len(values) != expected or any(not math.isfinite(v) or v <= 0 for v in values):
                raise _bad_config("Invalid DLOG range readback")
            self.ranges[quantity] = values
            self.notes.extend(f"{code:+d},{text} (after {command}); actual ranges={values}"
                              for code, text in errors)
            return
        raise RuntimeError("; ".join(f"{code:+d},{text} (after {command})" for code, text in errors))

    def query(self, command):
        result = self.raw.query(command)
        self.check_errors(command)
        return result

    def query_float(self, command):
        response = self.query(command)
        try:
            return float(response)
        except ValueError as exc:
            raise _bad_config(f"Invalid {command} readback: {response!r}") from exc

    def query_floats(self, command):
        return [float(value) for value in self.query(command).split(",")]

    def query_binary(self, command):
        result = self.raw.query_binary(command)
        self.check_errors(command)
        return result


def _layout(part, cfg, period):
    """Validate native bytes without importing analysis or rewriting the file.

    Native headers need not carry our marker or fixed-width time field. They
    deliberately remain outside DlogWriter's recover_part format.
    """
    size = part.stat().st_size
    if size > c.DLOG_FALLBACK_MAX_BYTES:
        raise _bad_config("Downloaded DLOG exceeds the fallback size limit")
    with part.open("rb") as stream:
        prefix = stream.read(_HEADER_LIMIT)
    end = prefix.find(b"</dlog>\n")
    if end < 0:
        raise ValueError("Missing native DLOG header")
    end += len(b"</dlog>\n")
    header = prefix[:end].decode("utf-8")
    fields = []
    for channel in re.finditer(r'<channel id="(\d+)">([\s\S]*?)</channel>', header):
        for kind, tag in (("V", "volt"), ("I", "curr")):
            if re.search(fr"<sense_{tag}>\s*1\s*</sense_{tag}>", channel[2]):
                fields.append(f"{kind}{int(channel[1])}")
    expected = [f"{kind}{ch}" for ch in cfg["channels"]
                for kind, enabled in (("V", cfg["sense_volt"]), ("I", cfg["sense_curr"])) if enabled]
    if fields != expected:
        raise ValueError("Native DLOG fields differ from the selected channels/functions")
    def value(tag):
        match = re.search(fr"<{tag}>\s*([^<]+)</{tag}>", header)
        if match is None:
            raise ValueError(f"Missing DLOG {tag}")
        return float(match[1])
    tint = value("tint")
    if (not math.isfinite(tint) or not math.isclose(tint, period, rel_tol=1e-6)
            or value("sense_minmax") != 0 or value("offset") != 0):
        raise ValueError("Unexpected native DLOG period/minmax/offset")
    time_match = re.search(rb"<time>([^<]+)</time>", prefix[:end])
    if time_match is None or not math.isfinite(float(time_match[1])):
        raise ValueError("Invalid native DLOG time")
    record_bytes = len(fields) * 4
    payload = size - end - 8
    if payload < 0 or payload % record_bytes:
        raise ValueError("Truncated native DLOG record")
    with part.open("rb") as stream:
        stream.seek(end)
        trigger, write = struct.unpack(">II", stream.read(8))
    records = payload // record_bytes
    # Real N6705C (E.02.09) BUS-triggered logs carry trigger=0xFFFFFFFF ("no trigger") and a write
    # index that is not the record count; the byte length alone fixes N, so only the sentinel is allowed.
    if trigger not in (0, 0xFFFFFFFF) or records > 2 ** 32 - 1:
        raise ValueError("Unsupported native DLOG indices")
    if records == 0:
        raise c.NoRecordsError("Recording contains no complete records")
    layout = dict(header_bytes=end, record_bytes=record_bytes, time_offset=time_match.start(1),
                  time_width=len(time_match[1]), records=records)
    # trigger=0xFFFFFFFF alone is the normal BUS-trigger sentinel; warn only when write != count.
    if write != records:
        layout["note"] = (f"Native DLOG write index {write} differs from the "
                          f"{records} records implied by file size (trigger={trigger}); samples are taken in file order "
                          "(ring-buffer ordering is unverified).")
    return layout


class _DlogBackend:
    def __init__(self, inst, cfg, final_path, *, untimed, finishing, target_state=None):
        self._inst = inst
        self._cfg = deepcopy(cfg)
        self._cfg.setdefault("run_id", uuid.uuid4().hex)
        self._cfg["channels"] = sorted(self._cfg["channels"])
        self._final = Path(final_path)
        self._target_state = target_state
        self._untimed = untimed
        self._finishing = finishing
        self._used = False
        # Real firmware rejects MEAS during DLOG (+319); only demo sessions
        # supply a separate live preview. Keep every published flag consistent.
        self._preview = str(inst.session.description).startswith("Demo mode")
        keys = [f"{kind}{ch}" for ch in self._cfg["channels"]
                for kind, enabled in (("V", cfg["sense_volt"]), ("I", cfg["sense_curr"])) if enabled]
        self._keys = keys
        self._envelope = Envelope(keys, _PREVIEW_PERIOD if self.preview else self._cfg["period_s"],
                                  run_id=self._cfg["run_id"])
        self._envelope.preview = self.preview

    @property
    def preview(self):
        return self._preview

    def snapshot(self, since_seq):
        return self._envelope.snapshot(since_seq)

    def run(self, stop: Event, emit: c.EmitStatus) -> c.RunResult:
        self._finishing()
        if self._used:
            raise RuntimeError("A recording backend can only run once")
        self._used = True
        cfg = self._cfg
        notes, gaps, ranges = [], [], {}
        started = None
        elapsed = 0.0
        records = 0
        state, last_emit = None, 0.0

        def status(next_state):
            nonlocal state, last_emit
            now = time.monotonic()
            if state != next_state or now - last_emit >= 0.5:
                state, last_emit = next_state, now
                emit(dict(run_id=cfg["run_id"], state=state, elapsed_s=elapsed,
                          records=records, gaps=len(gaps), preview=self.preview, name=self._final.name))

        status("starting")
        fields = len(cfg["channels"]) * (cfg["sense_volt"] + cfg["sense_curr"])
        _bounds(cfg["period_s"], cfg["duration_s"], fields)
        if any(ch not in self._inst.populated_channels for ch in cfg["channels"]):
            raise _bad_config("Selected DLOG channel is not populated")
        try:
            paths = _paths(self._final)
        except ValueError as exc:
            raise OSError(errno.EINVAL, str(exc), str(self._final)) from exc
        final, part, side, side_part = paths
        if not _safe_paths(paths):
            raise OSError(errno.EINVAL, "Unsafe DLOG path", str(final))
        targets = (self._target_state if self._target_state is not None
                   else _target_state((final, side)))
        remote = f"internal:\\record_{uuid.uuid4().hex}.dlog"
        checked = _CheckedSession(self._inst.session, notes, ranges)
        session = DlogSession(SimpleNamespace(session=checked, populated_channels=self._inst.populated_channels),
                              DlogConfig(channels=cfg["channels"], measure_voltage=cfg["sense_volt"],
                                         measure_current=cfg["sense_curr"], minmax=False,
                                         period=cfg["period_s"], duration=cfg["duration_s"],
                                         offset_percent=0, trigger_source="BUS", filename=remote))
        session.configure()
        period, duration = session.actual_period, session.actual_duration
        _bounds(period, duration, fields)
        # The instrument quantises the period to a 10.24 us grid and the duration to whole samples.
        if (abs(period - cfg["period_s"]) > _PERIOD_QUANTUM_S
                or abs(duration - cfg["duration_s"]) > max(2 * period, 1e-6 * cfg["duration_s"])):
            raise _bad_config("DLOG period/duration readback differs from the request")
        if not math.isclose(period, cfg["period_s"], rel_tol=1e-6):
            notes.append(f"Instrument adjusted the DLOG period from {cfg['period_s']} s to {period} s.")
        if not self.preview:
            self._envelope = Envelope(self._keys, period, run_id=cfg["run_id"])
        idn = checked.query("*IDN?")
        meta = dict(schema_version=1, producer="n6705c_seq.record", run_id=cfg["run_id"],
                    backend="dlog", preview=self.preview, name=final.name, requested=deepcopy(cfg),
                    actual=dict(channels=list(cfg["channels"]), sense_volt=cfg["sense_volt"],
                                sense_curr=cfg["sense_curr"], period_s=period, duration_s=duration,
                                ranges=ranges), idn=idn,
                    models={str(ch): self._inst.capabilities[ch].model for ch in cfg["channels"]},
                    started_utc=_utc(), ended_utc=None, elapsed_s=0.0, records=0,
                    header_bytes=0, record_bytes=fields * 4, time_offset=0, time_width=24,
                    gaps=gaps, notes=notes, complete=False, native_dlog=True,
                    layout_pending=True, remote_path=remote)
        notes.append("Native DLOG bytes are preserved; layout is unknown until download and is not recover_part compatible.")
        notes.append("No reliable remote size query is available; the preflight assumes a header no larger than 1 MiB.")
        owned_part = owned_side = False
        attempted = False
        no_records = False
        error = None

        def remember(exc):
            nonlocal error
            if error is None:
                error = exc
            elif error is not exc:
                error.add_note(f"DLOG cleanup failed: {exc!r}")
            notes.append(repr(exc))

        if not _claim(final):
            raise FileExistsError(errno.EEXIST, "Recording path is already active", str(final))
        try:
            # Claim both files exclusively before INIT; remove our claims if
            # capture is never attempted so the same name can be retried.
            with _disk(part):
                with part.open("xb") as stream:
                    owned_part = True
                    stream.flush()
                    os.fsync(stream.fileno())
            with _disk(side_part):
                with side_part.open("xb"):
                    owned_side = True
                _write_sidecar(side_part, meta)
            try:
                if not self._untimed():
                    raise RuntimeError("record owner is no longer active")
                try:
                    if stop.is_set():
                        raise c.NoRecordsError("Recording cancelled before INIT:DLOG")
                    attempted = True
                    session.start()
                    started = time.monotonic()
                    meta["started_utc"] = session.trigger_time_utc
                    status("running")
                    next_sample = started
                    failures = 0
                    # Real firmware rejects MEAS while the logger runs (+319) after a ~5 s VISA
                    # timeout, delaying stop; only the simulator gets a live preview.
                    preview_enabled = self.preview
                    if not preview_enabled:
                        notes.append("No live preview on hardware: MEAS is not allowed while the data logger runs.")
                    chanlist = format_channel_list(cfg["channels"])
                    while True:
                        now = time.monotonic()
                        elapsed = now - started
                        if stop.is_set() or elapsed >= duration:
                            break
                        if not preview_enabled:
                            status("running")
                            stop.wait(min(_PREVIEW_PERIOD, max(0.0, duration - elapsed)))
                            continue
                        try:
                            columns = {}
                            for kind, quantity, enabled in (("V", "VOLT", cfg["sense_volt"]),
                                                            ("I", "CURR", cfg["sense_curr"])):
                                if enabled:
                                    values = checked.query_floats(f"MEAS:{quantity}? {chanlist}")
                                    if len(values) != len(cfg["channels"]):
                                        raise ValueError("MEAS returned an unexpected channel count")
                                    columns.update({f"{kind}{ch}": value for ch, value in zip(cfg["channels"], values)})
                        except Exception as exc:
                            # Failures concern only the preview, not persisted DLOG.
                            notes.append(f"MEAS preview failed: {exc!r}")
                            failures += 1
                            try:
                                checked.check_errors("MEAS failure")
                            except Exception as check_exc:
                                notes.append(f"MEAS error drain failed: {check_exc!r}")
                                preview_enabled = False
                            if failures == 1:
                                notes.append("Preview omits failed MEAS rounds; its uniform time axis compresses those gaps.")
                            if failures >= c.MAX_FETCH_FAILURES:
                                preview_enabled = False
                            if not preview_enabled:
                                notes.append("MEAS preview stopped after errors; native DLOG capture continues.")
                        else:
                            failures = 0
                            self._envelope.append(np.array([[columns[key] for key in self._keys]], dtype=np.float32))
                        status("running")
                        next_sample += _PREVIEW_PERIOD
                        # I/O may be slower than 40 Hz; never spin to catch up.
                        next_sample = max(next_sample, time.monotonic())
                        now = time.monotonic()
                        stop.wait(min(max(0.0, next_sample - now), max(0.0, duration - (now - started))))
                except BaseException as exc:
                    remember(exc)
                finally:
                    self._finishing()
            except BaseException as exc:
                remember(exc)
            # The finish timer is armed before status callbacks, ABOR or disk I/O.
            if started is not None:
                elapsed = time.monotonic() - started
            try:
                status("stopping")
            except BaseException as exc:
                remember(exc)
            if attempted:
                try:
                    session.abort()
                except Exception as exc:
                    notes.append(f"ABOR:DLOG failed; attempting native DLOG download: {exc!r}")
                except BaseException as exc:
                    remember(exc)
                try:
                    if not self._untimed():
                        raise RuntimeError("record owner is no longer active")
                    try:
                        _transfer(checked, remote, part)
                    except BaseException as exc:
                        remember(exc)
                        raise
                    finally:
                        self._finishing()
                    with _disk(part):
                        with part.open("r+b") as stream:
                            stream.flush()
                            os.fsync(stream.fileno())
                    try:
                        layout = _layout(part, cfg, period)
                        if "note" in layout:
                            notes.append(layout.pop("note"))
                        meta.update(layout)
                    except c.NoRecordsError:
                        no_records = True
                        raise
                    meta["layout_pending"] = False
                    records = meta["records"]
                except BaseException as exc:
                    remember(exc)
            meta.update(elapsed_s=elapsed, ended_utc=_utc())
            if error is not None:
                raise error
            current = _target_state(targets)
            conflicts = [path for path, before in targets.items()
                         if not _same_target(before, current[path])]
            if conflicts:
                names = ", ".join(path.name for path in conflicts)
                raise FileExistsError(errno.EEXIST,
                                      f"Target created or changed by another writer: {names}; "
                                      f"recording retained at {part} and {side_part}", str(final))
            with _disk(final):
                # Publish an incomplete sidecar first, including on overwrite.
                # Only mark it complete once the matching data is in place.
                _write_sidecar(side_part, meta)
                replace_with_retry(side_part, side)
                replace_with_retry(part, final)
                meta["complete"] = True
                _write_sidecar(side, meta)
            _fsync_directory(final.parent)
            # Remote cleanup is allowed only after both local publications.
            try:
                checked.write(f'MMEM:DEL "{remote}"')
            except Exception as exc:
                notes.append(f"Published DLOG; remote cleanup failed: {exc!r}")
                try:
                    _write_sidecar(side, meta)
                except Exception as side_exc:
                    # Publication has succeeded; optional remote cleanup and
                    # its warning cannot retroactively invalidate that copy.
                    notes.append(f"Could not persist remote cleanup warning: {side_exc!r}")
            return dict(run_id=cfg["run_id"], final_path=self._final, elapsed_s=elapsed,
                        records=records, gaps=len(gaps), preview=self.preview, notes=list(notes))
        except BaseException as exc:
            try:
                empty = owned_part and part.stat().st_size == 0
            except OSError as cleanup_exc:
                empty = False
                exc.add_note(f"Could not inspect DLOG temporary file: {cleanup_exc!r}")
            if not attempted or no_records or empty:
                for path, owned in ((part, owned_part), (side_part, owned_side)):
                    if owned:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                        except OSError as cleanup_exc:
                            exc.add_note(f"Failed to remove unused DLOG temporary file: {cleanup_exc!r}")
            elif owned_side and part.exists():
                meta.update(complete=False, elapsed_s=elapsed, ended_utc=_utc())
                try:
                    _write_sidecar(side_part, meta)
                except BaseException as side_exc:
                    exc.add_note(f"Failed to persist DLOG failure metadata: {side_exc!r}")
            raise
        finally:
            _release(final)


def make_backend(inst, cfg: c.Config, final_path: Path, *,
                 untimed: c.Untimed, finishing: c.Finishing, target_state=None) -> c.Backend:
    """Construct an inert backend; caller retains instrument/token ownership."""
    kind = c.RECORD_BACKEND
    if kind == "dlog":
        return _DlogBackend(inst, cfg, final_path, untimed=untimed,
                            finishing=finishing, target_state=target_state)
    if kind == "elog":
        raise NotImplementedError("ELOG recording backend is not implemented")
    raise ValueError(f"unknown RECORD_BACKEND {kind!r}")
