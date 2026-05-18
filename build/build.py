"""Build DLib.exe with Nuitka.

Produces a standalone folder under build/dist/DLib.dist/ containing
DLib.exe and all its dependencies. AV-friendly: Nuitka compiles to real
machine code (no PyInstaller-style self-extracting bootloader).

Usage:
    python build/build.py            # full build
    python build/build.py --clean    # delete previous output first

Build takes ~10-20 minutes the first time (Nuitka may auto-download its
own clang/MinGW64 if needed). Subsequent rebuilds are faster due to
Nuitka's cache.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Force UTF-8 stdout so accented chars / em-dashes in build args don't crash on cp932 consoles.
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BUILD_DIR / 'dist'
ICON = BUILD_DIR / 'dlib.ico'
ENTRY = PROJECT_ROOT / 'run_app.py'

# Packages Nuitka must fully include (their .py files compiled into the bundle).
# 'webview' is here because we disable the pywebview plugin below — that plugin
# auto-excludes webview.platforms.win32, which winforms tries to import at
# runtime, causing a fatal "excluded module usage". Including the whole package
# manually is more verbose but reliable.
INCLUDE_PACKAGES = [
    'DLib',
    'library',
    'django',
    'dlsite_async',
    'waitress',
    'webview',
    'psutil',
    'PIL',
    'aiohttp',
    'cryptography',
    'asgiref',
    'sqlparse',
    'bs4',
    'lxml',
    'tzdata',  # Django needs zoneinfo on Windows
    'clr_loader',
    'pythonnet',
]

# Non-Python files we need at runtime, copied verbatim. Migrations are .py
# files compiled into the bundle via --include-package=library; Django finds
# them with pkgutil.iter_modules so no extra data copy is needed.
DATA_DIRS = [
    ('library/templates', 'library/templates'),
    ('static', 'static'),
]


def build(clean: bool) -> int:
    if not ICON.exists():
        subprocess.check_call([sys.executable, str(BUILD_DIR / 'make_icon.py')])

    if clean and OUTPUT_DIR.exists():
        print(f'cleaning {OUTPUT_DIR}')
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, '-m', 'nuitka',
        '--standalone',
        '--assume-yes-for-downloads',
        '--windows-console-mode=disable',  # no console window when launched
        f'--windows-icon-from-ico={ICON}',
        '--company-name=DLib',
        '--product-name=DLib',
        '--file-description=DLib - DLsite game library',
        '--product-version=0.1.1',
        '--file-version=0.1.1.0',
        f'--output-dir={OUTPUT_DIR}',
        '--output-filename=DLib.exe',
        '--remove-output',     # delete intermediate build files
        # Skip link-time optimisation. Cuts C-compile time by 30-50% at the
        # cost of a slightly bigger exe — worth it everywhere, critical on
        # GitHub's 4-vCPU windows-latest runners where LTO blows past the
        # 60-min default workflow timeout.
        '--lto=no',
        '--nofollow-import-to=tkinter',  # tkinter pulls a lot of Tcl/Tk; pywebview replaces it
        '--nofollow-import-to=test',
        '--nofollow-import-to=unittest',
        '--module-parameter=django-settings-module=DLib.settings',
        # The pywebview plugin proactively excludes webview.platforms.win32
        # (and related modules) which winforms tries to import at runtime.
        # Disabling the plugin and including the whole webview package
        # manually is more reliable.
        '--disable-plugin=pywebview',
    ]
    for pkg in INCLUDE_PACKAGES:
        cmd.append(f'--include-package={pkg}')
    # package-data: any non-Python files distributed inside these packages
    for pkg in ('django', 'library', 'tzdata'):
        cmd.append(f'--include-package-data={pkg}')
    for src, dst in DATA_DIRS:
        cmd.append(f'--include-data-dir={PROJECT_ROOT / src}={dst}')
    cmd.append(str(ENTRY))

    print('running:')
    print(' ', ' '.join(f'"{c}"' if ' ' in c else c for c in cmd))
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if proc.returncode != 0:
        print(f'\nBuild FAILED (exit {proc.returncode}).')
        return proc.returncode

    exe = OUTPUT_DIR / 'run_app.dist' / 'DLib.exe'
    if not exe.exists():
        # Nuitka names the dist folder after the entry script; rename for clarity.
        old = OUTPUT_DIR / 'run_app.dist'
        new = OUTPUT_DIR / 'DLib.dist'
        if old.exists():
            if new.exists():
                shutil.rmtree(new)
            old.rename(new)
            exe = new / 'DLib.exe'
    print(f'\nBuild OK — {exe}')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--clean', action='store_true',
                        help='wipe build/dist before building')
    args = parser.parse_args()
    sys.exit(build(args.clean))
