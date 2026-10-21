/* Verification bearer stays in memory, never in query strings or analytics. */
(() => {
    const get = id => document.getElementById(id);
    const form = get('verificationForm');
    const resend = get('verificationResend');
    const message = get('verificationMessage');
    const button = form ? get('verificationSubmit') : resend;
    if (!button || !message) return;
    let token = '', busy = false, finished = false, revision = 0;
    if (form) {
        const fragment = new URLSearchParams(window.location.hash.slice(1));
        token = fragment.getAll('token').length === 1 ? fragment.get('token') : '';
        window.history.replaceState(null, '', window.location.pathname);
        if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
            token = ''; button.disabled = true;
            message.textContent = 'Verification link is missing or invalid. Sign in to request another.';
        }
    }
    document.addEventListener('customer-session-ended', () => {
        ++revision;
        if (!form) { button.disabled = true; message.textContent = ''; }
    });
    document.addEventListener('customer-authenticated', () => { if (!form) button.disabled = false; });
    const submit = async event => {
        event.preventDefault();
        if (busy || finished || (form && !token)) return;
        const version = revision;
        busy = true; button.disabled = true; message.textContent = 'Submitting securely…';
        try {
            const response = await fetch('/api/customer/' + (form ? 'verify-email' : 'resend-verification'), {
                method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
                body:JSON.stringify(form ? {token} : {})
            });
            if (version !== revision) return;
            const data = await response.json();
            if (version !== revision) return;
            if (!response.ok || data.success !== true) {
                message.textContent = response.status === 429 ? 'Too many attempts. Wait before trying again.' :
                    response.status === 401 ? 'Sign in to request a verification email.' :
                    form ? 'Link is expired, used or invalid. Sign in to request a new verification link.' :
                    'Verification email is unavailable or not configured. Try later or contact support.';
                return;
            }
            if (form) {
                token = ''; finished = true;
                message.textContent = 'Email verified. Sign in to view your remaining lifetime attempts. Verification never renews them.';
            } else {
                message.textContent = 'If verification is needed, instructions are queued, not yet delivered. Check your inbox after delivery.';
            }
        } catch {
            if (version === revision) message.textContent = 'Unable to confirm the request. Retry later or check your account verification status.';
        } finally {
            busy = false;
            if (version === revision) button.disabled = finished || Boolean(form && !token);
        }
    };
    if (form) form.addEventListener('submit', submit);
    else resend.addEventListener('click', submit);
})();
