"""Operator-run ARB experiments; this script does not establish hardware facts.

Examples (from an uninstalled checkout):
  python scripts/hw_arb_trig_check.py bus_delay --resource VISA_ADDRESS --yes
  python scripts/hw_arb_trig_check.py seqnum --demo --yes
  python scripts/hw_arb_trig_check.py ext

Only bus_delay/residual_bus enable an output (0..1 V, 0.1 A limit). Only slew
sends *RST, requiring --allow-reset AND consent; all skips slew without that
flag. Non-power checks refuse already-enabled outputs without changing them.
Settings and sequence memory are changed, not restored, except ARB trigger
source: if modified, cleanup restores its as-found value (or IMM if unreadable).
Cleanup aborts TRAN/ACQ and disables the selected output, even after an
exception. Other channels are left alone except for the whole-frame reset in
slew. Disconnect the DUT before using this tool.
residual_bus compares a script-local pre-A1 control with the current
execute.start_together; both start with residual ARB source BUS and retain
separate samples and command evidence. Demo refusals are evidence of simulator
limitations, never a hardware verdict.
CSV files are overwritten in
--out (default hw_arb_results). Exit 0 = checks completed, 1 = FAIL/INCONCLUSIVE
or cancelled; demo always reports INCONCLUSIVE.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import math
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from n6705c.core.arb import ArbSpec, ConstantDwellArb, GroupArbRunner
from n6705c.core.arb.scpi import configure_commands
from n6705c.core.capability import SMU_MODELS
from n6705c.core.function import ChannelFunction
from n6705c.core.group import InstrumentGroup
from n6705c.core.status import OperationStatus
from n6705c_seq import execute

BUS_DELAY = 2.0
# E.02.09 firmware: TRIG:ARB:SOUR is frame-wide and rejects a channel list (-108); set by --frame-wide-arb-source.
FRAME_WIDE_ARB_SOURCE = False


def arb_source_set(source, chan):
    return f"TRIG:ARB:SOUR {source}" + ("" if FRAME_WIDE_ARB_SOURCE else f",{chan}")


def arb_source_query(chan):
    return "TRIG:ARB:SOUR?" + ("" if FRAME_WIDE_ARB_SOURCE else f" {chan}")
DWELL = 0.001024  # 100 * 10.24 us, exactly on the CDW grid
LOW_TIME = 100 * DWELL
DURATION = 3 * LOW_TIME
EXT_INSTRUCTIONS = """EXT shared BNC: manual oscilloscope check only (no commands sent).
Disconnect the DUT. With outputs disabled, configure ARB and acquisition EXT
sources at the front panel; connect a suitable external trigger to the shared
BNC and observe that trigger plus the output on an oscilloscope. Arm both,
then apply one edge and compare their start times. Check the instrument manual
for BNC levels/polarity and wiring. This script does not configure pins or EXT
and cannot infer shared-BNC behavior from BUS measurements."""


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


class Check:
    """One experiment's evidence, including signed SCPI errors drained to zero."""

    def __init__(self, group, channel, out, name, demo=False):
        self.group, self.channel, self.out, self.name = group, channel, out, name
        self.session = group.instruments[0].session
        self.chan = f"(@{channel})"
        self.demo = demo
        self.started = time.monotonic()
        self.commands = []
        self.rows = []
        self.arb_source_before = None
        self.arb_source_changed = False

    def errors(self):
        errors = []
        for _ in range(50):
            reply = self.session.query("SYST:ERR?")
            code, sep, message = reply.partition(",")
            if not sep:
                raise RuntimeError(f"malformed SYST:ERR? response: {reply!r}")
            number = float(code)
            if not math.isfinite(number) or number != int(number):
                raise RuntimeError(f"malformed error code: {code!r}")
            if number == 0:
                return errors
            errors.append(f"{int(number):+d},{message}")
        raise RuntimeError("SYST:ERR? did not reach zero after 50 reads")

    def exchange(self, command, *, query=False, probe=False):
        row = {"elapsed_s": time.monotonic() - self.started, "command": command,
               "reply": "", "errors": ""}
        self.commands.append(row)
        try:
            if query:
                row["reply"] = self.session.query(command)
            else:
                self.session.write(command)
            errors = self.errors()
            row["errors"] = "; ".join(errors)
            if errors and not probe:
                raise RuntimeError(f"{command}: {row['errors']}")
            return row["reply"], errors
        except BaseException as exc:
            row["exception"] = str(exc)
            raise

    def write(self, command):
        self.exchange(command)

    def query(self, command):
        return self.exchange(command, query=True)[0]

    def remember_arb_source(self):
        """Snapshot once per subcheck, before any trigger-source mutation."""
        try:
            reply, errors = self.exchange(
                arb_source_query(self.chan), query=True, probe=True)
        except Exception as exc:
            reply, errors = "", [f"{type(exc).__name__}: {exc}"]
        source = reply.strip().strip('"').upper()
        # Only a single SCPI source token can safely be sent back as a setting.
        if not errors and re.fullmatch(r"[A-Z][A-Z0-9]*", source):
            self.arb_source_before = source
        self.event(phase="as_found", source=reply, errors="; ".join(errors))
        return reply, errors

    def set_arb_source(self, source, *, probe=False):
        # Mark BEFORE I/O: a write/error-drain exception may follow a mutation.
        self.arb_source_changed = True
        return self.exchange(arb_source_set(source, self.chan), probe=probe)

    def event(self, **values):
        self.rows.append(values)

    def save(self, status, detail):
        if self.demo:
            status, detail = "INCONCLUSIVE", f"DEMO only; {status}: {detail}"
        write_csv(self.out / f"{self.name}.csv", [
            {"status": status, "detail": detail, "channel": self.channel, **row}
            for row in (self.rows or [{}])])
        write_csv(self.out / f"{self.name}_commands.csv", self.commands)
        print(f"{self.name}: {status}: {detail}")
        return status


