'use strict';

const API = '/api/research/workflows';
const PENDING_KEY = 'quant-workflow-pending-v1';
const activeStatuses = new Set(['queued', 'starting', 'running']);
const states = {
  queued: '排队中', starting: '正在启动', running: '运行中', completed: '已完成',
  failed: '失败', interrupted: '已中断', cancelled: '已取消', cancelling: '正在取消',
  timed_out: '等待超时', not_started: '未提交',
};
const byId = id => document.getElementById(id);
const value = id => byId(id).value;
const form = byId('workflow-form');
let pending = null;
let preview = null;
let selected = '';
let busy = false;
let listRead = 0;
let detailRead = 0;

function status(message) { byId('action-status').textContent = message; }
function label(state) { return states[state] || '状态待确认'; }
function validId(id) { return typeof id === 'string' && /^[0-9a-f]{32}$/.test(id); }
function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function field(row, title, name, properties) {
  const container = element('label', title);
  const input = element(properties.tag || 'input');
  const {tag, ...attributes} = properties;
  Object.assign(input, attributes);
  input.dataset.field = name;
  container.append(input);
  row.append(container);
}

function invalidate() {
  preview = null;
  byId('preview-panel').hidden = true;
  controls();
}

function controls() {
  byId('preview').disabled = busy || Boolean(pending);
  byId('run').disabled = busy || Boolean(pending) || !preview;
  byId('retry').hidden = !pending;
  byId('retry').disabled = busy;
}

function limitRows() {
  for (const [name, cap] of [['cases', 8], ['folds', 4]]) {
    const rows = byId(name);
    byId(name === 'cases' ? 'add-case' : 'add-fold').disabled = rows.children.length >= cap;
    Array.from(rows.children).forEach((row, index) => {
      row.querySelector('legend').textContent = name === 'cases' ? '参数组 ' + (index + 1) : '第 ' + (index + 1) + ' 折';
      row.querySelector('button').disabled = rows.children.length <= 2;
    });
  }
}

function addRow(name, preset = {}) {
  const rows = byId(name);
  const row = element('fieldset');
  row.append(element('legend'));
  const grid = element('div');
  grid.className = 'grid';
  row.append(grid);
  if (name === 'cases') {
    field(grid, '名称', 'label', {value: preset.label || '', required: true, maxLength: 80});
    field(grid, '滑点覆盖（留空沿用）', 'slippage_bps', {type: 'number', min: '0', step: 'any', value: preset.slippage || ''});
    field(grid, '资金比例覆盖（留空沿用）', 'target_fraction', {type: 'number', min: '0.0001', max: '1', step: 'any'});
    field(grid, '快线覆盖（可选）', 'fast', {type: 'number', min: '1', max: '240'});
    field(grid, '慢线覆盖（可选）', 'slow', {type: 'number', min: '2', max: '241'});
    field(grid, 'Python 参数覆盖 JSON（可选）', 'custom_params', {tag: 'textarea', rows: 2, maxLength: 4096});
  } else {
    for (const [key, title] of [['train_start','训练开始'],['train_end','训练结束'],
      ['validation_start','验证开始'],['validation_end','验证结束']]) {
      field(grid, title, key, {type: 'date', required: true, min: '2024-01-01', max: '2025-12-31', value: preset[key] || ''});
    }
  }
  const remove = element('button', '移除');
  remove.type = 'button';
  remove.addEventListener('click', () => { row.remove(); limitRows(); invalidate(); });
  row.append(remove);
  rows.append(row);
  limitRows();
  syncFields();
  invalidate();
}

function syncFields() {
  const kind = value('kind');
  const strategy = value('strategy');
  for (const [id, enabled] of [['batch-fields', kind === 'batch'], ['rolling-fields', kind === 'rolling'],
    ['symbols-fields', kind === 'symbols'], ['symbol-field', kind !== 'symbols'], ['date-fields', kind !== 'rolling'],
    ['sma-fields', strategy === 'sma_cross'], ['custom-fields', strategy === 'custom_python']]) {
    const node = byId(id);
    node.hidden = !enabled;
    if (node.tagName === 'FIELDSET') node.disabled = !enabled;
    else for (const input of node.querySelectorAll('input, textarea')) input.disabled = !enabled;
  }
  byId('source-id').required = strategy === 'custom_python';
  for (const row of byId('cases').children) {
    for (const input of row.querySelectorAll('[data-field="fast"],[data-field="slow"],[data-field="custom_params"]')) {
      const enabled = input.dataset.field === 'custom_params' ? strategy === 'custom_python' : strategy === 'sma_cross';
      input.disabled = !enabled;
      input.parentElement.hidden = !enabled;
    }
  }
}

function parameterObject(raw) {
  if (!raw.trim()) return {};
  let result;
  try { result = JSON.parse(raw); } catch { throw new Error('策略参数需要有效的 JSON 对象。'); }
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('策略参数需要 JSON 对象。');
  return result;
}

