"""Per-stage execution telemetry — the funnel.

The estimator predicts one of these from a plan; the executor emits one from a run. Same
shape either way, distinguished only by the Provenance its stages carry, so predicted and
measured can be compared row by row.

Totals are derived, never stored: a Funnel is its stages and nothing else.

Nothing here renders — see plan_editing.py.
"""

from dataclasses import dataclass

from optimizer.plan import Provenance

_WEAKEST_FIRST = (Provenance.PLACEHOLDER, Provenance.EXTRAPOLATED, Provenance.MEASURED)

@dataclass(frozen=True)
class StageMetrics:
    """What one operator did to the records that reached it.

    The invariant that makes the table honest: `model_calls` tracks `rows_in`, not
    `rows_out`. Each stage issues calls for the records it *receives*. A funnel whose
    call counts follow its output counts is describing a system that knows the answer
    before it asks the question.
    """
    node_id: str
    rows_in: float
    rows_out: float
    model_calls: float
    seconds: float
    provenance: Provenance
    remote_calls: float = 0.0
    """Carried separately from model_calls so the local-only claim stays measurable."""
    cache_hits: int = 0
    degraded: int = 0
    """Records that reached this stage with a degraded result from an earlier failure."""

@dataclass(frozen=True)
class Funnel:
    """An ordered run of stages in execution order, leaf-first."""
    stages: tuple[StageMetrics, ...]

    @property
    def rows_in(self) -> float:
        return self.stages[0].rows_in if self.stages else 0.0

    @property
    def rows_out(self) -> float:
        return self.stages[-1].rows_out if self.stages else 0.0

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.stages)

    @property
    def model_calls(self) -> float:
        return sum(s.model_calls for s in self.stages)

    @property
    def remote_calls(self) -> float:
        return sum(s.remote_calls for s in self.stages)

    @property
    def degraded(self) -> int:
        return sum(s.degraded for s in self.stages)

    @property
    def provenance(self) -> Provenance:
        """The weakest evidence backing any stage. One placeholder makes the whole
        funnel a placeholder — a total is only as trustworthy as its worst term."""
        if not self.stages: return Provenance.PLACEHOLDER
        return min((s.provenance for s in self.stages), key=_WEAKEST_FIRST.index)

    def stage(self, node_id: str) -> StageMetrics | None:
        return next((s for s in self.stages if s.node_id == node_id), None)
