"""SQLAlchemy ORM models — canonical BOM, order chain, decisions, audit."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, DateTime, Boolean, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship, backref
from app.database import Base


def generate_uuid():
    return str(uuid.uuid4())


# ─── RBAC — Roles, Pages/Menus (with submenus), Users ──────────────────────────

class Role(Base):
    """A named permission bundle. 'super_admin' bypasses all page checks."""
    __tablename__ = "role"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True, nullable=False, index=True)   # machine key, e.g. 'planner'
    label = Column(String, nullable=False)                            # display name
    description = Column(String, nullable=True)
    is_system = Column(Boolean, default=False)                        # built-in roles cannot be deleted
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("AppUser", back_populates="role")
    page_links = relationship("RolePage", back_populates="role", cascade="all, delete-orphan")


class Page(Base):
    """A menu entry / route. Self-referential parent_id gives submenus."""
    __tablename__ = "page"

    id = Column(String, primary_key=True, default=generate_uuid)
    key = Column(String, unique=True, nullable=False, index=True)     # route path, e.g. '/production'
    label = Column(String, nullable=False)
    icon = Column(String, nullable=True)                              # icon key resolved client-side
    parent_id = Column(String, ForeignKey("page.id"), nullable=True, index=True)
    sort_order = Column(Integer, default=0)
    is_menu = Column(Boolean, default=True)                           # show in sidebar vs hidden/API-only
    is_active = Column(Boolean, default=True)
    is_system = Column(Boolean, default=False)                        # built-in pages cannot be deleted
    created_at = Column(DateTime, default=datetime.utcnow)

    children = relationship("Page", backref=backref("parent", remote_side=[id]))
    role_links = relationship("RolePage", back_populates="page", cascade="all, delete-orphan")


class RolePage(Base):
    """Grants a Role access to a Page (many-to-many join)."""
    __tablename__ = "role_page"

    id = Column(String, primary_key=True, default=generate_uuid)
    role_id = Column(String, ForeignKey("role.id"), nullable=False, index=True)
    page_id = Column(String, ForeignKey("page.id"), nullable=False, index=True)

    role = relationship("Role", back_populates="page_links")
    page = relationship("Page", back_populates="role_links")


class AppUser(Base):
    """DB-backed user directory replacing the Phase 0 static demo dict."""
    __tablename__ = "app_user"

    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    username = Column(String, unique=True, nullable=True, index=True)
    full_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    role_id = Column(String, ForeignKey("role.id"), nullable=True, index=True)
    is_super_admin = Column(Boolean, default=False)                   # bypasses all page checks
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

    role = relationship("Role", back_populates="users")


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
    # Delta-extraction watermark: mirrors SAP LastChangeDateTime. On re-ingest
    # a row is skipped if it is not newer than the stored value.
    last_change_datetime = Column(DateTime, nullable=True)
    attributes = Column(JSON, default=dict)

    # Phase 1 — links the BOM to the production version / recipe that ran it
    production_version = Column(String, nullable=True, index=True)  # MKAL-VERID
    alternative_bom = Column(String, nullable=True)                 # MAST-STLAL
    recipe_id = Column(String, nullable=True, index=True)           # PLKO group
    production_line = Column(String, nullable=True, index=True)     # work center used
    order_start_date = Column(DateTime, nullable=True)              # AFKO-GSTRP

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
    # Real client column (ProdOrder Component sheet, WithdrawnQuantity):
    # "goods-issued quantity against the component reservation to date;
    # always <= RequirementQuantity" per the workbook's own legend. This IS
    # the movement-type-261 signal the RTM algorithm asks for — only
    # populated for PRODUCTION_ORDER items since Planned/Sales Order sheets
    # do not carry this column.
    withdrawn_quantity = Column(Float, nullable=True)
    attributes = Column(JSON, default=dict)

    header = relationship("CanonicalBomHeader", back_populates="items")


# ─── Canonical Material Master (MARA + MARC) ───────────────────────────────────

class CanonicalMaterial(Base):
    """Material master. Fields mirror the RTM 'Material fields' sheet."""
    __tablename__ = "canonical_material"

    id = Column(String, primary_key=True, default=generate_uuid)
    material = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    plant = Column(String, nullable=False, index=True)
    material_type = Column(String, nullable=True, index=True)  # FERT|HALB|ROH|VERP
    proc_type = Column(String, nullable=True)                  # E=in-house, F=external, Phantom
    mrp_type = Column(String, nullable=True)
    mrp_controller = Column(String, nullable=True)
    stock = Column(Float, default=0.0)
    uom = Column(String, nullable=False, default="KG")
    cost = Column(Float, default=0.0)
    currency = Column(String, default="USD")
    batch_managed = Column(Boolean, default=False)
    lead_time_days = Column(Integer, default=0)
    # RTM Algorithm item 3: raw material grade. Populated for ROH materials;
    # drives grade-aware ranking once the client confirms grade semantics.
    grade = Column(String, nullable=True)
    status = Column(String, default="ACTIVE")
    deletion_indicator = Column(Boolean, default=False)
    source_system = Column(String, default="mock-excel")
    extracted_at = Column(DateTime, default=datetime.utcnow)
    attributes = Column(JSON, default=dict)


# ─── Canonical Production Version (MKAL) ───────────────────────────────────────

class CanonicalProductionVersion(Base):
    """Ties one BOM alternative to one recipe/routing for a validity window."""
    __tablename__ = "canonical_production_version"

    id = Column(String, primary_key=True, default=generate_uuid)
    production_version = Column(String, nullable=False, index=True)  # VERID
    material = Column(String, nullable=False, index=True)
    plant = Column(String, nullable=False, index=True)
    bom_usage = Column(String, nullable=True)          # STLAN
    alternative_bom = Column(String, nullable=True)    # STLAL
    recipe_id = Column(String, nullable=True, index=True)
    valid_from = Column(DateTime, nullable=True)       # ADATU
    valid_to = Column(DateTime, nullable=True)         # BDATU
    production_line = Column(String, nullable=True, index=True)
    lot_size_min = Column(Float, nullable=True)
    lot_size_max = Column(Float, nullable=True)
    status = Column(String, default="ACTIVE")
    source_system = Column(String, default="mock-excel")
    extracted_at = Column(DateTime, default=datetime.utcnow)


# ─── Canonical Recipe / Routing (PLKO + PLPO) ──────────────────────────────────

class CanonicalRecipe(Base):
    """Recipe header. 'Recipe' in chemical, 'Routing' in discrete — same table."""
    __tablename__ = "canonical_recipe"

    id = Column(String, primary_key=True, default=generate_uuid)
    recipe_id = Column(String, nullable=False, index=True)
    recipe_name = Column(String, nullable=True)
    plant = Column(String, nullable=False, index=True)
    material = Column(String, nullable=True, index=True)
    usage = Column(String, nullable=True)              # VERWE
    status = Column(String, default="RELEASED")        # STATU
    base_quantity = Column(Float, default=1.0)
    base_unit = Column(String, default="KG")
    source_terminology = Column(String, default="RECIPE")  # RECIPE | ROUTING
    source_system = Column(String, default="mock-excel")
    extracted_at = Column(DateTime, default=datetime.utcnow)

    operations = relationship(
        "CanonicalRecipeOperation",
        back_populates="recipe",
        cascade="all, delete-orphan",
    )


class CanonicalRecipeOperation(Base):
    """One operation/phase inside a recipe (PLPO)."""
    __tablename__ = "canonical_recipe_operation"

    id = Column(String, primary_key=True, default=generate_uuid)
    recipe_pk = Column(String, ForeignKey("canonical_recipe.id"), nullable=False)
    operation_number = Column(Integer, nullable=False)   # VORNR
    description = Column(String, nullable=True)
    work_center = Column(String, nullable=True, index=True)  # ARBID / production line
    control_key = Column(String, nullable=True)          # STEUS
    setup_time_hours = Column(Float, default=0.0)
    run_time_hours = Column(Float, default=0.0)
    teardown_time_hours = Column(Float, default=0.0)
    cost_per_hour = Column(Float, default=0.0)
    attributes = Column(JSON, default=dict)

    recipe = relationship("CanonicalRecipe", back_populates="operations")


# ─── Canonical Goods Movement (MSEG + MKPF) ────────────────────────────────────

class CanonicalGoodsMovement(Base):
    """Actual component issues/receipts. Movement type 261 = issue to order."""
    __tablename__ = "canonical_goods_movement"

    id = Column(String, primary_key=True, default=generate_uuid)
    material_document = Column(String, nullable=True)   # MBLNR
    posting_date = Column(DateTime, nullable=True)      # BUDAT
    material = Column(String, nullable=False, index=True)
    plant = Column(String, nullable=False, index=True)
    batch = Column(String, nullable=True)
    quantity = Column(Float, nullable=False, default=0.0)
    unit = Column(String, default="KG")
    movement_type = Column(String, nullable=True, index=True)  # BWART
    order_reference = Column(String, nullable=True, index=True)  # AUFNR
    source_system = Column(String, default="mock-excel")


# ─── Canonical Order Confirmation (AFRU) ───────────────────────────────────────

class CanonicalOrderConfirmation(Base):
    """Actual production confirmation — yield, scrap, work center, timings."""
    __tablename__ = "canonical_order_confirmation"

    id = Column(String, primary_key=True, default=generate_uuid)
    order_number = Column(String, nullable=False, index=True)  # AUFNR
    confirmation_counter = Column(Integer, nullable=True)      # RMZHL
    operation_number = Column(Integer, nullable=True)
    work_center = Column(String, nullable=True, index=True)    # ARBPL
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)
    yield_quantity = Column(Float, default=0.0)                # GAMNG
    scrap_quantity = Column(Float, default=0.0)                # XMNGA
    unit = Column(String, default="KG")
    source_system = Column(String, default="mock-excel")


# ─── Rework / Reprocess Hierarchy ──────────────────────────────────────────────

class ReworkLink(Base):
    """Links a rework/reprocess order back to its original order."""
    __tablename__ = "rework_link"

    id = Column(String, primary_key=True, default=generate_uuid)
    original_order = Column(String, nullable=False, index=True)
    rework_order = Column(String, nullable=False, index=True)
    level = Column(Integer, default=1)
    material = Column(String, nullable=True, index=True)
    plant = Column(String, nullable=True, index=True)
    reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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


class RevokedToken(Base):
    __tablename__ = "revoked_token"

    id = Column(String, primary_key=True, default=generate_uuid)
    jti = Column(String, nullable=False, unique=True, index=True)
    token_type = Column(String, default="access")
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
