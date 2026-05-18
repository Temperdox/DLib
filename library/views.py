from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Iterable

from django.conf import settings as django_settings
from django.contrib import messages
from django.core import serializers
from django.core.management import call_command
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import (
    AppSettings, Creator, Game, GameAlias, ManualGameTag, PlaySession, Tag,
)
from .services import (
    dlsite_client,
    f95zone_client,
    install_scanner,
    native_dialog,
    process_tracker,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Page views
# ---------------------------------------------------------------------------

SORT_CHOICES = {
    'recent': ('-added_at', 'Recently added'),
    'title_asc': ('title', 'Title (A→Z)'),
    'title_desc': ('-title', 'Title (Z→A)'),
    'time_desc': ('-time_played_seconds', 'Most played'),
    'time_asc': ('time_played_seconds', 'Least played'),
    'rating_desc': ('-personal_rating', 'Highest rating'),
    'rating_asc': ('personal_rating', 'Lowest rating'),
    'last_played': ('-last_played_at', 'Recently played'),
}


def _filtered_games(request):
    qs = Game.objects.select_related('circle').prefetch_related('tags')
    query = (request.GET.get('q') or '').strip()
    if query:
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(title_masked__icontains=query)
            | Q(circle__name__icontains=query)
            | Q(source_id__icontains=query)
            | Q(tags__name__icontains=query)
        ).distinct()
    installed = request.GET.get('installed')
    if installed == '1':
        qs = qs.exclude(executable_path='')
    elif installed == '0':
        qs = qs.filter(executable_path='')
    source = request.GET.get('source')
    if source in {Game.SOURCE_DLSITE, Game.SOURCE_F95ZONE}:
        qs = qs.filter(source=source)
    # Default-hide bad games; user can flip via ?show_bad=1
    show_bad = request.GET.get('show_bad') == '1'
    if not show_bad:
        qs = qs.exclude(is_bad=True)
    if request.GET.get('favorites_only') == '1':
        qs = qs.filter(is_favorite=True)
    tag_id = request.GET.get('tag')
    if tag_id:
        qs = qs.filter(tags__pk=tag_id)
    sort = request.GET.get('sort', 'recent')
    order_field, _label = SORT_CHOICES.get(sort, SORT_CHOICES['recent'])
    qs = qs.order_by(order_field, '-added_at')
    return qs, query, sort


def library_view(request):
    games, query, sort = _filtered_games(request)
    settings_obj = AppSettings.load()
    context = {
        'games': games,
        'query': query,
        'sort': sort,
        'sort_choices': SORT_CHOICES,
        'settings_obj': settings_obj,
        'installed_filter': request.GET.get('installed', ''),
        'source_filter': request.GET.get('source', ''),
        'show_bad': request.GET.get('show_bad') == '1',
        'favorites_only': request.GET.get('favorites_only') == '1',
    }
    return render(request, 'library/library.html', context)


def game_detail_view(request, pk: int):
    game = get_object_or_404(
        Game.objects.select_related('circle').prefetch_related('tags', 'play_sessions'),
        pk=pk,
    )
    settings_obj = AppSettings.load()
    return render(request, 'library/game_detail.html', {
        'game': game,
        'is_running': process_tracker.is_running(game.pk),
        'recent_sessions': game.play_sessions.all()[:10],
        # Source-specific install root used to seed the native folder picker
        # when the game doesn't have an install_folder set yet. Falls back to
        # ~/DLib/Games/<source> (cross-platform) if the user hasn't set one,
        # so the picker never lands in Downloads.
        'default_install_root': settings_obj.effective_install_root_for(game.source),
    })


def settings_view(request):
    return render(request, 'library/settings.html', {
        'settings_obj': AppSettings.load(),
    })


# ---------------------------------------------------------------------------
# HTMX/API endpoints
# ---------------------------------------------------------------------------

def _hx_or_redirect(request, game: Game | None = None):
    if request.headers.get('HX-Request'):
        if game is not None:
            return _render_card(request, game)
        return games_grid_partial(request)
    if game is not None:
        return redirect(game.get_absolute_url())
    return redirect('library:library')


def _render_card(request, game: Game) -> HttpResponse:
    return render(request, 'library/partials/_game_card.html', {
        'game': game,
        'is_running': process_tracker.is_running(game.pk),
    })


def _normalize_tags(raw: Iterable[str] | None, category: str) -> list[Tag]:
    if not raw:
        return []
    tags = []
    for name in raw:
        clean = (name or '').strip()
        if not clean:
            continue
        tag, _ = Tag.objects.get_or_create(name=clean, category=category)
        tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Source detection + per-source metadata
# ---------------------------------------------------------------------------

_DLSITE_HOST_RE = re.compile(r'(?:dlsite|eisys)\.com', re.IGNORECASE)
_F95_HOST_RE = re.compile(r'f95zone\.(?:to|com)', re.IGNORECASE)


