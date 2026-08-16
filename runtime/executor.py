"""Stage 5 -- execute an ExecutionPlan against the corpus.

Fills the `runtime.executor.execute(plan) -> dict` seam that api.driver imports. The
plan is the operator tree from optimizer/plan.py (as JSON or as PlanNode objects); this
walks it bottom-up -- leaves read, the root emits -- through three backend families the
original stub named:

    relational  Scan / ExactFilter         SQL + Python over the local index; zero model calls
    bespoke     Expand / Collapse / Limit  grain and cardinality moves; zero model calls
    semantic    SemanticFilter             one model call per record, to the plan's bound model

The plan says WHICH model each semantic node uses (bound_model, a calibration key) and
WHAT data it reads (field, a FieldRef path). This module resolves the first to a real
endpoint (model_endpoints.resolve) and the second against the record, applies the model's
hard context limit, records a funnel row per node, and renders each surviving record with
citable evidence links.

Coverage is honest and partial: Scan, ExactFilter, SemanticFilter, Expand, Collapse, Limit
and Project run; Retrieve, Materialize, Aggregate, Union and SemanticJoin raise a precise
NotImplementedError naming what is missing, so a plan that needs them fails loudly instead
of lying. That is enough to run the Scan -> SEM -> Limit -> Project path end to end.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (Path(__file__).resolve().parent, _REPO_ROOT, _REPO_ROOT / "data_ingestion"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from data_ingestion.dataform.models import (  # noqa: E402
    AudioAsset, Citation, Document, Event, ImageAsset, Organization, Person, Proceeding,
)
from data_ingestion.dataform.store import Store  # noqa: E402
from optimizer.plan import (  # noqa: E402
    Aggregate, Collapse, Column, ExactFilter, Expand, Limit, Materialize, PlanNode,
    PredicateClass, Project, Retrieve, Scan, SemanticFilter, SemanticJoin, Union, grain, walk,
)
from optimizer.plan_editing import node_of  # noqa: E402  JSON -> PlanNode
from query_language.ast import FieldRef  # noqa: E402

import model_endpoints  # noqa: E402  sibling module

# The executor is real now; the driver's seam_status stops reporting `stub`.
STUB = False

# The store is one table per canonical entity; a dataform Scan names logical tables that
# map straight onto those pydantic models. Each store table has a `data` JSON blob plus a
# few indexed columns (id, source_system, source_id, doc_type, date).
_TABLE_MODEL: dict[str, Any] = {
    "document": Document, "proceeding": Proceeding, "organization": Organization,
    "person": Person, "citation": Citation, "event": Event,
}
_INDEXED_COLS = {"doc_type", "source_system", "source_id"}

# The optimizer leaves the dataform semantic nodes UNBOUND (its model-binding pass is
# calibrated for the courtlistener schema). Bind them to the local models by predicate
# class so real execution has something to call.
_DEFAULT_MODEL = {
    "SEM": "UNVERIFIED-sem-lightning-local",
    "AUDIO": "UNVERIFIED-sem-lightning-local",
    "VISUAL": "UNVERIFIED-vlm-nano-local",
}


def _endpoint_for(node: SemanticFilter):
    key = node.bound_model
    if key == "UNBOUND" or key not in model_endpoints.REGISTRY:
        key = _DEFAULT_MODEL[node.predicate_class.value.upper()]
    return model_endpoints.resolve(key)


# ---------------------------------------------------------------------------
# Records flowing through the tree
# ---------------------------------------------------------------------------

_ENVELOPE_FIELDS = {"id", "source_system", "source_id", "source_url",
                    "ingested_at", "effective_date", "version", "external_ids"}


@dataclass
class Row:
    """One record at some grain. `doc` is the base entity; `element` is the currently
    unnested sub-value (a page/segment) after an Expand, or None at base grain."""
    doc: Document
    element: Any = None
    evidence: list[dict] = field(default_factory=list)

    def value(self, ref: FieldRef) -> Any:
        # An unnested field resolves to the current element; everything else walks the doc.
        if self.element is not None and ref.path and ref.path[-1] in ("scan_pages", "segments",
                                                                       "images", "audio"):
            return self.element
        return _walk(self.doc, ref.path)


def _walk(obj: Any, path: tuple[str, ...]) -> Any:
    if not path:
        return obj
    head, *rest = path
    # schema.py flattens RecordEnvelope fields onto each table; the pydantic object nests them.
    if head in _ENVELOPE_FIELDS and hasattr(obj, "envelope"):
        cur = getattr(obj.envelope, head, None)
    else:
        cur = getattr(obj, head, None)
    for key in rest:
        if cur is None:
            return None
        cur = getattr(cur, key, None)
    return cur


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@dataclass
class _Stage:
    node_id: str
    rows_in: float
    rows_out: float
    model_calls: float = 0.0
    remote_calls: float = 0.0
    seconds: float = 0.0
    cache_hits: int = 0
    degraded: int = 0


@dataclass
class _Ctx:
    store: Store
    stages: list[_Stage] = field(default_factory=list)
    memo: dict[tuple, tuple[bool, float, str]] = field(default_factory=dict)  # (model,text,content)->verdict
    scan_queries: dict[str, tuple[str, tuple]] = field(default_factory=dict)  # scan node_id -> (sql, params)

    def record(self, s: _Stage) -> None:
        self.stages.append(s)


# ---------------------------------------------------------------------------
# Semantic backend: one model call per record, to the plan's bound model
# ---------------------------------------------------------------------------

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_JSON = re.compile(r"\{.*\}", re.DOTALL)

_JUDGE_SYSTEM = (
    "You are a strict binary classifier for a legal search system. Given a PREDICATE and "
    "CONTENT, decide whether the content satisfies the predicate. Respond with ONLY a JSON "
    'object: {"match": true|false, "confidence": 0.0-1.0, "rationale": "<one sentence>"}.'
)


def _parse_verdict(raw: str) -> tuple[bool, float, str]:
    m = _JSON.search(_THINK.sub("", raw).strip())
    if not m:
        return False, 0.0, "unparseable"
    try:
        d = json.loads(m.group(0))
        return bool(d.get("match", False)), float(d.get("confidence", 0.0)), str(d.get("rationale", ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, 0.0, "malformed"


def _extract_content(row: Row, node: SemanticFilter) -> tuple[str, str | None, dict]:
    """Return (text_content, image_ref, evidence_stub) for the field this node reads."""
    val = row.value(node.field)
    if isinstance(val, ImageAsset):
        loc = f"page {val.page_number}" if val.page_number else "exhibit"
        return "", val.image_ref, {"kind": "image", "label": loc, "ref": val.image_ref}
    if isinstance(val, AudioAsset):
        text = val.transcript or " ".join(s.get("text", "") for s in val.timestamp_index if s.get("text"))
        return text, None, {"kind": "audio", "label": "oral argument", "ref": val.audio_ref}
    if isinstance(val, str):
        label = node.field.path[-1] if node.field.path else "text"
        return val, None, {"kind": "text", "label": label, "ref": None}
    return "", None, {"kind": "text", "label": "text", "ref": None}


async def _judge(ep, content: str, image_ref: str | None, predicate: str, client) -> tuple[bool, float, str]:
    if image_ref is not None:
        messages = [{"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": f"PREDICATE: {predicate}"},
                        {"type": "image_url", "image_url": {"url": image_ref}}]}]
    else:
        content = content[: ep.max_input_chars]  # the hard context limit, enforced here
        messages = [{"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": f"PREDICATE: {predicate}\n\nCONTENT:\n{content}"}]
    resp = await client.chat.completions.create(model=ep.model_id, messages=messages, temperature=0)
    return _parse_verdict(resp.choices[0].message.content or "")


async def _run_semantic(node: SemanticFilter, rows: list[Row], ctx: _Ctx) -> list[Row]:
    from openai import AsyncOpenAI
    ep = _endpoint_for(node)  # resolves UNBOUND -> a default local model by predicate class
    if node.predicate_class.value.upper() not in ep.serves:
        raise ValueError(f"{node.node_id}: {ep.calibration_key} cannot serve "
                         f"{node.predicate_class.value.upper()}")
    client = AsyncOpenAI(base_url=ep.base_url, api_key="not-needed-locally")
    sem = asyncio.Semaphore(4 if not ep.is_remote else 2)
    stage = _Stage(node.node_id, rows_in=len(rows), rows_out=0)
    t0 = time.perf_counter()

    async def judge_row(row: Row) -> Row | None:
        content, image_ref, ev = _extract_content(row, node)
        if not content and image_ref is None:
            return None
        key = (ep.model_id, node.text, content[:512], image_ref)
        if key in ctx.memo:
            match, conf, why = ctx.memo[key]
            stage.cache_hits += 1
        else:
            async with sem:
                match, conf, why = await _judge(ep, content, image_ref, node.text, client)
            stage.model_calls += 1
            if ep.is_remote:
                stage.remote_calls += 1
            ctx.memo[key] = (match, conf, why)
        if node.negated:
            match = not match
        if match:
            ev = {**ev, "quote": why, "confidence": conf}
            return Row(row.doc, row.element, row.evidence + [ev])
        return None

    judged = await asyncio.gather(*(judge_row(r) for r in rows))
    survivors = [r for r in judged if r is not None]
    stage.rows_out = len(survivors)
    stage.seconds = time.perf_counter() - t0
    ctx.record(stage)
    return survivors


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------

# A downstream semantic stage costs a model call per row, so bound the candidates a Scan
# loads. Tight when a semantic filter follows (each row may cost ~20s on a reasoning model),
# generous for a pure-metadata query.
_SCAN_CAP_SEMANTIC = 15
_SCAN_CAP_PLAIN = 200


def _plan_scan_query(root: PlanNode, scan: Scan) -> tuple[str, tuple]:
    """Turn a dataform Scan into a real SQL query over the JSON-blob store.

    The optimizer emits `node.sql` as a relational FROM-fragment (e.g. `document AS doc`)
    that assumes real columns; the store keeps each record as a `data` JSON blob. So we
    ignore that fragment and build our own SELECT, pushing down what makes execution both
    fast and honest: indexed-column EXACT filters, and NOT-NULL on the field a downstream
    SEM will read (no point scanning rows that lack it). Single-table shape only; joins are
    not handled here yet."""
    table = scan.tables[0]
    if table not in _TABLE_MODEL:
        raise NotImplementedError(f"{scan.node_id}: no store mapping for table {table!r}")

    where: list[str] = []
    params: list[Any] = []
    has_semantic = False

    def push(sql: str, ps: tuple) -> None:
        """Re-apply one deterministic predicate as a store-native WHERE clause.

        Uses the parameterized relational form rather than reparsing the display-only BQL
        predicate, so strings, numbers and Date literals all arrive already unwrapped."""
        m = _SQL_CMP.match((sql or "").strip())
        if (m and m["op"] == "=" and len(ps) == 1
                and m["lhs"].split(".")[-1] in _INDEXED_COLS):
            where.append(f'{m["lhs"].split(".")[-1]} = ?')
            params.append(ps[0])

    # Predicates the optimizer already absorbed into the Scan. This list is the only
    # surviving record of them: pushdown *deletes* the ExactFilter nodes, and this backend
    # ignores scan.sql because the store keeps records as JSON blobs rather than columns.
    # Reading only the tree below would mean an optimized plan pushes nothing at all.
    for pp in scan.pushed:
        push(pp.sql, pp.params)

    for n in walk(root):
        if isinstance(n, ExactFilter):
            # Predicates pushdown could not absorb — over expanded elements, or above a
            # blocking aggregate — still standing as operators.
            push(n.sql, n.params)
        elif isinstance(n, SemanticFilter):
            has_semantic = True
            if n.predicate_class == PredicateClass.SEM and n.field.path:
                jp = "$." + ".".join(n.field.path)
                where.append(f"json_extract(data, '{jp}') IS NOT NULL "
                             f"AND length(json_extract(data, '{jp}')) > 0")

    sql = f"SELECT data FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" LIMIT {_SCAN_CAP_SEMANTIC if has_semantic else _SCAN_CAP_PLAIN}"
    return sql, tuple(params)


def _scan(node: Scan, ctx: _Ctx) -> list[Row]:
    """Load rows from the JSON-blob store and revive each `data` blob into its pydantic model.

    Uses the SQL computed by _plan_scan_query (stashed on ctx), not the optimizer's
    relational fragment."""
    t0 = time.perf_counter()
    model = _TABLE_MODEL.get(node.tables[0], Document)
    sql, params = ctx.scan_queries.get(node.node_id, (f"SELECT data FROM {node.tables[0]} LIMIT 200", ()))
    cur = ctx.store.conn.execute(sql, params)
    rows = [Row(model.model_validate_json(r["data"])) for r in cur.fetchall()]
    ctx.record(_Stage(node.node_id, rows_in=len(rows), rows_out=len(rows),
                      seconds=time.perf_counter() - t0))
    return rows


# ExactFilter carries two renderings of the same predicate: `predicate` is printed BQL for
# the node card -- `doc.date_issued >= date "2020-01-01"` -- and `sql`/`params` is the
# parameterised form pushdown splices into the Scan. This path evaluates the SQL form. It
# is the one without quoting to undo: the value arrives already unwrapped in `params`, so
# a Date literal, a string and a number all look the same here, and no display-only tag
# (`date`, quotes) has to be parsed back off.
_SQL_CMP = re.compile(r'^(?P<lhs>[\w.]+)\s*(?P<op>>=|<=|!=|=|>|<)\s*\?$')
_SQL_BETWEEN = re.compile(r'^(?P<lhs>[\w.]+)\s+BETWEEN\s+\?\s+AND\s+\?$', re.I)
_SQL_IN = re.compile(r'^(?P<lhs>[\w.]+)\s+IN\s*\([?,\s]+\)$', re.I)
_SQL_LIKE = re.compile(r'^(?P<lhs>[\w.]+)\s+LIKE\s+\?$', re.I)


def _compare(value: Any, param: Any) -> tuple[Any, Any]:
    """One comparable pair, the way SQLite would read the column's affinity: a numeric
    column numerically, everything else -- ISO dates included -- as text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return value, type(value)(param)
        except (TypeError, ValueError):
            pass
    return str(value), str(param)


