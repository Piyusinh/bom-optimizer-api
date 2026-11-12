"""Production-persona ranking: reads Layers M/T/R, applies rules stored in a_rule, ranks by the
user's ordered criteria (with tie bands) and stores the run in Layer A.

Design: docs/BOM_Optimizer_HLD_LLD.html sections 6 and 7. Deterministic: same data + same rule
set + same request gives the same order (candidate ids come from r_candidate, ties end on
times_used then signature).
"""
import uuid
from datetime import date, datetime

from psycopg.types.json import Jsonb

# Process-order statuses that mean the order is already released / in execution (RTM: the process
# order must be "not released" to be an anchor).
LOCKED_STATUSES = ("REL", "PCNF", "CNF", "TECO", "DLV", "CLSD")
HARD, WARN, FILTER, FLAG, CALC = "HARD", "WARN", "FILTER", "FLAG", "CALC"
LEGAL_ORDER = {"UNKNOWN": 0, "APPROVED": 1, "PENDING": 2, "RESTRICTED": 3}


class RankingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ───────────────────────────── pure helpers (unit-testable) ─────────────────────────────
def band_indexes(values: list, direction: str, pct: float) -> tuple[dict, int]:
    """Map each value to a band number (0 = best). Values within `pct` percent of the band's
    first (best) value share the band, so the next rank criterion decides between them.
    None sorts last. Bands are anchored on the best value, so the result is deterministic."""
    sign = 1.0 if direction == "MIN" else -1.0
    real = sorted({float(v) * sign for v in values if v is not None})
    out, band, start = {}, -1, None
    for v in real:
        if start is None or v > start + abs(start) * pct / 100.0:
            band, start = band + 1, v
        out[v * sign] = band
    return out, band + 1  # (value -> band, index used for None)


def season_of(month: int, season_map: dict) -> str | None:
    return season_map.get(month)


def unit_price(m: dict | None) -> float | None:
    """Price per base unit from MBEW fields. None when unknown (never 0)."""
    if not m:
        return None
    ctrl = m.get("price_control")
    price = m.get("standard_price") if ctrl == "S" else m.get("moving_price") if ctrl == "V" else None
    if price is None:
        price = m.get("moving_price") if m.get("moving_price") is not None else m.get("standard_price")
    if price is None:
        return None
    per = float(m.get("price_unit") or 1) or 1.0
    return float(price) / per


# ───────────────────────────── rule handlers ─────────────────────────────
# Each handler returns a list of problem texts (empty = rule satisfied).
def h_component_active(c, ctx, p):
    blocked = set(p.get("blocked_statuses", []))
    out = []
    for it in c["items"]:
        m = ctx["materials"].get(it["component"])
        if m and (m["deletion_flag"] or m["plant_status"] in blocked or m["cross_plant_status"] in blocked):
            out.append(f"Component {it['component']} is not active in material master")
    return out


def h_component_registered(c, ctx, p):
    return [f"Component {it['component']} is not in material master"
            for it in c["items"] if it["component"] not in ctx["materials"]]


def h_output_material_types(c, ctx, p):
    m = ctx["materials"].get(c["material"])
    if m and m["material_type"] and m["material_type"] not in p.get("types", []):
        return [f"Material type {m['material_type']} is not one of {p.get('types')}"]
    return []


def h_exclude_item_categories(c, ctx, p):
    return []  # r_candidate_item carries no item category yet; handled when the extraction adds it


def h_explode_phantoms(c, ctx, p):
    return []  # phantoms are skipped in costing (see _cost); nothing to reject


def h_final_quantity_261(c, ctx, p):
    return []  # CALC marker: historical candidates are built from final quantities (recipe + 261)


def h_grade_acceptable(c, ctx, p):
    return []  # inactive until the "grade" field is defined


def h_customer_restriction(c, ctx, p):
    return []  # inactive until a data source exists


