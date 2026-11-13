const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('live_paper_compare/static/research_workflows.html', 'utf8');
const script = fs.readFileSync('live_paper_compare/static/research_workflows.js', 'utf8');
const JOB = '0123456789ab4def8123456789abcdef';
const REQUEST = '12345678123442348234123456781234';

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.handlers = {};
    this.value = '';
    this.disabled = false;
    this.hidden = false;
  }
  append(...children) {
    for (const child of children) {
      child.parentElement = this;
      this.children.push(child);
    }
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  querySelectorAll(query) {
    const match = child => query.split(',').some(part => {
      const selector = part.trim();
      if (selector === '[data-field]') return Boolean(child.dataset.field);
      const dataField = selector.match(/^\[data-field="([^"]+)"\]$/);
      return dataField ? child.dataset.field === dataField[1] : child.tagName === selector.toUpperCase();
    });
    return this.children.flatMap(child => [...(match(child) ? [child] : []), ...child.querySelectorAll(query)]);
  }
  querySelector(query) { return this.querySelectorAll(query)[0]; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  fire(name) { return this.handlers[name]?.({preventDefault() {}}); }
  reportValidity() { return true; }
}

function page(storage, fetch) {
  const elements = Object.fromEntries(Array.from(html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g),
    match => [match[2], new Node(match[1])]));
  const defaults = {kind:'batch',strategy:'sma_cross',symbol:'000001.SZSE',cash:'100000',
    fraction:'0.5',participation:'0.01',commission:'0.00025',minimum:'5',slippage:'5',
    fast:'5',slow:'20',start:'2024-01-02',end:'2024-01-05',
    symbols:'000001.SZSE\n000002.SZSE'};
  for (const [id, value] of Object.entries(defaults)) elements[id].value = value;
  const document = {getElementById: id => elements[id], createElement: tag => new Node(tag)};
  const sessionStorage = {getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)};
  vm.runInNewContext(script, {document, sessionStorage, location:{hash:''}, fetch,
    crypto:{randomUUID:()=> '12345678-1234-4234-8234-123456781234'},
    window:{addEventListener(){},setInterval(){}}});
  return elements;
}

const ok = body => ({ok:true, json:async()=>body});
const yieldToFetch = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const storage = new Map();
  const posted = [];
  const fetch = async (url, options = {}) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    if (url.endsWith('/custom-sources')) return ok({sources:[]});
    if (url.endsWith('/preview')) {
      assert.equal(body.kind,'batch');
      assert.deepEqual(body.spec.cases.map(x => x.slippage_bps),[5,15]);
      return ok({cases:body.spec.cases.map(x => ({label:x.label, config:{...body.spec.base,...x}}))});
    }
    if (body) {
      posted.push(body);
      if (posted.length === 1) throw new Error('lost response');
      return ok({job_id:JOB,experiment_id:JOB,status:'queued',plan:{cases:[]}});
    }
    if (url.endsWith(JOB)) return ok({job_id:JOB,experiment_id:JOB,status:'queued',plan:{cases:[]}});
    return ok({jobs:[]});
  };
  const first = page(storage, fetch);
  assert.equal(first.run.disabled,true);
  await first['workflow-form'].fire('submit');
  assert.equal(first.run.disabled,false);
  first['workflow-form'].fire('input');
  assert.equal(first.run.disabled,true);
  await first['workflow-form'].fire('submit');
  first.run.fire('click');
  await yieldToFetch();
  assert.equal(posted.length,1);
  assert.equal(posted[0].request_id,REQUEST);
  assert.equal(first.retry.hidden,false);
  assert.equal(storage.size,1);

  const second = page(storage,fetch);
  await yieldToFetch();
  assert.equal(posted.length,1,'reload cannot submit');
  assert.equal(second.retry.hidden,false);
  await second.retry.fire('click');
  assert.deepEqual(posted[1],posted[0]);
  assert.equal(second.retry.hidden,true);
  assert.equal(storage.size,0);

  const symbolPage = page(new Map(), async (url, options = {}) => {
    if (url.endsWith('/custom-sources')) return ok({sources:[]});
    if (!url.endsWith('/preview')) return ok({jobs:[]});
    const body = JSON.parse(options.body);
    assert.equal(body.kind, 'symbols');
    assert.deepEqual(body.spec.symbols, ['000001.SZSE', '000002.SZSE']);
    assert.equal(body.spec.base.symbol, undefined);
    assert.equal(body.spec.base.start, '2024-01-02');
    return ok({cases:body.spec.symbols.map(symbol => ({
      label:symbol,config:{...body.spec.base,symbol},
    }))});
  });
  symbolPage.kind.value = 'symbols';
  symbolPage['workflow-form'].fire('change');
  assert.equal(symbolPage['symbols-fields'].hidden, false);
  assert.equal(symbolPage['batch-fields'].hidden, true);
  assert.equal(symbolPage['rolling-fields'].hidden, true);
  assert.equal(symbolPage['date-fields'].hidden, false);
  await symbolPage['workflow-form'].fire('submit');
  assert.equal(symbolPage.run.disabled, false);
  console.log('PASS: explicit preview, form edits invalidate preview, lost response persists, reload is read-only, same request retry');
})().catch(error => { console.error(error); process.exitCode = 1; });
