#!/usr/bin/env python3
"""Generate a small reverse-engineering catalog from Tableau workbooks.

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
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import quote
from xml.etree import ElementTree as ET


SCHEMA_VERSION = 1
MAX_TWB_BYTES = 50 * 1024 * 1024
FIELD_REFERENCE_RE = re.compile(
    r"(?:\[[^\]\r\n]+\]\.)?\[[^\]\r\n]+\]"
)
SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
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


@dataclass
class Worksheet:
    name: str
    direct_fields: set[str] = field(default_factory=set)
    filters: list[FilterInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Workbook:
    name: str
    source: str
    fields: dict[str, Field]
    worksheets: list[Worksheet]
    warnings: list[str] = field(default_factory=list)


def local_name(tag: str) -> str:
    """Return an XML tag without its namespace."""
    return tag.rsplit("}", 1)[-1].lower()


def slugify(value: str, fallback: str = "item") -> str:
    slug = SAFE_SLUG_RE.sub("-", value.casefold()).strip("-")
    return slug or fallback


def workbook_filename_stem(value: str) -> str:
    """Create a readable workbook filename stem portable across platforms."""
    filename = INVALID_FILENAME_RE.sub("-", value).strip().rstrip(". ")
    if not filename:
        filename = "Workbook"
    if filename.casefold() in {
        "aux",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "con",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
        "nul",
        "prn",
    }:
        filename = f"_{filename}"
    return filename[:160]


def workbook_markdown_filename(value: str) -> str:
    return f"{workbook_filename_stem(value)}.md"


def worksheets_markdown_filename(value: str) -> str:
    return f"{workbook_filename_stem(value)} - Worksheets.md"


def field_impact_markdown_filename(value: str) -> str:
    return f"{workbook_filename_stem(value)} - Field Impact.md"


def workbook_json_filename(value: str) -> str:
    return f"{workbook_filename_stem(value)}.json"


def markdown_escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def mermaid_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


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
) -> tuple[set[str], list[str]]:
    resolved: set[str] = set()
    warnings: list[str] = []

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
                    elif warning and warning not in warnings:
                        warnings.append(warning)
        for child in node:
            visit(child, inside_dependencies)

    visit(worksheet_node)
    return resolved, warnings


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
        direct, warnings = references_from_element(node, fields)
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
                filters=filters,
                warnings=warnings,
            )
        )
    return worksheets


def parse_workbook(path: Path) -> Workbook:
    root = parse_xml(read_workbook_xml(path), path)
    fields = build_field_catalog(root)
    resolve_formulas(fields)
    worksheets = extract_worksheets(root, fields)
    name = root.get("name") or path.stem
    workbook = Workbook(
        name=name,
        source=path.name,
        fields=fields,
        worksheets=worksheets,
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


def reverse_dependencies(
    fields: dict[str, Field],
    included: set[str],
) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = defaultdict(set)
    for key in included:
        for dependency in fields[key].dependencies:
            if dependency in included:
                reverse[dependency].add(key)
    return reverse


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


def shortest_impact_path(
    start: str,
    worksheet: Worksheet,
    reverse: dict[str, set[str]],
) -> list[str] | None:
    if start in worksheet.direct_fields:
        return [start]
    queue: deque[list[str]] = deque([[start]])
    visited = {start}
    while queue:
        path = queue.popleft()
        for dependent in sorted(reverse.get(path[-1], set())):
            if dependent in visited:
                continue
            new_path = [*path, dependent]
            if dependent in worksheet.direct_fields:
                return new_path
            visited.add(dependent)
            queue.append(new_path)
    return None


def build_cross_workbook_impacts(
    workbooks: list[Workbook],
) -> dict[tuple[int, str], list[dict[str, object]]]:
    """Match fields across workbooks only with a stable published identity."""
    groups: dict[tuple[str, str], list[tuple[int, Workbook, str]]] = defaultdict(list)
    workbook_models: dict[int, tuple[set[str], dict[str, set[str]]]] = {}
    for index, workbook in enumerate(workbooks):
        included = relevant_fields(workbook)
        reverse = reverse_dependencies(workbook.fields, included)
        workbook_models[index] = (included, reverse)
        for key in included:
            item = workbook.fields[key]
            if item.datasource_identity:
                groups[
                    (item.datasource_identity, item.internal_name.casefold())
                ].append((index, workbook, key))

    result: dict[tuple[int, str], list[dict[str, object]]] = defaultdict(list)
    for occurrences in groups.values():
        if len({index for index, _, _ in occurrences}) < 2:
            continue
        for source_index, _, source_key in occurrences:
            for target_index, target_workbook, target_key in occurrences:
                if source_index == target_index:
                    continue
                _, target_reverse = workbook_models[target_index]
                for worksheet in sorted(
                    target_workbook.worksheets,
                    key=lambda value: value.name.casefold(),
                ):
                    path = shortest_impact_path(
                        target_key, worksheet, target_reverse
                    )
                    if path is None:
                        continue
                    result[(source_index, source_key)].append(
                        {
                            "workbook": target_workbook.name,
                            "worksheet": worksheet.name,
                            "impact": "direct" if len(path) == 1 else "indirect",
                            "path": [
                                target_workbook.fields[node].caption for node in path
                            ],
                        }
                    )
    for impacts in result.values():
        impacts.sort(
            key=lambda item: (
                str(item["workbook"]).casefold(),
                str(item["worksheet"]).casefold(),
                tuple(str(part).casefold() for part in item["path"]),
            )
        )
    return result


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


def render_mermaid(
    edges: list[tuple[str, str]],
    labels: dict[str, str],
    max_nodes: int = 30,
) -> tuple[list[str], bool]:
    all_nodes = {node for edge in edges for node in edge}
    truncated = len(all_nodes) > max_nodes
    selected_edges = edges
    if truncated:
        selected_edges = edges[:max_nodes]
        all_nodes = {node for edge in selected_edges for node in edge}
    lines = ["```mermaid", "flowchart LR"]
    node_ids = {
        key: f"N{index}"
        for index, key in enumerate(sorted(all_nodes), start=1)
    }
    for key in sorted(all_nodes):
        lines.append(
            f'    {node_ids[key]}["{mermaid_escape(labels.get(key, key))}"]'
        )
    for left, right in selected_edges:
        if left in node_ids and right in node_ids:
            lines.append(f"    {node_ids[left]} --> {node_ids[right]}")
    lines.append("```")
    return lines, truncated


def worksheet_page(
    workbook: Workbook,
    worksheet: Worksheet,
    field_slugs: dict[str, str],
    field_impact_filename: str,
) -> str:
    included = worksheet_relevant_fields(worksheet, workbook.fields)
    calculations = sorted(
        (
            workbook.fields[key]
            for key in included
            if workbook.fields[key].raw_formula is not None
            and not workbook.fields[key].is_parameter
        ),
        key=lambda item: item.caption.casefold(),
    )
    worksheet_anchor = slugify(worksheet.name)
    lines = [
        f'<a id="{worksheet_anchor}"></a>',
        "",
        f"## {worksheet.name}",
        "",
        f"**Workbook:** {workbook.name}",
        "",
    ]
    lines.extend(["### Filters", ""])
    if worksheet.filters:
        lines.extend(["| Field | Operator | Value |", "|---|---|---|"])
        for item in worksheet.filters:
            lines.append(
                f"| {markdown_escape(item.field_label)} | "
                f"{markdown_escape(item.operator)} | {markdown_escape(item.value)} |"
            )
    else:
        lines.append("No worksheet filters detected.")
    lines.extend(["", "### Calculated Fields", ""])
    if not calculations:
        lines.extend(["No relevant calculated fields detected.", ""])
    for item in calculations:
        usage = "Directly by this worksheet" if item.key in worksheet.direct_fields else "Supporting dependency"
        lines.extend(
            [
                f"#### {item.caption}",
                "",
                "```tableau",
                item.display_formula or "",
                "```",
                "",
                "Depends on:",
                "",
            ]
        )
        if item.dependencies:
            for dependency in item.dependencies:
                target = workbook.fields[dependency]
                lines.append(
                    f"- [{target.caption}]"
                    f"({quote(field_impact_filename)}#{field_slugs[dependency]})"
                )
        else:
            lines.append("- None detected")
        lines.extend(["", f"Used: {usage}", ""])
    edges = [
        (dependency, item.key)
        for item in calculations
        for dependency in item.dependencies
        if dependency in included
    ]
    lines.extend(["### Dependency Graph", ""])
    if edges:
        graph, truncated = render_mermaid(
            edges,
            {key: workbook.fields[key].caption for key in included},
        )
        lines.extend(graph)
        if truncated:
            lines.extend(["", "> Graph reduced to keep it readable."])
    else:
        lines.append("No calculation dependency edges detected.")
    notes = list(worksheet.warnings)
    notes.extend(
        warning
        for key in included
        for warning in workbook.fields[key].warnings
        if warning not in notes
    )
    lines.extend(
        [
            "",
            "### Extraction Notes",
            "",
            "- Worksheet filters only.",
            "- Dashboard action filters were not analyzed.",
        ]
    )
    for warning in notes:
        lines.append(f"- Warning: {warning}")
    return "\n".join(lines).rstrip() + "\n"


def field_page(
    workbook: Workbook,
    key: str,
    included: set[str],
    reverse: dict[str, set[str]],
    worksheet_slugs: dict[str, str],
    field_slugs: dict[str, str],
    cross_impacts: list[dict[str, object]],
    worksheets_filename: str,
) -> str:
    item = workbook.fields[key]
    direct_worksheets = sorted(
        (sheet for sheet in workbook.worksheets if key in sheet.direct_fields),
        key=lambda sheet: sheet.name.casefold(),
    )
    impacts: list[tuple[Worksheet, str, list[str]]] = []
    for worksheet in sorted(workbook.worksheets, key=lambda sheet: sheet.name.casefold()):
        path = shortest_impact_path(key, worksheet, reverse)
        if path:
            impacts.append(
                (
                    worksheet,
                    "Direct" if len(path) == 1 else "Indirect",
                    path,
                )
            )
    dependent_calculations = sorted(
        reverse.get(key, set()),
        key=lambda dependency: workbook.fields[dependency].caption.casefold(),
    )
    lines = [
        f'<a id="{field_slugs[key]}"></a>',
        "",
        f"## {item.caption}",
        "",
        f"**Type:** {item.field_type}  ",
        f"**Datasource:** {item.datasource_caption}",
        "",
    ]
    if item.raw_formula is not None and not item.is_parameter:
        lines.extend(
            [
                "### Formula",
                "",
                "```tableau",
                item.display_formula or "",
                "```",
                "",
            ]
        )
    lines.extend(["### Direct Worksheet Usage", ""])
    if direct_worksheets:
        for worksheet in direct_worksheets:
            lines.append(
                f"- [{worksheet.name}]"
                f"({quote(worksheets_filename)}#{worksheet_slugs[worksheet.name]})"
            )
    else:
        lines.append("None.")
    lines.extend(["", "### Dependent Calculations", ""])
    if dependent_calculations:
        for dependent in dependent_calculations:
            target = workbook.fields[dependent]
            lines.append(f"- [{target.caption}](#{field_slugs[dependent]})")
    else:
        lines.append("None.")
    lines.extend(
        [
            "",
            "### Impacted Worksheets",
            "",
            "| Worksheet | Impact | Dependency Path |",
            "|---|---|---|",
        ]
    )
    if impacts:
        for worksheet, impact, path in impacts:
            path_text = " → ".join(workbook.fields[node].caption for node in path)
            lines.append(
                f"| [{markdown_escape(worksheet.name)}]"
                f"({quote(worksheets_filename)}#{worksheet_slugs[worksheet.name]}) | "
                f"{impact} | {markdown_escape(path_text)} |"
            )
    else:
        lines.append("| None | — | — |")
    graph_edges: list[tuple[str, str]] = []
    graph_labels = {field_key_: workbook.fields[field_key_].caption for field_key_ in included}
    queue = deque([key])
    seen = {key}
    while queue:
        current = queue.popleft()
        for dependent in sorted(reverse.get(current, set())):
            graph_edges.append((current, dependent))
            if dependent not in seen:
                seen.add(dependent)
                queue.append(dependent)
    for worksheet, _, path in impacts:
        terminal = path[-1]
        sheet_key = f"worksheet:{worksheet.name}"
        graph_labels[sheet_key] = worksheet.name
        graph_edges.append((terminal, sheet_key))
    lines.extend(["", "### Reverse Dependency Graph", ""])
    if graph_edges:
        graph, truncated = render_mermaid(graph_edges, graph_labels)
        lines.extend(graph)
        if truncated:
            lines.extend(["", "> Graph reduced to keep it readable."])
    else:
        lines.append("No reverse dependency edges detected.")
    if cross_impacts:
        lines.extend(
            [
                "",
                "### Cross-Workbook Impact",
                "",
                "| Workbook | Worksheet | Impact | Dependency Path |",
                "|---|---|---|---|",
            ]
        )
        for impact in cross_impacts:
            lines.append(
                f"| {markdown_escape(impact['workbook'])} | "
                f"{markdown_escape(impact['worksheet'])} | "
                f"{markdown_escape(impact['impact']).title()} | "
                f"{markdown_escape(' → '.join(impact['path']))} |"
            )
    if item.warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(f"- {warning}" for warning in item.warnings)
    lines.extend(["", "[↑ Back to top](#top)"])
    return "\n".join(lines).rstrip() + "\n"


def workbook_payload(
    workbook: Workbook,
    included: set[str],
    reverse: dict[str, set[str]],
    cross_impacts: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    worksheets_payload = []
    for worksheet in sorted(workbook.worksheets, key=lambda item: item.name.casefold()):
        worksheet_fields = worksheet_relevant_fields(worksheet, workbook.fields)
        worksheets_payload.append(
            {
                "name": worksheet.name,
                "direct_fields": sorted(
                    (workbook.fields[key].caption for key in worksheet.direct_fields),
                    key=str.casefold,
                ),
                "calculated_fields": sorted(
                    (
                        workbook.fields[key].caption
                        for key in worksheet_fields
                        if workbook.fields[key].raw_formula is not None
                        and not workbook.fields[key].is_parameter
                    ),
                    key=str.casefold,
                ),
                "filters": [
                    {
                        "field": item.field_label,
                        "operator": item.operator,
                        "value": item.value,
                    }
                    for item in worksheet.filters
                ],
                "warnings": worksheet.warnings,
            }
        )
    fields_payload = []
    for key in sorted(included, key=lambda value: workbook.fields[value].caption.casefold()):
        item = workbook.fields[key]
        impacts = []
        for worksheet in sorted(workbook.worksheets, key=lambda value: value.name.casefold()):
            path = shortest_impact_path(key, worksheet, reverse)
            if path:
                impacts.append(
                    {
                        "worksheet": worksheet.name,
                        "impact": "direct" if len(path) == 1 else "indirect",
                        "path": [workbook.fields[node].caption for node in path],
                    }
                )
        fields_payload.append(
            {
                "caption": item.caption,
                "internal_name": item.internal_name,
                "datasource": item.datasource,
                "datasource_identity": item.datasource_identity,
                "type": item.field_type,
                "classification": formula_classification(
                    item.raw_formula, item.is_parameter
                ),
                "raw_formula": item.raw_formula,
                "display_formula": item.display_formula,
                "dependencies": [
                    workbook.fields[dependency].caption
                    for dependency in item.dependencies
                    if dependency in included
                ],
                "dependents": [
                    workbook.fields[dependent].caption
                    for dependent in sorted(
                        reverse.get(key, set()),
                        key=lambda value: workbook.fields[value].caption.casefold(),
                    )
                ],
                "impacts": impacts,
                "cross_workbook_impacts": cross_impacts.get(key, []),
                "resolution_status": "warning" if item.warnings else "resolved",
                "warnings": item.warnings,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "workbook": workbook.name,
        "source": workbook.source,
        "worksheets": worksheets_payload,
        "fields": fields_payload,
        "warnings": workbook.warnings,
    }


def write_workbook_docs(
    workbook: Workbook,
    output_root: Path,
    emit_json: bool,
    cross_impacts: dict[str, list[dict[str, object]]] | None = None,
) -> tuple[Path, int, int]:
    cross_impacts = cross_impacts or {}
    included = relevant_fields(workbook)
    reverse = reverse_dependencies(workbook.fields, included)
    workbook_dir = output_root / slugify(workbook.name, slugify(Path(workbook.source).stem))
    workbook_dir.mkdir(parents=True, exist_ok=True)
    worksheets_filename = worksheets_markdown_filename(workbook.name)
    field_impact_filename = field_impact_markdown_filename(workbook.name)

    worksheet_slugs = unique_slugs((sheet.name, sheet.name) for sheet in workbook.worksheets)
    field_slugs = unique_slugs(
        (key, workbook.fields[key].caption) for key in included
    )

    ordered_worksheets = sorted(
        workbook.worksheets, key=lambda item: item.name.casefold()
    )
    worksheets_lines = [
        f"# {workbook.name} Worksheets",
        "",
        f"Workbook: **{workbook.name}**",
        "",
        "## Contents",
        "",
    ]
    for worksheet in ordered_worksheets:
        worksheets_lines.append(
            f"- [{worksheet.name}](#{worksheet_slugs[worksheet.name]})"
        )
    for worksheet in ordered_worksheets:
        worksheets_lines.extend(
            [
                "",
                "---",
                "",
                worksheet_page(
                    workbook,
                    worksheet,
                    field_slugs,
                    field_impact_filename,
                ).rstrip(),
            ]
        )
    (workbook_dir / worksheets_filename).write_text(
        "\n".join(worksheets_lines).rstrip() + "\n",
        encoding="utf-8",
    )

    ordered_fields = sorted(
        included, key=lambda value: workbook.fields[value].caption.casefold()
    )
    impact_lines = [
        '<a id="top"></a>',
        "",
        f"# {workbook.name} Field Impact",
        "",
        "| Field | Type | Direct Worksheets | Total Impacted Worksheets |",
        "|---|---|---:|---:|",
    ]
    for key in ordered_fields:
        item = workbook.fields[key]
        direct_count = sum(key in sheet.direct_fields for sheet in workbook.worksheets)
        impact_count = sum(
            shortest_impact_path(key, sheet, reverse) is not None
            for sheet in workbook.worksheets
        )
        impact_lines.append(
            f"| [{markdown_escape(item.caption)}](#{field_slugs[key]}) | "
            f"{item.field_type} | {direct_count} | {impact_count} |"
        )
    for key in ordered_fields:
        impact_lines.extend(
            [
                "",
                "---",
                "",
                field_page(
                    workbook,
                    key,
                    included,
                    reverse,
                    worksheet_slugs,
                    field_slugs,
                    cross_impacts.get(key, []),
                    worksheets_filename,
                ).rstrip(),
            ]
        )
    (workbook_dir / field_impact_filename).write_text(
        "\n".join(impact_lines).rstrip() + "\n",
        encoding="utf-8",
    )

    readme_lines = [
        f"# {workbook.name}",
        "",
        f"Source: `{workbook.source}`",
        "",
        f"- Worksheets: {len(workbook.worksheets)}",
        f"- Relevant fields: {len(included)}",
        f"- [Worksheets]({quote(worksheets_filename)})",
        f"- [Field Impact]({quote(field_impact_filename)})",
        "",
        "## Worksheets",
        "",
    ]
    for worksheet in sorted(workbook.worksheets, key=lambda item: item.name.casefold()):
        worksheet_fields = worksheet_relevant_fields(worksheet, workbook.fields)
        calc_count = sum(
            workbook.fields[key].raw_formula is not None
            and not workbook.fields[key].is_parameter
            for key in worksheet_fields
        )
        readme_lines.append(
            f"- [{worksheet.name}]"
            f"({quote(worksheets_filename)}#{worksheet_slugs[worksheet.name]}) "
            f"— {calc_count} calculations, {len(worksheet.filters)} filters"
        )
    (workbook_dir / workbook_markdown_filename(workbook.name)).write_text(
        "\n".join(readme_lines).rstrip() + "\n",
        encoding="utf-8",
    )
    if emit_json:
        (workbook_dir / workbook_json_filename(workbook.name)).write_text(
            json.dumps(
                workbook_payload(workbook, included, reverse, cross_impacts),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return workbook_dir, len(workbook.worksheets), len(included)


def write_root_index(
    output_root: Path,
    summaries: list[tuple[Workbook, Path, int, int]],
) -> None:
    lines = [
        "# Tableau Reverse-Engineering Catalog",
        "",
        "| Workbook | Worksheets | Relevant Fields |",
        "|---|---:|---:|",
    ]
    for workbook, workbook_dir, worksheet_count, field_count in sorted(
        summaries, key=lambda item: item[0].name.casefold()
    ):
        lines.append(
            f"| [{markdown_escape(workbook.name)}]"
            f"({quote(workbook_dir.name)}/"
            f"{quote(workbook_markdown_filename(workbook.name))}) | "
            f"{worksheet_count} | {field_count} |"
        )
    (output_root / "README.md").write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a reverse-engineering catalog from Tableau workbooks."
    )
    parser.add_argument("input", type=Path, help=".twb/.twbx file or directory")
    parser.add_argument("--output", type=Path, required=True, help="output directory")
    parser.add_argument("--workbook", help="only process this workbook name")
    parser.add_argument(
        "--worksheet",
        action="append",
        default=[],
        help="only document this worksheet name; may be repeated",
    )
    parser.add_argument(
        "--emit-json",
        action="store_true",
        help="also emit a compact <Workbook>.json file",
    )
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
    summaries: list[tuple[Workbook, Path, int, int]] = []
    workbooks: list[Workbook] = []
    failures: list[str] = []
    for source in files:
        try:
            workbook = parse_workbook(source)
            if args.workbook and workbook.name != args.workbook:
                continue
            if args.worksheet:
                requested = set(args.worksheet)
                workbook.worksheets = [
                    sheet for sheet in workbook.worksheets if sheet.name in requested
                ]
            workbooks.append(workbook)
        except (CatalogError, OSError, zipfile.BadZipFile) as exc:
            if args.strict:
                raise
            failures.append(f"{source.name}: {exc}")
    all_cross_impacts = build_cross_workbook_impacts(workbooks)
    for index, workbook in enumerate(workbooks):
        workbook_cross_impacts = {
            key: impacts
            for (workbook_index, key), impacts in all_cross_impacts.items()
            if workbook_index == index
        }
        workbook_dir, worksheet_count, field_count = write_workbook_docs(
            workbook,
            args.output,
            args.emit_json,
            workbook_cross_impacts,
        )
        summaries.append(
            (workbook, workbook_dir, worksheet_count, field_count)
        )
    write_root_index(args.output, summaries)
    print(f"Processed workbooks: {len(summaries)}")
    print(f"Output: {args.output.resolve()}")
    if failures:
        print("Warnings:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
    return 0 if summaries else 1


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
