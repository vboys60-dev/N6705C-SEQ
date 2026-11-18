import pytest
from n6705c.core.group import InstrumentGroup
from n6705c_seq.execute import DEMO_SLOTS, run_plan
from n6705c_seq.plan import SeqRow


def demo(frames):
    return InstrumentGroup.demo(frames=frames, slots=DEMO_SLOTS)


def row(ch, delay=0.001, high=1.0, limit=None, enabled=True):
    return SeqRow(ch, enabled, f"CH{ch}", delay, 0.0, high, 0.002, 0.01, 0.002, limit)


def sent(instrument):
    return [h.text for h in instrument.session.history if h.direction == "->"]


def test_each_frame_is_started_by_one_init_for_all_its_channels():
    group = demo(2)
    run_plan(group, [row(c) for c in range(1, 9)])
    for inst in group.instruments:
        cmds = sent(inst)
        assert [c for c in cmds if c.startswith("INIT:TRAN")] == ["INIT:TRAN (@1,2,3,4)"]
        assert "*TRG" not in cmds  # the unit starts at INIT; a trigger would only mislead
        assert any("ARB" in c for c in cmds)


def test_global_channels_are_renumbered_per_frame():
    group = demo(2)
    run_plan(group, [row(c) for c in range(1, 6)])  # CH5 = frame 2, local channel 1
    f2 = sent(group.instruments[1])
    assert any("(@1)" in c and "ARB" in c for c in f2)
    assert not any("(@2)" in c and "ARB" in c for c in f2)


def test_current_limit_is_sent_only_when_given():
    group = demo(1)
    run_plan(group, [row(1, limit=0.5), row(2)])
    cmds = sent(group.instruments[0])
    assert any(c.startswith("CURR 0.5,(@1)") for c in cmds)
    assert not any(c.startswith("CURR ") and "(@2)" in c for c in cmds)  # set, not CURR:RANG? probe


def test_too_few_frames_is_refused_before_anything_is_switched_on():
    group = demo(1)
    with pytest.raises(ValueError, match="frame"):
        run_plan(group, [row(c) for c in range(1, 6)])
    assert not any(c.startswith("OUTP ON") for c in sent(group.instruments[0]))


def test_voltage_above_module_limit_is_refused_before_output_on():
    group = demo(1)
    with pytest.raises(ValueError):
        run_plan(group, [row(1, high=500.0)])
    assert not any(c.startswith("OUTP ON") for c in sent(group.instruments[0]))


def test_outputs_are_off_after_the_run():
    group = demo(2)
    run_plan(group, [row(1), row(5)])
    for inst in group.instruments:
        assert not any(inst.output_state(inst.populated_channels).values())


def test_outputs_are_forced_off_if_the_start_fails(monkeypatch):
    group = demo(2)
    import n6705c_seq.execute as ex

    def boom(*_, **kwargs):
        raise RuntimeError("link dropped")

    monkeypatch.setattr(ex, "start_together", boom)
    with pytest.raises(RuntimeError, match="link dropped"):
        run_plan(group, [row(1), row(5)])
    for inst in group.instruments:
        assert not any(inst.output_state(inst.populated_channels).values())


def test_a_shutdown_that_cannot_be_confirmed_is_an_error(monkeypatch):
    from n6705c.core.instrument import N6705C
    from n6705c_seq.execute import OutputsNotConfirmedOff

    monkeypatch.setattr(N6705C, "all_outputs_off", lambda self: None)  # the stop silently does nothing
    with pytest.raises(OutputsNotConfirmedOff, match="still reports an output on"):
        run_plan(demo(1), [row(1)])


def test_a_short_readback_is_not_a_confirmed_shutdown(monkeypatch):
    from n6705c.core.instrument import N6705C
    from n6705c_seq.execute import OutputsNotConfirmedOff

    monkeypatch.setattr(N6705C, "output_state", lambda self, channels: {})
    with pytest.raises(OutputsNotConfirmedOff, match="answered for"):
        run_plan(demo(1), [row(1)])


