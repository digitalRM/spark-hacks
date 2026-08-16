"""Step 3 — natural language to a BQL query, via Nemotron.

What makes this a compiler and not a prompt is the VALIDATE-REPAIR LOOP. The model
writes JSON; we decode it against the canonical `ast.py` and check it against the schema,
and if anything is wrong we hand the model its own output plus the exact JSON path
of each mistake and ask for a correction. Up to `MAX_ATTEMPTS` times. Every
attempt is recorded, so a failure is a report rather than a mystery.

    question --[system prompt: encoding + schema + rules + few-shots]--> Nemotron
             --[serde.decode -> validate -> repair on error]-----------> Query

The output is JSON in the encoding `serde.py` defines, which is a faithful
serialization of the dataclasses in `ast.py`. `.query` is the wire form to
hand downstream; `.ast` is the same thing decoded.

Cost: zero on a cache hit, otherwise 1-3 round trips, once per question.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable

from . import checks, client, relevance, schema, serde
from .ast import (Aggregator, AggregatorOp, And, Between, Comparison,
                  ComparisonOperator as Op, FieldRef, Fuzzy, InList, Join, Like,
                  Not, Or, Query, TableRef, Unnest)
from .serde import BQL_VERSION, DecodeError

MAX_ATTEMPTS = int(os.environ.get("BQL_MAX_ATTEMPTS", "3"))
# Match the hosted Nemotron Super generation settings by default while leaving both
# phases independently configurable for production tuning.
INITIAL_TEMPERATURE = client.DEFAULT_TEMPERATURE
REPAIR_TEMPERATURE = float(os.environ.get(
    "BQL_REPAIR_TEMPERATURE", str(client.DEFAULT_TEMPERATURE)))
CACHE_DIR = Path(os.environ.get("BQL_CACHE_DIR", Path(__file__).resolve().parent / "cache"))
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"

ChatFn = Callable[..., client.ChatResponse]
RelevanceFn = Callable[[str], relevance.RelevanceResult]


@dataclass
class CompileResult:
    """The outcome of one compile. `ok` says which half of this is meaningful."""
    ok: bool
    question: str
    query: dict[str, Any] | None = None    # the wire form — hand this to the optimizer
    ast: Query | None = None               # the same thing, decoded
    errors: list[DecodeError] = dc_field(default_factory=list)
    attempts: list[dict[str, Any]] = dc_field(default_factory=list)
    model: str = ""
    schema_name: str = ""
    cached: bool = False
    latency_ms: float = 0.0
    is_legal: bool = True
    relevance_model: str = ""
    # Parts of the question the language cannot express. The query is still valid;
    # it just answers something narrower than what was asked.
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def printed(self) -> str:
        """The query in readable BQL, via grammar.pp_query."""
        from .ast import pp_query
        return pp_query(self.ast).strip() if self.ast else ""

    def envelope(self) -> dict[str, Any]:
        """The query plus the metadata a consumer needs to interpret it."""
        if not self.ok or self.query is None:
            raise ValueError("cannot build an envelope from a failed compile")
        envelope = {"bql_version": BQL_VERSION, "schema": self.schema_name,
                    "is_legal": self.is_legal,
                    "question": self.question, "query": self.query,
                    "bql": self.printed}
        if self.warnings:
            envelope["warnings"] = list(self.warnings)
        return envelope

    def error_report(self) -> dict[str, Any]:
        """A structured failure, safe to return over HTTP or show in a UI."""
        return {"ok": False,
                "stage": "relevance" if not self.is_legal else "compile",
                "is_legal": self.is_legal, "question": self.question,
                "message": self.message(), "errors": list(self.errors),
                "warnings": list(self.warnings), "attempts": self.attempts}

    def message(self) -> str:
        if self.ok:
            return "ok"
        if not self.is_legal:
            return relevance.REJECTION_MESSAGE
        if not self.errors:
            return "the compiler produced no query"
        head = "; ".join(str(e) for e in self.errors[:3])
        more = f" (+{len(self.errors) - 3} more)" if len(self.errors) > 3 else ""
        failed = f"could not compile after {len(self.attempts)} attempts: {head}{more}"
        # A question the language cannot express is the likeliest reason a compile
        # collapses: the model ties itself in knots trying to say something the
        # grammar has no word for, and the parse error that comes out is a symptom,
        # not the cause. Lead with the cause.
        if self.warnings:
            return (f"this question asks for something BQL cannot express yet — "
                    f"{self.warnings[0]} The compiler then failed: {failed}")
        return failed

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "is_legal": self.is_legal,
                "question": self.question, "query": self.query,
                "bql": self.printed, "printed": self.printed,
                "errors": list(self.errors), "attempts": self.attempts,
                "model": self.model, "schema": self.schema_name, "cached": self.cached,
                "relevance_model": self.relevance_model,
                "warnings": list(self.warnings), "latency_ms": round(self.latency_ms, 1)}


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #
ENCODING = """\
Every node is a JSON object with a "kind". These are all of them:

  Query       {"kind":"Query", "select":[<expr>,...], "source":<source>,
               "where":<condition>|null, "group_by":[<expr>,...], "limit":<int>|null}
  TableRef    {"kind":"TableRef", "name":"<table>", "alias":"<alias>"}
  FieldRef    {"kind":"FieldRef", "source":"<alias>", "path":["field","nested",...]}
  Unnest      {"kind":"Unnest", "ref":<FieldRef>}
  Aggregator  {"kind":"Aggregator", "op":"count|sum|avg|min|max", "arg":<expr>|null}
              Only count may use null, which means count(*).
  Join        {"kind":"Join", "condition":<Comparison>, "left":<source>, "right":<source>}
  Comparison  {"kind":"Comparison", "op":"<op>", "field1":<expr>, "field2":<expr>}
  InList      {"kind":"InList", "field":<FieldRef>, "values":[<literal>,...]}
  Between     {"kind":"Between", "field":<FieldRef>, "low":<literal>, "high":<literal>}
  Like        {"kind":"Like", "field":<FieldRef>, "pattern":"%text%"}
  Fuzzy       {"kind":"Fuzzy", "field":<FieldRef>|<Unnest>, "text":"<condition>"}
  And / Or    {"kind":"And", "children":[<condition>,...]}
  Not         {"kind":"Not", "child":<condition>}

