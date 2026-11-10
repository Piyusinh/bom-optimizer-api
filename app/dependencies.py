"""FastAPI dependencies — DB-backed RBAC auth guard used by protected routes.

Replaces the Phase 0 static app/auth_data.py dict. Users, roles and page
grants now live in the database (see app/models.py Role/Page/RolePage/AppUser
and app/rbac_seed.py for the default seed) and are managed by a super admin
through /api/admin/*.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.security import decode_access_token
from app.database import get_db
from app.models import AppUser, Page, RevokedToken, RolePage

bearer_scheme = HTTPBearer(auto_error=False)

# Documentation pages are always visible to every authenticated user,
# regardless of role grants — mirrors the Phase 0 behaviour where the
# Demo Guide / Architecture Readme were open to all roles.
ALWAYS_ALLOWED_PAGES = ["/", "/guide", "/architecture"]


class CurrentUser:
    def __init__(
        self,
        id: str,
        email: str,
        full_name: str,
        role: str,
        role_label: str,
        is_super_admin: bool,
        allowed_pages: list[str],
    ):
        self.id = id
        self.email = email
        self.full_name = full_name
        self.role = role
        self.role_label = role_label
        self.is_super_admin = is_super_admin
        self.allowed_pages = allowed_pages


async def load_allowed_pages(db: AsyncSession, role_id: str | None, is_super_admin: bool) -> list[str]:
    """Flat list of page keys this role (or super admin) may access."""
    if is_super_admin:
        result = await db.execute(select(Page.key).where(Page.is_active == True))  # noqa: E712
        keys = {row[0] for row in result.all()}
    elif role_id:
        result = await db.execute(
            select(Page.key)
            .join(RolePage, RolePage.page_id == Page.id)
            .where(RolePage.role_id == role_id, Page.is_active == True)  # noqa: E712
        )
        keys = {row[0] for row in result.all()}
    else:
        keys = set()
    keys.update(ALWAYS_ALLOWED_PAGES)
    return sorted(keys)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    revoked = (
        await db.execute(select(RevokedToken).where(RevokedToken.jti == jti))
    ).scalar_one_or_none()
    if revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    email = payload.get("sub")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(
        select(AppUser).options(selectinload(AppUser.role)).where(AppUser.email == email)
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    allowed_pages = await load_allowed_pages(db, user.role_id, user.is_super_admin)
    role_name = user.role.name if user.role else "user"
    role_label = user.role.label if user.role else "User"

    return CurrentUser(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=role_name,
        role_label=role_label,
        is_super_admin=user.is_super_admin,
        allowed_pages=allowed_pages,
    )


def require_page_access(page_path: str):
    """Dependency factory: raise 403 if the current user's role cannot access page_path."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.is_super_admin:
            return user
        if page_path not in user.allowed_pages:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' does not have access to '{page_path}'",
            )
        return user

    return _check


def require_super_admin():
    """Dependency factory: raise 403 unless the current user is a super admin."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.is_super_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Super administrator access required",
            )
        return user

    return _check
