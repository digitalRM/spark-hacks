"""Physical layer: makes BQL field paths reachable by index instead of by full scan.

`store.py` keeps one indexed envelope column set plus a `data` JSON blob. That is the
right shape for ingestion, but it means every BQL predicate on a canonical field --
`document.jurisdiction`, `document.date_issued`, `document.media.text.plain_text` --
compiles to `json_extract(data, ...)`, which SQLite can only answer with a full table
scan. Measured on a 10k-document / 128 MB corpus (see docs/QUERY_COST.md):

    json_extract predicate    26-42 ms   ALWAYS the whole table + whole blob
    indexed column            0.2-2 ms   proportional to MATCHING rows only

The scan cost does not depend on how selective the predicate is, so the funnel head that
step 3's optimizer relies on (push cheap deterministic filters DOWN, run models on the
survivors) collapses: every clause costs a full corpus scan before a single model runs.

This module closes that gap without changing the ingestion contract. Driven entirely by
`query_language/schemas/dataform.json` -- the same file the compiler and the optimizer
read -- it derives, for every queryable field:

    scalar field   -> a generated column (the JSON stays the source of truth) + an index
    long-form text -> an FTS5 index, so `like`/`fuzzy` prefilters are token lookups
    array field    -> a side table of its elements, so `unnest` is an indexed lookup

Generated columns are maintained by SQLite itself, so ingestion code needs no changes and
cannot drift. The FTS5 index and the unnest side tables are derived data: they are built
by `rebuild_derived()` after a load, not per row, because a bulk load pays for the
triggers on every insert while a query workload pays once.

Cost: `apply()` is one PRAGMA + a few DDL statements per table (milliseconds, idempotent).
`rebuild_derived()` is one pass over the corpus (~0.9 s for 10k documents).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
import typing
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Optional, Type, get_args, get_origin

from pydantic import BaseModel

from .models import ALL_MODELS

SCHEMA_PATH = (Path(__file__).resolve().parents[2] / "query_language" / "schemas" / "dataform.json")

# Columns store.py already writes and indexes. A BQL path that resolves to one of these
# maps onto it instead of generating a duplicate.
BASE_COLUMNS: frozenset[str] = frozenset({"id", "source_system", "source_id", "doc_type", "date", "data"})

# BQL path -> existing physical column, where store.py already persists exactly that value.
# `date` is store.py's `_date_value()` coalesce, which for Event resolves to Event.date.
BASE_PATH_MAP: dict[tuple[str, ...], str] = {
    ("envelope", "id"): "id",
    ("envelope", "source_system"): "source_system",
    ("envelope", "source_id"): "source_id",
    ("doc_type",): "doc_type",
    ("date",): "date",
}

# How store.py's own DDL indexes those columns. `source_id` is only the second column of
# idx_{table}_source, so a predicate on it alone cannot use that index -- the optimizer has
# to know that, or it will price a scan as a seek.
BASE_INDEX: dict[str, str] = {
    "id": "btree",            # PRIMARY KEY
    "source_system": "btree",  # leftmost column of idx_{table}_source
    "source_id": "none",       # non-leftmost; not independently searchable
    "doc_type": "btree",       # idx_{table}_doc_type
    "date": "btree",           # idx_{table}_date
}

MODELS_BY_TABLE: dict[str, Type[BaseModel]] = {m.__name__.lower(): m for m in ALL_MODELS}


# --------------------------------------------------------------------------- #
# Path resolution against the Pydantic models (the step-1 <-> step-2 contract)
# --------------------------------------------------------------------------- #
def _unwrap(annotation: Any) -> Any:
    """Strip Optional/Union-with-None down to the single real annotation."""
    origin = get_origin(annotation)
    if origin is typing.Union or isinstance(annotation, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


def resolve_path(model: Type[BaseModel], path: tuple[str, ...]) -> tuple[str, Any]:
    """Resolve a BQL `FieldRef.path` against the canonical models.

    Returns ``(kind, annotation)`` where kind is ``scalar`` | ``array`` | ``object``.
    Raises KeyError when the path does not exist -- which is the useful failure: it means
    `schemas/dataform.json` and `models.py` have drifted apart. Free (introspection only).
    """
    current: Any = model
    for i, segment in enumerate(path):
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            raise KeyError(f"{model.__name__}: {'.'.join(path[:i])} is not a nested model, "
                           f"cannot descend to {segment!r}")
        fields = current.model_fields
        if segment not in fields:
            raise KeyError(f"{model.__name__}: no field {'.'.join(path[:i + 1])!r}")
        current = _unwrap(fields[segment].annotation)
    origin = get_origin(current)
    if origin in (list, set, tuple):
        return "array", _unwrap((get_args(current) or (Any,))[0])
    if isinstance(current, type) and issubclass(current, BaseModel):
        return "object", current
    if origin is dict or current is dict:
        return "object", current
    return "scalar", current


# --------------------------------------------------------------------------- #
# The derived physical plan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ColumnPlan:
    """One BQL scalar field promoted to a queryable column."""
    field: str            # "document.jurisdiction" (the BQL name)
    table: str
    column: str           # physical column name
    json_path: str        # "$.jurisdiction"
    mode: str             # STORED | VIRTUAL | BASE (BASE = store.py already writes it)
    index: str            # btree | nocase | none
    field_type: str       # SCALAR | TEXT | ...

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def is_generated(self) -> bool:
        return self.mode != "BASE"


@dataclass(frozen=True)
class FtsPlan:
    """Long-form text fields of one table, indexed together in one FTS5 table."""
    table: str
    fts_table: str
    columns: tuple[str, ...]          # physical (generated) column names
    fields: tuple[str, ...]           # BQL names, positionally matching `columns`


@dataclass(frozen=True)
class UnnestPlan:
    """One array field materialized as a side table so `unnest` is an indexed lookup."""
    field: str
    table: str
    side_table: str
    json_path: str
    element_kind: str                 # scalar | object | pair (a JSON map: key + value)

    @property
    def qualified_value(self) -> str:
        return f"{self.side_table}.value"

    @property
    def is_map(self) -> bool:
        return self.element_kind == "pair"


@dataclass
class PhysicalPlan:
    columns: list[ColumnPlan] = dc_field(default_factory=list)
    fts: list[FtsPlan] = dc_field(default_factory=list)
    unnest: list[UnnestPlan] = dc_field(default_factory=list)
    unmapped: list[tuple[str, str]] = dc_field(default_factory=list)  # (field, why)

    def column_for(self, bql_field: str) -> Optional[ColumnPlan]:
        """The physical column backing a BQL field name, or None if it has none. Free."""
        return next((c for c in self.columns if c.field == bql_field), None)

    def unnest_for(self, bql_field: str) -> Optional[UnnestPlan]:
        return next((u for u in self.unnest if u.field == bql_field), None)

    def fts_for(self, table: str) -> Optional[FtsPlan]:
        return next((f for f in self.fts if f.table == table), None)

    def tables(self) -> list[str]:
        return sorted({c.table for c in self.columns})


INDEX_KINDS = ("btree", "nocase", "fts", "none")


def _index_kind(field_type: str, embeddable: bool, override: str | None) -> str:
    """Default indexing rule; `index` in the schema JSON overrides it.

    SCALAR         -> btree. Equality/range/IN all become index seeks.
    TEXT           -> nocase btree, so prefix `like "Smith%"` is an index seek too
                      (SQLite's LIKE optimization only fires on a NOCASE-collated index).
    TEXT+embeddable-> fts. `embeddable` marks prose in the schema; it is searched by token
                      through FTS5, and a b-tree over it would buy nothing for `%infix%`.
    """
    if override:
        if override not in INDEX_KINDS:
            raise ValueError(f"unknown index kind {override!r}; use one of {INDEX_KINDS}")
        return override
    if field_type == "SCALAR":
        return "btree"
    if field_type == "TEXT":
        return "fts" if embeddable else "nocase"
    return "none"


def build_plan(schema_path: Path | str | None = None) -> PhysicalPlan:
    """Derive the physical plan from the schema JSON + the canonical models. Free.

    Every field in the schema is classified; anything that cannot be mapped is recorded in
    `plan.unmapped` with a reason rather than silently dropped.
    """
    raw = json.loads(Path(schema_path or SCHEMA_PATH).read_text())
    plan = PhysicalPlan()
    fts_cols: dict[str, list[tuple[str, str]]] = {}
    seen: set[tuple[str, str]] = set()

    for spec in raw.get("fields", []):
        name: str = spec["name"]
        table, _, dotted = name.partition(".")
        path = tuple(dotted.split(".")) if dotted else ()
        ftype = spec.get("type", "SCALAR")
        model = MODELS_BY_TABLE.get(table)
        if model is None or not path:
            plan.unmapped.append((name, f"no canonical model for table {table!r}"))
            continue
        try:
            kind, _annotation = resolve_path(model, path)
        except KeyError as exc:
            plan.unmapped.append((name, str(exc)))
            continue

        if kind == "array":
            # A set-valued field: `unnest` over it, or a quantified fuzzy() at parent grain.
            element = "object" if isinstance(_annotation, type) and issubclass(_annotation, BaseModel) else "scalar"
            plan.unnest.append(UnnestPlan(
                field=name, table=table, side_table=f"{table}__{'_'.join(path)}",
                json_path="$." + ".".join(path), element_kind=element))
            continue
        if kind == "object":
            if _annotation is dict or get_origin(_annotation) is dict:
                # A JSON map (e.g. envelope.external_ids): queryable as key/value pairs,
                # which is exactly how cross-source identity resolution reads it.
                plan.unnest.append(UnnestPlan(
                    field=name, table=table, side_table=f"{table}__{'_'.join(path)}",
                    json_path="$." + ".".join(path), element_kind="pair"))
                continue
            plan.unmapped.append((name, "resolves to a nested model; its leaves map individually"))
            continue

        base = BASE_PATH_MAP.get(path)
        column = base or path[-1]
        if column in BASE_COLUMNS and not base:
            # leaf name collides with a store.py column but is not the same value
            column = "_".join(path[-2:]) if len(path) > 1 else f"{column}_value"
        if (table, column) in seen:
            column = "_".join(path[-2:])          # disambiguate two leaves with one name
        seen.add((table, column))

        embeddable = bool(spec.get("embeddable", False))
        index = BASE_INDEX[base] if base else _index_kind(ftype, embeddable, spec.get("index"))
        # STORED duplicates the value into the row, which costs bytes but removes the
        # json_extract from every read -- worth it for the short fields that end up in a
        # SELECT list. The exception is a field holding a whole document body: copying that
        # doubles the corpus, so the schema marks it `"storage": "virtual"` and it is
        # reached through FTS5 instead of through the column.
        mode = "BASE" if base else str(spec.get("storage", "stored")).upper()
        if mode not in ("STORED", "VIRTUAL", "BASE"):
            raise ValueError(f"{name}: unknown storage {mode!r}; use 'stored' or 'virtual'")
        plan.columns.append(ColumnPlan(field=name, table=table, column=column,
                                       json_path="$." + ".".join(path), mode=mode,
                                       index=index, field_type=ftype))
        if index == "fts":
            fts_cols.setdefault(table, []).append((column, name))

    for table, cols in fts_cols.items():
        plan.fts.append(FtsPlan(table=table, fts_table=f"{table}_fts",
                                columns=tuple(c for c, _ in cols),
                                fields=tuple(f for _, f in cols)))
    return plan


_PLAN_CACHE: dict[str, PhysicalPlan] = {}


def load_plan(schema_path: Path | str | None = None) -> PhysicalPlan:
    key = str(Path(schema_path or SCHEMA_PATH).resolve())
    if key not in _PLAN_CACHE:
        _PLAN_CACHE[key] = build_plan(schema_path)
    return _PLAN_CACHE[key]


# --------------------------------------------------------------------------- #
# Applying the plan
# --------------------------------------------------------------------------- #
def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of `table`, empty if it does not exist.

    `table_xinfo`, not `table_info`: the latter omits generated columns entirely, so an
    already-optimized table would look unoptimized and every open would try to re-add them.
    """
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def generated_column_ddl(col: ColumnPlan, *, mode: str | None = None) -> str:
    """The column definition for CREATE TABLE (STORED ok) or ALTER TABLE (VIRTUAL only)."""
    return (f'"{col.column}" TEXT GENERATED ALWAYS AS '
            f"(json_extract(data, '{col.json_path}')) {mode or col.mode}")


def create_table_columns(table: str, plan: PhysicalPlan | None = None) -> str:
    """Generated-column definitions to inline into a fresh CREATE TABLE. Free.

    Used by store.py so new databases get STORED columns; existing databases get the
    VIRTUAL equivalent via `apply()`, which is all ALTER TABLE can add.
    """
    plan = plan or load_plan()
    defs = [generated_column_ddl(c) for c in plan.columns if c.table == table and c.is_generated]
    return (",\n    " + ",\n    ".join(defs)) if defs else ""


def apply(conn: sqlite3.Connection, plan: PhysicalPlan | None = None) -> dict[str, int]:
    """Add every missing generated column and index. Idempotent; safe on a loaded database.

    Cost: one PRAGMA per table plus one DDL statement per missing column/index. Adding a
    VIRTUAL column is metadata-only (no table rewrite); each index costs one pass over the
    table it belongs to.
    """
    plan = plan or load_plan()
    added_cols = added_idx = 0
    for col in plan.columns:
        if not col.is_generated:
            continue
        have = _existing_columns(conn, col.table)
        if not have:
            continue                      # table not created yet; _ensure_schema will
        if col.column not in have:
            # ALTER TABLE cannot add STORED, only VIRTUAL -- same query plans, the
            # difference is projection cost, not index reachability.
            conn.execute(f"ALTER TABLE {col.table} ADD COLUMN "
                         + generated_column_ddl(col, mode="VIRTUAL"))
            added_cols += 1
        if col.index in ("btree", "nocase"):   # "fts"/"none" get no b-tree of their own
            collate = " COLLATE NOCASE" if col.index == "nocase" else ""
            conn.execute(f'CREATE INDEX IF NOT EXISTS "ix_{col.table}_{col.column}" '
                         f'ON {col.table}("{col.column}"{collate})')
            added_idx += 1
    for f in plan.fts:
        have = _existing_columns(conn, f.table)
        if not have:
            continue
        cols = ", ".join(f'"{c}"' for c in f.columns)
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {f.fts_table} USING fts5("
                     f"{cols}, content='{f.table}', content_rowid='rowid', "
                     f"tokenize='porter unicode61')")
    for u in plan.unnest:
        # `key` is NULL for arrays and the map key for JSON objects; `ord` keeps element
        # order so an `unnest` result can be reported back with its original position.
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{u.side_table}" ('
                     "owner_id TEXT NOT NULL, ord INTEGER NOT NULL, key TEXT, value TEXT, "
                     "PRIMARY KEY (owner_id, ord)) WITHOUT ROWID")
        conn.execute(f'CREATE INDEX IF NOT EXISTS "ix_{u.side_table}_value" ON "{u.side_table}"(value)')
        if u.is_map:
            conn.execute(f'CREATE INDEX IF NOT EXISTS "ix_{u.side_table}_key" '
                         f'ON "{u.side_table}"(key, value)')
    conn.commit()
    return {"columns_added": added_cols, "indexes_ensured": added_idx,
            "fts_tables": len(plan.fts), "unnest_tables": len(plan.unnest)}


