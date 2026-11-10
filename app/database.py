import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Create engine with connection pooling and timeouts
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=(settings.ENVIRONMENT == "development"),
    connect_args={
        "timeout": 10,  # Connection timeout in seconds
        "check_same_thread": False,  # For SQLite compatibility
    } if "sqlite" in settings.DATABASE_URL else {
        "timeout": 10,  # Connection timeout for PostgreSQL
        "command_timeout": 10,
    },
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


# Additive column migrations for databases created before Phase 1.
# create_all only creates missing tables — it never alters existing ones, so
# columns added to CanonicalBomHeader must be applied explicitly. Each entry is
# (table, column, column DDL). Safe to run repeatedly.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("canonical_bom_header", "production_version", "VARCHAR"),
    ("canonical_bom_header", "alternative_bom", "VARCHAR"),
    ("canonical_bom_header", "recipe_id", "VARCHAR"),
    ("canonical_bom_header", "production_line", "VARCHAR"),
    ("canonical_bom_header", "order_start_date", "DATETIME"),
    ("canonical_bom_header", "last_change_datetime", "DATETIME"),
    ("canonical_material", "grade", "VARCHAR"),
    ("canonical_bom_item", "withdrawn_quantity", "FLOAT"),
]


def _apply_column_migrations(sync_conn) -> None:
    """Add missing columns to existing tables. SQLite/Postgres compatible."""
    from sqlalchemy import inspect

    inspector = inspect(sync_conn)
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing:
            sync_conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            )


async def init_db() -> None:
    """Initialize database: create tables and seed default data.
    
    This function:
    1. Creates all tables if they don't exist
    2. Applies any pending column migrations
    3. Seeds default roles, pages, and users
    
    Raises:
        Exception: If database connection or initialization fails
    """
    try:
        logger.info("Creating database tables...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_apply_column_migrations)
        logger.info("Database tables created successfully")

        logger.info("Seeding default data...")
        # Import lazily to avoid a circular import (rbac_seed -> models -> this module).
        from app.rbac_seed import seed_rbac

        async with async_session() as session:
            await seed_rbac(session)
        logger.info("Default data seeded successfully")
        
    except Exception as e:
        logger.error("Database initialization failed: %s", str(e), exc_info=True)
        raise
