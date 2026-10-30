"""Analysis HTTP server (contract section 3). stdlib + numpy + analysis.* only.

Check order per request: Host -> route/method -> static -> token -> gen/req ->
JOB (gate.job_begin) or CONTROL (gate.state) -> params -> run.
"""

import datetime
import hmac
import http.client
import json
import logging
import math
import re
import secrets
import select
import struct
import threading
import time
import urllib.parse
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from . import contracts as c

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent / "web"
_THEMES = ("dark", "soft-dark", "light")
_INT_RE = re.compile(r"^-?\d{1,18}$")
_ROUTE_PATHS = {p for _, p in c.ROUTES}
_E = c.ErrorCode
_IO_POLL_S = 0.25
_IO_DEADLINE_S = 30.0
_IO_CHUNK_BYTES = 64 * 1024


class _IOStopped(Exception):
    """Abort an interrupted/expired transfer without writing another response."""


class _HttpError(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code, self.text = code, text


def frame_binary(meta, payload):
    """[uint32 LE meta length][JSON meta padded to x4][payload]."""
    m = _json_bytes(meta)
    m += b" " * ((-len(m)) % 4)
    return struct.pack("<I", len(m)) + m + payload


def _json_default(o):
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o).__name__)


def _json_bytes(obj):
    def finite(v):
        if isinstance(v, dict):
            return {k: finite(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [finite(x) for x in v]
        return _clean(v)
    return json.dumps(finite(obj), default=_json_default, allow_nan=False).encode("utf-8")


def _bad(text):
    return _HttpError(_E.BAD_REQUEST, text)


# ---------------------------------------------------------------- param helpers

def _int(v, name):
    if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and _INT_RE.match(v))):
        raise _bad("%s must be an integer" % name)
    return int(v)


