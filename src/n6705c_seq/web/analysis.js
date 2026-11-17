"use strict";

// Owns the Viewer session, recording mode, and the exact-origin postMessage bridge.
(() => {
  const App = window.App;
  const OPEN_PURPOSES = ["dlog", "markers"];
  const SAVE_PURPOSES = ["png", "html", "markers", "csv"];
  const ACTIVE_STATES = ["starting", "running", "stopping"];
  const SELFTEST_STEP_TIMEOUT_MS = 10000; // = contracts.SELFTEST_STEP_TIMEOUT_MS
  const REQ_ID = /^[A-Za-z0-9_-]{1,64}$/;
  const frame = document.getElementById("analysis-frame");
  const pane = document.getElementById("record-pane");
  const viewButton = document.getElementById("analysis-mode-view");
  const recordButton = document.getElementById("analysis-mode-record");
  let origin = null, currentGen = null, denyCode = null, readyGen = null;
  let recordEnabled = false, mode = "view", changing = false, recordState = "idle";
  let intent = 0, frameEpoch = 0, recordVersion = 0;
  const waiters = new Set();
  const wake = () => { for (const check of [...waiters]) check(); };
  const recording = () => ACTIVE_STATES.includes(recordState) || App.recordPane?.running() === true;
  const warning = error => App.showResult("warning", App.t("record_title"), error);

  // Exact targetOrigin only; never "*".
  const post = message => frame.contentWindow.postMessage({...message, v: 1, gen: currentGen}, origin);
  const session = () => ({gen: currentGen, origin, target: frame.contentWindow, src: frame.src, epoch: frameEpoch});
  const sameSession = s => currentGen !== null && s.gen === currentGen && s.origin === origin
    && s.target === frame.contentWindow && s.src === frame.src && s.epoch === frameEpoch;

  function unload() {
    frame.src = "about:blank";
    const gen = currentGen;
    currentGen = origin = readyGen = null;
    frameEpoch++;
    wake();
    return gen;
  }

  function buttons() {
    if (!recordEnabled) return;
    viewButton.disabled = changing || mode === "view" || recording();
    recordButton.disabled = changing || mode === "record";
    viewButton.setAttribute("aria-pressed", String(mode === "view"));
    recordButton.setAttribute("aria-pressed", String(mode === "record"));
  }

  async function displayMode() {
    const record = recordEnabled && mode === "record";
    frame.hidden = record;
    document.getElementById("analysis-note").hidden = record;
    pane.hidden = !record;
    buttons();
    if (record) await App.recordPane?.show();
    else if (recordEnabled) await App.recordPane?.hide();
  }

  async function enterViewer(request = intent) {
    const r = await App.api.analysis_enter();
    if (request !== intent) {
      // An enter reply can arrive after the user left during a mode change.
      if (r.ok && r.gen !== currentGen) await App.api.analysis_leave(r.gen);
      return false;
    }
    if (!r.ok) {
      denyCode = r.code;
      App.showResult("warning", App.t("analysis_tab"), r.error);
      return false;
    }
    if (r.gen !== currentGen) {
      origin = new URL(r.url).origin;
      currentGen = r.gen;
      readyGen = null;
      frameEpoch++;
      frame.src = r.url + "&parent=" + encodeURIComponent(location.origin);
    }
    return true;
  }

  async function enter() {
    if (recordEnabled) {
      const version = recordVersion;
      let status;
      try { status = await App.api.record_status(); }
      catch { status = {state: "idle"}; }
      if (version === recordVersion) recordState = status.state; // RecordStatus has no running field.
      if (ACTIVE_STATES.includes(recordState)) mode = "record";
      if (mode === "record") {
        await displayMode();
        return true;
      }
    }
    if (!(await enterViewer())) return false;
    if (recordEnabled) await displayMode();
    return true;
  }

  async function leave() {
    intent++;
    wake();
    if (recordEnabled) await App.recordPane?.hide();
    if (mode === "record") return; // Recording owns power; there is no analysis session to leave.
    const gen = unload();
    if (gen !== null) await App.api.analysis_leave(gen);
  }

  function waitUntil(cond, message, timeout = SELFTEST_STEP_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      let timer;
      const done = error => {
        if (timer !== undefined) window.clearTimeout(timer);
        waiters.delete(check);
        if (error) reject(error); else resolve();
      };
      const check = () => { if (cond()) done(); };
      waiters.add(check);
      if (timeout !== null) timer = window.setTimeout(() => done(new Error(message)), timeout);
      check();
    });
  }

  async function idle(request, timeout = null) {
    // A run may last hours. Gate/status events, navigation, or fault end this wait.
    await waitUntil(() => request !== intent || (App.state === "idle" && !recording())
      || App.state === "fault" || App.state === "closing", App.t("analysis_denied_draining"), timeout);
    if (request !== intent) return false;
    if (App.state === "fault" || App.state === "closing") {
      warning(App.state === "fault" ? App.t("record_error_fault") : App.t("record_error_closing"));
      return false;
    }
    return true;
  }

  async function setMode(next, request) {
    if (!recordEnabled || changing) return false;
    if (next === mode) return true;
    if (next === "view" && recording()) return false;
    changing = true;
    buttons();
    try {
      if (next === "record") {
        mode = "record"; // Suppress the draining event's Viewer-tab exit.
        const gen = currentGen;
        if (gen !== null) {
          const reply = await App.api.analysis_leave(gen);
          if (!reply.ok) throw new Error(reply.error || App.t("analysis_denied_draining"));
        }
        unload();
        if (!(await idle(request, SELFTEST_STEP_TIMEOUT_MS)) || request !== intent) {
          if (request === intent) { mode = "view"; await displayMode(); }
          return false;
        }
      } else {
        await App.recordPane?.hide();
        if (request !== intent) return false;
        if (!(await enterViewer(request))) {
          if (request === intent) await displayMode();
          return false;
        }
        mode = "view";
      }
      if (request !== intent) return false;
      await displayMode();
      return true;
    } catch (error) {
      if (request === intent && next === "record") {
        mode = "view";
        if (App.currentTab === "analysis") await displayMode();
      }
      throw error;
    } finally {
      changing = false;
      buttons();
    }
  }

  async function chooseMode(next) {
    if (changing || App.currentTab !== "analysis" || next === mode || (next === "view" && recording())) return;
    const request = ++intent;
    wake();
    try { await setMode(next, request); }
    catch (error) { warning(String(error?.message ?? error)); }
  }

  App.openRecorded = async () => {
    if (!recordEnabled) return;
    if (changing) { warning(App.t("analysis_denied_draining")); return; }
    const request = ++intent;
    wake();
    try {
      if (!(await idle(request))) return;
      if (App.currentTab !== "analysis" && !(await App.selectTab("analysis"))) {
        warning(App.t(App.state === "power" ? "analysis_denied_power" : "analysis_denied_draining"));
        return;
      }
      if (request !== intent || !(await setMode("view", request))) return;
      const s = session();
      if (s.gen === null) return;
      const valid = () => request === intent && App.currentTab === "analysis" && mode === "view" && sameSession(s);
      await waitUntil(() => !valid() || readyGen === s.gen, App.t("record_error_stale"));
      if (!valid()) return;
      const r = await App.api.record_load(s.gen);
      if (!valid()) return;
      if (!r.ok) { warning(r.error); return; }
      if (r.gen !== s.gen || typeof r.grant !== "string" || !r.grant.trim()
          || typeof r.name !== "string" || !r.name.trim() || /[/\\]/.test(r.name)) {
        warning(App.t("record_error_stale"));
        return;
      }
      s.target.postMessage({v: 1, type: "open_push", gen: s.gen, grant: r.grant, name: r.name}, s.origin);
    } catch (error) {
      if (request === intent) warning(String(error?.message ?? error));
    }
  };

  async function reply(d, type, call) {
    const s = session();
    let r;
    try {
      r = await call(s.gen);
    } catch (error) {
      r = {ok: false, error: String(error?.message ?? error)};
    }
    if (sameSession(s)) post({...r, ok: r?.ok === true, type, id: d.id});
  }

  window.addEventListener("message", event => {
    const d = event.data;
    if (currentGen === null || event.source !== frame.contentWindow || event.origin !== origin
        || !d || typeof d !== "object" || d.v !== 1 || d.gen !== currentGen) return;
    if (d.type === "ready") {
      readyGen = currentGen;
      wake();
      return post({type: "lang", lang: App.lang});
    }
    if (typeof d.id !== "string" || !REQ_ID.test(d.id)) return;
    if (d.type === "open_request" && OPEN_PURPOSES.includes(d.purpose)) {
      return reply(d, "open_result", gen => App.api.analysis_open_path(gen, d.purpose));
    }
    if (d.type === "save_request" && SAVE_PURPOSES.includes(d.purpose) && typeof d.suggested === "string") {
      return reply(d, "save_result", gen => App.api.analysis_save_path(gen, d.purpose, d.suggested));
    }
  });

  // Only a same-generation non-analysis event ends this Viewer's session.
  App.on("state", p => {
    if (mode === "view" && currentGen !== null && p.gen === currentGen && p.state !== "analysis") {
      unload();
      App.selectTab("seq");
    }
    wake();
  });
  App.on("record", status => {
    recordVersion++;
    recordState = status.state;
    // J's listener must first apply the status used by recordPane.running().
    Promise.resolve().then(() => { buttons(); wake(); });
  });
  App.on("lang", () => { if (currentGen !== null) post({type: "lang", lang: App.lang}); });

  // --selftest round trip: enter, Viewer ready, leave, same-generation idle.
  async function selftest() {
    denyCode = null;
    if (!(await App.selectTab("analysis"))) throw new Error(`analysis_enter failed: ${denyCode}`);
    const gen = currentGen;
    try {
      await waitUntil(() => readyGen === gen, "viewer ready timeout");
    } finally {
      await App.selectTab("seq");
    }
    await waitUntil(() => App.state === "idle" && App.gen === gen, "leave timeout");
  }

  App.register({
    async init() {
      if (App.config.analysis_enabled !== true) return;
      recordEnabled = (await App.api.record_meta()).enabled === true;
      document.getElementById("analysis-modes").hidden = !recordEnabled;
      if (recordEnabled) {
        viewButton.addEventListener("click", () => chooseMode("view"));
        recordButton.addEventListener("click", () => chooseMode("record"));
        buttons();
      }
      document.getElementById("tab-analysis").hidden = false;
      App.addTab("analysis", {enter, leave});
      App.selftest = selftest;
    },
    relabel() {},
  });
})();
