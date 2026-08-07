# Tableau Semantic Knowledge Base

This project extracts technical metadata from Tableau `.twb` and `.twbx`
workbooks and normalizes it into a small, file-based Semantic Knowledge Base.
Tableau is a source of evidence, not the final documentation format.

The MVP deliberately uses JSON, Markdown, YAML front matter, and plain Python.
It has no database, graph database, vector database, Tableau Server connection,
Confluence synchronization, or active MCP integration.

## Data flow

```text
TWB/TWBX
   ↓
tableau_doc.py
   ↓
knowledge/sources/tableau.json ──┐
                                ├─ knowledge_build.py
knowledge/manual/**/*.md ───────┘
                                      ↓
                             knowledge/markdown/
```

`tableau_doc.py` is only a Tableau metadata extractor and normalizer.
`knowledge_build.py` validates the complete file-based model, provides simple
dependency/impact functions, and renders disposable Markdown pages.

## Install

Extraction uses Python 3.10 or newer and only the standard library. The
Knowledge Base builder uses PyYAML:

```bash
python3 -m venv .venv-kb
source .venv-kb/bin/activate
pip install -r requirements-kb.txt
```

## Extract Tableau metadata

```bash
python3 tableau_doc.py path/to/workbooks --output knowledge
```

The command scans `.twb` and `.twbx` files and writes one generated source:

```text
knowledge/
└── sources/
    └── tableau.json
```

Useful options:

```bash
python3 tableau_doc.py path/to/workbooks \
  --output knowledge \
  --workbook "Superstore" \
  --strict
```

JSON is now mandatory. The former `--emit-json` and `--worksheet` options no
longer exist because a partial worksheet catalog is unsafe as a Knowledge Base.

## Normalized model

```json
{
  "schema_version": 2,
  "source_type": "tableau",
  "sources": [],
  "entities": [],
  "relations": [],
  "warnings": []
}
```

Initial entity types are `dashboard`, `visual`, `metric`, `field`,
`calculation`, `filter`, and `datasource`. Business Rules are human-authored.
Each Tableau worksheet becomes one Visual and may belong to zero or more
Dashboards. Metrics are marked `inferred`, not business-approved.

Relations use explicit IDs and include `contains`, `uses`, `displays`,
`affected_by`, `filters_on`, `calculated_by`, `depends_on`, `comes_from`, and
`same_source_field_as`. Reverse usage and impact are computed in memory.

## Add human context

Human content lives below `knowledge/manual` and is never written by the
extractor or renderer:

```text
knowledge/manual/
├── dashboards/
├── visuals/
├── metrics/
├── fields/
├── calculations/
├── filters/
├── datasources/
└── business-rules/
```

Example:

```markdown
---
id: metric:coverage-overview:revenue
type: metric
description: Revenue recognized in the selected period.
owner: Finance Analytics
business_rule_ids:
  - business-rule:recognized-revenue
relations:
  - type: affected_by
    to: business-rule:recognized-revenue
---

# Revenue

Use this metric for approved financial reporting.
```

Manual pages for Tableau-derived entities must reference an automatic ID. A
`business_rule` may be entirely manual. Broken IDs, duplicate pages, type
mismatches, and invalid relations fail validation.

## Validate and render Markdown

```bash
python3 knowledge_build.py knowledge --check
python3 knowledge_build.py knowledge
```

The builder recreates only `knowledge/markdown`. Every page separates
`Automatic metadata` from `Human context` and includes relationships and
reverse `Where used` links. Never edit generated Markdown.

## Safety and known limits

- TWBX packages are read without extracting contents to disk.
- Unsafe ZIP paths and embedded TWB files above 50 MiB are rejected.
- Tableau extracts, credentials, and connection data are not read.
- Only worksheet filters are modeled; dashboard actions are not.
- Layout, joins, relationships, Tableau order of operations, and SQL
  equivalence are not modeled.
- A Tableau rename can change a generated ID and orphan a manual page;
  validation reports it rather than discarding it.

## Tests

```bash
python3 -m unittest \
  tests.test_tableau_knowledge \
  tests.test_knowledge_build \
  tests.test_analytics_core -v
```

## Legacy components

`tableau_mcp.py`, `semantic_config_bootstrap.py`, and `MCP_SERVER.md` target the
former schema-v1 catalog and are not compatible with `tableau.json` v2. They
remain as historical code only. `analytics_core.py` is an independent
DuckDB/Parquet experiment and is not part of the Knowledge Base pipeline.
