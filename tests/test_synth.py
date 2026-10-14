import math
from pathlib import Path

import pytest
from n6705c.core.arb import ArbSpec, ConstantDwellArb
from n6705c.core.arb.models import CDWELL_DWELL_GRANULARITY, CDWELL_DWELL_MAX, CDWELL_DWELL_MIN, CDWELL_MAX_POINTS
from n6705c.core.function import ChannelFunction
from n6705c.core.group import InstrumentGroup
from n6705c_seq.execute import DEMO_SLOTS, run_functions
from n6705c_seq.listplan import ChannelList, ListPlan, Segment, load_json
from n6705c_seq.synth import auto_dwell, base_duration, channel_duration, compile_plan, to_functions, value


DWELL = CDWELL_DWELL_GRANULARITY * 100


def ramp(duration=0.01, start=0.0, end=1.0, repeat=1):
    return Segment("ramp", dict(start=start, end=end, delay=0.0, ramp_time=duration, end_time=0.0), repeat)


def periodic(shape="sine", frequency=1000.0, repeat=1, phase=0.0):
    return Segment(shape, dict(offset=2.0, amplitude=1.0, frequency=frequency, phase=phase), repeat)


def test_ramp_right_endpoint_and_rounded_points():
    c = compile_plan(ListPlan([ChannelList(1, segments=[ramp()])], dwell=DWELL))
    assert not c.errors
    assert c.n_points == 10
    assert c.levels[1][0] == pytest.approx(DWELL / 0.01)
    assert c.levels[1][-1] == 1.0
    assert c.segments[1][0].requested == 0.01
    assert c.segments[1][0].actual == pytest.approx(10 * DWELL)


@pytest.mark.parametrize("segment_count", [1, 2])
def test_rounded_down_ramp_keeps_endpoint_for_each_segment(segment_count):
    segments = [ramp(10.4 * DWELL)]
    if segment_count == 2:
        segments.append(ramp(10.4 * DWELL, start=1.0, end=2.0))
    c = compile_plan(ListPlan([ChannelList(1, segments=segments)], dwell=DWELL))
    assert not c.errors
    assert c.segments[1][0].points == 10
    assert c.segments[1][0].actual < c.segments[1][0].requested
    boundary = 0
    for seg, info in zip(segments, c.segments[1]):
        assert c.levels[1][boundary:boundary + info.points - 1] == pytest.approx(
            [value(seg, (k + 1) * DWELL) for k in range(info.points - 1)]
        )
        boundary += info.points
        assert c.levels[1][boundary - 1] == seg.params["end"]


def test_cumulative_bankers_rounding_does_not_accumulate():
    c = compile_plan(ListPlan([ChannelList(1, segments=[ramp(0.001536)] * 3)], dwell=DWELL))
    assert not c.errors
    assert [s.points for s in c.segments[1]] == [2, 1, 1]
    assert c.n_points == 4


def test_sine_repeats_share_one_time_grid():
    seg = periodic(repeat=3)
    dwell = CDWELL_DWELL_MIN * 4
    c = compile_plan(ListPlan([ChannelList(1, segments=[seg])], dwell=dwell))
    assert not c.errors
    assert c.n_points == round(0.003 / dwell)
    assert c.levels[1][0] == pytest.approx(value(seg, dwell))
    # A period is not an integer number of dwells; frequency must stay unchanged.
    for k, level in enumerate(c.levels[1][:-1]):
        assert level == pytest.approx(value(seg, (k + 1) * dwell))
    assert c.levels[1][-1] == value(seg, base_duration(seg))


def test_repeat_boundary_uses_endpoint_instead_of_start():
    seg = ramp(DWELL * 4, repeat=2)
    c = compile_plan(ListPlan([ChannelList(1, segments=[seg])], dwell=DWELL))
    assert c.levels[1] == pytest.approx([0.25, 0.5, 0.75, 1.0] * 2)
    assert any("repeat seam jump" in w for w in c.warnings)


def test_rounded_tail_and_float_repeat_boundaries_keep_endpoint():
    seg = ramp(DWELL * 3 / 5, repeat=5)
    c = compile_plan(ListPlan([ChannelList(1, segments=[seg])], dwell=DWELL))
    assert not c.errors
    assert c.levels[1][-1] == 1.0


def test_empty_and_disabled_channels_do_not_form_a_live_plan():
    c = compile_plan(ListPlan([ChannelList(1), ChannelList(2, enabled=False, segments=[ramp()])]))
    assert c.errors == ["no enabled channel with segments"]
    assert not c.levels


@pytest.mark.parametrize("phase,u,expected", [(0, 0.00025, 3), (90, 0, 3), (0, 0.00075, 1)])
def test_triangle_phase(phase, u, expected):
    assert value(periodic("triangle", phase=phase), u) == pytest.approx(expected)


