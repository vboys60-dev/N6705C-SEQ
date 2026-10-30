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

function setup({ enabled = true, selftest = false, api = {} } = {}) {
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
    config: async () => ({ resources: ['', ''], demo: false, lang: 'en', analysis_enabled: enabled, selftest_mode: selftest }),
    selftest_report: async (ok, detail) => calls.push(['selftest', ok, detail]),
    set_lang: async () => {},
    analysis_enter: async () => { calls.push(['enter']); gen += 1; return { ok: true, gen, url: `${VIEWER}/viewer.html#t=tok&gen=${gen}&lang=en` }; },
    analysis_leave: async g => { calls.push(['leave', g]); return { ok: true, gen: g }; },
    analysis_open_path: async (g, purpose) => { calls.push(['open', g, purpose]); return { ok: true, gen: g, grant: 'G', name: 'a.dlog' }; },
    analysis_save_path: async (g, purpose, suggested) => { calls.push(['save', g, purpose, suggested]); return { ok: true, gen: g, grant: 'S', name: suggested }; },
    ...api,
  };
  // Every bridge method the shell touches, to check the selftest deny list (contract 1.7.3).
  const touched = new Set();
  const proxy = new Proxy(bridge, { get(target, key) { touched.add(key); return target[key]; } });
  const winListeners = {};
  const timers = [];
  let timerId = 0;
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
    setTimeout: (fn, ms) => { timers.push({ fn, ms, id: ++timerId }); return timerId; },
    clearTimeout: id => { const i = timers.findIndex(x => x.id === id); if (i >= 0) timers.splice(i, 1); },
    pywebview: { api: proxy },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const name of ['app.js', 'analysis.js']) vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), ctx, { filename: name });
  const take = async ms => { const i = timers.findIndex(x => x.ms === ms); timers.splice(i, 1)[0].fn(); await flush(); };
  const fire = (type, event) => Promise.all((winListeners[type] || []).map(fn => fn(event)));
  return {
    ctx, els, run, results, posts, calls, queue, viewerWindow, touched,
    fire, timers,
    App: ctx.App,
    async start() { await fire('pywebviewready'); await flush(); },
    async poll(...events) { queue.push(...events); await take(100); },
    async timeout() { await take(10000); },
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

test('save_request purpose csv is forwarded to analysis_save_path and answered', async () => {
  const t = setup();
  await t.start();
  await t.els['tab-analysis'].click();
  await t.message({ v: 1, gen: 1, type: 'save_request', id: 'r9', purpose: 'csv', suggested: 'out.csv' });
  assert.deepEqual(t.calls.at(-1), ['save', 1, 'csv', 'out.csv']);
  assert.deepEqual(t.posts, [[{ ok: true, gen: 1, grant: 'S', name: 'out.csv', type: 'save_result', id: 'r9', v: 1 }, VIEWER]]);
});

// Selftest deny list, read from contracts.SELFTEST_JS_API_DENIED so it cannot drift (contract 1.7.3).
const DENIED = [...fs.readFileSync(path.join(WEB, '../analysis/contracts.py'), 'utf8')
  .match(/^SELFTEST_JS_API_DENIED = \(([^)]*)\)/m)[1].matchAll(/"(\w+)"/g)].map(m => m[1]);
assert.ok(DENIED.includes('seq_run') && DENIED.includes('analysis_open_path'));
const assertAllowed = t => {
  const used = [...t.touched].filter(k => typeof k === 'string');
  assert.deepEqual(used.filter(k => DENIED.includes(k)), []);
};
// Starts without awaiting: the pywebviewready handler blocks inside App.selftest.
const startSelftest = async t => { const done = t.fire('pywebviewready'); await flush(); return { done }; };

test('selftest: enter, ready, leave, same-gen idle, then report ok; no denied bridge calls', async () => {
  const t = setup({ selftest: true });
  const { done } = await startSelftest(t);
  await t.poll(state('idle', 0, 0));
  assert.deepEqual(t.calls, [['enter']]);
  await t.message({ v: 1, gen: 1, type: 'ready' });
  assert.deepEqual(t.calls, [['enter'], ['leave', 1]]);
  await t.poll(state('analysis', 1, 1), state('draining', 1, 2), state('idle', 0, 1)); // stale seq dropped
  assert.equal(t.calls.length, 2);
  await t.poll(state('idle', 1, 3));
  await done;
  assert.deepEqual(t.calls, [['enter'], ['leave', 1], ['selftest', true, '']]);
  assert.equal(t.App.currentTab, 'seq');
  assertAllowed(t);
});

