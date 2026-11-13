/* Synthetic DOM/API coverage for the local report's P1 chart and filter contracts. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('live_paper_compare/static/research.html', 'utf8');
assert.doesNotMatch(html, /<script[^>]+src=/i, 'report must remain self-contained');
assert.doesNotMatch(html, /(?:src|href)=["']https?:/i, 'report must not load remote resources');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1, 'expected one self-contained page script');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.value = '';
    this.checked = false;
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
    event.stopPropagation ||= () => {};
    event.target ||= this;
    for (const callback of this.listeners[type] || []) await callback(event);
    return true;
  }
}

const ids = {};
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
  createElement(tag) { return new Element(tag); },
  createElementNS(_namespace, tag) { return new Element(tag); },
};

const bar = (source, close) => ({
  source_datetime: source,
  open: close,
  high: close + 1,
  low: close - 1,
  close,
});
const records = [
  {run_id: 'run-main', strategy: 'buy_hold', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-03', created_at: '2024-01-04', status: 'running', progress: 50, backend: 'reference', config: {strategy: 'buy_hold', backend: 'reference'}, metrics: {total_return: 0.1, trade_count: 5}},
  {run_id: 'run-empty', strategy: 'buy_hold', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-03', created_at: '2024-01-03', status: 'completed', backend: 'reference', config: {strategy: 'buy_hold', backend: 'reference'}, metrics: {}},
  {run_id: 'run-single', strategy: 'buy_hold', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-02', created_at: '2024-01-02', status: 'completed', backend: 'reference', config: {strategy: 'buy_hold', backend: 'reference'}, metrics: {}},
  {run_id: 'run-sma', strategy: 'sma_cross', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-04', created_at: '2024-01-05', status: 'completed', backend: 'reference', config: {strategy: 'sma_cross', backend: 'reference', params: {fast: 2, slow: 3}}, metrics: {}},
  {run_id: 'run-sma-invalid', strategy: 'sma_cross', symbol: '000001.SZSE', start: '2024-01-02', end: '2024-01-04', created_at: '2024-01-06', status: 'completed', backend: 'reference', config: {strategy: 'sma_cross', backend: 'reference', params: {fast: 8, slow: 3}}, metrics: {}},
];
const dailyReturns = [
  {date: '2024-01-01', return: -0.11},
  {date: '2024-01-02', return: -0.10},
  {date: '2024-01-03', return: -0.05},
  {date: '2024-01-04', return: -0.02},
  {date: '2024-01-05', return: 0},
  {date: '2024-01-06', return: 0.02},
  {date: '2024-01-07', return: 0.05},
  {date: '2024-01-08', return: 0.10},
  {date: '2024-01-09', return: 0.1000001},
  {date: '2024-01-10', return: null},
  {date: '2024-01-11'},
  {date: '2024-01-12', return: '0.05'},
  {date: '2024-01-13', return: NaN},
];
const trades = [
  {fill_datetime: '2024-01-02T09:30:00', datetime: '2024-01-02T09:30:00', trade_id: 'exact-buy', symbol: '000001.SZSE', side: 'buy', quantity: 100, price: 10},
  {fill_datetime: '2024-01-02 09:31:00', datetime: '2024-01-02 09:31:00', trade_id: 'same-day-sell', symbol: '000001.SZSE', side: 'sell', quantity: 100, price: 11},
  {fill_datetime: '2024-01-03 09:30:00', datetime: '2024-01-03 09:30:00', trade_id: 'next-day-buy', symbol: '000001.SZSE', direction: 'buy', quantity: 100, price: 12},
  {datetime: '2024-01-03 09:31:00', trade_id: 'fallback-sell', symbol: '000001.SZSE', side: 'sell', quantity: 100, price: 13},
  {fill_datetime: 'not-a-time', datetime: '2024-01-03 09:31:00', trade_id: 'invalid-fill', symbol: '000001.SZSE', side: 'sell', quantity: 100, price: 14},
];
const mainDetail = {
  record: records[0],
  equity: [
    {datetime: '2024-01-02 00:00:00', source_datetime: null, cash: 1000, quantity: 0, equity: 1000},
    {datetime: '2024-01-02 09:30:00', source_datetime: '2024-01-02 09:30:00', cash: 900, quantity: 10, equity: 1000},
    {datetime: '2024-01-02 09:31:00', source_datetime: '2024-01-02 09:31:00', cash: 0, quantity: 10, equity: 100},
    {datetime: '2024-01-03 09:30:00', source_datetime: '2024-01-03 09:30:00', cash: -5, quantity: 10, equity: 95},
    {datetime: '2024-01-03 09:31:00', source_datetime: '2024-01-03 09:31:00', cash: null, quantity: 10, equity: 95, market_value: 95},
    {datetime: '2024-01-03 09:32:00', source_datetime: '2024-01-03 09:32:00', cash: '', quantity: 10, equity: 95, market_value: '95'},
    {datetime: '2024-01-03 09:33:00', source_datetime: '2024-01-03 09:33:00', cash: true, quantity: 10, equity: 95, market_value: []},
    {datetime: '2024-01-03 09:34:00', source_datetime: '2024-01-03 09:34:00', cash: {}, quantity: 10, equity: 95, market_value: NaN},
    {datetime: '2024-01-03 09:35:00', source_datetime: '2024-01-03 09:35:00', cash: NaN, quantity: 10, equity: 95, market_value: null},
  ],
  analytics: {status: 'available', origin: 'saved_with_run', metrics: {closed_pair_count: 1, win_rate: 1}, daily_returns: dailyReturns, monthly_returns: [], yearly_returns: [], closed_trades: [], benchmark: {status: 'unavailable', points: []}, methodology: {}},
  data_readiness: {status: 'unverified', checks: []},
  orders: [],
  trades,
  positions: [],
  bars: [bar('2024-01-02 09:30:00', 10), bar('2024-01-02 09:31:00', 11), bar('2024-01-03 09:30:00', 12), bar('2024-01-03 09:31:00', 13)],
};
const emptyDetail = {record: records[1], equity: [], analytics: {status: 'available', metrics: {}, daily_returns: [], monthly_returns: [], yearly_returns: [], closed_trades: [], benchmark: {status: 'unavailable', points: []}}, orders: [], trades: [], positions: [], bars: []};
const singleDetail = {record: records[2], equity: [{datetime: '2024-01-02 09:30:00', source_datetime: '2024-01-02 09:30:00', cash: 0, quantity: 0, equity: 0}], analytics: {status: 'available', metrics: {}, daily_returns: [{date: '2024-01-02', return: 0}], monthly_returns: [], yearly_returns: [], closed_trades: [], benchmark: {status: 'unavailable', points: []}}, orders: [], trades: [], positions: [], bars: []};
const smaBars = [bar('2024-01-02 09:30:00', 10), bar('2024-01-02 09:31:00', 20), bar('2024-01-03 09:30:00', 30), bar('2024-01-03 09:31:00', 40), bar('2024-01-04 09:30:00', 50)];
const smaDetail = {record: records[3], equity: [], analytics: {status: 'available', metrics: {}, daily_returns: [], monthly_returns: [], yearly_returns: [], closed_trades: [], benchmark: {status: 'unavailable', points: []}}, orders: [], trades: [], positions: [], bars: smaBars};
const invalidSmaDetail = {record: records[4], equity: [], analytics: {status: 'available', metrics: {}, daily_returns: [], monthly_returns: [], yearly_returns: [], closed_trades: [], benchmark: {status: 'unavailable', points: []}}, orders: [], trades: [], positions: [], bars: smaBars};
const payloadByRun = {'run-main': mainDetail, 'run-empty': emptyDetail, 'run-single': singleDetail, 'run-sma': smaDetail, 'run-sma-invalid': invalidSmaDetail};
const requests = [];
async function fakeFetch(url, options = {}) {
  requests.push({url, options});
  if (url === '/api/research/strategies') return {ok: true, status: 200, json: async () => ({strategies: [{id: 'buy_hold', name: '买入并持有', backends: ['reference']}, {id: 'sma_cross', name: '双均线交叉', backends: ['reference']}], backends: [{id: 'reference'}]})};
  if (url === '/api/research/runs' && !options.method) return {ok: true, status: 200, json: async () => ({runs: records})};
  if (url === '/api/research/health') return {ok: true, status: 200, json: async () => ({message: '本地测试'})};
  if (url === '/api/research/data') return {ok: true, status: 200, json: async () => ({exists: false, months: [], examples: []})};
  const match = url.match(/^\/api\/research\/runs\/([^/]+)$/);
  if (match) {
    const runId = decodeURIComponent(match[1]);
    if (runId === 'run-fail') return {ok: false, status: 500, json: async () => ({error: {message: 'offline'}})};
    if (payloadByRun[runId]) return {ok: true, status: 200, json: async () => payloadByRun[runId]};
  }
  throw new Error(`Unexpected API request: ${options.method || 'GET'} ${url}`);
}
const windowListeners = {};
let intervalCallback = null;
let hash = '';
const window = {
  location: {origin: 'http://127.0.0.1', get hash() { return hash; }, set hash(value) { hash = String(value).startsWith('#') ? String(value) : `#${value}`; }},
  addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); },
  setInterval(callback) { intervalCallback = callback; return 1; },
};
const context = {document, window, fetch: fakeFetch, URL, Intl, setTimeout: () => 1, clearTimeout: () => {}, encodeURIComponent, decodeURIComponent, Number, String, Math, JSON, Promise, Object, Array, Error, Date};
vm.runInNewContext(scripts[0][1], context, {filename: 'research.html.report-views.js'});

function nodesWithAttribute(element, tag, attribute) {
  return element.children.filter(node => node.tagName === tag.toUpperCase() && node.getAttribute(attribute) !== null);
}
function histogramBars() { return nodesWithAttribute(ids['daily-return-histogram'], 'rect', 'data-bin-index'); }
function seriesPoints(series) { return ids['account-components-chart'].children.filter(node => node.tagName === 'CIRCLE' && node.getAttribute('data-series') === series); }
function smaPoints(period) { return ids['kline-chart'].children.filter(node => node.tagName === 'CIRCLE' && node.getAttribute('data-sma-period') === String(period)); }

(async () => {
  const app = window.ResearchCenter;
  await app.ready;

  const originalEquity = JSON.stringify(mainDetail.equity);
  const originalAnalytics = JSON.stringify(mainDetail.analytics);
  await app.openRun('run-main');
  assert.equal(ids['account-components-chart'].classList.contains('hidden'), false, 'cash and market value chart renders actual points');
  assert.equal(seriesPoints('cash').length, 3, 'finite non-negative cash values include zero, while negative and malformed values are excluded');
  assert.equal(seriesPoints('market').length, 4, 'market value uses explicit same-point field or same-point equity minus cash');
  assert.ok(seriesPoints('cash').some(point => point.getAttribute('data-value') === '0'), 'zero cash is plotted as an observed value');
  assert.ok(seriesPoints('market').some(point => point.getAttribute('data-value') === '0'), 'zero market value is plotted as an observed value');
  assert.ok(!seriesPoints('cash').some(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:31:00'), 'missing cash is not filled with zero');
  assert.ok(!seriesPoints('cash').some(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:30:00'), 'unsupported negative cash is excluded');
  assert.match(ids['account-components-status'].textContent, /负现金 1 点/);
  assert.match(ids['account-components-status'].textContent, /同一时点账户权益减现金/);

  const bars = histogramBars();
  assert.equal(bars.length, 9, 'daily return histogram uses nine fixed interpretable bins');
  assert.deepEqual(bars.map(node => Number(node.getAttribute('data-count'))), [1, 1, 1, 1, 1, 1, 1, 1, 1], 'negative, zero, positive, and exact threshold boundaries land in their stated bins');
  assert.deepEqual(bars.map(node => Number(node.getAttribute('data-cumulative'))), [1, 2, 3, 4, 5, 6, 7, 8, 9], 'bars report cumulative sample counts');
  assert.match(ids['daily-return-histogram-status'].textContent, /有效日净收益样本：9；排除缺失、非有限或非数值记录：4/);
  assert.equal(ids['export-trades'].href, '/api/research/runs/run-main/trades.csv', 'trade export remains the unfiltered raw CSV route');
  assert.equal(JSON.stringify(mainDetail.equity), originalEquity, 'rendering does not modify the raw equity payload');
  assert.equal(JSON.stringify(mainDetail.analytics), originalAnalytics, 'rendering does not modify analytics.daily_returns');

  const analyticsMetricText = ids['analytics-metrics'].textContent;
  ids['trade-filter-date'].value = '2024-01-02';
  ids['trade-filter-direction'].value = 'buy';
  await ids['trade-filter-date'].dispatch('change');
  await ids['trade-filter-direction'].dispatch('change');
  assert.equal(ids['trades-body'].children.length, 1, 'date and direction filters apply to visible trade rows');
  assert.equal(ids['trades-body'].children[0].getAttribute('data-trade-index'), '0', 'filtered row retains its original API index');
  await ids['trades-body'].children[0].dispatch('click');
  assert.equal(app.state.chart.selectedTradeIndex, 0);
  assert.ok(ids['kline-chart'].children.some(node => node.tagName === 'CIRCLE' && node.getAttribute('data-trade-index') === '0' && node.getAttribute('data-source-datetime') === '2024-01-02 09:30:00'), 'selection keeps exact source_datetime matching');
  ids['trade-filter-date'].value = '2024-01-03';
  ids['trade-filter-direction'].value = 'sell';
  await ids['trade-filter-date'].dispatch('change');
  await ids['trade-filter-direction'].dispatch('change');
  assert.equal(ids['trades-body'].children[0].getAttribute('data-trade-index'), '3', 'date fallback to raw datetime keeps the original trade index');
  assert.equal(app.state.chart.selectedTradeIndex, 0, 'filter changes do not change K-line selection');
  assert.ok(ids['kline-chart'].children.some(node => node.tagName === 'CIRCLE' && node.getAttribute('data-trade-index') === '0' && node.getAttribute('data-source-datetime') === '2024-01-02 09:30:00'), 'filter changes leave the exact K-line marker in place');
  assert.equal(ids['analytics-metrics'].textContent, analyticsMetricText, 'filtering does not recalculate or alter report metrics');
  assert.equal(ids['export-trades'].href, '/api/research/runs/run-main/trades.csv', 'filtering does not alter raw CSV export');
  await ids['trades-body'].children[0].dispatch('click');
  assert.equal(app.state.chart.selectedTradeIndex, 3);
  assert.ok(!ids['kline-chart'].classList.contains('hidden'), 'a datetime-only legacy fill remains locatable');
  assert.ok(ids['kline-chart'].children.some(node => node.tagName === 'CIRCLE' && node.getAttribute('data-trade-index') === '3' && node.getAttribute('data-source-datetime') === '2024-01-03 09:31:00'), 'record-time fallback still requires an exact unique source bar');
  assert.match(ids['trade-locator-detail'].textContent, /2024-01-03 09:31:00（记录时间）/);
  assert.equal(app.helpers.resolveTradeBar(trades[4], app.state.chart.bars).status, 'invalid', 'explicit malformed fill time must not fall back to a different timestamp');

  ids['trade-filter-direction'].value = 'buy';
  await ids['trade-filter-direction'].dispatch('change');
  assert.equal(ids['trades-body'].children[0].getAttribute('data-trade-index'), '2');
  await ids['trades-body'].children[0].dispatch('click');
  assert.ok(ids['trades-body'].children[0].classList.contains('trade-selected'), 'a filtered original index must highlight its displayed row');
  assert.equal(app.state.chart.selectedTradeIndex, 2);
  assert.ok(ids['kline-chart'].children.some(node => node.tagName === 'CIRCLE' && node.getAttribute('data-trade-index') === '2' && node.getAttribute('data-source-datetime') === '2024-01-03 09:30:00'));

  ids['trade-filter-date'].value = '2024-01-02';
  ids['trade-filter-direction'].value = 'buy';
  assert.equal(typeof intervalCallback, 'function', 'page schedules its regular refresh');
  intervalCallback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ids['trade-filter-date'].value, '2024-01-02', 'automatic same-run refresh retains the date filter');
  assert.equal(ids['trade-filter-direction'].value, 'buy', 'automatic same-run refresh retains the direction filter');
  assert.equal(ids['trades-body'].children[0].getAttribute('data-trade-index'), '0');

  await app.openRun('run-single');
  assert.equal(ids['trade-filter-date'].value, '', 'switching runs resets the date filter');
  assert.equal(ids['trade-filter-direction'].value, '', 'switching runs resets the direction filter');
  assert.equal(seriesPoints('cash').length, 1, 'a single actual point is supported');
  assert.equal(seriesPoints('cash')[0].getAttribute('data-value'), '0', 'single zero point remains visible');
  assert.equal(seriesPoints('market').length, 1, 'single-point zero market value remains visible');
  assert.equal(histogramBars().length, 9, 'a zero-only sample still renders fixed histogram bins');
  assert.deepEqual(histogramBars().map(node => Number(node.getAttribute('data-count'))), [0, 0, 0, 0, 1, 0, 0, 0, 0], 'all-zero returns are counted in the exact zero bin');

  await app.openRun('run-empty');
  assert.equal(ids['account-components-chart'].classList.contains('hidden'), true, 'empty equity data does not invent account component points');
  assert.match(ids['account-components-empty'].textContent, /后端未返回实际权益点/);
  assert.equal(ids['daily-return-histogram'].classList.contains('hidden'), true, 'empty daily_returns does not produce a histogram');
  assert.match(ids['daily-return-histogram-empty'].textContent, /没有有效的日净收益样本/);

  ids['fast-period'].value = '8';
  ids['slow-period'].value = '16';
  await app.openRun('run-sma');
  assert.equal(ids['sma-overlay-toggle'].checked, false, 'SMA overlays default off for a new run');
  assert.equal(ids['sma-overlay-toggle'].disabled, false, 'valid frozen SMA parameters enable the optional overlay');
  assert.match(ids['sma-overlay-status'].textContent, /SMA\(2\) \/ SMA\(3\)/, 'overlay parameters come from this run’s frozen config');
  assert.equal(smaPoints(2).length + smaPoints(3).length, 0, 'no SMA points draw while the overlay is off');
  ids['kline-day'].value = '2024-01-03';
  await ids['kline-day'].dispatch('change');
  ids['sma-overlay-toggle'].checked = true;
  await ids['sma-overlay-toggle'].dispatch('change');
  assert.equal(smaPoints(2).find(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:30:00').getAttribute('data-sma-value'), '25', 'SMA(2) includes the prior day’s last actual close');
  assert.equal(smaPoints(3).find(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:30:00').getAttribute('data-sma-value'), '20', 'SMA(3) is computed over the full returned history before day filtering');
  assert.equal(smaPoints(2).find(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:31:00').getAttribute('data-sma-value'), '35');
  assert.equal(smaPoints(3).find(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:31:00').getAttribute('data-sma-value'), '30');
  ids['kline-window'].value = '30';
  await ids['kline-window'].dispatch('change');
  assert.equal(smaPoints(3).find(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:30:00').getAttribute('data-sma-value'), '20', 'changing the visible window keeps full-history SMA values');
  smaBars[4].open = 1000; smaBars[4].high = 1001; smaBars[4].low = 999; smaBars[4].close = 1000;
  await app.openRun('run-sma', false);
  assert.equal(ids['sma-overlay-toggle'].checked, true, 'same-run refresh retains the user’s overlay selection');
  assert.equal(smaPoints(3).find(point => point.getAttribute('data-source-datetime') === '2024-01-03 09:30:00').getAttribute('data-sma-value'), '20', 'future close changes do not alter earlier SMA values');
  ids['kline-day'].value = '2024-01-02';
  await ids['kline-day'].dispatch('change');
  assert.equal(smaPoints(2).length, 1, 'only the second bar has a complete SMA(2) window on the first day');
  assert.equal(smaPoints(3).length, 0, 'an incomplete SMA(3) window is left blank');

  await app.openRun('run-main');
  assert.equal(ids['sma-overlay-toggle'].checked, false, 'switching runs resets the optional overlay');
  assert.equal(ids['sma-overlay-toggle'].disabled, true, 'buy_hold does not offer SMA overlays');
  assert.equal(ids['sma-overlay-status'].textContent.includes('仅双均线策略适用'), true);
  await app.openRun('run-sma-invalid');
  assert.equal(ids['sma-overlay-toggle'].disabled, true, 'invalid frozen fast/slow parameters disable the overlay');
  assert.match(ids['sma-overlay-status'].textContent, /没有有效的已保存快慢窗口参数/);
  assert.equal(smaPoints(2).length + smaPoints(3).length, 0, 'invalid parameters do not reuse values from the current form');
  await app.openRun('run-fail');
  assert.equal(ids['account-components-chart'].classList.contains('hidden'), true, 'failed detail clears the component chart');
  assert.equal(ids['daily-return-histogram'].classList.contains('hidden'), true, 'failed detail clears the daily return histogram');
  assert.match(ids['account-components-empty'].textContent, /后端未返回实际权益点/);
  assert.match(ids['daily-return-histogram-empty'].textContent, /没有有效的日净收益样本/);
  assert.equal(ids['trades-body'].children.length, 1, 'failed detail clears old filtered trades');

  assert.ok(requests.some(request => request.url === '/api/research/runs/run-main/trades.csv') === false, 'raw CSV remains a direct browser link and is not fetched or rewritten');
  mainDetail.bars = [
    {symbol: '000001.SZSE', bar_start_datetime: '2024-01-02T09:30:00'},
    {symbol: '000001.SZSE', bar_start_datetime: '2024-01-02 09:30'},
    {symbol: '000001.SZSE', bar_start_datetime: '2024-01-02T14:59:00'},
    {symbol: '000001.SZSE', bar_start_datetime: '2024-01-02T11:30:00'},
    {symbol: '000001.SZSE', source_datetime: '2024-01-03T09:31:00'},
    {symbol: '000002.SZSE', bar_start_datetime: '2024-01-03T09:30:00'},
  ];
  await app.openRun('run-main');
  assert.deepEqual(ids['minute-coverage-body'].children.map(row => row.children.map(cell => cell.textContent)),
    [['2024-01-02', '计划开市', '2', '238', '1'],
     ['2024-01-03', '计划开市 · 无可核对分钟', '0', '240', '0']]);
  assert.match(ids['minute-coverage-status'].textContent, /3 行因标签、标的或时段不符/);
  mainDetail.record.start = '2024-01-01';
  mainDetail.bars.push({symbol: '000001.SZSE', bar_start_datetime: '2024-01-01T09:30:00'});
  await app.openRun('run-main');
  assert.deepEqual(ids['minute-coverage-body'].children[0].children.map(cell => cell.textContent),
    ['2024-01-01', '计划休市 · 有分钟标签', '1', '—', '0']);
  assert.match(ids['minute-coverage-status'].textContent, /1 个计划休市日出现分钟标签/);
  await app.openRun('run-empty');
  assert.equal(ids['minute-coverage-body'].children.length, 0);
  assert.match(ids['minute-coverage-status'].textContent, /尚无可核对/);
  process.stdout.write('PASS: actual cash/market-value points, fixed daily-return bins and cumulative counts, source-index-preserving trade filters, failure clearing, and frozen-parameter historical SMA overlays\n');
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
