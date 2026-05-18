# DLib
Local-only Django desktop app for managing a personal library of
DLsite + F95Zone games. Single-process pywebview launcher, native
folder/file pickers, per-launch playtime tracking, public read API
for a browser extension that overlays library state on f95zone.to
and dlsite.com.
## Features
- **Two sources, one library**: DLsite (via [dlsite-async]) and
  F95Zone (own scraper with embedded login for Cloudflare bypass).
  Same game on both gets linked via aliases so it shows the same
  state across origins.
- **Per-launch playtime tracker**: `psutil.Process.wait()` daemon
  thread per launch, auto-cleans, supports launchers like MTool's
  `StartWithTool.bat` (re-attaches to the real game process after
  the bat exits).
- **Auto-relocate** linked games into
  `<install-root>/linked/<folder-name>` so you can see at a glance
  which folders are already linked.
- **Splash screen** with rotating stats (favorite tag, most-played,
  last played, etc.) on launch.
- **Browser extension** (Chromium + Firefox) overlays
  `DOWNLOADED / IN LIBRARY / RUNNING / FAVORITE / BAD` pills on
  game links, and exposes one-click `Mark favorite / Mark bad /
  Add to library` actions from the source site.
## Quickstart (dev)
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python run_app.py
```
Opens a pywebview desktop window at `http://127.0.0.1:8000`.
Visit **Settings** to:
- Set per-source install roots (DLsite, F95Zone).
- Log in to F95Zone: pops a sub-window for you to complete login;
  cookies + UA are captured for use by the scraper.
## Build a Windows installer
Requires:
- Python 3.13 + the `.venv` from Quickstart
- A C compiler: Visual Studio 2022 Build Tools or Strawberry MinGW
  (Nuitka auto-downloads MinGW64 on first run if neither is found)
- [Inno Setup 6](https://jrsoftware.org/isdl.php) (only for the
  installer step)
```powershell
.\.venv\Scripts\python build\build.py --clean
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\dlib.iss
```
Output: `build\dist\Setup-DLib-0.1.14.exe`. Installs to
`%LOCALAPPDATA%\Programs\DLib`. User data (DB + media) lives in
`%LOCALAPPDATA%\DLib` and is **not** removed by the uninstaller,
so reinstalls preserve the library.
## CI / releases
`.github/workflows/build.yml` runs on push to `main`, on pull
requests, and on tag pushes (`v*`):
- Sets up Python 3.13, installs deps, primes the Nuitka cache.
- Builds the standalone bundle with Nuitka.
- Installs Inno Setup via Chocolatey and compiles the installer.
- Uploads `Setup-DLib-*.exe` and the portable bundle as workflow
  artifacts.
- On a tag push, also attaches the installer to a GitHub Release.
## Browser extension
Files live in `extension/`. See
[`extension/README.md`](extension/README.md) for loading
instructions on Chromium browsers (Opera GX, Chrome, Edge, Brave)
and Firefox 121+.
## Project layout
```
DLib/                  Django project (settings, urls, middleware, paths, splash)
library/               Single Django app: models, views, templates, services
  services/            dlsite_client, f95zone_client, install_scanner,
                       native_dialog, process_tracker
  templates/library/   Templates + partials
  migrations/          Schema history
DLib/paths.py          Frozen-mode detection + user-data-dir resolution
run_app.py             pywebview launcher (waitress + Django + sub-window F95 login)
manage.py              Django CLI
build/                 Nuitka build script + Inno Setup script
extension/             Browser extension (MV3)
.github/workflows/     CI
```
## License
[MIT](LICENSE). Use it, modify it, ship it in your own thing. Just keep
the copyright + license notice. No warranty, no liability.
All third-party dependencies bundled at build time keep their own
licenses (Django/pywebview/psutil/dlsite-async = BSD/MIT, waitress =
ZPL, aiohttp = Apache 2.0, etc.). Running an installer doesn't change
those terms.
[dlsite-async]: https://github.com/bhrevol/dlsite-async
