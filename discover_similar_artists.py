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
DISCOVER_CACHE_VERSION = 2

# Tags that carry no genre signal — excluded from overlap checks
IGNORED_TAGS: frozenset[str] = frozenset({
    "seen live", "favourite", "favorites", "my favorites", "my favourite",
    "love", "awesome", "best", "great", "amazing", "to listen",
    "all", "albums i own", "check out", "fix tags",
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


def build_card_html(suggestion: SimilarSuggestion) -> str:
    artist_escaped = html.escape(suggestion.candidate_display)
    query_str = (
        f"{suggestion.candidate_display} {suggestion.top_album_title}"
        if suggestion.top_album_title
        else suggestion.candidate_display
    )
    query = url_quote(query_str)
    lastfm_url = html.escape(suggestion.artist_lastfm_url or f"https://www.last.fm/music/{query}")
    discogs_url = f"https://www.discogs.com/search/?q={query}&type=release"
    bandcamp_url = f"https://bandcamp.com/search?q={query}"
    yt_url = f"https://music.youtube.com/search?q={query}"

    if suggestion.top_album_image_url:
        album_escaped = html.escape(suggestion.top_album_title or "")
        cover_html = (
            f'<div class="cover">'
            f'<img src="{html.escape(suggestion.top_album_image_url)}" '
            f'alt="{album_escaped} cover art" loading="lazy"></div>'
        )
    else:
        cover_html = '<div class="cover placeholder">No Artwork</div>'

    score_pct = f"{suggestion.similarity_score * 100:.0f}%"

    if suggestion.top_album_title:
        playcount_fmt = (
            f"{suggestion.top_album_playcount:,}" if suggestion.top_album_playcount else "—"
        )
        album_line = (
            f'<p class="album-line">'
            f'{html.escape(suggestion.top_album_title)}'
            f'<span class="plays">{playcount_fmt} plays</span>'
            f"</p>"
        )
    else:
        album_line = '<p class="album-line no-album">No qualifying album found</p>'

    # Truncate source artists list for display
    sources = suggestion.source_artists
    if len(sources) > 4:
        displayed = ", ".join(html.escape(s) for s in sources[:4])
        displayed += f" +{len(sources) - 4} more"
    else:
        displayed = ", ".join(html.escape(s) for s in sources)

    return (
        '<article class="card">'
        f"{cover_html}"
        '<div class="info">'
        '<div class="title-block">'
        f'<div class="title-text">'
        f'<h2><a href="{lastfm_url}" target="_blank" rel="noopener noreferrer">{artist_escaped}</a></h2>'
        f"{album_line}"
        f'<p class="similar-to">Similar to: {displayed}</p>'
        "</div>"
        f'<span class="score-badge">{score_pct}</span>'
        "</div>"
        '<div class="links">'
        f'<a href="{lastfm_url}" target="_blank" rel="noopener noreferrer">Last.fm</a>'
        f'<a href="{discogs_url}" target="_blank" rel="noopener noreferrer">Discogs</a>'
        f'<a href="{bandcamp_url}" target="_blank" rel="noopener noreferrer">Bandcamp</a>'
        f'<a href="{yt_url}" target="_blank" rel="noopener noreferrer">YouTube Music</a>'
        "</div>"
        "</div>"
        "</article>"
    )


def render_html(
    suggestions: Sequence[SimilarSuggestion],
    output_path: Path,
    total_artists: int,
) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    cards_html = "\n    ".join(build_card_html(s) for s in suggestions)
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Discover Similar Artists</title>
  <style>
    :root {{
      color-scheme: dark;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      padding: 2rem;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0a0a0a;
      color: #f3f3f3;
    }}
    header {{
      max-width: 1200px;
      margin: 0 auto 2rem;
    }}
    h1 {{
      margin: 0 0 0.5rem;
      font-size: 2.5rem;
      letter-spacing: -0.01em;
    }}
    p.meta {{
      margin: 0;
      color: #a0a0a0;
      font-size: 0.95rem;
    }}
    .grid {{
      display: grid;
      gap: 1.5rem;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      justify-content: center;
      max-width: 1400px;
      margin: 0 auto;
    }}
    .card {{
      background: #151515;
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 14px 30px rgba(0, 0, 0, 0.35);
      display: flex;
      flex-direction: column;
      transition: transform 0.2s ease-in-out, box-shadow 0.2s ease-in-out;
      max-width: 300px;
      width: 100%;
      margin: 0 auto;
    }}
    .card:hover {{
      transform: translateY(-6px);
      box-shadow: 0 18px 40px rgba(0, 0, 0, 0.45);
    }}
    .cover {{
      background: rgba(255, 255, 255, 0.06);
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 300px;
    }}
    .cover img {{
      width: 100%;
      height: auto;
      display: block;
      object-fit: cover;
    }}
    .cover.placeholder {{
      color: #666;
      font-size: 0.9rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .info {{
      padding: 1rem 1.2rem 1.4rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }}
    .title-block {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.75rem;
    }}
    .title-text {{
      flex: 1;
      min-width: 0;
    }}
    .title-text h2 {{
      margin: 0 0 0.3rem;
      font-size: 1.1rem;
      line-height: 1.3;
      font-weight: 700;
    }}
    .title-text h2 a {{
      color: #f3f3f3;
      text-decoration: none;
    }}
    .title-text h2 a:hover {{
      color: #7dd6ff;
    }}
    .album-line {{
      margin: 0 0 0.3rem;
      font-size: 0.88rem;
      color: #c8c8c8;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .album-line .plays {{
      margin-left: 0.4rem;
      color: #888;
      font-size: 0.8rem;
    }}
    .album-line.no-album {{
      color: #666;
      font-style: italic;
    }}
    .similar-to {{
      margin: 0;
      font-size: 0.78rem;
      color: #7dd6ff;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .score-badge {{
      flex-shrink: 0;
      background: rgba(125, 214, 255, 0.15);
      border: 1px solid rgba(125, 214, 255, 0.35);
      border-radius: 10px;
      color: #7dd6ff;
      font-size: 0.78rem;
      font-weight: 700;
      padding: 0.25rem 0.5rem;
      white-space: nowrap;
    }}
    .links {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.5rem;
    }}
    .links a {{
      color: #0b0b0b;
      background: #7dd6ff;
      border-radius: 12px;
      padding: 0.5rem 0.75rem;
      font-weight: 600;
      text-decoration: none;
      transition: background 0.2s ease-in-out;
      text-align: center;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.88rem;
    }}
    .links a:hover {{
      background: #54b8e3;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Discover Similar Artists</h1>
    <p class="meta">Generated {timestamp} · {len(suggestions)} suggestion(s) across {total_artists} artist(s) scanned</p>
  </header>
  <section class="grid">
    {cards_html}
  </section>
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

        # --- Phase A.5: tag-based genre filter --------------------------------
        if DISCOVER_TAG_OVERLAP > 0 and candidates:
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

            before = len(candidates)
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
