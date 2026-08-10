from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import knowledge_mcp


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "knowledge_mcp.py"


def write_knowledge(root: Path) -> None:
    source = {
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
                "id": "dashboard:sales:executive",
                "type": "dashboard",
                "name": "Executive Dashboard",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
            {
                "id": "visual:sales:revenue-card",
                "type": "visual",
                "name": "Revenue Card",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
            {
                "id": "metric:sales:revenue",
                "type": "metric",
                "name": "Revenue",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"semantic_status": "inferred"},
            },
            {
                "id": "field:sales:revenue",
                "type": "field",
                "name": "Revenue",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"field_type": "Base field"},
            },
            {
                "id": "calculation:sales:revenue-ytd",
                "type": "calculation",
                "name": "Revenue YTD",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "formula_display": "SUM([Revenue])",
                    "classification": "Aggregate calculation",
                },
            },
            {
                "id": "business-rule:sales:revenue-positive",
                "type": "business_rule",
                "name": "Positive revenue rule",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {
                    "semantic_status": "inferred",
                    "statement": "Revenue must be positive.",
                },
            },
        ],
        "relations": [
            {
                "from": "dashboard:sales:executive",
                "type": "contains",
                "to": "visual:sales:revenue-card",
            },
            {
                "from": "visual:sales:revenue-card",
                "type": "displays",
                "to": "metric:sales:revenue",
            },
            {
                "from": "visual:sales:revenue-card",
                "type": "uses",
                "to": "calculation:sales:revenue-ytd",
            },
            {
                "from": "calculation:sales:revenue-ytd",
                "type": "depends_on",
                "to": "field:sales:revenue",
            },
            {
                "from": "metric:sales:revenue",
                "type": "calculated_by",
                "to": "calculation:sales:revenue-ytd",
            },
            {
                "from": "business-rule:sales:revenue-positive",
                "type": "implemented_by",
                "to": "calculation:sales:revenue-ytd",
            },
        ],
        "warnings": [],
    }
    tableau = root / "sources" / "tableau"
    tableau.mkdir(parents=True)
    (tableau / "sales.json").write_text(
        json.dumps(source), encoding="utf-8"
    )
    manual = root / "manual" / "business-rules"
    manual.mkdir(parents=True)
    (manual / "positive-revenue.md").write_text(
        """---
id: business-rule:sales:revenue-positive
type: business_rule
description: Revenue values below zero require investigation.
owner: Finance
---

# Positive revenue
""",
        encoding="utf-8",
    )


def add_repeated_calculations(root: Path) -> None:
    path = root / "sources" / "tableau" / "sales.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    source["entities"].extend(
        [
            {
                "id": f"datasource:sales:orders{suffix}",
                "type": "datasource",
                "name": "Orders",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"internal_name": f"orders{suffix or '-1'}"},
            }
            for suffix in ("", "-2", "-3")
        ]
    )
    source["entities"].extend(
        [
            {
                "id": "field:sales:revenue-ytd",
                "type": "field",
                "name": "Revenue YTD",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"field_type": "Calculated"},
            },
            {
                "id": "field:sales:revenue-2",
                "type": "field",
                "name": "Revenue",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"field_type": "Base field"},
            },
            {
                "id": "field:sales:revenue-ytd-2",
                "type": "field",
                "name": "Revenue YTD",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"field_type": "Calculated"},
            },
            {
                "id": "calculation:sales:revenue-ytd-2",
                "type": "calculation",
                "name": "Revenue YTD",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"formula_display": "SUM([Revenue])"},
            },
            {
                "id": "visual:sales:revenue-card-2",
                "type": "visual",
                "name": "Revenue Card 2",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
            {
                "id": "field:sales:revenue-3",
                "type": "field",
                "name": "Revenue",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"field_type": "Base field"},
            },
            {
                "id": "field:sales:revenue-ytd-3",
                "type": "field",
                "name": "Revenue YTD",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"field_type": "Calculated"},
            },
            {
                "id": "calculation:sales:revenue-ytd-3",
                "type": "calculation",
                "name": "Revenue YTD",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {"formula_display": "SUM([Revenue])"},
            },
            {
                "id": "visual:sales:revenue-card-3",
                "type": "visual",
                "name": "Revenue Card 3",
                "provenance": {"source_id": "tableau-workbook:sales"},
                "attributes": {},
            },
        ]
    )
    source["relations"].extend(
        [
            {
                "from": "field:sales:revenue",
                "type": "comes_from",
                "to": "datasource:sales:orders",
            },
            {
                "from": "field:sales:revenue-ytd",
                "type": "calculated_by",
                "to": "calculation:sales:revenue-ytd",
            },
            {
                "from": "field:sales:revenue-ytd",
                "type": "comes_from",
                "to": "datasource:sales:orders",
            },
            {
                "from": "field:sales:revenue-2",
                "type": "comes_from",
                "to": "datasource:sales:orders-2",
            },
            {
                "from": "field:sales:revenue-ytd-2",
                "type": "calculated_by",
                "to": "calculation:sales:revenue-ytd-2",
            },
            {
                "from": "field:sales:revenue-ytd-2",
                "type": "comes_from",
                "to": "datasource:sales:orders-2",
            },
            {
                "from": "calculation:sales:revenue-ytd-2",
                "type": "depends_on",
                "to": "field:sales:revenue-2",
            },
            {
                "from": "visual:sales:revenue-card-2",
                "type": "uses",
                "to": "calculation:sales:revenue-ytd-2",
            },
            {
                "from": "dashboard:sales:executive",
                "type": "contains",
                "to": "visual:sales:revenue-card-2",
            },
            {
                "from": "field:sales:revenue-3",
                "type": "comes_from",
                "to": "datasource:sales:orders-3",
            },
            {
                "from": "field:sales:revenue-ytd-3",
                "type": "calculated_by",
                "to": "calculation:sales:revenue-ytd-3",
            },
            {
                "from": "field:sales:revenue-ytd-3",
                "type": "comes_from",
                "to": "datasource:sales:orders-3",
            },
            {
                "from": "calculation:sales:revenue-ytd-3",
                "type": "depends_on",
                "to": "field:sales:revenue-3",
            },
            {
                "from": "visual:sales:revenue-card-3",
                "type": "uses",
                "to": "calculation:sales:revenue-ytd-3",
            },
            {
                "from": "dashboard:sales:executive",
                "type": "contains",
                "to": "visual:sales:revenue-card-3",
            },
        ]
    )
    path.write_text(json.dumps(source), encoding="utf-8")


