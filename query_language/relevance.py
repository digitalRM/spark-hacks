"""Fast request router powered by local Nemotron Lightning on Spark :8001.

This runs before cache lookup, prompt construction, or hosted Super compilation.
Its small wire contract separates record searches, direct legal questions, and
unrelated requests before any expensive hosted-model work.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Literal

from . import client

ENABLED = os.environ.get("BQL_RELEVANCE_ENABLED", "1").lower() in {
    "1", "true", "yes", "on",
}
MAX_TOKENS = int(os.environ.get("BQL_RELEVANCE_MAX_TOKENS", "48"))
TIMEOUT_S = float(os.environ.get("BQL_RELEVANCE_TIMEOUT_S", "3"))
MAX_RETRIES = int(os.environ.get("BQL_RELEVANCE_MAX_RETRIES", "1"))
REJECTION_MESSAGE = os.environ.get(
    "BQL_RELEVANCE_MESSAGE",
    "I'm sorry! It looks like your request isn't related to legal research or "
    "searching court records. Try asking about a case, court, opinion, citation, "
    "law, or legal issue.",
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
"hi" -> {"is_legal": false, "route": "reject"}
"can you help me?" -> {"is_legal": false, "route": "reject"}
"Find cases about qualified immunity after 2020" -> {"is_legal": true, "route": "compile"}
"Roe v. Wade" -> {"is_legal": true, "route": "compile"}
"What is qualified immunity?" -> {"is_legal": true, "route": "answer"}
"Can my landlord evict me without notice?" -> {"is_legal": true, "route": "answer"}
"Summarize the holdings of cases about noncompetes" -> {"is_legal": true, "route": "answer"}
"What are the five latest privacy cases?" -> {"is_legal": true, "route": "answer"}

Output exactly one JSON object and nothing else. "route" must be "reject",
"compile", or "answer". "is_legal" must be false only for "reject".
"""

ChatFn = Callable[..., client.ChatResponse]
RequestRoute = Literal["reject", "compile", "answer"]


@dataclass(frozen=True)
class RelevanceResult:
    is_legal: bool
    model: str
    latency_ms: float = 0.0
    route: RequestRoute = "compile"

    def __post_init__(self) -> None:
        # Preserve the old convenient RelevanceResult(False, model) test/injection
        # boundary while keeping route and boolean impossible to contradict.
        if not self.is_legal and self.route == "compile":
            object.__setattr__(self, "route", "reject")
        elif self.is_legal != (self.route != "reject"):
            raise ValueError("is_legal must be false only for the reject route")


def _decode(text: str) -> tuple[bool, RequestRoute]:
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        raise client.ModelError("the relevance checker returned no JSON object")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise client.ModelError("the relevance checker returned invalid JSON") from exc
    is_legal = payload.get("is_legal") if isinstance(payload, dict) else None
    route = payload.get("route") if isinstance(payload, dict) else None
    if not isinstance(is_legal, bool):
        raise client.ModelError(
            'the relevance checker must return a boolean "is_legal" field'
        )
    if route not in {"reject", "compile", "answer"}:
        raise client.ModelError(
            'the relevance checker must return route "reject", "compile", or "answer"'
        )
    if is_legal != (route != "reject"):
        raise client.ModelError("the relevance checker returned contradictory fields")
    return is_legal, route


def classify(question: str, *, chat_fn: ChatFn | None = None) -> RelevanceResult:
    """Classify one question before it reaches any NL→JSON compiler work."""
    if not ENABLED:
        return RelevanceResult(True, "disabled", route="compile")
    if client.is_mock() and chat_fn is None:
        route = _mock_route(question)
        return RelevanceResult(route != "reject", "mock:relevance", route=route)

    send = chat_fn or client.chat
    print('sending from relevance')
    response = send(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        model=client.RELEVANCE_MODEL,
        temperature=0.0,
        max_tokens=MAX_TOKENS,
        enable_thinking=False,
        timeout_s=TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
    is_legal, route = _decode(response.text)
    return RelevanceResult(
        is_legal, response.model, response.latency_ms, route=route,
    )


def _mock_route(question: str) -> RequestRoute:
    """Deterministic three-way router used only under BQL_MOCK=1."""
    if not _mock_is_legal(question):
        return "reject"
    normalized = " ".join(question.lower().split())
    answer_starts = (
        "what is ", "what does ", "why ", "explain ", "define ",
        "summarize ", "can i ", "can my ", "is it legal ",
        "is a ", "are ", "should i ", "draft ",
    )
    return "answer" if normalized.startswith(answer_starts) else "compile"


def _mock_is_legal(question: str) -> bool:
    """Small deterministic stand-in used only under BQL_MOCK=1."""
    normalized = f" {question.lower()} "
    terms = (
        " law", "legal", "court", "case", "opinion", "docket", "judge",
        "citation", "statute", "regulation", "lawsuit", "plaintiff",
        "defendant", "appeal", "hearing", "trial", "evidence", "contract",
        "injunction", "liability", "immunity", " v. ",
    )
    citation = re.search(r"\b\d+\s+[A-Z][A-Za-z. ]+\s+\d+\b", question)
    return bool(citation or any(term in normalized for term in terms))
