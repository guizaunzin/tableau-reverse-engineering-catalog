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
knowledge/sources/tableau/*.json ──┐
                                  ├─ knowledge_build.py
knowledge/manual/**/*.md ─────────┘
                                      ↓
                       knowledge/markdown/workbooks/
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

The command scans `.twb` and `.twbx` files and writes one generated source per
workbook:

```text
knowledge/
└── sources/
    └── tableau/
        ├── overview-dashboard.json
        └── sales-dashboard.json
```

Running the extractor for one workbook updates only that workbook's JSON and
preserves the other generated sources. The builder reads every JSON below
`knowledge/sources` and regenerates Markdown for the combined Knowledge Base.
On the first incremental run, the former aggregate `sources/tableau.json` is
split automatically into per-workbook files.

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
`calculation`, `filter`, `datasource`, and `business_rule`. Business Rules may
be inferred automatically from Tableau evidence or authored manually. Each
inferred rule is explicitly marked as unapproved and retains its source formula.
Each Tableau worksheet becomes one Visual and may belong to zero or more
Dashboards. Metrics are marked `inferred`, not business-approved.

Relations use explicit IDs and include `contains`, `uses`, `displays`,
`affected_by`, `filters_on`, `calculated_by`, `depends_on`, `comes_from`, and
`same_source_field_as`, and `implemented_by`. Reverse usage and impact are
computed in memory.

### Inferred business rules

The extractor conservatively proposes one inferred rule for a calculated
Metric that is actually displayed in a Visual when its Tableau formula exposes
meaningful conditional or aggregation logic (`IF`, `CASE`, or LOD expressions).
It does not turn every filter into a rule and skips common presentation helpers,
labels, signs, color calculations, and simple parameter selectors.

An inferred rule records its generated statement, rule kind, confidence,
inference method, exact Tableau formula, and evidence Calculation IDs. It is
linked to the Metric and its dependencies, but is always rendered as
**Inferred Rules** with a verification warning. This is discoverable technical
evidence, not an approved business definition; a human-authored
`business_rule` remains the mechanism for approved context. Field mentions are
stored separately from the statement and underlined when the rule is rendered,
without changing the original Tableau formula.

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

The builder recreates only `knowledge/markdown`. Its top-level `README.md`
lists the available workbooks, and each workbook receives an independent
documentation tree:

```text
knowledge/markdown/
├── README.md
└── workbooks/
    ├── overview-dashboard/
    │   ├── README.md
    │   ├── dashboards/
    │   ├── visuals/
    │   ├── metrics/
    │   ├── fields/
    │   ├── calculations/
    │   ├── filters/
    │   ├── datasources/
    │   ├── business-rules/
    │   └── assets/layouts/
    └── sales-dashboard/
        └── ...
```

Every page separates
`Automatic metadata` from `Human context` and includes relationships and
reverse `Where used` links. Each workbook `README.md` starts with Dashboards
and indexes every entity type belonging to that workbook. Cross-workbook
relations remain available in the combined in-memory model for search and
impact analysis, but are not rendered as broken links inside an isolated
workbook tree. Never edit generated Markdown.

Rebuilds publish files into the existing Markdown tree and remove obsolete
outputs afterward. The live directory is not deleted during publication, so
open previews and file watchers do not temporarily lose workbook subdirectories.

Workbook indexes follow a human-navigation order: Dashboards, Data Sources,
Inferred Rules, Visuals, Metrics, Calculations, Filters, and Fields. Dashboards
are rendered as a compact two-column card grid. Navigable entity references use
color-coded pills throughout the generated documentation: green for Metrics,
violet for Calculations, blue for Filters, and gray for Fields. Styles are
inline so every Markdown file remains self-contained and requires no shared CSS
or external assets. Generated pages with two or more level-two sections receive
a Contents index at the top.
Every entity page also exposes a `top` anchor and a backlink to its workbook
README. Relationship and reverse-usage links target that anchor explicitly so
navigation never reuses a previous scroll position near the end of the page.

Dashboard pages additionally contain:

- a horizontally scrollable Summary strip with compact count cards for data
  sources, visuals, fields, calculations, filters, parameters, metrics,
  inferred rules, and approved business rules when present;
- the proportional Dashboard Layout before Inferred Rules whenever both are
  available;
- a numbered Visuals inventory whose titles link directly to each Visual, with
  color-coded metrics, calculations, filters, and fields;
- when Tableau exposes a valid fixed desktop layout, a proportional SVG
  wireframe in that workbook's `assets/layouts` directory.

Visual pages act as a data dictionary and list their dashboards, metrics,
fields, directly used calculations, filters, and data sources. Entity names
link to their dedicated pages. Parameters remain in the normalized dependency
model but are intentionally omitted from individual Visual pages because they
are workbook-scoped. Metric business definitions appear only when human
metadata supplies one, and complete Tableau formulas are rendered as code on
the dedicated Calculation pages. The numbering in a dashboard's Visuals
inventory matches its SVG wireframe.

Layout metadata remains embedded in the automatic Dashboard entity rather
than becoming a separate entity hierarchy. It stores the Tableau coordinate
space, fixed dashboard size, drawable items, and extraction warnings. SVG
files and all Markdown remain disposable builder output.

## Safety and known limits

- TWBX packages are read without extracting contents to disk.
- Unsafe ZIP paths and embedded TWB files above 50 MiB are rejected.
- Tableau extracts, credentials, and connection data are not read.
- Identical worksheet filters are represented once per workbook and linked to
  every affected Visual.
- Tableau Action filters are ignored, and fields whose names contain the word
  `Dummy` are excluded from the semantic model.
- Dashboard layout is best effort and only rendered for a valid fixed desktop
  size. It draws known visual and auxiliary zones but does not reconstruct
  containers, backgrounds, the chart itself, device layouts, tiled/floating
  behavior, or z-order. Missing or ambiguous zones produce warnings and never
  invented positions.
- Joins, relationships, Tableau order of operations, and SQL equivalence are
  not modeled.
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
former schema-v1 catalog and are not compatible with the schema-v2 source
documents. They remain as historical code only. `analytics_core.py` is an independent
DuckDB/Parquet experiment and is not part of the Knowledge Base pipeline.