def cleanup(check):
    """Attempt every safety action even if abort or error checking fails."""
    failures = []
    for command in (f"ABOR:TRAN {check.chan}", f"ABOR:ACQ {check.chan}",
                    f"OUTP OFF,{check.chan}"):
        try:
            check.write(command)
        except Exception as exc:
            failures.append(f"{command}: {exc}")
    try:
        if float(check.query(f"OUTP? {check.chan}")) != 0:
            failures.append("OUTP? still reports enabled")
    except Exception as exc:
        failures.append(f"cannot confirm output off: {exc}")
    if check.arb_source_changed:
        source = check.arb_source_before or "IMM"
        try:
            check.write(arb_source_set(source, check.chan))
            actual = check.query(arb_source_query(check.chan)).strip().strip('"').upper()
            if actual != source:
                raise RuntimeError(f"ARB source readback {actual!r}, expected {source}")
            check.arb_source_changed = False
        except Exception as exc:
            failures.append(f"cannot restore ARB source {source}: {exc}")
    if failures:
        raise RuntimeError("cleanup failed: " + "; ".join(failures))


def arbsrc(check):
    default, errors = check.remember_arb_source()
    accepted = not errors and check.arb_source_before is not None
    for source in ("IMM", "BUS"):
        _, write_errors = check.set_arb_source(source, probe=True)
        reply, read_errors = check.exchange(arb_source_query(check.chan), query=True, probe=True)
        ok = not (write_errors or read_errors) and reply.strip().strip('"').upper() == source
        accepted &= ok
        check.event(phase=source, source=reply, accepted=ok,
                    errors="; ".join(write_errors + read_errors))
    return ("PASS" if accepted else "FAIL",
            "IMM/BUS command acceptance and readback; as_found is NOT a reset-default measurement")


def seqnum(check):
    observations = []
    check.write(f"ARB:FUNC:TYPE VOLT,{check.chan}")
    for step in (0, 1):
        check.write(f"ARB:SEQ:RES {check.chan}")
        baseline = int(float(check.query(f"ARB:SEQ:LENG? {check.chan}")))
        _, errors = check.exchange(f"ARB:SEQ:STEP:FUNC:SHAP STEP,{step},{check.chan}", probe=True)
        length = int(float(check.query(f"ARB:SEQ:LENG? {check.chan}")))
        observations.append((not errors, baseline, length))
        check.event(step=step, baseline_len=baseline, length=length,
                    accepted=not errors, errors="; ".join(errors))
    # E.02.09 leaves one default step after ARB:SEQ:RES (baseline 1), F.1.1.0 leaves none (baseline 0).
    (ok0, b0, l0), (ok1, b1, l1) = observations
    if ok0 and ok1 and b0 == b1 and l0 >= max(b0, 1) and l1 == l0 + 1:
        return "PASS", f"consistent with zero-based steps (baseline {b0}); endpoints 99/100 NOT tested"
    if not ok0 and l0 == b0 and ok1 and l1 == b0 + 1:
        return "PASS", "consistent with one-based steps; endpoints 99/100 NOT tested"
    return "INCONCLUSIVE", "step 0/1 acceptance and LEN do not identify the numbering base"


