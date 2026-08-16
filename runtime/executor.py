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
    TextAsset,
)
import os  # noqa: E402
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
    # Entities reached through the Scan's joins, keyed by BOTH alias ("org") and table
    # ("organization"), since plan nodes name fields either way. Resolved once per row by
    # _resolve_joins; empty for single-table plans.
    joined: dict[str, Any] = field(default_factory=dict)

    def entity(self, source: str | None) -> Any:
        """The record a field source refers to: a joined entity, None for a known-but-
        unresolved alias, else the base row."""
        if source and source in self.joined:
            return self.joined[source]
        if source and source in self.aliases:
            return None
        return self.doc

    def value(self, ref: FieldRef) -> Any:
        # An unnested field resolves to the current element; everything else walks the doc.
        if self.element is not None and ref.path and ref.path[-1] in ("scan_pages", "segments",
                                                                       "images", "audio"):
            return self.element
        src = getattr(ref, "source", None)
        if src and src in self.joined:
            return _walk(self.joined[src], ref.path)
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
    scan_queries: dict[str, list[tuple[str, tuple]]] = field(default_factory=dict)  # scan node_id -> tiers
    scan_base: dict[str, tuple[str, str]] = field(default_factory=dict)  # scan node_id -> (table, alias)
    scan_cap: dict[str, int] = field(default_factory=dict)  # scan node_id -> rows handed to the pipeline
    has_text: bool = False  # textdb.opinion_text is attached and readable
    plan_root: Any = None

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


# Fields that mean "the document's text". When the requested one is empty (full text was
# not ingested for most of the corpus) fall back to the best text we do have, and label the
# evidence with the field actually judged so the claim stays honest.
_TEXT_FIELDS = {("media", "text", "plain_text"), ("plain_text",), ("text",)}
_TEXT_FALLBACK = (("media", "text", "plain_text"), ("summary",), ("title",))


def _audio_text(ent: Any) -> tuple[str, Any] | None:
    """(transcript text, AudioAsset) for the first audio asset that has any text."""
    media = getattr(ent, "media", None)
    for a in (getattr(media, "audio", None) or []):
        text = a.transcript or " ".join(s.get("text", "") for s in (a.timestamp_index or []) if s.get("text"))
        if text.strip():
            return text, a
    return None


def _extract_content(row: Row, node: SemanticFilter) -> tuple[str, str | None, dict]:
    """Return (text_content, image_ref, evidence_stub) for the field this node reads."""
    val = row.value(node.field)
    if (val is None or val == "") and tuple(node.field.path) in _TEXT_FIELDS:
        ent = row.entity(getattr(node.field, "source", None))
        for alt in _TEXT_FALLBACK:
            if alt == ("title",):
                # a transcript beats a bare title: oral arguments carry their text in audio
                hit = _audio_text(ent) if ent is not None else None
                if hit:
                    text, asset = hit
                    return text, None, {"kind": "audio", "label": "oral argument", "ref": asset.audio_ref}
            v = _walk(ent, alt) if ent is not None else None
            if isinstance(v, str) and v.strip():
                return v, None, {"kind": "text", "label": alt[-1], "ref": None}
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


# Long texts are judged on excerpts centred on the predicate's keywords rather than in
# full: the verdict is about whether the text discusses X, and the passages that mention X
# are where that is decided. Cheaper (prompt tokens) and sharper (less dilution).
_EXCERPT_CHARS = 4000
_EXCERPT_WINDOW = 700


