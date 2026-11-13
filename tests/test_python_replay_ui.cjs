'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const htmlPath = path.join(__dirname, '..', 'live_paper_compare', 'static', 'python_replay.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/i);
assert.ok(scriptMatch, 'page should contain its inline script');

class FakeClassList {
  constructor(element) { this.element = element; }
  values() { return new Set(String(this.element.className || '').split(/\s+/).filter(Boolean)); }
  write(values) { this.element.className = Array.from(values).join(' '); }
  add(...names) { const values = this.values(); names.forEach(name => values.add(name)); this.write(values); }
  remove(...names) { const values = this.values(); names.forEach(name => values.delete(name)); this.write(values); }
  contains(name) { return this.values().has(name); }
  toggle(name, force) {
    const values = this.values();
    const add = force === undefined ? !values.has(name) : Boolean(force);
    if (add) values.add(name); else values.delete(name);
    this.write(values);
    return add;
  }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName || 'div').toUpperCase();
    this.children = [];
    this.attributes = Object.create(null);
    this.listeners = Object.create(null);
    this.className = '';
    this.classList = new FakeClassList(this);
    this.style = Object.create(null);
    this.value = '';
    this.disabled = false;
    this._textContent = '';
  }
  set textContent(value) { this._textContent = value == null ? '' : String(value); this.children = []; }
  get textContent() { return this._textContent + this.children.map(child => child.textContent || '').join(''); }
  append(...items) {
    items.forEach(item => {
      const child = typeof item === 'string' ? new FakeElement('#text') : item;
      if (typeof item === 'string') child.textContent = item;
      child.parentNode = this;
      this.children.push(child);
    });
  }
  replaceChildren(...items) { this.children = []; this._textContent = ''; if (items.length) this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { (this.listeners[name] || (this.listeners[name] = [])).push(callback); }
  querySelectorAll(selector) {
    if (selector === 'button[data-account-id]') {
      return this.children.filter(item => item.tagName === 'BUTTON' && item.attributes['data-account-id']);
    }
    return [];
  }
}

function makeDocument() {
  const ids = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    if (!ids.has(match[1])) ids.set(match[1], new FakeElement('div'));
  }
  return {
    getElementById(name) {
      if (!ids.has(name)) ids.set(name, new FakeElement('div'));
      return ids.get(name);
    },
    createElement(tag) { return new FakeElement(tag); },
    createElementNS(_namespace, tag) { return new FakeElement(tag); },
  };
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function makeStorage() {
  const data = new Map();
  return {
    getItem(key) { return data.has(key) ? data.get(key) : null; },
    setItem(key, value) { data.set(key, String(value)); },
    removeItem(key) { data.delete(key); },
  };
}

const ACCOUNT_ID = 'a'.repeat(32);
const RUN_ID = 'b'.repeat(32);
const STRATEGY_VERSION = 'c'.repeat(64);
const CODE_HASH = 'd'.repeat(64);
const PENDING_KEY = 'python-replay-pending-step-v1';
const BASE = '/api/research/python-replays';
let uuidCounter = 0;

