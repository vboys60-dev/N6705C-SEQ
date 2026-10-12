# N6705C-SEQ Implementation Plan

> **Status (2026-10-12):** Tasks 1–5 are implemented; 28 tests pass on the simulator; **never run on real hardware**.
> The test code blocks below are the original drafts — `tests/` is authoritative. See `HANDOFF.md` for design decisions and open risks.

**Goal:** CLI that reads a power-up sequencing CSV and runs it across two (any number of) N6705C mainframes, and suggests a channel re-assignment when cross-mainframe start times are too close to order reliably.

**Architecture:** Thin layer over the existing `n6705c` core (installed editable from `../N6705C-APP`). `plan.py` turns CSV rows into `ChannelFunction`/`TrapezoidArb` and finds/fixes cross-frame ties; `execute.py` drives `GroupArbRunner` but fires the per-frame `*TRG` from concurrent threads; `cli.py` exposes `check` (with `--suggest-out`) and `run`.

**Tech Stack:** Python >=3.11 stdlib (`csv`, `argparse`, `threading`), `n6705c-app` core, pytest.

## Global Constraints

- No new third-party dependencies (CSV, not YAML).
- Channel number = CSV data-row order (1..N); frame = `GlobalChannel.from_index` (4 channels per frame).
- CSV columns: `enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms[,current_limit_a]`; times in ms in the file, seconds internally.
- Cross-frame start is NOT synchronised (`TRIGGER_SKEW_SECONDS` = 2 ms from `n6705c.core.group`); this must be reported, never hidden.
- All validation happens before any output is switched on.
- Core (`n6705c`) is never modified.
- Delay lives in `TrapezoidArb.start_time` only; `ChannelFunction.delay` stays 0 (the arb runs once, so setting both delays twice).

## File Structure

- `pyproject.toml` — package + `n6705c-seq` console script
- `src/n6705c_seq/__init__.py` — empty
- `src/n6705c_seq/plan.py` — `SeqRow`, `load_csv`, `write_csv`, `to_functions`, `order_warnings`, `suggest_assignment`, `timeline`
- `src/n6705c_seq/execute.py` — `fire_together`, `run_plan`
- `src/n6705c_seq/cli.py` — `main`
- `examples/powerup.csv` — the 8-channel table from the screenshot
- `tests/test_plan.py`, `tests/test_execute.py`, `tests/test_cli.py`

---

### Task 1: CSV → rows → plan (`plan.py`)

**Files:** Create `pyproject.toml`, `src/n6705c_seq/__init__.py`, `src/n6705c_seq/plan.py`, `examples/powerup.csv`; Test `tests/test_plan.py`

**Interfaces:**
- Produces: `SeqRow(channel:int, enabled:bool, name:str, delay:float, low_v:float, high_v:float, rise:float, hold:float, fall:float, current_limit:float|None)` (seconds/volts/amps);
  `load_csv(path) -> list[SeqRow]` (raises `ValueError("line N, column X: ...")`);
  `to_functions(rows) -> list[ChannelFunction]` (enabled rows only);
  `order_warnings(rows, skew: float) -> list[str]`; `timeline(rows) -> str`.
  (Task 5 adds `suggest_assignment` and `write_csv`.)

- [x] **Step 1: Scaffold + failing tests** — `tests/test_plan.py`:

```python
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
        load_csv(write(tmp_path, "1,A,1,0,1,8,3\n", "enabled,name,delay_ms,low_v,high_v,rise_ms,fall_ms\n"))


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
    body = "".join(f"1,CH{i},{d},0,1,1,1,1,\n" for i, d in enumerate([1, 21, 21, 29, 29, 37], 1))
    warnings = order_warnings(load_csv(write(tmp_path, body)), skew=0.002)
    assert len(warnings) == 1 and "CH4" in warnings[0] and "CH5" in warnings[0]


def test_timeline_lists_every_enabled_channel(tmp_path):
    rows = load_csv(write(tmp_path, "1,A,1,0,1.8,8,80,3,\n0,B,5,0,5,2,30,2,\n"))
    text = timeline(rows)
    assert "A" in text and "B" not in text and "F1:CH1" in text
```

