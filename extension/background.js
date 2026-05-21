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
// Bumped from 1 → 2 to add the outbox store. onupgradeneeded creates the
// new store without touching the existing 'games' cache.
const DB_VERSION = 2;
const STORE = 'games';
const OUTBOX = 'outbox';

// Probe the server when offline so a queued write drains as soon as the
// app comes back, without waiting for the user to hit a page. Uses
// chrome.alarms so the probe survives MV3 service-worker suspensions —
// setInterval wouldn't, since the SW gets torn down between events.
const PROBE_ALARM = 'dlib-offline-probe';
const PROBE_INTERVAL_MIN = 1;  // minimum allowed in chrome.alarms

// Pull the full library into the cache on connect / reconnect / every
// hour so games the user hasn't browsed recently still show pills when
// DLib goes offline. Without this the cache fills lazily and the popup
// shows "Cache: empty" right after install even though DLib reports
// hundreds of games.
const FULL_SYNC_ALARM = 'dlib-full-sync';
const FULL_SYNC_INTERVAL_MIN = 60;
let _lastFullSyncAt = 0;
let _fullSyncing = false;

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
            if (!db.objectStoreNames.contains(OUTBOX)) {
                // autoIncrement so we can replay in FIFO order. Each op has
                // { op_type, url, body, queued_at, attempts }.
                db.createObjectStore(OUTBOX, {
                    keyPath: 'id',
                    autoIncrement: true,
                });
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

// ---- Outbox (offline write queue) -----------------------------------------
//
// Every upsert / link that fails because the app is offline lands here.
// drainOutbox() replays them in FIFO order whenever we detect the server
// is reachable again. Replays use the same idempotent API endpoints, so
// re-sending an op produces the same end state regardless of how often
// the user toggled the same field in the meantime.

async function outboxAdd(opType, url, body) {
    const db = await openDb();
    const tx = db.transaction(OUTBOX, 'readwrite');
    tx.objectStore(OUTBOX).add({
        op_type: opType,           // 'upsert' | 'link'
        url: url,                  // primary URL (upsert) or { primary, alias }
        body: body || {},          // payload as sent to the API
        queued_at: Date.now(),
        attempts: 0,
    });
    await new Promise((resolve, reject) => {
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
    });
}

async function outboxList() {
    const db = await openDb();
    const tx = db.transaction(OUTBOX, 'readonly');
    const store = tx.objectStore(OUTBOX);
    return new Promise((resolve) => {
        const out = [];
        const cur = store.openCursor();
        cur.onsuccess = (e) => {
            const c = e.target.result;
            if (!c) return resolve(out);
            out.push(c.value);
            c.continue();
        };
        cur.onerror = () => resolve(out);
    });
}

async function outboxRemove(id) {
    const db = await openDb();
    const tx = db.transaction(OUTBOX, 'readwrite');
    tx.objectStore(OUTBOX).delete(id);
    await new Promise(resolve => tx.oncomplete = resolve);
}

async function outboxBumpAttempt(id) {
    const db = await openDb();
    const tx = db.transaction(OUTBOX, 'readwrite');
    const store = tx.objectStore(OUTBOX);
    await new Promise((resolve) => {
        const req = store.get(id);
        req.onsuccess = () => {
            const entry = req.result;
            if (!entry) return resolve();
            entry.attempts = (entry.attempts || 0) + 1;
            store.put(entry);
            resolve();
        };
        req.onerror = () => resolve();
    });
    await new Promise(resolve => tx.oncomplete = resolve);
}

async function outboxSize() {
    const db = await openDb();
    const tx = db.transaction(OUTBOX, 'readonly');
    return new Promise((resolve) => {
        const req = tx.objectStore(OUTBOX).count();
        req.onsuccess = () => resolve(req.result || 0);
        req.onerror = () => resolve(0);
    });
}

async function cacheGetOne(key) {
    const arr = await cacheGet([key]);
    return arr[0] || null;
}

// Restore a prior cache entry (or delete the key if there was none) — used
// to undo an optimistic write whose server confirmation never arrived.
async function cacheRollback(key, priorEntry) {
    if (priorEntry) {
        const db = await openDb();
        const tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).put(priorEntry);
        await new Promise((resolve, reject) => {
            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error);
        });
    } else {
        await cacheDelete([key]);
    }
}

