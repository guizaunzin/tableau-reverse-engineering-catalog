from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import tomli_w
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "mcp_rules_generator.py"


def write_knowledge(root: Path) -> None:
    payload = {
        "schema_version": 2,
        "source_type": "tableau",
        "sources": [
            {
                "id": "tableau-workbook:sales",
                "type": "tableau_workbook",
                "name": "Sales & Forecast",
                "file": "sales.twb",
            }
        ],
        "entities": [
            {
                "id": "dashboard:sales:executive",
                "type": "dashboard",
                "name": "Executive Overview",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
            {
                "id": "dashboard:sales:regional",
                "type": "dashboard",
                "name": "Regional Overview",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
            {
                "id": "visual:sales:shared-kpi",
                "type": "visual",
                "name": "Shared KPI",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
            {
                "id": "visual:sales:regional-map",
                "type": "visual",
                "name": "Regional Map",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
            {
                "id": "metric:sales:revenue",
                "type": "metric",
                "name": "Revenue",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "semantic_status": "inferred",
                    "calculation_scope": "aggregate",
                },
            },
            {
                "id": "calculation:sales:eligible-revenue",
                "type": "calculation",
                "name": "Eligible Revenue",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "formula_display": "SUM([Revenue])",
                    "classification": "Aggregate calculation",
                },
            },
            {
                "id": "field:sales:region",
                "type": "field",
                "name": "Região \"Norte\"",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "role": "dimension",
                    "datatype": "string",
                    "field_type": "Base field",
                },
            },
            {
                "id": "field:sales:show-region",
                "type": "field",
                "name": "Show Region Helper",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "role": "dimension",
                    "datatype": "boolean",
                    "field_type": "Calculated",
                },
            },
            {
                "id": "datasource:sales:orders",
                "type": "datasource",
                "name": "Orders",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"internal_name": "orders"},
            },
            {
                "id": "business-rule:sales:revenue-positive",
                "type": "business_rule",
                "name": "Positive Revenue",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "semantic_status": "inferred",
                    "statement": "Revenue includes only eligible orders.",
                    "confidence": "high",
                    "rule_kind": "inclusion_exclusion",
                    "inference_method": "deterministic_tableau_formula_v1",
                    "evidence_calculation_ids": [
                        "calculation:sales:eligible-revenue"
                    ],
                },
            },
        ],
        "relations": [
            {"from": "dashboard:sales:executive", "type": "contains", "to": "visual:sales:shared-kpi"},
            {"from": "dashboard:sales:regional", "type": "contains", "to": "visual:sales:shared-kpi"},
            {"from": "dashboard:sales:regional", "type": "contains", "to": "visual:sales:regional-map"},
            {"from": "visual:sales:shared-kpi", "type": "displays", "to": "metric:sales:revenue"},
            {"from": "metric:sales:revenue", "type": "calculated_by", "to": "calculation:sales:eligible-revenue"},
            {"from": "metric:sales:revenue", "type": "affected_by", "to": "business-rule:sales:revenue-positive"},
            {"from": "visual:sales:regional-map", "type": "uses", "to": "field:sales:region"},
            {"from": "visual:sales:regional-map", "type": "uses", "to": "field:sales:show-region"},
            {"from": "field:sales:region", "type": "comes_from", "to": "datasource:sales:orders"},
        ],
        "warnings": [],
    }
    source = root / "sources" / "tableau" / "sales.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(payload), encoding="utf-8")


