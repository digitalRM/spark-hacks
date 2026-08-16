"""Request router — what kind of thing did the user just ask for?

Runs before cache lookup, prompt construction or any compilation. One small call to
`config.ROUTER_MODEL` (hosted Super by default) sorts the input into three routes:

    reject    not a legal request at all; nothing downstream runs
    compile   a record search; goes to the NL -> JSON -> BQL compiler
    answer    a legal question BQL cannot express; answered directly by Super

`route` is the whole decision. `is_legal` is derived from it, not carried alongside it,
so the two can never disagree.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

import config

from . import client

MAX_TOKENS = 256
TIMEOUT_S = 60.0
MAX_RETRIES = 1

REJECTION_MESSAGE = (
    "I'm sorry! It looks like your request isn't related to legal research or "
    "searching court records. Try asking about a case, court, opinion, citation, "
    "law, or legal issue."
)

SYSTEM_PROMPT = """\
# legal_request_router
Route the user's input for a court-record search application.

Use "compile" for requests to find, list, retrieve, count, filter, group, or
compare court cases, opinions, dockets, judges, documents, or other legal records.
Also use "compile" for a recognizable case name, citation, or legal search
fragment that can reasonably be treated as a record search.

Use "answer" for legal questions that ask for an explanation, definition,
interpretation, summary, general legal information, practical legal guidance, or
drafting rather than asking to retrieve records. These are legal, but a database
query compiler cannot answer them well. The compiler can select, join, filter,
fuzzy-search, count, group, and limit records. It cannot write prose explanations,
summarize holdings, give advice, draft documents, reach legal conclusions, or
reliably sort/rank records by latest, newest, oldest, or best. If any essential
part of the request needs one of those unsupported abilities, use "answer" even
when the request also mentions cases or records.

Use "reject" when the words contain no affirmative legal meaning. Greetings,
acknowledgements, casual conversation, vague requests, arbitrary names, recipes,
entertainment, weather, shopping, and non-legal general knowledge are rejected.
Do not infer legal intent merely because this is a legal application.

Examples:
"hi" -> {"route": "reject"}
"can you help me?" -> {"route": "reject"}
"Find cases about qualified immunity after 2020" -> {"route": "compile"}
"Roe v. Wade" -> {"route": "compile"}
"What is qualified immunity?" -> {"route": "answer"}
"Can my landlord evict me without notice?" -> {"route": "answer"}
"Summarize the holdings of cases about noncompetes" -> {"route": "answer"}
"What are the five latest privacy cases?" -> {"route": "answer"}

Output exactly one JSON object and nothing else: {"route": "reject"|"compile"|"answer"}.
"""

Route = Literal["reject", "compile", "answer"]
ROUTES: frozenset[str] = frozenset(("reject", "compile", "answer"))


@dataclass(frozen=True)
class RelevanceResult:
    route: Route
    model: str
    latency_ms: float = 0.0

    @property
    def is_legal(self) -> bool:
        return self.route != "reject"


def decode(text: str) -> Route:
    """Pull the route out of the router's reply, or say why it is unusable."""
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        raise client.ModelError("the router returned no JSON object")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise client.ModelError("the router returned invalid JSON") from exc
    route = payload.get("route") if isinstance(payload, dict) else None
    if route not in ROUTES:
        raise client.ModelError('the router must return route "reject", "compile" or "answer"')
    return route


def classify(question: str) -> RelevanceResult:
    """Route one question. Cost: one call to the router model."""
    if client.is_mock():
        return RelevanceResult(_mock_route(question), "mock:router")

    response = client.chat(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": question}],
        model=config.ROUTER_MODEL,
        temperature=0.0,
        max_tokens=MAX_TOKENS,
        enable_thinking=False,
        timeout_s=TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
    return RelevanceResult(decode(response.text), response.model, response.latency_ms)


# Legal-sounding words, and the openers that ask for prose rather than records. Used
# only under AMICUS_MOCK=1, so the reject and answer panels can be built offline.
_LEGAL_TERMS = (" law", "legal", "court", "case", "opinion", "docket", "judge", "citation",
                "statute", "regulation", "lawsuit", "plaintiff", "defendant", "appeal",
                "hearing", "trial", "evidence", "contract", "injunction", "liability",
                "immunity", " v. ")
_ASKS_FOR_PROSE = ("what is ", "what does ", "why ", "explain ", "define ", "summarize ",
                   "can i ", "can my ", "is it legal ", "is a ", "are ", "should i ", "draft ")


def _mock_route(question: str) -> Route:
    normalized = " ".join(question.lower().split())
    legal = (re.search(r"\b\d+\s+[A-Z][A-Za-z. ]+\s+\d+\b", question)
             or any(term in f" {normalized} " for term in _LEGAL_TERMS))
    if not legal:
        return "reject"
    return "answer" if normalized.startswith(_ASKS_FOR_PROSE) else "compile"
