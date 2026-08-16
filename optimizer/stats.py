"""Corpus statistics — how big things are, and what has to be derived before it exists.

Shaped to mirror `Schema` in query_language.type_system, one stats constructor per type
constructor, so resolving a path against stats is the same walk as resolving it against
types. Deliberately not part of the type system: types describe shape, this describes
size and provenance, and this is the file a second corpus context replaces wholesale.

    ScalarType     -> ScalarStats       tokens, embeddability
    CollectionType -> CollectionStats   fanout and coverage
    OptionalType   -> OptionalStats     coverage
    ObjectType     -> ObjectStats       a map, same as the type

Coverage is the useful part: only ~13% of dockets have an argument recording, so
docket.argument is an empty sequence for the other 87%. That makes the ASR that produces
it an early filter as well as a cost, which is the single most consequential number in
the flagship query's plan.

Field names here track query_language/schema.py's registry, since that is what the
compiler emits against and what bridge.py hands the typechecker. They must not drift.

Derivations are the exception to the hierarchy, because a derivation is an *edge* between
two paths and edges do not live in trees. They stay a flat map.

Every number is PLACEHOLDER until scripts/bootstrap_corpus.py counts it.
"""

from dataclasses import dataclass
from typing import Iterator

from query_language.ast import FieldRef
from query_language.type_system import FrozenDict
from optimizer.plan import Derivation, Provenance

# ---------------------------------------------------------------------------
# Paths — plain FieldRefs. The language has no variable binding, so a table appears
# at most once in a query and an alias is never anything but its table's name. That
# is why stats can be keyed by the same type the AST uses.
# ---------------------------------------------------------------------------

def path_str(p: FieldRef) -> str:
    return '.'.join((p.source, *p.path))

def child_of(p: FieldRef, name: str) -> FieldRef:
    return FieldRef(p.source, (*p.path, name))

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
    """fanout is expected elements per *non-empty* parent — 47 pages per scanned cluster.

    coverage is the fraction of parent records where the collection is non-empty. Below
    1.0 the collection is also a filter: a docket with no argument recording yields an
    empty sequence, cannot satisfy a predicate over it, and should narrow the plan before
    the expensive part rather than be transcribed with nothing to transcribe. Keeping the
    two numbers apart matters — folding them into one fanout would make a rare recording
    look like a short one."""
    fanout: float
    element: 'FieldStats'
    coverage: float = 1.0
    provenance: Provenance = Provenance.PLACEHOLDER

@dataclass(frozen=True)
class OptionalStats:
    """coverage is the fraction of parent records where the field is present.

    Below 1.0 the field is also a filter: a docket with no recording cannot satisfy an
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
    audio.segments.transcript means "the transcript of a segment" and resolves. The
    typechecker is what rejects paths the *language* disallows; this is a lookup table,
    not a second typechecker. Wrappers on the final component are kept, because that is
    where callers read coverage and fanout."""
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
    return (t.rows, t.provenance) if t else (1_000.0, Provenance.PLACEHOLDER)

def fanout(s: CorpusStats, p: FieldRef) -> float:
    """Expected elements per parent record. 1.0 for anything not a collection."""
    match _unwrap_optional(field_stats(s, p)):
        case CollectionStats(fanout=f): return f
        case _: return 1.0

def coverage(s: CorpusStats, p: FieldRef) -> float:
    """Fraction of parent records where the field is present and non-empty."""
    match field_stats(s, p):
        case OptionalStats(coverage=c):   return c
        case CollectionStats(coverage=c): return c
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
# The legal corpus: CourtListener / Free Law Project, 9th Circuit, last 3 years.
# TODO: verify every count against the built index. bootstrap_corpus.py should rewrite
# these numbers and flip provenance to MEASURED.
# ---------------------------------------------------------------------------

def _obj(**fields: FieldStats) -> ObjectStats:
    return FrozenDict.of(dict(fields))