test('selftest: no ready -> timeout report, and leave was still called', async () => {
  const t = setup({ selftest: true });
  const { done } = await startSelftest(t);
  await t.timeout();
  await done;
  assert.deepEqual(t.calls.slice(0, 2), [['enter'], ['leave', 1]]);
  const [, ok, detail] = t.calls.at(-1);
  assert.equal(ok, false);
  assert.match(detail, /ready/);
  assert.equal(t.App.currentTab, 'seq');
  assertAllowed(t);
});

test('selftest: leave never reaches idle -> leave timeout report', async () => {
  const t = setup({ selftest: true });
  const { done } = await startSelftest(t);
  await t.message({ v: 1, gen: 1, type: 'ready' });
  await t.timeout();
  await done;
  assert.deepEqual(t.calls.at(-1), ['selftest', false, 'leave timeout']);
});

test('selftest: denied enter -> report failure with the code', async () => {
  const t = setup({ selftest: true, api: { analysis_enter: async () => ({ ok: false, code: 'power', error: 'busy' }) } });
  await (await startSelftest(t)).done;
  const [, ok, detail] = t.calls.at(-1);
  assert.equal(ok, false);
  assert.match(detail, /power/);
  assertAllowed(t);
});

test('selftest_mode false or flag off: unchanged, only selftest_report(true)', async () => {
  for (const opts of [{ selftest: false }, { selftest: true, enabled: false }]) {
    const t = setup(opts);
    await t.start();
    assert.deepEqual(t.calls, [['selftest', true, '']]);
  }
});

// Viewer logic: run the real code between two markers of viewer.html's inline script in a stub context.
const VIEWER_HTML = fs.readFileSync(path.join(__dirname, '../../src/n6705c_seq/analysis/web/viewer.html'), 'utf8');
function viewerSlice(from, to, ctx, expose) {
  const start = VIEWER_HTML.indexOf(from);
  const end = VIEWER_HTML.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `markers not found: ${from} .. ${to}`);
  vm.createContext(ctx);
  return vm.runInContext(VIEWER_HTML.slice(start, end) + `\n;({ ${expose} })`, ctx);
}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

test('viewer: a job slot is held until the response body is read', async () => {
  const bodies = [];
  const ctx = {
    API: '', shellCtx: { token: 't', gen: 1 },
    fetch: async () => { const d = deferred(); bodies.push(d); return { ok: true, arrayBuffer: () => d.promise }; },
  };
  const v = viewerSlice('const JOB_SLOTS', '// ---- postMessage', ctx, 'apiFetch, active: () => jobActive');
  const calls = [1, 2, 3, 4].map(() => v.apiFetch('/samples'));
  await flush();
  assert.equal(bodies.length, 3);   // headers arrived, bodies not read: all 3 slots still taken
  assert.equal(v.active(), 3);
  bodies[0].resolve('B0');
  assert.equal(await calls[0], 'B0');
  await flush();
  assert.equal(bodies.length, 4);   // only now does the 4th request start
  bodies.slice(1).forEach(d => d.resolve('x'));
  await Promise.all(calls);
  assert.equal(v.active(), 0);
});

test('viewer: a second open/export while one runs is ignored', async () => {
  const v = viewerSlice('let activeReq = null;', 'function cancelActive(){', {}, 'longOp');
  const gate = deferred();
  let runs = 0;
  const op = v.longOp(async () => { runs++; await gate.promise; });
  const first = op();
  await op();
  assert.equal(runs, 1);
  gate.resolve();
  await first;
  await op();
  assert.equal(runs, 2);
});

