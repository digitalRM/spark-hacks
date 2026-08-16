"""The `answer` route — a legal question BQL cannot express, answered by Super directly.

The router sends explanatory and advisory questions here. This path never fabricates a
query and never reaches the optimizer or the runtime.
"""
from __future__ import annotations

import config

from . import client

MAX_TOKENS = 4096
TIMEOUT_S = 300.0
MAX_RETRIES = 2
TEMPERATURE = 0.2

SYSTEM_PROMPT = """\
You are Amicus, a careful legal-information assistant. Answer the user's legal
question directly in concise plain text. Give general legal information, not a
database search or BQL query. Distinguish settled rules from uncertainty, say
when jurisdiction or current facts could change the answer, and never invent a
case, quotation, citation, or source. For requests that could affect the user's
rights, explain that the response is general information rather than legal advice
and suggest consulting a qualified lawyer when appropriate. Do not use Markdown.
"""


def answer_question(question: str) -> client.ChatResponse:
    """Ask Super for a direct legal-information answer. Cost: one round trip."""
    if client.is_mock():
        return client.ChatResponse(text=f"Mock legal answer for: {question}",
                                   model=f"mock:{config.MODEL}", mock=True)
    response = client.chat(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": question}],
        model=config.MODEL,
        purpose="answer",
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        enable_thinking=True,
        timeout_s=TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
    if not response.text.strip():
        raise client.ModelError("the legal answer model returned no answer")
    return response
