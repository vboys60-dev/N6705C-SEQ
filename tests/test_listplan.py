from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from n6705c_seq.listplan import ChannelList, FIELDS, ListPlan, Segment, SHAPES, load_json, write_json

EXAMPLE = Path(__file__).parent.parent / "examples" / "list_demo.json"
PARAMS = {
    "step": {"start": 0, "end": 2, "delay": 0, "end_time": 0.01},
    "ramp": {"start": 0, "end": 2, "delay": 0, "ramp_time": 0.01, "end_time": 0},
    "trapezoid": {
        "offset": 1, "amplitude": 2, "start_time": 0, "up_time": 0.01,
        "high_time": 0.01, "down_time": 0.01, "end_time": 0,
    },
    "sine": {"offset": 2, "amplitude": 1, "frequency": 100, "phase": -90},
    "triangle": {"offset": 2, "amplitude": 1, "frequency": 100, "phase": 90},
    "staircase": {"start": 0, "end": 2, "delay": 0, "n_steps": 3, "stair_time": 0.01, "end_time": 0},
    "exponential": {"start": 0, "end": 2, "delay": 0, "tau": 0.001, "exp_time": 0.01, "end_time": 0},
}


def raw_plan(shape="ramp"):
    # Validation cases use CH2's third segment to check both location indices.
    segments = [Segment("step", PARAMS["step"].copy()) for _ in range(2)]
    segments.append(Segment(shape, PARAMS[shape].copy()))
    return asdict(ListPlan([ChannelList(2, segments=segments)]))


def write(tmp_path, raw):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_write_load_round_trip(tmp_path):
    plan = ListPlan(
        [
            ChannelList(1, label="所有波形", delay=0.002, current_limit=0.25,
                        segments=[Segment(shape, PARAMS[shape].copy(), 2) for shape in SHAPES]),
            ChannelList(2, enabled=False, label="備用"),
        ],
        repeat=256, hold_last=True, dwell=0.0001024,
    )
    path = tmp_path / "roundtrip.json"
    write_json(plan, path)
    assert load_json(path) == plan
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_example_loads():
    plan = load_json(EXAMPLE)
    assert [ch.channel for ch in plan.channels] == [1, 2]
    assert {seg.shape for ch in plan.channels for seg in ch.segments} >= {
        "ramp", "sine", "trapezoid", "step",
    }


def test_defaults_load(tmp_path):
    raw = {"channels": [{"channel": 1, "segments": [{"shape": "step", "params": PARAMS["step"]}]}]}
    assert load_json(write(tmp_path, raw)) == ListPlan([
        ChannelList(1, segments=[Segment("step", PARAMS["step"])]),
    ])


@pytest.mark.parametrize("shape", SHAPES)
def test_all_shapes_and_exact_field_keys_load(tmp_path, shape):
    assert tuple(PARAMS[shape]) == FIELDS[shape]
    plan = load_json(write(tmp_path, raw_plan(shape)))
    assert plan.channels[0].segments[2] == Segment(shape, PARAMS[shape])


