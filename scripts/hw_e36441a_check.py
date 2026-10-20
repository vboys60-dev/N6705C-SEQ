"""Human-operated E36441A checks: can LIST + SLEW replace an ARB trapezoid?

No scope needed: the output is polled with MEAS:VOLT? and (t, v) pairs are
timed with time.perf_counter. The poll interval is set by VISA round trips
(tens of ms), so every verdict below tolerates one sample interval and the
interval is printed with the result. Uses one channel only.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from pathlib import Path

# Allow the documented command from an uninstalled source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from n6705c.core.session import Session
from n6705c.core.transport import VisaTransport

V_MAX, I_MAX = 32.0, 10.0  # E36441A channel limits used for argument validation
DWELL_MIN = 0.01           # LIST:DWEL range starts at 10 ms
LOW_DWELL = 0.5            # dwell of the trailing low step in checks 2 and 3
ABS_TOL = 0.005            # fixed timing tolerance (s) added to one sample interval


def sample_interval(samples):
    return statistics.median(b[0] - a[0] for a, b in zip(samples, samples[1:]))


def _crossings(samples, level, rising):
    """Interpolated times where the trace crosses level in the given direction."""
    out = []
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        if (v0 < level <= v1) if rising else (v0 > level >= v1):
            out.append(t0 + (t1 - t0) * (level - v0) / (v1 - v0))
    return out


def rise_time_10_90(samples, low, high):
    """First 10%-90% rise time in s, or None when either crossing is missing."""
    lo = _crossings(samples, low + 0.1 * (high - low), True)
    hi = _crossings(samples, low + 0.9 * (high - low), True)
    return hi[0] - lo[0] if lo and hi and hi[0] >= lo[0] else None


def flat_top_duration(samples, level):
    """Time from the first upward to the next downward crossing of level, else None."""
    up = _crossings(samples, level, True)
    if not up:
        return None
    down = [t for t in _crossings(samples, level, False) if t > up[0]]
    return down[0] - up[0] if down else None


def still_at(samples, level, tol):
    return bool(samples) and all(abs(v - level) <= tol for _, v in samples)


def classify_dwell(dur90, tr, rise, hold, tol):
    """'switch' if dwell counts from the switch point, 'settle' if from end of slew.

    dur90 + tr is the measured high-step dwell (ramp-up share below 90% plus the
    time above 90%); minus the nominal rise it is the full-level flat top.
    """
    flat = dur90 + tr - rise
    if abs(flat - hold) <= tol:
        return "switch", flat
    if abs(flat - (rise + hold)) <= tol:
        return "settle", flat
    return None, flat


def sample(session, ch, seconds, t0, out):
    """Poll MEAS:VOLT? until t0 + seconds; append (t, v) to out."""
    while time.perf_counter() - t0 < seconds:
        before = time.perf_counter()
        v = session.query_float(f"MEAS:VOLT? (@{ch})")
        out.append(((before + time.perf_counter()) / 2 - t0, v))


def configure(session, ch, args, volts, dwells, slew=None):
    """Most conservative LIST state after *RST: BUS trigger, output left at v_low."""
    c = f"(@{ch})"
    w = session.write_checked
    w(f"ABOR {c}")
    for cmd in ("INIT:CONT OFF", "TRIG:SOUR BUS", "VOLT:MODE LIST", "CURR:MODE FIX",
                f"LIST:VOLT {','.join(map(str, volts))}",
                f"LIST:DWEL {','.join(map(str, dwells))}",
                "LIST:COUN 1", "LIST:STEP AUTO", "LIST:TERM:LAST ON",
                "LIST:CURR %g" % args.current, "LIST:TOUT:BOST 0", "LIST:TOUT:EOST 0",
                "CURR %g" % args.current):
        w(f"{cmd},{c}")
    for edge in ("RIS", "FALL"):
        w(f"VOLT:SLEW:{edge} {'INF' if slew is None else '%g' % slew},{c}")
    w(f"VOLT {args.v_low:g},{c}")
    w(f"OUTP ON,{c}")
    time.sleep(1.0)  # let the output settle at v_low before arming


def finish(session, name, verdict, detail):
    """Print the error queue and demote PASS if it was not empty."""
    errors = session.read_errors()
    print(f"{name}: error queue: {[str(e) for e in errors] or 'empty'}")
    if errors and verdict == "PASS":
        verdict = "FAIL"
    print(f"{name}: {verdict} {detail}".rstrip())
    return verdict


def run_list(session, ch, args, volts, dwells, seconds, slew=None):
    """Arm, trigger once; samples have t=0 right after *TRG, one pre-trigger sample first."""
    configure(session, ch, args, volts, dwells, slew)
    session.write_checked(f"INIT (@{ch})")
    before = time.perf_counter()
    v_pre = session.query_float(f"MEAS:VOLT? (@{ch})")  # anchor so a hard step still has a low sample
    session.write("*TRG")
    t0 = time.perf_counter()
    data = [(before - t0, v_pre)]
    sample(session, ch, seconds, t0, data)
    return data


def check1(session, ch, args, out):
    """INIT must leave the output untouched until *TRG."""
    configure(session, ch, args, [args.v_low, args.v_high], [0.5, 0.5])
    session.write_checked(f"INIT (@{ch})")
    waiting = []
    sample(session, ch, 3.0, time.perf_counter(), waiting)
    moved = not still_at(waiting, args.v_low, 0.1)
    after = []
    session.write("*TRG")
    sample(session, ch, 1.5, time.perf_counter(), after)
    out["1_wait"], out["1_after_trg"] = waiting, after
    reached = max(v for _, v in after) >= args.v_low + 0.9 * (args.v_high - args.v_low)
    print(f"1: dt={sample_interval(waiting):.4f} s, wait min/max="
          f"{min(v for _, v in waiting):.3f}/{max(v for _, v in waiting):.3f} V, "
          f"after *TRG max={max(v for _, v in after):.3f} V")
    if moved:
        print("1: WARNING: output moved after INIT without *TRG (N6705C behaves this way too)")
        return finish(session, "1", "FAIL", "BUS trigger did not wait for *TRG")
    if not reached:
        return finish(session, "1", "INCONCLUSIVE", "held at v_low but *TRG did not step the output")
    return finish(session, "1", "PASS", "INIT waits for *TRG; *TRG starts the list")


def check2(session, ch, args, out):
    """Two-point LIST must honour VOLT:SLEW: compare 10-90% time with the programmed one."""
    dv = args.v_high - args.v_low
    data = run_list(session, ch, args, [args.v_high, args.v_low],
                    [args.rise + args.hold, LOW_DWELL], args.rise + args.hold + 1.0,
                    slew=dv / args.rise)
    out["2"] = data
    dt = sample_interval(data)
    tr, expected = rise_time_10_90(data, args.v_low, args.v_high), 0.8 * args.rise
    print(f"2: dt={dt:.4f} s, slew={dv / args.rise:g} V/s, expected 10-90%={expected:.4f} s, "
          f"measured={'n/a' if tr is None else f'{tr:.4f} s'}")
    if tr is None or expected < 2 * dt:
        verdict, detail = "INCONCLUSIVE", "(samples too coarse for this rise; lengthen --rise)"
    elif tr <= 1.5 * dt:
        verdict, detail = "FAIL", "(rise completed within one sample: SLEW not applied to LIST)"
    elif abs(tr - expected) <= 0.1 * expected + dt:
        verdict, detail = "PASS", "(SLEW applied to LIST)"
    else:
        verdict, detail = "FAIL", "(rise time off by more than 10% + one sample)"
    return finish(session, "2", verdict, detail)


def check3(session, ch, args, out):
    """Does the high step's dwell start at the switch point or after slew settles?"""
    dwell = args.rise + args.hold
    dv = args.v_high - args.v_low
    data = run_list(session, ch, args, [args.v_high, args.v_low], [dwell, LOW_DWELL],
                    dwell + args.rise + 1.0, slew=dv / args.rise)
    out["3"] = data
    dt = sample_interval(data)
    tr = rise_time_10_90(data, args.v_low, args.v_high)
    dur90 = flat_top_duration(data, args.v_low + 0.9 * dv)
    print(f"3: dt={dt:.4f} s, dwell={dwell:g} s, hold={args.hold:g} s, >=90% for "
          f"{'n/a' if dur90 is None else f'{dur90:.4f} s'}, rise10-90="
          f"{'n/a' if tr is None else f'{tr:.4f} s'}")
    if tr is None or dur90 is None:
        return finish(session, "3", "INCONCLUSIVE", "(missing rise or fall crossing)")
    if tr <= 1.5 * dt:
        return finish(session, "3", "INCONCLUSIVE", "(no slew ramp seen; see check 2)")
    kind, flat = classify_dwell(dur90, tr, args.rise, args.hold, dt + ABS_TOL)
    print(f"3: estimated flat top={flat:.4f} s (tolerance +-{dt + ABS_TOL:.4f} s)")
    if kind == "switch":
        return finish(session, "3", "PASS", "(dwell counts from the switch point; set dwell = rise + hold)")
    if kind == "settle":
        print("3: dwell counts only after slew settles; suggestion: set dwell = hold")
        return finish(session, "3", "FAIL", "(dwell starts after settle)")
    return finish(session, "3", "INCONCLUSIVE", "(flat top matches neither hold nor rise+hold)")


