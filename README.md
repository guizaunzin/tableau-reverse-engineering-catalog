# Tableau Reverse-Engineering Catalog

This project turns Tableau `.twb` and `.twbx` workbooks into a small,
navigable Markdown catalog focused on:

- calculated fields used by each worksheet;
- worksheet filters;
- readable formulas using Tableau captions;
- forward calculation dependencies;
- reverse field impact analysis;
- worksheets affected directly or indirectly by a field change.

It uses only the Python standard library and never connects to Tableau Server.

## Quick start

Python 3.10 or newer is required.

Generate documentation for one workbook:

```bash
python3 tableau_doc.py examples/sample_superstore.twb --output docs
```

Scan every `.twb` and `.twbx` below a directory:

```bash
python3 tableau_doc.py path/to/workbooks --output docs
```

Also generate the compact machine-readable representation:

```bash
python3 tableau_doc.py path/to/workbooks --output docs --emit-json
```

Limit the output to selected content:

```bash
python3 tableau_doc.py path/to/workbooks \
  --output docs \
  --workbook "Superstore" \
  --worksheet "Executive Overview"
```

Use `--worksheet` more than once to select multiple worksheets. Use `--strict`
to stop immediately when a workbook is invalid; without it, directory scans
continue and report the invalid files as warnings.

## Output

```text
docs/
├── README.md
└── superstore/
    ├── Superstore.md
    ├── Superstore - Worksheets.md
    ├── Superstore - Field Impact.md
    └── Superstore.json        # only with --emit-json
```

Each workbook produces only three Markdown files. Its overview file uses the
visible workbook name. `<Workbook> - Worksheets.md` contains a linked table of
contents and one anchored section per worksheet.
`<Workbook> - Field Impact.md` contains the compact field index followed by one
anchored section per relevant field.

The field impact document separates direct worksheet use from transitive
impact. Every indirect impact includes a dependency path, for example:

```text
Sales → Profit Ratio → Adjusted Profit
```

## How field usage is identified

The scanner reads field references from worksheet XML, including shelves,
encodings, filters, and sort expressions. These visual structures are not
documented individually; they are reduced to the relationship:

```text
Worksheet uses Field
```

Only fields used by a worksheet and their recursive calculation dependencies
are included. Unused calculations are omitted.

Raw formulas are retained in optional JSON. Markdown formulas replace internal
names such as `[Calculation_1001]` with visible captions such as
`[Profit Ratio]` when the mapping is unambiguous. Unknown or ambiguous
references are preserved and reported instead of guessed.

## Cross-workbook impact

Fields are linked across workbooks only when both workbooks expose the same
published datasource identity in `repository-location` and the same internal
field name. Matching captions alone are never considered sufficient.

## Safety and limitations

- `.twbx` packages are inspected without extracting their contents to disk.
- Tableau extracts, CSV files, images, and other packaged data are ignored.
- Unsafe ZIP paths and embedded TWB files larger than 50 MiB are rejected.
- Connection details and credentials are not extracted or documented.
- Only worksheet filters are analyzed.
- Dashboard actions, layout, joins, datasource documentation, SQL generation,
  Parquet data, LLM calls, and MCP serving are outside V1.
- Tableau XML varies by release. Unsupported references remain visible as
  warnings so they can become regression fixtures for later improvements.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers recursive impact, cycles, duplicate captions, shelf-token
normalization, safe cross-workbook matching, TWBX handling, path traversal, and
deterministic output.

## V2: local MCP catalog

V2 adds read-only MCP access to the compact JSON produced by V1. Generate JSON
catalogs first:

```bash
python3 tableau_doc.py path/to/workbooks --output docs --emit-json
```

The catalog loader and validation command use only the standard library:

```bash
python3 tableau_mcp.py --catalog docs --check
```

Starting the MCP server requires the optional official Python SDK:

```bash
pip install -r requirements-mcp.txt
python3 tableau_mcp.py --catalog docs
```

The server uses stdio and exposes four bounded, read-only tools:

- `search_catalog`: search workbooks, worksheets, and fields;
- `get_field_impact`: retrieve direct, indirect, and cross-workbook impact;
- `get_worksheet`: retrieve filters, calculations, and direct fields;
- `trace_dependencies`: trace upstream or downstream lineage with a depth cap.

No MCP tool reads TWB/TWBX files, queries Tableau Server, accesses business
data, or modifies the catalog. Copy `mcp_config.example.json` and replace the
placeholder paths with absolute local paths for the target MCP client.