// Write an optimistic cache entry reflecting `changes`, creating a synthetic
// payload when the game isn't cached yet (a not-in-library mark). Marked
// with _pending so a later real payload (or rollback) supersedes it.
async function cacheOptimisticUpsert(key, url, changes, meta) {
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    const existing = await new Promise((resolve) => {
        const r = store.get(key);
        r.onsuccess = () => resolve(r.result || null);
        r.onerror = () => resolve(null);
    });
    const payload = existing && existing.payload ? { ...existing.payload } : {
        found: true,
        source: (meta && meta.source) || null,
        source_id: (meta && meta.source_id) || null,
        url: url,
        title: (meta && meta.title) || url,
        is_installed: false,
        is_running: false,
        is_bad: false,
        is_favorite: false,
        status: 'unknown',
        personal_rating: null,
        time_played_seconds: 0,
        library_url: null,
    };
    if ('is_bad' in changes) payload.is_bad = !!changes.is_bad;
    if ('is_favorite' in changes) payload.is_favorite = !!changes.is_favorite;
    if ('status' in changes) payload.status = changes.status;
    if ('personal_rating' in changes) payload.personal_rating = changes.personal_rating;
    store.put({ cache_key: key, payload: payload, cached_at: Date.now(), _pending: true });
    await new Promise((resolve, reject) => {
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
    });
}

// Broadcast a message to every f95zone / dlsite content script. Used to tell
// overlays to refresh (cache_updated) or to surface a failure toast on
// whichever tab is alive (mark_failed / mark_recovered). Host permissions
// for those origins let us query + message the tabs without "tabs" perm.
function broadcastToContentTabs(message) {
    try {
        chrome.tabs.query(
            { url: ['https://f95zone.to/*', 'https://www.dlsite.com/*'] },
            (tabs) => {
                if (chrome.runtime.lastError) return;
                for (const t of tabs || []) {
                    if (t.id == null) continue;
                    chrome.tabs.sendMessage(t.id, message, () => {
                        // Swallow "no receiving end" for tabs without the
                        // content script loaded yet.
                        void chrome.runtime.lastError;
                    });
                }
            }
        );
    } catch (_) { /* chrome.tabs unavailable — ignore */ }
}

function _changeLabel(changes) {
    if (!changes) return 'updated';
    if (changes.is_bad === true) return 'marked as bad';
    if (changes.is_bad === false) return 'unmarked bad';
    if (changes.is_favorite === true) return 'marked as favorite';
    if (changes.is_favorite === false) return 'unfavorited';
    if ('status' in changes) return 'set to ' + changes.status;
    if ('personal_rating' in changes) return 'rated';
    return 'updated';
}

// Optimistically merge a queued upsert into the cache so the overlay pill
// reflects the user's intent immediately, even though the server hasn't
// seen it yet. The cache_key needs source + source_id, but for a brand-new
// upsert we don't know those locally — so we only patch if we already have
// the entry. (The drain will refresh on send.)
async function cacheOptimisticPatch(url, body) {
    if (!body || typeof body !== 'object') return;
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    await new Promise((resolve) => {
        const cur = store.openCursor();
        cur.onsuccess = (e) => {
            const c = e.target.result;
            if (!c) return resolve();
            const entry = c.value;
            if (entry && entry.payload && entry.payload.url === url) {
                const merged = { ...entry.payload };
                if ('is_bad' in body) merged.is_bad = !!body.is_bad;
                if ('is_favorite' in body) merged.is_favorite = !!body.is_favorite;
                if ('status' in body) merged.status = body.status;
                if ('personal_rating' in body) merged.personal_rating = body.personal_rating;
                entry.payload = merged;
                entry.cached_at = Date.now();
                entry._pending = true;
                c.update(entry);
            }
            c.continue();
        };
        cur.onerror = () => resolve();
    });
    await new Promise(resolve => tx.oncomplete = resolve);
}

// ---- Full library sync ----------------------------------------------------
//
// Replaces the entire IndexedDB cache with the server's current state.
// Called when DLib comes online and once an hour. Anything in the cache
// that's gone from the server (deleted from the library) is dropped.
//
// Outbox is drained BEFORE the cache replace — otherwise an in-flight
// optimistic patch would be clobbered by the server's pre-drain state.

