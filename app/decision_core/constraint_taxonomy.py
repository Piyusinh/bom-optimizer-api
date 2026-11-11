"""Constraint Taxonomy — 7-category framework for Phase 2.

RTM Value Driver #4: "Constraint-Aware Optimization — Optimizes simultaneously
across physical, logistical, policy, legal, market, cost & capacity constraints."

Phase 1 implements 4/7 categories (physical, logistical, policy, cost).
Phase 2 roadmap adds legal, market, capacity.

Each constraint category returns (feasible: bool, violations: list[str]).
"""
from datetime import datetime
from typing import Optional


# ─── PHASE 1: IMPLEMENTED ──────────────────────────────────────────────────


class PhysicalConstraint:
    """Component availability, stock levels, material type restrictions."""

    @staticmethod
    def evaluate(
        components: list[dict],
        available_stock: dict[str, float],
        material_types: dict[str, str],
        config: dict,
    ) -> tuple[bool, list[str]]:
        """
        Check physical constraints:
          - Component must exist in master data
          - Stock must be sufficient (with scrap allowance)
          - Material type restrictions (e.g., no external-only materials)
        """
        violations = []

        for comp in components:
            component_id = comp.get("component")
            planned_qty = comp.get("quantity", 0)
            scrap_pct = comp.get("scrap_percent", 0)
            effective_qty = planned_qty * (1.0 + scrap_pct / 100.0)

            # Check stock
            stock = available_stock.get(component_id, 0)
            if config.get("require_stock_coverage") and stock < effective_qty:
                violations.append(
                    f"Component {component_id}: stock {stock} < required {effective_qty}"
                )

            # Check material type (e.g., no external-only for production)
            mat_type = material_types.get(component_id)
            if config.get("exclude_external_only") and mat_type == "F":
                violations.append(
                    f"Component {component_id} is external-only; not allowed"
                )

        return len(violations) == 0, violations


class LogisticalConstraint:
    """Lead time, production line, delivery windows."""

    @staticmethod
    def evaluate(
        components: list[dict],
        lead_times: dict[str, int],
        production_line: Optional[str],
        delivery_date: Optional[datetime],
        config: dict,
    ) -> tuple[bool, list[str]]:
        """
        Check logistical constraints:
          - Lead time must fit within delivery window
          - Production line must be available
          - Component sourcing must be feasible
        """
        violations = []

        if delivery_date and config.get("check_lead_times"):
            max_lead_time = 0
            for comp in components:
                component_id = comp.get("component")
                lead_time = lead_times.get(component_id, 0)
                max_lead_time = max(max_lead_time, lead_time)

            from datetime import timedelta

            required_order_date = delivery_date - timedelta(days=max_lead_time)
            if required_order_date < datetime.utcnow():
                violations.append(
                    f"Max lead time {max_lead_time}d exceeds delivery window"
                )

        if production_line and config.get("check_line_availability"):
            # Placeholder: check if line is available on the planned date
            # (would query production schedule)
            pass

        return len(violations) == 0, violations


class PolicyConstraint:
    """Avoid lists, max components, min quantity per component."""

    @staticmethod
    def evaluate(
        components: list[dict],
        config: dict,
    ) -> tuple[bool, list[str]]:
        """
        Check policy constraints:
          - No components in avoid list
          - Total component count within limits
          - Each component meets minimum quantity
        """
        violations = []

        # Avoid list
        avoid_components = config.get("avoid_components", [])
        for comp in components:
            if comp.get("component") in avoid_components:
                violations.append(
                    f"Component {comp['component']} is in avoid list"
                )

        # Max components
        max_comps = config.get("max_components")
        if max_comps and len(components) > max_comps:
            violations.append(
                f"BOM has {len(components)} components, max {max_comps}"
            )

        # Min quantity per component
        min_qty = config.get("min_quantity_per_component")
        if min_qty:
            for comp in components:
                if comp.get("quantity", 0) < min_qty:
                    violations.append(
                        f"Component {comp['component']} qty {comp['quantity']} < min {min_qty}"
                    )

        return len(violations) == 0, violations


class CostConstraint:
    """Material cost + operational cost limits."""

    @staticmethod
    def evaluate(
        cost_breakdown: dict,
        config: dict,
    ) -> tuple[bool, list[str]]:
        """
        Check cost constraints:
          - Total cost within budget
          - Material cost within limits
          - Operational cost within limits
        """
        violations = []

        total_cost = cost_breakdown.get("total_cost", 0)
        max_cost = config.get("max_total_cost")
        if max_cost and total_cost > max_cost:
            violations.append(
                f"Total cost {total_cost} exceeds max {max_cost}"
            )

        material_cost = cost_breakdown.get("material_cost", 0)
        max_material = config.get("max_material_cost")
        if max_material and material_cost > max_material:
            violations.append(
                f"Material cost {material_cost} exceeds max {max_material}"
            )

        operational_cost = cost_breakdown.get("operational_cost", 0)
        max_operational = config.get("max_operational_cost")
        if max_operational and operational_cost > max_operational:
            violations.append(
                f"Operational cost {operational_cost} exceeds max {max_operational}"
            )

        return len(violations) == 0, violations


# ─── PHASE 2: ROADMAP ──────────────────────────────────────────────────────


