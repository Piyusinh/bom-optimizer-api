"""Excel Mock Connector — ingests Excel files into canonical model."""
import uuid
from datetime import datetime
from typing import Optional
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import CanonicalBomHeader, CanonicalBomItem, OrderChain, AuditLog


def _to_datetime(value) -> Optional[datetime]:
    """Coerce pandas/Excel values to a naive datetime; NaN/NaT/blank -> None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        ts = pd.to_datetime(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts.to_pydatetime().replace(tzinfo=None)


async def _existing_headers(
    object_type: str, object_ids: list, db: AsyncSession
) -> dict:
    """Load previously-ingested headers keyed by object_id so a delta ingest
    can skip unchanged rows (LastChangeDateTime watermark) or refresh changed
    ones instead of blindly duplicating everything."""
    stmt = select(CanonicalBomHeader).where(
        CanonicalBomHeader.object_type == object_type,
        CanonicalBomHeader.object_id.in_(object_ids),
    )
    result = await db.execute(stmt)
    return {h.object_id: h for h in result.scalars().all()}


def _is_unchanged(existing, new_ts) -> bool:
    """True when the incoming row is not newer than the stored watermark."""
    return (
        existing is not None
        and existing.last_change_datetime is not None
        and new_ts is not None
        and new_ts <= existing.last_change_datetime
    )


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
    headers_created = 0
    headers_updated = 0
    headers_skipped = 0

    if "ProdOrder Header" in xl.sheet_names and "ProdOrder Component" in xl.sheet_names:
        header_df = pd.read_excel(file_path, sheet_name="ProdOrder Header")
        item_df = pd.read_excel(file_path, sheet_name="ProdOrder Component")
        header_df.columns = [c.strip() for c in header_df.columns]
        item_df.columns = [c.strip() for c in item_df.columns]

        items_by_order = {k: v for k, v in item_df.groupby("ManufacturingOrder")}
        existing = await _existing_headers(
            "PRODUCTION_ORDER",
            [str(o) for o in header_df["ManufacturingOrder"].tolist()],
            db,
        )

        for _, row in header_df.iterrows():
            order_id = str(row["ManufacturingOrder"])
            new_ts = _to_datetime(row.get("LastChangeDateTime"))
            prior = existing.get(order_id)
            if _is_unchanged(prior, new_ts):
                headers_skipped += 1
                continue
            if prior is not None:
                await db.delete(prior)
                headers_updated += 1
            else:
                headers_created += 1

            header_id = str(uuid.uuid4())
            header = CanonicalBomHeader(
                id=header_id,
                object_type="PRODUCTION_ORDER",
                object_id=order_id,
                material=str(row.get("Material", "UNKNOWN")),
                plant=str(row.get("Plant", "P01")),
                quantity=float(row.get("OrderQuantity", 1.0) or 1.0),
                base_unit=str(row.get("OrderQuantityUnit", "KG")),
                status=str(row["ManufacturingOrderStatus"]) if pd.notna(row.get("ManufacturingOrderStatus")) else None,
                order_start_date=_to_datetime(row.get("ActualStartDate")) or _to_datetime(row.get("BasicStartDate")),
                last_change_datetime=new_ts,
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
                    # Real client column — goods-issued qty against this
                    # component's reservation. This is the actual movement-
                    # type-261 data the RTM asks for (see consumption_engine.py).
                    withdrawn_quantity=(
                        float(crow["WithdrawnQuantity"])
                        if pd.notna(crow.get("WithdrawnQuantity"))
                        else None
                    ),
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

    return {
        "status": "success",
        "records_ingested": records,
        "headers_created": headers_created,
        "headers_updated": headers_updated,
        "headers_skipped": headers_skipped,
        "object_type": "PRODUCTION_ORDER",
    }


async def ingest_planned_order_bom(file_path: str, db: AsyncSession) -> dict:
    """
    Ingest Chemical_Planned_Order_BOM_Data.xlsx.

    Two sheets: 'PlannedOrder Header' + 'PlannedOrder Component', joined on
    PlannedOrder — same fix as production orders (real items live on the
    component sheet, not the header sheet).
    """
    xl = pd.ExcelFile(file_path)
    records = 0
    headers_created = 0
    headers_updated = 0
    headers_skipped = 0

    if "PlannedOrder Header" in xl.sheet_names and "PlannedOrder Component" in xl.sheet_names:
        header_df = pd.read_excel(file_path, sheet_name="PlannedOrder Header")
        item_df = pd.read_excel(file_path, sheet_name="PlannedOrder Component")
        header_df.columns = [c.strip() for c in header_df.columns]
        item_df.columns = [c.strip() for c in item_df.columns]

        items_by_order = {k: v for k, v in item_df.groupby("PlannedOrder")}
        existing = await _existing_headers(
            "PLANNED_ORDER",
            [str(o) for o in header_df["PlannedOrder"].tolist()],
            db,
        )

        for _, row in header_df.iterrows():
            order_id = str(row["PlannedOrder"])
            new_ts = _to_datetime(row.get("LastChangeDateTime"))
            prior = existing.get(order_id)
            if _is_unchanged(prior, new_ts):
                headers_skipped += 1
                continue
            if prior is not None:
                await db.delete(prior)
                headers_updated += 1
            else:
                headers_created += 1

            header_id = str(uuid.uuid4())
            header = CanonicalBomHeader(
                id=header_id,
                object_type="PLANNED_ORDER",
                object_id=order_id,
                material=str(row.get("Material", "UNKNOWN")),
                plant=str(row.get("Plant", "P01")),
                quantity=float(row.get("PlannedOrderQuantity", 1.0) or 1.0),
                base_unit=str(row.get("PlannedOrderQuantityUnit", "KG")),
                status=str(row["PlannedOrderStatus"]) if pd.notna(row.get("PlannedOrderStatus")) else None,
                order_start_date=_to_datetime(row.get("PlannedOrderStartDate")),
                last_change_datetime=new_ts,
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
    return {
        "status": "success",
        "records_ingested": records,
        "headers_created": headers_created,
        "headers_updated": headers_updated,
        "headers_skipped": headers_skipped,
        "object_type": "PLANNED_ORDER",
    }


async def ingest_sales_order_bom(file_path: str, db: AsyncSession) -> dict:
    """
    Ingest Chemical_Sales_Order_BOM_Data.xlsx.

    Two sheets: 'SO BOM Header' (SalesOrder + SalesOrderItem = one finished-
    good line) and 'SO BOM Item' (components, linked back via HigherLevelItem
    == header's SalesOrderItem — NOT a simple single-column join).
    """
    xl = pd.ExcelFile(file_path)
    records = 0
    headers_created = 0
    headers_updated = 0
    headers_skipped = 0

    if "SO BOM Header" in xl.sheet_names and "SO BOM Item" in xl.sheet_names:
        header_df = pd.read_excel(file_path, sheet_name="SO BOM Header")
        item_df = pd.read_excel(file_path, sheet_name="SO BOM Item")
        header_df.columns = [c.strip() for c in header_df.columns]
        item_df.columns = [c.strip() for c in item_df.columns]

        items_by_key = {k: v for k, v in item_df.groupby(["SalesOrder", "HigherLevelItem"])}
        object_ids = [
            f"{so}-{item}" for so, item in
            zip(header_df["SalesOrder"], header_df["SalesOrderItem"])
        ]
        existing = await _existing_headers("SALES_ORDER", object_ids, db)

        for _, row in header_df.iterrows():
            so = row["SalesOrder"]
            so_item = row["SalesOrderItem"]
            object_id = f"{so}-{so_item}"
            new_ts = _to_datetime(row.get("LastChangeDateTime"))
            prior = existing.get(object_id)
            if _is_unchanged(prior, new_ts):
                headers_skipped += 1
                continue
            if prior is not None:
                await db.delete(prior)
                headers_updated += 1
            else:
                headers_created += 1

            header_id = str(uuid.uuid4())
            header = CanonicalBomHeader(
                id=header_id,
                object_type="SALES_ORDER",
                object_id=object_id,
                material=str(row.get("Material", "UNKNOWN")),
                plant=str(row.get("Plant", "P01")),
                quantity=float(row.get("OrderQuantity", 1.0) or 1.0),
                base_unit=str(row.get("OrderQuantityUnit", "KG")),
                status=str(row["OverallSDProcessStatus"]) if pd.notna(row.get("OverallSDProcessStatus")) else None,
                order_start_date=_to_datetime(row.get("SalesOrderDate")),
                last_change_datetime=new_ts,
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
    return {
        "status": "success",
        "records_ingested": records,
        "headers_created": headers_created,
        "headers_updated": headers_updated,
        "headers_skipped": headers_skipped,
        "object_type": "SALES_ORDER",
    }


async def ingest_order_linkage(file_path: str, db: AsyncSession) -> dict:
    """Ingest Chemical_SO_PlanOrd_ProdOrd_Linkage.xlsx."""
    df = pd.read_excel(file_path)
    df.columns = [c.strip() for c in df.columns]

    col_map = _detect_linkage_columns(df)
    records = 0
    skipped = 0

    # Delta guard: the linkage file has no change timestamp, so dedup on the
    # chain key instead — a chain that already exists is skipped unchanged.
    existing_stmt = await db.execute(
        select(
            OrderChain.sales_order,
            OrderChain.sales_order_item,
            OrderChain.production_order,
        )
    )
    existing_keys = set(existing_stmt.all())

    for _, row in df.iterrows():
        sales_order = str(row.get(col_map.get("sales_order", ""), "")) if col_map.get("sales_order") else None
        sales_order_item = str(row.get(col_map.get("so_item", ""), "")) if col_map.get("so_item") else None
        production_order = str(row.get(col_map.get("production_order", ""), "")) if col_map.get("production_order") else None
        key = (sales_order, sales_order_item, production_order)
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)

        chain_stage = (
            str(row.get(col_map.get("chain_stage", ""), ""))
            if col_map.get("chain_stage") else None
        )
        chain = OrderChain(
            id=str(uuid.uuid4()),
            sales_order=sales_order,
            sales_order_item=sales_order_item,
            planned_order=str(row.get(col_map.get("planned_order", ""), "")) if col_map.get("planned_order") else None,
            production_order=production_order,
            material=str(row.get(col_map.get("material", ""), "UNKNOWN")),
            plant=str(row.get(col_map.get("plant", ""), "P01")),
            quantity=float(row.get(col_map.get("quantity", ""), 0)) if col_map.get("quantity") else None,
            chain_stage=chain_stage or None,
        )
        db.add(chain)
        records += 1

    await db.commit()
    return {
        "status": "success",
        "records_ingested": records,
        "records_skipped": skipped,
        "object_type": "ORDER_CHAIN",
    }


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
        # "salesorderitem" must come first: the real client column is
        # 'SalesOrderItem'. Its stripped form does not contain "soitem" or
        # "salesitem" as a contiguous substring, so those alone silently
        # matched nothing and left sales_order_item as None on every row.
        "so_item": ["sales order item", "salesorderitem", "so item", "so_item", "sales item"],
        "planned_order": ["planned order", "plan order", "planned_order", "plan_order"],
        "production_order": ["production order", "prod order", "production_order", "prod_order"],
        "material": ["material", "product", "finished"],
        "plant": ["plant", "plnt"],
        "quantity": ["quantity", "qty"],
        "chain_stage": ["chain stage", "chainstage", "chain_stage", "stage"],
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
