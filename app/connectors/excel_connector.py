"""Excel Mock Connector — ingests Excel files into canonical model."""
import uuid
from datetime import datetime
from typing import Optional
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import CanonicalBomHeader, CanonicalBomItem, OrderChain, AuditLog


async def ingest_production_order_bom(
    file_path: str, db: AsyncSession
) -> dict:
    """
    Ingest Chemical_Production_Order_BOM_Data.xlsx into canonical model.

    The workbook has TWO sheets: 'ProdOrder Header' (one row per production
    order) and 'ProdOrder Component' (the actual components consumed — this
    is where the real BOM item data lives). Both must be read and joined on
    ManufacturingOrder, or the resulting BOM headers end up with zero items.
    """
    xl = pd.ExcelFile(file_path)
    records = 0

    if "ProdOrder Header" in xl.sheet_names and "ProdOrder Component" in xl.sheet_names:
        header_df = pd.read_excel(file_path, sheet_name="ProdOrder Header")
        item_df = pd.read_excel(file_path, sheet_name="ProdOrder Component")
        header_df.columns = [c.strip() for c in header_df.columns]
        item_df.columns = [c.strip() for c in item_df.columns]

        items_by_order = {k: v for k, v in item_df.groupby("ManufacturingOrder")}

        for _, row in header_df.iterrows():
            order_id = row["ManufacturingOrder"]
            header_id = str(uuid.uuid4())
            header = CanonicalBomHeader(
                id=header_id,
                object_type="PRODUCTION_ORDER",
                object_id=str(order_id),
                material=str(row.get("Material", "UNKNOWN")),
                plant=str(row.get("Plant", "P01")),
                quantity=float(row.get("OrderQuantity", 1.0) or 1.0),
                base_unit=str(row.get("OrderQuantityUnit", "KG")),
                source_system="mock-excel",
                extracted_at=datetime.utcnow(),
            )
            db.add(header)

            comp_rows = items_by_order.get(order_id)
            if comp_rows is None:
                continue
            for _, crow in comp_rows.iterrows():
                comp = str(crow.get("Material", ""))
                if not comp or comp == "nan":
                    continue
                item = CanonicalBomItem(
                    id=str(uuid.uuid4()),
                    header_id=header_id,
                    item_number=int(crow["ManufacturingOrderItem"]) if pd.notna(crow.get("ManufacturingOrderItem")) else None,
                    component=comp,
                    quantity=float(crow.get("RequirementQuantity", 1.0) or 1.0),
                    unit=str(crow.get("RequirementQuantityUnit", "KG")),
                    scrap_percent=float(crow.get("ComponentScrapInPercent", 0.0) or 0.0),
                    requirement_date=crow.get("RequirementDate") if pd.notna(crow.get("RequirementDate")) else None,
                )
                db.add(item)
                records += 1
    else:
        # Fallback: unknown/flat structure, best-effort fuzzy column detection
        df = pd.read_excel(file_path)
        df.columns = [c.strip() for c in df.columns]
        col_map = _detect_columns(df, "production_order")
        if not col_map:
            return {"status": "error", "message": "Could not detect column mapping", "records_ingested": 0}

        group_col = col_map.get("order_id", col_map.get("bom_number"))
        mat_col = col_map.get("material", df.columns[0])
        group_key = group_col if group_col and group_col in df.columns else mat_col
        for key, group in df.groupby(group_key):
            header_id = str(uuid.uuid4())
            material = str(group.iloc[0].get(col_map.get("material", ""), key))
            plant = str(group.iloc[0].get(col_map.get("plant", ""), "P01"))
            quantity = float(group.iloc[0].get(col_map.get("quantity", ""), 1.0)) if col_map.get("quantity") else 1.0

            header = CanonicalBomHeader(
                id=header_id,
                object_type="PRODUCTION_ORDER",
                object_id=str(key),
                material=material,
                plant=plant,
                quantity=quantity,
                base_unit="KG",
                source_system="mock-excel",
                extracted_at=datetime.utcnow(),
            )
            db.add(header)

            for _, row in group.iterrows():
                comp = str(row.get(col_map.get("component", ""), ""))
                if not comp or comp == "nan":
                    continue
                comp_qty = float(row.get(col_map.get("comp_quantity", ""), 1.0)) if col_map.get("comp_quantity") else 1.0
                scrap = float(row.get(col_map.get("scrap", ""), 0.0)) if col_map.get("scrap") else 0.0

                item = CanonicalBomItem(
                    id=str(uuid.uuid4()),
                    header_id=header_id,
                    component=comp,
                    quantity=comp_qty,
                    unit="KG",
                    scrap_percent=scrap,
                )
                db.add(item)
                records += 1

    await db.commit()

    # Audit
    db.add(AuditLog(
        event_type="data_ingested",
        entity_type="PRODUCTION_ORDER",
        details={"file": file_path, "records": records},
    ))
    await db.commit()

    return {"status": "success", "records_ingested": records, "object_type": "PRODUCTION_ORDER"}


