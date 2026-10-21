/* Server-backed, customer-owned library. Never use innerHTML or localStorage. */
(() => {
    const get = id => document.getElementById(id);
    const message = get('libraryMessage');
    const show = text => {
        message.textContent = text;
        const panelMessage = get('libraryPanelMessage'); if (panelMessage) panelMessage.textContent = text;
    };
    const save = get('librarySave');
    const labels = { promotional_copy: 'Promotional copy', product_description: 'Product description',
        social_caption: 'Social caption', ad_headline: 'Ad headline' };
    let pack = null, saved = false, saving = false, loading = false;
    let sessionRevision = 0;
    let version = 0, next = null, current = null;
    const updateSave = () => { save.disabled = !pack || saved || saving; };
    document.addEventListener('marketing-pack-pending', () => {
        version++; pack = null; saved = false; updateSave();
    });
    document.addEventListener('marketing-pack-ready', event => {
        version++; pack = event.detail; saved = false; updateSave();
    });

    document.addEventListener('customer-session-ended', () => {
        ++sessionRevision; ++version; pack = null; saved = false; next = null; current = null; loading = false; saving = false;
        updateSave(); get('libraryItems').replaceChildren(); get('libraryPanel').classList.add('hidden');
    });
    async function request(url, options = {}) {
        const epoch = sessionRevision;
        let response;
        try { response = await fetch(url, { credentials: 'same-origin', ...options }); }
        catch { throw new Error('Connection failed. Please retry; duplicate saves do not create another item.'); }
        if (epoch !== sessionRevision) throw new Error('Your session has ended.');
        if (response.status === 401) {
            if (typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('customer-session-ended'));
            throw new Error('Please sign in to use your library.');
        }
        let data;
        try { data = await response.json(); }
        catch { throw new Error('Library response was unreadable. Please try again.'); }
        if (epoch !== sessionRevision) throw new Error('Your session has ended.');
        if (!response.ok || !data.success) {
            throw new Error(response.status === 401 ? 'Please sign in to use your library.' :
                (data.error || 'Unable to update your library. Please try again.'));
        }
        return data;
    }
    async function copy(field) {
        try {
            await navigator.clipboard.writeText(field.value);
            show('Copied.');
        } catch {
            field.focus(); field.select();
            show('Text selected. Copy it manually.');
        }
    }
    function render(items) {
        const container = get('libraryItems');
        container.replaceChildren();
        if (!items.length) {
            const empty = document.createElement('p');
            empty.textContent = current === null ? 'Your library is empty. Generate and save a marketing pack with an eligible plan.' :
                'No saved items on this page. Refresh to return to the first page.';
            container.appendChild(empty);
        }
        items.forEach(item => {
            const card = document.createElement('article');
            const title = document.createElement('h3');
            title.textContent = item.product_name;
            card.appendChild(title);
            const facts = document.createElement('p');
            facts.textContent = `${item.created_at} UTC · ${item.details.audience} · ${item.details.tone} · ${item.details.goal}`;
            card.appendChild(facts);
            Object.entries(labels).forEach(([key, label]) => {
                const heading = document.createElement('h4'); heading.textContent = label;
                const field = document.createElement('textarea');
                field.value = item.outputs[key]; field.readOnly = true; field.rows = 4;
                field.style.width = '100%'; field.setAttribute('aria-label', label);
                const button = document.createElement('button');
                button.type = 'button'; button.textContent = 'Copy ' + label;
                button.addEventListener('click', () => copy(field));
                card.appendChild(heading); card.appendChild(field); card.appendChild(button);
            });
            const remove = document.createElement('button');
            remove.type = 'button'; remove.textContent = 'Delete saved pack';
            remove.addEventListener('click', async () => {
                if (remove.disabled || !window.confirm('Delete this saved marketing pack? This cannot be undone.')) return;
                remove.disabled = true;
                try {
                    await request('/api/marketing/library/' + item.id, { method: 'DELETE' });
                    await load(current);
                    show('Saved pack deleted.');
                } catch (error) { show(error.message); }
                finally { remove.disabled = false; }
            });
            card.appendChild(remove); container.appendChild(card);
        });
    }
    async function load(before = null) {
        if (loading) return;
        const epoch = sessionRevision;
        loading = true;
        get('libraryRefresh').disabled = true; get('libraryNext').disabled = true;
        show('Loading your library…');
        try {
            const data = await request('/api/marketing/library' + (before === null ? '' : '?before=' + before));
            if (!Array.isArray(data.items)) throw new Error('Library response was incomplete. Please retry.');
            current = before; next = data.next_cursor;
            render(data.items);
            show(data.items.length ? 'Your saved marketing packs.' : 'No saved packs on this page.');
        } catch (error) { if (epoch === sessionRevision) show(error.message); }
        finally {
            if (epoch !== sessionRevision) return;
            loading = false; get('libraryRefresh').disabled = false;
            get('libraryNext').disabled = next === null;
        }
    }
    save.addEventListener('click', async () => {
        if (!pack || saved || saving) return;
        const pending = pack, started = version, epoch = sessionRevision;
        saving = true; updateSave(); show('Saving your marketing pack…');
        try {
            await request('/api/marketing/library', { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pending) });
            if (started === version) saved = true;
            show('Saved to your library. Open or refresh the library to view it.');
        } catch (error) { if (epoch === sessionRevision) show(error.message); }
        finally { if (epoch === sessionRevision) { saving = false; updateSave(); } }
    });
    get('libraryOpen').addEventListener('click', () => {
        get('libraryPanel').classList.remove('hidden');
        if (typeof get('libraryPanel').scrollIntoView === 'function') get('libraryPanel').scrollIntoView({behavior:'smooth',block:'start'});
        return load();
    });
    get('libraryRefresh').addEventListener('click', () => load());
    get('libraryNext').addEventListener('click', () => { if (next !== null) return load(next); });
    updateSave();
})();
