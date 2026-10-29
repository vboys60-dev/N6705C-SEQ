"""Process-local storage coordination and atomic file publication."""
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
from contextlib import contextmanager

from .contracts import (APP_DIR_NAME, CACHE_SUBDIR, PREFS_FILENAME, PART_SUFFIX,
                        AnalysisCancelled, CacheWriteError)

# Core holds this lock while changing mappings/pins or evicting cache directories.
cache_lock = threading.RLock()
_registry_lock = threading.Lock()
_locks = {}
_active_tmp = set()


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise AnalysisCancelled()


@contextmanager
def path_lock(path, cancel=None):
    key = str(Path(path).resolve())
    with _registry_lock:
        lock, users = _locks.get(key, (threading.Lock(), 0))
        _locks[key] = (lock, users + 1)
    acquired = False
    try:
        while not acquired:
            check_cancel(cancel)
            acquired = lock.acquire(timeout=0.05)
        check_cancel(cancel)
        yield
    finally:
        if acquired:
            lock.release()
        with _registry_lock:
            _, users = _locks[key]
            if users == 1:
                del _locks[key]
            else:
                _locks[key] = (lock, users - 1)


def default_cache_root():
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / APP_DIR_NAME / CACHE_SUBDIR


def default_prefs_path():
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
    else:
        base = Path.home() / '.config'
    return base / APP_DIR_NAME / PREFS_FILENAME


def ensure_dir(path):
    path = Path(path)
    probe = path / ('.probe-' + secrets.token_hex(8))
    try:
        path.mkdir(parents=True, exist_ok=True)
        with probe.open('xb'):
            pass
        probe.unlink()
    except OSError as exc:
        raise CacheWriteError(f'{path}: {exc}') from exc
    return path


def sweep_tmp(cache_root):
    root = Path(cache_root)
    if not root.exists():
        return
    with cache_lock:
        for p in root.rglob('*'):
            if p.resolve() in _active_tmp or any(a in _active_tmp for a in p.resolve().parents):
                continue
            if p.is_dir() and '.tmp-' in p.name:
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file() and p.name.endswith(PART_SUFFIX):
                p.unlink(missing_ok=True)


def evict_lru(cache_root, max_bytes, pinned):
    """Caller must include every live mapping/build in pinned (core does so)."""
    with cache_lock:
        entries = []
        for p in Path(cache_root).iterdir():
            if p.is_dir() and re.fullmatch(r'[0-9a-f]{40}', p.name):
                size = sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
                meta = p / 'meta.json'
                entries.append((meta.stat().st_mtime_ns if meta.exists() else 0, p, size))
        total = sum(e[2] for e in entries)
        for _, p, size in sorted(entries):
            if total <= max_bytes:
                break
            if p.name not in pinned:
                shutil.rmtree(p)
                total -= size


def atomic_write_iter(path, chunks, cancel=None):
    path = Path(path).resolve()
    with path_lock(path, cancel):
        tmp = path.with_name(path.name + '.' + secrets.token_hex(4) + PART_SUFFIX)
        with cache_lock:
            _active_tmp.add(tmp)
        try:
            check_cancel(cancel)
            count = 0
            with tmp.open('xb') as f:
                for chunk in chunks:
                    check_cancel(cancel)
                    count += f.write(chunk)
                check_cancel(cancel)
                f.flush()
                os.fsync(f.fileno())
            check_cancel(cancel)
            os.replace(tmp, path)
            return count
        except OSError as exc:
            raise CacheWriteError(f'{path}: {exc}') from exc
        finally:
            tmp.unlink(missing_ok=True)
            with cache_lock:
                _active_tmp.discard(tmp)


def atomic_write_bytes(path, data):
    return atomic_write_iter(path, (data,))