def _predicate(node: ExactFilter):
    """A row test for this node's SQL fragment, or None when the shape is not handled."""
    sql = (node.sql or '').strip()
    params = list(node.params)

    def field(lhs: str):
        path = tuple(lhs.split('.')[1:])          # drop the source alias
        def read(r: Row) -> Any:
            v = _walk(r.doc, path)
            return getattr(v, 'value', v)
        return read

    if (m := _SQL_CMP.match(sql)) and len(params) == 1:
        read, op, (want,) = field(m['lhs']), m['op'], params
        ops = {'=': lambda a, b: a == b, '!=': lambda a, b: a != b,
               '>=': lambda a, b: a >= b, '<=': lambda a, b: a <= b,
               '>': lambda a, b: a > b, '<': lambda a, b: a < b}
        return lambda r: (v := read(r)) is not None and ops[op](*_compare(v, want))

    if (m := _SQL_BETWEEN.match(sql)) and len(params) == 2:
        read, (low, high) = field(m['lhs']), params
        def between(r: Row) -> bool:
            v = read(r)
            if v is None: return False
            lo_v, lo = _compare(v, low)
            hi_v, hi = _compare(v, high)
            return lo <= lo_v and hi_v <= hi
        return between

    if (m := _SQL_IN.match(sql)) and params:
        read = field(m['lhs'])
        def in_list(r: Row) -> bool:
            v = read(r)
            return v is not None and any(a == b for a, b in
                                         (_compare(v, want) for want in params))
        return in_list

    if (m := _SQL_LIKE.match(sql)) and len(params) == 1:
        read, pattern = field(m['lhs']), str(params[0])
        regex = re.compile('^' + '.*'.join(re.escape(part) for part in pattern.split('%')) + '$',
                           re.S)
        return lambda r: (v := read(r)) is not None and bool(regex.match(str(v)))

    return None


