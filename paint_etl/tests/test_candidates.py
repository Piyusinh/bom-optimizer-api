"""Integration test for app/candidates.py (and the hand-off to ranking). Seeds namespaced fake source rows
(plant TS02, material TB_F1), builds candidates, asserts, ranks, then deletes everything it created.
Run:  <python> -I -X utf8 tests\\test_candidates.py"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from psycopg.types.json import Jsonb  # noqa: E402
from app import candidates, ranking  # noqa: E402
from app.db import apply_schema, connect  # noqa: E402

P, F = "TS02", "TB_F1"
fails = []
RUN = None


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


check("sig_qty merges weighing noise", candidates.sig_qty(0.4001) == candidates.sig_qty(0.4004) == 0.4)
check("signature is order-independent and recipe-aware",
      candidates.signature({"A": 1, "B": 2}, "G", "01") == candidates.signature({"B": 2, "A": 1}, "G", "01")
      and candidates.signature({"A": 1}, "G", "01") != candidates.signature({"A": 1}, "G", "02"))
check("duration: 30 MIN = 0.5 h, unknown unit -> None",
      candidates.duration_hours(30, "MIN") == 0.5 and candidates.duration_hours(1, "D") is None)


def cleanup(conn):
    conn.rollback()
    conn.execute("DELETE FROM a_audit_log WHERE entity_id IN (SELECT run_id::text FROM a_ranking_run WHERE plant=%s)", (P,))
    conn.execute("DELETE FROM a_ranking_result WHERE run_id IN (SELECT run_id FROM a_ranking_run WHERE plant=%s)", (P,))
    conn.execute("DELETE FROM a_ranking_run_criterion WHERE run_id IN (SELECT run_id FROM a_ranking_run WHERE plant=%s)", (P,))
    conn.execute("DELETE FROM a_ranking_run WHERE plant=%s", (P,))
    conn.execute("DELETE FROM r_candidate WHERE plant=%s", (P,))
    conn.execute("DELETE FROM a_extraction_job WHERE job_type='CANDIDATE_BUILD' AND scope->>'material'=%s", (F,))
    conn.execute("DELETE FROM t_rework_link WHERE original_order_no LIKE 'TB0%'")
    conn.execute("DELETE FROM t_goods_movement WHERE order_no LIKE 'TB0%'")
    conn.execute("DELETE FROM t_process_order_component WHERE order_no LIKE 'TB0%'")
    conn.execute("DELETE FROM t_process_order_operation WHERE order_no LIKE 'TB0%'")
    conn.execute("UPDATE t_process_order SET parent_order_no=NULL WHERE plant=%s", (P,))
    conn.execute("DELETE FROM t_process_order WHERE plant=%s", (P,))
    conn.execute("DELETE FROM m_material WHERE plant=%s", (P,))
    conn.execute("DELETE FROM job_run WHERE selection_snapshot->>'test'='candidates'")   # cascades ds1/ds2/ds3
    conn.commit()


def bom(conn, run, mat, variant, bmeng, items):
    for i, (comp, qty, cat, phantom) in enumerate(items, 1):
        conn.execute("INSERT INTO ds1_bom_components (run_id, material, plant, bill_of_material_variant_usage, bill_of_material, "
                     "bill_of_material_variant, bom_header_quantity_in_base_unit, bom_header_base_unit, bill_of_material_item_node_number, "
                     "bill_of_material_component, component_quantity, component_unit, bill_of_material_item_category, is_phantom_item, "
                     "bom_is_marked_for_deletion, is_marked_for_deletion) VALUES (%s,%s,%s,'1',%s,%s,%s,'KG',%s,%s,%s,'KG',%s,%s,FALSE,FALSE)",
                     (run, mat, P, "B" + mat[-4:] + variant, variant, bmeng, f"{i:04d}", comp, qty, cat, phantom))


def seed(conn):
    global RUN
    RUN = conn.execute("INSERT INTO job_run (selection_snapshot, status) VALUES (%s,'COMPLETE') RETURNING run_id",
                       (Jsonb({"test": "candidates"}),)).fetchone()["run_id"]
    bom(conn, RUN, F, "01", 100, [("TB_R1", 40, "L", False), ("TB_R2", 60, "L", False), ("TB_PH", 10, "L", True), ("TB_TXT", 1, "T", False)])
    bom(conn, RUN, F, "02", 100, [("TB_R1", 35, "L", False), ("TB_R2", 65, "L", False)])
    bom(conn, RUN, "TB_PH", "01", 10, [("TB_R3", 5, "L", False), ("TB_R4", 5, "L", False)])
    for pv, var, grp_ctr, vf in [("0001", "01", "01", date(2020, 1, 1)), ("0002", "02", "02", date(2020, 1, 1))]:
        conn.execute("INSERT INTO ds2_production_version (run_id, material, plant, production_version, bill_of_material_variant, "
                     "mfg_order_routing_group, mfg_order_routing_grp_counter, production_version_valid_from) VALUES (%s,%s,%s,%s,%s,'G0000001',%s,%s)",
                     (RUN, F, P, pv, var, grp_ctr, vf))
    for rec, ops in [("01", [("0010", 2, "H", "T1"), ("0010", 2, "H", "T2"), ("0020", 30, "MIN", "T1")]), ("02", [("0010", 3, "H", "T1")])]:
        for node, dur, unit, comp in ops:   # one row per component, so the operation repeats: must not be double counted
            conn.execute("INSERT INTO ds3_master_recipe (run_id, recipe_group, recipe, plant, operation_group, operation_node, "
                         "standard_process_duration, standard_process_duration_unit, bill_of_material_component) "
                         "VALUES (%s,'G0000001',%s,%s,'OG',%s,%s,%s,%s)", (RUN, rec, P, node, dur, unit, comp))
    for mat, price in [("TB_R1", 10), ("TB_R2", 5), ("TB_R3", 2), ("TB_R4", 4)]:
        conn.execute("INSERT INTO m_material (material, plant, material_type, price_control, moving_price, price_unit) VALUES (%s,%s,'ROH','V',%s,1)",
                     (mat, P, price))

    def order(no, deliv, start, line, pv, grp, cnt, role="ORIGINAL", status="DLV", delivered=True):
        conn.execute("INSERT INTO t_process_order (order_no, plant, material, system_status, planned_start, delivered_date, target_qty, "
                     "production_version, recipe_group, recipe_counter, production_line, order_role) VALUES (%s,%s,%s,%s,%s,%s,1000,%s,%s,%s,%s,%s)",
                     (no, P, F, status, start, deliv if delivered else None, pv, grp, cnt, line, role))

    def comp(no, item, c, req, wd):
        conn.execute("INSERT INTO t_process_order_component (order_no, item_no, component, required_qty, withdrawn_qty) VALUES (%s,%s,%s,%s,%s)",
                     (no, item, c, req, wd))
    std = [("TB_R1", 400), ("TB_R2", 600), ("TB_R3", 50), ("TB_R4", 50)]
    order("TB0000000001", date(2026, 1, 10), date(2026, 1, 5), "L1", "0001", "G0000001", "01")
    order("TB0000000002", date(2026, 7, 10), date(2026, 7, 5), "L1", "0001", "G0000001", "01")
    order("TB0000000003", date(2026, 1, 20), date(2026, 1, 15), "L2", "0001", "G0000001", "01")
    for i, (c, q) in enumerate(std, 1):
        comp("TB0000000001", f"{i:04d}", c, q, None)        # O1: actuals come from goods movements
        comp("TB0000000002", f"{i:04d}", c, q - 1, q)       # O2: withdrawn qty on the reservation
        comp("TB0000000003", f"{i:04d}", c, q, None)        # O3: required only
    for c, q in [("TB_R1", 300), ("TB_R1", 100), ("TB_R2", 600), ("TB_R3", 50), ("TB_R4", 50)]:
        conn.execute("INSERT INTO t_goods_movement (order_no, movement_type, material, qty) VALUES ('TB0000000001','261',%s,%s)", (c, q))
    conn.execute("INSERT INTO t_process_order_operation (order_no, operation_no, standard_hours) VALUES ('TB0000000001','0010',2.0),('TB0000000001','0020',0.5)")
    # historical variant: R1 420 (450 issued - 30 reversed), R2 580
    for no in ("TB0000000004", "TB0000000005"):
        order(no, date(2026, 3, 10), date(2026, 3, 8), "L1", "0001", "G0000001", "01")
        comp(no, "0001", "TB_R1", 400, None)
        comp(no, "0002", "TB_R2", 580, 580)
    for no in ("TB0000000004", "TB0000000005"):
        conn.execute("INSERT INTO t_goods_movement (order_no, movement_type, material, qty) VALUES (%s,'261','TB_R1',450),(%s,'262','TB_R1',30)", (no, no))
    order("TB0000000006", None, date(2026, 4, 1), "L1", "0001", "G0000001", "01", status="REL", delivered=False)   # not completed -> excluded
    comp("TB0000000006", "0001", "TB_R1", 400, None)
    for rw, orig in [("TB000000001A", "TB0000000001"), ("TB000000001B", "TB0000000001"), ("TB000000003A", "TB0000000003")]:
        order(rw, date(2026, 2, 1), date(2026, 1, 25), "L1", "0001", "G0000001", "01", role="REWORK")
        conn.execute("INSERT INTO t_rework_link (rework_order_no, original_order_no, cycle_no, link_source) VALUES (%s,%s,1,'SAP_LINK')", (rw, orig))
    conn.commit()


conn = connect()
apply_schema()
cleanup(conn)
try:
    seed(conn)
    s = candidates.build(conn, material=F, plant=P)
    check("3 candidates: 1 standard, 1 alternate, 1 historical",
          (s["candidates"], s["standard"], s["alternate"], s["historical"]) == (3, 1, 1, 1), s)
    check("only completed orders count (1 not-completed skipped)", s["orders_skipped"].get("not_completed") == 1, s["orders_skipped"])
    rows = {r["candidate_type"]: r for r in conn.execute(
        "SELECT c.*, s.* FROM r_candidate c JOIN r_candidate_stats s USING (candidate_id) WHERE c.material=%s AND c.plant=%s", (F, P)).fetchall()}
    check("types: 0001 STANDARD, 0002 ALTERNATE", rows["STANDARD"]["production_version"] == "0001" and rows["ALTERNATE"]["production_version"] == "0002",
          {k: v["candidate_type"] for k, v in rows.items()})

    def items(cid):
        return {r["component"]: float(r["qty_per_base"]) for r in conn.execute("SELECT * FROM r_candidate_item WHERE candidate_id=%s", (cid,)).fetchall()}
    std = items(rows["STANDARD"]["candidate_id"])
    check("phantom exploded, text item dropped: R1 .4 R2 .6 R3 .05 R4 .05",
          std == {"TB_R1": 0.4, "TB_R2": 0.6, "TB_R3": 0.05, "TB_R4": 0.05}, std)
    check("alternate BOM per base unit", items(rows["ALTERNATE"]["candidate_id"]) == {"TB_R1": 0.35, "TB_R2": 0.65})
    st = rows["STANDARD"]
    check("history merged into STANDARD: used 3 times", st["times_used"] == 3, st["times_used"])
    check("avg reprocess cycles = (2+0+1)/3 = 1.0", float(st["avg_reprocess_cycles"]) == 1.0, st["avg_reprocess_cycles"])
    check("cycle strip 33.33 / 66.67 / 100", [float(st["cycle1_pct"]), float(st["cycle2_pct"]), float(st["cycle3_pct"])] == [33.33, 66.67, 100.0],
          [st["cycle1_pct"], st["cycle2_pct"], st["cycle3_pct"]])
    check("season counts WINTER 2, MONSOON 1", st["season_counts"] == {"WINTER": 2, "MONSOON": 1}, st["season_counts"])
    check("lines L1 2, L2 1", st["lines_used"] == {"L1": 2, "L2": 1}, st["lines_used"])
    check("lead time 5 days", float(st["lead_time_days"]) == 5.0, st["lead_time_days"])
    check("operation hours from order operations (2.5), last used 2026-07-10",
          float(st["operation_hours"]) == 2.5 and st["last_used_date"] == date(2026, 7, 10), (st["operation_hours"], st["last_used_date"]))
    alt = rows["ALTERNATE"]
    check("alternate: unused, no cycle evidence, recipe hours 3.0",
          alt["times_used"] == 0 and alt["avg_reprocess_cycles"] is None and float(alt["operation_hours"]) == 3.0, (alt["times_used"], alt["operation_hours"]))
    hist = rows["HISTORICAL"]
    check("historical variant: net issue 261-262 gives R1 .42, R2 .58, used 2",
          items(hist["candidate_id"]) == {"TB_R1": 0.42, "TB_R2": 0.58} and hist["times_used"] == 2, (items(hist["candidate_id"]), hist["times_used"]))
    check("historical operation hours fall back to recipe (2.5, duplicates not double counted)", float(hist["operation_hours"]) == 2.5, hist["operation_hours"])
    check("historical cycles 0 (links exist, none for these orders)", float(hist["avg_reprocess_cycles"]) == 0.0, hist["avg_reprocess_cycles"])

    # idempotent rebuild keeps ids
    ids_before = sorted(r["candidate_id"] for r in rows.values())
    candidates.build(conn, material=F, plant=P)
    ids_after = sorted(r["candidate_id"] for r in conn.execute("SELECT candidate_id FROM r_candidate WHERE material=%s AND plant=%s AND is_current", (F, P)).fetchall())
    check("rebuild keeps candidate ids and creates no duplicates", ids_before == ids_after, (ids_before, ids_after))
    # a vanished source marks the candidate not current
    conn.execute("DELETE FROM ds2_production_version WHERE run_id=%s AND production_version='0002'", (RUN,))
    conn.execute("DELETE FROM ds1_bom_components WHERE run_id=%s AND material=%s AND bill_of_material_variant='02'", (RUN, F))
    conn.commit()
    candidates.build(conn, material=F, plant=P)
    cur = conn.execute("SELECT count(*) n FROM r_candidate WHERE material=%s AND plant=%s AND is_current", (F, P)).fetchone()["n"]
    gone = conn.execute("SELECT count(*) n FROM r_candidate WHERE material=%s AND plant=%s AND NOT is_current", (F, P)).fetchone()["n"]
    check("removed alternate is kept but marked not current", cur == 2 and gone == 1, (cur, gone))
    check("build jobs logged as COMPLETE", conn.execute("SELECT count(*) n FROM a_extraction_job WHERE job_type='CANDIDATE_BUILD' AND status='COMPLETE' "
                                                         "AND scope->>'material'=%s", (F,)).fetchone()["n"] == 3)

    # hand-off to ranking: standard is most used -> baseline; the historical variant is the proposal
    res = ranking.analyze(conn, {"plant": P, "material": F, "constraints": {}, "criteria": [{"rank": 1, "criterion": "COST"}]}, "test")
    check("ranking reads built candidates; baseline = standard; one proposal",
          res["baseline"]["production_version"] == "0001" and res["candidates_count"] == 1, (res["baseline"] and res["baseline"]["production_version"], res["candidates_count"]))
    r1 = res["results"][0]
    check("cost per unit computed from prices (.42*10 + .58*5 = 7.1) vs baseline 7.3",
          r1["metrics"]["cost_per_unit"] == 7.1 and res["baseline"]["metrics"]["cost_per_unit"] == 7.3, (r1["metrics"]["cost_per_unit"], res["baseline"]["metrics"]["cost_per_unit"]))
finally:
    cleanup(conn)
    left = conn.execute("SELECT (SELECT count(*) FROM r_candidate WHERE plant=%s) + (SELECT count(*) FROM t_process_order WHERE plant=%s) + "
                        "(SELECT count(*) FROM job_run WHERE selection_snapshot->>'test'='candidates') + (SELECT count(*) FROM m_material WHERE plant=%s) n",
                        (P, P, P)).fetchone()["n"]
    print("test rows left behind:", left)
    conn.close()
print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
