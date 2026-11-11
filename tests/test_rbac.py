"""Boundary tests for the DB-backed RBAC / auth subsystem."""
import pytest
from httpx import AsyncClient

from app.rate_limit import LoginRateLimiter
from app.security import validate_password_strength

SUPER = {"email": "superadmin@bom.local", "password": "SuperAdmin@123"}
PLANNER = {"email": "planner@bom.local", "password": "Planner@123"}


async def _login(client: AsyncClient, creds: dict) -> str:
    r = await client.post("/api/auth/login", json=creds)
    assert r.status_code == 200
    return r.json()["access_token"]


async def _me(client: AsyncClient, token: str) -> dict:
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    return r.json()


async def test_super_admin_login_returns_menu(client: AsyncClient):
    r = await client.post("/api/auth/login", json=SUPER)
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data
    assert data["user"]["is_super_admin"] is True
    assert any(p["key"] == "/admin" for p in data["user"]["menu"])


async def test_non_super_admin_cannot_access_admin_console(client: AsyncClient):
    token = await _login(client, PLANNER)
    r = await client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_self_escalation_guard(client: AsyncClient):
    token = await _login(client, SUPER)
    me = await _me(client, token)
    r = await client.put(
        f"/api/admin/users/{me['id']}",
        json={"is_super_admin": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_logout_revokes_token(client: AsyncClient):
    token = await _login(client, SUPER)
    assert (await _me(client, token))["email"] == SUPER["email"]

    r = await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_create_user_enforces_password_complexity(client: AsyncClient):
    token = await _login(client, SUPER)
    r = await client.post(
        "/api/admin/users",
        json={
            "email": "newuser@bom.local",
            "full_name": "New User",
            "password": "weak",
            "is_active": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


async def test_reset_password_enforces_password_complexity(client: AsyncClient):
    token = await _login(client, SUPER)
    create = await client.post(
        "/api/admin/users",
        json={
            "email": "pwuser@bom.local",
            "full_name": "PW User",
            "password": "StrongP@ss1",
            "is_active": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create.status_code == 201
    user_id = create.json()["id"]

    r = await client.post(
        f"/api/admin/users/{user_id}/reset-password",
        json={"new_password": "short"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


def test_password_validator_rejects_weak_passwords():
    with pytest.raises(ValueError):
        validate_password_strength("weak")


def test_password_validator_accepts_strong_passwords():
    assert validate_password_strength("StrongP@ss1") == "StrongP@ss1"


def test_login_rate_limiter_blocks_after_threshold():
    limiter = LoginRateLimiter()
    limiter._attempts.clear()
    for _ in range(5):
        limiter.record("ip")
    assert limiter.is_allowed("ip") is False
