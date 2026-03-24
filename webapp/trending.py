"""
webapp/trending.py — New & Trending section: fetch, merge, and TTL-cache.

Sources (controlled by TRENDING_FEEDS env var, comma-separated):
  spotify   — Spotify New Releases API (requires SPOTIFY_CLIENT_ID/SECRET)
  lastfm    — Last.fm chart: top artists → their top albums
  bandcamp  — Bandcamp Daily RSS feed (best-effort editorial title parsing)
"""
import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import httpx

from webapp.normalize import normalize_album_title, normalize_text
from webapp.spotify import SPOTIFY_ENABLED, _get_spotify_token

logger = logging.getLogger(__name__)

_LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")

_NAVIDROME_URL  = os.environ.get("NAVIDROME_URL",  "").strip()
_NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "").strip()
_NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "").strip()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TRENDING_FILE = DATA_DIR / "trending_albums.json"

_TRENDING_FEEDS_RAW = os.environ.get("TRENDING_FEEDS", "spotify,lastfm,bandcamp")
TRENDING_FEEDS: list[str] = [f.strip() for f in _TRENDING_FEEDS_RAW.split(",") if f.strip()]

_TRENDING_TTL = 3600  # seconds
_trending_cache: dict = {"items": [], "expires_at": 0.0}
_library_cache: dict = {"keys": set(), "expires_at": 0.0}

# Bandcamp Daily title patterns (structured editorial formats)
_BC_STRUCTURED_RE = re.compile(
    r"(?:Album of the Day|Stream|New Album|Premiere):\s*(.+?)\s*[–—-]\s*(.+)",
    re.IGNORECASE,
)
_BC_EMDASH_RE = re.compile(r"^(.+?)\s*[–—]\s*(.+)$")  # em-dash split only (less ambiguous)


def _search_urls(artist: str, album: str) -> dict:
    q = quote_plus(f"{artist} {album}".strip())
    return {
        "discogs_url": f"https://www.discogs.com/search/?q={q}&type=release",
        "bandcamp_url": f"https://bandcamp.com/search?q={q}",
        "youtube_url": f"https://music.youtube.com/search?q={q}",
    }


async def fetch_spotify_new_releases() -> list[dict]:
    if not SPOTIFY_ENABLED:
        return []
    token = await _get_spotify_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://api.spotify.com/v1/browse/new-releases",
                params={"limit": 20, "country": "US"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        if r.status_code != 200:
            logger.warning("Spotify new-releases failed: HTTP %s", r.status_code)
            return []
        results = []
        for album in r.json().get("albums", {}).get("items", []):
            artist_display = album["artists"][0]["name"] if album.get("artists") else ""
            album_title = album.get("name", "")
            album_id = album.get("id", "")
            image_url = album["images"][0]["url"] if album.get("images") else None
            results.append({
                "artist_display": artist_display,
                "artist_normalized": normalize_text(artist_display),
                "album_title": album_title,
                "album_normalized": normalize_album_title(album_title),
                "image_url": image_url,
                "release_date": album.get("release_date", ""),
                "source": "spotify",
                "source_url": f"https://open.spotify.com/album/{album_id}",
                **_search_urls(artist_display, album_title),
            })
        return results
    except Exception:
        logger.exception("Error fetching Spotify new releases")
        return []


async def fetch_lastfm_trending() -> list[dict]:
    if not _LASTFM_API_KEY:
        return []
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method": "chart.getTopArtists",
                    "api_key": _LASTFM_API_KEY,
                    "format": "json",
                    "limit": 20,
                },
                timeout=10,
            )
        if r.status_code != 200:
            logger.warning("Last.fm chart.getTopArtists failed: HTTP %s", r.status_code)
            return []
        top_artists = r.json().get("artists", {}).get("artist", [])

        results = []
        async with httpx.AsyncClient() as c:
            for artist in top_artists[:15]:
                artist_name = artist.get("name", "")
                if not artist_name:
                    continue
                try:
                    r2 = await c.get(
                        "https://ws.audioscrobbler.com/2.0/",
                        params={
                            "method": "artist.getTopAlbums",
                            "artist": artist_name,
                            "api_key": _LASTFM_API_KEY,
                            "format": "json",
                            "limit": 3,
                        },
                        timeout=8,
                    )
                    if r2.status_code != 200:
                        continue
                    albums = r2.json().get("topalbums", {}).get("album", [])
                    for album in albums[:1]:  # top album per artist only
                        album_title = album.get("name", "")
                        image_url = None
                        for img in reversed(album.get("image", [])):
                            if img.get("#text"):
                                image_url = img["#text"]
                                break
                        results.append({
                            "artist_display": artist_name,
                            "artist_normalized": normalize_text(artist_name),
                            "album_title": album_title,
                            "album_normalized": normalize_album_title(album_title),
                            "image_url": image_url,
                            "release_date": None,
                            "source": "lastfm",
                            "source_url": album.get("url", ""),
                            **_search_urls(artist_name, album_title),
                        })
                except Exception:
                    logger.exception("Error fetching Last.fm albums for %s", artist_name)
        return results
    except Exception:
        logger.exception("Error fetching Last.fm trending")
        return []


