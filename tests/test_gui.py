import gc
import subprocess
import sys
from functools import lru_cache

import pytest
from n6705c_seq.gui import _tick_step, waveform
from n6705c_seq.plan import SeqRow
from n6705c_seq import i18n


@pytest.fixture(autouse=True)
def reset_language():
    i18n.set_lang("zh")
    yield
    i18n.set_lang("zh")
    gc.collect()  # drop dead Tk variables here, not later in some worker thread


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    """Workers probe ``*IDN?`` first; the fake resources here answer N6705C."""
    from n6705c_seq import e36441a, gui

    def use(model):
        for module in (e36441a, gui):
            monkeypatch.setattr(module, "detect_model", lambda *a, **k: model)

    use("N6705C")
    return use


@pytest.fixture
def english():
    i18n.set_lang("en")


@lru_cache(maxsize=1)
def _tk_display_available():
    probe = subprocess.run([sys.executable, "-c", "import tkinter as tk; r = tk.Tk(); r.destroy()"],
                           capture_output=True)
    return probe.returncode == 0


def _tk_root():
    import tkinter as tk

    if not _tk_display_available():
        pytest.skip("Tk display unavailable")
    return tk.Tk()


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

    default = {"resources": [], "demo": True, "lang": "zh"}
    for text in ('{"resources":"TCPIP::h","demo":false}', '{"resources":[],"demo":"false"}', "[]"):
        f = tmp_path / "c.json"
        f.write_text(text)
        monkeypatch.setattr(gui, "CONFIG", f)
        assert gui._load_config() == default
    f.write_text('{"resources":["A"],"demo":false}')
    assert gui._load_config() == {"resources": ["A"], "demo": False, "lang": "zh"}


def test_hold_dc_waveform_stays_at_the_high_level():
    row = SeqRow(1, True, "A", 0.005, 0.0, 5.0, 0.002, 0.055, 0.002)
    assert [v for _, v in waveform(row, 0.1, hold_dc=True)] == [0, 0, 5, 5]


def test_all_off_worker_reports_success_and_closes(monkeypatch):
    import queue
    from n6705c.core.group import InstrumentGroup
    from n6705c_seq import gui
    from n6705c_seq.execute import DEMO_SLOTS

    group = InstrumentGroup.demo(frames=1, slots=DEMO_SLOTS)
    monkeypatch.setattr(gui, "connect_group", lambda resources: group)
    app = object.__new__(gui.App)
    app.queue = queue.Queue()
    app._off_work(["x"])
    assert app.queue.get_nowait()[0] == "done"


def _list_test_app(monkeypatch, lang="en", themed=False):
    import tkinter as tk
    from types import SimpleNamespace
    from n6705c_seq import gui, list_gui

    try:
        root = _tk_root()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    if themed:
        gui.setup_theme(root)

    def compile_plan(plan):
        live = [ch for ch in plan.channels if ch.enabled and ch.segments]
        info = SimpleNamespace(points=2, requested=0.02, actual=0.02)
        return SimpleNamespace(dwell=0.01, n_points=2 if live else 0,
                               levels={ch.channel: [0, 1] for ch in live},
                               delay_points={ch.channel: 0 for ch in live},
                               pad_points={ch.channel: 0 for ch in live},
                               segments={ch.channel: [info for _ in ch.segments] for ch in live},
                               warnings=[], errors=[] if live else ["no enabled channel with segments"])

    monkeypatch.setattr(list_gui.synth, "compile_plan", compile_plan)
    monkeypatch.setattr(list_gui.synth, "base_duration", lambda seg: 0.02)
    monkeypatch.setattr(list_gui.synth, "value", lambda seg, u: u * 50)
    monkeypatch.setattr(gui, "_load_config", lambda: {"resources": [], "demo": True, "lang": lang})
    try:
        app = gui.App(root)
    except BaseException:
        root.destroy()
        raise
    return root, app


