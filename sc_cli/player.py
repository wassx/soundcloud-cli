"""Audio playback helpers — with animated VU meter, pause, and seek controls."""

from __future__ import annotations

import json
import os
import random
import select
import shutil
import signal
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
# mpv IPC / process pause + resume
# ---------------------------------------------------------------------------

def _send_mpv_cmd(socket_path: str, command: list) -> None:
    """Send a JSON command to mpv via its IPC socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(socket_path)
            sock.sendall((json.dumps({"command": command}) + "\n").encode())
    except Exception:
        pass


def _pause_player(proc: subprocess.Popen, ipc_path: str | None) -> None:
    if ipc_path:
        _send_mpv_cmd(ipc_path, ["set_property", "pause", True])
    else:
        try:
            proc.send_signal(signal.SIGSTOP)
        except Exception:
            pass


def _resume_player(proc: subprocess.Popen, ipc_path: str | None) -> None:
    if ipc_path:
        _send_mpv_cmd(ipc_path, ["set_property", "pause", False])
    else:
        try:
            proc.send_signal(signal.SIGCONT)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Keypress listener (raw terminal, non-blocking)
# ---------------------------------------------------------------------------

def _read_byte(fd: int) -> bytes:
    """Read exactly one raw byte from a file descriptor."""
    return os.read(fd, 1)


def _read_escape_sequence(fd: int) -> bytes | None:
    """After consuming ESC, try to read an ANSI escape sequence.

    Returns the final direction byte (e.g. b'C' for right arrow), or None
    for a bare Escape or an unrecognised sequence.
    """
    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return None   # bare Escape
    ch2 = _read_byte(fd)
    if ch2 != b"[":
        return None   # unrecognised sequence — treat as bare Escape
    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return None
    return _read_byte(fd)  # A / B / C / D


def _key_listener(
    stop_event: threading.Event,
    pause_event: threading.Event,
    seek_offset: list[float],
    duration_s: float,
    action: list[str],
    ipc_path: str | None = None,
) -> None:
    """Handle keypresses in both playing and paused modes."""
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop_event.is_set():
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            ch = _read_byte(fd)

            # Arrow keys work in both modes (seek forward / backward)
            if ch == b"\x1b":
                direction = _read_escape_sequence(fd)
                if direction == b"C":       # right arrow → seek forward
                    if ipc_path:
                        _send_mpv_cmd(ipc_path, ["seek", _SEEK_STEP, "relative"])
                    seek_offset[0] += _SEEK_STEP
                elif direction == b"D":     # left arrow ← seek backward
                    if ipc_path:
                        _send_mpv_cmd(ipc_path, ["seek", -_SEEK_STEP, "relative"])
                    seek_offset[0] -= _SEEK_STEP
                else:
                    # Bare Escape → quit (works in both modes)
                    action.append("quit")
                    stop_event.set()
                    break

            elif pause_event.is_set():
                # ── PAUSED mode ──────────────────────────────────────────
                if ch in (b" ", b"\r"):         # Space / Enter → resume
                    pause_event.clear()
                elif ch in (b"n", b"N"):        # n → stop + return to picker
                    stop_event.set()
                    break
                elif ch in (b"q", b"Q", b"\x03", b"\x04"):  # q / Ctrl-C/D → exit
                    action.append("quit")
                    stop_event.set()
                    break

            else:
                # ── PLAYING mode ─────────────────────────────────────────
                if ch in (b"q", b"Q", b" "):   # Space / q → pause
                    pause_event.set()
                elif ch in (b"\x03", b"\x04"): # Ctrl-C / Ctrl-D → quit
                    action.append("quit")
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

def _append_progress(t: Text, elapsed: float, duration_s: float) -> None:
    """Append progress bar and timestamp to *t*."""
    bar_width = _BANDS * 2
    if duration_s > 0:
        progress = min(elapsed / duration_s, 1.0)
        filled = int(bar_width * progress)
        em, es = divmod(int(elapsed), 60)
        tm, ts = divmod(int(duration_s), 60)
        bar = "█" * filled + "░" * (bar_width - filled)
        t.append("\n   ")
        t.append(bar[:filled], style="bold cyan")
        t.append(bar[filled:], style="dim")
        t.append(f"  {em}:{es:02d} / {tm}:{ts:02d}\n", style="dim")
    else:
        m, s = divmod(int(elapsed), 60)
        t.append(f"\n   {m}:{s:02d}\n", style="dim")


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

    _append_progress(t, elapsed, duration_s)
    t.append("   ", style="dim")
    t.append("←/→", style="bold yellow")
    t.append(" — seek   ", style="dim")
    t.append("Space/q", style="bold yellow")
    t.append(" — pause", style="dim")
    return t


def _render_paused(title: str, elapsed: float, duration_s: float) -> Text:
    t = Text(no_wrap=True)
    t.append(" ⏸  ", style="bold yellow")
    t.append(title + "\n\n", style="bold cyan")

    _append_progress(t, elapsed, duration_s)
    t.append("\n   ")
    t.append("Space/Enter", style="bold yellow")
    t.append(" — resume   ", style="dim")
    t.append("n", style="bold yellow")
    t.append(" — new song   ", style="dim")
    t.append("q", style="bold yellow")
    t.append(" — exit", style="dim")
    return t


def _smooth(levels: list[float], i: int, target: float, alpha: float = 0.45) -> None:
    levels[i] = levels[i] * (1 - alpha) + target * alpha


def _animate_vu(
    proc: subprocess.Popen,
    stop_event: threading.Event,
    pause_event: threading.Event,
    title: str,
    duration_s: float,
    seek_offset: list[float],
    ipc_path: str | None,
) -> None:
    levels_l = [0.0] * _BANDS
    levels_r = [0.0] * _BANDS
    start = time.monotonic()
    paused_total = 0.0          # cumulative seconds spent paused
    pause_start: float | None = None
    was_paused = False

    with Live(console=_console, refresh_per_second=20, transient=True) as live:
        while not stop_event.is_set() and proc.poll() is None:
            is_paused = pause_event.is_set()

            # React to pause / resume transitions
            if is_paused and not was_paused:
                _pause_player(proc, ipc_path)
                pause_start = time.monotonic()
            elif not is_paused and was_paused:
                _resume_player(proc, ipc_path)
                if pause_start is not None:
                    paused_total += time.monotonic() - pause_start
                    pause_start = None
            was_paused = is_paused

            # Elapsed excludes time spent paused
            in_pause = (time.monotonic() - pause_start) if (is_paused and pause_start) else 0.0
            elapsed = max(0.0, time.monotonic() - start - paused_total - in_pause + seek_offset[0])
            if duration_s > 0:
                elapsed = min(elapsed, duration_s)

            if is_paused:
                live.update(_render_paused(title, elapsed, duration_s))
                time.sleep(0.1)
            else:
                peak = random.uniform(0.35, 0.95)
                for i in range(_BANDS):
                    _smooth(levels_l, i, peak * _SHAPE[i] * random.uniform(0.55, 1.0))
                    _smooth(levels_r, i, peak * _SHAPE[i] * random.uniform(0.55, 1.0))
                live.update(_render_vu(levels_l, levels_r, title, elapsed, duration_s))
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def play(stream_url: str, title: str = "", duration_ms: int = 0) -> str | None:
    """Play *stream_url* with animated VU meter.

    Returns ``'quit'`` when the user chose to exit the player entirely,
    or ``None`` when the track finished or the user chose to pick a new song.
    """
    player = _find_player()
    if player is None:
        _console.print("No audio player found. Install [bold]mpv[/bold] (recommended) or ffplay.")
        _console.print(f"  Stream URL: [dim]{stream_url}[/dim]")
        return None

    ipc_path = f"/tmp/sc-cli-mpv-{id(stream_url)}.sock" if player == "mpv" else None

    try:
        proc = subprocess.Popen(
            _build_cmd(player, stream_url, title, ipc_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        _console.print(f"[red]Failed to start player:[/red] {exc}")
        return None

    action: list[str] = []
    stop_event = threading.Event()
    pause_event = threading.Event()
    seek_offset: list[float] = [0.0]

    key_thread = threading.Thread(
        target=_key_listener,
        args=(stop_event, pause_event, seek_offset, duration_ms / 1000, action, ipc_path),
        daemon=True,
    )
    key_thread.start()

    track_finished = False
    try:
        _animate_vu(proc, stop_event, pause_event, title, duration_ms / 1000, seek_offset, ipc_path)
        track_finished = proc.poll() is not None
    except KeyboardInterrupt:
        action.append("quit")
    finally:
        stop_event.set()
        # Must resume a SIGSTOP'd process before it can receive SIGTERM
        if pause_event.is_set():
            _resume_player(proc, ipc_path)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        key_thread.join(timeout=1)

    if track_finished:
        _console.print("[dim]✓ Finished.[/dim]")
    else:
        _console.print("[dim]■ Stopped.[/dim]")

    return "quit" if "quit" in action else None


def player_available() -> bool:
    return _find_player() is not None
