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

    def boom(*_):
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
