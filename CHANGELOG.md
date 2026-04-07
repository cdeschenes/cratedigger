# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.7] - 2026-04-07

### Fixed

- Streaming player buttons (Apple Music, Spotify, YouTube) were broken on the
  Report Viewer. The `{% with %}` blocks that include `_section_cards.html` for
  all three sections did not pass `slskd_mode`, causing a Jinja `UndefinedError`
  that corrupted the card HTML and broke all card interactivity.

## [1.2.6] - 2026-04-07

### Added

- SLSKD auto-download mode — set `SLSKD_MODE=download` to have the button
  automatically pick the best Soulseek peer and queue files instead of just
  queuing a search. Format is controlled by `SLSKD_FORMAT` (`flac` or `mp3`).
  For FLAC, `SLSKD_QUALITY_PREFER=24` tries 24-bit first and falls back to
  16-bit. For MP3, `SLSKD_MP3_BITRATE=320` requires 320 kbps, falling back to
  any MP3 if needed. `SLSKD_SEARCH_TIMEOUT` (default 30s) controls how long to
  wait for search results before selecting from partial results.

- In download mode, the SLSKD button label changes to "Download", the tooltip
  shows "Download via SLSKD", and the button shows "Searching…" while polling.
  A success toast displays the artist, album, format, and file count. Failure
  shows the reason from the server.

## [1.2.5] - 2026-04-07

### Fixed

- Juno records now show album artwork. The Juno RSS feed contains no cover
  images, but the product ID in each entry's URL can be used to construct the
  image URL directly (`imagescdn.juno.co.uk/full/CS{id}A-BIG.jpg`).

- ListenBrainz cards now attempt an iTunes artwork lookup when no image is
  available in the feed. Previously this fallback was accidentally disabled
  when the broken `data:,` placeholder was removed. Cards where iTunes finds
  nothing still show the "No Artwork" placeholder.

## [1.2.4] - 2026-04-07

### Fixed

- New & Trending cards with no album artwork now show a styled "No Artwork"
  placeholder instead of a broken image icon. Previously, cards from sources
  that don't provide cover art (such as ListenBrainz) rendered a blank broken
  image because the template used an empty data URI (`data:,`) as a fallback.

## [1.2.3] - 2026-04-07

### Fixed

- Discover Similar Artists no longer surfaces cross-genre false positives caused
  by decade tags. Era tags like `90s`, `80s`, `2000s`, and `retro` describe
  *when* an artist was active, not *what they sound like*. These are now ignored
  when computing Jaccard tag overlap, so a hip-hop and a gothic rock artist
  sharing only a decade tag no longer pass the genre filter.

## [1.2.2] - 2026-03-30

### Changed

- New & Trending known-artist scoring now checks your full Navidrome library in
  addition to Last.fm scrobble history. Any artist present in Navidrome receives
  `known_artist_score = 15`, qualifying their releases for the **New From Your
  Artists** section even if they don't appear in your Last.fm top artists.
  Previously only the top 50 scrobbled artists were checked, leaving most of the
  library unmatched. Requires Navidrome credentials; has no effect if only
  `LASTFM_USERNAME` is set.

- Reason text for library-matched cards is now "New release from an artist in
  your collection" rather than "New release from an artist in your listening
  history" (the latter is reserved for tail-end scrobble matches above rank 100).

### Fixed

- New & Trending home page preview capped at 4 items per subsection, matching
  the other report sections. The full results remain available via the section
  header link.

## [1.2.1] - 2026-03-24

### Added

- Full-page section pagination — `/section/{section}` now shows 100 items per page with
  URL-based Prev/Next navigation (`?page=N`). Previously the page loaded all items at once,
  which was slow at 1000+ cards. Page size controlled by `SECTION_FULL_PER_PAGE = 100` in
  `webapp/app.py`.
- Submit Request button on the Help page — opens a modal that pre-populates a GitHub Issues
  URL with the appropriate label, title, and body so bug reports and feature requests can be
  filed without leaving the app.
- Version badge update check — the navbar version number turns green when running the latest
  GitHub release and red (with a link) when behind. Checks
  `https://api.github.com/repos/cdeschenes/cratedigger/releases/latest` at page load; result
  is cached in `localStorage` under `cd_latest_ver` for one hour.
- Cover art fallback — when a Last.fm image URL fails to load, the card's `<img>` fires an
  `onerror` handler (`imgFallback()`) that tries the iTunes Search API as a no-credentials
  fallback. If that also fails, a "No Artwork" placeholder replaces the image. Implemented
  client-side in `base.html`.
- Debug Log viewer on the Help page — a "Debug Log" button opens a collapsible panel showing
  the last 1000 lines of application logs. ERROR lines are highlighted red, WARNING yellow,
  DEBUG dim. Sources: `/data/missing_popular_albums.log`,
  `/data/discover_similar_artists.log`, and an in-memory ring buffer (500 lines) attached to
  the root logger via `_DequeHandler`. Fetched from `GET /api/debug-log` (auth required). A
  Refresh button re-fetches without closing the panel.
- `DISCOVER_TAG_TOP_N` env var (default: `5`, range 1–10) — limits tag-mode Jaccard scoring
  to only the top N highest-weight Last.fm tags per artist. Tags are returned by Last.fm in
  descending weight order, so this cuts low-weight broad tags (e.g. "rock", "indie") that
  create false matches from contaminating the score.
- `DISCOVER_MIN_JACCARD` env var (default: `0.1`) — minimum Jaccard score for a candidate to
  survive in `tags` mode. Replaces the previous `> 0.0` threshold that passed any candidate
  sharing a single tag.
- Expanded tag blocklist (`IGNORED_TAGS`) — added platform/spam tags (`spotify`,
  `heard on pandora`, `pandora`, `youtube`, `under 2000 listeners`, `not on spotify`) and
  noise words (`music`, `good`, `cool`, `beautiful`, `nice`) to the existing blocklist.
- Matched tags on Discover cards — in `tags` mode, cards show up to 5 tag chips below the
  "Similar to:" line, indicating which genre tags the candidate shared with the source
  artists. Tags are stored in a new `matched_tags` field on `SimilarSuggestion` and included
  in `discover_similar_artists.json`.

### Fixed

- `save_dismissed()` in `webapp/app.py` now wraps file writes in try/except with
  `logger.exception()`, so write failures are logged rather than silently crashing the
  request.

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
