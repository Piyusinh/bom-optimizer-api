"""
Synthetic Demo Data Generator — Phase 0 fallback dataset.

Produces a small, deterministic, guaranteed-to-work Excel dataset in the
EXACT same multi-sheet shape our real connectors expect (ProdOrder Header/
Component, PlannedOrder Header/Component, SO BOM Header/Item, Linkage).

Why this exists: the real mock workbooks supplied by the client vary in
quality and structure across versions, and a live demo should never risk
showing "0 candidates" because of an upstream data-quality surprise. This
generator builds a compact, self-contained dataset where every finished
material is deliberately produced multiple times using 2-3 *different*
component recipes with a skewed usage frequency (e.g. Recipe A 60% of the
time, Recipe B 30%, Recipe C 10%) — this is the exact "same product, many
historical combinations" story the platform is built to prove.

Usage:
    from app.connectors.demo_data_generator import generate_demo_workbooks
    paths = generate_demo_workbooks("/path/to/output/dir")
    # -> {"production_order": "...xlsx", "planned_order": "...xlsx",
    #     "sales_order": "...xlsx", "linkage": "...xlsx"}
"""
import os
import random
from datetime import datetime, timedelta
import pandas as pd

# ─── Fixed demo universe (deterministic — same seed every run) ──────────────

random.seed(42)

PLANTS = [("1000", "Demo Bulk Plant (Hamburg)"), ("2000", "Demo Packing Plant (Ohio)")]

FINISHED_MATERIALS = [
    ("DEMO-F001", "Liquid Detergent Concentrate - Lemon"),
    ("DEMO-F002", "Powder Detergent Concentrate - Lavender"),
    ("DEMO-F003", "Fabric Softener - Ocean Breeze"),
    ("DEMO-F004", "Dish Soap Concentrate - Citrus"),
    ("DEMO-F005", "Surface Cleaner - Pine"),
    ("DEMO-F006", "Glass Cleaner - Blue"),
    ("DEMO-F007", "Toilet Bowl Cleaner - Ocean"),
    ("DEMO-F008", "Hand Soap Refill - Aloe"),
]

RAW_MATERIALS = [f"DEMO-R{n:03d}" for n in range(1, 16)]
PACKAGING = [f"DEMO-A{n:03d}" for n in range(1, 9)]


def _build_recipes(material: str) -> list[list[tuple[str, float, str]]]:
    """Return 2-3 distinct component recipes (component, qty, unit) for a material."""
    rng = random.Random(hash(material) % (2**31))
    base_raws = rng.sample(RAW_MATERIALS, 3)
    alt_raw = rng.choice([r for r in RAW_MATERIALS if r not in base_raws])
    pack = rng.sample(PACKAGING, 2)

    recipe_a = [(base_raws[0], round(rng.uniform(40, 60), 2), "KG"),
                (base_raws[1], round(rng.uniform(20, 35), 2), "KG"),
                (pack[0], round(rng.uniform(1, 5), 2), "EA")]
    recipe_b = [(base_raws[0], round(rng.uniform(40, 60), 2), "KG"),
                (alt_raw, round(rng.uniform(20, 35), 2), "KG"),  # substituted raw material
                (pack[0], round(rng.uniform(1, 5), 2), "EA")]
    recipe_c = [(base_raws[0], round(rng.uniform(40, 60), 2), "KG"),
                (base_raws[1], round(rng.uniform(20, 35), 2), "KG"),
                (base_raws[2], round(rng.uniform(5, 15), 2), "KG"),
                (pack[1], round(rng.uniform(1, 5), 2), "EA")]  # different packaging + extra component
    return [recipe_a, recipe_b, recipe_c]


