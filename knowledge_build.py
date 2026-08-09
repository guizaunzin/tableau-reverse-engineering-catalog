#!/usr/bin/env python3
"""Validate a file-based semantic knowledge base and render Markdown pages."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from collections import deque
from pathlib import Path
from typing import Any

import yaml


ENTITY_DIRECTORIES = {
    "dashboard": "dashboards",
    "visual": "visuals",
    "metric": "metrics",
    "field": "fields",
    "calculation": "calculations",
    "filter": "filters",
    "datasource": "datasources",
    "business_rule": "business-rules",
}

INDEX_SECTIONS = (
    ("dashboard", "Dashboards"),
    ("datasource", "Data Sources"),
    ("business_rule", "Business Rules"),
    ("visual", "Visuals"),
    ("metric", "Metrics"),
    ("calculation", "Calculations"),
    ("filter", "Filters"),
    ("field", "Fields"),
)

CHIP_INDEX_TYPES = {"metric", "calculation", "filter", "field"}

PILL_STYLES = {
    "metric": {
        "label": "Metric",
        "background": "#D1FAE5",
        "text": "#065F46",
        "border": "#6EE7B7",
    },
    "calculation": {
        "label": "Calculation",
        "background": "#F3E8FF",
        "text": "#6B21A8",
        "border": "#D8B4FE",
    },
    "filter": {
        "label": "Filter",
        "background": "#DBEAFE",
        "text": "#1E40AF",
        "border": "#93C5FD",
    },
    "field": {
        "label": "Field",
        "background": "#E2E8F0",
        "text": "#334155",
        "border": "#CBD5E1",
    },
}

SUMMARY_CARD_STYLES = {
    "datasource": ("#7C3AED", "#DDD6FE"),
    "visual": ("#0F766E", "#99F6E4"),
    "field": ("#059669", "#A7F3D0"),
    "calculation": ("#0284C7", "#BAE6FD"),
    "filter": ("#0891B2", "#A5F3FC"),
    "parameter": ("#DB2777", "#FBCFE8"),
    "metric": ("#D97706", "#FDE68A"),
    "inferred-rule": ("#9333EA", "#E9D5FF"),
    "business-rule": ("#B45309", "#FDE68A"),
}


class KnowledgeError(RuntimeError):
    """A user-facing knowledge validation error."""


def page_slug(entity_id: str) -> str:
    return "-".join(part for part in entity_id.replace("_", "-").split(":") if part)


def read_manual_page(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise KnowledgeError(f"manual page has no YAML front matter: {path}")
    marker = text.find("\n---", 4)
    if marker < 0:
        raise KnowledgeError(f"manual page has unterminated YAML front matter: {path}")
    try:
        metadata = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError as exc:
        raise KnowledgeError(f"invalid YAML front matter in {path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise KnowledgeError(f"manual front matter must be an object: {path}")
    return metadata, text[marker + 4 :].strip()


def _read_source_documents(root: Path) -> list[dict[str, Any]]:
    source_dir = root / "sources"
    if not source_dir.is_dir():
        raise KnowledgeError(f"knowledge source directory does not exist: {source_dir}")
    documents: list[dict[str, Any]] = []
    for path in sorted(source_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeError(f"invalid knowledge source {path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 2:
            raise KnowledgeError(f"knowledge source must use schema_version 2: {path}")
        documents.append(payload)
    if not documents:
        raise KnowledgeError(f"no JSON knowledge sources found in {source_dir}")
    return documents


def _cross_source_field_relations(
    entities: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    datasource_identities = {
        entity_id: entity.get("attributes", {}).get("published_identity")
        for entity_id, entity in entities.items()
        if entity.get("type") == "datasource"
        and isinstance(entity.get("attributes"), dict)
        and entity.get("attributes", {}).get("published_identity")
    }
    field_datasources = {
        str(relation["from"]): str(relation["to"])
        for relation in relations
        if relation.get("type") == "comes_from"
        and relation.get("to") in datasource_identities
    }
    groups: dict[tuple[str, str], list[str]] = {}
    for field_id, datasource_id in field_datasources.items():
        entity = entities.get(field_id)
        if entity is None or entity.get("type") != "field":
            continue
        attributes = entity.get("attributes", {})
        if not isinstance(attributes, dict):
            continue
        internal_name = attributes.get("internal_name")
        if not isinstance(internal_name, str):
            continue
        identity = str(datasource_identities[datasource_id])
        groups.setdefault((identity, internal_name.casefold()), []).append(field_id)

    result: list[dict[str, Any]] = []
    for matching_fields in groups.values():
        ordered = sorted(set(matching_fields))
        for index, source in enumerate(ordered):
            source_provenance = entities[source].get("provenance", {})
            source_workbook = source_provenance.get("source_id")
            for target in ordered[index + 1 :]:
                target_provenance = entities[target].get("provenance", {})
                if target_provenance.get("source_id") == source_workbook:
                    continue
                result.append(
                    {
                        "from": source,
                        "type": "same_source_field_as",
                        "to": target,
                        "evidence": {"source": "tableau", "direct": False},
                    }
                )
    return result


def load_knowledge(root: Path) -> dict[str, Any]:
    entities: dict[str, dict[str, Any]] = {}
    relations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    for document in _read_source_documents(root):
        sources.extend(document.get("sources", []))
        warnings.extend(document.get("warnings", []))
        for raw_entity in document.get("entities", []):
            if not isinstance(raw_entity, dict):
                raise KnowledgeError("automatic entity must be an object")
            entity_id = raw_entity.get("id")
            entity_type = raw_entity.get("type")
            if not isinstance(entity_id, str) or not isinstance(entity_type, str):
                raise KnowledgeError("automatic entity requires string id and type")
            if entity_type not in ENTITY_DIRECTORIES:
                raise KnowledgeError(f"unsupported entity type: {entity_type}")
            if entity_id in entities:
                raise KnowledgeError(f"duplicate entity id: {entity_id}")
            entity = dict(raw_entity)
            entity["manual"] = {}
            entity["manual_body"] = ""
            entities[entity_id] = entity
        raw_relations = document.get("relations", [])
        if not isinstance(raw_relations, list):
            raise KnowledgeError("relations must be a list")
        relations.extend(raw_relations)

    relations.extend(_cross_source_field_relations(entities, relations))

    pending_manual_relations: list[dict[str, Any]] = []
    manual_entity_ids: set[str] = set()
    manual_root = root / "manual"
    if manual_root.exists():
        for path in sorted(manual_root.rglob("*.md")):
            metadata, body = read_manual_page(path)
            entity_id = metadata.get("id")
            entity_type = metadata.get("type")
            if not isinstance(entity_id, str) or not isinstance(entity_type, str):
                raise KnowledgeError(f"manual page requires string id and type: {path}")
            if entity_id in manual_entity_ids:
                raise KnowledgeError(f"duplicate manual page for entity: {entity_id}")
            manual_entity_ids.add(entity_id)
            if entity_type not in ENTITY_DIRECTORIES:
                raise KnowledgeError(f"unsupported manual entity type {entity_type}: {path}")
            existing = entities.get(entity_id)
            if existing is None:
                if entity_type != "business_rule":
                    raise KnowledgeError(
                        f"manual page references unknown automatic entity: {entity_id}"
                    )
                existing = {
                    "id": entity_id,
                    "type": entity_type,
                    "name": str(
                        metadata.get("name")
                        or entity_id.rsplit(":", 1)[-1]
                        .replace("-", " ")
                        .title()
                    ),
                    "provenance": {
                        "source_id": "manual",
                        "tableau_object_type": None,
                        "tableau_name": None,
                    },
                    "attributes": {},
                    "manual": {},
                    "manual_body": "",
                }
                entities[entity_id] = existing
            elif existing["type"] != entity_type:
                raise KnowledgeError(
                    f"manual type {entity_type} does not match {existing['type']} for {entity_id}"
                )
            raw_manual_relations = metadata.get("relations", [])
            if raw_manual_relations is None:
                raw_manual_relations = []
            if not isinstance(raw_manual_relations, list):
                raise KnowledgeError(f"manual relations must be a list: {path}")
            for raw_relation in raw_manual_relations:
                if not isinstance(raw_relation, dict):
                    raise KnowledgeError(f"manual relation must be an object: {path}")
                relation_type = raw_relation.get("type")
                target = raw_relation.get("to")
                if not isinstance(relation_type, str) or not isinstance(target, str):
                    raise KnowledgeError(f"manual relation requires type and to: {path}")
                pending_manual_relations.append(
                    {
                        "from": entity_id,
                        "type": relation_type,
                        "to": target,
                        "evidence": {"source": "manual", "direct": True},
                    }
                )
            existing["manual"] = {
                key: value
                for key, value in metadata.items()
                if key not in {"id", "type", "relations"}
            }
            existing["manual_body"] = body

    relations.extend(pending_manual_relations)
    for entity in entities.values():
        manual = entity.get("manual", {})
        for key, value in manual.items():
            if not key.endswith("_ids"):
                continue
            if not isinstance(value, list) or not all(
                isinstance(reference, str) for reference in value
            ):
                raise KnowledgeError(
                    f"manual {key} must be a list of entity ids: {entity['id']}"
                )
            for reference in value:
                if reference not in entities:
                    raise KnowledgeError(
                        f"unknown {key} reference {reference}: {entity['id']}"
                    )
    for relation in relations:
        if not isinstance(relation, dict):
            raise KnowledgeError("relation must be an object")
        source = relation.get("from")
        target = relation.get("to")
        if source not in entities:
            raise KnowledgeError(f"unknown relation source: {source}")
        if target not in entities:
            raise KnowledgeError(f"unknown relation target: {target}")

    unique_relations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in relations:
        identity = (str(item["from"]), str(item["type"]), str(item["to"]))
        unique_relations.setdefault(identity, item)
    return {
        "schema_version": 2,
        "sources": sources,
        "entities": sorted(entities.values(), key=lambda item: str(item["id"])),
        "relations": sorted(
            unique_relations.values(),
            key=lambda item: (str(item["from"]), str(item["type"]), str(item["to"])),
        ),
        "warnings": warnings,
    }


def where_is_used(model: dict[str, Any], entity_id: str) -> list[dict[str, Any]]:
    return [relation for relation in model["relations"] if relation["to"] == entity_id]


def find_entities(
    model: dict[str, Any], query: str, entity_type: str | None = None
) -> list[dict[str, Any]]:
    needle = query.strip().casefold()
    matches: list[dict[str, Any]] = []
    for entity in model["entities"]:
        if entity_type is not None and entity["type"] != entity_type:
            continue
        searchable = " ".join(
            [
                str(entity.get("id", "")),
                str(entity.get("name", "")),
                json.dumps(entity.get("manual", {}), ensure_ascii=False, sort_keys=True),
                str(entity.get("manual_body", "")),
            ]
        ).casefold()
        if not needle or needle in searchable:
            matches.append(entity)
    return sorted(matches, key=lambda item: str(item["id"]))


def trace_dependencies(
    model: dict[str, Any], entity_id: str, max_depth: int = 20
) -> list[str]:
    adjacency: dict[str, list[str]] = {}
    for relation in model["relations"]:
        if relation["type"] in {
            "depends_on",
            "calculated_by",
            "comes_from",
            "implemented_by",
        }:
            adjacency.setdefault(relation["from"], []).append(relation["to"])
    visited = {entity_id}
    result: list[str] = []
    queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for target in sorted(adjacency.get(current, [])):
            if target in visited:
                continue
            visited.add(target)
            result.append(target)
            queue.append((target, depth + 1))
    return result


def impact_analysis(
    model: dict[str, Any], entity_id: str, max_depth: int = 20
) -> list[str]:
    """Return entities transitively affected by a change to ``entity_id``."""
    impact_relations = {
        "affected_by",
        "calculated_by",
        "contains",
        "depends_on",
        "displays",
        "filters_on",
        "implemented_by",
        "same_source_field_as",
        "uses",
    }
    reverse: dict[str, list[str]] = {}
    for relation in model["relations"]:
        if relation["type"] in impact_relations:
            reverse.setdefault(relation["to"], []).append(relation["from"])
    visited = {entity_id}
    result: list[str] = []
    queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for affected in sorted(reverse.get(current, [])):
            if affected in visited:
                continue
            visited.add(affected)
            result.append(affected)
            queue.append((affected, depth + 1))
    return result


def _page_path(markdown_root: Path, entity: dict[str, Any]) -> Path:
    return (
        markdown_root
        / ENTITY_DIRECTORIES[str(entity["type"])]
        / f"{page_slug(str(entity['id']))}.md"
    )


def _heading_anchor(title: str) -> str:
    plain = re.sub(r"<[^>]+>", "", title).casefold().strip()
    anchor = re.sub(r"[^\w\s-]", "", plain)
    return re.sub(r"[-\s]+", "-", anchor).strip("-")


def _with_contents(lines: list[str]) -> list[str]:
    headings = [line[3:].strip() for line in lines if line.startswith("## ")]
    if len(headings) < 2:
        return lines
    remainder = list(lines[1:])
    while remainder and not remainder[0]:
        remainder.pop(0)
    contents = ["## Contents", ""]
    contents.extend(
        f"- [{title}](#{_heading_anchor(title)})" for title in headings
    )
    return [lines[0], "", *contents, "", *remainder]


def _index_href(
    markdown_root: Path, entity: dict[str, Any]
) -> tuple[str, str]:
    relative = _page_path(markdown_root, entity).relative_to(markdown_root)
    return relative.as_posix(), str(entity["name"])


def _pill_html(
    label: str,
    entity_type: str,
    *,
    href: str | None = None,
    underline: bool = False,
) -> str:
    colors = PILL_STYLES[entity_type]
    clean_label = (
        html.escape(label.replace("\n", " ")).replace("|", "&#124;")
    )
    style = (
        "display: inline-block; padding: 2px 8px; margin: 2px 2px 2px 0; "
        "border-radius: 999px; "
        f"background-color: {colors['background']}; color: {colors['text']}; "
        f"border: 1px solid {colors['border']}; font-size: 0.9em; "
        "line-height: 1.4; white-space: nowrap;"
    )
    pill = (
        f'<span data-entity-type="{entity_type}" '
        f'title="{colors["label"]}" style="{style}">{clean_label}</span>'
    )
    if underline:
        pill = f"<u>{pill}</u>"
    if href is None:
        return pill
    return (
        f'<a href="{html.escape(href, quote=True)}" '
        f'style="text-decoration: none;">{pill}</a>'
    )


def _append_dashboard_cards(
    lines: list[str], markdown_root: Path, dashboards: list[dict[str, Any]]
) -> None:
    lines.append("<table>")
    for offset in range(0, len(dashboards), 2):
        lines.append("  <tr>")
        for dashboard in dashboards[offset : offset + 2]:
            href, label = _index_href(markdown_root, dashboard)
            lines.append(
                '    <td align="center" width="50%">'
                f'<a href="{html.escape(href, quote=True)}"><strong>'
                f"{html.escape(label)}</strong></a><br><sub>Dashboard</sub></td>"
            )
        if len(dashboards[offset : offset + 2]) == 1:
            lines.append('    <td width="50%"></td>')
        lines.append("  </tr>")
    lines.append("</table>")


def _append_index_chips(
    lines: list[str], markdown_root: Path, matching: list[dict[str, Any]]
) -> None:
    chips: list[str] = []
    for entity in matching:
        href, label = _index_href(markdown_root, entity)
        chips.append(_pill_html(label, str(entity["type"]), href=href))
    for offset in range(0, len(chips), 8):
        lines.append(" ".join(chips[offset : offset + 8]))


def _render_index(
    markdown_root: Path, model: dict[str, Any], title: str
) -> None:
    lines = [
        f"# {title}",
        "",
        "Start with a Dashboard, or navigate directly to any entity in this workbook.",
    ]
    for entity_type, title in INDEX_SECTIONS:
        matching = sorted(
            (
                entity
                for entity in model["entities"]
                if entity["type"] == entity_type
            ),
            key=lambda item: (str(item["name"]).casefold(), str(item["id"])),
        )
        if entity_type == "business_rule" and not matching:
            continue
        if entity_type == "business_rule" and all(
            item.get("attributes", {}).get("semantic_status") == "inferred"
            for item in matching
        ):
            title = "Inferred Rules"
        lines.extend(["", f"## {title}", ""])
        if not matching:
            lines.append("No entities.")
            continue
        if entity_type == "dashboard":
            _append_dashboard_cards(lines, markdown_root, matching)
        elif entity_type in CHIP_INDEX_TYPES:
            _append_index_chips(lines, markdown_root, matching)
        else:
            for entity in matching:
                relative = _page_path(markdown_root, entity).relative_to(markdown_root)
                lines.append(f"- [{entity['name']}]({relative.as_posix()})")
    lines = _with_contents(lines)
    (markdown_root / "README.md").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )


def _workbook_slug(source_id: str) -> str:
    prefix = "tableau-workbook:"
    identifier = source_id[len(prefix) :] if source_id.startswith(prefix) else source_id
    return page_slug(identifier)


def _workbook_model(
    model: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    source_id = str(source["id"])
    entities = {str(item["id"]): item for item in model["entities"]}
    included = {
        entity_id
        for entity_id, entity in entities.items()
        if entity.get("provenance", {}).get("source_id") == source_id
    }
    changed = True
    while changed:
        changed = False
        referenced_rules = {
            str(reference)
            for entity_id in included
            for key, value in (entities[entity_id].get("manual") or {}).items()
            if key.endswith("business_rule_ids") and isinstance(value, list)
            for reference in value
        }
        for relation in model["relations"]:
            relation_source = str(relation["from"])
            relation_target = str(relation["to"])
            if relation_source in included:
                referenced_rules.add(relation_target)
            if relation_target in included:
                referenced_rules.add(relation_source)
        for entity_id in referenced_rules:
            entity = entities.get(entity_id)
            if (
                entity is not None
                and entity["type"] == "business_rule"
                and entity_id not in included
            ):
                included.add(entity_id)
                changed = True
    return {
        "schema_version": model["schema_version"],
        "sources": [source],
        "entities": [
            entity for entity in model["entities"] if entity["id"] in included
        ],
        "relations": [
            relation
            for relation in model["relations"]
            if relation["from"] in included and relation["to"] in included
        ],
        "warnings": [
            warning
            for warning in model["warnings"]
            if warning.get("source_id") == source_id
        ],
    }


def _render_workbook_index(
    markdown_root: Path, workbooks: list[tuple[dict[str, Any], str]]
) -> None:
    lines = [
        "# Semantic Knowledge Base",
        "",
        "Choose a Tableau workbook.",
        "",
        "## Workbooks",
        "",
    ]
    for source, slug in sorted(
        workbooks,
        key=lambda item: (str(item[0].get("name", "")).casefold(), item[1]),
    ):
        lines.append(
            f"- [{source.get('name') or slug}](workbooks/{slug}/README.md)"
        )
    (markdown_root / "README.md").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )


def _entity_link(
    markdown_root: Path,
    current: dict[str, Any],
    target: dict[str, Any],
    *,
    anchor: str | None = None,
) -> str:
    target_path = _page_path(markdown_root, target)
    current_path = _page_path(markdown_root, current)
    relative = Path("..") / target_path.parent.name / target_path.name
    if current_path.parent == target_path.parent:
        relative = Path(target_path.name)
    href = relative.as_posix()
    if anchor:
        href = f"{href}#{anchor}"
    entity_type = str(target["type"])
    if entity_type in PILL_STYLES:
        return _pill_html(
            str(target["name"]), entity_type, href=href
        )
    label = str(target["name"]).replace("|", "\\|").replace("\n", " ")
    return f"[{label}]({href})"


def _table_cell(value: Any) -> str:
    return (
        str(value)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("|", "\\|")
        .replace("\n", "<br>")
    )


def _relation_index(
    model: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    for relation in model["relations"]:
        outgoing.setdefault(str(relation["from"]), []).append(relation)
        incoming.setdefault(str(relation["to"]), []).append(relation)
    return outgoing, incoming


def _reachable_entities(
    starts: list[str],
    outgoing: dict[str, list[dict[str, Any]]],
) -> set[str]:
    allowed = {
        "affected_by",
        "calculated_by",
        "comes_from",
        "contains",
        "depends_on",
        "displays",
        "filters_on",
        "uses",
    }
    seen = set(starts)
    pending = list(starts)
    while pending:
        current = pending.pop()
        for relation in outgoing.get(current, []):
            if relation["type"] not in allowed or relation["to"] in seen:
                continue
            seen.add(str(relation["to"]))
            pending.append(str(relation["to"]))
    return seen


def _sorted_entities(
    entity_ids: set[str] | list[str], entities: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return sorted(
        (entities[entity_id] for entity_id in entity_ids if entity_id in entities),
        key=lambda item: (str(item["name"]).casefold(), str(item["id"])),
    )


def _visual_contents(
    visual_id: str,
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    direct_ids = {
        str(relation["to"])
        for relation in outgoing.get(visual_id, [])
        if relation["type"] in {"affected_by", "displays", "uses"}
    }
    reachable = _reachable_entities([visual_id], outgoing)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for entity_type in ENTITY_DIRECTORIES:
        candidates = (
            direct_ids
            if entity_type in {"metric", "calculation", "filter"}
            else reachable
        )
        matching = {
            entity_id
            for entity_id in candidates
            if entity_id in entities and entities[entity_id]["type"] == entity_type
        }
        by_type[entity_type] = _sorted_entities(matching, entities)
    documented_fields = [
        item
        for item in by_type["field"]
        if item.get("attributes", {}).get("field_type") != "Parameter"
    ]
    datasource_ids = {
        str(relation["to"])
        for item in documented_fields
        for relation in outgoing.get(str(item["id"]), [])
        if relation["type"] == "comes_from"
        and relation["to"] in entities
        and entities[relation["to"]]["type"] == "datasource"
    }
    by_type["datasource"] = _sorted_entities(datasource_ids, entities)
    return by_type


def _append_summary(
    lines: list[str],
    dashboard: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> None:
    scope = _reachable_entities([str(dashboard["id"])], outgoing)
    scoped_entities = [entities[item] for item in scope if item in entities]
    parameters = {
        str(item["id"])
        for item in scoped_entities
        if item["type"] == "field"
        and item.get("attributes", {}).get("field_type") == "Parameter"
    }
    manual_rule_ids: set[str] = set()
    for item in scoped_entities:
        for key, value in (item.get("manual") or {}).items():
            if key.endswith("business_rule_ids") and isinstance(value, list):
                manual_rule_ids.update(str(rule_id) for rule_id in value)
    scope.update(manual_rule_ids)
    counts = {
        entity_type: sum(
            1
            for entity_id in scope
            if entity_id in entities and entities[entity_id]["type"] == entity_type
        )
        for entity_type in ENTITY_DIRECTORIES
    }
    inferred_rule_count = sum(
        1
        for entity_id in scope
        if entity_id in entities
        and entities[entity_id]["type"] == "business_rule"
        and entities[entity_id].get("attributes", {}).get("semantic_status")
        == "inferred"
    )
    approved_rule_count = counts["business_rule"] - inferred_rule_count
    cards = [
        ("datasource", "Data Sources", counts["datasource"]),
        ("visual", "Visuals", counts["visual"]),
        ("field", "Fields", counts["field"]),
        ("calculation", "Calculations", counts["calculation"]),
        ("filter", "Filters", counts["filter"]),
        ("parameter", "Parameters", len(parameters)),
        ("metric", "Metrics", counts["metric"]),
    ]
    if inferred_rule_count:
        cards.append(("inferred-rule", "Inferred Rules", inferred_rule_count))
    if approved_rule_count:
        cards.append(("business-rule", "Business Rules", approved_rule_count))
    lines.extend(
        [
            "",
            "## Summary",
            "",
            '<div style="overflow-x: auto;">',
            '<table style="border-collapse: separate; border-spacing: 8px; width: 100%;">',
            "  <tr>",
        ]
    )
    for summary_type, label, count in cards:
        color, border = SUMMARY_CARD_STYLES[summary_type]
        lines.append(
            f'    <td data-summary-entity="{summary_type}" align="left" '
            f'style="min-width: 112px; padding: 12px 14px; border: 1px solid {border}; '
            'border-radius: 14px; background-color: #FFFFFF; white-space: nowrap;">'
            f'<strong style="font-size: 1.75em; line-height: 1; color: {color};">'
            f'{count}</strong><br><span style="font-size: 0.72em; font-weight: 600; '
            f'letter-spacing: 0.04em; color: #475569;">{html.escape(label.upper())}</span></td>'
        )
    lines.extend(["  </tr>", "</table>", "</div>"])


def _append_dashboard_header(lines: list[str], dashboard: dict[str, Any]) -> None:
    manual = dashboard.get("manual") or {}
    description = manual.get("description") or manual.get("purpose")
    if description:
        lines.extend(["", str(description)])
    header_fields = (
        ("Owner", "owner"),
        ("Status", "status"),
        ("Last reviewed", "last_reviewed"),
    )
    for label, key in header_fields:
        if manual.get(key):
            lines.extend(["", f"{label}: {manual[key]}"])


def _append_dashboard_rules(
    lines: list[str],
    dashboard: dict[str, Any],
    markdown_root: Path,
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> None:
    scope = _reachable_entities([str(dashboard["id"])], outgoing)
    rules = _sorted_entities(
        {
            entity_id
            for entity_id in scope
            if entity_id in entities
            and entities[entity_id]["type"] == "business_rule"
            and entities[entity_id].get("attributes", {}).get("semantic_status")
            == "inferred"
        },
        entities,
    )
    if not rules:
        return
    lines.extend(
        [
            "",
            "## Inferred Rules",
            "",
            "> Automatically inferred from Tableau logic. Verify before treating these as approved business definitions.",
            "",
        ]
    )
    for rule in rules:
        attributes = rule.get("attributes", {})
        statement = attributes.get("statement") or "No statement generated."
        confidence = attributes.get("confidence") or "unknown"
        lines.append(
            f"- {_entity_link(markdown_root, dashboard, rule)} — "
            f"{_underline_field_mentions(str(statement), attributes)} "
            f"({confidence} confidence)"
        )


def _append_dashboard_visuals(
    lines: list[str],
    dashboard: dict[str, Any],
    markdown_root: Path,
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> None:
    visuals = _dashboard_visuals(dashboard, entities, outgoing)
    lines.extend(["", "## Visuals", ""])
    if not visuals:
        lines.append("No visuals identified.")
        return
    for number, visual in enumerate(visuals, start=1):
        if number > 1:
            lines.append("")
        contents = _visual_contents(str(visual["id"]), entities, outgoing)
        lines.append(
            f"### {number}. {_entity_link(markdown_root, dashboard, visual)}"
        )
        for label, entity_type in (
            ("Metrics", "metric"),
            ("Calculations", "calculation"),
            ("Filters", "filter"),
            ("Fields", "field"),
        ):
            items = contents[entity_type]
            if entity_type == "field":
                items = sorted(
                    items,
                    key=lambda item: (
                        item.get("attributes", {}).get("field_type") == "Parameter",
                        str(item["name"]).casefold(),
                    ),
                )
            pills = " ".join(
                _entity_link(markdown_root, dashboard, item) for item in items
            )
            lines.extend(["", f"**{label}:** {pills or 'None identified'}"])


def _dashboard_visuals(
    dashboard: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    contained_ids = {
        str(relation["to"])
        for relation in outgoing.get(str(dashboard["id"]), [])
        if relation["type"] == "contains"
        and relation["to"] in entities
        and entities[relation["to"]]["type"] == "visual"
    }
    layout = dashboard.get("attributes", {}).get("layout", {})
    layout_ids: list[str] = []
    if isinstance(layout, dict):
        ordered_items = sorted(
            (item for item in layout.get("items", []) if isinstance(item, dict)),
            key=lambda item: int(item.get("document_order", 0)),
        )
        for item in ordered_items:
            visual_id = item.get("visual_id")
            if visual_id in contained_ids and visual_id not in layout_ids:
                layout_ids.append(str(visual_id))
    remaining = contained_ids.difference(layout_ids)
    return [entities[item] for item in layout_ids] + _sorted_entities(
        remaining, entities
    )


def _svg_number(value: int, extent: int, coordinate_extent: int) -> str:
    scaled = value * extent / coordinate_extent
    return f"{scaled:.2f}".rstrip("0").rstrip(".")


def _render_dashboard_layout(
    lines: list[str],
    dashboard: dict[str, Any],
    markdown_root: Path,
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> None:
    layout = dashboard.get("attributes", {}).get("layout")
    if not isinstance(layout, dict) or layout.get("status") not in {
        "complete",
        "partial",
    }:
        return
    try:
        page_width = int(layout["dashboard_width"])
        page_height = int(layout["dashboard_height"])
        coordinate_width = int(layout["coordinate_space"]["width"])
        coordinate_height = int(layout["coordinate_space"]["height"])
    except (KeyError, TypeError, ValueError):
        return
    if min(page_width, page_height, coordinate_width, coordinate_height) <= 0:
        return
    items = sorted(
        (item for item in layout.get("items", []) if isinstance(item, dict)),
        key=lambda item: int(item.get("document_order", 0)),
    )
    if not items:
        return
    visuals = _dashboard_visuals(dashboard, entities, outgoing)
    visual_numbers = {
        str(visual["id"]): number for number, visual in enumerate(visuals, start=1)
    }
    title = f"{dashboard['name']} layout"
    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
            f'{page_width} {page_height}" role="img" aria-labelledby="title">'
        ),
        f'  <title id="title">{html.escape(title)}</title>',
        f'  <rect x="0" y="0" width="{page_width}" height="{page_height}" fill="#ffffff" stroke="#94a3b8"/>',
    ]
    for item in items:
        try:
            x = _svg_number(int(item["x"]), page_width, coordinate_width)
            y = _svg_number(int(item["y"]), page_height, coordinate_height)
            width = _svg_number(int(item["width"]), page_width, coordinate_width)
            height = _svg_number(int(item["height"]), page_height, coordinate_height)
        except (KeyError, TypeError, ValueError):
            continue
        kind = str(item.get("kind") or "object")
        visual_id = str(item.get("visual_id") or "")
        visual = entities.get(visual_id)
        if kind == "visual" and visual is not None:
            number = visual_numbers.get(visual_id)
            label = f"{number}. {visual['name']}" if number else str(visual["name"])
            fill, stroke = "#dbeafe", "#2563eb"
        else:
            label = str(item.get("label") or kind.replace("_", " ").title())
            fill, stroke = "#f1f5f9", "#64748b"
        opacity = ' opacity="0.45" stroke-dasharray="6 4"' if item.get("hidden") else ""
        svg_lines.append(
            f'  <rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"{opacity}/>'
        )
        text_x = _svg_number(int(item["x"]) + 700, page_width, coordinate_width)
        text_y = _svg_number(int(item["y"]) + 2200, page_height, coordinate_height)
        svg_lines.append(
            f'  <text x="{text_x}" y="{text_y}" fill="#0f172a" '
            f'font-family="sans-serif" font-size="14">{html.escape(label)}</text>'
        )
    svg_lines.append("</svg>")
    asset_name = f"{page_slug(str(dashboard['id']))}.svg"
    asset_path = markdown_root / "assets" / "layouts" / asset_name
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_text("\n".join(svg_lines) + "\n", encoding="utf-8")
    lines.extend(
        [
            "",
            "## Dashboard layout",
            "",
            f"![{title}](../assets/layouts/{asset_name})",
        ]
    )
    warnings = layout.get("warnings") or []
    if warnings:
        lines.extend(["", "Layout warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)


def _append_visual_dictionary(
    lines: list[str],
    visual: dict[str, Any],
    markdown_root: Path,
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
) -> None:
    contents = _visual_contents(str(visual["id"]), entities, outgoing)
    dashboards = _sorted_entities(
        {
            str(relation["from"])
            for relation in incoming.get(str(visual["id"]), [])
            if relation["type"] == "contains"
        },
        entities,
    )
    lines.extend(["", "## Dashboards", ""])
    lines.extend(
        f"- {_entity_link(markdown_root, visual, dashboard)}"
        for dashboard in dashboards
    )
    if not dashboards:
        lines.append("Not placed on a dashboard.")

    lines.extend(["", "## Metrics", ""])
    if contents["metric"]:
        definitions = {
            str(item["id"]): (
                (item.get("manual") or {}).get("business_definition")
                or (item.get("manual") or {}).get("description")
            )
            for item in contents["metric"]
        }
        if any(definitions.values()):
            lines.extend(
                ["| Metric | Business definition |", "|---|---|"]
            )
            for item in contents["metric"]:
                definition = definitions[str(item["id"])] or "Not documented"
                lines.append(
                    f"| {_entity_link(markdown_root, visual, item)} | "
                    f"{_table_cell(definition)} |"
                )
        else:
            lines.extend(
                f"- {_entity_link(markdown_root, visual, item)}"
                for item in contents["metric"]
            )
    else:
        lines.append("No metrics identified.")

    fields = [
        item
        for item in contents["field"]
        if item.get("attributes", {}).get("field_type") != "Parameter"
    ]
    lines.extend(["", "## Fields", ""])
    if fields:
        lines.extend(["| Field | Type | Role | Data Source |", "|---|---|---|---|"])
        for item in fields:
            attributes = item.get("attributes", {})
            datasources = [
                entities[relation["to"]]["name"]
                for relation in outgoing.get(str(item["id"]), [])
                if relation["type"] == "comes_from" and relation["to"] in entities
            ]
            lines.append(
                f"| {_entity_link(markdown_root, visual, item)} | "
                f"{_table_cell(attributes.get('field_type') or '—')} | "
                f"{_table_cell(attributes.get('role') or '—')} | "
                f"{_table_cell(', '.join(datasources) or '—')} |"
            )
    else:
        lines.append("No fields identified.")

    lines.extend(["", "## Calculations", ""])
    if contents["calculation"]:
        lines.extend(["| Calculation | Classification |", "|---|---|"])
        for item in contents["calculation"]:
            attributes = item.get("attributes", {})
            lines.append(
                f"| {_entity_link(markdown_root, visual, item)} | "
                f"{_table_cell(attributes.get('classification') or '—')} |"
            )
    else:
        lines.append("No calculations identified.")

    lines.extend(["", "## Filters", ""])
    if contents["filter"]:
        lines.extend(["| Filter | Field | Operator | Value |", "|---|---|---|---|"])
        for item in contents["filter"]:
            attributes = item.get("attributes", {})
            field_links = [
                _entity_link(markdown_root, visual, entities[relation["to"]])
                for relation in outgoing.get(str(item["id"]), [])
                if relation["type"] == "filters_on" and relation["to"] in entities
            ]
            lines.append(
                f"| {_entity_link(markdown_root, visual, item)} | "
                f"{', '.join(field_links) or '—'} | "
                f"{_table_cell(attributes.get('operator') or '—')} | "
                f"{_table_cell(attributes.get('value') or '—')} |"
            )
    else:
        lines.append("No filters identified.")

    lines.extend(["", "## Data Sources", ""])
    if contents["datasource"]:
        lines.extend(
            f"- {_entity_link(markdown_root, visual, item)}"
            for item in contents["datasource"]
        )
    else:
        lines.append("No data sources identified.")


def _append_calculation_formula(
    lines: list[str], calculation: dict[str, Any]
) -> None:
    formula = calculation.get("attributes", {}).get("formula_tableau")
    if not formula:
        return
    lines.extend(["", "## Formula", "", "```text", str(formula), "```"])


def _append_inferred_rule(
    lines: list[str],
    rule: dict[str, Any],
    markdown_root: Path,
    entities: dict[str, dict[str, Any]],
) -> None:
    attributes = rule.get("attributes", {})
    if attributes.get("semantic_status") != "inferred":
        return
    lines.extend(
        [
            "",
            "## Inferred rule",
            "",
            "> Automatically inferred from Tableau logic. Verify before treating this as an approved business definition.",
            "",
            _underline_field_mentions(
                str(attributes.get("statement") or "No statement generated."),
                attributes,
            ),
            "",
            f"Confidence: {attributes.get('confidence') or 'unknown'}",
            "",
            f"Rule kind: {str(attributes.get('rule_kind') or 'unknown').replace('_', ' ')}",
            "",
            "## Evidence",
            "",
        ]
    )
    calculation_ids = attributes.get("evidence_calculation_ids") or []
    for calculation_id in calculation_ids:
        calculation = entities.get(str(calculation_id))
        if calculation is not None:
            lines.append(
                f"- Implemented by {_entity_link(markdown_root, rule, calculation)}"
            )
    formula = attributes.get("formula_tableau")
    if formula:
        lines.extend(["", "```text", str(formula), "```"])


def _underline_field_mentions(statement: str, attributes: dict[str, Any]) -> str:
    mentions = sorted(
        {
            str(mention)
            for mention in attributes.get("field_mentions", [])
            if str(mention)
        },
        key=lambda value: (-len(value), value.casefold()),
    )
    if not mentions:
        return statement
    pattern = re.compile("|".join(re.escape(mention) for mention in mentions))
    return pattern.sub(
        lambda match: _pill_html(
            match.group(0), "field", underline=True
        ),
        statement,
    )


def _append_standard_sections(
    lines: list[str],
    entity: dict[str, Any],
    markdown_root: Path,
    entities: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
) -> None:
    lines.extend(["", "## Automatic metadata", ""])
    automatic = {
        "id": entity["id"],
        "type": entity["type"],
        "provenance": entity.get("provenance", {}),
        "attributes": entity.get("attributes", {}),
    }
    lines.extend(["```yaml", yaml.safe_dump(automatic, sort_keys=True).rstrip(), "```"])
    relations = outgoing.get(str(entity["id"]), [])
    if relations:
        lines.extend(["", "## Relationships", ""])
        for relation in relations:
            target = entities[relation["to"]]
            lines.append(
                f"- {relation['type']} → "
                f"{_entity_link(markdown_root, entity, target, anchor='top')}"
            )
    reverse_relations = incoming.get(str(entity["id"]), [])
    if reverse_relations:
        lines.extend(["", "## Where used", ""])
        for relation in reverse_relations:
            source = entities[relation["from"]]
            lines.append(
                f"- {relation['type']} ← "
                f"{_entity_link(markdown_root, entity, source, anchor='top')}"
            )
    manual = entity.get("manual") or {}
    manual_body = str(entity.get("manual_body") or "").strip()
    if manual or manual_body:
        lines.extend(["", "## Human context", ""])
        if manual:
            lines.extend(["```yaml", yaml.safe_dump(manual, sort_keys=True).rstrip(), "```", ""])
        if manual_body:
            lines.append(manual_body)


def _render_workbook_pages(
    markdown_root: Path, model: dict[str, Any], title: str
) -> int:
    entities = {entity["id"]: entity for entity in model["entities"]}
    outgoing, incoming = _relation_index(model)

    for entity in model["entities"]:
        output_path = _page_path(markdown_root, entity)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        entity_type = str(entity["type"])
        page_title = str(entity["name"])
        if entity_type in PILL_STYLES:
            page_title = _pill_html(page_title, entity_type)
        back_label = (
            str(title).replace("[", "\\[").replace("]", "\\]")
        )
        lines = [
            f'# {page_title}<a id="top"></a>\n\n'
            f"[← Back to {back_label}](../README.md)"
        ]
        if entity["type"] == "dashboard":
            _append_dashboard_header(lines, entity)
            _append_summary(lines, entity, entities, outgoing)
            _render_dashboard_layout(
                lines, entity, markdown_root, entities, outgoing
            )
            _append_dashboard_rules(
                lines, entity, markdown_root, entities, outgoing
            )
            _append_dashboard_visuals(
                lines, entity, markdown_root, entities, outgoing
            )
        elif entity["type"] == "visual":
            _append_visual_dictionary(
                lines, entity, markdown_root, entities, outgoing, incoming
            )
        elif entity["type"] == "calculation":
            _append_calculation_formula(lines, entity)
        elif entity["type"] == "business_rule":
            _append_inferred_rule(lines, entity, markdown_root, entities)
        _append_standard_sections(
            lines, entity, markdown_root, entities, outgoing, incoming
        )

        lines = _with_contents(lines)
        output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    _render_index(markdown_root, model, title)
    return len(model["entities"])


def _publish_markdown(staging_root: Path, markdown_root: Path) -> None:
    """Publish generated files without making the live tree disappear."""
    staged_directories = {
        path.relative_to(staging_root)
        for path in staging_root.rglob("*")
        if path.is_dir()
    }
    staged_files = {
        path.relative_to(staging_root)
        for path in staging_root.rglob("*")
        if path.is_file()
    }
    markdown_root.mkdir(parents=True, exist_ok=True)
    for relative in sorted(staged_directories, key=lambda path: len(path.parts)):
        (markdown_root / relative).mkdir(parents=True, exist_ok=True)
    for relative in sorted(staged_files):
        source = staging_root / relative
        target = markdown_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

    for path in sorted(markdown_root.rglob("*"), reverse=True):
        relative = path.relative_to(markdown_root)
        if path.is_file() and relative not in staged_files:
            path.unlink()
        elif path.is_dir() and relative not in staged_directories:
            path.rmdir()
    shutil.rmtree(staging_root)


def render_markdown(root: Path, model: dict[str, Any]) -> int:
    markdown_root = root / "markdown"
    staging_root = root / ".markdown.tmp"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)
    rendered = 0
    workbooks: list[tuple[dict[str, Any], str]] = []
    for source in model["sources"]:
        if source.get("type") != "tableau_workbook" or not source.get("id"):
            continue
        slug = _workbook_slug(str(source["id"]))
        workbook_model = _workbook_model(model, source)
        workbook_root = staging_root / "workbooks" / slug
        workbook_root.mkdir(parents=True)
        rendered += _render_workbook_pages(
            workbook_root,
            workbook_model,
            str(source.get("name") or slug),
        )
        workbooks.append((source, slug))
    if not workbooks:
        raise KnowledgeError("no Tableau workbook sources found")

    _render_workbook_index(staging_root, workbooks)
    _publish_markdown(staging_root, markdown_root)
    return rendered


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the Semantic Knowledge Base and render Markdown."
    )
    parser.add_argument("knowledge", type=Path, help="Knowledge Base root directory")
    parser.add_argument(
        "--check", action="store_true", help="validate only; do not render Markdown"
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        model = load_knowledge(args.knowledge)
        if args.check:
            print(
                json.dumps(
                    {
                        "entities": len(model["entities"]),
                        "relations": len(model["relations"]),
                        "warnings": len(model["warnings"]),
                    },
                    sort_keys=True,
                )
            )
        else:
            count = render_markdown(args.knowledge, model)
            print(f"Rendered entities: {count}")
            print(f"Output: {(args.knowledge / 'markdown').resolve()}")
        return 0
    except (KnowledgeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
