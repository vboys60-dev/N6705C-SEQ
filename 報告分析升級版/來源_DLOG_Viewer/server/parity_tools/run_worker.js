// POC對拍：直接載入 HTML 內嵌的 DLOG_WORKER_SRC（原班 Worker 解析程式碼），
// 呼叫 parseDlogWorker() + buildLod()，把結果 dump 成 raw binary 供 Python 端比對。
'use strict';
const fs = require('fs');
const path = require('path');

const line = fs.readFileSync(path.join(__dirname, 'worker_line.js'), 'utf8');
const m = line.match(/const DLOG_WORKER_SRC = (".*");\s*$/);
if (!m) throw new Error('抓不到 DLOG_WORKER_SRC 字串字面量');
const workerSrc = eval(m[1]); // 字串字面量本身，eval 出實際 JS 原始碼文字

// worker 原始碼裡有 self.onmessage = function(e){...} 用到 self/postMessage，
// 在 Node 裡跑之前先給空殼，我們只呼叫內部函式，不觸發 onmessage。
global.self = { onmessage: null };
global.postMessage = function(){};

const sandbox = {};
// 用 Function 包一層，把 workerSrc 當函式本體執行，執行後把要用的函式掛到 sandbox
const wrapped = new Function('postMessage', 'self', workerSrc + '\nreturn { parseDlogWorker, buildLod };');
Object.assign(sandbox, wrapped(global.postMessage, global.self));

const filePath = process.argv[2];
const outPrefix = process.argv[3];

const buf = fs.readFileSync(filePath);
const arrBuf = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);

const parsed = sandbox.parseDlogWorker(arrBuf);
const lod = sandbox.buildLod(parsed.V, parsed.I, parsed.nRecords, parsed.activeChannels);

console.log(JSON.stringify({
  tint: parsed.tint,
  declaredTime: parsed.declaredTime,
  nRecords: parsed.nRecords,
  channelModels: parsed.channelModels,
  activeChannels: parsed.activeChannels,
  fields: parsed.fields,
  minmax: parsed.minmax,
}, null, 2));

function dump(name, f32arr) {
  fs.writeFileSync(outPrefix + '.' + name + '.f32', Buffer.from(f32arr.buffer, f32arr.byteOffset, f32arr.byteLength));
}

for (let ch = 1; ch <= 4; ch++) {
  if (parsed.activeChannels.indexOf(ch) === -1) continue;
  dump('V' + ch, parsed.V[ch - 1]);
  dump('I' + ch, parsed.I[ch - 1]);
  const l = lod['V' + ch];
  dump('V' + ch + '_l0min', l.level0.min); dump('V' + ch + '_l0max', l.level0.max);
  dump('V' + ch + '_l1min', l.level1.min); dump('V' + ch + '_l1max', l.level1.max);
  const li = lod['I' + ch];
  dump('I' + ch + '_l0min', li.level0.min); dump('I' + ch + '_l0max', li.level0.max);
  dump('I' + ch + '_l1min', li.level1.min); dump('I' + ch + '_l1max', li.level1.max);
  const lp = lod['P' + ch];
  dump('P' + ch + '_l0min', lp.level0.min); dump('P' + ch + '_l0max', lp.level0.max);
  dump('P' + ch + '_l1min', lp.level1.min); dump('P' + ch + '_l1max', lp.level1.max);
}
