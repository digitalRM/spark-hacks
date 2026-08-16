#!/usr/bin/env bash
# One-time setup on a fresh clone: virtualenv, Python deps, npm deps, .env.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
npm --prefix frontend ci

echo
.venv/bin/python -m api.cli config
echo
echo "next: put NVIDIA_API_KEY in .env, then scripts/dev.sh"
