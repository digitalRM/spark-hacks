"""Stage 5 -- execute an ExecutionPlan against the corpus. Not built yet.

`api.driver` imports this lazily and reports the execute stage as `stub` for as long as
`execute` raises NotImplementedError, so the rest of the pipeline stays runnable. Filling
it in means dispatching every plan node through one ExecutionContext protocol with three
backend families behind it -- relational (compile the node to SQL), bespoke (rasterize,
segment, vector search, blocking) and semantic (invoke a model) -- and recording items in,
items out, wall clock, model calls and cache hits per stage.

The driver needs exactly one name from this module:

    execute(plan) -> dict   the results and per-stage telemetry, JSON-ready
"""
from __future__ import annotations

from typing import Any

# Read by api.driver.seam_status. Delete this line when `execute` actually executes.
STUB = True

def execute(plan: Any) -> dict[str, Any]:
    """Run an ExecutionPlan and return results plus per-stage telemetry.

    Cost: the whole query. Every other stage in the pipeline is planning; this is the one
    that spends the wall clock the estimator has been predicting.
    """
    raise NotImplementedError(
        'runtime.executor.execute: the executor is not built yet -- see the module docstring'
    )
