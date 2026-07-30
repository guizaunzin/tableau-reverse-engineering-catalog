#!/usr/bin/env python3
"""Small deterministic analytics core for DuckDB over Parquet."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import numbers
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_AGGREGATIONS = {
    "avg",
    "count",
    "count_distinct",
    "max",
    "min",
    "sum",
}
SUPPORTED_OPERATORS = {
    "eq",
    "gte",
    "in",
    "is_not_null",
    "is_null",
    "lte",
}
SUPPORTED_TYPES = {"boolean", "date", "integer", "number", "string"}
SEMANTIC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AnalyticsError(RuntimeError):
    """A user-facing analytics configuration or query error."""


@dataclass(frozen=True)
class FieldDefinition:
    column: str
    data_type: str
    operators: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MetricDefinition:
    column: str
    aggregation: str


@dataclass(frozen=True)
class FilterCondition:
    field: str
    operator: str
    value: Any = None


@dataclass(frozen=True)
class SortDefinition:
    field: str
    direction: str


@dataclass(frozen=True)
class AnalyticsRequest:
    metric: str
    dimensions: tuple[str, ...]
    filters: tuple[FilterCondition, ...]
    sort: SortDefinition | None
    limit: int


@dataclass(frozen=True)
class SemanticModel:
    dataset: str
    workbook: str
    worksheet: str
    source_path: str
    dimensions: dict[str, FieldDefinition]
    filter_fields: dict[str, FieldDefinition]
    metrics: dict[str, MetricDefinition]
    default_filters: tuple[FilterCondition, ...]


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: tuple[Any, ...]


@dataclass(frozen=True)
class QueryResult:
    dataset: str
    worksheet: str
    sql: str
    parameters: list[Any]
    row_count: int
    rows: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "worksheet": self.worksheet,
            "sql": self.sql,
            "parameters": _jsonable(self.parameters),
            "row_count": self.row_count,
            "rows": _jsonable(self.rows),
        }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnalyticsError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AnalyticsError(f"Invalid JSON in {path}: {exc}") from exc


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalyticsError(f"{label} must be a JSON object.")
    return value


def _semantic_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SEMANTIC_NAME_RE.fullmatch(value):
        raise AnalyticsError(
            f"{label} must use letters, numbers, and underscores and "
            "cannot start with a number."
        )
    return value


def _physical_column(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalyticsError(f"{label} must define a physical column.")
    return value


def _field_definitions(
    payload: Any,
    *,
    label: str,
    require_operators: bool,
) -> dict[str, FieldDefinition]:
    items = _require_object(payload, label)
    definitions: dict[str, FieldDefinition] = {}
    for raw_name, raw_item in items.items():
        name = _semantic_name(raw_name, f"{label} name")
        item = _require_object(raw_item, f"{label}.{name}")
        data_type = item.get("type")
        if data_type not in SUPPORTED_TYPES:
            raise AnalyticsError(
                f"{label}.{name}.type must be one of "
                f"{', '.join(sorted(SUPPORTED_TYPES))}."
            )
        raw_operators = item.get("operators", [])
        if not isinstance(raw_operators, list) or any(
            not isinstance(operator, str) for operator in raw_operators
        ):
            raise AnalyticsError(f"{label}.{name}.operators must be a list.")
        operators = frozenset(raw_operators)
        if require_operators and not operators:
            raise AnalyticsError(f"{label}.{name} must allow an operator.")
        unsupported = operators - SUPPORTED_OPERATORS
        if unsupported:
            raise AnalyticsError(
                f"{label}.{name} has unsupported operators: "
                f"{', '.join(sorted(unsupported))}."
            )
        definitions[name] = FieldDefinition(
            column=_physical_column(
                item.get("column"), f"{label}.{name}"
            ),
            data_type=data_type,
            operators=operators,
        )
    return definitions


def _metric_definitions(payload: Any) -> dict[str, MetricDefinition]:
    items = _require_object(payload, "metrics")
    if not items:
        raise AnalyticsError("metrics must define at least one metric.")
    definitions: dict[str, MetricDefinition] = {}
    for raw_name, raw_item in items.items():
        name = _semantic_name(raw_name, "metric name")
        item = _require_object(raw_item, f"metrics.{name}")
        aggregation = item.get("aggregation")
        if aggregation not in SUPPORTED_AGGREGATIONS:
            raise AnalyticsError(
                f"metrics.{name}.aggregation must be one of "
                f"{', '.join(sorted(SUPPORTED_AGGREGATIONS))}."
            )
        definitions[name] = MetricDefinition(
            column=_physical_column(
                item.get("column"), f"metrics.{name}"
            ),
            aggregation=aggregation,
        )
    return definitions


def _resolve_source_path(value: Any, base_dir: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalyticsError("source.path must be a non-empty string.")
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def load_semantic_model(path: Path) -> SemanticModel:
    payload = _require_object(_read_json(path), "configuration")
    if payload.get("version") != 1:
        raise AnalyticsError("configuration.version must be 1.")
    dataset = _semantic_name(payload.get("dataset"), "dataset")
    tableau = _require_object(payload.get("tableau"), "tableau")
    workbook = tableau.get("workbook")
    worksheet = tableau.get("worksheet")
    if not isinstance(workbook, str) or not workbook.strip():
        raise AnalyticsError("tableau.workbook must be a non-empty string.")
    if not isinstance(worksheet, str) or not worksheet.strip():
        raise AnalyticsError("tableau.worksheet must be a non-empty string.")
    source = _require_object(payload.get("source"), "source")
    dimensions = _field_definitions(
        payload.get("dimensions"),
        label="dimensions",
        require_operators=False,
    )
    filter_fields = _field_definitions(
        payload.get("filter_fields"),
        label="filter_fields",
        require_operators=True,
    )
    metrics = _metric_definitions(payload.get("metrics"))
    collisions = set(dimensions) & set(metrics)
    if collisions:
        raise AnalyticsError(
            "A name cannot be both a dimension and a metric: "
            + ", ".join(sorted(collisions))
        )
    model = SemanticModel(
        dataset=dataset,
        workbook=workbook,
        worksheet=worksheet,
        source_path=_resolve_source_path(
            source.get("path"), path.resolve().parent
        ),
        dimensions=dimensions,
        filter_fields=filter_fields,
        metrics=metrics,
        default_filters=(),
    )
    raw_defaults = payload.get("default_filters", [])
    if not isinstance(raw_defaults, list):
        raise AnalyticsError("default_filters must be a list.")
    default_filters = tuple(
        _parse_filter(item, model, f"default_filters[{index}]")
        for index, item in enumerate(raw_defaults)
    )
    return SemanticModel(
        dataset=model.dataset,
        workbook=model.workbook,
        worksheet=model.worksheet,
        source_path=model.source_path,
        dimensions=model.dimensions,
        filter_fields=model.filter_fields,
        metrics=model.metrics,
        default_filters=default_filters,
    )


def _convert_scalar(value: Any, data_type: str, label: str) -> Any:
    if data_type == "string":
        if not isinstance(value, str):
            raise AnalyticsError(f"{label} must be a string.")
        return value
    if data_type == "boolean":
        if not isinstance(value, bool):
            raise AnalyticsError(f"{label} must be a boolean.")
        return value
    if data_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise AnalyticsError(f"{label} must be an integer.")
        return value
    if data_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AnalyticsError(f"{label} must be a number.")
        return value
    if data_type == "date":
        if not isinstance(value, str):
            raise AnalyticsError(f"{label} must be an ISO date.")
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise AnalyticsError(f"{label} must be an ISO date.") from exc
    raise AnalyticsError(f"Unsupported data type: {data_type}")


def _parse_filter(
    payload: Any,
    model: SemanticModel,
    label: str,
) -> FilterCondition:
    item = _require_object(payload, label)
    unknown = set(item) - {"field", "operator", "value"}
    if unknown:
        raise AnalyticsError(
            f"{label} has unknown properties: {', '.join(sorted(unknown))}."
        )
    field_name = _semantic_name(item.get("field"), f"{label}.field")
    definition = model.filter_fields.get(field_name)
    if definition is None:
        raise AnalyticsError(f"Unknown filter field: {field_name}")
    operator = item.get("operator")
    if not isinstance(operator, str) or operator not in definition.operators:
        raise AnalyticsError(
            f"Operator {operator!r} is not allowed for filter {field_name}."
        )
    if operator in {"is_null", "is_not_null"}:
        if "value" in item and item["value"] is not None:
            raise AnalyticsError(f"{label}.{operator} does not accept a value.")
        value = None
    elif operator == "in":
        raw_values = item.get("value")
        if (
            not isinstance(raw_values, list)
            or not raw_values
            or len(raw_values) > 100
        ):
            raise AnalyticsError(
                f"{label}.value must contain between 1 and 100 values."
            )
        value = tuple(
            _convert_scalar(
                raw_value,
                definition.data_type,
                f"{label}.value[{index}]",
            )
            for index, raw_value in enumerate(raw_values)
        )
    else:
        if "value" not in item:
            raise AnalyticsError(f"{label}.value is required.")
        value = _convert_scalar(
            item["value"], definition.data_type, f"{label}.value"
        )
    return FilterCondition(field=field_name, operator=operator, value=value)


def load_analytics_request(
    payload: Any,
    model: SemanticModel,
) -> AnalyticsRequest:
    item = _require_object(payload, "request")
    unknown = set(item) - {
        "dimensions",
        "filters",
        "limit",
        "metric",
        "sort",
    }
    if unknown:
        raise AnalyticsError(
            f"request has unknown properties: {', '.join(sorted(unknown))}."
        )
    metric = _semantic_name(item.get("metric"), "request.metric")
    if metric not in model.metrics:
        raise AnalyticsError(f"Unknown metric: {metric}")

    raw_dimensions = item.get("dimensions", [])
    if (
        not isinstance(raw_dimensions, list)
        or len(raw_dimensions) > 10
        or any(not isinstance(value, str) for value in raw_dimensions)
    ):
        raise AnalyticsError("request.dimensions must contain up to 10 names.")
    dimensions = tuple(raw_dimensions)
    if len(set(dimensions)) != len(dimensions):
        raise AnalyticsError("request.dimensions cannot contain duplicates.")
    unknown_dimensions = [
        name for name in dimensions if name not in model.dimensions
    ]
    if unknown_dimensions:
        raise AnalyticsError(
            f"Unknown dimension: {unknown_dimensions[0]}"
        )

    raw_filters = item.get("filters", [])
    if not isinstance(raw_filters, list) or len(raw_filters) > 20:
        raise AnalyticsError("request.filters must contain up to 20 filters.")
    filters = tuple(
        _parse_filter(value, model, f"request.filters[{index}]")
        for index, value in enumerate(raw_filters)
    )

    raw_sort = item.get("sort")
    sort = None
    if raw_sort is not None:
        sort_item = _require_object(raw_sort, "request.sort")
        if set(sort_item) != {"field", "direction"}:
            raise AnalyticsError(
                "request.sort must contain only field and direction."
            )
        sort_field = _semantic_name(
            sort_item.get("field"), "request.sort.field"
        )
        sort_direction = sort_item.get("direction")
        if sort_direction not in {"asc", "desc"}:
            raise AnalyticsError(
                "request.sort.direction must be asc or desc."
            )
        if sort_field != metric and sort_field not in dimensions:
            raise AnalyticsError(
                "request.sort.field must be the selected metric or dimension."
            )
        sort = SortDefinition(sort_field, sort_direction)

    limit = item.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise AnalyticsError("request.limit must be between 1 and 1000.")
    return AnalyticsRequest(
        metric=metric,
        dimensions=dimensions,
        filters=filters,
        sort=sort,
        limit=limit,
    )


def load_request_file(path: Path, model: SemanticModel) -> AnalyticsRequest:
    return load_analytics_request(_read_json(path), model)


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _aggregate_sql(aggregation: str, column_sql: str) -> str:
    if aggregation == "count_distinct":
        return f"COUNT(DISTINCT {column_sql})"
    functions = {
        "avg": "AVG",
        "count": "COUNT",
        "max": "MAX",
        "min": "MIN",
        "sum": "SUM",
    }
    return f"{functions[aggregation]}({column_sql})"


def _filter_sql(
    condition: FilterCondition,
    model: SemanticModel,
) -> tuple[str, list[Any]]:
    definition = model.filter_fields[condition.field]
    column = _quote_identifier(definition.column)
    if condition.operator == "is_null":
        return f"{column} IS NULL", []
    if condition.operator == "is_not_null":
        return f"{column} IS NOT NULL", []
    if condition.operator == "in":
        placeholders = ", ".join("?" for _ in condition.value)
        return f"{column} IN ({placeholders})", list(condition.value)
    operators = {"eq": "=", "gte": ">=", "lte": "<="}
    return f"{column} {operators[condition.operator]} ?", [condition.value]


def build_query(
    request: AnalyticsRequest,
    model: SemanticModel,
) -> CompiledQuery:
    select_parts: list[str] = []
    group_parts: list[str] = []
    for name in request.dimensions:
        column = _quote_identifier(model.dimensions[name].column)
        select_parts.append(f"{column} AS {_quote_identifier(name)}")
        group_parts.append(column)

    metric = model.metrics[request.metric]
    metric_expression = _aggregate_sql(
        metric.aggregation,
        _quote_identifier(metric.column),
    )
    select_parts.append(
        f"{metric_expression} AS {_quote_identifier(request.metric)}"
    )

    parameters: list[Any] = [model.source_path]
    sql_parts = [
        f"SELECT {', '.join(select_parts)}",
        "FROM read_parquet(?)",
    ]
    filters = (*model.default_filters, *request.filters)
    if filters:
        filter_parts: list[str] = []
        for condition in filters:
            fragment, values = _filter_sql(condition, model)
            filter_parts.append(fragment)
            parameters.extend(values)
        sql_parts.append(f"WHERE {' AND '.join(filter_parts)}")
    if group_parts:
        sql_parts.append(f"GROUP BY {', '.join(group_parts)}")

    order_parts: list[str] = []
    if request.sort is not None:
        order_parts.append(
            f"{_quote_identifier(request.sort.field)} "
            f"{request.sort.direction.upper()}"
        )
    for dimension in request.dimensions:
        if request.sort is None or request.sort.field != dimension:
            order_parts.append(f"{_quote_identifier(dimension)} ASC")
    if order_parts:
        sql_parts.append(f"ORDER BY {', '.join(order_parts)}")
    sql_parts.append("LIMIT ?")
    parameters.append(request.limit)
    return CompiledQuery(
        sql="\n".join(sql_parts),
        parameters=tuple(parameters),
    )


def _duckdb_module() -> Any:
    try:
        import duckdb
    except ImportError as exc:
        raise AnalyticsError(
            "DuckDB is not installed. Run: "
            "pip install -r requirements-analytics.txt"
        ) from exc
    return duckdb


def source_files(model: SemanticModel) -> list[str]:
    matches = (
        glob.glob(model.source_path, recursive=True)
        if glob.has_magic(model.source_path)
        else [model.source_path]
    )
    files = sorted(str(Path(match).resolve()) for match in matches if Path(match).is_file())
    if not files:
        raise AnalyticsError(
            f"No Parquet files match source.path: {model.source_path}"
        )
    return files


def inspect_model(model: SemanticModel) -> dict[str, Any]:
    files = source_files(model)
    duckdb = _duckdb_module()
    connection = duckdb.connect(database=":memory:")
    try:
        cursor = connection.execute(
            "SELECT * FROM read_parquet(?) LIMIT 0",
            [model.source_path],
        )
        columns = [description[0] for description in cursor.description]
    except Exception as exc:
        raise AnalyticsError(f"Cannot read configured Parquet source: {exc}") from exc
    finally:
        connection.close()
    configured_columns = {
        definition.column for definition in model.dimensions.values()
    } | {
        definition.column for definition in model.filter_fields.values()
    } | {
        definition.column for definition in model.metrics.values()
    }
    missing = sorted(configured_columns - set(columns), key=str.casefold)
    return {
        "dataset": model.dataset,
        "workbook": model.workbook,
        "worksheet": model.worksheet,
        "source_files": files,
        "columns": columns,
        "missing_configured_columns": missing,
        "status": "OK" if not missing else "FAIL",
    }


def execute_query(
    query: CompiledQuery,
    model: SemanticModel,
) -> QueryResult:
    inspection = inspect_model(model)
    if inspection["missing_configured_columns"]:
        raise AnalyticsError(
            "Configured columns are missing from Parquet: "
            + ", ".join(inspection["missing_configured_columns"])
        )
    duckdb = _duckdb_module()
    connection = duckdb.connect(database=":memory:")
    try:
        cursor = connection.execute(query.sql, list(query.parameters))
        columns = [description[0] for description in cursor.description]
        rows = [
            dict(zip(columns, raw_row, strict=True))
            for raw_row in cursor.fetchall()
        ]
    except Exception as exc:
        raise AnalyticsError(f"Query execution failed: {exc}") from exc
    finally:
        connection.close()
    return QueryResult(
        dataset=model.dataset,
        worksheet=model.worksheet,
        sql=query.sql,
        parameters=list(query.parameters),
        row_count=len(rows),
        rows=rows,
    )


def query_analytics(
    request: AnalyticsRequest,
    model: SemanticModel,
) -> QueryResult:
    return execute_query(build_query(request, model), model)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, numbers.Number) and not isinstance(
        value, (bool, int, float)
    ):
        return float(value)
    return value


def _compare_rows(
    actual: list[dict[str, Any]],
    expected: Any,
    tolerance: float,
) -> str | None:
    actual = _jsonable(actual)
    if not isinstance(expected, list) or any(
        not isinstance(row, dict) for row in expected
    ):
        return "expected_rows must be a list of objects"
    if len(actual) != len(expected):
        return f"row count differs: expected {len(expected)}, got {len(actual)}"
    for row_index, (actual_row, expected_row) in enumerate(
        zip(actual, expected, strict=True),
        start=1,
    ):
        if set(actual_row) != set(expected_row):
            return (
                f"row {row_index} columns differ: expected "
                f"{sorted(expected_row)}, got {sorted(actual_row)}"
            )
        for column, expected_value in expected_row.items():
            actual_value = actual_row[column]
            numeric_pair = (
                isinstance(actual_value, numbers.Number)
                and not isinstance(actual_value, bool)
                and isinstance(expected_value, numbers.Number)
                and not isinstance(expected_value, bool)
            )
            if numeric_pair:
                if abs(float(actual_value) - float(expected_value)) > tolerance:
                    return (
                        f"row {row_index}, column {column}: expected "
                        f"{expected_value!r}, got {actual_value!r}"
                    )
            elif actual_value != expected_value:
                return (
                    f"row {row_index}, column {column}: expected "
                    f"{expected_value!r}, got {actual_value!r}"
                )
    return None


def validate_cases(
    model: SemanticModel,
    payload: Any,
) -> dict[str, Any]:
    document = _require_object(payload, "validation document")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise AnalyticsError("validation document must contain cases.")
    results: list[dict[str, Any]] = []
    for index, raw_case in enumerate(raw_cases):
        case = _require_object(raw_case, f"cases[{index}]")
        name = case.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AnalyticsError(f"cases[{index}].name must be a string.")
        tolerance = case.get("numeric_tolerance", 0.0)
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or tolerance < 0
        ):
            raise AnalyticsError(
                f"cases[{index}].numeric_tolerance must be non-negative."
            )
        try:
            request = load_analytics_request(case.get("request"), model)
            query_result = query_analytics(request, model)
            difference = _compare_rows(
                query_result.rows,
                case.get("expected_rows"),
                float(tolerance),
            )
        except AnalyticsError as exc:
            difference = str(exc)
        results.append(
            {
                "name": name,
                "status": "OK" if difference is None else "FAIL",
                "difference": difference,
            }
        )
    passed = sum(result["status"] == "OK" for result in results)
    return {
        "status": "OK" if passed == len(results) else "FAIL",
        "passed": passed,
        "failed": len(results) - passed,
        "total": len(results),
        "cases": results,
    }


def create_example_data(model: SemanticModel, *, force: bool) -> Path:
    if glob.has_magic(model.source_path):
        raise AnalyticsError(
            "create-example requires source.path to be a single file."
        )
    output = Path(model.source_path)
    if output.exists() and not force:
        raise AnalyticsError(
            f"Example Parquet already exists: {output}. Use --force to replace it."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    duckdb = _duckdb_module()
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute(
            'CREATE TABLE sample_orders ('
            '"Order ID" VARCHAR, "Source" VARCHAR, "Order Date" DATE, '
            '"Business Status" VARCHAR)'
        )
        connection.executemany(
            "INSERT INTO sample_orders VALUES (?, ?, ?, ?)",
            [
                ("A-001", "Online", dt.date(2025, 1, 2), "VALID"),
                ("A-002", "Online", dt.date(2025, 1, 3), "VALID"),
                ("A-002", "Online", dt.date(2025, 1, 4), "VALID"),
                ("A-003", "Retail", dt.date(2025, 1, 5), "VALID"),
                ("A-004", "Retail", dt.date(2025, 1, 6), "VALID"),
                ("A-005", "Partner", dt.date(2025, 1, 7), "VALID"),
                ("A-006", "Online", dt.date(2025, 1, 8), "INVALID"),
            ],
        )
        escaped_path = str(output).replace("'", "''")
        connection.execute(
            f"COPY sample_orders TO '{escaped_path}' (FORMAT PARQUET)"
        )
    except Exception as exc:
        raise AnalyticsError(f"Cannot create example Parquet: {exc}") from exc
    finally:
        connection.close()
    return output


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run controlled analytics queries over Parquet with DuckDB."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create-example", help="create the synthetic example Parquet"
    )
    create.add_argument("--config", required=True, type=Path)
    create.add_argument("--force", action="store_true")

    check = commands.add_parser(
        "check", help="check the configuration and Parquet schema"
    )
    check.add_argument("--config", required=True, type=Path)

    query = commands.add_parser("query", help="execute one analytics request")
    query.add_argument("--config", required=True, type=Path)
    query.add_argument("--request", required=True, type=Path)

    validate = commands.add_parser(
        "validate", help="compare analytics cases with expected Tableau rows"
    )
    validate.add_argument("--config", required=True, type=Path)
    validate.add_argument("--cases", required=True, type=Path)
    return parser


def run(args: argparse.Namespace) -> int:
    model = load_semantic_model(args.config)
    if args.command == "create-example":
        output = create_example_data(model, force=args.force)
        print(json.dumps({"status": "OK", "created": str(output)}, indent=2))
        return 0
    if args.command == "check":
        result = inspect_model(model)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "OK" else 1
    if args.command == "query":
        request = load_request_file(args.request, model)
        result = query_analytics(request, model)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate":
        result = validate_cases(model, _read_json(args.cases))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "OK" else 1
    raise AnalyticsError(f"Unknown command: {args.command}")


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except AnalyticsError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
