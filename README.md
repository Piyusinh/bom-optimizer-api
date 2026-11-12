# BOM Optimizer API

Enterprise AI Decision Platform for Manufacturing — FastAPI backend (Phase 0 + Production Persona).

## Requirements
- **Python 3.11 or 3.12** (the Docker image uses 3.11). Newer Python may need unpinned package versions.
- The UI lives in a separate repo: `bom-optimizer-ui` (runs on http://localhost:5173 and proxies `/api` to this API).

## Quick start (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt        # only needed to run the tests
copy .env.example .env                     # then edit if needed (SQLite works out of the box)
python run.py                              # http://127.0.0.1:8001  (docs at /docs)
```

Linux / macOS: `source .venv/bin/activate` and `cp .env.example .env`.

The database is SQLite (`bom_platform.db`, created on first start, gitignored). Roles, pages and demo users are seeded
automatically on startup (see the demo accounts on the UI login page).

Check it is up: `GET http://127.0.0.1:8001/health` returns `{"status":"healthy", ...}`.

## Tests
```powershell
pytest -q
```

## Notes
- `.env` is never committed. `.env.example` lists every setting. Leave `OPENAI_API_KEY` blank to use the rule-based explainer.
- The API can also serve a built UI from `api/ui` (copy the UI's `dist/` there). That folder is not in git; if it is absent the API still starts and you run the UI separately.
- Port: `run.py` uses 8001 (override with `PORT`). The Docker image listens on 8000.

## Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/analyze-bom` | Run decision analysis on a material |
| GET | `/api/decisions` | List all computed decisions |
| GET | `/api/decisions/{id}` | Get full decision detail with trace |
| POST | `/api/explain` | Generate explanation for a candidate |
| POST | `/api/data/ingest-local` | Ingest all Excel files from docs folder |
| POST | `/api/data/upload/{type}` | Upload Excel file for ingestion |
| GET | `/api/data/stats` | Dashboard statistics |
| GET | `/api/data/materials` | List available materials |

## Architecture

```
app/
├── main.py              # FastAPI app, lifespan, CORS
├── config.py            # Settings from .env
├── database.py          # SQLAlchemy async engine + session
├── models.py            # ORM models (canonical BOM, decisions, audit)
├── schemas.py           # Pydantic request/response contracts
├── decision_core/       # The Decision Core
│   ├── engine.py        # Orchestrator (explosion → constraints → scoring → ranking)
│   ├── explosion_engine.py   # BOM explosion: material → candidate paths
│   ├── constraint_engine.py  # Hard constraint validation
│   └── scoring_engine.py     # Weighted multi-criteria scoring
├── connectors/          # MCP Runtime (adapters)
│   └── excel_connector.py    # Mock connector: Excel → canonical model
└── routes/              # API routes
    ├── analyze.py       # POST /api/analyze-bom
    ├── decisions.py     # GET /api/decisions
    ├── explain.py       # POST /api/explain
    └── data.py          # Data ingestion & stats
```

## Phase 0 Flow

1. **Ingest** Excel data → canonical BOM model (SQLite)
2. **Analyze** → Explosion → Constraint Validation → Scoring → Ranking
3. **Explain** → Rule-based (or Azure OpenAI if configured)

## Environment Variables

See `.env` file. Azure OpenAI is optional for Phase 0 — the explain endpoint
falls back to rule-based explanations.
