# Query language — natural language → JSON → BQL

Step 3 sends a question to hosted Nemotron Super, validates/repairs the returned
JSON, and renders that canonical AST as readable BQL. The JSON is the wire
contract defined by `ast.py` and `serde.py`; BQL text is generated from it by the
deterministic pretty-printer, never accepted directly from the model.

```bash
BQL_MOCK=1 python3 -m query_language.cli compile \
  --json "CourtListener opinions discussing qualified immunity"

python3 -m query_language.cli --schema dataform schema
python3 -m unittest discover -s query_language/tests -t .
```

For the live cloud compiler:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r query_language/requirements.txt
export NVIDIA_API_KEY=nvapi-your-rotated-key
.venv/bin/python -m query_language.cli --schema dataform compile \
  "CourtListener opinions discussing qualified immunity" --json
```

The cloud call uses the OpenAI Python SDK with NVIDIA's OpenAI-compatible base
URL. It streams Nemotron reasoning and content separately: reasoning is never
printed or passed to the parser, while final content enters the JSON
decode/validate/repair loop.

Global CLI options such as `--schema` go before the subcommand:

```bash
python3 -m query_language.cli --schema dataform compile "Count documents by type" --json
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

Set `AMICUS_SCHEMA=dataform` in `frontend/.env.local` to make the frontend route
compile against this canonical ingestion model. `courtlistener` remains
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

## Frontend/API

The Next.js route `POST /api/compile` invokes:

```bash
python3 -m query_language.cli --schema "$AMICUS_SCHEMA" compile "$QUESTION" --json
```

Use `BQL_MOCK=1` for deterministic offline UI development. The live compiler
configuration is read at request time:

| variable | default |
|---|---|
| `AMICUS_SCHEMA` | `courtlistener` in the CLI; `dataform` in the frontend route |
| `BQL_REMOTE_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `COMPILER_MODEL` | `nvidia/nemotron-3-super-120b-a12b` |
| `FALLBACK_COMPILER_MODEL` | same as `COMPILER_MODEL` (no silent local fallback) |
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
