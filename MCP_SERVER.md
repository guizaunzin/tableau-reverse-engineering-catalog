# MCP Server: Implementation and Adoption Guide

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

## Recommended next steps at work

### 1. Select a small pilot

Choose an important dashboard with two or three metrics and known Tableau
reference values.

### 2. Define owners

For each metric, identify its business owner, data owner, approved datasource
and table, definition, default aggregation, exceptions, and authorized
dimensions.

### 3. Populate the configuration

Map only approved fields. A small allowlist makes the behavior safer and
easier to validate.

### 4. Validate against Tableau

Compare queries across known date and dimension combinations. Record the
Tableau value, MCP value, filters, difference, root cause, and owner approval.
A metric should be presented as equivalent only after this validation.

### 5. Select the corporate adapter

The current executor supports read-only SQLite. For production, the team should
select Snowflake, BigQuery, SQL Server, or another warehouse. The adapter
should:

- use credentials managed outside the configuration;
- execute with a read-only role;
- apply timeouts, cost controls, and row limits;
- retain the same validation and restricted compiler;
- record audit events without storing sensitive results.

### 6. Run a user pilot

Test whether the LLM selects the correct metric, uses the default aggregation,
asks for missing context, states limitations, and reproduces validated values.

## How to present this to your manager

A concise narrative:

> People currently treat Tableau as a golden source, but an LLM does not know
> the rules that produced those numbers. We built a layer that extracts metric
> definitions from Tableau and exposes only approved dimensions, indicators,
> and aggregations. The LLM does not write unrestricted SQL: it requests
> business concepts, the server validates those concepts, and then generates a
> bounded, read-only query. The next step is to validate a small set of metrics
> against a real dashboard and connect the same mechanism to our corporate
> warehouse.

For a demonstration:

1. show `get_metric_contract("Revenue")`;
2. show `get_indicators()` and the `sum` policy;
3. try an invalid dimension and show the rejection;
4. execute a valid query grouped by `Region`;
5. compare the result with a known Tableau value.

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
