"""Tests for sc_cli.main — pure helpers and CLI commands via CliRunner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from sc_cli.main import _fmt_count, _fmt_duration, _handle_api_error, cli


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------

class TestFmtDuration:
    def test_zero(self):
        assert _fmt_duration(0) == "0:00"

    def test_seconds_only(self):
        assert _fmt_duration(5_000) == "0:05"

    def test_one_minute(self):
        assert _fmt_duration(60_000) == "1:00"

    def test_minutes_and_seconds(self):
        assert _fmt_duration(65_000) == "1:05"

    def test_under_one_hour(self):
        assert _fmt_duration(3_599_000) == "59:59"

    def test_exactly_one_hour(self):
        assert _fmt_duration(3_600_000) == "1:00:00"

    def test_hours_minutes_seconds(self):
        assert _fmt_duration(3_661_000) == "1:01:01"

    def test_large_duration(self):
        assert _fmt_duration(7_200_000) == "2:00:00"


# ---------------------------------------------------------------------------
# _fmt_count
# ---------------------------------------------------------------------------

class TestFmtCount:
    def test_none_returns_dash(self):
        assert _fmt_count(None) == "—"

    def test_zero(self):
        assert _fmt_count(0) == "0"

    def test_below_thousand(self):
        assert _fmt_count(999) == "999"

    def test_exactly_one_thousand(self):
        assert _fmt_count(1_000) == "1.0K"

    def test_thousands(self):
        assert _fmt_count(1_500) == "1.5K"

    def test_just_under_million(self):
        assert _fmt_count(999_999) == "1000.0K"

    def test_exactly_one_million(self):
        assert _fmt_count(1_000_000) == "1.0M"

    def test_millions(self):
        assert _fmt_count(2_500_000) == "2.5M"


# ---------------------------------------------------------------------------
# _handle_api_error
# ---------------------------------------------------------------------------

class TestHandleApiError:
    def _run(self, exc, url=None):
        """Invoke _handle_api_error and capture SystemExit + console output."""
        from io import StringIO
        from unittest.mock import patch as _patch
        import sc_cli.main as m

        output = []
        original_print = m.console.print
        with _patch.object(m.console, "print", side_effect=lambda *a, **kw: output.append(str(a[0]))):
            with pytest.raises(SystemExit) as exc_info:
                _handle_api_error(exc, url)
        return exc_info.value.code, "\n".join(output)

    def test_404_error_mentions_not_found(self):
        resp = MagicMock()
        resp.status_code = 404
        exc = requests.HTTPError(response=resp)
        code, output = self._run(exc, url="https://soundcloud.com/missing")
        assert code == 1
        assert "404" in output or "Not found" in output

    def test_other_http_error_shows_status(self):
        resp = MagicMock()
        resp.status_code = 500
        resp.reason = "Internal Server Error"
        exc = requests.HTTPError(response=resp)
        code, output = self._run(exc)
        assert code == 1
        assert "500" in output

    def test_connection_error_shows_network_message(self):
        exc = requests.ConnectionError("no route")
        code, output = self._run(exc)
        assert code == 1
        assert "Network" in output or "network" in output

    def test_runtime_error_shows_message(self):
        exc = RuntimeError("client_id scraping failed")
        code, output = self._run(exc)
        assert code == 1
        assert "client_id scraping failed" in output

    def test_generic_exception_shows_unexpected(self):
        exc = ValueError("something weird")
        code, output = self._run(exc)
        assert code == 1
        assert "something weird" in output


# ---------------------------------------------------------------------------
# CLI — search command
# ---------------------------------------------------------------------------

FAKE_TRACKS = [
    {
        "title": "Glue",
        "user": {"username": "bicep-music"},
        "duration": 390_000,
        "playback_count": 1_200_000,
        "permalink_url": "https://soundcloud.com/bicep-music/glue",
    },
    {
        "title": "Rain",
        "user": {"username": "bicep-music"},
        "duration": 300_000,
        "playback_count": 500_000,
        "permalink_url": "https://soundcloud.com/bicep-music/rain",
    },
]

FAKE_USERS = [
    {
        "username": "bicep-music",
        "full_name": "Bicep",
        "followers_count": 300_000,
        "track_count": 50,
        "permalink_url": "https://soundcloud.com/bicep-music",
    }
]

FAKE_PLAYLISTS = [
    {
        "title": "Essential Mix",
        "user": {"username": "bbcradio1"},
        "track_count": 30,
        "likes_count": 10_000,
        "permalink_url": "https://soundcloud.com/bbcradio1/essential-mix",
    }
]


class TestSearchCommand:
    def test_search_tracks_displays_table(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_tracks.return_value = FAKE_TRACKS
            result = runner.invoke(cli, ["search", "bicep", "--list"])

        assert result.exit_code == 0
        assert "Glue" in result.output
        assert "bicep-music" in result.output

    def test_search_users_displays_users_table(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_users.return_value = FAKE_USERS
            result = runner.invoke(cli, ["search", "bicep", "--type", "users", "--list"])

        assert result.exit_code == 0
        assert "bicep-music" in result.output

    def test_search_playlists_displays_playlists_table(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_playlists.return_value = FAKE_PLAYLISTS
            result = runner.invoke(cli, ["search", "essential mix", "--type", "playlists", "--list"])

        assert result.exit_code == 0
        assert "Essential Mix" in result.output

    def test_search_no_results_prints_message(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_tracks.return_value = []
            result = runner.invoke(cli, ["search", "xyzzy", "--list"])

        assert result.exit_code == 0
        assert "No results" in result.output

    def test_search_api_error_exits_nonzero(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_tracks.side_effect = requests.ConnectionError()
            result = runner.invoke(cli, ["search", "bicep", "--list"])

        assert result.exit_code != 0

    def test_search_respects_limit_option(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_tracks.return_value = FAKE_TRACKS
            runner.invoke(cli, ["search", "bicep", "--limit", "5", "--list"])

        mock_api.search_tracks.assert_called_once_with("bicep", limit=5)

    def test_search_without_list_flag_shows_player_prompt(self):
        """Without --list, the interactive picker is entered (then immediately exits on empty input)."""
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api, \
             patch("sc_cli.player.player_available", return_value=True):
            mock_api.search_tracks.return_value = FAKE_TRACKS
            # Send empty input to exit the picker loop immediately
            result = runner.invoke(cli, ["search", "bicep"], input="\n")

        assert result.exit_code == 0
        assert "Play>" in result.output


# ---------------------------------------------------------------------------
# CLI — info command
# ---------------------------------------------------------------------------

class TestInfoCommand:
    def test_info_track(self):
        runner = CliRunner()
        track_data = {
            "kind": "track",
            "title": "Glue",
            "user": {"username": "bicep-music"},
            "duration": 390_000,
            "genre": "Electronic",
            "playback_count": 1_200_000,
            "likes_count": 50_000,
            "reposts_count": 5_000,
            "comment_count": 200,
            "permalink_url": "https://soundcloud.com/bicep-music/glue",
            "description": "A track.",
        }
        with patch("sc_cli.main._api") as mock_api:
            mock_api.resolve.return_value = track_data
            result = runner.invoke(cli, ["info", "https://soundcloud.com/bicep-music/glue"])

        assert result.exit_code == 0
        assert "Glue" in result.output
        assert "bicep-music" in result.output

    def test_info_user(self):
        runner = CliRunner()
        user_data = {
            "kind": "user",
            "username": "bicep-music",
            "full_name": "Bicep",
            "followers_count": 300_000,
            "followings_count": 100,
            "track_count": 50,
            "playlist_count": 5,
            "permalink_url": "https://soundcloud.com/bicep-music",
            "description": "",
        }
        with patch("sc_cli.main._api") as mock_api:
            mock_api.resolve.return_value = user_data
            result = runner.invoke(cli, ["info", "https://soundcloud.com/bicep-music"])

        assert result.exit_code == 0
        assert "bicep-music" in result.output

    def test_info_playlist(self):
        runner = CliRunner()
        playlist_data = {
            "kind": "playlist",
            "title": "Essential Mix",
            "user": {"username": "bbcradio1"},
            "track_count": 30,
            "likes_count": 10_000,
            "permalink_url": "https://soundcloud.com/bbcradio1/essential-mix",
            "tracks": [],
        }
        with patch("sc_cli.main._api") as mock_api:
            mock_api.resolve.return_value = playlist_data
            result = runner.invoke(cli, ["info", "https://soundcloud.com/bbcradio1/essential-mix"])

        assert result.exit_code == 0
        assert "Essential Mix" in result.output

    def test_info_api_error_exits_nonzero(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            resp = MagicMock()
            resp.status_code = 404
            mock_api.resolve.side_effect = requests.HTTPError(response=resp)
            result = runner.invoke(cli, ["info", "https://soundcloud.com/gone"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI — play command
# ---------------------------------------------------------------------------

class TestPlayCommand:
    def test_play_non_track_url_exits_with_error(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.resolve.return_value = {"kind": "user", "username": "someone"}
            result = runner.invoke(cli, ["play", "https://soundcloud.com/someone"])

        assert result.exit_code != 0
        assert "not" in result.output.lower() or "track" in result.output.lower()

    def test_play_track_calls_play_track(self):
        runner = CliRunner()
        track_data = {
            "kind": "track",
            "title": "Glue",
            "user": {"username": "bicep-music"},
            "duration": 390_000,
            "media": {"transcodings": []},
        }
        with patch("sc_cli.main._api") as mock_api, \
             patch("sc_cli.main._play_track") as mock_play:
            mock_api.resolve.return_value = track_data
            result = runner.invoke(cli, ["play", "https://soundcloud.com/bicep-music/glue"])

        mock_play.assert_called_once_with(track_data)

    def test_play_api_error_exits_nonzero(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            resp = MagicMock()
            resp.status_code = 404
            mock_api.resolve.side_effect = requests.HTTPError(response=resp)
            result = runner.invoke(cli, ["play", "https://soundcloud.com/gone"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI — stream command
# ---------------------------------------------------------------------------

class TestStreamCommand:
    def test_stream_no_results_exits_nonzero(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_tracks.return_value = []
            result = runner.invoke(cli, ["stream", "xyzzy nonexistent"])

        assert result.exit_code != 0
        assert "No tracks" in result.output

    def test_stream_plays_first_result(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api, \
             patch("sc_cli.main._play_track") as mock_play:
            mock_api.search_tracks.return_value = FAKE_TRACKS
            result = runner.invoke(cli, ["stream", "bicep glue"])

        mock_play.assert_called_once_with(FAKE_TRACKS[0])

    def test_stream_api_error_exits_nonzero(self):
        runner = CliRunner()
        with patch("sc_cli.main._api") as mock_api:
            mock_api.search_tracks.side_effect = requests.ConnectionError()
            result = runner.invoke(cli, ["stream", "bicep"])

        assert result.exit_code != 0