@pytest.mark.parametrize("themed", [False, True])
def test_list_tab_controls_fit_in_short_window(monkeypatch, themed):
    root, app = _list_test_app(monkeypatch, themed=themed)
    tab = app.list_tab
    try:
        root.deiconify()
        root.geometry("1180x860")
        app.notebook.select(tab)
        root.update()
        for shape in ("Step", "Trapezoid"):
            tab.shape.set(shape)
            root.update()
            bottom = tab.winfo_rooty() + tab.winfo_height()
            assert tab.run_button.winfo_rooty() + tab.run_button.winfo_height() <= bottom
            assert tab.warning_text.winfo_rooty() + tab.warning_text.winfo_height() <= bottom
    finally:
        root.destroy()


def test_list_numbers_use_shortest_repr(monkeypatch):
    from pathlib import Path
    from n6705c_seq import list_gui

    root, app = _list_test_app(monkeypatch)
    tab = app.list_tab
    try:
        monkeypatch.setattr(list_gui.filedialog, "askopenfilename", lambda **kw: pytest.fail("unexpected open dialog"))
        tab.open_json(path=Path(__file__).resolve().parents[1] / "examples" / "list_demo.json")
        assert tab.limit.get() == "0.1"
        assert "000000" not in tab.delay.get()
        tab.select_channel(2)
        assert tab.limit.get() == "0.1"
        assert tab.delay.get() == "0.002"
        assert "000000" not in tab.delay.get()
    finally:
        root.destroy()


@pytest.mark.parametrize("value, expected", [(0.1, "0.1"), (1.0, "1"), (0.00001024, "1.024e-05")])
def test_list_format_number_uses_shortest_repr(value, expected):
    from n6705c_seq import list_gui

    assert list_gui._format_number(value) == expected


def test_notebook_contains_csv_and_list_tabs(monkeypatch):
    root, app = _list_test_app(monkeypatch, lang="zh")
    try:
        assert [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()] == ["上電時序", "List 模式"]
        for tab in app.notebook.tabs():
            app.notebook.select(tab)
            root.update_idletasks()
        assert app.canvas.master is app.sequence_tab
        assert app.run_button.master.master is app.sequence_tab
        assert app.off_button.master.master is root
        assert len(app.read_rows()) == 8
        app.list_tab.refresh()
        assert app.list_tab.run_button.instate(["disabled"])
    finally:
        root.destroy()


def test_list_segment_add_delete_move_and_modify(monkeypatch):
    root, app = _list_test_app(monkeypatch)
    tab = app.list_tab
    try:
        tab.add_segment()
        first = tab.channel.segments[0]
        tab.params["end"].set("2")
        tab.add_segment()
        second = tab.channel.segments[1]
        assert second.params["end"] == 2
        tab.move_segment(-1)
        assert tab.channel.segments == [second, first]
        assert tab.segment_index == 0
        tab.move_segment(-1)
        assert tab.channel.segments == [second, first]
        tab.move_segment(1)
        assert tab.channel.segments == [first, second]
        tab.params["end"].set("3")
        tab.segment_repeat.set("4")
        tab.modify_segment()
        assert tab.channel.segments[1].params["end"] == 3
        assert tab.channel.segments[1].repeat == 4
        tab.navigate("first")
        assert tab.segment_index == 0
        tab.navigate("last")
        assert tab.segment_index == 1
        tab.delete_segment()
        assert tab.channel.segments == [first]
        tab.delete_segment()
        assert tab.channel.segments == []
        assert tab.segment_index is None
        tab.delete_segment()
    finally:
        root.destroy()


