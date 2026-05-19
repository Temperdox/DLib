"""Auto-discover newly-downloaded games + verify existing install paths.

Runs on every library page load. For each non-bad game:

  * If ``executable_path`` points at a file that no longer exists,
    clear it so the card flips to NOT DOWNLOADED.
  * If ``install_folder`` is unset, search the source's configured
    install root for a folder whose name matches the game
    (DLsite: by product id, F95Zone: by title-token overlap). On a
    confident match, set ``install_folder``.
  * If ``install_folder`` is known but ``executable_path`` is empty
    (e.g. the user just extracted the archive into a folder we
    already linked), re-scan the folder; auto-pick the executable
    if there's a single obvious candidate.

Bad games are skipped — the user already decided they're not
downloading those.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from library.models import AppSettings, Game
from library.services import install_scanner

log = logging.getLogger(__name__)

# Tokens too generic to be useful for title matching — they appear in
# half the F95 titles and would inflate the overlap score.
_STOP_TOKENS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from',
    'has', 'have', 'in', 'is', 'it', 'its', 'of', 'on', 'or', 'that',
    'the', 'this', 'to', 'was', 'were', 'will', 'with', 'vs', 'v', 'vol',
    'volume', 'ver', 'version', 'final', 'fin', 'game', 'eng',
})

_TOKEN_RE = re.compile(r'[a-z0-9]+')

# F95 match needs at least this fraction of the title's significant tokens
# to appear in the folder name. 0.7 = 7/10 — strict enough to avoid two
# unrelated titles colliding when they share a generic word or two.
_F95_TOKEN_THRESHOLD = 0.7
_F95_MIN_TITLE_TOKENS = 3


def _tokenize(text: str) -> set[str]:
    """Lowercase, split into alphanumeric runs, drop stop words + tiny
    tokens. Numeric-only tokens are dropped too — "v1", "2024", chapter
    numbers etc. add noise without identity."""
    return {
        t for t in _TOKEN_RE.findall((text or '').lower())
        if len(t) >= 3 and t not in _STOP_TOKENS and not t.isdigit()
    }


def _list_subdirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        return [
            p for p in root.iterdir()
            if p.is_dir() and p.name != install_scanner.LINKED_SUBDIR
        ]
    except OSError:
        return []


def _match_dlsite(game: Game, subdirs: list[Path]) -> Path | None:
    """DLsite folders almost always contain the product id (RJ123456)."""
    needle = (game.source_id or '').upper()
    if not needle:
        return None
    matches = [p for p in subdirs if needle in p.name.upper()]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Multiple hits — prefer the shortest name (less chance of being a
    # parent folder that happens to mention the id in passing).
    return min(matches, key=lambda p: len(p.name))


def _match_f95(game: Game, folder_tokens: list[tuple[Path, set[str]]]) -> Path | None:
    """F95 folders are title-based — match by token overlap."""
    title_tokens = _tokenize(game.title or '')
    if len(title_tokens) < _F95_MIN_TITLE_TOKENS:
        return None
    best: tuple[float, Path] | None = None
    for sub, tokens in folder_tokens:
        if not tokens:
            continue
        overlap = len(title_tokens & tokens)
        score = overlap / len(title_tokens)
        if score >= _F95_TOKEN_THRESHOLD and (best is None or score > best[0]):
            best = (score, sub)
    return best[1] if best else None


def _executable_still_exists(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def refresh_all() -> dict:
    """Sweep all non-bad games. Returns a stats dict for logging."""
    settings_obj = AppSettings.load()
    roots = {
        Game.SOURCE_DLSITE: Path(settings_obj.effective_install_root_for(Game.SOURCE_DLSITE)),
        Game.SOURCE_F95ZONE: Path(settings_obj.effective_install_root_for(Game.SOURCE_F95ZONE)),
    }
    subdirs_by_source = {src: _list_subdirs(root) for src, root in roots.items()}
    # Tokenize F95 folder names once — token sets get re-used across games.
    f95_folder_tokens = [(p, _tokenize(p.name)) for p in subdirs_by_source[Game.SOURCE_F95ZONE]]

    stats = {'verified_cleared': 0, 'folder_linked': 0, 'exe_picked': 0}

    for game in Game.objects.filter(is_bad=False):
        dirty: list[str] = []

        # Verify: stale executable_path → clear it.
        if game.executable_path and not _executable_still_exists(game.executable_path):
            game.executable_path = ''
            dirty.append('executable_path')
            stats['verified_cleared'] += 1

        # Verify: stale install_folder → clear it so re-discovery can run.
        if game.install_folder:
            try:
                if not Path(game.install_folder).is_dir():
                    game.install_folder = ''
                    dirty.append('install_folder')
            except OSError:
                game.install_folder = ''
                dirty.append('install_folder')

        # Discover: no install_folder yet → try to match one in the root.
        if not game.install_folder:
            if game.source == Game.SOURCE_F95ZONE:
                match = _match_f95(game, f95_folder_tokens)
            else:
                match = _match_dlsite(game, subdirs_by_source.get(game.source, []))
            if match is not None:
                game.install_folder = str(match)
                dirty.append('install_folder')
                stats['folder_linked'] += 1

        # Pick: install_folder known but no exe → re-scan in case it
        # was just extracted.
        if game.install_folder and not game.executable_path:
            try:
                auto = install_scanner.best_single_exe(game.install_folder)
            except Exception:
                log.exception('auto-pick exe failed for game %s', game.pk)
                auto = None
            if auto:
                game.executable_path = auto
                if 'executable_path' not in dirty:
                    dirty.append('executable_path')
                stats['exe_picked'] += 1

        if dirty:
            # Bump updated_at so the library page poller notices.
            dirty.append('updated_at')
            try:
                game.save(update_fields=dirty)
            except Exception:
                log.exception('auto-discover save failed for game %s', game.pk)

    if any(stats.values()):
        log.info('auto-discover: %s', stats)
    return stats
