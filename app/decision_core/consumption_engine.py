"""Consumption Engine — what was ACTUALLY consumed, not just what was planned.

RTM 'Algorithm Logic' item 4:

    "Sum up the quantities in the original recipe and all the goods issue
     against the process order (movement type 261) get the final quantities"

Planned quantity lives in the BOM/reservation (CanonicalBomItem.quantity).
Actual issued quantity is the client's own `WithdrawnQuantity` column on the
ProdOrder Component sheet — its legend defines it verbatim as "goods-issued
quantity against the component reservation to date", which IS the
movement-type-261 signal, already captured on ingest as
CanonicalBomItem.withdrawn_quantity. There is no separate goods-movement
workbook in the 4 files the lead shared, so this engine reads only that real
column rather than inventing a parallel data source.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CanonicalBomHeader, CanonicalBomItem


async def actual_consumption(
    orders: list[str],
    db: AsyncSession,
) -> dict[str, float]:
    """Total withdrawn (goods-issued) quantity per component across orders.

    Joins CanonicalBomItem -> CanonicalBomHeader so we only sum items that
    belong to a PRODUCTION_ORDER header whose object_id is in `orders` —
    Planned/Sales Order items never carry withdrawn_quantity.
    """
    if not orders:
        return {}

    stmt = (
        select(
            CanonicalBomItem.component,
            func.sum(CanonicalBomItem.withdrawn_quantity),
        )
        .join(CanonicalBomHeader, CanonicalBomItem.header_id == CanonicalBomHeader.id)
        .where(CanonicalBomHeader.object_type == "PRODUCTION_ORDER")
        .where(CanonicalBomHeader.object_id.in_(orders))
        .where(CanonicalBomItem.withdrawn_quantity.isnot(None))
        .group_by(CanonicalBomItem.component)
    )
    result = await db.execute(stmt)
    return {row[0]: float(row[1] or 0.0) for row in result.all()}


async def average_consumption_per_run(
    orders: list[str],
    db: AsyncSession,
) -> dict[str, float]:
    """Average withdrawn quantity per component per run.

    A candidate merged from several historical runs must report per-run
    quantities, otherwise a frequently used path looks artificially expensive.
    """
    totals = await actual_consumption(orders, db)
    if not orders:
        return totals
    divisor = float(len(orders))
    return {k: round(v / divisor, 4) for k, v in totals.items()}


def deviation(
    planned: dict[str, float], actual: dict[str, float]
) -> dict[str, float]:
    """Actual minus planned per component. Positive means over-issue."""
    components = set(planned) | set(actual)
    return {
        c: round(actual.get(c, 0.0) - planned.get(c, 0.0), 4) for c in components
    }
