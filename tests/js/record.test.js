const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'src/n6705c_seq/web/record.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'src/n6705c_seq/web/record.css'), 'utf8');
const i18n = fs.readFileSync(path.join(root, 'src/n6705c_seq/i18n.py'), 'utf8');
const strings = new Map([...i18n.matchAll(/^    '([^']+)': \('([^']*)', '([^']*)'\),$/gm)]
  .map(([, key, zh, en]) => [key, [zh, en]]));
const idle = () => ({run_id: '', state: 'idle', elapsed_s: 0, records: 0, gaps: 0, preview: false});
const status = (run_id = 'a', state = 'running', extra = {}) => ({...idle(), run_id, state, ...extra});
const snapshot = (run_id = 'a', seq = 1, extra = {}) => ({
  run_id, seq, preview: false, period_s: 0.001,
  keys: {V1: {t0: 0, dt: 0.001, min: [1, -4], max: [3, 7]}}, ...extra,
});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.events = {}; this.attributes = {}; this.value = ''; this.checked = false;
    this.disabled = false; this.hidden = false; this.textContent = ''; this.clientWidth = 800;
    this.draws = [];
  }
  appendChild(node) { this.children.push(node); return node; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { if (!this.disabled && !this.hidden) { await this.events.click?.(); await flush(); } }
  change() { this.events.change?.(); }
  getContext() {
    const draws = this.draws;
    return Object.fromEntries(['scale', 'clearRect', 'fillText', 'beginPath', 'moveTo', 'lineTo', 'stroke']
      .map(name => [name, (...args) => { if (name === 'clearRect') draws.length = 0; draws.push([name, ...args]); }]));
  }
}
function descendants(element) { return [element, ...element.children.flatMap(descendants)]; }
function harness(overrides = {}, metaExtra = {}) {
  const pane = new Element('div'); pane.hidden = true;
  const ids = new Map([['record-pane', pane], ...[0, 1].map(i => [`res-${i}`, Object.assign(new Element('input'), {value: `USB-${i}`} )]),
    ['chk-demo', Object.assign(new Element('input'), {checked: true})]]);
  const listeners = new Map(), intervals = new Map(), windowEvents = new Map(), calls = [], errors = [], translated = new Set();
  let current = idle(), timerId = 0, module, opens = 0;
  const meta = {enabled: true, snapshot_poll_ms: 500, snapshot_poll_min_ms: 250,
    snapshot_max_points: 2000, max_channels: 4, period_floor_table: Object.fromEntries(Array.from({length: 8}, (_, i) => [i + 1, (i + 1) * 102.4e-6])),
    period_max_s: 60, period_granularity_s: 20.48e-6, duration_min_s: 1, duration_max_s: 3599964000, ...metaExtra};
  const api = {
    record_meta: async () => meta,
    record_status: async () => current,
    record_snapshot: async since => { calls.push(since); return snapshot(current.run_id, 0, {keys: {}}); },
    record_start: async () => ({}), record_stop: async () => ({}), ...overrides,
  };
  const App = {api, powerAllowed: true, kind: null, lang: 'en',
    t: key => { translated.add(key); assert.ok(strings.has(key), `Missing translation: ${key}`); return strings.get(key)[App.lang === 'en' ? 1 : 0]; },
    on: (name, fn) => { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); },
    register: value => { module = value; },
    showResult: (...args) => errors.push(args), openRecorded: async () => { opens++; },
    relabel: element => { for (const node of descendants(element)) if (node.dataset.i18n) node.textContent = App.t(node.dataset.i18n)
      .replace(/\{(\w+)\}/g, (m, key) => node.dataset[key] ?? m); },
  };
  const window = {App, devicePixelRatio: 2, addEventListener: (name, fn) => windowEvents.set(name, fn),
    setInterval: (fn, ms) => { const id = ++timerId; intervals.set(id, {fn, ms}); return id; },
    clearInterval: id => intervals.delete(id)};
  vm.runInNewContext(source, {window, document: {getElementById: id => ids.get(id), createElement: tag => new Element(tag)}});
  const emit = (name, value) => { for (const fn of listeners.get(name) ?? []) fn(value); };
  return {App, api, meta, pane, calls, errors, intervals, ids, translated,
    init: () => module.init(), relabel: () => module.relabel(),
    setStatus: value => { current = value; },
    record: value => { current = value; emit('record', value); }, emit,
    tick: async () => { for (const {fn} of [...intervals.values()]) fn(); await flush(); },
    show: async () => { await App.recordPane.show(); await flush(); },
    hide: () => App.recordPane.hide(),
    get opens() { return opens; },
    nodes: key => descendants(pane).filter(node => node.dataset.i18n === key),
    get canvas() { return descendants(pane).find(node => node.tagName === 'canvas'); },
    get inputs() { return descendants(pane).filter(node => node.tagName === 'input'); },
    get statusRow() { return descendants(pane).find(node => node.attributes.role === 'status'); },
  };
}

