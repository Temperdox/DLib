// Popup script — pings DLib, displays cache stats, exposes Clear Cache.

const API_BASES = ['http://127.0.0.1:8000', 'http://localhost:8000'];

const statusEl = document.getElementById('status');
const statusText = document.getElementById('status-text');
const serverMeta = document.getElementById('server-meta');
const cacheRow = document.getElementById('cache-row');
const cacheText = document.getElementById('cache-text');
const openBtn = document.getElementById('open-btn');
const clearBtn = document.getElementById('clear-btn');
const syncBtn = document.getElementById('sync-btn');
const outboxRow = document.getElementById('outbox-row');
const outboxText = document.getElementById('outbox-text');

async function pingServer() {
    for (const base of API_BASES) {
        try {
            const resp = await fetch(base + '/api/v1/health/', { cache: 'no-store' });
            if (!resp.ok) continue;
            return { base, data: await resp.json() };
        } catch (_) {}
    }
    return null;
}

function sendBg(message) {
    return new Promise(resolve => {
        try {
            chrome.runtime.sendMessage(message, (response) => {
                if (chrome.runtime.lastError) {
                    resolve(null); return;
                }
                resolve(response);
            });
        } catch (_) {
            resolve(null);
        }
    });
}

function fmtAge(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60);
    if (h < 48) return h + 'h';
    return Math.floor(h / 24) + 'd';
}

function fmtBytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(2) + ' MB';
}

async function refreshAll() {
    // Server health
    statusEl.className = 'row checking';
    statusText.textContent = 'Checking server…';
    serverMeta.style.display = 'none';
    openBtn.disabled = true;

    const result = await pingServer();
    if (result) {
        statusEl.className = 'row ok';
        statusText.textContent = 'Connected — ' + result.data.app + ' v' + result.data.version;
        serverMeta.style.display = '';
        document.getElementById('stat-games').textContent = result.data.games;
        document.getElementById('stat-sources').textContent = (result.data.sources || []).join(', ');
        document.getElementById('stat-base').textContent = result.base;
        openBtn.disabled = false;
        openBtn.onclick = () => chrome.tabs.create({ url: result.base + '/' });
    } else {
        statusEl.className = 'row down';
        statusText.textContent = 'DLib server not reachable';
    }

    // Cache stats
    cacheText.textContent = 'Loading cache stats…';
    const stats = await sendBg({ type: 'cache_stats' });
    if (!stats) {
        cacheText.textContent = 'Cache: unavailable';
        cacheRow.className = 'row down';
        return;
    }
    if (!stats.count) {
        cacheText.textContent = result
            ? 'Cache: empty — syncing…'
            : 'Cache: empty';
        cacheRow.className = 'row checking';
        // If we're online but the cache is empty, the SW hasn't run its
        // initial sync yet (or it's still in flight). Kick one now so the
        // user sees progress instead of staring at "empty".
        if (result) {
            sendBg({ type: 'full_sync' }).then(() => refreshAll());
        }
    } else {
        const synced = stats.last_full_sync_at
            ? ' · last full sync ' + fmtAge(Date.now() - stats.last_full_sync_at) + ' ago'
            : '';
        cacheText.textContent =
            'Cache: ' + stats.count + ' games · '
            + fmtBytes(stats.bytes) + synced;
        cacheRow.className = 'row ok';
    }

    // Outbox: only shown when there's pending offline work.
    const pending = stats.outbox_size || 0;
    if (pending > 0) {
        outboxRow.style.display = '';
        outboxRow.className = result ? 'row checking' : 'row down';
        outboxText.textContent = 'Pending: ' + pending
            + ' offline ' + (pending === 1 ? 'write' : 'writes')
            + (result ? ' — syncing…' : ' (will sync when online)');
    } else {
        outboxRow.style.display = 'none';
    }
    // Resync button: visible whenever the server is reachable so the user
    // can force a refresh, plus when offline + queued writes need draining.
    syncBtn.style.display = (result || pending > 0) ? '' : 'none';
}

syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true;
    const orig = syncBtn.textContent;
    syncBtn.textContent = 'Syncing…';
    const r = await sendBg({ type: 'outbox_drain' });
    syncBtn.disabled = false;
    syncBtn.textContent = orig;
    if (r && !r.error) {
        await refreshAll();
        if (r.offline) {
            alert('DLib is still offline. Pending writes will sync automatically when it comes back.');
        }
    } else {
        alert('Sync failed: ' + (r && r.error ? r.error : 'unknown error'));
    }
});

clearBtn.addEventListener('click', async () => {
    if (!confirm('Clear all cached DLib data? Pills on offline pages will disappear until the server is reachable again.')) return;
    clearBtn.disabled = true;
    clearBtn.textContent = 'Clearing…';
    const r = await sendBg({ type: 'cache_clear' });
    clearBtn.disabled = false;
    clearBtn.textContent = 'Clear cache';
    if (r && r.ok) {
        await refreshAll();
    } else {
        alert('Clear failed: ' + (r && r.error ? r.error : 'unknown error'));
    }
});

refreshAll();
