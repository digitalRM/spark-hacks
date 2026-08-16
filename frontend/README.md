# Amicus frontend

The Next.js app talks to the Python API (`api/server.py`) directly — there is no
Next route in between, because the whole pipeline is Python. Model credentials
live in that process and never reach browser JavaScript; the only value this app
holds is the API's URL. The response is the driver envelope: the AST v2 JSON wire
contract from `query_language/serde.py`, BQL deterministically printed from that
JSON, and per-stage reporting for relevance, compile, typecheck, optimize and
execute. The redesigned UI preserves its Summary view and adds Tree, JSON, and
BQL tabs over the same canonical response.

Lightning routes each request before compilation. Record searches keep the
compiled-query interface; legal explanations that do not fit the query language
return a small `Legal Answer` panel populated directly by hosted Nemotron Super;
unrelated requests use the existing error panel.

Start the API first, then the app:

```bash
cd ..
python3 -m venv .venv
.venv/bin/python -m pip install \
  -r requirements.txt \
  -r data_ingestion/dataform/requirements.txt
.venv/bin/python -m api.server

cd frontend
cp .env.example .env.local
npm ci
npm run dev
```

This setup is required on every clone because `.venv` and `.env.local` are not
stored in Git. `AMICUS_CORS_ORIGINS` on the API must include this app's origin
(`http://localhost:3000` by default).

Set `BQL_MOCK=1` **in the API's environment** for offline UI work — the gate and
the compiler both return canned answers. For live compilation keep `BQL_MOCK=0`
and put a valid NVIDIA key in `NVIDIA_API_KEY`. Those settings are no longer read
from `.env.local`, because Python is no longer launched by this app; they belong
to the API process and are documented in `../.env.example` and
`../query_language/README.md`.

`AMICUS_SCHEMA=dataform` targets the canonical nested entities produced by step
1. Set it to `courtlistener` only for the flat physical CourtListener schema.