def _exact_filter(node: ExactFilter, rows: list[Row], ctx: _Ctx) -> list[Row]:
    """Final plans push EXACT into the Scan, so this only fires on pre-pushdown plans.
    An unhandled shape passes through, flagged degraded rather than silently dropped."""
    keep = _predicate(node)
    degraded = 0
    if keep is None:
        survivors, degraded = rows, len(rows)  # can't evaluate -> passthrough, but say so
    else:
        survivors = [r for r in rows if keep(r)]
    ctx.record(_Stage(node.node_id, rows_in=len(rows), rows_out=len(survivors), degraded=degraded))
    return survivors


def _expand(node: Expand, rows: list[Row], ctx: _Ctx) -> list[Row]:
    out: list[Row] = []
    for r in rows:
        coll = _walk(r.doc, node.field.path) or []
        for el in (coll if isinstance(coll, list) else [coll]):
            out.append(Row(r.doc, element=el, evidence=list(r.evidence)))
    ctx.record(_Stage(node.node_id, rows_in=len(rows), rows_out=len(out)))
    return out


def _collapse(node: Collapse, rows: list[Row], ctx: _Ctx) -> list[Row]:
    seen: set[str] = set()
    out: list[Row] = []
    for r in rows:
        key = r.doc.envelope.id
        if key in seen:
            continue
        seen.add(key)
        out.append(Row(r.doc, element=None, evidence=r.evidence))
    ctx.record(_Stage(node.node_id, rows_in=len(rows), rows_out=len(out)))
    return out


