"""Streaming average-only DLOG files and conservative recovery of our own parts.

The caller-owned sidecar dictionary stays live: the worker may add gaps, notes,
and measured elapsed_s before finalize/abort. Only this calling worker writes.
The registry excludes writers and recovery within this process, not other apps.
"""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import threading
import time
import uuid
from xml.sax.saxutils import escape

import numpy as np

__all__ = ["DlogWriter", "elog_block", "recover_part", "replace_with_retry"]

_PRODUCER = "n6705c_seq.record"
_MAX_RECORDS = 2**32 - 1
_MAX_HEADER = 1024**2
_TIME_WIDTH = 24
_registry_lock = threading.Lock()
_active = set()
_REPLACE_DELAYS = (0.05, 0.1, 0.2, 0.4)


def replace_with_retry(src, dst):
    """Replace atomically, tolerating brief Windows antivirus/indexer locks."""
    for attempt in range(len(_REPLACE_DELAYS) + 1):
        try:
            return os.replace(src, dst)
        except PermissionError as exc:
            if (os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33}
                    or attempt == len(_REPLACE_DELAYS)):
                raise
            time.sleep(_REPLACE_DELAYS[attempt])


def _claim(path):
    # Directory aliases must share the registry key; never resolve the leaf.
    path = path.parent.resolve() / path.name
    with _registry_lock:
        if path in _active:
            return False
        _active.add(path)
        return True


def _release(path):
    path = path.parent.resolve() / path.name
    with _registry_lock:
        _active.discard(path)


def _channels(channels):
    if (not isinstance(channels, list) or not channels
            or any(type(ch) is not int or not 1 <= ch <= 4 for ch in channels)
            or len(set(channels)) != len(channels)):
        raise ValueError("Expected distinct channels in 1..4")
    return sorted(channels)


def _settings(channels, sense_volt, sense_curr, tint):
    channels = _channels(channels)
    if type(sense_volt) is not bool or type(sense_curr) is not bool or not (sense_volt or sense_curr):
        raise ValueError("Enable voltage and/or current averages")
    if isinstance(tint, bool) or not isinstance(tint, (int, float)) or not math.isfinite(tint) or tint <= 0:
        raise ValueError("Expected a finite positive sampling period")
    return channels, float(tint)


