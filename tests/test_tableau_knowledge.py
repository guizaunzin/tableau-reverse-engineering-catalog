from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "tableau_doc.py"
BUILDER = PROJECT_ROOT / "knowledge_build.py"


def tableau_source(output: Path, workbook_slug: str) -> Path:
    return output / "sources" / "tableau" / f"{workbook_slug}.json"

SIMPLE_WORKBOOK = """\
<?xml version="1.0" encoding="utf-8"?>
<workbook name="Superstore">
  <datasources>
    <datasource name="federated.superstore" caption="Superstore">
      <column name="[Sales]" caption="Sales" datatype="real" role="measure" />
      <column name="[Profit]" caption="Profit" datatype="real" role="measure" />
      <column name="[Region]" caption="Region" datatype="string" role="dimension" />
      <column name="[Calculation_1001]" caption="Profit Ratio" datatype="real" role="measure">
        <calculation formula="SUM([Profit]) / SUM([Sales])" />
      </column>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name="Executive Overview">
      <table><view>
        <rows>[federated.superstore].[sum:Calculation_1001:qk]</rows>
        <filter class="categorical" column="[federated.superstore].[none:Region:nk]">
          <groupfilter function="member" member="&quot;West&quot;" />
        </filter>
      </view></table>
    </worksheet>
  </worksheets>
</workbook>
"""

DASHBOARD_GROUPED_WORKBOOK = """\
<?xml version="1.0" encoding="utf-8"?>
<workbook name="Dashboard Groups">
  <datasources>
    <datasource name="source.dashboard" caption="Dashboard Source">
      <column name="[Sales]" caption="Sales" datatype="real" role="measure" />
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name="Executive KPI"><table><view><rows>[source.dashboard].[Sales]</rows></view></table></worksheet>
    <worksheet name="Regional Detail"><table><view><rows>[source.dashboard].[Sales]</rows></view></table></worksheet>
    <worksheet name="Shared Legend"><table><view><rows>[source.dashboard].[Sales]</rows></view></table></worksheet>
    <worksheet name="Export Only"><table><view><rows>[source.dashboard].[Sales]</rows></view></table></worksheet>
  </worksheets>
  <dashboards>
    <dashboard name="Executive Overview"><zones><zone name="Executive KPI" /><zone name="Shared Legend" /></zones></dashboard>
    <dashboard name="Regional Analysis"><zones><zone name="Regional Detail" /><zone name="Shared Legend" /></zones></dashboard>
  </dashboards>
</workbook>
"""

FIXED_LAYOUT_WORKBOOK = """\
<?xml version="1.0" encoding="utf-8"?>
<workbook name="Fixed Layout">
  <datasources>
    <datasource name="source.layout" caption="Layout Source">
      <column name="[Sales]" caption="Sales" datatype="real" role="measure" />
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name="Revenue Evolution"><table><view><rows>[source.layout].[Sales]</rows></view></table></worksheet>
  </worksheets>
  <dashboards>
    <dashboard name="Executive Overview">
      <size maxheight="800" maxwidth="1200" minheight="800" minwidth="1200" sizing-mode="fixed" />
      <zones>
        <zone h="100000" id="1" type-v2="layout-basic" w="100000" x="0" y="0">
          <zone h="50000" id="2" param="horz" type-v2="layout-flow" w="100000" x="0" y="0">
            <zone h="40000" id="3" name="Revenue Evolution" w="50000" x="10000" y="20000" />
            <zone h="10000" id="4" type-v2="text" w="30000" x="65000" y="5000">
              <formatted-text><run>Executive summary</run></formatted-text>
            </zone>
            <zone h="10000" id="6" name="Sales legend" type-v2="color" w="20000" x="65000" y="16000" />
          </zone>
        </zone>
      </zones>
    </dashboard>
  </dashboards>
</workbook>
"""


