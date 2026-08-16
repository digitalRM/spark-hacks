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
import sys
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
    """One model call, and what it cost.

    A reasoning model spends most of a slow call before it emits its first visible
    character, so the useful split is not "how long did it take" but where the time and
    the tokens went: waiting, thinking, then writing.
    """
    text: str
    model: str
    purpose: str = ""            # route | compile | repair | answer -- which call this was
    tokens_in: int = 0
    tokens_out: int = 0          # everything generated, reasoning included
    reasoning_tokens: int = 0    # the part of tokens_out that was thinking
    reasoning_estimated: bool = False  # ...derived from character share, not reported
    reasoning_chars: int = 0     # the trace is measured, never kept or printed
    latency_ms: float = 0.0      # the whole call
    ttfb_ms: float = 0.0         # request to first chunk: queue + prefill
    thinking_ms: float = 0.0     # first chunk to first content token: the trace
    mock: bool = False
    dropped_shots: int = 0  # few-shots sacrificed to fit the context window
    thought: bool = False   # the model reasoned anyway: the switch did not take

    @property
    def writing_ms(self) -> float:
        """From the first content token to the end: emitting the answer itself."""
        return max(0.0, self.latency_ms - self.ttfb_ms - self.thinking_ms)

    @property
    def answer_tokens(self) -> int:
        return max(0, self.tokens_out - self.reasoning_tokens)

    @property
    def tokens_per_s(self) -> float:
        seconds = (self.thinking_ms + self.writing_ms) / 1000
        return self.tokens_out / seconds if seconds > 0 else 0.0

    def line(self) -> str:
        """One line, dense enough to read a slow call off the server log.

        A `~` on the thinking token count means it was split out by character share
        rather than reported by the endpoint — see `_reasoning_tokens`.
        """
        about = '~' if self.reasoning_estimated else ''
        return (f"{self.purpose or 'chat':<8} {self.model.rsplit('/', 1)[-1]:<28} "
                f"{self.latency_ms / 1000:6.2f}s = wait {self.ttfb_ms / 1000:5.2f}s "
                f"+ think {self.thinking_ms / 1000:6.2f}s + write {self.writing_ms / 1000:5.2f}s"
                f"   in {self.tokens_in:>6,}  out {self.tokens_out:>5,} "
                f"({about}{self.reasoning_tokens:,} thinking, {about}{self.answer_tokens:,} answer)"
                f"  {self.tokens_per_s:5.1f} tok/s")

    def telemetry(self) -> dict[str, float | int | str | bool]:
        """The same numbers, for the stage report and the wire."""
        return {"purpose": self.purpose, "model": self.model,
                "ms": round(self.latency_ms, 1), "ttfb_ms": round(self.ttfb_ms, 1),
                "thinking_ms": round(self.thinking_ms, 1),
                "writing_ms": round(self.writing_ms, 1),
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "reasoning_tokens": self.reasoning_tokens,
                "reasoning_estimated": self.reasoning_estimated,
                "answer_tokens": self.answer_tokens,
                "tokens_per_s": round(self.tokens_per_s, 1),
                "thought": self.thought, "dropped_shots": self.dropped_shots}


def trace(response: ChatResponse) -> None:
    """Print one call's cost to stderr as it happens.

    On by default: every model call is seconds of wall clock the user is waiting
    through, and a server log that does not say where they went is not a log. Set
    AMICUS_TRACE=0 to silence it.
    """
    if config.TRACE:
        print(f"[amicus] {response.line()}", file=sys.stderr, flush=True)


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
def chat(messages: list[dict[str, str]], *, model: str | None = None, purpose: str = "",
         temperature: float = TEMPERATURE, max_tokens: int = MAX_TOKENS,
         enable_thinking: bool = ENABLE_THINKING, timeout_s: float = TIMEOUT_S,
         max_retries: int = MAX_RETRIES) -> ChatResponse:
    """Send one chat completion and return the assistant's text, with its cost measured.

    Nemotron may emit private reasoning deltas before its answer. We time and count that
    trace but never concatenate, keep or print it: `purpose` says which call this was,
    and the returned ChatResponse says where its seconds and tokens went.

    Cost: one round trip, or free under AMICUS_MOCK=1.
    """
    model = model or config.MODEL
    if is_mock():
        return ChatResponse(text=_mock_reply(messages), model=f"mock:{model}",
                            purpose=purpose, mock=True)

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
            # Ask for the trailing usage chunk. Without it a streamed response carries no
            # token counts at all, which is why tokens_in/tokens_out used to read 0.
            stream_options={"include_usage": True},
        )
        content: list[str] = []
        reasoning_chars = 0
        usage = None
        ttfb = thinking = 0.0
        for chunk in stream:
            if not ttfb:
                ttfb = (time.perf_counter() - t0) * 1000
            usage = getattr(chunk, "usage", None) or usage
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            reasoning_chars += len(getattr(delta, "reasoning_content", None) or "")
            text = getattr(delta, "content", None)
            if text is not None:
                if not content:
                    # First visible character. Everything since the first chunk was the
                    # model thinking.
                    thinking = (time.perf_counter() - t0) * 1000 - ttfb
                content.append(text)
        text_out = "".join(content).strip()
        reasoning_tokens, estimated = _reasoning_tokens(usage, reasoning_chars, len(text_out))
        response = ChatResponse(
            text=text_out, model=model, purpose=purpose,
            tokens_in=_usage_tokens(usage, "prompt_tokens"),
            tokens_out=_usage_tokens(usage, "completion_tokens"),
            reasoning_tokens=reasoning_tokens, reasoning_estimated=estimated,
            reasoning_chars=reasoning_chars,
            latency_ms=(time.perf_counter() - t0) * 1000,
            ttfb_ms=ttfb, thinking_ms=thinking,
            dropped_shots=dropped, thought=reasoning_chars > 0)
        trace(response)
        return response
    except ModelError:
        raise
    except Exception as exc:
        # Do not echo request headers or client state: they contain the API key.
        status = getattr(exc, "status_code", None)
        detail = f" (HTTP {status})" if status else ""
        raise ModelError(
            f"{model} failed at {config.BASE_URL}{detail}: {type(exc).__name__}"
        ) from exc


def _usage_tokens(usage, field: str) -> int:
    return int(getattr(usage, field, 0) or 0)


def _reasoning_tokens(usage, reasoning_chars: int, content_chars: int) -> tuple[int, bool]:
    """How much of the output was thinking. Returns (tokens, estimated?).

    NVIDIA's endpoint returns `completion_tokens_details: None` — it counts reasoning in
    `completion_tokens` and never breaks it out — so this is normally derived: split the
    reported completion tokens by the character share of the two streams.

    That deliberately avoids a chars-per-token constant. `estimate_tokens` uses 2.5,
    measured on prompts, and applying it here overstated reasoning by half again
    (263 chars of trace scored 105 tokens against a reported 66 for the whole
    completion). A ratio only assumes the trace and the answer tokenize at roughly the
    same rate, which is far weaker — JSON is denser than prose, so what is left is a
    mild over-attribution to reasoning, and it is reported as an estimate.
    """
    reported = getattr(getattr(usage, "completion_tokens_details", None),
                       "reasoning_tokens", None)
    if reported is not None:
        return int(reported), False
    total_chars = reasoning_chars + content_chars
    completion = _usage_tokens(usage, "completion_tokens")
    if not reasoning_chars:
        return 0, False                       # nothing was streamed as reasoning
    if not completion or not total_chars:
        return int(reasoning_chars / 2.5), True   # no usage at all: fall back to chars
    return round(completion * reasoning_chars / total_chars), True


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
