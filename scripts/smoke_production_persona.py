"""End-to-end smoke test for the Phase 1 Production Persona pipeline.

Runs against a throwaway SQLite database so it never touches dev data:

    python scripts/smoke_production_persona.py

Verifies, in order:
  1. schema creates
  2. demo BOM data ingests
  3. master data (materials / PVs / recipes / movements / rework) generates
  4. production analyze returns ranked candidates with real cost numbers
  5. changing the criteria order actually changes the ranking
"""
import asyncio
import os
import sys
import tempfile

# Point the app at a temporary database BEFORE importing anything from app.
DB_PATH = os.path.join(tempfile.gettempdir(), "bom_smoke_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"
# Not 'development' — that turns on SQLAlchemy echo and floods the output.
os.environ["ENVIRONMENT"] = "test"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.connectors.demo_data_generator import generate_demo_workbooks  # noqa: E402
from app.connectors.excel_connector import (  # noqa: E402
    ingest_order_linkage,
    ingest_planned_order_bom,
    ingest_production_order_bom,
    ingest_sales_order_bom,
)
from app.connectors.master_data_generator import generate_master_data  # noqa: E402
from app.database import async_session, init_db  # noqa: E402
from app.decision_core import ProductionPersonaEngine  # noqa: E402
from app.schemas import (  # noqa: E402
    ProductionAnalyzeRequest,
    ProductionAnchor,
    RankingCriterionInput,
)

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        failures.append(label)


async def main() -> int:
    print("\n1. Creating schema")
    await init_db()
    print("   schema created")

    print("\n2. Ingesting demo BOM data")
    demo_dir = os.path.join(tempfile.gettempdir(), "bom_smoke_demo_data")
    paths = generate_demo_workbooks(demo_dir)

    async with async_session() as db:
        for key, fn in [
            ("production_order", ingest_production_order_bom),
            ("planned_order", ingest_planned_order_bom),
            ("sales_order", ingest_sales_order_bom),
            ("linkage", ingest_order_linkage),
        ]:
            result = await fn(paths[key], db)
            print(f"   {key}: {result['records_ingested']} records")

    print("\n3. Generating master data")
    async with async_session() as db:
        counts = await generate_master_data(db)
    for key, value in counts.items():
        print(f"   {key}: {value}")
    check(counts["materials"] > 0, "material master generated")
    check(counts["recipes"] > 0, "recipes generated")
    check(counts["production_versions"] > 0, "production versions generated")
    check(counts["goods_movements"] > 0, "goods movements (261) generated")

    print("\n4. Running production analyze (COST first)")
    engine = ProductionPersonaEngine()
    request = ProductionAnalyzeRequest(
        plant="1000",
        anchor=ProductionAnchor(type="MATERIAL", value="DEMO-F001"),
        quantity=1000.0,
        ranking_criteria=[
            RankingCriterionInput(criterion="COST", priority=1),
            RankingCriterionInput(criterion="REWORK_CYCLES", priority=2),
        ],
    )

    async with async_session() as db:
        response = await engine.analyze(request, db)

    print(f"   candidates: {response.candidates_count}")
    check(response.candidates_count > 0, "at least one candidate returned")

    if response.candidates:
        for c in response.candidates:
            print(
                f"   rank {c.rank}: {c.id} "
                f"cost={c.cost.total_cost:g} "
                f"(mat {c.cost.material_cost:g} + ops {c.cost.operational_cost:g}) "
                f"reprocess={c.avg_reprocess_cycles:g} "
                f"line={c.production_line} pv={c.production_version}"
            )
            print(f"      reason: {c.rank_reason}")

        top = response.candidates[0]
        check(top.cost.material_cost > 0, "material cost is computed")
        check(top.cost.operational_cost > 0, "operational cost is computed")
        check(top.recipe_id is not None, "candidate is linked to a recipe")
        check(top.production_version is not None, "candidate has a production version")
        check(bool(top.rank_reason), "rank reason is generated")
        check(len(top.components) > 0, "components are returned")
        check(len(top.operations) > 0, "operations are returned")
        check(response.baseline is not None, "baseline is returned")

        costs = [c.cost.total_cost for c in response.candidates]
        check(costs == sorted(costs), "candidates are sorted by cost ascending")

    print("\n5. Re-running with REWORK_CYCLES first")
    request_b = request.model_copy(
        update={
            "ranking_criteria": [
                RankingCriterionInput(criterion="REWORK_CYCLES", priority=1),
                RankingCriterionInput(criterion="COST", priority=2),
            ]
        }
    )
    async with async_session() as db:
        response_b = await engine.analyze(request_b, db)

    for c in response_b.candidates:
        print(
            f"   rank {c.rank}: {c.id} "
            f"reprocess={c.avg_reprocess_cycles:g} cost={c.cost.total_cost:g}"
        )

    reprocess = [c.avg_reprocess_cycles for c in response_b.candidates]
    check(
        reprocess == sorted(reprocess),
        "candidates are sorted by reprocess cycles ascending",
    )

    print("\n6. Determinism check")
    async with async_session() as db:
        response_c = await engine.analyze(request, db)
    check(
        [c.id for c in response_c.candidates] == [c.id for c in response.candidates],
        "same input produces the same ranking",
    )

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
