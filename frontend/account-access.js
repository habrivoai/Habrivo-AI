/* Access is server-derived. These display controls never grant a subscription. */
(() => {
    const get = id => document.getElementById(id);
    let revision = 0;
    async function load() {
        const version = ++revision;
        get('marketingFields').disabled = true;
        get('accountAccess').textContent = 'Loading account access…';
        for (const id of ['dashboardPlan','dashboardSubscription','dashboardUsage','aiAccessStatus']) {
            const field = get(id); if (field) field.textContent = 'Loading…';
        }
        try {
            const response = await fetch('/api/customer/entitlements', { credentials: 'same-origin' });
            if (version !== revision) return;
            if (response.status === 401) {
                get('accountAccess').textContent = 'Sign in to view your account.';
                get('marketingLock').textContent = 'Sign in to use Marketing Studio.';
                if (typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('customer-session-ended'));
                return;
            }
            const data = await response.json();
            if (version !== revision) return;
            if (!response.ok || !data.success || !data.features || !data.ai || !data.email ||
                typeof data.account_label !== 'string' || typeof data.ai.paid_entitlement !== 'boolean' ||
                typeof data.features.ai_generation !== 'boolean' || !Number.isInteger(data.ai.used) || data.ai.used < 0 ||
                !Number.isInteger(data.ai.monthly_limit) || data.ai.monthly_limit < 0) throw new Error();
            const statuses = {none:'No paid subscription',active:'Active verified subscription',expired:'Expired',
                scheduled:'Paid period has not started',pending:'Pending verification',unverified:'Not active: verification or reconciliation needed',
                canceled:'Canceled',paused:'Paused',past_due:'Past due',inactive:'No verified active subscription'};
            if (get('dashboardPlan')) get('dashboardPlan').textContent = data.account_label;
            if (get('dashboardSubscription')) get('dashboardSubscription').textContent = statuses[data.subscription?.status] || 'Status unavailable';
            const trial = data.free_ai;
            const remaining = trial && Number.isInteger(trial.remaining) && trial.remaining >= 0 && trial.remaining <= 3 ? trial.remaining : 0;
            if (get('dashboardUsage')) get('dashboardUsage').textContent = data.ai.paid_entitlement
                ? `${data.ai.used} / ${data.ai.monthly_limit} attempts used · ${Math.max(0,data.ai.monthly_limit-data.ai.used)} remaining`
                : `${remaining} lifetime free attempts remaining · ${data.ai.used} paid attempts recorded this month`;
            get('accountAccess').textContent = data.account_label + ' · Dashboard, account settings and saved-content access available.';
            get('emailVerificationStatus').textContent = data.email.verified
                ? 'Email verified.' : 'Email unverified. Sign in and request a verification link to unlock any eligible lifetime trial.';
            const resend = get('verificationResend'); if (resend) resend.disabled = data.email.verified === true;
            get('aiAccessStatus').textContent = data.ai.paid_entitlement
                ? `${data.ai.used} of ${data.ai.monthly_limit} monthly attempts used. Existing paid limits and cooldowns apply.`
                : remaining > 0 ? `Free account: ${remaining} of 3 lifetime attempts remaining. They never renew.` :
                  'Free account: 0 AI credits available. Verify your email if eligible, or upgrade after exhaustion.';
            const allowed = data.features.ai_generation === true;
            get('marketingFields').disabled = !allowed;
            get('marketingLock').textContent = allowed ? 'Marketing Studio is available; request-time quota checks still apply.' :
                data.ai.paid_entitlement ? 'Generation is unavailable: check your allowance and service configuration.' :
                'Locked: verify your email for an eligible trial, check service configuration, or upgrade. Settings and library remain available.';
        } catch {
            if (version !== revision) return;
            get('accountAccess').textContent = 'Unable to load account access. Please retry.';
            for (const id of ['dashboardPlan','dashboardSubscription','dashboardUsage','aiAccessStatus']) {
                const field = get(id); if (field) field.textContent = 'Unavailable — refresh to retry';
            }
            get('marketingLock').textContent = 'Generation remains locked until access can be checked.';
        }
    }
    get('accessRetry').addEventListener('click', load);
    get('productsBtn').addEventListener('click', () => get('libraryOpen').click());
    document.addEventListener('customer-authenticated', load);
    document.addEventListener('marketing-pack-ready', load);
    document.addEventListener('marketing-attempt-finished', load);
    document.addEventListener('customer-session-ended', () => { ++revision; get('marketingFields').disabled = true; });
    load();
})();
