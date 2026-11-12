-- =====================================================================
-- BOM / Paint-data database  (PostgreSQL 15)
-- Idempotent: safe to run on every start (all objects use IF NOT EXISTS).
--
-- PART A   Paint order cost & rework      -> data we HAVE today (loaded by the app)
-- PART B1  FDS extraction tables (ds1/ds2/ds3) -> exact FDS v1.5 field lists, empty until BTP extraction
-- PART B2  Draft model-feature tables    -> from the SAP Data Reference workbook, NOT in the FDS
--
-- =====================================================================

-- ---------------------------------------------------------------------
-- PART A0. Load log  (one row per file + sheet)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etl_ingest_batch (
    batch_id      BIGSERIAL    PRIMARY KEY,
    dataset       VARCHAR(20)  NOT NULL
                  CHECK (dataset IN ('ORDER_BASE','ORDER_REWORK','ORDER_STATUS','COST_REPORT','UNKNOWN')),
    source_file   VARCHAR(255) NOT NULL,
    source_sheet  VARCHAR(50)  NOT NULL DEFAULT '',
    file_sha256   CHAR(64)     NOT NULL,
    rows_in_file  INTEGER      NOT NULL DEFAULT 0,
    rows_loaded   INTEGER      NOT NULL DEFAULT 0,
    rows_rejected INTEGER      NOT NULL DEFAULT 0,
    status        VARCHAR(10)  NOT NULL DEFAULT 'RUNNING'
                  CHECK (status IN ('RUNNING','SUCCESS','FAILED','SKIPPED')),
    message       TEXT,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
-- the same file+sheet content can only be loaded once successfully
CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_once
    ON etl_ingest_batch (dataset, file_sha256, source_sheet)
    WHERE status IN ('RUNNING','SUCCESS');

-- rows we refused to load (first 1000 per batch), with the reason
CREATE TABLE IF NOT EXISTS etl_reject_row (
    reject_id  BIGSERIAL PRIMARY KEY,
    batch_id   BIGINT    NOT NULL REFERENCES etl_ingest_batch(batch_id) ON DELETE CASCADE,
    row_number INTEGER   NOT NULL,
    reason     TEXT      NOT NULL,
    raw_row    TEXT
);
CREATE INDEX IF NOT EXISTS ix_reject_batch ON etl_reject_row (batch_id);

-- ---------------------------------------------------------------------
-- PART A1. STG_ORDER_BASE   (SAP AUFK + AFKO + AFPO + MAKT)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_order_base (
    aufnr                CHAR(12)      PRIMARY KEY,     -- process order, zero-padded
    autyp                SMALLINT      NOT NULL,        -- 40 = process order
    werks                VARCHAR(4)    NOT NULL,        -- plant (US01 / US18)
    objnr                VARCHAR(22)   NOT NULL UNIQUE, -- 'OR'+aufnr, status key
    matnr                VARCHAR(18)   NOT NULL,        -- material / SKU
    maktx                VARCHAR(40),                   -- material description
    gstrp                DATE          NOT NULL,        -- basic start date
    batch_size_quantity  NUMERIC(15,3) NOT NULL,        -- (= PSMNG in the file)
    base_unit_of_measure VARCHAR(3)    NOT NULL,        -- LB, KG ...
    material_cost        NUMERIC(18,3) NOT NULL DEFAULT 0,
    labor_cost           NUMERIC(18,3) NOT NULL DEFAULT 0,
    batch_id             BIGINT        NOT NULL REFERENCES etl_ingest_batch(batch_id)
);
CREATE INDEX IF NOT EXISTS ix_stg_base_matnr      ON stg_order_base (matnr);
CREATE INDEX IF NOT EXISTS ix_stg_base_werks_gstrp ON stg_order_base (werks, gstrp);

-- ---------------------------------------------------------------------
-- PART A2. STG_ORDER_REWORK  (SAP MSEG + MKPF, movement type 261, raw rows)
--          The extract has no line item (ZEILE) -> surrogate key.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_order_rework (
    rework_id  BIGSERIAL     PRIMARY KEY,
    aufnr      CHAR(12)      NOT NULL,
    mblnr      VARCHAR(10)   NOT NULL,        -- material document
    mjahr      SMALLINT      NOT NULL,        -- document year
    dmbtr      NUMERIC(15,2) NOT NULL,        -- amount in local currency
    bwart      VARCHAR(3)    NOT NULL,        -- movement type ('261')
    budat      DATE          NOT NULL,        -- posting date
    cpudt      DATE,
    cputm      TIME,
    xblnr      VARCHAR(16),
    tcode2     VARCHAR(20),
    vbeln_im   VARCHAR(10),
    vbelp_im   INTEGER,
    batch_id   BIGINT        NOT NULL REFERENCES etl_ingest_batch(batch_id)
);
CREATE INDEX IF NOT EXISTS ix_stg_rework_aufnr       ON stg_order_rework (aufnr);
CREATE INDEX IF NOT EXISTS ix_stg_rework_aufnr_mblnr ON stg_order_rework (aufnr, mblnr);
CREATE INDEX IF NOT EXISTS ix_stg_rework_budat       ON stg_order_rework (budat);

-- ---------------------------------------------------------------------
-- PART A3. STG_ORDER_STATUS  (SAP JEST + JCDS, status I0012 = Delivered)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_order_status (
    objnr            VARCHAR(22) PRIMARY KEY,
    dlv_status_date  DATE        NOT NULL,
    batch_id         BIGINT      NOT NULL REFERENCES etl_ingest_batch(batch_id)
);

-- ---------------------------------------------------------------------
-- PART A4. CONSOLIDATED_ORDER_SUMMARY  (SQL Step 4 - rebuilt after loads)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consolidated_order_summary (
    process_order_number    CHAR(12)      PRIMARY KEY,
    plant_id                VARCHAR(4)    NOT NULL,
    material_sku            VARCHAR(18)   NOT NULL,
    material_description    VARCHAR(40),
    base_unit_of_measure    VARCHAR(3),
    batch_size_quantity     NUMERIC(15,3) NOT NULL,
    number_of_rework_cycles INTEGER       NOT NULL DEFAULT 0,
    rework_duration_hours   INTEGER       NOT NULL DEFAULT 0,   -- cycles x 48 (client assumption)
    standard_cost           NUMERIC(18,6),                      -- material_cost / batch size
    material_cost           NUMERIC(18,3) NOT NULL DEFAULT 0,
    labor_cost              NUMERIC(18,3) NOT NULL DEFAULT 0,
    total_standard_cost     NUMERIC(18,3) NOT NULL DEFAULT 0,
    total_rework_cost       NUMERIC(18,2) NOT NULL DEFAULT 0,
    ai_projected_savings    NUMERIC(18,2) NOT NULL DEFAULT 0,   -- 30% of rework cost
    dlv_status_date         DATE,                               -- NULL = not delivered
    refreshed_at            TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cons_material ON consolidated_order_summary (material_sku);
CREATE INDEX IF NOT EXISTS ix_cons_plant    ON consolidated_order_summary (plant_id);
CREATE INDEX IF NOT EXISTS ix_cons_dlv      ON consolidated_order_summary (dlv_status_date);

-- ---------------------------------------------------------------------
-- PART A5. REF_COST_REPORT  (client's own "Cost Report" - used only to
--          check that our calculated summary matches theirs)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref_cost_report (
    aufnr                   CHAR(12)      PRIMARY KEY,
    matnr                   VARCHAR(18),
    werks                   VARCHAR(4),
    maktx                   VARCHAR(40),
    autyp                   SMALLINT,
    objnr                   VARCHAR(22),
    dlv_status_date         DATE,
    report_year             SMALLINT,
    batch_size_quantity     NUMERIC(15,3),
    base_unit_of_measure    VARCHAR(3),
    number_of_rework_cycles INTEGER,          -- NULL in the file when there is none
    rework_duration_hours   INTEGER,
    standard_cost           NUMERIC(18,6),
    material_cost           NUMERIC(18,3),
    labor_cost              NUMERIC(18,3),
    total_standard_cost     NUMERIC(18,3),
    total_rework_cost       NUMERIC(18,2),
    ai_projected_savings    NUMERIC(18,2),
    batch_id                BIGINT        NOT NULL REFERENCES etl_ingest_batch(batch_id)
);


-- =====================================================================
-- PART B1. FDS extraction tables  (SAP BTP app output)
--
-- Source: FDS-PP-BOM-001 v1.5 "SAP Production BOM Extraction with Alternate Versions"
--   section 12.4  Storage Design  -> job_run, t_sel_variant, ds1/ds2/ds3
--   section 7.2   Dataset 1 (23 fields)   section 7.3  Dataset 2 (12 fields)
--   section 7.4   Dataset 3 (17 fields)   section 13   sample output (value formats)
--
-- Column names are the FDS OData property names in snake_case.
-- The OData property, SAP field and source view of every column are in COMMENT ON COLUMN.
-- Types/lengths are NOT stated in the FDS: they are standard S/4HANA lengths chosen
-- from the FDS sample values (e.g. material 40 chars, BOM number 8, plant 4).
-- Tables stay empty until the BTP extraction runs or an equivalent file is loaded.
-- =====================================================================

-- Saved UI selections (FDS 6.3 / 12.4: T_SEL_VARIANT)
CREATE TABLE IF NOT EXISTS t_sel_variant (
    variant_id     VARCHAR(40)  PRIMARY KEY,
    owner          VARCHAR(80),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    changed_at     TIMESTAMPTZ,
    selection_json JSONB        NOT NULL,   -- plants, materials, materialTypes, mrpTypes,
                                            -- procurementTypes, excludeMaterialDeleted, bomStatus,
                                            -- validityDate, includeDeletedBoms
    frequency      VARCHAR(10)  NOT NULL DEFAULT 'ON_DEMAND'
                   CHECK (frequency IN ('ON_DEMAND','DAILY','WEEKLY','MONTHLY'))
);

-- One row per extraction run (FDS 12.4: job_run)
CREATE TABLE IF NOT EXISTS job_run (
    run_id              BIGSERIAL    PRIMARY KEY,
    variant_id          VARCHAR(40),                -- no FK: a run keeps its own snapshot (BR-015)
    selection_snapshot  JSONB        NOT NULL,      -- filters used for THIS run
    triggered_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    status              VARCHAR(10)  NOT NULL DEFAULT 'RUNNING'
                        CHECK (status IN ('RUNNING','COMPLETE','FAILED')),
    materials_total     INTEGER,
    materials_processed INTEGER      NOT NULL DEFAULT 0,
    message             TEXT                         -- added: reason when status = FAILED
);

-- ---------------------------------------------------------------------
-- DATASET 1  Production BOM  (FDS 7.2, 23 fields, + run_id)
-- One row per Material x Plant x BOM alternative x BOM item node.
-- API_BILL_OF_MATERIAL_SRV  A_BillOfMaterial + $expand to_BillOfMaterialItem
-- Not unique-constrained on the link key: ECM / change numbers are out of scope (OI-001),
-- so the same item node may appear with different validity dates.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ds1_bom_components (
    ds1_id                            BIGSERIAL    PRIMARY KEY,
    run_id                            BIGINT       NOT NULL REFERENCES job_run(run_id) ON DELETE CASCADE,
    -- header fields #1-#11
    material                          VARCHAR(40)  NOT NULL,   -- #1  MATNR
    plant                             VARCHAR(4)   NOT NULL,   -- #2  WERKS
    bill_of_material_variant_usage    VARCHAR(1)   NOT NULL,   -- #3  STLAN (always '1')
    bill_of_material                  VARCHAR(8)   NOT NULL,   -- #4  STLNR
    bill_of_material_variant          VARCHAR(2)   NOT NULL,   -- #5  STLAL (01-99)
    bom_version_status                VARCHAR(2),              -- #6  STLST
    header_validity_start_date        DATE,                    -- #7  DATUV
    bom_is_marked_for_deletion        BOOLEAN,                 -- #8  header deletion flag
    bom_header_quantity_in_base_unit  NUMERIC(13,3),           -- #9  BMENG
    bom_header_base_unit              VARCHAR(3),              -- #10 BMEIN
    bom_header_text                   VARCHAR(40),             -- #11 STKTX
    -- item fields #12-#23
    bill_of_material_item_node_number VARCHAR(8)   NOT NULL,   -- #12 STLKN
    bill_of_material_component        VARCHAR(40),             -- #13 IDNRK
    component_quantity                NUMERIC(13,3),           -- #14 MENGE
    component_unit                    VARCHAR(3),              -- #15 MEINS
    bill_of_material_item_category    VARCHAR(1),              -- #16 POSTP (L/N/R/T)
    validity_start_date               DATE,                    -- #17 DATUV (item)
    validity_end_date                 DATE,                    -- #18 DATUB
    is_marked_for_deletion            BOOLEAN,                 -- #19 LOEKZ
    is_phantom_item                   BOOLEAN,                 -- #20 DUMPS
    bom_item_is_costing_relevant      BOOLEAN,                 -- #21 SANKA
    bom_item_is_bulk_material         BOOLEAN,                 -- #22 SCHGT
    is_backflush_enabled              BOOLEAN                  -- #23 RGEKZ
);
CREATE INDEX IF NOT EXISTS ix_ds1_link ON ds1_bom_components
    (run_id, material, plant, bill_of_material, bill_of_material_variant, bill_of_material_item_node_number);
CREATE INDEX IF NOT EXISTS ix_ds1_variant   ON ds1_bom_components (material, plant, bill_of_material_variant);
CREATE INDEX IF NOT EXISTS ix_ds1_component ON ds1_bom_components (bill_of_material_component);

COMMENT ON TABLE ds1_bom_components IS 'FDS Dataset 1 - Production BOM, 23 fields + run_id (API_BILL_OF_MATERIAL_SRV)';
COMMENT ON COLUMN ds1_bom_components.material                          IS '#1 OData Material | SAP MATNR | I_BOMHeader | finished/header material';
COMMENT ON COLUMN ds1_bom_components.plant                             IS '#2 OData Plant | SAP WERKS | I_BOMHeader';
COMMENT ON COLUMN ds1_bom_components.bill_of_material_variant_usage    IS '#3 OData BillOfMaterialVariantUsage | SAP STLAN | I_BOMHeader | always 1 = Production';
COMMENT ON COLUMN ds1_bom_components.bill_of_material                  IS '#4 OData BillOfMaterial | SAP STLNR | I_BOMHeader | BOM internal number';
COMMENT ON COLUMN ds1_bom_components.bill_of_material_variant          IS '#5 OData BillOfMaterialVariant | SAP STLAL | I_BOMHeader | alternate BOM 01-99';
COMMENT ON COLUMN ds1_bom_components.bom_version_status                IS '#6 OData BOMVersionStatus | SAP STLST | I_BOMHeader | 01=Active, 02=Inactive';
COMMENT ON COLUMN ds1_bom_components.header_validity_start_date        IS '#7 OData HeaderValidityStartDate | SAP DATUV | I_BOMHeader';
COMMENT ON COLUMN ds1_bom_components.bom_is_marked_for_deletion        IS '#8 OData BOMIsMarkedForDeletion | SAP LKENZ (as written in the FDS) | I_BOMHeader';
COMMENT ON COLUMN ds1_bom_components.bom_header_quantity_in_base_unit  IS '#9 OData BOMHeaderQuantityInBaseUnit | SAP BMENG | I_BOMHeader | reference qty for component qty';
COMMENT ON COLUMN ds1_bom_components.bom_header_base_unit              IS '#10 OData BOMHeaderBaseUnit | SAP BMEIN | I_BOMHeader';
COMMENT ON COLUMN ds1_bom_components.bom_header_text                   IS '#11 OData BOMHeaderText | SAP STKTX | I_BOMHeader';
COMMENT ON COLUMN ds1_bom_components.bill_of_material_item_node_number IS '#12 OData BillOfMaterialItemNodeNumber | SAP STLKN | I_BOMItem';
COMMENT ON COLUMN ds1_bom_components.bill_of_material_component        IS '#13 OData BillOfMaterialComponent | SAP IDNRK | I_BOMItem | component / raw material';
COMMENT ON COLUMN ds1_bom_components.component_quantity                IS '#14 OData ComponentQuantity | SAP MENGE | I_BOMItem | per header base quantity';
COMMENT ON COLUMN ds1_bom_components.component_unit                    IS '#15 OData ComponentUnit | SAP MEINS | I_BOMItem';
COMMENT ON COLUMN ds1_bom_components.bill_of_material_item_category    IS '#16 OData BillOfMaterialItemCategory | SAP POSTP | I_BOMItem | L=Stock N=Non-stock R=Variable-size T=Text';
COMMENT ON COLUMN ds1_bom_components.validity_start_date               IS '#17 OData ValidityStartDate | SAP DATUV | I_BOMItem | used to pick the component version active on a date';
COMMENT ON COLUMN ds1_bom_components.validity_end_date                 IS '#18 OData ValidityEndDate | SAP DATUB | I_BOMItem';
COMMENT ON COLUMN ds1_bom_components.is_marked_for_deletion            IS '#19 OData IsMarkedForDeletion | SAP LOEKZ | I_BOMItem';
COMMENT ON COLUMN ds1_bom_components.is_phantom_item                   IS '#20 OData IsPhantomItem | SAP DUMPS | I_BOMItem';
COMMENT ON COLUMN ds1_bom_components.bom_item_is_costing_relevant      IS '#21 OData BOMItemIsCostingRelevant | SAP SANKA | I_BOMItem';
COMMENT ON COLUMN ds1_bom_components.bom_item_is_bulk_material         IS '#22 OData BOMItemIsBulkMaterial | SAP SCHGT | I_BOMItem';
COMMENT ON COLUMN ds1_bom_components.is_backflush_enabled              IS '#23 OData IsBackflushEnabled | SAP RGEKZ | I_BOMItem';

-- ---------------------------------------------------------------------
-- DATASET 2  Production Version  (FDS 7.3, 12 fields, + run_id)
-- One row per Material x Plant x Production Version.
-- API_PRODUCT_SRV  A_ProductionVersion.  Links: BOM alternative -> ds1, recipe -> ds3.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ds2_production_version (
    run_id                          BIGINT       NOT NULL REFERENCES job_run(run_id) ON DELETE CASCADE,
    material                        VARCHAR(40)  NOT NULL,   -- #1  Material
    plant                           VARCHAR(4)   NOT NULL,   -- #2  Plant
    production_version              VARCHAR(4)   NOT NULL,   -- #3  ProductionVersion (0001, 0002 ...)
    production_version_text         VARCHAR(40),             -- #4  ProductionVersionText
    bill_of_material_variant        VARCHAR(2),              -- #5  BillOfMaterialVariant -> ds1
    mfg_order_routing_group         VARCHAR(8),              -- #6  MfgOrderRoutingGroup (PLNNR) -> ds3
    mfg_order_routing_grp_counter   VARCHAR(2),              -- #7  MfgOrderRoutingGrpCounter (PLNAL) -> ds3
    minimum_lot_size_quantity       NUMERIC(13,3),           -- #8
    maximum_lot_size_quantity       NUMERIC(13,3),           -- #9
    production_version_valid_from   DATE,                    -- #10
    production_version_valid_to     DATE,                    -- #11
    is_marked_for_deletion          BOOLEAN,                 -- #12 flagged, not excluded
    PRIMARY KEY (run_id, material, plant, production_version)
);
CREATE INDEX IF NOT EXISTS ix_ds2_bom    ON ds2_production_version (material, plant, bill_of_material_variant);
CREATE INDEX IF NOT EXISTS ix_ds2_recipe ON ds2_production_version (mfg_order_routing_group, mfg_order_routing_grp_counter);
COMMENT ON TABLE ds2_production_version IS 'FDS Dataset 2 - Production Version, 12 fields + run_id (API_PRODUCT_SRV / A_ProductionVersion)';
COMMENT ON COLUMN ds2_production_version.material                      IS '#1 OData Material | joins to ds1';
COMMENT ON COLUMN ds2_production_version.plant                         IS '#2 OData Plant | joins to ds1';
COMMENT ON COLUMN ds2_production_version.production_version            IS '#3 OData ProductionVersion | e.g. 0001, 0002';
COMMENT ON COLUMN ds2_production_version.production_version_text       IS '#4 OData ProductionVersionText';
COMMENT ON COLUMN ds2_production_version.bill_of_material_variant      IS '#5 OData BillOfMaterialVariant | BOM alternative of this version, joins ds1.bill_of_material_variant';
COMMENT ON COLUMN ds2_production_version.mfg_order_routing_group       IS '#6 OData MfgOrderRoutingGroup | recipe group PLNNR, primary key to ds3';
COMMENT ON COLUMN ds2_production_version.mfg_order_routing_grp_counter IS '#7 OData MfgOrderRoutingGrpCounter | recipe counter PLNAL, secondary key to ds3';
COMMENT ON COLUMN ds2_production_version.minimum_lot_size_quantity     IS '#8 OData MinimumLotSizeQuantity | min batch/lot size this version is valid for';
COMMENT ON COLUMN ds2_production_version.maximum_lot_size_quantity     IS '#9 OData MaximumLotSizeQuantity | max batch/lot size this version is valid for';
COMMENT ON COLUMN ds2_production_version.production_version_valid_from IS '#10 OData ProductionVersionValidFrom | drives seasonal / alternate BOM selection';
COMMENT ON COLUMN ds2_production_version.production_version_valid_to   IS '#11 OData ProductionVersionValidTo';
COMMENT ON COLUMN ds2_production_version.is_marked_for_deletion        IS '#12 OData IsMarkedForDeletion | flagged in output, not excluded';

-- ---------------------------------------------------------------------
-- DATASET 3  Master Recipe  (FDS 7.4, 17 fields, + run_id)
-- One row per Recipe group x Recipe x Operation (x allocated component).
-- Header #1-#8 repeat on every operation row; operation #9-#14; component #15-#17
-- are empty for operations that consume nothing (e.g. QA testing).
-- API_MASTERRECIPE_0001  A_MasterRecipeHeader + $expand operations + components
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ds3_master_recipe (
    ds3_id                          BIGSERIAL    PRIMARY KEY,
    run_id                          BIGINT       NOT NULL REFERENCES job_run(run_id) ON DELETE CASCADE,
    recipe_group                    VARCHAR(8)   NOT NULL,   -- #1  RecipeGroup (PLNNR)
    recipe                          VARCHAR(2)   NOT NULL,   -- #2  Recipe (PLNAL)
    recipe_desc                     VARCHAR(40),             -- #3  RecipeDesc
    plant                           VARCHAR(4),              -- #4  Plant
    recipe_status                   VARCHAR(2),              -- #5  RecipeStatus (4 = released)
    valid_from                      DATE,                    -- #6  ValidFrom
    base_quantity                   NUMERIC(13,3),           -- #7  BaseQuantity
    base_unit                       VARCHAR(3),              -- #8  BaseUnit
    operation_group                 VARCHAR(8)   NOT NULL,   -- #9  OperationGroup
    operation_node                  VARCHAR(8)   NOT NULL,   -- #10 OperationNode
    operation_text                  VARCHAR(40),             -- #11 OperationText
    work_center                     VARCHAR(20),             -- #12 WorkCenter
    standard_process_duration       NUMERIC(13,3),           -- #13 StandardProcessDuration
    standard_process_duration_unit  VARCHAR(3),              -- #14 H / MIN
    bill_of_material_component      VARCHAR(40),             -- #15 -> ds1 component
    component_quantity              NUMERIC(13,3),           -- #16
    component_unit                  VARCHAR(3)               -- #17
);
CREATE INDEX IF NOT EXISTS ix_ds3_recipe    ON ds3_master_recipe (run_id, recipe_group, recipe);
CREATE INDEX IF NOT EXISTS ix_ds3_component ON ds3_master_recipe (bill_of_material_component);
COMMENT ON TABLE ds3_master_recipe IS 'FDS Dataset 3 - Master Recipe, 17 fields + run_id (API_MASTERRECIPE_0001). OI-007: only valid for PP-PI plants.';
COMMENT ON COLUMN ds3_master_recipe.recipe_group                   IS '#1 OData RecipeGroup (header) | PLNNR, joins ds2.mfg_order_routing_group';
COMMENT ON COLUMN ds3_master_recipe.recipe                         IS '#2 OData Recipe (header) | PLNAL, joins ds2.mfg_order_routing_grp_counter';
COMMENT ON COLUMN ds3_master_recipe.recipe_desc                    IS '#3 OData RecipeDesc (header)';
COMMENT ON COLUMN ds3_master_recipe.plant                          IS '#4 OData Plant (header)';
COMMENT ON COLUMN ds3_master_recipe.recipe_status                  IS '#5 OData RecipeStatus (header) | 4 = released for order';
COMMENT ON COLUMN ds3_master_recipe.valid_from                     IS '#6 OData ValidFrom (header)';
COMMENT ON COLUMN ds3_master_recipe.base_quantity                  IS '#7 OData BaseQuantity (header)';
COMMENT ON COLUMN ds3_master_recipe.base_unit                      IS '#8 OData BaseUnit (header)';
COMMENT ON COLUMN ds3_master_recipe.operation_group                IS '#9 OData OperationGroup (operation)';
COMMENT ON COLUMN ds3_master_recipe.operation_node                 IS '#10 OData OperationNode (operation)';
COMMENT ON COLUMN ds3_master_recipe.operation_text                 IS '#11 OData OperationText (operation)';
COMMENT ON COLUMN ds3_master_recipe.work_center                    IS '#12 OData WorkCenter (operation)';
COMMENT ON COLUMN ds3_master_recipe.standard_process_duration      IS '#13 OData StandardProcessDuration (operation)';
COMMENT ON COLUMN ds3_master_recipe.standard_process_duration_unit IS '#14 OData StandardProcessDurationUnit (operation) | H or MIN';
COMMENT ON COLUMN ds3_master_recipe.bill_of_material_component     IS '#15 OData BillOfMaterialComponent (component allocation) | links to ds1';
COMMENT ON COLUMN ds3_master_recipe.component_quantity             IS '#16 OData ComponentQuantity (component allocation)';
COMMENT ON COLUMN ds3_master_recipe.component_unit                 IS '#17 OData ComponentUnit (component allocation)';

-- =====================================================================
-- PART B2. DRAFT model-feature tables  (NOT in the FDS)
-- Field names come from the "SAP Data Reference" workbook (Field Master List /
-- Transactional Data / Companion Data). Types/lengths are standard SAP, assumed.
-- Only a subset of the workbook's fields is included. Confirm the extraction
-- method with the SAP team (OI-002 / workbook "Extraction Notes") before relying on them.
-- sap_material_valuation (MBEW) matters: it is where REAL material cost lives,
-- because material cost in the paint order data is 0 on every order.
-- =====================================================================

-- Material master: MARA + MAKT
CREATE TABLE IF NOT EXISTS sap_material (
    matnr  VARCHAR(18) PRIMARY KEY,
    maktx  VARCHAR(40),            -- MAKT description (EN)
    mtart  VARCHAR(4),             -- material type FERT / HALB / ROH / VERP
    matkl  VARCHAR(9),             -- material group
    spart  VARCHAR(2),             -- division
    meins  VARCHAR(3),             -- base unit
    brgew  NUMERIC(13,3),          -- gross weight
    ntgew  NUMERIC(13,3),          -- net weight
    gewei  VARCHAR(3),
    volum  NUMERIC(13,3),
    voleh  VARCHAR(3),
    xchpf  BOOLEAN,                -- batch managed
    prdha  VARCHAR(18),            -- product hierarchy
    lvorm  BOOLEAN,                -- deletion flag
    ersda  DATE,
    stoff  VARCHAR(18)             -- hazardous material number
);

-- Material by plant: MARC (MRP / procurement / lead times)
CREATE TABLE IF NOT EXISTS sap_material_plant (
    matnr  VARCHAR(18) NOT NULL REFERENCES sap_material(matnr),
    werks  VARCHAR(4)  NOT NULL,
    dismm  VARCHAR(2),             -- MRP type
    dispo  VARCHAR(3),             -- MRP controller
    disls  VARCHAR(2),             -- lot-sizing procedure
    bstmi  NUMERIC(13,3),          -- min lot size
    bstma  NUMERIC(13,3),          -- max lot size
    bstfe  NUMERIC(13,3),          -- fixed lot size
    minbe  NUMERIC(13,3),          -- reorder point
    eisbe  NUMERIC(13,3),          -- safety stock
    plifz  SMALLINT,               -- planned delivery days
    dzeit  SMALLINT,               -- in-house production days
    wzeit  SMALLINT,               -- total replenishment lead time
    beskz  VARCHAR(1),             -- procurement type E / F / X
    sobsl  VARCHAR(2),
    rgekz  VARCHAR(1),             -- backflush indicator
    lgpro  VARCHAR(4),
    fevor  VARCHAR(3),             -- production scheduler
    qmpur  BOOLEAN,               -- QM active in procurement
    PRIMARY KEY (matnr, werks)
);

-- Valuation / cost: MBEW  (this is where REAL material cost lives)
CREATE TABLE IF NOT EXISTS sap_material_valuation (
    matnr  VARCHAR(18) NOT NULL REFERENCES sap_material(matnr),
    bwkey  VARCHAR(4)  NOT NULL,   -- valuation area (= plant)
    bwtar  VARCHAR(10) NOT NULL DEFAULT '',
    vprsv  VARCHAR(1),             -- price control S / V
    stprs  NUMERIC(15,4),          -- standard price
    verpr  NUMERIC(15,4),          -- moving average price
    peinh  INTEGER,                -- price unit
    bklas  VARCHAR(4),
    lbkum  NUMERIC(15,3),
    salk3  NUMERIC(18,2),
    PRIMARY KEY (matnr, bwkey, bwtar)
);

-- Stock by storage location: MARD
CREATE TABLE IF NOT EXISTS sap_material_stock (
    matnr  VARCHAR(18) NOT NULL REFERENCES sap_material(matnr),
    werks  VARCHAR(4)  NOT NULL,
    lgort  VARCHAR(4)  NOT NULL,
    labst  NUMERIC(13,3),          -- unrestricted
    insme  NUMERIC(13,3),          -- quality inspection
    speme  NUMERIC(13,3),          -- blocked
    PRIMARY KEY (matnr, werks, lgort)
);

-- Order confirmation (actual start/end, yield, scrap): AFRU
CREATE TABLE IF NOT EXISTS sap_order_confirmation (
    conf_id BIGSERIAL PRIMARY KEY,
    aufnr  CHAR(12)    NOT NULL,
    rmzhl  VARCHAR(8),
    isdd   DATE, isdz TIME,        -- actual start
    iedd   DATE, iedz TIME,        -- actual end
    gamng  NUMERIC(13,3),          -- yield
    xmnga  NUMERIC(13,3),          -- scrap
    arbpl  VARCHAR(8)              -- work center
);
CREATE INDEX IF NOT EXISTS ix_conf_aufnr ON sap_order_confirmation (aufnr);

-- Order component reservations (planned vs withdrawn): RESB
CREATE TABLE IF NOT EXISTS sap_order_component (
    rsnum  VARCHAR(10) NOT NULL,
    rspos  VARCHAR(4)  NOT NULL,
    aufnr  CHAR(12),
    matnr  VARCHAR(18),            -- component
    bdmng  NUMERIC(13,3),          -- required qty
    enmng  NUMERIC(13,3),          -- withdrawn qty
    PRIMARY KEY (rsnum, rspos)
);
CREATE INDEX IF NOT EXISTS ix_comp_aufnr ON sap_order_component (aufnr);

-- Batch master: MCHA
CREATE TABLE IF NOT EXISTS sap_batch (
    matnr  VARCHAR(18) NOT NULL,
    werks  VARCHAR(4)  NOT NULL,
    charg  VARCHAR(10) NOT NULL,
    hsdat  DATE,                   -- production date
    vfdat  DATE,                   -- expiry
    lwedt  DATE,                   -- last goods receipt
    PRIMARY KEY (matnr, werks, charg)
);


-- =====================================================================================
-- PART C  BOM Optimizer application model (design: docs/BOM_Optimizer_HLD_LLD.html s5)
--   Layer M  master (m_material + views over the FDS tables)
--   Layer T  transactional   Layer R  refined candidates   Layer A  rules / runs / adoption
-- Postgres syntax for local build; HANA mapping is in the design document (5.6).
-- =====================================================================================
-- m_material : one row per material + plant (MARA + MARC + MAKT + cost from MBEW)
CREATE TABLE IF NOT EXISTS m_material (
  material            VARCHAR(40)  NOT NULL,          -- MATNR
  plant               VARCHAR(4)   NOT NULL,          -- WERKS
  description         VARCHAR(40),                    -- MAKT-MAKTX
  material_type       VARCHAR(4),                     -- MARA-MTART FERT/HALB/ROH/VERP
  cross_plant_status  VARCHAR(2),                     -- MARA-MSTAE
  plant_status        VARCHAR(2),                     -- MARC-MMSTA
  deletion_flag       BOOLEAN NOT NULL DEFAULT FALSE, -- MARA-LVORM / MARC-LVORM
  procurement_type    VARCHAR(1),                     -- MARC-BESKZ E in-house / F external
  mrp_type            VARCHAR(2),                     -- MARC-DISMM
  mrp_controller      VARCHAR(3),                     -- MARC-DISPO
  base_unit           VARCHAR(3),                     -- MARA-MEINS
  batch_managed       BOOLEAN,                        -- MARA/MARC-XCHPF
  material_grade      VARCHAR(18),                    -- OPEN: which SAP field holds "grade"
  price_control       VARCHAR(1),                     -- MBEW-VPRSV S / V
  standard_price      NUMERIC(15,5),                  -- MBEW-STPRS
  moving_price        NUMERIC(15,5),                  -- MBEW-VERPR
  price_unit          NUMERIC(5,0),                   -- MBEW-PEINH
  currency            VARCHAR(5),
  stock_unrestricted  NUMERIC(15,3),                  -- MARD-LABST summed
  loaded_run_id       BIGINT,                         -- extraction run
  loaded_at           TIMESTAMP NOT NULL DEFAULT now(),
  PRIMARY KEY (material, plant)
);


CREATE TABLE IF NOT EXISTS t_process_order (
  order_no          CHAR(12) PRIMARY KEY,             -- AUFNR, zero padded
  plant             VARCHAR(4) NOT NULL,
  material          VARCHAR(40) NOT NULL,
  order_type        VARCHAR(4),                       -- AUART
  system_status     VARCHAR(60),                      -- e.g. REL, TECO, DLV
  planned_start     DATE,                             -- header start date (season driver)
  actual_start      DATE,
  delivered_date    DATE,                             -- JCDS I0012 latest
  target_qty        NUMERIC(15,3), base_unit VARCHAR(3),
  production_version VARCHAR(4),                      -- VERID
  bom_ref           VARCHAR(20), recipe_group VARCHAR(8), recipe_counter VARCHAR(2),
  production_line   VARCHAR(20),                      -- work centre / line. OPEN: which field
  parent_order_no   CHAR(12) REFERENCES t_process_order(order_no),  -- rework hierarchy. OPEN (Questions 1-3)
  order_role        VARCHAR(10) DEFAULT 'ORIGINAL',   -- ORIGINAL | REWORK
  loaded_at TIMESTAMP DEFAULT now()
);
CREATE TABLE IF NOT EXISTS t_process_order_component (
  order_no CHAR(12) REFERENCES t_process_order(order_no), item_no VARCHAR(4),
  component VARCHAR(40) NOT NULL, required_qty NUMERIC(15,3), withdrawn_qty NUMERIC(15,3),
  unit VARCHAR(3), batch VARCHAR(10), PRIMARY KEY (order_no, item_no)     -- RESB
);
CREATE TABLE IF NOT EXISTS t_process_order_operation (
  order_no CHAR(12) REFERENCES t_process_order(order_no), operation_no VARCHAR(4),
  work_center VARCHAR(8), standard_hours NUMERIC(11,3), actual_hours NUMERIC(11,3),
  PRIMARY KEY (order_no, operation_no)                                    -- AFVC / AFRU
);
CREATE TABLE IF NOT EXISTS t_goods_movement (
  movement_id BIGSERIAL PRIMARY KEY, order_no CHAR(12) NOT NULL, doc_no VARCHAR(10), doc_year VARCHAR(4),
  movement_type VARCHAR(3) NOT NULL,                -- 261 issue; others later
  material VARCHAR(40), qty NUMERIC(15,3), unit VARCHAR(3), amount NUMERIC(15,2), posting_date DATE
);                                                  -- = stg_order_rework (paint data) widened
CREATE TABLE IF NOT EXISTS t_planned_order (
  planned_order_no VARCHAR(10) PRIMARY KEY, plant VARCHAR(4), material VARCHAR(40),
  qty NUMERIC(15,3), unit VARCHAR(3), start_date DATE, finish_date DATE, bom_ref VARCHAR(20),
  production_version VARCHAR(4), status VARCHAR(10)
);
CREATE TABLE IF NOT EXISTS t_rework_link (                        -- the Questions sheet, rows 1-3. DEFINITION OPEN
  rework_order_no CHAR(12) PRIMARY KEY, original_order_no CHAR(12) NOT NULL,
  cycle_no SMALLINT NOT NULL DEFAULT 1, link_source VARCHAR(20)   -- SAP_LINK | INFERRED_261
);
CREATE TABLE IF NOT EXISTS r_candidate (
  candidate_id   BIGSERIAL PRIMARY KEY,
  material       VARCHAR(40) NOT NULL, plant VARCHAR(4) NOT NULL,
  candidate_type VARCHAR(12) NOT NULL,              -- STANDARD | ALTERNATE | HISTORICAL
  bom_ref        VARCHAR(20), bom_variant VARCHAR(2), production_version VARCHAR(4),
  recipe_group   VARCHAR(8),  recipe_counter VARCHAR(2),
  signature      CHAR(64) NOT NULL,                 -- SHA-256 of sorted (component, qty per base unit)
  valid_from DATE, valid_to DATE,
  built_run_id BIGINT, UNIQUE (material, plant, signature)
);
CREATE TABLE IF NOT EXISTS r_candidate_item (
  candidate_id BIGINT REFERENCES r_candidate ON DELETE CASCADE, component VARCHAR(40),
  qty_per_base NUMERIC(15,6) NOT NULL, unit VARCHAR(3), is_phantom BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (candidate_id, component)
);
CREATE TABLE IF NOT EXISTS r_candidate_stats (
  candidate_id BIGINT PRIMARY KEY REFERENCES r_candidate ON DELETE CASCADE,
  times_used INT, last_used_date DATE, first_used_date DATE,
  avg_reprocess_cycles NUMERIC(6,2),                -- from t_rework_link
  cycle1_pct NUMERIC(5,2), cycle2_pct NUMERIC(5,2), cycle3_pct NUMERIC(5,2),   -- the 78/96/100 strip
  cost_per_unit NUMERIC(15,5),                      -- from m_material prices, see 7.2
  operation_hours NUMERIC(11,3), lead_time_days NUMERIC(8,2),
  season_counts JSONB,                              -- {"WINTER":3,"SUMMER":9,...}
  lines_used JSONB,                                 -- {"LINE01":12}
  computed_at TIMESTAMP DEFAULT now()
);
CREATE TABLE IF NOT EXISTS a_criterion (                          -- what the user can pick as Rank 1..4
  criterion_code VARCHAR(20) PRIMARY KEY,           -- COST, RECENCY, REWORK_CYCLES, SEASON, PRODUCTION_LINE, EFFICIENCY
  label VARCHAR(60) NOT NULL, direction VARCHAR(3) NOT NULL CHECK (direction IN ('MIN','MAX')),
  metric_expr VARCHAR(200) NOT NULL,                -- column in r_candidate_stats or registered function
  tie_band_pct NUMERIC(5,2) DEFAULT 0,              -- see 7.3
  active BOOLEAN DEFAULT TRUE, sort_order INT
);
CREATE TABLE IF NOT EXISTS a_rule (                               -- the rule engine's catalogue
  rule_id VARCHAR(10) PRIMARY KEY,                  -- R-01 ...
  name VARCHAR(100) NOT NULL, description TEXT,
  rule_type VARCHAR(12) NOT NULL CHECK (rule_type IN ('HARD','WARN','FILTER','FLAG','CALC')),
  scope VARCHAR(12) NOT NULL,                       -- MATERIAL | COMPONENT | CANDIDATE | ORDER
  handler VARCHAR(60) NOT NULL,                     -- registered python function name
  params JSONB DEFAULT '{}',                        -- e.g. {"types":["VERP"]}
  exec_order INT NOT NULL,                          -- evaluation order
  source VARCHAR(12) NOT NULL,                      -- EXCEL | OLD_CODE | PROPOSED | CLIENT_CONFIRMED
  active BOOLEAN DEFAULT TRUE, version INT DEFAULT 1,
  valid_from DATE, valid_to DATE, updated_by VARCHAR(80), updated_at TIMESTAMP DEFAULT now()
);
CREATE TABLE IF NOT EXISTS a_rule_set (                           -- a named, versioned bundle; a run records its id
  rule_set_id SERIAL PRIMARY KEY, name VARCHAR(60), version INT, rule_ids JSONB, active BOOLEAN, created_at TIMESTAMP DEFAULT now()
);
CREATE TABLE IF NOT EXISTS a_season_map (month SMALLINT PRIMARY KEY CHECK (month BETWEEN 1 AND 12), season VARCHAR(20) NOT NULL);
CREATE TABLE IF NOT EXISTS a_legal_status (                       -- SOURCE OPEN; manual or imported until client names the system
  material VARCHAR(40), plant VARCHAR(4), status VARCHAR(10) NOT NULL,   -- APPROVED | PENDING | RESTRICTED
  note VARCHAR(200), valid_from DATE, valid_to DATE, source VARCHAR(30), PRIMARY KEY (material, plant)
);
CREATE TABLE IF NOT EXISTS a_ranking_run (
  run_id UUID PRIMARY KEY, requested_by VARCHAR(80) NOT NULL, requested_at TIMESTAMP DEFAULT now(),
  plant VARCHAR(4) NOT NULL, material VARCHAR(40), planned_order VARCHAR(10), process_order CHAR(12), production_line VARCHAR(20),
  anchor_type VARCHAR(16), baseline_candidate_id BIGINT, target_date DATE,
  rule_set_id INT, data_as_of TIMESTAMP, status VARCHAR(10), duration_ms INT
);
CREATE TABLE IF NOT EXISTS a_ranking_run_criterion (
  run_id UUID REFERENCES a_ranking_run, rank_no SMALLINT CHECK (rank_no BETWEEN 1 AND 4),
  criterion_code VARCHAR(20) REFERENCES a_criterion, PRIMARY KEY (run_id, rank_no)
);
CREATE TABLE IF NOT EXISTS a_ranking_result (
  run_id UUID REFERENCES a_ranking_run, candidate_id BIGINT REFERENCES r_candidate,
  rank_no INT, feasible BOOLEAN NOT NULL, violations JSONB, warnings JSONB,
  metrics JSONB NOT NULL,                           -- {cost_per_unit, cost_change_pct, avg_cycles, baseline_cycles, ...}
  reason_text TEXT, legal_status VARCHAR(10), PRIMARY KEY (run_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS a_adoption (
  adoption_id UUID PRIMARY KEY, run_id UUID NOT NULL, candidate_id BIGINT NOT NULL, target_order CHAR(12) NOT NULL,
  idempotency_key CHAR(64) UNIQUE NOT NULL,
  status VARCHAR(12) NOT NULL,                      -- STAGED, APPROVED, REJECTED, SENDING, CONFIRMED, FAILED, CANCELLED
  requested_by VARCHAR(80), requested_at TIMESTAMP DEFAULT now(), reason TEXT,
  approved_by VARCHAR(80), approved_at TIMESTAMP,
  sap_request JSONB, sap_response JSONB, sap_http_status INT, error_text TEXT, attempts INT DEFAULT 0, last_attempt_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS a_audit_log (
  audit_id BIGSERIAL PRIMARY KEY, event_type VARCHAR(30) NOT NULL, entity_type VARCHAR(30), entity_id VARCHAR(60),
  actor VARCHAR(80), details JSONB, created_at TIMESTAMP DEFAULT now()
);
CREATE TABLE IF NOT EXISTS a_extraction_job (                     -- = FDS job_run, generalised
  job_id BIGSERIAL PRIMARY KEY, job_type VARCHAR(20), scope JSONB, status VARCHAR(10),
  started_at TIMESTAMP, finished_at TIMESTAMP, rows_loaded BIGINT, message TEXT
);
-- The 4 core master objects of the RTM 'Requirements' sheet. Three are the FDS tables.
CREATE OR REPLACE VIEW m_bom                AS SELECT * FROM ds1_bom_components;
CREATE OR REPLACE VIEW m_recipe             AS SELECT * FROM ds3_master_recipe;
CREATE OR REPLACE VIEW m_production_version AS SELECT * FROM ds2_production_version;

-- seed data
INSERT INTO a_criterion VALUES
 ('COST','Total cost','MIN','cost_per_unit',2.0,TRUE,1),
 ('RECENCY','Most recently used','MAX','last_used_date',0,TRUE,2),
 ('REWORK_CYCLES','Reprocess cycles','MIN','avg_reprocess_cycles',5.0,TRUE,3),
 ('SEASON','Season match','MAX','season_match',0,TRUE,4),
 ('PRODUCTION_LINE','Production line match','MAX','line_match',0,TRUE,5),
 ('EFFICIENCY','Operation hours','MIN','operation_hours',2.0,TRUE,6)
ON CONFLICT DO NOTHING;
INSERT INTO a_season_map VALUES (1,'WINTER'),(2,'WINTER'),(3,'SUMMER'),(4,'SUMMER'),(5,'SUMMER'),
 (6,'MONSOON'),(7,'MONSOON'),(8,'MONSOON'),(9,'MONSOON'),(10,'POST_MONSOON'),(11,'POST_MONSOON'),(12,'WINTER')
ON CONFLICT DO NOTHING;
-- season buckets are the old-code default (Indian calendar); the client has NOT confirmed them
-- Rule catalogue (design section 6.2). Handlers are registered python names; they are NOT written yet.
INSERT INTO a_rule (rule_id,name,description,rule_type,scope,handler,params,exec_order,source,active) VALUES
 ('R-01','Components must be active','Status active and not marked for deletion','HARD','COMPONENT','component_active','{}',10,'EXCEL',TRUE),
 ('R-02','Only FERT/HALB get BOM candidates','Output materials limited to finished and intermediate','FILTER','MATERIAL','output_material_types','{"types":["FERT","HALB"]}',5,'EXCEL',TRUE),
 ('R-03','Ignore non-stock items when costing','Item categories T and N are not stock','FILTER','COMPONENT','exclude_item_categories','{"categories":["T","N"]}',6,'OLD_CODE',TRUE),
 ('R-04','Explode through phantoms','Phantom assemblies are not costed as stock','FILTER','COMPONENT','explode_phantoms','{}',7,'EXCEL',TRUE),
 ('R-05','Final quantity = recipe + goods issues 261','Used when building historical candidates and costing','CALC','CANDIDATE','final_quantity_261','{"movement_type":"261"}',8,'EXCEL',TRUE),
 ('R-06','Raw-material grade acceptable','Needs definition: which field holds grade','HARD','COMPONENT','grade_acceptable','{}',20,'EXCEL',FALSE),
 ('R-07','Raw-material availability','HARD for planned-order simulation, WARN otherwise','WARN','COMPONENT','availability','{"hard_for":["PLANNED_ORDER"]}',30,'EXCEL',TRUE),
 ('R-08','Valid on planned start date','Production version validity window (ADATU/BDATU)','HARD','CANDIDATE','valid_on_start_date','{}',15,'EXCEL',TRUE),
 ('R-09','Prefer same production line','Shown as flag and as ranking criterion','FLAG','CANDIDATE','line_match','{}',40,'EXCEL',TRUE),
 ('R-10','Legal approval status','Yellow/green flag; adopt blocked unless APPROVED','FLAG','CANDIDATE','legal_status','{"block_adopt_unless":"APPROVED"}',50,'EXCEL',TRUE),
 ('R-11','Customer component restriction','Needs a data source','HARD','COMPONENT','customer_restriction','{}',25,'EXCEL',FALSE),
 ('R-12','Components exist in material master','','HARD','COMPONENT','component_registered','{}',11,'OLD_CODE',TRUE),
 ('R-13','User constraints','Avoid list, max components, minimum quantity per component','HARD','CANDIDATE','request_constraints','{}',12,'OLD_CODE',TRUE),
 ('R-14','Minimum historical uses','Warn when a historical candidate was used fewer times','WARN','CANDIDATE','min_occurrences','{"min":3}',60,'PROPOSED',TRUE),
 ('R-15','No cycle evidence','Flag candidates with no cycle data instead of showing 0','WARN','CANDIDATE','cycle_evidence','{}',61,'PROPOSED',TRUE),
 ('R-16','Baseline is a reference, not a proposal','Do not propose the baseline as an improvement','FILTER','CANDIDATE','exclude_baseline','{}',70,'PROPOSED',TRUE)
ON CONFLICT (rule_id) DO NOTHING;
INSERT INTO a_rule_set (name,version,rule_ids,active)
 SELECT 'default',1,(SELECT jsonb_agg(rule_id ORDER BY exec_order) FROM a_rule WHERE active),TRUE
 WHERE NOT EXISTS (SELECT 1 FROM a_rule_set WHERE name='default' AND version=1);

-- widen: PROCESS_ORDER / PLANNED_ORDER do not fit in 10 characters
ALTER TABLE a_ranking_run ALTER COLUMN anchor_type TYPE VARCHAR(16);

-- candidates no longer produced by a rebuild are kept (stored runs reference them) but marked not current
ALTER TABLE r_candidate ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT TRUE;
