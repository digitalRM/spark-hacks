"""Cardinality and cost estimation — the optimizer's only view of what execution costs.

Kept apart from ExecutionContext so the optimizer never imports the executor. The
optimizer depends on two protocols; StaticEstimator implements both from stats.py and
calibration.json, and the executor can later implement them from measured numbers with
nothing in the optimizer changing.

estimate() emits a Funnel — the same shape the executor emits from a real run — so
predicted and measured render row by row against each other.

Assumptions, stated because this is where the model is weakest:
  * Predicates are independent, so selectivities multiply. False in the real world (a
    case citing Graham is likelier to be a §1983 case) and the first thing to fix if the
    estimates embarrass us.
  * Equality joins along foreign keys are key-preserving: they filter, never multiply.
  * Unit cost is 1/throughput, not token-proportional. TODO: scale by avg_tokens once
    calibrate.py reports tokens/sec.

Cost: arithmetic over the tree, microseconds. No I/O after construction.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from query_language.ast import FieldRef
from optimizer.funnel import Funnel, StageMetrics
from optimizer.plan import (
    Aggregate, Collapse, ExactFilter, Expand, Limit, Materialize, PlanNode,
    PredicateClass, Project, Provenance, Retrieve, Scan, SemanticFilter, SemanticJoin,
    Union, grain,
)
from optimizer.stats import ACTIVE, CorpusStats, coverage, derivation, placeholders, rows

CALIBRATION_PATH = Path(__file__).resolve().parent.parent / 'data' / 'calibration.json'

# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSpec:
    """One row of calibration.json."""
    name: str
    predicate_classes: tuple[PredicateClass, ...]
    throughput_items_per_sec: float
    latency_p50_ms: float
    latency_p95_ms: float
    vram_gb: float
    accuracy: float
    is_remote: bool
    provenance: Provenance

    @property
    def seconds_per_item(self) -> float:
        return 1.0 / self.throughput_items_per_sec if self.throughput_items_per_sec else 1e9

class CardinalityModel(Protocol):
    """How many records flow where."""
    def base_rows(self, table: str) -> tuple[float, Provenance]: ...
    def prior_selectivity(self, cls: PredicateClass) -> float: ...

class CostModel(Protocol):
    """What one evaluation costs, and which models may perform it."""
    def spec(self, model: str) -> ModelSpec | None: ...
    def eligible_models(self, cls: PredicateClass) -> tuple[ModelSpec, ...]: ...
    def unit_cost_s(self, model: str) -> float: ...

class StaticEstimator(CardinalityModel, CostModel):
    """Estimates from corpus stats and calibrated throughput. No probing."""

    def __init__(self, stats: CorpusStats = ACTIVE,
                 calibration_path: Path = CALIBRATION_PATH) -> None:
        self.stats = stats
        raw = json.loads(calibration_path.read_text())
        priors = raw.pop('_priors', {})
        raw.pop('_README', None)
        self.models: dict[str, ModelSpec] = {
            name: ModelSpec(
                name=name,
                predicate_classes=tuple(PredicateClass(c.lower())
                                        for c in d['predicate_classes']),
                throughput_items_per_sec=d['throughput_items_per_sec'],
                latency_p50_ms=d['latency_p50_ms'], latency_p95_ms=d['latency_p95_ms'],
                vram_gb=d['vram_gb'], accuracy=d['accuracy_on_labeled_set'],
                is_remote=d['is_remote'],
                provenance=Provenance(d['measurement_status'].lower()))
            for name, d in raw.items()}
        self._sel = {PredicateClass(k.lower()): v
                     for k, v in priors.get('selectivity_by_class', {}).items()}
        self.sql_seconds_per_1k = priors.get('sql_seconds_per_1k_rows', 0.0001)
        self.bespoke_seconds_per_item = priors.get('bespoke_seconds_per_item', 0.001)

    def base_rows(self, table: str) -> tuple[float, Provenance]:
        return rows(self.stats, table)

    def prior_selectivity(self, cls: PredicateClass) -> float:
        return self._sel.get(cls, 0.5)

    def spec(self, model: str) -> ModelSpec | None:
        return self.models.get(model)

    def eligible_models(self, cls: PredicateClass) -> tuple[ModelSpec, ...]:
        """Models that can serve this class, cheapest first — the order model binding
        walks to take the cheapest one clearing the accuracy floor."""
        return tuple(sorted((m for m in self.models.values() if cls in m.predicate_classes),
                            key=lambda m: m.seconds_per_item))

    def unit_cost_s(self, model: str) -> float:
        m = self.models.get(model)
        return m.seconds_per_item if m else 1e9

    def is_remote(self, model: str) -> bool:
        m = self.models.get(model)
        return bool(m and m.is_remote)

    def worst_provenance(self) -> Provenance:
        """The weakest evidence behind any number we would display. One placeholder and
        the whole funnel is a placeholder."""
        ps = {m.provenance for m in self.models.values()}
        if Provenance.PLACEHOLDER in ps or any(True for _ in placeholders(self.stats)):
            return Provenance.PLACEHOLDER
        if Provenance.EXTRAPOLATED in ps: return Provenance.EXTRAPOLATED
        return Provenance.MEASURED

# ---------------------------------------------------------------------------
# Funnel estimation
# ---------------------------------------------------------------------------

def expected_examined(fanout: float, s: float) -> float:
    """Elements examined per parent when scanning for a first match and stopping.

    With per-element pass probability s and independence, E = (1-(1-s)^n)/s, capped at n.
    At s=0.02 over 47 pages that is ~31 rather than 47 — the early-exit win, derived from
    the query's shape rather than annotated onto it."""
    if s <= 0.0: return fanout
    return min(fanout, (1.0 - (1.0 - s) ** fanout) / s)

