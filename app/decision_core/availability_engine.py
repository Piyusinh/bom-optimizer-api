"""Availability Engine — raw material availability for planned-order simulation.

RTM 'Algorithm Logic' item 5:

    "Raw material availability in case of planned order simulation."

Stock comes from the material master plant view (MARD/MARC). A path that needs
a component we do not have is still shown, but flagged, because the planner may
choose to expedite rather than switch formulation.
"""
from app.models import CanonicalMaterial
from app.schemas import ComponentDetail


def shortages(components: list[ComponentDetail]) -> list[ComponentDetail]:
    """Components whose required quantity exceeds known available stock."""
    result = []
    for c in components:
        if c.available_stock is None:
            continue
        required = c.planned_quantity * (1.0 + c.scrap_percent / 100.0)
        if required > c.available_stock:
            result.append(c)
    return result


def availability_violations(components: list[ComponentDetail]) -> list[str]:
    return [
        (
            f"Insufficient stock for {c.component}: "
            f"need {c.planned_quantity:g} {c.unit}, "
            f"have {c.available_stock:g} {c.unit}"
        )
        for c in shortages(components)
    ]


def max_lead_time_days(
    components: list[ComponentDetail],
    masters: dict[str, CanonicalMaterial],
) -> int:
    """Longest lead time among components that are short.

    This is how long the planner would actually wait if they picked this path
    and had to replenish, which is the number that matters for EFFICIENCY
    ranking, not the lead time of components already in stock.
    """
    short = {c.component for c in shortages(components)}
    values = [
        int(masters[c].lead_time_days or 0) for c in short if c in masters
    ]
    return max(values) if values else 0


def is_feasible(components: list[ComponentDetail]) -> bool:
    return not shortages(components)
