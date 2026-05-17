"""Path helpers used by settings.py and the launcher.

Centralizes the "are we running as a packaged exe?" check and the user-data
directory location so settings.py can stay declarative.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running as a Nuitka/PyInstaller bundle (not python.exe)."""
    if getattr(sys, 'frozen', False):  # PyInstaller / py2exe
        return True
    if '__compiled__' in globals():     # Nuitka sets this on modules it compiled
        return True
    # Nuitka standalone: sys.executable is DLib.exe, not python.exe
    exe = Path(sys.executable).name.lower()
    return exe not in ('python.exe', 'pythonw.exe', 'python', 'pythonw', 'py.exe')


def user_data_dir() -> Path:
    """Per-user writable directory for db.sqlite3, media/, etc.

    Frozen: %LOCALAPPDATA%\\DLib  (Windows) or ~/.local/share/DLib (POSIX).
    Dev:    the project root, so dev runs don't pollute AppData.
    """
    if not is_frozen():
        return Path(__file__).resolve().parent.parent

    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return Path(base) / 'DLib'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'DLib'
    return Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share')) / 'DLib'


def install_dir() -> Path:
    """Where the bundle (templates, static, code) lives.

    Frozen: the directory containing the exe.
    Dev:    the project root.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ensure_user_dirs() -> Path:
    """Create the user data tree (covers/ etc) if missing. Returns the root."""
    root = user_data_dir()
    (root / 'media' / 'covers').mkdir(parents=True, exist_ok=True)
    return root
