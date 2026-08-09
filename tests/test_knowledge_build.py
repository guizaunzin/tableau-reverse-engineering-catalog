from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from knowledge_build import (
    find_entities,
    impact_analysis,
    load_knowledge,
    trace_dependencies,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "knowledge_build.py"


def workbook_markdown(root: Path, workbook_slug: str = "sales") -> Path:
    return root / "markdown" / "workbooks" / workbook_slug


def source_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "source_type": "tableau",
        "sources": [
            {
                "id": "tableau-workbook:sales",
                "type": "tableau_workbook",
                "name": "Sales",
                "file": "sales.twb",
            }
        ],
        "entities": [
            {
                "id": "metric:sales:revenue",
                "type": "metric",
                "name": "Revenue",
                "provenance": {
                    "source_id": "tableau-workbook:sales",
                    "tableau_object_type": "column",
                    "tableau_name": "[Revenue]",
                },
                "attributes": {"semantic_status": "inferred"},
            },
            {
                "id": "visual:sales:overview",
                "type": "visual",
                "name": "Overview",
                "provenance": {
                    "source_id": "tableau-workbook:sales",
                    "tableau_object_type": "worksheet",
                    "tableau_name": "Overview",
                },
                "attributes": {},
            },
        ],
        "relations": [
            {
                "from": "visual:sales:overview",
                "type": "displays",
                "to": "metric:sales:revenue",
                "evidence": {"source": "tableau", "direct": True},
            }
        ],
        "warnings": [],
    }


class KnowledgeBuildCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "knowledge"
        (self.root / "sources").mkdir(parents=True)
        (self.root / "sources" / "tableau.json").write_text(
            json.dumps(source_payload()), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_builder(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), *extra],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_combines_manual_yaml_with_automatic_metadata(self) -> None:
        manual = self.root / "manual" / "metrics" / "revenue.md"
        manual.parent.mkdir(parents=True)
        manual.write_text(
            """---
id: metric:sales:revenue
type: metric
description: Recognized revenue.
owner: Finance Analytics
business_rule_ids:
  - business-rule:recognized-revenue
relations:
  - type: affected_by
    to: business-rule:recognized-revenue
---

# Revenue

Use this metric for approved reporting.
""",
            encoding="utf-8",
        )
        business_rule = (
            self.root
            / "manual"
            / "business-rules"
            / "recognized-revenue.md"
        )
        business_rule.parent.mkdir(parents=True)
        business_rule.write_text(
            """---
id: business-rule:recognized-revenue
type: business_rule
description: Revenue is recognized after approval.
---

# Recognized Revenue
""",
            encoding="utf-8",
        )
        before = hashlib.sha256(manual.read_bytes()).hexdigest()

        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, hashlib.sha256(manual.read_bytes()).hexdigest())
        page = (
            workbook_markdown(self.root)
            / "metrics"
            / "metric-sales-revenue.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Automatic metadata", page)
        self.assertIn("source_id: tableau-workbook:sales", page)
        self.assertIn("semantic_status: inferred", page)
        self.assertIn("## Human context", page)
        self.assertIn("Recognized revenue.", page)
        self.assertIn("Use this metric for approved reporting.", page)
        self.assertIn("## Where used", page)
        self.assertIn("Overview", page)
        self.assertTrue(
            (
                workbook_markdown(self.root)
                / "business-rules"
                / "business-rule-recognized-revenue.md"
            ).exists()
        )

    def test_renders_a_main_index_with_dashboards_first(self) -> None:
        payload = source_payload()
        payload["entities"].extend(
            [
                {
                "id": "dashboard:sales:executive",
                "type": "dashboard",
                "name": "Executive Dashboard",
                "provenance": {
                    "source_id": "tableau-workbook:sales",
                    "tableau_object_type": "dashboard",
                    "tableau_name": "Executive Dashboard",
                },
                "attributes": {},
                },
                {
                    "id": "datasource:sales:orders",
                    "type": "datasource",
                    "name": "Orders",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {},
                },
                {
                    "id": "business-rule:sales:revenue-inferred",
                    "type": "business_rule",
                    "name": "Revenue rule",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {"semantic_status": "inferred"},
                },
                {
                    "id": "calculation:sales:revenue",
                    "type": "calculation",
                    "name": "Revenue calculation",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {},
                },
                {
                    "id": "filter:sales:region",
                    "type": "filter",
                    "name": "Region",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {},
                },
                {
                    "id": "field:sales:revenue",
                    "type": "field",
                    "name": "Revenue field",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {},
                },
            ]
        )
        (self.root / "sources" / "tableau.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        global_index = (self.root / "markdown" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(global_index.startswith("# Semantic Knowledge Base\n"))
        self.assertIn("## Workbooks", global_index)
        self.assertIn("[Sales](workbooks/sales/README.md)", global_index)
        index = (workbook_markdown(self.root) / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(index.startswith("# Sales\n"))
        self.assertIn("## Contents", index)
        self.assertIn("- [Dashboards](#dashboards)", index)
        self.assertIn("## Dashboards", index)
        self.assertIn(
            '<a href="dashboards/dashboard-sales-executive.md"><strong>'
            "Executive Dashboard</strong></a>",
            index,
        )
        self.assertIn("<table>", index)
        self.assertIn("## Metrics", index)
        self.assertIn(
            'href="metrics/metric-sales-revenue.md"',
            index,
        )
        self.assertIn('data-entity-type="metric"', index)
        self.assertIn("background-color: #D1FAE5", index)
        self.assertNotIn("## Business Rules", index)
        expected_order = [
            "## Dashboards",
            "## Data Sources",
            "## Inferred Rules",
            "## Visuals",
            "## Metrics",
            "## Calculations",
            "## Filters",
            "## Fields",
        ]
        self.assertEqual(
            sorted(expected_order, key=index.index),
            expected_order,
        )

    def test_renders_each_workbook_in_an_independent_markdown_tree(self) -> None:
        inventory = {
            "schema_version": 2,
            "source_type": "tableau",
            "sources": [
                {
                    "id": "tableau-workbook:inventory",
                    "type": "tableau_workbook",
                    "name": "Inventory",
                    "file": "inventory.twb",
                }
            ],
            "entities": [
                {
                    "id": "metric:inventory:units",
                    "type": "metric",
                    "name": "Units",
                    "provenance": {"source_id": "tableau-workbook:inventory"},
                    "attributes": {},
                }
            ],
            "relations": [],
            "warnings": [],
        }
        tableau_dir = self.root / "sources" / "tableau"
        tableau_dir.mkdir()
        (tableau_dir / "inventory.json").write_text(
            json.dumps(inventory), encoding="utf-8"
        )

        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        global_index = (self.root / "markdown" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("[Inventory](workbooks/inventory/README.md)", global_index)
        self.assertIn("[Sales](workbooks/sales/README.md)", global_index)
        sales_index = (workbook_markdown(self.root) / "README.md").read_text(
            encoding="utf-8"
        )
        inventory_index = (
            workbook_markdown(self.root, "inventory") / "README.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Revenue", sales_index)
        self.assertNotIn("Units", sales_index)
        self.assertIn("Units", inventory_index)
        self.assertNotIn("Revenue", inventory_index)
        self.assertTrue(
            (
                workbook_markdown(self.root, "inventory")
                / "metrics"
                / "metric-inventory-units.md"
            ).exists()
        )
        self.assertFalse((self.root / "markdown" / "metrics").exists())

    def test_rebuild_keeps_live_markdown_directories_available(self) -> None:
        payload = source_payload()
        payload["entities"].append(
            {
                "id": "business-rule:sales:revenue-inferred",
                "type": "business_rule",
                "name": "Revenue rule",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"semantic_status": "inferred"},
            }
        )
        (self.root / "sources" / "tableau.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        first = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stderr)
        rules_directory = workbook_markdown(self.root) / "business-rules"
        stale = self.root / "markdown" / "stale.md"
        stale.write_text("stale", encoding="utf-8")
        directory_handle = os.open(rules_directory, os.O_RDONLY)
        try:
            original_inode = os.fstat(directory_handle).st_ino

            second = self.run_builder()

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(os.fstat(directory_handle).st_ino, original_inode)
            self.assertEqual(rules_directory.stat().st_ino, original_inode)
            self.assertFalse(stale.exists())
        finally:
            os.close(directory_handle)

    def test_renders_dashboard_summary_and_visual_data_dictionary(self) -> None:
        payload = source_payload()
        payload["entities"][0]["name"] = "Revenue | YTD"
        payload["entities"].extend(
            [
                {
                    "id": "dashboard:sales:executive",
                    "type": "dashboard",
                    "name": "Executive Dashboard",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {},
                },
                {
                    "id": "field:sales:revenue",
                    "type": "field",
                    "name": "Revenue | YTD",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {
                        "field_type": "Base field",
                        "role": "measure",
                    },
                },
                {
                    "id": "field:sales:period",
                    "type": "field",
                    "name": "Period",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {"field_type": "Parameter"},
                },
                {
                    "id": "calculation:sales:revenue",
                    "type": "calculation",
                    "name": "Revenue | YTD",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {
                        "classification": "Aggregate calculation",
                        "formula_tableau": "IF [Revenue] > 0\nTHEN [Revenue]\nEND",
                    },
                },
                {
                    "id": "calculation:sales:indirect-parameter-switch",
                    "type": "calculation",
                    "name": "Indirect parameter switch",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {
                        "classification": "Parameter-driven calculation",
                        "formula_tableau": "[Period] = 'Current'",
                    },
                },
                {
                    "id": "filter:sales:region",
                    "type": "filter",
                    "name": "Region",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {"operator": "member", "value": "West"},
                },
                {
                    "id": "datasource:sales:orders",
                    "type": "datasource",
                    "name": "Orders",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {},
                },
                {
                    "id": "datasource:sales:parameters",
                    "type": "datasource",
                    "name": "Parameters",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {},
                },
                {
                    "id": "business-rule:sales:revenue-ytd-inferred",
                    "type": "business_rule",
                    "name": "Revenue | YTD rule",
                    "provenance": {"source_id": "tableau-workbook:sales"},
                    "attributes": {
                        "semantic_status": "inferred",
                        "confidence": "high",
                        "rule_kind": "time_window",
                        "statement": "Revenue YTD includes Revenue only in the selected year.",
                        "field_mentions": ["Revenue YTD", "Revenue"],
                        "inference_method": "deterministic_tableau_formula_v1",
                        "formula_tableau": "IF YEAR([Date]) = [Year] THEN [Revenue] END",
                        "evidence_calculation_ids": [
                            "calculation:sales:revenue"
                        ],
                    },
                },
            ]
        )
        payload["relations"].extend(
            [
                {
                    "from": "dashboard:sales:executive",
                    "type": "contains",
                    "to": "visual:sales:overview",
                },
                {
                    "from": "visual:sales:overview",
                    "type": "uses",
                    "to": "field:sales:revenue",
                },
                {
                    "from": "visual:sales:overview",
                    "type": "uses",
                    "to": "field:sales:period",
                },
                {
                    "from": "visual:sales:overview",
                    "type": "uses",
                    "to": "calculation:sales:revenue",
                },
                {
                    "from": "visual:sales:overview",
                    "type": "affected_by",
                    "to": "filter:sales:region",
                },
                {
                    "from": "filter:sales:region",
                    "type": "filters_on",
                    "to": "field:sales:revenue",
                },
                {
                    "from": "field:sales:revenue",
                    "type": "comes_from",
                    "to": "datasource:sales:orders",
                },
                {
                    "from": "field:sales:period",
                    "type": "comes_from",
                    "to": "datasource:sales:parameters",
                },
                {
                    "from": "calculation:sales:revenue",
                    "type": "depends_on",
                    "to": "field:sales:revenue",
                },
                {
                    "from": "calculation:sales:revenue",
                    "type": "depends_on",
                    "to": "calculation:sales:indirect-parameter-switch",
                },
                {
                    "from": "metric:sales:revenue",
                    "type": "affected_by",
                    "to": "business-rule:sales:revenue-ytd-inferred",
                },
                {
                    "from": "business-rule:sales:revenue-ytd-inferred",
                    "type": "implemented_by",
                    "to": "calculation:sales:revenue",
                },
            ]
        )
        (self.root / "sources" / "tableau.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        manual_dashboard = self.root / "manual" / "dashboards" / "executive.md"
        manual_dashboard.parent.mkdir(parents=True)
        manual_dashboard.write_text(
            """---
id: dashboard:sales:executive
type: dashboard
description: Sales health and regional performance.
owner: Finance Analytics
status: Production
---
""",
            encoding="utf-8",
        )

        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        dashboard = (
            workbook_markdown(self.root)
            / "dashboards"
            / "dashboard-sales-executive.md"
        ).read_text(encoding="utf-8")
        self.assertIn('<a id="top"></a>', dashboard)
        self.assertIn("[← Back to Sales](../README.md)", dashboard)
        self.assertLess(
            dashboard.index("Sales health and regional performance."),
            dashboard.index("## Summary"),
        )
        self.assertIn("Owner: Finance Analytics", dashboard)
        self.assertIn("Status: Production", dashboard)
        self.assertIn("## Contents", dashboard)
        self.assertIn("- [Summary](#summary)", dashboard)
        self.assertLess(dashboard.index("## Contents"), dashboard.index("## Summary"))
        self.assertIn("## Summary", dashboard)
        self.assertNotIn("| Entity | Count |", dashboard)
        for summary_type, label, count in (
            ("datasource", "DATA SOURCES", 2),
            ("visual", "VISUALS", 1),
            ("field", "FIELDS", 2),
            ("calculation", "CALCULATIONS", 2),
            ("filter", "FILTERS", 1),
            ("parameter", "PARAMETERS", 1),
            ("metric", "METRICS", 1),
            ("inferred-rule", "INFERRED RULES", 1),
        ):
            self.assertIn(f'data-summary-entity="{summary_type}"', dashboard)
            self.assertIn(f">{count}</strong>", dashboard)
            self.assertIn(f">{label}</span>", dashboard)
        self.assertEqual(dashboard.count("<tr>"), 1)
        self.assertNotIn("| Business Rules |", dashboard)
        self.assertIn("## Inferred Rules", dashboard)
        self.assertIn(
            "[Revenue \\| YTD rule](../business-rules/"
            "business-rule-sales-revenue-ytd-inferred.md)",
            dashboard,
        )
        self.assertIn("<u><span", dashboard)
        self.assertIn('data-entity-type="field"', dashboard)
        self.assertIn("### 1. [Overview](../visuals/visual-sales-overview.md)", dashboard)
        self.assertNotIn("Page:", dashboard)
        self.assertIn("**Metrics:**", dashboard)
        self.assertIn("**Calculations:**", dashboard)
        self.assertIn("**Filters:**", dashboard)
        self.assertIn("**Fields:**", dashboard)
        for entity_type, color in (
            ("metric", "#D1FAE5"),
            ("calculation", "#F3E8FF"),
            ("filter", "#DBEAFE"),
            ("field", "#E2E8F0"),
        ):
            self.assertIn(f'data-entity-type="{entity_type}"', dashboard)
            self.assertIn(f"background-color: {color}", dashboard)

        visual = (
            workbook_markdown(self.root) / "visuals" / "visual-sales-overview.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Dashboards", visual)
        self.assertIn("## Contents", visual)
        self.assertIn("- [Dashboards](#dashboards)", visual)
        self.assertIn("Executive Dashboard", visual)
        self.assertIn("## Metrics", visual)
        self.assertIn('href="../metrics/metric-sales-revenue.md"', visual)
        self.assertIn('data-entity-type="metric"', visual)
        self.assertNotIn("| Metric | Definition |", visual)
        self.assertIn("## Fields", visual)
        self.assertIn('href="../fields/field-sales-revenue.md"', visual)
        self.assertIn('data-entity-type="field"', visual)
        self.assertIn("## Calculations", visual)
        self.assertIn('href="../calculations/calculation-sales-revenue.md"', visual)
        self.assertIn('data-entity-type="calculation"', visual)
        self.assertNotIn("Indirect parameter switch", visual)
        self.assertNotIn("THEN [Revenue]", visual)
        self.assertIn("## Filters", visual)
        self.assertIn('href="../filters/filter-sales-region.md"', visual)
        self.assertIn('data-entity-type="filter"', visual)
        self.assertNotIn("## Parameters", visual)
        self.assertIn("## Data Sources", visual)
        self.assertIn("Orders", visual)
        self.assertNotIn(
            "[Parameters](../datasources/datasource-sales-parameters.md)", visual
        )

        calculation = (
            workbook_markdown(self.root)
            / "calculations"
            / "calculation-sales-revenue.md"
        ).read_text(encoding="utf-8")
        self.assertIn('<a id="top"></a>', calculation)
        self.assertIn("[← Back to Sales](../README.md)", calculation)
        self.assertIn(
            'depends_on → <a href="../fields/field-sales-revenue.md#top"',
            calculation,
        )
        self.assertIn("## Formula", calculation)
        self.assertIn(
            "```text\nIF [Revenue] > 0\nTHEN [Revenue]\nEND\n```",
            calculation,
        )
        rule = (
            workbook_markdown(self.root)
            / "business-rules"
            / "business-rule-sales-revenue-ytd-inferred.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Inferred rule", rule)
        self.assertIn("Automatically inferred", rule)
        self.assertIn("Confidence: high", rule)
        self.assertIn("Rule kind: time window", rule)
        self.assertIn("<u><span", rule)
        self.assertIn('data-entity-type="field"', rule)
        self.assertIn("## Evidence", rule)
        self.assertIn('href="../calculations/calculation-sales-revenue.md"', rule)
        self.assertIn('data-entity-type="calculation"', rule)
        self.assertIn(
            "```text\nIF YEAR([Date]) = [Year] THEN [Revenue] END\n```", rule
        )
        for relative, entity_type in (
            ("metrics/metric-sales-revenue.md", "metric"),
            ("calculations/calculation-sales-revenue.md", "calculation"),
            ("filters/filter-sales-region.md", "filter"),
            ("fields/field-sales-revenue.md", "field"),
        ):
            entity_page = (workbook_markdown(self.root) / relative).read_text(
                encoding="utf-8"
            )
            self.assertTrue(
                entity_page.startswith(
                    f'# <span data-entity-type="{entity_type}"'
                ),
                relative,
            )

    def test_renders_deterministic_dashboard_layout_svg_with_matching_numbers(self) -> None:
        payload = source_payload()
        payload["entities"].append(
            {
                "id": "dashboard:sales:executive",
                "type": "dashboard",
                "name": "Executive & Overview",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "layout": {
                        "status": "complete",
                        "sizing_mode": "fixed",
                        "dashboard_width": 1200,
                        "dashboard_height": 800,
                        "coordinate_space": {"width": 100000, "height": 100000},
                        "items": [
                            {
                                "document_order": 1,
                                "tableau_zone_id": "3",
                                "kind": "visual",
                                "visual_id": "visual:sales:overview",
                                "x": 10000,
                                "y": 20000,
                                "width": 50000,
                                "height": 40000,
                                "hidden": False,
                            },
                            {
                                "document_order": 2,
                                "tableau_zone_id": "4",
                                "kind": "text",
                                "label": "Revenue < summary",
                                "x": 65000,
                                "y": 5000,
                                "width": 30000,
                                "height": 10000,
                                "hidden": False,
                            },
                        ],
                        "warnings": [],
                    }
                },
            }
        )
        payload["entities"].append(
            {
                "id": "business-rule:sales:revenue-inferred",
                "type": "business_rule",
                "name": "Revenue rule",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "semantic_status": "inferred",
                    "statement": "Revenue follows the selected scope.",
                },
            }
        )
        payload["relations"].extend(
            [
                {
                "from": "dashboard:sales:executive",
                "type": "contains",
                "to": "visual:sales:overview",
                },
                {
                    "from": "metric:sales:revenue",
                    "type": "affected_by",
                    "to": "business-rule:sales:revenue-inferred",
                },
            ]
        )
        (self.root / "sources" / "tableau.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        first_result = self.run_builder()

        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        dashboard_path = (
            workbook_markdown(self.root)
            / "dashboards"
            / "dashboard-sales-executive.md"
        )
        dashboard = dashboard_path.read_text(encoding="utf-8")
        self.assertIn("## Dashboard layout", dashboard)
        self.assertIn("## Inferred Rules", dashboard)
        self.assertLess(
            dashboard.index("## Dashboard layout"),
            dashboard.index("## Inferred Rules"),
        )
        self.assertIn(
            "![Executive & Overview layout](../assets/layouts/dashboard-sales-executive.svg)",
            dashboard,
        )
        self.assertIn(
            "### 1. [Overview](../visuals/visual-sales-overview.md)", dashboard
        )
        svg_path = (
            workbook_markdown(self.root)
            / "assets"
            / "layouts"
            / "dashboard-sales-executive.svg"
        )
        svg = svg_path.read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 1200 800"', svg)
        self.assertIn('x="120" y="160" width="600" height="320"', svg)
        self.assertIn("1. Overview", svg)
        self.assertIn("Revenue &lt; summary", svg)
        self.assertIn("Executive &amp; Overview layout", svg)
        first_hash = hashlib.sha256(svg_path.read_bytes()).hexdigest()

        second_result = self.run_builder()

        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        self.assertEqual(first_hash, hashlib.sha256(svg_path.read_bytes()).hexdigest())

    def test_rejects_manual_overlay_for_unknown_non_business_entity(self) -> None:
        manual = self.root / "manual" / "metrics" / "missing.md"
        manual.parent.mkdir(parents=True)
        manual.write_text(
            """---
id: metric:sales:missing
type: metric
description: Missing metric.
---
""",
            encoding="utf-8",
        )

        result = self.run_builder("--check")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown automatic entity", result.stderr)

    def test_rejects_broken_manual_relation(self) -> None:
        manual = self.root / "manual" / "metrics" / "revenue.md"
        manual.parent.mkdir(parents=True)
        manual.write_text(
            """---
id: metric:sales:revenue
type: metric
relations:
  - type: affected_by
    to: business-rule:missing
---
""",
            encoding="utf-8",
        )

        result = self.run_builder("--check")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown relation target", result.stderr)

    def test_rejects_unknown_business_rule_id_in_manual_metadata(self) -> None:
        manual = self.root / "manual" / "metrics" / "revenue.md"
        manual.parent.mkdir(parents=True)
        manual.write_text(
            """---
id: metric:sales:revenue
type: metric
business_rule_ids:
  - business-rule:missing
---
""",
            encoding="utf-8",
        )

        result = self.run_builder("--check")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown business_rule_ids reference", result.stderr)

    def test_rejects_duplicate_empty_manual_pages(self) -> None:
        first = self.root / "manual" / "metrics" / "revenue.md"
        second = self.root / "manual" / "metrics" / "revenue-duplicate.md"
        first.parent.mkdir(parents=True)
        content = """---
id: metric:sales:revenue
type: metric
---
"""
        first.write_text(content, encoding="utf-8")
        second.write_text(content, encoding="utf-8")

        result = self.run_builder("--check")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate manual page", result.stderr)

    def test_manual_duplicate_relation_does_not_replace_tableau_evidence(self) -> None:
        manual = self.root / "manual" / "visuals" / "overview.md"
        manual.parent.mkdir(parents=True)
        manual.write_text(
            """---
id: visual:sales:overview
type: visual
relations:
  - type: displays
    to: metric:sales:revenue
---
""",
            encoding="utf-8",
        )

        model = load_knowledge(self.root)

        relation = next(
            item
            for item in model["relations"]
            if item["from"] == "visual:sales:overview"
            and item["type"] == "displays"
        )
        self.assertEqual(relation["evidence"]["source"], "tableau")

    def test_reconciles_published_fields_across_per_workbook_sources(self) -> None:
        def document(workbook: str) -> dict[str, object]:
            source_id = f"tableau-workbook:{workbook}"
            datasource_id = f"datasource:{workbook}:published-sales"
            field_id = f"field:{workbook}:sales"
            return {
                "schema_version": 2,
                "source_type": "tableau",
                "sources": [
                    {
                        "id": source_id,
                        "type": "tableau_workbook",
                        "name": workbook.title(),
                        "file": f"{workbook}.twb",
                    }
                ],
                "entities": [
                    {
                        "id": datasource_id,
                        "type": "datasource",
                        "name": "Published Sales",
                        "provenance": {"source_id": source_id},
                        "attributes": {
                            "internal_name": f"source.{workbook}",
                            "published_identity": "path:/datasources/sales",
                        },
                    },
                    {
                        "id": field_id,
                        "type": "field",
                        "name": "Sales",
                        "provenance": {"source_id": source_id},
                        "attributes": {"internal_name": "[Sales]"},
                    },
                ],
                "relations": [
                    {
                        "from": field_id,
                        "type": "comes_from",
                        "to": datasource_id,
                        "evidence": {"source": "tableau", "direct": True},
                    }
                ],
                "warnings": [],
            }

        tableau_dir = self.root / "sources" / "tableau"
        tableau_dir.mkdir()
        (tableau_dir / "a.json").write_text(
            json.dumps(document("a")), encoding="utf-8"
        )
        (tableau_dir / "b.json").write_text(
            json.dumps(document("b")), encoding="utf-8"
        )

        model = load_knowledge(self.root)

        equivalences = [
            relation
            for relation in model["relations"]
            if relation["type"] == "same_source_field_as"
        ]
        self.assertEqual(len(equivalences), 1)
        self.assertEqual(equivalences[0]["from"], "field:a:sales")
        self.assertEqual(equivalences[0]["to"], "field:b:sales")

    def test_impact_analysis_walks_from_dependency_to_visual_and_dashboard(self) -> None:
        model = {
            "entities": [],
            "relations": [
                {"from": "calculation:profit", "type": "depends_on", "to": "field:sales"},
                {"from": "metric:profit", "type": "calculated_by", "to": "calculation:profit"},
                {
                    "from": "business-rule:profit",
                    "type": "implemented_by",
                    "to": "calculation:profit",
                },
                {
                    "from": "metric:profit",
                    "type": "affected_by",
                    "to": "business-rule:profit",
                },
                {"from": "visual:overview", "type": "displays", "to": "metric:profit"},
                {"from": "dashboard:executive", "type": "contains", "to": "visual:overview"},
            ],
        }

        impacted = impact_analysis(model, "field:sales")

        self.assertEqual(
            impacted,
            [
                "calculation:profit",
                "business-rule:profit",
                "metric:profit",
                "visual:overview",
                "dashboard:executive",
            ],
        )
        self.assertEqual(
            trace_dependencies(model, "business-rule:profit"),
            ["calculation:profit", "field:sales"],
        )

    def test_find_entities_searches_name_id_and_manual_context(self) -> None:
        model = {
            "entities": [
                {
                    "id": "metric:sales:revenue",
                    "type": "metric",
                    "name": "Revenue",
                    "manual": {"description": "Recognized turnover"},
                    "manual_body": "Approved finance metric.",
                },
                {
                    "id": "field:sales:region",
                    "type": "field",
                    "name": "Region",
                    "manual": {},
                    "manual_body": "",
                },
            ]
        }

        by_context = find_entities(model, "turnover", entity_type="metric")
        by_id = find_entities(model, "sales:region")

        self.assertEqual([item["id"] for item in by_context], ["metric:sales:revenue"])
        self.assertEqual([item["id"] for item in by_id], ["field:sales:region"])


if __name__ == "__main__":
    unittest.main()