def test_a_failed_status_query_fails_the_run_and_turns_outputs_off(monkeypatch):
    from n6705c.core.arb import ArbRunner

    def dead(self):
        raise TimeoutError("no answer")

    monkeypatch.setattr(ArbRunner, "status", dead)
    group = demo(1)
    with pytest.raises(TimeoutError, match="no answer"):
        run_plan(group, [row(1)])
    monkeypatch.undo()
    inst = group.instruments[0]
    assert not any(inst.output_state(inst.populated_channels).values())


def test_a_rejected_plan_is_reported_as_such():
    from n6705c_seq.execute import PlanRejected

    with pytest.raises(PlanRejected):
        run_plan(demo(1), [row(1, high=500.0)])  # beyond the module's range


def test_hold_dc_stops_at_the_high_level_and_leaves_outputs_on():
    group = demo(1)
    run_plan(group, [row(1, high=1.5), row(2, high=2.5)], hold_dc=True)
    inst = group.instruments[0]
    assert all(inst.output_state([1, 2]).values())
    assert not any("TRAP" in c for c in sent(inst))
    assert any(c.startswith("ARB:VOLT:RAMP:END 1.5") for c in sent(inst))


@pytest.mark.parametrize("keep_on", [False, True])
def test_run_functions_plays_constant_dwell_and_controls_outputs(keep_on):
    from n6705c.core.arb import ArbSpec, ArbState, ConstantDwellArb, GroupArbRunner
    from n6705c.core.function import ChannelFunction
    from n6705c_seq.execute import run_functions

    group = demo(1)
    functions = [
        ChannelFunction(
            channel=channel,
            voltage=levels[0],
            output_on=True,
            arb_enabled=True,
            spec=ArbSpec(
                channel=channel,
                waveform=ConstantDwellArb(levels=levels, dwell=10.24e-6 * 100),
                count=3,
                terminate_last=True,
            ),
        )
        for channel, levels in [(1, [0.0, 0.5, 1.0]), (2, [0.0, 1.0, 2.0])]
    ]
    runner = run_functions(group, functions, keep_on=keep_on)
    assert isinstance(runner, GroupArbRunner)
    assert not runner.is_running
    assert all(arb.state is ArbState.DONE for frame in runner.runners for arb in frame.runners)
    inst = group.instruments[0]
    cmds = sent(inst)
    for channel in [1, 2]:
        assert any(c.startswith("ARB:VOLT:CDW ") and f"(@{channel})" in c for c in cmds)
        assert f"ARB:COUN 3,(@{channel})" in cmds
    assert inst.output_state(inst.populated_channels) == {1: keep_on, 2: keep_on, 3: False, 4: False}


def test_run_functions_rejects_empty_input_before_instrument_commands():
    from n6705c_seq.execute import PlanRejected, run_functions

    group = demo(1)
    inst = group.instruments[0]
    before = sent(inst)
    with pytest.raises(PlanRejected, match="no enabled channels in the table"):
        run_functions(group, [])
    assert sent(inst) == before


@pytest.mark.parametrize("which", [[1], [0, 1]])  # a mix, then all E36441A
def test_e36441a_group_is_rejected_before_setting_commands(which):
    from n6705c_seq.execute import PlanRejected

    group = demo(2)
    for i in which:
        object.__setattr__(group.instruments[i].identity, "model", "E36441A")  # frozen
    before = [sent(inst) for inst in group.instruments]
    with pytest.raises(PlanRejected, match="E36441A"):
        run_plan(group, [row(1), row(5)])
    assert [sent(inst) for inst in group.instruments] == before