@pytest.mark.parametrize("bad", ["square", "Ramp", None, [], {}])
def test_invalid_shape_has_segment_location(tmp_path, bad):
    raw = raw_plan()
    raw["channels"][0]["segments"][2]["shape"] = bad
    with pytest.raises(ValueError, match="CH2 第 3 段：.*shape"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("change", ["missing", "extra", "both"])
def test_params_must_have_exact_fields(tmp_path, change):
    raw = raw_plan()
    params = raw["channels"][0]["segments"][2]["params"]
    if change in ("missing", "both"):
        del params["ramp_time"]
    if change in ("extra", "both"):
        params["rise_time"] = 0.01
    with pytest.raises(ValueError, match="CH2 第 3 段：.*params"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("bad", ["1", None, True, [], {}])
def test_params_must_be_numeric(tmp_path, bad):
    raw = raw_plan()
    raw["channels"][0]["segments"][2]["params"]["start"] = bad
    with pytest.raises(ValueError, match="CH2 第 3 段：start 必須是數字"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_params_must_be_finite(tmp_path, bad):
    raw = raw_plan()
    raw["channels"][0]["segments"][2]["params"]["start"] = bad
    with pytest.raises(ValueError, match="CH2 第 3 段：start 必須是有限數字"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("shape,key", [
    ("step", "delay"), ("step", "end_time"), ("ramp", "ramp_time"),
    ("trapezoid", "start_time"), ("trapezoid", "up_time"),
    ("trapezoid", "high_time"), ("trapezoid", "down_time"),
    ("staircase", "stair_time"), ("exponential", "exp_time"), ("exponential", "tau"),
])
def test_negative_times_have_segment_location(tmp_path, shape, key):
    raw = raw_plan(shape)
    raw["channels"][0]["segments"][2]["params"][key] = -0.001
    with pytest.raises(ValueError, match=f"CH2 第 3 段：{key} 必須"):
        load_json(write(tmp_path, raw))


def test_negative_channel_delay_has_channel_location(tmp_path):
    raw = raw_plan()
    raw["channels"][0]["delay"] = -0.001
    with pytest.raises(ValueError, match="CH2：delay 必須 >= 0"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("bad", [0, -1])
def test_current_limit_must_be_positive(tmp_path, bad):
    raw = raw_plan()
    raw["channels"][0]["current_limit"] = bad
    with pytest.raises(ValueError, match="CH2：current_limit 必須 > 0（不限流請留空）"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("shape", SHAPES)
def test_zero_times_are_allowed_without_duration_validation(tmp_path, shape):
    raw = raw_plan(shape)
    params = raw["channels"][0]["segments"][2]["params"]
    for key in params:
        if key in ("delay", "end_time", "ramp_time", "start_time", "up_time", "high_time",
                   "down_time", "stair_time", "exp_time"):
            params[key] = 0
    assert load_json(write(tmp_path, raw)).channels[0].segments[2].params == params


@pytest.mark.parametrize("shape", ["sine", "triangle"])
@pytest.mark.parametrize("bad", [0, -1])
def test_frequency_must_be_positive(tmp_path, shape, bad):
    raw = raw_plan(shape)
    raw["channels"][0]["segments"][2]["params"]["frequency"] = bad
    with pytest.raises(ValueError, match="CH2 第 3 段：frequency 必須 > 0"):
        load_json(write(tmp_path, raw))


def test_zero_tau_is_rejected(tmp_path):
    raw = raw_plan("exponential")
    raw["channels"][0]["segments"][2]["params"]["tau"] = 0
    with pytest.raises(ValueError, match="CH2 第 3 段：tau 必須 > 0"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("bad", [0, -1, 1.5, 1.0, True, "2"])
def test_n_steps_must_be_positive_integer(tmp_path, bad):
    raw = raw_plan("staircase")
    raw["channels"][0]["segments"][2]["params"]["n_steps"] = bad
    with pytest.raises(ValueError, match="CH2 第 3 段：n_steps 必須"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("bad", [0, -1, 1.5, 1.0, True, "2"])
def test_segment_repeat_must_be_positive_integer(tmp_path, bad):
    raw = raw_plan()
    raw["channels"][0]["segments"][2]["repeat"] = bad
    with pytest.raises(ValueError, match="CH2 第 3 段：repeat 必須是 >= 1 的整數"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("bad", [0, -1, 1.5, 1.0, True, "2", 257])
def test_plan_repeat_must_be_integer_in_range(tmp_path, bad):
    raw = raw_plan()
    raw["repeat"] = bad
    with pytest.raises(ValueError, match="計畫：repeat 必須"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("repeat", [1, 256])
def test_plan_repeat_boundaries_load(tmp_path, repeat):
    raw = raw_plan()
    raw["repeat"] = repeat
    assert load_json(write(tmp_path, raw)).repeat == repeat


def test_segment_repeat_has_no_plan_repeat_cap(tmp_path):
    raw = raw_plan()
    raw["channels"][0]["segments"][2]["repeat"] = 257
    assert load_json(write(tmp_path, raw)).channels[0].segments[2].repeat == 257


def test_duplicate_channel_has_channel_location(tmp_path):
    raw = raw_plan()
    raw["channels"].append({"channel": 2, "enabled": False})
    with pytest.raises(ValueError, match="CH2：通道不可以重複"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("raw,location", [
    ([], "計畫"), ({}, "計畫"), ({"channels": {}}, "計畫"),
    ({"channels": [None]}, "計畫第 1 個通道"),
    ({"channels": [{"channel": 2, "segments": {}}]}, "CH2"),
    ({"channels": [{"channel": 2, "segments": [None]}]}, "CH2 第 1 段"),
    ({"channels": [{"channel": 2, "segments": [{"shape": "step", "params": []}]}]}, "CH2 第 1 段"),
])
def test_malformed_structure_is_value_error_with_location(tmp_path, raw, location):
    with pytest.raises(ValueError, match=f"{location}："):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("bad", [0, -1, 1.0, True, "2", None])
def test_invalid_channel_has_list_location(tmp_path, bad):
    raw = raw_plan()
    raw["channels"][0]["channel"] = bad
    with pytest.raises(ValueError, match="計畫第 1 個通道：channel"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("key,bad", [
    ("enabled", "yes"), ("label", 1), ("delay", True), ("current_limit", "1"),
])
def test_invalid_channel_field_has_channel_location(tmp_path, key, bad):
    raw = raw_plan()
    raw["channels"][0][key] = bad
    with pytest.raises(ValueError, match=f"CH2：{key}"):
        load_json(write(tmp_path, raw))


@pytest.mark.parametrize("key,bad", [("hold_last", 1), ("dwell", "0.001")])
def test_invalid_plan_field_has_plan_location(tmp_path, key, bad):
    raw = raw_plan()
    raw[key] = bad
    with pytest.raises(ValueError, match=f"計畫：{key}"):
        load_json(write(tmp_path, raw))


def test_invalid_json_has_plan_location(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"channels":', encoding="utf-8")
    with pytest.raises(ValueError, match="計畫：無法讀取 JSON"):
        load_json(path)


def test_unreadable_file_is_value_error(tmp_path):
    with pytest.raises(ValueError, match="計畫：無法讀取 JSON"):
        load_json(tmp_path / "missing.json")


def test_unwritable_file_is_value_error(tmp_path):
    with pytest.raises(ValueError, match="計畫：無法寫入 JSON"):
        write_json(ListPlan([]), tmp_path)
