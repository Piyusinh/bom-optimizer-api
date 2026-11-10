"""DEPRECATED — superseded by DB-backed RBAC.

Users, roles and page grants now live in the database:
    - app/models.py       -> Role, Page, RolePage, AppUser
    - app/rbac_seed.py    -> default roles/pages/users seeded on startup
    - app/dependencies.py -> get_current_user / load_allowed_pages query the DB
    - app/routes/admin.py -> super-admin CRUD for users/roles/pages

Nothing in the codebase imports this module anymore. Kept only so any
external reference fails loudly instead of silently reintroducing the old
static demo directory.
"""

