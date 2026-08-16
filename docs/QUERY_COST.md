# What a BQL query costs against the dataform store

**Measured, not estimated.** Every number below comes from
`data_ingestion/dataform/store.py`'s real physical layout, loaded with a synthetic corpus
of the same shape as production (10,000 documents, 6.4 KB of text each, 40,000 citations,
20,000 events, 128 MB). Each optimized query was asserted to return the *same row set* as
the naive one it replaces.

---

## 1. The finding

`docs/DATAFORM.md` prices `EXACT` predicates as "SQL equality/range, near-zero cost". That
was true of the physical schema it was written against, and false of ours: the store keeps
one indexed envelope column set (`id`, `source_system`, `source_id`, `doc_type`, `date`)
plus a `data` JSON blob holding everything else. So a BQL predicate on any other canonical
field — `document.jurisdiction`, `document.date_issued`, `document.media.text.plain_text` —
compiled to `json_extract(data, ...)`, which SQLite can only answer by **reading and
parsing every blob in the table**.

The consequence is not that predicates were slow. It is that **their cost was independent
of their selectivity**:

```
                                  rows surviving      naive      with an index
document.jurisdiction = "ca9"      769 / 10,000     25.78 ms        0.18 ms
document.jurisdiction = "zz"         0 / 10,000     24.83 ms        0.00 ms
                                                    ↑ same cost     ↑ tracks selectivity
```

That breaks the premise the optimizer's funnel is built on. Pushing a cheap deterministic
filter down is only worth doing if narrowing to 8% of the corpus costs ~8% of the work; if
every clause costs a full corpus scan regardless, the "cheap" head of the funnel costs a
fixed tax per clause before a single model call happens.

The fix is `data_ingestion/dataform/physical.py`: every field the query language can name
is exposed as a generated column and indexed, derived from
`query_language/schemas/dataform.json` so the two cannot drift. The blob remains the source
of truth; SQLite maintains the columns itself.

---

## 2. Cost per BQL construct

Median of 5 runs, warm cache. "naive" = `json_extract` against the blob; "optimized" = the
same query through the physical layer.

| BQL construct | naive | optimized | speedup | access path |
|---|---:|---:|---:|---|
| `Comparison =` (selective, 769 rows) | 24.69 ms | **0.18 ms** | 140× | scan → index seek |
| `Comparison =` (broad, 5,032 rows) | 25.45 ms | **1.18 ms** | 22× | scan → index seek |
| `Comparison >` on a date | 24.86 ms | **1.02 ms** | 24× | scan → index range |
| `InList` (4 values) | 25.83 ms | **0.78 ms** | 33× | scan → index seeks |
| `Between` | 25.30 ms | **0.55 ms** | 46× | scan → index range |
| `Like "prefix%"` (short text) | 1.52 ms | **0.02 ms** | 84× | scan → NOCASE index |
| `Like "%infix%"` (short text) | 1.56 ms | **0.14 ms** | 11× | scan → scan, no blob parse |
| `Like "%...%"` on a document body | 40.07 ms | — | — | scan (use `Fuzzy` instead) |
| `Fuzzy` prefilter on a document body | 40.21 ms | **0.25 ms** | 164× | scan → FTS5 MATCH |
| `Join` (1 hop, 10,000 rows out) | 28.60 ms | **4.25 ms** | 7× | driving-side scan → index |
| `Join` + filter on the right table | 36.74 ms | **2.07 ms** | 18× | scan → index seek |
| `Join` (2 hops) + filter | 40.56 ms | **0.34 ms** | 119× | scan → index seeks |
| `Aggregator` + `group_by` | 43.44 ms | **0.23 ms** | 192× | scan → index-ordered group |
| `Unnest` + `Comparison` | 25.49 ms | **0.01 ms** | 4,600× | `json_each` → side-table index |
| `Unnest` at element grain (55,622 out) | 38.04 ms | **11.59 ms** | 3× | `json_each` → side-table scan |
| `Unnest` over an object array | 31.86 ms | **5.25 ms** | 6× | `json_each` → side table |
| `And` of 3 exact predicates | 24.70 ms | **0.17 ms** | 142× | scan → index seek |
| `limit 10`, nothing matches | 24.35 ms | **0.00 ms** | ~5,800× | scan → index seek |
| projection of 3 `select` fields | 30.46 ms | **16.30 ms** | 2× | scan → scan of stored columns |
| full funnel head (2 exact + 1 fuzzy) | 25.89 ms | **0.22 ms** | 119× | scan → FTS ∩ index |

