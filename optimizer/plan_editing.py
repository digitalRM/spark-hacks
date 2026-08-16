"""Everything that exists because a human looks at plans: validation, editing, JSON, text.

Cordoned off deliberately. plan.py and funnel.py assume no UI; this file assumes one, and
is expected to churn as the frontend firms up. Nothing here is imported by the optimizer
or the executor on their happy paths.

Validation lives here rather than beside the plan because if every plan comes from the
optimizer, its rules never fire — they are developer assertions. They become a real error
path only once a human edits a plan, which is this file's concern.
"""

from dataclasses import dataclass, replace
from typing import Any, Callable

from query_language.ast import FieldRef
from optimizer.funnel import Funnel, StageMetrics
from optimizer.plan import (
    Aggregate, AggSpec, Collapse, Column, Derivation, ExactFilter, Expand, Limit,
    Materialize, PlanNode, PlanWarning, PredicateClass, Project, Provenance, Retrieve,
    Scan, SelectivitySource, SemanticFilter, SemanticJoin, Snapshot, Union,
    children, grain, is_filter, pipeline, walk,
)

# ---------------------------------------------------------------------------
# Tree rebuilding
# ---------------------------------------------------------------------------

def with_children(n: PlanNode, cs: tuple[PlanNode, ...]) -> PlanNode:
    """Replace n's inputs. The one place that knows how each node names its children."""
    match n:
        case Scan():         return n
        case Union():        return replace(n, children=cs)
        case SemanticJoin(): return replace(n, left=cs[0], right=cs[1])
        case _:              return replace(n, child=cs[0])

def map_nodes(n: PlanNode, f: Callable[[PlanNode], PlanNode]) -> PlanNode:
    """Rebuild bottom-up, applying f to each node after its children are rebuilt."""
    return f(with_children(n, tuple(map_nodes(c, f) for c in children(n))))

def find(n: PlanNode, node_id: str) -> PlanNode | None:
    return next((m for m in walk(n) if m.node_id == node_id), None)

# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

def edit_node(root: PlanNode, node_id: str, **changes: Any) -> PlanNode:
    """Replace fields on one node — model rebinding, cap adjustment, escalation fraction.
    Does not validate; the caller does, so the UI can show what went wrong."""
    return map_nodes(root, lambda n: replace(n, **changes) if n.node_id == node_id else n)

def drop_node(root: PlanNode, node_id: str) -> PlanNode:
    """Splice a single-input operator out, its child taking its place.

    Dropping a filter is legal, and makes the plan cheaper and less precise. That
    tradeoff being available is the point of the editor."""
    def f(n: PlanNode) -> PlanNode:
        if n.node_id != node_id: return n
        cs = children(n)
        if len(cs) != 1:
            raise ValueError(f'cannot drop {kind(n)}: it has {len(cs)} inputs')
        return cs[0]
    return map_nodes(root, f)

def blocks(root: PlanNode) -> tuple[tuple[PlanNode, ...], ...]:
    """Partition the pipeline into reorderable units, leaf-first.

    A block is what the UI shows as one card. A semantic predicate over a set-valued
    field is not one node but four — materialize, expand, filter, collapse — and moving
    the filter without the other three is meaningless. Grouping by grain excursion gets
    this right without tracking parent ids: an Expand opens a block and the Collapse
    returning to the base grain closes it.
    """
    chain = pipeline(root)
    if not chain: return ()
    base = len(grain(chain[0]))
    out: list[tuple[PlanNode, ...]] = []
    cur: list[PlanNode] = []
    for n in chain:
        cur.append(n)
        closes = (isinstance(n, (Scan, Limit, Project, Aggregate))
                  or (is_filter(n) and len(grain(n)) == base)
                  or (isinstance(n, Collapse) and len(grain(n)) == base))
        if closes:
            out.append(tuple(cur))
            cur = []
    if cur: out.append(tuple(cur))
    return tuple(out)

