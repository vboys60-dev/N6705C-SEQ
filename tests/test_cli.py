from pathlib import Path

import pytest
from n6705c_seq.cli import main

EXAMPLE = Path(__file__).parent.parent / "examples" / "powerup.csv"


@pytest.mark.parametrize("status", [0, 1, 130])
def test_cli_closes_every_instrument_and_preserves_status(monkeypatch, capsys, status):
    from types import SimpleNamespace
    from n6705c_seq import cli

    closed = []
    def close(index):
        closed.append(index)
        if index == 1:
            raise OSError("first close failed")
    group = SimpleNamespace(instruments=[SimpleNamespace(close=lambda i=i: close(i)) for i in (1, 2)])
    monkeypatch.setattr(cli, "_group", lambda *args: group)
    def run(*args, **kwargs):
        if status == 1:
            raise RuntimeError("run failed")
        if status == 130:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli, "run_plan", run)
    assert main(["run", str(EXAMPLE), "--demo", "--yes"]) == (status or 1)
    assert closed == [1, 2]
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "first close failed" in captured.err
    assert "Sequence done" not in captured.out


def test_check_prints_the_cross_frame_warning(capsys):
    assert main(["check", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "F1:CH4" in out and "F2:CH1" in out and "not guaranteed" in out


def test_check_rejects_a_bad_file_with_a_line_number(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms\n1,A,x,0,1,1,1,1\n"
    )
    assert main(["check", str(bad)]) == 2
    assert "line 2" in capsys.readouterr().err


def test_run_demo_with_yes_completes(capsys):
    assert main(["run", str(EXAMPLE), "--demo", "--yes"]) == 0
    assert "done" in capsys.readouterr().out.lower()


def test_run_without_consent_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main(["run", str(EXAMPLE), "--demo"]) == 1


def test_run_needs_a_target(capsys):
    try:
        main(["run", str(EXAMPLE)])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse to demand --demo or --resource")


def test_check_suggests_a_frame_assignment_for_the_tie(capsys):
    assert main(["check", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "Suggested" in out and "CH4: F1:CH4 -> F2:CH1" in out


def test_suggest_out_writes_a_table_that_checks_clean(tmp_path, capsys):
    fixed = tmp_path / "fixed.csv"
    assert main(["check", str(EXAMPLE), "--suggest-out", str(fixed)]) == 0
    capsys.readouterr()
    assert main(["check", str(fixed)]) == 0
    out = capsys.readouterr().out
    assert "WARNING" not in out and "Suggested" not in out


def test_suggest_out_with_no_solution_fails_and_writes_nothing(tmp_path, capsys):
    plan = tmp_path / "tie5.csv"
    plan.write_text(
        "enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms\n"
        + "".join(f"1,R{i},10,0,1,1,1,1\n" for i in range(1, 6))
    )
    out_file = tmp_path / "x.csv"
    assert main(["check", str(plan), "--suggest-out", str(out_file)]) == 1
    assert "No frame assignment" in capsys.readouterr().out
    assert not out_file.exists()


def test_suggest_out_still_writes_a_file_when_there_is_nothing_to_fix(tmp_path, capsys):
    table = tmp_path / "ok.csv"
    table.write_text("enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms\n1,A,1,0,1,1,1,1\n")
    out = tmp_path / "out.csv"
    assert main(["check", str(table), "--suggest-out", str(out)]) == 0
    assert out.exists()


def test_run_hold_dc_says_the_outputs_stay_on(capsys):
    assert main(["run", str(EXAMPLE), "--demo", "--yes", "--hold-dc"]) == 0
    assert "left on" in capsys.readouterr().out.lower()


def test_run_rejects_mixed_models_before_opening_runners(monkeypatch, capsys):
    import pytest
    from n6705c_seq import cli

    models = {"first": "N6705C", "second": "E36441A"}
    monkeypatch.setattr(cli, "detect_model", lambda resource, **kwargs: models[resource])
    monkeypatch.setattr(cli, "connect_group", lambda *a, **k: pytest.fail("settings path opened"))
    monkeypatch.setattr(cli, "run_resources", lambda *a, **k: pytest.fail("settings path opened"))
    assert main(["run", str(EXAMPLE), "--resource", "first", "--resource", "second", "--yes"]) == 2
    assert "mixed instrument models" in capsys.readouterr().err


def test_cli_prints_slew_warning_and_completes(monkeypatch, capsys):
    from n6705c.core.group import InstrumentGroup
    from n6705c_seq import cli, i18n

    group = InstrumentGroup.demo(frames=2, slots=cli.DEMO_SLOTS)
    group.instruments[0].session.write_checked("VOLT:SLEW 2,(@1)")
    monkeypatch.setattr(cli, "_group", lambda args, rows: group)
    assert main(["run", str(EXAMPLE), "--demo", "--yes"]) == 0
    out = capsys.readouterr().out
    warning = i18n.tr("slew_warning", channel="F1:CH1", actual=2.0, required=599.9999999999994)
    assert f"WARNING: {warning}" in out
    assert out.index(warning) < out.index("Sequence done")


def test_e36441a_ctrl_c_reports_interrupt(monkeypatch, capsys):
    from types import SimpleNamespace
    from n6705c_seq import cli

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_resources", interrupt)
    assert cli._run_e36441a(SimpleNamespace(resource=["x"], hold_dc=False, backend=None, timeout=1000), []) == 130
    assert "interrupted" in capsys.readouterr().err