test('viewer: a response from before a cache invalidation is dropped and keeps the newer pending entry', async () => {
  const replies = [];
  const state = { offsetMode: false, offsetMaxLimit: 0, traces: [] };
  const ctx = {
    state, console,
    apiFetch: () => { const d = deferred(); replies.push(d); return d.promise; },
    splitBinary: x => x, f32View: () => new Float32Array(1),
    useBuckets: () => false, nKeysOfHandle: () => 2, handleOf: () => 'h',
    scheduleRedraw: () => {}, lastStatsSig: null,
  };
  const v = viewerSlice('function qs(params){', 'function showParseProgress(', ctx,
    'ensureRawWindow, getPoint, rawWindows, rawPending, pointCache, pointPending');
  const reply = (i, lo, hi) => replies[i].resolve({ meta: { lo, hi, layout: [] }, payload: null, base: 0 });
  v.ensureRawWindow('h', 100, 0, 9);                                  // A, settings '0'
  v.getPoint('h', 50);                                                 // point A, settings '0'
  state.offsetMode = true; state.offsetMaxLimit = 1;
  v.ensureRawWindow('h', 100, 0, 9);                                   // invalidates, B
  state.offsetMode = false;
  v.ensureRawWindow('h', 100, 0, 9);                                   // invalidates again, C: same settings as A
  v.getPoint('h', 50);
  assert.equal(replies.length, 5);
  reply(0, 0, 19);
  reply(1, 50, 50);
  await flush();
  assert.equal(v.rawWindows.size, 0);                                  // A not cached
  assert.equal(v.pointCache.size, 0);
  assert.ok(v.rawPending.has('h') && v.pointPending.has('h:50'));      // C's entries survive
  reply(3, 0, 19);
  reply(4, 50, 50);
  await flush();
  assert.equal(v.rawWindows.get('h').hi, 19);
  assert.ok(!v.rawPending.has('h'));
});

test('viewer: export preparation drops values fetched before an invalidation and fetches again', async () => {
  const replies = [];
  const state = { offsetMode: false, offsetMaxLimit: 0, traces: [], markers: [], handle: 'h', nRecords: 10, tint: 1, marker1: 2, marker2: null };
  const ctx = {
    state, console,
    apiFetch: () => { const d = deferred(); replies.push(d); return d.promise; },
    splitBinary: x => x, f32View: (buf, base, offset) => Float32Array.of(offset),
    useBuckets: () => false, nKeysOfHandle: () => 2, handleOf: () => 'h',
    scheduleRedraw: () => {}, lastStatsSig: null, computeViewBuckets: () => {}, N_BUCKETS: 1, refreshStats: async () => true,
  };
  const v = viewerSlice('function qs(params){', 'function showParseProgress(', ctx, 'ensureExportData, getPoint, pointCache');
  const reply = (i, value) => replies[i].resolve({ meta: { lo: 2, hi: 2, layout: [{ key: 'V1', offset: value, length: 1 }] } });
  const done = v.ensureExportData();
  await flush();
  state.offsetMode = true;
  v.getPoint('h', 7);                         // lazily invalidates the caches mid-export
  reply(0, 111);                              // old-settings value for index 2
  await flush(); await flush();
  assert.equal(replies.length, 3);            // preparation restarted and asked for index 2 again
  assert.ok(!v.pointCache.has('h:2'));
  reply(2, 222);
  reply(1, 0);
  await done;
  assert.equal(v.pointCache.get('h:2').V[1], 222);
});

test('viewer: a settings change during the stats refresh re-prepares, and endless changes abort the export', async () => {
  const state = { offsetMode: false, offsetMaxLimit: 0, traces: [], markers: [], handle: 'h', nRecords: 10, tint: 1, marker1: 2, marker2: null };
  let fetches = 0, flips = 0;
  const flashes = [];
  const ctx = {
    state, console,
    apiFetch: async () => { fetches++; return { meta: { lo: 2, hi: 2, layout: [] } }; },
    splitBinary: x => x, f32View: () => new Float32Array(1),
    useBuckets: () => false, nKeysOfHandle: () => 2, handleOf: () => 'h',
    scheduleRedraw: () => {}, lastStatsSig: null, computeViewBuckets: () => {}, N_BUCKETS: 1,
    refreshStats: async () => { if (flips > 0) { flips--; state.offsetMode = !state.offsetMode; } return true; },
    flashMessage: msg => flashes.push(msg), T: key => key,
  };
  const v = viewerSlice('function qs(params){', 'function showParseProgress(', ctx, 'ensureExportData');
  flips = 1;
  assert.equal(await v.ensureExportData(), true);
  assert.equal(fetches, 2);                  // the point was fetched again under the new settings
  flips = 100;
  assert.equal(await v.ensureExportData(), false);
  assert.deepEqual(flashes, ['msgExportDataChanged']);
});

