/* Candidate UI behavior, no browser profile, network, feed, or real input file. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('live_paper_compare/static/data_check.html','utf8');
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
const a='d'.repeat(32),b='e'.repeat(32);
const record=id=>({candidate_id:id,label:id===a?'<img src=x onerror=alert(1)>':'另一候选',row_count:1,symbols:['501018.SH'],symbol_count:1,status:'CANDIDATE_ONLY',summary_validation:'NOT_CHECKED',mode:'RESEARCH_ONLY',source_evidence_copied:false,start:'20260728',end:'20260728',manifest_sha256:'a'.repeat(64),rows_sha256:'b'.repeat(64),paper_admission:false,candidate_promoted_to_runtime:false});
const validation={ok:true,schema_valid:true,manifest_sha256_matches:true,rows_sha256_matches:true,content_id_matches:true};
const detail=id=>({record:record(id),preview_rows:[{ts_code:'501018.SH',trade_date:'20260728',open:2,high:2.1,low:1.9,close:2,pre_close:2,vol:100,amount:20}],validation});
let failList=false,corruptDetail=false,stale=null,holdA=false,listPayload=null;
const requests=[];
async function fetch(url,options){requests.push({url,options});assert.equal(options.method,undefined,'UI must use only GET');if(url==='/api/research/candidates')return {ok:!failList,json:async()=>listPayload||({candidates:[record(a),record(b)],truncated:false})};const id=url.split('/').at(-1);if(holdA&&id===a)return new Promise(resolve=>{stale=resolve;});return {ok:true,json:async()=>corruptDetail==='unchecked'?{...detail(id),validation:{...validation,rows_sha256_matches:false}}:corruptDetail==='oversize'?{record:record(id),preview_rows:Array(51).fill(detail(id).preview_rows[0]),validation}:corruptDetail?{record:{...record(id),paper_admission:true},preview_rows:[],validation}:detail(id)};}
const window={location:{hash:''},addEventListener(){}};
vm.runInNewContext(scripts[0][1],{document,window,fetch,console,encodeURIComponent,decodeURIComponent,Number,String,Array,Object,Error,Promise,JSON},{filename:'data_check.html'});
(async()=>{
 const app=window.CandidateData;await app.ready;
 assert.equal(ids['candidate-list'].children.length,2);
 assert.match(ids['detail-title'].textContent,/<img src=x/);
 assert.equal(made.includes('img'),false);
 assert.equal(ids['rows-body'].children.length,1);
 assert.equal(ids.download.href,'/api/research/candidates/'+a+'/rows.json');
 assert.match(ids['detail-status'].textContent,/未认证候选/);
 assert.doesNotMatch(ids['rows-body'].textContent,/NaN|undefined/);
 const before=requests.length;assert.equal(await app.open('../../private',true),null);assert.equal(requests.length,before);
 await app.open(b,true);assert.equal(ids['detail-title'].textContent,'另一候选');
 holdA=true;const pending=app.open(a,false);await app.open(b,false);stale({ok:false,json:async()=>({})});await pending;
 assert.equal(ids['detail-title'].textContent,'另一候选','late failure must not clear a newer detail');holdA=false;
 corruptDetail=true;await app.open(a,false);assert.ok(ids['detail-content'].classList.contains('hidden'));assert.equal(ids['rows-body'].children.length,0);assert.equal(ids.download.href,'');
 corruptDetail='unchecked';await app.open(a,false);assert.ok(ids['detail-content'].classList.contains('hidden'));assert.equal(ids['rows-body'].children.length,0);
 corruptDetail='oversize';await app.open(a,false);assert.ok(ids['detail-content'].classList.contains('hidden'));assert.equal(ids['rows-body'].children.length,0);
 corruptDetail=false;await app.refresh();assert.ok(!ids['detail-content'].classList.contains('hidden'));
 const outside='f'.repeat(32);app.state.selected='';window.location.hash='#'+outside;await app.refresh();assert.equal(app.state.selected,outside,'a direct candidate link must work beyond the bounded visible list');
 app.state.selected='';window.location.hash='';listPayload={candidates:[],truncated:true};await app.refresh();assert.match(ids['list-status'].textContent,/结果不完整/);assert.match(ids['list-status'].textContent,/不能.*判断.*为空/);assert.doesNotMatch(ids['detail-status'].textContent,/尚无已登记候选/);assert.ok(ids['detail-content'].classList.contains('hidden'));
 listPayload={candidates:[],truncated:false};await app.refresh();assert.match(ids['list-status'].textContent,/尚无已登记候选/);
 listPayload={candidates:[record(a)],truncated:true};await app.refresh();assert.match(ids['list-status'].textContent,/本次显示 1.*结果不完整/);
 for(const candidates of [[{candidate_id:'bad-id'}],[record(a),{candidate_id:'bad-id'}],[record(a),record(a)],[{...record(a),row_count:undefined}],[{...record(a),symbols:'not-array'}],Array(101).fill(record(a))]){listPayload={candidates,truncated:false};await app.refresh();assert.equal(ids['list-status'].textContent,'读取失败');assert.equal(ids['candidate-list'].children.length,0);assert.equal(ids['rows-body'].children.length,0);assert.equal(ids.download.href,'');}
 listPayload={candidates:[{...record(a),label:'',status:'CHECK_REQUIRED',summary_validation:'RECORD_INVALID',row_count:0,symbols:[],symbol_count:undefined}],truncated:false};await app.refresh();assert.equal(ids['candidate-list'].children.length,1);assert.match(ids['candidate-list'].textContent,/登记记录损坏/);
 listPayload={};await app.refresh();assert.equal(ids['list-status'].textContent,'读取失败');assert.equal(ids.download.href,'');listPayload=null;
 failList=true;await ids.refresh.click();assert.equal(ids['candidate-list'].children.length,0);assert.equal(ids['rows-body'].children.length,0);assert.ok(ids['detail-content'].classList.contains('hidden'));assert.match(ids['detail-status'].textContent,/未显示先前详情/);
 assert.ok(requests.every(r=>r.url.startsWith('/api/research/candidates')));
 process.stdout.write('PASS: candidate-only Chinese UI, safe text, bounded preview, local download, stale-response isolation and fail-closed clearing\n');
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
