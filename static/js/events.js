// Server-Sent Events bridge.
//
// Every page opens a single EventSource to /api/events/ at load time and
// re-emits incoming server events as DOM events that page-level code can
// listen for. EventSource reconnects automatically on dropped connections
// (browser default: ~3s) so transient hiccups don't require a reload.
//
// Server event types this client knows about:
//   game.added    — new game appeared (extension upsert, manual add, import)
//   game.changed  — any field on a game changed (favorite, bad, install,
//                   metadata refresh, alias added, tag toggled, etc.)
//   game.deleted  — game removed from the library
//   game.running  — a play session started
//   game.stopped  — a play session ended (time_played_seconds is updated)
//   tags.changed  — tag rename / delete
//
// Each is re-broadcast as a CustomEvent on `document` with the prefix
// "dlib:" — e.g. "dlib:game.changed" — and the server payload in
// event.detail. Listeners decide what to do (refresh a partial, reload
// the page, etc.) based on their own context.
(function () {
    if (window.dlibEventSource) return;  // idempotent — base template is included once but be safe

    var DLIB_EVENTS = [
        'game.added',
        'game.changed',
        'game.deleted',
        'game.running',
        'game.stopped',
        'tags.changed',
    ];

    function open() {
        var es;
        try {
            es = new EventSource('/api/events/');
        } catch (err) {
            console.warn('[dlib] EventSource construction failed', err);
            return null;
        }

        DLIB_EVENTS.forEach(function (name) {
            es.addEventListener(name, function (evt) {
                var detail = {};
                try { detail = JSON.parse(evt.data || '{}'); }
                catch (_) { /* keep empty */ }
                document.dispatchEvent(new CustomEvent('dlib:' + name, { detail: detail }));
            });
        });

        es.addEventListener('hello', function () {
            // Page-specific code can hook this to trigger a one-time
            // sync on (re)connect. Important after a network blip — the
            // page may have missed events while the EventSource was
            // backing off. Listeners attach with document.addEventListener
            // ('dlib:reconnect', ...).
            document.dispatchEvent(new CustomEvent('dlib:reconnect'));
        });

        es.onerror = function (e) {
            // EventSource auto-reconnects. We just log so the dev console
            // shows what's happening; no need to manually re-create it.
            if (es.readyState === EventSource.CLOSED) {
                console.warn('[dlib] EventSource closed; browser will retry');
            }
        };

        return es;
    }

    window.dlibEventSource = open();

    // If the page is being unloaded, close the stream so we don't keep
    // a worker thread on the server pinned to a phantom client.
    window.addEventListener('pagehide', function () {
        if (window.dlibEventSource) {
            try { window.dlibEventSource.close(); } catch (_) {}
            window.dlibEventSource = null;
        }
    });
})();
