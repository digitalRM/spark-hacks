"""Step 3 — natural language to a BQL query, via hosted Nemotron Super.

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

Routing is not this module's job. `relevance.py` decides whether a question is a
record search at all, and `api/driver.py` acts on that decision; by the time
`compile_question` is called the answer is yes.

Cost: zero on a cache hit, otherwise 1-3 round trips, once per question.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field as dc_field, replace
from pathlib import Path
from typing import Any

import config

from . import checks, client, schema, serde
from .ast import (Aggregator, AggregatorOp, And, Between, Comparison,
                  ComparisonOperator as Op, Date, FieldRef, Fuzzy, InList, Join, Like,
                  Not, Or, Query, TableRef, Unnest)
from .serde import BQL_VERSION, DecodeError

MAX_ATTEMPTS = 3
# The first attempt samples at the model's recommended reasoning temperature. Repair turns
# run cooler: the model is being handed exact error paths, not asked to explore, and at 1.0
# it was seen reproducing the same JSON syntax slip three attempts running.
TEMPERATURE = client.TEMPERATURE
REPAIR_TEMPERATURE = 0.6
# Ceiling on one attempt's output, reasoning included. The JSON AST itself is ~1k tokens;
# measured runs spent 4-8k more on reasoning before it. 8192 leaves room for that and
# fails a runaway attempt into the repair loop instead of waiting on client.MAX_TOKENS.
MAX_TOKENS = 8192
# Thinking stays on for every attempt. Correctness is preferred over a fast but
# semantically incomplete query; successful compiles are cached.
THINKING = True
CACHE_DIR = Path(__file__).resolve().parent / "cache"
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
# Prompt and semantic-contract version. Changing it invalidates old compiles that
# may still be structurally valid while answering a different question.
CACHE_VERSION = "compiler-v10-deterministic-relationships"


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
    # Parts of the question the language cannot express. The query is still valid;
    # it just answers something narrower than what was asked.
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def printed(self) -> str:
        """The query in readable BQL, via grammar.pp_query."""
        from .ast import pp_query
        return pp_query(self.ast).strip() if self.ast else ""

    def message(self) -> str:
        if self.ok:
            return "ok"
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
  Date        {"kind":"Date", "value":"YYYY-MM-DD"}   a calendar date, and the ONLY
              way to write one. "2020-01-01" on its own is text, and text has no order.
  Fuzzy       {"kind":"Fuzzy", "field":<FieldRef>|<Unnest>, "text":"<condition>"}
  And / Or    {"kind":"And", "children":[<condition>,...]}
  Not         {"kind":"Not", "child":<condition>}

<expr>      FieldRef, Unnest, Aggregator, Date, or a bare JSON literal.
<source>    a TableRef or Join object. Joins nest to the left.
<condition> Comparison, InList, Between, Like, Fuzzy, And, Or or Not.
<literal>   a JSON string, number or boolean, or a Date object.
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
 6. A comparison goes on SCALAR and DATE columns only. Fuzzy goes on everything else.
    Never the other way round.
 7. Comparison operators are exactly: {ops}.
 7b. A DATE column is compared against a Date object, never a bare string, in Comparison,
    Between and InList alike. Ordering (<, <=, >, >=) works on numbers and dates only.
 8. Keep Fuzzy text short, declarative and self-contained. It is judged one page, one
    segment or one opinion at a time, with no other context.
 8b. Fuzzy has ONE field expression. For independent semantic requirements, write one
    Fuzzy per field inside And/Or. Use Unnest when the result must be at element grain;
    otherwise a collection FieldRef is quantified as a whole.
 9. Always emit "group_by" (usually []). Default to "limit": 10 unless requested.
10. Respect the table, field, enum/value, and join descriptions in the selected schema.
    Never copy a table or field from a few-shot unless that name exists in this schema.
11. Before answering, internally inventory every constraint in the CURRENT user question
    (jurisdiction, court level, relationship, lower/upper dates, inclusion/exclusion, and
    limit). Verify that each appears in the JSON. A few-shot is only a shape example:
    never copy its court, date, modality, topic, predicate, or table unless the current
    question asks for it.
12. "Since/from YEAR" and "YEAR onwards" are open-ended: use date >= "YEAR-01-01".
    NEVER add Dec 31 or any upper bound unless the user gives one. A bare YEAR by itself
    means only that calendar year and uses Between Jan 1 and Dec 31.
13. Use the minimum tables and conditions needed. Do not add proceeding_type = "case"
    merely because the corpus contains cases; add it only when the current question asks
    for cases or proceedings."""


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
               Comparison(Op.GE, _f("cluster", "date_filed"), Date("2020-01-01")),
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
               Between(_f("docket", "date_filed"), Date("2023-01-01"), Date("2023-12-31")),
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

    ("Fourth Circuit employment-discrimination opinions since 2015 that affirmed "
     "summary judgment.",
     Query(select=(_fp("doc", "envelope", "id"), _fp("doc", "title")),
           source=_t("document", "doc"),
           where=And((
               _eq(_fp("doc", "doc_type"), "opinion"),
               _eq(_fp("doc", "jurisdiction"), "ca4"),
               Comparison(Op.GE, _fp("doc", "date_issued"), "2015-01-01"),
               Fuzzy(_fp("doc", "media", "text", "plain_text"),
                     "employment discrimination"),
               Fuzzy(_fp("doc", "media", "text", "plain_text"),
                     "affirmed summary judgment"))),
           group_by=(), limit=10)),

    ("Seventh Circuit cases appealed to or reviewed by the Supreme Court from 2008 "
     "through 2020.",
     Query(select=(_fp("proc", "envelope", "id"), _fp("proc", "title")),
           source=Join(
               _eq(_fp("ev", "proceeding_id"), _fp("proc", "envelope", "id")),
               Join(
                   _eq(_fp("proc", "organization_id"), _fp("court", "envelope", "id")),
                   _t("proceeding", "proc"), _t("organization", "court")),
               _t("event", "ev")),
           where=And((
               _eq(_fp("proc", "proceeding_type"), "case"),
               _eq(_fp("court", "jurisdiction"), "ca7"),
               Between(_fp("ev", "date"), "2008-01-01", "2020-12-31"),
               Fuzzy(_fp("ev", "description"),
                     "the case was appealed to or reviewed by the Supreme Court"))),
           group_by=(), limit=10)),

    ("Fifth Circuit 2021 voting rights cases.",
     Query(select=(_fp("doc", "envelope", "id"), _fp("doc", "title")),
           source=Join(
               _eq(_fp("doc", "issuing_body_id"), _fp("court", "envelope", "id")),
               _t("document", "doc"), _t("organization", "court")),
           where=And((
               _eq(_fp("court", "jurisdiction"), "ca5"),
               Between(_fp("doc", "date_issued"), "2021-01-01", "2021-12-31"),
               Fuzzy(_fp("doc", "summary"), "voting rights"))),
           group_by=(), limit=10)),
]


