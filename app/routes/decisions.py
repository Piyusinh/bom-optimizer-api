"""GET /api/decisions — retrieve decision history."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import AuditLog, Decision
from app.schemas import (
    DecisionActionRequest,
    DecisionActionResponse,
    DecisionDetail,
)
from app.dependencies import require_page_access, CurrentUser

router = APIRouter(prefix="/api", tags=["Decisions"])


@router.get("/decisions")
async def list_decisions(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/decisions")),
):
    """List all decisions, most recent first."""
    stmt = select(Decision).order_by(Decision.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    decisions = result.scalars().all()

    count_stmt = select(func.count()).select_from(Decision)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    return {
        "total": total,
        "items": [
            {
                "id": d.id,
                "material": d.request_material,
                "plant": d.request_plant,
                "quantity": d.request_quantity,
                "status": d.status,
                "candidates_count": len(d.candidates) if d.candidates else 0,
                "recommended": d.recommended_candidate_id,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
    }


@router.get("/decisions/{decision_id}", response_model=DecisionDetail)
async def get_decision(
    decision_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/decisions")),
):
    """Get full decision detail including all candidates and trace."""
    stmt = select(Decision).where(Decision.id == decision_id)
    result = await db.execute(stmt)
    decision = result.scalar_one_or_none()

    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")

    # Candidates are immutable audit JSON — two shapes exist (Analyze BOM's
    # CandidateResponse and Production Persona's ProductionCandidate), so the
    # detail endpoint returns them verbatim rather than re-validating.
    candidates = decision.candidates or []

    return DecisionDetail(
        id=decision.id,
        request_material=decision.request_material,
        request_plant=decision.request_plant,
        request_quantity=decision.request_quantity,
        request_due_date=decision.request_due_date.isoformat() if decision.request_due_date else None,
        constraints=decision.constraints or {},
        scoring_profile=decision.scoring_profile or {},
        candidates=candidates,
        recommended_candidate_id=decision.recommended_candidate_id,
        status=decision.status,
        trace=decision.trace or {},
        created_at=decision.created_at.isoformat() if decision.created_at else "",
    )


@router.post("/decisions/{decision_id}/action", response_model=DecisionActionResponse)
async def decision_action(
    decision_id: str,
    request: DecisionActionRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/decisions")),
):
    """Record the planner's verdict on a computed decision.

    Phase 1 plan (DIC-X05): every recommendation supports APPROVE, REJECT or
    OVERRIDE — each with a reason and the acting user — written to the audit
    log and stamped back onto the immutable Decision record.

      approve   → accepts recommended_candidate_id as-is
      reject    → marks the decision rejected; reason required
      override  → planner picks a different candidate; override_candidate_id
                  must be one of the candidates in this decision
    """
    decision = await db.get(Decision, decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")

    actor = request.actor or (user.username or user.email)

    if request.action in ("reject", "override") and not (request.reason or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"A reason is required when action is '{request.action}'",
        )

    if request.action == "override":
        candidate_ids = {c.get("id") for c in (decision.candidates or [])}
        if request.override_candidate_id not in candidate_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "override_candidate_id must be one of the decision's "
                    f"candidates ({len(candidate_ids)} available)"
                ),
            )
        decision.recommended_candidate_id = request.override_candidate_id

    decision.status = {
        "approve": "approved",
        "reject": "rejected",
        "override": "overridden",
    }[request.action]
    if request.action == "approve":
        decision.approved_at = datetime.utcnow()
        decision.approved_by = actor

    db.add(
        AuditLog(
            event_type=f"decision_{request.action}d",
            entity_type="decision",
            entity_id=decision_id,
            actor=actor,
            details={
                "action": request.action,
                "reason": request.reason,
                "override_candidate_id": request.override_candidate_id,
            },
        )
    )
    await db.commit()

    return DecisionActionResponse(
        decision_id=decision_id,
        status=decision.status,
        actor=actor,
        reason=request.reason,
        recommended_candidate_id=decision.recommended_candidate_id,
        updated_at=datetime.utcnow().isoformat(),
    )
