"""Best-effort F95Zone thread scraper.

We don't have an official API, so we fetch the public OpenGraph metadata
(stable across forum theme changes) plus parse the bracketed title format
F95 uses to embed version/status/dev. Login isn't attempted in v1.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from django.core.files.base import ContentFile

log = logging.getLogger(__name__)

THREAD_ID_RE = re.compile(r'/threads/(?:[^/]+\.)?(\d+)', re.IGNORECASE)
BARE_THREAD_RE = re.compile(r'^\d{1,9}$')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')


class F95ZoneError(Exception):
    pass


def extract_thread_id(url_or_id: str) -> str:
    s = (url_or_id or '').strip()
    if not s:
        raise F95ZoneError('Empty URL or thread id.')
    m = THREAD_ID_RE.search(s)
    if m:
        return m.group(1)
    if BARE_THREAD_RE.match(s):
        return s
    raise F95ZoneError(f'Could not find an F95Zone thread id in {url_or_id!r}.')


def thread_url(thread_id: str) -> str:
    return f'https://f95zone.to/threads/{thread_id}/'


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------

# Heuristic: status tags that frequently appear in titles.
_STATUSES = {
    'completed', 'ongoing', 'onhold', 'on hold', 'abandoned',
    'final', 'demo', 'beta', 'alpha',
}

# Version-y bracket contents: v1, v1.2, 0.5.1, 0.4a, etc.
_VERSION_RE = re.compile(r'^v?\d+(?:\.\d+)*[a-z\d]*$', re.IGNORECASE)


def parse_title(raw_title: str) -> dict[str, Any]:
    """Pull name, version, status, developer, tags out of an F95-style title.

    Example input:
        'Goblin Breeding Farm 2 [v1.2.3] [Completed] [3DCG] [SomeDev]'
    Output:
        {name: 'Goblin Breeding Farm 2', version: 'v1.2.3',
         status: 'Completed', developer: 'SomeDev', tags: ['3DCG']}
    """
    out: dict[str, Any] = {
        'name': raw_title.strip(),
        'version': '',
        'status': '',
        'developer': '',
        'tags': [],
    }
    if not raw_title:
        return out

    brackets = re.findall(r'\[([^\]]+)\]', raw_title)
    name = re.sub(r'\s*\[[^\]]+\]', '', raw_title).strip()
    out['name'] = name or raw_title.strip()

    leftovers: list[str] = []
    for b in brackets:
        b = b.strip()
        if not b:
            continue
        if _VERSION_RE.match(b):
            if not out['version']:
                out['version'] = b
            continue
        if b.lower() in _STATUSES:
            if not out['status']:
                out['status'] = b
            continue
        leftovers.append(b)

    # By convention the LAST remaining bracket is the developer/creator.
    if leftovers:
        out['developer'] = leftovers.pop()
        out['tags'] = leftovers
    return out


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _session_headers() -> dict[str, str]:
    """Build a request header set, merging in stored F95Zone session cookies +
    the user-agent captured at login (cf_clearance is UA-bound)."""
    ua = UA
    cookie_header = ''
    try:
        from library.models import AppSettings  # lazy: avoid app-loading race
        s = AppSettings.load()
        if s.f95zone_user_agent:
            ua = s.f95zone_user_agent
        cookies = s.f95zone_cookies or {}
        cookie_header = '; '.join(f'{k}={v}' for k, v in cookies.items() if v)
    except Exception:
        log.exception('failed to read F95Zone session from settings')

    headers = {
        'User-Agent': ua,
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/avif,image/webp,*/*;q=0.8'),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'identity',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    }
    if cookie_header:
        headers['Cookie'] = cookie_header
    return headers


def _fetch(url: str) -> str:
    request = Request(url, headers=_session_headers())
    try:
        with urlopen(request, timeout=30.0) as resp:
            data = resp.read()
    except (URLError, TimeoutError) as exc:
        raise F95ZoneError(f'Failed to fetch {url}: {exc}') from exc
    return data.decode('utf-8', errors='replace')


def title_from_url(url_or_slug: str, thread_id: str) -> str:
    """Best-effort title derived from the URL slug when scraping fails.

    For 'https://f95zone.to/threads/long-live-the-princess-v1-0-0-belle.94/'
    returns 'Long Live The Princess V1 0 0 Belle' — ugly but readable.
    """
    s = (url_or_slug or '').strip()
    # /threads/<slug>.<id>/
    m = re.search(r'/threads/([^/]+?)(?:\.\d+)?/?$', s)
    if not m:
        return f'F95Zone #{thread_id}'
    slug = m.group(1)
    if slug.endswith('.' + thread_id):
        slug = slug[:-len(thread_id) - 1]
    if not slug:
        return f'F95Zone #{thread_id}'
    words = re.split(r'[-_]+', slug)
    return ' '.join(w.capitalize() for w in words if w)


def _meta(soup, prop: str) -> str:
    el = soup.find('meta', attrs={'property': prop}) \
         or soup.find('meta', attrs={'name': prop})
    if not el:
        return ''
    return (el.get('content') or '').strip()


# Hosts whose links in the OP we treat as "download" links.
DOWNLOAD_HOSTS: list[tuple[str, str]] = [
    ('mega.nz', 'MEGA'),
    ('mediafire.com', 'MediaFire'),
    ('workupload', 'Workupload'),
    ('gofile.io', 'GoFile'),
    ('pixeldrain', 'Pixeldrain'),
    ('rapidgator', 'Rapidgator'),
    ('dropbox.com', 'Dropbox'),
    ('drive.google.com', 'Google Drive'),
    ('zippyshare', 'Zippyshare'),
    ('bunkr', 'Bunkr'),
    ('buzzheavier', 'Buzzheavier'),
    ('datanodes', 'Datanodes'),
    ('vikingfile', 'Vikingfile'),
    ('katfile', 'Katfile'),
    ('nitroflare', 'Nitroflare'),
    ('turbobit', 'Turbobit'),
    ('fikper', 'Fikper'),
    ('mixdrop', 'MixDrop'),
    ('send.cm', 'Send.cm'),
    ('qiwi.gg', 'Qiwi'),
    ('uploadhaven', 'UploadHaven'),
    ('itch.io', 'itch.io'),
    ('patreon.com', 'Patreon'),
    ('subscribestar', 'SubscribeStar'),
    # F95 attachments are kept only when they look like NON-image files
    # (saves, patches, etc.) — image attachments are filtered out in
    # _extract_downloads since they belong in the gallery, not Downloads.
    ('attachments.f95zone.to', 'F95 attachment'),
    ('/masked/', 'F95 masked link'),
]

# Image file extensions used to decide whether an attachments.f95zone.to
# link is a gallery preview (filter out) or a real download (keep).
_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif'}

# Substrings whose presence in an <img src> means it's UI chrome, not content.
_IMG_SKIP = (
    '/styles/', '/smilies/', '/emoji/', 'favicon', '/external-content',
    'duckduckgo', 'badges/', '/avatars/', 'graphics/',
)


def _find_op_body(soup):
    """Return the BeautifulSoup element holding the OP post body, or None."""
    op = soup.find('article', class_='message') or soup.find('article')
    if not op:
        return None
    return (op.find('div', class_='bbWrapper')
            or op.find('div', class_='message-body')
            or op.find('div', class_='messageContent')
            or op)


def _extract_images(op_body) -> list[str]:
    """Image URLs in the OP body, in document order, deduped.

    F95Zone's inline ``<img src>`` is usually a Cloudflare-proxied THUMBNAIL
    (blurry, downsized) — the full-size original is in the parent
    ``<a href="https://attachments.f95zone.to/.../image.jpg">``. We prefer
    the ``<a href>`` when present so the gallery downloads the high-res
    file instead of the preview thumb that used to show up blurred in the
    carousel.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for img in op_body.find_all('img'):
        raw = (img.get('src') or img.get('data-src')
               or img.get('data-url') or '').strip()
        if not raw or raw.startswith('data:'):
            continue
        if any(s in raw for s in _IMG_SKIP):
            continue

        # Prefer the parent <a> if it points at the full-size attachment.
        url = ''
        parent_a = img.find_parent('a')
        if parent_a and parent_a.has_attr('href'):
            href = parent_a['href'].strip()
            href_low = href.lower()
            ext = Path(href_low.split('?')[0]).suffix
            if ('attachments.f95zone.to' in href_low and ext in _IMAGE_EXTS) \
               or (ext in _IMAGE_EXTS and href.startswith('http')):
                url = href

        # Fall back to the img src if no full-size anchor.
        if not url:
            url = raw

        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/'):
            url = 'https://f95zone.to' + url
        if not url.startswith('http'):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _extract_forum_tags(soup) -> list[str]:
    """Read the official forum-supplied tag list from the thread header.

    F95Zone renders this as ``<span class="js-tagList">`` containing
    ``<a class="tagItem">…</a>`` children. These are the canonical
    classification tags (2d game, big tits, corruption, etc) — far more
    reliable than the bracketed dev-supplied tags in the title.
    """
    tag_list = (soup.find('span', class_='js-tagList')
                or soup.find('dl', class_='tagList'))
    if tag_list is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for a in tag_list.find_all('a'):
        text = (a.get_text() or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _extract_downloads(op_body, sample_image_urls: list[str] | None = None) -> list[dict[str, str]]:
    """Anchor tags in OP whose href hits a known download host.

    Excludes ``attachments.f95zone.to`` links that are clearly preview
    images (URL ends in an image extension OR the same URL was also
    extracted as a gallery sample). Those belong in the gallery, not the
    Downloads tab — they spammed the Downloads list with junk like
    "F95 attachment" entries pointing at thumbnails. Save files, patches,
    and other non-image attachments stay.
    """
    sample_set = set(sample_image_urls or [])
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in op_body.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href in seen:
            continue
        low = href.lower()
        host = next((label for pat, label in DOWNLOAD_HOSTS if pat in low), None)
        if not host:
            continue
        # Filter image-attachment noise out of Downloads.
        if 'attachments.f95zone.to' in low:
            if href in sample_set:
                continue
            ext = Path(low.split('?')[0]).suffix
            if ext in _IMAGE_EXTS:
                continue
        text = a.get_text(strip=True) or host
        seen.add(href)
        out.append({'label': text[:80], 'host': host, 'url': href})
    return out


def fetch_thread(url_or_id: str) -> dict[str, Any]:
    """Returns a dict with normalized metadata for a F95Zone thread.

    If F95Zone blocks the request (Cloudflare 403 etc.), returns a stub with
    a URL-derived title so the user can still add the game and refresh later.
    """
    thread_id = extract_thread_id(url_or_id)
    url = thread_url(thread_id)
    fallback = {
        'thread_id': thread_id,
        'url': url,
        'title': title_from_url(url_or_id, thread_id),
        'title_full': '',
        'version': '',
        'status': '',
        'developer': '',
        'tags': [],
        'description': '',
        'image': '',
        'sample_images': [],
        'downloads': [],
        'fetch_failed': True,
    }
    try:
        html = _fetch(url)
    except F95ZoneError as exc:
        log.warning('f95 fetch_thread fell back: %s', exc)
        return fallback

    soup = BeautifulSoup(html, 'lxml')

    title_full = _meta(soup, 'og:title') or (soup.title.string if soup.title else '')
    parsed = parse_title(title_full)
    image = _meta(soup, 'og:image')
    description = _meta(soup, 'og:description')
    canonical = _meta(soup, 'og:url') or url

    op_body = _find_op_body(soup)
    sample_images: list[str] = []
    downloads: list[dict[str, str]] = []
    if op_body is not None:
        sample_images = _extract_images(op_body)
        # Pass samples so attachment-image URLs are filtered out of Downloads
        # (they belong in the gallery, not the Downloads tab).
        downloads = _extract_downloads(op_body, sample_image_urls=sample_images)
        if not image and sample_images:
            image = sample_images[0]

    # Combine bracket-derived tags from the title with the official forum
    # tag list, deduped while preserving order (brackets first).
    forum_tags = _extract_forum_tags(soup)
    seen_tags = set()
    merged_tags: list[str] = []
    for t in (parsed['tags'] or []) + forum_tags:
        key = t.lower()
        if key in seen_tags:
            continue
        seen_tags.add(key)
        merged_tags.append(t)

    return {
        'thread_id': thread_id,
        'url': canonical,
        'title': parsed['name'] or fallback['title'],
        'title_full': title_full,
        'version': parsed['version'],
        'status': parsed['status'],
        'developer': parsed['developer'],
        'tags': merged_tags,
        'description': description,
        'image': image,
        'sample_images': sample_images,
        'downloads': downloads,
        'fetch_failed': False,
    }


# ---------------------------------------------------------------------------
# Cover download
# ---------------------------------------------------------------------------

def download_cover(url: str, thread_id: str) -> tuple[str, bytes] | None:
    if not url:
        return None
    headers = _session_headers()
    headers['Referer'] = 'https://f95zone.to/'
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30.0) as resp:
            data = resp.read()
    except (URLError, TimeoutError) as exc:
        log.warning('f95 cover download failed for %s: %s', thread_id, exc)
        return None
    suffix = Path(url.split('?')[0]).suffix or '.jpg'
    return f'f95-{thread_id}{suffix}', data