def _limit(node: Limit, rows: list[Row], ctx: _Ctx) -> list[Row]:
    out = rows[: node.k]
    ctx.record(_Stage(node.node_id, rows_in=len(rows), rows_out=len(out)))
    return out


async def _eval(node: PlanNode, ctx: _Ctx) -> list[Row]:
    match node:
        case Scan():
            return _scan(node, ctx)
        case ExactFilter():
            return _exact_filter(node, await _eval(node.child, ctx), ctx)
        case SemanticFilter():
            return await _run_semantic(node, await _eval(node.child, ctx), ctx)
        case Expand():
            return _expand(node, await _eval(node.child, ctx), ctx)
        case Collapse():
            return _collapse(node, await _eval(node.child, ctx), ctx)
        case Limit():
            return _limit(node, await _eval(node.child, ctx), ctx)
        case Project():
            return await _eval(node.child, ctx)  # projection shapes output, doesn't filter
        case Retrieve():
            raise NotImplementedError(
                f"{node.node_id}: Retrieve (embed+rerank on :8002/:8003) not wired yet")
        case Materialize():
            raise NotImplementedError(
                f"{node.node_id}: Materialize({node.method.value}) not wired yet "
                "(ASR/rasterize/parse derivation)")
        case Aggregate() | Union() | SemanticJoin():
            raise NotImplementedError(f"{node.node_id}: {type(node).__name__} not supported yet")
        case _:
            raise NotImplementedError(f"unknown plan node {type(node).__name__}")


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

