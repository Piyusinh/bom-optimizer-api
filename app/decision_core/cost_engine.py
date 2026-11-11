"""Cost Engine — material cost + operational cost.

From the transcript:

    "Material cost is one thing, operational cost is also another thing, right?
     So we need to consider both."

Material cost comes from the material master (MARA/MARC), NOT from a manual
monthly Excel upload. Operational cost comes from the recipe operations.
"""
from app.models import CanonicalMaterial
from app.schemas import ComponentDetail, CostBreakdown, OperationDetail
from app.decision_core import material_engine, recipe_engine


def build_component_details(
    components: list[dict],
    masters: dict[str, CanonicalMaterial],
    actual_quantities: dict[str, float] | None = None,
    quantity_factor: float = 1.0,
) -> list[ComponentDetail]:
    """Cost out each component line at the requested batch size."""
    actual_quantities = actual_quantities or {}
    details: list[ComponentDetail] = []

    for c in components:
        component = c["component"]
        master = masters.get(component)
        planned = float(c["quantity"]) * quantity_factor
        unit_cost = material_engine.unit_cost(component, masters)
        scrap = float(c.get("scrap_percent") or 0.0)

        # Scrap allowance increases the quantity that must actually be bought.
        effective_quantity = planned * (1.0 + scrap / 100.0)

        details.append(
            ComponentDetail(
                component=component,
                description=master.description if master else None,
                material_type=master.material_type if master else None,
                grade=master.grade if master else None,
                planned_quantity=round(planned, 4),
                actual_quantity=actual_quantities.get(component),
                unit=c.get("unit", "KG"),
                scrap_percent=scrap,
                unit_cost=unit_cost,
                extended_cost=round(effective_quantity * unit_cost, 2),
                available_stock=material_engine.available_stock(component, masters),
            )
        )

    return details


def material_cost(components: list[ComponentDetail]) -> float:
    return round(sum(c.extended_cost for c in components), 2)


def build_cost_breakdown(
    components: list[ComponentDetail],
    operations: list[OperationDetail],
    quantity_factor: float = 1.0,
    currency: str = "USD",
) -> CostBreakdown:
    mat = material_cost(components)
    ops = recipe_engine.total_operational_cost(operations, quantity_factor)
    return CostBreakdown(
        material_cost=mat,
        operational_cost=ops,
        total_cost=round(mat + ops, 2),
        currency=currency,
    )


def reduction_percent(baseline_total: float, candidate_total: float) -> float | None:
    """Positive means the candidate is cheaper than the baseline.

    This is the '40% reduction' column on the Prod Output Persona sheet.
    """
    if not baseline_total:
        return None
    return round((baseline_total - candidate_total) / baseline_total * 100.0, 1)
