// Popup script — pings DLib, displays cache stats, exposes Clear Cache.

const API_BASES = ['http://127.0.0.1:8000', 'http://localhost:8000'];

const statusEl = document.getElementById('status');
const statusText = document.getElementById('status-text');
const serverMeta = document.getElementById('server-meta');
const cacheRow = document.getElementById('cache-row');
const cacheText = document.getElementById('cache-text');
const openBtn = document.getElementById('open-btn');
const clearBtn = document.getElementById('clear-btn');

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
        cacheText.textContent = 'Cache: empty';
        cacheRow.className = 'row checking';
    } else {
        const age = stats.newest_at
            ? fmtAge(Date.now() - stats.newest_at) + ' ago'
            : 'unknown';
        cacheText.textContent =
            'Cache: ' + stats.count + ' games · '
            + fmtBytes(stats.bytes) + ' · newest ' + age;
        cacheRow.className = 'row ok';
    }
}

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
