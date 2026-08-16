#!/usr/bin/env bash
# Everything: API and frontend together, one environment, one Ctrl-C.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

trap 'kill 0' EXIT INT TERM
scripts/api.sh & scripts/web.sh &

echo "api  $AMICUS_API_URL   (bound to ${AMICUS_HOST:-0.0.0.0}:$AMICUS_PORT)"
echo "web  http://localhost:$AMICUS_WEB_PORT"
wait