test('record: fixed defaults and a copied, exact bridge configuration', async () => {
  let received;
  const h = harness({record_start: async (...args) => { received = args; return {cancelled: true}; }});
  await h.show();
  assert.deepEqual(h.inputs.map(n => n.type === 'checkbox' ? n.checked : n.value), [true, false, false, false, true, true, '0.001', '3600']);
  await h.nodes('record_start')[0].click();
  assert.deepEqual(JSON.parse(JSON.stringify(received)), [{machine_index: 0, channels: [1], sense_volt: true, sense_curr: true,
    period_s: 0.001, duration_s: 3600}, ['USB-0', 'USB-1'], true]);
  assert.equal(h.nodes('record_start')[0].disabled, false);
  const machine = descendants(h.pane).find(n => n.tagName === 'select');
  assert.equal(descendants(h.pane).filter(n => n.tagName === 'select').length, 1);
  machine.value = '1'; h.inputs[1].checked = true; h.inputs[4].checked = false;
  await h.nodes('record_start')[0].click();
  assert.deepEqual(JSON.parse(JSON.stringify(received[0])), {machine_index: 1, channels: [1, 2], sense_volt: false,
    sense_curr: true, period_s: 0.001, duration_s: 3600});
});

test('record: old run IDs, equal seq and older seq never replace the accepted envelope', async () => {
  const replies = [snapshot('a', 4), snapshot('old', 100, {preview: true}), snapshot('a', 4, {preview: true}),
    snapshot('a', 3, {preview: true}), snapshot('a', 5, {preview: true})];
  const h = harness({record_snapshot: async () => replies.shift()});
  h.setStatus(status()); await h.show();
  const preview = h.statusRow.children[4];
  assert.equal(preview.hidden, true);
  for (let i = 0; i < 3; i++) { await h.tick(); assert.equal(preview.hidden, true); }
  await h.tick(); assert.equal(preview.hidden, false);
  assert.equal(preview.textContent, 'Preview');
  assert.equal(h.nodes('record_preview_note')[0].hidden, false);
});

test('record: a new run resets seq to -1 and rejects a delayed previous-run response', async () => {
  const responses = [];
  const h = harness({record_snapshot: since => { const d = deferred(); responses.push({...d, since}); return d.promise; }});
  h.setStatus(status()); await h.show();
  await h.tick();
  assert.equal(responses.length, 1); // Skip overlapping interval ticks.
  h.record(status('b'));
  assert.equal(responses[1].since, -1);
  assert.equal(h.canvas.draws.filter(d => d[0] === 'stroke').length, 0);
  responses[1].resolve(snapshot('b', 1)); await flush();
  responses[0].resolve(snapshot('a', 9, {preview: true})); await flush();
  assert.equal(h.statusRow.children[4].hidden, true);
  await h.tick(); assert.equal(responses[2].since, 1);
});

test('record: hiding stops polling and drawing, and discards delayed responses even after redisplay', async () => {
  const first = deferred(); let count = 0;
  const h = harness({record_snapshot: async () => ++count === 1 ? first.promise : snapshot('a', 1)});
  h.setStatus(status()); await h.show();
  assert.equal(h.intervals.size, 1);
  h.hide(); h.hide();
  assert.equal(h.pane.hidden, true); assert.equal(h.intervals.size, 0);
  const drawing = JSON.stringify(h.canvas.draws);
  await h.tick(); h.record(status('a', 'running', {records: 5}));
  assert.equal(count, 1); assert.equal(JSON.stringify(h.canvas.draws), drawing);
  await h.show();
  first.resolve(snapshot('a', 20, {preview: true})); await flush();
  assert.equal(h.statusRow.children[4].hidden, true);
  assert.equal(h.intervals.size, 1);
  await h.show(); assert.equal(count, 2); assert.equal(h.intervals.size, 1);
});