def rebuild_derived(conn: sqlite3.Connection, plan: PhysicalPlan | None = None,
                    tables: Iterable[str] | None = None) -> dict[str, int]:
    """Rebuild the FTS5 indexes and unnest side tables from the current blobs.

    Derived data, so this runs AFTER a load rather than on every insert: triggers would tax
    every one of a bulk load's inserts, and a load is write-heavy while the query workload
    reads. Idempotent -- run it again after any further ingestion.

    Cost: one pass over each covered table (~0.9 s per 10k documents).
    """
    plan = plan or load_plan()
    only = set(tables) if tables else None
    counts: dict[str, int] = {}
    for f in plan.fts:
        if only and f.table not in only:
            continue
        if not _existing_columns(conn, f.table):
            continue
        # 'rebuild' re-reads every row of the external content table (generated columns
        # included) -- correct even when rows changed without triggers.
        conn.execute(f"INSERT INTO {f.fts_table}({f.fts_table}) VALUES('rebuild')")
        counts[f.fts_table] = int(conn.execute(f"SELECT COUNT(*) FROM {f.fts_table}").fetchone()[0])
    for u in plan.unnest:
        if only and u.table not in only:
            continue
        if not _existing_columns(conn, u.table):
            continue
        conn.execute(f'DELETE FROM "{u.side_table}"')
        # json_each's `key` is the array index for arrays and the field name for maps;
        # `id` is a stable per-element identifier, which keeps the map case orderable too.
        ord_expr, key_expr = ("je.id", "je.key") if u.is_map else ("je.key", "NULL")
        # `je.value` is already the right thing for both cases: the scalar itself for
        # scalar elements, the JSON text of the subtree for object/array elements. (Do not
        # route it through json_type()/json() -- those parse the value as a JSON document
        # and a bare string element like "doc-3415" is not one.)
        conn.execute(
            f'INSERT INTO "{u.side_table}"(owner_id, ord, key, value) '
            f"SELECT t.id, {ord_expr}, {key_expr}, je.value "
            f'FROM "{u.table}" t, json_each(t.data, ?) je', (u.json_path,))
        counts[u.side_table] = int(conn.execute(f'SELECT COUNT(*) FROM "{u.side_table}"').fetchone()[0])
    conn.execute("ANALYZE")
    conn.commit()
    return counts


