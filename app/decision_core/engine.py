"""Decision Engine — orchestrates explosion, constraints, scoring, ranking."""
import uuid
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import CanonicalBomItem, Decision
from app.schemas import (
    AnalyzeBomRequest,
    AnalyzeBomResponse,
    CandidateResponse,
    ComponentScore,
)
from app.decision_core.explosion_engine import explode_bom
from app.decision_core.constraint_engine import evaluate_constraints
from app.decision_core.scoring_engine import score_candidate


class DecisionEngine:
    """
    The Decision Core — deterministic computation.
    AI does not compute; it asks the Decision Core.
    """

    async def analyze(
        self, request: AnalyzeBomRequest, db: AsyncSession
    ) -> AnalyzeBomResponse:
        request_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())

        # Step 1: BOM Explosion — find all candidate production paths
        # (historically-identical combinations are merged; occurrence_count
        # reflects how many historical runs used that exact combination)
        raw_candidates, total_runs = await explode_bom(request.material, request.plant, db)

        if not raw_candidates:
            # Return empty result if no BOMs found
            decision = Decision(
                id=request_id,
                request_material=request.material,
                request_plant=request.plant,
                request_quantity=request.quantity,
                request_due_date=datetime.fromisoformat(request.due_date) if request.due_date else None,
                constraints=request.constraints.model_dump(),
                scoring_profile=request.scoring_profile.model_dump(),
                candidates=[],
                status="computed",
                trace={"trace_id": trace_id, "candidates_found": 0},
            )
            db.add(decision)
            await db.commit()

            return AnalyzeBomResponse(
                request_id=request_id,
                material=request.material,
                plant=request.plant,
                quantity=request.quantity,
                candidates_count=0,
                candidates=[],
                trace_id=trace_id,
                computed_at=datetime.utcnow().isoformat(),
            )

        # Step 2: Get all known components for constraint validation
        all_components_stmt = select(CanonicalBomItem.component).distinct()
        result = await db.execute(all_components_stmt)
        known_components = {row[0] for row in result.all()}

        # Step 3: Evaluate constraints & score each candidate
        scored_candidates: list[CandidateResponse] = []
        for idx, cand in enumerate(raw_candidates):
            # Constraint evaluation
            feasible, violations = evaluate_constraints(
                cand["components"], request.constraints, known_components
            )

            # Historical frequency: how often this EXACT combination recurred
            # across all historical runs for this material+plant.
            occurrence_count = cand["occurrence_count"]
            historical_freq = occurrence_count / total_runs if total_runs > 0 else 0.5

            # Scoring
            scores = score_candidate(
                cand["components"], request.scoring_profile, historical_freq
            )

            candidate_id = f"cand-{idx+1:03d}"
            source_orders = cand.get("source_orders", [])
            scored_candidates.append(
                CandidateResponse(
                    id=candidate_id,
                    source_order=source_orders[0] if source_orders else None,
                    source_orders=source_orders,
                    occurrence_count=occurrence_count,
                    source_type=cand["object_type"],
                    components=[
                        ComponentScore(
                            material=c["component"],
                            quantity=c["quantity"],
                            unit=c["unit"],
                            scrap_percent=c["scrap_percent"],
                        )
                        for c in cand["components"]
                    ],
                    feasible=feasible,
                    violations=violations,
                    scores=scores,
                    total_score=scores["total"],
                    rank=0,  # Will be set after sorting
                )
            )

        # Step 4: Rank — feasible first, then by total score descending
        scored_candidates.sort(
            key=lambda c: (not c.feasible, -c.total_score)
        )
        for rank, cand in enumerate(scored_candidates, 1):
            cand.rank = rank

        # Step 5: Persist decision
        recommended_id = scored_candidates[0].id if scored_candidates else None
        decision = Decision(
            id=request_id,
            request_material=request.material,
            request_plant=request.plant,
            request_quantity=request.quantity,
            request_due_date=datetime.fromisoformat(request.due_date) if request.due_date else None,
            constraints=request.constraints.model_dump(),
            scoring_profile=request.scoring_profile.model_dump(),
            candidates=[c.model_dump() for c in scored_candidates],
            recommended_candidate_id=recommended_id,
            status="computed",
            trace={
                "trace_id": trace_id,
                "total_historical_runs": total_runs,
                "distinct_candidates_found": len(raw_candidates),
                "feasible_count": sum(1 for c in scored_candidates if c.feasible),
                "infeasible_count": sum(1 for c in scored_candidates if not c.feasible),
            },
        )
        db.add(decision)
        await db.commit()

        return AnalyzeBomResponse(
            request_id=request_id,
            material=request.material,
            plant=request.plant,
            quantity=request.quantity,
            candidates_count=len(scored_candidates),
            candidates=scored_candidates,
            trace_id=trace_id,
            computed_at=datetime.utcnow().isoformat(),
        )
