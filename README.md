# Amicus

Amicus compiles natural-language questions into a repeatable, inspectable query
language over structured, text, image, document, and audio data.

1. **Data ingestion** normalizes sources into a canonical multimodal dataform.
2. **Frontend** accepts a question and visualizes the compiled query.
3. **Query language** compiles natural language into canonical BQL AST v2.
4. **Optimizer** converts the AST into an execution plan.
5. **Runtime** executes that plan.

## Current step 1 → 2 → 3 connection

The `dataform` schema maps nested fields from
`data_ingestion/dataform/models.py` directly to `FieldRef.path` in
`query_language/ast.py`. The frontend's `POST /api/compile` route invokes the
Python compiler server-side. Nemotron Super produces JSON, the compiler validates
it, and a deterministic pretty-printer converts the validated JSON to BQL. The UI
shows both forms alongside tree and plan views.

```text
data_ingestion.dataform
        │  schemas/dataform.json
        ▼
Lightning :8001 relevance gate ──► hosted Nemotron Super
                                      │
                                      ▼
                         validated AST v2 JSON ──► BQL printer ──► frontend
```

## Local setup

Run this setup on every machine after cloning. `.venv` and `.env.local` are
intentionally ignored by Git and therefore do not arrive with `git pull`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install \
  -r data_ingestion/dataform/requirements.txt \
  -r query_language/requirements.txt

cd frontend
cp .env.example .env.local
npm ci
npm run dev
```

The example environment targets hosted Nemotron Super. Add a rotated NVIDIA key
to `NVIDIA_API_KEY` in the ignored `frontend/.env.local` before starting Next.js.
Set `BQL_MOCK=1` when you want the full frontend/compiler path to run without a
live model. Restart `npm run dev` after changing any environment value.

## Verification

```bash
python3 -m unittest discover -s query_language/tests -t .
.venv/bin/python -m data_ingestion.dataform.models
.venv/bin/python -m data_ingestion.dataform.store

cd frontend
npm run lint
npm run build
```

See `docs/DATAFORM.md`, `query_language/README.md`, and `frontend/README.md` for
the individual contracts and configuration details.
