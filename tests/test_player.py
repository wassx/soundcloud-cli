"""Tests for sc_cli.player — player detection, command building, smoothing, VU rendering."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sc_cli.player import (
    _BANDS,
    _BLOCKS,
    _build_cmd,
    _find_player,
    _render_vu,
    _render_paused,
    _smooth,
    player_available,
    _SEEK_STEP,
)


# ---------------------------------------------------------------------------
# _find_player
# ---------------------------------------------------------------------------

class TestFindPlayer:
    def test_returns_mpv_when_available(self):
        with patch("sc_cli.player.shutil.which", side_effect=lambda p: "/usr/bin/mpv" if p == "mpv" else None):
            assert _find_player() == "mpv"

    def test_returns_ffplay_when_mpv_missing(self):
        def which(p):
            return "/usr/bin/ffplay" if p == "ffplay" else None

        with patch("sc_cli.player.shutil.which", side_effect=which):
            assert _find_player() == "ffplay"

    def test_returns_vlc_when_mpv_and_ffplay_missing(self):
        def which(p):
            return "/usr/bin/vlc" if p == "vlc" else None

        with patch("sc_cli.player.shutil.which", side_effect=which):
            assert _find_player() == "vlc"

    def test_returns_none_when_no_player_found(self):
        with patch("sc_cli.player.shutil.which", return_value=None):
            assert _find_player() is None

    def test_prefers_mpv_over_ffplay(self):
        """mpv is always preferred even if ffplay is also present."""
        with patch("sc_cli.player.shutil.which", return_value="/usr/bin/something"):
            assert _find_player() == "mpv"


# ---------------------------------------------------------------------------
# player_available
# ---------------------------------------------------------------------------

class TestPlayerAvailable:
    def test_true_when_player_found(self):
        with patch("sc_cli.player.shutil.which", return_value="/usr/bin/mpv"):
            assert player_available() is True

    def test_false_when_no_player(self):
        with patch("sc_cli.player.shutil.which", return_value=None):
            assert player_available() is False


# ---------------------------------------------------------------------------
# _build_cmd
# ---------------------------------------------------------------------------

class TestBuildCmd:
    URL = "https://cf-media.sndcdn.com/track.mp3"
    TITLE = "Bicep — Glue"

    def test_mpv_command(self):
        cmd = _build_cmd("mpv", self.URL, self.TITLE)
        assert cmd[0] == "mpv"
        assert "--no-video" in cmd
        assert self.URL in cmd
        assert any("--title=" in arg for arg in cmd)

    def test_mpv_command_with_ipc_path(self):
        cmd = _build_cmd("mpv", self.URL, self.TITLE, ipc_path="/tmp/test.sock")
        assert "--input-ipc-server=/tmp/test.sock" in cmd
        assert cmd[-1] == self.URL

    def test_mpv_command_without_ipc_path_has_no_ipc_flag(self):
        cmd = _build_cmd("mpv", self.URL, self.TITLE)
        assert not any("--input-ipc-server" in arg for arg in cmd)

    def test_ffplay_command(self):
        cmd = _build_cmd("ffplay", self.URL, self.TITLE)
        assert cmd[0] == "ffplay"
        assert "-nodisp" in cmd
        assert self.URL in cmd

    def test_vlc_command(self):
        cmd = _build_cmd("vlc", self.URL, self.TITLE)
        assert cmd[0] == "vlc"
        assert "--play-and-exit" in cmd
        assert self.URL in cmd

    def test_all_commands_end_with_url(self):
        for player in ("mpv", "ffplay", "vlc"):
            cmd = _build_cmd(player, self.URL, self.TITLE)
            assert cmd[-1] == self.URL


# ---------------------------------------------------------------------------
# _smooth
# ---------------------------------------------------------------------------

class TestSmooth:
    def test_basic_smoothing(self):
        levels = [0.0]
        _smooth(levels, 0, target=1.0, alpha=0.5)
        assert levels[0] == pytest.approx(0.5)

    def test_alpha_zero_leaves_level_unchanged(self):
        levels = [0.4]
        _smooth(levels, 0, target=1.0, alpha=0.0)
        assert levels[0] == pytest.approx(0.4)

    def test_alpha_one_sets_level_to_target(self):
        levels = [0.2]
        _smooth(levels, 0, target=0.9, alpha=1.0)
        assert levels[0] == pytest.approx(0.9)

    def test_default_alpha(self):
        """Default alpha=0.45; result = old*(0.55) + target*(0.45)."""
        levels = [0.6]
        _smooth(levels, 0, target=1.0)
        expected = 0.6 * 0.55 + 1.0 * 0.45
        assert levels[0] == pytest.approx(expected)

    def test_operates_on_correct_index(self):
        levels = [0.0, 0.0, 0.0]
        _smooth(levels, 1, target=1.0, alpha=1.0)
        assert levels[0] == 0.0
        assert levels[1] == pytest.approx(1.0)
        assert levels[2] == 0.0


# ---------------------------------------------------------------------------
# _render_vu
# ---------------------------------------------------------------------------

class TestRenderVu:
    """Tests for the VU meter Rich Text rendering."""

    def _levels(self, value: float = 0.5) -> list[float]:
        return [value] * _BANDS

    def _plain(self, text) -> str:
        """Extract plain string from a Rich Text object."""
        return text.plain

    def test_returns_rich_text_object(self):
        from rich.text import Text
        result = _render_vu(self._levels(), self._levels(), "Test Track", 0, 0)
        assert isinstance(result, Text)

    def test_contains_title(self):
        result = _render_vu(self._levels(), self._levels(), "My Song", 0, 0)
        assert "My Song" in self._plain(result)

    def test_contains_channel_labels(self):
        result = _render_vu(self._levels(), self._levels(), "T", 0, 0)
        plain = self._plain(result)
        assert "L" in plain
        assert "R" in plain

    def test_with_duration_shows_progress_timestamp(self):
        result = _render_vu(self._levels(), self._levels(), "T", elapsed=30, duration_s=210)
        plain = self._plain(result)
        # Should contain both elapsed and total time
        assert "0:30" in plain
        assert "3:30" in plain

    def test_without_duration_shows_only_elapsed(self):
        result = _render_vu(self._levels(), self._levels(), "T", elapsed=65, duration_s=0)
        plain = self._plain(result)
        assert "1:05" in plain
        # No "X:XX / Y:YY" progress timestamp when duration is unknown
        assert " / " not in plain

    def test_progress_bar_full_at_end(self):
        """At elapsed == duration, progress bar should be all filled blocks."""
        result = _render_vu(self._levels(), self._levels(), "T", elapsed=100, duration_s=100)
        plain = self._plain(result)
        bar_width = _BANDS * 2
        assert "█" * bar_width in plain

    def test_progress_bar_empty_at_start(self):
        """At elapsed == 0, progress bar should be all empty blocks."""
        result = _render_vu(self._levels(), self._levels(), "T", elapsed=0, duration_s=120)
        plain = self._plain(result)
        assert "░" * (_BANDS * 2) in plain

    def test_zero_level_renders_space(self):
        """A level of 0.0 maps to the first block character (space)."""
        levels = [0.0] * _BANDS
        result = _render_vu(levels, levels, "T", 0, 0)
        # The first block character in _BLOCKS is a space
        assert _BLOCKS[0] in self._plain(result)

    def test_max_level_renders_full_block(self):
        """A level of 1.0 maps to the last block character (full block)."""
        levels = [1.0] * _BANDS
        result = _render_vu(levels, levels, "T", 0, 0)
        assert _BLOCKS[-1] in self._plain(result)

    def test_contains_pause_hint(self):
        result = _render_vu(self._levels(), self._levels(), "T", 0, 0)
        plain = self._plain(result)
        assert "q" in plain
        assert "pause" in plain

    def test_contains_seek_hint(self):
        result = _render_vu(self._levels(), self._levels(), "T", 0, 0)
        plain = self._plain(result)
        assert "seek" in plain
        assert "←" in plain or "/" in plain  # arrow symbols or separator

    def test_elapsed_beyond_duration_clamps_progress(self):
        """elapsed > duration should not crash and should show a full bar."""
        result = _render_vu(self._levels(), self._levels(), "T", elapsed=200, duration_s=100)
        plain = self._plain(result)
        bar_width = _BANDS * 2
        assert "█" * bar_width in plain


# ---------------------------------------------------------------------------
# _render_paused
# ---------------------------------------------------------------------------

class TestRenderPaused:
    def _plain(self, text) -> str:
        return text.plain

    def test_contains_title(self):
        result = _render_paused("My Song", elapsed=30, duration_s=180)
        assert "My Song" in self._plain(result)

    def test_contains_resume_hint(self):
        result = _render_paused("T", elapsed=0, duration_s=0)
        plain = self._plain(result)
        assert "resume" in plain
        assert "Space" in plain

    def test_contains_next_and_search_history_hints(self):
        result = _render_paused("T", elapsed=0, duration_s=0)
        plain = self._plain(result)
        assert "next" in plain
        assert "search" in plain
        assert "history" in plain
        assert "n" in plain

    def test_contains_exit_hint(self):
        result = _render_paused("T", elapsed=0, duration_s=0)
        plain = self._plain(result)
        assert "exit" in plain
        assert "q" in plain

    def test_shows_progress_with_duration(self):
        result = _render_paused("T", elapsed=90, duration_s=180)
        plain = self._plain(result)
        assert "1:30" in plain
        assert "3:00" in plain

    def test_progress_bar_present(self):
        result = _render_paused("T", elapsed=0, duration_s=120)
        plain = self._plain(result)
        assert "░" * (_BANDS * 2) in plain