test('viewer: export retries when a background stats refresh supersedes its own (real refreshStats)', async () => {
  const statsReplies = [];
  const state = { loaded: true, offsetMode: false, offsetMaxLimit: 0, markers: [], handle: 'h', nRecords: 10, tint: 1,
    marker1: 2, marker2: 4, view: { t0: 0, t1: 9 }, traces: [{ key: 'p:V1', visible: true, kind: 'V', ch: 1 }] };
  const ctx = {
    state, console,
    apiFetch: path => {
      if (!path.startsWith('/stats')) return Promise.resolve({ meta: { lo: 2, hi: 2, layout: [] } });
      const d = deferred(); statsReplies.push(d); return d.promise;
    },
    splitBinary: x => x, f32View: () => new Float32Array(1),
    useBuckets: () => false, nKeysOfHandle: () => 2, handleOf: () => 'h',
    scheduleRedraw: () => {}, lastStatsSig: null, computeViewBuckets: () => {}, N_BUCKETS: 1,
    tintOf: () => 1, nRecOfTrace: () => 10, clampIdxTrace: (t, i) => i,
    flashMessage: () => {}, T: key => key,
  };
  const v = viewerSlice('function qs(params){', 'function showParseProgress(', ctx, 'ensureExportData');
  const s = vm.runInContext(VIEWER_HTML.slice(VIEWER_HTML.indexOf('const statsCache = new Map();'),
    VIEWER_HTML.indexOf('function currentViewIdxRange(){')) + '\n;({ refreshStats, statsCache })', ctx);
  const stat = n => ({ V1: { min: n, max: n, avg: n } });
  let result = null;
  const done = v.ensureExportData().then(r => { result = r; });
  await flush();
  assert.equal(statsReplies.length, 1);       // export's stats request (A)
  const background = s.refreshStats();         // updateStatsPanel-style refresh (B) supersedes A
  statsReplies[0].resolve(stat(1));
  await flush(); await flush();
  assert.equal(result, null);                  // A was dropped: export must not finish yet
  assert.equal(statsReplies.length, 3);        // export retried with its own request (C)
  statsReplies[1].resolve(stat(2));
  statsReplies[2].resolve(stat(3));
  await done; await background;
  assert.equal(result, true);
  assert.equal(s.statsCache.get('p:V1').avg, 3);
});

test('viewer: a failing /stats request makes export retry and then abort (real refreshStats)', async () => {
  let statsCalls = 0;
  const flashes = [];
  const state = { loaded: true, offsetMode: false, offsetMaxLimit: 0, markers: [], handle: 'h', nRecords: 10, tint: 1,
    marker1: 2, marker2: 4, view: { t0: 0, t1: 9 }, traces: [{ key: 'p:V1', visible: true, kind: 'V', ch: 1 }] };
  const ctx = {
    state, console: { ...console, error: () => {} },
    apiFetch: async path => {
      if (!path.startsWith('/stats')) return { meta: { lo: 2, hi: 2, layout: [] } };
      statsCalls++;
      throw Object.assign(new Error('HTTP 503'), { status: 503 });
    },
    splitBinary: x => x, f32View: () => new Float32Array(1),
    useBuckets: () => false, nKeysOfHandle: () => 2, handleOf: () => 'h',
    scheduleRedraw: () => {}, lastStatsSig: null, computeViewBuckets: () => {}, N_BUCKETS: 1,
    tintOf: () => 1, nRecOfTrace: () => 10, clampIdxTrace: (t, i) => i,
    flashMessage: msg => flashes.push(msg), T: key => key,
  };
  const v = viewerSlice('function qs(params){', 'function showParseProgress(', ctx, 'ensureExportData');
  vm.runInContext(VIEWER_HTML.slice(VIEWER_HTML.indexOf('const statsCache = new Map();'),
    VIEWER_HTML.indexOf('function currentViewIdxRange(){')), ctx);
  assert.equal(await v.ensureExportData(), false);
  assert.equal(statsCalls, 5);
  assert.deepEqual(flashes, ['msgExportDataChanged']);
});
