"""
webapp/spotify.py — Spotify OAuth Client Credentials helpers.

Extracted from app.py so that both app.py and trending.py can share the
token cache and search helper without circular imports.
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
_SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

SPOTIFY_ENABLED = bool(_SPOTIFY_CLIENT_ID and _SPOTIFY_CLIENT_SECRET)

_spotify_token: dict = {}   # {"token": str, "expires_at": float}


async def _get_spotify_token() -> str | None:
    if not SPOTIFY_ENABLED:
        return None
    now = time.monotonic()
    if _spotify_token.get("token") and now < _spotify_token.get("expires_at", 0):
        return _spotify_token["token"]
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(_SPOTIFY_CLIENT_ID, _SPOTIFY_CLIENT_SECRET),
            timeout=5,
        )
    if r.status_code != 200:
        logger.warning("Spotify token fetch failed: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    d = r.json()
    _spotify_token.update(
        token=d["access_token"],
        expires_at=now + d.get("expires_in", 3600) - 60,
    )
    return _spotify_token["token"]


async def _search_spotify(artist: str, album: str) -> str | None:
    token = await _get_spotify_token()
    if not token:
        return None
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "https://api.spotify.com/v1/search",
            params={"q": f"album:{album} artist:{artist}", "type": "album", "limit": 1},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
    if r.status_code != 200:
        logger.warning("Spotify search failed: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    items = r.json().get("albums", {}).get("items", [])
    return f"https://open.spotify.com/embed/album/{items[0]['id']}" if items else None
