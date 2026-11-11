"""Production Persona Engine — the Phase 1 orchestrator.

Implements the RTM 'Production' sheet algorithm end to end:

    0. Resolve exactly one anchor: Material OR Planned Order OR Process Order
    1. Season from the planned/process order start date
    2. Order gives material, quantity, BOM, recipe, production start date
    3. Raw material grades considered via material master
    4. Actual qty = recipe qty + goods issues (movement type 261)
    5. Raw material availability for planned-order simulation
    6. Production line considered on the shop floor

    Output A: ranked summary with the reason for each rank
    Output B: Adopt (staged in Phase 1, no SAP write)

AI does not decide anything here. Every number is deterministic, so the same
input always produces the same ranking — a hard requirement for audit sign-off.
"""
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CanonicalBomHeader, CanonicalMaterial, Decision
from app.schemas import (
    CostBreakdown,
    ProductionAnalyzeRequest,
    ProductionAnalyzeResponse,
    ProductionBaseline,
    ProductionCandidate,
)
from app.decision_core import (
    availability_engine,
    consumption_engine,
    cost_engine,
    cycle_engine,
    explosion_engine,
    material_engine,
    ranking_engine,
    recipe_engine,
    rule_engine,
    season_engine,
)

DEFAULT_LOOKBACK_MONTHS = 24


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class ProductionPersonaEngine:
    """Deterministic ranking of historical production paths for a planner."""

    async def analyze(
        self, request: ProductionAnalyzeRequest, db: AsyncSession
    ) -> ProductionAnalyzeResponse:
        request_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())

        # Step 0 — resolve the anchor into (material, quantity, start date)
        material, quantity, anchor_start = await self._resolve_anchor(request, db)
        quantity = request.quantity or quantity

        # Step 1 — date window. Defaults to the last 24 months because the
        # planner "may be interested in the production that happened in the
        # last two years", not a decade ago.
        date_from, date_to = self._resolve_window(request)

        # Step 2 — historical candidates, filtered by window and line
        raw_candidates, total_runs = await explosion_engine.explode_bom_filtered(
            material,
            request.plant,
            db,
            date_from=date_from,
            date_to=date_to,
            production_line=request.production_line,
        )

        if not raw_candidates:
            return self._empty_response(
                request_id, trace_id, request, material, quantity
            )

        # Step 3 — material master for every component in every candidate
        all_components = {
            c["component"] for cand in raw_candidates for c in cand["components"]
        }
        masters = await material_engine.load_materials(
            all_components, request.plant, db, active_only=False
        )

        known_components = set(masters) | all_components

        # Step 4 — build a fully costed candidate for each distinct path
        candidates: list[ProductionCandidate] = []
        for index, raw in enumerate(raw_candidates):
            candidate = await self._build_candidate(
                index,
                raw,
                material,
                request,
                quantity,
                anchor_start,
                masters,
                known_components,
                db,
            )
            candidates.append(candidate)

        # Step 5 — baseline is the most-used historical path, which is what the
        # planner would have run by default. Everything is measured against it.
        baseline = self._build_baseline(candidates)
        for candidate in candidates:
            candidate.cost_reduction_percent = cost_engine.reduction_percent(
                baseline.cost.total_cost, candidate.cost.total_cost
            )

        # Step 6 — criterion values, then rank by the planner's priority order
        for candidate in candidates:
            candidate.criterion_values = self._criterion_values(
                candidate, request, anchor_start
            )

        ranking_engine.rank_by_criteria(candidates, request.ranking_criteria)

        for candidate in candidates:
            candidate.rank_reason = ranking_engine.build_rank_reason(
                candidate,
                request.ranking_criteria,
                baseline.avg_reprocess_cycles,
            )

        # Step 7 — persist as an immutable, replayable decision record
        await self._persist(
            request_id, trace_id, request, material, quantity, candidates, total_runs, db
        )

        return ProductionAnalyzeResponse(
            request_id=request_id,
            plant=request.plant,
            anchor=request.anchor,
            material=material,
            quantity=quantity,
            production_line=request.production_line,
            ranking_criteria=request.ranking_criteria,
            baseline=baseline,
            candidates_count=len(candidates),
            candidates=candidates,
            trace_id=trace_id,
            computed_at=datetime.utcnow().isoformat(),
        )

    # ── Anchor resolution ─────────────────────────────────────────────────────

    async def _resolve_anchor(
        self, request: ProductionAnalyzeRequest, db: AsyncSession
    ) -> tuple[str, float, datetime | None]:
        """Turn the anchor into the material, quantity and start date.

        RTM: "from the above selection criteria, make one of Material, process
        order or planned order [mandatory]" and "Planned/Process order will have
        material, quantity BOM, recipe & production start date".
        """
        anchor = request.anchor

        if anchor.type == "MATERIAL":
            return anchor.value, 1000.0, None

        object_type = (
            "PLANNED_ORDER" if anchor.type == "PLANNED_ORDER" else "PRODUCTION_ORDER"
        )
        stmt = (
            select(CanonicalBomHeader)
            .where(CanonicalBomHeader.object_id == anchor.value)
            .where(CanonicalBomHeader.object_type == object_type)
            .where(CanonicalBomHeader.plant == request.plant)
            .limit(1)
        )
        result = await db.execute(stmt)
        header = result.scalar_one_or_none()

        if header is None:
            # Anchor not found — fall back to treating the value as a material
            # so the planner still gets a result instead of an error page.
            return anchor.value, 1000.0, None

        if object_type == "PRODUCTION_ORDER" and header.status and header.status != "Created":
            # RTM: "Make sure the Process Order is not released" — production
            # has already started, so its BOM is locked and it cannot be a
            # valid anchor for a BOM recommendation.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Process order {anchor.value} has status "
                    f"'{header.status}' — only orders that have not been "
                    "released can be used as an anchor."
                ),
            )

        return (
            header.material,
            float(header.quantity or 1000.0),
            header.order_start_date or header.valid_from,
        )

    def _resolve_window(
        self, request: ProductionAnalyzeRequest
    ) -> tuple[datetime | None, datetime | None]:
        if request.date_range:
            date_from = _parse_date(request.date_range.from_date)
            date_to = _parse_date(request.date_range.to_date)
            if date_from or date_to:
                return date_from, date_to

        now = datetime.utcnow()
        return now - timedelta(days=DEFAULT_LOOKBACK_MONTHS * 30), now

    # ── Candidate construction ────────────────────────────────────────────────

    async def _build_candidate(
        self,
        index: int,
        raw: dict,
        material: str,
        request: ProductionAnalyzeRequest,
        quantity: float,
        anchor_start: datetime | None,
        masters: dict[str, CanonicalMaterial],
        known_components: set[str],
        db: AsyncSession,
    ) -> ProductionCandidate:
        components = rule_engine.stock_components(raw["components"])
        orders = raw.get("source_orders") or []
        run_dates = [d for d in raw.get("run_dates") or [] if d]

        # Scale the historical BOM from its base quantity to the requested one.
        base_quantity = float(raw.get("base_quantity") or 1.0) or 1.0
        quantity_factor = quantity / base_quantity

        # Step 4 of the algorithm — actual consumption from movement type 261
        actual = await consumption_engine.average_consumption_per_run(orders, db)

        component_details = cost_engine.build_component_details(
            components, masters, actual, quantity_factor
        )

        # Recipe drives operational cost and operation hours
        recipe = None
        if raw.get("recipe_id"):
            recipe = await recipe_engine.load_recipe(
                raw["recipe_id"], request.plant, db
            )
        operations = recipe_engine.to_operation_details(recipe)

        cost = cost_engine.build_cost_breakdown(
            component_details, operations, quantity_factor
        )

        avg_cycles, avg_reprocess = await cycle_engine.cycle_metrics(orders, db)

        feasible, violations = rule_engine.evaluate_rules(
            components, request.constraints, known_components
        )
        violations += rule_engine.evaluate_master_data_rules(components, masters)
        violations += availability_engine.availability_violations(component_details)

        last_used = max(run_dates) if run_dates else None
        production_line = raw.get("production_line") or (
            recipe_engine.work_centers(operations)[0]
            if recipe_engine.work_centers(operations)
            else None
        )

        return ProductionCandidate(
            id=f"cand-{index + 1}",
            material=material,
            plant=request.plant,
            production_version=raw.get("production_version"),
            alternative_bom=raw.get("alternative_bom"),
            recipe_id=raw.get("recipe_id"),
            production_line=production_line,
            source_type=raw.get("object_type", "PRODUCTION_ORDER"),
            source_orders=orders,
            occurrence_count=raw.get("occurrence_count", 1),
            last_used_date=last_used.isoformat() if last_used else None,
            season=season_engine.season_of(last_used),
            avg_cycles=avg_cycles,
            avg_reprocess_cycles=avg_reprocess,
            total_operation_hours=recipe_engine.total_operation_hours(operations),
            cost=cost,
            components=component_details,
            operations=operations,
            feasible=len(violations) == 0,
            violations=violations,
            legal_approval_required=rule_engine.requires_legal_approval(
                components, masters
            ),
        )

    def _build_baseline(
        self, candidates: list[ProductionCandidate]
    ) -> ProductionBaseline:
        """The path the planner would run today — the most frequently used one."""
        most_used = max(candidates, key=lambda c: c.occurrence_count)
        return ProductionBaseline(
            source="MOST_USED_HISTORICAL_BOM",
            source_order=most_used.source_orders[0] if most_used.source_orders else None,
            production_version=most_used.production_version,
            avg_cycles=most_used.avg_cycles,
            avg_reprocess_cycles=most_used.avg_reprocess_cycles,
            cost=most_used.cost,
            components=most_used.components,
        )

    # ── Ranking inputs ────────────────────────────────────────────────────────

    def _criterion_values(
        self,
        candidate: ProductionCandidate,
        request: ProductionAnalyzeRequest,
        anchor_start: datetime | None,
    ) -> dict[str, float]:
        """Numeric value per criterion. Direction is handled by the ranker."""
        last_used = _parse_date(candidate.last_used_date)
        recency_days = (
            (datetime.utcnow() - last_used).days if last_used else 10_000
        )

        return {
            "COST": candidate.cost.total_cost,
            # Negated so that MAX direction prefers the smallest age.
            "RECENCY": float(-recency_days),
            "REWORK_CYCLES": candidate.avg_reprocess_cycles,
            "PRODUCTION_LINE": (
                1.0
                if request.production_line
                and candidate.production_line == request.production_line
                else 0.0
            ),
            "SEASON": season_engine.season_match_score(
                [last_used] if last_used else [], anchor_start or datetime.utcnow()
            ),
            "EFFICIENCY": candidate.total_operation_hours,
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _persist(
        self,
        request_id: str,
        trace_id: str,
        request: ProductionAnalyzeRequest,
        material: str,
        quantity: float,
        candidates: list[ProductionCandidate],
        total_runs: int,
        db: AsyncSession,
    ) -> None:
        decision = Decision(
            id=request_id,
            request_material=material,
            request_plant=request.plant,
            request_quantity=quantity,
            constraints=request.constraints.model_dump(),
            scoring_profile={
                "ranking_criteria": [
                    c.model_dump() for c in request.ranking_criteria
                ],
                "anchor": request.anchor.model_dump(),
                "production_line": request.production_line,
            },
            candidates=[c.model_dump() for c in candidates],
            recommended_candidate_id=candidates[0].id if candidates else None,
            status="computed",
            trace={
                "trace_id": trace_id,
                "persona": "PRODUCTION",
                "total_historical_runs": total_runs,
                "distinct_candidates_found": len(candidates),
                "feasible_count": sum(1 for c in candidates if c.feasible),
            },
        )
        db.add(decision)
        await db.commit()

    def _empty_response(
        self,
        request_id: str,
        trace_id: str,
        request: ProductionAnalyzeRequest,
        material: str,
        quantity: float,
    ) -> ProductionAnalyzeResponse:
        return ProductionAnalyzeResponse(
            request_id=request_id,
            plant=request.plant,
            anchor=request.anchor,
            material=material,
            quantity=quantity,
            production_line=request.production_line,
            ranking_criteria=request.ranking_criteria,
            baseline=ProductionBaseline(cost=CostBreakdown()),
            candidates_count=0,
            candidates=[],
            trace_id=trace_id,
            computed_at=datetime.utcnow().isoformat(),
        )
