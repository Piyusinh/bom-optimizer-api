"""Idempotent RBAC seed — default roles, pages/menus (with submenus) and users.

Runs on every startup (see database.init_db). Safe to re-run: only inserts
rows that don't already exist by natural key (role.name, page.key, user.email).
This replaces the old static app/auth_data.py demo dict with real DB rows that
a super admin can manage from the UI.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Role, Page, RolePage, AppUser
from app.security import hash_password

# (name, label, description, is_system)
DEFAULT_ROLES = [
    ("super_admin", "Super Administrator", "Full access to every page and the admin console.", True),
    ("admin", "Administrator", "Operational admin — production, analysis, decisions, data.", True),
    ("planner", "Production Planner", "Runs Production Persona and BOM analysis.", True),
    ("procurement", "Procurement Manager", "Data management and decisions.", True),
    ("sales", "Sales Manager", "Analyze BOM and decisions.", True),
    ("supply_chain", "Supply Chain Head", "Production, decisions and data.", True),
    ("rnd", "R&D Manager", "Research & Development — analyze BOM and review decisions.", True),
]

# (key, label, icon key, parent key or None, sort_order)
DEFAULT_PAGES = [
    ("/", "Dashboard", "dashboard", None, 0),
    ("/production", "Production Persona", "factory", None, 10),
    ("/analyze", "Analyze BOM", "beaker", None, 20),
    ("/decisions", "Decisions", "clipboard", None, 30),
    ("/data", "Data Management", "database", None, 40),
    ("/agent", "AI Assistant", "chat", None, 45),
    ("/guide", "Demo Guide", "book", None, 50),
    ("/architecture", "Architecture Readme", "doc", None, 60),
    ("/admin", "Administration", "shield", None, 100),
    ("/admin/users", "Users", "users", "/admin", 101),
    ("/admin/roles", "Roles", "key", "/admin", 102),
    ("/admin/pages", "Pages & Menus", "menu", "/admin", 103),
]

# role name -> list of page keys it can access. None = every page (super admin).
ROLE_PAGE_KEYS: dict[str, list[str] | None] = {
    "super_admin": None,
    "admin": ["/", "/production", "/analyze", "/decisions", "/data", "/agent"],
    "planner": ["/", "/production", "/analyze", "/decisions", "/agent"],
    "procurement": ["/", "/decisions", "/data", "/agent"],
    "sales": ["/", "/analyze", "/decisions", "/agent"],
    "supply_chain": ["/", "/production", "/decisions", "/data", "/agent"],
    "rnd": ["/", "/analyze", "/decisions", "/agent"],
}

# (email, username, full_name, password, role_name, is_super_admin)
DEFAULT_USERS = [
    ("superadmin@bom.local", "superadmin", "Sara SuperAdmin", "SuperAdmin@123", "super_admin", True),
    ("admin@bom.local", "admin", "Ava Administrator", "Admin@123", "admin", False),
    ("planner@bom.local", "planner", "Priya Planner", "Planner@123", "planner", False),
    ("procurement@bom.local", "procurement", "Raj Procurement", "Procure@123", "procurement", False),
    ("sales@bom.local", "sales", "Sana Sales", "Sales@123", "sales", False),
    ("supplychain@bom.local", "supplychain", "Sam Supply", "Supply@123", "supply_chain", False),
    ("rnd@bom.local", "rnd", "Ravi R&D", "RnD@123", "rnd", False),
]


async def seed_rbac(db: AsyncSession) -> None:
    # ── Roles ──────────────────────────────────────────────────────────────
    existing_roles = (await db.execute(select(Role))).scalars().all()
    role_by_name: dict[str, Role] = {r.name: r for r in existing_roles}
    for name, label, desc, is_system in DEFAULT_ROLES:
        if name not in role_by_name:
            role = Role(name=name, label=label, description=desc, is_system=is_system)
            db.add(role)
            role_by_name[name] = role
    await db.flush()

    # ── Pages ──────────────────────────────────────────────────────────────
    existing_pages = (await db.execute(select(Page))).scalars().all()
    page_by_key: dict[str, Page] = {p.key: p for p in existing_pages}
    for key, label, icon, _parent_key, sort_order in DEFAULT_PAGES:
        if key not in page_by_key:
            page = Page(key=key, label=label, icon=icon, sort_order=sort_order, is_system=True)
            db.add(page)
            page_by_key[key] = page
    await db.flush()

    # Wire parent_id in a second pass now that every page row exists.
    for key, _label, _icon, parent_key, _sort_order in DEFAULT_PAGES:
        if parent_key and page_by_key[key].parent_id is None:
            page_by_key[key].parent_id = page_by_key[parent_key].id
    await db.flush()

    # ── Role → Page grants ───────────────────────────────────────────────
    existing_links = (await db.execute(select(RolePage))).scalars().all()
    linked_pairs = {(link.role_id, link.page_id) for link in existing_links}
    for role_name, page_keys in ROLE_PAGE_KEYS.items():
        role = role_by_name[role_name]
        keys = page_keys if page_keys is not None else list(page_by_key.keys())
        for key in keys:
            page = page_by_key.get(key)
            if not page:
                continue
            pair = (role.id, page.id)
            if pair not in linked_pairs:
                db.add(RolePage(role_id=role.id, page_id=page.id))
                linked_pairs.add(pair)
    await db.flush()

    # ── Default users ────────────────────────────────────────────────────
    existing_users = (await db.execute(select(AppUser))).scalars().all()
    existing_emails = {u.email for u in existing_users}
    for email, username, full_name, password, role_name, is_super in DEFAULT_USERS:
        if email in existing_emails:
            continue
        db.add(AppUser(
            email=email,
            username=username,
            full_name=full_name,
            password_hash=hash_password(password),
            role_id=role_by_name[role_name].id,
            is_super_admin=is_super,
            is_active=True,
        ))

    await db.commit()
