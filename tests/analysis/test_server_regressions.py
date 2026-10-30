"""HTTP wire regressions over socketpair; no localhost bind is required."""
import http.client
import json
import socket
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from n6705c_seq.analysis import contracts as c, server, storage
from n6705c_seq.analysis.gate import Gate
from test_http import Env, StubCore, split


class WireEnv(Env):
    def __init__(self, tmp_path):
        self.tmp, self.gen, self.port = tmp_path, None, 12345
        self.gate, self.core = Gate(lambda e: None), StubCore()
        self.app = server._App(self.gate, self.core, storage, tmp_path / 'cache',
                               tmp_path / 'new' / 'nested' / 'prefs.json', False)
        self.handler = type('WireHandler', (server._Handler,),
                            {'app': self.app, 'port': self.port})
        self.srv = type('WireHandle', (), {'token': self.app.token})()
        self.workers = []
        self.gate.set_release_resources(self.app.release_all)
        self.enter()

    def _connection(self):
        client, peer = socket.socketpair()
        client.settimeout(5)
        # A small buffer makes the non-reading-client regression deterministic.
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

        def serve():
            try:
                self.handler(peer, ('127.0.0.1', 0), None)
            finally:
                peer.close()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        self.workers.append(worker)
        conn = http.client.HTTPConnection(c.HOST, self.port, timeout=5)
        conn.sock = client
        return conn


@pytest.fixture
def wire(tmp_path):
    e = WireEnv(tmp_path)
    yield e
    for worker in e.workers:
        worker.join(2)
        assert not worker.is_alive()
    assert e.gate.job_count() == 0
    assert e.app.release_all()


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def pending(e, route, length, *, req=None, extra=None):
    conn = e._connection()
    conn.putrequest('POST', route)
    headers = {c.HEADER_TOKEN: e.app.token, c.HEADER_GEN: str(e.gen),
               'Content-Length': str(length), **(extra or {})}
    if req:
        headers[c.HEADER_REQ] = req
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    return conn


def test_old_release_body_does_not_unhold_new_generation(wire, monkeypatch):
    h = wire.open_file()['handle']
    old_gen = wire.gen
    checked = threading.Event()
    original = server._Handler._need_session

    def session(self, gen):
        original(self, gen)
        if self.path == '/release' and gen == old_gen:
            checked.set()

    monkeypatch.setattr(server._Handler, '_need_session', session)
    body = json.dumps({'h': h}).encode()
    conn = pending(wire, '/release', len(body))
    try:
        assert checked.wait(2)
        assert wire.gate.leave(old_gen)
        wait_for(lambda: wire.gate.state()[0] == c.State.IDLE)
        wire.enter()
        assert wire.gen > old_gen
        assert wire.open_file()['handle'] == h
        conn.send(body)
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        assert wire.core.held[h] == 1
        assert (wire.gen, h) in wire.app.held
        assert wire.j('POST', '/release', {'h': h}) == (200, {'ok': True})
        assert wire.core.held[h] == 0
    finally:
        conn.close()


@pytest.mark.parametrize('route', ['/prefs', '/save'])
def test_cancel_missing_body_runs_job_end(wire, route):
    out = wire.tmp / 'shot.png'
    out.write_bytes(b'original')
    extra = {c.HEADER_PURPOSE: 'png',
             c.HEADER_GRANT: wire.grant('save', 'png', out)} if route == '/save' else {}
    conn = pending(wire, route, 100, req='stalled', extra=extra)
    try:
        wait_for(lambda: wire.gate.job_count() == 1)
        assert wire.j('POST', '/cancel', {'req': 'stalled'}) == (200, {'ok': True})
        wait_for(lambda: wire.gate.job_count() == 0)
        assert conn.sock.recv(1) == b''
        assert out.read_bytes() == b'original'
        assert not list(wire.tmp.glob('*.part'))
    finally:
        conn.close()


def test_missing_body_deadline_runs_job_end(wire, monkeypatch):
    monkeypatch.setattr(server, '_IO_DEADLINE_S', 0.1)
    conn = pending(wire, '/prefs', 100)
    try:
        wait_for(lambda: wire.gate.job_count() == 1)
        wait_for(lambda: wire.gate.job_count() == 0)
        assert conn.sock.recv(1) == b''
    finally:
        conn.close()


