"""List bridge checks without creating a window."""

import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from n6705c.core.group import InstrumentGroup
from n6705c_seq import i18n, list_form, synth, web_list, webgui
from n6705c_seq.listplan import ChannelList, ListPlan, Segment, load_json, write_json
from n6705c_seq.web_list import ListApi


class _Api(ListApi, webgui._Core):
    pass


class _Window:
    def __init__(self):
        self.path = None
        self.confirm = True
        self.calls = []

    def create_file_dialog(self, *args, **kwargs):
        return self.path

    def create_confirmation_dialog(self, title, text):
        self.calls.append((title, text))
        return self.confirm


@pytest.fixture
def api(tmp_path):
    original = i18n.lang
    bridge = _Api(tmp_path / "config.json")
    bridge._window = _Window()
    i18n.set_lang("en")
    yield bridge
    i18n.set_lang(original)


def _settings():
    return {"channel": 1, "enabled": True, "label": "", "delay": "0", "limit": "",
            "repeat": "1", "hold_last": False, "dwell_mode": "auto", "dwell": "0.00001024"}


def _plan():
    return list_form.plan_to_dict(ListPlan([ChannelList(1, segments=[
        Segment("step", {"start": 0, "end": 1, "delay": 0.01, "end_time": 0.01})])]))


def _draft(api, end="1", repeat="1", rev=1):
    return api.list_draft(rev, "step", {"start": "0", "end": end, "delay": "0.01", "end_time": "0.01"}, repeat)


def test_segment_array_operations_compile_and_draft(api):
    plan = _plan()
    segments = plan["channels"][0]["segments"]
    first = segments[0]
    second = _draft(api, "2")["segment"]
    segments.insert(1, second)
    segments[0], segments[1] = segments[1], segments[0]
    assert api.list_compile(2, plan, _settings(), 0)["segments"][0]["title"].startswith("1 Step")
    assert api.list_compile(3, plan, _settings(), 0)["plan"]["channels"][0]["segments"] == [second, first]
    segments[0], segments[1] = segments[1], segments[0]
    segments[1] = _draft(api, "3", "4")["segment"]
    view = api.list_compile(4, plan, _settings(), 1)
    assert view["plan"]["channels"][0]["segments"][1]["params"]["end"] == 3
    assert "Segment 2:" in view["statistics"]
    assert segments[1]["repeat"] == 4
    segments.pop()
    assert api.list_compile(5, plan, _settings(), 0)["plan"]["channels"][0]["segments"] == [first]
    segments.pop()
    assert api.list_compile(6, plan, _settings(), None)["segments"] == []


@pytest.mark.parametrize("empty", [False, True])
def test_json_round_trip_and_new_channel(api, tmp_path, empty):
    plan = ListPlan([] if empty else [ChannelList(2, True, "supply", 0.123456789012345,
                    0.987654321098765, [Segment("step", {"start": 0, "end": 2, "delay": 0, "end_time": 0.02}, 3)])],
                    repeat=4, hold_last=True, dwell=0.00123456789012345)
    path, output = tmp_path / "source.json", tmp_path / "saved.json"
    write_json(plan, path)
    api._window.path = (str(path),)
    api._measured["list"] = {2: (0.01, [1])}
    response = api.list_open_json()
    assert api._measured["list"] == {}
    api._window.path = str(output.with_suffix(""))
    assert api.list_save_json(response["plan"], response["settings"]) == {}
    assert load_json(output) == plan
    if not empty:
        assert response["settings"]["channel"] == 2
        assert "requested" in api.list_compile(1, response["plan"], response["settings"], 0)["statistics"]
        settings = {**response["settings"], "channel": 8, "delay": "0", "limit": "", "enabled": True}
        response["plan"]["channels"].append(list_form.plan_to_dict(ListPlan([ChannelList(8)]))["channels"][0])
        response["plan"]["channels"][-1]["segments"].append(_draft(api)["segment"])
        view = api.list_compile(2, response["plan"], settings, 0)
        assert list_form.plan_from_dict(view["plan"]).channels[0] == plan.channels[0]
        assert view["plan"]["channels"][-1]["channel"] == 8