def test_channels_pad_with_their_own_last_sample():
    plan = ListPlan([ChannelList(1, segments=[ramp(DWELL * 4)]),
                     ChannelList(5, segments=[ramp(DWELL * 2, end=2)])], dwell=DWELL)
    c = compile_plan(plan)
    assert not c.errors
    assert c.n_points == 4
    assert len(c.levels[1]) == len(c.levels[5])
    assert c.pad_points == {1: 0, 5: 2}
    assert c.levels[5][-3:] == [2.0] * 3


def test_channel_delay_uses_initial_voltage_and_shared_rounding():
    ch = ChannelList(1, delay=0.005, segments=[ramp(start=2, end=3)])
    c = compile_plan(ListPlan([ch], dwell=DWELL))
    assert not c.errors
    n = round(ch.delay / DWELL)
    assert c.delay_points[1] == n
    assert c.levels[1][:n] == [value(ch.segments[0], 0)] * n
    assert c.n_points == round(channel_duration(ch) / DWELL)


def test_auto_dwell_fits_100_seconds_and_ignores_whole_list_repeat():
    plan = ListPlan([ChannelList(1, segments=[ramp(100)])], repeat=256)
    expected = math.ceil(100 / CDWELL_MAX_POINTS / CDWELL_DWELL_GRANULARITY) * CDWELL_DWELL_GRANULARITY
    assert auto_dwell(plan) == expected
    c = compile_plan(plan)
    assert not c.errors
    assert c.dwell == expected
    assert c.n_points <= CDWELL_MAX_POINTS


def test_manual_dwell_exceeding_budget_is_rejected_before_allocating_samples():
    c = compile_plan(ListPlan([ChannelList(1, segments=[ramp(100)])], dwell=CDWELL_DWELL_MIN))
    assert any("exceed 65,535" in error for error in c.errors)
    assert c.n_points > CDWELL_MAX_POINTS
    assert not c.levels


@pytest.mark.parametrize("shape", ["sine", "triangle"])
@pytest.mark.parametrize("ppp,blocked,warned", [(9, True, False), (10, False, True), (15, False, True), (20, False, False)])
def test_period_quality_thresholds(shape, ppp, blocked, warned):
    seg = periodic(shape, frequency=1 / (ppp * CDWELL_DWELL_MIN))
    c = compile_plan(ListPlan([ChannelList(1, segments=[seg])], dwell=CDWELL_DWELL_MIN))
    assert bool(c.errors) == blocked
    assert any("points per period" in w for w in c.warnings) == warned


def test_seam_discontinuity_warns_without_blocking():
    c = compile_plan(ListPlan([ChannelList(1, segments=[ramp(end=1), ramp(start=3, end=4)])], dwell=DWELL))
    assert not c.errors
    assert any("seam jump 2 V between segments" in w for w in c.warnings)


def test_to_functions_preserves_core_contract_and_filters_inactive_channels():
    plan = ListPlan([ChannelList(1, current_limit=0.5, segments=[ramp()]),
                     ChannelList(2, segments=[ramp()]), ChannelList(3),
                     ChannelList(4, enabled=False, segments=[ramp(1000)])], repeat=3, hold_last=False, dwell=DWELL)
    c = compile_plan(plan)
    assert not c.errors
    functions = to_functions(plan, c)
    assert [fn.channel for fn in functions] == [1, 2]
    assert [fn.current for fn in functions] == [0.5, 0.0]
    for fn in functions:
        assert isinstance(fn, ChannelFunction)
        assert isinstance(fn.spec, ArbSpec)
        assert isinstance(fn.spec.waveform, ConstantDwellArb)
        assert fn.spec.count == plan.repeat
        assert fn.spec.terminate_last is True
        assert fn.voltage == c.levels[fn.channel][0]
        assert fn.spec.waveform.levels == c.levels[fn.channel]
        assert fn.spec.waveform.dwell == c.dwell
        assert fn.output_on and fn.arb_enabled and fn.delay == 0


def test_to_functions_rejects_errors():
    plan = ListPlan([])
    with pytest.raises(ValueError, match="no enabled channel"):
        to_functions(plan, compile_plan(plan))


@pytest.mark.parametrize("shape,params,duration,samples", [
    ("step", dict(start=1, end=3, delay=1, end_time=2), 3, [(0, 1), (1, 1), (1.1, 3), (3, 3)]),
    ("ramp", dict(start=1, end=3, delay=1, ramp_time=2, end_time=1), 4, [(0, 1), (1, 1), (2, 2), (3, 3), (4, 3)]),
    ("trapezoid", dict(offset=1, amplitude=2, start_time=1, up_time=2, high_time=1, down_time=2, end_time=1), 7,
     [(0, 1), (1, 1), (2, 2), (3, 3), (4, 3), (5, 2), (6, 1), (7, 1)]),
    ("staircase", dict(start=1, end=3, delay=1, n_steps=4, stair_time=2, end_time=1), 4,
     [(0, 1), (1, 1), (1.1, 1.5), (1.5, 1.5), (1.6, 2), (3, 3), (4, 3)]),
    ("exponential", dict(start=1, end=3, delay=1, tau=2, exp_time=2, end_time=1), 4,
     [(0, 1), (1, 1), (2, 3 - 2 * math.exp(-0.5)), (3, 3 - 2 / math.e), (4, 3 - 2 / math.e)]),
    ("sine", dict(offset=2, amplitude=1, frequency=1, phase=90), 1, [(0, 3), (0.5, 1), (1, 3)]),
    ("triangle", dict(offset=2, amplitude=1, frequency=1, phase=0), 1, [(0, 2), (0.25, 3), (0.5, 2), (0.75, 1), (1, 2)]),
])
def test_all_shape_durations_and_values(shape, params, duration, samples):
    seg = Segment(shape, params, repeat=3)
    assert base_duration(seg) == duration
    assert channel_duration(ChannelList(1, delay=2, segments=[seg])) == 2 + duration * 3
    for u, expected in samples:
        assert value(seg, u) == pytest.approx(expected)