@pytest.mark.parametrize("empty", [False, True])
def test_list_json_round_trip_without_dialogs(tmp_path, monkeypatch, empty):
    from n6705c_seq import list_gui
    from n6705c_seq.listplan import ChannelList, ListPlan, Segment, load_json, write_json

    root, app = _list_test_app(monkeypatch)
    tab = app.list_tab
    try:
        plan = ListPlan([] if empty else [ChannelList(2, True, "電源", 0.123456789012345,
                        0.987654321098765, [Segment("step", {"start": 0, "end": 2, "delay": 0, "end_time": 0.02}, 3)])],
                        repeat=4, hold_last=True, dwell=0.00123456789012345)
        path, output = tmp_path / "source.json", tmp_path / "saved.json"
        write_json(plan, path)
        monkeypatch.setattr(list_gui.filedialog, "askopenfilename", lambda **kw: pytest.fail("unexpected open dialog"))
        monkeypatch.setattr(list_gui.filedialog, "asksaveasfilename", lambda **kw: pytest.fail("unexpected save dialog"))
        tab.open_json(path)
        tab.save_json(output)
        assert load_json(output) == plan
        tab.refresh()
        if not empty:
            assert tab.channel_choice.get() == "CH2"
            assert "requested" in tab.statistics.get()
            tab.select_channel(8)
            tab.enabled.set(True)
            tab.add_segment()
            tab.refresh()
            assert tab.channel.channel == 8
            assert tab.plan.channels[0] == plan.channels[0]
    finally:
        root.destroy()


def test_list_compilation_errors_and_shared_busy_block_run(monkeypatch):
    from n6705c_seq import list_gui

    root, app = _list_test_app(monkeypatch)
    tab = app.list_tab
    try:
        tab.add_segment()
        tab.refresh()
        assert tab.run_button.instate(["!disabled"])
        monkeypatch.setattr(list_gui.messagebox, "askyesno", lambda *a, **kw: pytest.fail("unexpected confirmation"))
        app.busy = True
        tab.run()
        tab._button_state()
        assert tab.run_button.instate(["disabled"])
        app.busy = False
        compiled = tab.compiled
        compiled.errors = ["點數超過預算"]
        monkeypatch.setattr(list_gui.synth, "compile_plan", lambda plan: compiled)
        tab.run()
        assert tab.run_button.instate(["disabled"])
        assert "點數超過預算" in tab.message.get()
        tab.delay.set("nan")
        tab.refresh()
        assert tab.compiled is None
        assert tab.run_button.instate(["disabled"])
    finally:
        root.destroy()


def test_list_preview_has_200_points_and_no_arrows(monkeypatch):
    from n6705c_seq import list_gui

    root, app = _list_test_app(monkeypatch)
    tab = app.list_tab
    calls = []
    try:
        monkeypatch.setattr(list_gui.synth, "value", lambda seg, u: calls.append(u) or u)
        tab._draw_curve(tab.preview, tab.read_segment(), 0, 0, 300, 180)
        assert len(calls) == 200
        assert calls[0] == 0
        assert calls[-1] == 0.02
        item, = [i for i in tab.preview.find_all() if "annotation" not in tab.preview.gettags(i)]
        assert len(tab.preview.coords(item)) == 400
        assert tab.preview.itemcget(item, "arrow") == "none"
    finally:
        root.destroy()


def test_list_run_snapshots_plan_and_disables_shared_buttons(monkeypatch):
    from n6705c_seq import list_gui

    root, app = _list_test_app(monkeypatch)
    tab = app.list_tab
    jobs = []
    try:
        tab.add_segment()
        tab.hold_last.set(True)
        monkeypatch.setattr(list_gui.messagebox, "askyesno", lambda *a, **kw: True)
        monkeypatch.setattr(app, "_save_config", lambda: None)

        class Thread:
            def __init__(self, *, target, args, daemon):
                jobs.append((target, args))

            def start(self):
                pass

        monkeypatch.setattr(list_gui.threading, "Thread", Thread)
        tab.run()
        assert app.busy
        assert app.run_button.instate(["disabled"])
        assert app.off_button.instate(["disabled"])
        assert tab.run_button.instate(["disabled"])
        _, args = jobs[0]
        assert args[0].hold_last
        tab.channel.segments.clear()
        assert len(args[0].channels[0].segments) == 1
        app.run()
        assert len(jobs) == 1
    finally:
        app.busy = False
        root.destroy()


