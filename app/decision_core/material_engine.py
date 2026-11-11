"""Material Master Engine — MARA/MARC lookups, active filtering, cost per unit.

RTM 'Proposed Logic for master data' rules implemented here:
  1. Take unique materials from a BOM explosion, then pull MARA + MARC.
  3. Consider only materials that are ACTIVE (status + deletion indicator).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CanonicalMaterial

# Material types that are genuine stock components in a recipe.
STOCK_MATERIAL_TYPES = {"ROH", "HALB", "VERP"}
OUTPUT_MATERIAL_TYPES = {"FERT", "HALB"}


async def load_materials(
    materials: set[str],
    plant: str,
    db: AsyncSession,
    active_only: bool = True,
) -> dict[str, CanonicalMaterial]:
    """Load material master records for a set of material numbers at a plant.

    Returns a dict keyed by material number. Missing materials are simply
    absent from the result — callers must tolerate that, because Phase 1 mock
    data does not guarantee master data for every component.
    """
    if not materials:
        return {}

    stmt = (
        select(CanonicalMaterial)
        .where(CanonicalMaterial.material.in_(materials))
        .where(CanonicalMaterial.plant == plant)
    )
    if active_only:
        stmt = stmt.where(
            CanonicalMaterial.status == "ACTIVE",
            CanonicalMaterial.deletion_indicator.is_(False),
        )

    result = await db.execute(stmt)
    return {m.material: m for m in result.scalars().all()}


async def is_active(material: str, plant: str, db: AsyncSession) -> bool:
    """Whether a material is usable. Unknown materials are treated as active
    so that incomplete mock master data does not make every path infeasible."""
    stmt = (
        select(CanonicalMaterial)
        .where(CanonicalMaterial.material == material)
        .where(CanonicalMaterial.plant == plant)
        .limit(1)
    )
    result = await db.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        return True
    return record.status == "ACTIVE" and not record.deletion_indicator


def unit_cost(material: str, masters: dict[str, CanonicalMaterial]) -> float:
    record = masters.get(material)
    return float(record.cost) if record and record.cost else 0.0


def available_stock(material: str, masters: dict[str, CanonicalMaterial]) -> float | None:
    record = masters.get(material)
    return float(record.stock) if record and record.stock is not None else None


def max_lead_time_days(
    components: list[str], masters: dict[str, CanonicalMaterial]
) -> int:
    """Longest component lead time — the path's replenishment constraint."""
    values = [
        int(masters[c].lead_time_days or 0) for c in components if c in masters
    ]
    return max(values) if values else 0
