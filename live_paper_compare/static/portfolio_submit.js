(function () {
  'use strict';

  var API = '/api/research/workflows';
  var PENDING_KEY = 'quant-portfolio-pending-v1';
  var MAX_STORAGE_CHARS = 16384;
  var MAX_PAYLOAD_CHARS = 12000;
  var MAX_POLLS = 160;
  var POLL_DELAY_MS = 2000;
  var TERMINAL = {completed: true, failed: true, timed_out: true, cancelled: true, interrupted: true};
  var ACTIVE = {queued: true, running: true};
  var LABELS = {
    queued: '排队中', running: '运行中', completed: '已完成', failed: '失败',
    timed_out: '超时', cancelled: '已取消', interrupted: '已中断'
  };
  var state = {
    preview: null,
    previewBusy: false,
    previewToken: 0,
    submitBusy: false,
    recoveryBusy: false,
    pending: null,
    jobId: '',
    jobRequestId: '',
    jobStatus: '',
    jobLoading: false,
    jobRead: 0,
    recoveryRead: 0,
    pollEpoch: 0,
    pollTimer: null,
    cancelBusy: false
  };

  function byId(name) { return document.getElementById(name); }
  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function ownKeysExactly(value, keys) {
    if (!isObject(value)) return false;
    var actual = Object.keys(value).sort();
    var expected = keys.slice().sort();
    return actual.length === expected.length && actual.every(function (key, index) { return key === expected[index]; });
  }
  function safeId(value) { return typeof value === 'string' && /^[0-9a-f]{32}$/.test(value); }
  function safeRequestId(value) {
    return typeof value === 'string' && /^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/.test(value);
  }
  function safeVersion(value) { return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value); }
  function validSymbol(value) {
    return typeof value === 'string' && /^(?:(?:000|001|002|003)\d{3}\.SZSE|(?:600|601|603|605)\d{3}\.SHSE)$/.test(value);
  }
  function isDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    var parts = value.split('-').map(Number);
    var date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
    return date.getUTCFullYear() === parts[0] && date.getUTCMonth() === parts[1] - 1 && date.getUTCDate() === parts[2];
  }
  function daysBetween(start, end) {
    var first = Date.parse(start + 'T00:00:00Z');
    var last = Date.parse(end + 'T00:00:00Z');
    return Math.floor((last - first) / 86400000) + 1;
  }
  function finiteNumber(value) { return typeof value === 'number' && Number.isFinite(value); }
  function validConfig(config) {
    if (!ownKeysExactly(config, ['symbols', 'start', 'end', 'interval', 'strategy', 'initial_cash', 'commission_rate', 'min_commission', 'slippage_bps', 'params'])) return false;
    if (!Array.isArray(config.symbols) || config.symbols.length < 2 || config.symbols.length > 4) return false;
    if (config.symbols.some(function (symbol) { return !validSymbol(symbol); }) || new Set(config.symbols).size !== config.symbols.length) return false;
    if (!isDate(config.start) || !isDate(config.end) || config.start > config.end ||
        config.start < '2024-01-01' || config.end > '2025-12-31' || daysBetween(config.start, config.end) > 31) return false;
    if (config.interval !== '1m' || (config.strategy !== 'buy_hold' && config.strategy !== 'sma_cross')) return false;
    if (!finiteNumber(config.initial_cash) || config.initial_cash <= 0 ||
        !finiteNumber(config.commission_rate) || config.commission_rate < 0 ||
        !finiteNumber(config.min_commission) || config.min_commission < 0 ||
        !finiteNumber(config.slippage_bps) || config.slippage_bps < 0) return false;
    var params = config.params;
    if (!ownKeysExactly(params, ['target_fraction', 'max_turnover_participation', 'fast', 'slow'])) return false;
    if (!finiteNumber(params.target_fraction) || params.target_fraction <= 0 || params.target_fraction > 1 ||
        !finiteNumber(params.max_turnover_participation) || params.max_turnover_participation <= 0 || params.max_turnover_participation > 0.1 ||
        !Number.isInteger(params.fast) || params.fast < 1 || !Number.isInteger(params.slow) || params.slow < 2 || params.fast >= params.slow) return false;
    return true;
  }
  function validPayload(payload) {
    return ownKeysExactly(payload, ['kind', 'spec', 'request_id']) && payload.kind === 'portfolio' &&
      safeRequestId(payload.request_id) && ownKeysExactly(payload.spec, ['config', 'expected_source_version']) &&
      validConfig(payload.spec.config) && safeVersion(payload.spec.expected_source_version);
  }
  function element(tag, text) {
    var node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function setStatus(message, kind) {
    var node = byId('portfolio-submit-status');
    node.textContent = String(message || '');
    node.className = 'submit-status' + (kind ? ' ' + kind : '');
  }
  function setJobStatus(message, kind) {
    var node = byId('portfolio-job-status');
    node.textContent = String(message || '');
    node.className = kind ? kind : '';
  }
  function clear(node) {
    if (typeof node.replaceChildren === 'function') node.replaceChildren();
    else node.textContent = '';
  }
  function controls() {
    var blocked = Boolean(state.pending || state.jobLoading || state.recoveryBusy || state.jobId && !TERMINAL[state.jobStatus]);
    byId('portfolio-preview-button').disabled = state.previewBusy || state.submitBusy || blocked;
    byId('portfolio-confirm-button').disabled = state.previewBusy || state.submitBusy || blocked || !state.preview;
    byId('portfolio-retry-button').hidden = !state.pending;
    byId('portfolio-retry-button').disabled = state.submitBusy || state.recoveryBusy;
    byId('portfolio-find-button').hidden = !state.pending;
    byId('portfolio-find-button').disabled = state.submitBusy || state.recoveryBusy;
    byId('portfolio-refresh-job').disabled = state.jobLoading || state.cancelBusy || !state.jobId;
    byId('portfolio-cancel-job').hidden = !ACTIVE[state.jobStatus];
    byId('portfolio-cancel-job').disabled = state.cancelBusy || state.jobLoading || !ACTIVE[state.jobStatus];
  }
  function hidePreview() {
    state.preview = null;
    byId('portfolio-preview-panel').hidden = true;
    clear(byId('portfolio-preview-cases'));
    byId('portfolio-preview-summary').textContent = '';
  }
  function invalidatePreview(message) {
    state.previewToken += 1;
    state.previewBusy = false;
    if (state.preview) hidePreview();
    setStatus(message || '表单已修改，请重新预检。');
    controls();
  }
  function numberField(name) {
    var raw = byId(name).value;
    if (typeof raw !== 'string' || !raw.trim()) throw new Error('请完整填写所有数值参数。');
    var value = Number(raw);
    if (!Number.isFinite(value)) throw new Error('数值参数必须是有限数字。');
    return value;
  }
  function readConfig() {
    var symbols = byId('portfolio-symbols').value.split(',').map(function (value) { return value.trim().toUpperCase(); }).filter(Boolean);
    var config = {
      symbols: symbols,
      start: byId('portfolio-start').value,
      end: byId('portfolio-end').value,
      interval: '1m',
      strategy: byId('portfolio-strategy').value,
      initial_cash: numberField('portfolio-cash'),
      commission_rate: numberField('portfolio-commission'),
      min_commission: numberField('portfolio-min-commission'),
      slippage_bps: numberField('portfolio-slippage'),
      params: {
        target_fraction: numberField('portfolio-target-fraction'),
        max_turnover_participation: numberField('portfolio-participation'),
        fast: numberField('portfolio-fast'),
        slow: numberField('portfolio-slow')
      }
    };
    if (!validConfig(config)) throw new Error('请检查标的、日期和参数范围；组合须含 2–4 个主板标的，日期须在 2024–2025 年且不超过 31 天。');
    return config;
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
          var error = new Error(typeof data.error === 'string' ? data.error.slice(0, 300) : '本机请求失败。');
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }
  function dateLabel(value) { return typeof value === 'string' ? value : '—'; }
  function showPreview(result, config) {
    var normalized = result.cases[0].config;
    var symbols = Array.isArray(normalized.symbols) ? normalized.symbols : config.symbols;
    var windowText = dateLabel(normalized.start) + ' 至 ' + dateLabel(normalized.end);
    var cash = finiteNumber(normalized.initial_cash) ? normalized.initial_cash : config.initial_cash;
    var fraction = isObject(normalized.params) && finiteNumber(normalized.params.target_fraction) ? normalized.params.target_fraction : config.params.target_fraction;
    byId('portfolio-preview-summary').textContent = '标的顺序：' + symbols.join(' → ') + '；区间：' + windowText +
      '；共享现金预算：' + cash.toLocaleString('zh-CN') + ' 元，资金比例上限 ' + fraction +
      '；任务时限：' + result.limits.wall_seconds + ' 秒；源码版本：' + result.source_version +
      '；配置 SHA-256：' + result.config_sha256 + '。';
    var item = element('li', '现金不足时按证券代码顺序分配；本次预检只校验配置，不读取行情、不启动任务。');
    byId('portfolio-preview-cases').replaceChildren(item);
    byId('portfolio-preview-panel').hidden = false;
  }
  function validPreview(result) {
    return isObject(result) && result.valid === true && result.plan_only === true &&
      result.mode === 'RESEARCH_ONLY' && result.paper_admission === false &&
      safeVersion(result.source_version) && safeVersion(result.config_sha256) &&
      isObject(result.limits) && result.limits.wall_seconds === 300 &&
      Array.isArray(result.cases) && result.cases.length === 1 && isObject(result.cases[0]) &&
      isObject(result.cases[0].config) && isObject(result.scope) && result.scope.portfolio_aggregation === true;
  }
  function requestId() {
    var source = window.crypto;
    if (!source) throw new Error('浏览器无法生成安全请求编号。');
    var value = typeof source.randomUUID === 'function' ? source.randomUUID().replace(/-/g, '').toLowerCase() : '';
    if (!safeRequestId(value) && typeof source.getRandomValues === 'function') {
      var bytes = new Uint8Array(16);
      source.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      value = Array.from(bytes).map(function (byte) { return byte.toString(16).padStart(2, '0'); }).join('');
    }
    if (!safeRequestId(value)) throw new Error('无法生成有效的 UUID4 请求编号。');
    return value;
  }
  function decodePayload(payloadText) {
    if (typeof payloadText !== 'string' || payloadText.length < 1 || payloadText.length > MAX_PAYLOAD_CHARS) return null;
    try {
      var payload = JSON.parse(payloadText);
      return validPayload(payload) ? payload : null;
    } catch (_error) { return null; }
  }
  function encodeSaved(payloadText, jobId) {
    var saved = JSON.stringify({version: 1, payload_json: payloadText, job_id: jobId || ''});
    if (saved.length > MAX_STORAGE_CHARS) throw new Error('待确认请求超过本地保存上限。');
    return saved;
  }
  function savePending(payloadText, jobId) {
    try {
      var saved = encodeSaved(payloadText, jobId);
      window.localStorage.setItem(PENDING_KEY, saved);
      return true;
    } catch (_error) { return false; }
  }
  function removeSaved() {
    try { window.localStorage.removeItem(PENDING_KEY); } catch (_error) {}
  }
  function readSaved() {
    var raw;
    try { raw = window.localStorage.getItem(PENDING_KEY); } catch (_error) { setStatus('无法读取本地恢复记录；请确认浏览器本地存储可用。', 'error'); return; }
    if (!raw) return;
    if (typeof raw !== 'string' || raw.length > MAX_STORAGE_CHARS) {
      removeSaved();
      setStatus('本地恢复记录超过长度上限，已忽略；不会自动提交。', 'error');
      return;
    }
    try {
      var saved = JSON.parse(raw);
      if (!ownKeysExactly(saved, ['version', 'payload_json', 'job_id']) || saved.version !== 1 ||
          typeof saved.payload_json !== 'string' || (saved.job_id !== '' && !safeId(saved.job_id))) throw new Error('invalid_saved');
      var payload = decodePayload(saved.payload_json);
      if (!payload) throw new Error('invalid_payload');
      state.pending = saved.job_id ? null : {payload: payload, payloadText: saved.payload_json};
      state.jobId = saved.job_id || '';
      state.jobRequestId = payload.request_id;
      state.jobStatus = '';
      if (state.jobId) {
        state.jobLoading = true;
        byId('portfolio-job-panel').hidden = false;
        clear(byId('portfolio-job-cases'));
        setJobStatus('正在恢复上次确认的任务状态…');
        setStatus('发现已确认的任务，只读取当前状态。');
        controls();
        refreshJob(state.jobId, false);
      } else {
        setStatus('发现未确认的原请求。正在只读查找；不会自动重复提交。');
        controls();
        recoverPending(false);
      }
    } catch (_error) {
      removeSaved();
      state.pending = null;
      state.jobId = '';
      setStatus('本地恢复记录结构或编号无效，已忽略；不会执行其中内容。', 'error');
      controls();
    }
  }
  function clearTimer() {
    if (state.pollTimer !== null) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
  function stopPolling() {
    clearTimer();
    state.pollEpoch += 1;
  }
  function clearJobView(message) {
    byId('portfolio-job-panel').hidden = false;
    clear(byId('portfolio-job-cases'));
    setJobStatus(message || '');
  }
  function statusName(status) { return LABELS[status] || '状态待确认'; }
  function safeError(value) {
    if (typeof value === 'string') return value.slice(0, 300);
    if (isObject(value) && typeof value.message === 'string') return value.message.slice(0, 300);
    return '';
  }
  function addReportLink(parent, runId) {
    var link = element('a', '打开已完成报告');
    link.href = '/portfolio#' + runId;
    link.addEventListener('click', function (event) {
      if (window.PortfolioReport && typeof window.PortfolioReport.openRun === 'function') {
        event.preventDefault();
        window.PortfolioReport.openRun(runId, true, true);
      }
    });
    parent.append(link);
  }
  function renderJob(job) {
    var status = job.status;
    state.jobStatus = status;
    state.jobLoading = false;
    byId('portfolio-job-panel').hidden = false;
    var note = statusName(status);
    if (job.cancel_requested === true && ACTIVE[status]) note += ' · 已请求取消，等待任务确认';
    var reason = safeError(job.error);
    if (reason && (status === 'failed' || status === 'timed_out' || status === 'interrupted')) note += ' · ' + reason;
    setJobStatus(note, status === 'failed' ? 'error' : status === 'completed' ? 'ok' : '');
    clear(byId('portfolio-job-cases'));
    var cases = status === 'completed' && isObject(job.receipt) && Array.isArray(job.receipt.cases) ? job.receipt.cases.slice(0, 4) : [];
    cases.forEach(function (item, index) {
      if (!isObject(item)) return;
      var row = element('li');
      var caseStatus = typeof item.run_status === 'string' ? item.run_status : typeof item.status === 'string' ? item.status : 'unknown';
      row.append(element('span', '标的组合子任务 ' + (index + 1) + ' · ' + statusName(caseStatus)));
      var runId = safeId(item.run_id) ? item.run_id : '';
      var everyCaseStatusCompleted = caseStatus === 'completed' &&
        (!Object.prototype.hasOwnProperty.call(item, 'status') || item.status === 'completed') &&
        (!Object.prototype.hasOwnProperty.call(item, 'run_status') || item.run_status === 'completed');
      if (everyCaseStatusCompleted && runId) addReportLink(row, runId);
      byId('portfolio-job-cases').append(row);
    });
    controls();
  }
  function validJob(job, expectedId) {
    return isObject(job) && safeId(job.job_id) && job.job_id === expectedId && job.kind === 'portfolio' &&
      job.mode === 'RESEARCH_ONLY' && job.paper_admission === false &&
      safeRequestId(job.request_id) && job.request_id === state.jobRequestId &&
      Object.prototype.hasOwnProperty.call(LABELS, job.status);
  }
  function schedulePoll(jobId, epoch, count) {
    if (!ACTIVE[state.jobStatus]) return;
    if (count >= MAX_POLLS) {
      setJobStatus(statusName(state.jobStatus) + ' · 自动查询已暂停，请点击刷新状态。');
      return;
    }
    clearTimer();
    state.pollTimer = window.setTimeout(function () {
      state.pollTimer = null;
      if (epoch !== state.pollEpoch || state.jobId !== jobId) return;
      readJob(jobId, epoch, count + 1);
    }, POLL_DELAY_MS);
  }
  function readJob(jobId, epoch, count) {
    if (!safeId(jobId) || state.jobId !== jobId) return Promise.resolve(null);
    var reading = ++state.jobRead;
    state.jobLoading = true;
    state.jobStatus = '';
    clearJobView('正在读取最新任务状态…');
    controls();
    return fetchJson(API + '/' + encodeURIComponent(jobId)).then(function (job) {
      if (reading !== state.jobRead || epoch !== state.pollEpoch || state.jobId !== jobId) return null;
      if (!validJob(job, jobId)) throw new Error('任务状态响应格式无效。');
      renderJob(job);
      if (ACTIVE[job.status]) schedulePoll(jobId, epoch, count);
      else stopPolling();
      return job;
    }).catch(function () {
      if (reading !== state.jobRead || epoch !== state.pollEpoch || state.jobId !== jobId) return null;
      state.jobLoading = false;
      state.jobStatus = '';
      clearJobView('读取失败，当前任务状态未知；请显式刷新。');
      controls();
      stopPolling();
      return null;
    });
  }
  function refreshJob(jobId, fromPoll) {
    if (!safeId(jobId)) return Promise.resolve(null);
    if (!fromPoll) stopPolling();
    var epoch = state.pollEpoch;
    return readJob(jobId, epoch, 0);
  }
  function acceptJob(job) {
    if (!isObject(job) || !safeId(job.job_id) || job.kind !== 'portfolio' ||
        job.mode !== 'RESEARCH_ONLY' || job.paper_admission !== false ||
        !state.pending || job.request_id !== state.pending.payload.request_id ||
        !Object.prototype.hasOwnProperty.call(LABELS, job.status)) {
      state.pending = {payload: state.pending.payload, payloadText: state.pending.payloadText};
      setStatus('服务未返回可核验任务编号，提交结果暂不确定；可查找或重试原请求。', 'error');
      controls();
      return false;
    }
    var pending = state.pending;
    state.jobId = job.job_id;
    state.jobRequestId = pending.payload.request_id;
    state.jobStatus = job.status;
    state.jobLoading = false;
    state.pending = null;
    stopPolling();
    savePending(pending.payloadText, job.job_id);
    renderJob(job);
    setStatus('本机组合任务已确认；后续状态以只读查询为准。', 'ok');
    if (ACTIVE[job.status]) schedulePoll(job.job_id, state.pollEpoch, 0);
    controls();
    return true;
  }
  function clearRejectedRequest(message) {
    state.pending = null;
    removeSaved();
    hidePreview();
    setStatus(message, 'error');
    controls();
  }
  function postExactPending() {
    if (!state.pending || state.submitBusy || state.recoveryBusy) return Promise.resolve(null);
    var pending = state.pending;
    var payload = decodePayload(pending.payloadText);
    if (!payload || payload.request_id !== pending.payload.request_id) {
      clearRejectedRequest('原请求内容或编号无效，未发送请求。请重新填写并预检。');
      return Promise.resolve(null);
    }
    state.submitBusy = true;
    controls();
    setStatus('正在使用原请求编号确认任务…');
    return fetchJson(API, 'POST', pending.payloadText).then(function (job) {
      if (state.pending !== pending) return null;
      acceptJob(job);
      return job;
    }).catch(function (error) {
      if (state.pending !== pending) return null;
      if (error.status === 409) {
        clearRejectedRequest('服务端源码版本已变化或预期版本冲突；预检已失效，请重新预检。');
      } else if (error.status === 400 || error.status === 403) {
        clearRejectedRequest('服务端拒绝了该请求（HTTP ' + error.status + '）；请检查参数后重新预检。');
      } else {
        setStatus('提交响应未知；原请求已保存。可查找任务，或重试完全相同的请求。', 'error');
      }
      return null;
    }).finally(function () {
      state.submitBusy = false;
      controls();
    });
  }
  function recoverPending(explicit) {
    if (!state.pending || state.recoveryBusy) return Promise.resolve(null);
    var pending = state.pending;
    var reading = ++state.recoveryRead;
    state.recoveryBusy = true;
    controls();
    if (explicit) setStatus('正在只读查找相同请求编号…');
    return fetchJson(API).then(function (result) {
      if (reading !== state.recoveryRead || state.pending !== pending) return null;
      if (!isObject(result) || !Array.isArray(result.jobs) || result.jobs.length > 1000) throw new Error('invalid_jobs');
      var match = result.jobs.find(function (job) {
        return isObject(job) && job.kind === 'portfolio' && job.request_id === pending.payload.request_id && safeId(job.job_id);
      });
      if (!match) {
        setStatus('当前任务列表未找到该请求。没有自动重发；可重试原请求或稍后再次查找。');
        return null;
      }
      state.pending = null;
      state.jobId = match.job_id;
      state.jobRequestId = pending.payload.request_id;
      state.jobStatus = '';
      state.jobLoading = true;
      savePending(pending.payloadText, match.job_id);
      setStatus('已从本机任务列表找回同一请求，只读取任务状态。', 'ok');
      controls();
      return refreshJob(match.job_id, false);
    }).catch(function () {
      if (reading === state.recoveryRead && state.pending === pending) setStatus('只读查找暂时失败；原请求仍保存，不会自动重发。');
      return null;
    }).finally(function () {
      if (reading === state.recoveryRead) {
        state.recoveryBusy = false;
        controls();
      }
    });
  }

  byId('portfolio-submit-form').addEventListener('input', function () { invalidatePreview('表单已修改，请重新预检。'); });
  byId('portfolio-submit-form').addEventListener('change', function () { invalidatePreview('表单已修改，请重新预检。'); });
  byId('portfolio-submit-form').addEventListener('submit', function (event) {
    event.preventDefault();
    if (state.previewBusy || state.submitBusy || state.recoveryBusy || state.pending || state.jobLoading || state.jobId && !TERMINAL[state.jobStatus]) return;
    var config;
    try {
      if (typeof byId('portfolio-submit-form').reportValidity === 'function' && !byId('portfolio-submit-form').reportValidity()) return;
      config = readConfig();
    } catch (error) { setStatus(error.message, 'error'); return; }
    var signature = JSON.stringify(config);
    var token = ++state.previewToken;
    state.previewBusy = true;
    hidePreview();
    controls();
    setStatus('正在预检组合配置；此请求不会读取行情或启动任务。');
    fetchJson(API + '/preview', 'POST', JSON.stringify({kind: 'portfolio', spec: {config: config}})).then(function (result) {
      if (token !== state.previewToken) return;
      var current;
      try { current = JSON.stringify(readConfig()); } catch (_error) { current = ''; }
      if (current !== signature) { setStatus('预检期间表单已修改；请重新预检。', 'error'); return; }
      if (!validPreview(result)) {
        if (isObject(result) && result.valid === false && typeof result.error === 'string') setStatus('预检未通过：' + result.error.slice(0, 300), 'error');
        else setStatus('服务返回的预检摘要格式不完整，未启用提交。', 'error');
        return;
      }
      showPreview(result, config);
      state.preview = {signature: signature, config: config, result: result};
      setStatus('预检完成。请核对摘要后显式确认；当前尚未创建任务。', 'ok');
    }).catch(function (error) {
      if (token === state.previewToken) setStatus('预检失败：' + (error.message || '本机请求失败。'), 'error');
    }).finally(function () {
      if (token === state.previewToken) state.previewBusy = false;
      controls();
    });
  });
  byId('portfolio-confirm-button').addEventListener('click', function () {
    if (!state.preview || state.previewBusy || state.submitBusy || state.recoveryBusy || state.pending || state.jobLoading || state.jobId && !TERMINAL[state.jobStatus]) return;
    var current;
    try { current = JSON.stringify(readConfig()); } catch (error) { invalidatePreview(error.message); return; }
    if (current !== state.preview.signature) { invalidatePreview('表单已修改，请重新预检。'); return; }
    var payload;
    var payloadText;
    try {
      payload = {
        kind: 'portfolio',
        spec: {config: state.preview.config, expected_source_version: state.preview.result.source_version},
        request_id: requestId()
      };
      if (!validPayload(payload)) throw new Error('请求内容校验失败。');
      payloadText = JSON.stringify(payload);
      if (!savePending(payloadText, '')) throw new Error('无法将确切请求保存到本地，未提交任务。');
    } catch (error) { setStatus(error.message || '无法安全保存提交请求，未发送。', 'error'); return; }
    stopPolling();
    state.jobRead += 1;
    state.jobId = '';
    state.jobRequestId = '';
    state.jobStatus = '';
    clearJobView('新请求等待确认；旧任务状态已清除。');
    state.pending = {payload: payload, payloadText: payloadText};
    hidePreview();
    controls();
    postExactPending();
  });
  byId('portfolio-retry-button').addEventListener('click', function () { postExactPending(); });
  byId('portfolio-find-button').addEventListener('click', function () { recoverPending(true); });
  byId('portfolio-refresh-job').addEventListener('click', function () { if (state.jobId) refreshJob(state.jobId, false); });
  byId('portfolio-cancel-job').addEventListener('click', function () {
    if (!safeId(state.jobId) || !ACTIVE[state.jobStatus] || state.cancelBusy || state.jobLoading) return;
    var jobId = state.jobId;
    state.cancelBusy = true;
    stopPolling();
    state.jobStatus = '';
    clearJobView('正在请求取消；任务是否终止以随后读取的状态为准。');
    controls();
    fetchJson(API + '/' + encodeURIComponent(jobId) + '/cancel', 'POST', '{}').catch(function () { return null; }).then(function () {
      if (state.jobId !== jobId) return null;
      return refreshJob(jobId, false);
    }).finally(function () {
      state.cancelBusy = false;
      controls();
    });
  });
  window.addEventListener('pagehide', stopPolling);
  window.addEventListener('pageshow', function(event) {
    if (!event || event.persisted !== true) return;
    state.previewToken += 1;
    state.previewBusy = false;
    hidePreview();
    state.submitBusy = false;
    state.cancelBusy = false;
    state.recoveryRead += 1;
    state.recoveryBusy = false;
    setStatus('页面已恢复；已有任务仅查询状态，新任务需要重新预检。');
    if (state.jobId) refreshJob(state.jobId, false);
    else if (state.pending) recoverPending(false);
    controls();
  });
  controls();
  readSaved();
})();
