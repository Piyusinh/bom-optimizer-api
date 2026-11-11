"""Cycle Engine — average production cycles and average reprocess cycles.

These are the two headline columns on the RTM 'Prod Output Persona' sheet:

    Avg cycles            2 vs 6
    Avg. reprocess cycles 3 vs 6

A 'cycle' is one confirmation run against an order. A 'reprocess cycle' is a
rework order chained to an original order. The client's open question about
whether rework quantities are summed or follow specific logic is tracked in the
RTM 'Questions' sheet — for now we count cycles, not quantities.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CanonicalOrderConfirmation, ReworkLink


async def confirmation_counts(
    orders: list[str], db: AsyncSession
) -> dict[str, int]:
    """Number of confirmation records per order."""
    if not orders:
        return {}
    stmt = (
        select(
            CanonicalOrderConfirmation.order_number,
            func.count(CanonicalOrderConfirmation.id),
        )
        .where(CanonicalOrderConfirmation.order_number.in_(orders))
        .group_by(CanonicalOrderConfirmation.order_number)
    )
    result = await db.execute(stmt)
    return {row[0]: int(row[1]) for row in result.all()}


async def rework_counts(orders: list[str], db: AsyncSession) -> dict[str, int]:
    """Number of rework orders chained to each original order."""
    if not orders:
        return {}
    stmt = (
        select(ReworkLink.original_order, func.count(ReworkLink.id))
        .where(ReworkLink.original_order.in_(orders))
        .group_by(ReworkLink.original_order)
    )
    result = await db.execute(stmt)
    return {row[0]: int(row[1]) for row in result.all()}


async def scrap_totals(orders: list[str], db: AsyncSession) -> dict[str, float]:
    """Total confirmed scrap per order — a direct rework-cost signal."""
    if not orders:
        return {}
    stmt = (
        select(
            CanonicalOrderConfirmation.order_number,
            func.sum(CanonicalOrderConfirmation.scrap_quantity),
        )
        .where(CanonicalOrderConfirmation.order_number.in_(orders))
        .group_by(CanonicalOrderConfirmation.order_number)
    )
    result = await db.execute(stmt)
    return {row[0]: float(row[1] or 0.0) for row in result.all()}


def average(counts: dict[str, int], orders: list[str], default: float = 1.0) -> float:
    """Average count across the orders that used a candidate.

    Orders with no records contribute `default` so that a candidate backed by
    orders we have no confirmation data for is not unfairly rewarded with 0.
    """
    if not orders:
        return default
    total = sum(counts.get(o, default) for o in orders)
    return round(total / len(orders), 2)


async def cycle_metrics(
    orders: list[str], db: AsyncSession
) -> tuple[float, float]:
    """Return (avg_cycles, avg_reprocess_cycles) for a candidate's orders."""
    confirmations = await confirmation_counts(orders, db)
    reworks = await rework_counts(orders, db)

    avg_cycles = average(confirmations, orders, default=1.0)
    # Orders with no rework link genuinely had zero reprocess cycles.
    avg_reprocess = average(reworks, orders, default=0.0)
    return avg_cycles, avg_reprocess
