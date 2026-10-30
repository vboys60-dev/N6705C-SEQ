"""T4: analysis HTTP server against a real Gate, a stub core (CoreApi shape) and a stub storage."""
import http.client
import json
import os
import secrets
import struct
import threading
import time

import numpy as np
import pytest

from n6705c_seq.analysis import contracts as c
from n6705c_seq.analysis.gate import Gate
from n6705c_seq.analysis.server import start_server

H0 = "0" * 40


class StubCore:
    class DlogFormatError(ValueError):
        pass

    def __init__(self):
        self.n = 5000
        self.held = {}
        self.hold_evt = threading.Event()   # set -> iter_csv may finish
        self.hold_evt.set()
        self.closed = 0

    def open_handle(self, path, cache_root, *, cancel=None, progress=None):
        if str(path).endswith("bad.dlog"):
            raise self.DlogFormatError("bad file")
        h = "%040x" % (abs(hash(os.path.basename(str(path)))) % 10**30 + 1)
        meta = {"tint": 0.00098303996, "declaredTime": None, "nRecords": self.n,
                "channelModels": ["a", "b", None, None], "activeChannels": [1, 2],
                "fields": [[1, "V"], [1, "I"], [2, "V"]], "minmax": False, "cacheVersion": 1,
                "handle": h, "lodFactors": {"level0": 64, "level1": 4096}, "lodLayout": [],
                "fileName": os.path.basename(str(path))}
        self.held[h] = self.held.get(h, 0) + 1
        return meta, b"\x00" * 16, {"meta": meta, "digest": h}

    def acquire_handle(self, digest, cache_root):
        if digest not in self.held:
            raise KeyError(digest)
        self.held[digest] += 1
        return {"meta": {"nRecords": self.n, "activeChannels": [1, 2], "tint": 0.001,
                         "fields": [[1, "V"], [1, "I"], [2, "V"]]}, "digest": digest}

    def release_handle(self, entry):
        self.held[entry["digest"]] -= 1

    def close_all(self):
        self.closed += 1
        return all(v == 0 for v in self.held.values())

    def get_values(self, entry, kind, ch, lo, hi, offset, max_limit, *, cancel=None):
        return np.arange(lo, hi + 1, dtype=np.float64) * (1 + ch) + (1 if kind == "I" else 0)

    def compute_buckets(self, entry, kind, ch, lo, hi, n, offset, max_limit, *, cancel=None):
        k = min(n, hi - lo + 1)
        t = np.linspace(lo, hi, k)
        base = (np.arange(k) + (1 if offset else 0)).astype(np.float64)
        return t, base, base + 1

    def compute_stats(self, entry, kind, ch, lo, hi, offset, max_limit, *, cancel=None):
        return {"min": 1.0, "max": float("nan"), "avg": np.float32(2.5)}

    def iter_csv(self, primary, columns, lo, hi, offset, max_limit, now, *, cancel=None):
        yield (c.CSV_TITLE_LINE + "\r\n").encode()
        yield ("Sampling Period:0.00098303996\r\n").encode()
        yield ("Time," + ",".join(x["label"] for x in columns) + "\r\n").encode()
        while not self.hold_evt.is_set():
            if cancel.is_set():
                raise c.AnalysisCancelled()
            time.sleep(0.01)
        for i in range(lo, hi + 1):
            yield b"%.8f\r\n" % (i * 0.001)


