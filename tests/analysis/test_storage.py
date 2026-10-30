"""Atomic publication and cache storage regressions."""
import os
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from n6705c_seq.analysis import storage
from n6705c_seq.analysis.contracts import AnalysisCancelled, CacheWriteError


def test_atomic_same_target_serialized(tmp_path):
    target = tmp_path / 'out.csv'
    barrier = threading.Barrier(4)
    def write(i):
        barrier.wait()
        return storage.atomic_write_iter(target, (bytes([65 + i]) * 8192 for _ in range(10)))
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(write, range(4))) == [81920] * 4
    data = target.read_bytes()
    assert data in [bytes([65 + i]) * 81920 for i in range(4)]
    assert not list(tmp_path.glob('*.part'))
    assert not storage._locks


def test_atomic_cancel_keeps_original(tmp_path):
    target = tmp_path / 'out.csv'
    target.write_bytes(b'original')
    flag = threading.Event()
    def chunks():
        yield b'first'
        flag.set()
        yield b'second'
    with pytest.raises(AnalysisCancelled):
        storage.atomic_write_iter(target, chunks(), cancel=flag)
    assert target.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [target]


def test_generator_failure_keeps_original(tmp_path):
    target = tmp_path / 'out.csv'
    target.write_bytes(b'original')
    def chunks():
        yield b'first'
        raise RuntimeError('failed')
    with pytest.raises(RuntimeError):
        storage.atomic_write_iter(target, chunks())
    assert target.read_bytes() == b'original'
    assert not list(tmp_path.glob('*.part'))


def test_atomic_bytes_fsync_before_replace(tmp_path, monkeypatch):
    calls = []
    fsync, replace = storage.os.fsync, storage.os.replace
    def sync(fd):
        calls.append('sync')
        fsync(fd)
    def publish(a, b):
        calls.append('replace')
        replace(a, b)
    monkeypatch.setattr(storage.os, 'fsync', sync)
    monkeypatch.setattr(storage.os, 'replace', publish)
    target = tmp_path / 'data'
    assert storage.atomic_write_bytes(target, b'hello') == 5
    assert target.read_bytes() == b'hello'
    assert calls == ['sync', 'replace']


def test_sweep_only_cache_residue_and_live_tmp(tmp_path, cache_root, monkeypatch):
    monkeypatch.setattr(storage, '_pid_is_alive', lambda pid: False)
    outside = tmp_path / 'user.part'
    outside.write_bytes(b'keep')
    temp = cache_root / 'a.tmp-123-random'
    temp.mkdir()
    (temp / 'partial.npy').write_bytes(b'bad')
    part = cache_root / 'output.123.part'
    part.write_bytes(b'bad')
    good = cache_root / 'meta.json'
    good.write_bytes(b'keep')
    live = cache_root / 'live.tmp-123-live'
    live.mkdir()
    storage._active_tmp.add(live.resolve())
    try:
        storage.sweep_tmp(cache_root)
        assert live.exists()
    finally:
        storage._active_tmp.discard(live.resolve())
    assert not temp.exists() and not part.exists()
    assert good.exists() and outside.exists()


def test_lru_mtime_and_pins(cache_root):
    paths = [cache_root / (str(i) * 40) for i in range(1, 4)]
    for i, p in enumerate(paths):
        p.mkdir()
        (p / 'meta.json').write_bytes(b'x' * 10)
        os.utime(p / 'meta.json', (i + 1, i + 1))
    storage.evict_lru(cache_root, 20, {paths[0].name})
    assert paths[0].exists() and not paths[1].exists() and paths[2].exists()


def test_ensure_dir_reports_path(tmp_path):
    target = tmp_path / 'file'
    target.write_text('occupied')
    with pytest.raises(CacheWriteError, match='file'):
        storage.ensure_dir(target)


def test_platform_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(storage.sys, 'platform', 'win32')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    assert storage.default_cache_root() == tmp_path / 'local/N6705C-SEQ/analysis-cache'
    assert storage.default_prefs_path() == tmp_path / 'roaming/N6705C-SEQ/analysis_prefs.json'
    monkeypatch.setattr(storage.sys, 'platform', 'linux')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'xdg'))
    assert storage.default_cache_root() == tmp_path / 'xdg/N6705C-SEQ/analysis-cache'


