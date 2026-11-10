"""POST /api/explain — generate an explanation for a decision/candidate."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Decision
from app.schemas import ExplainRequest, ExplainResponse
from app.config import get_settings
from app.dependencies import get_current_user, CurrentUser

router = APIRouter(prefix="/api", tags=["Explain"])

logger = logging.getLogger(__name__)


@router.post("/explain", response_model=ExplainResponse)
async def explain_decision(
    request: ExplainRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Generate an explanation for a decision.
    With OPENAI_API_KEY configured: LLM narration of the stored record.
    Without a key: explicit rule-based mode rendering the stored record.
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

    # Single OpenAI-compatible provider path. If OPENAI_API_KEY is set the
    # call must succeed — a failure raises 502 with the real error rather
    # than silently serving rule-based text. Rule-based runs ONLY when no
    # key is configured (explicit Phase 0 mode).
    if settings.OPENAI_API_KEY:
        explanation, tokens = await _openai_explain(decision, target_candidate, settings)
        model_used = f"openai/{settings.OPENAI_MODEL}"
    else:
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


def _fmt_num(v) -> str:
    """Format a stored numeric value compactly — 3dp for floats, raw otherwise."""
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def _rule_based_explain(decision: Decision, candidate: dict) -> str:
    """Render a structured explanation straight from the stored record.

    Shape-agnostic: candidates are written either by Analyze BOM
    (ComponentScore + scores dict) or the Production Persona (ComponentDetail
    + cost + criterion_values). Only fields actually present in the stored
    JSON are rendered — no invented defaults, no assumed units, no fabricated
    numbers.
    """
    components = candidate.get("components") or []
    scores = candidate.get("scores") or {}
    criterion_values = candidate.get("criterion_values") or {}
    total_score = candidate.get("total_score")
    feasible = candidate.get("feasible")
    violations = candidate.get("violations") or []
    cost = candidate.get("cost") or {}
    operations = candidate.get("operations") or []

    lines = [f"## Recommendation for {decision.request_material} at Plant {decision.request_plant}"]
    # Decision.request_quantity has no unit column — print the number alone.
    lines.append(f"**Quantity requested:** {decision.request_quantity}")
    if decision.request_due_date:
        lines.append(f"**Due date:** {decision.request_due_date}")
    if candidate.get("rank"):
        lines.append(f"**Rank:** {candidate['rank']}")

    # Provenance — render only what the record actually carries.
    if candidate.get("source_type"):
        lines.append(f"**Source type:** {candidate['source_type']}")
    source_orders = candidate.get("source_orders") or []
    if source_orders:
        lines.append("**Source orders:** " + ", ".join(str(o) for o in source_orders))
    if candidate.get("occurrence_count") is not None:
        lines.append(f"**Historical usage:** {candidate['occurrence_count']} recorded production run(s)")
    if candidate.get("last_used_date"):
        lines.append(f"**Last used:** {candidate['last_used_date']}")
    for label, key in (
        ("Production version", "production_version"),
        ("Recipe", "recipe_id"),
        ("Production line", "production_line"),
        ("Alternative BOM", "alternative_bom"),
        ("Season", "season"),
    ):
        if candidate.get(key):
            lines.append(f"**{label}:** {candidate[key]}")
    if candidate.get("avg_cycles"):
        lines.append(f"**Average cycles:** {_fmt_num(candidate['avg_cycles'])}")
    if candidate.get("avg_reprocess_cycles"):
        lines.append(f"**Average reprocess cycles:** {_fmt_num(candidate['avg_reprocess_cycles'])}")

    lines.append("")
    if feasible is True:
        lines.append("### Feasibility: FEASIBLE")
    elif feasible is False:
        lines.append("### Feasibility: NOT FEASIBLE")
    else:
        lines.append("### Feasibility: not recorded")
    if violations:
        lines.append("**Violations:**")
        lines.extend(f"- {v}" for v in violations)
    if candidate.get("legal_approval_required"):
        lines.append("**Legal approval is required for this path.**")

    if components:
        lines.append("")
        lines.append("### Components")
        for comp in components:
            # ComponentScore uses 'material'/'quantity'; ComponentDetail uses
            # 'component'/'planned_quantity'/'actual_quantity' — take what exists.
            name = comp.get("component") or comp.get("material") or comp.get("id") or "component"
            qty = next(
                (comp[k] for k in ("planned_quantity", "quantity", "actual_quantity") if comp.get(k) is not None),
                None,
            )
            parts = [str(name)]
            if qty is not None:
                q = _fmt_num(qty)
                if comp.get("unit"):
                    q += f" {comp['unit']}"
                parts.append(f"qty {q}")
            if comp.get("scrap_percent"):
                parts.append(f"scrap {comp['scrap_percent']}%")
            if comp.get("unit_cost"):
                parts.append(f"unit cost {_fmt_num(comp['unit_cost'])}")
            if comp.get("extended_cost"):
                parts.append(f"extended cost {_fmt_num(comp['extended_cost'])}")
            if comp.get("available_stock") is not None:
                parts.append(f"available stock {_fmt_num(comp['available_stock'])}")
            lines.append("- " + " · ".join(parts))

    if isinstance(cost, dict) and (
        cost.get("total_cost") or cost.get("material_cost") or cost.get("operational_cost")
    ):
        currency = cost.get("currency") or ""
        lines.append("")
        lines.append("### Cost")
        if cost.get("total_cost"):
            lines.append(f"- **Total:** {_fmt_num(cost['total_cost'])} {currency}".rstrip())
        if cost.get("material_cost"):
            lines.append(f"- Material: {_fmt_num(cost['material_cost'])} {currency}".rstrip())
        if cost.get("operational_cost"):
            lines.append(f"- Operational: {_fmt_num(cost['operational_cost'])} {currency}".rstrip())
    if candidate.get("cost_reduction_percent") is not None:
        lines.append(f"- **Cost reduction:** {candidate['cost_reduction_percent']}%")

    if scores or total_score is not None:
        lines.append("")
        lines.append("### Scores")
        if total_score is not None:
            lines.append(f"- **Total score:** {_fmt_num(total_score)}")
        for key, val in scores.items():
            lines.append(f"- **{key.replace('_', ' ').title()}:** {_fmt_num(val)}")
    elif criterion_values:
        lines.append("")
        lines.append("### Criterion values")
        for key, val in criterion_values.items():
            lines.append(f"- **{key.replace('_', ' ').title()}:** {_fmt_num(val)}")

    if operations:
        lines.append("")
        lines.append(f"### Operations ({len(operations)})")
        if candidate.get("total_operation_hours"):
            lines.append(f"**Total operation hours:** {_fmt_num(candidate['total_operation_hours'])}")
        for op in operations:
            desc = op.get("description") or f"Operation {op.get('operation_number', '?')}"
            op_parts = [desc]
            if op.get("work_center"):
                op_parts.append(op["work_center"])
            if op.get("operation_cost"):
                op_parts.append(f"cost {_fmt_num(op['operation_cost'])}")
            lines.append("- " + " · ".join(str(p) for p in op_parts))

    lines.append("")
    lines.append("### Rationale")
    if candidate.get("rank_reason"):
        lines.append(str(candidate["rank_reason"]))
    elif feasible is True:
        lines.append("No constraint violations are recorded for this path.")
    elif feasible is False:
        lines.append("This path has recorded constraint violations and is not recommended.")

    return "\n".join(lines)


def _build_explain_prompt(decision: Decision, candidate: dict) -> str:
    # Pass the stored record verbatim — the request has no unit column and
    # candidate shapes differ (Analyze BOM vs Production Persona), so the
    # model must reason over actual fields rather than assumed ones.
    return f"""You are an expert manufacturing advisor explaining a stored BOM decision record.

