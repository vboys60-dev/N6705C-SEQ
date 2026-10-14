"""Human-operated hardware checks; demo validates plumbing, not metrology.

Waveforms are directly constructed ConstantDwellArb objects. Capture time zero
is the digitizer trigger (sent just after INIT:TRAN); seam residuals assume it
matches ARB time zero, so trigger latency can also appear in the residual.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import math
from pathlib import Path
import sys
import time

# Allow the documented command from an uninstalled source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from n6705c.core.arb import ArbSpec, ConstantDwellArb, GroupArbRunner
from n6705c.core.arb.models import CDWELL_DWELL_MIN, CDWELL_MAX_POINTS
from n6705c.core.function import ChannelFunction
from n6705c.core.group import InstrumentGroup
from n6705c_seq import execute

DWELL = CDWELL_DWELL_MIN
DB3_RATIO = 1 / math.sqrt(2)


def fit_sine_amplitude(times, volts, frequency):
    """Least squares a*sin(wt)+b*cos(wt)+c, including arbitrary phase/DC."""
    t, v = np.asarray(times, dtype=float), np.asarray(volts, dtype=float)
    if t.size < 3 or t.shape != v.shape or frequency <= 0:
        raise ValueError("sine fit needs matching samples and positive frequency")
    design = np.column_stack((np.sin(2 * np.pi * frequency * t),
                              np.cos(2 * np.pi * frequency * t), np.ones(t.size)))
    coeff, _, rank, _ = np.linalg.lstsq(design, v, rcond=None)
    if rank < 3:
        raise ValueError("sine fit has insufficient independent samples")
    return float(np.hypot(coeff[0], coeff[1]))


def interpolate_3db(frequencies, ratios, threshold=DB3_RATIO):
    """First downward crossing, linear in Hz; None means above highest Hz."""
    f, r = np.asarray(frequencies, dtype=float), np.asarray(ratios, dtype=float)
    if f.size == 0 or f.shape != r.shape or np.any(np.diff(f) <= 0):
        raise ValueError("frequencies must be increasing with matching ratios")
    if not np.all(np.isfinite(r)):
        raise ValueError("ratios must be finite")
    for i, ratio in enumerate(r):
        if ratio <= threshold:
            if i == 0:
                return float(f[0])
            return float(f[i - 1] + (f[i] - f[i - 1]) *
                         (threshold - r[i - 1]) / (ratio - r[i - 1]))
    return None


def rising_edge_times(times, volts, threshold=0.5):
    """Interpolated low-to-high threshold crossings; no invented initial edge."""
    t, v = np.asarray(times, dtype=float), np.asarray(volts, dtype=float)
    indices = np.flatnonzero((v[:-1] < threshold) & (v[1:] >= threshold))
    return t[indices] + (t[indices + 1] - t[indices]) * (
        threshold - v[indices]) / (v[indices + 1] - v[indices])


def seam_jumps(times, volts, period, dwell, count=3, radius=None):
    """Max |measured adjacent jump - ideal CDW jump| near each repeat seam.

    The intended 1 V -> first ramp point reset is subtracted, not called a
    glitch. NaN indicates missing samples around that boundary.
    """
    t, v = np.asarray(times, dtype=float), np.asarray(volts, dtype=float)
    n = round(period / dwell)
    indices = np.floor(t / dwell + 1e-9).astype(int) % n
    ideal = (indices + 1) / n
    residual = np.abs(np.diff(v) - np.diff(ideal))
    radius = radius if radius is not None else max(5 * dwell, 2 * float(np.median(np.diff(t))))
    result = []
    for k in range(1, count):
        mask = (t[:-1] <= k * period + radius) & (t[1:] >= k * period - radius)
        result.append(float(np.max(residual[mask])) if np.any(mask) else math.nan)
    return result


def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def function(channel, levels, count=1):
    return ChannelFunction(channel=channel, voltage=levels[0], output_on=True,
                           arb_enabled=True, spec=ArbSpec(
                               channel=channel, waveform=ConstantDwellArb(levels=list(levels), dwell=DWELL),
                               count=count, terminate_last=True))


def capture_run(group, functions, offset_points=0):
    capture = {}
    execute.run_functions(group, functions, wait=True, keep_on=False, capture=capture)
    if "error" in capture:
        raise RuntimeError(capture["error"])
    result = {}
    for fn in functions:
        tint, volts = capture[fn.channel]
        if len(volts) < 3 or not np.all(np.isfinite(volts)):
            raise RuntimeError(f"CH{fn.channel}: invalid capture")
        result[fn.channel] = ((offset_points + np.arange(len(volts))) * tint, np.asarray(volts), tint)
    return result


def transfer(group, out):
    fn = function(1, np.linspace(0, 1, CDWELL_MAX_POINTS).tolist())
    runner = GroupArbRunner.from_plan(group, [fn])
    # Use the core's actual ASCII formatter, excluding dwell/other commands and newline.
    command = fn.spec.waveform.scpi_commands("VOLT", "(@1)")[0]
    start = time.perf_counter()
    runner.load()  # no output enable, INIT or trigger
    elapsed = time.perf_counter() - start
    length = len(command.encode("ascii"))
    print(f"transfer: {elapsed:.6f} s, ASCII command length={length} bytes "
          f"(core formatter, CDW data command only, no newline); {'PASS' if elapsed <= 10 else 'FAIL'} (<= 10 s)")
    write_csv(out / "transfer.csv", ["seconds", "ascii_bytes", "pass"], [(elapsed, length, elapsed <= 10)])


def seam(group, out):
    n = round(0.01 / DWELL)
    period = n * DWELL
    times, volts, tint = capture_run(group, [function(1, np.arange(1, n + 1) / n, 3)])[1]
    write_csv(out / "seam.csv", ["time_s", "voltage_V"], zip(times, volts))
    print(f"seam: period={period:.9g} s, dwell={DWELL:.9g} s, sample interval={tint:.9g} s")
    print("seam: residual assumes capture/ARB time zero coincide; includes trigger latency")
    for k, jump in enumerate(seam_jumps(times, volts, period, DWELL), 1):
        print(f"seam: boundary {k}->{k + 1}, maximum excess jump={jump:.9g} V")


def align_metrics(t1, t256, period, dwell):
    """Channel spreads and CH1-relative drift in dwell units; missing edges abstain."""
    channels = list(t1)
    first = np.asarray([t1[ch] for ch in channels], dtype=float)
    last = np.asarray([t256[ch] for ch in channels], dtype=float)
    drift = {ch: (t256[ch] - t1[ch] - 255 * period) / dwell for ch in channels}
    relative = {ch: value - drift[1] for ch, value in drift.items()}
    spread1 = float(np.ptp(first) / dwell)
    spread256 = float(np.ptp(last) / dwell)
    max_relative = max(abs(value) for value in relative.values())
    if not np.all(np.isfinite(np.concatenate((first, last)))):
        status = "無法驗收：缺少上升緣"
    else:
        status = "PASS" if max(spread1, spread256, max_relative) <= 1 else "FAIL"
    return {"spread1": spread1, "spread256": spread256,
            "drift_dwell": drift, "rel_drift_dwell": relative, "status": status}


@contextmanager
def align_capture(offset_points, points, tint):
    """Temporarily configure the short align record through the existing run path."""
    original = execute._arm_capture

    def arm(group, functions, window):
        for instrument, channels in execute._frames(group, functions):
            chan = execute.format_channel_list(channels)
            session = instrument.session
            session.write_checked("FORM REAL")
            session.write_checked("FORM:BORD SWAP")
            for command in (
                "SENS:FUNC:VOLT ON",
                f"SENS:SWE:POIN {points}",
                f"SENS:SWE:TINT {tint:.6E}",
                f"SENS:SWE:OFFS:POIN {offset_points}",
                "TRIG:ACQ:SOUR BUS",
            ):
                session.write_checked(f"{command},{chan}")
            session.write_checked(f"INIT:IMM:ACQ {chan}")
        return tint

    # Keep execute.py unchanged: run_functions resolves this global; its tint *
    # CAPTURE_POINTS wait is safe because both short records finish before ARB ends.
    try:
        execute._arm_capture = arm
        yield
    finally:
        execute._arm_capture = original


def align(group, out):
    n = round(0.01 / DWELL)
    period = n * DWELL
    rise = round(0.005 / DWELL)
    width = round(0.001 / DWELL)
    levels = [0.0] * n
    levels[rise:rise + width] = [1.0] * width
    functions = [function(ch, levels, 256) for ch in range(1, 5)]
    tint = execute.CAPTURE_STEP
    points = math.floor((period - 0.002) / tint)
    edges = []
    print(f"align: period={period:.9g} s, dwell={DWELL:.9g} s; pulse rises at {rise * DWELL:.9g} s")
    for cycle in (0, 255):
        offset_points = round(cycle * period / tint)
        print(f"align: round {cycle + 1}, offset_points={offset_points}, "
              f"tint={tint:.9g} s ({tint / DWELL:.6g} dwell), points={points}")
        with align_capture(offset_points, points, tint):
            data = capture_run(group, functions, offset_points=offset_points)
        write_csv(out / f"align_round{cycle + 1}.csv", ["channel", "time_s", "voltage_V"],
                  ((ch, t, v) for ch, (times, volts, _) in data.items() for t, v in zip(times, volts)))
        selected = {}
        expected = cycle * period + 0.005
        for ch, (times, volts, _) in data.items():
            found = rising_edge_times(times, volts)
            candidates = found[np.abs(found - expected) <= period / 4]
            selected[ch] = (float(candidates[np.argmin(abs(candidates - expected))])
                            if candidates.size else math.nan)
            print(f"align: round {cycle + 1}, CH{ch}, rising edge={selected[ch]:.9g} s")
        edges.append(selected)
    metrics = align_metrics(edges[0], edges[1], period, DWELL)
    write_csv(out / "align.csv", ["channel", "t1_s", "t256_s", "drift_dwell", "rel_drift_dwell"],
              ((ch, edges[0][ch], edges[1][ch], metrics["drift_dwell"][ch],
                metrics["rel_drift_dwell"][ch]) for ch in edges[0]))
    print(f"align: spread1={metrics['spread1']:.6g}, spread256={metrics['spread256']:.6g} dwell")
    for ch in edges[0]:
        print(f"align: CH{ch}, d_k={metrics['drift_dwell'][ch]:.6g}, "
              f"rel_k={metrics['rel_drift_dwell'][ch]:.6g} dwell")
    print("align: d_CH1 含兩次觸發延遲差，僅供參考，不列入驗收")
    print(f"align: {metrics['status']} (spread1, spread256, max|rel_k| <= 1 dwell)")


def sinebw(group, out):
    frequencies = [10, 30, 100, 300, 1000, 3000]
    amplitudes = []
    for frequency in frequencies:
        n = round(max(5 / frequency, 0.05) / DWELL)
        levels = 2 + np.sin(2 * np.pi * frequency * (np.arange(n) + 1) * DWELL)
        times, volts, tint = capture_run(group, [function(1, levels)])[1]
        # Exclude post-waveform last-level hold and a short settling interval.
        mask = (times >= min(1 / frequency, 0.005)) & (times < n * DWELL)
        amplitude = fit_sine_amplitude(times[mask], volts[mask], frequency)
        amplitudes.append(amplitude)
        write_csv(out / f"sinebw_{frequency}Hz.csv", ["time_s", "voltage_V"], zip(times, volts))
        print(f"sinebw: f={frequency} Hz, amplitude={amplitude:.9g} V, sample interval={tint:.9g} s")
    if amplitudes[0] <= 1e-9:
        ratios = np.full(len(amplitudes), np.nan)
        print("sinebw: 無法求 -3 dB：10 Hz 基準振幅接近零")
    else:
        ratios = np.asarray(amplitudes) / amplitudes[0]
        crossing = interpolate_3db(frequencies, ratios)
        print("sinebw: -3 dB > 3 kHz" if crossing is None else f"sinebw: -3 dB={crossing:.9g} Hz")
    for frequency, ratio in zip(frequencies, ratios):
        print(f"sinebw: f={frequency} Hz, ratio={ratio:.9g}")
    write_csv(out / "sinebw.csv", ["frequency_Hz", "amplitude_V", "ratio"], zip(frequencies, amplitudes, ratios))


CHECKS = {"transfer": transfer, "seam": seam, "align": align, "sinebw": sinebw}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=[*CHECKS, "all"])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--resource", help="one VISA address; CH1-4 on this frame")
    mode.add_argument("--demo", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("hw_list_results"))
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    group = (InstrumentGroup.demo(frames=1, slots=execute.DEMO_SLOTS) if args.demo
             else execute.connect_group([args.resource]))
    try:
        execute.outputs_off(group)
        if args.demo:
            print("DEMO: simulator capture is not hardware measurement; numbers do not establish acceptance")
        for name in CHECKS if args.check == "all" else [args.check]:
            if name != "transfer":
                channels = "CH1..CH4" if name == "align" else "CH1"
                voltage = "1..3 V (offset 2 V, amplitude 1 V)" if name == "sinebw" else "0..1 V"
                print(f"{name}: 將施加 {voltage} 至 {channels}")
                if name == "sinebw" and not args.demo:
                    print("請接上實際負載")
                if not args.demo and not args.yes and input("輸入 y 繼續：").strip().lower() != "y":
                    print("已取消")
                    return 1
            try:
                CHECKS[name](group, args.out)
            finally:
                execute.outputs_off(group)
    finally:
        try:
            execute.outputs_off(group)
        finally:
            group.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
