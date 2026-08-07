"""BOM Explosion Engine — expands a material into candidate component lists."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import CanonicalBomHeader, CanonicalBomItem


def _signature(components: list[dict]) -> tuple:
    """
    Canonical signature for a component list — identifies whether two
    historical BOMs represent the SAME production combination.

    Core concept (per stakeholder transcripts): the same finished material
    may have been produced multiple times historically using different
    component combinations. Runs that used an IDENTICAL combination must be
    merged into a single candidate so we can measure how often it recurred
    (historical frequency) — not treated as separate, duplicate candidates.
    """
    return tuple(
        sorted(
            (c["component"], round(c["quantity"], 4), c["unit"])
            for c in components
        )
    )


async def explode_bom(
    material: str,
    plant: str,
    db: AsyncSession,
) -> tuple[list[dict], int]:
    """
    Find all known BOM variations for a material+plant, and merge
    historically-identical component combinations into a single candidate.

    Returns (candidates, total_runs) where:
      - candidates: deduplicated list of candidate dicts, each carrying
        `occurrence_count` (how many historical runs used this exact
        combination) and `source_orders` (all order IDs that used it).
      - total_runs: total number of historical BOM headers found (the
        denominator for historical-frequency scoring).
    """
    # Find all BOM headers for this material at this plant
    stmt = (
        select(CanonicalBomHeader)
        .where(CanonicalBomHeader.material == material)
        .where(CanonicalBomHeader.plant == plant)
    )
    result = await db.execute(stmt)
    headers = result.scalars().all()

    grouped: dict[tuple, dict] = {}
    total_runs = 0

    for header in headers:
        # Load items for this header
        items_stmt = (
            select(CanonicalBomItem)
            .where(CanonicalBomItem.header_id == header.id)
        )
        items_result = await db.execute(items_stmt)
        items = items_result.scalars().all()

        if not items:
            continue

        components = [
            {
                "component": item.component,
                "quantity": item.quantity,
                "unit": item.unit,
                "scrap_percent": item.scrap_percent or 0.0,
            }
            for item in items
        ]

        total_runs += 1
        sig = _signature(components)
        source_order = header.object_id if header.object_type != "MASTER" else None

        if sig not in grouped:
            grouped[sig] = {
                "object_type": header.object_type,
                "source_orders": [source_order] if source_order else [],
                "occurrence_count": 1,
                "components": components,
            }
        else:
            entry = grouped[sig]
            entry["occurrence_count"] += 1
            if source_order:
                entry["source_orders"].append(source_order)

    candidates = list(grouped.values())
    return candidates, total_runs
