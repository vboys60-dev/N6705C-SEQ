from pathlib import Path

from n6705c_seq.cli import main

EXAMPLE = Path(__file__).parent.parent / "examples" / "powerup.csv"


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
