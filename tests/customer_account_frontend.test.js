'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/customer-account.js'), 'utf8');
function setup(responses) {
    const elements = new Map();
    function get(id) {
        if (!elements.has(id)) elements.set(id, {
            value: '', textContent: '', disabled: false, hidden: true, handlers: {},
            classList: { add() { get(id).hidden = true; }, remove() { get(id).hidden = false; } },
            addEventListener(event, handler) { this.handlers[event] = handler; },
            focus() {}, reset() { for (const name of ['currentPassword','newPassword','confirmPassword']) get(name).value = ''; }
        });
        return elements.get(id);
    }
    const calls = [], redirects = [];
    vm.runInNewContext(source, {
        document: { getElementById: get },
        window: { location: { replace: url => redirects.push(url) } },
        fetch: async (url, options) => {
            calls.push({ url, options });
            const result = responses.shift();
            if (result instanceof Error) throw result;
            if (!result) throw new Error('Unexpected request');
            return { ok: result.status < 400, status: result.status, json: async () => result.body };
        }, TypeError
    });
    return { get, calls, redirects, click: id => get(id).handlers.click(),
        submit: () => get('passwordForm').handlers.submit({ preventDefault() {} }) };
}
function passwords(ui) {
    ui.get('currentPassword').value = 'Original-test-password';
    ui.get('newPassword').value = ui.get('confirmPassword').value = 'Replacement-test-password';
}
test('account button loads profile as text and opens settings', async () => {
    const ui = setup([{status:200,body:{authenticated:true,user:{name:'<script>synthetic</script>',email:'one@example.invalid'}}}]);
    await ui.click('accountBtn');
    assert.equal(ui.calls[0].url, '/api/check-auth');
    assert.equal(ui.get('accountPanel').hidden, false);
    assert.equal(ui.get('accountProfile').textContent, '<script>synthetic</script> · one@example.invalid');
});
test('successful password change sends values privately and redirects to login', async () => {
    const ui = setup([{status:200,body:{success:true}}]); passwords(ui);
    await ui.submit();
    assert.equal(ui.calls[0].url, '/api/customer/password');
    assert.equal(ui.calls[0].options.credentials, 'same-origin');
    assert.equal(JSON.parse(ui.calls[0].options.body).new_password, 'Replacement-test-password');
    assert.equal(ui.get('currentPassword').value, '');
    assert.deepEqual(ui.redirects, ['/customer.html']);
});
test('mismatching confirmation sends no request', async () => {
    const ui = setup([]); passwords(ui); ui.get('confirmPassword').value = 'different';
    await ui.submit();
    assert.equal(ui.calls.length, 0);
    assert.match(ui.get('accountMessage').textContent, /do not match/);
});
test('wrong password displays feedback without redirecting', async () => {
    const ui = setup([{status:403,body:{success:false,message:'Current password is incorrect'}}]); passwords(ui);
    await ui.submit();
    assert.equal(ui.get('accountMessage').textContent, 'Current password is incorrect');
    assert.equal(ui.redirects.length, 0);
    assert.equal(ui.get('passwordSubmit').disabled, false);
});
test('logout calls server before navigating; failure leaves retry available', async () => {
    const ui = setup([new TypeError('offline'),{status:200,body:{success:true}}]);
    await ui.click('logoutBtn');
    assert.equal(ui.redirects.length, 0);
    assert.equal(ui.get('logoutBtn').disabled, false);
    await ui.click('logoutBtn');
    assert.equal(ui.calls[1].url, '/api/customer/logout');
    assert.equal(ui.calls[1].options.method, 'POST');
    assert.deepEqual(ui.redirects, ['/customer.html']);
});
test('expired session clears password fields and returns to login', async () => {
    const ui = setup([{status:401,body:{success:false}}]); passwords(ui);
    await ui.submit();
    assert.equal(ui.get('currentPassword').value, '');
    assert.deepEqual(ui.redirects, ['/customer.html']);
});
