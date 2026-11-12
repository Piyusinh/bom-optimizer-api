"""Candidate builder: fills Layer R (r_candidate, r_candidate_item, r_candidate_stats) from Layer M and T.

Design: docs/BOM_Optimizer_HLD_LLD.html section 4.2.
  STANDARD / ALTERNATE : one candidate per production version in ds2 (BOM variant from ds1, recipe from ds3);
                         the lowest valid production version is STANDARD, others ALTERNATE. BOM variants that
                         no production version points to are added as ALTERNATE (no recipe).
  HISTORICAL           : completed original process orders grouped by identical final component quantities per
                         base unit (+ recipe). A group matching a master candidate attaches its statistics to it.
Final quantity per component (rule R-05, parameter "mode" in a_rule):
  ACTUAL_ISSUED (default)  net goods issue 261 minus 262 (t_goods_movement), else withdrawn qty, else required qty
  RECIPE_PLUS_ISSUES       required qty + net goods issue (the literal reading of the Excel; may double count)
Rebuild is an upsert on (material, plant, signature): candidate ids stay stable because stored runs point at them;
candidates no longer produced are marked is_current = FALSE.
"""
import hashlib
from collections import Counter, defaultdict
from datetime import date

from psycopg.types.json import Jsonb

COMPLETED_STATUS_TOKENS = ("TECO", "DLV", "CLSD")
HOURS = {"H": 1.0, "HR": 1.0, "MIN": 1 / 60.0, "S": 1 / 3600.0, "SEC": 1 / 3600.0}
MAX_PHANTOM_DEPTH = 5


def sig_qty(q: float) -> float:
    """3 significant digits: tiny weighing noise does not create a new candidate."""
    return float(f"{float(q):.3g}")


def signature(items: dict, recipe_group, recipe_counter) -> str:
    body = ";".join(f"{c}={sig_qty(q)}" for c, q in sorted(items.items())) + f"|{recipe_group or ''}/{recipe_counter or ''}"
    return hashlib.sha256(body.encode()).hexdigest()


def duration_hours(value, unit) -> float | None:
    if value is None:
        return None
    f = HOURS.get((unit or "H").upper())
    return None if f is None else float(value) * f


def _all(conn, sql, params=None):
    return conn.execute(sql, params).fetchall()


def _rule_param(conn, rule_id, key, default):
    r = _all(conn, "SELECT params, active FROM a_rule WHERE rule_id=%s", (rule_id,))
    if r and r[0]["active"] and key in (r[0]["params"] or {}):
        return r[0]["params"][key]
    return default


# ───────────────────────── master candidates (ds1 + ds2 + ds3) ─────────────────────────
def _bom_items(conn, run_id, material, plant, variant, skip_cats, depth=0) -> dict:
    """Components per base unit of one BOM variant, phantoms exploded through their own BOM."""
    rows = _all(conn, "SELECT * FROM ds1_bom_components WHERE run_id=%s AND material=%s AND plant=%s AND bill_of_material_variant=%s "
                      "AND NOT COALESCE(bom_is_marked_for_deletion, FALSE) AND NOT COALESCE(is_marked_for_deletion, FALSE) "
                      "ORDER BY bill_of_material_item_node_number", (run_id, material, plant, variant))
    out: dict = defaultdict(float)
    for r in rows:
        if not r["bill_of_material_component"] or r["component_quantity"] is None:
            continue
        if (r["bill_of_material_item_category"] or "L") in skip_cats:
            continue
        base = float(r["bom_header_quantity_in_base_unit"] or 1) or 1.0
        per_base = float(r["component_quantity"]) / base
        comp = r["bill_of_material_component"]
        if r["is_phantom_item"] and depth < MAX_PHANTOM_DEPTH:
            sub_variants = _all(conn, "SELECT DISTINCT bill_of_material_variant v FROM ds1_bom_components WHERE run_id=%s AND material=%s "
                                      "AND plant=%s ORDER BY 1", (run_id, comp, plant))
            if sub_variants:
                for c2, q2 in _bom_items(conn, run_id, comp, plant, sub_variants[0]["v"], skip_cats, depth + 1).items():
                    out[c2] += per_base * q2
                continue
        out[comp] += per_base
    return dict(out)


def _recipe_hours(conn, run_id, plant, group, counter) -> float | None:
    if not group:
        return None
    ops = _all(conn, "SELECT DISTINCT ON (operation_node) operation_node, standard_process_duration d, standard_process_duration_unit u "
                     "FROM ds3_master_recipe WHERE run_id=%s AND recipe_group=%s AND recipe=%s AND (plant=%s OR plant IS NULL) "
                     "ORDER BY operation_node", (run_id, group, counter, plant))
    hrs = [duration_hours(o["d"], o["u"]) for o in ops]
    return None if not hrs or any(h is None for h in hrs) else round(sum(hrs), 3)