def test_fsync_failure_preserves_target(tmp_path, monkeypatch):
    target = tmp_path / 'data'
    target.write_bytes(b'original')
    def fail(fd):
        raise OSError('disk full')
    monkeypatch.setattr(storage.os, 'fsync', fail)
    with pytest.raises(CacheWriteError, match='data'):
        storage.atomic_write_bytes(target, b'new')
    assert target.read_bytes() == b'original'
    assert not list(tmp_path.glob('*.part'))


def test_lru_all_pinned_can_exceed_limit(cache_root, monkeypatch):
    pinned = {str(i) * 40 for i in (1, 2)}
    for name in pinned:
        directory = cache_root / name
        directory.mkdir()
        (directory / 'meta.json').write_bytes(b'x' * 10)
    def unexpected(*args, **kwargs):
        pytest.fail('pinned directory must never be removed')
    monkeypatch.setattr(storage.shutil, 'rmtree', unexpected)
    storage.evict_lru(cache_root, 1, pinned)
    assert {p.name for p in cache_root.iterdir()} == pinned


def test_lru_busy_directory_keeps_bytes_counted(cache_root, monkeypatch):
    paths = [cache_root / (str(i) * 40) for i in range(1, 4)]
    for i, path in enumerate(paths):
        path.mkdir()
        (path / 'meta.json').write_bytes(b'x' * 10)
        os.utime(path / 'meta.json', (i + 1, i + 1))
    rename = type(cache_root).rename
    def busy(path, target):
        if path == paths[0]:
            raise PermissionError('busy')
        return rename(path, target)
    monkeypatch.setattr(type(cache_root), 'rename', busy)
    storage.evict_lru(cache_root, 10, set())
    assert paths[0].exists()
    assert not paths[1].exists() and not paths[2].exists()


@pytest.mark.parametrize('alive', [False, True])
def test_sweep_respects_other_process_and_its_children(cache_root, monkeypatch, alive):
    directory = cache_root / 'a.tmp-4567-random'
    nested = directory / 'nested.tmp-7654-random'
    nested.mkdir(parents=True)
    part = nested / 'data.part'
    part.write_bytes(b'live data')
    checked = []
    def probe(pid):
        checked.append(pid)
        return alive
    monkeypatch.setattr(storage, '_pid_is_alive', probe)
    storage.sweep_tmp(cache_root)
    assert directory.exists() is alive
    assert part.exists() is alive
    assert checked == [4567]


def test_pid_probe_current_process():
    assert storage._pid_is_alive(os.getpid())


@pytest.mark.parametrize('error, alive', [(ProcessLookupError(), False),
                                          (PermissionError(), True), (OSError(), True)])
def test_pid_probe_posix_errors(monkeypatch, error, alive):
    monkeypatch.setattr(storage.sys, 'platform', 'linux')
    def probe(pid, signal):
        assert (pid, signal) == (4567, 0)
        raise error
    monkeypatch.setattr(storage.os, 'kill', probe)
    assert storage._pid_is_alive(4567) is alive


def test_pid_probe_windows_conservatively_retains(monkeypatch):
    monkeypatch.setattr(storage.sys, 'platform', 'win32')
    def unexpected(*args):
        pytest.fail('Windows must not use os.kill as an existence probe')
    monkeypatch.setattr(storage.os, 'kill', unexpected)
    assert storage._pid_is_alive(4567)


@pytest.mark.parametrize('retry', ['sweep', 'evict'])
def test_lru_retires_before_delete_and_retries_residue(cache_root, monkeypatch, retry):
    directory = cache_root / ('a' * 40)
    directory.mkdir()
    (directory / 'meta.json').write_bytes(b'original metadata')
    remove = storage.shutil.rmtree
    retired = []
    def fail(path):
        assert not directory.exists()
        assert path.name.startswith('.evicted-')
        assert (path / 'meta.json').read_bytes() == b'original metadata'
        retired.append(path)
        # A new publisher may reuse the digest while the old tree is deleted.
        directory.mkdir()
        (directory / 'meta.json').write_bytes(b'new cache')
        raise PermissionError('still mapped by another process')
    monkeypatch.setattr(storage.shutil, 'rmtree', fail)
    storage.evict_lru(cache_root, 0, set())
    assert len(retired) == 1 and retired[0].exists()
    monkeypatch.setattr(storage.shutil, 'rmtree', remove)
    if retry == 'sweep':
        storage.sweep_tmp(cache_root)
    else:
        storage.evict_lru(cache_root, 0, {directory.name})
    assert not retired[0].exists()
    assert (directory / 'meta.json').read_bytes() == b'new cache'
