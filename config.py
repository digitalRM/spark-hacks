"""Every environment variable Amicus reads, in one place.

One rule: anything Amicus owns is `AMICUS_*`; a vendor credential keeps its vendor's
name (`NVIDIA_API_KEY`), because that is what build.nvidia.com tells you to export.
Nothing else in the codebase calls `os.environ` -- if a value is configurable it is a
constant here, and if it is not here it is a constant in the module that uses it.

`.env` in the repo root is the single source of truth for every process. Python reads
it through this module; `scripts/*.sh` export the same file into the API and Next.js
processes, so `AMICUS_API_URL` reaches the browser bundle as `NEXT_PUBLIC_AMICUS_API_URL`
without a second file to keep in sync. Real environment variables always win over `.env`.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')          # never overrides an already-exported value
except ImportError:                     # python-dotenv is a dependency, not a hard one
    pass

def _str(name: str, default: str) -> str:
    return (os.environ.get(name) or '').strip() or default

def _int(name: str, default: int) -> int:
    return int(_str(name, str(default)))

def _bool(name: str, default: bool = False) -> bool:
    return _str(name, '1' if default else '0').lower() in {'1', 'true', 'yes', 'on'}

# --------------------------------------------------------------------------- #
# Models. Everything speaks the OpenAI protocol against one endpoint, so there is
# one base URL and one key. ROUTER_MODEL defaults to MODEL: hosted Super handles
# routing as well as compiling unless a cheaper model is named explicitly.
# --------------------------------------------------------------------------- #
BASE_URL = _str('AMICUS_BASE_URL', 'https://integrate.api.nvidia.com/v1').rstrip('/')
API_KEY = _str('NVIDIA_API_KEY', '')
MODEL = _str('AMICUS_MODEL', 'nvidia/nemotron-3-super-120b-a12b')
ROUTER_MODEL = _str('AMICUS_ROUTER_MODEL', MODEL)

# Canned answers, no network, deterministic. For offline UI work and tests.
MOCK = _bool('AMICUS_MOCK')

# One stderr line per model call: where its seconds and tokens went. On by default --
# every call is wall clock someone is waiting through.
TRACE = _bool('AMICUS_TRACE', True)

# Which corpus the compiler targets: `dataform` (canonical nested entities) or
# `courtlistener` (the flat physical schema).
SCHEMA = _str('AMICUS_SCHEMA', 'courtlistener')

# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #
HOST = _str('AMICUS_HOST', '0.0.0.0')
PORT = _int('AMICUS_PORT', 8000)
WEB_PORT = _int('AMICUS_WEB_PORT', 3000)

# Where the browser reaches the API. Not derived from HOST/PORT: the API binds
# 0.0.0.0 and the browser needs a routable name for the box it runs on.
API_URL = _str('AMICUS_API_URL', f'http://127.0.0.1:{PORT}').rstrip('/')

# The origin the browser loads the app from -- what CORS has to allow.
CORS_ORIGINS = [o.strip() for o in
                _str('AMICUS_CORS_ORIGINS', f'http://localhost:{WEB_PORT}').split(',')
                if o.strip()]

def summary() -> str:
    """Every setting, with the key redacted. Printed by `python -m api.cli config`."""
    key = f'set ({API_KEY[:6]}...)' if API_KEY else 'MISSING'
    return '\n'.join(f'  {name:<14} {value}' for name, value in (
        ('base_url', BASE_URL), ('api_key', key), ('model', MODEL),
        ('router_model', ROUTER_MODEL), ('mock', MOCK), ('schema', SCHEMA),
        ('host:port', f'{HOST}:{PORT}'), ('api_url', API_URL),
        ('cors_origins', ', '.join(CORS_ORIGINS)),
    ))

if __name__ == '__main__':
    print(summary())
