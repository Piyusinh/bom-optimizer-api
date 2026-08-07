"""GET /api/decisions — retrieve decision history."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import Decision
from app.schemas import DecisionDetail, CandidateResponse
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

    candidates = [CandidateResponse(**c) for c in (decision.candidates or [])]

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