def _num(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise _bad("%s must be a number" % name)
    if isinstance(v, bool) or not math.isfinite(f):
        raise _bad("%s must be a finite number" % name)
    return f


def _handle_arg(v, name="h"):
    if not isinstance(v, str) or not re.match(c.HANDLE_PATTERN, v):
        raise _bad("%s is not a valid handle" % name)
    return v


def _parse_keys(raw):
    items = (raw or "").split(",")
    if not raw or len(items) > c.MAX_KEYS or not all(re.match(c.KEY_PATTERN, k) for k in items):
        raise _bad("keys must be like V1,I1,P3 (max %d)" % c.MAX_KEYS)
    return [(k[0], int(k[1:])) for k in items]


def _qs(qs, name):
    v = qs.get(name)
    return v[0] if v else None


def _qs_common(qs):
    """offset, maxLimit shared by samples/buckets/stats."""
    offset = _qs(qs, "offset") in c.QS_TRUE
    raw = _qs(qs, "maxLimit")
    return offset, (c.DEFAULT_MAX_LIMIT if raw is None else _num(raw, "maxLimit"))


def _need(qs, name):
    v = _qs(qs, name)
    if v is None:
        raise _bad("missing %s" % name)
    return v


def _range(entry, lo, hi):
    top = entry["meta"]["nRecords"] - 1
    lo, hi = max(0, min(top, lo)), max(0, min(top, hi))
    if hi < lo:
        raise _bad("hi < lo")
    return lo, hi


def _check_keys(entry, keys):
    active = entry["meta"]["activeChannels"]
    if any(ch not in active for _, ch in keys):
        raise _bad("key channel is not recorded")


def _f4(a):
    return np.ascontiguousarray(a, dtype="<f4")


def _clean(v):
    v = v.item() if hasattr(v, "item") else v
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _prefs_ok(p):
    return (isinstance(p, dict) and set(p) == {"version", "theme", "fold"}
            and p["version"] == c.PREFS_VERSION and p["theme"] in _THEMES
            and isinstance(p["fold"], dict)
            and all(isinstance(k, str) and isinstance(v, bool) for k, v in p["fold"].items()))


# ---------------------------------------------------------------- server state

class _App:
    def __init__(self, gate, core, storage, cache_root, prefs_path, dev):
        self.gate, self.core, self.storage = gate, core, storage
        self.cache_root, self.prefs_path = cache_root, prefs_path
        self.token = secrets.token_urlsafe(32)
        self.static = {"/" + n for n in c.STATIC_FILES + (c.DEV_STATIC_FILES if dev else ())}
        self.lock = threading.Lock()
        self.active = 0
        self.held = {}  # (gen, digest) -> [Entry, ...], one per /open
        self.progress = {}  # (gen, req) -> (stage, pct) of in-flight /open; guarded by self.lock

    def adopt(self, gen, digest, entry):
        # takes over the reference open_handle returned; must not call core (contract 6.5)
        with self.lock:
            self.held.setdefault((gen, digest), []).append(entry)

    def unhold(self, gen, digest):
        with self.lock:
            key = (gen, digest)
            lst = self.held.get(key)
            e = lst.pop() if lst else None
            if lst is not None and not lst:
                del self.held[key]
        if e is not None:
            self.core.release_handle(e)

    @contextmanager
    def pinned(self, h):
        try:
            e = self.core.acquire_handle(h, self.cache_root)
        except KeyError:
            raise _HttpError(_E.HANDLE, "unknown handle")
        try:
            yield e
        finally:
            self.core.release_handle(e)

    def release_all(self):
        with self.lock:
            items = [e for lst in self.held.values() for e in lst]
            self.held.clear()
        for e in items:
            try:
                self.core.release_handle(e)
            except Exception:
                log.exception("release_handle failed")
        return bool(self.core.close_all())


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # No read-ahead: select() below must see every pending body byte on the socket.
    rbufsize = 0
    app = None
    port = 0

    def setup(self):
        super().setup()
        self.connection.settimeout(_IO_POLL_S)

    def _check_io(self, deadline):
        job = self._job
        if (job is not None and job.cancel.is_set()) or time.monotonic() >= deadline:
            raise _IOStopped()

    def log_message(self, *a):
        pass

    # ---- responses
    def _send(self, status, ctype, body, extra=()):
        if len(body) > c.MAX_RESPONSE_BYTES:
            return self._error(_HttpError(_E.TOO_LARGE, "response too large"))
        deadline = time.monotonic() + _IO_DEADLINE_S
        self._check_io(deadline)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        view = memoryview(body)
        for start in range(0, len(body), _IO_CHUNK_BYTES):
            self._check_io(deadline)
            self.wfile.write(view[start:start + _IO_CHUNK_BYTES])

    def _json(self, obj, status=200):
        self._send(status, "application/json; charset=utf-8",
                   _json_bytes(obj))

    def _error(self, e):
        self._job = None  # computation cancellation still returns the contract's 409
        if self.command == "POST" and not self._consumed:
            self.close_connection = True  # unread body would corrupt keep-alive
        self._json({"error": e.text, "code": e.code.value}, c.HTTP_STATUS[e.code])

    # ---- body
    def _length(self, limit):
        raw = self.headers.get("Content-Length")
        if raw is None or not re.match(r"^\d{1,12}$", raw):
            raise _bad("Content-Length required")
        n = int(raw)
        if n > limit:
            raise _HttpError(_E.TOO_LARGE, "body too large")
        return n

    def _body_json(self, limit=c.MAX_JSON_BODY_BYTES):
        n = self._length(limit)
        data = b"".join(self._body_parts(n))
        try:
            obj = json.loads(data.decode("utf-8"))
        except ValueError:
            raise _bad("invalid JSON")
        if not isinstance(obj, dict):
            raise _bad("JSON object expected")
        return obj

    def _body_parts(self, n):
        deadline = time.monotonic() + _IO_DEADLINE_S
        left = n
        while left:
            self._check_io(deadline)
            if not select.select([self.connection], [], [], _IO_POLL_S)[0]:
                continue
            part = self.rfile.read(min(left, _IO_CHUNK_BYTES))
            self._check_io(deadline)  # cancellation can arrive during the last read
            if not part:
                self.close_connection = True
                raise _bad("short body")
            left -= len(part)
            yield part
        self._check_io(deadline)
        self._consumed = True

    # ---- dispatch
    def _dispatch(self):
        try:
            self._dispatch_request()
        except (_IOStopped, TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _dispatch_request(self):
        self._consumed = False
        self._job = None
        try:
            self._route()
        except _HttpError as e:
            self._error(e)
        except (_IOStopped, TimeoutError, BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:  # noqa: BLE001 - mapped per contract 3.2
            self._error(self._map(e))

    def _map(self, e):
        core = self.app.core
        if isinstance(e, c.AnalysisCancelled):
            return _HttpError(_E.CANCELLED, "cancelled")
        if isinstance(e, c.LimitExceeded):
            return _HttpError(_E.TOO_LARGE, str(e))
        if isinstance(e, getattr(core, "DlogFormatError", ())):
            return _HttpError(_E.FORMAT, str(e))
        if isinstance(e, KeyError):
            return _HttpError(_E.HANDLE, "unknown handle")
        if isinstance(e, c.SourceChanged):
            return _HttpError(_E.SOURCE_CHANGED, "source file changed")
        if isinstance(e, c.CacheWriteError):
            return _HttpError(_E.STORAGE, str(e))
        if isinstance(e, OSError):
            return _HttpError(_E.STORAGE, "file error")
        log.exception("analysis request failed")
        return _HttpError(_E.INTERNAL, "internal error")

    def _route(self):
        app, method = self.app, self.command
        if self.headers.get("Host") != "%s:%d" % (c.HOST, self.port):
            raise _HttpError(_E.HOST, "bad host")
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query, keep_blank_values=True)
        kind = c.ROUTES.get((method, path))
        if path in app.static:
            if method != "GET":
                raise _HttpError(_E.METHOD, "method not allowed")
            return self._static(path)
        if kind is None:
            raise (_HttpError(_E.METHOD, "method not allowed") if path in _ROUTE_PATHS
                   else _HttpError(_E.NOT_FOUND, "not found"))
        if kind is c.RouteKind.PUBLIC:
            return self._json({"ok": True, "version": c.CONTRACT_VERSION})
        got = self.headers.get(c.HEADER_TOKEN) or ""
        if not hmac.compare_digest(got.encode("utf-8"), app.token.encode("utf-8")):
            raise _HttpError(_E.TOKEN, "bad token")
        g = self.headers.get(c.HEADER_GEN)
        if g is None or not _INT_RE.match(g):
            raise _bad("%s required" % c.HEADER_GEN)
        gen = int(g)
        req = self.headers.get(c.HEADER_REQ)
        if req is not None and not re.match(c.REQ_ID_PATTERN, req):
            raise _bad("bad %s" % c.HEADER_REQ)
        self._gen = gen
        fn = getattr(self, "_r_" + path.strip("/").replace("/", "_"))
        if kind is c.RouteKind.CONTROL:
            self._need_session(gen)
            return fn(None, qs, gen)
        job = app.gate.job_begin(gen, req)
        if job is None:
            self._need_session(gen)
            raise _HttpError(_E.STALE_GEN, "stale generation")  # lost a race; treat as stale
        self._job = job
        with app.lock:
            app.active += 1
            over = app.active > c.MAX_CONCURRENT_JOBS
        try:
            if over:
                raise _HttpError(_E.OVERLOADED, "too many jobs")
            fn(job, qs, gen)
        finally:
            with app.lock:
                app.active -= 1
            app.gate.job_end(job)

    def _need_session(self, gen):
        st, g = self.app.gate.state()
        if st is not c.State.ANALYSIS:
            raise _HttpError(_E.LOCKED, "analysis is not active")
        if g != gen:
            raise _HttpError(_E.STALE_GEN, "stale generation")

    def _static(self, path):
        f = WEB_DIR / path.lstrip("/")
        try:
            body = f.read_bytes()
        except OSError:
            raise _HttpError(_E.NOT_FOUND, "not found")
        ctype = "text/html; charset=utf-8" if f.suffix == ".html" else "application/octet-stream"
        self._send(200, ctype, body, [(
            "Content-Security-Policy", "frame-ancestors http://127.0.0.1:* http://localhost:*")])

    def _grant(self, grant, mode, purpose):
        if not isinstance(grant, str):
            raise _HttpError(_E.GRANT, "grant required")
        # ROUTE_GRANT supplies mode/purposes; /save passes its header purpose
        path = self.app.gate.consume_grant(grant, self._gen, mode, purpose)
        if path is None:
            raise _HttpError(_E.GRANT, "grant invalid or expired")
        return Path(path)

    # ---- routes
    def _r_open(self, job, qs, gen):
        body = self._body_json()
        path = self._grant(body.get("grant"), "open", "dlog")
        app = self.app
        key = (gen, job.req) if job.req else None
        cb = None
        if key:
            def cb(stage, pct):  # never raises: would abort core
                try:
                    v = (str(stage), int(min(100, max(0, pct))))
                except (TypeError, ValueError, OverflowError):
                    return
                with app.lock:
                    app.progress[key] = v
            cb("hash", 0)
        try:
            meta, blob, entry = app.core.open_handle(path, app.cache_root, cancel=job.cancel, progress=cb)
        finally:
            if key:
                with app.lock:
                    app.progress.pop(key, None)
        try:
            # Validate the complete framed size before handing the reference to app.
            head = frame_binary(meta, b"")
            if len(head) + len(blob) > c.MAX_RESPONSE_BYTES:
                raise _HttpError(_E.TOO_LARGE, "response too large")
            self._check_io(time.monotonic() + _IO_DEADLINE_S)
            app.adopt(gen, meta["handle"], entry)
        except BaseException:
            app.core.release_handle(entry)
            raise
        self._send(200, "application/octet-stream", head + blob)

    def _r_release(self, job, qs, gen):
        h = _handle_arg(self._body_json().get("h"))
        self.app.unhold(gen, h)  # unknown or already released -> still ok
        self._json({"ok": True})

    def _r_cancel(self, job, qs, gen):
        req = self._body_json().get("req")
        if not isinstance(req, str) or not re.match(c.REQ_ID_PATTERN, req):
            raise _bad("req required")
        self._json({"ok": bool(self.app.gate.cancel_req(gen, req))})

    def _r_progress(self, job, qs, gen):
        req = _qs(qs, "req")
        if req is None or not re.match(c.REQ_ID_PATTERN, req):
            raise _bad("req required")
        with self.app.lock:
            v = self.app.progress.get((gen, req))
        self._json({"ok": True, "stage": v[0], "pct": v[1]} if v else {"ok": False})

    def _r_samples(self, job, qs, gen):
        h = _handle_arg(_qs(qs, "h"))
        lo, hi = _int(_need(qs, "lo"), "lo"), _int(_need(qs, "hi"), "hi")
        offset, max_limit = _qs_common(qs)
        raw = _qs(qs, "keys")
        keys = _parse_keys(raw) if raw is not None else None
        with self.app.pinned(h) as e:
            lo, hi = _range(e, lo, hi)
            if keys is None:
                keys = [(k, ch) for ch in e["meta"]["activeChannels"] for k in "VI"]
                if not keys:
                    raise _bad("no channels")
            _check_keys(e, keys)
            if c.use_buckets(hi - lo + 1, len(keys)):
                raise _HttpError(_E.TOO_LARGE, "range too large for /samples; use /buckets")
            chunks, layout, cur = [], [], 0
            for kind, ch in keys:
                a = _f4(self.app.core.get_values(e, kind, ch, lo, hi, offset, max_limit,
                                                 cancel=job.cancel))
                layout.append({"key": "%s%d" % (kind, ch), "offset": cur, "length": int(a.size)})
                chunks.append(a.tobytes())
                cur += a.nbytes
        self._send(200, "application/octet-stream",
                   frame_binary({"lo": lo, "hi": hi, "layout": layout}, b"".join(chunks)))

    def _query_keys(self, qs):
        h = _handle_arg(_qs(qs, "h"))
        keys = _parse_keys(_qs(qs, "keys"))
        lo, hi = _int(_need(qs, "lo"), "lo"), _int(_need(qs, "hi"), "hi")
        return h, keys, lo, hi, *_qs_common(qs)

    def _r_buckets(self, job, qs, gen):
        h, keys, lo, hi, offset, max_limit = self._query_keys(qs)
        n = _int(_qs(qs, "n") or c.DEFAULT_BUCKETS, "n")
        if not 1 <= n <= c.MAX_BUCKETS:
            raise _bad("n must be 1..%d" % c.MAX_BUCKETS)
        with self.app.pinned(h) as e:
            lo, hi = _range(e, lo, hi)
            _check_keys(e, keys)
            chunks, layout, cur = [], [], 0
            for kind, ch in keys:
                t, mn, mx = self.app.core.compute_buckets(
                    e, kind, ch, lo, hi, n, offset, max_limit, cancel=job.cancel)
                tri = [_f4(a) for a in (t, mn, mx)]
                layout.append({"key": "%s%d" % (kind, ch), "offset": cur, "length": int(tri[0].size)})
                for a in tri:
                    chunks.append(a.tobytes())
                    cur += a.nbytes
        self._send(200, "application/octet-stream", frame_binary({"layout": layout}, b"".join(chunks)))

    def _r_stats(self, job, qs, gen):
        h, keys, lo, hi, offset, max_limit = self._query_keys(qs)
        out = {}
        with self.app.pinned(h) as e:
            lo, hi = _range(e, lo, hi)
            _check_keys(e, keys)
            for kind, ch in keys:
                s = self.app.core.compute_stats(e, kind, ch, lo, hi, offset, max_limit,
                                                cancel=job.cancel)
                out["%s%d" % (kind, ch)] = {k: _clean(v) for k, v in s.items()}
        self._json(out)

    def _r_export_csv(self, job, qs, gen):
        b = self._body_json()
        h = _handle_arg(b.get("h"))
        hc = b.get("hc")
        hc = None if hc is None else _handle_arg(hc, "hc")
        lo, hi = _int(b.get("lo"), "lo"), _int(b.get("hi"), "hi")
        offset = b.get("offset", False)
        if not isinstance(offset, bool):
            raise _bad("offset must be boolean")
        max_limit = _num(b.get("maxLimit", c.DEFAULT_MAX_LIMIT), "maxLimit")
        cols = b.get("cols")
        if not isinstance(cols, str):
            raise _bad("cols required")
        if len(cols.split(",")) > c.MAX_CSV_KEYS:
            raise _bad("cols must have at most %d items" % c.MAX_CSV_KEYS)
        specs = []
        for tok in cols.split(","):
            m = re.match(r"^([pc]):([VIP])([1-4])$", tok)
            if not m:
                raise _bad("bad cols item %r" % tok[:20])
            if m[1] == "c" and hc is None:
                raise _bad("cols uses c: but hc is missing")
            specs.append((m[1], m[2], int(m[3])))
        with ExitStack() as st:
            primary = st.enter_context(self.app.pinned(h))
            compare = st.enter_context(self.app.pinned(hc)) if hc else None
            lo, hi = _range(primary, lo, hi)
            columns = []
            for src, kind, ch in specs:
                ent = primary if src == "p" else compare
                rec = {(f[0], f[1]) for f in ent["meta"]["fields"]}
                need = ("V", "I") if kind == "P" else (kind,)
                columns.append({
                    "entry": ent, "kind": kind, "ch": ch,
                    "label": c.CSV_LABEL_FMT.format(file="File1" if src == "p" else "File2",
                                                    ch=ch, word=c.CSV_KIND_WORD[kind]),
                    "missing": any((ch, k) not in rec for k in need)})
            path = self._grant(b.get("grant"), "save", "csv")
            chunks = self.app.core.iter_csv(primary, columns, lo, hi, offset, max_limit,
                                            datetime.datetime.now(), cancel=job.cancel)
            n = self.app.storage.atomic_write_iter(path, chunks, job.cancel)
        self._json({"ok": True, "bytes": n, "name": path.name})

    def _r_save(self, job, qs, gen):
        purpose = self.headers.get(c.HEADER_PURPOSE)
        if purpose not in c.UPLOAD_PURPOSES:
            raise _bad("%s must be one of %s" % (c.HEADER_PURPOSE, ", ".join(c.UPLOAD_PURPOSES)))
        n = self._length(c.MAX_UPLOAD_BYTES)
        path = self._grant(self.headers.get(c.HEADER_GRANT), "save", purpose)
        written = self.app.storage.atomic_write_iter(path, self._body_parts(n), job.cancel)
        self._json({"ok": True, "bytes": written, "name": path.name})

    def _r_import_markers(self, job, qs, gen):
        path = self._grant(self._body_json().get("grant"), "open", "markers")
        if path.stat().st_size > c.MAX_MARKERS_FILE_BYTES:
            raise _HttpError(_E.TOO_LARGE, "markers file too large")
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError
            raise _HttpError(_E.FORMAT, "markers file is not UTF-8 JSON")
        self._json({"ok": True, "data": data})

    def _r_prefs(self, job, qs, gen):
        app = self.app
        if self.command == "GET":
            try:
                p = json.loads(app.prefs_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                p = None
            return self._json(p if _prefs_ok(p) else c.PREFS_DEFAULT)
        p = self._body_json(c.MAX_PREFS_BYTES)
        if not _prefs_ok(p):
            raise _bad("bad prefs structure")
        app.storage.ensure_dir(app.prefs_path.parent)
        app.storage.atomic_write_bytes(app.prefs_path, _json_bytes(p))
        self._json({"ok": True})

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch


# ---------------------------------------------------------------- public API

class Handle:
    def __init__(self, app, httpd, port):
        self._app, self._httpd, self.port = app, httpd, port
        self.origin = "http://%s:%d" % (c.HOST, port)
        self.token = app.token

    def url(self, gen, lang):
        return "%s/viewer.html#t=%s&gen=%d&lang=%s" % (self.origin, self.token, gen, lang)

    def release_resources(self):
        return self._app.release_all()

    def self_check(self):
        try:
            conn = http.client.HTTPConnection(c.HOST, self.port, timeout=5)
            try:
                conn.request("GET", "/health")
                r = conn.getresponse()
                return r.status == 200 and json.loads(r.read()).get("ok") is True
            finally:
                conn.close()
        except (OSError, ValueError):
            return False

    def shutdown(self, timeout=c.CLOSING_CLEANUP_TIMEOUT_S):
        t = threading.Thread(target=self._httpd.shutdown, daemon=True)
        t.start()
        t.join(timeout)
        self._httpd.server_close()


def start_server(gate, *, cache_root=None, prefs_path=None, dev=False, core=None, storage=None):
    """core/storage default to analysis.core / analysis.storage (injectable for tests)."""
    if core is None:
        from . import core
    if storage is None:
        from . import storage
    app = _App(gate, core, storage, Path(cache_root or storage.default_cache_root()),
               Path(prefs_path or storage.default_prefs_path()), dev)
    try:
        storage.sweep_tmp(app.cache_root)
    except OSError:
        log.exception("sweep_tmp failed")
    httpd = ThreadingHTTPServer((c.HOST, 0), _Handler)
    httpd.daemon_threads = True
    httpd.block_on_close = False
    port = httpd.server_address[1]
    handler = type("Handler", (_Handler,), {"app": app, "port": port})
    httpd.RequestHandlerClass = handler
    threading.Thread(target=httpd.serve_forever, daemon=True, name="analysis-http").start()
    return Handle(app, httpd, port)
