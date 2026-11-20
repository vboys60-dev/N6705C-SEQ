import threading

import pytest

from n6705c.core.group import InstrumentGroup
from n6705c.core.instrument import N6705C
from n6705c.core.fake import FakeN6705C
from n6705c_seq.plan import atomic_text


class Closing(FakeN6705C):
    closed = 0

    def close(self):
        self.closed += 1
        super().close()


class BadProbe(Closing):
    def query(self, cmd):
        raise OSError("probe failed")


def test_probe_failure_closes_transport():
    t = BadProbe()
    with pytest.raises(OSError):
        N6705C.from_transport(t)
    assert t.closed == 1


def test_group_failure_closes_earlier_frames():
    good, bad = Closing(), BadProbe()
    with pytest.raises(OSError):
        InstrumentGroup.from_transports([good, bad])
    assert good.closed == 1 and bad.closed == 1


def test_atomic_text_keeps_existing_tmp_and_cleans_up(tmp_path):
    target = tmp_path / "a.csv"
    other = tmp_path / "a.csv.tmp"
    other.write_text("keep")
    with atomic_text(target) as h:
        h.write("x")
    assert target.read_text() == "x" and other.read_text() == "keep"
    with pytest.raises(RuntimeError):
        with atomic_text(target) as h:
            h.write("y")
            raise RuntimeError
    assert target.read_text() == "x"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.csv", "a.csv.tmp"]


def test_atomic_text_overlapping_saves_do_not_share_tmp(tmp_path):
    target = tmp_path / "a.csv"
    inner_done = threading.Event()
    with atomic_text(target) as outer:
        outer.write("outer")
        with atomic_text(target) as inner:
            inner.write("inner")
        inner_done.set()
    assert target.read_text() == "outer"
