'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../src/n6705c_seq/analysis/web/viewer.html'), 'utf8');
function slice(from, to, ctx, expose = '') {
  const start = html.indexOf(from), end = html.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `Missing markers: ${from} .. ${to}`);
  return vm.runInContext(html.slice(start, end) + `\n;({${expose}})`, ctx);
}
function setup(hash = '', dark = false) {
  const attrs = {}, calls = [], listeners = {};
  const parent = {};
  const ctx = vm.createContext({URLSearchParams, location: {hash, pathname: '/viewer.html', search: ''},
    history: {replaceState: (...args) => calls.push(args)},
    window: {parent, matchMedia: () => ({matches: dark}), addEventListener: (name, fn) => { listeners[name] = fn; }},
    document: {documentElement: {setAttribute: (key, value) => { attrs[key] = value; }}},
    getComputedStyle: () => ({getPropertyValue: () => ''}),
    renderChips: () => calls.push('chips'), scheduleRedraw: () => calls.push('draw'),
    COMPARE_CH_COLORS: ['#aa00ff'],
  });
  slice('const shellCtx =', 'const state =', ctx);
  vm.runInContext('const state = {theme: initialTheme(), traces: []};', ctx);
  const v = slice('function applyTheme(', "document.getElementById('caseNameInput')", ctx, 'applyTheme, state, CHART_THEMES, initialTheme');
  slice('let reqSeq = 0;', '// 向外殼要路徑授權', ctx);
  const send = (theme, envelope = {}, event = {}) => listeners.message({source: parent,
    origin: 'http://localhost:1234', data: {v: 1, gen: 7, type: 'theme', theme, ...envelope}, ...event});
  return {ctx, attrs, calls, send, ...v};
}
const shellHash = '#t=token&gen=7&parent=http%3A%2F%2Flocalhost%3A1234';
test('viewer theme messages apply both themes through the authenticated shell envelope', () => {
  const t = setup(shellHash);
  for (const name of ['dark', 'light']) {
    t.send(name);
    assert.equal(t.state.theme, name);
    assert.equal(t.attrs['data-theme'], name);
  }
  assert.equal(t.calls.filter(c => c === 'draw').length, 2);
  assert.equal(t.CHART_THEMES.dark.bg, '#000000');
  assert.equal(t.CHART_THEMES.dark.grid, '#3a3a3a');
});
test('viewer rejects invalid themes and unauthenticated messages without redraw', () => {
  const t = setup(shellHash);
  for (const value of ['soft-dark', 'DARK', '', null, undefined, {}, 'constructor', '__proto__']) t.send(value);
  for (const envelope of [{v: 2}, {gen: 8}, {gen: '7'}, {type: 'other'}]) t.send('dark', envelope);
  t.send('dark', {}, {source: {}});
  t.send('dark', {}, {origin: 'http://localhost:9999'});
  assert.equal(t.attrs['data-theme'], undefined);
  assert.ok(!t.calls.includes('draw'));
  t.applyTheme('constructor');
  assert.equal(t.attrs['data-theme'], undefined);
});
test('hash theme is applied at initialization and overrides system preference', () => {
  for (const hash of [shellHash, '']) for (const name of ['light', 'dark']) {
    const t = setup(hash + (hash ? '&' : '#') + 'theme=' + name, name === 'light');
    // Execute the actual init call before trace initialization.
    slice('  applyTheme(state.theme);', '  state.traces = initTraces();', t.ctx);
    assert.equal(t.attrs['data-theme'], name);
    assert.equal(t.calls[0][2], '/viewer.html');
  }
});
test('standalone chooses system preference once; embedded defaults to the root light theme', () => {
  assert.equal(setup('', true).state.theme, 'dark');
  assert.equal(setup('', false).state.theme, 'light');
  assert.equal(setup('#theme=soft-dark', true).state.theme, 'dark');
  assert.equal(setup(shellHash, true).state.theme, 'light');
  assert.equal(setup('').initialTheme(), 'light');
});
test('theme changes use CSS tokens, update defaults and preserve custom and compare colors', () => {
  const t = setup(shellHash);
  vm.runInContext("getComputedStyle = () => ({getPropertyValue: name => ({'--plot-bg':' #010203 ', '--ch1':' #aabbcc '})[name] || ''})", t.ctx);
  t.state.traces = [
    {ch: 1, src: 'p', color: '#old', colorIsDefault: true},
    {ch: 1, src: 'p', color: '#123456', colorIsDefault: false},
    {ch: 1, src: 'c', color: '#aa00ff', colorIsDefault: true},
  ];
  t.send('dark');
  assert.equal(t.CHART_THEMES.dark.bg, '#010203');
  assert.deepEqual(t.state.traces.map(t => t.color), ['#aabbcc', '#123456', '#aa00ff']);
  assert.ok(t.calls.includes('chips'));
});
test('legacy prefs theme cannot override the shell, but fold state is restored and saved', async () => {
  const t = setup(shellHash + '&theme=light');
  let toggle;
  const fold = {id: 'exportFold', open: true, addEventListener: (name, fn) => { toggle = fn; }};
  const saves = [];
  t.ctx.document.querySelectorAll = () => [fold];
  Object.assign(t.ctx, {apiFetch: async () => ({version: 1, theme: 'soft-dark', fold: {exportFold: false}}),
    clearTimeout: () => {}, setTimeout: fn => fn(), postJson: async (...args) => { saves.push(args); }});
  const p = slice('const prefs =', '// Init', t.ctx, 'loadPrefs');
  await p.loadPrefs();
  assert.equal(t.state.theme, 'light');
  assert.equal(fold.open, false);
  fold.open = true; toggle();
  assert.equal(saves[0][0], '/prefs');
  assert.equal(saves[0][1].fold.exportFold, true);
});
test('white print export maps all default dark channels and preserves unrelated colors', () => {
  const t = setup();
  const v = slice('function printColor(', 'function computeViewBuckets(', t.ctx, 'EXPORT_THEMES');
  t.CHART_THEMES.dark.chColors.forEach((color, i) => {
    assert.equal(v.EXPORT_THEMES.light.mapColor(color.toUpperCase()), t.CHART_THEMES.light.chColors[i]);
  });
  assert.equal(v.EXPORT_THEMES.light.mapColor('#123456'), '#123456');
});
test('viewer layout is full width and gives chart the remaining grid space', () => {
  const app = html.match(/\.app\{([^}]+)\}/)[1];
  assert.doesNotMatch(app, /max-width|margin:0 auto/);
  assert.match(app, /display:grid/);
  assert.match(app, /minmax\(0,1fr\)/);
  assert.doesNotMatch(html, /themeSelect|soft-dark|themeDarkOpt|themeSoftDarkOpt|themeLightOpt/);
});
test('complete viewer script parses after theme selector removal', () => {
  const script = html.split('<script>')[1].split('</script>')[0];
  assert.doesNotThrow(() => new vm.Script(script));
});
