"""H0 plumbing and conservative acceptance, with no hardware connections."""
import csv
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "hw_elog_h0", Path(__file__).resolve().parents[1] / "scripts" / "hw_elog_h0.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)


@pytest.fixture
def demo(monkeypatch):
    group = hw.InstrumentGroup.demo(frames=1, slots=hw.execute.DEMO_SLOTS)
    monkeypatch.setattr(hw.InstrumentGroup, "demo", lambda **kwargs: group)
    monkeypatch.setattr(group, "close", lambda: None)
    monkeypatch.setattr(hw.execute, "connect_group", lambda *a, **k: pytest.fail("no hardware"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    yield group, group.instruments[0].session._transport
    for inst in group.instruments:
        inst.close()


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize("check", hw.CHECKS)
def test_demo_each_check_csv_and_command_boundary(check, tmp_path, demo, capsys):
    group, transport = demo
    args = [check, "--demo", "--out", str(tmp_path)]
    if check == "elog_arb":
        args += ["--yes"]
    assert hw.main(args) == 0
    paths = list(tmp_path.glob(f"{check}_*.csv"))
    assert len(paths) == 1
    rows = read_rows(paths[0])
    summaries = [r for r in rows if r["kind"] == "summary"]
    assert len(summaries) == len(hw.trials(check, 3600, True))
    assert all(r["status"] == "INCONCLUSIVE" for r in summaries)
    assert all(not r["errors"] for r in summaries)
    assert all(float(r["period_s"]) > 0 for r in summaries)
    assert any(int(r["records"] or 0) > 0 for r in rows)
    commands = transport.command_log  # unbounded; Session.history truncates long runs
    assert not any(c.upper().startswith("*RST") for c in commands)
    if check != "elog_arb":
        assert not any(c.upper().startswith("OUTP ON") for c in commands)
        assert not any(c.upper().startswith("INIT:TRAN") for c in commands)
    else:
        assert "OUTP ON,(@1)" in commands
        assert "ABOR:TRAN (@1)" in commands
        assert "OUTP OFF,(@1)" in commands
    assert not any(group.output_state(group.populated_channels).values())
    assert "H0: INCONCLUSIVE" in capsys.readouterr().out


def test_demo_all(tmp_path, demo):
    assert hw.main(["all", "--demo", "--yes", "--out", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("*.csv"))) == len(hw.CHECKS)


@pytest.mark.parametrize("check", ["all", "elog_arb"])
def test_yes_required_before_connection(check, tmp_path, monkeypatch):
    monkeypatch.setattr(hw.InstrumentGroup, "demo", lambda **k: pytest.fail("must reject first"))
    with pytest.raises(SystemExit) as exc:
        hw.main([check, "--demo", "--out", str(tmp_path)])
    assert exc.value.code == 2


@pytest.mark.parametrize("check, variable", [
    ("firstread", "delay"), ("format", "fmt"), ("maxrec", "maxrecords"),
    ("stall", "delay"), ("tail", "tail"), ("elog_arb", "arb"),
])
def test_only_one_variable_per_comparison(check, variable):
    configs = [c for _, c in hw.trials(check, 3600, False)]
    baseline = configs[0]
    for config in configs[1:]:
        assert {key for key in baseline if baseline[key] != config[key]} == {variable}


def test_long_profiles_and_real_duration():
    configs = [c for _, c in hw.trials("long", 3600, False)]
    assert [c["channels"] for c in configs] == [[1], [1, 2, 3, 4]]
    assert [c["period"] for c in configs] == [0.001, 0.0008192]
    assert all(c["duration"] >= 3600 for c in configs)


def test_ascii_really_sent_and_maxrecord_requests(tmp_path, demo):
    _, transport = demo
    hw.main(["format", "--demo", "--out", str(tmp_path)])
    assert "FORM ASC" in transport.command_log
    assert [c for c in transport.command_log if c.startswith("FORM ")][-1] == "FORM REAL"
    rows = read_rows(next(tmp_path.glob("format_*.csv")))
    assert any(r["format"] == "ASC" and int(r["records"] or 0) > 1000 for r in rows)
    hw.main(["maxrec", "--demo", "--out", str(tmp_path)])
    assert any(c.startswith("FETC:ELOG? 8192,") for c in transport.command_log)
    assert any(c.startswith("FETC:ELOG? 16384,") for c in transport.command_log)


def test_tail_probe_uses_core_fetch_after_abort(tmp_path, demo):
    hw.main(["tail", "--demo", "--out", str(tmp_path)])
    rows = read_rows(next(tmp_path.glob("tail_*.csv")))
    pre = [r for r in rows if r["phase"] == "before_abort"]
    post = [r for r in rows if r["phase"] == "after_abort"]
    assert any(int(r["records"]) > 0 for r in pre)
    assert {r["variant"] for r in post} == {"drain_then_abort", "abort_then_drain"}
    # Current fake discards its buffer on ABOR. Do not teach it new behaviour.
    assert all(int(r["records"]) == 0 for r in post)
    for summary in (r for r in rows if r["kind"] == "summary"):
        counts = [int(r["records"]) for r in post if r["variant"] == summary["variant"]]
        assert int(summary["after_abort_records"]) == sum(counts)
        assert int(summary["record_deficit"]) == (
            int(summary["records_produced"]) - int(summary["records_received"]))


def test_tail_summary_counts_multiple_post_abort_batches(demo, monkeypatch):
    group, transport = demo
    original_write, original_query = transport.write, transport.query_binary
    aborted = False
    batches = iter((2, 3, 0))

    def write(command):
        nonlocal aborted
        original_write(command)
        if command.startswith("ABOR:ELOG"):
            aborted = True

    def query(command):
        if aborted and command.startswith("FETC:ELOG?"):
            # Inject retained wire batches locally, without changing core/fake.
            payload = hw.np.arange(2 * next(batches), dtype="<f4").tobytes()
            size = str(len(payload)).encode("ascii")
            return b"#" + str(len(size)).encode("ascii") + size + payload
        return original_query(command)

    monkeypatch.setattr(transport, "write", write)
    monkeypatch.setattr(transport, "query_binary", query)
    output = io.StringIO()
    writer = csv.DictWriter(output, hw.COLUMNS)
    writer.writeheader()
    variant, cfg = hw.trials("tail", 3600, True)[1]
    result = hw.run_trial(group.instruments[0], "tail", variant, cfg, writer,
                          demo=True, transport="DEMO", idn="fake")
    rows = list(csv.DictReader(io.StringIO(output.getvalue())))
    assert [int(r["records"]) for r in rows if r["phase"] == "after_abort"] == [2, 3, 0]
    assert result["after_abort_records"] == 5
    assert not result["errors"]


def test_arb_error_cleanup_independent_even_if_abort_fails(tmp_path, demo, monkeypatch):
    group, transport = demo
    original = transport.write

    def fail(command):
        if command.startswith("INIT:TRAN") or command.startswith("ABOR:TRAN"):
            raise RuntimeError("injected transient failure")
        return original(command)

    monkeypatch.setattr(transport, "write", fail)
    hw.main(["elog_arb", "--demo", "--yes", "--out", str(tmp_path)])
    assert not any(group.output_state([1]).values())
    rows = read_rows(next(tmp_path.glob("elog_arb*.csv")))
    assert "injected transient failure" in rows[-1]["errors"]
    assert "OUTP OFF,(@1)" in transport.command_log


def test_arb_waiting_is_not_simultaneous_running(tmp_path, demo, monkeypatch):
    group, transport = demo
    monkeypatch.setattr(group.instruments[0], "operation_status", lambda ch: {
        1: hw.OperationStatus.TRAN_ACTIVE | hw.OperationStatus.WTG_TRAN})
    hw.main(["elog_arb", "--demo", "--yes", "--out", str(tmp_path)])
    rows = read_rows(next(tmp_path.glob("elog_arb*.csv")))
    assert "ARB not running alongside ELOG" in rows[-1]["errors"]
    assert "ABOR:ELOG (@1)" in transport.command_log
    assert "ABOR:TRAN (@1)" in transport.command_log
    assert not any(group.output_state([1]).values())


@pytest.mark.parametrize("payload", [b"#10\n", b"broken block"])
def test_empty_backlog_and_parser_errors_preserved(payload, demo, monkeypatch):
    group, transport = demo
    original = transport.query_binary
    monkeypatch.setattr(transport, "query_binary", lambda c: payload if c.startswith("FETC:ELOG?") else original(c))
    output = io.StringIO()
    writer = csv.DictWriter(output, hw.COLUMNS)
    writer.writeheader()
    variant, cfg = hw.trials("firstread", 3600, True)[1]
    result = hw.run_trial(group.instruments[0], "firstread", variant, cfg, writer,
                          demo=True, transport="DEMO", idn="fake")
    rows = list(csv.DictReader(io.StringIO(output.getvalue())))
    assert result["empty_backlog"] > 0
    if payload == b"broken block":
        assert "expected '#'" in result["errors"]
    else:
        assert any(r["wire_bytes"] == "4" and r["empty_blocks"] == "1" for r in rows)


@pytest.mark.parametrize("code, command, fatal", [
    (310, "SENS:ELOG:VOLT:RANG:AUTO ON,(@1)", False),
    (323, "INIT:ELOG (@1)", True),
    (310, "FORM REAL", True),
])
def test_positive_errors_drained_and_autorange_not_fatal(code, command, fatal, demo):
    group, transport = demo
    transport.errors.append((code, "injected"))
    adapter = hw.ProbeIO(group.instruments[0].session)
    if fatal:
        with pytest.raises(RuntimeError, match=str(code)):
            adapter.write(command)
        assert adapter.errors
    else:
        adapter.write(command)
        assert adapter.notes and not adapter.errors
    assert not transport.errors


def good_row(**changes):
    return dict(dict(check="long", variant="single", profile="single", transport="USB",
                     demo=False, idn="unit1", status="PASS", elapsed_s=3600,
                     period_s=0.001, records_produced=3600000,
                     records_received=3600000, empty_backlog=0,
                     startup_delay_s=0.0, abort_delay_s=0.0, after_abort_records=0,
                     overflow_estimated=False, errors=""), **changes)


def tail_pair(*, preserves=False, **changes):
    baseline = good_row(check="tail", elapsed_s=2.25,
                        records_produced=2250, records_received=2250,
                        startup_delay_s=0.004, abort_delay_s=0.006, **changes)
    return [dict(baseline, variant="drain_then_abort"),
            dict(baseline, variant="abort_then_drain",
                 records_received=2250 if preserves else 2000,
                 after_abort_records=250 if preserves else 0)]


def test_gate_needs_all_four_long_runs_stall_and_tail():
    rows = [good_row(transport=link, profile=profile) for link in ("USB", "LAN")
            for profile in ("single", "four_vi")]
    rows += [good_row(check="stall", variant="5"), *tail_pair()]
    assert hw.gate_status(rows) == "PASS"
    for i in range(len(rows)):
        assert hw.gate_status(rows[:i] + rows[i + 1:]) == "INCONCLUSIVE"
    assert hw.gate_status([dict(r, demo=True) for r in rows]) == "INCONCLUSIVE"
    assert hw.gate_status(rows + [good_row(idn="unit2")]) == "INCONCLUSIVE"


def test_gate_fail_only_when_all_one_variable_alternatives_reproduce():
    rows = [good_row(check=check, variant=v, empty_backlog=1) for check in
            ("firstread", "format", "maxrec") for v, _ in hw.trials(check, 3600, False)]
    assert hw.gate_status(rows) == "FAIL"
    for i in range(len(rows)):
        assert hw.gate_status(rows[:i] + rows[i + 1:]) == "INCONCLUSIVE"


def test_local_acceptance_limits():
    assert hw.trial_status(good_row(elapsed_s=3599), "long", "single")[0] == "INCONCLUSIVE"
    # 250 records per poll + 180 records of 50 ppm drift + one boundary.
    assert hw.trial_status(good_row(records_received=3599568), "long", "single")[0] == "INCONCLUSIVE"
    assert hw.trial_status(good_row(records_received=3599569), "long", "single")[0] == "PASS"
    assert hw.trial_status(good_row(overflow_estimated=True), "long", "single")[0] == "INCONCLUSIVE"


@pytest.mark.parametrize("check,variant", [("stall", "5"), ("tail", "drain_then_abort")])
@pytest.mark.parametrize("deficit", [-280, -10, 10, 280])
def test_stall_tail_allow_poll_and_measured_edge_delays(check, variant, deficit):
    row = good_row(elapsed_s=5, records_produced=5000, records_received=5000 - deficit,
                   startup_delay_s=0.020, abort_delay_s=0.030)
    assert hw.trial_status(row, check, variant)[0] == "PASS"
    # The same error is too large without the measured TRIG/ABOR spans.
    if abs(deficit) == 280:
        row.update(startup_delay_s=0, abort_delay_s=0)
        assert hw.trial_status(row, check, variant)[0] == "INCONCLUSIVE"


@pytest.mark.parametrize("check,variant", [("stall", "5"), ("tail", "abort_then_drain")])
def test_stall_tail_do_not_hide_large_deficits_or_overflow(check, variant):
    row = good_row(elapsed_s=5, records_produced=5000, records_received=4600,
                   startup_delay_s=0.020, abort_delay_s=0.030)
    assert hw.trial_status(row, check, variant)[0] == "INCONCLUSIVE"
    assert hw.trial_status(good_row(overflow_estimated=True), check, variant)[0] == "INCONCLUSIVE"
    assert hw.trial_status(good_row(), "stall", "12")[0] == "INCONCLUSIVE"


def test_command_delays_measured_through_error_checks(demo, monkeypatch):
    group, _ = demo
    clock = [0.0]
    monkeypatch.setattr(hw, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = hw.ProbeIO.check_errors

    def delayed_checks(self, context=""):
        original(self, context)
        if context.startswith("TRIG:ELOG"):
            clock[0] += 0.004
        if context.startswith("ABOR:ELOG"):
            clock[0] += 0.006

    monkeypatch.setattr(hw.ProbeIO, "check_errors", delayed_checks)
    variant, cfg = hw.trials("stall", 3600, True)[0]
    result = hw.run_trial(group.instruments[0], "stall", variant, cfg,
                          csv.DictWriter(io.StringIO(), hw.COLUMNS),
                          demo=True, transport="DEMO", idn="fake")
    # Both ProbeIO.write and core fire() check TRIG errors before _t0 is set.
    assert result["startup_delay_s"] == pytest.approx(0.008)
    assert result["abort_delay_s"] == pytest.approx(0.006)
    assert result["accounting_tolerance"] >= 265


@pytest.mark.parametrize("failure", ["abort", "parse"])
def test_format_restored_even_on_failure(failure, tmp_path, demo, monkeypatch):
    _, transport = demo
    original_write = transport.write
    original_query = transport.query_binary

    def fail_abort(command):
        if command.startswith("ABOR:ELOG"):
            raise RuntimeError("injected abort failure")
        return original_write(command)

    if failure == "abort":
        monkeypatch.setattr(transport, "write", fail_abort)
    else:
        monkeypatch.setattr(transport, "query_binary", lambda c:
                            b"broken" if c.startswith("FETC:ELOG?") else original_query(c))
    hw.main(["format", "--demo", "--out", str(tmp_path)])
    summaries = [r for r in read_rows(next(tmp_path.glob("*.csv"))) if r["kind"] == "summary"]
    assert all(r["errors"] for r in summaries)
    assert [c for c in transport.command_log if c.startswith("FORM ")][-1] == "FORM REAL"


def test_overflow_uses_core_buffer_capacity(tmp_path, demo, monkeypatch):
    from n6705c.core.elog import ElogStats

    monkeypatch.setattr(ElogStats, "buffer_capacity", property(lambda self: 1000))
    hw.main(["stall", "--demo", "--out", str(tmp_path)])
    summaries = [r for r in read_rows(next(tmp_path.glob("*.csv"))) if r["kind"] == "summary"]
    assert summaries[0]["variant"] == "5"
    assert summaries[0]["overflow_estimated"] == "True"


@pytest.mark.parametrize("preserves,winner", [
    (False, "drain_then_abort"), (True, "abort_then_drain"),
])
def test_tail_choice_compares_deficits_and_post_abort_records(preserves, winner):
    rows = tail_pair(preserves=preserves)
    # A 250-record tail loss even fits local poll tolerance. Paired comparison
    # must still distinguish it from genuine post-ABOR tail retention.
    assert all(hw.trial_status(r, "tail", r["variant"])[0] == "PASS" for r in rows)
    choice, detail = hw.tail_choice(rows)
    assert choice == winner
    assert "after_abort drain/abort=" in detail
    assert "deficit_delta=" in detail


@pytest.mark.parametrize("change", [
    {"records_received": 2000, "after_abort_records": 250},  # data != complete tail
    {"records_received": 2250, "after_abort_records": 0},  # no unread tail evidence
    {"records_received": 2100, "abort_delay_s": 0.3},  # timing cannot distinguish loss
    {"errors": "SCPI error"}, {"empty_backlog": 1}, {"overflow_estimated": True},
    {"after_abort_records": ""}, {"startup_delay_s": ""},
    {"transport": "LAN"}, {"maxrecords": 16384}, {"_source": "another.csv"},
])
def test_tail_choice_requires_complete_matched_evidence(change):
    rows = tail_pair(preserves=True)
    if change.get("records_received") == 2000:
        rows[0]["records_received"] = 2000  # neither strategy complete
    rows[1].update(change)
    assert hw.tail_choice(rows)[0] is None
    long_rows = [good_row(transport=link, profile=profile) for link in ("USB", "LAN")
                 for profile in ("single", "four_vi")]
    assert hw.gate_status(long_rows + [good_row(check="stall", variant="5")] + rows) == "INCONCLUSIVE"


def test_tail_choice_rejects_demo_conflicts_and_unpaired_runs():
    assert hw.tail_choice([dict(r, demo=True) for r in tail_pair()])[0] is None
    assert hw.tail_choice(tail_pair()[:1])[0] is None
    assert hw.tail_choice(tail_pair() + tail_pair(preserves=True, transport="LAN"))[0] is None
    assert hw.tail_choice(tail_pair() + tail_pair(idn="unit2"))[0] is None
    old_rows = tail_pair()
    for row in old_rows:
        del row["after_abort_records"]
    assert hw.tail_choice(old_rows)[0] is None


@pytest.mark.parametrize("preserves,winner", [
    (False, "drain_then_abort"), (True, "abort_then_drain"),
])
def test_gate_prints_c11_tail_recommendation(preserves, winner, tmp_path, demo, capsys):
    evidence = [good_row(transport=link, profile=profile) for link in ("USB", "LAN")
                for profile in ("single", "four_vi")]
    evidence += [good_row(check="stall", variant="5"), *tail_pair(preserves=preserves)]
    with (tmp_path / "synthetic_hardware_evidence.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, hw.COLUMNS)
        writer.writeheader()
        writer.writerows(dict(row, kind="summary") for row in evidence)
    hw.main(["firstread", "--demo", "--out", str(tmp_path)])
    output = capsys.readouterr().out
    assert f"ELOG_TAIL={winner} (C11 recommendation;" in output
    assert "H0: PASS" in output


def test_refuse_enabled_outputs_without_changing_them(tmp_path, demo):
    group, transport = demo
    group.set_output([1], True)
    before = len(transport.command_log)
    with pytest.raises(RuntimeError, match="turn outputs off"):
        hw.main(["firstread", "--demo", "--out", str(tmp_path)])
    assert not any(c.startswith(("INIT:", "ABOR:")) for c in transport.command_log[before:])
    group.set_output([1], False)
