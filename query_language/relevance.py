"""Fast legal-domain gate powered by local Nemotron Lightning on Spark :8001.

This runs before cache lookup, prompt construction, or hosted Super compilation.
Its intentionally tiny wire contract makes the policy easy to change without
touching the frontend: ``{"is_legal": true|false}``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable

from . import client

ENABLED = os.environ.get("BQL_RELEVANCE_ENABLED", "1").lower() in {
    "1", "true", "yes", "on",
}
MAX_TOKENS = int(os.environ.get("BQL_RELEVANCE_MAX_TOKENS", "64"))
TIMEOUT_S = float(os.environ.get("BQL_RELEVANCE_TIMEOUT_S", "30"))
MAX_RETRIES = int(os.environ.get("BQL_RELEVANCE_MAX_RETRIES", "2"))
REJECTION_MESSAGE = os.environ.get(
    "BQL_RELEVANCE_MESSAGE",
    "I'm sorry! It looks like your request isn't related to legal research or "
    "searching court records. Try asking about a case, court, opinion, citation, "
    "law, or legal issue.",
)

SYSTEM_PROMPT = """\
# legal_relevance_gate
Decide whether the user's input belongs in a legal-research and court-record
search application.

Return true for requests involving court cases, opinions, dockets, judges,
citations, evidence, hearings, legal doctrines, statutes, regulations, contracts,
legal rights or duties, legal disputes, or searches/filters over legal records.
Short fragments, case names, citations, and plausibly legal follow-up requests
should be true. When genuinely uncertain, prefer true so a valid legal query is
not blocked.

Return false only when the request is clearly unrelated to law or legal research,
such as recipes, entertainment, weather, casual conversation, shopping, or a
general knowledge question with no legal connection.

Output exactly one JSON object and nothing else:
{"is_legal": true}
or
{"is_legal": false}
"""

ChatFn = Callable[..., client.ChatResponse]


@dataclass(frozen=True)
class RelevanceResult:
    is_legal: bool
    model: str
    latency_ms: float = 0.0


def _decode(text: str) -> bool:
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        raise client.ModelError("the relevance checker returned no JSON object")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise client.ModelError("the relevance checker returned invalid JSON") from exc
    value = payload.get("is_legal") if isinstance(payload, dict) else None
    if not isinstance(value, bool):
        raise client.ModelError(
            'the relevance checker must return a boolean "is_legal" field'
        )
    return value


def classify(question: str, *, chat_fn: ChatFn | None = None) -> RelevanceResult:
    """Classify one question before it reaches any NL→JSON compiler work."""
    if not ENABLED:
        return RelevanceResult(True, "disabled")
    if client.is_mock() and chat_fn is None:
        return RelevanceResult(_mock_is_legal(question), "mock:relevance")

    send = chat_fn or client.chat
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
    return RelevanceResult(
        _decode(response.text), response.model, response.latency_ms,
    )


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
