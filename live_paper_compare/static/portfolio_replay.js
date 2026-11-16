(function () {
  'use strict';

  var ACCOUNTS_API = '/api/research/portfolio-replays';
  var SOURCES_API = '/api/research/portfolios';
  var PENDING_KEY = 'quant-portfolio-replay-pending-v1';
  var MAX_PENDING_CHARS = 8192;
  var state = {
    accounts: [], sources: [], verifiedSources: {}, selectedId: '', detail: null, pending: null,
    busy: false, accountsRead: 0, sourcesRead: 0, sourceVerifyRead: 0, detailRead: 0, selectionEpoch: 0,
    canAbandon: false
  };

  function id(name) { return document.getElementById(name); }
  function object(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function safeRunId(value) { return typeof value === 'string' && /^[0-9a-f]{32}$/.test(value); }
  function safeRequestId(value) { return typeof value === 'string' && /^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/.test(value); }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function text(name, value) { id(name).textContent = value === undefined || value === null ? '' : String(value); }
  function hide(name, hidden) { id(name).toggleAttribute('hidden', Boolean(hidden)); }
  function clear(node) { if (typeof node.replaceChildren === 'function') node.replaceChildren(); else node.textContent = ''; }
  function make(tag, value, className) {
    var node = document.createElement(tag);
    if (value !== undefined) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  }
  function money(value) { return finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' 元' : '—'; }
  function count(value) { return Number.isInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—'; }
  function pretty(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'string') return value.slice(0, 500) || '—';
    if (typeof value === 'number') return Number.isFinite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 4}) : '—';
    if (typeof value === 'boolean') return value ? '是' : '否';
    try { return JSON.stringify(value).slice(0, 500); } catch (_error) { return '[无法显示]'; }
  }
  function valueOf() {
    for (var i = 0; i < arguments.length; i++) {
      if (arguments[i] !== undefined && arguments[i] !== null) return arguments[i];
    }
    return undefined;
  }
  function recordValid(record) {
    return object(record) && safeRunId(record.account_id) && typeof record.status === 'string' &&
      Number.isInteger(record.revision) && record.revision >= 0;
  }
  function detailValid(payload, accountId) {
    return object(payload) && recordValid(payload.record) && payload.record.account_id === accountId &&
      object(payload.config) && object(payload.source) && object(payload.data_readiness) &&
      Array.isArray(payload.equity) && Array.isArray(payload.orders) && Array.isArray(payload.trades) &&
      (Array.isArray(payload.positions) || object(payload.positions)) && Array.isArray(payload.events);
  }
  function sourceCandidate(item) {
    return object(item) && object(item.receipt) && object(item.config) && safeRunId(item.receipt.run_id) &&
      String(item.receipt.status || '').toLowerCase() === 'completed' &&
      item.receipt.schema_version === 2 && typeof item.receipt.code_version === 'string' && /^[0-9a-f]{64}$/.test(item.receipt.code_version);
  }
  function sourceDetailVerified(payload, runId) {
    if (!object(payload) || !object(payload.receipt) || !object(payload.config) || !object(payload.result)) return false;
    var receipt = payload.receipt, result = payload.result;
    return receipt.run_id === runId && String(receipt.status || '').toLowerCase() === 'completed' &&
      receipt.artifact_integrity === 'verified' && receipt.source_snapshot_status === 'archived_and_verified' &&
      typeof receipt.code_version === 'string' && /^[0-9a-f]{64}$/.test(receipt.code_version) &&
      object(result.provenance) && result.provenance.source_version === receipt.code_version &&
      result.mode === 'RESEARCH_ONLY' && result.paper_admission === false;
  }
  function statusName(value) {
    return ({paused: '已暂停', running: '手动运行中', completed: '已完成'})[value] || '状态待确认';
  }
  function parseHash() {
    var raw = window.location && typeof window.location.hash === 'string' ? window.location.hash : '';
    if (!raw) return {present: false, valid: false, id: ''};
    var value = raw.charAt(0) === '#' ? raw.slice(1) : raw;
    return {present: true, valid: safeRunId(value), id: safeRunId(value) ? value : ''};
  }
  function queryRunId() {
    var search = window.location && typeof window.location.search === 'string' ? window.location.search : '';
    var match = /(?:^|[?&])run_id=([^&]*)/.exec(search);
    if (!match) return '';
    try {
      var value = decodeURIComponent(match[1].replace(/\+/g, ' '));
      return safeRunId(value) ? value : '';
    } catch (_error) { return ''; }
  }
  function fetchJson(url, method, bodyText) {
    var options = {cache: 'no-store', credentials: 'omit', mode: 'same-origin', redirect: 'error'};
    if (method) {
      options.method = method;
      options.headers = {'Content-Type': 'application/json'};
      options.body = bodyText;
    }
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error(object(data) && typeof data.error === 'string' ? data.error.slice(0, 240) : '本地研究服务请求失败。');
          error.status = response.status;
          error.code = object(data) && typeof data.code === 'string' ? data.code : '';
          error.rollbackConfirmed = object(data) && data.rollback_confirmed === true;
          throw error;
        }
        return data;
      });
    });
  }
  function requestId() {
    var cryptoApi = window.crypto;
    var value = '';
    if (cryptoApi && typeof cryptoApi.randomUUID === 'function') value = cryptoApi.randomUUID().replace(/-/g, '').toLowerCase();
    if (!safeRequestId(value) && cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
      var bytes = new Uint8Array(16);
      cryptoApi.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      value = Array.from(bytes).map(function (byte) { return byte.toString(16).padStart(2, '0'); }).join('');
    }
    if (!safeRequestId(value)) throw new Error('浏览器无法生成安全的 UUID4 请求编号。');
    return value;
  }
  function savePending(value) {
    var encoded = JSON.stringify(value);
    if (encoded.length > MAX_PENDING_CHARS) throw new Error('本地请求恢复记录超出长度限制。');
    try { window.localStorage.setItem(PENDING_KEY, encoded); }
    catch (_error) { throw new Error('无法保存本地请求恢复记录；为避免丢失幂等编号，尚未发送请求。'); }
    state.pending = value;
    state.canAbandon = false;
    renderPending();
    updateControls();
  }
  function removePending() {
    try { window.localStorage.removeItem(PENDING_KEY); } catch (_error) {}
    state.pending = null;
    state.canAbandon = false;
    renderPending();
    updateControls();
  }
  function validatePending(raw) {
    if (!object(raw) || raw.version !== 1 || typeof raw.body_json !== 'string' || raw.body_json.length > 2048) return false;
    var body;
    try { body = JSON.parse(raw.body_json); } catch (_error) { return false; }
    if (raw.type === 'create') {
      var keys = Object.keys(body).sort().join(',');
      return keys === 'label,request_id,run_id' && safeRunId(body.run_id) && safeRequestId(body.request_id) &&
        typeof body.label === 'string' && body.label.trim().length > 0 && body.label.length <= 80;
    }
    if (raw.type === 'action' && safeRunId(raw.account_id) && ['resume', 'pause', 'step'].indexOf(raw.action) >= 0 &&
        Number.isInteger(body.expected_revision) && body.expected_revision >= 0) {
      if (raw.action === 'step') {
        return Number.isInteger(body.steps) && body.steps >= 1 && body.steps <= 240 && safeRequestId(body.request_id);
      }
      return Object.keys(body).sort().join(',') === 'expected_revision';
    }
    return false;
  }
  function readPending() {
    var raw;
    try { raw = window.localStorage.getItem(PENDING_KEY); }
    catch (_error) { text('accounts-status', '无法读取本地请求恢复记录；本次仍只读取账户，不发送修改请求。'); return; }
    if (!raw) return;
    if (typeof raw !== 'string' || raw.length > MAX_PENDING_CHARS) {
      text('accounts-status', '本地请求恢复记录超过安全长度，已忽略；未自动执行。');
      return;
    }
    try {
      var parsed = JSON.parse(raw);
      if (!validatePending(parsed)) throw new Error('invalid');
      state.pending = parsed;
      renderPending();
      updateControls();
    } catch (_error) {
      text('accounts-status', '本地请求恢复记录结构或编号无效；未执行其中内容。');
    }
  }
  function renderPending() {
    var pending = state.pending;
    hide('pending-box', !pending);
    if (!pending) return;
    var op = pending.type === 'create' ? '创建恢复账户' : (pending.action === 'step' ? '手动步进' : pending.action === 'resume' ? '继续账户' : '暂停账户');
    text('pending-title', '有一条请求尚待确认：' + op);
    text('pending-message', state.canAbandon
      ? '服务确认这次请求未提交。可以显式放弃后读取最新状态；若要重试，只会复用原请求内容和编号。'
      : '刷新只读取服务状态，不会重发请求。可显式重试本地保存的完全相同请求；不会生成新请求编号。');
    hide('retry-pending-btn', false);
    id('retry-pending-btn').textContent = '显式重试同一请求';
    hide('abandon-pending-btn', !state.canAbandon);
  }
  function setPendingError(error) {
    state.canAbandon = Boolean(state.pending && error && error.status === 409 && error.rollbackConfirmed === true &&
      (error.code === 'PortfolioReplaySourceChanged' || (state.pending.type === 'action' && state.pending.action === 'step' && error.code === 'PortfolioReplayRevisionConflict')));
    renderPending();
    updateControls();
    var message = error && error.message ? error.message : '请求结果尚未确认。';
    text('detail-status', message + ' 原请求已保存在本机；请刷新读取或显式重试。');
    id('detail-status').className = 'status error';
  }
  function updateControls() {
    var pending = Boolean(state.pending);
    var blocked = state.busy || pending;
    var sourceId = id('source-select').value;
    var selectedSource = Boolean(state.verifiedSources[sourceId]) && state.sources.some(function (item) { return item.receipt.run_id === sourceId; });
    id('create-btn').disabled = blocked || !selectedSource;
    id('retry-pending-btn').disabled = state.busy;
    id('abandon-pending-btn').disabled = state.busy;
    var detail = state.detail;
    var record = detail && detail.record;
    var canContinue = Boolean(record && record.status !== 'completed' && record.continuation_allowed === true &&
      record.code_current === true && Number.isInteger(record.remaining_frames) && record.remaining_frames > 0);
    var canPause = Boolean(record && record.status === 'running');
    var canResume = Boolean(record && record.status === 'paused' && canContinue);
    hide('resume-btn', !record || record.status !== 'paused');
    hide('pause-btn', !canPause);
    id('resume-btn').disabled = blocked || !canResume;
    id('pause-btn').disabled = blocked || !canPause;
    var canStep = Boolean(canContinue && record.status === 'running');
    id('step-btn').disabled = blocked || !canStep;
    id('step-count').disabled = blocked || !canStep;
    id('refresh-btn').disabled = state.busy;
  }
  function renderAccountList() {
    var root = id('account-list');
    clear(root);
    if (!state.accounts.length) {
      root.append(make('div', '尚无恢复点。可以从上方选择已完成、源码已归档的历史组合创建。', 'empty'));
      return;
    }
    state.accounts.forEach(function (record) {
      var button = make('button', undefined, 'account-item' + (record.account_id === state.selectedId ? ' selected' : ''));
      button.type = 'button';
      button.append(make('div', record.label || ('回放 ' + record.account_id), 'account-title'));
      var progress = count(record.cursor) + ' / ' + count(record.total_frames) + ' 帧';
      button.append(make('div', statusName(record.status) + ' · ' + progress + ' · 权益 ' + money(record.current_equity), 'account-meta'));
      button.addEventListener('click', function () { openAccount(record.account_id, true, false); });
      root.append(button);
    });
  }
  function sourceLabel(item) {
    var config = item.config || {}, receipt = item.receipt || {};
    var symbols = Array.isArray(config.symbols) ? config.symbols.join('、') : '';
    var dates = config.start && config.end ? config.start + ' 至 ' + config.end : '';
    return (symbols || '历史组合') + (dates ? ' · ' + dates : '') + ' · ' + receipt.run_id;
  }
  function renderSources() {
    var select = id('source-select');
    var old = select.value;
    clear(select);
    select.append(make('option', state.sources.length ? '请选择已完成的历史组合' : '没有可用的已归档完成组合'));
    select.children[0].value = '';
    state.sources.forEach(function (item) {
      var option = make('option', sourceLabel(item) + (state.verifiedSources[item.receipt.run_id] ? ' · 来源已核验' : ' · 待核验'));
      option.value = item.receipt.run_id;
      select.append(option);
    });
    var requested = queryRunId();
    var target = state.sources.some(function (item) { return item.receipt.run_id === old; }) ? old : requested;
    if (target && state.sources.some(function (item) { return item.receipt.run_id === target; })) select.value = target;
    updateControls();
  }
  async function verifySelectedSource() {
    var runId = id('source-select').value;
    var candidate = state.sources.find(function (item) { return item.receipt.run_id === runId; });
    var request = ++state.sourceVerifyRead;
    if (!candidate) {
      updateControls();
      text('source-status', '请选择一条已完成并有代码版本记录的历史组合。');
      return null;
    }
    delete state.verifiedSources[runId];
    updateControls();
    text('source-status', '正在读取所选来源详情并验证源码归档…');
    try {
      var detail = await fetchJson(SOURCES_API + '/' + encodeURIComponent(runId));
      if (request !== state.sourceVerifyRead || id('source-select').value !== runId) return null;
      if (!sourceDetailVerified(detail, runId)) throw new Error('所选运行的报告完整性、源码归档或版本对照未通过校验。');
      state.verifiedSources[runId] = true;
      renderSources();
      // renderSources preserves the current option, but avoid recursively revalidating it.
      text('source-status', '该来源详情已通过文件完整性和源码版本校验；创建请求仍需用户明确提交。');
      updateControls();
      return detail;
    } catch (error) {
      if (request !== state.sourceVerifyRead || id('source-select').value !== runId) return null;
      text('source-status', (error && error.message ? error.message : '来源详情校验失败。') + ' 无法据此创建回放账户。');
      updateControls();
      return null;
    }
  }
  function getSourceCatalog() {
    var request = ++state.sourcesRead;
    text('source-status', '正在读取并筛选已完成、源码归档已验证的来源…');
    return fetchJson(SOURCES_API).then(function (payload) {
      if (request !== state.sourcesRead) return;
      if (!object(payload) || !Array.isArray(payload.runs)) throw new Error('历史来源响应结构无效。');
      state.sources = payload.runs.filter(sourceCandidate).slice(0, 100);
      state.verifiedSources = {};
      renderSources();
      text('source-status', '找到 ' + state.sources.length + ' 条完成运行候选。列表是摘要；须读取所选单条详情验证归档后才能创建。');
      if (id('source-select').value) verifySelectedSource();
    }).catch(function () {
      if (request !== state.sourcesRead) return;
      state.sources = [];
      renderSources();
      text('source-status', '历史来源读取失败；本次不会创建账户。请刷新后重试。');
      updateControls();
    });
  }
  function clearDetail(message) {
    state.detail = null;
    text('detail-title', '选择一个回放账户');
    text('detail-status', message || '刷新账户列表后，从左侧选择账户。');
    id('detail-status').className = 'status';
    text('cash-value', '—'); text('equity-value', '—'); text('cursor-value', '—'); text('remaining-value', '—');
    text('warnings', '');
    renderChart([]);
    renderPositions({});
    renderRows('orders', []); renderRows('trades', []); renderRows('events', []);
    ['equity', 'positions', 'orders', 'trades', 'events'].forEach(function (name) { hide('csv-' + name, true); });
    updateControls();
  }
  function symbolName(value) { return typeof value === 'string' ? value : '—'; }
  function renderPositions(positions) {
    var body = id('positions-body'); clear(body);
    var rows = [];
    if (Array.isArray(positions)) rows = positions.map(function (item) { return object(item) ? item : {}; });
    else if (object(positions)) {
      var configuredSymbols = state.detail && Array.isArray(state.detail.config.symbols) ? state.detail.config.symbols : [];
      var symbols = configuredSymbols.filter(function (symbol, index) { return Object.prototype.hasOwnProperty.call(positions, symbol) && configuredSymbols.indexOf(symbol) === index; });
      Object.keys(positions).forEach(function (symbol) { if (symbols.indexOf(symbol) < 0) symbols.push(symbol); });
      symbols.forEach(function (symbol) {
      var row = object(positions[symbol]) ? Object.assign({}, positions[symbol]) : {quantity: positions[symbol]};
      if (row.symbol === undefined) row.symbol = symbol;
      rows.push(row);
      });
    }
    if (!rows.length) {
      var empty = make('tr'); var cell = make('td', '当前没有持仓记录。', 'empty-cell'); cell.setAttribute('colspan', '6'); empty.append(cell); body.append(empty); return;
    }
    rows.forEach(function (position) {
      var lots = Array.isArray(position.lots) ? position.lots.length + ' 批 · ' + pretty(position.lots) : pretty(valueOf(position.lots, position.lot_details));
      var values = [symbolName(position.symbol), pretty(valueOf(position.quantity, position.qty, position.volume)),
        pretty(valueOf(position.available_to_sell, position.available, position.sellable)),
        pretty(valueOf(position.market_value, position.value)), pretty(valueOf(position.average_cost, position.avg_cost, position.cost)), lots];
      var tr = make('tr'); values.forEach(function (value) { tr.append(make('td', value)); }); body.append(tr);
    });
  }
  function renderRows(prefix, rows) {
    var head = id(prefix + '-head'), body = id(prefix + '-body');
    clear(head); clear(body);
    var items = Array.isArray(rows) ? rows.filter(object) : [];
    var columns = {
      orders: ['signal_datetime', 'symbol', 'side', 'requested_quantity', 'filled_quantity', 'status', 'reason'],
      trades: ['fill_datetime', 'symbol', 'side', 'quantity', 'price', 'commission', 'stamp_duty', 'order_id'],
      events: ['created_at', 'event_type', 'revision', 'details']
    };
    var keys = items.length ? columns[prefix] : [];
    if (!keys.length) {
      var empty = make('tr'); var cell = make('td', prefix === 'orders' ? '当前没有委托记录。' : prefix === 'trades' ? '当前没有成交记录。' : '当前没有事件记录。', 'empty-cell'); cell.setAttribute('colspan', '1'); empty.append(cell); body.append(empty);
      text(prefix + '-caption', '0 条记录。'); return;
    }
    var labels = {
      order_id: '委托编号', id: '编号', trade_id: '成交编号', event_id: '事件编号', datetime: '时间', time: '时间', timestamp: '时间',
      signal_datetime: '信号时间', fill_datetime: '成交时间', requested_quantity: '委托数量', created_at: '记录时间', details: '事件详情', event: '事件', event_type: '事件类型', type: '类型', action: '动作', status: '状态', symbol: '证券代码', name: '名称', side: '方向', direction: '方向',
      quantity: '数量', qty: '数量', volume: '数量', price: '价格', limit_price: '委托价格', fill_price: '成交价格', cash_after: '成交后现金',
      commission: '佣金', stamp_duty: '印花税', reason: '原因', message: '说明', cursor: '帧序号', revision: '版本', request_id: '请求编号',
      target_quantity: '目标数量', filled_quantity: '成交数量', remaining_quantity: '剩余数量', source_datetime: '来源时间', frame: '帧序号'
    };
    var header = make('tr'); keys.forEach(function (key) { header.append(make('th', labels[key] || key)); }); head.append(header);
    function displayCell(key, value) {
      if ((key === 'side' || key === 'direction') && typeof value === 'string') return ({buy: '买入', sell: '卖出', BUY: '买入', SELL: '卖出'})[value] || pretty(value);
      if (key === 'status' && typeof value === 'string') return ({pending: '待处理', accepted: '已接受', submitted: '已提交', filled: '已成交', partial: '部分成交', partial_filled: '部分成交', rejected: '已拒绝', cancelled: '已撤销', open: '未完成'})[value.toLowerCase()] || pretty(value);
      if ((key === 'event' || key === 'event_type' || key === 'type' || key === 'action') && typeof value === 'string') return ({initialized: '初始化', create: '创建恢复点', created: '创建恢复点', start: '继续回放', resumed: '继续回放', pause: '暂停回放', paused: '暂停回放', step: '手动步进', stepped: '手动步进', complete: '回放完成', error: '发生错误'})[value.toLowerCase()] || pretty(value);
      return pretty(value);
    }
    items.slice(0, 300).forEach(function (item) {
      var row = make('tr'); keys.forEach(function (key) { row.append(make('td', displayCell(key, item[key]))); }); body.append(row);
    });
    text(prefix + '-caption', '显示 ' + Math.min(items.length, 300) + ' 条，共 ' + items.length + ' 条记录。');
  }
  function svg(tag, attrs, label) {
    var node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, String(attrs[key])); });
    if (label !== undefined) node.textContent = String(label);
    return node;
  }
  function renderChart(rows) {
    var root = id('equity-chart'); clear(root);
    var points = rows.map(function (row, index) {
      row = object(row) ? row : {};
      return {index: index, time: row.source_datetime, value: valueOf(row.equity, row.current_equity, row.value)};
    }).filter(function (point) { return typeof point.time === 'string' && point.time.length > 0 && finite(point.value); });
    points.forEach(function (point, index) { point.index = index; });
    if (!points.length) {
      hide('equity-chart', true); hide('chart-empty', false);
      text('chart-empty', '所选账户尚无带实际时间戳的有限权益点，未绘图。');
      return;
    }
    var values = points.map(function (point) { return point.value; });
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values);
    if (!Number.isFinite(max - min) || max === min) { var pad = Math.max(Math.abs(max) * 0.01, 1); min -= pad; max += pad; }
    var left = 72, right = 878, top = 18, bottom = 196;
    function xAt(index) { return points.length < 2 ? (left + right) / 2 : left + (right - left) * index / (points.length - 1); }
    function yAt(value) { return bottom - ((value - min) / (max - min)) * (bottom - top); }
    for (var tick = 0; tick <= 4; tick++) {
      var y = top + (bottom - top) * tick / 4;
      var label = max - (max - min) * tick / 4;
      root.append(svg('line', {x1: left, y1: y, x2: right, y2: y, class: 'grid-line'}));
      root.append(svg('text', {x: left - 8, y: y + 4, 'text-anchor': 'end', class: 'axis-label'}, Number.isFinite(label) ? label.toLocaleString('zh-CN', {maximumFractionDigits: 2}) : '—'));
    }
    root.append(svg('line', {x1: left, y1: top, x2: left, y2: bottom, class: 'axis-line'}));
    root.append(svg('line', {x1: left, y1: bottom, x2: right, y2: bottom, class: 'axis-line'}));
    for (var i = 1; i < points.length; i++) root.append(svg('line', {x1: xAt(i - 1), y1: yAt(points[i - 1].value), x2: xAt(i), y2: yAt(points[i].value), class: 'series-line'}));
    points.forEach(function (point) { root.append(svg('circle', {cx: xAt(point.index), cy: yAt(point.value), r: 3, fill: '#2463eb', 'data-time': point.time, 'data-value': point.value})); });
    [0, Math.floor((points.length - 1) / 2), points.length - 1].filter(function (value, index, array) { return array.indexOf(value) === index; }).forEach(function (index) {
      var point = points[index]; root.append(svg('text', {x: xAt(index), y: 226, 'text-anchor': index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle', class: 'axis-label'}, point.time));
    });
    hide('chart-empty', true); hide('equity-chart', false);
  }
  function makeCsvLinks(accountId) {
    var base = ACCOUNTS_API + '/' + encodeURIComponent(accountId) + '/';
    ['equity', 'positions', 'orders', 'trades', 'events'].forEach(function (name) {
      var link = id('csv-' + name);
      link.href = base + name + '.csv';
      hide('csv-' + name, false);
    });
  }
  function renderDetail(payload, accountId) {
    var record = payload.record;
    var result = object(payload.result) ? payload.result : {};
    state.detail = payload;
    text('detail-title', record.label || ('回放账户 ' + accountId));
    text('detail-status', statusName(record.status) + ' · 版本 ' + record.revision + ' · 已推进 ' + count(record.cursor) + ' / ' + count(record.total_frames) + ' 帧');
    id('detail-status').className = 'status';
    var latestEquity = payload.equity.length && object(payload.equity[payload.equity.length - 1]) ? payload.equity[payload.equity.length - 1] : {};
    var currentResult = object(result.current) ? result.current : {};
    text('cash-value', money(valueOf(currentResult.cash, result.cash, result.current_cash, record.current_cash, latestEquity.cash)));
    var finalEquity = valueOf(record.current_equity, result.current_equity);
    if (!finite(finalEquity) && payload.equity.length) {
      finalEquity = valueOf(latestEquity.equity, latestEquity.current_equity);
    }
    text('equity-value', money(finalEquity));
    text('cursor-value', count(record.cursor) + ' / ' + count(record.total_frames));
    text('remaining-value', count(record.remaining_frames));
    var warnings = [];
    var readiness = payload.data_readiness;
    var readinessStatus = typeof readiness.status === 'string' ? readiness.status.toLowerCase() : 'unverified';
    if (readinessStatus === 'mismatch') warnings.push('数据对照存在不一致。该账户只保留历史演练证据，不能据此认定行情和价格口径已认证。');
    else if (readinessStatus !== 'verified' && readinessStatus !== 'match') warnings.push('数据来源或价格口径尚未完成认证；请保留待确认状态。');
    if (record.code_current !== true) warnings.push('账户使用的源码版本与当前可执行版本不一致或无法确认；继续和步进已锁定。');
    if (record.continuation_allowed !== true && record.status !== 'completed') warnings.push('服务未允许继续回放；请核对账户状态与服务原因。');
    text('warnings', warnings.join('\n'));
    text('mode-badge', 'RESEARCH_ONLY');
    renderChart(payload.equity);
    renderPositions(payload.positions);
    renderRows('orders', payload.orders); renderRows('trades', payload.trades); renderRows('events', payload.events);
    makeCsvLinks(accountId);
    updateControls();
  }
  function setHash(value) {
    if (!window.location) return;
    var next = value ? '#' + value : '';
    if (window.location.hash !== next) window.location.hash = next;
  }
  async function openAccount(rawId, writeHash, force) {
    var accountId = safeRunId(rawId) ? rawId : '';
    if (!accountId) { state.detailRead++; state.selectionEpoch++; state.selectedId = ''; clearDetail('账户编号格式无效；未发送详情请求。'); renderAccountList(); return null; }
    if (!force && state.selectedId === accountId && state.detail && state.detail.record.account_id === accountId) return state.detail;
    state.selectedId = accountId;
    state.selectionEpoch++;
    var request = ++state.detailRead;
    renderAccountList();
    clearDetail('正在读取并核对恢复点…');
    text('detail-title', '回放账户 ' + accountId);
    if (writeHash) setHash(accountId);
    try {
      var payload = await fetchJson(ACCOUNTS_API + '/' + encodeURIComponent(accountId));
      if (request !== state.detailRead || state.selectedId !== accountId) return null;
      if (!detailValid(payload, accountId)) throw new Error('账户详情结构或编号校验失败。');
      reconcileStatusAction(payload);
      renderDetail(payload, accountId);
      return payload;
    } catch (error) {
      if (request !== state.detailRead || state.selectedId !== accountId) return null;
      clearDetail('账户详情读取失败；旧详情已清除。' + (error && error.status === 409 ? '服务报告了版本或源码变化，请刷新账户列表。' : '请检查本地研究服务。'));
      text('detail-title', '回放账户 ' + accountId);
      id('detail-status').className = 'status error';
      return null;
    }
  }
  function recordById(accountId) { return state.accounts.find(function (record) { return record.account_id === accountId; }) || null; }
  function reconcileStatusAction(payload) {
    var pending = state.pending;
    if (!pending || pending.type !== 'action' || (pending.action !== 'resume' && pending.action !== 'pause') || payload.record.account_id !== pending.account_id) return false;
    var body;
    try { body = JSON.parse(pending.body_json); } catch (_error) { return false; }
    var expectedEvent = pending.action === 'resume' ? 'start' : 'pause';
    var confirmed = Array.isArray(payload.events) && payload.events.some(function (event) {
      return object(event) && event.revision === body.expected_revision + 1 && event.event_type === expectedEvent;
    });
    if (!confirmed) return false;
    removePending();
    text('detail-status', statusName(payload.record.status) + ' · 恢复事件确认原状态请求已提交（当前版本 ' + payload.record.revision + '）。');
    return true;
  }
  function matchPendingCreate(accounts) {
    if (!state.pending || state.pending.type !== 'create') return '';
    var request = JSON.parse(state.pending.body_json).request_id;
    var match = accounts.find(function (record) { return record.creation_request_id === request; });
    if (!match) return '';
    removePending();
    return match.account_id;
  }
  async function loadAccounts(options) {
    options = options || {};
    var request = ++state.accountsRead;
    var epoch = options.expectedSelectionEpoch === undefined ? state.selectionEpoch : options.expectedSelectionEpoch;
    state.detailRead++;
    state.detail = null;
    text('accounts-status', '正在读取可恢复回放账户…');
    try {
      var payload = await fetchJson(ACCOUNTS_API);
      if (request !== state.accountsRead) return null;
      if (!object(payload) || !Array.isArray(payload.accounts) || payload.accounts.some(function (record) { return !recordValid(record); })) throw new Error('回放账户列表响应无效。');
      state.accounts = payload.accounts.slice(0, 200);
      var matchedId = matchPendingCreate(state.accounts);
      renderAccountList();
      text('accounts-status', '当前读取到 ' + state.accounts.length + ' 个本地隔离账户。');
      var preferred = options.preferredId;
      if (preferred && epoch === state.selectionEpoch) state.selectedId = preferred;
      else if (matchedId && epoch === state.selectionEpoch) state.selectedId = matchedId;
      var hash = parseHash();
      if (hash.present && !hash.valid) {
        state.selectedId = '';
        setHash('');
        clearDetail('分享链接中的账户编号格式无效；未发出详情请求。');
        renderAccountList();
        return state.accounts;
      }
      var pendingAccount = state.pending && state.pending.type === 'action' && recordById(state.pending.account_id) ? state.pending.account_id : '';
      var chosen = state.selectedId && recordById(state.selectedId) ? state.selectedId : pendingAccount || (hash.valid && recordById(hash.id) ? hash.id : '');
      if (chosen) await openAccount(chosen, !hash.present, Boolean(options.forceDetail));
      else { state.selectedId = ''; clearDetail(state.accounts.length ? '请选择左侧一个回放账户。' : '尚无恢复点。请从上方已归档的历史组合创建。'); renderAccountList(); }
      return state.accounts;
    } catch (_error) {
      if (request !== state.accountsRead) return null;
      state.accounts = [];
      state.selectedId = '';
      state.detailRead++;
      renderAccountList();
      text('accounts-status', '账户列表读取失败；旧选择已清除。本次没有发送创建或步进请求。');
      clearDetail('本地回放账户列表暂时无法读取；请稍后刷新。');
      return null;
    }
  }
  function handleHashChange() {
    var hash = parseHash();
    if (!hash.present) {
      state.detailRead++; state.selectionEpoch++; state.selectedId = '';
      clearDetail('请选择左侧一个回放账户。'); renderAccountList(); return;
    }
    if (!hash.valid) {
      state.detailRead++; state.selectionEpoch++; state.selectedId = '';
      clearDetail('分享链接中的账户编号格式无效；未发出详情请求。'); renderAccountList(); return;
    }
    openAccount(hash.id, false, false);
  }
  function createPayload() {
    var runId = id('source-select').value;
    if (!state.verifiedSources[runId] || !state.sources.some(function (item) { return item.receipt.run_id === runId; })) throw new Error('请先选择一条已完成历史组合，并等待单条详情的源码归档校验通过。');
    var label = id('account-label').value.trim();
    if (!label || label.length > 80) throw new Error('账户名称须为 1–80 个字符。');
    return {run_id: runId, label: label, request_id: requestId()};
  }
  function setBusy(value) { state.busy = Boolean(value); updateControls(); }
  async function sendPending() {
    if (!state.pending || state.busy) return;
    var saved = state.pending;
    var body = JSON.parse(saved.body_json);
    var epoch = state.selectionEpoch;
    setBusy(true);
    text('detail-status', '正在发送用户显式确认的原请求…');
    id('detail-status').className = 'status';
    try {
      var response;
      if (saved.type === 'create') {
        response = await fetchJson(ACCOUNTS_API, 'POST', saved.body_json);
        var created = object(response) && object(response.record) ? response.record : response;
        if (!recordValid(created) || created.creation_request_id !== body.request_id) throw new Error('服务确认创建，但返回账户编号或创建请求编号未通过核对；本地仍保留原请求。');
        await loadAccounts({preferredId: created.account_id, expectedSelectionEpoch: epoch, forceDetail: true});
      } else {
        var url = ACCOUNTS_API + '/' + encodeURIComponent(saved.account_id) + '/' + saved.action;
        response = await fetchJson(url, 'POST', saved.body_json);
        if (!detailValid(response, saved.account_id)) throw new Error('服务返回的回放详情未通过账户编号或结构核对；本地仍保留原请求。');
        if (saved.action === 'step') {
          var appliedRevision = body.expected_revision + 1;
          if (typeof response.replayed !== 'boolean' || response.request_id !== body.request_id ||
              response.original_revision !== body.expected_revision || response.applied_revision !== appliedRevision ||
              response.record.revision < appliedRevision) throw new Error('步进响应的幂等确认编号或版本边界不匹配；本地仍保留原请求。');
        } else if (!reconcileStatusAction(response)) {
          throw new Error('状态请求响应没有对应的恢复事件确认；本地仍保留原请求。');
        }
        removePending();
        if (state.selectedId === saved.account_id && state.selectionEpoch === epoch) renderDetail(response, saved.account_id);
        await loadAccounts({forceDetail: state.selectedId === saved.account_id});
      }
      if (state.pending === saved && saved.type === 'create') {
        // A successful create is finalized only after the read-only account list found its durable request ID.
        var found = state.accounts.find(function (record) { return record.creation_request_id === body.request_id; });
        if (found) removePending();
      }
    } catch (error) {
      setPendingError(error);
      if (error && error.status === 409) {
        // Read-only reconciliation may confirm a lost resume/pause ack; it never resends a request.
        await loadAccounts({forceDetail: true});
      }
    } finally { setBusy(false); }
  }
  function installCreateHandler() {
    id('create-form').addEventListener('submit', function (event) {
      event.preventDefault();
      if (state.pending || state.busy) return;
      try {
        var payload = createPayload();
        savePending({version: 1, type: 'create', body_json: JSON.stringify(payload)});
        sendPending();
      } catch (error) {
        text('source-status', error.message || '无法创建回放账户。');
      }
    });
    id('retry-pending-btn').addEventListener('click', function () { sendPending(); });
    id('abandon-pending-btn').addEventListener('click', async function () {
      if (!state.pending || !state.canAbandon || state.busy) return;
      removePending();
      text('accounts-status', '已按服务确认放弃未提交的旧请求；正在读取最新账户版本。');
      await loadAccounts({forceDetail: true});
    });
    id('refresh-btn').addEventListener('click', function () { loadAccounts({forceDetail: true}); getSourceCatalog(); });
    id('source-select').addEventListener('change', function () { verifySelectedSource(); });
    id('resume-btn').addEventListener('click', function () { submitAction('resume'); });
    id('pause-btn').addEventListener('click', function () { submitAction('pause'); });
    id('step-btn').addEventListener('click', function () { submitAction('step'); });
    window.addEventListener('hashchange', handleHashChange);
  }
  function submitAction(action) {
    var detail = state.detail;
    if (!detail || state.pending || state.busy) return;
    var record = detail.record;
    if (!safeRunId(record.account_id) || record.status === 'completed') return;
    if (action === 'pause' && record.status !== 'running') return;
    if (action !== 'pause' && (record.code_current !== true || record.continuation_allowed !== true)) return;
    if (action === 'resume' && record.status !== 'paused') return;
    if (action === 'step' && (record.status !== 'running' || !Number.isInteger(record.remaining_frames) || record.remaining_frames < 1)) return;
    var body = {expected_revision: record.revision};
    try {
      if (action === 'step') {
        var steps = Number(id('step-count').value);
        if ([1, 60, 240].indexOf(steps) < 0) return;
        body.steps = steps;
        body.request_id = requestId();
      }
      savePending({version: 1, type: 'action', account_id: record.account_id, action: action, body_json: JSON.stringify(body)});
      sendPending();
    } catch (error) {
      text('detail-status', error.message || '无法保存恢复请求。');
      id('detail-status').className = 'status error';
    }
  }
  function init() {
    installCreateHandler();
    readPending();
    Promise.all([getSourceCatalog(), loadAccounts()]);
  }
  init();
  window.PortfolioReplay = {state: state, refresh: loadAccounts, openAccount: openAccount, retry: sendPending,
    helpers: {safeRunId: safeRunId, safeRequestId: safeRequestId, detailValid: detailValid, sourceCandidate: sourceCandidate, sourceDetailVerified: sourceDetailVerified, parseHash: parseHash}};
})();
