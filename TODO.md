# Cratedigger — TODO

## Bugs / Fixes

- [ ] **New & Trending — more results** — Currently returning ~39 releases after owned-album filtering. Investigate current source limits (Spotify 20, Last.fm top 15 artists × 1 album, Bandcamp 20) and explore additional sources to increase variety: MusicBrainz new releases, RateYourMusic RSS, Apple Music new releases, Pitchfork RSS, or increasing per-source limits where the API allows.

- [x] **Discover similarity matching too loose** — v1.2.1 shipped several improvements: `DISCOVER_TAG_TOP_N` (top-N tags only, default 5), `DISCOVER_MIN_JACCARD` (minimum threshold, default 0.1), an expanded noise/spam tag blocklist, and matched tag chips on Discover cards. v1.2.3 added era/decade tags (`90s`, `80s`, `2000s`, etc.) to `IGNORED_TAGS` to prevent cross-genre false positives where artists shared only a decade tag.

- [ ] RSS feed input in app for New & Trending. I want a way to add RSS feeds to the web app and it parse the RSS and add releases to the New & Trending section. 

Syndication feed: selfdestroyer's Fresh Releases:
Example RSS: https://listenbrainz.org/syndication-feed/user/selfdestroyer/fresh-releases


