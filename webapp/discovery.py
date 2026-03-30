"""
webapp/discovery.py — Taste-aware New & Trending discovery pipeline.

Replaces webapp/trending.py with a SQLite-backed engine that:
  1. Fetches from up to 9 sources (Spotify, Last.fm, Bandcamp, AOTY, Juno ×4, ListenBrainz)
  2. Deduplicates releases into a SQLite DB
  3. Builds a taste profile from Last.fm scrobble history (user.getTopArtists)
  4. Scores each release against the taste profile
  5. Assigns releases to one of 3 sections with a human-readable reason

Public API:
  get_discovery_results(force=False) -> dict
    Returns {new_from_artists, trending_near_taste, genre_picks, generated_at, total_items}

  refresh_taste_profile() -> None
    Forces an immediate taste profile rebuild (bypasses 24h TTL).

Env vars:
  LASTFM_API_KEY         — required for Last.fm sources
  LASTFM_USERNAME        — required for taste profile (user.getTopArtists)
  LISTENBRAINZ_USERNAME  — enables ListenBrainz fresh-releases feed
  SPOTIFY_CLIENT_ID/SECRET — enables Spotify source
  NAVIDROME_*            — enables owned-album filtering
  DISCOVERY_FEEDS        — comma-separated source list (default: all enabled sources)
  DATA_DIR               — base directory for DB and cache (default: /data)
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import httpx

from webapp.discovery_db import (
    add_source,
    clear_scores,
    init_db,
    load_all_releases_with_sources,
    load_scored_releases,
    load_taste_cache,
    prune_old_releases,
    save_scores,
    save_taste_cache,
    upsert_release,
)
from webapp.lastfm_client import LastFMClient
from webapp.normalize import normalize_album_title, normalize_text
from webapp.spotify import SPOTIFY_ENABLED, _get_spotify_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_LASTFM_API_KEY      = os.environ.get("LASTFM_API_KEY", "")
_LASTFM_USERNAME     = os.environ.get("LASTFM_USERNAME", "").strip()
_LISTENBRAINZ_USERNAME = os.environ.get("LISTENBRAINZ_USERNAME", "").strip()

_NAVIDROME_URL  = os.environ.get("NAVIDROME_URL",  "").strip()
_NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "").strip()
_NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "").strip()

DATA_DIR       = Path(os.environ.get("DATA_DIR", "/data"))
_DB_PATH       = DATA_DIR / "discovery.db"
_RESULTS_FILE  = DATA_DIR / "discovery_results.json"

_DISCOVERY_TTL = 7200   # 2 hours — in-memory + disk result cache
_TASTE_TTL     = 86400  # 24 hours — taste profile rebuilt once per day

_ALL_FEEDS = [
    "spotify", "lastfm", "bandcamp",
    "juno_electronic", "juno_hiphop", "juno_rock", "juno_main",
    "listenbrainz",
]
_DISCOVERY_FEEDS_RAW = os.environ.get("DISCOVERY_FEEDS", ",".join(_ALL_FEEDS))
DISCOVERY_FEEDS: list[str] = [f.strip() for f in _DISCOVERY_FEEDS_RAW.split(",") if f.strip()]

# Juno genre feed URLs (slug may need adjustment if Juno changes their URL structure)
_JUNO_FEED_URLS: dict[str, str] = {
    "juno_electronic": "https://www.juno.co.uk/dance-and-electronic-music/feeds/rss",
    "juno_hiphop":     "https://www.juno.co.uk/hip-hop/feeds/rss",
    "juno_rock":       "https://www.juno.co.uk/rock/feeds/rss",
    "juno_main":       "https://www.juno.co.uk/all/feeds/rss",
}

# AOTY no longer exposes an album-release RSS feed — /rss/ returns an HTML index page.
# Keeping the source name and fetcher in the map so existing configs don't break,
# but the fetch returns empty and logs a one-time warning.
_AOTY_FEED_URL = ""

# Bandcamp Daily title patterns (migrated from trending.py)
_BC_COMMA_QUOTE_RE = re.compile(r'^(.+?),\s*[\u201c"](.+?)[\u201d"]')
_BC_STRUCTURED_RE  = re.compile(
    r"(?:Album of the Day|Stream|New Album|Premiere):\s*(.+?)\s*[–—-]\s*(.+)",
    re.IGNORECASE,
)
_BC_EMDASH_RE = re.compile(r"^(.+?)\s*[–—]\s*(.+)$")

# Browser-style User-Agent for feeds that block default Python agents
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_discovery_cache: dict = {"result": None, "expires_at": 0.0}
_library_cache:   dict = {"keys": set(), "expires_at": 0.0}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RawItem:
    artist_display:    str
    artist_normalized: str
    album_title:       str
    album_normalized:  str
    source_name:       str
    release_date:      str | None = None
    image_url:         str | None = None
    item_url:          str | None = None


def _search_urls(artist: str, album: str) -> dict:
    q = quote_plus(f"{artist} {album}".strip())
    return {
        "discogs_url": f"https://www.discogs.com/search/?q={q}&type=release",
        "bandcamp_url": f"https://bandcamp.com/search?q={q}",
        "youtube_url":  f"https://music.youtube.com/search?q={q}",
    }


def _raw(
    artist: str,
    album: str,
    source_name: str,
    release_date: str | None = None,
    image_url: str | None = None,
    item_url: str | None = None,
) -> RawItem | None:
    """Normalise and validate an artist/album pair; return None if either is empty."""
    artist = artist.strip()
    album  = album.strip()
    if not artist or not album:
        return None
    return RawItem(
        artist_display    = artist,
        artist_normalized = normalize_text(artist),
        album_title       = album,
        album_normalized  = normalize_album_title(album),
        source_name       = source_name,
        release_date      = release_date,
        image_url         = image_url,
        item_url          = item_url,
    )


# ---------------------------------------------------------------------------
# Source fetchers
# ---------------------------------------------------------------------------

async def _fetch_spotify() -> list[RawItem]:
    if not SPOTIFY_ENABLED:
        return []
    token = await _get_spotify_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(
                "https://api.spotify.com/v1/browse/new-releases",
                params={"limit": 20, "country": "US"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code != 200:
            logger.warning("Spotify new-releases HTTP %s", r.status_code)
            return []
        items = []
        for album in r.json().get("albums", {}).get("items", []):
            artist = album["artists"][0]["name"] if album.get("artists") else ""
            title  = album.get("name", "")
            album_id = album.get("id", "")
            image = album["images"][0]["url"] if album.get("images") else None
            item = _raw(
                artist, title, "spotify",
                release_date=album.get("release_date"),
                image_url=image,
                item_url=f"https://open.spotify.com/album/{album_id}",
            )
            if item:
                items.append(item)
        logger.info("Spotify: %d releases", len(items))
        return items
    except Exception:
        logger.exception("Error fetching Spotify new releases")
        return []


async def _fetch_lastfm_chart() -> list[RawItem]:
    """Last.fm chart.getTopArtists → top album per artist."""
    if not _LASTFM_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method": "chart.getTopArtists",
                    "api_key": _LASTFM_API_KEY,
                    "format": "json",
                    "limit": 20,
                },
            )
        if r.status_code != 200:
            logger.warning("Last.fm chart.getTopArtists HTTP %s", r.status_code)
            return []
        top_artists = r.json().get("artists", {}).get("artist", [])
        items = []
        async with httpx.AsyncClient(timeout=10) as c:
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
                    )
                    albums = r2.json().get("topalbums", {}).get("album", []) if r2.status_code == 200 else []
                    for album in albums[:1]:
                        title = album.get("name", "")
                        image_url = None
                        for img in reversed(album.get("image", [])):
                            if img.get("#text"):
                                image_url = img["#text"]
                                break
                        item = _raw(
                            artist_name, title, "lastfm",
                            image_url=image_url,
                            item_url=album.get("url"),
                        )
                        if item:
                            items.append(item)
                except Exception:
                    logger.debug("Last.fm album fetch failed for %s", artist_name)
        logger.info("Last.fm chart: %d releases", len(items))
        return items
    except Exception:
        logger.exception("Error fetching Last.fm trending")
        return []


async def _fetch_bandcamp() -> list[RawItem]:
    try:
        feed = feedparser.parse("https://daily.bandcamp.com/feed")
        if not feed.entries:
            return []
        items = []
        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            link  = entry.get("link", "")
            artist_display = ""
            album_title    = title
            m = _BC_COMMA_QUOTE_RE.match(title)
            if not m:
                m = _BC_STRUCTURED_RE.search(title)
            if not m:
                m = _BC_EMDASH_RE.match(title)
            if m:
                artist_display = m.group(1).strip()
                album_title    = m.group(2).strip()
            if not artist_display:
                continue
            image_url = None
            for thumb in getattr(entry, "media_thumbnail", []):
                image_url = thumb.get("url")
                break
            if not image_url:
                for enc in getattr(entry, "enclosures", []):
                    if enc.get("type", "").startswith("image/"):
                        image_url = enc.get("href")
                        break
            if not image_url:
                m2 = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", ""))
                if m2:
                    image_url = m2.group(1)
            item = _raw(artist_display, album_title, "bandcamp", image_url=image_url, item_url=link)
            if item:
                items.append(item)
        logger.info("Bandcamp Daily: %d releases", len(items))
        return items
    except Exception:
        logger.exception("Error fetching Bandcamp Daily RSS")
        return []


async def _fetch_generic_rss(source_name: str, feed_url: str, limit: int = 20) -> list[RawItem]:
    """
    Generic RSS/Atom parser for AOTY and Juno feeds.

    Title parsing tries (in order):
      1. "Artist - Album"  (em-dash variants)
      2. "Album by Artist" (ListenBrainz / some AOTY entries)
      3. Full title as album, empty artist → skipped
    """
    try:
        # feedparser with a real User-Agent to avoid 403s
        response_text: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=15,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            ) as c:
                r = await c.get(feed_url)
                if r.status_code == 200:
                    response_text = r.text
                else:
                    logger.warning("%s feed HTTP %s — skipping", source_name, r.status_code)
                    return []
        except Exception:
            logger.warning("%s feed unreachable — skipping", source_name)
            return []

        feed = feedparser.parse(response_text)
        if not feed.entries:
            logger.info("%s: no entries parsed", source_name)
            return []

        items = []
        for entry in feed.entries[:limit]:
            raw_title = entry.get("title", "").strip()
            link      = entry.get("link", "")
            pub_date  = None
            for date_field in ("published", "updated", "created"):
                val = entry.get(date_field)
                if val:
                    pub_date = val[:10]  # YYYY-MM-DD
                    break

            # Image: try media:thumbnail, enclosure, then summary HTML
            image_url = None
            for thumb in getattr(entry, "media_thumbnail", []):
                image_url = thumb.get("url")
                break
            if not image_url:
                for enc in getattr(entry, "enclosures", []):
                    if enc.get("type", "").startswith("image/") or enc.get("href", "").endswith((".jpg", ".jpeg", ".png", ".webp")):
                        image_url = enc.get("href") or enc.get("url")
                        break
            if not image_url:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", ""))
                if m:
                    image_url = m.group(1)

            artist, album = _parse_title(raw_title)
            item = _raw(artist, album, source_name, release_date=pub_date, image_url=image_url, item_url=link)
            if item:
                items.append(item)

        logger.info("%s: %d releases", source_name, len(items))
        return items
    except Exception:
        logger.exception("Error fetching %s feed (%s)", source_name, feed_url)
        return []


def _parse_title(title: str) -> tuple[str, str]:
    """
    Extract (artist, album) from common RSS title formats:
      "Artist - Album Title"    →  ("Artist", "Album Title")
      "Artist — Album Title"    →  ("Artist", "Album Title")
      "Album Title by Artist"   →  ("Artist", "Album Title")
      "Artist: Album Title"     →  ("Artist", "Album Title")  (less common)
    Returns ("", title) if no separator is found.
    """
    # "Album by Artist" (ListenBrainz Atom style)
    if " by " in title:
        parts = title.rsplit(" by ", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip(), parts[0].strip()

    # "Artist - Album" / "Artist — Album" / "Artist – Album"
    m = re.match(r"^(.+?)\s*[–—-]\s*(.+)$", title)
    if m:
        # Heuristic: if part before dash is longer, it's probably "Artist - Album"
        return m.group(1).strip(), m.group(2).strip()

    # "Artist: Album"
    if ": " in title:
        parts = title.split(": ", 1)
        return parts[0].strip(), parts[1].strip()

    return "", title


async def _fetch_aoty() -> list[RawItem]:
    if not _AOTY_FEED_URL:
        logger.warning("aoty source disabled — no album-release RSS feed available from albumoftheyear.org")
        return []
    return await _fetch_generic_rss("aoty", _AOTY_FEED_URL, limit=25)


async def _fetch_juno(source_name: str) -> list[RawItem]:
    url = _JUNO_FEED_URLS.get(source_name, "")
    if not url:
        return []
    return await _fetch_generic_rss(source_name, url, limit=20)


async def _fetch_listenbrainz() -> list[RawItem]:
    """
    ListenBrainz fresh-releases Atom feed.
    Title format: "Album Title by Artist Name"
    No cover art in feed — will rely on imgFallback() client-side.
    """
    if not _LISTENBRAINZ_USERNAME:
        return []
    url = f"https://listenbrainz.org/syndication-feed/user/{_LISTENBRAINZ_USERNAME}/fresh-releases"
    return await _fetch_generic_rss("listenbrainz", url, limit=30)


# ---------------------------------------------------------------------------
# Navidrome owned-album filter (migrated from trending.py)
# ---------------------------------------------------------------------------

import hashlib
import secrets as _secrets

async def _get_local_library() -> set[str]:
    """Return 'artist_norm|album_norm' keys for owned albums; TTL-cached 1h."""
    import hashlib, secrets as _s
    now = time.monotonic()
    if _library_cache["keys"] and now < _library_cache["expires_at"]:
        return _library_cache["keys"]
    if not (_NAVIDROME_URL and _NAVIDROME_USER and _NAVIDROME_PASS):
        return set()

    salt  = _secrets.token_hex(6)
    token = hashlib.md5(f"{_NAVIDROME_PASS}{salt}".encode()).hexdigest()
    auth  = {"u": _NAVIDROME_USER, "t": token, "s": salt, "v": "1.16.1", "c": "cratedigger", "f": "json"}
    endpoint = f"{_NAVIDROME_URL.rstrip('/')}/rest/getAlbumList2.view"
    owned: set[str] = set()
    size, offset = 500, 0
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                r = await client.get(endpoint, params={**auth, "type": "alphabeticalByArtist", "size": str(size), "offset": str(offset)})
                r.raise_for_status()
                sr = r.json().get("subsonic-response", {})
                if sr.get("status") != "ok":
                    break
                album_list = sr.get("albumList2", {}).get("album", [])
                for album in album_list:
                    an = album.get("albumArtist") or album.get("artist", "")
                    al = album.get("name") or album.get("title", "")
                    if an and al:
                        owned.add(f"{normalize_text(an)}|{normalize_album_title(al)}")
                if len(album_list) < size:
                    break
                offset += size
    except Exception:
        logger.exception("Error fetching Navidrome library")
        return set()

    _library_cache["keys"]       = owned
    _library_cache["expires_at"] = now + 3600
    logger.info("Navidrome library: %d albums", len(owned))
    return owned


# ---------------------------------------------------------------------------
# Taste profile builder
# ---------------------------------------------------------------------------

_TASTE_IGNORED_TAGS = frozenset({
    "seen live", "favourite", "favorites", "my favorites", "my favourite",
    "love", "awesome", "best", "great", "amazing", "to listen",
    "all", "albums i own", "check out", "fix tags",
    "spotify", "heard on pandora", "pandora", "youtube",
    "under 2000 listeners", "not on spotify",
    "music", "good", "cool", "beautiful", "nice",
})


async def _build_taste_profile(conn) -> dict:
    """
    Build a taste profile from Last.fm scrobbles.

    Blends 3 time periods:
      7day  → weight 0.30  (recent spikes)
      1month → weight 0.40  (medium-term favourites)
      3month → weight 0.30  (longer-term)

    Returns dict with:
      top_artists:     list of {name, normalized, score} sorted desc, top 50
      related_artists: {normalized_name: {display, seeds: [str], weight: float}}
      top_genres:      list of {tag, count} sorted desc, top 20
    """
    if not (_LASTFM_API_KEY and _LASTFM_USERNAME):
        logger.info("Taste profile skipped — LASTFM_API_KEY or LASTFM_USERNAME not set")
        return {}

    logger.info("Building taste profile for Last.fm user: %s", _LASTFM_USERNAME)
    async with httpx.AsyncClient(timeout=20) as session:
        lfm = LastFMClient(_LASTFM_API_KEY, session)

        # Fetch 3 periods concurrently
        period_weights = [("7day", 0.30), ("1month", 0.40), ("3month", 0.30)]
        period_results = await asyncio.gather(*[
            lfm.user_top_artists(_LASTFM_USERNAME, period=p, limit=200)
            for p, _ in period_weights
        ])

        # Weighted score: higher rank = lower score; invert rank
        artist_scores: dict[str, dict] = {}
        for (period, weight), artists in zip(period_weights, period_results):
            total = len(artists)
            for entry in artists:
                name = entry["name"]
                norm = normalize_text(name)
                # Rank 1 = score 1.0, rank N = score ~0
                rank_score = (total - entry["rank"] + 1) / max(total, 1)
                if norm not in artist_scores:
                    artist_scores[norm] = {"display": name, "score": 0.0}
                artist_scores[norm]["score"] += rank_score * weight

        # Top 50 by blended score
        top_50 = sorted(artist_scores.items(), key=lambda x: x[1]["score"], reverse=True)[:50]
        top_artists = [
            {"name": v["display"], "normalized": k, "score": round(v["score"], 4)}
            for k, v in top_50
        ]

        # Similar artists for top 20
        logger.info("Fetching similar artists for top 20...")
        related: dict[str, dict] = {}
        similar_results = await asyncio.gather(*[
            lfm.artist_similar(a["name"], limit=30)
            for a in top_artists[:20]
        ])
        for seed_artist, similars in zip(top_artists[:20], similar_results):
            seed_norm = seed_artist["normalized"]
            seed_weight = seed_artist["score"]
            for sim in similars:
                sim_norm = normalize_text(sim["name"])
                if sim_norm in artist_scores:
                    continue  # already a known artist
                if sim_norm not in related:
                    related[sim_norm] = {
                        "display": sim["name"],
                        "seeds": [],
                        "weight": 0.0,
                    }
                related[sim_norm]["seeds"].append(seed_artist["name"])
                related[sim_norm]["weight"] += seed_weight * float(sim.get("match", 0))

        # Genre tags for top 20
        logger.info("Fetching genre tags for top 20...")
        tag_results = await asyncio.gather(*[
            lfm.artist_top_tags(a["name"], top_n=8)
            for a in top_artists[:20]
        ])
        genre_counts: dict[str, int] = {}
        for tags in tag_results:
            for tag in tags:
                if tag not in _TASTE_IGNORED_TAGS:
                    genre_counts[tag] = genre_counts.get(tag, 0) + 1
        top_genres = sorted(
            [{"tag": t, "count": c} for t, c in genre_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:20]

    profile = {
        "top_artists":     top_artists,
        "related_artists": related,
        "top_genres":      top_genres,
        "lastfm_username": _LASTFM_USERNAME,
    }

    # Persist to DB
    save_taste_cache(conn, {
        "top_artists_json":     json.dumps(top_artists),
        "related_artists_json": json.dumps(related),
        "top_genres_json":      json.dumps(top_genres),
        "lastfm_username":      _LASTFM_USERNAME,
    })
    conn.commit()
    logger.info(
        "Taste profile built: %d top artists, %d related, %d genres",
        len(top_artists), len(related), len(top_genres),
    )
    return profile


async def _load_or_refresh_taste_profile(conn) -> dict:
    """Load taste profile from DB if fresh; rebuild if stale or missing."""
    row = load_taste_cache(conn)
    if row and row.get("computed_at"):
        age = (datetime.now(tz=timezone.utc) - datetime.fromisoformat(row["computed_at"])).total_seconds()
        if age < _TASTE_TTL:
            return {
                "top_artists":     json.loads(row["top_artists_json"] or "[]"),
                "related_artists": json.loads(row["related_artists_json"] or "{}"),
                "top_genres":      json.loads(row["top_genres_json"] or "[]"),
                "lastfm_username": row.get("lastfm_username", ""),
            }
    return await _build_taste_profile(conn)


async def refresh_taste_profile() -> None:
    """Force a taste profile rebuild (bypasses 24h TTL)."""
    conn = init_db(_DB_PATH)
    try:
        await _build_taste_profile(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def _score_releases(
    releases: list[dict],
    taste: dict,
    source_counts: dict[int, list[str]],
) -> list[dict]:
    """
    Score each release against the taste profile.
    Returns a list of score dicts ready for save_scores().
    """
    if not taste:
        # No taste profile — assign uniform trend-only scores
        scored = []
        for r in releases:
            sources = source_counts.get(r["id"], [])
            trend   = 10 if len(sources) >= 3 else (5 if len(sources) >= 2 else 0)
            scored.append({
                "release_id":           r["id"],
                "known_artist_score":   0,
                "related_artist_score": 0,
                "genre_score":          0,
                "trend_score":          trend,
                "recency_score":        _recency_score(r.get("release_date")),
                "total_score":          trend + _recency_score(r.get("release_date")),
                "section":              "genre_picks" if trend > 0 else "",
                "reason_text":          "Trending across multiple discovery sources" if trend else "",
                "_sources": sources,
                "_release": r,
            })
        return scored

    # Build fast-lookup sets
    top_artists_norm = {a["normalized"]: (i, a["score"]) for i, a in enumerate(taste.get("top_artists", []))}
    related_artists  = taste.get("related_artists", {})
    top_genre_tags   = {g["tag"] for g in taste.get("top_genres", [])}

    scored = []
    for r in releases:
        norm    = r["artist_normalized"]
        sources = source_counts.get(r["id"], [])

        # ── known_artist_score ──────────────────────────────────────────────
        known = 0
        if norm in top_artists_norm:
            rank, _ = top_artists_norm[norm]
            known = 40 if rank < 25 else (25 if rank < 100 else 15)

        # ── related_artist_score ────────────────────────────────────────────
        related_score = 0
        related_seeds: list[str] = []
        if norm in related_artists:
            seeds = related_artists[norm].get("seeds", [])
            related_seeds = seeds[:2]
            related_score = 25 if len(seeds) >= 2 else 12

        # ── genre_score (skip if already a known artist) ────────────────────
        genre_score = 0

        # ── trend_score ─────────────────────────────────────────────────────
        n_sources   = len(sources)
        trend_score = 10 if n_sources >= 3 else (5 if n_sources >= 2 else 0)

        # ── recency_score ───────────────────────────────────────────────────
        recency = _recency_score(r.get("release_date"))

        total = known + related_score + genre_score + trend_score + recency

        # ── section assignment ──────────────────────────────────────────────
        if known >= 15:
            section = "new_from_artists"
        elif related_score > 0 or genre_score > 0:
            section = "trending_near_taste"
        elif total > 0:
            section = "genre_picks"
        else:
            section = ""

        scored.append({
            "release_id":           r["id"],
            "known_artist_score":   known,
            "related_artist_score": related_score,
            "genre_score":          genre_score,
            "trend_score":          trend_score,
            "recency_score":        recency,
            "total_score":          total,
            "section":              section,
            "reason_text":          _build_reason(known, related_score, genre_score, trend_score, n_sources, related_seeds, taste),
            "_sources": sources,
            "_release": r,
        })

    return scored


def _recency_score(release_date: str | None) -> int:
    if not release_date:
        return 0
    try:
        rd = datetime.fromisoformat(release_date[:10])
        days = (datetime.now() - rd).days
        if days <= 7:
            return 10
        if days <= 30:
            return 6
        if days <= 90:
            return 3
    except (ValueError, TypeError):
        pass
    return 0


def _build_reason(
    known: int,
    related: int,
    genre: int,
    trend: int,
    n_sources: int,
    related_seeds: list[str],
    taste: dict,
) -> str:
    if known >= 40:
        return "New release from one of your top artists"
    if known >= 25:
        return "New release from an artist you listen to often"
    if known >= 15:
        return "New release from an artist in your listening history"
    if related >= 25 and len(related_seeds) >= 2:
        return f"Related to {related_seeds[0]} and {related_seeds[1]}"
    if related > 0 and related_seeds:
        return f"Related to {related_seeds[0]}, an artist you listen to"
    if genre >= 10:
        genres = [g["tag"] for g in taste.get("top_genres", [])[:2]]
        if genres:
            return f"Matches your top genres: {', '.join(genres)}"
    if trend >= 10:
        return f"Trending across {n_sources} discovery sources"
    if trend >= 5:
        return "Trending across multiple discovery sources"
    return "Suggested based on your taste profile"


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

_FETCHER_MAP = {
    "spotify":        lambda: _fetch_spotify(),
    "lastfm":         lambda: _fetch_lastfm_chart(),
    "bandcamp":       lambda: _fetch_bandcamp(),
    "aoty":           lambda: _fetch_aoty(),
    "juno_electronic": lambda: _fetch_juno("juno_electronic"),
    "juno_hiphop":    lambda: _fetch_juno("juno_hiphop"),
    "juno_rock":      lambda: _fetch_juno("juno_rock"),
    "juno_main":      lambda: _fetch_juno("juno_main"),
    "listenbrainz":   lambda: _fetch_listenbrainz(),
}


async def get_discovery_results(force: bool = False) -> dict:
    """
    Run the full discovery pipeline and return scored sections.

    Returns:
      {
        "new_from_artists":    [item, ...],
        "trending_near_taste": [item, ...],
        "genre_picks":         [item, ...],
        "generated_at":        "ISO datetime",
        "total_items":         int,
      }
    Each item is a flat dict ready for the card template.
    """
    now = time.monotonic()
    if not force and _discovery_cache["result"] and now < _discovery_cache["expires_at"]:
        return _discovery_cache["result"]

    conn = init_db(_DB_PATH)
    try:
        result = await _run_pipeline(conn)
    finally:
        conn.close()

    _discovery_cache["result"]     = result
    _discovery_cache["expires_at"] = now + _DISCOVERY_TTL

    try:
        _RESULTS_FILE.write_text(
            json.dumps({**result, "generated_at": result["generated_at"]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Could not write %s", _RESULTS_FILE)

    return result


async def _run_pipeline(conn) -> dict:
    # 1. Fetch all sources concurrently
    logger.info("Discovery pipeline — fetching from: %s", ", ".join(DISCOVERY_FEEDS))
    tasks = [_FETCHER_MAP[f]() for f in DISCOVERY_FEEDS if f in _FETCHER_MAP]
    all_raw: list[list[RawItem]] = await asyncio.gather(*tasks)

    total_raw = sum(len(r) for r in all_raw)
    logger.info("Sources returned %d raw releases", total_raw)

    # 2. Upsert into DB, track source associations
    for raw_list in all_raw:
        for item in raw_list:
            if not item.artist_normalized or not item.album_normalized:
                continue
            release_id = upsert_release(conn, {
                "artist_display":    item.artist_display,
                "artist_normalized": item.artist_normalized,
                "album_title":       item.album_title,
                "album_normalized":  item.album_normalized,
                "release_date":      item.release_date,
                "image_url":         item.image_url,
                "item_url":          item.item_url,
            })
            add_source(conn, release_id, item.source_name, item.item_url, item.release_date)
    conn.commit()

    # 3. Owned-album filter
    owned = await _get_local_library()

    # 4. Load all releases with source counts
    releases = load_all_releases_with_sources(conn)
    if owned:
        before = len(releases)
        releases = [
            r for r in releases
            if f"{r['artist_normalized']}|{r['album_normalized']}" not in owned
        ]
        logger.info("Owned-album filter: %d → %d releases", before, len(releases))

    # 5. Load / refresh taste profile
    taste = await _load_or_refresh_taste_profile(conn)

    # 6. Score releases
    source_counts = {r["id"]: r.get("sources", []) for r in releases}
    scored = _score_releases(releases, taste, source_counts)

    # 7. Prune and save scores
    clear_scores(conn)
    save_scores(conn, [{k: v for k, v in s.items() if not k.startswith("_")} for s in scored])
    conn.commit()
    prune_old_releases(conn, days=120)
    conn.commit()

    # 8. Build output sections
    sections: dict[str, list[dict]] = {
        "new_from_artists": [],
        "trending_near_taste": [],
        "genre_picks": [],
    }

    # Sort by total_score desc, then assign up to 20 per section
    scored.sort(key=lambda x: x["total_score"], reverse=True)
    section_caps = {"new_from_artists": 20, "trending_near_taste": 20, "genre_picks": 20}
    section_counts: dict[str, int] = {k: 0 for k in sections}

    for s in scored:
        section = s["section"]
        if section not in sections:
            continue
        if section_counts[section] >= section_caps[section]:
            continue
        r = s["_release"]
        sources_list = s["_sources"]
        primary_source = sources_list[0] if sources_list else "unknown"
        # Normalise juno_* badge label
        badge_source = "juno" if primary_source.startswith("juno") else primary_source
        sections[section].append({
            "artist_display":    r["artist_display"],
            "artist_normalized": r["artist_normalized"],
            "album_title":       r["album_title"],
            "album_normalized":  r["album_normalized"],
            "release_date":      r.get("release_date"),
            "image_url":         r.get("image_url"),
            "source_url":        r.get("item_url") or "",
            "source":            badge_source,
            "sources":           [("juno" if sn.startswith("juno") else sn) for sn in sources_list],
            "reason":            s["reason_text"],
            "total_score":       s["total_score"],
            **_search_urls(r["artist_display"], r["album_title"]),
        })
        section_counts[section] += 1

    total = sum(len(v) for v in sections.values())
    logger.info(
        "Discovery complete — new_from_artists: %d, trending_near_taste: %d, genre_picks: %d",
        len(sections["new_from_artists"]),
        len(sections["trending_near_taste"]),
        len(sections["genre_picks"]),
    )

    return {
        **sections,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_items":  total,
    }
