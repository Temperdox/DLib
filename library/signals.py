"""Model signal handlers that publish UI sync events to the event bus.

Wired in ``library/apps.py``'s ``ready()`` hook. Any code path that
``save()`` / ``delete()`` a Game (extension API, HTMX view, management
command, admin, etc.) triggers an event without callers needing to
remember to publish manually.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from library.models import (
    Game,
    GameAlias,
    ManualGameTag,
    PlaySession,
    Tag,
)
from library.services import event_bus

log = logging.getLogger(__name__)


def _game_payload(game: Game) -> dict:
    """Minimal payload — clients fetch full state via existing partials.
    We include enough to let the client decide whether the event is
    relevant (which game, which source) without an extra round trip."""
    return {
        'game_id': game.pk,
        'source': game.source,
        'source_id': game.source_id,
        'is_bad': game.is_bad,
        'is_favorite': game.is_favorite,
    }


@receiver(post_save, sender=Game)
def _on_game_saved(sender, instance: Game, created: bool, **kwargs) -> None:
    event_bus.publish(
        'game.added' if created else 'game.changed',
        _game_payload(instance),
    )


@receiver(post_delete, sender=Game)
def _on_game_deleted(sender, instance: Game, **kwargs) -> None:
    event_bus.publish('game.deleted', {
        'game_id': instance.pk,
        'source': instance.source,
        'source_id': instance.source_id,
    })


@receiver([post_save, post_delete], sender=GameAlias)
def _on_alias_changed(sender, instance: GameAlias, **kwargs) -> None:
    # Aliases affect lookup results in the extension and downloaded
    # state in the library — treat as a change to the canonical game.
    if instance.game_id:
        event_bus.publish('game.changed', {
            'game_id': instance.game_id,
            'source': instance.game.source,
            'source_id': instance.game.source_id,
        })


@receiver([post_save, post_delete], sender=Tag)
def _on_tag_changed(sender, instance: Tag, **kwargs) -> None:
    # Tag renames / deletions affect the autocomplete and any card
    # showing that tag — broadcast a generic "library changed" tick.
    event_bus.publish('tags.changed', {'tag_id': instance.pk})


@receiver([post_save, post_delete], sender=ManualGameTag)
def _on_manual_tag_changed(sender, instance: ManualGameTag, **kwargs) -> None:
    if instance.game_id:
        event_bus.publish('game.changed', {'game_id': instance.game_id})


@receiver([post_save, post_delete], sender=PlaySession)
def _on_play_session_changed(sender, instance: PlaySession, **kwargs) -> None:
    if instance.game_id:
        event_bus.publish('game.changed', {'game_id': instance.game_id})
