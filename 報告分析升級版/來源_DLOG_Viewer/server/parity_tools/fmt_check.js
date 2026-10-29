// 用原班 Worker 解析器取得 float32 值，輸出 JS toFixed(8)/toFixed(9) 結果供 Python 比對
const fs=require('fs'), path=require('path');
const workerSrc = eval(fs.readFileSync(path.join(__dirname,'worker_line.js'),'utf8')
  .match(/const DLOG_WORKER_SRC = (".*");\s*$/)[1]);
global.self={onmessage:null}; global.postMessage=function(){};
const w=new Function('postMessage','self',workerSrc+'\nreturn {parseDlogWorker};')(global.postMessage,global.self);
const buf=fs.readFileSync(process.argv[2]);
const p=w.parseDlogWorker(buf.buffer.slice(buf.byteOffset,buf.byteOffset+buf.byteLength));
const ch=3, N=20000;
const out=[];
for(let k=0;k<N;k++){
  const v=p.V[ch-1][k], i=p.I[ch-1][k];
  out.push([(k*p.tint).toFixed(8), v.toFixed(9), i.toFixed(9), (v*i).toFixed(9)].join(','));
}
fs.writeFileSync(path.join(__dirname,'js_fmt.txt'), out.join('\n'));
console.log('JS 產生', N, '列格式化結果');
