"""The flagship query, optimized end to end, as a regression test and a UI fixture.

This used to hand-build the plan the optimizer *should* produce. Now it runs the real
optimizer and asserts properties of the result, which is a much better test: a
hand-written golden only ever describes what someone believed on the day they wrote it.

The flagship query (§17):
    9th Circuit cases citing Graham v. Connor, where the scanned record contains a
    photographic exhibit, and a judge expressed skepticism at oral argument.

It exercises all three modalities, two derivations, two grain excursions, and both
early-exit rules. `python -m optimizer.fixtures` runs the assertions and writes
tests/queries/flagship_plan.json for the frontend.
"""

import json
from pathlib import Path

from query_language.ast import (
    And, Comparison, ComparisonOperator, FieldRef, Fuzzy, Join, Query, TableRef, Unnest,
)
from query_language.bridge import example_courtlistener
from query_language.type_system import Schema
from optimizer.estimator import StaticEstimator, estimate
from optimizer.lower import naive_binder
from optimizer.optimize import Optimized, optimize
from optimizer.plan import PredicateClass, SemanticFilter, Limit, walk
from optimizer.plan_editing import (
    node_json, node_of, render_funnel, render_plan, snapshot_json, validate,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / 'tests' / 'queries' / 'flagship_plan.json'

GRAHAM = '490 U.S. 386'
NINTH_CIRCUIT = 'ca9'

PROBED_SCAN_ROWS = 262.0
"""What a COUNT against the index returns for the flagship query's pushed predicates.
Stands in for a real prober until bootstrap_corpus.py has built the index. TODO: verify."""

def flagship_query() -> Query:
    """Cases, not pages — the projection is what makes early exit legal downstream."""
    return Query(
        select=(FieldRef('cluster', ('id',)), FieldRef('cluster', ('case_name',))),
        source=Join(
            Comparison(ComparisonOperator.EQ,
                       FieldRef('citation', ('citing_opinion_id',)),
                       FieldRef('opinion', ('id',))),
            Join(Comparison(ComparisonOperator.EQ,
                            FieldRef('opinion', ('cluster_id',)),
                            FieldRef('cluster', ('id',))),
                 Join(Comparison(ComparisonOperator.EQ,
                                 FieldRef('cluster', ('docket_id',)),
                                 FieldRef('docket', ('id',))),
                      TableRef('cluster', 'cluster'), TableRef('docket', 'docket')),
                 TableRef('opinion', 'opinion')),
            TableRef('citation', 'citation')),
        where=And((
            Comparison(ComparisonOperator.EQ, FieldRef('docket', ('court_id',)),
                       NINTH_CIRCUIT),
            Comparison(ComparisonOperator.EQ, FieldRef('citation', ('cited_cite',)),
                       GRAHAM),
            Fuzzy(FieldRef('opinion', ('plain_text',)),
                  'the opinion analyzes qualified immunity for excessive force'),
            Fuzzy(Unnest(FieldRef('cluster', ('scan_pages',))),
                  'the page contains a photographic exhibit'),
            Fuzzy(Unnest(FieldRef('docket', ('argument',))),
                  'a judge expressed skepticism'),
        )),
        group_by=(), limit=10)

def flagship_schema() -> Schema:
    """The real corpus registry, not a hand-written stand-in — so a registry change
    breaks this test rather than silently diverging from what the compiler emits."""
    return example_courtlistener()

def flagship(est: StaticEstimator | None = None) -> Optimized:
    est = est or StaticEstimator()
    return optimize(flagship_query(), flagship_schema(), est,
                    probe=lambda sql, params: PROBED_SCAN_ROWS,
                    base_rows=lambda table: est.base_rows(table)[0],
                    naive_bind=naive_binder(est))

# ---------------------------------------------------------------------------

def _regression(o: Optimized, est: StaticEstimator) -> None:
    """Assert what the optimizer is supposed to have done. Each check names the pass it
    covers, so a failure says which pass regressed rather than just that something did."""
    filters = [n for n in walk(o.plan) if isinstance(n, SemanticFilter)]
    by_class = {n.predicate_class: n for n in filters}
    funnels = [estimate(s.plan, est) for s in o.snapshots]

    # lower — every modality resolved from the field's type alone
    assert len(filters) == 3, f'expected 3 semantic filters, got {len(filters)}'
    assert set(by_class) == {PredicateClass.SEM, PredicateClass.VISUAL,
                             PredicateClass.AUDIO}, sorted(c.value for c in by_class)

    # push down — both EXACT predicates absorbed, nothing left as an operator
    scan = next(n for n in walk(o.plan) if type(n).__name__ == 'Scan')
    assert len(scan.pushed) == 2, f'expected 2 pushed predicates, got {scan.pushed}'
    assert 'WHERE' in scan.sql and scan.params, scan.sql
    assert scan.selectivity_source.value == 'probed', 'the prober did not run'

    # cost and order — ascending cost per elimination, and nothing bound remote
    from optimizer.order import _Block
    from optimizer.plan_editing import blocks
    from optimizer.stats import ACTIVE
    ratios = [b.ratio for b in (_Block(x, est, ACTIVE) for x in blocks(o.plan)) if b.movable]
    assert ratios == sorted(ratios), f'blocks are not in cost/(1-s) order: {ratios}'
    assert all(not est.is_remote(n.bound_model) for n in filters), \
        'a predicate is bound to a remote model; escalation should be buying that accuracy'

    # finalize — early exit exactly where the element is unobservable
    assert by_class[PredicateClass.VISUAL].early_exit, 'VISUAL should exit early'
    assert by_class[PredicateClass.AUDIO].early_exit, 'AUDIO should exit early'
    assert not by_class[PredicateClass.SEM].early_exit, \
        'SEM is not inside an expansion and has no element to stop at'
    assert next(n for n in walk(o.plan) if isinstance(n, Limit)).early_exit, \
        'LIMIT with no aggregate should exit early'
    assert all(n.escalation_fraction for n in filters), 'escalation not attached'

    # the whole thing
    assert not o.blocked, [w.message for w in o.warnings if w.blocking]
    assert not validate(o.plan), validate(o.plan)
    assert funnels[-1].model_calls < funnels[0].model_calls / 100, \
        'the optimizer should cut model calls by more than two orders of magnitude'
    assert flagship(est).plan == o.plan, 'optimization is not deterministic'
    assert node_of(json.loads(json.dumps(node_json(o.plan)))) == o.plan, \
        'plan JSON round-trip is not identity'

def _smoke() -> None:
    est = StaticEstimator()
    o = flagship(est)
    _regression(o, est)
    print(f'{len(o.snapshots)} passes, all assertions hold\n')

    for s in o.snapshots:
        f = estimate(s.plan, est)
        print(f'{s.pass_name:<16} {f.seconds:>12,.1f}s {f.model_calls:>10,.0f} calls '
              f'{f.remote_calls:>9,.0f} remote')

    print()
    print(render_plan(o.plan))
    print()
    print(render_funnel(estimate(o.plan, est), o.plan))

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps({
        'query': 'flagship',
        'snapshots': [snapshot_json(s, estimate(s.plan, est)) for s in o.snapshots],
        'warnings': [{'code': w.code, 'message': w.message, 'node_id': w.node_id,
                      'blocking': w.blocking} for w in o.warnings],
    }, indent=2) + '\n')
    print(f'\nwrote {FIXTURE_PATH.name} ({FIXTURE_PATH.stat().st_size:,} bytes)')

if __name__ == '__main__':
    _smoke()