@pytest.mark.parametrize("failure", [None, "connect", "run", "unconfirmed", "convert"])
def test_list_worker_uses_functions_and_reports_errors(monkeypatch, failure, english):
    import queue
    from n6705c.core.group import InstrumentGroup
    from n6705c_seq import list_gui
    from n6705c_seq.listplan import ChannelList, ListPlan

    tab = object.__new__(list_gui.ListTab)
    tab.queue = queue.Queue()
    plan = ListPlan([ChannelList(1)], hold_last=True)
    compiled = object()
    group = InstrumentGroup.demo(frames=1, slots=list_gui.execute.DEMO_SLOTS)
    closed = []
    inst = group.instruments[0]
    original_close = inst.close
    monkeypatch.setattr(inst, "close", lambda: (closed.append(True), original_close()))
    functions = [object()]

    def connect(resources):
        assert resources == ["test"]
        if failure == "connect":
            raise RuntimeError("連線中斷")
        return group

    def convert(got_plan, got_compiled):
        assert got_plan is plan and got_compiled is compiled
        if failure == "convert":
            raise ValueError("轉換失敗")
        return functions

    def run(got_group, got_functions, *, keep_on, capture):
        assert got_group is group and got_functions is functions
        assert keep_on and capture == {}
        if failure == "run":
            raise RuntimeError("執行失敗")
        if failure == "unconfirmed":
            raise list_gui.execute.OutputsNotConfirmedOff("無法確認關閉")
        capture.update({1: (0.01, [0, 1]), "error": "擷取失敗"})

    monkeypatch.setattr(list_gui.execute, "connect_group", connect)
    monkeypatch.setattr(list_gui.synth, "to_functions", convert)
    monkeypatch.setattr(list_gui.execute, "run_functions", run)
    try:
        tab._work(plan, compiled, 1, ["test"], False, True)
        result = tab.queue.get_nowait()
        assert result[0] == ("error" if failure else "done")
        if failure == "unconfirmed":
            assert "Outputs were forced off" not in result[1]
        elif failure == "convert":
            assert "rejected before outputs were enabled" in result[1]
        elif not failure:
            assert result[2] == {1: (0.01, [0, 1])}
            assert "擷取失敗" in result[1]
        assert closed == ([] if failure == "connect" else [True])
    finally:
        original_close()


@pytest.mark.parametrize("text, options", [("nan", {}), ("inf", {}), ("-1", {"minimum": 0}),
                                           ("0", {"positive": True}), ("1.5", {"positive": True, "integer": True})])
def test_list_form_rejects_invalid_numbers(text, options):
    from n6705c_seq.list_gui import ListTab

    with pytest.raises(ValueError):
        ListTab._number(text, "測試欄位", **options)


def test_list_segment_operations_without_a_display(monkeypatch):
    import tkinter as tk
    from n6705c_seq.list_gui import ListTab, SHAPE_NAMES
    from n6705c_seq.listplan import ChannelList, ListPlan

    master = tk.Tcl()
    tab = object.__new__(ListTab)
    tab.plan = ListPlan([ChannelList(1)])
    tab.channel_index = 0
    tab.segment_index = None
    tab.shape = tk.StringVar(master, value=SHAPE_NAMES[0])
    tab.segment_repeat = tk.StringVar(master, value="1")
    tab.params = {key: tk.StringVar(master, value=value) for key, value in
                  {"start": "0", "end": "1", "delay": "0", "end_time": "0.01"}.items()}
    monkeypatch.setattr(tab, "schedule", lambda: None)
    monkeypatch.setattr(tab, "select_segment", lambda index: setattr(tab, "segment_index", index))
    tab.add_segment()
    first = tab.channel.segments[0]
    tab.params["end"].set("2")
    tab.add_segment()
    second = tab.channel.segments[1]
    tab.move_segment(-1)
    assert tab.channel.segments == [second, first]
    tab.move_segment(1)
    assert tab.channel.segments == [first, second]
    tab.params["end"].set("3")
    tab.modify_segment()
    assert tab.channel.segments[1].params["end"] == 3
    tab.delete_segment()
    assert tab.channel.segments == [first]
    tab.delete_segment()
    assert not tab.channel.segments
    assert tab.segment_index is None