def slew(check):
    for phase in ("before_reset", "after_reset"):
        if phase == "after_reset":
            check.write("*RST")  # the ONLY reset in this script; main obtained consent
            if float(check.query("*OPC?")) != 1:
                raise RuntimeError("reset completion not confirmed")
        values = {}
        # N6705C Operating/Service Guide p.383: :MAX? is an override BOOL,
        # not a rate. Probe the numeric maximum separately; its query support
        # awaits HAs measurements. Refusal is logged and does not fail this check.
        # https://www.keysight.com/tz/en/assets/9018-04227/service-manuals/9018-04227.pdf
        for key, command in (("slew", f"VOLT:SLEW? {check.chan}"),
                             ("max_override", f"VOLT:SLEW:MAX? {check.chan}"),
                             ("maximum", f"VOLT:SLEW? MAX,{check.chan}")):
            reply, errors = check.exchange(command, query=True, probe=True)
            values[key], values[key + "_errors"] = reply, "; ".join(errors)
        check.event(phase=phase, **values)
    try:
        for row in check.rows:
            if row.get("phase") not in ("before_reset", "after_reset"):
                continue
            if row["slew_errors"] or row["max_override_errors"]:
                raise ValueError("query refused")
            rate, override = float(row["slew"]), float(row["max_override"])
            if not (math.isfinite(rate) and rate >= 0 and override in (0, 1)):
                raise ValueError("invalid slew/override reply")
    except ValueError:
        return "INCONCLUSIVE", "slew query unsupported or returned invalid data; see raw replies"
    return ("PASS", "reset-before/after replies collected; VOLT:SLEW:MAX? is a boolean override, NOT a numeric maximum; no rate-equality verdict")


def waveform(check):
    fn = ChannelFunction(channel=check.channel, voltage=0.0, current=0.1,
                         output_on=True, arb_enabled=True, spec=ArbSpec(
                             channel=check.channel, waveform=ConstantDwellArb(
                                 levels=[0.0] * 100 + [1.0] * 100 + [0.0] * 100,
                                 dwell=DWELL), terminate_last=True))
    # Validation against the detected module before enabling the output.
    GroupArbRunner.from_plan(check.group, [fn])
    check.write(f"VOLT:MODE FIX,{check.chan}")
    check.write(f"VOLT 0,{check.chan}")
    # N678xA SMUs reject CURR (+315, voltage priority); their limit is CURR:LIM (as in execute._apply_levels).
    smu = check.group.instruments[0].capabilities[check.channel].model.upper() in SMU_MODELS
    check.write(f"CURR{':LIM' if smu else ''} 0.1,{check.chan}")
    for command in configure_commands(fn.spec):
        check.write(command)
    return fn


def arm_capture(check, fn, window):
    # Same digitizer path and binary format as execute._arm_capture/_fetch.
    tint = execute._arm_capture(check.group, [fn], window)
    actual = float(check.query(f"SENS:SWE:TINT? {check.chan}"))
    if not math.isfinite(actual) or actual <= 0 or actual * execute.CAPTURE_POINTS < window:
        raise RuntimeError(f"digitizer interval {actual!r} cannot cover {window:g} s")
    check.event(phase="capture_config", requested_tint_s=tint, actual_tint_s=actual)
    check.write(f"OUTP ON,{check.chan}")
    origin = time.monotonic()
    # A targeted acquisition trigger cannot release the ARB, unlike *TRG.
    check.write(f"TRIG:ACQ {check.chan}")
    return actual, origin