def h_availability(c, ctx, p):
    qty = ctx["quantity"]
    if not qty:
        return []
    out = []
    for it in c["items"]:
        m = ctx["materials"].get(it["component"])
        if m and m["stock_unrestricted"] is not None and not it["is_phantom"]:
            need = float(it["qty_per_base"]) * qty
            if need > float(m["stock_unrestricted"]):
                out.append(f"Insufficient stock for {it['component']}: need {need:g}, have {float(m['stock_unrestricted']):g}")
    return out


def h_valid_on_start_date(c, ctx, p):
    d = ctx["target_date"]
    if c["valid_from"] and d < c["valid_from"]:
        return [f"Valid from {c['valid_from']}, order starts {d}"]
    if c["valid_to"] and d > c["valid_to"]:
        return [f"Valid until {c['valid_to']}, order starts {d}"]
    return []


def h_line_match(c, ctx, p):
    line = ctx["production_line"]
    if line and c["metrics"]["line_match"] == 0.0:
        return [f"Not run on line {line} historically"]
    return []


def h_legal_status(c, ctx, p):
    worst = "UNKNOWN"
    seen = False
    for key in {c["material"], *[i["component"] for i in c["items"]]}:
        s = ctx["legal"].get(key)
        if s:
            seen = True
            if LEGAL_ORDER.get(s, 0) > LEGAL_ORDER.get(worst, 0) or worst == "UNKNOWN":
                worst = s
    c["legal_status"] = worst if seen else "UNKNOWN"
    return [] if c["legal_status"] == "APPROVED" else [f"Legal status {c['legal_status']}"]


def h_request_constraints(c, ctx, p):
    k, out = ctx["constraints"], []
    for it in c["items"]:
        if it["component"] in (k.get("avoid_components") or []):
            out.append(f"Component {it['component']} is on the avoid list")
        mq = k.get("min_quantity_per_component")
        if mq and float(it["qty_per_base"]) < mq:
            out.append(f"Component {it['component']} quantity below minimum {mq}")
    mx = k.get("max_components")
    if mx and len(c["items"]) > mx:
        out.append(f"{len(c['items'])} components, maximum is {mx}")
    return out


def h_min_occurrences(c, ctx, p):
    if c["candidate_type"] == "HISTORICAL" and (c["stats"].get("times_used") or 0) < p.get("min", 3):
        return [f"Only used {c['stats'].get('times_used') or 0} time(s) historically"]
    return []


def h_cycle_evidence(c, ctx, p):
    return [] if c["stats"].get("avg_reprocess_cycles") is not None else ["No cycle data for this candidate"]


def h_exclude_baseline(c, ctx, p):
    return ["This is the current baseline"] if c["candidate_id"] == ctx["baseline_id"] else []


HANDLERS = {f.__name__[2:]: f for f in (
    h_component_active, h_component_registered, h_output_material_types, h_exclude_item_categories,
    h_explode_phantoms, h_final_quantity_261, h_grade_acceptable, h_customer_restriction, h_availability,
    h_valid_on_start_date, h_line_match, h_legal_status, h_request_constraints, h_min_occurrences,
    h_cycle_evidence, h_exclude_baseline)}


# ───────────────────────────── data access ─────────────────────────────
def _all(conn, sql, params=None):
    return conn.execute(sql, params).fetchall()


def list_criteria(conn) -> list[dict]:
    return _all(conn, "SELECT criterion_code, label, direction, metric_expr, tie_band_pct FROM a_criterion "
                      "WHERE active ORDER BY sort_order")