def _focus(content: str, predicate: str) -> str:
    if len(content) <= _EXCERPT_CHARS:
        return content
    low = content.lower()
    spans: list[tuple[int, int]] = []
    for kw in _keywords(predicate, limit=6):
        start = 0
        k = kw.lower()
        while len(spans) < 12:
            i = low.find(k, start)
            if i < 0:
                break
            spans.append((max(0, i - _EXCERPT_WINDOW // 2), min(len(content), i + _EXCERPT_WINDOW // 2)))
            start = i + len(k)
    if not spans:
        return content[:_EXCERPT_CHARS]
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, used = [], 0
    for a, b in merged:
        piece = content[a:b]
        if used + len(piece) > _EXCERPT_CHARS:
            piece = piece[: max(0, _EXCERPT_CHARS - used)]
        if piece:
            out.append(piece.strip())
            used += len(piece)
        if used >= _EXCERPT_CHARS:
            break
    return "\n…\n".join(out)


async def _judge(ep, content: str, image_ref: str | None, predicate: str, client) -> tuple[bool, float, str]:
    if image_ref is None:
        content = _focus(content, predicate)
    if image_ref is not None:
        messages = [{"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": f"PREDICATE: {predicate}"},
                        {"type": "image_url", "image_url": {"url": image_ref}}]}]
    else:
        content = content[: ep.max_input_chars]  # the hard context limit, enforced here
        messages = [{"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": f"PREDICATE: {predicate}\n\nCONTENT:\n{content}"}]
    # A verdict is 30 tokens of JSON. Lightning is a reasoning model and will otherwise spend
    # ~3k tokens (~100 s) thinking per row, so thinking is switched off and output capped.
    resp = await client.chat.completions.create(
        model=ep.model_id, messages=messages, temperature=0, max_tokens=200,
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}})
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
            return Row(row.doc, row.element, row.evidence + [ev], row.joined)
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


# The join shape the optimizer prints into Scan.sql: `<table> AS <alias> JOIN <table> AS
# <alias> ON <a.path> = <b.path> ...`. Parsed back here because the plan carries no AST.
_JOIN_RE = re.compile(r'JOIN\s+(?P<table>\w+)\s+AS\s+(?P<alias>\w+)\s+ON\s+'
                      r'(?P<l>[\w.]+)\s*=\s*(?P<r>[\w.]+)', re.I)
_BASE_RE = re.compile(r'^\s*(?P<table>\w+)\s+AS\s+(?P<alias>\w+)', re.I)

# Words that carry no signal for candidate retrieval (function words + the framing the
# compiler tends to add around a predicate).
_STOP = set("""a an the of in on at to from by for with and or nor but that this these those
which who whom whose where when what how was were is are be been being has have had do does
did not no any all some such than then there their they them its it as into onto over under
about above below between within without describes describe mentions mention discusses discuss
concerning regarding involving involves involve related relating includes include including
contains contain containing states state stating says said whether if while also very more
most less least other another each every either neither both few many much own same so too""".split())


