"""Pass 3 — cost and order. The actual optimizer.

Three steps that cannot be separated, because each needs the next one's answer:

    estimate selectivity  ->  bind a model  ->  sort by cost per elimination

Ordering needs a price; a price needs a bound model; which model is worth upgrading
depends on the order. The cycle is broken by binding provisionally to the cheapest
eligible model, sorting, then revisiting the binding once. No fixpoint, no search.

What gets ordered is *blocks*, not operators. A semantic predicate over a set-valued
field is four operators that move together, and the whole block has to be folded into a
single (cost per input record, fraction surviving) pair before the sort means anything.

Semantics-preserving in its ordering, lossy in its binding: reordering filters returns
the same rows, but a cheap model does not answer identically to an expensive one. That
split is why the benchmark reports agreement rather than equality.

Cost: arithmetic over the tree, plus one sort. No I/O.
"""

from dataclasses import replace

from optimizer.estimator import StaticEstimator
from optimizer.plan import (
    Aggregate, Collapse, Derivation, Expand, Limit, Materialize, PlanNode, PlanWarning,
    PredicateClass, Project, Retrieve, Scan, SelectivitySource, SemanticFilter, Union,
    walk,
)
from optimizer.plan_editing import anchor, blocks, map_nodes, ref_str, reorder
from optimizer.stats import ACTIVE, CorpusStats, coverage, derivation

ACCURACY_FLOOR = 0.85
"""Minimum accuracy_on_labeled_set a model must clear to serve a predicate. Overridable
per query, and exposed in the plan editor — it is the main precision/cost dial."""

UPGRADE_ACCURACY_DELTA = 0.05
"""How much accuracy the next model up must add before it is worth its extra cost."""


# ---------------------------------------------------------------------------

def cost_and_order(root: PlanNode, est: StaticEstimator,
                   accuracy_floor: float = ACCURACY_FLOOR,
                   upgrade_delta: float = UPGRADE_ACCURACY_DELTA,
                   stats: CorpusStats = ACTIVE,
                   ) -> tuple[PlanNode, list[PlanWarning]]:
    """Estimate, bind, and reorder. Returns the rewritten plan and any warnings."""
    warnings: list[PlanWarning] = []
    p = _estimate_selectivity(root, est)
    p = _bind(p, est, accuracy_floor, warnings, stats)
    p = _order(p, est, stats)
    p = _upgrade(p, est, upgrade_delta, stats)
    return p, warnings

# ---------------------------------------------------------------------------
# Step 1 — selectivity
# ---------------------------------------------------------------------------

def _estimate_selectivity(root: PlanNode, est: StaticEstimator) -> PlanNode:
    """Replace lowering's deliberately useless 0.5 with the class prior.

    Static only. A --probe run would sample each predicate on PROBE_N items with its
    cheapest eligible model and use the measured pass rate instead, recording PROBED —
    which is why the source travels with the number rather than being assumed.
    """
    def f(n: PlanNode) -> PlanNode:
        if isinstance(n, SemanticFilter):
            return replace(n, selectivity=est.prior_selectivity(n.predicate_class),
                           selectivity_source=SelectivitySource.STATIC)
        return n
    return map_nodes(root, f)

# ---------------------------------------------------------------------------
# Step 2 — binding
# ---------------------------------------------------------------------------

