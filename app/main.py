"""Enterprise AI Decision Platform — FastAPI Application."""
import logging
import os
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.config import get_settings
from app.database import init_db
from app.routes import (
    admin,
    agent,
    analyze,
    auth,
    data,
    decisions,
    explain,
    manufacturing,
    production,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Absolute paths to the built UI files so FileResponse never depends on cwd
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(BASE_DIR, "ui")

# Global flag to track if database is initialized
_db_initialized = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events.
    
    Handles graceful database initialization with error recovery.
    If database initialization fails, the app continues running but
    database-dependent endpoints will return appropriate errors.
    """
    global _db_initialized
    
    # Startup: attempt to initialize database
    try:
        logger.info("Initializing database...")
        await init_db()
        _db_initialized = True
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.warning(
            "Database initialization failed (app will continue with limited functionality): %s",
            str(e),
        )
        _db_initialized = False
    
    yield
    
    # Shutdown: cleanup if needed
    logger.info("Application shutting down")


app = FastAPI(
    title="BOM Optimizer API",
    description="Enterprise AI Decision Platform for Manufacturing — Phase 0",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def _add_request_id(request: Request, call_next):
    """Attach a request/correlation ID to every request and response."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# Global exception handlers — return client-friendly JSON with `detail`
import logging as _logging
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(request, exc: RequestValidationError):
    errors = [
        f"{'.'.join(str(loc) for loc in err.get('loc', []))}: {err.get('msg', '')}"
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": "Validation failed", "errors": errors},
    )


@app.exception_handler(ValidationError)
async def _pydantic_validation_handler(request, exc: ValidationError):
    errors = [
        f"{'.'.join(str(loc) for loc in err.get('loc', []))}: {err.get('msg', '')}"
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": "Validation failed", "errors": errors},
    )


@app.exception_handler(IntegrityError)
async def _integrity_error_handler(request, exc: IntegrityError):
    _logging.error("IntegrityError: %s", exc)
    return JSONResponse(
        status_code=409,
        content={"detail": "A record with this value already exists."},
    )


@app.exception_handler(SQLAlchemyError)
async def _sqlalchemy_error_handler(request, exc: SQLAlchemyError):
    _logging.error("SQLAlchemyError: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Database error. Please try again later."},
    )


@app.exception_handler(Exception)
async def _catch_all_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", None)
    _logging.error("Unhandled exception [request_id=%s]: %s", request_id, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected error occurred. Please try again later.",
            "request_id": request_id,
        },
    )


# CORS
origins = [o.strip() for o in settings.CORS_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(analyze.router)
app.include_router(decisions.router)
app.include_router(explain.router)
app.include_router(data.router)
app.include_router(manufacturing.router)
app.include_router(production.router)
app.include_router(agent.router)


def is_db_ready() -> bool:
    """Check if database is initialized and ready."""
    return _db_initialized


@app.get("/health")
async def health():
    """Health check endpoint.
    
    Returns 200 if the app is running, regardless of database status.
    Includes database readiness status in response for monitoring.
    """
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "database_ready": is_db_ready(),
    }


# Serve the built JS/CSS assets from the Vite output
# The built UI is not stored in git, so on a fresh clone this folder does not exist.
# Mount it only when present; the API must still start (run the UI with `npm run dev`).
if os.path.isdir(os.path.join(UI_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(UI_DIR, "assets")), name="assets")


def _index_response() -> FileResponse:
    if not os.path.isfile(os.path.join(UI_DIR, "index.html")):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail="UI bundle not found in api/ui. Run the UI separately (cd ui && npm run dev) or copy ui/dist to api/ui.",
        )
    # index.html references content-hashed JS/CSS bundles (e.g. index-XXXX.js).
    # Every rebuild produces new hashes and deletes the old files, so index.html
    # must NEVER be cached by the browser — otherwise a stale cached HTML page
    # will request bundles that no longer exist, producing a blank/broken page
    # until the user hard-refreshes.
    return FileResponse(
        os.path.join(UI_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/vite.svg", include_in_schema=False)
async def vite_svg():
    path = os.path.join(UI_DIR, "vite.svg")
    if not os.path.isfile(path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)


@app.get("/", include_in_schema=False)
async def root_index():
    return _index_response()


@app.get("/{full_path:path}", include_in_schema=False)
async def catch_all(request: Request, full_path: str):
    # Unknown API calls get a real 404 JSON; everything else falls through
    # to the SPA so React Router can handle client-side navigation.
    if full_path.startswith("api/"):
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Not found",
                "request_id": getattr(request.state, "request_id", None),
            },
        )
    return _index_response()