async def ingest_planned_order_bom(file_path: str, db: AsyncSession) -> dict:
    """
    Ingest Chemical_Planned_Order_BOM_Data.xlsx.

    Two sheets: 'PlannedOrder Header' + 'PlannedOrder Component', joined on
    PlannedOrder — same fix as production orders (real items live on the
    component sheet, not the header sheet).
    """
    xl = pd.ExcelFile(file_path)
    records = 0

    if "PlannedOrder Header" in xl.sheet_names and "PlannedOrder Component" in xl.sheet_names:
        header_df = pd.read_excel(file_path, sheet_name="PlannedOrder Header")
        item_df = pd.read_excel(file_path, sheet_name="PlannedOrder Component")
        header_df.columns = [c.strip() for c in header_df.columns]
        item_df.columns = [c.strip() for c in item_df.columns]

        items_by_order = {k: v for k, v in item_df.groupby("PlannedOrder")}

        for _, row in header_df.iterrows():
            order_id = row["PlannedOrder"]
            header_id = str(uuid.uuid4())
            header = CanonicalBomHeader(
                id=header_id,
                object_type="PLANNED_ORDER",
                object_id=str(order_id),
                material=str(row.get("Material", "UNKNOWN")),
                plant=str(row.get("Plant", "P01")),
                quantity=float(row.get("PlannedOrderQuantity", 1.0) or 1.0),
                base_unit=str(row.get("PlannedOrderQuantityUnit", "KG")),
                source_system="mock-excel",
            )
            db.add(header)

            comp_rows = items_by_order.get(order_id)
            if comp_rows is None:
                continue
            for _, crow in comp_rows.iterrows():
                comp = str(crow.get("Material", ""))
                if not comp or comp == "nan":
                    continue
                item = CanonicalBomItem(
                    id=str(uuid.uuid4()),
                    header_id=header_id,
                    item_number=int(crow["PlannedOrderItem"]) if pd.notna(crow.get("PlannedOrderItem")) else None,
                    component=comp,
                    quantity=float(crow.get("RequiredQuantity", 1.0) or 1.0),
                    unit=str(crow.get("RequiredQuantityUnit", "KG")),
                    scrap_percent=float(crow.get("ComponentScrapInPercent", 0.0) or 0.0),
                    requirement_date=crow.get("RequirementDate") if pd.notna(crow.get("RequirementDate")) else None,
                )
                db.add(item)
                records += 1
    else:
        df = pd.read_excel(file_path)
        df.columns = [c.strip() for c in df.columns]
        col_map = _detect_columns(df, "planned_order")
        group_col = col_map.get("order_id", col_map.get("bom_number"))

        if group_col and group_col in df.columns:
            for order_id, group in df.groupby(group_col):
                header_id = str(uuid.uuid4())
                material = str(group.iloc[0].get(col_map.get("material", ""), "UNKNOWN"))
                plant = str(group.iloc[0].get(col_map.get("plant", ""), "P01"))

                header = CanonicalBomHeader(
                    id=header_id,
                    object_type="PLANNED_ORDER",
                    object_id=str(order_id),
                    material=material,
                    plant=plant,
                    quantity=1.0,
                    base_unit="KG",
                    source_system="mock-excel",
                )
                db.add(header)

                for _, row in group.iterrows():
                    comp = str(row.get(col_map.get("component", ""), ""))
                    if not comp or comp == "nan":
                        continue
                    comp_qty = float(row.get(col_map.get("comp_quantity", ""), 1.0)) if col_map.get("comp_quantity") else 1.0

                    item = CanonicalBomItem(
                        id=str(uuid.uuid4()),
                        header_id=header_id,
                        component=comp,
                        quantity=comp_qty,
                        unit="KG",
                    )
                    db.add(item)
                    records += 1

    await db.commit()
    return {"status": "success", "records_ingested": records, "object_type": "PLANNED_ORDER"}


