// Loads the real app.js + analysis.js into a minimal fake DOM and drives poll/postMessage by hand.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = path.join(__dirname, '../../src/n6705c_seq/web');
const SHELL = 'http://127.0.0.1:5000';
const VIEWER = 'http://127.0.0.1:6000';
const flush = () => new Promise(resolve => setImmediate(resolve));

class El {
  constructor(id, props = {}) {
    Object.assign(this, { id, dataset: {}, attrs: {}, hidden: false, disabled: false, textContent: '', listeners: {} }, props);
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  matches() { return false; }
  querySelectorAll() { return []; }
  click() { return Promise.all((this.listeners.click || []).map(fn => fn())); }
}

function setup({ enabled = true, api = {} } = {}) {
  const els = {};
  const add = (id, props) => (els[id] = new El(id, props));
  for (const id of ['result-title', 'result-text', 'btn-lang', 'btn-off', 'btn-test', 'res-0', 'res-1', 'chk-demo',
    'seq-tab', 'tab-seq', 'tab-list']) add(id);
  add('list-tab', { hidden: true });
  add('analysis-tab', { hidden: true });
  add('tab-analysis', { hidden: true });
  const results = [];
  add('result-dialog', { open: false, showModal() { this.open = true; results.push([this.dataset.kind, els['result-title'].textContent, els['result-text'].textContent]); } });
  const posts = [];
  const viewerWindow = { postMessage: (msg, target) => posts.push([structuredClone(msg), target]) };
  add('analysis-frame', { src: '', contentWindow: viewerWindow });
  const run = new El('run', { dataset: { action: 'run' } });

  const queue = [];
  const calls = [];
  let gen = 0;
  const bridge = {
    poll: async () => queue.splice(0),
    strings: async () => ({ lang: 'en', strings: {} }),
    config: async () => ({ resources: ['', ''], demo: false, lang: 'en', analysis_enabled: enabled }),
    selftest_report: async (ok, detail) => calls.push(['selftest', ok, detail]),
    set_lang: async () => {},
    analysis_enter: async () => { calls.push(['enter']); gen += 1; return { ok: true, gen, url: `${VIEWER}/viewer.html#t=tok&gen=${gen}&lang=en` }; },
    analysis_leave: async g => { calls.push(['leave', g]); return { ok: true, gen: g }; },
    analysis_open_path: async (g, purpose) => { calls.push(['open', g, purpose]); return { ok: true, gen: g, grant: 'G', name: 'a.dlog' }; },
    analysis_save_path: async (g, purpose, suggested) => { calls.push(['save', g, purpose, suggested]); return { ok: true, gen: g, grant: 'S', name: suggested }; },
    ...api,
  };
  const winListeners = {};
  const timers = [];
  const ctx = {
    console, URL, Promise, setImmediate,
    location: { origin: SHELL },
    document: {
      documentElement: {}, title: '',
      getElementById: id => els[id],
      querySelectorAll: selector => selector.split(',').map(s => s.trim()).flatMap(s =>
        s.startsWith('#') ? [els[s.slice(1)]].filter(Boolean) : s === "[data-action='run']" ? [run] : []),
    },
    addEventListener: (type, fn) => (winListeners[type] ||= []).push(fn),
    setTimeout: fn => timers.push(fn),
    clearTimeout: () => {},
    pywebview: { api: bridge },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const name of ['app.js', 'analysis.js']) vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), ctx, { filename: name });
  const fire = (type, event) => Promise.all((winListeners[type] || []).map(fn => fn(event)));
  return {
    ctx, els, run, results, posts, calls, queue, viewerWindow,
    App: ctx.App,
    async start() { await fire('pywebviewready'); await flush(); },
    async poll(...events) { queue.push(...events); timers.shift()(); await flush(); },
    async message(data, { source = viewerWindow, origin = VIEWER } = {}) { fire('message', { data, source, origin }); await flush(); },
  };
}

const state = (s, gen, seq) => ['state', { state: s, gen, seq }];

test('flag off: analysis tab stays hidden and is not selectable', async () => {
  const t = setup({ enabled: false });
  await t.start();
  assert.equal(t.els['tab-analysis'].hidden, true);
  assert.equal(await t.App.selectTab('analysis'), false);
  assert.deepEqual(t.calls, [['selftest', true, '']]);
});

test('state seq filter and powerAllowed drive the power buttons', async () => {
  const t = setup();
  await t.start();
  await t.poll(state('idle', 0, 0), state('power', 0, 1), ['busy', true]);
  assert.equal(t.App.state, 'power');
  assert.equal(t.App.busy, true);
  assert.equal(t.App.powerAllowed, false);
  assert.ok(t.els['btn-off'].disabled && t.els['btn-test'].disabled && t.run.disabled);
  await t.poll(state('idle', 0, 0)); // late initial event from an interleaved poll is dropped
  assert.equal(t.App.state, 'power');
  await t.poll(state('analysis', 1, 2), ['busy', false]);
  assert.equal(t.App.busy, false);
  assert.equal(t.App.powerAllowed, false);
  assert.ok(t.els['btn-off'].disabled && t.run.disabled && t.els['res-0'].disabled && t.els['chk-demo'].disabled);
  await t.poll(state('idle', 1, 4));
  assert.equal(t.App.powerAllowed, true);
  assert.ok(!t.els['btn-off'].disabled && !t.run.disabled && !t.els['res-0'].disabled);
});