class StubStorage:
    def __init__(self):
        self.locks = {}

    def default_cache_root(self):
        raise AssertionError

    default_prefs_path = default_cache_root

    def sweep_tmp(self, root):
        pass

    def atomic_write_bytes(self, path, data):
        return self.atomic_write_iter(path, [data])

    def atomic_write_iter(self, path, chunks, cancel=None):
        tmp = "%s.%s%s" % (path, secrets.token_hex(4), c.PART_SUFFIX)
        n = 0
        try:
            with open(tmp, "wb") as f:
                for ch in chunks:
                    f.write(ch)
                    n += len(ch)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return n


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.gate = Gate(lambda e: None)
        self.core = StubCore()
        self.srv = start_server(self.gate, cache_root=tmp_path / "cache",
                                prefs_path=tmp_path / "prefs.json",
                                core=self.core, storage=StubStorage())
        self.port = int(self.srv.origin.rsplit(":", 1)[1])
        self.gen = None

    def enter(self):
        self.gen, _ = self.gate.enter(lambda: None)
        return self.gen

    def grant(self, mode, purpose, path):
        return self.gate.issue_grant(self.gen, mode, purpose, str(path)).id

    def req(self, method, path, body=None, headers=None, token=True, gen="auto", raw=None, host=None):
        hdr = {}
        if token:
            hdr[c.HEADER_TOKEN] = self.srv.token
        if gen == "auto":
            gen = self.gen if self.gen is not None else 0
        if gen is not None:
            hdr[c.HEADER_GEN] = str(gen)
        hdr.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=host is not None)
            if host is not None:
                conn.putheader("Host", host)
            for k, v in hdr.items():
                conn.putheader(k, v)
            if data is not None:
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            r = conn.getresponse()
            return r.status, r, r.read()
        finally:
            conn.close()

    def j(self, *a, **k):
        st, r, b = self.req(*a, **k)
        return st, json.loads(b)

    def open_file(self, name="a.dlog"):
        p = self.tmp / name
        p.write_bytes(b"x")
        st, r, b = self.req("POST", "/open", {"grant": self.grant("open", "dlog", p)})
        assert st == 200, b
        (n,) = struct.unpack("<I", b[:4])
        return json.loads(b[4:4 + n])


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.srv.shutdown(2)


@pytest.fixture
def live(env):
    env.enter()
    return env


def split(b):
    (n,) = struct.unpack("<I", b[:4])
    assert n % 4 == 0
    return json.loads(b[4:4 + n]), b[4 + n:]


# ---------------------------------------------------------------- access checks

def test_health_selfcheck_and_static(env):
    assert env.srv.self_check()
    st, r, b = env.req("GET", "/viewer.html", token=False, gen=None)
    assert st == 200 and "frame-ancestors" in r.getheader("Content-Security-Policy")
    assert r.getheader("Cache-Control") == "no-store"
    assert env.srv.url(3, "zh").endswith("/viewer.html#t=%s&gen=3&lang=zh" % env.srv.token)


def test_no_token_and_bad_token(live):
    st, b = live.j("GET", "/prefs", token=False)
    assert (st, b["code"]) == (403, "token")
    st, r, _ = live.req("GET", "/prefs", headers={c.HEADER_TOKEN: "nope"})
    assert st == 403


def test_host_mismatch_even_for_static_and_health(live):
    for p in ("/health", "/viewer.html", "/prefs"):
        st, _, b = live.req("GET", p, host="evil.example:%d" % live.port)
        assert st == 403 and json.loads(b)["code"] == "host"


def test_non_whitelist_404_and_method_405(live):
    for p in ("/", "/server.py", "/shell_stub.html", "/../x", "/web/viewer.html", "/nope"):
        st, _, b = live.req("GET", p, token=False)
        assert st == 404, p
    assert live.req("POST", "/health", raw=b"")[0] == 405
    assert live.req("GET", "/open")[0] == 405
    assert live.req("POST", "/viewer.html", raw=b"")[0] == 405


def test_missing_or_bad_gen_and_req(live):
    assert live.j("GET", "/prefs", gen=None)[0] == 400
    assert live.j("GET", "/prefs", headers={c.HEADER_GEN: "x"})[0] == 400
    assert live.j("GET", "/prefs", headers={c.HEADER_REQ: "bad id!"})[0] == 400


def test_power_mode_423_and_stale_409(env):
    env.gate.reserve("power")
    st, b = env.j("GET", "/prefs", gen=0)
    assert (st, b["code"]) == (423, "locked")
    assert env.j("POST", "/cancel", {"req": "r1"}, gen=0)[0] == 423
    assert env.j("POST", "/release", {"h": H0}, gen=0)[0] == 423


def test_stale_gen_409_and_after_leave_423(live):
    st, b = live.j("GET", "/prefs", gen=live.gen + 5)
    assert (st, b["code"]) == (409, "stale_gen")
    assert live.j("POST", "/cancel", {"req": "r"}, gen=live.gen + 1)[0] == 409
    live.gate.leave(live.gen)
    assert live.j("GET", "/prefs")[0] == 423


