"""BOM Comparison Engine — compares current/planned vs proposed BOMs."""
from typing import Any

from app.schemas import ComponentDetail, ComponentDiff


def compare_boms(current: list[dict], proposed: list[dict]) -> dict[str, Any]:
    """
    Compare a current/planned BOM against a proposed BOM.
    Returns a diff with SAME, ADDED, REMOVED, and CHANGED entries.
    """
    current_by_key = {_component_key(c): c for c in current}
    proposed_by_key = {_component_key(c): c for c in proposed}

    same: list[dict] = []
    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []

    for key, c in current_by_key.items():
        if key in proposed_by_key:
            p = proposed_by_key[key]
            if _is_equal(c, p):
                same.append({"key": key, "component": c["component"]})
            else:
                changed.append({
                    "key": key,
                    "component": c["component"],
                    "current": c,
                    "proposed": p,
                })
        else:
            removed.append({"key": key, "component": c["component"], "current": c})

    for key, p in proposed_by_key.items():
        if key not in current_by_key:
            added.append({"key": key, "component": p["component"], "proposed": p})

    return {
        "same": same,
        "added": added,
        "removed": removed,
        "changed": changed,
        "same_count": len(same),
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
    }


def _component_key(component: dict) -> str:
    """Canonical key for comparing a component across BOMs."""
    return component["component"]


def _is_equal(a: dict, b: dict) -> bool:
    """Check if two component entries are materially equal."""
    keys = ["quantity", "unit", "scrap_percent"]
    return all(a.get(k) == b.get(k) for k in keys)


# ─── Phase 1 — static BOM vs proposed BOM ──────────────────────────────────────

def diff_component_details(
    baseline: list[ComponentDetail],
    proposed: list[ComponentDetail],
) -> list[ComponentDiff]:
    """Line-by-line diff with cost impact.

    This is RTM Production item 1 — "Production/Planned static BOM vs proposed
    BOM comparison" — and it is what the 'Proposed Recipe' button shows.
    """
    baseline_by_component = {c.component: c for c in baseline}
    proposed_by_component = {c.component: c for c in proposed}
    diffs: list[ComponentDiff] = []

    for component in sorted(set(baseline_by_component) | set(proposed_by_component)):
        b = baseline_by_component.get(component)
        p = proposed_by_component.get(component)

        if b is None and p is not None:
            change_type = "ADDED"
        elif p is None and b is not None:
            change_type = "REMOVED"
        elif b and p and abs(b.planned_quantity - p.planned_quantity) > 1e-6:
            change_type = "QUANTITY_CHANGED"
        else:
            change_type = "UNCHANGED"

        diffs.append(
            ComponentDiff(
                component=component,
                change_type=change_type,
                baseline_quantity=b.planned_quantity if b else None,
                proposed_quantity=p.planned_quantity if p else None,
                unit=(p.unit if p else b.unit if b else "KG"),
                cost_impact=round(
                    (p.extended_cost if p else 0.0) - (b.extended_cost if b else 0.0),
                    2,
                ),
            )
        )

    return diffs


def cost_delta(
    baseline_total: float, proposed_total: float
) -> tuple[float, float]:
    """Return (absolute delta, percent delta). Negative means cheaper."""
    delta = round(proposed_total - baseline_total, 2)
    percent = (
        round(delta / baseline_total * 100.0, 1) if baseline_total else 0.0
    )
    return delta, percent
