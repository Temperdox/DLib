"""Thin wrapper around dlsite-async that returns plain dicts for Django consumption."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from urllib.error import URLError
from urllib.request import Request, urlopen

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.files.base import ContentFile

from dlsite_async import DlsiteAPI
try:
    from dlsite_async import PlayAPI
except ImportError:
    PlayAPI = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

PRODUCT_ID_RE = re.compile(r'((?:RJ|RE|VJ|BJ|RG)\d{4,10})', re.IGNORECASE)


class DlsiteError(Exception):
    pass


def extract_product_id(url_or_id: str) -> str:
    """Accepts a DLsite URL or a bare product id (RJ12345, BJ370220, etc.)."""
    if not url_or_id:
        raise DlsiteError('Empty URL or product id.')
    match = PRODUCT_ID_RE.search(url_or_id)
    if not match:
        raise DlsiteError(f'Could not find a DLsite product id in {url_or_id!r}.')
    return match.group(1).upper()


def _serialize_work(work: Any) -> dict[str, Any]:
    """Convert a Work dataclass / object into a plain dict for Django use."""
    if is_dataclass(work):
        data = asdict(work)
    elif hasattr(work, '__dict__'):
        data = dict(work.__dict__)
    else:
        data = dict(work)

    for key in ('regist_date', 'announce_date', 'modified_date'):
        value = data.get(key)
        if value is not None and hasattr(value, 'isoformat'):
            data[key] = value.isoformat()

    age = data.get('age_category')
    if age is not None and hasattr(age, 'name'):
        data['age_category'] = age.name
    elif isinstance(age, int):
        data['age_category'] = {1: 'ALL_AGES', 2: 'R15', 3: 'R18'}.get(age, 'ALL_AGES')

    work_type = data.get('work_type')
    if work_type is not None and hasattr(work_type, 'name'):
        data['work_type'] = work_type.name

    book_type = data.get('book_type')
    if book_type is not None and hasattr(book_type, 'name'):
        data['book_type'] = book_type.name

    return data


async def _fetch_work_async(product_id: str, locale: str = 'en_US') -> dict[str, Any]:
    async with DlsiteAPI(locale=locale) as api:
        work = await api.get_work(product_id)
    return _serialize_work(work)


def fetch_work(url_or_id: str, locale: str = 'en_US') -> dict[str, Any]:
    """Synchronous wrapper. Returns a dict of normalized work metadata."""
    product_id = extract_product_id(url_or_id)
    return async_to_sync(_fetch_work_async)(product_id, locale)


def normalize_cover_url(url: str | None) -> str | None:
    """DLsite often returns protocol-relative URLs like //img.dlsite.jp/..."""
    if not url:
        return None
    if url.startswith('//'):
        return f'https:{url}'
    return url


def download_cover(url: str, product_id: str) -> tuple[str, bytes] | None:
    """Download cover image bytes. Returns (filename, bytes) or None on failure."""
    url = normalize_cover_url(url) or ''
    if not url:
        return None
    request = Request(url, headers={
        'Referer': 'https://www.dlsite.com/',
        'User-Agent': 'Mozilla/5.0 DLib/0.1',
    })
    try:
        with urlopen(request, timeout=30.0) as resp:
            data = resp.read()
    except (URLError, TimeoutError) as exc:
        log.warning('cover download failed for %s: %s', product_id, exc)
        return None
    suffix = Path(url.split('?')[0]).suffix or '.jpg'
    return f'{product_id}{suffix}', data


def save_cover_to_game(game, url: str | None) -> None:
    """Download and assign the cover image to game.cover_image."""
    if not url:
        return
    result = download_cover(url, game.dlsite_id)
    if not result:
        return
    filename, data = result
    game.cover_image.save(filename, ContentFile(data), save=False)


def download_samples(urls: list[str], product_id: str) -> list[str]:
    """Download every sample image to MEDIA_ROOT/samples/<product_id>/NN.<ext>.

    Returns a list of MEDIA-relative paths suitable for {{ MEDIA_URL }}path
    concatenation (e.g. ``samples/RJ01402281/01.jpg``).
    Already-downloaded files are skipped, so this is cheap to call again on
    refresh.
    """
    from django.conf import settings  # local import to avoid app-loading races

    rel_dir = Path('samples') / product_id
    abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    results: list[str] = []
    for i, raw in enumerate(urls or [], start=1):
        url = normalize_cover_url(raw) or ''
        if not url:
            continue
        ext = Path(url.split('?')[0]).suffix or '.jpg'
        rel_path = rel_dir / f'{i:02d}{ext}'
        abs_path = Path(settings.MEDIA_ROOT) / rel_path
        if not abs_path.exists():
            request = Request(url, headers={
                'Referer': 'https://www.dlsite.com/',
                'User-Agent': 'Mozilla/5.0 DLib/0.1',
            })
            try:
                with urlopen(request, timeout=30.0) as resp:
                    data = resp.read()
            except (URLError, TimeoutError) as exc:
                log.warning('sample %s/%s download failed: %s', product_id, i, exc)
                continue
            abs_path.write_bytes(data)
        # Store with forward slashes so it works as a URL fragment.
        results.append(str(rel_path).replace('\\', '/'))
    return results


# ---------------------------------------------------------------------------
# PlayAPI (authenticated)
# ---------------------------------------------------------------------------

async def _list_purchases_async(username: str, password: str) -> list[dict[str, Any]]:
    if PlayAPI is None:
        raise DlsiteError('PlayAPI is not available in this dlsite-async version.')
    purchases: list[dict[str, Any]] = []
    async with PlayAPI() as api:
        await api.login(username, password)
        async for work, purchase_date in api.purchases():
            data = _serialize_work(work)
            data['purchase_date'] = (
                purchase_date.isoformat() if purchase_date is not None else None
            )
            purchases.append(data)
    return purchases


def list_purchases(username: str, password: str) -> list[dict[str, Any]]:
    if not username or not password:
        raise DlsiteError('DLsite credentials are not configured.')
    return async_to_sync(_list_purchases_async)(username, password)