class TableauKnowledgeCliTests(unittest.TestCase):
    def run_extractor(
        self,
        source: Path,
        output: Path,
        *extra_arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(source),
                "--output",
                str(output),
                *extra_arguments,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_writes_normalized_knowledge_source_as_the_only_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "superstore.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(SIMPLE_WORKBOOK), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = [
                path.relative_to(output)
                for path in output.rglob("*")
                if path.is_file()
            ]
            self.assertEqual(
                generated, [Path("sources/tableau/superstore.json")]
            )
            payload = json.loads((output / generated[0]).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["source_type"], "tableau")

    def test_normalizes_tableau_objects_and_semantic_relations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "dashboard-groups.twb"
            output = root / "knowledge"
            source.write_text(
                textwrap.dedent(DASHBOARD_GROUPED_WORKBOOK), encoding="utf-8"
            )

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "dashboard-groups").read_text(
                    encoding="utf-8"
                )
            )
            entities = payload["entities"]
            by_type: dict[str, list[dict[str, object]]] = {}
            for entity in entities:
                by_type.setdefault(str(entity["type"]), []).append(entity)
            self.assertEqual(len(by_type["dashboard"]), 2)
            self.assertEqual(len(by_type["visual"]), 4)
            shared = next(
                entity for entity in by_type["visual"] if entity["name"] == "Shared Legend"
            )
            containers = {
                relation["from"]
                for relation in payload["relations"]
                if relation["type"] == "contains" and relation["to"] == shared["id"]
            }
            self.assertEqual(len(containers), 2)
            self.assertIn("datasource", by_type)
            self.assertIn("field", by_type)
            self.assertIn("metric", by_type)
            self.assertTrue(
                any(relation["type"] == "displays" for relation in payload["relations"])
            )

    def test_displayed_metrics_exclude_generic_formatted_text_references(self) -> None:
        workbook = """\
        <workbook name="Display Contexts">
          <datasources><datasource name="source" caption="Source">
            <column name="[Sales]" caption="Sales" datatype="real" role="measure" />
            <column name="[Profit]" caption="Profit" datatype="real" role="measure" />
          </datasource></datasources>
          <worksheets><worksheet name="KPI"><table><view>
            <text column="[source].[Sales]" />
            <formatted-text><run>[source].[Profit]</run></formatted-text>
          </view></table></worksheet></worksheets>
        </workbook>
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "display-contexts.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(workbook), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "display-contexts").read_text(encoding="utf-8")
            )
            entities = {item["id"]: item for item in payload["entities"]}
            displayed_names = {
                entities[relation["to"]]["name"]
                for relation in payload["relations"]
                if relation["type"] == "displays"
            }
            self.assertEqual(displayed_names, {"Sales"})

    def test_infers_traceable_rules_for_displayed_conditional_metrics(self) -> None:
        workbook = """\
        <workbook name="Rule Inference">
          <datasources><datasource name="source" caption="Orders">
            <column name="[Order Date]" caption="Order Date" datatype="date" role="dimension" />
            <column name="[Customer ID]" caption="Customer ID" datatype="string" role="dimension" />
            <column name="[Profit]" caption="Profit" datatype="real" role="measure" />
            <column name="[Region]" caption="Region" datatype="string" role="dimension" />
            <column name="[Customers YTD]" caption="Customers YTD" datatype="integer" role="measure">
              <calculation formula="COUNTD(IF YEAR([Order Date]) = YEAR(TODAY()) THEN [Customer ID] END)" />
            </column>
            <column name="[Customers Sign]" caption="Customers Sign" datatype="string" role="measure">
              <calculation formula="IF [Customers YTD] &gt; 0 THEN '▲' END" />
            </column>
            <column name="[Positive Profit]" caption="Positive Profit" datatype="real" role="measure">
              <calculation formula="IF SUM([Profit]) &gt; 0 THEN SUM([Profit]) END" />
            </column>
            <column name="[Region Selector]" caption="Region Selector" datatype="string" role="measure">
              <calculation formula="IF [Region] = 'All' THEN 'All' ELSE ATTR([Region]) END" />
            </column>
            <column name="[Customers Delta Plus]" caption="Customers Delta +" datatype="real" role="measure">
              <calculation formula="IF [Customers YTD] &gt; 0 THEN [Customers YTD] END" />
            </column>
          </datasource></datasources>
          <worksheets><worksheet name="Customer KPI"><table><view>
            <text column="[source].[Customers YTD]" />
            <text column="[source].[Customers Sign]" />
            <text column="[source].[Positive Profit]" />
            <text column="[source].[Region Selector]" />
            <text column="[source].[Customers Delta Plus]" />
          </view></table></worksheet></worksheets>
        </workbook>
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "rules.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(workbook), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "rule-inference").read_text(encoding="utf-8")
            )
            entities = {item["id"]: item for item in payload["entities"]}
            rules = [item for item in entities.values() if item["type"] == "business_rule"]
            self.assertEqual(
                {item["id"] for item in rules},
                {
                    "business-rule:rule-inference:customers-ytd-inferred",
                    "business-rule:rule-inference:positive-profit-inferred",
                },
            )
            rule = entities["business-rule:rule-inference:customers-ytd-inferred"]
            self.assertEqual(
                rule["id"], "business-rule:rule-inference:customers-ytd-inferred"
            )
            self.assertEqual(rule["attributes"]["semantic_status"], "inferred")
            self.assertEqual(rule["attributes"]["confidence"], "high")
            self.assertEqual(rule["attributes"]["rule_kind"], "time_window")
            self.assertIn("counts distinct Customer ID", rule["attributes"]["statement"])
            self.assertIn("Order Date", rule["attributes"]["statement"])
            self.assertEqual(
                rule["attributes"]["field_mentions"],
                ["Customer ID", "Customers YTD", "Order Date"],
            )
            self.assertEqual(
                rule["attributes"]["evidence_calculation_ids"],
                ["calculation:rule-inference:customers-ytd"],
            )
            relation_keys = {
                (item["from"], item["type"], item["to"])
                for item in payload["relations"]
            }
            self.assertIn(
                (
                    "metric:rule-inference:customers-ytd",
                    "affected_by",
                    rule["id"],
                ),
                relation_keys,
            )
            self.assertIn(
                (
                    rule["id"],
                    "implemented_by",
                    "calculation:rule-inference:customers-ytd",
                ),
                relation_keys,
            )
            self.assertNotIn(
                "business-rule:rule-inference:customers-sign-inferred", entities
            )
            self.assertNotIn(
                "business-rule:rule-inference:region-selector-inferred", entities
            )
            self.assertNotIn(
                "business-rule:rule-inference:customers-delta-inferred", entities
            )
            self.assertEqual(
                entities[
                    "business-rule:rule-inference:positive-profit-inferred"
                ]["attributes"]["statement"],
                "Positive Profit applies SUM(Profit) only when SUM(Profit) > 0.",
            )

    def test_reuses_filters_and_ignores_actions_and_dummy_fields(self) -> None:
        workbook = """\
        <workbook name="Filter Semantics">
          <datasources><datasource name="source" caption="Orders">
            <column name="[Region]" caption="Region" datatype="string" role="dimension" />
            <column name="[Dummy]" caption="Dummy" datatype="string" role="dimension" />
            <column name="[Dummy Helper]" caption="Dummy Helper" datatype="string" role="measure">
              <calculation formula="[Dummy]" />
            </column>
          </datasource></datasources>
          <worksheets>
            <worksheet name="Map"><table><view>
              <rows>[source].[Region]</rows>
              <filter class="categorical" column="[source].[Region]">
                <groupfilter function="member" member="&quot;West&quot;" />
              </filter>
              <filter class="categorical" column="[source].[Action (Dummy,Region)]" />
              <text column="[source].[Dummy Helper]" />
            </view></table></worksheet>
            <worksheet name="Table"><table><view>
              <rows>[source].[Region]</rows>
              <filter class="categorical" column="[source].[Region]">
                <groupfilter function="member" member="&quot;West&quot;" />
              </filter>
            </view></table></worksheet>
          </worksheets>
        </workbook>
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "filters.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(workbook), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "filter-semantics").read_text(
                    encoding="utf-8"
                )
            )
            filters = [
                entity for entity in payload["entities"] if entity["type"] == "filter"
            ]
            self.assertEqual([entity["name"] for entity in filters], ["Region"])
            affected_visuals = {
                relation["from"]
                for relation in payload["relations"]
                if relation["type"] == "affected_by"
                and relation["to"] == filters[0]["id"]
            }
            self.assertEqual(
                affected_visuals,
                {
                    "visual:filter-semantics:map",
                    "visual:filter-semantics:table",
                },
            )
            names = {str(entity["name"]) for entity in payload["entities"]}
            self.assertFalse(any("action" in name.casefold() for name in names))
            self.assertFalse(any("dummy" in name.casefold() for name in names))

    def test_extracts_fixed_dashboard_layout_without_creating_zone_entities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "fixed-layout.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(FIXED_LAYOUT_WORKBOOK), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "fixed-layout").read_text(encoding="utf-8")
            )
            dashboard = next(
                item for item in payload["entities"] if item["type"] == "dashboard"
            )
            layout = dashboard["attributes"]["layout"]
            self.assertEqual(layout["status"], "complete")
            self.assertEqual(layout["sizing_mode"], "fixed")
            self.assertEqual(layout["dashboard_width"], 1200)
            self.assertEqual(layout["dashboard_height"], 800)
            self.assertEqual(
                layout["coordinate_space"], {"width": 100000, "height": 100000}
            )
            self.assertEqual(
                layout["items"],
                [
                    {
                        "document_order": 1,
                        "tableau_zone_id": "3",
                        "kind": "visual",
                        "visual_id": "visual:fixed-layout:revenue-evolution",
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
                        "label": "Executive summary",
                        "x": 65000,
                        "y": 5000,
                        "width": 30000,
                        "height": 10000,
                        "hidden": False,
                    },
                    {
                        "document_order": 3,
                        "tableau_zone_id": "6",
                        "kind": "control",
                        "label": "Sales legend",
                        "x": 65000,
                        "y": 16000,
                        "width": 20000,
                        "height": 10000,
                        "hidden": False,
                    },
                ],
            )
            self.assertFalse(any(item["type"] == "zone" for item in payload["entities"]))

    def test_layout_warnings_do_not_fail_extraction_or_invent_positions(self) -> None:
        workbook = FIXED_LAYOUT_WORKBOOK.replace(
            '<zone h="40000" id="3" name="Revenue Evolution" w="50000" x="10000" y="20000" />',
            '<zone h="40000" id="3" name="Revenue Evolution" w="50000" y="20000" />\n'
            '            <zone h="10000" id="5" name="Missing Worksheet" w="10000" x="1000" y="1000" />',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "partial-layout.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(workbook), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "fixed-layout").read_text(encoding="utf-8")
            )
            dashboard = next(
                item for item in payload["entities"] if item["type"] == "dashboard"
            )
            layout = dashboard["attributes"]["layout"]
            self.assertEqual(layout["status"], "partial")
            self.assertEqual(
                [item["kind"] for item in layout["items"]], ["text", "control"]
            )
            self.assertTrue(
                any("missing or invalid coordinates" in warning for warning in layout["warnings"])
            )
            self.assertTrue(
                any("unresolved worksheet" in warning for warning in layout["warnings"])
            )

    def test_generated_ids_and_output_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "superstore.twb"
            first = root / "first"
            second = root / "second"
            source.write_text(textwrap.dedent(SIMPLE_WORKBOOK), encoding="utf-8")

            first_result = self.run_extractor(source, first)
            second_result = self.run_extractor(source, second)

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertEqual(
                tableau_source(first, "superstore").read_bytes(),
                tableau_source(second, "superstore").read_bytes(),
            )

    def test_preserves_formula_filter_and_direct_display_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "superstore.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(SIMPLE_WORKBOOK), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "superstore").read_text(encoding="utf-8")
            )
            entities = {item["id"]: item for item in payload["entities"]}
            calculation = next(
                item
                for item in entities.values()
                if item["type"] == "calculation" and item["name"] == "Profit Ratio"
            )
            self.assertEqual(
                calculation["attributes"]["formula_tableau"],
                "SUM([Profit]) / SUM([Sales])",
            )
            filter_entity = next(
                item for item in entities.values() if item["type"] == "filter"
            )
            self.assertEqual(filter_entity["attributes"]["value"], "West")
            displayed_names = {
                entities[relation["to"]]["name"]
                for relation in payload["relations"]
                if relation["type"] == "displays"
            }
            self.assertEqual(displayed_names, {"Profit Ratio"})

    def test_calculated_measure_without_explicit_role_is_an_inferred_metric(self) -> None:
        workbook = """\
        <workbook name="Implicit Metric">
          <datasources><datasource name="source" caption="Source">
            <column name="[Sales]" caption="Sales" datatype="real" />
            <column name="[Calculation_Margin]" caption="Margin" datatype="real">
              <calculation formula="[Sales] * 0.2" />
            </column>
          </datasource></datasources>
          <worksheets><worksheet name="Summary"><table><view>
            <rows>[source].[Calculation_Margin]</rows>
          </view></table></worksheet></worksheets>
        </workbook>
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "implicit.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(workbook), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "implicit-metric").read_text(
                    encoding="utf-8"
                )
            )
            metrics = [item for item in payload["entities"] if item["type"] == "metric"]
            self.assertEqual([item["name"] for item in metrics], ["Margin"])

    def test_rejects_unsafe_twbx_member_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "unsafe.twbx"
            output = root / "knowledge"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("../escaped.twb", SIMPLE_WORKBOOK)

            result = self.run_extractor(source, output, "--strict")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe path in TWBX", result.stderr)
            self.assertFalse((root / "escaped.twb").exists())

    def test_duplicate_unnamed_worksheets_receive_distinct_visual_ids(self) -> None:
        workbook = """\
        <workbook name="Duplicate Worksheets">
          <datasources><datasource name="source" caption="Source">
            <column name="[Sales]" caption="Sales" datatype="real" role="measure" />
          </datasource></datasources>
          <worksheets>
            <worksheet><table><view><rows>[source].[Sales]</rows></view></table></worksheet>
            <worksheet><table><view><rows>[source].[Sales]</rows></view></table></worksheet>
          </worksheets>
        </workbook>
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "duplicates.twb"
            output = root / "knowledge"
            source.write_text(textwrap.dedent(workbook), encoding="utf-8")

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                tableau_source(output, "duplicate-worksheets").read_text(
                    encoding="utf-8"
                )
            )
            visual_ids = [
                item["id"] for item in payload["entities"] if item["type"] == "visual"
            ]
            self.assertEqual(len(visual_ids), 2)
            self.assertEqual(len(set(visual_ids)), 2)
            check = subprocess.run(
                [sys.executable, str(BUILDER), str(output), "--check"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_extracting_one_workbook_preserves_previous_workbook_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.twb"
            second = root / "second.twb"
            output = root / "knowledge"
            first.write_text(
                textwrap.dedent(SIMPLE_WORKBOOK).replace(
                    'name="Superstore"', 'name="First Workbook"', 1
                ),
                encoding="utf-8",
            )
            second.write_text(
                textwrap.dedent(SIMPLE_WORKBOOK).replace(
                    'name="Superstore"', 'name="Second Workbook"', 1
                ),
                encoding="utf-8",
            )

            first_result = self.run_extractor(first, output)
            second_result = self.run_extractor(second, output)

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertTrue(tableau_source(output, "first-workbook").exists())
            self.assertTrue(tableau_source(output, "second-workbook").exists())
            check = subprocess.run(
                [sys.executable, str(BUILDER), str(output), "--check"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_first_incremental_run_migrates_the_legacy_aggregate_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "second.twb"
            output = root / "knowledge"
            legacy_path = output / "sources" / "tableau.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_type": "tableau",
                        "sources": [
                            {
                                "id": "tableau-workbook:first-workbook",
                                "type": "tableau_workbook",
                                "name": "First Workbook",
                                "file": "first.twb",
                            }
                        ],
                        "entities": [],
                        "relations": [],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )
            source.write_text(
                textwrap.dedent(SIMPLE_WORKBOOK).replace(
                    'name="Superstore"', 'name="Second Workbook"', 1
                ),
                encoding="utf-8",
            )

            result = self.run_extractor(source, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(legacy_path.exists())
            self.assertTrue(tableau_source(output, "first-workbook").exists())
            self.assertTrue(tableau_source(output, "second-workbook").exists())


if __name__ == "__main__":
    unittest.main()
