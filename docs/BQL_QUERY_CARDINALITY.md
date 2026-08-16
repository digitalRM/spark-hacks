# How BQL Operators Change Query Size

BQL operators do **not** delete or modify records in the underlying database. They
change the **cardinality**: the number of temporary records flowing through a query.
The database file stays unchanged while the query's working set grows or shrinks at
each operator.

## Statistics to Report

For each query operator, report:

- `rows_in`: records received by the operator
- `rows_out`: records passed to the next operator
- `model_calls`: model requests made for the incoming records
- `seconds`: time spent in the operator

Model calls normally follow `rows_in`, not `rows_out`, because the system must examine
a record before it knows whether the record survives.

## Effect of Each Operator

| Operator | Effect on the temporary result set |
|---|---|
| `Scan` | Reads candidate rows from the database. |
| Exact filter | Shrinks the set: `rows_out = rows_in × selectivity`. |
| `AND` | Applies filters successively, shrinking the set after each condition. |
| `OR` / `Union` | Combines branch results and may grow before duplicate removal. |
| `Retrieve(top_k)` | Caps the set at `min(rows_in, top_k)`. |
| `Expand` / `UNNEST` | Grows the set: `rows_out = parent rows × average elements per parent`. |
| Semantic filter | Shrinks the set: `rows_out = rows_in × semantic selectivity`. |
| `Collapse` | Deduplicates matching elements back into their parent cases or documents. |
| `Join` | May grow for one-to-many relationships; key-preserving joins may leave the driving-table cardinality unchanged. |
| `Materialize` | Produces a derived value such as pages, parsed text, or audio segments; optional-source coverage may first shrink the set. |
| `Aggregate` | Shrinks records into groups or a single summary row. |
| `Limit 10` | Caps the output at ten records. |
| `Project` | Changes the returned columns without changing the number of rows. |

## Example Query Funnel

The repository's flagship static estimate changes size as follows:

```text
Database candidates                 32,000
  ↓ SQL scan/filter                    262
  ↓ vector retrieval                   200
  ↓ text semantic filter                68
  ↓ cases with available audio           9
  ↑ expand into audio segments        2,829
  ↓ audio semantic filter               170
  ↓ collapse back into cases              9
  ↓ cases with scanned pages              5
  ↑ expand into individual pages        258
  ↓ visual semantic filter                 5
  ↓ collapse back into cases             ~3
  ↓ limit 10                              ~3
```

Query size does not only decrease. `Expand` can temporarily make the working set much
larger. For example:

```text
9 cases × approximately 320 audio segments = 2,880 candidate segments
```

The semantic filter examines those segments. `Collapse` then turns the surviving
segments back into distinct cases.

## Recommended Explanation

> BQL operators do not alter the underlying database. They progressively transform a
> temporary working set. Cheap filters reduce candidate records early, expansion
> operators temporarily multiply records into pages or audio segments, semantic filters
> narrow those elements, and collapse operators return the query to case-level results
> before applying the final limit.

## Current Measurement Limitation

The current funnel records row counts rather than byte sizes. Measuring physical query
memory would additionally require:

- `bytes_in`
- `bytes_out`
- peak working-set memory
- temporary spill-to-disk bytes

The example funnel is a static estimate backed by placeholder corpus statistics. It
should be labeled **predicted**, not measured, until the executor runs it against the
ingested corpus and emits observed stage telemetry.