Two constructs are *unchanged by design*:

* **`Like "%infix%"`** cannot use a b-tree index in any database. It is now a scan of a
  narrow column instead of a scan of the blobs, which is where its 10× comes from. For
  long-form text, use `Fuzzy` (FTS5) instead — that is what the compiler emits anyway.
* **Element-grain `Unnest` in `select`** returns 55,622 rows; it is output-bound, not
  access-bound, so 3× is the whole available win.

---

## 3. The cost model, for the optimizer

Fitted across five tables spanning 400–40,000 rows and 0.2–64 MB of blob:

```
full scan (no usable index)   ≈  0.35 µs/row  +  0.33 ns per byte of blob scanned
index seek / range            ≈  0.30 µs per MATCHING row  +  ~0.05 ms fixed
FTS5 MATCH                    ≈  0.30 µs per hit
```

The distinction that matters is not "SQL is cheap". It is:

> **An indexed predicate costs `s × N`. An unindexed one costs `N`, plus the whole corpus in
> bytes, no matter what `s` is.**

`FieldSpec.index` (`btree` | `nocase` | `fts` | `none`) now carries this per field, and
`FieldSpec.is_indexed` answers it directly, so the optimizer never has to guess. A predicate
on a field with `index="none"` should be priced as a full table scan and ordered late among
the deterministic predicates, not early.

**Calibration handoff.** `_bespoke.SQL.seconds_per_unit = 1e-05 (PLACEHOLDER)` is one
constant for what are really two regimes, and it is ~30× too expensive for the seek case.
Suggested replacement:

```json
"SQL_SEEK":      {"seconds_per_unit": 3.0e-07, "measurement_status": "MEASURED"},
"SQL_SCAN_ROW":  {"seconds_per_unit": 3.5e-07, "measurement_status": "MEASURED"},
"SQL_SCAN_BYTE": {"seconds_per_unit": 3.3e-10, "measurement_status": "MEASURED"}
```

Measured on this box, warm page cache — so the scan figures are a *lower* bound. At the
bootstrapped-slice scale in the optimizer's `DEFAULT_STATS` (45,000 opinions at real
opinion length, ~1.35 GB), one unindexed predicate projects to **~0.46 s warm**, versus
**~1.1 ms** for the same predicate on an indexed column.

### Row hydration is not free either

| getting 10,000 candidate rows to a model | cost | per row |
|---|---:|---:|
| `SELECT id` only | 1.3 ms | 0.13 µs |
| raw `SELECT id, data`, no parsing | 19.9 ms | 2.0 µs |
| `json.loads` on each blob | 103.7 ms | 10.4 µs |
| `Store.list(Document, ...)` (Pydantic validate) | 91.5 ms | 9.1 µs |

Hydrating through `Store.list()` costs **more than the entire SQL scan**. An executor
feeding a semantic predicate should select the columns it needs, not validate whole models
— `Document` objects are for ingestion, not for the funnel.

---

## 4. What the physical layer builds

Driven entirely by `query_language/schemas/dataform.json`:

| schema declaration | physical structure | BQL construct it serves |
|---|---|---|
| `type: SCALAR` | generated `STORED` column + b-tree index | `Comparison`, `InList`, `Between`, `Join` |
| `type: TEXT` | generated column + `COLLATE NOCASE` index | `Like "prefix%"` |
| `type: TEXT, embeddable: true` | column in the table's FTS5 index | `Fuzzy` prefilter, `Like` on prose |
| a `List[...]` field in `models.py` | `table__field` side table, indexed | `Unnest` |
| a `Dict[...]` field (e.g. `external_ids`) | side table with `key` + `value` | cross-source lookup |