def test_progress(live, tmp_path):
    core, started, go = live.core, threading.Event(), threading.Event()
    orig = core.open_handle

    def slow(path, cache_root, *, cancel=None, progress=None):
        for st, pct in (("parse", 42.7), ("parse", -1), ("parse", 150)):
            progress(st, pct)
            if pct == 42.7:
                started.set()
                assert go.wait(5)
        if str(path).endswith("fail.dlog"):
            raise core.DlogFormatError("x")
        return orig(path, cache_root, cancel=cancel)

    core.open_handle = slow
    p = tmp_path / "a.dlog"
    p.write_bytes(b"x")
    g = live.grant("open", "dlog", p)
    out = []
    t = threading.Thread(target=lambda: out.append(live.req(
        "POST", "/open", {"grant": g}, headers={c.HEADER_REQ: "op1"})[0]))
    t.start()
    assert started.wait(5)
    # fill all JOB slots is not needed: /progress is CONTROL (see 503 test); check values here
    assert live.j("GET", "/progress?req=op1") == (200, {"ok": True, "stage": "parse", "pct": 42})
    assert live.j("GET", "/progress?req=nosuch") == (200, {"ok": False})
    assert live.j("GET", "/progress?req=op1", gen=live.gen + 1)[0] == 409
    assert live.j("GET", "/progress")[0] == 400
    assert live.j("GET", "/progress?req=bad%20id")[0] == 400
    assert live.j("GET", "/progress?req=op1", token=False)[0] == 403
    go.set()
    t.join(5)
    assert out == [200]
    assert live.j("GET", "/progress?req=op1") == (200, {"ok": False})
    # failure leaves no residue; open without req never registers
    p2 = tmp_path / "fail.dlog"
    p2.write_bytes(b"x")
    assert live.req("POST", "/open", {"grant": live.grant("open", "dlog", p2)},
                    headers={c.HEADER_REQ: "op2"})[0] == 422
    assert live.j("GET", "/progress?req=op2") == (200, {"ok": False})
    core.open_handle = orig


def test_progress_clamp_and_not_job_limited(live, tmp_path):
    seen, stop, n = {}, threading.Event(), c.MAX_CONCURRENT_JOBS
    orig = live.core.open_handle

    def blk(path, cache_root, *, cancel=None, progress=None):
        r = os.path.basename(str(path))[0]
        for pct in (-1, 150):
            progress("lod", pct)
            seen.setdefault(r, []).append(live.j("GET", "/progress?req=op" + r)[1]["pct"])
        assert stop.wait(10)
        return orig(path, cache_root, cancel=cancel)

    live.core.open_handle = blk
    ts = []
    for i in range(n):
        p = tmp_path / ("%d.dlog" % i)
        p.write_bytes(b"x")
        g = live.grant("open", "dlog", p)
        ts.append(threading.Thread(target=lambda g=g, i=i: live.req(
            "POST", "/open", {"grant": g}, headers={c.HEADER_REQ: "op%d" % i})))
        ts[-1].start()
    for _ in range(250):
        if len(seen) == n and all(len(v) == 2 for v in seen.values()):
            break
        time.sleep(0.02)
    assert all(v == [0, 100] for v in seen.values()) and len(seen) == n
    assert live.j("GET", "/prefs")[0] == 503
    assert live.j("GET", "/progress?req=op0") == (200, {"ok": True, "stage": "lod", "pct": 100})
    stop.set()
    for t in ts:
        t.join(5)
    live.core.open_handle = orig


# ---------------------------------------------------------------- handles and params

def test_open_release_and_resources(live):
    m = live.open_file("é.dlog")
    assert m["fileName"] == "é.dlog" and sum(live.core.held.values()) == 1
    assert live.j("POST", "/release", {"h": m["handle"]}) == (200, {"ok": True})
    assert sum(live.core.held.values()) == 0
    assert live.j("POST", "/release", {"h": m["handle"]}) == (200, {"ok": True})  # idempotent
    assert live.j("POST", "/release", {"h": H0}) == (200, {"ok": True})
    assert live.j("POST", "/release", {"h": "nope"})[0] == 400
    live.open_file("b.dlog")
    assert live.srv.release_resources() is True and live.core.closed == 1


