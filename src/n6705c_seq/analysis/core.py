"""Path-based DLOG normalization, mapped caches and bounded analysis operations.

Numerical behavior is checked against the frozen original JavaScript in the
parity suite. Cache publication and mapping ownership are coordinated through
analysis.storage; no instrument or GUI modules are imported here.
"""
import hashlib
import json
import math
import os
import secrets
import shutil
import re
import struct
from pathlib import Path

import numpy as np

from . import storage
from .contracts import (CACHE_VERSION, CACHE_MAX_BYTES, CORE_CHUNK_RECORDS, DLOG_MAX_HEADER_BYTES,
                        CORE_MAX_VALUES_SPAN, HASH_CHUNK_BYTES, CSV_ROWS_PER_CHUNK,
                        MAX_BUCKETS, MAX_CSV_KEYS, AnalysisCancelled, LimitExceeded,
                        SourceChanged, CacheWriteError)

_check_cancel = storage.check_cancel

LOD_F0 = 64
LOD_F1 = 4096


class DlogFormatError(ValueError):
    pass


def parse_dlog(buf, *, _destination=None, cancel=None, progress=None) -> dict:
    end_marker = b"</dlog>\n"
    _check_cancel(cancel)
    # Search only a bounded prefix: a file without the marker must not be scanned end to end.
    idx = buf.find(end_marker, 0, DLOG_MAX_HEADER_BYTES)
    if idx == -1 and len(buf) >= DLOG_MAX_HEADER_BYTES:
        raise DlogFormatError("Header exceeds %d bytes or has no </dlog> end marker" % DLOG_MAX_HEADER_BYTES)
    if idx == -1:
        raise DlogFormatError("找不到 </dlog> 結束標記，檔案格式不符")
    header_end = idx + len(end_marker)
    try:
        header_text = buf[:header_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DlogFormatError("Invalid UTF-8 header") from exc

    tint_m = re.search(r"<tint>([^<]+)</tint>", header_text)
    if not tint_m:
        raise DlogFormatError("Header 中找不到 <tint> 欄位")
    tint = float(tint_m.group(1))
    if not math.isfinite(tint) or tint <= 0:
        raise DlogFormatError("Invalid sampling period")

    tm = re.search(r"<time>([^<]+)</time>", header_text)
    declared_time = float(tm.group(1)) if tm else None
    if declared_time is not None and not math.isfinite(declared_time):
        declared_time = None

    fields = []
    for cm in re.finditer(r'<channel id="(\d+)">([\s\S]*?)</channel>', header_text):
        cid = int(cm.group(1))
        if cid < 1 or cid > 4:
            continue
        body = cm.group(2)
        if re.search(r"<sense_volt>\s*1\s*<", body):
            fields.append((cid, "V"))
        if re.search(r"<sense_curr>\s*1\s*<", body):
            fields.append((cid, "I"))
    if not fields:
        for i in range(1, 5):
            fields.append((i, "V"))
            fields.append((i, "I"))

    active_channels = sorted({ch for ch, _ in fields})

    channel_models = []
    for cid in range(1, 5):
        m = re.search(r'<channel id="%d">[\s\S]*?<model>([^<]+)</model>' % cid, header_text)
        if m:
            channel_models.append(m.group(1))
        else:
            channel_models.append(("CH%d" % cid) if cid in active_channels else None)

    mm = re.search(r"<sense_minmax>([^<]+)</sense_minmax>", header_text)
    minmax = bool(mm and int(mm.group(1)) == 1)
    per_field = 3 if minmax else 1

    if header_end + 8 > len(buf):
        raise DlogFormatError("檔案長度不足，缺少記錄筆數欄位")
    n_records = struct.unpack_from(">Q", buf, header_end)[0]
    records_start = header_end + 8
    bytes_per_record = len(fields) * per_field * 4
    main_bytes = n_records * bytes_per_record
    if records_start + main_bytes > len(buf):
        raise DlogFormatError(
            "資料長度不符：期望 %d bytes，實際剩餘 %d bytes"
            % (main_bytes, len(buf) - records_start)
        )

    remain_after_main = len(buf) - (records_start + main_bytes)
    trailer_records = remain_after_main // bytes_per_record
    total_records = trailer_records + n_records

    if total_records == 0:
        raise DlogFormatError("No records")
    def allocate(kind, ch):
        if _destination is None:
            return np.zeros(total_records, dtype=np.float32)
        arr = np.lib.format.open_memmap(_destination / f"{kind}{ch}.npy",
                                       mode="w+", dtype="<f4", shape=(total_records,))
        _destination_maps.append(arr)
        # A missing field is represented by zeros, without materializing a heap array.
        if (ch, kind) not in fields:
            for start in range(0, total_records, CORE_CHUNK_RECORDS):
                _check_cancel(cancel)
                arr[start:start + CORE_CHUNK_RECORDS] = 0
        return arr
    _destination_maps = []
    V, I = {}, {}
    try:
        for ch in active_channels:
            V[ch] = allocate("V", ch)
            I[ch] = allocate("I", ch)
    except BaseException:
        _close_maps(_destination_maps)
        raise

    def read_block(start_off, count, dest_offset):
        for start in range(0, count, CORE_CHUNK_RECORDS):
            _check_cancel(cancel)
            size = min(CORE_CHUNK_RECORDS, count - start)
            raw = np.frombuffer(buf, dtype=">f4", count=size * len(fields) * per_field,
                                offset=start_off + start * bytes_per_record).reshape(size, len(fields), per_field)
            try:
                for k, (ch, kind) in enumerate(fields):
                    (V if kind == "V" else I)[ch][dest_offset + start:dest_offset + start + size] = raw[:, k, 0]
            finally:
                del raw  # release the exported source buffer even on write/progress failure
            if progress:
                progress("parse", 100 * (dest_offset + start + size) / total_records)

    try:
        if trailer_records > 0:
            read_block(records_start + main_bytes, trailer_records, 0)
        read_block(records_start, n_records, trailer_records)
    except BaseException:
        _close_maps(_destination_maps)
        raise

    return {
        "tint": tint,
        "declaredTime": declared_time,
        "nRecords": total_records,
        "channelModels": channel_models,
        "activeChannels": active_channels,
        "fields": fields,
        "minmax": minmax,
        "V": V,
        "I": I,
    }


def build_lod(V: dict, I: dict, n: int, active_channels: list, *, cancel=None, progress=None) -> dict:
    """Vectorized reductions over aligned blocks preserve the legacy float32 LOD."""
    lod = {}
    chunk = max(LOD_F0, (CORE_CHUNK_RECORDS // LOD_F0) * LOD_F0)
    for ci, ch in enumerate(active_channels):
        len0 = max(1, -(-n // LOD_F0))
        arrays = {k: (np.empty(len0, dtype="<f4"), np.empty(len0, dtype="<f4")) for k in "VIP"}
        for start in range(0, n, chunk):
            _check_cancel(cancel)
            end = min(n, start + chunk)
            v = V[ch][start:end]
            i = I[ch][start:end]
            power = (v.astype(np.float64) * i.astype(np.float64)).astype(np.float32)
            indices = np.arange(0, end - start, LOD_F0)
            dest = slice(start // LOD_F0, -(-end // LOD_F0))
            for kind, values in (("V", v), ("I", i), ("P", power)):
                arrays[kind][0][dest] = np.minimum.reduceat(values, indices)
                arrays[kind][1][dest] = np.maximum.reduceat(values, indices)
            if progress:
                progress("lod", 100 * (ci + end / n) / len(active_channels))
        for kind, (mn, mx) in arrays.items():
            indices = np.arange(0, len0, LOD_F1 // LOD_F0)
            lod[f"{kind}{ch}"] = {"level0": (mn, mx),
                                  "level1": (np.minimum.reduceat(mn, indices),
                                             np.maximum.reduceat(mx, indices))}
    return lod


def _close_maps(arrays):
    for arr in arrays:
        if isinstance(arr, np.memmap) and not arr._mmap.closed:
            arr._mmap.close()


def _close_entry(entry):
    _close_maps(list(entry["V"].values()) + list(entry["I"].values()) + list(entry["lod"].values()))


_open_cache = {}  # (resolved cache root, digest) -> Entry; guarded by storage.cache_lock


def _cache_key(digest, cache_root):
    return (str(Path(cache_root).resolve()), digest)


def _load_entry(digest, cache_root):
    directory = Path(cache_root) / digest
    maps = []
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        if meta["cacheVersion"] != CACHE_VERSION or meta["nRecords"] <= 0:
            raise ValueError("Invalid cache version or record count")
        if not meta["activeChannels"] or any(ch not in (1, 2, 3, 4) for ch in meta["activeChannels"]):
            raise ValueError("Invalid channels")
        def load(name, length):
            arr = np.load(directory / (name + ".npy"), mmap_mode="r", allow_pickle=False)
            maps.append(arr)
            if arr.shape != (length,) or arr.dtype != np.dtype("<f4"):
                raise ValueError("Invalid cache array: " + name)
            return arr
        n = meta["nRecords"]
        V = {ch: load(f"V{ch}", n) for ch in meta["activeChannels"]}
        I = {ch: load(f"I{ch}", n) for ch in meta["activeChannels"]}
        lod = {}
        for ch in meta["activeChannels"]:
            for kind in "VIP":
                for level, length in ((0, -(-n // LOD_F0)), (1, -(-n // LOD_F1))):
                    for which in ("min", "max"):
                        name = f"{kind}{ch}_l{level}{which}"
                        lod[name] = load(name, length)
        return {"meta": meta, "dir": directory, "V": V, "I": I, "lod": lod,
                "digest": digest, "_refs": 0, "_key": _cache_key(digest, cache_root)}
    except BaseException:
        _close_maps(maps)
        raise


def acquire_handle(digest: str, cache_root: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", digest):
        raise KeyError(digest)
    with storage.cache_lock:
        key = _cache_key(digest, cache_root)
        entry = _open_cache.get(key)
        if entry is None:
            try:
                entry = _load_entry(digest, cache_root)
            except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
                raise KeyError(digest) from exc
            _open_cache[key] = entry
        try:
            os.utime(entry["dir"] / "meta.json", None)
        except OSError:
            pass  # Another process may have evicted this mapped directory.
        entry["_refs"] += 1
        return entry


def release_handle(entry):
    # No I/O on the CONTROL /release route.
    with storage.cache_lock:
        if _open_cache.get(entry["_key"]) is entry and entry["_refs"] > 0:
            entry["_refs"] -= 1


get_handle = acquire_handle


def close_all():
    with storage.cache_lock:
        for key, entry in list(_open_cache.items()):
            if not entry["_refs"]:
                _close_entry(entry)
                del _open_cache[key]
        return not _open_cache


def _evict(cache_root, keep):
    with storage.cache_lock:
        root = str(Path(cache_root).resolve())
        pinned = {keep}
        for key, entry in list(_open_cache.items()):
            if key[0] != root:
                continue
            if entry["_refs"]:
                pinned.add(entry["digest"])
            else:
                _close_entry(entry)
                del _open_cache[key]
        storage.evict_lru(cache_root, CACHE_MAX_BYTES, pinned)


def _stamp(path):
    st = Path(path).stat()
    return st.st_size, st.st_mtime_ns


def _assert_source(path, stamp):
    try:
        same = _stamp(path) == stamp
    except OSError:
        same = False
    if not same:
        raise SourceChanged(str(path))


def _digest_of(path, cancel=None, progress=None):
    digest = hashlib.sha1()
    size = Path(path).stat().st_size
    done = 0
    with Path(path).open("rb") as f:
        while True:
            _check_cancel(cancel)
            chunk = f.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress("hash", min(100, 100 * done / max(1, size)))
    return digest.hexdigest()


def _build_cache(path, directory, stamp, cancel, progress):
    source = None
    parsed = None
    try:
        if not stamp[0]:
            raise DlogFormatError("Empty file")
        _assert_source(path, stamp)
        try:
            source = np.memmap(path, mode="r", dtype=np.uint8)
        except OSError:
            _assert_source(path, stamp)
            raise
        _assert_source(path, stamp)
        def parse_progress(stage, pct):
            _assert_source(path, stamp)
            if progress:
                progress(stage, pct)
            _check_cancel(cancel)
            _assert_source(path, stamp)
        try:
            parsed = parse_dlog(source._mmap, _destination=directory, cancel=cancel, progress=parse_progress)
        except OSError:
            _assert_source(path, stamp)
            raise
        except (ValueError, UnicodeError, struct.error) as exc:
            _assert_source(path, stamp)
            if isinstance(exc, DlogFormatError):
                raise
            raise DlogFormatError(str(exc)) from exc
        _assert_source(path, stamp)
        lod = build_lod(parsed["V"], parsed["I"], parsed["nRecords"], parsed["activeChannels"],
                        cancel=cancel, progress=progress)
        for key, levels in lod.items():
            for level in (0, 1):
                for which, arr in zip(("min", "max"), levels[f"level{level}"]):
                    _check_cancel(cancel)
                    np.save(directory / f"{key}_l{level}{which}.npy", arr)
        meta = {k: v for k, v in parsed.items() if k not in ("V", "I")}
        meta["cacheVersion"] = CACHE_VERSION
        (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        _assert_source(path, stamp)
    finally:
        try:
            if parsed is not None:
                arrays = list(parsed["V"].values()) + list(parsed["I"].values())
                try:
                    for arr in arrays:
                        arr.flush()
                finally:
                    _close_maps(arrays)
        finally:
            if source is not None:
                source._mmap.close()


def open_handle(path, cache_root: Path, *, cancel=None, progress=None) -> tuple:
    path, cache_root = Path(path), Path(cache_root).resolve()
    _check_cancel(cancel)
    try:
        stamp = _stamp(path)
    except OSError:
        # No baseline yet: a second failed stat means the granted source is gone.
        try:
            _stamp(path)
        except OSError as exc:
            raise SourceChanged(str(path)) from exc
        raise
    try:
        digest = _digest_of(path, cancel, progress)
    except OSError:
        _assert_source(path, stamp)
        raise
    _assert_source(path, stamp)
    storage.ensure_dir(cache_root)
    directory = cache_root / digest
    entry = None
    try:
        with storage.path_lock(directory, cancel):
            # Validation, mapping registration and LRU share one lock, so no eviction can
            # slip between cache validation and pinning.
            with storage.cache_lock:
                key = _cache_key(digest, cache_root)
                old = _open_cache.get(key)
                if old is not None and not old["_refs"]:
                    _close_entry(old)
                    del _open_cache[key]
                try:
                    entry = acquire_handle(digest, cache_root)
                except KeyError:
                    entry = None
                if entry is None and directory.exists():
                    try:
                        if directory.is_dir():
                            shutil.rmtree(directory)
                        else:
                            directory.unlink()
                    except OSError as exc:
                        raise CacheWriteError(f"{directory}: {exc}") from exc
            if entry is None:
                tmp = cache_root / f"{digest}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
                try:
                    with storage.cache_lock:
                        tmp.mkdir()
                        storage._active_tmp.add(tmp)
                    _build_cache(path, tmp, stamp, cancel, progress)
                    _check_cancel(cancel)
                    _assert_source(path, stamp)
                    with storage.cache_lock:
                        # Another process may have published while we were building.
                        if not directory.exists():
                            try:
                                os.replace(tmp, directory)
                            except OSError:
                                if not directory.exists():
                                    raise
                        entry = acquire_handle(digest, cache_root)
                except (AnalysisCancelled, SourceChanged, DlogFormatError):
                    raise
                except OSError as exc:
                    raise CacheWriteError(f"{directory}: {exc}") from exc
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                    with storage.cache_lock:
                        storage._active_tmp.discard(tmp)
            _assert_source(path, stamp)
            _check_cancel(cancel)
            meta = dict(entry["meta"], handle=digest, fileName=path.name,
                        lodFactors={"level0": LOD_F0, "level1": LOD_F1})
            chunks, layout, cursor = [], [], 0
            for ch in meta["activeChannels"]:
                for kind in "VIP":
                    for level in (0, 1):
                        for which in ("min", "max"):
                            _check_cancel(cancel)
                            arr = entry["lod"][f"{kind}{ch}_l{level}{which}"]
                            layout.append(dict(key=f"{kind}{ch}", level=level, which=which,
                                               offset=cursor, length=int(arr.size)))
                            chunks.append(arr.tobytes())
                            cursor += arr.nbytes
            meta["lodLayout"] = layout
            _evict(cache_root, digest)
            return meta, b"".join(chunks), entry
    except BaseException:
        if entry is not None:
            release_handle(entry)
        raise


def open_dlog(path, cache_root: Path, *, cancel=None, progress=None) -> tuple:
    """Compatibility API: return metadata and LOD bytes without owning a pin."""
    meta, blob, entry = open_handle(path, cache_root, cancel=cancel, progress=progress)
    release_handle(entry)
    return meta, blob


# splitmix64：由索引推導 (0,1] 均勻亂數。同一索引不論落在哪個請求切片、哪個端點，
# 都得到完全相同的值，因此不需要任何快取狀態——取代前端舊版「Math.random() + 逐索引快取」。
_SM_PHI = np.uint64(0x9E3779B97F4A7C15)
_SM_M1 = np.uint64(0xBF58476D1CE4E5B9)
_SM_M2 = np.uint64(0x94D049BB133111EB)
_TWO53 = 9007199254740992.0


def offset_uniform(idx: np.ndarray) -> np.ndarray:
    z = idx.astype(np.uint64) + _SM_PHI
    z = (z ^ (z >> np.uint64(30))) * _SM_M1
    z = (z ^ (z >> np.uint64(27))) * _SM_M2
    z = z ^ (z >> np.uint64(31))
    return ((z >> np.uint64(11)).astype(np.float64) + 1.0) / _TWO53


def apply_current_offset(seg: np.ndarray, idx_start: int, max_limit: float) -> np.ndarray:
    """
    電流負值校正：把負值換成 (0, max_limit] 區間內、由索引決定的值。
    max_limit<=0 時退回 0（與前端舊版 maxLimit*(1-Math.random()) 在 maxLimit=0 的結果一致）。
    只動負值，正值原樣保留；電壓不套用此校正（真實物理現象）。
    """
    neg = np.isfinite(seg) & (seg < 0)  # -Inf 是無效樣本，不可換成正常正值
    if not neg.any():
        return seg
    out = np.array(seg, dtype=np.float32)
    if max_limit <= 0:
        out[neg] = 0.0
        return out
    idx = np.nonzero(neg)[0].astype(np.uint64) + np.uint64(idx_start)
    out[neg] = (max_limit * offset_uniform(idx)).astype(np.float32)
    return out


def _values_at(entry, kind, ch, indices, offset, max_limit):
    if kind == "V":
        return np.array(entry["V"][ch][indices], dtype=np.float32)
    cur = np.array(entry["I"][ch][indices], dtype=np.float32)
    if offset:
        neg = np.isfinite(cur) & (cur < 0)
        if max_limit <= 0:
            cur[neg] = 0
        elif neg.any():
            if isinstance(indices, slice):
                idx = np.nonzero(neg)[0].astype(np.uint64) + np.uint64(indices.start)
            else:
                idx = indices[neg].astype(np.uint64)
            cur[neg] = (max_limit * offset_uniform(idx)).astype(np.float32)
    if kind == "I":
        return cur
    return entry["V"][ch][indices].astype(np.float64) * cur.astype(np.float64)


def get_values(entry: dict, kind: str, ch: int, lo: int, hi: int,
               offset: bool, max_limit: float, *, cancel=None) -> np.ndarray:
    _check_cancel(cancel)
    if hi - lo + 1 > CORE_MAX_VALUES_SPAN:
        raise LimitExceeded("get_values span")
    n = entry["meta"]["nRecords"]
    lo, hi = max(0, lo), min(n - 1, hi)
    out = np.empty(max(0, hi - lo + 1), dtype=np.float64 if kind == "P" else np.float32)
    for start in range(lo, hi + 1, CORE_CHUNK_RECORDS):
        _check_cancel(cancel)
        end = min(hi + 1, start + CORE_CHUNK_RECORDS)
        out[start - lo:end - lo] = _values_at(entry, kind, ch, slice(start, end), offset, max_limit)
    _check_cancel(cancel)
    return out


def _lod_level(entry: dict, key: str, level: int):
    suffix = "l0" if level == 0 else "l1"
    factor = LOD_F0 if level == 0 else LOD_F1
    mn = entry["lod"][key + "_" + suffix + "min"]
    mx = entry["lod"][key + "_" + suffix + "max"]
    return {"min": mn, "max": mx, "factor": factor, "n": int(mn.size)}


def _buckets_from_lod_level(level, idx_lo, idx_hi, tint, n_buckets):
    """與前端 bucketsFromLodLevel()（HTML L1295）逐行對應。"""
    factor = level["factor"]
    e_lo = max(0, idx_lo // factor)
    e_hi = min(level["n"] - 1, idx_hi // factor)
    count = e_hi - e_lo + 1
    if count <= 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    mn_arr, mx_arr = level["min"], level["max"]
    if count <= n_buckets:
        e = np.arange(e_lo, e_hi + 1)
        raw_start = e * factor
        raw_end = np.minimum(idx_hi, raw_start + factor - 1)
        t = ((np.maximum(idx_lo, raw_start) + raw_end) / 2.0) * tint
        return t, mn_arr[e_lo:e_hi + 1].astype(np.float64), mx_arr[e_lo:e_hi + 1].astype(np.float64)

    bucket_size = -(-count // n_buckets)
    starts = np.arange(e_lo, e_hi + 1, bucket_size)
    ends = np.minimum(e_hi, starts + bucket_size - 1)
    seg = (starts - e_lo).astype(np.intp)
    mn = np.minimum.reduceat(mn_arr[e_lo:e_hi + 1], seg).astype(np.float64)
    mx = np.maximum.reduceat(mx_arr[e_lo:e_hi + 1], seg).astype(np.float64)
    raw_mid = (starts * factor + np.minimum(idx_hi, (ends + 1) * factor - 1)) / 2.0
    return raw_mid * tint, mn, mx


def compute_buckets(entry: dict, kind: str, ch: int, idx_lo: int, idx_hi: int,
                    n_buckets: int, offset: bool, max_limit: float, *, cancel=None):
    """
    與前端 computeViewBuckets()（HTML L1242）的三條路徑逐行對應，
    差別只在資料來源是 memmap 而非 Float32Array，且不含 posOffset（純視覺位移，留在前端）。
    回傳 (t[], min[], max[])。
    """
    _check_cancel(cancel)
    if n_buckets > MAX_BUCKETS:
        raise LimitExceeded("bucket count")
    if n_buckets < 1:
        raise ValueError("bucket count must be positive")
    tint = entry["meta"]["tint"]
    _check_cancel(cancel)
    n_rec = entry["meta"]["nRecords"]
    idx_lo = max(0, min(n_rec - 1, idx_lo))
    idx_hi = max(0, min(n_rec - 1, idx_hi))
    count = idx_hi - idx_lo + 1
    if count <= 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)

    if count <= n_buckets:
        vals = get_values(entry, kind, ch, idx_lo, idx_hi, offset, max_limit, cancel=cancel).astype(np.float64)
        t = np.arange(idx_lo, idx_hi + 1, dtype=np.float64) * tint
        return t, vals, vals

    # 校正開啟時，預建的 LOD 摘要未套用校正，與即時取值不一致 → 退回逐筆掃描（同前端 L1263）
    lod_usable = not (offset and kind != "V")
    if lod_usable:
        key = "%s%d" % (kind, ch)
        samples_per_bucket = count / n_buckets
        level = None
        if LOD_F1 <= samples_per_bucket:
            level = _lod_level(entry, key, 1)
        elif LOD_F0 <= samples_per_bucket:
            level = _lod_level(entry, key, 0)
        if level is not None:
            return _buckets_from_lod_level(level, idx_lo, idx_hi, tint, n_buckets)

    bucket_size = -(-count // n_buckets)
    starts = np.arange(idx_lo, idx_hi + 1, bucket_size)
    ends = np.minimum(idx_hi, starts + bucket_size - 1)
    mn = np.full(starts.size, np.inf)
    mx = np.full(starts.size, -np.inf)
    # Preserve global bucket boundaries even when a chunk cuts through a bucket.
    for start in range(idx_lo, idx_hi + 1, CORE_CHUNK_RECORDS):
        _check_cancel(cancel)
        end = min(idx_hi + 1, start + CORE_CHUNK_RECORDS)
        vals = get_values(entry, kind, ch, start, end - 1, offset, max_limit, cancel=cancel)
        first = (start - idx_lo) // bucket_size
        last = (end - 1 - idx_lo) // bucket_size
        cuts = np.maximum(start, starts[first:last + 1]) - start
        mn[first:last + 1] = np.minimum(mn[first:last + 1], np.minimum.reduceat(vals, cuts))
        mx[first:last + 1] = np.maximum(mx[first:last + 1], np.maximum.reduceat(vals, cuts))

    t = ((starts + ends) / 2.0) * tint
    return t, mn, mx


_STATS_CHUNK = CORE_CHUNK_RECORDS  # 分塊掃描，避免超大區間一次materialize 出巨大陣列


def compute_stats(entry: dict, kind: str, ch: int, idx_lo: int, idx_hi: int,
                  offset: bool, max_limit: float, *, cancel=None) -> dict:
    """與前端 computeRangeStats()（HTML L1343）對應：區間內的 min/max/avg。"""
    _check_cancel(cancel)
    n_rec = entry["meta"]["nRecords"]
    idx_lo = max(0, min(n_rec - 1, idx_lo))
    idx_hi = max(0, min(n_rec - 1, idx_hi))
    if idx_hi < idx_lo:
        return {"min": None, "max": None, "avg": None}
    mn, mx, total, cnt = np.inf, -np.inf, 0.0, 0
    for start in range(idx_lo, idx_hi + 1, _STATS_CHUNK):
        end = min(idx_hi, start + _STATS_CHUNK - 1)
        vals = get_values(entry, kind, ch, start, end, offset, max_limit, cancel=cancel).astype(np.float64)
        # 排除 NaN/Inf：否則會污染整段統計，且 json.dumps 產生的 NaN 前端 JSON.parse 解不開
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            continue
        mn = min(mn, float(vals.min()))
        mx = max(mx, float(vals.max()))
        total += float(vals.sum())
        cnt += int(vals.size)
    return {
        "min": mn if cnt else None,
        "max": mx if cnt else None,
        "avg": (total / cnt) if cnt else None,
    }


# ---------------------------------------------------------------------------
# CSV 匯出：串流產生、逐塊 yield，永遠不在記憶體裡組出完整字串
# （前端舊版把整份 CSV 組成一個 JS 字串再包 Blob，千萬筆會直接撐爆分頁）。
# 格式與前端 L2776-2803 完全一致：三行前導 + 標題列，CRLF 行尾，UTF-8 無 BOM。
# ---------------------------------------------------------------------------

_CSV_ROWS_PER_CHUNK = CSV_ROWS_PER_CHUNK


def ctime_like(d) -> str:
    """對應前端 fmtCtimeDate()：月份與日期間兩個空白，日期不足兩位補空白而非補零。"""
    days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return "%s %s %2d %02d:%02d:%02d %d" % (
        days[(d.weekday() + 1) % 7], months[d.month - 1], d.day,
        d.hour, d.minute, d.second, d.year,
    )


def _js_number(x: float) -> str:
    """JS 的 String(number)：repr() 同樣是最短往返表示，兩者對這類數值一致。"""
    s = repr(float(x))
    return s[:-2] if s.endswith(".0") else s


def iter_csv(primary: dict, columns: list, idx_lo: int, idx_hi: int,
             offset: bool, max_limit: float, now, *, cancel=None):
    """
    columns：[{entry, kind, ch, label}, ...]，順序即輸出欄位順序。
    時間軸一律以 primary 的取樣格點為準；其他來源（比對檔）依同一時間點就近取樣，
    超出其記錄範圍的儲存格留空——與前端 L2782-2797 的行為一致。
    """
    _check_cancel(cancel)
    if len(columns) > MAX_CSV_KEYS:
        raise LimitExceeded("CSV columns")
    idx_lo = max(0, min(primary["meta"]["nRecords"] - 1, idx_lo))
    idx_hi = max(0, min(primary["meta"]["nRecords"] - 1, idx_hi))
    tint = primary["meta"]["tint"]
    header = "\r\n".join([
        "14585A Exported Waveform",
        "Sampling Period:" + _js_number(tint),
        "Date:" + ctime_like(now),
        ",".join(["Time"] + [c["label"] for c in columns]),
    ])
    yield (header + "\r\n").encode("utf-8")

    for start in range(idx_lo, idx_hi + 1, _CSV_ROWS_PER_CHUNK):
        _check_cancel(cancel)
        end = min(idx_hi, start + _CSV_ROWS_PER_CHUNK - 1)
        count = end - start + 1
        times = np.arange(start, end + 1, dtype=np.float64) * tint

        col_values = []
        for c in columns:
            entry = c["entry"]
            if c.get("missing"):
                col_values.append(np.full(count, np.nan))  # 未記錄的欄位：整欄留空
                continue
            if entry is primary:
                col_values.append(get_values(entry, c["kind"], c["ch"], start, end,
                                             offset, max_limit, cancel=cancel).astype(np.float64))
                continue
            # 其他來源：以主檔案的時間點就近取樣（round 到最近索引）
            other_tint = entry["meta"]["tint"]
            other_n = entry["meta"]["nRecords"]
            kk = np.floor(times / other_tint + 0.5).astype(np.int64)  # Math.round, as the viewer
            valid = (kk >= 0) & (kk < other_n)
            vals = np.full(count, np.nan, dtype=np.float64)
            if valid.any():
                _check_cancel(cancel)
                vals[valid] = _values_at(entry, c["kind"], c["ch"], kk[valid], offset, max_limit)
            col_values.append(vals)

        fmt = "%.8f" + ",%.9f" * len(columns)
        lines = [(fmt % row).replace(",nan", ",") for row in zip(times, *col_values)]
        _check_cancel(cancel)
        yield ("\r\n".join(lines) + "\r\n").encode("utf-8")