def _resolve_anchor(conn, req: dict) -> dict:
    plant = req["plant"]
    if req.get("process_order"):
        no = str(req["process_order"]).zfill(12)
        r = _all(conn, "SELECT * FROM t_process_order WHERE order_no=%s AND plant=%s", (no, plant))
        if not r:
            raise RankingError(404, f"Process order {no} not found in plant {plant}")
        o = r[0]
        status = (o["system_status"] or "").upper().split()
        if any(s in LOCKED_STATUSES for s in status):
            raise RankingError(409, f"Process order {no} is already released/closed ({o['system_status']}); "
                                    "only orders not yet released can be an anchor")
        return dict(type="PROCESS_ORDER", value=no, material=o["material"], quantity=o["target_qty"],
                    target_date=o["planned_start"], line=o["production_line"],
                    pv=o["production_version"], bom_ref=o["bom_ref"])
    if req.get("planned_order"):
        r = _all(conn, "SELECT * FROM t_planned_order WHERE planned_order_no=%s AND plant=%s",
                 (req["planned_order"], plant))
        if not r:
            raise RankingError(404, f"Planned order {req['planned_order']} not found in plant {plant}")
        o = r[0]
        return dict(type="PLANNED_ORDER", value=o["planned_order_no"], material=o["material"], quantity=o["qty"],
                    target_date=o["start_date"], line=None, pv=o["production_version"], bom_ref=o["bom_ref"])
    if req.get("material"):
        return dict(type="MATERIAL", value=req["material"], material=req["material"], quantity=None,
                    target_date=None, line=None, pv=None, bom_ref=None)
    raise RankingError(400, "Give a material, a planned order or a process order")


def _validate_criteria(conn, req) -> list[dict]:
    crit = {c["criterion_code"]: c for c in list_criteria(conn)}
    chosen = sorted(req.get("criteria") or [], key=lambda x: x["rank"])
    if not chosen:
        raise RankingError(400, "Choose at least Rank 1")
    if len(chosen) > 4:
        raise RankingError(400, "At most 4 ranking criteria")
    ranks = [c["rank"] for c in chosen]
    codes = [c["criterion"] for c in chosen]
    if len(set(ranks)) != len(ranks) or len(set(codes)) != len(codes):
        raise RankingError(400, "Each rank and each criterion can be used once")
    for c in codes:
        if c not in crit:
            raise RankingError(400, f"Unknown or inactive criterion {c}")
    return [{**crit[c["criterion"]], "rank": c["rank"]} for c in chosen]


def _criterion_value(code: str, m: dict):
    return {"cost_per_unit": m["cost_per_unit"],
            "last_used_date": m["last_used_ordinal"],
            "avg_reprocess_cycles": m["avg_reprocess_cycles"],
            "season_match": m["season_match"],
            "line_match": m["line_match"],
            "operation_hours": m["operation_hours"]}[code]