def test_open_grant_and_format_errors(live):
    assert live.j("POST", "/open", {"grant": "bogus"})[0] == 403
    p = live.tmp / "bad.dlog"
    p.write_bytes(b"x")
    st, b = live.j("POST", "/open", {"grant": live.grant("open", "dlog", p)})
    assert (st, b["code"]) == (422, "format")
    g = live.grant("open", "markers", p)            # wrong purpose
    assert live.j("POST", "/open", {"grant": g})[0] == 403
    assert live.j("POST", "/open", {"grant": g})[0] == 403  # single use even on failure


@pytest.mark.parametrize("route", ["/samples", "/buckets", "/stats"])
def test_bad_keys_400(live, route):
    h = live.open_file()["handle"]
    for keys in ("X9", "V5", "", "V1,,I1", ",".join(["V1"] * 13)):
        st, b = live.j("GET", "%s?h=%s&lo=0&hi=10&keys=%s" % (route, h, keys))
        assert (st, b["code"]) == (400, "bad_request"), keys
    assert live.j("GET", "%s?h=nope&lo=0&hi=10&keys=V1" % route)[0] == 400
    st, b = live.j("GET", "%s?h=%s&lo=0&hi=10&keys=V1" % (route, H0))
    assert (st, b["code"]) == (404, "handle")
    assert live.j("GET", "%s?h=%s&lo=a&hi=10&keys=V1" % (route, h))[0] == 400
    assert live.j("GET", "%s?h=%s&hi=10&keys=V1" % (route, h))[0] == 400


def test_samples_layout_and_values(live):
    h = live.open_file()["handle"]
    st, r, b = live.req("GET", "/samples?h=%s&keys=V1,I1&lo=100&hi=1099" % h)
    meta, pay = split(b)
    assert meta["lo"] == 100 and meta["hi"] == 1099
    cur = 0
    for it in meta["layout"]:
        assert it["offset"] == cur * 4
        cur += it["length"]
    assert meta["layout"][-1]["offset"] + meta["layout"][-1]["length"] * 4 == len(pay)
    for it in meta["layout"]:
        got = np.frombuffer(pay, "<f4", it["length"], it["offset"])
        want = live.core.get_values(None, it["key"][0], int(it["key"][1]), 100, 1099, False, 0.0)
        assert np.array_equal(got, want.astype("<f4"))
    # default keys: active channels, V then I
    meta, _ = split(live.req("GET", "/samples?h=%s&lo=-5&hi=999999" % h)[2])
    assert [i["key"] for i in meta["layout"]] == ["V1", "I1", "V2", "I2"] and meta["hi"] == 4999
    # key for an unrecorded channel
    assert live.j("GET", "/samples?h=%s&keys=V3&lo=0&hi=5" % h)[0] == 400


def test_buckets_layout(live):
    h = live.open_file()["handle"]
    st, r, b = live.req("GET", "/buckets?h=%s&keys=V1,I2&lo=0&hi=4999&n=100&offset=1" % h)
    meta, pay = split(b)
    for it in meta["layout"]:
        n = it["length"]
        assert n <= 100
        t, mn, mx = (np.frombuffer(pay, "<f4", n, it["offset"] + k * n * 4) for k in range(3))
        et, emn, emx = live.core.compute_buckets(None, "V", 1, 0, 4999, 100, True, 0.0)
        assert np.array_equal(mn, emn.astype("<f4")) and (mn > 0).all() and np.array_equal(mx, emx.astype("<f4"))
    for n in ("0", str(c.MAX_BUCKETS + 1), "x"):
        assert live.j("GET", "/buckets?h=%s&keys=V1&lo=0&hi=9&n=%s" % (h, n))[0] == 400


def test_stats_json(live):
    h = live.open_file()["handle"]
    st, b = live.j("GET", "/stats?h=%s&keys=V1,P1&lo=0&hi=10" % h)
    assert st == 200 and b["V1"] == {"min": 1.0, "max": None, "avg": 2.5}