def anchor(block: tuple[PlanNode, ...]) -> PlanNode:
    """The node a block is named by — its filter, or its last node."""
    return next((n for n in block if is_filter(n)), block[-1])

def reorder(root: PlanNode, order: tuple[str, ...]) -> PlanNode:
    """Permute the pipeline's blocks into `order` (anchor node ids), leaf-first.

    Blocks move whole, so a materialization can never be separated from the predicate
    that consumes it — the editor cannot express that mistake in the first place."""
    bs = blocks(root)
    by_anchor = {anchor(b).node_id: b for b in bs}
    if set(order) != set(by_anchor):
        raise ValueError(f'reorder must permute all block anchors: {sorted(by_anchor)}')
    flat = [n for aid in order for n in by_anchor[aid]]
    rebuilt = flat[0]
    for n in flat[1:]:
        rebuilt = with_children(n, (rebuilt,))
    return rebuilt

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    """Structured so the UI can highlight the offending card."""
    node_id: str
    rule: str
    message: str

def validate(
    root: PlanNode,
    eligible: dict[str, tuple[PredicateClass, ...]] | None = None,
    derived: frozenset[str] | None = None,
    allow_unrefined: bool = False,
) -> list[Violation]:
    """Check a plan's structural legality. Pure, O(nodes^2) on a tree of a dozen nodes.

    `eligible` maps model -> the predicate classes it can serve, from calibration.
    `derived` is the set of qualified field names needing materialization, from stats —
    injected rather than imported so this module stays corpus-agnostic. Omit either to
    skip that family. `allow_unrefined` permits FUZZY, legal only before refinement.
    """
    out: list[Violation] = []
    seen: set[str] = set()
    projects_element = any(c.unnest for n in walk(root) if isinstance(n, Project)
                           for c in n.columns)

    for n in walk(root):
        if n.node_id in seen:
            out.append(Violation(n.node_id, 'duplicate_node_id',
                                 f'node id {n.node_id!r} is not unique'))
        seen.add(n.node_id)

        below = set(walk(n)) - {n}
        materialized = {ref_str(m.produces) for m in below if isinstance(m, Materialize)}

        # A filter above an aggregate would filter records the aggregate already folded.
        if is_filter(n) and any(isinstance(m, Aggregate) for m in below):
            out.append(Violation(n.node_id, 'filter_above_aggregate',
                                 f'{kind(n)} runs above a blocking aggregate'))

        match n:
            case SemanticFilter() | Retrieve() | Expand():
                if derived is not None and ref_str(n.field) in derived \
                        and ref_str(n.field) not in materialized:
                    out.append(Violation(n.node_id, 'unmaterialized_field',
                                         f'{ref_str(n.field)} is derived but nothing '
                                         'below this node materializes it'))
            case _: pass

        match n:
            case SemanticFilter(predicate_class=cls, selectivity=s,
                                escalation_fraction=esc, bound_model=model):
                if cls is PredicateClass.FUZZY and not allow_unrefined:
                    out.append(Violation(n.node_id, 'unrefined_predicate',
                                         'FUZZY survived refinement; no model can bind '
                                         'to an unrefined predicate'))
                if not 0.0 <= s <= 1.0:
                    out.append(Violation(n.node_id, 'selectivity_range',
                                         f'selectivity {s} outside [0, 1]'))
                if esc is not None and not 0.0 <= esc <= 1.0:
                    out.append(Violation(n.node_id, 'escalation_range',
                                         f'escalation_fraction {esc} outside [0, 1]'))
                if eligible is not None and cls is not PredicateClass.FUZZY:
                    ok = eligible.get(model)
                    if ok is None:
                        out.append(Violation(n.node_id, 'unknown_model',
                                             f'model {model!r} is not in calibration'))
                    elif cls not in ok:
                        out.append(Violation(n.node_id, 'model_ineligible',
                                             f'{model!r} cannot serve {cls.value.upper()}'))
                # Early exit stops at the first survivor in a group, so it is legal only
                # where no one downstream can tell which survivor it was.
                if n.early_exit and projects_element and len(grain(n)) > 1:
                    out.append(Violation(n.node_id, 'early_exit_observable',
                                         'early_exit is illegal: the query projects the '
                                         'expanded element'))

            case Limit(early_exit=True) if any(isinstance(m, Aggregate) for m in below):
                out.append(Violation(n.node_id, 'early_exit_blocked',
                                     'early_exit is illegal above a blocking aggregate'))

            case Collapse() if len(grain(n)) >= len(grain(n.child)):
                out.append(Violation(n.node_id, 'collapse_narrows',
                                     f'Collapse must narrow: {grain(n.child)} -> {grain(n)}'))

            case Aggregate(aggregators=aggs):
                for a in aggs:
                    if a.arg is None and a.op != 'count':
                        out.append(Violation(n.node_id, 'agg_arg',
                                             f'{a.op} requires an argument'))

            case Union(children=cs):
                gs = {grain(c) for c in cs}
                if len(gs) > 1:
                    out.append(Violation(n.node_id, 'union_grain',
                                         f'union branches disagree on grain: {sorted(gs)}'))

            case _: pass

    return out

# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def ref_str(f: FieldRef) -> str:
    return f.source if not f.path else f'{f.source}.{".".join(f.path)}'

def kind(n: PlanNode) -> str:
    return type(n).__name__

def label(n: PlanNode) -> str:
    """Short label for a funnel row or a node card."""
    match n:
        case Scan(tables=t):                     return f'Scan({", ".join(t)})'
        case ExactFilter(predicate=p):           return f'EXACT({p})'
        case Retrieve(top_k=k):                  return f'Retrieve(top {k})'
        case SemanticFilter(predicate_class=c):  return f'{"¬" if n.negated else ""}{c.value.upper()}'
        case Materialize(method=m):              return f'Materialize({m.value})'
        case Expand(field=f):                    return f'Expand({ref_str(f)})'
        case Collapse():                         return f'Collapse({".".join(grain(n))})'
        case Aggregate(group_by=g):              return f'Aggregate(by {len(g)})'
        case Limit(k=k):                         return f'Limit({k})'
        case Project(columns=c):                 return f'Project({len(c)})'
        case Union(children=cs):                 return f'Union({len(cs)} clauses)'
        case SemanticJoin():                     return 'SemanticJoin'
        case _: raise TypeError(f'unknown plan node: {n!r}')

def render_funnel(f: Funnel, root: PlanNode) -> str:
    """The funnel table. Labels come from the plan, so telemetry carries only ids."""
    if not f.stages: return '(empty funnel)'
    labels = {n.node_id: label(n) for n in walk(root)}
    rows = [(labels.get(s.node_id, s.node_id), s) for s in f.stages]
    w = max(len(l) for l, _ in rows)
    head = f'{f.rows_in:,.0f} records  [{f.provenance.value}]'
    body = '\n'.join(
        f'  → {l:<{w}} {s.rows_out:>10,.0f} out | {s.seconds:>8.1f}s | {s.model_calls:>9,.0f} calls'
        for l, s in rows)
    tail = (f'  = {"":<{w}} {f.rows_out:>10,.0f}     | {f.seconds:>8.1f}s | '
            f'{f.model_calls:>9,.0f} calls ({f.remote_calls:,.0f} remote)')
    return f'{head}\n{body}\n{tail}'

def render_plan(root: PlanNode, indent: int = 0) -> str:
    """The plan tree, root first, children indented — what EXPLAIN prints."""
    pad = '  ' * indent
    lines = [f'{pad}{label(root)}  [{root.node_id}]']
    lines += [render_plan(c, indent + 1) for c in children(root)]
    return '\n'.join(lines)

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def ref_json(f: FieldRef | None) -> dict[str, Any] | None:
    return None if f is None else {'source': f.source, 'path': list(f.path)}