def test_outputs_off_queries_all_channels_together():
    from n6705c_seq.execute import outputs_off

    group = demo(1)
    inst = group.instruments[0]
    before = len(sent(inst))
    outputs_off(group)
    assert [cmd for cmd in sent(inst)[before:] if cmd.startswith("OUTP?")] == [
        "OUTP? (@1,2,3,4)"
    ]


@pytest.mark.parametrize("entry", ["plan", "functions"])
@pytest.mark.parametrize("mode", ["normal", "failure", "keep_on"])
def test_cleanup_callback_only_precedes_outputs_off(monkeypatch, entry, mode):
    from n6705c_seq import execute
    from n6705c_seq.plan import to_functions

    group = demo(1)
    events = []
    def main(*args):
        assert events == []
        if mode == "failure":
            raise RuntimeError("main failed")
    monkeypatch.setattr(execute, "_wait", main)
    original_apply = execute._apply_levels
    def apply(*args):
        assert events == []
        original_apply(*args)
    monkeypatch.setattr(execute, "_apply_levels", apply)
    off = execute.outputs_off
    def cleanup(group):
        assert events == ["callback"]
        events.append("off")
        off(group)
    monkeypatch.setattr(execute, "outputs_off", cleanup)
    kwargs = {"on_cleanup": lambda: events.append("callback")}
    if entry == "plan":
        run = lambda: execute.run_plan(group, [row(1)], hold_dc=mode == "keep_on", **kwargs)
    else:
        run = lambda: execute.run_functions(group, to_functions([row(1)]),
                                           keep_on=mode == "keep_on", **kwargs)
    if mode == "failure":
        with pytest.raises(RuntimeError, match="main failed"):
            run()
    else:
        run()
    assert events == ([] if mode == "keep_on" else ["callback", "off"])


def test_connect_group_closes_transport_when_wrapping_fails(monkeypatch):
    from n6705c_seq import execute

    closed = []
    class FakeTransport:
        def __init__(self, resource, **kwargs):
            self.resource, self._inst = resource, type("Inst", (), {})()
        def close(self):
            closed.append(self.resource)
            if self.resource == "a":
                raise OSError("a stuck")
    class FakeInstrument:
        @staticmethod
        def from_transport(transport):
            if transport.resource == "c":
                raise RuntimeError("idn failed")
            return transport
    monkeypatch.setattr(execute, "VisaTransport", FakeTransport)
    monkeypatch.setattr(execute, "N6705C", FakeInstrument)
    with pytest.raises(RuntimeError, match="idn failed") as info:
        execute.connect_group(["a", "b", "c"])
    assert closed == ["a", "b", "c"]
    assert "frame 1 (a stuck)" in info.value.__notes__[0]


def test_run_plan_switches_off_even_if_cleanup_callback_fails(monkeypatch):
    from n6705c_seq import execute

    offs = []
    monkeypatch.setattr(execute, "outputs_off", lambda group: offs.append(group))
    monkeypatch.setattr(execute, "_wait", lambda runner: (_ for _ in ()).throw(RuntimeError("wait failed")))
    def bad_callback():
        raise OSError("callback broke")
    group = demo(1)
    with pytest.raises(RuntimeError, match="wait failed") as info:
        run_plan(group, [row(1)], on_cleanup=bad_callback)
    assert offs == [group] and "callback broke" in info.value.__notes__[0]


def test_all_sources_are_checked_before_enabling_outputs(monkeypatch):
    from n6705c_seq import execute

    group = demo(2)
    set_output = group.set_output
    apply_levels = execute._apply_levels

    def apply(*args):
        for inst in group.instruments:
            commands = sent(inst)
            assert "TRIG:TRAN:SOUR IMM,(@1)" in commands
            assert "TRIG:ARB:SOUR IMM,(@1)" in commands
            assert not any(c.startswith(("VOLT ", "CURR ", "OUTP ON", "ARB:")) for c in commands)
        apply_levels(*args)

    def enable(channels, on):
        if on:
            for inst in group.instruments:
                assert "TRIG:ARB:SOUR IMM,(@1)" in sent(inst)
                assert not any(c.startswith("INIT:TRAN") for c in sent(inst))
        set_output(channels, on)

    monkeypatch.setattr(group, "set_output", enable)
    monkeypatch.setattr(execute, "_apply_levels", apply)
    run_plan(group, [row(1), row(5)])
    for inst in group.instruments:
        cmds = sent(inst)
        assert cmds.index("TRIG:ARB:SOUR IMM,(@1)") < cmds.index("OUTP ON,(@1)")
        assert cmds.index("OUTP ON,(@1)") < cmds.index("INIT:TRAN (@1)")


