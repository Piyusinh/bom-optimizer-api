"""Read-only tool registry for the AI interaction layer.

Every tool here is:
  1. Read-only - no tool mutates any table. Approve/Reject/Override/Adopt
     remain human-only actions via their existing REST endpoints; the agent
     cannot call them (R21/R26 guardrail).
  2. Backed by rules, not vibes - each tool queries the same canonical
     tables and deterministic engines the UI uses. The LLM never invents
     numbers; it only narrates what a tool call returned.
  3. Described the way the OpenAI SDK / function-calling spec expects: a
     short imperative `name`, a one-sentence `description` a model can use
     to decide *when* to call it, and a strict JSON Schema `parameters`
     block. This is the "registered tools" the lead asked about.

Reference: https://platform.openai.com/docs/guides/function-calling
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Decision, CanonicalBomHeader, OrderChain
from app.routes.explain import _rule_based_explain

# ─── Tool schemas (OpenAI function-calling format) ─────────────────────────

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_recent_decisions",
            "description": (
                "List the most recent BOM decisions computed by the deterministic "
                "engine, optionally filtered by plant or material. Use this when "
                "the user asks what decisions have been made or wants recent "
                "analyses for a plant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plant": {"type": "string", "description": "Optional SAP plant code, e.g. '1200'."},
                    "material": {"type": "string", "description": "Optional material number to filter by."},
                    "limit": {"type": "integer", "description": "Max rows to return (default 10, max 50).", "default": 10},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_decision_detail",
            "description": (
                "Get the full record for one decision by id: request parameters, "
                "every ranked candidate with scores, the recommended candidate, "
                "and its current approval status. Call this before explaining or "
                "comparing a specific decision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "decision_id": {"type": "string", "description": "The decision id, e.g. from list_recent_decisions."},
                },
                "required": ["decision_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_candidate",
            "description": (
                "Produce a structured, rule-based explanation of why a specific "
                "candidate within a decision was ranked where it was: components, "
                "scores, feasibility, violations. Calls the SAME deterministic "
                "explainer the UI's 'Explain recommendation' button uses - it does "
                "not invent reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "decision_id": {"type": "string", "description": "The decision id."},
                    "candidate_id": {
                        "type": "string",
                        "description": "Candidate id within that decision. Omit to explain the top-ranked candidate.",
                    },
                },
                "required": ["decision_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_data_freshness",
            "description": (
                "Report how current the ingested BOM data is per object type "
                "(production/planned/sales order): counts of fresh (<30 days), "
                "stale (30-90 days), and very stale (>90 days) records. Use this "
                "when the user asks if the data is up to date or needs re-ingest."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plant": {"type": "string", "description": "Optional plant code to scope the report."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trace_order_chain",
            "description": (
                "Trace the sales order -> planned order -> production order chain "
                "for a given order id, including whether each linked order has an "
                "ingested BOM. Use this when the user asks how a production order "
                "relates to a sales or planned order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Any order id in the chain (SO, planned, or production order number)."},
                },
                "required": ["order_id"],
            },
        },
    },
]

_TOOL_NAMES = {spec["function"]["name"] for spec in TOOL_SPECS}


# ─── Tool implementations (pure read, no writes, no LLM calls inside) ──────

async def _list_recent_decisions(db: AsyncSession, plant: str | None = None, material: str | None = None, limit: int = 10) -> dict:
    limit = max(1, min(int(limit or 10), 50))
    stmt = select(Decision).order_by(Decision.created_at.desc())
    if plant:
        stmt = stmt.where(Decision.request_plant == plant)
    if material:
        stmt = stmt.where(Decision.request_material == material)
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {
        "count": len(rows),
        "decisions": [
            {
                "id": d.id,
                "material": d.request_material,
                "plant": d.request_plant,
                "quantity": d.request_quantity,
                "status": d.status,
                "candidates_count": len(d.candidates) if d.candidates else 0,
                "recommended_candidate_id": d.recommended_candidate_id,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ],
    }


async def _get_decision_detail(db: AsyncSession, decision_id: str) -> dict:
    decision = await db.get(Decision, decision_id)
    if not decision:
        return {"error": f"Decision {decision_id} not found"}
    return {
        "id": decision.id,
        "request_material": decision.request_material,
        "request_plant": decision.request_plant,
        "request_quantity": decision.request_quantity,
        "status": decision.status,
        "recommended_candidate_id": decision.recommended_candidate_id,
        "candidates": decision.candidates or [],
        "approved_by": decision.approved_by,
        "approved_at": decision.approved_at.isoformat() if decision.approved_at else None,
    }


async def _explain_candidate(db: AsyncSession, decision_id: str, candidate_id: str | None = None) -> dict:
    decision = await db.get(Decision, decision_id)
    if not decision:
        return {"error": f"Decision {decision_id} not found"}
    candidates = decision.candidates or []
    target = None
    if candidate_id:
        target = next((c for c in candidates if c.get("id") == candidate_id), None)
    elif candidates:
        target = candidates[0]
    if not target:
        return {"error": "Candidate not found"}
    return {"explanation": _rule_based_explain(decision, target)}


async def _get_data_freshness(db: AsyncSession, plant: str | None = None) -> dict:
    from datetime import datetime, timedelta
    from sqlalchemy import func, case

    now = datetime.utcnow()
    fresh_30d = now - timedelta(days=30)
    fresh_90d = now - timedelta(days=90)

    stmt = select(
        CanonicalBomHeader.object_type,
        func.count().label("total"),
        func.sum(case((CanonicalBomHeader.last_change_datetime >= fresh_30d, 1), else_=0)).label("fresh_30d"),
        func.sum(
            case(
                (
                    (CanonicalBomHeader.last_change_datetime >= fresh_90d)
                    & (CanonicalBomHeader.last_change_datetime < fresh_30d),
                    1,
                ),
                else_=0,
            )
        ).label("stale_30_90d"),
        func.sum(case((CanonicalBomHeader.last_change_datetime < fresh_90d, 1), else_=0)).label("very_stale_90d"),
    )
    if plant:
        stmt = stmt.where(CanonicalBomHeader.plant == plant)
    stmt = stmt.group_by(CanonicalBomHeader.object_type)
    result = await db.execute(stmt)

    out = []
    for object_type, total, f30, s3090, vs90 in result.all():
        total = total or 0
        out.append({
            "object_type": object_type,
            "total": total,
            "fresh_30d": f30 or 0,
            "stale_30_90d": s3090 or 0,
            "very_stale_90d": vs90 or 0,
        })
    return {"plant": plant or "all", "as_of": now.isoformat(), "freshness_by_type": out}


async def _trace_order_chain(db: AsyncSession, order_id: str) -> dict:
    from sqlalchemy import or_

    stmt = select(OrderChain).where(
        or_(
            OrderChain.production_order == order_id,
            OrderChain.planned_order == order_id,
            OrderChain.sales_order == order_id,
        )
    )
    result = await db.execute(stmt)
    chains = result.scalars().all()
    if not chains:
        return {"error": f"No order chain found containing order {order_id}"}
    return {
        "order_id": order_id,
        "chains": [
            {
                "sales_order": c.sales_order,
                "planned_order": c.planned_order,
                "production_order": c.production_order,
                "material": c.material,
                "plant": c.plant,
                "quantity": c.quantity,
                "chain_stage": c.chain_stage,
            }
            for c in chains
        ],
    }


_DISPATCH = {
    "list_recent_decisions": _list_recent_decisions,
    "get_decision_detail": _get_decision_detail,
    "explain_candidate": _explain_candidate,
    "get_data_freshness": _get_data_freshness,
    "trace_order_chain": _trace_order_chain,
}


async def call_tool(name: str, arguments: dict, db: AsyncSession) -> dict:
    """Dispatch a tool call by name. Returns a JSON-serialisable dict.

    Unknown tool names return an error payload rather than raising, so a
    single bad/hallucinated tool call from the model can't crash the chat
    turn - it just gets fed back as an error the model can react to.
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool '{name}'. Registered tools: {sorted(_TOOL_NAMES)}"}
    try:
        return await fn(db, **arguments)
    except TypeError as exc:
        return {"error": f"Invalid arguments for '{name}': {exc}"}