test('enter loads viewer with token fragment and parent; leave blanks then releases', async () => {
  const t = setup();
  await t.start();
  assert.equal(t.els['tab-analysis'].hidden, false);
  await t.els['tab-analysis'].click();
  assert.equal(t.App.currentTab, 'analysis');
  assert.equal(t.els['analysis-tab'].hidden, false);
  assert.equal(t.els['seq-tab'].hidden, true);
  assert.equal(t.els['analysis-frame'].src,
    `${VIEWER}/viewer.html#t=tok&gen=1&lang=en&parent=${encodeURIComponent(SHELL)}`);
  await t.els['tab-seq'].click();
  assert.equal(t.els['analysis-frame'].src, 'about:blank');
  assert.deepEqual(t.calls.slice(1), [['enter'], ['leave', 1]]);
  assert.equal(t.App.currentTab, 'seq');
});

test('denied enter stays on the previous tab and shows the localized error', async () => {
  const t = setup({ api: { analysis_enter: async () => ({ ok: false, code: 'power', error: 'busy text' }) } });
  await t.start();
  await t.els['tab-analysis'].click();
  assert.equal(t.App.currentTab, 'seq');
  assert.equal(t.els['analysis-tab'].hidden, true);
  assert.equal(t.els['analysis-frame'].src, '');
  assert.deepEqual(t.results, [['warning', 'analysis_tab', 'busy text']]);
});

test('bridge: ready gets lang, requests get exact-origin replies, bad messages are dropped', async () => {
  const t = setup();
  await t.start();
  await t.els['tab-analysis'].click();
  await t.message({ v: 1, gen: 1, type: 'ready' });
  assert.deepEqual(t.posts, [[{ type: 'lang', lang: 'en', v: 1, gen: 1 }, VIEWER]]);
  t.posts.length = 0;
  await t.message({ v: 1, gen: 1, type: 'open_request', id: 'r1', purpose: 'dlog' });
  await t.message({ v: 1, gen: 1, type: 'save_request', id: 'r2', purpose: 'png', suggested: 'x.png' });
  assert.deepEqual(t.posts, [
    [{ ok: true, gen: 1, grant: 'G', name: 'a.dlog', type: 'open_result', id: 'r1', v: 1 }, VIEWER],
    [{ ok: true, gen: 1, grant: 'S', name: 'x.png', type: 'save_result', id: 'r2', v: 1 }, VIEWER],
  ]);
  t.posts.length = 0;
  const before = t.calls.length;
  const good = { v: 1, gen: 1, type: 'open_request', id: 'r3', purpose: 'dlog' };
  await t.message(good, { origin: SHELL });
  await t.message(good, { source: {} });
  for (const bad of [{ ...good, v: 2 }, { ...good, gen: 2 }, { ...good, type: 'open_result' }, { ...good, id: 'bad id' },
    { ...good, id: undefined }, { ...good, purpose: 'csv' }, { ...good, type: 'save_request', purpose: 'png' },
    { ...good, type: 'lang', lang: 'en' }, null, 'ready']) await t.message(bad);
  assert.deepEqual(t.posts, []);
  assert.equal(t.calls.length, before);
  assert.ok(t.posts.every(([, target]) => target !== '*'));
});

test('bridge error still replies so the Viewer promise settles; stale reply after leave is dropped', async () => {
  let release;
  const t = setup({ api: {
    analysis_open_path: async () => { throw new Error('boom'); },
    analysis_save_path: () => new Promise(resolve => { release = resolve; }),
  } });
  await t.start();
  await t.els['tab-analysis'].click();
  await t.message({ v: 1, gen: 1, type: 'open_request', id: 'r1', purpose: 'markers' });
  assert.deepEqual(t.posts, [[{ ok: false, error: 'boom', type: 'open_result', id: 'r1', v: 1, gen: 1 }, VIEWER]]);
  t.posts.length = 0;
  await t.message({ v: 1, gen: 1, type: 'save_request', id: 'r2', purpose: 'html', suggested: 'r.html' });
  await t.els['tab-seq'].click();
  release({ ok: true, gen: 1, grant: 'late', name: 'r.html' });
  await flush();
  assert.deepEqual(t.posts, []);
});

test('only a same-generation non-analysis state clears the viewer', async () => {
  const t = setup();
  await t.start();
  await t.poll(state('idle', 0, 0));
  await t.els['tab-analysis'].click(); // gen 1
  await t.els['tab-seq'].click();
  await t.els['tab-analysis'].click(); // gen 2
  const src = t.els['analysis-frame'].src;
  // Late events of gen 1 (analysis, draining, idle) arrive after the gen 2 enter reply.
  await t.poll(state('analysis', 1, 1), state('draining', 1, 2), state('idle', 1, 3), state('analysis', 2, 4));
  assert.equal(t.els['analysis-frame'].src, src);
  assert.equal(t.App.currentTab, 'analysis');
  await t.poll(state('closing', 2, 5));
  assert.equal(t.els['analysis-frame'].src, 'about:blank');
  assert.equal(t.App.currentTab, 'seq');
  assert.deepEqual(t.calls.filter(c => c[0] === 'leave'), [['leave', 1]]); // no leave call for an already-ended session
});

test('lang change is forwarded to the loaded viewer only', async () => {
  const t = setup();
  await t.start();
  await t.els['btn-lang'].click();
  assert.deepEqual(t.posts, []);
  await t.els['tab-analysis'].click();
  await t.els['btn-lang'].click();
  assert.deepEqual(t.posts, [[{ type: 'lang', lang: 'en', v: 1, gen: 1 }, VIEWER]]);
});
