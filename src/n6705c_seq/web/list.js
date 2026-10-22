"use strict";

(() => {
  const App = window.App;
  const root = document.getElementById("list-tab");
  const fields = {}, params = {};
  let meta, channel = 1, selected = null, compileRev = 0, draftRev = 0, switchRev = 0;
  let compiled = null, draft = null;
  let plan = {channels: Array.from({length: 8}, (_, i) => emptyChannel(i + 1, i === 0)),
    repeat: 1, hold_last: false, dwell: null};
  function emptyChannel(number, enabled = false) {
    return {channel: number, enabled, label: "", delay: 0, current_limit: null, segments: []};
  }
  function element(tag, parent, key, cls) {
    const node = document.createElement(tag);
    if (key) { node.dataset.i18n = key; node.textContent = App.t(key); }
    if (cls) node.className = cls;
    parent.append(node);
    return node;
  }
  function button(parent, key, action, text) {
    const node = element("button", parent, key, "btn");
    if (text) node.textContent = text;
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }
  function input(parent, key, name, value, type = "text") {
    const label = element("label", parent, null, "list-field");
    const node = element("input", label, null, type === "checkbox" ? "" : "field");
    node.type = type;
    if (type === "checkbox") node.checked = value;
    else node.value = value;
    element("span", label, key);
    fields[name] = node;
    return node;
  }
  function current(create = false) {
    let ch = plan.channels.find(ch => ch.channel === channel);
    if (!ch && create) { ch = emptyChannel(channel); plan.channels.push(ch); }
    return ch || emptyChannel(channel);
  }
  function settings() {
    return {channel, enabled: fields.enabled.checked, label: fields.label.value,
      delay: fields.delay.value, limit: fields.limit.value, repeat: fields.repeat.value,
      hold_last: fields.hold_last.checked, dwell_mode: fields.manual.checked ? "manual" : "auto",
      dwell: fields.dwell.value};
  }
  function draftArgs() {
    return [shape.value, Object.fromEntries(Object.entries(params).map(([k, v]) => [k, v.value])), fields.segment_repeat.value];
  }
  function result(response, title = "invalid_field") {
    if (response.error) App.showResult("error", response.title ?? App.t(title), response.error);
    return !response.error && !response.cancelled;
  }
  function runState() { run.disabled = App.busy || !compiled?.ok; }
  async function compile() {
    const rev = ++compileRev;
    compiled = null;
    runState();
    const view = await App.api.list_compile(rev, plan, settings(), selected);
    if (view.rev !== compileRev) return view;
    compiled = view;
    message.textContent = view.error || view.message || "";
    statistics.textContent = view.statistics || "";
    drawCompiled();
    drawSegments(view.segments || []);
    runState();
    return view;
  }
  async function preview() {
    const rev = ++draftRev;
    draft = null;
    drawDraft();
    const view = await App.api.list_draft(rev, ...draftArgs());
    if (view.rev !== draftRev) return view;
    draft = view.preview;
    draftError.textContent = view.error || "";
    drawDraft();
    return view;
  }
  function parameterForm(segment) {
    parameterBox.replaceChildren();
    for (const key of Object.keys(params)) delete params[key];
    for (const field of meta.fields[shape.value]) {
      const label = element("label", parameterBox, null, "list-parameter");
      element("span", label, field.key);
      const unit = element("span", label);
      unit.textContent = field.unit ? ` (${field.unit})` : "";
      const node = element("input", label, null, "field");
      node.value = String(segment ? segment.params[field.key] : field.default);
      node.addEventListener("input", preview);
      params[field.key] = node;
    }
    const label = element("label", parameterBox, null, "list-parameter");
    element("span", label, "segment_repeat");
    const repeat = fields.segment_repeat = element("input", label, null, "field");
    repeat.value = String(segment?.repeat ?? fields.segment_repeat_value ?? 1);
    repeat.addEventListener("input", preview);
    App.relabel(parameterBox);
    preview();
  }
  function selectSegment(index) {
    selected = index;
    const segment = index === null ? null : current().segments[index];
    if (segment) { shape.value = segment.shape; parameterForm(segment); }
    compile();
  }
  function loadChannel() {
    choice.value = String(channel);
    const ch = current();
    fields.enabled.checked = ch.enabled;
    fields.label.value = ch.label;
    fields.delay.value = String(ch.delay);
    fields.limit.value = ch.current_limit === null ? "" : String(ch.current_limit);
    selectSegment(ch.segments.length ? 0 : null);
    if (!ch.segments.length) preview();
  }
  async function selectChannel(number) {
    choice.value = String(channel);
    const rev = ++switchRev;
    const view = await App.api.list_compile(rev, plan, settings(), selected);
    if (rev !== switchRev || !result(view)) return;
    if (!view.plan) return;
    plan = view.plan;
    channel = number;
    loadChannel();
  }
  async function editSegment(update) {
    if (update && selected === null) return;
    const view = await preview();
    if (view.rev !== draftRev || !result(view)) return;
    const segments = current(true).segments;
    if (update) { segments[selected] = view.segment; compile(); }
    else {
      const index = selected === null ? segments.length : selected + 1;
      segments.splice(index, 0, view.segment);
      selectSegment(index);
    }
  }
  function removeSegment() {
    if (selected === null) return;
    const segments = current().segments;
    segments.splice(selected, 1);
    selectSegment(segments.length ? Math.min(selected, segments.length - 1) : null);
  }
  function moveSegment(direction) {
    if (selected === null) return;
    const segments = current().segments, target = selected + direction;
    if (target < 0 || target >= segments.length) return;
    [segments[selected], segments[target]] = [segments[target], segments[selected]];
    selectSegment(target);
  }
  function navigate(move) {
    const count = current().segments.length;
    if (count) selectSegment(move === "first" ? 0 : move === "last" ? count - 1 :
      Math.max(0, Math.min(count - 1, (selected ?? 0) + move)));
  }
  function canvasContext(canvas, height) {
    const width = Math.max(200, canvas.getBoundingClientRect().width), ratio = window.devicePixelRatio || 1;
    canvas.style.height = `${height}px`;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio); ctx.font = "12px sans-serif";
    ctx.fillStyle = "#202020"; ctx.strokeStyle = "#94a3b8";
    return [ctx, width];
  }
  function line(ctx, points, color = "#94a3b8", width = 1) {
    if (points.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
    points.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.stroke();
  }
  function curve(ctx, samples, left, top, width, height) {
    const lo = Math.min(...samples), hi = Math.max(...samples);
    line(ctx, samples.map((v, i) => [left + width * i / 199, top + height - height * (v - lo) / (hi - lo || 1)]), "#2563eb", 2);
  }
  function drawSegments(segments) {
    while (strip.children.length > segments.length) strip.lastChild.remove();
    segments.forEach((segment, i) => {
      let node = strip.children[i];
      if (!node) {
        node = button(strip, null, () => selectSegment(i));
        element("canvas", node, null, "chart"); element("span", node);
      }
      node.classList.toggle("selected", i === selected);
      node.lastChild.textContent = segment.title;
      const [ctx, width] = canvasContext(node.firstChild, 38);
      curve(ctx, segment.samples, 5, 4, width - 10, 30);
    });
    strip.children[selected]?.scrollIntoView({block: "nearest", inline: "nearest"});
  }
  function drawCompiled() {
    const view = compiled;
    const [ctx, width] = canvasContext(lanes, 150);
    const laneHeight = 118 / Math.max(1, view?.lanes?.length || 0);
    (view?.lanes || []).forEach((lane, i) => {
      const top = i * laneHeight;
      ctx.strokeStyle = lane.selected ? "#dc2626" : "#e2e8f0"; ctx.lineWidth = 2;
      ctx.strokeRect(1, top + 1, width - 3, laneHeight - 2);
      ctx.textAlign = "left"; ctx.fillText(lane.label, 6, top + laneHeight / 2);
      const x = k => 100 + (width - 110) * k / Math.max(1, view.n_points);
      const y = v => top + laneHeight - 7 - (laneHeight - 18) * (v - lane.lo) / (lane.hi - lane.lo || 1);
      line(ctx, lane.steps.map(([k, v]) => [x(k), y(v)]), "#2563eb", 1.5);
      if (lane.measured) line(ctx, lane.measured.map(([k, v]) => [x(k), y(v)]), "#111827");
    });
    ctx.textAlign = "right"; ctx.fillText(view?.repeat_text || "", width - 10, 12);
    (view?.axis || []).forEach((label, i) => {
      ctx.textAlign = i === 4 ? "right" : "center";
      ctx.fillText(label, 100 + (width - 110) * i / 4, 134);
    });
    const [bc, bw] = canvasContext(budget, 20), b = view?.budget;
    bc.fillStyle = "#f1f5f9"; bc.fillRect(0, 0, bw, 20);
    if (b) {
      const scale = bw / Math.max(65535, b.n_points), end = Math.max(b.delay, b.n_points - b.pad);
      for (const [a, z, color] of [[0, b.delay, "#94a3b8"], [b.delay, end, "#60a5fa"], [end, b.n_points, "#fb923c"]]) {
        bc.fillStyle = color; bc.fillRect(a * scale, 0, (z - a) * scale, 20);
      }
    }
  }
  function drawDraft() {
    let [ctx, width] = canvasContext(draftCanvas, 190);
    const p = draft;
    if (!p) return;
    const span = p.hi - p.lo || 1;
    const close = !p.periodic && Math.abs(p.levels[0].value - p.levels[1].value) / span * 110 < 14;
    const labels = close ? [{text: p.combined, value: (p.levels[0].value + p.levels[1].value) / 2}] : p.levels;
    const margin = Math.max(80, ...labels.map(l => ctx.measureText(l.text).width + 10));
    const x0 = margin, x1 = width - 10, y0 = 30, y1 = 140;
    const x = t => x0 + (x1 - x0) * t / (p.duration || 1);
    const y = v => y1 - (y1 - y0) * (v - p.lo) / span;
    let elapsed = 0;
    const spans = p.times.map(t => {
      const start = x(elapsed); elapsed += t.seconds;
      return {start, end: x(elapsed), width: ctx.measureText(t.text).width};
    });
    const places = placeTimeLabels(spans, 0, width);
    const height = Math.max(190, 185 + Math.max(0, ...places.map(v => v.row - 1)) * 18);
    [ctx] = canvasContext(draftCanvas, height);
    ctx.textAlign = "right";
    labels.forEach(l => {
      ctx.fillText(l.text, x0 - 6, y(l.value) + 4);
      ctx.setLineDash([3, 3]); line(ctx, [[x0, y(l.value)], [x1, y(l.value)]]); ctx.setLineDash([]);
    });
    p.times.forEach((t, i) => {
      const s = spans[i], pos = places[i]; ctx.textAlign = "center";
      ctx.fillText(t.text, pos.x, y1 + 19 + pos.row * 18);
      line(ctx, [[s.start, y1 + 6], [s.end, y1 + 6]]);
      for (const tick of [s.start, s.end]) line(ctx, [[tick, y1 + 2], [tick, y1 + 10]]);
    });
    if (p.amplitude) {
      const a = p.amplitude, dx = x1 - 8, base = y(a.base), peak = y(a.peak);
      line(ctx, [[dx, base], [dx, peak]]);
      for (const tick of [base, peak]) line(ctx, [[dx - 4, tick], [dx + 4, tick]]);
      ctx.textAlign = "right"; ctx.fillText(a.text, dx - 6, (base + peak) / 2);
    }
    ctx.textAlign = "right"; ctx.fillText(p.extras, x1, 12);
    line(ctx, p.samples.map((v, i) => [x(p.duration * i / 199), y(v)]), "#2563eb", 2);
    fitZoom(root);
  }
  const lanes = element("canvas", root, null, "chart list-lanes");
  lanes.addEventListener("click", event => {
    const list = compiled?.lanes || [], index = Math.floor((event.clientY - lanes.getBoundingClientRect().top) / (118 / Math.max(1, list.length)));
    if (list[index]) selectChannel(list[index].channel);
  });
  const channelBar = element("div", root, null, "row");
  element("span", channelBar, "channel");
  const choice = element("select", channelBar, null, "field");
  for (let i = 1; i <= 8; i++) { const opt = element("option", choice); opt.value = String(i); opt.textContent = `CH${i}`; }
  choice.addEventListener("change", () => selectChannel(Number(choice.value)));
  input(channelBar, "enabled", "enabled", true, "checkbox");
  input(channelBar, "name", "label", "");
  input(channelBar, "channel_delay_s", "delay", "0");
  input(channelBar, "current_limit_optional", "limit", "");
  const stripBar = element("div", root, null, "row list-strip-bar");
  button(stripBar, null, () => navigate("first"), "\u25c0\u25c0");
  button(stripBar, null, () => navigate(-1), "\u25c0");
  const strip = element("div", stripBar, null, "list-strip");
  button(stripBar, null, () => navigate(1), "\u25b6");
  button(stripBar, null, () => navigate("last"), "\u25b6\u25b6");
  const actions = element("div", root, null, "row");
  button(actions, "move_left", () => moveSegment(-1));
  button(actions, "add", () => editSegment(false));
  button(actions, "update", () => editSegment(true));
  button(actions, "delete", removeSegment);
  button(actions, "move_right", () => moveSegment(1));
  const statistics = element("div", root, null, "list-statistics");
  const budget = element("canvas", root, null, "chart");
  const settingsBar = element("div", root, null, "row");
  input(settingsBar, "sequence_repeat_range", "repeat", "1");
  input(settingsBar, "hold_last", "hold_last", false, "checkbox");
  element("span", settingsBar, "dwell");
  input(settingsBar, "auto", "auto", false, "radio");
  input(settingsBar, "manual", "manual", false, "radio");
  fields.auto.name = fields.manual.name = "list-dwell-mode"; fields.auto.checked = true;
  const dwell = fields.dwell = element("input", settingsBar, null, "field"); dwell.value = "0.00001024";
  element("span", settingsBar).textContent = "s";
  const editor = element("div", root, null, "list-editor");
  const shape = element("select", editor, null, "field");
  shape.addEventListener("change", () => {
    fields.segment_repeat_value = fields.segment_repeat.value;
    parameterForm();
  });
  const parameterBox = element("div", editor, null, "list-parameters");
  const draftBox = element("div", editor, null, "list-draft");
  const draftCanvas = element("canvas", draftBox, null, "chart");
  const draftError = element("div", draftBox, null, "warn");
  const message = element("div", root, null, "warn list-message");
  const bar = element("div", root, null, "row");
  button(bar, "open_json", async () => {
    const response = await App.api.list_open_json();
    if (!result(response, "json_error")) return;
    ++compileRev; ++draftRev; ++switchRev;
    plan = response.plan;
    const s = response.settings;
    fields.repeat.value = s.repeat; fields.hold_last.checked = s.hold_last;
    fields.auto.checked = s.dwell_mode === "auto"; fields.manual.checked = !fields.auto.checked;
    if (s.dwell_mode === "manual") fields.dwell.value = s.dwell;
    channel = s.channel; loadChannel(); preview();
  });
  button(bar, "save_json", async () => result(await App.api.list_save_json(plan, settings()), "save_failed"));
  const run = button(bar, "run_list", async () => {
    const resources = [0, 1].map(i => document.getElementById(`res-${i}`).value);
    result(await App.api.list_run(plan, settings(), resources, document.getElementById("chk-demo").checked, fields.capture.checked), "run_result");
  });
  run.dataset.action = "run"; run.classList.add("btn-accent"); run.disabled = true;
  input(bar, "capture", "capture", false, "checkbox");
  for (const node of [...channelBar.querySelectorAll("input"), ...settingsBar.querySelectorAll("input")]) {
    node.addEventListener("input", () => { if (meta) compile(); });
  }
  new ResizeObserver(() => requestAnimationFrame(() => fitZoom(root))).observe(document.body);
  App.on("busy", runState);
  App.on("measured", kind => { if (kind === "list") compile(); });
  let observedWidth, redrawFrame = null;
  new ResizeObserver(entries => {
    const width = entries[0].contentRect.width;
    if (width === observedWidth) return;
    observedWidth = width;
    if (redrawFrame !== null) return;
    redrawFrame = window.requestAnimationFrame(() => {
      redrawFrame = null;
      drawCompiled(); drawDraft(); fitZoom(root);
      if (compiled) drawSegments(compiled.segments || []);
    });
  }).observe(root);
  App.register({
    async init() {
      meta = await App.api.list_meta();
      for (const [key, name] of meta.shapes) { const opt = element("option", shape); opt.value = key; opt.textContent = name; }
      shape.value = meta.shapes[0][0];
      parameterForm(); loadChannel(); App.relabel(root);
    },
    relabel() { if (meta) { compile(); preview(); } },
  });
})();