def detect_source(url_or_id: str) -> tuple[str, str]:
    """Return ``(source, source_id)`` derived from a URL or bare id.

    Heuristics:
      * Bare DLsite id (RJ12345, BJ12345, ...) -> dlsite
      * dlsite.com URL -> dlsite (extract RJ/BJ/...)
      * f95zone.to URL -> f95zone (extract numeric thread id)
      * Bare numeric id -> f95zone (forum thread)
    """
    s = (url_or_id or '').strip()
    if not s:
        raise ValueError('Empty URL or product id.')

    # Bare DLsite product id
    if re.fullmatch(r'(?:RJ|RE|VJ|BJ|RG)\d{4,10}', s, re.IGNORECASE):
        return (Game.SOURCE_DLSITE, s.upper())

    if _F95_HOST_RE.search(s):
        return (Game.SOURCE_F95ZONE, f95zone_client.extract_thread_id(s))

    if _DLSITE_HOST_RE.search(s):
        return (Game.SOURCE_DLSITE, dlsite_client.extract_product_id(s))

    # Bare DLsite id anywhere in the string
    m = re.search(r'((?:RJ|RE|VJ|BJ|RG)\d{4,10})', s, re.IGNORECASE)
    if m:
        return (Game.SOURCE_DLSITE, m.group(1).upper())

    # Bare numeric — assume f95zone forum thread id
    if re.fullmatch(r'\d{1,9}', s):
        return (Game.SOURCE_F95ZONE, s)

    raise ValueError(f'Could not detect source for {url_or_id!r}.')


def _apply_dlsite_metadata(game: Game, data: dict) -> Game:
    settings_obj = AppSettings.load()

    game.site_id = data.get('site_id') or ''
    game.title = data.get('work_name') or data.get('title_name') or game.title or game.source_id
    game.title_masked = data.get('work_name_masked') or data.get('title_name_masked') or ''
    game.work_type = data.get('work_type') or ''

    age_raw = (data.get('age_category') or '').upper()
    if age_raw == 'R18':
        game.age_category = Game.AGE_R18
    elif age_raw == 'R15':
        game.age_category = Game.AGE_R15
    else:
        game.age_category = Game.AGE_ALL

    circle_name = data.get('circle') or data.get('brand') or data.get('publisher')
    if circle_name:
        creator, _ = Creator.objects.get_or_create(
            name=circle_name,
            defaults={'dlsite_maker_id': data.get('maker_id') or ''},
        )
        if data.get('maker_id') and not creator.dlsite_maker_id:
            creator.dlsite_maker_id = data['maker_id']
            creator.save(update_fields=['dlsite_maker_id'])
        game.circle = creator

    game.description = data.get('description') or ''
    game.cover_url = dlsite_client.normalize_cover_url(data.get('work_image')) or ''
    game.scenario = data.get('scenario') or []
    game.illustration = data.get('illustration') or []
    game.voice_actor = data.get('voice_actor') or []
    game.music = data.get('music') or []
    game.sample_images = [
        dlsite_client.normalize_cover_url(u) for u in (data.get('sample_images') or []) if u
    ]
    try:
        game.gallery_images = dlsite_client.download_samples(
            game.sample_images, game.source_id,
        )
    except Exception:
        log.exception('sample download failed for %s', game.source_id)
    game.file_format = data.get('file_format') or []
    game.file_size = data.get('file_size') or ''
    game.language = data.get('language') or []

    regist = data.get('regist_date')
    if isinstance(regist, str):
        parsed = parse_datetime(regist)
        if parsed and timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        game.regist_date = parsed

    game.metadata_refreshed_at = timezone.now()

    if not game.cover_image and game.cover_url:
        dlsite_client.save_cover_to_game(game, game.cover_url)

    game.save()

    genre_tags = _normalize_tags(data.get('genre'), Tag.GENRE)
    _set_tags_preserving_manual(game, genre_tags)

    return game


def _set_tags_preserving_manual(game: Game, auto_tags: list[Tag]) -> None:
    """Replace the game's tag set with the new auto-pulled list, but keep
    every tag the user has manually added (tracked via ManualGameTag).
    """
    manual_tag_ids = list(game.manual_tag_set.values_list('tag_id', flat=True))
    if manual_tag_ids:
        manual_tags = list(Tag.objects.filter(pk__in=manual_tag_ids))
    else:
        manual_tags = []
    combined: dict[int, Tag] = {t.pk: t for t in auto_tags}
    for t in manual_tags:
        combined.setdefault(t.pk, t)
    game.tags.set(list(combined.values()))


def _apply_f95zone_metadata(game: Game, data: dict) -> Game:
    game.title = data.get('title') or game.title or game.source_id
    game.title_masked = ''
    game.work_type = data.get('version') or ''  # repurpose: shown as a version-ish chip

    game.age_category = Game.AGE_R18  # F95Zone is adult by default
    game.description = data.get('description') or ''
    game.cover_url = data.get('image') or ''

    dev = (data.get('developer') or '').strip()
    if dev:
        creator, _ = Creator.objects.get_or_create(name=dev)
        game.circle = creator
    else:
        game.circle = None

    game.sample_images = data.get('sample_images') or []
    # Drop the cover from the sample set so it isn't shown twice in the gallery.
    if game.cover_url and game.cover_url in game.sample_images:
        game.sample_images = [u for u in game.sample_images if u != game.cover_url]
    try:
        game.gallery_images = (
            dlsite_client.download_samples(game.sample_images, game.source_id)
            if game.sample_images else []
        )
    except Exception:
        log.exception('f95 sample download failed for %s', game.source_id)

    game.download_links = data.get('downloads') or []
    game.scenario = []
    game.illustration = []
    game.voice_actor = []
    game.music = []
    game.file_format = []
    game.file_size = ''
    game.language = []

    game.metadata_refreshed_at = timezone.now()

    if not game.cover_image and game.cover_url:
        f95zone_client.save_cover_to_game(game, game.cover_url)

    game.save()

    tag_objs = _normalize_tags(data.get('tags'), Tag.GENRE)
    _set_tags_preserving_manual(game, tag_objs)
    return game


