"""Builds docs/BOM_Data_Summary.html from the LIVE database + db/schema.sql.
Run:  <python> docs\\build_summary.py      (database container must be running)
Column lists come from PostgreSQL itself and the DDL is copied verbatim from schema.sql,
so the page cannot drift from what actually exists."""
import datetime as dt
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import psycopg  # noqa: E402

DSN = "postgresql://bom:bom@127.0.0.1:5433/bom_paint"
esc = lambda s: html.escape(str(s if s is not None else ""))  # noqa: E731

# ───────────────────────── table metadata (plain-English) ─────────────────────────
GROUPS = {
    "A": ("Part A — Paint order data", "We HAVE the files today. Loaded by the upload page / background worker."),
    "B1": ("Part B1 — FDS extraction tables", "Layout is exactly FDS v1.5. Empty until the SAP BTP extraction exists."),
    "B2": ("Part B2 — Draft model-feature tables", "From the SAP Data Reference workbook, NOT in the FDS. Subset of fields, types assumed."),
}

T = [
 # ---- Part A
 dict(n="etl_ingest_batch", g="A", title="Load log (one row per file + sheet)",
      what="A log book of every file we load: what it was detected as, rows read / loaded / rejected, success or failure. It also stops the same file content being loaded twice (SHA-256).",
      src="Our own design. Not in any client document.",
      write="Background worker, automatically, for every file that arrives in the inbox.",
      read="UI section 2 'Processing' (GET /api/jobs). Duplicate-file check inside the worker.",
      note=""),
 dict(n="etl_reject_row", g="A", title="Rejected rows",
      what="Rows that could not be loaded (bad date, empty order number, text too long ...) with the reason and the raw values. First 1,000 per sheet are kept.",
      src="Our own design.",
      write="Background worker, while loading.",
      read="UI: click the red 'Rejected' number in the Processing table (GET /api/rejects/{id}).",
      note="Right now 0 rows were rejected in all four real files."),
 dict(n="stg_order_base", g="A", title="STG_ORDER_BASE — one row per process order",
      what="Plant, material, description, batch size, unit, start date, material cost and labor cost for each process order (26,456 orders, plants US01 and US18).",
      src="SQL Queries.docx (root copy) → 'Step 1' and its Access table spec (AUFK + AFKO + AFPO + MAKT). Column names from the header row of PROCESS ORD BASE.xlsx.",
      write="Upload PROCESS ORD BASE.xlsx → worker (insert or update on order number).",
      read="Summary rebuild; UI table browser; data checks.",
      note="The file also has PSMNG, identical to BATCH_SIZE_QUANTITY, so it is stored once. MATERIAL_COST is 0 on every order (AUFK-USER4 is empty in SAP). LABOR_COST is just batch size × 12.50 (a rule in the client's SQL)."),
 dict(n="stg_order_rework", g="A", title="STG_ORDER_REWORK — raw movement-261 lines",
      what="Every goods-movement line with movement type 261 against a process order: document number, amount, posting date. About 1.29 million rows.",
      src="SQL Queries.docx → 'Step 2' (MSEG). Column names from REWORK MOVEMENTS.xlsx (4 sheets).",
      write="Upload REWORK MOVEMENTS.xlsx → worker (append, one transaction per sheet).",
      read="Summary rebuild (counts lines and sums amounts per order).",
      note="DIFFERENT from the client's doc: their STG_ORDER_REWORK table is already totalled per order, but the file we received is the raw lines before totalling. So we keep the raw lines and do the totals ourselves. Movement 261 is the normal goods issue to an order — the client's own note warns it may not mean true rework. The extract has no line number (ZEILE), so we use a surrogate key."),
 dict(n="stg_order_status", g="A", title="STG_ORDER_STATUS — delivered date",
      what="The latest date each order reached status I0012 'Delivered' (DLV).",
      src="SQL Queries.docx → 'Step 3' (JEST + JCDS). File: PROCESS ORDER STATUS.xlsx.",
      write="Upload PROCESS ORDER STATUS.xlsx → worker (insert or update, keeps the latest date).",
      read="Summary rebuild.",
      note="The file has 327,980 orders going back to 2021, but only 26,456 match the orders we hold. 4,780 of those orders have no delivered date."),
 dict(n="consolidated_order_summary", g="A", title="CONSOLIDATED_ORDER_SUMMARY — the final per-order report",
      what="One row per order combining base + rework + status: rework cycles and hours, unit cost, material and labor cost, total cost, rework cost, projected savings (30% of rework cost), delivered date.",
      src="SQL Queries.docx → 'Step 4' and the CONSOLIDATED_ORDER_SUMMARY Access spec.",
      write="Rebuilt automatically by the worker when the inbox is empty, or by the 'Rebuild summary now' button (POST /api/rebuild).",
      read="UI table browser; data checks (GET /api/checks); the future BOM-ranking model.",
      note="Rework cycles = number of movement lines. That equals the client's delivered Cost Report on all 26,456 orders, but their SQL text says COUNT(DISTINCT MBLNR), which differs on 2,396 orders. Hours = cycles × 48 is a fixed client assumption."),
 dict(n="ref_cost_report", g="A", title="REF_COST_REPORT — the client's own answer",
      what="A copy of the client's finished Cost Report. Used only to prove that our calculated summary is correct.",
      src="Cost Report.xlsx (18 columns).",
      write="Upload Cost Report.xlsx → worker.",
      read="UI section 4 'Data checks' (GET /api/checks).",
      note="Our summary matches it exactly on order set, rework cycles, rework cost, labor cost, delivered date and projected savings."),
 # ---- Part B1
 dict(n="t_sel_variant", g="B1", title="Saved selections (FDS: T_SEL_VARIANT)",
      what="Saved choices from the BTP app's selection screen (which plants, material types, MRP types ...). A scheduled job points at one variant.",
      src="FDS v1.5 section 6.3 and section 12.4.",
      write="Future: the SAP BTP app's UI. Nothing in our app writes it yet.",
      read="Future: the BTP extraction job.",
      note=""),
 dict(n="job_run", g="B1", title="Extraction runs (FDS: job_run)",
      what="One row per extraction run: the filters used (snapshot), start/end time, status RUNNING / COMPLETE / FAILED, materials total and processed.",
      src="FDS v1.5 section 12.4 (and 6.3).",
      write="Future: the BTP extraction job.",
      read="Future: consumers filter ds1/ds2/ds3 by run_id.",
      note="Column 'message' is our addition (the FDS says failures are logged but gives no column for the reason)."),
 dict(n="ds1_bom_components", g="B1", title="Dataset 1 — Production BOM (23 fields)",
      what="Every ingredient of every production BOM alternative. Each row carries 11 header fields (material, plant, BOM number, alternative, status, validity, base quantity ...) and 12 item fields (component, quantity, unit, category, validity, phantom / costing / bulk / backflush flags).",
      src="FDS v1.5 section 7.2 (fields), 13.1–13.2 (samples), 12.4 (table name). API_BILL_OF_MATERIAL_SRV → A_BillOfMaterial with $expand to_BillOfMaterialItem.",
      write="Future: BTP app, Call 3.",
      read="Future: ML model (finds the best BOM alternative at a given date).",
      note="FDS lists the header deletion flag as SAP field LKENZ; real SAP name is probably LOEKZ. Copied as written. Types are our choice (FDS gives none)."),
 dict(n="ds2_production_version", g="B1", title="Dataset 2 — Production Version (12 fields)",
      what="The link table: which BOM alternative and which recipe each production version uses, for which lot-size range and validity dates.",
      src="FDS v1.5 section 7.3, 13.3. API_PRODUCT_SRV → A_ProductionVersion.",
      write="Future: BTP app, Call 2.",
      read="Future: joins ds1 (via BOM variant) and ds3 (via recipe group + counter).",
      note=""),
 dict(n="ds3_master_recipe", g="B1", title="Dataset 3 — Master Recipe (17 fields)",
      what="Recipe steps (operations) with work centre and standard duration, plus the component consumed at each step. Steps that consume nothing (e.g. QA testing) leave the component columns empty.",
      src="FDS v1.5 section 7.4, 13.4. API_MASTERRECIPE_0001 → A_MasterRecipeHeader (+ operations + components).",
      write="Future: BTP app, Call 4.",
      read="Future: ML model; production-efficiency figures (hours, work centre).",
      note="Only valid for process-industry (PP-PI) plants. FDS open item OI-007: confirm each plant uses recipes, not routings."),
 # ---- Part B2
 dict(n="sap_material", g="B2", title="DRAFT — Material master (MARA + MAKT)",
      what="One row per material: description, type (FERT / HALB / ROH / VERP), group, units, weights, batch-managed flag, deletion flag.",
      src="SAP_Data_Reference… workbook → sheet 'Field Master List' (tables MARA, MAKT).",
      write="Future: extraction method not decided (FDS open item OI-002).",
      read="Future: join key MATNR for every other dataset.",
      note="Subset of the workbook's fields. Types assumed."),
 dict(n="sap_material_plant", g="B2", title="DRAFT — Material by plant (MARC)",
      what="Per material and plant: MRP type and controller, lot sizes, safety stock, lead times, procurement type (in-house / external), backflush.",
      src="Workbook → 'Field Master List' (MARC).",
      write="Future.", read="Future.",
      note="The FDS only uses MARC fields as filters (procurement type, plant, MRP type) and does not store them — FDS open item OI-009."),
 dict(n="sap_material_valuation", g="B2", title="DRAFT — Valuation / cost (MBEW)",
      what="Standard price, moving-average price, price control, price unit, total stock value per material and valuation area.",
      src="Workbook → 'Field Master List' (MBEW) and 'Extraction Notes' (which cost field is meaningful).",
      write="Future.", read="Future: real material cost.",
      note="Important: material cost in the paint order data is 0 on every order, so the model will need this table (or the BOM prices) for real cost."),
 dict(n="sap_material_stock", g="B2", title="DRAFT — Stock by storage location (MARD)",
      what="Unrestricted, quality-inspection and blocked stock per material, plant and storage location.",
      src="Workbook → 'Field Master List' (MARD).",
      write="Future.", read="Future: availability of raw materials.", note=""),
 dict(n="sap_order_confirmation", g="B2", title="DRAFT — Order confirmations (AFRU)",
      what="Actual start / end, yield, scrap and work centre of each order confirmation.",
      src="Workbook → 'Transactional Data' / 'Transactional Tables Explained' (AFRU).",
      write="Future.", read="Future: planned-vs-actual timing and real rework / delay signals.", note=""),
 dict(n="sap_order_component", g="B2", title="DRAFT — Order component reservations (RESB)",
      what="Required versus withdrawn quantity of each component on an order.",
      src="Workbook → 'Transactional Data' (RESB).",
      write="Future.", read="Future: what was really consumed vs the BOM.", note=""),
 dict(n="sap_batch", g="B2", title="DRAFT — Batch master (MCHA)",
      what="One row per physical batch: production date, expiry and last goods-receipt date.",
      src="Workbook → 'Companion Data (Required)' (MCHA).",
      write="Future.", read="Future: lot-level model features.", note=""),
]