def test_compile_errors_and_busy_block_run(api, monkeypatch):
    plan, settings = _plan(), _settings()
    assert api.list_compile(1, plan, settings, 0)["ok"]
    assert api._reserve()
    assert "error" in api.list_run(plan, settings, [], True, False)
    assert api._window.calls == []
    api._release()
    compiled = synth.compile_plan(list_form.plan_from_dict(plan))
    compiled.errors = ["over budget"]
    monkeypatch.setattr(synth, "compile_plan", lambda p: compiled)
    assert api.list_run(plan, settings, [], True, False) == {"error": "over budget", "title": i18n.tr("invalid_field")}
    assert not api.list_compile(2, plan, settings, None)["ok"]
    settings["delay"] = "nan"
    assert "error" in api.list_compile(3, plan, settings, None)
    assert "error" in api.list_run(plan, settings, [], True, False)
    assert api._window.calls == []


def test_preview_200_samples_and_all_shapes(api, monkeypatch):
    calls = []
    monkeypatch.setattr(synth, "value", lambda segment, t: calls.append(t) or t)
    draft = _draft(api, rev=99)
    assert draft["rev"] == 99
    assert len(draft["preview"]["samples"]) == 200
    assert calls[0] == 0 and calls[-1] == 0.02
    for shape, _ in api.list_meta()["shapes"]:
        params = {f["key"]: f["default"] for f in api.list_meta()["fields"][shape]}
        assert api.list_draft(1, shape, params, "1")["error"] is None
    assert _draft(api, "nan")["error"]


def test_run_snapshot_before_confirmation_and_shared_busy(api, monkeypatch):
    plan, settings, resources = _plan(), _settings(), [" test ", " other "]
    settings["hold_last"] = True
    jobs = []

    def confirm(title, text):
        assert api._busy
        plan["channels"][0]["segments"].clear()
        resources[0] = "changed"
        settings["hold_last"] = False
        return True

    class Thread:
        def __init__(self, *, target, args, daemon):
            assert daemon
            jobs.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(api._window, "create_confirmation_dialog", confirm)
    monkeypatch.setattr(web_list, "threading", SimpleNamespace(Thread=Thread))
    assert api.list_run(plan, settings, resources, True, True) == {}
    assert api._busy
    assert api.poll()[:1] == [["busy", True]]
    _, args = jobs[0]
    assert args[0].hold_last and len(args[0].channels[0].segments) == 1
    assert args[3] == ["test"]
    assert api._config["resources"] == [" test ", " other "]
    assert webgui.form.load_config(api._config_path)["resources"] == [" test ", " other "]
    assert "error" in api.list_run(_plan(), _settings(), [], True, False)
    assert len(jobs) == 1


def test_confirm_cancel_and_start_failure_release(api, monkeypatch):
    api._window.confirm = False
    assert api.list_run(_plan(), _settings(), [], True, False) == {"cancelled": True}
    assert not api._busy
    assert api.poll() == [["busy", True], ["busy", False]]
    api._window.confirm = True

    def fail(*args):
        raise OSError("config failure")

    monkeypatch.setattr(api, "_save_config", fail)
    assert api.list_run(_plan(), _settings(), [], True, False) == {"error": "config failure"}
    assert not api._busy


@pytest.mark.parametrize("failure", [None, "detect", "unsupported", "connect", "run", "unconfirmed", "convert", "close"])
def test_worker_functions_capture_errors_and_release(api, monkeypatch, failure):
    plan = ListPlan([ChannelList(1)], hold_last=True)
    compiled, functions = object(), [object()]
    group = InstrumentGroup.demo(frames=1, slots=web_list.execute.DEMO_SLOTS)
    closed = []

    def close():
        assert api._busy
        closed.append(True)
        if failure == "close":
            raise RuntimeError("close failed")

    monkeypatch.setattr(group.instruments[0], "close", close)

    def detect(resource):
        if failure == "detect":
            raise RuntimeError("detect failed")
        return "E36441A" if failure == "unsupported" else "N6705C"

    def connect(resources):
        assert resources == ["test"]
        if failure == "connect":
            raise RuntimeError("connection lost")
        return group

    def convert(got_plan, got_compiled):
        assert got_plan is plan and got_compiled is compiled
        if failure == "convert":
            raise ValueError("conversion failed")
        return functions

    def run(got_group, got_functions, *, keep_on, capture):
        assert got_group is group and got_functions is functions
        assert keep_on and capture == {}
        if failure == "run":
            raise RuntimeError("execution failed")
        if failure == "unconfirmed":
            raise web_list.execute.OutputsNotConfirmedOff("not confirmed")
        capture.update({1: (0.01, [0, 1]), "error": "capture failed"})

    monkeypatch.setattr(web_list.e36441a, "detect_model", detect)
    monkeypatch.setattr(web_list.execute, "connect_group", connect)
    monkeypatch.setattr(synth, "to_functions", convert)
    monkeypatch.setattr(web_list.execute, "run_functions", run)
    assert api._reserve()
    api._list_work(plan, compiled, 1, ["test"], False, True)
    assert not api._busy
    events = api.poll()
    assert events[-1] == ["busy", False]
    results = [e for e in events if e[0] == "result"]
    if failure:
        assert any(e[1] == "error" for e in results)
    else:
        assert results[0][1] == "info"
        assert "capture failed" in results[0][-1]
        assert api._measured["list"] == {1: (0.01, [0, 1])}
        assert ["measured", "list"] in events
    if failure == "unconfirmed":
        assert "Outputs were forced off" not in results[0][-1]
    if failure == "convert":
        assert "rejected before outputs were enabled" in results[0][-1]
    assert closed == ([] if failure in ("detect", "unsupported", "connect") else [True])