@pytest.mark.parametrize("capture_enabled", [False, True])
@pytest.mark.parametrize("entry", ["plan", "functions"])
def test_trigger_sources_levels_output_load_and_init_keep_hardware_order(monkeypatch, entry, capture_enabled):
    from n6705c.core.arb import GroupArbRunner
    from n6705c_seq import execute
    from n6705c_seq.plan import to_functions

    group = demo(2)
    load = GroupArbRunner.load
    loaded = []

    def load_after_output_on(runner):
        for inst in group.instruments:
            commands = sent(inst)
            assert "OUTP ON,(@1)" in commands
            assert not any(c.startswith(("ARB:", "INIT:")) for c in commands)
        loaded.append(runner)
        load(runner)

    monkeypatch.setattr(GroupArbRunner, "load", load_after_output_on)
    rows = [row(1, limit=0.5), row(5, limit=0.5)]
    capture = {} if capture_enabled else None
    if entry == "plan":
        execute.run_plan(group, rows, capture=capture)
    else:
        execute.run_functions(group, to_functions(rows), capture=capture)
    assert len(loaded) == 1
    for inst in group.instruments:
        commands = sent(inst)
        tran = "TRIG:TRAN:SOUR IMM,(@1)"
        arb = "TRIG:ARB:SOUR IMM,(@1)"
        voltage, current = commands.index("VOLT 0,(@1)"), commands.index("CURR 0.5,(@1)")
        output = commands.index("OUTP ON,(@1)")
        arb_settings = [i for i, c in enumerate(commands) if c.startswith("ARB:")]
        init = commands.index("INIT:TRAN (@1)")
        assert commands.index(tran) < commands.index(arb) < voltage < current < output
        assert output < min(arb_settings) <= max(arb_settings) < init
        assert commands.count(tran) == commands.count(arb) == 2
        assert max(arb_settings) < max(i for i, c in enumerate(commands) if c == tran)
        assert max(i for i, c in enumerate(commands) if c == tran) < max(
            i for i, c in enumerate(commands) if c == arb) < init
        if capture_enabled:
            assert max(arb_settings) < commands.index("INIT:IMM:ACQ (@1)") < init


@pytest.mark.parametrize("rejected_frame", [0, 1])
def test_rejected_arb_source_aborts_before_output_on_and_forces_all_off(monkeypatch, rejected_frame):
    group = demo(2)
    # Include a pre-existing output: the failure must still force it off.
    group.instruments[0].set_output([2], True)
    before = [len(sent(inst)) for inst in group.instruments]
    fake = group.instruments[rejected_frame].session._transport
    write = fake.write

    def reject(command):
        if command.startswith("TRIG:ARB:SOUR"):
            fake.errors.append((-113, "Undefined header"))
        else:
            write(command)

    monkeypatch.setattr(fake, "write", reject)
    with pytest.raises(Exception, match="-113"):
        run_plan(group, [row(1), row(5)])
    for index, inst in enumerate(group.instruments):
        commands = sent(inst)[before[index]:]
        assert not any(c.startswith(("VOLT ", "CURR ", "OUTP ON", "ARB:", "INIT:")) for c in commands)
        assert any(c.startswith("OUTP OFF") for c in commands)
        assert not any(inst.output_state(inst.populated_channels).values())