class KnowledgeMcpCliTests(unittest.TestCase):
    def test_mcp_requirements_pin_v1_and_include_loader_dependency(self) -> None:
        requirements = (PROJECT_ROOT / "requirements-mcp.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("mcp>=1.28,<2", requirements)
        self.assertIn("PyYAML", requirements)

    def test_check_loads_the_v2_knowledge_base_without_starting_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(root), "--check"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["schema_version"], 2)
            self.assertEqual(summary["sources"], 1)
            self.assertEqual(summary["entities"], 6)
            self.assertEqual(summary["relations"], 6)

    @unittest.skipUnless(
        importlib.util.find_spec("mcp"),
        "optional MCP SDK is not installed",
    )
    def test_stdio_server_exposes_the_read_only_knowledge_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_knowledge(root)

            async def scenario() -> None:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=[str(SCRIPT), str(root)],
                )
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual(
                            {tool.name for tool in tools.tools},
                            {
                                "knowledge_analyze_impact",
                                "knowledge_describe_entity",
                                "knowledge_find_business_rules",
                                "knowledge_search_entities",
                                "knowledge_show_dependencies",
                                "knowledge_where_is_used",
                            },
                        )
                        for tool in tools.tools:
                            self.assertIsNotNone(tool.annotations)
                            self.assertTrue(tool.annotations.readOnlyHint)
                            self.assertFalse(tool.annotations.destructiveHint)
                            self.assertTrue(tool.annotations.idempotentHint)
                            self.assertFalse(tool.annotations.openWorldHint)

                        search_tool = next(
                            tool
                            for tool in tools.tools
                            if tool.name == "knowledge_search_entities"
                        )
                        self.assertIn(
                            "offset", search_tool.inputSchema["properties"]
                        )
                        self.assertIsNotNone(search_tool.outputSchema)

                        result = await session.call_tool(
                            "knowledge_search_entities",
                            {"query": "revenue", "entity_type": "metric"},
                        )
                        self.assertFalse(result.isError)
                        self.assertIn("metric:sales:revenue", str(result.content))
                        self.assertEqual(
                            result.structuredContent["results"][0]["id"],
                            "metric:sales:revenue",
                        )

                        error = await session.call_tool(
                            "knowledge_describe_entity",
                            {"entity_id": "metric:sales:missing"},
                        )
                        self.assertTrue(error.isError)
                        self.assertIn("Entity not found", str(error.content))

            asyncio.run(scenario())


