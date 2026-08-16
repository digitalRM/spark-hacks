"""Pass 4 — finalize. Annotations only: nothing in the tree moves.

Two annotations that pull in opposite directions, and a self-check.

Early exit removes work by proving nobody can tell. Escalation adds work to buy back
accuracy the cheap models gave up. Both are single fields on existing nodes, which is
why they are one pass rather than three.

Cost: two walks of the tree. No I/O.
"""

from dataclasses import replace

from optimizer.estimator import StaticEstimator
from optimizer.plan import (
    Aggregate, Collapse, Column, Expand, Limit, PlanNode, PlanWarning, PredicateClass,
    Project, SemanticFilter, children, observed_fields, walk,
)
from optimizer.plan_editing import map_nodes, ref_str, validate
from optimizer.stats import ACTIVE, CorpusStats, path_str

ESCALATION_FRACTION = 0.10
"""Fraction of a node's calls re-run on the oracle, taken by confidence *rank* rather
than by an absolute threshold.

Logprobs are not calibrated — 0.7 from one model does not mean what 0.7 means from
another, and neither is a 70% chance of being right. A rank needs only an ordering, and
it makes the knob directly interpretable: this is the share of calls you are paying for
twice. The curve of agreement against this number is a measurable artifact."""

def finalize(root: PlanNode, est: StaticEstimator,
             escalation_fraction: float = ESCALATION_FRACTION,
             stats: CorpusStats = ACTIVE) -> tuple[PlanNode, list[PlanWarning]]:
    """Annotate early exit and escalation, then check the plan is executable."""
    p = _early_exit(root)
    p = _escalate(p, est, escalation_fraction)
    return p, _check(p, est, stats)

# ---------------------------------------------------------------------------
# Early exit
# ---------------------------------------------------------------------------

def _early_exit(root: PlanNode) -> PlanNode:
    """Mark every filter whose expanded element nobody downstream can observe.

    This is dead-binding analysis. Each element surviving a filter inside an expansion is
    a binding; if the only thing anyone above can learn is that the set was non-empty,
    then the first survivor answers the question and the rest of the scan is dead work.
    """
    observed = observed_fields(root)

    def visit(n: PlanNode, in_expansion: int, blocked: bool) -> PlanNode:
        """Descend carrying two facts: how many expansions we are inside, and whether an
        aggregate between here and the enclosing Collapse can see the elements."""
        match n:
            case Collapse():
                # Descending past a Collapse enters the expansion it closes.
                return replace(n, child=visit(n.child, in_expansion + 1, blocked))

            case Expand():
                return replace(n, child=visit(n.child, max(0, in_expansion - 1), blocked))

            case Aggregate():
                # An aggregate inside an expansion counts elements, so it observes which
                # ones matched — and below it nothing may exit early.
                return replace(n, child=visit(n.child, in_expansion,
                                              blocked or in_expansion > 0))

            case SemanticFilter(field=f):
                legal = in_expansion > 0 and not blocked and ref_str(f) not in observed
                return replace(n, early_exit=legal,
                               child=visit(n.child, in_expansion, blocked))

            case Limit():
                # Stopping the pipeline at k rows requires that nothing below needs to
                # see them all. An aggregate does, by definition.
                legal = not any(isinstance(m, Aggregate) for m in walk(n.child))
                return replace(n, early_exit=legal,
                               child=visit(n.child, in_expansion, blocked))

            case _:
                cs = children(n)
                if not cs: return n
                from optimizer.plan_editing import with_children
                return with_children(n, tuple(visit(c, in_expansion, blocked) for c in cs))

    return visit(root, 0, False)

# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def _escalate(root: PlanNode, est: StaticEstimator, fraction: float) -> PlanNode:
    """Attach oracle escalation where a cheap local model is doing the answering.

    A plan property, not executor magic: it is visible on the node card and editable,
    and turning it off is a legitimate thing for a user to want."""
    def f(n: PlanNode) -> PlanNode:
        if not isinstance(n, SemanticFilter): return n
        if n.predicate_class is PredicateClass.FUZZY: return n

        bound = est.spec(n.bound_model)
        if bound is None or bound.is_remote:
            # Nothing to escalate to: the oracle cannot adjudicate itself.
            return replace(n, escalation_fraction=None)

        oracle = max((m for m in est.eligible_models(n.predicate_class)
                      if m.is_remote and m.accuracy > bound.accuracy),
                     key=lambda m: m.accuracy, default=None)
        if oracle is None:
            return replace(n, escalation_fraction=None)
        return replace(n, escalation_fraction=fraction)
    return map_nodes(root, f)

# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _check(root: PlanNode, est: StaticEstimator,
           stats: CorpusStats) -> list[PlanWarning]:
    """Assert the plan is executable, reusing the editor's rules rather than a second set.

    If passes 1-3 are correct none of these fire. They exist so that a wrong plan fails
    here, naming the node and the rule, instead of failing inside the executor with a
    KeyError at three in the morning."""
    eligible = {name: m.predicate_classes for name, m in est.models.items()}
    derived = frozenset(path_str(k) for k in stats.derivations)
    return [PlanWarning('invalid_plan', f'{v.rule}: {v.message}', v.node_id, blocking=True)
            for v in validate(root, eligible=eligible, derived=derived)]