Decision request:
- Material: {decision.request_material}
- Plant: {decision.request_plant}
- Quantity: {decision.request_quantity}
- Due date: {decision.request_due_date}
- Status: {decision.status}
- Recommended candidate id: {decision.recommended_candidate_id}

Candidate record (verbatim JSON — these are the stored facts):
{json.dumps(candidate, default=str, indent=2)}

Explain why this production path is ranked where it is, in clear concise
language a production planner can understand. Describe only what the record
shows — if a field is absent, do not infer it. Do NOT invent scores, costs,
components, or other candidates."""


async def _openai_explain(decision: Decision, candidate: dict, settings) -> tuple[str, int]:
    """Generate explanation via the configured OpenAI-compatible endpoint.

    Returns (explanation_text, total_tokens_reported_by_provider). Any
    SDK/network failure raises HTTP 502 with the underlying error — a
    configured provider that fails must be visible, never silently replaced
    by rule-based output.
    """
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            # Empty -> api.openai.com; set for on-prem / Azure / any
            # OpenAI-compatible host.
            base_url=settings.OPENAI_BASE_URL or None,
        )

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You explain manufacturing BOM decisions. Be concise and factual."},
                {"role": "user", "content": _build_explain_prompt(decision, candidate)},
            ],
            max_tokens=500,
            temperature=0,
            seed=12345,
        )
        content = response.choices[0].message.content
        if not content:
            raise HTTPException(status_code=502, detail="LLM returned an empty explanation")
        # Real usage count from the provider — never estimated locally.
        usage = getattr(response, "usage", None)
        tokens = usage.total_tokens if usage is not None else 0
        return content, tokens
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("LLM explanation call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM explanation failed: {exc}")
