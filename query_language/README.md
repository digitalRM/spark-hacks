# Query language — natural language → JSON → BQL

Step 3 routes the input first: record searches go to the JSON/BQL compiler,
explanatory legal questions get a direct prose answer, and unrelated inputs stop
before any compilation. Compiled JSON is the wire contract defined by `ast.py` and
`serde.py`; BQL text is generated from it by the deterministic pretty-printer, never
accepted directly from the model.

| module | does one thing |
|---|---|
| `client.py` | one chat call, OpenAI protocol, one endpoint, one key |
| `relevance.py` | routes a question: `reject` \| `compile` \| `answer` |
| `compiler.py` | natural language → validated AST, with the repair loop |
| `legal_answer.py` | the `answer` route: prose from Super |

None of them decide what runs next — `api/driver.py` does. None of them take callbacks:
tests patch `client.chat`.

```bash
AMICUS_MOCK=1 python3 -m api.cli compile \
  --json "CourtListener opinions discussing qualified immunity"

python3 -m api.cli --schema dataform schema
python3 -m unittest discover -s query_language/tests -t .
```

For the live compiler, put a rotated key in the repo-root `.env`:

```bash
scripts/setup.sh
.venv/bin/python -m api.cli --schema dataform compile \
  "CourtListener opinions discussing qualified immunity" --json
```

The call uses the OpenAI Python SDK against NVIDIA's OpenAI-compatible base URL. It
streams Nemotron reasoning and content separately: reasoning is never printed or
passed to the parser, while final content enters the JSON decode/validate/repair loop.

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

Set `AMICUS_SCHEMA=dataform` in `.env` to compile against this canonical ingestion
model. `courtlistener` remains available for the original flat physical schema.

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

## The router

`relevance.classify` returns one route, and `is_legal` is derived from it rather than
carried alongside it, so the two cannot disagree:

- `reject` — no affirmative legal meaning; nothing downstream runs.
- `compile` — record retrieval/filtering/counting; the NL → JSON → BQL path.
- `answer` — a legal question BQL cannot express; one Super call for prose, and
  neither the optimizer nor the runtime is involved.

It runs on `AMICUS_ROUTER_MODEL`, which defaults to `AMICUS_MODEL` — hosted Super.
Point it at something cheaper only if the routing bill starts to matter.

## Configuration

Everything is in the repo-root `.env`, read through `config.py`; `.env.example`
documents it and `python -m api.cli config` prints what a process resolved. The
knobs that are *not* environment variables — temperature, top-p, token budgets,
attempt counts, timeouts — are constants in the module that owns them, because they
are properties of the prompt and the model rather than of a machine.