@pytest.mark.parametrize("override,rate,expected", [
    (0, 2, True), (1, 2, False), (0, 500, False), (1, 500, False),
])
def test_slew_warning_uses_override_and_plan_required_rate(override, rate, expected):
    from n6705c_seq.execute import slew_warnings
    from n6705c_seq.i18n import tr
    from n6705c_seq.plan import to_functions

    group = demo(2)
    channel = group.instruments[1].session._transport.channels[2]
    channel.voltage_slew_override = bool(override)
    channel.voltage_slew_rate = rate
    warnings = slew_warnings(group, to_functions([row(1), row(6)]))
    assert warnings == ([tr("slew_warning", channel="F2:CH2", actual=float(rate),
                            required=500.0)] if expected else [])
    for instrument in group.instruments:
        assert not any(c.startswith("VOLT:SLEW? MAX") for c in sent(instrument))


def test_slew_default_rst_state_does_not_warn():
    _assert_slew_rate_against_plan(3300, False, False)


def test_slew_user_rate_below_plan_requirement_warns():
    _assert_slew_rate_against_plan(2, False, True)


@pytest.mark.parametrize("rate", [9.9e37, 1e37])
def test_slew_inf_sentinel_rate_never_warns(rate):
    _assert_slew_rate_against_plan(rate, False, False)


def test_slew_rate_between_need_and_max_does_not_warn():
    _assert_slew_rate_against_plan(1000, False, False)


def test_slew_override_set_never_warns():
    _assert_slew_rate_against_plan(2, True, False)


def _assert_slew_rate_against_plan(rate, override, expected):
    from n6705c_seq.execute import slew_warnings
    from n6705c_seq.i18n import tr
    from n6705c_seq.plan import to_functions

    group = demo(1)
    channel = group.instruments[0].session._transport.channels[1]
    channel.voltage_slew_override = override
    channel.voltage_slew_rate = rate
    channel.voltage_slew_maximum = 9.9e37
    assert slew_warnings(group, to_functions([row(1)])) == (
        [tr("slew_warning", channel="F1:CH1", actual=2.0, required=500.0)] if expected else [])
    assert not any(c.startswith("VOLT:SLEW? MAX") for c in sent(group.instruments[0]))


@pytest.mark.parametrize("shape", ["step", "pulse", "zero_edges"])
def test_slew_zero_duration_jumps_do_not_require_rate(shape):
    from n6705c.core.arb import ArbSpec, PulseArb, StepArb, TrapezoidArb
    from n6705c.core.function import ChannelFunction
    from n6705c_seq.execute import slew_warnings

    waveform = {"step": StepArb(), "pulse": PulseArb(),
                "zero_edges": TrapezoidArb(rise_time=0, fall_time=0)}[shape]
    group = demo(1)
    group.instruments[0].session._transport.channels[1].voltage_slew_rate = 0
    fn = ChannelFunction(1, arb_enabled=True, spec=ArbSpec(1, waveform))
    assert slew_warnings(group, [fn]) == []


@pytest.mark.parametrize("sequence", [False, True])
def test_slew_uses_maximum_requirement_across_channel_functions(sequence):
    from n6705c.core.arb import ArbSequence, ArbSpec, RampArb, SequenceStep
    from n6705c.core.function import ChannelFunction
    from n6705c_seq.execute import slew_warnings
    from n6705c_seq.i18n import tr

    # Falling ramps count too; slower functions on the same channel cannot mask them.
    fast = RampArb(start_level=2, end_level=0, rise_time=0.002, start_time=0, end_time=0)
    slow = RampArb(start_level=0, end_level=1, rise_time=0.01, start_time=0, end_time=0)
    specs = ([ArbSequence(1, [SequenceStep(fast), SequenceStep(slow)])] if sequence
             else [ArbSpec(1, fast), ArbSpec(1, slow)])
    functions = [ChannelFunction(1, arb_enabled=True, spec=spec) for spec in specs]
    group = demo(1)
    group.instruments[0].session._transport.channels[1].voltage_slew_rate = 600
    assert slew_warnings(group, functions) == [
        tr("slew_warning", channel="F1:CH1", actual=600.0, required=1000.0)]


