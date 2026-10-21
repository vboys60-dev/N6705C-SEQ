"use strict";

(() => {
  const listeners = new Map();
  const tabs = [];
  let strings = {};
  let ready = false;
  const emit = (type, ...args) => {
    for (const fn of listeners.get(type) || []) fn(...args);
  };
  const App = window.App = {
    api: null,
    lang: "zh", busy: false, kind: null,
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
      dialog.dataset.kind = kind;
      document.getElementById("result-title").textContent = title;
      document.getElementById("result-text").textContent = text;
      if (!dialog.open) dialog.showModal();
    },
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
  window.addEventListener("error", event => { if (event.message) showError(event.error ?? event.message); });
  window.addEventListener("unhandledrejection", event => {
    event.preventDefault();
    showError(event.reason);
  });
  function selectTab(name) {
    if (name === "list" && App.kind === "E36441A") return;
    for (const tab of ["seq", "list"]) {
      document.getElementById(`${tab}-tab`).hidden = tab !== name;
      document.getElementById(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    }
  }
  function busyButtons() {
    document.querySelectorAll("#btn-off, [data-action='run']")
      .forEach(button => { button.disabled = App.busy; });
  }
  async function poll() {
    try {
      for (const [type, ...args] of await App.api.poll()) {
        if (type === "busy") {
          App.busy = args[0];
          busyButtons();
        } else if (type === "kind") {
          App.kind = args[0];
          document.getElementById("tab-list").disabled = App.kind === "E36441A";
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
      const config = await App.api.config();
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
