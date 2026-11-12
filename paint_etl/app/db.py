"""Database helpers."""
import time
import psycopg
from psycopg.rows import dict_row

from . import config


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(config.DSN, autocommit=autocommit, row_factory=dict_row)


def wait_for_db(seconds: int = 60) -> None:
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            with connect(autocommit=True) as c:
                c.execute("SELECT 1")
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"Database not reachable at {config.DSN}: {last}")


def apply_schema() -> None:
    """Create all tables (idempotent)."""
    sql = config.SCHEMA_FILE.read_text(encoding="utf-8")
    with connect(autocommit=True) as c:
        c.execute(sql)


def query(sql: str, params=None) -> list[dict]:
    with connect(autocommit=True) as c:
        return c.execute(sql, params).fetchall()