<expr>      FieldRef, Unnest, Aggregator, or a bare JSON literal.
<source>    a TableRef or Join object. Joins nest to the left.
<condition> Comparison, InList, Between, Like, Fuzzy, And, Or or Not.
<literal>   a JSON string, number or boolean. Dates are strings, "YYYY-MM-DD".
"""

RULES = """\
RULES. Breaking one gets your output rejected and sent back to you.
 1. Output ONLY the JSON object. No prose, no markdown fences, no comments, no trailing commas.
 2. There are exactly two kinds of condition. A comparison (Comparison, InList, Between,
    Like) tests a plain SCALAR column and costs nothing. Fuzzy states something a model
    must judge by reading, looking or listening, and costs a model call for every item.
 3. NEVER name a modality or a model. There is no Visual, Audio, Sem or Sim node. You pick
    the FIELD, and the field's type in the selected schema decides which model runs.
 4. Every field path exists in the schema. "source" is the TableRef alias, not necessarily
    the physical table name. Use Unnest only for a list/sequence field.
 5. Every table you use must appear in "source", joined along a listed JOIN EDGE. Nest Join
    objects to reach a third or fourth table.
 6. A comparison goes on SCALAR columns only. Fuzzy goes on everything that is not SCALAR.
    Never the other way round.
 7. Comparison operators are exactly: {ops}.
 8. Keep Fuzzy text short, declarative and self-contained. It is judged one page, one
    segment or one opinion at a time, with no other context.
 8b. Fuzzy has ONE field expression. For independent semantic requirements, write one
    Fuzzy per field inside And/Or. Use Unnest when the result must be at element grain;
    otherwise a collection FieldRef is quantified as a whole.
 9. Always emit "group_by" (usually []). Default to "limit": 10 unless requested.
