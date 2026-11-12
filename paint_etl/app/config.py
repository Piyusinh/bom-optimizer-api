"""Settings. Override with environment variables if needed."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Local PostgreSQL started by docker-compose.yml (port 5433, local dev only)
DSN = os.environ.get("BOM_DSN", "postgresql://bom:bom@127.0.0.1:5433/bom_paint")

# The "shared path": drop files in INBOX (or upload from the UI, which does the same).
# Flow:  inbox -> processing -> processed   (or failed)
DATA_DIR = Path(os.environ.get("BOM_DATA_DIR", ROOT / "data"))
INBOX = DATA_DIR / "inbox"
PROCESSING = DATA_DIR / "processing"
PROCESSED = DATA_DIR / "processed"
FAILED = DATA_DIR / "failed"

POLL_SECONDS = 3          # how often the worker looks at the inbox
FILE_SETTLE_SECONDS = 2   # ignore files still being copied (modified < N s ago)
COPY_CHUNK_PROGRESS = 50_000
MAX_REJECTS_STORED = 1000

SCHEMA_FILE = ROOT / "db" / "schema.sql"
STATIC_DIR = ROOT / "app" / "static"


def ensure_dirs() -> None:
    for d in (INBOX, PROCESSING, PROCESSED, FAILED):
        d.mkdir(parents=True, exist_ok=True)