function makeFixture() {
  const account = {
    record: {
      account_id: ACCOUNT_ID,
      run_id: RUN_ID,
      label: 'mock account',
      status: 'running',
      revision: 4,
      cursor: 0,
      total_bars: 4,
      remaining_bars: 4,
      current_equity: 100000,
      finalized: false,
      code_current: true,
      continuation_allowed: true,
      mode: 'RESEARCH_ONLY',
      paper_admission: false,
      kind: 'historical_python_replay',
      strategy: 'custom_python',
      strategy_version: STRATEGY_VERSION,
      source_hashes: {},
      dataset_hashes: [],
      code_hash: CODE_HASH,
      created_at: '2026-11-03T01:00:00+00:00',
      updated_at: '2026-11-03T01:00:00+00:00',
    },
    config: { symbol: '000001.SZSE', strategy: 'custom_python', start: '2024-01-02', end: '2024-01-05' },
    source: { source_manifest: { mode: 'RESEARCH_ONLY' } },
    data_readiness: { status: 'mismatch' },
    equity: [{ source_datetime: null, equity: 100000, cash: 100000, quantity: 0 }],
    orders: [],
    trades: [],
    positions: [],
    bars: [],
    events: [],
  };
  const requests = new Map();
  const behaviors = [];
  let failList = false;
  let failDetail = false;
  let stepCalls = 0;
  function detail() {
    const result = {
      equity: clone(account.equity),
      orders: clone(account.orders),
      trades: clone(account.trades),
      positions: clone(account.positions),
      bars: clone(account.bars),
    };
    return { ...clone(account), result };
  }
  function response(body, status = 200) {
    return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body) };
  }
  async function fetchMock(url, options = {}) {
    const method = options.method || 'GET';
    const target = String(url);
    if (target === '/api/research/runs' && method === 'GET') {
      return response({ runs: [{
        run_id: RUN_ID, label: 'mock source', status: 'completed',
        strategy: 'custom_python', backend: 'isolated_python',
        strategy_version: STRATEGY_VERSION, symbol: '000001.SZSE',
      }] });
    }
    if (target === BASE && method === 'GET') {
      if (failList) return response({ error: 'temporary list failure' }, 503);
      return response({ accounts: [clone(account.record)] });
    }
    if (target === BASE + '/' + ACCOUNT_ID && method === 'GET') {
      if (failDetail) return response({ error: 'temporary detail failure' }, 503);
      return response(detail());
    }
    if (target === BASE + '/' + ACCOUNT_ID + '/step' && method === 'POST') {
      stepCalls += 1;
      const payload = JSON.parse(options.body);
      if (requests.has(payload.request_id)) {
        const saved = requests.get(payload.request_id);
        assert.equal(JSON.stringify(payload), saved.payload, 'retry must keep the original step payload');
        return response({ ...detail(), ...saved.ack, replayed: true });
      }
      const behavior = behaviors.shift();
      if (!behavior) throw new Error('test is missing an explicit step response');
      return behavior({ payload, account, requests, detail, response });
    }
    throw new Error('unexpected mock request: ' + method + ' ' + target);
  }
  return {
    account, behaviors, fetchMock, response, requests, get stepCalls() { return stepCalls; },
    set failList(value) { failList = value; },
    set failDetail(value) { failDetail = value; },
  };
}

function makeEnvironment(fixture, storage, hash) {
  const document = makeDocument();
  const listeners = Object.create(null);
  const window = {
    location: { hash: hash || '', search: '' },
    crypto: { randomUUID() { uuidCounter += 1; return '00000000-0000-4000-8000-' + String(uuidCounter).padStart(12, '0'); } },
    localStorage: storage,
    addEventListener(name, callback) { (listeners[name] || (listeners[name] = [])).push(callback); },
  };
  const context = vm.createContext({ window, document, fetch: fixture.fetchMock, URLSearchParams, console });
  vm.runInContext(scriptMatch[1], context, { filename: 'python_replay.html' });
  return { window, document, page: window.PythonReplayPage };
}

