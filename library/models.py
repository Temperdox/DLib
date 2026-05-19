from __future__ import annotations

from pathlib import Path

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse


class Tag(models.Model):
    GENRE = 'genre'
    TAG = 'tag'
    CATEGORY_CHOICES = [(GENRE, 'Genre'), (TAG, 'Tag')]

    name = models.CharField(max_length=120)
    category = models.CharField(max_length=8, choices=CATEGORY_CHOICES, default=TAG)

    class Meta:
        unique_together = [('name', 'category')]
        ordering = ['category', 'name']

    def __str__(self) -> str:
        return self.name


class Creator(models.Model):
    name = models.CharField(max_length=200, unique=True)
    dlsite_maker_id = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return self.name


class Game(models.Model):
    STATUS_UNKNOWN = 'unknown'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETED = 'completed'
    STATUS_DROPPED = 'dropped'
    STATUS_CHOICES = [
        (STATUS_UNKNOWN, 'Unknown'),
        (STATUS_IN_PROGRESS, 'In Progress'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_DROPPED, 'Dropped'),
    ]

    AGE_ALL = 'all_ages'
    AGE_R15 = 'r15'
    AGE_R18 = 'r18'
    AGE_CHOICES = [(AGE_ALL, 'All ages'), (AGE_R15, 'R15'), (AGE_R18, 'R18')]

    SOURCE_DLSITE = 'dlsite'
    SOURCE_F95ZONE = 'f95zone'
    SOURCE_MANUAL = 'manual'
    SOURCE_CHOICES = [
        (SOURCE_DLSITE, 'DLsite'),
        (SOURCE_F95ZONE, 'F95Zone'),
        (SOURCE_MANUAL, 'Manual'),
    ]

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_DLSITE)
    source_id = models.CharField(max_length=40)
    site_id = models.CharField(max_length=40, blank=True)
    title = models.CharField(max_length=500)
    title_masked = models.CharField(max_length=500, blank=True)
    circle = models.ForeignKey(Creator, on_delete=models.SET_NULL, null=True, blank=True, related_name='games')
    work_type = models.CharField(max_length=40, blank=True)
    age_category = models.CharField(max_length=10, choices=AGE_CHOICES, default=AGE_ALL)

    description = models.TextField(blank=True)
    cover_image = models.ImageField(upload_to='covers/', blank=True, null=True)
    cover_url = models.URLField(max_length=600, blank=True)
    dlsite_url = models.URLField(max_length=600)

    scenario = models.JSONField(default=list, blank=True)
    illustration = models.JSONField(default=list, blank=True)
    voice_actor = models.JSONField(default=list, blank=True)
    music = models.JSONField(default=list, blank=True)
    sample_images = models.JSONField(default=list, blank=True)
    # Local relative paths (under MEDIA_ROOT) for downloaded sample images.
    gallery_images = models.JSONField(default=list, blank=True)
    # F95Zone-style download host links scraped from OP body
    # ([{label, host, url}, ...]). DLsite has its own single product URL.
    download_links = models.JSONField(default=list, blank=True)
    file_format = models.JSONField(default=list, blank=True)
    file_size = models.CharField(max_length=80, blank=True)
    language = models.JSONField(default=list, blank=True)

    tags = models.ManyToManyField(Tag, blank=True, related_name='games')

    install_folder = models.CharField(max_length=1000, blank=True)
    executable_path = models.CharField(max_length=1000, blank=True)
    launch_with_mtool = models.BooleanField(default=False)

    personal_rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_UNKNOWN)
    is_bad = models.BooleanField(default=False)
    is_favorite = models.BooleanField(default=False)
    time_played_seconds = models.PositiveIntegerField(default=0)
    last_played_at = models.DateTimeField(null=True, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)
    # Bumped on every save; used by the library page's auto-refresh poller
    # so external changes (e.g. browser extension marking a game bad) show
    # up without a manual reload.
    updated_at = models.DateTimeField(auto_now=True)
    metadata_refreshed_at = models.DateTimeField(null=True, blank=True)
    regist_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-added_at']
        unique_together = [('source', 'source_id')]

    def __str__(self) -> str:
        return f'{self.source}:{self.source_id} — {self.title}'

    # Back-compat alias for code/templates that still read `dlsite_id`.
    @property
    def dlsite_id(self) -> str:
        return self.source_id

    def get_absolute_url(self) -> str:
        return reverse('library:game_detail', args=[self.pk])

    @property
    def is_installed(self) -> bool:
        if not self.executable_path:
            return False
        try:
            return Path(self.executable_path).is_file()
        except OSError:
            return False

    @property
    def is_html_game(self) -> bool:
        """The 'executable' is an HTML file — launch in a pywebview sub-window."""
        if not self.executable_path:
            return False
        return self.executable_path.lower().endswith(('.html', '.htm'))

    @property
    def is_running(self) -> bool:
        # Lazy import to avoid a circular dependency: process_tracker imports
        # PlaySession from this module inside its worker thread.
        from library.services.process_tracker import is_running as _is_running
        return _is_running(self.pk)

    @property
    def time_played_display(self) -> str:
        total = int(self.time_played_seconds)
        if total < 60:
            return f'{total}s'
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        if hours:
            return f'{hours}h {minutes}m'
        return f'{minutes}m'


