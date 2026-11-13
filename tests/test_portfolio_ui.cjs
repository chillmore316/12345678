/* Offline synthetic DOM/API coverage for the read-only portfolio report page. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('live_paper_compare/static/portfolio.html', 'utf8');
assert.match(html, /<html lang="zh-CN">/);
assert.doesNotMatch(html, /<script[^>]+src=|(?:src|href)=["']https?:/i, 'page must not load remote resources');
assert.match(html, /href="\/research"/, 'page links back to the single-stock workbench');
assert.doesNotMatch(html, /method\s*:\s*["']POST|\.innerHTML\s*=/i, 'page must not submit or insert HTML strings');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1, 'page script must remain self-contained');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.className = '';
    this.type = '';
    this.disabled = false;
    this.href = '';
    this._text = '';
    this.classList = {
      add: (...names) => { const set = new Set(this.className.split(/\s+/).filter(Boolean)); names.forEach(name => set.add(name)); this.className = [...set].join(' '); },
      remove: (...names) => { const remove = new Set(names); this.className = this.className.split(/\s+/).filter(name => name && !remove.has(name)).join(' '); },
      toggle: (name, force) => { const add = force === undefined ? !this.classList.contains(name) : Boolean(force); if (add) this.classList.add(name); else this.classList.remove(name); return add; },
      contains: name => this.className.split(/\s+/).includes(name),
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  set innerHTML(_) { throw new Error('unsafe HTML insertion is forbidden'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  async dispatch(type, event = {}) {
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    event.target ||= this;
    for (const callback of this.listeners[type] || []) await callback(event);
  }
}

const ids = {};
for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
  const [, tag, attrs] = match;
  const name = attrs.match(/\bid="([^"]+)"/i)?.[1];
  if (!name) continue;
  const element = new Element(tag);
  element.className = attrs.match(/\bclass="([^"]*)"/i)?.[1] || '';
  element.type = attrs.match(/\btype="([^"]*)"/i)?.[1] || '';
  ids[name] = element;
}
const createdTags = [];
const document = {
  getElementById(name) { assert.ok(ids[name], `unknown DOM id: ${name}`); return ids[name]; },
  createElement(tag) { createdTags.push(tag.toLowerCase()); return new Element(tag); },
  createElementNS(_namespace, tag) { createdTags.push(tag.toLowerCase()); return new Element(tag); },
};

const idA = 'a'.repeat(32);
const idB = 'b'.repeat(32);
const idBadHash = 'c'.repeat(32);
const unsafe = '<img src=x onerror=alert(1)>';
function config(symbols = ['000001.SZSE', '000002.SZSE']) {
  return {symbols, start: '2024-01-02', end: '2024-01-05', strategy: 'buy_hold', initial_cash: 100000, commission_rate: 0.0003, min_commission: 5, slippage_bps: 10};
}
function record(runId, createdAt, symbols) {
  return {receipt: {run_id: runId, status: 'completed', artifact_integrity: 'not_checked', created_at: createdAt, finished_at: createdAt, frames: 960}, config: config(symbols)};
}
function detail(runId, overrides = {}) {
  const runConfig = overrides.config || config();
  const result = overrides.result || {
    mode: 'RESEARCH_ONLY', paper_admission: false,
    metrics: {ending_equity: 98723.997, total_return: -0.01276003, max_drawdown: -0.025, sharpe: 0.125, total_trades: 2, fees_paid: 11.003},
    data_readiness: {status: 'mismatch', paper_admission: false, by_symbol: {'000001.SZSE': {status: 'mismatch'}, '000002.SZSE': {status: 'unverified'}}},
    equity: [
      {datetime: '2024-01-02 09:31:00', source_datetime: '2024-01-02 09:31:00', cash: 50000, market_value: 50000, equity: 100000},
      {datetime: '2024-01-03 09:31:00', source_datetime: '2024-01-03 09:31:00', cash: 49000, market_value: 51000, equity: 100000},
      {datetime: '2024-01-04 09:31:00', source_datetime: '2024-01-04 09:31:00', cash: 48000, market_value: 50500, equity: 98500},
      {datetime: '2024-01-05 09:31:00', source_datetime: '2024-01-05 09:31:00', cash: Infinity, market_value: 50723.997, equity: 98723.997},
    ],
    positions: {
      '000001.SZSE': {quantity: 2300, avg_cost: 10.01, available_to_sell: 2300, market_value: 49734, lots: [{quantity: 2300}]},
      '000002.SZSE': {quantity: 2300, average_cost: 11.3, available_to_sell: 2200, market_value: 50989.997, lots: [{quantity: 1000}, {quantity: 1300}]},
    },
    trades: [
      {datetime: '2024-01-02 09:31:00', symbol: '000001.SZSE', side: 'buy', qty: 2300, price: 10.01, commission: 5, stamp_duty: 0},
      {datetime: '2024-01-02 09:31:00', symbol: '000002.SZSE', side: 'buy', quantity: 2300, price: 11.3, commission: 5, stamp_duty: 0},
    ],
  };
  return {receipt: {run_id: runId, status: 'completed', artifact_integrity: 'verified', created_at: '2026-11-13T09:00:00'}, config: runConfig, result, data_manifest: {}};
}

const records = [record(idA, '2026-11-13T10:00:00'), record(idB, '2026-11-13T09:59:00')];
for (let i = 0; i < 101; i++) {
  const runId = (i + 1).toString(16).padStart(32, '0');
  if (runId === idA || runId === idB) continue;
  records.push(record(runId, `2026-10-${String((i % 28) + 1).padStart(2, '0')}T09:00:00`));
}
let listFails = false;
let holdId = '';
let releaseHeld = null;
const requestUrls = [];
const payloadById = new Map([[idA, detail(idA)], [idB, detail(idB)]]);
async function fakeFetch(url, ...options) {
  assert.equal(options.length, 0, 'portfolio UI must make GET requests without a write method');
  requestUrls.push(url);
  if (url === '/api/research/portfolios') {
    return {ok: !listFails, status: listFails ? 503 : 200, json: async () => ({runs: records, truncated: true, mode: 'RESEARCH_ONLY', paper_admission: false})};
  }
  const match = url.match(/^\/api\/research\/portfolios\/([a-f0-9]{32})$/);
  assert.ok(match, `unsafe or unexpected API URL: ${url}`);
  const runId = match[1];
  if (runId === holdId) return new Promise(resolve => { releaseHeld = resolve; });
  if (runId === idBadHash) return {ok: false, status: 409, json: async () => ({error: 'integrity mismatch'})};
  const payload = payloadById.get(runId);
  if (!payload) return {ok: false, status: 404, json: async () => ({error: 'not found'})};
  return {ok: true, status: 200, json: async () => payload};
}

const windowListeners = {};
let currentHash = '';
const window = {
  location: {get hash() { return currentHash; }, set hash(value) { currentHash = String(value).startsWith('#') ? String(value) : `#${value}`; }},
  addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); },
};
vm.runInNewContext(scripts[0][1], {document, window, fetch: fakeFetch, Number, String, Math, JSON, Array, Object, Promise, Error, encodeURIComponent, decodeURIComponent, Intl}, {filename: 'portfolio.html'});

(async () => {
  const app = window.PortfolioReport;
  await app.ready;
  assert.equal(ids['run-list'].children.length, 100, 'visible history list is bounded at 100 entries');
  assert.match(ids['list-status'].textContent, /截断/);
  assert.match(ids['list-status'].textContent, /不代表完整历史/);
  assert.match(ids['detail-status'].textContent, /请选择/);
  assert.match(ids['run-list'].textContent,/执行已完成；报告完整性待读取校验/);
  assert.ok(requestUrls.every(url => url === '/api/research/portfolios'), 'initial view reads only the list');

  const beforeUnsafe = requestUrls.length;
  await app.openRun('../../private', true, false);
  assert.equal(requestUrls.length, beforeUnsafe, 'unsafe ids never reach the detail URL');
  currentHash = '#%2e%2e%2fprivate';
  windowListeners.hashchange[0]();
  assert.equal(requestUrls.length, beforeUnsafe, 'unsafe share hashes never reach the API');
  assert.match(ids['detail-status'].textContent, /编号格式无效/);
  currentHash = '';

  const selected = await app.openRun(idA, true, false);
  assert.ok(selected, 'valid 32-hex ids load a detail');
  assert.equal(currentHash, '#'+idA, 'selection writes a shareable safe hash');
  assert.equal(ids['share-link'].href, '#'+idA);
  assert.match(ids['detail-status'].textContent, /未认证/);
  assert.match(ids['readiness-title'].textContent, /不一致/);
  assert.match(ids['readiness-symbols'].textContent, /000001\.SZSE：对照不一致/);
  assert.match(ids['readiness-symbols'].textContent, /000002\.SZSE：未核实/);
  assert.match(ids['config-symbols'].textContent, /000001\.SZSE → 000002\.SZSE/, 'shared portfolio symbol order is preserved');
  assert.match(ids['config-dates'].textContent, /2024-01-02 至 2024-01-05/);
  assert.match(ids['config-cash'].textContent, /100,000\.00 元/);
  assert.match(ids['config-commission'].textContent, /0\.03%/);
  assert.match(ids['config-slippage'].textContent, /10 基点/);
  assert.match(ids['metric-fees'].textContent, /11\.00 元/);
  assert.match(ids['metric-trades'].textContent, /2 笔/);
  assert.equal(ids['positions-body'].children.length, 2);
  assert.match(ids['positions-body'].children[0].textContent, /000001\.SZSE/);
  assert.match(ids['positions-body'].children[1].textContent, /000002\.SZSE/);
  assert.equal(ids['trades-body'].children.length, 2);
  assert.match(ids['trades-body'].children[1].textContent, /000002\.SZSE/, 'trade details include each symbol');
  assert.match(ids['trades-caption'].textContent, /共 2 笔成交/);
  assert.equal(ids['equity-chart'].classList.contains('hidden'), false);
  const chartCircles = ids['equity-chart'].children.filter(node => node.tagName === 'CIRCLE');
  assert.equal(chartCircles.filter(node => node.getAttribute('data-series') === 'equity').length, 4);
  assert.equal(chartCircles.filter(node => node.getAttribute('data-series') === 'cash').length, 3, 'non-finite cash is not plotted');
  assert.ok(ids['equity-chart'].children.some(node => node.tagName === 'TEXT' && node.textContent === '2024-01-02 09:31:00'), 'x-axis uses a real timestamp label');
  assert.doesNotMatch(ids['equity-chart'].textContent, /NaN|Infinity|undefined/);

  assert.match(ids['config-version'].textContent,/旧记录未归档源码/);
  const versionId='2'.repeat(32),versioned=detail(versionId);
  Object.assign(versioned.receipt,{schema_version:2,source_snapshot_status:'archived_and_verified',code_version:'f'.repeat(64)});
  versioned.result.provenance={source_version:'f'.repeat(64)};
  payloadById.set(versionId,versioned);
  await app.openRun(versionId,false,false);
  assert.match(ids['config-version'].textContent,/源码已归档/);
  assert.ok(ids['config-version'].textContent.includes('f'.repeat(64)));
  versioned.result.provenance.source_version='0'.repeat(64);
  await app.openRun(versionId,false,true);
  assert.match(ids['config-version'].textContent,/归档版本不可确认/);

  const malicious = detail(idB, {config: config([unsafe, '000002.SZSE'])});
  malicious.result.positions = {[unsafe]: {quantity: 1, avg_cost: 2, available_to_sell: 1, market_value: 2, lots: []}, '000002.SZSE': {quantity: 0, avg_cost: 0, available_to_sell: 0, market_value: 0, lots: []}};
  malicious.result.trades = [{datetime: '2024-01-05 09:31:00', symbol: unsafe, side: 'buy', qty: 1, price: 2}];
  payloadById.set(idB, malicious);
  const oldDetailPending = (holdId = idA, app.openRun(idA, false, true));
  const newer = await app.openRun(idB, true, false);
  assert.ok(newer);
  assert.match(ids['config-symbols'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(ids['positions-body'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(ids['trades-body'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.ok(!createdTags.includes('img') && !createdTags.includes('script'), 'untrusted text never becomes an HTML element');
  assert.equal(ids['selected-title'].textContent, '组合 '+idB);
  assert.ok(releaseHeld, 'older response was held for the stale-response check');
  releaseHeld({ok: true, status: 200, json: async () => detail(idA)});
  await oldDetailPending;
  holdId = '';
  assert.equal(ids['selected-title'].textContent, '组合 '+idB, 'late responses cannot overwrite a newer selection');
  assert.match(ids['trades-body'].textContent, /<img src=x/, 'stale response cannot replace the selected trade rows');

  const badHashRequestCount = requestUrls.length;
  await app.openRun(idBadHash, false, false);
  assert.equal(requestUrls.length, badHashRequestCount + 1);
  assert.match(ids['detail-status'].textContent, /完整性校验未通过/);
  assert.equal(ids['equity-chart'].classList.contains('hidden'), true, 'hash mismatch clears the chart');
  assert.equal(ids['positions-body'].children.length, 1, 'hash mismatch clears old holdings');
  assert.equal(ids['trades-body'].children.length, 1, 'hash mismatch clears old trades');
  assert.doesNotMatch(ids['trades-body'].textContent, /<img src=x/);

  const unchecked=detail('9'.repeat(32));
  unchecked.receipt.artifact_integrity='not_checked';
  payloadById.set(unchecked.receipt.run_id,unchecked);
  await app.openRun(unchecked.receipt.run_id,false,false);
  assert.match(ids['detail-status'].textContent,/完整性尚未通过校验/);
  assert.equal(ids['equity-chart'].classList.contains('hidden'),true);

  const emptyId = 'd'.repeat(32);
  const empty = detail(emptyId);
  empty.result.equity = [];
  empty.result.positions = {};
  empty.result.trades = [];
  payloadById.set(emptyId, empty);
  await app.openRun(emptyId, false, false);
  assert.equal(ids['equity-chart'].classList.contains('hidden'), true, 'empty data does not draw a fake chart');
  assert.match(ids['chart-empty'].textContent, /未绘图/);
  assert.match(ids['trades-caption'].textContent, /共 0 笔成交/);

  const failedId = 'e'.repeat(32);
  const failed = detail(failedId);
  failed.receipt.status = 'failed';
  failed.receipt.error = {message: '价格清单校验不一致'};
  failed.result = null;
  payloadById.set(failedId, failed);
  await app.openRun(failedId, false, false);
  assert.match(ids['detail-status'].textContent, /失败/);
  assert.match(ids['detail-status'].textContent, /价格清单校验不一致/);
  assert.equal(ids['equity-chart'].classList.contains('hidden'), true, 'failed runs do not show result data');

  const runningId = 'f'.repeat(32);
  const running = detail(runningId);
  running.receipt.status = 'running';
  running.receipt.message = '正在等待最后一帧';
  running.result = null;
  payloadById.set(runningId, running);
  await app.openRun(runningId, false, false);
  assert.match(ids['detail-status'].textContent, /运行中/);
  assert.match(ids['detail-status'].textContent, /正在等待最后一帧/);
  assert.equal(ids['positions-body'].children.length, 1, 'running runs do not retain previous holdings');

  const manyId = '1'.repeat(32);
  const many = detail(manyId);
  many.result.trades = Array.from({length: 205}, (_, index) => ({datetime: `2024-01-${String((index % 5) + 1).padStart(2, '0')} 09:31:00`, symbol: '000001.SZSE', side: 'buy', quantity: 100, price: 10}));
  payloadById.set(manyId, many);
  await app.openRun(manyId, false, false);
  assert.equal(ids['trades-body'].children.length, 200, 'trade table displays at most 200 entries');
  assert.match(ids['trades-caption'].textContent, /显示前 200 笔，共 205 笔成交/);

  listFails = true;
  await ids['refresh-btn'].dispatch('click');
  assert.equal(ids['run-list'].children.length, 1, 'failed refresh clears the old list');
  assert.equal(ids['equity-chart'].classList.contains('hidden'), true, 'failed refresh clears the old report');
  assert.equal(ids['positions-body'].children.length, 1);
  assert.equal(ids['trades-body'].children.length, 1);
  assert.match(ids['list-status'].textContent, /读取失败/);
  assert.match(ids['detail-status'].textContent, /旧报告已清除/);
  assert.ok(requestUrls.every(url => url === '/api/research/portfolios' || /^\/api\/research\/portfolios\/[a-f0-9]{32}$/.test(url)));
  process.stdout.write('PASS: Chinese research-only portfolio report, GET-only API, safe hashes/text, stale and failure clearing, ordered holdings, real-time SVG and empty-data guard\n');
})().catch(error => { process.stderr.write(error.stack+'\n'); process.exitCode = 1; });