def test_list_tab_runs_demo_plan_end_to_end(monkeypatch):
    import tkinter as tk
    from pathlib import Path
    from n6705c.core.group import InstrumentGroup
    from n6705c_seq import gui

    try:
        root = _tk_root()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    monkeypatch.setattr(gui, "_load_config", lambda: {"resources": [], "demo": True, "lang": "en"})
    groups = []
    original_demo = InstrumentGroup.demo

    def demo(cls, *, frames, slots):
        group = original_demo(frames=frames, slots=slots)
        groups.append(group)
        return group

    monkeypatch.setattr(InstrumentGroup, "demo", classmethod(demo))
    try:
        app = gui.App(root)
        list_tab = app.list_tab
        list_tab.open_json(path=Path(__file__).resolve().parents[1] / "examples" / "list_demo.json")
        list_tab.refresh()
        plan, compiled = list_tab.plan, list_tab.compiled
        assert compiled is not None
        assert not compiled.errors
        frames = -(-max(compiled.levels) // 4)
        resources = [v.get().strip() for v in app.resources[:frames]]
        list_tab._work(plan, compiled, frames, resources, True, False)
        kind, message, capture = list_tab.queue.get_nowait()
        assert kind == "done", message
        assert "outputs are off" in message
        assert capture is None
        assert len(groups) == 1
        for inst in groups[0].instruments:
            states = inst.output_state(inst.populated_channels)
            assert set(states) == set(inst.populated_channels)
            assert not any(states.values())
    finally:
        root.destroy()


def _preview_test_tab():
    import tkinter as tk
    from types import SimpleNamespace
    from n6705c_seq.list_gui import ListTab

    try:
        root = _tk_root()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    i18n.set_lang("en")
    app = SimpleNamespace(resources=[tk.StringVar(root)], busy=False)
    tab = ListTab(root, app)
    return root, tab


def _annotation_texts(canvas, tag="annotation"):
    return [canvas.itemcget(item, "text") for item in canvas.find_withtag(tag)
            if canvas.type(item) == "text"]


def test_list_ramp_preview_annotations_follow_live_form():
    root, tab = _preview_test_tab()
    try:
        tab.shape.set("Ramp")
        tab.refresh()
        texts = _annotation_texts(tab.preview)
        assert {"Start 0 V", "End 1 V", "Delay 10 ms", "Ramp Time 10 ms", "End Time 10 ms"} <= set(texts)
        tab.params["ramp_time"].set("2")
        tab.refresh()
        assert "Ramp Time 2 s" in _annotation_texts(tab.preview)
        tab.params["ramp_time"].set("nan")
        tab.refresh()
        assert not tab.preview.find_all()
    finally:
        root.destroy()


@pytest.mark.parametrize("shape,amplitude", [("Sine", "1"), ("Triangle", "-1")])
def test_list_periodic_preview_annotations(shape, amplitude):
    root, tab = _preview_test_tab()
    try:
        tab.shape.set(shape)
        tab.params["offset"].set("2")
        tab.params["amplitude"].set(amplitude)
        tab.segment_repeat.set("5")
        tab.refresh()
        texts = _annotation_texts(tab.preview)
        assert {"Offset 2 V", f"Amplitude {amplitude} V", "Period 10 ms"} <= set(texts)
        assert "Frequency 100 Hz   Phase 0°   ×5" in texts
        dimension = [item for item in tab.preview.find_withtag("annotation")
                     if tab.preview.type(item) == "line" and len(tab.preview.coords(item)) == 4
                     and tab.preview.coords(item)[0] == tab.preview.coords(item)[2]
                     and abs(tab.preview.coords(item)[1] - tab.preview.coords(item)[3]) > 14]
        assert len(dimension) == 1
        _, base, _, peak = tab.preview.coords(dimension[0])
        assert (peak < base) == (float(amplitude) > 0)
    finally:
        root.destroy()


def test_list_tiny_time_span_labels_do_not_overlap():
    root, tab = _preview_test_tab()
    try:
        tab.shape.set("Ramp")
        tab.params["delay"].set("0.0001")
        tab._draw_curve(tab.preview, tab.read_segment(), 0, 0, 330, 185)
        labels = tab.preview.find_withtag("time_label")
        assert set(_annotation_texts(tab.preview, "time_label")) == {"Delay 0.1 ms", "Ramp Time 10 ms", "End Time 10 ms"}
        boxes = [tab.preview.bbox(item) for item in labels]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
        delay = next(item for item in labels if tab.preview.itemcget(item, "text").startswith("Delay"))
        assert tab.preview.coords(delay)[1] > min(tab.preview.coords(item)[1] for item in labels)
    finally:
        root.destroy()


def test_list_equal_levels_have_one_combined_annotation():
    root, tab = _preview_test_tab()
    try:
        tab.shape.set("Ramp")
        tab.params["start"].set("1")
        tab.refresh()
        assert _annotation_texts(tab.preview, "level_label") == ["Start = End 1 V"]
        tab.add_segment()
        tab.refresh()
        assert not tab.segment_canvas.find_withtag("annotation")
        assert not tab.canvas.find_withtag("annotation")
        assert "1 Ramp ×1" in _annotation_texts(tab.segment_canvas, "all")
    finally:
        root.destroy()


@pytest.mark.parametrize("shape,expected", [
    ("Step", {"Start 0 V", "End 1 V", "Delay 10 ms", "End Time 10 ms"}),
    ("Trapezoid", {"Offset 0 V", "Offset+Amp -1 V", "Start Time 10 ms", "Up Time 10 ms",
                   "High Time 10 ms", "Down Time 10 ms", "End Time 10 ms"}),
    ("Staircase", {"Start 0 V", "End 1 V", "Stair Time 10 ms", "Steps 5"}),
    ("Exponential", {"Start 0 V", "End 1 V", "Exp Time 10 ms", "Tau 10 ms"}),
])
def test_list_other_shape_annotations(shape, expected):
    root, tab = _preview_test_tab()
    try:
        tab.shape.set(shape)
        if shape == "Trapezoid":
            tab.params["amplitude"].set("-1")
        tab._draw_curve(tab.preview, tab.read_segment(), 0, 0, 330, 185)
        assert expected <= set(_annotation_texts(tab.preview))
        boxes = [tab.preview.bbox(item) for item in tab.preview.find_withtag("time_label")]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    finally:
        root.destroy()


def test_list_sources_have_no_cjk_text():
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "n6705c_seq"
    for name in ("list_gui.py", "synth.py", "listplan.py"):
        assert not re.search(r"[一-鿿　-〿＀-￯]", (source / name).read_text(encoding="utf-8"))


def test_gui_translation_keys():
    import re
    from pathlib import Path
    from n6705c_seq import gui, list_gui

    assert all(isinstance(pair, tuple) and len(pair) == 2 and
               all(isinstance(text, str) and text for text in pair) for pair in i18n.STRINGS.values())
    for module in (gui, list_gui):
        keys = re.findall(r'tr\("([^"\n]+)"', Path(module.__file__).read_text())
        assert set(keys) <= i18n.STRINGS.keys()
    from n6705c_seq.listplan import FIELDS
    assert set(gui.TITLES.values()) | {key for keys in FIELDS.values() for key in keys} <= i18n.STRINGS.keys()


def test_language_switch_preserves_live_entries(tmp_path, monkeypatch):
    from n6705c_seq import gui

    load_config = gui._load_config
    root, app = _list_test_app(monkeypatch, lang="zh")
    monkeypatch.setattr(gui, "_load_config", load_config)
    monkeypatch.setattr(gui, "CONFIG", tmp_path / "config.json")
    tab = app.list_tab
    try:
        app.forms[0]["name"].set("typed sequence")
        tab.add_segment()
        tab.params["end"].set("3.14159")
        parameter = tab.params["end"]
        header = next(widget for widget, key, _ in app.texts if key == "column_name")
        label = next(widget for widget, key, _ in tab.parameter_texts if key == "end")
        count = len(tab.parameter_texts)
        for lang in ("en", "zh"):
            app.toggle_language()
            assert i18n.lang == lang
            assert app.run_button.cget("text") == i18n.tr("run_sequence")
            assert tab.run_button.cget("text") == i18n.tr("run_list")
            assert header.cget("text") == i18n.tr("column_name")
            assert label.cget("text") == i18n.tr("end") + " (V)"
            assert [app.notebook.tab(t, "text") for t in app.notebook.tabs()] == [i18n.tr("sequence_tab"), i18n.tr("list_tab")]
            assert app.forms[0]["name"].get() == "typed sequence"
            assert tab.params["end"] is parameter and parameter.get() == "3.14159"
            assert tab.segment_index == 0 and tab.channel_index == 0
            assert len(tab.parameter_texts) == count
            assert gui._load_config()["lang"] == lang
        tab.shape.set("Ramp")
        assert all(widget.winfo_exists() for widget, *_ in tab.parameter_texts)
    finally:
        root.destroy()


@pytest.mark.parametrize("saved, expected", [("en", "en"), ("zh", "zh"), ("invalid", "zh"), (None, "zh")])
def test_config_language_keeps_other_fields(tmp_path, monkeypatch, saved, expected):
    import json
    from n6705c_seq import gui

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"resources": ["A"], "demo": False, "lang": saved}))
    monkeypatch.setattr(gui, "CONFIG", path)
    assert gui._load_config() == {"resources": ["A"], "demo": False, "lang": expected}


