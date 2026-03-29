"""
webapp/discovery_db.py — SQLite schema and CRUD helpers for the discovery engine.

DB lives at {DATA_DIR}/discovery.db (same named volume, no docker-compose changes).

Tables:
  releases        — deduplicated canonical releases
  release_sources — one row per source per release (tracks cross-source overlap)
  release_scores  — scored output; fully recomputed on each pipeline refresh
  taste_cache     — single-row taste profile (id=1); refreshed on a 24h TTL
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS releases (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_display     TEXT    NOT NULL,
    artist_normalized  TEXT    NOT NULL,
    album_title        TEXT    NOT NULL,
    album_normalized   TEXT    NOT NULL,
    release_date       TEXT,
    image_url          TEXT,
    item_url           TEXT,
    format_hint        TEXT    NOT NULL DEFAULT 'digital',
    first_seen_at      TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    UNIQUE (artist_normalized, album_normalized)
);

CREATE TABLE IF NOT EXISTS release_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id   INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    source_name  TEXT    NOT NULL,
    source_url   TEXT,
    published_at TEXT,
    created_at   TEXT    NOT NULL,
    UNIQUE (release_id, source_name)
);

CREATE TABLE IF NOT EXISTS release_scores (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id           INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    total_score          REAL    NOT NULL DEFAULT 0,
    known_artist_score   REAL    NOT NULL DEFAULT 0,
    related_artist_score REAL    NOT NULL DEFAULT 0,
    genre_score          REAL    NOT NULL DEFAULT 0,
    trend_score          REAL    NOT NULL DEFAULT 0,
    recency_score        REAL    NOT NULL DEFAULT 0,
    section              TEXT    NOT NULL DEFAULT '',
    reason_text          TEXT    NOT NULL DEFAULT '',
    computed_at          TEXT    NOT NULL,
    UNIQUE (release_id)
);

CREATE TABLE IF NOT EXISTS taste_cache (
    id                   INTEGER PRIMARY KEY,
    top_artists_json     TEXT,
    related_artists_json TEXT,
    top_genres_json      TEXT,
    lastfm_username      TEXT,
    computed_at          TEXT    NOT NULL
);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database and ensure all tables exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def upsert_release(conn: sqlite3.Connection, item: dict) -> int:
    """
    Insert a new release or update metadata on collision.
    Returns the release_id.

    item must have: artist_display, artist_normalized, album_title, album_normalized.
    Optional: release_date, image_url, item_url, format_hint.
    """
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO releases
            (artist_display, artist_normalized, album_title, album_normalized,
             release_date, image_url, item_url, format_hint, first_seen_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artist_normalized, album_normalized) DO UPDATE SET
            artist_display = excluded.artist_display,
            album_title    = excluded.album_title,
            image_url      = COALESCE(excluded.image_url, releases.image_url),
            item_url       = COALESCE(excluded.item_url,  releases.item_url),
            release_date   = COALESCE(excluded.release_date, releases.release_date),
            format_hint    = excluded.format_hint,
            updated_at     = excluded.updated_at
        RETURNING id
        """,
        (
            item["artist_display"],
            item["artist_normalized"],
            item["album_title"],
            item["album_normalized"],
            item.get("release_date"),
            item.get("image_url"),
            item.get("item_url"),
            item.get("format_hint", "digital"),
            now,
            now,
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def add_source(
    conn: sqlite3.Connection,
    release_id: int,
    source_name: str,
    source_url: str | None = None,
    published_at: str | None = None,
) -> None:
    """Record that a release was seen in a given source. Silently ignores duplicates."""
    conn.execute(
        """
        INSERT OR IGNORE INTO release_sources
            (release_id, source_name, source_url, published_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (release_id, source_name, source_url, published_at, _now()),
    )


def get_source_counts(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """Return {release_id: [source_name, ...]} for all releases with at least one source."""
    rows = conn.execute(
        "SELECT release_id, source_name FROM release_sources ORDER BY release_id"
    ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row["release_id"], []).append(row["source_name"])
    return result


def save_scores(conn: sqlite3.Connection, scores: list[dict]) -> None:
    """
    Upsert a list of score dicts.

    Each dict must have: release_id, total_score, known_artist_score,
    related_artist_score, genre_score, trend_score, recency_score,
    section, reason_text.
    """
    now = _now()
    conn.executemany(
        """
        INSERT INTO release_scores
            (release_id, total_score, known_artist_score, related_artist_score,
             genre_score, trend_score, recency_score, section, reason_text, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (release_id) DO UPDATE SET
            total_score          = excluded.total_score,
            known_artist_score   = excluded.known_artist_score,
            related_artist_score = excluded.related_artist_score,
            genre_score          = excluded.genre_score,
            trend_score          = excluded.trend_score,
            recency_score        = excluded.recency_score,
            section              = excluded.section,
            reason_text          = excluded.reason_text,
            computed_at          = excluded.computed_at
        """,
        [
            (
                s["release_id"],
                s["total_score"],
                s["known_artist_score"],
                s["related_artist_score"],
                s["genre_score"],
                s["trend_score"],
                s["recency_score"],
                s["section"],
                s["reason_text"],
                now,
            )
            for s in scores
        ],
    )


def load_scored_releases(
    conn: sqlite3.Connection,
    section: str,
    limit: int = 20,
) -> list[dict]:
    """Return up to `limit` releases for a given section, sorted by total_score desc."""
    rows = conn.execute(
        """
        SELECT
            r.id, r.artist_display, r.artist_normalized,
            r.album_title, r.album_normalized,
            r.release_date, r.image_url, r.item_url, r.format_hint,
            s.total_score, s.known_artist_score, s.related_artist_score,
            s.genre_score, s.trend_score, s.recency_score,
            s.section, s.reason_text
        FROM releases r
        JOIN release_scores s ON s.release_id = r.id
        WHERE s.section = ?
        ORDER BY s.total_score DESC
        LIMIT ?
        """,
        (section, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def load_all_releases_with_sources(conn: sqlite3.Connection) -> list[dict]:
    """
    Return every release joined with its aggregated source list.
    Used by the scoring pipeline to compute trend_score.
    """
    rows = conn.execute(
        """
        SELECT
            r.id, r.artist_display, r.artist_normalized,
            r.album_title, r.album_normalized,
            r.release_date, r.image_url, r.item_url, r.format_hint,
            GROUP_CONCAT(rs.source_name) AS sources_csv
        FROM releases r
        LEFT JOIN release_sources rs ON rs.release_id = r.id
        GROUP BY r.id
        """
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["sources"] = d.pop("sources_csv", "").split(",") if d.get("sources_csv") else []
        result.append(d)
    return result


def load_taste_cache(conn: sqlite3.Connection) -> dict | None:
    """Return the cached taste profile dict, or None if not present."""
    row = conn.execute("SELECT * FROM taste_cache WHERE id = 1").fetchone()
    if row is None:
        return None
    return dict(row)


def save_taste_cache(conn: sqlite3.Connection, profile: dict) -> None:
    """Upsert the taste profile cache (always id=1)."""
    conn.execute(
        """
        INSERT INTO taste_cache
            (id, top_artists_json, related_artists_json, top_genres_json,
             lastfm_username, computed_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            top_artists_json     = excluded.top_artists_json,
            related_artists_json = excluded.related_artists_json,
            top_genres_json      = excluded.top_genres_json,
            lastfm_username      = excluded.lastfm_username,
            computed_at          = excluded.computed_at
        """,
        (
            profile.get("top_artists_json"),
            profile.get("related_artists_json"),
            profile.get("top_genres_json"),
            profile.get("lastfm_username"),
            _now(),
        ),
    )


def clear_scores(conn: sqlite3.Connection) -> None:
    """Delete all score rows. Called before each pipeline refresh."""
    conn.execute("DELETE FROM release_scores")


def prune_old_releases(conn: sqlite3.Connection, days: int = 120) -> int:
    """
    Remove releases (and their sources/scores) not seen in the last `days` days.
    Returns the number of rows deleted.
    """
    cutoff = datetime.now(tz=timezone.utc).isoformat()[:10]  # YYYY-MM-DD
    cur = conn.execute(
        "DELETE FROM releases WHERE DATE(updated_at) < DATE(?, ?)",
        (cutoff, f"-{days} days"),
    )
    return cur.rowcount
