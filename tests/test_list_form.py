import copy
import re
from pathlib import Path

import pytest

from n6705c_seq import i18n, list_form, synth
from n6705c_seq.listplan import FIELDS, SHAPES, ChannelList, ListPlan, Segment, load_json, write_json


@pytest.fixture(autouse=True)
def english():
    previous = i18n.lang
    i18n.set_lang("en")
    yield
    i18n.set_lang(previous)


def params(shape, **changes):
    result = {field["key"]: field["default"] for field in list_form.meta()["fields"][shape]}
    result.update(changes)
    return result


def settings(**changes):
    raw = {"channel": 1, "enabled": True, "label": "", "delay": "0", "limit": "",
           "repeat": "1", "hold_last": False, "dwell_mode": "auto", "dwell": "invalid"}
    raw.update(changes)
    return raw


def plan():
    return ListPlan([ChannelList(1, segments=[Segment("step", {
        "start": 0, "end": 1, "delay": 0.002, "end_time": 0.003,
    })])], dwell=0.00100352)


def texts(preview):
    return {item["text"] for item in preview["levels"] + preview["times"]} | {preview["extras"]}


def test_meta_and_number():
    meta = list_form.meta()
    assert meta["shapes"] == [list(pair) for pair in zip(SHAPES, list_form.SHAPE_NAMES)]
    for shape in SHAPES:
        assert [field["key"] for field in meta["fields"][shape]] == list(FIELDS[shape])
        assert len(list_form.draft_view(shape, params(shape), "1")["preview"]["samples"]) == 200
    assert meta["fields"]["sine"] == [
        {"key": "offset", "unit": "V", "default": "0"},
        {"key": "amplitude", "unit": "V", "default": "1"},
        {"key": "frequency", "unit": "Hz", "default": "100"},
        {"key": "phase", "unit": "deg", "default": "0"},
    ]
    assert list_form.format_number(1.0) == "1"
    assert list_form.format_number(1e-10) == "1e-10"
    assert list_form.number("2", "Count", integer=True, positive=True) == 2
    for value in ("nan", "inf", "-1", "oops"):
        with pytest.raises(ValueError, match="Delay"):
            list_form.number(value, "Delay", minimum=0)
    with pytest.raises(ValueError, match="positive integer"):
        list_form.number("1.5", "Count", integer=True, positive=True)


@pytest.mark.parametrize("shape,raw,repeat", [
    ("unknown", {}, "1"), (None, {}, "1"), ([], {}, "1"),
    ("step", None, "1"), ("step", [], "1"), ("step", {}, "1"),
    ("step", params("step", extra="0"), "1"),
    ("step", {key: value for key, value in params("step").items() if key != "start"}, "1"),
    ("step", params("step", start=0), "1"),
    ("step", params("step", start=None), "1"),
    ("step", params("step"), 1), ("step", params("step"), None),
])
def test_segment_payload_types(shape, raw, repeat):
    for read in (list_form.read_segment, list_form.draft_view):
        with pytest.raises(TypeError) as error:
            read(shape, raw, repeat)
        assert str(error.value).isascii()


@pytest.mark.parametrize("shape", SHAPES)
def test_segment_fields_accept_any_order(shape):
    raw = params(shape)
    segment = list_form.read_segment(shape, dict(reversed(list(raw.items()))), "2")
    assert segment == list_form.read_segment(shape, raw, "2")
    assert segment.repeat == 2
    assert list(segment.params) == list(FIELDS[shape])


@pytest.mark.parametrize("shape", SHAPES)
def test_segment_errors_follow_field_order(shape):
    raw = {key: "invalid" for key in reversed(FIELDS[shape])}
    with pytest.raises(ValueError) as error:
        list_form.read_segment(shape, raw, "1")
    assert str(error.value) == str(list_form.draft_view(
        shape, dict(reversed(list(raw.items()))), "1")["error"])
    assert i18n.tr(FIELDS[shape][0]) in str(error.value)


