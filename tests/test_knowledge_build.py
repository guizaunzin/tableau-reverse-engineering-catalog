from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from knowledge_build import find_entities, impact_analysis, load_knowledge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "knowledge_build.py"


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
            self.root / "markdown" / "metrics" / "metric-sales-revenue.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Automatic metadata", page)
        self.assertIn("source_id: tableau-workbook:sales", page)
        self.assertIn("semantic_status: inferred", page)
        self.assertIn("## Human context", page)
        self.assertIn("Recognized revenue.", page)
        self.assertIn("Use this metric for approved reporting.", page)
        self.assertIn("## Where used", page)
        self.assertIn("Overview", page)

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

    def test_impact_analysis_walks_from_dependency_to_visual_and_dashboard(self) -> None:
        model = {
            "entities": [],
            "relations": [
                {"from": "calculation:profit", "type": "depends_on", "to": "field:sales"},
                {"from": "metric:profit", "type": "calculated_by", "to": "calculation:profit"},
                {"from": "visual:overview", "type": "displays", "to": "metric:profit"},
                {"from": "dashboard:executive", "type": "contains", "to": "visual:overview"},
            ],
        }

        impacted = impact_analysis(model, "field:sales")

        self.assertEqual(
            impacted,
            [
                "calculation:profit",
                "metric:profit",
                "visual:overview",
                "dashboard:executive",
            ],
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
