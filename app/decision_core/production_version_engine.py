"""Production Version Engine — resolves MKAL versions valid on a given date.

Critical rule from the SAP data reference workbook:

    "BOM data is time-dependent — resolve the version that was valid on the
     order date. When joining a historical order back to 'the BOM used',
     resolve the version valid on that order's creation date, not today's
     current BOM, or old batches will be silently mislabeled."

Every lookup here therefore takes an `as_of` date.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CanonicalProductionVersion


def _valid_on(pv: CanonicalProductionVersion, as_of: datetime) -> bool:
    if pv.valid_from and as_of < pv.valid_from:
        return False
    if pv.valid_to and as_of > pv.valid_to:
        return False
    return True


async def resolve_versions(
    material: str,
    plant: str,
    db: AsyncSession,
    as_of: datetime | None = None,
    production_line: str | None = None,
) -> list[CanonicalProductionVersion]:
    """All production versions for a material+plant valid on `as_of`.

    `as_of` defaults to now. When `production_line` is given, versions bound to
    a different line are excluded; versions with no line are always kept
    because they are line-agnostic.
    """
    as_of = as_of or datetime.utcnow()

    stmt = (
        select(CanonicalProductionVersion)
        .where(CanonicalProductionVersion.material == material)
        .where(CanonicalProductionVersion.plant == plant)
        .where(CanonicalProductionVersion.status == "ACTIVE")
    )
    result = await db.execute(stmt)
    versions = [pv for pv in result.scalars().all() if _valid_on(pv, as_of)]

    if production_line:
        versions = [
            pv
            for pv in versions
            if not pv.production_line or pv.production_line == production_line
        ]

    return versions


async def find_version(
    material: str,
    plant: str,
    production_version: str,
    db: AsyncSession,
) -> CanonicalProductionVersion | None:
    stmt = (
        select(CanonicalProductionVersion)
        .where(CanonicalProductionVersion.material == material)
        .where(CanonicalProductionVersion.plant == plant)
        .where(CanonicalProductionVersion.production_version == production_version)
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def lot_size_violation(
    pv: CanonicalProductionVersion, quantity: float
) -> str | None:
    """MARC/MKAL lot-size limits are hard constraints on any recommendation."""
    if pv.lot_size_min is not None and quantity < pv.lot_size_min:
        return (
            f"Quantity {quantity:g} is below the minimum lot size "
            f"{pv.lot_size_min:g} for production version {pv.production_version}"
        )
    if pv.lot_size_max is not None and quantity > pv.lot_size_max:
        return (
            f"Quantity {quantity:g} exceeds the maximum lot size "
            f"{pv.lot_size_max:g} for production version {pv.production_version}"
        )
    return None
