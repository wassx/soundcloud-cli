"""Tests for sc_cli.api — client_id scraping, caching, HTTP retry, and stream URL logic."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from sc_cli.api import (
    SoundCloudClient,
    _load_cached_client_id,
    _save_client_id,
    _scrape_client_id,
)


# ---------------------------------------------------------------------------
# _scrape_client_id
# ---------------------------------------------------------------------------

class TestScrapeClientId:
    """Tests for the regex-based client_id scraper."""

    def _make_session(self, html: str, js_pages: dict[str, str]) -> MagicMock:
        """Build a mock requests.Session whose .get() returns canned responses."""
        session = MagicMock()

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if url == "https://soundcloud.com":
                resp.text = html
            else:
                resp.text = js_pages.get(url, "")
            return resp

        session.get.side_effect = fake_get
        return session

    def test_extracts_client_id_from_js(self):
        """Finds the 32-char client_id in a JS bundle."""
        client_id = "a" * 32
        html = '<script src="https://a-v2.sndcdn.com/assets/app.js"></script>'
        js = f'(client_id:"{client_id}",other:"stuff")'
        session = self._make_session(html, {"https://a-v2.sndcdn.com/assets/app.js": js})

        result = _scrape_client_id(session)
        assert result == client_id

    def test_falls_back_to_second_script_when_first_lacks_id(self):
        """Tries scripts in reversed order; returns id from the first match."""
        client_id = "b" * 32
        html = (
            '<script src="https://a-v2.sndcdn.com/assets/a.js"></script>'
            '<script src="https://a-v2.sndcdn.com/assets/b.js"></script>'
        )
        js_pages = {
            "https://a-v2.sndcdn.com/assets/a.js": "no id here",
            "https://a-v2.sndcdn.com/assets/b.js": f'{{client_id:"{client_id}"}}',
        }
        session = self._make_session(html, js_pages)

        result = _scrape_client_id(session)
        assert result == client_id

    def test_skips_js_url_that_raises_request_exception(self):
        """Continues to next script if one JS fetch fails."""
        client_id = "c" * 32
        html = (
            '<script src="https://a-v2.sndcdn.com/assets/bad.js"></script>'
            '<script src="https://a-v2.sndcdn.com/assets/good.js"></script>'
        )

        session = MagicMock()
        main_resp = MagicMock()
        main_resp.raise_for_status = MagicMock()
        main_resp.text = html

        good_resp = MagicMock()
        good_resp.text = f',client_id:"{client_id}"'

        def fake_get(url, **kwargs):
            if url == "https://soundcloud.com":
                return main_resp
            if "bad" in url:
                raise requests.RequestException("timeout")
            return good_resp

        session.get.side_effect = fake_get
        result = _scrape_client_id(session)
        assert result == client_id

    def test_raises_runtime_error_when_no_id_found(self):
        """Raises RuntimeError if no JS bundle contains a client_id."""
        html = '<script src="https://a-v2.sndcdn.com/assets/app.js"></script>'
        session = self._make_session(html, {"https://a-v2.sndcdn.com/assets/app.js": "nothing useful"})

        with pytest.raises(RuntimeError, match="client_id"):
            _scrape_client_id(session)

    def test_raises_runtime_error_when_no_script_tags(self):
        """Raises RuntimeError if the HTML contains no asset script tags."""
        session = self._make_session("<html>no scripts</html>", {})

        with pytest.raises(RuntimeError):
            _scrape_client_id(session)

    def test_prefers_last_script_tag(self):
        """Scripts are iterated in reverse; the last matching one wins."""
        id_first = "d" * 32
        id_last = "e" * 32
        html = (
            '<script src="https://a-v2.sndcdn.com/assets/first.js"></script>'
            '<script src="https://a-v2.sndcdn.com/assets/last.js"></script>'
        )
        js_pages = {
            "https://a-v2.sndcdn.com/assets/first.js": f',client_id:"{id_first}"',
            "https://a-v2.sndcdn.com/assets/last.js": f',client_id:"{id_last}"',
        }
        session = self._make_session(html, js_pages)

        result = _scrape_client_id(session)
        # reversed iteration means last.js is checked first
        assert result == id_last


# ---------------------------------------------------------------------------
# _load_cached_client_id / _save_client_id
# ---------------------------------------------------------------------------

class TestClientIdCache:
    """Tests for the filesystem cache helpers."""

    def test_save_and_load_roundtrip(self, tmp_path):
        cache_file = tmp_path / "client_id"
        with patch("sc_cli.api._CLIENT_ID_CACHE", cache_file):
            _save_client_id("myid12345678901234567890123456")
            result = _load_cached_client_id()
        assert result == "myid12345678901234567890123456"

    def test_returns_none_when_no_cache_file(self, tmp_path):
        cache_file = tmp_path / "client_id"
        with patch("sc_cli.api._CLIENT_ID_CACHE", cache_file):
            result = _load_cached_client_id()
        assert result is None

    def test_returns_none_when_cache_expired(self, tmp_path):
        cache_file = tmp_path / "client_id"
        cache_file.write_text("expiredid" + "x" * 23)
        # Backdate mtime by 25 hours
        old_mtime = time.time() - 90_000
        import os
        os.utime(cache_file, (old_mtime, old_mtime))

        with patch("sc_cli.api._CLIENT_ID_CACHE", cache_file):
            result = _load_cached_client_id()
        assert result is None

    def test_returns_cached_value_when_fresh(self, tmp_path):
        cache_file = tmp_path / "client_id"
        cache_file.write_text("freshid" + "x" * 25)

        with patch("sc_cli.api._CLIENT_ID_CACHE", cache_file):
            result = _load_cached_client_id()
        assert result == "freshid" + "x" * 25

    def test_save_creates_parent_directories(self, tmp_path):
        cache_file = tmp_path / "deep" / "nested" / "client_id"
        with patch("sc_cli.api._CLIENT_ID_CACHE", cache_file):
            _save_client_id("someid" + "y" * 26)
        assert cache_file.exists()


# ---------------------------------------------------------------------------
# SoundCloudClient._get — 401/403 retry logic
# ---------------------------------------------------------------------------

class TestClientGet:
    """Tests for the automatic client_id refresh on 401/403."""

    def _make_client_with_id(self, client_id: str = "x" * 32) -> SoundCloudClient:
        client = SoundCloudClient()
        client._client_id = client_id
        return client

    def _mock_response(self, status: int, json_data: dict | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data or {}
        if status >= 400:
            http_err = requests.HTTPError(response=resp)
            resp.raise_for_status.side_effect = http_err
        else:
            resp.raise_for_status = MagicMock()
        return resp

    def test_successful_get_returns_json(self):
        client = self._make_client_with_id()
        ok_resp = self._mock_response(200, {"collection": [1, 2, 3]})
        client._session.get = MagicMock(return_value=ok_resp)

        result = client._get("/search/tracks", q="test")
        assert result == {"collection": [1, 2, 3]}

    def test_retries_on_401_with_fresh_client_id(self):
        """On 401, invalidates cached id and retries with a new one."""
        client = self._make_client_with_id("stale" + "a" * 27)

        unauth_resp = self._mock_response(401)
        unauth_resp.raise_for_status = MagicMock()  # don't raise on first call
        unauth_resp.status_code = 401

        ok_resp = self._mock_response(200, {"ok": True})

        client._session.get = MagicMock(side_effect=[unauth_resp, ok_resp])

        new_id = "fresh" + "b" * 27
        with patch.object(client, "_invalidate_client_id") as mock_invalidate, \
             patch.object(type(client), "client_id", new_callable=lambda: property(lambda self: new_id)):
            result = client._get("/resolve", url="https://soundcloud.com/x")

        assert result == {"ok": True}
        mock_invalidate.assert_called_once()

    def test_retries_on_403(self):
        """On 403, same retry behaviour as 401."""
        client = self._make_client_with_id("stale" + "a" * 27)

        forbidden_resp = MagicMock()
        forbidden_resp.status_code = 403
        forbidden_resp.raise_for_status = MagicMock()

        ok_resp = self._mock_response(200, {"ok": True})

        client._session.get = MagicMock(side_effect=[forbidden_resp, ok_resp])

        new_id = "fresh" + "b" * 27
        with patch.object(client, "_invalidate_client_id"), \
             patch.object(type(client), "client_id", new_callable=lambda: property(lambda self: new_id)):
            result = client._get("/resolve", url="https://soundcloud.com/x")

        assert result == {"ok": True}

    def test_raises_on_404(self):
        """Non-auth errors are raised without retry."""
        client = self._make_client_with_id()
        not_found_resp = self._mock_response(404)
        client._session.get = MagicMock(return_value=not_found_resp)

        with pytest.raises(requests.HTTPError):
            client._get("/resolve", url="https://soundcloud.com/missing")


# ---------------------------------------------------------------------------
# SoundCloudClient.get_stream_url — format prioritization
# ---------------------------------------------------------------------------

class TestGetStreamUrl:
    """Tests for the transcoding priority/scoring logic."""

    _tc_counter = 0

    def _make_transcoding(self, protocol: str, mime: str, stream_url: str) -> dict:
        TestGetStreamUrl._tc_counter += 1
        return {
            "url": f"https://api-v2.soundcloud.com/media/tc{TestGetStreamUrl._tc_counter}",
            "format": {"protocol": protocol, "mime_type": mime},
            "_stream_url": stream_url,  # used by fake session below
        }

    def _make_client_with_transcodings(self, transcodings: list[dict]) -> SoundCloudClient:
        client = SoundCloudClient()
        client._client_id = "x" * 32

        def fake_get(url, params=None, **kwargs):
            for tc in transcodings:
                if tc.get("url") == url:
                    resp = MagicMock()
                    resp.json.return_value = {"url": tc["_stream_url"]}
                    return resp
            resp = MagicMock()
            resp.json.return_value = {}
            return resp

        client._session.get = MagicMock(side_effect=fake_get)
        return client

    def test_prefers_progressive_mp3_over_hls(self):
        tcs = [
            self._make_transcoding("hls", "audio/mpeg", "https://hls.example/stream.m3u8"),
            self._make_transcoding("progressive", "audio/mpeg", "https://cdn.example/track.mp3"),
        ]
        client = self._make_client_with_transcodings(tcs)
        track = {"media": {"transcodings": tcs}}

        result = client.get_stream_url(track)
        assert result == "https://cdn.example/track.mp3"

    def test_prefers_progressive_opus_over_hls(self):
        tcs = [
            self._make_transcoding("hls", "audio/ogg; codecs=\"opus\"", "https://hls.example/stream.m3u8"),
            self._make_transcoding("progressive", "audio/ogg; codecs=\"opus\"", "https://cdn.example/track.opus"),
        ]
        client = self._make_client_with_transcodings(tcs)
        track = {"media": {"transcodings": tcs}}

        result = client.get_stream_url(track)
        assert result == "https://cdn.example/track.opus"

    def test_falls_back_to_hls_when_no_progressive(self):
        tcs = [
            self._make_transcoding("hls", "audio/mpeg", "https://hls.example/stream.m3u8"),
        ]
        client = self._make_client_with_transcodings(tcs)
        track = {"media": {"transcodings": tcs}}

        result = client.get_stream_url(track)
        assert result == "https://hls.example/stream.m3u8"

    def test_returns_none_for_empty_transcodings(self):
        client = SoundCloudClient()
        client._client_id = "x" * 32
        track = {"media": {"transcodings": []}}

        result = client.get_stream_url(track)
        assert result is None

    def test_returns_none_for_missing_media_key(self):
        client = SoundCloudClient()
        client._client_id = "x" * 32
        track = {}

        result = client.get_stream_url(track)
        assert result is None

    def test_skips_transcoding_with_no_url_field(self):
        tc_no_url = {"format": {"protocol": "progressive", "mime_type": "audio/mpeg"}}
        tc_with_url = self._make_transcoding("hls", "audio/mpeg", "https://hls.example/ok.m3u8")
        client = self._make_client_with_transcodings([tc_with_url])
        track = {"media": {"transcodings": [tc_no_url, tc_with_url]}}

        result = client.get_stream_url(track)
        assert result == "https://hls.example/ok.m3u8"

    def test_skips_transcoding_when_request_fails(self):
        tcs = [
            self._make_transcoding("progressive", "audio/mpeg", "https://cdn.example/track.mp3"),
            self._make_transcoding("hls", "audio/mpeg", "https://hls.example/stream.m3u8"),
        ]
        client = SoundCloudClient()
        client._client_id = "x" * 32

        call_count = 0

        def fake_get(url, params=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests.RequestException("network error")
            resp = MagicMock()
            resp.json.return_value = {"url": "https://hls.example/stream.m3u8"}
            return resp

        client._session.get = MagicMock(side_effect=fake_get)
        track = {"media": {"transcodings": tcs}}

        result = client.get_stream_url(track)
        assert result == "https://hls.example/stream.m3u8"


# ---------------------------------------------------------------------------
# SoundCloudClient.track_likes — auth error swallowing
# ---------------------------------------------------------------------------

class TestTrackLikes:
    def test_returns_empty_list_on_http_error(self):
        client = SoundCloudClient()
        client._client_id = "x" * 32

        resp = MagicMock()
        resp.status_code = 401
        http_err = requests.HTTPError(response=resp)
        resp.raise_for_status.side_effect = http_err

        with patch.object(client, "_get", side_effect=requests.HTTPError(response=resp)):
            result = client.track_likes()

        assert result == []

    def test_returns_collection_on_success(self):
        client = SoundCloudClient()
        client._client_id = "x" * 32

        fake_tracks = [{"id": 1}, {"id": 2}]
        with patch.object(client, "_get", return_value={"collection": fake_tracks}):
            result = client.track_likes()

        assert result == fake_tracks
