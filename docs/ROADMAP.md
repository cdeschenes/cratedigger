# TODO

---

## Feature: Discogs Marketplace Links

Add a "Buy on Discogs" button to each album card that links directly to the Discogs Marketplace listing for that specific release, pre-filtered by currency and country.

**Context:**
- Cards already generate a Discogs search URL (`item.discogs_url`) that points to the artist/release search page.
- The Marketplace URL format is: `https://www.discogs.com/sell/list?artist=...&release_title=...&currency=USD&ships_from=US`
- Two new optional env vars would control filtering:
  - `DISCOGS_CURRENCY` — e.g. `USD`, `CAD`, `EUR` (default: unset = any)
  - `DISCOGS_SHIPS_FROM` — e.g. `United States`, `Canada` (default: unset = any)
- No Discogs API key is required for marketplace search links — they're plain URLs.
- The button would sit alongside the existing Discogs link in `card-actions` in `_section_cards.html`.

---

## Feature: New & Trending Section

Add a third section to the viewer landing page that aggregates new and trending release feeds from curated record stores and labels.

**Context:**
- Would appear below the two existing sections (Discover Similar Artists, Missing Popular Albums) as a separate card grid.
- Data sourced from RSS/Atom feeds — no scripts need to run; the webapp fetches and caches them on demand.
- Proposed sources (all have RSS or discoverable feeds):
  - Discogs New Releases (user dashboard feed)
  - Turntable Lab
  - Bandcamp Discover
  - Fat Beats
  - Rough Trade
  - Erased Tapes
  - LUNA Music
  - Thrill Jockey Records
  - Lex Records
  - Wax Trax! Records
  - Further Records
  - secretambient.club
- Implementation sketch:
  - New `GET /api/trending` endpoint in `app.py` — fetches and merges RSS feeds, caches results for ~1 hour in memory to avoid hammering sources on every page load.
  - `feedparser` (or `httpx` + manual XML parse) for feed ingestion.
  - New `_section_trending.html` partial (or inline in `viewer.html`) for card rendering.
  - Cards would show: release title, artist, label, release date, cover art (if in feed), and a link to the store/source.
  - A settings UI or env var (e.g. `TRENDING_FEEDS`) could let you enable/disable specific sources.

---

## Known Behavior: Spotify Shows Preview-Only (30-sec Clip) Instead of Full Player

The Spotify embed currently plays a 30-second preview even though a Spotify Premium API key is in use.

**Context:**
- The Spotify embed (`open.spotify.com/embed/album/{id}`) respects the *listener's* Spotify login in the browser, not the API key used for searching.
- If the person viewing the page is logged into Spotify Premium in their browser, the embed plays the full album automatically. If not (or if using a private/incognito window), it falls back to 30-second previews.
- The `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` are used only for the Client Credentials OAuth flow to search for albums — they do not grant playback rights to the embed.
- **Resolution:** Log into Spotify in the browser before using the viewer. The embed will detect the active Spotify session and switch to full playback. No code change is required.
- If full playback still doesn't work after logging in, check that the embed URL includes `?utm_source=generator` — some Spotify embed configurations require this parameter to activate the full player UI.
