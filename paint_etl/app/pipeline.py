"""File -> database pipeline.

Steps for every sheet of every file:
  1. detect which dataset it is from its column headers
  2. stream the rows, clean each one (pad order numbers, parse dates, ...)
  3. bulk load with COPY inside ONE transaction (all-or-nothing per sheet)
  4. rebuild consolidated_order_summary (done by the worker when the inbox is empty)
"""
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Callable, Iterator, Optional

import openpyxl

from . import config
from .db import connect


# ───────────────────────── value cleaners ─────────────────────────

class RowError(ValueError):
    pass


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def t(maxlen: int, required: bool = True):
    def f(v):
        if _blank(v):
            if required:
                raise RowError("required text value is empty")
            return None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        s = str(v).strip()
        if len(s) > maxlen:
            raise RowError(f"text longer than {maxlen}: {s[:30]}...")
        return s
    return f


def aufnr(v):
    """SAP order numbers are 12 digits with leading zeros; Excel drops the zeros."""
    if _blank(v):
        raise RowError("order number is empty")
    s = str(int(v)) if isinstance(v, (int, float)) else str(v).strip()
    if not s.isdigit() or len(s) > 12:
        raise RowError(f"bad order number: {s}")
    return s.zfill(12)


def num(scale: int, required: bool = True, default=None):
    def f(v):
        if _blank(v):
            if default is not None:
                return default
            if required:
                raise RowError("required number is empty")
            return None
        try:
            return round(float(v), scale)
        except (TypeError, ValueError):
            raise RowError(f"not a number: {v!r}")
    return f


def integer(required: bool = True):
    def f(v):
        if _blank(v):
            if required:
                raise RowError("required integer is empty")
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            raise RowError(f"not an integer: {v!r}")
    return f


def date(required: bool = True):
    def f(v):
        if _blank(v):
            if required:
                raise RowError("required date is empty")
            return None
        if isinstance(v, dt.datetime):
            return v.date()
        if isinstance(v, dt.date):
            return v
        s = str(v).strip()
        for fmt in ("%Y.%m.%d", "%Y-%m-%d", "%d.%m.%Y", "%Y%m%d", "%m/%d/%Y"):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except ValueError:
                pass
        raise RowError(f"unrecognised date: {s}")
    return f


def time_(v):
    if _blank(v):
        return None
    if isinstance(v, dt.datetime):
        return v.time()
    if isinstance(v, dt.time):
        return v
    s = str(v).strip()
    for fmt in ("%H:%M:%S", "%H%M%S", "%H:%M"):
        try:
            return dt.datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    raise RowError(f"unrecognised time: {s}")


# ───────────────────────── dataset definitions ─────────────────────────
# columns: (source header, target column, cleaner)