def test_warning_callback_precedes_output_on_and_run_continues():
    from n6705c_seq.execute import run_functions
    from n6705c_seq.plan import to_functions

    group = demo(1)
    inst = group.instruments[0]
    inst.session.write_checked("VOLT:SLEW 2,(@1)")
    warnings = []

    def warning(text):
        assert not any(c.startswith("OUTP ON") for c in sent(inst))
        warnings.append(text)

    run_functions(group, to_functions([row(1)]), on_warning=warning)
    assert len(warnings) == 1
    assert any(c.startswith("INIT:TRAN") for c in sent(inst))
    assert not any(inst.output_state(inst.populated_channels).values())


def test_missing_warning_callback_logs_warning(caplog):
    group = demo(1)
    group.instruments[0].session.write_checked("VOLT:SLEW 2,(@1)")
    run_plan(group, [row(1)])
    assert "F1:CH1" in caplog.text
    record = next(record for record in caplog.records if "F1:CH1" in record.getMessage())
    assert record.msg == "%s" and len(record.args) == 1


@pytest.mark.parametrize("query", ["VOLT:SLEW:MAX? (@1)", "VOLT:SLEW? (@1)"])
@pytest.mark.parametrize("code", [-113, 323])
def test_rejected_slew_probe_logs_raw_reply_and_error_but_run_continues(monkeypatch, caplog, query, code):
    group = demo(1)
    inst = group.instruments[0]
    inst.session.write_checked("VOLT:SLEW 2,(@1)")
    fake = inst.session._transport
    original = fake.query

    def reject(command):
        if command == query:
            fake.errors.append((code, "Probe rejected"))
            return "2"  # Even a numeric response must not hide the SCPI error.
        return original(command)

    monkeypatch.setattr(fake, "query", reject)
    warnings = []
    run_plan(group, [row(1)], on_warning=warnings.append)
    assert warnings == []
    assert query in caplog.text and "'2'" in caplog.text and str(code) in caplog.text
    assert "Probe rejected" in caplog.text
    assert not fake.errors
    assert "INIT:TRAN (@1)" in sent(inst)


@pytest.mark.parametrize("check_fails", [False, True])
def test_slew_timeout_logs_query_and_error_check_failures_but_run_continues(monkeypatch, caplog, check_fails):
    group = demo(1)
    inst = group.instruments[0]
    fake = inst.session._transport
    query = fake.query
    command = "VOLT:SLEW? (@1)"

    def timeout(probe):
        if probe == command:
            if check_fails:
                fake.errors.append((-113, "Slew error check rejected"))
            raise TimeoutError("Slew query timed out")
        return query(probe)

    monkeypatch.setattr(fake, "query", timeout)
    warnings = []
    run_plan(group, [row(1)], on_warning=warnings.append)
    assert warnings == []
    records = [r for r in caplog.records if command in r.getMessage()]
    assert len(records) == (2 if check_fails else 1)
    assert any("Slew query timed out" in r.getMessage() for r in records)
    if check_fails:
        assert any("-113" in r.getMessage() and "Slew error check rejected" in r.getMessage()
                   for r in records)
    assert not fake.errors
    assert "INIT:TRAN (@1)" in sent(inst)
    assert not any(inst.output_state(inst.populated_channels).values())


@pytest.mark.parametrize("reply", ["", "nan", "1,2"])
def test_malformed_slew_reply_is_nonfatal(monkeypatch, caplog, reply):
    group = demo(1)
    session = group.instruments[0].session
    original = session.query
    monkeypatch.setattr(session, "query", lambda command: reply if command == "VOLT:SLEW? (@1)"
                        else original(command))
    warnings = []
    run_plan(group, [row(1)], on_warning=warnings.append)
    assert warnings == [] and "slew query VOLT:SLEW? (@1)" in caplog.text
    assert repr(reply) in caplog.text


