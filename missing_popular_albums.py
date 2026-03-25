"""Identify the single most popular missing album per artist.

The script scans audio files under ``MUSIC_ROOT``, extracts artist/album metadata
from tags or folder names, queries Last.fm for each artist's top albums, and
produces an HTML report listing the highest-playcount Album/EP that is missing
from the local collection. Results are cached and logged for repeat runs.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import html
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote as url_quote

import httpx
from mutagen import File
from rapidfuzz import fuzz
from tqdm import tqdm

ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_CONFIG = {
    "MUSIC_ROOT": "/Volumes/NAS/Media/Music/Music_Server",
    "HTML_OUT": "missing_popular_albums.html",
    "CACHE_FILE": ".cache/lastfm_top_albums.json",
    "LOG_FILE": "missing_popular_albums.log",
    "FUZZ_THRESHOLD": "90",
    "DEFAULT_WORKERS": "4",
    "MAX_WORKERS": "8",
    "TOP_ALBUM_LIMIT": "25",
    "TAG_INFO_CHECK_TOP_N": "3",
    "CACHE_VERSION": "2",
    "REQUEST_TIMEOUT": "15",
    "REQUEST_DELAY_MIN": "0.15",
    "REQUEST_DELAY_MAX": "0.3",
    "MAX_RETRIES": "3",
    "LASTFM_API_KEY": "",
    "NAVIDROME_URL": "",
    "NAVIDROME_USER": "",
    "NAVIDROME_PASS": "",
    "NAVIDROME_MUSIC_FOLDER": "",
    "JSON_OUT": "missing_popular_albums.json",
    "DISMISSED_FILE": "dismissed.json",
    # discover_similar_artists.py keys — included here so os.environ overrides work
    "DISCOVER_HTML_OUT": "discover_similar_artists.html",
    "DISCOVER_JSON_OUT": "discover_similar_artists.json",
    "DISCOVER_CACHE_FILE": ".cache/similar_artists.json",
    "DISCOVER_LOG_FILE": "discover_similar_artists.log",
    "SUGGESTIONS_PER_ARTIST": "2",
    "SIMILAR_ARTIST_LIMIT": "30",
    "DISCOVER_TAG_OVERLAP": "1",
}


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    except OSError as exc:
        raise RuntimeError(f"Failed to read configuration from {path}: {exc}") from exc
    return data


def load_config() -> dict[str, str]:
    config = dict(DEFAULT_CONFIG)
    overrides = load_env_file(ENV_FILE)
    for key, value in overrides.items():
        if not value:
            continue
        config[key] = value
    # Docker/12-factor: OS env vars take highest precedence
    for key in DEFAULT_CONFIG:
        env_val = os.environ.get(key, "").strip()
        if env_val:
            config[key] = env_val
    return config


CONFIG = load_config()

MUSIC_ROOT = Path(CONFIG["MUSIC_ROOT"]).expanduser()
HTML_OUT = Path(CONFIG["HTML_OUT"]).expanduser()
CACHE_FILE = Path(CONFIG["CACHE_FILE"]).expanduser()
LOG_FILE = Path(CONFIG["LOG_FILE"]).expanduser()

FUZZ_THRESHOLD = int(CONFIG["FUZZ_THRESHOLD"])
DEFAULT_WORKERS = int(CONFIG["DEFAULT_WORKERS"])
MAX_WORKERS = int(CONFIG["MAX_WORKERS"])

TOP_ALBUM_LIMIT = int(CONFIG["TOP_ALBUM_LIMIT"])
TAG_INFO_CHECK_TOP_N = int(CONFIG["TAG_INFO_CHECK_TOP_N"])
CACHE_VERSION = int(CONFIG["CACHE_VERSION"])
REQUEST_TIMEOUT = float(CONFIG["REQUEST_TIMEOUT"])
REQUEST_DELAY_RANGE = (
    float(CONFIG["REQUEST_DELAY_MIN"]),
    float(CONFIG["REQUEST_DELAY_MAX"]),
)
MAX_RETRIES = int(CONFIG["MAX_RETRIES"])

NAVIDROME_URL           = CONFIG.get("NAVIDROME_URL", "").strip()
NAVIDROME_USER          = CONFIG.get("NAVIDROME_USER", "").strip()
NAVIDROME_PASS          = CONFIG.get("NAVIDROME_PASS", "").strip()
NAVIDROME_MUSIC_FOLDER  = CONFIG.get("NAVIDROME_MUSIC_FOLDER", "").strip()

if REQUEST_DELAY_RANGE[0] > REQUEST_DELAY_RANGE[1]:
    REQUEST_DELAY_RANGE = (REQUEST_DELAY_RANGE[1], REQUEST_DELAY_RANGE[0])

AUDIO_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".aiff",
    ".aif",
    ".ogg",
    ".opus",
}

EXCLUDED_ARTIST_KEYWORDS = {
    "various artists",
    "various artist",
    "soundtrack",
    "ost",
    "score",
    "motion picture",
    "original soundtrack",
    "dj mix",
}

EXCLUDED_TITLE_KEYWORDS = {
    "live",
    "compilation",
    "greatest hits",
    "best of",
    "remix",
    "remixes",
    "anthology",
    "collection",
    "expanded",
    "deluxe edition",
    "deluxe",
    "reissue",
    "mixtape",
    "karaoke",
    "instrumental collection",
    "instrumental compilation",
    "soundtrack",
    "single",
}

EXCLUDED_TAGS = {"compilation", "live", "single", "soundtrack"}
ALLOWED_TAGS = {"album", "ep"}

PRIMARY_ARTIST_SPLIT_RE = re.compile(
    r"\s+(?:&|and|feat\.?|featuring|ft\.?|with)\s+",
    flags=re.IGNORECASE,
)

PAREN_PATTERN = re.compile(r"\([^)]*\)")
ALBUM_DIR_PATTERN = re.compile(r"^\s*(\d{4})\s*[-_]\s*(.+)$")


@dataclass
class LocalArtist:
    """Represents an artist discovered in the local library."""

    display_name: str
    normalized_name: str
    albums: set[str] = field(default_factory=set)
    album_display: dict[str, str] = field(default_factory=dict)
    album_sources: dict[str, str] = field(default_factory=dict)
    album_ids: dict[str, str] = field(default_factory=dict)  # normalized_album → navidrome id

    def add_album(self, normalized_album: str, display_album: str) -> None:
        if normalized_album not in self.album_display:
            self.album_display[normalized_album] = display_album
        self.albums.add(normalized_album)


def _parse_year(date_str: str | None) -> int | None:
    """Extract a 4-digit year from a Last.fm date string like '21 May 1997, 00:00'."""
    if not date_str or not date_str.strip():
        return None
    import re
    m = re.search(r"\b(19|20)\d{2}\b", date_str)
    return int(m.group()) if m else None


@dataclass
class RemoteAlbum:
    """Represents an album returned by Last.fm."""

    title: str
    normalized_title: str
    playcount: int
    image_url: str | None
    url: str | None
    tags: Sequence[str]
    release_year: int | None = None


@dataclass
class AlbumSuggestion:
    """Represents a suggested missing album for an artist."""

    artist_display: str
    artist_normalized: str
    album_title: str
    album_normalized: str
    image_url: str | None
    lastfm_url: str | None
    playcount: int
    release_year: int | None = None


class LastFMError(Exception):
    """Custom exception for Last.fm API issues."""


class LastFMClient:
    """Async client for interacting with the Last.fm API with retry and politeness handling."""

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self.api_key = api_key
        self._client = client
        self._rng = random.Random(time.time_ns())

    async def _request(self, params: dict[str, str]) -> dict:
        params_with_key = {**params, "api_key": self.api_key, "format": "json"}
        last_exception: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            await asyncio.sleep(self._rng.uniform(*REQUEST_DELAY_RANGE))
            try:
                response = await self._client.get(
                    "https://ws.audioscrobbler.com/2.0/",
                    params=params_with_key,
                )
                if response.status_code == 429:
                    raise LastFMError("Rate limited by Last.fm")
                response.raise_for_status()
                payload = response.json()
                if "error" in payload:
                    raise LastFMError(payload.get("message", "Unknown Last.fm error"))
                return payload
            except (httpx.HTTPError, ValueError, LastFMError) as exc:
                last_exception = exc
                if attempt == MAX_RETRIES:
                    break
                backoff = (0.5 * (2 ** (attempt - 1))) + self._rng.uniform(0, 0.25)
                logging.debug(
                    "Retrying Last.fm request (%s/%s) after %.2fs due to: %s",
                    attempt,
                    MAX_RETRIES,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
        raise LastFMError(str(last_exception or "Unknown Last.fm error"))

    async def artist_top_albums(self, artist_name: str, limit: int = TOP_ALBUM_LIMIT) -> dict:
        return await self._request(
            {
                "method": "artist.getTopAlbums",
                "artist": artist_name,
                "autocorrect": "1",
                "limit": str(limit),
            }
        )

    async def album_info(self, artist_name: str, album_title: str) -> dict | None:
        try:
            return await self._request(
                {
                    "method": "album.getInfo",
                    "artist": artist_name,
                    "album": album_title,
                    "autocorrect": "1",
                }
            )
        except LastFMError as exc:
            logging.debug(
                "album.getInfo failed for %s - %s: %s", artist_name, album_title, exc
            )
            return None

    async def similar_artists(self, artist_name: str, limit: int = 30) -> dict:
        return await self._request(
            {
                "method": "artist.getSimilar",
                "artist": artist_name,
                "autocorrect": "1",
                "limit": str(limit),
            }
        )

    async def artist_top_tags(self, artist_name: str) -> dict:
        return await self._request(
            {
                "method": "artist.getTopTags",
                "artist": artist_name,
                "autocorrect": "1",
            }
        )


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handlers = [
        file_handler,
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    logging.getLogger("mutagen").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def normalize_diacritics(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_text(value: str) -> str:
    if not value:
        return ""
    value = normalize_diacritics(value)
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value)
    value = normalize_spaces(value)
    if value.startswith("the "):
        value = value[4:]
    return normalize_spaces(value)


def normalize_album_title(value: str) -> str:
    if not value:
        return ""
    value = PAREN_PATTERN.sub(" ", value)
    value = normalize_text(value)
    replacements = [
        "deluxe edition",
        "deluxe",
        "expanded edition",
        "expanded",
        "remaster",
        "remastered",
        "special edition",
        "limited edition",
        "bonus track version",
        "anniversary edition",
        "20th anniversary",
        "30th anniversary",
        "40th anniversary",
    ]
    for keyword in replacements:
        value = value.replace(keyword, " ")
    return normalize_spaces(value)


def strip_name_variant(name: str) -> str:
    """Remove bullet-separated name variants, keeping only the primary name.

    Some music taggers and media servers (e.g. Navidrome) store both a display
    name and a sort/alternate name in the ALBUMARTIST tag, separated by U+2022
    (e.g. 'Dead Prez • dead prez'). Last.fm rejects the full string as unknown;
    we take only the first component.
    """
    if "\u2022" in name:
        parts = [p.strip() for p in name.split("\u2022") if p.strip()]
        return parts[0] if parts else name
    return name


def primary_artist_name(name: str) -> str:
    if not name:
        return ""
    return PRIMARY_ARTIST_SPLIT_RE.split(name)[0].strip()


def is_artist_excluded(normalized_artist: str) -> bool:
    return any(keyword in normalized_artist for keyword in EXCLUDED_ARTIST_KEYWORDS)


def extract_tag_value(tags: dict[str, Iterable[str]], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = tags.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            return value[0]
        return value
    return None


def read_audio_tags(file_path: Path) -> tuple[str | None, str | None]:
    try:
        audio = File(file_path)
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.debug("Failed to parse tags for %s: %s", file_path, exc)
        return None, None
    if not audio or not audio.tags:
        return None, None
    tags = audio.tags
    artist = extract_tag_value(
        tags,
        (
            "albumartist",
            "album artist",
            "album_artist",
            "artist",
        ),
    )
    album = extract_tag_value(tags, ("album", "albumtitle"))
    return artist, album


def parse_album_from_path(album_dir: Path) -> tuple[str | None, str | None]:
    artist_part = album_dir.parent.name
    album_part = album_dir.name
    match = ALBUM_DIR_PATTERN.match(album_part)
    if match:
        album_part = match.group(2)
    album_part = album_part.replace("_", " ").strip()
    artist_part = artist_part.replace("_", " ").strip()
    return artist_part or None, album_part or None


def add_local_album(
    artists: dict[str, LocalArtist], artist_name: str, album_name: str, source: str
) -> None:
    artist_name = strip_name_variant(artist_name)
    normalized_artist = normalize_text(artist_name)
    if not normalized_artist or is_artist_excluded(normalized_artist):
        return
    normalized_album = normalize_album_title(album_name)
    if not normalized_album:
        return
    if normalized_artist not in artists:
        artists[normalized_artist] = LocalArtist(
            display_name=artist_name,
            normalized_name=normalized_artist,
        )
    artist_entry = artists[normalized_artist]
    if len(artist_name) > len(artist_entry.display_name):
        artist_entry.display_name = artist_name
    artist_entry.add_album(normalized_album, album_name)
    artist_entry.album_sources[normalized_album] = source
    logging.debug("Added album '%s' for artist '%s' from %s", album_name, artist_name, source)


def scan_library(root: Path) -> dict[str, LocalArtist]:
    logging.info("Scanning local library at %s", root)
    artists: dict[str, LocalArtist] = {}
    if not root.exists():
        logging.error("Music root %s does not exist.", root)
        return artists

    for dirpath, _, filenames in root.walk():
        audio_files = [
            dirpath / filename
            for filename in filenames
            if Path(filename).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue

        tag_artist: str | None = None
        tag_album: str | None = None
        tag_found = False

        for audio_file in audio_files:
            artist_value, album_value = read_audio_tags(audio_file)
            if artist_value and not tag_artist:
                tag_artist = artist_value
            if album_value and not tag_album:
                tag_album = album_value
            if artist_value or album_value:
                tag_found = True

        if len(audio_files) < 2 and not tag_found:
            continue

        artist_name = tag_artist
        album_name = tag_album

        if not artist_name or not album_name:
            fallback_artist, fallback_album = parse_album_from_path(dirpath)
            artist_name = artist_name or fallback_artist
            album_name = album_name or fallback_album

        if not artist_name or not album_name:
            continue

        add_local_album(artists, artist_name, album_name, source=str(dirpath))

    logging.info("Scan complete. Found %d artists.", len(artists))
    return artists


def scan_navidrome(base_url: str, username: str, password: str, music_folder: str = "") -> dict[str, LocalArtist]:
    """Fetch the full album list from Navidrome via the Subsonic API."""
    import hashlib
    import secrets

    logging.info("Fetching library from Navidrome at %s", base_url)
    salt = secrets.token_hex(6)
    token = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
    auth = {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "missing-popular-albums",
        "f": "json",
    }
    artists: dict[str, LocalArtist] = {}
    size, offset = 500, 0
    endpoint = f"{base_url.rstrip('/')}/rest/getAlbumList2.view"

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        music_folder_id: str = ""
        if music_folder:
            folders_resp = client.get(
                f"{base_url.rstrip('/')}/rest/getMusicFolders.view", params=auth
            )
            folders_resp.raise_for_status()
            folders = (folders_resp.json()
                       .get("subsonic-response", {})
                       .get("musicFolders", {})
                       .get("musicFolder", []))
            if isinstance(folders, dict):
                folders = [folders]
            for folder in folders:
                if str(folder.get("name", "")).lower() == music_folder.lower():
                    music_folder_id = str(folder.get("id", ""))
                    break
            if music_folder_id:
                logging.info(
                    "Filtering Navidrome scan to music folder '%s' (id=%s)",
                    music_folder, music_folder_id,
                )
            else:
                available = ", ".join(
                    str(f.get("name", "")) for f in folders
                ) or "(none returned)"
                logging.error(
                    "Music folder '%s' not found in Navidrome. Available folders: %s",
                    music_folder,
                    available,
                )
                raise SystemExit(
                    f"ERROR: NAVIDROME_MUSIC_FOLDER='{music_folder}' not found. "
                    f"Available: {available}"
                )

        while True:
            params = {**auth, "type": "alphabeticalByArtist",
                      "size": str(size), "offset": str(offset)}
            if music_folder_id:
                params["musicFolderId"] = music_folder_id
            response = client.get(endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
            sr = payload.get("subsonic-response", {})
            if sr.get("status") != "ok":
                logging.error(
                    "Navidrome error: %s",
                    sr.get("error", {}).get("message", "unknown"),
                )
                break
            album_list = sr.get("albumList2", {}).get("album", [])
            for album in album_list:
                album_name = album.get("name") or album.get("title", "")
                artist_name = album.get("albumArtist") or album.get("artist", "")
                if album_name and artist_name:
                    album_id = album.get("id", "")
                    source = (
                        album.get("path")
                        or (f"{base_url}/app/#/album/{album_id}" if album_id else "navidrome")
                    )
                    add_local_album(artists, artist_name, album_name, source)
                    norm_artist = normalize_text(artist_name)
                    norm_album = normalize_album_title(album_name)
                    if album_id and norm_artist in artists and norm_album in artists[norm_artist].albums:
                        artists[norm_artist].album_ids[norm_album] = album_id
            if len(album_list) < size:
                break
            offset += size

    logging.info("Navidrome scan complete. Found %d artists.", len(artists))
    return artists


def load_cache(cache_path: Path) -> dict[str, dict]:
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("version") != CACHE_VERSION:
            logging.info("Cache version mismatch. Ignoring existing cache.")
            return {}
        return payload.get("artists", {}) or {}
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Failed to load cache from %s: %s", cache_path, exc)
        return {}


def save_cache(cache_path: Path, data: dict[str, dict]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "artists": data}
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_cached_albums(cache: dict[str, dict], artist_key: str) -> list[RemoteAlbum] | None:
    cached_entry = cache.get(artist_key)
    if not cached_entry:
        return None
    albums_data = cached_entry.get("albums")
    if not isinstance(albums_data, list):
        return None
    albums: list[RemoteAlbum] = []
    for album in albums_data:
        try:
            albums.append(
                RemoteAlbum(
                    title=album["title"],
                    normalized_title=album["normalized_title"],
                    playcount=int(album.get("playcount", 0)),
                    image_url=album.get("image_url"),
                    url=album.get("url"),
                    tags=tuple(album.get("tags", [])),
                    release_year=album.get("release_year"),
                )
            )
        except (KeyError, ValueError, TypeError):
            logging.debug("Ignoring malformed cache entry for artist key %s", artist_key)
            return None
    return albums


def cache_albums(
    cache: dict[str, dict], artist_key: str, artist_name: str, albums: Sequence[RemoteAlbum]
) -> None:
    cache[artist_key] = {
        "artist": artist_name,
        "albums": [
            {
                "title": album.title,
                "normalized_title": album.normalized_title,
                "playcount": album.playcount,
                "image_url": album.image_url,
                "url": album.url,
                "tags": list(album.tags),
                "release_year": album.release_year,
            }
            for album in albums
        ],
    }


def upgrade_image_url(url: str) -> str:
    replacements = [
        ("/34s/", "/600x600/"),
        ("/64s/", "/600x600/"),
        ("/128s/", "/600x600/"),
        ("/174s/", "/600x600/"),
        ("/300x300/", "/600x600/"),
        ("/400x400/", "/600x600/"),
    ]
    for source, target in replacements:
        if source in url:
            return url.replace(source, target)
    return url


def extract_image(images: Sequence[dict] | None) -> str | None:
    if not images:
        return None
    candidates = [img for img in images if isinstance(img, dict)]
    preferred_order = ("mega", "extralarge", "large", "medium", "small")
    for size in preferred_order:
        for image in candidates:
            if image.get("size") == size and image.get("#text"):
                url = image["#text"]
                if not url:
                    continue
                if size in {"mega", "extralarge", "large"}:
                    return upgrade_image_url(url)
                return url
    for image in candidates:
        url = image.get("#text")
        if url:
            return upgrade_image_url(url)
    return None


async def fetch_album_tags(
    client: LastFMClient, artist_name: str, album_title: str
) -> tuple[Sequence[str], int | None]:
    info = await client.album_info(artist_name, album_title)
    if not info:
        return (), None
    album_section = info.get("album") or {}
    if isinstance(album_section, str):
        return (), None
    tags_container = album_section.get("tags") or {}
    if isinstance(tags_container, str):
        tags_container = {}
    tags_section = tags_container.get("tag") or []
    if isinstance(tags_section, dict):
        tags_section = [tags_section]
    tag_names = [
        tag.get("name", "")
        for tag in tags_section
        if isinstance(tag, dict) and tag.get("name")
    ]
    tags = tuple(name.lower() for name in tag_names if name)
    wiki = album_section.get("wiki") or {}
    year = _parse_year(wiki.get("published")) or _parse_year(album_section.get("releasedate"))
    return tags, year


def is_album_or_ep(title: str, tags: Sequence[str]) -> bool:
    tags_lower = {tag.lower() for tag in tags}
    if tags_lower & EXCLUDED_TAGS:
        return False
    title_norm = normalize_album_title(title)
    if tags_lower & ALLOWED_TAGS:
        pass
    else:
        for keyword in EXCLUDED_TITLE_KEYWORDS:
            if keyword in title_norm:
                return False
    return bool(title_norm)


async def transform_top_albums(
    client: LastFMClient, artist_name: str, albums_payload: Sequence[dict]
) -> list[RemoteAlbum]:
    remote_albums: list[RemoteAlbum] = []
    seen_titles: set[str] = set()
    for index, album_data in enumerate(albums_payload):
        title = album_data.get("name")
        if not title:
            continue
        normalized_title = normalize_album_title(title)
        if not normalized_title or normalized_title in seen_titles:
            continue
        playcount_raw = album_data.get("playcount") or 0
        try:
            playcount = int(playcount_raw)
        except (TypeError, ValueError):
            playcount = 0
        image_url = extract_image(album_data.get("image"))
        tags: Sequence[str] = ()
        release_year: int | None = None
        if index < TAG_INFO_CHECK_TOP_N:
            tags, release_year = await fetch_album_tags(client, artist_name, title)
        remote_albums.append(
            RemoteAlbum(
                title=title,
                normalized_title=normalized_title,
                playcount=playcount,
                image_url=image_url,
                url=album_data.get("url"),
                tags=tags,
                release_year=release_year,
            )
        )
        seen_titles.add(normalized_title)
    remote_albums.sort(key=lambda item: item.playcount, reverse=True)
    return remote_albums


def pick_top_album_ep(albums: Sequence[RemoteAlbum]) -> RemoteAlbum | None:
    for album in albums:
        if is_album_or_ep(album.title, album.tags):
            return album
    return None


def has_album(local_albums: set[str], candidate_normalized: str) -> bool:
    if candidate_normalized in local_albums:
        return True
    for local_album in local_albums:
        score = fuzz.token_set_ratio(candidate_normalized, local_album)
        if score >= FUZZ_THRESHOLD:
            return True
    return False


def has_album_cross_artist(
    local_artists: dict[str, "LocalArtist"],
    primary_normalized: str,
    album_normalized: str,
) -> bool:
    """Check if album is owned under this artist or any collaborative group containing them.

    Handles cases like billy woods owning Maps under 'billy woods & Kenny Segal' —
    the primary artist's normalized name is a substring of the collaborative key.
    """
    for norm_key, la in local_artists.items():
        if primary_normalized in norm_key and has_album(la.albums, album_normalized):
            return True
    return False


def build_card_html(suggestion: AlbumSuggestion, slskd_enabled: bool = False) -> str:
    artist_esc = html.escape(suggestion.artist_display)
    album_esc = html.escape(suggestion.album_title)
    query = f"{suggestion.artist_display} {suggestion.album_title}"
    query_encoded = url_quote(query)
    discogs_url = f"https://www.discogs.com/search/?q={query_encoded}&type=release"
    bandcamp_url = f"https://bandcamp.com/search?q={query_encoded}"
    yt_url = f"https://music.youtube.com/search?q={query_encoded}"
    lastfm_url = html.escape(suggestion.lastfm_url or discogs_url)
    copy_text = html.escape(f"{suggestion.artist_display} {suggestion.album_title}", quote=True)
    year = suggestion.release_year or ""
    playcount = f"{suggestion.playcount:,}" if suggestion.playcount else "—"
    meta = f"{year} · {playcount} plays" if year else f"{playcount} plays"
    if suggestion.image_url:
        cover_html = (
            f'<img src="{html.escape(suggestion.image_url)}" '
            f'alt="{album_esc} cover" loading="lazy">'
        )
    else:
        cover_html = '<div class="cover-placeholder">No Artwork</div>'
    slskd_btn = (
        f'<button class="action-btn btn-slskd" data-artist="{artist_esc}" data-album="{album_esc}"'
        f' onclick="sendToSlskd(this)" title="Search on SLSKD">'
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16'
        'c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5z'
        'm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/>'
        '</svg> SLSKD</button>'
    ) if slskd_enabled else ""
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
        '</div>'
        '<div class="card-body">'
        f'<a class="card-artist" href="{lastfm_url}" target="_blank" rel="noopener">{artist_esc}</a>'
        f'<div class="card-album">{album_esc}</div>'
        f'<div class="card-meta">{meta}</div>'
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
        f'<button class="action-btn" onclick="copyText(\'{copy_text}\')" title="Copy artist &amp; album">'
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14'
        'c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/>'
        '</svg> Copy</button>'
        f'{slskd_btn}'
        '</div>'
        '</div>'
        '</article>'
    )


def render_html(
    suggestions: Sequence[AlbumSuggestion], output_path: Path, total_artists: int
) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    total_suggestions = len(suggestions)
    slskd_enabled = bool(CONFIG.get("SLSKD_URL"))
    cards_html = "\n    ".join(build_card_html(item, slskd_enabled) for item in suggestions)
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Missing Popular Albums</title>
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
    <h1>Missing Popular Albums</h1>
    <p class="meta">Generated {timestamp} · {total_suggestions} suggestion(s) across {total_artists} artist(s)</p>
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


def summarize_results(
    total_artists: int, suggestions: Sequence[AlbumSuggestion], cache_stats: dict[str, int]
) -> None:
    logging.info(
        "Finished - %d artist(s) processed, %d suggestion(s) found. Cache hits: %d, misses: %d",
        total_artists,
        len(suggestions),
        cache_stats.get("hits", 0),
        cache_stats.get("misses", 0),
    )
    print(
        f"Processed {total_artists} artist(s). Suggestions: {len(suggestions)}. "
        f"Cache hits: {cache_stats.get('hits', 0)}, misses: {cache_stats.get('misses', 0)}."
    )


async def fetch_top_albums_lastfm(
    client: LastFMClient,
    artist_name: str,
    cache: dict[str, dict],
    cache_lock: asyncio.Lock,
    stats_lock: asyncio.Lock,
    cache_stats: dict[str, int],
    use_cache: bool,
) -> list[RemoteAlbum]:
    candidate_names = [artist_name]
    simplified = primary_artist_name(artist_name)
    if simplified and simplified.lower() != artist_name.lower():
        candidate_names.append(simplified)
    normalized_keys = [normalize_text(name) for name in candidate_names]

    for candidate, cache_key in zip(candidate_names, normalized_keys):
        if use_cache:
            async with cache_lock:
                cached_albums = load_cached_albums(cache, cache_key)
            if cached_albums is not None:
                async with stats_lock:
                    cache_stats["hits"] += 1
                logging.debug("Cache hit for artist '%s'", candidate)
                return cached_albums

    for candidate, cache_key in zip(candidate_names, normalized_keys):
        try:
            response = await client.artist_top_albums(candidate)
        except LastFMError as exc:
            logging.warning("Failed to fetch top albums for '%s': %s", candidate, exc)
            continue
        top_albums = response.get("topalbums", {}).get("album", [])
        if isinstance(top_albums, dict):
            top_albums = [top_albums]
        remote_albums = await transform_top_albums(client, candidate, top_albums)
        if remote_albums:
            async with stats_lock:
                cache_stats["misses"] += 1
            async with cache_lock:
                cache_albums(cache, cache_key, candidate, remote_albums)
            return remote_albums
    return []


async def process_artist(
    artist: LocalArtist,
    client: LastFMClient,
    cache: dict[str, dict],
    cache_lock: asyncio.Lock,
    stats_lock: asyncio.Lock,
    cache_stats: dict[str, int],
    use_cache: bool,
    local_artists: dict[str, LocalArtist] | None = None,
) -> AlbumSuggestion | None:
    remote_albums = await fetch_top_albums_lastfm(
        client=client,
        artist_name=artist.display_name,
        cache=cache,
        cache_lock=cache_lock,
        stats_lock=stats_lock,
        cache_stats=cache_stats,
        use_cache=use_cache,
    )
    if not remote_albums:
        logging.info("No albums found on Last.fm for %s", artist.display_name)
        return None
    top_album = pick_top_album_ep(remote_albums)
    if not top_album:
        logging.info("No qualifying album/ep for %s", artist.display_name)
        return None
    owned = (
        has_album_cross_artist(local_artists, artist.normalized_name, top_album.normalized_title)
        if local_artists is not None
        else has_album(artist.albums, top_album.normalized_title)
    )
    if owned:
        logging.info(
            "Top album for %s already present locally: %s",
            artist.display_name,
            top_album.title,
        )
        return None
    local_info = "; ".join(
        f"{title} @ {artist.album_sources.get(norm, '?')}"
        for norm, title in sorted(artist.album_display.items())[:5]
    )
    logging.info(
        "Missing album for %s: %s (playcount %d). Local albums: [%s]",
        artist.display_name,
        top_album.title,
        top_album.playcount,
        local_info or "(none)",
    )
    return AlbumSuggestion(
        artist_display=artist.display_name,
        artist_normalized=artist.normalized_name,
        album_title=top_album.title,
        album_normalized=top_album.normalized_title,
        image_url=top_album.image_url,
        lastfm_url=top_album.url,
        playcount=top_album.playcount,
        release_year=top_album.release_year,
    )


def parse_arguments() -> argparse.Namespace:
    def worker_count(value: str) -> int:
        workers = int(value)
        if not 1 <= workers <= MAX_WORKERS:
            raise argparse.ArgumentTypeError(
                f"workers must be between 1 and {MAX_WORKERS}, got {workers}"
            )
        return workers

    parser = argparse.ArgumentParser(
        description="Find missing popular albums using Last.fm data."
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
        help="Process only the first N artists discovered (for testing).",
    )
    parser.add_argument(
        "--workers",
        type=worker_count,
        default=DEFAULT_WORKERS,
        help=f"Number of concurrent requests (1-{MAX_WORKERS}, default {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--trace-artist",
        metavar="NAME",
        default=None,
        help="Print filesystem paths for all local albums by NAME (requires Navidrome) then exit.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_arguments()
    setup_logging()
    logging.info("Missing Popular Albums script started.")

    api_key = os.environ.get("LASTFM_API_KEY") or CONFIG.get("LASTFM_API_KEY", "")
    if not api_key:
        print(
            "Last.fm API key missing. Set LASTFM_API_KEY environment variable, e.g.:\n"
            'export LASTFM_API_KEY="YOUR_KEY"'
        )
        logging.error("Missing LASTFM_API_KEY. Exiting.")
        sys.exit(1)

    cache = load_cache(CACHE_FILE) if not args.no_cache else {}
    cache_lock = asyncio.Lock()
    stats_lock = asyncio.Lock()
    cache_stats = {"hits": 0, "misses": 0}

    if NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASS:
        local_artists = scan_navidrome(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASS, NAVIDROME_MUSIC_FOLDER)
    else:
        local_artists = scan_library(MUSIC_ROOT)
    if not local_artists:
        logging.warning("No artists discovered in local library.")
        print("No artists discovered in the local library.")
        return

    if args.trace_artist and NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASS:
        import hashlib
        import secrets as _secrets
        import os as _os
        query = normalize_text(args.trace_artist)
        matches = {k: v for k, v in local_artists.items() if query in k}
        if not matches:
            print(f"No artist matching '{args.trace_artist}' found in local library.")
            return
        salt = _secrets.token_hex(6)
        token = hashlib.md5(f"{NAVIDROME_PASS}{salt}".encode()).hexdigest()
        auth = {"u": NAVIDROME_USER, "t": token, "s": salt,
                "v": "1.16.1", "c": "missing-popular-albums", "f": "json"}
        endpoint = f"{NAVIDROME_URL.rstrip('/')}/rest/getAlbum.view"
        with httpx.Client(timeout=REQUEST_TIMEOUT) as http:
            for artist in sorted(matches.values(), key=lambda a: a.display_name):
                print(f"\nArtist: {artist.display_name}")
                for norm_album, display_album in sorted(artist.album_display.items()):
                    album_id = artist.album_ids.get(norm_album, "")
                    print(f"  Album: {display_album}")
                    if album_id:
                        resp = http.get(endpoint, params={**auth, "id": album_id})
                        resp.raise_for_status()
                        songs = (resp.json()
                                 .get("subsonic-response", {})
                                 .get("album", {})
                                 .get("song", []))
                        if songs:
                            path = songs[0].get("path", "")
                            print(f"    Path: {_os.path.dirname(path) or path}")
                        else:
                            print(f"    Path: (no songs returned — id={album_id})")
                    else:
                        source = artist.album_sources.get(norm_album, "unknown")
                        print(f"    Source: {source}")
        return

    artist_items = sorted(
        local_artists.values(),
        key=lambda artist: normalize_text(artist.display_name),
    )
    original_count = len(artist_items)
    if args.limit_artists is not None:
        artist_items = artist_items[: args.limit_artists]
        logging.info(
            "Limiting processing to %d artist(s) out of %d due to --limit-artists flag.",
            len(artist_items),
            original_count,
        )

    suggestions: list[AlbumSuggestion] = []
    semaphore = asyncio.Semaphore(args.workers)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
        client = LastFMClient(api_key=api_key, client=http)

        async def bounded(artist: LocalArtist) -> AlbumSuggestion | None:
            async with semaphore:
                return await process_artist(
                    artist, client, cache, cache_lock, stats_lock, cache_stats,
                    not args.no_cache, local_artists,
                )

        with tqdm(total=len(artist_items), desc="Processing artists", unit="artist") as progress:
            tasks = [asyncio.create_task(bounded(a)) for a in artist_items]
            for coro in asyncio.as_completed(tasks):
                try:
                    result = await coro
                except Exception as exc:  # pragma: no cover - defensive logging
                    logging.exception("Error processing artist: %s", exc)
                    result = None
                if result:
                    suggestions.append(result)
                progress.update(1)

    suggestions.sort(key=lambda item: (item.artist_normalized, item.album_normalized))

    try:
        render_html(suggestions, HTML_OUT, total_artists=len(artist_items))
    except Exception:
        print(f"Failed to write HTML output to {HTML_OUT}. See log for details.")
        return

    dismissed_path = Path(CONFIG.get("DISMISSED_FILE", "dismissed.json"))
    if dismissed_path.exists():
        try:
            _dismissed_data = json.loads(dismissed_path.read_text(encoding="utf-8"))
            _dismissed_missing = set(_dismissed_data.get("missing", []))
            if _dismissed_missing:
                before = len(suggestions)
                suggestions = [
                    s for s in suggestions
                    if f"{s.artist_normalized}|{s.album_normalized}" not in _dismissed_missing
                ]
                logging.info("Filtered %d dismissed suggestion(s).", before - len(suggestions))
        except Exception:
            logging.warning("Could not read dismissed file %s", dismissed_path, exc_info=True)

    json_path = Path(CONFIG.get("JSON_OUT", "missing_popular_albums.json"))
    try:
        with json_path.open("w", encoding="utf-8") as _f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_items": len(suggestions),
                "items": [
                    {
                        "artist_display": s.artist_display,
                        "artist_normalized": s.artist_normalized,
                        "album_normalized": s.album_normalized,
                        "album_title": s.album_title,
                        "image_url": s.image_url,
                        "lastfm_url": s.lastfm_url,
                        "playcount": s.playcount,
                        "release_year": s.release_year,
                        "discogs_url": f"https://www.discogs.com/search/?q={url_quote(s.artist_display + ' ' + s.album_title)}&type=release",
                        "bandcamp_url": f"https://bandcamp.com/search?q={url_quote(s.artist_display + ' ' + s.album_title)}",
                        "youtube_url": f"https://music.youtube.com/search?q={url_quote(s.artist_display + ' ' + s.album_title)}",
                    }
                    for s in suggestions
                ],
            }, _f, ensure_ascii=False)
        logging.info("JSON data written to %s", json_path)
    except Exception:
        logging.warning("Failed to write JSON output to %s", json_path, exc_info=True)

    save_cache(CACHE_FILE, cache)
    summarize_results(len(artist_items), suggestions, cache_stats)


if __name__ == "__main__":
    asyncio.run(main())
