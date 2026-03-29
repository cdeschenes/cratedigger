"""
webapp/lastfm_client.py — Reusable async Last.fm API client for webapp modules.

Mirrors the retry/backoff pattern from missing_popular_albums.py but as a
self-contained webapp module so discovery.py doesn't import the top-level script.
"""

import asyncio
import logging
import random
import time

import httpx

_LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
_MAX_RETRIES = 3
_REQUEST_DELAY = (0.15, 0.30)  # polite delay between API calls

logger = logging.getLogger(__name__)


class LastFMError(Exception):
    pass


class LastFMClient:
    """Async Last.fm API client with retry, backoff, and rate-limit handling."""

    def __init__(self, api_key: str, session: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._session = session
        self._rng = random.Random(time.time_ns())

    async def _request(self, params: dict) -> dict:
        full_params = {**params, "api_key": self._api_key, "format": "json"}
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            await asyncio.sleep(self._rng.uniform(*_REQUEST_DELAY))
            try:
                resp = await self._session.get(_LASTFM_BASE, params=full_params)
                if resp.status_code == 429:
                    raise LastFMError("Rate limited by Last.fm")
                resp.raise_for_status()
                payload = resp.json()
                if "error" in payload:
                    raise LastFMError(payload.get("message", "Unknown Last.fm error"))
                return payload
            except (httpx.HTTPError, ValueError, LastFMError) as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    break
                backoff = 0.5 * (2 ** (attempt - 1)) + self._rng.uniform(0, 0.25)
                logger.debug("Last.fm retry %d/%d in %.2fs: %s", attempt, _MAX_RETRIES, backoff, exc)
                await asyncio.sleep(backoff)
        raise LastFMError(str(last_exc or "Unknown Last.fm error"))

    async def user_top_artists(
        self,
        username: str,
        period: str = "1month",
        limit: int = 200,
    ) -> list[dict]:
        """
        Fetch a user's top artists for a given period.

        period: '7day' | '1month' | '3month' | '6month' | '12month' | 'overall'
        Returns list of dicts with keys: name, playcount, rank (all strings).
        """
        try:
            resp = await self._request({
                "method": "user.getTopArtists",
                "user": username,
                "period": period,
                "limit": str(limit),
            })
        except LastFMError as exc:
            logger.warning("user.getTopArtists failed for %s (%s): %s", username, period, exc)
            return []

        raw = resp.get("topartists", {}).get("artist", [])
        if isinstance(raw, dict):
            raw = [raw]
        return [
            {
                "name": a.get("name", ""),
                "playcount": int(a.get("playcount", 0)),
                "rank": int(a.get("@attr", {}).get("rank", 0)),
            }
            for a in raw
            if isinstance(a, dict) and a.get("name")
        ]

    async def artist_similar(self, artist: str, limit: int = 30) -> list[dict]:
        """
        Fetch similar artists for a given artist.
        Returns list of dicts with keys: name, match (float 0-1).
        """
        try:
            resp = await self._request({
                "method": "artist.getSimilar",
                "artist": artist,
                "autocorrect": "1",
                "limit": str(limit),
            })
        except LastFMError as exc:
            logger.debug("artist.getSimilar failed for %s: %s", artist, exc)
            return []

        raw = resp.get("similarartists", {}).get("artist", [])
        if isinstance(raw, dict):
            raw = [raw]
        return [
            {
                "name": a.get("name", ""),
                "match": float(a.get("match", 0)),
            }
            for a in raw
            if isinstance(a, dict) and a.get("name")
        ]

    async def artist_top_tags(self, artist: str, top_n: int = 10) -> list[str]:
        """
        Fetch the top genre tags for a given artist.
        Returns list of lowercase tag names (up to top_n).
        """
        try:
            resp = await self._request({
                "method": "artist.getTopTags",
                "artist": artist,
                "autocorrect": "1",
            })
        except LastFMError as exc:
            logger.debug("artist.getTopTags failed for %s: %s", artist, exc)
            return []

        raw = resp.get("toptags", {}).get("tag", [])
        if isinstance(raw, dict):
            raw = [raw]
        return [
            t["name"].strip().lower()
            for t in raw[:top_n]
            if isinstance(t, dict) and t.get("name")
        ]

    async def artist_top_albums(self, artist: str, limit: int = 5) -> list[dict]:
        """
        Fetch the top albums for a given artist.
        Returns list of dicts with keys: name, playcount.
        """
        try:
            resp = await self._request({
                "method": "artist.getTopAlbums",
                "artist": artist,
                "autocorrect": "1",
                "limit": str(limit),
            })
        except LastFMError as exc:
            logger.debug("artist.getTopAlbums failed for %s: %s", artist, exc)
            return []

        raw = resp.get("topalbums", {}).get("album", [])
        if isinstance(raw, dict):
            raw = [raw]
        return [
            {
                "name": a.get("name", ""),
                "playcount": int(a.get("playcount", 0)),
            }
            for a in raw
            if isinstance(a, dict) and a.get("name")
        ]