DATASETS: dict[str, dict] = {
    "ORDER_BASE": {
        "table": "stg_order_base",
        "mode": "upsert",
        "key": "aufnr",
        "columns": [
            ("AUFNR", "aufnr", aufnr),
            ("AUTYP", "autyp", integer()),
            ("WERKS", "werks", t(4)),
            ("OBJNR", "objnr", t(22)),
            ("MATNR", "matnr", t(18)),
            ("MAKTX", "maktx", t(40, False)),
            ("GSTRP", "gstrp", date()),
            ("BATCH_SIZE_QUANTITY", "batch_size_quantity", num(3)),
            ("BASE_UNIT_OF_MEASURE", "base_unit_of_measure", t(3)),
            ("MATERIAL_COST", "material_cost", num(3, False, 0)),
            ("LABOR_COST", "labor_cost", num(3, False, 0)),
        ],
    },
    "ORDER_REWORK": {
        "table": "stg_order_rework",
        "mode": "append",
        "columns": [
            ("AUFNR", "aufnr", aufnr),
            ("MBLNR", "mblnr", t(10)),
            ("MJAHR", "mjahr", integer()),
            ("DMBTR", "dmbtr", num(2)),
            ("BWART", "bwart", t(3)),
            ("BUDAT_MKPF", "budat", date()),
            ("CPUDT_MKPF", "cpudt", date(False)),
            ("CPUTM_MKPF", "cputm", time_),
            ("XBLNR_MKPF", "xblnr", t(16, False)),
            ("TCODE2_MKPF", "tcode2", t(20, False)),
            ("VBELN_IM", "vbeln_im", t(10, False)),
            ("VBELP_IM", "vbelp_im", integer(False)),
        ],
    },
    "ORDER_STATUS": {
        "table": "stg_order_status",
        "mode": "upsert",
        "key": "objnr",
        "columns": [
            ("OBJNR", "objnr", t(22)),
            ("DLV_STATUS_DATE", "dlv_status_date", date()),
        ],
    },
    "COST_REPORT": {
        "table": "ref_cost_report",
        "mode": "upsert",
        "key": "aufnr",
        "columns": [
            ("AUFNR", "aufnr", aufnr),
            ("MATNR", "matnr", t(18, False)),
            ("WERKS", "werks", t(4, False)),
            ("MAKTX", "maktx", t(40, False)),
            ("AUTYP", "autyp", integer(False)),
            ("OBJNR", "objnr", t(22, False)),
            ("DLV_STATUS_DATE", "dlv_status_date", date(False)),
            ("YEAR", "report_year", integer(False)),
            ("BATCH_SIZE_QUANTITY", "batch_size_quantity", num(3, False)),
            ("BASE_UNIT_OF_MEASURE", "base_unit_of_measure", t(3, False)),
            ("NUMBER_OF_REWORK_CYCLES", "number_of_rework_cycles", integer(False)),
            ("REWORK_DURATION_HOURS", "rework_duration_hours", integer(False)),
            ("STANDARD_COST($)", "standard_cost", num(6, False)),
            ("MATERIAL_COST($)", "material_cost", num(3, False)),
            ("LABOR_COST($)", "labor_cost", num(3, False)),
            ("TOTAL_STANDARD_COST($)", "total_standard_cost", num(3, False)),
            ("TOTAL_REWORK_COST($)", "total_rework_cost", num(2, False)),
            ("AI_PROJECTED_SAVINGS($)", "ai_projected_savings", num(2, False)),
        ],
    },
}


def detect_dataset(header: list[str]) -> Optional[str]:
    h = {str(c).strip().upper() for c in header if c is not None}
    if {"AI_PROJECTED_SAVINGS($)", "AUFNR"} <= h or {"TOTAL_STANDARD_COST($)", "AUFNR"} <= h:
        return "COST_REPORT"
    if {"MATNR", "AUFNR", "WERKS", "OBJNR", "GSTRP", "LABOR_COST"} <= h:
        return "ORDER_BASE"
    if {"AUFNR", "MBLNR", "DMBTR", "BWART"} <= h:
        return "ORDER_REWORK"
    if {"OBJNR", "DLV_STATUS_DATE"} <= h and len(h) <= 3:
        return "ORDER_STATUS"
    return None


# ───────────────────────── reading files ─────────────────────────

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def list_sheets(path: Path) -> list[tuple[str, list]]:
    """[(sheet name, header row)] — name is '' for CSV files."""
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        return [("", header)]
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        out = []
        for ws in wb.worksheets:
            first = next(ws.iter_rows(values_only=True), None)
            if first and any(c is not None for c in first):
                out.append((ws.title, list(first)))
        return out
    finally:
        wb.close()


def iter_data_rows(path: Path, sheet: str) -> Iterator[list]:
    """Yield data rows (header excluded)."""
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                yield row
        return
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows = ws.iter_rows(values_only=True)
        next(rows, None)
        for row in rows:
            yield list(row)
    finally:
        wb.close()


# ───────────────────────── loading ─────────────────────────

