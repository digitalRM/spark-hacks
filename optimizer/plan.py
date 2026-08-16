"""The execution plan: a tree of operators. Leaves read, the root emits.

Children are inputs, so evaluation runs bottom-up and the root is the final result.
A plan is just a PlanNode — there is no wrapper, because there is nothing true of a
plan that is not true of its root.

Grain is not stored. Only Scan, Collapse and Aggregate declare one; Expand derives it
from its child, and every filter inherits. `grain()` computes it.

This module knows nothing about JSON, rendering, validation, or editing — see
plan_editing.py. It assumes no UI exists.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator

from query_language.ast import FieldRef

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

class PredicateClass(Enum):
    """What machinery evaluates a predicate. EXACT is free; the rest cost model calls.

    FUZZY is what the compiler emits and what type refinement removes. It is legal only
    in pre-refinement snapshots: an unrefined predicate has no bindable model."""
    FUZZY  = 'fuzzy'
    EXACT  = 'exact'
    SIM    = 'sim'
    SEM    = 'sem'
    VISUAL = 'visual'
    AUDIO  = 'audio'

SEMANTIC_CLASSES = (PredicateClass.SIM, PredicateClass.SEM,
                    PredicateClass.VISUAL, PredicateClass.AUDIO)

class Derivation(Enum):
    """How a derived field is produced from a stored one."""
    RASTERIZE = 'rasterize'  # pdf -> page images
    DOC_PARSE = 'doc_parse'  # page image -> text
    ASR       = 'asr'        # audio -> transcript segments
    CHUNK     = 'chunk'      # long text -> chunks

class Provenance(Enum):
    """Measurement discipline: every displayed number carries one of these."""
    MEASURED     = 'measured'
    EXTRAPOLATED = 'extrapolated'
    PLACEHOLDER  = 'placeholder'

class SelectivitySource(Enum):
    STATIC = 'static'  # prior from calibration
    PROBED = 'probed'  # measured on a sample, or counted against the index

type Grain = tuple[str, ...]
"""What one record is at a point in the tree.