def few_shots_for(registry: schema.Registry) -> list[tuple[str, Query]]:
    """Return examples that use only the selected ingestion schema."""
    return DATAFORM_FEW_SHOTS if registry.name == "dataform" else FEW_SHOTS


def select_few_shots(question: str, registry: schema.Registry,
                     limit: int = 2) -> list[tuple[str, Query]]:
    """Choose the few examples whose shape best matches the current question.

    Sending every example costs input latency and can make a small model copy an
    irrelevant predicate. Shape pins handle terse/date/court and other distinctive
    requests; lexical overlap fills any remaining slot. Free.
    """
    shots = few_shots_for(registry)
    if len(shots) <= limit:
        return list(shots)

    normalized = question.lower()
    words = set(re.findall(r"[a-z0-9-]+", normalized))
    selected: list[int] = []

    if registry.name == "dataform":
        terse_circuit_year = (
            len(re.findall(r"[a-z][a-z-]+", normalized)) <= 8
            and _requested_circuit(question) is not None
            and bool(re.search(r"\b(?:19|20)\d{2}\b", normalized))
        )
        if terse_circuit_year:
            # This shape is complete on its own. A second, less-relevant example
            # increases both input latency and predicate-copying risk.
            return [shots[-1]]
        if "supreme court" in normalized or "scotus" in normalized:
            selected.append(len(shots) - 2)  # appellate relationship + date range
        if ("opinion" in normalized and re.search(r"\b(since|after|from)\b", normalized)
                and re.search(r"\b(affirm|affirmed|reverse|reversed|deny|denied|"
                              r"dismiss|judgment)\b", normalized)):
            selected.append(3)  # circuit + date + topic + disposition
        if re.search(r"\b(count|how many|number of)\b", normalized):
            selected.append(2)
        if re.search(r"\b(photo|photograph|image|exhibit)\b", normalized):
            selected.append(1)
        if re.search(r"\b(opinion|qualified immunity)\b", normalized):
            selected.append(0)

    ranked = sorted(
        range(len(shots)),
        key=lambda i: (
            len(words & set(re.findall(r"[a-z0-9-]+", shots[i][0].lower()))),
            -i,
        ),
        reverse=True,
    )
    for index in ranked:
        if index not in selected:
            selected.append(index)
        if len(selected) >= limit:
            break
    return [shots[index] for index in selected[:limit]]


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
    for q, ast in select_few_shots(question, registry):
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
The source tree was wrong. Use the minimum tables needed. Every Join has exactly:
`condition`, `left`, and `right`. `condition` is ONE equality Comparison connecting
one alias present in `left` to one alias present in `right`; search filters belong in
Query.where, never Join.condition. Nest joins to the LEFT: join A to B first, then put
that complete Join in the outer `left` and a single TableRef C in the outer `right`.
An inner join may not reference an alias that only exists in an outer join. Count the
braces and ensure every referenced alias is introduced in the same join subtree."""

_WHERE_HINT = """\
The current input is a terse legal search, not permission to omit `where`.
Decompose it before rewriting the complete Query:
  * "Nth Circuit" -> join the court organization and compare jurisdiction to caN
  * a bare year -> an inclusive Jan 1 through Dec 31 Between condition
  * "since/from YEAR" or "YEAR onwards" -> date >= "YEAR-01-01", with NO upper bound
  * the remaining legal topic -> a Fuzzy condition on document text/summary
  * only an explicit case/cases/proceeding request -> proceeding_type = "case"
