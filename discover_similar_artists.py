"""Discover new artists similar to those already in your music library.

For each artist in the local collection the script queries Last.fm's
``artist.getSimilar`` endpoint, filters out artists already owned, and keeps
the top ``SUGGESTIONS_PER_ARTIST`` candidates. Candidates are deduplicated
globally; each unique new artist is then enriched with their most popular
album (by playcount). The result is an HTML report sorted by Last.fm
similarity score descending.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import html
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote as url_quote

import httpx
from rapidfuzz import fuzz
from tqdm import tqdm

from missing_popular_albums import (
    CONFIG,
    DEFAULT_WORKERS,
    FUZZ_THRESHOLD,
    MAX_WORKERS,
    MUSIC_ROOT,
    NAVIDROME_URL,
    NAVIDROME_USER,
    NAVIDROME_PASS,
    NAVIDROME_MUSIC_FOLDER,
    REQUEST_TIMEOUT,
    LocalArtist,
    LastFMClient,
    LastFMError,
    RemoteAlbum,
    cache_albums,
    load_cache,
    load_cached_albums,
    normalize_text,
    pick_top_album_ep,
    save_cache,
    scan_library,
    scan_navidrome,
    setup_logging,
    transform_top_albums,
    upgrade_image_url,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DISCOVER_HTML_OUT = Path(
    CONFIG.get("DISCOVER_HTML_OUT", "discover_similar_artists.html")
).expanduser()
DISCOVER_CACHE_FILE = Path(
    CONFIG.get("DISCOVER_CACHE_FILE", ".cache/similar_artists.json")
).expanduser()
DISCOVER_LOG_FILE = Path(
    CONFIG.get("DISCOVER_LOG_FILE", "discover_similar_artists.log")
).expanduser()
SUGGESTIONS_PER_ARTIST = int(CONFIG.get("SUGGESTIONS_PER_ARTIST", "2"))
SIMILAR_ARTIST_LIMIT = int(CONFIG.get("SIMILAR_ARTIST_LIMIT", "30"))
DISCOVER_TAG_OVERLAP = int(CONFIG.get("DISCOVER_TAG_OVERLAP", "1"))
DISCOVER_MIN_JACCARD = float(CONFIG.get("DISCOVER_MIN_JACCARD", "0.1"))
DISCOVER_TAG_TOP_N   = max(1, min(10, int(CONFIG.get("DISCOVER_TAG_TOP_N", "5"))))
_raw_mode = CONFIG.get("DISCOVER_SIMILARITY_MODE", "lastfm").strip().lower()
if _raw_mode not in ("lastfm", "tags"):
    import warnings
    warnings.warn(
        f"Unknown DISCOVER_SIMILARITY_MODE {_raw_mode!r} — defaulting to 'lastfm'",
        stacklevel=1,
    )
    _raw_mode = "lastfm"
DISCOVER_SIMILARITY_MODE: str = _raw_mode
DISCOVER_CACHE_VERSION = 2

# Tags that carry no genre signal — excluded from overlap checks
IGNORED_TAGS: frozenset[str] = frozenset({
    # user behavior / opinion
    "seen live", "favourite", "favorites", "my favorites", "my favourite",
    "love", "awesome", "best", "great", "amazing", "to listen",
    "all", "albums i own", "check out", "fix tags",
    # platform / spam
    "spotify", "heard on pandora", "pandora", "youtube",
    "under 2000 listeners", "not on spotify",
    # noise
    "music", "good", "cool", "beautiful", "nice",
})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SimilarSuggestion:
    """A new artist discovered via similarity search, enriched with their top album."""

    candidate_display: str
    candidate_normalized: str
    source_artists: list[str] = field(default_factory=list)
    similarity_score: float = 0.0
    matched_tags: list[str] = field(default_factory=list)
    top_album_title: str | None = None
    top_album_playcount: int = 0
    top_album_image_url: str | None = None
    top_album_lastfm_url: str | None = None
    top_album_release_year: int | None = None
    artist_lastfm_url: str | None = None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def load_discover_cache(cache_path: Path) -> tuple[dict[str, dict], dict[str, dict], dict[str, list]]:
    """Return (similar_cache, top_album_cache, tag_cache) from disk, or empty dicts."""
    if not cache_path.exists():
        return {}, {}, {}
    try:
        with cache_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("version") != DISCOVER_CACHE_VERSION:
            logging.info("Discover cache version mismatch — ignoring existing cache.")
            return {}, {}, {}
        return (
            payload.get("similar", {}) or {},
            payload.get("top_albums", {}) or {},
            payload.get("tags", {}) or {},
        )
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Failed to load discover cache from %s: %s", cache_path, exc)
        return {}, {}, {}


def save_discover_cache(
    cache_path: Path,
    similar_cache: dict[str, dict],
    top_album_cache: dict[str, dict],
    tag_cache: dict[str, list],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": DISCOVER_CACHE_VERSION,
        "similar": similar_cache,
        "top_albums": top_album_cache,
        "tags": tag_cache,
    }
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_cached_similar(
    similar_cache: dict[str, dict], artist_key: str
) -> list[tuple[str, str, float, str | None]] | None:
    """Return cached similar artist results or None if missing/malformed."""
    entry = similar_cache.get(artist_key)
    if not entry:
        return None
    results = entry.get("results")
    if not isinstance(results, list):
        return None
    out: list[tuple[str, str, float, str | None]] = []
    try:
        for item in results:
            out.append(
                (
                    item["name"],
                    item["normalized"],
                    float(item["score"]),
                    item.get("url"),
                )
            )
    except (KeyError, ValueError, TypeError):
        return None
    return out


def cache_similar(
    similar_cache: dict[str, dict],
    artist_key: str,
    artist_name: str,
    results: list[tuple[str, str, float, str | None]],
) -> None:
    similar_cache[artist_key] = {
        "artist": artist_name,
        "results": [
            {"name": name, "normalized": norm, "score": score, "url": url}
            for name, norm, score, url in results
        ],
    }


# ---------------------------------------------------------------------------
# Collection membership check
# ---------------------------------------------------------------------------


def has_artist(local_normalized: set[str], candidate_normalized: str) -> bool:
    """Return True if the candidate is already in the local collection (fuzzy)."""
    if candidate_normalized in local_normalized:
        return True
    for local in local_normalized:
        if fuzz.token_set_ratio(candidate_normalized, local) >= FUZZ_THRESHOLD:
            return True
    return False


# ---------------------------------------------------------------------------
# Core async logic
# ---------------------------------------------------------------------------


async def find_similar_not_in_collection(
    artist: LocalArtist,
    client: LastFMClient,
    local_normalized: set[str],
    similar_cache: dict[str, dict],
    cache_lock: asyncio.Lock,
    stats_lock: asyncio.Lock,
    cache_stats: dict[str, int],
    use_cache: bool,
) -> list[tuple[str, str, float, str | None]]:
    """Return up to SUGGESTIONS_PER_ARTIST similar artists not in the local collection."""
    artist_key = artist.normalized_name

    if use_cache:
        async with cache_lock:
            cached = load_cached_similar(similar_cache, artist_key)
        if cached is not None:
            async with stats_lock:
                cache_stats["similar_hits"] += 1
            logging.debug("Similar cache hit for '%s'", artist.display_name)
            return cached

    try:
        response = await client.similar_artists(
            artist.display_name, limit=SIMILAR_ARTIST_LIMIT
        )
    except LastFMError as exc:
        logging.warning("Failed to fetch similar artists for '%s': %s", artist.display_name, exc)
        return []

    raw = response.get("similarartists", {}).get("artist", [])
    if isinstance(raw, dict):
        raw = [raw]

    results: list[tuple[str, str, float, str | None]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "").strip()
        if not name:
            continue
        normalized = normalize_text(name)
        if not normalized:
            continue
        if has_artist(local_normalized, normalized):
            continue
        try:
            score = float(entry.get("match", 0))
        except (TypeError, ValueError):
            score = 0.0
        url = entry.get("url") or None
        results.append((name, normalized, score, url))
        if len(results) >= SUGGESTIONS_PER_ARTIST:
            break

    async with stats_lock:
        cache_stats["similar_misses"] += 1
    async with cache_lock:
        cache_similar(similar_cache, artist_key, artist.display_name, results)

    return results


async def enrich_with_top_album(
    candidate_display: str,
    candidate_normalized: str,
    client: LastFMClient,
    top_album_cache: dict[str, dict],
    cache_lock: asyncio.Lock,
    use_cache: bool,
) -> RemoteAlbum | None:
    """Fetch and return the most popular qualifying album for a candidate artist."""
    cache_key = candidate_normalized

    if use_cache:
        async with cache_lock:
            cached = load_cached_albums(top_album_cache, cache_key)
        if cached is not None:
            logging.debug("Top-album cache hit for '%s'", candidate_display)
            return pick_top_album_ep(cached)

    try:
        response = await client.artist_top_albums(candidate_display)
    except LastFMError as exc:
        logging.warning("Failed to fetch top albums for '%s': %s", candidate_display, exc)
        return None

    raw_albums = response.get("topalbums", {}).get("album", [])
    if isinstance(raw_albums, dict):
        raw_albums = [raw_albums]

    remote_albums = await transform_top_albums(client, candidate_display, raw_albums)
    async with cache_lock:
        cache_albums(top_album_cache, cache_key, candidate_display, remote_albums)

    return pick_top_album_ep(remote_albums)


async def fetch_artist_tags(
    artist_name: str,
    client: LastFMClient,
    tag_cache: dict[str, list[str]],
    cache_lock: asyncio.Lock,
    use_cache: bool,
    top_n: int = 15,
) -> list[str]:
    """Return top N cleaned tag names for an artist; empty list on failure."""
    cache_key = normalize_text(artist_name)

    if use_cache:
        async with cache_lock:
            if cache_key in tag_cache:
                return tag_cache[cache_key]

    try:
        response = await client.artist_top_tags(artist_name)
    except LastFMError:
        return []

    raw = response.get("toptags", {}).get("tag", [])
    if isinstance(raw, dict):
        raw = [raw]
    tags = [
        t["name"].strip().lower()
        for t in raw[:top_n]
        if isinstance(t, dict) and t.get("name")
    ]
    async with cache_lock:
        tag_cache[cache_key] = tags
    return tags


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------


def build_card_html(suggestion: SimilarSuggestion, slskd_enabled: bool = False) -> str:
    artist_esc = html.escape(suggestion.candidate_display)
    album_title = suggestion.top_album_title or ""
    album_esc = html.escape(album_title)
    query_str = (
        f"{suggestion.candidate_display} {album_title}"
        if album_title
        else suggestion.candidate_display
    )
    query = url_quote(query_str)
    lastfm_url = html.escape(suggestion.artist_lastfm_url or f"https://www.last.fm/music/{query}")
    discogs_url = f"https://www.discogs.com/search/?q={query}&type=release"
    bandcamp_url = f"https://bandcamp.com/search?q={query}"
    yt_url = f"https://music.youtube.com/search?q={query}"
    score_pct = f"{suggestion.similarity_score * 100:.0f}%"
    year = suggestion.top_album_release_year or ""
    playcount_fmt = (
        f"{suggestion.top_album_playcount:,}" if suggestion.top_album_playcount else "—"
    )
    meta = f"{year} · {playcount_fmt} plays" if year else f"{playcount_fmt} plays"

    if suggestion.top_album_image_url:
        cover_html = (
            f'<img src="{html.escape(suggestion.top_album_image_url)}" '
            f'alt="{album_esc} cover art" loading="lazy">'
        )
    else:
        cover_html = '<div class="cover-placeholder">No Artwork</div>'

    # Truncate source artists list for display
    sources = suggestion.source_artists
    if len(sources) > 4:
        displayed = ", ".join(html.escape(s) for s in sources[:4])
        displayed += f" +{len(sources) - 4} more"
    else:
        displayed = ", ".join(html.escape(s) for s in sources)

    copy_text = html.escape(f"{suggestion.candidate_display} {album_title}".strip(), quote=True)
    copy_btn = (
        f'<button class="action-btn" onclick="copyText(\'{copy_text}\')" title="Copy artist &amp; album">'
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14'
        'c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/>'
        '</svg> Copy</button>'
    ) if album_title else ""

    slskd_btn = (
        f'<button class="action-btn btn-slskd" data-artist="{artist_esc}" data-album="{album_esc}"'
        f' onclick="sendToSlskd(this)" title="Search on SLSKD">'
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16'
        'c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5z'
        'm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/>'
        '</svg> SLSKD</button>'
    ) if slskd_enabled and album_title else ""

    return (
        '<article class="report-card">'
        f'<div class="card-image-wrap" data-artist="{artist_esc}" data-album="{album_esc}">'
        f'{cover_html}'
        '<div class="stream-icons">'
        '<button class="stream-btn" data-service="apple" onclick="streamCard(this)" title="Apple Music">'
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4'
        ' 4-1.79 4-4V7h4V3h-6z"/></svg></button>'
        '<button class="stream-btn" data-service="spotify" onclick="streamCard(this)" title="Spotify">'
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M17.9 10.9C14.7 9 9.35 8.8 6.3 9.75c-.5.15-1-.15-1.15-.6-.15-.5.15-1 .6-1.15'
        ' 3.55-1.05 9.4-.85 13.1 1.35.45.25.6.85.35 1.3-.25.35-.85.5-1.3.25z'
        'm-.1 2.8c-.25.35-.7.5-1.05.25-2.7-1.65-6.8-2.15-9.95-1.15-.4.1-.85-.1-.95-.5'
        '-.1-.4.1-.85.5-.95 3.65-1.1 8.15-.55 11.25 1.35.3.15.45.65.2 1z'
        'm-1.2 2.75c-.2.3-.55.4-.85.2-2.35-1.45-5.3-1.75-8.8-.95-.35.1-.65-.15-.75-.45'
        '-.1-.35.15-.65.45-.75 3.8-.85 7.1-.5 9.7 1.1.35.2.4.55.25.85z"/>'
        '</svg></button>'
        '<button class="stream-btn" data-service="youtube" onclick="streamCard(this)" title="YouTube">'
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M8 5v14l11-7z"/></svg></button>'
        '</div>'
        '<div class="card-player">'
        '<button class="player-close" onclick="closePlayer(this)">'
        '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">'
        '<path d="M18 6L6 18M6 6l12 12"/></svg></button>'
        '<iframe frameborder="0" allowfullscreen allow="autoplay; encrypted-media"></iframe>'
        '</div>'
        f'<span class="score-badge">{score_pct}</span>'
        '</div>'
        '<div class="card-body">'
        f'<a class="card-artist" href="{lastfm_url}" target="_blank" rel="noopener">{artist_esc}</a>'
        f'<div class="card-album">{album_esc or "No qualifying album"}</div>'
        f'<div class="card-meta">{meta}</div>'
        f'<div class="similar-to">Similar to: {displayed}</div>'
        '</div>'
        '<div class="card-actions">'
        '<div class="card-links">'
        f'<a class="link-btn" href="{lastfm_url}" target="_blank" rel="noopener" title="Last.fm">'
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4'
        ' 4-1.79 4-4V7h4V3h-6z"/></svg></a>'
        f'<a class="link-btn" href="{discogs_url}" target="_blank" rel="noopener" title="Discogs">'
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<circle cx="12" cy="12" r="9"/>'
        '<circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/></svg></a>'
        f'<a class="link-btn" href="{bandcamp_url}" target="_blank" rel="noopener" title="Bandcamp">'
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M0 18.75l7.437-13.5H24l-7.438 13.5z"/></svg></a>'
        f'<a class="link-btn" href="{yt_url}" target="_blank" rel="noopener" title="YouTube Music">'
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M8 5v14l11-7z"/></svg></a>'
        '</div>'
        '<div class="card-action-row">'
        f'{copy_btn}'
        f'{slskd_btn}'
        '</div>'
        '</div>'
        '</article>'
    )


def render_html(
    suggestions: Sequence[SimilarSuggestion],
    output_path: Path,
    total_artists: int,
) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    slskd_enabled = bool(CONFIG.get("SLSKD_URL"))
    cards_html = "\n    ".join(build_card_html(s, slskd_enabled) for s in suggestions)
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Discover Similar Artists</title>
  <style>
    :root {{ color-scheme: dark; }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 2rem;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0a0a0a; color: #f3f3f3;
    }}
    header {{ max-width: 1200px; margin: 0 auto 2rem; }}
    h1 {{ margin: 0 0 .5rem; font-size: 2.5rem; letter-spacing: -.01em; }}
    p.meta {{ margin: 0; color: #a0a0a0; font-size: .95rem; }}
    .grid {{
      display: grid; gap: 1.25rem;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      max-width: 1400px; margin: 0 auto;
    }}
    /* Card */
    .report-card {{
      background: #111; border: 1px solid #1e1e1e; border-radius: 10px;
      display: flex; flex-direction: column; overflow: hidden;
      max-width: 300px; width: 100%; margin: 0 auto;
    }}
    /* Image area */
    .card-image-wrap {{
      position: relative; aspect-ratio: 1; overflow: hidden; flex-shrink: 0;
      background: #1a1a1a;
    }}
    .card-image-wrap img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .cover-placeholder {{
      width: 100%; height: 100%; display: flex;
      align-items: center; justify-content: center;
      color: #333; font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
    }}
    /* Score badge */
    .score-badge {{
      position: absolute; top: .45rem; right: .45rem;
      background: rgba(0,0,0,.75); border: 1px solid #2a2a2a;
      border-radius: 5px; padding: .15rem .45rem;
      font-size: .7rem; font-weight: 700; color: #4ade80;
    }}
    /* Streaming overlay */
    .stream-icons {{
      position: absolute; bottom: .6rem; left: 50%; transform: translateX(-50%);
      display: flex; gap: .45rem; opacity: 0; pointer-events: none;
      transition: opacity .15s;
    }}
    .card-image-wrap:hover:not(.player-open) .stream-icons {{ opacity: 1; pointer-events: auto; }}
    .stream-btn {{
      width: 2.25rem; height: 2.25rem; border-radius: 50%;
      background: rgba(0,0,0,.7); border: 1px solid rgba(255,255,255,.12);
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      transition: background .12s, transform .12s;
    }}
    .stream-btn:hover {{ background: rgba(0,0,0,.9); transform: scale(1.08); }}
    .stream-btn[data-service=apple]   {{ color: #fc3c44; }}
    .stream-btn[data-service=spotify] {{ color: #1DB954; }}
    .stream-btn[data-service=youtube] {{ color: #FF0000; }}
    @keyframes shake {{ 0%,100%{{transform:none}} 25%{{transform:translateX(-3px)}} 75%{{transform:translateX(3px)}} }}
    .stream-btn.not-found {{ animation: shake .3s; }}
    /* Player overlay */
    .card-player {{
      display: none; position: absolute; inset: 0;
      background: #0a0a0a; z-index: 10;
    }}
    .card-image-wrap.player-open .card-player {{ display: block; }}
    .card-image-wrap.player-open {{ aspect-ratio: unset; }}
    .card-image-wrap.player-apple   {{ height: 460px; }}
    .card-image-wrap.player-spotify {{ height: 200px; }}
    .card-image-wrap.player-youtube {{ height: 220px; }}
    .card-player iframe {{ width: 100%; height: 100%; border: none; overflow: hidden; }}
    .player-close {{
      position: absolute; top: .4rem; right: .4rem; z-index: 11;
      width: 1.6rem; height: 1.6rem; border-radius: 50%;
      background: rgba(0,0,0,.65); border: 1px solid rgba(255,255,255,.15);
      color: #ccc; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
    }}
    /* Card body */
    .card-body {{ padding: .65rem .9rem .5rem; flex: 1; }}
    .card-artist {{
      color: #7dd6ff; font-size: .88rem; font-weight: 600;
      text-decoration: none; display: block;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .card-artist:hover {{ text-decoration: underline; }}
    .card-album {{
      color: #e0e0e0; font-size: .82rem; margin-top: .15rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .card-meta {{ color: #555; font-size: .75rem; margin-top: .15rem; }}
    .similar-to {{
      color: #7dd6ff; font-size: .73rem; margin-top: .25rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    /* Action area */
    .card-actions {{
      display: flex; flex-direction: column; gap: .4rem;
      padding: .55rem .9rem .75rem;
      border-top: 1px solid #1a1a1a; margin-top: auto;
    }}
    .card-links {{ display: flex; gap: .3rem; }}
    .link-btn {{
      display: inline-flex; align-items: center; justify-content: center;
      width: 1.75rem; height: 1.75rem; border-radius: 5px;
      background: #1a1a1a; border: 1px solid #252525; color: #555;
      text-decoration: none; flex-shrink: 0; transition: background .12s, color .12s;
    }}
    .link-btn:hover {{ background: #242424; color: #bbb; border-color: #333; }}
    .card-action-row {{ display: flex; gap: .35rem; align-items: center; }}
    .action-btn {{
      display: inline-flex; align-items: center; gap: .3rem;
      padding: .3rem .6rem; border-radius: 5px;
      background: #1a1a1a; border: 1px solid #252525; color: #777;
      font-size: .72rem; font-weight: 500; cursor: pointer; font-family: inherit;
      transition: background .12s, color .12s;
    }}
    .action-btn:hover {{ background: #242424; color: #ccc; border-color: #333; }}
    .btn-slskd.queued {{ color: #4ade80 !important; border-color: #143320 !important; }}
    .btn-slskd.err    {{ color: #f87171 !important; border-color: #4a1515 !important; }}
    /* Toast */
    #toast-container {{
      position: fixed; bottom: 1.5rem; right: 1.5rem;
      display: flex; flex-direction: column; gap: .5rem; z-index: 9999;
    }}
    @keyframes slideIn {{ from{{transform:translateX(1.5rem);opacity:0}} to{{transform:none;opacity:1}} }}
    .toast {{
      padding: .55rem 1rem; border-radius: 7px; font-size: .8rem;
      border: 1px solid #252525; color: #ccc; background: #1a1a1a;
      animation: slideIn .2s ease;
    }}
    .toast-ok  {{ border-color: #143320; color: #4ade80; }}
    .toast-err {{ border-color: #4a1515; color: #f87171; }}
  </style>
</head>
<body>
  <div id="toast-container"></div>
  <header>
    <h1>Discover Similar Artists</h1>
    <p class="meta">Generated {timestamp} · {len(suggestions)} suggestion(s) across {total_artists} artist(s) scanned</p>
  </header>
  <section class="grid">
    {cards_html}
  </section>
  <script>
    function streamCard(btn) {{
      const wrap = btn.closest('.card-image-wrap');
      if (wrap.classList.contains('player-open')) {{
        const active = wrap.querySelector('.stream-btn.active');
        if (active === btn) {{ _closeActivePlayer(wrap); return; }}
        _closeActivePlayer(wrap);
      }}
      const artist = wrap.dataset.artist, album = wrap.dataset.album;
      const service = btn.dataset.service;
      btn.style.opacity = '.4';
      fetch('/api/stream-info?artist=' + encodeURIComponent(artist) +
            '&album=' + encodeURIComponent(album) + '&service=' + service)
        .then(r => r.json())
        .then(data => {{
          btn.style.opacity = '';
          if (!data.embed_url) {{
            btn.classList.add('not-found');
            setTimeout(() => btn.classList.remove('not-found'), 1500);
            return;
          }}
          wrap.classList.add('player-open', 'player-' + service);
          btn.classList.add('active');
          wrap.querySelector('iframe').src = data.embed_url;
        }})
        .catch(() => {{ btn.style.opacity = ''; }});
    }}
    function closePlayer(closeBtn) {{ _closeActivePlayer(closeBtn.closest('.card-image-wrap')); }}
    function _closeActivePlayer(wrap) {{
      const iframe = wrap.querySelector('iframe');
      if (iframe) {{ iframe.src = ''; }}
      wrap.classList.remove('player-open', 'player-apple', 'player-spotify', 'player-youtube');
      wrap.querySelectorAll('.stream-btn.active').forEach(b => b.classList.remove('active'));
    }}
    function copyText(text) {{
      if (navigator.clipboard) {{ navigator.clipboard.writeText(text).catch(() => {{}}); return; }}
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select(); document.execCommand('copy');
      document.body.removeChild(ta);
    }}
    function sendToSlskd(btn) {{
      const artist = btn.dataset.artist, album = btn.dataset.album;
      btn.disabled = true;
      fetch('/api/slskd-search', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{artist, album}})
      }})
      .then(r => {{ if (!r.ok) throw r; return r.json(); }})
      .then(() => {{
        btn.classList.add('queued');
        showToast('Queued: ' + artist + ' \u2014 ' + album, 'ok');
        setTimeout(() => btn.classList.remove('queued'), 2000);
      }})
      .catch(() => {{
        btn.classList.add('err');
        showToast('SLSKD search failed', 'err');
        setTimeout(() => btn.classList.remove('err'), 2000);
      }})
      .finally(() => {{ btn.disabled = false; }});
    }}
    function showToast(msg, type) {{
      const el = document.createElement('div');
      el.className = 'toast toast-' + type; el.textContent = msg;
      document.getElementById('toast-container').appendChild(el);
      setTimeout(() => el.remove(), 3500);
    }}
  </script>
</body>
</html>
"""
    try:
        output_path.write_text(html_content, encoding="utf-8")
    except OSError as exc:
        logging.error("Failed to write HTML output to %s: %s", output_path, exc)
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_arguments() -> argparse.Namespace:
    def worker_count(value: str) -> int:
        workers = int(value)
        if not 1 <= workers <= MAX_WORKERS:
            raise argparse.ArgumentTypeError(
                f"workers must be between 1 and {MAX_WORKERS}, got {workers}"
            )
        return workers

    parser = argparse.ArgumentParser(
        description="Discover artists similar to those in your music library."
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore cache when fetching Last.fm data (still writes cache).",
    )
    parser.add_argument(
        "--limit-artists",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N artists (for testing).",
    )
    parser.add_argument(
        "--workers",
        type=worker_count,
        default=DEFAULT_WORKERS,
        help=f"Number of concurrent requests (1–{MAX_WORKERS}, default {DEFAULT_WORKERS}).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    args = parse_arguments()

    # Re-configure logging to use the discover log file
    import logging.handlers

    DISCOVER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        DISCOVER_LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [file_handler, logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mutagen").setLevel(logging.ERROR)

    logging.info("Discover Similar Artists script started.")

    api_key = os.environ.get("LASTFM_API_KEY") or CONFIG.get("LASTFM_API_KEY", "")
    if not api_key:
        print(
            "Last.fm API key missing. Set LASTFM_API_KEY environment variable, e.g.:\n"
            'export LASTFM_API_KEY="YOUR_KEY"'
        )
        logging.error("Missing LASTFM_API_KEY. Exiting.")
        sys.exit(1)

    if NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASS:
        local_artists = scan_navidrome(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASS, NAVIDROME_MUSIC_FOLDER)
    else:
        local_artists = scan_library(MUSIC_ROOT)
    if not local_artists:
        logging.warning("No artists discovered in local library.")
        print("No artists discovered in the local library.")
        return

    local_normalized: set[str] = set(local_artists.keys())

    artist_items = sorted(
        local_artists.values(),
        key=lambda a: normalize_text(a.display_name),
    )
    original_count = len(artist_items)
    if args.limit_artists is not None:
        artist_items = artist_items[: args.limit_artists]
        logging.info(
            "Limiting to %d/%d artists due to --limit-artists.", len(artist_items), original_count
        )

    use_cache = not args.no_cache
    similar_cache, top_album_cache, tag_cache = (
        load_discover_cache(DISCOVER_CACHE_FILE) if use_cache else ({}, {}, {})
    )
    cache_lock = asyncio.Lock()
    stats_lock = asyncio.Lock()
    cache_stats = {"similar_hits": 0, "similar_misses": 0}
    semaphore = asyncio.Semaphore(args.workers)

    # --- Phase A: discover similar artists -----------------------------------
    candidates: dict[str, SimilarSuggestion] = {}

    async def discover_bounded(artist: LocalArtist) -> list[tuple[str, str, float, str | None]]:
        async with semaphore:
            return await find_similar_not_in_collection(
                artist, client, local_normalized,
                similar_cache, cache_lock, stats_lock, cache_stats, use_cache,
            )

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
        client = LastFMClient(api_key=api_key, client=http)

        with tqdm(total=len(artist_items), desc="Finding similar artists", unit="artist") as progress:
            tasks_a = [asyncio.create_task(discover_bounded(a)) for a in artist_items]
            for artist_obj, coro in zip(artist_items, asyncio.as_completed(tasks_a)):
                try:
                    results = await coro
                except Exception as exc:  # pragma: no cover
                    logging.exception("Error finding similar for %s: %s", artist_obj.display_name, exc)
                    results = []

                for display, normalized, score, url in results:
                    if normalized in candidates:
                        existing = candidates[normalized]
                        if artist_obj.display_name not in existing.source_artists:
                            existing.source_artists.append(artist_obj.display_name)
                        if score > existing.similarity_score:
                            existing.similarity_score = score
                    else:
                        candidates[normalized] = SimilarSuggestion(
                            candidate_display=display,
                            candidate_normalized=normalized,
                            source_artists=[artist_obj.display_name],
                            similarity_score=score,
                            artist_lastfm_url=url,
                        )
                progress.update(1)

        logging.info("Phase A complete — %d unique candidate artists found.", len(candidates))

        # --- Phase A.1: dismissed / blacklist filter --------------------------
        dismissed_path = Path(CONFIG.get("DISMISSED_FILE", "dismissed.json"))
        if dismissed_path.exists():
            try:
                _dismissed_data = json.loads(dismissed_path.read_text(encoding="utf-8"))
                _dismissed_discover = set(_dismissed_data.get("discover", []))
                if _dismissed_discover:
                    before = len(candidates)
                    candidates = {k: v for k, v in candidates.items() if k not in _dismissed_discover}
                    logging.info("Filtered %d dismissed candidate(s).", before - len(candidates))
            except Exception:
                logging.warning("Could not read dismissed file %s", dismissed_path, exc_info=True)

        # --- Phase A.5: tag-based genre filter / re-score ---------------------
        if candidates and (DISCOVER_TAG_OVERLAP > 0 or DISCOVER_SIMILARITY_MODE == "tags"):
            all_names: set[str] = {s.candidate_display for s in candidates.values()}
            for s in candidates.values():
                all_names.update(s.source_artists)

            async def fetch_tags_bounded(name: str) -> None:
                async with semaphore:
                    await fetch_artist_tags(name, client, tag_cache, cache_lock, use_cache)

            await asyncio.gather(
                *[asyncio.create_task(fetch_tags_bounded(n)) for n in all_names]
            )

            def tags_overlap(candidate_display: str, sources: list[str]) -> bool:
                cand_tags = {
                    t for t in tag_cache.get(normalize_text(candidate_display), [])
                    if t not in IGNORED_TAGS
                }
                if not cand_tags:
                    return True  # no tags for candidate → pass through
                for source in sources:
                    src_tags = {
                        t for t in tag_cache.get(normalize_text(source), [])
                        if t not in IGNORED_TAGS
                    }
                    if len(cand_tags & src_tags) >= DISCOVER_TAG_OVERLAP:
                        return True
                # Pass through only if ALL sources also had no usable tags
                return not any(
                    bool({
                        t for t in tag_cache.get(normalize_text(s), [])
                        if t not in IGNORED_TAGS
                    })
                    for s in sources
                )

            def jaccard_tag_details(
                candidate_display: str, sources: list[str]
            ) -> tuple[float, list[str]]:
                """Return (mean Jaccard score, sorted matched tags) using top-N highest-weight tags."""
                cand_tags = {
                    t for t in tag_cache.get(normalize_text(candidate_display), [])[:DISCOVER_TAG_TOP_N]
                    if t not in IGNORED_TAGS
                }
                scores: list[float] = []
                matched: set[str] = set()
                for source in sources:
                    src_tags = {
                        t for t in tag_cache.get(normalize_text(source), [])[:DISCOVER_TAG_TOP_N]
                        if t not in IGNORED_TAGS
                    }
                    union = cand_tags | src_tags
                    if union:
                        intersection = cand_tags & src_tags
                        scores.append(len(intersection) / len(union))
                        matched |= intersection
                score = sum(scores) / len(scores) if scores else 0.0
                return score, sorted(matched)

            before = len(candidates)
            if DISCOVER_SIMILARITY_MODE == "tags":
                survivors: dict = {}
                for k, v in candidates.items():
                    score, matched = jaccard_tag_details(v.candidate_display, v.source_artists)
                    if score >= DISCOVER_MIN_JACCARD:
                        v.similarity_score = score
                        v.matched_tags = matched
                        survivors[k] = v
                candidates = survivors
                logging.info(
                    "Phase A.5 complete — %d/%d candidates kept after genre re-scoring "
                    "(tags mode, top_n=%d, min_jaccard=%.2f).",
                    len(candidates), before, DISCOVER_TAG_TOP_N, DISCOVER_MIN_JACCARD,
                )
            else:
                candidates = {
                    k: v for k, v in candidates.items()
                    if tags_overlap(v.candidate_display, v.source_artists)
                }
                logging.info(
                    "Phase A.5 complete — %d/%d candidates kept after tag filter (overlap≥%d).",
                    len(candidates), before, DISCOVER_TAG_OVERLAP,
                )

        # --- Phase B: enrich with top album ----------------------------------
        candidate_list = list(candidates.values())

        async def enrich_bounded(suggestion: SimilarSuggestion) -> tuple[SimilarSuggestion, RemoteAlbum | None]:
            async with semaphore:
                album = await enrich_with_top_album(
                    suggestion.candidate_display,
                    suggestion.candidate_normalized,
                    client,
                    top_album_cache,
                    cache_lock,
                    use_cache,
                )
                return suggestion, album

        with tqdm(total=len(candidate_list), desc="Fetching top albums", unit="artist") as progress:
            tasks_b = [asyncio.create_task(enrich_bounded(s)) for s in candidate_list]
            for coro in asyncio.as_completed(tasks_b):
                try:
                    suggestion, album = await coro
                except Exception as exc:  # pragma: no cover
                    logging.exception("Error enriching candidate: %s", exc)
                    progress.update(1)
                    continue
                if album:
                    suggestion.top_album_title = album.title
                    suggestion.top_album_playcount = album.playcount
                    suggestion.top_album_image_url = album.image_url
                    suggestion.top_album_lastfm_url = album.url
                    suggestion.top_album_release_year = album.release_year
                progress.update(1)

    # Filter out candidates with no qualifying album, then sort
    suggestions = [s for s in candidates.values() if s.top_album_title]
    suggestions.sort(key=lambda s: s.similarity_score, reverse=True)

    try:
        render_html(suggestions, DISCOVER_HTML_OUT, total_artists=len(artist_items))
        logging.info("HTML report written to %s", DISCOVER_HTML_OUT)
    except Exception:
        print(f"Failed to write HTML output to {DISCOVER_HTML_OUT}. See log for details.")
        return

    json_path = Path(CONFIG.get("DISCOVER_JSON_OUT", "discover_similar_artists.json"))
    try:
        with json_path.open("w", encoding="utf-8") as _f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_items": len(suggestions),
                "items": [
                    {
                        "candidate_display": s.candidate_display,
                        "candidate_normalized": s.candidate_normalized,
                        "source_artists": s.source_artists,
                        "similarity_score": round(s.similarity_score, 4),
                        "matched_tags": s.matched_tags,
                        "top_album_title": s.top_album_title,
                        "top_album_playcount": s.top_album_playcount,
                        "top_album_image_url": s.top_album_image_url,
                        "top_album_lastfm_url": s.top_album_lastfm_url,
                        "top_album_release_year": s.top_album_release_year,
                        "artist_lastfm_url": s.artist_lastfm_url,
                        "discogs_url": f"https://www.discogs.com/search/?q={url_quote((s.candidate_display + ' ' + s.top_album_title) if s.top_album_title else s.candidate_display)}&type=release",
                        "bandcamp_url": f"https://bandcamp.com/search?q={url_quote((s.candidate_display + ' ' + s.top_album_title) if s.top_album_title else s.candidate_display)}",
                        "youtube_url": f"https://music.youtube.com/search?q={url_quote((s.candidate_display + ' ' + s.top_album_title) if s.top_album_title else s.candidate_display)}",
                    }
                    for s in suggestions
                ],
            }, _f, ensure_ascii=False)
        logging.info("JSON data written to %s", json_path)
    except Exception:
        logging.warning("Failed to write JSON output to %s", json_path, exc_info=True)

    save_discover_cache(DISCOVER_CACHE_FILE, similar_cache, top_album_cache, tag_cache)

    logging.info(
        "Finished — %d artist(s) scanned, %d suggestion(s). "
        "Similar cache hits: %d, misses: %d",
        len(artist_items),
        len(suggestions),
        cache_stats["similar_hits"],
        cache_stats["similar_misses"],
    )
    print(
        f"Scanned {len(artist_items)} artist(s). "
        f"Suggestions: {len(suggestions)}. "
        f"Similar cache hits: {cache_stats['similar_hits']}, "
        f"misses: {cache_stats['similar_misses']}."
    )


if __name__ == "__main__":
    asyncio.run(main())
