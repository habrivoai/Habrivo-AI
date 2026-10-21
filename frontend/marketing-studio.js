/* Content is never persisted in browser storage or rendered as HTML. */
(() => {
    const get = id => document.getElementById(id);
    const keys = ['promotional_copy', 'product_description', 'social_caption', 'ad_headline'];
    const limits = [900, 1200, 600, 120];
    const form = get('marketingForm');
    const button = get('marketingSubmit');
    const status = get('marketingStatus');
    const results = get('marketingResults');
    let revision = 0;
    if (typeof document.addEventListener === 'function') document.addEventListener('customer-session-ended', () => {
        ++revision; button.disabled = false; form.setAttribute('aria-busy','false'); results.classList.add('hidden'); keys.forEach(key => { get('marketing_' + key).value = ''; });
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (button.disabled) return;
        const version = ++revision; let completed = false;
        if (typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('marketing-pack-pending'));
        results.classList.add('hidden');
        keys.forEach(key => { get('marketing_' + key).value = ''; });
        const payload = {
            product_name: get('marketingName').value.trim(),
            description: get('marketingDescription').value.trim(),
            audience: get('marketingAudience').value.trim(),
            tone: get('marketingTone').value,
            goal: get('marketingGoal').value
        };
        if (!payload.product_name || !payload.description || !payload.audience ||
            payload.product_name.length > 120 || payload.description.length > 1600 || payload.audience.length > 300) {
            status.textContent = 'Enter product name, description and audience within the field limits.';
            return;
        }
        button.disabled = true;
        form.setAttribute('aria-busy', 'true');
        status.textContent = 'Generating your marketing pack… Please keep this page open.';
        try {
            const response = await fetch('/api/marketing/generate', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
            });
            if (version !== revision) return;
            if (response.status === 401) {
                if (typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('customer-session-ended'));
                status.textContent = 'Please sign in to continue.';
                return;
            }
            let data;
            try { data = await response.json(); }
            catch { throw new Error('The service returned an unreadable response. Please try later.'); }
            if (version !== revision) return;
            if (!response.ok || !data.success) {
                const counted = data.attempt_counted ? ' This attempt counted toward your allowance. Wait at least 60 seconds before retrying.' : '';
                throw new Error((data.error || 'Unable to generate your marketing pack.') + counted);
            }
            if (!data.outputs || Object.keys(data.outputs).length !== keys.length ||
                !keys.every((key, index) => typeof data.outputs[key] === 'string' && data.outputs[key].trim() &&
                    Array.from(data.outputs[key]).length <= limits[index] &&
                    !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(data.outputs[key])) ||
                !data.quota || !Number.isInteger(data.quota.used) || !Number.isInteger(data.quota.limit) ||
                data.quota.used < 1 || data.quota.limit < data.quota.used ||
                typeof data.save_token !== 'string' || !data.save_token || data.save_token.length > 4096) {
                throw new Error('The service returned an incomplete pack. Please contact support.');
            }
            completed = true;
            keys.forEach(key => { get('marketing_' + key).value = data.outputs[key]; });
            if (typeof data.save_token === 'string' && typeof document.dispatchEvent === 'function') {
                document.dispatchEvent(new CustomEvent('marketing-pack-ready', { detail: { details: payload, outputs: data.outputs, save_token: data.save_token } }));
            }
            results.classList.remove('hidden');
            status.textContent = `Marketing pack ready. ${data.quota.used} of ${data.quota.limit} ${data.quota.period === 'lifetime' ? 'lifetime trial' : 'monthly'} attempts used. Save to your library or copy before leaving.`;
        } catch (error) {
            if (version !== revision) return;
            status.textContent = error instanceof TypeError
                ? 'Connection lost. The attempt may have counted. Wait at least 60 seconds before retrying.'
                : error.message;
        } finally {
            if (version !== revision) return;
            if (!completed && typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('marketing-attempt-finished'));
            button.disabled = false;
            form.setAttribute('aria-busy', 'false');
        }
    });
    document.querySelectorAll('[data-marketing-copy]').forEach(copy => {
        copy.addEventListener('click', async () => {
            const field = get('marketing_' + copy.dataset.marketingCopy);
            try {
                await navigator.clipboard.writeText(field.value);
                status.textContent = 'Copied. Review the content before publishing.';
            } catch {
                field.focus();
                field.select();
                status.textContent = 'Clipboard unavailable. The text is selected; copy it manually.';
            }
        });
    });
})();