async def fetch_bandcamp_daily() -> list[dict]:
    try:
        feed = feedparser.parse("https://daily.bandcamp.com/feed")
        if not feed.entries:
            return []
        results = []
        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            link = entry.get("link", "")

            # Best-effort structured parsing
            artist_display = ""
            album_title = title
            m = _BC_STRUCTURED_RE.search(title)
            if not m:
                m = _BC_EMDASH_RE.match(title)
            if m:
                artist_display = m.group(1).strip()
                album_title = m.group(2).strip()

            # Skip entries we couldn't parse into artist + album (editorial articles, etc.)
            if not artist_display:
                continue

            # Extract cover image
            image_url = None
            media_thumbnails = getattr(entry, "media_thumbnail", None)
            if media_thumbnails:
                image_url = media_thumbnails[0].get("url")
            if not image_url:
                for enc in getattr(entry, "enclosures", []):
                    if enc.get("type", "").startswith("image/"):
                        image_url = enc.get("href")
                        break
            if not image_url:
                img_match = re.search(
                    r'<img[^>]+src=["\']([^"\']+)["\']',
                    entry.get("summary", ""),
                )
                if img_match:
                    image_url = img_match.group(1)

            results.append({
                "artist_display": artist_display,
                "artist_normalized": normalize_text(artist_display),
                "album_title": album_title,
                "album_normalized": normalize_album_title(album_title),
                "image_url": image_url,
                "release_date": None,
                "source": "bandcamp",
                "source_url": link,
                **_search_urls(artist_display, album_title),
            })
        return results
    except Exception:
        logger.exception("Error fetching Bandcamp Daily RSS")
        return []


async def get_local_library() -> set[str]:
    """Return owned album keys ('artist_normalized|album_normalized') from Navidrome, TTL-cached."""
    now = time.monotonic()
    if _library_cache["keys"] and now < _library_cache["expires_at"]:
        return _library_cache["keys"]

    if not (_NAVIDROME_URL and _NAVIDROME_USER and _NAVIDROME_PASS):
        return set()

    salt = secrets.token_hex(6)
    token = hashlib.md5(f"{_NAVIDROME_PASS}{salt}".encode()).hexdigest()
    auth = {
        "u": _NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "cratedigger",
        "f": "json",
    }
    endpoint = f"{_NAVIDROME_URL.rstrip('/')}/rest/getAlbumList2.view"
    owned: set[str] = set()
    size, offset = 500, 0

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                params = {
                    **auth,
                    "type": "alphabeticalByArtist",
                    "size": str(size),
                    "offset": str(offset),
                }
                r = await client.get(endpoint, params=params)
                r.raise_for_status()
                sr = r.json().get("subsonic-response", {})
                if sr.get("status") != "ok":
                    logger.warning("Navidrome library fetch error: %s", sr.get("error", {}))
                    break
                album_list = sr.get("albumList2", {}).get("album", [])
                for album in album_list:
                    album_name = album.get("name") or album.get("title", "")
                    artist_name = album.get("albumArtist") or album.get("artist", "")
                    if album_name and artist_name:
                        key = f"{normalize_text(artist_name)}|{normalize_album_title(album_name)}"
                        owned.add(key)
                if len(album_list) < size:
                    break
                offset += size
    except Exception:
        logger.exception("Error fetching local library from Navidrome")
        return set()

    _library_cache["keys"] = owned
    _library_cache["expires_at"] = now + _TRENDING_TTL
    logger.info("Navidrome library loaded: %d albums", len(owned))
    return owned


def merge_and_deduplicate(sources: list[list[dict]]) -> list[dict]:
    """Interleave sources by index for variety; deduplicate on artist+album key."""
    seen: set[str] = set()
    merged: list[dict] = []
    max_len = max((len(s) for s in sources), default=0)
    for i in range(max_len):
        for source in sources:
            if i >= len(source):
                continue
            item = source[i]
            key = f"{item['artist_normalized']}|{item['album_normalized']}"
            if key not in seen and item["album_normalized"]:
                seen.add(key)
                merged.append(item)
    return merged


async def get_trending(force: bool = False) -> list[dict]:
    """Return merged trending items using a 1-hour in-memory TTL cache."""
    now = time.monotonic()
    if not force and _trending_cache["items"] and now < _trending_cache["expires_at"]:
        return _trending_cache["items"]

    _fetchers = {
        "spotify": fetch_spotify_new_releases,
        "lastfm": fetch_lastfm_trending,
        "bandcamp": fetch_bandcamp_daily,
    }
    sources = []
    for feed_name in TRENDING_FEEDS:
        if feed_name in _fetchers:
            sources.append(await _fetchers[feed_name]())

    merged = merge_and_deduplicate(sources)

    owned = await get_local_library()
    if owned:
        merged = [
            item for item in merged
            if f"{item['artist_normalized']}|{item['album_normalized']}" not in owned
        ]

    _trending_cache["items"] = merged
    _trending_cache["expires_at"] = now + _TRENDING_TTL

    try:
        TRENDING_FILE.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "total_items": len(merged),
                    "items": merged,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Could not write %s", TRENDING_FILE)

    return merged