def test_fake_slew_defaults_coupling_prefixes_and_channel_order():
    from n6705c.core.fake import FakeN6705C

    fake = FakeN6705C()
    assert fake.query("VOLT:SLEW? (@2,1)") == fake.query("VOLT:SLEW? MAX,(@2,1)")
    assert fake.query("SOUR:VOLT:SLEW:MAX? (@2,1)") == "0,0"
    fake.write("SOURCE:VOLTAGE:SLEW 2,(@1)")
    fake.write("VOLT:SLEW 3,(@2)")
    assert fake.query("SOURCE:VOLTAGE:SLEW? (@2,1)") == "3.0,2.0"
    assert fake.query("VOLT:SLEW:MAX? (@2,1)") == "0,0"
    for value in ("MAX", "INF"):
        fake.write(f"VOLT:SLEW {value},(@1)")
        assert fake.query("VOLT:SLEW? (@1)") == fake.query("SOUR:VOLT:SLEW? MAX,(@1)")
        assert fake.query("VOLT:SLEW:MAX? (@2,1)") == "0,1"
    fake.write("VOLT:SLEW 4,(@1)")
    assert fake.query("VOLT:SLEW:MAX? (@1)") == "0"
    assert fake.query("SYST:ERR?").startswith("+0,")
    fake.write("UNKNOWN:HEADER")
    assert fake.query("SYST:ERR?").startswith("-113,")


def test_fake_arb_source_storage_does_not_change_init_behavior():
    from n6705c.core.fake import FakeN6705C

    fake = FakeN6705C()
    assert fake.query("TRIG:ARB:SOUR? (@2,1)") == "IMM,IMM"
    fake.write("TRIG:ARB:SOUR BUS,(@1)")
    assert fake.query("TRIGGER:ARB:SOURCE? (@2,1)") == "IMM,BUS"
    fake.write("ARB:FUNC:SHAP RAMP,(@1,2)")
    fake.write("TRIG:TRAN:SOUR IMM,(@1)")
    fake.write("TRIG:TRAN:SOUR BUS,(@2)")
    fake.write("INIT:TRAN (@1,2)")
    assert fake.channels[1].tran_running
    assert not fake.channels[2].tran_running
    assert fake.query("SYST:ERR?").startswith("+0,")


class _Session:
    def __init__(self, reject_list):
        self.reject_list, self.sent = reject_list, []

    def write_checked(self, command):
        from n6705c.core.session import ScpiError
        self.sent.append(command)
        if self.reject_list and "(@" in command:
            raise ScpiError(-108, "Parameter not allowed", command)


@pytest.mark.parametrize("reject, expected", [
    (False, ["TRIG:ARB:SOUR IMM,(@1,2)"]),
    (True, ["TRIG:ARB:SOUR IMM,(@1,2)", "TRIG:ARB:SOUR IMM"]),  # E.02.09 is frame-wide
])
def test_arb_source_falls_back_to_frame_wide_form_on_minus_108(reject, expected):
    from types import SimpleNamespace
    from n6705c_seq.execute import _set_arb_source_imm
    inst = SimpleNamespace(session=_Session(reject))
    _set_arb_source_imm(inst, [1, 2])
    assert inst.session.sent == expected


def test_arb_source_other_errors_still_raise():
    from types import SimpleNamespace
    from n6705c.core.session import ScpiError
    from n6705c_seq.execute import _set_arb_source_imm

    class Bad:
        def write_checked(self, command):
            raise ScpiError(-221, "Settings conflict", command)
    with pytest.raises(ScpiError):
        _set_arb_source_imm(SimpleNamespace(session=Bad()), [1])
