import ast
import copy
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from n6705c_seq import form, gui, i18n
from n6705c_seq.plan import SeqRow, order_warnings
from n6705c.core.group import TRIGGER_SKEW_SECONDS


@pytest.fixture(autouse=True)
def reset_language():
    previous = i18n.lang
    i18n.set_lang("en")
    yield
    i18n.set_lang(previous)


def test_defaults_and_parse_units():
    raw = form.default_raw(9)
    assert all(set(row) == set(form.RAW_KEYS) for row in raw)
    assert raw[0] == dict(enabled=True, name="CH1", delay="0", low="0", high="12",
                          rise="3", hold="60", fall="3", limit="")
    assert raw[8] == dict(enabled=False, name="CH9", delay="0", low="0", high="1",
                          rise="1", hold="10", fall="1", limit="")
    raw[0].update(name="", delay="12.5", low="0.25", limit=" 0.125 ")
    before = copy.deepcopy(raw)
    rows = form.parse_rows(raw)
    assert rows[0] == SeqRow(1, True, "CH1", 0.0125, 0.25, 12, 0.003, 0.06, 0.003, 0.125)
    assert [r.channel for r in rows] == list(range(1, 10))
    assert rows[1].current_limit is None
    assert raw == before
    assert form.default_raw(0) == form.parse_rows([]) == []


@pytest.mark.parametrize("key", form.NUMERIC)
@pytest.mark.parametrize("text", ["", "bad", "-1", "nan", "inf", "-inf"])
def test_numeric_errors_and_disabled_tolerance(key, text):
    raw = form.default_raw(1)
    raw[0][key] = text
    expected = i18n.tr("invalid_nonnegative", label="CH1", title=i18n.tr(form.TITLES[key]).splitlines()[0])
    with pytest.raises(ValueError) as error:
        form.parse_rows(raw)
    assert str(error.value) == expected
    raw[0]["enabled"] = False
    row, = form.parse_rows(raw)
    attr = {"low": "low_v", "high": "high_v"}.get(key, key)
    assert getattr(row, attr) == 0
    assert not row.enabled


@pytest.mark.parametrize("text", ["bad", "0", "-1", "nan", "inf", "-inf"])
def test_limit_errors_and_disabled_tolerance(text):
    raw = form.default_raw(1)
    raw[0]["limit"] = text
    with pytest.raises(ValueError) as error:
        form.parse_rows(raw)
    assert str(error.value) == i18n.tr("invalid_limit", label="CH1")
    raw[0]["enabled"] = False
    assert form.parse_rows(raw)[0].current_limit is None


@pytest.mark.parametrize("text", ["", " ", "\t"])
def test_blank_limit_is_unlimited(text):
    raw = form.default_raw(1)
    raw[0]["limit"] = text
    assert form.parse_rows(raw)[0].current_limit is None


@pytest.mark.parametrize("raw", [None, {}, (), [None], [[]], [{}],
                                  [{**form.default_raw(1)[0], "extra": ""}],
                                  [{k: v for k, v in form.default_raw(1)[0].items() if k != "name"}]])
def test_bad_structure(raw):
    with pytest.raises(TypeError):
        form.parse_rows(raw)


@pytest.mark.parametrize("key", form.RAW_KEYS)
def test_bad_value_types_even_when_disabled(key):
    raw = form.default_raw(1)
    raw[0]["enabled"] = False
    raw[0][key] = 1
    with pytest.raises(TypeError):
        form.parse_rows(raw)


def test_structure_checked_before_user_values():
    raw = form.default_raw(2)
    raw[0]["delay"] = "bad"
    raw[1]["enabled"] = "false"
    with pytest.raises(TypeError):
        form.parse_rows(raw)


def test_rows_to_raw_precision_capacity_and_copy():
    current = form.default_raw(3)
    current[2]["high"] = "unfinished"
    before = copy.deepcopy(current)
    row = SeqRow(1, True, "rail", 0.1234567890123, 0.1234567890123,
                 12.34567890123, 0.0001234567890123, 1.234567890123, 0.01234567890123, 0.1234567890123)
    raw = form.rows_to_raw([row, replace(row, current_limit=None)], current)
    assert raw[0] == dict(enabled=True, name="rail", delay="123.456789", low="0.123456789",
                          high="12.3456789", rise="0.123456789", hold="1234.56789",
                          fall="12.3456789", limit="0.123457")
    assert raw[1]["limit"] == ""
    assert raw[2] == {**current[2], "enabled": False}
    assert current == before
    assert all(a is not b for a, b in zip(raw, current))
    with pytest.raises(ValueError) as error:
        form.rows_to_raw([row] * 4, current)
    assert str(error.value) == i18n.tr("csv_capacity", rows=4, channels=3)
    assert current == before


