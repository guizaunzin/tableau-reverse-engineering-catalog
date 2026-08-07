#!/usr/bin/env python3
"""Extract and normalize semantic metadata from Tableau workbooks.

The implementation intentionally uses only the Python standard library.  It
reads workbook metadata, not extracts or the data referenced by a workbook.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable
from xml.etree import ElementTree as ET


SCHEMA_VERSION = 2
MAX_TWB_BYTES = 50 * 1024 * 1024
FIELD_REFERENCE_RE = re.compile(
    r"(?:\[[^\]\r\n]+\]\.)?\[[^\]\r\n]+\]"
)
SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")
AGGREGATE_FUNCTIONS = {
    "AVG",
    "COUNT",
    "COUNTD",
    "MAX",
    "MEDIAN",
    "MIN",
    "PERCENTILE",
    "STDEV",
    "STDEVP",
    "SUM",
    "VAR",
    "VARP",
}
TABLE_CALC_FUNCTIONS = {
    "FIRST",
    "INDEX",
    "LAST",
    "LOOKUP",
    "PREVIOUS_VALUE",
    "RANK",
    "RANK_DENSE",
    "RANK_MODIFIED",
    "RANK_PERCENTILE",
    "RANK_UNIQUE",
    "RUNNING_AVG",
    "RUNNING_COUNT",
    "RUNNING_MAX",
    "RUNNING_MIN",
    "RUNNING_SUM",
    "SIZE",
    "TOTAL",
    "WINDOW_AVG",
    "WINDOW_COUNT",
    "WINDOW_MAX",
    "WINDOW_MEDIAN",
    "WINDOW_MIN",
    "WINDOW_PERCENTILE",
    "WINDOW_STDEV",
    "WINDOW_STDEVP",
    "WINDOW_SUM",
    "WINDOW_VAR",
    "WINDOW_VARP",
}


class CatalogError(RuntimeError):
    """A user-facing extraction error."""


@dataclass
class Field:
    key: str
    datasource: str
    datasource_caption: str
    datasource_identity: str | None
    internal_name: str
    caption: str
    datatype: str | None
    role: str | None
    raw_formula: str | None
    is_parameter: bool
    dependencies: list[str] = field(default_factory=list)
    display_formula: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def field_type(self) -> str:
        if self.is_parameter:
            return "Parameter"
        if self.raw_formula is not None:
            return "Calculated"
        return "Base field"


@dataclass
class FilterInfo:
    field_key: str | None
    field_label: str
    operator: str
    value: str
    raw_reference: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FieldUsage:
    field_key: str
    context: str
    aggregation: str | None = None


@dataclass
class Worksheet:
    name: str
    direct_fields: set[str] = field(default_factory=set)
    field_usages: list[FieldUsage] = field(default_factory=list)
    filters: list[FilterInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Dashboard:
    name: str
    worksheets: list[str] = field(default_factory=list)


@dataclass
class Workbook:
    name: str
    source: str
    fields: dict[str, Field]
    worksheets: list[Worksheet]
    dashboards: list[Dashboard] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def local_name(tag: str) -> str:
    """Return an XML tag without its namespace."""
    return tag.rsplit("}", 1)[-1].lower()


def slugify(value: str, fallback: str = "item") -> str:
    slug = SAFE_SLUG_RE.sub("-", value.casefold()).strip("-")
    return slug or fallback


def unbracket(value: str) -> str:
    if value.startswith("[") and value.endswith("]"):
        return value[1:-1]
    return value


def is_generated_measure_field(value: str) -> bool:
    """Identify Tableau's synthetic Measure Names/Measure Values fields."""
    _, token = reference_parts(value)
    normalized = re.sub(r"[^a-z]", "", unbracket(token).casefold())
    return normalized in {"measurenames", "measurevalues"}


def field_key(datasource: str, internal_name: str) -> str:
    return f"{datasource}\x1f{internal_name}"


def iter_source_files(input_path: Path) -> list[Path]:
    if not input_path.exists():
        raise CatalogError(f"Input does not exist: {input_path}")
    if input_path.is_file():
        if input_path.suffix.casefold() not in {".twb", ".twbx"}:
            raise CatalogError("Input must be a .twb, .twbx, or a directory.")
        return [input_path]
    return sorted(
        (
            path
            for path in input_path.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".twb", ".twbx"}
        ),
        key=lambda path: str(path).casefold(),
    )


