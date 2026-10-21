/* No query parameter authenticates a customer. Reset bearer stays in memory only. */
(() => {
    const get = id => document.getElementById(id);
    const form = get('recoveryForm'), button = get('recoverySubmit'), message = get('recoveryMessage');
    const resetting = form.dataset.mode === 'reset';
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    let token = resetting && fragment.getAll('token').length === 1 ? fragment.get('token') : '';
    window.history.replaceState(null, '', window.location.pathname);
    if (resetting && !/^[A-Za-z0-9_-]{43}$/.test(token)) {
        token = ''; button.disabled = true;
        message.textContent = 'This reset link is missing or invalid. Request a new link.';
    }
    let busy = false, finished = false;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || finished || (resetting && !token)) return;
        let payload;
        if (resetting) {
            const password = get('recoveryPassword').value;
            if (password !== get('recoveryConfirm').value || password.length < 8 || password.length > 128) {
                message.textContent = 'Passwords must match and contain 8–128 characters.'; return;
            }
            payload = {token, new_password: password};
        } else {
            const email = get('recoveryEmail').value.trim();
            if (!email || email.length > 254) { message.textContent = 'Enter your account email.'; return; }
            payload = {email};
        }
        busy = true; button.disabled = true; message.textContent = 'Submitting securely…';
        try {
            const response = await fetch('/api/customer/' + (resetting ? 'reset-password' : 'forgot-password'), {
                method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (!response.ok || data.success !== true) {
                message.textContent = response.status === 429 ? 'Too many attempts. Wait before trying again.' :
                    (resetting ? 'Unable to reset. The link may be expired or used. Request a new link, or retry later.' :
                     'Recovery is unavailable or not configured. Try later or contact support.');
                return;
            }
            message.textContent = resetting ? 'Password reset. All customer sessions have ended. Sign in with your new password.' :
                'If an eligible account exists, instructions are queued for delivery. If none arrives, contact support.';
            if (resetting) { token = ''; finished = true; form.reset(); }
        } catch {
            message.textContent = 'Connection failed. Try again. If your reset already completed, sign in with your new password.';
        } finally {
            busy = false; button.disabled = finished || (resetting && !token);
        }
    });
})();
