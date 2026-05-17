"""Native OS dialogs (tkinter) for picking folders and files.

Each call spins up a transient Tk root, runs a single dialog, and destroys the
root so we don't leak Tk instances. We run the dialog on the request thread —
fine on Windows when Django is serving locally with the dev server.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Iterable

log = logging.getLogger(__name__)


def _make_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    root.update()
    return root


def pick_folder(initial: str | None = None, title: str = 'Select folder') -> str | None:
    try:
        from tkinter import filedialog
        root = _make_root()
    except Exception as exc:  # pragma: no cover - depends on display
        log.warning('tkinter unavailable: %s', exc)
        return None
    try:
        kwargs = {'title': title}
        if initial and os.path.isdir(initial):
            kwargs['initialdir'] = initial
        path = filedialog.askdirectory(**kwargs)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return path or None


def pick_file(
    initial: str | None = None,
    title: str = 'Select file',
    filetypes: Iterable[tuple[str, str]] | None = None,
) -> str | None:
    try:
        from tkinter import filedialog
        root = _make_root()
    except Exception as exc:  # pragma: no cover
        log.warning('tkinter unavailable: %s', exc)
        return None
    try:
        kwargs = {'title': title}
        if initial:
            if os.path.isdir(initial):
                kwargs['initialdir'] = initial
            elif os.path.isfile(initial):
                kwargs['initialdir'] = os.path.dirname(initial)
                kwargs['initialfile'] = os.path.basename(initial)
        if filetypes:
            kwargs['filetypes'] = list(filetypes)
        path = filedialog.askopenfilename(**kwargs)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return path or None


EXECUTABLE_FILETYPES = (
    ('Executable', '*.exe' if sys.platform == 'win32' else '*'),
    ('All files', '*.*'),
)
