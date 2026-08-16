#!/usr/bin/env bash
# Load .env from the repo root into this shell, without overriding anything already
# exported. Sourced by every other script; not meant to be run on its own.
#
# This is the shell half of config.py: same file, same precedence, so the API, the
# frontend and the CLI cannot disagree about what is configured.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "no .env -- copying .env.example (add NVIDIA_API_KEY to it)" >&2
  cp .env.example .env
fi

while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|'#'*) continue ;; esac
  case "$line" in *=*) ;; *) continue ;; esac
  name="${line%%=*}"
  case "$name" in *[!A-Za-z0-9_]*) continue ;; esac
  if [ -n "${!name:-}" ]; then continue; fi   # a real environment variable wins
  export "$name=${line#*=}"
done < .env

: "${AMICUS_PORT:=8000}"
: "${AMICUS_WEB_PORT:=3000}"
: "${AMICUS_API_URL:=http://127.0.0.1:$AMICUS_PORT}"
export AMICUS_PORT AMICUS_WEB_PORT AMICUS_API_URL

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
export PYTHON