async function syncFullCache() {
    if (_fullSyncing) return { skipped: true };
    _fullSyncing = true;
    try {
        // Drain first so any queued offline writes land on the server
        // before we snapshot back.
        try { await drainOutbox(); } catch (_) { /* drain is best-effort */ }

        const bases = activeBase
            ? [activeBase, ...API_BASES.filter(b => b !== activeBase)]
            : API_BASES;
        let data = null;
        for (const base of bases) {
            try {
                const resp = await fetch(base + '/api/v1/all-games/', {
                    method: 'GET',
                    cache: 'no-store',
                });
                if (!resp.ok) continue;
                data = await resp.json();
                activeBase = base;
                serverReachable = true;
                break;
            } catch (_) { /* try next */ }
        }
        if (!data) {
            return { ok: false, error: 'unreachable' };
        }
        const entries = Array.isArray(data.games) ? data.games : [];

        // Replace the cache atomically: clear, then put, in one tx. If the
        // tx fails the cache is left untouched (no half-wiped state).
        const db = await openDb();
        const tx = db.transaction(STORE, 'readwrite');
        const store = tx.objectStore(STORE);
        store.clear();
        const now = Date.now();
        for (const p of entries) {
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
        _lastFullSyncAt = now;
        console.log('[DLib SW] full cache sync: cached', entries.length, 'entries');
        return { ok: true, count: entries.length };
    } finally {
        _fullSyncing = false;
    }
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
            if (wasDown) {
                scheduleProbe(false);
                // Reconnect — drain + full cache resync in the background.
                // The lookup response itself returns immediately.
                syncFullCache().catch(e =>
                    console.warn('[DLib SW] reconnect sync failed', e));
            }
            return { ok: true, results: data.results || {}, recovered: wasDown };
        } catch (e) {
            lastErr = e;
        }
    }
    if (serverReachable) {
        console.debug('[DLib] API went offline:', lastErr);
        serverReachable = false;
        scheduleProbe(true);
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

async function handleLink(primaryUrl, aliasUrl, opts) {
    if (!primaryUrl || !aliasUrl) return { ok: false, error: 'urls required' };
    const fromOutbox = opts && opts.fromOutbox;
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
                    // Permanent: primary not in library / alias claimed elsewhere.
                    // Don't queue — replaying won't fix it. Drop the outbox entry
                    // if this came from a drain.
                    return { ok: false, error: lastErr.message, permanent: true };
                }
                continue;
            }
            activeBase = base;
            serverReachable = true;
            scheduleProbe(false);
            return { ok: true, payload: await resp.json() };
        } catch (e) {
            lastErr = e;
        }
    }
    // All bases unreachable. Queue (unless we're already replaying from the
    // queue — that would loop) and mark offline.
    if (!fromOutbox) {
        try {
            await outboxAdd('link', { primary: primaryUrl, alias: aliasUrl },
                            { primary_url: primaryUrl, alias_url: aliasUrl });
        } catch (e) {
            console.warn('[DLib SW] outbox enqueue (link) failed', e);
        }
    }
    if (serverReachable) {
        serverReachable = false;
        scheduleProbe(true);
    }
    return {
        ok: false,
        error: String(lastErr || 'unreachable'),
        queued: !fromOutbox,
    };
}


// How long to wait for the app to confirm an optimistic mark before we
// roll it back and surface a failure toast. The server's add path can be
// slow (DLsite metadata + sample downloads), so this is generous.
const OPTIMISTIC_CONFIRM_MS = 30000;

// Raw upsert fetch. Returns one of:
//   { ok: true, payload }
//   { ok: false, permanent: true, error }   (4xx — don't retry/queue)
//   { ok: false, networkError: true, error } (all bases unreachable)
async function _upsertFetch(url, changes) {
    const bases = activeBase
        ? [activeBase, ...API_BASES.filter(b => b !== activeBase)]
        : API_BASES;
    let lastErr = null;
    const body = { url, ...(changes || {}) };
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
                if (resp.status >= 400 && resp.status < 500) {
                    return { ok: false, permanent: true, error: lastErr.message };
                }
                continue;
            }
            const data = await resp.json();
            activeBase = base;
            const wasDown = !serverReachable;
            serverReachable = true;
            if (wasDown) {
                scheduleProbe(false);
                syncFullCache().catch(() => {});
            }
            if (data && data.found && data.source && data.source_id) {
                try { await cachePut([data]); } catch (e) { /* ignore */ }
            }
            return { ok: true, payload: data };
        } catch (e) {
            lastErr = e;
        }
    }
    return { ok: false, networkError: true, error: String(lastErr || 'unreachable') };
}

