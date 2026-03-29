"""
webapp/app.py — FastAPI application for the music-reports dashboard.

Routes:
  GET  /healthz                  — public healthcheck
  GET  /login, POST /login       — session auth
  GET  /logout                   — clear session
  GET  /                         — combined paginated report viewer (home)
  GET  /dashboard                — script run dashboard
  GET  /help                     — help / about page
  GET  /report/{job_id}          — serve generated HTML report
  POST /run/{job_id}             — trigger a script run
  GET  /status/{job_id}          — JSON job status
  GET  /logs/{job_id}            — SSE log stream
  GET  /section/{section}        — full scrollable view of one section (all items)
  GET  /api/section/{section}    — AJAX partial: card grid + pager for one section
  POST /api/trending/refresh     — force-refresh the trending cache
  POST /api/slskd-search         — queue a search on a running SLSKD instance
  POST /dismiss                  — add item to dismissed list
  DELETE /dismiss                — remove item from dismissed list
  GET  /dismissed                — return current dismissed list
"""
import json
import logging
import os
import secrets as _secrets_mod
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# ── In-memory app log buffer ──────────────────────────────────────────────────

_app_log_buffer: deque[str] = deque(maxlen=500)


class _DequeHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _app_log_buffer.append(self.format(record))
        except Exception:
            pass


_dh = _DequeHandler()
_dh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_dh)

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from webapp.auth import NotAuthenticatedException, check_credentials, require_auth
from webapp.runner import SCRIPTS_DIR, get_all_status, get_status, run_job, stream_logs
from webapp.scheduler import get_next_run, start_scheduler, stop_scheduler
from webapp.spotify import SPOTIFY_ENABLED, _get_spotify_token, _search_spotify
from webapp.discovery import DISCOVERY_FEEDS, get_discovery_results

__version__ = "1.2.1"

ITEMS_PER_PAGE = 4
SECTION_FULL_PER_PAGE = 100

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

_MISSING_LOG  = Path(os.environ.get("LOG_FILE",          "/data/missing_popular_albums.log"))
_DISCOVER_LOG = Path(os.environ.get("DISCOVER_LOG_FILE", "/data/discover_similar_artists.log"))

TEMPLATES_DIR = Path(__file__).parent / "templates"

REPORT_FILES: dict[str, str] = {
    "missing": "missing_popular_albums.html",
    "discover": "discover_similar_artists.html",
}

JSON_FILES: dict[str, str] = {
    "missing":  "missing_popular_albums.json",
    "discover": "discover_similar_artists.json",
    "trending": "discovery_results.json",
}

JOB_LABELS: dict[str, str] = {
    "missing":  "Missing Popular Albums",
    "discover": "Discover Similar Artists",
    "trending": "New & Trending",
}

RUNNABLE_JOBS = frozenset(JOB_LABELS)

DISMISSED_FILE = DATA_DIR / "dismissed.json"

# ── Streaming config ──────────────────────────────────────────────────────────

_YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

ENABLED_SERVICES = {
    "apple":   True,
    "spotify": SPOTIFY_ENABLED,
    "youtube": bool(_YOUTUBE_API_KEY),
}

_stream_cache: dict[tuple[str, str, str], str | None] = {}

# ── SLSKD config ──────────────────────────────────────────────────────────────

_SLSKD_URL     = os.environ.get("SLSKD_URL", "").rstrip("/")
_SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY", "")
_SLSKD_USER    = os.environ.get("SLSKD_USER", "")
_SLSKD_PASS    = os.environ.get("SLSKD_PASS", "")

SLSKD_ENABLED = bool(_SLSKD_URL and (_SLSKD_API_KEY or (_SLSKD_USER and _SLSKD_PASS)))

_slskd_token: dict = {}   # {"token": str, "expires_at": float} — user/pass auth only


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json_report(job_id: str) -> dict | None:
    """Load a JSON report file. Returns envelope dict or None if missing/corrupt."""
    path = DATA_DIR / JSON_FILES[job_id]
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # Backwards compat: old format was a bare list
    if isinstance(raw, list):
        return {"generated_at": None, "total_items": len(raw), "items": raw}
    if isinstance(raw, dict) and "items" in raw:
        return raw
    return None