('cluster',) is one cluster; ('cluster', 'scan_pages') is one (cluster, page) pair.
Expand is the only operator that widens, which is the plan-level statement of the rule
that `unnest` is the only grain-widening construct in the language.
"""

@dataclass(frozen=True)
class AggSpec:
    op: str                  # 'count' | 'sum' | 'avg' | 'min' | 'max'
    arg: FieldRef | None     # None = count(*)
    label: str

@dataclass(frozen=True)
class Column:
    """One projected output column."""
    label: str
    ref: FieldRef | None = None
    unnest: bool = False
    """Projects the expanded element itself. This is what makes the element observable
    downstream, and therefore what forbids early exit on the filter that produced it."""
    agg: AggSpec | None = None

# ---------------------------------------------------------------------------
# Leaves
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scan:
    """Read from the local index. Pushed-down EXACT predicates and equality joins along
    foreign keys collapse into one SQL statement. Zero model calls.

    `selectivity` can be PROBED for free — a COUNT against the index costs milliseconds,
    so the deterministic half of a plan never has to guess. Only semantic selectivity
    needs priors or sampling."""
    node_id: str
    tables: tuple[str, ...]
    sql: str
    params: tuple[Any, ...]
    pushed: tuple[str, ...]        # printed predicates absorbed into the SQL
    selectivity: float
    selectivity_source: SelectivitySource
    scan_grain: Grain

# ---------------------------------------------------------------------------
# Single-input operators
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExactFilter:
    """A deterministic predicate that did not make it into the Scan. Zero model calls.

    Every one surviving to a final plan is a pushdown failure; it exists so the
    pre-pushdown snapshot is honest, and so a genuinely unpushable predicate has a home."""
    node_id: str
    predicate: str
    selectivity: float
    child: 'PlanNode'

@dataclass(frozen=True)
class Retrieve:
    """Vector search then rerank, keeping top_k. Corpus embeddings are precomputed at
    index time, so this costs one query embedding plus a rerank call per candidate."""
    node_id: str
    field: FieldRef
    text: str
    top_k: int
    embed_model: str
    rerank_model: str
    child: 'PlanNode'

@dataclass(frozen=True)
class SemanticFilter:
    """SEM / VISUAL / AUDIO: one model call per record received.

    early_exit is a decision, not a property of the predicate: it is legal exactly when
    nothing downstream observes the expanded element, so the executor may stop at the
    first survivor within a group."""
    node_id: str
    predicate_class: PredicateClass
    field: FieldRef
    text: str
    negated: bool
    bound_model: str
    binding_reason: str
    selectivity: float
    selectivity_source: SelectivitySource
    escalation_fraction: float | None   # None = no cascade
    early_exit: bool
    child: 'PlanNode'

@dataclass(frozen=True)
class Materialize:
    """Produce a derived field. A first-class operator because transcription dominates
    the AUDIO predicate that reads its output — hidden inside a semantic backend, the
    cost model would plan against a fiction.

    It sits directly beneath the predicate that needs it, so "nested inside its parent"
    is the tree rather than a stored parent id."""
    node_id: str
    source: FieldRef
    produces: FieldRef
    method: Derivation
    bound_model: str
    binding_reason: str
    child: 'PlanNode'

@dataclass(frozen=True)
class Expand:
    """Widen the grain — the plan-level image of `unnest`. One record in, `fanout` out.

    A multiplication rather than a filter, so it is the largest cost lever in any plan
    containing one, and it is a visible operator so the funnel shows the explosion
    instead of burying it in the filter above."""
    node_id: str
    field: FieldRef
    fanout: float
    fanout_provenance: Provenance
    child: 'PlanNode'

@dataclass(frozen=True)
class Collapse:
    """Narrow the grain, deduplicating on the target keys. Emitted where a query widened
    in order to filter but does not project the element."""
    node_id: str
    collapse_grain: Grain
    child: 'PlanNode'

@dataclass(frozen=True)
class Aggregate:
    """Blocking: runs after all filtering, with no short-circuit and no early exit above.

    Degraded counts travel alongside aggregate values at execution time — a COUNT over
    partially-degraded rows is otherwise a lie."""
    node_id: str
    group_by: tuple[FieldRef, ...]
    aggregators: tuple[AggSpec, ...]
    agg_grain: Grain
    child: 'PlanNode'

@dataclass(frozen=True)
class Limit:
    """early_exit=True stops execution once k records survive. Requires no ORDER BY and
    no blocking aggregate above."""
    node_id: str
    k: int
    early_exit: bool
    child: 'PlanNode'

@dataclass(frozen=True)
class Project:
    """The root of a plan: what the query returns."""
    node_id: str
    columns: tuple[Column, ...]
    child: 'PlanNode'

# ---------------------------------------------------------------------------
# Multi-input operators
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Union:
    """One child per disjunctive-normal-form clause; results are unioned and deduped.

    Clauses share their Scan prefix, which the tree duplicates. A real optimizer would
    make this a DAG with common-subexpression elimination. We do not, because the memo
    cache keyed on (predicate, item, model) already makes the duplicated work free —
    the same argument that makes DNF affordable in the first place."""
    node_id: str
    children: tuple['PlanNode', ...]

@dataclass(frozen=True)
class SemanticJoin:
    """A join whose condition is a semantic predicate: |L| x |R| model calls before
    blocking, |L| x block_top_k after.

    Present so the IR can express what the grammar admits — `Join.condition` accepts any
    Condition, including Fuzzy. Execution is out of scope; the executor raises."""
    node_id: str
    left: 'PlanNode'
    right: 'PlanNode'
    left_field: FieldRef
    right_field: FieldRef
    text: str
    block_top_k: int
    bound_model: str
    selectivity: float

type PlanNode = (Scan | ExactFilter | Retrieve | SemanticFilter | Materialize | Expand
                 | Collapse | Aggregate | Limit | Project | Union | SemanticJoin)

# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def children(n: PlanNode) -> tuple[PlanNode, ...]:
    match n:
        case Scan():                        return ()
        case Union(children=cs):            return cs
        case SemanticJoin(left=l, right=r): return (l, r)
        case _:                             return (n.child,)

def walk(n: PlanNode) -> Iterator[PlanNode]:
    """Every node, parents before children."""
    yield n
    for c in children(n):
        yield from walk(c)

def leaves(n: PlanNode) -> Iterator[Scan]:
    return (m for m in walk(n) if isinstance(m, Scan))

def grain(n: PlanNode) -> Grain:
    """What one record looks like coming out of n. Derived, never stored."""
    match n:
        case Scan(scan_grain=g):              return g
        case Collapse(collapse_grain=g):      return g
        case Aggregate(agg_grain=g):          return g
        case Expand(field=f, child=c):        return (*grain(c), f.path[-1] if f.path else f.source)
        case Union(children=cs):              return grain(cs[0]) if cs else ()
        case SemanticJoin(left=l, right=r):   return (*grain(l), *grain(r))
        case _:                               return grain(n.child)

def is_filter(n: PlanNode) -> bool:
    """True for operators that can only shrink the record set."""
    return isinstance(n, (ExactFilter, Retrieve, SemanticFilter))

def is_blocking(n: PlanNode) -> bool:
    """True for operators that must see every input record before emitting any."""
    return isinstance(n, Aggregate)

def pipeline(n: PlanNode) -> tuple[PlanNode, ...]:
    """The maximal single-input chain rooted at n, leaf-first — the linear run of stages
    the funnel reports and the editor reorders. Stops at a Scan or a multi-input node."""
    chain: list[PlanNode] = []
    cur = n
    while True:
        chain.append(cur)
        cs = children(cur)
        if len(cs) != 1: break
        cur = cs[0]
    chain.reverse()
    return tuple(chain)

# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanWarning:
    """Something the optimizer wants said about a plan it produced. blocking=True means
    it refuses to auto-execute — a DNF clause count past the cap, or a semantic join
    whose estimated pair count exceeds the maximum. Returned alongside a plan, never
    stored inside one."""
    code: str
    message: str
    node_id: str | None = None
    blocking: bool = False

@dataclass(frozen=True)
class Snapshot:
    """A plan as it stood after one optimizer pass. Watching these in sequence is the
    evidence that a real optimizer ran rather than a fixed order being printed."""
    pass_name: str
    note: str
    plan: PlanNode
