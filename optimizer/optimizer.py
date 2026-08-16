"""The optimizer's entry point. Today it lowers, and stops.

`lower()` already turns a typechecked AST into a correct, unoptimized plan tree. That is
pass 0 and it is the only pass that exists, so this module is currently a one-line
pipeline -- but it is the module `api.driver` looks for, so wiring it now means the whole
system runs end to end and every later pass is a line added to `PASSES` rather than a
change anywhere else.

    optimize(ast, schema) -> Optimized     the plan plus one snapshot per pass
    to_json(optimized)    -> dict          what the plan pane steps through

Snapshots are the point. A single snapshot named `lower` is honest about there being no
optimization yet; when P1..P11 land, the same structure shows the plan collapsing rule by
rule, which is the evidence that an optimizer exists rather than a fixed order being
printed.

Cost: one walk of the AST, plus one costing walk per snapshot for the funnel. No I/O
beyond reading calibration.json once, and no model calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from query_language.ast import Query
from query_language.type_system import Schema

from optimizer.estimator import StaticEstimator, estimate
from optimizer.lower import UNBOUND, lower
from optimizer.plan import PlanNode, PlanWarning, Snapshot, walk
from optimizer.plan_editing import snapshot_json, warning_json
from optimizer.stats import ACTIVE, CorpusStats

PASSES: tuple[str, ...] = ('lower',)
"""Every pass that runs, in order. P1..P11 from the design doc append here."""

_ESTIMATOR: StaticEstimator | None = None

def estimator() -> StaticEstimator:
    """One estimator per process. Constructing it reads calibration.json, so it is not
    something to do per request."""
    global _ESTIMATOR
    if _ESTIMATOR is None: _ESTIMATOR = StaticEstimator()
    return _ESTIMATOR

@dataclass(frozen=True)
class Optimized:
    """A plan, how it got there, and anything the optimizer wants said about it."""
    plan: PlanNode
    snapshots: tuple[Snapshot, ...]
    warnings: tuple[PlanWarning, ...] = ()

def optimize(ast: Query, schema: Schema, stats: CorpusStats = ACTIVE) -> Optimized:
    """Turn a typechecked query into an execution plan.

    Deterministic: same AST, same schema, same stats produce a byte-identical plan. That
    is the difference between a cost-based optimizer and a prompt with extra steps, and it
    holds trivially today because the only pass is structural lowering.

    Raises LoweringError for constructs the plan IR cannot express yet.
    """
    plan = lower(ast, schema, stats)
    note = ('structural lowering only: every predicate is its own operator in written '
            'order, nothing pushed down, reordered, bound, or exited early')
    return Optimized(plan, (Snapshot('lower', note, plan),))

def is_bound(plan: PlanNode) -> bool:
    """Whether every node that needs a model has one."""
    return all(getattr(n, 'bound_model', UNBOUND) != UNBOUND for n in walk(plan))

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
    import json
    from query_language import schema as registry
    from query_language.bridge import registry_to_schema
    from query_language.ast import example

    reg = registry.load('courtlistener')
    out = to_json(optimize(example(), registry_to_schema(reg)))
    snap = out['snapshots'][0]
    print(f"passes: {out['passes']}")
    print(f"nodes:  {len(json.dumps(snap['plan']))} bytes of plan JSON")
    print(f"funnel: {'costed' if snap['estimate'] else 'not costable yet (UNBOUND models)'}")
    print(json.dumps(snap['plan'], indent=2)[:400] + ' ...')

if __name__ == '__main__':
    _smoke()