function specFromForm() {
  const kind = value('kind');
  const strategy = value('strategy');
  const params = {target_fraction: Number(value('fraction')), max_turnover_participation: Number(value('participation'))};
  const base = {
    strategy, backend: strategy === 'custom_python' ? 'isolated_python' : 'reference',
    initial_cash: Number(value('cash')),
    commission_rate: Number(value('commission')), min_commission: Number(value('minimum')),
    slippage_bps: Number(value('slippage')), params,
  };
  if (strategy === 'sma_cross') Object.assign(params, {fast: Number(value('fast')), slow: Number(value('slow'))});
  if (strategy === 'custom_python') Object.assign(base, {
    source_id: value('source-id').trim(), history_limit: Number(value('history-limit')),
    custom_params: parameterObject(value('custom-params')),
  });
  if (kind !== 'rolling') Object.assign(base, {start: value('start'), end: value('end')});
  if (kind === 'symbols') {
    const symbols = value('symbols').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
    return {base, symbols};
  }
  base.symbol = value('symbol').trim();
  const batch = kind === 'batch';
  const rows = Array.from(byId(batch ? 'cases' : 'folds').children);
  const entries = rows.map(row => {
    const raw = Object.fromEntries(Array.from(row.querySelectorAll('[data-field]'))
      .filter(input => !input.disabled).map(input => [input.dataset.field, input.value.trim()]));
    if (!batch) return raw;
    const result = {label: raw.label};
    if (raw.slippage_bps) result.slippage_bps = Number(raw.slippage_bps);
    const overrides = Object.fromEntries(['target_fraction','fast','slow']
      .filter(key => raw[key]).map(key => [key, Number(raw[key])]));
    if (Object.keys(overrides).length) result.params = overrides;
    if (raw.custom_params) result.custom_params = parameterObject(raw.custom_params);
    return result;
  });
  if (!batch) return {base, folds: entries};
  return {base, cases: entries};
}

async function loadSources() {
  try {
    const response = await fetch('/api/research/custom-sources', {cache: 'no-store', credentials: 'omit', mode: 'same-origin'});
    if (!response.ok) return;
    const data = await response.json();
    const options = (data.sources || []).filter(item => /^[0-9a-f]{64}$/.test(item.registration_id)).map(item => {
      const option = element('option');
      option.value = item.registration_id;
      option.label = item.name;
      return option;
    });
    byId('registered-sources').replaceChildren(...options);
  } catch {}
}

async function request(path = '', body) {
  const options = {cache: 'no-store', credentials: 'omit', mode: 'same-origin', redirect: 'error'};
  if (body !== undefined) Object.assign(options, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const response = await fetch(API + path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.error === 'string' ? data.error : '本机请求失败。');
    error.status = response.status;
    throw error;
  }
  return data;
}

function setPending(next) {
  if (next) sessionStorage.setItem(PENDING_KEY, JSON.stringify(next));
  else sessionStorage.removeItem(PENDING_KEY);
  pending = next;
  controls();
}

function showPreview(plan) {
  const unit = plan.scope?.symbol_count ? '个标的' : '个窗口';
  byId('preview-summary').textContent = plan.cases.length + ' ' + unit + '已通过配置检查。预检未提交任务。';
  const lines = plan.cases.map(item => {
    const config = item.config;
    const params = config.params || {};
    const parts = [item.label, config.start + ' 至 ' + config.end, '滑点 ' + config.slippage_bps + ' 基点'];
    if (item.label !== config.symbol) parts.splice(1, 0, config.symbol);
    if (params.target_fraction !== undefined) parts.push('资金比例 ' + params.target_fraction);
    if (config.strategy === 'sma_cross') parts.push('快线 ' + params.fast + ' / 慢线 ' + params.slow);
    if (config.strategy === 'custom_python') parts.push('登记版本 ' + config.source_id.slice(0, 12));
    return element('li', parts.join(' · '));
  });
  byId('preview-cases').replaceChildren(...lines);
  byId('preview-panel').hidden = false;
}

function renderJob(job) {
  byId('job-panel').hidden = false;
  byId('job-status').textContent = label(job.status) + ' · 实验 ' + job.experiment_id;
  const group = byId('experiment-link');
  group.hidden = !validId(job.experiment_id);
  if (!group.hidden) group.href = '/research?experiment=' + job.experiment_id;
  const cases = job.plan?.cases || [];
  const rows = cases.map((planned, index) => {
    const item = job.receipt?.cases?.[index] || planned;
    const row = element('tr');
    const window = planned.window || planned.config || {};
    row.append(element('td', planned.label), element('td', window.start ? window.start + ' 至 ' + window.end : '—'),
      element('td', label(item.status || 'not_started')));
    const report = element('td');
    if (validId(item.run_id)) {
      const link = element('a', '打开回测');
      link.href = '/research#' + item.run_id;
      report.append(link);
    }
    row.append(report);
    return row;
  });
  byId('job-cases').replaceChildren(...rows);
  byId('job-note').textContent = activeStatuses.has(job.status) ? '任务在本机后台执行。关闭此页后，可以重新打开查看进度。' :
    job.status === 'completed' ? '可打开各窗口的独立报告。' :
    (job.error || job.receipt?.stop_reason || '已停止提交后续窗口。已创建的回测仍可在历史中查看。');
}

