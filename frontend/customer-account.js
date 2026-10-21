/* Customer account management. Password values stay in the form/request only. */
(() => {
    const get = id => document.getElementById(id);
    const panel = get('accountPanel');
    const form = get('passwordForm');
    const message = get('accountMessage');
    const show = text => { message.textContent = text; };
    let revision = 0;
    const signIn = () => {
        if (typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('customer-session-ended'));
        window.location.replace('/customer.html');
    };
    if (typeof document.addEventListener === 'function') document.addEventListener('customer-session-ended', () => {
        ++revision; form.reset(); panel.classList.add('hidden'); get('accountProfile').textContent = '';
    });

    async function request(url, options = {}) {
        const version = revision;
        const response = await fetch(url, { credentials: 'same-origin', ...options });
        if (version !== revision) return null;
        if (response.status === 401) {
            form.reset();
            panel.classList.add('hidden');
            show('Your session has ended. Please sign in again.');
            signIn();
            return null;
        }
        let data;
        try { data = await response.json(); }
        catch { throw new Error('Unable to complete the request. Please try again.'); }
        if (version !== revision) return null;
        if (!response.ok || data.success === false) {
            throw new Error(data.message || 'Unable to complete the request. Please try again.');
        }
        return data;
    }

    get('accountBtn').addEventListener('click', async () => {
        const button = get('accountBtn');
        button.disabled = true;
        show('Loading your account…');
        try {
            const data = await request('/api/check-auth');
            if (!data) return;
            if (!data.authenticated || !data.user) throw new Error('Please sign in again.');
            get('accountProfile').textContent = `${data.user.name} · ${data.user.email}`;
            panel.classList.remove('hidden');
            if (typeof panel.scrollIntoView === 'function') panel.scrollIntoView({behavior:'smooth',block:'start'});
            show('');
            get('currentPassword').focus();
        } catch { show('Unable to load your account. Please try again.'); }
        finally { button.disabled = false; }
    });

    get('closeAccountBtn').addEventListener('click', () => {
        form.reset();
        panel.classList.add('hidden');
        show('');
    });

    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = get('passwordSubmit');
        if (button.disabled) return;
        const current = get('currentPassword').value;
        const replacement = get('newPassword').value;
        if (replacement !== get('confirmPassword').value) {
            show('New passwords do not match.');
            return;
        }
        if (!current || replacement.length < 8 || replacement.length > 128 || current.length > 128) {
            show('Enter your current password and a new password of 8-128 characters.');
            return;
        }
        button.disabled = true;
        show('Changing your password…');
        try {
            const data = await request('/api/customer/password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password: current, new_password: replacement })
            });
            if (!data) return;
            form.reset();
            get('password').value = '';
            show('Password changed. Please sign in again.');
            signIn();
        } catch (error) {
            show(error instanceof TypeError ? 'Connection failed. Please try again.' : error.message);
        } finally { button.disabled = false; }
    });

    get('logoutBtn').addEventListener('click', async () => {
        const button = get('logoutBtn');
        if (button.disabled) return;
        button.disabled = true;
        show('Signing out…');
        try {
            const data = await request('/api/customer/logout', { method: 'POST' });
            if (!data) return;
            form.reset();
            get('password').value = '';
            signIn();
        } catch { show('Unable to sign out. Please try again.'); }
        finally { button.disabled = false; }
    });
})();
