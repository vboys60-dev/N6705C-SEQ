from dataclasses import replace

import pytest

from n6705c_seq import e36441a
from n6705c_seq.e36441a import (
    arm_commands, check, configure_commands, prepare_commands, restore_commands,
)
from n6705c_seq.plan import SeqRow

ROW = SeqRow(7, True, "rail", 0.125, 1, 5, 0.2, 0.3, 0.8, 2)


def test_trapezoid_command_order_and_local_channel():
    assert configure_commands(ROW, 2) == [
        "TRIG:SOUR BUS,(@2)",
        "TRIG:DEL 0.125,(@2)",
        "VOLT:SLEW:RIS 20,(@2)",
        "VOLT:SLEW:FALL 5,(@2)",
        "LIST:VOLT 5,1,(@2)",
        "LIST:DWEL 0.500,0.800,(@2)",
        "LIST:CURR 2,(@2)",
        "LIST:TOUT:BOST 0,(@2)",
        "LIST:TOUT:EOST 0,(@2)",
        "LIST:COUN 1,(@2)",
        "LIST:STEP AUTO,(@2)",
        "LIST:TERM:LAST ON,(@2)",
        "VOLT:MODE LIST,(@2)",
        "CURR:MODE FIX,(@2)",
    ]
    assert check(ROW) == ([], [])


def test_downward_first_edge_swaps_slew_directions():
    commands = configure_commands(replace(ROW, low_v=5, high_v=1), 1)
    assert commands[2:5] == [
        "VOLT:SLEW:RIS 5,(@1)", "VOLT:SLEW:FALL 20,(@1)", "LIST:VOLT 1,5,(@1)",
    ]


@pytest.mark.parametrize("changes, expected", [
    ({"high_v": 1}, ["INF", "INF"]),
    ({"rise": 0}, ["INF", "5"]),
    ({"fall": 0}, ["20", "INF"]),
])
def test_zero_delta_or_edge_time_uses_fastest_slew(changes, expected):
    commands = configure_commands(replace(ROW, **changes), 1)
    assert commands[2:4] == [
        f"VOLT:SLEW:RIS {expected[0]},(@1)", f"VOLT:SLEW:FALL {expected[1]},(@1)",
    ]


def test_hold_dc_is_one_point_and_keeps_last_value():
    commands = configure_commands(replace(ROW, current_limit=None), 4, hold_dc=True)
    assert commands[2:4] == [
        "VOLT:SLEW:RIS 20,(@4)", "VOLT:SLEW:FALL INF,(@4)",
    ]
    assert commands[4:7] == [
        "LIST:VOLT 5,(@4)", "LIST:DWEL 0.200,(@4)", "LIST:CURR 10.3,(@4)",
    ]
    assert "LIST:TERM:LAST ON,(@4)" in commands


def test_hold_dc_downward_ramp_uses_only_falling_slew():
    commands = configure_commands(replace(ROW, low_v=5, high_v=1), 1, hold_dc=True)
    assert commands[2:6] == [
        "VOLT:SLEW:RIS INF,(@1)", "VOLT:SLEW:FALL 20,(@1)",
        "LIST:VOLT 1,(@1)", "LIST:DWEL 0.200,(@1)",
    ]


def test_dwell_floor_and_millisecond_quantization():
    row = replace(ROW, delay=0.1236, rise=0.001, hold=0.002, fall=0.1236)
    commands = configure_commands(row, 1)
    assert commands[1] == "TRIG:DEL 0.124,(@1)"
    assert commands[5] == "LIST:DWEL 0.010,0.124,(@1)"
    assert configure_commands(replace(row, fall=0), 1)[5] == "LIST:DWEL 0.010,0.010,(@1)"


@pytest.mark.parametrize("hold_dc", [False, True])
def test_large_times_preserve_millisecond_resolution(hold_dc):
    row = replace(ROW, delay=1234.568, rise=1000.123, hold=0, fall=1000.123)
    commands = configure_commands(row, 1, hold_dc=hold_dc)
    assert commands[1] == "TRIG:DEL 1234.568,(@1)"
    expected = "1000.123" if hold_dc else "1000.123,1000.123"
    assert commands[5] == f"LIST:DWEL {expected},(@1)"


def test_prepare_sets_fast_slew_before_low_and_optional_current():
    assert prepare_commands(ROW, 3) == [
        "ABOR (@3)",
        "INIT:CONT OFF,(@3)",
        "VOLT:SLEW:RIS INF,(@3)",
        "VOLT:SLEW:FALL INF,(@3)",
        "VOLT 1,(@3)",
        "CURR 2,(@3)",
        "CURR:MODE FIX,(@3)",
        "VOLT:MODE FIX,(@3)",
        "OUTP ON,(@3)",
    ]
    assert not any(c.startswith("CURR ") for c in prepare_commands(replace(ROW, current_limit=None), 3))


