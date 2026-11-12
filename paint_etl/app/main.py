"""FastAPI app: upload endpoint, job status, table browser, checks."""
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from datetime import date

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import candidates, config, pipeline, ranking
from .db import apply_schema, connect, query, wait_for_db
from .worker import Worker, SUPPORTED

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PAINT_TABLES = ["etl_ingest_batch", "etl_reject_row", "stg_order_base", "stg_order_rework",
                "stg_order_status", "consolidated_order_summary", "ref_cost_report"]
# FDS-PP-BOM-001 v1.5 section 12.4 (BTP extraction output) - empty until extraction exists
FDS_TABLES = ["t_sel_variant", "job_run", "ds1_bom_components", "ds2_production_version",
              "ds3_master_recipe"]
# Draft model-feature tables from the SAP Data Reference workbook (not in the FDS)
DRAFT_TABLES = ["sap_material", "sap_material_plant", "sap_material_valuation", "sap_material_stock",
                "sap_order_confirmation", "sap_order_component", "sap_batch"]
# BOM Optimizer application model (docs/BOM_Optimizer_HLD_LLD.html section 5)
APP_TABLES = ["m_material", "t_process_order", "t_process_order_component", "t_process_order_operation",
              "t_goods_movement", "t_planned_order", "t_rework_link",
              "r_candidate", "r_candidate_item", "r_candidate_stats",
              "a_criterion", "a_rule", "a_rule_set", "a_season_map", "a_legal_status",
              "a_ranking_run", "a_ranking_run_criterion", "a_ranking_result",
              "a_adoption", "a_audit_log", "a_extraction_job"]
ALL_TABLES = PAINT_TABLES + FDS_TABLES + DRAFT_TABLES + APP_TABLES

worker = Worker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    wait_for_db()
    apply_schema()
    worker.start()
    yield
    worker.stop_event.set()


app = FastAPI(title="BOM Paint Data Loader", lifespan=lifespan)


def _safe_name(name: str) -> str:
    name = Path(name or "upload").name
    return re.sub(r"[^A-Za-z0-9._() \-]", "_", name) or "upload"


@app.get("/api/health")
def health():
    return {"status": "ok", "database": query("SELECT version() v")[0]["v"].split(",")[0]}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """Save each file into the inbox folder. The background worker picks it up."""
    saved = []
    for f in files:
        name = _safe_name(f.filename)
        if Path(name).suffix.lower() not in SUPPORTED:
            raise HTTPException(400, f"{name}: unsupported type. Use .xlsx or .csv")
        dest = config.INBOX / name
        n = 1
        while dest.exists():
            dest = config.INBOX / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
        tmp = dest.with_name(dest.name + ".part")   # worker ignores .part until the copy is done
        size = 0
        with open(tmp, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)
                size += len(chunk)
        os.replace(tmp, dest)
        saved.append({"file": dest.name, "bytes": size})
    return {"queued": saved}


@app.get("/api/jobs")
def jobs(limit: int = 60):
    rows = query("SELECT * FROM etl_ingest_batch ORDER BY batch_id DESC LIMIT %s", (min(limit, 500),))
    queued = [p.name for p in sorted(config.INBOX.iterdir()) if p.is_file() and not p.name.startswith(".")]
    return {"current_file": worker.current, "queued_files": queued, "summary_dirty": worker.dirty,
            "last_rebuild": worker.last_rebuild, "jobs": rows}


@app.get("/api/overview")
def overview():
    def count(t):
        return query(f"SELECT COUNT(*) n FROM {t}")[0]["n"]
    def files(d: Path):
        return len([p for p in d.iterdir() if p.is_file()])
    return {
        "paint_tables": [{"name": t, "rows": count(t)} for t in PAINT_TABLES],
        "fds_tables": [{"name": t, "rows": count(t)} for t in FDS_TABLES],
        "draft_tables": [{"name": t, "rows": count(t)} for t in DRAFT_TABLES],
        "app_tables": [{"name": t, "rows": count(t)} for t in APP_TABLES],
        "folders": {k: {"path": str(d), "files": files(d)} for k, d in
                    {"inbox": config.INBOX, "processing": config.PROCESSING,
                     "processed": config.PROCESSED, "failed": config.FAILED}.items()},
    }


@app.get("/api/table/{name}")
def table_preview(name: str, limit: int = 25, offset: int = 0):
    if name not in ALL_TABLES:
        raise HTTPException(404, "unknown table")
    cols = query("SELECT column_name, data_type, "
                 "col_description(format('public.%%I', table_name)::regclass, ordinal_position) AS comment "
                 "FROM information_schema.columns "
                 "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (name,))
    rows = query(f"SELECT * FROM {name} ORDER BY 1 LIMIT %s OFFSET %s", (min(limit, 200), offset))
    return {"name": name, "columns": cols, "rows": rows}


@app.get("/api/rejects/{batch_id}")
def rejects(batch_id: int):
    return query("SELECT row_number, reason, raw_row FROM etl_reject_row WHERE batch_id=%s "
                 "ORDER BY reject_id LIMIT 200", (batch_id,))


@app.post("/api/rebuild")
def rebuild():
    n = pipeline.rebuild_summary()
    worker.dirty = False
    return {"rows": n}


@app.get("/api/checks")
def checks():
    return pipeline.run_checks()


# ───────────── BOM Optimizer: production persona ranking (design sections 7-8) ─────────────
class RankChoice(BaseModel):
    rank: int = Field(ge=1, le=4)
    criterion: str


class AnalyzeRequest(BaseModel):
    plant: str = Field(min_length=1)
    material: str | None = None
    planned_order: str | None = None
    process_order: str | None = None
    production_line: str | None = None
    quantity: float | None = Field(default=None, gt=0)
    target_date: date | None = None
    criteria: list[RankChoice]
    constraints: dict = {}


@app.get("/api/criteria")
def criteria():
    return query("SELECT criterion_code, label, direction, tie_band_pct FROM a_criterion WHERE active ORDER BY sort_order")


@app.post("/api/production/analyze")
def analyze(req: AnalyzeRequest):
    with connect() as conn:
        try:
            return ranking.analyze(conn, req.model_dump(mode="json") | {"target_date": req.target_date})
        except ranking.RankingError as e:
            conn.rollback()
            raise HTTPException(e.status, e.message)


@app.post("/api/candidates/rebuild")
def candidates_rebuild(material: str | None = None, plant: str | None = None):
    """Rebuild Layer R (candidates + statistics) from the master and transactional tables."""
    with connect() as conn:
        return candidates.build(conn, material=material, plant=plant)


@app.get("/api/candidates/jobs")
def candidate_jobs(limit: int = 20):
    return query("SELECT job_id, status, scope, started_at, finished_at, rows_loaded, message FROM a_extraction_job "
                 "WHERE job_type='CANDIDATE_BUILD' ORDER BY job_id DESC LIMIT %s", (min(limit, 200),))


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    with connect() as conn:
        try:
            return ranking.get_run(conn, run_id)
        except ranking.RankingError as e:
            raise HTTPException(e.status, e.message)


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
