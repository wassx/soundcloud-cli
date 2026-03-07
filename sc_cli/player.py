"""Audio playback helpers — with animated VU meter and stop control."""

from __future__ import annotations

import random
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty

from rich.console import Console
from rich.live import Live
from rich.text import Text

_console = Console()

# VU meter geometry
_BANDS = 20        # frequency bands per channel
_BLOCKS = " ▁▂▃▄▅▆▇█"

# Bell-curve shape: mids louder than extremes
_SHAPE = [0.25 + 0.75 * (1 - abs(i - _BANDS // 2) / (_BANDS // 2)) for i in range(_BANDS)]


# ---------------------------------------------------------------------------
# Player helpers
# ---------------------------------------------------------------------------

def _find_player() -> str | None:
    for player in ("mpv", "ffplay", "vlc"):
        if shutil.which(player):
            return player
    return None


def _build_cmd(player: str, stream_url: str, title: str) -> list[str]:
    if player == "mpv":
        return ["mpv", "--no-video", "--really-quiet", f"--title={title}", stream_url]
    elif player == "ffplay":
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", stream_url]
    else:  # vlc
        return ["vlc", "--intf", "dummy", "--play-and-exit", "--quiet", stream_url]


# ---------------------------------------------------------------------------
# Keypress listener (raw terminal, non-blocking)
# ---------------------------------------------------------------------------

def _key_listener(stop_event: threading.Event) -> None:
    """Set stop_event when the user presses q / Q / Space / Ctrl-C / Esc."""
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop_event.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if r:
                ch = sys.stdin.read(1)
                if ch in ("q", "Q", " ", "\x03", "\x04", "\x1b"):
                    stop_event.set()
                    break
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# VU meter rendering
# ---------------------------------------------------------------------------

def _render_vu(
    levels_l: list[float],
    levels_r: list[float],
    title: str,
    elapsed: float,
) -> Text:
    t = Text(no_wrap=True)
    t.append(" ♪  ", style="bold green")
    t.append(title + "\n", style="bold cyan")

    for label, levels in (("L", levels_l), ("R", levels_r)):
        t.append(f" {label} │", style="dim")
        for lvl in levels:
            idx = max(0, min(len(_BLOCKS) - 1, int(lvl * (len(_BLOCKS) - 1))))
            style = "green" if lvl < 0.55 else ("yellow" if lvl < 0.82 else "bold red")
            t.append(_BLOCKS[idx], style=style)
        t.append("│\n", style="dim")

    m, s = divmod(int(elapsed), 60)
    t.append(f"\n   {m}:{s:02d}  ", style="dim")
    t.append("q", style="bold yellow")
    t.append("/", style="dim")
    t.append("Space", style="bold yellow")
    t.append(" — stop", style="dim")
    return t


def _smooth(levels: list[float], i: int, target: float, alpha: float = 0.45) -> None:
    levels[i] = levels[i] * (1 - alpha) + target * alpha


def _animate_vu(
    proc: subprocess.Popen,
    stop_event: threading.Event,
    title: str,
) -> None:
    levels_l = [0.0] * _BANDS
    levels_r = [0.0] * _BANDS
    start = time.monotonic()

    with Live(console=_console, refresh_per_second=20, transient=True) as live:
        while not stop_event.is_set() and proc.poll() is None:
            peak = random.uniform(0.35, 0.95)
            for i in range(_BANDS):
                _smooth(levels_l, i, peak * _SHAPE[i] * random.uniform(0.55, 1.0))
                _smooth(levels_r, i, peak * _SHAPE[i] * random.uniform(0.55, 1.0))
            live.update(_render_vu(levels_l, levels_r, title, time.monotonic() - start))
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def play(stream_url: str, title: str = "") -> None:
    """Play stream_url with animated VU meter. Press q/Space to stop."""
    player = _find_player()
    if player is None:
        _console.print("No audio player found. Install [bold]mpv[/bold] (recommended) or ffplay.")
        _console.print(f"  Stream URL: [dim]{stream_url}[/dim]")
        return

    try:
        proc = subprocess.Popen(
            _build_cmd(player, stream_url, title),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        _console.print(f"[red]Failed to start player:[/red] {exc}")
        return

    stop_event = threading.Event()
    key_thread = threading.Thread(target=_key_listener, args=(stop_event,), daemon=True)
    key_thread.start()

    still_running = False
    try:
        _animate_vu(proc, stop_event, title)
        still_running = proc.poll() is None
    except KeyboardInterrupt:
        stop_event.set()
        still_running = True
    finally:
        stop_event.set()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        key_thread.join(timeout=1)

    if still_running:
        _console.print("[dim]■ Stopped.[/dim]")
    else:
        _console.print("[dim]✓ Finished.[/dim]")


def player_available() -> bool:
    return _find_player() is not None
