"""``n6705c-seq check|run PLAN.csv``."""

from __future__ import annotations

import argparse
import sys

from n6705c.core.group import (
    CHANNELS_PER_FRAME,
    TRIGGER_SKEW_SECONDS,
    GlobalChannel,
    InstrumentGroup,
)

from .e36441a import detect_model, kind_of, run_resources
from .execute import DEMO_SLOTS, connect_group, OutputsNotConfirmedOff, PlanRejected, run_plan
from .plan import load_csv, order_warnings, suggest_assignment, timeline, write_csv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="n6705c-seq", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate the table and print the timeline; touches nothing")
    check.add_argument("plan")
    check.add_argument(
        "--suggest-out", metavar="FILE", help="write the table re-assigned to avoid cross-frame ties"
    )

    run = sub.add_parser("run", help="switch outputs on and run the sequence")
    run.add_argument("plan")
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--resource", action="append", help="VISA resource, once per mainframe, in frame order"
    )
    target.add_argument("--demo", action="store_true", help="use the built-in simulator")
    run.add_argument("--backend", default="auto")
    run.add_argument("--timeout", type=int, default=10_000, help="I/O timeout in ms")
    run.add_argument(
        "--hold-dc", action="store_true",
        help="ignore hold/fall: stop at the high level and leave the outputs ON",
    )
    run.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser


def _report(rows, suggest_out: str | None = None) -> int:
    """Print the timeline and any cross-frame ties, with a fix when one exists."""
    print(timeline(rows))
    warnings = order_warnings(rows, TRIGGER_SKEW_SECONDS)
    for warning in warnings:
        print(f"WARNING: {warning}")
    if not warnings:
        return _write(rows, suggest_out, "No cross-frame ties; wrote the table unchanged")
    frames = -(-max(r.channel for r in rows if r.enabled) // CHANNELS_PER_FRAME)
    result = suggest_assignment(rows, TRIGGER_SKEW_SECONDS, frames)
    if result is None:
        print(
            f"No frame assignment avoids these ties on {frames} mainframe(s): a group of "
            f"channels starting within {TRIGGER_SKEW_SECONDS * 1000:g} ms of each other "
            f"is larger than {CHANNELS_PER_FRAME}, or the groups do not fit."
        )
        return 1 if suggest_out else 0
    new_rows, moves = result
    print(
        f"Suggested assignment (no pair on different frames closer than "
        f"{TRIGGER_SKEW_SECONDS * 1000:g} ms):"
    )
    for row, channel in moves:
        print(f"  {row.name}: {row.label} -> {GlobalChannel.from_index(channel).label()}")
    if suggest_out:
        return _write(new_rows, suggest_out, "Rewire the rails to match, then run that file")
    print("Add --suggest-out FILE to write the adjusted table.")
    return 0


def _write(rows, path: str | None, note: str) -> int:
    if not path:
        return 0
    try:
        write_csv(rows, path)
    except OSError as exc:
        print(f"error: cannot write {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {path}. {note}.")
    return 0


def _group(args, rows) -> InstrumentGroup:
    if args.demo:
        frames = -(-max(r.channel for r in rows if r.enabled) // 4)
        return InstrumentGroup.demo(frames=frames, slots=DEMO_SLOTS)
    return connect_group(args.resource, backend=args.backend, timeout_ms=args.timeout)


def _run_e36441a(args, rows) -> int:
    try:
        run_resources(args.resource, rows, hold_dc=args.hold_dc, backend=args.backend, timeout_ms=args.timeout)
    except Exception as exc:  # noqa: BLE001 - run_rows already restored what it touched
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Sequence done; outputs left ON at the high level." if args.hold_dc else "Sequence done; outputs off.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        rows = load_csv(args.plan)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not any(r.enabled for r in rows):
        print("error: no enabled channels in the table", file=sys.stderr)
        return 2

    status = _report(rows, getattr(args, "suggest_out", None))
    if args.command == "check":
        return status

    if not args.yes and input("Switch outputs ON and run? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted; nothing was switched on")
        return 1
    if not args.demo:
        try:
            models = {r: detect_model(r, backend=args.backend, timeout_ms=args.timeout) for r in args.resource}
        except Exception as exc:  # noqa: BLE001
            print(f"connection failed: {exc}", file=sys.stderr)
            return 1
        try:
            kind = kind_of(list(models.values()))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if kind == "E36441A":
            return _run_e36441a(args, rows)
    try:
        group = _group(args, rows)
    except Exception as exc:  # noqa: BLE001 - connection errors vary by backend
        print(f"connection failed: {exc}", file=sys.stderr)
        return 1
    try:
        run_plan(group, rows, hold_dc=args.hold_dc)
    except KeyboardInterrupt:
        print("interrupted; outputs forced off", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - run_plan already forced outputs off
        forced = (
            "" if isinstance(exc, OutputsNotConfirmedOff)
            else "; rejected before any output was switched" if isinstance(exc, PlanRejected)
            else "; outputs forced off"
        )
        print(f"error: {exc}{forced}", file=sys.stderr)
        return 1
    finally:
        for instrument in group.instruments:
            instrument.close()
    print("Sequence done; outputs left ON at the high level." if args.hold_dc else "Sequence done; outputs off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