async function loadJob(id) {
  if (!validId(id)) return;
  const reading = ++detailRead;
  try {
    const job = await request('/' + id);
    if (selected === id && reading === detailRead) renderJob(job);
  } catch {
    if (selected === id && reading === detailRead) {
      byId('job-panel').hidden = true;
      byId('history-status').textContent = '暂时无法读取这项实验。';
    }
  }
}

async function refresh() {
  const reading = ++listRead;
  try {
    const data = await request();
    if (reading !== listRead) return;
    const items = (data.jobs || []).filter(job => validId(job.job_id)).map(job => {
      const item = element('li');
      const names = {rolling: '滚动验证', symbols: '多标的对照', batch: '参数对照'};
      const link = element('a', (names[job.kind] || '研究实验') + ' · ' + label(job.status) + ' · ' + job.job_id.slice(0, 10));
      link.href = '#' + job.job_id;
      item.append(link);
      return item;
    });
    byId('history').replaceChildren(...items);
    byId('history-status').textContent = items.length ? '已读取 ' + items.length + ' 条记录。' : '还没有已保存的网页实验。';
  } catch {
    if (reading !== listRead) return;
    byId('history').replaceChildren();
    byId('history-status').textContent = '无法读取实验列表，请稍后刷新。';
  }
  if (selected) await loadJob(selected);
}

async function sendPending() {
  if (!pending || busy) return;
  busy = true;
  controls();
  status('正在确认本机实验编号…');
  try {
    const job = await request('', pending);
    if (!validId(job.job_id)) throw new Error('服务未返回有效实验编号。');
    setPending(null);
    invalidate();
    selected = job.job_id;
    location.hash = selected;
    renderJob(job);
    status('提交已确认。可关闭页面，稍后从实验列表继续查看。');
    await refresh();
  } catch (error) {
    if ([400, 403, 409].includes(error.status)) {
      setPending(null);
      status(error.message);
    } else {
      status('提交结果暂不确定。可重试同一请求，或刷新历史核对。');
    }
  } finally {
    busy = false;
    controls();
  }
}

form.addEventListener('input', invalidate);
form.addEventListener('change', () => { syncFields(); invalidate(); });
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || pending || !form.reportValidity()) return;
  busy = true;
  controls();
  status('正在预检全部窗口…');
  try {
    const envelope = {kind: value('kind'), spec: specFromForm()};
    const result = await request('/preview', envelope);
    if (JSON.stringify(envelope) === JSON.stringify({kind: value('kind'), spec: specFromForm()})) {
      showPreview(result);
      preview = envelope;
      status('预检完成，请核对后提交。');
    } else status('参数已修改，请重新预检。');
  } catch (error) { status(error.message); }
  finally { busy = false; controls(); }
});
byId('run').addEventListener('click', () => {
  if (!preview || busy || pending) return;
  try {
    setPending({...preview, request_id: crypto.randomUUID().replaceAll('-', '')});
  } catch {
    status('无法保存重试编号，请允许本站会话存储后再提交。');
    return;
  }
  sendPending();
});
byId('retry').addEventListener('click', sendPending);
byId('refresh').addEventListener('click', refresh);
byId('add-case').addEventListener('click', () => addRow('cases'));
byId('add-fold').addEventListener('click', () => addRow('folds'));
window.addEventListener('hashchange', () => {
  selected = location.hash.slice(1);
  byId('job-panel').hidden = true;
  loadJob(selected);
});

addRow('cases', {label: '参考滑点 5 基点', slippage: '5'});
addRow('cases', {label: '压力滑点 15 基点', slippage: '15'});
addRow('folds', {train_start:'2024-01-02',train_end:'2024-01-03',validation_start:'2024-01-04',validation_end:'2024-01-04'});
addRow('folds', {train_start:'2024-01-03',train_end:'2024-01-04',validation_start:'2024-01-05',validation_end:'2024-01-05'});
try {
  const saved = JSON.parse(sessionStorage.getItem(PENDING_KEY));
  if (saved && validId(saved.request_id)) {
    pending = saved;
    status('发现上次未确认的提交。请重试同一请求，页面不会自动重发。');
  }
} catch {}
selected = location.hash.slice(1);
controls();
refresh();
loadSources();
window.setInterval(refresh, 2500);
