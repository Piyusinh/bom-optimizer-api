# Enterprise Auth — Phase 2 Roadmap (Entra ID, Key Vault, Postgres)

This document captures the remaining enterprise-grade pieces that are **out of scope for a single client demo** but are the natural next steps after the DB-backed RBAC foundation in place.

## 1. Microsoft Entra ID SSO (OAuth2 / OIDC)

### Why
Local password tables are acceptable for a pilot/demo; enterprise buyers expect their own identity provider (IdP) to own the lifecycle, MFA, and offboarding.

### Implementation path
1. Add `ms-identity-web` or `authlib`/itsdangerary FastAPI integration for OIDC.
2. Register an app in Microsoft Entra ID (BTP/SAP context: `SAP BTP → Microsoft Entra ID` trust or a standalone app registration).
3. Add routes:
   - `GET /api/auth/login` → redirect to Entra `/authorize`
   - `GET /api/auth/callback` → receive `code`, exchange for tokens, read `oid`/`upn`/`groups`
4. Map Entra ID groups to `Role` rows (e.g. `BOM-Planner` → `planner`).
5. Keep the local `AppUser` table as a mirror/cache of IdP users (no passwords), keyed by `oid`.
6. Replace JWT `sub` with the Entra `oid` or email.

### Config additions
```python
ENTRA_CLIENT_ID: str = ""
ENTRA_CLIENT_SECRET: str = ""
ENTRA_TENANT_ID: str = ""
ENTRA_REDIRECT_URI: str = "http://localhost:8000/api/auth/callback"
```

### UX impact
- Login page redirects to Entra or a branded Microsoft login pop-up.
- Super admin console still manages `Role` → `Page` grants; user provisioning becomes read-only from Entra groups.

---

## 2. Azure Key Vault for secrets

### Why
`JWT_SECRET_KEY`, `ENTRA_CLIENT_SECRET`, and `AZURE_OPENAI_KEY` should never live in `.env` files in production.

### Implementation path
1. Use `azure-identity` + `azure-keyvault-secrets`.
2. In `app/config.py`, change the `JWT_SECRET_KEY` field to pull from Key Vault if `AZURE_KEY_VAULT_URL` is set:
   ```python
   @property
   def JWT_SECRET_KEY(self) -> str:
       return self._secret_from_keyvault("jwt-secret-key")
   ```
3. Store required secrets in Key Vault:
   - `jwt-secret-key`
   - `entra-client-secret`
   - `azure-openai-key`
4. Use a managed identity (App Service / Container Apps) for local-dev, fall back to `DefaultAzureCredential`.

### Config additions
```python
AZURE_KEY_VAULT_URL: str = ""
```

---

## 3. Refresh tokens + token revocation at scale

### Why
The current in-memory denylist and 30-minute tokens work for the demo. A multi-worker / HA deployment needs a shared revocation store (Redis or Postgres) and short-lived access tokens with refresh tokens.

### Implementation path
1. Keep access tokens at 15 minutes.
2. Issue a separate `refresh_token` (JWT with `jti`, 7-day expiry) stored in a `RefreshToken` table or Redis.
3. `/api/auth/logout` revokes the access token *and* all refresh tokens for the user.
4. Use Redis `SET <jti> <exp> EX <ttl>` as the denylist for faster lookups.

---

## 4. PostgreSQL instead of SQLite

### Why
SQLite is fine for a demo but does not support real concurrency, row-level locking, or high availability.

### Migration
1. Switch `DATABASE_URL` to `postgresql+asyncpg://...`.
2. Add `asyncpg` to `requirements.txt`.
3. Add `alembic` migration scripts for the RBAC tables.
4. Use managed Azure Database for PostgreSQL with backups and private endpoints.

---

## 5. Multi-tenant isolation (only if multi-customer)

### Implementation path
1. Add `tenant_id` to `AppUser`, `Role`, `Page`, `RolePage`, `AuditLog`, and `RevokedToken`.
2. Every query in `dependencies.py` and `admin.py` is filtered by `tenant_id`.
3. Map Entra `tid` (tenant ID) to `tenant_id` at login.

---

## 6. Field- and row-level permissions

If roles need to see different data (e.g., a planner only sees plant `X`), extend the permission model beyond pages:
- Add `ResourcePolicy` (role + resource + action + filters).
- Apply filters to SQLAlchemy queries per route.

---

## Recommended order for a BTP client demo

1. **Entra ID SSO** — highest demo impact; buyers ask about it first.
2. **Key Vault for JWT secret** — cheap trust signal.
3. **Postgres for production** — needed before go-live.
4. **Refresh-token revocation with Redis** — needed for scale.
5. **Multi-tenant / field-level** — only if the contract requires it.
