"""ARB hardware-script plumbing with a self-contained, strict transport stub.

These tests model alternative hardware behaviors; none is hardware evidence.
No import from n6705c.core.fake and no VISA connection is used.
"""
import csv
import importlib.util
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location(
    "hw_arb_trig_check", Path(__file__).resolve().parents[1] / "scripts" / "hw_arb_trig_check.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


class ArbTransportStub:
    description = "self-contained ARB test transport"

    def __init__(self, clock, *, seq_base=0, ignore_arb_source=False, release_bus=True):
        self.clock = clock
        self.seq_base = seq_base
        self.ignore_arb_source = ignore_arb_source
        self.release_bus = release_bus
        self.calls = []
        self.errors = []
        self.output = dict.fromkeys(range(1, 5), False)
        self.arb_sources = dict.fromkeys(range(1, 5), "EXT")
        self.tran_source = "BUS"
        self.seq_len = 0
        self.slew = 2.0
        self.maximum = 100.0
        self.max_override = 0
        self.reset_override = 0
        self.reset_slew = self.maximum
        self.closed = False
        self.capture_origin = None
        self.wave_origin = None
        self.pending = False
        self.points = hw.execute.CAPTURE_POINTS
        self.tint = hw.execute.CAPTURE_STEP
        self.fail_at = None
        self.failure = RuntimeError("injected transport failure")
        self.fail_once = True
        self.error_at = {}
        self.bad_capture = None
        self.resets = 0

    @property
    def arb_source(self):
        return self.arb_sources[1]

    @arb_source.setter
    def arb_source(self, value):
        self.arb_sources[1] = value

    def exchange(self, command):
        assert not self.closed, "I/O after close"
        self.calls.append((self.clock.now, command))
        if command == self.fail_at:
            if self.fail_once:
                self.fail_at = None
            raise self.failure
        if command in self.error_at:
            self.errors.extend(self.error_at[command])
            return False
        return True

    @staticmethod
    def channel(command):
        return int(command.split("(@")[1].split(")")[0])

    def write(self, command):
        if not self.exchange(command):
            return
        argument = command.split(" ", 1)[1].split(",(@")[0] if " " in command else ""
        if command.startswith("TRIG:ARB:SOUR "):
            self.arb_sources[self.channel(command)] = argument
        elif command.startswith("TRIG:TRAN:SOUR "):
            self.tran_source = argument
        elif command.startswith("OUTP "):
            self.output[self.channel(command)] = argument == "ON"
        elif command.startswith("ABOR:TRAN "):
            self.pending, self.wave_origin = False, None
        elif command.startswith("ABOR:ACQ "):
            self.capture_origin = None
        elif command.startswith("SENS:SWE:TINT "):
            self.tint = float(argument)
        elif command.startswith("SENS:SWE:POIN "):
            self.points = int(argument)
        elif command.startswith("INIT:IMM:ACQ "):
            self.capture_origin = None
        elif command.startswith("TRIG:ACQ "):
            if self.capture_origin is None:
                self.capture_origin = self.clock.now
        elif command.startswith("INIT:TRAN "):
            assert self.output[self.channel(command)], "INIT before output on"
            if self.arb_sources[self.channel(command)] == "IMM" or self.ignore_arb_source:
                self.wave_origin = self.clock.now
            else:
                self.pending = True
        elif command == "*TRG":
            if self.pending and self.release_bus:
                self.wave_origin = self.clock.now
                self.pending = False
            # An already-running acquisition must not be restarted by *TRG.
            if self.capture_origin is None:
                self.capture_origin = self.clock.now
        elif command.startswith("ARB:SEQ:RES "):
            self.seq_len = 0
        elif command.startswith("ARB:SEQ:STEP:FUNC:SHAP "):
            _, index = argument.split(",")
            index = int(index)
            if not self.seq_base <= index < self.seq_base + 100:
                self.errors.append('-222,"Step out of range"')
            else:
                self.seq_len = index - self.seq_base + 1
        elif command == "*RST":
            self.resets += 1
            self.slew = self.reset_slew
            self.max_override = self.reset_override
            self.arb_sources = dict.fromkeys(range(1, 5), "IMM")
            self.tran_source = "BUS"
            self.output = dict.fromkeys(range(1, 5), False)
        elif command in ("FORM REAL", "FORM:BORD SWAP") or command.startswith((
            "VOLT:MODE ", "VOLT ", "CURR ", "ARB:FUNC:TYPE ", "ARB:FUNC:SHAP ",
            "ARB:VOLT:CDW ", "ARB:VOLT:CDW:DWEL ", "ARB:COUN ", "ARB:TERM:LAST ",
            "SENS:FUNC:VOLT ", "SENS:SWE:OFFS:POIN ", "TRIG:ACQ:SOUR ",
        )):
            pass
        else:
            raise AssertionError(f"unexpected write: {command}")

    def query(self, command):
        if not self.exchange(command):
            return ""
        if command == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if command == "*IDN?":
            return "Keysight,N6705C,TEST,F.1.1.0"
        if command == "SYST:CHAN:COUN?":
            return "4"
        for prefix, value in (("SYST:CHAN:MOD?", "N6752A"), ("SYST:CHAN:SER?", "STUB"),
                              ("SYST:CHAN:OPT?", ""), ("VOLT:RANG? MAX", "50"),
                              ("CURR:RANG? MAX", "10")):
            if command.startswith(prefix):
                return value
        if command.startswith("OUTP?"):
            return str(int(self.output[self.channel(command)]))
        if command.startswith("TRIG:ARB:SOUR?"):
            return self.arb_sources[self.channel(command)]
        if command.startswith("TRIG:TRAN:SOUR?"):
            return self.tran_source
        if command.startswith("ARB:SEQ:LEN?"):
            return str(self.seq_len)
        if command.startswith("VOLT:SLEW:MAX?"):
            return str(self.max_override)
        if command.startswith("VOLT:SLEW? MAX,"):
            return str(self.maximum)
        if command.startswith("VOLT:SLEW?"):
            return str(self.slew)
        if command == "*OPC?":
            return "1"
        if command.startswith("SENS:SWE:TINT?"):
            return str(self.tint)
        if command.startswith("STAT:OPER:COND?"):
            if self.pending:
                return "80"  # WTG_TRAN | TRAN_ACTIVE
            if self.wave_origin is not None and self.clock.now < self.wave_origin + hw.DURATION:
                return "64"
            return "1"
        raise AssertionError(f"unexpected query: {command}")

    def query_binary(self, command):
        self.exchange(command)
        assert command.startswith("FETC:ARR:VOLT? ")
        assert self.capture_origin is not None
        assert self.clock.now >= self.capture_origin + self.tint * self.points
        t = self.capture_origin + np.arange(self.points) * self.tint
        volts = np.zeros(self.points)
        if self.wave_origin is not None:
            volts[(t >= self.wave_origin + hw.LOW_TIME) &
                  (t < self.wave_origin + 2 * hw.LOW_TIME)] = 1
        if self.bad_capture == "short":
            volts = volts[:-1]
        elif self.bad_capture == "nan":
            volts[0] = np.nan
        elif self.bad_capture == "flat":
            volts[:] = 0
        payload = volts.astype("<f4").tobytes()
        length = str(len(payload)).encode("ascii")
        return b"#" + str(len(length)).encode("ascii") + length + payload

    def close(self):
        self.closed = True


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(hw.time, "sleep", clock.sleep)
    monkeypatch.setattr(hw.time, "monotonic", clock.monotonic)
    transport = ArbTransportStub(clock)
    group = hw.InstrumentGroup.from_transports([transport])
    monkeypatch.setattr(hw.execute, "connect_group", lambda resources: group)
    monkeypatch.setattr(hw.InstrumentGroup, "demo", lambda **kwargs: group)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("--yes must not prompt"))
    transport.calls.clear()
    return transport, group


