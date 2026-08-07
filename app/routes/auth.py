"""Authentication routes — Phase 0 mock login (replaced by Entra ID in later phases)."""
from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import LoginRequest, LoginResponse, UserInfo
from app.security import verify_password, create_access_token
from app.auth_data import get_user_by_email, get_allowed_pages, get_role_label
from app.dependencies import get_current_user, CurrentUser

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest):
    user = get_user_by_email(payload.email)
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive",
        )

    token, expires_at = create_access_token({"sub": user["email"], "role": user["role"]})

    return LoginResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
        user=UserInfo(
            id=user["id"],
            email=user["email"],
            full_name=user["full_name"],
            role=user["role"],
            role_label=get_role_label(user["role"]),
            allowed_pages=get_allowed_pages(user["role"]),
        ),
    )


@router.get("/me", response_model=UserInfo)
async def me(current_user: CurrentUser = Depends(get_current_user)):
    return UserInfo(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        role_label=current_user.role_label,
        allowed_pages=current_user.allowed_pages,
    )


@router.post("/logout")
async def logout(current_user: CurrentUser = Depends(get_current_user)):
    # Stateless JWT — nothing to invalidate server-side in Phase 0.
    # A token denylist / refresh-token revocation would be added in later phases.
    return {"status": "ok"}