10. Respect the table, field, enum/value, and join descriptions in the selected schema.
    Never copy a table or field from a few-shot unless that name exists in this schema."""


def _f(source: str, column: str) -> FieldRef:
    return FieldRef(source, (column,))


def _fp(source: str, *path: str) -> FieldRef:
    return FieldRef(source, tuple(path))


def _t(name: str, alias: str | None = None) -> TableRef:
    return TableRef(name, alias or name)


def _eq(a: FieldRef, b: Any) -> Comparison:
    return Comparison(Op.EQ, a, b)


def _join(left: Any, right: str, on_left: FieldRef, on_right: FieldRef) -> Join:
    return Join(_eq(on_left, on_right), left, _t(right))


# cluster -> docket, then -> opinion, then -> citation. Joins nest.
_CD = _join(_t("cluster"), "docket", _f("cluster", "docket_id"), _f("docket", "id"))
_CDO = _join(_CD, "opinion", _f("opinion", "cluster_id"), _f("cluster", "id"))
_CDOC = _join(_CDO, "citation", _f("citation", "citing_opinion_id"), _f("opinion", "id"))
_CO = _join(_t("cluster"), "opinion", _f("opinion", "cluster_id"), _f("cluster", "id"))

# Few-shots, built as dataclasses so they are valid by construction and encoded on
# the way into the prompt. Between them they use every node in the grammar.
FEW_SHOTS: list[tuple[str, Query]] = [
    ("9th Circuit qualified-immunity cases citing Graham v. Connor, where the scanned record "
     "contains a photographic exhibit, and a judge expressed skepticism at oral argument.",
     Query(select=(_f("cluster", "id"), _f("cluster", "case_name")), source=_CDOC,
           where=And((
               _eq(_f("docket", "court_id"), "ca9"),
               _eq(_f("citation", "cited_cite"), "490 U.S. 386"),
               Fuzzy(_f("opinion", "plain_text"), "qualified immunity for excessive force"),
               Fuzzy(_f("cluster", "scan_pages"), "contains a photographic exhibit"),
               Fuzzy(_f("docket", "argument"), "a judge expressed skepticism"))),
           group_by=(),
           limit=10)),

    ("Published 9th or 10th Circuit decisions since 2020 that discuss excessive force.",
     Query(select=(_f("cluster", "id"), _f("cluster", "case_name")), source=_CDO,
           where=And((
               InList(_f("docket", "court_id"), ("ca9", "ca10")),
               _eq(_f("cluster", "precedential_status"), "Published"),
               Comparison(Op.GE, _f("cluster", "date_filed"), "2020-01-01"),
               Fuzzy(_f("opinion", "plain_text"), "excessive force by police officers"))),
           group_by=(),
           limit=10)),

    ("Ninth Circuit cases, not unpublished, where either a judge asked at oral argument "
     "whether the right was clearly established or the opinion holds the officer is "
     "entitled to qualified immunity, and which do not discuss municipal liability.",
     Query(select=(_f("cluster", "id"), _f("cluster", "case_name")), source=_CDO,
           where=And((
               _eq(_f("docket", "court_id"), "ca9"),
               Comparison(Op.NE, _f("cluster", "precedential_status"), "Unpublished"),
               Or((Fuzzy(_f("docket", "argument"),
                         "a judge asked whether the right was clearly established"),
                   Fuzzy(_f("opinion", "plain_text"),
                         "the officer is entitled to qualified immunity"))),
               Not(Fuzzy(_f("opinion", "plain_text"), "municipal liability")))),
           group_by=(),
           limit=10)),

    ("Second Circuit oral arguments from 2023 where counsel conceded a point under questioning.",
     Query(select=(_f("docket", "id"), _f("docket", "case_name")), source=_t("docket"),
           where=And((
               _eq(_f("docket", "court_id"), "ca2"),
               Between(_f("docket", "date_filed"), "2023-01-01", "2023-12-31"),
               Fuzzy(_f("docket", "argument"), "counsel conceded a point under questioning"))),
           group_by=(),
           limit=10)),

    ("Opinions by Judge Berzon whose scanned record cites Graham v. Connor.",
     Query(select=(_f("opinion", "id"), _f("opinion", "author_str")), source=_CO,
           where=And((
               Like(_f("opinion", "author_str"), "%Berzon%"),
               Fuzzy(_f("cluster", "scan_text"), "cites Graham v. Connor"))),
           group_by=(),
           limit=10)),

    ("Five 9th Circuit cases where a scanned page contains a photograph.",
     Query(select=(_f("cluster", "id"), _f("cluster", "case_name")), source=_CDO,
           where=And((
               _eq(_f("docket", "court_id"), "ca9"),
               Fuzzy(Unnest(_f("cluster", "scan_pages")), "contains a photograph"))),
           group_by=(),
           limit=5)),

    # The same two sources, but two independent conditions: an And of two Fuzzy
    # nodes, each judged on its own, which is far cheaper than one joint judgement.
    ("9th Circuit cases where the opinion discusses body-camera footage and the scanned "
     "record contains a photograph.",
     Query(select=(_f("cluster", "id"), _f("cluster", "case_name")), source=_CDO,
           where=And((
               _eq(_f("docket", "court_id"), "ca9"),
               Fuzzy(_f("opinion", "plain_text"), "body-camera footage"),
               Fuzzy(_f("cluster", "scan_pages"), "contains a photograph"))),
           group_by=(),
           limit=10)),

    ("Count published cases by court.",
     Query(select=(_f("docket", "court_id"), Aggregator(AggregatorOp.COUNT, None)),
           source=_CD,
           where=_eq(_f("cluster", "precedential_status"), "Published"),
           group_by=(_f("docket", "court_id"),),
           limit=None)),
]


DATAFORM_FEW_SHOTS: list[tuple[str, Query]] = [
    ("CourtListener opinions discussing qualified immunity.",
     Query(select=(_fp("doc", "envelope", "id"), _fp("doc", "title")),
           source=_t("document", "doc"),
           where=And((
               _eq(_fp("doc", "envelope", "source_system"), "courtlistener"),
               _eq(_fp("doc", "doc_type"), "opinion"),
               Fuzzy(_fp("doc", "media", "text", "plain_text"),
                     "discusses qualified immunity"))),
           group_by=(), limit=10)),

    ("Ninth Circuit proceedings with a document containing a photographic exhibit.",
     Query(select=(_fp("proc", "envelope", "id"), _fp("proc", "title")),
           source=Join(
               _eq(_fp("doc", "proceeding_id"), _fp("proc", "envelope", "id")),
               _t("proceeding", "proc"), _t("document", "doc")),
           where=And((
               Fuzzy(_fp("proc", "title"), "Ninth Circuit case"),
               Fuzzy(Unnest(_fp("doc", "media", "images")),
                     "contains a photographic exhibit"))),
           group_by=(), limit=10)),

    ("Count documents by document type.",
     Query(select=(_fp("doc", "doc_type"), Aggregator(AggregatorOp.COUNT, None)),
           source=_t("document", "doc"), where=None,
           group_by=(_fp("doc", "doc_type"),), limit=None)),
]


def few_shots_for(registry: schema.Registry) -> list[tuple[str, Query]]:
    """Return examples that use only the selected ingestion schema."""
    return DATAFORM_FEW_SHOTS if registry.name == "dataform" else FEW_SHOTS


def build_system_prompt(registry: schema.Registry) -> str:
    """Encoding + schema + rules. Generated, never hand-maintained. Free."""
    return (
        "You translate ONE natural-language question into a BQL query, encoded as JSON.\n\n"
        "BQL is a small SQL-shaped language over a corpus that mixes structured columns, "
        "written text, scanned page images and audio recordings. A query filters, joins and "
        "projects; it never says how to answer a question, only what is being asked.\n\n"
        "== JSON ENCODING ==\n" + ENCODING + "\n\n"
        "== SCHEMA (the only fields that exist) ==\n" + registry.render_for_prompt() + "\n\n"
        "== " + RULES.format(ops=", ".join(f'"{o}"' for o in serde.OPS)) + "\n\n"
        "Respond with the JSON object and nothing else."
    )


def build_messages(question: str, registry: schema.Registry) -> list[dict[str, str]]:
    """System prompt, then the few-shots as user/assistant turns, then the question. Free."""
    msgs = [{"role": "system", "content": build_system_prompt(registry)}]
    for q, ast in few_shots_for(registry):
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant",
                     "content": json.dumps(serde.encode(ast), separators=(",", ":"))})
    msgs.append({"role": "user", "content": question})
    return msgs


# Nested joins are where the model actually loses its footing: four tables means
# three levels of `{"kind":"Join", "left": {...}}` and it miscounts the braces,
# emitting a source tree that closes early and leaves the last table out. Repeating
# the rule does not help — showing the exact skeleton does, so this goes in verbatim
# whenever a scope or join error comes back.
_JOIN_SKELETON = """\
The source tree was wrong. Joins nest to the LEFT: each Join's "left" is everything
joined so far and its "right" is ONE TableRef. Four tables look exactly like
this, and nothing else:

