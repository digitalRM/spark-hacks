"""AST -> plan tree, structurally and without a single optimization.

Every predicate becomes its own operator in the order the query wrote it. Nothing is
pushed into SQL, nothing is reordered, nothing is bound intelligently, nothing exits
early. The result is correct and slow, which makes it two useful things at once: the
input the optimizer's passes rewrite, and — bound to the heaviest eligible model — the
naive baseline the benchmark measures against.

The only work here that is *not* optional is the work the language requires: a derived
field needs a Materialize before anything can read it, and an unnest needs an
Expand/Collapse pair. Those are lowering, not optimization.

Cost: one walk of the AST. No I/O, no model calls.
"""

from dataclasses import dataclass
from typing import Callable

from query_language.ast import (
    Aggregator, And, Between, Comparison, Condition, Date,
    Expression, FieldRef, Fuzzy, InList, Join, Like, Not, Or, Query, Source, TableRef,
    Unnest, pp_condition, pp_field_ref,
)
from query_language.type_system import (
    AudioType, ImageType, Schema, TextType,
)
from query_language.typechecker import Env, modal_base, resolve_expression, typecheck
from optimizer.plan import (
    AggSpec, Collapse, Column, ExactFilter, Expand, Grain, Limit, Materialize, PlanNode,
    PredicateClass, Project, Provenance, Scan, SelectivitySource, SemanticFilter, Union,
    Aggregate, grain,
)
from optimizer.stats import ACTIVE, CorpusStats, derivation, fanout

UNBOUND = 'UNBOUND'
"""Placeholder model. Binding is pass 3's job; lowering only records that a model is
needed. Pass a `bind` function to get something executable out of lowering alone."""

NAIVE_SELECTIVITY = 0.5
"""No estimate yet — pass 3 replaces this. 0.5 is deliberately useless rather than
plausible, so an unestimated plan is obvious in the funnel."""

type Binder = Callable[[PredicateClass], str]

def unbound(_: PredicateClass) -> str:
    return UNBOUND

class LoweringError(Exception): pass

# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    env: Env
    tables: dict[str, str]          # alias -> table name
    stats: CorpusStats
    bind: Binder
    unnested_in_select: frozenset[tuple[str, ...]]
    counter: int = 0

    def nid(self, tag: str) -> str:
        """Deterministic ids: same AST in, same ids out, so plans are comparable."""
        self.counter += 1
        return f'{tag}{self.counter}'

    def rooted(self, r: FieldRef) -> FieldRef:
        """Rewrite a query-alias-rooted ref to a table-rooted one, which is what stats
        are keyed by."""
        return FieldRef(self.tables.get(r.source, r.source), r.path)

# ---------------------------------------------------------------------------

def lower(q: Query, schema: Schema, stats: CorpusStats = ACTIVE,
          bind: Binder = unbound) -> PlanNode:
    """Lower a typechecked query into an unoptimized plan tree.

    Raises LoweringError for constructs the plan IR cannot yet express. Typechecking
    runs first, so a plan is never built from a query we could not validate.
    """
    env = typecheck(q, schema)
    ctx = _Ctx(env=env, tables=_aliases(q.source), stats=stats, bind=bind,
               unnested_in_select=_select_unnests(q))

    plan: PlanNode = _scan(q.source, ctx)
    if q.where is not None:
        plan = _condition(q.where, plan, ctx)
    if q.group_by:
        plan = _aggregate(q, plan, ctx)
    if q.limit is not None:
        # early_exit stays False: proving it legal is pass 4's job.
        plan = Limit(ctx.nid('limit'), q.limit, False, plan)
    return Project(ctx.nid('project'), _columns(q, ctx), plan)

# ---------------------------------------------------------------------------
# FROM
# ---------------------------------------------------------------------------

def _aliases(s: Source) -> dict[str, str]:
    match s:
        case TableRef(name, alias):    return {alias: name}
        case Join(_, left, right):     return _aliases(left) | _aliases(right)
        case _: raise LoweringError(f'unknown source: {s!r}')

def _scan(s: Source, ctx: _Ctx) -> Scan:
    """One Scan for the whole FROM clause.

    Join conditions go into the SQL because there is no cross-product operator to filter
    above — and there should not be one, since materializing a cross product is never the
    right plan. This is the one thing lowering does that looks like an optimization and
    is not: it is the only way to express a join at all.
    """
    tables = _aliases(s)
    frm, params = _sql(s, ctx)
    return Scan(ctx.nid('scan'), tuple(dict.fromkeys(tables.values())),
                f'SELECT * FROM {frm}', tuple(params),
                (), 1.0, SelectivitySource.STATIC, _base_grain(s))

def _base_grain(s: Source) -> Grain:
    """The leftmost table names the grain — one record per row of it."""
    match s:
        case TableRef(name, _):  return (name,)
        case Join(_, left, _):   return _base_grain(left)
        case _: raise LoweringError(f'unknown source: {s!r}')

