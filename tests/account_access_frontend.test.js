'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/account-access.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function entitlements(paid = false) {
    return { success:true, account_label:paid ? 'Starter' : 'Free account',
        subscription:{status:paid?'active':'none'},email:{verified:false,verification_available:false},
        features:{ai_generation:paid,dashboard:true,account_settings:true,content_library:true},
        ai:{paid_entitlement:paid,used:0,monthly_limit:paid ? 100 : 0} };
}
function setup(responder) {
    const map = new Map(), events = new Map(), calls = [];
    function get(id) {
        if (!map.has(id)) map.set(id, { disabled:false, textContent:'', handlers:{}, clicks:0,
            addEventListener(type,fn) { this.handlers[type]=fn; }, click() { this.clicks++; } });
        return map.get(id);
    }
    vm.runInNewContext(source, {
        document:{getElementById:get,addEventListener:(name,fn) => events.set(name,fn)},
        fetch:async (url, options) => { calls.push({url,options}); return responder(); }
    });
    return { get,calls,refresh:() => get('accessRetry').handlers.click(),authenticate:() => events.get('customer-authenticated')(),
        generated:() => events.get('marketing-pack-ready')() };
}
function response(body,status=200) { return {ok:status<400,status,json:async()=>body}; }
test('free account displays available features and keeps generation locked', async () => {
    const ui=setup(()=>response(entitlements())); await flush();
    assert.match(ui.get('accountAccess').textContent,/Free account/);
    assert.match(ui.get('aiAccessStatus').textContent,/0 AI credits/);
    assert.equal(ui.get('marketingFields').disabled,true);
    assert.match(ui.get('emailVerificationStatus').textContent,/unverified/);
    assert.match(ui.get('marketingLock').textContent,/Locked/);
    assert.equal(ui.calls[0].url,'/api/customer/entitlements');
    assert.equal(ui.calls[0].options.credentials,'same-origin');
});
test('paid access remains server-derived and refreshes after generation', async () => {
    let paid=false;
    const ui=setup(()=>response(entitlements(paid))); await flush();
    paid=true; await ui.authenticate();
    assert.equal(ui.get('marketingFields').disabled,false);
    assert.match(ui.get('aiAccessStatus').textContent,/100 monthly attempts/);
    const before=ui.calls.length; await ui.generated();
    assert.equal(ui.calls.length,before+1);
});
test('expired or unavailable access fails closed with retry information', async () => {
    for (const result of [response({},401),response({success:false},503),response({success:true})]) {
        const ui=setup(()=>result); await flush();
        assert.equal(ui.get('marketingFields').disabled,true);
        assert.ok(ui.get('accountAccess').textContent.length);
    }
    const ui=setup(()=>{throw new TypeError('PRIVATE_NETWORK');}); await flush();
    assert.doesNotMatch(ui.get('accountAccess').textContent,/PRIVATE_/);
    assert.equal(ui.get('marketingFields').disabled,true);
});
test('content library remains reachable without AI access', async () => {
    const ui=setup(()=>response(entitlements())); await flush();
    ui.get('productsBtn').handlers.click();
    assert.equal(ui.get('libraryOpen').clicks,1);
    assert.equal(ui.calls.some(call=>call.url.includes('/generate')),false);
});
test('stale access responses cannot unlock a newer locked account state', async () => {
    const resolvers=[];
    const ui=setup(()=>new Promise(resolve=>resolvers.push(resolve)));
    const newer=ui.authenticate();
    resolvers[1](response(entitlements(false))); await newer;
    resolvers[0](response(entitlements(true))); await flush();
    assert.equal(ui.get('marketingFields').disabled,true);
    assert.match(ui.get('accountAccess').textContent,/Free account/);
});

test('overview renders only server plan status and usage then clears stale metrics on failure', async () => {
    let fail=false;
    const ui=setup(()=>fail?response({},503):response(entitlements(true)));await flush();
    assert.equal(ui.get('dashboardPlan').textContent,'Starter');
    assert.match(ui.get('dashboardSubscription').textContent,/Active verified/);
    assert.match(ui.get('dashboardUsage').textContent,/100 remaining/);
    fail=true;await ui.refresh();assert.match(ui.get('dashboardPlan').textContent,/Unavailable/);
    assert.equal(ui.get('marketingFields').disabled,true);
});

test('verified lifetime trial displays server remaining count and exhausts without monthly renewal', async () => {
    for (const remaining of [3,2,1,0]) {
        const data=entitlements(false);data.email.verified=true;
        data.free_ai={remaining,lifetime_limit:3,used:3-remaining,eligible:true};
        data.features.ai_generation=remaining>0;
        const ui=setup(()=>response(data));await flush();
        assert.match(ui.get('dashboardUsage').textContent,new RegExp(`${remaining} lifetime free attempts remaining`));
        assert.equal(ui.get('marketingFields').disabled,remaining===0);
        assert.equal(ui.get('emailVerificationStatus').textContent,'Email verified.');
        assert.equal(ui.get('verificationResend').disabled,true);
        if(remaining>0)assert.match(ui.get('aiAccessStatus').textContent,/never renew/);
    }
});