Two per-field overrides are available in the schema JSON:
`"storage": "stored" | "virtual"` and `"index": "btree" | "nocase" | "fts" | "none"`.
`document.media.text.plain_text` uses `"storage": "virtual"` — copying a document body into
its own row would double the corpus, and it is reached through FTS5 anyway.

Usage:

```python
store = Store()                 # generated columns + indexes: automatic, always current
store.save_many(records)
store.optimize_for_queries()    # FTS5 + unnest side tables: once, after a load
```

Generated columns are maintained by SQLite, so ingestion code needs no changes and cannot
forget. The FTS5 index and the side tables are **derived data rebuilt on demand rather than
by trigger**, because a bulk load would otherwise pay trigger cost on every insert — call
`optimize_for_queries()` when a load finishes. `DATAFORM_PHYSICAL=0` disables the whole
layer for an ingestion-only process.

```bash
python -m data_ingestion.dataform.physical              # print the derived mapping
python -m data_ingestion.dataform.physical --apply      # apply to the default database
python -m data_ingestion.dataform.physical --rebuild    # rebuild FTS + unnest tables
```

Anything in the schema that cannot be mapped is reported as `UNMAPPED` with a reason rather
than silently left behind, which doubles as a drift check between
`schemas/dataform.json` and `models.py`.

---

## 5. What it costs

| | baseline | with the physical layer |
|---|---:|---:|
| load 80,400 records | 2.31 s | 3.22 s (+39%) |
| database after load | 128.4 MB | 158.0 MB (+23%) |
| database after `optimize_for_queries()` | — | 187.8 MB (+46%) |
| `rebuild_derived()` for 10k documents | — | 0.81 s |

Where the extra 59 MB goes: generated `STORED` columns +14 MB, unnest side tables 16.4 MB,
b-tree indexes 16.1 MB, FTS5 12.8 MB. If ingest throughput ever matters more than query
latency, the cheapest things to drop are the FTS5 indexes on tables nobody searches by
prose (`event_fts` alone covers 20,000 rows) — set `"index": "nocase"` on those fields.

---

## 6. Open item for step 3

`optimizer/lower.py` renders a field into SQL with the BQL name as written:

```python
def _sql_ref(r: FieldRef) -> str:
    return pp_field_ref(r)
```

A canonical BQL path is not a column name, so the FROM clause it emits today is:

```sql
document AS doc JOIN proceeding AS p ON doc.proceeding_id = p.envelope.id
-- sqlite3.OperationalError: no such column: p.envelope.id
```

The left side resolves only because the physical layer now generates a `proceeding_id`
column; the right side cannot resolve at all, because SQLite has no three-part identifier.
Routing through the registry fixes both, and is the same one-line change that makes every
future pushdown land on an index instead of a scan:

```python
def _sql_ref(r: FieldRef, reg: schema.Registry, tables: dict[str, str]) -> str:
    spec = reg.get(f"{tables[r.source]}.{'.'.join(r.path)}")   # BQL name -> FieldSpec
    return f"{r.source}.{spec.sql_ref.split('.', 1)[1]}"       # alias.physical_column
    # ON doc.proceeding_id = p.id
```

`_sql_ref` is currently reached only from join conditions, because lowering deliberately
pushes nothing else into SQL. The pass that *does* push predicates down is where the cost
model in §3 starts paying: with `FieldSpec.is_indexed` it can push the index-reachable
predicates and leave the rest above the scan, instead of assuming all of them are cheap.

---

## 7. Fixed along the way

`Event.date` could never hold a date. Written as `date: Optional[date] = None`, the field
name shadowed the `date` type inside the class body, and because `models.py` uses
`from __future__ import annotations`, Pydantic resolved the deferred annotation against
that shadow and typed the field `NoneType` — so constructing an `Event` with a date raised
a `ValidationError`, and `store.py`'s indexed `date` column was `NULL` for every event ever
loaded. The field now annotates against an unshadowed `_Date` alias and keeps its name.

---

## 8. Reproducing

The benchmark harness is not checked in (it builds a 128 MB corpus). To re-measure:
build a corpus through `Store`, then time each construct in both forms against the same
database, asserting equal row sets. The numbers above are from `sqlite 3.53.0`,
`Python 3.14.4`, Apple silicon, warm page cache.
