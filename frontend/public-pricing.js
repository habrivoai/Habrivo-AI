/* Public prices and entitlements are fetched from the server; no fallback offers. */
(() => {
    const get = id => document.getElementById(id);
    const cards = get('publicPlans'), billing = get('publicBilling');
    const status = get('pricingStatus'), retry = get('pricingRetry');
    const names = { monthly: 'Monthly', quarterly: 'Every 3 months', half_yearly: 'Every 6 months', yearly: 'Yearly' };
    let catalog = null, busy = false;
    function validate(data) {
        if (!data || !data.success || data.currency !== 'USD' || !data.plans || !data.entitlements || !data.billing_periods) throw new Error();
        if (data.free_account && (data.free_account.amount !== 0 || data.free_account.ai_monthly_limit !== 0 || data.free_account.payment_required !== false)) throw new Error();
        for (const key of ['starter','pro','premium']) {
            const plan = data.plans[key];
            if (!plan || typeof plan.name !== 'string' || !Number.isInteger(plan.monthly_limit) || plan.monthly_limit <= 0) throw new Error();
            for (const cycle of Object.keys(names)) {
                if (!Number.isInteger(plan.prices?.[cycle]) || plan.prices[cycle] <= 0 ||
                    !Number.isInteger(data.billing_periods[cycle]) || data.billing_periods[cycle] <= 0 ||
                    typeof plan.checkout_available?.[cycle] !== 'boolean') throw new Error();
            }
        }
        for (const value of Object.values(data.entitlements)) if (!Number.isInteger(value) || value <= 0) throw new Error();
        for (const key of ['saved_packs','cooldown_seconds','daily_service_limit']) if (!data.entitlements[key]) throw new Error();
    }
    function render() {
        cards.replaceChildren();
        const freePrice = get('freeAccountPrice');
        if (freePrice) freePrice.textContent = catalog.free_account ? (catalog.free_account.ai_lifetime_limit === 3 ? '$0 · No payment details required · 3 lifetime AI attempts after email verification for eligible new accounts' : '$0 · No payment details required · No AI credits included') : 'Account access without checkout';
        const cycle = billing.value;
        for (const key of ['starter','pro','premium']) {
            const plan = catalog.plans[key];
            const card = document.createElement('article'); card.className = 'plan-card';
            const title = document.createElement('h3'); title.textContent = plan.name;
            const price = document.createElement('p'); price.className = 'plan-price';
            price.textContent = new Intl.NumberFormat('en-US', { style: 'currency', currency: catalog.currency, maximumFractionDigits: 0 }).format(plan.prices[cycle]);
            const period = document.createElement('p'); period.className = 'plan-period';
            period.textContent = catalog.currency + ' / ' + (catalog.billing_periods[cycle] === 1 ? 'month' : catalog.billing_periods[cycle] + ' months');
            const list = document.createElement('ul');
            for (const text of [`${plan.monthly_limit} generation attempts per calendar month`, 'Four outputs in each marketing pack',
                `${catalog.entitlements.saved_packs} saved packs in your private library`, 'Individual copy controls and account settings']) {
                const li = document.createElement('li'); li.textContent = text; list.appendChild(li);
            }
            const state = document.createElement('p'); state.className = 'plan-state';
            state.textContent = plan.checkout_available[cycle]
                ? (catalog.checkout_environment === 'sandbox' ? 'Sandbox checkout available · test mode only' : 'Continue to checkout after signing in')
                : 'Checkout setup pending · account creation available';
            const choose = document.createElement('a'); choose.className = 'button';
            choose.textContent = 'Choose ' + plan.name;
            choose.href = '/start?' + new URLSearchParams({ plan: key, billing_cycle: cycle, mode: 'signup' });
            card.appendChild(title); card.appendChild(price); card.appendChild(period); card.appendChild(list); card.appendChild(state); card.appendChild(choose);
            cards.appendChild(card);
        }
        get('pricingTerms').textContent = `USD per selected billing period. ${catalog.entitlements.cooldown_seconds}-second cooldown; shared daily capacity of ${catalog.entitlements.daily_service_limit} attempts across the service. Reserved attempts count even on provider failure. Eligible new accounts receive 3 lifetime AI attempts after email verification; they never renew.`;
        const availability = [];
        if (catalog.checkout_environment === 'sandbox') availability.push('Paddle is in sandbox mode: this is not a live purchase flow.');
        if (!catalog.generation_configured) availability.push('AI provider setup is pending.');
        availability.push('Choosing a plan does not charge you or activate access. Application-configured prices must be matched to Paddle prices before launch; confirm totals and tax treatment at checkout.');
        get('pricingAvailability').textContent = availability.join(' ');
    }
    async function load() {
        if (busy) return;
        busy = true; billing.disabled = true; retry.hidden = true; retry.disabled = true;
        catalog = null; cards.replaceChildren(); cards.setAttribute('aria-busy', 'true');
        get('pricingTerms').textContent = ''; get('pricingAvailability').textContent = '';
        status.textContent = 'Loading current plans…';
        try {
            const response = await fetch('/api/promo/plans', { credentials: 'same-origin' });
            if (!response.ok) throw new Error();
            const data = await response.json(); validate(data); catalog = data;
            billing.replaceChildren();
            for (const [key, label] of Object.entries(names)) {
                const option = document.createElement('option'); option.value = key; option.textContent = label; billing.appendChild(option);
            }
            billing.value = 'monthly'; render(); status.textContent = 'Current application plans. Choose a billing period to compare.';
        } catch {
            catalog = null; cards.replaceChildren(); retry.hidden = false;
            status.textContent = 'Pricing is unavailable. Please retry or contact support. No prices or checkout offers are being assumed.';
        } finally { busy = false; billing.disabled = !catalog; retry.disabled = false; cards.setAttribute('aria-busy', 'false'); }
    }
    billing.addEventListener('change', () => { if (catalog) render(); });
    retry.addEventListener('click', load);
    load();
})();
