"""CLI entry point for sc (SoundCloud CLI)."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

import click
import requests
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .api import SoundCloudClient

console = Console()
_api = SoundCloudClient()

_HISTORY_FILE = pathlib.Path.home() / ".local" / "share" / "sc_cli" / "history.json"
_HISTORY_MAX  = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(ms: int) -> str:
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_count(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _record_history(track: dict) -> None:
    entry = {
        "title":       track.get("title", ""),
        "artist":      track.get("user", {}).get("username", ""),
        "url":         track.get("permalink_url", ""),
        "duration_ms": track.get("duration") or 0,
        "played_at":   datetime.now(timezone.utc).isoformat(),
    }
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        history: list = json.loads(_HISTORY_FILE.read_text()) if _HISTORY_FILE.exists() else []
    except (json.JSONDecodeError, OSError):
        history = []
    history.append(entry)
    if len(history) > _HISTORY_MAX:
        history = history[-_HISTORY_MAX:]
    try:
        _HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except OSError:
        pass


def _play_track(track: dict) -> str | None:
    """Fetch stream URL for *track* and hand off to the player.

    Returns ``'quit'`` if the user chose to exit, otherwise ``None``.
    """
    from . import player as _player

    title    = track.get("title", "")
    artist   = track.get("user", {}).get("username", "")
    duration = _fmt_duration(track.get("duration") or 0)

    console.print(
        f"[bold green]\u25b6[/bold green] [bold]{title}[/bold] "
        f"[cyan]\u2014 {artist}[/cyan]  [dim]{duration}[/dim]"
    )

    try:
        with console.status("Fetching stream URL \u2026"):
            stream_url = _api.get_stream_url(track)
    except Exception as exc:
        console.print(f"[red]Could not fetch stream URL:[/red] {exc}")
        return None

    if not stream_url:
        console.print("[red]No stream URL available for this track.[/red]")
        return None

    _record_history(track)
    return _player.play(stream_url, title=f"{title} \u2014 {artist}", duration_ms=track.get("duration") or 0)


def _handle_api_error(exc: Exception, url: str | None = None) -> None:
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code
        if status == 404:
            msg = "[red]Not found (404).[/red] The URL does not exist on SoundCloud."
            if url:
                msg += f"\n  Tried: [dim]{url}[/dim]"
            msg += "\n  Tip: use [bold]sc search[/bold] to find the correct URL."
            console.print(msg)
        else:
            console.print(f"[red]API error:[/red] {status} {exc.response.reason}")
    elif isinstance(exc, requests.ConnectionError):
        console.print("[red]Network error:[/red] Could not reach SoundCloud.")
    elif isinstance(exc, RuntimeError):
        console.print(f"[red]Error:[/red] {exc}")
    else:
        console.print(f"[red]Unexpected error:[/red] {exc}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="sc-cli")
def cli() -> None:
    """SoundCloud CLI — search, stream, download, and inspect SoundCloud content."""


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query")
@click.option(
    "--type", "-t", "kind",
    type=click.Choice(["tracks", "users", "playlists"]),
    default="tracks",
    show_default=True,
    help="Kind of results to return.",
)
@click.option(
    "--limit", "-n",
    default=10,
    show_default=True,
    metavar="N",
    help="Maximum number of results.",
)
@click.option(
    "--list", "-l", "list_only",
    is_flag=True,
    default=False,
    help="Print results only; skip interactive player prompt.",
)
def search(query: str, kind: str, limit: int, list_only: bool) -> None:
    """Search SoundCloud for tracks, users, or playlists.

    After showing track results you will be prompted to enter a number to
    play that track. Press Enter or q to quit without playing.
    Pass --list / -l to suppress the prompt (useful for scripting).

    \b
    Examples:
      sc search "bicep glue"
      sc search "four tet" --type users
      sc search "boiler room" --type playlists --limit 5
      sc search "bicep" --list
    """
    try:
        with console.status(f"Searching for [bold]{query}[/bold] …"):
            if kind == "tracks":
                results = _api.search_tracks(query, limit=limit)
            elif kind == "users":
                results = _api.search_users(query, limit=limit)
            else:
                results = _api.search_playlists(query, limit=limit)
    except Exception as exc:
        _handle_api_error(exc)

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    if kind == "tracks":
        _print_tracks_table(results, title=f'Tracks — "{query}"')
        if not list_only:
            _interactive_track_picker(results)
    elif kind == "users":
        _print_users_table(results, title=f'Users — "{query}"')
    else:
        _print_playlists_table(results, title=f'Playlists — "{query}"')


def _print_tracks_table(tracks: list[dict], title: str) -> None:
    table = Table(title=title, show_lines=False, header_style="bold magenta")
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Title", style="bold", min_width=25)
    table.add_column("Artist", style="cyan", min_width=15)
    table.add_column("Duration", justify="right", width=8)
    table.add_column("Plays", justify="right", width=7)
    table.add_column("URL", style="dim")

    for i, t in enumerate(tracks, 1):
        table.add_row(
            str(i),
            t.get("title") or "—",
            t.get("user", {}).get("username") or "—",
            _fmt_duration(t.get("duration") or 0),
            _fmt_count(t.get("playback_count")),
            t.get("permalink_url") or "—",
        )

    console.print(table)


def _interactive_track_picker(tracks: list[dict]) -> None:
    """Prompt the user to pick and play tracks from *tracks* in a loop."""
    from . import player as _player

    if not _player.player_available():
        console.print(
            "[dim]Tip: install mpv to play tracks directly (mpv is not found).[/dim]"
        )
        return

    n = len(tracks)
    console.print(f"\n[dim]Enter a track number to play [1\u2013{n}], or press Enter / q to quit.[/dim]")

    while True:
        try:
            raw = input("  Play> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if raw == "" or raw.lower() in ("q", "quit", "exit"):
            break

        if not raw.isdigit():
            console.print(f"  [yellow]Enter a number between 1 and {n}.[/yellow]")
            continue

        idx = int(raw)
        if not (1 <= idx <= n):
            console.print(f"  [yellow]Please enter a number between 1 and {n}.[/yellow]")
            continue

        result = _play_track(tracks[idx - 1])
        if result == "quit":
            break


def _print_users_table(users: list[dict], title: str) -> None:
    table = Table(title=title, show_lines=False, header_style="bold magenta")
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Username", style="bold", min_width=20)
    table.add_column("Full Name", style="cyan", min_width=15)
    table.add_column("Followers", justify="right", width=9)
    table.add_column("Tracks", justify="right", width=7)
    table.add_column("URL", style="dim")

    for i, u in enumerate(users, 1):
        table.add_row(
            str(i),
            u.get("username") or "—",
            u.get("full_name") or "—",
            _fmt_count(u.get("followers_count")),
            _fmt_count(u.get("track_count")),
            u.get("permalink_url") or "—",
        )

    console.print(table)


def _print_playlists_table(playlists: list[dict], title: str) -> None:
    table = Table(title=title, show_lines=False, header_style="bold magenta")
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Title", style="bold", min_width=25)
    table.add_column("Creator", style="cyan", min_width=15)
    table.add_column("Tracks", justify="right", width=7)
    table.add_column("Likes", justify="right", width=7)
    table.add_column("URL", style="dim")

    for i, p in enumerate(playlists, 1):
        table.add_row(
            str(i),
            p.get("title") or "—",
            p.get("user", {}).get("username") or "—",
            _fmt_count(p.get("track_count")),
            _fmt_count(p.get("likes_count")),
            p.get("permalink_url") or "—",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("url")
def info(url: str) -> None:
    """Show details about a track, user, or playlist URL.

    \b
    Examples:
      sc info https://soundcloud.com/bicep-music/glue
      sc info https://soundcloud.com/bicep-music
    """
    try:
        with console.status("Fetching info …"):
            data = _api.resolve(url)
    except Exception as exc:
        _handle_api_error(exc, url=url)

    kind = data.get("kind")
    if kind == "track":
        _print_track_info(data)
    elif kind == "user":
        _print_user_info(data)
    elif kind in ("playlist", "system-playlist"):
        _print_playlist_info(data)
    else:
        console.print(data)


def _print_track_info(t: dict) -> None:
    console.rule("[bold]Track[/bold]")
    pairs = [
        ("Title",    t.get("title")),
        ("Artist",   t.get("user", {}).get("username")),
        ("Duration", _fmt_duration(t.get("duration") or 0)),
        ("Genre",    t.get("genre") or "—"),
        ("Plays",    _fmt_count(t.get("playback_count"))),
        ("Likes",    _fmt_count(t.get("likes_count"))),
        ("Reposts",  _fmt_count(t.get("reposts_count"))),
        ("Comments", _fmt_count(t.get("comment_count"))),
        ("URL",      t.get("permalink_url")),
    ]
    for label, value in pairs:
        console.print(f"  [bold]{label}:[/bold] {value}")

    if desc := (t.get("description") or "").strip():
        console.print(f"\n  [bold]Description:[/bold]")
        console.print(Text(desc[:600] + ("…" if len(desc) > 600 else ""), style="dim"))


def _print_user_info(u: dict) -> None:
    console.rule("[bold]User[/bold]")
    pairs = [
        ("Username",   u.get("username")),
        ("Full Name",  u.get("full_name") or "—"),
        ("Followers",  _fmt_count(u.get("followers_count"))),
        ("Following",  _fmt_count(u.get("followings_count"))),
        ("Tracks",     _fmt_count(u.get("track_count"))),
        ("Playlists",  _fmt_count(u.get("playlist_count"))),
        ("URL",        u.get("permalink_url")),
    ]
    for label, value in pairs:
        console.print(f"  [bold]{label}:[/bold] {value}")

    if desc := (u.get("description") or "").strip():
        console.print(f"\n  [bold]Bio:[/bold]")
        console.print(Text(desc[:400] + ("…" if len(desc) > 400 else ""), style="dim"))


def _print_playlist_info(p: dict) -> None:
    console.rule("[bold]Playlist[/bold]")
    pairs = [
        ("Title",   p.get("title")),
        ("Creator", p.get("user", {}).get("username")),
        ("Tracks",  _fmt_count(p.get("track_count"))),
        ("Likes",   _fmt_count(p.get("likes_count"))),
        ("URL",     p.get("permalink_url")),
    ]
    for label, value in pairs:
        console.print(f"  [bold]{label}:[/bold] {value}")

    tracks = p.get("tracks") or []
    if tracks:
        console.print("\n  [bold]Track listing (first 15):[/bold]")
        for i, t in enumerate(tracks[:15], 1):
            artist = t.get("user", {}).get("username", "")
            title  = t.get("title", "—")
            console.print(f"    [dim]{i:>3}.[/dim] {title}" + (f" [cyan]— {artist}[/cyan]" if artist else ""))


# ---------------------------------------------------------------------------
# play
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("url")
def play(url: str) -> None:
    """Stream and play a track from its SoundCloud URL.

    Requires mpv, ffplay, or vlc to be installed.

    \b
    Example:
      sc play https://soundcloud.com/feelmybicep/bicep-glue-clip
    """
    try:
        with console.status("Fetching track …"):
            data = _api.resolve(url)
    except Exception as exc:
        _handle_api_error(exc, url=url)

    if data.get("kind") != "track":
        console.print("[red]The URL does not point to a track.[/red]")
        sys.exit(1)

    _play_track(data)


# ---------------------------------------------------------------------------
# stream  (search + play first result)
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query")
def stream(query: str) -> None:
    """Search for a track and immediately stream the top result.

    \b
    Example:
      sc stream "bicep glue"
    """
    from . import player as _player

    try:
        with console.status(f"Searching for [bold]{query}[/bold] …"):
            results = _api.search_tracks(query, limit=1)
    except Exception as exc:
        _handle_api_error(exc)

    if not results:
        console.print("[yellow]No tracks found.[/yellow]")
        sys.exit(1)

    _play_track(results[0])


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--limit", "-n",
    default=20,
    show_default=True,
    metavar="N",
    help="Number of recent entries to show.",
)
@click.option(
    "--clear",
    is_flag=True,
    default=False,
    help="Clear the play history.",
)
def history(limit: int, clear: bool) -> None:
    """Show recently played tracks.

    After listing entries you will be prompted to enter a number to
    replay a track. Press Enter or q to quit without replaying.

    \b
    Examples:
      sc history
      sc history --limit 50
      sc history --clear
    """
    if clear:
        if _HISTORY_FILE.exists():
            _HISTORY_FILE.unlink()
        console.print("[green]History cleared.[/green]")
        return

    if not _HISTORY_FILE.exists():
        console.print("[dim]No play history yet.[/dim]")
        return

    try:
        entries: list = json.loads(_HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]Could not read history:[/red] {exc}")
        return

    if not entries:
        console.print("[dim]No play history yet.[/dim]")
        return

    recent = entries[-limit:][::-1]  # most-recent first

    table = Table(title="Play History", show_lines=False, header_style="bold magenta")
    table.add_column("#",        style="dim",   width=3,  justify="right")
    table.add_column("Title",    style="bold",  min_width=25)
    table.add_column("Artist",   style="cyan",  min_width=15)
    table.add_column("Duration", justify="right", width=8)
    table.add_column("Played At", style="dim",  min_width=16)
    table.add_column("URL",      style="dim")

    for i, e in enumerate(recent, 1):
        played_at = e.get("played_at", "")
        if played_at:
            try:
                dt = datetime.fromisoformat(played_at)
                played_at = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        table.add_row(
            str(i),
            e.get("title")  or "—",
            e.get("artist") or "—",
            _fmt_duration(e.get("duration_ms") or 0),
            played_at or "—",
            e.get("url")    or "—",
        )

    console.print(table)
    _interactive_history_picker(recent)


def _interactive_history_picker(entries: list[dict]) -> None:
    from . import player as _player

    if not _player.player_available():
        return

    n = len(entries)
    console.print(f"\n[dim]Enter a number to replay [1\u2013{n}], or press Enter / q to quit.[/dim]")

    while True:
        try:
            raw = input("  Play> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if raw == "" or raw.lower() in ("q", "quit", "exit"):
            break

        if not raw.isdigit():
            console.print(f"  [yellow]Enter a number between 1 and {n}.[/yellow]")
            continue

        idx = int(raw)
        if not (1 <= idx <= n):
            console.print(f"  [yellow]Please enter a number between 1 and {n}.[/yellow]")
            continue

        entry = entries[idx - 1]
        url = entry.get("url", "")
        if not url:
            console.print("  [red]No URL stored for this entry.[/red]")
            continue

        try:
            with console.status("Fetching track …"):
                data = _api.resolve(url)
        except Exception as exc:
            _handle_api_error(exc, url=url)

        if data.get("kind") != "track":
            console.print("[red]Stored URL no longer points to a track.[/red]")
            continue

        result = _play_track(data)
        if result == "quit":
            break


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    default=".",
    show_default=True,
    metavar="DIR",
    help="Directory to save the file in.",
)
@click.option(
    "--format", "-f", "fmt",
    type=click.Choice(["mp3", "m4a", "opus", "flac", "best"]),
    default="mp3",
    show_default=True,
    help="Output audio format (best = no re-encode).",
)
def download(url: str, output: str, fmt: str) -> None:
    """Download a track (or playlist) from SoundCloud.

    Uses yt-dlp under the hood, so playlists and sets are supported too.

    \b
    Examples:
      sc download https://soundcloud.com/bicep-music/glue
      sc download https://soundcloud.com/bicep-music/glue --format opus
      sc download https://soundcloud.com/bicep-music/glue -o ~/Music
    """
    try:
        import yt_dlp
    except ImportError:
        console.print("[red]yt-dlp is not installed.[/red]  Run: pip install yt-dlp")
        sys.exit(1)

    ydl_opts: dict = {
        "outtmpl": f"{output}/%(uploader)s - %(title)s.%(ext)s",
    }

    if fmt == "best":
        ydl_opts["format"] = "bestaudio/best"
    else:
        ydl_opts["format"]         = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": "0",
            }
        ]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
