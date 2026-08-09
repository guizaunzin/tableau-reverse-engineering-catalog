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
DISPLAY_CONTEXTS = {
    "angle",
    "color",
    "cols",
    "lod",
    "rows",
    "shape",
    "size",
    "text",
    "tooltip",
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
    layout: dict[str, object] | None = None


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


def is_dummy_field(item: Field) -> bool:
    """Return whether a Tableau field is a non-semantic Dummy helper."""
    return bool(
        re.search(
            r"\bdummy\b",
            f"{item.caption} {unbracket(item.internal_name)}",
            flags=re.IGNORECASE,
        )
    )


def is_ignored_filter(item: FilterInfo, fields: dict[str, Field]) -> bool:
    """Exclude Tableau action filters and filters backed by Dummy fields."""
    if re.search(r"\[Action(?:\s|\()", item.raw_reference, flags=re.IGNORECASE):
        return True
    if item.field_key is not None and is_dummy_field(fields[item.field_key]):
        return True
    return bool(re.search(r"\bdummy\b", item.field_label, flags=re.IGNORECASE))


def filter_signature(item: FilterInfo) -> tuple[str, str, str]:
    """Return the workbook-level semantic identity of a Tableau filter."""
    field_identity = item.field_key or item.raw_reference.casefold()
    return (field_identity, item.operator.casefold(), item.value)


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
        dashboards.append(
            Dashboard(
                name=name,
                worksheets=referenced,
                layout=extract_dashboard_layout(node, worksheet_names),
            )
        )
    return dashboards


def _direct_child(node: ET.Element, tag_name: str) -> ET.Element | None:
    return next(
        (child for child in node if local_name(child.tag) == tag_name), None
    )


def _layout_coordinates(zone: ET.Element) -> dict[str, int] | None:
    names = {"x": "x", "y": "y", "width": "w", "height": "h"}
    values: dict[str, int] = {}
    try:
        for output_name, xml_name in names.items():
            raw_value = zone.get(xml_name)
            if raw_value is None:
                return None
            values[output_name] = int(raw_value)
    except ValueError:
        return None
    if (
        values["x"] < 0
        or values["y"] < 0
        or values["width"] <= 0
        or values["height"] <= 0
        or any(value > 100000 for value in values.values())
    ):
        return None
    return values


def _zone_text(zone: ET.Element) -> str:
    fragments = [
        (item.text or "").strip()
        for item in zone.iter()
        if local_name(item.tag) == "run" and (item.text or "").strip()
    ]
    return " ".join(fragments)


def extract_dashboard_layout(
    dashboard: ET.Element,
    worksheet_names: set[str],
) -> dict[str, object]:
    """Extract a conservative, presentation-independent dashboard wireframe."""
    size = _direct_child(dashboard, "size")
    warnings: list[str] = []
    if size is None:
        return {
            "status": "unavailable",
            "warnings": ["Dashboard has no fixed desktop size metadata."],
        }
    sizing_mode = size.get("sizing-mode") or "unknown"
    try:
        min_width = int(size.get("minwidth") or "")
        max_width = int(size.get("maxwidth") or "")
        min_height = int(size.get("minheight") or "")
        max_height = int(size.get("maxheight") or "")
    except ValueError:
        min_width = max_width = min_height = max_height = 0
    if (
        sizing_mode != "fixed"
        or min_width <= 0
        or min_height <= 0
        or min_width != max_width
        or min_height != max_height
    ):
        return {
            "status": "unavailable",
            "sizing_mode": sizing_mode,
            "warnings": ["Dashboard does not have a valid fixed desktop size."],
        }

    items: list[dict[str, object]] = []
    drawable_order = 0
    auxiliary_types = {
        "text": "text",
        "color": "control",
        "paramctrl": "parameter_control",
        "dashboard-object": "navigation",
    }
    for zone in (
        item for item in dashboard.iter() if local_name(item.tag) == "zone"
    ):
        zone_type = zone.get("type-v2") or ""
        worksheet_name = zone.get("name") or zone.get("worksheet")
        kind: str | None = None
        item: dict[str, object] = {}
        if worksheet_name in worksheet_names and zone_type not in auxiliary_types:
            kind = "visual"
            item["worksheet_name"] = worksheet_name
        elif zone_type in auxiliary_types:
            kind = auxiliary_types[zone_type]
            label = _zone_text(zone) or zone.get("name") or zone.get("param")
            if label:
                item["label"] = label
        elif worksheet_name and not zone_type:
            warnings.append(
                f"Skipped zone {zone.get('id') or '?'} with unresolved worksheet "
                f"{worksheet_name!r}."
            )
            continue
        else:
            continue

        coordinates = _layout_coordinates(zone)
        if coordinates is None:
            warnings.append(
                f"Skipped {kind} zone {zone.get('id') or '?'} with missing or invalid coordinates."
            )
            continue
        drawable_order += 1
        item = {
            "document_order": drawable_order,
            "tableau_zone_id": zone.get("id") or "",
            "kind": kind,
            **item,
            **coordinates,
            "hidden": (zone.get("hidden-by-user") or "").casefold() == "true",
        }
        items.append(item)

    return {
        "status": "partial" if warnings else "complete",
        "sizing_mode": sizing_mode,
        "dashboard_width": min_width,
        "dashboard_height": min_height,
        "coordinate_space": {"width": 100000, "height": 100000},
        "items": items,
        "warnings": warnings,
    }


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


def _rule_expression(value: str) -> str:
    compact = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    compact = re.sub(r"\[([^\]]+)\]", r"\1", compact)
    compact = re.sub(r"\s*=\s*TRUE\b", " is true", compact, flags=re.IGNORECASE)
    return compact.strip()


def inferred_rule_attributes(item: Field) -> dict[str, object] | None:
    """Return a conservative deterministic rule candidate for a metric field."""
    formula = item.raw_formula
    if not formula:
        return None
    name_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", item.caption.casefold())
        if token
    }
    presentation_tokens = {
        "color",
        "dummy",
        "hide",
        "label",
        "show",
        "sign",
        "title",
        "tooltip",
    }
    upper_formula = formula.upper()
    if (
        name_tokens.intersection(presentation_tokens)
        or re.search(r"[+-]\s*$", item.caption)
        or "STR(" in upper_formula
        or "▲" in formula
        or "▼" in formula
    ):
        return None

    display_formula = item.display_formula or formula
    conditional = re.search(
        r"\bIF\s+(.+?)\s+THEN\s+(.+?)\s+END\b",
        display_formula,
        flags=re.IGNORECASE | re.DOTALL,
    )
    aggregate = re.search(
        r"\b(COUNTD|COUNT|SUM|AVG|MIN|MAX)\s*\(\s*IF\b",
        display_formula,
        flags=re.IGNORECASE,
    )
    is_lod = "{" in formula and "}" in formula
    is_case = bool(re.search(r"\bCASE\b", formula, flags=re.IGNORECASE))
    if conditional is not None and aggregate is None and re.search(
        r"\bELSE\b", formula, flags=re.IGNORECASE
    ):
        return None
    if conditional is None and not is_lod and not is_case:
        return None

    date_logic = bool(
        re.search(
            r"\b(YTD|LYTD|DATE|DATEADD|DATEDIFF|DATETRUNC|TODAY|YEAR|MONTH|WEEK)\b",
            f"{item.caption} {formula}",
            flags=re.IGNORECASE,
        )
    )
    if date_logic:
        rule_kind = "time_window"
    elif is_lod:
        rule_kind = "aggregation_scope"
    elif aggregate:
        rule_kind = "conditional_aggregation"
    else:
        rule_kind = "inclusion_exclusion"

    if conditional is not None:
        condition = _rule_expression(conditional.group(1))
        result = _rule_expression(conditional.group(2))
        if aggregate:
            operation = aggregate.group(1).upper()
            verb = {
                "COUNTD": "counts distinct",
                "COUNT": "counts",
                "SUM": "sums",
                "AVG": "averages",
                "MIN": "takes the minimum of",
                "MAX": "takes the maximum of",
            }[operation]
            statement = f"{item.caption} {verb} {result} only when {condition}."
        else:
            statement = f"{item.caption} applies {result} only when {condition}."
    elif is_lod:
        statement = (
            f"{item.caption} uses a Tableau level-of-detail expression to define "
            "its aggregation scope."
        )
    else:
        statement = f"{item.caption} selects its result using a CASE condition."

    field_mentions = {item.caption}
    for match in FIELD_REFERENCE_RE.finditer(display_formula):
        _, final_token = reference_parts(match.group(0))
        field_mentions.add(unbracket(final_token))

    return {
        "semantic_status": "inferred",
        "confidence": "high" if aggregate or is_lod else "medium",
        "rule_kind": rule_kind,
        "statement": statement,
        "field_mentions": sorted(field_mentions, key=str.casefold),
        "inference_method": "deterministic_tableau_formula_v1",
        "formula_tableau": formula,
    }


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

        included = {
            key
            for key in relevant_fields(workbook)
            if not is_dummy_field(workbook.fields[key])
        }
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

        worksheet_entries = list(enumerate(workbook.worksheets))
        worksheet_slugs = unique_slugs(
            (str(index), worksheet.name)
            for index, worksheet in worksheet_entries
        )
        visual_ids = {
            index: f"visual:{workbook_slug}:{worksheet_slugs[str(index)]}"
            for index, _ in worksheet_entries
        }
        worksheet_indices_by_name: dict[str, list[int]] = defaultdict(list)
        for index, worksheet in worksheet_entries:
            worksheet_indices_by_name[worksheet.name].append(index)

        semantic_filters: dict[tuple[str, str, str], FilterInfo] = {}
        for _, worksheet in worksheet_entries:
            for item in worksheet.filters:
                if is_ignored_filter(item, workbook.fields):
                    continue
                semantic_filters.setdefault(filter_signature(item), item)
        filter_slugs = unique_slugs(
            (
                json.dumps(signature, ensure_ascii=False),
                item.field_label,
            )
            for signature, item in semantic_filters.items()
        )
        filter_ids = {
            signature: (
                f"filter:{workbook_slug}:"
                f"{filter_slugs[json.dumps(signature, ensure_ascii=False)]}"
            )
            for signature in semantic_filters
        }
        emitted_filters: set[tuple[str, str, str]] = set()

        dashboard_entries = list(enumerate(workbook.dashboards))
        dashboard_slugs = unique_slugs(
            (str(index), dashboard.name)
            for index, dashboard in dashboard_entries
        )
        dashboard_ids = {
            index: f"dashboard:{workbook_slug}:{dashboard_slugs[str(index)]}"
            for index, _ in dashboard_entries
        }

        for dashboard_index, dashboard in sorted(
            dashboard_entries,
            key=lambda value: (value[1].name.casefold(), value[0]),
        ):
            dashboard_id = dashboard_ids[dashboard_index]
            normalized_layout: dict[str, object] | None = None
            if dashboard.layout is not None:
                normalized_layout = {
                    key: value
                    for key, value in dashboard.layout.items()
                    if key not in {"items", "warnings"}
                }
                layout_warnings = list(dashboard.layout.get("warnings", []))
                layout_items: list[dict[str, object]] = []
                for raw_item in dashboard.layout.get("items", []):
                    if not isinstance(raw_item, dict):
                        continue
                    item = dict(raw_item)
                    worksheet_name = item.pop("worksheet_name", None)
                    if worksheet_name is not None:
                        matching_indices = worksheet_indices_by_name.get(
                            str(worksheet_name), []
                        )
                        if len(matching_indices) != 1:
                            layout_warnings.append(
                                f"Skipped layout zone with ambiguous worksheet {worksheet_name!r}."
                            )
                            continue
                        item["visual_id"] = visual_ids[matching_indices[0]]
                    layout_items.append(item)
                if layout_warnings and normalized_layout.get("status") == "complete":
                    normalized_layout["status"] = "partial"
                normalized_layout["items"] = layout_items
                normalized_layout["warnings"] = layout_warnings
            entities.append(
                _entity(
                    dashboard_id,
                    "dashboard",
                    dashboard.name,
                    source_id,
                    "dashboard",
                    dashboard.name,
                    {"layout": normalized_layout} if normalized_layout else None,
                )
            )
            dashboard_datasources: set[str] = set()
            for worksheet_name in dashboard.worksheets:
                matching_indices = worksheet_indices_by_name.get(
                    worksheet_name, []
                )
                if len(matching_indices) != 1:
                    if matching_indices:
                        warnings.append(
                            {
                                "source_id": source_id,
                                "message": (
                                    f"Dashboard {dashboard.name!r} references "
                                    f"ambiguous worksheet name {worksheet_name!r}."
                                ),
                            }
                        )
                    continue
                worksheet_index = matching_indices[0]
                visual_id = visual_ids[worksheet_index]
                relations.append(_relation(dashboard_id, "contains", visual_id))
                worksheet = workbook.worksheets[worksheet_index]
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

        displayed_metric_keys: set[str] = set()
        for worksheet_index, worksheet in sorted(
            worksheet_entries,
            key=lambda value: (value[1].name.casefold(), value[0]),
        ):
            visual_id = visual_ids[worksheet_index]
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
                if usage.context in DISPLAY_CONTEXTS
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
                    displayed_metric_keys.add(key)

            for item in worksheet.filters:
                if is_ignored_filter(item, workbook.fields):
                    continue
                signature = filter_signature(item)
                filter_id = filter_ids[signature]
                if signature not in emitted_filters:
                    emitted_filters.add(signature)
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

        for key in sorted(displayed_metric_keys, key=lambda value: metric_ids[value]):
            item = workbook.fields[key]
            attributes = inferred_rule_attributes(item)
            if attributes is None or key not in calculation_ids:
                continue
            calculation_id = calculation_ids[key]
            rule_id = (
                f"business-rule:{workbook_slug}:{field_slugs[key]}-inferred"
            )
            attributes["evidence_calculation_ids"] = [calculation_id]
            entities.append(
                _entity(
                    rule_id,
                    "business_rule",
                    f"{item.caption} rule",
                    source_id,
                    "inferred_business_rule",
                    item.internal_name,
                    attributes,
                )
            )
            relations.append(
                _relation(metric_ids[key], "affected_by", rule_id, direct=False)
            )
            relations.append(
                _relation(rule_id, "implemented_by", calculation_id, direct=False)
            )
            for dependency in item.dependencies:
                if dependency not in included:
                    continue
                relations.append(
                    _relation(
                        rule_id,
                        "depends_on",
                        calculation_ids.get(dependency, field_ids[dependency]),
                        direct=False,
                    )
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


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def tableau_source_filename(source_id: str) -> str:
    prefix = "tableau-workbook:"
    identifier = source_id[len(prefix) :] if source_id.startswith(prefix) else source_id
    return f"{slugify(identifier, 'workbook')}.json"


def migrate_legacy_tableau_source(output_root: Path) -> None:
    """Split the former aggregate Tableau source without losing other workbooks."""
    legacy_path = output_root / "sources" / "tableau.json"
    if not legacy_path.exists():
        return
    try:
        payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot migrate legacy Tableau source: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("source_type") != "tableau"
    ):
        raise CatalogError(
            f"Cannot migrate incompatible legacy Tableau source: {legacy_path}"
        )

    entities = [
        item for item in payload.get("entities", []) if isinstance(item, dict)
    ]
    relations = [
        item for item in payload.get("relations", []) if isinstance(item, dict)
    ]
    warnings = [
        item for item in payload.get("warnings", []) if isinstance(item, dict)
    ]
    tableau_dir = output_root / "sources" / "tableau"
    for source in payload.get("sources", []):
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise CatalogError(
                f"Cannot migrate malformed Tableau source entry: {legacy_path}"
            )
        source_id = str(source["id"])
        source_entities = [
            item
            for item in entities
            if item.get("provenance", {}).get("source_id") == source_id
        ]
        source_entity_ids = {str(item["id"]) for item in source_entities}
        source_relations = [
            item
            for item in relations
            if item.get("from") in source_entity_ids
            and item.get("to") in source_entity_ids
        ]
        source_warnings = [
            item for item in warnings if item.get("source_id") == source_id
        ]
        target = tableau_dir / tableau_source_filename(source_id)
        if not target.exists():
            write_json_atomic(
                target,
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_type": "tableau",
                    "sources": [source],
                    "entities": source_entities,
                    "relations": source_relations,
                    "warnings": source_warnings,
                },
            )
    legacy_path.unlink()


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
        migrate_legacy_tableau_source(args.output)
        tableau_dir = args.output / "sources" / "tableau"
        for workbook in workbooks:
            payload = normalized_knowledge_payload([workbook])
            source_id = str(payload["sources"][0]["id"])
            write_json_atomic(
                tableau_dir / tableau_source_filename(source_id), payload
            )
    print(f"Processed workbooks: {len(workbooks)}")
    print(f"Output: {(args.output / 'sources' / 'tableau').resolve()}")
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
