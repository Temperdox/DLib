"""DLib desktop launcher.

Runs Django (via Waitress) in a background daemon thread and renders the UI
in a pywebview desktop window. Single process, single visible window — no
subprocess spawning, so antivirus heuristics don't flag it.

Closing the window exits the process (the daemon thread dies with it).

Usage:
    python  run_app.py     # normal
    pythonw run_app.py     # no console window (still AV-friendly because
                           # the pywebview window itself is visible)

Or double-click ``dlib.bat``.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DLib.settings')
sys.path.insert(0, str(BASE_DIR))

# Ensure the user data dir exists before Django opens db.sqlite3.
from DLib.paths import ensure_user_dirs, user_data_dir   # noqa: E402
from DLib import splash as splash_mod                    # noqa: E402
_USER_DIR = ensure_user_dirs()

import django  # noqa: E402
django.setup()

from django.contrib.staticfiles.handlers import StaticFilesHandler  # noqa: E402
from django.core.management import call_command   # noqa: E402
from django.core.wsgi import get_wsgi_application  # noqa: E402

import webview  # pywebview                          # noqa: E402
from waitress import serve as waitress_serve         # noqa: E402

HOST = '127.0.0.1'
DEFAULT_PORT = 8000

logging.basicConfig(
    filename=str(_USER_DIR / 'dlib.log'),
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)
log = logging.getLogger('dlib.app')


def pick_port(preferred: int) -> int:
    """Use the preferred port if free, otherwise an OS-assigned one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _default_install_root() -> str:
    """First configured install root, preferring DLsite then F95Zone.

    Used as the default starting directory for native file/folder pickers
    when the caller doesn't pass a hint. Falls back to the same per-source
    default the template uses (``~/DLib/Games/DLsite``) so behavior is
    consistent regardless of which path the picker came from. Works on
    Windows, Linux, and macOS via pathlib.Path.home().
    """
    try:
        from library.models import AppSettings, Game
        s = AppSettings.load()
        for path in (s.dlsite_install_root, s.f95zone_install_root):
            path = (path or '').strip()
            if path and os.path.isdir(path):
                return path
        # Nothing configured — match the template's cross-platform fallback.
        return s.effective_install_root_for(Game.SOURCE_DLSITE)
    except Exception:
        log.exception('default_install_root lookup failed')
        # Absolute last resort: home dir always exists.
        try:
            from pathlib import Path as _Path
            return str(_Path.home())
        except Exception:
            return ''