def test_ramp_live_preview():
    draft = list_form.draft_view("ramp", params("ramp"), "1")
    assert draft["error"] is None
    preview = draft["preview"]
    assert {"Start 0 V", "End 1 V", "Delay 10 ms", "Ramp Time 10 ms", "End Time 10 ms"} <= texts(preview)
    assert preview["duration"] == pytest.approx(0.03)
    assert (preview["lo"], preview["hi"]) == (0, 1)
    assert preview["samples"][0] == 0
    assert preview["samples"][-1] == 1
    changed = list_form.draft_view("ramp", params("ramp", ramp_time="2"), "1")
    assert "Ramp Time 2 s" in texts(changed["preview"])
    invalid = list_form.draft_view("ramp", params("ramp", ramp_time="nan"), "1")
    assert invalid["segment"] is None and invalid["preview"] is None
    assert "Ramp Time" in invalid["error"]


@pytest.mark.parametrize("shape,amplitude", [("sine", "1"), ("triangle", "-1")])
def test_periodic_annotations(shape, amplitude):
    draft = list_form.draft_view(shape, params(shape, offset="2", amplitude=amplitude), "5")
    preview = draft["preview"]
    assert texts(preview) >= {"Offset 2 V", "Period 10 ms", "Frequency 100 Hz   Phase 0°   ×5"}
    assert preview["amplitude"] == {"text": f"Amplitude {amplitude} V", "base": 2, "peak": 2 + float(amplitude)}
    assert len(preview["levels"]) == 1 and preview["periodic"]
    assert (preview["lo"], preview["hi"]) == (1, 3)
    assert preview["duration"] == 0.01
    assert draft["segment"]["repeat"] == 5
    assert (preview["amplitude"]["peak"] > preview["amplitude"]["base"]) == (float(amplitude) > 0)


def test_tiny_spans_and_equal_levels():
    preview = list_form.draft_view("ramp", params("ramp", delay="0.0001"), "1")["preview"]
    assert {item["text"] for item in preview["times"]} == {"Delay 0.1 ms", "Ramp Time 10 ms", "End Time 10 ms"}
    assert [item["seconds"] for item in preview["times"]] == [0.0001, 0.01, 0.01]
    preview = list_form.draft_view("ramp", params("ramp", start="1"), "1")["preview"]
    assert preview["combined"] == "Start = End 1 V"
    assert preview["lo"] == preview["hi"] == 1
    assert preview["samples"] == [1] * 200


@pytest.mark.parametrize("shape,expected", [
    ("step", {"Start 0 V", "End 1 V", "Delay 10 ms", "End Time 10 ms"}),
    ("trapezoid", {"Offset 0 V", "Offset+Amp -1 V", "Start Time 10 ms", "Up Time 10 ms",
                   "High Time 10 ms", "Down Time 10 ms", "End Time 10 ms"}),
    ("staircase", {"Start 0 V", "End 1 V", "Stair Time 10 ms", "Steps 5"}),
    ("exponential", {"Start 0 V", "End 1 V", "Exp Time 10 ms", "Tau 10 ms"}),
])
def test_other_annotations(shape, expected):
    raw = params(shape, **({"amplitude": "-1"} if shape == "trapezoid" else {}))
    preview = list_form.draft_view(shape, raw, "1")["preview"]
    assert expected <= texts(preview)
    assert preview["amplitude"] is None and not preview["periodic"]
    assert len(preview["levels"]) == 2
    assert preview["lo"] == (-1 if shape == "trapezoid" else 0)
    assert preview["hi"] == (0 if shape == "trapezoid" else 1)


def test_settings_are_validated_and_applied_without_mutation():
    original = plan()
    before = copy.deepcopy(original)
    parsed = list_form.parse_settings(settings(label="Supply", delay="0.1", limit="2.5", repeat="4",
                                               hold_last=True, dwell_mode="manual", dwell="0.001"))
    frozen = copy.deepcopy(parsed)
    result = list_form.apply_settings(original, parsed)
    assert original == before and parsed == frozen
    assert result.channels[0].label == "Supply"
    assert result.channels[0].delay == 0.1 and result.channels[0].current_limit == 2.5
    assert (result.repeat, result.hold_last, result.dwell) == (4, True, 0.001)
    result.channels[0].segments[0].params["end"] = 9
    assert original == before
    assert list_form.parse_settings(settings(limit="  "))["limit"] is None
    assert list_form.parse_settings(settings())["dwell"] is None


