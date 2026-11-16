/* Offline mock-DOM coverage for the manually stepped portfolio replay UI. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('live_paper_compare/static/portfolio_replay.html', 'utf8');
const script = fs.readFileSync('live_paper_compare/static/portfolio_replay.js', 'utf8');
const SOURCE = 'a'.repeat(32);
const ACCOUNT = 'b'.repeat(32);
const OTHER_ACCOUNT = 'c'.repeat(32);
const VERSION = 'd'.repeat(64);
const REQUEST = '12345678123442348234123456781234';
const PENDING_KEY = 'quant-portfolio-replay-pending-v1';

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
    this.attributes = {};
    this._text = '';
    this.parentElement = null;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  set innerHTML(_) { throw new Error('innerHTML is forbidden in the replay UI'); }
  append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  addEventListener(type, callback) { (this.handlers[type] ||= []).push(callback); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  toggleAttribute(name, force) { if (force) this.attributes[name] = ''; else delete this.attributes[name]; if (name === 'hidden') this.hidden = force; }
  async fire(type, event = {}) {
    if (type === 'click' && this.disabled) return;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (const callback of this.handlers[type] || []) await callback(event);
  }
}

function response(data, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => data};
}
function sourceSummary(overrides = {}) {
  return {receipt: {run_id: SOURCE, status: 'completed', schema_version: 2, code_version: VERSION,
    source_snapshot_status: 'not_checked', artifact_integrity: 'not_checked'},
    config: {symbols: ['000001.SZSE', '000002.SZSE'], start: '2024-01-02', end: '2024-01-05'}, ...overrides};
}
function sourceDetail(overrides = {}) {
  return {receipt: {run_id: SOURCE, status: 'completed', schema_version: 2, code_version: VERSION,
    source_snapshot_status: 'archived_and_verified', artifact_integrity: 'verified'},
    config: {symbols: ['000001.SZSE', '000002.SZSE'], start: '2024-01-02', end: '2024-01-05'},
    result: {mode: 'RESEARCH_ONLY', paper_admission: false, provenance: {source_version: VERSION}}, ...overrides};
}
function record(overrides = {}) {
  return {account_id: ACCOUNT, label: '手动回放', status: 'paused', revision: 1, cursor: 0, total_frames: 240,
    remaining_frames: 240, continuation_allowed: true, code_current: true, current_equity: 100000,
    current_cash: 100000, creation_request_id: REQUEST, ...overrides};
}
function detail(accountId = ACCOUNT, overrides = {}) {
  const baseRecord = record({account_id: accountId, creation_request_id: REQUEST});
  return {record: baseRecord, config: {symbols: ['000002.SZSE', '000001.SZSE'], start: '2024-01-02', end: '2024-01-05'},
    source: {run_id: SOURCE, code_version: VERSION}, data_readiness: {status: 'mismatch'}, result: {cash: 72500, current_equity: 99900},
    equity: [{datetime: '2024-01-02 09:31', equity: 100000}, {datetime: '2024-01-02 09:32', equity: 99900}],
    orders: [{order_id: 'o1', symbol: '000001.SZSE', status: 'filled'}],
    trades: [{trade_id: 't1', symbol: '000001.SZSE', side: 'buy', quantity: 100}],
    positions: {'000001.SZSE': {quantity: 100, available_to_sell: 0, market_value: 7500, average_cost: 75, lots: [{quantity: 100, sellable_after: 'next day'}]}},
    events: [{event: 'initialized', cursor: 0}], ...overrides};
}
function makePage(fetcher, storage = new Map(), search = '') {
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
  ids['step-count'].value = '60';
  const document = {
    getElementById(id) { assert.ok(ids[id], `unknown element ${id}`); return ids[id]; },
    createElement(tag) { return new Node(tag); },
    createElementNS(_ns, tag) { return new Node(tag); },
  };
  const localStorage = {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  };
  const window = {
    localStorage,
    location: {hash: '', search},
    crypto: {randomUUID: () => '12345678-1234-4234-8234-123456781234'},
    handlers: {},
    addEventListener(type, callback) { (this.handlers[type] ||= []).push(callback); },
  };
  vm.runInNewContext(script, {document, window, fetch: fetcher, console, Uint8Array}, {filename: 'portfolio_replay.js'});
  return {ids, storage, window};
}
function tick() { return new Promise(resolve => setImmediate(resolve)); }
async function flush() { for (let i = 0; i < 5; i++) await tick(); }
function baseFetch(log, accountRecords = []) {
  return async (url, options = {}) => {
    log.push({url, options});
    if (url === '/api/research/portfolios') return response({runs: [sourceSummary()], truncated: false});
    if (url === `/api/research/portfolios/${SOURCE}`) return response(sourceDetail());
    if (url === '/api/research/portfolio-replays') return response({accounts: accountRecords});
    if (url.startsWith('/api/research/portfolio-replays/')) {
      const accountId = url.split('/')[4];
      const found = accountRecords.find(item => item.account_id === accountId) || record({account_id: accountId});
      return response(detail(accountId, {record: found}));
    }
    throw new Error(`unexpected fetch ${options.method || 'GET'} ${url}`);
  };
}

async function testSourceVerificationAndCodeGuard() {
  const calls = [];
  const page = makePage(baseFetch(calls, [record({code_current: false})]), new Map(), `?run_id=${SOURCE}`);
  await flush();
  assert.equal(page.ids['source-select'].value, SOURCE, 'a validated query run_id selects a matching source without creating an account');
  assert.ok(calls.some(call => call.url === `/api/research/portfolios/${SOURCE}`), 'selected candidate is verified through its full archived report');
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'page load never creates an account');
  assert.equal(page.ids['create-btn'].disabled, false, 'creation unlocks only after single-source verification');
  page.ids['source-select'].value = '';
  await page.ids['source-select'].fire('change');
  assert.equal(page.ids['create-btn'].disabled, true);
  page.ids['source-select'].value = SOURCE;
  await page.ids['source-select'].fire('change'); await flush();
  const listButton = page.ids['account-list'].children[0];
  await listButton.fire('click');
  await flush();
  assert.equal(page.ids['step-btn'].disabled, true, 'source code drift blocks manual steps');
  assert.equal(page.ids['resume-btn'].disabled, true, 'source code drift blocks resume');
  assert.match(page.ids.warnings.textContent, /源码版本/);
}

async function testCreateLostResponsePersistsAndRetriesSamePayload() {
  const storage = new Map(), calls = [];
  let accepted = false, postAttempt = 0, createdRecord;
  const fetcher = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/research/portfolios') return response({runs: [sourceSummary()], truncated: false});
    if (url === `/api/research/portfolios/${SOURCE}`) return response(sourceDetail());
    if (url === '/api/research/portfolio-replays' && !options.method) return response({accounts: accepted ? [createdRecord] : []});
    if (url === '/api/research/portfolio-replays' && options.method === 'POST') {
      const body = JSON.parse(options.body);
      postAttempt += 1;
      if (postAttempt === 1) throw new Error('response lost before server accepted create');
      accepted = true;
      createdRecord = record({creation_request_id: body.request_id});
      assert.equal(options.body, calls.find(call => call.url === url && call.options.method === 'POST').options.body);
      return response({record: createdRecord});
    }
    if (url === `/api/research/portfolio-replays/${ACCOUNT}`) return response(detail(ACCOUNT, {record: createdRecord}));
    throw new Error(`unexpected fetch ${options.method || 'GET'} ${url}`);
  };
  let page = makePage(fetcher, storage, `?run_id=${SOURCE}`);
  await flush();
  await page.ids['create-form'].fire('submit');
  await flush();
  assert.equal(storage.has(PENDING_KEY), true, 'create stays on disk after an uncertain response');
  const saved = JSON.parse(storage.get(PENDING_KEY));
  const exactPayload = saved.body_json;
  const postCount = calls.filter(call => call.options.method === 'POST').length;
  page = makePage(fetcher, storage, `?run_id=${SOURCE}`);
  await flush();
  assert.equal(calls.filter(call => call.options.method === 'POST').length, postCount, 'reload performs only GETs');
  assert.equal(JSON.parse(storage.get(PENDING_KEY)).body_json, exactPayload, 'reload preserves exact create payload and request ID');
  await page.ids['retry-pending-btn'].fire('click');
  await flush();
  const posts = calls.filter(call => call.url === '/api/research/portfolio-replays' && call.options.method === 'POST');
  assert.equal(posts.length, 2, 'only explicit retry sends the second create request');
  assert.equal(posts[0].options.body, posts[1].options.body, 'retry uses byte-identical payload');
  assert.equal(JSON.parse(posts[1].options.body).request_id, REQUEST, 'retry reuses the UUID4 request ID');
  assert.equal(storage.has(PENDING_KEY), false, 'durable request match clears pending recovery state');
}

async function testReloadFindsCreateByRequestIdWithoutRetry() {
  const storage = new Map(), calls = [];
  const createdRecord = record({creation_request_id: REQUEST});
  storage.set(PENDING_KEY, JSON.stringify({version: 1, type: 'create', body_json: JSON.stringify({run_id: SOURCE, label: '已创建', request_id: REQUEST})}));
  const fetcher = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/research/portfolios') return response({runs: [sourceSummary()], truncated: false});
    if (url === `/api/research/portfolios/${SOURCE}`) return response(sourceDetail());
    if (url === '/api/research/portfolio-replays') return response({accounts: [createdRecord]});
    if (url === `/api/research/portfolio-replays/${ACCOUNT}`) return response(detail(ACCOUNT, {record: createdRecord}));
    throw new Error(`unexpected fetch ${options.method || 'GET'} ${url}`);
  };
  const page = makePage(fetcher, storage);
  await flush();
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0, 'reload only reads, even when the source request already committed');
  assert.equal(storage.has(PENDING_KEY), false, 'matching durable creation_request_id resolves the pending create from GET evidence');
  assert.equal(page.window.PortfolioReplay.state.selectedId, ACCOUNT, 'matching account becomes selected after read-only recovery');
}

async function testLostStepReloadIsReadOnlyThenExplicitSameRequestRetry() {
  const storage = new Map(), calls = [];
  let failStep = true;
  const account = record({status: 'running'});
  const fetcher = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/research/portfolios') return response({runs: [sourceSummary()], truncated: false});
    if (url === '/api/research/portfolio-replays') return response({accounts: [account]});
    if (url === `/api/research/portfolio-replays/${ACCOUNT}` && !options.method) return response(detail(ACCOUNT, {record: account}));
    if (url === `/api/research/portfolio-replays/${ACCOUNT}/step`) {
      if (failStep) { failStep = false; throw new Error('response lost after step'); }
      const body = JSON.parse(options.body);
      return response(detail(ACCOUNT, {record: record({status: 'running', revision: 2, cursor: body.steps, remaining_frames: 240 - body.steps}),
        replayed: true, request_id: body.request_id, original_revision: body.expected_revision, applied_revision: body.expected_revision + 1}));
    }
    throw new Error(`unexpected fetch ${options.method || 'GET'} ${url}`);
  };
  let page = makePage(fetcher, storage);
  await flush();
  await page.window.PortfolioReplay.openAccount(ACCOUNT, false, false);
  page.ids['step-count'].value = '60';
  await page.ids['step-btn'].fire('click');
  await flush();
  assert.equal(storage.has(PENDING_KEY), true, 'lost step response preserves exact request');
  const original = JSON.parse(storage.get(PENDING_KEY)).body_json;
  const postCount = calls.filter(call => call.options.method === 'POST').length;
  page = makePage(fetcher, storage);
  await flush();
  assert.equal(calls.filter(call => call.options.method === 'POST').length, postCount, 'reload never resends a step automatically');
  assert.equal(JSON.parse(storage.get(PENDING_KEY)).body_json, original, 'reload preserves the same step request ID and body');
  await page.ids['retry-pending-btn'].fire('click');
  await flush();
  const stepPosts = calls.filter(call => call.url.endsWith('/step') && call.options.method === 'POST');
  assert.equal(stepPosts.length, 2, 'second step request follows an explicit user action');
  assert.equal(stepPosts[0].options.body, stepPosts[1].options.body, 'step retry is byte-identical');
  assert.equal(JSON.parse(stepPosts[1].options.body).request_id, REQUEST, 'step retry retains original request ID');
  assert.equal(storage.has(PENDING_KEY), false, 'validated replay response clears pending step');
}

async function testRevisionConflictNeedsExplicitAbandon() {
  const storage = new Map(), calls = [];
  const account = record({status: 'running'});
  const fetcher = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/research/portfolios') return response({runs: [], truncated: false});
    if (url === '/api/research/portfolio-replays') return response({accounts: [account]});
    if (url === `/api/research/portfolio-replays/${ACCOUNT}` && !options.method) return response(detail(ACCOUNT, {record: account}));
    if (url.endsWith('/step')) return response({code: 'PortfolioReplayRevisionConflict', rollback_confirmed: true, error: 'revision changed'}, 409);
    throw new Error(`unexpected fetch ${options.method || 'GET'} ${url}`);
  };
  const page = makePage(fetcher, storage);
  await flush();
  await page.window.PortfolioReplay.openAccount(ACCOUNT, false, false);
  await page.ids['step-btn'].fire('click');
  await flush();
  assert.equal(page.ids['abandon-pending-btn'].hidden, false, 'only a server-confirmed pre-commit revision conflict exposes abandon');
  assert.equal(storage.has(PENDING_KEY), true, 'conflicting step remains stored until explicit user choice');
  await page.ids['abandon-pending-btn'].fire('click');
  await flush();
  assert.equal(storage.has(PENDING_KEY), false, 'explicit abandon removes the confirmed-uncommitted step');
  assert.ok(calls.some(call => call.url === '/api/research/portfolio-replays' && !call.options.method), 'abandon refreshes account state through GET');
}

async function testDetailSelectionRejectsStaleResponse() {
  const calls = [];
  let resolveA;
  const a = record({account_id: ACCOUNT, label: '较旧账户'});
  const b = record({account_id: OTHER_ACCOUNT, label: '当前账户'});
  const fetcher = (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/research/portfolios') return Promise.resolve(response({runs: [], truncated: false}));
    if (url === '/api/research/portfolio-replays') return Promise.resolve(response({accounts: [a, b]}));
    if (url === `/api/research/portfolio-replays/${ACCOUNT}`) return new Promise(resolve => { resolveA = resolve; });
    if (url === `/api/research/portfolio-replays/${OTHER_ACCOUNT}`) return Promise.resolve(response(detail(OTHER_ACCOUNT, {record: b})));
    throw new Error(`unexpected fetch ${options.method || 'GET'} ${url}`);
  };
  const page = makePage(fetcher);
  await flush();
  const first = page.window.PortfolioReplay.openAccount(ACCOUNT, false, true);
  const second = page.window.PortfolioReplay.openAccount(OTHER_ACCOUNT, false, true);
  await second;
  resolveA(response(detail(ACCOUNT, {record: a})));
  await first;
  assert.equal(page.ids['detail-title'].textContent, '当前账户', 'late response cannot overwrite the newest selection');
  assert.equal(page.window.PortfolioReplay.state.selectedId, OTHER_ACCOUNT);
}

async function testChartUsesObservedTimesAndFilteredIndexes() {
  const fetcher = async url => {
    if (url === '/api/research/portfolios') return response({runs: []});
    if (url === '/api/research/portfolio-replays') return response({accounts: [record()]});
    return response(detail(ACCOUNT, {equity: [
      {datetime: '2024-01-02T00:00:00', source_datetime: null, equity: 100000},
      {source_datetime: '2024-01-02T09:31:00', equity: 100001},
      {source_datetime: '2024-01-02T09:32:00', equity: NaN},
      {source_datetime: '2024-01-02T09:33:00', equity: 100002}
    ]}));
  };
  const page = makePage(fetcher); await flush();
  await page.window.PortfolioReplay.openAccount(ACCOUNT, false, true);
  const circles = page.ids['equity-chart'].children.filter(node => node.tagName === 'CIRCLE');
  assert.equal(circles.length, 2, 'seed and invalid equity are excluded');
  assert.equal(circles[0].attributes.cx, '72');
  assert.equal(circles[1].attributes.cx, '878', 'filtered points remain inside plot bounds');
}
async function testKnownSourceRejectionCanBeExplicitlyDiscarded() {
  for (const [status, confirmed] of [[409, true], [409, false], [500, true]]) {
    const storage = new Map();
    const fetcher = async (url, options = {}) => {
      if (url === '/api/research/portfolios') return response({runs: [sourceSummary()]});
      if (url === `/api/research/portfolios/${SOURCE}`) return response(sourceDetail());
      if (options.method === 'POST') return response({code: 'PortfolioReplaySourceChanged', rollback_confirmed: confirmed, error: 'source changed'}, status);
      return response({accounts: []});
    };
    const page = makePage(fetcher, storage, `?run_id=${SOURCE}`); await flush();
    page.ids['account-label'].value = '旧版来源';
    await page.ids['create-form'].fire('submit'); await flush();
    assert.equal(storage.has(PENDING_KEY), true);
    const canDiscard = status === 409 && confirmed;
    assert.equal(page.ids['abandon-pending-btn'].hidden, !canDiscard);
    await page.ids['abandon-pending-btn'].fire('click'); await flush();
    assert.equal(storage.has(PENDING_KEY), !canDiscard, 'unknown commit state stays pending');
  }
}

(async () => {
  await testChartUsesObservedTimesAndFilteredIndexes();
  await testKnownSourceRejectionCanBeExplicitlyDiscarded();
  await testSourceVerificationAndCodeGuard();
  await testCreateLostResponsePersistsAndRetriesSamePayload();
  await testReloadFindsCreateByRequestIdWithoutRetry();
  await testLostStepReloadIsReadOnlyThenExplicitSameRequestRetry();
  await testRevisionConflictNeedsExplicitAbandon();
  await testDetailSelectionRejectsStaleResponse();
  console.log('portfolio replay UI mock-DOM tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