def _primary_url(doc: Document) -> str | None:
    env = doc.envelope
    if env.source_url:
        return env.source_url
    cl = env.external_ids.get("courtlistener_cluster_id")
    if cl:
        return f"https://www.courtlistener.com/opinion/{cl}/x/"
    return doc.source_pdf_url


def _project(root: Project, rows: list[Row]) -> list[dict]:
    out = []
    for r in rows:
        cols = {}
        for c in root.columns:
            cols[c.label] = _walk(r.doc, c.ref.path) if c.ref else None
        out.append({
            "id": r.doc.envelope.id,
            "columns": cols,
            "title": r.doc.title or "(untitled)",
            "url": _primary_url(r.doc),
            "evidence": [_link(r.doc, e) for e in r.evidence],
        })
    return out


def _link(doc: Document, ev: dict) -> dict:
    kind, label, ref = ev.get("kind"), ev.get("label"), ev.get("ref")
    if kind == "image":
        base = doc.source_pdf_url or _primary_url(doc)
        url = f"{base}#page={label.split()[-1]}" if (base and label.startswith("page")) else (ref or base)
    elif kind == "audio":
        url = ref
    else:
        url = _primary_url(doc)
    return {"label": label, "url": url, "quote": ev.get("quote", ""), "confidence": ev.get("confidence")}