async def ingest_sales_order_bom(file_path: str, db: AsyncSession) -> dict:
    """
    Ingest Chemical_Sales_Order_BOM_Data.xlsx.

    Two sheets: 'SO BOM Header' (SalesOrder + SalesOrderItem = one finished-
    good line) and 'SO BOM Item' (components, linked back via HigherLevelItem
    == header's SalesOrderItem — NOT a simple single-column join).
    """
    xl = pd.ExcelFile(file_path)
    records = 0

    if "SO BOM Header" in xl.sheet_names and "SO BOM Item" in xl.sheet_names:
        header_df = pd.read_excel(file_path, sheet_name="SO BOM Header")
        item_df = pd.read_excel(file_path, sheet_name="SO BOM Item")
        header_df.columns = [c.strip() for c in header_df.columns]
        item_df.columns = [c.strip() for c in item_df.columns]

        items_by_key = {k: v for k, v in item_df.groupby(["SalesOrder", "HigherLevelItem"])}

        for _, row in header_df.iterrows():
            so = row["SalesOrder"]
            so_item = row["SalesOrderItem"]
            header_id = str(uuid.uuid4())
            header = CanonicalBomHeader(
                id=header_id,
                object_type="SALES_ORDER",
                object_id=f"{so}-{so_item}",
                material=str(row.get("Material", "UNKNOWN")),
                plant=str(row.get("Plant", "P01")),
                quantity=float(row.get("OrderQuantity", 1.0) or 1.0),
                base_unit=str(row.get("OrderQuantityUnit", "KG")),
                source_system="mock-excel",
            )
            db.add(header)

            comp_rows = items_by_key.get((so, so_item))
            if comp_rows is None:
                continue
            for _, crow in comp_rows.iterrows():
                comp = str(crow.get("Material", ""))
                if not comp or comp == "nan":
                    continue
                item = CanonicalBomItem(
                    id=str(uuid.uuid4()),
                    header_id=header_id,
                    item_number=int(crow["SalesOrderItem"]) if pd.notna(crow.get("SalesOrderItem")) else None,
                    component=comp,
                    quantity=float(crow.get("ComponentQuantity", 1.0) or 1.0),
                    unit=str(crow.get("ComponentUnit", "KG")),
                )
                db.add(item)
                records += 1
    else:
        df = pd.read_excel(file_path)
        df.columns = [c.strip() for c in df.columns]
        col_map = _detect_columns(df, "sales_order")
        group_col = col_map.get("order_id", col_map.get("bom_number"))

        if group_col and group_col in df.columns:
            for order_id, group in df.groupby(group_col):
                header_id = str(uuid.uuid4())
                material = str(group.iloc[0].get(col_map.get("material", ""), "UNKNOWN"))
                plant = str(group.iloc[0].get(col_map.get("plant", ""), "P01"))

                header = CanonicalBomHeader(
                    id=header_id,
                    object_type="SALES_ORDER",
                    object_id=str(order_id),
                    material=material,
                    plant=plant,
                    quantity=1.0,
                    base_unit="KG",
                    source_system="mock-excel",
                )
                db.add(header)

                for _, row in group.iterrows():
                    comp = str(row.get(col_map.get("component", ""), ""))
                    if not comp or comp == "nan":
                        continue
                    comp_qty = float(row.get(col_map.get("comp_quantity", ""), 1.0)) if col_map.get("comp_quantity") else 1.0

                    item = CanonicalBomItem(
                        id=str(uuid.uuid4()),
                        header_id=header_id,
                        component=comp,
                        quantity=comp_qty,
                        unit="KG",
                    )
                    db.add(item)
                    records += 1

    await db.commit()
    return {"status": "success", "records_ingested": records, "object_type": "SALES_ORDER"}


