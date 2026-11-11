"""Candidate Generation Engine — builds scored candidate responses."""
from app.schemas import CandidateResponse, ComponentScore, ScoringProfileInput
from app.decision_core.scoring_engine import score_candidate
from app.decision_core.rule_engine import evaluate_rules


def build_candidate(
    idx: int,
    raw_candidate: dict,
    constraints,
    known_components: set[str],
    scoring_profile: ScoringProfileInput,
    total_runs: int,
) -> CandidateResponse:
    """
    Build a fully scored and ranked CandidateResponse from a raw candidate.
    """
    components = raw_candidate["components"]

    # 1. Apply hard rules / constraints
    feasible, violations = evaluate_rules(components, constraints, known_components)

    # 2. Historical frequency
    occurrence_count = raw_candidate["occurrence_count"]
    historical_freq = occurrence_count / total_runs if total_runs > 0 else 0.5

    # 3. Score the candidate
    scores = score_candidate(components, scoring_profile, historical_freq)

    candidate_id = f"cand-{idx+1:03d}"
    source_orders = raw_candidate.get("source_orders", [])

    return CandidateResponse(
        id=candidate_id,
        source_order=source_orders[0] if source_orders else None,
        source_orders=source_orders,
        occurrence_count=occurrence_count,
        source_type=raw_candidate["object_type"],
        components=[
            ComponentScore(
                material=c["component"],
                quantity=c["quantity"],
                unit=c["unit"],
                scrap_percent=c["scrap_percent"],
            )
            for c in components
        ],
        feasible=feasible,
        violations=violations,
        scores=scores,
        total_score=scores["total"],
        rank=0,
    )
