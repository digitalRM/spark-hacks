"""Corpus statistics — how big things are, and what has to be derived before it exists.

Shaped to mirror `Schema` in query_language.type_system, one stats constructor per type
constructor, so resolving a path against stats is the same walk as resolving it against
types. Deliberately not part of the type system: types describe shape, this describes
size and provenance, and this is the file a second corpus context replaces wholesale.

    ScalarType     -> ScalarStats       tokens, embeddability
    CollectionType -> CollectionStats   fanout
    OptionalType   -> OptionalStats     coverage
    ObjectType     -> ObjectStats       a map, same as the type

Coverage falling out of OptionalType is the useful part: only a fraction of documents
carry an audio recording, and that is a property of the field being absent, not of the
ASR that transcribes it. It makes the derivation an early filter for free.

Derivations are the exception to the hierarchy, because a derivation is an *edge* between
two paths and edges do not live in trees. They stay a flat map.

Where the numbers come from
---------------------------
Nothing in this file is written by hand any more. A CorpusStats is built in two layers:

  1. `from_registry(reg)` — the *structure*, from the same schema JSON the compiler and
     typechecker read (query_language/schemas/<name>.json). Every table and field the
     registry declares gets a stats node; every number is the registry's own guess (fanout)
     or a default (rows), and every provenance is PLACEHOLDER. Works for any registry.
  2. `overlay(stats, measurement)` — the *numbers*, from data/stats/<name>.json, which
     scripts/bootstrap_corpus.py writes by counting the actual database. Row counts,
     coverage, fanout and token lengths land on the paths they were measured for, and
     those nodes flip to MEASURED. Anything the bootstrap could not measure stays
     PLACEHOLDER and is reported by `placeholders()`.

`load(name)` does both. `ACTIVE` is `load()` for the configured schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from query_language.ast import FieldRef
from query_language.schema import Registry, FieldSpec
from query_language.type_system import FrozenDict
from optimizer.plan import Derivation, Provenance

STATS_DIR = Path(__file__).resolve().parent.parent / 'data' / 'stats'
"""Where bootstrap_corpus.py writes measurements: one <schema-name>.json per registry."""

DEFAULT_ROWS = 1_000.0
"""Base cardinality assumed for a table nobody has counted. Deliberately small and
deliberately PLACEHOLDER, so an uncounted table is cheap to plan against and loud."""

CHARS_PER_TOKEN = 4.0
"""avg_tokens is measured as characters / this. English prose averages ~4; dense legal
text with citations runs a little lower. The executor truncates with a more conservative
3.5 (runtime.model_endpoints) because there a miss is a hard failure; here it is a cost."""

# ---------------------------------------------------------------------------
# Paths — plain FieldRefs. The language has no variable binding, so a table appears
# at most once in a query and an alias is never anything but its table's name. That
# is why stats can be keyed by the same type the AST uses.
# ---------------------------------------------------------------------------

def path_str(p: FieldRef) -> str:
    return '.'.join((p.source, *p.path))

def child_of(p: FieldRef, name: str) -> FieldRef:
    return FieldRef(p.source, (*p.path, name))

def parse_ref(qualified: str) -> FieldRef:
    """'document.media.audio' -> FieldRef('document', ('media', 'audio'))."""
    table, _, rest = qualified.partition('.')
    return FieldRef(table, tuple(rest.split('.')) if rest else ())

# ---------------------------------------------------------------------------
# Stats, mirroring FieldType
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScalarStats:
    embeddable: bool = False
    """Can a similarity predicate target this? Requires a precomputed index."""
    avg_tokens: float = 0.0
    provenance: Provenance = Provenance.PLACEHOLDER

@dataclass(frozen=True)
class CollectionStats:
    """fanout is expected elements per parent record *that has the field* — pages per
    scanned document, segments per recording. Records without the field are the
    OptionalStats wrapper's business, not this one's."""
    fanout: float
    element: 'FieldStats'
    provenance: Provenance = Provenance.PLACEHOLDER

@dataclass(frozen=True)
class OptionalStats:
    """coverage is the fraction of parent records where the field is present.

    Below 1.0 the field is also a filter: a document with no recording cannot satisfy an
    audio predicate, and the plan should narrow before the expensive part rather than
    transcribe records that have nothing to transcribe."""
    coverage: float
    inner: 'FieldStats'
    provenance: Provenance = Provenance.PLACEHOLDER

type ObjectStats = FrozenDict[str, 'FieldStats']
type FieldStats  = ScalarStats | CollectionStats | OptionalStats | ObjectStats

@dataclass(frozen=True)
class TableStats:
    rows: float
    fields: ObjectStats
    provenance: Provenance = Provenance.PLACEHOLDER
    partitions: FrozenDict[str, FrozenDict[str, float]] = FrozenDict.of({})
    """Row counts by the values of a low-cardinality indexed column — `doc_type` on a
    table that mixes 5.3M opinions with 4.7K oral arguments — so an EXACT filter on
    that column can be sized exactly instead of by a prior. column -> value -> rows."""

@dataclass(frozen=True)
class DerivationSpec:
    """How a derived field is produced, and what one unit costs to produce.

    This is what makes transcription visible to the cost model. Without it the optimizer
    would price an audio predicate at the text call that reads the transcript and ignore
    the ninety seconds that produced it."""
    produces: FieldRef
    source: FieldRef
    method: Derivation
    model_role: str = 'NONE'   # role name in config.py, never a model string
    units_per_source_unit: float = 1.0
    seconds_per_source_unit: float = 0.0
    provenance: Provenance = Provenance.PLACEHOLDER

@dataclass(frozen=True)
class CorpusStats:
    name: str
    tables: FrozenDict[str, TableStats]
    derivations: FrozenDict[FieldRef, DerivationSpec]

# ---------------------------------------------------------------------------
# Resolution — the same walk as resolve_field_ref against types
# ---------------------------------------------------------------------------

class StatsError(Exception): pass

def field_stats(s: CorpusStats, p: FieldRef) -> FieldStats | None:
    """Stats for a path, or None if unrecorded.

    Descends through Optional and Collection wrappers to reach a field — so
    document.media.audio.timestamp_index means "the segments of a recording" and
    resolves. The typechecker is what rejects paths the *language* disallows; this is a
    lookup table, not a second typechecker. Wrappers on the final component are kept,
    because that is where callers read coverage and fanout."""
    t = s.tables.get(p.source)
    if t is None: return None
    cur: FieldStats = t.fields
    for key in p.path:
        cur = _element(_unwrap_optional(cur))
        if not isinstance(cur, dict): return None
        nxt = cur.get(key)
        if nxt is None: return None
        cur = nxt
    return cur

def rows(s: CorpusStats, table: str) -> tuple[float, Provenance]:
    """Base cardinality. An unrecorded table is assumed small, and says so."""
    t = s.tables.get(table)
    return (t.rows, t.provenance) if t else (DEFAULT_ROWS, Provenance.PLACEHOLDER)

def partition_rows(s: CorpusStats, table: str, column: str, value: str) -> float | None:
    """Rows of `table` where `column = value`, if the bootstrap counted that column."""
    t = s.tables.get(table)
    if t is None: return None
    by = t.partitions.get(column)
    return None if by is None else by.get(value, 0.0)

def fanout(s: CorpusStats, p: FieldRef) -> float:
    """Expected elements per parent record that has the field. 1.0 for anything not a
    collection. Multiply by coverage() for elements per record overall."""
    match _unwrap_optional(field_stats(s, p)):
        case CollectionStats(fanout=f): return f
        case _: return 1.0

def fanout_provenance(s: CorpusStats, p: FieldRef) -> Provenance:
    match _unwrap_optional(field_stats(s, p)):
        case CollectionStats(provenance=pr): return pr
        case _: return Provenance.PLACEHOLDER

def coverage(s: CorpusStats, p: FieldRef) -> float:
    """Fraction of parent records where the field is present. 1.0 unless Optional."""
    match field_stats(s, p):
        case OptionalStats(coverage=c): return c
        case _: return 1.0

def avg_tokens(s: CorpusStats, p: FieldRef) -> float:
    match _element(_unwrap_optional(field_stats(s, p))):
        case ScalarStats(avg_tokens=t): return t
        case _: return 0.0

def embeddable(s: CorpusStats, p: FieldRef) -> bool:
    match _element(_unwrap_optional(field_stats(s, p))):
        case ScalarStats(embeddable=e): return e
        case _: return False

def derivation(s: CorpusStats, p: FieldRef) -> DerivationSpec | None:
    """The derivation producing p, or None if p is stored."""
    return s.derivations.get(p)

def is_derived(s: CorpusStats, p: FieldRef) -> bool:
    return p in s.derivations

def _unwrap_optional(f: FieldStats | None) -> FieldStats | None:
    return f.inner if isinstance(f, OptionalStats) else f

def _element(f: FieldStats | None) -> FieldStats | None:
    return f.element if isinstance(f, CollectionStats) else f

def placeholders(s: CorpusStats) -> Iterator[str]:
    """Every stat still carrying PLACEHOLDER. Logged loudly at startup and badged in the
    UI until bootstrap_corpus.py and calibrate.py have run."""
    def walk(p: FieldRef, f: FieldStats) -> Iterator[str]:
        match f:
            case dict() as obj:
                for k, v in obj.items(): yield from walk(child_of(p, k), v)
            case CollectionStats(element=el, provenance=pr):
                if pr is Provenance.PLACEHOLDER: yield path_str(p)
                yield from walk(p, el)
            case OptionalStats(inner=inner, provenance=pr):
                if pr is Provenance.PLACEHOLDER: yield path_str(p)
                yield from walk(p, inner)
            case ScalarStats(provenance=pr):
                if pr is Provenance.PLACEHOLDER: yield path_str(p)
    for name, t in s.tables.items():
        if t.provenance is Provenance.PLACEHOLDER: yield f'{name} (rows)'
        yield from walk(FieldRef(name, ()), t.fields)
    for p, d in s.derivations.items():
        if d.provenance is Provenance.PLACEHOLDER: yield f'{path_str(p)} (derivation)'

# ---------------------------------------------------------------------------
# Layer 1: structure, from the registry. Generic over schema JSONs.
# ---------------------------------------------------------------------------

_COLLECTION_TYPES = frozenset({'IMAGE_SET', 'DOC_SCAN', 'TEXT_CHUNKED', 'AUDIO'})
"""Registry types whose one row is many model units — mirrors bridge.ELEMENT."""

DERIVATION_MODEL_ROLE: dict[Derivation, str] = {
    Derivation.RASTERIZE: 'NONE',              # bespoke, no model
    Derivation.DOC_PARSE: 'DOC_PARSE_MODEL',
    Derivation.ASR:       'ASR_MODEL',
    Derivation.CHUNK:     'NONE',              # bespoke, no model
}

DERIVATION_SECONDS_PER_SOURCE_UNIT: dict[Derivation, float] = {
    Derivation.RASTERIZE: 0.031, Derivation.DOC_PARSE: 0.44,
    Derivation.ASR: 94.0, Derivation.CHUNK: 0.004,
}
"""PLACEHOLDER unit costs — the one number in a derivation that is a cost rather than a
cardinality, and so belongs to calibrate.py rather than bootstrap_corpus.py. Kept here
until calibration.json carries derivation throughput; every DerivationSpec built from
these is PLACEHOLDER and says so."""

def _field_from_spec(spec: FieldSpec) -> FieldStats:
    """The stats node for one registry field — same shape rule as bridge.field_type."""
    if spec.type in _COLLECTION_TYPES:
        return CollectionStats(spec.fanout, ScalarStats(embeddable=spec.embeddable))
    if spec.type == 'TEXT':
        return ScalarStats(embeddable=spec.embeddable)
    # SCALAR. The registry types a list of scalars as SCALAR carrying a fanout.
    return CollectionStats(spec.fanout, ScalarStats()) if spec.is_set_valued else ScalarStats()

def _insert(tree: dict[str, Any], path: tuple[str, ...], node: FieldStats, name: str) -> None:
    head, rest = path[0], path[1:]
    if not rest:
        if isinstance(tree.get(head), dict):
            raise StatsError(f'{name}: {head!r} is both a field and an object')
        tree[head] = node
        return
    child = tree.setdefault(head, {})
    if not isinstance(child, dict):
        raise StatsError(f'{name}: {head!r} is both a field and an object')
    _insert(child, rest, node, name)

def _freeze(tree: dict[str, Any]) -> ObjectStats:
    return FrozenDict.of({k: _freeze(v) if isinstance(v, dict) else v for k, v in tree.items()})

def _derivation_from_spec(spec: FieldSpec) -> DerivationSpec:
    method = Derivation[spec.derivation]      # registry validated the name already
    produces = parse_ref(spec.name)
    # The registry may name a physical source column; if it does not, the derivation
    # reads the field it produces (a scanned PDF path rasterized into its own pages).
    source = parse_ref(spec.source_field) if spec.source_field else produces
    return DerivationSpec(
        produces, source, method, DERIVATION_MODEL_ROLE[method],
        units_per_source_unit=spec.fanout,
        seconds_per_source_unit=DERIVATION_SECONDS_PER_SOURCE_UNIT[method])

def from_registry(reg: Registry) -> CorpusStats:
    """A PLACEHOLDER CorpusStats with the registry's structure and the registry's guesses.

    Every table gets DEFAULT_ROWS; every collection gets the registry's fanout; nothing
    is Optional, because a registry cannot say what is missing — only a count can."""
    tables: dict[str, TableStats] = {}
    for table in reg.tables:
        tree: dict[str, Any] = {}
        for spec in reg.fields_of(table):
            _insert(tree, tuple(spec.column.split('.')), _field_from_spec(spec), spec.name)
        tables[table] = TableStats(DEFAULT_ROWS, _freeze(tree))
    derivations = {parse_ref(spec.name): _derivation_from_spec(spec)
                   for spec in reg.fields.values() if spec.derivation}
    return CorpusStats(reg.name, FrozenDict.of(tables), FrozenDict.of(derivations))

# ---------------------------------------------------------------------------
# Layer 2: numbers, from a measurement file. See scripts/bootstrap_corpus.py for the
# writer; the format is:
#
#   {"schema": ..., "tables": {<table>: {"rows": N, "provenance": "measured",
#        "partitions": {<column>: {<value>: N}},
#        "fields": {<dotted path>: <M>}}}}
#   M = {"kind": text|number|bool|array|object|null|unknown, "coverage": f,
#        "avg_tokens": f?, "fanout": f?, "element": {<key>: <M>}?, "provenance": ...}
#
# `element` describes what is *inside* an array whose elements are objects, and can go
# deeper than the registry does — the registry says document.media.audio is AUDIO, the
# measurement says each recording carries ~350 timestamp_index segments of ~40 tokens.
# ---------------------------------------------------------------------------

def measurement_path(name: str) -> Path:
    return STATS_DIR / f'{name}.json'

def _prov(m: dict[str, Any]) -> Provenance:
    return Provenance(m.get('provenance', 'measured'))

def _from_measurement(m: dict[str, Any], base: FieldStats | None) -> FieldStats:
    """Apply one measurement node onto a structural node (or build it from nothing)."""
    pr = _prov(m)
    kind = m.get('kind', 'unknown')
    inner = _unwrap_optional(base)

    if kind in ('null', 'unknown'):
        # Counted, and never present. Nothing beneath can be sized because there is
        # nothing to size; the structure is kept for the walk and stamped as measured,
        # since "zero of them" is the measurement.
        node: FieldStats = _stamp(inner if inner is not None else ScalarStats(), pr)
    elif kind == 'array':
        el_m = m.get('element')
        if isinstance(inner, CollectionStats):
            element = inner.element
        else:
            # The registry called this a scalar; the data says it is a list of them.
            element = inner if isinstance(inner, ScalarStats) else ScalarStats()
        if isinstance(el_m, dict) and el_m:
            element = _object_from_measurement(el_m, element if isinstance(element, dict) else None)
        elif 'element_avg_tokens' in m and isinstance(element, ScalarStats):
            element = replace(element, avg_tokens=float(m['element_avg_tokens']), provenance=pr)
        node = CollectionStats(float(m.get('fanout', 0.0)), element, pr)
        if not m.get('fanout'):
            node = _stamp(node, pr)     # never non-empty: nothing inside to size
    elif isinstance(inner, CollectionStats):
        # Structure says collection (registry fanout > 1 or a modal set), data says
        # each row holds one value. Keep the collection with fanout 1 — the type system
        # still sees an Array — and note the element's size.
        el = inner.element
        if isinstance(el, ScalarStats) and 'avg_tokens' in m:
            el = replace(el, avg_tokens=float(m['avg_tokens']), provenance=pr)
        node = CollectionStats(1.0, el, pr)
    elif isinstance(inner, dict):
        node = inner
    else:
        sc = inner if isinstance(inner, ScalarStats) else ScalarStats()
        node = replace(sc, avg_tokens=float(m.get('avg_tokens', sc.avg_tokens)), provenance=pr)

    cov = float(m.get('coverage', 1.0))
    return OptionalStats(cov, node, pr) if cov < 1.0 else node

def _stamp(f: FieldStats, pr: Provenance) -> FieldStats:
    """The same node with every provenance beneath set to pr."""
    match f:
        case dict() as obj: return FrozenDict.of({k: _stamp(v, pr) for k, v in obj.items()})
        case CollectionStats(fanout=fan, element=el): return CollectionStats(fan, _stamp(el, pr), pr)
        case OptionalStats(coverage=c, inner=inner): return OptionalStats(c, _stamp(inner, pr), pr)
        case ScalarStats(): return replace(f, provenance=pr)
    return f

def _object_from_measurement(ms: dict[str, Any], base: ObjectStats | None) -> ObjectStats:
    out: dict[str, FieldStats] = dict(base) if base else {}
    for key, m in ms.items():
        out[key] = _from_measurement(m, out.get(key))
    return FrozenDict.of(out)

def _apply_at(fields: ObjectStats, path: tuple[str, ...], m: dict[str, Any],
              name: str) -> ObjectStats:
    """Return `fields` with the measurement applied at `path`, creating objects as needed."""
    head, rest = path[0], path[1:]
    out = dict(fields)
    if not rest:
        out[head] = _from_measurement(m, out.get(head))
        return FrozenDict.of(out)
    child = out.get(head, FrozenDict.of({}))
    if not isinstance(child, dict):
        raise StatsError(f'{name}: measurement descends into {head!r}, which is a leaf')
    out[head] = _apply_at(child, rest, m, name)
    return FrozenDict.of(out)

def overlay(s: CorpusStats, measurement: dict[str, Any]) -> CorpusStats:
    """The stats with every measured number laid over the structural ones.

    Tables the measurement names but the registry does not are ignored: the compiler
    cannot reach them, so no plan will ask. Fields the measurement names but the
    registry does not are *kept* — that is how element structure below a registry
    field (segments inside a recording) becomes visible to the cost model."""
    tables = dict(s.tables)
    for name, tm in (measurement.get('tables') or {}).items():
        t = tables.get(name)
        if t is None: continue
        fields = t.fields
        for dotted, m in (tm.get('fields') or {}).items():
            fields = _apply_at(fields, tuple(dotted.split('.')), m, f'{name}.{dotted}')
        parts = FrozenDict.of({col: FrozenDict.of({str(v): float(n) for v, n in by.items()})
                               for col, by in (tm.get('partitions') or {}).items()})
        rows_ = tm.get('rows')
        tables[name] = TableStats(
            float(rows_) if rows_ is not None else t.rows, fields,
            Provenance(tm.get('provenance', 'measured')) if rows_ is not None else t.provenance,
            parts or t.partitions)
    return CorpusStats(s.name, FrozenDict.of(tables), s.derivations)

_CACHE: dict[str, CorpusStats] = {}

def load(name_or_path: str | Path | None = None) -> CorpusStats:
    """Structure from the registry, numbers from its measurement file if one exists.

    Same resolution as query_language.schema.load: a name means schemas/<name>.json,
    and the measurement is data/stats/<name>.json. Cached per registry, like the
    registry itself; the first call is two small file reads."""
    from query_language import schema as registry
    reg = registry.load(name_or_path)
    key = Path(name_or_path).stem if name_or_path else reg.name
    if key in _CACHE:
        return _CACHE[key]
    s = from_registry(reg)
    for candidate in dict.fromkeys((key, reg.name)):
        p = measurement_path(candidate)
        if p.exists():
            s = overlay(s, json.loads(p.read_text()))
            break
    _CACHE[key] = s
    return s

ACTIVE: CorpusStats = load()
"""The stats the optimizer plans against: the configured schema ($AMICUS_SCHEMA), with
its measurement if scripts/bootstrap_corpus.py has produced one."""

# ---------------------------------------------------------------------------

def render(s: CorpusStats) -> str:
    """One line per table and field: what the optimizer believes, and on what evidence."""
    lines = [f'# {s.name}']
    def tag(pr: Provenance) -> str: return {'measured': '', 'extrapolated': ' ~',
                                            'placeholder': ' ?'}[pr.value]
    def walk(p: FieldRef, f: FieldStats, cov: str = '') -> Iterator[str]:
        match f:
            case dict() as obj:
                for k, v in obj.items(): yield from walk(child_of(p, k), v)
            case OptionalStats(coverage=c, inner=inner, provenance=pr):
                yield from walk(p, inner, f' cov={c:.4g}{tag(pr)}')
            case CollectionStats(fanout=fan, element=el, provenance=pr):
                yield f'  {path_str(p):<50} x{fan:,.4g}{tag(pr)}{cov}'
                yield from walk(FieldRef(p.source, (*p.path[:-1], p.path[-1] + '[]')), el)
            case ScalarStats(embeddable=e, avg_tokens=t, provenance=pr):
                extra = (f' tok={t:,.4g}' if t else '') + (' emb' if e else '')
                yield f'  {path_str(p):<50}{extra}{tag(pr)}{cov}'
    for name, t in s.tables.items():
        lines.append(f'{name}: {t.rows:,.0f} rows{tag(t.provenance)}')
        for col, by in t.partitions.items():
            top = sorted(by.items(), key=lambda kv: -kv[1])[:6]
            lines.append(f'  by {col}: ' + ', '.join(f'{v or "∅"}={n:,.0f}' for v, n in top))
        lines.extend(walk(FieldRef(name, ()), t.fields))
    for p, d in s.derivations.items():
        lines.append(f'derive {path_str(p)} <- {path_str(d.source)} via {d.method.value}'
                     f' x{d.units_per_source_unit:g} @{d.seconds_per_source_unit:g}s'
                     f'{tag(d.provenance)}')
    return '\n'.join(lines)

if __name__ == '__main__':
    import sys
    print(render(load(sys.argv[1]) if len(sys.argv) > 1 else ACTIVE))
    n = sum(1 for _ in placeholders(ACTIVE if len(sys.argv) < 2 else load(sys.argv[1])))
    print(f'\n{n} placeholder(s) — ? marks a number nobody has measured')
