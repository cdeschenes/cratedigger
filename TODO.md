# Cratedigger — TODO

## Features

- [x] **Full-screen report view** — Clicking a section title ("Discover Similar Artists", "Missing Popular Albums", "New & Trending") opens the full report as a standalone, scrollable page rather than the paginated cards in the viewer.

- [x] **Mobile support** — Make the site usable on mobile devices (responsive layout, touch-friendly cards, readable font sizes on small screens).

- [x] **Debug log viewer** — Add a "Debug Log" button to the top of the Help & Documentation page that tails the last 1000 lines of the app log. Syntax highlighting to make errors stand out (red for ERROR, yellow for WARNING, dimmed for DEBUG).

- [x] **Submit Request button** — Add a "Submit Request" button to the top of the Help & Documentation page. Opens a form (bug report or feature request) that auto-populates the GitHub Issues "new issue" URL for this repo so the user can submit without leaving the app.

- [x] **Version badge with update check** — Show the app version number in green when it matches the latest GitHub release tag, red when it's behind. Check the GitHub Releases API at page load and cache the result.

## Bugs / Fixes

- [x] **Broken cover art fallback** — Some Last.fm artwork URLs return 404 (e.g. `lastfm.freetls.fastly.net` CDN misses). When an image fails to load, try fallback sources: MusicBrainz Cover Art Archive, iTunes Search API, or a generic placeholder. Should be handled client-side (`onerror`) or server-side in `/api/stream-info`.

- [ ] **New & Trending — more results** — Currently returning ~39 releases after owned-album filtering. Investigate current source limits (Spotify 20, Last.fm top 15 artists × 1 album, Bandcamp 20) and explore additional sources to increase variety: MusicBrainz new releases, RateYourMusic RSS, Apple Music new releases, Pitchfork RSS, or increasing per-source limits where the API allows.

- [ ] **Discover similarity matching too loose** — `DISCOVER_SIMILARITY_MODE=tags` is still producing clearly unrelated pairings (e.g. Haunt → Beach Fossils). Revisit the Jaccard tag-overlap scoring: check which tags are being compared, whether the blocklist is catching noise tags, and whether the minimum overlap threshold is enforced correctly. Consider raising the default `DISCOVER_TAG_OVERLAP`, improving the tag blocklist, or displaying matched tags on the card so users can see why a match was made.

- [x] **Dismissed albums not persistent across container rebuilds** — Investigated: already working correctly. `dismissed.json` is written to `/data/dismissed.json` on the `cratedigger-data` named Docker volume. Data survives `docker compose pull && docker compose up -d`. Only `docker compose down -v` destroys it (that command explicitly removes volumes). Data loss on migration from the old `music-reports` bind-mount setup was a one-time event. Hardened `save_dismissed()` with try/except + logger.exception so write failures are logged instead of silently crashing.
