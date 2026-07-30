from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analytics_core import (
    AnalyticsError,
    create_example_data,
    load_analytics_request,
    load_semantic_model,
    query_analytics,
    validate_cases,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AnalyticsCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        config = json.loads(
            (PROJECT_ROOT / "analytics_config.example.json").read_text(
                encoding="utf-8"
            )
        )
        config["source"]["path"] = str(root / "sample_orders.parquet")
        self.config_path = root / "analytics_config.json"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.model = load_semantic_model(self.config_path)
        create_example_data(self.model, force=False)

    def run_request(self, payload: dict) -> list[dict]:
        request = load_analytics_request(payload, self.model)
        return query_analytics(request, self.model).rows

    def test_groups_and_sorts_distinct_orders(self) -> None:
        rows = self.run_request(
            {
                "metric": "orders",
                "dimensions": ["source"],
                "filters": [],
                "sort": {"field": "orders", "direction": "desc"},
                "limit": 10,
            }
        )

        self.assertEqual(
            rows,
            [
                {"source": "Online", "orders": 2},
                {"source": "Retail", "orders": 2},
                {"source": "Partner", "orders": 1},
            ],
        )

    def test_applies_configured_filter(self) -> None:
        rows = self.run_request(
            {
                "metric": "orders",
                "dimensions": ["source"],
                "filters": [
                    {
                        "field": "source",
                        "operator": "eq",
                        "value": "Partner",
                    }
                ],
                "limit": 10,
            }
        )

        self.assertEqual(rows, [{"source": "Partner", "orders": 1}])

    def test_returns_total_without_dimension(self) -> None:
        rows = self.run_request(
            {
                "metric": "orders",
                "dimensions": [],
                "filters": [],
                "limit": 10,
            }
        )

        self.assertEqual(rows, [{"orders": 5}])

    def test_rejects_unknown_dimension(self) -> None:
        with self.assertRaisesRegex(AnalyticsError, "Unknown dimension"):
            load_analytics_request(
                {
                    "metric": "orders",
                    "dimensions": ["drop_table"],
                    "filters": [],
                },
                self.model,
            )

    def test_rejects_disallowed_filter_operator(self) -> None:
        with self.assertRaisesRegex(AnalyticsError, "is not allowed"):
            load_analytics_request(
                {
                    "metric": "orders",
                    "dimensions": [],
                    "filters": [
                        {
                            "field": "source",
                            "operator": "gte",
                            "value": "Online",
                        }
                    ],
                },
                self.model,
            )

    def test_example_validation_cases_pass(self) -> None:
        cases = json.loads(
            (PROJECT_ROOT / "validation_cases.example.json").read_text(
                encoding="utf-8"
            )
        )

        report = validate_cases(self.model, cases)

        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["passed"], 5)


if __name__ == "__main__":
    unittest.main()