# ───────────────────────────── main entry ─────────────────────────────
def analyze(conn, req: dict, user: str = "local") -> dict:
    t0 = datetime.now()
    criteria = _validate_criteria(conn, req)
    anchor = _resolve_anchor(conn, req)
    today = date.today()
    target_date = anchor["target_date"] or req.get("target_date") or today
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    quantity = req.get("quantity") or (float(anchor["quantity"]) if anchor["quantity"] else None)
    line = req.get("production_line") or anchor["line"]

    season_map = {r["month"]: r["season"] for r in _all(conn, "SELECT month, season FROM a_season_map")}
    target_season = season_map.get(target_date.month)

    rows = _all(conn, "SELECT c.*, s.times_used, s.last_used_date, s.avg_reprocess_cycles, s.cycle1_pct, s.cycle2_pct, "
                      "s.cycle3_pct, s.operation_hours, s.lead_time_days, s.season_counts, s.lines_used, "
                      "s.computed_at FROM r_candidate c LEFT JOIN r_candidate_stats s USING (candidate_id) "
                      "WHERE c.material=%s AND c.plant=%s AND c.is_current ORDER BY c.candidate_id", (anchor["material"], req["plant"]))
    if not rows:
        return _store(conn, req, anchor, criteria, None, [], [], 0, user, t0, None, target_date, line, quantity,
                      target_season, {"filtered_out": 0, "note": "No candidates for this material and plant"})

    ids = [r["candidate_id"] for r in rows]
    items_by = {}
    for it in _all(conn, "SELECT * FROM r_candidate_item WHERE candidate_id = ANY(%s) ORDER BY candidate_id, component", (ids,)):
        items_by.setdefault(it["candidate_id"], []).append(it)
    comps = {it["component"] for lst in items_by.values() for it in lst} | {anchor["material"]}
    materials = {m["material"]: m for m in _all(conn, "SELECT * FROM m_material WHERE plant=%s AND material = ANY(%s)",
                                                 (req["plant"], list(comps)))}
    legal = {l["material"]: l["status"] for l in _all(
        conn, "SELECT material, status FROM a_legal_status WHERE plant=%s AND (valid_from IS NULL OR valid_from <= %s) "
              "AND (valid_to IS NULL OR valid_to >= %s)", (req["plant"], target_date, target_date))}

    cands = []
    for r in rows:
        items = items_by.get(r["candidate_id"], [])
        # cost: phantoms are exploded through, not costed; a missing price makes the cost unknown (never 0)
        cost, missing = 0.0, []
        for it in items:
            if it["is_phantom"]:
                continue
            up = unit_price(materials.get(it["component"]))
            if up is None:
                missing.append(it["component"])
            else:
                cost += float(it["qty_per_base"]) * up
        sc = r["season_counts"] or {}
        total_s = sum(sc.values())
        lu = r["last_used_date"]
        cands.append({
            "candidate_id": r["candidate_id"], "material": r["material"], "candidate_type": r["candidate_type"],
            "bom_ref": r["bom_ref"], "production_version": r["production_version"], "signature": r["signature"],
            "valid_from": r["valid_from"], "valid_to": r["valid_to"], "items": items, "stats": r,
            "legal_status": None, "violations": [], "warnings": [], "flags": [], "filtered": [],
            "missing_cost": missing,
            "metrics": {
                "cost_per_unit": None if missing or not items else round(cost, 5),
                "avg_reprocess_cycles": None if r["avg_reprocess_cycles"] is None else float(r["avg_reprocess_cycles"]),
                "cycle_strip": [None if r[k] is None else float(r[k]) for k in ("cycle1_pct", "cycle2_pct", "cycle3_pct")],
                "times_used": r["times_used"] or 0,
                "last_used_date": lu.isoformat() if lu else None,
                "last_used_ordinal": lu.toordinal() if lu else None,
                "recency_days": (today - lu).days if lu else None,
                "season_match": round((sc.get(target_season, 0) / total_s), 4) if total_s and target_season else 0.0,
                "line_match": 1.0 if line and (r["lines_used"] or {}).get(line) else 0.0,
                "operation_hours": None if r["operation_hours"] is None else float(r["operation_hours"]),
                "lead_time_days": None if r["lead_time_days"] is None else float(r["lead_time_days"]),
            },
        })

    # baseline: the anchor order's own BOM/version if it matches, otherwise the most used candidate
    base, base_src = None, "MOST_USED"
    if anchor["bom_ref"] or anchor["pv"]:
        for c in cands:
            if (anchor["bom_ref"] is None or c["bom_ref"] == anchor["bom_ref"]) and \
               (anchor["pv"] is None or c["production_version"] == anchor["pv"]):
                base, base_src = c, "ANCHOR_ORDER"
                break
    if base is None:
        base = max(cands, key=lambda c: (c["metrics"]["times_used"], -c["candidate_id"]))

    rs = _all(conn, "SELECT rule_set_id, rule_ids FROM a_rule_set WHERE active ORDER BY version DESC, rule_set_id DESC LIMIT 1")
    rule_set_id = rs[0]["rule_set_id"] if rs else None
    wanted = rs[0]["rule_ids"] if rs else []
    rule_rows = _all(conn, "SELECT * FROM a_rule WHERE active AND rule_id = ANY(%s) ORDER BY exec_order, rule_id", (wanted,))
    ctx = {"materials": materials, "legal": legal, "quantity": quantity, "target_date": target_date,
           "production_line": line, "constraints": req.get("constraints") or {}, "baseline_id": base["candidate_id"],
           "anchor_type": anchor["type"]}

    for c in cands:
        if c["missing_cost"]:
            c["warnings"].append("Cost unknown: no price for " + ", ".join(sorted(set(c["missing_cost"]))))
        for rule in rule_rows:
            h = HANDLERS.get(rule["handler"])
            if h is None:
                c["warnings"].append(f"Rule {rule['rule_id']}: handler '{rule['handler']}' not implemented")
                continue
            problems = h(c, ctx, rule["params"] or {})
            if not problems:
                continue
            kind = rule["rule_type"]
            if ctx["anchor_type"] in (rule["params"] or {}).get("hard_for", []):
                kind = HARD
            tag = f"[{rule['rule_id']}] "
            if kind == HARD:
                c["violations"] += [tag + p for p in problems]
            elif kind == WARN:
                c["warnings"] += [tag + p for p in problems]
            elif kind == FLAG:
                c["flags"] += [tag + p for p in problems]
            elif kind == FILTER:
                c["filtered"] += [tag + p for p in problems]

    kept = [c for c in cands if not c["filtered"]]
    dropped = len(cands) - len(kept)
    for c in kept:
        c["feasible"] = not c["violations"]
        cb = base["metrics"]["cost_per_unit"]
        cc = c["metrics"]["cost_per_unit"]
        c["metrics"]["cost_reduction_pct"] = None if not cb or cc is None else round((cb - cc) / cb * 100, 1)
        c["metrics"]["baseline_cycles"] = base["metrics"]["avg_reprocess_cycles"]

    # ranking: feasible first, then band index per criterion in the user's order, then stable tie-breaks
    feas = [c for c in kept if c["feasible"]]
    bands = {}
    for cr in criteria:
        vals = [_criterion_value(cr["metric_expr"], c["metrics"]) for c in feas]
        bands[cr["criterion_code"]] = band_indexes(vals, cr["direction"], float(cr["tie_band_pct"] or 0))

    def key(c):
        parts = [not c["feasible"]]
        for cr in criteria:
            v = _criterion_value(cr["metric_expr"], c["metrics"])
            bmap, none_band = bands[cr["criterion_code"]]
            parts.append(none_band if v is None or not c["feasible"] else bmap[v])
        parts += [-c["metrics"]["times_used"], c["signature"]]
        return tuple(parts)

    kept.sort(key=key)
    for i, c in enumerate(kept, 1):
        c["rank"] = i
        c["reason"] = _reason(c, criteria, base, base_src)
    return _store(conn, req, anchor, criteria, rule_set_id, kept, [base], dropped, user, t0, base, target_date, line,
                  quantity, target_season, {"filtered_out": dropped, "baseline_source": base_src},
                  data_as_of=max((r["computed_at"] for r in rows if r["computed_at"]), default=None))