async function handleUpsert(url, changes, opts) {
    if (!url) return { ok: false, error: 'url required' };
    opts = opts || {};
    const fromOutbox = opts.fromOutbox;
    const meta = opts.meta || null;
    const key = meta && meta.source && meta.source_id
        ? meta.source + ':' + meta.source_id : null;
    const hasChanges = changes && Object.keys(changes).length > 0;
    const optimistic = !fromOutbox && key && hasChanges;
    console.log('[DLib SW] handleUpsert →', { url, changes, optimistic });

    // 1) Optimistic cache write so every open overlay reflects the user's
    //    intent right away — and persists across navigation while the
    //    server works.
    let priorEntry = null;
    if (optimistic) {
        try {
            priorEntry = await cacheGetOne(key);
            await cacheOptimisticUpsert(key, url, changes, meta);
            broadcastToContentTabs({ type: 'cache_updated' });
        } catch (e) {
            console.warn('[DLib SW] optimistic write failed', e);
        }
    }

    const fetchPromise = _upsertFetch(url, changes);

    // 2a) Non-optimistic path (outbox replay, or no derivable key): preserve
    //     the original behavior.
    if (!optimistic) {
        const r = await fetchPromise;
        if (r.ok) return r;
        if (r.permanent) return r;
        if (!fromOutbox) {
            try {
                await outboxAdd('upsert', url, { url, ...(changes || {}) });
                await cacheOptimisticPatch(url, changes || {});
            } catch (e) { /* ignore */ }
        }
        if (serverReachable) { serverReachable = false; scheduleProbe(true); }
        return { ok: false, error: r.error, queued: !fromOutbox, offline: true };
    }

    // 2b) Optimistic path: race the fetch against a confirmation timeout.
    let timer = null;
    const timeout = new Promise((resolve) => {
        timer = setTimeout(() => resolve({ _timeout: true }), OPTIMISTIC_CONFIRM_MS);
    });
    const raced = await Promise.race([fetchPromise, timeout]);
    if (timer) clearTimeout(timer);

    if (raced && raced._timeout) {
        // No confirmation in time → roll back + alert. Keep the fetch alive:
        // if it succeeds late, re-apply and clear the toast (mark_recovered).
        await cacheRollback(key, priorEntry);
        broadcastToContentTabs({ type: 'cache_updated' });
        broadcastToContentTabs({
            type: 'mark_failed', title: meta.title, url: url,
            change: _changeLabel(changes),
        });
        fetchPromise.then(async (late) => {
            if (late && late.ok && late.payload) {
                try { await cachePut([late.payload]); } catch (e) { /* ignore */ }
                broadcastToContentTabs({ type: 'cache_updated' });
                broadcastToContentTabs({
                    type: 'mark_recovered', title: meta.title, url: url,
                });
            }
        }).catch(() => {});
        return { ok: false, timedOut: true,
                 error: 'timed out waiting for DLib confirmation' };
    }

    const r = raced;
    if (r.ok) {
        // _upsertFetch already cached the real payload. Refresh overlays.
        broadcastToContentTabs({ type: 'cache_updated' });
        return r;
    }
    if (r.permanent) {
        await cacheRollback(key, priorEntry);
        broadcastToContentTabs({ type: 'cache_updated' });
        broadcastToContentTabs({
            type: 'mark_failed', title: meta.title, url: url,
            change: _changeLabel(changes),
        });
        return r;
    }
    // Network error → queue for replay and KEEP the optimistic state (the
    // outbox will reconcile it). Mark offline.
    try {
        await outboxAdd('upsert', url, { url, ...(changes || {}) });
    } catch (e) { /* ignore */ }
    if (serverReachable) { serverReachable = false; scheduleProbe(true); }
    return { ok: false, error: r.error, queued: true, offline: true };
}

// ---- Drain + probe --------------------------------------------------------

let _draining = false;

async function drainOutbox() {
    if (_draining) return { skipped: true };
    _draining = true;
    let drained = 0;
    let stillQueued = 0;
    try {
        const entries = await outboxList();
        for (const entry of entries) {
            await outboxBumpAttempt(entry.id);
            let res;
            if (entry.op_type === 'upsert') {
                const { url, ...changes } = entry.body || {};
                res = await handleUpsert(url || entry.url, changes,
                                         { fromOutbox: true });
            } else if (entry.op_type === 'link') {
                const b = entry.body || {};
                res = await handleLink(b.primary_url, b.alias_url,
                                       { fromOutbox: true });
            } else {
                // Unknown op type — drop it, replaying isn't safe.
                await outboxRemove(entry.id);
                continue;
            }
            if (res && (res.ok || res.permanent)) {
                await outboxRemove(entry.id);
                drained++;
            } else {
                // Server still unreachable — stop draining; we'll try
                // again on the next probe tick.
                stillQueued++;
                break;
            }
        }
    } finally {
        _draining = false;
    }
    if (drained > 0) {
        console.log('[DLib SW] outbox drained', drained, 'still=', stillQueued);
    }
    return { drained, still_queued: stillQueued };
}

