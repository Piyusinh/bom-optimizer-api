"""BOM Constraint & Business Rule Engine — validates hard constraints."""
from app.models import CanonicalMaterial
from app.schemas import ConstraintsInput

# Item categories that are not real stock components and must be excluded from
# costing. 'L' = stock item, 'N' = non-stock, 'T' = text, 'R' = variable-size.
NON_STOCK_ITEM_CATEGORIES = {"T", "N"}

# Material types that trigger a legal / regulatory approval flag. The client has
# not yet named the SAP source for approval status, so this is a placeholder
# rule tracked as an open item in the Phase 1 plan.
LEGALLY_CONTROLLED_TYPES = {"VERP"}


def evaluate_rules(
    components: list[dict],
    constraints: ConstraintsInput,
    known_components: set[str],
) -> tuple[bool, list[str]]:
    """
    Evaluate hard business rules and constraints on a candidate BOM.
    Returns (feasible, list_of_violations).
    """
    violations: list[str] = []

    # 1. Must use only registered/known components
    if constraints.must_use_registered_components:
        for comp in components:
            if comp["component"] not in known_components:
                violations.append(
                    f"Component {comp['component']} is not registered in master data"
                )

    # 2. Avoid specific components
    if constraints.avoid_components:
        for comp in components:
            if comp["component"] in constraints.avoid_components:
                violations.append(
                    f"Component {comp['component']} is in the avoid list"
                )

    # 3. Max components limit
    if constraints.max_components and len(components) > constraints.max_components:
        violations.append(
            f"BOM has {len(components)} components, max allowed is {constraints.max_components}"
        )

    # 4. Min quantity per component
    if constraints.min_quantity_per_component:
        for comp in components:
            if comp["quantity"] < constraints.min_quantity_per_component:
                violations.append(
                    f"Component {comp['component']} has quantity {comp['quantity']} below minimum {constraints.min_quantity_per_component}"
                )

    feasible = len(violations) == 0
    return feasible, violations


# ─── Phase 1 master-data rules ─────────────────────────────────────────────────

def evaluate_master_data_rules(
    components: list[dict],
    masters: dict[str, CanonicalMaterial],
) -> list[str]:
    """Rules that depend on material master, not on the user's constraints.

    RTM 'Proposed Logic for master data' item 3:
        "Consider materials that are active (status or deletion indicator)"
    """
    violations: list[str] = []

    for comp in components:
        component = comp["component"]
        master = masters.get(component)
        if master is None:
            continue
        if master.deletion_indicator:
            violations.append(
                f"Component {component} is flagged for deletion in material master"
            )
        elif master.status != "ACTIVE":
            violations.append(
                f"Component {component} is not active (status {master.status})"
            )

    return violations


def stock_components(components: list[dict]) -> list[dict]:
    """Filter to real stock components.

    Per the SAP data reference: STPO-POSTP "distinguishes stock items from text
    items, phantom assemblies, etc. — filter to real stock components."
    """
    return [
        c
        for c in components
        if (c.get("item_category") or "L") not in NON_STOCK_ITEM_CATEGORIES
    ]


def requires_legal_approval(
    components: list[dict],
    masters: dict[str, CanonicalMaterial],
) -> bool:
    """Whether adopting this path needs the 'Legal approval' flag shown in the
    RTM Prod Output Persona layout."""
    for comp in components:
        master = masters.get(comp["component"])
        if master and master.material_type in LEGALLY_CONTROLLED_TYPES:
            return True
    return False
