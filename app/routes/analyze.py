"""POST /api/analyze-bom — the primary decision endpoint."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.schemas import AnalyzeBomRequest, AnalyzeBomResponse
from app.decision_core import DecisionEngine
from app.dependencies import require_page_access, CurrentUser

router = APIRouter(prefix="/api", tags=["Decision"])
engine = DecisionEngine()


@router.post("/analyze-bom", response_model=AnalyzeBomResponse)
async def analyze_bom(
    request: AnalyzeBomRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/analyze")),
):
    """
    Given a demand signal (material, plant, quantity, due date),
    determine feasible production paths, score them, and return ranked recommendations.
    """
    return await engine.analyze(request, db)