Include every needed table through a declared join edge. Do not return a Query with
`where` missing or null when the user supplied any search terms."""


def repair_prompt(errors: list[DecodeError], attempt: int) -> str:
    """Hand the model the exact JSON path of every mistake. Free."""
    listed = "\n".join(f"  {e['path']}  [{e['code']}]  {e['message']}" for e in errors[:12])
    codes = {e["code"] for e in errors}
    hint = ""
    if codes & {"table_not_in_scope", "unknown_join_edge", "bad_join", "bad_join_scope",
                "unknown_table"}:
        hint = "\n\n" + _JOIN_SKELETON
    if any(e["path"] == "$.where" for e in errors):
        hint += "\n\n" + _WHERE_HINT
    return (
        f"Your JSON was rejected (attempt {attempt} of {MAX_ATTEMPTS}). Problems:\n{listed}"
        f"{hint}\n\n"
        "Return the corrected JSON object, complete, from scratch. Fix exactly these problems "
        "and change nothing else. Remember: no Visual/Audio/Sem/Sim nodes; comparisons on "
        "SCALAR columns only and Fuzzy on everything else; a DATE column takes a Date object, "
        "not a bare string; Fuzzy.field is one FieldRef or "
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
_CANNOT_EXPRESS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(most recent|latest|newest|oldest|earliest|"
                r"sorted by|ranked by|in order of|chronological)\b", re.I),
     "BQL has no ORDER BY yet, so any LIMIT returns an arbitrary subset rather than "
     "the first ones by the order you asked for."),
]


def cannot_express(question: str) -> list[str]:
    """Warnings for parts of a question the language has no way to say. Free."""
    return [message for pattern, message in _CANNOT_EXPRESS if pattern.search(question)]


_CIRCUIT = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+circuit\b", re.I)
_CIRCUIT_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11,
}
_LOWER_YEAR = re.compile(
    r"(?:\bfrom|\bsince)\s+(19\d{2}|20\d{2})\b|"
    r"\b(19\d{2}|20\d{2})\s+(?:and\s+)?onwards?\b",
    re.I,
)
_AFTER_YEAR = re.compile(r"(?<!not )\bafter\s+(19\d{2}|20\d{2})\b", re.I)
_UPPER_YEAR = re.compile(
    r"(?:\bnot\s+after|\bthrough|\buntil|\bup\s+to)\s+(19\d{2}|20\d{2})\b",
    re.I,
)
_BEFORE_YEAR = re.compile(r"\bbefore\s+(19\d{2}|20\d{2})\b", re.I)
_ANY_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SUPREME_REVIEW = re.compile(
    r"(?:\b(?:went|go(?:es)?)\s+up\s+to\s+(?:the\s+)?supreme\s+court\b|"
    r"\b(?:appeal(?:ed)?\s+to|review(?:ed)?\s+by)\s+(?:the\s+)?supreme\s+court\b|"
    r"\b(?:the\s+)?supreme\s+court\s+review\s+of\b|"
    r"\breview(?:ed)?\s+by\s+scotus\b|\bscotus\s+review\b)",
    re.I,
)
_TERSE_STOP = frozenset({
    "find", "show", "list", "case", "cases", "court", "courts", "circuit",
    "appeal", "appeals", "appellate", "opinion", "opinions", "document",
    "documents", "from", "after", "before", "since", "through", "until",
    "with", "about", "that", "went", "not", "and", "the", "of", "in",
})


def _requested_circuit(question: str) -> tuple[int, str] | None:
    """Return a requested federal circuit written as either ``6th`` or ``Sixth``."""
    numeric = _CIRCUIT.search(question)
    if numeric:
        return int(numeric.group(1)), numeric.group(0)
    for word, number in _CIRCUIT_WORDS.items():
        written = re.search(rf"\b{word}\s+circuit\b", question, re.I)
        if written:
            return number, written.group(0)
    return None


def _year(match: re.Match[str] | None) -> str | None:
    return next((group for group in match.groups() if group), None) if match else None


def _question_for_model(question: str, registry: schema.Registry) -> str:
    """Remove relationships that the compiler can attach more reliably than the model."""
    if registry.name != "dataform" or not _SUPREME_REVIEW.search(question):
        return question
    simplified = re.sub(
        r"\b(?:the\s+)?supreme\s+court\s+review\s+of\s+", "", question,
        flags=re.I,
    )
    simplified = re.sub(
        r"\b(?:that\s+)?(?:went|go(?:es)?)\s+up\s+to\s+(?:the\s+)?supreme\s+court\b",
        "", simplified, flags=re.I,
    )
    simplified = re.sub(
        r"\b(?:that\s+were\s+)?(?:appeal(?:ed)?\s+to|review(?:ed)?\s+by)\s+"
        r"(?:the\s+)?supreme\s+court\b",
        "", simplified, flags=re.I,
    )
    simplified = re.sub(r"\bscotus\s+review\s+of\s+", "", simplified, flags=re.I)
    simplified = re.sub(r"\b(?:that\s+were\s+)?review(?:ed)?\s+by\s+scotus\b",
                        "", simplified, flags=re.I)
    simplified = re.sub(r"\s+", " ", simplified).strip(" ,.-")
    # Ask the model only for the document-level filters. The compiler attaches the
    # proceeding/event relationship below; this avoids eliciting the malformed nested
    # joins that Lightning repeatedly produced for the word "cases".
    simplified = re.sub(r"\bcases?\b", "opinions", simplified, flags=re.I)
    return simplified or question


def _requested_topic(question: str) -> str | None:
    """Extract an explicitly delimited legal topic, without trying to parse prose."""
    introduced = re.search(
        r"\b(?:about|involving|discussing|concerning)\s+(.+?)"
        r"(?:\s+(?:from|since|after|before|through|reviewed|appealed|that|which)\b|[.?!]|$)",
        question, re.I,
    )
    candidate = introduced.group(1) if introduced else None
    if candidate is None:
        before_noun = re.search(
            r"\bcircuit\s+(.+?)\s+(?:opinions?|cases?|decisions?|documents?)\b",
            question, re.I,
        )
        candidate = before_noun.group(1) if before_noun else None
    if candidate is None:
        return None
    words = [word for word in re.findall(r"[a-z][a-z-]+", candidate.lower())
             if word not in _TERSE_STOP and word not in _CIRCUIT_WORDS]
    return " ".join(words).replace("-", " ") if words else None


def _walk_objects(value: Any):
    """Yield every object in a JSON-shaped value. Free."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def _has_range_bound(query: dict[str, Any], year: str, *, upper: bool) -> bool:
    """Whether a date constraint uses `year` in the requested direction. Free."""
    for node in _walk_objects(query):
        kind = node.get("kind")
        if kind == "Between":
            endpoint = node.get("high" if upper else "low")
            if year in str(endpoint):
                return True
        if kind != "Comparison":
            continue
        op = node.get("op")
        if op not in ({"<", "<="} if upper else {">", ">="}):
            continue
        if year in str(node.get("field2")):
            return True
    return False


