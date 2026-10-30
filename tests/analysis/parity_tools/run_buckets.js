// 對拍工具：載入 HTML 內嵌的 Worker 解析器 + 逐行抽出的 computeViewBuckets/
// bucketsFromLodLevel/getVal 系列（原班程式碼，未改寫），對指定索引區間產生分桶結果，
// 供 Python compute_buckets() 逐 float32 比對。
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = process.env.PARITY_SRC || __dirname; // 由 extract_original_js.py 產生的片段目錄
const line = fs.readFileSync(path.join(SRC, 'worker_line.js'), 'utf8');
const workerSrc = eval(line.match(/const DLOG_WORKER_SRC = (".*");\s*$/)[1]);
global.self = { onmessage: null };
global.postMessage = function () {};
const worker = new Function('postMessage', 'self', workerSrc + '\nreturn { parseDlogWorker, buildLod };')(
  global.postMessage, global.self);

const getters = fs.readFileSync(path.join(SRC, 'js_getters.js'), 'utf8');
const buckets = fs.readFileSync(path.join(SRC, 'js_buckets.js'), 'utf8');

const filePath = process.argv[2];
const outPrefix = process.argv[3];
const casesPath = process.argv[4];

const buf = fs.readFileSync(filePath);
const arrBuf = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
const parsed = worker.parseDlogWorker(arrBuf);
const lodObj = worker.buildLod(parsed.V, parsed.I, parsed.nRecords, parsed.activeChannels);

// 依 HTML installLod() 的格式把 lod 裝進 Map（key 前綴 'p:'）
const lodMap = new Map();
['V', 'I', 'P'].forEach(kind => {
  for (let ch = 1; ch <= 4; ch++) {
    if (lodObj[kind + ch]) lodMap.set('p:' + kind + ch, lodObj[kind + ch]);
  }
});

const state = {
  tint: parsed.tint,
  nRecords: parsed.nRecords,
  V: parsed.V,
  I: parsed.I,
  lod: lodMap,
  compare: null,
  offsetMode: false,
  offsetMaxLimit: 0,
  offsetRandCache: null,
  compareOffsetRandCache: null,
  view: { t0: 0, t1: 0 },
};

// 把抽出來的原班函式接上這個 state
const scope = new Function('state', getters + '\n' + buckets +
  '\nreturn { computeViewBuckets, getVal };')(state);

const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const results = [];

cases.forEach((c, ci) => {
  state.offsetMode = !!c.offset;
  state.offsetMaxLimit = c.maxLimit || 0;
  state.offsetRandCache = null;
  // 直接指定索引區間：view 換算回時間，讓 computeViewBuckets 內部算出同樣的 idxLo/idxHi
  state.view = { t0: c.idxLo * state.tint, t1: c.idxHi * state.tint };
  const trace = { ch: c.ch, kind: c.kind, src: 'p', posOffset: 0 };
  const out = scope.computeViewBuckets(trace, c.nBuckets);

  const t = new Float32Array(out.length);
  const mn = new Float32Array(out.length);
  const mx = new Float32Array(out.length);
  out.forEach((b, i) => { t[i] = b.t; mn[i] = b.min; mx[i] = b.max; });
  const tag = outPrefix + '.case' + ci;
  fs.writeFileSync(tag + '.t.f32', Buffer.from(t.buffer));
  fs.writeFileSync(tag + '.min.f32', Buffer.from(mn.buffer));
  fs.writeFileSync(tag + '.max.f32', Buffer.from(mx.buffer));

  // computeViewBuckets 內部實際使用的索引範圍（受 clamp 與 floor/ceil 影響），
  // Python 端要用同一組值，才能只比對分桶邏輯本身、不混入時間換算的浮點差異
  const idxLo = Math.max(0, Math.min(state.nRecords - 1, Math.floor(state.view.t0 / state.tint)));
  const idxHi = Math.max(0, Math.min(state.nRecords - 1, Math.ceil(state.view.t1 / state.tint)));
  results.push({ ...c, idxLoActual: idxLo, idxHiActual: idxHi, nBucketsOut: out.length });
});

fs.writeFileSync(outPrefix + '.cases.json', JSON.stringify({
  tint: parsed.tint, nRecords: parsed.nRecords,
  activeChannels: parsed.activeChannels, cases: results,
}, null, 2));
console.log('JS 產生', cases.length, '組案例，nRecords =', parsed.nRecords);
