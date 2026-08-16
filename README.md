# Amicus

Amicus compiles natural-language questions into a repeatable, inspectable query
language over structured, text, image, document, and audio data.

1. **Data ingestion** normalizes sources into a canonical multimodal dataform.
2. **Frontend** accepts a question and visualizes the compiled query.
3. **Query language** compiles natural language into canonical BQL AST v2.
4. **Optimizer** converts the AST into an execution plan.
5. **Runtime** executes that plan. *(still a stub)*

## The pipeline

```text
data_ingestion.dataform
        │  schemas/dataform.json
        ▼
question ─► router ─┬─ reject  ──────────────────────► rejection
                    ├─ answer  ─► Super prose ───────► frontend
                    └─ compile ─► Super JSON ─► AST v2 ─► typecheck ─► plan ─► results
```

Every model call — routing, compiling, direct answers — goes to hosted Nemotron
Super over the OpenAI protocol. One endpoint, one key, one dialect.

`api/driver.py` owns the pipeline and the routing decision; every other module does
one job and does not know what runs before or after it.

## Setup

```bash
scripts/setup.sh          # venv, Python deps, npm deps, .env from .env.example
```

Then put a rotated NVIDIA key in `.env` (`NVIDIA_API_KEY`, from build.nvidia.com).

## Run

```bash
scripts/dev.sh            # API and frontend together, one environment
```

| script | runs |
|---|---|
| `scripts/dev.sh` | both, with one Ctrl-C |
| `scripts/api.sh` | the Python API only |
| `scripts/web.sh` | the Next.js app only |
| `scripts/setup.sh` | one-time install |
| `scripts/env.sh` | sourced by the others; loads `.env` |

## Configuration

`.env` in this directory is the only configuration file. Python reads it through
[`config.py`](config.py); the scripts export it into the API and Next.js processes,
which is how `AMICUS_API_URL` reaches the browser as `NEXT_PUBLIC_AMICUS_API_URL`.
A real exported environment variable always beats a value in `.env`.

Everything Amicus owns is `AMICUS_*`; vendor credentials keep their vendor's name.
`.env.example` documents every variable; `python -m api.cli config` prints what a
process actually resolved, key redacted.

Set `AMICUS_MOCK=1` for deterministic offline work: the router and the compiler both
return canned answers and nothing touches the network.

## Verification

```bash
python3 -m unittest discover -s query_language/tests -t .
```

```bash
AMICUS_MOCK=1 .venv/bin/python -m api.cli compile "9th Circuit cases with a photo in the record"
```

```bash
npm --prefix frontend run lint && npm --prefix frontend run build
```

See `docs/DATAFORM.md`, `query_language/README.md`, `api/README.md`, and
`frontend/README.md` for the individual contracts.