def _fetch_and_apply(game: Game) -> None:
    """Refresh metadata for an existing game, dispatching by source."""
    if game.source == Game.SOURCE_F95ZONE:
        data = f95zone_client.fetch_thread(game.dlsite_url or game.source_id)
        _apply_f95zone_metadata(game, data)
    else:
        settings_obj = AppSettings.load()
        data = dlsite_client.fetch_work(
            game.dlsite_url or game.source_id,
            locale=settings_obj.locale or 'en_US',
        )
        _apply_dlsite_metadata(game, data)


@require_POST
def add_game(request):
    url = (request.POST.get('url') or '').strip()
    if not url:
        return HttpResponse('Please paste a DLsite or F95Zone URL (or product id).', status=400)

    try:
        source, source_id = detect_source(url)
    except (ValueError, dlsite_client.DlsiteError, f95zone_client.F95ZoneError) as exc:
        return HttpResponse(str(exc), status=400)

    existing = Game.objects.filter(source=source, source_id=source_id).first()
    if existing:
        return HttpResponse(
            f'<div class="toast toast-warn">Already in library: '
            f'<a href="{existing.get_absolute_url()}">{existing.title}</a></div>',
            status=409,
        )

    try:
        if source == Game.SOURCE_F95ZONE:
            # Pass the original url so the slug-fallback can craft a readable title
            data = f95zone_client.fetch_thread(url)
            canonical_url = data.get('url') or f95zone_client.thread_url(source_id)
            game = Game(
                source=source,
                source_id=source_id,
                title=data.get('title') or source_id,
                dlsite_url=canonical_url,
            )
            game.save()
            _apply_f95zone_metadata(game, data)
        else:
            settings_obj = AppSettings.load()
            data = dlsite_client.fetch_work(url, locale=settings_obj.locale or 'en_US')
            canonical_url = (
                url if url.startswith('http')
                else f'https://www.dlsite.com/maniax/work/=/product_id/{source_id}.html'
            )
            game = Game(
                source=source,
                source_id=source_id,
                title=data.get('work_name') or source_id,
                dlsite_url=canonical_url,
            )
            game.save()
            _apply_dlsite_metadata(game, data)
    except Exception as exc:
        log.exception('add_game fetch failed for %s', url)
        return HttpResponse(f'Failed to fetch metadata: {exc}', status=502)

    if request.headers.get('HX-Request'):
        response = render(request, 'library/partials/_game_card.html', {
            'game': game,
            'is_running': False,
        })
        response['HX-Trigger'] = 'gameAdded'
        return response
    return redirect(game.get_absolute_url())


