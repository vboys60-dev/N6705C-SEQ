"""Human-operated H0 probes; demo tests plumbing and never establishes H0 PASS.

Run each transport separately, with the same --out directory (default
hw_elog_results). `long` runs TWO profiles, 3600 s each, at 250 ms cadence.
Use --seconds for a shorter diagnostic (not acceptance). `all` includes an
energised CH1 0..1 V / 0.1 A ARB probe and therefore requires --yes.
No probe resets the instrument. Non-ARB probes refuse already-enabled outputs.
ELOG has no timestamps: produced/backlog/overflow are host-time estimates,
not instrument counters. Constant input cannot identify which samples were lost.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import math
from pathlib import Path
from types import SimpleNamespace
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from n6705c.core.arb import ArbSpec, ConstantDwellArb
from n6705c.core.arb.scpi import configure_commands
from n6705c.core.elog import ElogConfig, ElogSession
from n6705c.core.group import InstrumentGroup
from n6705c.core.parsing import split_block_list
from n6705c.core.status import OperationStatus
from n6705c_seq import execute

CHECKS = ("firstread", "format", "maxrec", "stall", "tail", "long", "elog_arb")
INTERVAL = 0.250
CH = 1  # --channel; the ARB probe and single-channel profiles use this channel
# Minimum host/instrument clock-rate uncertainty for production estimates.
CLOCK_DRIFT_PPM = 50
COLUMNS = ("kind", "check", "variant", "profile", "transport", "demo", "idn",
           "utc", "channels", "requested_period_s", "period_s", "format",
           "maxrecords", "elapsed_s", "records_produced", "records_received",
           "backlog_before", "records", "wire_bytes", "empty_blocks",
           "empty_backlog", "overflow_estimated", "errors", "notes",
           "first_values", "last_values", "phase", "startup_delay_s",
           "abort_delay_s", "accounting_tolerance", "record_deficit",
           "after_abort_records", "status", "detail")


class ProbeIO:
    """Local session adapter: checked settings and ASCII-to-REAL normalisation.

    ElogSession.fetch only parses REAL blocks. Read ASCII on the wire, then
    encode the numeric values in a block for its unchanged alignment/accounting.
    Keep original wire length/empty-block evidence, including on parser failure.
    """

    def __init__(self, session, *, demo=False):
        self.base = session
        self.demo = demo
        self.ascii = False
        self.errors = []
        self.notes = []
        self.wire_bytes = 0
        self.empty_blocks = 0
        self.trigger_started_at = None

    def __getattr__(self, name):
        return getattr(self.base, name)

    def read_errors(self):
        errors = self.base.read_errors()
        self.errors.extend(str(e) for e in errors)
        return errors

    def check_errors(self, context=""):
        errors = self.read_errors()
        fatal = [e for e in errors if not (e.code == 310 and "RANG:AUTO ON" in context)]
        self.notes.extend(str(e) for e in errors if e not in fatal)
        if errors:
            del self.errors[-len(errors):]
            self.errors.extend(str(e) for e in fatal)
        if fatal:
            raise RuntimeError(f"{context}: " + "; ".join(str(e) for e in fatal))

    def write(self, command):
        # Wave-0 fake has no TRIG:ARB:SOUR handler. The simulator's IMM
        # transient semantics suffice for plumbing; do not change core/fake.py.
        if self.demo and command.startswith("TRIG:ARB:SOUR "):
            self.notes.append("DEMO only: TRIG:ARB:SOUR IMM not modelled")
            return
        if command.startswith("TRIG:ELOG "):
            self.trigger_started_at = time.monotonic()
        self.base.write(command)
        self.check_errors(command)

    write_checked = write

    def query_binary(self, command):
        self.wire_bytes = self.empty_blocks = 0
        raw = self.base.query_binary(command)
        self.wire_bytes = len(raw)
        try:
            if self.ascii:
                text = raw.decode("ascii").strip()
                values = [] if not text else [float(v) for v in text.split(",")]
                if not np.all(np.isfinite(values)):
                    raise ValueError("non-finite ASCII data")
                payload = np.asarray(values, dtype="<f4").tobytes()
                size = str(len(payload)).encode("ascii")
                self.empty_blocks = int(not payload)
                return b"#" + str(len(size)).encode("ascii") + size + payload
            blocks = split_block_list(raw)
            self.empty_blocks = sum(not b for b in blocks)
            return raw
        finally:
            self.check_errors(command)


class FixedFetch(ElogSession):
    """Change only maxrecords; all fetch I/O/parsing stays in current core."""

    def __init__(self, instrument, config, maxrecords):
        super().__init__(instrument, config)
        self.maxrecords = maxrecords

    def _records_to_request(self):
        return self.maxrecords


def pause(elog, seconds, demo):
    if not demo:
        time.sleep(max(0, seconds))
        return
    # Advance only this demo's time origins. No global clock monkeypatch and
    # no one-hour sleeps; long demo is deliberately a short smoke run.
    elog._t0 -= seconds
    transport = elog.instrument.session.base._transport
    for channel in transport.channels.values():
        channel.elog_t0 -= seconds


def trials(check, seconds, demo):
    # Deliberate fixed 8192 baseline: hold request size constant across probes
    # and avoid the suspected 16384 boundary. Only maxrec changes this value;
    # it is a diagnostic control, not a measured ELOG_FETCH_RECORDS decision.
    base = dict(profile="single", channels=[CH], period=0.001,
                fmt="REAL", maxrecords=8192, delay=0.0, duration=2.0,
                tail="abort_then_drain", arb=False)
    changes = {
        "firstread": [("immediate", {}), ("delay5", dict(delay=5.0))],
        "format": [("real", dict(delay=5.0)), ("ascii", dict(delay=5.0, fmt="ASC"))],
        "maxrec": [("8192", dict(delay=5.0)), ("16384", dict(delay=5.0, maxrecords=16384))],
        "stall": [(str(s), dict(delay=float(s), duration=0.0,
                               tail="drain_then_abort")) for s in (5, 12, 25)],
        "tail": [(s, dict(tail=s)) for s in ("drain_then_abort", "abort_then_drain")],
        "long": [("single", dict(duration=1.0 if demo else seconds)),
                 ("four_vi", dict(profile="four_vi", channels=[1, 2, 3, 4],
                                  period=0.0008192, duration=1.0 if demo else seconds))],
        "elog_arb": [("off", {}), ("on", dict(arb=True))],
    }
    return [(name, dict(base, **delta)) for name, delta in changes[check]]


def accounting_tolerance(row, *, polling=True):
    """Host-time bounds, not an instrument loss counter.

    _t0 follows TRIG's error checks and cutoff precedes ABOR's error checks.
    Their measured spans bound uncounted production at both ends. Allow one
    sample boundary plus >=50 ppm clock drift, as well as one poll interval.
    """
    period = float(row["period_s"])
    edges = float(row.get("startup_delay_s") or 0) + float(row.get("abort_delay_s") or 0)
    drift = float(row["elapsed_s"]) * CLOCK_DRIFT_PPM / 1_000_000
    return (math.ceil(INTERVAL / period) if polling else 0) + math.ceil((edges + drift) / period) + 1


def trial_status(row, check, variant):
    """Local result only; the aggregate H0 gate is evaluated separately."""
    if row["demo"]:
        return "INCONCLUSIVE", "demo plumbing only"
    if row["errors"] or row["empty_backlog"]:
        return "INCONCLUSIVE", "error or empty backlog; compare one-variable alternatives"
    loss = abs(row["records_produced"] - row["records_received"])
    if check == "stall" and variant != "5":
        return "INCONCLUSIVE", "overflow depth observed; discard direction needs identifiable external input"
    if check == "long" and row["elapsed_s"] < 3600:
        return "INCONCLUSIVE", "requires >=3600 s per profile on USB and LAN"
    limit = accounting_tolerance(row)
    if loss > limit or row["overflow_estimated"]:
        return "INCONCLUSIVE", "record deficit/estimated overflow; insufficient for section 5 FAIL alone"
    return "PASS", f"local accounting within {limit} records; host-time production estimate"


def run_trial(inst, check, variant, cfg, writer, *, demo, transport, idn):
    io = ProbeIO(inst.session, demo=demo)
    elog = FixedFetch(SimpleNamespace(session=io), ElogConfig(
        channels=cfg["channels"], period=cfg["period"]), cfg["maxrecords"])
    common = dict(check=check, variant=variant, profile=cfg["profile"],
                  transport=transport, demo=demo, idn=idn,
                  utc=datetime.now(timezone.utc).isoformat(),
                  channels=",".join(map(str, cfg["channels"])),
                  requested_period_s=cfg["period"], format=cfg["fmt"],
                  maxrecords=cfg["maxrecords"])
    empty_backlog = 0
    overflow = False
    cutoff = None
    problem = ""
    failures = []
    first = last = ""
    startup_delay = abort_delay = 0.0
    after_abort_records = 0

    def fetch(phase):
        nonlocal empty_backlog, overflow, first, last, after_abort_records
        elapsed = cutoff if cutoff is not None else elog.elapsed
        produced = int(elapsed / elog.actual_period)
        backlog = max(0, produced - elog.stats.records_received)
        io.wire_bytes = io.empty_blocks = 0
        count = 0
        failure = ""
        try:
            chunk = elog.fetch()
            count = chunk.n_records
            if count:
                values = np.concatenate(list(chunk.data.values()), axis=1)
                if not np.all(np.isfinite(values)):
                    raise ValueError("non-finite ELOG data")
                first, last = repr(values[0].tolist()), repr(values[-1].tolist())
            if phase == "after_abort":
                after_abort_records += count
        except Exception as exc:
            failure = str(exc)
            failures.append(failure)
            # Transport/parser errors must drain the queue too.
            io.read_errors()
        anomaly = backlog > 2 * math.ceil(INTERVAL / elog.actual_period) and (
            count == 0 or io.empty_blocks > 0)
        empty_backlog += int(anomaly)
        overflow |= backlog > elog.stats.buffer_capacity
        row = dict(common, kind="fetch", period_s=elog.actual_period,
                   elapsed_s=elapsed, records_produced=produced,
                   records_received=elog.stats.records_received, backlog_before=backlog,
                   records=count, wire_bytes=io.wire_bytes, empty_blocks=io.empty_blocks,
                   empty_backlog=int(anomaly), overflow_estimated=overflow,
                   errors="; ".join(io.errors + ([failure] if failure else [])),
                   notes="; ".join(io.notes + elog.notes), first_values=first,
                   last_values=last, phase=phase)
        writer.writerow(row)
        return count, failure

    try:
        elog.configure()
        io.write("FORM " + cfg["fmt"])
        io.ascii = cfg["fmt"] == "ASC"
        if cfg["arb"]:
            spec = ArbSpec(CH, ConstantDwellArb(levels=[0.0, 1.0], dwell=0.1), count="INF")
            spec.validate()
            for command in [f"VOLT 0,(@{CH})", f"CURR 0.1,(@{CH})", *configure_commands(spec),
                            f"TRIG:ARB:SOUR IMM,(@{CH})", f"TRIG:TRAN:SOUR IMM,(@{CH})"]:
                io.write(command)
        elog.start()
        startup_delay = max(0, time.monotonic() - io.trigger_started_at)
        if cfg["arb"]:
            io.write(f"OUTP ON,(@{CH})")
            io.write(f"INIT:TRAN (@{CH})")
            status = inst.operation_status([CH])[CH]
            io.check_errors("STAT:OPER:COND?")
            if not (status & OperationStatus.TRAN_ACTIVE) or status & OperationStatus.WTG_TRAN:
                raise RuntimeError("ARB not running alongside ELOG")
        pause(elog, cfg["delay"], demo)
        fetch("first")
        start = elog.elapsed
        end = start + cfg["duration"]
        due = start + INTERVAL
        while elog.elapsed < end:
            pause(elog, max(0, min(due, end) - elog.elapsed), demo)
            fetch("poll")
            due += INTERVAL
            if due < elog.elapsed:
                due = elog.elapsed + INTERVAL
        # Same 250 ms unread tail for both strategies; only ABOR order varies.
        if check == "tail":
            pause(elog, INTERVAL, demo)
        if cfg["tail"] == "drain_then_abort":
            for _ in range(64):
                n, failure = fetch("before_abort")
                if not n or failure:
                    break
        cutoff = elog.elapsed
        # Do not call stop(): current core forbids fetching in STOPPED state.
        # Leave the local state RUNNING solely to probe hardware after ABOR.
        abort_start = time.monotonic()
        try:
            io.write(f"ABOR:ELOG {elog.chanlist}")
        finally:
            abort_delay = max(0, time.monotonic() - abort_start)
        for _ in range(64):
            n, failure = fetch("after_abort")
            if not n or failure:
                break
    except BaseException as exc:
        problem = str(exc)
        io.read_errors()
        if not isinstance(exc, Exception):
            raise
    finally:
        # Independent cleanup attempts: one failed ABOR must not skip OFF.
        commands = [f"ABOR:ELOG {elog.chanlist}"]
        if check == "elog_arb":
            commands += [f"ABOR:TRAN (@{CH})", f"OUTP OFF,(@{CH})"]
        if check == "format":
            commands += ["FORM REAL"]
        for command in commands:
            try:
                io.write(command)
            except Exception as exc:
                problem += f"; cleanup {command}: {exc}"
        elapsed = cutoff if cutoff is not None else max(0, elog.elapsed)
        row = dict(common, kind="summary", period_s=elog.actual_period,
                   elapsed_s=elapsed, records_produced=int(elapsed / elog.actual_period),
                   records_received=elog.stats.records_received,
                   startup_delay_s=startup_delay, abort_delay_s=abort_delay,
                   after_abort_records=after_abort_records,
                   empty_backlog=empty_backlog, overflow_estimated=overflow,
                   errors="; ".join(io.errors + failures + ([problem] if problem else [])),
                   notes="; ".join(io.notes + elog.notes))
        row["record_deficit"] = row["records_produced"] - row["records_received"]
        row["accounting_tolerance"] = accounting_tolerance(row)
        row["status"], row["detail"] = trial_status(row, check, variant)
        if check == "stall":
            row["detail"] += (f"; retained={row['records_received']}, "
                              f"produced~={row['records_produced']}; "
                              "discard direction INCONCLUSIVE (no sample timestamps/marker)")
        writer.writerow(row)
        print(f"{check}/{variant}: {row['status']} — {row['detail']}")
    return row


def tail_choice(rows):
    """Compare matched real trials; never infer a winner from one local PASS.

    Poll-interval slack is useful for local accounting but could hide the whole
    deliberately unread tail. Use only measured edge/clock uncertainty here.
    More after-ABOR records alone do not prove completeness: compare deficits
    too, since drain_then_abort has already consumed its tail before ABOR.
    """
    groups = {}
    for row in rows:
        if row["check"] != "tail" or str(row.get("demo")).lower() != "false":
            continue
        key = tuple(row.get(k) for k in ("idn", "transport", "profile", "channels",
                    "period_s", "requested_period_s", "format", "maxrecords", "_source"))
        groups.setdefault(key, {}).setdefault(row["variant"], []).append(row)
    if not groups:
        return None, "no paired hardware tail evidence"
    if len({key[0] for key in groups}) != 1:
        return None, "same-unit tail evidence required"
    winners, details = set(), []
    for variants in groups.values():
        if not {"drain_then_abort", "abort_then_drain"} <= variants.keys():
            return None, "both tail strategies required in each matched comparison"
        for drain in variants["drain_then_abort"]:
            for abort in variants["abort_then_drain"]:
                pair = (drain, abort)
                required = ("after_abort_records", "startup_delay_s", "abort_delay_s")
                if any(any(r.get(k) in (None, "") for k in required) or r["errors"] or
                       int(r["empty_backlog"]) or
                       str(r["overflow_estimated"]).lower() == "true" for r in pair):
                    return None, "tail evidence missing timing/counts, or contains errors/overflow"
                deficits = [int(r["records_produced"]) - int(r["records_received"]) for r in pair]
                bounds = [accounting_tolerance(r, polling=False) for r in pair]
                post = [int(r["after_abort_records"]) for r in pair]
                complete = [abs(d) <= b for d, b in zip(deficits, bounds)]
                delta = deficits[1] - deficits[0]
                uncertainty = sum(bounds)
                detail = (f"{drain['transport']}: after_abort drain/abort={post[0]}/{post[1]}, "
                          f"deficit drain/abort={deficits[0]}/{deficits[1]}, "
                          f"deficit_delta={delta}, uncertainty={uncertainty}")
                if complete[0] and delta > uncertainty:
                    winner = "drain_then_abort"
                elif complete[1] and post[1] - post[0] > uncertainty and (
                        complete[0] or delta < -uncertainty):
                    winner = "abort_then_drain"
                else:
                    return None, detail + "; no complete, distinguishable tail strategy"
                winners.add(winner)
                details.append(detail)
    if len(winners) != 1:
        return None, "; ".join(details) + "; tail strategy differs across comparisons"
    return winners.pop(), "; ".join(details)


def gate_status(rows):
    """Section 5: no single failed probe or simulator run decides the branch."""
    rows = [r for r in rows if str(r.get("demo")).lower() == "false"]
    # Do not combine different units into a hardware gate.
    if len({r["idn"] for r in rows}) != 1:
        return "INCONCLUSIVE"
    for check in ("firstread", "format", "maxrec"):
        needed = {v for v, _ in trials(check, 3600, False)}
        observed = {r["variant"] for r in rows if r["check"] == check and
                    int(r["empty_backlog"]) > 0 and not r["errors"]}
        if not needed <= observed:
            break
    else:
        return "FAIL"
    required = {(link, profile) for link in ("USB", "LAN") for profile in ("single", "four_vi")}
    good = {(r["transport"], r["profile"]) for r in rows if r["check"] == "long" and
            r["status"] == "PASS" and float(r["elapsed_s"]) >= 3600}
    stall = any(r["check"] == "stall" and r["variant"] == "5" and r["status"] == "PASS" for r in rows)
    tail, _ = tail_choice(rows)
    return "PASS" if required <= good and stall and tail is not None else "INCONCLUSIVE"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=(*CHECKS, "all"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--resource", help="one USB or TCPIP VISA address")
    mode.add_argument("--demo", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("hw_elog_results"))
    parser.add_argument("--yes", action="store_true", help="authorise CH1 ARB 0..1 V, 0.1 A")
    parser.add_argument("--channel", type=int, choices=range(1, 5), default=1, help="channel for single-channel probes and the ARB probe")
    parser.add_argument("--seconds", type=float, default=3600, help="long duration per profile")
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be positive and finite")
    if args.check in ("elog_arb", "all") and not args.yes:
        parser.error("elog_arb enables CH1 output: --yes is required (also in demo)")
    global CH
    CH = args.channel
    args.out.mkdir(parents=True, exist_ok=True)
    group = (InstrumentGroup.demo(frames=1, slots=execute.DEMO_SLOTS) if args.demo
             else execute.connect_group([args.resource]))
    link = "DEMO" if args.demo else ("USB" if args.resource.upper().startswith("USB")
                                    else "LAN" if args.resource.upper().startswith("TCPIP") else "OTHER")
    try:
        inst = group.instruments[0]
        if any(group.output_state(group.populated_channels).values()):
            raise RuntimeError("turn outputs off before H0 probes")
        idn = inst.session.query("*IDN?")
        if args.demo:
            print("DEMO: accelerated waits, short long run; all hardware conclusions INCONCLUSIVE")
        for check in CHECKS if args.check == "all" else [args.check]:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = args.out / f"{check}_{link}_{stamp}.csv"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, COLUMNS)
                writer.writeheader()
                for variant, cfg in trials(check, args.seconds, args.demo):
                    run_trial(inst, check, variant, cfg, writer,
                              demo=args.demo, transport=link, idn=idn)
                    stream.flush()
            print(f"{check}: CSV {path}")
    finally:
        group.close()
    rows = []
    for path in args.out.glob("*.csv"):
        with path.open(newline="", encoding="utf-8") as stream:
            rows.extend(dict(r, _source=str(path)) for r in csv.DictReader(stream)
                        if r.get("kind") == "summary")
    tail, detail = tail_choice(rows)
    print(f"ELOG_TAIL={tail or 'INCONCLUSIVE'} (C11 recommendation; {detail})")
    print(f"H0: {gate_status(rows)} (section 5; same-unit USB + LAN evidence required)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
