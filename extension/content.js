// DLib Library Overlay — content script.
//
// Two distinct treatments, picked per page:
//
//   On a game's OWN thread/product page:
//     - suppress every thread-link pill (avoids spam on tabs/breadcrumbs)
//     - overlay ONE pill on the OP cover image
//     - if marked BAD, add the loud page-wide banner + image-grayscale +
//       download-link disable treatment
//
//   On a listing page (latest updates, search, ...):
//     - per-game dedupe: at most one pill per game id
//     - pill goes on the *best* link to that thread (largest area with img)
//     - a colored card-banner is prepended to the row's container
//
// Re-runs on DOM mutations (debounced), every 30 s for state refresh,
// and on tab-visibility return. All lookups go through the SW's IndexedDB
// cache so offline state still shows up.

(function () {
    'use strict';

    const SCAN_DEBOUNCE_MS = 350;
    const MAX_BATCH = 200;
    const REQUERY_INTERVAL_MS = 30000;

    // ----- link classification --------------------------------------------

    const F95_THREAD_RE = /\/threads\/(?:[^/]+\.)?(\d+)/i;
    const DLSITE_PRODUCT_RE = /\/product_id\/((?:RJ|RE|VJ|BJ|RG)\d+)/i;

    function classify(href) {
        if (!href) return null;
        let m = href.match(F95_THREAD_RE);
        if (m && /f95zone\.to/i.test(href)) return { source: 'f95zone', id: m[1] };
        m = href.match(DLSITE_PRODUCT_RE);
        if (m && /dlsite\.com/i.test(href)) return { source: 'dlsite', id: m[1].toUpperCase() };
        return null;
    }

    function collectLinks() {
        const links = document.querySelectorAll(
            'a[href*="/threads/"], a[href*="/product_id/"]'
        );
        const out = [];
        for (const a of links) {
            const cls = classify(a.href);
            if (!cls) continue;
            const rect = a.getBoundingClientRect();
            if (rect.width < 8 || rect.height < 8) continue;
            out.push({
                link: a,
                url: a.href,
                source: cls.source,
                id: cls.id,
                rect,
                hasImg: !!a.querySelector('img'),
            });
        }
        return out;
    }

    // ----- SW messaging ----------------------------------------------------

    function sendLookup(items) {
        return new Promise(resolve => {
            const payload = items.map(i => ({ url: i.url, source: i.source, id: i.id }));
            try {
                chrome.runtime.sendMessage({ type: 'lookup', items: payload }, (response) => {
                    if (chrome.runtime.lastError) {
                        resolve({ results: {}, server_up: false });
                        return;
                    }
                    resolve(response || { results: {}, server_up: false });
                });
            } catch (_) {
                resolve({ results: {}, server_up: false });
            }
        });
    }

    // ----- pill description ------------------------------------------------

    function pillFor(payload) {
        if (!payload || !payload.found) return null;
        if (payload.is_bad)
            return { kind: 'bad', label: 'DO NOT DOWNLOAD',
                     bannerLabel: '⚠ BAD — DO NOT DOWNLOAD',
                     title: 'Marked as bad: ' + (payload.title || '') };
        if (payload.is_favorite)
            return { kind: 'favorite', label: 'FAVORITE',
                     bannerLabel: '★ FAVORITE',
                     title: 'Favorite: ' + (payload.title || '') };
        if (payload.is_running)
            return { kind: 'running', label: 'RUNNING',
                     bannerLabel: '▶ RUNNING',
                     title: 'Currently running: ' + (payload.title || '') };
        if (payload.is_installed)
            return { kind: 'installed', label: 'DOWNLOADED',
                     bannerLabel: '✓ DOWNLOADED',
                     title: 'In library, installed: ' + (payload.title || '') };
        return { kind: 'library', label: 'IN LIBRARY',
                 bannerLabel: '+ IN LIBRARY',
                 title: 'In library, not installed: ' + (payload.title || '') };
    }

    function formatAge(ms) {
        const s = Math.floor(ms / 1000);
        if (s < 60) return s + 's';
        const m = Math.floor(s / 60);
        if (m < 60) return m + 'm';
        const h = Math.floor(m / 60);
        if (h < 48) return h + 'h';
        return Math.floor(h / 24) + 'd';
    }

    function buildPill(desc, payload, cached) {
        const pill = document.createElement('span');
        pill.className = 'dlib-pill dlib-pill-' + desc.kind
            + (cached ? ' dlib-pill-cached' : '');
        pill.textContent = desc.label;
        pill.dataset.kind = desc.kind;
        pill.title = cached
            ? desc.title + ' (cached ' + formatAge(Date.now() - payload._cached_at) + ' ago, DLib offline)'
            : desc.title;
        if (payload.library_url) {
            const fullUrl = 'http://127.0.0.1:8000' + payload.library_url;
            pill.style.cursor = 'pointer';
            pill.addEventListener('click', (e) => {
                e.preventDefault(); e.stopPropagation();
                window.open(fullUrl, '_blank', 'noopener');
            }, true);
        }
        return pill;
    }

    // ----- card-banner (listing rows) --------------------------------------

    // Outer-container-first ordering: the banner needs a block-level parent
    // it can sit at the top of. Inner flex wrappers (.contentRow) come last
    // because they squash the banner into a sidebar shape.
    const CARD_SELECTORS = [
        'li.block-row',                 // f95 search results / sub-forum lists OUTER
        'li.js-inlineModContainer',
        '.structItem',                  // f95 forum latest / forum index
        'tr.search_result_img_box',     // dlsite list row
        'li.search_result_img_box',
        '.work_1col',
        '.work_text_1col',
        '.block-row',
        '[class*="card"]', '[class*="Card"]',
        'article',
        '.contentRow',                  // inner flex — last-resort fallback
        'li',
    ];

    function findCardContainer(link) {
        for (const sel of CARD_SELECTORS) {
            const hit = link.closest(sel);
            if (hit && hit !== document.body && hit.contains(link)) return hit;
        }
        return link.parentElement || link;
    }

    function setCardState(link, desc) {
        const card = findCardContainer(link);
        if (!card) return;
        ['installed','library','running','bad'].forEach(k =>
            card.classList.remove('dlib-bad-card', 'dlib-card-' + k));
        card.querySelectorAll(':scope > .dlib-card-banner').forEach(b => b.remove());

        if (!desc) return;

        card.classList.add('dlib-card-' + desc.kind);
        if (desc.kind === 'bad') card.classList.add('dlib-bad-card');

        const banner = document.createElement('div');
        banner.className = 'dlib-card-banner dlib-card-banner-' + desc.kind;
        banner.textContent = desc.bannerLabel;
        card.insertBefore(banner, card.firstChild);
    }

    function clearPillOnLink(link) {
        const pill = link.querySelector(':scope > .dlib-pill');
        if (pill) pill.remove();
        link.dataset.dlibPilled = '';
    }

    // ----- pill placement on an IMAGE (thread page cover) ------------------

    function findOpCoverImage() {
        // First post = OP. Class names differ slightly across XF themes.
        const op = document.querySelector('article.message')
                 || document.querySelector('.message');
        if (!op) return null;
        const body = op.querySelector('.bbWrapper, .message-body, .messageContent');
        if (!body) return null;
        let best = null;
        let bestArea = 0;
        for (const img of body.querySelectorAll('img')) {
            // Skip our own pills' nodes etc.
            if (img.closest('.dlib-pill')) continue;
            const rect = img.getBoundingClientRect();
            if (rect.width < 120 || rect.height < 80) continue;
            const area = rect.width * rect.height;
            if (area > bestArea) {
                bestArea = area;
                best = img;
            }
        }
        return best;
    }

    function findDlsiteProductImage() {
        // DLsite product page main image. Different markup than F95.
        const candidates = [
            'img.target_type',
            'img.work_slider_img',
            '.product-slider img',
            '#work_left img',
        ];
        for (const sel of candidates) {
            const img = document.querySelector(sel);
            if (img) return img;
        }
        // Fallback: biggest visible image in the main content area
        const main = document.querySelector('#main_inner, .work_outline_area, body');
        if (!main) return null;
        let best = null;
        let bestArea = 0;
        for (const img of main.querySelectorAll('img')) {
            const rect = img.getBoundingClientRect();
            if (rect.width < 200) continue;
            const area = rect.width * rect.height;
            if (area > bestArea) { bestArea = area; best = img; }
        }
        return best;
    }

    let autoLinkAttempted = false;
    async function maybeAutoLinkF95ToDlsite() {
        if (autoLinkAttempted) return;
        autoLinkAttempted = true;

        const op = document.querySelector('article.message');
        if (!op) return;

        const dlsiteAnchors = Array.from(
            op.querySelectorAll('a[href*="/product_id/"]')
        );
        if (!dlsiteAnchors.length) return;

        // Build distinct items list for the DLsite links.
        const seen = new Set();
        const items = [];
        for (const a of dlsiteAnchors) {
            const cls = classify(a.href);
            if (!cls || cls.source !== 'dlsite') continue;
            const key = cls.source + ':' + cls.id;
            if (seen.has(key)) continue;
            seen.add(key);
            items.push({ url: a.href, source: cls.source, id: cls.id });
        }
        if (!items.length) return;

        // Ask the SW to look them up (uses cache + API).
        const lookup = await sendLookup(items);
        const lookupResults = lookup.results || {};

        // Find the first one that IS in the library.
        const dlsiteMatch = items.find(it => {
            const p = lookupResults[it.url];
            return p && p.found;
        });
        if (!dlsiteMatch) return;

        // Tell DLib to link this F95 thread URL as an alias of that DLsite game.
        await new Promise((resolve) => {
            try {
                chrome.runtime.sendMessage(
                    { type: 'link',
                      primary_url: dlsiteMatch.url,
                      alias_url: location.href },
                    (response) => {
                        if (response && response.ok) {
                            console.log('[DLib] auto-linked F95 thread to DLsite game');
                        }
                        resolve();
                    }
                );
            } catch (e) {
                resolve();
            }
        });

        // Re-scan so the new alias resolves on this page (and the SW caches it).
        scan({ requeryAll: true });
    }

    // ---- Verbose debug logging (filter console with "[DLib]") ----------
    // Lets us trace the exact request/response flow for upsert + scan so
    // we can see whether a flag like is_bad actually persisted server-side
    // or got dropped somewhere in the extension/server pipeline.
    function dlog(...args) {
        try { console.log('[DLib]', ...args); } catch (_) {}
    }

    function upsertViaSw(url, changes) {
        const reqId = Math.random().toString(36).slice(2, 8);
        dlog('upsert→SW', reqId, { url, changes });
        return new Promise((resolve) => {
            try {
                chrome.runtime.sendMessage(
                    { type: 'upsert', url, changes },
                    (response) => {
                        if (chrome.runtime.lastError) {
                            dlog('upsert←SW LASTERROR', reqId,
                                 chrome.runtime.lastError.message);
                            resolve({ ok: false, error: chrome.runtime.lastError.message });
                            return;
                        }
                        const summary = response && response.payload
                            ? { ok: response.ok,
                                found: response.payload.found,
                                is_bad: response.payload.is_bad,
                                is_favorite: response.payload.is_favorite,
                                created: response.payload.created,
                                source: response.payload.source,
                                source_id: response.payload.source_id }
                            : response;
                        dlog('upsert←SW', reqId, summary);
                        resolve(response || { ok: false });
                    }
                );
            } catch (e) {
                dlog('upsert→SW THREW', reqId, String(e));
                resolve({ ok: false, error: String(e) });
            }
        });
    }

    // -- Action queue ------------------------------------------------------
    // Every API call from a button goes through this queue so rapid clicks
    // serialize cleanly. The visual feedback (spinner, toast) is INSTANT —
    // only the actual fetch is queued, so the user always sees their click
    // register even if previous actions are still in flight.
    //
    // _actionInFlight is also used to LOCK toolbar rebuilds. DLsite metadata
    // enrichment can take 30-60s, during which the server's intermediate
    // game.save() may make the row visible to scans. Without this lock, the
    // toolbar would rebuild to "in-library" state mid-click, destroying the
    // user's in-progress button and tricking them into clicking the new one.
    const _actionQueue = [];
    let _actionRunning = false;
    let _actionInFlight = 0;
    function enqueueAction(handler) {
        return new Promise((resolve) => {
            _actionQueue.push({ handler, resolve });
            _drainActionQueue();
        });
    }
    async function _drainActionQueue() {
        if (_actionRunning) return;
        _actionRunning = true;
        try {
            while (_actionQueue.length) {
                const { handler, resolve } = _actionQueue.shift();
                _actionInFlight++;
                let r;
                try { r = await handler(); }
                catch (e) { r = { ok: false, error: String(e) }; }
                finally { _actionInFlight--; }
                resolve(r);
            }
        } finally {
            _actionRunning = false;
        }
    }

    function showToast(message, kind) {
        let host = document.getElementById('dlib-toast-host');
        if (!host) {
            host = document.createElement('div');
            host.id = 'dlib-toast-host';
            document.documentElement.appendChild(host);
        }
        const toast = document.createElement('div');
        toast.className = 'dlib-toast dlib-toast-' + (kind || 'info');
        toast.textContent = message;
        host.appendChild(toast);
        // Force layout then add 'show' so the transition runs.
        // eslint-disable-next-line no-unused-expressions
        toast.offsetWidth;
        toast.classList.add('show');
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 2400);
    }

    function makeActionBtn(label, kind, onClick, opts) {
        opts = opts || {};
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'dlib-action-btn' + (kind ? ' dlib-action-btn-' + kind : '');
        btn.textContent = label;
        btn.addEventListener('click', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            const originalText = btn.textContent;

            // INSTANT visual feedback — runs before any await.
            btn.disabled = true;
            btn.classList.add('dlib-action-btn-loading');
            btn.textContent = opts.workingText || 'Working';
            if (opts.toastWorking) showToast(opts.toastWorking, 'info');

            try {
                const result = await enqueueAction(onClick);
                if (result && result.ok === false) {
                    const errMsg = result.error || 'request failed';
                    btn.classList.remove('dlib-action-btn-loading');
                    btn.classList.add('dlib-action-btn-failed');
                    btn.textContent = '✕ Failed';
                    btn.title = errMsg;
                    showToast(errMsg, 'error');
                    setTimeout(() => {
                        btn.classList.remove('dlib-action-btn-failed');
                        btn.textContent = originalText;
                        btn.disabled = false;
                        btn.title = '';
                    }, 2200);
                } else {
                    btn.classList.remove('dlib-action-btn-loading');
                    btn.classList.add('dlib-action-btn-ok');
                    btn.textContent = '✓ Done';
                    if (opts.toastSuccess) showToast(opts.toastSuccess, 'success');
                    // Re-scan so banners + pill state refresh from the SW cache.
                    // The button itself will be replaced by the re-rendered toolbar.
                    scan({ requeryAll: true });
                    // Defensive: if scan didn't replace the button (e.g. server
                    // didn't change state), restore after a beat.
                    setTimeout(() => {
                        if (btn.isConnected) {
                            btn.classList.remove('dlib-action-btn-ok');
                            btn.textContent = originalText;
                            btn.disabled = false;
                        }
                    }, 1200);
                }
            } catch (err) {
                btn.classList.remove('dlib-action-btn-loading');
                btn.classList.add('dlib-action-btn-failed');
                btn.textContent = '✕ Failed';
                btn.title = String(err);
                showToast(String(err), 'error');
                setTimeout(() => {
                    btn.classList.remove('dlib-action-btn-failed');
                    btn.textContent = originalText;
                    btn.disabled = false;
                    btn.title = '';
                }, 2200);
            }
        }, true);
        return btn;
    }

    function _toolbarSig(payload) {
        if (!payload || !payload.found) return 'not-in-library';
        const bits = ['in-library'];
        if (payload.is_bad) bits.push('bad');
        if (payload.is_favorite) bits.push('fav');
        return bits.join('|');
    }

    // Add the game first (no flags) then, only after the SW confirms the row
    // exists, send a follow-up upsert with the requested flag. This is the
    // belt-and-braces approach: even if the server's atomic add+mark path
    // ever regresses, the extension still sees the game land in the library
    // before flipping the flag. Both calls share the same queue slot so
    // rapid clicks across buttons still serialize cleanly.
    async function addThenFlag(url, flagChanges, flagLabel) {
        dlog('addThenFlag START', { url, flagChanges, flagLabel });
        const addResp = await upsertViaSw(url, {});
        dlog('addThenFlag step1 done', {
            ok: addResp && addResp.ok,
            payload: addResp && addResp.payload,
        });
        if (!addResp || addResp.ok === false) return addResp;
        const payload = addResp.payload || {};
        if (!payload.found || !payload.source) {
            dlog('addThenFlag ABORT — add returned no game payload');
            return { ok: false, error: 'add returned no game payload' };
        }
        // Server already added — now apply the flag in a follow-up upsert.
        // We re-send `url` (not source/source_id) because that's what the
        // API expects; the server's _resolve_game_by_source will hit the
        // row we just created, skip auto-add, and apply the flag.
        const flagResp = await upsertViaSw(url, flagChanges);
        dlog('addThenFlag step2 done', {
            ok: flagResp && flagResp.ok,
            payload: flagResp && flagResp.payload,
            requested_flag: flagChanges,
        });

        // Sanity check: did the server payload actually reflect the flag we
        // sent? If not, the flag was silently dropped server-side and the
        // user would be stuck clicking again to "re-apply" it.
        if (flagResp && flagResp.ok && flagResp.payload) {
            for (const k of Object.keys(flagChanges)) {
                if (flagResp.payload[k] !== flagChanges[k]) {
                    dlog('addThenFlag MISMATCH! server payload does not reflect requested flag',
                         { key: k, requested: flagChanges[k], got: flagResp.payload[k] });
                    return {
                        ok: false,
                        error: 'server did not persist ' + k + '=' + flagChanges[k]
                               + ' (got ' + flagResp.payload[k] + ')',
                    };
                }
            }
        }
        if (!flagResp || flagResp.ok === false) {
            const errMsg = (flagResp && flagResp.error) || 'unknown error';
            return {
                ok: false,
                error: 'added to library but ' + flagLabel + ' failed: ' + errMsg,
            };
        }
        return flagResp;
    }

    function _buildToolbarButtons(host, payload) {
        const url = location.href;
        const inLibrary = !!(payload && payload.found);
        if (!inLibrary) {
            // DLsite metadata enrichment can take 30-60s for new adds because
            // the server downloads sample images, fetches the work data, etc.
            // Be honest about the wait so the user doesn't think it's stuck
            // and start clicking again.
            host.appendChild(makeActionBtn('+ Add to library', 'add',
                () => upsertViaSw(url, {}),
                { workingText: 'Adding (may take 30-60s)…',
                  toastWorking: 'Adding to library — fetching metadata, please wait…',
                  toastSuccess: '✓ Added to library' }));
            host.appendChild(makeActionBtn('★ Mark favorite', 'favorite',
                () => addThenFlag(url, { is_favorite: true }, 'mark favorite'),
                { workingText: 'Adding + marking (30-60s)…',
                  toastWorking: 'Adding to library, then marking as favorite (~30-60s)…',
                  toastSuccess: '★ Added & marked as favorite' }));
            host.appendChild(makeActionBtn('⚠ Mark as bad', 'danger',
                () => addThenFlag(url, { is_bad: true }, 'mark as bad'),
                { workingText: 'Adding + marking (30-60s)…',
                  toastWorking: 'Adding to library, then marking as bad (~30-60s)…',
                  toastSuccess: '⚠ Added & marked as bad' }));
        } else {
            if (payload.is_favorite) {
                host.appendChild(makeActionBtn('★ Unfavorite', '',
                    () => upsertViaSw(url, { is_favorite: false }),
                    { workingText: 'Saving…',
                      toastSuccess: 'Removed from favorites' }));
            } else {
                host.appendChild(makeActionBtn('★ Mark favorite', 'favorite',
                    () => upsertViaSw(url, { is_favorite: true }),
                    { workingText: 'Saving…',
                      toastSuccess: '★ Marked as favorite' }));
            }
            if (payload.is_bad) {
                host.appendChild(makeActionBtn('✓ Unmark bad', '',
                    () => upsertViaSw(url, { is_bad: false }),
                    { workingText: 'Saving…',
                      toastSuccess: 'Bad mark cleared' }));
            } else {
                host.appendChild(makeActionBtn('⚠ Mark as bad', 'danger',
                    () => upsertViaSw(url, { is_bad: true }),
                    { workingText: 'Saving…',
                      toastSuccess: '⚠ Marked as bad' }));
            }
        }
    }

    function findOpInlineHost() {
        const lower = location.href.toLowerCase();
        if (lower.includes('f95zone.to')) {
            const op = document.querySelector('article.message');
            if (!op) return null;
            return op.querySelector(
                '.bbWrapper, .messageContent, .message-userContent'
            );
        }
        if (lower.includes('dlsite.com')) {
            return document.querySelector(
                '#work_outline, .work_outline_area, '
                + '.work_buy_title, #main_inner'
            );
        }
        return null;
    }

    /** Prepend a horizontal toolbar above the OP body so the buttons are
     *  always visible — independent of whether the OP has a cover image
     *  or what its dimensions are. Returns true if it placed/updated the
     *  toolbar, false if no suitable host element was found.
     */
    function placeActionsInline(payload) {
        const host = findOpInlineHost();
        if (!host) {
            dlog('placeActionsInline: no host found');
            return false;
        }

        const sig = _toolbarSig(payload);
        let toolbar = host.querySelector(':scope > .dlib-inline-toolbar');
        if (toolbar && toolbar.dataset.dlibSig === sig) {
            dlog('placeActionsInline: sig unchanged, skipping rebuild', sig);
            return true;
        }

        // Don't rebuild while a user action is in flight. DLsite metadata
        // enrichment can take 30-60s, during which the server's intermediate
        // game.save() can flip the row from not-in-library to in-library —
        // which would change the sig and destroy the "Saving…" button the
        // user is waiting on. The post-action scan() will reconcile state
        // once _actionInFlight drops back to 0.
        if (_actionInFlight > 0 && toolbar) {
            dlog('placeActionsInline: LOCKED — action in flight, skipping rebuild', {
                prev_sig: toolbar.dataset.dlibSig,
                new_sig: sig,
                actions_in_flight: _actionInFlight,
            });
            return true;
        }

        dlog('placeActionsInline: REBUILDING', {
            prev_sig: toolbar ? toolbar.dataset.dlibSig : '(none)',
            new_sig: sig,
        });
        if (!toolbar) {
            toolbar = document.createElement('div');
            toolbar.className = 'dlib-inline-toolbar';
            host.insertBefore(toolbar, host.firstChild);
        }
        toolbar.dataset.dlibSig = sig;
        toolbar.innerHTML = '';
        _buildToolbarButtons(toolbar, payload);
        return true;
    }

    function removeInlineToolbar() {
        document.querySelectorAll('.dlib-inline-toolbar').forEach(t => t.remove());
    }

    function placeActionsOnImage(img, payload) {
        // Page banner is now the primary status indicator, so this toolbar
        // only carries action buttons (no redundant on-image pill).
        if (!img) return;
        let host = img.parentElement;
        if (!host) return;

        if (getComputedStyle(host).position === 'static') {
            host.style.position = 'relative';
        }

        let wrap = host.querySelector(':scope > .dlib-pill-wrap');
        if (!wrap) {
            wrap = document.createElement('div');
            wrap.className = 'dlib-pill-wrap';
            host.appendChild(wrap);
        }

        // Idempotent: if the buttons currently rendered already match the
        // desired state, leave the DOM alone. Prevents the toolbar from
        // being wiped + rebuilt under the user's mouse on every random
        // page mutation — which was eating clicks and causing hover flicker.
        const sig = _toolbarSig(payload);
        if (wrap.dataset.dlibSig === sig) return;
        // Same lock as placeActionsInline — don't destroy the in-progress
        // button while the user's click is still being processed by the
        // server (which can take 30-60s for DLsite metadata enrichment).
        if (_actionInFlight > 0 && wrap.dataset.dlibSig) {
            dlog('placeActionsOnImage: LOCKED — action in flight, skipping rebuild', {
                prev_sig: wrap.dataset.dlibSig,
                new_sig: sig,
                actions_in_flight: _actionInFlight,
            });
            return;
        }
        wrap.dataset.dlibSig = sig;

        wrap.innerHTML = '';
        _buildToolbarButtons(wrap, payload);
    }

    function removeImageToolbar() {
        document.querySelectorAll('.dlib-pill-wrap').forEach(w => w.remove());
    }

    // ----- page-wide BAD treatment (banner + grayscale + link disable) -----

    const HOST_LINK_PATTERNS = [
        'mega.nz', 'mediafire.com', 'workupload', 'gofile.io', 'pixeldrain',
        'anonfile', 'rapidgator', 'dropbox.com', 'drive.google.com',
        'zippyshare', 'dropmefiles', 'fikper', 'multiup', 'krakenfiles',
        'filemonster', 'userscloud', 'ouo.io', 'bunkr', 'send.cm', 'mixdrop',
        'turbobit', 'nitroflare', 'katfile', 'uploadhaven', 'qiwi.gg',
        'buzzheavier', 'datanodes', 'vikingfile',
        '/attachments/', 'cart_add', '/cart/', '_btn_cart',
    ];

    const BANNER_TEXT = {
        bad: 'THIS GAME IS MARKED AS BAD IN YOUR DLIB LIBRARY — DO NOT DOWNLOAD',
        favorite: 'FAVORITE — IN YOUR DLIB LIBRARY',
        installed: 'DOWNLOADED — IN YOUR DLIB LIBRARY',
        running: 'RUNNING NOW',
        library: 'IN YOUR DLIB LIBRARY — NOT YET INSTALLED',
    };
    const BANNER_ICON = {
        bad: '⚠',
        favorite: '★',
        installed: '✓',
        running: '▶',
        library: '+',
    };

    function ensurePageBanner(desc, payload) {
        let banner = document.getElementById('dlib-page-banner');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'dlib-page-banner';
            document.documentElement.appendChild(banner);
        }
        banner.className = 'dlib-page-banner dlib-page-banner-' + desc.kind;
        const title = (payload && payload.title) || '';
        banner.innerHTML = ''
            + '<span class="dlib-page-banner-icon">' + (BANNER_ICON[desc.kind] || '') + '</span>'
            + '<span class="dlib-page-banner-main">' + (BANNER_TEXT[desc.kind] || desc.label) + '</span>'
            + (title ? '<span class="dlib-page-banner-sub"> · ' + title + '</span>' : '')
            + '<button class="dlib-page-banner-close" type="button" title="Dismiss">×</button>';

        if (payload && payload.library_url) {
            const fullUrl = 'http://127.0.0.1:8000' + payload.library_url;
            banner.style.cursor = 'pointer';
            banner.onclick = (e) => {
                if (e.target.classList.contains('dlib-page-banner-close')) return;
                e.preventDefault();
                window.open(fullUrl, '_blank', 'noopener');
            };
        } else {
            banner.style.cursor = '';
            banner.onclick = null;
        }

        banner.querySelector('.dlib-page-banner-close').addEventListener('click', (e) => {
            e.stopPropagation();
            removePageState();
        });
        document.body.classList.add('dlib-page-banner-shown');
    }

    function applyPageState(payload) {
        const desc = (payload && payload.found) ? pillFor(payload) : null;
        if (!desc) {
            removePageState();
            return;
        }
        ensurePageBanner(desc, payload);
        // Only BAD gets the extra page-wide protections (image grayscale +
        // download-link disable). Other states are just informational.
        if (desc.kind === 'bad') applyBadPageExtras();
        else removeBadPageExtras();
    }

    function removePageState() {
        const banner = document.getElementById('dlib-page-banner');
        if (banner) banner.remove();
        document.body.classList.remove('dlib-page-banner-shown');
        removeBadPageExtras();
    }

    function applyBadPageExtras() {
        document.body.classList.add('dlib-bad-page');
        document.querySelectorAll('a[href]').forEach(a => {
            const h = a.href.toLowerCase();
            if (HOST_LINK_PATTERNS.some(p => h.includes(p))) {
                a.dataset.dlibBadLink = '1';
            }
        });
    }

    function removeBadPageExtras() {
        document.body.classList.remove('dlib-bad-page');
        document.querySelectorAll('[data-dlib-bad-link]').forEach(a =>
            delete a.dataset.dlibBadLink);
    }

    // ----- main scan -------------------------------------------------------

    let pending = false;
    let lastServerUp = true;
    const currentPage = classify(location.href);
    const currentKey = currentPage ? currentPage.source + ':' + currentPage.id : null;

    async function scan(opts) {
        if (pending) {
            dlog('scan SKIPPED — already pending', opts);
            return;
        }
        pending = true;
        dlog('scan START', opts);
        try {
            const items = collectLinks();

            // Group by game id; pick the best link (image-bearing, biggest) per group.
            const byGame = new Map();
            for (const it of items) {
                const key = it.source + ':' + it.id;
                if (currentKey && key === currentKey) continue; // suppress own-game spam
                let group = byGame.get(key);
                if (!group) { group = []; byGame.set(key, group); }
                group.push(it);
            }
            const bestPerGame = [];
            for (const [, group] of byGame) {
                group.sort((a, b) => {
                    // image-bearing first, then larger area
                    if (a.hasImg !== b.hasImg) return a.hasImg ? -1 : 1;
                    return (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height);
                });
                const best = group[0];
                for (let i = 1; i < group.length; i++) clearPillOnLink(group[i].link);
                bestPerGame.push(best);
            }

            // Build a lookup list (best links + the current-page game, if any).
            const lookupItems = bestPerGame.slice();
            if (currentPage) {
                lookupItems.push({
                    url: location.href,
                    source: currentPage.source,
                    id: currentPage.id,
                });
            }
            if (!lookupItems.length) return;

            const wantAll = !!(opts && opts.requeryAll);
            const batched = wantAll
                ? lookupItems
                : lookupItems.filter(i => !i.link || !i.link.dataset.dlibPilled);
            if (!batched.length) return;

            const response = await sendLookup(batched.slice(0, MAX_BATCH));
            const results = response.results || {};

            if (response.server_up) {
                lastServerUp = true;
            } else {
                lastServerUp = false;
            }

            // Identify the current page's game (via primary or alias) so we
            // can suppress per-link banners for any link that resolves to it
            // — those are redundant with the page-level banner.
            const currentPayload = currentPage ? results[location.href] : null;
            const currentGameId =
                (currentPayload && currentPayload.found) ? currentPayload.id : null;

            // Listings: card-banner only (no pill). The banner across the top
            // of the row is the indicator; an extra floating pill on the title
            // text/avatar was redundant and overlapped surrounding content.
            for (const it of bestPerGame) {
                clearPillOnLink(it.link);
                const payload = results[it.url];
                if (payload && currentGameId && payload.id === currentGameId) {
                    setCardState(it.link, null); // dedupe vs the page banner
                    continue;
                }
                if (payload) setCardState(it.link, pillFor(payload));
                else setCardState(it.link, null);
            }

            // Handle the current page (game we're viewing).
            if (currentPage) {
                const visiblePayload = currentPayload || { found: false };
                dlog('scan: currentPage payload', {
                    url: location.href,
                    source: currentPage.source,
                    id: currentPage.id,
                    found: visiblePayload.found,
                    is_bad: visiblePayload.is_bad,
                    is_favorite: visiblePayload.is_favorite,
                    sig: _toolbarSig(visiblePayload),
                });

                // Prefer an inline toolbar prepended to the OP body — always
                // visible even when the post has no big cover image. Fall back
                // to an image overlay if no suitable inline host is found.
                const inlinePlaced = placeActionsInline(visiblePayload);
                if (inlinePlaced) {
                    removeImageToolbar();
                } else {
                    const img = currentPage.source === 'dlsite'
                        ? findDlsiteProductImage()
                        : findOpCoverImage();
                    if (img) placeActionsOnImage(img, visiblePayload);
                }
                applyPageState(visiblePayload);

                // F95 cross-link: if we're on a F95 thread page whose OP body
                // contains a DLsite link, and that DLsite game IS in the
                // library but the F95 thread isn't (yet), automatically alias
                // the F95 thread to the DLsite game. Then this thread + any
                // listing rows pointing at it will resolve correctly.
                if (currentPage.source === 'f95zone'
                    && (!currentPayload || !currentPayload.found)) {
                    maybeAutoLinkF95ToDlsite();
                }
            }
        } finally {
            pending = false;
        }
    }

    let scanTimer = null;
    function scheduleScan() {
        clearTimeout(scanTimer);
        scanTimer = setTimeout(() => scan(), SCAN_DEBOUNCE_MS);
    }

    scan();

    // Mutations inside our own injected nodes (toolbars / banners / toasts)
    // shouldn't re-trigger a scan — that would create a feedback loop and
    // wipe the buttons the user is hovering.
    const OWN_SELECTORS = [
        '.dlib-pill-wrap',
        '.dlib-inline-toolbar',
        '.dlib-page-banner',
        '.dlib-card-banner',
        '#dlib-toast-host',
        '.dlib-bad-banner',
    ];
    function _isOwnMutation(target) {
        if (!target || !target.closest) return false;
        for (const sel of OWN_SELECTORS) {
            if (target.closest(sel)) return true;
        }
        return false;
    }
    const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
            if (!_isOwnMutation(m.target)) {
                scheduleScan();
                return;
            }
        }
    });
    obs.observe(document.body, { childList: true, subtree: true });

    setInterval(() => scan({ requeryAll: true }), REQUERY_INTERVAL_MS);

    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) scan({ requeryAll: true });
    });
})();
