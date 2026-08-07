"""POST /api/explain — generate an explanation for a decision/candidate."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Decision
from app.schemas import ExplainRequest, ExplainResponse
from app.config import get_settings
from app.dependencies import get_current_user, CurrentUser

router = APIRouter(prefix="/api", tags=["Explain"])


@router.post("/explain", response_model=ExplainResponse)
async def explain_decision(
    request: ExplainRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Generate an explanation for a decision.
    Phase 0: Rule-based template explanation.
    Phase 2+: Azure OpenAI GPT-4o narration.
    """
    settings = get_settings()

    # Load decision
    stmt = select(Decision).where(Decision.id == request.decision_id)
    result = await db.execute(stmt)
    decision = result.scalar_one_or_none()
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")

    candidates = decision.candidates or []
    target_candidate = None

    if request.candidate_id:
        target_candidate = next(
            (c for c in candidates if c.get("id") == request.candidate_id), None
        )
    elif candidates:
        target_candidate = candidates[0]  # Top ranked

    if not target_candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Try LLM explanation if configured
    if settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_KEY:
        explanation = await _llm_explain(decision, target_candidate, settings)
        model_used = f"azure-openai/{settings.AZURE_OPENAI_DEPLOYMENT}"
        tokens = len(explanation.split()) * 2  # rough estimate
    else:
        # Phase 0: Rule-based explanation
        explanation = _rule_based_explain(decision, target_candidate)
        model_used = "rule-based"
        tokens = 0

    return ExplainResponse(
        decision_id=request.decision_id,
        candidate_id=target_candidate.get("id"),
        explanation=explanation,
        model_used=model_used,
        tokens_used=tokens,
    )


def _rule_based_explain(decision: Decision, candidate: dict) -> str:
    """Generate a structured explanation without LLM."""
    material = decision.request_material
    plant = decision.request_plant
    qty = decision.request_quantity
    components = candidate.get("components", [])
    scores = candidate.get("scores", {})
    feasible = candidate.get("feasible", True)
    violations = candidate.get("violations", [])
    source = candidate.get("source_order", "master BOM")
    occurrence_count = candidate.get("occurrence_count", 1)
    historical_score = scores.get("historical_success")

    lines = [
        f"## Recommendation for {material} at Plant {plant}",
        f"**Quantity requested:** {qty} KG",
        f"**Source:** {source} ({candidate.get('source_type', 'N/A')})",
        f"**Historical usage:** This exact component combination was used in "
        f"{occurrence_count} historical production run{'s' if occurrence_count != 1 else ''}"
        + (f" ({historical_score * 100:.0f}% of all historical runs for this material)." if historical_score is not None else "."),
        "",
        f"### Feasibility: {'✅ FEASIBLE' if feasible else '❌ NOT FEASIBLE'}",
    ]

    if violations:
        lines.append("**Violations:**")
        for v in violations:
            lines.append(f"- {v}")

    lines.append("")
    lines.append("### Components")
    lines.append("| Material | Quantity | Unit | Scrap% |")
    lines.append("|----------|----------|------|--------|")
    for comp in components:
        lines.append(
            f"| {comp['material']} | {comp['quantity']} | {comp.get('unit', 'KG')} | {comp.get('scrap_percent', 0)}% |"
        )

    lines.append("")
    lines.append("### Scores")
    for key, val in scores.items():
        lines.append(f"- **{key.replace('_', ' ').title()}:** {val:.3f}")

    lines.append("")
    lines.append("### Rationale")
    lines.append(
        f"This production path uses {len(components)} components with a total weighted score of "
        f"{scores.get('total', 0):.3f}. "
    )
    if feasible:
        lines.append("All hard constraints are satisfied. This is the recommended path.")
    else:
        lines.append("This path violates one or more constraints and is not recommended.")

    return "\n".join(lines)


async def _llm_explain(decision: Decision, candidate: dict, settings) -> str:
    """Generate explanation using Azure OpenAI."""
    try:
        from openai import AsyncAzureOpenAI

        client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION,
        )

        prompt = f"""You are an expert manufacturing advisor explaining a BOM recommendation.
        
Material: {decision.request_material}
Plant: {decision.request_plant}
Quantity: {decision.request_quantity} KG

Candidate components: {candidate.get('components', [])}
Scores: {candidate.get('scores', {})}
Feasible: {candidate.get('feasible')}
Violations: {candidate.get('violations', [])}

Explain why this production path was ranked #{candidate.get('rank', 1)} in clear, concise language 
that a production planner can understand. Include the score breakdown and any risk factors.
Do NOT invent new candidates. Only explain this one."""

        response = await client.chat.completions.create(
            model=settings.AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": "You explain manufacturing BOM decisions. Be concise and factual."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
            temperature=0.3,
        )
        return response.choices[0].message.content or _rule_based_explain(decision, candidate)
    except Exception:
        return _rule_based_explain(decision, candidate)
