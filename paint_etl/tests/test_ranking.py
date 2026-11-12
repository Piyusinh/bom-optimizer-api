"""Integration test for app/ranking.py. Seeds namespaced fake rows (plant TS01), asserts, then deletes them.
Run:  <python> -I -X utf8 tests\\test_ranking.py   (database container must be running)"""
import hashlib
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from psycopg.types.json import Jsonb  # noqa: E402
from app import ranking  # noqa: E402
from app.db import apply_schema, connect  # noqa: E402

P, M = "TS01", "TEST_F1"
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


# ---- pure unit tests of the helpers
b, nb = ranking.band_indexes([18, 18.2, 25, None], "MIN", 2.0)
check("band: 18 and 18.2 share a band at 2%", b[18.0] == b[18.2] == 0 and b[25.0] == 1 and nb == 2, (b, nb))
b, _ = ranking.band_indexes([5, 4, 1], "MAX", 0)
check("band MAX: highest value is band 0", b[5.0] == 0 and b[4.0] == 1 and b[1.0] == 2, b)
check("unit_price: V uses moving price / price unit",
      abs(ranking.unit_price({"price_control": "V", "moving_price": 50, "standard_price": 99, "price_unit": 10}) - 5.0) < 1e-9)
check("unit_price: no price -> None, never 0",
      ranking.unit_price({"price_control": "S", "standard_price": None, "moving_price": None}) is None)


def cleanup(conn):
    conn.rollback()
    conn.execute("DELETE FROM a_audit_log WHERE entity_id IN (SELECT run_id::text FROM a_ranking_run WHERE plant=%s)", (P,))
    conn.execute("DELETE FROM a_ranking_result WHERE run_id IN (SELECT run_id FROM a_ranking_run WHERE plant=%s)", (P,))
    conn.execute("DELETE FROM a_ranking_run_criterion WHERE run_id IN (SELECT run_id FROM a_ranking_run WHERE plant=%s)", (P,))
    conn.execute("DELETE FROM a_ranking_run WHERE plant=%s", (P,))
    conn.execute("DELETE FROM r_candidate WHERE plant=%s", (P,))
    conn.execute("DELETE FROM t_process_order WHERE plant=%s", (P,))
    conn.execute("DELETE FROM a_legal_status WHERE plant=%s", (P,))
    conn.execute("DELETE FROM m_material WHERE plant=%s", (P,))
    conn.commit()


def seed(conn):
    for mat, mt, price, deleted in [(M, "FERT", None, False), ("TEST_R1", "ROH", 10, False), ("TEST_R2", "ROH", 5, False),
                                    ("TEST_R3", "ROH", 2, False), ("TEST_R4", "ROH", None, False), ("TEST_R5", "ROH", 1, True)]:
        conn.execute("INSERT INTO m_material (material, plant, material_type, deletion_flag, price_control, moving_price, "
                     "price_unit, stock_unrestricted) VALUES (%s,%s,%s,%s,'V',%s,1,1000)", (mat, P, mt, deleted, price))
    conn.execute("INSERT INTO a_legal_status (material, plant, status) VALUES ('TEST_R3',%s,'PENDING')", (P,))
    # key: type, items, times_used, cycles, bom_ref, production_version, season_counts, lines_used
    defs = {
        "A": ("STANDARD", {"TEST_R1": 1.0, "TEST_R2": 2.0}, 10, 6, "B1", "0001", {"WINTER": 2, "SUMMER": 8}, {"LINE01": 10}),
        "B": ("HISTORICAL", {"TEST_R1": 0.6, "TEST_R2": 2.0, "TEST_R3": 1.0}, 5, 3, "B2", "0002", {"WINTER": 5}, {"LINE01": 5}),
        "C": ("HISTORICAL", {"TEST_R1": 0.6, "TEST_R2": 2.0, "TEST_R3": 1.1}, 3, 2, "B3", "0003", {"WINTER": 1, "SUMMER": 2}, {"LINE02": 3}),
        "D": ("HISTORICAL", {"TEST_R1": 0.5, "TEST_R4": 1.0}, 1, 1, "B4", "0004", {}, {}),
        "E": ("HISTORICAL", {"TEST_R1": 0.4, "TEST_R5": 1.0}, 4, None, "B5", "0005", {}, {}),
    }
    ids = {}
    for k, (ctype, items, used, cyc, bom, pv, sc, ln) in defs.items():
        sig = hashlib.sha256((k + str(items)).encode()).hexdigest()
        cid = conn.execute("INSERT INTO r_candidate (material, plant, candidate_type, bom_ref, production_version, signature) "
                           "VALUES (%s,%s,%s,%s,%s,%s) RETURNING candidate_id", (M, P, ctype, bom, pv, sig)).fetchone()["candidate_id"]
        ids[k] = cid
        for comp, q in items.items():
            conn.execute("INSERT INTO r_candidate_item (candidate_id, component, qty_per_base, unit) VALUES (%s,%s,%s,'KG')", (cid, comp, q))
        conn.execute("INSERT INTO r_candidate_stats (candidate_id, times_used, last_used_date, avg_reprocess_cycles, cycle1_pct, "
                     "cycle2_pct, cycle3_pct, operation_hours, season_counts, lines_used) VALUES (%s,%s,%s,%s,78,96,100,%s,%s,%s)",
                     (cid, used, date(2026, 6, 1), cyc, 4.0, Jsonb(sc), Jsonb(ln)))
    conn.execute("INSERT INTO t_process_order (order_no, plant, material, system_status, planned_start, target_qty, "
                 "production_version, bom_ref, production_line) VALUES ('TS0000000001',%s,%s,'CRTD',%s,500,'0001','B1','LINE01')",
                 (P, M, date(2026, 12, 15)))
    conn.execute("INSERT INTO t_process_order (order_no, plant, material, system_status, planned_start, target_qty) "
                 "VALUES ('TS0000000002',%s,%s,'REL',%s,500)", (P, M, date(2026, 12, 15)))
    conn.commit()
    return ids