def test_samples_413_formula(live):
    live.core.n = 3_000_000
    h = live.open_file()["handle"]
    k12 = ",".join(["V1", "I1", "V2", "I2"] * 3)
    lo_ok = c.use_buckets(1_396_736, 12)
    assert not lo_ok and c.use_buckets(1_396_737, 12)
    st, b = live.j("GET", "/samples?h=%s&keys=%s&lo=0&hi=2000000" % (h, k12))
    assert (st, b["code"]) == (413, "too_large")
    assert live.j("GET", "/samples?h=%s&keys=%s&lo=0&hi=1396736" % (h, k12))[0] == 413  # span 1,396,737
    assert live.j("GET", "/samples?h=%s&keys=V1&lo=0&hi=2000000" % h)[0] == 413           # span > MAX
    assert c.samples_response_bytes(1_396_736, 12) <= c.MAX_RESPONSE_BYTES


# ---------------------------------------------------------------- export / save / import / prefs

def test_export_csv_ok_and_name_ignored(live, tmp_path):
    live.core.n = 2001
    h = live.open_file()["handle"]
    out = tmp_path / "資料.csv"
    g = live.grant("save", "csv", out)
    st, r, b = live.req("POST", "/export/csv", {
        "grant": g, "h": h, "lo": 0, "hi": 2000, "cols": "p:V1,p:I1,p:P2", "name": "evil.csv"})
    d = json.loads(b)
    assert st == 200 and d["name"] == "資料.csv" and d["bytes"] == out.stat().st_size
    assert r.getheader("Content-Disposition") is None
    lines = out.read_bytes().split(b"\r\n")
    assert lines[0] == c.CSV_TITLE_LINE.encode() and lines[1] == b"Sampling Period:0.00098303996"
    assert lines[2].count(b"Instrument A Channel") == 3 and len(lines) - 4 == 2001
    assert not list(tmp_path.glob("*.part")) and not (tmp_path / "evil.csv").exists()


def test_export_csv_param_errors(live, tmp_path):
    h = live.open_file()["handle"]
    base = {"h": h, "lo": 0, "hi": 9, "cols": "p:V1"}
    g = lambda: live.grant("save", "csv", tmp_path / "o.csv")  # noqa: E731
    assert live.j("POST", "/export/csv", {**base, "grant": g(), "cols": "p:V1,c:I1"})[0] == 400  # no hc
    assert live.j("POST", "/export/csv", {**base, "grant": g(), "cols": "p:V9"})[0] == 400
    assert live.j("POST", "/export/csv", {**base, "grant": g(), "cols": ""})[0] == 400
    assert live.j("POST", "/export/csv", {**base, "grant": g(), "hc": "zz"})[0] == 400
    st, b = live.j("POST", "/export/csv", {**base, "grant": g(), "hc": H0})
    assert (st, b["code"]) == (404, "handle")
    assert live.j("POST", "/export/csv", {**base, "grant": "bogus"})[0] == 403
    assert live.j("POST", "/export/csv", {**base, "grant": live.grant("save", "png", tmp_path / "o.csv")})[0] == 403
    assert live.j("POST", "/export/csv", None, raw=b"x" * (c.MAX_JSON_BODY_BYTES + 1))[0] == 413


def test_export_cancel_and_cancel_unlimited_by_jobs(live, tmp_path):
    h = live.open_file()["handle"]
    live.core.hold_evt.clear()
    res, threads = {}, []

    def run(i):
        g = live.grant("save", "csv", tmp_path / ("o%d.csv" % i))
        res[i] = live.req("POST", "/export/csv", {"grant": g, "h": h, "lo": 0, "hi": 9, "cols": "p:V1"},
                          headers={c.HEADER_REQ: "job%d" % i})[0]

    for i in range(c.MAX_CONCURRENT_JOBS):
        t = threading.Thread(target=run, args=(i,))
        t.start()
        threads.append(t)
    for _ in range(200):
        if live.gate.job_count() == c.MAX_CONCURRENT_JOBS:
            break
        time.sleep(0.02)
    st, b = live.j("GET", "/prefs")
    assert (st, b["code"]) == (503, "overloaded")
    assert live.j("POST", "/cancel", {"req": "nosuch"}) == (200, {"ok": False})
    for i in range(c.MAX_CONCURRENT_JOBS):
        assert live.j("POST", "/cancel", {"req": "job%d" % i}) == (200, {"ok": True})
    for t in threads:
        t.join(5)
    assert set(res.values()) == {409}
    assert not list(tmp_path.glob("o*")), list(tmp_path.glob("o*"))
    assert live.j("GET", "/prefs")[0] == 200


