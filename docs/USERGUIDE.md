# User guide — Cratedigger

This guide covers how both scripts work, how to set them up, and how to get the most out of the web dashboard. It assumes you've read the README and have a working environment.

---

## Table of contents

1. [How the scripts work](#how-the-scripts-work)
2. [Setup — CLI](#setup--cli)
3. [Setup — Docker](#setup--docker)
4. [Configuration reference](#configuration-reference)
5. [Using the web dashboard](#using-the-web-dashboard)
6. [Viewer features](#viewer-features)
7. [New & Trending section](#new--trending-section)
8. [Full-page section view](#full-page-section-view)
9. [Mobile support](#mobile-support)
10. [Scheduling](#scheduling)
11. [Reading the HTML reports](#reading-the-html-reports)
12. [How matching works](#how-matching-works)
13. [The --trace-artist diagnostic](#the---trace-artist-diagnostic)
14. [NAVIDROME_MUSIC_FOLDER](#navidrome_music_folder)
15. [Cache management](#cache-management)
16. [Troubleshooting](#troubleshooting)
17. [FAQ](#faq)

---

## How the scripts work

### missing_popular_albums.py

**Library scan.** The script starts by enumerating every artist and album in your collection. If Navidrome credentials are configured, it calls the Subsonic API (`getAlbumList2`) in paginated batches of 500 until it has the full album list. Without Navidrome, it walks the filesystem at `MUSIC_ROOT`, reading audio tags from every directory that contains at least two audio files. Either way, artist and album names go through the same normalization pipeline (strip diacritics, lowercase, strip "The" prefix, strip edition suffixes) and are deduplicated into a dictionary keyed by normalized artist name.

Artists matching excluded keywords (Various Artists, Soundtrack, OST, DJ Mix, etc.) are silently skipped. Feature-artist credit lines like "Billy Woods & Kenny Segal" are split on `&`, `and`, `feat.`, `with`, and similar — so that entry is indexed under both "billy woods" and "kenny segal" for ownership checks.

**Last.fm lookup.** For each artist, the script calls `artist.getTopAlbums` and fetches up to `TOP_ALBUM_LIMIT` releases. The top three (controlled by `TAG_INFO_CHECK_TOP_N`) make individual `album.getInfo` calls to get their tags. A release is kept only if it passes the album/EP filter: no excluded tags (compilation, live, single, soundtrack), no excluded title keywords (Greatest Hits, Remix, Deluxe, etc.) — unless a positive tag (`album` or `ep`) overrides the keyword check. The highest-playcount qualifying release becomes the candidate.

Requests are rate-limited by a random delay between `REQUEST_DELAY_MIN` and `REQUEST_DELAY_MAX` seconds per request, plus exponential backoff with jitter on retries. Concurrency is controlled by a semaphore set to `--workers`.

**Ownership check and output.** The candidate album is fuzzy-matched against every album the artist has locally. A match score above `FUZZ_THRESHOLD` means the album is considered owned and the artist is skipped. The check also covers collaborative entries — if "billy woods & kenny segal" is in the library and the candidate album's normalized title matches, the solo "billy woods" artist counts as owning it too. Artists with a gap produce a card in the HTML report, sorted alphabetically.

### discover_similar_artists.py

**Similar artist discovery.** For each local artist, the script calls `artist.getSimilar` and takes up to `SIMILAR_ARTIST_LIMIT` results. Each result is fuzzy-matched against every local artist name. Any candidate not already in the collection is kept, up to `SUGGESTIONS_PER_ARTIST` per local artist. Results are globally deduplicated — if two local artists both suggest "Armand Hammer", there is one card, and the "Similar to" line shows both sources.

**Genre tag filter / re-score.** Tags are always fetched when `DISCOVER_TAG_OVERLAP > 0` or `DISCOVER_SIMILARITY_MODE=tags`. In `lastfm` mode, candidates are filtered by minimum shared-tag count (`DISCOVER_TAG_OVERLAP`). In `tags` mode, candidates are re-scored by Jaccard genre-tag similarity; only the top `DISCOVER_TAG_TOP_N` tags per artist (by Last.fm weight) are used for scoring, and any candidate scoring below `DISCOVER_MIN_JACCARD` is dropped. Tags on a blocklist (platform tags like "spotify" and "heard on pandora", user-behavior tags like "seen live" and "favourite", and noise words like "music" and "cool") don't count toward any score.

**Top album enrichment.** Each surviving candidate gets its most popular qualifying album fetched from Last.fm. Candidates with no qualifying album are dropped. The remaining suggestions are sorted by similarity score descending.

---

## Setup — CLI

### 1. Python version

Python 3.12 is required. Check with `python3 --version`. On macOS, use [pyenv](https://github.com/pyenv/pyenv) or [Homebrew](https://brew.sh/) to install 3.12 if needed.

### 2. Create and activate a virtualenv

```bash
cd /path/to/Scripts/cratedigger
python3.12 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

For running tests, also install:

```bash
pip install -r requirements-dev.txt
```

### 4. Get a Last.fm API key

1. Go to https://www.last.fm/api and sign in.
2. Click "Create API Account", fill in the short form (name and description can be anything), and submit.
3. Copy the API key shown on the confirmation page.

### 5. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:

```dotenv
LASTFM_API_KEY=your_key_here

# Option A: Navidrome (faster, recommended)
NAVIDROME_URL=https://navidrome.example.com
NAVIDROME_USER=youruser
NAVIDROME_PASS=yourpass

# Option B: filesystem (slower, no Navidrome needed)
MUSIC_ROOT=/path/to/your/music
```

### 6. Verify the setup

```bash
python missing_popular_albums.py --limit-artists 5
```

If it scans artists, hits Last.fm, and writes an HTML file, you're good. The `--limit-artists 5` flag keeps the test run short.

---

## Setup — Docker

The compose file is `docker-compose.yaml` in the project root. It uses the pre-built image from GitHub Container Registry — no local build required.

### 1. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:

```dotenv
# Required
LASTFM_API_KEY=your_key_here
AUTH_PASS=choose_a_strong_password
SECRET_KEY=any_long_random_string

# Navidrome (recommended)
NAVIDROME_URL=https://navidrome.example.com
NAVIDROME_USER=youruser
NAVIDROME_PASS=yourpass

# Optional: restrict to one Navidrome library
# NAVIDROME_MUSIC_FOLDER=music_main

# Optional: cron schedules
# SCHEDULE_MISSING=0 3 * * 0
# SCHEDULE_DISCOVER=0 4 * * 0

# Optional: streaming previews
# SPOTIFY_CLIENT_ID=
# SPOTIFY_CLIENT_SECRET=
# YOUTUBE_API_KEY=

# Optional: personalized New & Trending
# LASTFM_USERNAME=your_lastfm_username
# LISTENBRAINZ_USERNAME=your_lb_username

# Optional: New & Trending sources (default: all nine)
# DISCOVERY_FEEDS=spotify,lastfm,bandcamp,aoty,juno_electronic,juno_hiphop,juno_rock,juno_main,listenbrainz

# Optional: SLSKD integration
# SLSKD_URL=https://slskd.yourdomain.com
# SLSKD_API_KEY=your-api-key
```

### 2. Start

```bash
docker compose pull
docker compose up -d
```

### 3. Verify

```bash
docker logs -f cratedigger
curl http://localhost:8080/healthz
```

The health check returns `{"status": "ok"}` with no authentication required.

### Data persistence

The compose file creates a named Docker volume (`cratedigger-data`) mounted at `/data` inside the container. This holds all reports, cache files, the discovery database, logs, and your dismissed items list. The volume persists across container restarts and image updates.

To mount a local music folder (only needed if you're not using Navidrome):

```yaml
# Uncomment in docker-compose.yaml:
- /path/to/your/music:/music:ro
```

---

## Configuration reference

Config resolution order (highest wins):

1. OS environment variables
2. `.env` file in the script directory
3. Hardcoded defaults in `DEFAULT_CONFIG`

In Docker, the compose file sets `HTML_OUT`, `CACHE_FILE`, `LOG_FILE`, `DISCOVER_HTML_OUT`, `DISCOVER_CACHE_FILE`, `DISCOVER_LOG_FILE`, `DATA_DIR`, and `MUSIC_ROOT` directly as environment variables. These override anything in `.env`.

### FUZZ_THRESHOLD

Controls how similar two names must be (0–100) to be considered the same. The default of 90 is deliberately strict — it prevents false positives like "Mogwai" matching "Mogwai (Live)". Lowering it below 85 risks marking albums as owned when they aren't. Raising it above 95 risks false negatives on artists with unusual punctuation or accents.

The matching algorithm is `rapidfuzz.fuzz.token_set_ratio`, which tokenizes both strings before comparing. This makes it order-independent and tolerant of minor word differences.

### TOP_ALBUM_LIMIT

How many of an artist's top Last.fm albums are fetched and inspected. The default of 25 is usually enough. If an artist has many compilations or live albums cluttering their top releases, increasing this gives the filter more to work through. Decreasing it speeds up runs but risks missing a studio album buried below position 25.

### TAG_INFO_CHECK_TOP_N

Only the top N albums (by playcount, within the fetched list) make individual `album.getInfo` API calls to retrieve tags. The rest are filtered by title keywords alone. The default is 3. This exists to reduce API calls — tag lookups double the number of requests per artist for the checked albums.

### DISCOVER_SIMILARITY_MODE

Controls how candidates discovered via `artist.getSimilar` are scored and filtered.

| Value | Behavior |
|-------|----------|
| `lastfm` (default) | Candidates are sorted by Last.fm's collaborative-filtering match score (shared listener overlap). `DISCOVER_TAG_OVERLAP` applies as a post-filter. |
| `tags` | Candidates are re-scored by Jaccard genre-tag similarity — the ratio of shared genre tags to total distinct genre tags between the candidate and its source artists. Any candidate with zero tag overlap is excluded, regardless of `DISCOVER_TAG_OVERLAP`. Results are sorted by Jaccard score descending. |

Use `tags` mode if you're seeing cross-genre mismatches — for example, an ambient artist appearing alongside hip-hop suggestions. Last.fm's listener overlap can produce these because audiences sometimes cross genre lines even when the music doesn't.

### DISCOVER_TAG_OVERLAP

The minimum number of matching genre tags required to keep a similar-artist candidate (in `lastfm` mode). The tag comparison ignores tags on a blocklist (platform tags, user-behavior tags like "seen live", and noise words like "music") that carry no real genre signal.

Set to `0` to disable genre filtering entirely — every candidate from `artist.getSimilar` passes through as long as they're not already in your collection.

Set to `2` or higher for stricter genre matching. Useful if you're getting suggestions that are similar to one of your artists in Last.fm's model but don't match the kind of music you actually want.

In `tags` mode this setting is ignored — `DISCOVER_MIN_JACCARD` controls the threshold instead.

### DISCOVER_TAG_TOP_N

(`tags` mode only.) Default: `5`, range 1–10.

Controls how many tags per artist are used when computing Jaccard similarity. Last.fm returns tags in descending weight order, so the top 5 represent an artist's strongest genre signals. Tags further down the list tend to be broad terms ("rock", "indie") that appear on so many artists that they inflate similarity scores between unrelated artists.

Lower this if suggestions still feel genre-mismatched — using only the top 2 or 3 tags produces a tighter genre match. Raise it if results feel too narrow and you want more breadth.

### DISCOVER_MIN_JACCARD

(`tags` mode only.) Default: `0.1`.

Minimum Jaccard score (0.0–1.0) a candidate must reach to appear in results. A score of 0.1 means at least 10% of the combined tag set must overlap. The old threshold was any score above 0.0, which let through candidates sharing a single low-signal tag.

Raise this to tighten results — 0.2 or 0.3 tends to keep only clear genre matches. Lower it toward 0.05 if your library is eclectic and you're seeing too few suggestions.

### SUGGESTIONS_PER_ARTIST

How many candidates are collected per local artist before deduplication. The default of 2 means each artist in your library contributes at most 2 new candidates to the global pool. Last.fm returns similar artists in descending similarity order, so only the strongest candidates are taken.

### DISCOVERY_FEEDS

Comma-separated list of sources for the New & Trending section. Default: all nine sources enabled. Valid values:

| Value | Source | Credentials |
|---|---|---|
| `spotify` | Spotify new releases API | `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET` |
| `lastfm` | Last.fm chart.getTopArtists | `LASTFM_API_KEY` (reused) |
| `bandcamp` | Bandcamp Daily RSS | None |
| `aoty` | Album of the Year RSS | None |
| `juno_electronic` | Juno new releases — electronic | None |
| `juno_hiphop` | Juno new releases — hip-hop/R&B | None |
| `juno_rock` | Juno new releases — rock/indie | None |
| `juno_main` | Juno new releases — all genres | None |
| `listenbrainz` | ListenBrainz fresh-releases Atom feed | `LISTENBRAINZ_USERNAME` |

Remove a source name to disable it. Leave `DISCOVERY_FEEDS` empty to hide the New & Trending section entirely.

> **If upgrading from v1.2.x:** this variable replaces `TRENDING_FEEDS`. The format is the same (comma-separated), but the name changed and the valid values have expanded. Update your `.env`.

### LASTFM_USERNAME

Your Last.fm username (not your API key). Used by the discovery engine to fetch `user.getTopArtists` across three time periods (7day, 1month, 3month) and build a taste profile. Without it, New & Trending still runs but every release scores 0 for taste-match and falls through to Genre Picks.

The taste profile is rebuilt once per day (24-hour TTL in the discovery database).

### LISTENBRAINZ_USERNAME

Your ListenBrainz username. Enables the `listenbrainz` feed in `DISCOVERY_FEEDS`. The feed fetches the ListenBrainz fresh-releases page for your account. Leave blank to skip this source.

### LISTENBRAINZ_TOKEN

Your ListenBrainz user token (found under your LB account settings). Optional — enables authenticated API calls. Without it, the feed still works but is limited to public data.

---

## Using the web dashboard

Open the dashboard at `http://localhost:8080/dashboard` (or your configured domain). You'll be prompted to log in with `AUTH_USER` / `AUTH_PASS`.

The dashboard has three job panels: Missing Popular Albums, Discover Similar Artists, and New & Trending. Each panel shows:

- A status badge: `idle`, `running`, `succeeded`, or `failed`. All start idle on first launch.
- A Run Now button that triggers the script immediately. It is disabled while a job is running. Clicking it when the job is already running returns 409 and does nothing.
- A live log area. When a job starts, the dashboard opens an SSE connection and streams log output line by line. If you reload mid-run, it replays the buffered output (up to 2000 lines) before resuming live.
- Next run time below each panel when a schedule is configured.

A **Run All** button at the top triggers all three jobs at once. It is disabled while any job is running.

A **Clear Cache** button at the top deletes the three output JSON files (`missing_popular_albums.json`, `discover_similar_artists.json`, `discovery_results.json`) and the two Last.fm cache files (`.cache/lastfm_top_albums.json`, `.cache/similar_artists.json`). A confirm dialog appears before anything is deleted. A toast notification confirms what was removed. `dismissed.json`, HTML reports, and log files are not affected. Use this before triggering a full re-run if you want a clean slate.

---

## Viewer features

The Report Viewer (`/`) combines all three reports into a single paginated card interface.

### Navigation

Each section (Discover Similar Artists, Missing Popular Albums, New & Trending) has its own Prev / Next pager. Pagination is AJAX — clicking Prev/Next replaces the cards in-place without a full page reload. The URL updates to reflect the current page, and browser back/forward navigation works correctly.

### Streaming preview

Hovering over album art reveals circular service icons centered on the image. Clicking a service icon:

1. Searches for the album on that service's API.
2. If found, replaces the album art with an embedded player (the × button returns to the artwork).
3. If not found (or service not configured), the icon briefly flashes red.

Only one player is open at a time — opening a second automatically closes the first.

If a Last.fm cover image fails to load (404 or network error), the card automatically retries using the iTunes Search API as a fallback — no credentials required. If that also fails, the card shows a "No Artwork" placeholder.

Service setup:

| Service | Credentials needed | Notes |
|---|---|---|
| Apple Music | None | Works immediately. Uses iTunes Search API + Apple Music embed. |
| Spotify | `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET` | Requires a Spotify developer app (Spotify Premium account needed as of Feb 2026). |
| YouTube | `YOUTUBE_API_KEY` | Must be a YouTube Data API v3 key. In Google Cloud Console, go to APIs & Services > Library > search "YouTube Data API v3" > Enable. A key created only for the IFrame Player API will not work. |

After adding credentials to `.env`, restart the container (`docker compose down && docker compose up -d`) — no rebuild required.

### SLSKD search queue

When SLSKD integration is configured, each card shows a SLSKD button in the action bar. Clicking it sends a search request directly to your running SLSKD instance via its REST API — no copy-paste, no tab-switching. The search appears in the SLSKD UI immediately for you to browse results and queue downloads.

Set `SLSKD_URL` and either `SLSKD_API_KEY` or `SLSKD_USER` / `SLSKD_PASS` in your `.env` to enable the button. The API key approach is simpler: add a key to `appsettings.yml` under `web.authentication.api_keys` in SLSKD.

After adding credentials, restart the container — no rebuild required.

### Copy button

Each card has a Copy button that copies the artist name and album title to the clipboard (e.g., `Radiohead OK Computer`). Useful for searching in a music store or download manager.

### Dismiss

The ✕ button on each card permanently hides it from the viewer. Dismissed items are stored in `/data/dismissed.json` (inside the Docker volume, so they survive container restarts and rebuilds). They are also excluded when the scripts run — so a re-run won't re-surface albums you've already dismissed.

### Help page

The Help page (`/help`) has two sections:

**Debug Log** — opens a collapsible panel showing the last 1000 lines from the application logs, sourced from `/data/missing_popular_albums.log`, `/data/discover_similar_artists.log`, and an in-memory ring buffer. ERROR lines are highlighted red, WARNING yellow, DEBUG dim. A Refresh button re-fetches without closing the panel. Useful when a job reports failure and you want to see why without shelling into the container.

**Submit Request** — opens a modal with a pre-filled GitHub Issues URL. Clicking through takes you to a new issue form with the label, title, and body already populated. Handy for filing a bug report without having to find the repo URL.

### Version badge

The navbar version number (top right) changes color based on the running version vs. the latest GitHub release: green means you're current, red means you're behind and links to the releases page. The check runs once at page load and caches the result in `localStorage` for one hour. If you're offline or GitHub is unreachable, the badge stays its default color.

### Triggering via API

You can trigger runs without the browser:

```bash
# Trigger missing_popular_albums.py
curl -X POST -u admin:yourpass https://cratedigger.yourdomain.com/run/missing

# Trigger discover_similar_artists.py
curl -X POST -u admin:yourpass https://cratedigger.yourdomain.com/run/discover

# Refresh the New & Trending section
curl -X POST -u admin:yourpass https://cratedigger.yourdomain.com/run/trending

# Check status
curl -u admin:yourpass https://cratedigger.yourdomain.com/status/missing
```

The status response looks like:

```json
{
  "job_id": "missing",
  "status": "succeeded",
  "pid": 42,
  "started_at": "2026-03-18T03:00:01.123456+00:00",
  "finished_at": "2026-03-18T03:14:22.654321+00:00",
  "exit_code": 0
}
```

---

## New & Trending section

The Report Viewer's third section uses a taste-aware discovery engine (`webapp/discovery.py`). It pulls from up to 9 sources, scores results against your Last.fm scrobble history, and organizes them into three subsections.

### Sources

| Source name | What it pulls |
|---|---|
| `spotify` | Spotify new-releases API (requires `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`) |
| `lastfm` | `chart.getTopArtists` top 50, each artist's most popular album (requires `LASTFM_API_KEY`) |
| `bandcamp` | Bandcamp Daily RSS editorial picks (no credentials) |
| `aoty` | Album of the Year RSS (no credentials) |
| `juno_electronic` | Juno new releases — electronic (no credentials) |
| `juno_hiphop` | Juno new releases — hip-hop/R&B (no credentials) |
| `juno_rock` | Juno new releases — rock/indie (no credentials) |
| `juno_main` | Juno new releases — all genres (no credentials) |
| `listenbrainz` | ListenBrainz fresh-releases Atom feed for your account (requires `LISTENBRAINZ_USERNAME`) |

Control which sources are active with `DISCOVERY_FEEDS` in your `.env`.

### Taste profile

When `LASTFM_USERNAME` is set, the engine fetches `user.getTopArtists` across three time periods (7day, 1month, 3month) and blends the results by weight to build a ranked artist list. It also fetches similar artists and genre tags for the top 20 of those. The profile is stored in the discovery SQLite database and rebuilt once per day.

Without `LASTFM_USERNAME`, the taste profile is skipped. Every release scores 0 for taste-match and falls through to Genre Picks.

### Scoring

Each release is scored against the taste profile:

| Component | Max points | Condition |
|---|---|---|
| `known_artist` | 40 | Artist is in your top scrobbled artists (40 for top 25, 25 for top 100, 15 for rank 100+). Also 15 if the artist is in your Navidrome library but not in your scrobble history at all. |
| `related_artist` | 25 | Artist is similar to one of your top artists (25 if related via 2+ seeds, 12 if 1 seed) |
| `trend` | 10 | Release appeared in 3+ sources (10), 2 sources (5), or 1 source (0) |
| `recency` | 10 | Release date within the last 30 days |

The Navidrome library check is a separate pass after Last.fm scrobble scoring. If Navidrome credentials are not configured, only scrobble history is used.

Genre scoring is reserved for a future release — the field exists in the database but is currently always 0.

### Subsections

Results are organized into three sections based on their score:

- **New From Your Artists** — `known_artist` score ≥ 15. Releases from artists you scrobble on Last.fm, or from any artist in your Navidrome library (when Navidrome credentials are configured).
- **Trending Near Your Taste** — `related_artist` score > 0. Releases from artists similar to your top scrobbled artists.
- **Genre Picks** — Everything else with a positive total score. Appears when `LASTFM_USERNAME` is not set or for releases that match only on trend/recency.

### Reason text

Each card in New & Trending shows a short reason line explaining why it appeared. The text is generated by the scoring engine, not the source feed:

| Reason | Condition |
|---|---|
| "New release from one of your top artists" | `known_artist` = 40 (top 25 scrobbles) |
| "New release from an artist you listen to often" | `known_artist` = 25 (top 100 scrobbles) |
| "New release from an artist in your listening history" | `known_artist` = 15, matched via scrobble rank > 100 |
| "New release from an artist in your collection" | `known_artist` = 15, matched via Navidrome library (not scrobble history) |
| "Related to X and Y" / "Related to X" | `related_artist` > 0 |
| "Trending across N discovery sources" | Multiple sources, no taste match |

### Source badges

Cards show a colored badge for each source the release was found in:

| Badge | Color | Source |
|---|---|---|
| Spotify | green | `spotify` |
| Last.fm | red | `lastfm` |
| Bandcamp | teal | `bandcamp` |
| AOTY | purple | `aoty` |
| Juno | orange | any `juno_*` feed |
| LB | teal | `listenbrainz` |

A release appearing in multiple sources shows multiple badges.

### Storage and cache

Releases, source mappings, scores, and the taste profile are stored in `/data/discovery.db` (SQLite). This is part of the `cratedigger-data` volume and persists across restarts.

The rendered result set is cached for 2 hours. To force a refresh, use the Run Now button on the New & Trending dashboard panel, or `POST /run/trending`. Owned albums are filtered out when Navidrome credentials are configured.

---

## Full-page section view

Clicking a section title ("Discover Similar Artists", "Missing Popular Albums", or "New & Trending") in the Report Viewer opens a full-page view at `/section/{section}`. Items are shown 100 per page with URL-based Prev/Next navigation (`?page=N`). This replaces the previous behavior of loading all items at once, which was impractically slow with large result sets. A back link at the top returns to the main viewer.

Dismiss buttons work the same way on the full-page view.

---

## Mobile support

The navigation bar adapts at 640px and 480px breakpoints. The version label is hidden below 640px; the GitHub icon is hidden below 480px. Dashboard cards stack to full width below 480px. Album card action buttons have larger tap targets on mobile.

---

## Scheduling

Schedules use standard 5-field cron expressions:

```
minute  hour  day-of-month  month  day-of-week
```

Examples:

| Expression | Meaning |
|---|---|
| `0 3 * * 0` | Every Sunday at 3:00 AM |
| `0 4 * * 0` | Every Sunday at 4:00 AM |
| `0 6 * * 1-5` | Weekdays at 6:00 AM |
| `0 */12 * * *` | Every 12 hours |
| `30 2 1 * *` | 1st of every month at 2:30 AM |

The scheduler runs inside the FastAPI process using APScheduler's `AsyncIOScheduler`. Times are interpreted in the container's timezone (`TZ` env var). If a previous run is still in progress when the next one is scheduled, the scheduler skips the new run rather than stacking them.

A malformed cron expression logs a warning and the job is not scheduled — the container still starts normally.

---

## Reading the HTML reports

### Missing Popular Albums report

Each card is one artist with at least one album gap, sorted alphabetically by artist name.

- Album title at the top, artist name below it.
- Cover art fetched from Last.fm at 600×600. Shows "No Artwork" if Last.fm had no image.
- Links to Last.fm (album page), Discogs (release search), Bandcamp (search), and YouTube Music (search). All open in a new tab.
- A copy button that puts "Artist Album" as plain text on the clipboard. Useful for pasting into Discogs or a search box.

The report header shows a timestamp and counts.

### Discover Similar Artists report

<p align="center">
  <img src="screenshots/Discover.png" alt="Discover Similar Artists report" width="640">
</p>

Cards are sorted by Last.fm similarity score descending — strongest matches first.

- Artist name links to their Last.fm page.
- Top album title and playcount below the name.
- "Similar to" line showing which local artists triggered the suggestion. If more than four are listed, the rest are collapsed to "+N more".
- Matched tag chips (in `tags` mode) — up to 5 small genre tags shown below the "Similar to" line, indicating which tags the candidate shared with the source artists. These exist to make the match legible: if you see "post-rock / ambient / drone" you know why the suggestion appeared.
- A percentage badge in the top-right corner showing the similarity score.
- Same search links as above.

---

## How matching works

### Name normalization

Before any comparison, both local and remote names go through the same pipeline:

1. Strip diacritics (NFKD decompose, drop combining characters)
2. Lowercase
3. Replace `&` with `and`
4. Replace non-word characters with spaces
5. Collapse multiple spaces
6. Strip leading "The " prefix

So "The National", "the national", and "Thé National" all normalize to `national`.

Album titles go through the same pipeline plus: strip parenthesized text, strip edition keywords (Deluxe, Remaster, 20th Anniversary, etc.).

### Fuzzy matching

Direct string equality on the normalized forms is checked first. If that fails, `rapidfuzz.fuzz.token_set_ratio` runs. This algorithm tokenizes both strings, sorts the tokens, and compares the intersection against each full string and the difference. It handles extra words like "featuring X" and reordering, but is strict about core token content.

`FUZZ_THRESHOLD` (default 90) is applied to the 0–100 score. Exact normalized matches always score 100.

### What gets filtered out

The following are never reported as missing albums:

- Albums tagged on Last.fm as: compilation, live, single, soundtrack
- Albums whose titles contain (after normalization): live, compilation, greatest hits, best of, remix, remixes, anthology, collection, expanded, deluxe, deluxe edition, reissue, mixtape, karaoke, instrumental collection, instrumental compilation, soundtrack, single
- Exception: if an album is tagged `album` or `ep` by Last.fm, the title keyword filter is bypassed — the positive tag wins.

Artists are excluded if their name contains: various artists, various artist, soundtrack, ost, score, motion picture, original soundtrack, dj mix.

---

## The --trace-artist diagnostic

`--trace-artist` is only in `missing_popular_albums.py` and only works when Navidrome is configured. It uses album IDs from the Navidrome scan to look up filesystem paths via `getAlbum.view`.

```bash
python missing_popular_albums.py --trace-artist "Arca"
```

Output:

```
Artist: Arca
  Album: Kick i
    Path: /music/Arca/2020 - Kick i
  Album: Mutant
    Path: /music/Arca/2015 - Mutant
```

Use this when:

- The report claims you're missing an album you know you own. The paths show what directory Navidrome is actually reading, so you can verify the tags and folder structure.
- An artist is absent from the report and you want to confirm they were scanned and which albums were found.
- You suspect a naming mismatch — the path reveals the exact album name that was registered, which you can then compare to what Last.fm returns.

If Navidrome returns no songs for an album (unlikely but possible), the tool prints the album ID instead of a path.

---

## NAVIDROME_MUSIC_FOLDER

If you have multiple libraries in Navidrome (e.g., a main music library and a classical library), `NAVIDROME_MUSIC_FOLDER` restricts the scan to one of them. Without it, every library gets scanned, which can pull in artists you didn't intend to include.

To find your library name: in Navidrome, go to Settings > Libraries. The name shown there is what to use. The match is case-insensitive.

If the name is wrong, the script calls `getMusicFolders`, tries to resolve the name, and aborts immediately if nothing matches:

```
ERROR: NAVIDROME_MUSIC_FOLDER='music_main' not found. Available: Music Server, Classical
```

This is by design. A silent fallback to all libraries could produce a report that mixes libraries you didn't want combined — better to fail loudly.

If `NAVIDROME_MUSIC_FOLDER` is not set or is empty, the scan queries all libraries without passing a `musicFolderId` to `getAlbumList2`.

---

## Cache management

### What is cached

`missing_popular_albums.py` caches Last.fm top-album data per artist (including tags for the top three albums) in `.cache/lastfm_top_albums.json`. Keys are normalized artist names.

`discover_similar_artists.py` caches three datasets in `.cache/similar_artists.json`:

- `similar` — the similar-artist results per local artist
- `top_albums` — top-album data per candidate artist
- `tags` — genre tags per artist

Both cache files include a version number. A version mismatch (caused by a `CACHE_VERSION` change in the code) causes a complete cache discard on the next run, then rebuilds from scratch.

The discovery engine stores its data in `/data/discovery.db` (SQLite). The rendered result set is cached for 2 hours. The taste profile has a 24-hour TTL.

### When to clear the cache

The Last.fm cache doesn't auto-expire, so you only need to clear it if:

- You've added many new artists and want to force a full re-fetch (though new artists will be fetched fresh on cache miss anyway)
- Last.fm changed their data significantly for an artist
- You're seeing incorrect results and want to rule out stale cache

### How to clear the cache

**From the web UI:** use the Clear Cache button on the Run Dashboard. This deletes the three output JSON files and both Last.fm cache files in one step.

**From the command line:**

```bash
rm .cache/lastfm_top_albums.json   # missing_popular_albums
rm .cache/similar_artists.json     # discover_similar_artists
```

Or run with `--no-cache` to ignore the cache for one run without deleting it. Fresh data is written back either way.

In Docker, the cache lives at `/data/.cache/` inside the container. The `cratedigger-data` Docker volume keeps it intact across restarts and image updates.

To reset the discovery database (releases, scores, taste profile), delete `/data/discovery.db` — it will be recreated on the next run.

### Cache size

A library of ~800 artists produces cache files in the 17–36 MB range. The similar-artists cache is typically larger because it stores data for both your local artists and their candidates.

---

## Troubleshooting

### "No artists discovered in local library"

With Navidrome: check credentials by opening the Navidrome web UI directly. Confirm `NAVIDROME_URL` includes the correct scheme and no trailing slash issues (the script strips trailing slashes). Check the log for HTTP errors.

With filesystem scan: verify `MUSIC_ROOT` is the correct path and the process has read permission. Directories with fewer than 2 audio files are skipped.

### Artist present in library but not in report

A few possible causes:

- Last.fm has no top albums for this artist (niche or misspelled name). Check the log for `No albums found on Last.fm for <name>`.
- The artist's top album is already in your library, correctly matched.
- The artist name normalizes to something Last.fm doesn't recognize. Use `--trace-artist "Name"` to see what album names were indexed locally, then search manually on Last.fm to compare.

### Album appears in report but you own it

The fuzzy matcher didn't connect the local album name to the Last.fm album name. Use `--trace-artist "Artist Name"` to see the exact local album name and compare it to the Last.fm title. If there's a real mismatch (e.g., Last.fm calls it "OK Computer" and your tags say "OK Computer OKNOTOK 1997-2017"), the album title keyword filter should catch the edition tag — but if not, lowering `FUZZ_THRESHOLD` may help at the cost of more false positives elsewhere.

### Script runs slowly

Increase `DEFAULT_WORKERS` or pass `--workers N`. The upper bound is `MAX_WORKERS`. The first run on a large library is always slow (all cache misses). Subsequent runs on a warm cache are much faster. Don't reduce `REQUEST_DELAY_MIN` below 0.1 — that's where Last.fm rate limiting starts.

### Last.fm rate limit errors

These log as `Rate limited by Last.fm`. The client retries with exponential backoff up to `MAX_RETRIES` times. If you see persistent rate limit errors, increase `REQUEST_DELAY_MIN` and `REQUEST_DELAY_MAX` or reduce `DEFAULT_WORKERS`.

### Docker container exits immediately

Check `docker logs cratedigger`. Common causes:

- Port 8080 is already in use. Change the host-side port in the compose file.
- Missing env vars causing an import error. Check whether required variables are set.
- Note: an empty `AUTH_PASS` does not crash the container — it just rejects every login. Verify by hitting the health check endpoint: `curl http://localhost:8080/healthz`.

### discover_similar_artists.py returns far fewer results than expected

- `DISCOVER_TAG_OVERLAP` may be filtering aggressively. Try setting it to `0` temporarily to see the unfiltered candidate count in the log.
- In `tags` mode, `DISCOVER_MIN_JACCARD` (default 0.1) and `DISCOVER_TAG_TOP_N` (default 5) both affect how many candidates survive. Try lowering `DISCOVER_MIN_JACCARD` to `0.05` or raising `DISCOVER_TAG_TOP_N` to `8` or `10` to widen the match set.
- `SUGGESTIONS_PER_ARTIST` limits how many candidates each local artist contributes. Increasing it raises the global pool size.
- Artists with no Last.fm similar-artist data are skipped silently.

### New & Trending shows no personalized results

Set `LASTFM_USERNAME` in your `.env` and restart the container. The taste profile is built on the next discovery run. You can force it immediately by clicking Run Now on the New & Trending panel. Without a taste profile, all releases fall through to Genre Picks scored only on trend (appearing in multiple sources) and recency.

### New & Trending is missing from the viewer

`DISCOVERY_FEEDS` is empty or not set to any valid source names. Check your `.env`. If you were previously using `TRENDING_FEEDS` (v1.2.x), rename it to `DISCOVERY_FEEDS` and add any new source names you want enabled.

---

## FAQ

**Why does the report show only one album per artist?**

The goal is a prioritized action list, not an exhaustive backlog. One album per artist keeps the report scannable — you can open it, spot ten artists you care about, and go shopping. Five albums per artist and it becomes a spreadsheet.

**Why are live albums and compilations excluded?**

They have high playcounts on Last.fm but you probably don't want them showing up ahead of a studio album. The filter removes them so the top result is almost always a proper studio release or EP.

**Why does `--no-cache` still write a cache file?**

`--no-cache` means "don't trust the existing cache this time" — it still saves fresh data so the next run is fast. If you want to discard the cache entirely, delete the file manually or use the Clear Cache button on the dashboard.

**Can I run both scripts at the same time?**

Yes. From the CLI they write to different log files and output files. From the web dashboard each job is independent — running one doesn't block the other.

**Why does the discover script drop candidates with no qualifying album?**

A suggestion with no known album isn't actionable. You'd have no album to look up or listen to. The filter keeps the report focused on candidates you can actually do something with.

**The similarity scores seem arbitrary. What do they mean?**

They're Last.fm's own similarity scores from `artist.getSimilar`, expressed as a value between 0 and 1 (displayed as a percentage). Last.fm computes them from listening patterns across their user base — a score of 85% means people who listen to the source artist also frequently listen to the candidate. The exact computation isn't public.

**Why is Navidrome preferred over the filesystem scan?**

Speed and accuracy. The filesystem scan reads audio tags from every file, which is slow over a network mount and brittle if tags are inconsistent. Navidrome has already indexed everything and returns structured JSON in a few seconds. It also gives the script album IDs, which is what makes `--trace-artist` work.

**Can I filter to multiple Navidrome libraries?**

No. `NAVIDROME_MUSIC_FOLDER` is a single-value filter. If you want to scan multiple specific libraries but not all of them, leave `NAVIDROME_MUSIC_FOLDER` empty (scans everything) or run the script separately for each library with different env configs.

**Why does the New & Trending Genre Picks section show releases unrelated to my taste?**

Genre Picks catches everything with a positive total score — which currently means trend score (appearing in 2+ sources) or recency score (released in the last 30 days). Genre-based filtering is not yet implemented; the field is reserved in the scoring engine but always evaluates to 0. If you only want taste-matched results, look at the first two sections.
