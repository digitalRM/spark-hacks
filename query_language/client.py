"""Nemotron chat client for compiler and local execution models.

The NL→JSON compiler uses NVIDIA-hosted Nemotron Super through the official
``openai`` Python client and NVIDIA's OpenAI-compatible endpoint. Responses are
streamed, reasoning deltas are deliberately discarded, and only final content is
fed into the JSON decoder/validator/repair loop.

Local llama-server/Ollama support remains for downstream runtime models, but the
compiler no longer probes or silently falls back to a local model.

Offline: BQL_MOCK=1 returns a canned response, so the compiler, its repair loop and
the tests all run with no box and no wifi. Mock output is never a measurement.

Cost: one HTTP round trip per call.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Where the models live
# --------------------------------------------------------------------------- #
SPARK_HOST = os.environ.get("SPARK_HOST", "172.16.94.53")
# Set BQL_LOCAL_BASE_URL only to force every local model through one endpoint.
LOCAL_BASE_URL_OVERRIDE = os.environ.get("BQL_LOCAL_BASE_URL", "")
REMOTE_BASE_URL = os.environ.get("BQL_REMOTE_BASE_URL", "https://integrate.api.nvidia.com/v1")
API_KEY_ENV = "NVIDIA_API_KEY"

OLLAMA, OPENAI = "ollama", "openai"

# Served on the box: model id -> (port, which protocol that server speaks). Anything
# not in here is assumed to be on the hosted endpoint and to need a key.
# :8002-:8006 are stopped on purpose — Super needs 87 GB of the box's 121 GB.
LOCAL_MODELS: dict[str, tuple[int, str]] = {
    "nvidia/nemotron-3.5-lightning": (8001, OPENAI),              # bulk semantic judge
    "nvidia/Nemotron-3-Embed-1B-BF16": (8002, OPENAI),
    "nvidia/llama-nemotron-rerank-1b-v2": (8003, OPENAI),
    "nvidia/NVIDIA-Nemotron-Parse-v1.1": (8004, OPENAI),
    "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4": (8005, OPENAI),
    "nvidia/parakeet-tdt-0.6b-v3": (8006, OPENAI),
}

# Hosted compiler. Preserve this exact model id unless explicitly overridden.
SUPER_MODEL = os.environ.get("SUPER_MODEL", "nvidia/nemotron-3-super-120b-a12b")

# NL -> BQL. Runs once per question and sees only the question and the schema, never
# any case data: the compiler is data-free by construction.
#
# Super is much stronger at structured output and runs on NVIDIA's hosted endpoint.
COMPILER_MODEL = os.environ.get("COMPILER_MODEL", SUPER_MODEL)
FALLBACK_COMPILER_MODEL = os.environ.get("FALLBACK_COMPILER_MODEL", COMPILER_MODEL)
# Cheap local gate that runs before the hosted compiler sees a question.
RELEVANCE_MODEL = os.environ.get("RELEVANCE_MODEL", "nvidia/nemotron-3.5-lightning")

# How much context to ask for. Ollama defaults to 4096 whatever the model supports,
# and silently truncates the front of an over-long prompt, so we always say.
NUM_CTX = int(os.environ.get("BQL_NUM_CTX", "16384"))
CONTEXT_TOKENS = int(os.environ.get("BQL_CONTEXT_TOKENS", "32768"))
MAX_TOKENS = int(os.environ.get("BQL_MAX_TOKENS", "16384"))
TIMEOUT_S = float(os.environ.get("BQL_TIMEOUT_S", "300"))
MAX_RETRIES = int(os.environ.get("BQL_MAX_RETRIES", "3"))
DEFAULT_TEMPERATURE = float(os.environ.get("BQL_TEMPERATURE", "1"))
TOP_P = float(os.environ.get("BQL_TOP_P", "0.95"))
REASONING_BUDGET = int(os.environ.get("BQL_REASONING_BUDGET", "16384"))

# A reasoning model thinks out loud unless told not to, and we want JSON.
ENABLE_THINKING = os.environ.get("BQL_ENABLE_THINKING", "1").lower() in {"1", "true", "yes", "on"}


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
    return os.environ.get("BQL_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}


def is_local(model: str) -> bool:
    return model in LOCAL_MODELS


def api_for(model: str) -> str:
    """Which protocol this model's server speaks. Ollama is not OpenAI-compatible
    enough for our purposes — see the module docstring."""
    return LOCAL_MODELS[model][1] if is_local(model) else OPENAI


def host_for(model: str) -> str:
    """Scheme and host:port, no path."""
    if not is_local(model):
        return REMOTE_BASE_URL.rsplit("/v1", 1)[0]
    if LOCAL_BASE_URL_OVERRIDE:
        return LOCAL_BASE_URL_OVERRIDE.rsplit("/v1", 1)[0]
    return f"http://{SPARK_HOST}:{LOCAL_MODELS[model][0]}"


def base_url_for(model: str) -> str:
    """The OpenAI-style base. Ollama serves one of these too, for `models`."""
    return f"{host_for(model)}/v1"


def is_up(model: str) -> bool:
    """Is this model's server answering right now? One short round trip.

    Used to fall back from Super to Lightning rather than failing a demo because a
    123B model is still loading.
    """
    try:
        served = list_models(model=model)
    except ModelError:
        return False
    # Ollama reports tagged names; accept an untagged request for a tagged model.
    return any(s == model or s.split(":")[0] == model.split(":")[0] for s in served)


def resolve_compiler_model() -> str:
    """Return the configured compiler without probing the hosted service.

    Hosted authentication and availability are checked by the actual completion,
    avoiding a redundant model-list request and any silent local-model fallback.
    """
    if is_mock() or not is_local(COMPILER_MODEL) or COMPILER_MODEL == FALLBACK_COMPILER_MODEL:
        return COMPILER_MODEL
    if is_up(COMPILER_MODEL):
        return COMPILER_MODEL
    return FALLBACK_COMPILER_MODEL


def api_key_for(model: str) -> str:
    """Local llama-server ignores the header; the hosted endpoint requires it."""
    if is_local(model):
        return "local"
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key and not is_mock():
        raise ModelError(
            f"{model} uses the hosted NVIDIA endpoint, and {API_KEY_ENV} is not set.\n"
            f"    export {API_KEY_ENV}=nvapi-...          (get one at build.nvidia.com)\n"
            f"or run offline:        export BQL_MOCK=1"
        )
    return key or "MISSING"


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
         temperature: float = DEFAULT_TEMPERATURE,
         max_tokens: int = MAX_TOKENS, base_url: str | None = None,
         enable_thinking: bool | None = None, timeout_s: float = TIMEOUT_S,
         max_retries: int = MAX_RETRIES) -> ChatResponse:
    """Send a chat completion and return the assistant's text.

    Hosted calls use the official OpenAI SDK against NVIDIA's compatible API and
    consume its streaming response. Local calls retain the small urllib adapters
    needed for llama-server and Ollama.

    Cost: one round trip, or free under BQL_MOCK=1.
    """
    model = model or COMPILER_MODEL
    thinking = ENABLE_THINKING if enable_thinking is None else enable_thinking
    if is_mock():
        return ChatResponse(text=_mock_reply(messages), model=f"mock:{model}", mock=True)

    messages, dropped = fit_context(messages, max_tokens)
    if not is_local(model):
        return _hosted_chat(messages, model=model, temperature=temperature,
                            max_tokens=max_tokens, base_url=base_url,
                            dropped=dropped, enable_thinking=thinking,
                            timeout_s=timeout_s, max_retries=max_retries)

    ollama = api_for(model) == OLLAMA and base_url is None
    if ollama:
        url = f"{host_for(model)}/api/chat"
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            # Ollama ignores chat_template_kwargs. THIS is the switch that works,
            # and only on /api/chat.
            "think": thinking,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                # Ollama defaults to 4096 regardless of the model, and truncates the
                # FRONT of an over-long prompt, which is where the schema lives.
                "num_ctx": NUM_CTX,
            },
        }
    else:
        url = f"{(base_url or base_url_for(model)).rstrip('/')}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": TOP_P,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
    body = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {api_key_for(model)}",
               "Content-Type": "application/json", "Accept": "application/json"}

    last: Exception | None = None
    for attempt in range(1, max_retries + 1):
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode())
            return _response(data, model, ollama, dropped,
                             (time.perf_counter() - t0) * 1000)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:500]
            if e.code in (400, 401, 403, 404):  # bad key, bad model id, bad request
                raise ModelError(f"HTTP {e.code} from {url}: {detail}") from e
            last = ModelError(f"HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            last = ModelError(f"{type(e).__name__}: {e}")
        if attempt < max_retries:
            time.sleep(min(2.0 ** attempt * 0.5, 8.0))
    raise ModelError(f"{model} failed after {max_retries} attempts at {url}: {last}")


def _load_openai():
    """Import lazily so mock mode and local runtime models need no cloud SDK."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ModelError(
            "the hosted compiler needs the OpenAI Python SDK; install "
            "query_language/requirements.txt"
        ) from exc
    return OpenAI