def test_demo_plan_end_to_end(api, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "examples" / "list_demo.json"
    api._window.path = str(source)
    opened = api.list_open_json()
    groups = []
    demo = InstrumentGroup.demo

    def create(*, frames, slots):
        group = demo(frames=frames, slots=slots)
        groups.append(group)
        return group

    class Thread:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(InstrumentGroup, "demo", create)
    monkeypatch.setattr(web_list, "threading", SimpleNamespace(Thread=Thread))
    assert api.list_run(opened["plan"], opened["settings"], [], True, False) == {}
    assert not api._busy
    events = api.poll()
    assert any(e[0] == "result" and e[1] == "info" and "outputs are off" in e[-1] for e in events)
    assert len(groups) == 1
    for inst in groups[0].instruments:
        assert not any(inst.output_state(inst.populated_channels).values())


def test_open_errors_cancel_and_missing_addresses(api, tmp_path):
    assert api.list_open_json() == {"cancelled": True}
    assert api.list_save_json(_plan(), _settings()) == {"cancelled": True}
    api._measured["list"] = {1: (0.1, [1])}
    path = tmp_path / "bad.json"
    path.write_text("{")
    api._window.path = str(path)
    assert "error" in api.list_open_json()
    assert api._measured["list"]
    write_json(ListPlan([ChannelList(9)]), path)
    assert "8 channels" in api.list_open_json()["error"]
    assert "addresses" in api.list_run(_plan(), _settings(), [], False, False)["error"]
    assert not api._busy


@pytest.mark.parametrize("text", ["nan", "inf", "-1", "0", "1.5"])
def test_invalid_repeat_values(api, text):
    assert _draft(api, repeat=text)["error"]
    assert "error" in api.list_run(_plan(), {**_settings(), "repeat": text}, [], True, False)


def test_payload_types(api):
    calls = [lambda: api.list_draft(True, "step", {}, "1"),
             lambda: api.list_draft(1, "step", {}, "1"),
             lambda: api.list_draft(1, "step", {"start": 0, "end": "1", "delay": "0", "end_time": "1"}, "1"),
             lambda: api.list_compile(1, [], _settings(), None),
             lambda: api.list_compile(1, _plan(), _settings(), False),
             lambda: api.list_compile(1, _plan(), {**_settings(), "enabled": 1}, None),
             lambda: api.list_save_json({"channels": [None]}, _settings()),
             lambda: api.list_run(_plan(), _settings(), "test", True, False),
             lambda: api.list_run(_plan(), _settings(), [None], True, False),
             lambda: api.list_run(_plan(), _settings(), [], 1, False),
             lambda: api.list_run(_plan(), _settings(), [], True, 0)]
    for call in calls:
        with pytest.raises(TypeError):
            call()
    raw = copy.deepcopy(_plan())
    raw["channels"][0]["segments"][0]["params"]["end"] = "2"
    with pytest.raises(TypeError):
        api.list_compile(1, raw, _settings(), None)


def test_sources_and_translation_keys():
    source = Path(web_list.__file__)
    paths = [source, source.parent / "web" / "list.js", Path(__file__)]
    keys = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", text), path
        if path == Path(__file__):
            continue
        keys += re.findall(r'tr\("([^"\n]+)"', text)
        keys += re.findall(r'\.t\("([^"\n]+)"', text)
        keys += re.findall(r'data-i18n="([^"\n]+)"', text)
        if path.suffix == ".js":
            keys += re.findall(r'(?:button|input|element)\("?[^,\n]+,\s*"([a-z_]+)"', text)
    assert set(keys) <= i18n.STRINGS.keys()
    assert {key for key in ListApi.__dict__ if not key.startswith("_")} == {
        "list_meta", "list_draft", "list_compile", "list_open_json", "list_save_json", "list_run"}
    assert "innerHTML" not in paths[1].read_text()


def test_list_javascript_handlers_without_window(api):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    web = Path(web_list.__file__).parent / "web"
    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.events = {}; this.value = ""; this.checked = false; this.disabled = false;
    this.classList = {add() {}, toggle() {}};
  }
  append(node) { node.parent = this; this.children.push(node); }
  replaceChildren() { this.children = []; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children.at(-1); }
  addEventListener(type, fn) { this.events[type] = fn; }
  async fire(type) { return this.events[type]?.({clientY: 20}); }
  getBoundingClientRect() { return {width: 600, top: 0}; }
  scrollIntoView() {}
  querySelectorAll(selector) {
    const all = this.children.flatMap(c => [c, ...c.querySelectorAll(selector)]);
    return all.filter(c => selector === "input" ? c.tagName === "input" : c.dataset.i18n);
  }
  getContext() {
    return {scale() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillRect() {},
      strokeRect() {}, fillText() {}, setLineDash() {}, measureText(text) { return {width: text.length * 7}; }};
  }
}
const elements = {"list-tab": new Node("section"), "res-0": new Node("input"),
  "res-1": new Node("input"), "chk-demo": new Node("input")};
