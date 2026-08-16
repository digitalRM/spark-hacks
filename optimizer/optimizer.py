"""The optimizer's entry point: AST in, plan tree plus one snapshot per pass out.

    optimize(ast, schema) -> Optimized     the plan plus one snapshot per pass
    to_json(optimized)    -> dict          what the plan pane steps through

Four passes, and each is a rewrite of the tree the previous one produced:

    lower           AST to operators. Derived fields become Materialize, unnest becomes
                    Expand/Collapse, FUZZY resolves by field type. Nothing optimized.
    push down       EXACT predicates and foreign-key joins absorbed into one SQL
                    statement, so the index filters before anything expensive runs.
    cost and order  Estimate selectivity, bind models, sort blocks by cost/(1-s).
    finalize        Early exit where nothing can observe which element matched, plus
                    oracle escalation. Annotations only.

Deterministic: the same AST, calibration and stats give a byte-identical plan. That is
the difference between a cost-based optimizer and a prompt with extra steps. No pass
calls a model.

Snapshots are the point. Stepping through them shows the plan collapsing rule by rule,
which is the evidence that an optimizer exists rather than a fixed order being printed.

Cost: a few walks of the tree, plus one costing walk per snapshot for the funnel. No I/O
beyond reading calibration.json once, and no model calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from query_language.ast import Query
from query_language.type_system import Schema

from optimizer.estimator import StaticEstimator, estimate
from optimizer.finalize import ESCALATION_FRACTION, finalize
from optimizer.lower import UNBOUND, Binder, lower, naive_binder, unbound
from optimizer.order import ACCURACY_FLOOR, UPGRADE_ACCURACY_DELTA, cost_and_order
from optimizer.plan import PlanNode, PlanWarning, Snapshot, walk
from optimizer.plan_editing import snapshot_json, warning_json
from optimizer.pushdown import Prober, push_down
from optimizer.stats import ACTIVE, CorpusStats

PASSES: tuple[str, ...] = ('lower', 'push down', 'cost and order', 'finalize')
"""Every pass that runs, in order."""

NOTES: dict[str, str] = {
    'lower':
        'AST to operator tree. Derived fields become Materialize, unnest becomes '
        'Expand/Collapse, and FUZZY resolves to SEM, VISUAL or AUDIO from the field type '
        'alone -- no model call, because the type system already decided it. Every '
        'predicate is its own operator in written order. Bound to the heaviest eligible '
        'model, this is the naive baseline.',
    'push down':
        'Every EXACT predicate and foreign-key join absorbed into a single SQL statement, '
        'so the index evaluates them before anything expensive sees a row. '
        'Semantics-preserving: the same rows for less work. With a prober attached the '
        'scan selectivity becomes a COUNT rather than a prior -- the one quantity in the '
        'cost model that can be measured instead of estimated.',
    'cost and order':
        'Estimate selectivity, bind each predicate to the cheapest model clearing the '
        'accuracy floor, then sort blocks by cost/(1-s): cost per row eliminated. A set-'
        'valued predicate is four operators that move together, folded into one cost and '
        'one selectivity before the sort means anything. Ordering needs a price and a '
        'price needs a model, so binding happens here and is revisited once.',
    'finalize':
        'Early exit wherever nothing downstream can observe which element matched -- a '
        'liveness argument, not an annotation -- and oracle escalation on the least '
        'confident fraction of each local model\'s answers. Nothing moves.',
}

_ESTIMATOR: StaticEstimator | None = None

def estimator() -> StaticEstimator:
    """One estimator per process. Constructing it reads calibration.json, so it is not
    something to do per request."""
    global _ESTIMATOR
    if _ESTIMATOR is None: _ESTIMATOR = StaticEstimator()
    return _ESTIMATOR

@dataclass(frozen=True)
class Optimized:
    """A plan, how it got there, and anything the optimizer wants said about it.

    Warnings ride alongside rather than inside the plan: a plan is a plan, and what we
    think of it is a separate question."""
    plan: PlanNode
    snapshots: tuple[Snapshot, ...]
    warnings: tuple[PlanWarning, ...] = ()

    @property
    def blocked(self) -> bool:
        """True when the optimizer refuses to auto-execute and wants a human decision --
        a cardinality cap exceeded, or a plan that failed its own self-check."""
        return any(w.blocking for w in self.warnings)

def optimize(ast: Query, schema: Schema, stats: CorpusStats = ACTIVE,
             est: StaticEstimator | None = None,
             probe: Prober | None = None,
             base_rows: Any = None,
             accuracy_floor: float = ACCURACY_FLOOR,
             upgrade_delta: float = UPGRADE_ACCURACY_DELTA,
             escalation_fraction: float = ESCALATION_FRACTION,
             naive_bind: Binder | None = None) -> Optimized:
    """Turn a typechecked query into an execution plan.

    Deterministic: same AST, schema, stats and calibration produce a byte-identical plan.

    `probe` runs a COUNT for the pushed-down predicates. Supplying one is worth far more
    than it looks -- everything downstream is sized by the scan's output, so measuring it
    rather than assuming a prior moved the flagship query by 30x.

    `naive_bind` binds the lowered plan before any optimization runs, which is only
    useful for producing the naive baseline: it makes the first snapshot costable, and
    that snapshot is the top of the benchmark table.

    Raises LoweringError for constructs the plan IR cannot express yet.
    """
    est = est or estimator()
    snaps: list[Snapshot] = []
    warnings: list[PlanWarning] = []

    plan = lower(ast, schema, stats, naive_bind or unbound)
    snaps.append(Snapshot('lower', NOTES['lower'], plan))

    plan, w = push_down(plan, probe, base_rows)
    warnings += w
    snaps.append(Snapshot('push down', NOTES['push down'], plan))

    plan, w = cost_and_order(plan, est, accuracy_floor, upgrade_delta, stats)
    warnings += w
    snaps.append(Snapshot('cost and order', NOTES['cost and order'], plan))

    plan, w = finalize(plan, est, escalation_fraction, stats)
    warnings += w
    snaps.append(Snapshot('finalize', NOTES['finalize'], plan))

    return Optimized(plan, tuple(snaps), tuple(warnings))

def is_bound(plan: PlanNode) -> bool:
    """Whether every node that needs a model has one.

    Only nodes that carry a bound_model are asked. Defaulting the attribute to UNBOUND
    would make a Scan -- which has no model and needs none -- report the plan unbound,
    and no plan would ever be costable."""
    return all(n.bound_model != UNBOUND
               for n in walk(plan) if hasattr(n, 'bound_model'))

def funnel_for(plan: PlanNode):
    """The predicted funnel for a plan, or None when the plan cannot honestly be costed.

    Lowering binds nothing, and `unit_cost_s` prices an unknown model at 1e9 seconds as a
    deliberate poison value. Costing an unbound plan therefore *succeeds* and returns a
    number roughly three hundred thousand years wide. Refusing to emit a funnel is the
    only honest answer until P8 binds models -- an absurd estimate rendered as an estimate
    is worse than no estimate at all (spec section 8.1).
    """
    if not is_bound(plan): return None
    try: return estimate(plan, estimator())
    except Exception: return None

def to_json(o: Optimized) -> dict[str, Any]:
    """The wire form the plan pane steps through. Free apart from the costing walks."""
    return {
        'passes': list(PASSES),
        'snapshots': [snapshot_json(s, funnel_for(s.plan)) for s in o.snapshots],
        'warnings': [warning_json(w) for w in o.warnings],
        'blocking': any(w.blocking for w in o.warnings),
    }

def _smoke() -> None:
    """python3 -m optimizer.optimizer"""
    from query_language import schema as registry
    from query_language.bridge import registry_to_schema
    from optimizer.fixtures import flagship_query
    from optimizer.lower import naive_binder

    est = estimator()
    schema = registry_to_schema(registry.load('courtlistener'))
    out = to_json(optimize(flagship_query(), schema, est=est,
                           probe=lambda sql, params: 262.0,
                           base_rows=lambda t: est.base_rows(t)[0],
                           naive_bind=naive_binder(est)))
    print(f"passes: {' -> '.join(out['passes'])}\n")
    for s in out['snapshots']:
        f = s['estimate']
        cost = (f"{f['seconds']:>12,.1f}s {f['model_calls']:>10,.0f} calls"
                if f else f"{'not costable (unbound models)':>30}")
        print(f"  {s['pass_name']:<16}{cost}")
    print(f"\nblocking: {out['blocking']}   "
          f"warnings: {[w['code'] for w in out['warnings']] or 'none'}")

if __name__ == '__main__':
    _smoke()
