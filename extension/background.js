// DLib Library Overlay — background service worker.
//
// Owns the IndexedDB cache and brokers every API call so all extension
// contexts (content scripts on multiple tabs + popup) share one source of
// truth. Cache survives browser restarts; DLib being offline becomes a
// degraded-but-still-functional state instead of "no data".

const API_BASES = [
    'http://127.0.0.1:8000',
    'http://localhost:8000',
];

let activeBase = null;
let serverReachable = true;

const DB_NAME = 'dlib-overlay';
const DB_VERSION = 1;
const STORE = 'games';

// ---- IndexedDB helpers ----------------------------------------------------

function openDb() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onerror = () => reject(req.error);
        req.onupgradeneeded = (event) => {
            const db = event.target.result;
            if (!db.objectStoreNames.contains(STORE)) {
                db.createObjectStore(STORE, { keyPath: 'cache_key' });
            }
        };
        req.onsuccess = () => resolve(req.result);
    });
}

async function cachePut(payloads) {
    if (!payloads.length) return;
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    const now = Date.now();
    for (const p of payloads) {
        if (!p || !p.source || !p.source_id) continue;
        store.put({
            cache_key: p.source + ':' + p.source_id,
            payload: p,
            cached_at: now,
        });
    }
    await new Promise((resolve, reject) => {
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
    });
}

async function cacheDelete(keys) {
    if (!keys.length) return;
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    for (const k of keys) store.delete(k);
    await new Promise((resolve, reject) => {
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
    });
}

async function cacheGet(keys) {
    if (!keys.length) return [];
    const db = await openDb();
    const tx = db.transaction(STORE, 'readonly');
    const store = tx.objectStore(STORE);
    return Promise.all(keys.map(key => new Promise((resolve) => {
        const req = store.get(key);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => resolve(null);
    })));
}

async function cacheStats() {
    const db = await openDb();
    const tx = db.transaction(STORE, 'readonly');
    const store = tx.objectStore(STORE);
    const count = await new Promise(resolve => {
        const req = store.count();
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(0);
    });
    let newest = 0;
    let oldest = Number.MAX_SAFE_INTEGER;
    let estimateBytes = 0;
    if (count > 0) {
        await new Promise(resolve => {
            const tx2 = db.transaction(STORE, 'readonly');
            const cur = tx2.objectStore(STORE).openCursor();
            cur.onsuccess = (e) => {
                const c = e.target.result;
                if (!c) return resolve();
                const e2 = c.value;
                if (e2.cached_at > newest) newest = e2.cached_at;
                if (e2.cached_at < oldest) oldest = e2.cached_at;
                try { estimateBytes += JSON.stringify(e2).length; } catch (_) {}
                c.continue();
            };
            cur.onerror = () => resolve();
        });
    }
    return {
        count,
        newest_at: newest || null,
        oldest_at: count > 0 ? oldest : null,
        bytes: estimateBytes,
    };
}

async function cacheClear() {
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).clear();
    await new Promise(resolve => tx.oncomplete = resolve);
}

// ---- API fetch ------------------------------------------------------------

async function tryApi(urls) {
    const bases = activeBase
        ? [activeBase, ...API_BASES.filter(b => b !== activeBase)]
        : API_BASES;
    let lastErr = null;
    for (const base of bases) {
        try {
            const resp = await fetch(base + '/api/v1/lookup-bulk/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ urls }),
                cache: 'no-store',
            });
            if (!resp.ok) {
                lastErr = new Error('HTTP ' + resp.status);
                continue;
            }
            const data = await resp.json();
            const wasDown = !serverReachable;
            activeBase = base;
            serverReachable = true;
            return { ok: true, results: data.results || {}, recovered: wasDown };
        } catch (e) {
            lastErr = e;
        }
    }
    if (serverReachable) {
        console.debug('[DLib] API went offline:', lastErr);
        serverReachable = false;
    }
    return { ok: false, results: {}, recovered: false };
}

// ---- Main lookup flow -----------------------------------------------------

async function handleLookup(items) {
    // items: [{ url, source, id }, ...]
    if (!Array.isArray(items) || items.length === 0) {
        return { results: {}, server_up: serverReachable, recovered: false };
    }
    const uniqueUrls = Array.from(new Set(items.map(i => i.url)));
    console.log('[DLib SW] handleLookup → urls=' + uniqueUrls.length, uniqueUrls);

    const apiResp = await tryApi(uniqueUrls);

    if (apiResp.ok) {
        // Log per-URL flag state so we can see whether the server actually
        // returns is_bad=true after an upsert that asked for it.
        const summary = {};
        for (const u in apiResp.results) {
            const p = apiResp.results[u];
            if (p && p.found) {
                summary[u] = {
                    found: true, is_bad: p.is_bad, is_favorite: p.is_favorite,
                };
            } else {
                summary[u] = { found: false };
            }
        }
        console.log('[DLib SW] handleLookup ←', summary);
    } else {
        console.warn('[DLib SW] handleLookup ← API down');
    }
    if (apiResp.ok) {
        // Persist found entries, evict not-found ones from cache.
        const toCache = [];
        const toDelete = [];
        for (const url in apiResp.results) {
            const p = apiResp.results[url];
            if (!p) continue;
            if (p.found) toCache.push(p);
            else if (p.source && p.source_id) {
                toDelete.push(p.source + ':' + p.source_id);
            }
        }
        try {
            await Promise.all([cachePut(toCache), cacheDelete(toDelete)]);
        } catch (e) {
            console.warn('[DLib] cache write failed', e);
        }
        return {
            results: apiResp.results,
            server_up: true,
            recovered: apiResp.recovered,
            from_cache: false,
        };
    }

    // API down → cache fallback.
    const keys = items.map(i => i.source + ':' + i.id);
    const cached = await cacheGet(keys);
    const results = {};
    for (let i = 0; i < items.length; i++) {
        const entry = cached[i];
        const url = items[i].url;
        if (entry && entry.payload) {
            results[url] = {
                ...entry.payload,
                _cached: true,
                _cached_at: entry.cached_at,
            };
        }
        // else: leave out — the content script treats missing entries as
        // "we don't know" and leaves the pill alone (or removes it).
    }
    return {
        results,
        server_up: false,
        recovered: false,
        from_cache: true,
    };
}