def _master_candidates(conn, run_id, material, plant, skip_cats) -> list[dict]:
    today = date.today()
    pvs = _all(conn, "SELECT * FROM ds2_production_version WHERE run_id=%s AND material=%s AND plant=%s "
                     "AND NOT COALESCE(is_marked_for_deletion, FALSE) ORDER BY production_version", (run_id, material, plant))
    cands, used_variants = [], set()
    standard_given = False
    for pv in pvs:
        variant = pv["bill_of_material_variant"] or "01"
        items = _bom_items(conn, run_id, material, plant, variant, skip_cats)
        if not items:
            continue
        used_variants.add(variant)
        vf, vt = pv["production_version_valid_from"], pv["production_version_valid_to"]
        valid_today = (vf is None or vf <= today) and (vt is None or vt >= today)
        ctype = "STANDARD" if (valid_today and not standard_given) else "ALTERNATE"
        standard_given = standard_given or ctype == "STANDARD"
        cands.append(dict(type=ctype, items=items, bom_variant=variant, pv=pv["production_version"],
                          group=pv["mfg_order_routing_group"], counter=pv["mfg_order_routing_grp_counter"], valid_from=vf, valid_to=vt,
                          bom_ref=_all(conn, "SELECT bill_of_material b FROM ds1_bom_components WHERE run_id=%s AND material=%s AND plant=%s "
                                             "AND bill_of_material_variant=%s LIMIT 1", (run_id, material, plant, variant))[0]["b"],
                          recipe_hours=_recipe_hours(conn, run_id, plant, pv["mfg_order_routing_group"], pv["mfg_order_routing_grp_counter"])))
    for v in _all(conn, "SELECT DISTINCT bill_of_material_variant v, bill_of_material b FROM ds1_bom_components WHERE run_id=%s "
                        "AND material=%s AND plant=%s ORDER BY 1", (run_id, material, plant)):
        if v["v"] in used_variants:
            continue
        items = _bom_items(conn, run_id, material, plant, v["v"], skip_cats)
        if items:
            cands.append(dict(type="ALTERNATE", items=items, bom_variant=v["v"], pv=None, group=None, counter=None,
                              valid_from=None, valid_to=None, bom_ref=v["b"], recipe_hours=None))
    return cands


# ───────────────────────── historical candidates (orders) ─────────────────────────
def _history(conn, material, plant, mode, season_map) -> tuple[dict, Counter]:
    orders = _all(conn, "SELECT * FROM t_process_order WHERE material=%s AND plant=%s AND order_role='ORIGINAL' "
                        "AND COALESCE(target_qty,0) > 0", (material, plant))
    have_links = _all(conn, "SELECT 1 FROM t_rework_link LIMIT 1") != []
    groups: dict = {}
    skipped = Counter()
    for o in orders:
        status = (o["system_status"] or "").upper().split()
        if not (o["delivered_date"] or any(s in COMPLETED_STATUS_TOKENS for s in status)):
            skipped["not_completed"] += 1
            continue
        comps = _all(conn, "SELECT component, required_qty, withdrawn_qty FROM t_process_order_component WHERE order_no=%s", (o["order_no"],))
        issued = {r["material"]: float(r["net"]) for r in _all(
            conn, "SELECT material, SUM(CASE movement_type WHEN '261' THEN qty WHEN '262' THEN -qty ELSE 0 END) net "
                  "FROM t_goods_movement WHERE order_no=%s AND movement_type IN ('261','262') GROUP BY material", (o["order_no"],))}
        final: dict = defaultdict(float)
        for c in comps:
            req = float(c["required_qty"] or 0)
            if c["component"] in issued:
                actual = issued[c["component"]]
            elif c["withdrawn_qty"] is not None:
                actual = float(c["withdrawn_qty"])
            else:
                actual = None
            q = (req + (actual or 0.0)) if mode == "RECIPE_PLUS_ISSUES" else (actual if actual is not None else req)
            final[c["component"]] += q
        # components only seen in goods issues (not on the reservation list)
        for m, net in issued.items():
            if m not in final:
                final[m] = net
        per_base = {c: q / float(o["target_qty"]) for c, q in final.items() if q and q > 0}
        if not per_base:
            skipped["no_components"] += 1
            continue
        sig = signature(per_base, o["recipe_group"], o["recipe_counter"])
        g = groups.setdefault(sig, dict(items=defaultdict(list), orders=[], group=o["recipe_group"], counter=o["recipe_counter"]))
        for c, q in per_base.items():
            g["items"][c].append(q)
        ops = _all(conn, "SELECT SUM(standard_hours) h FROM t_process_order_operation WHERE order_no=%s", (o["order_no"],))
        reworks = (_all(conn, "SELECT COUNT(*) n FROM t_rework_link WHERE original_order_no=%s", (o["order_no"],))[0]["n"]
                   if have_links else None)
        used = o["delivered_date"] or o["actual_start"] or o["planned_start"]
        lead = ((o["delivered_date"] - (o["actual_start"] or o["planned_start"])).days
                if o["delivered_date"] and (o["actual_start"] or o["planned_start"]) else None)
        g["orders"].append(dict(order=o["order_no"], used=used, line=o["production_line"], pv=o["production_version"], bom_ref=o["bom_ref"],
                                hours=float(ops[0]["h"]) if ops and ops[0]["h"] is not None else None, reworks=reworks, lead=lead))
    return groups, skipped


