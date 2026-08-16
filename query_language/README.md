# Query language — natural language → JSON → BQL

Step 3 first asks local Nemotron Lightning on Spark `:8001` to route the input.
Court-record searches go to the JSON/BQL compiler, explanatory legal questions
go to hosted Nemotron Super for a direct answer, and unrelated inputs stop before
any hosted call. Compiled JSON is the wire contract defined by `ast.py` and
`serde.py`; BQL text is generated from it by the deterministic pretty-printer,
never accepted directly from the model.

```bash
BQL_MOCK=1 python3 -m api.cli compile \
  --json "CourtListener opinions discussing qualified immunity"

python3 -m api.cli --schema dataform schema
python3 -m unittest discover -s query_language/tests -t .
```

For the live cloud compiler:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r query_language/requirements.txt
export NVIDIA_API_KEY=nvapi-your-rotated-key
.venv/bin/python -m api.cli --schema dataform compile \
  "CourtListener opinions discussing qualified immunity" --json
```

The cloud call uses the OpenAI Python SDK with NVIDIA's OpenAI-compatible base
URL. It streams Nemotron reasoning and content separately: reasoning is never
printed or passed to the parser, while final content enters the JSON
decode/validate/repair loop.

Global CLI options such as `--schema` go before the subcommand:

```bash
python3 -m api.cli --schema dataform compile "Count documents by type" --json
```

## Step-1 handoff

`schemas/dataform.json` maps the canonical Pydantic entities in
`data_ingestion/dataform/models.py` to BQL. Nested entity fields map directly to
the new `FieldRef.path`:

```json
{
  "kind": "FieldRef",
  "source": "doc",
  "path": ["media", "text", "plain_text"]
}
```

Collections use `Unnest` only when the query/result is at element grain. A
plain collection `FieldRef` is quantified at its parent-row grain.

Set `AMICUS_SCHEMA=dataform` in the API process's environment to compile against
this canonical ingestion model. `courtlistener` remains
available for the original flat physical schema.

## Compiler boundary

The compiler emits exact conditions plus `Fuzzy`; it never emits optimizer-only
`Sim`, `Sem`, `Visual`, or `Audio` nodes. Schema metadata tells the optimizer how
to refine each fuzzy predicate.

The v2 wire format adds:

- `TableRef {name, alias}` instead of bare source strings
- `FieldRef {source, path[]}` instead of `{source, column}`
- a single expression in `Fuzzy.field`
- `Unnest` and `Aggregator`
- mandatory `group_by` (usually `[]`)

Every compile uses a validate–repair loop. Invalid JSON, AST-shape errors, bad
aliases, unknown nested paths, illegal joins, modality mismatches, and grouping
errors are returned to the model with exact JSON paths for up to three attempts.

## Legal request router

Before cache lookup or NL→JSON work, Lightning returns `is_legal` and one of
three routes:

- `reject`: unrelated input; returns structured `stage: "relevance"` and makes
  no hosted call.
- `compile`: record retrieval/filtering/counting; runs the existing validated
  natural-language → JSON → BQL path.
- `answer`: legal explanation or guidance that BQL cannot answer; calls hosted
  Super once and returns `{mode: "answer", answer: "..."}` without invoking the
  optimizer or query runtime.

The rejection copy can be changed with `BQL_RELEVANCE_MESSAGE`. Set
`BQL_RELEVANCE_ENABLED=0` only when the gate must be bypassed for diagnostics.

## Frontend/API

The frontend calls the Python API directly; there is no Next.js route. The API
runs the whole pipeline (relevance, compile, typecheck, optimize, execute):

```bash
.venv/bin/python -m api.server
```

Use `BQL_MOCK=1` for deterministic offline UI development. The live compiler
configuration is read at request time:

| variable | default |
|---|---|
| `AMICUS_SCHEMA` | `courtlistener` from `schema.load()`; set `dataform` for the app |
| `AMICUS_HOST` / `AMICUS_PORT` | `127.0.0.1` / `8000` |
| `AMICUS_CORS_ORIGINS` | `http://localhost:3000` |
| `SPARK_HOST` | `172.16.94.53` |
| `RELEVANCE_MODEL` | `nvidia/nemotron-3.5-lightning` on local `:8001` |
| `BQL_RELEVANCE_ENABLED` | `1` |
| `BQL_RELEVANCE_MAX_TOKENS` | `48` |
| `BQL_RELEVANCE_TIMEOUT_S` / `BQL_RELEVANCE_MAX_RETRIES` | `3` / `1` |
| `BQL_REMOTE_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `COMPILER_MODEL` | `nvidia/nemotron-3-super-120b-a12b` |
| `FALLBACK_COMPILER_MODEL` | same as `COMPILER_MODEL` (no silent local fallback) |
| `LEGAL_ANSWER_MODEL` | `nvidia/nemotron-3-super-120b-a12b` |
| `LEGAL_ANSWER_MAX_TOKENS` | `4096` |
| `NVIDIA_API_KEY` | required for live hosted compilation |
| `BQL_ENABLE_THINKING` | `1` |
| `BQL_REASONING_BUDGET` | `16384` |
| `BQL_MAX_TOKENS` | `16384` |
| `BQL_CONTEXT_TOKENS` | `32768` |
| `BQL_TEMPERATURE` / `BQL_TOP_P` | `1` / `0.95` |
| `BQL_MAX_ATTEMPTS` | `3` |
| `BQL_MOCK` | unset |

The local llama-server/Ollama adapters remain available for downstream runtime
models, but compilation no longer probes them or silently downgrades from Super.