def _funnel_json(ctx: _Ctx) -> dict:
    stages = [{"node_id": s.node_id, "rows_in": s.rows_in, "rows_out": s.rows_out,
               "model_calls": s.model_calls, "seconds": round(s.seconds, 3),
               "provenance": "measured", "remote_calls": s.remote_calls,
               "cache_hits": s.cache_hits, "degraded": s.degraded} for s in ctx.stages]
    return {"provenance": "measured",
            "seconds": round(sum(s.seconds for s in ctx.stages), 3),
            "model_calls": sum(s.model_calls for s in ctx.stages),
            "remote_calls": sum(s.remote_calls for s in ctx.stages),
            "rows_in": ctx.stages[0].rows_in if ctx.stages else 0,
            "rows_out": ctx.stages[-1].rows_out if ctx.stages else 0,
            "stages": stages}


# ---------------------------------------------------------------------------
# The seam api.driver calls
# ---------------------------------------------------------------------------

def _as_plannode(plan: Any) -> PlanNode:
    """Accept a PlanNode, a node_json dict, a {'plan': ...} wrapper, or a
    {'snapshots': [...]} bundle (the last snapshot is the final optimized plan)."""
    if not isinstance(plan, dict):
        return plan  # already a PlanNode
    if "node" in plan:
        return node_of(plan)
    if "plan" in plan:
        return _as_plannode(plan["plan"])
    if plan.get("snapshots"):
        return node_of(plan["snapshots"][-1]["plan"])
    raise ValueError("execute: could not find a plan tree in the given dict")


def execute(plan: Any, *, db_path: Path | None = None, rows: list[Row] | None = None) -> dict[str, Any]:
    """Run an ExecutionPlan and return results plus per-stage telemetry, JSON-ready.

    `db_path` overrides the corpus location; `rows` injects a pre-loaded candidate set
    (used by the demo and tests to bypass Scan). The driver calls execute(plan) with
    neither, and the Scan node reads the configured corpus."""
    root = _as_plannode(plan)
    store = Store(db_path=db_path) if db_path else Store()
    ctx = _Ctx(store=store)

    # Precompute each Scan's real SQL (pushdowns need the whole pipeline, which a bottom-up
    # walk of a single node can't see).
    for n in walk(root):
        if isinstance(n, Scan) and n.tables and n.tables[0] in _TABLE_MODEL:
            ctx.scan_queries[n.node_id] = _plan_scan_query(root, n)

    async def go() -> list[Row]:
        if rows is not None:
            # Skip the leaf Scan, feed injected rows into the pipeline above it.
            from optimizer.plan import pipeline
            chain = pipeline(root)
            cur = rows
            for node in chain[1:]:  # chain[0] is the Scan we're replacing
                cur = await _step(node, cur, ctx)
            return cur
        return await _eval(root, ctx)

    try:
        survivors = asyncio.run(go())
    finally:
        store.close()

    project_root = root if isinstance(root, Project) else None
    results = _project(project_root, survivors) if project_root else [
        {"id": r.doc.envelope.id, "title": r.doc.title, "url": _primary_url(r.doc),
         "evidence": [_link(r.doc, e) for e in r.evidence]} for r in survivors]

    return {"total": len(results), "results": results, "funnel": _funnel_json(ctx),
            "provenance": "measured"}


async def _step(node: PlanNode, rows: list[Row], ctx: _Ctx) -> list[Row]:
    """Apply one single-input operator to an in-memory row set (for the injected-rows path)."""
    match node:
        case ExactFilter():    return _exact_filter(node, rows, ctx)
        case SemanticFilter(): return await _run_semantic(node, rows, ctx)
        case Expand():         return _expand(node, rows, ctx)
        case Collapse():       return _collapse(node, rows, ctx)
        case Limit():          return _limit(node, rows, ctx)
        case Project():        return rows
        case _:                raise NotImplementedError(f"{type(node).__name__} in injected-rows path")


if __name__ == "__main__":
    # Structural smoke test: decode the flagship fixture JSON and walk its shape without
    # touching a model (the fixture targets the cluster/docket schema, not the dataform db).
    from optimizer import fixtures
    from optimizer.plan_editing import node_json, render_plan
    root = fixtures.flagship()
    assert node_of(node_json(root)) == root, "JSON round-trip must be identity"
    print("flagship plan decodes and round-trips:\n")
    print(render_plan(root))
