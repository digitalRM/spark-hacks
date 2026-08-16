"""End-to-end demo of the Execute step against the real dataform corpus.

Builds a real PlanNode tree in the optimizer's own operator vocabulary -- the same shape
optimizer.py will emit -- targeting the dataform `document` table, then runs it through
runtime.executor.execute(). Proves the executor consumes a real plan, resolves the bound
model to a live GN100 server, enforces the context limit, and renders linked evidence.

The flagship fixture (optimizer/fixtures.flagship) targets the cluster/docket relational
schema, which the ingested dataform db does not have -- so this uses a dataform-shaped plan
for the live run. Run `python -m runtime.executor` for the fixture's structural round-trip.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (_REPO_ROOT, _REPO_ROOT / "data_ingestion", Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from optimizer.plan import (  # noqa: E402
    Column, Limit, PredicateClass, Project, Scan, SelectivitySource, SemanticFilter,
)
from optimizer.plan_editing import node_json, render_plan  # noqa: E402
from query_language.ast import FieldRef  # noqa: E402
from executor import execute  # noqa: E402

DB_PATH = Path.home() / "amicus-dataform" / "data" / "dataform.db"

# Five real CourtListener documents that carry `summary` text -- a deterministic,
# content-rich Scan for the demo. In production the optimizer's Scan pushes the real
# WHERE clause; here we pin ids so the run is fast and repeatable.
SEED_IDS = [
    "d3d30f84-a973-5622-9030-80b025d31759", "8b45fe9d-7bde-557e-92af-ad6f803fabef",
    "a4fa1102-ae8a-5010-8f2b-15d48f4f0752", "ca7fd43c-db0d-5677-af68-92756914b958",
    "8335796c-1a34-5482-b3cc-0bf754f204a7",
]

LIGHTNING = "UNVERIFIED-sem-lightning-local"


def build_plan() -> Project:
    placeholders = ",".join("?" for _ in SEED_IDS)
    scan = Scan(
        node_id="n0", tables=("document",),
        sql=f"SELECT data FROM document WHERE id IN ({placeholders})",
        params=tuple(SEED_IDS), pushed=("document.source_system = \"courtlistener\"",),
        selectivity=0.0002, selectivity_source=SelectivitySource.PROBED, scan_grain=("document",),
    )
    sem = SemanticFilter(
        node_id="n1", predicate_class=PredicateClass.SEM,
        field=FieldRef("document", ("summary",)),
        text="the case is an appeal from a lower court's judgment or order",
        negated=False, bound_model=LIGHTNING,
        binding_reason="cheapest SEM model clearing the accuracy floor",
        selectivity=0.34, selectivity_source=SelectivitySource.STATIC,
        escalation_fraction=None, early_exit=False, child=scan,
    )
    limit = Limit(node_id="n2", k=10, early_exit=True, child=sem)
    return Project(node_id="n3", columns=(
        Column("cluster_id", FieldRef("document", ("id",))),
        Column("case_name", FieldRef("document", ("title",))),
    ), child=limit)


def main() -> None:
    plan = build_plan()
    print("=== EXECUTION PLAN (operator tree) ===")
    print(render_plan(plan))
    print()
    print("=== PLAN JSON (what the optimizer hands the executor) ===")
    print(json.dumps(node_json(plan), indent=2)[:900] + "\n  ...")
    print()

    result = execute(plan, db_path=DB_PATH)

    print("=== FUNNEL ===")
    for s in result["funnel"]["stages"]:
        print(f"  {s['node_id']:<5} {s['rows_in']:>4.0f} -> {s['rows_out']:<4.0f} "
              f"| {s['model_calls']:>3.0f} calls | {s['seconds']:>6.1f}s")
    print(f"  total: {result['total']} results | "
          f"{result['funnel']['model_calls']:.0f} calls | {result['funnel']['seconds']:.1f}s")
    print()
    print("=== RESULTS (with evidence links) ===")
    for r in result["results"]:
        print(f"  {r['title']}")
        print(f"    {r['url']}")
        for e in r["evidence"]:
            print(f"    - [{e['label']}]({e['url']}) (conf {e['confidence']}) {e['quote']}")


if __name__ == "__main__":
    main()