def _reason(c, criteria, base, base_src) -> str:
    m, parts = c["metrics"], []
    ref = "the current order's BOM" if base_src == "ANCHOR_ORDER" else "the most-used BOM"
    for cr in criteria:
        code = cr["criterion_code"]
        if code == "COST":
            if m["cost_per_unit"] is None:
                parts.append("cost unknown")
            elif m.get("cost_reduction_pct") is not None:
                p = m["cost_reduction_pct"]
                parts.append(f"{abs(p):g}% {'lower' if p >= 0 else 'higher'} cost than {ref}")
        elif code == "REWORK_CYCLES":
            if m["avg_reprocess_cycles"] is None:
                parts.append("no cycle data")
            else:
                parts.append(f"{m['avg_reprocess_cycles']:g} reprocess cycles vs {m['baseline_cycles']:g}"
                             if m["baseline_cycles"] is not None else f"{m['avg_reprocess_cycles']:g} reprocess cycles")
        elif code == "RECENCY":
            parts.append(f"last used {m['last_used_date']}" if m["last_used_date"] else "never used")
        elif code == "SEASON":
            parts.append(f"{round(m['season_match'] * 100):g}% of runs in the same season")
        elif code == "PRODUCTION_LINE":
            parts.append("run on this line" if m["line_match"] else "not run on this line")
        elif code == "EFFICIENCY":
            parts.append("operation hours unknown" if m["operation_hours"] is None else f"{m['operation_hours']:g} operation hours")
    parts.append(f"used {m['times_used']} time{'s' if m['times_used'] != 1 else ''}")
    if not c["feasible"]:
        parts.append("NOT feasible: " + "; ".join(c["violations"]))
    return ", ".join(parts)


