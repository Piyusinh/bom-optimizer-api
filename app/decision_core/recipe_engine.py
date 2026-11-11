"""Recipe Engine — PLKO/PLPO operations, operation hours and operational cost.

'Recipe' in the chemical world, 'Routing' in discrete manufacturing. Same
tables, same structure — only the terminology differs.

Operational cost is the second half of the cost picture the client insisted on:
    total cost = material cost + operational cost
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import CanonicalRecipe, CanonicalRecipeOperation
from app.schemas import OperationDetail


async def load_recipe(
    recipe_id: str, plant: str, db: AsyncSession
) -> CanonicalRecipe | None:
    stmt = (
        select(CanonicalRecipe)
        .options(selectinload(CanonicalRecipe.operations))
        .where(CanonicalRecipe.recipe_id == recipe_id)
        .where(CanonicalRecipe.plant == plant)
        .where(CanonicalRecipe.status == "RELEASED")
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def operation_hours(op: CanonicalRecipeOperation) -> float:
    return (
        float(op.setup_time_hours or 0.0)
        + float(op.run_time_hours or 0.0)
        + float(op.teardown_time_hours or 0.0)
    )


def operation_cost(op: CanonicalRecipeOperation) -> float:
    return round(operation_hours(op) * float(op.cost_per_hour or 0.0), 2)


def to_operation_details(recipe: CanonicalRecipe | None) -> list[OperationDetail]:
    if recipe is None:
        return []
    return [
        OperationDetail(
            operation_number=op.operation_number,
            description=op.description,
            work_center=op.work_center,
            setup_time_hours=float(op.setup_time_hours or 0.0),
            run_time_hours=float(op.run_time_hours or 0.0),
            teardown_time_hours=float(op.teardown_time_hours or 0.0),
            cost_per_hour=float(op.cost_per_hour or 0.0),
            operation_cost=operation_cost(op),
        )
        for op in sorted(recipe.operations, key=lambda o: o.operation_number)
    ]


def total_operation_hours(operations: list[OperationDetail]) -> float:
    return round(
        sum(
            o.setup_time_hours + o.run_time_hours + o.teardown_time_hours
            for o in operations
        ),
        2,
    )


def total_operational_cost(
    operations: list[OperationDetail], quantity_factor: float = 1.0
) -> float:
    """Sum of operation costs.

    Setup and teardown are fixed per run; run time scales with batch size, so
    only run time is multiplied by `quantity_factor`.
    """
    total = 0.0
    for o in operations:
        fixed_hours = o.setup_time_hours + o.teardown_time_hours
        variable_hours = o.run_time_hours * quantity_factor
        total += (fixed_hours + variable_hours) * o.cost_per_hour
    return round(total, 2)


def work_centers(operations: list[OperationDetail]) -> list[str]:
    seen: list[str] = []
    for o in operations:
        if o.work_center and o.work_center not in seen:
            seen.append(o.work_center)
    return seen
