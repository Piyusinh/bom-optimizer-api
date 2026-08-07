"""Scoring Engine — normalizes criteria and computes weighted scores."""
from app.schemas import ScoringProfileInput


def score_candidate(
    components: list[dict],
    scoring_profile: ScoringProfileInput,
    historical_frequency: float = 0.5,
) -> dict[str, float]:
    """
    Score a candidate BOM based on multiple criteria.
    In Phase 0, we use deterministic heuristics — the same inputs must
    always produce the same scores/ranking (Decision Core is deterministic;
    AI never computes, it only explains). In later phases, these heuristics
    are replaced by real cost data, lead-time predictions, and supplier
    risk models.
    """
    # Cost score: fewer components and lower scrap = better (heuristic)
    total_scrap = sum(c.get("scrap_percent", 0) for c in components)
    component_count = len(components)
    cost_score = max(0.0, min(1.0, 1.0 - (total_scrap / 100) - (component_count * 0.05)))

    # Lead time score: fewer components = faster assembly (heuristic)
    lead_time_score = max(0.3, min(1.0, 1.0 - (component_count * 0.08)))

    # Risk score: known components with low scrap = lower risk
    risk_score = max(0.2, min(1.0, 1.0 - (total_scrap / 50)))

    # Historical success: how often this combination was used
    historical_score = min(1.0, historical_frequency)

    # Weighted total
    total = (
        scoring_profile.cost_weight * cost_score
        + scoring_profile.lead_time_weight * lead_time_score
        + scoring_profile.risk_weight * risk_score
        + scoring_profile.historical_success_weight * historical_score
    )

    return {
        "cost": round(cost_score, 3),
        "lead_time": round(lead_time_score, 3),
        "risk": round(risk_score, 3),
        "historical_success": round(historical_score, 3),
        "total": round(total, 3),
    }
