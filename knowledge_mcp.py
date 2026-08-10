#!/usr/bin/env python3
"""Read-only MCP access to the file-based Semantic Knowledge Base."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledge_build import (
    ENTITY_DIRECTORIES,
    KnowledgeError,
    impact_analysis as analyze_impact,
    load_knowledge,
    trace_dependencies,
)


class KnowledgeMcpError(RuntimeError):
    """A clear, user-facing Knowledge MCP error."""


@dataclass
class KnowledgeIndex:
    model: dict[str, Any]
    entities_by_id: dict[str, dict[str, Any]] = field(init=False)
    outgoing: dict[str, list[dict[str, Any]]] = field(init=False)
    incoming: dict[str, list[dict[str, Any]]] = field(init=False)
    workbook_names: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.entities_by_id = {
            str(entity["id"]): entity for entity in self.model["entities"]
        }
        self.outgoing = {}
        self.incoming = {}
        for relation in self.model["relations"]:
            self.outgoing.setdefault(str(relation["from"]), []).append(relation)
            self.incoming.setdefault(str(relation["to"]), []).append(relation)
        self.workbook_names = {
            str(source["id"]): str(source.get("name") or source["id"])
            for source in self.model["sources"]
            if isinstance(source, dict) and isinstance(source.get("id"), str)
        }

    @classmethod
    def load(cls, root: Path) -> "KnowledgeIndex":
        return cls(load_knowledge(root))

    def summary(self) -> dict[str, int]:
        return {
            "schema_version": int(self.model["schema_version"]),
            "sources": len(self.model["sources"]),
            "entities": len(self.model["entities"]),
            "relations": len(self.model["relations"]),
            "warnings": len(self.model["warnings"]),
        }

    def _require_entity(self, entity_id: str) -> dict[str, Any]:
        entity = self.entities_by_id.get(entity_id)
        if entity is None:
            raise KnowledgeMcpError(f"Entity not found: {entity_id}")
        return entity

    def _compact(self, entity: dict[str, Any]) -> dict[str, Any]:
        source_id = str(entity.get("provenance", {}).get("source_id") or "")
        return {
            "id": str(entity["id"]),
            "type": str(entity["type"]),
            "name": str(entity["name"]),
            "workbook": self.workbook_names.get(source_id),
        }

    @staticmethod
    def _limit(value: int) -> int:
        if value < 1 or value > 100:
            raise KnowledgeMcpError("limit must be between 1 and 100")
        return value

    @staticmethod
    def _depth(value: int) -> int:
        if value < 1 or value > 10:
            raise KnowledgeMcpError("max_depth must be between 1 and 10")
        return value

    def search_entities(
        self,
        query: str,
        entity_type: str = "all",
        workbook: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        limit = self._limit(limit)
        if entity_type != "all" and entity_type not in ENTITY_DIRECTORIES:
            raise KnowledgeMcpError(f"Unsupported entity type: {entity_type}")
        needle = query.strip().casefold()
        workbook_needle = workbook.strip().casefold() if workbook else None
        matches: list[dict[str, Any]] = []
        for entity in self.model["entities"]:
            if entity_type != "all" and entity["type"] != entity_type:
                continue
            compact = self._compact(entity)
            if workbook_needle and workbook_needle not in {
                str(compact.get("workbook") or "").casefold(),
                str(entity.get("provenance", {}).get("source_id") or "").casefold(),
            }:
                continue
            searchable = json.dumps(
                {
                    "id": entity.get("id"),
                    "name": entity.get("name"),
                    "attributes": entity.get("attributes", {}),
                    "manual": entity.get("manual", {}),
                    "manual_body": entity.get("manual_body", ""),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).casefold()
            if not needle or needle in searchable:
                matches.append(compact)
        matches.sort(
            key=lambda item: (
                str(item["name"]).casefold(),
                str(item["type"]),
                str(item["id"]),
            )
        )
        return {
            "query": query,
            "count": min(len(matches), limit),
            "total_matches": len(matches),
            "truncated": len(matches) > limit,
            "results": matches[:limit],
        }

    def describe_entity(self, entity_id: str) -> dict[str, Any]:
        entity = self._require_entity(entity_id)

        def relation_item(
            relation: dict[str, Any], target_id: str
        ) -> dict[str, Any]:
            return {
                "relation": str(relation["type"]),
                "entity": self._compact(self.entities_by_id[target_id]),
                "evidence": relation.get("evidence", {}),
            }

        return {
            **self._compact(entity),
            "automatic": {
                "provenance": entity.get("provenance", {}),
                "attributes": entity.get("attributes", {}),
            },
            "manual": entity.get("manual", {}),
            "manual_body": entity.get("manual_body", ""),
            "relationships": [
                relation_item(relation, str(relation["to"]))
                for relation in self.outgoing.get(entity_id, [])
            ],
            "where_used": [
                relation_item(relation, str(relation["from"]))
                for relation in self.incoming.get(entity_id, [])
            ],
        }

    def where_is_used(self, entity_id: str) -> dict[str, Any]:
        entity = self._require_entity(entity_id)
        return {
            "entity": self._compact(entity),
            "used_by": [
                {
                    "relation": str(relation["type"]),
                    "entity": self._compact(
                        self.entities_by_id[str(relation["from"])]
                    ),
                }
                for relation in self.incoming.get(entity_id, [])
            ],
        }

    def show_dependencies(
        self, entity_id: str, max_depth: int = 3
    ) -> dict[str, Any]:
        entity = self._require_entity(entity_id)
        dependencies = trace_dependencies(
            self.model, entity_id, max_depth=self._depth(max_depth)
        )
        return {
            "entity": self._compact(entity),
            "dependencies": [
                self._compact(self.entities_by_id[item]) for item in dependencies
            ],
        }

    def impact_analysis(
        self, entity_id: str, max_depth: int = 3
    ) -> dict[str, Any]:
        entity = self._require_entity(entity_id)
        impacted = analyze_impact(
            self.model, entity_id, max_depth=self._depth(max_depth)
        )
        return {
            "entity": self._compact(entity),
            "impacted_entities": [
                self._compact(self.entities_by_id[item]) for item in impacted
            ],
        }

    def find_business_rules(
        self,
        query: str,
        workbook: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return self.search_entities(
            query,
            entity_type="business_rule",
            workbook=workbook,
            limit=limit,
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the Semantic Knowledge Base over MCP."
    )
    parser.add_argument("knowledge", type=Path, help="Knowledge Base root directory")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and summarize the Knowledge Base without starting MCP",
    )
    return parser


def create_mcp_server(index: KnowledgeIndex) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise KnowledgeMcpError(
            "The optional MCP SDK is not installed. "
            "Run: pip install -r requirements-mcp.txt"
        ) from exc

    server = FastMCP(
        "Semantic Knowledge Base",
        instructions=(
            "Read-only access to normalized dashboard semantics. Search before "
            "describing when an entity ID is unknown. Automatic metadata and "
            "manual human context are returned separately. Use dependencies for "
            "upstream lineage, where_is_used for direct consumers, and impact "
            "analysis for transitive downstream effects."
        ),
    )

    @server.tool()
    def search_entities(
        query: str,
        entity_type: str = "all",
        workbook: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search entities by text, optional type, and optional workbook."""
        return index.search_entities(
            query,
            entity_type=entity_type,
            workbook=workbook,
            limit=limit,
        )

    @server.tool()
    def describe_entity(entity_id: str) -> dict[str, Any]:
        """Describe one entity with automatic, manual, and relationship context."""
        return index.describe_entity(entity_id)

    @server.tool()
    def where_is_used(entity_id: str) -> dict[str, Any]:
        """List the direct entities and relation types that use an entity."""
        return index.where_is_used(entity_id)

    @server.tool()
    def show_dependencies(
        entity_id: str, max_depth: int = 3
    ) -> dict[str, Any]:
        """Trace bounded upstream dependencies for an entity."""
        return index.show_dependencies(entity_id, max_depth=max_depth)

    @server.tool()
    def impact_analysis(
        entity_id: str, max_depth: int = 3
    ) -> dict[str, Any]:
        """Trace bounded downstream entities potentially affected by a change."""
        return index.impact_analysis(entity_id, max_depth=max_depth)

    @server.tool()
    def find_business_rules(
        query: str,
        workbook: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search inferred and manual business rules, including human context."""
        return index.find_business_rules(query, workbook=workbook, limit=limit)

    return server


def run(args: argparse.Namespace) -> int:
    index = KnowledgeIndex.load(args.knowledge)
    if args.check:
        print(json.dumps(index.summary(), sort_keys=True))
        return 0
    create_mcp_server(index).run(transport="stdio")
    return 0


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (KnowledgeError, KnowledgeMcpError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
