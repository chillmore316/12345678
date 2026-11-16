/* Offline mock-DOM coverage for the local portfolio submission workflow. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('live_paper_compare/static/portfolio.html', 'utf8');
const script = fs.readFileSync('live_paper_compare/static/portfolio_submit.js', 'utf8');
const JOB = '0123456789abcdef0123456789abcdef';
const OTHER_JOB = '11111111111111111111111111111111';
const RUN = 'abcdefabcdefabcdefabcdefabcdefab';
const REQUEST = '12345678123442348234123456781234';
const VERSION = 'a'.repeat(64);
const HASH = 'b'.repeat(64);
const ATTACK = '<img src=x onerror=alert(1)>';

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.handlers = {};
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.className = '';
    this.href = '';
    this._text = '';
    this.parentElement = null;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  set innerHTML(_) { throw new Error('innerHTML is forbidden in the submission UI'); }
  append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  addEventListener(type, callback) { (this.handlers[type] ||= []).push(callback); }
  async fire(type, event = {}) {
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (const callback of this.handlers[type] || []) await callback(event);
  }
  reportValidity() { return true; }
}

function response(data, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => data};
}
function preview(config = {}) {
  const normalized = {
    symbols: ['000001.SZSE', '000002.SZSE'], start: '2024-01-02', end: '2024-01-05',
    interval: '1m', strategy: 'buy_hold', initial_cash: 100000, params: {target_fraction: 0.5}, ...config,
  };
  return {valid: true, plan_only: true, mode: 'RESEARCH_ONLY', paper_admission: false, source_version: VERSION, config_sha256: HASH,
    limits: {wall_seconds: 300}, cases: [{config: normalized}], scope: {portfolio_aggregation: true}};
}
function job(status = 'queued', overrides = {}) {
  return {job_id: JOB, kind: 'portfolio', request_id: REQUEST, mode: 'RESEARCH_ONLY', paper_admission: false, status, plan: {cases: []}, receipt: {cases: []}, ...overrides};
}
function configFromForm(ids) {
  return {
    symbols: ids['portfolio-symbols'].value.split(',').map(value => value.trim().toUpperCase()).filter(Boolean),
    start: ids['portfolio-start'].value, end: ids['portfolio-end'].value, interval: '1m',
    strategy: ids['portfolio-strategy'].value, initial_cash: Number(ids['portfolio-cash'].value),
    commission_rate: Number(ids['portfolio-commission'].value), min_commission: Number(ids['portfolio-min-commission'].value),
    slippage_bps: Number(ids['portfolio-slippage'].value),
    params: {target_fraction: Number(ids['portfolio-target-fraction'].value),
      max_turnover_participation: Number(ids['portfolio-participation'].value),
      fast: Number(ids['portfolio-fast'].value), slow: Number(ids['portfolio-slow'].value)},
  };
}
function savedPending({jobId = '', requestId = REQUEST} = {}) {
  const payload = {kind: 'portfolio', spec: {config: {
    symbols: ['000001.SZSE', '000002.SZSE'], start: '2024-01-02', end: '2024-01-05', interval: '1m',
    strategy: 'buy_hold', initial_cash: 100000, commission_rate: 0.00025, min_commission: 5,
    slippage_bps: 5, params: {target_fraction: 0.5, max_turnover_participation: 0.01, fast: 5, slow: 20},
  }, expected_source_version: VERSION}, request_id: requestId};
  return JSON.stringify({version: 1, payload_json: JSON.stringify(payload), job_id: jobId});
}
function makePage(fetch, storage = new Map(), options = {}) {
  const ids = {};
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
    const [, tag, attrs] = match;
    const id = attrs.match(/\bid="([^"]+)"/i)?.[1];
    if (!id) continue;
    const node = new Node(tag);
    node.className = attrs.match(/\bclass="([^"]*)"/i)?.[1] || '';
    node.hidden = /\shidden(?:\s|>|$)/i.test(attrs);
    node.disabled = /\sdisabled(?:\s|>|$)/i.test(attrs);
    node.value = attrs.match(/\bvalue="([^"]*)"/i)?.[1] || '';
    ids[id] = node;
  }
  Object.assign(ids['portfolio-symbols'], {value: '000001.SZSE,000002.SZSE'});
  Object.assign(ids['portfolio-start'], {value: '2024-01-02'});
  Object.assign(ids['portfolio-end'], {value: '2024-01-05'});
  Object.assign(ids['portfolio-strategy'], {value: 'buy_hold'});
  Object.assign(ids['portfolio-cash'], {value: '100000'});
  Object.assign(ids['portfolio-commission'], {value: '0.00025'});
  Object.assign(ids['portfolio-min-commission'], {value: '5'});
  Object.assign(ids['portfolio-slippage'], {value: '5'});
  Object.assign(ids['portfolio-target-fraction'], {value: '0.5'});
  Object.assign(ids['portfolio-participation'], {value: '0.01'});
  Object.assign(ids['portfolio-fast'], {value: '5'});
  Object.assign(ids['portfolio-slow'], {value: '20'});
  const document = {
    getElementById(id) { assert.ok(ids[id], `unknown element ${id}`); return ids[id]; },
    createElement(tag) { (options.createdTags ||= []).push(tag.toLowerCase()); return new Node(tag); },
  };
  const localStorage = {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  };
  const timers = new Map();
  let nextTimer = 1;
  const window = {
    localStorage,
    crypto: {randomUUID: () => '12345678-1234-4234-8234-123456781234'},
    handlers: {},
    addEventListener(type, callback) { (this.handlers[type] ||= []).push(callback); },
    setTimeout(callback) { const id = nextTimer++; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    ...(options.window || {}),
  };
  vm.runInNewContext(script, {document, window, fetch, console, setTimeout, clearTimeout}, {filename: 'portfolio_submit.js'});
  return {ids, storage, window, timers, createdTags: options.createdTags || []};
}
function tick() { return new Promise(resolve => setImmediate(resolve)); }
async function flush() { await tick(); await tick(); }
async function previewPage(page) {
  await page.ids['portfolio-submit-form'].fire('submit');
  await flush();
  assert.equal(page.ids['portfolio-preview-panel'].hidden, false, 'valid response opens the preview summary');
}

(async () => {
  // No preview, no create request.
  {
    const calls = [];
    const page = makePage(async (url, options = {}) => { calls.push({url, options}); return response(preview()); });
    await page.ids['portfolio-confirm-button'].fire('click');
    await flush();
    assert.equal(calls.length, 0);
    assert.equal(page.ids['portfolio-confirm-button'].disabled, true);
  }

  // An edit invalidates the displayed preview and cannot submit it.
  {
    const calls = [];
    const page = makePage(async (url, options = {}) => {
      calls.push({url, options});
      if (url.endsWith('/preview')) return response(preview());
      throw new Error('unexpected request');
    });
    await previewPage(page);
    await page.ids['portfolio-submit-form'].fire('input');
    assert.equal(page.ids['portfolio-preview-panel'].hidden, true);
    assert.equal(page.ids['portfolio-confirm-button'].disabled, true);
    await page.ids['portfolio-confirm-button'].fire('click');
    assert.equal(calls.filter(call => call.options.method === 'POST' && !call.url.endsWith('/preview')).length, 0);
  }

  // Standard preview and explicit confirmation persist the exact payload first.
  {
    const calls = [];
    const storage = new Map();
    const page = makePage(async (url, options = {}) => {
      calls.push({url, options});
      if (url.endsWith('/preview')) return response(preview());
      if (url === '/api/research/workflows') return response(job('queued'));
      throw new Error('unexpected URL ' + url);
    }, storage);
    await previewPage(page);
    assert.match(page.ids['portfolio-preview-summary'].textContent, /共享现金预算/);
    assert.match(page.ids['portfolio-preview-summary'].textContent, /300 秒/);
    await page.ids['portfolio-confirm-button'].fire('click');
    await flush();
    const create = calls.find(call => call.url === '/api/research/workflows' && call.options.method === 'POST');
    assert.ok(create, 'confirmation sends one durable create request');
    const body = JSON.parse(create.options.body);
    assert.equal(body.kind, 'portfolio');
    assert.deepEqual(body.spec.config.symbols, ['000001.SZSE', '000002.SZSE']);
    assert.equal(body.spec.config.interval, '1m');
    assert.equal(body.spec.expected_source_version, VERSION);
    assert.match(body.request_id, /^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/);
    const stored = JSON.parse(storage.get('quant-portfolio-pending-v1'));
    assert.equal(stored.payload_json, create.options.body, 'the exact POST JSON is persisted');
    assert.equal(stored.job_id, JOB);
    assert.equal(page.timers.size, 1, 'active jobs use bounded delayed polling');
  }

  // An acknowledgement for another request or mode never clears the original payload.
  {
    const storage=new Map();
    const page=makePage(async (url)=>url.endsWith('/preview')?response(preview()):response(job('queued',{request_id:'99999999999949998999999999999999'})),storage);
    await previewPage(page);
    await page.ids['portfolio-confirm-button'].fire('click');
    await flush();
    assert.equal(JSON.parse(storage.get('quant-portfolio-pending-v1')).job_id,'');
    assert.equal(page.ids['portfolio-retry-button'].hidden,false);
    assert.match(page.ids['portfolio-submit-status'].textContent,/暂不确定/);
  }

  // A new uncertain submission cannot retain an old completed job as its result.
  {
    const storage=new Map([['quant-portfolio-pending-v1',savedPending({jobId:JOB})]]);
    const page=makePage(async(url,options={})=>{
      if(url.endsWith('/'+JOB))return response(job('completed',{receipt:{cases:[{status:'completed',run_status:'completed',run_id:RUN}]}}));
      if(url.endsWith('/preview'))return response(preview());
      throw new Error('lost new response');
    },storage);
    await flush();
    assert.match(page.ids['portfolio-job-cases'].textContent,/打开已完成报告/);
    await previewPage(page);
    await page.ids['portfolio-confirm-button'].fire('click');await flush();
    assert.doesNotMatch(page.ids['portfolio-job-cases'].textContent,/打开已完成报告/);
    assert.match(page.ids['portfolio-job-status'].textContent,/新请求等待确认/);
  }

  // Polling covers the execution window but remains finite and visibly pauses.
  {
    const storage=new Map([['quant-portfolio-pending-v1',savedPending({jobId:JOB})]]);
    const page=makePage(async()=>response(job('running')),storage);await flush();
    let polls=0;
    while(page.timers.size&&polls<1000){const [key,callback]=[...page.timers.entries()][0];page.timers.delete(key);callback();await flush();polls++;}
    assert.ok(polls>=150&&polls<=160);
    assert.equal(page.timers.size,0);
    assert.match(page.ids['portfolio-job-status'].textContent,/自动查询已暂停/);
    assert.equal(page.ids['portfolio-refresh-job'].disabled,false);
  }

  // Returning from the browser page cache restarts only reads, not submission.
  {
    const storage=new Map([['quant-portfolio-pending-v1',savedPending({jobId:JOB})]]);
    let release,reads=0;const calls=[];
    const page=makePage((url,options={})=>{calls.push({url,options});reads++;
      if(reads===1)return new Promise(resolve=>{release=resolve;});return Promise.resolve(response(job('running')));
    },storage);await flush();
    assert.equal(page.ids['portfolio-refresh-job'].disabled,true);
    page.window.handlers.pagehide[0]();page.window.handlers.pageshow[0]({persisted:true});await flush();
    assert.equal(reads,2);assert.ok(calls.every(x=>!x.options.method));
    assert.equal(page.ids['portfolio-refresh-job'].disabled,false);
    release(response(job('completed')));await flush();
    assert.match(page.ids['portfolio-job-status'].textContent,/运行中/);
  }

  // Lost create response survives reload; reload only GETs, and explicit retry is byte-identical.
  {
    const storage = new Map();
    const firstCalls = [];
    const first = makePage(async (url, options = {}) => {
      firstCalls.push({url, options});
      if (url.endsWith('/preview')) return response(preview());
      throw new Error('response lost after request may have reached the service');
    }, storage);
    await previewPage(first);
    await first.ids['portfolio-confirm-button'].fire('click');
    await flush();
    const original = firstCalls.find(call => call.url === '/api/research/workflows' && call.options.method === 'POST').options.body;
    assert.equal(first.ids['portfolio-retry-button'].hidden, false);
    assert.equal(JSON.parse(storage.get('quant-portfolio-pending-v1')).job_id, '');

    const reloadCalls = [];
    const reloaded = makePage(async (url, options = {}) => {
      reloadCalls.push({url, options});
      if (url === '/api/research/workflows' && !options.method) return response({jobs: []});
      if (url === '/api/research/workflows') return response(job('queued'));
      throw new Error('unexpected URL ' + url);
    }, storage);
    await flush();
    assert.equal(reloadCalls.some(call => call.options.method === 'POST'), false, 'reload never resubmits');
    assert.equal(reloaded.ids['portfolio-retry-button'].hidden, false);
    await reloaded.ids['portfolio-retry-button'].fire('click');
    await flush();
    const retry = reloadCalls.find(call => call.url === '/api/research/workflows' && call.options.method === 'POST');
    assert.ok(retry);
    assert.equal(retry.options.body, original, 'retry sends the exact original JSON and request_id');
  }

  // Reload with a known job ID continues by GET only and restores its state.
  {
    const storage = new Map([['quant-portfolio-pending-v1', savedPending({jobId: JOB})]]);
    const calls = [];
    const page = makePage(async (url, options = {}) => {
      calls.push({url, options});
      if (url === '/api/research/workflows/' + JOB) return response(job('running'));
      throw new Error('unexpected URL ' + url);
    }, storage);
    await flush();
    assert.deepEqual(calls.map(call => call.url), ['/api/research/workflows/' + JOB]);
    assert.ok(calls.every(call => !call.options.method));
    assert.match(page.ids['portfolio-job-status'].textContent, /运行中/);
  }

  // Reload with an unknown response finds the request by request_id, then reads the job.
  {
    const storage = new Map([['quant-portfolio-pending-v1', savedPending()]]);
    const calls = [];
    const page = makePage(async (url, options = {}) => {
      calls.push({url, options});
      if (url === '/api/research/workflows' && !options.method) return response({jobs: [{
        job_id: OTHER_JOB, request_id: REQUEST, kind: 'portfolio', status: 'running',
      }]});
      if (url === '/api/research/workflows/' + OTHER_JOB) return response(job('running', {job_id: OTHER_JOB}));
      throw new Error('unexpected URL ' + url);
    }, storage);
    await flush();
    assert.deepEqual(calls.map(call => call.url), ['/api/research/workflows', '/api/research/workflows/' + OTHER_JOB]);
    assert.ok(calls.every(call => !call.options.method), 'recovery is GET-only');
    assert.equal(JSON.parse(storage.get('quant-portfolio-pending-v1')).job_id, OTHER_JOB);
    assert.match(page.ids['portfolio-job-status'].textContent, /运行中/);
  }

  // Cancellation is a request only; terminal state comes from the subsequent GET.
  {
    const storage = new Map([['quant-portfolio-pending-v1', savedPending({jobId: JOB})]]);
    const calls = [];
    let reads = 0;
    const page = makePage(async (url, options = {}) => {
      calls.push({url, options});
      if (url === '/api/research/workflows/' + JOB + '/cancel') return response(job('cancelled'));
      if (url === '/api/research/workflows/' + JOB) {
        reads += 1;
        return response(job('running', {cancel_requested: reads > 1}));
      }
      throw new Error('unexpected URL ' + url);
    }, storage);
    await flush();
    assert.equal(page.ids['portfolio-cancel-job'].hidden, false);
    await page.ids['portfolio-cancel-job'].fire('click');
    await flush();
    const cancel = calls.find(call => call.url.endsWith('/cancel'));
    assert.equal(cancel.options.method, 'POST');
    assert.equal(cancel.options.body, '{}');
    assert.equal(calls.filter(call => call.url.endsWith('/' + JOB) && !call.options.method).length, 2);
    assert.match(page.ids['portfolio-job-status'].textContent, /运行中/);
    assert.match(page.ids['portfolio-job-status'].textContent, /已请求取消/);
    assert.doesNotMatch(page.ids['portfolio-job-status'].textContent, /已取消/);
  }

  // A source-version conflict clears the stale preview and known rejected request.
  {
    const storage = new Map();
    const calls = [];
    const page = makePage(async (url, options = {}) => {
      calls.push({url, options});
      if (url.endsWith('/preview')) return response(preview());
      if (url === '/api/research/workflows') return response({error: 'source version changed'}, 409);
      throw new Error('unexpected URL ' + url);
    }, storage);
    await previewPage(page);
    await page.ids['portfolio-confirm-button'].fire('click');
    await flush();
    assert.equal(page.ids['portfolio-preview-panel'].hidden, true);
    assert.equal(page.ids['portfolio-retry-button'].hidden, true);
    assert.equal(storage.has('quant-portfolio-pending-v1'), false);
    assert.match(page.ids['portfolio-submit-status'].textContent, /源码版本已变化/);
    assert.equal(calls.filter(call => call.options.method === 'POST' && !call.url.endsWith('/preview')).length, 1);
  }

  // An old preview response cannot replace a newer form state; error text stays text.
  {
    const calls = [];
    let release;
    const page = makePage((url, options = {}) => {
      calls.push({url, options});
      if (url.endsWith('/preview')) return new Promise(resolve => { release = resolve; });
      throw new Error('unexpected request');
    });
    await page.ids['portfolio-submit-form'].fire('submit');
    await page.ids['portfolio-submit-form'].fire('input');
    release(response(preview()));
    await flush();
    assert.equal(page.ids['portfolio-preview-panel'].hidden, true);
    assert.equal(page.ids['portfolio-confirm-button'].disabled, true);
    assert.equal(calls.filter(call => call.options.method === 'POST' && !call.url.endsWith('/preview')).length, 0);

    const safePage = makePage(async () => response({error: ATTACK}, 400));
    await safePage.ids['portfolio-submit-form'].fire('submit');
    await flush();
    assert.equal(safePage.ids['portfolio-submit-status'].textContent, '预检失败：' + ATTACK);
    assert.ok(!safePage.createdTags.includes('img') && !safePage.createdTags.includes('script'));
  }

  // Malformed stored IDs are discarded without any network request or execution.
  {
    const storage = new Map([['quant-portfolio-pending-v1', savedPending({requestId: 'f'.repeat(32)})]]);
    let networkCalls = 0;
    const page = makePage(async () => { networkCalls += 1; return response({jobs: []}); }, storage);
    await flush();
    assert.equal(networkCalls, 0);
    assert.equal(storage.has('quant-portfolio-pending-v1'), false);
    assert.match(page.ids['portfolio-submit-status'].textContent, /结构或编号无效/);
  }

  // Completed links require a completed overall job, completed case, and safe run ID.
  {
    const storage = new Map([['quant-portfolio-pending-v1', savedPending({jobId: JOB})]]);
    const page = makePage(async () => response(job('completed', {receipt: {cases: [
      {run_id: RUN, run_status: 'completed'}, {run_id: '<script>', run_status: 'completed'},
    ]}})), storage, {window: {PortfolioReport: {openRun(id, writeHash, force) {
      assert.equal(id, RUN); assert.equal(writeHash, true); assert.equal(force, true);
    }}}});
    await flush();
    const links = page.ids['portfolio-job-cases'].children.flatMap(row => row.children.filter(child => child.tagName === 'A'));
    assert.equal(links.length, 1);
    assert.equal(links[0].href, '/portfolio#' + RUN);
    await links[0].fire('click', {preventDefault() { this.prevented = true; }});
  }

  // Failed jobs do not expose receipt cases as completed results.
  {
    const storage = new Map([['quant-portfolio-pending-v1', savedPending({jobId: JOB})]]);
    const page = makePage(async () => response(job('failed', {receipt: {cases: [
      {run_id: RUN, run_status: 'completed', status: 'completed'},
    ]}})), storage);
    await flush();
    assert.equal(page.ids['portfolio-job-cases'].children.length, 0);
    assert.doesNotMatch(page.ids['portfolio-job-status'].textContent, /已完成/);
  }

  process.stdout.write('PASS: portfolio preview/submit idempotency, read-only recovery, cancellation, source conflicts, safe rendering, bounded polling and report links\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
