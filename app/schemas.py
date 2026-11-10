"""Pydantic schemas for API request/response contracts."""
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
from app.security import validate_password_strength


# ─── Auth ───────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class MenuPageOut(BaseModel):
    id: str
    key: str
    label: str
    icon: Optional[str] = None
    sort_order: int = 0
    children: list["MenuPageOut"] = Field(default_factory=list)


MenuPageOut.model_rebuild()


class UserInfo(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    role_label: str
    is_super_admin: bool = False
    allowed_pages: list[str]
    menu: list[MenuPageOut] = Field(default_factory=list)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserInfo


# ─── Admin — Users, Roles, Pages/Menus (RBAC console) ──────────────────────────

class RoleOut(BaseModel):
    id: str
    name: str
    label: str
    description: Optional[str] = None
    is_system: bool
    user_count: int = 0
    page_ids: list[str] = Field(default_factory=list)


class RoleCreate(BaseModel):
    name: str
    label: str
    description: Optional[str] = None
    page_ids: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    page_ids: Optional[list[str]] = None


class PageOut(BaseModel):
    id: str
    key: str
    label: str
    icon: Optional[str] = None
    parent_id: Optional[str] = None
    sort_order: int = 0
    is_menu: bool = True
    is_active: bool = True
    is_system: bool = False


class PageCreate(BaseModel):
    key: str
    label: str
    icon: Optional[str] = None
    parent_id: Optional[str] = None
    sort_order: int = 0
    is_menu: bool = True


class PageUpdate(BaseModel):
    key: Optional[str] = None
    label: Optional[str] = None
    icon: Optional[str] = None
    parent_id: Optional[str] = None
    sort_order: Optional[int] = None
    is_menu: Optional[bool] = None
    is_active: Optional[bool] = None


class UserAdminOut(BaseModel):
    id: str
    email: str
    username: Optional[str] = None
    full_name: str
    role_id: Optional[str] = None
    role_name: Optional[str] = None
    role_label: Optional[str] = None
    is_super_admin: bool = False
    is_active: bool = True
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None


class UserCreate(BaseModel):
    email: str
    username: Optional[str] = None
    full_name: str
    password: str
    role_id: Optional[str] = None
    is_super_admin: bool = False
    is_active: bool = True

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        return validate_password_strength(v)


class UserUpdate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    role_id: Optional[str] = None
    is_super_admin: Optional[bool] = None
    is_active: Optional[bool] = None


class UserPasswordReset(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        return validate_password_strength(v)


# ─── Analyze BOM Request/Response ──────────────────────────────────────────────

class ConstraintsInput(BaseModel):
    must_use_registered_components: bool = True
    avoid_components: list[str] = Field(default_factory=list)
    max_components: Optional[int] = None
    min_quantity_per_component: Optional[float] = None


class ScoringProfileInput(BaseModel):
    cost_weight: float = 0.5
    lead_time_weight: float = 0.3
    risk_weight: float = 0.1
    historical_success_weight: float = 0.1


class AnalyzeBomRequest(BaseModel):
    material: str
    plant: str
    quantity: float = 1000.0
    due_date: Optional[str] = None
    constraints: ConstraintsInput = Field(default_factory=ConstraintsInput)
    scoring_profile: ScoringProfileInput = Field(default_factory=ScoringProfileInput)


class ComponentScore(BaseModel):
    material: str
    quantity: float
    unit: str = "KG"
    scrap_percent: float = 0.0


class CandidateResponse(BaseModel):
    id: str
    source_order: Optional[str] = None
    source_orders: list[str] = Field(default_factory=list)
    occurrence_count: int = 1
    source_type: str = "PRODUCTION_ORDER"
    components: list[ComponentScore]
    feasible: bool
    violations: list[str] = Field(default_factory=list)
    scores: dict[str, float]
    total_score: float
    rank: int


class AnalyzeBomResponse(BaseModel):
    request_id: str
    material: str
    plant: str
    quantity: float
    candidates_count: int
    candidates: list[CandidateResponse]
    trace_id: str
    computed_at: str


# ─── Explain ────────────────────────────────────────────────────────────────────

class ExplainRequest(BaseModel):
    decision_id: str
    candidate_id: Optional[str] = None


class ExplainResponse(BaseModel):
    decision_id: str
    candidate_id: Optional[str] = None
    explanation: str
    model_used: str = "rule-based"
    tokens_used: int = 0


# ─── Decision Detail ───────────────────────────────────────────────────────────

class DecisionDetail(BaseModel):
    id: str
    request_material: str
    request_plant: str
    request_quantity: float
    request_due_date: Optional[str] = None
    constraints: dict
    scoring_profile: dict
    # Candidates are stored as immutable audit JSON. They may be
    # CandidateResponse (Analyze BOM) or ProductionCandidate (Production
    # Persona) shaped — pass them through verbatim instead of re-validating.
    candidates: list[dict]
    recommended_candidate_id: Optional[str] = None
    status: str
    trace: dict
    created_at: str


# ─── Data Ingestion ────────────────────────────────────────────────────────────

class IngestResponse(BaseModel):
    status: str
    records_ingested: int
    source_file: str
    object_type: str
    message: str


# ─── Dashboard Stats ───────────────────────────────────────────────────────────

class DashboardStats(BaseModel):
    total_materials: int
    total_bom_headers: int
    total_bom_items: int
    total_order_chains: int
    total_decisions: int
    total_production_orders: int
    total_planned_orders: int
    total_sales_orders: int


# ─── Material List ─────────────────────────────────────────────────────────────

class MaterialSummary(BaseModel):
    material: str
    plant: str
    bom_count: int
    latest_order: Optional[str] = None


# ─── Manufacturing Master Data (Phase 0 Placeholders) ───────────────────────────

class MaterialMaster(BaseModel):
    material: str
    material_type: str
    base_unit: str
    description: str
    plant: str
    mrp_controller: str
    mrp_type: str
    lead_time_days: int
    material_cost: float
    currency: str = "USD"
    status: str = "ACTIVE"
    source_terminology: str = "MARA"
    source_system: str = "SAP"


class Operation(BaseModel):
    operation_number: int
    work_center: str
    description: str
    operational_cost: float
    setup_time: float
    run_time: float


class Recipe(BaseModel):
    recipe_id: str
    recipe_name: str
    plant: str
    source_terminology: str = "Recipe"
    source_system: str = "SAP"
    operations: list[Operation]


class ProductionVersion(BaseModel):
    production_version_id: str
    plant: str
    material: str
    bom_id: str
    recipe_id: str
    valid_from: str
    valid_to: str
    status: str = "ACTIVE"


class ProductionPathCandidate(BaseModel):
    candidate_id: str
    production_version: ProductionVersion
    bom_components: list[ComponentScore]
    recipe: Recipe
    material_cost: float
    operational_cost: float
    total_cost: float
    feasibility: str


class ProductionFeasibilityRequest(BaseModel):
    material: str
    plant: str
    quantity: float
    due_date: Optional[str] = None


class ProductionFeasibilityResponse(BaseModel):
    material: str
    plant: str
    quantity: float
    feasible: bool
    candidate_paths: list[ProductionPathCandidate]
    constraints_passed: list[str]
    constraints_failed: list[str]


class ScenarioRequest(BaseModel):
    base_decision_id: str
    changes: dict[str, str]


class ScenarioResponse(BaseModel):
    scenario_id: str
    base_decision_id: str
    changed_assumptions: dict[str, str]
    candidate_paths: list[ProductionPathCandidate]
    recommendation: str


# ─── Phase 1 — Production Persona ──────────────────────────────────────────────
#
# Contract is driven by the RTM sheets 'Production' and 'Prod Output Persona'.
# The planner picks a plant plus ONE anchor, optionally narrows by production
# line and date range, then ORDERS the ranking criteria by priority.

AnchorType = Literal["MATERIAL", "PLANNED_ORDER", "PROCESS_ORDER"]

RankingCriterion = Literal[
    "COST",             # material + operational cost, minimise
    "RECENCY",          # how recently the path was used, maximise
    "REWORK_CYCLES",    # average reprocess cycles, minimise
    "PRODUCTION_LINE",  # prefer paths run on the selected line
    "SEASON",           # prefer paths run in the same season
    "EFFICIENCY",       # operation hours / lead time, minimise
]


class ProductionAnchor(BaseModel):
    """Exactly one of material / planned order / process order."""
    type: AnchorType
    value: str


class DateRange(BaseModel):
    from_date: Optional[str] = Field(default=None, alias="from")
    to_date: Optional[str] = Field(default=None, alias="to")

    model_config = {"populate_by_name": True}


class RankingCriterionInput(BaseModel):
    criterion: RankingCriterion
    priority: int = 1


class ProductionAnalyzeRequest(BaseModel):
    plant: str
    anchor: ProductionAnchor
    production_line: Optional[str] = None
    date_range: Optional[DateRange] = None
    quantity: Optional[float] = None
    ranking_criteria: list[RankingCriterionInput] = Field(
        default_factory=lambda: [
            RankingCriterionInput(criterion="COST", priority=1),
            RankingCriterionInput(criterion="REWORK_CYCLES", priority=2),
            RankingCriterionInput(criterion="RECENCY", priority=3),
        ]
    )
    constraints: ConstraintsInput = Field(default_factory=ConstraintsInput)


class OperationDetail(BaseModel):
    operation_number: int
    description: Optional[str] = None
    work_center: Optional[str] = None
    setup_time_hours: float = 0.0
    run_time_hours: float = 0.0
    teardown_time_hours: float = 0.0
    cost_per_hour: float = 0.0
    operation_cost: float = 0.0


class ComponentDetail(BaseModel):
    component: str
    description: Optional[str] = None
    material_type: Optional[str] = None
    grade: Optional[str] = None  # raw-material grade (RTM algorithm item 3)
    planned_quantity: float
    actual_quantity: Optional[float] = None
    unit: str = "KG"
    scrap_percent: float = 0.0
    unit_cost: float = 0.0
    extended_cost: float = 0.0
    available_stock: Optional[float] = None


class CostBreakdown(BaseModel):
    material_cost: float = 0.0
    operational_cost: float = 0.0
    total_cost: float = 0.0
    currency: str = "USD"


class ProductionCandidate(BaseModel):
    """One ranked production path — mirrors the Prod Output Persona row."""
    id: str
    rank: int = 0
    material: str
    plant: str
    production_version: Optional[str] = None
    alternative_bom: Optional[str] = None
    recipe_id: Optional[str] = None
    production_line: Optional[str] = None
    source_type: str = "PRODUCTION_ORDER"
    source_orders: list[str] = Field(default_factory=list)
    occurrence_count: int = 1
    last_used_date: Optional[str] = None
    season: Optional[str] = None
    avg_cycles: float = 0.0
    avg_reprocess_cycles: float = 0.0
    total_operation_hours: float = 0.0
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    cost_reduction_percent: Optional[float] = None
    components: list[ComponentDetail] = Field(default_factory=list)
    operations: list[OperationDetail] = Field(default_factory=list)
    feasible: bool = True
    violations: list[str] = Field(default_factory=list)
    legal_approval_required: bool = False
    criterion_values: dict[str, float] = Field(default_factory=dict)
    rank_reason: str = ""


class ProductionBaseline(BaseModel):
    """The static/current BOM the proposal is compared against."""
    source: str = "STATIC_BOM"
    source_order: Optional[str] = None
    production_version: Optional[str] = None
    avg_cycles: float = 0.0
    avg_reprocess_cycles: float = 0.0
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    components: list[ComponentDetail] = Field(default_factory=list)


class ProductionAnalyzeResponse(BaseModel):
    request_id: str
    plant: str
    anchor: ProductionAnchor
    material: str
    quantity: float
    production_line: Optional[str] = None
    ranking_criteria: list[RankingCriterionInput]
    baseline: Optional[ProductionBaseline] = None
    candidates_count: int
    candidates: list[ProductionCandidate]
    trace_id: str
    computed_at: str


# ─── Pickers ───────────────────────────────────────────────────────────────────

class ProductionLineOption(BaseModel):
    work_center: str
    description: Optional[str] = None
    plant: str
    run_count: int = 0


class OrderOption(BaseModel):
    order_id: str
    material: str
    plant: str
    quantity: float
    start_date: Optional[str] = None
    status: Optional[str] = None


# ─── BOM Comparison ────────────────────────────────────────────────────────────

class ComponentDiff(BaseModel):
    component: str
    change_type: str  # ADDED | REMOVED | QUANTITY_CHANGED | UNCHANGED
    baseline_quantity: Optional[float] = None
    proposed_quantity: Optional[float] = None
    unit: str = "KG"
    cost_impact: float = 0.0


class BomCompareRequest(BaseModel):
    decision_id: str
    candidate_id: str


class BomCompareResponse(BaseModel):
    decision_id: str
    candidate_id: str
    baseline: ProductionBaseline
    proposed: ProductionCandidate
    diffs: list[ComponentDiff]
    total_cost_delta: float
    total_cost_delta_percent: float


# ─── Adopt / Decision Action ───────────────────────────────────────────────────

class AdoptRequest(BaseModel):
    decision_id: str
    candidate_id: str
    target_order: Optional[str] = None
    reason: Optional[str] = None


class AdoptResponse(BaseModel):
    adoption_id: str
    decision_id: str
    candidate_id: str
    status: str  # STAGED — Phase 1 records intent only, no SAP write
    target_order: Optional[str] = None
    message: str


class DecisionActionRequest(BaseModel):
    action: Literal["approve", "reject", "override"]
    reason: Optional[str] = None
    actor: Optional[str] = None
    override_candidate_id: Optional[str] = None


class DecisionActionResponse(BaseModel):
    decision_id: str
    status: str
    actor: Optional[str] = None
    reason: Optional[str] = None
    recommended_candidate_id: Optional[str] = None
    updated_at: str