- [x] **Step 2:** `cd /Users/lianshi/Desktop/claude-cowork/N6705C-SEQ && python3 -m pytest tests/test_plan.py -q` → FAIL (module missing).
- [x] **Step 3: Implement `plan.py`** (code in the file; key rules): header must contain all required columns; `enabled` accepts `1/0/true/false/yes/no`; numeric fields parsed with `float`, voltages/times must be >= 0; `current_limit_a` blank/absent → `None`; `to_functions` builds `ChannelFunction(channel, voltage=low_v, current=limit or 0.0, output_on=True, arb_enabled=True, delay=0.0, spec=ArbSpec(channel, TrapezoidArb(start_level=low_v, start_time=delay, rise_time=rise, top_level=high_v, top_time=hold, fall_time=fall, end_time=0.0), terminate_last=True))`; `order_warnings` pairs enabled rows on different `GlobalChannel.frame` whose `abs(delay_a-delay_b) < skew`.
- [x] **Step 4:** rerun → PASS.

### Task 2: Execution with concurrent frame trigger (`execute.py`)

**Files:** Create `src/n6705c_seq/execute.py`; Test `tests/test_execute.py`

**Interfaces:**
- Consumes: Task 1 `SeqRow`, `to_functions`; `InstrumentGroup`, `GroupArbRunner.from_plan(group, plan)`, `MultiArbRunner.trigger()`, `GlobalChannel`.
- Produces: `fire_together(runners: list[MultiArbRunner]) -> None`;
  `run_plan(group: InstrumentGroup, rows: list[SeqRow], *, wait: bool = True) -> GroupArbRunner`.

Behaviour of `run_plan` (order matters):
1. Enabled rows → plan; reject if a channel's frame > `group.frame_count` (`ValueError`, nothing sent).
2. `GroupArbRunner.from_plan` (runner constructors validate against module capability) — still nothing switched on.
3. Per frame: `VOLT low,(@ch)`, optional `CURR lim,(@ch)` via `session.write_checked`, then `set_output(on)`.
4. `runner.load()`, `runner.arm()` (all-or-none), `fire_together(group_runner.runners)`, set `group_runner.state = ArbState.RUNNING`.
5. If `wait`: sleep `total_duration`, then poll `running_channels` (50 ms steps, 2 s grace); finally `group.all_outputs_off()`.
6. Any exception after step 3 → `group.all_outputs_off()` then re-raise.

- [x] **Step 1: Failing tests** — `tests/test_execute.py`:

```python
import pytest
from n6705c.core.group import InstrumentGroup
from n6705c_seq.execute import run_plan
from n6705c_seq.plan import SeqRow


def row(ch, delay=0.001, high=1.0, limit=None, enabled=True):
    return SeqRow(ch, enabled, f"CH{ch}", delay, 0.0, high, 0.002, 0.01, 0.002, limit)


def sent(instrument):
    return [h.text for h in instrument.session.history if h.direction == "->"]


def test_each_frame_gets_one_trigger_and_its_own_channels():
    group = InstrumentGroup.demo(frames=2)
    run_plan(group, [row(c) for c in range(1, 9)], wait=True)
    for frame, inst in enumerate(group.instruments):
        cmds = sent(inst)
        assert cmds.count("*TRG") == 1
        assert any("ARB" in c for c in cmds)


def test_current_limit_is_sent_only_when_given():
    group = InstrumentGroup.demo(frames=1)
    run_plan(group, [row(1, limit=0.5), row(2)], wait=True)
    cmds = sent(group.instruments[0])
    assert any(c.startswith("CURR 0.5,(@1)") for c in cmds)
    assert not any(c.startswith("CURR") and "(@2)" in c for c in cmds)


def test_too_few_frames_is_refused_before_anything_is_switched_on():
    group = InstrumentGroup.demo(frames=1)
    with pytest.raises(ValueError, match="frame"):
        run_plan(group, [row(c) for c in range(1, 6)], wait=False)
    assert not any(c.startswith("OUTP ON") for c in sent(group.instruments[0]))


def test_voltage_above_module_limit_is_refused_before_output_on():
    group = InstrumentGroup.demo(frames=1)
    with pytest.raises(ValueError):
        run_plan(group, [row(1, high=500.0)], wait=False)
    assert not any(c.startswith("OUTP ON") for c in sent(group.instruments[0]))


def test_outputs_are_off_after_the_run():
    group = InstrumentGroup.demo(frames=2)
    run_plan(group, [row(1), row(5)], wait=True)
    for inst in group.instruments:
        assert not any(inst.output_state(inst.populated_channels).values())
```

- [x] **Step 2:** run → FAIL. **Step 3:** implement `execute.py`; `fire_together` uses `threading.Barrier(len(runners))` and one thread per runner, collects exceptions and re-raises the first (`# ponytail: host-side threads, sub-ms jitter remains; digital-port wiring (M11.3) is the real fix`). **Step 4:** run → PASS.
- [x] **Step 5:** adjust test fixtures only if the demo simulator's module limit differs from the assumption (check `group.capabilities[1].voltage_max`).

### Task 3: CLI (`cli.py`)

