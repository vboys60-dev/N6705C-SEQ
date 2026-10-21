"use strict";

(() => {
  const App = window.App;
  const panel = document.getElementById("seq-tab");
  const forms = [];
  let rev = 0, timer = null, view = null, canvas, message, hold, capture;
  const el = (tag, parent, key, className) => {
    const node = document.createElement(tag);
    if (key) node.dataset.i18n = key;
    if (className) node.className = className;
    parent.appendChild(node);
    return node;
  };
  const resources = () => [0, 1].map(i => document.getElementById(`res-${i}`).value);
  const demo = () => document.getElementById("chk-demo").checked;
  const rows = () => forms.map(form => Object.fromEntries(Object.entries(form.fields)
    .map(([key, field]) => [key, key === "enabled" ? field.checked : field.value])));
  function result(response, title) {
    if (response.error) App.showResult("error", response.title ?? App.t(title), response.error);
    if (response.info) App.showResult("info", App.t(title), response.info);
    return response;
  }
  function preview() {
    const current = ++rev;
    window.clearTimeout(timer);
    timer = window.setTimeout(async () => {
      const response = await App.api.seq_preview(current, rows(), hold.checked);
      if (response.rev < rev) return;
      view = response;
      message.textContent = view.message;
      draw();
    }, 30);
  }
  function draw() {
    if (!canvas || !view) return;
    const width = Math.max(canvas.clientWidth, 400), height = Math.max(canvas.clientHeight, 200);
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const c = canvas.getContext("2d");
    c.scale(scale, scale);
    c.clearRect(0, 0, width, height);
    if (!view.lanes.length) return;
    const left = 72, right = 12, top = 12, bottom = 40;
    const x = ms => left + (width - left - right) * ms / view.t_end;
    const laneHeight = (height - top - bottom) / view.lanes.length;
    function line(points, color, weight = 1, dash = []) {
      if (points.length < 2) return;
      c.beginPath();
      c.strokeStyle = color;
      c.lineWidth = weight;
      c.setLineDash(dash);
      points.forEach(([px, py], i) => i ? c.lineTo(px, py) : c.moveTo(px, py));
      c.stroke();
      c.setLineDash([]);
    }
    function text(value, px, py, color, align = "center", bold = false) {
      c.font = `${bold ? "bold " : ""}12px ${getComputedStyle(panel).fontFamily}`;
      c.fillStyle = color;
      c.textAlign = align;
      c.textBaseline = "middle";
      c.fillText(value, px, py);
    }
    function arrow(start, end, py, color) {
      line([[start, py], [end, py]], color, 1, [4, 3]);
      line([[start + 4, py - 3], [start, py], [start + 4, py + 3]], color);
      line([[end - 4, py - 3], [end, py], [end - 4, py + 3]], color);
    }
    for (const tick of view.ticks) {
      line([[x(tick), top], [x(tick), height - bottom]], "#e5e7eb");
      text(String(tick), x(tick), height - bottom + 12, "#475569");
    }
    text(view.t_end.toFixed(1), x(view.t_end), height - bottom + 12, "#475569", "right");
    text(view.time_axis, (left + width) / 2, height - 10, "#475569");
    view.lanes.forEach((lane, i) => {
      const color = forms[lane.channel - 1].color.value;
      const y0 = top + i * laneHeight, yHi = y0 + 20, yLo = y0 + laneHeight - 8;
      const y = v => yLo - (yLo - yHi) * (v - lane.low) / (lane.high - lane.low);
      const mid = (yHi + yLo) / 2;
      line([[left, y0], [width - right, y0]], "#d1d5db");
      line(lane.points.map(([ms, v]) => [x(ms), y(v)]), color, 2.5);
      if (lane.measured !== null) {
        line(lane.measured.map(([ms, v]) => [x(ms), y(v)]), "#111827");
        text(view.measured_label, left - 6, y0 + laneHeight / 2 + 14, "#111827", "right");
      }
      text(lane.name, left - 6, y0 + laneHeight / 2, color, "right", true);
      if (lane.delay > 0) {
        arrow(x(0), x(lane.delay), mid, color);
        if (laneHeight >= 44) text(lane.delay_text, x(0) + 2, mid - 8, color, "left");
      }
      if (lane.hold_text !== null) {
        arrow(x(lane.top0), x(lane.top1), mid, color);
        if (laneHeight >= 44) text(lane.hold_text, x((lane.top0 + lane.top1) / 2), mid + 9, color);
      }
      text(lane.levels_text, x(lane.top1) + 4, yHi - 8, color, "left");
    });
  }
  function checkbox(parent, key) {
    const label = el("label", parent);
    const input = el("input", label);
    input.type = "checkbox";
    el("span", label, key);
    return input;
  }
  function setKind(kind) {
    if (!capture) return;
    capture.disabled = kind === "E36441A";
    if (capture.disabled) capture.checked = false;
  }
  App.on("kind", setKind);
  App.on("status", (i, text) => {
    const status = document.getElementById(`conn-${i}`);
    if (status) status.textContent = text;
  });
  App.on("busy", busy => { document.getElementById("btn-test").disabled = busy; });
  App.on("measured", tab => { if (tab === "seq" && canvas) preview(); });
  App.register({
    async init() {
      const meta = await App.api.seq_meta();
      const wrap = el("div", panel, null, "seq-table-wrap");
      const table = el("table", wrap, null, "seq-table");
      const header = el("tr", el("thead", table));
      for (const key of ["frame_channel", "enabled", "color", ...meta.columns.map(c => c[1])]) el("th", header, key);
      const body = el("tbody", table);
      meta.rows.forEach((raw, i) => {
        const row = el("tr", body);
        el("td", row).textContent = `F${Math.floor(i / meta.channels_per_frame) + 1}:CH${i % meta.channels_per_frame + 1}`;
        const fields = {};
        fields.enabled = el("input", el("td", row));
        fields.enabled.type = "checkbox";
        fields.enabled.checked = raw.enabled;
        const color = el("input", el("td", row), null, "seq-color");
        color.type = "color";
        color.value = meta.palette[i % meta.palette.length];
        color.addEventListener("input", draw);
        for (const [key] of meta.columns) {
          const field = fields[key] = el("input", el("td", row), null, "field");
          field.type = "text";
          field.value = raw[key];
          field.spellcheck = false;
          if (key !== "name") field.inputMode = "decimal";
        }
        for (const field of Object.values(fields)) field.addEventListener("input", preview);
        forms.push({fields, color});
      });
      const bar = el("div", panel, null, "row seq-actions");
      const open = el("button", bar, "open_csv", "btn");
      const save = el("button", bar, "save_csv", "btn");
      hold = checkbox(bar, "hold_dc");
      capture = checkbox(bar, "capture");
      const run = el("button", bar, "run_sequence", "btn btn-accent seq-run");
      run.dataset.action = "run";
      run.disabled = App.busy;
      hold.addEventListener("change", preview);
      open.addEventListener("click", async () => {
        const response = result(await App.api.seq_open_csv(rows()), "csv_error");
        if (!response.rows) return;
        response.rows.forEach((raw, i) => {
          for (const [key, value] of Object.entries(raw)) {
            forms[i].fields[key][key === "enabled" ? "checked" : "value"] = value;
          }
        });
        preview();
      });
      save.addEventListener("click", async () => result(await App.api.seq_save_csv(rows()), "save_failed"));
      run.addEventListener("click", async () => result(await App.api.seq_run(
        rows(), resources(), demo(), hold.checked, capture.checked), "run_result"));
      document.getElementById("btn-test").addEventListener("click", async () => {
        if (App.busy) return;
        for (let i = 0; i < meta.frames; i++) document.getElementById(`conn-${i}`).textContent = "";
        result(await App.api.test_connection(resources(), demo()), "connection_failed");
      });
      document.getElementById("btn-off").addEventListener("click", async () =>
        result(await App.api.all_off(resources(), demo()), "all_off"));
      message = el("p", panel, null, "warn seq-message");
      canvas = el("canvas", panel, null, "chart seq-chart");
      App.relabel(panel);
      this.relabel();
      setKind(App.kind);
      new ResizeObserver(draw).observe(canvas);
      preview();
    },
    relabel() {
      forms.forEach((form, i) => {
        form.color.setAttribute("aria-label", `${App.t("color")} CH${i + 1}`);
        for (const [key, field] of Object.entries(form.fields)) {
          field.setAttribute("aria-label", `${App.t(key === "enabled" ? "enabled" : `column_${key}`)} CH${i + 1}`);
        }
      });
      if (canvas) preview();
    },
  });
})();
