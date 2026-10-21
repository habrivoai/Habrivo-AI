/* Customer identity comes only from authenticated backend responses. */
(() => {
    const get = id => document.getElementById(id);
    let signup = new URLSearchParams(window.location.search).get('mode') === 'signup';
    let revision = 0, busy = false;
    const hide = (id, value) => get(id).classList.toggle('hidden', value);
    function authMode() {
        hide('nameField', !signup); get('name').required = signup;
        get('subtitle').textContent = signup ? 'Create your customer account' : 'Login to your AI workspace';
        get('submitBtn').textContent = signup ? 'Create Account' : 'Login';
        get('switchMode').textContent = signup ? 'Already registered? Login' : 'New customer? Create account';
        get('password').autocomplete = signup ? 'new-password' : 'current-password';
    }
    function clearPrivate() {
        hide('welcome', true); hide('accountPanel', true); hide('libraryPanel', true); hide('marketingResults', true);
        ['email','name','password','currentPassword','newPassword','confirmPassword','marketingName','marketingDescription','marketingAudience',
         'marketing_promotional_copy','marketing_product_description','marketing_social_caption','marketing_ad_headline'].forEach(id => { get(id).value = ''; });
        ['welcomeName','accountProfile','accountMessage','libraryMessage','libraryPanelMessage','marketingStatus','dashboardPlan','dashboardSubscription','dashboardUsage','aiAccessStatus','accountAccess'].forEach(id => { get(id).textContent = ''; });
        get('libraryItems').replaceChildren(); get('marketingLock').textContent = 'Sign in to check generation access.'; get('marketingFields').disabled = true; get('librarySave').disabled = true;
    }
    function showAuth(text = '') {
        clearPrivate(); hide('authForm', false); hide('switchMode', false); hide('authExplanation', false); hide('recoveryLink', false);
        get('message').textContent = text; authMode();
    }
    function signedIn(user) {
        if (!user || !Number.isInteger(user.id) || typeof user.name !== 'string') throw new Error();
        hide('authForm', true); hide('switchMode', true); hide('authExplanation', true); hide('recoveryLink', true); hide('sessionRetry', true);
        get('password').value = ''; get('message').textContent = ''; get('subtitle').textContent = 'Your customer workspace';
        get('welcomeName').textContent = 'Welcome, ' + user.name + '!'; hide('welcome', false);
        document.dispatchEvent(new Event('customer-authenticated'));
    }
    async function restore() {
        const version = ++revision; busy = true; get('submitBtn').disabled = true;
        clearPrivate(); hide('authForm', true); hide('switchMode', true); hide('sessionRetry', true);
        get('message').textContent = 'Checking your sign-in…';
        try {
            const response = await fetch('/api/check-auth', {credentials: 'same-origin', cache: 'no-store'});
            const data = await response.json();
            if (version !== revision) return;
            if (response.status === 401) { showAuth(); return; }
            if (!response.ok || data.authenticated !== true) throw new Error();
            signedIn(data.user);
        } catch {
            if (version !== revision) return;
            showAuth('Unable to check your session. Retry or sign in.'); hide('sessionRetry', false);
        } finally { if (version === revision) { busy = false; get('submitBtn').disabled = false; } }
    }
    get('switchMode').addEventListener('click', () => { if (busy) return; signup = !signup; authMode(); get('message').textContent = ''; });
    get('sessionRetry').addEventListener('click', restore);
    get('authForm').addEventListener('submit', async event => {
        event.preventDefault(); if (busy) return;
        const version = ++revision; busy = true; get('submitBtn').disabled = true; hide('sessionRetry', true);
        get('message').textContent = signup ? 'Creating your account…' : 'Signing in…';
        const creating = signup;
        const payload = {email: get('email').value, password: get('password').value};
        if (creating) payload.name = get('name').value;
        try {
            const response = await fetch(creating ? '/api/register' : '/api/customer/login', {
                method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
            });
            const data = await response.json(); if (version !== revision) return;
            if (!response.ok || data.success !== true) {
                get('message').textContent = response.status === 429 ? 'Too many attempts. Please wait before trying again.' :
                    response.status === 401 ? 'Invalid email or password.' : 'Unable to complete sign-in or registration. Check your details or retry later.';
                return;
            }
            if (creating) { signup = false; authMode(); get('password').value = ''; get('message').textContent = 'Registration request complete. Sign in to view verification status and request your email link. Eligible new accounts unlock 3 lifetime attempts only after verification.'; }
            else signedIn(data.user);
        } catch { if (version === revision) get('message').textContent = 'Connection failed or response unavailable. Please try again.'; }
        finally { if (version === revision) { busy = false; get('submitBtn').disabled = false; } }
    });
    get('aiChatBtn').addEventListener('click', () => get('marketingStudio').scrollIntoView({behavior: 'smooth'}));
    document.querySelectorAll('[data-dashboard-action]').forEach(link => link.addEventListener('click', event => {
        event.preventDefault();
        const target = {library:'productsBtn',account:'accountBtn',logout:'logoutBtn'}[link.dataset.dashboardAction];
        if (target) get(target).click();
    }));
    document.addEventListener('customer-session-ended', () => {
        ++revision; busy = false; get('submitBtn').disabled = false; showAuth('Your session has ended. Please sign in again.');
    });
    window.addEventListener('pagehide', () => document.dispatchEvent(new Event('customer-session-ended')));
    window.addEventListener('pageshow', event => { if (event.persisted) restore(); });
    authMode(); restore();
})();
