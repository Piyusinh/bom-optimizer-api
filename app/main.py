"""Enterprise AI Decision Platform — FastAPI Application."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import get_settings
from app.database import init_db
from app.routes import analyze, decisions, explain, data, auth

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables
    await init_db()
    yield
    # Shutdown: cleanup if needed


app = FastAPI(
    title="BOM Optimizer API",
    description="Enterprise AI Decision Platform for Manufacturing — Phase 0",
    version="0.1.0",
    lifespan=lifespan,
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
app.include_router(analyze.router)
app.include_router(decisions.router)
app.include_router(explain.router)
app.include_router(data.router)


@app.get("/health")
async def health():
    return {"status": "healthy", "environment": settings.ENVIRONMENT}


@app.get("/")
async def root():
    return {
        "name": "BOM Optimizer API",
        "version": "0.1.0",
        "phase": "Phase 0 — Local Development",
        "docs": "/docs",
    }