class LegalConstraint:
    """Approval flags, compliance holds, regulatory restrictions.
    
    Phase 2 roadmap item.
    """

    @staticmethod
    def evaluate(
        components: list[dict],
        component_metadata: dict[str, dict],
        legal_config: dict,
    ) -> tuple[bool, list[str]]:
        """
        Check legal/regulatory constraints:
          - Component requires approval (e.g., new supplier)
          - Component is restricted in region/market
          - Compliance hold on material
          - Legal approval status
        """
        violations = []

        for comp in components:
            component_id = comp.get("component")
            metadata = component_metadata.get(component_id, {})

            # Check approval requirement
            if metadata.get("requires_approval") and not legal_config.get("approved"):
                violations.append(
                    f"Component {component_id} requires legal approval"
                )

            # Check regional restrictions
            restricted_regions = metadata.get("restricted_regions", [])
            if restricted_regions and legal_config.get("region") in restricted_regions:
                violations.append(
                    f"Component {component_id} restricted in {legal_config['region']}"
                )

            # Check compliance hold
            if metadata.get("compliance_hold"):
                violations.append(
                    f"Component {component_id} has compliance hold"
                )

        return len(violations) == 0, violations


class MarketConstraint:
    """Supplier risk, commodity pricing volatility, market availability.
    
    Phase 2 roadmap item.
    """

    @staticmethod
    def evaluate(
        components: list[dict],
        market_data: dict[str, dict],
        config: dict,
    ) -> tuple[bool, list[str]]:
        """
        Check market constraints:
          - Supplier risk level (HIGH/MEDIUM/LOW)
          - Commodity price volatility (%)
          - Market availability (can we source it?)
          - Geopolitical risk
        """
        violations = []

        max_supplier_risk = config.get("max_supplier_risk", "HIGH")
        risk_levels = ["LOW", "MEDIUM", "HIGH"]
        max_risk_idx = risk_levels.index(max_supplier_risk)

        max_volatility = config.get("max_price_volatility", 100)  # %

        for comp in components:
            component_id = comp.get("component")
            market_info = market_data.get(component_id, {})

            # Check supplier risk
            supplier_risk = market_info.get("supplier_risk", "LOW")
            if risk_levels.index(supplier_risk) > max_risk_idx:
                violations.append(
                    f"Component {component_id} has {supplier_risk} supplier risk"
                )

            # Check price volatility
            volatility = market_info.get("price_volatility_pct", 0)
            if volatility > max_volatility:
                violations.append(
                    f"Component {component_id} volatility {volatility}% > max {max_volatility}%"
                )

            # Check availability
            if not market_info.get("available"):
                violations.append(
                    f"Component {component_id} not available in market"
                )

        return len(violations) == 0, violations


class CapacityConstraint:
    """Machine utilization, throughput limits, production scheduling.
    
    Phase 2 roadmap item.
    """

    @staticmethod
    def evaluate(
        components: list[dict],
        production_line: Optional[str],
        capacity_data: dict[str, dict],
        config: dict,
    ) -> tuple[bool, list[str]]:
        """
        Check capacity constraints:
          - Production line utilization (%)
          - Throughput feasibility (can we make this qty?)
          - Work center availability
          - Scheduling conflicts
        """
        violations = []

        if not production_line:
            return True, []

        line_info = capacity_data.get(production_line, {})

        # Check utilization
        max_utilization = config.get("max_line_utilization", 100)  # %
        utilization = line_info.get("utilization_pct", 0)
        if utilization > max_utilization:
            violations.append(
                f"Line {production_line} utilization {utilization}% > max {max_utilization}%"
            )

        # Check throughput
        required_throughput = sum(c.get("quantity", 0) for c in components)
        max_throughput = line_info.get("max_throughput_per_hour", 0)
        if max_throughput > 0 and required_throughput > max_throughput:
            violations.append(
                f"Required throughput {required_throughput} > line max {max_throughput}"
            )

        # Check work center availability
        work_centers = line_info.get("work_centers", [])
        if not work_centers:
            violations.append(
                f"Line {production_line} has no available work centers"
            )

        return len(violations) == 0, violations


# ─── UNIFIED EVALUATOR ─────────────────────────────────────────────────────


class ConstraintTaxonomy:
    """Unified constraint evaluator — Phase 1 + Phase 2."""

    def __init__(self):
        self.categories = {
            "physical": PhysicalConstraint,
            "logistical": LogisticalConstraint,
            "policy": PolicyConstraint,
            "cost": CostConstraint,
            "legal": LegalConstraint,
            "market": MarketConstraint,
            "capacity": CapacityConstraint,
        }

    async def evaluate_all(
        self,
        components: list[dict],
        context: dict,
    ) -> dict:
        """
        Evaluate all enabled constraints.

        Returns:
          {
            "feasible": bool,
            "violations_by_category": {
              "physical": [...],
              "legal": [...],
              ...
            },
            "summary": "X violations across Y categories"
          }
        """
        violations_by_category = {}
        all_violations = []

        # Phase 1 constraints (always enabled)
        for category in ["physical", "logistical", "policy", "cost"]:
            constraint_class = self.categories[category]
            feasible, violations = constraint_class.evaluate(
                components, context.get(f"{category}_config", {})
            )
            if violations:
                violations_by_category[category] = violations
                all_violations.extend(violations)

        # Phase 2 constraints (optional, if enabled in config)
        phase2_enabled = context.get("enable_phase2_constraints", False)
        if phase2_enabled:
            for category in ["legal", "market", "capacity"]:
                constraint_class = self.categories[category]
                feasible, violations = constraint_class.evaluate(
                    components, context.get(f"{category}_config", {})
                )
                if violations:
                    violations_by_category[category] = violations
                    all_violations.extend(violations)

        return {
            "feasible": len(all_violations) == 0,
            "violations_by_category": violations_by_category,
            "total_violations": len(all_violations),
            "summary": (
                "No violations"
                if len(all_violations) == 0
                else f"{len(all_violations)} violations across {len(violations_by_category)} categories"
            ),
        }
