# Amicus frontend

The Next.js app talks to the Python API (`api/server.py`) directly — there is no
Next route in between, because the whole pipeline is Python. Model credentials live
in that process and never reach browser JavaScript; the only value this app holds is
the API's URL. The response is the driver envelope: the AST v2 JSON wire contract
from `query_language/serde.py`, BQL deterministically printed from that JSON, and
per-stage reporting for relevance, compile, typecheck, optimize and execute. The UI
keeps its Summary view and adds Tree, JSON, and BQL tabs over the same response.

The router sorts each request before compilation. Record searches keep the
compiled-query interface; legal explanations that do not fit the query language
return a small `Legal Answer` panel; unrelated requests use the error panel.

## Run

```bash
../scripts/dev.sh      # API + this app, one environment
```

or this app alone, against an API that is already up:

```bash
../scripts/web.sh
```

## Configuration

There is no env file under `frontend/`. The repo-root `.env` is the only one:
`scripts/web.sh` exports `AMICUS_API_URL` into this process as
`NEXT_PUBLIC_AMICUS_API_URL`, and Next inlines it into the browser bundle at build
time. Running `npm run dev` by hand skips that step and falls back to
`http://127.0.0.1:8000`.

| what the browser uses | comes from |
|---|---|
| `NEXT_PUBLIC_AMICUS_API_URL` | `AMICUS_API_URL` in `../.env` |
| `NEXT_PUBLIC_AMICUS_RUNTIME_URL` | `AMICUS_RUNTIME_URL` in `../.env`; unset means bundled fixtures |

`AMICUS_CORS_ORIGINS` on the API must include this app's origin — it defaults to
`http://localhost:$AMICUS_WEB_PORT`, so it already does unless you moved the port.

Everything else — `NVIDIA_API_KEY`, `AMICUS_MODEL`, `AMICUS_SCHEMA`, `AMICUS_MOCK` —
belongs to the API process. Set `AMICUS_MOCK=1` there for offline UI work.