def fetch_capture(check, fn, tint, origin, label):
    time.sleep(max(0, origin + tint * execute.CAPTURE_POINTS + 0.05 - time.monotonic()))
    data = {}
    execute._fetch(check.group, [fn], tint, data)
    errors = check.errors()  # include errors queued by the binary fetch
    if "error" in data or errors:
        raise RuntimeError(f"fetch failed: {data.get('error', '')}; {'; '.join(errors)}")
    volts = np.asarray(data[fn.channel][1], dtype=float)
    if volts.size != execute.CAPTURE_POINTS or not np.all(np.isfinite(volts)):
        raise RuntimeError(f"invalid capture: {volts.size} samples (expected {execute.CAPTURE_POINTS})")
    write_csv(check.out / f"{check.name}_{label}_samples.csv", [
        {"time_s": index * tint, "voltage_V": value} for index, value in enumerate(volts)])
    return volts


def edge_time(volts, tint):
    """First measured rising edge; an already-high first sample is ambiguous."""
    if volts[0] >= 0.5:
        return math.nan
    edges = np.flatnonzero((volts[:-1] < 0.5) & (volts[1:] >= 0.5))
    if not edges.size:
        return math.nan
    i = int(edges[0])
    return (i + (0.5 - volts[i]) / (volts[i + 1] - volts[i])) * tint


def classify_edge(edge, init_s, trigger_s, tint):
    # Host timestamps bound commands only; USB/LAN and instrument latency remain.
    tolerance = max(0.05, 5 * tint)
    if not math.isfinite(edge):
        return "no_observed_edge"
    if abs(edge - init_s - LOW_TIME) <= tolerance:
        return "at_init"
    if abs(edge - trigger_s - LOW_TIME) <= tolerance:
        return "at_bus_trigger"
    return "other_timing"


def bus_delay(check):
    check.remember_arb_source()
    results = []
    for source in ("IMM", "BUS"):
        cleanup(check)
        fn = waveform(check)
        check.set_arb_source(source)
        check.write(f"TRIG:TRAN:SOUR BUS,{check.chan}")
        for system, expected in (("ARB", source), ("TRAN", "BUS")):
            actual = check.query(arb_source_query(check.chan) if system == "ARB" else f"TRIG:TRAN:SOUR? {check.chan}").strip().strip('"').upper()
            if actual != expected:
                raise RuntimeError(f"{system} source readback {actual!r}, expected {expected}")
        tint, origin = arm_capture(check, fn, BUS_DELAY + DURATION + 0.5)
        init = time.monotonic() - origin
        check.write(f"INIT:TRAN {check.chan}")
        time.sleep(BUS_DELAY)
        trigger = time.monotonic() - origin
        check.write("*TRG")
        volts = fetch_capture(check, fn, tint, origin, source.lower())
        edge = edge_time(volts, tint)
        timing = classify_edge(edge, init, trigger, tint)
        results.append(timing)
        check.event(phase=source, init_s=init, bus_trigger_s=trigger, edge_s=edge,
                    inferred_wave_start_s=edge - LOW_TIME, timing=timing)
    if results == ["at_init", "at_bus_trigger"]:
        return "PASS", "supports ARB-source hypothesis (TRAN=BUS control; 50 ms minimum timing tolerance)"
    if results == ["at_init", "at_init"]:
        return "FAIL", "contradicts ARB-source hypothesis: both waveforms started at INIT"
    return "INCONCLUSIVE", f"timing={results}; inspect samples, load, slew and trigger latency"


def wait_finished(check):
    time.sleep(DURATION)
    deadline = time.monotonic() + execute.GRACE_SECONDS
    active = int(OperationStatus.TRAN_ACTIVE | OperationStatus.WTG_TRAN)
    while True:
        status = int(float(check.query(f"STAT:OPER:COND? {check.chan}")))
        if not status & active:
            return False, status
        if time.monotonic() >= deadline:
            return True, status
        time.sleep(0.05)


def start_pre_a1(check, trigger_acquisition=False):
    """Single-frame pre-A1 control: leave ARB source alone, set only TRAN IMM."""
    check.write(f"TRIG:TRAN:SOUR IMM,{check.chan}")
    check.write(f"INIT:TRAN {check.chan}")
    if trigger_acquisition:
        check.write("*TRG")


