"""Direct legal-information answers from hosted Nemotron Super.

Lightning routes explanatory/advisory legal questions here when the BQL record
search compiler is the wrong tool. This path never fabricates a query or invokes
the optimizer/runtime.
"""
from __future__ import annotations

import os
from typing import Callable

from . import client

ANSWER_MODEL = os.environ.get("LEGAL_ANSWER_MODEL", client.SUPER_MODEL)
MAX_TOKENS = int(os.environ.get("LEGAL_ANSWER_MAX_TOKENS", "4096"))
TIMEOUT_S = float(os.environ.get("LEGAL_ANSWER_TIMEOUT_S", str(client.TIMEOUT_S)))
MAX_RETRIES = int(os.environ.get("LEGAL_ANSWER_MAX_RETRIES", "2"))
TEMPERATURE = float(os.environ.get("LEGAL_ANSWER_TEMPERATURE", "0.2"))

SYSTEM_PROMPT = """\
You are Amicus, a careful legal-information assistant. Answer the user's legal
question directly in concise plain text. Give general legal information, not a
database search or BQL query. Distinguish settled rules from uncertainty, say
when jurisdiction or current facts could change the answer, and never invent a
case, quotation, citation, or source. For requests that could affect the user's
rights, explain that the response is general information rather than legal advice
and suggest consulting a qualified lawyer when appropriate. Do not use Markdown.
"""

ChatFn = Callable[..., client.ChatResponse]


def answer_question(question: str, *, chat_fn: ChatFn | None = None) -> client.ChatResponse:
    """Ask Super for a direct legal-information answer."""
    if client.is_mock() and chat_fn is None:
        return client.ChatResponse(
            text=f"Mock legal answer for: {question}",
            model=f"mock:{ANSWER_MODEL}",
            mock=True,
        )
    send = chat_fn or client.chat
    response = send(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        model=ANSWER_MODEL,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        enable_thinking=True,
        timeout_s=TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
    if not response.text.strip():
        raise client.ModelError("the legal answer model returned no answer")
    return response