def run(name, tmp_path, *, extra=()):
    return hw.main([name, "--resource", "STUB", "--yes", "--out", str(tmp_path), *extra])


def rows(tmp_path, name):
    with (tmp_path / f"{name}.csv").open(encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def commands(transport):
    return [command for _, command in transport.calls]


@pytest.mark.parametrize("name", hw.CHECKS)
def test_each_check_with_standalone_transport(name, rig, tmp_path):
    transport, _ = rig
    assert run(name, tmp_path, extra=["--allow-reset"] if name == "slew" else []) == 0
    assert {r["status"] for r in rows(tmp_path, name)} == {"PASS"}
    assert rows(tmp_path, name + "_commands")
    sent = commands(transport)
    assert transport.closed and not any(transport.output.values())
    final_cleanup = sent[max(i for i, c in enumerate(sent) if c == "ABOR:TRAN (@1)"):]
    assert final_cleanup[:8] == ["ABOR:TRAN (@1)", "SYST:ERR?", "ABOR:ACQ (@1)",
                                "SYST:ERR?", "OUTP OFF,(@1)", "SYST:ERR?", "OUTP? (@1)", "SYST:ERR?"]
    if name in ("arbsrc", "bus_delay", "residual_bus"):
        assert final_cleanup[8:] == ["TRIG:ARB:SOUR EXT,(@1)", "SYST:ERR?",
                                     "TRIG:ARB:SOUR? (@1)", "SYST:ERR?"]
        assert transport.arb_source == "EXT"
    else:
        assert final_cleanup[8:] == []
    assert transport.resets == int(name == "slew")
    assert ("OUTP ON,(@1)" in sent) == (name in ("bus_delay", "residual_bus"))
    assert not any(c.startswith(("ABOR:ELOG", "INIT:ELOG", "DIG:", "SYST:LOC", "*CLS")) for c in sent)


def test_all_outputs_csv_and_resets_only_once_at_slew(rig, tmp_path):
    transport, _ = rig
    assert run("all", tmp_path, extra=["--allow-reset"]) == 0
    assert all((tmp_path / f"{name}.csv").is_file() for name in hw.CHECKS)
    assert transport.resets == 1
    sent = commands(transport)
    reset = sent.index("*RST")
    assert "OUTP ON,(@1)" not in sent[reset:]
    assert not any(transport.output.values())


def test_arbsrc_records_as_found_and_each_source_readback(rig, tmp_path):
    transport, _ = rig
    assert run("arbsrc", tmp_path) == 0
    data = rows(tmp_path, "arbsrc")
    assert [(r["phase"], r["source"]) for r in data if r["phase"] != "identity"] == [
        ("as_found", "EXT"), ("IMM", "IMM"), ("BUS", "BUS")]
    assert transport.resets == 0
    assert "NOT a reset-default" in data[0]["detail"]


def test_bus_delay_capture_starts_before_init_and_waits_two_seconds(rig, tmp_path):
    transport, _ = rig
    assert run("bus_delay", tmp_path) == 0
    events = [r for r in rows(tmp_path, "bus_delay") if r["phase"] in ("IMM", "BUS")]
    assert [r["timing"] for r in events] == ["at_init", "at_bus_trigger"]
    for event in events:
        assert float(event["bus_trigger_s"]) - float(event["init_s"]) == pytest.approx(2)
    sent = commands(transport)
    initiations = [i for i, c in enumerate(sent) if c == "INIT:TRAN (@1)"]
    for index in initiations:
        assert sent[:index].count("TRIG:ACQ (@1)") == initiations.index(index) + 1
        assert sent[:index].count("*TRG") == initiations.index(index)
    for source in ("imm", "bus"):
        samples = rows(tmp_path, f"bus_delay_{source}_samples")
        assert len(samples) == hw.execute.CAPTURE_POINTS
        assert max(float(r["voltage_V"]) for r in samples) == 1


def test_bus_delay_reports_hypothesis_contradiction(rig, tmp_path):
    transport, _ = rig
    transport.ignore_arb_source = True
    assert run("bus_delay", tmp_path) == 1
    assert rows(tmp_path, "bus_delay")[0]["status"] == "FAIL"


def test_residual_bus_uses_actual_start_together_in_both_modes(rig, tmp_path, monkeypatch):
    transport, _ = rig
    original = hw.execute.start_together
    flags = []

    def start(group, functions, trigger_acquisition=False):
        flags.append(trigger_acquisition)
        assert transport.arb_source == "BUS"
        original(group, functions, trigger_acquisition)

    monkeypatch.setattr(hw.execute, "start_together", start)
    assert run("residual_bus", tmp_path) == 0
    assert flags == [False, True]
    data = [r for r in rows(tmp_path, "residual_bus") if r["phase"].endswith("capture_trigger")]
    assert [r["timed_out"] for r in data] == ["True", "False"]
    assert [r["observation"] for r in data] == ["released_by_late_bus", "waveform_observed"]


def test_residual_bus_without_waveform_is_not_false_success(rig, tmp_path):
    transport, _ = rig
    transport.release_bus = False
    assert run("residual_bus", tmp_path) == 1
    data = [r for r in rows(tmp_path, "residual_bus") if r["phase"].endswith("capture_trigger")]
    assert [r["timed_out"] for r in data] == ["True", "True"]
    assert {r["observation"] for r in data} == {"no_waveform_observed"}


def test_residual_bus_detects_future_start_together_change(rig, tmp_path, monkeypatch):
    transport, _ = rig
    original = hw.execute.start_together

    def patched(group, functions, trigger_acquisition=False):
        group.instruments[0].session.write_checked("TRIG:ARB:SOUR IMM,(@1)")
        original(group, functions, trigger_acquisition)

    monkeypatch.setattr(hw.execute, "start_together", patched)
    assert run("residual_bus", tmp_path) == 1
    assert "case no longer applies" in rows(tmp_path, "residual_bus")[0]["detail"]
    assert not any(transport.output.values())


@pytest.mark.parametrize("base, lengths", [(0, [1, 2]), (1, [0, 1])])
def test_seqnum_resets_each_probe_and_reports_numbering_only(base, lengths, rig, tmp_path):
    transport, _ = rig
    transport.seq_base = base
    assert run("seqnum", tmp_path) == 0
    data = [r for r in rows(tmp_path, "seqnum") if r["phase"] != "identity"]
    assert [int(r["length"]) for r in data] == lengths
    assert [int(r["baseline_len"]) for r in data] == [0, 0]
    assert commands(transport).count("ARB:SEQ:RES (@1)") == 2
    assert "endpoints 99/100 NOT tested" in data[0]["detail"]


@pytest.mark.parametrize("reset_slew", [100, 2])
@pytest.mark.parametrize("reset_override", [0, 1])
def test_slew_reads_three_values_without_comparing_rate_with_boolean(reset_slew, reset_override, rig, tmp_path):
    transport, _ = rig
    transport.reset_slew = reset_slew
    transport.reset_override = reset_override
    assert run("slew", tmp_path, extra=["--allow-reset"]) == 0
    sent = commands(transport)
    index = sent.index("*RST")
    for command in ("VOLT:SLEW? (@1)", "VOLT:SLEW:MAX? (@1)", "VOLT:SLEW? MAX,(@1)"):
        assert sent[:index].count(command) == sent[index:].count(command) == 1
    data = [r for r in rows(tmp_path, "slew") if r["phase"] != "identity"]
    assert [r["phase"] for r in data] == ["before_reset", "after_reset"]
    assert {r["status"] for r in data} == {"PASS"}
    assert float(data[-1]["max_override"]) == reset_override
    assert float(data[-1]["slew"]) == reset_slew
    assert {float(r["maximum"]) for r in data} == {transport.maximum}
    assert {r["maximum_errors"] for r in data} == {""}
    assert "NOT a numeric maximum" in data[0]["detail"]


@pytest.mark.parametrize("rejected,raw_reply", [
    (False, "  +1.00000E+02\n"), (True, ""), (True, "  +1.00000E+02\n"),
])
def test_slew_maximum_probe_preserves_raw_reply_and_errors_without_failing(rejected, raw_reply, rig, tmp_path, monkeypatch):
    transport, _ = rig
    command = "VOLT:SLEW? MAX,(@1)"
    original = transport.query
    errors = ['-113,"Undefined header"', '-222,"Data out of range"'] if rejected else []

    def query(request):
        if request == command:
            transport.exchange(request)
            transport.errors.extend(errors)
            return raw_reply
        return original(request)

    monkeypatch.setattr(transport, "query", query)
    assert run("slew", tmp_path, extra=["--allow-reset"]) == 0
    data = [r for r in rows(tmp_path, "slew") if r["phase"] != "identity"]
    evidence = [r for r in rows(tmp_path, "slew_commands") if r["command"] == command]
    assert [r["phase"] for r in data] == ["before_reset", "after_reset"]
    assert len(evidence) == 2
    for result in data:
        assert result["status"] == "PASS"
        assert result["maximum"] == raw_reply
        assert result["maximum_errors"] == "; ".join(errors)
    for result in evidence:
        assert result["reply"] == raw_reply
        assert result["errors"] == "; ".join(errors)
    assert transport.resets == 1 and not transport.errors
    assert not any(c.startswith("OUTP ON") for c in commands(transport))


def test_slew_rejects_a_numeric_rate_as_boolean_override(rig, tmp_path):
    transport, _ = rig
    transport.reset_override = 100
    assert run("slew", tmp_path, extra=["--allow-reset"]) == 1
    assert rows(tmp_path, "slew")[0]["status"] == "INCONCLUSIVE"


@pytest.mark.parametrize("code", [310, 323, -113])
def test_signed_errors_are_drained_and_preserved(code, rig, tmp_path):
    transport, _ = rig
    command = "TRIG:ARB:SOUR IMM,(@1)"
    transport.error_at[command] = [f'{code},"first"', '-222,"second"']
    assert run("arbsrc", tmp_path) == 1
    evidence = next(r for r in rows(tmp_path, "arbsrc_commands") if r["command"] == command)
    assert f"{code:+d}" in evidence["errors"] and "-222" in evidence["errors"]
    sent = commands(transport)
    index = sent.index(command)
    assert sent[index + 1:index + 4] == ["SYST:ERR?"] * 3
    assert not transport.errors


@pytest.mark.parametrize("command", ["VOLT:SLEW? (@1)", "VOLT:SLEW:MAX? (@1)"])
def test_unsupported_slew_still_reads_both_sides_without_output(command, rig, tmp_path):
    transport, _ = rig
    transport.error_at[command] = ['-113,"Undefined header"']
    assert run("slew", tmp_path, extra=["--allow-reset"]) == 1
    assert rows(tmp_path, "slew")[0]["status"] == "INCONCLUSIVE"
    assert transport.resets == 1
    assert not any(c.startswith("OUTP ON") for c in commands(transport))


@pytest.mark.parametrize("failure_command", [
    "OUTP ON,(@1)", "TRIG:ACQ (@1)", "INIT:TRAN (@1)", "*TRG", "FETC:ARR:VOLT? (@1)",
])
def test_failure_after_arm_aborts_and_disables_output(failure_command, rig, tmp_path):
    transport, _ = rig
    transport.fail_at = failure_command
    assert run("bus_delay", tmp_path) == 1
    assert not transport.output[1] and transport.closed
    sent = commands(transport)
    failure = sent.index(failure_command)
    assert "ABOR:TRAN (@1)" in sent[failure + 1:]
    assert "ABOR:ACQ (@1)" in sent[failure + 1:]
    assert "OUTP OFF,(@1)" in sent[failure + 1:]
    assert transport.arb_source == "EXT"
    assert "injected transport failure" in rows(tmp_path, "bus_delay")[0]["detail"]


def test_keyboard_interrupt_preserves_evidence_and_cleans_up(rig, tmp_path):
    transport, _ = rig
    transport.fail_at, transport.failure = "*TRG", KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run("bus_delay", tmp_path)
    assert not transport.output[1] and transport.closed
    assert transport.arb_source == "EXT"
    assert rows(tmp_path, "bus_delay")[0]["status"] == "INCONCLUSIVE"


def test_cleanup_abort_failure_still_attempts_acq_abort_off_and_close(rig, tmp_path):
    transport, _ = rig
    transport.fail_at = "ABOR:TRAN (@1)"
    transport.fail_once = False
    with pytest.raises(RuntimeError, match="cleanup failed"):
        run("all", tmp_path)
    assert "ABOR:ACQ (@1)" in commands(transport)
    assert "OUTP OFF,(@1)" in commands(transport)
    assert not transport.output[1] and transport.closed
    assert not (tmp_path / "bus_delay.csv").exists()
    assert rows(tmp_path, "arbsrc")[0]["status"] == "FAIL"


@pytest.mark.parametrize("bad", ["short", "nan", "flat"])
def test_bad_capture_does_not_claim_hardware_pass(bad, rig, tmp_path):
    transport, _ = rig
    transport.bad_capture = bad
    assert run("bus_delay", tmp_path) == 1
    assert rows(tmp_path, "bus_delay")[0]["status"] == "INCONCLUSIVE"


@pytest.mark.parametrize("name", ["bus_delay", "residual_bus", "slew", "all"])
@pytest.mark.parametrize("reply", ["n", "eof", "interrupt"])
def test_declining_consent_never_connects(name, reply, tmp_path, monkeypatch, capsys):
    def prompt(_):
        output = capsys.readouterr().out
        assert "WARNING:" in output
        if name in ("slew", "all"):
            assert "ENTIRE FRAME" in output
        if reply == "eof":
            raise EOFError
        if reply == "interrupt":
            raise KeyboardInterrupt
        return reply

    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(hw.execute, "connect_group", lambda *args: pytest.fail("must not connect"))
    assert hw.main([name, "--resource", "STUB", "--out", str(tmp_path), "--allow-reset"]) == 1
    assert not list(tmp_path.iterdir())


def test_interactive_consent_with_allow_reset_allows_reset(rig, tmp_path, monkeypatch):
    transport, _ = rig
    monkeypatch.setattr("builtins.input", lambda _: " y ")
    assert hw.main(["slew", "--resource", "STUB", "--out", str(tmp_path), "--allow-reset"]) == 0
    assert transport.resets == 1


def test_demo_uses_same_consent_and_cannot_report_pass(rig, tmp_path, capsys):
    transport, _ = rig
    assert hw.main(["all", "--demo", "--yes", "--allow-reset", "--out", str(tmp_path)]) == 1
    for name in hw.CHECKS:
        assert {r["status"] for r in rows(tmp_path, name)} == {"INCONCLUSIVE"}
    assert "never hardware acceptance" in capsys.readouterr().out
    assert transport.closed


def test_ext_prints_instructions_without_instrument_io(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hw.execute, "connect_group", lambda *a: pytest.fail("EXT must not connect"))
    monkeypatch.setattr(hw.InstrumentGroup, "demo", lambda **kw: pytest.fail("EXT must not connect"))
    assert hw.main(["ext", "--out", str(tmp_path)]) == 0
    assert "oscilloscope" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_selected_channel_only(rig, tmp_path):
    transport, _ = rig
    transport.output[3] = True  # another channel is not part of this experiment
    assert run("bus_delay", tmp_path, extra=["--channel", "2"]) == 0
    assert transport.output[3] and not transport.output[2]
    assert "OUTP ON,(@2)" in commands(transport)
    assert not any("(@1)" in c or "(@3)" in c for c in commands(transport))
    assert transport.arb_sources == dict.fromkeys(range(1, 5), "EXT")


@pytest.mark.parametrize("name", ["arbsrc", "bus_delay", "residual_bus"])
@pytest.mark.parametrize("source", ["IMM", "BUS", "EXT", "PIN1"])
def test_source_restored_to_subcheck_as_found(name, source, rig, tmp_path):
    transport, _ = rig
    transport.arb_source = source
    assert run(name, tmp_path) == 0
    assert transport.arb_source == source
    sent = commands(transport)
    assert sent[-4:] == [f"TRIG:ARB:SOUR {source},(@1)", "SYST:ERR?",
                        "TRIG:ARB:SOUR? (@1)", "SYST:ERR?"]
    assert next(r for r in rows(tmp_path, name) if r.get("phase") == "as_found")["source"] == source


@pytest.mark.parametrize("name", ["arbsrc", "bus_delay", "residual_bus"])
@pytest.mark.parametrize("unavailable", ["exception", "scpi_error", "empty", "invalid"])
def test_unavailable_as_found_falls_back_to_imm(name, unavailable, rig, tmp_path, monkeypatch):
    transport, _ = rig
    original = transport.query
    first = True

    def query(command):
        nonlocal first
        if command == "TRIG:ARB:SOUR? (@1)" and first:
            first = False
            transport.exchange(command)
            if unavailable == "exception":
                raise RuntimeError("snapshot unavailable")
            if unavailable == "scpi_error":
                transport.errors.append('-113,"snapshot refused"')
                return "EXT"  # A reply accompanied by an error is not reliable.
            return "" if unavailable == "empty" else "BUS;*RST"
        return original(command)

    monkeypatch.setattr(transport, "query", query)
    assert run(name, tmp_path) == int(name == "arbsrc")
    assert transport.arb_source == "IMM"
    assert commands(transport)[-4:] == ["TRIG:ARB:SOUR IMM,(@1)", "SYST:ERR?",
                                       "TRIG:ARB:SOUR? (@1)", "SYST:ERR?"]
    assert transport.resets == 0


def test_all_takes_a_fresh_source_snapshot_for_each_subcheck(rig, tmp_path, monkeypatch):
    transport, _ = rig
    original = hw.CHECKS["arbsrc"]

    def arbsrc_then_external_change(check):
        result = original(check)
        hw.cleanup(check)
        assert transport.arb_source == "EXT"
        transport.arb_source = "PIN1"
        return result

    monkeypatch.setitem(hw.CHECKS, "arbsrc", arbsrc_then_external_change)
    assert run("all", tmp_path) == 0
    for name, source in (("arbsrc", "EXT"), ("bus_delay", "PIN1"), ("residual_bus", "PIN1")):
        assert next(r for r in rows(tmp_path, name) if r.get("phase") == "as_found")["source"] == source
    assert transport.arb_source == "PIN1"


def test_no_source_restore_if_failure_precedes_source_write(rig, tmp_path):
    transport, _ = rig
    transport.fail_at = "VOLT:MODE FIX,(@1)"
    assert run("bus_delay", tmp_path) == 1
    assert not any(c.startswith("TRIG:ARB:SOUR ") for c in commands(transport))
    assert transport.arb_source == "EXT"


@pytest.mark.parametrize("failure", [RuntimeError("write completed then failed"), KeyboardInterrupt()])
def test_source_write_exception_after_mutation_still_restores(failure, rig, tmp_path, monkeypatch):
    transport, _ = rig
    original = transport.write

    def write(command):
        original(command)
        if command == "TRIG:ARB:SOUR IMM,(@1)":
            raise failure

    monkeypatch.setattr(transport, "write", write)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run("bus_delay", tmp_path)
    else:
        assert run("bus_delay", tmp_path) == 1
    assert transport.arb_source == "EXT"
    assert not transport.output[1] and transport.closed


def test_cleanup_restores_source_even_when_abort_fails(rig, tmp_path):
    transport, group = rig
    check = hw.Check(group, 2, tmp_path, "bus_delay")
    check.remember_arb_source()
    check.set_arb_source("BUS")
    transport.output[2] = True
    transport.fail_at = "ABOR:TRAN (@2)"
    with pytest.raises(RuntimeError, match="cleanup failed"):
        hw.cleanup(check)
    assert not transport.output[2]
    assert transport.arb_sources[2] == "EXT"
    sent = commands(transport)
    assert sent[-4:] == ["TRIG:ARB:SOUR EXT,(@2)", "SYST:ERR?",
                        "TRIG:ARB:SOUR? (@2)", "SYST:ERR?"]
    # A later cleanup must not send another restore for a completed restoration.
    hw.cleanup(check)
    assert commands(transport).count("TRIG:ARB:SOUR EXT,(@2)") == 1


@pytest.mark.parametrize("failure", ["scpi_error", "readback"])
def test_failed_source_restore_stops_all_and_saves_failure(failure, rig, tmp_path, monkeypatch):
    transport, _ = rig
    if failure == "scpi_error":
        transport.error_at["TRIG:ARB:SOUR EXT,(@1)"] = ['-222,"restore refused"']
    else:
        original = transport.write

        def ignore_restore(command):
            if command == "TRIG:ARB:SOUR EXT,(@1)":
                transport.exchange(command)
            else:
                original(command)

        monkeypatch.setattr(transport, "write", ignore_restore)
    with pytest.raises(RuntimeError, match="cannot restore ARB source EXT"):
        run("all", tmp_path)
    assert transport.closed and not transport.output[1]
    assert not (tmp_path / "bus_delay.csv").exists()
    assert rows(tmp_path, "arbsrc")[0]["status"] == "FAIL"
    if failure == "scpi_error":
        assert '-222' in rows(tmp_path, "arbsrc")[0]["detail"]
        assert not transport.errors


@pytest.mark.parametrize("name", ["arbsrc", "seqnum", "slew", "all"])
@pytest.mark.parametrize("enabled_channel", [1, 3])
def test_non_power_checks_refuse_enabled_outputs_without_writes(name, enabled_channel, rig, tmp_path):
    transport, _ = rig
    transport.output[enabled_channel] = True
    before = transport.output.copy()
    with pytest.raises(RuntimeError, match="turn outputs off"):
        run(name, tmp_path, extra=["--allow-reset"])
    assert transport.output == before and transport.closed
    assert all("?" in c for c in commands(transport))
    assert transport.resets == 0
    assert rows(tmp_path, "arbsrc" if name == "all" else name)[0]["status"] == "FAIL"


def test_output_state_query_failure_refuses_without_cleanup(rig, tmp_path):
    transport, _ = rig
    transport.fail_at = "OUTP? (@1)"
    with pytest.raises(RuntimeError, match="injected transport failure"):
        run("seqnum", tmp_path)
    assert all("?" in c for c in commands(transport))
    assert transport.closed


@pytest.mark.parametrize("extra", [[], ["--yes"]])
@pytest.mark.parametrize("mode", ["--demo", "--resource"])
def test_slew_requires_allow_reset_before_connection(extra, mode, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hw.execute, "connect_group", lambda *a: pytest.fail("must not connect"))
    monkeypatch.setattr(hw.InstrumentGroup, "demo", lambda **kw: pytest.fail("must not connect"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    mode_args = [mode, "STUB"] if mode == "--resource" else [mode]
    with pytest.raises(SystemExit) as exc:
        hw.main(["slew", *mode_args, "--out", str(tmp_path), *extra])
    assert exc.value.code == 2
    assert "--allow-reset is required" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_all_yes_skips_slew_without_reset_authorisation(rig, tmp_path, capsys):
    transport, _ = rig
    assert run("all", tmp_path) == 0
    assert transport.resets == 0 and "*RST" not in commands(transport)
    assert not (tmp_path / "slew.csv").exists()
    assert not (tmp_path / "slew_commands.csv").exists()
    assert all((tmp_path / f"{name}.csv").exists() for name in hw.CHECKS if name != "slew")
    assert "Skipping slew" in capsys.readouterr().out
