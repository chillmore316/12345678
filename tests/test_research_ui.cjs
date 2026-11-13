/* Isolated local-UI/API contract test. No browser profile, network, backtest, provider or ledger. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('live_paper_compare/static/research.html', 'utf8');
assert.match(html, /<html lang="zh-CN">/);
assert.match(html, /RESEARCH_ONLY/);
assert.match(html, /Python 源码请在策略工作台登记，实时模拟盘部署尚未准入/);
assert.match(html, /不得部署到模拟盘或实盘/);
assert.doesNotMatch(html, /<script[^>]+src=/i, 'page must not load remote or external scripts');
assert.doesNotMatch(html, /(?:src|href)=['\"]https?:/i, 'page must not load remote resources');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1, 'expected one self-contained inline script');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.value = '';
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.className = '';
    this.disabled = false;
    this.href = '';
    this.hidden = false;
    this._text = '';
    this.type = '';
    this.form = null;
    this.elements = [];
    this.classList = {
      add: (...names) => { const set = new Set(this.className.split(/\s+/).filter(Boolean)); names.forEach(name => set.add(name)); this.className = [...set].join(' '); },
      remove: (...names) => { const remove = new Set(names); this.className = this.className.split(/\s+/).filter(name => name && !remove.has(name)).join(' '); },
      toggle: (name, force) => {
        const present = this.className.split(/\s+/).includes(name);
        const shouldAdd = force === undefined ? !present : Boolean(force);
        if (shouldAdd) this.classList.add(name); else this.classList.remove(name);
        return shouldAdd;
      },
      contains: name => this.className.split(/\s+/).includes(name),
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  set innerHTML(_) { throw new Error('unsafe HTML insertion is forbidden'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  scrollIntoView() {}
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  async dispatch(type, event = {}) {
    if (this.tagName === 'FORM' && type === 'submit' && !this.checkValidity()) return false;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    event.target ||= this;
    let result;
    for (const callback of this.listeners[type] || []) result = await callback(event);
    return result === undefined ? true : result;
  }
  async click() {
    if (this.tagName === 'BUTTON' && this.type === 'submit' && this.form) return this.form.dispatch('submit');
    return true;
  }
  get validity() {
    const value = String(this.value ?? '').trim();
    const missing = this.attributes.required !== undefined && value === '';
    let underflow = false, overflow = false, stepMismatch = false, badInput = false;
    if (this.type === 'number' && value !== '') {
      const number = Number(value);
      badInput = !Number.isFinite(number);
      const min = this.getAttribute('min'), max = this.getAttribute('max');
      underflow = !badInput && min !== null && number < Number(min);
      overflow = !badInput && max !== null && number > Number(max);
      const stepText = this.getAttribute('step');
      if (!badInput && stepText && stepText !== 'any') {
        const step = decimalScaled(stepText, 10);
        const baseText = min !== null ? min : this.getAttribute('value');
        const base = decimalScaled(baseText === null ? '0' : baseText, 10);
        const current = decimalScaled(value, 10);
        stepMismatch = step === 0n || (current - base) % step !== 0n;
      }
    }
    const valid = !(missing || underflow || overflow || stepMismatch || badInput);
    return {valueMissing: missing, rangeUnderflow: underflow, rangeOverflow: overflow, stepMismatch, badInput, valid};
  }
  checkValidity() {
    if (this.tagName === 'FORM') return this.elements.every(element => element.disabled || element.checkValidity());
    return this.validity.valid;
  }
}

function decimalScaled(text, scale) {
  const match = String(text).match(/^([+-]?)(\d+)(?:\.(\d*))?$/);
  assert.ok(match, 'expected decimal input: ' + text);
  const fraction = (match[3] || '').padEnd(scale, '0');
  assert.ok(fraction.length <= scale, 'decimal precision exceeds test scale: ' + text);
  const magnitude = BigInt(match[2]) * (10n ** BigInt(scale)) + BigInt(fraction || '0');
  return match[1] === '-' ? -magnitude : magnitude;
}
const ids = {};
const inputPattern = /<([a-z][\w-]*)\b([^>]*)>/gi;
for (const match of html.matchAll(inputPattern)) {
  const tag = match[1], attrs = match[2], idMatch = attrs.match(/\bid="([^"]+)"/i);
  if (!idMatch) continue;
  const element = new Element(tag);
  for (const attr of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
    if (attr[1] === idMatch[1]) continue;
    element.attributes[attr[1]] = attr[2] === undefined ? '' : attr[2];
  }
  element.type = element.getAttribute('type') || '';
  if (element.getAttribute('value') !== null) element.value = element.getAttribute('value');
  ids[idMatch[1]] = element;
}
const formMatch = html.match(/<form\b[^>]*id="backtest-form"[^>]*>([\s\S]*?)<\/form>/i);
assert.ok(formMatch, 'backtest form must exist');
ids['backtest-form'].elements = [...formMatch[1].matchAll(inputPattern)]
  .map(match => { const idMatch = match[2].match(/\bid="([^"]+)"/i); return idMatch && ids[idMatch[1]]; })
  .filter(Boolean);
for (const element of ids['backtest-form'].elements) element.form = ids['backtest-form'];
const createdTags = [];
const document = {
  getElementById(name) { assert.ok(ids[name], 'unknown DOM id: ' + name); return ids[name]; },
  createElement(tag) { createdTags.push(String(tag).toLowerCase()); return new Element(tag); },
  createElementNS(_ns, tag) { createdTags.push(String(tag).toLowerCase()); return new Element(tag); },
};

const records = [
  {run_id: 'run-old', experiment_id: 'fixed-batch-id', strategy: 'buy_hold', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-05', initial_cash: 100000, created_at: '2024-01-06T00:00:00Z', status: 'completed', progress: 1, metrics: {total_return: 0.02, max_drawdown: -0.02, trade_count: 1}},
  {run_id: 'run-other', strategy: 'sma_cross', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-05', created_at: '2024-01-07T00:00:00', status: 'completed', metrics: {total_return: 0.01}},
  {run_id: 'run-active', strategy: '<img src=x onerror=alert(1)>', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-05', created_at: '2024-01-08T00:00:00', status: 'running', progress: 50},
];
const detail = {
  record: {...records[0], report_url: 'http://127.0.0.1:8060/research#run-old', config: {strategy: 'buy_hold', params: {target_fraction: 0.5}, cost: {commission_rate: 0.0003}}},
  equity: [
    {date: '2024-01-02', equity: 100000},
    {date: '2024-01-03', equity: 110000},
    {date: '2024-01-04', equity: 98000},
  ],
  orders: [{datetime: '2024-01-02 09:30', order_id: 'o1', symbol: '000001.SZSE', side: 'buy', quantity: 100, price: 10, status: 'rejected', reason: 'one_price_bar', stamp_duty_total: 0}],
  trades: [{signal_datetime: '2024-01-02 09:30', fill_datetime: '2024-01-02 09:31', trade_id: 't1', symbol: '000001.SZSE', direction: 'buy', requested_quantity: 100, filled_quantity: 100, remaining_quantity: 0, average_fill_price: 10, price: 10.0000000000004, notional: 1000.000000000004, stamp_duty: 0.125, commission_total: 5}],
  positions: [{symbol: '000001.SZSE', quantity: 100, avg_cost: 10, market_value: 980}],
  bars: [{datetime: '2024-01-02 09:30', symbol: '000001.SZSE', open: 10, high: 10.1, low: 9.9, close: 10, volume: 1000}],
  logs: '<img src=x onerror=alert(1)>\n回放完成',
  data_manifest: {source: 'local minute fixture', price_adjustment: 'unverified'},
  limitations: ['复权未验证'],
  provenance: {engine: 'bounded-minute-research-v1', dependencies: {pandas: '3.0.5'}},
};
let createdRecord = null;
let cancelled = false;
let rejectNextCreate = false;
let mismatchComparison = false;
let hideComparisonHistory = false;
let verificationOnly = false;
const requests = [];
function currentRecords() {
  const list = hideComparisonHistory ? records.filter(record => record.run_id === 'run-active') : records.slice();
  if (createdRecord) list.unshift({...createdRecord, status: cancelled ? 'cancelled' : createdRecord.status});
  return list;
}
async function fakeFetch(url, options = {}) {
  requests.push({url, options});
  const method = options.method || 'GET';
  if (method === 'GET' && url === '/api/research/health') return {ok: true, status: 200, json: async () => ({message: '测试环境', verification_only: verificationOnly})};
  if (method === 'GET' && url === '/api/research/data') return {ok: true, status: 200, json: async () => ({
    exists: true, inventory_truncated: true,
    months: [{month: '2024-01', file_count: 1}, {month: '2024-03', file_count: 1}],
    examples: [{symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-05', source_run_id: 'run-old'}],
  })};
  if (method === 'GET' && url === '/api/research/runs') return {ok: true, status: 200, json: async () => ({runs: currentRecords()})};
  if (method === 'POST' && url === '/api/research/runs') {
    if (rejectNextCreate) { rejectNextCreate = false; return {ok: false, status: 422, json: async () => ({error: '单次回放最多 31 个自然日。'})}; }
    const config = JSON.parse(options.body);
    createdRecord = {run_id: 'run-new', ...config, created_at: '2024-01-09T00:00:00', status: 'queued', progress: 0};
    return {ok: true, status: 201, json: async () => ({record: createdRecord})};
  }
  const cancelMatch = url.match(/^\/api\/research\/runs\/([^/]+)\/cancel$/);
  if (method === 'POST' && cancelMatch) { cancelled = true; return {ok: true, status: 200, json: async () => ({record: {...createdRecord, status: 'cancelled'}})}; }
  const detailMatch = url.match(/^\/api\/research\/runs\/([^/]+)$/);
  if (method === 'GET' && detailMatch) {
    const runId = decodeURIComponent(detailMatch[1]);
    if (runId === 'run-old') return {ok: true, status: 200, json: async () => detail};
    if (runId === 'run-other') return {ok: true, status: 200, json: async () => ({record: {...records[1], metrics: {daily_sharpe: 1.25}}, equity: [{date: '2024-01-02', balance: 200}, {date: mismatchComparison ? '2024-01-03 09:31' : '2024-01-03', balance: 240}, {date: '2024-01-04', balance: 210}], orders: [], trades: [], positions: [], bars: [], logs: ''})};
    if (runId === 'run-active') return {ok: true, status: 200, json: async () => ({record: records[2], equity: [], orders: [], trades: [], positions: [], bars: [], logs: ''})};
    if (runId === 'run-new') return {ok: true, status: 200, json: async () => ({record: {...createdRecord, status: cancelled ? 'cancelled' : createdRecord.status}, equity: [], orders: [], trades: [], positions: [], bars: [], logs: ''})};
  }
  throw new Error('Unexpected fake API request: ' + method + ' ' + url);
}

const windowListeners = {};
let locationHash = '';
const window = {
  location: {origin: 'http://127.0.0.1', search: '?experiment=fixed-batch-id', get hash() { return locationHash; }, set hash(value) { locationHash = String(value).startsWith('#') ? String(value) : '#' + String(value); (windowListeners.hashchange || []).forEach(callback => callback()); }},
  addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); },
  setInterval() { return 1; },
};
const context = {document, window, fetch: fakeFetch, URL, setTimeout: () => 1, clearTimeout: () => {}, encodeURIComponent, decodeURIComponent, Number, String, Math, JSON, Promise, Object, Array, Error};
vm.runInNewContext(scripts[0][1], context, {filename: 'research.html.inline.js'});

(async () => {
  const app = window.ResearchCenter;
  assert.ok(app, 'page exports its testable controller');
  await app.ready;
  const evidence = ids['selection-evidence'];
  const evidenceLink = ids['selection-report'];
  assert.match(evidence.textContent, /有同标的同日期的已完成报告/);
  assert.equal(evidenceLink.href, '/research#run-old');
  assert.equal(evidenceLink.classList.contains('hidden'), false);
  const evidenceRequestCount = requests.length;
  ids['symbol'].value = '000002.SZSE';
  await ids['symbol'].dispatch('input');
  assert.match(evidence.textContent, /当前摘要没有同标的同日期/);
  assert.equal(evidenceLink.href, '');
  assert.equal(evidenceLink.classList.contains('hidden'), true);
  ids['start'].value = '2024-01-31';
  ids['end'].value = '2024-03-01';
  await ids['end'].dispatch('input');
  assert.match(evidence.textContent, /未扫描完整，尚未看到 2024-02/);
  ids['end'].value = '2026-01-01';
  await ids['end'].dispatch('input');
  assert.match(evidence.textContent, /2025-12-31/);
  assert.equal(requests.length, evidenceRequestCount, 'selection changes do not trigger additional reads');
  ids['symbol'].value = '000001.SZSE';
  ids['start'].value = '2024-01-02';
  ids['end'].value = '2024-01-05';
  await ids['end'].dispatch('input');
  assert.equal(ids['initial-cash'].getAttribute('step'), '0.01');
  assert.equal(ids['commission-rate'].getAttribute('step'), '0.00001');
  assert.equal(ids['initial-cash'].validity.stepMismatch, false, 'default initial cash must pass native number-step validation');
  assert.equal(ids['commission-rate'].validity.stepMismatch, false, 'default commission rate must pass native number-step validation');
  assert.equal(ids['backtest-form'].checkValidity(), true, 'all default form values must be natively submittable');
  assert.equal(app.state.runs.length, 3);
  assert.equal(ids['search-runs'].value, 'fixed-batch-id');
  assert.equal(ids['run-list'].children.length, 1);
  assert.equal(ids['run-list'].children[0].getAttribute('data-run-id'), 'run-old');
  assert.equal(ids['compare-left'].value, 'run-old');
  assert.equal(ids['compare-right'].value, '');
  ids['search-runs'].value = '';
  await app.refreshRuns();
  assert.match(ids['run-list'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(createdTags.includes('img'), false, 'untrusted run labels must be rendered as text');

  await app.openRun('run-old', true);
  assert.equal(app.state.selectedId, 'run-old');
  assert.equal(window.location.hash, '#run-old');
  assert.match(ids['selected-meta'].textContent, /000001\.SZSE/);
  assert.match(ids['selected-meta'].textContent, /2024年01月06日 08:00:00（北京时间）/);
  assert.match(ids['metrics-grid'].textContent, /2\.00%/);
  assert.match(ids['metrics-grid'].textContent, /夏普未提供/);
  await app.openRun('run-other');
  assert.match(ids['metrics-grid'].textContent, /1.25/);
  await app.openRun('run-old');
  assert.equal(ids['equity-chart'].classList.contains('hidden'), false);
  assert.equal(ids['drawdown-chart'].classList.contains('hidden'), false);
  await app.openRun('run-active');
  assert.equal(ids['progress-label'].textContent, '50.0%');
  assert.match(ids['equity-empty'].textContent, /尚未提供有效权益序列/);
  await app.openRun('run-old');
  assert.match(ids['orders-head'].textContent, /订单编号/);
  assert.match(ids['orders-head'].textContent, /印花税合计/);
  assert.match(ids['orders-body'].textContent, /买入/);
  assert.match(ids['orders-body'].textContent, /拒绝/);
  assert.match(ids['orders-body'].textContent, /一字K线，无法成交/);
  assert.match(ids['trades-head'].textContent, /成交编号/);
  assert.match(ids['trades-head'].textContent, /印花税/);
  assert.match(ids['trades-body'].textContent, /买入/);
  assert.match(ids['trades-body'].textContent, /10\.00/);
  assert.doesNotMatch(ids['trades-body'].textContent, /10\.0000000000004/);
  assert.equal(detail.trades[0].price, 10.0000000000004, 'display formatting must not mutate/export-round the raw API value');
  assert.match(ids['bars-body'].textContent, /2024-01-02 09:30/);
  assert.match(ids['logs-view'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(ids['manifest-view'].textContent, /price_adjustment/);
  assert.match(ids['provenance-view'].textContent, /bounded-minute-research-v1/);
  assert.equal(ids['report-link'].href, '/research#run-old');
  assert.equal(ids['export-trades'].href, '/api/research/runs/run-old/trades.csv');
  assert.equal(JSON.stringify(app.helpers.drawdownPoints(app.helpers.normalizeEquity(detail.equity)).map(point => point.value)), JSON.stringify([0, 0, -0.10909090909090913]));
  assert.match(ids['manifest-view'].textContent, /price_adjustment/);
  assert.match(ids['provenance-view'].textContent, /bounded-minute-research-v1/);
  assert.match(ids['limitations-view'].textContent, /复权未验证/);

  ids.strategy.value = 'sma_cross';
  ids.symbol.value = '000001.SZSE';
  ids.start.value = '2024-01-02';
  ids.end.value = '2024-01-05';
  ids['initial-cash'].value = '10000';
  ids['fast-period'].value = '5';
  ids['slow-period'].value = '20';
  ids['commission-rate'].value = '0.0003';
  ids['min-commission'].value = '5';
  ids['slippage-bps'].value = '10';
  const createsBeforeInvalidNativeSubmit = requests.filter(request => request.url === '/api/research/runs' && request.options.method === 'POST').length;
  ids['initial-cash'].value = '10000.001';
  assert.equal(ids['initial-cash'].validity.stepMismatch, true, 'fractional cash outside the declared step must be rejected by native validation');
  assert.equal(await ids['submit-button'].click(), false, 'invalid native form submission must not dispatch the submit handler');
  assert.equal(requests.filter(request => request.url === '/api/research/runs' && request.options.method === 'POST').length, createsBeforeInvalidNativeSubmit, 'invalid native submission must not issue an API request');
  ids['initial-cash'].value = '10000';
  const submitted = await ids['submit-button'].click();
  assert.equal(submitted.run_id, 'run-new');
  const post = requests.find(request => request.url === '/api/research/runs' && request.options.method === 'POST');
  const body = JSON.parse(post.options.body);
  assert.deepEqual(body.params, {fast: 5, slow: 20, target_fraction:0.5});
  assert.equal(body.slippage_bps, 10);
  assert.equal(body.commission_rate, 0.0003);
  assert.equal(body.symbol, '000001.SZSE');
  assert.equal(app.state.selectedId, 'run-new');
  assert.match(ids['submit-status'].textContent, /任务已创建/);

  detail.record.config = {backend:'reference',slippage_bps:0,commission_rate:0.00025,params:{target_fraction:0.5}};
  ids['compare-left'].value = 'run-old';
  ids['compare-right'].value = 'run-other';
  await app.renderComparison();
  assert.match(ids['compare-status'].textContent, /归一化/);
  assert.match(ids['compare-legend'].textContent, /run-old/);
  assert.equal(ids['compare-metrics'].children.length, 14);
  assert.match(ids['compare-metrics'].textContent, /最大回撤/);
  assert.match(ids['compare-metrics'].textContent, /滑点（基点）/);
  assert.match(ids['compare-metrics'].textContent, /target_fraction/);
  assert.deepEqual(ids['compare-metrics'].children.find(row => row.children[0].textContent === '佣金费率（小数）').children.map(cell => cell.textContent),
    ['佣金费率（小数）','0.00025','未提供']);
  assert.match(ids['compare-metrics'].textContent, /未提供/);
  assert.match(ids['compare-metrics'].textContent, /2.00%/);
  assert.equal(ids['compare-chart-svg'].classList.contains('hidden'), false);
  assert.equal(ids['compare-link'].href, '/research?compare_a=run-old&compare_b=run-other#run-old');
  assert.equal(ids['compare-link'].classList.contains('hidden'), false);
  ids['compare-right'].value = 'run-active';
  await ids['compare-right'].dispatch('change');
  assert.equal(ids['compare-link'].classList.contains('hidden'), true);
  assert.equal(ids['compare-chart-svg'].classList.contains('hidden'), true);
  ids['compare-right'].value = 'run-other';
  mismatchComparison = true;
  await app.renderComparison();
  assert.match(ids['compare-status'].textContent, /时间点不完全一致/);
  assert.equal(ids['compare-metrics'].children.length, 0);
  assert.equal(ids['compare-link'].href, '');
  assert.equal(ids['compare-chart-svg'].classList.contains('hidden'), true);
  mismatchComparison = false;

  await app.cancelRun();
  assert.ok(requests.some(request => request.url === '/api/research/runs/run-new/cancel' && request.options.method === 'POST'));
  assert.match(ids['run-list'].textContent, /已取消/);
  assert.equal(ids['cancel-run'].classList.contains('hidden'), true);
  rejectNextCreate = true;
  const rejected = await ids['submit-button'].click();
  assert.equal(rejected, null);
  assert.match(ids['submit-status'].textContent, /单次回放最多 31 个自然日/);

  app.helpers.renderAnalytics({record:{run_id:'a'.repeat(32)},analytics:{status:'available',origin:'derived_read_only',metrics:{closed_pair_count:1,win_rate:null,turnover:3.28,slippage_cash_debit:0},daily_returns:[{date:'2024-01-02',return:-0.02}],yearly_returns:[{period:'2024',partial:true,return:-0.02}],closed_trades:[{buy_datetime:'2024-01-02T10:00:00',sell_datetime:'2024-01-03T10:00:00',quantity:100,net_realized_pnl:-12,return_on_cost:-0.01}],benchmark:{status:'unavailable',reason:'无行情'}},data_readiness:{status:'mismatch',checks:[{title:'<unsafe>',status:'mismatch',message:'价格口径冲突'}]}});
  assert.match(ids['data-readiness-status'].textContent,/不一致/);
  assert.match(ids['data-readiness-checks'].textContent,/<unsafe>/);
  assert.match(ids['analytics-status'].textContent,/只读派生/);
  assert.match(ids['analytics-metrics'].textContent,/未提供/);
  assert.match(ids['analytics-metrics'].textContent,/328.00%/);
  assert.match(ids['yearly_returns-body'].textContent,/2024/);
  assert.doesNotMatch(ids['yearly_returns-body'].textContent,/2,024/);
  assert.match(ids['closed_trades-body'].textContent,/-12/);
  assert.match(ids['closed_trades-head'].textContent,/净盈亏/);
  assert.equal(ids['benchmark-chart'].classList.contains('hidden'),true);
  assert.match(ids['export-daily_returns'].href,/daily_returns.csv$/);

  app.helpers.applyConfig({strategy:'sma_cross',backend:'reference',symbol:'000001.SZSE',start:'2024-01-02',end:'2024-01-05',initial_cash:10000,params:{fast:5,slow:20,target_fraction:0.3},commission_rate:0.00025,min_commission:5,slippage_bps:5},'run-old');
  assert.equal(evidenceLink.href, '/research#run-old');
  assert.equal(app.helpers.readConfig().parent_run_id,'run-old');
  assert.equal(app.helpers.readConfig().params.target_fraction,0.3);
  ids['run-label'].value='中文实验';assert.equal(app.helpers.readConfig().label,'中文实验');
  ids['filter-status'].value='failed';await ids['filter-status'].dispatch('change');assert.doesNotMatch(ids['run-list'].textContent,/已完成/);
  ids['filter-status'].value='';await ids['filter-status'].dispatch('change');

  hideComparisonHistory = true;
  verificationOnly = true;
  window.location.search = '?compare_a=run-old&compare_b=run-other';
  app.init();
  await app.ready;
  assert.match(ids['research-mode'].textContent, /备份验证/);
  assert.ok(app.state.runs.every(record => !['run-old', 'run-other'].includes(record.run_id)));
  assert.equal(ids['compare-left'].value, 'run-old');
  assert.equal(ids['compare-right'].value, 'run-other');
  assert.match(ids['compare-status'].textContent, /归一化/);
  assert.equal(ids['compare-chart-svg'].classList.contains('hidden'), false);
  await app.refreshRuns();
  assert.equal(ids['compare-left'].value, 'run-old');
  assert.equal(ids['compare-right'].value, 'run-other');
  assert.ok(ids['compare-left'].children.some(option => option.value === 'run-old'));

  process.stdout.write('PASS: local-only API contract, native form validity and step checks, safe text rendering, async runs, form submission, API validation, metrics, curves, CSV/report links, aligned comparison and cancellation\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
