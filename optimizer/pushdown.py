"""Pass 2 — push deterministic predicates into the storage engine.

Every EXACT predicate that lowering left as its own operator gets absorbed into the
Scan's SQL, so the index evaluates it instead of us. Zero model calls either way; the
difference is that SQLite filters 32,000 rows in microseconds and does it *before* the
rows reach anything expensive.

In compiler terms this is instruction selection: the same computation, emitted as one
native operation instead of a sequence of interpreted ones.

Semantics-preserving. The result set is identical; only the work is smaller.

Cost: one walk down the tree. Optionally one COUNT per Scan if a prober is supplied —
which is worth it, because EXACT selectivity is the one quantity in the whole cost model
we can *measure* for milliseconds instead of guessing at.
"""

from dataclasses import replace
from typing import Callable

from optimizer.plan import (
    Aggregate, Collapse, ExactFilter, Expand, PlanNode, PlanWarning, Scan,
    SelectivitySource, Union,
)
from optimizer.plan_editing import with_children

type Prober = Callable[[str, tuple], float]
"""Runs `SELECT COUNT(*)` for a statement and returns the row count. Supplying one turns
the Scan's selectivity from a prior into a measurement."""

def push_down(root: PlanNode, probe: Prober | None = None,
              base_rows: Callable[[str], float] | None = None
              ) -> tuple[PlanNode, list[PlanWarning]]:
    """Absorb pushable EXACT predicates into their Scan. Returns the rewritten plan and
    any warnings about predicates that could not be pushed."""
    warnings: list[PlanWarning] = []
    out = _push(root, (), 0, warnings, probe, base_rows)
    return out, warnings

def _push(n: PlanNode, carry: tuple[ExactFilter, ...], depth: int,
          warnings: list[PlanWarning], probe: Prober | None,
          base_rows: Callable[[str], float] | None) -> PlanNode:
    """Descend, collecting pushable EXACT filters, and absorb them at the Scan.

    `depth` counts how many expansions we are inside. Descending, a Collapse enters one
    and an Expand leaves it. A predicate at depth > 0 ranges over elements — pages, audio
    segments — which do not exist as rows in the index, so it cannot be pushed.
    """
    match n:
        case Scan():
            return _absorb(n, carry, probe, base_rows)

        case ExactFilter() if depth == 0:
            # Safe to sink past anything below it: a deterministic predicate over base-
            # grain fields never reads what a semantic operator produced.
            return _push(n.child, (*carry, n), depth, warnings, probe, base_rows)

        case ExactFilter():
            warnings.append(PlanWarning(
                'unpushable_predicate',
                f'{n.predicate} ranges over expanded elements, which are not rows in the '
                'index; it stays an operator and runs per element',
                n.node_id))
            return replace(n, child=_push(n.child, carry, depth, warnings, probe, base_rows))

        case Union(children=cs):
            # Each branch gets its own copy of the carried predicates. Correct, and the
            # duplicated scan is what the memo cache exists to make free.
            return replace(n, children=tuple(
                _push(c, carry, depth, warnings, probe, base_rows) for c in cs))

        case Aggregate():
            # Nothing crosses a blocking aggregate: below it the grouped columns do not
            # exist yet, above it the ungrouped ones no longer do.
            if carry:
                warnings.append(PlanWarning(
                    'blocked_by_aggregate',
                    f'{len(carry)} predicate(s) cannot be pushed past an aggregate',
                    n.node_id))
            rebuilt = _push(n.child, (), depth, warnings, probe, base_rows)
            return _restack(replace(n, child=rebuilt), carry)

        case Collapse():
            return replace(n, child=_push(n.child, carry, depth + 1, warnings, probe,
                                          base_rows))

        case Expand():
            # Clamped: a query that projects its expanded element gets no Collapse, so
            # Expands are not always balanced and the count would run negative — leaving
            # base-grain predicates below it looking unpushable when they are not.
            return replace(n, child=_push(n.child, carry, max(0, depth - 1), warnings,
                                          probe, base_rows))

        case _:
            return with_children(n, tuple(
                _push(c, carry, depth, warnings, probe, base_rows) for c in n_children(n)))

def n_children(n: PlanNode) -> tuple[PlanNode, ...]:
    from optimizer.plan import children
    return children(n)

def _restack(base: PlanNode, carry: tuple[ExactFilter, ...]) -> PlanNode:
    """Put predicates back as operators when they could not be pushed."""
    for f in reversed(carry):
        base = replace(f, child=base)
    return base

def _absorb(scan: Scan, carry: tuple[ExactFilter, ...], probe: Prober | None,
            base_rows: Callable[[str], float] | None) -> Scan:
    """Splice the carried predicates into the Scan as one WHERE clause."""
    if not carry:
        return scan

    # Carried top-down, so the deepest operator — the one the query wrote first — is last.
    carry = tuple(reversed(carry))
    clauses = [f.sql for f in carry]
    params = tuple(p for f in carry for p in f.params)
    sql = (f'{scan.sql} AND {" AND ".join(clauses)}' if ' WHERE ' in scan.sql
           else f'{scan.sql} WHERE {" AND ".join(clauses)}')

    selectivity = scan.selectivity
    for f in carry:
        selectivity *= f.selectivity
    source = SelectivitySource.STATIC

    if probe is not None and base_rows is not None:
        # The measurement the rest of the cost model cannot have. A COUNT against the
        # index costs milliseconds, so the deterministic half of the plan is never a
        # guess — which confines the weak numbers to exactly the stages already flagged.
        total = base_rows(scan.tables[0]) if scan.tables else 0.0
        if total > 0:
            selectivity = min(1.0, probe(sql, (*scan.params, *params)) / total)
            source = SelectivitySource.PROBED

    return replace(scan, sql=sql, params=(*scan.params, *params),
                   pushed=(*scan.pushed, *(f.predicate for f in carry)),
                   selectivity=selectivity, selectivity_source=source)
