from django import template
from django.conf import settings

register = template.Library()


@register.filter
def gallery_image_list(game):
    """Return the list of image URLs to show in the detail-page carousel.

    Order: cover first, then locally-downloaded samples (gallery_images),
    falling back to the remote sample_images URLs if no locals exist yet
    (e.g. games added before the gallery feature shipped, until they're
    refreshed).
    """
    urls = []
    if getattr(game, 'cover_image', None):
        try:
            urls.append(game.cover_image.url)
        except Exception:
            pass
    if not urls and game.cover_url:
        urls.append(game.cover_url)

    locals_ = list(game.gallery_images or [])
    if locals_:
        media_url = settings.MEDIA_URL.rstrip('/') + '/'
        # Cache-bust local sample URLs with the metadata refresh timestamp
        # so the browser doesn't keep showing the OLD cached file bytes
        # after a Refresh metadata replaces the contents on disk.
        cache_bust = ''
        if getattr(game, 'metadata_refreshed_at', None):
            cache_bust = '?v=' + str(int(game.metadata_refreshed_at.timestamp()))
        urls.extend(media_url + p.lstrip('/') + cache_bust for p in locals_)
    else:
        urls.extend(u for u in (game.sample_images or []) if u)

    # de-dupe while preserving order
    seen, ordered = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


@register.filter
def seconds_display(value):
    try:
        total = int(value or 0)
    except (TypeError, ValueError):
        return '0s'
    if total < 60:
        return f'{total}s'
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f'{hours}h {minutes}m'
    return f'{minutes}m'