class McpRulesGeneratorCliTests(unittest.TestCase):
    def run_generator(
        self, root: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(root), *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_generates_workbook_and_isolated_dashboard_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            workbook_path = root / "mcp_rules" / "workbooks" / "sales.toml"
            executive_path = (
                root
                / "mcp_rules"
                / "dashboards"
                / "sales"
                / "executive.toml"
            )
            regional_path = executive_path.with_name("regional.toml")
            self.assertTrue(workbook_path.exists())
            self.assertTrue(executive_path.exists())
            self.assertTrue(regional_path.exists())

            executive = tomllib.loads(executive_path.read_text(encoding="utf-8"))
            regional = tomllib.loads(regional_path.read_text(encoding="utf-8"))
            workbook = tomllib.loads(workbook_path.read_text(encoding="utf-8"))
            self.assertEqual(executive["mcp_rules_version"], 1)
            self.assertEqual(executive["scope"]["id"], "dashboard:sales:executive")
            self.assertEqual(
                [item["id"] for item in executive["metrics"]],
                ["metric:sales:revenue"],
            )
            self.assertEqual(
                executive["metrics"][0]["calculation_ids"],
                ["calculation:sales:eligible-revenue"],
            )
            self.assertEqual(
                executive["metrics"][0]["formulas"], ["SUM([Revenue])"]
            )
            self.assertEqual(executive["dimensions"], [])
            self.assertEqual(
                [item["id"] for item in regional["dimensions"]],
                ["field:sales:region"],
            )
            self.assertEqual(len(workbook["metrics"]), 1)
            self.assertEqual(len(workbook["dimensions"]), 1)

    def test_emits_inferred_and_manual_rules_with_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            dashboard_manual = root / "manual" / "dashboards" / "executive.md"
            dashboard_manual.parent.mkdir(parents=True)
            dashboard_manual.write_text(
                """---
id: dashboard:sales:executive
type: dashboard
business_rule_ids:
  - business-rule:approved-revenue
---

# Executive Overview
""",
                encoding="utf-8",
            )
            rule_manual = root / "manual" / "business-rules" / "approved-revenue.md"
            rule_manual.parent.mkdir(parents=True)
            rule_manual.write_text(
                """---
id: business-rule:approved-revenue
type: business_rule
name: Approved Revenue
description: Revenue is reported after finance approval.
---

Use the approved posting date.
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = tomllib.loads(
                (
                    root
                    / "mcp_rules"
                    / "dashboards"
                    / "sales"
                    / "executive.toml"
                ).read_text(encoding="utf-8")
            )
            rules = {item["id"]: item for item in generated["rules"]}
            self.assertEqual(
                set(rules),
                {
                    "business-rule:sales:revenue-positive",
                    "business-rule:approved-revenue",
                },
            )
            inferred = rules["business-rule:sales:revenue-positive"]
            self.assertTrue(inferred["enabled"])
            self.assertEqual(inferred["source"], "inferred")
            self.assertEqual(inferred["confidence"], "high")
            self.assertEqual(
                inferred["evidence_ids"],
                ["calculation:sales:eligible-revenue"],
            )
            manual = rules["business-rule:approved-revenue"]
            self.assertTrue(manual["enabled"])
            self.assertEqual(manual["source"], "manual")
            self.assertEqual(
                manual["statement"],
                "Revenue is reported after finance approval.",
            )

    def test_preserves_curation_and_applies_overrides_on_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            first = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            path = root / "mcp_rules" / "workbooks" / "sales.toml"
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            payload["curation"] = {
                "description": "Finance-reviewed context.",
                "additional_instructions": ["Prefer the approved fiscal calendar."],
                "disabled_rule_ids": ["business-rule:sales:revenue-positive"],
                "overrides": [
                    {
                        "id": "business-rule:sales:revenue-positive",
                        "statement": "Use only finance-eligible orders.",
                    }
                ],
                "rules": [
                    {
                        "id": "curated-rule:sales:fiscal-calendar",
                        "name": "Fiscal calendar",
                        "statement": "Use the approved fiscal calendar.",
                        "enabled": True,
                    }
                ],
            }
            path.write_text(tomli_w.dumps(payload), encoding="utf-8")

            second = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(second.returncode, 0, second.stderr)
            regenerated_text = path.read_text(encoding="utf-8")
            regenerated = tomllib.loads(regenerated_text)
            self.assertEqual(regenerated["curation"], payload["curation"])
            rules = {item["id"]: item for item in regenerated["rules"]}
            inferred = rules["business-rule:sales:revenue-positive"]
            self.assertEqual(inferred["statement"], "Use only finance-eligible orders.")
            self.assertFalse(inferred["enabled"])
            self.assertEqual(
                rules["curated-rule:sales:fiscal-calendar"]["source"], "manual"
            )

            third = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), regenerated_text)

    def test_cli_can_generate_one_dashboard_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)

            result = self.run_generator(
                root,
                "--scope",
                "dashboard",
                "--id",
                "dashboard:sales:regional",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rules_root = root / "mcp_rules"
            self.assertTrue(
                (rules_root / "dashboards" / "sales" / "regional.toml").exists()
            )
            self.assertFalse(
                (rules_root / "dashboards" / "sales" / "executive.toml").exists()
            )
            self.assertFalse((rules_root / "workbooks" / "sales.toml").exists())

    def test_check_detects_stale_context_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            generated = self.run_generator(root)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            path = root / "mcp_rules" / "workbooks" / "sales.toml"
            before = path.read_text(encoding="utf-8")

            source_path = root / "sources" / "tableau" / "sales.json"
            source = json.loads(source_path.read_text(encoding="utf-8"))
            source["entities"][4]["name"] = "Recognized Revenue"
            source_path.write_text(json.dumps(source), encoding="utf-8")

            checked = self.run_generator(root, "--check")

            self.assertEqual(checked.returncode, 2)
            self.assertIn("stale MCP rule context", checked.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_reports_orphaned_context_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            orphan = root / "mcp_rules" / "dashboards" / "sales" / "renamed.toml"
            orphan.parent.mkdir(parents=True)
            orphan.write_text("mcp_rules_version = 1\n", encoding="utf-8")

            result = self.run_generator(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Orphaned MCP rule context", result.stderr)
            self.assertIn(str(orphan), result.stderr)
            self.assertTrue(orphan.exists())

    def test_check_detects_protocol_changes_without_rewriting_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            generated = self.run_generator(root)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            context = root / "mcp_rules" / "workbooks" / "sales.toml"
            before = context.read_text(encoding="utf-8")
            protocol = root / "mcp_rules" / "protocol.toml"
            protocol_payload = tomllib.loads(protocol.read_text(encoding="utf-8"))
            protocol_payload["protocol"]["instructions"].append(
                "Apply the approved reporting timezone."
            )
            protocol.write_text(tomli_w.dumps(protocol_payload), encoding="utf-8")

            checked = self.run_generator(root, "--check")

            self.assertEqual(checked.returncode, 2)
            self.assertIn("stale MCP rule protocol", checked.stderr)
            self.assertEqual(context.read_text(encoding="utf-8"), before)

    def test_invalid_curation_fails_without_overwriting_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            generated = self.run_generator(root)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            path = root / "mcp_rules" / "workbooks" / "sales.toml"
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            payload["curation"]["disabled_rule_ids"] = ["rule:does-not-exist"]
            path.write_text(tomli_w.dumps(payload), encoding="utf-8")
            before = path.read_text(encoding="utf-8")

            result = self.run_generator(root)

            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown disabled rule IDs", result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_malformed_toml_fails_without_overwriting_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            generated = self.run_generator(root)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            path = root / "mcp_rules" / "workbooks" / "sales.toml"
            malformed = 'mcp_rules_version = 1\n[scope\nid = "broken"\n'
            path.write_text(malformed, encoding="utf-8")

            result = self.run_generator(root)

            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid TOML", result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), malformed)

    def test_check_rejects_edits_outside_curation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)
            generated = self.run_generator(root)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            path = root / "mcp_rules" / "workbooks" / "sales.toml"
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            payload["metrics"][0]["name"] = "Edited outside curation"
            path.write_text(tomli_w.dumps(payload), encoding="utf-8")

            checked = self.run_generator(root, "--check")

            self.assertEqual(checked.returncode, 2)
            self.assertIn("generated MCP rule content differs", checked.stderr)


if __name__ == "__main__":
    unittest.main()