@require_POST
def refresh_metadata(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    try:
        _fetch_and_apply(game)
    except Exception as exc:
        log.exception('refresh_metadata failed for game %s', pk)
        return HttpResponse(f'Refresh failed: {exc}', status=502)
    return _hx_or_redirect(request, game)


def _pill_response(request, game, error=None):
    """Render the install-status pill, with HX-Retarget so HTMX places it
    in #install-status-<pk> regardless of which target the caller used."""
    response = render(request, 'library/partials/_install_status.html', {
        'game': game,
        'error': error,
    })
    response['HX-Retarget'] = f'#install-status-{game.pk}'
    response['HX-Reswap'] = 'outerHTML'
    return response


def _maybe_relocate(game) -> None:
    """Move the game's folder into the source-specific linked/ tree."""
    settings_obj = AppSettings.load()
    root = settings_obj.install_root_for(game.source)
    try:
        install_scanner.relocate_to_linked(game, root)
    except Exception:
        log.exception('relocate_to_linked failed for game %s', game.pk)


@require_POST
def set_install_folder(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    folder = (request.POST.get('folder') or '').strip()
    if not folder:
        return HttpResponse('Folder is required.', status=400)

    game.install_folder = folder
    candidates = install_scanner.scan_executables(folder)
    is_hx = bool(request.headers.get('HX-Request'))

    if not candidates:
        game.executable_path = ''
        game.save(update_fields=['install_folder', 'executable_path'])
        if is_hx:
            return _pill_response(request, game,
                                  error=f'No executables found inside "{folder}".')
        messages.error(request, 'No executables found in that folder.')
        return redirect(game.get_absolute_url())

    auto = install_scanner.best_single_exe(folder)
    if auto:
        game.executable_path = auto
        game.save(update_fields=['install_folder', 'executable_path'])
        _maybe_relocate(game)
        if is_hx:
            return _pill_response(request, game)
        return redirect(game.get_absolute_url())

    game.save(update_fields=['install_folder'])
    return render(request, 'library/partials/_exe_picker_modal.html', {
        'game': game,
        'candidates': candidates[:25],
    })


@require_POST
def set_executable(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    exe = (request.POST.get('exe') or '').strip()
    if not exe:
        return HttpResponse('exe is required', status=400)
    game.executable_path = exe
    if not game.install_folder:
        game.install_folder = str(Path(exe).parent)
    game.save(update_fields=['executable_path', 'install_folder'])
    _maybe_relocate(game)
    if request.headers.get('HX-Request'):
        return _pill_response(request, game)
    return redirect(game.get_absolute_url())


@require_POST
def launch_game(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    exe_override = None
    track_exe = None

    if game.launch_with_mtool:
        if not game.executable_path:
            return HttpResponse('Set the game executable first.', status=400)
        exe_dir = Path(game.executable_path).parent
        bat = exe_dir / 'StartWithTool.bat'
        if not bat.is_file():
            return HttpResponse(
                'StartWithTool.bat was not found next to the game executable. '
                'Launch this game through MTool once first — MTool will create '
                'StartWithTool.bat in the game folder, then DLib can use it.',
                status=400,
            )
        exe_override = str(bat)
        track_exe = game.executable_path

    try:
        process_tracker.launch_and_track(game, exe_override=exe_override,
                                         track_exe=track_exe)
    except process_tracker.LaunchError as exc:
        return HttpResponse(str(exc), status=400)
    if request.headers.get('HX-Request'):
        return _pill_response(request, game)
    return redirect(game.get_absolute_url())


@require_POST
def open_install_folder(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    folder = game.install_folder
    if not folder and game.executable_path:
        folder = str(Path(game.executable_path).parent)
    if not folder or not os.path.isdir(folder):
        return HttpResponse('No install folder is set for this game.', status=400)
    try:
        if sys.platform == 'win32':
            os.startfile(folder)  # noqa: S606
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])
    except OSError as exc:
        return HttpResponse(f'Failed to open folder: {exc}', status=500)
    return HttpResponse('', status=204)


@require_POST
def toggle_mtool(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    game.launch_with_mtool = request.POST.get('enabled') in ('1', 'true', 'on')
    game.save(update_fields=['launch_with_mtool'])
    if request.headers.get('HX-Request'):
        return HttpResponse('')
    return redirect(game.get_absolute_url())


@require_POST
def toggle_bad(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    game.is_bad = request.POST.get('enabled') in ('1', 'true', 'on')
    game.save(update_fields=['is_bad'])
    if request.headers.get('HX-Request'):
        return _render_card(request, game)
    return redirect(game.get_absolute_url())


@require_POST
def toggle_favorite(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    game.is_favorite = request.POST.get('enabled') in ('1', 'true', 'on')
    game.save(update_fields=['is_favorite'])
    if request.headers.get('HX-Request'):
        return _render_card(request, game)
    return redirect(game.get_absolute_url())


@require_POST
def update_rating(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    raw = (request.POST.get('rating') or '').strip()
    if raw == '' or raw == '0':
        game.personal_rating = None
    else:
        try:
            value = int(raw)
        except ValueError:
            return HttpResponse('Rating must be an integer 1-10.', status=400)
        if not 1 <= value <= 10:
            return HttpResponse('Rating must be 1-10.', status=400)
        game.personal_rating = value
    game.save(update_fields=['personal_rating'])
    if request.headers.get('HX-Request'):
        return render(request, 'library/partials/_rating.html', {'game': game})
    return redirect(game.get_absolute_url())


@require_POST
def update_status(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    new_status = request.POST.get('status', Game.STATUS_UNKNOWN)
    if new_status not in dict(Game.STATUS_CHOICES):
        return HttpResponse('Invalid status.', status=400)
    game.status = new_status
    game.save(update_fields=['status'])
    return _hx_or_redirect(request, game)


@require_POST
def delete_game(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    game.delete()
    if request.headers.get('HX-Request'):
        return HttpResponse('')
    return redirect('library:library')


def game_card_partial(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    return _render_card(request, game)


def _render_tag_section(request, game: Game) -> HttpResponse:
    return render(request, 'library/partials/_tag_section.html', {'game': game})


def tag_section_partial(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    return _render_tag_section(request, game)


@require_GET
def tag_suggestions(request, pk: int):
    """Suggestions for the add-tag autocomplete.

    With a non-empty q: case-insensitive prefix/contains match across ALL tags
    (auto-pulled or manual), alphabetized, capped at 20.

    With an empty q: the most-frequently-manually-added tags across the whole
    library, ordered by descending count, capped at 20. This is "what tags do
    you usually slap onto games" suggested before you type.
    """
    get_object_or_404(Game, pk=pk)  # validate pk exists
    query = (request.GET.get('q') or '').strip()
    if query:
        from django.db.models import Case, IntegerField, Value, When
        qs = (Tag.objects
              .filter(name__icontains=query)
              .annotate(prefix=Case(
                  When(name__istartswith=query, then=Value(0)),
                  default=Value(1),
                  output_field=IntegerField(),
              ))
              .order_by('prefix', 'name')[:20])
        names = list(qs.values_list('name', flat=True))
    else:
        from django.db.models import Count
        rows = (ManualGameTag.objects
                .values('tag__name')
                .annotate(c=Count('id'))
                .order_by('-c', 'tag__name')[:20])
        names = [r['tag__name'] for r in rows]
    return JsonResponse({'tags': names})


@require_POST
def add_tag(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    name = (request.POST.get('name') or '').strip()
    if not name:
        return HttpResponse('name is required', status=400)
    if len(name) > 120:
        return HttpResponse('tag name too long (120 max)', status=400)
    tag, _ = Tag.objects.get_or_create(name=name, category=Tag.GENRE)
    game.tags.add(tag)
    ManualGameTag.objects.get_or_create(game=game, tag=tag)
    game.save(update_fields=['updated_at'])  # bump for auto-refresh poller
    if request.headers.get('HX-Request'):
        return _render_tag_section(request, game)
    return redirect(game.get_absolute_url())


@require_POST
def remove_tag(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    name = (request.POST.get('name') or '').strip()
    if not name:
        return HttpResponse('name is required', status=400)
    tag = Tag.objects.filter(name=name).first()
    if tag:
        game.tags.remove(tag)
        ManualGameTag.objects.filter(game=game, tag=tag).delete()
        game.save(update_fields=['updated_at'])
    if request.headers.get('HX-Request'):
        return _render_tag_section(request, game)
    return redirect(game.get_absolute_url())


def install_status_partial(request, pk: int):
    game = get_object_or_404(Game, pk=pk)
    return render(request, 'library/partials/_install_status.html', {'game': game})


def games_grid_partial(request):
    games, query, sort = _filtered_games(request)
    return render(request, 'library/partials/_grid.html', {
        'games': games,
        'query': query,
        'sort': sort,
    })


@require_POST
def pick_folder_dialog(request):
    initial = (request.POST.get('initial') or '').strip()
    folder = native_dialog.pick_folder(initial=initial or None,
                                       title='Select install folder')
    return JsonResponse({'path': folder or ''})


@require_POST
def pick_file_dialog(request):
    initial = (request.POST.get('initial') or '').strip()
    path = native_dialog.pick_file(
        initial=initial or None,
        title='Select game executable',
        filetypes=native_dialog.EXECUTABLE_FILETYPES,
    )
    return JsonResponse({'path': path or ''})


@require_POST
def save_settings(request):
    settings_obj = AppSettings.load()
    settings_obj.dlsite_install_root = (request.POST.get('dlsite_install_root') or '').strip()
    settings_obj.f95zone_install_root = (request.POST.get('f95zone_install_root') or '').strip()
    settings_obj.dlsite_username = (request.POST.get('dlsite_username') or '').strip()
    password = request.POST.get('dlsite_password')
    if password is not None and password != '':
        settings_obj.dlsite_password = password
    locale = (request.POST.get('locale') or 'en_US').strip()
    settings_obj.locale = locale or 'en_US'
    settings_obj.save()
    if request.headers.get('HX-Request'):
        return HttpResponse(
            '<div class="toast toast-ok" x-init="setTimeout(()=>$el.remove(),2500)">Settings saved.</div>'
        )
    return redirect('library:settings')


# ---------------------------------------------------------------------------
# Public API (for browser extension)
#
# These endpoints expose game state to a browser extension that runs on
# f95zone.to / dlsite.com pages and overlays "downloaded" / "bad" pills.
# CSRF is exempted because requests come from a different origin (the extension);
# CORS headers are set globally for /api/v1/ in middleware.CorsMiddleware.
# ---------------------------------------------------------------------------

def _game_to_api(game: Game) -> dict:
    return {
        'found': True,
        'id': game.pk,
        'source': game.source,
        'source_id': game.source_id,
        'title': game.title,
        'url': game.dlsite_url,
        'is_installed': game.is_installed,
        'is_running': game.is_running,
        'is_bad': game.is_bad,
        'is_favorite': game.is_favorite,
        'status': game.status,
        'personal_rating': game.personal_rating,
        'time_played_seconds': game.time_played_seconds,
        'library_url': game.get_absolute_url(),
    }


def _resolve_game_by_source(source: str, source_id: str) -> Game | None:
    """Find a game by primary (source, source_id) or by any alias."""
    game = Game.objects.filter(source=source, source_id=source_id).first()
    if game:
        return game
    alias = (GameAlias.objects
             .filter(source=source, source_id=source_id)
             .select_related('game')
             .first())
    return alias.game if alias else None


@csrf_exempt
@require_GET
def api_lookup(request):
    url = (request.GET.get('url') or '').strip()
    if not url:
        return JsonResponse({'error': 'url query parameter is required'}, status=400)
    try:
        source, source_id = detect_source(url)
    except (ValueError, dlsite_client.DlsiteError, f95zone_client.F95ZoneError) as exc:
        return JsonResponse({'error': str(exc), 'input': url}, status=400)
    game = _resolve_game_by_source(source, source_id)
    if not game:
        return JsonResponse({
            'found': False,
            'source': source,
            'source_id': source_id,
            'input': url,
        })
    return JsonResponse(_game_to_api(game))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def api_lookup_bulk(request):
    if request.method == 'OPTIONS':
        return HttpResponse(status=204)
    try:
        body = json.loads(request.body or b'{}')
    except json.JSONDecodeError as exc:
        return JsonResponse({'error': f'invalid JSON: {exc}'}, status=400)
    urls = body.get('urls') if isinstance(body, dict) else None
    if not isinstance(urls, list):
        return JsonResponse({'error': 'JSON body must be {"urls": [...]}'}, status=400)

    by_key: dict[tuple[str, str], list[str]] = {}
    invalid: list[str] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        try:
            key = detect_source(url)
        except (ValueError, dlsite_client.DlsiteError, f95zone_client.F95ZoneError):
            invalid.append(url)
            continue
        by_key.setdefault(key, []).append(url)

    games: dict[tuple[str, str], Game] = {}
    if by_key:
        primary_q = Q()
        for source, source_id in by_key:
            primary_q |= Q(source=source, source_id=source_id)
        for g in Game.objects.filter(primary_q):
            games[(g.source, g.source_id)] = g
        # Anything not matched as a primary, try as an alias.
        unresolved = [k for k in by_key if k not in games]
        if unresolved:
            alias_q = Q()
            for source, source_id in unresolved:
                alias_q |= Q(source=source, source_id=source_id)
            for a in GameAlias.objects.filter(alias_q).select_related('game'):
                games[(a.source, a.source_id)] = a.game

    result: dict[str, dict] = {}
    for key, originals in by_key.items():
        source, source_id = key
        game = games.get(key)
        payload = _game_to_api(game) if game else {
            'found': False, 'source': source, 'source_id': source_id,
        }
        for u in originals:
            result[u] = payload
    for u in invalid:
        result[u] = {'found': False, 'error': 'unrecognized URL'}
    return JsonResponse({'results': result})


def _autoadd_game(source: str, source_id: str, url: str) -> Game:
    """Fetch metadata + create a Game row. Caller has already classified URL.

    Falls back to a minimal stub (just product_id as title) if metadata
    fetching fails for any reason — DLsite 404s for delisted / age-gated
    products, F95Zone Cloudflare-blocks without login, etc. The user can
    still mark / favorite / unfavorite the stub and hit Refresh metadata
    later when the source comes back online.
    """
    if source == Game.SOURCE_F95ZONE:
        # f95zone_client.fetch_thread already returns a slug-derived stub on
        # failure (with fetch_failed=True), so no extra try/except needed.
        data = f95zone_client.fetch_thread(url)
        canonical_url = data.get('url') or f95zone_client.thread_url(source_id)
        game = Game(
            source=source,
            source_id=source_id,
            title=data.get('title') or source_id,
            dlsite_url=canonical_url,
        )
        game.save()
        try:
            _apply_f95zone_metadata(game, data)
        except Exception:
            # Metadata enrichment is best-effort. The game row is already
            # persisted, so callers (api_upsert) can still apply user flags
            # like is_bad / is_favorite. Refresh-metadata will retry later.
            log.exception('F95Zone metadata enrichment failed for %s', source_id)
        return game

    settings_obj = AppSettings.load()
    canonical_url = (
        url if url.startswith('http')
        else f'https://www.dlsite.com/maniax/work/=/product_id/{source_id}.html'
    )
    try:
        data = dlsite_client.fetch_work(url, locale=settings_obj.locale or 'en_US')
    except Exception as exc:
        # DLsite often 404s on delisted / age-gated / region-locked items.
        # Don't fail the whole add — save a stub and let the user retry
        # metadata refresh later from the detail page.
        log.warning('DLsite fetch failed for %s (%s) — creating stub: %s',
                    source_id, url, exc)
        game = Game(
            source=source,
            source_id=source_id,
            title=source_id,  # placeholder until a successful refresh
            dlsite_url=canonical_url,
        )
        game.save()
        return game

    game = Game(
        source=source,
        source_id=source_id,
        title=data.get('work_name') or source_id,
        dlsite_url=canonical_url,
    )
    game.save()
    try:
        _apply_dlsite_metadata(game, data)
    except Exception:
        # Same rationale as above: the game row is committed, so don't let a
        # metadata hiccup (sample download, malformed date, tag normalization)
        # kill the whole upsert and force the user to click twice. The flags
        # the caller wants to set (is_bad, is_favorite) must still go through.
        log.exception('DLsite metadata enrichment failed for %s', source_id)
    return game


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def api_upsert(request):
    """Ensure a game exists for ``url``, then optionally update is_bad /
    status / personal_rating. Returns the resulting game payload.

    Body::

        { "url": "...",
          "is_bad": true | false,        // optional
          "status": "in_progress" | ...,  // optional
          "personal_rating": 1..10 | null // optional
        }
    """
    if request.method == 'OPTIONS':
        return HttpResponse(status=204)
    try:
        body = json.loads(request.body or b'{}')
    except json.JSONDecodeError as exc:
        return JsonResponse({'error': f'invalid JSON: {exc}'}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({'error': 'body must be a JSON object'}, status=400)

    url = (body.get('url') or '').strip()
    if not url:
        return JsonResponse({'error': 'url is required'}, status=400)

    try:
        source, source_id = detect_source(url)
    except (ValueError, dlsite_client.DlsiteError, f95zone_client.F95ZoneError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    game = _resolve_game_by_source(source, source_id)
    created = False
    if game is None:
        try:
            game = _autoadd_game(source, source_id, url)
            created = True
        except Exception as exc:
            log.exception('api_upsert auto-add failed for %s', url)
            # _autoadd_game's initial game.save() may already have committed
            # before the exception (e.g. metadata enrichment crashed). Re-query
            # — if the row is there, fall through and apply the caller's flags
            # so they don't have to click again.
            game = _resolve_game_by_source(source, source_id)
            if game is None:
                return JsonResponse({'error': f'auto-add failed: {exc}'}, status=502)
            created = True

    update_fields: list[str] = []
    if 'is_bad' in body:
        game.is_bad = bool(body['is_bad'])
        update_fields.append('is_bad')
    if 'is_favorite' in body:
        game.is_favorite = bool(body['is_favorite'])
        update_fields.append('is_favorite')
    if 'status' in body:
        s = body['status']
        if s in dict(Game.STATUS_CHOICES):
            game.status = s
            update_fields.append('status')
    if 'personal_rating' in body:
        raw = body['personal_rating']
        if raw in (None, 0, ''):
            game.personal_rating = None
        else:
            try:
                v = int(raw)
                if 1 <= v <= 10:
                    game.personal_rating = v
            except (TypeError, ValueError):
                pass
        update_fields.append('personal_rating')
    if update_fields:
        game.save(update_fields=update_fields)

    payload = _game_to_api(game)
    payload['created'] = created
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def api_link(request):
    """Make ``alias_url`` resolve to the same game as ``primary_url``.

    Body: ``{"primary_url": "...", "alias_url": "..."}``

    Primary must already exist in the library (or resolve via an existing
    alias). The alias's source+id pair is added as a GameAlias row. Subsequent
    lookups for ``alias_url`` return the primary game. No-op if the alias is
    already pointed at the same primary; 409 if it's pointed elsewhere.
    """
    if request.method == 'OPTIONS':
        return HttpResponse(status=204)
    try:
        body = json.loads(request.body or b'{}')
    except json.JSONDecodeError as exc:
        return JsonResponse({'error': f'invalid JSON: {exc}'}, status=400)
    primary_url = (body.get('primary_url') or '').strip()
    alias_url = (body.get('alias_url') or '').strip()
    if not primary_url or not alias_url:
        return JsonResponse({'error': 'both primary_url and alias_url required'}, status=400)

    try:
        primary_source, primary_id = detect_source(primary_url)
        alias_source, alias_id = detect_source(alias_url)
    except (ValueError, dlsite_client.DlsiteError, f95zone_client.F95ZoneError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    primary_game = _resolve_game_by_source(primary_source, primary_id)
    if primary_game is None:
        return JsonResponse({'error': 'primary game not in library'}, status=404)

    # No-op: alias is the primary itself.
    if alias_source == primary_game.source and alias_id == primary_game.source_id:
        return JsonResponse({'ok': True, 'noop': 'self', 'game_id': primary_game.pk})

    existing = _resolve_game_by_source(alias_source, alias_id)
    if existing is not None:
        if existing.pk == primary_game.pk:
            return JsonResponse({'ok': True, 'noop': 'already-linked',
                                 'game_id': primary_game.pk})
        return JsonResponse({
            'error': 'alias already linked to a different game',
            'existing_game_id': existing.pk,
        }, status=409)

    GameAlias.objects.create(
        game=primary_game,
        source=alias_source,
        source_id=alias_id,
    )
    # Bump updated_at so library page auto-refresh notices.
    primary_game.updated_at = timezone.now()
    primary_game.save(update_fields=['updated_at'])

    return JsonResponse({'ok': True, 'game_id': primary_game.pk, 'noop': False})


# ---------------------------------------------------------------------------
# Export / import: bundle DB + media + settings into a portable .dlib zip
# so the user can move a library between machines or back it up.
# ---------------------------------------------------------------------------

EXPORT_SCHEMA_VERSION = 1
EXPORT_APP_VERSION = '0.1.8'


def _stream_then_unlink(path: str):
    """Yield a file's bytes in chunks, then delete the file. Lets us write
    big exports to a tempfile and stream them out without holding the whole
    archive in RAM."""
    try:
        with open(path, 'rb') as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _write_export_zip(target_path: str) -> int:
    """Write the archive to a filesystem path. Returns its size in bytes."""
    with zipfile.ZipFile(target_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = {
            'schema_version': EXPORT_SCHEMA_VERSION,
            'app_version': EXPORT_APP_VERSION,
            'exported_at': timezone.now().isoformat(),
            'counts': {
                'games': Game.objects.count(),
                'tags': Tag.objects.count(),
                'creators': Creator.objects.count(),
                'play_sessions': sum(g.play_sessions.count() for g in Game.objects.all()),
                'aliases': GameAlias.objects.count(),
                'manual_tags': ManualGameTag.objects.count(),
            },
        }
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))

        data_buf = io.StringIO()
        call_command('dumpdata', 'library',
                     indent=2, stdout=data_buf,
                     use_natural_foreign_keys=False,
                     use_natural_primary_keys=False)
        zf.writestr('data.json', data_buf.getvalue())

        media_root = Path(django_settings.MEDIA_ROOT)
        if media_root.is_dir():
            for fp in media_root.rglob('*'):
                if fp.is_file():
                    rel = fp.relative_to(media_root)
                    zf.write(fp, f'media/{rel.as_posix()}')
    return os.path.getsize(target_path)


@require_POST
def export_data(request):
    """Bundle DB + media + settings into a ``.dlib`` archive.

    Two modes:
    * If ``path`` POST param is given, write the archive there and return
      a JSON status (used by the desktop UI which gets the path via the
      native Save As dialog).
    * Otherwise stream the archive back as an attachment download.
    """
    if process_tracker.active_games():
        return HttpResponse(
            'Cannot export while games are running — close them first.',
            status=409,
        )

    target = (request.POST.get('path') or '').strip()
    if target:
        try:
            parent = os.path.dirname(target) or '.'
            os.makedirs(parent, exist_ok=True)
            size = _write_export_zip(target)
        except (OSError, ValueError) as exc:
            log.exception('export to %s failed', target)
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        return JsonResponse({'ok': True, 'path': target, 'bytes': size})

    # Browser download fallback (pywebview struggles with these).
    tmp = tempfile.NamedTemporaryFile(suffix='.dlib', delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        size = _write_export_zip(tmp_path)
    except Exception:
        try: os.unlink(tmp_path)
        except OSError: pass
        log.exception('export_data failed')
        return HttpResponse('Export failed — see dlib.log.', status=500)

    filename = f'DLib-export-{timezone.now().strftime("%Y%m%d-%H%M%S")}.dlib'
    response = StreamingHttpResponse(
        _stream_then_unlink(tmp_path),
        content_type='application/octet-stream',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['Content-Length'] = str(size)
    return response


@require_POST
def import_data(request):
    """Wipe + replace library state from an uploaded ``.dlib`` bundle.

    DESTRUCTIVE — drops all Games, Tags, Creators, GameAlias, ManualGameTag,
    PlaySession, AppSettings rows and the media/ tree before extracting the
    archive's contents. The form on the settings page wraps it in a
    confirm() to make sure the user means it.
    """
    upload = request.FILES.get('file')
    if upload is None:
        return HttpResponse('No file uploaded (expected field "file").', status=400)

    if process_tracker.active_games():
        return HttpResponse(
            'Cannot import while games are running — close them first.',
            status=409,
        )

    try:
        with zipfile.ZipFile(upload) as zf:
            try:
                manifest = json.loads(zf.read('manifest.json').decode('utf-8'))
            except (KeyError, json.JSONDecodeError) as exc:
                return HttpResponse(
                    f'Not a valid DLib export (manifest.json missing or invalid: {exc}).',
                    status=400,
                )

            version = manifest.get('schema_version')
            if version != EXPORT_SCHEMA_VERSION:
                return HttpResponse(
                    f'Export schema version {version} is incompatible with this '
                    f'DLib (expected {EXPORT_SCHEMA_VERSION}).',
                    status=400,
                )

            try:
                data_json = zf.read('data.json').decode('utf-8')
            except KeyError:
                return HttpResponse('Export missing data.json.', status=400)

            # Wipe existing tables in a single transaction. AppSettings is a
            # singleton — delete + reload so the imported one wins.
            with transaction.atomic():
                PlaySession.objects.all().delete()
                ManualGameTag.objects.all().delete()
                GameAlias.objects.all().delete()
                Game.objects.all().delete()
                Tag.objects.all().delete()
                Creator.objects.all().delete()
                AppSettings.objects.all().delete()

                for obj in serializers.deserialize('json', data_json):
                    obj.save()

            # Replace media tree with the archive's media/.
            media_root = Path(django_settings.MEDIA_ROOT)
            if media_root.is_dir():
                for sub in ('covers', 'samples'):
                    target = media_root / sub
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
            media_root.mkdir(parents=True, exist_ok=True)

            for name in zf.namelist():
                if not name.startswith('media/') or name.endswith('/'):
                    continue
                rel = name[len('media/'):]
                if not rel:
                    continue
                dst = media_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(dst, 'wb') as out:
                    shutil.copyfileobj(src, out)

    except zipfile.BadZipFile:
        return HttpResponse('Not a valid .dlib archive (bad zip).', status=400)
    except Exception:
        log.exception('import_data failed')
        return HttpResponse('Import failed — see dlib.log.', status=500)

    if request.headers.get('HX-Request'):
        return HttpResponse(
            '<div class="toast toast-ok" '
            'x-init="setTimeout(()=>$el.remove(),3500)">Import complete.</div>'
        )
    return redirect('library:settings')


@csrf_exempt
@require_GET
def api_health(request):
    return JsonResponse({
        'ok': True,
        'app': 'DLib',
        'version': '0.1.8',
        'sources': [Game.SOURCE_DLSITE, Game.SOURCE_F95ZONE],
        'games': Game.objects.count(),
    })


@require_GET
def api_changes(request):
    """Cheap version token used by the library page poller.

    Returns counts + the most recent updated_at/added_at timestamps. The
    library page compares the JSON string to its last-seen value; if it
    differs it kicks an HTMX grid refresh. No grid data fetched on each
    poll, just this small payload.
    """
    from django.db.models import Count, Max
    agg = Game.objects.aggregate(
        count=Count('id'),
        updated=Max('updated_at'),
        added=Max('added_at'),
    )
    return JsonResponse({
        'count': agg['count'] or 0,
        'updated': agg['updated'].isoformat() if agg['updated'] else '',
        'added': agg['added'].isoformat() if agg['added'] else '',
    })
