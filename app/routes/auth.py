"""Authentication routes — DB-backed RBAC login (users/roles/pages managed by a
super admin via /api/admin/*, see app.rbac_seed.py for the default seed)."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models import AppUser, RevokedToken
from app.schemas import LoginRequest, LoginResponse, UserInfo
from app.security import verify_password, create_access_token, decode_access_token
from app.dependencies import get_current_user, CurrentUser, load_allowed_pages, bearer_scheme
from app.menu import build_menu_tree
from app.rate_limit import login_rate_limit

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(login_rate_limit),
):
    result = await db.execute(
        select(AppUser)
        .options(selectinload(AppUser.role))
        .where(AppUser.email == payload.email.lower().strip())
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive",
        )

    user.last_login_at = datetime.utcnow()
    await db.commit()

    allowed_pages = await load_allowed_pages(db, user.role_id, user.is_super_admin)
    role_name = user.role.name if user.role else "user"
    role_label = user.role.label if user.role else "User"
    menu = await build_menu_tree(db, allowed_pages)

    token, expires_at = create_access_token({"sub": user.email, "role": role_name})

    return LoginResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
        user=UserInfo(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=role_name,
            role_label=role_label,
            is_super_admin=user.is_super_admin,
            allowed_pages=allowed_pages,
            menu=menu,
        ),
    )


@router.get("/me", response_model=UserInfo)
async def me(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    menu = await build_menu_tree(db, current_user.allowed_pages)
    return UserInfo(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        role_label=current_user.role_label,
        is_super_admin=current_user.is_super_admin,
        allowed_pages=current_user.allowed_pages,
        menu=menu,
    )


@router.post("/refresh", response_model=LoginResponse)
async def refresh(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Issue a new access token for the currently authenticated user."""
    menu = await build_menu_tree(db, current_user.allowed_pages)
    token, expires_at = create_access_token({"sub": current_user.email, "role": current_user.role})
    return LoginResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
        user=UserInfo(
            id=current_user.id,
            email=current_user.email,
            full_name=current_user.full_name,
            role=current_user.role,
            role_label=current_user.role_label,
            is_super_admin=current_user.is_super_admin,
            allowed_pages=current_user.allowed_pages,
            menu=menu,
        ),
    )


@router.post("/logout")
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    db.add(
        RevokedToken(
            jti=jti,
            token_type="access",
            expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
        )
    )
    await db.commit()
    return {"status": "ok"}