# ───────────────────────── read live database + schema.sql ─────────────────────────
schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf8")
create_re = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", re.S)
creates = {m.group(1): m.group(0) for m in create_re.finditer(schema)}
index_re = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS \w+\s+ON (\w+)[^;]*;", re.S)
indexes: dict[str, list[str]] = {}
for m in index_re.finditer(schema):
    indexes.setdefault(m.group(1), []).append(m.group(0))
tcomment_re = re.compile(r"COMMENT ON TABLE (\w+) IS '(?:[^']|'')*';")
tcomments = {m.group(1): m.group(0) for m in tcomment_re.finditer(schema)}


def inline_comments(ddl: str) -> dict:
    out = {}
    for line in ddl.split("\n")[1:]:
        m = re.match(r"\s+(\w+)\s+[A-Za-z].*?--\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def ddl_for(name: str) -> str:
    parts = [creates[name]] + indexes.get(name, []) + ([tcomments[name]] if name in tcomments else [])
    return "\n".join(parts)


conn = psycopg.connect(DSN, autocommit=True)
info: dict[str, dict] = {}
for t in T:
    n = t["n"]
    cols = conn.execute("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, col_description(a.attrelid, a.attnum)
        FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum""", (f"public.{n}",)).fetchall()
    pk = {r[0] for r in conn.execute("""
        SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary""", (f"public.{n}",)).fetchall()}
    fk = {r[0]: r[1] for r in conn.execute("""
        SELECT a.attname, c.confrelid::regclass::text FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.contype = 'f' AND c.conrelid = %s::regclass""", (f"public.{n}",)).fetchall()}
    rows = conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
    info[n] = dict(cols=cols, pk=pk, fk=fk, rows=rows, inline=inline_comments(creates[n]))
db_version = conn.execute("SELECT version()").fetchone()[0].split(",")[0]
conn.close()
missing = [t["n"] for t in T if t["n"] not in creates]
assert not missing, f"not found in schema.sql: {missing}"

# ───────────────────────── html pieces ─────────────────────────
CSS = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#1b2430;--muted:#5d6878;--line:#dfe3ea;--accent:#2457d6;--accent2:#e8eefc;
--ok:#12805c;--okbg:#e3f5ec;--warn:#9a5b06;--warnbg:#fdf1dc;--bad:#b42318;--badbg:#fde8e6;--code:#f0f2f6;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#12161c;--card:#1a2028;--ink:#e8ecf2;--muted:#a3adbd;
--line:#2b3340;--accent:#7ea5ff;--accent2:#1c2a4a;--ok:#4cc79a;--okbg:#143428;--warn:#f0b35a;--warnbg:#3a2a10;--bad:#ff8a80;--badbg:#3c1816;--code:#10141a;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:18px 16px 70px}
header.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px}
header.top .in{max-width:1040px;margin:0 auto;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header.top b{font-size:15px}
nav a{color:var(--muted);text-decoration:none;font-size:13px;margin-right:12px;white-space:nowrap}nav a:hover{color:var(--accent)}
section.page{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin:18px 0}
h1{font-size:26px;margin:4px 0 6px}h2{font-size:21px;margin:0 0 4px}h3{font-size:16px;margin:20px 0 6px}
.lead{color:var(--muted);margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:8px 0}
th{text-align:left;color:var(--muted);font-weight:600;padding:7px 9px;border-bottom:2px solid var(--line);white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.tw{overflow-x:auto}
code{background:var(--code);padding:1px 6px;border-radius:5px;font-size:12.5px}
pre{background:var(--code);border:1px solid var(--line);border-radius:9px;padding:12px 14px;overflow:auto;font:12.5px/1.5 ui-monospace,Consolas,monospace;margin:8px 0}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:600;margin-right:4px}
.pill.ok{background:var(--okbg);color:var(--ok)}.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.bad{background:var(--badbg);color:var(--bad)}.pill.info{background:var(--accent2);color:var(--accent)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:760px){.grid2,.grid3{grid-template-columns:1fr}}
.box{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--bg)}
.box h4{margin:0 0 4px;font-size:14px}.box p{margin:0;font-size:13.5px;color:var(--muted)}
dl.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;margin:10px 0}dl.kv dt{color:var(--muted);font-size:13px}dl.kv dd{margin:0}
.callout{border-left:4px solid var(--warn);background:var(--warnbg);border-radius:6px;padding:9px 13px;margin:12px 0;font-size:14px}
.callout.ok{border-color:var(--ok);background:var(--okbg)}
button.copy{float:right;font:12px system-ui;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:2px 9px;cursor:pointer}
ul.tight{margin:6px 0 6px 18px;padding:0}ul.tight li{margin:3px 0}
.k{color:var(--muted);font-size:12.5px}
@media print{header.top{display:none}body{background:#fff;color:#000;font-size:11pt}section.page{border:0;padding:0;margin:0;page-break-before:always}
section.page:first-of-type{page-break-before:auto}pre{white-space:pre-wrap;border:1px solid #bbb}button.copy{display:none}:root{--card:#fff;--code:#f4f4f4;--line:#ccc;--ink:#000;--muted:#444}}
"""

JS = """
document.querySelectorAll('button.copy').forEach(b=>b.onclick=()=>{
  const t=b.parentElement.querySelector('pre').innerText; navigator.clipboard.writeText(t).then(()=>{b.textContent='Copied';setTimeout(()=>b.textContent='Copy DDL',1500)});});
"""


def pill(text, kind="info"):
    return f'<span class="pill {kind}">{esc(text)}</span>'


def table_page(t: dict) -> str:
    n, d = t["n"], info[t["n"]]
    status = {"A": pill("Built · loaded" if d["rows"] else "Built", "ok"),
              "B1": pill("Built · empty (waiting for BTP)", "warn"),
              "B2": pill("Draft · empty", "bad")}[t["g"]]
    rows = []
    for name, typ, notnull, dbcomment in d["cols"]:
        key = []
        if name in d["pk"]:
            key.append("PK")
        if name in d["fk"]:
            key.append(f"FK → {d['fk'][name]}")
        meaning = dbcomment or d["inline"].get(name, "")
        rows.append(f"<tr><td><code>{esc(name)}</code></td><td>{esc(typ)}</td><td>{'NOT NULL' if notnull else 'null ok'}</td>"
                    f"<td>{esc(', '.join(key))}</td><td>{esc(meaning)}</td></tr>")
    note = f'<div class="callout">{esc(t["note"])}</div>' if t["note"] else ""
    return f"""
<section class="page" id="t-{n}">
  <div class="k">{esc(GROUPS[t['g']][0])}</div>
  <h2><code style="font-size:19px">{n}</code></h2>
  <p class="lead">{esc(t['title'])} &nbsp; {status} {pill(f"{d['rows']:,} rows now")}</p>
  <dl class="kv">
    <dt>What it is for</dt><dd>{esc(t['what'])}</dd>
    <dt>Where it is written</dt><dd>{esc(t['src'])}</dd>
    <dt>Filled by</dt><dd>{esc(t['write'])}</dd>
    <dt>Read by</dt><dd>{esc(t['read'])}</dd>
  </dl>
  {note}
  <h3>Columns ({len(d['cols'])})</h3>
  <div class="tw"><table><thead><tr><th>Column</th><th>Type</th><th>Null</th><th>Key</th><th>Meaning / source field</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table></div>
  <h3>DDL (exact, from db/schema.sql)</h3>
  <div><button class="copy">Copy DDL</button><pre>{esc(ddl_for(n))}</pre></div>
</section>"""


def mini_row(t):
    d = info[t["n"]]
    return (f'<tr><td><a href="#t-{t["n"]}"><code>{t["n"]}</code></a></td><td>{esc(t["title"])}</td>'
            f'<td class="num">{len(d["cols"])}</td><td class="num">{d["rows"]:,}</td></tr>')


def group_table(g):
    rows = "".join(mini_row(t) for t in T if t["g"] == g)
    return (f'<h3>{esc(GROUPS[g][0])}</h3><p class="lead" style="margin:0">{esc(GROUPS[g][1])}</p>'
            f'<div class="tw"><table><thead><tr><th>Table</th><th>What it holds</th><th class="num">Columns</th><th class="num">Rows now</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def matrix_rows():
    out = []
    for t in T:
        out.append(f'<tr><td><a href="#t-{t["n"]}"><code>{t["n"]}</code></a></td><td>{esc(t["write"])}</td><td>{esc(t["read"])}</td></tr>')
    return "".join(out)


today = dt.date.today().strftime("%d %b %Y")
n_tables = len(T)
n_cols = sum(len(info[t["n"]]["cols"]) for t in T)
rows_total = {t["n"]: info[t["n"]]["rows"] for t in T}

# ───────────────────────── static pages ─────────────────────────
P_OVERVIEW = f"""
<section class="page" id="overview">
  <h1>BOM Paint Data — Summary</h1>
  <p class="lead">What we have, what each document says, where the database lives, and every table with its exact DDL. Generated {today} from the live database.</p>
  <div class="grid3">
    <div class="box"><h4>{n_tables} tables · {n_cols} columns</h4><p>All created on the local PostgreSQL. Seven carry real data.</p></div>
    <div class="box"><h4>1,675,… rows of real data</h4><p>{rows_total['stg_order_rework']:,} rework lines, {rows_total['stg_order_status']:,} statuses, {rows_total['stg_order_base']:,} orders.</p></div>
    <div class="box"><h4>Totals match the client</h4><p>Our summary equals the client's Cost Report on all {rows_total['consolidated_order_summary']:,} orders.</p></div>
  </div>
  <h3>The whole thing in one minute</h3>
  <ul class="tight">
    <li><b>The goal:</b> load SAP data into a database so a model can later pick the best BOM / recipe per product, looking at cost and rework.</li>
    <li><b>Two kinds of data exist.</b>
      <ul class="tight"><li><b>Stream A — paint order cost &amp; rework:</b> the client already ran four SQL queries on SAP and gave us the results as Excel files. We have this now and it is loaded.</li>
      <li><b>Stream B — BOM, recipe, production version, master data:</b> only a <i>design</i> exists (the FDS, plus a field list). A SAP BTP app is supposed to pull it through standard SAP OData APIs. No data has arrived yet, so those tables are created but empty.</li></ul></li>
    <li><b>How data gets in today:</b> UI upload (or copy a file into a folder) → background worker cleans it → bulk-loads into PostgreSQL → rebuilds the summary → moves the file to <code>processed</code>.</li>
  </ul>
  <div class="callout ok"><b>Confirmed from the documents:</b> all column names in Part A (client's files + SQL doc) and all 52 field names in Part B1 (FDS, compared automatically).
  <b>Not confirmed:</b> column <i>types and lengths</i> in Part B (no document gives them) and the whole of Part B2 (draft).</div>
  <h3>Contents</h3>
  <ul class="tight"><li><a href="#docs">1. The documents, explained simply</a></li><li><a href="#sources">2. "Where is it written?" — topic → document map</a></li>
  <li><a href="#database">3. Where the database is on your machine</a></li><li><a href="#perspective">4. Tables from the API and UI point of view</a></li>
  <li><a href="#findings">5. Findings and open questions</a></li><li><a href="#tables">6. One page per table (with DDL)</a></li><li><a href="#alldll">7. Complete DDL</a></li></ul>
</section>"""
P_OVERVIEW = P_OVERVIEW.replace("1,675,… rows of real data", f"{rows_total['stg_order_rework'] + rows_total['stg_order_status'] + rows_total['stg_order_base'] + rows_total['ref_cost_report']:,} rows of real data")

DOCS = [
 ("Chat thread you pasted", "Messages between the team", "Piyush asks for: login/server details for BTP, a list of tables and columns, expected data size (GB), and the source of each table (BTP database or REST endpoint). Prathap replies with the SQL document, a size estimate of 10 GB (3 years is under 200 MB, 10 years under 1 GB), and the data files. Pushkar is getting BTP instructions from the vendor. Someone also shared the SAP Data Reference workbook and the FDS.",
  "Tells you what the team is waiting for and what was delivered.", "Read in full"),
 ("SQL Queries.docx — the ROOT copy (8,027 characters)", "Client SQL + table specs", "Four SQL steps (base orders, rework movements, delivered status, final join) <b>and</b> an 'SAP to MS Access' specification listing four tables with column names, types and sizes: STG_ORDER_BASE, STG_ORDER_REWORK, STG_ORDER_STATUS, CONSOLIDATED_ORDER_SUMMARY.",
  "This is THE document for Part A table and column names.", "Read in full"),
 ("SQL Queries.docx — copy inside the Paint data folder (2,708 characters)", "Client SQL only", "The same four SQL steps but <b>without</b> the table specs. Different file from the root copy.", "Don't use it for table specs.", "Read in full"),
 ("Paint data folder — 4 Excel files", "Real data from SAP", "PROCESS ORD BASE.xlsx (26,456 orders) · REWORK MOVEMENTS.xlsx (about 1.29 million raw movement lines, split over 4 sheets because Excel allows ~1M rows per sheet) · PROCESS ORDER STATUS.xlsx (327,980 rows) · Cost Report.xlsx (26,456 rows, 18 columns — the client's finished result).",
  "The only real data we have. Every column was profiled (types, lengths, empties).", "Profiled fully and loaded"),
 ("SAP_Data_Reference_Master_Transactional_BOM_v5 (two copies)", "Catalog of SAP fields", "10 sheets: which SAP tables and fields (MARA, MARC, MBEW, MARD, AFKO, AFPO, AFRU, RESB, MSEG, QALS, MAST, STKO, STPO, MKAL, PLKO, PLPO, MCHA ...) could feed the model, each with a plain-English 'why useful', plus a list of questions to confirm with the SAP team ('Extraction Notes').",
  "A shopping list of fields, not a table design: it gives NO data types. The two copies are identical in content; only the file size differs.", "Read; both copies compared cell by cell"),
 ("FDS_Master_Data_Extraction_v1.5.docx", "Functional design for the SAP BTP app", "How a SAP BTP app will extract production BOMs, production versions and master recipes using three standard OData services (API_PRODUCT_SRV, API_BILL_OF_MATERIAL_SRV, API_MASTERRECIPE_0001). Defines three output datasets of 23, 12 and 17 fields, the storage tables, selection screen, business rules and open items.",
  "THE document for Part B1 table and column names. It does NOT cover the paint orders / rework / status data.", "Read (sections 4, 6, 7, 10, 12, 13)"),
 ("Master_Data_Extraction_flow_v1.5.pdf", "Flow diagram for the FDS", "8 pages, probably the extraction flow chart.", "Could not extract text here.", "NOT read — open it yourself"),
 ("BOM Optimizer RTM - V1.xlsx", "Requirements for the optimizer", "What the optimizer must do: compare planned/static BOMs with proposed ones, rank by history, cost and number of rework cycles, show raw material cost, legal restrictions, season. Lists the data objects needed, a screen sketch ('Prod Output Persona' with rank, cost, average cycles, Adopt button) and open questions about how rework orders link to the original order.",
  "Explains <i>why</i> the data is needed. It does not define tables.", "Read"),
 ("bom details 1.md", "Your own notes (ChatGPT style)", "Make-to-order vs make-to-stock ideas, an Azure architecture with React UI and microservices, and a list of agents and modules.", "Vision/architecture only. Not a client specification, no tables.", "Read"),
 ("fortun8tech Cloud Connector link", "Install guide", "Said to explain how to install SAP Cloud Connector so BTP can reach an on-premise SAP. A secure tunnel only; something must still call SAP through it.", "Needed later for live extraction.", "NOT opened"),
]

doc_cards = "".join(
    f'<tr><td><b>{n}</b><div class="k">{k}</div></td><td>{c}</td><td>{w}</td><td>{pill(r, "ok" if r.startswith(("Read", "Profiled")) else "bad")}</td></tr>'
    for n, k, c, w, r in DOCS)
P_DOCS = f"""
<section class="page" id="docs">
  <h2>1. The documents, explained simply</h2><p class="lead">Every file you shared, what is inside, and whether I actually read it.</p>
  <div class="tw"><table><thead><tr><th>File</th><th>What it contains</th><th>What it is good for</th><th>Read?</th></tr></thead><tbody>{doc_cards}</tbody></table></div>
</section>"""

SOURCES = [
 ("Paint table names, column names, sizes", "SQL Queries.docx (ROOT copy)", "'SAP to MS Access Database Structure Specification', Steps 1–4"),
 ("The four SQL queries", "SQL Queries.docx (either copy)", "Steps 1–4"),
 ("Real column headers of the data", "The 4 Paint data Excel files", "Row 1 of each sheet"),
 ("SAP APIs to call (and for which data)", "FDS v1.5", "Section 4.1 / 4.2"),
 ("BOM output fields (23), production version (12), recipe (17)", "FDS v1.5", "Sections 7.2 / 7.3 / 7.4, examples in 13"),
 ("Storage tables ds1 / ds2 / ds3, job_run, t_sel_variant", "FDS v1.5", "Section 12.4 'Storage Design'"),
 ("Selection screen / saved variants", "FDS v1.5", "Sections 6.1–6.3"),
 ("Open decisions (where to store data, PP vs PP-PI ...)", "FDS v1.5", "Section 10 (OI-001 … OI-012)"),
 ("Material / transaction / BOM / batch field catalog", "SAP Data Reference workbook", "Sheets: Field Master List, Transactional Data, BOM Data, Companion Data"),
 ("Questions for the SAP team (ECC vs S/4, cost field ...)", "SAP Data Reference workbook · RTM", "'Extraction Notes' · 'Questions'"),
 ("What the model must rank on (history, cost, cycles)", "BOM Optimizer RTM", "Sheet 'Production'"),
 ("Screen sketch of the output", "BOM Optimizer RTM", "Sheet 'Prod Output Persona'"),
 ("Data size: 10 GB planned", "Chat message from Prathap", "'Size - For now, lets go with 10 GB'"),
 ("Tables etl_*, ref_*, stg_* naming, load log, rejects", "Our own design", "db/schema.sql — not in any client document"),
]
P_SOURCES = f"""
<section class="page" id="sources">
  <h2>2. "Where is it written?" — topic → document</h2><p class="lead">Use this when someone asks "who said that?".</p>
  <div class="tw"><table><thead><tr><th>Topic</th><th>Document</th><th>Where inside</th></tr></thead><tbody>
  {''.join(f'<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>' for a, b, c in SOURCES)}</tbody></table></div>
</section>"""

P_DB = f"""
<section class="page" id="database">
  <h2>3. Where the database is on your machine</h2>
  <p class="lead">All tables were created on a local PostgreSQL running in Docker. Nothing is in the cloud.</p>
  <dl class="kv">
    <dt>What</dt><dd>Docker container <code>bom-paint-postgres</code> (image <code>postgres:15-alpine</code>) — {esc(db_version)}</dd>
    <dt>Address</dt><dd><code>localhost:5433</code> (inside the container it is 5432)</dd>
    <dt>Database / user</dt><dd>database <code>bom_paint</code> · user <code>bom</code> · password is in <code>docker-compose.yml</code> (local development only)</dd>
    <dt>Where the data sits</dt><dd>Docker volume <code>paint_etl_bom_paint_pgdata</code> — survives restarts and <code>docker compose down</code></dd>
    <dt>Project folder</dt><dd><code>C:\\Users\\dell\\Documents\\src\\bom\\paint_etl</code></dd>
    <dt>Where the DDL is</dt><dd><code>paint_etl\\db\\schema.sql</code> — the app runs it at every start (safe to repeat), which is how the tables were created</dd>
    <dt>The app</dt><dd><code>http://localhost:8010</code> — upload page. Start with <code>.\\run.ps1</code></dd>
    <dt>File folders</dt><dd><code>paint_etl\\data\\inbox</code> → <code>processing</code> → <code>processed</code> (or <code>failed</code>)</dd>
  </dl>
  <h3>How to look at the tables</h3>
  <ul class="tight">
    <li><b>In the UI:</b> http://localhost:8010 → section "3. Database tables" → click a table.</li>
    <li><b>In a terminal:</b> <code>docker exec -it bom-paint-postgres psql -U bom -d bom_paint</code>, then <code>\\dt</code> (list tables) or <code>\\d stg_order_base</code> (one table).</li>
    <li><b>With a SQL tool</b> (DBeaver, pgAdmin ...): connect to localhost, port 5433, database bom_paint.</li>
  </ul>
  <div class="callout">Two other databases exist on this computer and are <b>not</b> used here: the older BOM app's SQLite file (<code>src\\bom\\api\\bom_platform.db</code>) and <code>blueplus-postgres</code> on port 5432.</div>
</section>"""

P_PERSPECTIVE = f"""
<section class="page" id="perspective">
  <h2>4. Tables from the API and UI point of view</h2>
  <p class="lead">Which table is filled by what, and who reads it.</p>
  <h3>What the UI page does (http://localhost:8010)</h3>
  <ul class="tight">
    <li><b>1 Upload</b> — pick files (type is detected from the column headers) → <code>POST /api/upload</code> → file lands in the inbox.</li>
    <li><b>2 Processing</b> — live status of each file/sheet → <code>GET /api/jobs</code> (table <code>etl_ingest_batch</code>); rejected rows → <code>GET /api/rejects/{{id}}</code>; "Rebuild summary now" → <code>POST /api/rebuild</code>.</li>
    <li><b>3 Database tables</b> — row counts and first rows of every table → <code>GET /api/overview</code>, <code>GET /api/table/{{name}}</code>.</li>
    <li><b>4 Data checks</b> — our summary vs the client's Cost Report → <code>GET /api/checks</code>.</li>
  </ul>
  <h3>What the background worker does</h3>
  <p>Watches <code>data/inbox</code> every 3 seconds. For each file: detect type → clean rows → bulk load (one transaction per sheet, all-or-nothing) → log in <code>etl_ingest_batch</code> → when the inbox is empty, rebuild <code>consolidated_order_summary</code> → move the file to <code>processed</code> or <code>failed</code>.</p>
  <h3>Table by table</h3>
  <div class="tw"><table><thead><tr><th>Table</th><th>Written by</th><th>Read by</th></tr></thead><tbody>{matrix_rows()}</tbody></table></div>
  <div class="callout">The Part B tables have no loader in our app yet. They will be filled by the SAP BTP extraction (or by files in the same shared-folder flow once we have a sample).</div>
</section>"""

P_FINDINGS = """
<section class="page" id="findings">
  <h2>5. Findings and open questions</h2>
  <h3>Things found in the client's data and SQL</h3>
  <ul class="tight">
    <li><b>Material cost is 0 on every order.</b> The SQL reads it from AUFK-USER4, which is empty. Real cost needs the valuation table (MBEW) or BOM prices.</li>
    <li><b>Labor cost is a formula, not SAP data:</b> batch size × 12.50.</li>
    <li><b>"Rework" = movement type 261</b>, which is the normal goods issue to an order. The client's own note warns it may not be true rework. Some orders show hundreds of "cycles". "48 hours per cycle" is hard-coded.</li>
    <li><b>Rework cycle definition differs:</b> SQL says COUNT(DISTINCT document); the delivered Cost Report equals the number of lines (matches all 26,456 orders; DISTINCT differs on 2,396). We follow the delivered report.</li>
    <li><b>The SQL would not run as written</b> (missing commas, aliases used in Step 4 that Step 1 never defines, HANA-only ADD_MONTHS).</li>
    <li><b>The rework extract is wider than the order extract:</b> 895,713 rework lines (39,248 orders) have no matching order in the base file.</li>
    <li>Order numbers lose their leading zeros in Excel; we pad them back to 12 digits so they join with the status key (OR + 12 digits).</li>
  </ul>
  <h3>Questions to ask</h3>
  <ul class="tight">
    <li><b>Client:</b> what is the true rework signal? Which cycle definition is intended? Where does real material cost live?</li>
    <li><b>SAP team:</b> ECC or S/4HANA? Is there an API for the status history (JEST / JCDS)? Which plants use recipes (PP-PI) and which routings? (FDS OI-007)</li>
    <li><b>Architecture:</b> where do extracted BOM / recipe data go — BTP HANA Cloud, Datasphere, or a local database? (FDS OI-002) Should material type, MRP type and procurement type be stored as columns? (OI-009)</li>
    <li><b>Access:</b> BTP login and server details (Pushkar / vendor); Cloud Connector set-up.</li>
  </ul>
  <h3>What is not done yet</h3>
  <ul class="tight">
    <li>No loader for Part B (needs the BTP extraction or a sample output file).</li>
    <li>Column types / lengths for Part B1 are our choice, based on the FDS sample values — verify against the real OData $metadata.</li>
    <li>Part B2 is a draft, a subset of the workbook's fields.</li>
    <li>The flow PDF and the Cloud Connector link have not been read.</li>
    <li>No login on the upload page; local use only.</li>
  </ul>
</section>"""

P_TABLES_HEAD = f"""
<section class="page" id="tables">
  <h2>6. One page per table</h2>
  <p class="lead">Each following page: purpose, the document where it is written, who fills and reads it, all columns, and the exact DDL.</p>
  {group_table('A')}{group_table('B1')}{group_table('B2')}
</section>"""

P_ALL = f"""
<section class="page" id="alldll">
  <h2>7. Complete DDL</h2><p class="lead">The full contents of <code>db/schema.sql</code> — run it on any empty PostgreSQL 15 database to create everything.</p>
  <div><button class="copy">Copy DDL</button><pre>{esc(schema)}</pre></div>
</section>"""

nav = ('<a href="#overview">Overview</a><a href="#docs">Documents</a><a href="#sources">Where written</a><a href="#database">Database</a>'
       '<a href="#perspective">API &amp; UI</a><a href="#findings">Findings</a><a href="#tables">Tables</a><a href="#alldll">Full DDL</a>')

page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>BOM Data Summary</title><style>{CSS}</style></head><body>'
        f'<header class="top"><div class="in"><b>BOM Data Summary</b><nav>{nav}</nav></div></header><div class="wrap">'
        f'{P_OVERVIEW}{P_DOCS}{P_SOURCES}{P_DB}{P_PERSPECTIVE}{P_FINDINGS}{P_TABLES_HEAD}'
        + "".join(table_page(t) for t in T) + P_ALL +
        f'</div><script>{JS}</script></body></html>')

out = ROOT / "docs" / "BOM_Data_Summary.html"
out.write_text(page, encoding="utf8")
print(f"written {out}  ({len(page)//1024} KB)  tables={n_tables} columns={n_cols}")
