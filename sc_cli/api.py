"""SoundCloud API client using the unofficial public API."""

import re
import time
from pathlib import Path

import requests

_API_V2 = "https://api-v2.soundcloud.com"
_SC_URL = "https://soundcloud.com"
_CLIENT_ID_CACHE = Path.home() / ".cache" / "sc_cli" / "client_id"
_CACHE_TTL = 86_400  # 24 hours

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://soundcloud.com/",
}


# ---------------------------------------------------------------------------
# client_id resolution
# ---------------------------------------------------------------------------

def _scrape_client_id(session: requests.Session) -> str:
    """Scrape a valid client_id from SoundCloud's public JavaScript bundle."""
    resp = session.get(_SC_URL, timeout=15)
    resp.raise_for_status()

    # Collect asset JS URLs (order matters – prefer later ones which are bigger)
    js_urls = re.findall(
        r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"',
        resp.text,
    )

    for url in reversed(js_urls):
        try:
            js = session.get(url, timeout=15).text
            m = re.search(r'[,{(]client_id:"([a-zA-Z0-9]{32})"', js)
            if m:
                return m.group(1)
        except requests.RequestException:
            continue

    raise RuntimeError(
        "Could not extract SoundCloud client_id. "
        "The page structure may have changed."
    )


def _load_cached_client_id() -> str | None:
    if _CLIENT_ID_CACHE.exists():
        age = time.time() - _CLIENT_ID_CACHE.stat().st_mtime
        if age < _CACHE_TTL:
            return _CLIENT_ID_CACHE.read_text().strip()
    return None


def _save_client_id(client_id: str) -> None:
    _CLIENT_ID_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CLIENT_ID_CACHE.write_text(client_id)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class SoundCloudClient:
    """Thin wrapper around the SoundCloud API v2."""

    def __init__(self) -> None:
        self._client_id: str | None = None
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    # -- client_id -----------------------------------------------------------

    @property
    def client_id(self) -> str:
        if not self._client_id:
            cached = _load_cached_client_id()
            if cached:
                self._client_id = cached
            else:
                self._client_id = _scrape_client_id(self._session)
                _save_client_id(self._client_id)
        return self._client_id

    def _invalidate_client_id(self) -> None:
        _CLIENT_ID_CACHE.unlink(missing_ok=True)
        self._client_id = None

    # -- HTTP ----------------------------------------------------------------

    def _get(self, endpoint: str, **params) -> dict:
        params["client_id"] = self.client_id
        url = f"{_API_V2}{endpoint}"
        resp = self._session.get(url, params=params, timeout=15)

        if resp.status_code in (401, 403):
            # client_id stale – fetch a fresh one and retry once
            self._invalidate_client_id()
            params["client_id"] = self.client_id
            resp = self._session.get(url, params=params, timeout=15)

        resp.raise_for_status()
        return resp.json()

    # -- public API ----------------------------------------------------------

    def resolve(self, url: str) -> dict:
        """Resolve a SoundCloud permalink to its API representation."""
        return self._get("/resolve", url=url)

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        data = self._get("/search/tracks", q=query, limit=limit)
        return data.get("collection", [])

    def search_users(self, query: str, limit: int = 10) -> list[dict]:
        data = self._get("/search/users", q=query, limit=limit)
        return data.get("collection", [])

    def search_playlists(self, query: str, limit: int = 10) -> list[dict]:
        data = self._get("/search/playlists", q=query, limit=limit)
        return data.get("collection", [])

    def get_stream_url(self, track: dict) -> str | None:
        """Return a playable HTTP stream URL for a track."""
        transcodings = track.get("media", {}).get("transcodings", [])

        # Prefer progressive MP3, then progressive AAC, then HLS
        order = [
            ("progressive", "audio/mpeg"),
            ("progressive", "audio/ogg; codecs=\"opus\""),
            ("progressive", None),
            ("hls", None),
        ]

        def _score(tc: dict) -> int:
            fmt = tc.get("format", {})
            proto = fmt.get("protocol", "")
            mime = fmt.get("mime_type", "")
            for rank, (p, m) in enumerate(order):
                if proto == p and (m is None or mime == m):
                    return rank
            return len(order)

        for tc in sorted(transcodings, key=_score):
            url = tc.get("url")
            if not url:
                continue
            try:
                data = self._session.get(
                    url,
                    params={"client_id": self.client_id},
                    timeout=15,
                ).json()
                stream_url = data.get("url")
                if stream_url:
                    return stream_url
            except requests.RequestException:
                continue

        return None

    def track_likes(self, limit: int = 10) -> list[dict]:
        """Fetch liked tracks (requires login – returns [] if not authenticated)."""
        try:
            data = self._get("/me/likes/tracks", limit=limit)
            return data.get("collection", [])
        except requests.HTTPError:
            return []
