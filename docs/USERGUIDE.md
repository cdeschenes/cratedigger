# User guide — Missing Popular Albums

This guide covers how both scripts work, how to set them up, and how to get the most out of the web dashboard. It assumes you've read the README and have a working environment.

---

## Table of contents

1. [How the scripts work](#how-the-scripts-work)
2. [Setup — CLI](#setup--cli)
3. [Setup — Docker](#setup--docker)
4. [Configuration reference](#configuration-reference)
5. [Using the web dashboard](#using-the-web-dashboard)
6. [Viewer features](#viewer-features)
7. [Scheduling](#scheduling)
8. [Reading the HTML reports](#reading-the-html-reports)
8. [How matching works](#how-matching-works)
9. [The --trace-artist diagnostic](#the---trace-artist-diagnostic)
10. [NAVIDROME_MUSIC_FOLDER](#navidrome_music_folder)
11. [Cache management](#cache-management)
12. [Troubleshooting](#troubleshooting)
13. [FAQ](#faq)

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

**Genre tag filter.** If `DISCOVER_TAG_OVERLAP` is greater than zero, the script fetches top genre tags for every candidate and every source artist in parallel. A candidate passes if it shares at least `DISCOVER_TAG_OVERLAP` non-trivial tags with at least one of its source artists. Tags like "seen live", "favourite", and "awesome" are on a blocklist and don't count. If a candidate has no tags at all, or all source artists have no tags, it passes through rather than being dropped silently.

**Top album enrichment.** Each surviving candidate gets its most popular qualifying album fetched from Last.fm. Candidates with no qualifying album are dropped. The remaining suggestions are sorted by similarity score descending.

---

## Setup — CLI

### 1. Python version

Python 3.12 is required. Check with `python3 --version`. On macOS, use [pyenv](https://github.com/pyenv/pyenv) or [Homebrew](https://brew.sh/) to install 3.12 if needed.

### 2. Create and activate a virtualenv

```bash
cd /path/to/Scripts/missing_popular_albums
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

The compose file lives at `Docker/music-reports/docker-compose.yaml`. It builds the image from `Scripts/missing_popular_albums/`.

### 1. Create the Docker env file

Create `Docker/music-reports/.env` with at minimum:

```dotenv
# Required
LASTFM_API_KEY=your_key_here
AUTH_PASS=choose_a_strong_password

# Navidrome
NAVIDROME_URL=https://navidrome.example.com
NAVIDROME_USER=youruser
NAVIDROME_PASS=yourpass
NAVIDROME_MUSIC_FOLDER=music_main   # or leave empty to scan all

# Optional schedules (cron format, 5 fields)
SCHEDULE_MISSING=0 3 * * 0
SCHEDULE_DISCOVER=0 4 * * 0

# Streaming previews (optional)
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
YOUTUBE_API_KEY=

# SLSKD integration (optional)
SLSKD_URL=https://slskd.yourdomain.com
SLSKD_API_KEY=your-api-key

# From Docker/_defaults/default.env — set these if they're not inherited
TZ=America/Los_Angeles
DPATH=/opt/docker/appdata
NASPATH=/media/NAS
```

Variables inherited from `Docker/_defaults/default.env` (PROXYNETWORK, WATCHTOWER, HP_*, PORT_INTERNAL, DOMAIN_NAME, SERVICE_NAME, PROXY_ENTRYPOINT, PROXY_ENTRYPOINT_SECURE) must be available in the shell environment or in the defaults env file when you run compose.

### 2. Start the container

```bash
cd Docker/music-reports
docker compose up -d --build
```

### 3. Verify

```bash
docker logs -f music-reports
curl http://localhost:5099/healthz
```

The health check endpoint returns `{"status": "ok"}` with no authentication.

### Data persistence

The compose file mounts two volumes:

- `${DPATH}/music-reports/data` → `/data` (read-write) — stores all reports, cache files, and logs. This directory must exist on the host before the first run.
- `${NASPATH}/Media/Music` → `/music` (read-only) — the NAS music folder, used when Navidrome is not configured.

Create the data directory if needed:

```bash
mkdir -p /opt/docker/appdata/music-reports/data
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

### DISCOVER_TAG_OVERLAP

The minimum number of matching genre tags required to keep a similar-artist candidate. The tag comparison ignores tags on a blocklist (seen live, favourite, awesome, etc.) that carry no real genre signal.

Set to `0` to disable genre filtering entirely — every candidate from `artist.getSimilar` passes through as long as they're not already in your collection.

Set to `2` or higher for stricter genre alignment. Useful if you're getting suggestions that are similar to one of your artists in Last.fm's model but don't match the kind of music you actually want.

### SUGGESTIONS_PER_ARTIST

How many candidates are collected per local artist before deduplication. The default of 2 means each artist in your library contributes at most 2 new candidates to the global pool. Last.fm returns similar artists in descending similarity order, so only the strongest candidates are taken.

---

## Using the web dashboard

Open the dashboard at your configured domain (or `http://localhost:5099` for local access). You'll be prompted for HTTP Basic Auth credentials (`AUTH_USER` / `AUTH_PASS`).

The dashboard has two job panels, one per script. Each shows:

- A status badge: `idle`, `running`, `succeeded`, or `failed`. Both start idle on first launch.
- A **Run Now** button that triggers the script immediately. It's disabled while a job is running. Clicking it when the job is already running — e.g., from a scheduled trigger — returns 409 and does nothing.
- A live log area. When a job starts, the dashboard opens an SSE connection and streams log output line by line. If you reload mid-run, it replays the buffered output (up to 2000 lines) before resuming live.
- A **View in Viewer** link that appears once JSON output is available. Opens the combined report viewer.
- Next run time below each panel when a schedule is configured.

---

## Viewer features

The Report Viewer (`/`) combines both reports into a single paginated card interface.

### Navigation

Each section (Discover Similar Artists, Missing Popular Albums) has its own Prev / Next pager. Pagination is AJAX — clicking Prev/Next replaces the cards in-place without a full page reload. The URL updates to reflect the current page, and browser back/forward navigation works correctly.

### Streaming preview

Hovering over album art reveals circular service icons centered on the image. Clicking a service icon:

1. Searches for the album on that service's API.
2. If found, replaces the album art with an embedded player (the × button returns to the artwork).
3. If not found (or service not configured), the icon briefly flashes red.

Only one player is open at a time — opening a second automatically closes the first.

**Service setup:**

| Service | Credentials needed | Notes |
|---|---|---|
| Apple Music | None | Works immediately. Uses iTunes Search API + Apple Music embed. |
| Spotify | `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET` | Requires a Spotify developer app (Spotify Premium account needed as of Feb 2026). |
| YouTube | `YOUTUBE_API_KEY` | Must be a **YouTube Data API v3** key. In Google Cloud Console, go to APIs & Services → Library → search "YouTube Data API v3" → Enable. A key created only for the IFrame Player API will not work. |

After adding credentials to `Docker/music-reports/.env`, restart the container (`docker compose down && docker compose up -d`) — no rebuild required.

### SLSKD search queue

When SLSKD integration is configured, each card shows a **SLSKD** button in the action bar. Clicking it sends a search request directly to your running SLSKD instance via its REST API — no copy-paste, no tab-switching. The search appears in the SLSKD UI immediately for you to browse results and queue downloads.

Set `SLSKD_URL` and either `SLSKD_API_KEY` or `SLSKD_USER` / `SLSKD_PASS` in your `.env` to enable the button. The API key approach is simpler: add a key to `appsettings.yml` under `web.authentication.api_keys` in SLSKD.

After adding credentials, restart the container — no rebuild required.

### Copy button

Each card has a **Copy** button that copies the artist name and album title to the clipboard (e.g., `Radiohead OK Computer`). Useful for searching in a music store or download manager.

### Dismiss

The **✕** button on each card permanently hides it from the viewer. Dismissed items are stored in `/data/dismissed.json` (inside the Docker volume, so they survive container restarts and rebuilds). They are also excluded when the scripts run — so a re-run won't re-surface albums you've already dismissed.

### Triggering via API

You can trigger runs without the browser:

```bash
# Trigger missing_popular_albums.py
curl -X POST -u admin:yourpass https://music-reports.yourdomain.com/run/missing

# Trigger discover_similar_artists.py
curl -X POST -u admin:yourpass https://music-reports.yourdomain.com/run/discover

# Check status
curl -u admin:yourpass https://music-reports.yourdomain.com/status/missing
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

Cards are sorted by Last.fm similarity score descending — strongest matches first.

- Artist name links to their Last.fm page.
- Top album title and playcount below the name.
- "Similar to" line showing which local artists triggered the suggestion. If more than four are listed, the rest are collapsed to "+N more".
- A percentage badge in the top-right corner showing Last.fm's similarity score.
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

**How to find your library name:** In Navidrome, go to Settings > Libraries. The name shown there is what to use. The match is case-insensitive.

**What happens if the name is wrong:** The script calls `getMusicFolders`, tries to resolve the name, and aborts immediately if nothing matches:

```
ERROR: NAVIDROME_MUSIC_FOLDER='music_main' not found. Available: Music Server, Classical
```

This is by design. A silent fallback to all libraries could produce a report that mixes libraries you didn't want combined — better to fail loudly.

**Leaving it empty:** If `NAVIDROME_MUSIC_FOLDER` is not set or is empty, the scan queries all libraries without passing a `musicFolderId` to `getAlbumList2`.

---

## Cache management

### What is cached

`missing_popular_albums.py` caches Last.fm top-album data per artist (including tags for the top three albums) in `.cache/lastfm_top_albums.json`. Keys are normalized artist names.

`discover_similar_artists.py` caches three datasets in `.cache/similar_artists.json`:

- `similar` — the similar-artist results per local artist
- `top_albums` — top-album data per candidate artist
- `tags` — genre tags per artist

Both cache files include a version number. A version mismatch (caused by a `CACHE_VERSION` change in the code) causes a complete cache discard on the next run, then rebuilds from scratch.

### When to clear the cache

The cache doesn't auto-expire, so you only need to clear it if:

- You've added many new artists and want to force a full re-fetch (though new artists will be fetched fresh on cache miss anyway)
- Last.fm changed their data significantly for an artist
- You're seeing incorrect results and want to rule out stale cache

### How to clear the cache

Delete the relevant JSON file:

```bash
rm .cache/lastfm_top_albums.json   # missing_popular_albums
rm .cache/similar_artists.json     # discover_similar_artists
```

Or run with `--no-cache` to ignore the cache for one run without deleting it. Fresh data is written back either way.

In Docker, the cache lives at `/data/.cache/` inside the container, which maps to `${DPATH}/music-reports/data/.cache/` on the host.

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

Check `docker logs music-reports`. Common causes:

- Port 5099 is already in use. Change the host-side port in the compose file.
- Missing env vars causing an import error. Check whether required variables are set.
- Note: an empty `AUTH_PASS` does not crash the container — it just rejects every login. Verify by hitting the health check endpoint: `curl http://localhost:5099/healthz`.

### discover_similar_artists.py returns far fewer results than expected

- `DISCOVER_TAG_OVERLAP` may be filtering aggressively. Try setting it to `0` temporarily to see the unfiltered candidate count in the log.
- `SUGGESTIONS_PER_ARTIST` limits how many candidates each local artist contributes. Increasing it raises the global pool size.
- Artists with no Last.fm similar-artist data are skipped silently.

---

## FAQ

**Why does the report show only one album per artist?**

The goal is a prioritized action list, not an exhaustive backlog. One album per artist keeps the report scannable — you can open it, spot ten artists you care about, and go shopping. Five albums per artist and it becomes a spreadsheet.

**Why are live albums and compilations excluded?**

They have high playcounts on Last.fm but you probably don't want them showing up ahead of a studio album. The filter removes them so the top result is almost always a proper studio release or EP.

**Why does `--no-cache` still write a cache file?**

`--no-cache` means "don't trust the existing cache this time" — it still saves fresh data so the next run is fast. If you want to discard the cache entirely, delete the file manually.

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