def _bind(root: PlanNode, est: StaticEstimator, floor: float,
          warnings: list[PlanWarning], stats: CorpusStats) -> PlanNode:
    """Bind the cheapest eligible model that clears the accuracy floor.

    eligible_models() returns cheapest first, so this is a scan for the first acceptable
    one. If nothing clears the floor we take the most accurate available and say so
    loudly — silently shipping a plan below the requested accuracy would be worse than
    an expensive one."""
    def f(n: PlanNode) -> PlanNode:
        match n:
            case SemanticFilter(predicate_class=cls):
                models = est.eligible_models(cls)
                if not models:
                    warnings.append(PlanWarning(
                        'no_eligible_model',
                        f'no calibrated model serves {cls.value.upper()}', n.node_id, True))
                    return n
                ok = [m for m in models if m.accuracy >= floor]
                if ok:
                    m = ok[0]
                    reason = (f'cheapest {cls.value.upper()} model clearing accuracy '
                              f'floor {floor:.2f} (acc {m.accuracy:.2f}, '
                              f'{m.seconds_per_item:.3f}s/item)')
                else:
                    m = max(models, key=lambda x: x.accuracy)
                    reason = (f'no {cls.value.upper()} model clears {floor:.2f}; using the '
                              f'most accurate available (acc {m.accuracy:.2f})')
                    warnings.append(PlanWarning(
                        'below_accuracy_floor',
                        f'{cls.value.upper()} predicate bound below the accuracy floor',
                        n.node_id))
                return replace(n, bound_model=m.name, binding_reason=reason)

            case Materialize(method=method):
                # A derivation's model comes from the derivation section of calibration,
                # which predicate binding cannot see and which cannot see predicate
                # classes. Conflating the two is how a transcription model ends up
                # bound to a yes/no question.
                m = est.derivation_model(method)
                if m is None:
                    return replace(n, bound_model=f'bespoke:{method.value}',
                                   binding_reason='deterministic code, no model')
                return replace(n, bound_model=m.name,
                               binding_reason=f'serves the {method.value} derivation '
                                              f'({m.seconds_per_item:.2f}s/unit)')

            case _: return n
    return map_nodes(root, f)

# ---------------------------------------------------------------------------
# Step 3 — ordering
# ---------------------------------------------------------------------------

class _Block:
    """A run of operators that moves as one, folded into a single filter's worth of
    numbers so the exchange argument applies to it."""

    def __init__(self, nodes: tuple[PlanNode, ...], est: StaticEstimator,
                 stats: CorpusStats) -> None:
        self.nodes = nodes
        self.anchor = anchor(nodes)
        self.id = self.anchor.node_id

        filt = next((n for n in nodes if isinstance(n, (SemanticFilter, Retrieve))), None)
        mat = next((n for n in nodes if isinstance(n, Materialize)), None)
        exp = next((n for n in nodes if isinstance(n, Expand)), None)

        self.movable = filt is not None
        self.produces = frozenset(ref_str(n.produces) for n in nodes
                                  if isinstance(n, Materialize))
        self.consumes = frozenset(
            {ref_str(n.field) for n in nodes if isinstance(n, (SemanticFilter, Retrieve, Expand))}
            | {ref_str(n.source) for n in nodes if isinstance(n, Materialize)}
        ) - self.produces

        fanout = exp.fanout if exp is not None else 1.0
        cov = coverage(stats, mat.produces) if mat is not None else 1.0
        d = derivation(stats, mat.produces) if mat is not None else None
        mat_secs = d.seconds_per_source_unit if d is not None else 0.0
        per_call = (est.unit_cost_s(filt.bound_model)
                    if isinstance(filt, SemanticFilter) else 0.0)
        s_elem = filt.selectivity if isinstance(filt, SemanticFilter) else 1.0

        # Cost of the whole block per record entering it, and the fraction of those
        # records that leave it. Coverage appears in both: it is what a derivation
        # filters, and the records it removes cost nothing.
        self.cost = cov * (mat_secs + fanout * per_call)
        self.selectivity = min(1.0, cov * (1.0 - (1.0 - s_elem) ** fanout))

    @property
    def ratio(self) -> float:
        """Cost per unit of expected elimination. The sort key.

        Note it is cost/(1-s) and not cost/s: s is the fraction that *passes*, so 1-s is
        the fraction eliminated, and we want the least cost per row removed."""
        eliminated = 1.0 - self.selectivity
        return self.cost / eliminated if eliminated > 1e-9 else float('inf')

    def __repr__(self) -> str:
        return (f'<{self.id} cost={self.cost:.2f}s s={self.selectivity:.3f} '
                f'ratio={self.ratio:.1f}>')