def _sql(s: Source, ctx: _Ctx) -> tuple[str, list[object]]:
    match s:
        case TableRef(name, alias):
            return f'{name} AS {alias}', []
        case Join(cond, left, right):
            ls, lp = _sql(left, ctx)
            rs, rp = _sql(right, ctx)
            cs, cp = _sql_condition(cond, ctx)
            return f'{ls} JOIN {rs} ON {cs}', [*lp, *rp, *cp]
        case _: raise LoweringError(f'unknown source: {s!r}')

def _sql_condition(c: Condition, ctx: _Ctx) -> tuple[str, list[object]]:
    """Render a deterministic condition as SQL. Refuses anything semantic: a join on a
    Fuzzy is a SemanticJoin, which the executor does not implement."""
    match c:
        case Comparison(op, l, r):
            ls, lp = _sql_expression(l, ctx)
            rs, rp = _sql_expression(r, ctx)
            return f'{ls} {op.value} {rs}', [*lp, *rp]
        case InList(f, values):
            return (f'{_sql_ref(f)} IN ({", ".join("?" * len(values))})',
                    [_param(v) for v in values])
        case Between(f, lo, hi):
            return f'{_sql_ref(f)} BETWEEN ? AND ?', [_param(lo), _param(hi)]
        case Like(f, pattern):
            return f'{_sql_ref(f)} LIKE ?', [pattern]
        case And(children):
            parts = [_sql_condition(ch, ctx) for ch in children]
            return ' AND '.join(p for p, _ in parts), [x for _, ps in parts for x in ps]
        case Or(children):
            parts = [_sql_condition(ch, ctx) for ch in children]
            return ' OR '.join(p for p, _ in parts), [x for _, ps in parts for x in ps]
        case Not(child):
            s, p = _sql_condition(child, ctx)
            return f'NOT ({s})', p
        case Fuzzy():
            raise LoweringError('semantic join conditions are not supported; '
                                'SemanticJoin exists in the IR but nothing executes it')
        case _: raise LoweringError(f'cannot render as SQL: {c!r}')

def _sql_expression(e: Expression, ctx: _Ctx) -> tuple[str, list[object]]:
    match e:
        case FieldRef() as r: return _sql_ref(r), []
        case Unnest():        raise LoweringError('cannot unnest inside SQL')
        case Aggregator():    raise LoweringError('cannot aggregate inside a join condition')
        case _:               return '?', [_param(e)]

def _param(value: object) -> object:
    """A literal as a bound SQL parameter. A Date binds as its ISO-8601 text, which is
    what the column holds and what SQLite orders correctly."""
    return value.value if isinstance(value, Date) else value

def _sql_ref(r: FieldRef) -> str:
    return pp_field_ref(r)

# ---------------------------------------------------------------------------
# WHERE
# ---------------------------------------------------------------------------

def _condition(c: Condition, child: PlanNode, ctx: _Ctx) -> PlanNode:
    """Lower a condition into operators stacked above `child`.

    Conjunction is a chain, disjunction is a Union of chains. That is enough to lower any
    boolean structure without normalizing it — normalization is pass 1's job, and its
    point is that a Union in the middle of a pipeline is harder to optimize than a Union
    at the top, not that this shape is wrong.
    """
    match c:
        case And(children):
            for ch in children:
                child = _condition(ch, child, ctx)
            return child
        case Or(children):
            return Union(ctx.nid('union'),
                         tuple(_condition(ch, child, ctx) for ch in children))
        case Not(Fuzzy() as f):
            return _fuzzy(f, child, ctx, negated=True)
        case Not(child_cond):
            return _exact(child_cond, child, ctx, negated=True)
        case Fuzzy() as f:
            return _fuzzy(f, child, ctx, negated=False)
        case _:
            return _exact(c, child, ctx, negated=False)

def _exact(c: Condition, child: PlanNode, ctx: _Ctx, negated: bool) -> ExactFilter:
    """A deterministic predicate as its own operator. Pass 2 absorbs these into the Scan;
    until then they are evaluated one at a time, which is exactly the point."""
    if isinstance(c, (And, Or, Not, Fuzzy)):
        raise LoweringError(f'not a leaf predicate: {c!r}')
    text = pp_condition(c)
    sql, params = _sql_condition(c, ctx)
    return ExactFilter(ctx.nid('exact'),
                       f'NOT ({text})' if negated else text,
                       f'NOT ({sql})' if negated else sql, tuple(params),
                       NAIVE_SELECTIVITY, child)

