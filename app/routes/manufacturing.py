"""Placeholder API endpoints for manufacturing master data.

These endpoints return hard-coded / dummy data so the API surface matches
the architecture discussion. In later phases they will be backed by real
SAP adapters and canonical master data.
"""
import uuid
from fastapi import APIRouter, Depends, Query
from app.dependencies import CurrentUser, require_page_access
from app.schemas import (
    MaterialMaster,
    Recipe,
    ProductionVersion,
    ProductionFeasibilityRequest,
    ProductionFeasibilityResponse,
    ProductionPathCandidate,
    ScenarioRequest,
    ScenarioResponse,
)


router = APIRouter(prefix="/api", tags=["Manufacturing Master Data"])


# Dummy data stores
DUMMY_MATERIALS = [
    {
        "material": "RM-001",
        "material_type": "RAW",
        "base_unit": "KG",
        "description": "Raw Material One",
        "plant": "P01",
        "mrp_controller": "MRP-01",
        "mrp_type": "PD",
        "lead_time_days": 5,
        "material_cost": 2.5,
        "currency": "USD",
        "status": "ACTIVE",
        "source_terminology": "MARA",
        "source_system": "SAP",
    },
    {
        "material": "RM-002",
        "material_type": "RAW",
        "base_unit": "KG",
        "description": "Raw Material Two",
        "plant": "P01",
        "mrp_controller": "MRP-02",
        "mrp_type": "PD",
        "lead_time_days": 7,
        "material_cost": 3.0,
        "currency": "USD",
        "status": "ACTIVE",
        "source_terminology": "MARA",
        "source_system": "SAP",
    },
    {
        "material": "F-100",
        "material_type": "FINISHED",
        "base_unit": "KG",
        "description": "Finished Product One",
        "plant": "P01",
        "mrp_controller": "MRP-01",
        "mrp_type": "PD",
        "lead_time_days": 10,
        "material_cost": 0.0,
        "currency": "USD",
        "status": "ACTIVE",
        "source_terminology": "MARA",
        "source_system": "SAP",
    },
]

DUMMY_RECIPE = {
    "recipe_id": "R-100",
    "recipe_name": "Recipe for F-100",
    "plant": "P01",
    "source_terminology": "Recipe",
    "source_system": "SAP",
    "operations": [
        {
            "operation_number": 10,
            "work_center": "WC-001",
            "description": "Mixing",
            "operational_cost": 50.0,
            "setup_time": 0.5,
            "run_time": 1.0,
        },
        {
            "operation_number": 20,
            "work_center": "WC-002",
            "description": "Packing",
            "operational_cost": 25.0,
            "setup_time": 0.25,
            "run_time": 0.5,
        },
    ],
}

DUMMY_PRODUCTION_VERSIONS = [
    {
        "production_version_id": "PV-100-A",
        "plant": "P01",
        "material": "F-100",
        "bom_id": "BOM-100-A",
        "recipe_id": "R-100",
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "status": "ACTIVE",
    },
    {
        "production_version_id": "PV-100-B",
        "plant": "P01",
        "material": "F-100",
        "bom_id": "BOM-100-B",
        "recipe_id": "R-100",
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "status": "ACTIVE",
    },
]


@router.get("/materials", response_model=list[MaterialMaster])
async def list_materials(
    plant: str = Query(default="P01"),
    user: CurrentUser = Depends(require_page_access("/data")),
):
    """Return dummy material master data."""
    return [MaterialMaster(**m) for m in DUMMY_MATERIALS if m["plant"] == plant]


@router.get("/materials/{material}", response_model=MaterialMaster)
async def get_material(
    material: str,
    plant: str = Query(default="P01"),
    user: CurrentUser = Depends(require_page_access("/data")),
):
    """Return a single dummy material master record."""
    for m in DUMMY_MATERIALS:
        if m["material"] == material and m["plant"] == plant:
            return MaterialMaster(**m)
    return MaterialMaster(
        material=material,
        material_type="UNKNOWN",
        base_unit="KG",
        description="Not found in dummy data",
        plant=plant,
        mrp_controller="",
        mrp_type="",
        lead_time_days=0,
        material_cost=0.0,
        status="UNKNOWN",
    )


@router.get("/recipes/{recipe_id}", response_model=Recipe)
async def get_recipe(
    recipe_id: str,
    user: CurrentUser = Depends(require_page_access("/data")),
):
    """Return a dummy recipe / process definition."""
    if recipe_id == DUMMY_RECIPE["recipe_id"]:
        return Recipe(**DUMMY_RECIPE)
    return Recipe(
        recipe_id=recipe_id,
        recipe_name="Unknown Recipe",
        plant="P01",
        source_terminology="Recipe",
        source_system="SAP",
        operations=[],
    )


@router.get("/production-versions", response_model=list[ProductionVersion])
async def list_production_versions(
    material: str = Query(...),
    plant: str = Query(default="P01"),
    user: CurrentUser = Depends(require_page_access("/data")),
):
    """Return dummy production versions for a finished material."""
    return [
        ProductionVersion(**pv)
        for pv in DUMMY_PRODUCTION_VERSIONS
        if pv["material"] == material and pv["plant"] == plant
    ]


@router.post("/production-feasibility", response_model=ProductionFeasibilityResponse)
async def production_feasibility(
    request: ProductionFeasibilityRequest,
    user: CurrentUser = Depends(require_page_access("/analyze")),
):
    """Placeholder production feasibility check."""
    candidate = ProductionPathCandidate(
        candidate_id="pv-path-001",
        production_version=ProductionVersion(**DUMMY_PRODUCTION_VERSIONS[0]),
        bom_components=[
            {
                "material": "RM-001",
                "quantity": 500.0,
                "unit": "KG",
                "scrap_percent": 0.0,
            },
            {
                "material": "RM-002",
                "quantity": 200.0,
                "unit": "KG",
                "scrap_percent": 0.0,
            },
        ],
        recipe=Recipe(**DUMMY_RECIPE),
        material_cost=2500.0,
        operational_cost=75.0,
        total_cost=2575.0,
        feasibility="FEASIBLE",
    )
    return ProductionFeasibilityResponse(
        material=request.material,
        plant=request.plant,
        quantity=request.quantity,
        feasible=True,
        candidate_paths=[candidate],
        constraints_passed=["MATERIAL_ACTIVE", "BOM_VALID", "PV_VALID"],
        constraints_failed=[],
    )


@router.post("/scenario", response_model=ScenarioResponse)
async def run_scenario(
    request: ScenarioRequest,
    user: CurrentUser = Depends(require_page_access("/analyze")),
):
    """Placeholder scenario / what-if endpoint."""
    candidate = ProductionPathCandidate(
        candidate_id="pv-path-002",
        production_version=ProductionVersion(**DUMMY_PRODUCTION_VERSIONS[1]),
        bom_components=[
            {
                "material": "RM-001",
                "quantity": 500.0,
                "unit": "KG",
                "scrap_percent": 0.0,
            },
            {
                "material": "RM-003",
                "quantity": 200.0,
                "unit": "KG",
                "scrap_percent": 0.0,
            },
        ],
        recipe=Recipe(**DUMMY_RECIPE),
        material_cost=2450.0,
        operational_cost=75.0,
        total_cost=2525.0,
        feasibility=request.changes.get("feasibility", "FEASIBLE"),
    )
    return ScenarioResponse(
        scenario_id=str(uuid.uuid4()),
        base_decision_id=request.base_decision_id,
        changed_assumptions=request.changes,
        candidate_paths=[candidate],
        recommendation="Candidate path with RM-003 is feasible and slightly cheaper under the new assumptions.",
    )
