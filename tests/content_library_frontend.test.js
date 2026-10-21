'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/content-library.js'), 'utf8');
const keys = ['promotional_copy','product_description','social_caption','ad_headline'];
function pack() {
    return { details: { product_name: 'Synthetic', description: 'Cotton', audience: 'Sellers', tone: 'friendly', goal: 'sales' },
        outputs: Object.fromEntries(keys.map(key => [key, '<script>synthetic ' + key + '</script>'])), save_token: 'synthetic-signed-proof' };
}
function item(id = 1) { return { id, product_name: '<img src=x onerror=alert(1)>', ...pack(), created_at: '2026-01-01 00:00:00' }; }
function response(body, status = 200) { return { ok: status < 400, status, json: async () => body }; }
function setup(responder, clipboard = async () => {}) {
    let nodeId = 0;
    const nodes = new Map(), events = new Map(), calls = [];
    function get(id) {
        if (!nodes.has(id)) nodes.set(id, {
            id, children: [], handlers: {}, hidden: true, disabled: false, style: {}, value: '', textContent: '',
            addEventListener(type, handler) { this.handlers[type] = handler; },
            classList: { add() { get(id).hidden = true; }, remove() { get(id).hidden = false; } },
            appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
            setAttribute(name, value) { this[name] = value; }, focus() {}, select() { this.selected = true; }
        });
        return nodes.get(id);
    }
    vm.runInNewContext(source, {
        document: {
            getElementById: get,
            addEventListener: (type, handler) => events.set(type, handler),
            createElement: tag => { const node = get('node' + nodeId++); node.tag = tag; return node; }
        },
        navigator: { clipboard: { writeText: clipboard } },
        window: { confirm: () => true },
        fetch: async (url, options) => { calls.push({ url, options }); return responder(url, options); }
    });
    return { get, calls, ended: () => events.get('customer-session-ended')(), ready: value => events.get('marketing-pack-ready')({ detail: value }),
        pending: () => events.get('marketing-pack-pending')(),
        click: id => get(id).handlers.click(),
        card: () => get('libraryItems').children[0] };
}
test('save sends existing pack only, prevents duplicate submissions and marks saved', async () => {
    let finish;
    const ui = setup(() => new Promise(resolve => { finish = resolve; }));
    assert.equal(ui.get('librarySave').disabled, true);
    ui.ready(pack());
    const pending = ui.click('librarySave');
    await ui.click('librarySave');
    assert.equal(ui.calls.length, 1);
    assert.equal(ui.calls[0].url, '/api/marketing/library');
    assert.deepEqual(JSON.parse(ui.calls[0].options.body), pack());
    assert.equal(ui.calls[0].options.credentials, 'same-origin');
    finish(response({ success: true, item: item(), created: true }, 201)); await pending;
    assert.equal(ui.get('librarySave').disabled, true);
    assert.match(ui.get('libraryMessage').textContent, /Saved/);
    assert.equal(ui.calls.some(call => call.url.includes('generate')), false);
});
test('failed save can be retried and an existing duplicate is accepted', async () => {
    let count = 0;
    const ui = setup(() => ++count === 1 ? response({ success: false, error: 'Storage unavailable' }, 503) :
        response({ success: true, item: item(), created: false }));
    ui.ready(pack()); await ui.click('librarySave');
    assert.equal(ui.get('librarySave').disabled, false);
    assert.match(ui.get('libraryMessage').textContent, /Storage unavailable/);
    await ui.click('librarySave');
    assert.equal(ui.get('librarySave').disabled, true);
});
test('list renders untrusted strings as text and copies all individual outputs', async () => {
    const copied = [];
    const ui = setup(() => response({ success: true, items: [item()], next_cursor: null }), async text => copied.push(text));
    await ui.click('libraryOpen');
    const card = ui.card();
    assert.equal(card.children[0].textContent, item().product_name);
    assert.equal(card.innerHTML, undefined);
    const buttons = card.children.filter(child => child.tag === 'button' && child.textContent.startsWith('Copy'));
    assert.equal(buttons.length, 4);
    for (const button of buttons) await button.handlers.click();
    assert.deepEqual(copied, Object.values(pack().outputs));
    assert.equal(ui.get('libraryNext').disabled, true);
});
test('empty state, pagination and refresh use bounded server pages', async () => {
    const ui = setup(url => response({ success: true,
        items: url.includes('before=') ? [] : [item(22)], next_cursor: url.includes('before=') ? null : 22 }));
    await ui.click('libraryOpen');
    assert.equal(ui.get('libraryNext').disabled, false);
    await ui.click('libraryNext');
    assert.equal(ui.calls[1].url, '/api/marketing/library?before=22');
    assert.match(ui.card().textContent, /No saved items/);
    await ui.click('libraryRefresh');
    assert.equal(ui.calls[2].url, '/api/marketing/library');
    const empty = setup(() => response({ success: true, items: [], next_cursor: null }));
    await empty.click('libraryOpen');
    assert.match(empty.card().textContent, /library is empty/);
});
test('delete waits for server, blocks duplicate clicks, and preserves retry after failure', async () => {
    let failed = false, deleted = false, finish;
    const ui = setup((url, options) => {
        if (options.method === 'DELETE') {
            if (!failed) { failed = true; return response({ success: false, error: 'Try again' }, 503); }
            return new Promise(resolve => { finish = () => { deleted = true; resolve(response({ success: true })); }; });
        }
        return response({ success: true, items: deleted ? [] : [item()], next_cursor: null });
    });
    await ui.click('libraryOpen');
    const remove = ui.card().children.at(-1);
    await remove.handlers.click();
    assert.equal(remove.disabled, false);
    assert.match(ui.get('libraryMessage').textContent, /Try again/);
    const pending = remove.handlers.click();
    await remove.handlers.click();
    assert.equal(ui.calls.filter(call => call.options.method === 'DELETE').length, 2);
    finish(); await pending;
    assert.match(ui.card().textContent, /library is empty/);
    assert.match(ui.get('libraryMessage').textContent, /deleted/);
});
test('authentication, invalid JSON and network failures display safe retry states', async () => {
    for (const responder of [() => response({ success: false }, 401),
        () => ({ ok: true, json: async () => { throw new Error('PRIVATE_JSON'); } }),
        () => { throw new TypeError('PRIVATE_NETWORK'); }]) {
        const ui = setup(responder);
        await ui.click('libraryOpen');
        assert.equal(ui.get('libraryRefresh').disabled, false);
        assert.doesNotMatch(ui.get('libraryMessage').textContent, /PRIVATE_/);
        assert.ok(ui.get('libraryMessage').textContent.length);
    }
});
test('a new generation cannot inherit a stale successful save state', async () => {
    let finish;
    const ui = setup(() => new Promise(resolve => { finish = resolve; }));
    ui.ready(pack()); const pending = ui.click('librarySave');
    ui.pending(); ui.ready({ ...pack(), save_token: 'new-proof' });
    finish(response({ success: true })); await pending;
    assert.equal(ui.get('librarySave').disabled, false);
    ui.pending();
    assert.equal(ui.get('librarySave').disabled, true);
});
test('copy failure selects saved output for manual copying', async () => {
    const ui = setup(() => response({ success: true, items: [item()], next_cursor: null }), async () => { throw new Error('denied'); });
    await ui.click('libraryOpen');
    const card = ui.card();
    const copy = card.children.find(child => child.tag === 'button');
    await copy.handlers.click();
    assert.equal(card.children.find(child => child.tag === 'textarea').selected, true);
    assert.match(ui.get('libraryMessage').textContent, /manually/);
});

test('late library response cannot restore another sessions private packs', async () => {
    let finish;const ui=setup(()=>new Promise(resolve=>{finish=resolve;}));
    const pending=ui.click('libraryOpen');ui.ended();finish(response({success:true,items:[item()],next_cursor:null}));await pending;
    assert.equal(ui.get('libraryItems').children.length,0);
    assert.equal(ui.get('libraryPanel').hidden,true);
    assert.equal(ui.get('librarySave').disabled,true);
});