def _fuzzy(f: Fuzzy, child: PlanNode, ctx: _Ctx, negated: bool) -> PlanNode:
    """A semantic predicate, plus whatever the language requires to reach its field.

    A reference through `unnest` widens the grain, so it becomes Expand ... Collapse. A
    derived field cannot be read until it exists, so it becomes a Materialize below. Both
    are required for correctness; neither is an optimization.
    """
    cls = _modality(f.field, ctx)
    match f.field:
        case Unnest(ref):
            rooted = ctx.rooted(ref)
            child = _materialize(rooted, child, ctx)
            child = Expand(ctx.nid('expand'), rooted, fanout(ctx.stats, rooted),
                           Provenance.PLACEHOLDER, child)
            inner = grain(child)
            filt = _semantic(f, cls, rooted, child, ctx, negated)
            # The element stays live if the query projects it, and a Collapse would throw
            # away exactly the binding the projection needs.
            if (ref.source, *ref.path) in ctx.unnested_in_select:
                return filt
            return Collapse(ctx.nid('collapse'), inner[:-1], filt)
        case FieldRef() as ref:
            rooted = ctx.rooted(ref)
            child = _materialize(rooted, child, ctx)
            return _semantic(f, cls, rooted, child, ctx, negated)
        case _:
            raise LoweringError(f'fuzzy over unsupported expression: {f.field!r}')

def _semantic(f: Fuzzy, cls: PredicateClass, ref: FieldRef, child: PlanNode,
              ctx: _Ctx, negated: bool) -> SemanticFilter:
    return SemanticFilter(
        ctx.nid('sem'), cls, ref, f.text, negated, ctx.bind(cls),
        'lowered without binding' if ctx.bind is unbound else 'naive: heaviest eligible',
        NAIVE_SELECTIVITY, SelectivitySource.STATIC, None, False, child)

def _materialize(ref: FieldRef, child: PlanNode, ctx: _Ctx) -> PlanNode:
    """Insert a Materialize if the field does not exist until something produces it."""
    d = derivation(ctx.stats, ref)
    if d is None: return child
    # A derivation with no model role is bespoke deterministic code, not a model call.
    model = f'bespoke:{d.method.value}' if d.model_role == 'NONE' else ctx.bind(PredicateClass.SEM)
    return Materialize(ctx.nid('mat'), d.source, d.produces, d.method, model,
                       f'{d.method.value} is required before {pp_field_ref(ref)} exists',
                       child)

def _modality(e: Expression, ctx: _Ctx) -> PredicateClass:
    """FUZZY resolves by field type alone — no model call, no heuristic.

    This is refinement, and it is here rather than in a pass because the type system
    already decided it: a predicate over an Image field is a VISUAL predicate and there
    is nothing to optimize about that.
    """
    match modal_base(resolve_expression(e, ctx.env), 'fuzzy'):
        case TextType():  return PredicateClass.SEM
        case ImageType(): return PredicateClass.VISUAL
        case AudioType(): return PredicateClass.AUDIO
        case t: raise LoweringError(f'no predicate class for modal type {t!r}')

# ---------------------------------------------------------------------------
# GROUP BY, SELECT
# ---------------------------------------------------------------------------

def _aggregate(q: Query, child: PlanNode, ctx: _Ctx) -> Aggregate:
    keys = tuple(e for e in q.group_by if isinstance(e, FieldRef))
    if len(keys) != len(q.group_by):
        raise LoweringError('GROUP BY takes fields, not expressions')
    aggs = tuple(_agg(e, ctx) for e in q.select if isinstance(e, Aggregator))
    return Aggregate(ctx.nid('agg'), keys, aggs, _grain_of_keys(keys, ctx), child)

def _agg(a: Aggregator, ctx: _Ctx) -> AggSpec:
    match a.arg:
        case None:            return AggSpec(a.op.value, None, f'{a.op.value}(*)')
        case FieldRef() as r: return AggSpec(a.op.value, r, f'{a.op.value}({pp_field_ref(r)})')
        case other:           raise LoweringError(f'cannot aggregate {other!r}')

def _grain_of_keys(keys: tuple[FieldRef, ...], ctx: _Ctx) -> Grain:
    return tuple(dict.fromkeys(ctx.tables.get(k.source, k.source) for k in keys))

def _columns(q: Query, ctx: _Ctx) -> tuple[Column, ...]:
    out: list[Column] = []
    for e in q.select:
        match e:
            case FieldRef() as r:
                out.append(Column(pp_field_ref(r), r))
            case Unnest(ref):
                out.append(Column(f'unnest({pp_field_ref(ref)})', ref, unnest=True))
            case Aggregator() as a:
                spec = _agg(a, ctx)
                out.append(Column(spec.label, spec.arg, agg=spec))
            case other:
                raise LoweringError(f'cannot project {other!r}')
    return tuple(out)

def _select_unnests(q: Query) -> frozenset[tuple[str, ...]]:
    """Paths the query projects through `unnest` — the ones whose elements stay live."""
    return frozenset((e.ref.source, *e.ref.path) for e in q.select if isinstance(e, Unnest))

# ---------------------------------------------------------------------------

def naive_binder(estimator) -> Binder:
    """Bind every predicate to the *heaviest* eligible model — the naive plan of §10,
    and the top of the benchmark table."""
    def bind(cls: PredicateClass) -> str:
        models = estimator.eligible_models(cls)
        return models[-1].name if models else UNBOUND
    return bind
