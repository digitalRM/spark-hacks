#!/usr/bin/env python3
"""Count the corpus: measure a registry's tables and fields against the database that
actually holds them, and write the numbers optimizer.stats plans against.

    python scripts/bootstrap_corpus.py                       # $AMICUS_SCHEMA vs the default db
    python scripts/bootstrap_corpus.py --schema dataform --db ~/amicus-dataform/data/dataform.db
    python scripts/bootstrap_corpus.py --sample 20000        # quick: aggregate over a row sample

Output: data/stats/<schema>.json, the measurement file `optimizer.stats.load()` overlays
on the registry. Re-run whenever the corpus changes; the file is small and diffs cleanly.

What is measured, and how
-------------------------
The registry (query_language/schemas/<schema>.json) says which tables and fields exist.
This script never assumes a physical layout: for each registry field it looks for a real
column of that name first, and otherwise for a JSON-object column (the dataform store's
`data` blob) it can `json_extract` the dotted path from. A field it can reach neither way
is reported and left PLACEHOLDER. That is what makes the same script correct for the
one-table-per-entity dataform db and for a flat relational schema.

Per table, exact unless --sample:
    rows          COUNT(*)
    partitions    rows by value of each indexed low-cardinality column (doc_type, ...)
Per field, in ONE full pass over the table:
    coverage      fraction of rows where the value is present (not null, not '', not [], not {})
    avg_tokens    for text: mean length / CHARS_PER_TOKEN, over rows where present
    fanout        for arrays: mean length, over rows where non-empty
Per array field, from a sample of rows that have it:
    element       what an element looks like — for objects, the same stats per key,
                  recursively (a recording's ~350 segments of ~40 tokens each)

Provenance: numbers from a full pass are MEASURED; numbers from a sample (--sample, or the
element introspection when the population exceeds the sample) are EXTRAPOLATED.

Cost: one COUNT and one aggregate scan per table (a few json_* calls per field per row),
plus one partial scan per sparse field. Measured on the GN100 against the 20 GB dataform
db: ~6 min for the 14.8M-row proceeding table, ~3.5 min for the 5.6M-row document table,
seconds for the rest. `--sample 50000` gets within a percent or two in under a minute.
No model calls.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config                                                       # noqa: E402
from query_language import schema as registry                       # noqa: E402
from optimizer.stats import (                                       # noqa: E402
    CHARS_PER_TOKEN, STATS_DIR, load, measurement_path, placeholders, render,
)

DEFAULT_DB = ROOT / 'data_ingestion' / 'data' / 'dataform.db'
"""Where data_ingestion.dataform.store puts the db by default (a symlink on the GN100)."""

MAX_PARTITION_VALUES = 64
ELEMENT_DEPTH = 3
"""How far below a registry field the element introspection descends."""

# ---------------------------------------------------------------------------
# Physical resolution: how a registry field is reached in this db
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Physical:
    value: str      # SQL expression yielding the value
    kind: str       # SQL expression yielding a type name: json_type(...) or typeof(...)
    arrlen: str     # SQL expression: element count if the value is an array, else 0/NULL
    via: str        # 'column' | 'json'

def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'

def json_path(path: tuple[str, ...]) -> str:
    return '$' + ''.join('.' + q(k) for k in path)

def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info({q(table)})')]

def json_column(conn: sqlite3.Connection, table: str, cols: list[str]) -> str | None:
    """The column holding a JSON object per row, if the table has one. `data` by
    convention (data_ingestion.dataform.store); otherwise the first column whose
    value in some row is a JSON object."""
    if 'data' in cols:
        return 'data'
    row = conn.execute(f'SELECT * FROM {q(table)} LIMIT 1').fetchone()
    if row is None:
        return None
    for col, val in zip(cols, row):
        if isinstance(val, str) and val.startswith('{'):
            try:
                if conn.execute('SELECT json_type(?)', (val,)).fetchone()[0] == 'object':
                    return col
            except sqlite3.OperationalError:
                pass
    return None

def resolve(path: tuple[str, ...], cols: list[str], jcol: str | None) -> Physical | None:
    if len(path) == 1 and path[0] in cols:
        c = q(path[0])
        return Physical(c, f'typeof({c})', 'NULL', 'column')
    if jcol is not None:
        j, p = q(jcol), json_path(path)
        # Two-argument json_* forms: they read the (well-formed) blob and answer 0/NULL for
        # a path that is not an array, where the one-argument form over a text value
        # would raise "malformed JSON".
        return Physical(f"json_extract({j}, '{p}')", f"json_type({j}, '{p}')",
                        f"json_array_length({j}, '{p}')", 'json')
    return None

# json_type() / typeof() names -> the measurement's kind vocabulary
_KIND = {'text': 'text', 'integer': 'number', 'real': 'number', 'true': 'bool',
         'false': 'bool', 'array': 'array', 'object': 'object', 'blob': 'blob'}

def present_sql(ph: Physical) -> str:
    """1 when the value is there in a way a predicate could use, else 0."""
    v, k, n = ph.value, ph.kind, ph.arrlen
    if ph.via == 'column':
        return f"({v} IS NOT NULL AND {v} != '')"
    return (f"({k} IS NOT NULL AND {k} != 'null'"
            f" AND NOT ({k} = 'text' AND {v} = '')"
            f" AND NOT ({k} = 'array' AND {n} = 0)"
            f" AND NOT ({k} = 'object' AND {v} = '{{}}'))")

# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def sample_rowids(conn: sqlite3.Connection, table: str, n: int, rng: random.Random) -> list[int]:
    """Up to n existing rowids, uniformly-ish. Draws over [min, max] and keeps the hits,
    which is fast on a 15M-row table where ORDER BY random() is a full sort."""
    lo, hi = conn.execute(f'SELECT MIN(rowid), MAX(rowid) FROM {q(table)}').fetchone()
    if lo is None:
        return []
    span = hi - lo + 1
    if span <= n:
        return [r[0] for r in conn.execute(f'SELECT rowid FROM {q(table)}')]
    draws = {rng.randrange(lo, hi + 1) for _ in range(int(n * 1.5) + 16)}
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS _draw (r INTEGER PRIMARY KEY)')
    conn.execute('DELETE FROM _draw')
    conn.executemany('INSERT OR IGNORE INTO _draw VALUES (?)', ((d,) for d in draws))
    hits = [r[0] for r in conn.execute(
        f'SELECT t.rowid FROM {q(table)} t JOIN _draw d ON d.r = t.rowid')]
    if not hits:   # rowids so sparse the draw missed everything: take what there is
        hits = [r[0] for r in conn.execute(f'SELECT rowid FROM {q(table)} LIMIT {n}')]
    rng.shuffle(hits)
    return hits[:n]

def sample_where(rowids: list[int] | None) -> str:
    if rowids is None:
        return ''
    conn_tmp = ','.join(str(r) for r in rowids)
    return f' WHERE rowid IN ({conn_tmp})' if rowids else ' WHERE 0'

# ---------------------------------------------------------------------------
# Element introspection (Python, over decoded JSON)
# ---------------------------------------------------------------------------

def _present(v: Any) -> bool:
    return v is not None and v != '' and v != [] and v != {}

def _kind_of(v: Any) -> str:
    if isinstance(v, bool): return 'bool'
    if isinstance(v, (int, float)): return 'number'
    if isinstance(v, str): return 'text'
    if isinstance(v, list): return 'array'
    if isinstance(v, dict): return 'object'
    return 'null'

def describe_values(values: list[Any], depth: int, prov: str) -> dict[str, Any]:
    """The measurement node for a multiset of values one field took (nulls included)."""
    n = len(values)
    present = [v for v in values if _present(v)]
    kinds = Counter(_kind_of(v) for v in present)
    kind = kinds.most_common(1)[0][0] if kinds else 'null'
    m: dict[str, Any] = {'kind': kind, 'coverage': (len(present) / n) if n else 0.0,
                         'provenance': prov}
    if kind == 'text':
        texts = [v for v in present if isinstance(v, str)]
        m['avg_tokens'] = sum(len(t) for t in texts) / len(texts) / CHARS_PER_TOKEN
    elif kind == 'array':
        arrays = [v for v in present if isinstance(v, list)]
        m['fanout'] = sum(len(a) for a in arrays) / len(arrays)
        m.update(describe_elements([e for a in arrays for e in a], depth, prov))
    return m

def describe_elements(elements: list[Any], depth: int, prov: str) -> dict[str, Any]:
    """What is inside an array: `element` per key for objects, `element_avg_tokens` for
    strings. Bounded by ELEMENT_DEPTH so a pathological blob cannot recurse forever."""
    if not elements or depth <= 0:
        return {}
    kinds = Counter(_kind_of(e) for e in elements)
    kind = kinds.most_common(1)[0][0]
    if kind == 'text':
        texts = [e for e in elements if isinstance(e, str)]
        return {'element_avg_tokens': sum(len(t) for t in texts) / len(texts) / CHARS_PER_TOKEN}
    if kind == 'object':
        objs = [e for e in elements if isinstance(e, dict)]
        keys = sorted({k for o in objs for k in o})
        return {'element': {k: describe_values([o.get(k) for o in objs], depth - 1, prov)
                            for k in keys}}
    return {}

def describe_sparse(pulled: list[tuple[str, Any]], exhaustive: bool) -> dict[str, Any]:
    """Kind and size for a field the probe never saw, from rows that do have it."""
    prov = 'measured' if exhaustive else 'extrapolated'
    kinds = Counter(_KIND.get(k, 'unknown') for k, _ in pulled if k not in (None, 'null'))
    if not kinds:
        return {}
    kind = kinds.most_common(1)[0][0]
    m: dict[str, Any] = {'kind': kind}
    if kind == 'text':
        texts = [v for k, v in pulled if k == 'text' and isinstance(v, str)]
        m['avg_tokens'] = sum(len(t) for t in texts) / len(texts) / CHARS_PER_TOKEN
        m['provenance'] = prov
    elif kind == 'array':
        arrays = [json.loads(v) for k, v in pulled if k == 'array']
        m['fanout'] = sum(len(x) for x in arrays) / len(arrays)
        m['provenance'] = prov
        m.update(describe_elements([e for x in arrays for e in x], ELEMENT_DEPTH, prov))
    return m

def tidy(m: Any) -> Any:
    """Round the floats so the file diffs cleanly between runs."""
    if isinstance(m, dict):
        return {k: tidy(v) for k, v in m.items()}
    if isinstance(m, float):
        return round(m, 6) if m < 1.0 else round(m, 3)
    return m

# ---------------------------------------------------------------------------
# One table
# ---------------------------------------------------------------------------

@dataclass
class TableReport:
    name: str
    rows: int
    resolved: dict[str, Physical]
    unresolved: list[str]
    via: str
    seconds: float

def partitions(conn: sqlite3.Connection, table: str, jcol: str | None) -> dict[str, dict[str, int]]:
    """Row counts by value for each indexed column with few distinct values. Walks the
    index, so it is cheap even on the 15M-row tables; a column with more than
    MAX_PARTITION_VALUES values is not a partition and is dropped after 65 groups."""
    out: dict[str, dict[str, int]] = {}
    for idx in conn.execute(f'PRAGMA index_list({q(table)})'):
        cols = [r[2] for r in conn.execute(f'PRAGMA index_info({q(idx[1])})')]
        if not cols or cols[0] is None or cols[0] == jcol or cols[0] in out:
            continue
        col = cols[0]
        groups = conn.execute(
            f'SELECT {q(col)}, COUNT(*) FROM {q(table)} GROUP BY {q(col)} '
            f'LIMIT {MAX_PARTITION_VALUES + 1}').fetchall()
        if 1 <= len(groups) <= MAX_PARTITION_VALUES and any(v is not None for v, _ in groups):
            out[col] = {('' if v is None else str(v)): int(n) for v, n in groups}
    return out

def measure_table(conn: sqlite3.Connection, table: str, specs: list[registry.FieldSpec],
                  sample: int | None, element_rows: int, rng: random.Random,
                  log) -> tuple[dict[str, Any], TableReport]:
    import time
    t0 = time.perf_counter()
    cols = table_columns(conn, table)
    jcol = json_column(conn, table, cols)
    rows = conn.execute(f'SELECT COUNT(*) FROM {q(table)}').fetchone()[0]

    resolved: dict[str, Physical] = {}
    unresolved: list[str] = []
    for spec in specs:
        ph = resolve(tuple(spec.column.split('.')), cols, jcol)
        (resolved.__setitem__(spec.column, ph) if ph else unresolved.append(spec.column))

    out: dict[str, Any] = {'rows': rows, 'provenance': 'measured',
                           'partitions': partitions(conn, table, jcol), 'fields': {}}
    if rows == 0 or not resolved:
        # An empty table is a measurement too: nothing is present, and nothing can be
        # sized. Fields the registry declares get coverage 0 so the estimator sees the
        # table as counted rather than as unknown.
        out['fields'] = {n: {'kind': 'null', 'coverage': 0.0, 'provenance': 'measured'}
                         for n in resolved}
        rep = TableReport(table, rows, resolved, unresolved, jcol and f'json({jcol})' or 'columns',
                          time.perf_counter() - t0)
        return out, rep

    # -- pass 0: a row sample, to learn each field's kind and to seed the element sample
    probe_n = min(rows, max(element_rows, 512))
    probe_ids = sample_rowids(conn, table, probe_n, rng)
    names = list(resolved)
    sel = ', '.join(f'{resolved[n].kind}, {resolved[n].value}' for n in names)
    probe = conn.execute(f'SELECT {sel} FROM {q(table)}{sample_where(probe_ids)}').fetchall()
    kinds: dict[str, Counter[str]] = {}
    probe_values: dict[str, list[Any]] = {n: [] for n in names}
    for row in probe:
        for i, n in enumerate(names):
            k, v = row[2 * i], row[2 * i + 1]
            probe_values[n].append(v)
            if k not in (None, 'null'):
                kinds.setdefault(n, Counter())[k] += 1
    kind_of = {n: _KIND.get(c.most_common(1)[0][0], 'unknown') for n, c in kinds.items()}

    # -- pass 1: exact aggregates in one scan (or over the --sample rowids)
    agg_ids = sample_rowids(conn, table, sample, rng) if sample and sample < rows else None
    prov = 'extrapolated' if agg_ids is not None else 'measured'
    exprs = ['COUNT(*)']
    slots: list[tuple[str, str]] = []
    for n in names:
        ph, k = resolved[n], kind_of.get(n, 'unknown')
        exprs.append(f'SUM({present_sql(ph)})'); slots.append((n, 'present'))
        if k == 'text':
            exprs.append(f"SUM(CASE WHEN {ph.kind} = 'text' THEN length({ph.value}) END)")
            slots.append((n, 'text_len'))
            exprs.append(f"SUM({ph.kind} = 'text' AND {ph.value} != '')"); slots.append((n, 'text_n'))
        elif k == 'array':
            exprs.append(f"SUM(CASE WHEN {ph.kind} = 'array' THEN {ph.arrlen} END)")
            slots.append((n, 'arr_len'))
            exprs.append(f"SUM({ph.kind} = 'array' AND {ph.arrlen} > 0)")
            slots.append((n, 'arr_n'))
    row = conn.execute(f'SELECT {", ".join(exprs)} FROM {q(table)}{sample_where(agg_ids)}').fetchone()
    scanned = row[0] or 0
    agg: dict[str, dict[str, float]] = {n: {} for n in names}
    for (n, what), val in zip(slots, row[1:]):
        agg[n][what] = float(val or 0)

    for n in names:
        a, k = agg[n], kind_of.get(n, 'unknown')
        m: dict[str, Any] = {'kind': k, 'coverage': (a['present'] / scanned) if scanned else 0.0,
                             'provenance': prov}
        if k == 'unknown' and a['present'] > 0:
            # Too sparse for the probe to have seen one: pull a few rows that have it and
            # size those. Presence stays exact; the size is from the sample.
            ph = resolved[n]
            pulled = conn.execute(
                f'SELECT {ph.kind}, {ph.value} FROM {q(table)} WHERE {present_sql(ph)} LIMIT ?',
                (element_rows,)).fetchall()
            m.update(describe_sparse(pulled, len(pulled) >= a['present'] and prov == 'measured'))
        elif k == 'text' and a.get('text_n'):
            m['avg_tokens'] = a['text_len'] / a['text_n'] / CHARS_PER_TOKEN
        elif k == 'array':
            # Elements per row that has the array; 0.0 when no row does.
            m['fanout'] = (a['arr_len'] / a['arr_n']) if a.get('arr_n') else 0.0
            # -- element introspection from rows that have the array. The probe sample
            # covers dense arrays; sparse ones (4.7K recordings in 5.6M documents) need
            # a targeted pull, which is one more scan of the table.
            have = [json.loads(v) for v in probe_values[n]
                    if isinstance(v, str) and v.startswith('[') and v != '[]']
            population = int(a.get('arr_n', 0))
            if len(have) < min(element_rows, population):
                ph = resolved[n]
                pulled = conn.execute(
                    f"SELECT {ph.value} FROM {q(table)} WHERE {ph.kind} = 'array' "
                    f"AND {ph.arrlen} > 0 ORDER BY random() LIMIT ?",
                    (element_rows,)).fetchall()
                have = [json.loads(r[0]) for r in pulled]
            el_prov = 'measured' if len(have) >= population and prov == 'measured' else 'extrapolated'
            m.update(describe_elements([e for arr in have for e in arr], ELEMENT_DEPTH, el_prov))
        out['fields'][n] = tidy(m)

    rep = TableReport(table, rows, resolved, unresolved, f'json({jcol})' if jcol else 'columns',
                      time.perf_counter() - t0)
    return out, rep

# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--schema', default=config.SCHEMA,
                    help='registry name or path (default: $AMICUS_SCHEMA = %(default)s)')
    ap.add_argument('--db', type=Path, default=DEFAULT_DB, help='SQLite file (default: %(default)s)')
    ap.add_argument('--out', type=Path, default=None,
                    help='measurement file (default: data/stats/<schema>.json)')
    ap.add_argument('--sample', type=int, default=None,
                    help='aggregate over this many random rows per table instead of all of them')
    ap.add_argument('--element-rows', type=int, default=200,
                    help='rows per array field to introspect elements from (default %(default)s)')
    ap.add_argument('--tables', nargs='*', default=None, help='only these tables')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--allow-missing', action='store_true',
                    help='proceed even if the db lacks a registry table')
    ap.add_argument('--dry-run', action='store_true', help='verify and measure, write nothing')
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f'no database at {args.db}', file=sys.stderr)
        return 2
    reg = registry.load(args.schema)
    out_path = args.out or measurement_path(Path(args.schema).stem if Path(args.schema).suffix else args.schema)
    rng = random.Random(args.seed)

    # Read-only on the corpus; the temp db (for the rowid sample) stays writable.
    conn = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True, timeout=30)
    db_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}

    # -- verify: is this db the schema we think it is?
    want = list(reg.tables)
    missing = [t for t in want if t not in db_tables]
    extra = sorted(db_tables - set(want))
    print(f'schema {reg.name!r}: {len(want)} tables, {len(reg.fields)} fields')
    print(f'db     {args.db}: {len(db_tables)} tables')
    if missing:
        print(f'  MISSING from db: {", ".join(missing)}')
    if extra:
        print(f'  in db, not in schema (ignored): {", ".join(extra)}')
    if missing and not args.allow_missing:
        print('schema/db mismatch — refusing to write; pass --allow-missing to measure the rest',
              file=sys.stderr)
        return 1

    tables = [t for t in want if t in db_tables and (not args.tables or t in args.tables)]
    measured: dict[str, Any] = {}
    reports: list[TableReport] = []
    for t in tables:
        m, rep = measure_table(conn, t, reg.fields_of(t), args.sample, args.element_rows, rng, print)
        measured[t] = m
        reports.append(rep)
        n_res, n_all = len(rep.resolved), len(rep.resolved) + len(rep.unresolved)
        print(f'  {t:<22} {rep.rows:>12,} rows  {n_res}/{n_all} fields via {rep.via}'
              f'  {rep.seconds:5.1f}s' + (f'  UNRESOLVED: {", ".join(rep.unresolved)}'
                                          if rep.unresolved else ''))
        sys.stdout.flush()

    payload = {
        '_README': ('Written by scripts/bootstrap_corpus.py. Numbers optimizer.stats overlays '
                    'on the registry of the same name. Do not edit by hand; re-run the script.'),
        'schema': reg.name,
        'db': str(args.db),
        'measured_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'method': f'sample:{args.sample}' if args.sample else 'exact',
        'chars_per_token': CHARS_PER_TOKEN,
        'tables': measured,
    }
    if args.dry_run:
        print(json.dumps(payload, indent=1)[:4000])
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=1, sort_keys=False) + '\n')
    print(f'\nwrote {out_path} ({out_path.stat().st_size:,} bytes)\n')

    # -- show what the optimizer will now believe, if this is a registry it can load
    if out_path == measurement_path(reg.name) or out_path == measurement_path(Path(args.schema).stem):
        s = load(args.schema)
        print(render(s))
        left = list(placeholders(s))
        print(f'\n{len(left)} placeholder(s) remain' + (': ' + ', '.join(left[:12]) +
                                                       (' ...' if len(left) > 12 else '') if left else ''))
    return 0

if __name__ == '__main__':
    sys.exit(main())
