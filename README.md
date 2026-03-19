# Missing Popular Albums

Two music discovery scripts that scan your local collection and generate HTML reports from Last.fm data. Run them from the command line or let a Docker-hosted web dashboard trigger and schedule them for you.

**Missing Popular Albums** — for every artist you own, finds the single highest-playcount album or EP you don't have yet.

**Discover Similar Artists** — queries Last.fm for artists similar to those in your collection, filters out anything you already own, and surfaces the top recommendation per candidate with their most popular album.

<p align="center">
  <img src="screenshots/htmloutput.png" alt="Missing Popular Albums report screenshot" width="640">
</p>

---

## Requirements

- Python 3.12+
- A [Last.fm API key](https://www.last.fm/api) (free)
- Either a [Navidrome](https://www.navidrome.org/) instance (recommended) or a local music directory readable by the script

---

## Quick Start — CLI

```bash
cd Scripts/missing_popular_albums

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: set LASTFM_API_KEY and NAVIDROME_* (or MUSIC_ROOT)

python missing_popular_albums.py
python discover_similar_artists.py
```

Reports are written to `missing_popular_albums.html` and `discover_similar_artists.html` in the same directory (overridable via `.env`).

---

## Quick Start — Docker

```bash
# 1. Edit Docker/music-reports/.env — set LASTFM_API_KEY, AUTH_PASS,
#    and NAVIDROME_* credentials

cd Docker/music-reports
docker compose up -d --build

# 2. Open https://<your-domain>  (or http://localhost:5099)
#    Log in with AUTH_USER / AUTH_PASS
```

The dashboard lets you run either script on demand, watch live log output, and view the generated reports in the browser.

---

## Configuration

All settings live in `.env` (CLI) or `Docker/music-reports/.env` (Docker). The Docker compose file pins the output paths to `/data/*` — do not set those manually when using Docker.

### Library / API

| Variable | Default | Required | Purpose |
|---|---|---|---|
| `LASTFM_API_KEY` | | Yes | Last.fm API key. Get one at last.fm/api. |
| `MUSIC_ROOT` | `/Volumes/NAS/Media/Music/Music_Server` | Fallback | Filesystem path to scan when Navidrome is not configured. |
| `NAVIDROME_URL` | | No | Base URL of your Navidrome instance, e.g. `https://navidrome.example.com` |
| `NAVIDROME_USER` | | No | Navidrome username |
| `NAVIDROME_PASS` | | No | Navidrome password |
| `NAVIDROME_MUSIC_FOLDER` | | No | Navidrome library name to restrict the scan to. If set and the name doesn't match, the script aborts and lists available names. Leave empty to scan all libraries. |

All three of `NAVIDROME_URL`, `NAVIDROME_USER`, and `NAVIDROME_PASS` must be set to use Navidrome. If any are missing, the script falls back to the filesystem scan.

### Script tuning

| Variable | Default | Purpose |
|---|---|---|
| `FUZZ_THRESHOLD` | `90` | Fuzzy-match sensitivity (0–100). Lower = more permissive matching. Rarely needs changing. |
| `DEFAULT_WORKERS` | `4` | Concurrent Last.fm requests per run. |
| `MAX_WORKERS` | `8` | Upper bound enforced by `--workers`. |
| `TOP_ALBUM_LIMIT` | `25` | How many of an artist's top albums to fetch from Last.fm. |
| `REQUEST_TIMEOUT` | `15` | HTTP timeout in seconds. |
| `REQUEST_DELAY_MIN` | `0.15` | Minimum random delay between Last.fm requests (seconds). |
| `REQUEST_DELAY_MAX` | `0.3` | Maximum random delay between Last.fm requests (seconds). |
| `MAX_RETRIES` | `3` | Retry attempts on Last.fm API errors. |
| `CACHE_VERSION` | `2` | Internal. Increment to force a full cache refresh after structural changes. |

### Output paths

| Variable | Default | Purpose |
|---|---|---|
| `HTML_OUT` | `missing_popular_albums.html` | Output path for the missing albums report. |
| `CACHE_FILE` | `.cache/lastfm_top_albums.json` | Cache file for Last.fm top-album data. |
| `LOG_FILE` | `missing_popular_albums.log` | Log file for `missing_popular_albums.py`. |
| `DISCOVER_HTML_OUT` | `discover_similar_artists.html` | Output path for the similar artists report. |
| `DISCOVER_CACHE_FILE` | `.cache/similar_artists.json` | Cache file for similar-artist and tag data. |
| `DISCOVER_LOG_FILE` | `discover_similar_artists.log` | Log file for `discover_similar_artists.py`. |

### discover_similar_artists.py only

| Variable | Default | Purpose |
|---|---|---|
| `SUGGESTIONS_PER_ARTIST` | `2` | Max candidate artists to collect per local artist from Last.fm's similar-artist list. |
| `SIMILAR_ARTIST_LIMIT` | `30` | How many similar artists Last.fm returns per query before filtering. |
| `DISCOVER_TAG_OVERLAP` | `1` | Minimum number of shared Last.fm genre tags between a candidate and at least one source artist. Set to `0` to disable genre filtering entirely. |

### Web app / Docker only

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_USER` | `admin` | HTTP Basic Auth username. |
| `AUTH_PASS` | | HTTP Basic Auth password. **Required.** Empty string means every login attempt fails. |
| `SCHEDULE_MISSING` | | 5-field cron expression for automatic runs of `missing_popular_albums.py`. Empty = disabled. Example: `0 3 * * 0` (Sunday 3 AM). |
| `SCHEDULE_DISCOVER` | | 5-field cron expression for automatic runs of `discover_similar_artists.py`. Empty = disabled. |
| `DATA_DIR` | `/data` | Directory where the web app looks for reports. Set automatically in Docker. |
| `SPOTIFY_CLIENT_ID` | | Spotify app client ID. Enables Spotify embeds in the viewer. Requires a Spotify Premium account to register a dev app (as of Feb 2026). |
| `SPOTIFY_CLIENT_SECRET` | | Spotify app client secret. |
| `YOUTUBE_API_KEY` | | YouTube **Data API v3** key. Enables YouTube embeds in the viewer. Must have the Data API v3 enabled in Google Cloud Console (not the IFrame Player API). |
| `SLSKD_URL` | | Base URL of your SLSKD instance, e.g. `https://slskd.yourdomain.com`. Enables the SLSKD search button on every card. |
| `SLSKD_API_KEY` | | API key for SLSKD (set in `appsettings.yml` under `web.authentication.api_keys`). Preferred over username/password. |
| `SLSKD_USER` / `SLSKD_PASS` | | Fallback credentials if not using an API key. |

---

## CLI flags

Both scripts share these flags:

| Flag | Purpose |
|---|---|
| `--no-cache` | Ignore cached Last.fm data and re-fetch everything. Still writes fresh cache after the run. |
| `--limit-artists N` | Process only the first N artists alphabetically. Useful for testing without a full run. |
| `--workers N` | Number of concurrent Last.fm requests (1 to `MAX_WORKERS`). |

`missing_popular_albums.py` also accepts:

| Flag | Purpose |
|---|---|
| `--trace-artist "Name"` | Print Navidrome filesystem paths for every album by that artist, then exit. Requires Navidrome to be configured. |

---

## Web app dashboard

The Docker container runs a FastAPI app on port 8080. The dashboard shows job status (idle / running / succeeded / failed), lets you trigger runs on demand, and streams live log output to the browser.

The **Report Viewer** (`/`) shows both reports as paginated card grids with AJAX navigation (Prev / Next updates the cards without a full page reload, and browser back/forward works). Each card includes:

- **Streaming preview** — hover the album art to reveal service icons (Apple Music, Spotify, YouTube). Click one to open an embedded player directly inside the card. No credentials needed for Apple Music; Spotify and YouTube require `SPOTIFY_*` / `YOUTUBE_API_KEY` in your `.env`.
- **Copy** — copies the artist + album title to clipboard.
- **Dismiss** — hides the card permanently. Dismissed items are stored in `/data/dismissed.json` and are excluded from future script runs as well.

---

## API endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | None | Docker health check |
| GET | `/` | Basic | Combined report viewer (home) |
| GET | `/dashboard` | Basic | Script run dashboard |
| GET | `/report/{missing\|discover}` | Basic | Serve the generated HTML report |
| POST | `/run/{missing\|discover}` | Basic | Trigger a script run. Returns 409 if already running. |
| GET | `/status/{missing\|discover}` | Basic | JSON job status snapshot |
| GET | `/logs/{missing\|discover}` | Basic | SSE live log stream |
| GET | `/api/section/{section}` | Basic | AJAX partial — card grid + pager for one section |
| GET | `/api/stream-info` | Basic | Look up streaming embed URL for an album |
| POST | `/api/slskd-search` | Basic | Queue album search on a running SLSKD instance |
| POST | `/dismiss` | Basic | Add item to dismissed list |
| DELETE | `/dismiss` | Basic | Remove item from dismissed list |
| GET | `/dismissed` | Basic | Return full dismissed list |

---

## Security

- HTTP Basic Auth uses `secrets.compare_digest()` — timing-safe against brute-force enumeration.
- `AUTH_PASS` defaults to an empty string, which causes every login to fail until you set it. This is intentional.
- HTTPS is handled by Traefik. The app itself speaks plain HTTP on 8080.
- OpenAPI docs are disabled (`/docs` and `/redoc` return 404).
- The container runs as UID 1002 / GID 990 (non-root).
- The NAS music volume is mounted read-only.
- Scripts are launched via `subprocess` list form — no shell interpolation.

---

## Cache behavior

Last.fm responses are cached in `.cache/` as JSON files. Typical size is 17–36 MB for a large library. In Docker, the cache directory lives at `/data/.cache/` and survives container restarts via the volume mount.

`--no-cache` skips reading the cache but still writes fresh data at the end. Cache version is embedded in each file — a version mismatch causes a full re-fetch on the next run, which also overwrites the old cache.

---

## Troubleshooting

**`NAVIDROME_MUSIC_FOLDER not found` error on startup**

The name you set must match a Navidrome library name exactly (case-insensitive). The error message lists available names. Run `--trace-artist` to verify the connection is working, or leave `NAVIDROME_MUSIC_FOLDER` empty to scan all libraries.

**Report is empty or has far fewer entries than expected**

Check the log file — artists with no Last.fm data log `No albums found on Last.fm`. If the library scan returned zero artists, verify `MUSIC_ROOT` exists and contains audio files, or that Navidrome credentials are correct. Run with `--limit-artists 5` first to confirm the pipeline works end-to-end.

**Cache seems stale after a library change**

Run with `--no-cache` to force fresh Last.fm data. The cache doesn't auto-expire — it only updates when an artist is looked up and the cache misses.

**Auth not working in the web app**

Confirm `AUTH_PASS` is set in `Docker/music-reports/.env` and the container was restarted after the change. An empty `AUTH_PASS` rejects every login attempt by design.

**SSE log stream stops immediately or never connects**

The `X-Accel-Buffering: no` header is set to prevent Traefik and nginx from buffering the stream. If you have an intermediate proxy not honoring this header, disable response buffering in its config. Refreshing the dashboard mid-run replays the buffered log output (up to 2000 lines) before resuming live streaming.

---

## Further reading

- [docs/USERGUIDE.md](docs/USERGUIDE.md) — detailed setup, configuration reference, viewer features, and troubleshooting
- [docs/ROADMAP.md](docs/ROADMAP.md) — planned features and known behaviors
- [CHANGELOG.md](CHANGELOG.md) — version history
