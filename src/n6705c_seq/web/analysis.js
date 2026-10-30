"use strict";

// Analysis tab shell (contract sections 1.5, 2, 4): owns the Viewer iframe and its postMessage bridge.
(() => {
  const App = window.App;
  const OPEN_PURPOSES = ["dlog", "markers"];
  const SAVE_PURPOSES = ["png", "html", "markers", "csv"];
  const SELFTEST_STEP_TIMEOUT_MS = 10000; // = contracts.SELFTEST_STEP_TIMEOUT_MS
  const REQ_ID = /^[A-Za-z0-9_-]{1,64}$/;
  const frame = document.getElementById("analysis-frame");
  let origin = null, currentGen = null, denyCode = null, readyGen = null;
  let wake = () => {}; // re-checks the pending selftest wait, if any

  // Exact targetOrigin only; never "*".
  const post = message => frame.contentWindow.postMessage({...message, v: 1, gen: currentGen}, origin);

  function unload() {
    frame.src = "about:blank";
    const gen = currentGen;
    currentGen = origin = null;
    return gen;
  }

  async function enter() {
    const r = await App.api.analysis_enter();
    if (!r.ok) {
      denyCode = r.code;
      App.showResult("warning", App.t("analysis_tab"), r.error);
      return false;
    }
    if (r.gen !== currentGen) {
      origin = new URL(r.url).origin;
      currentGen = r.gen;
      frame.src = r.url + "&parent=" + encodeURIComponent(location.origin);
    }
    return true;
  }

  async function leave() {
    const gen = unload();
    if (gen !== null) await App.api.analysis_leave(gen);
  }

  async function reply(d, type, call) {
    const gen = currentGen;
    let r;
    try {
      r = await call(gen);
    } catch (error) {
      r = {ok: false, error: String(error?.message ?? error)};
    }
    if (gen === currentGen) post({...r, ok: r?.ok === true, type, id: d.id});
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

  // Only a same-generation non-analysis event means this Viewer's session ended (contract 4.2).
  App.on("state", p => {
    wake();
    if (currentGen === null || p.gen !== currentGen || p.state === "analysis") return;
    unload();
    App.selectTab("seq");
  });
  App.on("lang", () => { if (currentGen !== null) post({type: "lang", lang: App.lang}); });

  function waitUntil(cond, message) {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => { wake = () => {}; reject(new Error(message)); }, SELFTEST_STEP_TIMEOUT_MS);
      wake = () => { if (cond()) { window.clearTimeout(timer); wake = () => {}; resolve(); } };
      wake();
    });
  }

  // --selftest round trip (contract 1.7): enter, Viewer ready, leave, same-generation idle.
  async function selftest() {
    denyCode = null;
    if (!(await App.selectTab("analysis"))) throw new Error(`analysis_enter failed: ${denyCode}`);
    const gen = currentGen;
    try {
      await waitUntil(() => readyGen === gen, "viewer ready timeout");
    } finally {
      await App.selectTab("seq"); // never leave the gate in analysis
    }
    await waitUntil(() => App.state === "idle" && App.gen === gen, "leave timeout");
  }

  App.register({
    init() {
      if (App.config.analysis_enabled !== true) return;
      document.getElementById("tab-analysis").hidden = false;
      App.addTab("analysis", {enter, leave});
      App.selftest = selftest;
    },
    relabel() {},
  });
})();
