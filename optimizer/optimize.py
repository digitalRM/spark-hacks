"""The optimizer: AST in, plan tree plus a snapshot per pass out.

    lower  ->  push down  ->  cost and order  ->  finalize

Deterministic. The same AST, calibration and stats produce a byte-identical plan, which
is the difference between a cost-based optimizer and a prompt with extra steps. No pass
calls a model.

Snapshots accumulate as the passes run rather than being reconstructed afterward —
watching a plan collapse rule by rule is the clearest evidence that a real optimizer
exists rather than a fixed order being printed.
"""

from dataclasses import dataclass

from query_language.type_system import Schema
from query_language.ast import Query
from optimizer.estimator import StaticEstimator
from optimizer.finalize import ESCALATION_FRACTION, finalize
from optimizer.lower import Binder, lower, unbound
from optimizer.order import ACCURACY_FLOOR, UPGRADE_ACCURACY_DELTA, cost_and_order
from optimizer.plan import PlanNode, PlanWarning, Snapshot
from optimizer.pushdown import Prober, push_down
from optimizer.stats import ACTIVE, CorpusStats

@dataclass(frozen=True)
class Optimized:
    """The plan, how it got there, and anything the optimizer wants said about it.

    Warnings ride alongside rather than inside the plan: a plan is a plan, and what we
    think of it is a separate question."""
    plan: PlanNode
    snapshots: tuple[Snapshot, ...]
    warnings: tuple[PlanWarning, ...]

    @property
    def blocked(self) -> bool:
        """True when the optimizer refuses to auto-execute and wants a human decision."""
        return any(w.blocking for w in self.warnings)

def optimize(q: Query, schema: Schema, est: StaticEstimator | None = None,
             stats: CorpusStats = ACTIVE,
             probe: Prober | None = None,
             base_rows=None,
             accuracy_floor: float = ACCURACY_FLOOR,
             upgrade_delta: float = UPGRADE_ACCURACY_DELTA,
             escalation_fraction: float = ESCALATION_FRACTION,
             naive_bind: Binder = unbound) -> Optimized:
    """Compile a typechecked query into an execution plan.

    `probe` turns the Scan's selectivity from a prior into a COUNT against the index,
    which is worth far more than it looks: it is the only quantity in the cost model that
    can be measured rather than estimated, and everything downstream is sized by it.
    """
    est = est or StaticEstimator(stats)
    snaps: list[Snapshot] = []
    warnings: list[PlanWarning] = []

    p = lower(q, schema, stats, naive_bind)
    snaps.append(Snapshot(
        'lower',
        'AST to operator tree. Derived fields become Materialize, unnest becomes '
        'Expand/Collapse, FUZZY resolves by field type. Every predicate is its own '
        'operator in the order the query wrote it, and nothing is optimized — bound to '
        'the heaviest eligible model this is the naive baseline.', p))

    p, w = push_down(p, probe, base_rows)
    warnings += w
    snaps.append(Snapshot(
        'push down',
        'Every EXACT predicate and foreign-key join absorbed into one SQL statement, so '
        'the index evaluates them before anything expensive sees a row. '
        'Semantics-preserving: the same rows, less work.', p))

    p, w = cost_and_order(p, est, accuracy_floor, upgrade_delta, stats)
    warnings += w
    snaps.append(Snapshot(
        'cost and order',
        'Estimate selectivity, bind each predicate to its cheapest model clearing the '
        'accuracy floor, then sort blocks by cost/(1-s) — cost per row eliminated. '
        'Ordering needs a price and a price needs a model, so binding happens here and '
        'is revisited once.', p))

    p, w = finalize(p, est, escalation_fraction, stats)
    warnings += w
    snaps.append(Snapshot(
        'finalize',
        'Early exit where nothing downstream can observe which element matched, and '
        'oracle escalation on the least-confident fraction of each local model\'s '
        'answers. Annotations only; nothing moves.', p))

    return Optimized(p, tuple(snaps), tuple(warnings))