def _order(root: PlanNode, est: StaticEstimator, stats: CorpusStats) -> PlanNode:
    """Reorder each pipeline's movable blocks, then recurse into any Union branches."""
    bs = [_Block(b, est, stats) for b in blocks(root)]
    movable = [b for b in bs if b.movable]

    if len(movable) > 1:
        first = bs.index(movable[0])
        head = [b.id for b in bs[:first]]
        tail = [b.id for b in bs[first:] if not b.movable]
        root = reorder(root, tuple([*head, *(b.id for b in _greedy(movable)), *tail]))

    # Union branches are independent funnels — each gets its own ordering, because the
    # selectivities that justify an order differ per clause.
    def f(n: PlanNode) -> PlanNode:
        if isinstance(n, Union):
            return replace(n, children=tuple(_order(c, est, stats) for c in n.children))
        return n
    return map_nodes(root, f)

def _greedy(movable: list[_Block]) -> list[_Block]:
    """Cheapest ratio first, subject to producing before consuming.

    Sorting alone would be optimal for independent filters; the topological constraint is
    for the case where one block materializes a field another reads — scan_page.parsed_text
    depends on cluster.scan_pages, which is itself derived."""
    remaining = sorted(movable, key=lambda b: b.ratio)
    available: set[str] = set()
    out: list[_Block] = []
    while remaining:
        pick = next((b for b in remaining if _ready(b, remaining, available)), remaining[0])
        remaining.remove(pick)
        available |= pick.produces
        out.append(pick)
    return out

def _ready(b: _Block, remaining: list[_Block], available: set[str]) -> bool:
    """True when nothing still to be scheduled produces a field b needs."""
    pending = {f for other in remaining if other is not b for f in other.produces}
    return not (b.consumes & pending) - available

# ---------------------------------------------------------------------------
# Step 4 — the upgrade pass
# ---------------------------------------------------------------------------

UPGRADE_COST_CEILING = 2.0
"""How many times the current per-call cost an upgrade may reach. Without this the pass
upgrades on accuracy alone and cheerfully pays 14x for +0.08."""

def _upgrade(root: PlanNode, est: StaticEstimator, delta: float,
             stats: CorpusStats) -> PlanNode:
    """Revisit the binding of the earliest movable block, once.

    The earliest filter is the one worth spending on: a false positive there is
    self-correcting, since the row survives and gets caught downstream, but a false
    negative is unrecoverable and it destroys more rows than anywhere else in the plan.

    Two guards, both learned the hard way. Remote models are never upgrade candidates —
    the oracle is reached through escalation, which pays for it on the fraction of items
    that need it rather than on all of them, and routing every row off-box would give up
    the local-only property for an accuracy gain we can buy far more cheaply. And an
    upgrade may not blow past UPGRADE_COST_CEILING, because "more accurate" with no
    bound on price is not an optimization.
    """
    bs = [_Block(b, est, stats) for b in blocks(root)]
    movable = [b for b in bs if b.movable]
    if not movable: return root

    target = movable[0]
    filt = target.anchor
    if not isinstance(filt, SemanticFilter): return root

    models = [m for m in est.eligible_models(filt.predicate_class) if not m.is_remote]
    current = next((i for i, m in enumerate(models) if m.name == filt.bound_model), None)
    if current is None or current + 1 >= len(models): return root

    nxt = models[current + 1]
    cur = models[current]
    if nxt.accuracy - cur.accuracy < delta: return root
    if nxt.seconds_per_item > cur.seconds_per_item * UPGRADE_COST_CEILING: return root

    def f(n: PlanNode) -> PlanNode:
        if n.node_id != filt.node_id: return n
        return replace(n, bound_model=nxt.name,
                       binding_reason=(f'upgraded from {cur.name} (+{nxt.accuracy - cur.accuracy:.2f} '
                                       f'accuracy): first filter in the order, where a '
                                       f'false negative is unrecoverable'))
    return map_nodes(root, f)

# ---------------------------------------------------------------------------

def explain_order(root: PlanNode, est: StaticEstimator,
                  stats: CorpusStats = ACTIVE) -> str:
    """Why the blocks are in this order. Printed by EXPLAIN and shown on the node card."""
    lines = ['block        cost/rec   selectivity   cost/(1-s)   bound model']
    for b in (_Block(x, est, stats) for x in blocks(root)):
        if not b.movable: continue
        model = getattr(b.anchor, 'bound_model', '')
        lines.append(f'{b.id:<12} {b.cost:>8.2f}s   {b.selectivity:>9.3f}   '
                     f'{b.ratio:>10.1f}   {model}')
    return '\n'.join(lines)
