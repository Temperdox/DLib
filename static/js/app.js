// Wire CSRF token into every HTMX request.
document.body.addEventListener('htmx:configRequest', function (event) {
    var token = document.querySelector('[name=csrfmiddlewaretoken]');
    if (token) {
        event.detail.headers['X-CSRFToken'] = token.value;
    }
});

// HTMX-friendly helper: open URL in default browser via target=_blank.
window.openDlsite = function (url) {
    if (!url) return;
    window.open(url, '_blank', 'noopener');
};

// Folder/file picker.
//
// When running inside the pywebview desktop window, use its native dialog API
// (window.pywebview.api.pick_folder / pick_file). When opened in a plain
// browser, fall back to the server-side tkinter endpoint.

function _hasPywebview() {
    return !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder);
}

async function _httpPick(path, initial) {
    var token = document.querySelector('[name=csrfmiddlewaretoken]').value;
    var body = new URLSearchParams({initial: initial || ''});
    var resp = await fetch(path, {
        method: 'POST',
        headers: {'X-CSRFToken': token, 'Content-Type': 'application/x-www-form-urlencoded'},
        body: body,
    });
    if (!resp.ok) return '';
    var data = await resp.json();
    return data.path || '';
}

window.pickFolder = async function (initial) {
    if (_hasPywebview()) {
        try { return (await window.pywebview.api.pick_folder(initial || '')) || ''; }
        catch (e) { console.warn('pywebview pick_folder failed', e); }
    }
    return _httpPick('/api/pick-folder/', initial);
};

window.pickFile = async function (initial) {
    if (_hasPywebview()) {
        try { return (await window.pywebview.api.pick_file(initial || '')) || ''; }
        catch (e) { console.warn('pywebview pick_file failed', e); }
    }
    return _httpPick('/api/pick-file/', initial);
};

// Launch an HTML game in a pywebview sub-window. The window IS the play
// session — closing it finalizes the session via JsApi.launch_html_game's
// closed-event handler. Falls back to a warning if not running inside the
// desktop shell (HTML games can't be played from a plain browser tab — we
// rely on the native window's closed event for lifetime tracking).
window.dlibLaunchHtml = async function (gameId) {
    if (!_hasPywebview() || !window.pywebview.api.launch_html_game) {
        alert('HTML games can only be launched from inside the DLib desktop app, '
              + 'not from a plain browser tab. (We use the native window\'s lifetime '
              + 'to track playtime.)');
        return;
    }
    try {
        const resp = await window.pywebview.api.launch_html_game(gameId);
        if (!resp || resp.ok === false) {
            alert('Could not launch: ' + ((resp && resp.error) || 'unknown error'));
            return;
        }
        // Refresh after a beat so the running pill / play-status updates.
        setTimeout(() => { try { window.location.reload(); } catch (_) {} }, 400);
    } catch (e) {
        console.warn('launch_html_game failed', e);
        alert('Launch failed: ' + e);
    }
};