LEGAL = CorpusStats(
    name='courtlistener-ca9-3y',
    tables=FrozenDict.of({
        'court': TableStats(2_100.0, _obj(
            id=ScalarStats(), name=ScalarStats(avg_tokens=6.0),
            jurisdiction=ScalarStats())),
        'docket': TableStats(28_000.0, _obj(
            id=ScalarStats(), court_id=ScalarStats(), docket_number=ScalarStats(),
            case_name=ScalarStats(embeddable=True, avg_tokens=12.0),
            date_filed=ScalarStats(),
            # The 13% is why §2 demands the three-modality intersection be validated
            # before the demo depends on it existing at all.
            argument=CollectionStats(320.0, ScalarStats(embeddable=True, avg_tokens=55.0),
                                     coverage=0.13))),
        'cluster': TableStats(32_000.0, _obj(
            id=ScalarStats(), docket_id=ScalarStats(),
            case_name=ScalarStats(embeddable=True, avg_tokens=12.0),
            date_filed=ScalarStats(), precedential_status=ScalarStats(),
            scan_pdf_url=ScalarStats(),
            scan_pages=CollectionStats(47.0, ScalarStats(), coverage=0.62),
            scan_text=CollectionStats(47.0, ScalarStats(embeddable=True, avg_tokens=640.0),
                                      coverage=0.62))),
        'opinion': TableStats(40_000.0, _obj(
            id=ScalarStats(), cluster_id=ScalarStats(), author_str=ScalarStats(),
            type=ScalarStats(),
            plain_text=ScalarStats(embeddable=True, avg_tokens=31_000.0),
            chunks=CollectionStats(42.0, ScalarStats(embeddable=True, avg_tokens=760.0)),
            token_count=ScalarStats())),
        'citation': TableStats(1_900_000.0, _obj(
            citing_opinion_id=ScalarStats(), cited_opinion_id=ScalarStats(),
            cited_cite=ScalarStats())),
        'scan_page': TableStats(1_504_000.0, _obj(
            id=ScalarStats(), cluster_id=ScalarStats(), page_no=ScalarStats(),
            image_path=ScalarStats(), pdf_path=ScalarStats(), dpi=ScalarStats(),
            parsed_text=ScalarStats(embeddable=True, avg_tokens=640.0))),
        'audio': TableStats(3_640.0, _obj(
            id=ScalarStats(), docket_id=ScalarStats(), local_path=ScalarStats(),
            source_url=ScalarStats(), duration_s=ScalarStats())),
        'audio_segment': TableStats(1_164_800.0, _obj(
            id=ScalarStats(), audio_id=ScalarStats(), start_s=ScalarStats(),
            end_s=ScalarStats(), speaker=ScalarStats(),
            transcript=ScalarStats(embeddable=True, avg_tokens=55.0))),
    }),
    derivations=FrozenDict.of({
        FieldRef('cluster', ('scan_pages',)): DerivationSpec(
            FieldRef('cluster', ('scan_pages',)), FieldRef('cluster', ('scan_pdf_url',)),
            Derivation.RASTERIZE, 'NONE', 47.0, 0.031),      # bespoke, no model
        FieldRef('cluster', ('scan_text',)): DerivationSpec(
            FieldRef('cluster', ('scan_text',)), FieldRef('cluster', ('scan_pages',)),
            Derivation.DOC_PARSE, 'DOC_PARSE_MODEL', 47.0, 0.44),
        FieldRef('docket', ('argument',)): DerivationSpec(
            FieldRef('docket', ('argument',)), FieldRef('audio', ('local_path',)),
            Derivation.ASR, 'ASR_MODEL', 320.0, 94.0),
        FieldRef('opinion', ('chunks',)): DerivationSpec(
            FieldRef('opinion', ('chunks',)), FieldRef('opinion', ('plain_text',)),
            Derivation.CHUNK, 'NONE', 42.0, 0.004),
    }),
)

ACTIVE: CorpusStats = LEGAL
"""The stats the optimizer plans against. A second context reassigns this."""