def _is_date_field(value: Any) -> bool:
    """Whether a wire FieldRef names a date-like field."""
    if not isinstance(value, dict) or value.get("kind") != "FieldRef":
        return False
    path = value.get("path")
    return bool(isinstance(path, list) and path and (
        path[-1] in {"date", "date_filed", "date_issued", "date_start", "date_end",
                     "effective_date", "updated_at"}
        or path[-1].endswith("_date")
    ))


def _upper_date_bounds(query: dict[str, Any]) -> list[Any]:
    """Return explicit upper bounds placed on date fields."""
    bounds: list[Any] = []
    for node in _walk_objects(query):
        if node.get("kind") == "Between" and _is_date_field(node.get("field")):
            bounds.append(node.get("high"))
        elif (node.get("kind") == "Comparison" and node.get("op") in {"<", "<="}
              and _is_date_field(node.get("field1"))):
            bounds.append(node.get("field2"))
    return bounds


def semantic_coverage_errors(question: str, query: dict[str, Any]) -> list[DecodeError]:
    """Catch obvious constraint loss that schema/type validation cannot see.

    This is intentionally narrow: jurisdiction, Supreme Court, and explicit year
    bounds are high-signal literals. A syntax-valid query that substitutes one of
    them is worse than a repair attempt. Free.
    """
    rendered = json.dumps(query, sort_keys=True).lower()
    errors: list[DecodeError] = []

    circuit = _requested_circuit(question)
    if circuit:
        expected_number, circuit_label = circuit
        expected = f"ca{expected_number}"
        if expected not in rendered:
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                f'the question requires {circuit_label!r}; include jurisdiction "{expected}"',
            ))
        wrong_numbers = {
            int(value) for value in re.findall(r'"ca(\d{1,2})"', rendered)
            if int(value) != expected_number
        }
        wrong_numbers.update(
            number for word, number in _CIRCUIT_WORDS.items()
            if number != expected_number and f"{word} circuit" in rendered
        )
        wrong = sorted(wrong_numbers)
        if wrong:
            errors.append(DecodeError(
                "$.where", "copied_question_constraint",
                f"the query substituted circuit value(s) {', '.join('ca' + str(x) for x in wrong)}; "
                f'use only {expected} for the requested circuit',
            ))

    if _SUPREME_REVIEW.search(question):
        if "supreme court" not in rendered and "scotus" not in rendered:
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                "the question requires Supreme Court review; represent that relationship in a condition",
            ))

    lower = _LOWER_YEAR.search(question)
    after = _AFTER_YEAR.search(question)
    if lower:
        year = _year(lower)
        assert year is not None
        if not _has_range_bound(query, year, upper=False):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                f"the question sets {year} as a lower date bound; use >= or Between.low",
            ))
    if after:
        year = after.group(1)
        if not _has_range_bound(query, year, upper=False):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                f"the question says after {year}; use > \"{year}-12-31\"",
            ))

    upper = _UPPER_YEAR.search(question)
    before = _BEFORE_YEAR.search(question)
    if upper:
        year = upper.group(1)
        if not _has_range_bound(query, year, upper=True):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                f"the question sets {year} as an upper date bound; use <= or Between.high",
            ))
    if before:
        year = before.group(1)
        if not _has_range_bound(query, year, upper=True):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                f"the question says before {year}; use < \"{year}-01-01\"",
            ))

    if lower and upper is None:
        invented = _upper_date_bounds(query)
        if invented:
            year = _year(lower)
            assert year is not None
            errors.append(DecodeError(
                "$.where", "invented_question_constraint",
                f"the question says since/from {year} and gives no end date; remove the "
                f"invented upper bound {invented[0]!r} and use date >= \"{year}-01-01\"",
            ))

    # A lone year in a terse legal-search fragment means that calendar year, not an
    # unconstrained mention. Require an inclusive range so 2019 cannot become >= 2019.
    years = _ANY_YEAR.findall(question)
    if (len(years) == 1 and lower is None and after is None
            and upper is None and before is None):
        year = years[0]
        if not (_has_range_bound(query, year, upper=False)
                and _has_range_bound(query, year, upper=True)):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                f"the bare year {year} means that calendar year; use a range from "
                f'"{year}-01-01" through "{year}-12-31"',
            ))

    # For short fragments, whatever remains after court/year scaffolding is the legal
    # topic. This deliberately does not run on prose, where paraphrase is expected.
    words = re.findall(r"[a-z][a-z-]+", question.lower())
    if len(words) <= 8 and (circuit is not None or bool(years)):
        ordinal_words = set(_CIRCUIT_WORDS)
        topics = [w for w in words if len(w) >= 4 and w not in _TERSE_STOP
                  and w not in ordinal_words]
        if topics and not any(topic in rendered for topic in topics):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                "the terse search topic was dropped; include a Fuzzy condition for "
                + ", ".join(topics),
            ))

    # These are deliberately high-signal legal phrases. Keeping each concept in a
    # distinct semantic predicate prevents the model from returning a generic court
    # and date query while silently dropping the subject or requested disposition.
    if re.search(r"\bsecurities[- ]fraud\b", question, re.I):
        if not all(word in rendered for word in ("securities", "fraud")):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                'the question requires the topic "securities fraud"; include it in a Fuzzy condition',
            ))
    if re.search(r"\brevers(?:e|ed|ing)\b.{0,32}\bmotion\s+to\s+dismiss\b",
                 question, re.I):
        if not ("revers" in rendered and "motion" in rendered and "dismiss" in rendered):
            errors.append(DecodeError(
                "$.where", "missing_question_constraint",
                'the question requires a reversed motion to dismiss; include that disposition '
                'in a Fuzzy condition',
            ))

    topic = _requested_topic(question)
    if topic and not all(word in rendered for word in topic.split()):
        errors.append(DecodeError(
            "$.where", "missing_question_constraint",
            f'the question requires the legal topic "{topic}"; include it in a Fuzzy condition',
        ))

    asks_for_cases = bool(re.search(r"\b(case|cases|proceeding|proceedings)\b", question, re.I))
    has_unasked_type = "proceeding_type" in rendered
    has_unasked_table = (re.search(r'"name"\s*:\s*"proceeding"', rendered)
                         and not _SUPREME_REVIEW.search(question))
    if not asks_for_cases and (has_unasked_type or has_unasked_table):
        errors.append(DecodeError(
            "$.where", "invented_question_constraint",
            "the question does not ask for cases or proceedings; remove proceeding_type "
            "and any proceeding table joined only for that copied condition",
        ))

    return errors


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