def load_sheet(path: Path, sheet: str, header: list, dataset: str, batch_id: int,
               progress: Callable[[int, int, int], None]) -> tuple[int, int, int]:
    """Load one sheet. Returns (rows_in_file, rows_loaded, rows_rejected).
    Runs in a single transaction; any unexpected error rolls everything back."""
    spec = DATASETS[dataset]
    idx = {str(c).strip().upper(): i for i, c in enumerate(header) if c is not None}
    missing = [src for src, _, _ in spec["columns"] if src.upper() not in idx
               and src not in ("MATERIAL_COST", "LABOR_COST", "MAKTX", "CPUDT_MKPF", "CPUTM_MKPF",
                               "XBLNR_MKPF", "TCODE2_MKPF", "VBELN_IM", "VBELP_IM", "YEAR")]
    if missing:
        raise RuntimeError(f"missing columns: {', '.join(missing)}")

    cols = [(idx.get(src.upper()), tgt, fn) for src, tgt, fn in spec["columns"]]
    target_cols = [tgt for _, tgt, _ in cols] + ["batch_id"]
    table = spec["table"]
    in_rows = loaded = rejected = 0
    stored_rejects: list[tuple] = []

    with connect() as conn:
        with conn.cursor() as cur:
            if spec["mode"] == "upsert":
                cur.execute(f"CREATE TEMP TABLE _load ON COMMIT DROP AS "
                            f"SELECT {', '.join(target_cols)} FROM {table} WITH NO DATA")
                copy_target = "_load"
            else:
                copy_target = table

            with cur.copy(f"COPY {copy_target} ({', '.join(target_cols)}) FROM STDIN") as cp:
                for n, raw in enumerate(iter_data_rows(path, sheet), start=2):
                    if not raw or all(_blank(v) for v in raw):
                        continue
                    in_rows += 1
                    try:
                        out = [fn(raw[i] if i is not None and i < len(raw) else None)
                               for i, _, fn in cols]
                    except RowError as e:
                        rejected += 1
                        if len(stored_rejects) < config.MAX_REJECTS_STORED:
                            stored_rejects.append((batch_id, n, str(e),
                                                   json.dumps([str(x) for x in raw])[:500]))
                        continue
                    out.append(batch_id)
                    cp.write_row(out)
                    loaded += 1
                    if in_rows % config.COPY_CHUNK_PROGRESS == 0:
                        progress(in_rows, loaded, rejected)

            if spec["mode"] == "upsert":
                key = spec["key"]
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in target_cols if c != key)
                if dataset == "ORDER_STATUS":  # keep the latest delivered date
                    sel = ("SELECT objnr, MAX(dlv_status_date), MAX(batch_id) FROM _load GROUP BY objnr")
                    updates = ("dlv_status_date = GREATEST(stg_order_status.dlv_status_date, EXCLUDED.dlv_status_date), "
                               "batch_id = EXCLUDED.batch_id")
                else:
                    sel = f"SELECT DISTINCT ON ({key}) {', '.join(target_cols)} FROM _load ORDER BY {key}"
                cur.execute(f"INSERT INTO {table} ({', '.join(target_cols)}) {sel} "
                            f"ON CONFLICT ({key}) DO UPDATE SET {updates}")

            if stored_rejects:
                cur.executemany("INSERT INTO etl_reject_row (batch_id,row_number,reason,raw_row) "
                                "VALUES (%s,%s,%s,%s)", stored_rejects)
        conn.commit()
    return in_rows, loaded, rejected


# ───────────────────────── consolidated summary ─────────────────────────

REBUILD_SQL = """
TRUNCATE consolidated_order_summary;
INSERT INTO consolidated_order_summary
  (process_order_number, plant_id, material_sku, material_description, base_unit_of_measure,
   batch_size_quantity, number_of_rework_cycles, rework_duration_hours, standard_cost,
   material_cost, labor_cost, total_standard_cost, total_rework_cost, ai_projected_savings,
   dlv_status_date, refreshed_at)
SELECT b.aufnr, b.werks, b.matnr, b.maktx, b.base_unit_of_measure, b.batch_size_quantity,
       COALESCE(r.cycles, 0), COALESCE(r.cycles, 0) * 48,
       b.material_cost / NULLIF(b.batch_size_quantity, 0),
       b.material_cost, b.labor_cost, b.material_cost + b.labor_cost,
       COALESCE(r.cost, 0), ROUND(COALESCE(r.cost, 0) * 0.30, 2),
       s.dlv_status_date, now()
FROM stg_order_base b
-- NOTE: the client's SQL doc says COUNT(DISTINCT MBLNR), but their delivered Cost Report equals the
-- number of movement LINES (COUNT(*)) on all 26,456 orders. COUNT(DISTINCT) differs on 2,396 orders.
-- We reproduce what the client actually delivered; confirm the intended definition with them.
LEFT JOIN (SELECT aufnr, COUNT(*) AS cycles, SUM(dmbtr) AS cost
           FROM stg_order_rework WHERE bwart = '261' GROUP BY aufnr) r ON r.aufnr = b.aufnr
LEFT JOIN stg_order_status s ON s.objnr = b.objnr;
"""


def rebuild_summary() -> int:
    with connect() as conn:
        conn.execute(REBUILD_SQL)
        n = conn.execute("SELECT COUNT(*) AS n FROM consolidated_order_summary").fetchone()["n"]
        conn.commit()
    return n


# ───────────────────────── validation against the client's report ─────────────────────────

