# Cratedigger — TODO

## Bugs / Fixes

- [ ] **New & Trending — more results** — Currently returning ~39 releases after owned-album filtering. Investigate current source limits (Spotify 20, Last.fm top 15 artists × 1 album, Bandcamp 20) and explore additional sources to increase variety: MusicBrainz new releases, RateYourMusic RSS, Apple Music new releases, Pitchfork RSS, or increasing per-source limits where the API allows.

- [ ] **Discover similarity matching too loose** — `DISCOVER_SIMILARITY_MODE=tags` is still producing clearly unrelated pairings (e.g. Haunt → Beach Fossils). Revisit the Jaccard tag-overlap scoring: check which tags are being compared, whether the blocklist is catching noise tags, and whether the minimum overlap threshold is enforced correctly. Consider raising the default `DISCOVER_TAG_OVERLAP`, improving the tag blocklist, or displaying matched tags on the card so users can see why a match was made.
