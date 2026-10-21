'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../frontend/marketing-studio.js'), 'utf8');
const keys = ['promotional_copy','product_description','social_caption','ad_headline'];
function setup(respond, clipboard = async () => {}) {
    const elements = new Map();
    function get(id) {
        if (!elements.has(id)) elements.set(id, {
            value: '', textContent: '', disabled: false, hidden: true, handlers: {},
            classList: { add() { get(id).hidden = true; }, remove() { get(id).hidden = false; } },
            addEventListener(event, fn) { this.handlers[event] = fn; },
            setAttribute(name, value) { this[name] = value; },
            focus() { this.focused = true; }, select() { this.selected = true; }
        });
        return elements.get(id);
    }
    const buttons = keys.map(key => { const b = get('copy_' + key); b.dataset = { marketingCopy: key }; return b; });
    const calls = [], events = [], listeners = new Map();
    vm.runInNewContext(script, {
        document: { getElementById: get, querySelectorAll: () => buttons, addEventListener: (name, fn) => listeners.set(name, fn), dispatchEvent: event => events.push(event) },
        CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
        navigator: { clipboard: { writeText: clipboard } }, TypeError,
        fetch: async (url, options) => { calls.push({ url, options }); return respond(); }
    });
    get('marketingName').value = 'Canvas tote';
    get('marketingDescription').value = 'Zip closure';
    get('marketingAudience').value = 'Commuters';
    get('marketingTone').value = 'friendly';
    get('marketingGoal').value = 'sales';
    return { get, calls, events, ended: () => listeners.get('customer-session-ended')(), submit: () => get('marketingForm').handlers.submit({ preventDefault() {} }),
        copy: key => get('copy_' + key).handlers.click() };
}
function response(status, body) { return { status, ok: status < 400, json: async () => body }; }
function pack() { return { success: true, outputs: Object.fromEntries(keys.map(key => [key, '<b>Synthetic ' + key + '</b>'])), quota: { used: 1, limit: 100 }, save_token: 'synthetic-proof' }; }
test('form submits all five fields and displays four plain text copyable outputs', async () => {
    let copied;
    const ui = setup(() => response(200, pack()), async text => { copied = text; });
    await ui.submit();
    const request = ui.calls[0];
    assert.equal(request.url, '/api/marketing/generate');
    assert.equal(request.options.credentials, 'same-origin');
    assert.deepEqual(Object.keys(JSON.parse(request.options.body)).sort(), ['audience','description','goal','product_name','tone']);
    keys.forEach(key => assert.equal(ui.get('marketing_' + key).value, '<b>Synthetic ' + key + '</b>'));
    assert.equal(ui.get('marketingResults').hidden, false);
    assert.deepEqual(ui.events.map(event => event.type), ['marketing-pack-pending','marketing-pack-ready']);
    assert.equal(ui.events[1].detail.save_token, 'synthetic-proof');
    await ui.copy('social_caption');
    assert.equal(copied, '<b>Synthetic social_caption</b>');
});
test('pending request disables duplicate submission and restores loading state', async () => {
    let finish;
    const ui = setup(() => new Promise(resolve => { finish = resolve; }));
    const pending = ui.submit();
    assert.equal(ui.get('marketingSubmit').disabled, true);
    assert.equal(ui.get('marketingForm')['aria-busy'], 'true');
    await ui.submit();
    assert.equal(ui.calls.length, 1);
    finish(response(200, pack())); await pending;
    assert.equal(ui.get('marketingSubmit').disabled, false);
    assert.equal(ui.get('marketingForm')['aria-busy'], 'false');
});
test('invalid input never calls backend', async () => {
    const ui = setup(() => { throw new Error('Unexpected'); });
    ui.get('marketingAudience').value = '';
    await ui.submit();
    assert.equal(ui.calls.length, 0);
});
test('auth, missing configuration, quota and provider errors clear stale results', async () => {
    for (const status of [401,403,429,503,504]) {
        const ui = setup(() => response(status, { success: false, error: 'Safe service error', attempt_counted: status === 504 }));
        ui.get('marketing_ad_headline').value = 'stale';
        await ui.submit();
        assert.equal(ui.get('marketingResults').hidden, true);
        assert.equal(ui.get('marketing_ad_headline').value, '');
        assert.match(ui.get('marketingStatus').textContent, status === 401 ? /Please sign in/ : /Safe service error/);
        if (status === 504) assert.match(ui.get('marketingStatus').textContent, /counted/);
    }
});
test('malformed or incomplete output is not displayed', async () => {
    for (const respond of [() => ({ ok: false, json: async () => { throw new Error('PRIVATE_BODY'); } }),
                            () => response(200, { success: true, outputs: {} })]) {
        const ui = setup(respond); await ui.submit();
        assert.equal(ui.get('marketingResults').hidden, true);
        assert.doesNotMatch(ui.get('marketingStatus').textContent, /PRIVATE_BODY/);
    }
});
test('network failure warns that billing may be uncertain; copy failure allows manual copying', async () => {
    const ui = setup(() => { throw new TypeError('PRIVATE_NETWORK'); }, async () => { throw new Error('denied'); });
    await ui.submit();
    assert.match(ui.get('marketingStatus').textContent, /may have counted/);
    assert.doesNotMatch(ui.get('marketingStatus').textContent, /PRIVATE_NETWORK/);
    await ui.copy('ad_headline');
    assert.equal(ui.get('marketing_ad_headline').selected, true);
    assert.match(ui.get('marketingStatus').textContent, /manually/);
});

