import pytest
from n6705c_seq.gui import waveform
from n6705c_seq.plan import SeqRow


def test_waveform_corners_follow_delay_rise_hold_fall():
    row = SeqRow(1, True, "A", 0.005, 0.0, 5.0, 0.002, 0.055, 0.002)
    pts = waveform(row, 0.1)
    assert [v for _, v in pts] == [0, 0, 5, 5, 0, 0]
    assert [t for t, _ in pts] == pytest.approx([0, 0.005, 0.007, 0.062, 0.064, 0.1])