class JsApi:
    """Exposed to JS as ``window.pywebview.api.<method>``."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None

    def attach(self, window: webview.Window) -> None:
        self._window = window

    @staticmethod
    def _first(result):
        if not result:
            return ''
        if isinstance(result, (list, tuple)):
            return result[0] if result else ''
        return str(result)

    @staticmethod
    def _resolve_initial(initial: str, is_file: bool = False) -> str:
        """Pick the directory pywebview should open in.

        Priority:
          1. ``initial`` if it's a real folder (or the parent dir if a file)
          2. the user's AppSettings.default_install_root
          3. '' (pywebview's last-used directory)
        """
        if initial:
            if os.path.isdir(initial):
                return initial
            if is_file and os.path.isfile(initial):
                return os.path.dirname(initial)
            # Even if the path doesn't exist, an existing parent is a good guess
            parent = os.path.dirname(initial)
            if parent and os.path.isdir(parent):
                return parent
        return _default_install_root()

    def pick_folder(self, initial: str = '') -> str:
        if self._window is None:
            return ''
        directory = self._resolve_initial(initial)
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=directory,
            allow_multiple=False,
        )
        return self._first(result)

    def pick_file(self, initial: str = '') -> str:
        if self._window is None:
            return ''
        directory = self._resolve_initial(initial, is_file=True)
        # HTML is offered as its own filter so users can pick an .html
        # entrypoint for browser-based games (RenPy web build, Twine, etc.).
        file_types = (
            ('Executable (*.exe)', 'HTML game (*.html;*.htm)', 'All files (*.*)')
            if sys.platform == 'win32'
            else ('HTML game (*.html;*.htm)', 'All files (*)')
        )
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=directory,
            allow_multiple=False,
            file_types=file_types,
        )
        return self._first(result)

    def pick_save_path(self, default_name: str = '',
                        file_types: str = '') -> str:
        """Native Save As dialog. ``file_types`` is a semicolon-separated
        list of pywebview file-type descriptors (e.g. ``"DLib export (*.dlib);;All files (*.*)"``);
        empty means all files."""
        if self._window is None:
            return ''
        directory = self._resolve_initial('')
        if not directory:
            directory = os.path.expanduser('~') + os.sep + 'Downloads'
            if not os.path.isdir(directory):
                directory = os.path.expanduser('~')
        kwargs = {'directory': directory, 'save_filename': default_name}
        if file_types:
            kwargs['file_types'] = tuple(file_types.split(';;'))
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            **kwargs,
        )
        return self._first(result)

    # ------------------------------------------------------------------
    # HTML game launcher — opens the HTML file in a pywebview sub-window
    # so the user can play it without leaving DLib, and we get a clean
    # lifecycle signal (the window's `closed` event) to finalize the
    # play session. No process tracking needed — the window IS the session.
    # ------------------------------------------------------------------

    def launch_html_game(self, game_id: int) -> dict:
        log.info('launch_html_game: game_id=%s', game_id)
        if webview is None:
            return {'ok': False, 'error': 'pywebview not loaded'}
        try:
            from library.models import Game
            from library.services import process_tracker
        except Exception as exc:
            log.exception('launch_html_game import failed')
            return {'ok': False, 'error': f'import failed: {exc}'}

        try:
            game = Game.objects.get(pk=int(game_id))
        except Game.DoesNotExist:
            return {'ok': False, 'error': 'game not found'}

        path = (game.executable_path or '').strip()
        if not path:
            return {'ok': False, 'error': 'no executable set for this game'}
        if not os.path.isfile(path):
            return {'ok': False, 'error': f'html file not found: {path}'}
        if not path.lower().endswith(('.html', '.htm')):
            return {'ok': False, 'error': 'executable is not an html file'}

        # Build a file:// URL pathlib-style so spaces / unicode survive.
        try:
            from pathlib import Path as _Path
            file_url = _Path(path).resolve().as_uri()
        except Exception as exc:
            return {'ok': False, 'error': f'could not build file url: {exc}'}

        try:
            session_id, started_at = process_tracker.start_html_session(game)
        except process_tracker.LaunchError as exc:
            return {'ok': False, 'error': str(exc)}
        except Exception as exc:
            log.exception('start_html_session failed')
            return {'ok': False, 'error': f'session start failed: {exc}'}

        try:
            game_window = webview.create_window(
                title=f'{game.title} — DLib',
                url=file_url,
                width=1280,
                height=820,
                min_size=(640, 480),
                background_color='#000000',
                resizable=True,
            )
        except Exception as exc:
            log.exception('create_window failed for html game')
            # Roll back the session if window creation fails.
            try:
                process_tracker.finalize_html_session(game.pk)
            except Exception:
                pass
            return {'ok': False, 'error': f'create_window failed: {exc}'}

        finalized = {'done': False}

        def _on_closed():
            if finalized['done']:
                return
            finalized['done'] = True
            try:
                process_tracker.finalize_html_session(game.pk)
            except Exception:
                log.exception('finalize_html_session failed for game %s', game.pk)

        try:
            game_window.events.closed += _on_closed
        except Exception:
            log.exception('failed to wire html game window closed event')
            # If we can't observe closure we'll leak the session. Roll it back
            # now rather than leaving a dangling open-forever entry.
            _on_closed()
            return {'ok': False, 'error': 'could not attach close handler'}

        log.info('launch_html_game OK: game=%s session=%s url=%s',
                 game.pk, session_id, file_url)
        return {'ok': True, 'session_id': session_id}

    # ------------------------------------------------------------------
    # F95Zone embedded login — captures session cookies + UA so the
    # f95zone_client can punch through Cloudflare.
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_cookies(login_window) -> dict[str, str]:
        """pywebview's get_cookies() returns either SimpleCookie or Cookie objects
        depending on backend; iterate defensively."""
        try:
            raw = login_window.get_cookies() or []
        except Exception:
            log.exception('get_cookies() failed')
            return {}
        out: dict[str, str] = {}
        for entry in raw:
            try:
                if hasattr(entry, 'items'):
                    for name, morsel in entry.items():
                        value = getattr(morsel, 'value', morsel)
                        if name:
                            out[name] = value
                elif hasattr(entry, 'name'):
                    out[entry.name] = getattr(entry, 'value', '')
            except Exception:
                continue
        return out

    def start_f95_login(self) -> dict:
        """Open a sub-window pointed at F95Zone login, wait for cookies.

        On Windows / WebView2 secondary windows can be slow to appear or
        end up behind the main window. We poke at them aggressively (resize,
        restore, evaluate_js ping) to coax them to render, and log every
        step so dlib.log shows what actually happened.
        """
        log.info('start_f95_login: ENTER (main window=%s)', self._window)

        if webview is None:
            log.error('start_f95_login: webview module is None')
            return {'ok': False, 'error': 'pywebview not loaded'}

        try:
            existing_count = len(webview.windows)
        except Exception:
            existing_count = -1
        log.info('start_f95_login: existing window count = %s', existing_count)

        state: dict = {'cookies': None, 'ua': '', 'cancelled': False,
                       'window': None}
        done = threading.Event()

        _CANCEL_JS = (
            "if (!document.getElementById('__dlib_cancel_btn')) {"
            "  var b = document.createElement('button');"
            "  b.id = '__dlib_cancel_btn';"
            "  b.textContent = 'X  Cancel - back to DLib';"
            "  b.style.cssText = 'position:fixed;bottom:18px;right:18px;'"
            "    +'z-index:2147483647;padding:10px 16px;background:#f04848;'"
            "    +'color:#fff;border:none;border-radius:6px;font:bold 14px '"
            "    +'system-ui,sans-serif;cursor:pointer;'"
            "    +'box-shadow:0 4px 14px rgba(0,0,0,0.55);';"
            "  b.onclick = function() {"
            "    window.__dlib_cancel_clicked = true;"
            "    document.title = '__DLIB_CANCEL__';"
            "  };"
            "  (document.body || document.documentElement).appendChild(b);"
            "}"
        )

        def on_loaded():
            win = state['window']
            if win is None:
                return
            try:
                current = win.get_current_url() or ''
            except Exception:
                current = ''
            try:
                title = win.get_current_url and (
                    win.evaluate_js('document.title') or ''
                )
            except Exception:
                title = ''
            log.info('f95 login (sub): loaded %s (title=%s)',
                     current[:140], (title or '')[:80])

            if title == '__DLIB_CANCEL__':
                log.info('f95 login (sub): user clicked Cancel')
                state['cancelled'] = True
                done.set()
                return

            lower = current.lower()
            if 'f95zone.to' not in lower:
                return

            # Inject rescue Cancel button on every F95 page load.
            try:
                win.evaluate_js(_CANCEL_JS)
            except Exception:
                log.exception('inject cancel js failed')

            if any(seg in lower for seg in ('/login', '/lost-password',
                                            '/register', '/two-step',
                                            '/captcha')):
                return
            cookies = self._collect_cookies(win)
            log.info('f95 login (sub): saw cookies: %s', list(cookies.keys()))
            if not cookies.get('xf_user'):
                return
            try:
                ua = win.evaluate_js('navigator.userAgent') or ''
            except Exception:
                ua = ''
            state['cookies'] = cookies
            state['ua'] = ua
            log.info('f95 login (sub): CAPTURED %d cookies', len(cookies))
            done.set()

        def on_closed():
            log.info('f95 login (sub): window closed')
            if state['cookies'] is None:
                state['cancelled'] = True
            done.set()

        # Create the sub-window. webview.create_window IS callable from any
        # thread on pywebview 4+ — but the window might appear behind, so
        # we explicitly restore/resize after a short delay.
        try:
            login_window = webview.create_window(
                title='Log in to F95Zone — DLib',
                url='https://f95zone.to/login/',
                width=1000,
                height=760,
                min_size=(800, 600),
                background_color='#15171b',
                resizable=True,
            )
        except Exception as exc:
            log.exception('create_window failed')
            return {'ok': False, 'error': f'create_window failed: {exc}'}

        state['window'] = login_window
        log.info('start_f95_login: created sub-window (now %d windows)',
                 len(webview.windows))

        try:
            login_window.events.loaded += on_loaded
            login_window.events.closed += on_closed
        except Exception:
            log.exception('failed to wire sub-window events')

        # Coax the window to the foreground after a beat.
        def _coax():
            time.sleep(0.6)
            for action in (
                lambda: login_window.restore(),
                lambda: login_window.resize(1000, 760),
            ):
                try:
                    action()
                except Exception:
                    pass
            try:
                ping = login_window.evaluate_js('1')
                log.info('start_f95_login: sub-window ping = %r', ping)
            except Exception:
                log.exception('start_f95_login: sub-window ping failed')

        threading.Thread(target=_coax, daemon=True, name='f95-coax').start()

        finished = done.wait(timeout=600)

        try:
            login_window.events.loaded -= on_loaded
        except Exception:
            pass
        try:
            login_window.events.closed -= on_closed
        except Exception:
            pass

        def _destroy():
            try:
                login_window.destroy()
            except Exception:
                log.exception('destroy login window failed')

        if not finished:
            _destroy()
            return {'ok': False, 'error': 'Login timed out after 10 minutes.'}

        if state['cancelled']:
            _destroy()
            return {'ok': False, 'error': 'Login was cancelled.'}

        cookies = state['cookies'] or {}
        if not cookies:
            _destroy()
            return {'ok': False, 'error': 'Could not read cookies after login.'}

        try:
            from library.models import AppSettings
            s = AppSettings.load()
            s.f95zone_cookies = cookies
            s.f95zone_user_agent = state['ua'] or ''
            s.save()
        except Exception as exc:
            log.exception('failed to save f95 session')
            _destroy()
            return {'ok': False, 'error': f'Failed to save session: {exc}'}

        _destroy()

        key_cookies = sorted(
            k for k in ('xf_user', 'xf_session', 'cf_clearance', 'cf_bm', '__cf_bm')
            if k in cookies
        )
        return {
            'ok': True,
            'cookie_count': len(cookies),
            'key_cookies': key_cookies,
        }

    def f95_login_status(self) -> dict:
        try:
            from library.models import AppSettings
            s = AppSettings.load()
            cookies = s.f95zone_cookies or {}
            return {
                'logged_in': bool(cookies.get('xf_user')),
                'cookie_count': len(cookies),
                'has_cf_clearance': bool(cookies.get('cf_clearance')),
                'ua': (s.f95zone_user_agent or '')[:80],
            }
        except Exception as exc:
            return {'logged_in': False, 'error': str(exc)}

    def clear_f95_login(self) -> dict:
        try:
            from library.models import AppSettings
            s = AppSettings.load()
            s.f95zone_cookies = {}
            s.f95zone_user_agent = ''
            s.save()
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}
        return {'ok': True}


def _serve(app, host: str, port: int) -> None:
    log.info('serving Django on %s:%s', host, port)
    try:
        waitress_serve(app, host=host, port=port, threads=8, _quiet=True)
    except Exception:
        log.exception('waitress crashed')


def _wait_for(url: str, timeout: float = 10.0) -> bool:
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.8):
                return True
        except Exception:
            time.sleep(0.15)
    return False


def _boot(window: 'webview.Window', port: int, url: str) -> None:
    """Run migrations, start waitress, then swap the splash for the library."""
    started = time.monotonic()
    try:
        call_command('migrate', verbosity=0, interactive=False)
    except Exception:
        log.exception('migration failed')

    app = StaticFilesHandler(get_wsgi_application())
    threading.Thread(target=_serve, args=(app, HOST, port),
                     name='dlib-waitress', daemon=True).start()

    if not _wait_for(url):
        log.error('Django did not respond within timeout')
        return

    # Guarantee the splash is on screen long enough to actually read it.
    min_splash = 1.4
    elapsed = time.monotonic() - started
    if elapsed < min_splash:
        time.sleep(min_splash - elapsed)

    try:
        window.load_url(url)
    except Exception:
        log.exception('failed to navigate window to library')


def main() -> int:
    port = pick_port(DEFAULT_PORT)
    url = f'http://{HOST}:{port}/'

    # Compute stats from the existing sqlite (if any) so the splash has
    # something interesting to show immediately — no Django needed yet.
    db_path = _USER_DIR / 'db.sqlite3'
    try:
        stats = splash_mod.compute_stats(db_path)
    except Exception:
        log.exception('splash stat computation failed')
        stats = ['Loading…']
    splash_html = splash_mod.render_splash(stats)

    api = JsApi()
    window = webview.create_window(
        title='DLib',
        html=splash_html,
        js_api=api,
        width=1280,
        height=820,
        min_size=(880, 600),
        background_color='#15171b',
    )
    api.attach(window)

    # Boot Django in the background so the splash is visible immediately.
    threading.Thread(target=_boot, args=(window, port, url),
                     name='dlib-boot', daemon=True).start()

    log.info('showing splash; Django boot in progress')
    webview.start()
    log.info('window closed, shutting down')
    return 0


if __name__ == '__main__':
    sys.exit(main())
