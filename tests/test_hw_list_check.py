"""Pure measurement logic and simulator smoke checks for the hardware CLI."""
import csv
import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location(
    "hw_list_check", Path(__file__).resolve().parents[1] / "scripts" / "hw_list_check.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)


def test_sine_fit_with_phase_offset_and_noise():
    t = np.arange(5000) / 10000
    rng = np.random.default_rng(17)
    v = 2 + 0.7 * np.sin(2 * np.pi * 30 * t + 0.6) + rng.normal(0, 0.005, t.size)
    assert abs(hw.fit_sine_amplitude(t, v, 30) - 0.7) / 0.7 < 0.01


def test_3db_linear_interpolation_and_above_highest():
    expected = 100 + (300 - 100) * (1 - hw.DB3_RATIO) / (1 - 0.5)
    assert hw.interpolate_3db([10, 100, 300], [1, 1, 0.5]) == pytest.approx(expected)
    assert hw.interpolate_3db([10, 100, 3000], [1, 0.9, 0.8]) is None


def test_known_pulse_edge_offset():
    t = np.arange(1000) * 1e-5
    first = ((t >= 0.002) & (t < 0.003)).astype(float)
    second = ((t >= 0.0024) & (t < 0.0034)).astype(float)
    a = hw.rising_edge_times(t, first)
    b = hw.rising_edge_times(t, second)
    assert len(a) == len(b) == 1
    assert b[0] - a[0] == pytest.approx(0.0004)


def test_seam_subtracts_intended_reset_and_detects_glitch():
    dwell, n = 0.001, 10
    t = np.arange(30) * dwell
    ideal = (np.arange(30) % n + 1) / n
    assert hw.seam_jumps(t, ideal, n * dwell, dwell) == pytest.approx([0, 0])
    glitch = ideal.copy()
    glitch[10] += 0.2
    assert hw.seam_jumps(t, glitch, n * dwell, dwell, radius=dwell)[0] == pytest.approx(0.2)


@pytest.mark.parametrize("check", hw.CHECKS)
def test_demo_smoke(check, tmp_path, monkeypatch, capsys):
    group = hw.InstrumentGroup.demo(frames=1, slots=hw.execute.DEMO_SLOTS)
    monkeypatch.setattr(hw.InstrumentGroup, "demo", lambda **kwargs: group)
    # Inspect after CLI cleanup while retaining the real simulator session.
    monkeypatch.setattr(group, "close", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("demo must not prompt"))
    try:
        assert hw.main([check, "--demo", "--out", str(tmp_path)]) == 0
        assert (tmp_path / f"{check}.csv").is_file()
        assert (tmp_path / f"{check}.csv").stat().st_size > 0
        assert not any(group.output_state([1, 2, 3, 4]).values())
        output = capsys.readouterr().out
        if check == "align":
            assert "無法驗收：缺少上升緣" in output
            assert "PASS" not in output
        if check == "transfer":
            commands = [h.text for h in group.instruments[0].session.history if h.direction == "->"]
            assert not any(c.startswith("INIT:TRAN") for c in commands)
            assert not any(c.startswith("OUTP ON") for c in commands)
    finally:
        for inst in group.instruments:
            inst.close()


def test_failure_turns_outputs_off(tmp_path, monkeypatch):
    group = hw.InstrumentGroup.demo(frames=1, slots=hw.execute.DEMO_SLOTS)
    monkeypatch.setattr(hw.InstrumentGroup, "demo", lambda **kwargs: group)
    monkeypatch.setattr(group, "close", lambda: None)

    def fail(group, out):
        group.set_output([1, 2], True)
        raise RuntimeError("injected failure")

    monkeypatch.setitem(hw.CHECKS, "seam", fail)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            hw.main(["seam", "--demo", "--out", str(tmp_path)])
        assert not any(group.output_state([1, 2, 3, 4]).values())
    finally:
        for inst in group.instruments:
            inst.close()


@pytest.mark.parametrize("extra_drift, status", [(0.25, "PASS"), (2.0, "FAIL")])
@pytest.mark.parametrize("trigger_shift", [0.0, 0.017])
def test_align_metrics_relative_drift(extra_drift, status, trigger_shift):
    dwell, period = 0.001, 0.01
    t1 = {1: 0.005, 2: 0.0055, 3: 0.005, 4: 0.005}
    t256 = {ch: t + 255 * period + trigger_shift for ch, t in t1.items()}
    t256[3] += extra_drift * dwell
    result = hw.align_metrics(t1, t256, period, dwell)
    assert result["spread1"] == pytest.approx(0.5)
    assert result["spread256"] == pytest.approx(max(0.5, extra_drift))
    assert result["drift_dwell"][1] == pytest.approx(trigger_shift / dwell)
    assert result["rel_drift_dwell"] == pytest.approx({1: 0, 2: 0, 3: extra_drift, 4: 0})
    assert result["status"] == status


@pytest.mark.parametrize("missing_round", [1, 256])
@pytest.mark.parametrize("channel", [1, 3])
def test_align_metrics_missing_edge(missing_round, channel):
    t1 = dict.fromkeys(range(1, 5), 0.005)
    t256 = dict.fromkeys(range(1, 5), 2.555)
    (t1 if missing_round == 1 else t256)[channel] = math.nan
    result = hw.align_metrics(t1, t256, 0.01, 0.001)
    assert result["status"] == "無法驗收：缺少上升緣"


def test_align_demo_short_records(tmp_path, monkeypatch):
    group = hw.InstrumentGroup.demo(frames=1, slots=hw.execute.DEMO_SLOTS)
    original = hw.execute._arm_capture
    period = round(0.01 / hw.DWELL) * hw.DWELL
    tint = hw.execute.CAPTURE_STEP
    points = math.floor((period - 0.002) / tint)
    offsets = [0, round(255 * period / tint)]
    original_run = hw.execute.run_functions
    run_count = 0

    def inspect_waveform(group, functions, **kwargs):
        nonlocal run_count
        run_count += 1
        assert [fn.channel for fn in functions] == [1, 2, 3, 4]
        expected = np.zeros(round(0.01 / hw.DWELL))
        rise, width = round(0.005 / hw.DWELL), round(0.001 / hw.DWELL)
        expected[rise:rise + width] = 1
        for fn in functions:
            assert fn.spec.count == 256
            assert fn.spec.waveform.dwell == hw.DWELL
            assert fn.spec.waveform.levels == expected.tolist()
        assert kwargs == {"wait": True, "keep_on": False, "capture": {}}
        return original_run(group, functions, **kwargs)

    monkeypatch.setattr(hw.execute, "run_functions", inspect_waveform)
    try:
        hw.align(group, tmp_path)
        assert run_count == 2
        assert hw.execute._arm_capture is original
        commands = [h.text for h in group.instruments[0].session.history if h.direction == "->"]
        for prefix, values in (
            ("SENS:SWE:OFFS:POIN", offsets),
            ("SENS:SWE:POIN", [points, points]),
            ("SENS:SWE:TINT", [tint, tint]),
        ):
            actual = [float(c.split()[1].split(",")[0]) for c in commands if c.startswith(prefix + " ")]
            assert actual == values
        for offset in offsets:
            sequence = ["FORM REAL", "FORM:BORD SWAP"] + [
                f"{command},(@1,2,3,4)" for command in (
                    "SENS:FUNC:VOLT ON", f"SENS:SWE:POIN {points}",
                    f"SENS:SWE:TINT {tint:.6E}", f"SENS:SWE:OFFS:POIN {offset}",
                    "TRIG:ACQ:SOUR BUS",
                )] + ["INIT:IMM:ACQ (@1,2,3,4)"]
            # Error queries may occur between writes; inspect only configuration writes.
            configuration = [c for c in commands if c.startswith(("FORM ", "FORM:", "SENS:", "TRIG:ACQ:", "INIT:IMM:ACQ"))]
            start = offsets.index(offset) * len(sequence)
            assert configuration[start:start + len(sequence)] == sequence
        for cycle, offset in zip((1, 256), offsets):
            with (tmp_path / f"align_round{cycle}.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            assert len(rows) == 4 * points
            for ch in range(1, 5):
                times = [float(r["time_s"]) for r in rows if int(r["channel"]) == ch]
                assert times == pytest.approx((offset + np.arange(points)) * tint)
        with (tmp_path / "align.csv").open() as stream:
            reader = csv.DictReader(stream)
            assert reader.fieldnames == ["channel", "t1_s", "t256_s", "drift_dwell", "rel_drift_dwell"]
            assert all(math.isnan(float(row["t1_s"])) for row in reader)
        assert not any(group.output_state([1, 2, 3, 4]).values())
    finally:
        group.close()


def test_align_failure_restores_arm_and_turns_outputs_off(tmp_path, monkeypatch):
    group = hw.InstrumentGroup.demo(frames=1, slots=hw.execute.DEMO_SLOTS)
    original_arm = hw.execute._arm_capture
    original_start = hw.execute.start_together
    calls = 0

    def fail_second_start(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert hw.execute._arm_capture is not original_arm
        if calls == 2:
            assert all(group.output_state([1, 2, 3, 4]).values())
            raise RuntimeError("injected align failure")
        return original_start(*args, **kwargs)

    monkeypatch.setattr(hw.execute, "start_together", fail_second_start)
    try:
        with pytest.raises(RuntimeError, match="injected align failure"):
            hw.align(group, tmp_path)
        assert calls == 2
        assert hw.execute._arm_capture is original_arm
        assert not any(group.output_state([1, 2, 3, 4]).values())
    finally:
        group.close()


@pytest.mark.parametrize("first, last, status", [
    ([0, 1, 0, 0], [0, 1, 0, 0], "PASS"),
    ([0, 1.1, 0, 0], [0, 0, 0, 0], "FAIL"),
    ([0, 0, 0, 0], [0, 1.1, 0, 0], "FAIL"),
    ([0, -0.75, 0, 0], [0, 0.75, 0, 0], "FAIL"),
])
def test_align_metrics_each_acceptance_limit(first, last, status):
    t1 = dict(enumerate(first, 1))
    t256 = {ch: 255 + value for ch, value in enumerate(last, 1)}
    assert hw.align_metrics(t1, t256, 1, 1)["status"] == status


def test_align_selects_nearest_edge_within_window(tmp_path, monkeypatch):
    period = round(0.01 / hw.DWELL) * hw.DWELL
    offsets = []

    def synthetic_capture(group, functions, offset_points=0):
        offsets.append(offset_points)
        expected = (255 * period if offset_points else 0) + 0.005
        # Two in-window edges and one outside; interpolation gives the nearest.
        crossings = np.array([expected - period / 3, expected + 0.0001, expected + 0.001])
        times = np.column_stack((crossings - 1e-6, crossings + 1e-6,
                                 crossings + 2e-6)).ravel()
        volts = np.tile([0., 1., 0.], 3)
        return {fn.channel: (times, volts, hw.execute.CAPTURE_STEP) for fn in functions}

    monkeypatch.setattr(hw, "capture_run", synthetic_capture)
    hw.align(None, tmp_path)
    assert offsets == [0, round(255 * period / hw.execute.CAPTURE_STEP)]
    with (tmp_path / "align.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    for row in rows:
        assert float(row["t1_s"]) == pytest.approx(0.0051)
        assert float(row["t256_s"]) == pytest.approx(255 * period + 0.0051)
        assert float(row["rel_drift_dwell"]) == pytest.approx(0)