def ref_of(d: dict[str, Any] | None) -> FieldRef | None:
    return None if d is None else FieldRef(d['source'], tuple(d['path']))

def _agg_json(a: AggSpec) -> dict[str, Any]:
    return {'op': a.op, 'arg': ref_json(a.arg), 'label': a.label}

def _agg_of(d: dict[str, Any]) -> AggSpec:
    return AggSpec(d['op'], ref_of(d['arg']), d['label'])

def node_json(n: PlanNode) -> dict[str, Any]:
    """Tagged encoding. `node` is the discriminator the UI switches on; `label` and
    `grain` are denormalized so the frontend never reimplements plan.py."""
    base: dict[str, Any] = {'node_id': n.node_id, 'label': label(n),
                            'grain': list(grain(n)),
                            'children': [node_json(c) for c in children(n)]}
    match n:
        case Scan():
            return base | {'node': 'scan', 'tables': list(n.tables), 'sql': n.sql,
                           'params': list(n.params), 'pushed': list(n.pushed),
                           'selectivity': n.selectivity,
                           'selectivity_source': n.selectivity_source.value}
        case ExactFilter():
            return base | {'node': 'exact_filter', 'predicate': n.predicate,
                           'sql': n.sql, 'params': list(n.params),
                           'selectivity': n.selectivity}
        case Retrieve():
            return base | {'node': 'retrieve', 'field': ref_json(n.field), 'text': n.text,
                           'top_k': n.top_k, 'embed_model': n.embed_model,
                           'rerank_model': n.rerank_model}
        case SemanticFilter():
            return base | {'node': 'semantic_filter',
                           'predicate_class': n.predicate_class.value,
                           'field': ref_json(n.field), 'text': n.text,
                           'negated': n.negated, 'bound_model': n.bound_model,
                           'binding_reason': n.binding_reason,
                           'selectivity': n.selectivity,
                           'selectivity_source': n.selectivity_source.value,
                           'escalation_fraction': n.escalation_fraction,
                           'early_exit': n.early_exit}
        case Materialize():
            return base | {'node': 'materialize', 'source': ref_json(n.source),
                           'produces': ref_json(n.produces), 'method': n.method.value,
                           'bound_model': n.bound_model,
                           'binding_reason': n.binding_reason}
        case Expand():
            return base | {'node': 'expand', 'field': ref_json(n.field),
                           'fanout': n.fanout,
                           'fanout_provenance': n.fanout_provenance.value}
        case Collapse():
            return base | {'node': 'collapse'}
        case Aggregate():
            return base | {'node': 'aggregate',
                           'group_by': [ref_json(g) for g in n.group_by],
                           'aggregators': [_agg_json(a) for a in n.aggregators]}
        case Limit():
            return base | {'node': 'limit', 'k': n.k, 'early_exit': n.early_exit}
        case Project():
            return base | {'node': 'project',
                           'columns': [{'label': c.label, 'ref': ref_json(c.ref),
                                        'unnest': c.unnest,
                                        'agg': None if c.agg is None else _agg_json(c.agg)}
                                       for c in n.columns]}
        case Union():
            return base | {'node': 'union'}
        case SemanticJoin():
            return base | {'node': 'semantic_join', 'left_field': ref_json(n.left_field),
                           'right_field': ref_json(n.right_field), 'text': n.text,
                           'block_top_k': n.block_top_k, 'bound_model': n.bound_model,
                           'selectivity': n.selectivity}
        case _: raise TypeError(f'unknown plan node: {n!r}')