def test_new_channel_rules():
    original = ListPlan([])
    parsed = list_form.parse_settings(settings(channel=3, enabled=False))
    assert list_form.apply_settings(original, parsed).channels == []
    for changes in ({"enabled": True}, {"label": "New"}, {"delay": 0.1}, {"limit": 1.0}):
        result = list_form.apply_settings(original, {**parsed, **changes})
        assert len(result.channels) == 1 and result.channels[0].channel == 3
    existing = ListPlan([ChannelList(3, enabled=False)])
    assert len(list_form.apply_settings(existing, parsed).channels) == 1
    assert original.channels == []


@pytest.mark.parametrize("changes", [
    {"delay": "-1"}, {"delay": "nan"}, {"limit": "0"}, {"limit": "inf"},
    {"repeat": "257"}, {"repeat": "1.5"}, {"repeat": "0"},
    {"dwell_mode": "manual", "dwell": "0"},
])
def test_invalid_settings_values(changes):
    with pytest.raises(ValueError):
        list_form.parse_settings(settings(**changes))


@pytest.mark.parametrize("raw", [None, [], {}, settings(channel=True), settings(enabled=1),
                                 settings(delay=0), settings(hold_last=0), settings(dwell_mode="other"),
                                 {**settings(), "extra": ""}])
def test_settings_types(raw):
    with pytest.raises(TypeError):
        list_form.parse_settings(raw)
    with pytest.raises(TypeError) as error:
        list_form.compiled_view(plan(), raw, None, {})
    assert str(error.value).isascii()


def test_plan_roundtrip_matches_json(tmp_path):
    original = plan()
    original.channels.append(ChannelList(2, enabled=False, label="Spare"))
    path = tmp_path / "plan.json"
    write_json(original, path)
    raw = list_form.plan_to_dict(original)
    assert "version" not in raw
    assert list_form.plan_from_dict(raw) == load_json(path) == original
    raw["channels"][0]["segments"][0]["params"]["end"] = 8
    assert original.channels[0].segments[0].params["end"] == 1


@pytest.mark.parametrize("raw,message", [
    ([], "plan: must be a JSON object"),
    ({"channels": [{}]}, "plan channel 1: channel must be an integer >= 1"),
    ({"channels": [{"channel": 2, "delay": -1}]}, "CH2: delay must be >= 0"),
    ({"channels": [{"channel": 2, "segments": [{"shape": "ramp", "params": {}}]}]},
     "CH2 segment 1: params fields do not match; missing: start, end, delay, ramp_time, end_time; extra: none"),
    ({"channels": [{"channel": 1}, {"channel": 1}]}, "CH1: duplicate channel is not allowed"),
])
def test_plan_errors_match_loader(raw, message, tmp_path):
    import json

    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError) as direct:
        load_json(path)
    with pytest.raises(ValueError) as converted:
        list_form.plan_from_dict(raw)
    assert str(converted.value) == str(direct.value) == message


def test_compiled_steps_measured_bounds_and_statistics():
    original = plan()
    c = synth.compile_plan(original)
    capture = {1: (c.dwell, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 20])}
    before = copy.deepcopy(original)
    view = list_form.compiled_view(original, settings(dwell_mode="manual", dwell=str(original.dwell)), 0, capture)
    assert original == before
    assert view["ok"] == (not c.errors)
    assert view["plan"] == list_form.plan_to_dict(original)
    assert view["n_points"] == c.n_points == 5
    assert view["lanes"][0]["steps"] == [[0, 0], [1, 0], [1, 1], [5, 1]]
    assert view["lanes"][0]["selected"]
    assert (view["lanes"][0]["lo"], view["lanes"][0]["hi"]) == (0, 20)
    assert len(view["lanes"][0]["measured"]) == 6
    assert view["lanes"][0]["measured"][-1][0] == pytest.approx(5)
    assert view["budget"] == {"n_points": 5, "delay": 0, "pad": 0}
    assert len(view["axis"]) == 5 and view["axis"][0] == "0 ms"
    assert view["repeat_text"] == "× 1"
    assert view["segments"][0]["title"] == "1 Step ×1"
    assert len(view["segments"][0]["samples"]) == 200
    assert view["statistics"].startswith("Segment 1")