async def ingest_order_linkage(file_path: str, db: AsyncSession) -> dict:
    """Ingest Chemical_SO_PlanOrd_ProdOrd_Linkage.xlsx."""
    df = pd.read_excel(file_path)
    df.columns = [c.strip() for c in df.columns]

    col_map = _detect_linkage_columns(df)
    records = 0

    for _, row in df.iterrows():
        chain = OrderChain(
            id=str(uuid.uuid4()),
            sales_order=str(row.get(col_map.get("sales_order", ""), "")) if col_map.get("sales_order") else None,
            sales_order_item=str(row.get(col_map.get("so_item", ""), "")) if col_map.get("so_item") else None,
            planned_order=str(row.get(col_map.get("planned_order", ""), "")) if col_map.get("planned_order") else None,
            production_order=str(row.get(col_map.get("production_order", ""), "")) if col_map.get("production_order") else None,
            material=str(row.get(col_map.get("material", ""), "UNKNOWN")),
            plant=str(row.get(col_map.get("plant", ""), "P01")),
            quantity=float(row.get(col_map.get("quantity", ""), 0)) if col_map.get("quantity") else None,
        )
        db.add(chain)
        records += 1

    await db.commit()
    return {"status": "success", "records_ingested": records, "object_type": "ORDER_CHAIN"}


def _detect_columns(df: pd.DataFrame, order_type: str) -> dict:
    """Auto-detect column names from Excel headers (case-insensitive fuzzy match)."""
    cols = {c.lower(): c for c in df.columns}
    mapping = {}

    # Order ID patterns
    for pattern in ["production order", "prod order", "prod_order", "order number", "order_number",
                    "planned order", "plan order", "plan_order", "sales order", "so number",
                    "bom number", "bom_number", "order"]:
        for key, orig in cols.items():
            if pattern.replace(" ", "") in key.replace(" ", "").replace("_", ""):
                mapping["order_id"] = orig
                break
        if "order_id" in mapping:
            break

    # Material
    for pattern in ["material", "finished", "product", "fg material", "fg_material"]:
        for key, orig in cols.items():
            if pattern.replace(" ", "") in key.replace(" ", "").replace("_", ""):
                if "component" not in key and "comp" not in key and "raw" not in key:
                    mapping["material"] = orig
                    break
        if "material" in mapping:
            break

    # Plant
    for pattern in ["plant", "plnt", "werk"]:
        for key, orig in cols.items():
            if pattern in key:
                mapping["plant"] = orig
                break
        if "plant" in mapping:
            break

    # Component
    for pattern in ["component", "comp material", "comp_material", "raw material", "bom component"]:
        for key, orig in cols.items():
            if pattern.replace(" ", "") in key.replace(" ", "").replace("_", ""):
                mapping["component"] = orig
                break
        if "component" in mapping:
            break

    # Component quantity
    for pattern in ["comp qty", "comp_qty", "component qty", "component_qty", "item qty", "item quantity",
                    "requirement qty", "req qty"]:
        for key, orig in cols.items():
            if pattern.replace(" ", "") in key.replace(" ", "").replace("_", ""):
                mapping["comp_quantity"] = orig
                break
        if "comp_quantity" in mapping:
            break

    # Quantity (header)
    if "comp_quantity" not in mapping:
        for pattern in ["quantity", "qty", "order qty"]:
            for key, orig in cols.items():
                if pattern.replace(" ", "") in key.replace(" ", "").replace("_", ""):
                    mapping["comp_quantity"] = orig
                    break
            if "comp_quantity" in mapping:
                break

    # Scrap
    for pattern in ["scrap", "scrap%", "scrap_percent"]:
        for key, orig in cols.items():
            if pattern.replace(" ", "") in key.replace(" ", "").replace("_", ""):
                mapping["scrap"] = orig
                break
        if "scrap" in mapping:
            break

    return mapping


def _detect_linkage_columns(df: pd.DataFrame) -> dict:
    """Detect columns for the linkage file."""
    cols = {c.lower(): c for c in df.columns}
    mapping = {}

    patterns = {
        "sales_order": ["sales order", "so number", "so_number", "sales_order"],
        "so_item": ["so item", "so_item", "sales item"],
        "planned_order": ["planned order", "plan order", "planned_order", "plan_order"],
        "production_order": ["production order", "prod order", "production_order", "prod_order"],
        "material": ["material", "product", "finished"],
        "plant": ["plant", "plnt"],
        "quantity": ["quantity", "qty"],
    }

    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            for key, orig in cols.items():
                if pattern.replace(" ", "") in key.replace(" ", "").replace("_", ""):
                    mapping[field] = orig
                    break
            if field in mapping:
                break

    return mapping