{"kind":"Join","condition":<C3>,
 "left":{"kind":"Join","condition":<C2>,
         "left":{"kind":"Join","condition":<C1>,
                 "left":{"kind":"TableRef","name":"cluster","alias":"cluster"},
                 "right":{"kind":"TableRef","name":"docket","alias":"docket"}},
         "right":{"kind":"TableRef","name":"opinion","alias":"opinion"}},
 "right":{"kind":"TableRef","name":"citation","alias":"citation"}}

Count your braces before answering: every table you mention anywhere in the query
must appear as a "left" or "right" in that tree."""


def repair_prompt(errors: list[DecodeError], attempt: int) -> str:
    """Hand the model the exact JSON path of every mistake. Free."""
    listed = "\n".join(f"  {e['path']}  [{e['code']}]  {e['message']}" for e in errors[:12])
    codes = {e["code"] for e in errors}
    hint = ""
    if codes & {"table_not_in_scope", "unknown_join_edge", "bad_join", "unknown_table"}:
        hint = "\n\n" + _JOIN_SKELETON
    return (
        f"Your JSON was rejected (attempt {attempt} of {MAX_ATTEMPTS}). Problems:\n{listed}"
        f"{hint}\n\n"
        "Return the corrected JSON object, complete, from scratch. Fix exactly these problems "
        "and change nothing else. Remember: no Visual/Audio/Sem/Sim nodes; comparisons on "
        "SCALAR columns only and Fuzzy on everything else; Fuzzy.field is one FieldRef or "
        "Unnest expression; every FieldRef uses a source alias and path list from the schema. "
        "Output the JSON object only, "
        "with nothing after the closing brace."
    )


# --------------------------------------------------------------------------- #
# Getting JSON out of a model's reply
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Questions BQL cannot express
# --------------------------------------------------------------------------- #
# The canonical AST now has aggregation/GROUP BY but still has no ORDER BY. A
# question that needs ordering can otherwise compile to a plausible, wrong limit.
#
# Both patterns are deliberately narrow. A false positive here is a confusing note
# on a correct query, so anything ambiguous is left out: " per " is not a signal,
# because "per curiam" is everywhere in case law.
_CANNOT_EXPRESS: list[tuple[str, re.Pattern[str], str]] = [
    ("ordering",
     re.compile(r"\b(most recent|latest|newest|oldest|earliest|"
                r"sorted by|ranked by|in order of|chronological)\b", re.I),
     "BQL has no ORDER BY yet, so any LIMIT returns an arbitrary subset rather than "
     "the first ones by the order you asked for."),
]


def cannot_express(question: str) -> list[str]:
    """Warnings for parts of a question the language has no way to say. Free."""
    return [message for _, pattern, message in _CANNOT_EXPRESS if pattern.search(question)]


def extract_json(text: str) -> tuple[dict | None, str | None]:
    """Pull the JSON object out of a model reply. Returns (object, error_message).

    Tolerates markdown fences, leading prose and a reasoning trace, because models
    emit all three even when told not to. Free.
    """
    if not text or not text.strip():
        return None, "the model returned an empty response"
    text = _THINK.sub("", text)
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    stripped = text.strip()
    candidates.append(stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    decoder = json.JSONDecoder()
    last = "no JSON object found in the response"
    for cand in candidates:
        cand = cand.strip()
        if not cand.startswith("{"):
            continue
        try:
            # raw_decode, not loads: models routinely emit a complete object and then
            # keep going — a second copy, a stray brace, a sentence of commentary.
            # loads rejects the whole reply for that; raw_decode takes the first
            # complete value and stops.
            obj, end = decoder.raw_decode(cand)
        except json.JSONDecodeError as e:
            last = f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}"
            continue
        if not isinstance(obj, dict):
            last = f"expected a JSON object, got {type(obj).__name__}"
            continue

        rest = cand[end:].strip()
        # What follows the first complete object decides whether it IS the answer.
        # Junk after a finished object is harmless. But text that continues the
        # object — ",\"where\": ..." — means a stray brace closed it early, and the
        # part we parsed is a real query with whole clauses silently missing. That is
        # far worse than a parse error, so it is reported as one.
        if rest[:1] in (",", ":") or rest[:1] == '"':
            cut = rest[:60].replace("\n", " ")
            last = (f"the object closed early — there is an extra '}}' before the end. "
                    f"Everything from {cut!r} onward was left outside it, so clauses "
                    f"are missing. Re-emit one balanced object.")
            continue
        return obj, None
    return None, last


def check_payload(obj: Any, registry: schema.Registry) -> tuple[Query | None, list[DecodeError]]:
    """Decode then validate. Returns (query, errors); query is None if it did not decode."""
    errors = serde.decode_errors(obj)
    if errors:
        return None, errors
    query = serde.decode(obj)
    return query, checks.validate(query, registry)


# --------------------------------------------------------------------------- #
# The compile
# --------------------------------------------------------------------------- #
def compile_question(question: str, *, registry: schema.Registry | None = None,
                     use_cache: bool = True, model: str | None = None,
                     chat_fn: ChatFn | None = None,
                     relevance_fn: RelevanceFn | None = None,
                     max_attempts: int = MAX_ATTEMPTS) -> CompileResult:
    """Compile a question into a BQL query. Never raises for a bad model answer.

    On success `.query` is the wire form for the optimizer and `.ast` is the
    decoded Query. On failure `.errors` says what was wrong and `.attempts`
    records every round trip. A transport failure (no key, endpoint down) does
    raise, because that is an operator problem rather than a query problem.

    Cost: one small local relevance call, plus 0 calls on a query-cache hit or
    1..max_attempts hosted compiler calls on a miss.
    """
    t0 = time.perf_counter()
    reg = registry or schema.load()
    send = chat_fn or client.chat
    question = " ".join(question.split())

    # An injected compiler chat function is a unit-test/replay boundary and is
    # already preclassified unless the caller injects a relevance function too.
    if relevance_fn is not None:
        decision = relevance_fn(question)
    elif chat_fn is not None:
        decision = relevance.RelevanceResult(True, "preclassified")
    else:
        decision = relevance.classify(question)
    if not decision.is_legal:
        return CompileResult(
            ok=False, question=question, model=decision.model,
            schema_name=reg.name, is_legal=False,
            relevance_model=decision.model,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    # Resolve the compiler only after Lightning approves the domain. An injected
    # chat function is a test/replay and should never trigger a server probe.
    mdl = model or (client.COMPILER_MODEL if chat_fn else client.resolve_compiler_model())

    if use_cache:
        hit = load_cached(question, reg.name)
        if hit is not None:
            ast, errors = check_payload(hit, reg)
            if not errors and ast is not None:
                return CompileResult(ok=True, question=question, query=hit, ast=ast,
                                     model="cache", schema_name=reg.name, cached=True,
                                     relevance_model=decision.model,
                                     latency_ms=(time.perf_counter() - t0) * 1000)

    messages = build_messages(question, reg)
    attempts: list[dict[str, Any]] = []
    errors: list[DecodeError] = []

    for attempt in range(1, max_attempts + 1):
        temperature = INITIAL_TEMPERATURE if attempt == 1 else REPAIR_TEMPERATURE
        resp = send(messages, model=mdl, temperature=temperature)
        obj, parse_error = extract_json(resp.text)
        if parse_error is not None:
            ast, errors = None, [DecodeError("$", "invalid_json", parse_error)]
        else:
            ast, errors = check_payload(obj, reg)

        attempts.append({"attempt": attempt, "model": resp.model, "ok": not errors,
                         "errors": list(errors), "tokens_in": resp.tokens_in,
                         "tokens_out": resp.tokens_out, "latency_ms": round(resp.latency_ms, 1),
                         "dropped_shots": resp.dropped_shots,
                         "temperature": temperature,
                         "raw": resp.text[:2000]})

        if not errors and ast is not None:
            result = CompileResult(ok=True, question=question, query=serde.encode(ast), ast=ast,
                                   attempts=attempts, model=resp.model, schema_name=reg.name,
                                   relevance_model=decision.model,
                                   warnings=cannot_express(question),
                                   latency_ms=(time.perf_counter() - t0) * 1000)
            if use_cache:
                save_cached(result)
            return result

        if attempt < max_attempts:
            messages = messages + [{"role": "assistant", "content": resp.text},
                                   {"role": "user", "content": repair_prompt(errors, attempt)}]

    return CompileResult(ok=False, question=question, errors=errors, attempts=attempts,
                         model=mdl, schema_name=reg.name,
                         relevance_model=decision.model,
                         warnings=cannot_express(question),
                         latency_ms=(time.perf_counter() - t0) * 1000)


# --------------------------------------------------------------------------- #
# Cache — so the demo does not depend on wifi
# --------------------------------------------------------------------------- #
def cache_key(question: str, schema_name: str) -> str:
    norm = " ".join(question.lower().split()).strip(" .?!")
    return hashlib.sha256(f"{schema_name}|{norm}".encode()).hexdigest()[:16]


def load_cached(question: str, schema_name: str) -> dict | None:
    """A previously compiled query for this exact question, or a shipped example. Free."""
    key = cache_key(question, schema_name)
    for path in (CACHE_DIR / f"{key}.json", *sorted(EXAMPLES_DIR.glob("*.json"))):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if path.parent == CACHE_DIR or cache_key(data.get("question", ""), schema_name) == key:
            q = data.get("query")
            if isinstance(q, dict):
                return q
    return None


def save_cached(result: CompileResult) -> Path | None:
    """Write a successful compile to the cache. Best effort; never fails a compile."""
    if not result.ok or result.query is None:
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{cache_key(result.question, result.schema_name)}.json"
        path.write_text(json.dumps(result.envelope(), indent=2) + "\n")
        return path
    except OSError:
        return None