def load_dismissed() -> dict[str, list[str]]:
    if not DISMISSED_FILE.exists():
        return {"missing": [], "discover": [], "trending": []}
    try:
        data = json.loads(DISMISSED_FILE.read_text(encoding="utf-8"))
        return {
            "missing": data.get("missing", []),
            "discover": data.get("discover", []),
            "trending": data.get("trending", []),
        }
    except Exception:
        return {"missing": [], "discover": [], "trending": []}


def save_dismissed(data: dict[str, list[str]]) -> None:
    try:
        DISMISSED_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Failed to write dismissed file: %s", DISMISSED_FILE)
        raise


def _apply_dismissed(items: list, section: str, dismissed: dict) -> tuple[list, int]:
    """Filter dismissed items from a list. Returns (filtered_list, dismissed_count)."""
    original = len(items)
    if section == "discover":
        dismissed_set = set(dismissed["discover"])
        filtered = [i for i in items if i.get("candidate_normalized") not in dismissed_set]
    elif section == "trending":
        dismissed_set = set(dismissed["trending"])
        filtered = [
            i for i in items
            if f"{i.get('artist_normalized')}|{i.get('album_normalized')}" not in dismissed_set
        ]
    else:
        dismissed_set = set(dismissed["missing"])
        filtered = [
            i for i in items
            if f"{i.get('artist_normalized')}|{i.get('album_normalized')}" not in dismissed_set
        ]
    return filtered, original - len(filtered)


def _datetimeformat(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%B %-d, %Y at %-I:%M %p UTC")
    except Exception:
        return value


# ── App setup ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="Cratedigger",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

_secret_key = os.environ.get("SECRET_KEY") or _secrets_mod.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_secret_key, https_only=False)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates.env.filters["datetimeformat"] = _datetimeformat


# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(NotAuthenticatedException)
async def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=303)


# ── Healthcheck (public) ──────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── Login / Logout ────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if check_credentials(username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid username or password."},
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ── Combined report viewer (home) ─────────────────────────────────────────────

