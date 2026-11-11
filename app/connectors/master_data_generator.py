"""Deterministic master-data generator for the Phase 1 demo.

The client has supplied final transactional BOM mock data, but NOT material
master, production versions, recipes, goods movements or rework hierarchies.
Those are exactly the objects the transcript said we need:

    "So we need materials basically, that will give us the cost. We'll have the
     BOM that will tell us the combinations. We need the recipes that will give
     us the operations and other details, like the operational costs... And then
     the PVs will give the different combinations of recipes."

This module derives all of them from whatever BOM data is already ingested, so
the demo is complete without waiting on another client delivery.

Everything is seeded from the material number, so the same input always
produces the same master data. That keeps the demo reproducible and keeps
scoring deterministic, which is a hard audit requirement.
"""
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CanonicalBomHeader,
    CanonicalBomItem,
    CanonicalGoodsMovement,
    CanonicalMaterial,
    CanonicalOrderConfirmation,
    CanonicalProductionVersion,
    CanonicalRecipe,
    CanonicalRecipeOperation,
    ReworkLink,
)

# Material-type inference from the client's own nomenclature, documented in
# 'Prompt.txt' and the RTM 'Sheet2' tab.
TYPE_PREFIXES: list[tuple[tuple[str, ...], str]] = [
    (("F",), "FERT"),
    (("B", "P", "SF", "INT"), "HALB"),
    (("R",), "ROH"),
    (("A", "X", "PACK"), "VERP"),
]

# Indicative cost bands per material type, in USD per base unit.
COST_BANDS: dict[str, tuple[float, float]] = {
    "FERT": (40.0, 120.0),
    "HALB": (15.0, 60.0),
    "ROH": (2.0, 25.0),
    "VERP": (0.5, 6.0),
}

MRP_TYPES = ["PD", "VB", "ND"]
MRP_CONTROLLERS = ["001", "002", "003"]
WORK_CENTERS = ["WC-MIX-01", "WC-MIX-02", "WC-REACT-01", "WC-PACK-01"]

OPERATION_TEMPLATE = [
    ("CHARGE", 0.5, 1.0),
    ("MIX", 0.3, 2.5),
    ("REACT", 0.4, 3.0),
    ("TEST", 0.2, 0.5),
    ("PACK", 0.3, 1.5),
]


