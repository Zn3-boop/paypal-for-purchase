import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# Force UTF-8 encoding
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
os.environ.setdefault('PYTHONUTF8', '1')

if hasattr(sys.stdout, 'reconfigure') and (sys.stdout.encoding or '').lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure') and (sys.stderr.encoding or '').lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# .env is loaded centrally by app/core/config.py (pydantic-settings Settings with
# env_file pointing to apps/server/.env); no need to call load_dotenv again here.

# Web demo (apps/web) focus: import only the 6 routers used by the web demo.
# Desktop routers (automation / voice / desktop / provider / browser / agent …)
# are intentionally not imported; their files no longer expose routes.
from app.api import health, payment, stt, tools
from app.utils.response_cleaner import clean_fastapi_response_body


# ========== Lifespan replaces the deprecated on_event("startup") ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from app.services.rag.store import DualIndexStore
        print("RAG store available")
    except Exception as e:
        print(f"Warning: RAG store init skipped: {e}")
    yield


app = FastAPI(
    title="Channel3 x PayPal Agentic Commerce API",
    version="0.1.0",
    lifespan=lifespan,
)


# ========== CORS: supports the CORS_ALLOW_ALL switch ==========
# Deployment-critical config lives in app/core/config.py (pydantic-settings), parsed once at startup.
from app.core.config import get_settings

_settings = get_settings(cache=True)
_allowed_origins = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins if not _settings.cors_allow_all else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== Global middleware: UTF-8 + exception safety ==========
@app.middleware("http")
async def ensure_utf8(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as exc:
        # Never leak internal error details to clients; return a generic message.
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Please try again later or contact the administrator."},
            media_type='application/json; charset=utf-8',
        )

    content_type = response.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
    elif 'charset=' not in content_type and content_type.startswith('text/'):
        response.headers['Content-Type'] = f'{content_type}; charset=utf-8'
    return response


# ========== Global middleware: response cleaning ==========
@app.middleware("http")
async def clean_response_middleware(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get('Content-Type', '')

    if 'application/json' not in content_type:
        return response

    body = b''
    async for chunk in response.body_iterator:
        body += chunk

    cleaned_body = clean_fastapi_response_body(body)
    headers = dict(response.headers)
    headers.pop('content-length', None)

    import json
    return JSONResponse(
        content=json.loads(cleaned_body.decode('utf-8')),
        status_code=response.status_code,
        headers=headers,
        media_type='application/json; charset=utf-8',
    )

# Simple route smoke tests: when the built web demo is present (apps/server/static),
# the domain root serves the SPA index.html (single-domain deployment); otherwise it
# degrades to a plain JSON liveness payload (local dev without a build).
_DIST_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/", include_in_schema=False)
async def root():
    index_html = _DIST_DIR / "index.html"
    if _DIST_DIR.is_dir() and index_html.is_file():
        return FileResponse(index_html)
    return {"status": "ok", "name": "Channel3 x PayPal Agentic Commerce API"}

@app.get("/health")
async def Health_check():
    return {"status": "healthy"}

# ========== Web demo routes (business routers under /api/v1; / and /health are liveness paths, no version) ==========
API_V1_PREFIX = "/api/v1"
app.include_router(health.router)
app.include_router(stt.router, prefix=API_V1_PREFIX)
app.include_router(tools.router, prefix=API_V1_PREFIX)
app.include_router(payment.router, prefix=API_V1_PREFIX)


# ========== Static SPA hosting (apps/web built into apps/server/static) ==========
# The web demo builds to apps/web/dist, which is copied into apps/server/static and
# shipped in the image via COPY . . (see apps/server/.dockerignore — static/ is NOT
# excluded). This lets SnapDeploy serve frontend + backend on ONE domain with no CORS.
# The catch-all is registered LAST, after every API router, so /api/v1/*, /health,
# /docs, /openapi.json etc. keep matching their real routes first.

# (_DIST_DIR is defined above next to the root route.)
_SPA_ASSET_EXTENSIONS = {
    ".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".map", ".json", ".txt",
}

# _API_PREFIXES: paths that must never fall back to index.html even if unmatched by a
# specific router (avoids SPA swallowing unknown API calls into a 200 index page).
_API_PREFIXES = ("api/", "api", "docs", "redoc", "openapi.json", "health")


def _spa_asset_path(rel: str) -> Path | None:
    """Resolve a request path under the static dir; None if traversal/unsafe."""
    if not rel or rel.startswith((".", "..")) or ".." in rel.split("/"):
        return None
    candidate = (_DIST_DIR / rel).resolve()
    try:
        candidate.relative_to(_DIST_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    # Never hijack API / liveness / OpenAPI paths (defensive; specific routes above
    # already match first, this guards unknown sub-paths like /api/v1/nonexistent).
    first_seg = full_path.split("/", 1)[0].lower()
    if first_seg in _API_PREFIXES or full_path.lower().startswith(_API_PREFIXES):
        raise HTTPException(status_code=404, detail="Not found")

    if not _DIST_DIR.is_dir():
        raise HTTPException(status_code=404, detail="Not found")

    # Existing static asset (e.g. /assets/index-*.js) → serve the file.
    asset = _spa_asset_path(full_path)
    if asset is not None:
        return FileResponse(asset)

    # Unknown deep link (e.g. /checkout) → serve index.html so the SPA router
    # (React state) renders the right view; browser refresh must not 404.
    ext = Path(full_path).suffix.lower()
    if ext and ext not in _SPA_ASSET_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")

    index_html = _DIST_DIR / "index.html"
    if index_html.is_file():
        return FileResponse(index_html)
    raise HTTPException(status_code=404, detail="Not found")