async function run() {
  const storage = makeStorage();
  const fixture = makeFixture();
  fixture.behaviors.push(({ payload, account, requests, detail }) => {
    const originalCursor = account.record.cursor;
    const originalRevision = account.record.revision;
    account.record.cursor += 1;
    account.record.remaining_bars = account.record.total_bars - account.record.cursor;
    account.record.revision += 1;
    account.record.current_equity += 1;
    account.record.updated_at = '2026-11-03T01:01:00+00:00';
    account.equity.push({
      source_datetime: '2024-01-02 09:31:00',
      equity: account.record.current_equity,
      cash: account.record.current_equity,
      quantity: 0,
    });
    const ack = {
      request_id: payload.request_id,
      original_revision: originalRevision,
      original_cursor: originalCursor,
      applied_revision: account.record.revision,
      applied_cursor: account.record.cursor,
      bars_processed: 1,
      status_at_apply: 'running',
    };
    requests.set(payload.request_id, { payload: JSON.stringify(payload), ack });
    throw new TypeError('connection dropped after commit');
  });

  const first = makeEnvironment(fixture, storage, '#' + ACCOUNT_ID);
  await first.page.ready;
  assert.equal(first.page.state.detail.record.cursor, 0);
  const uncertain = await first.page.issueStep(1);
  assert.equal(uncertain.ok, false);
  assert.ok(first.page.state.pendingStep, 'network uncertainty must retain the pending step');
  const savedEnvelope = JSON.parse(storage.getItem(PENDING_KEY));
  assert.equal(savedEnvelope.account_id, ACCOUNT_ID);
  const originalPayload = clone(savedEnvelope.payload);
  assert.equal(fixture.stepCalls, 1);

  const reopened = makeEnvironment(fixture, storage, '');
  await reopened.page.ready;
  assert.equal(reopened.page.state.selectedId, ACCOUNT_ID, 'reopen should return to the pending step account');
  assert.equal(reopened.page.state.pendingStep.payload.request_id, originalPayload.request_id);
  assert.equal(reopened.page.state.detail.record.cursor, 1, 'the refreshed server checkpoint should be displayed');
  assert.equal(fixture.stepCalls, 1, 'opening a page must not resend an uncertain step');
  const replayed = await reopened.page.retryStep();
  assert.equal(replayed.ok, true, 'same-ID retry should receive the saved acknowledgment');
  assert.equal(reopened.page.state.pendingStep, null);
  assert.equal(storage.getItem(PENDING_KEY), null, 'matching acknowledgment should clear browser storage');
  assert.equal(fixture.stepCalls, 2);

  fixture.behaviors.push(({ response }) => response({
    error: 'request_id was already used with a different payload',
    code: 'PythonReplayAccountError',
    reason: 'request_id was already used with a different payload',
  }, 400));
  const unknown400 = await reopened.page.issueStep(1);
  assert.equal(unknown400.ok, false);
  assert.ok(reopened.page.state.pendingStep, 'an unclassified 400 must keep the same pending request');
  const retainedAfterUnknown400 = JSON.parse(storage.getItem(PENDING_KEY));
  assert.equal(retainedAfterUnknown400.payload.request_id, reopened.page.state.pendingStep.payload.request_id);
  const retainedPayload = JSON.stringify(retainedAfterUnknown400.payload);

  fixture.behaviors.push(({ response }) => response({
    error: 'stale revision',
    code: 'PythonReplayRevisionConflict',
    reason: 'stale revision',
  }, 409));
  const rejected409 = await reopened.page.retryStep();
  assert.equal(rejected409.rejected, true);
  assert.equal(reopened.page.state.pendingStep, null, 'known 409 rejection must release the pending step');
  assert.equal(storage.getItem(PENDING_KEY), null);
  assert.match(reopened.document.getElementById('operation-status').textContent, /检查点未按此请求推进/);
  assert.equal(reopened.page.state.detail.record.cursor, 1, 'the rejection path should refresh the account detail');
  assert.equal(fixture.stepCalls, 4);
  assert.equal(retainedPayload, JSON.stringify({ expected_revision: 5, steps: 1, request_id: retainedAfterUnknown400.payload.request_id }));

  fixture.behaviors.push(({ response }) => response({
    error: 'guest strategy callback failed; checkpoint was not committed',
    code: 'PythonReplayAccountError',
    reason: 'guest strategy callback failed; checkpoint was not committed',
  }, 400));
  const rejected400 = await reopened.page.issueStep(1);
  assert.equal(rejected400.rejected, true, 'recognized pre-commit 400 errors should release the pending step');
  assert.equal(reopened.page.state.pendingStep, null);
  assert.equal(storage.getItem(PENDING_KEY), null);
  assert.equal(fixture.stepCalls, 5);

  fixture.behaviors.push(({ response }) => response({
    error: 'stale revision',
    code: 'PythonReplayRevisionConflict',
    reason: 'stale revision',
  }, 409));
  fixture.failDetail = true;
  const rejectedWithoutRefresh = await reopened.page.issueStep(1);
  assert.equal(rejectedWithoutRefresh.rejected, true);
  assert.equal(reopened.page.state.pendingStep, null);
  assert.equal(reopened.page.state.detail, null, 'failed detail refresh must discard stale account data');
  assert.equal(reopened.page.state.detailReadBlocked, true);
  assert.equal(reopened.document.getElementById('step-one').disabled, true);
  assert.equal(reopened.document.getElementById('create-account').disabled, true);
  assert.match(reopened.document.getElementById('operation-status').textContent, /已停用相关操作/);

  fixture.failDetail = false;
  await reopened.page.openAccount(ACCOUNT_ID, false);
  assert.equal(reopened.page.state.detailReadBlocked, false, 'successful detail refresh should unlock the account');
  assert.equal(reopened.page.state.detail.record.cursor, 1);

  fixture.failList = true;
  await assert.rejects(reopened.page.refreshAccounts());
  assert.deepEqual(Array.from(reopened.page.state.accounts), [], 'failed account-list refresh must clear stale rows');
  assert.equal(reopened.page.state.accountsLoaded, false);
  assert.match(reopened.document.getElementById('account-list-status').textContent, /已清除旧列表显示/);

  console.log('python replay UI retry-state checks passed (mock DOM and local fetch only)');
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
