import pytest
from n6705c_seq.gui import _tick_step, waveform
from n6705c_seq.plan import SeqRow


def test_waveform_corners_follow_delay_rise_hold_fall():
    row = SeqRow(1, True, "A", 0.005, 0.0, 5.0, 0.002, 0.055, 0.002)
    pts = waveform(row, 0.1)
    assert [v for _, v in pts] == [0, 0, 5, 5, 0, 0]
    assert [t for t, _ in pts] == pytest.approx([0, 0.005, 0.007, 0.062, 0.064, 0.1])


@pytest.mark.parametrize("span, step", [(8, 1), (95.6, 10), (60_000, 10_000), (3_600_000, 500_000)])
def test_tick_step_covers_long_sequences(span, step):
    assert _tick_step(span) == step


def test_config_with_the_wrong_shape_falls_back_to_defaults(tmp_path, monkeypatch):
    from n6705c_seq import gui

    default = {"resources": [], "demo": True}
    for text in ('{"resources":"TCPIP::h","demo":false}', '{"resources":[],"demo":"false"}', "[]"):
        f = tmp_path / "c.json"
        f.write_text(text)
        monkeypatch.setattr(gui, "CONFIG", f)
        assert gui._load_config() == default
    f.write_text('{"resources":["A"],"demo":false}')
    assert gui._load_config() == {"resources": ["A"], "demo": False}