def _seed(*parts: str) -> int:
    """Stable integer seed from string parts. Deterministic across processes,
    unlike Python's built-in hash which is salted per run."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:8], 16)


def _pick(options: list, *parts: str):
    return options[_seed(*parts) % len(options)]


def _in_band(low: float, high: float, *parts: str) -> float:
    span = high - low
    fraction = (_seed(*parts) % 1000) / 1000.0
    return round(low + span * fraction, 2)


def infer_material_type(material: str) -> str:
    upper = material.upper()
    # Strip a DEMO- style prefix so generated demo materials classify correctly.
    for marker in ("DEMO-", "TEST-"):
        if upper.startswith(marker):
            upper = upper[len(marker):]
            break
    for prefixes, material_type in TYPE_PREFIXES:
        if upper.startswith(prefixes):
            return material_type
    return "ROH"


def build_material(material: str, plant: str) -> CanonicalMaterial:
    material_type = infer_material_type(material)
    low, high = COST_BANDS[material_type]

    return CanonicalMaterial(
        material=material,
        description=f"{material_type} {material}",
        plant=plant,
        material_type=material_type,
        proc_type="E" if material_type in ("FERT", "HALB") else "F",
        mrp_type=_pick(MRP_TYPES, material, plant, "mrp_type"),
        mrp_controller=_pick(MRP_CONTROLLERS, material, plant, "mrp_controller"),
        stock=_in_band(500.0, 20000.0, material, plant, "stock"),
        uom="EA" if material_type == "VERP" else "KG",
        cost=_in_band(low, high, material, plant, "cost"),
        currency="USD",
        batch_managed=material_type in ("FERT", "HALB", "ROH"),
        lead_time_days=1 + _seed(material, plant, "lead") % 21,
        grade=_pick(["A", "B", "C"], material, plant, "grade") if material_type == "ROH" else None,
        status="ACTIVE",
        deletion_indicator=False,
        source_system="generated-master-data",
    )


def build_recipe(
    material: str, plant: str, variant: int
) -> tuple[CanonicalRecipe, list[CanonicalRecipeOperation]]:
    recipe_id = f"RCP-{material}-{variant:02d}"
    recipe = CanonicalRecipe(
        recipe_id=recipe_id,
        recipe_name=f"Recipe {variant} for {material}",
        plant=plant,
        material=material,
        usage="1",
        status="RELEASED",
        base_quantity=1000.0,
        base_unit="KG",
        source_terminology="RECIPE",
        source_system="generated-master-data",
    )

    operations = []
    for index, (name, setup, run) in enumerate(OPERATION_TEMPLATE, start=1):
        # Later variants are progressively more efficient, which is what makes
        # the cost ranking tell a story instead of being flat.
        efficiency = 1.0 - (variant - 1) * 0.08
        work_center = _pick(WORK_CENTERS, recipe_id, name)
        operations.append(
            CanonicalRecipeOperation(
                operation_number=index * 10,
                description=name,
                work_center=work_center,
                control_key="PP01",
                setup_time_hours=round(setup * efficiency, 2),
                run_time_hours=round(run * efficiency, 2),
                teardown_time_hours=0.1,
                cost_per_hour=_in_band(45.0, 120.0, recipe_id, name, "rate"),
            )
        )

    # Attach via the relationship so SQLAlchemy fills recipe_pk on flush —
    # recipe.id is not populated until then.
    recipe.operations = operations
    return recipe, operations


async def generate_master_data(
    db: AsyncSession, replace: bool = True
) -> dict[str, int]:
    """Derive Phase 1 master data from the BOM data already in the database.

    Returns a count per object type so the UI can report what was created.
    """
    if replace:
        for model in (
            CanonicalRecipeOperation,
            CanonicalRecipe,
            CanonicalProductionVersion,
            CanonicalMaterial,
            CanonicalGoodsMovement,
            CanonicalOrderConfirmation,
            ReworkLink,
        ):
            await db.execute(delete(model))

    counts = {
        "materials": 0,
        "production_versions": 0,
        "recipes": 0,
        "recipe_operations": 0,
        "goods_movements": 0,
        "order_confirmations": 0,
        "rework_links": 0,
    }

    # ── 1. Material master for every material and component, per plant ────────
    header_rows = (
        await db.execute(
            select(CanonicalBomHeader.material, CanonicalBomHeader.plant).distinct()
        )
    ).all()

    plants = {plant for _, plant in header_rows}
    finished_by_plant: dict[str, set[str]] = {}
    for material, plant in header_rows:
        finished_by_plant.setdefault(plant, set()).add(material)

    component_rows = (
        await db.execute(
            select(CanonicalBomItem.component, CanonicalBomHeader.plant)
            .join(CanonicalBomHeader, CanonicalBomItem.header_id == CanonicalBomHeader.id)
            .distinct()
        )
    ).all()

    pairs: set[tuple[str, str]] = set(header_rows) | set(component_rows)
    for material, plant in sorted(pairs):
        db.add(build_material(material, plant))
        counts["materials"] += 1

    # ── 2. Recipes + production versions per finished material ───────────────
    now = datetime.utcnow()
    recipe_by_material: dict[tuple[str, str], list[str]] = {}

    for plant in sorted(plants):
        for material in sorted(finished_by_plant.get(plant, set())):
            variants = 1 + _seed(material, plant, "variants") % 3  # 1..3
            recipe_ids: list[str] = []

            for variant in range(1, variants + 1):
                recipe, operations = build_recipe(material, plant, variant)
                # Operations cascade from the recipe relationship.
                db.add(recipe)
                counts["recipes"] += 1
                counts["recipe_operations"] += len(operations)
                recipe_ids.append(recipe.recipe_id)

                primary_line = operations[1].work_center if len(operations) > 1 else None
                db.add(
                    CanonicalProductionVersion(
                        production_version=f"PV-{variant:02d}",
                        material=material,
                        plant=plant,
                        bom_usage="1",
                        alternative_bom=f"{variant:02d}",
                        recipe_id=recipe.recipe_id,
                        # Wide validity window so historical orders resolve.
                        valid_from=now - timedelta(days=365 * 5),
                        valid_to=now + timedelta(days=365 * 2),
                        production_line=primary_line,
                        lot_size_min=100.0,
                        lot_size_max=50000.0,
                        status="ACTIVE",
                        source_system="generated-master-data",
                    )
                )
                counts["production_versions"] += 1

            recipe_by_material[(material, plant)] = recipe_ids

    await db.flush()

    # ── 3. Attach PV / recipe / line / start date to each BOM header ──────────
    headers = (await db.execute(select(CanonicalBomHeader))).scalars().all()

    for header in headers:
        items = (
            await db.execute(
                select(CanonicalBomItem.component, CanonicalBomItem.quantity)
                .where(CanonicalBomItem.header_id == header.id)
            )
        ).all()

        # MKAL ties ONE BOM alternative to ONE recipe. Headers sharing the same
        # component combination must therefore resolve to the same production
        # version, and different combinations to different versions.
        signature = "|".join(
            sorted(f"{c}:{round(float(q or 0.0), 4)}" for c, q in items)
        )

        recipe_ids = recipe_by_material.get((header.material, header.plant))
        if recipe_ids and signature:
            index = _seed(signature, "recipe") % len(recipe_ids)
            header.recipe_id = recipe_ids[index]
            header.production_version = f"PV-{index + 1:02d}"
            header.alternative_bom = f"{index + 1:02d}"
            header.production_line = _pick(WORK_CENTERS, signature, "line")
        else:
            header.production_line = _pick(WORK_CENTERS, header.object_id, "line")

        if header.order_start_date is None:
            # Spread runs across the last 24 months so the date-range filter and
            # season ranking have something meaningful to work with.
            days_ago = _seed(header.object_id, "start") % (24 * 30)
            header.order_start_date = now - timedelta(days=days_ago)

    await db.flush()

    # ── 4. Goods movements (261), confirmations and rework links ──────────────
    for header in headers:
        if header.object_type not in ("PRODUCTION_ORDER", "PLANNED_ORDER"):
            continue

        items = (
            await db.execute(
                select(CanonicalBomItem).where(
                    CanonicalBomItem.header_id == header.id
                )
            )
        ).scalars().all()

        order = header.object_id
        posting_date = header.order_start_date or now

        for item in items:
            # Actual issue deviates from plan by up to +/-4%, which is the real
            # dosing deviation the consumption engine is designed to surface.
            drift = ((_seed(order, item.component, "drift") % 81) - 40) / 1000.0
            db.add(
                CanonicalGoodsMovement(
                    material_document=f"MD-{_seed(order, item.component) % 10**8:08d}",
                    posting_date=posting_date,
                    material=item.component,
                    plant=header.plant,
                    batch=f"B{_seed(order, item.component, 'batch') % 10**6:06d}",
                    quantity=round(item.quantity * (1.0 + drift), 4),
                    unit=item.unit,
                    movement_type="261",
                    order_reference=order,
                    source_system="generated-master-data",
                )
            )
            counts["goods_movements"] += 1

        cycles = 1 + _seed(order, "cycles") % 3
        for counter in range(1, cycles + 1):
            db.add(
                CanonicalOrderConfirmation(
                    order_number=order,
                    confirmation_counter=counter,
                    operation_number=counter * 10,
                    work_center=header.production_line,
                    actual_start=posting_date + timedelta(hours=counter * 4),
                    actual_end=posting_date + timedelta(hours=counter * 4 + 3),
                    yield_quantity=round(float(header.quantity or 1000.0) / cycles, 2),
                    scrap_quantity=round(
                        float(header.quantity or 1000.0)
                        * (_seed(order, "scrap", str(counter)) % 30)
                        / 1000.0,
                        2,
                    ),
                    unit=header.base_unit or "KG",
                    source_system="generated-master-data",
                )
            )
            counts["order_confirmations"] += 1

        # Rework only on a subset of orders, so 'avg reprocess cycles' varies
        # between candidates instead of being uniform.
        reworks = _seed(order, "rework") % 3
        for level in range(1, reworks + 1):
            db.add(
                ReworkLink(
                    original_order=order,
                    rework_order=f"{order}-RW{level}",
                    level=level,
                    material=header.material,
                    plant=header.plant,
                    reason="Off-spec blend requiring reprocessing",
                )
            )
            counts["rework_links"] += 1

    await db.commit()
    return counts
