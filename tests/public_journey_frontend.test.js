'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const pricingScript = fs.readFileSync(path.join(__dirname, '../frontend/public-pricing.js'), 'utf8');
const checkoutScript = fs.readFileSync(path.join(__dirname, '../frontend/checkout-journey.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
function catalog() {
    const plans = {};
    for (const [key, name, limit, prices] of [
        ['starter','Starter',100,[39,105,199,349]],['pro','Pro',500,[79,209,399,699]],['premium','Premium',2000,[149,399,749,1299]]
    ]) plans[key] = { name, monthly_limit: limit,
        prices: Object.fromEntries(['monthly','quarterly','half_yearly','yearly'].map((cycle, n) => [cycle, prices[n]])),
        checkout_available: { monthly: false, quarterly: false, half_yearly: false, yearly: false } };
    return { success: true, currency: 'USD', plans, free_account:{name:'Free',amount:0,ai_monthly_limit:0,payment_required:false}, billing_periods: { monthly:1, quarterly:3, half_yearly:6, yearly:12 },
        entitlements: { saved_packs:200, cooldown_seconds:60, daily_service_limit:50 }, checkout_environment:'sandbox', generation_configured:false };
}
function nodes() {
    const map = new Map(); let next = 0;
    function get(id) {
        if (!map.has(id)) map.set(id, {
            children: [], handlers: {}, textContent: '', disabled: false, hidden: id !== 'authForm', value: '', clicks: 0,
            classList: { add() { get(id).hidden = true; }, remove() { get(id).hidden = false; }, contains() { return get(id).hidden; } },
            appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
            addEventListener(type, fn) { this.handlers[type] = fn; },
            setAttribute(key, value) { this[key] = value; }, click() { this.clicks++; }
        });
        return map.get(id);
    }
    return { get, createElement: tag => { const node = get('node' + next++); node.tag = tag; return node; } };
}
function pricing(responder) {
    const dom = nodes(), calls = [];
    vm.runInNewContext(pricingScript, {
        document: { getElementById:dom.get, createElement:dom.createElement }, URLSearchParams,
        fetch: async url => { calls.push(url); return responder(); }
    });
    return { ...dom, calls };
}
function intent(authenticated = false, available = true) {
    return { success:true, authenticated, selection:{ plan:'pro', billing_cycle:'yearly', name:'Pro', amount:699,
        currency:'USD', months:12, checkout_available:available, environment:'sandbox' } };
}
function journey(responder, mode = 'login') {
    const dom = nodes(), calls = [], listeners = new Map(), redirects = [];
    vm.runInNewContext(checkoutScript, {
        document: { getElementById:dom.get, addEventListener:(name, fn) => listeners.set(name, fn) },
        window: { location:{ search:'?mode=' + mode, origin:'https://habrivo.test', assign:url => redirects.push(url) } },
        URL, URLSearchParams, TypeError, SyntaxError,
        fetch: async (url, options) => { calls.push({ url, options }); return responder(url, options); }
    });
    return { ...dom, calls, redirects, authenticate:() => listeners.get('customer-authenticated')() };
}
test('public prices, allowances and billing periods come from fetched catalog', async () => {
    const ui = pricing(() => response(catalog())); await flush();
    const cards = ui.get('publicPlans').children;
    assert.equal(cards.length, 3);
    assert.equal(cards[0].children[1].textContent, '$39');
    assert.match(cards[0].children[3].children[0].textContent, /100 generation attempts/);
    assert.equal(ui.calls[0], '/api/promo/plans');
    ui.get('publicBilling').value = 'yearly'; ui.get('publicBilling').handlers.change();
    const pro = ui.get('publicPlans').children[1];
    assert.equal(pro.children[1].textContent, '$699');
    assert.match(pro.children[2].textContent, /12 months/);
    const selected = new URL(pro.children.at(-1).href, 'https://habrivo.test');
    assert.equal(selected.pathname, '/start');
    assert.equal(selected.searchParams.get('plan'), 'pro');
    assert.equal(selected.searchParams.get('billing_cycle'), 'yearly');
    assert.match(ui.get('pricingAvailability').textContent, /sandbox/i);
    assert.match(ui.get('pricingTerms').textContent, /3 lifetime AI attempts after email verification/);
});
test('missing or malformed public prices show no fallback offers and permit retry', async () => {
    let failed = true;
    const ui = pricing(() => failed ? response({ success:true, currency:'USD', plans:{} }) : response(catalog()));
    await flush();
    assert.equal(ui.get('publicPlans').children.length, 0);
    assert.equal(ui.get('pricingRetry').hidden, false);
    assert.equal(ui.get('publicBilling').disabled, true);
    failed = false; await ui.get('pricingRetry').handlers.click();
    assert.equal(ui.get('publicPlans').children.length, 3);
});
test('public pricing renders names as text and prevents duplicate loads', async () => {
    let resolve;
    const ui = pricing(() => new Promise(finish => { resolve = finish; }));
    await ui.get('pricingRetry').handlers.click();
    assert.equal(ui.calls.length, 1);
    const data = catalog(); data.plans.starter.name = '<img src=x onerror=alert(1)>';
    resolve(response(data)); await flush();
    assert.equal(ui.get('publicPlans').children[0].children[0].textContent, data.plans.starter.name);
});
test('selected plan is shown before login and resumes only after explicit checkout click', async () => {
    let authenticated = false;
    const ui = journey(url => url === '/api/checkout/intent' ? response(intent(authenticated)) :
        response({ success:true, checkout_url:'https://sandbox-pay.paddle.io/checkout/synthetic' }), 'signup');
    await flush();
    assert.equal(ui.get('switchMode').clicks, 0);
    assert.match(ui.get('checkoutSelection').textContent, /699/);
    assert.equal(ui.get('checkoutContinue').disabled, true);
    authenticated = true; await ui.authenticate();
    assert.equal(ui.get('checkoutContinue').disabled, false);
    assert.equal(ui.calls.filter(call => call.url === '/api/paddle/checkout').length, 0);
    await ui.get('checkoutContinue').handlers.click();
    const checkout = ui.calls.at(-1);
    assert.equal(checkout.url, '/api/paddle/checkout');
    assert.deepEqual(JSON.parse(checkout.options.body), { plan:'pro', billing_cycle:'yearly' });
    assert.equal(checkout.options.credentials, 'same-origin');
    assert.deepEqual(ui.redirects, ['https://sandbox-pay.paddle.io/checkout/synthetic']);
});
test('missing checkout configuration keeps selection visible without calling Paddle', async () => {
    const ui = journey(() => response(intent(true, false))); await flush();
    assert.equal(ui.get('checkoutContinue').disabled, true);
    await ui.get('checkoutContinue').handlers.click();
    assert.equal(ui.calls.length, 1);
    assert.match(ui.get('checkoutMessage').textContent, /setup is pending/);
});
test('checkout errors preserve retry and reject untrusted redirect destinations', async () => {
    for (const checkout of [response({ success:false, error:'Checkout unavailable' },503),
        response({ success:true, checkout_url:'https://untrusted.invalid/pay' }),
        response({ success:true, checkout_url:'https://user:secret@paddle.com/pay' })]) {
        const ui = journey(url => url === '/api/checkout/intent' ? response(intent(true)) : checkout); await flush();
        await ui.get('checkoutContinue').handlers.click();
        assert.equal(ui.redirects.length, 0);
        assert.equal(ui.get('checkoutContinue').disabled, false);
    }
});
test('duplicate checkout clicks start only one provider initiation', async () => {
    let resolve;
    const ui = journey(url => url === '/api/checkout/intent' ? response(intent(true)) : new Promise(finish => { resolve = finish; }));
    await flush();
    const pending = ui.get('checkoutContinue').handlers.click();
    await ui.get('checkoutContinue').handlers.click();
    assert.equal(ui.calls.filter(call => call.url === '/api/paddle/checkout').length, 1);
    resolve(response({ success:true, checkout_url:'https://sandbox-pay.paddle.io/checkout/synthetic' })); await pending;
});
test('out-of-order session checks cannot overwrite a newer authenticated selection', async () => {
    const resolvers = [];
    const ui = journey(() => new Promise(resolve => resolvers.push(resolve)));
    const newer = ui.authenticate();
    resolvers[1](response(intent(true))); await newer;
    resolvers[0](response(intent(false))); await flush();
    assert.equal(ui.get('checkoutContinue').disabled, false);
});
test('selection service failure exposes no stale price or unsafe response details', async () => {
    const ui = journey(() => { throw new TypeError('PRIVATE_NETWORK'); }); await flush();
    assert.equal(ui.get('checkoutContinue').disabled, true);
    assert.equal(ui.get('checkoutSelection').textContent, '');
    assert.doesNotMatch(ui.get('checkoutMessage').textContent, /PRIVATE_/);
});

test('free account metadata displays zero cost without adding a paid/free AI plan', async () => {
    const ui=pricing(()=>response(catalog())); await flush();
    assert.match(ui.get('freeAccountPrice').textContent,/\$0/);
    assert.match(ui.get('freeAccountPrice').textContent,/No AI credits/);
    assert.equal(ui.get('publicPlans').children.length,3);
    const bad=catalog(); bad.free_account.ai_monthly_limit=999;
    const denied=pricing(()=>response(bad)); await flush();
    assert.equal(denied.get('publicPlans').children.length,0);
});
test('free entry has no checkout selection and never initiates Paddle', async () => {
    const ui=journey(()=>response({success:true,authenticated:false,selection:null}),'signup'); await flush();
    assert.equal(ui.get('switchMode').clicks,0);
    assert.equal(ui.get('checkoutPanel').hidden,true);
    await ui.get('checkoutContinue').handlers.click();
    assert.equal(ui.calls.length,1);
    assert.equal(ui.calls[0].url,'/api/checkout/intent');
});

test('approved trial catalog describes three lifetime attempts only after verification', async()=>{
    const data=catalog();data.free_account.ai_lifetime_limit=3;
    const ui=pricing(()=>response(data));await flush();
    assert.match(ui.get('freeAccountPrice').textContent,/3 lifetime AI attempts after email verification/);
    assert.match(ui.get('pricingTerms').textContent,/never renew/);
    assert.equal(ui.get('publicPlans').children.length,3);
});
