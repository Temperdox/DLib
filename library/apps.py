from django.apps import AppConfig


class LibraryConfig(AppConfig):
    name = 'library'

    def ready(self) -> None:
        # Register model signal handlers that publish to event_bus.
        from . import signals  # noqa: F401