def req(**kw):
    return {"plant": P, "material": M, "constraints": {},
            "criteria": [{"rank": 1, "criterion": "COST"}, {"rank": 2, "criterion": "REWORK_CYCLES"}], **kw}


def order_of(res):
    return [r["candidate_id"] for r in res["results"]]


conn = connect()
apply_schema()
cleanup(conn)
try:
    ids = seed(conn)
    inv = {v: k for k, v in ids.items()}
    res = ranking.analyze(conn, req(), "test")
    names = [inv[c] for c in order_of(res)]
    check("baseline (most used) A is excluded from proposals by R-16",
          "A" not in names and inv[res["baseline"]["candidate_id"]] == "A", names)
    check("cost rank 1, cycles rank 2: tie band lets C (2 cycles) beat B (3 cycles)", names == ["C", "B", "D", "E"], names)
    b = next(r for r in res["results"] if inv[r["candidate_id"]] == "B")
    check("cost reduction vs baseline is 10% for B (18 vs 20)", b["metrics"]["cost_reduction_pct"] == 10.0, b["metrics"])
    d = next(r for r in res["results"] if inv[r["candidate_id"]] == "D")
    check("missing price -> cost unknown with a warning, not zero",
          d["metrics"]["cost_per_unit"] is None and any("Cost unknown" in w for w in d["warnings"]), d)
    e = next(r for r in res["results"] if inv[r["candidate_id"]] == "E")
    check("deleted component makes E infeasible and last",
          (not e["feasible"]) and e["rank"] == 4 and any("R-01" in v for v in e["violations"]), e["violations"])
    check("legal PENDING flows to B; unknown to D", b["legal_status"] == "PENDING" and d["legal_status"] == "UNKNOWN",
          (b["legal_status"], d["legal_status"]))
    check("R-14 warns for D used once", any("R-14" in w for w in d["warnings"]), d["warnings"])
    check("cycle strip returned", b["metrics"]["cycle_strip"] == [78.0, 96.0, 100.0])

    res2 = ranking.analyze(conn, req(criteria=[{"rank": 1, "criterion": "REWORK_CYCLES"}, {"rank": 2, "criterion": "COST"}]), "test")
    check("cycles rank 1 gives D, C, B, E", [inv[c] for c in order_of(res2)] == ["D", "C", "B", "E"],
          [inv[c] for c in order_of(res2)])

    res3 = ranking.analyze(conn, req(constraints={"avoid_components": ["TEST_R3"]}), "test")
    feas = {inv[r["candidate_id"]]: r["feasible"] for r in res3["results"]}
    check("avoid-list makes B and C infeasible", feas["B"] is False and feas["C"] is False and feas["D"] is True, feas)

    res4 = ranking.analyze(conn, {"plant": P, "process_order": "TS0000000001",
                                  "criteria": [{"rank": 1, "criterion": "SEASON"}]}, "test")
    check("process order anchor: baseline is the order's own BOM (A), season WINTER",
          res4["baseline_source"] == "ANCHOR_ORDER" and res4["anchor"]["season"] == "WINTER"
          and inv[res4["baseline"]["candidate_id"]] == "A", (res4["baseline_source"], res4["anchor"]))
    check("season criterion ranks B (100% winter) first", inv[res4["results"][0]["candidate_id"]] == "B",
          [inv[r["candidate_id"]] for r in res4["results"]])

    saved = ranking.get_run(conn, res["run_id"])
    check("run stored with criteria and results",
          len(saved["results"]) == 4 and [c["criterion_code"] for c in saved["criteria"]] == ["COST", "REWORK_CYCLES"])
    check("audit log entry written",
          conn.execute("SELECT count(*) n FROM a_audit_log WHERE entity_id=%s", (res["run_id"],)).fetchone()["n"] == 1)

    res5 = ranking.analyze(conn, req(), "test")
    check("same input gives the same order (deterministic)", order_of(res5) == order_of(res))

    errors = [("locked order -> 409", {"plant": P, "process_order": "TS0000000002", "criteria": [{"rank": 1, "criterion": "COST"}]}, 409),
              ("unknown order -> 404", {"plant": P, "process_order": "000000000999", "criteria": [{"rank": 1, "criterion": "COST"}]}, 404),
              ("bad criterion -> 400", req(criteria=[{"rank": 1, "criterion": "NOPE"}]), 400),
              ("duplicate criterion -> 400", req(criteria=[{"rank": 1, "criterion": "COST"}, {"rank": 2, "criterion": "COST"}]), 400),
              ("no anchor -> 400", {"plant": P, "criteria": [{"rank": 1, "criterion": "COST"}]}, 400)]
    for name, r, code in errors:
        try:
            ranking.analyze(conn, r, "test")
            check(name, False, "no error raised")
        except ranking.RankingError as ex:
            conn.rollback()
            check(name, ex.status == code, ex.status)

    none = ranking.analyze(conn, req(material="NO_SUCH"), "test")
    check("no candidates -> empty result, no crash", none["candidates_count"] == 0 and none["results"] == [])
finally:
    cleanup(conn)
    left = conn.execute("SELECT (SELECT count(*) FROM r_candidate WHERE plant=%s) + (SELECT count(*) FROM a_ranking_run WHERE plant=%s) + "
                        "(SELECT count(*) FROM m_material WHERE plant=%s) n", (P, P, P)).fetchone()["n"]
    print("test rows left behind:", left)
    conn.close()
print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
