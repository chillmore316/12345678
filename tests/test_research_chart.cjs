/* Synthetic DOM/API test for the local research page's real-bar chart contract. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('live_paper_compare/static/research.html', 'utf8');
assert.doesNotMatch(html, /<script[^>]+src=/i, 'chart must not load an external script');
assert.doesNotMatch(html, /(?:src|href)=["']https?:/i, 'chart must not load remote resources');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1, 'expected the self-contained page script');

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
    this._text = '';
    this.type = '';
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
    return true;
  }
}

const ids = {};
const createdTags = [];
for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
  const [, tag, attrs] = match;
  const id = attrs.match(/\bid="([^"]+)"/i)?.[1];
  if (!id) continue;
  const element = new Element(tag);
  for (const attr of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
    if (attr[1] !== 'id') element.attributes[attr[1]] = attr[2] === undefined ? '' : attr[2];
  }
  element.type = element.getAttribute('type') || '';
  if (element.getAttribute('value') !== null) element.value = element.getAttribute('value');
  ids[id] = element;
}
const document = {
  getElementById(name) { assert.ok(ids[name], `unknown DOM id: ${name}`); return ids[name]; },
  createElement(tag) { createdTags.push(String(tag).toLowerCase()); return new Element(tag); },
  createElementNS(_namespace, tag) { createdTags.push(String(tag).toLowerCase()); return new Element(tag); },
};

function minuteTime(day, index) {
  const minute = 9 * 60 + 30 + index;
  return `${day} ${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}:00`;
}
const bars = [];
for (let index = 0; index < 130; index += 1) {
  const open = 10 + index * 0.001;
  const close = open + 0.01;
  bars.push({source_datetime: minuteTime('2024-01-02', index), datetime: `bar-start-${index}`, open, high: close + 0.005, low: open - 0.005, close, volume: 1000});
}
for (let index = 0; index < 3; index += 1) {
  const open = 11 + index * 0.01;
  bars.push({source_datetime: minuteTime('2024-01-03', index), open, high: open + 0.02, low: open - 0.01, close: open + 0.01});
}
const trades = [
  {signal_datetime: '2024-01-02T09:29:00', fill_datetime: '2024-01-02T09:30:00', trade_id: 'exact', symbol: '000001.SZSE', direction: 'buy', filled_quantity: 100, average_fill_price: 10.01},
  {signal_datetime: '2024-01-02 09:30:00', fill_datetime: '2024-01-02 09:30:30', trade_id: 'between-bars', symbol: '000001.SZSE', direction: 'sell', quantity: 100, price: 10.05},
];
const records = [
  {run_id: 'run-chart', strategy: 'buy_hold', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-03', created_at: '2024-01-04', status: 'completed', backend: 'reference', strategy_version: 'buy_hold-v1', experiment_id: 'exp-1'},
  {run_id: 'run-vnpy', strategy: 'buy_hold', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-03', created_at: '2024-01-05', status: 'completed', backend: 'vnpy'},
];
const detail = {
  record: {...records[0], config: {backend: 'reference', cost: {commission_rate: 0.0003}}},
  equity: [{date: '2024-01-02', equity: 100}, {date: '2024-01-03', equity: 101}],
  orders: [
    {datetime: '2024-01-02 09:30', order_id: 'rejected-known', side: 'buy', status: 'rejected', reason: 'one_price_bar'},
    {datetime: '2024-01-02 09:31', order_id: 'untrusted', side: 'buy', status: 'rejected', reason: '<img src=x onerror=alert(1)>'},
  ],
  trades,
  positions: [],
  bars,
};
let createdConfig = null;
const requests = [];
async function fakeFetch(url, options = {}) {
  requests.push({url, options});
  if (url === '/api/research/strategies') return {ok: true, status: 200, json: async () => ({
    strategies: [
      {id: 'buy_hold', name: '买入并持有', version: '1.0', backends: ['reference', 'vnpy'], description: '有限描述 <img>'},
      {id: 'sma_cross', name: '双均线交叉', version: '1.0', backends: ['reference'], description: '仅参考后端'},
      {id: 'untrusted_upload', name: '<img src=x>', backends: ['reference', 'vnpy']},
    ],
    backends: [{id: 'reference'}, {id: 'vnpy'}],
    limits: {strategies: ['buy_hold', 'sma_cross']},
  })};
  if (url === '/api/research/runs' && !options.method) return {ok: true, status: 200, json: async () => ({runs: records})};
  if (url === '/api/research/runs' && options.method === 'POST') {
    createdConfig = JSON.parse(options.body);
    const record = {run_id: 'run-created', ...createdConfig, status: 'queued'};
    return {ok: true, status: 201, json: async () => ({record})};
  }
  const match = url.match(/^\/api\/research\/runs\/([^/]+)$/);
  if (match) {
    const runId = decodeURIComponent(match[1]);
    if (runId === 'run-chart') return {ok: true, status: 200, json: async () => detail};
    if (runId === 'run-vnpy') return {ok: true, status: 200, json: async () => ({record: records[1], equity: detail.equity, orders: [], trades: [], positions: [], bars: []})};
    if (runId === 'run-created') return {ok: true, status: 200, json: async () => ({record: {run_id: runId, ...createdConfig, status: 'queued'}, equity: [], orders: [], trades: [], positions: [], bars: []})};
  }
  throw new Error(`Unexpected API request: ${options.method || 'GET'} ${url}`);
}
const windowListeners = {};
let hash = '';
const window = {
  location: {origin: 'http://127.0.0.1', get hash() { return hash; }, set hash(value) { hash = String(value).startsWith('#') ? String(value) : `#${value}`; }},
  addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); },
  setInterval() { return 1; },
};
const context = {document, window, fetch: fakeFetch, URL, setTimeout: () => 1, clearTimeout: () => {}, encodeURIComponent, decodeURIComponent, Number, String, Math, JSON, Promise, Object, Array, Error, Date};
vm.runInNewContext(scripts[0][1], context, {filename: 'research.html.chart.js'});

function chartCandles() { return ids['kline-chart'].children.filter(node => node.tagName === 'RECT' && node.getAttribute('data-source-datetime')); }
function tradeMarkers() { return ids['kline-chart'].children.filter(node => node.tagName === 'CIRCLE' && node.getAttribute('data-trade-index') !== null); }

(async () => {
  const app = window.ResearchCenter;
  await app.ready;
  assert.equal(app.state.strategyCatalogAvailable, true);
  assert.deepEqual(ids.strategy.children.map(option => option.value), ['buy_hold', 'sma_cross'], 'main form lists built-in strategies');
  assert.match(ids['strategy-catalog-note'].textContent, /本页表单仅展示内置策略.*Python 源码请使用顶部的策略工作台/);
  assert.match(html, /href="\/python-strategy"/);
  assert.equal(ids.backend.value, 'reference');
  ids.strategy.value = 'sma_cross';
  await ids.strategy.dispatch('change');
  assert.deepEqual(ids.backend.children.map(option => option.value), ['reference'], 'VeighNa is only offered for buy_hold');
  ids.strategy.value = 'buy_hold';
  await ids.strategy.dispatch('change');
  assert.deepEqual(ids.backend.children.map(option => option.value), ['reference', 'vnpy']);

  await app.openRun('run-chart');
  assert.match(ids['selected-meta'].textContent, /后端：保守分钟回放/);
  assert.match(ids['selected-meta'].textContent, /策略版本：buy_hold-v1/);
  assert.match(ids['selected-meta'].textContent, /实验编号：exp-1/);
  assert.equal(ids['kline-day'].value, '2024-01-03', 'latest returned trading day is selected by default');
  assert.equal(chartCandles().length, 3, 'candles are drawn from actual OHLC rows on the selected day');
  detail.record.metrics = {total_return: 1.5};
  await app.openRun('run-chart');
  assert.match(ids['metrics-grid'].textContent, /150\.00%/, 'fractional returns above 100 percent must retain their unit');
  delete detail.record.metrics;
  assert.equal(app.helpers.resolveTradeBar({...trades[0], symbol: '600000.SSE'}, app.state.chart.bars).status, 'missing', 'different securities must never share a candle marker');

  ids['kline-day'].value = '2024-01-02';
  await ids['kline-day'].dispatch('change');
  ids['kline-window'].value = '120';
  await ids['kline-window'].dispatch('change');
  assert.equal(chartCandles().length, 120, 'the selected window is capped at 120 real bars');
  const exactRow = ids['trades-body'].children[0];
  await exactRow.dispatch('click');
  assert.equal(app.state.chart.anchorTime, '2024-01-02 09:30:00');
  assert.equal(ids['kline-day'].value, '2024-01-02');
  assert.ok(tradeMarkers().some(marker => marker.getAttribute('data-trade-index') === '0' && marker.getAttribute('data-source-datetime') === '2024-01-02 09:30:00'), 'fill is plotted only at its exact source bar');
  assert.match(ids['trade-locator-detail'].textContent, /信号时间：2024-01-02T09:29:00/);
  assert.match(ids['trade-locator-detail'].textContent, /成交时间：2024-01-02T09:30:00/);
  assert.match(ids['trade-locator-detail'].textContent, /价格：10\.01/);
  assert.match(ids['trade-locator-detail'].textContent, /方向：买入/);
  assert.match(ids['trade-locator-detail'].textContent, /数量：100/);

  await ids['trades-body'].children[1].children.at(-1).children[0].dispatch('click');
  assert.match(ids['trade-locator-detail'].textContent, /未找到相同 source_datetime/);
  assert.equal(app.state.chart.windowTime, '2024-01-02 09:30:30', 'an unmatched trade selects its time window without plotting a point');
  assert.match(ids['kline-status'].textContent, /窗口按原成交时间范围展示/);
  assert.doesNotMatch(tradeMarkers().map(marker => marker.getAttribute('data-trade-index')).join(','), /1/, 'a fill between bars is not snapped to the nearest candle');
  assert.match(ids['orders-body'].textContent, /一字K线，无法成交/);
  assert.match(ids['orders-body'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(createdTags.includes('img'), false, 'untrusted reasons/descriptions are inserted as text only');

  await app.openRun('run-vnpy');
  assert.equal(ids['kline-chart'].classList.contains('hidden'), true, 'missing bars do not produce a synthetic candle chart');
  assert.match(ids['kline-empty'].textContent, /后端未返回分钟行情/);
  ids['compare-left'].value = 'run-chart';
  ids['compare-right'].value = 'run-vnpy';
  await app.renderComparison();
  assert.equal(ids['compare-chart-svg'].classList.contains('hidden'), false, 'different backends remain comparable');
  assert.match(ids['compare-status'].textContent, /撮合口径不同，收益差异不能视为策略优劣/);

  assert.equal(app.helpers.readConfig().backend, 'reference');
  app.state.selectedId = '';
  const submitted = await app.submitForm({preventDefault() {}});
  assert.ok(submitted);
  assert.equal(createdConfig.backend, 'reference');
  assert.equal(createdConfig.commission_rate, 0.0003, 'top-level cost fields remain compatible');
  assert.ok(requests.some(request => request.url === '/api/research/strategies'));
  process.stdout.write('PASS: allowlisted strategy registry, backend restrictions, native SVG OHLC candles, <=120 real-bar window, exact fill matching, no nearest-bar relocation, safe text, identity metadata, and backend comparison warning\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