def save_cover_to_game(game, url: str | None) -> None:
    if not url:
        return
    result = download_cover(url, game.source_id)
    if not result:
        return
    filename, data = result
    game.cover_image.save(filename, ContentFile(data), save=False)


def download_samples(urls: list[str], thread_id: str,
                     force: bool = True) -> list[str]:
    """Download every F95 sample image to ``MEDIA_ROOT/samples/f95-<thread_id>/NN.<ext>``.

    Uses the same session headers as ``_fetch`` (Cloudflare clearance +
    xf_session cookies, captured-at-login UA) so ``attachments.f95zone.to``
    images succeed — the dlsite downloader's generic UA/Referer can't
    fetch those because they require a valid F95 session.

    When ``force=True`` (the default for refresh-metadata) the existing
    ``f95-<thread_id>/`` directory is wiped first so old blurry-thumbnail
    files left behind by previous extraction logic get replaced with the
    new full-size attachment originals. When called with ``force=False``
    existing files are kept (cheap re-runs).

    Returns MEDIA-relative paths suitable for ``{{ MEDIA_URL }}path``.
    """
    from django.conf import settings  # local import: app-loading races

    rel_dir = Path('samples') / f'f95-{thread_id}'
    abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
    if force and abs_dir.exists():
        # Wipe stale files so old preview-thumb downloads can't shadow new
        # full-size ones (the per-index NN.ext filenames would otherwise
        # short-circuit re-download).
        for old in abs_dir.iterdir():
            try:
                if old.is_file():
                    old.unlink()
            except OSError:
                pass
    abs_dir.mkdir(parents=True, exist_ok=True)

    headers = _session_headers()
    headers['Referer'] = 'https://f95zone.to/'

    saved = 0
    failed = 0
    results: list[str] = []
    for i, raw in enumerate(urls or [], start=1):
        url = (raw or '').strip()
        if not url:
            continue
        if url.startswith('//'):
            url = 'https:' + url
        if not url.startswith('http'):
            continue
        ext = Path(url.split('?')[0]).suffix.lower()
        if ext not in _IMAGE_EXTS:
            # Unknown extension — guess .jpg so the file at least has a
            # suffix the browser/OS can render.
            ext = '.jpg'
        rel_path = rel_dir / f'{i:02d}{ext}'
        abs_path = Path(settings.MEDIA_ROOT) / rel_path
        if not abs_path.exists():
            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=30.0) as resp:
                    data = resp.read()
            except (URLError, TimeoutError) as exc:
                failed += 1
                log.warning('f95 sample %s/%s download failed (%s): %s',
                            thread_id, i, url, exc)
                continue
            abs_path.write_bytes(data)
            saved += 1
        results.append(str(rel_path).replace('\\', '/'))
    log.info('f95 download_samples thread=%s requested=%d saved=%d failed=%d kept=%d',
             thread_id, len(urls or []), saved, failed, len(results))
    return results