def _stats(orders: list[dict], season_map: dict) -> dict:
    n = len(orders)
    dates = [o["used"] for o in orders if o["used"]]
    rw = [o["reworks"] for o in orders if o["reworks"] is not None]
    hrs = [o["hours"] for o in orders if o["hours"] is not None]
    lead = [o["lead"] for o in orders if o["lead"] is not None]

    def cum(k):  # share of batches finished within k cycles (k-1 reworks): the 78/96/100 strip
        return round(100.0 * sum(1 for r in rw if r <= k - 1) / len(rw), 2) if rw else None
    return dict(times_used=n,
                last_used=max(dates) if dates else None, first_used=min(dates) if dates else None,
                avg_cycles=round(sum(rw) / len(rw), 2) if rw else None,
                c1=cum(1), c2=cum(2), c3=cum(3),
                hours=round(sum(hrs) / len(hrs), 3) if hrs else None,
                lead=round(sum(lead) / len(lead), 2) if lead else None,
                seasons=dict(Counter(season_map.get(d.month) for d in dates if season_map.get(d.month))),
                lines=dict(Counter(o["line"] for o in orders if o["line"])))


# ───────────────────────── build ─────────────────────────
def build(conn, material: str | None = None, plant: str | None = None, user: str = "local") -> dict:
    season_map = {r["month"]: r["season"] for r in _all(conn, "SELECT month, season FROM a_season_map")}
    skip_cats = set(_rule_param(conn, "R-03", "categories", ["T", "N"]))
    mode = _rule_param(conn, "R-05", "mode", "ACTUAL_ISSUED")
    run = _all(conn, "SELECT max(run_id) r FROM job_run WHERE status='COMPLETE'")[0]["r"]
    scope = {"material": material, "plant": plant, "bom_run_id": run, "quantity_mode": mode}
    job_id = conn.execute("INSERT INTO a_extraction_job (job_type, scope, status, started_at) VALUES ('CANDIDATE_BUILD',%s,'RUNNING',now()) "
                          "RETURNING job_id", (Jsonb(scope),)).fetchone()["job_id"]
    conn.commit()
    targets = set()
    if run:
        q, p = "SELECT DISTINCT material, plant FROM ds1_bom_components WHERE run_id=%s", [run]
        targets |= {(r["material"], r["plant"]) for r in _all(conn, q, p)}
    targets |= {(r["material"], r["plant"]) for r in _all(conn, "SELECT DISTINCT material, plant FROM t_process_order WHERE order_role='ORIGINAL'")}
    if material:
        targets = {t for t in targets if t[0] == material}
    if plant:
        targets = {t for t in targets if t[1] == plant}
    summary = dict(job_id=job_id, materials=0, candidates=0, standard=0, alternate=0, historical=0, orders_skipped=Counter(), quantity_mode=mode,
                   bom_run_id=run, warnings=[])
    try:
        for mat, pl in sorted(targets):
            master = _master_candidates(conn, run, mat, pl, skip_cats) if run else []
            groups, skipped = _history(conn, mat, pl, mode, season_map)
            summary["orders_skipped"].update(skipped)
            built = {}
            for m in master:
                sig = signature(m["items"], m["group"], m["counter"])
                if sig in built:                      # identical BOM + recipe under another version: keep first
                    summary["warnings"].append(f"{mat}/{pl}: production version {m['pv']} duplicates {built[sig]['pv']}")
                    continue
                m["sig"], m["history"], m["items_final"] = sig, groups.pop(sig, None), m["items"]
                built[sig] = m
            for sig, g in groups.items():
                avg_items = {c: sum(v) / len(v) for c, v in g["items"].items()}
                pvs = Counter(o["pv"] for o in g["orders"] if o["pv"])
                refs = Counter(o["bom_ref"] for o in g["orders"] if o["bom_ref"])
                built[sig] = dict(type="HISTORICAL", sig=sig, items_final=avg_items, bom_variant=None,
                                  pv=pvs.most_common(1)[0][0] if pvs else None, group=g["group"], counter=g["counter"], valid_from=None,
                                  valid_to=None, bom_ref=refs.most_common(1)[0][0] if refs else None,
                                  recipe_hours=_recipe_hours(conn, run, pl, g["group"], g["counter"]) if run else None, history=g)
            keep_ids = []
            for sig, c in built.items():
                row = conn.execute(
                    "INSERT INTO r_candidate (material, plant, candidate_type, bom_ref, bom_variant, production_version, recipe_group, recipe_counter, "
                    "signature, valid_from, valid_to, built_run_id, is_current) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE) "
                    "ON CONFLICT (material, plant, signature) DO UPDATE SET candidate_type=EXCLUDED.candidate_type, bom_ref=EXCLUDED.bom_ref, "
                    "bom_variant=EXCLUDED.bom_variant, production_version=EXCLUDED.production_version, recipe_group=EXCLUDED.recipe_group, "
                    "recipe_counter=EXCLUDED.recipe_counter, valid_from=EXCLUDED.valid_from, valid_to=EXCLUDED.valid_to, "
                    "built_run_id=EXCLUDED.built_run_id, is_current=TRUE RETURNING candidate_id",
                    (mat, pl, c["type"], c["bom_ref"], c["bom_variant"], c["pv"], c["group"], c["counter"], sig, c["valid_from"], c["valid_to"], job_id)).fetchone()
                cid = row["candidate_id"]
                keep_ids.append(cid)
                conn.execute("DELETE FROM r_candidate_item WHERE candidate_id=%s", (cid,))
                for comp, q in sorted(c["items_final"].items()):
                    conn.execute("INSERT INTO r_candidate_item (candidate_id, component, qty_per_base, unit, is_phantom) VALUES (%s,%s,%s,NULL,FALSE)",
                                 (cid, comp, round(q, 6)))
                st = _stats(c["history"]["orders"], season_map) if c.get("history") else _stats([], season_map)
                hours = st["hours"] if st["hours"] is not None else c.get("recipe_hours")
                conn.execute(
                    "INSERT INTO r_candidate_stats (candidate_id, times_used, last_used_date, first_used_date, avg_reprocess_cycles, cycle1_pct, cycle2_pct, "
                    "cycle3_pct, operation_hours, lead_time_days, season_counts, lines_used, computed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
                    "ON CONFLICT (candidate_id) DO UPDATE SET times_used=EXCLUDED.times_used, last_used_date=EXCLUDED.last_used_date, "
                    "first_used_date=EXCLUDED.first_used_date, avg_reprocess_cycles=EXCLUDED.avg_reprocess_cycles, cycle1_pct=EXCLUDED.cycle1_pct, "
                    "cycle2_pct=EXCLUDED.cycle2_pct, cycle3_pct=EXCLUDED.cycle3_pct, operation_hours=EXCLUDED.operation_hours, "
                    "lead_time_days=EXCLUDED.lead_time_days, season_counts=EXCLUDED.season_counts, lines_used=EXCLUDED.lines_used, computed_at=now()",
                    (cid, st["times_used"], st["last_used"], st["first_used"], st["avg_cycles"], st["c1"], st["c2"], st["c3"], hours, st["lead"],
                     Jsonb(st["seasons"]), Jsonb(st["lines"])))
                summary[c["type"].lower()] += 1
            conn.execute("UPDATE r_candidate SET is_current=FALSE WHERE material=%s AND plant=%s AND NOT (candidate_id = ANY(%s))", (mat, pl, keep_ids))
            conn.commit()
            summary["materials"] += 1
            summary["candidates"] += len(built)
        summary["orders_skipped"] = dict(summary["orders_skipped"])
        conn.execute("UPDATE a_extraction_job SET status='COMPLETE', finished_at=now(), rows_loaded=%s, message=%s WHERE job_id=%s",
                     (summary["candidates"], f"{summary['materials']} materials", job_id))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        conn.execute("UPDATE a_extraction_job SET status='FAILED', finished_at=now(), message=%s WHERE job_id=%s", (str(e)[:500], job_id))
        conn.commit()
        raise
    return summary
