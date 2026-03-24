# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - 2026-03-24

### Added

- New & Trending section — third section in the Report Viewer pulling new releases from
  Spotify (new-releases API), Last.fm (`chart.getTopArtists` + `artist.getTopAlbums`), and
  Bandcamp Daily RSS. Controlled by `TRENDING_FEEDS` env var (comma-separated; default:
  `spotify,lastfm,bandcamp`). Results are merged by interleaving sources for variety and
  deduplicated on normalized artist+album key.
- Owned album filtering for trending — when Navidrome credentials are configured, albums
  already in the library are excluded from New & Trending results. The library is fetched via
  `getAlbumList2` and cached for one hour alongside the trending data.
- Trending job on Run Dashboard — New & Trending has its own job card (status badge, Run Now,
  live log, last run time). Refreshing regenerates and re-filters the trending list.
- Run All button — triggers all three jobs (missing, discover, trending) at once from the
  dashboard. Disabled while any job is running.
- Full-page section view (`/section/{section}`) — clicking a section title in the Report
  Viewer opens a standalone scrollable page with all items and no pagination. Dismiss buttons
  work the same way. A back link returns to the main viewer.
- Mobile responsive layout — navigation bar adapts at 640px and 480px breakpoints (version
  label hidden, GitHub icon hidden on smallest screens). Dashboard cards stack to full width
  below 480px. Album card action buttons have larger tap targets on mobile.
- Pre-built Docker image via GitHub Actions — image published to
  `ghcr.io/cdeschenes/cratedigger:latest` on every push to `beta`. Users run
  `docker compose pull && docker compose up -d` instead of building locally.

### Fixed

- Bandcamp Daily parsing — Bandcamp changed their RSS title format in 2025 from
  `Artist — Title` (em-dash) to `Artist, "Title"` using Unicode curly quotes (U+201C/U+201D).
  Added `_BC_COMMA_QUOTE_RE` as the primary pattern; legacy em-dash pattern kept as fallback.

## [1.1.0] - 2026-03-19

### Added

- **`DISCOVER_SIMILARITY_MODE`** env var — `tags` mode re-scores similar artist candidates
  using Jaccard genre-tag similarity instead of Last.fm's shared-listener score, and drops
  zero-overlap matches to eliminate cross-genre mismatches (e.g. ambient matched with hip-hop)
- Standalone HTML reports (`missing_popular_albums.html`, `discover_similar_artists.html`)
  now have full card UI parity with the webapp viewer: hover-to-reveal streaming icons
  (Apple Music, Spotify, YouTube), embedded player overlay, two-row action bar with
  icon-only external links plus Copy and SLSKD buttons, and toast notifications
- Help page "Card Reference" section with a live-rendered card anatomy diagram, streaming
  service icon guide with brand colors, external link descriptions, and action button guide
- Dashboard "Last run" timestamps now format to a human-readable locale string on page load
  (previously displayed as raw ISO 8601)

### Changed

- Project rebranded from "Music Reports" to **Cratedigger**
- In `tags` mode, similar artist candidates are re-scored using weighted Jaccard similarity
  instead of the raw Last.fm `match` value; the Jaccard score becomes the `similarity_score`

## [1.0.0] - 2026-03-19

### Added

- Web dashboard with live SSE log streaming and per-job status badges
- Combined report viewer with AJAX pagination — Prev/Next updates cards without a full page reload; browser back/forward navigation preserved
- Streaming preview player on each card — hover album art to reveal service icons (Apple Music, Spotify, YouTube); click to open an embedded player inside the card; × to close
- Apple Music previews require no credentials (iTunes Search API)
- Spotify previews via Client Credentials flow (`SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`)
- YouTube previews via Data API v3 (`YOUTUBE_API_KEY`)
- Dismiss / blacklist — ✕ button per card hides it permanently; dismissals persisted in `/data/dismissed.json` and applied to future script runs
- SLSKD integration — SLSKD button on each card queues a search directly in a running SLSKD instance (`SLSKD_URL` + `SLSKD_API_KEY`)
- Discover Similar Artists script with genre tag overlap filtering (`DISCOVER_TAG_OVERLAP`)
- Release year on album cards (extracted from Last.fm `wiki.published` field)
- Session-based login (HTTP Basic) with configurable `AUTH_USER` / `AUTH_PASS`
- Cron scheduling for both scripts (`SCHEDULE_MISSING`, `SCHEDULE_DISCOVER`) via APScheduler
- Docker deployment via named volume for persistent data; Traefik reverse proxy integration
- `--trace-artist` diagnostic flag to print Navidrome filesystem paths for a given artist
- `--no-cache` flag to force fresh Last.fm data without deleting the cache file
