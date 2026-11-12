# BOM Data Loader (local)

Loads the client's paint-order files into a local PostgreSQL database.

## Run
Needs Docker Desktop and Python 3.12+. The first run creates `.venv` and installs `requirements.txt`.
```powershell
.\run.ps1          # starts PostgreSQL (Docker, port 5433) + the app on http://localhost:8010
```

## How it works
```
UI upload ─┐
           ├─> data/inbox ─> (background worker) ─> data/processing ─> data/processed
copy files ┘                       │                                  └> data/failed
                                   ├─ detect dataset from column headers
                                   ├─ clean rows, bulk COPY into stg_* tables (one transaction per sheet)
                                   └─ rebuild consolidated_order_summary when the inbox is empty
```
* Same file content is never loaded twice (SHA-256 per file + sheet).
* Bad rows are rejected and stored in `etl_reject_row`; the rest load.
* `GET /api/checks` compares our summary with the client's `Cost Report.xlsx`.

## Database
Connect with any SQL client: `localhost:5433`, db `bom_paint`, user `bom`, password `bom` (local dev only).

Part A tables (loaded): `etl_ingest_batch, etl_reject_row, stg_order_base, stg_order_rework, stg_order_status,
consolidated_order_summary, ref_cost_report`.
Part B1 tables (empty, built from FDS v1.5 sections 12.4 and 7 — exact field lists, in order):
`t_sel_variant, job_run, ds1_bom_components (23 fields), ds2_production_version (12), ds3_master_recipe (17)`.
Each column's OData / SAP field is stored as a column comment (hover a column in the UI preview).

Part B2 tables (empty DRAFT, from the SAP Data Reference workbook, not in the FDS; types assumed):
`sap_material, sap_material_plant, sap_material_valuation, sap_material_stock, sap_order_confirmation,
sap_order_component, sap_batch`.
Full DDL: `db/schema.sql`.

## Tests
Need the database running (`docker compose up -d`). They create namespaced test rows (plants TS01/TS02) and delete them afterwards.
```powershell
.\.venv\Scripts\python.exe tests\test_ranking.py
.\.venv\Scripts\python.exe tests\test_candidates.py
```

## Design documents
`docs\BOM_Data_Summary.html` (tables, sources, DDL) and `docs\BOM_Optimizer_HLD_LLD.html` (requirements, HLD, LLD).
Client data files are never committed: `data\` is gitignored.
