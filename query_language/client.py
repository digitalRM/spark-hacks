"""One chat client: the OpenAI protocol, one endpoint, one key.

Every model Amicus talks to -- the router, the compiler, the direct legal answer -- is
hosted Nemotron reached through the official ``openai`` SDK against NVIDIA's
OpenAI-compatible API. There is no second dialect, no local/remote routing table and no
availability probing: a model that is down is an error from the call that needed it.

Responses are streamed and reasoning deltas are discarded, so the JSON
decoder/validator/repair loop in `compiler.py` sees exactly the final content.

Offline: AMICUS_MOCK=1 returns a canned response, so the compiler, its repair loop and
the tests all run with no box and no wifi. Mock output is never a measurement.

Cost: one HTTP round trip per call.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import config

# Generation settings. Not environment variables: they are properties of the prompt and
# the model, tuned here, not per machine.
TEMPERATURE = 1.0
TOP_P = 0.95
MAX_TOKENS = 16384
CONTEXT_TOKENS = 32768          # what we budget the conversation against
REASONING_BUDGET = 16384
ENABLE_THINKING = True          # a reasoning model thinks out loud unless told not to
TIMEOUT_S = 300.0
MAX_RETRIES = 3


class ModelError(RuntimeError):
    """The endpoint could not be reached, or returned something unusable."""


@dataclass
class ChatResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    mock: bool = False
    dropped_shots: int = 0  # few-shots sacrificed to fit the context window
    thought: bool = False   # the model reasoned anyway: the switch did not take


def is_mock() -> bool:
    return config.MOCK


def api_key() -> str:
    if not config.API_KEY and not is_mock():
        raise ModelError(
            f"{config.BASE_URL} needs a key, and NVIDIA_API_KEY is not set.\n"
            "    put it in .env               (get one at build.nvidia.com)\n"
            "or run offline:        AMICUS_MOCK=1"
        )
    return config.API_KEY or "MISSING"


def _sdk(timeout_s: float, max_retries: int):
    """The OpenAI client, imported lazily so mock mode needs no SDK installed."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ModelError("the compiler needs the OpenAI Python SDK; "
                         "install -r requirements.txt") from exc
    return OpenAI(base_url=config.BASE_URL, api_key=api_key(),
                  timeout=timeout_s, max_retries=max_retries)


# --------------------------------------------------------------------------- #
# Context budgeting
# --------------------------------------------------------------------------- #
def estimate_tokens(messages: list[dict[str, str]]) -> int:
    """Rough token count for a conversation. Deliberately pessimistic. Free.

    2.5 characters per token, measured against Super's real tokenizer: a 13,336
    character prompt reported 5,031 prompt tokens, i.e. 2.65 chars/token. The usual
    rule of thumb of 4 would have under-counted by a third, and under-counting is
    the dangerous direction — it means we stop dropping few-shots just before the
    server starts silently truncating the schema.
    """
    return int(sum(len(m.get("content") or "") for m in messages) / 2.5) + 8 * len(messages)


