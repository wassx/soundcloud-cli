"""Audio playback helpers — with animated VU meter and stop control."""

from __future__ import annotations

import json
import os
import random
import select
import shutil
import socket
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

_SEEK_STEP = 10  # seconds per arrow key press


# ---------------------------------------------------------------------------
# Player helpers
# ---------------------------------------------------------------------------

def _find_player() -> str | None:
    for player in ("mpv", "ffplay", "vlc"):
        if shutil.which(player):
            return player
    return None


def _build_cmd(player: str, stream_url: str, title: str, ipc_path: str | None = None) -> list[str]:
    if player == "mpv":
        cmd = ["mpv", "--no-video", "--really-quiet", f"--title={title}"]
        if ipc_path:
            cmd.append(f"--input-ipc-server={ipc_path}")
        cmd.append(stream_url)
        return cmd
    elif player == "ffplay":
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", stream_url]
    else:  # vlc
        return ["vlc", "--intf", "dummy", "--play-and-exit", "--quiet", stream_url]


# ---------------------------------------------------------------------------
# Seek via mpv IPC socket
# ---------------------------------------------------------------------------

def _seek_mpv(socket_path: str, seconds: float) -> None:
    """Send a relative seek command to mpv via its IPC socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(socket_path)
            cmd = json.dumps({"command": ["seek", seconds, "relative"]}) + "\n"
            sock.sendall(cmd.encode())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Keypress listener (raw terminal, non-blocking)
# ---------------------------------------------------------------------------

def _read_byte(fd: int) -> bytes:
    """Read exactly one raw byte from a file descriptor."""
    return os.read(fd, 1)


def _key_listener(
    stop_event: threading.Event,
    seek_offset: list[float],
    duration_s: float,
    ipc_path: str | None = None,
) -> None:
    """Listen for keypresses: q/Space/Ctrl-C/Esc to stop; ←/→ to seek."""
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop_event.is_set():
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                ch = _read_byte(fd)
                if ch == b"\x1b":
                    # Could be Escape key or start of an arrow-key escape sequence
                    r2, _, _ = select.select([fd], [], [], 0.05)
                    if r2:
                        ch2 = _read_byte(fd)
                        if ch2 == b"[":
                            r3, _, _ = select.select([fd], [], [], 0.05)
                            if r3:
                                ch3 = _read_byte(fd)
                                if ch3 == b"C":  # Right arrow → seek forward
                                    if ipc_path:
                                        _seek_mpv(ipc_path, _SEEK_STEP)
                                    seek_offset[0] += _SEEK_STEP
                                    continue
                                elif ch3 == b"D":  # Left arrow ← seek backward
                                    if ipc_path:
                                        _seek_mpv(ipc_path, -_SEEK_STEP)
                                    seek_offset[0] -= _SEEK_STEP
                                    continue
                    # Escape alone (or unrecognised sequence) → stop
                    stop_event.set()
                    break
                elif ch in (b"q", b"Q", b" ", b"\x03", b"\x04"):
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
    duration_s: float,
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

    # Progress bar
    bar_width = _BANDS * 2  # same visual width as both channels combined
    if duration_s > 0:
        progress = min(elapsed / duration_s, 1.0)
        filled = int(bar_width * progress)
        elapsed_s = int(elapsed)
        total_s = int(duration_s)
        em, es = divmod(elapsed_s, 60)
        tm, ts = divmod(total_s, 60)
        bar = "█" * filled + "░" * (bar_width - filled)
        t.append(f"\n   ", style="dim")
        t.append(bar[:filled], style="bold cyan")
        t.append(bar[filled:], style="dim")
        t.append(f"  {em}:{es:02d} / {tm}:{ts:02d}", style="dim")
    else:
        m, s = divmod(int(elapsed), 60)
        t.append(f"\n   {m}:{s:02d}", style="dim")

    t.append("   ", style="dim")
    t.append("←/→", style="bold yellow")
    t.append(" — seek   ", style="dim")
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
    duration_s: float,
    seek_offset: list[float],
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
            elapsed = max(0.0, time.monotonic() - start + seek_offset[0])
            if duration_s > 0:
                elapsed = min(elapsed, duration_s)
            live.update(_render_vu(levels_l, levels_r, title, elapsed, duration_s))
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def play(stream_url: str, title: str = "", duration_ms: int = 0) -> None:
    """Play stream_url with animated VU meter and progress bar. Press q/Space to stop."""
    player = _find_player()
    if player is None:
        _console.print("No audio player found. Install [bold]mpv[/bold] (recommended) or ffplay.")
        _console.print(f"  Stream URL: [dim]{stream_url}[/dim]")
        return

    ipc_path = f"/tmp/sc-cli-mpv-{id(stream_url)}.sock" if player == "mpv" else None

    try:
        proc = subprocess.Popen(
            _build_cmd(player, stream_url, title, ipc_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        _console.print(f"[red]Failed to start player:[/red] {exc}")
        return

    seek_offset: list[float] = [0.0]
    stop_event = threading.Event()
    key_thread = threading.Thread(
        target=_key_listener,
        args=(stop_event, seek_offset, duration_ms / 1000, ipc_path),
        daemon=True,
    )
    key_thread.start()

    still_running = False
    try:
        _animate_vu(proc, stop_event, title, duration_ms / 1000, seek_offset)
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