def _keywords(text: str, limit: int = 5) -> list[str]:
    """Content words of a semantic predicate, proper nouns first, longest next."""
    seen: list[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'\-]{2,}", text):
        lw = w.lower()
        if lw in _STOP or lw in (x.lower() for x in seen):
            continue
        seen.append(w)
    proper = [w for w in seen if w[0].isupper() and text.find(w) > 0]  # not sentence-initial
    rest = sorted((w for w in seen if w not in proper), key=len, reverse=True)
    return (proper + rest)[:limit]


def _scan_joins(scan: Scan) -> tuple[str, str, list[tuple[str, tuple[str, ...], str, tuple[str, ...]]]]:
    """(base_table, base_alias, [(lhs_alias, lhs_path, rhs_alias, rhs_path), ...])."""
    sql = scan.sql or ""
    m = _BASE_RE.match(sql)
    base_table = m["table"] if m else scan.tables[0]
    base_alias = m["alias"] if m else base_table
    conds = []
    for j in _JOIN_RE.finditer(sql):
        la, *lp = j["l"].split(".")
        ra, *rp = j["r"].split(".")
        conds.append((la, tuple(lp), ra, tuple(rp)))
    return base_table, base_alias, conds


def _alias_tables(scan: Scan) -> dict[str, str]:
    """alias -> table for every table the Scan names."""
    out: dict[str, str] = {}
    sql = scan.sql or ""
    m = _BASE_RE.match(sql)
    if m: out[m["alias"]] = m["table"]
    for j in _JOIN_RE.finditer(sql):
        out[j["alias"]] = j["table"]
    for t in scan.tables:  # a table is also addressable by its own name
        out.setdefault(t, t)
    return out


def _prefilter_joined(root: PlanNode, scan: Scan, rows: list[Row],
                      base: tuple[str, str]) -> list[Row]:
    """Apply the plan's filters on JOINED tables to the retrieved pool, cheaply, before the
    cap: exact predicates exactly; semantic predicates on a small joined table (a court
    name) as a keyword pre-check -- every proper noun in the predicate must appear in the
    field, or any keyword if there are none. The semantic node still judges survivors."""
    base_table, base_alias = base
    base_names = {base_table, base_alias}
    keeps = []
    for n in walk(root):
        if isinstance(n, ExactFilter):
            m = _EXACT_RE.search(n.predicate)
            if not m:
                continue
            alias, *rest = m["lhs"].split(".")
            if alias in base_names or not rest:
                continue
            ref, op, rhs = FieldRef(alias, tuple(rest)), m["op"], m["rhs"]
            def k_exact(r: Row, ref=ref, op=op, rhs=rhs) -> bool:
                v = r.value(ref)
                v = getattr(v, "value", v)
                if v is None:
                    return False
                sv = str(v)
                return {"=": sv == rhs, ">=": sv >= rhs, "<=": sv <= rhs, ">": sv > rhs, "<": sv < rhs}[op]
            keeps.append(k_exact)
        elif isinstance(n, SemanticFilter):
            src = getattr(n.field, "source", None)
            if not src or src in base_names:
                continue
            kws = _keywords(n.text, limit=6)
            if not kws:
                continue
            proper = [k for k in kws if k[0].isupper()]
            need = proper or kws
            mode_all = bool(proper)
            def k_sem(r: Row, ref=n.field, need=need, mode_all=mode_all) -> bool:
                v = r.value(ref)
                if not isinstance(v, str):
                    return False
                low = v.lower()
                hits = [k.lower() in low for k in need]
                return all(hits) if mode_all else any(hits)
            keeps.append(k_sem)
    if not keeps:
        return rows
    return [r for r in rows if all(k(r) for k in keeps)]


def _resolve_joins(scan: Scan, rows: list[Row], ctx: "_Ctx",
                   base: tuple[str, str] | None = None) -> None:
    """Bind joined entities onto each row by following FK-equality conditions with
    indexed primary-key lookups (`<alias>.envelope.id`). A condition whose unbound side is
    not a primary key (a reverse foreign key) is left unresolved: there is no index for
    it, and a full scan per row would be dishonest about cost. Lookups are memoised."""
    base_table, base_alias, conds = _scan_joins(scan)
    if base is not None:
        base_table, base_alias = base
    if not conds:
        return
    tables = _alias_tables(scan)
    cache: dict[tuple[str, str], Any] = {}

    def fetch(table: str, key: str) -> Any:
        ck = (table, key)
        if ck not in cache:
            model = _TABLE_MODEL.get(table)
            row = ctx.store.conn.execute(f"SELECT data FROM {table} WHERE id = ?", (key,)).fetchone()
            cache[ck] = model.model_validate_json(row["data"]) if (row and model) else None
        return cache[ck]

    def is_pk(path: tuple[str, ...]) -> bool:
        return path in (("envelope", "id"), ("id",))

    for r in rows:
        bound: dict[str, Any] = {base_alias: r.doc, base_table: r.doc}
        progress = True
        while progress:
            progress = False
            for la, lp, ra, rp in conds:
                for (ba, bp, ua, up) in ((la, lp, ra, rp), (ra, rp, la, lp)):
                    if ba in bound and ua not in bound and is_pk(up) and ua in tables:
                        key = _walk(bound[ba], bp)
                        if key is None:
                            continue
                        ent = fetch(tables[ua], str(key))
                        if ent is not None:
                            bound[ua] = ent
                            bound[tables[ua]] = ent
                            progress = True
        r.joined = {k: v for k, v in bound.items() if v is not r.doc}


# Retrieval budget: the whole tiered pre-filter must finish inside this many seconds; a
# tier that overruns is abandoned and the next (broader, cheaper) one runs.
_RETRIEVAL_BUDGET_S = 75.0

# Opinion full text lives in a side database written by the opinions ingestion pass
# (`opinion_text(opinion_id, cluster_id, ..., plain_text)`, indexed by cluster_id, WAL so it
# is readable while it fills). `document.source_id` is the CourtListener cluster id, which
# is how a document finds its text. Attached read-only as `textdb` when present.
_TEXT_DB_ENV = "DATAFORM_TEXT_DB"


def _attach_text_db(store: Store) -> bool:
    candidates = [os.environ.get(_TEXT_DB_ENV, "")]
    try:
        candidates.append(str(Path(os.path.realpath(str(store.db_path))).parent / "dataform_api.db"))
    except Exception:
        pass
    for p in candidates:
        if not p or not Path(p).exists():
            continue
        try:
            store.conn.execute("ATTACH DATABASE ? AS textdb", (f"file:{p}?mode=ro",))
        except Exception:
            try:
                store.conn.execute("ATTACH DATABASE ? AS textdb", (p,))
            except Exception:
                continue
        try:
            store.conn.execute("SELECT 1 FROM textdb.opinion_text LIMIT 1").fetchall()
            return True
        except Exception:
            try: store.conn.execute("DETACH DATABASE textdb")
            except Exception: pass
    return False


def _hydrate_text(rows: list[Row], ctx: "_Ctx") -> None:
    """Fill `media.text.plain_text` on CourtListener opinions from the side table."""
    if not ctx.has_text:
        return
    for r in rows:
        d = r.doc
        if not isinstance(d, Document) or d.envelope.source_system != "courtlistener":
            continue
        if d.media.text is not None and (d.media.text.plain_text or "").strip():
            continue
        hit = ctx.store.conn.execute(
            "SELECT plain_text FROM textdb.opinion_text WHERE cluster_id = ? LIMIT 1",
            (d.envelope.source_id,)).fetchone()
        if hit and hit[0]:
            d.media.text = TextAsset(plain_text=hit[0])

# Tables small enough to sit inside an IN (SELECT id ...) semi-join without an index.
_SMALL_TABLES = {"organization", "person"}
# BQL date fields that the store mirrors into its indexed `date` column.
_DATE_FIELDS = {"date_issued", "date_filed", "date", "effective_date"}


def _choose_base(root: PlanNode, scan: Scan) -> tuple[str, str]:
    """(table, alias) to scan from. The optimizer's base is whatever the query listed
    first; we scan from the table the semantic predicates actually read (most SEM nodes,
    ties -> the optimizer's choice), because joins are resolved by primary-key lookups
    from the scanned row outward and a semantic filter needs its text on the row."""
    base_table, base_alias, _ = _scan_joins(scan)
    tables = _alias_tables(scan)
    votes: dict[str, int] = {}
    for n in walk(root):
        if isinstance(n, SemanticFilter):
            src = getattr(n.field, "source", None)
            t = tables.get(src, src) if src else base_table
            if t in _TABLE_MODEL:
                votes[t] = votes.get(t, 0) + 1
    if not votes or votes.get(base_table, 0) >= max(votes.values()):
        return base_table, base_alias
    best = max(votes, key=lambda t: votes[t])
    alias = next((a for a, t in tables.items() if t == best and a != t), best)
    return best, alias


def _json_path(path: tuple[str, ...]) -> str:
    return "$." + ".".join(path)


def _push_exact(m, base_names: set[str], scan: Scan, base_table: str,
                where: list[str], params: list[Any]) -> None:
    """Push one EXACT predicate into the Scan's SQL when it can be evaluated there:
    a base-table field (indexed column, the `date` column, or json_extract), or a
    single-hop semi-join into a small table the base points at by primary key."""
    alias, *path = m["lhs"].split(".")
    op, rhs = m["op"], m["rhs"]
    if not path:
        return
    last = path[-1]
    if alias in base_names:
        if path[0] == "envelope" and last in _INDEXED_COLS or last in _INDEXED_COLS and len(path) == 1:
            where.append(f"{last} {op} ?")
        elif last in _DATE_FIELDS:
            where.append(f"date {op} ?")
        else:
            where.append(f"json_extract(data, '{_json_path(tuple(path))}') {op} ?")
        params.append(rhs)
        return
    # semi-join: base.<fk> = alias.envelope.id  and alias's table is small
    tables = _alias_tables(scan)
    t = tables.get(alias)
    if t not in _SMALL_TABLES:
        return
    _, _, conds = _scan_joins(scan)
    for la, lp, ra, rp in conds:
        for (a1, p1, a2, p2) in ((la, lp, ra, rp), (ra, rp, la, lp)):
            if a1 in base_names and a2 == alias and p2 in (("envelope", "id"), ("id",)):
                where.append(f"json_extract(data, '{_json_path(p1)}') IN "
                             f"(SELECT id FROM {t} WHERE json_extract(data, '{_json_path(tuple(path))}') {op} ?)")
                params.append(rhs)
                return


# Display-form predicate (`doc.doc_type = "opinion"`, optionally EXACT(...)-wrapped or with a
# `date` literal tag) -- used only to read the alias/field/value off a plan node.
_EXACT_RE = re.compile(r'(?:EXACT\()?(?P<lhs>[\w.]+)\s*(?P<op>>=|<=|=|>|<)\s*'
                       r'(?:date\s+)?"(?P<rhs>[^"]*)"\)?')


def _joined_filters(root: PlanNode, scan: Scan, base_names: set[str]) -> bool:
    """Does the plan filter (exact or semantic) on a table other than the scanned base?"""
    for n in walk(root):
        if isinstance(n, ExactFilter):
            m = _EXACT_RE.search(n.predicate) if "_EXACT_RE" in globals() else None
            alias = (m["lhs"].split(".")[0] if m else (n.predicate.split(".")[0] if n.predicate else ""))
            if alias and alias not in base_names and len(scan.tables) > 1:
                return True
        elif isinstance(n, SemanticFilter):
            src = getattr(n.field, "source", None)
            if src and src not in base_names and len(scan.tables) > 1:
                return True
    return False


# When the plan filters on joined tables, retrieve a larger pool so those filters can be
# applied BEFORE the semantic stage's cap rather than after it (else a court filter would
# be applied to 15 arbitrary candidates and usually kill them all).
_POOL_FACTOR = 200  # joins are primary-key reads, so a big pool is cheap; the filter is what is selective


def _plan_scan_query(root: PlanNode, scan: Scan, has_text: bool = False) -> list[tuple[str, tuple]]:
    """Turn a dataform Scan into a real SQL query over the JSON-blob store.

    The optimizer emits `node.sql` as a relational FROM-fragment (e.g. `document AS doc`)
    that assumes real columns; the store keeps each record as a `data` JSON blob. So we
    ignore that fragment and build our own SELECT, pushing down what makes execution both
    fast and honest: indexed-column EXACT filters, and NOT-NULL on the field a downstream
    SEM will read (no point scanning rows that lack it). Single-table shape only; joins are
    not handled here yet."""
    base_table, base_alias = _choose_base(root, scan)
    table = base_table
    if table not in _TABLE_MODEL:
        raise NotImplementedError(f"{scan.node_id}: no store mapping for table {table!r}")
    base_names = {base_table, base_alias}

    where: list[str] = []
    params: list[Any] = []
    has_semantic = False
    keywords: list[str] = []

    def push(sql: str, ps: tuple) -> None:
        """Push one deterministic predicate, from wherever it now lives.

        Parameterised relational form only; the display-only BQL string is never
        reparsed, so strings, numbers and Date literals all arrive already unwrapped."""
        m = _SQL_CMP.match((sql or "").strip())
        if m and len(ps) == 1 and m["op"] != "!=":
            _push_exact({"lhs": m["lhs"], "op": m["op"], "rhs": ps[0]},
                        base_names, scan, base_table, where, params)

    # Predicates the optimizer already absorbed into the Scan. This list is their only
    # surviving record: pushdown *deletes* the ExactFilter nodes it absorbs, and this
    # backend ignores scan.sql because the store keeps records as JSON blobs rather than
    # columns. Reading only the tree below would mean an optimized plan pushes nothing.
    for pp in scan.pushed:
        push(pp.sql, pp.params)

    for n in walk(root):
        if isinstance(n, ExactFilter):
            # Predicates pushdown could not absorb — over expanded elements, or above a
            # blocking aggregate — still standing as operators.
            push(n.sql, n.params)
        elif isinstance(n, SemanticFilter):
            has_semantic = True
            src = getattr(n.field, "source", None)
            on_base = (not src) or (src in base_names) or len(scan.tables) == 1
            if n.predicate_class == PredicateClass.SEM and n.field.path and on_base:
                paths = ([tuple(n.field.path)] + list(_TEXT_FALLBACK)
                         if tuple(n.field.path) in _TEXT_FIELDS else [tuple(n.field.path)])
                clauses = [f"length(coalesce(json_extract(data, '{_json_path(p)}'), '')) > 0"
                           for p in dict.fromkeys(paths)]
                if tuple(n.field.path) in _TEXT_FIELDS:
                    clauses.append("json_array_length(coalesce(json_extract(data, '$.media.audio'), '[]')) > 0")
                where.append("(" + " OR ".join(clauses) + ")")
            if on_base:
                for k in _keywords(n.text):
                    if k.lower() not in (x.lower() for x in keywords):
                        keywords.append(k)

    cap = _SCAN_CAP_SEMANTIC if has_semantic else _SCAN_CAP_PLAIN
    pool = cap * _POOL_FACTOR if _joined_filters(root, scan, base_names) else cap
    base_where = " AND ".join(where)
    text_sem = has_text and table == "document" and any(
        isinstance(n, SemanticFilter) and tuple(n.field.path) in _TEXT_FIELDS
        and ((getattr(n.field, "source", None) or "") in base_names or len(scan.tables) == 1)
        for n in walk(root))

    def q(extra: list[str], extra_params: list[Any], via_text: bool = False) -> tuple[str, tuple]:
        conds = ([base_where] if base_where else []) + extra
        if via_text:
            # keyword tier over the opinion text side table, joined back to documents
            sql = (f"SELECT {table}.data FROM textdb.opinion_text t JOIN {table} "
                   f"ON {table}.source_system = 'courtlistener' AND {table}.source_id = t.cluster_id")
        else:
            sql = f"SELECT data FROM {table}"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return sql + f" LIMIT {pool}", tuple(params + extra_params)

    # Tiers, most selective first. Each is capped; _scan merges them by id until `cap`.
    # Substring match on the JSON blob is a deliberate, index-free retrieval: the corpus
    # has no FTS, and a bounded LIKE scan is far cheaper than a model call per row.
    tiers: list[tuple[str, tuple]] = []
    if has_semantic and keywords:
        if text_sem:
            # opinion text first: it is where topical predicates are actually decided
            if len(keywords) > 1:
                tiers.append(q(["t.plain_text LIKE ?"] * len(keywords), [f"%{k}%" for k in keywords], via_text=True))
            for k in keywords[:3]:
                tiers.append(q(["t.plain_text LIKE ?"], [f"%{k}%"], via_text=True))
        if len(keywords) > 1:
            tiers.append(q(["data LIKE ?"] * len(keywords), [f"%{k}%" for k in keywords]))
        for k in keywords:
            tiers.append(q(["data LIKE ?"], [f"%{k}%"]))
    tiers.append(q([], []))
    return tiers


def _scan(node: Scan, ctx: _Ctx) -> list[Row]:
    """Load rows from the JSON-blob store and revive each `data` blob into its pydantic model.

    Uses the SQL computed by _plan_scan_query (stashed on ctx), not the optimizer's
    relational fragment."""
    t0 = time.perf_counter()
    base_table, base_alias = ctx.scan_base.get(node.node_id, (node.tables[0], node.tables[0]))
    model = _TABLE_MODEL.get(base_table, Document)
    tiers = ctx.scan_queries.get(node.node_id) or [(f"SELECT data FROM {base_table} LIMIT 200", ())]
    pool = int(re.search(r"LIMIT (\d+)", tiers[-1][0]).group(1))
    cap = ctx.scan_cap.get(node.node_id, pool)
    conn = ctx.store.conn

    # Abort a tier that overruns the retrieval budget; sqlite raises "interrupted".
    deadline = t0 + _RETRIEVAL_BUDGET_S
    def _tick() -> int:
        return 1 if time.perf_counter() > deadline else 0
    conn.set_progress_handler(_tick, 20000)

    seen: set[str] = set()
    rows: list[Row] = []
    try:
        for i, (sql, params) in enumerate(tiers):
            if len(rows) >= pool:
                break
            last = i == len(tiers) - 1
            if last:
                conn.set_progress_handler(None, 0)  # the plain tier always completes
            try:
                fetched = conn.execute(sql, params).fetchall()
            except Exception as e:  # sqlite3.OperationalError: interrupted
                if "interrupt" in str(e).lower():
                    continue
                raise
            for r in fetched:
                doc = model.model_validate_json(r["data"])
                key = doc.envelope.id
                if key in seen:
                    continue
                seen.add(key)
                rows.append(Row(doc))
                if len(rows) >= pool:
                    break
    finally:
        conn.set_progress_handler(None, 0)

    if len(node.tables) > 1:
        _resolve_joins(node, rows, ctx, base=(base_table, base_alias))
        rows = _prefilter_joined(ctx.plan_root, node, rows, base=(base_table, base_alias))
    rows = rows[:cap]
    _hydrate_text(rows, ctx)

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
        alias, *rest = lhs.split('.')
        ref = FieldRef(alias, tuple(rest))         # alias-aware: reads a joined entity when bound
        def read(r: Row) -> Any:
            v = r.value(ref)
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
            out.append(Row(r.doc, element=el, evidence=list(r.evidence), joined=r.joined))
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
        out.append(Row(r.doc, element=None, evidence=r.evidence, joined=r.joined))
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
            cols[c.label] = r.value(c.ref) if c.ref else None
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
    ctx.has_text = _attach_text_db(store)
    ctx.plan_root = root

    # Precompute each Scan's real SQL (pushdowns need the whole pipeline, which a bottom-up
    # walk of a single node can't see).
    for n in walk(root):
        if isinstance(n, Scan) and n.tables and n.tables[0] in _TABLE_MODEL:
            ctx.scan_base[n.node_id] = _choose_base(root, n)
            ctx.scan_queries[n.node_id] = _plan_scan_query(root, n, has_text=ctx.has_text)
            has_sem = any(isinstance(x, SemanticFilter) for x in walk(root))
            ctx.scan_cap[n.node_id] = _SCAN_CAP_SEMANTIC if has_sem else _SCAN_CAP_PLAIN

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
