"""Helpers for the manual-add-game flow.

Handles cover + gallery images that come either as remote URLs (which
we fetch server-side) or as uploaded files (Django ``UploadedFile``
instances). Both end up in ``MEDIA_ROOT/samples/manual-<source_id>/``
matching the layout the DLsite and F95 scrapers produce, so the
gallery template renders them the same way.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile

log = logging.getLogger(__name__)

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
_DEFAULT_EXT = '.jpg'
_UA = 'Mozilla/5.0 DLib/manual-add'


def new_source_id() -> str:
    """Short opaque id used for manual entries' (source, source_id) key."""
    return uuid.uuid4().hex[:16]


def _ext_for(name: str) -> str:
    suffix = Path(name.split('?')[0]).suffix.lower()
    return suffix if suffix in _IMAGE_EXTS else _DEFAULT_EXT


def _fetch_url(url: str) -> bytes | None:
    try:
        request = Request(url, headers={'User-Agent': _UA})
        with urlopen(request, timeout=30.0) as resp:
            return resp.read()
    except (URLError, TimeoutError, ValueError) as exc:
        log.warning('manual_add: URL fetch failed for %s: %s', url, exc)
        return None


def save_cover(game, source: str | UploadedFile | None) -> bool:
    """Resolve ``source`` (URL string OR uploaded file) into game.cover_image.

    Returns True if a cover was saved. Caller is responsible for
    ``game.save()`` afterwards (we use ``save=False`` so multiple field
    updates can land in a single write).
    """
    if not source:
        return False
    if isinstance(source, UploadedFile):
        name = f'cover{_ext_for(source.name)}'
        game.cover_image.save(name, ContentFile(source.read()), save=False)
        return True
    if isinstance(source, str):
        data = _fetch_url(source)
        if not data:
            return False
        name = f'cover{_ext_for(source)}'
        game.cover_image.save(name, ContentFile(data), save=False)
        return True
    return False


def save_gallery(source_id: str, items: list[UploadedFile | str]) -> list[str]:
    """Save each item under MEDIA_ROOT/samples/manual-<source_id>/.

    Items may be uploaded files (saved verbatim) or URL strings (fetched
    server-side). Returns a list of MEDIA-relative POSIX paths that can
    be stored in ``game.gallery_images``. Failed items are skipped — the
    user sees what landed when the detail page renders.
    """
    if not items:
        return []
    rel_dir = Path('samples') / f'manual-{source_id}'
    abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    results: list[str] = []
    for i, item in enumerate(items, start=1):
        if isinstance(item, UploadedFile):
            data = item.read()
            ext = _ext_for(item.name)
        elif isinstance(item, str) and item.strip():
            data = _fetch_url(item.strip())
            ext = _ext_for(item)
        else:
            continue
        if not data:
            continue
        rel_path = rel_dir / f'{i:02d}{ext}'
        abs_path = Path(settings.MEDIA_ROOT) / rel_path
        try:
            abs_path.write_bytes(data)
        except OSError as exc:
            log.warning('manual_add: write failed for %s: %s', abs_path, exc)
            continue
        results.append(str(rel_path).replace('\\', '/'))
    return results