def node_of(d: dict[str, Any]) -> PlanNode:
    nid = d['node_id']
    cs = [node_of(c) for c in d['children']]
    g = tuple(d['grain'])
    match d['node']:
        case 'scan':
            return Scan(nid, tuple(d['tables']), d['sql'], tuple(d['params']),
                        tuple(d['pushed']), d['selectivity'],
                        SelectivitySource(d['selectivity_source']), g)
        case 'exact_filter':
            return ExactFilter(nid, d['predicate'], d['sql'], tuple(d['params']),
                               d['selectivity'], cs[0])
        case 'retrieve':
            return Retrieve(nid, ref_of(d['field']), d['text'], d['top_k'],
                            d['embed_model'], d['rerank_model'], cs[0])
        case 'semantic_filter':
            return SemanticFilter(nid, PredicateClass(d['predicate_class']),
                                  ref_of(d['field']), d['text'], d['negated'],
                                  d['bound_model'], d['binding_reason'],
                                  d['selectivity'],
                                  SelectivitySource(d['selectivity_source']),
                                  d['escalation_fraction'], d['early_exit'], cs[0])
        case 'materialize':
            return Materialize(nid, ref_of(d['source']), ref_of(d['produces']),
                               Derivation(d['method']), d['bound_model'],
                               d['binding_reason'], cs[0])
        case 'expand':
            return Expand(nid, ref_of(d['field']), d['fanout'],
                          Provenance(d['fanout_provenance']), cs[0])
        case 'collapse':
            return Collapse(nid, g, cs[0])
        case 'aggregate':
            return Aggregate(nid, tuple(ref_of(x) for x in d['group_by']),
                             tuple(_agg_of(a) for a in d['aggregators']), g, cs[0])
        case 'limit':
            return Limit(nid, d['k'], d['early_exit'], cs[0])
        case 'project':
            return Project(nid, tuple(Column(c['label'], ref_of(c['ref']), c['unnest'],
                                             None if c['agg'] is None else _agg_of(c['agg']))
                                      for c in d['columns']), cs[0])
        case 'union':
            return Union(nid, tuple(cs))
        case 'semantic_join':
            return SemanticJoin(nid, cs[0], cs[1], ref_of(d['left_field']),
                                ref_of(d['right_field']), d['text'], d['block_top_k'],
                                d['bound_model'], d['selectivity'])
        case _: raise ValueError(f'unknown node tag: {d["node"]!r}')

def funnel_json(f: Funnel) -> dict[str, Any]:
    return {'provenance': f.provenance.value, 'seconds': f.seconds,
            'model_calls': f.model_calls, 'remote_calls': f.remote_calls,
            'rows_in': f.rows_in, 'rows_out': f.rows_out,
            'stages': [{'node_id': s.node_id, 'rows_in': s.rows_in,
                        'rows_out': s.rows_out, 'model_calls': s.model_calls,
                        'seconds': s.seconds, 'provenance': s.provenance.value,
                        'remote_calls': s.remote_calls, 'cache_hits': s.cache_hits,
                        'degraded': s.degraded} for s in f.stages]}

def funnel_of(d: dict[str, Any]) -> Funnel:
    return Funnel(tuple(StageMetrics(s['node_id'], s['rows_in'], s['rows_out'],
                                     s['model_calls'], s['seconds'],
                                     Provenance(s['provenance']), s['remote_calls'],
                                     s['cache_hits'], s['degraded'])
                        for s in d['stages']))

def warning_json(w: PlanWarning) -> dict[str, Any]:
    return {'code': w.code, 'message': w.message, 'node_id': w.node_id,
            'blocking': w.blocking}

def warning_of(d: dict[str, Any]) -> PlanWarning:
    return PlanWarning(d['code'], d['message'], d['node_id'], d['blocking'])

def snapshot_json(s: Snapshot, f: Funnel | None = None) -> dict[str, Any]:
    """A snapshot plus its funnel. The estimate rides alongside rather than inside,
    because a Snapshot is a plan and nothing else."""
    return {'pass_name': s.pass_name, 'note': s.note, 'plan': node_json(s.plan),
            'estimate': None if f is None else funnel_json(f)}

def snapshot_of(d: dict[str, Any]) -> tuple[Snapshot, Funnel | None]:
    return (Snapshot(d['pass_name'], d['note'], node_of(d['plan'])),
            None if d['estimate'] is None else funnel_of(d['estimate']))
