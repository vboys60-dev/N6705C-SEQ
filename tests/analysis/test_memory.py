"""Opt-in contract budgets. Each measured step runs in a fresh isolated process.

N6705C_ANALYSIS_BIG=1: 10M cold/warm open and full-range 12-column CSV.
N6705C_ANALYSIS_BIG=30: additionally 30M cold open/full stats/full buckets.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fixtures.make_big import make_big
from n6705c_seq.analysis import contracts as C

SRC = Path(__file__).resolve().parents[2] / 'src'
BIG = os.environ.get('N6705C_ANALYSIS_BIG', '')

# Executed after an explicit sys.path insertion by python -I (no PYTHONPATH).
WORKER = r'''
import datetime
import json
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from n6705c_seq.analysis import core
from n6705c_seq.analysis import contracts as C

path, root, step = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if step != 'cold':
    meta, _ = core.open_dlog(path, root)
    entry = core.acquire_handle(meta['handle'], root)
    del _
gaps = []
stop = threading.Event()
def probe():
    previous = time.perf_counter()
    while not stop.wait(.02):
        current = time.perf_counter()
        gaps.append(current - previous)
        previous = current
thread = threading.Thread(target=probe, daemon=True)
thread.start()
tracemalloc.start()
start = time.perf_counter()
rows = 0
try:
    if step in ('cold', 'warm'):
        meta, blob = core.open_dlog(path, root)
        assert len(blob) > 0
    elif step == 'csv':
        columns = [dict(entry=entry, kind=k, ch=ch, label=f'{k}{ch}')
                   for ch in entry['meta']['activeChannels'] for k in 'VIP']
        for chunk in core.iter_csv(entry, columns, 0, entry['meta']['nRecords'] - 1,
                                   False, 0, datetime.datetime(2026, 1, 1)):
            rows += chunk.count(b'\r\n')
        assert rows == entry['meta']['nRecords'] + 4
    elif step in ('stats', 'buckets'):
        for ch in entry['meta']['activeChannels']:
            for kind in 'VIP':
                for offset in (False, True):
                    if step == 'stats':
                        result = core.compute_stats(entry, kind, ch, 0, entry['meta']['nRecords'] - 1, offset, .1)
                        assert result['avg'] is not None
                    else:
                        t, mn, mx = core.compute_buckets(entry, kind, ch, 0, entry['meta']['nRecords'] - 1,
                                                         C.DEFAULT_BUCKETS, offset, .1)
                        assert 0 < len(t) <= C.DEFAULT_BUCKETS
finally:
    elapsed = time.perf_counter() - start
    heap = tracemalloc.get_traced_memory()[1] / 1024**2
    tracemalloc.stop()
    stop.set()
    thread.join()
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (k, ctypes.c_size_t) for k in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                                          'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                                          'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    rss = counters.PeakWorkingSetSize / 1024**2
else:
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform == 'darwin' else 1024)
print(json.dumps(dict(step=step, rss_mb=rss, heap_mb=heap, seconds=elapsed,
                      gil_ms=max(gaps, default=0) * 1000, csv_rows=rows)))
'''


def measure(path, root, step):
    code = f'import sys; sys.path.insert(0, {str(SRC)!r});\n' + WORKER
    result = subprocess.run([sys.executable, '-I', '-c', code, str(path), str(root), step],
                            capture_output=True, text=True, timeout=3600)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    print(json.dumps(report, sort_keys=True), flush=True)
    return report


@pytest.mark.skipif(BIG not in ('1', '30'), reason='Set N6705C_ANALYSIS_BIG=1 or 30 for 10M budgets')
def test_10m_budgets(tmp_path):
    path = make_big(tmp_path / '10m.dlog', C.TARGET_MAX_RECORDS)
    root = tmp_path / 'cache'
    for step in ('cold', 'warm', 'csv'):
        report = measure(path, root, step)
        assert report['rss_mb'] <= C.MEM_BUDGET_PEAK_RSS_MB
        assert report['heap_mb'] <= C.MEM_BUDGET_PEAK_HEAP_MB
        if step == 'cold':
            assert report['seconds'] <= C.OPEN_COLD_MAX_S
            assert report['gil_ms'] <= C.GIL_STALL_MAX_MS
        elif step == 'warm':
            assert report['seconds'] <= C.OPEN_WARM_MAX_S


@pytest.mark.skipif(BIG != '30', reason='Set N6705C_ANALYSIS_BIG=30 for 30M no-OOM budgets')
def test_30m_budgets(tmp_path):
    path = make_big(tmp_path / '30m.dlog', C.NO_OOM_RECORDS)
    root = tmp_path / 'cache'
    for step in ('cold', 'stats', 'buckets'):
        report = measure(path, root, step)
        assert report['rss_mb'] <= C.NO_OOM_PEAK_RSS_MB
        assert report['heap_mb'] <= C.WORKER_SWITCH_HEAP_MB
        if step == 'cold':
            assert report['heap_mb'] <= C.MEM_BUDGET_PEAK_HEAP_MB