def test_arm_and_restore():
    assert arm_commands([1, 3, 4]) == ["INIT (@1,3,4)"]
    assert restore_commands(2) == [
        "ABOR (@2)", "OUTP OFF,(@2)", "VOLT:SLEW:RIS INF,(@2)",
        "VOLT:SLEW:FALL INF,(@2)", "LIST:TERM:LAST OFF,(@2)", "VOLT:MODE FIX,(@2)",
    ]


@pytest.mark.parametrize("changes, message", [
    ({"low_v": -0.1}, "low_v"),
    ({"low_v": 32.01}, "low_v"),
    ({"high_v": -0.1}, "high_v"),
    ({"high_v": 32.01}, "high_v"),
    ({"current_limit": 0}, "current_limit"),
    ({"current_limit": -1}, "current_limit"),
    ({"current_limit": 10.01}, "current_limit"),
    ({"delay": 3600.0001}, "delay"),
    ({"rise": 3000}, "rise slew"),
    ({"fall": 3000}, "fall slew"),
    ({"rise": 1800, "hold": 1800.0001}, "rise+hold dwell"),
    ({"fall": 3600.0001}, "fall dwell"),
    ({"high_v": 32, "current_limit": 5.01}, "160 W"),
])
def test_check_errors(changes, message):
    errors, _ = check(replace(ROW, **changes))
    assert any(message in e for e in errors)


@pytest.mark.parametrize("changes, message", [
    ({"rise": 0.079}, "typical rising settling"),
    ({"fall": 0.499}, "no-load falling settling"),
    ({"rise": 0.001, "hold": 0.001}, "rise+hold dwell"),
    ({"fall": 0.009}, "fall dwell"),
    ({"rise": 0}, "rise is 0: fastest"),
    ({"fall": 0}, "fall is 0: fastest"),
    ({"delay": 0.1234}, "delay will be rounded"),
])
def test_check_warnings(changes, message):
    errors, warnings = check(replace(ROW, **changes))
    assert errors == []
    assert any(message in w for w in warnings)


def test_check_boundaries_and_no_limit():
    row = replace(ROW, low_v=0, high_v=32, current_limit=5, delay=3600, rise=0.08, fall=0.5)
    assert check(row) == ([], [])
    assert check(replace(row, current_limit=None)) == ([], [])
    assert check(replace(ROW, high_v=8.2, rise=3600, hold=0, fall=3600)) == ([], [])
    assert check(replace(ROW, high_v=1, rise=3600, hold=0, fall=3600)) == ([], [])
    assert check(replace(ROW, rise=0.01, hold=0, fall=0.01))[1] == [
        "rise is below 0.08 s (typical rising settling)",
        "fall is below 0.5 s (no-load falling settling)",
    ]


@pytest.mark.parametrize("fall", [4000, 0.499, 0.009, 0])
def test_hold_dc_check_ignores_fall(fall):
    assert check(replace(ROW, fall=fall), hold_dc=True) == ([], [])


def test_hold_dc_check_ignores_hold_for_dwell_limit_and_floor():
    assert check(replace(ROW, hold=4000), hold_dc=True) == ([], [])
    errors, warnings = check(replace(ROW, rise=0.001, hold=4000), hold_dc=True)
    assert errors == []
    assert warnings == [
        "rise is below 0.08 s (typical rising settling)",
        "rise dwell will be raised to 10 ms",
    ]


def test_hold_dc_check_still_validates_rise_dwell_limit():
    errors, warnings = check(replace(ROW, high_v=ROW.low_v, rise=4000), hold_dc=True)
    assert errors == ["rise dwell exceeds 3600 s"]
    assert warnings == []


class _FakeSession:
    def __init__(self):
        self.sent = []

    def write(self, cmd):
        self.sent.append(cmd)

    write_checked = write

    def check_errors(self, context=""):
        pass


def test_model_of():
    assert e36441a.model_of("Keysight Technologies,E36441A,MY1,1.0") == "E36441A"
    assert e36441a.model_of("garbage") == ""


def test_run_rows_sequence_and_restore(monkeypatch):
    monkeypatch.setattr(e36441a.time, "sleep", lambda s: None)
    s = _FakeSession()
    row = SeqRow(2, True, "A", 0.0, 0.0, 3.3, 0.1, 0.1, 0.1)
    e36441a.run_rows([s], [row])
    i = s.sent.index("INIT (@2)")
    assert s.sent.index("*TRG") > i
    assert s.sent[-1] == "VOLT:MODE FIX,(@2)" and "OUTP OFF,(@2)" in s.sent


def test_run_rows_hold_dc_leaves_on(monkeypatch):
    monkeypatch.setattr(e36441a.time, "sleep", lambda s: None)
    s = _FakeSession()
    e36441a.run_rows([s], [SeqRow(1, True, "A", 0.0, 0.0, 3.3, 0.1, 0.0, 0.0)], hold_dc=True)
    assert "OUTP OFF,(@1)" not in s.sent


def test_run_rows_rejects_before_output(monkeypatch):
    import pytest
    s = _FakeSession()
    with pytest.raises(Exception):
        e36441a.run_rows([s], [SeqRow(5, True, "A", 0.0, 0.0, 3.3, 0.1, 0.1, 0.1)])
    assert s.sent == []
