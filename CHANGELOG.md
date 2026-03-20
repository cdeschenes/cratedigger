# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