CHECKS = {1: check1, 2: check2, 3: check3}


def restore(session, ch):
    """Best-effort safe state; each step is independent so one failure cannot skip the rest."""
    c = f"(@{ch})"
    for cmd in (f"ABOR {c}", f"OUTP OFF,{c}", f"VOLT:SLEW:RIS INF,{c}", f"VOLT:SLEW:FALL INF,{c}",
                f"LIST:TERM:LAST OFF,{c}", f"VOLT:MODE FIX,{c}"):
        try:
            session.write(cmd)
        except Exception as exc:
            print(f"restore: {cmd!r} failed: {exc}")
    try:
        errors = session.read_errors()
        print(f"restore: error queue: {[str(e) for e in errors] or 'empty'}")
    except Exception as exc:
        print(f"restore: could not read error queue: {exc}")


def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def validate(args):
    if not 0 <= args.v_low < args.v_high <= V_MAX:
        raise SystemExit(f"need 0 <= v-low < v-high <= {V_MAX:g} V")
    if not 0 <= args.current <= I_MAX:
        raise SystemExit(f"--current must be within 0..{I_MAX:g} A")
    if args.rise <= 0 or args.hold <= 0 or args.rise + args.hold <= DWELL_MIN:
        raise SystemExit("--rise and --hold must be > 0 and rise + hold > 10 ms")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True, help="one VISA address of the E36441A")
    parser.add_argument("--channel", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--only", type=int, choices=sorted(CHECKS), help="run one check (default all)")
    parser.add_argument("--v-low", type=float, default=2.0)
    parser.add_argument("--v-high", type=float, default=5.0)
    parser.add_argument("--rise", type=float, default=0.2, help="programmed full-swing slew time (s)")
    parser.add_argument("--hold", type=float, default=0.3, help="intended flat top (s)")
    parser.add_argument("--current", type=float, default=0.5, help="current limit (A)")
    parser.add_argument("--csv", type=Path, help="write raw (t, v) samples here")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    validate(args)
    transport = VisaTransport(args.resource)
    transport._inst.encoding = "latin-1"  # noqa: SLF001 - *IDN? may hold non-ASCII bytes
    session = Session(transport)
    ch, out, results = args.channel, {}, {}
    try:
        idn = session.query("*IDN?")
        print(f"IDN: {idn}")
        if "E36441A" not in idn:
            print("拒絕執行：不是 E36441A")
            return 2
        names = [args.only] if args.only else list(CHECKS)
        print(f"將對整台儀器（四個通道）執行 *RST，並在 CH{ch} 施加 {args.v_low:g}..{args.v_high:g} V、限流 {args.current:g} A，"
              f"檢查 {names}（請接上實際負載）")
        if not args.yes and input("輸入 y 繼續：").strip().lower() != "y":
            print("已取消")
            return 1
        session.write_checked("*RST")
        for n in names:
            results[n] = CHECKS[n](session, ch, args, out)
    finally:
        restore(session, ch)
        session.close()
    if args.csv:
        write_csv(args.csv, ["check", "time_s", "voltage_V"],
                  ((k, t, v) for k, s in out.items() for t, v in s))
    print("summary: " + ", ".join(f"check {n}={r}" for n, r in results.items()))
    return 1 if "FAIL" in results.values() else 0


if __name__ == "__main__":
    raise SystemExit(main())
