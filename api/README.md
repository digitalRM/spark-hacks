# api

The pipeline, and the only HTTP surface. The frontend talks to this directly; there is
no Next.js route in between.

```text
question ──[route]──▶ reject | answer | compile
           compile ──▶ BQL AST ──[typecheck]──▶ schema-resolved query
                   ──▶ ExecutionPlan ──[execute]──▶ results + telemetry
```

| file | holds |
|---|---|
| `driver.py` | all pipeline logic — routing, stages, the envelope |
| `cli.py` | argparse over `driver`, no logic of its own |
| `server.py` | FastAPI over `driver`, no logic of its own |

## Run

```bash
scripts/api.sh
```

```bash
AMICUS_MOCK=1 .venv/bin/python -m api.cli compile "9th Circuit cases with a photo in the record"
```

`AMICUS_MOCK=1` makes the router and compiler deterministic and offline. Everything
else is configured in the repo-root `.env` and read through `config.py`; see
`.env.example`, or run `python -m api.cli config` to print what this process resolved.

## Endpoints

| route | does |
|---|---|
| `POST /compile` | `{question, schema?}` → envelope. 200 answered, 422 did not |
| `POST /check` | `{query}` → decode + schema-check + typecheck, no model call |
| `GET /schema` | the schema as the compiler renders it into the prompt |
| `GET /health` | which stages are live and which are still stubs |

## The envelope

`mode` is the discriminant, and it comes straight from the router:

| mode | shape |
|---|---|
| `compile` | `query` (AST v2 wire JSON), `bql`, `plan`, `results` |
| `answer` | `answer` — prose from Super; no query, no plan |
| `reject` | `message` — nothing downstream ran |

`ok` means the question produced an answer: a compiled query or direct prose. It says
nothing about the later stages, and it must not start failing the day the executor
lands and errors on a hard query — a typecheck, optimize or execute failure is
reported on its own stage, not on `ok`.

`stages` reports every stage in order with `status` in `ok | failed | stub | skipped`.
`stub` means the module behind it is not written yet (today: `runtime/executor.py`);
`skipped` means an earlier stage made it meaningless.
