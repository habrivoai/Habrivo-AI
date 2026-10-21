/* Only a server-validated selection is displayed. No client-side entitlement writes. */
(() => {
    const get = id => document.getElementById(id);
    const panel = get('checkoutPanel'), status = get('checkoutMessage');
    const button = get('checkoutContinue');
    let selection = null, authenticated = false, busy = false, revision = 0;
    const available = () => authenticated && selection && selection.checkout_available;
    // Authentication display mode is owned by customer-dashboard.js.

    async function load() {
        const version = ++revision;
        button.disabled = true;
        try {
            const response = await fetch('/api/checkout/intent', { credentials: 'same-origin' });
            if (!response.ok) throw new Error();
            const data = await response.json();
            if (!data.success) throw new Error();
            if (version !== revision) return;
            selection = data.selection; authenticated = data.authenticated === true;
            if (!selection) { panel.classList.add('hidden'); return; }
            if (typeof selection.plan !== 'string' || typeof selection.billing_cycle !== 'string' ||
                !Number.isInteger(selection.amount) || selection.amount <= 0 || selection.currency !== 'USD' ||
                !Number.isInteger(selection.months) || typeof selection.checkout_available !== 'boolean') throw new Error();
            panel.classList.remove('hidden');
            get('checkoutSelection').textContent = `${selection.name} · ${new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(selection.amount)} per ${selection.months === 1 ? 'month' : selection.months + ' months'}`;
            const notices = [];
            if (!authenticated) notices.push('Create an account or log in to continue with this selection.');
            if (!selection.checkout_available) notices.push('Checkout setup is pending. No payment has been taken.');
            if (selection.environment === 'sandbox') notices.push('Sandbox test mode only; this is not a live purchase.');
            if (!notices.length) notices.push('Review your selection, then continue to checkout.');
            status.textContent = notices.join(' ');
        } catch {
            if (version !== revision) return;
            selection = null; authenticated = false; panel.classList.remove('hidden');
            get('checkoutSelection').textContent = '';
            status.textContent = 'Unable to load your plan selection. Retry or choose a plan from Pricing.';
        } finally { if (version === revision) button.disabled = busy || !available(); }
    }
    get('checkoutRetry').addEventListener('click', load);
    document.addEventListener('customer-authenticated', load);
    button.addEventListener('click', async () => {
        if (busy || !available()) return;
        busy = true; button.disabled = true; status.textContent = 'Preparing checkout…';
        const chosen = selection;
        try {
            const response = await fetch('/api/paddle/checkout', {
                method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plan: chosen.plan, billing_cycle: chosen.billing_cycle })
            });
            const data = await response.json();
            if (!response.ok || !data.success || typeof data.checkout_url !== 'string') {
                throw new Error(data.error || 'Checkout is unavailable. Please try later.');
            }
            const url = new URL(data.checkout_url);
            const host = url.hostname;
            const trusted = url.origin === window.location.origin || host === 'paddle.com' || host === 'paddle.io' ||
                host.endsWith('.paddle.com') || host.endsWith('.paddle.io');
            if (url.protocol !== 'https:' || url.username || url.password || !trusted) throw new Error('Checkout returned an unsupported destination. Contact support.');
            status.textContent = 'Opening checkout. Payment confirmation is still required for access.';
            window.location.assign(url.href);
        } catch (error) {
            status.textContent = error instanceof TypeError || error instanceof SyntaxError
                ? 'Unable to connect to checkout. Please try again.' : error.message;
        } finally { busy = false; button.disabled = !available(); }
    });
    load();
})();