def run_checks() -> dict:
    with connect(autocommit=True) as c:
        one = lambda q: c.execute(q).fetchone()["n"]  # noqa: E731
        counts = {
            "stg_order_base": one("SELECT COUNT(*) n FROM stg_order_base"),
            "stg_order_rework": one("SELECT COUNT(*) n FROM stg_order_rework"),
            "stg_order_status": one("SELECT COUNT(*) n FROM stg_order_status"),
            "consolidated_order_summary": one("SELECT COUNT(*) n FROM consolidated_order_summary"),
            "ref_cost_report": one("SELECT COUNT(*) n FROM ref_cost_report"),
        }
        checks = []

        def add(name, ok, detail, level=None):
            checks.append({"name": name, "ok": ok, "detail": detail,
                           "level": level or ("pass" if ok else "fail")})

        if counts["stg_order_base"] == 0:
            add("Orders loaded", False, "No orders loaded yet (PROCESS ORD BASE file).", "info")
            return {"counts": counts, "checks": checks}

        orph = c.execute("""SELECT COUNT(*) AS rows, COUNT(DISTINCT r.aufnr) AS orders
                            FROM stg_order_rework r
                            WHERE NOT EXISTS (SELECT 1 FROM stg_order_base b WHERE b.aufnr = r.aufnr)""").fetchone()
        add("Rework rows belong to a loaded order", orph["rows"] == 0,
            f"{orph['rows']:,} rework rows ({orph['orders']:,} orders) have no matching order in the base file "
            f"— expected if the rework extract covers a wider period than the order extract." if orph["rows"]
            else "All rework rows match an order.", "pass" if orph["rows"] == 0 else "warn")

        nostat = one("SELECT COUNT(*) n FROM stg_order_base b WHERE NOT EXISTS "
                     "(SELECT 1 FROM stg_order_status s WHERE s.objnr = b.objnr)")
        add("Orders with a delivered date", True,
            f"{counts['stg_order_base'] - nostat:,} of {counts['stg_order_base']:,} orders have a delivered (I0012) date; "
            f"{nostat:,} do not.", "info")

        mat = one("SELECT COUNT(*) n FROM stg_order_base WHERE material_cost > 0")
        add("Material cost present", mat > 0,
            "Material cost is 0 on every order — USER4 is empty in SAP. Real cost needs MBEW / BOM (Part B)."
            if mat == 0 else f"{mat:,} orders have material cost.", "warn" if mat == 0 else "pass")

        if counts["ref_cost_report"]:
            def cmp(label, cond, tol_msg):
                n = one(f"""SELECT COUNT(*) n FROM consolidated_order_summary s
                            JOIN ref_cost_report r ON r.aufnr = s.process_order_number WHERE {cond}""")
                add(label, n == 0, f"{n:,} orders differ ({tol_msg})." if n else "All orders match.")
            only_ref = one("""SELECT COUNT(*) n FROM ref_cost_report r WHERE NOT EXISTS
                              (SELECT 1 FROM consolidated_order_summary s WHERE s.process_order_number = r.aufnr)""")
            only_us = one("""SELECT COUNT(*) n FROM consolidated_order_summary s WHERE NOT EXISTS
                             (SELECT 1 FROM ref_cost_report r WHERE r.aufnr = s.process_order_number)""")
            add("Same set of orders as client Cost Report", only_ref == 0 and only_us == 0,
                f"in client report only: {only_ref:,}; in ours only: {only_us:,}.")
            cmp("Rework cycles match client report",
                "s.number_of_rework_cycles <> COALESCE(r.number_of_rework_cycles, 0)", "cycle counts")
            cmp("Rework cost matches client report (±0.05)",
                "ABS(s.total_rework_cost - COALESCE(r.total_rework_cost, 0)) > 0.05", "rework cost")
            cmp("Labor cost matches client report (±0.05)",
                "ABS(s.labor_cost - COALESCE(r.labor_cost, 0)) > 0.05", "labor cost")
            cmp("Delivered date matches client report",
                "s.dlv_status_date IS DISTINCT FROM r.dlv_status_date", "delivered dates")
            cmp("AI projected savings match (±0.05)",
                "ABS(s.ai_projected_savings - COALESCE(r.ai_projected_savings, 0)) > 0.05", "savings")
        else:
            add("Client Cost Report loaded", False,
                "Load 'Cost Report.xlsx' to cross-check our numbers against the client's.", "info")
    return {"counts": counts, "checks": checks}
