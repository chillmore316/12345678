/* Saved-source UI behavior, no browser profile, network, feed, or real input file. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('live_paper_compare/static/source_check.html','utf8');
assert.match(html,/<html lang="zh-CN">/);
assert.doesNotMatch(html,/<script[^>]+src=|(?:src|href)=["']https?:/i);
const scripts=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];assert.equal(scripts.length,1);
const made=[];
class Element{
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.attrs={};this.listeners={};this._text='';this.className='';this.href='';this.classList={add:n=>{this.className+=' '+n;},remove:n=>{this.className=this.className.split(/\s+/).filter(x=>x!==n).join(' ');},contains:n=>this.className.split(/\s+/).includes(n)};}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  set innerHTML(_){throw new Error('unsafe insertion');}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this._text='';this.children=nodes;}
  setAttribute(k,v){this.attrs[k]=String(v);}
  getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null;}
  removeAttribute(k){delete this.attrs[k];if(k==='href')this.href='';}
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}
  async click(){for(const fn of this.listeners.click||[])await fn({});}
}
const ids={};for(const m of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)){const name=m[2].match(/\bid="([^"]+)"/)?.[1];if(name){ids[name]=new Element(m[1]);ids[name].className=m[2].match(/class="([^"]*)"/)?.[1]||'';}}
const document={getElementById:name=>{assert.ok(ids[name],name);return ids[name];},createElement:tag=>{made.push(tag);return new Element(tag);}};

const a='a'.repeat(32),b='b'.repeat(32),outside='c'.repeat(32);
let olderPrevious=false;
const summary=id=>({check_id:id,provider:'promax',symbol:'510300.SH',observed_at:olderPrevious&&id===b?'2026-11-01T15:00:00+08:00':'2026-11-01T16:00:00+08:00',status:'SAVED_SAMPLE',mode:'RESEARCH_ONLY',paper_admission:false});
const detail=id=>({...summary(id),schema_version:1,daily_start:'2026-10-26',daily_end:'2026-11-01',network_attempts:2,retry_count:0,raw_payload_saved:false,source_certification:false,timestamp_semantics_verified:false,coverage_complete:false,runtime_changed:false,checks:[{kind:'daily',outcome:'sample_received',http_status:200,rows:5,latest_time:'2026-10-30T00:00:00+08:00',age_seconds:null,temporal_state:'daily_history_sample'},{kind:'minute',outcome:'sample_received',http_status:200,rows:1,latest_time:'2026-10-30T15:00:00+08:00',age_seconds:176400,temporal_state:'stale_sample'}]});
let listValue={records:[summary(a),summary(b)],truncated:false},badList=false,badDetail=false,held=false,resolveHeld;
const requests=[];
async function fetch(url,options){requests.push(url);assert.equal(options.method,undefined);assert.ok(url.startsWith('/api/research/source-checks'));if(url==='/api/research/source-checks')return {ok:!badList,json:async()=>listValue};const ident=url.split('/').at(-1);if(held&&ident===a)return new Promise(resolve=>{resolveHeld=resolve;});return {ok:true,json:async()=>badDetail?{...detail(ident),source_certification:true}:detail(ident)};}
const window={location:{hash:''},addEventListener(){}};
vm.runInNewContext(scripts[0][1],{document,window,fetch,console,Number,String,Array,Object,Date,Error,Promise,JSON,decodeURIComponent},{filename:'source_check.html'});
(async()=>{
 const app=window.SourceChecksUI;await app.ready;
 assert.equal(ids.records.children.length,2);assert.equal(ids.rows.children.length,2);
 assert.match(ids.rows.textContent,/已过期，不能用于执行/);assert.match(ids['request-window'].textContent,/2026-10-26 至 2026-11-01/);
 assert.doesNotMatch(ids.rows.textContent,/NaN|undefined/);assert.match(ids.status.textContent,/历史检查/);
 const count=requests.length;await app.open('../private',true);assert.equal(requests.length,count);
 held=true;const pending=app.open(a,false);await app.open(b,true);resolveHeld({ok:false,json:async()=>({})});await pending;assert.match(ids['receipt-id'].textContent,new RegExp(b));held=false;
 badDetail=true;await app.open(a,false);assert.equal(ids.rows.children.length,0);assert.equal(ids['receipt-id'].textContent,'');assert.ok(ids.detail.classList.contains('hidden'));badDetail=false;
 app.state.selected='';window.location.hash='';listValue={records:[],truncated:true};await app.refresh();assert.match(ids['list-status'].textContent,/结果不完整/);assert.doesNotMatch(ids.status.textContent,/尚无检查记录/);
 listValue={records:[],truncated:false};await app.refresh();assert.match(ids['list-status'].textContent,/尚无检查记录/);
 for(const records of [[{check_id:'bad'}],[summary(a),{check_id:'bad'}],[summary(a),summary(a)],Array(101).fill(summary(a))]){listValue={records,truncated:false};await app.refresh();assert.equal(ids['list-status'].textContent,'读取失败');assert.equal(ids.records.children.length,0);assert.equal(ids.rows.children.length,0);}
 listValue={records:[{check_id:a,status:'CHECK_REQUIRED',provider:null,symbol:null,observed_at:null,mode:'RESEARCH_ONLY',paper_admission:false}],truncated:false};await app.refresh();assert.match(ids.records.textContent,/记录损坏，需核查/);
 app.state.selected='';window.location.hash='#'+outside;listValue={records:[],truncated:true};await app.refresh();assert.equal(app.state.selected,outside);assert.match(ids['receipt-id'].textContent,new RegExp(outside));
 olderPrevious=true;listValue={records:[summary(a),summary(b)],truncated:false};
 app.state.selected=a;await app.refresh();assert.match(ids['sample-progression'].textContent,/分钟时间相同/);
 assert.match(ids['sample-progression'].textContent,/不能证明窗口连续覆盖/);
 badList=true;await app.refresh();assert.equal(ids.rows.children.length,0);assert.equal(ids.cards.children.length,0);assert.ok(ids.detail.classList.contains('hidden'));assert.match(ids.status.textContent,/旧详情已清空/);
 assert.doesNotMatch(html,/href=["']https?:|src=["']https?:/);assert.match(html,/不是当前行情延迟|并非当前行情延迟/);assert.match(html,/不向供应商发请求/);
 process.stdout.write('PASS: Chinese saved-source history, no network-trigger UI, bounded validated lists, stale snapshots, false-admission gates, direct IDs and failure clearing\n');
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
