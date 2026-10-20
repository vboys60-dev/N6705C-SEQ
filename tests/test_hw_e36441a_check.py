"""Pure sample analysis for the E36441A check, plus a fake-clock run of all checks."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "hw_e36441a_check", Path(__file__).resolve().parents[1] / "scripts" / "hw_e36441a_check.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)

LOW, HIGH, RISE, HOLD, DT = 2.0, 5.0, 0.2, 0.3, 0.01


def wave(t, slew=True, dwell=RISE + HOLD):
    """Ideal LIST 5,2 with symmetric slew (or a hard step when slew=False)."""
    r = RISE if slew else 1e-9
    if t < 0:
        return LOW
    if t < dwell:
        return LOW + (HIGH - LOW) * min(t / r, 1)
    return HIGH - (HIGH - LOW) * min((t - dwell) / r, 1)


def trapezoid(slew=True, dwell=RISE + HOLD, end=1.5):
    return [(i * DT, wave(i * DT, slew, dwell)) for i in range(int(end / DT))]


def test_rise_time_ramp_vs_step():
    assert hw.rise_time_10_90(trapezoid(), LOW, HIGH) == pytest.approx(0.8 * RISE, abs=1e-6)
    assert hw.rise_time_10_90(trapezoid(slew=False), LOW, HIGH) < DT
    assert hw.rise_time_10_90([(0, LOW), (1, LOW)], LOW, HIGH) is None


def test_flat_top_and_still():
    s = trapezoid()
    # 90% level: dwell - 0.8*RISE above it
    assert hw.flat_top_duration(s, LOW + 0.9 * (HIGH - LOW)) == pytest.approx(RISE + HOLD - 0.8 * RISE, abs=DT)
    assert hw.flat_top_duration([(0, 0), (1, 0)], 1) is None
    assert hw.still_at([(0, 2.01), (1, 1.98)], 2.0, 0.05)
    assert not hw.still_at([(0, 2.0), (1, 3.0)], 2.0, 0.05)


@pytest.mark.parametrize("dwell, kind", [(RISE + HOLD, "switch"), (RISE + RISE + HOLD, "settle")])
def test_classify_dwell(dwell, kind):
    s = trapezoid(dwell=dwell)
    tr = hw.rise_time_10_90(s, LOW, HIGH)
    dur = hw.flat_top_duration(s, LOW + 0.9 * (HIGH - LOW))
    assert hw.classify_dwell(dur, tr, RISE, HOLD, DT + hw.ABS_TOL)[0] == kind


class FakeSession:
    """Virtual-clock instrument: each MEAS:VOLT? advances 10 ms."""
    def __init__(self, mode):
        self.now, self.mode, self.t_init, self.t_trg = 0.0, mode, None, None

    def write(self, cmd):
        if cmd.startswith("INIT "):
            self.t_init = self.now
            if self.mode == "early":
                self.t_trg = self.now
        if cmd == "*TRG" and self.t_init is not None:
            self.t_trg = self.now

    write_checked = write

    def query_float(self, cmd):
        self.now += DT
        if self.t_trg is None:
            return LOW
        t = self.now - self.t_trg
        return wave(t, slew=self.mode != "step")

    def read_errors(self):
        return []


@pytest.mark.parametrize("mode, expect", [
    ("ideal", ("PASS", "PASS", "PASS")), ("early", ("FAIL", "PASS", "PASS")),
    ("step", ("PASS", "FAIL", "INCONCLUSIVE"))])
def test_checks_with_fake_clock(mode, expect, monkeypatch):
    s = FakeSession(mode)
    monkeypatch.setattr(hw, "time", SimpleNamespace(perf_counter=lambda: s.now, sleep=lambda x: None))
    args = SimpleNamespace(v_low=LOW, v_high=HIGH, rise=RISE, hold=HOLD, current=0.5)
    assert tuple(hw.CHECKS[n](s, 1, args, {}) for n in (1, 2, 3)) == expect