def test_cancel_nonreading_response_runs_job_end(wire, monkeypatch):
    writing = threading.Event()

    def big(self, job, qs, gen):
        writing.set()
        self._send(200, 'application/octet-stream', b'x' * (4 << 20))

    monkeypatch.setattr(server._Handler, '_r_samples', big)
    conn = wire._connection()
    try:
        conn.request('GET', '/samples', headers={c.HEADER_TOKEN: wire.app.token,
                     c.HEADER_GEN: str(wire.gen), c.HEADER_REQ: 'blockedwrite'})
        assert writing.wait(2)
        assert wire.j('POST', '/cancel', {'req': 'blockedwrite'}) == (200, {'ok': True})
        wait_for(lambda: wire.gate.job_count() == 0)
    finally:
        conn.close()


def test_save_cancel_at_last_read_preserves_existing_file(wire, monkeypatch):
    out = wire.tmp / 'shot.png'
    out.write_bytes(b'original')
    original = server._Handler._body_parts

    def cancel_last(self, n):
        for part in original(self, n):
            # Cancel after the last network read but before publication.
            self._job.cancel.set()
            yield part

    monkeypatch.setattr(server._Handler, '_body_parts', cancel_last)
    status, body = wire.j('POST', '/save', raw=b'new contents', headers={
        c.HEADER_PURPOSE: 'png', c.HEADER_GRANT: wire.grant('save', 'png', out)})
    assert (status, body['code']) == (409, 'cancelled')
    assert out.read_bytes() == b'original'
    assert not list(wire.tmp.glob('*.part'))


def test_cancel_during_final_read_is_checked_before_yield(monkeypatch):
    handler = server._Handler.__new__(server._Handler)
    cancel = threading.Event()
    handler._job = SimpleNamespace(cancel=cancel)
    handler.connection = object()
    handler._consumed = False

    def read(n):
        cancel.set()
        return b'x' * n

    handler.rfile = SimpleNamespace(read=read)
    monkeypatch.setattr(server.select, 'select', lambda *a: ([handler.connection], [], []))
    with pytest.raises(server._IOStopped):
        next(handler._body_parts(3))
    assert not handler._consumed


def test_save_waiting_for_target_lock_is_cancellable(wire):
    out = wire.tmp / 'shot.png'
    out.write_bytes(b'original')
    with storage.path_lock(out):
        conn = pending(wire, '/save', 3, req='locked', extra={
            c.HEADER_PURPOSE: 'png', c.HEADER_GRANT: wire.grant('save', 'png', out)})
        try:
            conn.send(b'new')
            wait_for(lambda: wire.gate.job_count() == 1)
            assert wire.j('POST', '/cancel', {'req': 'locked'}) == (200, {'ok': True})
            response = conn.getresponse()
            assert response.status == 409
            response.read()
            wait_for(lambda: wire.gate.job_count() == 0)
        finally:
            conn.close()
    assert out.read_bytes() == b'original'
    assert not list(wire.tmp.glob('*.part'))


def test_save_streams_multiple_chunks(wire, monkeypatch):
    out = wire.tmp / 'shot.png'
    real = storage.atomic_write_iter
    seen = []

    def write(path, parts, cancel=None):
        assert not isinstance(parts, (bytes, list))
        assert cancel is not None

        def counted():
            for part in parts:
                seen.append(len(part))
                yield part
        return real(path, counted(), cancel)

    monkeypatch.setattr(storage, 'atomic_write_iter', write)
    payload = b'x' * (server._IO_CHUNK_BYTES * 3 + 7)
    status, body = wire.j('POST', '/save', raw=payload, headers={
        c.HEADER_PURPOSE: 'png', c.HEADER_GRANT: wire.grant('save', 'png', out)})
    assert status == 200 and body['bytes'] == len(payload)
    assert out.read_bytes() == payload
    assert len(seen) >= 4 and max(seen) <= server._IO_CHUNK_BYTES