@dataclass
class _Frame:
    """One Expand's worth of context, so the matching Collapse can estimate how many
    distinct parents kept at least one element."""
    parent_rows: float
    fanout: float
    survived: float = 1.0     # product of selectivities applied since the Expand

@dataclass
class _State:
    rows: float
    frames: list[_Frame] = field(default_factory=list)

def estimate(root: PlanNode, est: StaticEstimator,
             projects_element: bool | None = None) -> Funnel:
    """Predict the funnel for a plan without executing it.

    Stages come out leaf-first, in execution order. Union branches concatenate and their
    totals sum, which over-counts the union by its overlap — the memo cache makes that
    overlap free at execution time, but we do not model the cache here.
    """
    if projects_element is None:
        projects_element = any(c.unnest for n in _walk(root) if isinstance(n, Project)
                               for c in n.columns)
    stages: list[StageMetrics] = []
    _visit(root, est, stages, projects_element)
    return Funnel(tuple(stages))

def _walk(n: PlanNode):
    from optimizer.plan import walk
    return walk(n)

def _visit(n: PlanNode, est: StaticEstimator, stages: list[StageMetrics],
           projects_element: bool) -> _State:
    """Estimate n bottom-up, appending one StageMetrics per node in execution order."""
    prov = est.worst_provenance()
    st = _State(0.0)
    rows_in = secs = calls = remote = 0.0

    match n:
        case Scan(selectivity=s):
            # The driving table is the one the scan's grain names; foreign-key joins are
            # key-preserving, so taking the largest joined table would report the
            # citation table's 1.9M rows as input to a per-cluster pipeline.
            driver = grain(n)[0] if grain(n) else (n.tables[0] if n.tables else '')
            rows_in = est.base_rows(driver)[0] if driver else 0.0
            st = _State(rows_in * s)
            secs = rows_in / 1000.0 * est.sql_seconds_per_1k

        case Union(children=cs):
            subs = [_visit(c, est, stages, projects_element) for c in cs]
            rows_in = sum(x.rows for x in subs)
            st = _State(rows_in)   # dedup is not modelled; see the docstring
            secs = rows_in * est.bespoke_seconds_per_item

        case SemanticJoin(left=l, right=r, block_top_k=k, bound_model=model,
                          selectivity=s):
            ls = _visit(l, est, stages, projects_element)
            rs = _visit(r, est, stages, projects_element)
            rows_in = ls.rows
            calls = ls.rows * min(rs.rows, float(k))
            secs = calls * est.unit_cost_s(model)
            remote = calls if est.is_remote(model) else 0.0
            st = _State(calls * s)

        case _:
            child = _visit(n.child, est, stages, projects_element)
            rows_in = child.rows
            st = _State(child.rows, child.frames)

            match n:
                case ExactFilter(selectivity=s):
                    st.rows = rows_in * s
                    secs = rows_in * est.bespoke_seconds_per_item
                    _survive(st, s)

                case Retrieve(top_k=k, embed_model=em, rerank_model=rm):
                    # Corpus embeddings are precomputed at index time, so this costs one
                    # query embedding plus a rerank call per candidate. Charging an embed
                    # call per corpus row would make retrieval look more expensive than
                    # the LLM it exists to protect.
                    st.rows = min(rows_in, float(k))
                    calls = 1.0 + st.rows
                    secs = est.unit_cost_s(em) + st.rows * est.unit_cost_s(rm)
                    _survive(st, st.rows / rows_in if rows_in else 1.0)

                case Materialize(source=src, produces=prod, bound_model=model):
                    d = derivation(est.stats, prod)
                    per = d.seconds_per_source_unit if d else est.unit_cost_s(model)
                    cov = coverage(est.stats, src)
                    # A derivation produces rather than filters, except where the source
                    # is simply absent: a docket with no recording cannot satisfy an
                    # audio predicate, so coverage narrows before the expensive part.
                    calls = rows_in * cov
                    secs = calls * per
                    remote = calls if est.is_remote(model) else 0.0
                    st.rows = rows_in * cov
                    _survive(st, cov)

                case Expand(fanout=fan):
                    st.frames = [*child.frames, _Frame(rows_in, fan)]
                    st.rows = rows_in * fan
                    secs = st.rows * est.bespoke_seconds_per_item

                case SemanticFilter(selectivity=s, bound_model=model,
                                    escalation_fraction=esc, early_exit=early,
                                    predicate_class=cls):
                    if early and st.frames and not projects_element:
                        fr = st.frames[-1]
                        calls = fr.parent_rows * expected_examined(fr.fanout, s)
                    else:
                        calls = rows_in
                    st.rows = rows_in * s
                    secs = calls * est.unit_cost_s(model)
                    if est.is_remote(model): remote += calls
                    if esc:
                        # Cascade: the bottom fraction by confidence rank re-runs on the
                        # oracle, so those items are paid for twice.
                        oracle = next((m for m in est.eligible_models(cls) if m.is_remote),
                                      None)
                        if oracle is not None:
                            extra = calls * esc
                            secs += extra * oracle.seconds_per_item
                            calls += extra
                            remote += extra
                    _survive(st, s)

                case Collapse():
                    if st.frames:
                        fr = st.frames[-1]
                        st.frames = st.frames[:-1]
                        # Distinct parents keeping at least one element, under independence.
                        st.rows = fr.parent_rows * (1.0 - (1.0 - fr.survived) ** fr.fanout)
                    secs = rows_in * est.bespoke_seconds_per_item

                case Aggregate(group_by=gb):
                    st.rows = rows_in if not gb else max(rows_in * 0.2, 1.0)
                    secs = rows_in * est.bespoke_seconds_per_item

                case Limit(k=k):
                    st.rows = min(rows_in, float(k))

                case Project():
                    pass

                case _: raise TypeError(f'unknown plan node: {n!r}')

    stages.append(StageMetrics(node_id=n.node_id, rows_in=rows_in, rows_out=st.rows,
                               model_calls=calls, seconds=secs, provenance=prov,
                               remote_calls=remote))
    return st

def _survive(st: _State, s: float) -> None:
    """Record a selectivity against the innermost open Expand, so its Collapse can
    estimate how many parents kept an element."""
    if st.frames:
        st.frames = [*st.frames[:-1],
                     _Frame(st.frames[-1].parent_rows, st.frames[-1].fanout,
                            st.frames[-1].survived * s)]
