"""Splash screen — instant-load HTML pywebview shows before Django is ready.

Reads stats directly from the sqlite DB via stdlib `sqlite3` so we don't have
to wait for Django's full app loading on every cold start.
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path


def _fmt_hm(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f'{h}h {m}m'
    if h:
        return f'{h}h'
    return f'{m}m' if m else f'{seconds}s'


def compute_stats(db_path: Path) -> list[str]:
    """Return a list of short stat strings to cycle through on the splash."""
    if not db_path.exists():
        return _welcome_stats()

    stats: list[str] = []
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2.0)
    except sqlite3.Error:
        return _welcome_stats()

    try:
        cur = conn.cursor()

        try:
            (n,) = cur.execute("SELECT COUNT(*) FROM library_game").fetchone()
        except sqlite3.OperationalError:
            return _welcome_stats()

        if not n:
            return _welcome_stats()

        stats.append(f"{n} game{'s' if n != 1 else ''} in your library")

        (d,) = cur.execute(
            "SELECT COUNT(*) FROM library_game WHERE executable_path != ''"
        ).fetchone()
        if d:
            stats.append(f"{d} downloaded")

        (p,) = cur.execute(
            "SELECT COUNT(*) FROM library_game WHERE time_played_seconds > 0"
        ).fetchone()
        if p:
            stats.append(f"You've played {p} game{'s' if p != 1 else ''}")

        (total,) = cur.execute(
            "SELECT COALESCE(SUM(time_played_seconds), 0) FROM library_game"
        ).fetchone()
        if total:
            stats.append(f"Total play time: {_fmt_hm(total)}")

        row = cur.execute(
            "SELECT title, time_played_seconds FROM library_game "
            "WHERE time_played_seconds > 0 "
            "ORDER BY time_played_seconds DESC LIMIT 1"
        ).fetchone()
        if row:
            title, secs = row
            stats.append(f"Most played: {title} ({_fmt_hm(secs)})")

        row = cur.execute(
            "SELECT g.title FROM library_game g "
            "JOIN library_playsession s ON s.game_id = g.id "
            "ORDER BY s.started_at DESC LIMIT 1"
        ).fetchone()
        if row:
            stats.append(f"Last played: {row[0]}")

        row = cur.execute(
            "SELECT title, personal_rating FROM library_game "
            "WHERE personal_rating IS NOT NULL "
            "ORDER BY personal_rating DESC, last_played_at DESC LIMIT 1"
        ).fetchone()
        if row:
            title, rating = row
            stats.append(f"Highest rated: {title} ({'★' * int(rating)})")

        # Favorite tag by total time played
        row = cur.execute(
            """
            SELECT t.name, SUM(g.time_played_seconds) AS total
            FROM library_game g
            JOIN library_game_tags gt ON gt.game_id = g.id
            JOIN library_tag t ON t.id = gt.tag_id
            WHERE g.time_played_seconds > 0
            GROUP BY t.name
            ORDER BY total DESC
            LIMIT 1
            """
        ).fetchone()
        if row:
            name, secs = row
            stats.append(f"Favorite tag: {name} ({_fmt_hm(secs)} played)")

        # Favorite creator by total time played
        row = cur.execute(
            """
            SELECT c.name, SUM(g.time_played_seconds) AS total
            FROM library_game g
            JOIN library_creator c ON c.id = g.circle_id
            WHERE g.time_played_seconds > 0
            GROUP BY c.name
            ORDER BY total DESC
            LIMIT 1
            """
        ).fetchone()
        if row:
            name, secs = row
            stats.append(f"Favorite creator: {name} ({_fmt_hm(secs)} played)")

        row = cur.execute(
            "SELECT title FROM library_game ORDER BY added_at DESC LIMIT 1"
        ).fetchone()
        if row:
            stats.append(f"Newest add: {row[0]}")
    finally:
        conn.close()

    return stats or _welcome_stats()


def _welcome_stats() -> list[str]:
    return [
        "Welcome to DLib",
        "Click + Add Game to start your library",
        "Paste a DLsite URL or product ID (RJ12345, BJ12345...)",
    ]


SPLASH_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DLib</title>
<style>
    *{box-sizing:border-box}
    html,body{margin:0;padding:0;height:100%;background:#15171b;color:#e6e8eb;
        font-family:'Inter','Segoe UI',system-ui,-apple-system,sans-serif;
        overflow:hidden;user-select:none}
    .splash{display:flex;flex-direction:column;align-items:center;justify-content:center;
        height:100vh;text-align:center;padding:24px;
        background:
            radial-gradient(1200px 600px at 50% -200px, rgba(240,72,72,0.16), transparent 60%),
            radial-gradient(800px 500px at 50% 110%, rgba(80,180,255,0.05), transparent 60%),
            #15171b}
    .brand{display:flex;align-items:center;gap:14px;font-size:42px;font-weight:800;
        letter-spacing:1.5px;margin-bottom:34px}
    .brand .dot{width:18px;height:18px;border-radius:50%;background:#f04848;
        box-shadow:0 0 18px rgba(240,72,72,0.85);animation:pulse 2s infinite ease-in-out}
    @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.55;transform:scale(0.9)}}
    .stat{font-size:18px;color:#cfd4dc;min-height:30px;max-width:760px;
        opacity:0;transform:translateY(6px);
        transition:opacity 0.6s ease, transform 0.6s ease}
    .stat.show{opacity:1;transform:translateY(0)}
    .loader{margin-top:42px;width:42px;height:42px;border-radius:50%;
        border:3px solid rgba(154,162,175,0.25);border-top-color:#f04848;
        animation:spin 1s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    .loading-text{margin-top:14px;font-size:13px;color:#6c7380;
        text-transform:uppercase;letter-spacing:2px}
    .version{position:fixed;bottom:14px;right:18px;
        font-size:11px;color:#6c7380;letter-spacing:0.5px}
</style>
</head>
<body>
<div class="splash">
    <div class="brand"><span class="dot"></span><span>DLib</span></div>
    <div id="stat" class="stat"></div>
    <div class="loader"></div>
    <div class="loading-text">Loading library…</div>
</div>
<div class="version">v__VERSION__</div>
<script>
    var stats = __STATS_JSON__;
    var el = document.getElementById('stat');
    var i = 0;
    function show() {
        if (!stats.length) return;
        el.classList.remove('show');
        setTimeout(function () {
            el.textContent = stats[i % stats.length];
            el.classList.add('show');
            i++;
        }, 300);
    }
    show();
    setInterval(show, 2800);
</script>
</body>
</html>
"""


def render_splash(stats: list[str], version: str = '0.1.8') -> str:
    return (
        SPLASH_HTML_TEMPLATE
        .replace('__STATS_JSON__', json.dumps(stats))
        .replace('__VERSION__', html.escape(version))
    )