elements["chk-demo"].checked = true;
global.document = {getElementById: id => elements[id], createElement: tag => new Node(tag)};
global.ResizeObserver = class { observe() {} };
const listeners = {}, messages = [];
let tab, args, placements = 0;
const clone = obj => JSON.parse(JSON.stringify(obj));
const meta = META;
function compile(rev, plan, settings, index) {
  plan = clone(plan);
  if (!Number.isFinite(Number(settings.delay))) return {rev, error: "invalid delay", ok: false};
  const ch = plan.channels.find(c => c.channel === settings.channel);
  if (ch) Object.assign(ch, {enabled: settings.enabled, label: settings.label, delay: Number(settings.delay)});
  const segments = ch?.segments || [];
  return {rev, plan, ok: segments.length > 0, message: "", statistics: "", axis: [], lanes: [],
    segments: segments.map((s, i) => ({title: `${i + 1} ${s.params.end}`, samples: Array(200).fill(s.params.end)}))};
}
const api = {
  async list_meta() { return meta; },
  async list_open_json() { return OPEN_RESPONSE; },
  async list_compile(...a) { return compile(...a); },
  async list_draft(rev, shape, params, repeat) {
    return {rev, error: null, segment: {shape, params: Object.fromEntries(Object.entries(params).map(([k,v]) => [k, Number(v)])), repeat: Number(repeat)}, preview: PREVIEW};
  },
  async list_run(...a) { args = a; return {}; },
};
global.window = {devicePixelRatio: 1, App: {
  api, busy: false, t: key => key, on(type, fn) { listeners[type] = fn; },
  register(value) { tab = value; }, relabel() {}, showResult(...a) { messages.push(a); },
}};
vm.runInThisContext(fs.readFileSync(LAYOUT, "utf8"));
const place = placeTimeLabels;
placeTimeLabels = (...a) => { placements++; assert(a[0].every(s => s.width > 0)); return place(...a); };
vm.runInThisContext(fs.readFileSync(SOURCE, "utf8"));
const root = elements["list-tab"];
function all(node = root) { return [node, ...node.children.flatMap(c => all(c))]; }
function action(key) { return all().find(n => n.tagName === "button" && n.dataset.i18n === key); }
function field(key) { return all().find(n => n.tagName === "label" && n.children.some(c => c.dataset.i18n === key)).children.find(n => n.tagName === "input"); }
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
(async () => {
  await tab.init(); await flush();
  assert(placements > 0);
  await action("add").fire("click"); await flush();
  field("end").value = "2";
  await action("add").fire("click"); await flush();
  await action("move_left").fire("click"); await flush();
  await action("run_list").fire("click");
  assert.deepEqual(args[0].channels[0].segments.map(s => s.params.end), [2, 1]);
  await action("move_left").fire("click"); await flush();
  await action("run_list").fire("click");
  assert.deepEqual(args[0].channels[0].segments.map(s => s.params.end), [2, 1]);
  const navigate = text => all().find(n => n.tagName === "button" && n.textContent === text);
  await navigate("\u25b6\u25b6").fire("click"); await flush();
  assert.equal(field("end").value, "1");
  await navigate("\u25c0\u25c0").fire("click"); await flush();
  assert.equal(field("end").value, "2");
  field("end").value = "3"; field("segment_repeat").value = "4";
  await action("update").fire("click"); await flush();
  await action("run_list").fire("click");
  assert.equal(args[0].channels[0].segments[0].repeat, 4);
  assert.equal(args[0].channels[0].segments[0].params.end, 3);
  await action("delete").fire("click"); await flush();
  await action("run_list").fire("click");
  assert.deepEqual(args[0].channels[0].segments.map(s => s.params.end), [1]);
  const choice = all().find(n => n.tagName === "select");
  field("channel_delay_s").value = "nan";
  choice.value = "2"; await choice.fire("change"); await flush();
  assert.equal(choice.value, "1"); assert.equal(messages.at(-1)[2], "invalid delay");
  field("channel_delay_s").value = "0";
  choice.value = "2"; await choice.fire("change"); await flush();
  assert.equal(choice.value, "2");
  const end = field("end"); end.value = "7.125";
  tab.relabel(); await flush();
  assert.equal(field("end"), end); assert.equal(end.value, "7.125");
  await action("open_json").fire("click"); await flush();
  assert.equal(choice.value, "1");
  assert.equal(field("current_limit_optional").value, "0.1");
  assert.equal(field("channel_delay_s").value, "0.001");
  choice.value = "2"; await choice.fire("change"); await flush();
  assert.equal(choice.value, "2");
  assert.equal(field("current_limit_optional").value, "0.1");
  assert.equal(field("channel_delay_s").value, "0.002");
  const pending = [];
  api.list_compile = (...a) => new Promise(resolve => pending.push({a, resolve}));
  field("name").value = "old"; field("name").fire("input");
  field("name").value = "new"; field("name").fire("input");
  assert.equal(pending.length, 2);
  pending[1].resolve({rev: pending[1].a[0], ok: false, error: "new invalid"}); await flush();
  pending[0].resolve({rev: pending[0].a[0], ok: true, message: "old valid"}); await flush();
  assert.equal(action("run_list").disabled, true);
  assert(all().some(n => n.textContent === "new invalid"));
  assert(!all().some(n => n.textContent === "old valid"));
  choice.value = "3";
  const switching = choice.fire("change");
  listeners.measured("list");
  tab.relabel();
  for (let i = 3; i < pending.length; i++) pending[i].resolve(compile(...pending[i].a));
  await flush();
  pending[2].resolve(compile(...pending[2].a));
  await switching; await flush();
  assert.equal(choice.value, "3");
  field("channel_delay_s").value = "nan";
  const start = pending.length;
  choice.value = "4";
  const blocked = choice.fire("change");
  listeners.measured("list");
  pending[start + 1].resolve(compile(...pending[start + 1].a));
  pending[start].resolve(compile(...pending[start].a));
  await blocked; await flush();
  assert.equal(choice.value, "3");
  assert.equal(messages.at(-1)[2], "invalid delay");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    preview = list_form.draft_view("step", {"start": "0", "end": "1", "delay": "0.01", "end_time": "0.01"}, "1")["preview"]
    api._window.path = str(Path(__file__).resolve().parents[1] / "examples" / "list_demo.json")
    script = script.replace("OPEN_RESPONSE", json.dumps(api.list_open_json()))
    script = script.replace("META", json.dumps(list_form.meta())).replace("PREVIEW", json.dumps(preview))
    script = script.replace("LAYOUT", json.dumps(str(web / "layout.js"))).replace("SOURCE", json.dumps(str(web / "list.js")))
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_run_error_titles(api):
    response = api.list_run(_plan(), _settings(), [" "], False, False)
    assert response == {"error": i18n.tr("address_required", frames=1),
                        "title": i18n.tr("missing_address")}
    response = api.list_run(_plan(), {**_settings(), "delay": "bad"}, [], True, False)
    assert response["title"] == i18n.tr("invalid_field")
    assert not api._busy and not api._window.calls


def test_open_numbers_use_shortest_repr(api):
    api._window.path = str(Path(__file__).resolve().parents[1] / "examples" / "list_demo.json")
    response = api.list_open_json()
    assert response["settings"]["limit"] == "0.1"
    assert "000000" not in response["settings"]["delay"]
    second = next(ch for ch in response["plan"]["channels"] if ch["channel"] == 2)
    assert second["current_limit"] == 0.1
    assert second["delay"] == 0.002
