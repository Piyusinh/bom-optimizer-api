"""BOM Explosion Engine — expands a material into candidate component lists."""
from datetime import datetime

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


async def explode_bom_filtered(
    material: str,
    plant: str,
    db: AsyncSession,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    production_line: str | None = None,
    object_types: list[str] | None = None,
) -> tuple[list[dict], int]:
    """Phase 1 explosion — same dedup logic, but narrowed and richer.

    Adds the filters the RTM Production sheet requires:
      - date range, because "maybe they are not interested in that BOM [from]
        10 years ago... they may be interested in the production that happened
        in the last two years"
      - production line, because "on shop floor, program should consider the
        machine/production line for analysis"

    Each candidate additionally carries the production version, alternative
    BOM, recipe, line and the run dates, so cost, season and recency ranking
    have something to work with.
    """
    stmt = (
        select(CanonicalBomHeader)
        .where(CanonicalBomHeader.material == material)
        .where(CanonicalBomHeader.plant == plant)
    )
    if object_types:
        stmt = stmt.where(CanonicalBomHeader.object_type.in_(object_types))
    if production_line:
        stmt = stmt.where(CanonicalBomHeader.production_line == production_line)

    result = await db.execute(stmt)
    headers = result.scalars().all()

    grouped: dict[tuple, dict] = {}
    total_runs = 0

    for header in headers:
        run_date = header.order_start_date or header.valid_from
        if date_from and run_date and run_date < date_from:
            continue
        if date_to and run_date and run_date > date_to:
            continue

        items_result = await db.execute(
            select(CanonicalBomItem).where(CanonicalBomItem.header_id == header.id)
        )
        items = items_result.scalars().all()
        if not items:
            continue

        components = [
            {
                "component": item.component,
                "quantity": item.quantity,
                "unit": item.unit,
                "scrap_percent": item.scrap_percent or 0.0,
                "item_category": item.item_category,
            }
            for item in items
        ]

        total_runs += 1
        sig = _signature(components)
        source_order = header.object_id if header.object_type != "MASTER" else None

        entry = grouped.get(sig)
        if entry is None:
            grouped[sig] = {
                "object_type": header.object_type,
                "source_orders": [source_order] if source_order else [],
                "occurrence_count": 1,
                "components": components,
                "production_version": header.production_version,
                "alternative_bom": header.alternative_bom,
                "recipe_id": header.recipe_id,
                "production_line": header.production_line,
                "run_dates": [run_date] if run_date else [],
                "base_quantity": header.quantity or 1.0,
                "base_unit": header.base_unit or "KG",
            }
        else:
            entry["occurrence_count"] += 1
            if source_order:
                entry["source_orders"].append(source_order)
            if run_date:
                entry["run_dates"].append(run_date)
            # Keep the first non-null master-data reference we encounter.
            for key, value in (
                ("production_version", header.production_version),
                ("alternative_bom", header.alternative_bom),
                ("recipe_id", header.recipe_id),
                ("production_line", header.production_line),
            ):
                if entry.get(key) is None and value is not None:
                    entry[key] = value

    return list(grouped.values()), total_runs


async def explode_multi_level(
    material: str,
    plant: str,
    db: AsyncSession,
    max_depth: int = 10,
) -> set[str]:
    """Multi-level explosion returning every unique material in the structure.

    This is RTM 'Proposed Logic for master data' item 1:

        "Materials to be extracted based on the list of finished products by
         multi-level BOM explosion. Take the unique materials from the output
         and extract MARA & MARC"

    `visited` guarantees cycle safety — a component that reappears deeper in
    the structure is not expanded twice, so a circular BOM cannot hang the
    explosion.
    """
    collected: set[str] = {material}
    visited: set[str] = set()
    frontier: list[tuple[str, int]] = [(material, 0)]

    while frontier:
        current, depth = frontier.pop()
        if current in visited or depth >= max_depth:
            continue
        visited.add(current)

        header_result = await db.execute(
            select(CanonicalBomHeader.id)
            .where(CanonicalBomHeader.material == current)
            .where(CanonicalBomHeader.plant == plant)
        )
        header_ids = [row[0] for row in header_result.all()]
        if not header_ids:
            continue

        item_result = await db.execute(
            select(CanonicalBomItem.component)
            .where(CanonicalBomItem.header_id.in_(header_ids))
            .distinct()
        )
        for (component,) in item_result.all():
            collected.add(component)
            if component not in visited:
                frontier.append((component, depth + 1))

    return collected
