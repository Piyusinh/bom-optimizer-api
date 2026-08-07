"""SQLAlchemy ORM models — canonical BOM, order chain, decisions, audit."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, DateTime, Boolean, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


def generate_uuid():
    return str(uuid.uuid4())


# ─── Canonical BOM ─────────────────────────────────────────────────────────────

class CanonicalBomHeader(Base):
    __tablename__ = "canonical_bom_header"

    id = Column(String, primary_key=True, default=generate_uuid)
    object_type = Column(String, nullable=False)  # MASTER | SALES_ORDER | PLANNED_ORDER | PRODUCTION_ORDER
    object_id = Column(String, nullable=False)
    material = Column(String, nullable=False, index=True)
    plant = Column(String, nullable=False, index=True)
    bom_usage = Column(String, nullable=True)  # 1=Production, 5=Sales, 3=Universal
    quantity = Column(Float, nullable=False, default=1.0)
    base_unit = Column(String, nullable=False, default="KG")
    valid_from = Column(DateTime, nullable=True)
    valid_to = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)
    source_system = Column(String, default="mock-excel")
    extracted_at = Column(DateTime, default=datetime.utcnow)
    attributes = Column(JSON, default=dict)

    items = relationship("CanonicalBomItem", back_populates="header", cascade="all, delete-orphan")


class CanonicalBomItem(Base):
    __tablename__ = "canonical_bom_item"

    id = Column(String, primary_key=True, default=generate_uuid)
    header_id = Column(String, ForeignKey("canonical_bom_header.id"), nullable=False)
    item_number = Column(Integer, nullable=True)
    component = Column(String, nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    unit = Column(String, nullable=False, default="KG")
    scrap_percent = Column(Float, default=0.0)
    item_category = Column(String, nullable=True)
    requirement_date = Column(DateTime, nullable=True)
    attributes = Column(JSON, default=dict)

    header = relationship("CanonicalBomHeader", back_populates="items")


# ─── Order Chain ────────────────────────────────────────────────────────────────

class OrderChain(Base):
    __tablename__ = "order_chain"

    id = Column(String, primary_key=True, default=generate_uuid)
    sales_order = Column(String, nullable=True, index=True)
    sales_order_item = Column(String, nullable=True)
    planned_order = Column(String, nullable=True, index=True)
    production_order = Column(String, nullable=True, index=True)
    material = Column(String, nullable=False)
    plant = Column(String, nullable=False)
    quantity = Column(Float, nullable=True)
    chain_stage = Column(String, nullable=True)  # INTENDED | SCHEDULED | EXECUTED
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── Decision Records ──────────────────────────────────────────────────────────

class Decision(Base):
    __tablename__ = "decision"

    id = Column(String, primary_key=True, default=generate_uuid)
    request_material = Column(String, nullable=False)
    request_plant = Column(String, nullable=False)
    request_quantity = Column(Float, nullable=False)
    request_due_date = Column(DateTime, nullable=True)
    constraints = Column(JSON, default=dict)
    scoring_profile = Column(JSON, default=dict)
    candidates = Column(JSON, default=list)  # Full ranked candidates list
    recommended_candidate_id = Column(String, nullable=True)
    status = Column(String, default="computed")  # computed | approved | rejected
    trace = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String, nullable=True)


# ─── Audit Log ──────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(String, primary_key=True, default=generate_uuid)
    event_type = Column(String, nullable=False)  # decision_created | decision_approved | data_ingested
    entity_type = Column(String, nullable=True)
    entity_id = Column(String, nullable=True)
    actor = Column(String, default="system")
    details = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