def fit_context(messages: list[dict[str, str]], max_tokens: int,
                budget: int = CONTEXT_TOKENS) -> tuple[list[dict[str, str]], int]:
    """Drop few-shots, oldest first, until the conversation leaves room to answer.

    The system prompt and the actual question are never dropped: without the schema
    the model cannot name a field, and without the question there is nothing to
    compile. Few-shots are the only expendable part, and the first two carry most
    of the signal, so they go last. Returns (messages, how_many_dropped). Free.
    """
    if estimate_tokens(messages) + max_tokens <= budget:
        return messages, 0

    system, tail = messages[:1], messages[1:]
    # Few-shots are the leading user/assistant pairs; everything from the final
    # user turn onward is the live exchange and stays.
    pairs: list[list[dict[str, str]]] = []
    i = 0
    while i + 1 < len(tail) and tail[i]["role"] == "user" and tail[i + 1]["role"] == "assistant":
        pairs.append([tail[i], tail[i + 1]])
        i += 2
    live = tail[i:]

    dropped = 0
    while pairs and estimate_tokens(system + [m for p in pairs for m in p] + live) + max_tokens > budget:
        pairs.pop(0)
        dropped += 1
    return system + [m for p in pairs for m in p] + live, dropped


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #
def chat(messages: list[dict[str, str]], *, model: str | None = None,
         temperature: float = TEMPERATURE, max_tokens: int = MAX_TOKENS,
         enable_thinking: bool = ENABLE_THINKING, timeout_s: float = TIMEOUT_S,
         max_retries: int = MAX_RETRIES) -> ChatResponse:
    """Send one chat completion and return the assistant's text.

    Nemotron may emit private reasoning deltas before its answer. We record only that
    reasoning occurred and never concatenate or print it.

    Cost: one round trip, or free under AMICUS_MOCK=1.
    """
    model = model or config.MODEL
    if is_mock():
        return ChatResponse(text=_mock_reply(messages), model=f"mock:{model}", mock=True)

    messages, dropped = fit_context(messages, max_tokens)
    try:
        sdk = _sdk(timeout_s, max_retries)
        t0 = time.perf_counter()
        stream = sdk.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=TOP_P,
            max_tokens=max_tokens,
            extra_body={
                "chat_template_kwargs": {"enable_thinking": enable_thinking},
                "reasoning_budget": REASONING_BUDGET,
            },
            stream=True,
        )
        content: list[str] = []
        thought = False
        for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            thought = thought or bool(getattr(delta, "reasoning_content", None))
            text = getattr(delta, "content", None)
            if text is not None:
                content.append(text)
        result = ChatResponse(text="".join(content).strip(), model=model,
                            latency_ms=(time.perf_counter() - t0) * 1000,
                            dropped_shots=dropped, thought=thought)
        print(f"latency_ms = {(time.perf_counter() - t0) * 1000}")
        return result
    except ModelError:
        raise
    except Exception as exc:
        # Do not echo request headers or client state: they contain the API key.
        status = getattr(exc, "status_code", None)
        detail = f" (HTTP {status})" if status else ""
        raise ModelError(
            f"{model} failed at {config.BASE_URL}{detail}: {type(exc).__name__}"
        ) from exc


def list_models() -> list[str]:
    """Model ids the endpoint actually serves — use this to verify AMICUS_MODEL.

    Cost: one round trip.
    """
    try:
        return sorted(m.id for m in _sdk(30.0, 1).models.list().data)
    except ModelError:
        raise
    except Exception as exc:
        raise ModelError(f"could not list models at {config.BASE_URL}: "
                         f"{type(exc).__name__}: {exc}") from exc


def _mock_reply(messages: list[dict[str, str]]) -> str:
    """Deterministic offline stand-in.

    A small valid query, enough to exercise the loop's plumbing with no box and no
    wifi. Tests that need specific model behaviour patch `client.chat`.
    """
    def field(source: str, column: str) -> dict:
        return {"kind": "FieldRef", "source": source, "path": [column]}

    def table(name: str) -> dict:
        return {"kind": "TableRef", "name": name, "alias": name}

    if any("# dataform:" in message.get("content", "") for message in messages):
        return json.dumps({
            "kind": "Query",
            "select": [
                {"kind": "FieldRef", "source": "doc", "path": ["envelope", "id"]},
                {"kind": "FieldRef", "source": "doc", "path": ["title"]},
            ],
            "source": {"kind": "TableRef", "name": "document", "alias": "doc"},
            "where": {"kind": "And", "children": [
                {"kind": "Comparison", "op": "=",
                 "field1": {"kind": "FieldRef", "source": "doc",
                            "path": ["envelope", "source_system"]},
                 "field2": "courtlistener"},
                {"kind": "Fuzzy",
                 "field": {"kind": "FieldRef", "source": "doc",
                           "path": ["media", "text", "plain_text"]},
                 "text": "discusses qualified immunity"},
            ]},
            "group_by": [],
            "limit": 10,
        })

    return json.dumps({
        "kind": "Query",
        "select": [field("cluster", "id"), field("cluster", "case_name")],
        "source": {"kind": "Join",
                   "condition": {"kind": "Comparison", "op": "=",
                                 "field1": field("cluster", "docket_id"),
                                 "field2": field("docket", "id")},
                   "left": table("cluster"), "right": table("docket")},
        "where": {"kind": "And", "children": [
            {"kind": "Comparison", "op": "=",
             "field1": field("docket", "court_id"), "field2": "ca9"},
            {"kind": "Fuzzy", "field": field("cluster", "scan_pages"),
             "text": "contains a photographic exhibit"},
        ]},
        "group_by": [],
        "limit": 10,
    })