def _card(c):
    m = {k: v for k, v in c["metrics"].items() if k != "last_used_ordinal"}
    return {"rank": c.get("rank"), "candidate_id": c["candidate_id"], "material": c["material"],
            "candidate_type": c["candidate_type"], "production_version": c["production_version"],
            "bom_ref": c["bom_ref"], "feasible": c.get("feasible", True), "violations": c["violations"],
            "warnings": c["warnings"], "flags": c["flags"], "legal_status": c["legal_status"] or "UNKNOWN",
            "metrics": m, "reason": c.get("reason"),
            "components": [{"component": i["component"], "qty_per_base": float(i["qty_per_base"]), "unit": i["unit"],
                            "is_phantom": i["is_phantom"]} for i in c["items"]]}


def _store(conn, req, anchor, criteria, rule_set_id, ranked, base_list, dropped, user, t0, base, target_date, line,
           quantity, season, info, data_as_of=None) -> dict:
    run_id = str(uuid.uuid4())
    duration = int((datetime.now() - t0).total_seconds() * 1000)
    conn.execute(
        "INSERT INTO a_ranking_run (run_id, requested_by, plant, material, planned_order, process_order, production_line, "
        "anchor_type, baseline_candidate_id, target_date, rule_set_id, data_as_of, status, duration_ms) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (run_id, user, req["plant"], anchor["material"], req.get("planned_order"), req.get("process_order"), line,
         anchor["type"], base["candidate_id"] if base else None, target_date, rule_set_id, data_as_of, "DONE", duration))
    for cr in criteria:
        conn.execute("INSERT INTO a_ranking_run_criterion VALUES (%s,%s,%s)", (run_id, cr["rank"], cr["criterion_code"]))
    cards = [_card(c) for c in ranked]
    for c, card in zip(ranked, cards):
        conn.execute(
            "INSERT INTO a_ranking_result (run_id, candidate_id, rank_no, feasible, violations, warnings, metrics, reason_text, legal_status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, c["candidate_id"], c["rank"], c["feasible"], Jsonb(c["violations"]),
             Jsonb(c["warnings"] + c["flags"]), Jsonb(card["metrics"]), c["reason"], card["legal_status"]))
    conn.execute("INSERT INTO a_audit_log (event_type, entity_type, entity_id, actor, details) VALUES (%s,%s,%s,%s,%s)",
                 ("analysis_run", "a_ranking_run", run_id, user,
                  Jsonb({"plant": req["plant"], "material": anchor["material"], "candidates": len(cards),
                         "criteria": [c["criterion_code"] for c in criteria]})))
    conn.commit()
    return {"run_id": run_id,
            "anchor": {"type": anchor["type"], "value": anchor["value"], "material": anchor["material"],
                       "target_date": target_date.isoformat() if target_date else None, "season": season,
                       "quantity": quantity, "production_line": line},
            "criteria": [{"rank": c["rank"], "criterion": c["criterion_code"], "tie_band_pct": float(c["tie_band_pct"] or 0)}
                         for c in criteria],
            "rule_set_id": rule_set_id,
            "baseline": _card(base) if base else None,
            "candidates_count": len(cards), **info, "results": cards}


def get_run(conn, run_id: str) -> dict:
    run = _all(conn, "SELECT * FROM a_ranking_run WHERE run_id=%s", (run_id,))
    if not run:
        raise RankingError(404, "Run not found")
    crit = _all(conn, "SELECT rank_no, criterion_code FROM a_ranking_run_criterion WHERE run_id=%s ORDER BY rank_no", (run_id,))
    res = _all(conn, "SELECT candidate_id, rank_no, feasible, violations, warnings, metrics, reason_text, legal_status "
                     "FROM a_ranking_result WHERE run_id=%s ORDER BY rank_no", (run_id,))
    return {"run": run[0], "criteria": crit, "results": res}
