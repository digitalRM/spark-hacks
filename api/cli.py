"""Command line over api.driver.

    python3 -m api.cli compile "9th Circuit cases with a photo in the record"
    python3 -m api.cli compile "..." --json   # the envelope only, for piping
    python3 -m api.cli check query.json       # validate a query written by hand
    python3 -m api.cli schema                 # what the model is told
    python3 -m api.cli models                 # what the endpoint actually serves
    python3 -m api.cli prompt                 # the full system prompt
    python3 -m api.cli config                 # every setting, key redacted

Every command is formatting over one driver call; the pipeline logic lives in
api/driver.py and this file deliberately holds none of it.

`compile --json` writes the driver envelope: the compiler's handoff fields
(`bql_version`, `schema`, `question`, `query`, `bql`) plus `stages`, `plan` and
`results`. Exit code is 0 on success and 1 on failure, so it composes in a shell.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config

from query_language import client, compiler, schema
from . import driver

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog='api.cli', description='Natural language to BQL, and onward.')
    p.add_argument('--schema', default=None, help='schema name or path (default: courtlistener)')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('compile', help='run a question through the pipeline')
    c.add_argument('question')
    c.add_argument('--json', action='store_true', help='print the envelope only')
    c.add_argument('--no-cache', action='store_true')
    c.add_argument('--attempts', type=int, default=compiler.MAX_ATTEMPTS)

    k = sub.add_parser('check', help='validate a BQL query file')
    k.add_argument('path')

    sub.add_parser('schema', help='print the schema as the model sees it')
    sub.add_parser('models', help='list the model ids the endpoint serves')
    sub.add_parser('prompt', help='print the full system prompt')
    sub.add_parser('config', help='print every setting, key redacted')

    a = p.parse_args(argv)
    if a.cmd == 'config': return _print(config.summary())

    reg = schema.load(a.schema)
    if a.cmd == 'compile': return _compile(a, reg)
    if a.cmd == 'check': return _check(a.path, reg)
    if a.cmd == 'schema': return _print(reg.render_for_prompt())
    if a.cmd == 'prompt': return _print(compiler.build_system_prompt(reg))
    if a.cmd == 'models': return _models()
    return 1

def _print(text: str) -> int:
    print(text)
    return 0

def _compile(a, reg) -> int:
    out = driver.run(a.question, schema_name=reg.name, use_cache=not a.no_cache,
                     max_attempts=a.attempts)
    if a.json:
        print(json.dumps(out, indent=2))
        return 0 if out['ok'] else 1

    for stage in out['stages']:
        print(f"  {stage['name']:<10} {stage['status']:<8} {stage['ms']:>8.1f} ms"
              f"  {stage.get('message', '')}".rstrip())
    print()
    if not out['ok']:
        # A relevance rejection carries a message and no structured errors, so lead with
        # the message -- otherwise a gated question prints nothing at all.
        print(out.get('message', 'the pipeline produced no query'), file=sys.stderr)
        for e in out.get('errors', ()):
            print(f"  {e['path']}  [{e['code']}]  {e['message']}", file=sys.stderr)
        return 1

    if out['mode'] == 'answer':
        print(out['answer'])
        return 0

    print(out['bql'] + '\n')
    for w in out.get('warnings', ()):
        print(f'NOTE  {w}\n', file=sys.stderr)
    print(json.dumps(out['query'], indent=2))
    return 0

def _check(path: str, reg) -> int:
    raw = json.loads(Path(path).read_text())
    wire = raw.get('query', raw) if isinstance(raw, dict) else raw
    query, errors = driver.check(wire, reg)
    if not errors and query is not None:
        print('valid\n')
        print(driver.printed(query))
        return 0
    print(f'{len(errors)} problem(s):', file=sys.stderr)
    for e in errors:
        print(f"  {e['path']}  [{e['code']}]  {e['message']}", file=sys.stderr)
    return 1

def _models() -> int:
    """Ask the endpoint what it serves. This verifies the configured model ids."""
    try:
        served = set(client.list_models())
    except client.ModelError as exc:
        print(f'{config.BASE_URL} unavailable: {exc}', file=sys.stderr)
        return 1
    print(config.BASE_URL)
    ok = True
    for name, model in (('AMICUS_MODEL', config.MODEL),
                        ('AMICUS_ROUTER_MODEL', config.ROUTER_MODEL)):
        found = model in served
        ok = ok and found
        print(f"  {name:<20} {model}  {'FOUND' if found else '*** NOT SERVED ***'}")
    return 0 if ok else 1

if __name__ == '__main__':
    raise SystemExit(main())
