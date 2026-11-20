"use strict";

(() => {
  const App = window.App;
  const channelFallbacks = ["#f0e442", "#56b4e9", "#e58fc4", "#3dd6a3",
    "#e69f00", "#b79cff", "#f4845f", "#a6d854"];
  const channelColor = number => App.cssVar(`--ch${number}`, channelFallbacks[number - 1]);
  const ACTIVE_STATES = new Set(["starting", "running", "stopping"]);
  const STATE_KEYS = {
    idle: "record_state_idle", starting: "record_state_starting",
    running: "record_state_running", stopping: "record_state_stopping",
    done: "record_state_done", failed: "record_state_failed",
  };
  const text = (key, values = {}) => App.t(key).replace(/\{(\w+)\}/g, (m, k) => values[k] ?? m);
  let panel, controls, meta, initialization, visible = false, timer = null;
  let epoch = 0, statusVersion = 0, seq = -1, snapshot = null, pending = false, loading = false;
  let snapshotRequest = null, pollingFailed = false;
  let status = {run_id: "", state: "idle", elapsed_s: 0, records: 0, gaps: 0, preview: false};
  const running = () => ACTIVE_STATES.has(status.state);
  const el = (tag, parent, key, className) => {
    const node = document.createElement(tag);
    if (key) node.dataset.i18n = key;
    if (className) node.className = className;
    parent.appendChild(node);
    return node;
  };
  function errorResult(error) {
    App.showResult("error", App.t("record_title"), error);
  }
  function stopPolling() {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
    epoch++;
  }
  async function fetchSnapshot(since = seq) {
    if (!visible || !controls || pollingFailed || snapshotRequest?.epoch === epoch) return;
    const requestEpoch = epoch, run = status.run_id;
    const request = snapshotRequest = {epoch: requestEpoch};
    try {
      const result = await App.api.record_snapshot(since);
      if (!visible || requestEpoch !== epoch || run !== status.run_id || result.unchanged === true) return;
      if (result.run_id !== run || !Number.isInteger(result.seq) || result.seq <= seq) return;
      seq = result.seq;
      snapshot = result;
      render();
    } catch (error) {
      if (visible && requestEpoch === epoch && run === status.run_id) {
        pollingFailed = true;
        stopPolling();
        errorResult(text("record_error_fetch", {error: String(error?.message ?? error)}));
      }
    } finally {
      if (snapshotRequest === request) snapshotRequest = null;
    }
  }
  function startPolling() {
    if (!visible || !meta?.enabled || !running() || timer !== null || pollingFailed) return;
    const delay = Math.max(250, Number(meta.snapshot_poll_min_ms) || 500, Number(meta.snapshot_poll_ms) || 500);
    timer = window.setInterval(() => { void fetchSnapshot(); }, delay);
  }
  function updateStatus(next, refresh = false) {
    if (!next || !Object.hasOwn(STATE_KEYS, next.state) || typeof next.run_id !== "string") return;
    const newRun = next.run_id !== status.run_id;
    const terminal = running() && !ACTIVE_STATES.has(next.state);
    statusVersion++;
    status = {...next};
    if (newRun) {
      stopPolling();
      pollingFailed = false;
      seq = -1;
      snapshot = null;
    } else if (terminal) stopPolling();
    render();
    if (!visible) return;
    if (newRun || terminal || refresh) void fetchSnapshot(-1);
    startPolling();
  }
  async function readStatus() {
    const requestEpoch = epoch, version = statusVersion;
    const next = await App.api.record_status();
    if (visible && requestEpoch === epoch && version === statusVersion) updateStatus(next, true);
  }
  function parseHms(v) { // HH:MM:SS(.f) -> seconds; NaN if malformed
    const m = /^\s*(\d+):([0-5]?\d):([0-5]?\d(?:\.\d+)?)\s*$/.exec(v);
    return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) : NaN;
  }
  function configuration() {
    return {
      machine_index: Number(controls.machine.value),
      channels: controls.channels.flatMap((field, i) => field.checked ? [i + 1] : []),
      sense_volt: controls.volt.checked, sense_curr: controls.curr.checked,
      period_s: Number(controls.period.value), duration_s: parseHms(controls.duration.value),
    };
  }
  async function command(start) {
    if (pending || (start ? controls.start.disabled : controls.stop.disabled)) return;
    pending = true;
    render();
    try {
      const result = start
        ? await App.api.record_start(configuration(), [0, 1].map(i => document.getElementById(`res-${i}`).value),
          document.getElementById("chk-demo").checked)
        : await App.api.record_stop();
      if (result.error) errorResult(result.error);
      else if (!result.cancelled && visible) await readStatus();
    } catch (error) {
      errorResult(text("record_error_fetch", {error: String(error?.message ?? error)}));
    } finally {
      pending = false;
      render();
    }
  }
  function build() {
    controls = {channels: [], fields: []};
    el("h2", panel, "record_title");
    const form = el("div", panel, null, "record-form");
    function field(key, tag = "input") {
      const label = el("label", form, null, "record-field");
      el("span", label, key);
      const input = el(tag, label, null, "field");
      controls.fields.push(input);
      return input;
    }
    controls.machine = field("record_machine", "select");
    for (let i = 0; i < 2; i++) {
      const option = el("option", controls.machine, "record_machine_option");
      option.dataset.number = String(i + 1);
      option.value = String(i);
    }
    controls.machine.value = "0";
    const channels = el("fieldset", form, null, "record-choices");
    el("legend", channels, "record_channels");
    function checkbox(parent, key, checked) {
      const label = el("label", parent);
      const input = el("input", label);
      input.type = "checkbox";
      input.checked = checked;
      controls.fields.push(input);
      const span = el("span", label, key);
      return {input, span};
    }
    for (let i = 1; i <= 4; i++) {
      const item = checkbox(channels, "record_channel", i === 1);
      item.span.dataset.number = String(i);
      controls.channels.push(item.input);
    }
    const quantities = el("div", form, null, "record-choices");
    controls.volt = checkbox(quantities, "record_voltage", true).input;
    controls.curr = checkbox(quantities, "record_current", true).input;
    controls.period = field("record_period");
    controls.period.type = "number";
    controls.period.step = "any"; // Keep the requested value; backend validates the hardware grid.
    controls.period.value = "0.001";
    controls.period.max = String(meta.period_max_s);
    controls.duration = field("record_duration");
    controls.duration.type = "text";
    controls.duration.placeholder = "HH:MM:SS";
    controls.duration.value = "01:00:00";
    controls.fields.forEach(input => input.addEventListener("change", render));
    const actions = el("div", panel, null, "row record-actions");
    controls.start = el("button", actions, "record_start", "btn");
    controls.stop = el("button", actions, "record_stop", "btn btn-danger");
    controls.load = el("button", actions, "record_load", "btn");
    controls.start.addEventListener("click", () => { void command(true); });
    controls.stop.addEventListener("click", () => { void command(false); });
    controls.load.addEventListener("click", async () => {
      if (controls.load.disabled) return;
      loading = true;
      render();
      try { await App.openRecorded(); }
      catch (error) { errorResult(String(error?.message ?? error)); }
      finally { loading = false; render(); }
    });
    const row = el("div", panel, null, "record-status");
    row.setAttribute("role", "status");
    row.setAttribute("aria-live", "polite");
    for (const name of ["state", "elapsed", "records", "gaps", "preview"]) controls[name] = el("span", row);
    controls.preview.className = "record-preview";
    controls.file = el("p", panel);
    controls.warning = el("p", panel, "record_gap_warning", "warn");
    controls.previewNote = el("p", panel, "record_preview_note", "muted");
    controls.error = el("p", panel, null, "warn");
    controls.canvas = el("canvas", panel, null, "record-chart");
    controls.canvas.setAttribute("role", "img");
    relabel();
  }
  function relabel() {
    if (!controls) return;
    App.relabel(panel);
    controls.canvas.setAttribute("aria-label", App.t("record_title"));
    render();
  }
  function render() {
    if (!controls) return;
    const active = running(), cfg = configuration();
    const parameters = cfg.channels.length * (Number(cfg.sense_volt) + Number(cfg.sense_curr));
    const floor = meta.period_floor_table[String(parameters)];
    controls.period.min = String(floor ?? meta.period_floor_table["1"]);
    const valid = parameters > 0 && Number.isFinite(cfg.period_s) && cfg.period_s >= floor
      && cfg.period_s <= meta.period_max_s && Number.isFinite(cfg.duration_s)
      && cfg.duration_s >= meta.duration_min_s && cfg.duration_s <= meta.duration_max_s;
    controls.fields.forEach(field => { field.disabled = active || pending || !App.powerAllowed || !meta.enabled; });
    controls.start.disabled = !meta.enabled || !App.powerAllowed || active || pending || !valid || App.kind === "E36441A";
    // Power is owned by this recording; stop must remain available while powerAllowed is false.
    controls.stop.disabled = !active || status.state === "stopping" || pending;
    controls.load.hidden = status.state !== "done";
    controls.load.disabled = status.state !== "done" || !App.powerAllowed || pending || loading;
    controls.state.textContent = App.t(STATE_KEYS[status.state]);
    controls.elapsed.textContent = text("record_elapsed", {seconds: Number(status.elapsed_s).toFixed(1)});
    controls.records.textContent = text("record_records", {count: status.records});
    controls.gaps.textContent = text("record_gaps", {count: status.gaps});
    controls.warning.hidden = !(status.gaps > 0);
    const preview = snapshot?.preview === true || status.preview === true;
    controls.preview.hidden = !preview;
    controls.previewNote.hidden = !preview || status.state !== "running";
    controls.preview.textContent = App.t("record_preview");
    controls.file.hidden = !status.name;
    controls.file.textContent = status.name ? text("record_file", {name: status.name}) : "";
    controls.error.hidden = !status.error;
    controls.error.textContent = status.error ?? "";
    if (visible) draw();
  }
  function draw() {
    const canvas = controls.canvas, ctx = canvas.getContext("2d");
    if (!ctx) return;
    const entries = Object.entries(snapshot?.keys ?? {});
    const width = Math.max(canvas.clientWidth, 320), height = Math.max(200, entries.length * 90 + 40);
    const scale = window.devicePixelRatio || 1;
    canvas.style.height = `${height}px`;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    ctx.scale(scale, scale);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = App.cssVar("--plot-bg", "#ffffff");
    ctx.fillRect(0, 0, width, height);
    ctx.font = "12px sans-serif";
    ctx.fillStyle = App.cssVar("--plot-axis", "#5c5c5c");
    ctx.textAlign = "center";
    ctx.fillText(App.t("record_time_axis"), width / 2, height - 8);
    let end = 0;
    for (const [, s] of entries) end = Math.max(end, s.t0 + s.dt * s.min.length);
    const left = Math.min(200, width * 0.4), right = width - 12;
    const x = t => left + (right - left) * t / (end || 1);
    entries.forEach(([key, s], lane) => {
      const values = [...s.min, ...s.max].filter(v => typeof v === "number" && Number.isFinite(v));
      if (!values.length) return;
      const low = Math.min(...values), high = Math.max(...values), span = high - low || Math.max(Math.abs(high) * 0.1, 1);
      const top = lane * 90 + 12, y = v => top + 62 - (v - low) * 54 / span;
      ctx.fillStyle = App.cssVar("--plot-text", "#333333");
      ctx.textAlign = "left";
      ctx.fillText(text("record_channel", {number: key.slice(1)}), 4, top + 12);
      ctx.fillText(App.t(key.startsWith("V") ? "record_voltage" : "record_current"), 4, top + 28);
      ctx.fillText(`${low.toPrecision(4)} / ${high.toPrecision(4)}`, 4, top + 44);
      ctx.strokeStyle = channelColor(Number(key.slice(1)));
      ctx.setLineDash(key.startsWith("V") ? [] : [6, 4]);
      ctx.beginPath();
      for (let i = 0; i < s.min.length; i++) {
        const lo = s.min[i], hi = s.max[i];
        if (typeof lo !== "number" || typeof hi !== "number" || !Number.isFinite(lo) || !Number.isFinite(hi)) continue;
        const x0 = x(s.t0 + i * s.dt), x1 = x(s.t0 + (i + 1) * s.dt);
        // Each bucket spans its full time width, retaining both extrema (including a single bucket).
        ctx.moveTo(x0, y(lo)); ctx.lineTo(x1, y(lo));
        ctx.moveTo(x0, y(hi)); ctx.lineTo(x1, y(hi));
        ctx.moveTo((x0 + x1) / 2, y(lo)); ctx.lineTo((x0 + x1) / 2, y(hi));
      }
      ctx.stroke();
      ctx.setLineDash([]);
    });
    ctx.fillStyle = App.cssVar("--plot-axis", "#5c5c5c");
    ctx.textAlign = "left";
    ctx.fillText("0", left, height - 24);
    ctx.textAlign = "right";
    ctx.fillText(end.toPrecision(4), right, height - 24);
  }
  function initialize() {
    if (!initialization) initialization = (async () => {
      meta = await App.api.record_meta();
      panel = document.getElementById("record-pane");
      if (!panel || !meta.enabled) { if (panel) panel.hidden = true; return; }
      build();
    })().catch(error => { initialization = null; throw error; });
    return initialization;
  }
  App.recordPane = {
    async show() {
      const requestEpoch = epoch;
      try {
        await initialize();
        if (!controls || visible || requestEpoch !== epoch) return;
        visible = true;
        panel.hidden = false;
        stopPolling();
        pollingFailed = false;
        seq = -1; // A redisplayed pane always requests the complete snapshot.
        render();
        await readStatus();
      } catch (error) {
        if (visible || requestEpoch === epoch) errorResult(text("record_error_fetch", {error: String(error?.message ?? error)}));
      }
    },
    hide() {
      visible = false;
      stopPolling();
      if (panel) panel.hidden = true;
    },
    running,
  };
  App.on("record", updateStatus);
  for (const event of ["state", "busy", "kind", "theme"]) App.on(event, render);
  window.addEventListener("resize", () => { if (visible && controls) draw(); });
  App.register({init: initialize, relabel});
})();