@pytest.mark.parametrize("hold_dc, count", [(False, 6), (True, 4)])
def test_view_points_labels_and_units(hold_dc, count):
    raw = form.default_raw(2)
    raw[1]["enabled"] = False
    rows = form.parse_rows(raw)
    view = form.seq_view(rows, hold_dc, {})
    lane, = view["lanes"]
    assert view["t_end"] == pytest.approx((3 if hold_dc else 66) * 1.18)
    assert view["ticks"] == list(range(0, int(view["t_end"]) + 1, form.tick_step(view["t_end"])))
    assert view["time_axis"] == i18n.tr("time_axis")
    assert view["measured_label"] == i18n.tr("measured")
    assert len(lane["points"]) == count
    assert lane["points"][2] == [3, 12]
    assert lane["points"][-1] == [view["t_end"], 12 if hold_dc else 0]
    assert lane["delay"] == 0 and lane["top0"] == 3
    assert lane["top1"] == (3 if hold_dc else 63)
    assert lane["delay_text"] == i18n.tr("delay_annotation", value=0)
    assert lane["hold_text"] == (None if hold_dc else i18n.tr("hold_annotation", value=60))
    assert lane["levels_text"] == i18n.tr("levels_annotation", high=12, low=0)
    assert lane["measured"] is None


@pytest.mark.parametrize("size", [0, 1, 2000, 2001, 3999, 20000])
def test_view_measured_downsampling_and_clipping(size):
    rows = form.parse_rows(form.default_raw(1))
    measured = {1: (0.00001, list(range(size)))}
    before = copy.deepcopy(measured)
    view = form.seq_view(rows, False, measured)
    got = view["lanes"][0]["measured"]
    assert len(got) <= 2000
    assert all(t <= view["t_end"] for t, _ in got)
    assert all(t == pytest.approx(v * 0.01) for t, v in got)
    assert bool(got) == bool(size)
    assert measured == before


def test_view_warnings_empty_and_zero_duration():
    rows = [replace(r, delay=0) for r in form.parse_rows(form.default_raw(8))]
    warnings = order_warnings(rows, TRIGGER_SKEW_SECONDS)
    assert len(warnings) > 3
    assert form.seq_view(rows, False, {})["message"] == (
        "\n".join(f"\u26a0 {w}" for w in warnings[:3]) + i18n.tr("more_warnings", count=len(warnings) - 3))
    assert form.seq_view([], False, {})["lanes"] == []
    zero = SeqRow(1, True, "zero", 0, 1, 1, 0, 0, 0)
    view = form.seq_view([zero], False, {})
    assert view["t_end"] == 1
    assert view["lanes"][0]["high"] > view["lanes"][0]["low"]


def test_config_and_gui_aliases(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    default = dict(resources=[], demo=True, lang="zh")
    assert form.load_config(path) == default
    for saved in ("bad", "[]", "null", '{"resources":[],"demo":1}', '{"resources":[1],"demo":true}'):
        path.write_text(saved)
        assert form.load_config(path) == default
    form.save_config(path, ["A", "B"], False, "en")
    assert json.loads(path.read_text()) == dict(resources=["A", "B"], demo=False, lang="en")
    monkeypatch.setattr(gui, "CONFIG", path)
    assert gui._load_config() == form.load_config(path)
    assert gui.waveform is form.waveform and gui._tick_step is form.tick_step
    for name in ("FRAMES", "PALETTE", "DEFAULTS", "COLUMNS", "TITLES", "NUMERIC"):
        assert getattr(gui, name) == getattr(form, name)


def test_gui_adapters_without_display():
    import tkinter as tk

    master = tk.Tcl()
    app = object.__new__(gui.App)
    raw = form.default_raw(2)
    app.forms = [{key: (tk.BooleanVar(master, value=value) if key == "enabled" else
                       tk.StringVar(master, value=value)) for key, value in r.items()} for r in raw]
    assert app.read_rows() == form.parse_rows(raw)
    rows = [replace(app.read_rows()[0], name="changed", current_limit=0.123456789)]
    app.fill(rows)
    assert [{key: f[key].get() for key in form.RAW_KEYS} for f in app.forms] == form.rows_to_raw(rows, raw)


def test_form_sources_and_translation_keys():
    source = Path(form.__file__).read_text(encoding="utf-8")
    assert not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", source)
    keys = re.findall(r'tr\("([^"\n]+)"', source)
    assert set(keys) | set(form.TITLES.values()) <= i18n.STRINGS.keys()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] not in ("tkinter", "webview") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in ("tkinter", "webview")
