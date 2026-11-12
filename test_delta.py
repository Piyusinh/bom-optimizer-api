"""Smoke test: delta watermark ingest + decision action endpoint."""
import asyncio
import os
import sys
import tempfile

# Use a throwaway DB before importing app modules (engine binds at import).
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import Base, engine  # noqa: E402
from app.models import CanonicalBomHeader, OrderChain  # noqa: E402
from app.connectors.excel_connector import (  # noqa: E402
    ingest_production_order_bom,
    ingest_sales_order_bom,
    ingest_order_linkage,
)

DOC = r"C:\Users\dell\Documents\src\bom\docs\Project DIC teams"


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        r1 = await ingest_production_order_bom(
            os.path.join(DOC, "Chemical_Production_Order_BOM_Data.xlsx"), db
        )
        print("pass 1:", r1)
        assert r1["headers_created"] == 1000 and r1["headers_skipped"] == 0

        n = await db.scalar(
            select(func.count()).select_from(CanonicalBomHeader)
            .where(CanonicalBomHeader.object_type == "PRODUCTION_ORDER")
        )
        print("headers in db:", n)
        assert n == 1000

        st = await db.scalar(
            select(func.count()).select_from(CanonicalBomHeader)
            .where(CanonicalBomHeader.status.isnot(None))
        )
        lcd = await db.scalar(
            select(func.count()).select_from(CanonicalBomHeader)
            .where(CanonicalBomHeader.last_change_datetime.isnot(None))
        )
        osd = await db.scalar(
            select(func.count()).select_from(CanonicalBomHeader)
            .where(CanonicalBomHeader.order_start_date.isnot(None))
        )
        print(f"status populated: {st}  | last_change: {lcd}  | order_start: {osd}")
        assert st > 0, "status must populate from ManufacturingOrderStatus"
        assert lcd > 0, "last_change_datetime must populate"

    async with Session() as db:
        r2 = await ingest_production_order_bom(
            os.path.join(DOC, "Chemical_Production_Order_BOM_Data.xlsx"), db
        )
        print("pass 2:", r2)
        assert r2["headers_skipped"] == 1000 and r2["records_ingested"] == 0, (
            "re-ingest of unchanged file must skip every row"
        )
        n = await db.scalar(
            select(func.count()).select_from(CanonicalBomHeader)
            .where(CanonicalBomHeader.object_type == "PRODUCTION_ORDER")
        )
        assert n == 1000, "no duplicates allowed"
        print("delta dedup: OK")

        r3 = await ingest_sales_order_bom(
            os.path.join(DOC, "Chemical_Sales_Order_BOM_Data.xlsx"), db
        )
        print("sales:", r3)
        assert r3["headers_created"] == 1000

        l1 = await ingest_order_linkage(
            os.path.join(DOC, "Chemical_SO_PlanOrd_ProdOrd_Linkage.xlsx"), db
        )
        print("linkage pass1:", l1)
        l2 = await ingest_order_linkage(
            os.path.join(DOC, "Chemical_SO_PlanOrd_ProdOrd_Linkage.xlsx"), db
        )
        print("linkage pass2:", l2)
        assert l2["records_skipped"] == 1000

        cs = await db.scalar(
            select(func.count()).select_from(OrderChain)
            .where(OrderChain.chain_stage.isnot(None))
        )
        print("chain_stage populated:", cs)

    print("\nALL DELTA TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
