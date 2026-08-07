"""Constraint Engine — validates hard constraints, returns violations."""
from app.schemas import ConstraintsInput


def evaluate_constraints(
    components: list[dict],
    constraints: ConstraintsInput,
    known_components: set[str],
) -> tuple[bool, list[str]]:
    """
    Evaluate hard constraints on a candidate BOM.
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
