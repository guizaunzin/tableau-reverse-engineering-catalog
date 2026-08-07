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
            self.assertEqual(generated, [Path("sources/tableau.json")])
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
                (output / "sources" / "tableau.json").read_text(encoding="utf-8")
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
                (first / "sources" / "tableau.json").read_bytes(),
                (second / "sources" / "tableau.json").read_bytes(),
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
                (output / "sources" / "tableau.json").read_text(encoding="utf-8")
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
                (output / "sources" / "tableau.json").read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
