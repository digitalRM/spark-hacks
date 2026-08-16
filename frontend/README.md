# Amicus frontend

The Next.js app is wired to the canonical Python BQL compiler through
`POST /api/compile`. The route runs server-side, so Spark/model credentials are
never exposed to browser JavaScript. Its response includes the AST v2 JSON wire
contract from `query_language/serde.py` plus BQL deterministically printed from
that JSON. The UI displays JSON first, with BQL, JSON Tree, and Plan tabs.

```bash
cp .env.example .env.local
npm ci
npm run dev
```

Set `BQL_MOCK=1` for offline UI work. For live compilation, keep `BQL_MOCK=0`
and put a rotated NVIDIA key in `NVIDIA_API_KEY` inside `.env.local`; that file
is ignored by Git. The remaining model settings are documented in
`../query_language/README.md`.

`AMICUS_SCHEMA=dataform` targets the canonical nested entities produced by step
1. Set it to `courtlistener` only for the legacy flat CourtListener schema.