def generate_demo_workbooks(output_dir: str) -> dict[str, str]:
    """Generate the 4 demo Excel workbooks and return their file paths."""
    os.makedirs(output_dir, exist_ok=True)

    prod_headers, prod_items = [], []
    plan_headers, plan_items = [], []
    so_headers, so_items = [], []
    linkage_rows = []

    order_seq = 70000001
    plan_seq = 80000001
    so_seq = 4600000001
    link_seq = 1

    for material, mat_desc in FINISHED_MATERIALS:
        for plant, plant_name in PLANTS:
            recipes = _build_recipes(material)
            # Skewed usage: recipe A used most often, B less, C rarely —
            # this is what the Decision Core's occurrence_count/historical
            # frequency scoring is designed to surface.
            usage_plan = [0] * 6 + [1] * 3 + [2] * 1  # 6xA, 3xB, 1xC = 10 orders
            random.shuffle(usage_plan)

            for run_idx, recipe_idx in enumerate(usage_plan):
                recipe = recipes[recipe_idx]
                order_id = order_seq
                planned_id = plan_seq
                so_id = so_seq
                order_seq += 1
                plan_seq += 1
                so_seq += 1

                start = datetime(2026, 1, 1) + timedelta(days=run_idx * 12)
                # Fixed order quantity (not randomized) — this is essential:
                # component quantities are derived proportionally from qty,
                # so runs using the SAME recipe must produce IDENTICAL
                # absolute component quantities for the dedup/occurrence-
                # count logic in explosion_engine.py to correctly recognize
                # them as the same historical combination.
                qty = 1000.0

                prod_headers.append({
                    "ManufacturingOrder": order_id,
                    "ManufacturingOrderType": "PP01",
                    "ManufacturingOrderTypeDescription": "Standard Production Order",
                    "Material": material,
                    "MaterialType": "FERT",
                    "MaterialDescription": mat_desc,
                    "Plant": plant,
                    "PlantName": plant_name,
                    "MRPController": "M01",
                    "ProductionSupervisor": "PS-DEMO",
                    "OrderQuantity": qty,
                    "OrderQuantityUnit": "KG",
                    "ConfirmedYieldQuantity": round(qty * 0.97, 1),
                    "ManufacturingOrderStatus": "Confirmed",
                    "BasicStartDate": start,
                    "BasicEndDate": start + timedelta(days=2),
                    "ActualReleaseDate": start,
                    "ActualStartDate": start,
                    "ActualFinishDate": start + timedelta(days=2),
                    "LinkedPlannedOrder": planned_id,
                    "LinkedSalesOrder": so_id,
                    "LinkedSalesOrderItem": 10,
                    "CreatedByUser": "DEMO_GEN",
                    "CreationDate": start,
                    "LastChangeDateTime": start,
                })
                for item_no, (comp, comp_qty, unit) in enumerate(recipe, start=1):
                    prod_items.append({
                        "ManufacturingOrder": order_id,
                        "ManufacturingOrderItem": item_no * 10,
                        "ProductionOrderComponent": item_no,
                        "Material": comp,
                        "MaterialType": "ROH" if comp.startswith("DEMO-R") else "VERP",
                        "MaterialDescription": f"{comp} component",
                        "Plant": plant,
                        "RequirementQuantity": round(comp_qty * qty / 100, 2),
                        "RequirementQuantityUnit": unit,
                        "WithdrawnQuantity": round(comp_qty * qty / 100 * 0.98, 2),
                        "Reservation": 90000000 + item_no,
                        "ReservationItem": 1,
                        "RequirementDate": start,
                        "ComponentScrapInPercent": 0,
                        "ItemText": f"{comp} - demo component",
                    })

                # Planned order mirrors the same recipe (pre-execution intent)
                plan_headers.append({
                    "PlannedOrder": planned_id,
                    "PlannedOrderProcurementType": "E",
                    "PlannedOrderProcurementTypeDescription": "In-house production",
                    "Material": material,
                    "MaterialType": "FERT",
                    "MaterialDescription": mat_desc,
                    "Plant": plant,
                    "PlantName": plant_name,
                    "MRPController": "M01",
                    "PlannedOrderQuantity": qty,
                    "PlannedOrderQuantityUnit": "KG",
                    "MRPDate": start - timedelta(days=5),
                    "PlannedOrderStartDate": start - timedelta(days=3),
                    "PlannedOrderFinishDate": start,
                    "PlannedOrderIsFirmed": True,
                    "PlannedOrderStatus": "Converted",
                    "LinkedSalesOrder": so_id,
                    "LinkedSalesOrderItem": 10,
                    "LinkedProductionOrder": order_id,
                    "CreatedByUser": "DEMO_GEN",
                    "CreationDate": start - timedelta(days=5),
                    "LastChangeDateTime": start - timedelta(days=3),
                })
                for item_no, (comp, comp_qty, unit) in enumerate(recipe, start=1):
                    plan_items.append({
                        "PlannedOrder": planned_id,
                        "PlannedOrderItem": item_no * 10,
                        "Material": comp,
                        "MaterialType": "ROH" if comp.startswith("DEMO-R") else "VERP",
                        "MaterialDescription": f"{comp} component",
                        "Plant": plant,
                        "RequiredQuantity": round(comp_qty * qty / 100, 2),
                        "RequiredQuantityUnit": unit,
                        "RequirementDate": start - timedelta(days=3),
                        "ComponentScrapInPercent": 0,
                        "Reservation": 91000000 + item_no,
                        "ItemText": f"{comp} - demo component",
                    })

                # Sales order — one FG line (item 10) + its component breakdown
                so_headers.append({
                    "SalesOrder": so_id,
                    "SalesOrderItem": 10,
                    "SalesOrderType": "OR",
                    "SalesOrderTypeDescription": "Standard Order",
                    "SoldToParty": 100000 + run_idx,
                    "SoldToPartyName": f"Demo Customer {run_idx + 1}",
                    "SoldToPartyCountry": "US",
                    "SalesOrganization": "1000",
                    "DistributionChannel": "10",
                    "Division": "00",
                    "Plant": plant,
                    "PlantName": plant_name,
                    "Material": material,
                    "MaterialType": "FERT",
                    "MaterialDescription": mat_desc,
                    "HigherLevelItem": None,
                    "OrderQuantity": qty,
                    "OrderQuantityUnit": "KG",
                    "SalesOrderDate": start - timedelta(days=10),
                    "RequestedDeliveryDate": start + timedelta(days=5),
                    "OverallSDProcessStatus": "Completed",
                    "LinkedPlannedOrder": planned_id,
                    "LinkedProductionOrder": order_id,
                    "CreatedByUser": "DEMO_GEN",
                    "LastChangeDateTime": start,
                })
                for item_no, (comp, comp_qty, unit) in enumerate(recipe, start=1):
                    so_items.append({
                        "SalesOrder": so_id,
                        "SalesOrderItem": 10 + item_no * 10,
                        "HigherLevelItem": 10,
                        "Material": comp,
                        "MaterialType": "ROH" if comp.startswith("DEMO-R") else "VERP",
                        "MaterialDescription": f"{comp} component",
                        "ItemCategory": "TAE",
                        "Plant": plant,
                        "ComponentQuantity": round(comp_qty * qty / 100, 2),
                        "ComponentUnit": unit,
                        "ItemValidityStartDate": start - timedelta(days=10),
                        "ItemValidityEndDate": start + timedelta(days=365),
                        "ItemText": f"{comp} - demo component",
                    })

                linkage_rows.append({
                    "LinkID": link_seq,
                    "Material": material,
                    "MaterialType": "FERT",
                    "MaterialDescription": mat_desc,
                    "Plant": plant,
                    "SalesOrder": so_id,
                    "SalesOrderItem": 10,
                    "SoldToPartyName": f"Demo Customer {run_idx + 1}",
                    "SalesOrderQuantity": qty,
                    "SalesOrderUnit": "KG",
                    "SalesOrderDate": start - timedelta(days=10),
                    "RequestedDeliveryDate": start + timedelta(days=5),
                    "SalesOrderStatus": "Completed",
                    "PlannedOrder": planned_id,
                    "PlannedOrderQuantity": qty,
                    "PlannedOrderFinishDate": start,
                    "PlannedOrderStatus": "Converted",
                    "ProductionOrder": order_id,
                    "ProductionOrderQuantity": qty,
                    "ConfirmedYieldQuantity": round(qty * 0.97, 1),
                    "ProductionOrderStatus": "Confirmed",
                    "BasicStartDate": start,
                    "BasicEndDate": start + timedelta(days=2),
                    "ChainStage": "EXECUTED",
                })
                link_seq += 1

    readme = pd.DataFrame({
        "Demo Dataset — Phase 0 Synthetic Fallback": [
            "Generated deterministically (fixed seed) for Phase 0 demo purposes.",
            "Every finished material (DEMO-F00x) is produced 10 times per plant "
            "using 3 distinct component recipes with skewed frequency (~60/30/10%), "
            "so Analyze BOM always returns multiple ranked candidates with a clear "
            "historical-frequency story.",
        ]
    })

    paths = {}

    prod_path = os.path.join(output_dir, "Demo_Production_Order_BOM_Data.xlsx")
    with pd.ExcelWriter(prod_path, engine="openpyxl") as writer:
        pd.DataFrame(prod_headers).to_excel(writer, sheet_name="ProdOrder Header", index=False)
        pd.DataFrame(prod_items).to_excel(writer, sheet_name="ProdOrder Component", index=False)
        readme.to_excel(writer, sheet_name="ReadMe & Legend", index=False)
    paths["production_order"] = prod_path

    plan_path = os.path.join(output_dir, "Demo_Planned_Order_BOM_Data.xlsx")
    with pd.ExcelWriter(plan_path, engine="openpyxl") as writer:
        pd.DataFrame(plan_headers).to_excel(writer, sheet_name="PlannedOrder Header", index=False)
        pd.DataFrame(plan_items).to_excel(writer, sheet_name="PlannedOrder Component", index=False)
        readme.to_excel(writer, sheet_name="ReadMe & Legend", index=False)
    paths["planned_order"] = plan_path

    so_path = os.path.join(output_dir, "Demo_Sales_Order_BOM_Data.xlsx")
    with pd.ExcelWriter(so_path, engine="openpyxl") as writer:
        pd.DataFrame(so_headers).to_excel(writer, sheet_name="SO BOM Header", index=False)
        pd.DataFrame(so_items).to_excel(writer, sheet_name="SO BOM Item", index=False)
        readme.to_excel(writer, sheet_name="ReadMe & Legend", index=False)
    paths["sales_order"] = so_path

    link_path = os.path.join(output_dir, "Demo_SO_PlanOrd_ProdOrd_Linkage.xlsx")
    pd.DataFrame(linkage_rows).to_excel(link_path, index=False)
    paths["linkage"] = link_path

    return paths