class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "knowledge"
        write_knowledge(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def index(self):
        index_type = getattr(knowledge_mcp, "KnowledgeIndex", None)
        self.assertIsNotNone(index_type, "KnowledgeIndex must be implemented")
        return index_type.load(self.root)

    def test_search_and_describe_keep_automatic_and_manual_context_separate(
        self,
    ) -> None:
        index = self.index()

        search = index.search_entities(
            "revenue ytd",
            entity_type="calculation",
            workbook="Sales",
            limit=5,
        )
        described = index.describe_entity(
            "business-rule:sales:revenue-positive"
        )

        self.assertEqual(search["count"], 1)
        self.assertEqual(
            search["results"][0]["id"],
            "calculation:sales:revenue-ytd",
        )
        self.assertEqual(
            described["automatic"]["attributes"]["semantic_status"],
            "inferred",
        )
        self.assertEqual(described["manual"]["owner"], "Finance")
        self.assertIn("Positive revenue", described["manual_body"])

    def test_relation_tools_return_dependencies_usage_and_impact(self) -> None:
        index = self.index()

        used = index.where_is_used("field:sales:revenue")
        dependencies = index.show_dependencies(
            "metric:sales:revenue", max_depth=3
        )
        impact = index.impact_analysis(["field:sales:revenue"], max_depth=4)

        self.assertEqual(
            used["used_by"][0]["entity"]["id"],
            "calculation:sales:revenue-ytd",
        )
        self.assertEqual(
            [item["id"] for item in dependencies["dependencies"]],
            [
                "calculation:sales:revenue-ytd",
                "field:sales:revenue",
            ],
        )
        impacted_ids = {item["id"] for item in impact["impacted_entities"]}
        self.assertIn("visual:sales:revenue-card", impacted_ids)
        self.assertIn("dashboard:sales:executive", impacted_ids)

    def test_find_business_rules_searches_inferred_and_manual_text(self) -> None:
        result = self.index().find_business_rules("below zero", limit=5)

        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["results"][0]["id"],
            "business-rule:sales:revenue-positive",
        )

    def test_search_disambiguates_repeated_calculations_by_datasource(self) -> None:
        add_repeated_calculations(self.root)

        result = self.index().search_entities(
            "Revenue YTD", entity_type="calculation", limit=10
        )

        self.assertEqual(result["count"], 3)
        self.assertIn("ambiguous", result)
        self.assertTrue(result["ambiguous"])
        self.assertEqual(len(result["ambiguity_groups"]), 1)
        self.assertEqual(
            set(result["ambiguity_groups"][0]["entity_ids"]),
            {
                "calculation:sales:revenue-ytd",
                "calculation:sales:revenue-ytd-2",
                "calculation:sales:revenue-ytd-3",
            },
        )
        datasource_ids = {
            item["datasources"][0]["id"] for item in result["results"]
        }
        self.assertEqual(
            datasource_ids,
            {
                "datasource:sales:orders",
                "datasource:sales:orders-2",
                "datasource:sales:orders-3",
            },
        )
        self.assertEqual(
            {item["formula_preview"] for item in result["results"]},
            {"SUM([Revenue])"},
        )

    def test_search_paginates_results_and_reports_the_next_offset(self) -> None:
        add_repeated_calculations(self.root)

        result = self.index().search_entities(
            "Revenue YTD",
            entity_type="calculation",
            offset=1,
            limit=1,
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total_matches"], 3)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(
            result["results"][0]["id"],
            "calculation:sales:revenue-ytd-2",
        )

    def test_query_validation_rejects_invalid_inputs(self) -> None:
        index = self.index()

        with self.assertRaisesRegex(
            knowledge_mcp.KnowledgeMcpError,
            "limit must be between 1 and 100",
        ):
            index.search_entities("revenue", limit=0)
        with self.assertRaisesRegex(
            knowledge_mcp.KnowledgeMcpError,
            "offset must be zero or greater",
        ):
            index.search_entities("revenue", offset=-1)
        with self.assertRaisesRegex(
            knowledge_mcp.KnowledgeMcpError,
            "Unsupported entity type",
        ):
            index.search_entities("revenue", entity_type="unknown")
        with self.assertRaisesRegex(
            knowledge_mcp.KnowledgeMcpError,
            "max_depth must be between 1 and 10",
        ):
            index.show_dependencies("metric:sales:revenue", max_depth=0)
        with self.assertRaisesRegex(
            knowledge_mcp.KnowledgeMcpError,
            "Entity not found",
        ):
            index.describe_entity("metric:sales:missing")

    def test_impact_analysis_unions_multiple_starting_entities(self) -> None:
        add_repeated_calculations(self.root)
        starting_ids = [
            "calculation:sales:revenue-ytd",
            "calculation:sales:revenue-ytd-2",
            "calculation:sales:revenue-ytd-3",
        ]

        try:
            result = self.index().impact_analysis(starting_ids, max_depth=4)
        except TypeError as exc:
            self.fail(f"impact_analysis must accept multiple IDs: {exc}")

        self.assertEqual(
            [item["id"] for item in result["starting_entities"]], starting_ids
        )
        impacted = {item["id"]: item for item in result["impacted_entities"]}
        self.assertIn("visual:sales:revenue-card", impacted)
        self.assertIn("visual:sales:revenue-card-2", impacted)
        self.assertIn("visual:sales:revenue-card-3", impacted)
        dashboard = impacted["dashboard:sales:executive"]
        self.assertEqual(set(dashboard["reached_from"]), set(starting_ids))
        self.assertEqual(
            sum(
                item["id"] == "dashboard:sales:executive"
                for item in result["impacted_entities"]
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
