"""Applies db/schema.sql twice (idempotency test) and prints table / seed counts."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import apply_schema, query  # noqa: E402

apply_schema()
apply_schema()
print("base tables:", query("select count(*) n from information_schema.tables where table_schema='public' and table_type='BASE TABLE'")[0]["n"])
print("views:", [r["table_name"] for r in query("select table_name from information_schema.views where table_schema='public'")])
for t in ["a_rule", "a_rule_set", "a_criterion", "a_season_map", "m_bom", "stg_order_base"]:
    print(t, query(f"select count(*) n from {t}")[0]["n"])
print(query("select rule_set_id, name, version, jsonb_array_length(rule_ids) n from a_rule_set"))
