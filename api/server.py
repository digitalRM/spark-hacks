"""HTTP in front of api.driver.

    scripts/api.sh                            # or: .venv/bin/python -m api.server

    POST /compile   {"question": "...", "schema": "courtlistener"}  -> envelope
    POST /check     {"query": {...}}                                -> validation only
    GET  /schema                                                    -> what the model is told
    GET  /health                                                    -> which stages are live

Thin by construction: each handler is one driver call plus a status code. Anything that
looks like pipeline logic belongs in api/driver.py instead. Every setting comes from
config.py, which reads the repo-root .env.

A compile that fails returns 422 with the same envelope shape as a success, so a caller
parses one thing. Only an operator problem -- schema missing, body unparseable -- returns
4xx/5xx with a bare message.
"""
from __future__ import annotations

from typing import Any

import config
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from runtime import executor

from query_language import compiler, schema
from . import driver

MAX_QUESTION_LENGTH = 4_000

app = FastAPI(title='Amicus', version=driver.BQL_VERSION,
              summary='Natural language to BQL, planned and executed.')

# The browser talks to this API directly, so its origin has to be allowed. Tighten
# AMICUS_CORS_ORIGINS before anything is exposed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=['GET', 'POST'],
    allow_headers=['*'],
)

class CompileRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    schema_name: str | None = Field(default=None, alias='schema')
    no_cache: bool = False
    attempts: int = Field(default=compiler.MAX_ATTEMPTS, ge=1, le=10)
    # False: stop after optimize and let the caller POST the plan to /execute. Lets a UI
    # show the compiled query while the (slow) execution runs.
    execute: bool = True

class CheckRequest(BaseModel):
    query: dict[str, Any]
    schema_name: str | None = Field(default=None, alias='schema')

class ExecuteRequest(BaseModel):
    plan: dict[str, Any]

def registry(name: str | None):
    """Load a schema, turning a bad name into a 400 rather than a stack trace."""
    try: return schema.load(name)
    except FileNotFoundError as e: raise HTTPException(400, str(e)) from None

@app.post('/compile')
def compile_question(req: CompileRequest) -> JSONResponse:
    """Run one question through the whole pipeline. 200 when it compiled, 422 when it did not."""
    reg = registry(req.schema_name)
    out = driver.run(req.question, schema_name=reg.name, use_cache=not req.no_cache,
                     max_attempts=req.attempts, execute=req.execute)
    return JSONResponse(out, status_code=200 if out['ok'] else 422)

@app.post('/execute')
def execute_plan(req: ExecuteRequest) -> JSONResponse:
    """Run a plan from a previous /compile (its `plan` field). 200 with `results` when the
    executor ran, 422 with a `message` when it could not."""
    out = driver.execute_plan(req.plan)
    return JSONResponse(out, status_code=200 if out['ok'] else 422)

@app.post('/check')
def check_query(req: CheckRequest) -> JSONResponse:
    """Decode, schema-check and typecheck a hand-written query. No model call."""
    reg = registry(req.schema_name)
    query, errors = driver.check(req.query, reg)
    body = {'ok': not errors, 'schema': reg.name, 'errors': errors,
            'bql': driver.printed(query) if query is not None and not errors else ''}
    return JSONResponse(body, status_code=200 if not errors else 422)

@app.get('/schema')
def get_schema(name: str | None = None) -> dict[str, Any]:
    """The schema as the compiler renders it into the prompt, plus its raw form."""
    reg = registry(name)
    return {'name': reg.name, 'available': schema.available(),
            'rendered': reg.render_for_prompt(), 'schema': reg.to_dict()}

@app.get('/health')
def health() -> dict[str, Any]:
    """Which stages are actually wired. `stub` here is the honest answer, not an error."""
    return {'status': 'ok', 'bql_version': driver.BQL_VERSION,
            'schemas': schema.available(), 'model': config.MODEL, 'mock': config.MOCK,
            'stages': {'relevance': 'live', 'compile': 'live', 'typecheck': 'live',
                       'optimize': 'live',
                       'execute': 'stub' if executor.STUB else 'live'}}

def serve() -> None:
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)

if __name__ == '__main__':
    serve()
