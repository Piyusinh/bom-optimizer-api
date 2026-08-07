# BOM Optimizer API — Phase 0

Enterprise AI Decision Platform for Manufacturing — FastAPI Backend

## Quick Start

```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Run the server (auto-creates SQLite DB)
python run.py
```

API available at **http://localhost:8000**  
Swagger docs at **http://localhost:8000/docs**

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