def is_enabled() -> bool:
    """`DATAFORM_PHYSICAL=0` disables the physical layer (ingestion-only databases)."""
    return os.environ.get("DATAFORM_PHYSICAL", "1") not in ("0", "false", "no")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def describe(plan: PhysicalPlan | None = None) -> str:
    plan = plan or load_plan()
    lines: list[str] = []
    for table in plan.tables():
        cols = [c for c in plan.columns if c.table == table]
        lines.append(f"TABLE {table}")
        for c in cols:
            tag = {"BASE": "base column", "STORED": "generated STORED", "VIRTUAL": "generated VIRTUAL"}[c.mode]
            idx = f" + {c.index} index" if c.index in ("btree", "nocase") else ""
            lines.append(f"  {c.field:<44} -> {c.qualified:<34} [{tag}{idx}]")
        f = plan.fts_for(table)
        if f:
            lines.append(f"  {'(fts)':<44} -> {f.fts_table:<34} [fts5: {', '.join(f.fields)}]")
        for u in [u for u in plan.unnest if u.table == table]:
            lines.append(f"  {u.field:<44} -> {u.side_table:<34} [unnest side table, {u.element_kind} elements]")
    if plan.unmapped:
        lines.append("UNMAPPED (schema/models drift -- these stay full-scan only):")
        for name, why in plan.unmapped:
            lines.append(f"  {name:<44} {why}")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    plan = load_plan()
    if "--apply" in argv or "--rebuild" in argv:
        from .store import Store
        store = Store()
        if "--apply" in argv:
            print("apply:", apply(store.conn, plan))
        if "--rebuild" in argv:
            print("rebuild:", rebuild_derived(store.conn, plan))
        store.close()
        return 0
    print(describe(plan))
    print(f"\n{len(plan.columns)} fields mapped to columns, {len(plan.fts)} FTS indexes, "
          f"{len(plan.unnest)} unnest side tables, {len(plan.unmapped)} unmapped")
    return 1 if plan.unmapped else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