// Lightweight reachability probe. Scheduled only when we know the server
// is down — otherwise we depend on user activity (lookups / writes) to
// notice connectivity changes, which is cheaper than a constant heartbeat.
async function probeServer() {
    const bases = activeBase
        ? [activeBase, ...API_BASES.filter(b => b !== activeBase)]
        : API_BASES;
    for (const base of bases) {
        try {
            const resp = await fetch(base + '/api/v1/health/', {
                method: 'GET',
                cache: 'no-store',
            });
            if (resp.ok) {
                activeBase = base;
                serverReachable = true;
                scheduleProbe(false);
                // Drain queued writes, then re-fetch the full library so
                // the cache reflects everything the app knows about. The
                // drain happens inside syncFullCache so the two are
                // serialised correctly.
                syncFullCache().catch(e =>
                    console.warn('[DLib SW] post-reconnect sync failed', e));
                return true;
            }
        } catch (_) { /* try next */ }
    }
    return false;
}

function scheduleProbe(enable) {
    if (enable) {
        chrome.alarms.create(PROBE_ALARM, {
            delayInMinutes: PROBE_INTERVAL_MIN,
            periodInMinutes: PROBE_INTERVAL_MIN,
        });
    } else {
        chrome.alarms.clear(PROBE_ALARM);
    }
}

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === PROBE_ALARM) {
        probeServer().catch(e => console.warn('[DLib SW] probe failed', e));
    } else if (alarm.name === FULL_SYNC_ALARM) {
        if (serverReachable) {
            syncFullCache().catch(e => console.warn('[DLib SW] full-sync failed', e));
        }
    }
});

// Run an initial full sync when the SW starts up (extension install /
// browser launch / wake-up). Also schedule the recurring hourly alarm.
// Both are no-ops if DLib is offline — the offline probe takes over
// and we full-sync on its first successful response.
chrome.alarms.create(FULL_SYNC_ALARM, {
    delayInMinutes: FULL_SYNC_INTERVAL_MIN,
    periodInMinutes: FULL_SYNC_INTERVAL_MIN,
});
(async () => {
    // Best-effort initial sync. If DLib is offline this just no-ops.
    try { await syncFullCache(); } catch (_) { /* offline */ }
})();

// When the SW starts up (extension install / browser launch / wake-up
// from suspension), check if there's queued work — if so, kick a probe
// immediately rather than waiting up to a minute for the first alarm.
(async () => {
    try {
        const pending = await outboxSize();
        if (pending > 0) {
            scheduleProbe(true);
            probeServer().catch(() => { /* alarm will retry */ });
        }
    } catch (e) {
        console.warn('[DLib SW] startup probe check failed', e);
    }
})();


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
            handleUpsert(msg.url, msg.changes, { meta: msg.meta })
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
            (async () => {
                const stats = await cacheStats();
                stats.outbox_size = await outboxSize();
                stats.last_full_sync_at = _lastFullSyncAt || null;
                sendResponse(stats);
            })().catch(err => sendResponse({ count: 0, error: String(err) }));
            return true;
        }
        if (msg.type === 'cache_clear') {
            cacheClear().then(() => sendResponse({ ok: true })).catch(err =>
                sendResponse({ ok: false, error: String(err) }));
            return true;
        }
        if (msg.type === 'outbox_list') {
            outboxList().then(list => sendResponse({ items: list }))
                .catch(err => sendResponse({ items: [], error: String(err) }));
            return true;
        }
        if (msg.type === 'outbox_drain') {
            // Forces a probe + drain on demand (popup "Sync now" button).
            // Also kicks a full cache sync so the popup's count reflects
            // anything that changed on the server while we were offline.
            (async () => {
                const reached = await probeServer();
                if (!reached) {
                    sendResponse({ server_up: false, offline: true, drained: 0 });
                    return;
                }
                const res = await syncFullCache();
                sendResponse({
                    server_up: true,
                    drained: (await outboxSize()) === 0 ? 'all' : 'partial',
                    full_sync: res,
                });
            })().catch(err => sendResponse({ error: String(err) }));
            return true;
        }
        if (msg.type === 'full_sync') {
            syncFullCache()
                .then(sendResponse)
                .catch(err => sendResponse({ ok: false, error: String(err) }));
            return true;
        }
        if (msg.type === 'state') {
            (async () => {
                sendResponse({
                    server_up: serverReachable,
                    active_base: activeBase,
                    outbox_size: await outboxSize(),
                    last_full_sync_at: _lastFullSyncAt || null,
                });
            })();
            return true; // async due to outboxSize()
        }
    } catch (err) {
        console.error('[DLib] message handler crashed', err);
        sendResponse({ error: String(err) });
    }
});
