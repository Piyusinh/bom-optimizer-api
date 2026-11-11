"""BOM Ranking / Optimization Engine — ranks feasible candidates.

Two ranking models live here:

1. `rank_candidates`  — the Phase 0 weighted-score ranking, still used by
   `/api/analyze-bom`.
2. `rank_by_criteria` — the Phase 1 Production Persona ranking, where the
   planner ORDERS the criteria instead of supplying weights.

The second one matters because of what the client actually asked for:

    "So let's say I want to consider the cost. In addition to the cost, I want
     to consider the date range also. In addition to that, I want to consider
     production line. In addition to that, I want to consider my margin. So
     they can actually rank one, two, three, 4."

That is a priority list, not a weight vector.
"""
from app.schemas import CandidateResponse, ProductionCandidate, RankingCriterionInput

# Direction each criterion should be optimised in.
# MIN → lower is better, MAX → higher is better.
CRITERION_DIRECTION: dict[str, str] = {
    "COST": "MIN",
    "RECENCY": "MAX",
    "REWORK_CYCLES": "MIN",
    "PRODUCTION_LINE": "MAX",
    "SEASON": "MAX",
    "EFFICIENCY": "MIN",
}

CRITERION_LABEL: dict[str, str] = {
    "COST": "total cost",
    "RECENCY": "how recently it was used",
    "REWORK_CYCLES": "reprocess cycles",
    "PRODUCTION_LINE": "production line match",
    "SEASON": "seasonal match",
    "EFFICIENCY": "operation hours",
}


def rank_candidates(candidates: list[CandidateResponse]) -> list[CandidateResponse]:
    """
    Sort candidates: feasible first, then by total score descending.
    Assigns a rank to each candidate in place.
    """
    candidates.sort(key=lambda c: (not c.feasible, -c.total_score))
    for rank, cand in enumerate(candidates, 1):
        cand.rank = rank
    return candidates


def _sort_value(candidate: ProductionCandidate, criterion: str) -> float:
    """Normalise a criterion into a value where LOWER always sorts first."""
    raw = candidate.criterion_values.get(criterion, 0.0)
    return raw if CRITERION_DIRECTION.get(criterion, "MIN") == "MIN" else -raw


def rank_by_criteria(
    candidates: list[ProductionCandidate],
    criteria: list[RankingCriterionInput],
) -> list[ProductionCandidate]:
    """Lexicographic ranking by the planner's ordered criteria.

    Feasible candidates always come first. Within each feasibility group the
    criteria are applied in priority order, so criterion 1 dominates and later
    criteria only break ties. `occurrence_count` is the final deterministic
    tie-break, which guarantees a stable order for the same input.
    """
    ordered = sorted(criteria, key=lambda c: c.priority)
    keys = [c.criterion for c in ordered]

    candidates.sort(
        key=lambda c: (
            not c.feasible,
            *[_sort_value(c, k) for k in keys],
            -c.occurrence_count,
            c.id,
        )
    )

    for rank, cand in enumerate(candidates, 1):
        cand.rank = rank

    return candidates


def build_rank_reason(
    candidate: ProductionCandidate,
    criteria: list[RankingCriterionInput],
    baseline_reprocess_cycles: float | None = None,
) -> str:
    """Human-readable 'why is it ranked this way' summary.

    The client asked for exactly this: "by rank, we can give a summary of what
    is, why are we ranking it this way."
    """
    ordered = sorted(criteria, key=lambda c: c.priority)
    parts: list[str] = []

    for c in ordered:
        criterion = c.criterion
        if criterion == "COST":
            reduction = candidate.cost_reduction_percent
            if reduction and reduction > 0:
                parts.append(
                    f"{reduction:g}% lower total cost than the current BOM"
                )
            elif reduction and reduction < 0:
                parts.append(
                    f"{abs(reduction):g}% higher total cost than the current BOM"
                )
            else:
                parts.append(
                    f"total cost {candidate.cost.total_cost:g} "
                    f"{candidate.cost.currency}"
                )
        elif criterion == "REWORK_CYCLES":
            if baseline_reprocess_cycles is not None:
                parts.append(
                    f"{candidate.avg_reprocess_cycles:g} reprocess cycles "
                    f"vs {baseline_reprocess_cycles:g} on the current BOM"
                )
            else:
                parts.append(
                    f"{candidate.avg_reprocess_cycles:g} average reprocess cycles"
                )
        elif criterion == "RECENCY":
            if candidate.last_used_date:
                parts.append(f"last used {candidate.last_used_date[:10]}")
        elif criterion == "PRODUCTION_LINE":
            if candidate.production_line:
                parts.append(f"run on line {candidate.production_line}")
        elif criterion == "SEASON":
            if candidate.season:
                parts.append(f"historically run in {candidate.season}")
        elif criterion == "EFFICIENCY":
            parts.append(f"{candidate.total_operation_hours:g} operation hours")

    parts.append(
        f"used {candidate.occurrence_count} "
        f"time{'s' if candidate.occurrence_count != 1 else ''} historically"
    )

    if not candidate.feasible:
        parts.append(f"NOT feasible: {'; '.join(candidate.violations)}")

    return ", ".join(parts)
