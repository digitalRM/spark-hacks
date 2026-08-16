#!/usr/bin/env bash
# The Next.js app. Next inlines NEXT_PUBLIC_* from this process's environment at build
# time, which is how the browser bundle learns the API's address from the root .env --
# there is no second env file under frontend/.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
export NEXT_PUBLIC_AMICUS_API_URL="$AMICUS_API_URL"
export NEXT_PUBLIC_AMICUS_RUNTIME_URL="${AMICUS_RUNTIME_URL:-}"
exec npm --prefix frontend run dev -- --port "$AMICUS_WEB_PORT"