def _repair_wire_typos(value: Any) -> Any:
    """Repair tiny unambiguous JSON-key slips before canonical decoding."""
    if isinstance(value, list):
        return [_repair_wire_typos(item) for item in value]
    if not isinstance(value, dict):
        return value
    fixed = {key: _repair_wire_typos(child) for key, child in value.items()}
    if fixed.get("kind") == "Comparison" and "op" not in fixed and "op=" in fixed:
        fixed["op"] = fixed.pop("op=")
    return fixed


def _reconcile_unknown_aliases(query: Query, registry: schema.Registry) -> Query:
    """Repair an invented alias only when its field has one in-scope owner.

    Small models occasionally write ``org.jurisdiction`` while declaring only
    ``document as doc``. If exactly one declared table owns that field path, the
    intended alias is certain and a model retry adds latency without information.
    Ambiguous references are deliberately left untouched for normal validation.
    """
    aliases: dict[str, str] = {}

    def collect(source: TableRef | Join) -> None:
        if isinstance(source, TableRef):
            aliases[source.alias] = source.name
        else:
            collect(source.left)
            collect(source.right)

    collect(query.source)

    def field(ref: FieldRef) -> FieldRef:
        if ref.source in aliases or not ref.path:
            return ref
        # Models sometimes use the table name where the declared alias is required,
        # e.g. organization.envelope.id beside `organization as court`.
        table_matches = [alias for alias, table in aliases.items() if table == ref.source]
        if len(table_matches) == 1:
            return replace(ref, source=table_matches[0])
        suffix = ".".join(ref.path)
        candidates = [
            alias for alias, table in aliases.items()
            if registry.has(f"{table}.{suffix}")
        ]
        return replace(ref, source=candidates[0]) if len(candidates) == 1 else ref

    def expression(expr: Any) -> Any:
        if isinstance(expr, FieldRef):
            return field(expr)
        if isinstance(expr, Unnest):
            return replace(expr, ref=field(expr.ref))
        if isinstance(expr, Aggregator) and expr.arg is not None:
            return replace(expr, arg=expression(expr.arg))
        return expr

    def condition(value: Any) -> Any:
        if isinstance(value, (And, Or)):
            return replace(value, children=tuple(condition(child) for child in value.children))
        if isinstance(value, Not):
            return replace(value, child=condition(value.child))
        if isinstance(value, Comparison):
            return replace(value, field1=expression(value.field1),
                           field2=expression(value.field2))
        if isinstance(value, Fuzzy):
            return replace(value, field=expression(value.field))
        if isinstance(value, (InList, Between, Like)):
            return replace(value, field=field(value.field))
        return value

    def source(value: TableRef | Join) -> TableRef | Join:
        if isinstance(value, TableRef):
            return value
        return replace(value, condition=condition(value.condition),
                       left=source(value.left), right=source(value.right))

    return replace(
        query,
        select=tuple(expression(expr) for expr in query.select),
        source=source(query.source),
        where=condition(query.where) if query.where is not None else None,
        group_by=tuple(expression(expr) for expr in query.group_by),
    )