@contextmanager
def record_start(check, path, phase):
    """Record actual start I/O, including raw error replies, without extra I/O.

    Session.read_errors accesses its transport directly. Temporarily wrap that
    transport so the real start_together's checked writes are also in the CSV.
    Its workers join before this scope exits; always restore on failure too.
    """
    transport = check.session._transport

    class Recorder:
        def __getattr__(self, name):
            return getattr(transport, name)

        def exchange(self, command, query=False):
            row = {"elapsed_s": time.monotonic() - check.started,
                   "path": path, "phase": phase, "command": command,
                   "reply": "", "errors": ""}
            check.commands.append(row)
            try:
                if query:
                    row["reply"] = transport.query(command)
                    return row["reply"]
                transport.write(command)
            except BaseException as exc:
                row["exception"] = str(exc)
                raise

        def write(self, command):
            self.exchange(command)

        def query(self, command):
            return self.exchange(command, query=True)

    try:
        check.session._transport = Recorder()
        yield
    finally:
        check.session._transport = transport


def residual_bus(check):
    """Compare residual BUS before and after A1, with/without capture *TRG.

    pre_a1 is a script-local historical control, NOT execute.start_together:
    only TRAN is set to IMM. BUS must remain the ARB source; either INIT or
    BUS release is an observation, not an assumed hardware outcome. Without
    capture *TRG, a late rescue *TRG exposes waiting on residual BUS.

    current calls the real execute.start_together. PASS requires ARB/TRAN IMM,
    completion without timeout and a measured edge at INIT in both modes.
    A source mismatch, timeout or observed wrong timing is FAIL. Missing edges
    or ambiguous pre-A1 timing are INCONCLUSIVE; neither is a false PASS.
    PASS also requires usable pre-A1 observations in both modes. Exceptions
    remain INCONCLUSIVE (cleanup failures are FAIL); demo is always INCONCLUSIVE.
    Each path/mode retains source/status/timestamps, raw samples and start I/O.
    """
    check.remember_arb_source()
    verdicts = []
    for path, capture_trigger in ((path, flag) for path in ("pre_a1", "current")
                                  for flag in (False, True)):
        cleanup(check)
        fn = waveform(check)
        check.set_arb_source("BUS")
        source_before = check.query(arb_source_query(check.chan)).strip().strip('"').upper()
        if source_before != "BUS":
            raise RuntimeError(f"cannot establish residual BUS: readback {source_before!r}")
        window = execute.GRACE_SECONDS + 2 * DURATION + 0.75
        tint, origin = arm_capture(check, fn, window)
        started = time.monotonic() - origin
        label = "with_capture_trigger" if capture_trigger else "without_capture_trigger"
        if path == "pre_a1":
            first_command = len(check.commands)
            try:
                start_pre_a1(check, capture_trigger)
            finally:
                for row in check.commands[first_command:]:
                    row.update(path=path, phase=label)
        else:
            # Exercise the real implementation, including its worker and error
            # checks. No I/O overlaps it: start_together joins before returning.
            with record_start(check, path, label):
                execute.start_together(check.group, [fn], trigger_acquisition=capture_trigger)
        source = check.query(arb_source_query(check.chan)).strip().strip('"').upper()
        tran_source = check.query(f"TRIG:TRAN:SOUR? {check.chan}").strip().strip('"').upper()
        timed_out, status_before = wait_finished(check)
        rescue = math.nan
        if path == "pre_a1" and not capture_trigger:
            rescue = time.monotonic() - origin
            check.write("*TRG")
            time.sleep(DURATION + 0.05)
        status_after = check.query(f"STAT:OPER:COND? {check.chan}")
        volts = fetch_capture(check, fn, tint, origin, f"{path}_{label}")
        edge = edge_time(volts, tint)
        timing = classify_edge(edge, started, rescue, tint)
        if source != ("BUS" if path == "pre_a1" else "IMM") or tran_source != "IMM":
            verdict = "FAIL"
        elif path == "current" and (timed_out or timing not in ("at_init", "no_observed_edge")):
            verdict = "FAIL"
        elif timing not in ("at_init", "at_bus_trigger"):
            verdict = "INCONCLUSIVE"
        else:
            verdict = "PASS"
        verdicts.append(verdict)
        check.event(path=path, phase=label, verdict=verdict,
                    source_before=source_before, arb_source_after=source, tran_source_after=tran_source,
                    timed_out=timed_out, status_before=status_before,
                    status_after=status_after, start_call_s=started, rescue_trigger_s=rescue,
                    edge_s=edge, timing=timing,
                    observation=("released_by_late_bus" if timing == "at_bus_trigger" else
                                 "waveform_observed" if math.isfinite(edge) else "no_waveform_observed"))
    status = ("FAIL" if "FAIL" in verdicts else
              "INCONCLUSIVE" if "INCONCLUSIVE" in verdicts else "PASS")
    return status, ("pre-A1 control observes residual BUS; current start_together must replace BUS "
                    "with IMM and finish at INIT; compare per-path verdicts, sources and raw evidence")


