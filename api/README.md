# api

The pipeline, and the only HTTP surface. The frontend talks to this directly; there is
no Next.js route in between.

```text
question ──[compile]──▶ BQL AST ──[typecheck]──▶ schema-resolved query
         ──[optimize]─▶ ExecutionPlan ──[execute]──▶ results + telemetry
```

| file | holds |
|---|---|
| `driver.py` | all pipeline logic — stages, seams, the envelope |
| `cli.py` | argparse over `driver`, no logic of its own |
| `server.py` | FastAPI over `driver`, no logic of its own |

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m api.server
```

```bash
BQL_MOCK=1 .venv/bin/python -m api.cli compile "9th Circuit cases with a photo in the record"
```

`BQL_MOCK=1` makes the compiler deterministic and offline. Compiler and model
configuration (`NVIDIA_API_KEY`, `COMPILER_MODEL`, `AMICUS_SCHEMA`, …) is read by this
process — see the table in `query_language/README.md`.

| variable | default |
|---|---|
| `AMICUS_HOST` / `AMICUS_PORT` | `127.0.0.1` / `8000` |
| `AMICUS_CORS_ORIGINS` | `http://localhost:3000` |

## Endpoints

| route | does |
|---|---|
| `POST /compile` | `{question, schema?}` → envelope. 200 compiled, 422 did not |
| `POST /check` | `{query}` → decode + schema-check + typecheck, no model call |
| `GET /schema` | the schema as the compiler renders it into the prompt |
| `GET /health` | which stages are live and which are still stubs |

## Stages and seams

Compile and typecheck are live. Optimize and execute are **seams**: `driver` imports them
lazily and reports `stub` until the module exists, so the pipeline stays runnable while
they are being written.

| stage | module | must export |
|---|---|---|
| optimize | `optimizer/optimizer.py` | `optimize(ast, schema) -> plan`, `to_json(plan) -> dict` |
| execute | `runtime/executor.py` | `execute(plan) -> dict` |

A stub module declares `STUB = True` so `/health` can tell "exists" from "implemented"
without calling it. Delete that line when the module does the work.

`ok` on the envelope reports the **compile** stage only. That is what the frontend gates
on, and it must not start failing the day the executor lands and errors on a hard query.
