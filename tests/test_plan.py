import pytest
from n6705c.core.arb import TrapezoidArb
from n6705c_seq.plan import load_csv, order_warnings, timeline, to_functions

HEADER = "enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms,current_limit_a\n"


def write(tmp_path, body, header=HEADER):
    path = tmp_path / "p.csv"
    path.write_text(header + body)
    return path


def test_ms_become_seconds_and_row_order_is_channel(tmp_path):
    rows = load_csv(write(tmp_path, "1,A,1,0,1.8,8,80,3,0.5\n0,B,5,0,5,2,30,2,\n"))
    assert [r.channel for r in rows] == [1, 2]
    assert rows[0].delay == pytest.approx(0.001) and rows[0].hold == pytest.approx(0.08)
    assert rows[0].current_limit == 0.5 and rows[1].current_limit is None
    assert rows[1].enabled is False


def test_current_limit_column_is_optional(tmp_path):
    header = HEADER.replace(",current_limit_a", "")
    rows = load_csv(write(tmp_path, "1,A,1,0,1.8,8,80,3\n", header))
    assert rows[0].current_limit is None


def test_errors_name_the_line_and_column(tmp_path):
    with pytest.raises(ValueError, match=r"line 2.*high_v"):
        load_csv(write(tmp_path, "1,A,1,0,abc,8,80,3,\n"))
    with pytest.raises(ValueError, match=r"line 2.*delay_ms.*negative"):
        load_csv(write(tmp_path, "1,A,-1,0,1,8,80,3,\n"))


def test_missing_column_is_reported(tmp_path):
    with pytest.raises(ValueError, match="hold_ms"):
        load_csv(
            write(
                tmp_path,
                "1,A,1,0,1,8,3\n",
                "enabled,name,delay_ms,low_v,high_v,rise_ms,fall_ms\n",
            )
        )


def test_only_enabled_rows_become_trapezoids_with_delay_as_start_time(tmp_path):
    rows = load_csv(write(tmp_path, "1,A,1,0,1.8,8,80,3,0.5\n0,B,5,0,5,2,30,2,\n"))
    (fn,) = to_functions(rows)
    wf = fn.spec.waveform
    assert isinstance(wf, TrapezoidArb)
    assert (wf.start_level, wf.top_level) == (0, 1.8)
    assert wf.start_time == pytest.approx(0.001) and wf.top_time == pytest.approx(0.08)
    assert fn.channel == 1 and fn.voltage == 0 and fn.current == 0.5
    assert fn.spec.terminate_last and fn.delay == 0  # delay is in start_time, not twice


def test_cross_frame_ties_are_flagged_but_same_frame_ties_are_not(tmp_path):
    # CH2/CH3 share a delay but live on frame 1; CH4 (29 ms) vs CH5 (29 ms) cross frames.
    body = "".join(
        f"1,CH{i},{d},0,1,1,1,1,\n" for i, d in enumerate([1, 21, 21, 29, 29, 37], 1)
    )
    warnings = order_warnings(load_csv(write(tmp_path, body)), skew=0.002)
    assert len(warnings) == 1 and "CH4" in warnings[0] and "CH5" in warnings[0]


def test_timeline_lists_every_enabled_channel(tmp_path):
    rows = load_csv(write(tmp_path, "1,A,1,0,1.8,8,80,3,\n0,B,5,0,5,2,30,2,\n"))
    text = timeline(rows)
    assert "A" in text and "B" not in text and "F1:CH1" in text


# -- cross-frame suggestion ---------------------------------------------------

from pathlib import Path

from n6705c_seq.plan import SeqRow, suggest_assignment, write_csv

EXAMPLE = Path(__file__).parent.parent / "examples" / "powerup.csv"
SKEW = 0.002


def spec_row(ch, delay_ms, enabled=True):
    return SeqRow(ch, enabled, f"R{ch}", delay_ms / 1000, 0, 1, 0.001, 0.01, 0.001, None)


def test_a_gap_of_exactly_the_skew_is_not_a_tie():
    # 29 ms vs 31 ms: 0.031 - 0.029 is 0.00199999... in floating point
    rows = [spec_row(1, 1), spec_row(2, 2), spec_row(3, 3), spec_row(4, 29), spec_row(5, 31)]
    assert order_warnings(rows, SKEW) == []  # R4 (frame 1) vs R5 (frame 2), gap == skew


def test_suggestion_for_the_example_table_puts_the_two_29ms_rails_together():
    rows = load_csv(EXAMPLE)
    assert order_warnings(rows, SKEW)  # the problem exists
    result = suggest_assignment(rows, SKEW, frames=2)
    new_rows, moves = result
    assert order_warnings(new_rows, SKEW) == []
    by_name = {r.name: r.channel for r in new_rows}
    assert [by_name[n] for n in ("CH1", "CH2", "CH3")] == [1, 2, 3]  # frame 1
    assert [by_name[n] for n in ("CH4", "CH5", "CH6")] == [5, 6, 7]  # frame 2
    assert [(r.name, n) for r, n in moves] == [("CH4", 5), ("CH5", 6), ("CH6", 7)]


def test_unused_slots_are_filled_so_frame_two_starts_on_row_five():
    new_rows, _ = suggest_assignment(load_csv(EXAMPLE), SKEW, frames=2)
    assert [r.channel for r in new_rows] == list(range(1, len(new_rows) + 1))
    assert new_rows[3].enabled is False  # slot F1:CH4 is now a disabled spare


def test_a_tie_group_bigger_than_a_frame_has_no_solution():
    rows = [spec_row(c, 10) for c in range(1, 6)]  # five rails that must start together
    assert suggest_assignment(rows, SKEW, frames=2) is None


def test_packing_falls_back_when_contiguous_filling_needs_too_many_frames():
    # groups of 2, 3, 2 on two frames: contiguous fill needs three, 3 | 2+2 fits
    rows = [spec_row(1, 0), spec_row(2, 0)]
    rows += [spec_row(c, 10) for c in (3, 4, 5)]
    rows += [spec_row(c, 20) for c in (6, 7)]
    new_rows, _ = suggest_assignment(rows, SKEW, frames=2)
    assert order_warnings(new_rows, SKEW) == []
    assert max(r.channel for r in new_rows if r.enabled) <= 8


def test_written_csv_loads_back_the_same(tmp_path):
    rows = load_csv(EXAMPLE)
    path = tmp_path / "out.csv"
    write_csv(rows, path)
    again = load_csv(path)
    assert len(again) == len(rows)
    for a, b in zip(rows, again):
        assert (a.channel, a.enabled, a.name, a.current_limit) == (
            b.channel, b.enabled, b.name, b.current_limit,
        )
        assert (a.delay, a.low_v, a.high_v, a.rise, a.hold, a.fall) == pytest.approx(
            (b.delay, b.low_v, b.high_v, b.rise, b.hold, b.fall)
        )


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_non_finite_numbers_are_rejected(tmp_path, bad):
    path = tmp_path / "p.csv"
    path.write_text(f"enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms\n1,A,{bad},0,1,1,1,1\n")
    with pytest.raises(ValueError, match="delay_ms.*finite"):
        load_csv(path)


def test_a_zero_current_limit_is_rejected_not_silently_dropped(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text("enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms,current_limit_a\n1,A,1,0,1,1,1,1,0\n")
    with pytest.raises(ValueError, match="current_limit_a.*0 A"):
        load_csv(path)
