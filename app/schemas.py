"""Pydantic schemas for API request/response contracts."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ─── Auth ───────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class UserInfo(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    role_label: str
    allowed_pages: list[str]


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserInfo


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
    candidates: list[CandidateResponse]
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