// ---- Message router -------------------------------------------------------

async function handleLink(primaryUrl, aliasUrl) {
    if (!primaryUrl || !aliasUrl) return { ok: false, error: 'urls required' };
    const bases = activeBase
        ? [activeBase, ...API_BASES.filter(b => b !== activeBase)]
        : API_BASES;
    let lastErr = null;
    for (const base of bases) {
        try {
            const resp = await fetch(base + '/api/v1/link/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ primary_url: primaryUrl, alias_url: aliasUrl }),
                cache: 'no-store',
            });
            if (!resp.ok) {
                const text = await resp.text();
                lastErr = new Error('HTTP ' + resp.status + ' ' + text.slice(0, 200));
                if (resp.status === 404 || resp.status === 409) {
                    // primary not in library / alias claimed elsewhere — don't retry other bases
                    return { ok: false, error: lastErr.message };
                }
                continue;
            }
            activeBase = base;
            serverReachable = true;
            return { ok: true, payload: await resp.json() };
        } catch (e) {
            lastErr = e;
        }
    }
    return { ok: false, error: String(lastErr || 'unreachable') };
}


async function handleUpsert(url, changes) {
    if (!url) return { ok: false, error: 'url required' };
    const bases = activeBase
        ? [activeBase, ...API_BASES.filter(b => b !== activeBase)]
        : API_BASES;
    let lastErr = null;
    const body = { url, ...(changes || {}) };
    console.log('[DLib SW] handleUpsert →', body);
    for (const base of bases) {
        try {
            const resp = await fetch(base + '/api/v1/upsert/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                cache: 'no-store',
            });
            if (!resp.ok) {
                const text = await resp.text();
                lastErr = new Error('HTTP ' + resp.status + ' ' + text.slice(0, 200));
                console.warn('[DLib SW] handleUpsert ← HTTP', resp.status,
                             'base=' + base, 'body=' + text.slice(0, 400));
                continue;
            }
            const data = await resp.json();
            console.log('[DLib SW] handleUpsert ←', {
                status: resp.status,
                base,
                found: data && data.found,
                is_bad: data && data.is_bad,
                is_favorite: data && data.is_favorite,
                created: data && data.created,
                source: data && data.source,
                source_id: data && data.source_id,
            });
            activeBase = base;
            serverReachable = true;
            if (data && data.found && data.source && data.source_id) {
                try { await cachePut([data]); } catch (e) { /* ignore */ }
            }
            return { ok: true, payload: data };
        } catch (e) {
            console.warn('[DLib SW] handleUpsert THREW', String(e), 'base=' + base);
            lastErr = e;
        }
    }
    if (serverReachable) {
        serverReachable = false;
    }
    console.error('[DLib SW] handleUpsert FAILED all bases', String(lastErr));
    return { ok: false, error: String(lastErr || 'unreachable') };
}


chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || !msg.type) return;
    try {
        if (msg.type === 'lookup') {
            handleLookup(msg.items)
                .then(sendResponse)
                .catch(err => {
                    console.error('[DLib] lookup failed', err);
                    sendResponse({ results: {}, server_up: serverReachable,
                                   recovered: false, error: String(err) });
                });
            return true;
        }
        if (msg.type === 'upsert') {
            handleUpsert(msg.url, msg.changes)
                .then(sendResponse)
                .catch(err => sendResponse({ ok: false, error: String(err) }));
            return true;
        }
        if (msg.type === 'link') {
            handleLink(msg.primary_url, msg.alias_url)
                .then(sendResponse)
                .catch(err => sendResponse({ ok: false, error: String(err) }));
            return true;
        }
        if (msg.type === 'cache_stats') {
            cacheStats().then(sendResponse).catch(err =>
                sendResponse({ count: 0, error: String(err) }));
            return true;
        }
        if (msg.type === 'cache_clear') {
            cacheClear().then(() => sendResponse({ ok: true })).catch(err =>
                sendResponse({ ok: false, error: String(err) }));
            return true;
        }
        if (msg.type === 'state') {
            sendResponse({
                server_up: serverReachable,
                active_base: activeBase,
            });
            return; // sync response
        }
    } catch (err) {
        console.error('[DLib] message handler crashed', err);
        sendResponse({ error: String(err) });
    }
});
