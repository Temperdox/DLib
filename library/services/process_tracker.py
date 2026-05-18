"""Launch games and track their playtime in a background daemon thread.

Design notes:

* One daemon thread per active game launch.
* The thread calls ``psutil.Process.wait()`` (blocking, kernel-driven — NOT a
  polling sleep loop) so it returns the moment the game process exits.
  When ``wait()`` returns the thread immediately finalizes the session and
  exits — guaranteeing no zombie threads.
* An ``atexit`` hook gracefully ends any sessions still open when Django
  shuts down (e.g. dev-server reload) so the database isn't left with
  orphaned in-flight PlaySession rows.
* A module-level dict + lock prevents a duplicate tracker from being spawned
  if the user clicks Play twice in quick succession.
"""
from __future__ import annotations

import atexit
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
from django.utils import timezone

log = logging.getLogger(__name__)


class LaunchError(Exception):
    pass


@dataclass
class TrackerHandle:
    game_id: int
    pid: int
    session_id: int
    started_at: float
    thread: threading.Thread | None  # patched in after the Thread is created


_active: dict[int, TrackerHandle] = {}
_lock = threading.Lock()


def is_running(game_id: int) -> bool:
    with _lock:
        handle = _active.get(game_id)
    if handle is None:
        return False
    return psutil.pid_exists(handle.pid)


def active_games() -> list[int]:
    with _lock:
        return list(_active.keys())


def _finalize(game_id: int, session_id: int, elapsed_seconds: int) -> None:
    """Write the elapsed time back to the DB. Imported lazily to avoid app-loading issues."""
    from django.db.models import F

    from library.models import Game, PlaySession

    now = timezone.now()
    PlaySession.objects.filter(pk=session_id).update(
        ended_at=now,
        duration_seconds=elapsed_seconds,
    )
    Game.objects.filter(pk=game_id).update(
        time_played_seconds=F('time_played_seconds') + elapsed_seconds,
        last_played_at=now,
    )


def _find_pid_by_exe(target_exe: str, exclude_pids: set[int] = frozenset()) -> int | None:
    """Find the first running process whose exe path resolves to ``target_exe``."""
    try:
        target = str(Path(target_exe).resolve()).lower()
    except (OSError, ValueError):
        return None
    for proc in psutil.process_iter(['pid', 'exe']):
        try:
            pid = proc.info['pid']
            exe = proc.info['exe']
            if pid in exclude_pids or not exe:
                continue
            if str(Path(exe).resolve()).lower() == target:
                return pid
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return None


def _reattach_to_real_game(game_id: int, launched_pid: int, track_exe: str,
                           initial_handle: TrackerHandle) -> int:
    """When the launched process (e.g. StartWithTool.bat) is a short-lived
    wrapper that spawns mtool which spawns the game, the bat exits in
    milliseconds. We poll for up to ~20s to find the real game.exe by path
    and re-target the tracker to that pid.

    Returns the pid we should ultimately wait on (the original ``launched_pid``
    if nothing better was found).
    """
    deadline = time.monotonic() + 20.0
    found_pid: int | None = None
    while time.monotonic() < deadline:
        found_pid = _find_pid_by_exe(track_exe, exclude_pids={launched_pid})
        if found_pid:
            break
        time.sleep(0.4)

    if found_pid is None:
        return launched_pid

    # Update _active so Game.is_running reflects the real game pid.
    with _lock:
        current = _active.get(game_id)
        if current is initial_handle:
            _active[game_id] = TrackerHandle(
                game_id=current.game_id,
                pid=found_pid,
                session_id=current.session_id,
                started_at=current.started_at,
                thread=current.thread,
            )
    log.info('re-attached tracker for game %s: %s -> %s (%s)',
             game_id, launched_pid, found_pid, track_exe)
    return found_pid


