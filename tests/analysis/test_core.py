"""Large-file core lifecycle, bounds, cancellation and chunk boundary regressions."""
import datetime
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from fixtures.make_big import make_big
from n6705c_seq.analysis import core
from n6705c_seq.analysis.contracts import AnalysisCancelled, SourceChanged, LimitExceeded, CacheWriteError


def opened(tmp_path, cache_root, n=3001):
    path = make_big(tmp_path / 'source.dlog', n, channels=2, trailer=True)
    meta, blob = core.open_dlog(path, cache_root)
    return path, meta, core.acquire_handle(meta['handle'], cache_root), blob


def test_paths_maps_refcounts_and_roots(tmp_path, cache_root):
    path, meta, entry, _ = opened(tmp_path, cache_root)
    assert meta['handle'] == hashlib.sha1(path.read_bytes()).hexdigest()
    assert meta['fileName'] == path.name
    assert meta['cacheVersion'] == core.CACHE_VERSION
    assert all(isinstance(a, np.memmap) and not a.flags.writeable for a in entry['V'].values())
    second = core.acquire_handle(meta['handle'], cache_root)
    assert second is entry
    assert core.close_all() is False
    core.release_handle(entry)
    assert core.close_all() is False
    other_root = tmp_path / 'other'
    core.open_dlog(path, other_root)
    other = core.acquire_handle(meta['handle'], other_root)
    assert other is not entry
    core.release_handle(second)
    assert not core.close_all()
    assert entry['V'][1]._mmap.closed
    core.release_handle(other)
    assert core.close_all()


@pytest.mark.parametrize('damage', ['version', 'missing', 'truncated', 'metadata'])
def test_cache_damage_rebuilt(tmp_path, cache_root, damage):
    path, meta, entry, blob = opened(tmp_path, cache_root)
    core.release_handle(entry)
    directory = cache_root / meta['handle']
    if damage == 'version':
        m = json.loads((directory / 'meta.json').read_text())
        m['cacheVersion'] = -1
        (directory / 'meta.json').write_text(json.dumps(m))
    elif damage == 'missing':
        (directory / 'P1_l0min.npy').unlink()
    elif damage == 'truncated':
        (directory / 'V1.npy').write_bytes(b'broken')
    else:
        (directory / 'meta.json').write_text('{')
    rebuilt, new_blob = core.open_dlog(path, cache_root)
    assert rebuilt['cacheVersion'] == core.CACHE_VERSION and new_blob == blob
    assert not list(cache_root.glob('*.tmp-*'))


@pytest.mark.parametrize('stage', ['hash', 'parse', 'lod'])
def test_cancel_cleans_build(tmp_path, cache_root, stage):
    path = make_big(tmp_path / 'cancel.dlog', 10003, channels=2)
    flag = threading.Event()
    cancelled_at = []
    def progress(current, pct):
        if current == stage and not flag.is_set():
            cancelled_at.append(time.monotonic())
            flag.set()
    with pytest.raises(AnalysisCancelled):
        core.open_dlog(path, cache_root, cancel=flag, progress=progress)
    assert time.monotonic() - cancelled_at[0] < 2
    assert list(cache_root.iterdir()) == []
    assert core.close_all()


@pytest.mark.parametrize('stage', ['hash', 'parse', 'lod'])
def test_source_change_not_published(tmp_path, cache_root, stage):
    path = make_big(tmp_path / 'changing.dlog', 10003, channels=2)
    changed = False
    def progress(current, pct):
        nonlocal changed
        if current == stage and not changed:
            changed = True
            with path.open('ab') as f:
                f.write(b'change')
    with pytest.raises(SourceChanged):
        core.open_dlog(path, cache_root, progress=progress)
    assert list(cache_root.iterdir()) == []


def test_same_digest_only_one_build(tmp_path, cache_root, monkeypatch):
    path = make_big(tmp_path / 'parallel.dlog', 10003)
    original = core._build_cache
    builds = []
    def build(*args):
        builds.append(1)
        time.sleep(.05)
        return original(*args)
    monkeypatch.setattr(core, '_build_cache', build)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: core.open_dlog(path, cache_root), range(4)))
    assert len(builds) == 1
    assert all(m == results[0][0] and b == results[0][1] for m, b in results)
    assert len(list(cache_root.iterdir())) == 1


