# Amicus frontend

The Next.js app is wired to the canonical Python BQL compiler through
`POST /api/compile`. The route runs server-side, so Spark/model credentials are
never exposed to browser JavaScript. Its response includes the AST v2 JSON wire
contract from `query_language/serde.py` plus BQL deterministically printed from
that JSON. The redesigned UI preserves its Summary view and adds Tree, JSON, and
BQL tabs over the same canonical response.

Lightning routes each request before compilation. Record searches keep the
compiled-query interface; legal explanations that do not fit the query language
return a small `Legal Answer` panel populated directly by hosted Nemotron Super;
unrelated requests use the existing error panel.

```bash
cd ..
python3 -m venv .venv
.venv/bin/python -m pip install \
  -r data_ingestion/dataform/requirements.txt \
  -r query_language/requirements.txt

cd frontend
cp .env.example .env.local
npm ci
npm run dev
```

This setup is required on every clone because `.venv` and `.env.local` are not
stored in Git. Set `BQL_MOCK=1` for offline UI work. For live compilation, keep
`BQL_MOCK=0` and put a valid NVIDIA key in `NVIDIA_API_KEY` inside `.env.local`;
that file is ignored by Git. Restart Next.js after editing it. The remaining
model settings are documented in `../query_language/README.md`.

`AMICUS_SCHEMA=dataform` targets the canonical nested entities produced by step
1. Set it to `courtlistener` only for the legacy flat CourtListener schema.