test('record: redisplay reads status before requesting a complete snapshot', async () => {
  const order = []; let state = status();
  const h = harness({record_status: async () => { order.push('status'); return state; },
    record_snapshot: async since => { order.push(since); return snapshot(state.run_id, 5); }});
  await h.show(); await h.tick(); h.hide();
  order.length = 0; state = status('b');
  await h.show(); assert.deepEqual(order, ['status', -1]);
  assert.equal(h.intervals.values().next().value.ms, 500);
});

test('record: hide cancels a show waiting on metadata or status', async () => {
  const meta = deferred(), h = harness({record_meta: () => meta.promise});
  const showing = h.App.recordPane.show(); h.hide();
  meta.resolve(h.meta); await showing;
  assert.equal(h.pane.hidden, true); assert.equal(h.intervals.size, 0);
  const delayed = deferred(), k = harness({record_status: () => delayed.promise});
  const showingStatus = k.App.recordPane.show(); await flush(); k.hide();
  delayed.resolve(status()); await showingStatus;
  assert.equal(k.calls.length, 0); assert.equal(k.intervals.size, 0);
});

test('record: a status event supersedes a delayed status read', async () => {
  const read = deferred(), h = harness({record_status: () => read.promise});
  const showing = h.App.recordPane.show(); await flush();
  h.record(status('new'));
  read.resolve(status('old', 'done')); await showing;
  assert.equal(h.App.recordPane.running(), true);
  assert.equal(h.nodes('record_load')[0].hidden, true);
});

test('record: terminal state stops polling, fetches once, and exposes openRecorded only on done', async () => {
  const h = harness(); h.setStatus(status()); await h.show();
  h.record(status('a', 'stopping')); assert.equal(h.App.recordPane.running(), true);
  h.record(status('a', 'done', {records: 12, gaps: 2, elapsed_s: 3.25, name: 'sample.dlog'})); await flush();
  const count = h.calls.length;
  assert.equal(h.intervals.size, 0); assert.equal(h.App.recordPane.running(), false);
  await h.tick(); assert.equal(h.calls.length, count);
  assert.equal(count, 2);
  assert.equal(h.nodes('record_gap_warning')[0].hidden, false);
  assert.equal(h.statusRow.children[1].textContent, 'Elapsed 3.3 s');
  assert.equal(h.statusRow.children[2].textContent, '12 records');
  assert.equal(h.statusRow.children[3].textContent, '2 gap events');
  await h.nodes('record_load')[0].click(); assert.equal(h.opens, 1);
  h.record(status('a', 'failed', {error: 'failure'}));
  assert.equal(h.nodes('record_load')[0].hidden, true);
});

test('record: buttons follow power ownership and every ACTIVE_STATE; stopping remains locked', async () => {
  const h = harness(); await h.show();
  const start = h.nodes('record_start')[0], stop = h.nodes('record_stop')[0];
  assert.equal(start.disabled, false); assert.equal(stop.disabled, true);
  h.App.powerAllowed = false; h.emit('state');
  assert.equal(start.disabled, true);
  for (const state of ['starting', 'running', 'stopping']) {
    h.record(status('a', state));
    assert.equal(h.App.recordPane.running(), true); assert.equal(start.disabled, true);
    assert.equal(stop.disabled, state === 'stopping');
    assert.ok(h.inputs.every(n => n.disabled));
  }
  h.record(status('a', 'done')); assert.equal(h.nodes('record_load')[0].disabled, true);
  h.App.powerAllowed = true; h.emit('state');
  assert.equal(start.disabled, false); assert.equal(h.nodes('record_load')[0].disabled, false);
  h.App.kind = 'E36441A'; h.emit('kind'); assert.equal(start.disabled, true);
});

test('record: invalid selections and period/duration bounds prevent start without changing inputs', async () => {
  const h = harness(); await h.show();
  const start = h.nodes('record_start')[0], period = h.inputs[6], duration = h.inputs[7];
  h.inputs[0].checked = false; h.inputs[0].change(); assert.equal(start.disabled, true);
  h.inputs[0].checked = true; h.inputs[4].checked = h.inputs[5].checked = false;
  h.inputs[5].change(); assert.equal(start.disabled, true);
  h.inputs[4].checked = h.inputs[5].checked = true;
  for (const value of ['0', '0.0001', '61', 'NaN']) {
    period.value = value; period.change(); assert.equal(start.disabled, true); assert.equal(period.value, value);
  }
  period.value = '0.001'; duration.value = '0.5'; duration.change(); assert.equal(start.disabled, true);
  duration.value = '3600'; duration.change(); assert.equal(start.disabled, false);
});