def check_payload(obj: Any, registry: schema.Registry) -> tuple[Query | None, list[DecodeError]]:
    """Decode then validate. Returns (query, errors); query is None if it did not decode."""
    obj = _repair_wire_typos(obj)
    errors = serde.decode_errors(obj)
    if errors:
        return None, errors
    query = _reconcile_unknown_aliases(serde.decode(obj), registry)
    return query, checks.validate(query, registry)


def _reconcile_question_constraints(question: str, query: Query) -> Query:
    """Apply only corrections whose intended value follows directly from the question."""
    circuit = _requested_circuit(question)
    lower = _LOWER_YEAR.search(question)
    after = _AFTER_YEAR.search(question)
    upper = _UPPER_YEAR.search(question)
    before = _BEFORE_YEAR.search(question)
    lower_year = _year(lower)
    after_year = _year(after)
    upper_year = _year(upper)
    before_year = _year(before)
    years = _ANY_YEAR.findall(question)
    bare_year = (years[0] if len(years) == 1 and not any((lower, after, upper, before))
                 else None)
    requested_date = any((lower_year, after_year, upper_year, before_year, bare_year))
    expected_jurisdiction = f"ca{circuit[0]}" if circuit else None
    asks_for_cases = bool(re.search(r"\b(case|cases|proceeding|proceedings)\b", question, re.I))

    def table_aliases(value: TableRef | Join) -> dict[str, str]:
        if isinstance(value, TableRef):
            return {value.name: value.alias}
        return table_aliases(value.left) | table_aliases(value.right)

    initial_aliases = table_aliases(query.source)
    document_alias = initial_aliases.get("document")

    def is_path(ref: Any, leaf: str) -> bool:
        return isinstance(ref, FieldRef) and bool(ref.path) and ref.path[-1] == leaf

    date_leaves = {
        "date", "date_filed", "date_issued", "date_start", "date_end",
        "effective_date", "updated_at",
    }

    def date_replacement(ref: FieldRef) -> Any:
        if lower_year and upper_year:
            return Between(ref, f"{lower_year}-01-01", f"{upper_year}-12-31")
        if lower_year:
            return Comparison(Op.GE, ref, f"{lower_year}-01-01")
        if after_year:
            return Comparison(Op.GT, ref, f"{after_year}-12-31")
        if upper_year:
            return Comparison(Op.LE, ref, f"{upper_year}-12-31")
        if before_year:
            return Comparison(Op.LT, ref, f"{before_year}-01-01")
        if bare_year:
            return Between(ref, f"{bare_year}-01-01", f"{bare_year}-12-31")
        return None

    def condition(value: Any, *, removable: bool = False) -> Any:
        if isinstance(value, And):
            children = [condition(child, removable=True) for child in value.children]
            unique: list[Any] = []
            for child in children:
                if child is not None and child not in unique:
                    unique.append(child)
            return replace(value, children=tuple(unique))
        if isinstance(value, Or):
            return replace(value, children=tuple(condition(child) for child in value.children))
        if isinstance(value, Not):
            return replace(value, child=condition(value.child))
        if isinstance(value, Comparison):
            if (expected_jurisdiction and value.op is Op.EQ
                    and is_path(value.field1, "jurisdiction")
                    and isinstance(value.field2, str)):
                jurisdiction_ref = (FieldRef(document_alias, ("jurisdiction",))
                                    if document_alias else value.field1)
                return replace(value, field1=jurisdiction_ref,
                               field2=expected_jurisdiction)
            if (removable and not asks_for_cases and value.op is Op.EQ
                    and is_path(value.field1, "proceeding_type")
                    and str(value.field2).lower() == "case"):
                return None
            if (isinstance(value.field1, FieldRef) and value.field1.path
                    and value.field1.path[-1] in date_leaves and requested_date):
                return date_replacement(value.field1) or value
            return value
        if (isinstance(value, Between) and value.field.path
                and value.field.path[-1] in date_leaves and requested_date):
            return date_replacement(value.field) or value
        return value

    where = condition(query.where) if query.where is not None else None

    def aliases_in(value: Any) -> set[str]:
        if isinstance(value, FieldRef):
            return {value.source}
        if isinstance(value, Unnest):
            return aliases_in(value.ref)
        if isinstance(value, Aggregator):
            return aliases_in(value.arg) if value.arg is not None else set()
        if isinstance(value, Comparison):
            return aliases_in(value.field1) | aliases_in(value.field2)
        if isinstance(value, (InList, Between, Like)):
            return aliases_in(value.field)
        if isinstance(value, Fuzzy):
            return aliases_in(value.field)
        if isinstance(value, (And, Or)):
            return set().union(*(aliases_in(child) for child in value.children), set())
        if isinstance(value, Not):
            return aliases_in(value.child)
        if isinstance(value, (tuple, list)):
            return set().union(*(aliases_in(item) for item in value), set())
        return set()

    aliases = table_aliases(query.source)

    def add_condition(current: Any, extra: Any) -> Any:
        if current is None:
            return extra
        if isinstance(current, And):
            return replace(current, children=(*current.children, extra))
        return And((current, extra))

    def date_ref() -> FieldRef | None:
        for table, leaf in (("document", "date_issued"),
                            ("proceeding", "date_filed"), ("event", "date")):
            if table in aliases:
                return FieldRef(aliases[table], (leaf,))
        return None

    def has_date_condition(value: Any) -> bool:
        if isinstance(value, Comparison):
            return (isinstance(value.field1, FieldRef) and bool(value.field1.path)
                    and value.field1.path[-1] in date_leaves)
        if isinstance(value, Between):
            return bool(value.field.path) and value.field.path[-1] in date_leaves
        if isinstance(value, (And, Or)):
            return any(has_date_condition(child) for child in value.children)
        if isinstance(value, Not):
            return has_date_condition(value.child)
        return False

    if requested_date and not has_date_condition(where):
        ref = date_ref()
        if ref is not None:
            where = add_condition(where, date_replacement(ref))

    used = aliases_in(query.select) | aliases_in(where) | aliases_in(query.group_by)
    source = query.source
    # Prune only the unambiguous one-hop case. Nested joins can contain bridge tables
    # that are not selected directly but are still required to reach another source.
    if isinstance(source, Join) and isinstance(source.left, TableRef) and isinstance(source.right, TableRef):
        if source.left.alias in used and source.right.alias not in used:
            source = source.left
        elif source.right.alias in used and source.left.alias not in used:
            source = source.right

    aliases = table_aliases(source)

    def has_leaf(value: Any, leaf: str) -> bool:
        if isinstance(value, FieldRef):
            return bool(value.path) and value.path[-1] == leaf
        if isinstance(value, Unnest):
            return has_leaf(value.ref, leaf)
        if isinstance(value, Aggregator):
            return value.arg is not None and has_leaf(value.arg, leaf)
        if isinstance(value, Comparison):
            return has_leaf(value.field1, leaf) or has_leaf(value.field2, leaf)
        if isinstance(value, (InList, Between, Like)):
            return has_leaf(value.field, leaf)
        if isinstance(value, Fuzzy):
            return has_leaf(value.field, leaf)
        if isinstance(value, (And, Or)):
            return any(has_leaf(child, leaf) for child in value.children)
        if isinstance(value, Not):
            return has_leaf(value.child, leaf)
        return False

    if expected_jurisdiction and not has_leaf(where, "jurisdiction"):
        jurisdiction_ref: FieldRef | None = None
        if "document" in aliases:
            jurisdiction_ref = FieldRef(aliases["document"], ("jurisdiction",))
        elif "organization" in aliases:
            jurisdiction_ref = FieldRef(aliases["organization"], ("jurisdiction",))
        elif "proceeding" in aliases:
            org_alias = "court" if "court" not in aliases.values() else "review_court"
            source = Join(
                Comparison(Op.EQ, FieldRef(aliases["proceeding"], ("organization_id",)),
                           FieldRef(org_alias, ("envelope", "id"))),
                source, TableRef("organization", org_alias),
            )
            jurisdiction_ref = FieldRef(org_alias, ("jurisdiction",))
        if jurisdiction_ref is not None:
            where = add_condition(
                where, Comparison(Op.EQ, jurisdiction_ref, expected_jurisdiction),
            )

    # Explicitly delimited topics and terse court/year fragments are mechanically
    # extractable even when the model drops them during a repair.
    words = re.findall(r"[a-z][a-z-]+", question.lower())
    topic = _requested_topic(question)
    if len(words) <= 8 and (circuit is not None or bool(years)):
        topics = [word for word in words if len(word) >= 4 and word not in _TERSE_STOP
                  and word not in _CIRCUIT_WORDS]
        topic = topic or " ".join(topics).replace("-", " ")
    if topic:
        rendered_where = repr(where).lower()
        if not all(word in rendered_where for word in topic.split()):
            topic_ref: FieldRef | None = None
            aliases = table_aliases(source)
            if "document" in aliases:
                topic_ref = FieldRef(aliases["document"], ("summary",))
            elif "proceeding" in aliases:
                topic_ref = FieldRef(aliases["proceeding"], ("title",))
            elif "event" in aliases:
                topic_ref = FieldRef(aliases["event"], ("description",))
            if topic_ref is not None:
                where = add_condition(where, Fuzzy(topic_ref, topic))

    # Supreme Court review is a relationship through the proceeding's event stream.
    # Build it locally so the model never has to balance this nested Join JSON.
    if _SUPREME_REVIEW.search(question):
        aliases = table_aliases(source)
        proc_alias = aliases.get("proceeding")
        event_alias = aliases.get("event")
        doc_alias = aliases.get("document")
        if proc_alias is None and doc_alias is not None:
            proc_alias = "proc" if "proc" not in aliases.values() else "review_proc"
            source = Join(
                Comparison(Op.EQ, FieldRef(doc_alias, ("proceeding_id",)),
                           FieldRef(proc_alias, ("envelope", "id"))),
                source, TableRef("proceeding", proc_alias),
            )
        if event_alias is None and proc_alias is not None:
            event_alias = "ev" if "ev" not in aliases.values() else "review_ev"
            source = Join(
                Comparison(Op.EQ, FieldRef(proc_alias, ("envelope", "id")),
                           FieldRef(event_alias, ("proceeding_id",))),
                source, TableRef("event", event_alias),
            )
        if event_alias is not None and "supreme court" not in repr(where).lower():
            where = add_condition(
                where,
                Fuzzy(FieldRef(event_alias, ("description",)),
                      "the case was appealed to or reviewed by the Supreme Court"),
            )

    return replace(query, source=source, where=where)


