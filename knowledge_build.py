#!/usr/bin/env python3
"""Validate a file-based semantic knowledge base and render Markdown pages."""

from __future__ import annotations

import argparse
import json
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
    for path in sorted(source_dir.glob("*.json")):
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
        if relation["type"] in {"depends_on", "calculated_by", "comes_from"}:
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


def render_markdown(root: Path, model: dict[str, Any]) -> int:
    markdown_root = root / "markdown"
    staging_root = root / ".markdown.tmp"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)
    entities = {entity["id"]: entity for entity in model["entities"]}

    for entity in model["entities"]:
        output_path = _page_path(staging_root, entity)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {entity['name']}", "", "## Automatic metadata", ""]
        automatic = {
            "id": entity["id"],
            "type": entity["type"],
            "provenance": entity.get("provenance", {}),
            "attributes": entity.get("attributes", {}),
        }
        lines.extend(
            ["```yaml", yaml.safe_dump(automatic, sort_keys=True).rstrip(), "```"]
        )

        outgoing = [item for item in model["relations"] if item["from"] == entity["id"]]
        if outgoing:
            lines.extend(["", "## Relationships", ""])
            for relation in outgoing:
                target = entities[relation["to"]]
                target_path = _page_path(staging_root, target)
                relative = Path("..") / target_path.parent.name / target_path.name
                lines.append(
                    f"- {relation['type']} → [{target['name']}]({relative.as_posix()})"
                )

        incoming = where_is_used(model, str(entity["id"]))
        if incoming:
            lines.extend(["", "## Where used", ""])
            for relation in incoming:
                source = entities[relation["from"]]
                source_path = _page_path(staging_root, source)
                relative = Path("..") / source_path.parent.name / source_path.name
                lines.append(
                    f"- {relation['type']} ← [{source['name']}]({relative.as_posix()})"
                )

        manual = entity.get("manual") or {}
        manual_body = str(entity.get("manual_body") or "").strip()
        if manual or manual_body:
            lines.extend(["", "## Human context", ""])
            if manual:
                lines.extend(
                    ["```yaml", yaml.safe_dump(manual, sort_keys=True).rstrip(), "```", ""]
                )
            if manual_body:
                lines.append(manual_body)

        output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    if markdown_root.exists():
        shutil.rmtree(markdown_root)
    staging_root.replace(markdown_root)
    return len(model["entities"])


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
