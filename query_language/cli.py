"""Command line for the query language.

    python3 -m query_language.cli compile "9th Circuit cases with a photo in the record"
    python3 -m query_language.cli compile "..." --json   # the envelope only, for piping
    python3 -m query_language.cli check query.json       # validate a query written by hand
    python3 -m query_language.cli schema                 # what the model is told
    python3 -m query_language.cli models                 # what the Spark actually serves
    python3 -m query_language.cli prompt                 # the full system prompt

`compile --json` writes the handoff envelope: `bql_version`, `schema`, `question`,
the query JSON in the encoding `serde.py` defines, and deterministic BQL rendered
from that JSON. That is what goes downstream.

Exit code is 0 on success and 1 on failure, so it composes in a shell.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import checks, client, compiler, schema, serde
from .ast import pp_query


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="query_language", description="Natural language to BQL.")
    p.add_argument("--schema", default=None, help="schema name or path (default: courtlistener)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compile", help="compile a question")
    c.add_argument("question")
    c.add_argument("--json", action="store_true", help="print the handoff envelope only")
    c.add_argument("--no-cache", action="store_true")
    c.add_argument("--model", default=None)
    c.add_argument("--attempts", type=int, default=compiler.MAX_ATTEMPTS)

    k = sub.add_parser("check", help="validate a BQL query file")
    k.add_argument("path")

    sub.add_parser("schema", help="print the schema as the model sees it")
    sub.add_parser("models", help="list the model ids each server reports")
    sub.add_parser("prompt", help="print the full system prompt")

    a = p.parse_args(argv)
    reg = schema.load(a.schema)

    if a.cmd == "compile":
        return _compile(a, reg)
    if a.cmd == "check":
        return _check(a.path, reg)
    if a.cmd == "schema":
        print(reg.render_for_prompt())
        return 0
    if a.cmd == "models":
        return _models()
    if a.cmd == "prompt":
        print(compiler.build_system_prompt(reg))
        return 0
    return 1


def _compile(a, reg) -> int:
    try:
        r = compiler.compile_question(a.question, registry=reg, use_cache=not a.no_cache,
                                      model=a.model, max_attempts=a.attempts)
    except client.ModelError as e:
        print(f"model unavailable: {e}", file=sys.stderr)
        return 1

    if a.json:
        print(json.dumps(r.envelope() if r.ok else r.error_report(), indent=2))
        return 0 if r.ok else 1

    if r.ok:
        if r.answer is not None:
            print(f"OK  ({r.model}, direct legal answer, {r.latency_ms:.0f} ms)\n")
            print(r.answer)
            return 0
        source = "cache" if r.cached else f"{r.model}, {len(r.attempts)} attempt(s)"
        print(f"OK  ({source}, {r.latency_ms:.0f} ms)\n")
        print(r.printed + "\n")
        for w in r.warnings:
            print(f"NOTE  {w}\n", file=sys.stderr)
        print(json.dumps(r.query, indent=2))
        return 0

    print(f"FAILED  {r.message()}\n", file=sys.stderr)
    for e in r.errors:
        print(f"  {e['path']}  [{e['code']}]  {e['message']}", file=sys.stderr)
    return 1


def _check(path: str, reg) -> int:
    raw = json.loads(Path(path).read_text())
    wire = raw.get("query", raw) if isinstance(raw, dict) else raw
    errors = serde.decode_errors(wire)
    if not errors:
        query = serde.decode(wire)
        errors = checks.validate(query, reg)
        if not errors:
            print("valid\n")
            print(pp_query(query).strip())
            return 0
    print(f"{len(errors)} problem(s):", file=sys.stderr)
    for e in errors:
        print(f"  {e['path']}  [{e['code']}]  {e['message']}", file=sys.stderr)
    return 1


def _models() -> int:
    """Ask the configured endpoint what it serves. This verifies the model id."""
    want = client.COMPILER_MODEL
    if not client.is_local(want):
        try:
            served = client.list_models(model=want)
        except client.ModelError as exc:
            print(f"hosted endpoint unavailable: {exc}", file=sys.stderr)
            return 1
        found = want in served
        print(f"hosted  {client.base_url_for(want)}")
        print(f"COMPILER_MODEL = {want}  {'FOUND' if found else '*** NOT SERVED ***'}")
        return 0 if found else 1

    found = False
    for model, (port, api) in sorted(client.LOCAL_MODELS.items(), key=lambda kv: kv[1][0]):
        try:
            served = client.list_models(model=model)
        except client.ModelError as e:
            print(f"  :{port:<6} {api:<11} down     {model}   ({str(e)[:50]})")
            continue
        for served_id in served:
            mark = "*" if served_id == want else " "
            found = found or served_id == want
            print(f"{mark} :{port:<6} {api:<11} serving  {served_id}")
    print()
    print(f"COMPILER_MODEL = {want}  {'FOUND' if found else '*** NOT SERVED ***'}")
    if not found:
        print("verify the local model id, port, and server process.")
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