**Files:** Create `src/n6705c_seq/cli.py`; Test `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: `main(argv: list[str] | None = None) -> int`; console script `n6705c-seq`.
  - `check PLAN.csv [--suggest-out FILE]` → prints timeline, skew warnings and a suggested assignment (Task 5), exit 0; exit 2 on a bad CSV (message to stderr); exit 1 if `--suggest-out` is given but no assignment exists.
  - `run PLAN.csv (--resource R [--resource R2 ...] | --demo) [--backend B] [--timeout MS] [--yes]` → prints timeline + warnings, asks `Switch outputs ON and run? [y/N]` unless `--yes`, then `run_plan`; Ctrl-C → `group.all_outputs_off()`, exit 130; any other error → outputs forced off, exit 1. `--demo` uses `InstrumentGroup.demo(frames=ceil(channels/4))`.

- [x] **Step 1: Failing tests** — `tests/test_cli.py`:

```python
from pathlib import Path
from n6705c_seq.cli import main

EXAMPLE = Path(__file__).parent.parent / "examples" / "powerup.csv"


def test_check_prints_the_cross_frame_warning(capsys):
    assert main(["check", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "CH4" in out and "CH5" in out and "F2:CH1" in out


def test_check_rejects_a_bad_file_with_a_line_number(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms\n1,A,x,0,1,1,1,1\n")
    assert main(["check", str(bad)]) == 2
    assert "line 2" in capsys.readouterr().err


def test_run_demo_with_yes_completes(capsys):
    assert main(["run", str(EXAMPLE), "--demo", "--yes"]) == 0


def test_run_without_yes_and_without_consent_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main(["run", str(EXAMPLE), "--demo"]) == 1
```

- [x] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS.
- [x] **Step 5:** `examples/powerup.csv` = the screenshot table (8 rows, CH7/CH8 disabled), with `current_limit_a` blank for all rows except none.

### Task 4: Verify

- [x] `python3 -m pytest -q` all green; `n6705c-seq check examples/powerup.csv`; `n6705c-seq run examples/powerup.csv --demo --yes`.
- [x] Invoke `verification-before-completion` before reporting done.

### Task 5: Cross-frame tie suggestion (`plan.py`, `cli.py`)

Added after the first three tasks; not in the original draft.

**Files:** Modify `src/n6705c_seq/plan.py`, `src/n6705c_seq/cli.py`; Test `tests/test_plan.py`, `tests/test_cli.py`

**Interfaces:**
- `_tied(a, b, skew) -> bool` — `round(abs(a.delay - b.delay), 9) < skew`. Rounding is required: `0.031 - 0.029` is `0.00199999…` in floats, so a gap of exactly the skew would otherwise count as a tie.
- `suggest_assignment(rows, skew, frames, per_frame=4) -> (new_rows, moves) | None` — channels closer than the skew (chained: A~B, B~C → all three) form a group that must share a frame, because one `*TRG` starts a whole frame at once. Groups are packed contiguously in delay order, falling back to first-fit-decreasing; `None` if a group exceeds `per_frame` or nothing fits. Unused slots in non-final frames are filled with disabled spare rows so frame 2 still starts at row 5. `moves` is `[(old_row, new_channel)]`.
- `write_csv(rows, path)` — round-trips with `load_csv`.
- `cli._report` prints the timeline and warnings; on ties it prints the suggestion (or `No frame assignment ...` when none exists) and, with `--suggest-out`, writes the file.

- [x] Tests: `test_a_gap_of_exactly_the_skew_is_not_a_tie`, `test_suggestion_for_the_example_table_puts_the_two_29ms_rails_together`, `test_unused_slots_are_filled_so_frame_two_starts_on_row_five`, `test_a_tie_group_bigger_than_a_frame_has_no_solution`, `test_packing_falls_back_when_contiguous_filling_needs_too_many_frames`, `test_written_csv_loads_back_the_same`, and the three `--suggest-out` CLI tests.
- [x] The user must re-wire the DUT rails to the new channels; the tool cannot do that.

## Known limits (not tasks)

- Real-hardware behaviour of `TRAP:END:TIM 0` and of the concurrent trigger is unverified; first real run should be `--resource` with a dummy load.
- Cross-frame skew stays 0.2–2 ms until the digital ports are wired; `2 ms` is a conservative estimate, not a measurement.
- The suggested assignment only considers start order, not the hold/fall phases, and does not know which rail is wired to which channel.
- Not done, not requested: a `--allow-skew` flag that turns ties into errors, measuring the host-side `*TRG` spacing, hardware sync wiring (M11.3), measurement/logging, YAML config.