def test_save(live, tmp_path):
    out = tmp_path / "shot.png"
    hd = lambda g, p="png": {c.HEADER_GRANT: g, c.HEADER_PURPOSE: p}  # noqa: E731
    g = live.grant("save", "png", out)
    st, b = live.j("POST", "/save", raw=b"PNGDATA", headers=hd(g))
    assert (st, b) == (200, {"ok": True, "bytes": 7, "name": "shot.png"}) and out.read_bytes() == b"PNGDATA"
    assert live.j("POST", "/save", raw=b"x", headers={c.HEADER_GRANT: g})[0] == 400          # no purpose
    for p in ("csv", "exe"):
        g = live.grant("save", "csv", out)
        assert live.j("POST", "/save", raw=b"x", headers=hd(g, p))[0] == 400
    assert live.j("POST", "/save", raw=b"x", headers=hd(live.grant("save", "html", out), "png"))[0] == 403
    assert live.j("POST", "/save", raw=b"x", headers=hd(live.grant("save", "csv", out), "png"))[0] == 403
    # oversize rejected from the header alone, before any body is read
    conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=5)
    conn.putrequest("POST", "/save")
    for k, v in {c.HEADER_TOKEN: live.srv.token, c.HEADER_GEN: str(live.gen),
                 c.HEADER_PURPOSE: "png", c.HEADER_GRANT: live.grant("save", "png", out),
                 "Content-Length": str(c.MAX_UPLOAD_BYTES + 1)}.items():
        conn.putheader(k, v)
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


def test_import_markers(live, tmp_path):
    p = tmp_path / "m.json"
    p.write_text('{"a": [1, 2]}', encoding="utf-8")
    st, b = live.j("POST", "/import/markers", {"grant": live.grant("open", "markers", p)})
    assert (st, b) == (200, {"ok": True, "data": {"a": [1, 2]}})
    p.write_bytes(b"\xff\xfe\x00bad")
    st, b = live.j("POST", "/import/markers", {"grant": live.grant("open", "markers", p)})
    assert (st, b["code"]) == (422, "format")
    p.write_bytes(b"[" + b" " * c.MAX_MARKERS_FILE_BYTES + b"]")
    assert live.j("POST", "/import/markers", {"grant": live.grant("open", "markers", p)})[0] == 413
    assert live.j("POST", "/import/markers", {"grant": live.grant("open", "dlog", p)})[0] == 403


def test_prefs_roundtrip(live, tmp_path):
    assert live.j("GET", "/prefs") == (200, c.PREFS_DEFAULT)
    good = {"version": 1, "theme": "light", "fold": {"x": True}}
    assert live.j("POST", "/prefs", good) == (200, {"ok": True})
    assert live.j("GET", "/prefs") == (200, good)
    for bad in ({**good, "theme": "pink"}, {**good, "extra": 1}, {**good, "fold": {"x": 1}}, {"version": 2}):
        assert live.j("POST", "/prefs", bad)[0] == 400
    (tmp_path / "prefs.json").write_text("{corrupt", encoding="utf-8")
    assert live.j("GET", "/prefs") == (200, c.PREFS_DEFAULT)
    assert live.j("POST", "/prefs", None, raw=b" " * (c.MAX_PREFS_BYTES + 1))[0] == 413


def test_shutdown_is_fast(tmp_path):
    e = Env(tmp_path)
    t0 = time.monotonic()
    e.srv.shutdown()
    assert time.monotonic() - t0 < c.CLOSING_CLEANUP_TIMEOUT_S
    assert not e.srv.self_check()