def _hosted_chat(messages: list[dict[str, str]], *, model: str, temperature: float,
                 max_tokens: int, base_url: str | None, dropped: int,
                 enable_thinking: bool, timeout_s: float,
                 max_retries: int) -> ChatResponse:
    """Stream one NVIDIA-hosted OpenAI-compatible completion.

    Nemotron may emit private reasoning deltas before its answer. We record only
    that reasoning occurred and never concatenate or print it; the compiler must
    receive exactly the final JSON content.
    """
    endpoint = (base_url or base_url_for(model)).rstrip("/")
    try:
        sdk = _load_openai()(
            base_url=endpoint,
            api_key=api_key_for(model),
            timeout=timeout_s,
            max_retries=max_retries,
        )
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
        return ChatResponse(
            text="".join(content).strip(), model=model,
            latency_ms=(time.perf_counter() - t0) * 1000,
            dropped_shots=dropped, thought=thought,
        )
    except ModelError:
        raise
    except Exception as exc:
        # Do not echo request headers or client state: they contain the API key.
        status = getattr(exc, "status_code", None)
        detail = f" (HTTP {status})" if status else ""
        raise ModelError(
            f"{model} failed at {endpoint}{detail}: {type(exc).__name__}"
        ) from exc


def _response(data: dict, model: str, ollama: bool, dropped: int, latency_ms: float) -> ChatResponse:
    """Read a reply from either server shape.

    Ollama puts the answer in `message.content` and any thinking in
    `message.thinking`; llama-server uses `choices[0].message`. Either way, if
    `content` is empty the model spent its whole budget reasoning, and reporting
    the trace as if it were the answer would send garbage into the JSON parser —
    so an empty content stays empty and the repair loop sees it as such.
    """
    if ollama:
        message = data.get("message") or {}
        return ChatResponse(
            text=(message.get("content") or "").strip(),
            model=data.get("model", model),
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
            latency_ms=latency_ms, dropped_shots=dropped,
            thought=bool(message.get("thinking")),
        )
    usage = data.get("usage") or {}
    message = data["choices"][0]["message"]
    return ChatResponse(
        text=(message.get("content") or "").strip(),
        model=data.get("model", model),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        latency_ms=latency_ms, dropped_shots=dropped,
        thought=bool(message.get("reasoning") or message.get("reasoning_content")),
    )


def list_models(base_url: str | None = None, model: str | None = None) -> list[str]:
    """Model ids an endpoint actually serves — use this to verify COMPILER_MODEL.

    Cost: one round trip.
    """
    model = model or COMPILER_MODEL
    url = f"{(base_url or base_url_for(model)).rstrip('/')}/models"
    headers = {} if is_local(model) else {"Authorization": f"Bearer {api_key_for(model)}"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ModelError(f"HTTP {e.code} from {url}: {e.read().decode()[:300]}") from e
    except urllib.error.URLError as e:
        raise ModelError(f"could not reach {url}: {e}") from e
    return sorted(m.get("id", "") for m in data.get("data") or [])


# There is no `health()` here on purpose. llama-server serves /health and Ollama
# does not (it 404s), so a single health probe would have to know the dialect —
# and `is_up` already answers the only question worth asking, correctly, for both:
# does this server serve this model right now?


def _mock_reply(messages: list[dict[str, str]]) -> str:
    """Deterministic offline stand-in.

    A small valid query, enough to exercise the loop's plumbing with no box and no
    wifi. Tests that need specific model behaviour inject their own `chat`.
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
