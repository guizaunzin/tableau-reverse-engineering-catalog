# MCP Server: Implementation and Adoption Guide

> **Legacy:** this server consumes the former schema-v1 Tableau catalog and is
> not compatible with the Semantic Knowledge Base v2. It is intentionally not
> being adapted in the current MVP.

## Executive summary

The MCP server turns two controlled sources into tools that an LLM can query:

1. the catalog extracted from Tableau workbooks, which describes formulas,
   dependencies, worksheets, dashboards, and metric contracts;
2. an approved semantic configuration that maps business names to physical
   tables and columns, descriptions, and aggregation policies.

The goal is not to let the LLM write unrestricted SQL. The server generates SQL
only after confirming that the datasource, dimensions, indicators, and
aggregations belong to the approved configuration.

## What has been built

The server exposes eight read-only tools:

| Tool | Purpose |
|---|---|
| `search_catalog` | Find Tableau workbooks, worksheets, and fields. |
| `get_field_impact` | Inspect a field's dependencies and impact. |
| `get_worksheet` | Inspect a worksheet's filters, calculations, and fields. |
| `get_metric_contract` | Retrieve the semantic recipe extracted for a Tableau metric. |
| `trace_dependencies` | Traverse upstream or downstream dependencies. |
| `get_dimensions` | List authorized dimensions with physical columns and descriptions. |
| `get_indicators` | List indicators, descriptions, and aggregation policies. |
| `get_data` | Validate a request, generate safe SQL, and return JSON results. |

### `get_dimensions()`

This tool receives a configured datasource and returns its available
dimensions, including the business name, physical column, and description.

### `get_indicators()`

This tool returns the indicators, their descriptions, default aggregation, and
allowed aggregations. For example, `Revenue` may use `sum` by default while
also allowing `avg`, whereas `Orders` may accept only `count_distinct`.

### `get_data()`

This tool receives a datasource, dimensions, indicators, optional aggregation
overrides, and a limit between 1 and 1,000 rows. Its response contains the SQL,
parameters, effective aggregation policies, row count, and JSON-serializable
data.

### Input validation

Before opening a database connection, the server validates:

- that the datasource exists;
- that dimensions and indicators exist and contain no duplicates;
- the maximum of 10 dimensions and 20 indicators;
- each requested aggregation against the indicator's allowlist;
- the result row limit;
- the safe format of every identifier defined in the configuration.

### Secure SQL generation

Users and LLMs never provide physical identifiers directly. Business names are
resolved to identifiers that were approved in the configuration. SQL
generation:

- accepts only validated identifiers;
- applies only `SUM`, `AVG`, `MIN`, `MAX`, `COUNT`, and `COUNT DISTINCT`;
- adds `GROUP BY` for the selected dimensions;
- uses a parameterized `LIMIT ?`;
- opens SQLite with `mode=ro`;
- does not accept free-form SQL fragments, expressions, clauses, or filters.

An input such as `Region; DROP TABLE sales` does not match a configured
dimension and is rejected before execution.

## How the components fit together

```text
TWB/TWBX
   │
   ▼
tableau_doc.py ──► JSON catalog ──► formulas, lineage, and metric contracts
                                           │
semantic_config.json ──► approved names ───┤
                                           ▼
                                    tableau_mcp.py
                                      │         │
                                      │         └─► get_data() ──► read-only SQLite
                                      └─► documentation tools
```

The catalog answers, "How does Tableau define and use this metric?" The
semantic configuration answers, "Where are the approved physical data and
which operations are allowed?" The two parts are complementary, but they are
not yet reconciled automatically.

## Semantic configuration

Copy `semantic_config.example.json` to `semantic_config.json`. Do not place
passwords, tokens, or credentials in this file.

```json
{
  "version": 1,
  "datasources": {
    "Commercial Metrics": {
      "description": "Approved semantic source.",
      "table": "commercial_metrics",
      "connection": {
        "driver": "sqlite",
        "database": "data/commercial_metrics.db"
      },
      "dimensions": {
        "Region": {
          "column": "region",
          "description": "Approved reporting region."
        }
      },
      "indicators": {
        "Revenue": {
          "column": "revenue",
          "description": "Gross recognized revenue.",
          "default_aggregation": "sum",
          "allowed_aggregations": ["sum", "avg"]
        }
      }
    }
  }
}
```

Names such as `Region` and `Revenue` form the contract presented to the LLM.
The `column` and `table` values are physical identifiers controlled by the
data team.

### Generate a draft automatically

After generating workbook JSON catalogs with `--emit-json`, create an initial
semantic configuration draft:

```bash
python3 semantic_config_bootstrap.py docs \
  --output semantic_config.draft.json
```

The bootstrap script:

- groups metric contracts by visible Tableau datasource;
- suggests dimensions from the detected worksheet grain;
- suggests physical column names from Tableau field names;
- converts base measures and simple formulas such as `SUM([Members])`;
- infers draft aggregation policies from Tableau contexts;
- places compound formulas, LODs, and table calculations in
  `unsupported_metric_contracts`.

Every generated datasource is marked:

```json
"review_status": "needs_review"
```

The MCP server refuses to load such a datasource. Before using the draft:

1. confirm the physical table and database path;
2. confirm every suggested physical column;
3. replace generated descriptions with approved business definitions;
4. confirm default and allowed aggregation policies;
5. review `unsupported_metric_contracts`;
6. change the datasource status to `"review_status": "approved"`;
7. save the reviewed file as `semantic_config.json`.

The bootstrap is intentionally conservative. It accelerates mapping but does
not claim that Tableau captions are physical database columns.

## Running locally

Generate the catalog first:

```bash
python3 tableau_doc.py /path/to/workbooks --output docs --emit-json
```

Install the optional SDK:

```bash
python3 -m venv .venv-mcp
source .venv-mcp/bin/activate
pip install -r requirements-mcp.txt
```

Validate the catalog and configuration without starting the server:

```bash
python3 tableau_mcp.py \
  --catalog docs \
  --semantic-config semantic_config.json \
  --check
```

Start the server:

```bash
python3 tableau_mcp.py \
  --catalog docs \
  --semantic-config semantic_config.json
```

To configure an MCP client, copy `mcp_config.example.json`, replace the
placeholders with absolute paths, and restart the client.

## Expected LLM workflow

To answer "What was revenue by region?":

1. call `get_metric_contract` to understand Tableau's definition of
   `Revenue`;
2. call `get_dimensions` to confirm that `Region` is available;
3. call `get_indicators` to confirm `Revenue` and its default aggregation;
4. call `get_data` with `dimensions=["Region"]` and
   `indicators=["Revenue"]`;
5. explain the result together with the metric contract and known
   limitations.

The LLM should not skip directly to `get_data` and invent field names.

## Current state and limitations

- Metric contracts do not yet capture the complete physical model, joins, or
  Tableau's full order of operations.
- The executor currently supports read-only SQLite.
- `get_data` does not accept free-form filters; this is intentional in the
  first secure implementation.
- Corporate authentication, persistent audit logging, timeouts, and cost
  controls are not yet implemented.
- Equivalence with Tableau must be validated one metric at a time.

These limitations do not prevent a controlled pilot. They clearly define the
difference between a prototype and a production service.
