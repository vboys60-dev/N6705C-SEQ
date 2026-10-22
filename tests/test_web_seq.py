"""Sequence bridge checks with fake dialogs and instruments."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from n6705c.core.group import InstrumentGroup
from n6705c_seq import form, i18n, web_seq, webgui
from n6705c_seq.execute import DEMO_SLOTS, OutputsNotConfirmedOff, PlanRejected
from n6705c_seq.web_seq import SeqApi


class _Api(SeqApi, webgui._Core):
    pass


class _Window:
    def __init__(self):
        self.confirm = True
        self.path = None
        self.calls = []

    def create_confirmation_dialog(self, title, text):
        self.calls.append((title, text))
        return self.confirm

    def create_file_dialog(self, *args, **kwargs):
        return self.path


@pytest.fixture
def api(tmp_path, monkeypatch):
    original = i18n.lang
    value = _Api(tmp_path / "config.json")
    i18n.set_lang("en")
    value._window = _Window()
    monkeypatch.setattr(web_seq, "detect_model", lambda resource: "N6705C")
    yield value
    i18n.set_lang(original)


@pytest.fixture
def threads(monkeypatch):
    pending = []

    class Thread:
        def __init__(self, *, target, args, daemon):
            assert daemon is True
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

        def run(self):
            self.target(*self.args)

    monkeypatch.setattr(web_seq.threading, "Thread", Thread)
    return pending


def _raw(count=1):
    return form.default_raw(count)


def _results(api):
    return [event for event in api.poll() if event[0] == "result"]


def _group(api, monkeypatch):
    closed = []
    group = SimpleNamespace(instruments=[SimpleNamespace(close=lambda: closed.append(api._busy))])
    monkeypatch.setattr(web_seq, "connect_group", lambda resources: group)
    return group, closed


def test_meta_preview_and_invalid_fields(api):
    meta = api.seq_meta()
    assert meta == {"frames": 2, "channels_per_frame": 4, "palette": form.PALETTE,
                    "columns": [list(c) for c in form.COLUMNS], "rows": _raw(8)}
    assert api.seq_preview(12, _raw(), False) == {"rev": 12, **form.seq_view(form.parse_rows(_raw()), False, {})}
    meta["rows"][0]["name"] = "changed"
    assert api.seq_meta()["rows"][0]["name"] == "CH1"
    raw = _raw()
    raw[0]["rise"] = "bad"
    preview = api.seq_preview(13, raw, True)
    assert preview["rev"] == 13 and preview["lanes"] == [] and preview["ticks"] == []
    assert preview["t_end"] == 1.0 and "Rise" in preview["message"]
    assert api.seq_run(raw, [], True, False, False) == {"error": preview["message"], "title": i18n.tr("invalid_field")}
    assert not api._busy and not api._window.calls


def test_csv_roundtrip_cancel_capacity_and_invalid_current(api, tmp_path):
    assert api.seq_open_csv(_raw()) == {"cancelled": True}
    assert api.seq_save_csv(_raw()) == {"cancelled": True}
    api._window.path = str(tmp_path / "sequence")
    raw = _raw(2)
    raw[0]["name"] = "<rail>"
    raw[0]["limit"] = "0.125"
    assert api.seq_save_csv(raw) == {}
    assert (tmp_path / "sequence.csv").exists()
    api._window.path += ".csv"
    assert api.seq_open_csv(_raw(2)) == {"rows": raw}
    assert "error" in api.seq_open_csv(_raw())
    invalid = _raw(3)
    invalid[0]["delay"] = "bad"
    loaded = api.seq_open_csv(invalid)["rows"]
    assert loaded[:2] == raw and loaded[2]["enabled"] is False
    assert api.seq_save_csv(invalid).get("error")
    api._window.path = str(tmp_path / "missing.csv")
    assert api.seq_open_csv(_raw()).get("error")


@pytest.mark.parametrize("method,args", [
    ("seq_preview", (True, _raw(), False)),
    ("seq_preview", (0, _raw(), 0)),
    ("seq_preview", (0, {}, False)),
    ("seq_open_csv", ([{}],)),
    ("seq_save_csv", ([{**_raw()[0], "enabled": 1}],)),
    ("seq_run", (_raw(), "address", True, False, False)),
    ("seq_run", (_raw(), [None], True, False, False)),
    ("seq_run", (_raw(), [], 1, False, False)),
    ("seq_run", (_raw(), [], True, "false", False)),
    ("seq_run", (_raw(), [], True, False, None)),
    ("test_connection", (("address",), False)),
    ("test_connection", ([], None)),
    ("all_off", ([1], False)),
    ("all_off", ([], 0)),
])
def test_bridge_types_raise_typeerror(api, method, args):
    with pytest.raises(TypeError):
        getattr(api, method)(*args)
    assert not api._busy


def test_no_channel_missing_addresses_and_busy(api, threads):
    raw = _raw()
    raw[0]["enabled"] = False
    assert api.seq_run(raw, [], True, False, False) == {"error": i18n.tr("enable_channel"), "title": i18n.tr("no_channel")}
    for addresses in ([], ["x"], ["x", " "]):
        assert api.seq_run(_raw(8), addresses, False, False, False) == {"error": i18n.tr("address_required", frames=2), "title": i18n.tr("missing_address")}
    assert api._reserve()
    assert api.seq_run(_raw(), [], True, False, False) == {"error": i18n.tr("busy_body")}
    assert api.test_connection([], True) == {"error": i18n.tr("busy_body")}
    assert api.all_off(["x"], False) == {"error": i18n.tr("busy_body")}
    assert threads == []


@pytest.mark.parametrize("operation", ["run", "off"])
def test_confirmation_cancel_releases(api, threads, operation):
    api._window.confirm = False
    result = (api.seq_run(_raw(), ["x"], False, False, False) if operation == "run"
              else api.all_off(["x"], False))
    assert result == {"cancelled": True}
    assert api.poll() == [["busy", True], ["busy", False]]
    assert not api._busy and not threads and not api._config_path.exists()


def test_snapshot_frozen_before_confirmation(api, threads, monkeypatch):
    raw, addresses = _raw(), [" first ", "second"]
    calls = []
    _, closed = _group(api, monkeypatch)

    def confirm(title, text):
        assert api._busy
        raw[0]["high"] = "99"
        addresses[:] = ["later"]
        return True

    api._window.create_confirmation_dialog = confirm
    monkeypatch.setattr(web_seq, "run_plan", lambda group, rows, **kw: calls.append((rows, kw)))
    assert api.seq_run(raw, addresses, False, True, True) == {}
    assert api._busy
    assert api.config()["resources"] == [" first ", "second"]
    thread = threads.pop()
    assert thread.args[2] == ["first"]
    thread.run()
    assert calls[0][0][0].high_v == 12
    assert calls[0][1] == {"hold_dc": True, "capture": {}}
    assert closed == [True] and not api._busy
    assert api.poll()[-1] == ["busy", False]


@pytest.mark.parametrize("failure", [RuntimeError("boom"), PlanRejected("bad plan"), OutputsNotConfirmedOff("unknown")])
def test_worker_failure_notes_and_close_before_release(api, threads, monkeypatch, failure):
    _, closed = _group(api, monkeypatch)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(web_seq, "run_plan", fail)
    assert api.seq_run(_raw(), ["x"], False, False, False) == {}
    threads.pop().run()
    expected = ("" if isinstance(failure, OutputsNotConfirmedOff) else i18n.tr("rejected")
                if isinstance(failure, PlanRejected) else i18n.tr("forced_off"))
    assert _results(api) == [["result", "error", i18n.tr("run_result"), str(failure) + expected]]
    assert closed == [True] and not api._busy


def test_connection_failure_and_startup_failure_release(api, threads, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(web_seq, "connect_group", fail)
    assert api.seq_run(_raw(), ["x"], False, False, False) == {}
    threads.pop().run()
    assert _results(api)[0][3] == i18n.tr("connection_error", error="unavailable")
    assert not api._busy
    monkeypatch.setattr(api, "_save_config", fail)
    assert api.seq_run(_raw(), ["x"], False, False, False) == {"error": "unavailable"}
    assert not api._busy and not threads


def test_capture_overlay_and_capture_error(api, threads, monkeypatch):
    _group(api, monkeypatch)

    def run(group, rows, *, capture, hold_dc):
        capture.update({1: (0.001, [0, 1, 2]), "error": "partial"})

    monkeypatch.setattr(web_seq, "run_plan", run)
    api._measured["seq"] = {2: (1, [4])}
    assert api.seq_run(_raw(), ["x"], False, False, True) == {}
    assert api._measured["seq"] == {}
    threads.pop().run()
    events = api.poll()
    assert ["measured", "seq"] in events
    assert api._measured["seq"] == {1: (0.001, [0, 1, 2])}
    assert "partial" in next(e[3] for e in events if e[0] == "result")
    assert api.seq_preview(1, _raw(), False)["lanes"][0]["measured"] == [[0, 0], [1, 1], [2, 2]]


def test_e36441a_uses_own_runner_and_refuses_capture(api, threads, monkeypatch):
    calls = []
    monkeypatch.setattr(web_seq, "detect_model", lambda resource: "E36441A")
    monkeypatch.setattr(web_seq, "run_resources", lambda resources, rows, hold_dc: calls.append((resources, hold_dc)))
    monkeypatch.setattr(web_seq, "connect_group", lambda *args: pytest.fail("N6705C path used"))
    for capture in (False, True):
        api.seq_run(_raw(), ["x"], False, True, capture)
        threads.pop().run()
        results = _results(api)
        assert results == [["result", "error" if capture else "info", i18n.tr("run_result"),
                            i18n.tr("e36441a_no_capture" if capture else "sequence_hold_done")]]
        assert not api._busy
    assert calls == [(["x"], True)]


def test_mixed_models_rejected_before_runners(api, threads, monkeypatch):
    monkeypatch.setattr(web_seq, "detect_model", lambda resource: {"first": "N6705C", "second": "E36441A"}[resource])
    monkeypatch.setattr(web_seq, "connect_group", lambda *args: pytest.fail("settings path opened"))
    monkeypatch.setattr(web_seq, "run_resources", lambda *args, **kw: pytest.fail("settings path opened"))
    api.seq_run(_raw(8), ["first", "second"], False, False, False)
    threads.pop().run()
    assert "mixed instrument models" in _results(api)[0][3]
    assert not api._busy
    api.all_off(["first", "second"], False)
    threads.pop().run()
    assert "mixed instrument models" in _results(api)[0][3]
    assert not api._busy


@pytest.mark.parametrize("model", ["N6705C", "E36441A"])
def test_all_off_routes_and_closes(api, threads, monkeypatch, model):
    monkeypatch.setattr(web_seq, "detect_model", lambda resource: model)
    calls = []
    group = InstrumentGroup.demo(frames=1, slots=DEMO_SLOTS)
    original_close = group.instruments[0].close

    def close():
        assert api._busy
        calls.append("close")
        original_close()

    monkeypatch.setattr(group.instruments[0], "close", close)
    monkeypatch.setattr(web_seq, "connect_group", lambda resources: group)
    monkeypatch.setattr(web_seq, "off_resources", lambda resources: calls.append(resources))
    assert api.all_off([" x ", ""], False) == {}
    threads.pop().run()
    assert calls == (["close"] if model == "N6705C" else [["x"]])
    assert _results(api) == [["result", "info", i18n.tr("run_result"), i18n.tr("off_done")]]
    assert not api._busy


def test_all_off_demo_or_empty_is_immediate(api, threads):
    for addresses, demo in [(["x"], True), ([" "], False)]:
        assert api.all_off(addresses, demo) == {"info": i18n.tr("off_demo")}
    assert not threads and not api._busy and not api._window.calls and not api.poll()


@pytest.mark.parametrize("model", ["N6705C", "E36441A"])
def test_connection_status_kind_and_no_confirmation(api, threads, monkeypatch, model):
    monkeypatch.setattr(web_seq, "detect_model", lambda resource: model)
    group = InstrumentGroup.demo(frames=1, slots=DEMO_SLOTS)
    closed = []
    monkeypatch.setattr(group.instruments[0], "close", lambda: closed.append(api._busy))
    monkeypatch.setattr(web_seq, "connect_group", lambda resources: group)
    assert api.test_connection(["x", ""], False) == {}
    assert not api._window.calls
    threads.pop().run()
    events = api.poll()
    assert ["status", 1, i18n.tr("not_set")] in events
    assert any(e[:2] == ["status", 0] and e[2].startswith("✔ ") for e in events)
    assert ["kind", model] in events and api._kind == model
    assert closed == ([True] if model == "N6705C" else [])
    assert events[-1] == ["busy", False] and not api._busy


def test_connection_errors_and_demo(api, threads, monkeypatch):
    def fail(resource):
        raise RuntimeError("offline\nmore details")

    monkeypatch.setattr(web_seq, "detect_model", fail)
    api.test_connection(["x"], False)
    threads.pop().run()
    events = api.poll()
    assert ["status", 0, "✘ offline"] in events
    assert ["result", "error", i18n.tr("connection_failed"),
            i18n.tr("frame_connection_error", number=1, error="offline\nmore details")] in events
    assert not api._busy
    api.test_connection(["", ""], True)
    threads.pop().run()
    assert api._kind == "N6705C" and not api._busy
    assert sum(e[0] == "status" and e[2].startswith("✔ ") for e in api.poll()) == 2


def test_public_surface():
    assert {key for key in dir(SeqApi) if not key.startswith("_")} == {
        "seq_meta", "seq_preview", "seq_open_csv", "seq_save_csv", "seq_run", "test_connection", "all_off",
    }


def test_sources_have_no_cjk_text():
    source = Path(web_seq.__file__)
    for path in (source, source.parent / "web" / "seq.js", source.parent / "web" / "seq.css", Path(__file__)):
        assert not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", path.read_text(encoding="utf-8")), path


def test_translation_keys():
    source = Path(web_seq.__file__)
    python = source.read_text()
    js = (source.parent / "web" / "seq.js").read_text()
    keys = re.findall(r'tr\("([^"\n]+)"', python) + re.findall(r'App\.t\("([^"\n]+)"', js)
    keys += re.findall(r'data-i18n="([^"\n]+)"', js)
    keys += re.findall(r'el\("[^"\n]+", [^,\n]+, "([a-z_]+)"', js)
    keys += re.findall(r'checkbox\([^,\n]+, "([a-z_]+)"', js)
    keys += [title for _, title in form.COLUMNS] + ["enabled", "color", "frame_channel"]
    assert set(keys) <= i18n.STRINGS.keys()


@pytest.mark.parametrize("operation", ["run", "off"])
def test_dialog_exception_releases(api, threads, monkeypatch, operation):
    def fail(*args):
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(api._window, "create_confirmation_dialog", fail)
    response = (api.seq_run(_raw(), ["x"], False, False, False) if operation == "run"
                else api.all_off(["x"], False))
    assert response == {"error": "dialog failed"}
    assert not api._busy and not threads
    assert api.poll() == [["busy", True], ["busy", False]]


def test_thread_start_failure_releases(api, monkeypatch):
    def fail(**kwargs):
        assert kwargs["daemon"] is True
        raise RuntimeError("thread failed")

    monkeypatch.setattr(web_seq.threading, "Thread", fail)
    assert api.seq_run(_raw(), [], True, False, False) == {"error": "thread failed"}
    assert not api._busy
    assert api.poll()[-1] == ["busy", False]


@pytest.mark.parametrize("stage", ["detect", "connect", "off", "close"])
def test_all_off_failures_release(api, threads, monkeypatch, stage):
    _, closed = _group(api, monkeypatch)

    def fail(*args):
        raise RuntimeError("failed")

    if stage == "detect":
        monkeypatch.setattr(web_seq, "detect_model", fail)
    elif stage == "connect":
        monkeypatch.setattr(web_seq, "connect_group", fail)
    elif stage == "off":
        monkeypatch.setattr(web_seq, "outputs_off", fail)
    else:
        def close():
            assert api._busy
            closed.append(True)
            raise RuntimeError("close failed")

        group, closed = _group(api, monkeypatch)
        monkeypatch.setattr(group.instruments[0], "close", close)
    if stage != "off":
        monkeypatch.setattr(web_seq, "outputs_off", lambda group: None)
    api.all_off(["x"], False)
    threads.pop().run()
    events = api.poll()
    assert any(e[:2] == ["result", "error"] and "failed" in e[3] for e in events)
    assert closed == ([True] if stage in ("off", "close") else [])
    assert events[-1] == ["busy", False] and not api._busy


def test_demo_run_uses_group_and_releases(api, threads, monkeypatch):
    calls = []
    group, closed = _group(api, monkeypatch)

    def demo(*, frames, slots):
        calls.append((frames, slots))
        return group

    monkeypatch.setattr(web_seq.InstrumentGroup, "demo", demo)
    monkeypatch.setattr(web_seq, "run_plan", lambda group, rows, **kwargs: None)
    assert api.seq_run(_raw(8), [], True, False, False) == {}
    threads.pop().run()
    assert calls == [(2, DEMO_SLOTS)] and closed == [True]
    assert _results(api) == [["result", "info", i18n.tr("run_result"), i18n.tr("sequence_done")]]
    assert not api._busy


@pytest.mark.parametrize("method", ["seq_run", "test_connection", "all_off"])
def test_config_save_matches_tk(api, threads, method):
    addresses = [" first ", "second"]
    if method == "seq_run":
        response = api.seq_run(_raw(), addresses, False, False, False)
    else:
        response = getattr(api, method)(addresses, False)
    assert response == {}
    assert api._config_path.exists() is (method != "all_off")
    if method != "all_off":
        assert form.load_config(api._config_path)["resources"] == addresses
    assert len(threads) == 1


def test_save_validation_title_and_write_error_default(api, monkeypatch):
    raw = _raw()
    raw[0]["rise"] = "bad"
    assert api.seq_save_csv(raw)["title"] == i18n.tr("invalid_field")
    assert not api._window.calls
    api._window.path = "unused.csv"

    def fail(*args):
        raise OSError("write failed")

    monkeypatch.setattr(web_seq, "write_csv", fail)
    assert api.seq_save_csv(_raw()) == {"error": "write failed"}


def test_javascript_language_kind_and_thin_lanes():
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const painted = [], timers = [], listeners = {}, events = [];
class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.events = {}; this.attributes = {}; this.value = ""; this.checked = false;
    this.disabled = false; this.clientWidth = 600; this.clientHeight = 200;
    this.classList = {add() {}, toggle() {}};
  }
  appendChild(child) { child.parent = this; this.children.push(child); }
  append(child) { this.appendChild(child); }
  replaceChildren() { this.children = []; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children.at(-1); }
  getBoundingClientRect() { return {width: 600, top: 0}; }
  scrollIntoView() {}
  addEventListener(type, fn) { this.events[type] = fn; }
  setAttribute(key, value) { this.attributes[key] = value; }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)])
      .filter(child => selector === "input" ? child.tagName === "input" :
        selector === "#btn-off, [data-action='run']" ? child.dataset.action === "run" :
        selector === "[data-i18n]" && child.dataset.i18n);
  }
  getContext() {
    return {scale() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
      stroke() {}, fillRect() {}, strokeRect() {},
      measureText(text) { return {width: text.length * 7}; }, setLineDash() {}, fillText(text) { painted.push(text); }};
  }
}
const ids = Object.fromEntries(["seq-tab", "list-tab", "res-0", "res-1", "conn-0", "conn-1",
  "btn-test", "btn-off", "btn-lang", "chk-demo", "tab-seq", "tab-list", "result-dialog",
  "result-title", "result-text"].map(id => [id, new Node("div")]));
const document = global.document = {getElementById: id => ids[id], createElement: tag => new Node(tag),
  documentElement: {}, querySelectorAll(selector) {
    return [...(selector === "#btn-off, [data-action='run']" ? [ids["btn-off"]] : []),
      ...Object.values(ids).flatMap(node => node.querySelectorAll(selector))];
  }};
const meta = META, strings = STRINGS;
let lang = "zh", lanes = 8, listOk = false;
const api = {
  async strings() { return {lang, strings}; },
  async config() { return {resources: [], demo: true}; },
  async set_lang(value) { lang = value; },
  async selftest_report() {},
  async poll() { return events.splice(0); },
  async seq_meta() { return meta; },
  async list_meta() { return LIST_META; },
  async list_compile(rev) { return {rev, ok: listOk, lanes: [], segments: []}; },
  async list_draft(rev) { return {rev, preview: null}; },
  async seq_preview(rev) {
    return {rev, message: "", ticks: [], t_end: 100, time_axis: "axis", lanes:
      Array.from({length: lanes}, (_, i) => ({channel: i + 1, name: "rail", low: 0, high: 1,
        points: [[0, 0], [100, 1]], measured: null, delay: 1, top0: 2, top1: 3,
        delay_text: "delay marker", hold_text: "hold marker", levels_text: "levels"}))};
  },
};
global.window = {App: null, pywebview: {api}, devicePixelRatio: 1,
  addEventListener(type, fn) { listeners[type] = fn; },
  setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; }, clearTimeout() {}};
global.ResizeObserver = class { observe() {} };
global.getComputedStyle = () => ({fontFamily: "sans-serif"});
for (const file of [APP, LAYOUT, SOURCE, LIST]) vm.runInThisContext(fs.readFileSync(file, "utf8"));
const all = (root = ids["seq-tab"]) => [root, ...root.children.flatMap(child => all(child))];
async function timer(ms) {
  const index = timers.findLastIndex(item => item.ms === ms);
  assert(index >= 0);
  await timers.splice(index, 1)[0].fn();
}
(async () => {
  await listeners.pywebviewready();
  await timer(30);
  const seqRun = all().find(n => n.dataset.action === "run");
  const listRun = all(ids["list-tab"]).find(n => n.dataset.action === "run");
  for (const ok of [false, true]) {
    listOk = ok;
    events.push(["measured", "list"]); await timer(100);
    await Promise.resolve();
    assert.equal(listRun.disabled, !ok);
    events.push(["busy", true]); await timer(100);
    for (const button of [seqRun, listRun, ids["btn-off"], ids["btn-test"]]) assert(button.disabled);
    events.push(["busy", false]); await timer(100);
    for (const button of [seqRun, ids["btn-off"], ids["btn-test"]]) assert(!button.disabled);
    assert.equal(listRun.disabled, !ok);
  }
  assert(!painted.includes("delay marker") && !painted.includes("hold marker"));
  const inputs = all().filter(n => n.tagName === "input" && n.type === "text");
  const name = inputs[0]; name.value = "typed sequence";
  lanes = 1; painted.length = 0;
  await ids["btn-lang"].events.click(); await timer(30);
  assert.equal(lang, "en");
  assert.equal(inputs[0], name); assert.equal(name.value, "typed sequence");
  assert(painted.includes("delay marker") && painted.includes("hold marker"));
  assert.equal(all().find(n => n.dataset.i18n === "run_sequence").textContent,
    strings.run_sequence[1]);
  await ids["btn-lang"].events.click(); await timer(30);
  assert.equal(lang, "zh"); assert.equal(name.value, "typed sequence");
  const capture = all().find(n => n.tagName === "label" &&
    n.children.some(c => c.dataset.i18n === "capture")).children[0];
  capture.checked = true;
  events.push(["kind", "E36441A"]); await timer(100);
  assert(capture.disabled && !capture.checked && ids["tab-list"].disabled);
  assert.equal(ids["tab-seq"].attributes["aria-selected"], "true");
  events.push(["kind", "N6705C"]); await timer(100);
  assert(!capture.disabled && ids["tab-list"].disabled); // List tab locked until verified
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    web = Path(web_seq.__file__).parent / "web"
    from n6705c_seq import list_form

    script = script.replace("LIST_META", json.dumps(list_form.meta()))
    script = script.replace("META", json.dumps(_Api.seq_meta(None)))
    script = script.replace("STRINGS", json.dumps(i18n.STRINGS))
    script = script.replace("APP", json.dumps(str(web / "app.js")))
    script = script.replace("SOURCE", json.dumps(str(web / "seq.js")))
    script = script.replace("LAYOUT", json.dumps(str(web / "layout.js")))
    script = script.replace("LIST", json.dumps(str(web / "list.js")))
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