CHECKS = {"arbsrc": arbsrc, "bus_delay": bus_delay, "residual_bus": residual_bus,
          "seqnum": seqnum, "slew": slew}
POWER_CHECKS = {"bus_delay", "residual_bus"}


def require_outputs_off(check):
    """Refuse non-power probes before cleanup or any other setting write."""
    for channel in check.group.instruments[0].populated_channels:
        state = float(check.query(f"OUTP? (@{channel})"))
        if state != 0:
            raise RuntimeError(f"turn outputs off before {check.name}; CH{channel} is not confirmed off")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=[*CHECKS, "all", "ext"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resource", help="one N6705C VISA address")
    mode.add_argument("--demo", action="store_true")
    parser.add_argument("--channel", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--out", type=Path, default=Path("hw_arb_results"))
    parser.add_argument("--yes", action="store_true", help="confirm the selected actions; reset also requires --allow-reset")
    parser.add_argument("--frame-wide-arb-source", action="store_true",
                        help="firmware E.02.09: TRIG:ARB:SOUR takes no channel list")
    parser.add_argument("--allow-reset", action="store_true", help="allow slew to reset the entire frame with *RST")
    args = parser.parse_args(argv)
    global FRAME_WIDE_ARB_SOURCE
    FRAME_WIDE_ARB_SOURCE = args.frame_wide_arb_source
    print(EXT_INSTRUCTIONS)
    if args.check == "ext":
        return 0
    if not args.resource and not args.demo:
        parser.error("one of --resource or --demo is required")
    names = list(CHECKS) if args.check == "all" else [args.check]
    if "slew" in names and not args.allow_reset:
        if args.check == "slew":
            parser.error("slew sends whole-frame *RST: --allow-reset is required (even with --yes)")
        names.remove("slew")
        print("Skipping slew: whole-frame *RST requires --allow-reset; --yes alone does not authorise reset.")
    reset = "slew" in names
    power = bool(POWER_CHECKS.intersection(names))
    print(f"Selected CH{args.channel}; changes settings/sequence memory; finally aborts TRAN/ACQ and turns this output off.")
    if reset:
        print("WARNING: slew sends *RST ON THE ENTIRE FRAME; all channels/settings are reset. Disconnect the DUT.")
    if power:
        print(f"WARNING: enables CH{args.channel}, 0..1 V waveform, current limit 0.1 A. Disconnect the DUT.")
    if (reset or power) and not args.yes:
        try:
            consent = input("Confirm the above actions (including *RST if listed), type y: ")
        except (EOFError, KeyboardInterrupt):
            consent = ""
        if consent.strip().lower() != "y":
            print("Cancelled; no connection opened")
            return 1
    if args.demo:
        print("DEMO: simulator results are never hardware acceptance; unsupported ARB source/slew probes may return errors.")
    args.out.mkdir(parents=True, exist_ok=True)
    group = (InstrumentGroup.demo(frames=1, slots=execute.DEMO_SLOTS) if args.demo
             else execute.connect_group([args.resource]))
    result = 0
    try:
        if args.channel not in group.instruments[0].populated_channels:
            raise ValueError(f"CH{args.channel} is not populated")
        for name in names:
            check = Check(group, args.channel, args.out, name, args.demo)
            try:
                if name not in POWER_CHECKS:
                    require_outputs_off(check)
            except Exception as exc:
                check.save("FAIL", f"{type(exc).__name__}: {exc}")
                raise  # No cleanup: preserve the pre-existing outputs/settings.
            status, detail = "INCONCLUSIVE", "interrupted before completion"
            try:
                cleanup(check)
                check.event(phase="identity", idn=check.query("*IDN?"))
                status, detail = CHECKS[name](check)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    cleanup(check)
                except Exception as exc:
                    status, detail = "FAIL", f"{detail}; {exc}"
                    # An unconfirmed-off output must stop 'all', not run another check.
                    raise
                finally:
                    # Save incomplete evidence on Ctrl-C too, before close.
                    saved_status = check.save(status, detail)
            if saved_status != "PASS":
                result = 1
    finally:
        group.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