@pytest.mark.parametrize("seg,u,expected", [
    (ramp(0), 0.1, 1),
    (Segment("step", dict(start=1, end=3, delay=0, end_time=1)), 0.1, 3),
    (Segment("staircase", dict(start=1, end=3, delay=0, n_steps=4, stair_time=0, end_time=1)), 0.1, 3),
    (Segment("exponential", dict(start=1, end=3, delay=0, tau=1, exp_time=0, end_time=1)), 0.1, 1),
    (Segment("trapezoid", dict(offset=1, amplitude=2, start_time=0, up_time=0, high_time=1, down_time=0, end_time=1)), 0.5, 3),
    (Segment("trapezoid", dict(offset=1, amplitude=2, start_time=0, up_time=0, high_time=1, down_time=0, end_time=1)), 1.1, 1),
])
def test_zero_time_parts_are_skipped(seg, u, expected):
    assert value(seg, u) == expected


def test_zero_duration_is_an_error_and_tiny_segments_are_retained():
    c = compile_plan(ListPlan([ChannelList(1, segments=[ramp(0)])]))
    assert any("segment duration must be > 0" in error for error in c.errors)
    c = compile_plan(ListPlan([ChannelList(1, segments=[ramp(DWELL / 10)] * 3)], dwell=DWELL))
    assert not c.errors
    assert c.n_points == 3
    assert all(s.points == 1 for s in c.segments[1])
    assert sum("kept 1 point" in w for w in c.warnings) == 3


def test_edge_quality_warnings():
    segments = [ramp(DWELL * 2),
                Segment("trapezoid", dict(offset=0, amplitude=1, start_time=0, up_time=DWELL * 2,
                                         high_time=DWELL, down_time=DWELL * 2, end_time=0)),
                Segment("exponential", dict(start=0, end=1, delay=0, tau=DWELL * 2, exp_time=DWELL * 4, end_time=0))]
    c = compile_plan(ListPlan([ChannelList(1, segments=segments)], dwell=DWELL))
    assert not c.errors
    for edge in ("ramp_time", "up_time", "down_time", "tau"):
        assert any(edge in w for w in c.warnings)


def test_manual_dwell_snaps_to_grid():
    c = compile_plan(ListPlan([ChannelList(1, segments=[ramp()])], dwell=CDWELL_DWELL_MIN * 100.4))
    assert not c.errors
    assert c.dwell == DWELL


@pytest.mark.parametrize("dwell", [0, -1, CDWELL_DWELL_MAX * 2, math.inf, math.nan])
def test_invalid_dwell_returns_errors(dwell):
    assert compile_plan(ListPlan([ChannelList(1, segments=[ramp()])], dwell=dwell)).errors


def test_auto_dwell_exceeding_hardware_limit_returns_errors():
    assert compile_plan(ListPlan([ChannelList(1, segments=[ramp(30000)])])).errors


def test_bad_in_memory_parameters_return_errors():
    for seg in [periodic(frequency=0), Segment("unknown", {}), Segment("ramp", {}), ramp(repeat=0)]:
        assert compile_plan(ListPlan([ChannelList(1, segments=[seg])])).errors


def test_list_demo_compiles_and_runs_to_completion_with_outputs_off():
    plan = load_json(Path(__file__).resolve().parents[1] / "examples" / "list_demo.json")
    c = compile_plan(plan)
    assert not c.errors
    functions = to_functions(plan, c)
    group = InstrumentGroup.demo(frames=1, slots=DEMO_SLOTS)
    try:
        runner = run_functions(group, functions, keep_on=plan.hold_last)
        assert not runner.is_running
        inst = group.instruments[0]
        assert not any(inst.output_state(inst.populated_channels).values())
        commands = [h.text for h in inst.session.history if h.direction == "->"]
        assert any(command.startswith("ARB:VOLT:CDW ") for command in commands)
        assert "ARB:COUN 2,(@1)" in commands
    finally:
        group.close()


def test_invalid_segment_diagnostic_keeps_channel_and_segment_location():
    c = compile_plan(ListPlan([ChannelList(2, segments=[ramp(), ramp(), periodic(frequency=0)])]))
    assert c.errors == ["CH2 segment 3: frequency must be > 0"]
