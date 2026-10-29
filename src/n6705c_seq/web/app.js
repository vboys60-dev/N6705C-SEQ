"use strict";

(() => {
  const LIST_VERIFIED = false; // List mode awaits hardware verification; flip to true to enable the tab
  const listeners = new Map();
  const tabs = [];
  const BUSY_STATES = ["power", "draining", "closing", "fault"];
  const panes = new Map([["seq", {}], ["list", {}]]); // analysis.js adds "analysis" when config enables it
  let currentTab = "seq", switching = false;
  const results = [];
  let strings = {};
  let ready = false;
  const emit = (type, ...args) => {
    for (const fn of listeners.get(type) || []) fn(...args);
  };
  const App = window.App = {
    api: null,
    lang: "zh", busy: false, kind: null, config: {},
    state: "idle", gen: 0, stateSeq: -1, powerAllowed: true,
    t(key) { return strings[key]?.[this.lang === "en" ? 1 : 0] ?? key; },
    on(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    register(tab) {
      tabs.push(tab);
      if (ready) Promise.resolve().then(() => tab.init()).catch(showError);
    },
    showResult(kind, title, text) {
      const dialog = document.getElementById("result-dialog");
      if (dialog.open) { results.push([kind, title, text]); return; }
      dialog.dataset.kind = kind;
      document.getElementById("result-title").textContent = title;
      document.getElementById("result-text").textContent = text;
      if (!dialog.open) dialog.showModal();
    },
    addTab(name, hooks) {
      panes.set(name, hooks);
      document.getElementById(`tab-${name}`).addEventListener("click", () => selectTab(name));
    },
    selectTab: name => selectTab(name),
    get currentTab() { return currentTab; },
    relabel(root = document) {
      const elements = [...root.querySelectorAll("[data-i18n]")];
      if (root.matches?.("[data-i18n]")) elements.unshift(root);
      for (const element of elements) {
        element.textContent = this.t(element.dataset.i18n).replace(/\{(\w+)\}/g,
          (match, key) => element.dataset[key] ?? match);
      }
    },
  };
  function showError(error) {
    App.showResult("error", App.t("run_result"), String(error?.message ?? error));
  }
  window.addEventListener("error", event => {
    if (event.message && !event.message.startsWith("ResizeObserver loop")) showError(event.error ?? event.message);
  });
  document.getElementById("result-dialog").addEventListener("close", () => {
    if (results.length) App.showResult(...results.shift());
  });
  window.addEventListener("unhandledrejection", event => {
    event.preventDefault();
    showError(event.reason);
  });
  // A pane's enter() may refuse (returns false): the previous tab stays shown.
  async function selectTab(name) {
    if (switching || !panes.has(name)) return false;
    if (name === "list" && App.kind === "E36441A") return false;
    switching = true;
    try {
      if (name !== currentTab) {
        if (panes.get(name).enter && !(await panes.get(name).enter())) return false;
        await panes.get(currentTab).leave?.();
      }
      currentTab = name;
      for (const tab of panes.keys()) {
        document.getElementById(`${tab}-tab`).hidden = tab !== name;
        document.getElementById(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
      }
      return true;
    } finally {
      switching = false;
    }
  }
  function busyButtons() {
    document.querySelectorAll("#btn-off, [data-action='run']")
      .forEach(button => { button.disabled = !App.powerAllowed; });
    document.getElementById("btn-test").disabled = !App.powerAllowed;
    for (const id of ["res-0", "res-1", "chk-demo"]) document.getElementById(id).disabled = App.state === "analysis";
  }
  async function poll() {
    try {
      for (const [type, ...args] of await App.api.poll()) {
        if (type === "state") {
          const p = args[0];
          if (p.seq <= App.stateSeq) continue; // late event from an interleaved poll: drop, do not emit
          App.stateSeq = p.seq; App.state = p.state; App.gen = p.gen;
          App.busy = BUSY_STATES.includes(p.state);
          App.powerAllowed = p.state === "idle";
          busyButtons();
        } else if (type === "busy") {
          if (App.stateSeq < 0) { App.busy = args[0]; App.powerAllowed = !args[0]; busyButtons(); }
        } else if (type === "kind") {
          App.kind = args[0];
          document.getElementById("tab-list").disabled = !LIST_VERIFIED || App.kind === "E36441A";
          if (App.kind === "E36441A") selectTab("seq");
        } else if (type === "result") {
          App.showResult(...args);
        }
        emit(type, ...args);
      }
    } catch (error) {
      showError(error);
    }
    window.setTimeout(poll, 100);
  }
  document.getElementById("tab-seq").addEventListener("click", () => selectTab("seq"));
  document.getElementById("tab-list").addEventListener("click", () => selectTab("list"));
  document.getElementById("btn-lang").addEventListener("click", async () => {
    if (!App.api) return;
    await App.api.set_lang(App.lang === "zh" ? "en" : "zh");
    App.lang = (await App.api.strings()).lang;
    document.documentElement.lang = App.lang === "en" ? "en" : "zh-Hant";
    document.title = App.t("title");
    App.relabel();
    for (const tab of tabs) tab.relabel();
    emit("lang");
  });
  window.addEventListener("pywebviewready", async () => {
    App.api = window.pywebview.api;
    poll();
    try {
      const translated = await App.api.strings();
      const config = App.config = await App.api.config();
      strings = translated.strings;
      App.lang = translated.lang;
      document.documentElement.lang = App.lang === "en" ? "en" : "zh-Hant";
      document.title = App.t("title");
      for (let i = 0; i < 2; i++) document.getElementById(`res-${i}`).value = config.resources[i] ?? "";
      document.getElementById("chk-demo").checked = config.demo;
      App.relabel();
      ready = true;
      for (const tab of tabs) await tab.init();
      emit("ready");
      await App.api.selftest_report(true, "");
    } catch (error) {
      await App.api.selftest_report(false, String(error?.message ?? error));
      showError(error);
    }
  });
})();