test('record: pending native start dialog blocks duplicate starts and cancellation restores controls', async () => {
  const d = deferred(); let starts = 0;
  const h = harness({record_start: () => { starts++; return d.promise; }}); await h.show();
  const start = h.nodes('record_start')[0]; await start.click(); await start.click();
  assert.equal(starts, 1); assert.equal(start.disabled, true);
  d.resolve({cancelled: true}); await flush(); assert.equal(start.disabled, false);
});

test('record: stop calls the bridge while powerAllowed is false; bridge errors are shown', async () => {
  let stops = 0;
  const h = harness({record_stop: async () => { stops++; return {error: 'stop rejected', code: 'no_recording'}; }});
  h.setStatus(status()); await h.show(); h.App.powerAllowed = false; h.emit('state');
  await h.nodes('record_stop')[0].click();
  assert.equal(stops, 1); assert.equal(h.errors[0][2], 'stop rejected');
});

test('record: preview uses status or snapshot and never substitutes sample count for persisted records', async () => {
  const h = harness({record_snapshot: async () => snapshot('a', 1, {preview: true})});
  h.setStatus(status('a', 'running', {records: 0})); await h.show();
  assert.equal(h.statusRow.children[4].textContent, 'Preview');
  assert.equal(h.statusRow.children[4].hidden, false);
  assert.equal(h.statusRow.children[2].textContent, '0 records');
  h.record(status('b', 'starting', {preview: true}));
  assert.equal(h.statusRow.children[4].hidden, false);
  h.App.lang = 'zh'; h.relabel();
  assert.equal(h.statusRow.children[4].textContent, strings.get('record_preview')[0]);
});

test('record: hardware DLOG with an empty snapshot hides preview indicators', async () => {
  const h = harness({record_snapshot: async () => snapshot('a', 0, {keys: {}})});
  h.setStatus(status('a', 'running', {preview: false})); await h.show();
  for (const state of ['running', 'stopping', 'done']) {
    h.record(status('a', state, {preview: false, records: state === 'done' ? 1000 : 0}));
    await flush();
    assert.equal(h.statusRow.children[4].hidden, true, state);
    assert.equal(h.nodes('record_preview_note')[0].hidden, true, state);
  }
  assert.equal(h.statusRow.children[2].textContent, '1000 records');
});

test('record: ELOG live snapshot (preview false) draws data and keeps preview indicators hidden', async () => {
  let seq = 0;
  const h = harness({record_snapshot: async () => snapshot('a', ++seq, {
    preview: false, period_s: 0.00100352, keys: {
      V2: {t0: 0, dt: 0.00100352, min: [1, 2], max: [3, 4]},
      I2: {t0: 0, dt: 0.00100352, min: [0.1, 0.2], max: [0.3, 0.4]},
    },
  })});
  h.setStatus(status('a', 'running', {preview: false, records: 498})); await h.show();
  for (const records of [498, 996]) {
    h.record(status('a', 'running', {preview: false, records})); await h.tick();
    assert.equal(h.canvas.draws.filter(d => d[0] === 'stroke').length, 2);
    assert.ok(h.canvas.draws.some(d => d[0] === 'lineTo'));
    assert.equal(h.statusRow.children[4].hidden, true);
    assert.equal(h.nodes('record_preview_note')[0].hidden, true);
    assert.equal(h.statusRow.children[2].textContent, `${records} records`);
  }
});

test('record: envelope draws both extrema, skips nulls, and supports one bucket and constant values', async () => {
  const h = harness({record_snapshot: async () => snapshot('a', 1, {keys: {
    V1: {t0: 0, dt: 1, min: [-8, null], max: [12, null]},
    I1: {t0: 0, dt: 2, min: [2], max: [2]},
  }})});
  h.setStatus(status()); await h.show();
  const lines = h.canvas.draws.filter(d => ['moveTo', 'lineTo'].includes(d[0]));
  assert.equal(lines.length, 12);
  assert.ok(lines.every(([, x, y]) => Number.isFinite(x) && Number.isFinite(y)));
  assert.notEqual(lines[0][2], lines[2][2]);
  assert.equal(h.canvas.width, 1600);
});

test('record: unchanged responses do not change preview or seq and cannot identify a new run', async () => {
  const calls = [];
  const h = harness({record_snapshot: async since => { calls.push(since); return {unchanged: true, seq: 500}; }});
  h.setStatus(status()); await h.show(); await h.tick();
  assert.deepEqual(calls, [-1, -1]);
  h.record(status('b')); await flush();
  assert.deepEqual(calls, [-1, -1, -1]);
  assert.equal(h.App.recordPane.running(), true);
  assert.equal(h.statusRow.children[4].hidden, true);
});