def test_prefs_first_use_creates_parent_and_roundtrips(wire):
    assert not wire.app.prefs_path.parent.exists()
    good = {'version': 1, 'theme': 'light', 'fold': {'x': True}}
    assert wire.j('POST', '/prefs', good) == (200, {'ok': True})
    assert wire.j('GET', '/prefs') == (200, good)


def test_oversized_open_releases_before_adopt(wire, monkeypatch):
    real = wire.core.open_handle

    def oversized(*args, **kwargs):
        meta, blob, entry = real(*args, **kwargs)
        # The blob alone fits; metadata and prefix push it over the limit.
        return meta, b'x' * c.MAX_RESPONSE_BYTES, entry

    monkeypatch.setattr(c, 'MAX_RESPONSE_BYTES', 1024)
    monkeypatch.setattr(wire.core, 'open_handle', oversized)
    path = wire.tmp / 'a.dlog'
    path.write_bytes(b'x')
    status, body = wire.j('POST', '/open', {'grant': wire.grant('open', 'dlog', path)})
    assert (status, body['code']) == (413, 'too_large')
    assert not wire.app.held and sum(wire.core.held.values()) == 0


def strict_load(data):
    def reject(value):
        raise AssertionError('nonstandard JSON token: ' + value)
    return json.loads(data, parse_constant=reject)


def test_frame_binary_nonfinite_metadata_is_null():
    meta, payload = split(server.frame_binary(
        {'tint': np.float64('nan'), 'nested': [float('inf'), {'v': np.float32('-inf')}]}, b'raw'))
    assert meta == {'tint': None, 'nested': [None, {'v': None}]} and payload == b'raw'
    framed = server.frame_binary({'tint': float('nan')}, b'')
    assert strict_load(framed[4:]) == {'tint': None}


def test_open_and_import_json_nonfinite_values_are_null(wire, monkeypatch):
    real = wire.core.open_handle

    def nonfinite(*args, **kwargs):
        meta, blob, entry = real(*args, **kwargs)
        meta.update(tint=float('inf'), declaredTime=np.float64('nan'))
        return meta, blob, entry

    monkeypatch.setattr(wire.core, 'open_handle', nonfinite)
    meta = wire.open_file()
    assert meta['tint'] is None and meta['declaredTime'] is None
    path = wire.tmp / 'markers.json'
    path.write_text('{"x": [NaN, Infinity, -Infinity]}')
    status, _, data = wire.req('POST', '/import/markers', {
        'grant': wire.grant('open', 'markers', path)})
    assert status == 200
    assert strict_load(data) == {'ok': True, 'data': {'x': [None, None, None]}}
    assert wire.j('GET', '/stats?h=%s&keys=V1&lo=0&hi=1' % meta['handle'])[1]['V1']['max'] is None


def test_csv_24_columns_allowed_25_rejected(wire, default_dlog):
    from n6705c_seq.analysis import core
    # Include the real core iterator's limit, not just server validation.
    wire.app.core = core
    path = default_dlog
    status, _, response = wire.req('POST', '/open', {'grant': wire.grant('open', 'dlog', path)})
    assert status == 200
    h = split(response)[0]['handle']
    out = wire.tmp / 'compare.csv'
    cols = ','.join('%s:%s%d' % (src, kind, ch) for src in 'pc' for ch in range(1, 5) for kind in 'VIP')
    base = {'h': h, 'hc': h, 'lo': 0, 'hi': 1, 'cols': cols}
    assert wire.j('POST', '/export/csv', {**base, 'grant': wire.grant('save', 'csv', out)})[0] == 200
    assert len(out.read_text().splitlines()[3].split(',')) == 25  # time + 24 keys
    status, body = wire.j('POST', '/export/csv', {**base, 'cols': cols + ',p:V1',
                         'grant': wire.grant('save', 'csv', out)})
    assert (status, body['code']) == (400, 'bad_request')
    assert wire.j('GET', '/stats?h=%s&keys=%s&lo=0&hi=1' % (h, ','.join(['V1'] * 13)))[0] == 400