test('late generation cannot repopulate private outputs after session termination', async () => {
    let finish;const ui=setup(()=>new Promise(resolve=>{finish=resolve;}));
    const pending=ui.submit();ui.ended();finish(response(200,pack()));await pending;
    assert.equal(ui.get('marketingResults').hidden,true);
    keys.forEach(key=>assert.equal(ui.get('marketing_'+key).value,''));
    assert.equal(ui.events.some(event=>event.type==='marketing-pack-ready'),false);
    assert.equal(ui.get('marketingSubmit').disabled,false);
});

test('unauthorized response clears the displayed session even when its body is unreadable',async()=>{
    let parsed=false;const ui=setup(()=>({status:401,ok:false,json:async()=>{parsed=true;throw new Error('PRIVATE');}}));
    await ui.submit();assert.equal(parsed,false);
    assert.equal(ui.events.some(event=>event.type==='customer-session-ended'),true);
    assert.match(ui.get('marketingStatus').textContent,/Please sign in/);
});

test('invalid quota, save proof and oversized output never publish a partial pack', async () => {
    for (const alter of [p => { delete p.quota; }, p => { p.quota.used = -1; },
        p => { p.quota.limit = 0; }, p => { delete p.save_token; },
        p => { p.outputs.ad_headline = 'x'.repeat(121); },
        p => { p.outputs.social_caption = 'invalid\u0000'; }]) {
        const data = pack(); alter(data);
        const ui = setup(() => response(200, data)); await ui.submit();
        assert.equal(ui.get('marketingResults').hidden, true);
        assert.equal(ui.events.some(e => e.type === 'marketing-pack-ready'), false);
        keys.forEach(key => assert.equal(ui.get('marketing_' + key).value, ''));
        assert.match(ui.get('marketingStatus').textContent, /incomplete pack/);
        assert.equal(ui.get('marketingSubmit').disabled, false);
    }
});
test('counted provider failure explains a bounded retry instead of immediate resubmission', async () => {
    const ui = setup(() => response(503, {success:false, error:'Provider at capacity.', attempt_counted:true}));
    await ui.submit();
    assert.match(ui.get('marketingStatus').textContent, /counted.*60 seconds/);
    assert.equal(ui.events.some(e => e.type === 'marketing-pack-ready'), false);
});

test('trial generation reports lifetime attempts and retains copy and save workflow',async()=>{
    const data=pack();data.quota={used:1,limit:3,period:'lifetime'};
    const ui=setup(()=>response(200,data));await ui.submit();
    assert.match(ui.get('marketingStatus').textContent,/1 of 3 lifetime trial attempts/);
    assert.equal(ui.events.some(e=>e.type==='marketing-pack-ready'),true);
});
