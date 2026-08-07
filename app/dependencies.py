"""FastAPI dependencies — auth guard used by protected routes."""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.security import decode_access_token
from app.auth_data import get_user_by_email, get_allowed_pages, get_role_label

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    def __init__(self, id: str, email: str, full_name: str, role: str):
        self.id = id
        self.email = email
        self.full_name = full_name
        self.role = role
        self.allowed_pages = get_allowed_pages(role)
        self.role_label = get_role_label(role)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
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

    email = payload.get("sub")
    user = get_user_by_email(email) if email else None
    if user is None or not user.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CurrentUser(
        id=user["id"],
        email=user["email"],
        full_name=user["full_name"],
        role=user["role"],
    )


def require_page_access(page_path: str):
    """Dependency factory: raise 403 if the current user's role cannot access page_path."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if page_path not in user.allowed_pages:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' does not have access to '{page_path}'",
            )
        return user

    return _check