def _watch(game_id: int, pid: int, session_id: int, started_at: float,
           track_exe: str | None = None,
           handle: TrackerHandle | None = None) -> None:
    """Block on psutil.wait() until the game exits, then finalize the session.

    If ``track_exe`` is provided, after the initial process exits (typical for
    StartWithTool.bat which immediately spawns mtool and dies), we scan for a
    running process matching that exe path and wait on IT instead — that's
    the real game lifetime.
    """
    target_pid = pid
    try:
        try:
            # Wait on the launched process first. For mtool: this is the bat
            # which exits almost immediately. For direct launches: this IS
            # the game process and the wait blocks for the full session.
            psutil.Process(pid).wait()
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:
            log.exception('tracker wait() failed for game %s pid %s: %s', game_id, pid, exc)

        if track_exe and handle is not None:
            real_pid = _reattach_to_real_game(game_id, pid, track_exe, handle)
            if real_pid != pid:
                target_pid = real_pid
                try:
                    psutil.Process(real_pid).wait()
                except psutil.NoSuchProcess:
                    pass
                except Exception:
                    log.exception('tracker re-wait failed for game %s pid %s',
                                  game_id, real_pid)

        elapsed = int(time.monotonic() - started_at)
        try:
            _finalize(game_id, session_id, elapsed)
        except Exception:
            log.exception('failed to finalize session %s for game %s', session_id, game_id)
    finally:
        with _lock:
            current = _active.get(game_id)
            if current is not None and current.pid in (pid, target_pid):
                _active.pop(game_id, None)


def launch_and_track(game, exe_override: str | None = None,
                     track_exe: str | None = None) -> TrackerHandle:
    """Launch a game and start a tracker thread.

    Parameters:
        exe_override:  alternate executable to run instead of game.executable_path
                       (e.g. a StartWithTool.bat that wraps the real launch).
        track_exe:     if the spawned process is just a launcher that exits
                       quickly, after it dies we poll for a process whose
                       exe path matches this value and wait on THAT instead.
                       Typically set to the real game.executable_path.
    """
    from library.models import PlaySession

    if not game.executable_path:
        raise LaunchError('No executable is set for this game.')
    real_exe = Path(game.executable_path)
    if not real_exe.is_file():
        raise LaunchError(f'Executable not found: {real_exe}')

    launch_target = Path(exe_override) if exe_override else real_exe
    if not launch_target.is_file():
        raise LaunchError(f'Launcher not found: {launch_target}')

    with _lock:
        existing = _active.get(game.pk)
        if existing is not None and psutil.pid_exists(existing.pid):
            raise LaunchError('Game is already running.')

    # Run from the real game's folder so relative paths in the bat / game
    # resolve correctly.
    cwd = str(real_exe.parent)
    creationflags = 0
    if sys.platform == 'win32':
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    suffix = launch_target.suffix.lower()
    if sys.platform == 'win32':
        # Windows shell scripts must go through cmd.exe.
        cmd = [str(launch_target)]
        use_shell = suffix in {'.bat', '.cmd'}
    else:
        # POSIX: route .sh through /bin/sh explicitly so launchers without
        # the executable bit (or without a shebang) still run. Other files
        # are executed directly — they're either +x ELF binaries or Wine
        # wrappers the user set up themselves.
        use_shell = False
        if suffix == '.sh':
            cmd = ['/bin/sh', str(launch_target)]
        else:
            cmd = [str(launch_target)]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            close_fds=True,
            creationflags=creationflags,
            shell=use_shell,
        )
    except OSError as exc:
        raise LaunchError(f'Failed to start game: {exc}') from exc

    started_at = time.monotonic()
    session = PlaySession.objects.create(
        game=game,
        started_at=timezone.now(),
        pid=proc.pid,
    )

    # Create a mutable handle first so both _active and the watcher thread
    # reference the SAME object — _reattach_to_real_game uses `is`-identity
    # to confirm it's safe to swap in the re-discovered pid.
    handle = TrackerHandle(
        game_id=game.pk,
        pid=proc.pid,
        session_id=session.pk,
        started_at=started_at,
        thread=None,  # patched in once the Thread exists
    )
    thread = threading.Thread(
        target=_watch,
        args=(game.pk, proc.pid, session.pk, started_at,
              str(real_exe) if track_exe else None,
              handle),
        name=f'GameTracker-{game.pk}-{proc.pid}',
        daemon=True,
    )
    handle.thread = thread
    with _lock:
        _active[game.pk] = handle
    thread.start()
    log.info('launched game %s pid %s session %s (override=%s, track=%s)',
             game.pk, proc.pid, session.pk, exe_override, track_exe)
    return handle


def _shutdown_cleanup() -> None:
    """Close any open sessions when Django shuts down."""
    try:
        from library.models import Game, PlaySession  # noqa: F401
    except Exception:
        return

    with _lock:
        handles = list(_active.values())
        _active.clear()

    for handle in handles:
        elapsed = int(time.monotonic() - handle.started_at)
        try:
            _finalize(handle.game_id, handle.session_id, elapsed)
        except Exception:
            log.exception('shutdown finalize failed for game %s', handle.game_id)


atexit.register(_shutdown_cleanup)
