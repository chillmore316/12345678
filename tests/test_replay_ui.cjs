/* Isolated DOM/API lifecycle: no actual services, data or accounts. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('live_paper_compare/static/replay.html','utf8');
assert.match(html,/<html lang="zh-CN">/);assert.match(html,/历史模拟演练/);assert.match(html,/RESEARCH_ONLY/);assert.doesNotMatch(html,/<script[^>]+src=/i);
class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.value='';this.disabled=false;this.attributes={};this.listeners={};this._text='';this.className='';this.style={};this.classList={add:(name)=>{if(!this.className.includes(name))this.className+=' '+name;},remove:(name)=>{this.className=this.className.split(/\s+/).filter(n=>n!==name).join(' ');},contains:(name)=>this.className.split(/\s+/).includes(name)};}
 set textContent(text){this._text=String(text);this.children=[];}get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
 set innerHTML(value){throw Error('unsafe HTML is forbidden');}append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this._text='';this.children=[...nodes];}setAttribute(k,v){this.attributes[k]=v;}addEventListener(k,v){this.listeners[k]=v;}
}
const ids={};for(const m of html.matchAll(/id="([^"]+)"/g))ids[m[1]]=new Element();
const run='b'.repeat(32),account='a'.repeat(32),calls=[];let cursor=0,revision=0,status='paused',failNext=false;
function detail(){return {record:{account_id:account,run_id:run,label:'<unsafe>',status,revision,cursor,total_bars:3,current_equity:1000},config:{symbol:'000001.SZSE',strategy:'buy_hold',start:'2024-01-02',end:'2024-01-02'},data_readiness:{status:'mismatch'},source:{data_readiness:{status:'mismatch'}},equity:[{source_datetime:null,cash:1000,quantity:0,equity:1000},...Array.from({length:cursor},(_,i)=>({source_datetime:`2024-01-02T09:${31+i}:00`,cash:1000,quantity:0,equity:1000}))],bars:Array.from({length:cursor},()=>({})),orders:[],trades:[],positions:[],events:[]};}
const context={document:{getElementById:(name)=>{assert.ok(ids[name],name);return ids[name];},createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag)},window:{location:{hash:'',search:''},addEventListener:()=>{}},URLSearchParams,setTimeout:()=>1,clearTimeout:()=>{},console,fetch:async(url,options={})=>{
 calls.push({url,options});let response,ok=true;
 if(url==='/api/research/runs')response={runs:[{run_id:run,status:'completed',backend:'reference',strategy_version:'v1',symbol:'000001.SZSE',start:'2024-01-02'}]};
 else if(url==='/api/research/replays'&&!options.method)response={accounts:[]};
 else if(url==='/api/research/replays')response=detail();
 else if(!options.method)response=detail();
 else {const body=JSON.parse(options.body);if(failNext){failNext=false;ok=false;response={error:'simulated failure'};}else {assert.equal(body.expected_revision,revision);if(url.endsWith('/resume'))status='running';else if(url.endsWith('/pause'))status='paused';else if(url.endsWith('/step')){assert.equal(status,'running');assert.ok(body.request_id);cursor=Math.min(3,cursor+body.steps);if(cursor===3)status='completed';}revision++;response=detail();}}
 return {ok,json:async()=>response};
}};
vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
(async()=>{const app=context.window.ReplayCenter;await app.ready;assert.equal(app.helpers.safeId('../bad'),'');ids['source-run'].value=run;await app.create({preventDefault(){}});assert.equal(app.state.selected,account);assert.equal(ids['account-title'].textContent,'<unsafe>');assert.match(ids['account-warning'].textContent,/冲突/);assert.equal(cursor,0);assert.equal(ids['play'].disabled,false);
 await app.manualStep(1);assert.equal(cursor,1);assert.equal(status,'running');assert.equal(app.state.detail.bars.length,1);assert.equal(ids['account-chart'].classList.contains('hidden'),false);assert.ok(calls.some(call=>call.url.endsWith('/step')&&JSON.parse(call.options.body).steps===1));
 await app.pause();assert.equal(status,'paused');assert.equal(cursor,1);assert.match(ids['page-status'].textContent,/已暂停/);
 await app.play();assert.equal(cursor,3);assert.equal(status,'completed');assert.equal(app.state.auto,false);assert.equal(ids['play'].disabled,true);assert.equal(ids['step-one'].disabled,true);assert.equal(ids['exports'].children.length,5);
 status='paused';cursor=0;revision++;await app.openAccount(account,false);failNext=true;await app.play();assert.equal(app.state.auto,false);assert.equal(cursor,0);assert.match(ids['page-status'].textContent,/操作未完成/);
 const stepsBeforePause=calls.filter(call=>call.url.endsWith('/step')).length;await Promise.all([app.play(),app.pause()]);assert.equal(status,'paused');assert.equal(cursor,0);assert.equal(calls.filter(call=>call.url.endsWith('/step')).length,stepsBeforePause);
 console.log('PASS: isolated Chinese replay lifecycle, safe text, request revisions, step IDs, pause, auto-complete, no future rows, read failure handling and exports');
})().catch(e=>{console.error(e);process.exitCode=1;});