def _utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_valid(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0
    except ValueError:
        return False


def _text(value):
    if not isinstance(value, str) or not value or any(
        not (c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF
             or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
        for c in value
    ):
        raise ValueError("Expected nonempty XML text")
    return escape(value)


def _duration(records, tint):
    value = records * tint
    text = f"{value:024.9f}"
    if not math.isfinite(value) or len(text) != _TIME_WIDTH:
        raise ValueError("Saved duration exceeds the 24-byte time field")
    return text.encode("ascii")


def _header(name, run_id, channels, sense_volt, sense_curr, tint, models, started):
    # Structure and unused defaults follow default_20260803_092740.dlog.
    # Its option names (1ua/2ua) are instrument syntax, not standard XML.
    # Literal date quotes match the fixture; UTC date syntax awaits HW-P2.
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id) or "--" in run_id:
        raise ValueError("Unsafe record run_id")
    if not _utc_valid(started):
        raise ValueError("Expected a UTC started_utc")
    lines = ["<!-- N6700X dlog settings -->", "<dlog>", f"\t<!-- record_run_id={run_id} -->"]
    for ch in channels:
        lines.append(f'\t<channel id="{ch}">')
        for tag, value in (("curr_trig_lev", 0), ("volt_trig_lev", 0),
                           ("curr_trig_slope", 0), ("volt_trig_slope", 0),
                           ("sense_volt", int(sense_volt)), ("sense_curr", int(sense_curr)),
                           ("volt_range", 0), ("curr_range", 0),
                           ("curr_auto_range", 0), ("volt_auto_range", 0)):
            lines.append(f"\t\t<{tag}>{value}</{tag}>")
        lines.extend(["\t\t<ident>", f"\t\t\t<model>{_text(models[str(ch)])}</model>", "\t\t\t<option>"])
        for tag in ("1ua", "2ua", "lga", "relay", "reverse"):
            lines.append(f"\t\t\t\t<{tag}>0</{tag}>")
        lines.extend(["\t\t\t</option>", "\t\t</ident>", "\t</channel>"])
    lines.extend(["\t<frame>", "\t\t<sense_minmax>0</sense_minmax>",
                  "\t\t<trig_source>1</trig_source>",
                  f"\t\t<time>{_duration(0, tint).decode()}</time>",
                  "\t\t<offset>0</offset>", f"\t\t<tint>{tint!r}</tint>",
                  f'\t\t<date>"{_text(started)}"</date>', "\t</frame>"])
    for ch in channels:
        lines.append(f'\t<gui_chan id="{ch}">')
        for tag, value in (("volt_trace", int(sense_volt)), ("curr_trace", int(sense_curr)),
                           ("volt_gain", 2), ("volt_offset", 0), ("curr_gain", 0.05),
                           ("curr_offset", 0), ("power_trace", int(sense_volt and sense_curr)),
                           ("power_gain", 0.05), ("power_offset", 0)):
            lines.append(f"\t\t<{tag}>{value}</{tag}>")
        lines.append("\t</gui_chan>")
    lines.extend(["\t<gui_frame>", "\t\t<horiz_gain>1</horiz_gain>",
                  "\t\t<horiz_offset>0</horiz_offset>", "\t\t<date_time>1</date_time>",
                  f'\t\t<base_filename>"{_text(name)}"</base_filename>',
                  "\t\t<marker1_pos>0</marker1_pos>", "\t\t<marker2_pos>0</marker2_pos>",
                  "\t</gui_frame>", "</dlog>", ""])
    header = "\n".join(lines).encode("utf-8")
    if len(header) > _MAX_HEADER:
        raise ValueError("DLOG header too large")
    return header, header.index(b"<time>") + len(b"<time>")


def _paths(final):
    final = Path(final)
    if not final.name.lower().endswith(".dlog") or final.name.lower() == ".dlog" or "\\" in final.name:
        raise ValueError("Expected a .dlog basename")
    if ".." in final.parts:
        raise ValueError("Parent traversal is not allowed")
    final = final.absolute()
    return final, Path(str(final) + ".part"), Path(str(final) + ".json"), Path(str(final) + ".json.part")


def _safe_paths(paths):
    # Allow directory aliases (e.g. macOS /tmp); reject unsafe leaf files.
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
    return True


def _identity(path):
    info = path.lstat()
    return info.st_dev, info.st_ino


def _json_bytes(meta):
    return (json.dumps(meta, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _write_sidecar(path, meta):
    payload = _json_bytes(meta)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _patch(stream, meta, records, tint):
    duration = _duration(records, tint)
    stream.truncate(meta["header_bytes"] + 8 + records * meta["record_bytes"])
    stream.seek(meta["time_offset"])
    stream.write(duration)
    stream.seek(meta["header_bytes"])
    stream.write(struct.pack(">II", 0, records))
    stream.flush()
    os.fsync(stream.fileno())


def _fsync_directory(path):
    if os.name == "nt":
        return  # Windows does not support opening directories this way.
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # Best effort on filesystems without directory fsync support.


def _publish(paths, meta):
    final, part, side, side_part = paths
    incomplete = dict(meta, complete=False, ended_utc=None)
    _write_sidecar(side_part, incomplete)
    replace_with_retry(side_part, side)
    replace_with_retry(part, final)
    try:
        _write_sidecar(side, meta)
    except Exception as exc:
        exc.add_note(
            f"DLOG data is already in place at {final}, but the completion marker "
            "was not written; the sidecar remains complete=False and no .part remains."
        )
        raise
    _fsync_directory(final.parent)
    return final


class DlogWriter:
    """One worker, exclusive temporary files, sidecar-first publication.

    Sidecar defaults are filled in place; backend-supplied requested/actual,
    gaps/notes and elapsed_s are retained. abort is required after append errors.
    """

    def __init__(self, final_path: Path, *, channels: list[int], sense_volt: bool,
                 sense_curr: bool, tint: float, models: dict[int, str], sidecar: dict):
        channels, tint = _settings(channels, sense_volt, sense_curr, tint)
        self._paths = _paths(final_path)
        final, part, _, side_part = self._paths
        if not _safe_paths(self._paths):
            raise ValueError("Unsafe DLOG path")
        self._sidecar = sidecar
        self._elapsed_supplied = "elapsed_s" in sidecar
        self._started = time.monotonic()
        self._tint = tint
        self._records = 0
        self._stream = None
        self._owned = {}
        self._closed = False
        self._failed = False
        actual = dict(sidecar.get("actual", {}))
        actual.update(channels=channels, sense_volt=sense_volt, sense_curr=sense_curr, period_s=tint)
        actual.setdefault("duration_s", sidecar.get("requested", {}).get("duration_s"))
        sidecar.setdefault("run_id", uuid.uuid4().hex)
        sidecar.setdefault("started_utc", _utc())
        model_names = {str(ch): models[ch] for ch in channels}
        header, offset = _header(final.name, sidecar["run_id"], channels, sense_volt,
                                 sense_curr, tint, model_names, sidecar["started_utc"])
        self._fixed = dict(schema_version=1, producer=_PRODUCER, run_id=sidecar["run_id"],
                           name=final.name, actual=actual, models=model_names,
                           started_utc=sidecar["started_utc"], header_bytes=len(header),
                           record_bytes=len(channels) * (sense_volt + sense_curr) * 4,
                           time_offset=offset, time_width=_TIME_WIDTH)
        sidecar.update(self._fixed)
        for key, value in dict(backend="elog", preview=False, requested={}, idn="", ended_utc=None,
                               elapsed_s=0, records=0, gaps=[], notes=[], complete=False).items():
            sidecar.setdefault(key, value)
        sidecar.update(records=0, complete=False, ended_utc=None)
        _recovery_layout(sidecar, final)
        _json_bytes(sidecar)  # Fail before creating files for unserializable input.
        if not _claim(final):
            raise FileExistsError("DLOG writer or recovery is already active")
        try:
            self._stream = part.open("xb+")
            self._owned[part] = _identity(part)
            with side_part.open("xb"):
                self._owned[side_part] = _identity(side_part)
            self._stream.write(header + struct.pack(">II", 0, 0))
            self._stream.flush()
            os.fsync(self._stream.fileno())
            _write_sidecar(side_part, sidecar)
            self._owned[side_part] = _identity(side_part)
        except BaseException:
            self.abort(keep_part=False)
            raise

    def append(self, block) -> None:
        if self._closed or self._failed:
            raise RuntimeError("DLOG writer is closed or has failed")
        if (not isinstance(block, np.ndarray) or block.dtype.kind != "f" or block.dtype.itemsize != 4
                or block.ndim != 2 or block.shape[1] * 4 != self._fixed["record_bytes"]):
            raise ValueError("Expected float32[n, n_fields] in channel V,I order")
        count = self._records + len(block)
        if count > _MAX_RECORDS:
            raise ValueError("DLOG UInt32 record limit exceeded")
        try:
            payload = block.astype(">f4", copy=False).tobytes(order="C")
            if self._stream.write(payload) != len(payload):
                raise OSError("Short DLOG write")
            self._stream.flush()
        except BaseException:
            self._failed = True
            raise
        self._records = count

    def _metadata(self, complete):
        meta = dict(self._sidecar)
        meta.update(self._fixed, records=self._records, complete=complete)
        if complete:
            meta["ended_utc"] = _utc()
        if not self._elapsed_supplied and not meta["elapsed_s"]:
            meta["elapsed_s"] = time.monotonic() - self._started
        return meta

    def finalize(self) -> Path:
        if self._closed or self._failed:
            raise RuntimeError("DLOG writer is closed or has failed")
        if not 1 <= self._records <= _MAX_RECORDS:
            raise ValueError("Cannot finalize a zero-record DLOG")
        _duration(self._records, self._tint)
        try:
            meta = self._metadata(True)
            _json_bytes(meta)
            _patch(self._stream, meta, self._records, self._tint)
            self._stream.close()  # Windows requires this before replace.
            result = _publish(self._paths, meta)
            self._sidecar.update(meta)
            return result
        finally:
            try:
                self._stream.close()
            finally:
                self._closed = True
                _release(self._paths[0])

    def abort(self, keep_part: bool = True) -> None:
        if self._closed:
            return
        try:
            if self._stream is not None:
                try:
                    self._stream.flush()
                    if keep_part:
                        os.fsync(self._stream.fileno())
                finally:
                    self._stream.close()
            side_part = self._paths[3]
            if keep_part and side_part in self._owned:
                _write_sidecar(side_part, self._metadata(False))
                self._owned[side_part] = _identity(side_part)
        finally:
            try:
                if not keep_part:
                    for path, identity in self._owned.items():
                        if path.exists() and not path.is_symlink() and _identity(path) == identity:
                            path.unlink()
            finally:
                self._closed = True
                _release(self._paths[0])


def elog_block(data: dict[int, np.ndarray], fields: tuple[str, ...], channels: list[int]) -> np.ndarray:
    """Select averages only; sorted channels, each channel's voltage then current."""
    channels = _channels(channels)
    if len(set(fields)) != len(fields):
        raise ValueError("Duplicate ELOG fields")
    columns = [fields.index(name) for name in ("V_avg", "I_avg") if name in fields]
    if not columns:
        raise ValueError("No average ELOG fields")
    arrays = []
    length = None
    for ch in channels:
        if ch not in data:
            raise ValueError("Missing selected ELOG channel")
        array = np.asarray(data[ch])
        if array.ndim != 2 or array.shape[1] != len(fields):
            raise ValueError("ELOG shape does not match fields")
        if length is not None and len(array) != length:
            raise ValueError("Unaligned ELOG channel lengths")
        length = len(array)
        arrays.append(array[:, columns])
    return np.concatenate(arrays, axis=1).astype(np.float32, copy=False)


def _recovery_layout(meta, final):
    """Validate self-description before trusting any offset or mutating bytes."""
    if (meta["schema_version"] != 1 or type(meta["schema_version"]) is not int
            or meta["producer"] != _PRODUCER or meta["name"] != final.name
            or meta["backend"] not in ("elog", "dlog") or type(meta["preview"]) is not bool
            or type(meta["complete"]) is not bool
            or type(meta["records"]) is not int or not 0 <= meta["records"] <= _MAX_RECORDS
            or not isinstance(meta["requested"], dict)
            or not isinstance(meta["idn"], str) or not isinstance(meta["notes"], list)
            or not all(isinstance(note, str) for note in meta["notes"])
            or not isinstance(meta["gaps"], list)
            or (meta["ended_utc"] is not None and not _utc_valid(meta["ended_utc"]))):
        raise ValueError("Invalid recording sidecar")
    elapsed = meta["elapsed_s"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (float, int)) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("Invalid elapsed_s")
    for gap in meta["gaps"]:
        if (not isinstance(gap["kind"], str) or type(gap["index"]) is not int or gap["index"] < 0
                or (gap["count"] is not None and (type(gap["count"]) is not int or gap["count"] < 0))):
            raise ValueError("Invalid gap")
    actual = meta["actual"]
    channels, tint = _settings(actual["channels"], actual["sense_volt"], actual["sense_curr"], actual["period_s"])
    if channels != actual["channels"]:
        raise ValueError("Unsorted channels")
    header, offset = _header(final.name, meta["run_id"], channels, actual["sense_volt"],
                             actual["sense_curr"], tint, meta["models"], meta["started_utc"])
    expected = dict(header_bytes=len(header), record_bytes=len(channels) *
                    (actual["sense_volt"] + actual["sense_curr"]) * 4,
                    time_offset=offset, time_width=_TIME_WIDTH)
    if any(type(meta[key]) is not int or meta[key] != value for key, value in expected.items()):
        raise ValueError("Sidecar offsets do not match header/fields")
    return header, offset, tint


def recover_part(part: Path) -> Path | None:
    """Recover a closed, matching recording part; reject foreign files unchanged.

    Invalid/unsafe inputs return None. Real I/O failures propagate. The data part
    remains available until replaced into final; a later completion-marker
    failure leaves the data in final with an incomplete sidecar and no part.
    No pathname from the JSON is ever used to select an input or output file.
    """
    part = Path(part)
    if not part.name.endswith(".dlog.part"):
        return None
    try:
        paths = _paths(part.with_name(part.name[:-5]))
    except ValueError:
        return None
    final, part, side, side_part = paths
    if not _safe_paths(paths) or not part.exists() or not _claim(final):
        return None
    try:
        source_side = side_part if side_part.exists() else side
        if not source_side.exists():
            return None
        # Opening with NOFOLLOW also guards the leaf between lstat and open.
        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(source_side, os.O_RDONLY | flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != _identity(source_side):
                return None
            try:
                meta = json.load(stream)
            except (ValueError, UnicodeError):
                return None
        try:
            header, offset, tint = _recovery_layout(meta, final)
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
            return None
        with os.fdopen(os.open(part, os.O_RDWR | flags), "r+b") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != _identity(part):
                return None
            size = info.st_size
            raw = stream.read(len(header) + 8)
            if len(raw) != len(header) + 8:
                return None
            if raw[:offset] != header[:offset] or raw[offset + _TIME_WIDTH:len(header)] != header[offset + _TIME_WIDTH:]:
                return None
            duration = raw[offset:offset + _TIME_WIDTH]
            if not re.fullmatch(rb"[0-9]{14}\.[0-9]{9}", duration):
                return None
            records = (size - len(header) - 8) // meta["record_bytes"]
            trigger, _ = struct.unpack(">II", raw[-8:])
            # A torn final index is irrelevant: only the byte length determines N.
            if not 1 <= records <= _MAX_RECORDS or trigger != 0:
                return None
            meta.update(records=records, complete=True, recovered=True, ended_utc=_utc())
            meta["notes"].append("Recovered complete records from an interrupted recording; any partial record was discarded.")
            try:
                _duration(records, tint)
                _json_bytes(meta)
            except (ValueError, UnicodeError):
                return None
            if not _safe_paths(paths):
                return None
            _patch(stream, meta, records, tint)
        return _publish(paths, meta)
    finally:
        _release(final)
