"""
release_years.py — original release years from MusicBrainz.

Last.fm has no reliable release date: album.getInfo's `wiki.published` is the
date the wiki text was last edited (so a 1999 album can show 2026), and
`releasedate` is almost always blank. MusicBrainz release groups carry a
`first-release-date`, the earliest date across every edition and reissue of
an album, which is what the reports should show.

Lookups are rate limited to MusicBrainz's 1 request/second policy, retried
with backoff on 503 (their rate-limit response), and cached on disk. Release
years don't change, so a found year is cached forever; "not found" is retried
after NOT_FOUND_RETRY_DAYS. Network failures are not cached.

Used by missing_popular_albums.py and discover_similar_artists.py. The
caller passes its own normalizers so titles match the rest of the pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from rapidfuzz import fuzz
from tqdm import tqdm

MB_URL = "https://musicbrainz.org/ws/2/release-group/"
# MusicBrainz requires an identifying User-Agent for API clients.
USER_AGENT = "Cratedigger ( https://github.com/cdeschenes/cratedigger )"

ENABLED = os.environ.get("RELEASE_YEAR_LOOKUP", "true").strip().lower() != "false"

REQUEST_INTERVAL = 1.1      # seconds between requests (MB allows 1/s per IP)
MAX_ATTEMPTS = 5            # per lookup, including retries on 503/network errors
NOT_FOUND_RETRY_DAYS = 30
SAVE_EVERY = 50             # persist the cache periodically so interrupted runs keep progress
MIN_SCORE = 90              # MusicBrainz search relevance (0-100)
TITLE_MATCH = 90            # rapidfuzz ratio on normalized titles
ARTIST_MATCH = 85           # rapidfuzz token_sort_ratio on normalized artist names

_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_YEAR = re.compile(r"^(\d{4})")
_SUBTITLE = re.compile(r"\s*:\s+")

Normalizer = Callable[[str], str]


def _lucene_phrase(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _query_title(title: str) -> str:
    """Drop bracketed qualifiers ("(Deluxe Edition)", "[Remastered]") for the search."""
    cleaned = _BRACKETS.sub("", title).strip()
    return cleaned or title


def _pick_year(
    release_groups: list[dict],
    artist: str,
    title: str,
    norm_text: Normalizer,
    norm_title: Normalizer,
) -> int | None:
    """Earliest first-release year among results that match artist and title.

    Singles are ignored and Album/EP release groups win over other types, so a
    same-titled single can't pull the album's year back.
    """
    # Title variants: Last.fm says "Interstellar (Original Motion Picture Soundtrack)",
    # MusicBrainz says "Interstellar: Original Motion Picture Soundtrack".
    def variants(t: str) -> set[str]:
        forms = {t, _query_title(t), _SUBTITLE.split(_query_title(t), 1)[0]}
        return {n for n in (norm_title(f) for f in forms) if n}

    want_titles = variants(title)
    want_artist = norm_text(artist)
    preferred: list[int] = []
    other: list[int] = []
    for rg in release_groups:
        if rg.get("score", 0) < MIN_SCORE:
            continue
        m = _YEAR.match(rg.get("first-release-date") or "")
        if not m:
            continue
        if not any(
            fuzz.ratio(have, want) >= TITLE_MATCH
            for have in variants(rg.get("title", ""))
            for want in want_titles
        ):
            continue
        credit = "".join(
            (c.get("name", "") + c.get("joinphrase", "")) if isinstance(c, dict) else str(c)
            for c in rg.get("artist-credit", [])
        )
        if fuzz.token_sort_ratio(norm_text(credit), want_artist) < ARTIST_MATCH:
            continue
        ptype = rg.get("primary-type")
        if ptype == "Single":
            # A same-titled single (often the album's lead single, or a classic the album
            # is named after, e.g. "Bohemian Rhapsody" 1975 vs. the 2018 soundtrack) would
            # give the wrong year. The reports only list albums and EPs.
            continue
        bucket = preferred if ptype in ("Album", "EP") else other
        bucket.append(int(m.group(1)))
    years = preferred or other
    return min(years) if years else None


class ReleaseYearLookup:
    def __init__(self, cache_path: Path, norm_text: Normalizer, norm_title: Normalizer) -> None:
        self.cache_path = cache_path
        self.norm_text = norm_text
        self.norm_title = norm_title
        self.cache: dict[str, dict] = self._load()
        self.stats = {"cached": 0, "found": 0, "not_found": 0, "failed": 0}

    def key(self, artist: str, title: str) -> str:
        return f"{self.norm_text(artist)}|{self.norm_title(title)}"

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Ignoring unreadable release-year cache %s: %s", self.cache_path, exc)
            return {}

    def save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)  # atomic, so a concurrent run never reads a half-written file
        except OSError as exc:
            logging.warning("Could not save release-year cache %s: %s", self.cache_path, exc)

    def _cached(self, key: str) -> tuple[bool, int | None]:
        entry = self.cache.get(key)
        if not entry:
            return False, None
        if entry.get("year") is not None:
            return True, entry["year"]
        try:
            checked = datetime.fromisoformat(entry.get("checked", ""))
        except ValueError:
            return False, None
        fresh = datetime.now(timezone.utc) - checked < timedelta(days=NOT_FOUND_RETRY_DAYS)
        return fresh, None

    async def _fetch(self, client: httpx.AsyncClient, artist: str, title: str) -> tuple[bool, int | None]:
        """Returns (ok, year). ok=False means the request itself failed (don't cache)."""
        query = f"releasegroup:{_lucene_phrase(_query_title(title))} AND artist:{_lucene_phrase(artist)}"
        delay = 2.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = await client.get(MB_URL, params={"query": query, "fmt": "json", "limit": 10})
                if r.status_code == 503:
                    wait = float(r.headers.get("Retry-After") or delay)
                    await asyncio.sleep(wait)
                    delay = min(delay * 2, 30)
                    continue
                r.raise_for_status()
                groups = r.json().get("release-groups", [])
                return True, _pick_year(groups, artist, title, self.norm_text, self.norm_title)
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == MAX_ATTEMPTS:
                    logging.debug("MusicBrainz lookup failed for %s — %s: %s", artist, title, exc)
                    return False, None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        return False, None

    async def lookup(self, pairs: Iterable[tuple[str, str]], timeout: float = 15) -> dict[str, int | None]:
        """Resolve (artist, album title) pairs to original release years, keyed by self.key()."""
        results: dict[str, int | None] = {}
        todo: list[tuple[str, str, str]] = []
        pending: set[str] = set()
        for artist, title in pairs:
            k = self.key(artist, title)
            if k in results or k in pending:
                continue
            hit, year = self._cached(k)
            if hit:
                results[k] = year
                self.stats["cached"] += 1
            else:
                todo.append((k, artist, title))
                pending.add(k)

        if todo:
            logging.info(
                "Release years: %d cached, %d to look up on MusicBrainz (~%d min)",
                self.stats["cached"], len(todo), round(len(todo) * REQUEST_INTERVAL / 60) or 1,
            )
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
                with tqdm(total=len(todo), desc="Original release years", unit="album") as progress:
                    for i, (k, artist, title) in enumerate(todo, 1):
                        ok, year = await self._fetch(client, artist, title)
                        if ok:
                            self.cache[k] = {
                                "year": year,
                                "checked": datetime.now(timezone.utc).isoformat(),
                            }
                            self.stats["found" if year is not None else "not_found"] += 1
                        else:
                            self.stats["failed"] += 1
                        results[k] = year
                        if i % SAVE_EVERY == 0:
                            self.save()
                        progress.update(1)
                        await asyncio.sleep(REQUEST_INTERVAL)
            self.save()

        logging.info(
            "Release years — cached: %d, found: %d, not found: %d, failed: %d",
            self.stats["cached"], self.stats["found"], self.stats["not_found"], self.stats["failed"],
        )
        return results


async def original_years(
    pairs: list[tuple[str, str]],
    cache_dir: Path,
    norm_text: Normalizer,
    norm_title: Normalizer,
) -> tuple[ReleaseYearLookup, dict[str, int | None]] | tuple[None, dict]:
    """Convenience wrapper. Returns (lookup, years) or (None, {}) when disabled."""
    if not ENABLED:
        logging.info("RELEASE_YEAR_LOOKUP=false — skipping MusicBrainz release years")
        return None, {}
    lk = ReleaseYearLookup(cache_dir / "release_years.json", norm_text, norm_title)
    return lk, await lk.lookup(pairs)