def test_compiled_errors_keep_thumbnails_and_match_ok():
    original = ListPlan([ChannelList(1, segments=[list_form.read_segment("sine", params("sine"), "1")])], dwell=0.002)
    c = synth.compile_plan(original)
    assert c.errors
    view = list_form.compiled_view(original, settings(dwell_mode="manual", dwell=str(original.dwell)), None, {})
    assert not view["ok"]
    assert view["message"] == "\n".join([f"✘ {e}" for e in c.errors] + [f"⚠ {w}" for w in c.warnings])
    assert len(view["segments"][0]["samples"]) == 200
    assert not list_form.compiled_view(ListPlan([]), settings(), None, {})["ok"]


def test_compiled_channel_selection_and_empty_measured():
    original = plan()
    original.channels.extend([ChannelList(2, segments=copy.deepcopy(original.channels[0].segments)),
                              ChannelList(3, enabled=False), ChannelList(4)])
    view = list_form.compiled_view(original, settings(channel=2), None, {})
    assert [lane["channel"] for lane in view["lanes"]] == [1, 2, 4]
    assert [lane["selected"] for lane in view["lanes"]] == [False, True, False]
    assert view["lanes"][-1]["steps"] == []
    assert all(lane["measured"] is None for lane in view["lanes"])


def test_compiled_view_applies_raw_settings_without_mutation():
    original = plan()
    original.channels.append(ChannelList(2, segments=[list_form.read_segment("sine", params("sine"), "2")]))
    raw = settings(channel=2, label="Updated", delay="0.003", limit="1.5", repeat="3",
                   hold_last=True, dwell_mode="manual", dwell="0.0001")
    before, raw_before = copy.deepcopy(original), copy.deepcopy(raw)
    applied = list_form.apply_settings(original, list_form.parse_settings(raw))
    compiled = synth.compile_plan(applied)
    view = list_form.compiled_view(original, raw, 0, {})
    assert original == before and raw == raw_before
    assert view["plan"] == list_form.plan_to_dict(applied)
    assert view["ok"] == (not compiled.errors)
    assert view["n_points"] == compiled.n_points
    assert view["budget"]["delay"] == compiled.delay_points[2]
    assert view["repeat_text"] == "× 3"
    assert view["segments"][0]["title"] == "1 Sine ×2"
    assert [lane["label"] for lane in view["lanes"] if lane["selected"]] == ["Updated"]


@pytest.mark.parametrize("changes", [{"delay": "bad"}, {"repeat": "257"},
                                         {"dwell_mode": "manual", "dwell": "0"}])
def test_compiled_settings_errors_keep_input_channel_thumbnails(changes):
    original = plan()
    segment = list_form.read_segment("sine", params("sine"), "2")
    original.channels.append(ChannelList(2, segments=[segment]))
    before = copy.deepcopy(original)
    raw = settings(channel=2, **changes)
    with pytest.raises(ValueError) as error:
        list_form.parse_settings(raw)
    view = list_form.compiled_view(original, raw, 0, {})
    assert original == before
    assert not view["ok"] and view["message"] == str(error.value)
    assert view["statistics"] == "" and view["plan"] is None
    assert view["budget"] is None and view["lanes"] == []
    assert view["segments"] == [{"title": "1 Sine ×2", "samples":
                                 list_form.draft_view("sine", params("sine"), "2")["preview"]["samples"]}]


def test_list_form_sources_and_translation_keys():
    source = Path(list_form.__file__).read_text(encoding="utf-8")
    assert not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", source)
    keys = re.findall(r'tr\("([^"\n]+)"', source)
    assert set(keys) <= i18n.STRINGS.keys()
    assert {key for fields in FIELDS.values() for key in fields} <= i18n.STRINGS.keys()
    assert not re.search(r"\b(?:import|from)\s+(?:tkinter|webview)\b", source)


def test_compilation_exception_keeps_thumbnails(monkeypatch):
    def fail(plan):
        raise ValueError("cannot compile")

    monkeypatch.setattr(synth, "compile_plan", fail)
    view = list_form.compiled_view(plan(), settings(), 0, {})
    assert not view["ok"] and view["message"] == "cannot compile"
    assert view["plan"] is None and view["budget"] is None
    assert view["statistics"] == "" and view["lanes"] == []
    assert len(view["axis"]) == 5
    assert len(view["segments"][0]["samples"]) == 200
