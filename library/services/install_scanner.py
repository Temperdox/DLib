"""Discover game executables inside an install folder.

Returns a ranked list of candidates so the view can auto-select when there's
a single obvious choice, or present a chooser modal when there are multiple.
"""
from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
LINKED_SUBDIR = 'linked'

# Patterns scored as "almost certainly the game" — higher = stronger
_STRONG_BONUS = {
    'game.exe': 100,
    'rpg_rt.exe': 90,
    'nw.exe': 80,
    'nwjs.exe': 80,
    'wolfrpgeditor.exe': 70,
    'launcher.exe': 50,
}

# Filename substrings that knock candidates out of consideration entirely
_BLACKLIST_SUBSTRINGS = (
    'unins',
    'uninstall',
    'setup',
    'vcredist',
    'vc_redist',
    'dxsetup',
    'directx',
    'crashreport',
    'crashpad',
    'dotnetfx',
    'redist',
    'updater',
    'patch',
    'reginstall',
    'helper',
    'ffmpeg',
)

_EXE_EXT = '.exe' if sys.platform == 'win32' else ''


@dataclass(frozen=True, order=True)
class ExeCandidate:
    score: int
    path: str
    name: str


def _is_blacklisted(name_lower: str) -> bool:
    return any(token in name_lower for token in _BLACKLIST_SUBSTRINGS)


def _score(path: Path, root: Path) -> int:
    name = path.name.lower()
    bonus = _STRONG_BONUS.get(name, 0)
    try:
        depth = len(path.relative_to(root).parts) - 1
    except ValueError:
        depth = 0
    size_bonus = 0
    try:
        size = path.stat().st_size
        if size > 1_000_000:
            size_bonus = 10
        if size > 10_000_000:
            size_bonus = 20
    except OSError:
        pass
    return bonus - depth * 5 + size_bonus


def scan_executables(folder: str | Path) -> list[ExeCandidate]:
    root = Path(folder)
    if not root.is_dir():
        return []

    pattern = '*.exe' if sys.platform == 'win32' else '*'
    candidates: list[ExeCandidate] = []
    for entry in root.rglob(pattern):
        if not entry.is_file():
            continue
        if sys.platform != 'win32':
            mode = entry.stat().st_mode
            if not (mode & 0o111):
                continue
        name_lower = entry.name.lower()
        if sys.platform == 'win32' and not name_lower.endswith('.exe'):
            continue
        if _is_blacklisted(name_lower):
            continue
        candidates.append(ExeCandidate(
            score=_score(entry, root),
            path=str(entry),
            name=entry.name,
        ))
    # Highest score first
    candidates.sort(reverse=True)
    return candidates


def best_single_exe(folder: str | Path) -> str | None:
    """Return path if there is one obvious choice; else None (needs user pick)."""
    cands = scan_executables(folder)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0].path
    top = cands[0]
    # Auto-pick if the top candidate dominates by a clear margin
    if top.score >= 80 and top.score - cands[1].score >= 30:
        return top.path
    return None


def relocate_to_linked(game, install_root: str) -> tuple[bool, str | None]:
    """Move the game's install folder under ``<install_root>/linked/``.

    No-op (returns ``(False, None)``) when:
      * install_root is empty or doesn't exist
      * the game has no install_folder set
      * the source folder is already inside linked/
      * the source folder contains the linked folder (would be recursive)

    On success, updates ``game.install_folder`` and ``game.executable_path``
    in-memory AND saves them. Returns ``(True, new_install_folder)``.
    On failure, logs the exception and returns ``(False, error_message)``.
    """
    if not install_root or not game.install_folder:
        return False, None

    try:
        root = Path(install_root).resolve()
    except (OSError, ValueError) as exc:
        log.warning('invalid install_root %s: %s', install_root, exc)
        return False, str(exc)
    if not root.is_dir():
        return False, None

    try:
        src = Path(game.install_folder).resolve()
    except (OSError, ValueError) as exc:
        return False, str(exc)
    if not src.is_dir():
        return False, None

    linked_root = root / LINKED_SUBDIR

    # Already inside linked/ — nothing to do.
    try:
        src.relative_to(linked_root)
        return False, None
    except ValueError:
        pass

    # Would the move be recursive? (e.g. linked/ lives inside src)
    try:
        linked_root.relative_to(src)
        return False, 'install folder contains the linked target'
    except ValueError:
        pass

    linked_root.mkdir(parents=True, exist_ok=True)
    dst = linked_root / src.name
    n = 2
    while dst.exists():
        dst = linked_root / f'{src.name}_{n}'
        n += 1

    # Remember the exe's path relative to src so we can rewrite it.
    rel_exe: Path | None = None
    if game.executable_path:
        try:
            rel_exe = Path(game.executable_path).resolve().relative_to(src)
        except ValueError:
            rel_exe = None

    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        log.exception('relocate failed: %s -> %s', src, dst)
        return False, str(exc)

    game.install_folder = str(dst)
    if rel_exe is not None:
        game.executable_path = str(dst / rel_exe)
    elif game.executable_path:
        # Fallback: keep just the basename inside the new folder.
        game.executable_path = str(dst / Path(game.executable_path).name)
    game.save(update_fields=['install_folder', 'executable_path'])
    log.info('relocated game %s: %s -> %s', game.pk, src, dst)
    return True, str(dst)
