"""T4 integration with the real core/storage; runs only when T3 is done:
N6705C_ANALYSIS_REAL_CORE=1 pytest tests/analysis/test_server_core.py"""
import json
import os
import time
import struct

import pytest

from n6705c_seq.analysis import contracts as c

pytestmark = pytest.mark.skipif(os.environ.get("N6705C_ANALYSIS_REAL_CORE") != "1",
                                reason="set N6705C_ANALYSIS_REAL_CORE=1 once T3 core/storage are ready")


def test_open_samples_csv_with_real_core(tmp_path, default_dlog):
    from n6705c_seq.analysis.gate import Gate
    from n6705c_seq.analysis.server import start_server
    from test_http import Env  # same helper client

    class RealEnv(Env):
        def __init__(self, tmp):
            self.tmp, self.gate, self.gen = tmp, Gate(lambda e: None), None
            self.core = None
            self.srv = start_server(self.gate, cache_root=tmp / "cache", prefs_path=tmp / "p.json")
            self.port = int(self.srv.origin.rsplit(":", 1)[1])

    e = RealEnv(tmp_path)
    try:
        e.enter()
        st, _, b = e.req("POST", "/open", {"grant": e.grant("open", "dlog", default_dlog)})
        assert st == 200
        (n,) = struct.unpack("<I", b[:4])
        meta = json.loads(b[4:4 + n])
        assert meta["fileName"] == default_dlog.name
        h = meta["handle"]
        out = tmp_path / "o.csv"
        st, d = e.j("POST", "/export/csv", {"grant": e.grant("save", "csv", out), "h": h, "lo": 0,
                                            "hi": 2000, "cols": "p:V1,p:I1"})
        assert st == 200 and out.read_bytes().startswith(b"14585A Exported Waveform\r\n")
        assert e.j("POST", "/release", {"h": h}) == (200, {"ok": True})
        assert e.srv.release_resources() is True
    finally:
        e.srv.shutdown(2)


# ---- contract 6.5 H4-H6 (real core)

def _real_env(tmp_path):
    from n6705c_seq.analysis.gate import Gate
    from n6705c_seq.analysis.server import start_server
    from test_http import Env

    class RealEnv(Env):
        def __init__(self, tmp):
            self.tmp, self.gate, self.gen = tmp, Gate(lambda e: None), None
            self.core = None
            self.srv = start_server(self.gate, cache_root=tmp / "cache", prefs_path=tmp / "p.json")
            self.gate.set_release_resources(self.srv.release_resources)
            self.port = int(self.srv.origin.rsplit(":", 1)[1])

    e = RealEnv(tmp_path)
    e.enter()
    return e


def _open_req(e, path):
    return e.req("POST", "/open", {"grant": e.grant("open", "dlog", path)})


def _refs():
    from n6705c_seq.analysis import core
    return [en["_refs"] for en in core._open_cache.values()]


def test_h4_eviction_in_handoff_window(tmp_path, default_dlog, monkeypatch):
    from n6705c_seq.analysis import core
    from fixtures.make_big import make_big
    monkeypatch.setattr(core, "CACHE_MAX_BYTES", 1)
    b = make_big(tmp_path / "b.dlog", 100)
    real = core.open_handle

    def wrapped(path, root, **kw):
        r = real(path, root, **kw)
        core.release_handle(real(b, root)[2])   # another opener evicts while A is between core and server
        return r
    monkeypatch.setattr(core, "open_handle", wrapped)
    e = _real_env(tmp_path)
    try:
        st, _, body = _open_req(e, default_dlog)
        assert st == 200
        (n,) = struct.unpack("<I", body[:4])
        h = json.loads(body[4:4 + n])["handle"]
        st, _, _ = e.req("GET", "/samples?h=%s&lo=0&hi=10&kind=V&ch=1" % h)
        assert st == 200
    finally:
        e.srv.release_resources()
        e.srv.shutdown(2)


def test_h5_adopt_failure_releases(tmp_path, default_dlog, monkeypatch):
    from n6705c_seq.analysis import server
    e = _real_env(tmp_path)
    try:
        def boom(self, gen, digest, entry):
            raise RuntimeError("boom")
        monkeypatch.setattr(server._App, "adopt", boom)
        st, _, body = _open_req(e, default_dlog)
        assert st == 500 and b"internal" in body
        assert _refs() == [0]
    finally:
        e.srv.shutdown(2)


def test_h6_leave_during_open(tmp_path, default_dlog, monkeypatch):
    import threading
    from n6705c_seq.analysis import core
    real = core.open_handle
    inside, go = threading.Event(), threading.Event()

    def block(*a):
        inside.set()
        assert go.wait(10)

    def wrapped(path, root, *, cancel=None, progress=None):
        return real(path, root, cancel=cancel, progress=block)
    monkeypatch.setattr(core, "open_handle", wrapped)
    e = _real_env(tmp_path)
    try:
        res = {}
        t = threading.Thread(target=lambda: res.update(r=_open_req(e, default_dlog)))
        t.start()
        assert inside.wait(10)
        assert e.gate.leave(e.gen)
        go.set()
        t.join(10)
        for _ in range(500):
            if e.gate.state()[0] == c.State.IDLE:
                break
            time.sleep(0.01)
        assert e.gate.state()[0] == c.State.IDLE
        assert core.close_all() is True and all(r == 0 for r in _refs())
    finally:
        go.set()
        e.srv.shutdown(2)
