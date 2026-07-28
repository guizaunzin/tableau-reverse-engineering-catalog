#!/usr/bin/env python3
"""Generate a review-required semantic configuration from metric contracts."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_AGGREGATIONS = {
    "avg",
    "count",
    "count_distinct",
    "max",
    "min",
    "sum",
}
SIMPLE_AGGREGATE_RE = re.compile(
    r"^\s*(SUM|AVG|MIN|MAX|COUNT|COUNTD)\s*"
    r"\(\s*(\[[^\]]+\])\s*\)\s*$",
    re.IGNORECASE,
)


class BootstrapError(RuntimeError):
    """A user-facing bootstrap error."""


def safe_identifier(value: str, fallback: str = "field") -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    identifier = re.sub(r"[^A-Za-z0-9_]+", "_", ascii_value).strip("_")
    identifier = re.sub(r"_+", "_", identifier).casefold()
    if not identifier:
        identifier = fallback
    if identifier[0].isdigit():
        identifier = f"field_{identifier}"
    return identifier


def unbracket(value: str) -> str:
    return value[1:-1] if value.startswith("[") and value.endswith("]") else value


def catalog_paths(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(source.rglob("*.json"))
    raise BootstrapError(f"Catalog source does not exist: {source}")


def load_contracts(source: Path) -> tuple[list[dict[str, Any]], list[str]]:
    contracts: list[dict[str, Any]] = []
    sources: list[str] = []
    for path in catalog_paths(source):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = payload.get("metric_contracts") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        sources.append(str(path))
        contracts.extend(item for item in items if isinstance(item, dict))
    if not contracts:
        raise BootstrapError(
            f"No metric_contracts found below catalog source: {source}"
        )
    return contracts, sources


def context_aggregations(contract: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for context in contract.get("contexts", []):
        if not isinstance(context, dict):
            continue
        for value in context.get("aggregations", []):
            if (
                isinstance(value, str)
                and value.casefold() in SUPPORTED_AGGREGATIONS
            ):
                values.append(value.casefold())
    return values


def context_grain(contract: dict[str, Any]) -> Iterable[str]:
    for context in contract.get("contexts", []):
        if not isinstance(context, dict):
            continue
        for value in context.get("grain", []):
            if isinstance(value, str) and value.strip():
                yield value


def indicator_suggestion(
    contract: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    metric = str(contract.get("metric", "")).strip()
    if not metric:
        return None, "metric_name_missing"
    scope = str(contract.get("calculation_scope", ""))
    formula = contract.get("formula_tableau")
    internal_name = unbracket(str(contract.get("internal_name", metric)))
    aggregations = context_aggregations(contract)
    review_notes: list[str] = []

    if scope == "base_measure" and formula is None:
        column = safe_identifier(internal_name, safe_identifier(metric))
        if aggregations:
            default = Counter(aggregations).most_common(1)[0][0]
            allowed = sorted(set(aggregations))
        else:
            default = "sum"
            allowed = ["sum"]
            review_notes.append(
                "No supported Tableau aggregation was detected; SUM is a draft guess."
            )
    elif isinstance(formula, str):
        match = SIMPLE_AGGREGATE_RE.fullmatch(formula)
        if match is None:
            reason = (
                f"{scope}_requires_manual_modeling"
                if scope in {"lod", "table_calculation"}
                else "compound_formula_requires_manual_modeling"
            )
            return None, reason
        function, reference = match.groups()
        default = {
            "countd": "count_distinct",
        }.get(function.casefold(), function.casefold())
        column = safe_identifier(unbracket(reference), safe_identifier(metric))
        allowed = [default]
    else:
        return None, "unsupported_metric_contract"

    return (
        {
            "column": column,
            "description": f"TODO: add the approved business definition for {metric}.",
            "default_aggregation": default,
            "allowed_aggregations": allowed,
            "review_status": "needs_review",
            "review_notes": review_notes,
            "source_contract": {
                "formula_tableau": formula,
                "calculation_scope": scope,
                "dependencies": contract.get("dependencies", []),
            },
        },
        None,
    )


def build_draft(
    contracts: list[dict[str, Any]],
    sources: list[str],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contract in contracts:
        datasource = str(contract.get("datasource", "")).strip()
        if datasource:
            grouped[datasource].append(contract)
    if not grouped:
        raise BootstrapError("Metric contracts do not contain datasource names.")

    datasources: dict[str, dict[str, Any]] = {}
    unsupported: list[dict[str, Any]] = []
    for datasource_name in sorted(grouped, key=str.casefold):
        datasource_contracts = grouped[datasource_name]
        dimensions = sorted(
            {
                dimension
                for contract in datasource_contracts
                for dimension in context_grain(contract)
            },
            key=str.casefold,
        )
        indicators: dict[str, dict[str, Any]] = {}
        for contract in sorted(
            datasource_contracts,
            key=lambda item: str(item.get("metric", "")).casefold(),
        ):
            metric = str(contract.get("metric", "")).strip()
            suggestion, reason = indicator_suggestion(contract)
            if suggestion is None:
                unsupported.append(
                    {
                        "datasource": datasource_name,
                        "metric": metric,
                        "formula_tableau": contract.get("formula_tableau"),
                        "dependencies": contract.get("dependencies", []),
                        "reason": reason,
                    }
                )
                continue
            indicators[metric] = suggestion

        slug = safe_identifier(datasource_name, "datasource")
        datasources[datasource_name] = {
            "review_status": "needs_review",
            "description": (
                f"TODO: add the approved description for {datasource_name}."
            ),
            "table": f"todo_{slug}",
            "connection": {
                "driver": "sqlite",
                "database": f"TODO_{slug}.db",
            },
            "dimensions": {
                name: {
                    "column": safe_identifier(name),
                    "description": (
                        f"TODO: add the approved business definition for {name}."
                    ),
                    "review_status": "needs_review",
                }
                for name in dimensions
            },
            "indicators": indicators,
        }

    return {
        "version": 1,
        "review_status": "needs_review",
        "generated_from": sources,
        "review_instructions": [
            "Confirm every datasource table and database path.",
            "Confirm every physical column mapping.",
            "Confirm descriptions and aggregation policies with data owners.",
            "Review unsupported_metric_contracts manually.",
            "Set each datasource review_status to approved before MCP use.",
        ],
        "datasources": datasources,
        "unsupported_metric_contracts": unsupported,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a review-required semantic config draft from Tableau "
            "metric contracts."
        )
    )
    parser.add_argument(
        "catalog",
        type=Path,
        help="generated workbook JSON file or directory containing catalogs",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="new semantic config draft path",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise BootstrapError(
            f"Refusing to overwrite existing output: {args.output}"
        )
    contracts, sources = load_contracts(args.catalog)
    payload = build_draft(contracts, sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Generated {args.output} with {len(payload['datasources'])} "
        f"datasource(s) and "
        f"{len(payload['unsupported_metric_contracts'])} unsupported "
        "metric contract(s)."
    )
    return 0


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except BootstrapError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
