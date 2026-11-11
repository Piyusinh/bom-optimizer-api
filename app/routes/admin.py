"""Super-admin console — manage users, roles, and pages/menus (with submenus)
and the permission grants between them. Every route here requires
is_super_admin (see app.dependencies.require_super_admin).
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.dependencies import require_super_admin, CurrentUser
from app.models import AppUser, AuditLog, Role, Page, RolePage
from app.security import hash_password
from app.schemas import (
    UserAdminOut,
    UserCreate,
    UserUpdate,
    UserPasswordReset,
    RoleOut,
    RoleCreate,
    RoleUpdate,
    PageOut,
    PageCreate,
    PageUpdate,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ─── Helpers ────────────────────────────────────────────────────────────────

def _user_out(u: AppUser) -> UserAdminOut:
    return UserAdminOut(
        id=u.id,
        email=u.email,
        username=u.username,
        full_name=u.full_name,
        role_id=u.role_id,
        role_name=u.role.name if u.role else None,
        role_label=u.role.label if u.role else None,
        is_super_admin=u.is_super_admin,
        is_active=u.is_active,
        created_at=u.created_at.isoformat() if u.created_at else None,
        last_login_at=u.last_login_at.isoformat() if u.last_login_at else None,
    )


def _page_out(p: Page) -> PageOut:
    return PageOut(
        id=p.id,
        key=p.key,
        label=p.label,
        icon=p.icon,
        parent_id=p.parent_id,
        sort_order=p.sort_order,
        is_menu=p.is_menu,
        is_active=p.is_active,
        is_system=p.is_system,
    )


def _audit(
    db: AsyncSession,
    actor: str,
    event_type: str,
    entity_type: str,
    entity_id: str | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            details=details or {},
        )
    )


# ─── Users ──────────────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserAdminOut])
async def list_users(
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: CurrentUser = Depends(require_super_admin()),
):
    stmt = select(AppUser).options(selectinload(AppUser.role))
    if q:
        like = f"%{q.lower().strip()}%"
        stmt = stmt.where(
            func.lower(AppUser.email).like(like) | func.lower(AppUser.full_name).like(like)
        )
    stmt = stmt.order_by(AppUser.created_at.desc())
    result = await db.execute(stmt)
    return [_user_out(u) for u in result.scalars().all()]


@router.post("/users", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    email = payload.email.lower().strip()
    existing = await db.execute(select(AppUser).where(AppUser.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    if payload.role_id:
        role = (await db.execute(select(Role).where(Role.id == payload.role_id))).scalar_one_or_none()
        if not role:
            raise HTTPException(status_code=400, detail="Unknown role_id")

    user = AppUser(
        email=email,
        username=payload.username,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role_id=payload.role_id,
        is_super_admin=payload.is_super_admin,
        is_active=payload.is_active,
    )
    db.add(user)
    await db.flush()
    _audit(
        db,
        current.email,
        "user_created",
        "app_user",
        user.id,
        {
            "email": user.email,
            "role_id": user.role_id,
            "is_super_admin": user.is_super_admin,
        },
    )
    await db.commit()
    result = await db.execute(
        select(AppUser).options(selectinload(AppUser.role)).where(AppUser.id == user.id)
    )
    return _user_out(result.scalar_one())


@router.put("/users/{user_id}", response_model=UserAdminOut)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    result = await db.execute(
        select(AppUser).options(selectinload(AppUser.role)).where(AppUser.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user_id == current.id and (payload.role_id is not None or payload.is_super_admin is not None):
        raise HTTPException(status_code=403, detail="You cannot modify your own role or super admin status")

    if payload.role_id is not None:
        if payload.role_id:
            role = (await db.execute(select(Role).where(Role.id == payload.role_id))).scalar_one_or_none()
            if not role:
                raise HTTPException(status_code=400, detail="Unknown role_id")
        user.role_id = payload.role_id or None

    if payload.email is not None:
        user.email = payload.email.lower().strip()
    if payload.username is not None:
        user.username = payload.username
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.is_super_admin is not None:
        user.is_super_admin = payload.is_super_admin
    if payload.is_active is not None:
        user.is_active = payload.is_active

    user.updated_at = datetime.utcnow()
    _audit(
        db,
        current.email,
        "user_updated",
        "app_user",
        user_id,
        {"updates": payload.model_dump(exclude_unset=True)},
    )
    await db.commit()
    result = await db.execute(
        select(AppUser).options(selectinload(AppUser.role)).where(AppUser.id == user_id)
    )
    return _user_out(result.scalar_one())


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    if user_id == current.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    user = (await db.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _audit(db, current.email, "user_deleted", "app_user", user.id, {"email": user.email})
    await db.delete(user)
    await db.commit()


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: str,
    payload: UserPasswordReset,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    user = (await db.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(payload.new_password)
    _audit(db, current.email, "user_password_reset", "app_user", user.id, {})
    await db.commit()


# ─── Roles ──────────────────────────────────────────────────────────────────

@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    _: CurrentUser = Depends(require_super_admin()),
):
    roles = (await db.execute(select(Role))).scalars().all()
    out: list[RoleOut] = []
    for r in roles:
        links = (await db.execute(select(RolePage).where(RolePage.role_id == r.id))).scalars().all()
        user_count = (
            await db.execute(select(func.count(AppUser.id)).where(AppUser.role_id == r.id))
        ).scalar_one()
        out.append(
            RoleOut(
                id=r.id,
                name=r.name,
                label=r.label,
                description=r.description,
                is_system=r.is_system,
                user_count=user_count,
                page_ids=[link.page_id for link in links],
            )
        )
    return out


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    name = payload.name.strip().lower().replace(" ", "_")
    existing = await db.execute(select(Role).where(Role.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A role with this name already exists")

    role = Role(name=name, label=payload.label, description=payload.description, is_system=False)
    db.add(role)
    await db.flush()
    for page_id in payload.page_ids:
        db.add(RolePage(role_id=role.id, page_id=page_id))
    _audit(
        db,
        current.email,
        "role_created",
        "role",
        role.id,
        {"name": role.name, "label": role.label, "page_ids": payload.page_ids},
    )
    await db.commit()

    return RoleOut(
        id=role.id,
        name=role.name,
        label=role.label,
        description=role.description,
        is_system=role.is_system,
        user_count=0,
        page_ids=payload.page_ids,
    )


@router.put("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    role = (await db.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if payload.label is not None:
        role.label = payload.label
    if payload.description is not None:
        role.description = payload.description
    if payload.page_ids is not None:
        existing_links = (
            await db.execute(select(RolePage).where(RolePage.role_id == role.id))
        ).scalars().all()
        for link in existing_links:
            await db.delete(link)
        await db.flush()
        for page_id in payload.page_ids:
            db.add(RolePage(role_id=role.id, page_id=page_id))

    _audit(
        db,
        current.email,
        "role_updated",
        "role",
        role_id,
        {"updates": payload.model_dump(exclude_unset=True)},
    )
    await db.commit()

    links = (await db.execute(select(RolePage).where(RolePage.role_id == role.id))).scalars().all()
    user_count = (
        await db.execute(select(func.count(AppUser.id)).where(AppUser.role_id == role.id))
    ).scalar_one()
    return RoleOut(
        id=role.id,
        name=role.name,
        label=role.label,
        description=role.description,
        is_system=role.is_system,
        user_count=user_count,
        page_ids=[link.page_id for link in links],
    )


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    role = (await db.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(status_code=400, detail="Built-in roles cannot be deleted")
    in_use = (
        await db.execute(select(func.count(AppUser.id)).where(AppUser.role_id == role.id))
    ).scalar_one()
    if in_use:
        raise HTTPException(status_code=400, detail=f"{in_use} user(s) still have this role")
    _audit(db, current.email, "role_deleted", "role", role.id, {"name": role.name})
    await db.delete(role)
    await db.commit()


# ─── Pages / Menus ──────────────────────────────────────────────────────────

@router.get("/pages", response_model=list[PageOut])
async def list_pages(
    db: AsyncSession = Depends(get_db),
    _: CurrentUser = Depends(require_super_admin()),
):
    pages = (await db.execute(select(Page).order_by(Page.sort_order))).scalars().all()
    return [_page_out(p) for p in pages]


@router.post("/pages", response_model=PageOut, status_code=status.HTTP_201_CREATED)
async def create_page(
    payload: PageCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    key = payload.key.strip()
    existing = await db.execute(select(Page).where(Page.key == key))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A page with this key already exists")

    if payload.parent_id:
        parent = (await db.execute(select(Page).where(Page.id == payload.parent_id))).scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=400, detail="Unknown parent_id")

    page = Page(
        key=key,
        label=payload.label,
        icon=payload.icon,
        parent_id=payload.parent_id,
        sort_order=payload.sort_order,
        is_menu=payload.is_menu,
        is_active=True,
        is_system=False,
    )
    db.add(page)
    await db.flush()
    _audit(
        db,
        current.email,
        "page_created",
        "page",
        page.id,
        {"key": page.key, "label": page.label, "parent_id": page.parent_id},
    )
    await db.commit()
    return _page_out(page)


@router.put("/pages/{page_id}", response_model=PageOut)
async def update_page(
    page_id: str,
    payload: PageUpdate,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    page = (await db.execute(select(Page).where(Page.id == page_id))).scalar_one_or_none()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    if payload.parent_id is not None:
        if payload.parent_id == page_id:
            raise HTTPException(status_code=400, detail="A page cannot be its own parent")
        if payload.parent_id:
            parent = (await db.execute(select(Page).where(Page.id == payload.parent_id))).scalar_one_or_none()
            if not parent:
                raise HTTPException(status_code=400, detail="Unknown parent_id")
            # Reject descendant cycles: walk the proposed parent's ancestor
            # chain. If this page appears in it, linking it would create a
            # cycle that infinitely recurses the menu tree builder.
            ancestor_id = parent.id
            while ancestor_id:
                if ancestor_id == page_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot set a page's parent to one of its own descendants",
                    )
                ancestor_id = (
                    await db.execute(select(Page.parent_id).where(Page.id == ancestor_id))
                ).scalar_one_or_none()

    if payload.key is not None:
        page.key = payload.key.strip()
    if payload.label is not None:
        page.label = payload.label
    if payload.icon is not None:
        page.icon = payload.icon
    if payload.parent_id is not None:
        page.parent_id = payload.parent_id or None
    if payload.sort_order is not None:
        page.sort_order = payload.sort_order
    if payload.is_menu is not None:
        page.is_menu = payload.is_menu
    if payload.is_active is not None:
        page.is_active = payload.is_active

    _audit(
        db,
        current.email,
        "page_updated",
        "page",
        page_id,
        {"updates": payload.model_dump(exclude_unset=True)},
    )
    await db.commit()
    return _page_out(page)


@router.delete("/pages/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page(
    page_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentUser = Depends(require_super_admin()),
):
    page = (await db.execute(select(Page).where(Page.id == page_id))).scalar_one_or_none()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.is_system:
        raise HTTPException(status_code=400, detail="Built-in pages cannot be deleted")
    children = (
        await db.execute(select(func.count(Page.id)).where(Page.parent_id == page_id))
    ).scalar_one()
    if children:
        raise HTTPException(status_code=400, detail="Remove submenu pages first")
    _audit(db, current.email, "page_deleted", "page", page.id, {"key": page.key, "label": page.label})
    await db.delete(page)
    await db.commit()