test('record: disabled feature stays hidden, and metadata cannot lower polling below the contract minimum', async () => {
  const h = harness({}, {enabled: false}); await h.init(); await h.show();
  assert.equal(h.pane.hidden, true); assert.equal(h.pane.children.length, 0); assert.equal(h.calls.length, 0);
  const k = harness({}, {snapshot_poll_ms: 10, snapshot_poll_min_ms: 20});
  k.setStatus(status()); await k.show(); assert.equal(k.intervals.values().next().value.ms, 250);
});

test('record: web assets contain no CJK and all translated keys already exist in i18n', async () => {
  assert.doesNotMatch(source + css, /[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]/);
  for (const [, key] of source.matchAll(/"(record_[a-z_]+)"/g)) assert.ok(strings.has(key), key);
  const h = harness(); await h.show();
  for (const state of ['idle', 'starting', 'running', 'stopping', 'done', 'failed']) h.record(status('a', state));
  await flush();
  assert.ok(h.translated.size > 20);
});

test('record: first polling failure stops polling and reports once until redisplay or a new run', async () => {
  let count = 0;
  const h = harness({record_snapshot: async () => {
    count++;
    if (count === 1) return snapshot();
    throw new Error('snapshot unavailable');
  }});
  h.setStatus(status()); await h.show();
  await h.tick();
  assert.equal(h.errors.length, 1);
  assert.equal(h.intervals.size, 0);
  await h.tick(); h.record(status('a', 'running', {records: 10})); await flush();
  await h.tick();
  assert.equal(count, 2);
  assert.equal(h.errors.length, 1);
  h.hide(); await h.show();
  assert.equal(count, 3);
  assert.equal(h.errors.length, 2);
  h.record(status('b')); await flush();
  assert.equal(count, 4);
  assert.equal(h.errors.length, 3);
});

test('record: polling skips in-flight snapshots and old epochs cannot release a new request', async () => {
  const replies = [];
  const h = harness({record_snapshot: () => {
    const d = deferred(); replies.push(d); return d.promise;
  }});
  h.setStatus(status()); await h.show();
  await h.tick(); await h.tick();
  assert.equal(replies.length, 1);
  h.hide(); await h.show();
  assert.equal(replies.length, 2);
  replies[0].resolve(snapshot('a', 10, {preview: true})); await flush();
  await h.tick(); assert.equal(replies.length, 2);
  replies[1].resolve(snapshot('a', 1)); await flush();
  await h.tick(); assert.equal(replies.length, 3);
});

test('record: invalid polling metadata uses a finite interval at least 250ms', async () => {
  for (const extra of [{snapshot_poll_ms: 'bad'}, {snapshot_poll_min_ms: undefined},
    {snapshot_poll_ms: NaN, snapshot_poll_min_ms: NaN},
    {snapshot_poll_ms: '600', snapshot_poll_min_ms: '300'}]) {
    const h = harness({}, extra); h.setStatus(status()); await h.show();
    const ms = h.intervals.values().next().value.ms;
    assert.ok(Number.isFinite(ms) && ms >= 250);
    assert.equal(ms, extra.snapshot_poll_ms === '600' ? 600 : 500);
  }
});

test('record: DLOG preview hint is shown only while running, including a final preview snapshot', async () => {
  const h = harness({record_snapshot: async () => snapshot('a', 1, {preview: true})});
  h.setStatus(status('a', 'running', {preview: true})); await h.show();
  const note = h.nodes('record_preview_note')[0];
  assert.equal(note.hidden, false);
  for (const state of ['stopping', 'done', 'failed', 'starting']) {
    h.record(status('a', state, {preview: true})); await flush();
    assert.equal(note.hidden, true, state);
  }
});

test('record: show catches metadata and status failures and can retry initialization', async () => {
  let tries = 0;
  const h = harness({record_meta: async () => { if (!tries++) throw new Error('metadata unavailable'); return h.meta; }});
  await h.show();
  assert.equal(h.errors.length, 1);
  assert.equal(h.intervals.size, 0);
  await h.show();
  assert.equal(h.pane.hidden, false);
  const k = harness({record_status: async () => { throw new Error('status unavailable'); }});
  await k.show();
  assert.equal(k.errors.length, 1);
  assert.equal(k.intervals.size, 0);
});
