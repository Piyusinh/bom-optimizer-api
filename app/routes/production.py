"""Production Persona routes — the Phase 1 decision surface.

Endpoint set is driven by the RTM 'Prod Output Persona' sheet:
  selection pickers -> analyze -> proposed recipe -> adopt
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentUser, require_page_access
from app.decision_core import ProductionPersonaEngine
from app.decision_core import bom_comparison_engine
from app.models import (
    AuditLog,
    CanonicalBomHeader,
    CanonicalProductionVersion,
    CanonicalRecipeOperation,
    Decision,
    OrderChain,
)
from app.schemas import (
    AdoptRequest,
    AdoptResponse,
    BomCompareRequest,
    BomCompareResponse,
    OrderOption,
    ProductionAnalyzeRequest,
    ProductionAnalyzeResponse,
    ProductionBaseline,
    ProductionCandidate,
    ProductionLineOption,
)

router = APIRouter(prefix="/api/production", tags=["production"])

engine = ProductionPersonaEngine()


@router.post("/analyze", response_model=ProductionAnalyzeResponse)
async def analyze_production(
    request: ProductionAnalyzeRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/production")),
):
    """Rank historical production paths for the production persona.

    Plant is mandatory; the anchor is exactly one of material, planned order or
    process order. Ranking follows the planner's ordered criteria, not fixed
    weights.
    """
    return await engine.analyze(request, db)


@router.get("/lines", response_model=list[ProductionLineOption])
async def list_production_lines(
    plant: str = Query(..., description="Plant is mandatory"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/production")),
):
    """Production lines / work centers available at a plant.

    Sourced from historical BOM headers first, then topped up with recipe work
    centers so lines that exist in master data but have no history still appear.
    """
    header_stmt = (
        select(
            CanonicalBomHeader.production_line,
            func.count(CanonicalBomHeader.id),
        )
        .where(CanonicalBomHeader.plant == plant)
        .where(CanonicalBomHeader.production_line.isnot(None))
        .group_by(CanonicalBomHeader.production_line)
    )
    result = await db.execute(header_stmt)
    lines = {
        row[0]: ProductionLineOption(
            work_center=row[0], plant=plant, run_count=int(row[1])
        )
        for row in result.all()
    }

    pv_stmt = (
        select(CanonicalProductionVersion.production_line)
        .where(CanonicalProductionVersion.plant == plant)
        .where(CanonicalProductionVersion.production_line.isnot(None))
        .distinct()
    )
    pv_result = await db.execute(pv_stmt)
    for (line,) in pv_result.all():
        lines.setdefault(
            line, ProductionLineOption(work_center=line, plant=plant, run_count=0)
        )

    op_stmt = (
        select(CanonicalRecipeOperation.work_center)
        .where(CanonicalRecipeOperation.work_center.isnot(None))
        .distinct()
    )
    op_result = await db.execute(op_stmt)
    for (line,) in op_result.all():
        lines.setdefault(
            line, ProductionLineOption(work_center=line, plant=plant, run_count=0)
        )

    return sorted(lines.values(), key=lambda l: (-l.run_count, l.work_center))


@router.get("/planned-orders", response_model=list[OrderOption])
async def list_planned_orders(
    plant: str = Query(...),
    material: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/production")),
):
    """Open planned orders — one of the three valid anchors."""
    return await _list_orders("PLANNED_ORDER", plant, material, limit, db)


@router.get("/process-orders", response_model=list[OrderOption])
async def list_process_orders(
    plant: str = Query(...),
    material: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/production")),
):
    """Process orders. RTM restricts the anchor to orders that are NOT released,
    because once production has started the BOM can no longer be changed."""
    return await _list_orders("PRODUCTION_ORDER", plant, material, limit, db)


async def _list_orders(
    object_type: str,
    plant: str,
    material: str | None,
    limit: int,
    db: AsyncSession,
) -> list[OrderOption]:
    stmt = (
        select(CanonicalBomHeader)
        .where(CanonicalBomHeader.object_type == object_type)
        .where(CanonicalBomHeader.plant == plant)
        .limit(limit)
    )
    if object_type == "PRODUCTION_ORDER":
        # RTM: "Make sure the Process Order is not released" — once released,
        # confirmed or closed, production has started and the BOM can no
        # longer be changed, so those orders are not valid anchors.
        stmt = stmt.where(CanonicalBomHeader.status == "Created")
    if material:
        stmt = stmt.where(CanonicalBomHeader.material == material)

    result = await db.execute(stmt)
    headers = result.scalars().all()

    return [
        OrderOption(
            order_id=h.object_id,
            material=h.material,
            plant=h.plant,
            quantity=float(h.quantity or 0.0),
            start_date=(
                h.order_start_date.isoformat() if h.order_start_date else None
            ),
            status=h.status,
        )
        for h in headers
    ]


@router.post("/compare", response_model=BomCompareResponse)
async def compare_static_vs_proposed(
    request: BomCompareRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/production")),
):
    """Static/current BOM vs the proposed BOM for one candidate.

    This backs the 'Proposed Recipe' button on the results table.
    """
    decision = await db.get(Decision, request.decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")

    candidates = [ProductionCandidate(**c) for c in (decision.candidates or [])]
    proposed = next((c for c in candidates if c.id == request.candidate_id), None)
    if proposed is None:
        raise HTTPException(status_code=404, detail="Candidate not found in decision")

    most_used = max(candidates, key=lambda c: c.occurrence_count)
    baseline = ProductionBaseline(
        source="MOST_USED_HISTORICAL_BOM",
        source_order=most_used.source_orders[0] if most_used.source_orders else None,
        production_version=most_used.production_version,
        avg_cycles=most_used.avg_cycles,
        avg_reprocess_cycles=most_used.avg_reprocess_cycles,
        cost=most_used.cost,
        components=most_used.components,
    )

    diffs = bom_comparison_engine.diff_component_details(
        baseline.components, proposed.components
    )
    delta, percent = bom_comparison_engine.cost_delta(
        baseline.cost.total_cost, proposed.cost.total_cost
    )

    return BomCompareResponse(
        decision_id=request.decision_id,
        candidate_id=request.candidate_id,
        baseline=baseline,
        proposed=proposed,
        diffs=diffs,
        total_cost_delta=delta,
        total_cost_delta_percent=percent,
    )


@router.get("/chain/{order_id}")
async def trace_order_chain(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/production")),
):
    """Trace the SO -> Planned Order -> Production Order chain for any order id.

    The linkage workbook (Chemical_SO_PlanOrd_ProdOrd_Linkage.xlsx) captures
    the MTS/MTO relationships; this endpoint exposes them so the planner can
    see which sales order and planned order drove a given production order —
    or vice versa — and confirm each linked object has an ingested BOM.
    """
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
        raise HTTPException(
            status_code=404,
            detail=f"No order chain found containing order {order_id}",
        )

    linked_ids: set[str] = set()
    for c in chains:
        for oid in (c.sales_order, c.planned_order, c.production_order):
            if oid:
                linked_ids.add(oid)

    bom_stmt = select(
        CanonicalBomHeader.object_id,
        CanonicalBomHeader.object_type,
        CanonicalBomHeader.status,
    ).where(CanonicalBomHeader.object_id.in_(linked_ids))
    bom_result = await db.execute(bom_stmt)
    bom_map = {row[0]: {"object_type": row[1], "status": row[2]} for row in bom_result.all()}

    # Sales-order BOM headers are keyed "SalesOrder-SalesOrderItem"; resolve
    # those composite ids too so chain coverage is accurate.
    so_pairs = {
        (c.sales_order, c.sales_order_item) for c in chains if c.sales_order
    }
    so_bom_stmt = select(
        CanonicalBomHeader.object_id,
        CanonicalBomHeader.status,
    ).where(
        CanonicalBomHeader.object_id.in_(
            [f"{so}-{item}" for so, item in so_pairs if so]
        )
    )
    so_bom_result = await db.execute(so_bom_stmt)
    so_bom_map = {row[0]: row[1] for row in so_bom_result.all()}

    return {
        "order_id": order_id,
        "chains": [
            {
                "sales_order": c.sales_order,
                "sales_order_item": c.sales_order_item,
                "planned_order": c.planned_order,
                "production_order": c.production_order,
                "material": c.material,
                "plant": c.plant,
                "quantity": c.quantity,
                "chain_stage": c.chain_stage,
                "bom_status": {
                    "sales_order": so_bom_map.get(f"{c.sales_order}-{c.sales_order_item}")
                    if c.sales_order
                    else None,
                    "planned_order": bom_map.get(c.planned_order or "", {}).get("status"),
                    "production_order": bom_map.get(c.production_order or "", {}).get("status"),
                },
            }
            for c in chains
        ],
    }


@router.post("/adopt", response_model=AdoptResponse)
async def adopt_candidate(
    request: AdoptRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/production")),
):
    """Adopt a proposed BOM/recipe.

    RTM output B says Adopt "should sync the BOM/Recipe changes from the UI
    screen to Process order in SAP". Phase 1 deliberately records the intent
    only — there is no SAP write path yet, and DIC-C09 requires explicit human
    approval before any ERP write.
    """
    decision = await db.get(Decision, request.decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")

    candidate_ids = {c.get("id") for c in (decision.candidates or [])}
    if request.candidate_id not in candidate_ids:
        raise HTTPException(status_code=404, detail="Candidate not found in decision")

    adoption_id = str(uuid.uuid4())
    db.add(
        AuditLog(
            event_type="candidate_adopted",
            entity_type="decision",
            entity_id=request.decision_id,
            details={
                "adoption_id": adoption_id,
                "candidate_id": request.candidate_id,
                "target_order": request.target_order,
                "reason": request.reason,
                "sap_write": False,
                "staged_at": datetime.utcnow().isoformat(),
            },
        )
    )
    await db.commit()

    return AdoptResponse(
        adoption_id=adoption_id,
        decision_id=request.decision_id,
        candidate_id=request.candidate_id,
        status="STAGED",
        target_order=request.target_order,
        message=(
            "Adoption staged and recorded in the audit log. "
            "SAP write-back is a Phase 2 capability."
        ),
    )