class ManualGameTag(models.Model):
    """Tracks which (game, tag) pairs the user explicitly added themselves.

    Used to:
      * Preserve manual tags when auto metadata is refreshed (otherwise
        the refresh's ``game.tags.set(...)`` would wipe them).
      * Surface the most-frequently-added manual tags as default suggestions
        when the user opens the autocomplete on another game.
    """
    game = models.ForeignKey('Game', on_delete=models.CASCADE,
                              related_name='manual_tag_set')
    tag = models.ForeignKey('Tag', on_delete=models.CASCADE,
                             related_name='manual_uses')
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('game', 'tag')]
        ordering = ['-added_at']

    def __str__(self) -> str:
        return f'manual: game#{self.game_id} -> tag#{self.tag_id}'


class GameAlias(models.Model):
    """Cross-source identity: one canonical Game can claim other source+id
    pairs as aliases. Lookups for an alias resolve to its canonical game so
    e.g. the same title's DLsite product page AND its F95Zone thread show
    the same downloaded/bad state in the browser extension.
    """
    game = models.ForeignKey('Game', related_name='alias_set', on_delete=models.CASCADE)
    source = models.CharField(max_length=20, choices=Game.SOURCE_CHOICES)
    source_id = models.CharField(max_length=40)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('source', 'source_id')]
        indexes = [models.Index(fields=['source', 'source_id'])]

    def __str__(self) -> str:
        return f'{self.source}:{self.source_id} -> game#{self.game_id}'


class PlaySession(models.Model):
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='play_sessions')
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    pid = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']


class AppSettings(models.Model):
    """Singleton row holding application-wide settings."""
    SINGLETON_ID = 1

    dlsite_install_root = models.CharField(max_length=1000, blank=True)
    f95zone_install_root = models.CharField(max_length=1000, blank=True)
    manual_install_root = models.CharField(max_length=1000, blank=True)
    dlsite_username = models.CharField(max_length=200, blank=True)
    dlsite_password = models.CharField(max_length=200, blank=True)
    locale = models.CharField(max_length=10, default='en_US')
    mtool_path = models.CharField(max_length=1000, blank=True)
    # F95Zone session captured by JsApi.start_f95_login() so we can punch
    # through Cloudflare. Includes xf_user / xf_session / cf_clearance.
    f95zone_cookies = models.JSONField(default=dict, blank=True)
    # The UA used at login time — cf_clearance is UA-bound, must match.
    f95zone_user_agent = models.CharField(max_length=500, blank=True)

    def install_root_for(self, source: str) -> str:
        """Returns the raw configured value (may be empty)."""
        if source == Game.SOURCE_F95ZONE:
            return self.f95zone_install_root
        if source == Game.SOURCE_MANUAL:
            return self.manual_install_root
        return self.dlsite_install_root

    def effective_install_root_for(self, source: str) -> str:
        """Configured root if set + valid, else a cross-platform default
        rooted in the user's home directory (``~/DLib/Games/<source>``).
        The default directory is created lazily so the native file picker
        has a real path to open at. Returning a path that doesn't exist
        is fine on its own — but several picker backends (WebView2 on
        Windows in particular) silently fall back to "Downloads" if the
        directory argument doesn't resolve, which is exactly what we're
        trying to avoid.
        """
        configured = (self.install_root_for(source) or '').strip()
        if configured and Path(configured).is_dir():
            return configured
        sub = {
            Game.SOURCE_F95ZONE: 'F95Zone',
            Game.SOURCE_MANUAL: 'Manual',
        }.get(source, 'DLsite')
        default = Path.home() / 'DLib' / 'Games' / sub
        try:
            default.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Locked-down env (no write to home, sandbox, etc.) — fall back
            # to plain home, which is guaranteed to exist.
            return str(Path.home())
        return str(default)

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_ID
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> 'AppSettings':
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_ID)
        return obj
