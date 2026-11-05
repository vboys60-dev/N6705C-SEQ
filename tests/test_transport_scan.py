"""Discovery merges backends and contains backend hangs/crashes."""

import multiprocessing as mp
import os
import sys
import time
from types import SimpleNamespace

import pytest

from n6705c.core import transport


def _success(conn, candidates):
    conn.send(candidates)
    conn.close()


def _hang(conn, candidates):
    time.sleep(60)


def _crash(conn, candidates):
    os._exit(7)


def _no_answer(conn, candidates):
    conn.close()


def test_list_resources_merges_skips_and_closes(monkeypatch):
    opened, closed, registered = [], [], []

    class Manager:
        def __init__(self, candidate):
            opened.append(candidate)
            self.candidate = candidate
            if candidate == "missing":
                raise OSError("not installed")

        def list_resources(self):
            if self.candidate == "broken":
                raise OSError("discovery failed")
            return {"": ["ASRL1", "USB1", "USB1"], "@py": ["USB1", "TCPIP1"]}[self.candidate]

        def close(self):
            closed.append(self.candidate)

    monkeypatch.setitem(sys.modules, "pyvisa", SimpleNamespace(ResourceManager=Manager))
    monkeypatch.setattr(transport, "_candidates", lambda backend: ["", "missing", "broken", "@py"])
    monkeypatch.setattr(transport, "_register_libusb", lambda: registered.append(True))
    assert transport.list_resources() == ["ASRL1", "USB1", "TCPIP1"]
    assert opened == ["", "missing", "broken", "@py"]
    assert closed == ["", "broken", "@py"]
    assert registered == [True]


def test_scan_candidates_computed_in_parent(monkeypatch):
    calls = []

    def candidates(backend):
        assert not mp.current_process().daemon
        calls.append(backend)
        return ["", "@py"]

    monkeypatch.setattr(transport, "_candidates", candidates)
    assert transport.scan_resources(5, _target=_success) == [("", ""), ("@py", "")]
    assert calls == ["auto"]


@pytest.mark.parametrize("target,match", [(_hang, "timed out"), (_crash, "crashed"), (_no_answer, "crashed")])
def test_scan_contains_hang_and_crash(monkeypatch, target, match):
    monkeypatch.setattr(transport, "_candidates", lambda backend: ["@py"])
    before = {p.pid for p in mp.active_children()}
    start = time.monotonic()
    with pytest.raises(transport.TransportError, match=match):
        transport.scan_resources(1, _target=target)
    assert time.monotonic() - start < 4
    assert {p.pid for p in mp.active_children()} == before


def test_scan_poll_broken_pipe_is_crash(monkeypatch):
    pipe = mp.Pipe

    def broken_poll(timeout):
        raise BrokenPipeError("child pipe closed")

    def broken_pipe(*args, **kwargs):
        parent_conn, child_conn = pipe(*args, **kwargs)
        return SimpleNamespace(poll=broken_poll, recv=parent_conn.recv, close=parent_conn.close), child_conn

    monkeypatch.setattr(transport, "_candidates", lambda backend: ["@py"])
    monkeypatch.setattr(mp, "Pipe", broken_pipe)
    with pytest.raises(transport.TransportError, match="crashed"):
        transport.scan_resources(1, _target=_no_answer)


def test_scan_child_filters_serial_before_idn_and_closes(monkeypatch):
    sent, closed, queried = [], [], []
    monkeypatch.setattr(transport, "_list_on", lambda candidates: ["ASRL1", "asrl2", "USB1", "TCPIP1"])

    def idn(address, candidates):
        assert sent[0] == ["USB1", "TCPIP1"]
        queried.append((address, candidates))
        return "Keysight,N6705C,serial,fw" if address == "USB1" else ""

    monkeypatch.setattr(transport, "_idn_of", idn)
    transport._scan_child(SimpleNamespace(send=sent.append, close=lambda: closed.append(True)), ["@py"])
    assert queried == [("USB1", ["@py"]), ("TCPIP1", ["@py"])]
    assert sent == [["USB1", "TCPIP1"], ("USB1", "Keysight,N6705C,serial,fw"), ("TCPIP1", "")]
    assert closed == [True]


@pytest.mark.parametrize("failure", ["open", "encoding", "query", "empty"])
@pytest.mark.parametrize("answer", ["Keysight,N6705C,非ASCII,fw", ""])
def test_idn_explicit_backends_encoding_and_close(monkeypatch, failure, answer):
    opened, closed, commands = [], [], []

    class Instrument:
        @property
        def encoding(self):
            return self._encoding

        @encoding.setter
        def encoding(self, value):
            if failure == "encoding" and len(opened) == 1:
                raise OSError("encoding failed")
            self._encoding = value

    class FakeTransport:
        def __init__(self, resource, *, backend, timeout_ms):
            assert resource == "USB1" and timeout_ms == 2000
            assert backend != "auto"
            opened.append(backend)
            if failure == "open" and backend == "":
                raise OSError("open failed")
            self._inst = Instrument()
            self.backend = backend

        def query(self, command):
            assert self._inst.encoding == "latin-1"
            commands.append(command)
            if self.backend == "":
                if failure == "empty":
                    return ""
                raise OSError("query failed")
            if not answer:
                raise OSError("no reply")
            return answer

        def close(self):
            closed.append(self.backend)

    monkeypatch.setattr(transport, "VisaTransport", FakeTransport)
    assert transport._idn_of("USB1", ["", "@py"]) == answer
    assert opened == ["", "@py"]
    assert closed == (["@py"] if failure == "open" else ["", "@py"])
    assert commands and set(commands) == {"*IDN?"}


def _partial_hang(conn, candidates):
    conn.send(["USB1", "TCPIP1", "USB2"])
    conn.send(("TCPIP1", "Keysight,N6705C,serial,fw"))
    time.sleep(60)


def _partial_crash(conn, candidates):
    conn.send(["USB1", "TCPIP1", "USB2"])
    conn.send(("TCPIP1", "Keysight,N6705C,serial,fw"))
    os._exit(7)


@pytest.mark.parametrize("target", [_partial_hang, _partial_crash])
def test_scan_returns_partial_after_list(monkeypatch, target):
    monkeypatch.setattr(transport, "_candidates", lambda backend: ["@py"])
    before = {p.pid for p in mp.active_children()}
    start = time.monotonic()
    assert transport.scan_resources(1, _target=target) == [
        ("USB1", ""), ("TCPIP1", "Keysight,N6705C,serial,fw"), ("USB2", ""),
    ]
    assert time.monotonic() - start < 4
    assert {p.pid for p in mp.active_children()} == before
