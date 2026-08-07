"""Data ingestion and query routes."""
import os
import shutil
import tempfile
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, distinct
from app.database import get_db
from app.models import CanonicalBomHeader, CanonicalBomItem, OrderChain, Decision
from app.schemas import IngestResponse, DashboardStats, MaterialSummary
from app.connectors.excel_connector import (
    ingest_production_order_bom,
    ingest_planned_order_bom,
    ingest_sales_order_bom,
    ingest_order_linkage,
)
from app.connectors.demo_data_generator import generate_demo_workbooks
from app.dependencies import require_page_access, get_current_user, CurrentUser

router = APIRouter(prefix="/api/data", tags=["Data"])


@router.post("/upload/{data_type}", response_model=IngestResponse)
async def upload_data(
    data_type: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/data")),
):
    """
    Upload Excel file for ingestion.
    data_type: production_order | planned_order | sales_order | linkage
    """
    if data_type not in ("production_order", "planned_order", "sales_order", "linkage"):
        raise HTTPException(status_code=400, detail=f"Invalid data_type: {data_type}")

    # Save uploaded file to temp
    suffix = os.path.splitext(file.filename or "data.xlsx")[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        if data_type == "production_order":
            result = await ingest_production_order_bom(tmp_path, db)
        elif data_type == "planned_order":
            result = await ingest_planned_order_bom(tmp_path, db)
        elif data_type == "sales_order":
            result = await ingest_sales_order_bom(tmp_path, db)
        elif data_type == "linkage":
            result = await ingest_order_linkage(tmp_path, db)
        else:
            raise HTTPException(status_code=400, detail="Unknown type")
    finally:
        os.unlink(tmp_path)

    return IngestResponse(
        status=result["status"],
        records_ingested=result["records_ingested"],
        source_file=file.filename or "unknown",
        object_type=result["object_type"],
        message=f"Successfully ingested {result['records_ingested']} records",
    )


@router.post("/ingest-local")
async def ingest_local_files(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/data")),
):
    """
    Ingest all Excel files from the configured sample-data folder.
    Falls back to a packaged `sample_data` directory next to the API package,
    then to the repo docs/Project DIC teams/ folder. This makes the endpoint
    deployable to Azure or any other environment: just set the
    SAMPLES_DIR environment variable or place the sample files next to the API.

    If no sample folder is found, the endpoint returns a friendly error telling
    the caller to use the demo generator or upload files instead.
    """
    from app.config import get_settings

    settings = get_settings()
    configured = getattr(settings, "SAMPLES_DIR", None)

    candidates = []
    if configured:
        candidates.append(configured)
    candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sample_data")))

    base_path = None
    for path in candidates:
        if os.path.isdir(path):
            base_path = path
            break

    if base_path is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No sample-data folder found. "
                "Set SAMPLES_DIR, place sample files in api/app/sample_data, "
                "or use the 'Generate & Ingest Demo Data' button for a guaranteed demo."
            ),
        )

    results = []

    files_map = {
        "Chemical_Production_Order_BOM_Data.xlsx": ingest_production_order_bom,
        "Chemical_Planned_Order_BOM_Data.xlsx": ingest_planned_order_bom,
        "Chemical_Sales_Order_BOM_Data.xlsx": ingest_sales_order_bom,
        "Chemical_SO_PlanOrd_ProdOrd_Linkage.xlsx": ingest_order_linkage,
    }

    for filename, ingest_fn in files_map.items():
        file_path = os.path.join(base_path, filename)
        if os.path.isfile(file_path):
            try:
                result = await ingest_fn(file_path, db)
                results.append({"file": filename, **result})
            except Exception as e:
                results.append({"file": filename, "status": "error", "message": str(e)})
        else:
            results.append({"file": filename, "status": "skipped", "message": "File not found"})

    return {"results": results}


@router.post("/generate-demo")
async def generate_and_ingest_demo_data(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_page_access("/data")),
):
    """
    Generate a small, deterministic, guaranteed-clean synthetic dataset and
    ingest it through the same connectors used for real files. Every demo
    material (DEMO-F00x) is produced 10 times per plant across 3 distinct
    component recipes with skewed frequency — guarantees Analyze BOM always
    returns multiple ranked candidates with a clear historical-frequency
    story, regardless of the quality/availability of real client files.
    """
    demo_dir = os.path.join(tempfile.gettempdir(), "bom_demo_data")
    paths = generate_demo_workbooks(demo_dir)

    results = []
    for key, ingest_fn in [
        ("production_order", ingest_production_order_bom),
        ("planned_order", ingest_planned_order_bom),
        ("sales_order", ingest_sales_order_bom),
        ("linkage", ingest_order_linkage),
    ]:
        result = await ingest_fn(paths[key], db)
        results.append({"file": os.path.basename(paths[key]), **result})

    return {"results": results}


@router.get("/stats", response_model=DashboardStats)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Dashboard statistics."""
    materials_stmt = select(func.count(distinct(CanonicalBomHeader.material)))
    headers_stmt = select(func.count()).select_from(CanonicalBomHeader)
    items_stmt = select(func.count()).select_from(CanonicalBomItem)
    chains_stmt = select(func.count()).select_from(OrderChain)
    decisions_stmt = select(func.count()).select_from(Decision)

    prod_stmt = select(func.count()).select_from(CanonicalBomHeader).where(
        CanonicalBomHeader.object_type == "PRODUCTION_ORDER"
    )
    plan_stmt = select(func.count()).select_from(CanonicalBomHeader).where(
        CanonicalBomHeader.object_type == "PLANNED_ORDER"
    )
    sales_stmt = select(func.count()).select_from(CanonicalBomHeader).where(
        CanonicalBomHeader.object_type == "SALES_ORDER"
    )

    return DashboardStats(
        total_materials=(await db.execute(materials_stmt)).scalar() or 0,
        total_bom_headers=(await db.execute(headers_stmt)).scalar() or 0,
        total_bom_items=(await db.execute(items_stmt)).scalar() or 0,
        total_order_chains=(await db.execute(chains_stmt)).scalar() or 0,
        total_decisions=(await db.execute(decisions_stmt)).scalar() or 0,
        total_production_orders=(await db.execute(prod_stmt)).scalar() or 0,
        total_planned_orders=(await db.execute(plan_stmt)).scalar() or 0,
        total_sales_orders=(await db.execute(sales_stmt)).scalar() or 0,
    )


@router.get("/materials")
async def list_materials(
    plant: str = Query(default=None),
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List distinct materials with BOM counts."""
    stmt = select(
        CanonicalBomHeader.material,
        CanonicalBomHeader.plant,
        func.count(CanonicalBomHeader.id).label("bom_count"),
    ).group_by(CanonicalBomHeader.material, CanonicalBomHeader.plant)

    if plant:
        stmt = stmt.where(CanonicalBomHeader.plant == plant)

    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    rows = result.all()

    return [
        MaterialSummary(material=r[0], plant=r[1], bom_count=r[2])
        for r in rows
    ]
