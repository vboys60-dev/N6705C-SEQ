// 今日基準：現行 HTML 在本地用 Float32Array + LOD 算一次重繪所有曲線要多久
const fs=require('fs'), path=require('path');
const workerSrc = eval(fs.readFileSync(path.join(__dirname,'worker_line.js'),'utf8')
  .match(/const DLOG_WORKER_SRC = (".*");\s*$/)[1]);
global.self={onmessage:null}; global.postMessage=function(){};
const w=new Function('postMessage','self',workerSrc+'\nreturn {parseDlogWorker,buildLod};')(global.postMessage,global.self);
const getters=fs.readFileSync(path.join(__dirname,'js_getters.js'),'utf8');
const buckets=fs.readFileSync(path.join(__dirname,'js_buckets.js'),'utf8');

const buf=fs.readFileSync(process.argv[2]);
const p=w.parseDlogWorker(buf.buffer.slice(buf.byteOffset,buf.byteOffset+buf.byteLength));
const lodObj=w.buildLod(p.V,p.I,p.nRecords,p.activeChannels);
const lodMap=new Map();
['V','I','P'].forEach(k=>{for(let c=1;c<=4;c++) if(lodObj[k+c]) lodMap.set('p:'+k+c,lodObj[k+c]);});

const state={tint:p.tint,nRecords:p.nRecords,V:p.V,I:p.I,lod:lodMap,compare:null,
  offsetMode:false,offsetMaxLimit:0,offsetRandCache:null,compareOffsetRandCache:null,
  view:{t0:0,t1:(p.nRecords-1)*p.tint}};
const scope=new Function('state',getters+'\n'+buckets+'\nreturn {computeViewBuckets};')(state);

function bench(label, traces, offset){
  state.offsetMode=offset; state.offsetRandCache=null;
  // 暖機
  traces.forEach(t=>scope.computeViewBuckets(t,1800));
  const N=25, t0=process.hrtime.bigint();
  for(let i=0;i<N;i++){
    state.view={t0:i*20*p.tint, t1:(p.nRecords-1)*p.tint};
    traces.forEach(t=>scope.computeViewBuckets(t,1800));
  }
  const t1=process.hrtime.bigint();
  console.log(`  ${label}: ${(Number(t1-t0)/N/1e6).toFixed(2)} ms/幀`);
}
const mk=(kinds)=>{const o=[];kinds.forEach(k=>p.activeChannels.forEach(c=>o.push({ch:c,kind:k,src:'p',posOffset:0})));return o;};
console.log(`檔案 ${path.basename(process.argv[2])}  nRecords=${p.nRecords}  聲道=${p.activeChannels.join(',')}`);
bench('V 只勾電壓        ', mk(['V']), false);
bench('V+I               ', mk(['V','I']), false);
bench('V+I+P 全開        ', mk(['V','I','P']), false);
bench('V+I+P 全開 + 校正 ', mk(['V','I','P']), true);