def test_thin_lanes_drop_delay_and_hold_labels(monkeypatch):
    root, app = _list_test_app(monkeypatch, lang="zh")
    try:
        def labels():
            app.draw()
            return [t for i in app.canvas.find_all() if app.canvas.type(i) == "text"
                    for t in [app.canvas.itemcget(i, "text")] if t.startswith(("延遲", "維持"))]

        assert labels() == []  # withdrawn root: 200 px canvas, 8 lanes of 18 px
        for form in app.forms[1:]:
            form["enabled"].set(False)
        assert any(t.startswith("維持") for t in labels())
    finally:
        root.destroy()


def test_e36441a_sequence_worker_uses_its_own_runner(monkeypatch, fake_model):
    import queue
    from n6705c_seq import gui
    fake_model("E36441A")
    calls = []
    monkeypatch.setattr(gui, "run_resources", lambda res, rows, hold_dc: calls.append((res, hold_dc)))
    monkeypatch.setattr(gui, "connect_group", lambda r: pytest.fail("N6705C path used"))
    app = object.__new__(gui.App)
    app.queue = queue.Queue()
    app._work([], 1, ["x"], False, False, False)
    assert app.queue.get_nowait()[0] == "done" and calls == [(["x"], False)]
    app._work([], 1, ["x"], False, False, True)  # capture has no digitizer to use
    assert app.queue.get_nowait()[0] == "error"


def test_e36441a_locks_list_tab_and_capture():
    if not _tk_display_available():
        pytest.skip("no display")
    import tkinter as tk
    from n6705c_seq import gui
    root = tk.Tk()
    try:
        app = gui.App(root)
        app.set_kind("E36441A")
        assert str(app.notebook.tab(app.list_tab, "state")) == "disabled"
        assert app.capture_check.instate(["disabled"])
        app.set_kind("N6705C")
        assert str(app.notebook.tab(app.list_tab, "state")) == "normal"
    finally:
        root.destroy()