def read_workbook_xml(path: Path) -> bytes:
    if path.suffix.casefold() == ".twb":
        payload = path.read_bytes()
        if len(payload) > MAX_TWB_BYTES:
            raise CatalogError(f"TWB exceeds {MAX_TWB_BYTES} bytes: {path}")
        return payload

    with zipfile.ZipFile(path) as archive:
        candidates: list[zipfile.ZipInfo] = []
        for member in archive.infolist():
            pure_path = PurePosixPath(member.filename)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                raise CatalogError(f"Unsafe path in TWBX: {member.filename}")
            if not member.is_dir() and member.filename.casefold().endswith(".twb"):
                candidates.append(member)
        if len(candidates) != 1:
            raise CatalogError(
                f"Expected exactly one .twb inside {path.name}; found {len(candidates)}."
            )
        member = candidates[0]
        if member.file_size > MAX_TWB_BYTES:
            raise CatalogError(f"Embedded TWB is too large: {path}")
        return archive.read(member)


def parse_xml(payload: bytes, source: Path) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise CatalogError(f"Invalid Tableau XML in {source}: {exc}") from exc


def formula_classification(value: str | None, is_parameter: bool) -> str:
    if is_parameter:
        return "Parameter"
    if not value:
        return "Base field"
    upper = value.upper()
    if re.search(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", upper):
        return "LOD"
    if any(re.search(rf"\b{re.escape(name)}\s*\(", upper) for name in TABLE_CALC_FUNCTIONS):
        return "Table calculation"
    if any(re.search(rf"\b{re.escape(name)}\s*\(", upper) for name in AGGREGATE_FUNCTIONS):
        return "Aggregate calculation"
    if "[PARAMETERS]." in upper:
        return "Parameter-driven calculation"
    return "Row-level calculation"


def build_field_catalog(root: ET.Element) -> dict[str, Field]:
    fields: dict[str, Field] = {}
    for datasource in (node for node in root.iter() if local_name(node.tag) == "datasource"):
        datasource_name = (
            datasource.get("name")
            or datasource.get("caption")
            or "unknown-datasource"
        )
        datasource_caption = datasource.get("caption") or datasource_name
        repository = next(
            (
                node
                for node in datasource.iter()
                if local_name(node.tag) == "repository-location"
            ),
            None,
        )
        datasource_identity = None
        if repository is not None:
            repository_path = repository.get("path")
            repository_id = repository.get("id")
            if repository_path:
                datasource_identity = f"path:{repository_path}"
            elif repository_id:
                datasource_identity = f"id:{repository_id}"
        for column in (
            node for node in datasource.iter() if local_name(node.tag) == "column"
        ):
            internal = column.get("name")
            if not internal or not internal.startswith("["):
                continue
            if is_generated_measure_field(internal) or is_generated_measure_field(
                column.get("caption") or ""
            ):
                continue
            calculation = next(
                (
                    child
                    for child in column
                    if local_name(child.tag) == "calculation"
                ),
                None,
            )
            raw_formula = calculation.get("formula") if calculation is not None else None
            is_parameter = bool(column.get("param-domain-type")) or internal.casefold().startswith(
                "[parameters]."
            )
            key = field_key(datasource_name, internal)
            candidate = Field(
                key=key,
                datasource=datasource_name,
                datasource_caption=datasource_caption,
                datasource_identity=datasource_identity,
                internal_name=internal,
                caption=column.get("caption") or unbracket(internal.split(".")[-1]),
                datatype=column.get("datatype"),
                role=column.get("role"),
                raw_formula=html.unescape(raw_formula) if raw_formula is not None else None,
                is_parameter=is_parameter,
            )
            existing = fields.get(key)
            if existing is None or (
                existing.raw_formula is None and candidate.raw_formula is not None
            ):
                fields[key] = candidate
    return fields


def reference_parts(reference: str) -> tuple[str | None, str]:
    parts = re.findall(r"\[[^\]]+\]", reference)
    if len(parts) >= 2:
        return unbracket(parts[-2]), normalize_shelf_token(parts[-1])
    return None, normalize_shelf_token(parts[-1]) if parts else reference


def normalize_shelf_token(token: str) -> str:
    """Turn Tableau shelf tokens such as [sum:Sales:qk] into [Sales]."""
    value = unbracket(token)
    parts = value.split(":")
    if len(parts) >= 3 and parts[-1].casefold() in {
        "nk",
        "ok",
        "qk",
        "sk",
        "tk",
    }:
        value = ":".join(parts[1:-1])
    return f"[{value}]"


def aggregation_from_reference(reference: str) -> str | None:
    parts = re.findall(r"\[[^\]]+\]", reference)
    if not parts:
        return None
    tokens = unbracket(parts[-1]).split(":")
    if (
        len(tokens) >= 3
        and tokens[-1].casefold() in {"nk", "ok", "qk", "sk", "tk"}
        and tokens[0].casefold() != "none"
    ):
        return tokens[0].casefold()
    return None


def resolve_reference(
    reference: str,
    fields: dict[str, Field],
    preferred_datasource: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a Tableau field reference to exactly one catalog field."""
    qualifier, final_token = reference_parts(reference)
    candidates: set[str] = set()

    datasource_qualified = bool(qualifier) and any(
        item.datasource.casefold() == qualifier.casefold()
        for item in fields.values()
    )
    if datasource_qualified:
        for key, item in fields.items():
            if item.datasource.casefold() != qualifier.casefold():
                continue
            if final_token == item.internal_name or (
                unbracket(final_token).casefold() == item.caption.casefold()
            ):
                candidates.add(key)
        if len(candidates) == 1:
            return next(iter(candidates)), None
        if len(candidates) > 1:
            return None, f"Ambiguous field reference: {reference}"

    for key, item in fields.items():
        if preferred_datasource and item.datasource != preferred_datasource:
            continue
        if reference == item.internal_name or final_token == item.internal_name:
            candidates.add(key)
        elif unbracket(final_token).casefold() == item.caption.casefold():
            candidates.add(key)

    if not candidates and qualifier and not datasource_qualified:
        for key, item in fields.items():
            if item.datasource.casefold() != qualifier.casefold():
                continue
            if final_token == item.internal_name or (
                unbracket(final_token).casefold() == item.caption.casefold()
            ):
                candidates.add(key)

    if not candidates and preferred_datasource:
        return resolve_reference(reference, fields, None)
    if len(candidates) == 1:
        return next(iter(candidates)), None
    if len(candidates) > 1:
        return None, f"Ambiguous field reference: {reference}"
    return None, f"Unresolved field reference: {reference}"


def resolve_formulas(fields: dict[str, Field]) -> None:
    for item in fields.values():
        if item.raw_formula is None or item.is_parameter:
            item.display_formula = item.raw_formula
            continue
        replacements: list[tuple[int, int, str]] = []
        dependencies: list[str] = []
        for match in FIELD_REFERENCE_RE.finditer(item.raw_formula):
            reference = match.group(0)
            dependency, warning = resolve_reference(
                reference, fields, preferred_datasource=item.datasource
            )
            if dependency is None:
                if warning and warning not in item.warnings:
                    item.warnings.append(warning)
                continue
            dependencies.append(dependency)
            replacements.append(
                (match.start(), match.end(), f"[{fields[dependency].caption}]")
            )
        item.dependencies = list(dict.fromkeys(dependencies))
        display = item.raw_formula
        for start, end, replacement in reversed(replacements):
            display = display[:start] + replacement + display[end:]
        item.display_formula = display


def references_from_element(
    worksheet_node: ET.Element,
    fields: dict[str, Field],
) -> tuple[set[str], list[str], list[FieldUsage]]:
    resolved: set[str] = set()
    warnings: list[str] = []
    usages: list[FieldUsage] = []

    def visit(node: ET.Element, inside_dependencies: bool = False) -> None:
        name = local_name(node.tag)
        if name == "datasource-dependencies":
            inside_dependencies = True
        if not inside_dependencies:
            values = [node.text or ""]
            for attr_name, value in node.attrib.items():
                if attr_name.casefold() in {
                    "column",
                    "field",
                    "expression",
                    "sort",
                    "value",
                }:
                    values.append(value)
            for value in values:
                for match in FIELD_REFERENCE_RE.finditer(value):
                    if is_generated_measure_field(match.group(0)):
                        continue
                    key, warning = resolve_reference(match.group(0), fields)
                    if key:
                        resolved.add(key)
                        usage = FieldUsage(
                            field_key=key,
                            context=name,
                            aggregation=aggregation_from_reference(
                                match.group(0)
                            ),
                        )
                        if usage not in usages:
                            usages.append(usage)
                    elif warning and warning not in warnings:
                        warnings.append(warning)
        for child in node:
            visit(child, inside_dependencies)

    visit(worksheet_node)
    return resolved, warnings, usages


def extract_filter(
    node: ET.Element,
    fields: dict[str, Field],
) -> FilterInfo:
    reference = node.get("column") or node.get("field") or "Unknown"
    key, warning = resolve_reference(reference, fields)
    operator = node.get("filter-function") or node.get("class") or "Unknown"
    values: list[str] = []
    for child in node.iter():
        function = child.get("function")
        if function:
            operator = function
        member = child.get("member")
        if member is not None:
            clean = html.unescape(member).strip()
            if len(clean) >= 2 and clean[0] == clean[-1] == '"':
                clean = clean[1:-1]
            values.append(clean)
    return FilterInfo(
        field_key=key,
        field_label=fields[key].caption if key else reference,
        operator=operator,
        value=", ".join(values) if values else "Unknown",
        raw_reference=reference,
        warnings=[warning] if warning else [],
    )


def extract_worksheets(
    root: ET.Element,
    fields: dict[str, Field],
) -> list[Worksheet]:
    worksheets: list[Worksheet] = []
    for node in (item for item in root.iter() if local_name(item.tag) == "worksheet"):
        name = node.get("name") or "Unnamed Worksheet"
        direct, warnings, usages = references_from_element(node, fields)
        filters = [
            extract_filter(item, fields)
            for item in node.iter()
            if local_name(item.tag) == "filter"
            and not is_generated_measure_field(
                item.get("column") or item.get("field") or ""
            )
        ]
        direct.update(
            item.field_key for item in filters if item.field_key is not None
        )
        for filter_info in filters:
            warnings.extend(
                warning
                for warning in filter_info.warnings
                if warning not in warnings
            )
        worksheets.append(
            Worksheet(
                name=name,
                direct_fields=direct,
                field_usages=usages,
                filters=filters,
                warnings=warnings,
            )
        )
    return worksheets


def extract_dashboards(
    root: ET.Element,
    worksheets: list[Worksheet],
) -> list[Dashboard]:
    worksheet_names = {worksheet.name for worksheet in worksheets}
    dashboards: list[Dashboard] = []
    for node in (
        item for item in root.iter() if local_name(item.tag) == "dashboard"
    ):
        name = node.get("name")
        if not name:
            continue
        referenced: list[str] = []
        for zone in (
            item for item in node.iter() if local_name(item.tag) == "zone"
        ):
            worksheet_name = zone.get("name") or zone.get("worksheet")
            if (
                worksheet_name in worksheet_names
                and worksheet_name not in referenced
            ):
                referenced.append(worksheet_name)
        dashboards.append(Dashboard(name=name, worksheets=referenced))
    return dashboards


def parse_workbook(path: Path) -> Workbook:
    root = parse_xml(read_workbook_xml(path), path)
    fields = build_field_catalog(root)
    resolve_formulas(fields)
    worksheets = extract_worksheets(root, fields)
    dashboards = extract_dashboards(root, worksheets)
    name = root.get("name") or path.stem
    workbook = Workbook(
        name=name,
        source=path.name,
        fields=fields,
        worksheets=worksheets,
        dashboards=dashboards,
    )
    detect_cycles(workbook, relevant_fields(workbook))
    return workbook


def dependency_closure(
    starts: Iterable[str],
    fields: dict[str, Field],
) -> set[str]:
    seen: set[str] = set()
    pending = list(starts)
    while pending:
        key = pending.pop()
        if key in seen or key not in fields:
            continue
        seen.add(key)
        pending.extend(fields[key].dependencies)
    return seen


def relevant_fields(workbook: Workbook) -> set[str]:
    return dependency_closure(
        (key for worksheet in workbook.worksheets for key in worksheet.direct_fields),
        workbook.fields,
    )


def detect_cycles(workbook: Workbook, included: set[str]) -> None:
    """Record relevant calculated-field dependency cycles."""
    state: dict[str, int] = {}
    stack: list[str] = []
    recorded: set[frozenset[str]] = set()

    def visit(key: str) -> None:
        state[key] = 1
        stack.append(key)
        for dependency in workbook.fields[key].dependencies:
            if dependency not in included:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycle = [*stack[start:], dependency]
                identity = frozenset(cycle)
                if identity in recorded:
                    continue
                recorded.add(identity)
                labels = " → ".join(
                    workbook.fields[item].caption for item in cycle
                )
                warning = f"Circular dependency detected: {labels}"
                workbook.warnings.append(warning)
                for cycle_key in identity:
                    workbook.fields[cycle_key].warnings.append(warning)
        stack.pop()
        state[key] = 2

    for key in sorted(included):
        if state.get(key, 0) == 0:
            visit(key)


def unique_slugs(labels: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Return stable, collision-safe slugs keyed by entity key."""
    result: dict[str, str] = {}
    used: dict[str, int] = defaultdict(int)
    for key, label in sorted(labels, key=lambda item: (item[1].casefold(), item[0])):
        base = slugify(label)
        used[base] += 1
        result[key] = base if used[base] == 1 else f"{base}-{used[base]}"
    return result


def worksheet_relevant_fields(
    worksheet: Worksheet,
    fields: dict[str, Field],
) -> set[str]:
    return dependency_closure(worksheet.direct_fields, fields)


def metric_calculation_scope(item: Field) -> str:
    classification = formula_classification(
        item.raw_formula, item.is_parameter
    ).casefold()
    if classification == "aggregate calculation":
        return "aggregate"
    if classification == "lod":
        return "lod"
    if classification == "table calculation":
        return "table_calculation"
    if item.raw_formula is not None:
        return "row_level"
    return "base_measure"


def _entity(
    entity_id: str,
    entity_type: str,
    name: str,
    source_id: str,
    tableau_object_type: str,
    tableau_name: str,
    attributes: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": entity_id,
        "type": entity_type,
        "name": name,
        "provenance": {
            "source_id": source_id,
            "tableau_object_type": tableau_object_type,
            "tableau_name": tableau_name,
        },
        "attributes": attributes or {},
    }


def _relation(
    source: str,
    relation_type: str,
    target: str,
    *,
    direct: bool = True,
) -> dict[str, object]:
    return {
        "from": source,
        "type": relation_type,
        "to": target,
        "evidence": {"source": "tableau", "direct": direct},
    }


def normalized_knowledge_payload(workbooks: list[Workbook]) -> dict[str, object]:
    """Build a deterministic, presentation-independent Tableau source model."""
    source_slugs = unique_slugs(
        (str(index), workbook.name) for index, workbook in enumerate(workbooks)
    )
    sources: list[dict[str, object]] = []
    entities: list[dict[str, object]] = []
    relations: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    published_fields: dict[tuple[str, str], list[str]] = defaultdict(list)

    for workbook_index, workbook in enumerate(workbooks):
        workbook_key = str(workbook_index)
        workbook_slug = source_slugs[workbook_key]
        source_id = f"tableau-workbook:{workbook_slug}"
        sources.append(
            {
                "id": source_id,
                "type": "tableau_workbook",
                "name": workbook.name,
                "file": workbook.source,
            }
        )
        for warning in workbook.warnings:
            warnings.append({"source_id": source_id, "message": warning})

        included = relevant_fields(workbook)
        datasource_names = sorted(
            {workbook.fields[key].datasource for key in included}, key=str.casefold
        )
        datasource_slugs = unique_slugs(
            (
                name,
                next(
                    field.datasource_caption
                    for field in workbook.fields.values()
                    if field.datasource == name
                ),
            )
            for name in datasource_names
        )
        datasource_ids = {
            name: f"datasource:{workbook_slug}:{datasource_slugs[name]}"
            for name in datasource_names
        }
        for name in datasource_names:
            representative = next(
                field for field in workbook.fields.values() if field.datasource == name
            )
            entities.append(
                _entity(
                    datasource_ids[name],
                    "datasource",
                    representative.datasource_caption,
                    source_id,
                    "datasource",
                    name,
                    {
                        "internal_name": name,
                        "published_identity": representative.datasource_identity,
                    },
                )
            )

        field_slugs = unique_slugs(
            (key, workbook.fields[key].caption) for key in included
        )
        field_ids = {
            key: f"field:{workbook_slug}:{field_slugs[key]}" for key in included
        }
        calculation_ids = {
            key: f"calculation:{workbook_slug}:{field_slugs[key]}"
            for key in included
            if workbook.fields[key].raw_formula is not None
            and not workbook.fields[key].is_parameter
        }
        metric_ids = {
            key: f"metric:{workbook_slug}:{field_slugs[key]}"
            for key in included
            if not workbook.fields[key].is_parameter
            and (
                (workbook.fields[key].role or "").casefold() == "measure"
                or (
                    workbook.fields[key].raw_formula is not None
                    and (workbook.fields[key].role or "").casefold()
                    != "dimension"
                )
            )
        }

        for key in sorted(included, key=lambda value: field_ids[value]):
            item = workbook.fields[key]
            entities.append(
                _entity(
                    field_ids[key],
                    "field",
                    item.caption,
                    source_id,
                    "column",
                    item.internal_name,
                    {
                        "internal_name": item.internal_name,
                        "datatype": item.datatype,
                        "role": item.role,
                        "field_type": item.field_type,
                        "classification": formula_classification(
                            item.raw_formula, item.is_parameter
                        ),
                        "resolution_status": (
                            "warning" if item.warnings else "resolved"
                        ),
                        "warnings": item.warnings,
                    },
                )
            )
            relations.append(
                _relation(field_ids[key], "comes_from", datasource_ids[item.datasource])
            )
            if item.datasource_identity:
                published_fields[
                    (item.datasource_identity, item.internal_name.casefold())
                ].append(field_ids[key])

            if key in calculation_ids:
                calculation_id = calculation_ids[key]
                entities.append(
                    _entity(
                        calculation_id,
                        "calculation",
                        item.caption,
                        source_id,
                        "calculation",
                        item.internal_name,
                        {
                            "formula_tableau": item.raw_formula,
                            "formula_display": item.display_formula,
                            "classification": formula_classification(
                                item.raw_formula, item.is_parameter
                            ),
                            "warnings": item.warnings,
                        },
                    )
                )
                relations.append(
                    _relation(field_ids[key], "calculated_by", calculation_id)
                )
                for dependency in item.dependencies:
                    if dependency not in included:
                        continue
                    target = calculation_ids.get(dependency, field_ids[dependency])
                    relations.append(
                        _relation(calculation_id, "depends_on", target)
                    )

            if key in metric_ids:
                metric_id = metric_ids[key]
                entities.append(
                    _entity(
                        metric_id,
                        "metric",
                        item.caption,
                        source_id,
                        "column",
                        item.internal_name,
                        {
                            "semantic_status": "inferred",
                            "calculation_scope": metric_calculation_scope(item),
                        },
                    )
                )
                if key in calculation_ids:
                    relations.append(
                        _relation(metric_id, "calculated_by", calculation_ids[key])
                    )
                    for dependency in item.dependencies:
                        if dependency in included:
                            relations.append(
                                _relation(
                                    metric_id,
                                    "depends_on",
                                    calculation_ids.get(
                                        dependency, field_ids[dependency]
                                    ),
                                )
                            )
                else:
                    relations.append(
                        _relation(metric_id, "depends_on", field_ids[key])
                    )

        worksheet_slugs = unique_slugs(
            (worksheet.name, worksheet.name) for worksheet in workbook.worksheets
        )
        visual_ids = {
            worksheet.name: f"visual:{workbook_slug}:{worksheet_slugs[worksheet.name]}"
            for worksheet in workbook.worksheets
        }
        dashboard_slugs = unique_slugs(
            (dashboard.name, dashboard.name) for dashboard in workbook.dashboards
        )
        dashboard_ids = {
            dashboard.name: f"dashboard:{workbook_slug}:{dashboard_slugs[dashboard.name]}"
            for dashboard in workbook.dashboards
        }

        for dashboard in sorted(
            workbook.dashboards, key=lambda value: value.name.casefold()
        ):
            dashboard_id = dashboard_ids[dashboard.name]
            entities.append(
                _entity(
                    dashboard_id,
                    "dashboard",
                    dashboard.name,
                    source_id,
                    "dashboard",
                    dashboard.name,
                )
            )
            dashboard_datasources: set[str] = set()
            for worksheet_name in dashboard.worksheets:
                visual_id = visual_ids.get(worksheet_name)
                if visual_id is None:
                    continue
                relations.append(_relation(dashboard_id, "contains", visual_id))
                worksheet = next(
                    item for item in workbook.worksheets if item.name == worksheet_name
                )
                for field_key_value in worksheet_relevant_fields(
                    worksheet, workbook.fields
                ):
                    if field_key_value in included:
                        dashboard_datasources.add(
                            datasource_ids[workbook.fields[field_key_value].datasource]
                        )
            for datasource_id in sorted(dashboard_datasources):
                relations.append(
                    _relation(dashboard_id, "depends_on", datasource_id, direct=False)
                )

        for worksheet in sorted(
            workbook.worksheets, key=lambda value: value.name.casefold()
        ):
            visual_id = visual_ids[worksheet.name]
            entities.append(
                _entity(
                    visual_id,
                    "visual",
                    worksheet.name,
                    source_id,
                    "worksheet",
                    worksheet.name,
                    {"warnings": worksheet.warnings},
                )
            )
            displayed_keys = {
                usage.field_key
                for usage in worksheet.field_usages
                if usage.context not in {"filter", "groupfilter"}
            }
            for key in sorted(worksheet.direct_fields):
                if key not in included:
                    continue
                relations.append(_relation(visual_id, "uses", field_ids[key]))
                if key in calculation_ids:
                    relations.append(
                        _relation(visual_id, "uses", calculation_ids[key])
                    )
                if key in metric_ids and key in displayed_keys:
                    relations.append(
                        _relation(visual_id, "displays", metric_ids[key])
                    )

            filter_labels = [
                f"{item.field_label}-{index}"
                for index, item in enumerate(worksheet.filters, start=1)
            ]
            filter_slugs = unique_slugs(
                (str(index), label) for index, label in enumerate(filter_labels)
            )
            for index, item in enumerate(worksheet.filters):
                filter_id = (
                    f"filter:{workbook_slug}:{worksheet_slugs[worksheet.name]}:"
                    f"{filter_slugs[str(index)]}"
                )
                entities.append(
                    _entity(
                        filter_id,
                        "filter",
                        item.field_label,
                        source_id,
                        "filter",
                        item.raw_reference,
                        {"operator": item.operator, "value": item.value},
                    )
                )
                relations.append(_relation(visual_id, "affected_by", filter_id))
                if item.field_key in field_ids:
                    relations.append(
                        _relation(filter_id, "filters_on", field_ids[item.field_key])
                    )

    for matching_fields in published_fields.values():
        ordered = sorted(set(matching_fields))
        for index, source in enumerate(ordered):
            for target in ordered[index + 1 :]:
                relations.append(
                    _relation(source, "same_source_field_as", target, direct=False)
                )

    entities.sort(key=lambda item: str(item["id"]))
    relations = sorted(
        {
            (str(item["from"]), str(item["type"]), str(item["to"])): item
            for item in relations
        }.values(),
        key=lambda item: (str(item["from"]), str(item["type"]), str(item["to"])),
    )
    warnings.sort(key=lambda item: (str(item["source_id"]), str(item["message"])))
    return {
        "schema_version": SCHEMA_VERSION,
        "source_type": "tableau",
        "sources": sources,
        "entities": entities,
        "relations": relations,
        "warnings": warnings,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract normalized semantic metadata from Tableau workbooks."
    )
    parser.add_argument("input", type=Path, help=".twb/.twbx file or directory")
    parser.add_argument("--output", type=Path, required=True, help="output directory")
    parser.add_argument("--workbook", help="only process this workbook name")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="stop at the first workbook extraction error",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    files = iter_source_files(args.input)
    if not files:
        raise CatalogError(f"No .twb or .twbx files found in {args.input}")
    args.output.mkdir(parents=True, exist_ok=True)
    workbooks: list[Workbook] = []
    failures: list[str] = []
    for source in files:
        try:
            workbook = parse_workbook(source)
            if args.workbook and workbook.name != args.workbook:
                continue
            workbooks.append(workbook)
        except (CatalogError, OSError, zipfile.BadZipFile) as exc:
            if args.strict:
                raise
            failures.append(f"{source.name}: {exc}")
    if workbooks:
        sources_dir = args.output / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        output_path = sources_dir / "tableau.json"
        temporary_path = sources_dir / ".tableau.json.tmp"
        temporary_path.write_text(
            json.dumps(
                normalized_knowledge_payload(workbooks),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(output_path)
    print(f"Processed workbooks: {len(workbooks)}")
    print(f"Output: {(args.output / 'sources' / 'tableau.json').resolve()}")
    if failures:
        print("Warnings:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
    return 0 if workbooks else 1


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (CatalogError, OSError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