# --------------------------------------------------------------------------- #
# The compile
# --------------------------------------------------------------------------- #
def compile_question(question: str, *, registry: schema.Registry | None = None,
                     use_cache: bool = True, model: str | None = None,
                     max_attempts: int = MAX_ATTEMPTS) -> CompileResult:
    """Compile a question into a BQL query. Never raises for a bad model answer.

    On success `.query` is the wire form for the optimizer and `.ast` is the
    decoded Query. On failure `.errors` says what was wrong and `.attempts`
    records every round trip. A transport failure (no key, endpoint down) does
    raise, because that is an operator problem rather than a query problem.

    Cost: zero calls on a cache hit, otherwise 1..max_attempts compiler calls.
    """
    t0 = time.perf_counter()
    reg = registry or schema.load()
    mdl = model or config.MODEL
    question = " ".join(question.split())

    if use_cache:
        hit = load_cached(question, reg.name)
        if hit is not None:
            ast, errors = check_payload(hit, reg)
            if not errors and ast is not None:
                return CompileResult(ok=True, question=question, query=serde.encode(ast), ast=ast,
                                     model="cache", schema_name=reg.name, cached=True,
                                     latency_ms=(time.perf_counter() - t0) * 1000)

    model_question = _question_for_model(question, reg)
    messages = build_messages(model_question, reg)
    attempts: list[dict[str, Any]] = []
    errors: list[DecodeError] = []

    for attempt in range(1, max_attempts + 1):
        thinking = THINKING
        temperature = TEMPERATURE if attempt == 1 else REPAIR_TEMPERATURE
        resp = client.chat(messages, model=mdl, temperature=temperature,
                           max_tokens=MAX_TOKENS, enable_thinking=thinking,
                           purpose='compile' if attempt == 1 else f'repair {attempt - 1}')
        obj, parse_error = extract_json(resp.text)
        if parse_error is not None:
            ast, errors = None, [DecodeError("$", "invalid_json", parse_error)]
        else:
            ast, errors = check_payload(obj, reg)
            if ast is not None:
                ast = _reconcile_question_constraints(question, ast)
                errors = checks.validate(ast, reg)
            if not errors and ast is not None and obj is not None:
                errors = semantic_coverage_errors(question, serde.encode(ast))

        attempts.append({"attempt": attempt, "ok": not errors, "errors": list(errors),
                         "thinking": thinking, "temperature": temperature,
                         **resp.telemetry(), "raw": resp.text[:2000]})

        if not errors and ast is not None:
            result = CompileResult(ok=True, question=question, query=serde.encode(ast), ast=ast,
                                   attempts=attempts, model=resp.model, schema_name=reg.name,
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
                         warnings=cannot_express(question),
                         latency_ms=(time.perf_counter() - t0) * 1000)


# --------------------------------------------------------------------------- #
# Cache — so the demo does not depend on wifi
# --------------------------------------------------------------------------- #
def cache_key(question: str, schema_name: str) -> str:
    norm = " ".join(question.lower().split()).strip(" .?!")
    return hashlib.sha256(
        f"{CACHE_VERSION}|{schema_name}|{norm}".encode()
    ).hexdigest()[:16]


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
    """Write a successful compile to the cache. Best effort; never fails a compile.

    Same shape as the checked-in `examples/*.json`, so a cache entry can be promoted to
    an example by moving the file.
    """
    if not result.ok or result.query is None:
        return None
    entry = {"bql_version": BQL_VERSION, "schema": result.schema_name,
             "question": result.question, "query": result.query, "bql": result.printed}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{cache_key(result.question, result.schema_name)}.json"
        path.write_text(json.dumps(entry, indent=2) + "\n")
        return path
    except OSError:
        return None