@app.get("/")
async def viewer(
    request: Request,
    _: str = Depends(require_auth),
    d_page: int = 1,
    m_page: int = 1,
):
    discover_report = load_json_report("discover")
    missing_report  = load_json_report("missing")
    # Discovery: use persisted file for fast initial render (AJAX refreshes use live cache)
    discovery_report = load_json_report("trending") if DISCOVERY_FEEDS else None
    dismissed = load_dismissed()

    d_items_raw = discover_report["items"] if discover_report else None
    m_items_raw = missing_report["items"]  if missing_report  else None

    d_items_all, d_dismissed_count = _apply_dismissed(d_items_raw, "discover", dismissed) if d_items_raw is not None else (None, 0)
    m_items_all, m_dismissed_count = _apply_dismissed(m_items_raw, "missing",  dismissed) if m_items_raw is not None else (None, 0)

    d_total = len(d_items_all) if d_items_all is not None else 0
    m_total = len(m_items_all) if m_items_all is not None else 0

    d_pages = max(1, -(-d_total // ITEMS_PER_PAGE))
    m_pages = max(1, -(-m_total // ITEMS_PER_PAGE))

    d_page = max(1, min(d_page, d_pages))
    m_page = max(1, min(m_page, m_pages))

    d_start = (d_page - 1) * ITEMS_PER_PAGE
    m_start = (m_page - 1) * ITEMS_PER_PAGE

    # Build discovery sections from persisted file (or empty if not yet run)
    _disc_sections = {
        "new_from_artists":    [],
        "trending_near_taste": [],
        "genre_picks":         [],
    }
    _disc_generated_at: str | None = None
    _disc_total = 0
    if discovery_report:
        _disc_generated_at = discovery_report.get("generated_at")
        for key in _disc_sections:
            raw = discovery_report.get(key, [])
            filtered, _ = _apply_dismissed(raw, "trending", dismissed)
            _disc_sections[key] = filtered[:ITEMS_PER_PAGE]
        _disc_total = sum(len(v) for v in _disc_sections.values())

    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "version": __version__,
            "discover_items": d_items_all[d_start:d_start + ITEMS_PER_PAGE] if d_items_all is not None else None,
            "missing_items":  m_items_all[m_start:m_start + ITEMS_PER_PAGE] if m_items_all is not None else None,
            "d_page": d_page, "d_pages": d_pages, "d_total": d_total,
            "d_dismissed_count": d_dismissed_count,
            "d_generated_at": discover_report.get("generated_at") if discover_report else None,
            "m_page": m_page, "m_pages": m_pages, "m_total": m_total,
            "m_dismissed_count": m_dismissed_count,
            "m_generated_at": missing_report.get("generated_at") if missing_report else None,
            # Discovery sections (replaces flat trending_items)
            "discovery_sections":     _disc_sections,
            "discovery_generated_at": _disc_generated_at,
            "discovery_total":        _disc_total,
            "discovery_enabled":      bool(DISCOVERY_FEEDS),
            "enabled_services": ENABLED_SERVICES,
            "slskd_enabled": SLSKD_ENABLED,
        },
    )


# ── Full section page ─────────────────────────────────────────────────────────

@app.get("/section/{section}")
async def section_full_view(
    section: str,
    request: Request,
    _: str = Depends(require_auth),
    page: int = 1,
):
    if section not in ("discover", "missing", "trending"):
        raise HTTPException(status_code=404, detail="Unknown section")

    dismissed = load_dismissed()
    items_all: list | None = None
    generated_at: str | None = None
    dismissed_count = 0

    if section == "trending":
        disc = await get_discovery_results()
        # Flatten all sections into one list for the full-page view
        raw_items = (
            disc.get("new_from_artists", [])
            + disc.get("trending_near_taste", [])
            + disc.get("genre_picks", [])
        )
        generated_at = disc.get("generated_at")
        items_all, dismissed_count = _apply_dismissed(raw_items, "trending", dismissed)
    else:
        report = load_json_report(section)
        if report:
            generated_at = report.get("generated_at")
            raw_items = report.get("items", [])
            items_all, dismissed_count = _apply_dismissed(raw_items, section, dismissed)

    total = len(items_all) if items_all is not None else 0
    pages = max(1, -(-total // SECTION_FULL_PER_PAGE))
    page = max(1, min(page, pages))
    start = (page - 1) * SECTION_FULL_PER_PAGE
    page_items = (items_all or [])[start : start + SECTION_FULL_PER_PAGE]

    return templates.TemplateResponse(
        request,
        "section_full.html",
        {
            "version": __version__,
            "section": section,
            "section_title": JOB_LABELS[section],
            "items": page_items,
            "page": page,
            "pages": pages,
            "total": total,
            "dismissed_count": dismissed_count,
            "generated_at": generated_at,
            "enabled_services": ENABLED_SERVICES,
            "slskd_enabled": SLSKD_ENABLED,
            "show_pager": False,
        },
    )


# ── AJAX section fragment ─────────────────────────────────────────────────────

@app.get("/api/section/{section}")
async def section_fragment(
    section: str,
    request: Request,
    _: str = Depends(require_auth),
    page: int = 1,
):
    if section not in ("discover", "missing", "trending"):
        raise HTTPException(status_code=404, detail="Unknown section")

    dismissed = load_dismissed()
    items_all: list | None = None
    generated_at: str | None = None
    dismissed_count = 0

    if section == "trending":
        disc = await get_discovery_results()
        raw_items = (
            disc.get("new_from_artists", [])
            + disc.get("trending_near_taste", [])
            + disc.get("genre_picks", [])
        )
        generated_at = disc.get("generated_at")
        items_all, dismissed_count = _apply_dismissed(raw_items, "trending", dismissed)
    else:
        report = load_json_report(section)
        if report:
            generated_at = report.get("generated_at")
            raw_items = report.get("items", [])
            items_all, dismissed_count = _apply_dismissed(raw_items, section, dismissed)

    total = len(items_all) if items_all is not None else 0
    pages = max(1, -(-total // ITEMS_PER_PAGE))
    page = max(1, min(page, pages))
    start = (page - 1) * ITEMS_PER_PAGE
    page_items = items_all[start:start + ITEMS_PER_PAGE] if items_all is not None else []

    return templates.TemplateResponse(
        request,
        "_section_cards.html",
        {
            "section": section,
            "items": page_items,
            "page": page,
            "pages": pages,
            "total": total,
            "dismissed_count": dismissed_count,
            "generated_at": generated_at,
            "enabled_services": ENABLED_SERVICES,
            "slskd_enabled": SLSKD_ENABLED,
        },
    )


# ── Run dashboard ─────────────────────────────────────────────────────────────

@app.get("/dashboard")
async def dashboard(request: Request, _: str = Depends(require_auth)):
    jobs = get_all_status()
    reports = {job_id: (DATA_DIR / filename).exists() for job_id, filename in REPORT_FILES.items()}
    reports["trending"] = False  # no HTML report for trending
    json_data = {job_id: (DATA_DIR / filename).exists() for job_id, filename in JSON_FILES.items()}
    next_runs = {job_id: get_next_run(job_id) for job_id in REPORT_FILES}
    next_runs["trending"] = None
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "version": __version__,
            "jobs": jobs,
            "reports": reports,
            "json_data": json_data,
            "next_runs": next_runs,
            "labels": JOB_LABELS,
            "schedule_missing": os.environ.get("SCHEDULE_MISSING", ""),
            "schedule_discover": os.environ.get("SCHEDULE_DISCOVER", ""),
        },
    )


# ── Help page ─────────────────────────────────────────────────────────────────

@app.get("/help")
async def help_page(request: Request, _: str = Depends(require_auth)):
    return templates.TemplateResponse(
        request,
        "help.html",
        {"version": __version__},
    )


# ── Reports ───────────────────────────────────────────────────────────────────

@app.get("/report/{job_id}")
async def serve_report(job_id: str, _: str = Depends(require_auth)):
    if job_id not in REPORT_FILES:
        raise HTTPException(status_code=404, detail="Unknown report")
    path = DATA_DIR / REPORT_FILES[job_id]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not yet generated — run the script first")
    return FileResponse(path, media_type="text/html")


# ── Run trigger ───────────────────────────────────────────────────────────────

@app.post("/run/{job_id}")
async def trigger_run(job_id: str, _: str = Depends(require_auth)):
    if job_id not in RUNNABLE_JOBS:
        raise HTTPException(status_code=404, detail="Unknown job")
    try:
        await run_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {"status": "started", "job_id": job_id}


# ── Status ────────────────────────────────────────────────────────────────────

@app.get("/status/{job_id}")
async def job_status_endpoint(job_id: str, _: str = Depends(require_auth)):
    if job_id not in RUNNABLE_JOBS:
        raise HTTPException(status_code=404, detail="Unknown job")
    return get_status(job_id)


# ── SSE log stream ────────────────────────────────────────────────────────────

@app.get("/logs/{job_id}")
async def log_stream(job_id: str, _: str = Depends(require_auth)):
    if job_id not in RUNNABLE_JOBS:
        raise HTTPException(status_code=404, detail="Unknown job")
    return StreamingResponse(
        stream_logs(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Trending refresh ──────────────────────────────────────────────────────────

@app.post("/api/trending/refresh")
async def trending_refresh(_: str = Depends(require_auth)):
    await get_discovery_results(force=True)
    return {"status": "ok"}


# ── Streaming search helpers ──────────────────────────────────────────────────

async def _search_apple(artist: str, album: str) -> str | None:
    async with httpx.AsyncClient() as c:
        r = await c.get("https://itunes.apple.com/search",
                        params={"term": f"{artist} {album}", "entity": "album",
                                "limit": 5, "media": "music"},
                        timeout=5)
    if r.status_code != 200:
        logger.warning("Apple Music search failed: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    for result in r.json().get("results", []):
        if result.get("wrapperType") == "collection":
            return f"https://embed.music.apple.com/us/album/{result['collectionId']}"
    return None


async def _search_spotify(artist: str, album: str) -> str | None:
    token = await _get_spotify_token()
    if not token:
        return None
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.spotify.com/v1/search",
                        params={"q": f"album:{album} artist:{artist}",
                                "type": "album", "limit": 1},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=5)
    if r.status_code != 200:
        logger.warning("Spotify search failed: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    items = r.json().get("albums", {}).get("items", [])
    return f"https://open.spotify.com/embed/album/{items[0]['id']}" if items else None


async def _search_youtube(artist: str, album: str) -> str | None:
    if not _YOUTUBE_API_KEY:
        return None
    async with httpx.AsyncClient() as c:
        r = await c.get("https://www.googleapis.com/youtube/v3/search",
                        params={"q": f"{artist} {album} full album", "key": _YOUTUBE_API_KEY,
                                "part": "snippet", "type": "video", "maxResults": 1},
                        timeout=5)
    if r.status_code != 200:
        logger.warning("YouTube search failed: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    items = r.json().get("items", [])
    return f"https://www.youtube.com/embed/{items[0]['id']['videoId']}?rel=0" if items else None


@app.get("/api/stream-info")
async def stream_info(artist: str, album: str, service: str, _: str = Depends(require_auth)):
    if service not in ("apple", "spotify", "youtube"):
        return {"found": False, "embed_url": None}
    key = (artist.lower(), album.lower(), service)
    if key not in _stream_cache:
        if service == "apple":
            url = await _search_apple(artist, album)
        elif service == "spotify":
            url = await _search_spotify(artist, album)
        else:
            url = await _search_youtube(artist, album)
        _stream_cache[key] = url
    url = _stream_cache[key]
    return {"found": url is not None, "embed_url": url}


# ── SLSKD API ─────────────────────────────────────────────────────────────────

async def _get_slskd_token() -> str | None:
    """Obtain a JWT from SLSKD via username/password. Cached for 23 hours."""
    if not (_SLSKD_USER and _SLSKD_PASS):
        return None
    now = time.monotonic()
    if _slskd_token.get("token") and now < _slskd_token.get("expires_at", 0):
        return _slskd_token["token"]
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{_SLSKD_URL}/api/v0/session",
                         json={"username": _SLSKD_USER, "password": _SLSKD_PASS},
                         timeout=5)
    if r.status_code != 200:
        logger.warning("SLSKD login failed: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    token = r.json().get("token")
    _slskd_token.update(token=token, expires_at=now + 82800)  # cache 23 h
    return token


class SlskdSearchRequest(BaseModel):
    artist: str
    album: str


@app.post("/api/slskd-search")
async def slskd_search(body: SlskdSearchRequest, _: str = Depends(require_auth)):
    if not SLSKD_ENABLED:
        raise HTTPException(status_code=503, detail="SLSKD not configured")

    headers: dict[str, str] = {}
    if _SLSKD_API_KEY:
        headers["X-API-Key"] = _SLSKD_API_KEY
    else:
        token = await _get_slskd_token()
        if not token:
            raise HTTPException(status_code=503, detail="SLSKD authentication failed")
        headers["Authorization"] = f"Bearer {token}"

    search_text = f"{body.artist} {body.album}"
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{_SLSKD_URL}/api/v0/searches",
                         json={"searchText": search_text},
                         headers=headers,
                         timeout=5)
    if r.status_code not in (200, 201):
        logger.warning("SLSKD search failed: HTTP %s — %s", r.status_code, r.text[:200])
        raise HTTPException(status_code=502, detail="SLSKD search request failed")

    return {"queued": True, "search_text": search_text}


# ── Dismiss API ───────────────────────────────────────────────────────────────

class DismissRequest(BaseModel):
    type: str   # "missing" or "discover"
    key: str    # normalized dismiss key


@app.post("/dismiss")
async def dismiss_item(body: DismissRequest, _: str = Depends(require_auth)):
    if body.type not in ("missing", "discover", "trending"):
        raise HTTPException(status_code=400, detail="type must be 'missing', 'discover', or 'trending'")
    data = load_dismissed()
    if body.key not in data[body.type]:
        data[body.type].append(body.key)
        save_dismissed(data)
    return {"status": "ok"}


@app.delete("/dismiss")
async def undismiss_item(body: DismissRequest, _: str = Depends(require_auth)):
    if body.type not in ("missing", "discover", "trending"):
        raise HTTPException(status_code=400, detail="type must be 'missing', 'discover', or 'trending'")
    data = load_dismissed()
    try:
        data[body.type].remove(body.key)
        save_dismissed(data)
    except ValueError:
        pass
    return {"status": "ok"}


@app.get("/dismissed")
async def get_dismissed(_: str = Depends(require_auth)):
    return load_dismissed()


@app.get("/api/debug-log")
async def debug_log(_: str = Depends(require_auth)):
    lines: list[str] = []
    for log_path in (_MISSING_LOG, _DISCOVER_LOG):
        if log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
                lines.extend(text.splitlines()[-500:])
            except Exception:
                logger.exception("Could not read log file: %s", log_path)
    lines.extend(_app_log_buffer)
    return {"lines": lines[-1000:]}


@app.post("/api/clear-cache")
async def clear_cache(_: str = Depends(require_auth)):
    targets = [DATA_DIR / name for name in JSON_FILES.values()] + [
        SCRIPTS_DIR / ".cache" / "lastfm_top_albums.json",
        SCRIPTS_DIR / ".cache" / "similar_artists.json",
    ]
    deleted, errors = [], []
    for path in targets:
        try:
            path.unlink(missing_ok=True)
            deleted.append(path.name)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            errors.append(path.name)
    return {"deleted": deleted, "errors": errors}