def test_waiting_builder_cancellable(tmp_path, cache_root):
    path = make_big(tmp_path / 'waiting.dlog', 100)
    digest = hashlib.sha1(path.read_bytes()).hexdigest()
    flag = threading.Event()
    with core.storage.path_lock(cache_root / digest):
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(core.open_dlog, path, cache_root, cancel=flag)
            flag.set()
            with pytest.raises(AnalysisCancelled):
                future.result(timeout=2)


def test_chunked_values_stats_and_corrected_buckets(tmp_path, cache_root, monkeypatch):
    _, _, entry, _ = opened(tmp_path, cache_root)
    ref = core.get_values(entry, 'P', 1, 0, 3005, True, .1)
    monkeypatch.setattr(core, 'CORE_CHUNK_RECORDS', 127)
    monkeypatch.setattr(core, '_STATS_CHUNK', 127)
    got = core.get_values(entry, 'P', 1, 0, 3005, True, .1)
    assert np.array_equal(ref, got)
    stats = core.compute_stats(entry, 'P', 1, 0, 3005, True, .1)
    assert stats['min'] == ref.min() and stats['max'] == ref.max()
    assert np.isclose(stats['avg'], ref.mean(), rtol=1e-9)
    t, mn, mx = core.compute_buckets(entry, 'P', 1, 0, 3005, 19, True, .1)
    size = -(-len(ref) // 19)
    starts = np.arange(0, len(ref), size)
    assert np.array_equal(mn, np.minimum.reduceat(ref, starts))
    assert np.array_equal(mx, np.maximum.reduceat(ref, starts))
    assert t.size == mn.size
    with pytest.raises(LimitExceeded):
        core.get_values(entry, 'V', 1, 0, core.CORE_MAX_VALUES_SPAN, False, 0)
    with pytest.raises(LimitExceeded):
        core.compute_buckets(entry, 'V', 1, 0, 10, core.MAX_BUCKETS + 1, False, 0)


def test_lru_closes_unpinned_maps(tmp_path, cache_root, monkeypatch):
    path, meta, entry, _ = opened(tmp_path, cache_root)
    monkeypatch.setattr(core, 'CACHE_MAX_BYTES', 1)
    path2 = make_big(tmp_path / 'two.dlog', 100)
    meta2, _ = core.open_dlog(path2, cache_root)
    assert entry['dir'].exists() and not entry['V'][1]._mmap.closed
    core.release_handle(entry)
    core.open_dlog(path2, cache_root)
    assert not entry['dir'].exists() and entry['V'][1]._mmap.closed
    assert (cache_root / meta2['handle']).exists()


def test_csv_sparse_compare_gathers_and_cancels(tmp_path, cache_root, monkeypatch):
    _, _, primary, _ = opened(tmp_path, cache_root)
    # A virtual huge source detects accidental contiguous reads without allocating it.
    class Sparse:
        def __getitem__(self, index):
            assert isinstance(index, np.ndarray), 'compare must gather indices'
            assert index.size <= core.CSV_ROWS_PER_CHUNK
            return index.astype(np.float32)
    other = {'meta': {'tint': 1e-9, 'nRecords': 10**12}, 'V': {1: Sparse()}, 'I': {1: Sparse()}}
    cols = [{'entry': other, 'kind': 'P', 'ch': 1, 'label': 'compare'}]
    flag = threading.Event()
    stream = core.iter_csv(primary, cols, 0, 100, True, .1, datetime.datetime(2026, 1, 1), cancel=flag)
    assert next(stream).startswith(b'14585A')
    body = next(stream)
    assert body.count(b'\r\n') == 101
    stream = core.iter_csv(primary, cols, 0, 100, False, 0, datetime.datetime.now(), cancel=flag)
    next(stream)
    flag.set()
    with pytest.raises(AnalysisCancelled):
        next(stream)


def test_failed_publish_closes_maps_and_removes_tmp(tmp_path, cache_root, monkeypatch):
    path = make_big(tmp_path / 'write.dlog', 100)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(core.os, 'replace', fail)
    with pytest.raises(CacheWriteError):
        core.open_dlog(path, cache_root)
    assert list(cache_root.iterdir()) == []
