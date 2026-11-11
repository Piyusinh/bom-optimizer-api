"""Builds the nested sidebar menu tree from the Page table, pruned to what
the current user is allowed to see (union of role grants + super-admin bypass,
already resolved into a flat `allowed_keys` list by dependencies.load_allowed_pages).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Page
from app.schemas import MenuPageOut


async def build_menu_tree(db: AsyncSession, allowed_keys: list[str]) -> list[MenuPageOut]:
    result = await db.execute(
        select(Page).where(Page.is_active == True, Page.is_menu == True).order_by(Page.sort_order)  # noqa: E712
    )
    pages = result.scalars().all()
    allowed = set(allowed_keys)

    nodes: dict[str, MenuPageOut] = {
        p.id: MenuPageOut(id=p.id, key=p.key, label=p.label, icon=p.icon, sort_order=p.sort_order, children=[])
        for p in pages
    }

    roots: list[MenuPageOut] = []
    for p in pages:
        node = nodes[p.id]
        if p.parent_id and p.parent_id in nodes:
            nodes[p.parent_id].children.append(node)
        else:
            roots.append(node)

    def prune(node: MenuPageOut, visiting: set[str]) -> bool:
        # Cycle guard — drop a branch that references an ancestor, otherwise
        # recursion would never terminate on a corrupt parent chain.
        if node.id in visiting:
            return False
        visiting.add(node.id)
        node.children = [c for c in node.children if prune(c, visiting)]
        visiting.discard(node.id)
        return node.key in allowed or len(node.children) > 0

    roots = [r for r in roots if prune(r, set())]
    roots.sort(key=lambda n: n.sort_order)
    for r in roots:
        r.children.sort(key=lambda n: n.sort_order)
    return roots
