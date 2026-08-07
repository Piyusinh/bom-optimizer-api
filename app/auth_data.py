"""Demo user directory and role → page-access mapping (Phase 0 mock auth).

In later phases this is replaced by Microsoft Entra ID (SSO) per NFR-020,
with role/page assignments sourced from an admin-managed RBAC store.
"""
from app.security import hash_password

# ─── Roles (mirrors stakeholders in docs/01-requirements.md) ──────────────────

ROLE_ADMIN = "admin"
ROLE_PLANNER = "planner"
ROLE_PROCUREMENT = "procurement"
ROLE_SALES = "sales"
ROLE_SUPPLY_CHAIN = "supply_chain"

ROLE_LABELS = {
    ROLE_ADMIN: "Administrator",
    ROLE_PLANNER: "Production Planner",
    ROLE_PROCUREMENT: "Procurement Manager",
    ROLE_SALES: "Sales Manager",
    ROLE_SUPPLY_CHAIN: "Supply Chain Head",
}

# ─── Page access per role (source of truth — UI mirrors this) ────────────────

ROLE_ALLOWED_PAGES = {
    ROLE_ADMIN: ["/", "/analyze", "/decisions", "/data"],
    ROLE_PLANNER: ["/", "/analyze", "/decisions"],
    ROLE_PROCUREMENT: ["/", "/decisions", "/data"],
    ROLE_SALES: ["/", "/analyze", "/decisions"],
    ROLE_SUPPLY_CHAIN: ["/", "/decisions", "/data"],
}

# ─── Demo directory (Phase 0 only — replace with real IdP in later phases) ───
# Passwords are simple for local dev convenience; never use in production.

DEMO_USERS = {
    "admin@bom.local": {
        "id": "u-admin",
        "full_name": "Ava Administrator",
        "email": "admin@bom.local",
        "password_hash": hash_password("Admin@123"),
        "role": ROLE_ADMIN,
        "is_active": True,
    },
    "planner@bom.local": {
        "id": "u-planner",
        "full_name": "Priya Planner",
        "email": "planner@bom.local",
        "password_hash": hash_password("Planner@123"),
        "role": ROLE_PLANNER,
        "is_active": True,
    },
    "procurement@bom.local": {
        "id": "u-procurement",
        "full_name": "Raj Procurement",
        "email": "procurement@bom.local",
        "password_hash": hash_password("Procure@123"),
        "role": ROLE_PROCUREMENT,
        "is_active": True,
    },
    "sales@bom.local": {
        "id": "u-sales",
        "full_name": "Sana Sales",
        "email": "sales@bom.local",
        "password_hash": hash_password("Sales@123"),
        "role": ROLE_SALES,
        "is_active": True,
    },
    "supplychain@bom.local": {
        "id": "u-supplychain",
        "full_name": "Sam Supply",
        "email": "supplychain@bom.local",
        "password_hash": hash_password("Supply@123"),
        "role": ROLE_SUPPLY_CHAIN,
        "is_active": True,
    },
}


def get_user_by_email(email: str) -> dict | None:
    return DEMO_USERS.get(email.lower().strip())


def get_allowed_pages(role: str) -> list[str]:
    return ROLE_ALLOWED_PAGES.get(role, ["/"])


def get_role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)
